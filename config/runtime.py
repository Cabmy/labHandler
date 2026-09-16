"""运行时配置中心：集中读取 config/.env 并提供类型化访问。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


_DEFAULT_LLM_HEADERS = {
    "User-Agent": "claude-cli/1.0.108 (external, cli)",
    "x-app": "cli",
    "anthropic-version": "2023-06-01",
}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_headers() -> dict[str, str]:
    raw = os.getenv("LLM_DEFAULT_HEADERS", "").strip()
    if not raw:
        return dict(_DEFAULT_LLM_HEADERS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(_DEFAULT_LLM_HEADERS)
    if not isinstance(data, dict):
        return dict(_DEFAULT_LLM_HEADERS)
    return {str(k): str(v) for k, v in data.items()}


@dataclass(frozen=True)
class RuntimeSettings:
    workspace_dir: Path
    skills_dir: Path
    profile_path: Path
    memory_db_path: Path
    cards_dir: Path
    llm_base_url: str
    llm_api_key: str
    pro_model: str
    flash_model: str
    llm_default_headers: dict[str, str]
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    pro_step_budget: int
    flash_step_budget: int
    task_wall_time_s: float
    tool_timeout_s: float
    transient_retry_max: int
    validation_retry_max: int
    stagnation_repeat_threshold: int
    stagnation_grace_steps: int
    consecutive_logic_failure_max: int
    max_parallel_readonly_workers: int
    llm_max_concurrency: int
    context_budget_tokens: int
    compact_trigger_ratio: float
    otel_exporter_otlp_endpoint: str | None
    aio_sandbox_mcp_url: str
    aio_sandbox_image: str
    aio_sandbox_port: int
    lab_autostart_sandbox: bool
    proxy: str | None
    search_max_retries: int
    search_retry_delay: int


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    embed_key = _env_str("EMBEDDING_API_KEY", "") or _env_str("PARATERA_API_KEY", "")
    embed_url = _env_str("EMBEDDING_BASE_URL", "") or _env_str(
        "PARATERA_BASE_URL", "https://llmapi.paratera.com/v1/"
    )
    embed_model = _env_str("EMBEDDING_MODEL", "") or _env_str(
        "PARATERA_EMBEDDING_MODEL", "GLM-Embedding-3"
    )
    otel = _env_str("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or None
    autostart = _env_str("LAB_AUTOSTART_SANDBOX", "true").lower() not in {
        "false",
        "0",
        "no",
    }
    return RuntimeSettings(
        workspace_dir=Path(os.getenv("WORKSPACE_DIR", "./workspace")).resolve(),
        skills_dir=Path(os.getenv("SKILLS_DIR", "./skills")).resolve(),
        profile_path=Path(os.getenv("PROFILE_PATH", "./profile/me.yaml")).resolve(),
        memory_db_path=Path(
            os.getenv("MEMORY_DB_PATH", "./.labhandler_data/memory.db")
        ).resolve(),
        cards_dir=Path(os.getenv("CARDS_DIR", "./.labhandler_data/cards")).resolve(),
        llm_base_url=_env_str("LLM_BASE_URL", "https://agentrouter.org/v1"),
        llm_api_key=_env_str("LLM_API_KEY", ""),
        pro_model=_env_str("PRO_MODEL", "glm-5.3"),
        flash_model=_env_str("FLASH_MODEL", "deepseek-v4-flash"),
        llm_default_headers=_env_headers(),
        embedding_base_url=embed_url,
        embedding_api_key=embed_key,
        embedding_model=embed_model,
        pro_step_budget=_env_int("PRO_STEP_BUDGET", 20),
        flash_step_budget=_env_int("FLASH_STEP_BUDGET", 30),
        task_wall_time_s=_env_float("TASK_WALL_TIME_S", 900.0),
        tool_timeout_s=_env_float("TOOL_TIMEOUT_S", 120.0),
        transient_retry_max=_env_int("TRANSIENT_RETRY_MAX", 3),
        validation_retry_max=_env_int("VALIDATION_RETRY_MAX", 3),
        stagnation_repeat_threshold=_env_int("STAGNATION_REPEAT_THRESHOLD", 3),
        stagnation_grace_steps=_env_int("STAGNATION_GRACE_STEPS", 2),
        consecutive_logic_failure_max=_env_int("CONSECUTIVE_LOGIC_FAILURE_MAX", 3),
        max_parallel_readonly_workers=_env_int("MAX_PARALLEL_READONLY_WORKERS", 3),
        llm_max_concurrency=_env_int("LLM_MAX_CONCURRENCY", 4),
        context_budget_tokens=_env_int("CONTEXT_BUDGET_TOKENS", 32000),
        compact_trigger_ratio=_env_float("COMPACT_TRIGGER_RATIO", 0.7),
        otel_exporter_otlp_endpoint=otel,
        aio_sandbox_mcp_url=_env_str(
            "AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp"
        ),
        aio_sandbox_image=_env_str(
            "AIO_SANDBOX_IMAGE",
            "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest",
        ),
        aio_sandbox_port=_env_int("AIO_SANDBOX_PORT", 8080),
        lab_autostart_sandbox=autostart,
        proxy=os.getenv("PROXY") or None,
        search_max_retries=_env_int("SEARCH_MAX_RETRIES", 2),
        search_retry_delay=_env_int("SEARCH_RETRY_DELAY", 3),
    )
