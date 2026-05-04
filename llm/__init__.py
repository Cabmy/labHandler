"""hwHandler LLM 模块入口。"""

from .provider import (
    DS_V4_PRO_KWARGS,
    append_assistant,
    chat,
    chat_with_tools,
    get_embeddings,
    get_llm,
    stream_chat,
)

__all__ = [
    "DS_V4_PRO_KWARGS",
    "append_assistant",
    "chat",
    "chat_with_tools",
    "get_embeddings",
    "get_llm",
    "stream_chat",
]
