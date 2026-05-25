import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from core.filters import normalize_text


def cluster_representatives(
    items: list[dict],
    embedder: SentenceTransformer,
    threshold: float = 0.75,
    max_items: int = 30,
) -> list[dict]:
    if len(items) <= max_items:
        return items

    vectors = [
        embedder.encode(normalize_text(item["text"])).astype(np.float32) for item in items
    ]
    used = set()
    selected: list[dict] = []

    for idx, item in enumerate(items):
        if idx in used:
            continue
        selected.append(item)
        if len(selected) >= max_items:
            break
        sims = cosine_similarity([vectors[idx]], vectors)[0]
        for j, score in enumerate(sims):
            if j != idx and score >= threshold:
                used.add(j)

    return selected
