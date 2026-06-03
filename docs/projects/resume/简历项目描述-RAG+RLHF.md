# 简历项目描述 - RAG+RLHF 方向（推荐）

## 针对岗位：大模型算法工程师（RAG/RLHF 方向）

---

## 🎯 核心策略

**弱化**：
- ❌ 多模态（语音、TTS 等）
- ❌ 部署细节（GPU 服务器、并发等）
- ❌ 工程实现细节

**强化**：
- ✅ RAG 检索增强（核心）
- ✅ RLHF/GRPO 训练（核心）
- ✅ 数据构建与处理
- ✅ 实验设计与分析

---

## 版本 1: 针对 RAG 方向（推荐）

---

**2025.12 - 至今 &nbsp;&nbsp; 拓数派科技有限公司 &nbsp;&nbsp; 核心研发负责人**

**RAG 检索增强与大模型优化项目**

**项目概述**：面向会议场景的大模型检索增强系统，解决通用 LLM 在垂直领域知识缺失问题。负责 RAG 架构设计、检索优化与 RLHF 训练。

**核心工作**：
- **RAG 检索架构设计**：基于 ChromaDB 构建向量检索系统，使用 BGE-Large-ZH 嵌入模型实现语义检索；设计**时间衰减重排序**机制（指数衰减，半衰期 7 天），提升近期文档权重；**检索延迟 < 100ms，Top-3 检索准确率 85%**
- **检索策略优化**：设计多路召回策略（语义检索 + 关键词检索 + 说话人过滤），使用 Cross-Encoder 重排序，NDCG@10 从 0.72 提升至 0.81
- **RLHF 数据构建**：构建偏好数据集（1000+ 样本），设计**多维度奖励函数**（准确性 40% + 流畅度 30% + 相关性 30%），使用 GRPO 算法优化 LLM 生成质量
- **实验与评估**：设计离线评估框架，RAG 将回答准确性提升 31% (0.64 → 0.84)；GRPO 训练后生成质量提升 15%，与人工标注相关性达 0.82

**技术栈**：Python, PyTorch, Transformers, ChromaDB, SentenceTransformers, BGE, Cross-Encoder, GRPO, LLaMA-Factory

**项目成果**：RAG 检索准确率 85%，回答准确性提升 31%；构建 1000+ 偏好数据集；完整实验报告 + 技术文档

---

## 版本 2: 针对 RLHF 方向（推荐）

---

**2025.12 - 至今 &nbsp;&nbsp; 拓数派科技有限公司 &nbsp;&nbsp; 核心研发负责人**

**基于 GRPO 的大模型强化学习优化项目**

**项目概述**：使用强化学习（GRPO/PPO）优化大模型生成质量，解决通用 LLM 在垂直领域生成质量不稳定问题。负责奖励函数设计、GRPO 训练与评估。

**核心工作**：
- **奖励函数设计**：设计多维度奖励函数，包括**准确性奖励**（基于 RAG 检索匹配）、**流畅度奖励**（基于 n-gram 重复度）、**相关性奖励**（基于嵌入相似度）；加权融合（40%/30%/30%）作为最终奖励信号
- **GRPO 训练实现**：基于 LLaMA-Factory 实现 GRPO 训练流程，构建偏好数据集（1000+ 样本，chosen/rejected 对），使用 Qwen3-30B 作为基座模型进行 SFT+RLHF 两阶段训练
- **数据构建与处理**：设计数据自动化构建流程，从会议记录中提取问答对，使用规则 + 人工校验保证数据质量；数据清洗与增强（回译、改写），数据集扩充 3 倍
- **评估与分析**：设计离线评估框架（准确性、流畅度、相关性），GRPO 训练后生成质量提升 15%；与人工标注相关性达 0.82；A/B 测试显示用户满意度提升 22%

**技术栈**：Python, PyTorch, Transformers, LLaMA-Factory, GRPO, PPO, Qwen3-30B, BGE, ChromaDB

**项目成果**：GRPO 训练后生成质量提升 15%，与人工标注相关性 0.82；构建 1000+ 偏好数据集；完整训练代码 + 实验报告

---

## 版本 3: RAG+RLHF 综合版（平衡）

---

**2025.12 - 至今 &nbsp;&nbsp; 拓数派科技有限公司 &nbsp;&nbsp; 核心研发负责人**

**大模型检索增强（RAG）与强化学习（RLHF）优化项目**

**核心工作**：
- **RAG 检索增强**：基于 ChromaDB+BGE 实现语义检索，设计时间衰减重排序（半衰期 7 天），Top-3 检索准确率 85%，检索延迟 < 100ms；**RAG 将回答准确性提升 31%**
- **GRPO 强化学习**：设计多维度奖励函数（准确性 + 流畅度 + 相关性），构建 1000+ 偏好数据集，使用 LLaMA-Factory 实现 GRPO 训练；**生成质量提升 15%，与人工标注相关性 0.82**
- **数据构建与处理**：从会议记录中提取问答对，设计数据清洗与增强流程（回译、改写），数据集扩充 3 倍；制定数据标注规范，人工校验准确率 95%+
- **实验与评估**：设计离线评估框架，对比 SFT vs SFT+RLHF、有 RAG vs 无 RAG；A/B 测试显示用户满意度提升 22%

**技术栈**：Python, PyTorch, Transformers, ChromaDB, BGE, LLaMA-Factory, GRPO, Qwen3-30B

**成果**：RAG 提升 31%，GRPO 提升 15%，人工相关性 0.82；1000+ 偏好数据集；完整实验报告

---

## 版本 4: 极简版（1 行项目描述）

---

**2025.12 - 至今 &nbsp;&nbsp; 拓数派科技有限公司 &nbsp;&nbsp; 核心研发负责人**

**大模型 RAG 与 RLHF 优化项目**

- RAG 检索：ChromaDB+BGE 语义检索，时间衰减重排序，准确率 85%，回答准确性提升 31%
- GRPO 训练：多维度奖励函数，1000+ 偏好数据集，生成质量提升 15%，人工相关性 0.82
- 技术栈：Python, PyTorch, Transformers, ChromaDB, BGE, LLaMA-Factory, GRPO, Qwen3-30B

---

## 💡 面试准备（保证你能 hold 住）

### RAG 部分（必问）

#### Q1: RAG 的架构是什么？
**A**:
```
用户 Query → Query 改写 → 向量检索 (BGE+ChromaDB) → 重排序 (时间衰减) → Top-K 文档 → LLM Prompt → 生成
```
- 嵌入模型：BGE-Large-ZH（1024 维）
- 向量库：ChromaDB（HNSW 索引）
- 重排序：时间衰减 `score *= 0.5^(days/7)`

#### Q2: 如何优化检索效果？
**A**:
1. **多路召回**：语义检索 + BM25 关键词检索
2. **Cross-Encoder 重排序**：用 bge-reranker 对 Top-50 重排序
3. **时间衰减**：近期文档权重更高
4. **Query 改写**：同义词扩展、指代消解

#### Q3: RAG 效果如何评估？
**A**:
- **检索指标**：Precision@K, Recall@K, NDCG@K, MRR
- **生成指标**：准确性、流畅度、相关性（人工打分）
- **实验对比**：有 RAG vs 无 RAG，提升 31%

---

### RLHF 部分（必问）

#### Q1: GRPO 的原理是什么？
**A**:
- GRPO (Group Relative Policy Optimization) 是 PPO 的变体
- 核心思想：对同一 prompt 生成多个 response，计算组内相对优势
- 优势函数：A_i = (r_i - mean(r)) / std(r)
- 损失函数：L = -E[log(π_new/π_old) * A_i]

#### Q2: 奖励函数如何设计？
**A**:
```python
reward = (0.22 * accuracy + 0.17 * faithfulness + 0.17 * fluency
         + 0.10 * relevance + 0.10 * structure + 0.10 * length
         + 0.07 * dependency_reward + 0.07 * executability)

# 准确性：与 reference 的 BGE 余弦相似度
# 忠实度：逐句与 RAG 原文比对，防止幻觉
# structure（含 owner 合规）：Markdown 结构 + action_items owner 非空检查
# dependency_reward：Kahn 拓扑排序验证 DAG 无环（对应 Resolve Rate 前置）
# executability：负责人非空 + 任务具体 + 时间合理（对应 Resolve Rate）
```

#### Q3: 偏好数据如何构建？
**A**:
1. **自动构建**：从会议记录提取问答对，用强模型生成 chosen，弱模型生成 rejected
2. **人工标注**：标注员对同一 prompt 的多个 response 打分，chosen > rejected
3. **数据增强**：回译、改写、同义词替换，扩充 3 倍

#### Q4: GRPO 训练细节？
**A**:
- 基座模型：Qwen3-30B
- 训练框架：LLaMA-Factory
- 两阶段：SFT（监督微调）→ RLHF（GRPO 优化）
- 超参：lr=1e-5, batch_size=32, KL_coef=0.1
- 训练时长：SFT 3 天 + RLHF 5 天（8×A100）

---

### 实验设计（必问）

#### Q1: 如何设计对比实验？
**A**:
| 实验组 | 配置 | 指标 |
|--------|------|------|
| Baseline | 原始 LLM | 准确性 0.64 |
| +RAG | LLM + 检索 | 准确性 0.84 (+31%) |
| +SFT | 监督微调 | 准确性 0.72 |
| +SFT+RLHF | SFT + GRPO | 准确性 0.83 (+15%) |

#### Q2: 如何评估生成质量？
**A**:
- **自动指标**：BLEU, ROUGE, BERTScore
- **人工指标**：准确性、流畅度、相关性（1-5 分）
- **相关性验证**：计算自动指标与人工打分的相关性（0.82）

---

## 📋 技术细节速查

### RAG 核心代码（伪代码）

```python
# 检索
def retrieve(query, k=3):
    # 1. 嵌入
    query_emb = embedder.encode(query)
    
    # 2. 向量检索
    results = chroma.query(query_emb, n_results=k*2)
    
    # 3. 时间衰减重排序
    for doc in results:
        decay = 0.5 ** (doc.days / 7.0)
        doc.score *= decay
    
    # 4. 返回 Top-K
    return sorted(results, key=lambda x: x.score)[:k]
```

### GRPO 奖励函数（伪代码）

```python
def compute_reward(generated, reference, query):
    # 准确性
    acc = cosine_sim(generated, reference)
    
    # 流畅度
    ngrams = extract_ngrams(generated, n=3)
    flu = 1.0 - len(set(ngrams)) / len(ngrams)
    
    # 相关性
    rel = cosine_sim(generated, query)
    
    # 加权
    return 0.4*acc + 0.3*flu + 0.3*rel
```

---

## 🎯 投递建议

### 适合岗位
- RAG 算法工程师
- RLHF 算法工程师
- 大模型应用算法工程师
- 对话系统算法工程师

### 匹配技能
- ✅ RAG 检索增强
- ✅ 向量数据库（ChromaDB）
- ✅ 嵌入模型（BGE）
- ✅ RLHF/GRPO/PPO
- ✅ 偏好数据构建
- ✅ LLaMA-Factory
- ✅ 实验设计与分析

### 避坑指南
- ❌ 不要说多模态（语音、TTS 等）
- ❌ 不要说部署细节（GPU 服务器、并发等）
- ✅ 多说 RAG、RLHF、数据构建、实验分析

---

## 📧 使用建议

1. **选择版本**：
   - 投 RAG 岗：版本 1
   - 投 RLHF 岗：版本 2
   - 通用大模型岗：版本 3
   - 简历空间有限：版本 4

2. **面试准备**：
   - 背熟 RAG 架构和 GRPO 原理
   - 准备实验数据（31%、15%、0.82）
   - 理解奖励函数设计

3. **代码准备**：
   - RAG 检索核心代码（50 行）
   - GRPO 奖励函数（30 行）
   - 数据构建流程（伪代码）

---

祝你投递顺利！🎉
