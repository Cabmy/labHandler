"""host workspace 文件操作（无 langchain）。安全检查委托 SecurityPolicy。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from config.runtime import get_settings
from tools.policy import PERM_HINT, get_auditor, get_policy
from tools.workspace_utils import is_excluded_path

WORKSPACE_DIR: Path = get_settings().workspace_dir
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

_GREP_LIMIT = 40
_GLOB_LIMIT = 80


def _perm_msg(e: PermissionError) -> str:
    return f"[ERROR/PermissionError] {e}\nHint: {PERM_HINT}"


def _denied(tool_name: str, args: dict, e: PermissionError) -> str:
    get_auditor().record(tool_name, args, f"denied:{e}")
    return _perm_msg(e)


def read_file(path: str) -> str:
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("read_file", {"path": path}, e)
    if not p.exists():
        get_auditor().record("read_file", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        get_auditor().record("read_file", {"path": path}, "error:IsADirectoryError")
        raise IsADirectoryError(f"Not a file: {path}")
    get_auditor().record("read_file", {"path": path}, "ok")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > 80_000:
        return text[:80_000] + f"\n[truncated {len(text) - 80_000} chars]"
    return text


def write_file(path: str, content: str, role: str = "write") -> str:
    try:
        p = get_policy().check_write(path, role)
    except PermissionError as e:
        return _denied("write_file", {"path": path}, e)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    get_auditor().record("write_file", {"path": path, "n_chars": len(content)}, "ok")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


def list_dir(path: str = ".") -> list[str] | str:
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("list_dir", {"path": path}, e)
    if not p.exists():
        get_auditor().record("list_dir", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"Directory not found: {path}")
    if not p.is_dir():
        get_auditor().record("list_dir", {"path": path}, "error:NotADirectoryError")
        raise NotADirectoryError(f"Not a directory: {path}")
    get_auditor().record("list_dir", {"path": path}, "ok")
    names = sorted(x.name for x in p.iterdir())
    if len(names) > _GLOB_LIMIT:
        return names[:_GLOB_LIMIT] + [f"[truncated {len(names) - _GLOB_LIMIT} entries]"]
    return names


def patch_file(path: str, old: str, new: str, role: str = "write") -> str:
    try:
        p = get_policy().check_write(path, role)
    except PermissionError as e:
        return _denied("patch_file", {"path": path}, e)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        get_auditor().record("patch_file", {"path": path}, "error:old_not_found")
        raise ValueError(f"patch_file: old string not found in {path}")
    if count > 1:
        get_auditor().record("patch_file", {"path": path}, f"error:old_x{count}")
        raise ValueError(
            f"patch_file: old string appears {count} times in {path} (must be unique)"
        )
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    get_auditor().record("patch_file", {"path": path}, "ok")
    return f"patched {path} (1 occurrence)"


def glob_files(pattern: str, limit: int = _GLOB_LIMIT) -> list[str]:
    root = WORKSPACE_DIR
    matches: list[str] = []
    for p in root.glob(pattern):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_excluded_path(rel):
            continue
        matches.append(str(rel))
        if len(matches) >= limit:
            matches.append(f"[truncated limit={limit}]")
            break
    get_auditor().record("glob_files", {"pattern": pattern}, "ok")
    return matches


def grep_files(pattern: str, glob: str = "**/*", limit: int = _GREP_LIMIT) -> str:
    import re

    root = WORKSPACE_DIR
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[ERROR/Validation] invalid regex: {e}"
    hits: list[str] = []
    truncated = False
    for p in root.glob(glob):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if is_excluded_path(rel) and ".labhandler" not in rel.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}:{line[:200]}")
                if len(hits) >= limit:
                    truncated = True
                    break
        if truncated:
            break
    get_auditor().record("grep_files", {"pattern": pattern}, "ok")
    if not hits:
        return "(no matches)"
    body = "\n".join(hits)
    if truncated:
        body += f"\n[truncated limit={limit}]"
    return body


def host_bash(cmd: str, timeout: int = 30) -> str:
    try:
        get_policy().check_command(cmd)
    except PermissionError as e:
        return _denied("host_bash", {"cmd": cmd}, e)
    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(WORKSPACE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        get_auditor().record("host_bash", {"cmd": cmd}, f"error:timeout_{timeout}s")
        return f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}"
    get_auditor().record("host_bash", {"cmd": cmd}, f"ok:exit={result.returncode}")
    out = result.stdout or ""
    err = result.stderr or ""
    tail = f"\n[exit={result.returncode}]"
    if err:
        return f"{out}\n--- stderr ---\n{err}{tail}"
    return out + tail
