"""
SFT 监督微调训练脚本 - 会议纪要生成任务

用于对 Fun-Audio-Chat-8B 或 Qwen 系列模型进行监督微调，训练数据为会议纪要格式

用法:
    # 单卡训练
    python train_sft.py \
        --model_name_or_path /data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B \
        --train_data data/sft_train_data.jsonl \
        --output_dir saves/sft_model \
        --per_device_train_batch_size 2 \
        --gradient_accumulation_steps 4 \
        --num_train_epochs 3 \
        --learning_rate 1e-5

    # 多卡训练 (学校 A100)
    torchrun --nproc_per_node=8 train_sft.py \
        --model_name_or_path /data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B \
        --train_data data/sft_train_data.jsonl \
        --output_dir saves/sft_model \
        --per_device_train_batch_size 2 \
        --gradient_accumulation_steps 4 \
        --num_train_epochs 3 \
        --learning_rate 1e-5
"""

import os
import json
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from pathlib import Path

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    Trainer,
    HfArgumentParser,
    DataCollatorForLanguageModeling,
)
from datasets import Dataset
from peft import LoraConfig, get_peft_model, TaskType, prepare_model_for_kbit_training


# ============================================================
# 1. 参数定义
# ============================================================

@dataclass
class ModelArguments:
    """模型参数"""
    model_name_or_path: str = field(
        default="/data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B",
        metadata={"help": "预训练模型路径"}
    )
    tokenizer_name_or_path: Optional[str] = field(
        default=None,
        metadata={"help": "Tokenizer 路径，默认与模型相同"}
    )
    use_lora: bool = field(
        default=True,
        metadata={"help": "是否使用 LoRA 微调"}
    )
    lora_r: int = field(default=64, metadata={"help": "LoRA rank"})
    lora_alpha: int = field(default=16, metadata={"help": "LoRA alpha"})
    lora_dropout: float = field(default=0.1, metadata={"help": "LoRA dropout"})
    lora_target_modules: Optional[str] = field(
        default="q_proj,v_proj,k_proj,o_proj",
        metadata={"help": "LoRA 目标模块，逗号分隔"}
    )


@dataclass
class DataArguments:
    """数据参数"""
    train_data: str = field(
        default="data/sft_train_data.jsonl",
        metadata={"help": "训练数据路径 (JSONL 格式)"}
    )
    eval_data: Optional[str] = field(
        default=None,
        metadata={"help": "验证数据路径"}
    )
    max_seq_length: int = field(
        default=2048,
        metadata={"help": "最大序列长度"}
    )


@dataclass
class SFTTrainingArguments(TrainingArguments):
    """训练参数"""
    output_dir: str = field(
        default="saves/sft_model",
        metadata={"help": "输出目录"}
    )
    # 添加额外参数用于记录详细日志
    log_memory: bool = field(
        default=True,
        metadata={"help": "是否记录显存使用"}
    )
    per_device_train_batch_size: int = field(
        default=2,
        metadata={"help": "每设备训练批次大小"}
    )
    per_device_eval_batch_size: int = field(
        default=4,
        metadata={"help": "每设备评估批次大小"}
    )
    gradient_accumulation_steps: int = field(
        default=4,
        metadata={"help": "梯度累积步数"}
    )
    num_train_epochs: int = field(
        default=3,
        metadata={"help": "训练轮数"}
    )
    learning_rate: float = field(
        default=1e-5,
        metadata={"help": "学习率"}
    )
    warmup_ratio: float = field(
        default=0.1,
        metadata={"help": "warmup 比例"}
    )
    lr_scheduler_type: str = field(
        default="cosine",
        metadata={"help": "学习率调度器类型"}
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
        metadata={"help": "是否使用 FP16 混合精度训练"}
    )
    gradient_checkpointing: bool = field(
        default=True,
        metadata={"help": "是否使用梯度检查点"}
    )


# ============================================================
# 2. 数据处理
# ============================================================

def load_sft_data(data_path: str) -> List[Dict]:
    """
    加载 SFT 训练数据

    数据格式 (JSONL):
    {
        "conversation": [
            {
                "role": "user",
                "content": "请总结以下会议记录：\n张三：...\n李四：..."
            },
            {
                "role": "assistant",
                "content": "## 会议纪要\n### 参会人员：张三、李四\n### 主要议题：..."
            }
        ]
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


def format_conversation(example: Dict, tokenizer: AutoTokenizer, max_length: int) -> Dict:
    """
    格式化对话为训练格式，使用 response-only loss masking

    只对 assistant 回复部分计算 loss（prompt 部分 label 设为 -100）。
    这样模型只学习"如何回答"，而不是"如何复读 prompt"。
    """
    conversation = example["conversation"]

    # 分别 tokenize prompt-only 和完整对话，确定 prompt 边界
    prompt_only = [m for m in conversation if m["role"] != "assistant"]
    prompt_text = tokenizer.apply_chat_template(
        prompt_only,
        tokenize=False,
        add_generation_prompt=True,
    )
    prompt_ids = tokenizer(
        prompt_text, truncation=True, max_length=max_length,
        padding=False, return_tensors=None,
    )["input_ids"]
    prompt_len = len(prompt_ids)

    full_text = tokenizer.apply_chat_template(
        conversation,
        tokenize=False,
        add_generation_prompt=False,
    )
    tokenized = tokenizer(
        full_text, truncation=True, max_length=max_length,
        padding=False, return_tensors=None,
    )

    input_ids = tokenized["input_ids"]
    attention_mask = tokenized["attention_mask"]

    # Response-only masking: prompt 部分 label 设为 IGNORE_INDEX (-100)
    labels = [-100] * min(prompt_len, len(input_ids)) + input_ids[prompt_len:]
    labels = labels[: len(input_ids)]

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
    }


def prepare_dataset(
    data_path: str,
    tokenizer: AutoTokenizer,
    max_length: int
) -> Dataset:
    """准备训练数据集"""
    raw_data = load_sft_data(data_path)
    dataset = Dataset.from_list(raw_data)

    # 处理数据
    processed_dataset = dataset.map(
        lambda x: format_conversation(x, tokenizer, max_length),
        batched=False,
        num_proc=4
    )

    return processed_dataset


# ============================================================
# 3. 模型加载
# ============================================================

def load_model_and_tokenizer(
    model_args: ModelArguments
) -> tuple:
    """加载模型和 tokenizer"""
    tokenizer_path = model_args.tokenizer_name_or_path or model_args.model_name_or_path

    # 加载 tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        tokenizer_path,
        trust_remote_code=True,
        padding_side="right"
    )

    # 设置 pad_token
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # 加载模型
    model = AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        trust_remote_code=True,
        torch_dtype=torch.float16,
        device_map="auto"
    )

    # 使用 LoRA 微调
    if model_args.use_lora:
        print("使用 LoRA 微调...")

        # 解析目标模块
        target_modules = [
            m.strip() for m in model_args.lora_target_modules.split(",")
        ]

        lora_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=model_args.lora_r,
            lora_alpha=model_args.lora_alpha,
            lora_dropout=model_args.lora_dropout,
            target_modules=target_modules,
            bias="none",
        )

        # 准备模型用于 k-bit 训练（如果需要）
        model = prepare_model_for_kbit_training(model)
        model = get_peft_model(model, lora_config)
        model.print_trainable_parameters()

    return model, tokenizer


# ============================================================
# 4. 训练主流程
# ============================================================

def main():
    # 解析参数
    parser = HfArgumentParser(
        (ModelArguments, DataArguments, SFTTrainingArguments)
    )
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    print("=" * 60)
    print("SFT 监督微调训练")
    print("=" * 60)
    print(f"模型路径：{model_args.model_name_or_path}")
    print(f"训练数据：{data_args.train_data}")
    print(f"输出目录：{training_args.output_dir}")
    print(f"使用 LoRA: {model_args.use_lora}")
    print("=" * 60)

    # 加载模型和 tokenizer
    model, tokenizer = load_model_and_tokenizer(model_args)

    # 准备数据集
    print("准备训练数据集...")
    train_dataset = prepare_dataset(
        data_args.train_data,
        tokenizer,
        data_args.max_seq_length
    )
    print(f"训练样本数：{len(train_dataset)}")

    # 验证数据集（如果有）
    eval_dataset = None
    if data_args.eval_data:
        print("准备验证数据集...")
        eval_dataset = prepare_dataset(
            data_args.eval_data,
            tokenizer,
            data_args.max_seq_length
        )
        print(f"验证样本数：{len(eval_dataset)}")

    # 数据 collator
    from transformers import DataCollatorForLanguageModeling
    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=False,
        pad_to_multiple_of=8 if training_args.fp16 else None
    )

    # 初始化 Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        data_collator=data_collator,
        tokenizer=tokenizer
    )

    # 开始训练
    print("开始训练...")
    trainer.train()

    # 保存模型
    print("保存模型...")
    output_dir = Path(training_args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 保存 LoRA 权重
    if model_args.use_lora:
        model.save_pretrained(output_dir)
    else:
        model.save_pretrained(output_dir)

    tokenizer.save_pretrained(output_dir)

    print(f"模型已保存到：{output_dir}")
    print("训练完成！")


if __name__ == "__main__":
    main()
