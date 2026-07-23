"""LLM Provider 封装：提供 Paratera (DeepSeek-V4-Pro) 与 Ollama 的统一接入。

支持单例模式以复用连接池，并集成进程内 InMemoryCache 以提升重复调用的响应速度。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from langchain_core.caches import InMemoryCache
from langchain_core.globals import set_llm_cache
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


# ─── 单例 + 进程内缓存基础设施 ─────────────────────────────────
#
# 设计：
# 1) _LLM_INSTANCES / _EMBEDDINGS_INSTANCES 缓存按 env 当前值构造的客户端实例。
#    key 故意带上 provider 字符串，使 LLM_PROVIDER 在运行期切换时不会复用旧 provider 实例
#    （但同一 provider 下 PARATERA_BASE_URL 等参数变化不会重建——这是常规约定，rare case）。
# 2) double-checked locking：减少热路径下的锁开销（Python GIL 下读字典本就线程安全，
#    但写入 + 状态变更需要锁防止重复构造）。
# 3) LLM cache 用 InMemoryCache（langchain_core）：
#    - 进程内 dict，进程退出即清；REPL 一次会话内同 prompt 复命中省 API 费
#    - 跨进程不复用，避免"改 prompt 后旧响应顽固缓存"问题
#    - 仅缓存 chat completion；Embeddings 单例本身已避免重建客户端，向量本地另有 Chroma 持久化

_LLM_INSTANCES: dict[tuple[str, bool], Any] = {}
_EMBEDDINGS_INSTANCES: dict[str, Any] = {}
_CACHE_INITIALIZED: bool = False
_LOCK = threading.Lock()


def _init_llm_cache_once() -> None:
    """首次调用时（按需）通过 langchain 全局 cache 钩子挂上 InMemoryCache。

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
        set_llm_cache(InMemoryCache())
        _CACHE_INITIALIZED = True


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

    默认 streaming=True，让 LangGraph stream_mode="messages" 能逐 token 透出
    AIMessageChunk；显式传 streaming=False 可用于一次性 invoke 场景（如 Intake 抽 JSON）。

    模块级单例 by (provider, streaming)；首次调用时挂载 InMemoryCache 全局钩子。
    """
    _init_llm_cache_once()
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

    模块级单例按 provider 复用实例；与 get_llm 共用 InMemoryCache 初始化路径。
    """
    _init_llm_cache_once()
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
