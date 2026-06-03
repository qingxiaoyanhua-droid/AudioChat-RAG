"""
GA 风格分层记忆体系 - 四层记忆架构

论文: GenericAgent (arXiv:2604.17091)
改造自 GA 的 L1-L4 分层记忆 + meta-memory 设计，针对会议场景定制。

核心设计原则（来自 GA）：
  1. L1 必须有界（bounded）：随着 L2/L3 扩展，L1 只记录"知识类别的存在指针"
  2. LLM 自身充当压缩器和解码器：新知识蒸馏成极简摘要；检索时按需解码完整内容
  3. "No Execution, No Memory"：只有验证过的信息才写入 L2/L3
  4. 按需加载：无关内容不进入活跃上下文

四层设计（对应 GA）：
  L1 (Index层)  — 轻量导航索引，始终注入
                  存储：会议类型路由指针、说话人索引、关键词映射、硬约束

  L2 (Fact层)   — 验证过的事实知识
                  存储：项目背景、团队成员、技术栈、已确认决策
                  写入条件：通过执行验证（会议结束后、结构化输出确认）

  L3 (SOP层)    — 可复用流程知识
                  存储：面试 SOP、周会 SOP、评审 Checklist、action_items 生成模板
                  写入条件：同类会议成功完成 2+ 次，验证流程可复用

  L4 (Raw Archive) — 原始会话存档
                  存储：完整会议转录（原始 utterances）
                  作用：溯源、重建轨迹，非运行时注入

L1 注入策略（来自 GA 的 always-on 原则）：
  默认注入 L1 → L2/L3 按需检索（工具调用/路由触发）
  L4 仅在需要重建上下文时读取

上下文截断策略（来自 GA 的 Stage 1-4 机制）：
  Stage 1: 工具输出截断（utterance 按头尾截断）
  Stage 2: Tag级压缩（每 N 轮一次）
  Stage 3: 消息驱逐（FIFO，超 budget 时）
  Stage 4: Working Memory Anchor（每轮注入锚点）

Usage:
  from audiochat.rag.memory_hierarchy import HierarchicalMeetingStore, MeetingMemoryHierarchy
  store = HierarchicalMeetingStore(persist_dir="./rag_storage")
  hierarchy = MeetingMemoryHierarchy(store)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
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


# =============================================================================
# 数据结构
# =============================================================================

class MeetingType(Enum):
    """会议类型枚举（对应 L1 索引层的高频入口点）"""
    WEEKLY_REVIEW = "周会"         # 周报同步、进度汇报
    REQUIREMENT_REVIEW = "需求评审"  # PRD 评审、技术方案讨论
    INTERVIEW = "面试"              # 面试（行为面/技术面）
    STANDUP = "站会"               # 每日站会
    BRAINSTORM = "头脑风暴"          # 自由讨论
    RETROSPECTIVE = "复盘"          # 项目复盘、总结
    ONE_ON_ONE = "一对一"           # 1v1 沟通
    UNKNOWN = "未分类"


class MemoryLayer(Enum):
    """记忆层级"""
    L1_INDEX = "L1"    # 索引层（始终可见）
    L2_FACT = "L2"     # 事实层（按需加载）
    L3_SOP = "L3"      # SOP层（按需加载）
    L4_RAW = "L4"      # 原始存档层（非运行时注入）


@dataclass
class L1IndexEntry:
    """L1 索引条目 — 只记录知识类别的存在指针"""
    layer: str = "L1"

    # 会议类型路由（高频入口点）
    meeting_types: list[str] = field(default_factory=list)
    speakers: dict[str, int] = field(default_factory=dict)  # speaker -> occurrence_count

    # 项目关键词映射
    keywords: dict[str, float] = field(default_factory=dict)  # keyword -> importance_score

    # 硬约束（来自 GA：高频入口点 + 关键词映射 + 硬约束 = L1 三要素）
    hard_constraints: list[str] = field(default_factory=list)

    # 底层知识指针（L2/L3 存在性标记，不含实质内容）
    l2_pointer_count: int = 0
    l3_pointer_count: int = 0
    l4_pointer_count: int = 0

    # 元数据
    updated_at: str = ""
    meeting_id: str = ""

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "meeting_types": self.meeting_types,
            "speakers": self.speakers,
            "keywords": self.keywords,
            "hard_constraints": self.hard_constraints,
            "l2_pointer_count": self.l2_pointer_count,
            "l3_pointer_count": self.l3_pointer_count,
            "l4_pointer_count": self.l4_pointer_count,
            "updated_at": self.updated_at,
            "meeting_id": self.meeting_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> L1IndexEntry:
        return cls(
            layer=d.get("layer", "L1"),
            meeting_types=d.get("meeting_types", []),
            speakers=d.get("speakers", {}),
            keywords=d.get("keywords", {}),
            hard_constraints=d.get("hard_constraints", []),
            l2_pointer_count=d.get("l2_pointer_count", 0),
            l3_pointer_count=d.get("l3_pointer_count", 0),
            l4_pointer_count=d.get("l4_pointer_count", 0),
            updated_at=d.get("updated_at", ""),
            meeting_id=d.get("meeting_id", ""),
        )


@dataclass
class L2FactEntry:
    """L2 事实条目 — 验证过的稳定事实"""
    layer: str = "L2"
    content: str = ""
    category: str = ""  # project_bg / team / tech_stack / decision / action_item
    verified: bool = False  # No Execution, No Memory
    meeting_id: str = ""
    timestamp: str = ""
    # 关联的 L1 关键词（用于 L1→L2 路由）
    related_keywords: list[str] = field(default_factory=list)
    # 可信度（来自 GA：只有通过执行验证的信息才写入 L2）
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "content": self.content,
            "category": self.category,
            "verified": self.verified,
            "meeting_id": self.meeting_id,
            "timestamp": self.timestamp,
            "related_keywords": self.related_keywords,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> L2FactEntry:
        return cls(
            layer=d.get("layer", "L2"),
            content=d.get("content", ""),
            category=d.get("category", ""),
            verified=d.get("verified", False),
            meeting_id=d.get("meeting_id", ""),
            timestamp=d.get("timestamp", ""),
            related_keywords=d.get("related_keywords", []),
            confidence=d.get("confidence", 0.0),
        )


@dataclass
class L3SOPEntry:
    """L3 SOP 条目 — 可复用流程知识"""
    layer: str = "L3"
    content: str = ""
    sop_type: str = ""   # interview / weekly_meeting / review / standup / ...
    meeting_types: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    key_steps: list[str] = field(default_factory=list)
    failure_cases: list[str] = field(default_factory=list)  # 常见失败案例 + 调试策略
    # 来自 GA：常见失败案例和调试/恢复策略是 SOP 的核心组成
    usage_count: int = 0  # 使用次数（用于 SOP 进化：高频 SOP → 代码化）
    verified: bool = False
    meeting_id: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "content": self.content,
            "sop_type": self.sop_type,
            "meeting_types": self.meeting_types,
            "preconditions": self.preconditions,
            "key_steps": self.key_steps,
            "failure_cases": self.failure_cases,
            "usage_count": self.usage_count,
            "verified": self.verified,
            "meeting_id": self.meeting_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> L3SOPEntry:
        return cls(
            layer=d.get("layer", "L3"),
            content=d.get("content", ""),
            sop_type=d.get("sop_type", ""),
            meeting_types=d.get("meeting_types", []),
            preconditions=d.get("preconditions", []),
            key_steps=d.get("key_steps", []),
            failure_cases=d.get("failure_cases", []),
            usage_count=d.get("usage_count", 0),
            verified=d.get("verified", False),
            meeting_id=d.get("meeting_id", ""),
            timestamp=d.get("timestamp", ""),
        )


# =============================================================================
# L2 人物画像 — 从会议记录中聚合的团队成员画像
# =============================================================================

@dataclass
class L2PersonProfile:
    """L2 人物画像 — 描述团队成员的角色、任务与协作特征"""
    layer: str = "L2"

    # 基础信息
    name: str = ""
    role: str = ""                      # "后端工程师"
    department: str = ""                 # "基础架构"
    permissions: list[str] = field(default_factory=list)   # ["commit", "approve_pr"]

    # 任务相关
    active_tasks: list[str] = field(default_factory=list)  # 当前 issue_id
    completed_tasks_count: int = 0
    task_success_rate: float = 0.0

    # 语音/沟通特征（从会议记录统计）
    dominat_topics: list[str] = field(default_factory=list)   # 高频讨论话题
    speaking_ratio: float = 0.0        # 发言时长占比
    avg_response_time: float = 0.0     # 平均响应时长（秒）

    # 协作特征
    frequent_collaborators: list[str] = field(default_factory=list)
    escalation_tendency: float = 0.0   # 升频频率

    # 元数据
    meeting_ids: list[str] = field(default_factory=list)   # 参与过的会议
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "name": self.name,
            "role": self.role,
            "department": self.department,
            "permissions": self.permissions,
            "active_tasks": self.active_tasks,
            "completed_tasks_count": self.completed_tasks_count,
            "task_success_rate": self.task_success_rate,
            "dominat_topics": self.dominat_topics,
            "speaking_ratio": self.speaking_ratio,
            "avg_response_time": self.avg_response_time,
            "frequent_collaborators": self.frequent_collaborators,
            "escalation_tendency": self.escalation_tendency,
            "meeting_ids": self.meeting_ids,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> L2PersonProfile:
        return cls(
            layer=d.get("layer", "L2"),
            name=d.get("name", ""),
            role=d.get("role", ""),
            department=d.get("department", ""),
            permissions=d.get("permissions", []),
            active_tasks=d.get("active_tasks", []),
            completed_tasks_count=d.get("completed_tasks_count", 0),
            task_success_rate=d.get("task_success_rate", 0.0),
            dominat_topics=d.get("dominat_topics", []),
            speaking_ratio=d.get("speaking_ratio", 0.0),
            avg_response_time=d.get("avg_response_time", 0.0),
            frequent_collaborators=d.get("frequent_collaborators", []),
            escalation_tendency=d.get("escalation_tendency", 0.0),
            meeting_ids=d.get("meeting_ids", []),
            updated_at=d.get("updated_at", ""),
        )


# =============================================================================
# L3 需求演进轨迹 — 追踪需求从提出到上线的全生命周期
# =============================================================================

@dataclass
class L3RequirementEvolution:
    """L3 需求演进轨迹 — 追踪需求从提出到上线的全生命周期"""
    layer: str = "L3"

    # 需求标识
    requirement_id: str = ""
    title: str = ""

    # 演进时间线（phase -> timestamp）
    milestone_timestamps: dict[str, str] = field(default_factory=dict)
    # phases: proposed / approved / dev_started / dev_done / testing / launched

    # 版本历史（每次重大变更记录一次）
    version_history: list[dict] = field(default_factory=list)
    # {"version": "v1", "summary": "方案A", "meeting_id": "...", "timestamp": "..."}

    # 阻塞记录
    blockers: list[dict] = field(default_factory=list)
    # {"type": "排期冲突", "description": "...", "meeting_id": "...", "resolved": False}

    # 关联会议
    meeting_ids: list[str] = field(default_factory=list)

    # 当前阶段
    current_phase: str = "proposed"
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "requirement_id": self.requirement_id,
            "title": self.title,
            "milestone_timestamps": self.milestone_timestamps,
            "version_history": self.version_history,
            "blockers": self.blockers,
            "meeting_ids": self.meeting_ids,
            "current_phase": self.current_phase,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> L3RequirementEvolution:
        return cls(
            layer=d.get("layer", "L3"),
            requirement_id=d.get("requirement_id", ""),
            title=d.get("title", ""),
            milestone_timestamps=d.get("milestone_timestamps", {}),
            version_history=d.get("version_history", []),
            blockers=d.get("blockers", []),
            meeting_ids=d.get("meeting_ids", []),
            current_phase=d.get("current_phase", "proposed"),
            updated_at=d.get("updated_at", ""),
        )


@dataclass
class L4RawEntry:
    """L4 原始存档条目 — 完整会议记录"""
    layer: str = "L4"
    meeting_id: str = ""
    timestamp: str = ""
    meeting_types: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    # 原始 utterances（JSON 序列化存储）
    utterances_json: str = ""
    # 结构化摘要（由 LLM 生成，用于快速浏览）
    summary: str = ""
    action_items: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layer": self.layer,
            "meeting_id": self.meeting_id,
            "timestamp": self.timestamp,
            "meeting_types": self.meeting_types,
            "speakers": self.speakers,
            "utterances_json": self.utterances_json,
            "summary": self.summary,
            "action_items": self.action_items,
            "decisions": self.decisions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> L4RawEntry:
        return cls(
            layer=d.get("layer", "L4"),
            meeting_id=d.get("meeting_id", ""),
            timestamp=d.get("timestamp", ""),
            meeting_types=d.get("meeting_types", []),
            speakers=d.get("speakers", []),
            utterances_json=d.get("utterances_json", ""),
            summary=d.get("summary", ""),
            action_items=d.get("action_items", []),
            decisions=d.get("decisions", []),
        )


# =============================================================================
# 工具函数
# =============================================================================

# 会议类型识别关键词（用于 L1 路由）
MEETING_TYPE_KEYWORDS = {
    MeetingType.WEEKLY_REVIEW: ["周会", "周报", "周总结", "本周进展", "本周工作", "weekly", "周度"],
    MeetingType.REQUIREMENT_REVIEW: ["评审", "需求", "方案", "review", "prd", "设计", "技术方案"],
    MeetingType.INTERVIEW: ["面试", "候选人", "技术面", "hr面", "行为面", "面试官", "interview"],
    MeetingType.STANDUP: ["站会", "daily", "今日", "昨天", "今天", "standup", "stand-up"],
    MeetingType.BRAINSTORM: ["讨论", "头脑风暴", "想法", "brainstorm", "大家有什么"],
    MeetingType.RETROSPECTIVE: ["复盘", "总结", "反思", "retro", "做得好的", "改进点"],
    MeetingType.ONE_ON_ONE: ["1v1", "一对一", "单独聊", "私下", "one-on-one", "one on one"],
}


def classify_meeting_type(text: str) -> MeetingType:
    """从会议文本推断会议类型（L1 路由的第一步）"""
    text_lower = text.lower()
    scores: dict[MeetingType, int] = {}

    for mtype, keywords in MEETING_TYPE_KEYWORDS.items():
        if mtype == MeetingType.UNKNOWN:
            continue
        score = sum(1 for kw in keywords if kw.lower() in text_lower)
        scores[mtype] = score

    if not scores or max(scores.values()) == 0:
        return MeetingType.UNKNOWN

    return max(scores, key=scores.get)


def extract_keywords(text: str, top_k: int = 10) -> dict[str, float]:
    """从文本中提取关键词及其重要性得分"""
    # 简单实现：统计实词频率
    stopwords = {
        "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
        "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
        "这", "那", "什么", "怎么", "可以", "这个", "那个", "一下", "我们", "他们", "你们",
        "还", "能", "把", "但是", "因为", "所以", "如果", "虽然", "然后", "比如", "以及",
    }

    # 提取连续的中文词串和英文词
    words = re.findall(r'[\u4e00-\u9fff]{2,}|[\w]{3,}', text)
    freq: dict[str, int] = {}
    for w in words:
        w_clean = w.lower().strip()
        if w_clean not in stopwords and len(w_clean) >= 2:
            freq[w_clean] = freq.get(w_clean, 0) + 1

    if not freq:
        return {}

    max_freq = max(freq.values())
    # 归一化到 [0, 1]
    return {w: round(cnt / max_freq, 3) for w, cnt in sorted(freq.items(), key=lambda x: -x[1])[:top_k]}


# =============================================================================
# Meta-Memory 层（来自 GA：定义整体记忆地图、核心规则、更新边界）
# =============================================================================

@dataclass
class MetaMemory:
    """Meta-Memory — 全局元记忆层（对应 GA 的 meta-memory layer）

    定义整体记忆地图、核心规则、更新边界。
    在执行前加载，为 LLM 提供共享参考框架：
    - 记忆如何组织
    - 每层用途
    - 如何处理更新
    减少任意写入、历史误读、跨任务泄漏。
    """
    memory_map: dict[str, str] = field(default_factory=dict)
    core_rules: list[str] = field(default_factory=list)
    update_boundaries: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.memory_map:
            self.memory_map = {
                "L1": "轻量导航索引，始终可见，存储知识类别存在指针",
                "L2": "验证过的事实知识，通过执行验证才写入",
                "L3": "可复用 SOP/流程知识，同类任务成功 2+ 次后写入",
                "L4": "原始会议存档，用于溯源，非运行时注入",
            }
        if not self.core_rules:
            self.core_rules = [
                "No Execution, No Memory：未通过执行验证的信息不得写入 L2/L3",
                "L1 必须有界：新条目只在真正出现新类别时引入",
                "L1 只记录存在性：存储指针而非实质内容",
                "L4 存档永不自动升级：原始记录不直接转为 L2/L3",
                "按需加载：L2/L3 通过 L1 路由触发，不默认全量加载",
            ]
        if not self.update_boundaries:
            self.update_boundaries = {
                "L1": "任何时候可更新（索引层）",
                "L2": "会议结束且结构化输出确认后更新",
                "L3": "同类会议成功完成 2+ 次后更新",
                "L4": "仅追加，不修改（原始存档）",
            }

    def render(self) -> str:
        """将 Meta-Memory 渲染为可注入 prompt 的文本"""
        lines = [
            "【记忆体系说明】",
            "本系统的记忆分为四层：",
        ]
        for layer, desc in self.memory_map.items():
            lines.append(f"  {layer}: {desc}")
        lines.append("\n核心规则：")
        for rule in self.core_rules:
            lines.append(f"  - {rule}")
        lines.append("\n更新边界：")
        for layer, boundary in self.update_boundaries.items():
            lines.append(f"  {layer}: {boundary}")
        return "\n".join(lines)


# =============================================================================
# Working Memory Anchor（来自 GA Stage 4：每轮注入的锚点）
# =============================================================================

@dataclass
class WorkingMemoryAnchor:
    """Working Memory Anchor — 来自 GA Stage 4

    每轮工具调用后自动注入的锚点，包含：
    1. 最近 N 条单行摘要
    2. 当前轮次号
    3. key_info 块（当前会议状态）
    """
    turn_summaries: list[str] = field(default_factory=list)  # 每轮一行摘要
    current_turn: int = 0
    key_info: dict[str, str] = field(default_factory=dict)  # 当前会议关键状态

    # 上下文截断配置
    max_summaries: int = 20          # 保留最近 N 条摘要
    summary_max_chars: int = 100     # 每条摘要最大字符数

    def add_turn(self, summary: str):
        """追加一轮摘要"""
        # 截断到最大长度
        truncated = summary[:self.summary_max_chars]
        self.turn_summaries.append(truncated)
        if len(self.turn_summaries) > self.max_summaries:
            self.turn_summaries = self.turn_summaries[-self.max_summaries:]
        self.current_turn += 1

    def update_key_info(self, **kwargs):
        """更新关键信息"""
        self.key_info.update(kwargs)

    def render(self) -> str:
        """渲染为注入 prompt 的锚点文本"""
        lines = [
            f"【Working Memory Anchor】 Turn #{self.current_turn}",
        ]
        if self.turn_summaries:
            lines.append("最近轮次摘要：")
            for i, s in enumerate(self.turn_summaries[-5:], 1):  # 只显示最近 5 条
                lines.append(f"  [{i}] {s}")
        if self.key_info:
            lines.append("当前会议状态：")
            for k, v in self.key_info.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)


# =============================================================================
# 分层存储（改造 MeetingMemoryStore）
# =============================================================================

class HierarchicalMeetingStore:
    """
    分层会议记忆存储 — 改造自 MeetingMemoryStore

    在原有 ChromaDB 向量存储基础上，叠加 GA 风格的四层记忆体系：
    - ChromaDB collection: L2_FACT / L3_SOP（事实层 + SOP层，共享向量检索）
    - JSON 文件: L1_INDEX（索引层）+ L4_RAW（原始存档层）
    - L1 按需加载（不写入 ChromaDB）

    设计参考 GA Table 7：安装 20 个技能后，GA 的 prompt 长度仅 2,298 tokens，
    而 OpenClaw 为 43,321 tokens。分层存储是这一优势的核心来源。
    """

    def __init__(
        self,
        persist_dir: str = "./rag_storage",
        embedder_model: str = "bge-large-zh-v1.5",
    ):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # L1 索引存储（JSON 文件）
        self.l1_path = self.persist_dir / "l1_index.json"
        self.l1_entries: dict[str, L1IndexEntry] = {}
        self._load_l1()

        # L4 原始存档存储（JSONL 文件，每行一条记录）
        self.l4_path = self.persist_dir / "l4_raw.jsonl"

        # ChromaDB 存储 L2 + L3（共用一个 collection，用 metadata 区分层级）
        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir / "chroma_hierarchical")
        )
        self.embedder = SentenceTransformer(embedder_model)

        # L2 事实层
        self.l2_collection = self.client.get_or_create_collection(
            name="l2_facts",
            metadata={"hnsw:space": "cosine"},
        )

        # L3 SOP 层
        self.l3_collection = self.client.get_or_create_collection(
            name="l3_sops",
            metadata={"hnsw:space": "cosine"},
        )

        # 来自 GA：meta-memory 定义记忆地图
        self.meta_memory = MetaMemory()

    # =========================================================================
    # L1 索引层
    # =========================================================================

    def _load_l1(self):
        """从磁盘加载 L1 索引"""
        if self.l1_path.exists():
            try:
                with open(self.l1_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.l1_entries = {
                    k: L1IndexEntry.from_dict(v) for k, v in data.items()
                }
            except Exception:
                self.l1_entries = {}

    def _save_l1(self):
        """持久化 L1 索引到磁盘"""
        with open(self.l1_path, "w", encoding="utf-8") as f:
            json.dump(
                {k: v.to_dict() for k, v in self.l1_entries.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def update_l1_from_meeting(
        self,
        meeting_id: str,
        utterances_text: str,
        speakers: list[str],
        meeting_types: list[str],
    ) -> L1IndexEntry:
        """
        从会议内容更新 L1 索引

        来自 GA 的 L1 设计原则：
        - 只记录知识类别的存在，而非实质内容
        - 关键词归一化到 [0, 1] 重要性得分
        - 说话人统计 occurrence_count
        - L1 必须有界：新条目只在真正出现新类别时引入
        """
        keywords = extract_keywords(utterances_text, top_k=10)

        if meeting_id not in self.l1_entries:
            self.l1_entries[meeting_id] = L1IndexEntry(
                meeting_id=meeting_id,
                updated_at=datetime.now().isoformat(),
            )

        entry = self.l1_entries[meeting_id]

        # 更新说话人统计
        for spk in speakers:
            entry.speakers[spk] = entry.speakers.get(spk, 0) + 1

        # 更新会议类型
        for mt in meeting_types:
            if mt not in entry.meeting_types:
                entry.meeting_types.append(mt)

        # 合并关键词（取 max 分数）
        for kw, score in keywords.items():
            if kw not in entry.keywords or entry.keywords[kw] < score:
                entry.keywords[kw] = score

        # 限制关键词数量（防止 L1 膨胀）
        if len(entry.keywords) > 50:
            sorted_kws = sorted(entry.keywords.items(), key=lambda x: -x[1])
            entry.keywords = dict(sorted_kws[:50])

        entry.updated_at = datetime.now().isoformat()
        self._save_l1()
        return entry

    def get_l1_entry(self, meeting_id: str) -> Optional[L1IndexEntry]:
        """获取指定会议的 L1 索引条目"""
        return self.l1_entries.get(meeting_id)

    def get_all_l1_entries(self) -> list[L1IndexEntry]:
        """获取所有 L1 索引条目"""
        return list(self.l1_entries.values())

    def render_l1_for_context(self, meeting_id: Optional[str] = None) -> str:
        """
        渲染 L1 为始终注入的上下文（对应 GA 的 always-on memory）

        如果指定 meeting_id，只渲染该会议的 L1
        否则渲染全局 L1 索引
        """
        if meeting_id and meeting_id in self.l1_entries:
            entry = self.l1_entries[meeting_id]
            lines = [
                f"【L1 索引: {meeting_id}】",
                f"会议类型: {', '.join(entry.meeting_types) or '未分类'}",
                f"说话人: {', '.join(f'{k}({v}次)' for k, v in entry.speakers.items())}",
                f"关键词: {', '.join(f'{k}({v})' for k, v in list(entry.keywords.items())[:10])}",
                f"L2事实: {entry.l2_pointer_count}条, L3_SOP: {entry.l3_pointer_count}条, L4存档: {entry.l4_pointer_count}条",
            ]
            return "\n".join(lines)

        # 全局 L1 渲染（只显示存在性指针）
        if not self.l1_entries:
            return "【L1 索引】暂无记录"

        lines = ["【L1 全局索引】", f"共 {len(self.l1_entries)} 个会议记录"]
        for mid, entry in list(self.l1_entries.items())[:20]:  # 最多显示 20 个
            lines.append(
                f"  - {mid}: {', '.join(entry.meeting_types) or '未分类'} "
                f"(L2:{entry.l2_pointer_count} L3:{entry.l3_pointer_count} L4:{entry.l4_pointer_count})"
            )
        return "\n".join(lines)

    # =========================================================================
    # L2 事实层
    # =========================================================================

    def add_l2_fact(self, fact: L2FactEntry) -> str:
        """添加 L2 事实条目"""
        doc_id = hashlib.md5(
            f"L2_{fact.meeting_id}_{fact.category}_{fact.content[:50]}".encode()
        ).hexdigest()

        embedding = self.embedder.encode(fact.content, convert_to_numpy=True).tolist()

        self.l2_collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            metadatas=[{
                "layer": "L2",
                "category": fact.category,
                "meeting_id": fact.meeting_id,
                "timestamp": fact.timestamp,
                "verified": fact.verified,
                "confidence": fact.confidence,
                "related_keywords": ",".join(fact.related_keywords),
            }],
            documents=[fact.content],
        )

        # 更新 L1 指针计数
        if fact.meeting_id in self.l1_entries:
            self.l1_entries[fact.meeting_id].l2_pointer_count += 1
            self._save_l1()

        return doc_id

    def search_l2(
        self,
        query: str,
        k: int = 5,
        keywords_filter: Optional[list[str]] = None,
        verified_only: bool = True,
    ) -> list[L2FactEntry]:
        """检索 L2 事实层"""
        embedding = self.embedder.encode(query, convert_to_numpy=True).tolist()

        # 先按向量相似度检索
        results = self.l2_collection.query(
            query_embeddings=[embedding],
            n_results=k * 2,  # 多检索一些，过滤后达标
            include=["documents", "metadatas", "distances"],
        )

        facts = []
        if results["documents"] and results["documents"][0]:
            for i, content in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                if verified_only and not meta.get("verified", False):
                    continue
                if keywords_filter:
                    related = meta.get("related_keywords", "").split(",")
                    if not any(kw in related for kw in keywords_filter):
                        continue
                facts.append(L2FactEntry(
                    content=content,
                    category=meta.get("category", ""),
                    verified=meta.get("verified", False),
                    meeting_id=meta.get("meeting_id", ""),
                    timestamp=meta.get("timestamp", ""),
                    related_keywords=meta.get("related_keywords", "").split(","),
                    confidence=meta.get("confidence", 0.0),
                ))

        # 按 confidence 排序
        facts.sort(key=lambda f: -f.confidence)
        return facts[:k]

    # =========================================================================
    # L3 SOP 层
    # =========================================================================

    def add_l3_sop(self, sop: L3SOPEntry) -> str:
        """添加 L3 SOP 条目"""
        doc_id = hashlib.md5(
            f"L3_{sop.sop_type}_{sop.meeting_id}_{sop.content[:50]}".encode()
        ).hexdigest()

        # SOP 内容 = 类型 + 关键步骤 + 失败案例
        composite_content = (
            f"{sop.sop_type} SOP\n"
            f"前置条件: {', '.join(sop.preconditions)}\n"
            f"关键步骤: {' -> '.join(sop.key_steps)}\n"
            f"失败案例: {'; '.join(sop.failure_cases)}"
        )
        embedding = self.embedder.encode(composite_content, convert_to_numpy=True).tolist()

        self.l3_collection.upsert(
            ids=[doc_id],
            embeddings=[embedding],
            metadatas=[{
                "layer": "L3",
                "sop_type": sop.sop_type,
                "meeting_types": ",".join(sop.meeting_types),
                "meeting_id": sop.meeting_id,
                "timestamp": sop.timestamp,
                "usage_count": sop.usage_count,
                "verified": sop.verified,
            }],
            documents=[sop.content],
        )

        # 更新 L1 指针计数
        if sop.meeting_id in self.l1_entries:
            self.l1_entries[sop.meeting_id].l3_pointer_count += 1
            self._save_l1()

        return doc_id

    def search_l3(
        self,
        query: str,
        k: int = 3,
        sop_types_filter: Optional[list[str]] = None,
    ) -> list[L3SOPEntry]:
        """检索 L3 SOP 层"""
        embedding = self.embedder.encode(query, convert_to_numpy=True).tolist()

        results = self.l3_collection.query(
            query_embeddings=[embedding],
            n_results=k * 2,
            include=["documents", "metadatas", "distances"],
        )

        sops = []
        if results["documents"] and results["documents"][0]:
            for i, content in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                if sop_types_filter and meta.get("sop_type", "") not in sop_types_filter:
                    continue
                sops.append(L3SOPEntry(
                    content=content,
                    sop_type=meta.get("sop_type", ""),
                    meeting_types=meta.get("meeting_types", "").split(","),
                    usage_count=meta.get("usage_count", 0),
                    verified=meta.get("verified", False),
                    meeting_id=meta.get("meeting_id", ""),
                    timestamp=meta.get("timestamp", ""),
                ))

        # 优先返回高频使用的 SOP（GA 原则：usage_count 高的更可靠）
        sops.sort(key=lambda s: -(s.usage_count if s.usage_count else 0))
        return sops[:k]

    def increment_sop_usage(self, sop_id: str):
        """增加 SOP 使用计数（用于 SOP 进化追踪）"""
        try:
            result = self.l3_collection.get(ids=[sop_id])
            if result["metadatas"]:
                meta = result["metadatas"][0]
                meta["usage_count"] = meta.get("usage_count", 0) + 1
                self.l3_collection.upsert(
                    ids=[sop_id],
                    embeddings=result["embeddings"],
                    metadatas=[meta],
                    documents=result["documents"],
                )
        except Exception:
            pass

    # =========================================================================
    # L2 人物画像存储（JSON 文件）
    # =========================================================================

    def _load_l2_profiles(self) -> dict[str, L2PersonProfile]:
        path = self.persist_dir / "l2_profiles.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: L2PersonProfile.from_dict(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_l2_profiles(self, profiles: dict[str, L2PersonProfile]):
        path = self.persist_dir / "l2_profiles.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {k: v.to_dict() for k, v in profiles.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )

    @property
    def _l2_profiles(self) -> dict[str, L2PersonProfile]:
        if not hasattr(self, "_l2_profiles_cache"):
            self._l2_profiles_cache = self._load_l2_profiles()
        return self._l2_profiles_cache

    def upsert_l2_profile(self, profile: L2PersonProfile) -> str:
        """写入/更新 L2 人物画像"""
        profile.updated_at = datetime.now().isoformat()
        self._l2_profiles[profile.name] = profile
        self._save_l2_profiles(self._l2_profiles)
        return profile.name

    def get_l2_profile(self, name: str) -> Optional[L2PersonProfile]:
        """获取指定人物画像"""
        return self._l2_profiles.get(name)

    def get_all_l2_profiles(self) -> list[L2PersonProfile]:
        return list(self._l2_profiles.values())

    def build_profile_from_meetings(
        self,
        name: str,
        meetings: list[L4RawEntry],
    ) -> L2PersonProfile:
        """
        从多场会议记录聚合生成人物画像

        统计逻辑：
        - 发言占比：从各会议utterances中统计该speaker时长
        - 高频话题：从会议类型 + 关键词推断
        - 协作关系：从共同参会者中统计
        """
        if not meetings:
            return L2PersonProfile(name=name, updated_at=datetime.now().isoformat())

        topic_counts: dict[str, int] = {}
        collaborator_counts: dict[str, int] = {}
        total_speaking_chars = 0
        total_chars = 0

        for meeting in meetings:
            total_chars += len(meeting.summary)
            meeting_types_str = ",".join(meeting.meeting_types)
            for mt in meeting.meeting_types:
                topic_counts[mt] = topic_counts.get(mt, 0) + 1

            for spk in meeting.speakers:
                if spk != name:
                    collaborator_counts[spk] = collaborator_counts.get(spk, 0) + 1

            try:
                utterances = json.loads(meeting.utterances_json)
                for u in utterances:
                    if isinstance(u, dict):
                        if u.get("speaker") == name:
                            total_speaking_chars += len(u.get("text", ""))
                    elif hasattr(u, "speaker") and u.speaker == name:
                        total_speaking_chars += len(getattr(u, "text", ""))
            except Exception:
                pass

        dominat = sorted(topic_counts, key=topic_counts.get, reverse=True)[:3]
        collaborators = sorted(collaborator_counts, key=collaborator_counts.get, reverse=True)[:5]

        profile = L2PersonProfile(
            name=name,
            dominat_topics=dominat,
            speaking_ratio=round(total_speaking_chars / max(total_chars, 1), 3),
            frequent_collaborators=collaborators,
            meeting_ids=[m.meeting_id for m in meetings],
            updated_at=datetime.now().isoformat(),
        )

        self.upsert_l2_profile(profile)
        return profile

    # =========================================================================
    # L3 需求演进轨迹存储（JSON 文件）
    # =========================================================================

    def _load_l3_requirement_evolutions(self) -> dict[str, L3RequirementEvolution]:
        path = self.persist_dir / "l3_requirement_evolution.json"
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: L3RequirementEvolution.from_dict(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_l3_requirement_evolutions(self, evos: dict[str, L3RequirementEvolution]):
        path = self.persist_dir / "l3_requirement_evolution.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {k: v.to_dict() for k, v in evos.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )

    @property
    def _l3_requirement_evolutions(self) -> dict[str, L3RequirementEvolution]:
        if not hasattr(self, "_l3_requirement_evolution_cache"):
            self._l3_requirement_evolution_cache = self._load_l3_requirement_evolutions()
        return self._l3_requirement_evolution_cache

    def upsert_l3_requirement_evolution(self, evo: L3RequirementEvolution) -> str:
        """写入/更新需求演进轨迹"""
        evo.updated_at = datetime.now().isoformat()
        self._l3_requirement_evolutions[evo.requirement_id] = evo
        self._save_l3_requirement_evolutions(self._l3_requirement_evolutions)
        return evo.requirement_id

    def get_l3_requirement_evolution(self, requirement_id: str) -> Optional[L3RequirementEvolution]:
        """获取指定需求演进轨迹"""
        return self._l3_requirement_evolutions.get(requirement_id)

    def get_all_l3_requirement_evolutions(self) -> list[L3RequirementEvolution]:
        return list(self._l3_requirement_evolutions.values())

    def get_requirement_by_phase(self, phase: str) -> list[L3RequirementEvolution]:
        """按阶段筛选需求"""
        return [e for e in self._l3_requirement_evolutions.values() if e.current_phase == phase]

    def advance_requirement_phase(
        self,
        requirement_id: str,
        new_phase: str,
        meeting_id: str = "",
    ) -> Optional[L3RequirementEvolution]:
        """推进需求到新阶段（自动记录里程碑时间戳）"""
        evo = self._l3_requirement_evolutions.get(requirement_id)
        if not evo:
            return None

        evo.current_phase = new_phase
        evo.milestone_timestamps[new_phase] = datetime.now().isoformat()
        if meeting_id and meeting_id not in evo.meeting_ids:
            evo.meeting_ids.append(meeting_id)

        self.upsert_l3_requirement_evolution(evo)
        return evo

    def add_requirement_blocker(
        self,
        requirement_id: str,
        blocker_type: str,
        description: str,
        meeting_id: str = "",
    ) -> Optional[L3RequirementEvolution]:
        """为需求添加阻塞记录"""
        evo = self._l3_requirement_evolutions.get(requirement_id)
        if not evo:
            return None

        evo.blockers.append({
            "type": blocker_type,
            "description": description,
            "meeting_id": meeting_id,
            "resolved": False,
            "added_at": datetime.now().isoformat(),
        })
        if meeting_id and meeting_id not in evo.meeting_ids:
            evo.meeting_ids.append(meeting_id)

        self.upsert_l3_requirement_evolution(evo)
        return evo

    def resolve_requirement_blocker(
        self,
        requirement_id: str,
        blocker_index: int,
    ) -> Optional[L3RequirementEvolution]:
        """标记阻塞为已解决"""
        evo = self._l3_requirement_evolutions.get(requirement_id)
        if not evo or blocker_index >= len(evo.blockers):
            return None

        evo.blockers[blocker_index]["resolved"] = True
        evo.blockers[blocker_index]["resolved_at"] = datetime.now().isoformat()
        self.upsert_l3_requirement_evolution(evo)
        return evo

    # =========================================================================
    # L4 原始存档层
    # =========================================================================

    def add_l4_raw(self, entry: L4RawEntry) -> str:
        """追加 L4 原始存档（只追加，不修改）"""
        doc_id = hashlib.md5(
            f"L4_{entry.meeting_id}_{entry.timestamp}".encode()
        ).hexdigest()

        with open(self.l4_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

        # 更新 L1 指针计数
        if entry.meeting_id in self.l1_entries:
            self.l1_entries[entry.meeting_id].l4_pointer_count += 1
            self._save_l1()

        return doc_id

    def get_l4_entries(self, meeting_id: Optional[str] = None, limit: int = 10) -> list[L4RawEntry]:
        """读取 L4 原始存档（用于溯源，不默认注入上下文）"""
        entries = []
        if not self.l4_path.exists():
            return entries

        try:
            with open(self.l4_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if meeting_id and obj.get("meeting_id") != meeting_id:
                        continue
                    entries.append(L4RawEntry.from_dict(obj))
                    if len(entries) >= limit:
                        break
        except Exception:
            pass

        return entries

    # =========================================================================
    # 统计 & 清理
    # =========================================================================

    def get_stats(self) -> dict:
        """获取分层存储统计信息"""
        return {
            "l1_entries": len(self.l1_entries),
            "l2_facts": self.l2_collection.count(),
            "l2_person_profiles": len(self._l2_profiles),
            "l3_sops": self.l3_collection.count(),
            "l3_requirement_evolutions": len(self._l3_requirement_evolutions),
            "l4_raw_lines": 0,
            "persist_dir": str(self.persist_dir),
        }

    def clear(self):
        """清空所有层"""
        self.l1_entries = {}
        self._save_l1()
        self.client.delete_collection("l2_facts")
        self.client.delete_collection("l3_sops")
        if self.l4_path.exists():
            self.l4_path.unlink()
        # 重建
        self.l2_collection = self.client.get_or_create_collection(
            name="l2_facts", metadata={"hnsw:space": "cosine"}
        )
        self.l3_collection = self.client.get_or_create_collection(
            name="l3_sops", metadata={"hnsw:space": "cosine"}
        )
