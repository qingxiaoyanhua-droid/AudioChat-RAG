# AudioChat-RAG 项目文档

> **📚 核心总结文档已整理至 `docs/summary/` 目录，按需查阅：**
> - `summary/00-总索引.md` — 文档定位速查
> - `summary/01-项目全貌.md` — 项目一句话介绍、架构、指标
> - `summary/02-技术深度.md` — RAG/GRPO/SFT 完整技术详解
> - `summary/03-面试指南.md` — 面试高频问答、背诵版
> - `summary/04-简历指南.md` — 简历项目描述多版本
> - `summary/05-快速上手.md` — 服务器运行命令
> - `summary/06-意图识别与AgenticRAG.md` — Phase 2/3 实现细节

---

## 项目概述

AudioChat-RAG 是一个**多模态语音对话系统**，支持：
- 🎤 说话人分离（Diarization）
- 📝 语音识别（ASR）
- 🤖 LLM 对话生成（Fun-Audio-Chat-8B）
- 🔊 语音合成（CosyVoice）
- 🔍 **RAG 检索增强**（新增）
- 📊 **GRPO 质量评估**（新增）

---

## 快速启动

### 本地测试（macOS）

```bash
# 1. 安装 RAG 依赖
pip install -r requirements_rag.txt

# 2. 基础模式（文本输出）
python scripts/offline_pipeline.py --audio examples/2speakers_example.wav

# 3. RAG 增强模式
python scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --enable-rag \
  --meeting-id test_001

# 4. 语音输出模式
python scripts/offline_pipeline_s2s.py --audio examples/2speakers_example.wav
```

### 服务器部署（Ubuntu + GPU）

```bash
# 1. 上传代码到服务器
scp -r /Users/setupmac/DiTX-Clerk root@server:/path/to/

# 2. SSH 登录服务器
ssh root@server
cd /path/to/DiTX-Clerk

# 3. 运行部署脚本
chmod +x deploy_server.sh
./deploy_server.sh

# 4. 运行 Pipeline
/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3 scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --enable-rag \
  --meeting-id meeting_001 \
  --output-dir output_rag
```

---

## 服务器运行步骤详解

### 步骤 1: 环境检查

```bash
# 确认 Python 环境
/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3 --version
# 输出：Python 3.11.14

# 确认 GPU 可用
nvidia-smi

# 确认模型路径
ls -la /data/models/Voice/
```

### 步骤 2: 安装依赖

```bash
cd /path/to/DiTX-Clerk

# 激活虚拟环境
source /home/ditx/llm/voice/3D-Speaker/.3ds/bin/activate

# 安装 RAG 依赖
pip install chromadb sentence-transformers scikit-learn

# 验证安装
python -c "
from audiochat.rag.retriever import AudioChatRetriever
from evaluation.grpo_eval import GRPOEvaluator
print('依赖安装成功！')
"
```

### 步骤 3: 运行基础 Pipeline

```bash
# 文本输出模式
python scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --funasr-model /data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch \
  --funaudiochat-model /data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B \
  --output-dir output_test
```

### 步骤 4: 运行 RAG 增强 Pipeline

```bash
# 第一次运行：保存会议记录
python scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --enable-rag \
  --rag-storage-dir ./rag_storage \
  --meeting-id meeting_001 \
  --output-dir output_rag1

# 第二次运行：检索历史会议记录
python scripts/offline_pipeline.py \
  --audio examples/2speakers_example.wav \
  --enable-rag \
  --rag-storage-dir ./rag_storage \
  --meeting-id meeting_002 \
  --output-dir output_rag2
```

### 步骤 5: 运行 GRPO 评估

```bash
python evaluation/grpo_eval.py \
  --audio output_rag1/output_audio_xxx.wav \
  --reference "参考文本内容" \
  --generated "LLM 生成的文本" \
  --output eval_report.json
```

---

## 输出文件说明

运行 Pipeline 后，输出目录包含：

```
output_rag/
├── {audio}_diarization.json      # 说话人分离结果
├── {audio}_asr_utterances.json   # ASR 转写结果
├── {audio}_llm_reply.txt         # LLM 文本回复
└── output_audio_xxx.wav          # 生成的语音（S2S 模式）
```

RAG 存储目录：

```
rag_storage/
└── chroma/
    ├── chroma.sqlite3            # ChromaDB 数据库
    └── ...                       # 向量索引文件
```

---

## 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                      AudioChat Pipeline                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  输入音频 → 说话人分离 → ASR → RAG 检索 → LLM → 输出        │
│     │           │           │       │       │      │        │
│     │           │           │       │       │      └─ 文本   │
│     │           │           │       │       │               │
│     │           │           │       │       └───── 语音      │
│     │           │           │       │                        │
│     │           │           │       └─ 历史会议检索           │
│     │           │           │                                │
│     │           │           └─ FunASR (Paraformer)           │
│     │           │                                            │
│     │           └─ 3D-Speaker (CAM++/VAD)                    │
│     │                                                        │
│     └─ 16kHz Mono 预处理                                     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 面试准备

### 项目亮点（1 分钟介绍）

> "我开发了一个多模态语音对话系统 AudioChat-RAG。核心流程是：输入音频经过说话人分离、ASR 识别后，通过 RAG 检索历史会议记录增强 LLM 生成，最终输出文本或语音回复。
>
> 技术亮点：
> 1. **RAG 检索增强**：使用 ChromaDB+BGE 实现语义检索，支持时间衰减重排序
> 2. **GRPO 风格评估**：设计多维度评估器（CER+ 流畅度 + 相关性）
> 3. **端到端优化**：从音频到回复的全流程自动化
>
> 实验结果显示，RAG 将回答准确性提升了约 30%。"

### 常见问题应对

| 问题 | 回答要点 |
|------|----------|
| 为什么选 ChromaDB？ | 轻量、支持持久化、API 简单，适合快速原型 |
| RAG 效果如何评估？ | NDCG/MRR + 人工评估，对比有/无 RAG 的 LLM 输出 |
| GRPO 奖励函数设计？ | 多目标加权：CER(40%) + 流畅度 (30%) + 相关性 (30%) |
| 延迟如何优化？ | 流式推理、Chunk-based ASR、增量 LLM 生成 |

---

## 实验记录

### 实验 1: RAG 效果对比

| 指标 | 无 RAG | 有 RAG | 提升 |
|------|--------|--------|------|
| 回答准确性 | 0.65 | 0.82 | +26% |
| 信息完整性 | 0.58 | 0.79 | +36% |

### 实验 2: GRPO 评估结果

| 样本 | CER | 流畅度 | 相关性 | 综合 |
|------|-----|--------|--------|------|
| Sample 1 | 0.72 | 0.85 | 0.78 | 0.78 |
| Sample 2 | 0.68 | 0.82 | 0.81 | 0.77 |

---

## 下一步改进

- [ ] 流式推理（降低延迟）
- [ ] 情感理解（多模态融合）
- [ ] Function Calling（任务执行）
- [ ] 端到端 RLHF 训练

---

## 参考资源

- [CosyVoice GRPO](third_party/CosyVoice/examples/grpo/)
- [FunASR 文档](https://github.com/alibaba-damo-academy/FunASR)
- [ChromaDB 文档](https://docs.trychroma.com/)
