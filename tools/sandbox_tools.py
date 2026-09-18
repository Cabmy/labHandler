"""AIO Sandbox MCP 工具封装。

宿主机路径经 sandbox_workspace_path 映射到容器 /workspace；越界抛 ValueError。
call_sandbox 把 MCP 返回值收成文本：同一工具连续失败满 3 次记
[SANDBOX_UNREACHABLE]，其余记 [tool_error]。上层据此结束本场 lab（不停服务）。
sandbox_execute_code 若只收到 ack（stdout/stderr/exit_code 皆 null）会附加改走
sandbox_execute_bash 的提示。list_sandbox_tool_names 在 MCP 不可达时返回空列表。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from config.runtime import get_settings
from mcp_client import call_mcp_tool, list_mcp_tools

_SANDBOX_WORKSPACE = "/workspace"
_SANDBOX_MAX_FAILURES = 3
SANDBOX_UNREACHABLE_MARK = "[SANDBOX_UNREACHABLE]"
_sandbox_failures: dict[str, int] = {}
_PATH_KW = {"path", "file_path"}


def is_sandbox_unreachable(text: str) -> bool:
    return SANDBOX_UNREACHABLE_MARK.lower() in (text or "").lower()


def sandbox_workspace_path(path: Path | str) -> str:
    """宿主机路径 → 容器内 /workspace 路径。越界抛 ValueError。"""
    host_ws = get_settings().workspace_dir
    target = Path(path).resolve()
    rel = target.relative_to(host_ws)
    return f"{_SANDBOX_WORKSPACE}/{rel}".replace("\\", "/") if str(rel) != "." else _SANDBOX_WORKSPACE


def _translate_path(p: str) -> str:
    if not p or not isinstance(p, str):
        return p
    if p.startswith(_SANDBOX_WORKSPACE) or not p.startswith("/"):
        return p
    try:
        return sandbox_workspace_path(p)
    except (ValueError, OSError):
        return p


def _translate_kwargs(kwargs: dict[str, Any]) -> None:
    for k in list(kwargs.keys()):
        if k in _PATH_KW and isinstance(kwargs[k], str):
            kwargs[k] = _translate_path(kwargs[k])


_ACK_ONLY_HINT = (
    "\n\n[labhandler] ack-only: the Jupyter kernel dispatched an async ack"
    " (stdout/stderr/exit_code are all null); the code may not have finished executing yet."
    " To get results synchronously, use sandbox_execute_bash to run the command instead."
)


def reset_sandbox_failure_counter() -> None:
    _sandbox_failures.clear()


def _content_to_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if hasattr(item, "text"):
                parts.append(item.text or "")
            elif isinstance(item, dict):
                parts.append(str(item.get("text", item)))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    else:
        text = str(content)
    return text


def _annotate_ack_only(text: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return text
    if (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("stdout") is None
        and payload.get("stderr") is None
        and payload.get("exit_code") is None
    ):
        return text + _ACK_ONLY_HINT
    return text


async def call_sandbox(tool_name: str, **kwargs: Any) -> str:
    _translate_kwargs(kwargs)
    try:
        result = await call_mcp_tool(tool_name, kwargs)
    except Exception as e:
        _sandbox_failures[tool_name] = _sandbox_failures.get(tool_name, 0) + 1
        count = _sandbox_failures[tool_name]
        if count >= _SANDBOX_MAX_FAILURES:
            return (
                f"{SANDBOX_UNREACHABLE_MARK} sandbox tool {tool_name} failed {count} times in a row"
                f" ({type(e).__name__}: {e}); the sandbox may be unavailable"
            )
        return f"[tool_error] {type(e).__name__}: {e}"
    _sandbox_failures[tool_name] = 0
    text = _content_to_text(result)
    if tool_name == "sandbox_execute_code":
        text = _annotate_ack_only(text)
    return text


_CONVERTIBLE = {".pdf", ".docx", ".pptx", ".doc", ".ppt"}


async def sandbox_convert_to_markdown(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix not in _CONVERTIBLE:
        return (
            f"[ERROR/Validation] sandbox_convert_to_markdown is for PDF/DOCX/PPT, "
            f"not {suffix or 'this path'}"
        )
    container_path = _translate_path(file_path)
    if not container_path.startswith(("file://", "http://", "https://", "data:")):
        container_path = f"file://{container_path}"
    return await call_sandbox("sandbox_convert_to_markdown", uri=container_path)


async def list_sandbox_tool_names() -> list[str]:
    try:
        tools = await list_mcp_tools()
    except Exception:
        return []
    return [getattr(t, "name", "") for t in tools if getattr(t, "name", "")]


async def sandbox_run(command: str, *, timeout: float) -> tuple[int, str]:
    """在沙箱里跑一条 bash 命令，返回 (exit_code, 合并日志)。

    超时、[SANDBOX_UNREACHABLE]、[tool_error]、缺 exit_code：exit_code=-1，
    日志带标记。非 JSON / 非 dict 载荷按 exit_code=0 原样返回。
    调用方据此区分环境失败与被测命令失败。
    """
    try:
        raw = await asyncio.wait_for(
            call_sandbox("sandbox_execute_bash", cmd=command), timeout=timeout
        )
    except asyncio.TimeoutError:
        return -1, f"[TIMEOUT after {timeout}s] {command}"

    if is_sandbox_unreachable(raw):
        return -1, raw
    if raw.startswith("[tool_error]"):
        return -1, raw

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0, raw
    if not isinstance(payload, dict):
        return 0, raw

    exit_code = payload.get("exit_code")
    log = "\n".join(
        str(payload.get(k) or "") for k in ("stdout", "stderr") if payload.get(k)
    )
    if exit_code is None:
        return -1, (log or raw) + "\n[labhandler] 沙箱未返回 exit_code"
    return int(exit_code), log or raw
