"""混合检索 - BM25 + Vector 双路召回 + RRF 融合

面试要点：
1. RRF 公式：score(d) = Σ_i 1 / (k_const + rank_i(d))
2. k_const=60 是 Cormack et al. 2009 论文经验值
3. 为什么不做加权求和：BM25 分数和余弦相似度尺度不齐，直接加权需要先归一化；
   RRF 只用排名信息规避这个问题，也不需要调节权重超参
4. BM25 强在精确关键词（专有名词、专业术语），Vector 强在语义泛化，两者互补
"""

import os

from langchain_core.documents import Document

from rag.bm25_store import get_bm25_store
from rag.vectorstore import get_vectorstore

HYBRID_TOP_K = int(os.getenv("HYBRID_TOP_K", "8"))
RRF_K = int(os.getenv("RRF_K", "60"))
BM25_TOP_K = int(os.getenv("BM25_TOP_K", "10"))
VECTOR_TOP_K = int(os.getenv("VECTOR_TOP_K", "10"))
RETRIEVAL_MODE = os.getenv("RETRIEVAL_MODE", "hybrid")


def rrf_fuse(
    rankings: list[list[str]],
    k_const: int = RRF_K,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion

    Args:
        rankings: 多路召回的排序结果，每路是 [doc_id_rank1, doc_id_rank2, ...]
        k_const: RRF 常数，经验值 60

    Returns:
        按 RRF 分数降序的 [(doc_id, score)] 列表
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k_const + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def _chunk_id(doc: Document) -> str:
    """从 Document metadata 取归并用的 chunk_id"""
    cid = doc.metadata.get("chunk_id")
    if cid:
        return str(cid)
    # fallback（Step 1 之前的老数据可能没有 chunk_id）
    return f"{doc.metadata.get('source', '')}#{doc.metadata.get('chunk_index', 0)}"


def hybrid_retrieve(query: str, k: int = HYBRID_TOP_K) -> list[Document]:
    """BM25 + Vector 双路召回 + RRF 融合

    Args:
        query: 检索查询
        k: 最终返回 top-k 个 Document

    Returns:
        融合后 top-k 的 Document 列表（按 RRF 分数降序）
    """
    # A/B 评估用：vector_only 模式直接走单路向量召回作为 baseline
    if RETRIEVAL_MODE == "vector_only":
        try:
            return get_vectorstore().similarity_search(query, k=k)
        except Exception:
            return []

    vectorstore = get_vectorstore()
    bm25_store = get_bm25_store()

    # 向量召回
    try:
        vec_hits = vectorstore.similarity_search(query, k=VECTOR_TOP_K)
    except Exception:
        vec_hits = []

    # BM25 召回
    bm25_hits = bm25_store.search(query, k=BM25_TOP_K)

    # 以 chunk_id 归并
    vec_ranking = [_chunk_id(d) for d in vec_hits]
    bm25_ranking = [_chunk_id(d) for d, _ in bm25_hits]

    fused = rrf_fuse([vec_ranking, bm25_ranking], k_const=RRF_K)

    # 恢复 Document 对象（同一个 chunk_id 可能来自两路，任选一个即可）
    doc_map: dict[str, Document] = {}
    for d in vec_hits:
        doc_map[_chunk_id(d)] = d
    for d, _ in bm25_hits:
        doc_map.setdefault(_chunk_id(d), d)

    return [doc_map[cid] for cid, _ in fused[:k] if cid in doc_map]
