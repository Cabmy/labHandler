"""任务归档的 SQLite 事实层。

task_archive 一行一次 lab；archive_cards 挂在其上。
卡片 markdown 与向量索引由 memory.retrieve / memory.vectors 派生；本模块不写文件。
card_type 仅 lesson / strategy / pattern。retired_at 非空视为淘汰，读接口一律排除。
"""

import os
import sqlite3
from typing import Any

from config.runtime import get_settings
from memory.db import connect
from memory.vectors import content_sha256

# card_type 仅允许这三项；其余在 create_cards 中丢弃。
VALID_CARD_TYPES = frozenset({"lesson", "strategy", "pattern"})


class TaskArchive:
    """绑定一个 memory.db 的归档读写。父目录不存在则创建。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path: str = db_path or str(get_settings().memory_db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """保证 task_archive 与 archive_cards 两表存在（IF NOT EXISTS）。"""
        with connect(self.db_path) as conn:
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
        """插入一条 task_archive，返回新 task_id。"""
        with connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO task_archive (task_title, task_type, user_summary) VALUES (?, ?, ?)",
                (task_title, task_type, user_summary),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def _parse_card(self, card: dict) -> tuple[str, str, str] | None:
        """合法卡片 → (card_type, content, content_hash)；否则 None。"""
        card_type = str(card.get("type", "")).strip()
        content = str(card.get("content", "")).strip()
        if card_type not in VALID_CARD_TYPES or not content:
            return None
        return card_type, content, content_sha256(content)[:16]

    def _active_duplicate(self, conn: sqlite3.Connection, card_type: str, content_hash: str) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM archive_cards
            WHERE card_type = ? AND content_hash = ? AND retired_at IS NULL
            LIMIT 1
            """,
            (card_type, content_hash),
        ).fetchone()
        return row is not None

    def has_new_cards(self, knowledge_cards: list[dict]) -> bool:
        """是否存在尚未入库的活跃卡（跨 task 同 hash 视为已有）。"""
        seen: set[tuple[str, str]] = set()
        with connect(self.db_path) as conn:
            for card in knowledge_cards:
                parsed = self._parse_card(card)
                if parsed is None:
                    continue
                card_type, _content, content_hash = parsed
                key = (card_type, content_hash)
                if key in seen:
                    continue
                seen.add(key)
                if not self._active_duplicate(conn, card_type, content_hash):
                    return True
        return False

    def create_cards(
        self, task_id: int, knowledge_cards: list[dict], task_title: str, task_type: str
    ) -> list[int]:
        """写入 archive_cards，返回实际插入的 card_id。

        不入库：card_type 不在白名单、content 为空、
        任意活跃卡已有同一 (card_type, content_hash)（跨 task 也不再收）、
        同 task 内 UNIQUE 冲突（吞 IntegrityError）。
        search_text 由 task_type / card_type / task_title / content 拼成。
        """
        inserted_ids: list[int] = []
        with connect(self.db_path) as conn:
            for card in knowledge_cards:
                parsed = self._parse_card(card)
                if parsed is None:
                    continue
                card_type, content, content_hash = parsed
                if self._active_duplicate(conn, card_type, content_hash):
                    continue
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
        """把该卡向量索引失败原因写入 vector_error（截断 500 字）。"""
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE archive_cards SET vector_error = ? WHERE id = ?",
                (error[:500], card_id),
            )
            conn.commit()

    def clear_card_vector_error(self, card_id: int) -> None:
        """将该卡 vector_error 置空。"""
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE archive_cards SET vector_error = NULL WHERE id = ?",
                (card_id,),
            )
            conn.commit()

    def retire_cards(self, card_ids: list[int]) -> int:
        """把仍活跃的卡片 retired_at 置为当前时间，返回实际标记数。已淘汰的行不动。"""
        if not card_ids:
            return 0
        placeholders = ",".join("?" * len(card_ids))
        with connect(self.db_path) as conn:
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

    def _active_cards(self, extra_columns: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        """全部未淘汰卡片 + 父任务字段，按 card_id 升序。

        extra_columns 允许调用方在基础列之上追加额外列（如 vector_error、search_text）。
        """
        # 基础列：card_id / card_type / content / task_id + 父任务字段
        base_cols = [
            "c.id as card_id",
            "c.card_type",
            "c.content",
            "c.task_id",
            "t.task_title",
            "t.task_type",
        ]
        cols = ", ".join(base_cols)
        if extra_columns:
            cols += ", " + ", ".join(extra_columns)
        with connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT {cols}
                FROM archive_cards c
                JOIN task_archive t ON c.task_id = t.id
                WHERE c.retired_at IS NULL
                ORDER BY c.id
                """
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_cards_for_indexing(self) -> list[dict[str, Any]]:
        """全部未淘汰卡片 + 父任务字段 + search_text + vector_error，供建索引。"""
        return self._active_cards(extra_columns=("c.search_text", "c.vector_error"))

    def get_cards_by_ids(self, card_ids: list[int]) -> list[dict[str, Any]]:
        """按 card_id 取未淘汰卡片及父任务。返回顺序与入参中仍存在的 id 一致。"""
        if not card_ids:
            return []
        placeholders = ",".join("?" * len(card_ids))
        with connect(self.db_path) as conn:
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
            rows_by_id = {row["card_id"]: dict(
                row) for row in cursor.fetchall()}
            return [rows_by_id[rid] for rid in card_ids if rid in rows_by_id]

    def get_all_active_cards(self) -> list[dict[str, Any]]:
        """全部未淘汰卡片（card_id / 分组键 / content），按 card_id 升序。/dream 输入。"""
        return self._active_cards()


# 进程内唯一 TaskArchive，绑定 settings.memory_db_path。

_default_archive: TaskArchive | None = None


def get_task_archive() -> TaskArchive:
    """返回进程内唯一 TaskArchive。首次调用时按 settings.memory_db_path 创建。"""
    global _default_archive
    if _default_archive is None:
        _default_archive = TaskArchive()
    return _default_archive
