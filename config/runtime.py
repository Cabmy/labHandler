"""运行时配置中心：集中管理高频环境变量并提供类型化读取。"""

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


@dataclass(frozen=True)
class RuntimeSettings:
    workspace_dir: Path
    skills_dir: Path
    planner_archive_top_k: int
    max_step_retry: int
    max_react_iter: int
    max_replan_iter: int
    checkpoint_db_path: Path


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
    )
