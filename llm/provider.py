"""LLM Provider 封装：统一接入 Paratera（DeepSeek-V4-Flash-0731）与 Ollama。

支持单例模式以复用连接池。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

# 统一的思考模式参数封装（防止某个 agent 漏传该配置）
LLM_KWARGS: dict[str, Any] = {
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


def _stream_usage_enabled() -> bool:
    """是否要求上游在流式响应里回传 token 用量（stream_options.include_usage）。

    langchain-openai **只在 base_url 为空**（即官方端点）时自动打开 stream_usage；
    我们走 Paratera 自定义 base_url，所以默认是关的。而 streaming=True 会让
    `invoke()` 也走流式分支（BaseChatModel._should_stream 命中实例级 streaming=True），
    于是**全部** LLM 调用都拿不到 usage_metadata —— benchmark 的 tokens_exact
    因此恒为 False、token 数按 len/4 估算低估约一个数量级。

    逃生阀：个别 OpenAI 兼容网关不认 stream_options 参数，
    置 LLM_STREAM_USAGE=false 可关掉（代价是回到估算口径）。
    """
    return os.getenv("LLM_STREAM_USAGE", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ─── 单例基础设施 ─────────────────────────────────────────────
#
# 设计：
# 1) _LLM_INSTANCES / _EMBEDDINGS_INSTANCES 缓存以当前 env 值构建的客户端实例。
#    Key 有意包含 provider 字符串，使 LLM_PROVIDER 运行时切换不会复用旧 provider
#    实例（但同 provider 内 PARATERA_BASE_URL 等参数变更不会重建——标准
#    约定，罕见场景）。
# 2) 双重检查锁：减少热路径上的锁开销（Python GIL 使 dict 读取线程安全，
#    但写入 + 状态变更需加锁以防重复构建）。

_LLM_INSTANCES: dict[tuple[str, bool], Any] = {}
_EMBEDDINGS_INSTANCES: dict[str, Any] = {}
_LOCK = threading.Lock()


# ─── 实例工厂（内部构建 + 对外单例入口） ───────────


def _build_llm(provider: str, streaming: bool) -> Any:
    """实际构建 ChatModel 实例；调用方需自行做单例缓存。"""
    if provider == "ollama":
        return ChatOllama(
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:14b"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            num_ctx=2048,
        )
    if provider == "paratera":
        return ChatOpenAI(
            model=os.getenv("PARATERA_LLM_MODEL", "DeepSeek-V4-Flash-0731"),
            api_key=SecretStr(_require_paratera_key()),
            base_url=os.getenv("PARATERA_BASE_URL"),
            streaming=streaming,
            stream_usage=_stream_usage_enabled(),
            **LLM_KWARGS,
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
    """获取 LangChain Chat 模型实例（由 LLM_PROVIDER 选择）。

    Paratera 路径自动带上思考模式；Ollama 路径不传思考参数。

    默认 streaming=True 使 LangGraph stream_mode="messages" 能逐 token
    流式输出 AIMessageChunk；显式 streaming=False 可用于一次性 invoke 场景
    （如 Intake 提取 JSON）。

    按 (provider, streaming) 做模块级单例。
    """
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
    """Embeddings 不需思考模式参数。

    按 provider 做模块级单例复用。
    """
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
