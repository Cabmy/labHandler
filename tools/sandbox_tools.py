"""sandbox_tools - AIO Sandbox MCP 封装（官方 SDK，无 langchain）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp_client import call_mcp_tool, list_mcp_tools

_SANDBOX_WORKSPACE = "/workspace"
_SANDBOX_MAX_FAILURES = 3
_sandbox_failures: dict[str, int] = {}
_PATH_KW = {"path", "file_path"}


def _translate_path(p: str) -> str:
    if not p or not isinstance(p, str):
        return p
    if p.startswith(_SANDBOX_WORKSPACE):
        return p
    if not p.startswith("/"):
        return p
    try:
        host_ws = Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve()
        target = Path(p).resolve()
        try:
            rel = target.relative_to(host_ws)
        except ValueError:
            return p
        return f"{_SANDBOX_WORKSPACE}/{rel}".replace("\\", "/")
    except Exception:
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
                f"[SANDBOX_UNREACHABLE] sandbox tool {tool_name} failed {count} times in a row"
                f" ({type(e).__name__}: {e}); the sandbox may be unavailable"
            )
        return f"[tool_error] {type(e).__name__}: {e}"
    _sandbox_failures[tool_name] = 0
    text = _content_to_text(result)
    if tool_name == "sandbox_execute_code":
        text = _annotate_ack_only(text)
    return text


async def sandbox_convert_to_markdown(file_path: str) -> str:
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
