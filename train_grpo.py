"""
GRPO 强化学习训练脚本 - 会议纪要生成质量优化

基于 GRPO (Group Relative Policy Optimization) 算法，对 SFT 后的模型进行强化学习微调
通过多维度奖励函数（准确性、流畅度、相关性）优化生成质量

用法:
    # 单卡训练
    python train_grpo.py \
        --model_name_or_path saves/sft_model \
        --train_data data/grpo_train_data.jsonl \
        --output_dir saves/grpo_model \
        --num_generations 4 \
        --per_device_train_batch_size 1 \
        --num_train_epochs 3 \
        --learning_rate 1e-6

    # 多卡训练 (学校 A100)
    torchrun --nproc_per_node=4 train_grpo.py \
        --model_name_or_path saves/sft_model \
        --train_data data/grpo_train_data.jsonl \
        --output_dir saves/grpo_model \
        --num_generations 4 \
        --per_device_train_batch_size 2 \
        --num_train_epochs 3 \
        --learning_rate 1e-6
"""

import os
import json
import random
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import torch
import torch.nn as nn
import numpy as np
from torch.utils.data import Dataset, DataLoader
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    HfArgumentParser,
    TrainingArguments,
)
from tqdm import tqdm
from datetime import datetime


# ============================================================
# 1. 参数定义
# ============================================================

@dataclass
class ModelArguments:
    """模型参数"""
    model_name_or_path: str = field(
        default="saves/sft_model",
        metadata={"help": "SFT 模型路径"}
    )
    ref_model_name_or_path: Optional[str] = field(
        default=None,
        metadata={"help": "Reference 模型路径（用于 KL 散度）"}
    )
    use_lora: bool = field(
        default=False,
        metadata={"help": "GRPO 阶段通常全量微调"}
    )


@dataclass
class DataArguments:
    """数据参数"""
    train_data: str = field(
        default="data/grpo_train_data.jsonl",
        metadata={"help": "训练数据路径 (JSONL 格式)"}
    )
    max_prompt_length: int = field(
        default=1024,
        metadata={"help": "最大 prompt 长度"}
    )
    max_response_length: int = field(
        default=1024,
        metadata={"help": "最大 response 长度"}
    )


@dataclass
class GRPOTrainingArguments(TrainingArguments):
    """GRPO 训练参数"""
    output_dir: str = field(
        default="saves/grpo_model",
        metadata={"help": "输出目录"}
    )
    num_generations: int = field(
        default=4,
        metadata={"help": "每个 prompt 生成的 response 数量（group size）"}
    )
    per_device_train_batch_size: int = field(
        default=1,
        metadata={"help": "每设备批次大小"}
    )
    num_train_epochs: int = field(
        default=3,
        metadata={"help": "训练轮数"}
    )
    learning_rate: float = field(
        default=1e-6,
        metadata={"help": "学习率"}
    )
    kl_coef: float = field(
        default=0.1,
        metadata={"help": "KL 散度惩罚系数"}
    )
    temperature: float = field(
        default=0.7,
        metadata={"help": "采样温度"}
    )
    top_p: float = field(
        default=0.9,
        metadata={"help": "Top-p 采样"}
    )
    logging_steps: int = field(
        default=10,
        metadata={"help": "日志记录步数"}
    )
    save_steps: int = field(
        default=100,
        metadata={"help": "保存模型步数"}
    )
    fp16: bool = field(
        default=True,
        metadata={"help": "是否使用 FP16"}
    )
    gradient_accumulation_steps: int = field(
        default=4,
        metadata={"help": "梯度累积步数"}
    )
    max_grad_norm: float = field(
        default=1.0,
        metadata={"help": "梯度裁剪"}
    )


# ============================================================
# 2. 奖励函数
# ============================================================

class MultiDimensionReward:
    """
    多维度奖励函数（面试重点）

    reward = 0.4 * accuracy + 0.3 * fluency + 0.3 * relevance
    """

    def __init__(self, embedder=None):
        self.embedder = embedder
        self._init_embedder()

    def _init_embedder(self):
        """初始化嵌入模型"""
        try:
            from sentence_transformers import SentenceTransformer
            self.embedder = SentenceTransformer("bge-large-zh-v1.5")
            print("已加载 BGE 嵌入模型")
        except ImportError:
            print("警告：sentence-transformers 未安装，使用简化奖励函数")
            self.embedder = None

    def compute_reward(
        self,
        generated: str,
        reference: Optional[str] = None,
        query: Optional[str] = None,
        retrieved_docs: Optional[List[str]] = None
    ) -> float:
        """
        计算多维度奖励

        Args:
            generated: 生成的文本
            reference: 参考文本（ground truth）
            query: 原始查询
            retrieved_docs: 检索到的文档

        Returns:
            奖励分数 [0, 1]
        """
        # 1. 准确性奖励
        accuracy_reward = self._accuracy_reward(generated, reference, retrieved_docs)

        # 2. 流畅度奖励
        fluency_reward = self._fluency_reward(generated)

        # 3. 相关性奖励
        relevance_reward = self._relevance_reward(generated, query)

        # 加权求和
        reward = (0.4 * accuracy_reward +
                  0.3 * fluency_reward +
                  0.3 * relevance_reward)

        return reward

    def _accuracy_reward(self, generated: str, reference: Optional[str],
                         retrieved_docs: Optional[List[str]]) -> float:
        """准确性奖励"""
        if reference and self.embedder:
            # 基于嵌入相似度
            gen_emb = self.embedder.encode(generated)
            ref_emb = self.embedder.encode(reference)
            from sklearn.metrics.pairwise import cosine_similarity
            sim = cosine_similarity([gen_emb], [ref_emb])[0][0]
            return (sim + 1) / 2  # 归一化到 [0, 1]

        if retrieved_docs and self.embedder:
            # 基于与检索文档的匹配度
            gen_emb = self.embedder.encode(generated)
            doc_scores = []
            for doc in retrieved_docs:
                doc_emb = self.embedder.encode(doc)
                from sklearn.metrics.pairwise import cosine_similarity
                sim = cosine_similarity([gen_emb], [doc_emb])[0][0]
                doc_scores.append(sim)
            return max(doc_scores) if doc_scores else 0.5

        # 简化版本：基于长度和关键词匹配
        return self._simple_accuracy_reward(generated, reference)

    def _simple_accuracy_reward(self, generated: str, reference: Optional[str]) -> float:
        """简化准确性奖励（无 embedder 时使用）"""
        if not reference:
            return 0.5

        # 基于字符重叠度
        gen_chars = set(generated)
        ref_chars = set(reference)

        overlap = len(gen_chars & ref_chars)
        union = len(gen_chars | ref_chars)

        if union == 0:
            return 0.0

        jaccard = overlap / union
        return jaccard

    def _fluency_reward(self, generated: str) -> float:
        """流畅度奖励 - 基于 n-gram 重复度"""
        if len(generated) < 3:
            return 0.0

        # 提取 3-gram
        ngrams = self._extract_ngrams(generated, n=3)
        if len(ngrams) == 0:
            return 0.0

        # 唯一 n-gram 比例
        unique_ratio = len(set(ngrams)) / len(ngrams)

        # 长度惩罚
        length = len(generated)
        if length < 10:
            length_penalty = 0.3
        elif length > 500:
            length_penalty = 0.5
        else:
            length_penalty = 1.0

        return unique_ratio * length_penalty

    def _relevance_reward(self, generated: str, query: Optional[str]) -> float:
        """相关性奖励"""
        if not query:
            return 0.5

        if self.embedder:
            gen_emb = self.embedder.encode(generated)
            query_emb = self.embedder.encode(query)
            from sklearn.metrics.pairwise import cosine_similarity
            sim = cosine_similarity([gen_emb], [query_emb])[0][0]
            return (sim + 1) / 2

        # 简化版本：基于关键词重叠
        gen_words = set(generated)
        query_words = set(query)
        overlap = len(gen_words & query_words)
        if len(query_words) == 0:
            return 0.5
        return min(1.0, overlap / len(query_words))

    def _extract_ngrams(self, text: str, n: int = 3) -> List[str]:
        """提取 n-gram"""
        chars = list(text)
        return ["".join(chars[i:i+n]) for i in range(len(chars) - n + 1)]


# ============================================================
# 3. 数据集
# ============================================================

class GRPODataset(Dataset):
    """GRPO 训练数据集"""

    def __init__(self, data_path: str, tokenizer: AutoTokenizer,
                 max_prompt_length: int = 1024):
        self.data = self._load_data(data_path)
        self.tokenizer = tokenizer
        self.max_prompt_length = max_prompt_length

    def _load_data(self, data_path: str) -> List[Dict]:
        """
        加载 GRPO 训练数据

        数据格式 (JSONL):
        {
            "prompt": "请总结以下会议记录：...",
            "reference": "标准答案/参考总结",
            "query": "原始查询（可选）"
        }
        """
        data = []
        with open(data_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                sample = json.loads(line)
                data.append(sample)
        return data

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int) -> Dict:
        sample = self.data[idx]

        # Tokenize prompt
        prompt = sample["prompt"]
        tokenized = self.tokenizer(
            prompt,
            truncation=True,
            max_length=self.max_prompt_length,
            padding=False,
            return_tensors="pt"
        )

        return {
            "prompt": prompt,
            "prompt_ids": tokenized["input_ids"].squeeze(0),
            "attention_mask": tokenized["attention_mask"].squeeze(0),
            "reference": sample.get("reference", None),
            "query": sample.get("query", None)
        }


# ============================================================
# 4. GRPO 训练器
# ============================================================

class GRPOTrainer:
    """
    GRPO 训练器

    核心思想:
    - 对同一 prompt 生成多个 response（比如 4 个）
    - 计算每个 response 的奖励（reward）
    - 计算组内相对优势：A_i = (r_i - mean(r)) / std(r)
    - 更新策略：最大化 log_prob * advantage
    """

    def __init__(
        self,
        model: AutoModelForCausalLM,
        ref_model: Optional[AutoModelForCausalLM],
        tokenizer: AutoTokenizer,
        args: GRPOTrainingArguments,
        reward_fn: MultiDimensionReward
    ):
        self.model = model
        self.ref_model = ref_model
        self.tokenizer = tokenizer
        self.args = args
        self.reward_fn = reward_fn

        # 优化器
        self.optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=args.learning_rate
        )

        # 设备
        self.device = args.device

        # 统计信息
        self.global_step = 0
        self.stats = {
            "rewards": [],
            "losses": [],
            "kl_divs": []
        }

    def compute_advantage(self, rewards: List[float]) -> torch.Tensor:
        """
        计算组内相对优势（GRPO 核心）

        A_i = (r_i - mean(r)) / std(r)
        """
        rewards = torch.tensor(rewards, dtype=torch.float32, device=self.device)

        mean_reward = rewards.mean()
        std_reward = rewards.std() + 1e-8  # 防止除 0

        advantages = (rewards - mean_reward) / std_reward
        return advantages

    def generate_responses(
        self,
        prompt_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        num_generations: int
    ) -> Tuple[List[torch.Tensor], List[str]]:
        """对同一 prompt 生成多个 response"""
        responses_ids = []
        responses_text = []

        self.model.eval()
        with torch.no_grad():
            for _ in range(num_generations):
                output = self.model.generate(
                    prompt_ids.unsqueeze(0).to(self.device),
                    attention_mask=attention_mask.unsqueeze(0).to(self.device),
                    max_new_tokens=self.args.max_response_length,
                    temperature=self.args.temperature,
                    top_p=self.args.top_p,
                    do_sample=True,
                    pad_token_id=self.tokenizer.eos_token_id
                )

                # 只取新生成的部分
                new_tokens = output[0, prompt_ids.shape[0]:]
                responses_ids.append(new_tokens)

                # 解码为文本
                response_text = self.tokenizer.decode(
                    new_tokens,
                    skip_special_tokens=True
                )
                responses_text.append(response_text)

        return responses_ids, responses_text

    def compute_log_probs(
        self,
        prompt_ids: torch.Tensor,
        response_ids: List[torch.Tensor]
    ) -> List[torch.Tensor]:
        """计算每个 response 的 log 概率"""
        log_probs = []

        self.model.eval()
        with torch.no_grad():
            for resp_ids in response_ids:
                # 拼接 prompt 和 response
                input_ids = torch.cat([prompt_ids, resp_ids]).unsqueeze(0).to(self.device)
                attention_mask = (input_ids != self.tokenizer.pad_token_id).long()

                outputs = self.model(
                    input_ids=input_ids,
                    attention_mask=attention_mask
                )

                # 只取 response 部分的 log 概率
                prompt_len = prompt_ids.shape[0]
                logits = outputs.logits[0, prompt_len-1:-1, :]  # [seq_len, vocab_size]
                labels = input_ids[0, prompt_len:]  # [seq_len]

                log_probs_seq = nn.functional.log_softmax(logits, dim=-1)
                token_log_probs = log_probs_seq.gather(-1, labels.unsqueeze(-1)).squeeze(-1)

                # 平均 log 概率
                avg_log_prob = token_log_probs.mean()
                log_probs.append(avg_log_prob)

        return log_probs

    def compute_kl_divergence(
        self,
        prompt_ids: torch.Tensor,
        response_ids: List[torch.Tensor]
    ) -> torch.Tensor:
        """计算 KL 散度：KL(π_new || π_ref)"""
        if self.ref_model is None:
            return torch.tensor(0.0, device=self.device)

        kl_divs = []

        with torch.no_grad():
            for resp_ids in response_ids:
                input_ids = torch.cat([prompt_ids, resp_ids]).unsqueeze(0).to(self.device)
                attention_mask = (input_ids != self.tokenizer.pad_token_id).long()

                # 当前模型输出
                outputs_new = self.model(input_ids=input_ids, attention_mask=attention_mask)
                logits_new = outputs_new.logits[0]

                # 参考模型输出
                outputs_ref = self.ref_model(input_ids=input_ids, attention_mask=attention_mask)
                logits_ref = outputs_ref.logits[0]

                # KL 散度
                log_probs_new = nn.functional.log_softmax(logits_new, dim=-1)
                probs_ref = nn.functional.softmax(logits_ref, dim=-1)

                kl = (probs_ref * (log_probs_new - torch.log(probs_ref + 1e-8))).sum(dim=-1).mean()
                kl_divs.append(kl)

        return torch.tensor(kl_divs).mean()

    def train_step(
        self,
        batch: Dict
    ) -> Dict:
        """单步 GRPO 训练"""
        prompt_ids = batch["prompt_ids"]
        attention_mask = batch["attention_mask"]
        reference = batch.get("reference")
        query = batch.get("query")

        # 1. 生成多个 response
        response_ids, response_texts = self.generate_responses(
            prompt_ids,
            attention_mask,
            self.args.num_generations
        )

        # 2. 计算每个 response 的奖励
        rewards = []
        for resp_text in response_texts:
            reward = self.reward_fn.compute_reward(
                generated=resp_text,
                reference=reference,
                query=query
            )
            rewards.append(reward)

        # 3. 计算优势
        advantages = self.compute_advantage(rewards)

        # 4. 计算 log 概率
        log_probs = self.compute_log_probs(prompt_ids, response_ids)

        # 5. 计算参考模型 log 概率（用于 importance sampling）
        if self.ref_model is not None:
            old_log_probs = self.compute_log_probs_with_model(
                self.ref_model, prompt_ids, response_ids
            )
        else:
            old_log_probs = log_probs

        # 6. 计算 KL 散度
        kl_div = self.compute_kl_divergence(prompt_ids, response_ids)

        # 7. 计算 GRPO 损失
        loss, avg_clip = self.grpo_loss(
            log_probs=log_probs,
            advantages=advantages,
            old_log_probs=old_log_probs,
            kl_div=kl_div
        )

        # 8. 反向传播
        self.optimizer.zero_grad()
        loss.backward()

        # 梯度裁剪
        torch.nn.utils.clip_grad_norm_(
            self.model.parameters(),
            self.args.max_grad_norm
        )

        self.optimizer.step()

        # 更新统计信息
        self.global_step += 1
        self.stats["rewards"].append(np.mean(rewards))
        self.stats["losses"].append(loss.item())
        self.stats["kl_divs"].append(kl_div.item())

        return {
            "loss": loss.item(),
            "reward": np.mean(rewards),
            "kl_div": kl_div.item(),
            "clip_fraction": avg_clip,
        }

    def compute_log_probs_with_model(
        self,
        model: AutoModelForCausalLM,
        prompt_ids: torch.Tensor,
        response_ids: List[torch.Tensor]
    ) -> List[torch.Tensor]:
        """使用指定模型计算 log 概率"""
        log_probs = []
        model.eval()
        with torch.no_grad():
            for resp_ids in response_ids:
                input_ids = torch.cat([prompt_ids, resp_ids]).unsqueeze(0).to(self.device)
                attention_mask = (input_ids != self.tokenizer.pad_token_id).long()

                outputs = model(input_ids=input_ids, attention_mask=attention_mask)

                prompt_len = prompt_ids.shape[0]
                logits = outputs.logits[0, prompt_len-1:-1, :]
                labels = input_ids[0, prompt_len:]

                log_probs_seq = nn.functional.log_softmax(logits, dim=-1)
                token_log_probs = log_probs_seq.gather(-1, labels.unsqueeze(-1)).squeeze(-1)

                avg_log_prob = token_log_probs.mean()
                log_probs.append(avg_log_prob)

        return log_probs

    def grpo_loss(
        self,
        log_probs: List[torch.Tensor],
        advantages: torch.Tensor,
        old_log_probs: List[torch.Tensor],
        kl_div: torch.Tensor,
        clip_eps: float = 0.2,
    ) -> Tuple[torch.Tensor, float]:
        """
        GRPO 损失函数（带 PPO-clip 和自适应 KL）

        L = -E[min(ratio * A, clip(ratio, 1-ε, 1+ε) * A)] + β * KL

        为什么要 clip？
        - 防止 importance ratio 过大导致策略更新幅度太大
        - 当 advantage > 0 时，ratio 被截断到 1+ε，防止过度强化
        - 当 advantage < 0 时，ratio 被截断到 1-ε，防止过度惩罚
        """
        # Importance ratio: π_new / π_old
        ratios = []
        for lp, olp in zip(log_probs, old_log_probs):
            ratio = torch.exp(lp - olp)
            ratios.append(ratio)

        # PPO-clip policy loss
        policy_losses = []
        clip_fractions = []
        for ratio, adv in zip(ratios, advantages):
            surr1 = ratio * adv
            surr2 = torch.clamp(ratio, 1.0 - clip_eps, 1.0 + clip_eps) * adv
            policy_loss = -torch.min(surr1, surr2)
            policy_losses.append(policy_loss)
            clip_fractions.append(
                float((torch.abs(ratio - 1.0) > clip_eps).float().mean())
            )

        policy_loss = torch.stack(policy_losses).mean()

        # Adaptive KL penalty (Lagrangian style)
        kl_target = 0.01
        kl_loss = kl_div
        if kl_div.item() > kl_target * 1.5:
            self.args.kl_coef = min(self.args.kl_coef * 1.5, 1.0)
        elif kl_div.item() < kl_target * 0.5:
            self.args.kl_coef = max(self.args.kl_coef / 1.5, 0.01)

        loss = policy_loss + self.args.kl_coef * kl_loss

        avg_clip = float(np.mean(clip_fractions))
        self.stats.setdefault("clip_fractions", []).append(avg_clip)
        return loss, avg_clip

    def save_model(self, output_dir: str):
        """保存模型"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        self.model.save_pretrained(output_dir)
        self.tokenizer.save_pretrained(output_dir)
        print(f"模型已保存到：{output_dir}")


# ============================================================
# 5. 训练主流程
# ============================================================

def main():
    # 解析参数
    parser = HfArgumentParser(
        (ModelArguments, DataArguments, GRPOTrainingArguments)
    )
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    print("=" * 60)
    print("GRPO 强化学习训练")
    print("=" * 60)
    print(f"模型路径：{model_args.model_name_or_path}")
    print(f"训练数据：{data_args.train_data}")
    print(f"输出目录：{training_args.output_dir}")
    print(f"Group Size: {training_args.num_generations}")
    print("=" * 60)

    # 加载模型和 tokenizer
    print("加载模型...")
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        trust_remote_code=True
    )

    model = AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        trust_remote_code=True,
        torch_dtype=torch.float16,
        device_map="auto"
    )

    # 加载参考模型（用于 KL 散度）
    ref_model = None
    if model_args.ref_model_name_or_path:
        print("加载参考模型...")
        ref_model = AutoModelForCausalLM.from_pretrained(
            model_args.ref_model_name_or_path,
            trust_remote_code=True,
            torch_dtype=torch.float16,
            device_map="auto"
        )
        ref_model.eval()

    # 准备数据集
    print("准备训练数据集...")
    train_dataset = GRPODataset(
        data_args.train_data,
        tokenizer,
        data_args.max_prompt_length
    )
    print(f"训练样本数：{len(train_dataset)}")

    # 创建数据加载器
    train_loader = DataLoader(
        train_dataset,
        batch_size=training_args.per_device_train_batch_size,
        shuffle=True,
        num_workers=2
    )

    # 初始化奖励函数
    reward_fn = MultiDimensionReward()

    # 初始化 GRPO 训练器
    trainer = GRPOTrainer(
        model=model,
        ref_model=ref_model,
        tokenizer=tokenizer,
        args=training_args,
        reward_fn=reward_fn
    )

    # 训练循环
    print("开始训练...")
    total_steps = len(train_loader) * training_args.num_train_epochs

    progress_bar = tqdm(total=total_steps, desc="Training")

    for epoch in range(training_args.num_train_epochs):
        for batch in train_loader:
            # 执行训练步
            metrics = trainer.train_step(batch)

            # 更新进度条
            progress_bar.update(1)
            progress_bar.set_postfix({
                "loss": f"{metrics['loss']:.4f}",
                "reward": f"{metrics['reward']:.4f}",
                "kl": f"{metrics['kl_div']:.4f}"
            })

            # 记录日志
            if trainer.global_step % training_args.logging_steps == 0:
                avg_reward = np.mean(trainer.stats["rewards"][-100:])
                avg_loss = np.mean(trainer.stats["losses"][-100:])
                avg_kl = np.mean(trainer.stats["kl_divs"][-100:])
                avg_clip = np.mean(trainer.stats.get("clip_fractions", [0])[-100:])
                print(f"\nStep {trainer.global_step}: "
                      f"loss={avg_loss:.4f}, reward={avg_reward:.4f}, "
                      f"kl={avg_kl:.4f}, clip={avg_clip:.2%}")

            # 保存模型
            if trainer.global_step % training_args.save_steps == 0:
                save_dir = os.path.join(
                    training_args.output_dir,
                    f"checkpoint-{trainer.global_step}"
                )
                trainer.save_model(save_dir)

    progress_bar.close()

    # 保存最终模型
    trainer.save_model(training_args.output_dir)

    # 保存训练统计
    stats_path = os.path.join(training_args.output_dir, "training_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump({
            "global_step": trainer.global_step,
            "final_avg_reward": float(np.mean(trainer.stats["rewards"][-100:])),
            "final_avg_loss": float(np.mean(trainer.stats["losses"][-100:]))
        }, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print("GRPO 训练完成！")
    print(f"最终模型保存到：{training_args.output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
