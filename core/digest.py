import logging
import time
from datetime import datetime

import vk_api
from google import genai
from sentence_transformers import SentenceTransformer
from telebot import TeleBot

from core.cluster import cluster_representatives
from core.dedup import NewsStore
from core.filters import is_ad
from core.gemini_client import evaluate_news
from core.parsers import parse_all_sources
from core.profile import DigestProfile
from core.telegram_util import safe_send_message
from config import DATA_DIR, DIGEST_MAX_AGE_HOURS

logger = logging.getLogger(__name__)


class DigestRunner:
    def __init__(
        self,
        profile: DigestProfile,
        embedder: SentenceTransformer,
        vk,
        model: str,
    ):
        self.profile = profile
        self.embedder = embedder
        self.vk = vk
        self.model = model
        self.bot = TeleBot(profile.telegram_token)
        self.client = genai.Client(api_key=profile.gemini_api_key)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        profile.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = NewsStore(
            str(profile.db_path),
            embedder,
            similarity_threshold=profile.similarity_threshold,
        )
        self.store.init_db()

    def run_job(self) -> None:
        profile = self.profile
        stamp = datetime.now().strftime("%H:%M:%S")
        logger.info("[%s] %s — начинаю проверку", stamp, profile.id)

        try:
            raw_news = parse_all_sources(
                self.vk,
                profile.tg_channels,
                profile.vk_groups,
                profile.rss_feeds,
                posts_per_source=profile.posts_per_source,
                max_age_hours=DIGEST_MAX_AGE_HOURS,
            )
            logger.info(
                "[%s] Свежих записей (≤%s ч): %s",
                profile.id,
                DIGEST_MAX_AGE_HOURS,
                len(raw_news),
            )
            raw_news = [n for n in raw_news if not is_ad(n.get("text", ""))]

            self.store.begin_cycle()
            unique_news = self.store.filter_new_items(
                raw_news, min_length=profile.min_text_length
            )

            if not unique_news:
                logger.info("[%s] Новых новостей нет", profile.id)
                return

            batch = cluster_representatives(
                unique_news,
                self.embedder,
                threshold=profile.cluster_threshold,
                max_items=profile.max_gemini_items,
            )

            logger.info(
                "[%s] Новых: %s, в Gemini: %s",
                profile.id,
                len(unique_news),
                len(batch),
            )

            summary = evaluate_news(
                self.client,
                self.model,
                profile.system_prompt,
                profile.user_prompt,
                batch,
            )

            header = f"{profile.digest_header} ({datetime.now().strftime('%H:%M')})\n"
            header += "--------------------------------\n\n"
            safe_send_message(self.bot, profile.admin_chat_id, header + summary)
            logger.info("[%s] Дайджест отправлен", profile.id)

        except Exception as exc:
            logger.exception("[%s] Ошибка в job: %s", profile.id, exc)
            try:
                safe_send_message(
                    self.bot,
                    profile.admin_chat_id,
                    f"⚠️ Ошибка бота {profile.name}: {exc}",
                )
            except Exception:
                pass


def create_vk():
    from config import env

    vk_session = vk_api.VkApi(token=env("VK_TOKEN"))
    return vk_session.get_api()
