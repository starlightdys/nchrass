import logging
import time

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
RETRY_BASE_SEC = 15


def _is_retryable(error_msg: str) -> bool:
    lowered = error_msg.lower()
    return (
        "503" in error_msg
        or "UNAVAILABLE" in error_msg
        or "high demand" in lowered
        or "429" in error_msg
        or "resource_exhausted" in lowered
    )


def generate_with_retry(client: genai.Client, model: str, contents, config=None) -> str:
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response.text.strip()
        except Exception as exc:
            last_error = exc
            error_msg = str(exc)
            if _is_retryable(error_msg):
                delay = RETRY_BASE_SEC * (attempt + 1)
                logger.warning(
                    "Gemini перегружена, повтор через %s с (попытка %s/%s)",
                    delay,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(f"Gemini недоступна после {MAX_RETRIES} попыток: {last_error}")


def build_news_block(batch: list[dict]) -> str:
    lines = []
    for i, item in enumerate(batch, start=1):
        clean_text = item["text"][:400].replace("\n", " ")
        lines.append(
            f"\n[{i}] Источник: {item['source']}\n"
            f"Ссылка: {item['url']}\n"
            f"Текст: {clean_text}...\n"
        )
    return "".join(lines)


def evaluate_news(
    client: genai.Client,
    model: str,
    system_instruction: str,
    user_prompt_template: str,
    batch: list[dict],
) -> str:
    """Отправляет в Gemini ваши промпты без изменений, ответ — текст как в оригинале."""
    news_content = build_news_block(batch)
    prompt = user_prompt_template.replace("{news_content}", news_content)

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.3,
    )

    return generate_with_retry(client, model, prompt, config=config)
