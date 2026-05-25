import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import requests
import telebot
from google import genai
from telebot import types

from config import DATA_DIR, GEMINI_MODEL, env
from core.vk_broadcast import send_broadcast
from core.vk_photo import PhotoUploadFailedError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

VK_OWNER_ID = -87721351
CHECK_INTERVAL = 60
STATE_FILE = DATA_DIR / "rass_nch_state.json"

# Фразы в тексте поста → id списка рассылки (как в приложении VK)
BROADCAST_LISTS: list[dict] = [
    {
        "list_id": 1424009,
        "phrase": "Новости об экологии",
        "label": "Новости об экологии",
    },
    {
        "list_id": 1424063,
        "phrase": "Новости ЖКХ",
        "label": "Новости ЖКХ",
    },
    {
        "list_id": 1424062,
        "phrase": "Происшествия",
        "label": "Происшествия",
    },
]

PROMPT_TEMPLATE = """Перед тобой текст новостного поста. Сформулируй одно короткое цепляющее предложение (до 23 символов) для push-рассылки, которое вызовет интерес и побудит перейти по ссылке.
Требования:
- Без точки в конце
- Конкретно и по делу (суть новости)
- Без кликбейта и преувеличений
- В разговорном новостном стиле
- Можно использовать глаголы действия
- Избегай общих фраз
Ориентируйся на примеры:
суд над подростками, перекроют центр, сроки опрессовки, кинопоказ в парке, вручили медаль, потратят на салют, напали на пешехода, пополнение в зоопарке, отключат интернет, увидим звездопад
Ответ: только одна фраза без пояснений

Текст поста:
{post_text}"""

pending_sends: dict[str, dict] = {}
editing_sessions: dict[int, str] = {}

bot: telebot.TeleBot
gemini_client: genai.Client
vk_token: str
vk_community_token: str
admin_chat_id: str
broadcast_api_token: str
broadcast_group_id: int


def load_state() -> dict[str, int]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def detect_list_from_post_text(post_text: str) -> dict | None:
    """Выбирает рассылку по фразе из текста поста на стене."""
    text_lower = post_text.lower()
    matches = [
        entry
        for entry in BROADCAST_LISTS
        if entry["phrase"].lower() in text_lower
    ]
    if not matches:
        return None
    # При нескольких совпадениях — самая длинная фраза (точнее)
    return max(matches, key=lambda e: len(e["phrase"]))


def list_label_by_id(list_id: int) -> str:
    for entry in BROADCAST_LISTS:
        if entry["list_id"] == list_id:
            return entry["label"]
    return str(list_id)


def get_gemini_summary(text: str) -> str:
    if not text:
        return "Новый пост"
    for attempt in range(3):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=PROMPT_TEMPLATE.format(post_text=text),
            )
            return response.text.strip().rstrip(".")
        except Exception as exc:
            if "503" in str(exc) or "UNAVAILABLE" in str(exc):
                time.sleep(5 * (attempt + 1))
                continue
            logger.warning("Gemini: %s", exc)
            break
    return "Интересные новости"


def get_latest_posts(owner_id: int, count: int = 5) -> list:
    url = "https://api.vk.com/method/wall.get"
    params = {
        "owner_id": owner_id,
        "count": count,
        "access_token": vk_token,
        "v": "5.193",
    }
    try:
        response = requests.get(url, params=params, timeout=15).json()
        return response.get("response", {}).get("items", [])
    except Exception as exc:
        logger.warning("Ошибка ВК (owner_id=%s): %s", owner_id, exc)
        return []


def get_media_url(post: dict) -> str | None:
    from core.vk_photo import pick_vk_photo_url

    for att in post.get("attachments", []):
        if att["type"] == "photo":
            return pick_vk_photo_url(att["photo"].get("sizes", []))
        if att["type"] == "video":
            images = att["video"].get("image", [])
            if images:
                return pick_vk_photo_url(images) or images[-1].get("url")
    return None


def mailing_manage_url(community_group_id: int) -> str:
    return f"https://vk.com/app5748831_-{community_group_id}"


def build_broadcast_text(push_text: str, post_url: str, community_group_id: int) -> str:
    push_text = push_text.strip().rstrip(".")
    return (
        f"{push_text}. Подробнее: {post_url}\n\n"
        f"📝 Управление рассылками: {mailing_manage_url(community_group_id)}"
    )


def build_preview_caption(payload: dict) -> str:
    text = build_broadcast_text(
        payload["push_text"], payload["post_url"], broadcast_group_id
    )
    list_id = payload.get("list_id")
    if list_id:
        list_label = payload.get("list_label") or list_label_by_id(list_id)
        header = f"📬 Рассылка: {list_label} (id {list_id})\n\n"
    else:
        header = (
            "⚠️ Рассылка не определена — выберите вручную или отмените.\n\n"
        )
    return f"{header}{text}\n\nВыберите действие:"


def build_action_keyboard(send_id: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("Отправить", callback_data=f"nch_send:{send_id}"),
    )
    markup.row(
        types.InlineKeyboardButton(
            "Изменить текст рассылки", callback_data=f"nch_edit:{send_id}"
        ),
        types.InlineKeyboardButton(
            "Изменить рассылку", callback_data=f"nch_chlist:{send_id}"
        ),
    )
    markup.row(
        types.InlineKeyboardButton("Отменить", callback_data=f"nch_cancel:{send_id}"),
    )
    return markup


def build_list_pick_keyboard(
    send_id: str, *, with_back: bool = True
) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    for entry in BROADCAST_LISTS:
        markup.row(
            types.InlineKeyboardButton(
                f"{entry['label']} ({entry['list_id']})",
                callback_data=f"nch_list:{send_id}:{entry['list_id']}",
            )
        )
    if with_back:
        markup.row(
            types.InlineKeyboardButton("« Назад", callback_data=f"nch_back:{send_id}"),
        )
    else:
        markup.row(
            types.InlineKeyboardButton("Отменить", callback_data=f"nch_cancel:{send_id}"),
        )
    return markup


def preview_reply_markup(send_id: str, payload: dict) -> types.InlineKeyboardMarkup:
    if payload.get("list_id"):
        return build_action_keyboard(send_id)
    return build_list_pick_keyboard(send_id, with_back=False)


def send_preview_to_telegram(
    short_summary: str,
    post_url: str,
    media_url: str,
    list_entry: dict | None,
) -> None:
    send_id = uuid.uuid4().hex[:12]
    pending_sends[send_id] = {
        "push_text": short_summary,
        "post_url": post_url,
        "photo_url": media_url,
        "list_id": list_entry["list_id"] if list_entry else None,
        "list_label": list_entry["label"] if list_entry else None,
        "message_id": None,
    }

    try:
        sent = bot.send_photo(
            admin_chat_id,
            media_url,
            caption=build_preview_caption(pending_sends[send_id]),
            reply_markup=preview_reply_markup(send_id, pending_sends[send_id]),
        )
        pending_sends[send_id]["message_id"] = sent.message_id
        if list_entry:
            logger.info(
                "[owner_id=%s] Превью: %s → %s (%s)",
                VK_OWNER_ID,
                short_summary,
                list_entry["label"],
                list_entry["list_id"],
            )
        else:
            logger.info(
                "[owner_id=%s] Превью без рассылки (выбор вручную): %s",
                VK_OWNER_ID,
                short_summary,
            )
    except Exception as exc:
        pending_sends.pop(send_id, None)
        logger.error("Ошибка Telegram: %s", exc)


def register_handlers():
    @bot.callback_query_handler(
        func=lambda call: call.data and call.data.startswith("nch_send:")
    )
    def on_send_broadcast(call):
        send_id = call.data.split(":", 1)[1]
        payload = pending_sends.pop(send_id, None)
        editing_sessions.pop(call.message.chat.id, None)

        if not payload:
            bot.answer_callback_query(call.id, "Уже отправлено или устарело")
            return

        photo_url = payload.get("photo_url")
        if not photo_url:
            bot.answer_callback_query(call.id, "Нет фото")
            bot.send_message(
                call.message.chat.id,
                "❌ У этого поста нет фото. Рассылка отправляется только с изображением.",
            )
            return

        list_id = payload.get("list_id")
        if not list_id:
            bot.answer_callback_query(call.id, "Сначала выберите рассылку")
            pending_sends[send_id] = payload
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=build_list_pick_keyboard(send_id, with_back=False),
                )
            except Exception as exc:
                logger.warning("Не удалось показать выбор рассылки: %s", exc)
            return

        broadcast_text = build_broadcast_text(
            payload["push_text"], payload["post_url"], broadcast_group_id
        )

        bot.answer_callback_query(call.id, "Загружаю фото и отправляю…")

        try:
            result = send_broadcast(
                api_token=broadcast_api_token,
                list_id=list_id,
                text=broadcast_text,
                vk_access_token=vk_community_token,
                group_id=broadcast_group_id,
                photo_url=photo_url,
            )
            broadcast_id = result.get("response", {}).get("id", "?")
            list_label = payload.get("list_label") or list_label_by_id(list_id)
            try:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=None,
                )
            except Exception as markup_exc:
                logger.warning("Кнопки не убрались с превью: %s", markup_exc)
            bot.send_message(
                call.message.chat.id,
                f"✅ Рассылка «{list_label}» (список {list_id}) с фото запущена, "
                f"ID {broadcast_id}.\n\n{broadcast_text}",
                disable_web_page_preview=True,
            )
            logger.info("VK рассылка %s → list_id=%s", broadcast_id, list_id)
        except PhotoUploadFailedError as exc:
            pending_sends[send_id] = payload
            logger.error("Фото не загрузилось после %s попыток", exc.attempts)
            bot.send_message(
                call.message.chat.id,
                f"❌ Отправить рассылку не удалось: фото не загрузилось на сервер ВК "
                f"после {exc.attempts} попыток.\n\n"
                f"Причина: {exc}\n\n"
                f"Попробуйте нажать «Отправить» ещё раз позже.",
            )
        except Exception as exc:
            pending_sends[send_id] = payload
            logger.exception("Ошибка отправки рассылки")
            bot.send_message(call.message.chat.id, f"❌ Не удалось отправить: {exc}")

    @bot.callback_query_handler(
        func=lambda call: call.data and call.data.startswith("nch_edit:")
    )
    def on_edit_push(call):
        send_id = call.data.split(":", 1)[1]
        if send_id not in pending_sends:
            bot.answer_callback_query(call.id, "Черновик устарел")
            return

        editing_sessions[call.message.chat.id] = send_id
        bot.answer_callback_query(call.id, "Жду новый текст")
        bot.send_message(
            call.message.chat.id,
            "✏️ Пришлите новый текст push-рассылки одним сообщением.\n"
            "Только фраза до «Подробнее» — без ссылки и без второго абзаца.",
        )

    @bot.callback_query_handler(
        func=lambda call: call.data and call.data.startswith("nch_chlist:")
    )
    def on_change_list(call):
        send_id = call.data.split(":", 1)[1]
        if send_id not in pending_sends:
            bot.answer_callback_query(call.id, "Черновик устарел")
            return

        bot.answer_callback_query(call.id, "Выберите рассылку")
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_list_pick_keyboard(send_id),
            )
        except Exception as exc:
            logger.warning("Не удалось показать список рассылок: %s", exc)
            bot.send_message(
                call.message.chat.id,
                "Выберите рассылку:",
                reply_markup=build_list_pick_keyboard(send_id),
            )

    @bot.callback_query_handler(
        func=lambda call: call.data and call.data.startswith("nch_list:")
    )
    def on_list_selected(call):
        parts = call.data.split(":")
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Ошибка данных")
            return

        send_id, list_id_str = parts[1], parts[2]
        payload = pending_sends.get(send_id)
        if not payload:
            bot.answer_callback_query(call.id, "Черновик устарел")
            return

        try:
            list_id = int(list_id_str)
        except ValueError:
            bot.answer_callback_query(call.id, "Неверный id")
            return

        entry = next((e for e in BROADCAST_LISTS if e["list_id"] == list_id), None)
        if not entry:
            bot.answer_callback_query(call.id, "Неизвестная рассылка")
            return

        payload["list_id"] = entry["list_id"]
        payload["list_label"] = entry["label"]
        bot.answer_callback_query(call.id, entry["label"])

        try:
            bot.edit_message_caption(
                build_preview_caption(payload),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_action_keyboard(send_id),
            )
        except Exception as exc:
            logger.warning("Не удалось обновить превью: %s", exc)

    @bot.callback_query_handler(
        func=lambda call: call.data and call.data.startswith("nch_back:")
    )
    def on_list_back(call):
        send_id = call.data.split(":", 1)[1]
        payload = pending_sends.get(send_id)
        if not payload:
            bot.answer_callback_query(call.id, "Черновик устарел")
            return

        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_action_keyboard(send_id),
            )
        except Exception as exc:
            logger.warning("Не удалось вернуть кнопки: %s", exc)

    @bot.callback_query_handler(
        func=lambda call: call.data and call.data.startswith("nch_cancel:")
    )
    def on_cancel_broadcast(call):
        send_id = call.data.split(":", 1)[1]
        payload = pending_sends.pop(send_id, None)
        editing_sessions.pop(call.message.chat.id, None)

        bot.answer_callback_query(call.id, "Отменено")

        if not payload:
            return

        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None,
            )
            bot.edit_message_caption(
                (
                    f"{build_preview_caption(payload)}\n\n"
                    "❌ Рассылка отменена."
                ),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        except Exception as exc:
            logger.warning("Не удалось обновить превью после отмены: %s", exc)

        logger.info("Рассылка отменена пользователем")

    @bot.message_handler(content_types=["text"])
    def on_text_message(message):
        if str(message.chat.id) != str(admin_chat_id):
            return

        send_id = editing_sessions.pop(message.chat.id, None)
        if not send_id:
            return

        payload = pending_sends.get(send_id)
        if not payload:
            bot.send_message(message.chat.id, "Черновик устарел — начните с нового поста.")
            return

        payload["push_text"] = message.text.strip().rstrip(".")
        caption = build_preview_caption(payload)
        msg_id = payload.get("message_id")

        try:
            if msg_id:
                bot.edit_message_caption(
                    caption,
                    chat_id=message.chat.id,
                    message_id=msg_id,
                    reply_markup=preview_reply_markup(send_id, payload),
                )
            bot.send_message(message.chat.id, "✏️ Текст рассылки обновлён.")
        except Exception as exc:
            logger.error("Не удалось обновить превью: %s", exc)
            bot.send_message(
                message.chat.id,
                f"Текст сохранён, но превью не обновилось: {exc}\n\n{caption}",
            )


def establish_baseline() -> int:
    posts = get_latest_posts(VK_OWNER_ID)
    if posts:
        last_id = max(post["id"] for post in posts)
        logger.info(
            "[owner_id=%s] Старт: последний post_id=%s, старые посты пропускаем",
            VK_OWNER_ID,
            last_id,
        )
    else:
        last_id = 0
        logger.warning("[owner_id=%s] Старт: постов не получено, id=0", VK_OWNER_ID)

    save_state({str(VK_OWNER_ID): last_id})
    return last_id


def monitor_loop():
    logger.info("Мониторинг VK: owner_id=%s", VK_OWNER_ID)
    last_seen_id = establish_baseline()

    while True:
        current_posts = get_latest_posts(VK_OWNER_ID)
        if current_posts:
            new_posts = [p for p in current_posts if p["id"] > last_seen_id]
            if new_posts:
                new_posts.sort(key=lambda x: x["id"])
                for post in new_posts:
                    post_text = post.get("text", "")
                    if "erid" in post_text.lower():
                        logger.info(
                            "[owner_id=%s] Пропущен рекламный пост (id=%s)",
                            VK_OWNER_ID,
                            post["id"],
                        )
                        continue

                    list_entry = detect_list_from_post_text(post_text)

                    post_id = post["id"]
                    owner_id = post["owner_id"]
                    post_url = f"https://vk.com/wall{owner_id}_{post_id}"
                    short_summary = get_gemini_summary(post_text)
                    media_url = get_media_url(post)
                    if not media_url:
                        logger.info(
                            "[owner_id=%s] Пропущен пост без фото (id=%s)",
                            VK_OWNER_ID,
                            post_id,
                        )
                        continue

                    send_preview_to_telegram(
                        short_summary, post_url, media_url, list_entry
                    )

                last_seen_id = max(post["id"] for post in current_posts)
                save_state({str(VK_OWNER_ID): last_seen_id})

        time.sleep(CHECK_INTERVAL)


def main():
    global bot, gemini_client, vk_token, vk_community_token, admin_chat_id
    global broadcast_api_token, broadcast_group_id

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot = telebot.TeleBot(env("TG_BOT_TOKEN_RASS_NCH"))
    gemini_client = genai.Client(api_key=env("GEMINI_API_KEY_RASS"))
    vk_token = env("VK_TOKEN_NCH")
    vk_community_token = env("VK_COMMUNITY_TOKEN_NCH")
    admin_chat_id = env("ADMIN_CHAT_ID")
    broadcast_api_token = env("VK_BROADCAST_API_TOKEN_NCH")
    broadcast_group_id = int(env("VK_BROADCAST_GROUP_ID_NCH"))

    register_handlers()
    threading.Thread(target=monitor_loop, daemon=True).start()
    lists_info = ", ".join(
        f"{e['label']} ({e['list_id']})" for e in BROADCAST_LISTS
    )
    logger.info(
        "Rass-NCH: Gemini=%s, owner_id=%s, рассылки: %s. Ctrl+C — выход.",
        GEMINI_MODEL,
        VK_OWNER_ID,
        lists_info,
    )
    bot.infinity_polling()


if __name__ == "__main__":
    main()
