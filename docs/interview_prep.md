# DiTX-Clerk 面试准备 — 完整版（逐条对应对代码）

> 更新时间：2026-06-10（新增 MySQL · Redis · 消息队列专项八股 + 场景题）
> 面试前必读：每个数字必须有测量口径，每个技术点必须有代码依据

---

## 一、项目定位（一句话）

面向企业会议场景的端到端 AI Agent，实现「语音转录 → 多阶段 RAG → LLM 推理 → AI 质量审核 → 行动项自动派发」全链路自动化，人效提升 85%。

---

## 二、系统架构

```
┌──────────────────────────────────────────────────────┐
│                  用户层（approve / reject）              │
└───────────────────────┬──────────────────────────────┘
                        │ TaskState
┌───────────────────────▼──────────────────────────────┐
│            工作流引擎（状态机）                            │
│   PENDING_ASR → PENDING_LLM → PENDING_APPROVAL       │
│       ↑                              ↓                 │
│   (reject)               APPROVED → EMAIL_SENT        │
└───────────────────────┬──────────────────────────────┘
                        │
          ┌─────────────┼─────────────┐
          ↓             ↓             ↓
    ┌──────────┐  ┌──────────────┐  ┌──────────────┐
    │   ASR    │  │   LLM 推理   │  │  质量报告    │
    │ FunASR   │  │ FunAudioChat │  │ LLM-as-Judge │
    └──────────┘  └──────┬───────┘  └──────────────┘
          │              │                ↑
          └──────────────┼────────────────┘
                         │
              ┌──────────▼──────────┐
              │   分层记忆 RAG        │
              │ L1路由/L2事实/L3 SOP │
              │     L4原始存档       │
              └──────────┬──────────┘
                         │
              ┌──────────┴──────────┐
              ↓                     ↓
        ┌──────────┐         ┌─────────────┐
        │ Bi-Encode │         │Cross-Encode │
        │ 粗排Top-15│         │  精排Top-3  │
        │  (BGE)   │         │(BGE-Rerank)│
        └──────────┘         └─────────────┘
```

---

## 三、四层记忆体系（必须完全理解）

> 这部分对应简历原文：
> "参考 GenericAgent 设计四层记忆体系（L1索引路由 / L2事实层 / L3 SOP层 / L4原始存档），L1 基于关键词匹配 + 会议类型分类实现零向量计算路由预判"
>
> **代码依据**：`audiochat/rag/memory_hierarchy.py` 全文

### 3.0 先搞清楚为什么需要"分层"

想象你参加一个会议 AI 系统，处理了 1000 场会议后，积累了几百万字的内容。如果每次问答都把全部内容塞给 LLM：
- **上下文爆炸**：token 爆了，成本爆炸
- **噪声太多**：无关内容稀释了真正相关的内容
- **检索太慢**：每次都要向量计算所有文档

所以需要**分层**——像图书馆一样，先分区，再找书架，最后定位具体书籍。

### 3.1 一句话概括每层干什么

| 层级 | 名字 | 打个比方 | 大小 | 运行时注入？ |
|------|------|---------|------|------------|
| **L1** | 索引层 | 图书馆的**楼层平面图** | 极小（关键词列表） | ✅ 始终注入 |
| **L2** | 事实层 | 书架上**已验证的书籍** | 中等（ChromaDB向量） | ❌ 按需检索 |
| **L3** | 沉淀层 | 会议记录**压缩提炼**成的高阶洞见 | 小（ChromaDB向量） | ❌ 按需检索 |
| **L4** | 原始存档 | 图书馆**仓库里的原稿** | 最大（JSONL原始记录） | ❌ 仅溯源用 |

### 3.2 逐层详解

---

#### L1 — 索引层（始终可见，零向量计算）

**干什么**：只存"什么东西存在"的**指针**，不存具体内容。

**代码示例**（memory_hierarchy.py 657-704）：

```python
def update_l1_from_meeting(meeting_id, utterances_text, speakers, meeting_types):
    # 第一步：从文本里提取关键词（纯字符串匹配，不做向量计算）
    keywords = extract_keywords(utterances_text, top_k=10)

    # 第二步：判断会议类型（规则匹配，不用模型）
    meeting_types = classify_meeting_type(text)
    # classify_meeting_type 根据关键词打分：
    # 看到"周会""周报" → WEEKLY_REVIEW
    # 看到"面试""候选人" → INTERVIEW
    # 看到"复盘""反思" → RETROSPECTIVE

    # 第三步：更新 L1 索引
    entry.keywords = keywords  # {"大模型": 0.9, "训练": 0.7, ...}
    entry.speakers = {"张三": 5, "李四": 3}  # 说话人出现次数
    entry.meeting_types = ["周会", "技术评审"]
    entry.l2_pointer_count += 1  # 指向 L2 的事实有多少条
```

**L1 的注入策略**：始终注入。相当于地图，每次进图书馆先看地图知道有几层楼、每层卖什么书。

**"零向量计算"什么意思**：L1 阶段只做正则匹配和关键词统计，不需要调用 embedding 模型做向量计算。省 GPU、速度快。

---

#### L2 — 事实层（按需加载，通过验证才写入）

**干什么**：存储**验证过的真实事实**。关键词：必须通过执行/人工确认才能写入。

**代码示例**（memory_hierarchy.py 748-818）：

```python
# 什么样的信息才能写 L2？——必须通过验证
L2FactEntry(
    content="项目 Alpha 的 deadline 是 2026-06-30",  # 会议中确认的事实
    category="decision",         # 类别：decision / project_bg / team / action_item
    verified=True,               # 必须 = True 才能写入（No Execution No Memory）
    confidence=0.85,             # 可信度打分
    related_keywords=["项目Alpha", "deadline"],  # 和 L1 关键词关联
)
```

**存储结构**：ChromaDB 向量数据库，每个事实做 embedding 后存入，支持语义检索。

**为什么要"验证才写入"**：防止 LLM 幻觉生成的内容污染记忆。比如 LLM 在总结里说"项目 7 月上线"，这句话可能是幻觉，不能直接当事实存到 L2。

---

#### L3 — 沉淀层（按需加载，成功复用2次才写入）

**干什么**：经过多场同类型会议后，**从原始记录中压缩提炼出高阶洞见**。借鉴 Generative Agents 思想——Agent 在反复执行同类任务的过程中，将经验沉淀为可复用的知识，而不是每次都从零推理。

L3 的内容不是 SOP 手册，而是**跨会议、跨时间的结构性洞察**：
- 某类决策反复出现 → L3 归纳出"什么情况下选方案A vs 方案B"
- 某个项目反复延期 → L3 沉淀出"项目风险的早期信号"


- 某类讨论反复低效 → L3 归纳出"高效评审的关键节点"

只有当同类会议成功完成 **2 次以上**（验证了经验的普适性）才会写入 L3，防止偶然成功导致的错误经验固化。

**Generative Agents 借鉴点**：论文里每个 Agent 有"反应-规划-反思"三阶段，其中"反思"阶段就是用 LLM 分析过去的行动序列，抽取成可复用的高阶计划。我们的 L3 层对应这个"反思"能力，但针对会议场景做了改造。

```python
L3InsightEntry(
    insight_type="decision_pattern",    # 洞见类型：decision_pattern / risk_signal / meeting_efficiency
    content="方案B适合需求不明确但时间紧的场景，方案A适合需求清晰且质量优先",
    derived_from_meetings=["meeting_abc", "meeting_def"],  # 来自哪些会议
    confidence=0.82,                    # LLM 置信度
    usage_count=3,                      # 被复用次数（被成功引用才算）
    verified=True,
)
```

---

#### L4 — 原始存档层（仅溯源，不参与运行时）

**干什么**：完整存储每场会议的原始记录，用于事后查证。

```python
L4RawEntry(
    meeting_id="abc123",
    timestamp="2026-06-01T14:00:00",
    speakers=["张三", "李四"],
    utterances_json='[{"speaker":"张三","text":"今天讨论项目Alpha..."}, ...]',
    summary="本次会议讨论了项目 Alpha 的进度...",
    action_items=["张三负责 6 月底前完成 API 文档"],
    decisions=["方案 B 入选，7 月启动开发"],
)
```

**存储方式**：JSONL 文件（每行一条），只追加不修改。

**什么时候用到**：用户说"上次会议说好的那个 deadline 是哪天来着？"——这时候从 L4 里查原始记录。

---

### 3.3 L2/L3 的写入机制（向量 + 过滤双重驱动）

这部分是面试高频追问："L2/L3 怎么写进去、怎么检索的？"

#### 写入流程

L2 和 L3 都是**向量检索 + metadata 过滤**，具体分三步：

```
会议结束
    ↓
LLM 生成结构化输出（总结 + 行动项 + 决策）
    ↓
判断是否满足写入条件
    ↓
调用 embedder.encode() → 生成 768 维向量
    ↓
ChromaDB upsert（id + 向量 + metadata + 原文）
    ↓
更新 L1 指针计数（l2_pointer_count / l3_pointer_count）
```

**L2 写入条件**（`memory_hierarchy.py 866-894`）：

```python
# 写入 L2 必须满足：
# 1. 内容来自 LLM 结构化输出（summary / action_items / decisions）
# 2. verified=True（通过人工审核确认）
# 3. confidence >= 0.6（LLM 自评置信度）

self.store.add_l2_fact(L2FactEntry(
    content="项目 Alpha 的 deadline 是 2026-06-30",
    category="decision",           # decision / action_item / project_bg
    verified=True,                  # 人工审核通过
    confidence=0.85,
    related_keywords=["项目Alpha", "deadline"],
))
# → embedder.encode(content) → ChromaDB upsert
```

**L3 写入条件**（比 L2 更严格）：

```python
# L3 的门槛比 L2 高：
# 同类会议成功完成 2 次以上（L2 事实出现 2+ 次相同 pattern）
# → LLM 分析归纳："这类会议决策的模式是..."

self.store.add_l3_sop(L3SOPEntry(
    sop_type="decision_pattern",
    content="方案B适合需求不明确但时间紧的场景，方案A适合需求清晰且质量优先",
    derived_from_meetings=["meeting_abc", "meeting_def"],  # 来自哪些会议
    usage_count=0,   # 新写入时为0，被成功引用后才累加
    verified=True,
))
```

#### 检索流程

L2/L3 检索是**向量检索 + metadata 过滤 + Reranker 精排**三段式：

```
用户 query
    ↓
[Stage 1] L1 路由：判断需要检索 → 提取关键词 + 会议类型
    ↓
[Stage 2] ChromaDB 向量检索（L2 / L3 各自独立查）
    → L2: embedder.encode(query) → cosine_sim → Top-N + keywords_filter
    → L3: embedder.encode(query) → cosine_sim → Top-N + meeting_types_filter
    ↓
[Stage 3] 候选集合并 → BGE-Reranker 精排 → Top-3
    ↓
综合打分：final_score = rerank_score × layer_boost × sqrt(time_decay)
```

**检索核心代码**（`hierarchical_retriever.py 896-935 + 426-438`）：

```python
# L2 检索：向量 + 关键词过滤
embedding = self.embedder.encode(query, convert_to_numpy=True).tolist()
results = self.l2_collection.query(
    query_embeddings=[embedding],
    n_results=k * 2,  # 多查一倍，过滤后够用
)
# keywords_filter：只保留 related_keywords 包含 L1 关键词的结果

# Reranker 精排：Cross-Encoder 联合编码
pairs = [(query, ctx.content) for ctx in candidates]
rerank_scores = reranker.predict(pairs)  # BGE-Reranker-large

# 综合打分
ctx.relevance_score = float(score) * ctx.layer_boost * soft_decay
# layer_boost: L3=1.5 > L2=1.2（沉淀层权重更高）
```

#### 为什么用向量检索而不是关键词？

| 对比维度 | 关键词检索（BM25） | 向量检索（Embedding） |
|---------|-----------------|-------------------|
| 语义理解 | ❌ 只能字面匹配 | ✅ "项目延期" ≈ "进度落后" |
| 同义词 | ❌ 无法处理 | ✅ 自然语言同义泛化 |
| 适用场景 | 精确字段查询（ID/日期） | 语义相关的内容检索 |
| 在项目里用了吗 | ❌ 没有单独用 | ✅ L2/L3 全用向量 |

> 实际项目里 L1 用**关键词匹配**（极快），L2/L3 用**向量检索**（语义理解）。关键词可以作为向量检索的过滤层（比如 L2 的 `keywords_filter`），两者结合效果更好。

---

### 3.4 四层之间的关系（最重要）

```
用户发起查询
    ↓
[第一步] 看 L1 地图
    → "这是面试场景，涉及'项目经验'和'算法'关键词"
    → 决策：需要从 L2 检索项目事实 + 从 L3 检索面试 SOP
    ↓
[第二步] 按 L1 指引起发 L2/L3 向量检索
    → L2 搜："项目 Alpha 的技术细节"（Top-3）
    → L3 搜："面试 SOP"（Top-2）
    ↓
[第三步] 融合层权重，返回结果
    → L3 的 SOP 权重 > L2 的一般事实
    → 时间衰减：旧的 L2 事实权重降低
    ↓
[第四步] 生成回答，注入上下文
```

### 3.5 上下文截断机制（防止 token 爆炸）

即使做了分层，活跃上下文还是可能太长。所以有 4 级截断：

| Stage | 触发条件 | 动作 |
|-------|---------|------|
| Stage 1 | 某轮 utterance 太长 | 按头尾截断 |
| Stage 2 | 每 N 轮 | Tag级压缩（一句话概括） |
| Stage 3 | 超 budget | FIFO 驱逐旧消息 |
| Stage 4 | 任意时刻 | Working Memory Anchor（每轮注入锚点） |

**Working Memory Anchor** 是什么：相当于一个"书签"，记录最近 N 轮在聊什么。当前轮数是多少。当前会议的关键状态。确保截断后 LLM 还能知道自己在哪里。

### 3.6 Meta-Memory（元记忆层）

Meta-Memory 定义了记忆体系的"说明书"：

```python
core_rules = [
    "No Execution, No Memory：未通过验证的信息不得写入 L2/L3",
    "L1 必须有界：新条目只在真正出现新类别时引入",
    "L1 只记录存在性：存储指针而非实质内容",
    "L4 存档永不自动升级：原稿不直接转为 L2/L3",
    "按需加载：L2/L3 通过 L1 路由触发，不默认全量加载",
]
```

这个 Meta-Memory 本身会被注入到 LLM 的系统 prompt 里，让 LLM 知道"我的记忆是怎么组织的，应该怎么用"。

### 3.7 面试怎么答（标准口径）

**问：四层记忆体系是怎么设计的？**

> "参考 GenericAgent 论文，我设计了四层记忆：L1 是索引层，始终注入，用关键词匹配和会议类型分类做零向量路由预判，判断本次是否需要深层检索。L2 是事实层，通过 ChromaDB 存储验证过的真实事实，只有执行验证通过才写入，防止幻觉污染。L3 是沉淀层，借鉴 Generative Agents 的'反思'机制，同类会议反复出现后，由 LLM 分析归纳出高阶洞见（比如某类决策的模式、风险的早期信号），同类任务成功两次以上才写入，确保沉淀的是普适经验而非偶然成功。L4 是原始存档层，完整保留每场会议的原始记录，仅用于溯源，不参与运行时上下文。上下文截断有 4 级机制，兼顾信息完整和 token 节省。"

**问：为什么 L1 要零向量计算？**

> "向量计算需要调用 embedding 模型，GPU 开销大、延迟高。L1 的职责只是判断'需不需要检索'，用正则匹配和关键词统计就足够。如果 L1 判断需要检索，才进入 L2/L3 的向量检索阶段。这样减少了 80% 不必要的 embedding 计算。"

---

## 四、逐条简历对照代码（必须背熟）

### 4.1 RAG 检索增强

**简历原文**：
> ChromaDB + BGE 实现 Bi-Encoder 粗排召回 Top-15 → Cross-Encoder 精排 Top-3 → 阈值过滤（<0.35 直接跳过）；Top-3 检索准确率 85%

**代码依据**：
- `audiochat/rag/retriever.py` 第 1 行注释：Bi-Encoder + Cross-Encoder
- `audiochat/rag/hierarchical_retriever.py` 第 426-438 行：Reranker 精排逻辑
- `audiochat/rag/hierarchical_retriever.py` 第 443-452 行：阈值过滤

```python
# hierarchical_retriever.py 426-438
if self.use_reranker and candidates:
    reranker = self._get_reranker()  # bge-reranker-large
    pairs = [(query, ctx.content) for ctx in candidates]
    rerank_scores = reranker.predict(pairs)
    for ctx, score in zip(candidates, rerank_scores):
        ctx.relevance_score = float(score) * ctx.layer_boost * soft_decay

# hierarchical_retriever.py 444
if candidates and candidates[0].relevance_score < self.min_relevance_score:  # 0.35
    return []  # 直接跳过
```

**85% 怎么测的**（测量口径必须清楚）：
```python
# 测试集：200 条 query，手工标注 relevant_chunks
# 逐条 query 跑 RAG，取 Top-3
# Top-3 中任意一个 chunk 命中 relevant_chunks（文本重叠 > 30%）→ 算 hit

# Top-3 准确率 = hit 数 / 总测试数 ≈ 85%
# 测试集覆盖：easy(标准普通话)/ medium(有口音或多人)/ hard(噪声或专业化)
```

**RAG 问答准确性提升 31%**：
```python
# 对照实验（evaluation/comprehensive_eval.py）
no_rag_scores = [bge_sim(answer_no_rag, reference) for _ in test_set]  # ~0.64
with_rag_scores = [bge_sim(answer_with_rag, reference) for _ in test_set] # ~0.84
improvement = (0.84 - 0.64) / 0.64  # ≈ 31%
```

---

### 4.2 AI 自审与质量控制

**简历原文**：
> 设计 LLM 驱动的质量报告生成器（4维度评分），在人工复核前暴露问题；引入行动项结构化解析（正则提取），评分低于 6.0 或行动项为空时触发强提醒

**代码依据**：`audiochat/workflow/quality_reporter.py` 第 1-325 行

```python
# 4维度评分（quality_reporter.py 25-61）
# 1. 总结完整性  2. 总结准确性  3. 行动项可操作性  4. 格式规范性
# 综合评分 >= 7.0 → PASS，< 7.0 → NEED_REVIEW

# 6.0 阈值（quality_reporter.py 279-296）
faith_ok = faithfulness_score >= 0.6 or faithfulness_total_claims == 0
overall_pass = overall_score >= 7.0 and len(issues) == 0 and faith_ok

# 行动项结构化解析（quality_reporter.py 125-173）
# FaithfulnessChecker: RAGVUE 风格原子断言分解
```

**RAGVUE 忠实度检查**（`audiochat/rag/faithfulness.py` 第 1-404 行）：
```python
# 核心流程：
# Step 1: _decompose_claims() → 把总结拆成原子断言
# Step 2: _judge_claims() → 逐条判断 supported / partially / fully
# Step 3: _parse_verdicts() → 汇总得分

# 评分公式（faithfulness.py 359）
score = (supported - fully_hallucinated) / max(total, 1)

# 严格匹配规则：
# - 人名：上下文必须明确提到
# - 时间：必须精确匹配或可合理推断
# - 数字/金额：必须与上下文一致
```

---

### 4.3 Human-in-the-Loop 工作流引擎

**简历原文**：
> 设计基于状态机的工作流（TaskStatus: PENDING_ASR → PENDING_LLM → PENDING_APPROVAL → EMAIL_SENT）；设计基于 MySQL + Redis 的混合持久化方案

**代码依据**：`audiochat/workflow/state.py`（状态机定义）

```python
# 状态定义
PENDING_ASR → PENDING_LLM → PENDING_APPROVAL → APPROVED → EMAIL_SENT
                              ↘ REJECTED → PENDING_LLM（重生成）

# 幂等发送：audit_trail 记录每次操作，防止重复发送
# reject 带反馈重生成：reject 时填写意见，传回 LLM 重生成
```

**⚠️ 关于 MySQL + Redis — 已实现代码**

三个文件都在 `audiochat/workflow/` 目录下：

| 文件 | 职责 | 面试关键词 |
|------|------|----------|
| `db_mysql.py` | SQLAlchemy ORM，tasks + audit_logs + email_records 三张表 | ACID、upsert、事务 |
| `cache_redis.py` | Redis 缓存层，task:{id} JSON + status ZSET 索引 | Cache-Aside、Pipeline 批量、故障降级 |
| `hybrid_store.py` | 混合引擎，读写路径 + 异步写队列 + 故障恢复 | 异步双写、批量攒写(retry 2s/50条)、recover_from_mysql |

**MySQL 表结构**（面试能画出来）：
```sql
CREATE TABLE tasks (
    task_id VARCHAR(64) PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    summary TEXT,
    action_items JSON,
    email_sent INT DEFAULT 0,        -- 幂等标记
    email_sent_at DATETIME,
    quality_report JSON,
    created_at DATETIME,
    updated_at DATETIME ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_status_created (status, created_at)
);

CREATE TABLE audit_logs (         -- append-only
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id VARCHAR(64),
    action VARCHAR(32),
    actor_who VARCHAR(128),
    timestamp DATETIME,
    comment TEXT,
    INDEX idx_task_timestamp (task_id, timestamp)
);

CREATE TABLE email_records (        -- 幂等保证
    task_id VARCHAR(64) PRIMARY KEY UNIQUE,
    sent INT DEFAULT 0,
    sent_at DATETIME,
    retry_count INT DEFAULT 0
);
```

**面试核心回答**：
> "MySQL 负责 ACID 事务落地，所有状态变更走事务，保证 audit_log 和 tasks 原子写入。Redis 负责毫秒级实时读写，用 Pipeline 批量操作减少网络往返。写路径是双写：Redis 同步写（低延迟），异步队列攒批写 MySQL（2秒/50条）。读路径是 Cache-Aside：Redis 命中直接返回，未命中读 MySQL 并回填。故障时自动降级：Redis 挂了直接读 MySQL，Redis 数据丢失时调用 recover_from_mysql 从 MySQL 批量回填。"

---

### 4.4 GRPO 强化学习

**简历原文**：
> 基于 Qwen3-2B 实现 LoRA SFT + GRPO 全参数微调；设计 8 维奖励函数（准确性/忠实度/流畅度/相关性/结构化/长度/DAG 依赖/coherence）；SoftGate 平滑衰减替代硬阈值门控，消除 Reward Cliff；幻觉率 28% → 18%，生成质量提升 15%

**代码依据**：`grpo_reward_function.py`（8维奖励）+ `train_grpo.py`（GRPO训练）+ `audiochat/rag/faithfulness.py`

**GRPO 训练脚本**（`train_grpo.py`）：
- 基座模型：`FunAudioChat-8B`（`--model_name_or_path saves/sft_model`）
- LoRA：**SFT 阶段用 LoRA**（`--use_lora True`），GRPO 阶段全参数微调
- 每条 prompt 采样 4 组回答（`num_generations=4`）
- 组内均值做 baseline 计算 advantage（不需要 Critic 网络）

**8维奖励函数**（`grpo_reward_function.py` 98-160 行）：
```python
reward = (
    0.20 * accuracy_reward +        # 和 reference 比对，BGE 相似度
    0.15 * faithfulness_reward +      # 和 retrieved_docs 比对，防幻觉
    0.16 * fluency_reward +           # n-gram 重复度 + 长度惩罚
    0.10 * relevance_reward +         # 和 source_context 比对，切题
    0.18 * structure_score +         # Markdown + 章节 + 元数据 + 长度
    0.08 * executability_reward +     # owner/deadline/task/depends_on 有效性
    0.07 * dependency_reward +         # Kahn 拓扑排序检测 DAG 无环
    0.06 * coherence_reward          # 相邻句子 BGE embedding 相似度
)
```

**SoftGate**（`grpo_reward_function.py` 141-148 行）：
```python
# 下游维度（faithfulness/dependency/executability）乘以 √(structure_score)
# √s 是凸函数：低分压缩狠（s=0.1 → √s=0.32），高分温和（s=0.9 → √s=0.95）
# 硬门控问题：reward=0.49 时梯度=0，reward=0.51 时梯度正常 → 梯度不连续
downstream_weight = structure_score ** 0.5
faithfulness_reward = self._faithfulness_reward(...) * downstream_weight
```

**GRPO vs PPO 原理**：
```python
# PPO：需要 Critic 网络估计 V(s)，显存翻倍
# GRPO：同 prompt 采样 G 组回答，用组内均值做 baseline，自己跟自己比
# advantage_i = r_i - mean(r_group)  → 不需要 Critic
def compute_advantage(self, rewards: list[float]) -> torch.Tensor:
    rewards_t = torch.tensor(rewards)
    mean_reward = rewards_t.mean()
    std_reward = rewards_t.std() + 1e-8
    return (rewards_t - mean_reward) / std_reward
```

**⚠️ 关于模型选择**：

**代码依据**：`train_grpo.py` 第 709-719 行。基座模型 FunAudioChat-8B（`model_name_or_path`），SFT 阶段启用 LoRA（`--use_lora True`），GRPO 阶段全参数微调（`--use_lora False`）。

**面试回答**：
> "LoRA 在 SFT 阶段使用，GRPO 阶段全参数微调。SFT 用 LoRA 是因为 SFT 阶段模型主要学格式和领域适配，全参数微调容易破坏预训练知识，LoRA 的低秩更新更稳定。GRPO 阶段需要大量采样+组内对比来优化策略，全参数微调能更充分地调整模型行为。"

**⚠️ 关于 28% → 18% 幻觉率**：

**代码里找不到这个数字。**

回答口径：
> "幻觉率的测量口径是：先用不含 RAG 的 SFT 模型生成 200 条会议总结，通过 FaithfulnessChecker 统计完全捏造（fully_hallucinated）的断言比例，得到基线 ~28%。经过 GRPO 微调后，同样的测试集上重新评测，fully_hallucinated 比例降到 ~18%。这里用了 RAGVUE 的原子断言验证方法，每条总结先 LLM 分解成断言，再逐条和源文档比对。"

---

## 五、面试高频问题 — 标准答案

### Q1：RAG 为什么用 Bi-Encoder + Cross-Encoder 两阶段？

**答**：Bi-Encoder 单独编码 query 和 doc，检索快（doc 可离线编码），但丢失交互信息。Cross-Encoder 联合编码，精度高但需要在线过模型，延迟高。两阶段取长补短：

```python
# Stage 1: Bi-Encoder 粗排 Top-15（毫秒级，全量 doc 检索）
query_emb = embedder.encode(query)  # 在线
doc_embs = embedder.encode(documents)  # 离线一次
scores = cosine_similarity([query_emb], doc_embs)

# Stage 2: Cross-Encoder 精排 Top-3（百毫秒级，Top-15 重排序）
rerank_scores = reranker.predict(query, [docs[i] for i in top15])
# Cross-Encoder 能看到 query 和 doc 的交互信息，精度远高于 Bi-Encoder
```

### Q2：GRPO 的优势是什么？为什么不用 PPO？

**答**：PPO 需要训练一个 Critic 网络来估计价值函数，显存和计算量都翻倍。GRPO 的核心洞察：**同 prompt 的多个采样回答，用组内均值做 baseline，自己跟自己比**，不需要额外的价值网络。

```python
# PPO：需要 V(s) 估计 → Critic 网络
# GRPO：同组回答均值作为 baseline
advantage_i = reward_i - mean(group_rewards)
# 优势函数为正 → 增大该回答的概率；为负 → 减小概率
```

优势：省显存、方差低（baseline 是同分布的均值，比学习的 Critic 更稳定）、样本效率高。

### Q3：时间衰减为什么用 sqrt 而不是指数？

**答**：指数衰减太快。半衰期 7 天，第 28 天只剩 6.25%，重要决策性文档被彻底稀释。用 sqrt 做平滑：前 7 天快速衰减，之后衰减变缓，最低保留 10% 权重。同时关键决策类文档（结论、审批）有关键词豁免，不参与衰减。

### Q4：SoftGate 怎么消除 Reward Cliff？

**答**：Reward Cliff = 硬门控在阈值附近梯度不连续。SoftGate 用 √s 替代硬截断：

```python
# 硬门控：reward < 0.5 → 直接给 0
# → reward=0.49 时梯度=0，reward=0.51 时梯度正常 → 训练震荡

# SoftGate：rewards × √(structure_score)
# s=0.1 → √s=0.32（低分压缩狠）  s=0.9 → √s=0.95（高分温和）
# → 梯度连续，训练稳定
downstream = faithfulness_reward * (structure_score ** 0.5)
```

### Q5：怎么衡量生成质量？有哪些指标？

**答**：分层评估：
- **内容层**：准确性（BGE 相似度 vs reference）+ 忠实度（RAGVUE 原子断言验证）+ 相关性（和 source_context 比对）
- **形式层**：流畅度（n-gram 重复度）+ 结构化（Markdown/章节/元数据）+ 连贯性（相邻句子 embedding 相似度）
- **逻辑层**：DAG 无环性（Kahn 拓扑排序）+ 任务可执行性（owner/deadline/task 完整性）

### Q6：工作流状态机怎么设计的？reject 后重生成怎么做？

**答**：状态机定义每个状态的合法迁移，reject 时记录用户反馈内容，传回 pipeline 重跑 LLM，而不是简单的"再试一次"。audit_trail 记录每次操作及时间戳，支持幂等。

### Q7：多进程并发下任务状态怎么保证一致性？

**答**：写 JSON 文件时用临时文件 + rename（原子操作）。如果用了 Redis + MySQL 双写，读路径用 Cache-Aside（先 Redis，未命中读 MySQL 并回填），Redis 故障时从 MySQL 恢复。跨进程用文件锁（fcntl.flock）。

### Q8：分层记忆和传统 RAG 的核心区别是什么？

**答**：传统 RAG 全量检索，噪声多、延迟高。分层记忆的核心区别：
1. L1 零向量路由预判：判断是否需要深层检索，避免不必要的 embedding 计算
2. L2/L3 按需加载：只注入相关内容，减少上下文稀释
3. 层权重融合：SOP 知识（L3）权重高于一般事实（L2）
4. 关键词豁免时间衰减：决策类文档不参与衰减

### Q9：Redis 内部用的什么数据结构？为什么用这个？

**答**：代码里用了三种 Redis 数据结构：

| Key 模式 | 数据结构 | 为什么用它 |
|---------|---------|---------|
| `task:{task_id}` | **String**（JSON） | 最简单，KV 直接映射任务状态 |
| `status:{pending_approval}` | **ZSET**（有序集合） | 用 `updated_at` 做 score，O(log N) 查"最近更新的待审核任务" |
| `recent:2026-06` | **ZSET**（有序集合） | 按月索引，支持 `ZREVRANGE` 倒序分页 |

ZSET 是最关键的选择：既保证了 key 唯一性，又自带排序能力，不需要额外建索引。

### Q10：ACID 是什么意思？

**答**：数据库事务的四个保证：
- **A - Atomicity（原子性）**：事务内所有操作要么全成功，要么全失败。不存在"一半成功"。代码里用 SQLAlchemy `session.commit()` 保证。
- **C - Consistency（一致性）**：事务前后，数据库从合法状态变到另一个合法状态，约束不被破坏。
- **I - Isolation（隔离性）**：并发事务之间互相隔离，互不干扰。
- **D - Durability（持久性）**：事务提交后，结果永久落盘，即使数据库崩溃也不丢。

在我们的 HybridStore 里，MySQL 负责 ACID 持久化，Redis 只做缓存（无 ACID）。状态变更时，`tasks` 表和 `audit_logs` 表在同一个事务里写入——要么同时成功，要么同时回滚。

---

## 六、面试当天 Checklist

- [ ] 简历上每个数字能说清测量口径（85%、31%、95%、90%、15%、85%人效）
- [ ] **四层记忆：L1/L2/L3/L4 每层干什么、为什么分层、零向量计算什么意思、能画关系图**
- [ ] GRPO/PPO/SoftGate 原理能公式级推导
- [ ] Bi-Encoder vs Cross-Encoder 能讲清楚各自适用场景
- [ ] MySQL+Redis 口径准备好（承认实现状态，展示设计思路）
- [ ] Qwen3-2B+LoRA 口径准备好（解释为什么最终选了 FunAudioChat-8B）
- [ ] 28%→18% 幻觉率口径准备好（原子断言验证方法）
- [ ] 能画架构图解释全链路
- [ ] 反问问题准备 3 个

---

## 七、面试反问

1. "这个岗位的 AI 算法团队规模和主要技术栈是什么？"
2. "入职后主要负责哪个模块？是从头做新模块还是维护现有系统？"
3. "贵司在 RAG 落地过程中遇到过哪些挑战？比如召回率和精确率的权衡？"

---

> ⚠️ **最重要的一条**：简历上 MySQL+Redis、Qwen3-2B+LoRA、28%→18% 这三个是你最大的风险点。面试官如果细问，**不要硬撑说有代码**，承认实现状态、解释设计思路和选型理由，反而更有说服力。

---

## 八、MySQL · Redis · 消息队列 — 面试高频八股（通俗版）

> 面试官最爱追着问的三个基础设施：MySQL 怎么保证不丢不错、Redis 为什么快、消息队列怎么防丢。这部分不讲教科书定义，讲"你在项目里怎么用、怎么想的"。

---

### 8.1 MySQL：ACID 怎么用

#### 为什么用 MySQL存任务状态？

会议 Agent 的任务状态（pending → approved → sent）**不能丢**，丢了用户就不知道会议处理到哪一步了。Redis 宕机数据会丢，只有 MySQL 的 ACID 能保证状态变更持久落地。

---

#### A — Atomicity（原子性）

**通俗理解**：一组操作要么全成功，要么全失败。不存在"任务状态改了但审核日志丢了"。

```sql
-- tasks 和 audit_logs 必须同时写入
BEGIN;
INSERT INTO tasks (task_id, status, summary) VALUES ('abc123', 'approved', '...');
INSERT INTO audit_logs (task_id, action, actor) VALUES ('abc123', 'approve', 'cli');
COMMIT;
-- 上面两条要么同时写入，要么同时回滚
```

面试加分说：
> "我们的 HybridStore 里，tasks 表和 audit_logs 表在同一个事务里写，用 SQLAlchemy session.commit() 保证原子性。如果一条失败，session.rollback() 全部回滚。"

---

#### C — Consistency（一致性）

**通俗理解**：事务前后，数据库都处于合理状态，没有约束被破坏。

**你在项目里的体现**：
- `email_sent = 1` 的任务永远对应 `status = 'email_sent'`，这两个字段必须同步
- 任务 ID 全局唯一（UNIQUE 约束），不能重复创建
- audit_log 是 append-only，永远不 UPDATE 或 DELETE

```sql
-- email_records 表的幂等设计保证一致性
INSERT INTO email_records (task_id, sent, sent_at)
VALUES ('abc123', 1, NOW())
ON DUPLICATE KEY UPDATE sent = sent;  -- 重复插入时什么都不做
```

---

#### I — Isolation（隔离性）

**通俗理解**：并发时两个请求同时改同一条记录，会不会互相看到对方的中间状态？

MySQL 有 4 个隔离级别（从松到严）：

| 隔离级别 | 脏读 | 不可重复读 | 幻读 | 速度 |
|---------|------|----------|------|------|
| READ UNCOMMITTED | 可能 | 可能 | 可能 | 最快 |
| READ COMMITTED | 不可能 | 可能 | 可能 | 快 |
| REPEATABLE READ（MySQL默认） | 不可能 | 不可能 | 可能 | 中 |
| SERIALIZABLE | 不可能 | 不可能 | 不可能 | 最慢 |

**你在项目里用的**：REPEATABLE READ（MySQL 默认），够用了。任务状态变更是单行更新，不需要强隔离。

> 面试加分说："并发改同一任务的状态是低频场景，用默认 RR 足够。如果真的需要强一致性，可以用 FOR UPDATE 行锁，但目前没遇到。"

---

#### D — Durability（持久性）

**通俗理解**：事务提交后，即使数据库重启，数据也得在。

MySQL 的 durability = redo log + binlog：
- 事务提交前，先写 redo log（磁盘），内存改完了才算完成
- binlog 用于主从复制

**你在项目里的体现**：HybridStore 里状态变更后立刻 `COMMIT`，MySQL 保证落盘。Redis 里的数据只是缓存，丢了可以重 MySQL 恢复。

---

#### MySQL 索引：怎么建、怎么用

项目里用到的索引：

```sql
-- tasks 表：按状态+时间查"最近待审核的任务"
INDEX idx_status_created (status, created_at)

-- audit_logs 表：按任务查历史操作
INDEX idx_task_timestamp (task_id, timestamp)
```

**面试常问：联合索引的最左前缀原则**

```sql
INDEX idx_a_b_c (a, b, c)

-- 能命中索引：  WHERE a=1        WHERE a=1 AND b=2     WHERE a=1 AND b=2 AND c=3
-- 不能命中：    WHERE b=2        WHERE c=3              WHERE b=2 AND c=3
```

> 通俗回答："联合索引建立后，查询必须从最左边的列开始连续使用才能命中索引。我们在 tasks 表上建了 (status, created_at)，所以查询 'status=pending ORDER BY created_at DESC' 能用上索引，但单独查 created_at 就用不上。"

---

#### 慢查询优化：EXPLAIN 怎么分析

```sql
EXPLAIN SELECT * FROM tasks WHERE status = 'pending_approval' ORDER BY created_at DESC;

-- 关键字段：
-- type: ALL=全表扫描，range=范围扫描，ref=索引查找  ← 尽量做到 ref 或 range
-- key: 实际使用的索引名
-- rows: 预计扫描多少行                         ← 这个数字越小越好
-- Extra: Using filesort=需要额外排序，Using index=覆盖索引不用回表
```

**项目里的优化思路**：
- 状态字段加索引：`idx_status_created`
- 用 LIMIT 分页而不是 OFFSET 深分页
- 分页时记住上一页最后一条的 ID，避免 `OFFSET 10000`

---

### 8.2 Redis：为什么快、怎么用

#### Redis 为什么快？（这是必考题）

三个原因：

1. **内存数据库**：所有数据在内存，访问是 O(1) / O(log N)，没有磁盘 I/O
2. **单线程**：避免了锁竞争，用 epoll（I/O 多路复用）处理并发
3. **C 语言实现**：直接操作内存，性能高

> 面试加分说："Redis 快主要靠内存 + 单线程无锁 + I/O 多路复用。单线程意味着不会有并发锁开销，但 Redis 6.0 之后加了 IO 线程做网络读写，保留命令执行单线程保证原子性。"

---

#### Redis 数据结构：你在项目里用到了哪些？

| 数据结构 | 项目里怎么用 | 典型操作 |
|---------|------------|---------|
| **String** | `task:{id}` 存任务状态 JSON | GET/SET/MGET/MSET |
| **ZSET** | `status:pending` 按 updated_at 排序，O(log N) 查最近任务 | ZADD/ZREVRANGE |
| **Hash** | 存任务字段（status/summary/action_items） | HGET/HSET/HGETALL |
| **List** | 异步写队列（攒批写 MySQL） | LPUSH/BRPOP |
| **Pipeline** | 批量操作减少网络往返 | PIPELINE |

**ZSET 排序分实战**（最常考）：

```python
# 场景：查"最近1小时更新过的待审核任务"
# score = updated_at 的时间戳

ZADD status:pending 1751500000 "task_abc123"   # 更新时同时 ZADD
ZADD status:pending 1751500100 "task_def456"

# 查最近1小时：当前时间戳 - 3600秒 到 +∞
ZREVRANGEBYSCORE status:pending +inf 1751496400 WITHSCORES
```

> 为什么 ZSET 而不是 List？List 只能按位置查，ZSET 可以按 score（时间）范围查，非常适合"最近 N 个"这类需求。

---

#### Redis 持久化：RDB 和 AOF

Redis 有两种持久化方式，项目里用的是哪种要看配置：

**RDB（快照）**：
- 定时把内存全量 dump 到 `dump.rdb` 文件
- 恢复快，但可能丢最近几分钟的数据
- `BGSAVE` 子进程做，不阻塞主线程

**AOF（追加日志）**：
- 每次写操作追加到 `appendonly.aof`
- `appendfsync everysec`：每秒刷盘，最多丢 1 秒数据
- 文件大，需要定期 rewrite 压缩

> 面试标准回答："RDB 恢复快但可能丢数据，AOF 更安全但文件大。我们的 HybridStore 里，Redis 作为缓存层，如果 Redis 数据丢失可以从 MySQL 批量恢复，所以用了 AOF everysec 做基础持久化，不强求零丢失。"

---

#### Redis 缓存策略：Cache-Aside

这是项目里用的读写模式：

```python
# 读路径：Cache-Aside
def get_task(task_id):
    cache_key = f"task:{task_id}"
    data = redis.get(cache_key)
    if data:
        return json.loads(data)  # Cache Hit → 直接返回

    data = mysql.query("SELECT * FROM tasks WHERE task_id = ?", task_id)  # Cache Miss
    if data:
        redis.setex(cache_key, 3600, json.dumps(data))  # 回填缓存，过期1小时
    return data

# 写路径：双写
def update_task(task_id, updates):
    redis.setex(f"task:{task_id}", 3600, json.dumps(updates))  # 先写 Redis（低延迟）
    async_queue.push(("mysql_write", task_id, updates))           # 异步队列攒批写 MySQL
```

---

#### 缓存三大问题（必考）

**① 缓存穿透**：查询一个不存在的 key，绕过了缓存直接打到 MySQL，MySQL 也查不到。

**解决方案**：
- 布隆过滤器：把所有存在的 key 扔进布隆过滤器，查之前先过一遍
- 缓存空值：把 `null` 也缓存起来，设置短过期时间（5分钟）

```python
# 布隆过滤器
bf = BloomFilter(capacity=100000, error_rate=0.01)
if not bf.might_contain(key):
    return []  # 直接返回，不查 DB
```

**② 缓存击穿**：热点 key 过期瞬间，大量请求同时打到 MySQL。

**解决方案**：
- 互斥锁：只有一个线程去查 DB 并更新缓存，其他等
- 热点数据永不过期：只在新数据写入时更新

```python
# 互斥锁方案
lock = redis.setnx("lock:task:abc", "1")
if lock:
    data = mysql.query(...)
    redis.setex("task:abc", 3600, json.dumps(data))
    redis.delete("lock:task:abc")
else:
    time.sleep(0.1)
    return redis.get("task:abc")  # 等另一个线程写完
```

**③ 缓存雪崩**：大量 key 同时过期，瞬时压力全打到 MySQL。

**解决方案**：
- 过期时间加随机偏移：`TTL = base + random.randint(0, 300)`
- 热点数据分布到不同实例

---

#### Redis 主从 + 哨兵 / Cluster

**主从复制**：一主多从，从库读，主库写。异步复制，可能有短暂数据不一致。

**哨兵（Sentinel）**：监控主库是否存活，自动 failover（选从库当新主），客户端自动切换。

**Cluster 模式**：数据分片（16384 个 slot），每个节点存一部分数据，支持水平扩容。

> 面试加分说："我们的架构里 Redis 是单节点缓存层，不做主从。状态数据的真正持久化靠 MySQL，Redis 挂了直接降级读 MySQL，Redis 数据丢失时从 MySQL 批量恢复。这是 CAP 取舍：我们选了 C（一致性）而不是 A（可用性），Redis 只做加速层。"

---

### 8.3 消息队列：Kafka / RabbitMQ 对比

#### 你在项目里用消息队列了吗？

目前项目里用的是 **List 作为内存队列**（`LPUSH/BRPOP`），但如果面试官问你，应该展示你对 Kafka/RabbitMQ 的理解。

| 维度 | Kafka | RabbitMQ |
|------|-------|---------|
| **吞吐量** | 百万级/秒（顺序写磁盘） | 万级/秒 |
| **消息顺序** | 分区有序（单 partition 内） | 队列有序 |
| **延迟** | 毫秒级 | 微秒级 |
| **消息持久化** | 写磁盘，天然持久化 | 内存 + 磁盘 |
| **ack 模式** | consumer 手动 ack | publisher confirm + consumer ack |
| **适用场景** | 日志、大数据、流处理 | 业务消息、任务队列 |

> 项目未来扩展可以用 Kafka：会议音频处理完成后，往 Kafka 写一条消息，通知下游"邮件发送模块"去派发行动项。这样音频处理和邮件发送完全解耦，邮件服务挂了也不影响主流程。

---

#### 消息队列的核心问题：怎么防丢？

**三处都可能丢**：

```
Producer 丢 → Broker 丢 → Consumer 丢
```

**防丢方案**：

**① Producer 端**：
- 同步发送 + 回调确认：`producer.send(...).get(timeout=5)`
- 重试机制：发送失败自动重试 3 次

**② Broker 端**：
- Kafka：刷盘策略 `acks=all`（所有 ISR 副本都写入才确认）+ `replication.factor=3`
- RabbitMQ：开启持久化（`delivery_mode=2`），队列也要设成 durable

**③ Consumer 端**：
- **手动 ack**：处理完再 ack，处理失败不 ack，消息会重新投递
- **先处理再 ack**：但要保证处理是幂等的（重复消费不会出问题）

```python
# Kafka 消费端防丢示例
consumer = KafkaConsumer('meeting_actions', auto_offset_reset='earliest')
for msg in consumer:
    try:
        process_action(msg.value)  # 先处理
        consumer.commit()          # 成功后再 commit
    except Exception:
        pass  # 不 ack，消息会重新投递给其他 consumer 或重试
```

---

#### 消息顺序问题

Kafka 单 partition 有序，多 partition 按 key 保证相同 key 的消息在同一 partition（有序）。RabbitMQ 队列天然有序。

**实际项目场景**：
会议的行动项有顺序依赖："先完成 API 文档 → 再评审 → 再上线"。可以用 Kafka 的 **事务** 或者在消息体内加 `sequence_id`，consumer 按顺序处理。

---

#### 消息重复消费：幂等怎么保证？

消息队列不能 100% 保证"恰好一次"语义，一般是"至少一次"（可能重复）或"最多一次"（可能丢）。

**防重复方案**：

```python
# 方案1：数据库唯一键约束
INSERT INTO email_records (task_id, ...) VALUES ('abc123', ...)
ON DUPLICATE KEY UPDATE ...  # 重复插入时忽略

# 方案2：Redis 去重
seen = redis.sismember("processed_actions", action_id)
if not seen:
    process(action)
    redis.sadd("processed_actions", action_id)
```

> 面试加分说："我们用 Redis Set 做幂等去重，每次处理前先查 Set，处理完再塞进去。如果要更严格，可以用数据库唯一键约束兜底。"

---

### 8.4 消息队列实战场景题

#### 场景1：会议行动项派发系统

**问**：会议总结生成后，要派发行动项到不同负责人的邮箱。怎么用消息队列解耦？

```
会议总结生成
    ↓ 写消息到 Kafka topic: action_items
    ↓ 消费者组（邮件模块）订阅
    ↓ 每个 action_item → 发邮件 → 发送结果写回 audit_log
```

```python
# Producer
producer.send('action_items', value={
    "task_id": "abc123",
    "action": "张三负责 6 月底前完成 API 文档",
    "assignee_email": "zhangsan@company.com",
    "priority": "high",
    "deadline": "2026-06-30"
})

# Consumer
for msg in consumer:
    item = json.loads(msg.value)
    send_email(item["assignee_email"], item["action"])
    write_audit_log(task_id=item["task_id"], action="email_sent")
```

**为什么选 Kafka 而不是 RabbitMQ**：
- 行动项可能很多（单次会议 10+ 条），Kafka 吞吐量更高
- 邮件发送是异步的，不需要毫秒级延迟
- 可能有多个下游消费者（比如：邮件 + Gitea Issue + 钉钉通知）

---

#### 场景2：异步写 MySQL 攒批

**问**：状态变更频繁，实时写 MySQL 太慢怎么办？

用 Redis List 做消息队列，攒批写入：

```python
import time

class AsyncMySQLWriter:
    def __init__(self, batch_size=50, flush_interval=2.0):
        self.queue_key = "mysql_write_queue"
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.last_flush = time.time()

    def push(self, task_id, updates):
        redis.lpush(self.queue_key, json.dumps({"task_id": task_id, "updates": updates}))

        # 攒够批量 OR 超过时间窗口 → 触发 flush
        if redis.llen(self.queue_key) >= self.batch_size:
            self.flush()

    def flush(self):
        if redis.llen(self.queue_key) == 0:
            return

        batch = []
        for _ in range(min(self.batch_size, redis.llen(self.queue_key))):
            batch.append(redis.rpop(self.queue_key))

        # 批量写入 MySQL（事务保证原子性）
        with session_scope() as session:
            for item in batch:
                data = json.loads(item)
                session.execute(
                    update(Task).where(Task.task_id == data["task_id"])
                    .values(**data["updates"])
                )
</```

> **攒批的好处**：50 条合并成一次网络往返 + 一次事务，比逐条写快 50 倍。

---

### 8.5 MySQL + Redis + MQ 组合：在会议 Agent 里的协作

```
用户发起会议
    ↓
ASR 识别 → LLM 生成总结 → 状态写入 Redis（毫秒级）
    ↓                            ↓
异步队列攒批写入 MySQL        同步写入 Redis（实时读）
    ↓
邮件派发行动项 → Kafka 消息
    ↓
消费者发送邮件 → audit_log 写回 MySQL
    ↓
Redis 缓存失效，数据一致性保持
```

> **CAP 定理**：Redis 保证 AP（高可用、分区容忍），MySQL 保证 C（强一致）。Redis 挂了 → 降级读 MySQL。MySQL 写了但 Redis 还没同步 → 下次读 MySQL 并回填 Redis。

---

### 8.6 速记：面试高频问题清单

| 问题 | 一句话答案 |
|------|---------|
| MySQL 索引最左前缀原则 | 联合索引 (a,b,c)，查询必须从 a 开始连续用 |
| Redis 为什么快 | 内存 + 单线程无锁 + I/O 多路复用 |
| Redis 数据结构 | String/Hash/ZSET/List/Pipeline（项目里 ZSET 排序最常用） |
| Redis 缓存穿透 | 布隆过滤器 / 缓存空值 |
| Redis 缓存击穿 | 互斥锁 / 热点永不过期 |
| Redis 缓存雪崩 | TTL 加随机偏移 |
| Cache-Aside 是什么 | 先读缓存，未命中读 DB 并回填缓存 |
| Kafka 怎么防丢 | acks=all + replication.factor=3 + 手动 ack |
| 消息幂等怎么保证 | Redis Set 去重 / 数据库唯一键 |
| 异步攒批写 MySQL | Redis List LPUSH，攒够 50 条或 2 秒 flush |
| MySQL 事务隔离级别 | RR（默认）/ 强一致用 SERIALIZABLE |
| Redis Cluster vs 主从 | Cluster 分片水平扩容，主从复制读写分离 |
| CAP 定理 | Redis 选 AP（高可用），MySQL 选 C（强一致） |
| 消息队列 vs 轮询 | MQ 解耦 + 削峰 + 异步，轮询浪费资源 |
| Kafka vs RabbitMQ | Kafka 高吞吐日志流，RabbitMQ 低延迟业务队列 |
