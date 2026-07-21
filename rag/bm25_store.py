"""BM25 关键词检索索引

内存索引（每次 research 开始时 clear 重建），使用 jieba 中文分词。
注意：必须分词，否则 BM25 会退化成字符级匹配，中文语义完全失效。
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

    def clear(self) -> None:
        self.docs = []
        self._tokenized = []
        self._bm25 = None


