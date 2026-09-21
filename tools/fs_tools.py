"""host workspace 文件操作。路径与命令一律经 SecurityPolicy，调用记入 ToolAuditor。

不变量：读写只落在 WORKSPACE_DIR 内。越界 / 非白名单命令以
[ERROR/PermissionError] 字符串返回（含 PERM_HINT），到不了 OS。
read 默认从第 1 行起，可传 1-based offset / limit（行数）；单次最多 80_000 字符，
截断时标明下一行 offset。list_dir / glob 上限 80 条；grep 上限 40 条。
patch_file 要求 old 在文件中恰好出现一次。host_bash cwd=WORKSPACE_DIR，
默认超时 30s。FileNotFound / IsADirectory / 非法 regex 走错误通道并审计。
"""

import subprocess
from pathlib import Path

from config.runtime import get_settings
from tools.policy import PERM_HINT, get_auditor, get_policy
from tools.workspace_utils import READ_CHAR_CAP, grep_in_roots, is_excluded_path

WORKSPACE_DIR: Path = get_settings().workspace_dir
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

_GREP_LIMIT = 40
_GLOB_LIMIT = 80


def _perm_msg(e: PermissionError) -> str:
    return f"[ERROR/PermissionError] {e}\nHint: {PERM_HINT}"


def _denied(tool_name: str, args: dict, e: PermissionError) -> str:
    get_auditor().record(tool_name, args, f"denied:{e}")
    return _perm_msg(e)


def read_file(path: str, offset: int = 1, limit: int | None = None, role: str = "readonly") -> str:
    try:
        p = get_policy().check_read(path, role)
    except PermissionError as e:
        return _denied("read_file", {"path": path}, e)
    if not p.exists():
        get_auditor().record(
            "read_file", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"File not found: {path}")
    if not p.is_file():
        get_auditor().record(
            "read_file", {"path": path}, "error:IsADirectoryError")
        raise IsADirectoryError(f"Not a file: {path}")
    if offset < 1:
        return "[ERROR/Validation] offset must be a 1-based line number"
    if limit is not None and limit < 1:
        return "[ERROR/Validation] limit must be a positive line count"

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    n = len(lines)
    start = offset - 1
    if start >= n:
        get_auditor().record(
            "read_file", {"path": path, "offset": offset}, "ok")
        return f"[ERROR/Validation] offset {offset} past end ({n} lines)"
    end = n if limit is None else min(n, start + limit)
    chunk = "".join(lines[start:end])
    next_line = end + 1
    leftover_lines = n - end
    if len(chunk) > READ_CHAR_CAP:
        cut = chunk[:READ_CHAR_CAP]
        nl = cut.rfind("\n")
        if nl >= 0:
            cut = cut[: nl + 1]
        next_line = offset + cut.count("\n")
        leftover_lines = n - (next_line - 1)
        chunk = cut.rstrip("\n") + (
            f"\n[truncated at char cap {READ_CHAR_CAP}; next offset={next_line}, {leftover_lines} lines remain]"
        )
    elif leftover_lines > 0:
        chunk += f"\n[truncated {leftover_lines} lines; next offset={next_line}]"
    get_auditor().record(
        "read_file",
        {"path": path, "offset": offset,
            "limit": limit, "n_chars": len(chunk)},
        "ok",
    )
    return chunk


def _gate_via_write_file(path: str) -> str | None:
    """workspace/acceptance/ 不是门禁目录；门禁必须走 write_acceptance。"""
    rel = path.replace("\\", "/").lstrip("./")
    if rel == "acceptance" or rel.startswith("acceptance/"):
        return (
            f"[ERROR/PermissionError] {path!r} is not the harness gate. "
            "Call write_acceptance(task_id, filename, content)."
        )
    return None


def write_file(path: str, content: str, role: str = "write") -> str:
    blocked = _gate_via_write_file(path)
    if blocked:
        get_auditor().record("write_file", {
            "path": path}, "denied:workspace_acceptance")
        return blocked
    try:
        p = get_policy().check_write(path, role)
    except PermissionError as e:
        return _denied("write_file", {"path": path}, e)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    get_auditor().record("write_file", {
        "path": path, "n_chars": len(content)}, "ok")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


def list_dir(path: str = ".", role: str = "readonly") -> list[str] | str:
    try:
        p = get_policy().check_read(path, role)
    except PermissionError as e:
        return _denied("list_dir", {"path": path}, e)
    if not p.exists():
        get_auditor().record(
            "list_dir", {"path": path}, "error:FileNotFoundError")
        raise FileNotFoundError(f"Directory not found: {path}")
    if not p.is_dir():
        get_auditor().record(
            "list_dir", {"path": path}, "error:NotADirectoryError")
        raise NotADirectoryError(f"Not a directory: {path}")
    get_auditor().record("list_dir", {"path": path}, "ok")
    rel = p.relative_to(WORKSPACE_DIR)
    inside_dot = is_excluded_path(rel)
    names = sorted(
        x.name
        for x in p.iterdir()
        if (inside_dot or not (x.name.startswith(".") or x.name == "__pycache__"))
        and not get_policy().is_control_file(x)
    )
    if len(names) > _GLOB_LIMIT:
        return names[:_GLOB_LIMIT] + [f"[truncated {len(names) - _GLOB_LIMIT} entries]"]
    return names


def patch_file(path: str, old: str, new: str, role: str = "write") -> str:
    blocked = _gate_via_write_file(path)
    if blocked:
        get_auditor().record("patch_file", {
            "path": path}, "denied:workspace_acceptance")
        return blocked
    try:
        p = get_policy().check_write(path, role)
    except PermissionError as e:
        return _denied("patch_file", {"path": path}, e)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        get_auditor().record("patch_file", {
            "path": path}, "error:old_not_found")
        raise ValueError(f"patch_file: old string not found in {path}")
    if count > 1:
        get_auditor().record("patch_file", {
            "path": path}, f"error:old_x{count}")
        raise ValueError(
            f"patch_file: old string appears {count} times in {path} (must be unique)"
        )
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    get_auditor().record("patch_file", {"path": path}, "ok")
    return f"patched {path} (1 occurrence)"


def glob_files(pattern: str, limit: int = _GLOB_LIMIT) -> list[str]:
    root = WORKSPACE_DIR
    get_policy().check_glob(pattern)
    matches: list[str] = []
    for p in root.glob(pattern):
        if not p.is_file() or not get_policy().contains(p):
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
    """在 workspace 内按正则搜索文件，委托给公共 grep_in_roots。"""
    root = WORKSPACE_DIR
    get_policy().check_glob(glob)

    def _skip(p: Path) -> bool:
        rel = p.relative_to(root)
        return is_excluded_path(rel) or not get_policy().contains(p)

    result = grep_in_roots(
        pattern,
        [root],
        limit,
        file_filter=_skip,
        path_fmt=lambda p: str(p.relative_to(root)),
    )
    get_auditor().record("grep_files", {"pattern": pattern}, "ok")
    return result


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
        get_auditor().record(
            "host_bash", {"cmd": cmd}, f"error:timeout_{timeout}s")
        return f"[TIMEOUT after {timeout}s]\n{e.stdout or ''}\n{e.stderr or ''}"
    get_auditor().record(
        "host_bash", {"cmd": cmd}, f"ok:exit={result.returncode}")
    out = result.stdout or ""
    err = result.stderr or ""
    tail = f"\n[exit={result.returncode}]"
    if err:
        return f"{out}\n--- stderr ---\n{err}{tail}"
    return out + tail
