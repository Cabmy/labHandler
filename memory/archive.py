"""task_archive - SQLite + Chroma 双写

字段（PLAN §10.1 / STEPS P2.1）：
    (id, task_title, task_type, summary, lessons, workspace_snapshot, created_at)

设计要点：
1. SQLite 是 source of truth（结构化查询、按时间、事务一致性）
2. Chroma 当语义索引（用 task_title+summary+lessons 拼成 embedding 文本）
3. 双写一致性：先 SQLite 拿 row_id，再写 Chroma；Chroma 失败不影响主流程
4. workspace_snapshot 存 SUMMARY.md 全文（用户决策；不打包二进制）
5. Chroma collection 复用 .env 的 task_archive_glm_embedding_3（与 RAG 共用 embedding）
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

from langchain_chroma import Chroma

MEMORY_DB_PATH = os.getenv("MEMORY_DB_PATH", "./.hwhandler_data/memory.db")
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./.hwhandler_data/chroma")
CHROMA_COLLECTION_NAME = os.getenv(
    "CHROMA_COLLECTION_NAME", "task_archive_glm_embedding_3"
)


class TaskArchive:
    """任务归档 - SQLite + Chroma 双写"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path: str = db_path or MEMORY_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
        self._store: Optional[Chroma] = None

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_title TEXT NOT NULL,
                    task_type TEXT,
                    summary TEXT,
                    lessons TEXT,
                    workspace_snapshot TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def _get_store(self) -> Chroma:
        """懒加载 Chroma；embedding 也延迟 import 避免环依赖"""
        if self._store is None:
            from llm import get_embeddings  # 延迟 import，与 rag/vectorstore.py 同策略
            self._store = Chroma(
                collection_name=CHROMA_COLLECTION_NAME,
                embedding_function=get_embeddings(),
                persist_directory=CHROMA_PERSIST_DIR,
            )
        return self._store

    def archive_task(
        self,
        task_title: str,
        task_type: str,
        summary: str,
        lessons: str = "",
        workspace_snapshot: str = "",
    ) -> int:
        """归档任务：SQLite 落库 + Chroma 写索引

        Args:
            task_title: 任务标题（如"实现二分查找"）
            task_type: 任务类型（coding/essay/lab_report 等）
            summary: SUMMARY.md 主体或精简摘要
            lessons: 教训/经验（Planner 后续 rag_search 主要用这个）
            workspace_snapshot: SUMMARY.md 全文（用户决策：不打包二进制）

        Returns:
            row_id（SQLite 自增）
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_archive
                    (task_title, task_type, summary, lessons, workspace_snapshot)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_title, task_type, summary, lessons, workspace_snapshot),
            )
            conn.commit()
            row_id = cursor.lastrowid or 0

        # Chroma 写索引：用 title+summary+lessons 拼起来做 embedding 输入
        try:
            embed_text = (
                f"标题: {task_title}\n类型: {task_type}\n"
                f"摘要: {summary[:1500]}\n教训: {lessons[:1000]}"
            )
            self._get_store().add_texts(
                texts=[embed_text],
                metadatas=[
                    {
                        "row_id": row_id,
                        "task_title": task_title,
                        "task_type": task_type,
                    }
                ],
                ids=[f"archive_{row_id}"],
            )
        except Exception:
            # Chroma 写失败静默：SQLite 已落，下次 reindex 可补
            pass

        return row_id

    def search_archive(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """混合检索历史任务：Chroma 语义 + BM25 关键词 + RRF 融合（k=60）。

        BM25 兜中文短关键词（向量在中文短串上不稳）；Chroma 兜语义泛化。
        融合复用 rag.hybrid.rrf_fuse；BM25 复用 rag.bm25_store.BM25Store
        （现场实例化而非用单例——单例是给 RAG 通用库用的，task_archive 是另一组语料）。
        archive 量级 < 100 行，每次查询现场建 BM25 无性能压力。

        Returns:
            按 RRF 分数降序的 dict 列表（key 同 task_archive 表字段）
        """
        over = max(limit * 2, 10)

        # 1) Chroma 语义召回 → row_id 排名
        chroma_ranking: list[str] = []
        try:
            hits = self._get_store().similarity_search(query, k=over)
            chroma_ranking = [
                str(h.metadata["row_id"])
                for h in hits
                if isinstance(h.metadata.get("row_id"), int)
            ]
        except Exception:
            pass

        # 2) BM25 关键词召回 → row_id 排名
        bm25_ranking = self._bm25_rank_ids(query, top_k=over)

        if not chroma_ranking and not bm25_ranking:
            return []

        # 3) RRF 融合（rank_bm25.BM25Okapi 在 N<5 时 IDF 会退化为 0 → BM25 单路返回空，
        #    此时 fused 只取 Chroma 一路；archive 满 5 条以上即正常工作）
        from rag.hybrid import rrf_fuse
        fused = rrf_fuse([chroma_ranking, bm25_ranking])
        fused_ids: list[int] = []
        for rid_str, _ in fused[:limit]:
            try:
                fused_ids.append(int(rid_str))
            except ValueError:
                continue

        if not fused_ids:
            return []

        # 4) SQLite 取原文（保持 fused 顺序）
        placeholders = ",".join("?" * len(fused_ids))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT * FROM task_archive WHERE id IN ({placeholders})",
                fused_ids,
            )
            rows_by_id = {row["id"]: dict(row) for row in cursor.fetchall()}
        return [rows_by_id[rid] for rid in fused_ids if rid in rows_by_id]

    def _bm25_rank_ids(self, query: str, top_k: int) -> list[str]:
        """对 task_archive 全表 jieba 分词 + BM25 排序，返回 row_id 字符串列表。"""
        from langchain_core.documents import Document
        from rag.bm25_store import BM25Store

        rows = self.list_all(limit=1000)
        if not rows:
            return []
        docs = [
            Document(
                page_content=(
                    f"{r.get('task_title') or ''} "
                    f"{r.get('lessons') or ''} "
                    f"{(r.get('summary') or '')[:500]}"
                ),
                metadata={"rid_str": str(r["id"])},
            )
            for r in rows
        ]
        store = BM25Store()
        store.add_documents(docs)
        hits = store.search(query, k=top_k)
        return [d.metadata["rid_str"] for d, _ in hits]

    def get_by_id(self, row_id: int) -> Optional[dict[str, Any]]:
        """按 id 直接取一条（管理用途）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM task_archive WHERE id = ?", (row_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_all(self, limit: int = 50) -> list[dict[str, Any]]:
        """按时间倒序列全部（管理/调试用途）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM task_archive ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]


# ─── 全局单例 ──────────────────────────────────────────────────────

_default_archive: Optional[TaskArchive] = None


def get_task_archive() -> TaskArchive:
    """获取全局 TaskArchive 实例"""
    global _default_archive
    if _default_archive is None:
        _default_archive = TaskArchive()
    return _default_archive


# 便捷函数（让 tools/archive_tool.py / tools/rag_tool.py 调用更顺手）

def archive_task(
    task_title: str,
    task_type: str,
    summary: str,
    lessons: str = "",
    workspace_snapshot: str = "",
) -> int:
    return get_task_archive().archive_task(
        task_title=task_title,
        task_type=task_type,
        summary=summary,
        lessons=lessons,
        workspace_snapshot=workspace_snapshot,
    )


def search_archive(query: str, limit: int = 5) -> list[dict[str, Any]]:
    return get_task_archive().search_archive(query, limit=limit)
