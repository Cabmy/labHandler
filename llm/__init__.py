"""labHandler LLM 模块入口。"""

from .provider import (
    get_embeddings,
    get_llm,
)

__all__ = [
    "get_embeddings",
    "get_llm",
]
