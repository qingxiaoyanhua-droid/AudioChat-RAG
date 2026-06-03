#!/bin/bash
# AudioChat 服务器部署脚本
# 用法：./deploy_server.sh

set -e

echo "========================================"
echo "  AudioChat 服务器部署脚本"
echo "========================================"

# 1. 检查 Python 环境
PYTHON="/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3"

echo "[1/5] 检查 Python 环境..."
if [ ! -f "$PYTHON" ]; then
    echo "错误：Python 不存在：$PYTHON"
    exit 1
fi
$PYTHON --version

# 2. 安装 RAG 依赖
echo "[2/5] 安装 RAG 依赖..."
$PYTHON -m pip install -r requirements_rag.txt --quiet

# 3. 验证导入
echo "[3/5] 验证模块导入..."
$PYTHON -c "
from audiochat.rag.retriever import AudioChatRetriever
from audiochat.rag.storage import MeetingMemoryStore
from evaluation.grpo_eval import GRPOEvaluator
print('所有模块导入成功！')
"

# 4. 检查模型路径
echo "[4/5] 检查模型路径..."
MODELS=(
    "/data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    "/data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B"
    "/data/models/Voice/iic/speech_campplus_sv_zh-cn_16k-common"
)

for model in "${MODELS[@]}"; do
    if [ ! -d "$model" ]; then
        echo "警告：模型路径不存在：$model"
    else
        echo "  ✓ $model"
    fi
done

# 5. 运行快速测试
echo "[5/5] 运行快速测试..."
$PYTHON -c "
from audiochat.rag.storage import MeetingMemoryStore
store = MeetingMemoryStore(persist_dir='./test_rag_storage')
stats = store.get_stats()
print(f'RAG 存储初始化成功：{stats}')
"

echo ""
echo "========================================"
echo "  部署完成！"
echo "========================================"
echo ""
echo "运行示例:"
echo "  # 基础模式（文本输出）"
echo "  $PYTHON scripts/offline_pipeline.py --audio examples/2speakers_example.wav"
echo ""
echo "  # RAG 增强模式"
echo "  $PYTHON scripts/offline_pipeline.py --audio examples/2speakers_example.wav --enable-rag --meeting-id test_001"
echo ""
echo "  # 语音输出模式"
echo "  $PYTHON scripts/offline_pipeline_s2s.py --audio examples/2speakers_example.wav"
echo ""
