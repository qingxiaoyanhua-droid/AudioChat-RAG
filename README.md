# AudioChat-RAG

**多模态语音对话系统** - 支持 RAG 检索增强和 GRPO 质量评估

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

---

## 🌟 特性

- 🎤 **说话人分离** - 3D-Speaker (CAM++/VAD)
- 📝 **语音识别** - FunASR (Paraformer/SenseVoice)
- 🤖 **LLM 对话** - Fun-Audio-Chat-8B
- 🔊 **语音合成** - CosyVoice-0.5B
- 🔍 **RAG 检索增强** - ChromaDB + BGE 两阶段（粗排+精排）+ Soft Decay
- 🌐 **意图识别路由** - 规则兜底 + 小模型精判，三类意图分流检索（Phase 2）
- 🚀 **Agentic RAG** - 信息缺口分析 + 并发批量检索 + 上下文压缩（Phase 3）
- 🏛️ **分层 RAG** - L1/L2/L3/L4 四层知识体系（人物画像、需求演进轨迹）
- 📊 **GRPO 强化学习** - 9维奖励函数 + 软 gate 机制
- ✅ **HITL 质量报告** - AI 质量评估 + 人工决策
- 📈 **SFT 微调** - LoRA 高效参数微调

---

## 🚀 快速开始

### 安装依赖

```bash
# 基础依赖（已有）
pip install torch transformers soundfile

# RAG 依赖（新增）
pip install -r requirements_rag.txt

# 训练依赖（SFT/GRPO）
pip install datasets accelerate peft
```

### 快速演示（1 分钟）

```bash
# 生成数据 + 搭建 RAG + 运行演示
python scripts/offline_pipeline_workflow.py --audio examples/2speakers_example.wav
```

### 手动运行

```bash
# 文本输出
python scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --funasr-model /data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch \
  --funaudiochat-model /data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B \
  --output-dir AudioChat_saves

# RAG 增强
python scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --enable-rag \
  --meeting-id meeting_001 \
  --output-dir output_rag

# QA 模式（意图识别路由）
python scripts/offline_pipeline_workflow.py \
  --audio examples/2speakers_example.wav \
  --enable-rag --mode qa \
  --query "这场会上张三说了什么"

# 总结模式 + Agentic RAG
python scripts/offline_pipeline_workflow.py \
  --audio examples/2speakers_example.wav \
  --enable-rag --agentic-rag \
  --time-range 2026-05-01,2026-06-03

# GRPO 评估
python evaluation/grpo_eval.py \
  --audio output_rag/output_audio_xxx.wav \
  --reference "参考文本" \
  --generated "生成的文本" \
  --output eval_report.json
```

### 训练流程

```bash
# 1. 准备训练数据
python prepare_data.py --output_dir data --num_sft 1000 --num_grpo 1000

# 2. 搭建 RAG 数据库
python setup_rag_db.py --storage_dir ./rag_storage --data_dir data/meeting_records

# 3. SFT 训练
python train_sft.py \
  --model_name_or_path /data/models/Fun-Audio-Chat-8B \
  --train_data data/sft_train_data.jsonl \
  --output_dir saves/sft_model

# 4. GRPO 训练
python train_grpo.py \
  --model_name_or_path saves/sft_model \
  --train_data data/grpo_train_data.jsonl \
  --output_dir saves/grpo_model
```

---

## 📊 实验结果

### RAG 效果提升

| 指标 | 无 RAG | 有 RAG | 提升 |
|------|--------|--------|------|
| 回答准确性 | 0.64 | 0.84 | **+31%** |
| 信息完整性 | 0.58 | 0.79 | **+36%** |

### GRPO 评估结果

| 维度 | 平均得分 |
|------|----------|
| CER 分数 | 0.68 |
| 流畅度 | 0.83 |
| 相关性 | 0.79 |
| **综合** | **0.76** |

详细实验数据：[experiments/results.md](experiments/results.md)

---

## 📁 项目结构

```
DiTX-Clerk/
├── audiochat/              # 核心库
│   ├── asr/               # ASR 模块
│   ├── diarization/       # 说话人分离
│   ├── llm/               # LLM 模块
│   └── rag/               # RAG 检索（新增）
├── scripts/               # CLI 入口
│   ├── offline_pipeline.py
│   └── offline_pipeline_s2s.py
├── evaluation/            # 评估模块（新增）
│   └── grpo_eval.py
├── experiments/           # 实验记录
├── docs/                  # 文档
│   ├── 面试准备.md
│   └── 训练指南.md
├── third_party/           # 第三方代码
├── utils/                 # 工具函数
├── train_sft.py           # SFT 训练
├── train_grpo.py         # GRPO 训练
├── prepare_data.py       # 数据生成
├── setup_rag_db.py      # RAG 建库
├── grpo_reward_function.py  # 奖励函数
├── scripts/              # Pipeline 入口
│   ├── offline_pipeline.py
│   ├── offline_pipeline_workflow.py  # 含意图识别 + Agentic RAG
│   ├── task_cli.py
│   ├── archives/         # 归档脚本（部署/运行入口）
│   └── demos/            # 演示脚本
```

---

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                    DiTX-Clerk Pipeline                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  输入音频 → 说话人分离 → ASR → RAG 检索增强 → LLM → 输出  │
│     │           │           │       │       │      │        │
│     │           │           │       │       │      ├─ 文本   │
│     │           │           │       │       │              │
│     │           │           │       │       └──── 语音      │
│     │           │           │       │                        │
│     │           │           │       └─ 意图识别路由           │
│     │           │           │           ├─ 当前会议：精确过滤│
│     │           │           │           ├─ 历史会议：时间过滤│
│     │           │           │           └─ Agentic RAG      │
│     │           │           │                                │
│     │           │           └─ FunASR (Paraformer)           │
│     │           │                                            │
│     │           └─ 3D-Speaker (CAM++/VAD)                   │
│     │                                                        │
│     └─ 16kHz Mono 预处理                                    │
│                                                              │
├─────────────────────────────────────────────────────────────┤
│              分层 RAG 知识体系                               │
│  L1 索引层 ─── ChromaDB metadata (always-on)              │
│  L2 事实层 ─── ChromaDB facts + JSON 人物画像              │
│  L3 SOP 层 ─── ChromaDB SOP + JSON 需求演进轨迹            │
│  L4 存档层 ─── JSONL 原始会议记录                          │
└─────────────────────────────────────────────────────────────┘
```

---

## 📖 文档

- **文档总入口**: [docs/summary/00-总索引.md](docs/summary/00-总索引.md) — 整理后的精华文档，按需查阅
- **面试记录**: [interviews/](interviews/) — 所有面试问答记录
- **RAG 文档**: [docs/README_RAG.md](docs/README_RAG.md)
- **实验记录**: [experiments/results.md](experiments/results.md)
- **技术决策**: [PROJECT_WORKLOG.md](PROJECT_WORKLOG.md)
- **项目全貌**: [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md)

---

## 🖥️ 服务器部署

### 步骤 1: 上传代码

```bash
scp -r /Users/setupmac/DiTX-Clerk root@server:/path/to/
```

### 步骤 2: SSH 登录

```bash
ssh root@server
cd /path/to/DiTX-Clerk
```

### 步骤 3: 运行部署脚本

```bash
chmod +x deploy_server.sh
./deploy_server.sh
```

### 步骤 4: 运行 Pipeline

```bash
# 基础模式
/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3 scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --output-dir output_test

# RAG 模式
/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3 scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --enable-rag \
  --meeting-id meeting_001 \
  --output-dir output_rag
```

---

## 🎯 面试准备

### 1 分钟项目介绍

> "我开发了一个多模态语音对话系统 AudioChat-RAG。核心流程是：输入音频经过说话人分离、ASR 识别后，通过 RAG 检索历史会议记录增强 LLM 生成，最终输出文本或语音回复。
>
> 技术亮点：
> 1. **RAG 检索增强** - ChromaDB+BGE 语义检索，支持时间衰减重排序
> 2. **GRPO 强化学习** - 多维度奖励函数（CER+ 流畅度 + 相关性）
> 3. **端到端优化** - 从音频到回复的全流程自动化
>
> 实验结果显示，RAG 将回答准确性提升了约 31%。"

### 技术要点

| 模块 | 技术点 | 面试价值 |
|------|--------|----------|
| **RAG** | Bi-Encoder粗排 + Cross-Encoder精排 + Soft Decay + **意图识别路由** | ⭐⭐⭐⭐⭐ |
| **Agentic RAG** | 信息缺口分析 + 并发批量检索 + 上下文压缩 | ⭐⭐⭐⭐⭐ |
| **GRPO** | 9维奖励函数 + 软 gate 机制 + 串行 reward 设计 | ⭐⭐⭐⭐⭐ |
| SFT | LoRA 高效微调 | ⭐⭐⭐⭐ |
| Pipeline | 多模态端到端集成 + HITL 质量报告 | ⭐⭐⭐⭐ |

### 快速运行演示

```bash
# 1 分钟演示（生成数据+ 搭建 RAG+ 运行测试）
python scripts/offline_pipeline_workflow.py --audio examples/2speakers_example.wav
```

### 详细文档

- **面试准备**: `docs/面试准备.md` - 常见问题、手撕代码
- **训练指南**: `docs/训练指南.md` - A100 训练流程、参数调优

---

## 📝 License

Apache 2.0

---

## 🙏 致谢

- [FunASR](https://github.com/alibaba-damo-academy/FunASR)
- [3D-Speaker](https://github.com/alibaba-damo-academy/3D-Speaker)
- [CosyVoice](https://github.com/FunAudioLLM/CosyVoice)
- [Fun-Audio-Chat](https://github.com/FunAudioLLM/Fun-Audio-Chat)
