"""任务归档与卡片生命周期。正文只存在于 Markdown，不在 SQL 留副本。"""

from typing import Any

from config.runtime import RuntimeSettings, get_settings
from memory.cards import VALID_CARD_TYPES, card_path, content_sha256, snapshot, write_card_file
from memory.db import connect


class TaskArchive:
    def __init__(self, settings: RuntimeSettings | None = None) -> None:
        self.settings = settings or get_settings()
        self.db_path = self.settings.memory_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_title TEXT NOT NULL,
                    task_type TEXT,
                    user_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS archive_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES task_archive(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    retired_at TIMESTAMP
                )
            """)
            if "content" in {r[1] for r in conn.execute("PRAGMA table_info(archive_cards)")}:
                raise RuntimeError("旧 memory.db schema 不受支持；请使用新的 MEMORY_DB_PATH")

    def create_task(self, task_title: str, task_type: str, user_summary: str) -> int:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "INSERT INTO task_archive(task_title, task_type, user_summary) VALUES (?, ?, ?)",
                (task_title, task_type, user_summary))
            return row.lastrowid

    @staticmethod
    def _parse_card(card: dict) -> tuple[str, str] | None:
        ctype = str(card.get("type") or "")
        body = str(card.get("content") or "").strip()
        return (ctype, body) if ctype in VALID_CARD_TYPES and body else None

    def get_all_active_cards(self) -> list[dict[str, Any]]:
        cards, errors = snapshot(self.settings)
        if errors:
            raise ValueError("; ".join(errors))
        return sorted(cards.values(), key=lambda c: c["card_id"])

    def has_new_cards(self, knowledge_cards: list[dict]) -> bool:
        seen = {(c["card_type"], content_sha256(c["content"]))
                for c in self.get_all_active_cards()}
        for card in knowledge_cards:
            parsed = self._parse_card(card)
            if parsed and (parsed[0], content_sha256(parsed[1])) not in seen:
                return True
        return False

    def create_cards(self, task_id: int, knowledge_cards: list[dict],
                     task_title: str, task_type: str) -> list[int]:
        """分配 id 后原子写文件。文件成功才返回 id；索引失败不影响卡片事实。"""
        ids: list[int] = []
        for card in knowledge_cards:
            parsed = self._parse_card(card)
            if parsed is None:
                continue
            ctype, body = parsed
            with connect(self.db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                active = self.get_all_active_cards()
                if any(c["card_type"] == ctype and c["content"] == body for c in active):
                    continue
                max_id = max([c["card_id"] for c in active] + [0])
                max_id = max(max_id, conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM archive_cards").fetchone()[0])
                cid = max_id + 1
                conn.execute("INSERT INTO archive_cards(id, task_id) VALUES (?, ?)", (cid, task_id))
                write_card_file({"card_id": cid, "task_id": task_id, "card_type": ctype,
                                 "task_title": task_title, "task_type": task_type, "content": body},
                                self.settings)
            ids.append(cid)
        return ids

    def retire_cards(self, card_ids: list[int]) -> int:
        """先删除事实文件，再记录生命周期；检索不依赖 SQL 淘汰标志。"""
        retired = 0
        for cid in sorted(set(card_ids)):
            path = card_path(cid, self.settings)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            with connect(self.db_path) as conn:
                conn.execute("UPDATE archive_cards SET retired_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
            retired += 1
        return retired


_default_archive: TaskArchive | None = None


def get_task_archive() -> TaskArchive:
    global _default_archive
    if _default_archive is None:
        _default_archive = TaskArchive()
    return _default_archive
