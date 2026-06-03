#!/bin/bash
# 本地快速启动脚本（简化版，无需 GPU）
# 用途：快速验证 RAG 和 Pipeline 功能

set -e

echo "========================================"
echo "  AudioChat-RAG 本地快速启动"
echo "========================================"

# 1. 检查 Python 环境
echo "[1/5] 检查 Python 环境..."
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo "错误：未找到 Python"
    exit 1
fi

$PYTHON --version

# 2. 创建虚拟环境（如果不存在）
echo "[2/5] 检查虚拟环境..."
if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    $PYTHON -m venv .venv
fi

# 激活虚拟环境
source .venv/bin/activate

# 3. 安装依赖
echo "[3/5] 安装依赖..."
pip install --upgrade pip --quiet
pip install torch torchaudio --quiet
pip install transformers soundfile --quiet
pip install chromadb sentence-transformers scikit-learn --quiet
pip install funasr modelscope --quiet

echo "✅ 依赖安装完成"

# 4. 下载 FunASR 模型（小模型，仅需 2GB）
echo "[4/5] 下载 FunASR 模型..."
if [ ! -d "models/funasr" ]; then
    echo "下载中...（约 2GB，可能需要几分钟）"
    modelscope download --model iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch --local_dir ./models/funasr
else
    echo "✅ 模型已存在"
fi

# 5. 运行测试
echo "[5/5] 运行测试..."

# 测试 RAG 模块
echo "测试 RAG 模块..."
python -c "
from audiochat.rag.storage import MeetingMemoryStore
from audiochat.rag.retriever import AudioChatRetriever

# 初始化存储
store = MeetingMemoryStore(persist_dir='./test_rag_storage')

# 添加测试数据
from audiochat.rag.storage import MeetingDocument
docs = [
    MeetingDocument(content='张三：项目进度 80%，预计下周完成', meeting_id='test_001', speaker='张三'),
    MeetingDocument(content='李四：前端已完成，等待后端 API', meeting_id='test_001', speaker='李四'),
]
store.add_batch(docs)

# 测试检索
from audiochat.rag.retriever import AudioChatRetriever
retriever = AudioChatRetriever(store)
results = retriever.retrieve('项目进度如何？', k=2)

print('✅ RAG 测试成功！')
print(f'检索到 {len(results)} 条结果:')
for r in results:
    print(f'  - {r.content[:50]}... (分数：{r.relevance_score:.3f})')
"

# 清理测试文件
rm -rf ./test_rag_storage

echo ""
echo "========================================"
echo "  ✅ 本地环境准备完成！"
echo "========================================"
echo ""
echo "运行示例:"
echo ""
echo "  # 激活虚拟环境"
echo "  source .venv/bin/activate"
echo ""
echo "  # 运行 Pipeline（需要 GPU 和完整模型）"
echo "  python scripts/offline_pipeline.py \\"
echo "    --audio examples/2speakers_example.wav \\"
echo "    --enable-rag \\"
echo "    --meeting-id test_001 \\"
echo "    --output-dir output_test"
echo ""
echo "  # 仅测试 RAG 功能（无需 GPU）"
echo "  python -c \"from audiochat.rag.retriever import AudioChatRetriever; print('OK')\""
echo ""
echo "========================================"
echo ""
echo "注意：完整 Pipeline 需要 GPU 和 Fun-Audio-Chat 模型"
echo "      详见：docs/本地运行指南.md"
echo ""
