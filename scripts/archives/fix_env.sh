#!/bin/bash
# 服务器环境快速修复脚本

set -e

echo "========================================"
echo "  环境修复脚本"
echo "========================================"

# 1. 修复 NumPy 版本
echo ""
echo "[1/4] 修复 NumPy 版本..."
pip install "numpy>=1.24.0,<2.0.0" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 修复 transformers 版本
echo ""
echo "[2/4] 修复 transformers 版本..."
pip install "transformers>=4.35.0,<4.45.0" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 修复 peft 版本
echo ""
echo "[3/4] 修复 peft 版本..."
pip install "peft>=0.6.0,<0.12.0" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 4. 验证安装
echo ""
echo "[4/4] 验证安装..."
python3 -c "
import numpy as np
import torch
import transformers
import peft
print(f'NumPy: {np.__version__}')
print(f'PyTorch: {torch.__version__}')
print(f'Transformers: {transformers.__version__}')
print(f'PEFT: {peft.__version__}')
print('✅ 所有依赖版本正确！')
"

echo ""
echo "========================================"
echo "  修复完成！"
echo "========================================"
echo ""
echo "现在可以运行:"
echo "  python3 prepare_data.py --output_dir data --num_sft 10 --num_grpo 10"
echo ""
