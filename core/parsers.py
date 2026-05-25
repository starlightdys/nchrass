import calendar
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import vk_api
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.strip().replace("Z", "+00:00")
        return _to_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def struct_to_datetime(st) -> datetime | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)


def is_fresh(published_at: datetime | None, max_age_hours: float) -> bool:
    if published_at is None:
        return False
    age = _utc_now() - _to_utc(published_at)
    return age <= timedelta(hours=max_age_hours)


def _parse_rss(url: str, count: int, max_age_hours: float) -> list[dict]:
    items = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:count]:
            published_at = None
            for field in ("published_parsed", "updated_parsed", "created_parsed"):
                published_at = struct_to_datetime(entry.get(field))
                if published_at:
                    break
            if not is_fresh(published_at, max_age_hours):
                continue

            text = f"{entry.title}\n{entry.get('description', '')}"
            link = entry.get("link", url)
            external_id = entry.get("id") or link
            items.append(
                {
                    "source": url.split("/")[2],
                    "text": text,
                    "url": link,
                    "external_id": f"rss:{external_id}",
                    "published_at": published_at,
                }
            )
    except Exception as exc:
        logger.warning("Ошибка RSS (%s): %s", url, exc)
    return items


def _parse_vk(vk, group: str, count: int, max_age_hours: float) -> list[dict]:
    items = []
    try:
        response = vk.wall.get(domain=group, count=count)
        for item in response["items"]:
            if not item.get("text"):
                continue
            published_at = datetime.fromtimestamp(item["date"], tz=timezone.utc)
            if not is_fresh(published_at, max_age_hours):
                continue

            owner_id = item["owner_id"]
            post_id = item["id"]
            items.append(
                {
                    "source": f"vk.com/{group}",
                    "text": item["text"],
                    "url": f"https://vk.com/wall{owner_id}_{post_id}",
                    "external_id": f"vk:{owner_id}_{post_id}",
                    "published_at": published_at,
                }
            )
    except Exception as exc:
        logger.warning("Ошибка VK (%s): %s", group, exc)
    return items


def _parse_tg(channel: str, count: int, max_age_hours: float) -> list[dict]:
    items = []
    try:
        url = f"https://t.me/s/{channel}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        messages = soup.find_all("div", class_="tgme_widget_message")
        for msg in messages[-count:]:
            text_div = msg.find("div", class_="tgme_widget_message_text")
            if not text_div:
                continue

            published_at = None
            time_el = msg.find("time")
            if time_el and time_el.get("datetime"):
                published_at = parse_iso_datetime(time_el["datetime"])
            if not is_fresh(published_at, max_age_hours):
                continue

            text = text_div.get_text(separator=" ")
            post_data = msg.get("data-post")
            link = f"https://t.me/{post_data}" if post_data else url
            external_id = f"tg:{post_data}" if post_data else f"tg:{channel}:{hash(text)}"
            items.append(
                {
                    "source": f"t.me/{channel}",
                    "text": text,
                    "url": link,
                    "external_id": external_id,
                    "published_at": published_at,
                }
            )
    except Exception as exc:
        logger.warning("Ошибка TG (%s): %s", channel, exc)
    return items


def parse_all_sources(
    vk,
    tg_channels: list[str],
    vk_groups: list[str],
    rss_feeds: list[str],
    posts_per_source: int = 7,
    max_workers: int = 8,
    max_age_hours: float = 2,
) -> list[dict]:
    tasks = []
    all_news: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for url in rss_feeds:
            tasks.append(
                executor.submit(_parse_rss, url, posts_per_source, max_age_hours)
            )
        for group in vk_groups:
            tasks.append(
                executor.submit(_parse_vk, vk, group, posts_per_source, max_age_hours)
            )
        for channel in tg_channels:
            tasks.append(
                executor.submit(_parse_tg, channel, posts_per_source, max_age_hours)
            )

        for future in as_completed(tasks):
            all_news.extend(future.result())

    return all_news
