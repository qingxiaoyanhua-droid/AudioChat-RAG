# scripts/ — 脚本入口

## 核心脚本（常用）

| 脚本 | 说明 |
|------|------|
| `offline_pipeline.py` | 离线 Pipeline 入口（文本输出） |
| `offline_pipeline_workflow.py` | 完整工作流（含意图识别 + Agentic RAG + HITL 审核） |
| `offline_pipeline_s2s.py` | S2S 模式（语音输出） |
| `task_cli.py` | 任务审核 CLI |
| `run_ablations.py` | 消融实验脚本 |
| `api_server.py` | API 服务入口 |

## 训练脚本（根目录）

| 脚本 | 说明 |
|------|------|
| `train_sft.py` | SFT 监督微调 |
| `train_grpo.py` | GRPO 强化学习 |
| `prepare_data.py` | 数据生成脚本 |
| `setup_rag_db.py` | RAG 数据库搭建 |
| `grpo_reward_function.py` | GRPO 奖励函数 |
| `grpo_multitask_reward.py` | 多任务奖励函数 |
| `grpo_training_simple.py` | 简化版 GRPO 训练 |
| `sft_loss_function.py` | SFT Loss（含 Response-only masking）|
| `compute_wer.py` | WER/CER 计算脚本 |
| `plot_training_curves.py` | 训练曲线绘制 |
| `monitor_training.py` | 训练过程监控 |

## 归档脚本

| 目录 | 说明 |
|------|------|
| `scripts/archives/` | 不常用的启动/部署脚本（run_*.sh, deploy_*.sh） |
| `scripts/demos/` | 演示/测试脚本 |
