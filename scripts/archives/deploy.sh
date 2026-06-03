#!/bin/bash
# 服务器部署脚本
# 用法：在服务器上运行 ./deploy.sh

set -e

echo "========================================"
echo "  DiTX-Clerk 服务器部署脚本"
echo "========================================"

# 1. 检查 Python 环境
echo ""
echo "[1/6] 检查 Python 环境..."

# 优先使用 conda base 环境，如果没有则使用 3D-Speaker 环境
if command -v python3 &> /dev/null; then
    PYTHON="python3"
elif [ -f "/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3" ]; then
    PYTHON="/home/ditx/llm/voice/3D-Speaker/.3ds/bin/python3"
else
    echo "错误：找不到 Python"
    exit 1
fi

$PYTHON --version
echo "Python 检查通过"

# 2. 安装项目依赖
echo ""
echo "[2/6] 安装项目依赖..."
$PYTHON -m pip install -r requirements.txt --quiet
echo "依赖安装完成"

# 3. 验证模块导入
echo ""
echo "[3/6] 验证模块导入..."
$PYTHON -c "
from audiochat.rag.retriever import AudioChatRetriever
from audiochat.rag.storage import MeetingMemoryStore
from evaluation.grpo_eval import GRPOEvaluator
print('所有核心模块导入成功！')
"

# 4. 检查模型路径
echo ""
echo "[4/6] 检查模型路径..."
MODELS=(
    "/data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    "/data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B"
    "/data/models/Voice/iic/speech_campplus_sv_zh-cn_16k-common"
)

for model in "${MODELS[@]}"; do
    if [ ! -d "$model" ]; then
        echo "⚠️  警告：模型路径不存在：$model"
    else
        echo "  ✓ $model"
    fi
done

# 5. 创建输出目录
echo ""
echo "[5/6] 创建输出目录..."
mkdir -p data
mkdir -p saves
mkdir -p rag_storage
mkdir -p output
echo "目录创建完成"

# 6. 快速测试
echo ""
echo "[6/6] 运行快速测试..."
$PYTHON -c "
from audiochat.rag.storage import MeetingMemoryStore
store = MeetingMemoryStore(persist_dir='./rag_storage')
stats = store.get_stats()
print(f'RAG 存储初始化成功：{stats}')
"

echo ""
echo "========================================"
echo "  ✅ 部署完成！"
echo "========================================"
echo ""
echo "📁 项目结构:"
echo "  /data/DiTX-Clerk/"
echo "  ├── train_sft.py          # SFT 训练"
echo "  ├── train_grpo.py         # GRPO 训练"
echo "  ├── setup_rag_db.py       # RAG 数据库搭建"
echo "  ├── prepare_data.py       # 数据准备"
echo "  ├── scripts/              # Pipeline 脚本"
echo "  └── audiochat/            # 核心模块"
echo ""
echo "🚀 使用指南:"
echo ""
echo "  # 1. 生成训练数据"
echo "  $PYTHON prepare_data.py --output_dir data --num_sft 1000 --num_grpo 1000"
echo ""
echo "  # 2. 搭建 RAG 数据库"
echo "  $PYTHON setup_rag_db.py --storage_dir ./rag_storage --data_dir data/meeting_records"
echo ""
echo "  # 3. SFT 训练（8 卡并行）"
echo "  torchrun --nproc_per_node=8 train_sft.py \\"
echo "    --model_name_or_path /data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B \\"
echo "    --train_data data/sft_train_data.jsonl \\"
echo "    --output_dir saves/sft_model \\"
echo "    --num_train_epochs 3"
echo ""
echo "  # 4. GRPO 训练（4 卡并行）"
echo "  torchrun --nproc_per_node=4 train_grpo.py \\"
echo "    --model_name_or_path saves/sft_model \\"
echo "    --train_data data/grpo_train_data.jsonl \\"
echo "    --output_dir saves/grpo_model \\"
echo "    --num_train_epochs 3"
echo ""
echo "  # 5. Pipeline 测试"
echo "  $PYTHON scripts/offline_pipeline.py --audio examples/test.wav --output-dir output"
echo ""
echo "========================================"
