"""fs_tools - host workspace 文件操作与受限命令执行。

本模块提供在 host 环境安全操作作业文件的工具集。安全检查委托给
`tools/policy.py:SecurityPolicy`（P2-2 策略层）：

1. 路径沙箱化：所有路径参数经 `policy.safe_path` 解析，强制
   留在 `WORKSPACE_DIR` 内。
2. 命令白名单：`host_bash` 命令经 `policy.check_command`
   白名单检查 + 路径逃逸正则校验后才执行；
   禁止 `..` 穿越、`~` 展开、`/` 前缀绝对路径。
3. 超时与权限隔离：Bash 命令 cwd 锁在 workspace，
   硬超时（默认 30s）。
4. 出错自纠正：未授权操作被拦截并返回
   `[ERROR/PermissionError]` 结构化提示，引导 ReAct agent 用
   合法相对路径或改用沙箱执行——危险命令永远到不了
   subprocess.run。
5. 实时审计：每次调用（含被拒的）由 `ToolAuditor` 追加到
   workspace/.labhandler/audit.jsonl。

核心能力：
- read_file / write_file / patch_file：读、写、补丁 workspace 内文件。
- list_dir：列 workspace 目录结构。
- host_bash：在 host 执行安全 shell 命令（如 pytest、代码扫描）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import tool
from config.runtime import get_settings
from tools.policy import PERM_HINT, get_auditor, get_policy

WORKSPACE_DIR: Path = get_settings().workspace_dir
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _perm_msg(e: PermissionError) -> str:
    """将 PermissionError 翻译成 LLM 可见文本（供 ReAct 自纠正）。"""
    return f"[ERROR/PermissionError] {e}\nHint: {PERM_HINT}"


def _denied(tool_name: str, args: dict, e: PermissionError) -> str:
    """未授权访问的统一出口：审计 denied + 返回自纠正观测。"""
    get_auditor().record(tool_name, args, f"denied:{e}")
    return _perm_msg(e)


# ─── 工具（@tool 装饰器自动生成 OpenAI schema）───────────


@tool
def read_file(path: str) -> str:
    """Read a text file inside the workspace and return its full content. path is relative to WORKSPACE_DIR."""
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
    return p.read_text(encoding="utf-8")


@tool
def write_file(path: str, content: str) -> str:
    """Write (overwrite) a text file inside the workspace. path is relative to WORKSPACE_DIR. Returns a char-count description."""
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("write_file", {"path": path}, e)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    get_auditor().record("write_file", {"path": path, "n_chars": len(content)}, "ok")
    return f"wrote {len(content)} chars to {p.relative_to(WORKSPACE_DIR)}"


@tool
def list_dir(path: str = ".") -> list[str] | str:
    """List file names in a workspace directory (single level, non-recursive). Out-of-bounds paths return an [ERROR/PermissionError] string."""
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
    return sorted(x.name for x in p.iterdir())


@tool
def patch_file(path: str, old: str, new: str) -> str:
    """Do an exact string replacement in a workspace file (old must occur exactly once, otherwise raises)."""
    try:
        p = get_policy().safe_path(path)
    except PermissionError as e:
        return _denied("patch_file", {"path": path}, e)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        get_auditor().record("patch_file", {"path": path}, "error:old_not_found")
        raise ValueError(f"patch_file: old string not found in {path}")
    if count > 1:
        get_auditor().record("patch_file", {"path": path}, f"error:old_x{count}")
        raise ValueError(f"patch_file: old string appears {count} times in {path} (must be unique)")
    new_text = text.replace(old, new, 1)
    p.write_text(new_text, encoding="utf-8")
    get_auditor().record("patch_file", {"path": path}, "ok")
    return f"patched {path} (1 occurrence)"


@tool
def host_bash(cmd: str, timeout: int = 30) -> str:
    """Run a bash command in the host workspace directory (cwd=WORKSPACE_DIR).

    Security constraints: command-name whitelist (only predefined commands such as
    pytest / python / git / ls are allowed); path-escape regex checks (rejects ..,
    absolute-path prefixes, ~ expansion, etc.); cwd forced to WORKSPACE_DIR; default
    timeout 30s.

    Usage: `pytest -v` / `ls -la` / `python solution.py`
    Returns merged stdout + stderr text; on out-of-bounds the command is not executed
    and an `[ERROR/PermissionError] ...` string is returned — rewrite the command and
    retry (drop absolute-path prefixes, use relative paths, or use sandbox_run_python).
    """
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
