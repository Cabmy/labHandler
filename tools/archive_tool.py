"""archive_tool - 任务归档（写入 task_archive）

触发：用户 /done 或 /done --clear 时由 Summarizer 后置调用。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def archive_task(
    task_title: str,
    task_type: str,
    summary: str,
    lessons: str = "",
    workspace_snapshot: str = "",
) -> dict:
    """归档完成的任务到 task_archive（SQLite + Chroma 双写）。

    Args:
        task_title: 任务标题（"实现二分查找"）
        task_type: 类型（coding/essay/lab_report）
        summary: SUMMARY.md 主体或精简摘要
        lessons: 教训/经验（Planner 后续 rag_search 主要用）
        workspace_snapshot: SUMMARY.md 全文

    Returns:
        {"row_id": int, "task_title": str}
    """
    from memory import archive_task as _archive

    rid = _archive(
        task_title=task_title,
        task_type=task_type,
        summary=summary,
        lessons=lessons,
        workspace_snapshot=workspace_snapshot,
    )
    return {"row_id": rid, "task_title": task_title}


ARCHIVE_TOOLS = [archive_task]
