"""
GRPO 训练简化版 - 用于理解 GRPO 原理和面试讲解

这个文件帮助你理解 GRPO 训练流程，面试时可以手撕伪代码
"""

import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer
from typing import List, Tuple
import numpy as np


# ============================================================
# 1. GRPO 核心原理
# ============================================================

class GRPOTrainer:
    """
    GRPO (Group Relative Policy Optimization) 训练器
    
    核心思想:
    - 对同一 prompt 生成多个 response（比如 4 个）
    - 计算每个 response 的奖励（reward）
    - 计算组内相对优势：A_i = (r_i - mean(r)) / std(r)
    - 更新策略：最大化 log_prob * advantage
    """
    
    def __init__(self, model, tokenizer, kl_coef=0.1, lr=1e-5):
        self.model = model
        self.tokenizer = tokenizer
        self.kl_coef = kl_coef  # KL 惩罚系数
        self.optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    
    def compute_advantage(self, rewards: List[float]) -> torch.Tensor:
        """
        计算组内相对优势（GRPO 核心）
        
        Args:
            rewards: 同一 prompt 的多个 response 的奖励 [r1, r2, r3, r4]
        
        Returns:
            advantages: 优势值 [A1, A2, A3, A4]
        """
        rewards = torch.tensor(rewards, dtype=torch.float32)
        
        # 组内归一化：A_i = (r_i - mean(r)) / std(r)
        mean_reward = rewards.mean()
        std_reward = rewards.std() + 1e-8  # 防止除 0
        
        advantages = (rewards - mean_reward) / std_reward
        return advantages
    
    def grpo_loss(self, 
                  log_probs: torch.Tensor, 
                  advantages: torch.Tensor,
                  old_log_probs: torch.Tensor,
                  kl_div: torch.Tensor) -> torch.Tensor:
        """
        GRPO 损失函数
        
        L = -E[log(π_new/π_old) * A_i] + kl_coef * KL(π_new || π_old)
        
        Args:
            log_probs: 当前策略的 log 概率
            advantages: 优势值
            old_log_probs: 旧策略的 log 概率（用于 importance sampling）
            kl_div: KL 散度
        """
        # Importance ratio: π_new / π_old
        ratio = torch.exp(log_probs - old_log_probs)
        
        # Policy loss: -log(ratio) * advantage
        policy_loss = -ratio * advantages
        
        # KL penalty
        kl_loss = kl_div.mean()
        
        # Total loss
        loss = policy_loss.mean() + self.kl_coef * kl_loss
        return loss
    
    def train_step(self, 
                   prompts: List[str], 
                   generate_fn, 
                   reward_fn) -> float:
        """
        单步 GRPO 训练
        
        Args:
            prompts: prompt 列表 [prompt1, prompt2, ...]
            generate_fn: 生成函数，输入 prompt，输出 response
            reward_fn: 奖励函数，输入 response，输出 reward
        
        Returns:
            loss: 损失值
        """
        all_log_probs = []
        all_old_log_probs = []
        all_advantages = []
        
        for prompt in prompts:
            # 1. 对同一 prompt 生成多个 response（比如 4 个）
            responses = []
            rewards = []
            for _ in range(4):  # group_size = 4
                response = generate_fn(prompt)
                reward = reward_fn(response)
                responses.append(response)
                rewards.append(reward)
            
            # 2. 计算优势
            advantages = self.compute_advantage(rewards)
            
            # 3. 计算 log 概率
            for response in responses:
                log_prob = self.get_log_prob(prompt, response)
                old_log_prob = self.get_old_log_prob(prompt, response)
                
                all_log_probs.append(log_prob)
                all_old_log_probs.append(old_log_prob)
                all_advantages.append(advantages)
        
        # 4. 计算 KL 散度
        kl_div = self.compute_kl_divergence()
        
        # 5. 计算损失并反向传播
        loss = self.grpo_loss(
            torch.stack(all_log_probs),
            torch.stack(all_advantages),
            torch.stack(all_old_log_probs),
            kl_div
        )
        
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        
        return loss.item()
    
    def get_log_prob(self, prompt: str, response: str) -> torch.Tensor:
        """计算当前策略的 log 概率"""
        # 实现细节：forward pass 获取 log_probs
        pass
    
    def get_old_log_prob(self, prompt: str, response: str) -> torch.Tensor:
        """计算旧策略的 log 概率（用 reference model）"""
        pass
    
    def compute_kl_divergence(self) -> torch.Tensor:
        """计算 KL 散度：KL(π_new || π_ref)"""
        pass


# ============================================================
# 2. 奖励函数设计（面试重点）
# ============================================================

class RewardFunction:
    """
    多维度奖励函数（面试必问）
    """
    
    def __init__(self, embedder=None):
        self.embedder = embedder  # BGE 嵌入模型
    
    def compute_reward(self, 
                       generated: str, 
                       reference: str, 
                       query: str,
                       retrieved_docs: List[str] = None) -> float:
        """
        计算多维度奖励
        
        reward = 0.4 * accuracy + 0.3 * fluency + 0.3 * relevance
        """
        # 1. 准确性奖励（基于 reference 或 RAG 检索）
        accuracy_reward = self._accuracy_reward(generated, reference, retrieved_docs)
        
        # 2. 流畅度奖励（基于 n-gram 重复度）
        fluency_reward = self._fluency_reward(generated)
        
        # 3. 相关性奖励（基于 query 相似度）
        relevance_reward = self._relevance_reward(generated, query)
        
        # 加权求和
        reward = (0.4 * accuracy_reward + 
                  0.3 * fluency_reward + 
                  0.3 * relevance_reward)
        
        return reward
    
    def _accuracy_reward(self, generated: str, reference: str, 
                         retrieved_docs: List[str] = None) -> float:
        """
        准确性奖励
        
        如果有 reference：计算与 reference 的相似度
        如果有 retrieved_docs：计算与检索文档的匹配度
        """
        if reference:
            # 基于嵌入相似度
            gen_emb = self.embedder.encode(generated)
            ref_emb = self.embedder.encode(reference)
            sim = cosine_similarity(gen_emb, ref_emb)
            return (sim + 1) / 2  # 归一化到 [0, 1]
        
        if retrieved_docs:
            # 基于与检索文档的匹配度
            doc_scores = []
            for doc in retrieved_docs:
                gen_emb = self.embedder.encode(generated)
                doc_emb = self.embedder.encode(doc)
                sim = cosine_similarity(gen_emb, doc_emb)
                doc_scores.append(sim)
            return max(doc_scores)
        
        return 0.5  # 默认中性分数
    
    def _fluency_reward(self, generated: str) -> float:
        """
        流畅度奖励
        
        基于 n-gram 重复度：重复越少越流畅
        """
        ngrams = self._extract_ngrams(generated, n=3)
        if len(ngrams) == 0:
            return 0.0
        
        # 重复度 = 重复的 n-gram 数量 / 总 n-gram 数量
        unique_ratio = len(set(ngrams)) / len(ngrams)
        
        # 长度惩罚（太短或太长都不好）
        length = len(generated)
        if length < 10 or length > 500:
            length_penalty = 0.5
        else:
            length_penalty = 1.0
        
        return unique_ratio * length_penalty
    
    def _relevance_reward(self, generated: str, query: str) -> float:
        """
        相关性奖励
        
        基于生成结果与 query 的语义相似度
        """
        gen_emb = self.embedder.encode(generated)
        query_emb = self.embedder.encode(query)
        sim = cosine_similarity(gen_emb, query_emb)
        return (sim + 1) / 2
    
    def _extract_ngrams(self, text: str, n: int = 3) -> List[str]:
        """提取 n-gram"""
        chars = list(text)
        return ["".join(chars[i:i+n]) for i in range(len(chars) - n + 1)]


# ============================================================
# 3. 偏好数据构建（面试重点）
# ============================================================

class PreferenceDataBuilder:
    """
    偏好数据集构建（chosen / rejected 对）
    """
    
    def build_preference_data(self, 
                              prompts: List[str], 
                              model_strong, 
                              model_weak) -> List[dict]:
        """
        构建偏好数据
        
        方法 1: 强模型生成 chosen，弱模型生成 rejected
        """
        data = []
        for prompt in prompts:
            # 强模型生成（chosen）
            chosen = model_strong.generate(prompt)
            
            # 弱模型生成（rejected）
            rejected = model_weak.generate(prompt)
            
            # 构建数据对
            data.append({
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected
            })
        
        return data
    
    def augment_data(self, 
                     data: List[dict], 
                     augment_fn) -> List[dict]:
        """
        数据增强
        
        方法：回译、改写、同义词替换
        """
        augmented = []
        for sample in data:
            # 原始样本
            augmented.append(sample)
            
            # 回译增强（中→英→中）
            prompt_trans = augment_fn.translate(sample["prompt"])
            chosen_trans = augment_fn.translate(sample["chosen"])
            rejected_trans = augment_fn.translate(sample["rejected"])
            
            augmented.append({
                "prompt": prompt_trans,
                "chosen": chosen_trans,
                "rejected": rejected_trans
            })
        
        return augmented  # 数据量扩充 2-3 倍


# ============================================================
# 4. 训练流程（面试可以手撕的伪代码）
# ============================================================

def grpo_training_pipeline():
    """
    GRPO 训练完整流程（伪代码）
    """
    # 1. 加载模型和数据
    model = AutoModelForCausalLM.from_pretrained("Qwen3-30B")
    tokenizer = AutoTokenizer.from_pretrained("Qwen3-30B")
    trainer = GRPOTrainer(model, tokenizer)
    
    # 2. 准备数据
    prompts = load_prompts("train_data.jsonl")  # 1000+ prompts
    
    # 3. 定义奖励函数
    reward_fn = RewardFunction(embedder=bge_model)
    
    # 4. 训练循环
    for epoch in range(3):  # 3 epochs
        for batch_idx in range(len(prompts) // batch_size):
            batch_prompts = prompts[batch_idx:batch_idx+batch_size]
            
            # GRPO 训练步
            loss = trainer.train_step(
                prompts=batch_prompts,
                generate_fn=model.generate,
                reward_fn=reward_fn.compute_reward
            )
            
            # 打印日志
            if batch_idx % 10 == 0:
                print(f"Epoch {epoch}, Batch {batch_idx}, Loss: {loss:.4f}")
    
    # 5. 保存模型
    model.save_pretrained("qwen3-30b-grpo")


# ============================================================
# 5. 面试常见问题与回答
# ============================================================

"""
Q1: GRPO 和 PPO 的区别？
A1:
- PPO: 需要 critic 模型估计 value，计算复杂
- GRPO: 不需要 critic，用组内相对优势代替，计算简单
- GRPO 优势：A_i = (r_i - mean(r)) / std(r)

Q2: 为什么用 GRPO 不用 PPO？
A2:
- 计算效率：GRPO 不需要训练 critic 模型
- 实现简单：GRPO 只需奖励函数，PPO 需要 value 网络
- 效果相当：实验中 GRPO 与 PPO 效果接近

Q3: 奖励函数如何设计？
A3:
reward = 0.4 * accuracy + 0.3 * fluency + 0.3 * relevance
- accuracy: 与 reference 或检索文档的相似度
- fluency: n-gram 重复度（越低越流畅）
- relevance: 与 query 的语义相似度

Q4: 偏好数据如何构建？
A4:
- 强模型生成 chosen，弱模型生成 rejected
- 人工标注：标注员对多个 response 打分
- 数据增强：回译、改写扩充数据

Q5: 训练不稳定怎么办？
A5:
- 调小 learning rate（1e-5 → 5e-6）
- 增加 KL 惩罚（0.1 → 0.2）
- gradient clipping（max_norm=1.0）
- warmup + cosine decay
"""


# ============================================================
# 6. 关键超参数（面试可以提）
# ============================================================

GRPO_HYPERPARAMS = {
    "learning_rate": 1e-5,
    "batch_size": 32,
    "group_size": 4,  # 每个 prompt 生成 4 个 response
    "kl_coef": 0.1,
    "epochs": 3,
    "max_length": 2048,
    "gradient_clipping": 1.0,
    "warmup_ratio": 0.1,
    "lr_scheduler": "cosine",
}

"""
训练配置（以 Qwen3-30B 为例）：
- GPU: 8×A100 80GB
- SFT 阶段：3 天
- RLHF 阶段：5 天
- 数据量：1000+ prompts
"""


if __name__ == "__main__":
    # 这个文件主要用于理解 GRPO 原理
    # 面试时可以手撕核心代码（compute_advantage 和 reward function）
    print("GRPO 训练简化版 - 用于面试准备")
    print("重点掌握：")
    print("1. GRPO 优势计算：A_i = (r_i - mean(r)) / std(r)")
    print("2. 奖励函数设计：accuracy + fluency + relevance")
    print("3. 偏好数据构建：强模型 chosen + 弱模型 rejected")
