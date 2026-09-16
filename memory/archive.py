"""任务归档存储（SQLite 事实层）。卡片 markdown 文件见 memory.retrieve。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any

from config.runtime import get_settings

# card_type 白名单
VALID_CARD_TYPES = frozenset({"lesson", "strategy", "pattern"})


class TaskArchive:
    """任务归档服务。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path: str = db_path or str(get_settings().memory_db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """创建数据库表。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_title TEXT NOT NULL,
                    task_type TEXT,
                    user_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS archive_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    card_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    vector_error TEXT,
                    content_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    retired_at TIMESTAMP,
                    FOREIGN KEY(task_id) REFERENCES task_archive(id),
                    UNIQUE(task_id, card_type, content_hash)
                )
                """
            )
            conn.commit()

    # --- 写接口 -------------------------------------------------------

    def create_task(self, task_title: str, task_type: str, user_summary: str) -> int:
        """创建任务归档条目，返回 task_id。"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO task_archive (task_title, task_type, user_summary) VALUES (?, ?, ?)",
                (task_title, task_type, user_summary),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def create_cards(
        self, task_id: int, knowledge_cards: list[dict], task_title: str, task_type: str
    ) -> list[int]:
        """批量写入知识卡片，返回成功写入的 card_id 列表。

        校验逻辑：
        - card_type 不在白名单 -> 丢弃
        - content 为空 -> 丢弃
        - 同 task 内 card_type + content_hash 重复 -> 跳过
        """
        inserted_ids: list[int] = []
        with sqlite3.connect(self.db_path) as conn:
            for card in knowledge_cards:
                card_type = str(card.get("type", "")).strip()
                content = str(card.get("content", "")).strip()
                if card_type not in VALID_CARD_TYPES or not content:
                    continue

                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
                search_text = (
                    f"任务类型: {task_type}\n"
                    f"卡片类型: {card_type}\n"
                    f"任务标题: {task_title}\n"
                    f"内容: {content}"
                )

                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO archive_cards
                            (task_id, card_type, content, search_text, content_hash)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (task_id, card_type, content, search_text, content_hash),
                    )
                    conn.commit()
                    inserted_ids.append(cursor.lastrowid or 0)
                except sqlite3.IntegrityError:
                    pass

        return inserted_ids

    def mark_card_vector_error(self, card_id: int, error: str) -> None:
        """记录卡片 Chroma 写入失败的原因。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE archive_cards SET vector_error = ? WHERE id = ?",
                (error[:500], card_id),
            )
            conn.commit()

    def clear_card_vector_error(self, card_id: int) -> None:
        """清除卡片的 vector error 标志（供重建用）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE archive_cards SET vector_error = NULL WHERE id = ?",
                (card_id,),
            )
            conn.commit()

    def retire_cards(self, card_ids: list[int]) -> int:
        """软删除卡片（/dream 治理淘汰/合并卡片）。返回实际标记数。"""
        if not card_ids:
            return 0
        placeholders = ",".join("?" * len(card_ids))
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"""
                UPDATE archive_cards SET retired_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders}) AND retired_at IS NULL
                """,
                card_ids,
            )
            conn.commit()
            return cursor.rowcount

    # --- 读接口 -------------------------------------------------------

    def get_cards_for_indexing(self) -> list[dict[str, Any]]:
        """获取所有需索引的卡片（含父任务信息；排除软删除）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT c.id as card_id, c.card_type, c.content, c.search_text, c.vector_error,
                       t.id as task_id, t.task_title, t.task_type
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.retired_at IS NULL
                ORDER BY c.id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_cards_by_ids(self, card_ids: list[int]) -> list[dict[str, Any]]:
        """按 card_id 水合卡片及父任务（排除软删除）。"""
        if not card_ids:
            return []
        placeholders = ",".join("?" * len(card_ids))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT c.id as card_id, c.card_type, c.content, c.search_text,
                       t.id as task_id, t.task_title, t.task_type
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.id IN ({placeholders}) AND c.retired_at IS NULL
                """,
                card_ids,
            )
            rows_by_id = {row["card_id"]: dict(row) for row in cursor.fetchall()}
            return [rows_by_id[rid] for rid in card_ids if rid in rows_by_id]

    def get_all_active_cards(self) -> list[dict[str, Any]]:
        """/dream 治理的输入：所有非软删除卡片（带 card_id / 分组键 / content）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT c.id as card_id, c.card_type, c.content, c.task_id,
                       t.task_title, t.task_type
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.retired_at IS NULL
                ORDER BY c.id
                """
            )
            return [dict(row) for row in cursor.fetchall()]


# 模块级单例

_default_archive: TaskArchive | None = None


def get_task_archive() -> TaskArchive:
    """获取全局 TaskArchive 实例。"""
    global _default_archive
    if _default_archive is None:
        _default_archive = TaskArchive()
    return _default_archive