import logging
import time
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199"
UPLOAD_TIMEOUT = 180
UPLOAD_MAX_ATTEMPTS = 4

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


class PhotoUploadFailedError(RuntimeError):
    def __init__(self, message: str, attempts: int = UPLOAD_MAX_ATTEMPTS):
        self.attempts = attempts
        super().__init__(message)


def pick_vk_photo_url(sizes: list[dict]) -> str | None:
    """Берёт максимальный размер фото без изменений при загрузке."""
    if not sizes:
        return None
    largest = max(
        sizes,
        key=lambda s: (s.get("width", 0) or 0) * (s.get("height", 0) or 0),
    )
    return largest.get("url")


def _vk_call(method: str, access_token: str, **params):
    params["access_token"] = access_token
    params["v"] = VK_API_VERSION
    response = requests.get(
        f"https://api.vk.com/method/{method}",
        params=params,
        timeout=30,
    )
    data = response.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"VK API {method}: {err.get('error_msg', err)}")
    return data["response"]


def download_image(url: str) -> tuple[bytes, str]:
    """Скачивает изображение как есть, без сжатия и ресайза."""
    response = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=120)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()

    path = urlparse(url).path.lower()
    if path.endswith(".png") or "png" in content_type:
        filename = "photo.png"
        mime = "image/png"
    elif path.endswith(".webp") or "webp" in content_type:
        filename = "photo.webp"
        mime = "image/webp"
    elif path.endswith(".gif") or "gif" in content_type:
        filename = "photo.gif"
        mime = "image/gif"
    else:
        filename = "photo.jpg"
        mime = content_type or "image/jpeg"

    logger.info("Скачано фото: %s KB (%s)", len(response.content) // 1024, mime)
    return response.content, filename, mime


def _post_to_upload_server(
    upload_url: str, image_bytes: bytes, filename: str, mime: str
) -> dict:
    last_error = None
    files = {"photo": (filename, image_bytes, mime)}

    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                upload_url,
                files=files,
                timeout=UPLOAD_TIMEOUT,
            )
            if response.status_code in (502, 503, 504):
                raise requests.HTTPError(
                    f"{response.status_code} {response.reason}",
                    response=response,
                )
            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.HTTPError, requests.ConnectionError) as exc:
            last_error = exc
            if attempt >= UPLOAD_MAX_ATTEMPTS:
                break
            delay = 8 * attempt
            logger.warning(
                "Таймаут/ошибка pu.vk.com (%s/%s): %s, ждём %s с",
                attempt,
                UPLOAD_MAX_ATTEMPTS,
                exc,
                delay,
            )
            time.sleep(delay)

    raise PhotoUploadFailedError(
        f"Сервер загрузки ВК не ответил: {last_error}",
        attempts=UPLOAD_MAX_ATTEMPTS,
    )


def upload_photo_for_messages(
    access_token: str,
    group_id: int,
    image_url: str,
) -> str:
    image_bytes, filename, mime = download_image(image_url)

    upload_data = None
    last_error = None

    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        try:
            upload_server = _vk_call(
                "photos.getMessagesUploadServer",
                access_token,
                group_id=group_id,
            )
            upload_url = upload_server["upload_url"]
            upload_data = _post_to_upload_server(
                upload_url, image_bytes, filename, mime
            )
            break
        except PhotoUploadFailedError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= UPLOAD_MAX_ATTEMPTS:
                break
            delay = 8 * attempt
            logger.warning(
                "Повтор загрузки фото (%s/%s): %s",
                attempt,
                UPLOAD_MAX_ATTEMPTS,
                exc,
            )
            time.sleep(delay)

    if not upload_data:
        raise PhotoUploadFailedError(
            f"Не удалось загрузить фото: {last_error}",
            attempts=UPLOAD_MAX_ATTEMPTS,
        )

    if "error" in upload_data:
        raise PhotoUploadFailedError(
            f"Ошибка ответа сервера ВК: {upload_data['error']}",
            attempts=UPLOAD_MAX_ATTEMPTS,
        )

    saved = _vk_call(
        "photos.saveMessagesPhoto",
        access_token,
        group_id=group_id,
        server=upload_data["server"],
        photo=upload_data["photo"],
        hash=upload_data["hash"],
    )
    photo = saved[0]
    attachment = f"photo{photo['owner_id']}_{photo['id']}"
    logger.info("Фото загружено в ВК: %s", attachment)
    return attachment
