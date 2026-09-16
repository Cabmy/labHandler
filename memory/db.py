"""SQLite 连接入口。archive 与 vectors 共用同一库文件，必须走本函数，连接参数才一致。"""

import sqlite3
from pathlib import Path

_BUSY_TIMEOUT_MS = 10_000


def connect(db_path: Path) -> sqlite3.Connection:
    """打开一条 WAL 连接：busy_timeout=10s，synchronous=NORMAL。

    /dream 与 memory_search 会并发打同一库；锁等待最多 10s，
    超时抛 OperationalError: database is locked。
    """
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
