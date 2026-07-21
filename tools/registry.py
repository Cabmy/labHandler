"""tools registry - 收集所有工具列表。

Coder 手选工具子集，不依赖 registry。该文件保留供参考。
"""

from __future__ import annotations

from typing import Any

from .fs_tools import FS_TOOLS
from .profile_tool import PROFILE_TOOLS
from .rag_tool import RAG_TOOLS
from .search_tool import SEARCH_TOOLS
from .skill_tool import SKILL_TOOLS

ALL_TOOLS: list[Any] = (
    FS_TOOLS + SEARCH_TOOLS + RAG_TOOLS + SKILL_TOOLS + PROFILE_TOOLS
)