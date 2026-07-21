"""rag_tool - 跨任务经验知识卡片检索。

只暴露 archive_search（检索历史任务归档中沉淀的知识卡片），
不暴露通用文档检索（rag_search 已移除）。
"""

from __future__ import annotations

from langchain_core.tools import tool

from rag.archive_retriever import search_cards


@tool
def archive_search(
    query: str,
    limit: int = 5,
    card_types: list[str] | None = None,
    task_type: str | None = None,
) -> list[dict]:
    """检索历史任务归档中沉淀的跨任务经验卡片。

    返回的卡片包含 lesson（教训）、strategy（策略）、pattern（模式）三种类型。
    Planner 接到新作业时用：召回相似历史作业的经验辅助规划。

    Args:
        query: 检索查询（任务标题、约束、需求描述等）
        limit: 返回卡片上限（默认 5）
        card_types: 过滤卡片类型，如 ["lesson", "strategy"]
        task_type: 过滤任务类型，如 "coding"

    Returns:
        知识卡片列表，每张卡片含 card_id / card_type / content / task_title / rrf_score
    """
    result = search_cards(
        query=query,
        limit=limit,
        card_types=card_types,
        task_type=task_type,
    )
    return result.get("items", [])


RAG_TOOLS = [archive_search]