#!/bin/bash
# AudioChat-RAG 服务器部署脚本（适配 Fun-Audio-Chat 环境）
# 用法：./deploy_funaudiochat.sh

set -e

echo "========================================"
echo "  AudioChat-RAG 部署脚本"
echo "  适配 Fun-Audio-Chat 环境"
echo "========================================"

# 1. 激活 Fun-Audio-Chat 虚拟环境
echo "[1/6] 激活 Fun-Audio-Chat 虚拟环境..."
source ~/llm/voice/Fun-Audio-Chat/.AudioChat/bin/activate

# 检查 Python 版本
python --version

# 2. 切换到项目目录
PROJECT_DIR="/home/ditx/projects/DiTX-Clerk"
echo "[2/6] 切换到项目目录：$PROJECT_DIR"
cd "$PROJECT_DIR"

# 3. 安装 RAG 依赖
echo "[3/6] 安装 RAG 依赖..."
pip install chromadb sentence-transformers scikit-learn --quiet

# 4. 验证模块导入
echo "[4/6] 验证模块导入..."
python -c "
from audiochat.rag.retriever import AudioChatRetriever
from audiochat.rag.storage import MeetingMemoryStore
from evaluation.grpo_eval import GRPOEvaluator
print('✅ 所有模块导入成功！')
"

# 5. 检查模型路径
echo "[5/6] 检查模型路径..."

# Fun-Audio-Chat 模型路径
FUNAUDIOCHAT_MODEL="$HOME/llm/voice/Fun-Audio-Chat/pretrained_models/Fun-Audio-Chat-8B"
COSYVOICE_MODEL="$HOME/llm/voice/Fun-Audio-Chat/pretrained_models/Fun-CosyVoice3-0.5B-2512"

echo "  Fun-Audio-Chat: $FUNAUDIOCHAT_MODEL"
if [ ! -d "$FUNAUDIOCHAT_MODEL" ]; then
    echo "  ⚠️  警告：Fun-Audio-Chat 模型不存在"
else
    echo "  ✅ 存在"
fi

echo "  CosyVoice: $COSYVOICE_MODEL"
if [ ! -d "$COSYVOICE_MODEL" ]; then
    echo "  ⚠️  警告：CosyVoice 模型不存在"
else
    echo "  ✅ 存在"
fi

# 6. 运行快速测试
echo "[6/6] 运行快速测试..."

# 测试 RAG 存储初始化
python -c "
from audiochat.rag.storage import MeetingMemoryStore
store = MeetingMemoryStore(persist_dir='./test_rag_storage')
stats = store.get_stats()
print(f'✅ RAG 存储初始化成功：{stats}')
"

# 清理测试文件
rm -rf ./test_rag_storage

echo ""
echo "========================================"
echo "  ✅ 部署完成！"
echo "========================================"
echo ""
echo "运行示例:"
echo ""
echo "  # 1. 基础模式（文本输出）"
echo "  python scripts/offline_pipeline.py \\"
echo "    --audio examples/2speakers_example.wav \\"
echo "    --funaudiochat-model $FUNAUDIOCHAT_MODEL \\"
echo "    --output-dir output_test"
echo ""
echo "  # 2. RAG 增强模式"
echo "  python scripts/offline_pipeline.py \\"
echo "    --audio examples/2speakers_example.wav \\"
echo "    --enable-rag \\"
echo "    --meeting-id demo_001 \\"
echo "    --funaudiochat-model $FUNAUDIOCHAT_MODEL \\"
echo "    --output-dir output_rag"
echo ""
echo "  # 3. 语音输出模式 (S2S)"
echo "  python scripts/offline_pipeline_s2s.py \\"
echo "    --audio examples/2speakers_example.wav \\"
echo "    --funaudiochat-model $FUNAUDIOCHAT_MODEL \\"
echo "    --output-dir output_s2s"
echo ""
echo "  # 4. GRPO 评估"
echo "  python evaluation/grpo_eval.py \\"
echo "    --audio output_rag/output_audio_xxx.wav \\"
echo "    --reference '参考文本' \\"
echo "    --generated '生成的文本' \\"
echo "    --output eval_report.json"
echo ""
echo "========================================"
