"""LLM Provider 封装：Paratera (DeepSeek-V4-Pro 思考模式) + Ollama 兜底

Phase 0 实测要点
----------------
1. reasoning_effort 必须顶层传给 ChatOpenAI（model_kwargs 会触发 langchain-openai 1.2 警告）
2. extra_body={"thinking":{"type":"enabled"}} 透传给 Paratera 启用思考模式
3. Paratera 不强制 reasoning_content 回灌（与文档不一致），langchain-openai 当前版本也不暴露
   该字段到 AIMessage.additional_kwargs；因此 messages 拼装层只 warn 不 raise
4. 思考模式下 temperature / top_p 是 no-op，不暴露

Fix 1 — 单例 + SQLite 缓存
--------------------------
- 改造 get_llm() / get_embeddings() 为线程安全单例（按 (provider, streaming) / provider 键），
  避免每次调用都新建 ChatOpenAI 客户端 + 重置 httpx 连接池（Phase 0 实测下高频调用会触发
  "RemoteProtocolError: Server disconnected" 偶发）。
- 模块首次进入 get_llm/get_embeddings 时通过 langchain_core.globals.set_llm_cache 注入
  SQLiteCache，让 Intake / Profile-intent / Verifier-coverage 这类「相同输入应得相同输出」
  的非 ReAct 调用直接命中缓存，省 token 也省时间；ReAct 工具循环里相同 prompt 几乎不会
  重复，缓存几乎不会误命中。
- 通过 LLM_CACHE_ENABLED=0 可整体关闭；通过 LLM_CACHE_DB_PATH 指定 db 文件位置（默认
  ./.hwhandler_data/llm_cache.db）。
- 测试钩子 reset_llm_singletons() 清空所有缓存（含 langchain 全局 cache），让测试可隔离。
"""

from __future__ import annotations

import os
import threading
import warnings
from pathlib import Path
from typing import Any, Generator, Optional

from langchain_community.cache import SQLiteCache
from langchain_core.globals import set_llm_cache
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

# 统一封装思考模式参数（避免某个 agent 漏配）
DS_V4_PRO_KWARGS: dict[str, Any] = {
    "reasoning_effort": os.getenv("PARATERA_REASONING_EFFORT", "high"),
    "extra_body": {"thinking": {"type": "enabled"}},
}


def _provider() -> str:
    return os.getenv("LLM_PROVIDER", "paratera").lower().strip()


def _require_paratera_key() -> str:
    key = os.getenv("PARATERA_API_KEY")
    if not key or key.startswith("sk-xxx"):
        raise ValueError("未配置 PARATERA_API_KEY，请检查 .env")
    return key


def _env_truthy(name: str, default: str = "1") -> bool:
    """env var 解析为 bool（接受 1/true/yes/on，不区分大小写）。"""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# ─── 单例 + SQLite 缓存基础设施 ─────────────────────────────────
#
# 设计：
# 1) _LLM_INSTANCES / _EMBEDDINGS_INSTANCES 缓存按 env 当前值构造的客户端实例。
#    key 故意带上 provider 字符串，使 LLM_PROVIDER 在运行期切换时不会复用旧 provider 实例
#    （但同一 provider 下 PARATERA_BASE_URL 等参数变化不会重建——这是常规约定，rare case）。
# 2) double-checked locking：减少热路径下的锁开销（Python GIL 下读字典本就线程安全，
#    但写入 + 状态变更需要锁防止重复构造）。
# 3) SQLite cache 初始化做成「全模块一次」幂等，不和单例字典共用 key。

_LLM_INSTANCES: dict[tuple[str, bool], Any] = {}
_EMBEDDINGS_INSTANCES: dict[str, Any] = {}
_CACHE_INITIALIZED: bool = False
_LOCK = threading.Lock()


def _init_sqlite_cache_once() -> None:
    """首次调用时（按需）通过 langchain 全局 cache 钩子挂上 SQLiteCache。

    LLM_CACHE_ENABLED=0/false/no/off 时跳过，但仍标记已初始化避免每次重判 env。
    """
    global _CACHE_INITIALIZED
    if _CACHE_INITIALIZED:
        return
    with _LOCK:
        if _CACHE_INITIALIZED:
            return
        if not _env_truthy("LLM_CACHE_ENABLED", "1"):
            _CACHE_INITIALIZED = True
            return
        db_path = os.getenv(
            "LLM_CACHE_DB_PATH", "./.hwhandler_data/llm_cache.db"
        ).strip()
        # 父目录不存在则建（包括多层嵌套）
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        set_llm_cache(SQLiteCache(database_path=db_path))
        _CACHE_INITIALIZED = True


def reset_llm_singletons() -> None:
    """清空所有进程内缓存（实例 + 全局 cache 钩子）；测试用。

    生产代码不要调；切 provider / db 路径后调用，下一次 get_llm/get_embeddings 会重建。
    """
    global _CACHE_INITIALIZED
    with _LOCK:
        _LLM_INSTANCES.clear()
        _EMBEDDINGS_INSTANCES.clear()
        _CACHE_INITIALIZED = False
        set_llm_cache(None)


# ─── 实例工厂（内部构造 + 公开单例入口） ─────────────────────────


def _build_llm(provider: str, streaming: bool) -> Any:
    """实际新建 ChatModel 实例；调用方需自己做单例缓存。"""
    if provider == "ollama":
        return ChatOllama(
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:14b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            num_ctx=2048,
        )
    if provider == "paratera":
        return ChatOpenAI(
            model=os.getenv("PARATERA_LLM_MODEL", "DeepSeek-V4-Pro"),
            api_key=SecretStr(_require_paratera_key()),
            base_url=os.getenv("PARATERA_BASE_URL"),
            streaming=streaming,
            **DS_V4_PRO_KWARGS,
        )
    raise ValueError(f"不支持的 LLM_PROVIDER: {provider}（可选 paratera / ollama）")


def _build_embeddings(provider: str) -> Any:
    if provider == "ollama":
        return OllamaEmbeddings(
            model=os.getenv("OLLAMA_EMBEDDING_MODEL", "bge-m3"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        )
    if provider == "paratera":
        return OpenAIEmbeddings(
            model=os.getenv("PARATERA_EMBEDDING_MODEL", "GLM-Embedding-3"),
            api_key=SecretStr(_require_paratera_key()),
            base_url=os.getenv("PARATERA_BASE_URL"),
        )
    raise ValueError(f"不支持的 LLM_PROVIDER: {provider}")


def get_llm(streaming: bool = True) -> Any:
    """获取 LangChain Chat 模型实例（按 LLM_PROVIDER 选择）。

    Paratera 路径自动带上思考模式；Ollama 路径不传 thinking 参数。

    Fix 4A：默认 streaming=True，让 LangGraph stream_mode="messages" 能逐 token 透出
    AIMessageChunk；显式传 streaming=False 可用于一次性 invoke 场景（如 Intake 抽 JSON）。

    Fix 1：模块级单例 by (provider, streaming)；首次调用时挂载 SQLiteCache 全局钩子。
    """
    _init_sqlite_cache_once()
    provider = _provider()
    key = (provider, streaming)
    inst = _LLM_INSTANCES.get(key)
    if inst is not None:
        return inst
    with _LOCK:
        inst = _LLM_INSTANCES.get(key)
        if inst is not None:
            return inst
        inst = _build_llm(provider, streaming)
        _LLM_INSTANCES[key] = inst
        return inst


def get_embeddings() -> Any:
    """Embeddings 不需要思考模式参数。

    Fix 1：模块级单例 by provider；与 get_llm 共用 SQLite cache 初始化路径。
    """
    _init_sqlite_cache_once()
    provider = _provider()
    inst = _EMBEDDINGS_INSTANCES.get(provider)
    if inst is not None:
        return inst
    with _LOCK:
        inst = _EMBEDDINGS_INSTANCES.get(provider)
        if inst is not None:
            return inst
        inst = _build_embeddings(provider)
        _EMBEDDINGS_INSTANCES[provider] = inst
        return inst


# ─── 调用入口 ─────────────────────────────────────────────────────


def chat(prompt: str, system: str = "") -> str:
    """单轮 chat，返回 content 文本。"""
    llm = get_llm()
    messages: list[BaseMessage] = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    resp = llm.invoke(messages)
    content = resp.content
    return content if isinstance(content, str) else str(content)


def chat_with_tools(messages: list[BaseMessage], tools: list[Any]) -> AIMessage:
    """绑定工具后单次 invoke，返回 AIMessage（含 tool_calls）。"""
    llm = get_llm().bind_tools(tools)
    return llm.invoke(messages)


def stream_chat(
    messages: list[BaseMessage], tools: Optional[list[Any]] = None
) -> Generator[dict[str, str], None, None]:
    """流式 chat。yield {"reasoning": str, "content": str} chunk。

    Phase 0 实测：langchain-openai 当前版本不暴露 reasoning_content 到 AIMessageChunk；
    本函数兼容未来版本（若 additional_kwargs 出现 reasoning_content 自动分流）。
    """
    llm = get_llm(streaming=True)
    if tools:
        llm = llm.bind_tools(tools)
    for chunk in llm.stream(messages):
        text = chunk.content if isinstance(chunk.content, str) else ""
        ak = getattr(chunk, "additional_kwargs", {}) or {}
        reasoning = ak.get("reasoning_content", "") or ""
        if text or reasoning:
            yield {"reasoning": reasoning, "content": text}


# ─── messages 拼装辅助（Phase 0 修正：仅 warn 不 raise） ──────────


def append_assistant(
    messages: list[BaseMessage], response: AIMessage
) -> list[BaseMessage]:
    """把 assistant 消息加进 messages 列表。

    Phase 0 实测：Paratera 不强制 reasoning_content 回灌；langchain-openai 当前
    版本也不暴露该字段。因此本函数仅在上层手工构造的 dict 缺字段且带 tool_calls
    时 warnings.warn 提示思维连贯性建议，**不抛异常 / 不阻断流程**。
    """
    if isinstance(response, dict):
        has_tool = bool(response.get("tool_calls"))
        if has_tool and "reasoning_content" not in response:
            warnings.warn(
                "append_assistant: assistant 消息含 tool_calls 但缺 reasoning_content；"
                "Paratera 实测不强制，但保留有助思维连贯（本警告不阻断流程）。",
                stacklevel=2,
            )
    messages.append(response)
    return messages
