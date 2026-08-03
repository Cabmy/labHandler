"""RRF 融合（Reciprocal Rank Fusion，倒数排名融合）。

公式：score(d) = Σ_i 1 / (k_const + rank_i(d))
k_const=60 是 Cormack 等人 2009 论文的经验值。

不用加权和：BM25 分数与余弦相似度量纲不同，
直接加权需先做归一化。RRF 只用排名信息规避该问题，
也无需调权重超参。
"""

from __future__ import annotations

RRF_K = 60


def rrf_fuse(
    rankings: list[list[str]],
    k_const: int = RRF_K,
) -> list[tuple[str, float]]:
    """倒数排名融合。

    Args:
        rankings: 多路召回的排序结果，每路是 [doc_id_rank1, doc_id_rank2, ...]
        k_const: RRF 常数

    Returns:
        按 RRF 分数降序的 [(doc_id, score)] 列表
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, 1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k_const + rank)
    return sorted(scores.items(), key=lambda x: -x[1])