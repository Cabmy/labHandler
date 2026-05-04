"""tools registry - 收集所有工具 → OpenAI function-calling schema 列表

设计要点：
1. 所有 tool 用 langchain_core.tools.@tool 装饰，schema 自动生成
2. registry 暴露：
   - ALL_TOOLS: 全部本地工具列表（不含沙箱 MCP tools，那些异步加载）
   - get_openai_tools(): 转 OpenAI function-calling schema 列表
   - get_tool_by_name(name): 工具实例查找
3. Sandbox MCP tools 走 sandbox_tools.get_sandbox_tools()（异步），
   Phase 4 Coder 节点拼到一起再 bind_tools；本 registry 只管本地 7 类
"""

from __future__ import annotations

from typing import Any

from langchain_core.utils.function_calling import convert_to_openai_tool

from .archive_tool import ARCHIVE_TOOLS
from .fs_tools import FS_TOOLS
from .profile_tool import PROFILE_TOOLS
from .rag_tool import RAG_TOOLS
from .search_tool import SEARCH_TOOLS
from .skill_tool import SKILL_TOOLS

# 沙箱 tools 异步加载，不入 ALL_TOOLS（Phase 4 Coder 自己异步拼）
ALL_TOOLS: list[Any] = (
    FS_TOOLS + SEARCH_TOOLS + RAG_TOOLS + ARCHIVE_TOOLS + SKILL_TOOLS + PROFILE_TOOLS
)

_TOOL_BY_NAME: dict[str, Any] = {t.name: t for t in ALL_TOOLS}


def get_openai_tools() -> list[dict]:
    """返回 OpenAI function-calling schema 列表（喂给 ChatOpenAI.bind_tools 或裸 SDK）"""
    return [convert_to_openai_tool(t) for t in ALL_TOOLS]


def get_tool_by_name(name: str) -> Any:
    """按 name 取 tool 实例（执行时 dispatch 用）"""
    if name not in _TOOL_BY_NAME:
        raise KeyError(f"未注册的 tool：{name}（已注册 {list(_TOOL_BY_NAME.keys())}）")
    return _TOOL_BY_NAME[name]


def list_tool_names() -> list[str]:
    return [t.name for t in ALL_TOOLS]
