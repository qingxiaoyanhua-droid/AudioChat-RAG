"""
计算 WER（Word Error Rate）

用法:
    python compute_wer.py --predict "predict text" --label "label text"
    python compute_wer.py --predict_file predict.txt --label_file label.txt
    python compute_wer.py --predict "hello world" --label "hello world"  # 期望 WER = 0
"""

import argparse
import re
from typing import List


def tokenize(text: str) -> List[str]:
    """将文本按空格和标点分词，转小写"""
    tokens = re.findall(r'\b\w+\b', text.lower())
    return tokens


def levenshtein_distance(ref: List[str], hyp: List[str]) -> int:
    """计算两个 token 序列之间的编辑距离（Levenshtein Distance）"""
    m, n = len(ref), len(hyp)
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j],      # deletion
                                   dp[i][j - 1],      # insertion
                                   dp[i - 1][j - 1])  # substitution

    return dp[m][n]


def compute_wer(predict: str, label: str) -> dict:
    """
    计算 WER

    WER = 编辑距离 / 参考词数 * 100%
    """
    ref_tokens = tokenize(label)
    hyp_tokens = tokenize(predict)

    distance = levenshtein_distance(ref_tokens, hyp_tokens)
    ref_len = len(ref_tokens)

    if ref_len == 0:
        wer = 0.0 if len(hyp_tokens) == 0 else float('inf')
    else:
        wer = distance / ref_len

    return {
        "predict": predict,
        "label": label,
        "predict_tokens": hyp_tokens,
        "label_tokens": ref_tokens,
        "distance": distance,
        "ref_len": ref_len,
        "wer_percent": round(wer * 100, 2),
        "wer": round(wer, 4),
    }


def main():
    parser = argparse.ArgumentParser(description="计算 WER（Word Error Rate）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--predict", type=str, help="预测文本")
    group.add_argument("--predict_file", type=str, help="预测文本文件路径")
    group.add_argument("--predict_dir", type=str, help="预测文本目录（目录下所有 .txt 文件）")
    group.add_argument("--sft_file", type=str, help="SFT 输出文件（自动取 predict 列）")
    parser.add_argument("--label", type=str, help="参考文本")
    parser.add_argument("--label_file", type=str, help="参考文本文件路径")
    parser.add_argument("--output", type=str, help="输出报告文件路径")
    args = parser.parse_args()

    results = []

    # --- 单条对比 ---
    if args.predict and args.label:
        r = compute_wer(args.predict, args.label)
        results.append(r)

    # --- 文件对比 ---
    elif args.predict_file and args.label_file:
        with open(args.predict_file, "r", encoding="utf-8") as f:
            predicts = [line.strip() for line in f if line.strip()]
        with open(args.label_file, "r", encoding="utf-8") as f:
            labels = [line.strip() for line in f if line.strip()]

        if len(predicts) != len(labels):
            print(f"警告: predict 文件有 {len(predicts)} 行，label 文件有 {len(labels)} 行，行数不一致！")
            min_len = min(len(predicts), len(labels))
            predicts = predicts[:min_len]
            labels = labels[:min_len]

        for i, (p, l) in enumerate(zip(predicts, labels)):
            r = compute_wer(p, l)
            r["id"] = i
            results.append(r)

    # --- 目录批量对比 ---
    elif args.predict_dir:
        import os
        from pathlib import Path

        predict_dir = Path(args.predict_dir)
        for txt_file in sorted(predict_dir.glob("*.txt")):
            label_file = predict_dir / txt_file.name
            if label_file.exists():
                with open(txt_file, "r", encoding="utf-8") as f:
                    p = f.read().strip()
                with open(label_file, "r", encoding="utf-8") as f:
                    l = f.read().strip()
                r = compute_wer(p, l)
                r["file"] = txt_file.name
                results.append(r)

    # --- SFT 文件对比（自动取 predict 和 label 列）---
    elif args.sft_file:
        import json

        with open(args.sft_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                predict = obj.get("predict", obj.get("generated", obj.get("response", "")))
                label = obj.get("label", obj.get("reference", obj.get("ground_truth", "")))
                r = compute_wer(predict, label)
                r["id"] = len(results)
                results.append(r)

    # --- 输出 ---
    if not results:
        print("没有可比较的内容。")
        return

    # 打印每条结果
    print(f"\n{'='*70}")
    print(f"{'ID':<6} {'WER%':<10} {'Dist':<6} {'RefLen':<8}  样例")
    print(f"{'='*70}")
    for r in results:
        fid = r.get("id", r.get("file", ""))
        sample_label = " ".join(r["label_tokens"][:5])
        sample_predict = " ".join(r["predict_tokens"][:5])
        sample = f"label: [{sample_label}...]"
        print(f"{fid:<6} {r['wer_percent']:<10.2f} {r['distance']:<6} {r['ref_len']:<8}  {sample}")

    # 汇总统计
    total_dist = sum(r["distance"] for r in results)
    total_ref_len = sum(r["ref_len"] for r in results)
    avg_wer = total_dist / total_ref_len * 100 if total_ref_len > 0 else 0.0

    print(f"{'='*70}")
    print(f"样本数:      {len(results)}")
    print(f"总编辑距离:  {total_dist}")
    print(f"总参考词数:  {total_ref_len}")
    print(f"平均 WER:    {avg_wer:.2f}%")
    print(f"{'='*70}")

    # 写入文件
    if args.output:
        import json
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({
                "summary": {
                    "num_samples": len(results),
                    "total_distance": total_dist,
                    "total_ref_len": total_ref_len,
                    "avg_wer_percent": round(avg_wer, 2),
                },
                "details": results
            }, f, ensure_ascii=False, indent=2)
        print(f"\n详细报告已保存到: {args.output}")


if __name__ == "__main__":
    main()
