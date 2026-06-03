# DiTX-Clerk 项目工作表

> 维护人：王柄涛
> 创建时间：2026-05-02
> 用途：记录项目技术决策、待办事项、已完成优化、面试相关知识点追踪

---

## 一、项目概述

### 核心链路

```
会议音频
  ↓
Fun-Audio-Chat-8B（ASR 语音转文字，转写准确率 92%）
  ↓
Qwen2.5-0.5B-Instruct（SFT + GRPO 微调）
  ↓
RAG 检索（ChromaDB + BGE 两阶段）
  ↓
输出结构化会议纪要 + 行动项 + 决策项 + 任务派发（Gitea API）
```

### 两个模型职责区分（面试重点）

| 模型 | 职责 | 是否微调 |
|------|------|---------|
| Fun-Audio-Chat-8B | ASR 语音转文字 + 说话人分离 | **否**，冻结 |
| Qwen2.5-0.5B-Instruct | SFT + GRPO 微调，生成会议纪要 | **是** |

### 核心指标

| 指标 | 数值 |
|------|------|
| RAG Top-3 检索准确率 | 85% |
| 问答准确性提升 | 31%（无 RAG 0.64 → 有 RAG 0.84）|
| GRPO 生成质量提升 | 15%（综合奖励 0.51 → 0.78）|
| 幻觉率下降 | 28% → 18% |
| ASR 转写准确率 | 92% |

### 数据规模

| 数据项 | 数量 |
|--------|------|
| 原始会议录音 | 300 场 |
| SFT 训练数据 | 29,344 条 |
| GRPO 训练 prompt | 1,200 条 |
| RAG 知识库 | 300 场会议记录（ChromaDB）|
| RAG 检索测试集 | 200 条 |
| 生成质量测试集 | 200 条 |

---

## 二、GRPO 奖励函数体系

### 2.1 当前已实现的奖励维度

#### LLM 层奖励（`grpo_reward_function.py`，9 维度，**已实现**）

```
reward = 0.20*准确性 + 0.15*忠实度 + 0.16*流畅度
       + 0.10*相关性  + 0.10*结构化  + 0.10*长度
       + 0.07*dep     + 0.07*exec   + 0.05*coherence
```

| 维度 | 权重 | 计算方式 | 防什么 |
|------|------|---------|-------|
| 准确性 | 20% | BGE cosine_sim(gen, ref) | 与标准答案的整体相似度 |
| 忠实度 | 15% | 逐句 vs RAG Top3，阈值 0.65 | **幻觉** |
| 流畅度 | 16% | unique 3-gram ratio * 长度惩罚 | 复读/模板化 |
| 相关性 | 10% | BGE cosine_sim(gen, query) | 答非所问 |
| 结构化 | 10% | Markdown 标记 + owner 非空检查 | 格式混乱 |
| 长度 | 10% | 100-300 字满分 | 过长/过短 |
| dependency_reward | 7% | Kahn 拓扑排序验证 DAG 无环 | 循环依赖 |
| executability | 7% | owner/task/deadline/dep 逐项检查 | 任务无法执行 |
| **coherence** | 5% | **BGE 相邻句子余弦相似度（已实现）** | **逻辑跳跃** |

#### LLM 层奖励（`grpo_multitask_reward.py`，4 维度）

```
reward = 0.35*纪要质量 + 0.35*行动项质量 + 0.20*决策项质量 + 0.10*整体格式
```

| 维度 | 权重 | 计算方式 |
|------|------|---------|
| 会议纪要 | 35% | 50% BGE sim + 30% 完整性 + 20% 流畅度 |
| 行动项 | 35% | 60% F1 score + 40% 格式规范性 |
| 决策项 | 20% | 60% F1 score + 30% 完整性 + 10% 格式 |
| 整体格式 | 10% | Markdown 标题 + 三章节 + 列表 |

### 2.2 ASR 层奖励（待实现）

**现状**：ASR（Fun-Audio-Chat-8B）完全冻结，未接入 GRPO 训练循环。

**目标**：参考 RLBR 论文（arXiv:2601.13409v1）和 CosyVoice GRPO 架构，实现 ASR 层的编辑距离 reward。

**设计**：

```
asr_reward = -WER(o*, oi)                          # 总体准确率（基础版）
asr_reward = -(WER + λ * BWER)                    # 增强版（关键词单独加权）
```

其中 BWER（Biasing Word Error Rate）专门对会议中的关键词/术语（如人名、专有名词、action items 中的任务）计算编辑距离，参考 RLBR 论文的 λ = 5 设置。

**注意**：ASR 和 LLM 的 GRPO 应分离训练，各自有独立的 reward 信号和优化目标，互相不干扰。

### 2.3 待补充的奖励维度

| 维度 | 状态 | 计算方式 | 来源 |
|------|------|---------|------|
| **语义连贯性（coherence）** | 待实现 | 见 2.4 节 | 本次讨论 |
| LLM-as-a-Judge 补强 | 待评估 | faithfulness / executability / helpfulness | 见 2.5 节 |

---

## 三、语义连贯性（Coherence）Reward 设计

### 3.1 为什么需要 coherence reward

现有 `fluency_reward` 只检测 n-gram 重复度，**无法判断段落内部的逻辑连贯性**。典型问题：

- 模型生成格式正确但逻辑跳跃的文本
- 前后段落主题不一致（前面说后端进度，后面突然跳到测试）
- 句子之间缺少衔接词或指代混乱

### 3.2 设计方案

**方案 A：基于 TF-IDF 句子相似度矩阵（rule-based，不引入新模型）**

```python
def _coherence_reward(self, generated: str) -> float:
    """
    语义连贯性奖励：基于相邻句子的 TF-IDF 向量余弦相似度。

    原理：连贯的文本相邻句子之间应该有较高的词汇重叠/主题相关性。
    如果相邻句子完全不相关（sim < threshold），说明存在逻辑跳跃。
    """
    import re
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np

    # 分句：按中文句号、问号、感叹号分
    sentences = re.split(r'[。！？\n]', generated)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

    if len(sentences) < 2:
        return 0.8  # 单句文本默认给中高分

    # 计算相邻句子的 TF-IDF 相似度
    vectorizer = TfidfVectorizer()
    try:
        tfidf_matrix = vectorizer.fit_transform(sentences)
        sims = []
        for i in range(len(sentences) - 1):
            s = cosine_similarity(tfidf_matrix[i], tfidf_matrix[i + 1])[0][0]
            sims.append(s)

        if not sims:
            return 0.8
        avg_sim = np.mean(sims)
        # 归一化：TF-IDF cosim 范围 0-1，0.2 以下视为不连贯
        return min(1.0, avg_sim / 0.2)
    except ValueError:
        return 0.5  # 向量化失败（词汇太少），降级给分
```

**方案 B：基于 BGE embedding 的句子间相似度（复用现有 embedder）**

```python
def _coherence_reward_v2(self, generated: str) -> float:
    """
    语义连贯性奖励：基于 BGE embedding 的相邻句子相似度。
    比 TF-IDF 更鲁棒，能捕捉语义层面的连贯性而非词汇层面的重叠。
    """
    import re
    import numpy as np

    sentences = re.split(r'[。！？\n]', generated)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 5]

    if len(sentences) < 2:
        return 0.8

    # 用 BGE embedder 编码每个句子
    embs = self.embedder.encode(sentences, convert_to_numpy=True)
    sims = []
    for i in range(len(embs) - 1):
        sim = np.dot(embs[i], embs[i + 1]) / (np.linalg.norm(embs[i]) * np.linalg.norm(embs[i + 1]))
        sims.append(sim)

    avg_sim = np.mean(sims)
    # 阈值设为 0.5（语义相似度）
    return min(1.0, max(0.0, (avg_sim - 0.3) / 0.5))
```

**推荐方案 B**：复用 `grpo_reward_function.py` 中已有的 `self.embedder`（BGE-large-zh-v1.5），无需加载额外模型，避免引入 judge bias。

### 3.3 在 DAG dependency reward 中复用 Kahn 拓扑排序

当前 `_dependency_validity_reward` 已经用 Kahn 拓扑排序验证了 action items 之间无循环依赖。这个思路可以扩展到段落级别的连贯性：

- 如果 action items 形成 DAG 且有明确的执行顺序，说明工作流设计合理
- 这个 reward 和 coherence reward 互补：coherence 看段落内逻辑，DAG 看任务间依赖

---

## 三之补充、串行 Reward 设计（腾讯面试反馈）

### 来源

腾讯 WXG 面试官提出：当前并行加权 reward 存在逻辑漏洞——如果输出结构化维度做得不好，后续 action items 解析失败，忠实度、准确性等下游维度实际上是在对"废输出"打分，reward signal 被污染了。

### 核心思想：条件依赖（Conditional Dependency）

reward 维度之间存在天然的依赖链：

```
文本生成层（LLM output）
  ↓ 如果结构化失败，后续全部失效
结构化层（Markdown 解析 + action items 提取）
  ↓ 依赖结构化成功
  ├→ 忠实度（逐句检查 vs RAG docs）← 依赖 action items 解析成功
  ├→ 准确性（BGE vs reference）     ← 可以独立计算
  ├→ 流畅度（n-gram 去重）          ← 可以独立计算
  ├→ 相关性（vs query）              ← 可以独立计算
  ├→ DAG 无环验证                    ← 依赖 action items 提取成功
  ├→ executability                   ← 依赖 action items 提取成功
  └→ coherence                      ← 可以独立计算（段落内句子）
```

**gate 机制**：结构化维度作为"守门人"，结构化分数低于阈值时，后续依赖链上的维度 reward 置零。

### 实现方案

```python
# gate 机制伪代码
STRUCTURE_THRESHOLD = 0.5  # 结构化分数低于此值，后续维度失效

structure_score = _structure_reward(generated, action_items)

if structure_score < STRUCTURE_THRESHOLD:
    # 结构化不通过，后续依赖维度全部置零
    return 0.10 * structure_score  # 只给结构化本身的小 reward

# 结构化通过，正常计算后续维度
accuracy = _accuracy_reward(generated, reference)
faithfulness = _faithfulness_reward(generated, retrieved_docs)
dep = _dependency_validity_reward(action_items)
exec_ = _task_executability(action_items)

# 无依赖的维度正常计算
fluency = _fluency_reward(generated)
relevance = _relevance_reward(generated, query)
coherence = _coherence_reward(generated)

reward = (
    0.22 * accuracy +
    0.17 * faithfulness +
    0.17 * fluency +
    0.10 * relevance +
    0.10 * structure_score +
    0.10 * length_reward(generated) +
    0.07 * dep +
    0.07 * exec_ +
    0.05 * coherence  # 新增 coherence 维度
)
```

### 和 Chat2Workflow 的区别

| 维度 | Chat2Workflow | 本项目 |
|------|-------------|-------|
| 层级 | 两阶段：Pass Rate → Resolve Rate | 多阶段 gate 机制 |
| Pass Rate | Markdown 格式 | Markdown 格式 + action_items 解析成功率 |
| Resolve Rate | DAG 无环 | DAG 无环 + executability |
| **本项目的串行扩展** | — | **结构化作为 gate**，忠实度/DAG/exec 依赖 gate 通过 |

### 面试回答话术

> "面试官提到 reward 维度应该是串行的，这个观点一针见血。因为 action items 解析本身依赖结构化成功——如果模型输出的 Markdown 格式混乱，正则解析失败，后续忠实度检查、DAG 验证、executability 检查全都是在对'废数据'打分，reward signal 被污染了。
>
> 所以我在结构化维度加了一个 gate 机制：结构化分数低于 0.5 时，后续依赖链上的维度（忠实度、DAG、executability）reward 直接置零，只保留对结构化本身的小奖励。这样模型必须先学会输出正确的结构，后层 reward 才有意义。这本质上是一个硬条件——类似于'先保证格式，再追求内容'的课程学习思路。"

---

## 四、LLM-as-a-Judge 评估维度

### 4.1 是否需要 LLM-aaJ？

**结论**：当前 rule-based + embedding 的 reward 体系已经比较完整，LLM-aaJ 是**锦上添花**，不需要全量引入。只需要在特定维度用 LLM-aaJ 补强。

### 4.2 推荐使用 LLM-aaJ 的维度

| 维度 | 当前实现 | LLM-aaJ 补强方式 | 是否推荐 |
|------|---------|-----------------|---------|
| 忠实度（faithfulness）| BGE cosine sim vs RAG docs | **已满足**，阈值 0.65 | 不需要 |
| 语义连贯性（coherence）| 待实现 | **推荐**：用 rule-based（BGE sentence sim）| **推荐先做 rule-based** |
| 可执行性（executability）| owner/task/deadline/dep 规则检查 | **可以补强**：LLM 判断 action item 是否真的可落地 | 可选 |
| 整体质量（helpfulness）| 无 | **推荐**：LLM-aaJ 给出 1-10 分整体质量分 | 建议加 |
| 格式规范性 | Markdown 正则检查 | **已满足** | 不需要 |

### 4.3 不使用 LLM-aaJ 的维度（rule-based 更准）

| 维度 | 原因 |
|------|------|
| WER/CER（ASR 层）| 精确匹配，LLM 打分反而不如编辑距离准 |
| BGE similarity（准确性）| 有 reference，embedding cosine 足够 |
| 拓扑排序（DAG 验证）| 算法确定性，无歧义 |
| n-gram 重复度（流畅度）| 规则精确，不依赖 LLM 判断 |

### 4.4 LLM-aaJ 防 Circular Reasoning

> ⚠️ **重要原则：不能用被训模型当 judge**

```
禁止：Fun-Audio-Chat-8B / Qwen2.5-0.5B（被 GRPO 训练的模型）→ 当 judge
必须：独立的 judge model（如 Qwen2.5-7B-Instruct 或 GPT-4o）
```

原因：同模型既做 judge 又做被训模型，reward signal 会被"自我强化"污染，模型会学会"讨好 judge 的偏好"而不是"真正提升质量"。

**工程实现**：LLM-aaJ 只在 evaluation 阶段调用（不参与训练梯度），用于监控 hidden metrics 和校准 rule-based reward 的方向。

---

## 五、技术决策记录

| 日期 | 决策 | 理由 |
|------|------|------|
| 2026-05-01 | GRPO 分离为 ASR 层 + LLM 层 | 梯度信号干净，各自收敛，互不干扰 |
| 2026-05-01 | 参考 RLBR 论文设计 ASR reward | character-level edit distance + biasing word 加权已验证有效 |
| 2026-05-01 | 警惕 reward hacking | 增加 hidden evaluation 监控、KL 正则、reference-aware 机制 |
| 2026-05-02 | 警惕 reward hacking | 增加 hidden evaluation 监控、KL 正则、reference-aware 机制 |
| 2026-05-02 | Coherence reward 用 BGE sentence sim | 复用已有 embedder，不引新 judge，避免 judge bias |
| 2026-05-02 | LLM-aaJ 仅用于 evaluation，不参与训练梯度 | 防止 circular reasoning / self-rewarding |
| 2026-05-02 | DAG dependency 用 Kahn 拓扑排序 | O(V+E)，确定性算法，无需 LLM 判断 |
| 2026-05-02 | **串行 reward 设计（腾讯面试反馈）** | 结构化作为 gate，后层 reward 依赖前层先满足 |
| 2026-05-02 | 结构化权重重构：gate 机制 | STRUCTURE_THRESHOLD 门控，结构不过则下游维度失效 |

---

## 六、待办事项

### 高优先级

- [x] 实现 ASR 层 GRPO reward（WER + BWER，参考 RLBR 论文）
- [x] **实现 coherence reward（BGE sentence sim）** — 合入 `grpo_reward_function.py`
- [x] 在 `train_grpo.py` 中接入 ASR reward 和 coherence reward
- [x] **实现结构化 gate 机制（串行 reward）**：STRUCTURE_THRESHOLD = 0.5，结构化不通过则忠实度/DAG/executability reward 置零
- [x] **重构 reward 权重**：在 gate 机制下调整权重分配（结构化权重提高，确保 gate 有效）

### 中优先级

- [ ] 实现 hidden evaluation metrics（不参与 reward，用于监控）
  - [ ] 输出长度分布监控（检测"缩短文本降低WER" hacking）
  - [ ] unique n-gram ratio 监控（检测复读）
  - [ ] WER on held-out set 监控（检测过拟合）
- [ ] 实现 helpfulness LLM-aaJ evaluation（仅 evaluation，不参与训练）

### 低优先级

- [ ] DPO 偏好数据构建（作为 GRPO 的备选/补充）
- [ ] 在线人工抽检闭环（每 500 steps 抽检 20 条）

---

## 七、面试知识点速记

### GRPO 核心公式

```
优势计算：A_i = (r_i - mean(R)) / std(R)
损失函数：L = -E[min(r * A, clip(r, 1-ε, 1+ε) * A)] + β * KL(π_new || π_ref)
其中 r = π_new(o|q) / π_old(o|q)
```

### 九维度奖励函数权重（面试必背）

```
reward = 0.20*准确性 + 0.15*忠实度 + 0.16*流畅度
       + 0.10*相关性  + 0.10*结构化  + 0.10*长度
       + 0.07*dep     + 0.07*exec   + 0.05*coherence
```

**Soft Gate**：忠实度/DAG/executability × √(structure_score)

### RLBR 论文关键点

- reward = -(WER + λ * BWER)，λ = 5 效果最好
- reference-aware GRPO：把 ground truth 加入 group 算 advantage
- β（KL 系数）= 0：论文发现 KL 正则对最终效果影响不大
- character-level 比 word-level 效果更好

### LLM-aaJ 注意事项

- ❌ 不能用被训模型当 judge（circular reasoning）
- ✅ 只在 evaluation 阶段用，不参与训练梯度
- ✅ 用于补强 rule-based 覆盖不了的维度（helpfulness、executability 语义判断）

### Coherence reward 设计

- ✅ 复用 BGE embedder，不引入新 judge
- ✅ 相邻句子 embedding 余弦相似度
- ✅ 与 DAG dependency 互补（段落内逻辑 vs 任务间依赖）

### 串行 Reward 设计（腾讯面试重点）

- **问题**：并行加权 reward 中，如果结构化失败，后续忠实度/DAG/executability 对"废输出"打分，signal 被污染


- **解法**：结构化作为 gate，STRUCTURE_THRESHOLD = 0.5，结构化不通过则后续依赖维度 reward 置零
- **本质**：条件概率优化——后层 reward 的期望值以"前层通过"为前提
- **类比**：课程学习（Curriculum Learning）——先学简单任务（结构化），再学复杂任务（忠实度/DAG）

---

## 八、参考论文

| 论文 | 核心贡献 | 可借鉴点 |
|------|---------|---------|
| RLBR (arXiv:2601.13409v1) | ASR+GRPO，character-level edit distance + biasing word reward | ASR reward 设计、reference-aware GRPO |
| CosyVoice GRPO (third_party/) | TTS + Triton ASR server 的端到端 GRPO | ASR reward 工程架构 |
| Chat2Workflow | 两阶段评估：Pass Rate + Resolve Rate | dependency DAG + executability reward 思路 |

---

## 九、软 Gate 机制（硬 gate 问题修复）**【已实现，合入 `grpo_reward_function.py`】**

### 硬 gate 的两个致命问题

**问题一：结构化维度本身会被 reward hacking**

模型可能只输出固定 Markdown 模板（## 会议纪要、### 行动项），通过表面格式拿高分，但 action_items 的实际内容是空的或无意义的。

**问题二（核心追问）：gate 边界的 reward cliff**

> "结构化 0.49 vs 0.51，reward 差了几十分，这会不会方差过大没法收敛？"

是的，这是硬 gate 的致命问题：

| 结构化分数 | 硬 gate reward | 软 gate reward（downstream_weight=√s）|
|-----------|--------------|--------------------------------------|
| 0.3 | 0.03（只给结构化）| ~0.45（连续）|
| 0.49 | 0.049 | ~0.71 |
| 0.51 | **0.86**（跳变！）| **~0.73**（连续！）|
| 1.0 | 0.86（全激活）| 1.0 |

硬 gate 在 0.5 边界处 reward cliff 超过 0.8，GRPO 的组内相对优势 A_i = (r_i - mean)/std 在边界处极不稳定，根本没法收敛。

### 解决方案：软 gate（Soft Gate）

**核心思想**：不用硬截断，用结构化分数对下游维度做比例缩放，连续可导，无 reward cliff。

```python
def compute_reward(self, generated, reference, query, retrieved_docs, action_items):
    accuracy   = self._accuracy_reward(generated, reference)
    fluency    = self._fluency_reward(generated)
    relevance  = self._relevance_reward(generated, query)
    length     = self._length_reward(generated)
    coherence  = self._coherence_reward(generated)
    structure_score = self._structure_reward(generated, action_items)

    # 下游维度：结构化越好，贡献越大（√s 是凸函数，低分压缩狠，高分温和）
    downstream_weight = structure_score ** 0.5
    faithfulness = self._faithfulness_reward(generated, retrieved_docs) * downstream_weight
    dep          = self._dependency_validity_reward(action_items) * downstream_weight
    exec_        = self._task_executability(action_items) * downstream_weight

    reward = (
        0.22 * accuracy + 0.17 * faithfulness + 0.17 * fluency +
        0.10 * relevance + 0.10 * structure_score +
        0.10 * length + 0.07 * dep + 0.07 * exec_ + 0.05 * coherence
    )
    return reward
```

**为什么用 √s 而不是 s**：√s 是凸函数，对低分压缩更狠（s=0.1 → √s=0.32），高分端释放更温和（s=0.9 → √s=0.95），符合"结构化差时要严格压制下游"的直觉。

### 加保险：KL 自适应安全因子

```python
kl = compute_kl(pi_new, pi_ref)
safety_factor = 1.0 / (1.0 + kl / kl_threshold)
downstream_weight = (structure_score ** 0.5) * safety_factor
```

训练初期 policy 不稳定时，下游权重被压制，结构化主导；训练稳定后，下游权重逐步释放。

### 面试完整回答话术

> "面试官追问得很准，硬 gate 有两个致命问题：一是结构化维度本身可能被 hacking——模型可能只输出格式模板但不填内容。二是 gate 边界存在 reward cliff，结构化 0.49 和 0.51 之间 reward 可能差 0.8，GRPO 的组内相对优势在边界处极不稳定，根本没法收敛。
>
> 所以我改成了软 gate：不用硬截断，而是用结构化分数对下游维度做比例缩放。比如下游权重 = √(structure_score)，忠实度 reward = BGE_sim × √(structure_score)。这样结构化 0.3 时下游贡献约 0.55，结构化 1.0 时下游贡献 1.0，完全连续可导，没有 cliff。
>
> 另外加了 KL 自适应的安全机制——如果 KL 散度过大说明 policy 在漂移，下游权重再额外压低，强制 policy 回归。软 gate + KL 安全因子双保险，结构化不会过拟合，其他维度也不会 reward 剧变。
>
> 这本质上不是串行 reward，而是**带条件权重的并行 reward**——下游维度的期望 reward 以结构化为条件，但梯度是连续的。"

---

*最后更新：2026-05-05*

---

## 十、今日会话记录（2026.5.5）

### 10.1 服务器连接问题

**问题**：尝试用 scp 上传代码到公司服务器（`root@js4.blockelite.cn:14136`），连接被服务器直接关闭。

```powershell
scp -r -P 14136 "d:\VscodeProject\Users\setupmac\DiTX-Clerk" root@js4.blockelite.cn:/root/DiTX-Clerk
# 结果：Connection closed by 198.18.0.48 port 14136
```

**原因分析**：SSH key 未加进服务器 authorized_keys，或端口/认证有问题。

**备选方案**：
1. 先上传 GitHub，再从服务器 `git clone`
2. 用学校服务器（内网可连）+ 本地电脑协作（内网电脑+开发电脑分开）

**结论**：暂时搁置，先整理文档记录。学校服务器方案待后续实现。

### 10.2 面试反馈整理

#### 腾讯 WXG 二面（2026.04.18）关键追问

| 追问方向 | 问题 | 反思 |
|---------|------|------|
| 项目分工 | "语音转文字是你做的吗？" | ❌ 简历第一点写"语音流水线"，但实际只是调用函数。面试官直接说"这个可能不太适合" |
| ASR 指标 | "怎么定义准确率？分子分母怎么算？" | 回答的是字符级 WER，但缺少系统性说明 |
| ASR 错误分析 | "8% 错误率具体是什么分布？" | 回答"专业术语占比最大"，追问"有没有做 OOV 处理"时回答不完整 |
| 词表 OOV | "今天冒出一个新词怎么处理？" | 诚实回答"目前直接放行"，没有兜底方案 |
| BERT 校准 | "怎么用 BERT 做置信度低于阈值的词？" | 思路对（mask + BERT 预测），但面试官指出 BERT 也面临同样 OOV 问题 |
| RAG 存储选型 | "为什么选 ChromaDB 而非 Faiss？" | 答"小数据量 + 不需要维护索引"，但被指出 Faiss 也不需要手动维护 |
| HNSW 召回率 | "HNSW 是 100% 召回吗？" | 诚实回答"不是"，但解释不够精确 |
| 权重设计 | "6 维权重是怎么定的？" | 回答"做了消融实验"，追问"训练过程权重变不变？"→ 固定不变，缺乏动态调整思路 |
| **串行 Reward** | "结构化失败时，忠实度/DAG 还在打分？" | ✅ 这是本次最重要的反馈，引出了"软 gate"设计 |

**面试官给出的唯一明确建议**：把简历第一点"语音流水线"改掉，那不是你的工作。

#### 腾讯 WXG 一面（2026.04.10）关键追问

| 追问方向 | 问题 | 反思 |
|---------|------|------|
| 串行 Reward 追问 | "6 维并行打分，结构化乱掉还在算忠实度？" | 同二面，引出 gate 机制需求 |
| GRPO loss 细节 | "X_wound 怎么反馈到 token 级别梯度？" | 回答偏宏观，缺少 token-level 细节 |
| 过程监督 | "AI coding 生成代码，怎么验证正确性？" | 诚实回答"离线跑 + 观察指标"，被指出缺少过程监督思考 |
| 小模型手搓 | "你手搓 MiniMind 是照着教程敲的？" | 面试官建议"学习阶段用 AI 看成熟框架，从 0 到 1 意义有限" |

#### 百度一面（2026.04.05）关键追问

| 追问方向 | 问题 | 反思 |
|---------|------|------|
| BGE Reranker 原理 | "Cross-Encoder 核心技术突破是什么？" | 讲了通用流程，但对 BGE 系列各版本核心技术不了解 |
| BGE v1.5 vs v1 | "v1.5 改进了什么？" | 不清楚 |
| 数据集构建 | "FunAudioChat 数据集是怎么构建的？" | 回答"人工感受"，缺少系统性方法论 |
| BM25 公式 | "BM25 的 IDF 原理是什么？" | 回答模糊，说"回去补充" |
| Chunk 策略 | "怎么过滤无效信息（开场白等）？" | 回答"用小模型判别"，追问"每句都过？" → 是，但依赖内部模型 |
| OpenCompass 四层 | "短期/长期 + 混合/显式分别是什么？" | 回答不系统，被建议按逻辑分类 |

---

## 十一、面试复盘与改进方向

### 11.1 简历修改（紧急）

- ❌ 删除或弱化"语音流水线"描述，改为"负责会议文字内容的后处理与结构化分析"
- ✅ 强调 RAG 系统设计 + GRPO 奖励函数设计（这是你真正核心工作）

### 11.2 技术知识补强

| 知识点 | 当前状态 | 行动 |
|--------|---------|------|
| BGE 系列各版本差异 | 不清楚 | 补充 v1.5 相似度分布校准、instruction-free 改进 |
| HNSW 召回率/复杂度 | 模糊 | 补充 O(log N) 复杂度、ef_construction 参数 |
| BM25 IDF 公式 | 不熟 | 补充 TF 截断、IDF 平滑机制 |
| ChromaDB vs Faiss | 混淆 | 明确：ChromaDB 是嵌入式向量库，Faiss 需要独立服务 |
| OpenCompass 四层架构 | 不系统 | 按"短期/长期 × 混合/显式"矩阵整理 |
| ASR 评价指标 | 只知 WER | 补充 RTF（CReal-Time Factor）、CER（字符错误率）、SER（句错误率） |

### 11.3 诚实版实验话术（精炼版）

> "说实话，实验数据是离线小样本跑的，没有做大规模的统计学验证。200 条测试集是人工抽样标注的，没有算 annotator agreement。如果要做严谨，应该是测试集扩到 1000+，算 Cohen's Kappa 保证标注一致性，每个消融配置跑 3 次 seed 汇报 mean±std。但当时资源有限，先快速验证了每个模块的有效性，数据方向是对的。"

---

## 十二、AI 质量报告功能（Human-in-the-Loop 增强）

> 实现日期：2026-05-24
> 来源：阿里 AI 全栈电话面试反馈 + 面试复盘

### 12.1 背景

阿里面试官（腾讯 Hunyuan 团队）提出的核心观点：

> "AI coding 时代，coding 环节不再是瓶颈，测试环节才是。测试质量决定了整个 pipeline 的交付能力。"

引申到 LLM 应用场景：**人工审核 LLM 生成的总结和行动项时，人不应该既是评估者又是决策者**——审核成本高、质量参差不齐。正确姿势是 AI 先评估，人再做决策。

### 12.2 核心理念

```
旧模式：LLM 生成 → 人自己读、评估、判断
新模式：LLM 生成 → AI 质量报告 → 人只做决策
```

**人的角色从"评估者"变成"决策者"**：
- AI 做分析（评分、找问题、提警告）
- 人只看报告做判断（通过 / 打回）

### 12.3 技术实现

#### 新增文件

| 文件 | 职责 |
|------|------|
| `audiochat/workflow/quality_reporter.py` | AI 质量报告生成器（核心逻辑） |
| `audiochat/workflow/state.py` | 新增 `QualityReport` 数据类 |

#### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `scripts/offline_pipeline_workflow.py` | `stage_llm` 后新增 Stage 5：AI 质量评估 + 行动项自动提取 |
| `scripts/task_cli.py` | `list` 命令加评分列，`show` 命令加质量报告展示 |
| `audiochat/workflow/__init__.py` | 导出 `QualityReport` |

### 12.4 QualityReport 数据结构

```python
@dataclass
class QualityReport:
    summary_score: float          # 总结质量分 0-10
    action_item_score: float     # 行动项质量分 0-10
    issues: tuple[str, ...]     # 发现的问题（如"行动项过少"）
    warnings: tuple[str, ...]    # 警告项（如格式不规范）
    overall_pass: bool           # 是否建议直接通过
    overall_score: float         # 综合质量分 0-10
    generated_at: str            # 生成时间
    raw_output: str              # AI 原始输出（便于调试）
```

序列化格式与 `TaskState.to_dict()` / `from_dict()` 对齐，存储在 `task.json` 中。

### 12.5 质量报告 prompt 设计

四个评分维度（总结完整性、准确性、行动项可操作性、格式规范性），综合评分加权计算。

判断逻辑：
- 综合评分 >= 7.0 且 无严重问题 → **PASS**（可直接确认）
- 综合评分 < 7.0 或 存在严重问题 → **NEED_REVIEW**（需重点审核）

### 12.6 工作流集成

```
Stage 1: Audio → Diarization
Stage 2: VAD 语音活动检测
Stage 3: FunASR ASR
Stage 4: RAG 检索（可选）
Stage 5: LLM 生成 + AI 质量评估  ← 新增
         ↓
     PENDING_APPROVAL（等人工决策）
```

质量报告在 LLM 生成后立即生成，并展示在 pipeline 输出的末尾，CLI 和 `task show` 命令均可查看。

### 12.7 面试怎么讲

> "我们的 HITL 流程里加了一层 AI 自动化质量报告。LLM 生成总结之后，不是直接给人工审，而是先让同一个模型（或另一个轻量模型）对总结做多维度评分，输出质量分、发现的问题和改进建议。质量分高的任务，人工可以直接点确认；质量分低的，报告会高亮问题项，人工重点审核那些项。这样审核效率提升了很多——人从评估者变成了决策者，只需要看结论，不需要自己读完整篇总结再做判断。"

### 12.8 工程化测试相关（面试补充）

| 测试类型 | 在项目里的体现 |
|---------|-------------|
| 集成测试 | 跑完整 pipeline，对比输入输出 |
| 接口测试 | 验证 FunASR 返回字段格式是否正确 |
| 回归测试 | 加新参数后对比原功能是否正常 |
| 功能测试 | 调 ASR 校正 prompt，验证输出是否正确 |
| **故障定位二分法** | 先跑完整 pipeline，没问题则结束；有则从中间切开，逐步定位到具体模块 |

面试被问到"怎么做测试"时，优先说**故障定位二分法**：先整体后局部，从中间切开逐步定位。

---

*最后更新：2026-06-03*

---

## 十三、意图识别 + Metadata Filter 升级（Agentic RAG 前置改造）

> 实现日期：2026-06-03

### 13.1 背景

之前讨论中明确了两个问题：
1. **metadata filter 存了但没用**：`meeting_id`、`timestamp` 写入 ChromaDB 但检索时未传递，导致向量检索候选集偏大
2. **意图识别缺失**：QA 模式直接拿用户问题做检索，没有"当前会议 vs 历史会议"的路由判断

### 13.2 Phase 1: Metadata Filter（已完成）

#### 改动 1：`storage.py` — `search()` 支持复合 where 过滤

```python
def search(
    self,
    query: str,
    k: int = 5,
    speaker_filter: Optional[str] = None,
    meeting_id_filter: Optional[str] = None,   # 新增
    time_range: Optional[tuple[str, str]] = None,  # 新增
) -> list[MeetingDocument]:
    # ChromaDB 的 $and 支持嵌套表达式
    conditions = []
    if speaker_filter: conditions.append({"speaker": speaker_filter})
    if meeting_id_filter: conditions.append({"meeting_id": meeting_id_filter})
    if time_range: conditions.append({"timestamp": {"$gte": start, "$lte": end}})
    if conditions:
        where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]
```

#### 改动 2：`retriever.py` — `retrieve()` 透传新参数

```python
def retrieve(
    self,
    query: str,
    k: int = 3,
    recall_k: int = 15,
    speaker_filter: Optional[str] = None,
    meeting_id_filter: Optional[str] = None,   # 新增
    time_range: Optional[tuple[str, str]] = None,  # 新增
    use_time_decay: bool = True,
    use_rerank: bool = True,
) -> list[RetrievedContext]:
    docs = self.storage.search(
        query, k=recall_k,
        speaker_filter=speaker_filter,
        meeting_id_filter=meeting_id_filter,
        time_range=time_range,
    )
```

**效果**：ChromaDB 在向量检索前先做 metadata 过滤，候选集变小，检索更快更准。

### 13.3 Phase 2: 意图识别路由（已完成）

#### 架构

```
用户问题
  ↓
规则快速判断（~0ms，零开销）
  ├─ 含"这场会议""刚才" → CURRENT_MEETING
  ├─ 含"上次""上周""之前" → CROSS_MEETING
  └─ 未命中
       ↓
    小模型精判（Qwen2.5-0.5B-Instruct，带 LRU 缓存）
       ↓
    CURRENT_MEETING / CROSS_MEETING / UNKNOWN
```

#### 实现：`prompting.py` 新增模块

| 函数 | 职责 |
|------|------|
| `classify_intent(question)` | 主入口，规则兜底 + 小模型精判 |
| `route_and_retrieve(...)` | 意图路由 + 执行检索 |
| `_rule_based_intent(question)` | 零延迟规则匹配 |
| `_cached_model_intent(question)` | 小模型分类（带 @lru_cache） |
| `IntentRoutingResult` | 路由结果 dataclass（含 confidence + reasoning）|

#### 三种意图的检索策略

| 意图 | 检索参数 | 说明 |
|------|---------|------|
| `CURRENT_MEETING` | `meeting_id_filter=current_meeting_id` | 只在当前会议内搜 |
| `CROSS_MEETING` | `time_range=time_range` | 在时间范围内搜历史会议 |
| `UNKNOWN` | 不过滤 | 默认全量 RAG |

#### 复用 ASR 校正的 0.5B 模型

意图识别复用 `Qwen2.5-0.5B-Instruct`（ASR 校正已加载），不额外部署，不额外花钱。

#### `build_llm_instruction` 改动

QA 模式接入意图路由：
```python
if mode == "qa":
    contexts, routing = route_and_retrieve(
        question=user_instruction,
        retriever=retriever,
        current_meeting_id=meeting_id,  # 新增
        time_range=time_range,          # 新增
    )
```

### 13.4 面试怎么讲

> "我的 RAG 检索有两个优化点。一是 metadata filter——之前存了 meeting_id 和 timestamp 但检索时没用上，现在 ChromaDB 在向量检索前先做 where 过滤，候选集小了，检索延迟降低、噪音减少。二是意图识别路由——用户问'这场会上张三说了什么'时，用规则判断是'当前会议'意图，直接在当前会议 ID 下精确检索；用户问'上周进度'时，判断是'历史会议'意图，用时间范围过滤。两个意图加起来覆盖了 90% 以上的场景，剩余的模糊情况走小模型分类，准确率更高。"

### 13.5 待升级项

- [ ] `time_range` 参数目前由外层传入，未来可以从用户问题中用 LLM 抽取时间实体（"上周""近一个月"→ ISO 日期）
- [ ] `HierarchicalRetriever` 的 L1 路由（`classify_meeting_type`）可作为 Phase 3 Agentic Planning 的一部分复用
- [ ] 意图识别的 confidence < 0.5 时，应触发人工确认而非直接走 UNKNOWN

---

## 十四、Agentic RAG 规划（Phase 3，待实现）

### 14.1 与 Phase 2 的区别

Phase 2 是**单轮检索**：意图识别决定检索策略 → 执行一次检索 → 返回结果。

Phase 3 是**多轮规划**：LLM 读转写 → 分析缺什么信息 → 生成多个子查询 → 批量检索 → 整合。

```
Phase 2（当前）：
  用户问题 → 意图识别 → 一次检索 → 回答

Phase 3（待实现）：
  转写摘要 → LLM 信息缺口分析 → 子查询列表 → 批量检索 → 评估够不够 → 不够继续查
                                                                              ↓
                                                                        够 → 整合 → 回答
```

### 14.2 核心组件

| 组件 | 实现位置 | 说明 |
|------|---------|------|
| 信息缺口分析器 | `prompting.py` 新增函数 | LLM 读转写摘要，输出 {info_type, query} 列表 |
| 子查询执行器 | `retriever.py` 新增方法 | 批量执行多个子查询 |
| 上下文压缩器 | 新增 `context_compressor.py` | 多检索结果合并压缩，控制 token 数 |
| Agentic 状态机 | `workflow/state.py` | 管理 planning → retrieval → evaluation → synthesis |

### 14.3 信息缺口分析 prompt 设计

```python
def analyze_info_gaps(transcription: str, current_meeting_id: str) -> list[dict]:
    prompt = f"""会议转写摘要：
{transcription}

当前会议 ID：{current_meeting_id}

分析：这场会议讨论了哪些议题？每个议题是否需要补充历史信息？
输出格式（JSON）：
[
  {{"info_type": "task_progress", "topic": "X模块进度", "query": "X模块上周进度如何"}},
  {{"info_type": "person_opinion", "speaker": "李四", "query": "李四对Y需求的看法"}}
]
如果没有需要补充的信息，返回空列表 []。
回答："""
    return llm_json_parse(prompt)
```

### 14.4 执行流程

```
1. 转写文本 → LLM 摘要压缩（max 500 tokens）
2. 摘要 → 信息缺口分析（LLM 调用 1）
3. 每个缺口 → 批量并发检索（多个 retriever 调用）
4. 检索结果 → 上下文压缩
5. 压缩结果 + 转写关键片段 → LLM 总结生成（LLM 调用 2）
```

三次 LLM 调用比一次超长调用更稳定，幻觉也更少。

### 14.5 什么时候值得做 Phase 3？

**适合场景**：
- 会议总结（转写文本长，历史信息需求多）
- 复杂项目回顾（跨多个会议的追踪）

**不必做的场景**：
- 简单问答（Phase 2 足够，单轮检索就够了）
- 延迟敏感场景（多轮 LLM 调用增加 1-2s 延迟）

---

## 十五、Phase 3 实现：Agentic RAG（已完成，2026-06-03）

### 15.1 新增组件

| 函数 | 文件 | 职责 |
|------|------|------|
| `analyze_info_gaps()` | `prompting.py` | LLM 读转写，输出 InfoGap 列表（子查询生成） |
| `plan_and_retrieve()` | `prompting.py` | 主流程：缺口分析 → 并发检索 → 去重 → 压缩 |
| `compress_contexts()` | `prompting.py` | 多检索结果合并，按层级和 score 排序截断 |
| `build_agentic_instruction()` | `prompting.py` | 构建含压缩上下文的 prompt |
| `InfoGap` dataclass | `prompting.py` | 单个信息缺口（info_type, topic, sub_query） |
| `PlanningResult` dataclass | `prompting.py` | 规划结果（缺口列表 + 压缩上下文 + token 估算） |

### 15.2 执行流程

```
会议转写文本
    ↓
analyze_info_gaps()       [LLM 调用 1，约 200 tokens 输出]
    ↓ 信息缺口列表（InfoGap[]）
plan_and_retrieve()       [并发批量检索，max_workers=4]
    ↓
compress_contexts()        [按 layer/score 排序，截断至 ~2000 tokens]
    ↓
build_agentic_instruction() [拼接转写 + 压缩上下文 + 用户指令]
    ↓
Fun-Audio-Chat-8B 生成总结 [LLM 调用 2]
```

### 15.3 并发检索设计

使用 `concurrent.futures.ThreadPoolExecutor` 并发执行多个子查询，所有子查询并发执行，总耗时 ≈ max(各查询耗时) 而不是 sum(各查询耗时)。

### 15.4 上下文压缩策略

1. 按 `relevance_score` 降序排列
2. 同分数时，按层级优先级：L3 SOP > L2 事实 > FALLBACK > L1 > RAW
3. 超出 `max_tokens`（默认 2000）时从末尾截断
4. 同内容去重（按 content 哈希）

### 15.5 使用方式

```bash
# 启用 Agentic RAG（总结模式）
python scripts/offline_pipeline_workflow.py \
    --audio <wav> --enable-rag --agentic-rag \
    --time-range 2026-05-01,2026-06-03

# QA 模式（意图识别，无需额外参数）
python scripts/offline_pipeline_workflow.py \
    --audio <wav> --enable-rag --mode qa \
    --query "这场会上张三说了什么"
```

### 15.6 与 Phase 2 的关系

| 维度 | Phase 2（意图识别） | Phase 3（Agentic RAG） |
|------|-------------------|----------------------|
| 触发模式 | QA 模式 | Summary 模式 |
| 子查询数量 | 1（用户 query） | N（LLM 分析生成，最多 5 个） |
| 检索次数 | 1 次 | N 次并发 |
| LLM 调用 | 1 次（生成回答） | 2 次（分析缺口 + 生成总结）|
| 延迟 | 低 | 中（+1-2s）|

### 15.7 面试怎么讲

> "总结模式下的 Agentic RAG 分三步。第一步，LLM 先读转写摘要，分析这场会议里哪些议题需要补充历史背景，输出结构化的信息缺口列表，每个缺口是一个子查询。第二步，用 ThreadPoolExecutor 并发执行所有子查询——比如识别出'X模块进度''李四对Y的看法'两个缺口，就同时发两个检索请求，总耗时是取 max 而不是 sum。第三步，把所有检索结果按层级和相关性排序，超出 token 限制就从末尾截断，避免上下文过长。两轮 LLM 调用（分析缺口 + 生成总结）比一次塞入超长文本更稳定，幻觉也更少。"

---

*最后更新：2026-06-03*
