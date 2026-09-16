"""运行时配置中心：config/.env 读成一份不可变的 RuntimeSettings。

进程内单例。缺项或非法值在 get_settings() 时抛 ConfigError，无静默回落。
硬约束：LLM_API_KEY 非空；PRO_MODEL / FLASH_MODEL 非空；
OUTPUT_RESERVE_TOKENS < CONTEXT_BUDGET_TOKENS。
"""

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


class ConfigError(RuntimeError):
    """配置缺失或不合法。只在 get_settings() 时抛出。"""


# LLM_DEFAULT_HEADERS 为空时使用。AgentRouter 缺这组头会 401 unauthorized client detected。
_DEFAULT_LLM_HEADERS = {
    "User-Agent": "claude-cli/1.0.108 (external, cli)",
    "x-app": "cli",
    "anthropic-version": "2023-06-01",
}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """读整数环境变量。缺省用 default；非整数或低于 minimum 抛 ConfigError。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as e:
            raise ConfigError(f"{name} 必须是整数，实际是 {raw!r}") from e
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 必须 >= {minimum}，实际是 {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    """读浮点环境变量。缺省用 default；非数字或越界抛 ConfigError。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as e:
            raise ConfigError(f"{name} 必须是浮点数，实际是 {raw!r}") from e
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} 必须 >= {minimum}，实际是 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} 必须 <= {maximum}，实际是 {value}")
    return value


def _env_str(name: str, default: str = "") -> str:
    """读字符串环境变量并 strip。未设置时用 default。"""
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool) -> bool:
    """读布尔环境变量。false/0/no/off（大小写不敏感）为假，其余非空为真。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def _env_headers() -> dict[str, str]:
    """LLM_DEFAULT_HEADERS：合法 JSON 对象 → str→str；空则 _DEFAULT_LLM_HEADERS；非法 JSON 抛 ConfigError。"""
    raw = _env_str("LLM_DEFAULT_HEADERS")
    if not raw:
        return dict(_DEFAULT_LLM_HEADERS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"LLM_DEFAULT_HEADERS 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise ConfigError("LLM_DEFAULT_HEADERS 必须是 JSON 对象")
    return {str(k): str(v) for k, v in data.items()}


@dataclass(frozen=True)
class RuntimeSettings:
    """一次启动的全部旋钮。frozen：读完后进程内不再变。"""

    # 工作区 / 技能 / 画像 / 记忆库路径
    workspace_dir: Path
    skills_dir: Path
    profile_path: Path
    memory_db_path: Path
    cards_dir: Path
    # Chat
    llm_base_url: str
    llm_api_key: str
    pro_model: str
    flash_model: str
    llm_default_headers: dict[str, str]
    # Embedding
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    # 预算与控制
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
    # 上下文
    context_budget_tokens: int
    compact_trigger_ratio: float
    compact_keep_recent_turns: int
    output_reserve_tokens: int
    # 可观测
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_host: str
    langfuse_environment: str
    trace_jsonl_enabled: bool
    # 沙箱
    aio_sandbox_mcp_url: str
    aio_sandbox_image: str
    aio_sandbox_port: int
    lab_autostart_sandbox: bool
    # 检索
    proxy: str | None
    search_max_retries: int
    search_retry_delay: int

    @property
    def traces_path(self) -> Path:
        """本地 JSONL 轨迹：{workspace_dir}/.labhandler/traces.jsonl。"""
        return self.workspace_dir / ".labhandler" / "traces.jsonl"


@lru_cache(maxsize=1)
def get_settings() -> RuntimeSettings:
    """返回进程内唯一 RuntimeSettings。首次调用读环境；非法值抛 ConfigError。"""
    llm_key = _env_str("LLM_API_KEY")
    if not llm_key:
        raise ConfigError("LLM_API_KEY 未配置。复制 config/.env.example 到 config/.env 并填入密钥。")

    pro_model = _env_str("PRO_MODEL", "glm-5.3")
    flash_model = _env_str("FLASH_MODEL", "deepseek-v4-flash")
    if not pro_model or not flash_model:
        raise ConfigError("PRO_MODEL 与 FLASH_MODEL 不能为空")

    context_budget = _env_int("CONTEXT_BUDGET_TOKENS", 200000, minimum=4096)
    output_reserve = _env_int("OUTPUT_RESERVE_TOKENS", 8192, minimum=256)
    if output_reserve >= context_budget:
        raise ConfigError(
            f"OUTPUT_RESERVE_TOKENS({output_reserve}) 必须小于 CONTEXT_BUDGET_TOKENS({context_budget})"
        )

    return RuntimeSettings(
        workspace_dir=Path(_env_str("WORKSPACE_DIR", "./workspace")).resolve(),
        skills_dir=Path(_env_str("SKILLS_DIR", "./skills")).resolve(),
        profile_path=Path(_env_str("PROFILE_PATH", "./profile/me.yaml")).resolve(),
        memory_db_path=Path(_env_str("MEMORY_DB_PATH", "./.labhandler_data/memory.db")).resolve(),
        cards_dir=Path(_env_str("CARDS_DIR", "./.labhandler_data/cards")).resolve(),
        llm_base_url=_env_str("LLM_BASE_URL", "https://agentrouter.org/v1"),
        llm_api_key=llm_key,
        pro_model=pro_model,
        flash_model=flash_model,
        llm_default_headers=_env_headers(),
        embedding_base_url=_env_str("EMBEDDING_BASE_URL", "https://llmapi.paratera.com/v1/"),
        embedding_api_key=_env_str("EMBEDDING_API_KEY"),
        embedding_model=_env_str("EMBEDDING_MODEL", "GLM-Embedding-3"),
        pro_step_budget=_env_int("PRO_STEP_BUDGET", 20, minimum=1),
        flash_step_budget=_env_int("FLASH_STEP_BUDGET", 30, minimum=1),
        task_wall_time_s=_env_float("TASK_WALL_TIME_S", 900.0, minimum=10.0),
        tool_timeout_s=_env_float("TOOL_TIMEOUT_S", 120.0, minimum=1.0),
        transient_retry_max=_env_int("TRANSIENT_RETRY_MAX", 3, minimum=1),
        validation_retry_max=_env_int("VALIDATION_RETRY_MAX", 3, minimum=1),
        stagnation_repeat_threshold=_env_int("STAGNATION_REPEAT_THRESHOLD", 3, minimum=2),
        stagnation_grace_steps=_env_int("STAGNATION_GRACE_STEPS", 2, minimum=0),
        consecutive_logic_failure_max=_env_int("CONSECUTIVE_LOGIC_FAILURE_MAX", 3, minimum=1),
        max_parallel_readonly_workers=_env_int("MAX_PARALLEL_READONLY_WORKERS", 3, minimum=1),
        llm_max_concurrency=_env_int("LLM_MAX_CONCURRENCY", 4, minimum=1),
        context_budget_tokens=context_budget,
        compact_trigger_ratio=_env_float("COMPACT_TRIGGER_RATIO", 0.7, minimum=0.1, maximum=0.95),
        compact_keep_recent_turns=_env_int("COMPACT_KEEP_RECENT_TURNS", 3, minimum=1),
        output_reserve_tokens=output_reserve,
        langfuse_public_key=_env_str("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=_env_str("LANGFUSE_SECRET_KEY"),
        langfuse_host=_env_str("LANGFUSE_BASE_URL")
        or _env_str("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        langfuse_environment=_env_str("LANGFUSE_TRACING_ENVIRONMENT", "development"),
        trace_jsonl_enabled=_env_bool("TRACE_JSONL_ENABLED", True),
        aio_sandbox_mcp_url=_env_str("AIO_SANDBOX_MCP_URL", "http://127.0.0.1:8080/mcp"),
        aio_sandbox_image=_env_str(
            "AIO_SANDBOX_IMAGE",
            "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest",
        ),
        aio_sandbox_port=_env_int("AIO_SANDBOX_PORT", 8080, minimum=1),
        lab_autostart_sandbox=_env_bool("LAB_AUTOSTART_SANDBOX", True),
        proxy=_env_str("PROXY") or None,
        search_max_retries=_env_int("SEARCH_MAX_RETRIES", 2, minimum=0),
        search_retry_delay=_env_int("SEARCH_RETRY_DELAY", 3, minimum=0),
    )
