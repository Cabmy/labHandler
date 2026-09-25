"""python -m server 启动入口（uvicorn，默认仅本机 127.0.0.1:8000）。"""

import os

import uvicorn


def main() -> None:
    host = os.getenv("LAB_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("LAB_WEB_PORT", "8000"))
    from infra.sandbox_boot import ensure_sandbox, stop_sandbox

    try:
        ensure_sandbox(log=print)
    except Exception as e:
        print(f"[server] sandbox 自动启动检查失败（已跳过）：{e}")
    try:
        uvicorn.run("server.app:app", host=host, port=port, log_level="info")
    finally:
        stop_sandbox(log=print)


if __name__ == "__main__":
    main()
