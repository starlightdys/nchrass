from dataclasses import dataclass
from pathlib import Path

import yaml

from config import ROOT_DIR, env


@dataclass
class DigestProfile:
    id: str
    name: str
    telegram_token: str
    gemini_api_key: str
    admin_chat_id: str
    db_path: Path
    schedule_minutes: int
    digest_header: str
    similarity_threshold: float
    cluster_threshold: float
    min_text_length: int
    posts_per_source: int
    max_gemini_items: int
    tg_channels: list[str]
    vk_groups: list[str]
    rss_feeds: list[str]
    system_prompt: str
    user_prompt: str


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_profile(profile_id: str) -> DigestProfile:
    path = ROOT_DIR / "profiles" / f"{profile_id}.yaml"
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    prompts_dir = ROOT_DIR / "profiles" / "prompts"
    system_file = data.get("system_prompt_file", f"{profile_id}_system.txt")
    user_file = data.get("user_prompt_file", f"{profile_id}_user.txt")

    return DigestProfile(
        id=data["id"],
        name=data["name"],
        telegram_token=env(data["telegram_token_env"]),
        gemini_api_key=env(data["gemini_api_key_env"]),
        admin_chat_id=env("ADMIN_CHAT_ID"),
        db_path=ROOT_DIR / data["db_path"],
        schedule_minutes=int(data["schedule_minutes"]),
        digest_header=data["digest_header"],
        similarity_threshold=float(data.get("similarity_threshold", 0.83)),
        cluster_threshold=float(data.get("cluster_threshold", 0.75)),
        min_text_length=int(data.get("min_text_length", 60)),
        posts_per_source=int(data.get("posts_per_source", 7)),
        max_gemini_items=int(data.get("max_gemini_items", 30)),
        tg_channels=list(data["sources"]["tg_channels"]),
        vk_groups=list(data["sources"]["vk_groups"]),
        rss_feeds=list(data["sources"]["rss_feeds"]),
        system_prompt=_read_text(prompts_dir / system_file),
        user_prompt=_read_text(prompts_dir / user_file),
    )
