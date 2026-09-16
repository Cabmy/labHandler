"""AIO Sandbox 的 MCP 客户端（官方 SDK streamable HTTP）。

模块级单例 session + tools 列表缓存。连上前 probe_port；探活失败抛 RuntimeError。
call_mcp_tool 失败则 reset 后重连再调一次。reset_mcp_client 清空 session/缓存，
并在已有 event loop 里异步关闭旧连接；无 running loop 时只丢引用。
"""

from typing import Any

from config.runtime import get_settings
from infra.net_probe import probe_port

_session: Any = None
_cm: Any = None
_tools_cache: list[Any] | None = None
_call_lock: Any = None


def _lock() -> Any:
    global _call_lock
    import asyncio

    if _call_lock is None:
        _call_lock = asyncio.Lock()
    return _call_lock


async def _open_session() -> Any:
    global _session, _cm
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    mcp_url = get_settings().aio_sandbox_mcp_url
    if not probe_port(mcp_url):
        raise RuntimeError(
            f"MCP 探活失败：{mcp_url}。请确认 AIO Sandbox 容器在跑且 health=healthy。"
        )
    _cm = streamable_http_client(mcp_url)
    read, write, *_ = await _cm.__aenter__()
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
    async with _lock():
        if _tools_cache is not None:
            return _tools_cache
        session = await get_session()
        listed = await session.list_tools()
        _tools_cache = list(listed.tools)
        return _tools_cache


async def call_mcp_tool(name: str, arguments: dict[str, Any]) -> Any:
    async with _lock():
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
