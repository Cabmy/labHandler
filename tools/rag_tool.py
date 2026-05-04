"""rag_tool - task_archive 检索 + RAG hybrid 检索包装

提供两个工具：
- archive_search: 检索历史任务归档（task_archive 表 + Chroma 同 collection）
- rag_search: 通用 hybrid 检索（vector + bm25 + RRF）

Planner / Coder 节点首轮调 archive_search 召回历史经验拼 prompt。
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def archive_search(query: str, limit: int = 3) -> list[dict]:
    """检索历史任务归档（task_archive）。返回 [{id, task_title, task_type, summary, lessons, ...}]。

    Planner 接到新作业时用：召回相似历史作业的 lessons 拼 prompt。
    """
    from memory import search_archive

    return search_archive(query, limit=limit)


@tool
def rag_search(query: str, k: int = 5) -> list[dict]:
    """RAG 混合检索（BM25 + Vector + RRF）。返回 [{content, source, chunk_id}] 列表。

    用于检索通用知识库（PDF 实验指导、教材片段等）。
    """
    from rag import hybrid_retrieve

    docs = hybrid_retrieve(query, k=k)
    return [
        {
            "content": d.page_content,
            "source": d.metadata.get("source", ""),
            "chunk_id": d.metadata.get("chunk_id", ""),
        }
        for d in docs
    ]


RAG_TOOLS = [archive_search, rag_search]
