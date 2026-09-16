"""MCP 客户端：官方 SDK streamable HTTP，不用 langchain。"""

from __future__ import annotations

import warnings
from typing import Any

from config.runtime import get_settings
from infra.net_probe import probe_port

_session: Any = None
_cm: Any = None
_tools_cache: list[Any] | None = None


async def _open_session() -> Any:
    global _session, _cm
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    mcp_url = get_settings().aio_sandbox_mcp_url
    if not probe_port(mcp_url):
        raise RuntimeError(
            f"MCP 探活失败：{mcp_url}。请确认 AIO Sandbox 容器在跑且 health=healthy。"
        )
    _cm = streamablehttp_client(
        mcp_url,
        headers={"Accept": "application/json, text/event-stream"},
    )
    read, write, _sid = await _cm.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    _session = session
    return session


async def get_session() -> Any:
    global _session
    if _session is None:
        return await _open_session()
    return _session


async def list_mcp_tools() -> list[Any]:
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache
    session = await get_session()
    listed = await session.list_tools()
    _tools_cache = list(listed.tools)
    return _tools_cache


async def call_mcp_tool(name: str, arguments: dict[str, Any]) -> Any:
    session = await get_session()
    try:
        return await session.call_tool(name, arguments)
    except Exception:
        reset_mcp_client()
        session = await get_session()
        return await session.call_tool(name, arguments)


def reset_mcp_client() -> None:
    global _session, _cm, _tools_cache
    _tools_cache = None
    sess, cm = _session, _cm
    _session = None
    _cm = None

    async def _close() -> None:
        if sess is not None:
            try:
                await sess.__aexit__(None, None, None)
            except Exception:
                pass
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    try:
        import asyncio

        loop = asyncio.get_running_loop()
        loop.create_task(_close())
    except RuntimeError:
        pass
