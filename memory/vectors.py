"""GLM-Embedding-3 + SQLite 向量表。卡片文件为事实源，本表为派生索引。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from pathlib import Path

import numpy as np

from config.runtime import RuntimeSettings, get_settings


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.frombuffer(blob, dtype="<f4", count=n)


class VectorIndex:
    def __init__(
        self,
        db_path: Path | None = None,
        settings: RuntimeSettings | None = None,
        embed_fn=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db_path = Path(db_path or self.settings.memory_db_path)
        self.embed_fn = embed_fn
        os.makedirs(self.db_path.parent, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS card_vectors (
                    path TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    vector BLOB NOT NULL
                )
                """
            )
            conn.commit()

    def upsert(self, path: str, content: str, vector: list[float]) -> None:
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO card_vectors(path, content_sha256, embedding_model, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_sha256=excluded.content_sha256,
                    embedding_model=excluded.embedding_model,
                    vector=excluded.vector
                """,
                (path, sha, self.settings.embedding_model, _pack(vector)),
            )
            conn.commit()

    def delete(self, path: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM card_vectors WHERE path = ?", (path,))
            conn.commit()

    def get(self, path: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT path, content_sha256, embedding_model FROM card_vectors WHERE path = ?",
                (path,),
            ).fetchone()
            return dict(row) if row else None

    def search(self, query_vec: list[float], k: int = 5) -> list[tuple[str, float]]:
        q = np.asarray(query_vec, dtype=np.float32)
        qn = np.linalg.norm(q) or 1.0
        scored: list[tuple[str, float]] = []
        with sqlite3.connect(self.db_path) as conn:
            for path, blob in conn.execute("SELECT path, vector FROM card_vectors"):
                v = _unpack(blob)
                denom = (np.linalg.norm(v) * qn) or 1.0
                scored.append((path, float(np.dot(v, q) / denom)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def all_rows(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT path, content_sha256, embedding_model FROM card_vectors"
                )
            ]


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
