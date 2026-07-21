"""归档知识卡片检索：Chroma + BM25 双路 + RRF 融合。

职责：
- index_cards：写入 Chroma 向量 + 更新 BM25 持久化索引
- search_cards：双路召回 + RRF 融合 + SQLite 回表 hydrate
- rebuild_archive_index：全量重建（SQLite 是唯一事实源）
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.documents import Document

from memory.archive import get_task_archive
from rag.bm25_store import BM25Store
from rag.hybrid import rrf_fuse
from rag.vectorstore import VectorStore

# Chroma collection 按 embedding 模型名隔离
EMBEDDING_MODEL = (
    os.getenv("PARATERA_EMBEDDING_MODEL", "GLM-Embedding-3")
    .lower()
    .replace("-", "_")
)
CHROMA_COLLECTION = f"archive_cards_{EMBEDDING_MODEL}"

# BM25 持久化单例（进程级，与 InMemoryCache 生命周期一致）
_bm25_store: BM25Store | None = None


def _get_bm25() -> BM25Store:
    global _bm25_store
    if _bm25_store is None:
        _bm25_store = BM25Store()
    return _bm25_store


def _get_vectorstore() -> VectorStore:
    return VectorStore(collection_name=CHROMA_COLLECTION)


def _card_id_str(doc: Document) -> str:
    return str(doc.metadata.get("card_id", ""))


# ─── 写入索引 -----------------------------------------------------------


def index_cards(card_ids: list[int]) -> dict[str, Any]:
    """写入 Chroma + 更新 BM25 持久化索引。

    Args:
        card_ids: 要索引的卡片 id 列表（由 memory.archive 写入后返回）

    Returns:
        {indexed: n, failed: n, errors: [...]}
    """
    if not card_ids:
        return {"indexed": 0, "failed": 0, "errors": []}

    archive = get_task_archive()
    cards = archive.get_cards_by_ids(card_ids)
    vs = _get_vectorstore()
    bm25 = _get_bm25()

    documents: list[Document] = []
    for card in cards:
        doc = Document(
            page_content=card["search_text"],
            metadata={
                "card_id": card["card_id"],
                "task_id": card["task_id"],
                "card_type": card["card_type"],
                "task_type": card["task_type"],
            },
        )
        documents.append(doc)

    # Chroma 写入
    indexed = 0
    failed = 0
    errors: list[str] = []
    try:
        vs.add_documents(documents)
        indexed = len(documents)
    except Exception as e:
        failed = len(documents)
        err_msg = f"{type(e).__name__}: {e}"
        errors.append(err_msg)
        for card in cards:
            archive.mark_card_vector_error(card["card_id"], err_msg)

    # BM25 增量更新（即使 Chroma 失败也更新 BM25，保证至少一路可用）
    bm25.add_documents(documents)

    return {"indexed": indexed, "failed": failed, "errors": errors}


# ─── 检索 ---------------------------------------------------------------


def search_cards(
    query: str,
    limit: int = 5,
    card_types: list[str] | None = None,
    task_type: str | None = None,
) -> dict[str, Any]:
    """Chroma + BM25 + RRF 融合检索知识卡片。

    Args:
        query: 检索查询
        limit: 返回 top-k 条
        card_types: 过滤卡片类型，如 ["lesson", "strategy"]
        task_type: 过滤任务类型，如 "coding"

    Returns:
        {items: [{card_id, task_id, card_type, content, task_title, task_type,
                  matched_by: [...], rrf_score}, ...],
         degraded: bool, vector_error: str | None}
    """
    vs = _get_vectorstore()
    over = max(limit * 2, 10)

    # 1) Chroma 语义召回 -> card_id ranking
    chroma_ranking: list[str] = []
    vector_error: str | None = None
    chroma_filter: dict[str, Any] | None = None
    if card_types:
        chroma_filter = {"card_type": {"$in": card_types}}
    if task_type:
        if chroma_filter is None:
            chroma_filter = {}
        chroma_filter["task_type"] = task_type

    try:
        kwargs: dict[str, Any] = {"query": query, "k": over}
        if chroma_filter is not None:
            kwargs["filter"] = chroma_filter
        hits = vs.vectorstore.similarity_search(**kwargs)
        chroma_ranking = [_card_id_str(d) for d in hits if d.metadata.get("card_id")]
    except Exception as e:
        vector_error = f"{type(e).__name__}: {e}"

    # 2) BM25 持久化索引召回 -> card_id ranking
    # BM25 不支持原数据过滤，先全量召回再做后置过滤
    bm25_ranking: list[str] = []
    try:
        bm25_hits = _get_bm25().search(query, k=over)
        for d, _ in bm25_hits:
            cid = d.metadata.get("card_id")
            if not cid:
                continue
            meta_type = d.metadata.get("card_type", "")
            meta_task = d.metadata.get("task_type", "")
            if card_types and meta_type not in card_types:
                continue
            if task_type and meta_task != task_type:
                continue
            bm25_ranking.append(str(cid))
    except Exception:
        pass

    if not chroma_ranking and not bm25_ranking:
        return {"items": [], "degraded": bool(vector_error), "vector_error": vector_error}

    # 3) RRF 融合 card_id
    fused = rrf_fuse([chroma_ranking, bm25_ranking])
    fused_ids: list[int] = []
    fused_scores: dict[int, float] = {}
    for cid_str, score in fused[:limit]:
        try:
            cid = int(cid_str)
            fused_ids.append(cid)
            fused_scores[cid] = round(score, 4)
        except ValueError:
            continue

    if not fused_ids:
        return {"items": [], "degraded": bool(vector_error), "vector_error": vector_error}

    # 4) 记录每张 card 被哪路命中
    set_vec = set(chroma_ranking)
    set_bm25 = set(bm25_ranking)

    # 5) 回表 hydrate
    archive = get_task_archive()
    rows = archive.get_cards_by_ids(fused_ids)

    items: list[dict[str, Any]] = []
    for row in rows:
        cid = row["card_id"]
        matched_by: list[str] = []
        if str(cid) in set_vec:
            matched_by.append("vector")
        if str(cid) in set_bm25:
            matched_by.append("bm25")
        items.append({
            "card_id": cid,
            "task_id": row["task_id"],
            "card_type": row["card_type"],
            "content": row["content"],
            "task_title": row["task_title"],
            "task_type": row["task_type"],
            "rrf_score": fused_scores.get(cid, 0),
            "matched_by": matched_by,
        })

    return {
        "items": items,
        "degraded": bool(vector_error),
        "vector_error": vector_error,
    }


# ─── 索引重建 -----------------------------------------------------------


def rebuild_archive_index() -> dict[str, Any]:
    """全量重建 Chroma + BM25 索引（以 SQLite 为事实源）。

    Returns:
        {total: n, indexed: n, failed: n, errors: [...]}
    """
    archive = get_task_archive()
    cards = archive.get_cards_for_indexing(limit=500)
    if not cards:
        return {"total": 0, "indexed": 0, "failed": 0, "errors": []}

    # 清空 BM25 重建
    bm25 = _get_bm25()
    bm25.clear()

    # 清空 Chroma collection
    vs = _get_vectorstore()
    try:
        vs.clear()
    except Exception:
        pass  # collection 不存在时忽略

    documents: list[Document] = []
    card_ids: list[int] = []
    for card in cards:
        # 重建前清除旧错误标记
        archive.clear_card_vector_error(card["id"])
        doc = Document(
            page_content=card["search_text"],
            metadata={
                "card_id": card["id"],
                "task_id": card["task_id"],
                "card_type": card["card_type"],
                "task_type": card["task_type"],
            },
        )
        documents.append(doc)
        card_ids.append(card["id"])

    # Chroma 全量写入
    indexed = 0
    failed = 0
    errors: list[str] = []
    try:
        vs.add_documents(documents)
        indexed = len(documents)
    except Exception as e:
        failed = len(documents)
        err_msg = f"{type(e).__name__}: {e}"
        errors.append(err_msg)
        for cid in card_ids:
            archive.mark_card_vector_error(cid, err_msg)

    # BM25 全量重建
    bm25.add_documents(documents)

    return {
        "total": len(documents),
        "indexed": indexed,
        "failed": failed,
        "errors": errors,
    }