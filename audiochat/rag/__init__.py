"""
RAG (检索增强生成) 模块

提供会议记录检索功能，增强 LLM 生成质量。

核心组件：
  - MeetingMemoryStore: 扁平向量存储（原有）
  - AudioChatRetriever: 扁平检索器（原有）
  - HierarchicalMeetingStore: 分层记忆存储（GA 风格）
  - HierarchicalRetriever: 分层检索器（GA 风格）

GA 风格分层体系：
  L1 (Index层)  — 轻量导航索引，始终可见，存储知识类别存在指针
  L2 (Fact层)   — 验证过的事实知识（项目背景、决策、行动项）
  L3 (SOP层)    — 可复用流程知识（面试 SOP、周会 SOP 等）
  L4 (Raw层)    — 原始会议存档，用于溯源，非运行时注入
"""

from audiochat.rag.storage import MeetingMemoryStore, MeetingDocument
from audiochat.rag.retriever import AudioChatRetriever, RetrievedContext
from audiochat.rag.memory_hierarchy import (
    HierarchicalMeetingStore,
    L1IndexEntry,
    L2FactEntry,
    L3SOPEntry,
    L4RawEntry,
    WorkingMemoryAnchor,
    MetaMemory,
    MeetingType,
    classify_meeting_type,
)
from audiochat.rag.hierarchical_retriever import (
    HierarchicalRetriever,
    HierarchicalRetrievedContext,
)
from audiochat.rag.faithfulness import FaithfulnessChecker, FaithfulnessResult, Claim

__all__ = [
    # 原有
    "MeetingMemoryStore",
    "MeetingDocument",
    "AudioChatRetriever",
    "RetrievedContext",
    # 新增：分层记忆
    "HierarchicalMeetingStore",
    "L1IndexEntry",
    "L2FactEntry",
    "L3SOPEntry",
    "L4RawEntry",
    "WorkingMemoryAnchor",
    "MetaMemory",
    "MeetingType",
    "classify_meeting_type",
    # 新增：分层检索器
    "HierarchicalRetriever",
    "HierarchicalRetrievedContext",
    # 新增：RAGVUE 忠实度检查
    "FaithfulnessChecker",
    "FaithfulnessResult",
    "Claim",
]
