"""ChromaDB 向量库管理。"""

from typing import Any
import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document

from config.runtime import get_settings


class VectorStore:
    """ChromaDB 向量库封装（collection 名由调用方指定，唯一消费者是 rag/archive_retriever.py）。"""

    def __init__(self, collection_name: str, persist_dir: str | None = None) -> None:
        self.collection_name: str = collection_name
        self.persist_dir: str = persist_dir or str(get_settings().chroma_persist_dir)
        self._vectorstore: Chroma | None = None

    @property
    def vectorstore(self) -> Chroma:
        """懒加载向量库（embeddings 延迟导入，避免模块初始化时机问题）。"""
        if self._vectorstore is None:
            from llm import get_embeddings
            self._vectorstore = Chroma(
                collection_name=self.collection_name,
                embedding_function=get_embeddings(),
                persist_directory=self.persist_dir,
            )
        return self._vectorstore
    
    def add_documents(self, documents: list[Document]) -> list[str]:
        """向向量库添加文档。"""
        if not documents:
            return []
        return self.vectorstore.add_documents(documents)
    
    def similarity_search(self, query: str, k: int = 4) -> list[Document]:
        """相似度检索。"""
        return self.vectorstore.similarity_search(query, k=k)
    
    def clear(self) -> None:
        """清空当前 collection。"""
        client = chromadb.PersistentClient(path=self.persist_dir)
        try:
            client.delete_collection(self.collection_name)
        except Exception:
            pass  # collection 不存在时忽略
        self._vectorstore = None
