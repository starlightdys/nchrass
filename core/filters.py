import re

AD_MARKERS = (
    "erid",
    "на правах рекламы",
)


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_ad(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AD_MARKERS)
