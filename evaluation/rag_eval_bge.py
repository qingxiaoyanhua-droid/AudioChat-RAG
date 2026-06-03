"""
RAG 效果评估（真实 BGE 版本）

使用 BGE Embedding + 余弦相似度计算无 RAG / 有 RAG 的问答准确性
替代 test_rag_accuracy.py 的 char overlap 模拟

用法:
    python evaluation/rag_eval_bge.py
    python evaluation/rag_eval_bge.py --test_data data/rag_eval_test.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


def get_embedder():
    """懒加载 BGE 模型"""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("BAAI/bge-large-zh-v1.5")


def compute_bge_similarity(embedder, text_a: str, text_b: str) -> float:
    """BGE 余弦相似度，归一化到 0-1"""
    from sentence_transformers import util
    a_emb = embedder.encode(text_a, convert_to_tensor=True)
    b_emb = embedder.encode(text_b, convert_to_tensor=True)
    sim = util.cos_sim(a_emb, b_emb).item()
    return float((sim + 1) / 2)


def load_test_data(path: str | None) -> list[dict]:
    """加载测试数据"""
    if path and Path(path).exists():
        cases = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    cases.append(json.loads(line))
        return cases
    
    # 内置默认测试集（与 test_rag_accuracy 对齐）
    return [
        {"query": "张三负责什么任务？", "answer": "张三负责后端 API 开发，目前完成 80%",
         "no_rag": "张三负责项目开发工作，具体任务包括后端开发和团队协作。",
         "with_rag": "张三负责后端 API 开发，目前完成 80%，预计本周完成。"},
        {"query": "前端页面进度如何？", "answer": "前端页面刚开始，需要等待后端接口",
         "no_rag": "前端页面正在进行中，进度正常。",
         "with_rag": "前端页面刚开始，需要等待后端接口。"},
        {"query": "项目预计什么时候完成？", "answer": "项目预计下周完成联调",
         "no_rag": "项目预计很快完成，具体时间待定。",
         "with_rag": "项目预计下周完成联调。"},
        {"query": "谁负责数据库设计？", "answer": "王五负责数据库设计，已经完成",
         "no_rag": "数据库设计由专人负责，已经完成。",
         "with_rag": "王五负责数据库设计，已经完成。"},
        {"query": "测试工作进展如何？", "answer": "赵六负责测试，完成约 50%",
         "no_rag": "测试工作正在有序开展。",
         "with_rag": "赵六负责测试，完成约 50%。"},
    ]


def run_eval(test_cases: list[dict], embedder) -> dict:
    """运行评估"""
    no_rag_scores = []
    with_rag_scores = []
    
    for case in test_cases:
        ans = case["answer"]
        no_rag = case.get("no_rag", "")
        with_rag = case.get("with_rag", "")
        
        s_no = compute_bge_similarity(embedder, no_rag, ans)
        s_yes = compute_bge_similarity(embedder, with_rag, ans)
        no_rag_scores.append(s_no)
        with_rag_scores.append(s_yes)
    
    no_rag_avg = float(np.mean(no_rag_scores))
    with_rag_avg = float(np.mean(with_rag_scores))
    improvement = (with_rag_avg - no_rag_avg) / no_rag_avg if no_rag_avg > 0 else 0
    
    return {
        "no_rag_avg": round(no_rag_avg, 4),
        "with_rag_avg": round(with_rag_avg, 4),
        "improvement_pct": round(improvement * 100, 1),
        "n_samples": len(test_cases),
        "no_rag_scores": [round(s, 4) for s in no_rag_scores],
        "with_rag_scores": [round(s, 4) for s in with_rag_scores],
    }


def main():
    parser = argparse.ArgumentParser(description="RAG 效果评估（BGE）")
    parser.add_argument("--test_data", default=None, help="测试数据 JSONL 路径")
    parser.add_argument("--output", default=None, help="结果 JSON 输出路径")
    args = parser.parse_args()
    
    print("加载 BGE 模型...")
    embedder = get_embedder()
    
    cases = load_test_data(args.test_data)
    print(f"测试样本数: {len(cases)}")
    
    result = run_eval(cases, embedder)
    
    print("=" * 50)
    print("RAG 效果评估结果（BGE 语义相似度）")
    print("=" * 50)
    print(f"无 RAG 平均准确性: {result['no_rag_avg']:.2f}")
    print(f"有 RAG 平均准确性: {result['with_rag_avg']:.2f}")
    print(f"提升: {result['improvement_pct']:.1f}%")
    print("=" * 50)
    
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已保存: {args.output}")


if __name__ == "__main__":
    main()
