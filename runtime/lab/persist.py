"""会话目录的持久化原语：原子写、Task 树快照、续跑扫描。

所有 JSON 写入走 atomic_write_text（tmp + fsync + rename），崩在半路不会留下
截断的文件。阶段进度、Pro 对话与副作用账本不在这里——它们全部走
runtime/lab/journal.py 的 append-only 事件日志（JOURNAL.jsonl）。
"""

import json
import os
from pathlib import Path
from typing import Any

from runtime.lab.journal import JOURNAL_FILE
from runtime.task import TaskTree, TaskStatus

STATE_FILE = "STATE.json"
SPEC_FILE = "SPEC.md"
CATALOG_FILE = "CATALOG.md"


def session_root(workspace: Path) -> Path:
    return workspace / ".labhandler" / "sessions"


def session_dir(workspace: Path, thread_id: str) -> Path:
    return session_root(workspace) / thread_id


# ── 原子写 ──────────────────────────────────────────────

def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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


# ── Task 树快照 ─────────────────────────────────────────

def save_tree(sdir: Path, tree: TaskTree) -> None:
    write_json(sdir, STATE_FILE, tree.to_dict())


def load_tree(sdir: Path) -> TaskTree | None:
    """损坏或缺 root_id 的 STATE.json 返回 None。调用方据此判定不可续跑。"""
    data = read_json(sdir, STATE_FILE)
    if not isinstance(data, dict) or "root_id" not in data:
        return None
    try:
        return TaskTree.from_dict(data)
    except (KeyError, ValueError, TypeError):
        return None


# ── 续跑扫描 ────────────────────────────────────────────

def latest_incomplete(workspace: Path) -> tuple[str, TaskTree] | None:
    root = session_root(workspace)
    if not root.is_dir():
        return None
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for d in dirs:
        tree = load_tree(d)
        if tree is None:
            continue
        root_task = tree.get(tree.root_id)
        if root_task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return d.name, tree
        # 用户停止或 SPEC 失败：有事件日志就能接着跑，终态树也能挂回去。
        if root_task.status in {TaskStatus.CANCELLED, TaskStatus.FAILED} and (
            d / JOURNAL_FILE
        ).is_file():
            return d.name, tree
        if any(n.status is TaskStatus.RUNNING for n in tree.nodes.values()):
            return d.name, tree
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
