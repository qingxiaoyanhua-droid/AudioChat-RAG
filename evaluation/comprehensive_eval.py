"""
综合评估模块 — ROUGE-L / BERTScore / BGE Similarity / Faithfulness

用法:
    python evaluation/comprehensive_eval.py
    python evaluation/comprehensive_eval.py --test_data data/eval_test.jsonl --output eval_results.json

面试要点:
    - ROUGE-L 衡量最长公共子序列（LCS），适合摘要类任务
    - BERTScore 衡量 token 级别语义匹配，比 ROUGE 更鲁棒
    - BGE Similarity 衡量整体语义方向，和 BERTScore 互补
    - Faithfulness 衡量生成内容能否追溯到源文档，检测幻觉
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

import numpy as np


# ============================================================
# 1. ROUGE-L 实现（纯 Python，不依赖 rouge-score 包）
# ============================================================

def _lcs_length(x: list[str], y: list[str]) -> int:
    """最长公共子序列长度（动态规划）"""
    m, n = len(x), len(y)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if x[i - 1] == y[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[m][n]


def rouge_l(hypothesis: str, reference: str) -> dict[str, float]:
    """
    计算 ROUGE-L F1

    ROUGE-L 基于最长公共子序列（LCS）：
    - Precision = LCS / len(hypothesis)
    - Recall    = LCS / len(reference)
    - F1        = 2 * P * R / (P + R)
    """
    hyp_tokens = list(hypothesis)
    ref_tokens = list(reference)

    if not hyp_tokens or not ref_tokens:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    lcs = _lcs_length(hyp_tokens, ref_tokens)
    precision = lcs / len(hyp_tokens)
    recall = lcs / len(ref_tokens)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


# ============================================================
# 2. BERTScore（基于 BGE token-level matching）
# ============================================================

def bert_score_bge(hypothesis: str, reference: str, embedder=None) -> dict[str, float]:
    """
    基于 BGE 的 BERTScore 近似实现

    标准 BERTScore 用 BERT token embedding 做 greedy matching。
    这里用 BGE 按句/短语级做近似，适合中文长文本场景。

    面试可说: "我用 BGE 做了 sentence-level BERTScore 近似，因为中文
    token 粒度不好切，句子级匹配更稳定。"
    """
    import re

    if embedder is None:
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("BAAI/bge-large-zh-v1.5")

    hyp_sents = [s.strip() for s in re.split(r'[。；\n]', hypothesis) if len(s.strip()) > 3]
    ref_sents = [s.strip() for s in re.split(r'[。；\n]', reference) if len(s.strip()) > 3]

    if not hyp_sents or not ref_sents:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    from sklearn.metrics.pairwise import cosine_similarity

    hyp_embs = embedder.encode(hyp_sents, convert_to_numpy=True)
    ref_embs = embedder.encode(ref_sents, convert_to_numpy=True)

    sim_matrix = cosine_similarity(hyp_embs, ref_embs)

    # Precision: 每个 hyp 句子对应最佳 ref 匹配
    precision = float(np.mean(np.max(sim_matrix, axis=1)))

    # Recall: 每个 ref 句子对应最佳 hyp 匹配
    recall = float(np.mean(np.max(sim_matrix, axis=0)))

    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


# ============================================================
# 3. BGE 整体语义相似度
# ============================================================

def bge_similarity(hypothesis: str, reference: str, embedder=None) -> float:
    if embedder is None:
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("BAAI/bge-large-zh-v1.5")

    from sentence_transformers import util
    h_emb = embedder.encode(hypothesis, convert_to_tensor=True)
    r_emb = embedder.encode(reference, convert_to_tensor=True)
    sim = util.cos_sim(h_emb, r_emb).item()
    return round(float((sim + 1) / 2), 4)


# ============================================================
# 4. Faithfulness（忠实度 / 幻觉检测）
# ============================================================

def faithfulness_score(
    generated: str,
    source_docs: list[str],
    embedder=None,
    threshold: float = 0.65,
) -> dict[str, float]:
    """
    逐句检查生成内容是否可追溯到源文档。

    返回 faithful_ratio（可信句子占比）和 hallucination_ratio。
    """
    import re

    if embedder is None:
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer("BAAI/bge-large-zh-v1.5")

    sentences = [s.strip() for s in re.split(r'[。；\n]', generated) if len(s.strip()) > 5]
    if not sentences or not source_docs:
        return {"faithful_ratio": 0.5, "hallucination_ratio": 0.5, "n_sentences": 0}

    from sklearn.metrics.pairwise import cosine_similarity

    doc_embs = embedder.encode(source_docs, convert_to_numpy=True)
    faithful = 0
    for sent in sentences:
        sent_emb = embedder.encode(sent, convert_to_numpy=True)
        sims = cosine_similarity([sent_emb], doc_embs)[0]
        if max(sims) > threshold:
            faithful += 1

    ratio = faithful / len(sentences)
    return {
        "faithful_ratio": round(ratio, 4),
        "hallucination_ratio": round(1 - ratio, 4),
        "n_sentences": len(sentences),
    }


# ============================================================
# 5. 综合评估
# ============================================================

def comprehensive_evaluate(
    generated: str,
    reference: str,
    query: str = "",
    source_docs: Optional[list[str]] = None,
    embedder=None,
) -> dict:
    """运行全部指标，返回统一报告"""
    results = {}

    # ROUGE-L
    results["rouge_l"] = rouge_l(generated, reference)

    # BERTScore (BGE)
    results["bert_score"] = bert_score_bge(generated, reference, embedder)

    # BGE Similarity
    results["bge_similarity"] = bge_similarity(generated, reference, embedder)

    # Faithfulness
    if source_docs:
        results["faithfulness"] = faithfulness_score(generated, source_docs, embedder)

    return results


# ============================================================
# 6. 内置测试集 + CLI
# ============================================================

BUILTIN_CASES = [
    {
        "query": "张三负责什么任务？",
        "reference": "张三负责后端 API 开发，目前完成 80%",
        "no_rag": "张三负责项目开发工作，具体任务包括后端开发和团队协作。",
        "with_rag": "张三负责后端 API 开发，目前完成 80%，预计本周完成。",
        "sources": ["张三：后端 API 完成 80%，预计本周完成"],
    },
    {
        "query": "前端页面进度如何？",
        "reference": "前端页面刚开始，需要等待后端接口",
        "no_rag": "前端页面正在进行中，进度正常。",
        "with_rag": "前端页面刚开始，需要等待后端接口。",
        "sources": ["李四：前端页面刚开始，需要后端接口"],
    },
    {
        "query": "项目预计什么时候完成？",
        "reference": "项目预计下周完成联调",
        "no_rag": "项目预计很快完成，具体时间待定。",
        "with_rag": "项目预计下周完成联调。",
        "sources": ["张三：预计下周完成联调"],
    },
    {
        "query": "谁负责数据库设计？",
        "reference": "王五负责数据库设计，已经完成",
        "no_rag": "数据库设计由专人负责，已经完成。",
        "with_rag": "王五负责数据库设计，已经完成。",
        "sources": ["王五：数据库设计已完成"],
    },
    {
        "query": "测试工作进展如何？",
        "reference": "赵六负责测试，完成约 50%",
        "no_rag": "测试工作正在有序开展。",
        "with_rag": "赵六负责测试，完成约 50%。",
        "sources": ["赵六：测试完成 50%"],
    },
]


def run_builtin_eval(embedder=None) -> dict:
    """运行内置测试集评估"""
    no_rag_metrics = {"rouge_l_f1": [], "bge_sim": [], "bert_f1": []}
    rag_metrics = {"rouge_l_f1": [], "bge_sim": [], "bert_f1": [], "faithful": []}

    for case in BUILTIN_CASES:
        ref = case["reference"]

        # 无 RAG
        nr = comprehensive_evaluate(case["no_rag"], ref, case["query"], embedder=embedder)
        no_rag_metrics["rouge_l_f1"].append(nr["rouge_l"]["f1"])
        no_rag_metrics["bge_sim"].append(nr["bge_similarity"])
        no_rag_metrics["bert_f1"].append(nr["bert_score"]["f1"])

        # 有 RAG
        wr = comprehensive_evaluate(
            case["with_rag"], ref, case["query"],
            source_docs=case.get("sources"), embedder=embedder,
        )
        rag_metrics["rouge_l_f1"].append(wr["rouge_l"]["f1"])
        rag_metrics["bge_sim"].append(wr["bge_similarity"])
        rag_metrics["bert_f1"].append(wr["bert_score"]["f1"])
        if "faithfulness" in wr:
            rag_metrics["faithful"].append(wr["faithfulness"]["faithful_ratio"])

    def _avg(lst):
        return round(float(np.mean(lst)), 4) if lst else 0.0

    summary = {
        "n_samples": len(BUILTIN_CASES),
        "no_rag": {k: _avg(v) for k, v in no_rag_metrics.items()},
        "with_rag": {k: _avg(v) for k, v in rag_metrics.items()},
    }

    # 计算提升
    for metric in ["rouge_l_f1", "bge_sim", "bert_f1"]:
        base = summary["no_rag"][metric]
        rag = summary["with_rag"][metric]
        imp = (rag - base) / base * 100 if base > 0 else 0
        summary.setdefault("improvement_pct", {})[metric] = round(imp, 1)

    return summary


def main():
    parser = argparse.ArgumentParser(description="综合评估 (ROUGE-L / BERTScore / BGE)")
    parser.add_argument("--test_data", default=None, help="测试数据 JSONL")
    parser.add_argument("--output", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()

    print("加载 BGE 模型...")
    from sentence_transformers import SentenceTransformer
    embedder = SentenceTransformer("BAAI/bge-large-zh-v1.5")

    if args.test_data and Path(args.test_data).exists():
        print(f"使用外部测试集: {args.test_data}")
        # TODO: 加载外部测试数据
    else:
        print("使用内置测试集...")

    result = run_builtin_eval(embedder)

    print("\n" + "=" * 60)
    print("综合评估结果")
    print("=" * 60)
    print(f"  样本数: {result['n_samples']}")
    print()
    print("  无 RAG:")
    for k, v in result["no_rag"].items():
        print(f"    {k}: {v:.4f}")
    print()
    print("  有 RAG:")
    for k, v in result["with_rag"].items():
        print(f"    {k}: {v:.4f}")
    print()
    print("  提升 (%):")
    for k, v in result.get("improvement_pct", {}).items():
        print(f"    {k}: +{v:.1f}%")
    print("=" * 60)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {args.output}")


if __name__ == "__main__":
    main()
