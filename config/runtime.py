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


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    return RuntimeSettings(
        workspace_dir=Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve(),
        skills_dir=Path(os.getenv("SKILLS_DIR", "./skills")).resolve(),
        planner_archive_top_k=_env_int("PLANNER_ARCHIVE_TOP_K", 3),
    )
