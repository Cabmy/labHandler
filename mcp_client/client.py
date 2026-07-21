"""MCP 客户端构建与访问入口。

负责：
1. 维护 AIO Sandbox MCP 服务的连接配置；
2. 在构建客户端前执行可达性探测并剔除不可用服务；
3. 以模块级单例对外提供客户端与工具列表访问。
"""

from __future__ import annotations

import os
import socket
import warnings
from typing import Any, Optional
from urllib.parse import urlparse

AIO_SANDBOX_MCP_URL = os.getenv("AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp")


def _probe(url: str, timeout: float = 1.0) -> bool:
    """基于 TCP 连接检测目标 URL 对应端口是否可达。"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_mcp_client() -> Any:
    """构建 MultiServerMCPClient，并过滤不可达的 MCP 服务。"""
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


# 模块级单例缓存
_default_client: Optional[Any] = None


def get_mcp_client() -> Any:
    global _default_client
    if _default_client is None:
        _default_client = build_mcp_client()
    return _default_client


async def get_tools() -> list[Any]:
    """异步获取所有 MCP 工具（LangChain Tool 形态）。"""
    client = get_mcp_client()
    return await client.get_tools()


def reset_mcp_client() -> None:
    """重置模块级客户端缓存，供连接拓扑变化后重建使用。"""
    global _default_client
    _default_client = None
