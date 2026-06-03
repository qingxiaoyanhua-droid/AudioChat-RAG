#!/bin/bash
# ============================================================
# run_mini_eval.sh — 小范围实验一键脚本
# 
# 用途：快速跑通 RAG 建库 → 综合评估 → 消融实验，产出真实日志
# 预计耗时：10-15 分钟（A100 上）
# ============================================================

set -e

echo "============================================================"
echo "小范围实验流程"
echo "============================================================"
echo "开始时间: $(date)"
echo ""

# 1. 生成数据（50 场会议，够跑实验就行）
echo "===== Step 1: 生成 50 场会议数据 ====="
python3 prepare_data.py \
    --output_dir data_mini \
    --task rag \
    --num_rag 50 \
    --seed 42
echo ""

# 2. 搭建 RAG 数据库
echo "===== Step 2: 搭建 RAG 数据库 ====="
python3 setup_rag_db.py \
    --storage_dir ./rag_storage_mini \
    --data_dir data_mini/meeting_records \
    --test_queries "后端进度如何" "谁负责数据库设计" "项目预计什么时候完成" \
    --skip_test
echo ""

# 3. 运行综合评估
echo "===== Step 3: 运行综合评估 ====="
python3 evaluation/comprehensive_eval.py \
    --output eval_results_mini.json
echo ""

# 4. 运行消融实验
echo "===== Step 4: 运行消融实验 ====="
mkdir -p experiments
python3 scripts/run_ablations.py \
    --storage_dir ./rag_storage_mini \
    --queries \
        "后端进度如何" \
        "前端完成情况" \
        "测试覆盖率目标" \
        "谁负责数据库设计" \
        "项目预计什么时候完成" \
        "上周讨论了什么" \
        "张三负责什么" \
        "技术方案是什么" \
        "预算多少" \
        "最近的进度" \
    --output experiments/ablations_results_mini.md
echo ""

echo "============================================================"
echo "全部完成！"
echo "结束时间: $(date)"
echo "============================================================"
echo ""
echo "产出文件:"
echo "  - eval_results_mini.json       (综合评估结果)"
echo "  - experiments/ablations_results_mini.md  (消融实验结果)"
echo "  - rag_storage_mini/            (RAG 数据库)"
echo ""
echo "查看结果:"
echo "  cat eval_results_mini.json"
echo "  cat experiments/ablations_results_mini.md"
