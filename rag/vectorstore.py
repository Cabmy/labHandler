"""ChromaDB 向量存储管理"""

import os
from typing import Optional, Any
import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document

CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./.labhandler_data/chroma")


class VectorStore:
    """ChromaDB 向量存储封装（collection 名由调用方指定，唯一消费者在 rag/archive_retriever.py）"""

    def __init__(self, collection_name: str, persist_dir: Optional[str] = None) -> None:
        self.collection_name: str = collection_name
        self.persist_dir: str = persist_dir or CHROMA_PERSIST_DIR
        self._vectorstore: Optional[Chroma] = None

    @property
    def vectorstore(self) -> Chroma:
        """懒加载 vectorstore（embeddings 延迟 import，避免模块初始化时序问题）"""
        if self._vectorstore is None:
            from llm import get_embeddings
            self._vectorstore = Chroma(
                collection_name=self.collection_name,
                embedding_function=get_embeddings(),
                persist_directory=self.persist_dir,
            )
        return self._vectorstore
    
    def add_documents(self, documents: list[Document]) -> list[str]:
        """添加文档到向量库"""
        if not documents:
            return []
        return self.vectorstore.add_documents(documents)
    
    def similarity_search(self, query: str, k: int = 4) -> list[Document]:
        """相似度检索"""
        return self.vectorstore.similarity_search(query, k=k)
    
    def similarity_search_with_score(self, query: str, k: int = 4) -> list[tuple[Document, float]]:
        """带分数的相似度检索"""
        return self.vectorstore.similarity_search_with_score(query, k=k)
    
    def clear(self) -> None:
        """清空当前 collection"""
        client = chromadb.PersistentClient(path=self.persist_dir)
        try:
            client.delete_collection(self.collection_name)
        except Exception:
            pass  # Collection 不存在
        self._vectorstore = None
