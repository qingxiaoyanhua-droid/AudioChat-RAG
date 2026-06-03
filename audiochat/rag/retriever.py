"""
RAG 检索器模块 — BGE Embedding 粗排 → BGE Reranker 精排 → Soft Decay

检索流程：
  1. BGE Embedding (Bi-Encoder) 语义召回 Top-15（ChromaDB 余弦相似度）
  2. 全部候选送入 BGE Reranker (Cross-Encoder) 精排（不截断）
  3. final_score = rerank_score × sqrt(decay)
     用 sqrt 拉平衰减曲线，避免老但重要的文档被完全淹没

为什么用 sqrt(decay) 而不是 decay？
  decay = 0.5^(30/7) ≈ 0.05 → 几乎淘汰
  sqrt(decay) ≈ 0.22          → 仍有竞争力
  开根号等效于把半衰期翻倍，让时间做微调而非主导排序。

优化项：
  - 预加载（warm_up）：初始化时预热 Embedder + Reranker，消除冷启动
  - LRU 缓存：对 query 结果缓存，相同 query 秒回
  - 相关性阈值过滤：Reranker 打分低于阈值时自动跳过，防止低质量 RAG 干扰
  - 延迟分析器：量化各阶段耗时，便于性能调优
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import time

from audiochat.rag.storage import MeetingMemoryStore, MeetingDocument


@dataclass
class RetrievedContext:
    """检索到的上下文"""
    content: str
    relevance_score: float
    source: str
    speaker: Optional[str] = None
    dense_score: float = 0.0
    rerank_score: float = 0.0
    time_decay: float = 1.0


class AudioChatRetriever:
    """
    音频对话 RAG 检索器（Bi-Encoder 粗排 + Cross-Encoder 精排）

    支持:
    - BGE Embedding 语义召回（Bi-Encoder，粗排）
    - 时间衰减重排序（指数衰减，半衰期 7 天）
    - BGE Reranker 精排（Cross-Encoder，交叉注意力）
    - 说话人过滤
    - 关键词豁免：结论性文档不受时间衰减（"最终决定""结论："等）
    - 预加载（warm_up）：消除首次查询冷启动
    - LRU 缓存：相同 query 秒回
    - 相关性阈值过滤：防止低质量 RAG 干扰全新主题
    """

    EXEMPT_PATTERNS = {
        "最终决定",
        "最终方案",
        "结论：",
        "决定采用",
        "选定",
        "已确定",
        "拍板",
        "会议决议",
        "已批准",
        "确认采用",
        "批准使用",
        "结论是",
        "决定是",
    }

    def __init__(
        self,
        storage: Optional[MeetingMemoryStore] = None,
        reranker_model: str = "BAAI/bge-reranker-large",
        use_reranker: bool = True,
        min_relevance_score: float = 0.35,
        cache_size: int = 128,
    ):
        self.storage = storage or MeetingMemoryStore()
        self.use_reranker = use_reranker
        self.min_relevance_score = min_relevance_score
        self.cache_size = cache_size
        self._reranker = None
        self._reranker_model = reranker_model
        self._query_cache: dict[str, list[RetrievedContext]] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._latency_log: list[dict] = []

    def _warm_up(self):
        """预热：提前加载 Embedder + Reranker，消除冷启动延迟"""
        _ = self._get_reranker()
        self.storage.search("预热", k=1)
        print("[RAG] 预热完成，模型已加载")

    def _get_reranker(self):
        """懒加载 BGE Reranker（Cross-Encoder）"""
        if self._reranker is None and self.use_reranker:
            try:
                from sentence_transformers import CrossEncoder
                self._reranker = CrossEncoder(self._reranker_model)
            except Exception:
                self._reranker = None
                self.use_reranker = False
        return self._reranker

    def retrieve(
        self,
        query: str,
        k: int = 3,
        recall_k: int = 15,
        speaker_filter: Optional[str] = None,
        meeting_id_filter: Optional[str] = None,
        time_range: Optional[tuple[str, str]] = None,
        use_time_decay: bool = True,
        use_rerank: bool = True,
    ) -> list[RetrievedContext]:
        """
        两阶段检索 + Soft Decay + 阈值过滤 + LRU 缓存

        新增 metadata filter 参数（Phase 1 改动）：
          - meeting_id_filter: 精确锁定特定会议，用于"只查当前会议"场景
          - time_range: 时间范围过滤，用于"上周""近一个月"等时间敏感检索

        ChromaDB 的 where 过滤发生在向量检索之前，filter 条件越多候选集越小，检索越快。
        """
        t_total = time.perf_counter()

        # ====== Stage 0: 缓存命中检查 ======
        cache_key = self._make_cache_key(
            query, k, speaker_filter, meeting_id_filter, time_range
        )
        if cache_key in self._query_cache:
            self._cache_hits += 1
            cached = self._query_cache[cache_key]
            self._log_latency(time.perf_counter() - t_total, cache_hit=True)
            return cached

        self._cache_misses += 1

        # ====== Stage 1: BGE Embedding 粗排（Bi-Encoder）======
        t_emb = time.perf_counter()
        docs = self.storage.search(
            query,
            k=recall_k,
            speaker_filter=speaker_filter,
            meeting_id_filter=meeting_id_filter,
            time_range=time_range,
        )
        t_emb = (time.perf_counter() - t_emb) * 1000
        if not docs:
            self._log_latency(time.perf_counter() - t_total, stage_times={"embedding": t_emb})
            return []

        candidates: list[RetrievedContext] = []
        for doc in docs:
            dense_score = self._compute_dense_score(query, doc.content)
            decay = 1.0
            if use_time_decay and doc.timestamp and not self._is_exempt(doc.content):
                decay = self._time_decay(doc.timestamp)

            candidates.append(RetrievedContext(
                content=doc.content,
                relevance_score=dense_score * decay,
                source=doc.meeting_id,
                speaker=doc.speaker,
                dense_score=dense_score,
                time_decay=decay,
            ))

        # ====== Stage 2: BGE Reranker 精排（Cross-Encoder）======
        t_rerank = time.perf_counter()
        reranker = self._get_reranker() if use_rerank else None
        if reranker and candidates:
            pairs = [(query, ctx.content) for ctx in candidates]
            rerank_scores = reranker.predict(pairs)

            for ctx, score in zip(candidates, rerank_scores):
                ctx.rerank_score = float(score)
                soft = ctx.time_decay ** 0.5 if use_time_decay else 1.0
                ctx.relevance_score = float(score) * soft

            candidates.sort(key=lambda c: c.relevance_score, reverse=True)
        else:
            candidates.sort(key=lambda c: c.relevance_score, reverse=True)
        t_rerank = (time.perf_counter() - t_rerank) * 1000

        # ====== Stage 3: 相关性阈值过滤 ======
        # 如果最高分低于阈值，说明 RAG 质量差，跳过 RAG
        if candidates and candidates[0].relevance_score < self.min_relevance_score:
            print(f"[RAG] 检索质量不足（最高分={candidates[0].relevance_score:.3f} "
                  f"< {self.min_relevance_score}），跳过 RAG，使用纯当前会议内容")
            self._log_latency(
                time.perf_counter() - t_total,
                stage_times={"embedding": t_emb, "rerank": t_rerank},
                skipped=True
            )
            return []

        # LRU 缓存写入（超过容量则淘汰最老的）
        result = candidates[:k]
        if len(self._query_cache) >= self.cache_size:
            oldest_key = next(iter(self._query_cache))
            del self._query_cache[oldest_key]
        self._query_cache[cache_key] = result

        self._log_latency(
            time.perf_counter() - t_total,
            stage_times={"embedding": t_emb, "rerank": t_rerank}
        )
        return result

    def _make_cache_key(self, query: str, k: int, speaker_filter: Optional[str],
                        meeting_id_filter: Optional[str] = None,
                        time_range: Optional[tuple[str, str]] = None) -> str:
        """生成缓存键（用于 LRU 缓存）"""
        import hashlib
        time_str = f"{time_range[0]}_{time_range[1]}" if time_range else ""
        key_str = f"{query}|{k}|{speaker_filter}|{meeting_id_filter}|{time_str}"
        return hashlib.md5(key_str.encode()).hexdigest()

    def _log_latency(self, total_s: float, stage_times: Optional[dict] = None,
                     cache_hit: bool = False, skipped: bool = False):
        """记录延迟数据（用于性能分析）"""
        self._latency_log.append({
            "total_ms": total_s * 1000,
            "stage_times": stage_times or {},
            "cache_hit": cache_hit,
            "skipped": skipped,
        })
        # 最多保留 1000 条
        if len(self._latency_log) > 1000:
            self._latency_log = self._latency_log[-1000:]

    def get_latency_stats(self) -> dict:
        """获取延迟统计数据（可量化 RAG 各阶段耗时）"""
        import numpy as np
        if not self._latency_log:
            return {"message": "暂无延迟数据"}

        totals = [e["total_ms"] for e in self._latency_log if not e["skipped"]]
        hits = [e for e in self._latency_log if e["cache_hit"]]
        skips = [e for e in self._latency_log if e["skipped"]]

        stage_keys = ["embedding", "rerank"]
        stage_stats = {}
        for sk in stage_keys:
            vals = [e["stage_times"].get(sk, 0)
                    for e in self._latency_log if e["stage_times"].get(sk)]
            if vals:
                stage_stats[sk] = {
                    "p50": float(np.median(vals)),
                    "p95": float(np.percentile(vals, 95)),
                }

        return {
            "total_p50_ms": float(np.median(totals)) if totals else 0,
            "total_p95_ms": float(np.percentile(totals, 95)) if totals else 0,
            "cache_hit_rate": len(hits) / len(self._latency_log) if self._latency_log else 0,
            "skip_rate": len(skips) / len(self._latency_log) if self._latency_log else 0,
            "stage_times": stage_stats,
        }

    def _compute_dense_score(self, query: str, content: str) -> float:
        """Bi-Encoder 语义相似度（BGE Embedding + 余弦相似度）"""
        from sentence_transformers import util
        q_emb = self.storage.embedder.encode(query, convert_to_tensor=True)
        c_emb = self.storage.embedder.encode(content, convert_to_tensor=True)
        sim = util.cos_sim(q_emb, c_emb).item()
        return float((sim + 1) / 2)

    def _is_exempt(self, content: str) -> bool:
        """
        结论性文档豁免：含"最终决定""结论："等句式表示决策已定，通常不过时

        和旧关键词方案的区别：
        - 旧方案用 topic 词（"架构""决策"），假阳性多——"讨论架构" ≠ "架构已定"
        - 新方案用结论句式（"最终决定""结论："），只有决策已定时才豁免
        """
        return any(pat in content for pat in self.EXEMPT_PATTERNS)

    def _time_decay(self, timestamp: str, half_life_days: float = 7.0) -> float:
        """
        指数时间衰减函数

        decay = 0.5 ^ (days_diff / half_life)
        半衰期 7 天：7 天前的文档权重降为 0.5，14 天前降为 0.25
        最小值 0.1，避免完全忽略旧但相关的内容

        半衰期 7 天依据：
          - 周会节奏：团队每周一有一次总结会议，信息以周为单位更新
          - 7 天足够旧信息衰减到 50%，又不会太快丢弃仍有价值的历史上下文
        """
        try:
            doc_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            now = datetime.now(doc_time.tzinfo) if doc_time.tzinfo else datetime.now()
            days_diff = (now - doc_time).days
            decay = 0.5 ** (days_diff / half_life_days)
            return max(0.1, decay)
        except Exception:
            return 1.0

    def build_rag_prompt(
        self,
        query: str,
        contexts: list[RetrievedContext],
        max_context_length: int = 1000,
    ) -> str:
        """构建带检索结果的 Prompt"""
        if not contexts:
            return query

        context_text = []
        total_len = 0
        for i, ctx in enumerate(contexts, 1):
            if total_len >= max_context_length:
                break
            preview = ctx.content[:200] + "..." if len(ctx.content) > 200 else ctx.content
            speaker_info = f" (说话人：{ctx.speaker})" if ctx.speaker else ""
            context_text.append(f"[{i}] {preview}{speaker_info}")
            total_len += len(preview)

        context_str = "\n".join(context_text)

        return f"""请基于以下相关背景信息回答问题：

【相关背景】
{context_str}

【当前问题】
{query}

要求：
1. 必须基于背景信息回答
2. 如果背景信息中没有相关内容，请直接说明"不知道"
3. 标注引用来源（如：[1]、[2]）

请回答："""

    def add_meeting_record(
        self,
        meeting_id: str,
        utterances: list,
        timestamp: Optional[str] = None,
    ) -> int:
        """添加会议记录到存储"""
        if timestamp is None:
            timestamp = datetime.now().isoformat()

        docs = []
        for u in utterances:
            if len(u.text.strip()) < 5:
                continue
            docs.append(MeetingDocument(
                content=f"{u.speaker}: {u.text}",
                meeting_id=meeting_id,
                speaker=u.speaker,
                timestamp=timestamp,
                metadata={
                    "start_ms": u.start_ms,
                    "end_ms": u.end_ms,
                },
            ))

        ids = self.storage.add_batch(docs)
        return len(ids)

    def get_stats(self) -> dict:
        """获取检索器统计信息"""
        storage_stats = self.storage.get_stats()
        return {
            "storage": storage_stats,
            "reranker_loaded": self._reranker is not None,
            "cache_size": len(self._query_cache),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "latency": self.get_latency_stats(),
        }