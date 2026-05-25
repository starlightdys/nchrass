from telebot import TeleBot


def safe_send_message(bot: TeleBot, chat_id: str, text: str) -> None:
    if len(text) <= 4096:
        bot.send_message(chat_id, text, disable_web_page_preview=True)
        return
    for i in range(0, len(text), 4000):
        bot.send_message(chat_id, text[i : i + 4000], disable_web_page_preview=True)
