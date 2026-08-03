"""BM25 关键词检索索引。

进程内内存索引（不持久化；由 rag.archive_retriever._get_bm25 从 SQLite 懒加载全量重建），
使用 jieba 中文分词。
注意：必须分词，否则 BM25 退化为字符级匹配，中文语义完全失效。

权衡（有意为之）：rank_bm25 不支持增量更新，每次 add_documents 后全量重建。
知识卡片数 <1k 时全量重建 <100ms；以重建的简单性换取增量索引的复杂性
（无脏数据 / 无索引漂移 / 无并发一致性问题）；
卡片数达到数万级时再考虑切换增量引擎。
"""

import jieba
import numpy as np
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi


class BM25Store:
    """BM25Okapi 封装，支持 langchain Document 直接入索引。"""

    def __init__(self) -> None:
        self.docs: list[Document] = []
        self._tokenized: list[list[str]] = []
        self._bm25: BM25Okapi | None = None

    def add_documents(self, docs: list[Document]) -> None:
        """入索引并全量重建。

        rank_bm25 无增量 API；卡片 <1k 时重建 <100ms，
        简单优先（见模块 docstring）。
        """
        if not docs:
            return
        for d in docs:
            self.docs.append(d)
            self._tokenized.append(list(jieba.cut(d.page_content)))
        # rank_bm25 不支持增量；每次 add 后全量重建
        self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, k: int = 6) -> list[tuple[Document, float]]:
        """返回 [(Document, bm25_score)] top-k，仅含 score > 0 的条目。"""
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


