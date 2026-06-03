# Phase 2 & 3 实现：意图识别 + Agentic RAG

> 2026-06-03 本次会话新增功能的技术文档。
> 完整会话记录见：`AI聊天记录2026.6.3-意图识别与AgenticRAG实现.md`

---

## 一、Phase 1：Metadata Filter 修复

### 问题

`meeting_id` 和 `timestamp` 写入了 ChromaDB，但检索时没有传递，候选集偏大。

### 改动

**`audiochat/rag/storage.py`**：`search()` 增加复合 where 过滤：

```python
def search(
    self,
    query: str,
    k: int = 5,
    speaker_filter: Optional[str] = None,
    meeting_id_filter: Optional[str] = None,   # 新增
    time_range: Optional[tuple[str, str]] = None,  # 新增
) -> list[MeetingDocument]:
    conditions = []
    if speaker_filter:    conditions.append({"speaker": speaker_filter})
    if meeting_id_filter: conditions.append({"meeting_id": meeting_id_filter})
    if time_range:        conditions.append({"timestamp": {"$gte": start, "$lte": end}})
    if conditions:
        where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]
```

**`audiochat/rag/retriever.py`**：`retrieve()` 透传新参数，缓存 key 加上 filter 维度。

### 效果

ChromaDB 在向量检索前先做 metadata 过滤（HNSW 搜索前缩小候选集），检索更快更准。

---

## 二、Phase 2：意图识别路由

### 架构

```
用户问题
  ↓
规则快速判断（~0ms，零开销）
  ├─ 含"这场会议""刚才" → CURRENT_MEETING
  ├─ 含"上次""上周""之前" → CROSS_MEETING
  └─ 未命中
       ↓
    小模型精判（Qwen2.5-0.5B-Instruct，@lru_cache）
       ↓
    CURRENT_MEETING / CROSS_MEETING / UNKNOWN
```

### 三种意图的检索策略

| 意图 | 检索参数 | 说明 |
|------|---------|------|
| `CURRENT_MEETING` | `meeting_id_filter=当前会议ID` | 只在当前会议内搜 |
| `CROSS_MEETING` | `time_range=(start, end)` | 在时间范围内搜历史会议 |
| `UNKNOWN` | 不过滤 | 默认全量 RAG |

### 复用 ASR 校正的 0.5B 模型

意图识别复用 `Qwen2.5-0.5B-Instruct`（ASR 校正已加载），不额外部署，不额外花钱。

### 实现位置

`audiochat/prompting.py`：

| 函数 | 职责 |
|------|------|
| `classify_intent(question)` | 主入口，规则兜底 + 小模型精判 |
| `route_and_retrieve(...)` | 意图路由 + 执行检索 |
| `_rule_based_intent(question)` | 零延迟规则匹配 |
| `_cached_model_intent(question)` | 小模型分类（带 @lru_cache）|
| `IntentRoutingResult` | 路由结果 dataclass（含 confidence + reasoning）|

---

## 三、Phase 3：Agentic RAG

### 执行流程

```
转写文本
  ↓
analyze_info_gaps()    [LLM 调用 1]
  → 读转写摘要，输出 {info_type, topic, sub_query} 列表（最多 5 个缺口）
  ↓
plan_and_retrieve()    [并发批量检索，max_workers=4]
  → ThreadPoolExecutor 并发执行所有子查询
  → 总耗时 ≈ max(各查询耗时)，而非 sum
  ↓
compress_contexts()    [按层级/score 排序，截断至 ~2000 tokens]
  → 层级优先级：L3 SOP > L2 事实 > FALLBACK > L1 > RAW
  ↓
build_agentic_instruction() [拼接]
  ↓
Fun-Audio-Chat-8B 生成  [LLM 调用 2]
```

### 并发检索设计

```python
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    futures = [
        executor.submit(_execute_single_query, retriever, gap, time_range)
        for gap in gaps
    ]
    for future in concurrent.futures.as_completed(futures):
        gap, ctxs = future.result()
        all_contexts.extend(ctxs)
```

所有子查询并发执行，总耗时 ≈ max(各查询耗时) 而不是 sum(各查询耗时)。

### 上下文压缩策略

1. 按 `relevance_score` 降序排列
2. 同分数时，按层级优先级：L3 SOP > L2 事实 > FALLBACK > L1 > RAW
3. 超出 `max_tokens`（默认 2000）时从末尾截断
4. 同内容去重（按 content 哈希）

---

## 四、使用方式

```bash
# QA 模式（意图识别路由，Phase 2）
python scripts/offline_pipeline_workflow.py \
    --audio <wav> --enable-rag --mode qa \
    --query "这场会上张三说了什么"

# 总结模式 + Agentic RAG（Phase 3）
python scripts/offline_pipeline_workflow.py \
    --audio <wav> --enable-rag --agentic-rag \
    --time-range 2026-05-01,2026-06-03
```

---

## 五、Phase 2 vs Phase 3 对比

| 维度 | Phase 2（意图识别） | Phase 3（Agentic RAG） |
|------|-------------------|----------------------|
| 触发模式 | QA 模式 | Summary 模式 |
| 子查询数量 | 1（用户 query） | N（LLM 分析生成，最多 5 个） |
| 检索次数 | 1 次 | N 次并发 |
| LLM 调用 | 1 次（生成回答） | 2 次（分析缺口 + 生成总结）|
| 延迟 | 低 | 中（+1-2s）|

Phase 3 是 Phase 2 的扩展：QA 模式的单 query 路由 → 总结模式的多 query 规划。

---

## 六、技术决策记录

### 为什么用规则 + 小模型，不用纯规则或纯模型？

| 方案 | 延迟 | 准确率 | 成本 |
|------|------|--------|------|
| 纯规则 | ~0ms | 低，歧义必漏 | 0 |
| 纯小模型 | ~200ms | 高 | 已有 0.5B 模型在用，不额外花钱 |
| **规则兜底 + 小模型精判** | ~0ms（命中时）| 高 | 0 |

### 为什么用并发而不是串行？

串行：总耗时 = sum(各阶段)

并发：总耗时 ≈ max(各查询)，从线性叠加变成并行加速。

### 上下文压缩为什么用启发式而非 LLM？

LLM 压缩需要额外一次 LLM 调用（约 500ms），且输出不稳定。启发式压缩零开销、稳定可复现，效果足够——高相关的 L3/L2 内容会被优先保留。

---

*最后更新：2026-06-03*
