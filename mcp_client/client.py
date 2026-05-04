"""MCP Client - MultiServerMCPClient 封装

设计要点（PLAN §6.1 / STEPS P2.3）：
1. **当前只注册 aio_sandbox 一个 MCP server**：AIO Sandbox 容器自带 33 个 tools
   （browser_* + sandbox_convert_to_markdown 已是 fetch MCP 的超集；fetch MCP 已裁掉，
    见 PLAN §6.1 决策记录）
2. transport 用 streamable_http
3. URL 默认 127.0.0.1（避免 IPv6 解析失败）
4. HTTP header 必须带 `Accept: application/json, text/event-stream`（Phase 0 P0.5 实测）
5. **运行时降级**：build 时探活 server URL（TCP connect 1s 超时）；连不通的剔除并告警。
   单 server 时 alive 为空会直接 RuntimeError 提醒起容器。
"""

from __future__ import annotations

import os
import socket
import warnings
from typing import Any, Optional
from urllib.parse import urlparse

AIO_SANDBOX_MCP_URL = os.getenv("AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp")


def _probe(url: str, timeout: float = 1.0) -> bool:
    """TCP connect 探活；通就 True"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_mcp_client() -> Any:
    """构建 MultiServerMCPClient（懒 import + 探活降级）"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    candidates: dict[str, dict[str, Any]] = {
        "aio_sandbox": {
            "transport": "streamable_http",
            "url": AIO_SANDBOX_MCP_URL,
            "headers": {"Accept": "application/json, text/event-stream"},
        },
    }

    alive: dict[str, dict[str, Any]] = {}
    for name, cfg in candidates.items():
        if _probe(cfg["url"]):
            alive[name] = cfg
        else:
            warnings.warn(
                f"MCP server {name} 在 {cfg['url']} 探活失败，已从注册表剔除。",
                stacklevel=2,
            )

    if not alive:
        raise RuntimeError(
            "所有 MCP server 都探活失败。请检查：\n"
            f"- AIO Sandbox 容器是否运行在 {AIO_SANDBOX_MCP_URL}？\n"
            "- 容器健康检查是否通过（docker ps 看 health=healthy）？"
        )

    return MultiServerMCPClient(alive)


# 全局单例
_default_client: Optional[Any] = None


def get_mcp_client() -> Any:
    global _default_client
    if _default_client is None:
        _default_client = build_mcp_client()
    return _default_client


async def get_tools() -> list[Any]:
    """异步获取所有 MCP tools（已归一为 LangChain Tool）"""
    client = get_mcp_client()
    return await client.get_tools()


def reset_mcp_client() -> None:
    """重置单例（探活拓扑变了想重 build 时调用）"""
    global _default_client
    _default_client = None
