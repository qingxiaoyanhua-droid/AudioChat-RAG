"""
会议记录检索器（RAG）
支持精确到秒的定位
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

import chromadb
from sentence_transformers import SentenceTransformer


@dataclass
class MeetingRecord:
    """会议记录"""
    content: str
    speaker: str
    meeting_id: str
    timestamp: str
    start_ms: int
    end_ms: int
    audio_path: Optional[str] = None
    score: float = field(default=0.0, init=False)


class MeetingRetriever:
    """会议检索器"""
    
    def __init__(self, persist_dir: str = "./rag_storage"):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        
        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir / "chroma")
        )
        
        # 使用 ModelScope 下载 BGE 模型
        print("[RAG] 加载嵌入模型（使用 ModelScope）...")
        from modelscope import snapshot_download
        model_dir = snapshot_download(
            "AI-ModelScope/bge-large-zh-v1.5",
            cache_dir="~/.cache/modelscope"
        )
        self.embedder = SentenceTransformer(model_dir)
        print(f"[RAG] 模型加载成功：{model_dir}")
        
        self.collection = self.client.get_or_create_collection(
            name="meeting_memory",
            metadata={"hnsw:space": "cosine"}
        )
    
    def add_record(self, record: MeetingRecord) -> str:
        doc_id = hashlib.md5(
            f"{record.meeting_id}_{record.speaker}_{record.start_ms}".encode()
        ).hexdigest()
        
        embedding = self.embedder.encode(record.content, convert_to_numpy=True)
        
        metadata = {
            "meeting_id": record.meeting_id,
            "speaker": record.speaker,
            "timestamp": record.timestamp,
            "start_ms": record.start_ms,
            "end_ms": record.end_ms,
            "audio_path": record.audio_path or "",
            "content": record.content
        }
        
        self.collection.upsert(
            ids=[doc_id],
            embeddings=[embedding.tolist()],
            metadatas=[metadata],
            documents=[record.content]
        )
        
        return doc_id
    
    def add_batch(self, records: List[MeetingRecord]) -> List[str]:
        ids = []
        embeddings = []
        metadatas = []
        contents = []
        
        for record in records:
            doc_id = hashlib.md5(
                f"{record.meeting_id}_{record.speaker}_{record.start_ms}".encode()
            ).hexdigest()
            embedding = self.embedder.encode(record.content, convert_to_numpy=True)
            
            ids.append(doc_id)
            embeddings.append(embedding.tolist())
            metadatas.append({
                "meeting_id": record.meeting_id,
                "speaker": record.speaker,
                "timestamp": record.timestamp,
                "start_ms": record.start_ms,
                "end_ms": record.end_ms,
                "audio_path": record.audio_path or "",
                "content": record.content
            })
            contents.append(record.content)
        
        self.collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=contents
        )
        
        return ids
    
    def retrieve(
        self,
        query: str,
        k: int = 3,
        speaker_filter: Optional[str] = None,
        half_life_days: float = 7.0
    ) -> List[MeetingRecord]:
        query_embedding = self.embedder.encode(query, convert_to_numpy=True)
        
        where_filter = None
        if speaker_filter:
            where_filter = {"speaker": speaker_filter}
        
        results = self.collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=k * 2,
            where=where_filter,
            include=["documents", "metadatas", "distances"]
        )
        
        docs = []
        if results['documents'] and results['documents'][0]:
            for i, doc in enumerate(results['documents'][0]):
                meta = results['metadatas'][0][i]
                distance = results['distances'][0][i]
                
                similarity = 1 - distance
                
                if 'timestamp' in meta:
                    try:
                        doc_time = datetime.fromisoformat(meta['timestamp'])
                        days_diff = (datetime.now() - doc_time).days
                        decay = 0.5 ** (days_diff / half_life_days)
                    except Exception:
                        decay = 1.0
                else:
                    decay = 1.0
                
                final_score = similarity * decay
                
                record = MeetingRecord(
                    content=doc,
                    speaker=meta.get('speaker', 'unknown'),
                    meeting_id=meta.get('meeting_id', ''),
                    timestamp=meta.get('timestamp', ''),
                    start_ms=meta.get('start_ms', 0),
                    end_ms=meta.get('end_ms', 0),
                    audio_path=meta.get('audio_path')
                )
                record.score = final_score
                docs.append(record)
        
        docs.sort(key=lambda x: x.score, reverse=True)
        return docs[:k]
    
    def get_stats(self) -> Dict:
        count = self.collection.count()
        return {
            "total_documents": count,
            "persist_dir": str(self.persist_dir),
        }
