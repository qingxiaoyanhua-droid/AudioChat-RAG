"""
GA 风格分层检索器 — HierarchicalRetriever

改造自 AudioChatRetriever，将 GA 的四层记忆路由集成进来。

检索流程（来自 GA 的 on-demand 原则）：
  1. L1 路由：快速识别会议类型 + 相关 L2/L3 指针（轻量，无向量计算）
  2. L2 检索：按 L1 关键词过滤，检索事实层（L2_FACT ChromaDB + L2_PersonProfile 人物画像）
  3. L3 检索：按 L1 会议类型过滤，检索 SOP 层 + 需求演进轨迹
  4. 融合打分：final_score = rerank_score × sqrt(decay) × layer_boost

三层知识体系：
  L1 (Index)     — 索引层，始终注入，存储知识类别指针
  L2 (Fact层)    — 两条线：事实条目（ChromaDB）+ 人物画像（JSON）
  L3 (SOP层)     — 两条线：SOP 流程（ChromaDB）+ 需求演进轨迹（JSON）
  L4 (Raw)       — 原始存档层，用于溯源，非运行时注入

关键设计：
  - 不破坏原有 AudioChatRetriever 接口
  - 通过 use_hierarchical 参数开关
  - L1 路由结果可用于快速决定是否需要 RAG（来自 GA 的阈值过滤思想）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from audiochat.rag.memory_hierarchy import (
    HierarchicalMeetingStore,
    L1IndexEntry,
    L2FactEntry,
    L3SOPEntry,
    L2PersonProfile,
    L3RequirementEvolution,
    MeetingType,
    WorkingMemoryAnchor,
    MetaMemory,
    classify_meeting_type,
    extract_keywords,
)
from audiochat.rag.storage import MeetingDocument


@dataclass
class HierarchicalRetrievedContext:
    """分层检索结果"""
    content: str
    layer: str          # "L1" / "L2" / "L3"
    relevance_score: float
    source: str         # meeting_id
    speaker: Optional[str] = None

    # L2/L3 特有字段
    category: str = ""          # L2: fact category
    sop_type: str = ""         # L3: SOP type
    usage_count: int = 0       # L3: 使用次数
    verified: bool = False      # L2/L3: 是否验证过

    # 原始分数
    dense_score: float = 0.0
    rerank_score: float = 0.0
    time_decay: float = 1.0
    layer_boost: float = 1.0   # 层级权重


# 层权重配置（可调）
LAYER_BOOST = {
    "L1": 0.5,    # L1 只提供路由信号，不直接参与排序
    "L2": 1.2,    # L2 事实层，高可信度优先
    "L3": 1.5,    # L3 SOP 层，可复用流程知识最优先
}


class HierarchicalRetriever:
    """
    分层会议检索器 — GA 风格

    通过 use_hierarchical 参数控制是否启用分层模式：
      - False: 使用原有的 AudioChatRetriever（单层扁平检索）
      - True:  启用分层路由（L1→L2/L3 分层检索 + 层权重融合）

    分层模式的核心价值：
      1. L1 路由预判：判断是否需要 RAG（节省不必要的向量计算）
      2. 层级感知：SOP 知识（L3）权重高于一般事实（L2）
      3. 按需加载：无关内容不进入上下文（来自 GA 的 Conciseness 原则）
    """

    def __init__(
        self,
        hierarchical_store: Optional[HierarchicalMeetingStore] = None,
        base_retriever=None,  # 原有 AudioChatRetriever 作为 fallback
        use_reranker: bool = True,
        reranker_model: str = "BAAI/bge-reranker-large",
        min_relevance_score: float = 0.35,
        cache_size: int = 128,
    ):
        self.store = hierarchical_store or HierarchicalMeetingStore()
        self.base_retriever = base_retriever
        self.use_reranker = use_reranker
        self.min_relevance_score = min_relevance_score
        self.cache_size = cache_size

        self._reranker = None
        self._reranker_model = reranker_model

        # Working Memory Anchor（对应 GA Stage 4）
        self.working_memory = WorkingMemoryAnchor()

        # Meta-Memory（对应 GA 的 meta-memory layer）
        self.meta_memory = MetaMemory()

        # LRU 缓存
        self._query_cache: dict[str, list[HierarchicalRetrievedContext]] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    # =========================================================================
    # L1 路由层（轻量，无向量计算）
    # =========================================================================

    def _l1_route(self, query: str) -> dict:
        """
        L1 路由 — 来自 GA 的 always-on memory 原则

        快速识别：
        1. 会议类型
        2. 相关关键词
        3. L1 中的相关会议条目
        4. 对应的 L2/L3 指针数量

        这步极轻量，用于：
        - 判断 query 是否需要 RAG
        - 确定 L2/L3 的过滤关键词
        - 提供层权重提示
        """
        meeting_type = classify_meeting_type(query)
        keywords = extract_keywords(query, top_k=5)

        # 从 L1 中找最相关的条目
        relevant_l1_entries: list[tuple[L1IndexEntry, float]] = []
        all_l1 = self.store.get_all_l1_entries()

        for entry in all_l1:
            score = 0.0
            # 会议类型匹配
            if meeting_type.value in entry.meeting_types:
                score += 2.0
            # 关键词匹配
            entry_kws = set(entry.keywords.keys())
            query_kws = set(keywords.keys())
            overlap = entry_kws & query_kws
            score += len(overlap) * 0.5
            if score > 0:
                relevant_l1_entries.append((entry, score))

        # 按 L2/L3 指针数量排序（GA：指针多的条目知识更丰富）
        relevant_l1_entries.sort(key=lambda x: -(x[0].l2_pointer_count + x[0].l3_pointer_count))
        top_l1 = [e for e, s in relevant_l1_entries[:5]]

        # 判断是否需要 RAG（来自 GA 的阈值过滤）
        need_rag = (
            len(relevant_l1_entries) > 0
            or meeting_type != MeetingType.UNKNOWN
            or len(keywords) > 0
        )

        return {
            "meeting_type": meeting_type,
            "keywords": keywords,
            "relevant_l1_entries": top_l1,
            "need_rag": need_rag,
            "l1_match_count": len(relevant_l1_entries),
        }

    # =========================================================================
    # L2 / L3 检索层
    # =========================================================================

    def _search_l2(self, query: str, l1_keywords: list[str], k: int = 3) -> list[L2FactEntry]:
        """检索 L2 事实层"""
        facts = self.store.search_l2(
            query=query,
            k=k,
            keywords_filter=l1_keywords,
            verified_only=True,
        )
        return facts

    def _search_l3(self, query: str, meeting_types: list[str], k: int = 2) -> list[L3SOPEntry]:
        """检索 L3 SOP 层"""
        sop_types = [mt.value for mt in meeting_types if mt != MeetingType.UNKNOWN]
        sops = self.store.search_l3(
            query=query,
            k=k,
            sop_types_filter=sop_types if sop_types else None,
        )
        return sops

    # =========================================================================
    # L2 人物画像检索
    # =========================================================================

    def search_person_profile(
        self,
        query: str,
    ) -> list[L2PersonProfile]:
        """
        按角色/部门/话题关键词检索人物画像

        用法：
          - "后端工程师" -> 返回所有 role=后端工程师的 profile
          - "API 相关" -> 返回 dominat_topics 含 API 的 profile
        """
        profiles = self.store.get_all_l2_profiles()
        if not query:
            return profiles

        query_lower = query.lower()
        scored: list[tuple[L2PersonProfile, float]] = []

        for p in profiles:
            score = 0.0
            if query_lower in p.role.lower():
                score += 3.0
            if query_lower in p.department.lower():
                score += 2.0
            if any(query_lower in t.lower() for t in p.dominat_topics):
                score += 1.5
            if any(query_lower in t.lower() for t in p.active_tasks):
                score += 1.0
            if score > 0:
                scored.append((p, score))

        scored.sort(key=lambda x: -x[1])
        return [p for p, _ in scored]

    def get_person_profile(self, name: str) -> Optional[L2PersonProfile]:
        """按姓名获取人物画像"""
        return self.store.get_l2_profile(name)

    def build_profile_for_person(
        self,
        name: str,
        meeting_ids: Optional[list[str]] = None,
    ) -> Optional[L2PersonProfile]:
        """为指定人物聚合生成画像"""
        if meeting_ids:
            meetings = []
            for mid in meeting_ids:
                entries = self.store.get_l4_entries(meeting_id=mid, limit=100)
                meetings.extend(entries)
        else:
            meetings = self.store.get_l4_entries(limit=1000)

        filtered = [m for m in meetings if name in m.speakers]
        return self.store.build_profile_from_meetings(name, filtered)

    # =========================================================================
    # L3 需求演进轨迹检索
    # =========================================================================

    def search_requirement_evolution(
        self,
        query: str,
        phase: Optional[str] = None,
    ) -> list[L3RequirementEvolution]:
        """
        检索需求演进轨迹

        - query: 按 requirement_id 或 title 模糊匹配
        - phase: 可选，按当前阶段筛选（proposed/approved/dev_started/dev_done/testing/launched）
        """
        if phase:
            candidates = self.store.get_requirement_by_phase(phase)
        else:
            candidates = self.store.get_all_l3_requirement_evolutions()

        if not query:
            return candidates

        query_lower = query.lower()
        scored: list[tuple[L3RequirementEvolution, float]] = []

        for evo in candidates:
            score = 0.0
            if query_lower in evo.requirement_id.lower():
                score += 3.0
            if query_lower in evo.title.lower():
                score += 2.0
            if any(query_lower in m.lower() for m in evo.meeting_ids):
                score += 1.5
            if score > 0:
                scored.append((evo, score))

        scored.sort(key=lambda x: -x[1])
        return [e for e, _ in scored]

    def get_requirement_evolution(self, requirement_id: str) -> Optional[L3RequirementEvolution]:
        """获取指定需求演进轨迹"""
        return self.store.get_l3_requirement_evolution(requirement_id)

    def create_requirement_evolution(
        self,
        requirement_id: str,
        title: str,
        meeting_id: str = "",
    ) -> L3RequirementEvolution:
        """创建新的需求演进轨迹"""
        evo = L3RequirementEvolution(
            requirement_id=requirement_id,
            title=title,
            current_phase="proposed",
            milestone_timestamps={"proposed": datetime.now().isoformat()},
            meeting_ids=[meeting_id] if meeting_id else [],
        )
        self.store.upsert_l3_requirement_evolution(evo)
        return evo

    # =========================================================================
    # 分层检索主流程
    # =========================================================================

    def retrieve(
        self,
        query: str,
        k: int = 3,
        use_time_decay: bool = True,
    ) -> list[HierarchicalRetrievedContext]:
        """
        分层检索主流程

        流程：
          1. L1 路由预判（轻量）
          2. L2 事实检索（按关键词过滤）
          3. L3 SOP 检索（按会议类型过滤）
          4. 层权重融合 + Reranker 精排
          5. 相关性阈值过滤
        """
        t_total = time.perf_counter()

        # ====== Stage 0: 缓存命中 ======
        cache_key = f"{query}|{k}"
        if cache_key in self._query_cache:
            self._cache_hits += 1
            return self._query_cache[cache_key]
        self._cache_misses += 1

        # ====== Stage 1: L1 路由 ======
        t_l1 = time.perf_counter()
        l1_result = self._l1_route(query)
        t_l1 = (time.perf_counter() - t_l1) * 1000

        if not l1_result["need_rag"] and not l1_result["relevant_l1_entries"]:
            self._log_latency(time.perf_counter() - t_total, stages={"l1": t_l1}, skipped=True)
            return []

        # 提取 L1 路由信息
        l1_keywords = list(l1_result["keywords"].keys())
        meeting_types = [l1_result["meeting_type"]]

        # ====== Stage 2: L2 事实检索 ======
        t_l2 = time.perf_counter()
        l2_facts = self._search_l2(query, l1_keywords, k=k)
        t_l2 = (time.perf_counter() - t_l2) * 1000

        # ====== Stage 3: L3 SOP 检索 ======
        t_l3 = time.perf_counter()
        l3_sops = self._search_l3(query, meeting_types, k=2)
        t_l3 = (time.perf_counter() - t_l3) * 1000

        # ====== Stage 4: 融合候选集 ======
        candidates: list[HierarchicalRetrievedContext] = []

        for fact in l2_facts:
            candidates.append(HierarchicalRetrievedContext(
                content=fact.content,
                layer="L2",
                relevance_score=0.0,
                source=fact.meeting_id,
                category=fact.category,
                verified=fact.verified,
                dense_score=0.5,
                time_decay=1.0,
                layer_boost=LAYER_BOOST["L2"],
            ))

        for sop in l3_sops:
            candidates.append(HierarchicalRetrievedContext(
                content=sop.content,
                layer="L3",
                relevance_score=0.0,
                source=sop.meeting_id,
                sop_type=sop.sop_type,
                usage_count=sop.usage_count,
                verified=sop.verified,
                dense_score=0.5,
                time_decay=1.0,
                layer_boost=LAYER_BOOST["L3"],
            ))

        if not candidates:
            # Fallback: 使用 base_retriever（原有扁平检索）
            if self.base_retriever:
                base_results = self.base_retriever.retrieve(query, k=k, use_time_decay=use_time_decay)
                self._log_latency(
                    time.perf_counter() - t_total,
                    stages={"l1": t_l1, "l2": t_l2, "l3": t_l3, "fallback": True}
                )
                return [HierarchicalRetrievedContext(
                    content=r.content,
                    layer="FALLBACK",
                    relevance_score=r.relevance_score,
                    source=r.source,
                    speaker=r.speaker,
                    dense_score=r.dense_score,
                    rerank_score=r.rerank_score,
                    time_decay=r.time_decay,
                    layer_boost=1.0,
                ) for r in base_results]
            return []

        # ====== Stage 5: Reranker 精排 ======
        t_rerank = time.perf_counter()
        if self.use_reranker and candidates:
            reranker = self._get_reranker()
            if reranker:
                pairs = [(query, ctx.content) for ctx in candidates]
                rerank_scores = reranker.predict(pairs)
                for ctx, score in zip(candidates, rerank_scores):
                    ctx.rerank_score = float(score)

                    # 综合打分：rerank × 层权重 × 时间衰减
                    soft_decay = ctx.time_decay ** 0.5 if use_time_decay else 1.0
                    ctx.relevance_score = float(score) * ctx.layer_boost * soft_decay

        # 按综合分数排序
        candidates.sort(key=lambda c: c.relevance_score, reverse=True)

        # ====== Stage 6: 阈值过滤 ======
        if candidates and candidates[0].relevance_score < self.min_relevance_score:
            print(f"[分层RAG] 检索质量不足（最高分={candidates[0].relevance_score:.3f} "
                  f"< {self.min_relevance_score}），跳过 RAG")
            self._log_latency(
                time.perf_counter() - t_total,
                stages={"l1": t_l1, "l2": t_l2, "l3": t_l3, "rerank": t_rerank},
                skipped=True,
            )
            return []

        # 写入缓存
        result = candidates[:k]
        if len(self._query_cache) >= self.cache_size:
            oldest_key = next(iter(self._query_cache))
            del self._query_cache[oldest_key]
        self._query_cache[cache_key] = result

        t_rerank = (time.perf_counter() - t_rerank) * 1000
        self._log_latency(
            time.perf_counter() - t_total,
            stages={"l1": t_l1, "l2": t_l2, "l3": t_l3, "rerank": t_rerank}
        )

        # 更新 Working Memory Anchor（每轮检索后追加摘要）
        self.working_memory.add_turn(
            f"检索 query={query[:30]}... 返回{len(result)}条，L1匹配{l1_result['l1_match_count']}条"
        )

        return result

    # =========================================================================
    # 上下文构建（GA 风格：按层级组织上下文）
    # =========================================================================

    def build_rag_prompt(
        self,
        query: str,
        contexts: list[HierarchicalRetrievedContext],
        include_meta: bool = True,
    ) -> str:
        """
        构建带分层检索结果的 Prompt

        按层级组织上下文（L3 SOP 在前，L2 事实次之）
        来自 GA 的 Completeness + Conciseness 原则：
        - 按优先级排列（最有价值的信息在前）
        - 每条标注来源和层级
        """
        if not contexts:
            return ""

        # 按层级优先级排序：L3 > L2 > FALLBACK
        layer_order = {"L3": 0, "L2": 1, "FALLBACK": 2, "L1": 3}
        contexts = sorted(contexts, key=lambda c: layer_order.get(c.layer, 99))

        context_lines = []
        total_len = 0

        for i, ctx in enumerate(contexts, 1):
            layer_tag = f"[{ctx.layer}]"
            source_info = f"来源：{ctx.source}"
            layer_info = f"层级：{ctx.layer}"

            if ctx.layer == "L2":
                meta = f"{layer_tag} {source_info} | {layer_info} | 类型：{ctx.category} | 已验证：{ctx.verified}"
            elif ctx.layer == "L3":
                meta = f"{layer_tag} {source_info} | {layer_info} | SOP类型：{ctx.sop_type} | 使用{ctx.usage_count}次"
            else:
                meta = f"{layer_tag} {source_info}"

            context_lines.append(f"[{i}] {meta}")
            context_lines.append(f"    内容：{ctx.content}")
            total_len += len(ctx.content)

        header = f"【分层RAG上下文 | 共{len(contexts)}条】\n"

        if include_meta:
            # 添加 L1 路由信息（帮助 LLM 理解信息来源）
            l1_result = self._l1_route(query)
            meta_lines = [
                "",
                f"【L1 路由信息】",
                f"  会议类型：{l1_result['meeting_type'].value}",
                f"  关键词：{', '.join(l1_result['keywords'].keys())}",
                f"  匹配记录：{l1_result['l1_match_count']}条",
            ]
            header += "\n".join(meta_lines) + "\n"

        header += "\n".join(context_lines)

        return f"""{header}

【当前问题】
{query}

要求：
1. 优先参考 L3 SOP 层（如有）：SOP 是经过验证的可复用流程
2. 结合 L2 事实层：事实知识提供项目背景
3. 如果信息不足，直接说明不知道，不要编造
4. 引用时标注层级（如：[L3]、[L2]）
"""

    # =========================================================================
    # 辅助方法
    # =========================================================================

    def _get_reranker(self):
        """懒加载 Reranker"""
        if self._reranker is None and self.use_reranker:
            try:
                from sentence_transformers import CrossEncoder
                self._reranker = CrossEncoder(self._reranker_model)
            except Exception:
                self._reranker = None
                self.use_reranker = False
        return self._reranker

    def _log_latency(self, total_s: float, stages: Optional[dict] = None, skipped: bool = False):
        """延迟日志（用于性能分析）"""
        pass  # 简化实现，可扩展

    def render_always_on_context(self, meeting_id: Optional[str] = None) -> str:
        """
        渲染始终注入的上下文（来自 GA 的 always-on memory）

        包括：
        1. Meta-Memory（记忆体系说明）
        2. L1 索引（轻量导航）
        3. Working Memory Anchor（当前会议状态）
        """
        parts = [
            self.meta_memory.render(),
            "",
            self.store.render_l1_for_context(meeting_id),
            "",
            self.working_memory.render(),
        ]
        return "\n\n".join(filter(None, parts))

    def add_meeting_record(
        self,
        meeting_id: str,
        utterances: list,
        timestamp: Optional[str] = None,
        meeting_types: Optional[list[str]] = None,
        summary: Optional[str] = None,
        action_items: Optional[list[str]] = None,
        decisions: Optional[list[str]] = None,
    ) -> dict:
        """
        添加会议记录并自动分层写入

        流程：
          1. 追加 L4 原始存档
          2. 更新 L1 索引
          3. 如果提供了 summary/action_items/decisions，写入 L2
        """
        if timestamp is None:
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 收集 utterances 文本
        utterances_texts = []
        speakers = []
        for u in utterances:
            if hasattr(u, "text"):
                utterances_texts.append(f"{getattr(u, 'speaker', 'unknown')}: {u.text}")
            elif isinstance(u, dict):
                utterances_texts.append(f"{u.get('speaker', 'unknown')}: {u.get('text', '')}")
            if hasattr(u, "speaker") and u.speaker not in speakers:
                speakers.append(u.speaker)
            elif isinstance(u, dict) and u.get("speaker") not in speakers:
                speakers.append(u.get("speaker", ""))

        full_text = "\n".join(utterances_texts)

        # 会议类型识别
        if meeting_types is None:
            detected_type = classify_meeting_type(full_text)
            meeting_types = [detected_type.value]

        # 写入 L4 原始存档
        from audiochat.rag.memory_hierarchy import L4RawEntry
        l4_entry = L4RawEntry(
            meeting_id=meeting_id,
            timestamp=timestamp,
            meeting_types=meeting_types,
            speakers=speakers,
            utterances_json=json.dumps(utterances, ensure_ascii=False) if not isinstance(utterances[0], str) else str(utterances),
            summary=summary or "",
            action_items=action_items or [],
            decisions=decisions or [],
        )
        self.store.add_l4_raw(l4_entry)

        # 更新 L1 索引
        self.store.update_l1_from_meeting(
            meeting_id=meeting_id,
            utterances_text=full_text,
            speakers=speakers,
            meeting_types=meeting_types,
        )

        # 如果提供了结构化输出，写入 L2 事实层
        written_facts = 0
        if summary or action_items or decisions:
            from audiochat.rag.memory_hierarchy import L2FactEntry
            if summary:
                self.store.add_l2_fact(L2FactEntry(
                    content=f"会议总结：{summary}",
                    category="meeting_summary",
                    verified=True,
                    meeting_id=meeting_id,
                    timestamp=timestamp,
                    confidence=0.9,
                ))
                written_facts += 1
            for item in (action_items or []):
                self.store.add_l2_fact(L2FactEntry(
                    content=f"行动项：{item}",
                    category="action_item",
                    verified=True,
                    meeting_id=meeting_id,
                    timestamp=timestamp,
                    confidence=0.9,
                ))
                written_facts += 1
            for decision in (decisions or []):
                self.store.add_l2_fact(L2FactEntry(
                    content=f"决策：{decision}",
                    category="decision",
                    verified=True,
                    meeting_id=meeting_id,
                    timestamp=timestamp,
                    confidence=0.95,
                ))
                written_facts += 1

        return {
            "meeting_id": meeting_id,
            "l4_written": True,
            "l1_updated": True,
            "l2_facts_written": written_facts,
        }

    def get_stats(self) -> dict:
        """获取检索器统计"""
        store_stats = self.store.get_stats()
        return {
            "store": store_stats,
            "reranker_loaded": self._reranker is not None,
            "cache_size": len(self._query_cache),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "working_memory_turns": self.working_memory.current_turn,
        }
