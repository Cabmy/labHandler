"""运行时配置中心：集中管理常用环境变量并提供类型化访问。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class RuntimeSettings:
    workspace_dir: Path
    skills_dir: Path
    planner_archive_top_k: int
    max_step_retry: int
    max_react_iter: int
    max_replan_iter: int
    checkpoint_db_path: Path
    memory_db_path: Path
    chroma_persist_dir: Path
    profile_path: Path
    aio_sandbox_mcp_url: str
    proxy: str | None
    search_max_retries: int
    search_retry_delay: int


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    return RuntimeSettings(
        workspace_dir=Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve(),
        skills_dir=Path(os.getenv("SKILLS_DIR", "./skills")).resolve(),
        planner_archive_top_k=_env_int("PLANNER_ARCHIVE_TOP_K", 3),
        max_step_retry=_env_int("MAX_STEP_RETRY", 2),
        max_react_iter=_env_int("MAX_REACT_ITER", 6),
        max_replan_iter=_env_int("MAX_REPLAN_ITER", 2),
        checkpoint_db_path=Path(
            os.getenv("CHECKPOINT_DB_PATH", "./.labhandler_data/checkpoints.db")
        ).resolve(),
        memory_db_path=Path(
            os.getenv("MEMORY_DB_PATH", "./.labhandler_data/memory.db")
        ).resolve(),
        chroma_persist_dir=Path(
            os.getenv("CHROMA_PERSIST_DIR", "./.labhandler_data/chroma")
        ).resolve(),
        profile_path=Path(
            os.getenv("PROFILE_PATH", "./profile/me.yaml")
        ).resolve(),
        aio_sandbox_mcp_url=_env_str(
            "AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp"
        ),
        proxy=os.getenv("PROXY") or None,
        search_max_retries=_env_int("SEARCH_MAX_RETRIES", 2),
        search_retry_delay=_env_int("SEARCH_RETRY_DELAY", 3),
    )
