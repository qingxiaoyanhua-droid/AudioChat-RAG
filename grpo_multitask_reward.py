"""
GRPO 多维度奖励函数 - 多任务版本

支持：
1. 会议纪要生成
2. 行动项提取
3. 决策项提取

用途：GRPO 强化学习中，给生成的会议纪要打分
核心思想：reward = 0.35*纪要质量 + 0.35*行动项质量 + 0.20*决策项质量 + 0.10*整体格式
"""

import torch
import re
from typing import List, Dict, Tuple
from sentence_transformers import SentenceTransformer, CrossEncoder
from sklearn.metrics.pairwise import cosine_similarity


# ============================================================
# 多任务奖励函数
# ============================================================

class MultiTaskRewardFunction:
    """
    多任务 GRPO 奖励函数
    
    输入：
        - generated: 模型生成的完整输出（包含纪要、行动项、决策项）
        - reference: 标准答案（也包含三部分）
        - query: 用户的问题
    
    输出：
        - reward: 0-1 之间的分数
        - sub_rewards: 各子任务的分数（用于分析）
    """
    
    def __init__(self, 
                 embedder_model: str = "BAAI/bge-large-zh-v1.5",
                 reranker_model: str = "BAAI/bge-reranker-large"):
        """
        初始化奖励函数
        
        参数：
            embedder_model: BGE 嵌入模型名称
            reranker_model: BGE ReRanker 模型名称
        """
        print("正在加载多任务奖励函数模型...")
        
        # 1. 加载 BGE 嵌入模型（用于计算语义相似度）
        self.embedder = SentenceTransformer(embedder_model)
        
        # 2. 加载 BGE ReRanker 模型（用于精排）
        self.reranker = CrossEncoder(reranker_model)
        
        print("✅ 多任务奖励函数加载完成")
    
    def compute_reward(self, 
                       generated: str, 
                       reference: str,
                       query: str) -> Tuple[float, Dict[str, float]]:
        """
        计算多维度奖励（主函数）
        
        参数：
            generated: 模型生成的完整输出
            reference: 标准答案
            query: 用户的问题
        
        返回：
            reward: 总分数
            sub_rewards: 各子任务的分数
        """
        
        # ========== 1. 解析三个部分 ==========
        # 从 generated 和 reference 中提取三个部分
        gen_summary = self._extract_section(generated, "会议纪要")
        gen_actions = self._extract_section(generated, "行动项")
        gen_decisions = self._extract_section(generated, "决策项")
        
        ref_summary = self._extract_section(reference, "会议纪要")
        ref_actions = self._extract_section(reference, "行动项")
        ref_decisions = self._extract_section(reference, "决策项")
        
        # ========== 2. 计算各部分奖励 ==========
        
        # 2.1 会议纪要质量（35%）
        summary_reward = self._summary_reward(gen_summary, ref_summary)
        
        # 2.2 行动项质量（35%）
        actions_reward = self._actions_reward(gen_actions, ref_actions)
        
        # 2.3 决策项质量（20%）
        decisions_reward = self._decisions_reward(gen_decisions, ref_decisions)
        
        # 2.4 整体格式（10%）
        format_reward = self._format_reward(generated)
        
        # ========== 3. 加权求和 ==========
        reward = (
            0.35 * summary_reward +      # 会议纪要（35%）
            0.35 * actions_reward +      # 行动项（35%）
            0.20 * decisions_reward +    # 决策项（20%）
            0.10 * format_reward         # 整体格式（10%）
        )
        
        sub_rewards = {
            "summary": summary_reward,
            "actions": actions_reward,
            "decisions": decisions_reward,
            "format": format_reward
        }
        
        return reward, sub_rewards
    
    def _extract_section(self, text: str, section_name: str) -> str:
        """
        从文本中提取指定章节的内容
        
        参数：
            text: 完整文本
            section_name: 章节名称（如"会议纪要"、"行动项"、"决策项"）
        
        返回：
            章节内容
        """
        # 匹配 Markdown 格式：## 会议纪要 或 ### 行动项
        patterns = [
            rf"##\s*{section_name}\s*\n(.*?)(?=##|\Z)",  # ## 标题
            rf"###\s*{section_name}\s*\n(.*?)(?=###|\Z)", # ### 标题
            rf"{section_name}[:：]\s*\n(.*?)(?=\n\s*\n|\Z)", # 纯文本
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
        
        # 如果找不到章节，返回全文（降级处理）
        return text
    
    def _summary_reward(self, generated: str, reference: str) -> float:
        """
        会议纪要质量奖励
        
        评估维度：
        1. 语义相似度（和 reference 比）
        2. 信息完整性（是否覆盖关键点）
        3. 流畅度
        """
        
        # 1. 语义相似度（50%）
        if len(generated) < 10 or len(reference) < 10:
            sim_score = 0.0
        else:
            gen_emb = self.embedder.encode(generated, convert_to_numpy=True)
            ref_emb = self.embedder.encode(reference, convert_to_numpy=True)
            sim = cosine_similarity([gen_emb], [ref_emb])[0][0]
            sim_score = (sim + 1) / 2  # 归一化到 0-1
        
        # 2. 信息完整性（30%）
        # 检查 reference 中的关键信息是否在 generated 中出现
        completeness = self._check_completeness(generated, reference)
        
        # 3. 流畅度（20%）
        fluency = self._fluency_score(generated)
        
        # 加权求和
        summary_reward = 0.5 * sim_score + 0.3 * completeness + 0.2 * fluency
        
        return summary_reward
    
    def _actions_reward(self, generated: str, reference: str) -> float:
        """
        行动项质量奖励
        
        评估维度：
        1. 行动项召回率（reference 中的行动项有多少被提取）
        2. 行动项准确率（生成的行动项有多少是正确的）
        3. 格式规范性（是否有负责人、截止时间）
        """
        
        # 1. 解析行动项列表
        gen_actions = self._parse_action_items(generated)
        ref_actions = self._parse_action_items(reference)
        
        if len(ref_actions) == 0:
            # 如果没有参考行动项，只检查格式
            return self._action_format_score(gen_actions)
        
        # 2. 计算召回率（recall）
        # reference 中的行动项有多少在 generated 中出现
        recalled = 0
        for ref_action in ref_actions:
            for gen_action in gen_actions:
                if self._action_match(ref_action, gen_action):
                    recalled += 1
                    break
        recall = recalled / len(ref_actions) if ref_actions else 0.0
        
        # 3. 计算准确率（precision）
        # generated 中的行动项有多少是正确的
        matched = 0
        for gen_action in gen_actions:
            for ref_action in ref_actions:
                if self._action_match(gen_action, ref_action):
                    matched += 1
                    break
        precision = matched / len(gen_actions) if gen_actions else 0.0
        
        # 4. 计算 F1 分数
        if recall + precision > 0:
            f1 = 2 * recall * precision / (recall + precision)
        else:
            f1 = 0.0
        
        # 5. 格式规范性
        format_score = self._action_format_score(gen_actions)
        
        # 加权求和
        actions_reward = 0.6 * f1 + 0.4 * format_score
        
        return actions_reward
    
    def _decisions_reward(self, generated: str, reference: str) -> float:
        """
        决策项质量奖励
        
        评估维度：
        1. 决策项召回率
        2. 决策项准确率
        3. 决策信息完整性（是否有决策内容、时间等）
        """
        
        # 1. 解析决策项列表
        gen_decisions = self._parse_decision_items(generated)
        ref_decisions = self._parse_decision_items(reference)
        
        if len(ref_decisions) == 0:
            # 如果没有参考决策项，只检查格式
            return self._decision_format_score(gen_decisions)
        
        # 2. 计算召回率
        recalled = 0
        for ref_decision in ref_decisions:
            for gen_decision in gen_decisions:
                if self._decision_match(ref_decision, gen_decision):
                    recalled += 1
                    break
        recall = recalled / len(ref_decisions) if ref_decisions else 0.0
        
        # 3. 计算准确率
        matched = 0
        for gen_decision in gen_decisions:
            for ref_decision in ref_decisions:
                if self._decision_match(gen_decision, ref_decision):
                    matched += 1
                    break
        precision = matched / len(gen_decisions) if gen_decisions else 0.0
        
        # 4. 计算 F1 分数
        if recall + precision > 0:
            f1 = 2 * recall * precision / (recall + precision)
        else:
            f1 = 0.0
        
        # 5. 信息完整性
        completeness = self._decision_completeness(gen_decisions)
        
        # 加权求和
        decisions_reward = 0.6 * f1 + 0.3 * completeness + 0.1 * self._decision_format_score(gen_decisions)
        
        return decisions_reward
    
    def _format_reward(self, generated: str) -> float:
        """
        整体格式奖励
        
        检查：
        1. 是否有 Markdown 标题
        2. 是否有三个章节（纪要、行动项、决策项）
        3. 是否有列表
        """
        
        score = 0.0
        
        # 1. 检查 Markdown 标题
        if "##" in generated or "###" in generated:
            score += 0.3
        
        # 2. 检查三个章节
        sections = ["会议纪要", "行动项", "决策项"]
        for section in sections:
            if section in generated:
                score += 0.2
        # 三个章节都有，额外加分
        if all(s in generated for s in sections):
            score += 0.2
        
        # 3. 检查列表
        if "- " in generated or "1." in generated or "* " in generated:
            score += 0.2
        
        return min(1.0, score)
    
    # ============================================================
    # 辅助函数
    # ============================================================
    
    def _check_completeness(self, generated: str, reference: str) -> float:
        """
        检查信息完整性
        
        提取 reference 中的关键信息（数字、人名、时间），检查是否在 generated 中出现
        """
        
        # 提取数字
        ref_numbers = re.findall(r'\d+%', reference)
        gen_numbers = re.findall(r'\d+%', generated)
        
        if len(ref_numbers) == 0:
            return 1.0
        
        # 计算数字覆盖率
        covered = 0
        for num in ref_numbers:
            if num in generated:
                covered += 1
        number_coverage = covered / len(ref_numbers)
        
        # 提取人名（假设是中文名字，2-3 个字）
        ref_names = re.findall(r'[张王李赵刘陈杨黄周吴徐孙朱马胡郭何] [一二三四五六七八九十]?', reference)
        gen_names = re.findall(r'[张王李赵刘陈杨黄周吴徐孙朱马胡郭何] [一二三四五六七八九十]?', generated)
        
        if len(ref_names) > 0:
            name_coverage = len(set(ref_names) & set(gen_names)) / len(ref_names)
        else:
            name_coverage = 1.0
        
        # 加权
        completeness = 0.7 * number_coverage + 0.3 * name_coverage
        
        return completeness
    
    def _fluency_score(self, text: str) -> float:
        """
        流畅度评分
        
        基于 n-gram 重复度
        """
        
        if len(text) < 10:
            return 0.0
        
        # 提取 3-gram
        ngrams = [text[i:i+3] for i in range(len(text)-2)]
        
        if len(ngrams) == 0:
            return 0.0
        
        # 唯一 n-gram 比例
        unique_ratio = len(set(ngrams)) / len(ngrams)
        
        # 长度惩罚
        length = len(text)
        if length < 50:
            length_penalty = 0.5
        elif length > 500:
            length_penalty = 0.7
        else:
            length_penalty = 1.0
        
        fluency = unique_ratio * length_penalty
        
        return fluency
    
    def _parse_action_items(self, text: str) -> List[Dict]:
        """
        解析行动项
        
        期望格式：
        1. 张三：负责后端 API，本周五完成
        2. 李四：准备前端对接，下周完成
        
        返回：
            [{"owner": "张三", "task": "负责后端 API", "deadline": "本周五"}, ...]
        """
        
        actions = []
        
        # 匹配行动项格式
        patterns = [
            r'(\d+)[.、]\s*([张王李赵刘陈杨黄周吴徐孙朱马胡郭何][一二三四五六七八九十]?)[：:]\s*(.+?)(?=，|,|。|$)',
            r'([张王李赵刘陈杨黄周吴徐孙朱马胡郭何][一二三四五六七八九十]?)\s*负责\s*(.+?)(?=，|,|。|$)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.DOTALL)
            for match in matches:
                if len(match) == 3:
                    actions.append({
                        "owner": match[1],
                        "task": match[2].strip(),
                        "deadline": ""  # 可以从文本中提取
                    })
                elif len(match) == 2:
                    actions.append({
                        "owner": match[0],
                        "task": match[1].strip(),
                        "deadline": ""
                    })
        
        return actions
    
    def _parse_decision_items(self, text: str) -> List[Dict]:
        """
        解析决策项
        
        期望格式：
        1. 确定后端 API 本周五完成
        2. 同意前端页面延期到下周一
        
        返回：
            [{"decision": "确定后端 API 本周五完成", "time": "本周五"}, ...]
        """
        
        decisions = []
        
        # 匹配决策项格式
        patterns = [
            r'(\d+)[.、]\s*(确定 | 决定 | 同意 | 确认 | 通过)(.+?)(?=。|$)',
            r'(确定 | 决定 | 同意 | 确认 | 通过)(.+?)(?=。|$)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.DOTALL)
            for match in matches:
                if len(match) == 3:
                    decisions.append({
                        "type": match[1],
                        "content": match[2].strip()
                    })
                elif len(match) == 2:
                    decisions.append({
                        "type": match[0],
                        "content": match[1].strip()
                    })
        
        return decisions
    
    def _action_match(self, action1: Dict, action2: Dict) -> bool:
        """
        判断两个行动项是否匹配
        """
        
        # 负责人相同
        if action1.get("owner") == action2.get("owner"):
            return True
        
        # 任务内容语义相似
        if len(action1.get("task", "")) > 5 and len(action2.get("task", "")) > 5:
            emb1 = self.embedder.encode(action1["task"], convert_to_numpy=True)
            emb2 = self.embedder.encode(action2["task"], convert_to_numpy=True)
            sim = cosine_similarity([emb1], [emb2])[0][0]
            if sim > 0.7:
                return True
        
        return False
    
    def _decision_match(self, decision1: Dict, decision2: Dict) -> bool:
        """
        判断两个决策项是否匹配
        """
        
        # 决策类型相同且内容相似
        if decision1.get("type") == decision2.get("type"):
            emb1 = self.embedder.encode(decision1["content"], convert_to_numpy=True)
            emb2 = self.embedder.encode(decision2["content"], convert_to_numpy=True)
            sim = cosine_similarity([emb1], [emb2])[0][0]
            if sim > 0.7:
                return True
        
        return False
    
    def _action_format_score(self, actions: List[Dict]) -> float:
        """
        行动项格式评分
        
        检查：
        1. 是否有负责人
        2. 是否有具体任务
        3. 是否有截止时间
        """
        
        if len(actions) == 0:
            return 0.5  # 没有行动项，给中等分数
        
        score = 0.0
        
        for action in actions:
            if action.get("owner"):
                score += 0.4
            if action.get("task"):
                score += 0.4
            if action.get("deadline"):
                score += 0.2
        
        # 平均分
        score = score / (len(actions) * 1.0)
        
        return min(1.0, score)
    
    def _decision_format_score(self, decisions: List[Dict]) -> float:
        """
        决策项格式评分
        
        检查：
        1. 是否有决策类型
        2. 是否有决策内容
        """
        
        if len(decisions) == 0:
            return 0.5
        
        score = 0.0
        
        for decision in decisions:
            if decision.get("type"):
                score += 0.5
            if decision.get("content"):
                score += 0.5
        
        score = score / (len(decisions) * 1.0)
        
        return min(1.0, score)
    
    def _decision_completeness(self, decisions: List[Dict]) -> float:
        """
        决策信息完整性
        
        检查决策是否包含关键信息（时间、数字等）
        """
        
        if len(decisions) == 0:
            return 0.0
        
        score = 0.0
        
        for decision in decisions:
            content = decision.get("content", "")
            
            # 检查是否有时间信息
            if any(t in content for t in ["周", "月", "日", "天", "完成", "前"]):
                score += 0.5
            
            # 检查是否有数字信息
            if re.search(r'\d+', content):
                score += 0.5
        
        score = score / len(decisions)
        
        return min(1.0, score)


# ============================================================
# 测试代码
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("多任务 GRPO 奖励函数测试")
    print("=" * 60)
    
    reward_fn = MultiTaskRewardFunction()
    
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
1. 张三：负责后端 API，本周五完成
2. 李四：准备前端对接，下周完成
3. 王五：继续编写测试用例

### 决策项
1. 确定后端 API 本周五完成第一次集成测试
2. 同意前端页面延期到下周一
3. 通过测试覆盖率目标 85%
"""
    
    reference = """
## 会议纪要

### 参会人员
张三，李四，王五

### 进度汇报
- 张三：后端 API 完成 80%
- 李四：前端页面完成 60%
- 王五：测试用例 50 个

### 行动项
1. 张三：完成后端 API
2. 李四：准备对接
3. 王五：继续测试

### 决策项
1. 确定后端 API 本周五完成
2. 同意前端页面延期
3. 通过测试覆盖率 85%
"""
    
    query = "总结这次会议"
    
    # 计算奖励
    reward, sub_rewards = reward_fn.compute_reward(
        generated=generated,
        reference=reference,
        query=query
    )
    
    # 打印结果
    print("\n生成内容长度：{} 字".format(len(generated)))
    print("\n各维度分数:")
    print("  会议纪要：  {:.3f} (权重 35%)".format(sub_rewards["summary"]))
    print("  行动项：    {:.3f} (权重 35%)".format(sub_rewards["actions"]))
    print("  决策项：    {:.3f} (权重 20%)".format(sub_rewards["decisions"]))
    print("  整体格式：  {:.3f} (权重 10%)".format(sub_rewards["format"]))
    print("\n最终奖励：  {:.3f}".format(reward))
    print("=" * 60)
    
    # 测试低质量生成
    print("\n测试低质量生成:")
    low_quality = "后端完成 80%，前端 60%"
    
    reward_low, sub_rewards_low = reward_fn.compute_reward(
        generated=low_quality,
        reference=reference,
        query=query
    )
    
    print("低质量生成奖励：{:.3f}".format(reward_low))
    print("各维度：纪要{:.3f}, 行动项{:.3f}, 决策项{:.3f}, 格式{:.3f}".format(
        sub_rewards_low["summary"],
        sub_rewards_low["actions"],
        sub_rewards_low["decisions"],
        sub_rewards_low["format"]
    ))
    
    print("\n✅ 测试完成！")
