"""
统一评测入口 — 三层评测合一

用法：
    # 1. LLM-as-Judge 评测（推荐，量化语义质量）
    python evaluation/run_eval.py --mode judge --model_path saves/sft_model --test_data evaluation/test_set.json

    # 2. 人工打分（交互式，1-5分）
    python evaluation/run_eval.py --mode human --test_data evaluation/test_set.json

    # 3. 自动指标（ROUGE-L / BERTScore / BGE sim）
    python evaluation/run_eval.py --mode auto --test_data evaluation/test_set.json

    # 4. 全部跑一遍
    python evaluation/run_eval.py --mode all --model_path saves/sft_model --test_data evaluation/test_set.json

三层评测内容：
  Layer 1: RAG 检索质量（recall@K, top-K acc）
  Layer 2: 生成质量（LLM-Judge / 人工打分 / 自动指标）
  Layer 3: 结构化质量（Markdown通过率, 字段完整率, DAG无环率）
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np


# ============================================================
# 数据结构
# ============================================================

@dataclass
class EvalSample:
    """单条评测样本"""
    id: str
    query: str
    reference: str
    generated: str
    retrieved_docs: list[str]
    action_items: list[dict]
    speaker_filter: Optional[str] = None


@dataclass
class SampleScore:
    """单条样本的评测结果"""
    sample_id: str
    query: str

    # 自动指标
    rouge_l_f1: float = 0.0
    bertscore_f1: float = 0.0
    bge_sim: float = 0.0
    faithful_ratio: float = 0.0

    # 人工 / LLM-Judge 评分
    accuracy_score: float = 0.0      # 1-5
    completeness_score: float = 0.0  # 1-5  关键字段是否完整
    structure_score: float = 0.0     # 1-5  格式规范性
    coherence_score: float = 0.0    # 1-5  逻辑连贯性
    overall_score: float = 0.0       # 1-5  综合评分

    # 评语（Judge 或人工填写）
    comment: str = ""

    # 结构化专项
    owner_present: bool = False
    deadline_present: bool = False
    task_specific: bool = False
    dag_valid: bool = False
    markdown_ok: bool = False
    action_section_present: bool = False  # 行动项专属章节是否存在

    def format_pass_rate(self) -> float:
        """格式通过率：6项检查通过几项"""
        checks = [
            self.markdown_ok,
            self.action_section_present,
            self.owner_present,
            self.deadline_present,
            self.task_specific,
            self.dag_valid,
        ]
        return sum(checks) / len(checks)

    def field_completeness(self) -> float:
        """字段完整率：owner + deadline + task具体"""
        return sum([self.owner_present, self.deadline_present, self.task_specific]) / 3

    def avg_human_score(self) -> float:
        return (self.accuracy_score + self.completeness_score +
                self.structure_score + self.coherence_score + self.overall_score) / 5


@dataclass
class EvalReport:
    """最终汇总报告"""
    timestamp: str
    n_samples: int
    mode: str

    # 自动指标均值
    rouge_l_f1_avg: float = 0.0
    bertscore_f1_avg: float = 0.0
    bge_sim_avg: float = 0.0
    faithful_ratio_avg: float = 0.0

    # 人工/LLM评分均值（1-5分）
    accuracy_avg: float = 0.0
    completeness_avg: float = 0.0
    structure_avg: float = 0.0
    coherence_avg: float = 0.0
    overall_avg: float = 0.0

    # 结构化通过率
    format_pass_rate: float = 0.0   # Markdown + owner + deadline + task + DAG
    field_completeness: float = 0.0  # owner + deadline + task
    dag_valid_rate: float = 0.0      # DAG 无环率

    # 逐条明细
    per_sample: list[dict] = None

    def __post_init__(self):
        if self.per_sample is None:
            self.per_sample = []

    def summary(self) -> str:
        lines = [
            "=" * 60,
            f"评测报告  [{self.timestamp}]  模式: {self.mode}",
            "=" * 60,
            f"样本数: {self.n_samples}",
            "",
        ]

        if self.mode in ("auto", "all"):
            lines += [
                "【Layer 2: 自动指标】",
                f"  ROUGE-L F1:     {self.rouge_l_f1_avg:.3f}",
                f"  BERTScore F1:   {self.bertscore_f1_avg:.3f}",
                f"  BGE Similarity: {self.bge_sim_avg:.3f}",
                f"  Faithful Ratio: {self.faithful_ratio_avg:.3f}",
                "",
            ]

        if self.mode in ("human", "judge", "all"):
            lines += [
                "【Layer 2: 质量评分（1-5分）】",
                f"  准确性:   {self.accuracy_avg:.2f}",
                f"  完整性:   {self.completeness_avg:.2f}",
                f"  结构化:   {self.structure_avg:.2f}",
                f"  连贯性:   {self.coherence_avg:.2f}",
                f"  综合评分: {self.overall_avg:.2f}",
                "",
            ]

        lines += [
            "【Layer 3: 结构化通过率】",
            f"  格式通过率:   {self.format_pass_rate:.1%}",
            f"  字段完整率:   {self.field_completeness:.1%}",
            f"  DAG无环率:    {self.dag_valid_rate:.1%}",
            "=" * 60,
        ]
        return "\n".join(lines)


# ============================================================
# Layer 1: RAG 检索评测
# ============================================================

def evaluate_rag(retriever, test_cases: list[dict], recall_k: int = 15) -> dict:
    """
    评估 RAG 检索质量
    返回 top-1/3/5 accuracy 和 recall@K
    """
    from evaluation.test_rag_accuracy import _chunk_overlap

    top1, top3, top5 = 0, 0, 0
    recall_scores = []

    for tc in test_cases:
        results = retriever.retrieve(
            query=tc["query"],
            k=recall_k,
            speaker_filter=tc.get("speaker_filter"),
        )
        retrieved = [r.content for r in results]

        # top-1/3/5 hit
        relevant = tc.get("relevant_chunks", [])
        if relevant:
            top1 += int(_chunk_overlap(relevant[0], retrieved[0])) if retrieved else 0
            top3_hits = any(_chunk_overlap(relevant[0], c) for c in retrieved[:3])
            top5_hits = any(_chunk_overlap(relevant[0], c) for c in retrieved[:5])
            top3 += int(top3_hits)
            top5 += int(top5_hits)

        # recall@K
        matched = 0
        for rel in relevant:
            for ret in retrieved:
                if _chunk_overlap(rel, ret):
                    matched += 1
                    break
        recall_scores.append(matched / max(len(relevant), 1) if relevant else 0)

    n = len(test_cases)
    return {
        "top1_acc": top1 / n,
        "top3_acc": top3 / n,
        "top5_acc": top5 / n,
        "recall@K": np.mean(recall_scores) if recall_scores else 0.0,
    }


# ============================================================
# Layer 2a: 自动指标
# ============================================================

def compute_auto_metrics(sample: EvalSample, embedder) -> dict:
    """计算 ROUGE-L / BERTScore / BGE sim / Faithfulness"""
    from evaluation.comprehensive_eval import (
        rouge_l, bert_score_bge, bge_similarity, faithfulness_score
    )

    gen = sample.generated
    ref = sample.reference
    docs = sample.retrieved_docs

    rouge = rouge_l(gen, ref)
    bert = bert_score_bge(gen, ref, embedder)
    bge = bge_similarity(gen, ref, embedder)
    faithful = faithfulness_score(gen, docs, embedder) if docs else {
        "faithful_ratio": 0.5
    }

    return {
        "rouge_l_f1": rouge["f1"],
        "bertscore_f1": bert["f1"],
        "bge_sim": bge,
        "faithful_ratio": faithful["faithful_ratio"],
    }


# ============================================================
# Layer 2b: LLM-as-Judge
# ============================================================

LLM_JUDGE_PROMPT = """你是一个专业的会议纪要评测员。请对以下生成内容打分（1-5分）。

【查询】
{query}

【标准答案（参考）】
{reference}

【生成内容（待评测）】
{generated}

请从以下四个维度打分，给出每项分数（1-5整数）和简短评语：

1. 准确性（accuracy）：生成内容是否准确复现了关键信息（人名、进度、数字、结论）？
2. 完整性（completeness）：关键字段（owner/deadline/task）是否齐全？是否有遗漏的重要信息？
3. 结构化（structure）：Markdown 格式是否规范？章节标题是否清晰？列表是否整齐？
4. 连贯性（coherence）：段落之间逻辑是否连贯？是否有前后矛盾或跳跃？

输出格式（必须严格遵循）：
```json
{{
    "accuracy": 4,
    "completeness": 3,
    "structure": 5,
    "coherence": 4,
    "overall": 4,
    "comment": "简要评语"
}}
```
评分标准：1=很差，2=较差，3=中等，4=较好，5=优秀。
"""


def call_llm_judge(
    sample: EvalSample,
    model_path: str,
    api_base: Optional[str] = None,
) -> dict:
    """
    调用本地 LLM（vLLM / Ollama / OpenAI 兼容 API）做 Judge

    Args:
        model_path: 模型名称或路径
        api_base: API 地址（如 http://localhost:8000/v1）
    """
    try:
        import openai
    except ImportError:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("请安装 openai 库: pip install openai")

    if api_base is None:
        api_base = "http://localhost:8000/v1"

    client = openai.OpenAI(base_url=api_base, api_key="EMPTY")

    prompt = LLM_JUDGE_PROMPT.format(
        query=sample.query,
        reference=sample.reference,
        generated=sample.generated,
    )

    response = client.chat.completions.create(
        model=model_path,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=256,
    )

    raw = response.choices[0].message.content

    # 解析 JSON
    try:
        # 尝试从 markdown 块中提取
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
        else:
            data = json.loads(raw)
        return {k: int(v) if k != "comment" else str(v) for k, v in data.items()}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return {
            "accuracy": 3, "completeness": 3,
            "structure": 3, "coherence": 3,
            "overall": 3,
            "comment": f"[解析失败] {raw[:100]}",
        }


# ============================================================
# Layer 2c: 人工打分
# ============================================================

def human_score_sample(sample: EvalSample) -> dict:
    """
    交互式人工打分（1-5分）
    直接在命令行输入分数，无需模型
    """
    print("\n" + "=" * 50)
    print(f"【样本 {sample.id}】")
    print(f"查询: {sample.query}")
    print("-" * 50)
    print("【标准答案】")
    print(sample.reference)
    print("-" * 50)
    print("【生成内容】")
    print(sample.generated)
    print("=" * 50)

    def _ask(dim: str, desc: str) -> int:
        while True:
            try:
                val = int(input(f"  {dim}（{desc}）[1-5，回车默认3]: ").strip() or 3)
                if 1 <= val <= 5:
                    return val
                print("  请输入 1-5 之间的整数")
            except ValueError:
                print("  请输入 1-5 之间的整数")

    print("\n请打分（1=很差，2=较差，3=中等，4=较好，5=优秀）：")
    accuracy = _ask("准确性", "关键信息是否准确")
    completeness = _ask("完整性", "关键字段是否齐全")
    structure = _ask("结构化", "Markdown格式是否规范")
    coherence = _ask("连贯性", "段落逻辑是否连贯")
    overall = _ask("综合", "整体质量评分")
    comment = input("  评语（可选，直接回车跳过）: ").strip()

    return {
        "accuracy": accuracy,
        "completeness": completeness,
        "structure": structure,
        "coherence": coherence,
        "overall": overall,
        "comment": comment,
    }


# ============================================================
# Layer 3: 结构化专项检查
# ============================================================

def check_structural(sample: EvalSample) -> dict:
    """检查 Markdown 结构、字段完整性、DAG 无环"""
    text = sample.generated
    items = sample.action_items or []

    # Markdown 检查
    markdown_ok = bool(
        re.search(r"#{1,3}", text) and
        re.search(r"会议纪要|行动项|决策项|参会人员", text)
    )

    # 行动项专属章节存在性（对应训练期 _structure_reward 子项2）
    action_section_present = bool(
        re.search(r"###\s*行|##\s*行|行动项[：:\s]|action[_\s]?item", text, re.IGNORECASE)
    )

    # owner 存在（从 action_items 或文本中）
    owner_present = False
    deadline_present = False
    task_specific = False

    time_keywords = ["周", "月", "日", "天", "号", "点", "时", "本周", "下周", "完成"]

    if items:
        owner_present = all(
            item.get("owner", "") not in ("", "unknown", "待定", "TBD")
            for item in items
        )
        deadline_present = all(
            item.get("deadline", "") and
            any(kw in item.get("deadline", "") for kw in time_keywords)
            for item in items
        )
        task_specific = all(
            len(item.get("task", "")) >= 5
            for item in items
        )
    else:
        # Fallback: 从文本中正则解析
        owner_pattern = r'([\u4e00-\u9fa5]{2,4})[：:]\s*\S+'
        deadline_pattern = r'截止[时分]?(?:间)?[：:]?\s*([^\n]{2,20})'
        task_pattern = r'任务[：:]?\s*([^\n]{5,})'

        owners = re.findall(owner_pattern, text)
        deadlines = re.findall(deadline_pattern, text)
        tasks = re.findall(task_pattern, text)

        owner_present = len(owners) >= 1 and all(
            o not in ("待定", "TBD") for o in owners
        )
        deadline_present = any(
            any(kw in d for kw in time_keywords) for d in deadlines
        )
        task_specific = any(len(t) >= 5 for t in tasks)

    # DAG 无环（Kahn 拓扑排序）
    dag_valid = True
    if items and len(items) > 1:
        n = len(items)
        edges = []
        for i, item in enumerate(items):
            for dep in item.get("depends_on", []):
                if 0 <= dep < n and dep != i:
                    edges.append((dep, i))

        in_degree = [0] * n
        for (u, v) in edges:
            in_degree[v] += 1

        queue = [i for i in range(n) if in_degree[i] == 0]
        topo = []
        while queue:
            node = queue.pop(0)
            topo.append(node)
            for (u, v) in edges:
                if u == node:
                    in_degree[v] -= 1
                    if in_degree[v] == 0:
                        queue.append(v)

        dag_valid = len(topo) == n

    return {
        "markdown_ok": markdown_ok,
        "action_section_present": action_section_present,
        "owner_present": owner_present,
        "deadline_present": deadline_present,
        "task_specific": task_specific,
        "dag_valid": dag_valid,
    }


# ============================================================
# 主评测流程
# ============================================================

def run_evaluation(
    samples: list[EvalSample],
    mode: str,
    model_path: Optional[str] = None,
    api_base: Optional[str] = None,
    output_path: Optional[str] = None,
    skip_rag: bool = False,
) -> EvalReport:
    """
    统一评测入口

    Args:
        samples: 评测样本列表
        mode: judge / human / auto / all
        model_path: LLM-as-Judge 模型名（如 "Qwen2.5-7B"）
        api_base: vLLM/Ollama API 地址
        output_path: 结果保存路径
        skip_rag: 是否跳过 RAG 检索评测
    """
    ts = time.strftime("%Y-%m-%d %H:%M")
    report = EvalReport(timestamp=ts, n_samples=len(samples), mode=mode)

    # 加载 embedder（用于 auto metrics）
    embedder = None
    if mode in ("auto", "all"):
        try:
            from sentence_transformers import SentenceTransformer
            embedder = SentenceTransformer("BAAI/bge-large-zh-v1.5")
            print("BGE 模型加载完成")
        except Exception as e:
            print(f"警告：BGE 模型加载失败，自动指标将跳过: {e}")

    per_sample_results = []

    for i, sample in enumerate(samples):
        print(f"\n[{i+1}/{len(samples)}] 评测样本 {sample.id}...")

        # Layer 3: 结构化检查
        struct = check_structural(sample)
        score = SampleScore(
            sample_id=sample.id,
            query=sample.query,
            owner_present=struct["owner_present"],
            deadline_present=struct["deadline_present"],
            task_specific=struct["task_specific"],
            dag_valid=struct["dag_valid"],
            markdown_ok=struct["markdown_ok"],
            action_section_present=struct["action_section_present"],
        )

        # Layer 2a: 自动指标
        if mode in ("auto", "all") and embedder:
            auto = compute_auto_metrics(sample, embedder)
            score.rouge_l_f1 = auto["rouge_l_f1"]
            score.bertscore_f1 = auto["bertscore_f1"]
            score.bge_sim = auto["bge_sim"]
            score.faithful_ratio = auto["faithful_ratio"]
            print(f"  Auto: ROUGE-L={score.rouge_l_f1:.3f} BGE={score.bge_sim:.3f}")

        # Layer 2b: LLM-as-Judge
        if mode in ("judge", "all"):
            if not model_path:
                raise ValueError("LLM-as-Judge 模式需要指定 --model_path")
            judge_result = call_llm_judge(sample, model_path, api_base)
            score.accuracy_score = judge_result.get("accuracy", 3)
            score.completeness_score = judge_result.get("completeness", 3)
            score.structure_score = judge_result.get("structure", 3)
            score.coherence_score = judge_result.get("coherence", 3)
            score.overall_score = judge_result.get("overall", 3)
            score.comment = judge_result.get("comment", "")
            print(f"  Judge: acc={score.accuracy_score} comp={score.completeness_score} "
                  f"struct={score.structure_score} coh={score.coherence_score} "
                  f"overall={score.overall_score}")
            print(f"  Comment: {score.comment}")

        # Layer 2c: 人工打分
        elif mode == "human":
            human_result = human_score_sample(sample)
            score.accuracy_score = human_result["accuracy"]
            score.completeness_score = human_result["completeness"]
            score.structure_score = human_result["structure"]
            score.coherence_score = human_result["coherence"]
            score.overall_score = human_result["overall"]
            score.comment = human_result["comment"]

        per_sample_results.append(score)

    # 汇总
    n = len(per_sample_results)

    if mode in ("auto", "all"):
        report.rouge_l_f1_avg = np.mean([s.rouge_l_f1 for s in per_sample_results])
        report.bertscore_f1_avg = np.mean([s.bertscore_f1 for s in per_sample_results])
        report.bge_sim_avg = np.mean([s.bge_sim for s in per_sample_results])
        report.faithful_ratio_avg = np.mean([s.faithful_ratio for s in per_sample_results])

    if mode in ("human", "judge", "all"):
        report.accuracy_avg = np.mean([s.accuracy_score for s in per_sample_results])
        report.completeness_avg = np.mean([s.completeness_score for s in per_sample_results])
        report.structure_avg = np.mean([s.structure_score for s in per_sample_results])
        report.coherence_avg = np.mean([s.coherence_score for s in per_sample_results])
        report.overall_avg = np.mean([s.overall_score for s in per_sample_results])

    report.format_pass_rate = np.mean([s.format_pass_rate() for s in per_sample_results])
    report.field_completeness = np.mean([s.field_completeness() for s in per_sample_results])
    report.dag_valid_rate = np.mean([float(s.dag_valid) for s in per_sample_results])

    report.per_sample = []
    for s in per_sample_results:
        d = asdict(s)
        d["format_pass_rate"] = s.format_pass_rate()
        d["field_completeness"] = s.field_completeness()
        report.per_sample.append(d)

    # 打印汇总
    print("\n" + report.summary())

    # 保存结果
    if output_path:
        output_data = {
            "timestamp": report.timestamp,
            "mode": report.mode,
            "n_samples": report.n_samples,
            "metrics": {
                "auto": {
                    "rouge_l_f1_avg": round(report.rouge_l_f1_avg, 4),
                    "bertscore_f1_avg": round(report.bertscore_f1_avg, 4),
                    "bge_sim_avg": round(report.bge_sim_avg, 4),
                    "faithful_ratio_avg": round(report.faithful_ratio_avg, 4),
                } if mode in ("auto", "all") else {},
                "human_judge": {
                    "accuracy_avg": round(report.accuracy_avg, 2),
                    "completeness_avg": round(report.completeness_avg, 2),
                    "structure_avg": round(report.structure_avg, 2),
                    "coherence_avg": round(report.coherence_avg, 2),
                    "overall_avg": round(report.overall_avg, 2),
                } if mode in ("human", "judge", "all") else {},
                "structural": {
                    "format_pass_rate": round(report.format_pass_rate, 4),
                    "field_completeness": round(report.field_completeness, 4),
                    "dag_valid_rate": round(report.dag_valid_rate, 4),
                }
            },
            "per_sample": report.per_sample,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {output_path}")

    return report


# ============================================================
# CLI 入口
# ============================================================

def load_test_data(path: str) -> list[EvalSample]:
    """加载测试集"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [EvalSample(**item) for item in data]


def main():
    parser = argparse.ArgumentParser(description="DiTX-Clerk 统一评测入口")
    parser.add_argument("--mode", required=True,
                        choices=["judge", "human", "auto", "all"],
                        help="judge=LLM打分, human=人工1-5分, auto=自动指标, all=全部")
    parser.add_argument("--test_data", required=True,
                        help="测试集 JSON 文件路径")
    parser.add_argument("--model_path", default=None,
                        help="LLM-as-Judge 模型名（如 Qwen2.5-7B）")
    parser.add_argument("--api_base", default="http://localhost:8000/v1",
                        help="vLLM/Ollama API 地址")
    parser.add_argument("--output", default="eval_results.json",
                        help="结果输出路径")
    parser.add_argument("--skip_rag", action="store_true",
                        help="跳过 RAG 检索评测")
    args = parser.parse_args()

    print(f"加载测试集: {args.test_data}")
    samples = load_test_data(args.test_data)
    print(f"共 {len(samples)} 条样本")

    # Layer 1: RAG 检索评测（可选）
    if not args.skip_rag:
        print("\n--- Layer 1: RAG 检索评测 ---")
        try:
            from audiochat.rag.retriever import AudioChatRetriever
            from audiochat.rag.storage import MeetingMemoryStore
            storage = MeetingMemoryStore(persist_directory="./rag_storage")
            retriever = AudioChatRetriever(storage=storage)
            retriever._warm_up()

            rag_cases = [
                {"query": s.query, "relevant_chunks": s.retrieved_docs,
                 "speaker_filter": s.speaker_filter}
                for s in samples
            ]
            rag_results = evaluate_rag(retriever, rag_cases)
            print(f"  Top-1 Acc: {rag_results['top1_acc']:.1%}")
            print(f"  Top-3 Acc: {rag_results['top3_acc']:.1%}")
            print(f"  Top-5 Acc: {rag_results['top5_acc']:.1%}")
            print(f"  Recall@K:  {rag_results['recall@K']:.1%}")
        except Exception as e:
            print(f"  [跳过] RAG 评测失败: {e}")

    # Layer 2 + 3: 质量评测
    print(f"\n--- Layer 2+3: 质量评测 [mode={args.mode}] ---")
    run_evaluation(
        samples=samples,
        mode=args.mode,
        model_path=args.model_path,
        api_base=args.api_base,
        output_path=args.output,
        skip_rag=args.skip_rag,
    )


if __name__ == "__main__":
    main()
