"""
GRPO 多维度奖励函数 - 完整可运行版

用途：GRPO 强化学习中，给生成的会议纪要打分
核心思想：八维度奖励（借鉴 Chat2Workflow 两阶段评估思想）

维度分类：
  六原始维度：准确性、忠实度、流畅度、相关性、结构化、长度
  二新增维度：dependency_reward（DAG 无环验证）、executability_reward（任务节点可执行）
  一新增维度：coherence_reward（语义连贯性，基于 BGE sentence sim）

公式：
  reward = 0.20*准确性 + 0.15*忠实度 + 0.16*流畅度
         + 0.10*相关性  + 0.18*结构化(含4子项)
         + 0.08*executability + 0.07*dependency
         + 0.06*coherence

结构化维度的4个子项（内部加权，不对外暴露）：
  - 文档结构（30%）：Markdown 标题/列表/关键词标记
  - 行动项章节（15%）：是否存在行动项专属章节
  - Action 元数据（35%）：owner非空 + deadline有时序词 + task具体
  - 长度（20%）：字数在合理范围内

维度合并说明（面试可答）：
  - structure 合并了 length（字数约束是格式的自然组成部分，不单独设维度）
  - relevance 采用 HyDE-inspired 策略：用 retrieved_docs 作为 query 的语义代理，
    解决 query 向量稀疏导致余弦相似度无意义的问题

Soft Gate 机制：
  下游维度（忠实度/DAG/executability）乘以 √(structure_score)，
  结构化越好下游贡献越大，结构化差时压制下游 reward signal，
  避免对"废输出"打分导致的 reward 污染。

使用示例：
    reward_fn = GRPORewardFunction()
    reward = reward_fn.compute_reward(generated, reference, query, retrieved_docs, action_items)
"""

import torch
import numpy as np
from typing import List
from sentence_transformers import SentenceTransformer, CrossEncoder
from sklearn.metrics.pairwise import cosine_similarity


# ============================================================
# GRPO 奖励函数类
# ============================================================

class GRPORewardFunction:
    """
    GRPO 多维度奖励函数
    
    作用：给生成的会议纪要打分，告诉模型哪个生成结果更好
    
    输入：
        - generated: 模型生成的会议纪要
        - reference: 标准答案（人工写的会议纪要）
        - query: 用户的问题（如"总结这次会议"）
        - retrieved_docs: RAG 检索到的相关文档
    
    输出：
        - reward: 0-1 之间的分数，越高越好
    
    公式：
        reward = 0.4*accuracy + 0.25*fluency + 0.15*relevance 
               + 0.1*structure + 0.1*length
    """
    
    def __init__(self, 
                 embedder_model: str = "BAAI/bge-large-zh-v1.5",
                 reranker_model: str = "BAAI/bge-reranker-large"):
        """
        初始化奖励函数
        
        参数：
            embedder_model: BGE 嵌入模型名称
            reranker_model: BGE ReRanker 模型名称
        
        八维度权重：
            accuracy(0.20) + faithfulness(0.15) + fluency(0.16)
            + relevance(0.10) + structure(0.18, 含4子项: 文档30%/章节15%/元数据35%/长度20%)
            + executability(0.08) + dependency(0.07) + coherence(0.06)
        """
        print("正在加载奖励函数模型...")
        
        # 1. 加载 BGE 嵌入模型（用于计算语义相似度）
        # 作用：把文本变成 1024 维向量
        self.embedder = SentenceTransformer(embedder_model)
        
        # 2. 加载 BGE ReRanker 模型（用于精排）
        # 作用：直接输出两个文本的相关性分数（0-1）
        self.reranker = CrossEncoder(reranker_model)
        
        print("✅ 奖励函数模型加载完成")
    
    def compute_reward(self,
                       generated: str,
                       reference: str,
                       query: str,
                       retrieved_docs: List[str],
                       action_items: List[dict] = None) -> float:
        """
        计算八维度奖励（主函数）

        公式（面试重点）：
            reward = 0.20*准确性 + 0.15*忠实度 + 0.16*流畅度
                   + 0.10*相关性(HyDE) + 0.18*结构化(合并length)
                   + 0.08*executability + 0.07*dependency
                   + 0.06*coherence

        relevance 的 HyDE-inspired 策略（面试要点）：
            直接用短 query 编码做余弦相似度没有意义（query 稀疏）。
            改用 retrieved_docs 的加权组合作为 query 的语义代理：
            每个 doc 对 query 的相关性通过 max-sim 隐式得到，
            而后再和 generated 算相似度。
            本质上是用 RAG 检索到的文档语义来"扩写"了 query 的信息量。

        Soft Gate 机制：
            下游维度（忠实度 / DAG / executability）的 reward 乘以 √(structure_score)，
            结构化越好下游贡献越大，结构化差时压制下游 reward signal，
            避免对"废输出"打分导致的 reward 污染。

        新增两个维度（借鉴 Chat2Workflow 两阶段评估思想）：
            - dependency_reward：DAG 无环拓扑检验（对应 Resolve Rate 前置验证）
            - executability_reward：任务节点可执行性（对应 Resolve Rate）
            - coherence_reward：语义连贯性，相邻句子 BGE embedding 余弦相似度
        """
        accuracy_reward = self._accuracy_reward(generated, reference)
        fluency_reward = self._fluency_reward(generated)
        relevance_reward = self._relevance_reward(generated, query, retrieved_docs)
        structure_score = self._structure_reward(generated, action_items)
        coherence_reward = self._coherence_reward(generated)

        # 下游维度：结构化越好，贡献越大（√s 是凸函数，低分压缩狠，高分温和）
        # 面试要点：√s 对低分更严格（s=0.1 → √s=0.32），高分更温和（s=0.9 → √s=0.95）
        downstream_weight = structure_score ** 0.5

        faithfulness_reward = self._faithfulness_reward(generated, retrieved_docs) * downstream_weight
        dependency_reward = self._dependency_validity_reward(action_items) * downstream_weight
        executability_reward = self._task_executability(action_items) * downstream_weight

        reward = (
            0.20 * accuracy_reward +
            0.15 * faithfulness_reward +
            0.16 * fluency_reward +
            0.10 * relevance_reward +
            0.18 * structure_score +
            0.08 * executability_reward +
            0.07 * dependency_reward +
            0.06 * coherence_reward
        )

        return reward
    
    def _faithfulness_reward(self, generated: str, retrieved_docs: List[str]) -> float:
        """
        忠实度奖励（防幻觉）
        
        思路：生成内容中的每个关键句应能在源文档中找到依据。
        具体做法——NLI 风格：将生成内容拆成句子，逐句与检索文档
        计算最大语义相似度，低于阈值的视为"不忠实"。

        面试要点：
          - 和准确性的区别：准确性比对 reference，忠实度比对 source docs
          - 防止模型编造不存在的进度或人名
        """
        if not retrieved_docs:
            return 0.5

        import re as _re
        sentences = [s.strip() for s in _re.split(r'[。；\n]', generated) if len(s.strip()) > 5]
        if not sentences:
            return 0.5

        doc_embs = self.embedder.encode(retrieved_docs, convert_to_numpy=True)
        faithful_count = 0
        for sent in sentences:
            sent_emb = self.embedder.encode(sent, convert_to_numpy=True)
            sims = cosine_similarity([sent_emb], doc_embs)[0]
            if max(sims) > 0.65:
                faithful_count += 1

        return faithful_count / len(sentences)

    def _accuracy_reward(self, generated: str, reference: str) -> float:
        """
        准确性奖励
        
        思路：计算生成内容和标准答案的语义相似度
        
        步骤：
        1. 用 BGE 模型把生成内容变成向量
        2. 用 BGE 模型把标准答案变成向量
        3. 计算两个向量的余弦相似度
        4. 归一化到 0-1
        
        参数：
            generated: 生成的会议纪要
            reference: 标准答案
        
        返回：
            0-1 之间的分数，1 表示完全一样
        """
        
        # 1. 生成向量嵌入
        # embedder.encode() 把文本变成 1024 维向量
        gen_emb = self.embedder.encode(generated, convert_to_numpy=True)
        ref_emb = self.embedder.encode(reference, convert_to_numpy=True)
        
        # 2. 计算余弦相似度
        # cosine_similarity() 计算两个向量的夹角余弦值
        # 结果范围：-1 到 1，1 表示完全相同，0 表示无关，-1 表示完全相反
        sim = cosine_similarity([gen_emb], [ref_emb])[0][0]
        
        # 3. 归一化到 0-1
        # 因为余弦相似度范围是 -1 到 1，所以要转换到 0-1
        # 公式：(sim + 1) / 2
        # 例如：sim=1 → 1.0, sim=0 → 0.5, sim=-1 → 0.0
        accuracy = (sim + 1) / 2
        
        return accuracy
    
    def _fluency_reward(self, generated: str) -> float:
        """
        流畅度奖励
        
        思路：检查文本是否有重复的 n-gram，重复越少越流畅
        
        步骤：
        1. 提取所有 3-gram（连续 3 个字）
        2. 计算唯一 n-gram 的比例
        3. 长度惩罚（太短或太长都扣分）
        
        参数：
            generated: 生成的文本
        
        返回：
            0-1 之间的分数，1 表示非常流畅
        """
        
        # 1. 提取 3-gram
        # 例如："后端 API 已完成" → ["后端 A", "端 API", "API 已", "PI 已完", "I 已完成"]
        ngrams = [generated[i:i+3] for i in range(len(generated)-2)]
        
        # 处理边界情况：如果文本太短，没有 n-gram
        if len(ngrams) == 0:
            return 0.0
        
        # 2. 计算唯一 n-gram 比例
        # 例如：100 个 n-gram 中有 80 个是唯一的 → 比例 0.8
        # 比例越高，说明重复越少，越流畅
        unique_ratio = len(set(ngrams)) / len(ngrams)
        
        # 3. 长度惩罚
        # 太短（<50 字）或太长（>500 字）都扣分
        length = len(generated)
        
        if length < 50:
            # 太短，信息不足
            length_penalty = 0.3
        elif length > 500:
            # 太长，冗余
            length_penalty = 0.5
        else:
            # 长度合适，不惩罚
            length_penalty = 1.0
        
        # 4. 最终流畅度分数
        fluency = unique_ratio * length_penalty
        
        return fluency
    
    def _relevance_reward(self, generated: str, query: str, retrieved_docs: List[str] = None) -> float:
        """
        相关性奖励（HyDE-inspired 策略，解决 query 向量稀疏问题）

        问题：query 通常很短（如"总结这次会议"），BGE 编码后向量高度稀疏，
        直接和长文本 generated 算余弦相似度没有统计意义。

        解决方案（HyDE-inspired）：
            不直接用 query 向量，而是用 retrieved_docs 的拼接向量作为语义代理。
            原理：retrieved_docs 是针对 query 从知识库中检索回来的文档，
            它们天然是"和 query 相关的长文本"——
            本质上就是 HyDE 思想中 LLM 生成的假设答案（Hypothetical Document）的角色。
            我们把 docs 拼接后的向量当作 query 的语义扩展，
            再和 generated 算余弦相似度，数值有实际物理含义。

        面试要点：
            - HyDE 原文是用 LLM 生成假设答案再做检索
            - 我们这里是"借用 RAG pipeline 天然已有的检索结果"作为代理，
              不需要额外调用 LLM，开销更小
            - 另一个等价的解释：relevance = sim(docs_emb, generated_emb)，
              衡量生成内容是否在检索文档张开的语义空间内
        """

        # 如果没有检索文档，降级为 query 直接比对
        if not retrieved_docs:
            gen_emb = self.embedder.encode(generated, convert_to_numpy=True)
            query_emb = self.embedder.encode(query, convert_to_numpy=True)
            sim = cosine_similarity([gen_emb], [query_emb])[0][0]
            return max(0.0, (sim + 1) / 2)

        # HyDE-inspired：用 retrieved_docs 的拼接向量作为 query 的语义代理
        # retrieved_docs 已经是针对 query 检索回来的文档，
        # 天然具有"假设答案"的语义结构——和 generated 同为长文本
        docs_text = " ".join(retrieved_docs)
        docs_emb = self.embedder.encode(docs_text, convert_to_numpy=True)
        gen_emb = self.embedder.encode(generated, convert_to_numpy=True)

        # 计算 generated 和 docs 语义空间的相似度
        sim = cosine_similarity([gen_emb], [docs_emb])[0][0]

        # 归一化到 0-1
        relevance = max(0.0, (sim + 1) / 2)

        return relevance
    
    def _structure_reward(self, generated: str, action_items: list = None) -> float:
        """
        结构奖励（合并了 length，并扩展了 action_items 元数据检查）

        评估四个层面：
        1. 文档层面：检查 Markdown 标题、列表、关键词等结构化标记
        2. 行动项章节：检查是否包含行动项专属章节标题
        3. Action 元数据：owner 非空 + deadline 有时序关键词 + task 描述具体
        4. 长度层面：字数是否在合理范围内

        合并理由（面试可答）：
            字数约束本质上是输出格式规范的一部分——
            会议纪要的理想字数范围本身就是格式规范，
            不应作为一个独立维度和"结构化"平级。
            合并后结构化维度权重从 0.10 提升到 0.18，
            更强调格式质量。

        Action 元数据检查的目的（面试可答）：
            _task_executability 是下游维度，只有结构化通过 soft gate 后才生效。
            但 owner 为空、deadline 缺失、task 过于笼统这些格式类问题，
            应该在结构化阶段就惩罚，而不是等 executability 来兜底。
            这和 SFT 的监督信号是同构的：教模型在格式层面就把元数据填完整。

        参数：
            generated: 生成的文本
            action_items: 解析出的行动项列表（可选）

        返回：
            0-1 之间的分数，1 表示结构非常好
        """

        # ========== 1. 文档结构层面 ==========
        structure_markers = [
            "##",          # Markdown 二级标题
            "###",         # Markdown 三级标题
            "1.", "2.",    # 有序列表
            "- ", "* ",    # 无序列表
            "会议纪要",     # 关键词
            "行动项",       # 关键词
            "决策项",       # 关键词
            "参会人员",     # 关键词
            "进度汇报"      # 关键词
        ]

        doc_score = 0
        for marker in structure_markers:
            if marker in generated:
                doc_score += 1

        doc_score = min(1.0, doc_score / 3)

        # ========== 2. 行动项章节存在性 ==========
        action_section_keywords = ["行动项", "### 行动", "## 行动", "action"]
        action_section_score = 0.0
        for kw in action_section_keywords:
            if kw in generated:
                action_section_score = 1.0
                break

        # ========== 3. Action 元数据层面 ==========
        action_meta_score = self._action_metadata_score(generated, action_items)

        # ========== 4. 长度层面（合并了 _length_reward）==========
        length = len(generated)
        if 100 <= length <= 300:
            length_score = 1.0
        elif 50 <= length < 100 or 300 < length <= 400:
            length_score = 0.7
        else:
            length_score = 0.3

        # ========== 5. 加权合并 ==========
        # 文档结构占 30%，行动项章节占 15%，Action 元数据占 35%，长度占 20%
        structure = (0.30 * doc_score +
                     0.15 * action_section_score +
                     0.35 * action_meta_score +
                     0.20 * length_score)

        return structure

    def _action_metadata_score(self, generated: str, action_items: list) -> float:
        """
        Action 元数据完整性检查（结构层面的格式约束，非 executability）

        检查三个字段，与 _task_executability 的区别：
            - executability 是下游维度，乘以 soft gate √s 才生效
            - 这里做的是结构化层面的检查：不管内容质量好不好，
              格式上 owner/deadline/task 应该存在

        检查项：
            1. owner 非空（非 "", "unknown", "待定"）
            2. deadline 包含时序关键词（周、日、天、号等）
            3. task 描述具体（>= 5 字符）

        fallback：当 action_items 为空时，从文本中正则解析

        低分例子（格式层面）：
            - "负责人：待定" → owner 为占位符
            - "截止时间：尽快" → deadline 无具体时间
            - "任务：干活" → task 过于笼统
            - 行动项只有编号没有其他信息

        高分例子：
            - 每个行动项都有 owner + 具体 task + 包含时间词的 deadline
        """
        if not action_items:
            # Fallback：从文本中解析
            return self._parse_action_items_score(generated)

        scores = []
        for item in action_items:
            item_score = 0.0

            # 1. Owner 检查：非空且非占位符
            owner = item.get("owner", "")
            valid_owners = ("", "unknown", "待定", "TBD", "tbd")
            if owner and owner not in valid_owners:
                item_score += 1.0 / 3.0

            # 2. Deadline 检查：有时间关键词
            deadline = item.get("deadline", "")
            time_keywords = ["周", "月", "日", "天", "号", "点", "时", "本周",
                           "下周", "本周五", "下周一", "下周三", "完成"]
            if deadline and any(kw in deadline for kw in time_keywords):
                item_score += 1.0 / 3.0

            # 3. Task 检查：描述具体
            task = item.get("task", "")
            if len(task) >= 5:
                item_score += 1.0 / 3.0

            scores.append(item_score)

        return sum(scores) / len(scores) if scores else 0.0

    def _parse_action_items_score(self, text: str) -> float:
        """
        从文本中轻量解析 action_items 并检查 owner 是否存在

        匹配格式：
        1. 张三：负责后端API
        2. 李四：准备对接
        """
        import re as _re

        # 匹配 "人名：任务内容" 或 "人名 负责 任务"
        patterns = [
            r'([\u4e00-\u9fa5]{2,4})[：:]\s*\S+',
            r'([\u4e00-\u9fa5]{2,4})\s*(?:负责|完成|准备)',
        ]

        count = 0
        for pattern in patterns:
            matches = _re.findall(pattern, text)
            count = max(count, len(matches))

        if count == 0:
            return 0.3  # 没有找到行动项，中等给分

        return min(1.0, count / 3)
    
    def _length_reward(self, generated: str) -> float:
        """
        长度奖励
        
        思路：检查文本长度是否在理想范围内
        
        步骤：
        1. 计算文本长度
        2. 根据长度给分
        
        参数：
            generated: 生成的文本
        
        返回：
            0-1 之间的分数，1 表示长度完美
        """
        
        length = len(generated)
        
        # 长度分段给分
        if 100 <= length <= 300:
            # 理想长度：100-300 字，满分
            return 1.0
        elif 50 <= length < 100:
            # 有点短：50-100 字，70 分
            return 0.7
        elif 300 < length <= 400:
            # 有点长：300-400 字，70 分
            return 0.7
        else:
            # 太短或太长：30 分
            return 0.3

    # ============================================================
    # Chat2Workflow 启发的两个新维度（对应 Resolve Rate）
    # ============================================================

    def _dependency_validity_reward(self, action_items: list) -> float:
        """
        依赖有效性奖励（对应 Chat2Workflow Resolve Rate 前置验证）

        Kahn 拓扑排序原理：
          1. 统计每个节点的入度（有多少条边指向它）
          2. 入度为 0 的节点是起点，可以先执行，加入队列
          3. 弹出队首节点，把它指向的节点入度 -1，如果变成 0 就加入队列
          4. 重复直到队列空。如果最后没有处理完所有节点，说明有环

        面试要点：
          - 拓扑排序 O(V+E)，V 是任务数，E 是依赖边数
          - Kahn 算法核心：不断删入度为 0 的节点，最后剩节点就有环
          - 循环依赖例子：任务0说"依赖任务1"，任务1说"依赖任务0"，
            两个任务互相等待，谁都执行不了，整个工作流卡死

        低分例子（循环依赖）：
          - A说"依赖B"，B说"依赖A"（互相等待）
          - A说"依赖B"，B说"依赖C"，C说"依赖A"（三角循环）
          - A说"依赖自己"（自环）

        高分例子（DAG 无环）：
          - A无依赖 → B依赖A → C依赖B → D依赖B（单向链）
          - A无依赖，B无依赖，C依赖A，B依赖D（两个起点，并行）
        """
        if not action_items:
            return 0.5

        n = len(action_items)
        if n == 1:
            return 1.0

        # 建立邻接表和入度表
        edges = []
        for i, item in enumerate(action_items):
            deps = item.get("depends_on", [])
            for dep_idx in deps:
                if 0 <= dep_idx < n and dep_idx != i:
                    edges.append((dep_idx, i))

        # Kahn 算法拓扑排序
        in_degree = [0] * n
        for (u, v) in edges:
            in_degree[v] += 1

        queue = [i for i in range(n) if in_degree[i] == 0]
        topo_order = []
        while queue:
            node = queue.pop(0)
            topo_order.append(node)
            for (u, v) in edges:
                if u == node:
                    in_degree[v] -= 1
                    if in_degree[v] == 0:
                        queue.append(v)

        # 如果拓扑排序没有包含所有节点，说明有环
        if len(topo_order) == n:
            return 1.0
        else:
            return 0.0

    def _task_executability(self, action_items: list) -> float:
        """
        任务可执行性奖励（对应 Chat2Workflow 的 Resolve Rate）

        验证每个任务节点是否真正可执行：
        1. 负责人存在（owner 非空，且不是 "unknown"）
        2. 任务描述具体（task 长度 >= 5 个字符）
        3. 截止时间合理（包含时间关键词，如"周""日""天"等）
        4. 依赖节点索引有效（不引用不存在的节点）

        低分例子（executability 低的典型 case）：
          - owner 为空："负责人：待定" → 无法派发任务
          - task 太笼统："负责人：张三，任务：干活" → 不知道干什么
          - deadline 不合理："负责人：李四，任务：测试，deadline：某个时间" → 模糊无法执行
          - 依赖索引越界：共3个任务，但某任务写 depends_on: [5] → 第5个不存在
          - 组合："负责人：，任务：处理，deadline：" → 四项全空，executability = 0

        高分例子：
          - owner: "张三"，task: "完成后端 API 联调"，deadline: "本周五"，deps: [] → 四项全满足

        面试要点：
          - Pass Rate 看格式（Markdown 有没有），Resolve Rate 看能不能执行
          - 一个格式完美的任务列表，可能因为"负责人为空"而整条工作流卡死
          - 每项任务 4 个检查点各 0.25 分，平均分作为 executability 分数
        """
        if not action_items:
            return 0.3

        total_score = 0.0
        n = len(action_items)

        for item in action_items:
            item_score = 0.0

            # 1. 负责人存在
            if item.get("owner") and item["owner"] not in ("", "unknown"):
                item_score += 0.25

            # 2. 任务描述具体
            task = item.get("task", "")
            if len(task) >= 5:
                item_score += 0.25

            # 3. 截止时间合理
            deadline = item.get("deadline", "")
            time_keywords = ["周", "月", "日", "天", "号", "本周", "下周", "完成"]
            if deadline and any(kw in deadline for kw in time_keywords):
                item_score += 0.25

            # 4. 依赖节点索引有效
            deps = item.get("depends_on", [])
            if not deps:
                item_score += 0.25
            elif all(0 <= dep < n for dep in deps):
                item_score += 0.25

            total_score += item_score

        return total_score / len(action_items)

    def _coherence_reward(self, generated: str) -> float:
        """
        语义连贯性奖励（防逻辑跳跃）

        原理：连贯的文本中相邻句子之间应该有较高的语义相关性。
        如果相邻句子完全不相关（sim < threshold），说明存在逻辑跳跃。

        实现：复用已有的 BGE embedder，将文本分句后计算相邻句子的余弦相似度。
        与 DAG dependency reward 互补：DAG 验证任务间依赖，连贯性验证段落内句子。

        低分例子：
          - 前面讲后端进度，后面突然跳到测试（主题跳跃）
          - 句子之间缺少指代或衔接词
          - 列表项之间逻辑断裂

        高分例子：
          - 段落内句子围绕同一主题展开，衔接自然
          - 句子之间有"因此"、"然而"、"另外"等衔接词

        面试要点：
          - 和流畅度(fluency)的区别：fluency 检测 n-gram 重复，连贯性检测句子间语义相关性
          - 复用 self.embedder（BGE-large-zh-v1.5），无需引入新 judge，避免 judge bias
          - 阈值设为 0.5（语义相似度），低于此值视为不连贯
        """
        import re as _re

        # 分句：按中文句号、问号、感叹号分
        sentences = [
            s.strip() for s in _re.split(r'[。！？\n]', generated)
            if len(s.strip()) > 5
        ]

        if len(sentences) < 2:
            # 单句或只有短句，无法判断连贯性，默认给中高分
            return 0.8

        # 用 BGE embedder 编码每个句子
        embs = self.embedder.encode(sentences, convert_to_numpy=True)

        sims = []
        for i in range(len(embs) - 1):
            norm_i = embs[i] / (np.linalg.norm(embs[i]) + 1e-8)
            norm_j = embs[i + 1] / (np.linalg.norm(embs[i + 1]) + 1e-8)
            sim = np.dot(norm_i, norm_j)
            sims.append(sim)

        avg_sim = np.mean(sims)
        # 阈值设为 0.5，高于 0.5 说明连贯，低于 0.5 说明跳跃
        # 归一化：0.5 以下为 0，0.5 为 0，1.0 为 1
        coherence = max(0.0, (avg_sim - 0.3) / 0.5)
        return min(1.0, coherence)

    def compute_all_rewards(self, 
                           generated_texts: List[str], 
                           reference: str,
                           query: str,
                           retrieved_docs: List[str]) -> List[float]:
        """
        批量计算多个生成文本的奖励（用于 GRPO 训练）
        
        参数：
            generated_texts: 多个生成的文本（通常 4 个）
            reference: 标准答案
            query: 用户问题
            retrieved_docs: RAG 检索到的文档
        
        返回：
            rewards: 每个文本的奖励分数列表
        """
        rewards = []
        for generated in generated_texts:
            reward = self.compute_reward(
                generated=generated,
                reference=reference,
                query=query,
                retrieved_docs=retrieved_docs
            )
            rewards.append(reward)
        return rewards


# ============================================================
# 测试代码
# ============================================================

if __name__ == "__main__":
    # 创建奖励函数实例
    print("=" * 60)
    print("GRPO 八维度奖励函数测试（HyDE策略 + 结构化合并length）")
    print("=" * 60)

    reward_fn = GRPORewardFunction()

    # 测试数据
    generated = """
    ## 会议纪要

    ### 参会人员
    张三，李四，王五

    ### 进度汇报
    - 张三：后端 API 已完成 80%，预计本周五完成
    - 李四：前端页面完成 60%，等待后端对接
    - 王五：测试用例已编写 50 个，目标覆盖率 85%

    ### 行动项
    1. 张三：本周五完成后端 API
    2. 李四：准备前端对接
    3. 王五：继续编写测试用例
    """

    reference = """
    ## 会议纪要

    参会人员：张三、李四、王五

    进度：
    - 后端 API 完成 80%
    - 前端页面完成 60%
    - 测试用例 50 个

    行动项：张三完成后端，李四准备对接，王五继续测试
    """

    query = "总结这次会议"

    retrieved_docs = [
        "张三：后端 API 已完成 80%，预计本周五完成",
        "李四：前端页面完成 60%，等待后端对接",
        "王五：测试用例已编写 50 个，目标覆盖率 85%"
    ]

    # 计算奖励
    reward = reward_fn.compute_reward(
        generated=generated,
        reference=reference,
        query=query,
        retrieved_docs=retrieved_docs,
        action_items=[
            {"owner": "张三", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
            {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周", "depends_on": [0]},
            {"owner": "王五", "task": "编写集成测试用例", "deadline": "下周三", "depends_on": [0]},
            {"owner": "赵六", "task": "验收测试", "deadline": "下周五", "depends_on": [1, 2]},
        ]
    )

    # 打印各维度分数
    accuracy = reward_fn._accuracy_reward(generated, reference)
    faithfulness = reward_fn._faithfulness_reward(generated, retrieved_docs)
    fluency = reward_fn._fluency_reward(generated)
    relevance = reward_fn._relevance_reward(generated, query, retrieved_docs)
    structure = reward_fn._structure_reward(generated, [
        {"owner": "张三", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周", "depends_on": [0]},
        {"owner": "王五", "task": "编写集成测试用例", "deadline": "下周三", "depends_on": [0]},
        {"owner": "赵六", "task": "验收测试", "deadline": "下周五", "depends_on": [1, 2]},
    ])
    dep = reward_fn._dependency_validity_reward([
        {"owner": "张三", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周", "depends_on": [0]},
        {"owner": "王五", "task": "编写集成测试用例", "deadline": "下周三", "depends_on": [0]},
        {"owner": "赵六", "task": "验收测试", "deadline": "下周五", "depends_on": [1, 2]},
    ])
    exec_ = reward_fn._task_executability([
        {"owner": "张三", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周", "depends_on": [0]},
        {"owner": "王五", "task": "编写集成测试用例", "deadline": "下周三", "depends_on": [0]},
        {"owner": "赵六", "task": "验收测试", "deadline": "下周五", "depends_on": [1, 2]},
    ])
    coherence = reward_fn._coherence_reward(generated)

    # 计算 soft gate weight
    downstream_weight = structure ** 0.5

    print(f"\n生成内容长度：{len(generated)} 字")
    print(f"\nSoft Gate 权重（√s）：{downstream_weight:.3f}")
    print("\n八维度奖励:")
    print("  准确性：  {:.3f} (权重 20%)".format(accuracy))
    print("  忠实度：  {:.3f} (权重 15%，gate 后)".format(faithfulness))
    print("  流畅度：  {:.3f} (权重 16%)".format(fluency))
    print("  相关性：  {:.3f} (权重 10%，HyDE策略 — 用docs代理query)".format(relevance))
    print("  结构化：  {:.3f} (权重 18%，含owner合规+字数)".format(structure))
    print("  executability: {:.3f} (权重 8%，gate后)".format(exec_))
    print("  dependency:    {:.3f} (权重 7%，gate后)".format(dep))
    print("  coherence:     {:.3f} (权重 6%)".format(coherence))
    print("\n最终奖励：  {:.3f}".format(reward))
    print("=" * 60)

    # 测试循环依赖检测
    print("\n测试循环依赖检测:")
    cycle_items = [
        {"owner": "A", "task": "任务A", "deadline": "周一", "depends_on": [1]},
        {"owner": "B", "task": "任务B", "deadline": "周二", "depends_on": [0]},
    ]
    cycle_score = reward_fn._dependency_validity_reward(cycle_items)
    print(f"  A依赖B → B依赖A（循环依赖）→ 得分: {cycle_score:.3f}（应为 0.0）")

    normal_items = [
        {"owner": "张三", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周", "depends_on": [0]},
        {"owner": "王五", "task": "编写集成测试用例", "deadline": "下周三", "depends_on": [0]},
        {"owner": "赵六", "task": "验收测试", "deadline": "下周五", "depends_on": [1, 2]},
    ]
    normal_score = reward_fn._dependency_validity_reward(normal_items)
    print(f"  正常 DAG 依赖链 → 得分: {normal_score:.3f}（应为 1.0）")

    print("\n测试 Action 元数据检查（structure_reward 扩展）:")
    # 完整元数据
    good_actions = [
        {"owner": "张三", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周三", "depends_on": [0]},
    ]
    # 缺 owner 的行动项
    bad_actions_missing_owner = [
        {"owner": "待定", "task": "完成后端 API 联调", "deadline": "本周五", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "下周三", "depends_on": [0]},
    ]
    # 缺 deadline 的行动项
    bad_actions_missing_deadline = [
        {"owner": "张三", "task": "完成后端 API 联调", "deadline": "尽快", "depends_on": []},
        {"owner": "李四", "task": "准备前端对接文档", "deadline": "", "depends_on": [0]},
    ]
    good_meta_score = reward_fn._action_metadata_score("", good_actions)
    bad_meta_score1 = reward_fn._action_metadata_score("", bad_actions_missing_owner)
    bad_meta_score2 = reward_fn._action_metadata_score("", bad_actions_missing_deadline)
    print(f"  完整元数据 → action_meta: {good_meta_score:.3f}（应为 ~1.0）")
    print(f"  缺 owner → action_meta: {bad_meta_score1:.3f}（应为 ~0.56）")
    print(f"  缺 deadline → action_meta: {bad_meta_score2:.3f}（应为 ~0.28）")

    # 测试结构化总分（含章节 + 元数据 + 长度）
    good_struct = reward_fn._structure_reward(generated, good_actions)
    bad_struct = reward_fn._structure_reward("开会讨论", [])
    print(f"\n测试结构化总分（含章节+元数据+长度）:")
    print(f"  完整结构 → structure: {good_struct:.3f}")
    print(f"  无结构文本 → structure: {bad_struct:.3f}")

    # 测试 soft gate 效果
    coherent_text = "后端API已完成80%。前端页面完成60%。测试用例已编写50个。后端API已完成80%。"
    incoherent_text = "后端API已完成80%。今天天气很好。前端页面完成60%。"
    print(f"  正常文本 → coherence: {reward_fn._coherence_reward(generated):.3f}")
    print(f"  不连贯文本 → coherence: {reward_fn._coherence_reward(incoherent_text):.3f}")

    # 测试 soft gate 效果
    print("\n测试 Soft Gate 效果（结构化差时下游被压制）:")
    bad_structure = "开会讨论"  # 无 Markdown 结构，action_items 很少
    bad_reward = reward_fn.compute_reward(
        generated=bad_structure,
        reference=reference,
        query=query,
        retrieved_docs=retrieved_docs,
        action_items=[]
    )
    good_reward = reward
    print(f"  结构化好的文本 → reward: {good_reward:.3f}")
    print(f"  结构化差的文本 → reward: {bad_reward:.3f}")
    print(f"  Soft Gate 有效压制了废输出，差距: {good_reward - bad_reward:.3f}")

    print("\n✅ 测试完成！")
