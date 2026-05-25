import sqlite3
from datetime import datetime, timedelta

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer

from core.filters import normalize_text


class NewsStore:
    def __init__(
        self,
        db_path: str,
        embedder: SentenceTransformer,
        similarity_threshold: float = 0.83,
        vector_hours: int = 15,
    ):
        self.db_path = db_path
        self.embedder = embedder
        self.similarity_threshold = similarity_threshold
        self.vector_hours = vector_hours
        self._vectors: list[np.ndarray] = []
        self._matrix: np.ndarray | None = None

    def init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS seen_posts (
                    external_id TEXT PRIMARY KEY,
                    source TEXT,
                    url TEXT,
                    seen_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    text TEXT,
                    vector BLOB,
                    external_id TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_news_created ON news(created_at)"
            )
            try:
                conn.execute("ALTER TABLE news ADD COLUMN external_id TEXT")
            except sqlite3.OperationalError:
                pass

    def _load_vectors(self) -> None:
        cutoff = (datetime.utcnow() - timedelta(hours=self.vector_hours)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT vector FROM news WHERE created_at > ?",
                (cutoff,),
            ).fetchall()
        self._vectors = [np.frombuffer(row[0], dtype=np.float32) for row in rows]
        self._matrix = (
            np.vstack(self._vectors) if self._vectors else None
        )

    def is_seen(self, external_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_posts WHERE external_id = ?",
                (external_id,),
            ).fetchone()
        return row is not None

    def mark_seen(self, item: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_posts (external_id, source, url) VALUES (?, ?, ?)",
                (item["external_id"], item["source"], item["url"]),
            )

    def is_semantic_duplicate(self, text: str) -> tuple[bool, np.ndarray]:
        normalized = normalize_text(text)
        vector = self.embedder.encode(normalized).astype(np.float32)

        if self._matrix is None or len(self._vectors) == 0:
            return False, vector

        similarities = cosine_similarity([vector], self._matrix)[0]
        if len(similarities) > 0 and float(np.max(similarities)) >= self.similarity_threshold:
            return True, vector

        return False, vector

    def save_news(self, item: dict, vector: np.ndarray, text: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO news (source, text, vector, external_id) VALUES (?, ?, ?, ?)",
                (item["source"], text, vector.tobytes(), item["external_id"]),
            )
        self._vectors.append(vector)
        if self._matrix is None:
            self._matrix = vector.reshape(1, -1)
        else:
            self._matrix = np.vstack([self._matrix, vector.reshape(1, -1)])

    def begin_cycle(self) -> None:
        self._load_vectors()

    def filter_new_items(self, items: list[dict], min_length: int = 60) -> list[dict]:
        unique: list[dict] = []
        for item in items:
            text = item.get("text", "")
            if len(text) < min_length:
                continue
            if self.is_seen(item["external_id"]):
                continue
            is_dup, vector = self.is_semantic_duplicate(text)
            if is_dup:
                self.mark_seen(item)
                continue
            self.save_news(item, vector, text)
            self.mark_seen(item)
            unique.append(item)
        return unique
