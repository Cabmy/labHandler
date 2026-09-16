"""turn 边界落盘 Task 树；启动扫描未完成 lab。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from runtime.task import TaskTree, TaskStatus


def session_root(workspace: Path) -> Path:
    return workspace / ".labhandler" / "sessions"


def session_dir(workspace: Path, thread_id: str) -> Path:
    return session_root(workspace) / thread_id


def _fsync_file(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_tree(sdir: Path, tree: TaskTree) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / "STATE.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(tree.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _fsync_file(path)


def load_tree(sdir: Path) -> TaskTree | None:
    path = sdir / "STATE.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return TaskTree.from_dict(data)


def write_text(sdir: Path, name: str, content: str) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / name
    path.write_text(content, encoding="utf-8")
    _fsync_file(path)


def read_text(sdir: Path, name: str) -> str:
    path = sdir / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def append_notes(sdir: Path, text: str) -> None:
    if not text.strip():
        return
    existing = read_text(sdir, "NOTES.md")
    body = existing.rstrip() + "\n\n" + text.strip() + "\n"
    write_text(sdir, "NOTES.md", body)


def latest_incomplete(workspace: Path) -> tuple[str, TaskTree] | None:
    root = session_root(workspace)
    if not root.is_dir():
        return None
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for d in dirs:
        tree = load_tree(d)
        if tree is None:
            continue
        root_task = tree.get(tree.root_id)
        if root_task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return d.name, tree
        # any running child also counts
        if any(n.status is TaskStatus.RUNNING for n in tree.nodes.values()):
            return d.name, tree
    return None


def changed_files_from_audit(workspace: Path) -> list[str]:
    path = workspace / ".labhandler" / "audit.jsonl"
    if not path.is_file():
        return []
    write_tools = {
        "write_file",
        "patch_file",
        "sandbox_file_operations",
        "sandbox_str_replace_editor",
        "write_acceptance",
        "use_skill_script",
    }
    files: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("tool") not in write_tools:
                continue
            if not str(rec.get("outcome", "")).startswith("ok"):
                continue
            args = rec.get("args") or {}
            for key in ("path", "file_path", "filename"):
                if args.get(key):
                    files.append(str(args[key]))
    except Exception:
        return files
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out
