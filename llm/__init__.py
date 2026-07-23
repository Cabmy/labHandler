"""labHandler LLM 模块入口。"""

from .provider import (
    DS_V4_PRO_KWARGS,
    get_embeddings,
    get_llm,
)

__all__ = [
    "DS_V4_PRO_KWARGS",
    "get_embeddings",
    "get_llm",
]
