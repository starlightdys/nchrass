"""
Единый процесс дайджестов: общий парсинг и эмбеддинги, два профиля — два Telegram-бота.
"""
import logging
import sys
import threading
import time

import schedule
from sentence_transformers import SentenceTransformer

from config import DATA_DIR, GEMINI_MODEL
from core.digest import DigestRunner, create_vk
from core.profile import load_profile

logger = logging.getLogger(__name__)

# Эмбеддинги не потокобезопасны — один job за раз
_digest_job_lock = threading.Lock()
STARTUP_STAGGER_SECONDS = 90


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(DATA_DIR / "digest.log", encoding="utf-8"),
        ],
        force=True,
    )


def _run_job_safe(runner: DigestRunner) -> None:
    """Запуск дайджеста в фоне; блокировка не даёт двум профилям ломать embedder."""
    def _target():
        with _digest_job_lock:
            runner.run_job()

    threading.Thread(target=_target, daemon=True).start()


def main():
    setup_logging()
    logger.info("Gemini: %s", GEMINI_MODEL)
    logger.info("Загрузка модели эмбеддингов...")
    embedder = SentenceTransformer("cointegrated/rubert-tiny2")
    vk = create_vk()

    profiles = [load_profile("nch"), load_profile("chp")]
    runners = [DigestRunner(p, embedder, vk, GEMINI_MODEL) for p in profiles]

    for runner in runners:
        profile = runner.profile
        schedule.every(profile.schedule_minutes).minutes.do(
            _run_job_safe, runner
        )
        logger.info(
            "Профиль %s: бот %s…, интервал %s мин",
            profile.id,
            profile.telegram_token[:8],
            profile.schedule_minutes,
        )

    logger.info("Стартовый прогон обоих профилей…")
    for i, runner in enumerate(runners):
        with _digest_job_lock:
            runner.run_job()
        if i < len(runners) - 1:
            logger.info(
                "Пауза %s с перед профилем %s",
                STARTUP_STAGGER_SECONDS,
                runners[i + 1].profile.id,
            )
            time.sleep(STARTUP_STAGGER_SECONDS)

    logger.info("Планировщик запущен. Ctrl+C для остановки.")
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
