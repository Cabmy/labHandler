"""会话文件原语与 Journal 恢复扫描；不维护第二份生命周期快照。"""

import json
from pathlib import Path
from typing import Any

from infra.files import atomic_write_text
from runtime.lab.journal import JOURNAL_FILE, Journal, JournalState

SPEC_FILE = "SPEC.md"
CATALOG_FILE = "CATALOG.md"


def session_root(workspace: Path) -> Path:
    return workspace / ".labhandler" / "sessions"


def session_dir(workspace: Path, thread_id: str) -> Path:
    return session_root(workspace) / thread_id


# ── 原子写 ──────────────────────────────────────────────

def write_text(sdir: Path, name: str, content: str) -> None:
    atomic_write_text(sdir / name, content)


def read_text(sdir: Path, name: str) -> str:
    path = sdir / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def write_json(sdir: Path, name: str, payload: Any) -> None:
    atomic_write_text(sdir / name, json.dumps(payload,
                      ensure_ascii=False, indent=2))


def read_json(sdir: Path, name: str) -> Any | None:
    path = sdir / name
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def latest_incomplete(workspace: Path) -> tuple[str, JournalState] | None:
    root = session_root(workspace)
    if not root.is_dir():
        return None
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for directory in dirs:
        try:
            state = Journal(directory / JOURNAL_FILE).replay()
        except (ValueError, KeyError, TypeError):
            continue
        if state.resumable:
            return directory.name, state
    return None


# ── 审计回溯 ────────────────────────────────────────────

_WRITE_TOOLS = {
    "write_file",
    "patch_file",
    "sandbox_file_operations",
    "sandbox_str_replace_editor",
    "write_acceptance",
    "use_skill_script",
}


def audit_offset(workspace: Path) -> int:
    """当前审计日志的行数。worker 启动时取一次，用于界定它自己的写入。"""
    path = workspace / ".labhandler" / "audit.jsonl"
    if not path.is_file():
        return 0
    try:
        return sum(1 for _ in path.open(encoding="utf-8"))
    except OSError:
        return 0


def changed_files_from_audit(workspace: Path, *, since: int = 0) -> list[str]:
    """回捞 since 行之后写过的文件。brief 没给 changed_files 时兜底。

    不带 since 会把整个会话的写入都算到某一个 worker 头上，账本按 id 记的
    指纹就会混进别人的产物，续跑判断随之失真。
    """
    path = workspace / ".labhandler" / "audit.jsonl"
    if not path.is_file():
        return []
    seen: set[str] = set()
    out: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[since:]
    except OSError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("tool") not in _WRITE_TOOLS:
            continue
        if not str(rec.get("outcome", "")).startswith("ok"):
            continue
        args = rec.get("args") or {}
        for key in ("path", "file_path", "filename"):
            value = args.get(key)
            if value and str(value) not in seen:
                seen.add(str(value))
                out.append(str(value))
    return out
