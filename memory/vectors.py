"""卡片向量索引（SQLite card_vectors 表）。

卡片 markdown 为事实源；本表按 path 存 embedding。
content_sha256 + embedding_model 供对账：与当前正文或当前模型不一致则重建。
向量为 little-endian float32 blob。
"""

import hashlib
import os
import sqlite3
import struct
from pathlib import Path

import numpy as np

from config.runtime import RuntimeSettings, get_settings
from memory.db import connect


def _pack(vec: list[float]) -> bytes:
    """float 列表 → little-endian float32 blob。"""
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> np.ndarray:
    """little-endian float32 blob → 一维 ndarray。长度必须是 4 的倍数。"""
    n = len(blob) // 4
    return np.frombuffer(blob, dtype="<f4", count=n)



class VectorIndex:
    """一张 memory.db 上的派生索引。父目录不存在则创建。"""

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
        """保证 card_vectors 表存在（IF NOT EXISTS）。"""
        with connect(self.db_path) as conn:
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
        """按 path 覆盖写入向量。content_sha256 取自当前正文，模型名取 settings.embedding_model。"""
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with connect(self.db_path) as conn:
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
        """删除该 path 的索引行。行不存在则无操作。"""
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM card_vectors WHERE path = ?", (path,))
            conn.commit()

    def get(self, path: str) -> dict | None:
        """读该 path 的元数据（不含向量 blob）。无此行返回 None。"""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT path, content_sha256, embedding_model FROM card_vectors WHERE path = ?",
                (path,),
            ).fetchone()
            return dict(row) if row else None

    def search(self, query_vec: list[float], k: int = 5) -> list[tuple[str, float]]:
        """余弦相似度 top-k：(path, score) 按分数降序。空表或无可比行返回 []。

        向量维度与 query_vec 不一致的行不参与本次排序。
        """
        q = np.asarray(query_vec, dtype=np.float32)
        qn = float(np.linalg.norm(q)) or 1.0
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT path, vector FROM card_vectors").fetchall()
        if not rows:
            return []

        dim = q.shape[0]
        paths: list[str] = []
        vectors: list[np.ndarray] = []
        for path, blob in rows:
            vec = _unpack(blob)
            if vec.shape[0] != dim:
                # 维度与查询向量不一致的行不进入本次排序
                continue
            paths.append(path)
            vectors.append(vec)
        if not paths:
            return []

        matrix = np.vstack(vectors)
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ q) / (norms * qn)
        top = np.argsort(-scores)[:k]
        return [(paths[i], float(scores[i])) for i in top]

    def all_rows(self) -> list[dict]:
        """全部索引行的元数据（path / content_sha256 / embedding_model），不含向量 blob。"""
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT path, content_sha256, embedding_model FROM card_vectors"
                )
            ]


def content_sha256(text: str) -> str:
    """UTF-8 SHA-256 hex。卡片正文与索引对账用同一函数。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
