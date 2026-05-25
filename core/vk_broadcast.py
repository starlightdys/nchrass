import logging

import requests

from core.vk_photo import PhotoUploadFailedError, upload_photo_for_messages

logger = logging.getLogger(__name__)

BROADCAST_API_URL = "https://broadcast.vkforms.ru/api/v2/broadcast"


def send_broadcast(
    api_token: str,
    list_id: int,
    text: str,
    vk_access_token: str,
    group_id: int,
    photo_url: str,
    attachment: str | None = None,
) -> dict:
    """
    Рассылка только с фото. Без успешной загрузки вложения не отправляется.
    """
    if not photo_url and not attachment:
        raise RuntimeError("Нет фото — рассылка отправляется только с изображением.")

    if not attachment:
        attachment = upload_photo_for_messages(vk_access_token, group_id, photo_url)

    message_obj = {
        "message": text,
        "attachment": attachment,
    }

    payload: dict = {
        "message": message_obj,
        "list_ids": [list_id],
        "run_now": 1,
        "access_token": vk_access_token,
    }

    response = requests.post(
        f"{BROADCAST_API_URL}?token={api_token}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()

    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"VK Broadcast API {err.get('code')}: {err.get('description') or err.get('message')}"
        )

    logger.info("Рассылка создана: %s", data.get("response", {}).get("id"))
    return data
