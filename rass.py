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

VK_DOMAINS = ["chp174"]
CHECK_INTERVAL = 60
STATE_FILE = DATA_DIR / "rass_state.json"

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
broadcast_list_id: int
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


def get_latest_posts(domain: str, count: int = 5) -> list:
    url = "https://api.vk.com/method/wall.get"
    params = {
        "domain": domain,
        "count": count,
        "access_token": vk_token,
        "v": "5.193",
    }
    try:
        response = requests.get(url, params=params, timeout=15).json()
        return response.get("response", {}).get("items", [])
    except Exception as exc:
        logger.warning("Ошибка ВК (%s): %s", domain, exc)
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
    return f"{text}\n\nВыберите действие:"


def build_action_keyboard(send_id: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("Отправить", callback_data=f"vk_send:{send_id}"),
    )
    markup.row(
        types.InlineKeyboardButton(
            "Изменить текст рассылки", callback_data=f"vk_edit:{send_id}"
        ),
        types.InlineKeyboardButton("Отменить", callback_data=f"vk_cancel:{send_id}"),
    )
    return markup


def send_preview_to_telegram(
    short_summary: str,
    post_url: str,
    media_url: str,
    domain: str,
) -> None:
    send_id = uuid.uuid4().hex[:12]
    pending_sends[send_id] = {
        "push_text": short_summary,
        "post_url": post_url,
        "photo_url": media_url,
        "message_id": None,
    }

    try:
        sent = bot.send_photo(
            admin_chat_id,
            media_url,
            caption=build_preview_caption(pending_sends[send_id]),
            reply_markup=build_action_keyboard(send_id),
        )
        pending_sends[send_id]["message_id"] = sent.message_id
        logger.info("[%s] Превью отправлено: %s", domain, short_summary)
    except Exception as exc:
        pending_sends.pop(send_id, None)
        logger.error("Ошибка Telegram: %s", exc)


def register_handlers():
    @bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("vk_send:"))
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

        broadcast_text = build_broadcast_text(
            payload["push_text"], payload["post_url"], broadcast_group_id
        )

        bot.answer_callback_query(call.id, "Загружаю фото и отправляю…")

        try:
            result = send_broadcast(
                api_token=broadcast_api_token,
                list_id=broadcast_list_id,
                text=broadcast_text,
                vk_access_token=vk_community_token,
                group_id=broadcast_group_id,
                photo_url=photo_url,
            )
            broadcast_id = result.get("response", {}).get("id", "?")
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
                f"✅ Рассылка с фото запущена, ID {broadcast_id}.\n\n{broadcast_text}",
                disable_web_page_preview=True,
            )
            logger.info("VK рассылка %s запущена", broadcast_id)
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

    @bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("vk_edit:"))
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

    @bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("vk_cancel:"))
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
                    f"{build_broadcast_text(payload['push_text'], payload['post_url'], broadcast_group_id)}\n\n"
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
                    reply_markup=build_action_keyboard(send_id),
                )
            bot.send_message(message.chat.id, "✏️ Текст рассылки обновлён.")
        except Exception as exc:
            logger.error("Не удалось обновить превью: %s", exc)
            bot.send_message(
                message.chat.id,
                f"Текст сохранён, но превью не обновилось: {exc}\n\n{caption}",
            )


def establish_baseline() -> dict[str, int]:
    baseline: dict[str, int] = {}
    for domain in VK_DOMAINS:
        posts = get_latest_posts(domain)
        if posts:
            baseline[domain] = max(post["id"] for post in posts)
            logger.info("[%s] Старт: последний post_id=%s, старые посты пропускаем", domain, baseline[domain])
        else:
            baseline[domain] = 0
            logger.warning("[%s] Старт: постов не получено, id=0", domain)
        time.sleep(1)
    save_state(baseline)
    return baseline


def monitor_loop():
    logger.info("Мониторинг VK: %s", ", ".join(VK_DOMAINS))
    last_seen_ids = establish_baseline()

    while True:
        for domain in VK_DOMAINS:
            current_posts = get_latest_posts(domain)
            if not current_posts:
                continue

            new_posts = [p for p in current_posts if p["id"] > last_seen_ids.get(domain, 0)]
            if not new_posts:
                continue

            new_posts.sort(key=lambda x: x["id"])
            for post in new_posts:
                post_text = post.get("text", "")
                if "erid" in post_text.lower():
                    logger.info("[%s] Пропущен рекламный пост (id=%s)", domain, post["id"])
                    continue

                post_id = post["id"]
                owner_id = post["owner_id"]
                post_url = f"https://vk.com/wall{owner_id}_{post_id}"
                short_summary = get_gemini_summary(post_text)
                media_url = get_media_url(post)
                if not media_url:
                    logger.info("[%s] Пропущен пост без фото (id=%s)", domain, post_id)
                    continue
                send_preview_to_telegram(short_summary, post_url, media_url, domain)

            last_seen_ids[domain] = max(post["id"] for post in current_posts)
            save_state(last_seen_ids)
            time.sleep(2)

        time.sleep(CHECK_INTERVAL)


def main():
    global bot, gemini_client, vk_token, vk_community_token, admin_chat_id
    global broadcast_api_token, broadcast_list_id, broadcast_group_id

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bot = telebot.TeleBot(env("TG_BOT_TOKEN_RASS"))
    gemini_client = genai.Client(api_key=env("GEMINI_API_KEY_RASS"))
    vk_token = env("VK_TOKEN")
    vk_community_token = env("VK_COMMUNITY_TOKEN")
    admin_chat_id = env("ADMIN_CHAT_ID")
    broadcast_api_token = env("VK_BROADCAST_API_TOKEN")
    broadcast_list_id = int(env("VK_BROADCAST_LIST_ID"))
    broadcast_group_id = int(env("VK_BROADCAST_GROUP_ID"))

    register_handlers()
    threading.Thread(target=monitor_loop, daemon=True).start()
    logger.info(
        "Rass-бот: Gemini=%s, рассылка VK list_id=%s. Ctrl+C — выход.",
        GEMINI_MODEL,
        broadcast_list_id,
    )
    bot.infinity_polling()


if __name__ == "__main__":
    main()
