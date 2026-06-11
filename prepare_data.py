"""
数据准备脚本 - 生成 SFT 和 GRPO 训练数据

生成模拟的会议纪要训练数据，包括：
1. SFT 训练数据（监督微调）
2. GRPO 训练数据（强化学习偏好优化）

用法:
    # 生成所有数据
    python prepare_data.py --output_dir data

    # 只生成 SFT 数据
    python prepare_data.py --output_dir data --task sft

    # 只生成 GRPO 数据
    python prepare_data.py --output_dir data --task grpo
"""

import os
import json
import random
from pathlib import Path
from typing import List, Dict
from datetime import datetime, timedelta


# ============================================================
# 1. 模拟数据生成
# ============================================================

# 模拟说话人名称
SPEAKERS = ["张三", "李四", "王五", "赵六", "钱七", "孙八", "周九", "吴十"]

# 模拟会议主题
MEETING_TOPICS = [
    "项目进度汇报",
    "技术方案评审",
    "产品需求讨论",
    "季度总结会议",
    " sprint 规划会",
    "代码 Review 会议",
    "架构设计讨论",
    "bug 修复方案",
    "新功能 brainstorm",
    "客户反馈讨论"
]

# 模拟工作内容
WORK_ITEMS = [
    "后端 API 开发",
    "前端页面实现",
    "数据库设计",
    "接口联调",
    "单元测试编写",
    "性能优化",
    "文档编写",
    "代码 Review",
    "部署配置",
    "监控告警配置",
    "用户调研",
    "数据分析",
    "安全审计",
    "兼容性测试",
    "自动化测试"
]

# 模拟进度状态
PROGRESS_STATUS = [
    "已完成",
    "完成 80%",
    "完成 60%",
    "完成 50%",
    "完成 30%",
    "刚开始",
    "准备开始"
]

# 模拟时间表达
TIME_EXPRESSIONS = [
    "今天完成",
    "明天完成",
    "本周完成",
    "下周完成",
    "预计 3 天后",
    "预计一周后",
    "月底前完成",
    "下周三之前",
    "本周五之前",
    "两天后"
]


def generate_meeting_transcript(
    meeting_id: str,
    num_speakers: int = 3,
    num_turns: int = 10
) -> List[Dict]:
    """生成模拟会议对话记录"""
    speakers = random.sample(SPEAKERS, num_speakers)
    records = []

    base_time = datetime.now() - timedelta(days=random.randint(0, 30))

    for i in range(num_turns):
        speaker = random.choice(speakers)
        topic = random.choice(WORK_ITEMS)
        status = random.choice(PROGRESS_STATUS)
        time_expr = random.choice(TIME_EXPRESSIONS)

        # 生成对话内容模板
        templates = [
            f"{topic} {status}，{time_expr}",
            f"关于{topic}，目前{status}，计划{time_expr}",
            f"{topic}进展：{status}，预计{time_expr}完成",
            f"汇报一下{topic}的情况，{status}，{time_expr}",
            f"{topic}这边{status}，{time_expr}可以完成",
            f"我说下{topic}进度，{status}，{time_expr}",
        ]

        content = random.choice(templates)

        record = {
            "meeting_id": meeting_id,
            "speaker": speaker,
            "content": content,
            "timestamp": (base_time + timedelta(minutes=i*5)).isoformat(),
            "start_ms": i * 300000,
            "end_ms": (i + 1) * 300000
        }
        records.append(record)

    return records


def generate_meeting_summary(transcript: List[Dict]) -> str:
    """根据对话记录生成会议纪要总结"""
    speakers = set(r["speaker"] for r in transcript)
    topics = []
    actions = []

    for record in transcript:
        content = record["content"]
        # 简单提取关键词
        for topic in WORK_ITEMS:
            if topic in content:
                topics.append(topic)
                if "完成" in content or "预计" in content:
                    actions.append(f"{record['speaker']} 负责 {topic}")

    # 生成纪要模板
    summary = f"""## 会议纪要

### 参会人员
{', '.join(speakers)}

### 主要议题
{', '.join(set(topics)) if topics else '项目进度讨论'}

### 进度汇报
"""

    # 按说话人分组
    speaker_records = {}
    for record in transcript:
        spk = record["speaker"]
        if spk not in speaker_records:
            speaker_records[spk] = []
        speaker_records[spk].append(record["content"])

    for spk, contents in speaker_records.items():
        summary += f"\n**{spk}**: {'; '.join(contents[:3])}"

    summary += f"""

### 行动项
"""
    unique_actions = list(dict.fromkeys(actions))[:5]
    for i, action in enumerate(unique_actions, 1):
        summary += f"\n{i}. {action}"

    if not actions:
        summary += "\n暂无明确行动项"

    summary += f"""

### 下次会议
待定
"""

    return summary


# ============================================================
# 2. SFT 数据生成
# ============================================================

def generate_sft_data(num_samples: int = 100) -> List[Dict]:
    """
    生成 SFT 训练数据

    格式:
    {
        "conversation": [
            {"role": "user", "content": "请总结以下会议记录：..."},
            {"role": "assistant", "content": "## 会议纪要\n..."}
        ]
    }
    """
    data = []

    for i in range(num_samples):
        meeting_id = f"meeting_{i:04d}"
        transcript = generate_meeting_transcript(
            meeting_id,
            num_speakers=random.randint(2, 5),
            num_turns=random.randint(8, 15)
        )

        # 构建对话文本
        transcript_text = "\n".join([
            f"{r['speaker']}: {r['content']}" for r in transcript
        ])

        # 生成总结
        summary = generate_meeting_summary(transcript)

        # 构建对话样本
        sample = {
            "conversation": [
                {
                    "role": "user",
                    "content": f"请总结以下会议记录：\n\n{transcript_text}"
                },
                {
                    "role": "assistant",
                    "content": summary
                }
            ]
        }

        data.append(sample)

    return data


# ============================================================
# 3. GRPO 数据生成
# ============================================================

def generate_grpo_data(num_samples: int = 100) -> List[Dict]:
    """
    生成 GRPO 训练数据

    格式:
    {
        "prompt": "请总结以下会议记录：...",
        "reference": "标准答案/参考总结",
        "query": "会议总结",
        "source_context": "张三：后端API已完成80%...",  # 当前会议转写
        "retrieved_docs": ["[历史会议] 张三上周提到...", ...],  # RAG 历史文档
    }
    """
    data = []

    for i in range(num_samples):
        meeting_id = f"meeting_{i:04d}"
        transcript = generate_meeting_transcript(
            meeting_id,
            num_speakers=random.randint(2, 5),
            num_turns=random.randint(8, 15)
        )

        # 构建对话文本
        transcript_text = "\n".join([
            f"{r['speaker']}: {r['content']}" for r in transcript
        ])

        # 生成总结（作为 reference）
        reference = generate_meeting_summary(transcript)

        # 生成 source_context（当前会议转写）
        source_context = transcript_text

        # retrieved_docs 来自 RAG 历史文档池，每次随机抽取 1-3 条
        rag_pool = generate_rag_data(num_meetings=20)
        num_docs = random.randint(1, 3)
        retrieved_docs = [
            f"[历史会议 {m['meeting_id']}] " + " ".join(
                r["content"] for r in m["records"][:3]
            )
            for m in random.sample(rag_pool, min(num_docs, len(rag_pool)))
        ]

        # 构建样本
        sample = {
            "prompt": f"请总结以下会议记录：\n\n{transcript_text}",
            "reference": reference,
            "query": "会议总结",
            "source_context": source_context,
            "retrieved_docs": retrieved_docs,
        }

        data.append(sample)

    return data


# ============================================================
# 4. 偏好数据生成（用于 DPO/ORPO）
# ============================================================

def generate_preference_data(num_samples: int = 100) -> List[Dict]:
    """
    生成偏好数据（chosen / rejected 对）

    格式:
    {
        "prompt": "请总结以下会议记录：...",
        "chosen": "优质总结",
        "rejected": "劣质总结",
        "source_context": "张三：后端API已完成80%...",  # 当前会议转写
        "retrieved_docs": ["[历史会议] 张三上周提到...", ...],  # RAG 历史文档
    }
    """
    data = []

    for i in range(num_samples):
        meeting_id = f"meeting_{i:04d}"
        transcript = generate_meeting_transcript(
            meeting_id,
            num_speakers=random.randint(2, 5),
            num_turns=random.randint(8, 15)
        )

        transcript_text = "\n".join([
            f"{r['speaker']}: {r['content']}" for r in transcript
        ])

        # 生成优质总结（chosen）
        chosen = generate_meeting_summary(transcript)

        # 生成劣质总结（rejected）- 更短、更模糊
        rejected = f"""会议讨论了相关工作内容。

参会人员：{', '.join(set(r['speaker'] for r in transcript))}

具体内容详见会议记录。
"""

        # 生成 source_context（当前会议转写）
        source_context = transcript_text

        # retrieved_docs 来自 RAG 历史文档池，每次随机抽取 1-3 条
        rag_pool = generate_rag_data(num_meetings=20)
        num_docs = random.randint(1, 3)
        retrieved_docs = [
            f"[历史会议 {m['meeting_id']}] " + " ".join(
                r["content"] for r in m["records"][:3]
            )
            for m in random.sample(rag_pool, min(num_docs, len(rag_pool)))
        ]

        sample = {
            "prompt": f"请总结以下会议记录：\n\n{transcript_text}",
            "chosen": chosen,
            "rejected": rejected,
            "source_context": source_context,
            "retrieved_docs": retrieved_docs,
        }

        data.append(sample)

    return data


# ============================================================
# 5. RAG 数据生成
# ============================================================

def generate_rag_data(num_meetings: int = 20) -> List[Dict]:
    """
    生成 RAG 检索用会议数据

    格式:
    {
        "meeting_id": "meeting_001",
        "topic": "项目进度汇报",
        "records": [
            {"speaker": "张三", "content": "...", "timestamp": "..."}
        ]
    }
    """
    data = []

    for i in range(num_meetings):
        meeting_id = f"meeting_{i:04d}"
        topic = random.choice(MEETING_TOPICS)

        transcript = generate_meeting_transcript(
            meeting_id,
            num_speakers=random.randint(2, 5),
            num_turns=random.randint(8, 15)
        )

        meeting_data = {
            "meeting_id": meeting_id,
            "topic": topic,
            "date": (datetime.now() - timedelta(days=random.randint(0, 30))).strftime("%Y-%m-%d"),
            "records": transcript
        }

        data.append(meeting_data)

    return data


# ============================================================
# 6. 主流程
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="训练数据准备")
    parser.add_argument("--output_dir", type=str, default="data",
                        help="输出目录")
    parser.add_argument("--task", type=str, default="all",
                        choices=["sft", "grpo", "preference", "rag", "all"],
                        help="生成任务类型")
    parser.add_argument("--num_sft", type=int, default=100,
                        help="SFT 数据数量")
    parser.add_argument("--num_grpo", type=int, default=100,
                        help="GRPO 数据数量")
    parser.add_argument("--num_preference", type=int, default=100,
                        help="偏好数据数量")
    parser.add_argument("--num_rag", type=int, default=20,
                        help="RAG 会议数量")
    parser.add_argument("--seed", type=int, default=42,
                        help="随机种子")

    args = parser.parse_args()

    # 设置随机种子
    random.seed(args.seed)

    print("=" * 60)
    print("训练数据准备")
    print("=" * 60)
    print(f"输出目录：{args.output_dir}")
    print(f"任务类型：{args.task}")
    print("=" * 60)

    # 创建输出目录
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 创建 meeting_records 子目录（用于 RAG）
    records_dir = output_dir / "meeting_records"
    records_dir.mkdir(exist_ok=True)

    # 生成 SFT 数据
    if args.task in ["sft", "all"]:
        print(f"\n生成 SFT 训练数据 ({args.num_sft} 条)...")
        sft_data = generate_sft_data(args.num_sft)
        sft_path = output_dir / "sft_train_data.jsonl"
        with open(sft_path, "w", encoding="utf-8") as f:
            for sample in sft_data:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        print(f"  已保存：{sft_path}")

    # 生成 GRPO 数据
    if args.task in ["grpo", "all"]:
        print(f"\n生成 GRPO 训练数据 ({args.num_grpo} 条)...")
        grpo_data = generate_grpo_data(args.num_grpo)
        grpo_path = output_dir / "grpo_train_data.jsonl"
        with open(grpo_path, "w", encoding="utf-8") as f:
            for sample in grpo_data:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        print(f"  已保存：{grpo_path}")

    # 生成偏好数据
    if args.task in ["preference", "all"]:
        print(f"\n生成偏好数据 ({args.num_preference} 条)...")
        pref_data = generate_preference_data(args.num_preference)
        pref_path = output_dir / "preference_data.jsonl"
        with open(pref_path, "w", encoding="utf-8") as f:
            for sample in pref_data:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        print(f"  已保存：{pref_path}")

    # 生成 RAG 数据
    if args.task in ["rag", "all"]:
        print(f"\n生成 RAG 会议数据 ({args.num_rag} 个会议)...")
        rag_data = generate_rag_data(args.num_rag)

        # 保存为 JSON 文件（每个会议一个文件）
        for meeting in rag_data:
            meeting_path = records_dir / f"{meeting['meeting_id']}.json"
            with open(meeting_path, "w", encoding="utf-8") as f:
                json.dump(meeting, f, ensure_ascii=False, indent=2)

        # 同时保存一个汇总文件
        rag_path = output_dir / "rag_meetings.json"
        with open(rag_path, "w", encoding="utf-8") as f:
            json.dump(rag_data, f, ensure_ascii=False, indent=2)
        print(f"  已保存：{records_dir}/*.json")

    print("\n" + "=" * 60)
    print("数据生成完成！")
    print("=" * 60)

    # 打印数据样例
    print("\n数据样例:")
    print("-" * 60)

    if args.task in ["sft", "all"]:
        print("\n[SFT 数据样例]")
        sample = sft_data[0]
        print(f"User: {sample['conversation'][0]['content'][:100]}...")
        print(f"Assistant: {sample['conversation'][1]['content'][:100]}...")

    if args.task in ["grpo", "all"]:
        print("\n[GRPO 数据样例]")
        sample = grpo_data[0]
        print(f"Prompt: {sample['prompt'][:100]}...")
        print(f"Reference: {sample['reference'][:100]}...")
        print(f"Source Context: {sample.get('source_context', 'N/A')[:100]}...")
        print(f"Retrieved Docs: {len(sample.get('retrieved_docs', []))} 条")

    if args.task in ["preference", "all"]:
        print("\n[偏好数据样例]")
        sample = pref_data[0]
        print(f"Prompt: {sample['prompt'][:50]}...")
        print(f"Chosen: {sample['chosen'][:50]}...")
        print(f"Rejected: {sample['rejected'][:50]}...")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
