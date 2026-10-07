"""sqlite-vec 精确余弦索引。只存可重建数据，不读取卡片文件。

使用 vec_distance_cosine 在 SQL 内排序，避免在 Python 搬运向量矩阵。
模型空间、维度和文件指纹先过滤；不需要固定维度的额外配置或索引表。
"""

from contextlib import contextmanager
import json
import math
import struct

import sqlite_vec

from config.runtime import RuntimeSettings, get_settings
from memory.cards import content_sha256
from memory.db import connect


def _pack(vector: list[float]) -> bytes:
    if not vector or not all(math.isfinite(x) for x in vector):
        raise ValueError("embedding 必须是非空、有限数值的一维向量")
    blob = struct.pack(f"<{len(vector)}f", *vector)
    values = struct.unpack(f"<{len(vector)}f", blob)
    if not all(math.isfinite(x) for x in values) or not any(values):
        raise ValueError("embedding 不能是零向量或超出 float32 范围")
    return blob


class VectorIndex:
    def __init__(self, settings: RuntimeSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.db_path = self.settings.memory_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.space = json.dumps([
            self.settings.embedding_base_url.rstrip("/"), self.settings.embedding_model,
        ])
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS card_vectors (
                    path TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL,
                    embedding_space TEXT NOT NULL,
                    vector BLOB NOT NULL
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(card_vectors)")}
            if "embedding_space" not in columns:
                raise RuntimeError("旧 memory.db schema 不受支持；请配置新的 MEMORY_DB_PATH，从卡片文件重建索引")

    @contextmanager
    def _connect(self):
        with connect(self.db_path) as conn:
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
            yield conn

    def upsert(self, path: str, text: str, vector: list[float]) -> None:
        blob = _pack(vector)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO card_vectors VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    content_sha256=excluded.content_sha256,
                    embedding_space=excluded.embedding_space,
                    vector=excluded.vector
            """, (path, content_sha256(text), self.space, blob))

    def delete(self, path: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM card_vectors WHERE path = ?", (path,))

    def all_rows(self) -> dict[str, dict]:
        with self._connect() as conn:
            return {path: {"content_sha256": sha, "embedding_space": space, "dimension": dim}
                    for path, sha, space, dim in conn.execute(
                        "SELECT path, content_sha256, embedding_space, vec_length(vector) FROM card_vectors")}

    def search(self, query: list[float], fingerprints: dict[str, str],
               min_score: float) -> list[tuple[str, float]]:
        """返回有效快照中全部过阈值候选，供检索层去重后截断。"""
        blob = _pack(query)
        if not fingerprints:
            return []
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT v.path,
                       1 - CASE WHEN vec_length(v.vector) = ?
                           THEN vec_distance_cosine(v.vector, ?) END AS score
                FROM card_vectors v JOIN json_each(?) f
                    ON v.path = f.key AND v.content_sha256 = f.value
                WHERE v.embedding_space = ? AND vec_length(v.vector) = ?
                  AND score >= ?
                ORDER BY score DESC, v.path ASC
            """, (len(query), blob, json.dumps(fingerprints), self.space, len(query), min_score))
            return [(path, float(score)) for path, score in rows]
