"""MCP 客户端构建与访问入口。

职责：
1. 维护 AIO Sandbox MCP 服务连接配置；
2. 构建客户端前做可达性探活，剔除不可用服务；
3. 提供模块级单例的客户端与工具列表访问。
"""

from __future__ import annotations

import warnings
from typing import Any

from config.runtime import get_settings
from infra.net_probe import probe_port


def build_mcp_client() -> Any:
    """构建 MultiServerMCPClient，过滤掉不可达的 MCP 服务。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    mcp_url = get_settings().aio_sandbox_mcp_url
    candidates: dict[str, dict[str, Any]] = {
        "aio_sandbox": {
            "transport": "streamable_http",
            "url": mcp_url,
            "headers": {"Accept": "application/json, text/event-stream"},
        },
    }

    alive: dict[str, dict[str, Any]] = {}
    for name, cfg in candidates.items():
        if probe_port(cfg["url"]):
            alive[name] = cfg
        else:
            warnings.warn(
                f"MCP server {name} 在 {cfg['url']} 探活失败，已从注册表剔除。",
                stacklevel=2,
            )

    if not alive:
        raise RuntimeError(
            "所有 MCP 服务探活均失败。请检查：\n"
            f"- AIO Sandbox 容器是否运行于 {mcp_url}？\n"
            "- 容器健康检查是否通过（docker ps 看 health=healthy）？"
        )

    return MultiServerMCPClient(alive)


# 模块级单例缓存
_default_client: Any | None = None


def get_mcp_client() -> Any:
    global _default_client
    if _default_client is None:
        _default_client = build_mcp_client()
    return _default_client


async def get_tools() -> list[Any]:
    """异步获取全部 MCP 工具（LangChain Tool 形式）。"""
    client = get_mcp_client()
    return await client.get_tools()


def reset_mcp_client() -> None:
    """重置模块级客户端缓存，供连接拓扑变更后重建。"""
    global _default_client
    _default_client = None
