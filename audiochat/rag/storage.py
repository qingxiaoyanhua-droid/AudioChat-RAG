"""
会议记忆存储模块
使用 ChromaDB 做轻量级向量存储
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import chromadb
    from chromadb.config import Settings
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    raise ImportError(
        "请安装 RAG 依赖：pip install chromadb sentence-transformers"
    ) from e


@dataclass
class MeetingDocument:
    """会议文档片段"""
    content: str
    meeting_id: str
    speaker: Optional[str] = None
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)


class MeetingMemoryStore:
    """
    会议记忆存储
    
    功能:
    - 使用 ChromaDB 持久化存储
    - 使用 BGE 中文嵌入模型
    - 支持批量添加和语义检索
    """
    
    def __init__(self, persist_dir: str = "./rag_storage"):
        """
        初始化存储
        
        Args:
            persist_dir: 持久化存储目录
        """
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化 ChromaDB
        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir / "chroma")
        )
        
        # 初始化嵌入模型（中文优化）
        self.embedder = SentenceTransformer("bge-large-zh-v1.5")
        
        # 获取或创建集合
        self.collection = self.client.get_or_create_collection(
            name="meeting_memory",
            metadata={"hnsw:space": "cosine"}
        )
    
    def add_document(self, doc: MeetingDocument) -> str:
        """添加文档到存储"""
        # 生成唯一 ID
        doc_id = hashlib.md5(
            f"{doc.meeting_id}_{doc.timestamp}_{doc.content[:50]}".encode()
        ).hexdigest()
        
        # 生成嵌入
        embedding = self.embedder.encode(doc.content, convert_to_numpy=True)
        
        # 存储元数据
        metadata = {
            "meeting_id": doc.meeting_id,
            "speaker": doc.speaker or "unknown",
            "timestamp": doc.timestamp,
            "content_preview": doc.content[:100],
        }
        metadata.update(doc.metadata)
        
        # 添加到集合
        self.collection.upsert(
            ids=[doc_id],
            embeddings=[embedding.tolist()],
            metadatas=[metadata],
            documents=[doc.content]
        )
        return doc_id
    
    def add_batch(self, documents: list[MeetingDocument]) -> list[str]:
        """批量添加文档"""
        ids = []
        contents = []
        embeddings = []
        metadatas = []
        
        for doc in documents:
            doc_id = hashlib.md5(
                f"{doc.meeting_id}_{doc.timestamp}_{doc.content[:50]}".encode()
            ).hexdigest()
            embedding = self.embedder.encode(doc.content, convert_to_numpy=True)
            
            ids.append(doc_id)
            contents.append(doc.content)
            embeddings.append(embedding.tolist())
            metadatas.append({
                "meeting_id": doc.meeting_id,
                "speaker": doc.speaker or "unknown",
                "timestamp": doc.timestamp,
            })
        
        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=contents
        )
        return ids
    
    def search(
        self,
        query: str,
        k: int = 5,
        speaker_filter: Optional[str] = None,
        meeting_id_filter: Optional[str] = None,
        time_range: Optional[tuple[str, str]] = None,
    ) -> list[MeetingDocument]:
        """
        语义检索（支持 metadata 复合过滤）

        Args:
            query: 语义检索 query
            k: 返回数量
            speaker_filter: 按说话人过滤
            meeting_id_filter: 按会议 ID 精确过滤（用于"只查当前会议"场景）
            time_range: 按时间范围过滤，tuple(start_iso, end_iso)
                        用于"上周相关会议""近一个月"等时间敏感检索

        ChromaDB 的 where 过滤发生在向量检索之前（HNSW 搜索前就缩小候选集），
        因此传递的过滤条件越多，检索越快、噪音越少。
        """
        embedding = self.embedder.encode(query, convert_to_numpy=True)

        # 构建复合 where 过滤条件（ChromaDB 支持嵌套的字典表达式）
        where_filter: Optional[dict] = None
        conditions: list[dict] = []
        if speaker_filter:
            conditions.append({"speaker": speaker_filter})
        if meeting_id_filter:
            conditions.append({"meeting_id": meeting_id_filter})
        if time_range:
            start_iso, end_iso = time_range
            conditions.append({"timestamp": {"$gte": start_iso, "$lte": end_iso}})

        if conditions:
            if len(conditions) == 1:
                where_filter = conditions[0]
            else:
                where_filter = {"$and": conditions}

        results = self.collection.query(
            query_embeddings=[embedding.tolist()],
            n_results=k,
            where=where_filter,
            include=["documents", "metadatas", "distances"]
        )
        
        docs = []
        if results["documents"] and results["documents"][0]:
            for i, content in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                docs.append(MeetingDocument(
                    content=content,
                    meeting_id=meta.get("meeting_id", "unknown"),
                    speaker=meta.get("speaker"),
                    timestamp=meta.get("timestamp", ""),
                    metadata=meta
                ))
        return docs
    
    def get_stats(self) -> dict:
        """获取存储统计信息"""
        count = self.collection.count()
        return {
            "total_documents": count,
            "persist_dir": str(self.persist_dir),
        }
    
    def clear(self):
        """清空存储"""
        self.client.delete_collection("meeting_memory")
        self.collection = self.client.create_collection(
            name="meeting_memory",
            metadata={"hnsw:space": "cosine"}
        )
