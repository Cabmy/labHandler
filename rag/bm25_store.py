"""BM25 关键词检索索引

进程内存索引（无持久化；由 rag.archive_retriever._get_bm25 惰性从 SQLite 全量重建），
使用 jieba 中文分词。注意：必须分词，否则 BM25 会退化成字符级匹配，中文语义完全失效。

Trade-off（刻意为之）：rank_bm25 不支持增量更新，每次 add_documents 后全量重建。
知识卡片规模 <1k 张时全量重建 <100ms，用重建的简单性换掉增量索引的复杂度
（无脏数据/无索引漂移/无并发一致性问题）；卡片量级若上万再考虑换支持增量的引擎。
"""

import jieba
import numpy as np
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi


class BM25Store:
    """BM25Okapi 封装，支持 langchain Document 直接入库"""

    def __init__(self) -> None:
        self.docs: list[Document] = []
        self._tokenized: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    def add_documents(self, docs: list[Document]) -> None:
        """入库并全量重建索引。

        rank_bm25 无增量 API；卡片 <1k 时重建 <100ms，简单性优先（见模块 docstring）。
        """
        if not docs:
            return
        for d in docs:
            self.docs.append(d)
            self._tokenized.append(list(jieba.cut(d.page_content)))
        # rank_bm25 不支持增量，每次 add 后全量重建索引
        self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, k: int = 6) -> list[tuple[Document, float]]:
        """返回 [(Document, bm25_score)] top-k，score > 0 的才返回"""
        if self._bm25 is None or not self.docs:
            return []
        q_tokens = list(jieba.cut(query))
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        top_idx = np.argsort(scores)[-k:][::-1]
        return [
            (self.docs[int(i)], float(scores[int(i)]))
            for i in top_idx
            if scores[int(i)] > 0
        ]


