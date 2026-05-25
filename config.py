import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"

load_dotenv(ROOT_DIR / ".env")

# Единственная модель Gemini для всех скриптов
GEMINI_MODEL = "gemini-3.1-flash-lite-preview"

# Дайджест: учитывать только публикации не старше N часов
DIGEST_MAX_AGE_HOURS = 2


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Переменная окружения {name} не задана в .env")
    return value


def env_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default)
