"""rag_tool - 跨任务经验知识卡片检索。

只暴露 archive_search（检索从历史任务归档蒸馏出的知识卡片）；
通用文档检索（rag_search）已移除。
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
    """Retrieve cross-task experience cards distilled from historical task archives.

    Returned cards fall into three types: lesson, strategy, pattern.
    Intended for the Planner to call upon receiving a new assignment: recall
    similar historical assignment experience to aid planning.

    Args:
        query: search query (task title, constraints, requirement description, etc.)
        limit: maximum number of cards to return (default 5)
        card_types: filter by card type, e.g. ["lesson", "strategy"]
        task_type: filter by task type, e.g. "coding"

    Returns:
        A list of knowledge cards, each containing card_id / card_type / content / task_title / rrf_score
    """
    result = search_cards(
        query=query,
        limit=limit,
        card_types=card_types,
        task_type=task_type,
    )
    return result.get("items", [])