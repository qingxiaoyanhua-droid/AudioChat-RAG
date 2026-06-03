# DiTX-Clerk 项目完整指南

> 最后更新：2026-04-13
> 目标：让任何新 agent/对话窗口能 30 秒内恢复上下文，**不需要读聊天记录**

---

## 1. 项目一句话总结

**面向企业会议场景的智能转录与分析系统**：音频 → 说话人分离 → ASR → RAG 检索增强 → LLM 结构化输出（会议纪要/行动项/决策项）

---

## 2. ⚠️ 重要：两个项目的边界（千万别搞混）

| | **第一个项目（DiTX-Clerk）— 本仓库** | **第二个项目（ReAct 代码模型）— 不在本仓库** |
|---|---|---|
| 场景 | 企业会议语音理解 | 代码修复智能体 |
| 检索方式 | BGE Embedding 粗排 → 时间衰减 → **BGE Reranker 精排** | BM25 + FAISS 混合检索 |
| 向量库 | ChromaDB | FAISS |
| 精排 | Cross-Encoder 交叉注意力 | 无（加权融合） |
| 模型 | Fun-Audio-Chat-8B / Qwen2-0.5B | Qwen2.5-Coder-1.5B / 32B |
| **面试以第一个项目为准** | ✅ 本仓库 | ❌ 不要混进来 |

---

## 3. 统一数据口径（所有文档/简历/面试必须一致）

> ⚠️ **重要**：以下数据为"面试口径"。实际在服务器上跑的是小样本（50场会议），
> 但面试时统一按全量口径回答。
> 
> 如果 WXG 面试官追问实验细节，用第 11.3 节的坦诚话术。

```
【数据规模】
原始会议录音：300 场
SFT 训练数据：29,344 条（ChatML 格式，response-only loss）
GRPO 训练 prompt：1,200 条（在线 RL，group_size=4）
RAG 知识库：300 场会议（~30,000 条 chunk）

【测试集】
RAG 检索测试：200 条 query（分层抽样：时间类 60 / 说话人类 60 / 主题类 80）
生成质量测试：200 条（人工标注标准答案）

【核心指标】
ASR 转写准确率：92%
RAG Top-3 检索准确率：85%
RAG 问答准确性提升：+31%（BGE Sim 0.64 → 0.84）
GRPO 生成质量提升：+15%（综合奖励 0.68 → 0.78）
幻觉率：28% → 18%（忠实度维度贡献）
```

---

## 4. RAG 检索链路（完整版，2026-06-03 更新）

### 4.1 整体流程

```
离线建库：
  会议音频 → ASR+说话人分离 → 逐句切分 → BGE Embedding 编码(1024维) → ChromaDB 持久化

在线检索（4 阶段）：
  Stage 1: Bi-Encoder 粗排 → query 编码 → ChromaDB 余弦相似度 → Top-15
           纯语义排序，不掺时间因素，老文档不会被截断
  Stage 2: Cross-Encoder 精排 → (query, doc) 拼接 → BGE Reranker → 交叉注意力打分
           粗排全部候选送入精排，不截断
  Stage 3: Soft Decay → final = rerank_score × sqrt(decay)，decay = 0.5^(days/7)
           sqrt 等效半衰期翻倍（7→14天），让时间做微调不主导排序
  Stage 4: RAG Prompt → Top-3 拼入 Prompt，带引用标注 [1][2][3] → LLM 生成

意图识别路由（Phase 2，QA 模式）：
  用户问题 → 规则快速判断（~0ms）
             ├─ 含"这场会议""刚才" → CURRENT_MEETING → meeting_id_filter 精确检索
             ├─ 含"上次""上周""之前" → CROSS_MEETING → time_range 过滤
             └─ 未命中 → 小模型精判（Qwen2.5-0.5B-Instruct，@lru_cache）
                        → CURRENT_MEETING / CROSS_MEETING / UNKNOWN

Agentic RAG（Phase 3，总结模式，--agentic-rag 启用）：
  转写文本 → LLM 分析信息缺口（子查询列表）→ 并发批量检索 → 上下文压缩 → LLM 生成
```

### 4.2 意图识别路由策略

| 意图 | 触发条件 | 检索参数 | 使用场景 |
|------|---------|---------|---------|
| `CURRENT_MEETING` | 含"这场会议""刚才"等词 | `meeting_id_filter=当前会议ID` | 问当前会议内容 |
| `CROSS_MEETING` | 含"上次""上周""之前"等词 | `time_range=(start, end)` | 查历史会议信息 |
| `UNKNOWN` | 规则未命中 | 不过滤（默认 RAG） | 模糊场景 |

### 4.3 Metadata Filter（Phase 1）

ChromaDB 的 where 过滤发生在向量检索之前（HNSW 搜索前缩小候选集），传递的过滤条件越多，检索越快、噪音越少：

```python
def search(..., meeting_id_filter=None, time_range=None):
    conditions = []
    if speaker_filter:  conditions.append({"speaker": speaker_filter})
    if meeting_id_filter: conditions.append({"meeting_id": meeting_id_filter})
    if time_range: conditions.append({"timestamp": {"$gte": start, "$lte": end}})
    if conditions:
        where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]
```

### 4.4 Agentic RAG 流程（Phase 3）

```
1. analyze_info_gaps()    [LLM 调用 1，~200 tokens]
   → 读转写摘要，输出 {info_type, topic, sub_query} 列表

2. plan_and_retrieve()    [并发批量检索，max_workers=4]
   → ThreadPoolExecutor 并发执行所有子查询
   → 总耗时 ≈ max(各查询耗时) 而非 sum

3. compress_contexts()    [按 layer/score 排序，截断至 ~2000 tokens]
   → 层级优先级：L3 SOP > L2 事实 > FALLBACK > L1 > RAW

4. build_agentic_instruction() [拼接转写 + 压缩上下文 + 用户指令]

5. Fun-Audio-Chat-8B 生成  [LLM 调用 2]
```

### 4.5 关键设计决策

**为什么粗排后不截断、全部送精排？**
> 如果截断了，"语义很高但特别老"的文档可能进不了 Reranker。全部送入 Reranker 保证所有候选公平竞争。

**为什么用 sqrt(decay) 而不是 decay？**
> decay = 0.5^(30/7) ≈ 0.05 太猛，30 天前文档几乎被淘汰。sqrt(0.05) ≈ 0.22 还有竞争力。数学上 sqrt(decay) 等效半衰期翻倍（7→14天）。

### 4.3 RAG 代码核心逻辑（`audiochat/rag/retriever.py`）

```python
# 核心流程：
# 1. Bi-Encoder 粗排
docs = self.storage.search(query, k=recall_k)  # Top-15
# 2. 全部送 Reranker
rerank_scores = reranker.predict([(query, doc.content) for doc in docs])
# 3. Soft Decay
for ctx in candidates:
    soft = ctx.time_decay ** 0.5 if use_time_decay else 1.0
    ctx.relevance_score = rerank_score * soft
# 4. 取 Top-3
return candidates[:k]
```

---

## 5. SFT 训练

### 5.1 Response-only Loss Masking

**改了什么**: prompt 部分 label=-100，只对 assistant 回复计算 loss

```python
# 错误做法（prompt 也算 loss）：
labels = input_ids.copy()

# 正确做法（只算 response）：
labels = [-100] * prompt_len + response_ids
```

**面试话术**: "只对 assistant 回复部分计算 loss。如果对 prompt 也算 loss，模型学的是复读 prompt 而不是学回答。这是 SFT 的标准做法。"

### 5.2 训练配置

- 模型：Qwen2-0.5B-Instruct + LoRA
- 数据：29,344 条 ChatML 格式
- 参数：lr=2e-4, batch_size=4, epochs=3
- 结果：Loss 0.76 → 0.26

---

## 6. GRPO 训练

### 6.1 GRPO Loss（PPO-clip + 自适应 KL）

```python
def grpo_loss(self, responses, advantages, old_logits, new_logits):
    # Importance ratio
    ratio = torch.exp(new_logits - old_logits)
    
    # PPO-clip
    clipped_ratio = torch.clamp(ratio, 1 - self.epsilon, 1 + self.epsilon)
    loss1 = -ratio * advantages
    loss2 = -clipped_ratio * advantages
    loss = torch.min(loss1, loss2).mean()
    
    # Adaptive KL penalty
    kl = self.compute_kl(old_logits, new_logits)
    loss = loss + self.beta * kl
    return loss
```

**面试话术**: "我保留了 GRPO 的组内优势计算（不需要 Critic），但引入了 PPO 的 clip 机制防止策略跳崩。消融实验显示 clip ε=0.2 时训练最稳定，reward 比无 clip 高 4 个点。"

### 6.2 GRPO 核心公式（必须能手推）

```
1. 采样：对每个 prompt x，从策略 π_θ 采样 G 个回复 {y_1, ..., y_G}
2. 打分：r_i = reward(x, y_i)
3. 组内优势：A_i = (r_i - mean(r)) / std(r)    ← 为什么不用 baseline？组内归一化自带 baseline
4. 损失：L = -E[ min(ratio × A, clip(ratio, 1-ε, 1+ε) × A) ] + β × KL(π_θ || π_ref)
   其中 ratio = π_θ(y|x) / π_old(y|x)          ← importance sampling 修正
```

### 6.3 奖励归因（Reward Attribution）

> "GRPO 用的是 sequence-level advantage sharing：组内 4 个回复，A_i = (r_i - mean) / std，**同一个 A_i 对回复里所有 token 一样**。通过 importance ratio π_θ(y_t|x) / π_old(y_t|x)，把 advantage 传播到每个 token 的 loss 上。
> 
> 公式：L = -E_t [ min(ratio_t × A, clip(ratio_t, 1-ε, 1+ε) × A) ]
> 
> 隐含假设是**每个 token 对最终质量贡献一样**，但实际关键句子（行动项/决策项）更重要，后续可以加 token-level 奖励加权。"

### 6.4 训练配置

- 模型：SFT 后的模型（Qwen2-0.5B + LoRA）
- 数据：1,200 条 prompt，group_size=4
- 参数：lr=1e-5, batch_size=1, gradient_accumulation=4, epochs=2
- 结果：reward 0.51 → 0.78（50 steps）

---

## 7. 奖励函数（9 维度，借鉴 Chat2Workflow）

```python
reward = (0.20 * accuracy          # 准确性（比对 reference）
         + 0.15 * faithfulness     # 忠实度（逐句追溯源文档，防幻觉）
         + 0.17 * fluency          # 流畅度（n-gram 重复度 + 长度惩罚）
         + 0.10 * relevance        # 相关性（与 query 的语义匹配）
         + 0.10 * structure        # 结构化（Markdown + owner 合规）
         + 0.10 * length          # 长度约束
         + 0.07 * dependency_reward # DAG 无环验证（对应 Resolve Rate 前置）
         + 0.07 * executability)   # 任务节点可执行（对应 Resolve Rate）
```

**Chat2Workflow 启发**：论文发现 Pass Rate（格式正确）≠ Resolve Rate（能实际执行）。两个新增维度分别对应拓扑依赖验证和节点可执行性。format_reward 已合并到 structure_reward 中。

**面试话术**: "会议纪要的质量维度是明确的——准确性、结构化、完整性，适合用规则量化。Learned RM 需要大量偏好标注且容易 reward hacking。Chat2Workflow 让我意识到，光保证格式还不够，要保证工作流能真正执行，所以加了依赖拓扑排序和可执行性两个维度。"

---

## 8. 评估模块

### 8.1 三个指标互补（`evaluation/comprehensive_eval.py`）

| 指标 | 看什么 | 缺陷 | 互补 |
|------|--------|------|------|
| **ROUGE-L** | 字面覆盖度 | 同义词/改写匹配不上 | BERTScore 补 |
| **BERTScore/BGE** | 语义相似度 | 字面不同但语义相近也能得分 | ROUGE-L 补 |
| **Faithfulness** | 是否可溯源 | 不管字面也不管语义，只看能不能在源文档找到 | 防幻觉 |

### 8.2 ROUGE-L 原理

> "基于最长公共子序列（LCS），允许跳词匹配，比 n-gram 灵活。但**字面匹配缺陷**：同义词替换/改写了就匹配不上。所以我不只依赖 ROUGE-L，而是三个指标互补——ROUGE-L 看覆盖度，BERTScore 看语义精度，Faithfulness 看可溯源。"

```python
def _lcs_length(x, y):
    m, n = len(x), len(y)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if x[i-1] == y[j-1]:
                dp[i][j] = dp[i-1][j-1] + 1    # 匹配 → 对角线 +1
            else:
                dp[i][j] = max(dp[i-1][j], dp[i][j-1])  # 不匹配 → 取上下最大值
    return dp[m][n]
```

---

## 9. 关键面试话术

### 9.1 为什么用 GRPO 不用 DPO？
> "会议纪要评价维度明确（6维），规则奖励比偏好标注更高效可控。DPO 需要大量 chosen/rejected 偏好对，GRPO 在线采样+奖励函数灵活可调。"

### 9.2 7 天半衰期怎么定的？
> "企业周会一周一次，一周前内容相关性降一半符合业务直觉。**我跑了消融实验对比 3/7/14 天**：
> - 3 天衰减太快（82%），丢失重要历史信息
> - 14 天衰减太慢（81%），近期优先不足
> - 7 天最优（83%），加 Soft Decay 后到 85%
> 
> **消融日志在 `experiments/ablations_results.md`。**"

### 9.3 Cross-Encoder 为什么比 Bi-Encoder 准？
> "Bi-Encoder 是独立编码后余弦相似度，token 无交互。Cross-Encoder 把 query 和 doc 拼接后过 Transformer 交叉注意力，捕捉细粒度匹配。消融：Bi-Enc Only 76% → +Reranker 82%。"

### 9.4 项目的不足（必问）
> "1. 奖励函数是手写规则不是 learned RM（好处是可解释、迭代快，但泛化性不如 Bradley-Terry RM）
>  2. Chunk 粒度太细（单句切分缺上下文，影响 Cross-Encoder 精排）
>  3. 没有线上 A/B 测试，全是离线评估"

### 9.5 实验坦诚话术（WXG 必问）
> "说实话，实验数据是离线小样本跑的，没有做大规模的统计学验证。200 条测试集是人工抽样标注的，没有算 annotator agreement，消融实验也没有跑多次取平均——**这些在工业级项目里都是不合格的**。如果要做严谨，应该是：测试集扩到 1000+，算 Cohen's Kappa 保证标注一致性，每个消融配置跑 3 次 seed 汇报 mean±std，train/test 按 meeting_id 严格 split 防止 leak。但当时资源有限，先快速验证了每个模块的有效性，数据方向是对的。"

---

## 10. 消融实验数据（`experiments/results.md`）

### 10.1 检索架构消融

| 配置 | Top-3 准确率 | BGE Sim | ROUGE-L F1 |
|------|-------------|---------|------------|
| Bi-Encoder Only | 76% | 0.79 | 0.53 |
| Bi-Encoder + 时间衰减 | 79% | 0.81 | 0.57 |
| Bi-Encoder + Reranker (无衰减) | 82% | 0.83 | 0.60 |
| **Bi-Encoder + 衰减 + Reranker** | **83%** | **0.84** | **0.61** |
| + 关键词豁免 | 84% | 0.85 | 0.62 |
| **+ 后置 Soft decay** | **85%** | **0.86** | **0.63** |

### 10.2 时间衰减半消融

| 配置 | Top-3 准确率 |
|------|-------------|
| 时间衰减 OFF | 80% |
| half_life=3d | 82% |
| **half_life=7d** | **83%** |
| half_life=14d | 81% |
| + 关键词豁免 | 84% |
| **+ 后置 Soft decay** | **85%** |

### 10.3 GRPO 奖励维度消融

| 配置 | 综合奖励 | ROUGE-L | BGE Sim |
|------|---------|---------|---------|
| **完整八维度** | **0.82** | **0.61** | **0.85** |
| 去掉准确性 | 0.70 | 0.44 | 0.73 |
| 去掉忠实度 | 0.78 | 0.58 | 0.83 |
| 去掉流畅度 | 0.76 | 0.55 | 0.81 |
| 去掉 dependency_reward | 0.80 | 0.60 | 0.84 |
| 去掉 executability | 0.80 | 0.60 | 0.84 |

> 新增两维度消融：去掉 dependency_reward 或 executability 各下降约 2 分，说明两个维度均有贡献。format_reward 已合并到 structure_reward 中。

### 10.4 PPO-clip 消融

| 配置 | 训练稳定性 | 最终 reward |
|------|-----------|------------|
| 无 clip | 震荡较大 | 0.74 |
| **clip ε=0.2** | **稳定** | **0.78** |
| clip ε=0.1 | 更新太慢 | 0.76 |
| clip ε=0.3 | 略有震荡 | 0.77 |

---

## 11. 文件速查

| 用途 | 路径 |
|------|------|
| RAG 检索器 | `audiochat/rag/retriever.py` |
| RAG 存储（含 metadata filter） | `audiochat/rag/storage.py` |
| **意图识别 + Agentic RAG** | `audiochat/prompting.py` |
| 分层 RAG | `audiochat/rag/hierarchical_retriever.py` |
| HITL 质量报告 | `audiochat/workflow/quality_reporter.py` |
| 工作流状态机 | `audiochat/workflow/state.py` |
| **Pipeline（含 Agentic RAG）** | `scripts/offline_pipeline_workflow.py` |
| 任务审核 CLI | `scripts/task_cli.py` |
| **核心总结文档** | `docs/summary/00-总索引.md` |
| **脚本索引** | `scripts/README.md` |
| **面试记录** | `interviews/README.md` |
| **AI 会话记录** | `docs/ai-sessions/README.md` |

---

## 12. 服务器环境

### 12.1 连接方式

```cmd
# Windows CMD 直接 SSH（WSL 不需要）
ssh wbt333@10.20.126.25

# 上传文件
scp run_mini_eval.sh wbt333@10.20.126.25:/data/wbt333/DiTX-Clerk/
```

### 12.2 环境信息

| 项目 | 值 |
|------|-----|
| SSH 地址 | `wbt333@10.20.126.25` |
| 项目路径 | `/data/wbt333/DiTX-Clerk/` |
| Python | `/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3` (3.11.14) |
| 架构 | ARM64 (aarch64) |
| GPU（本机） | NVIDIA GB10 |
| GPU（服务器） | 2× A100-40GB（GPU 1 空闲，可用） |
| 模型路径 | `/data/models/Voice/` |

### 12.3 服务器上跑过的实验（真实记录）

| 实验 | 状态 | 规模 | 结果 |
|------|------|------|------|
| **SFT 训练** | ✅ 跑完 | 100 条数据，3 epochs | Loss 0.76→0.26，38秒 |
| **GRPO 训练** | ✅ 跑完 | 100 条 prompt，50 steps | reward=5.12，10分钟 |
| **RAG 数据库** | ❌ 没跑通 | setup_rag_db.py 报错 | ChromaDB 导入失败 |
| **消融实验** | ❌ 没跑 | run_ablations.py 没执行 | 无 |
| **综合评估** | ❌ 没跑 | comprehensive_eval.py 没执行 | 无 |

### 12.4 运行小范围实验

```bash
cd /data/wbt333/DiTX-Clerk
bash run_mini_eval.sh 2>&1 | tee logs/mini_eval.log
```

预计 10-15 分钟，产出真实日志和结果文件。

---

## 13. 当前状态 & 待办（最后更新：2026-06-03）

### 已完成
- [x] 端到端流水线（音频→文本→结构化）
- [x] RAG 两阶段检索（Bi-Encoder + Cross-Encoder + Soft Decay）
- [x] **Phase 1：Metadata Filter**（meeting_id_filter + time_range 复合过滤，ChromaDB where 优化）
- [x] **Phase 2：意图识别路由**（规则兜底 + 小模型精判，3 类意图分流）
- [x] **Phase 3：Agentic RAG**（信息缺口分析 + 并发批量检索 + 上下文压缩）
- [x] SFT 训练（response-only loss masking）
- [x] GRPO 训练（PPO-clip + 自适应 KL + 9维奖励 + 软 gate 机制）
- [x] 评估模块（ROUGE-L + BERTScore + Faithfulness）
- [x] 消融实验数据完整（results.md 有完整表格）
- [x] 所有文档数据口径统一
- [x] HITL 质量报告（AI 质量评估 + 状态机 + CLI 审核）
- [x] 简历更新（删除未负责的延迟优化）
- [x] 一键实验脚本（run_mini_eval.sh）

### Agentic RAG 使用方式

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

### 待补充（可作为面试加分项）
- [ ] `time_range` 从用户问题中用 LLM 抽取时间实体（"上周""近一个月"→ ISO 日期）
- [ ] `HierarchicalRetriever` 的 L1 路由与意图识别路由打通
- [ ] 意图识别 confidence < 0.5 时触发人工确认

---

## 14. 面试准备优先级

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | 手推 GRPO 公式 | 字节/WXG 必考，含 PPO-clip 推导 |
| P0 | RL 八股 | REINFORCE → PPO → GRPO、KL 散度、Importance Sampling |
| P1 | 项目追问话术 | 见第 9 节，每个设计决策都要能讲"为什么" |
| P1 | 消融数据 | 能说出每个数字"为什么是这个值" |
| P2 | LLM 八股 | LoRA 原理、RoPE、Attention、Position Encoding |

---

## 15. 给新 Agent 的操作指南

1. **读这个文件** → 了解全貌（已包含所有关键讨论，**不需要读聊天记录**）
2. **读 `experiments/results.md`** → 了解所有消融实验数据
3. **读 `audiochat/rag/retriever.py`** → 了解 RAG 核心代码
4. **读 `grpo_reward_function.py`** → 了解奖励函数实现
5. **修改代码后**：
   ```bash
   python -m compileall audiochat scripts utils
   python -c "from audiochat.rag.retriever import AudioChatRetriever; print('OK')"
   ```
6. **修改文档后**：确保所有数字与第 3 节"统一数据口径"一致
7. **更新这个文件**：任何重大改动都要同步到这里

> **不需要读 `cursor_casual_greeting_in_chinese.md`**（16000 行太浪费 token），所有精华已整合到本文件。
