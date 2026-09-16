"""net_probe - 轻量 TCP 端口可达性探测（sandbox_boot 与 mcp_client 共用）。

单一职责：解析 URL 的 host/port，用 socket.create_connection 检查
端口是否可达。不依赖 docker/subprocess 等重依赖，可安全跨模块复用。
"""

import socket
from urllib.parse import urlparse


def probe_port(url: str, timeout: float = 1.0) -> bool:
    """检查目标 URL 的端口是否可经 TCP 连接到达。"""
    try:
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
