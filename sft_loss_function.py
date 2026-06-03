"""
SFT 交叉熵损失函数 - 完整可运行版

用途：SFT 监督微调中，计算模型预测和真实标签的损失
核心思想：CrossEntropyLoss = -log(p(correct_token))

使用示例：
    loss_fn = SFTLossFunction(vocab_size=32000)
    loss = loss_fn.compute_loss(logits, labels)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple


# ============================================================
# SFT 损失函数类
# ============================================================

class SFTLossFunction:
    """
    SFT 交叉熵损失函数
    
    作用：计算模型预测和真实标签的差异，指导模型学习
    
    输入：
        - logits: 模型输出 [batch_size, seq_len, vocab_size]
        - labels: 真实标签 [batch_size, seq_len]
    
    输出：
        - loss: 标量，越小越好
    
    公式：
        Loss = -Σ y_true × log(y_pred)
        
        语言模型中简化为：
        Loss = -log(p(next_token | context))
    """
    
    def __init__(self, 
                 vocab_size: int = 32000,
                 ignore_index: int = -100,
                 label_smoothing: float = 0.0):
        """
        初始化损失函数
        
        参数：
            vocab_size: 词表大小
            ignore_index: 忽略的标签索引（用于 padding）
            label_smoothing: 标签平滑（防止过拟合）
        """
        print("初始化 SFT 损失函数...")
        
        # 1. 交叉熵损失
        # CrossEntropyLoss 内部做了三件事：
        #   1. softmax 归一化
        #   2. log 取对数
        #   3. nll_loss 负对数似然
        self.cross_entropy_loss = nn.CrossEntropyLoss(
            ignore_index=ignore_index,      # 忽略 padding 位置
            label_smoothing=label_smoothing # 标签平滑
        )
        
        # 2. 保存参数
        self.vocab_size = vocab_size
        self.ignore_index = ignore_index
        self.label_smoothing = label_smoothing
        
        print("✅ SFT 损失函数初始化完成")
        print("   词表大小：{}".format(vocab_size))
        print("   忽略索引：{}".format(ignore_index))
        print("   标签平滑：{}".format(label_smoothing))
    
    def compute_loss(self, 
                     logits: torch.Tensor, 
                     labels: torch.Tensor) -> torch.Tensor:
        """
        计算交叉熵损失（主函数）
        
        参数：
            logits: 模型输出 [batch_size, seq_len, vocab_size]
                   每个位置对下一个 token 的预测分数
            labels: 真实标签 [batch_size, seq_len]
                   每个位置的真实下一个 token ID
        
        返回：
            loss: 标量，越小越好
        
        形状变换：
            输入 logits: [batch_size, seq_len, vocab_size]
            输入 labels: [batch_size, seq_len]
            
            调整后 logits: [batch_size × seq_len, vocab_size]
            调整后 labels: [batch_size × seq_len]
        """
        
        # ========== 步骤 1: 调整形状 ==========
        # 把 batch 和 seq_len 维度合并
        # 例如：batch=2, seq_len=100 → batch×seq_len=200
        
        batch_size, seq_len, vocab_size = logits.shape
        
        # logits: [batch, seq_len, vocab] → [batch×seq_len, vocab]
        logits = logits.view(-1, vocab_size)
        
        # labels: [batch, seq_len] → [batch×seq_len]
        labels = labels.view(-1)
        
        # ========== 步骤 2: 计算交叉熵损失 ==========
        # CrossEntropyLoss 内部计算：
        #   1. softmax: 把 logits 归一化到 0-1
        #   2. log: 取对数
        #   3. nll_loss: 负对数似然损失
        
        loss = self.cross_entropy_loss(logits, labels)
        
        return loss
    
    def compute_perplexity(self, 
                          logits: torch.Tensor, 
                          labels: torch.Tensor) -> float:
        """
        计算困惑度（Perplexity）
        
        作用：衡量模型预测的不确定性，越低越好
        
        公式：
            Perplexity = exp(Loss)
        
        参数：
            logits: 模型输出
            labels: 真实标签
        
        返回：
            perplexity: 困惑度分数
        """
        
        # 1. 计算损失
        loss = self.compute_loss(logits, labels)
        
        # 2. 计算困惑度
        # exp(loss) 把负对数似然转换回概率空间
        perplexity = torch.exp(loss).item()
        
        return perplexity
    
    def compute_accuracy(self, 
                        logits: torch.Tensor, 
                        labels: torch.Tensor) -> float:
        """
        计算准确率
        
        作用：衡量模型预测正确的比例
        
        公式：
            Accuracy = correct_predictions / total_predictions
        
        参数：
            logits: 模型输出
            labels: 真实标签
        
        返回：
            accuracy: 准确率分数
        """
        
        # 1. 获取预测结果
        # argmax: 取概率最大的 token ID
        predictions = torch.argmax(logits, dim=-1)
        
        # 2. 调整形状
        predictions = predictions.view(-1)
        labels = labels.view(-1)
        
        # 3. 计算正确数量（忽略 ignore_index）
        mask = labels != self.ignore_index
        correct = (predictions == labels) & mask
        total = mask.sum()
        
        # 4. 计算准确率
        if total > 0:
            accuracy = correct.sum().item() / total.item()
        else:
            accuracy = 0.0
        
        return accuracy


# ============================================================
# 交叉熵损失详解（教学用）
# ============================================================

class CrossEntropyLossExplained:
    """
    交叉熵损失详解（教学用，实际训练用上面的 SFTLossFunction）
    
    目的：展示交叉熵损失的内部计算过程
    """
    
    def __init__(self):
        pass
    
    def softmax(self, logits: torch.Tensor) -> torch.Tensor:
        """
        Softmax 函数
        
        作用：把 logits 归一化到 0-1，表示概率
        
        公式：
            softmax(x_i) = exp(x_i) / Σ exp(x_j)
        
        参数：
            logits: [batch, vocab] 未归一化的分数
        
        返回：
            probabilities: [batch, vocab] 概率分布
        """
        # 减去最大值，防止数值溢出
        max_logits = torch.max(logits, dim=-1, keepdim=True)[0]
        exp_logits = torch.exp(logits - max_logits)
        sum_exp_logits = torch.sum(exp_logits, dim=-1, keepdim=True)
        probabilities = exp_logits / sum_exp_logits
        return probabilities
    
    def log_loss(self, 
                 probabilities: torch.Tensor, 
                 labels: torch.Tensor) -> torch.Tensor:
        """
        对数损失
        
        作用：计算预测概率和真实标签的差异
        
        公式：
            loss = -log(p(correct_token))
        
        参数：
            probabilities: [batch, vocab] 概率分布
            labels: [batch] 真实标签 ID
        
        返回：
            loss: 标量
        """
        batch_size = probabilities.shape[0]
        
        # 取出正确 token 的概率
        # gather: 按照 labels 索引取出对应概率
        correct_probs = probabilities.gather(1, labels.unsqueeze(1)).squeeze(1)
        
        # 取对数
        log_probs = torch.log(correct_probs)
        
        # 取平均
        loss = -torch.mean(log_probs)
        
        return loss
    
    def compute_loss(self, 
                     logits: torch.Tensor, 
                     labels: torch.Tensor) -> torch.Tensor:
        """
        完整的交叉熵损失计算
        
        步骤：
        1. softmax 归一化
        2. log 取对数
        3. nll_loss 负对数似然
        
        参数：
            logits: [batch, vocab] 未归一化的分数
            labels: [batch] 真实标签 ID
        
        返回：
            loss: 标量
        """
        # 步骤 1: softmax
        probabilities = self.softmax(logits)
        
        # 步骤 2: log loss
        loss = self.log_loss(probabilities, labels)
        
        return loss


# ============================================================
# 测试代码
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("SFT 交叉熵损失函数测试")
    print("=" * 60)
    
    # 创建损失函数实例
    loss_fn = SFTLossFunction(
        vocab_size=1000,      # 假设词表大小 1000
        ignore_index=-100,    # 忽略 padding
        label_smoothing=0.0   # 不用标签平滑
    )
    
    # 创建测试数据
    batch_size = 2
    seq_len = 5
    vocab_size = 1000
    
    # logits: [batch, seq_len, vocab]
    # 每个位置对下一个 token 的预测分数
    logits = torch.randn(batch_size, seq_len, vocab_size)
    
    # labels: [batch, seq_len]
    # 每个位置的真实下一个 token ID
    labels = torch.randint(0, vocab_size, (batch_size, seq_len))
    
    # 设置一些 padding（用 ignore_index 标记）
    labels[0, 3:] = -100  # 第一个样本的后两个位置是 padding
    labels[1, 4:] = -100  # 第二个样本的最后一个位置是 padding
    
    print("\n输入形状:")
    print("  logits: {}".format(logits.shape))
    print("  labels: {}".format(labels.shape))
    
    # ========== 测试 1: 计算损失 ==========
    print("\n" + "-" * 60)
    print("测试 1: 计算交叉熵损失")
    print("-" * 60)
    
    loss = loss_fn.compute_loss(logits, labels)
    print("损失值：{:.4f}".format(loss.item()))
    
    # ========== 测试 2: 计算困惑度 ==========
    print("\n" + "-" * 60)
    print("测试 2: 计算困惑度")
    print("-" * 60)
    
    perplexity = loss_fn.compute_perplexity(logits, labels)
    print("困惑度：{:.4f}".format(perplexity))
    print("解释：困惑度越低，模型预测越准确")
    
    # ========== 测试 3: 计算准确率 ==========
    print("\n" + "-" * 60)
    print("测试 3: 计算准确率")
    print("-" * 60)
    
    accuracy = loss_fn.compute_accuracy(logits, labels)
    print("准确率：{:.4f}".format(accuracy))
    print("解释：准确率越高，模型预测越准确")
    
    # ========== 测试 4: 交叉熵损失详解 ==========
    print("\n" + "-" * 60)
    print("测试 4: 交叉熵损失详解（教学）")
    print("-" * 60)
    
    explained_loss_fn = CrossEntropyLossExplained()
    
    # 简化测试（单个样本，单个位置）
    single_logits = torch.randn(1, vocab_size)  # 单个样本
    single_labels = torch.randint(0, vocab_size, (1,))  # 单个标签
    
    # 方法 1: 使用详解版本
    explained_loss = explained_loss_fn.compute_loss(single_logits, single_labels)
    
    # 方法 2: 使用标准版本
    standard_loss = loss_fn.compute_loss(single_logits.unsqueeze(0), single_labels.unsqueeze(0))
    
    print("详解版本损失：{:.4f}".format(explained_loss.item()))
    print("标准版本损失：{:.4f}".format(standard_loss.item()))
    print("两者应该接近")
    
    # ========== 测试 5: 不同预测质量的损失对比 ==========
    print("\n" + "-" * 60)
    print("测试 5: 不同预测质量的损失对比")
    print("-" * 60)
    
    # 情况 1: 完美预测（正确 token 概率 0.9）
    perfect_logits = torch.zeros(1, vocab_size)
    perfect_logits[0, 100] = 10.0  # 让第 100 个 token 概率很高
    perfect_labels = torch.tensor([100])
    perfect_loss = loss_fn.compute_loss(perfect_logits.unsqueeze(0), perfect_labels.unsqueeze(0))
    
    # 情况 2: 随机预测（所有 token 概率相等）
    random_logits = torch.zeros(1, vocab_size)
    random_labels = torch.tensor([100])
    random_loss = loss_fn.compute_loss(random_logits.unsqueeze(0), random_labels.unsqueeze(0))
    
    # 情况 3: 错误预测（正确 token 概率很低）
    wrong_logits = torch.zeros(1, vocab_size)
    wrong_logits[0, 200] = 10.0  # 让第 200 个 token 概率很高（但正确答案是 100）
    wrong_labels = torch.tensor([100])
    wrong_loss = loss_fn.compute_loss(wrong_logits.unsqueeze(0), wrong_labels.unsqueeze(0))
    
    print("完美预测损失：{:.4f} (应该最小)".format(perfect_loss.item()))
    print("随机预测损失：{:.4f}".format(random_loss.item()))
    print("错误预测损失：{:.4f} (应该最大)".format(wrong_loss.item()))
    
    print("\n" + "=" * 60)
    print("✅ 测试完成！")
    print("=" * 60)
