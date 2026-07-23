"""net_probe - 轻量 TCP 端口可达性探测（sandbox_boot 与 mcp_client 共用）。

单一职责：解析 URL 取 host/port，用 socket.create_connection 检测端口是否可达。
不引入 docker/subprocess 等重依赖，便于被多处安全复用。
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse


def probe_port(url: str, timeout: float = 1.0) -> bool:
    """基于 TCP 连接检测目标 URL 对应端口是否可达。"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
