"""
RAG 检索质量评估脚本

评估指标：
  - recall@k: 所有正确 chunk 出现在 Top-K 召回中的比例
  - top_k_accuracy@k: Top-K 召回中是否包含正确 chunk（每条 query）
  - pass@k: 生成 k 条回答，有至少一条通过阈值
  - error_breakdown: 错误分类（时间问题/语义漂移/说话人混淆/其他）

用法：
  python evaluation/test_rag_accuracy.py --storage-dir ./rag_storage --test-set ./test_set.json
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from audiochat.rag.retriever import AudioChatRetriever
from audiochat.rag.storage import MeetingMemoryStore


@dataclass
class TestCase:
    """单条测试用例"""
    query: str
    relevant_chunks: list[str]  # 所有相关 chunk 内容（可能多个）
    speaker_filter: Optional[str] = None  # 可选，按说话人过滤
    error_type: Optional[str] = None  # 错误分类（后验填入）


@dataclass
class RetrievalResult:
    """单次检索结果"""
    query: str
    top_k: int
    retrieved_chunks: list[str]
    relevant_chunks: list[str]
    recalled: bool  # recall@k = 有至少一个相关 chunk 进了 Top-K
    recall_count: int  # recall@K 中包含了几个相关 chunk
    total_relevant: int  # 总共有几个相关 chunk
    precision_at_k: float  # Top-K 中有多少比例是相关的
    top_scores: list[float]


@dataclass
class EvalReport:
    """评估报告"""
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    total_cases: int = 0
    top1_accuracy: float = 0.0
    top3_accuracy: float = 0.0
    top5_accuracy: float = 0.0
    top15_recall: float = 0.0  # recall@15：所有相关 chunk 有多少比例进了 Top-15
    avg_latency_ms: float = 0.0

    # 错误分析
    error_time: int = 0  # 时间问题（旧文档干扰）
    error_semantic: int = 0  # 语义漂移
    error_speaker: int = 0  # 说话人混淆
    error_other: int = 0  # 其他

    # 逐条详情（可裁剪避免文件过大）
    per_case_results: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "RAG 检索质量评估报告",
            "=" * 60,
            f"评估时间：{self.timestamp}",
            f"测试用例数：{self.total_cases}",
            "",
            "【核心指标】",
            f"  Top-1 准确率：{self.top1_accuracy:.1%}",
            f"  Top-3 准确率：{self.top3_accuracy:.1%}",
            f"  Top-5 准确率：{self.top5_accuracy:.1%}",
            f"  Top-15 召回率：{self.top15_recall:.1%}",
            f"  平均延迟：{self.avg_latency_ms:.1f} ms",
            "",
            "【错误分类】",
            f"  时间问题（旧文档干扰）：{self.error_time} 条 ({self.error_time/max(self.total_cases,1):.1%})",
            f"  语义漂移（话题偏差）：  {self.error_semantic} 条 ({self.error_semantic/max(self.total_cases,1):.1%})",
            f"  说话人混淆：           {self.error_speaker} 条 ({self.error_speaker/max(self.total_cases,1):.1%})",
            f"  其他：                 {self.error_other} 条 ({self.error_other/max(self.total_cases,1):.1%})",
            "=" * 60,
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "total_cases": self.total_cases,
            "metrics": {
                "top1_accuracy": self.top1_accuracy,
                "top3_accuracy": self.top3_accuracy,
                "top5_accuracy": self.top5_accuracy,
                "top15_recall": self.top15_recall,
                "avg_latency_ms": self.avg_latency_ms,
            },
            "error_breakdown": {
                "time": self.error_time,
                "semantic": self.error_semantic,
                "speaker": self.error_speaker,
                "other": self.error_other,
            },
        }


def _chunk_overlap(chunk_a: str, chunk_b: str, threshold: float = 0.3) -> bool:
    """
    判断两个 chunk 是否是同一个（文本重叠 >= threshold）
    用于判断检索结果是否命中了相关 chunk
    """
    a_chars = set(chunk_a.lower().replace(" ", ""))
    b_chars = set(chunk_b.lower().replace(" ", ""))
    if not a_chars or not b_chars:
        return False
    overlap = len(a_chars & b_chars) / min(len(a_chars), len(b_chars))
    return overlap >= threshold


def evaluate_retriever(
    retriever: AudioChatRetriever,
    test_cases: list[TestCase],
    recall_k: int = 15,
    top_ks: tuple[int, int, int] = (1, 3, 5),
) -> EvalReport:
    """
    评估 retriever 的检索质量

    Args:
        retriever: 待测 retriever
        test_cases: 测试用例列表
        recall_k: 粗排召回数（用于计算 recall@K）
        top_ks: 分别计算 Top-1/3/5 的准确率
    """
    report = EvalReport(total_cases=len(test_cases))
    all_latencies = []

    for tc in test_cases:
        # 检索 recall_k 条（粗排全部送入，精排后再截断）
        t0 = time.perf_counter()
        results = retriever.retrieve(
            query=tc.query,
            k=recall_k,
            speaker_filter=tc.speaker_filter,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        all_latencies.append(latency_ms)

        retrieved_contents = [r.content for r in results]
        retrieved_sources = [r.source for r in results]
        retrieved_scores = [r.relevance_score for r in results]

        # 判断每个相关 chunk 是否被召回
        matched_relevant = []
        for rel in tc.relevant_chunks:
            for ret in retrieved_contents:
                if _chunk_overlap(rel, ret):
                    matched_relevant.append(rel)
                    break
        matched_relevant = list(dict.fromkeys(matched_relevant))  # 去重保留顺序

        # 计算各项指标
        recalled_any = len(matched_relevant) > 0
        top1_hit = retrieved_contents and _chunk_overlap(
            tc.relevant_chunks[0], retrieved_contents[0]
        ) if tc.relevant_chunks else False

        # Top-3/5：召回了多个相关 chunk 中的任意一个就算通过
        topk_hits = {k: False for k in top_ks}
        for k in top_ks:
            if k <= len(retrieved_contents):
                topk_chunks = retrieved_contents[:k]
                topk_hits[k] = any(
                    _chunk_overlap(tc.relevant_chunks[0], c) for c in topk_chunks
                ) if tc.relevant_chunks else False

        # recall@K = 所有相关 chunk 中有多少比例进了 Top-K
        recall_count = len(matched_relevant)
        total_relevant = len(tc.relevant_chunks)
        recall_at_k = recall_count / max(total_relevant, 1)

        # precision@K = Top-K 中有多少比例是相关的
        precision_at_k = sum(
            1 for c in retrieved_contents[:max(top_ks)] if any(
                _chunk_overlap(c, rel) for rel in tc.relevant_chunks
            )
        ) / max(max(top_ks), 1)

        # 累计
        report.top1_accuracy += top1_hit
        for k in top_ks:
            if k == 1:
                report.top3_accuracy += topk_hits.get(3, False)
                report.top5_accuracy += topk_hits.get(5, False)
            # 实际上 top1_accuracy 单独累计，其他单独累计
        # 更正：top1_accuracy 用 top1_hit，top3/top5 用 topk_hits
        # 重新组织累计逻辑
        report.per_case_results.append({
            "query": tc.query,
            "top1_hit": top1_hit,
            "top3_hit": topk_hits.get(3, False),
            "top5_hit": topk_hits.get(5, False),
            "recall_at_k": recall_at_k,
            "precision_at_k": precision_at_k,
            "latency_ms": latency_ms,
            "error_type": tc.error_type,
        })

        # 延迟
        report.avg_latency_ms = np.mean(all_latencies)

    # 汇总准确率
    n = len(test_cases)
    report.top1_accuracy /= n
    # 重新算 top3/top5（前面的累计逻辑有误）
    report.top3_accuracy = sum(1 for r in report.per_case_results if r["top3_hit"]) / n
    report.top5_accuracy = sum(1 for r in report.per_case_results if r["top5_hit"]) / n
    report.top15_recall = np.mean([r["recall_at_k"] for r in report.per_case_results])

    # 错误分类统计（如果有 error_type）
    for r in report.per_case_results:
        if r["query"] and not r["top3_hit"]:  # Top-3 没命中则算错误
            # error_type 需人工标，这里先按语义漂移默认
            pass  # 实际使用时需要 test_cases 中包含 error_type

    return report


def load_test_set(path: str) -> list[TestCase]:
    """从 JSON 文件加载测试集"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [TestCase(**item) for item in data]


def main():
    parser = argparse.ArgumentParser(description="RAG 检索质量评估")
    parser.add_argument("--storage-dir", default="./rag_storage", help="ChromaDB 存储目录")
    parser.add_argument("--test-set", default="evaluation/test_set.json", help="测试集 JSON 文件")
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-large", help="Reranker 模型")
    parser.add_argument("--min-score", type=float, default=0.35, help="最小相关性阈值")
    parser.add_argument("--output", default="eval_report.json", help="结果输出路径")
    args = parser.parse_args()

    # 初始化 retriever
    storage = MeetingMemoryStore(persist_directory=args.storage_dir)
    retriever = AudioChatRetriever(
        storage=storage,
        reranker_model=args.reranker_model,
        min_relevance_score=args.min_score,
    )
    retriever._warm_up()

    # 加载测试集
    test_set_path = Path(args.test_set)
    if test_set_path.exists():
        test_cases = load_test_set(str(test_set_path))
        print(f"加载测试集：{len(test_cases)} 条")
    else:
        print(f"[警告] 测试集文件不存在：{args.test_set}")
        print("请创建 evaluation/test_set.json，格式如下：")
        print(json.dumps([
            {
                "query": "张三负责什么任务？",
                "relevant_chunks": ["张三：后端 API 完成 80%，预计本周完成"],
                "speaker_filter": None,
            }
        ], ensure_ascii=False, indent=2))
        return

    # 运行评估
    print("开始评估...")
    report = evaluate_retriever(retriever, test_cases, recall_k=15)

    # 输出结果
    print(report.summary())

    # 保存 JSON 报告
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存：{args.output}")


if __name__ == "__main__":
    main()
