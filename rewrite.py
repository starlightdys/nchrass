import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import telebot
from google import genai

from config import DATA_DIR, GEMINI_MODEL, env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

NEWSLETTER_LINES = [
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/588719|Хорошие новости]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68382|Новости ЖКХ]!",
    "Важно! Подпишись на рассылку  [https://vk.com/app5748831_-87721351#subscribe/68388|Происшествия]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68374|Новости об экологии!]",
    "Интересно?! Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68473|Важные новости]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68385|Спортивные новости]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68376|Катаклизмы]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68391|Афиша Челябинска]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68389|Новости политики и экономики]!",
    "Важно! Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68384|ХК «Трактор»]!",
    "Интересно? Подпишись на рассылку [https://vk.com/app5748831_-87721351#subscribe/68396|Новости медицины]!",
]

user_cache: dict[int, dict] = {}

bot = telebot.TeleBot(env("TG_BOT_TOKEN_REWRITE"))
client = genai.Client(api_key=env("GEMINI_API_KEY_REWRITE"))
OUTPUT_DIR = DATA_DIR / "rewrites"


def build_prompt(news_text: str) -> str:
    return f"""
Для того чтобы тексты всегда попадали в цель, строго следуй инструкции ниже. Пиши так, как будто рассказываешь новость знакомому — просто, ясно и без официоза. Избегай канцелярита, сложных конструкций и сухого «новостного» языка.

1. Заголовки (5 вариантов)
- Каждый начинается с одного подходящего к новости эмодзи
- До 60 символов с пробелами
- Без двоеточий
- Пиши просто и понятно, без официальных формулировок
- Можно использовать разговорные, но уместные формулировки
- Пример: В Челябинске официально завершили отопительный сезон из-за резкого потепления

2. Текст новости
- Два абзаца, не слишком длинные
- Без воды, домыслов и лишних деталей
- Стиль: короткие, понятные фразы, живой язык

Первый абзац:

- Сразу суть — что произошло, где и почему это важно
- Без длинных вводных и «в рамках», «в связи с тем, что» и т.п.

Второй абзац:

- Детали, последствия, что будет дальше
- Можно добавить контекст, если он помогает понять новость

Цитаты:

- Если есть — делай их естественными, «разговорными»
- Убирай канцелярщину и ошибки
- Не повторяй в цитате то, что уже сказано в тексте
- Если цитаты нет — не придумывай

❗ Избегай:

- «В рамках реализации», «осуществляется», «данный вопрос»
- слишком длинных предложений
- перегруженных формулировок

3. Вопросы для подписчиков (5 вариантов)
- Короткие, интересные, вовлекающие вопросы для наших читателей, относящиеся к новости
- Каждый вопрос должен начинаться с эмодзи вопросительного знака
- Пиши так, чтобы хотелось ответить
- Можно чуть разговорный тон

❗ Строго соблюдай формат:

Заголовки:
1.
2.
3.
4.
5.

Текст новости:
...

Пример:

Заголовки:
1. Студентам хотят запретить менять специальность в магистратуре
2. Смену профессии через магистратуру хотят запретить
3. Южноуральским студентам могут ограничить смену специальности
4. Выпускникам запретят менять направление в магистратуре?
5. Минобр собирается ужесточить правила поступления в магистратуру

Текст новости:
В Минобре предложили ограничить возможность смены профиля при поступлении в магистратуру. По законопроекту, выпускникам бакалавриата разрешат продолжать обучение только по своей специальности.

Если инициативу примут, привычная схема, когда студенты выбирают магистратуру в другой области, станет недоступной. Нововведение может серьезно сократить возможности для смены профессии и переквалификации.

Вопросы:
1. Справедливо ли запрещать смену профессии после бакалавриата?
2. А вы бы хотели сменить направление в магистратуре?
3. Это повысит качество образования или ограничит возможности?
4. Нужно ли «держаться» одной специальности всю учёбу?
5. Как это повлияет на карьеру выпускников?

Если нарушен формат — перепиши ответ заново.
Не добавляй ничего вне структуры.

Вот исходная новость:

{news_text}
"""


def build_newsletter_prompt(rewritten_text: str) -> str:
    newsletter_list = "\n".join(NEWSLETTER_LINES)
    return f"""
Проанализируй текст новости и выбери одну подходящую рассылку из списка ниже.
ВАЖНО: Ты должен вернуть выбранную строку ПОЛНОСТЬЮ И ДОСЛОВНО (вместе с эмодзи, текстом и ссылкой в скобках).
Ничего не сокращай и не добавляй от себя. Только одну готовую строку.

Список для выбора:
{newsletter_list}

Текст новости для анализа:
{rewritten_text}
"""


def parse_ai_response(text: str) -> tuple[list[str], str, list[str]]:
    headlines: list[str] = []
    body = ""
    questions: list[str] = []

    h_match = re.search(r"Заголовки:(.*?)Текст новости:", text, re.S | re.I)
    if h_match:
        headlines = re.findall(r"\d\.\s*(.*)", h_match.group(1))

    b_match = re.search(r"Текст новости:(.*?)Вопросы:", text, re.S | re.I)
    if b_match:
        body = b_match.group(1).strip()

    q_match = re.search(r"Вопросы:(.*)", text, re.S | re.I)
    if q_match:
        questions = re.findall(r"\d\.\s*(.*)", q_match.group(1))

    return headlines[:5], body, questions[:5]


def resolve_newsletter_line(raw_line: str) -> str:
    line = raw_line.strip().strip('"').strip("'")
    if line in NEWSLETTER_LINES:
        return line
    for candidate in NEWSLETTER_LINES:
        if candidate in line or line in candidate:
            return candidate
    match = re.search(r"subscribe/(\d+)", line)
    if match:
        sub_id = match.group(1)
        for candidate in NEWSLETTER_LINES:
            if sub_id in candidate:
                return candidate
    return NEWSLETTER_LINES[4]


def save_output(text: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".txt"
    (OUTPUT_DIR / filename).write_text(text, encoding="utf-8")


def extract_news_text(message) -> str | None:
    """Текст обычного или пересланного сообщения (текст, подпись к медиа)."""
    parts: list[str] = []
    if getattr(message, "text", None) and message.text.strip():
        parts.append(message.text.strip())
    if getattr(message, "caption", None) and message.caption.strip():
        caption = message.caption.strip()
        if caption not in parts:
            parts.append(caption)
    if not parts:
        return None
    return "\n\n".join(parts)


def handle_digit_choice(chat_id: int, text: str) -> bool:
    """Две цифры: первая — заголовок (1–5), вторая — вопрос (1–5) или 0 без вопроса."""
    if len(text) != 2 or not text.isdigit():
        return False

    if chat_id not in user_cache:
        bot.send_message(chat_id, "Сначала пришли новость для обработки!")
        return True

    h_idx = int(text[0]) - 1
    q_digit = int(text[1])
    data = user_cache[chat_id]

    if not (0 <= h_idx < len(data["headlines"])):
        bot.send_message(chat_id, "Ошибка! Первая цифра (заголовок) должна быть от 1 до 5.")
        return True

    chosen_headline = data["headlines"][h_idx]
    news_body = data["body"]
    newsletter = data["newsletter"]

    if q_digit == 0:
        final_text = f"{chosen_headline}\n\n{news_body}\n\n{newsletter}"
    else:
        q_idx = q_digit - 1
        if not (0 <= q_idx < len(data["questions"])):
            bot.send_message(
                chat_id,
                "Ошибка в выборе вопроса! Введи от 1 до 5 или 0 для удаления.",
            )
            return True
        chosen_question = data["questions"][q_idx]
        final_text = (
            f"{chosen_headline}\n\n{news_body}\n\n{chosen_question}\n\n{newsletter}"
        )

    try:
        save_output(final_text)
        bot.send_message(chat_id, final_text, disable_web_page_preview=True)
    except Exception as exc:
        logger.exception("Ошибка сохранения")
        bot.send_message(chat_id, f"❌ Ошибка: {exc}")
    return True


def process_rewrite(chat_id: int, news_text: str) -> None:
    processing_msg = bot.send_message(chat_id, "Генерирую варианты... ⏳")

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=build_prompt(news_text),
        )
        raw_text = response.text or ""
        headlines, body, questions = parse_ai_response(raw_text)

        if not headlines or not questions or not body:
            bot.edit_message_text(
                "❌ Ошибка формата. Попробуй ещё раз.",
                chat_id,
                processing_msg.message_id,
            )
            return

        n_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=build_newsletter_prompt(body),
        )
        newsletter_line = resolve_newsletter_line(n_resp.text or "")

        user_cache[chat_id] = {
            "headlines": headlines,
            "body": body,
            "questions": questions,
            "newsletter": newsletter_line,
        }

        full_message = "<b>Варианты готовы:</b>\n\n"
        full_message += "<b>Заголовки:</b>\n" + "\n".join(
            f"{i + 1}. {h}" for i, h in enumerate(headlines)
        )
        full_message += f"\n\n<b>Текст:</b>\n{body}\n\n"
        full_message += "<b>Вопросы:</b>\n" + "\n".join(
            f"{i + 1}. {q}" for i, q in enumerate(questions)
        )
        full_message += f"\n\n<b>Рассылка:</b>\n{newsletter_line}"
        full_message += (
            "\n\n<i>Введи две цифры. Вторая «0» уберёт вопрос (например, 40).</i>"
        )

        bot.edit_message_text(
            full_message,
            chat_id,
            processing_msg.message_id,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as exc:
        logger.exception("Ошибка генерации")
        bot.edit_message_text(f"❌ Ошибка: {exc}", chat_id, processing_msg.message_id)


def handle_incoming(message) -> None:
    chat_id = message.chat.id
    news_text = extract_news_text(message)

    if news_text and handle_digit_choice(chat_id, news_text):
        return

    if not news_text:
        bot.reply_to(
            message,
            "Не вижу текста. Перешли пост с текстом или медиа с подписью.",
        )
        return

    if getattr(message, "forward_origin", None) or getattr(message, "forward_from", None):
        logger.info("[%s] Пересланное сообщение, %s символов", chat_id, len(news_text))

    process_rewrite(chat_id, news_text)


@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(
        message.chat.id,
        "Пришли новость — сделаю рерайт и подберу рассылку ✍️\n\n"
        "Можно переслать пост из канала (текст или фото/видео с подписью).\n\n"
        "После генерации отправь 2 цифры: первая — заголовок (1–5), "
        "вторая — вопрос (1–5). Например, 45 — 4-й заголовок и 5-й вопрос. "
        "Вторая цифра 0 — без вопроса (например, 40).",
    )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    handle_incoming(message)


@bot.message_handler(content_types=["photo", "video", "document", "animation"])
def handle_media(message):
    handle_incoming(message)


if __name__ == "__main__":
    logger.info("Rewrite-бот запущен")
    bot.infinity_polling()
