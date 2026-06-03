"""
训练曲线绘制脚本
从训练日志中提取 loss 和 reward，绘制曲线图
"""

import re
import json
import matplotlib.pyplot as plt
from pathlib import Path
from datetime import datetime


def _smooth(values: list, window: int = 5) -> dict:
    """移动平均平滑"""
    smoothed = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        smoothed.append(sum(values[start:i + 1]) / (i - start + 1))
    return {'steps': list(range(1, len(smoothed) + 1)), 'values': smoothed}


def parse_sft_log(log_path: str) -> dict:
    """解析 SFT 训练日志"""
    steps = []
    losses = []
    
    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            # 匹配格式：{'loss': 2.345, 'learning_rate': 1e-5, 'epoch': 1.5}
            match = re.search(r"\{'loss': ([\d.]+),.*'epoch': ([\d.]+)\}", line)
            if match:
                loss = float(match.group(1))
                epoch = float(match.group(2))
                steps.append(len(steps) + 1)
                losses.append(loss)
    
    return {'steps': steps, 'losses': losses}


def parse_grpo_log(log_path: str) -> dict:
    """解析 GRPO 训练日志"""
    steps = []
    losses = []
    rewards = []
    kls = []
    clip_fractions = []

    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            # 匹配格式：loss=0.234, reward=0.567, kl=0.023
            loss_match = re.search(r"loss=([\d.]+)", line)
            reward_match = re.search(r"reward=([\d.]+)", line)
            kl_match = re.search(r"kl=([\d.]+)", line)

            if loss_match and reward_match:
                loss = float(loss_match.group(1))
                reward = float(reward_match.group(1))
                kl = float(kl_match.group(1)) if kl_match else None
                steps.append(len(steps) + 1)
                losses.append(loss)
                rewards.append(reward)
                kls.append(kl if kl is not None else 0.0)

            # 也从 "clip_fractions" 关键字解析
            clip_match = re.search(r"clip_fraction=([\d.]+)", line)
            if clip_match:
                # 找到对应的 step，追加 clip_fraction
                if len(clip_fractions) == len(steps) - 1:
                    clip_fractions.append(float(clip_match.group(1)))
                elif len(clip_fractions) < len(steps):
                    clip_fractions.append(float(clip_match.group(1)))

    return {'steps': steps, 'losses': losses, 'rewards': rewards,
            'kls': kls, 'clip_fractions': clip_fractions}


def plot_sft_curves(sft_data: dict, output_dir: str = 'saves/figures'):
    """绘制 SFT 训练曲线"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    plt.figure(figsize=(12, 5))
    
    # Loss 曲线
    plt.subplot(1, 2, 1)
    plt.plot(sft_data['steps'], sft_data['losses'], 'b-', linewidth=2, label='Training Loss')
    plt.xlabel('Step')
    plt.ylabel('Loss')
    plt.title('SFT Training Loss')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    # Loss 平滑曲线
    if len(sft_data['losses']) > 10:
        plt.subplot(1, 2, 2)
        window = 10
        smoothed = [sum(sft_data['losses'][i:i+window])/window 
                   for i in range(0, len(sft_data['losses'])-window, window)]
        plt.plot(range(1, len(smoothed)+1), smoothed, 'r-', linewidth=2, label='Smoothed Loss')
        plt.xlabel('Step (windowed)')
        plt.ylabel('Smoothed Loss')
        plt.title('SFT Training Loss (Smoothed)')
        plt.legend()
        plt.grid(True, alpha=0.3)
    
    plt.tight_layout()
    output_path = f'{output_dir}/sft_training_curve_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f'SFT 曲线已保存：{output_path}')
    plt.close()


def plot_grpo_curves(grpo_data: dict, output_dir: str = 'saves/figures'):
    """绘制 GRPO 训练曲线（4 子图）"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # 1. Reward 曲线
    axes[0, 0].plot(grpo_data['steps'], grpo_data['rewards'], 'g-',
                    linewidth=1.5, alpha=0.7, label='Reward')
    if len(grpo_data['rewards']) > 5:
        smoothed = _smooth(grpo_data['rewards'])
        axes[0, 0].plot(smoothed['steps'], smoothed['values'], 'g--',
                        linewidth=2, label='Smoothed Reward')
    axes[0, 0].set_xlabel('Step')
    axes[0, 0].set_ylabel('Reward')
    axes[0, 0].set_title('GRPO Reward (判断收敛: 单调上升并趋于平稳)')
    axes[0, 0].legend()
    axes[0, 0].grid(True, alpha=0.3)

    # 2. Loss 曲线
    axes[0, 1].plot(grpo_data['steps'], grpo_data['losses'], 'r-',
                    linewidth=1.5, alpha=0.7, label='Loss')
    if len(grpo_data['losses']) > 5:
        smoothed = _smooth(grpo_data['losses'])
        axes[0, 1].plot(smoothed['steps'], smoothed['values'], 'r--',
                        linewidth=2, label='Smoothed Loss')
    axes[0, 1].set_xlabel('Step')
    axes[0, 1].set_ylabel('Loss')
    axes[0, 1].set_title('GRPO Policy Loss (判断收敛: 整体下降)')
    axes[0, 1].legend()
    axes[0, 1].grid(True, alpha=0.3)

    # 3. KL 散度曲线
    if grpo_data.get('kls') and any(v > 0 for v in grpo_data['kls']):
        axes[1, 0].plot(grpo_data['steps'], grpo_data['kls'], 'b-',
                        linewidth=1.5, alpha=0.7, label='KL(π_new||π_ref)')
        axes[1, 0].axhline(y=0.5, color='orange', linestyle='--',
                           linewidth=1.5, label='KL 警戒线 (0.5)')
        axes[1, 0].axhline(y=1.0, color='red', linestyle='--',
                           linewidth=1.5, label='KL 危险线 (1.0)')
        axes[1, 0].set_xlabel('Step')
        axes[1, 0].set_ylabel('KL Divergence')
        axes[1, 0].set_title('KL 散度 (判断稳定: 在 0.5~1.0 附近收敛)')
        axes[1, 0].legend()
        axes[1, 0].grid(True, alpha=0.3)

    # 4. Clip Fraction 曲线
    if grpo_data.get('clip_fractions'):
        axes[1, 1].plot(grpo_data['steps'][:len(grpo_data['clip_fractions'])],
                        grpo_data['clip_fractions'], 'purple',
                        linewidth=1.5, alpha=0.7, label='Clip Fraction')
        axes[1, 1].axhline(y=0.1, color='green', linestyle='--',
                           linewidth=1.5, label='理想下限 (10%)')
        axes[1, 1].axhline(y=0.2, color='orange', linestyle='--',
                           linewidth=1.5, label='警戒线 (20%)')
        axes[1, 1].set_xlabel('Step')
        axes[1, 1].set_ylabel('Clip Fraction')
        axes[1, 1].set_title('PPO Clip Fraction (判断稳定: 10%~20% 震荡)')
        axes[1, 1].legend()
        axes[1, 1].grid(True, alpha=0.3)
    else:
        axes[1, 1].text(0.5, 0.5,
                        'Clip Fraction 数据未记录\n\n提示: 在 trainer.stats 中添加 clip_fraction 统计',
                        ha='center', va='center', transform=axes[1, 1].transAxes,
                        fontsize=10, color='gray')
        axes[1, 1].set_title('PPO Clip Fraction (无数据)')

    plt.tight_layout()
    output_path = f'{output_dir}/grpo_training_curve_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f'GRPO 曲线已保存：{output_path}')
    plt.close()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='绘制训练曲线')
    parser.add_argument('--sft_log', type=str, default='logs/sft_train.log', help='SFT 日志路径')
    parser.add_argument('--grpo_log', type=str, default='logs/grpo_train.log', help='GRPO 日志路径')
    parser.add_argument('--output_dir', type=str, default='saves/figures', help='输出目录')
    
    args = parser.parse_args()
    
    print('=' * 60)
    print('训练曲线绘制')
    print('=' * 60)
    
    # 解析 SFT 日志
    if Path(args.sft_log).exists():
        print(f'\n解析 SFT 日志：{args.sft_log}')
        sft_data = parse_sft_log(args.sft_log)
        if sft_data['steps']:
            print(f'  找到 {len(sft_data["steps"])} 个训练步骤')
            print(f'  初始 Loss: {sft_data["losses"][0]:.4f}')
            print(f'  最终 Loss: {sft_data["losses"][-1]:.4f}')
            plot_sft_curves(sft_data, args.output_dir)
        else:
            print('  未找到有效的训练数据')
    else:
        print(f'SFT 日志不存在：{args.sft_log}')
    
    # 解析 GRPO 日志
    if Path(args.grpo_log).exists():
        print(f'\n解析 GRPO 日志：{args.grpo_log}')
        grpo_data = parse_grpo_log(args.grpo_log)
        if grpo_data['steps']:
            print(f'  找到 {len(grpo_data["steps"])} 个训练步骤')
            print(f'  初始 Reward: {grpo_data["rewards"][0]:.4f}')
            print(f'  最终 Reward: {grpo_data["rewards"][-1]:.4f}')
            if grpo_data.get('kls') and any(v > 0 for v in grpo_data['kls']):
                print(f'  初始 KL: {grpo_data["kls"][0]:.4f}')
                print(f'  最终 KL: {grpo_data["kls"][-1]:.4f}')
            if grpo_data.get('clip_fractions'):
                print(f'  Clip Fraction 均值: {sum(grpo_data["clip_fractions"])/len(grpo_data["clip_fractions"]):.2%}')
            plot_grpo_curves(grpo_data, args.output_dir)
        else:
            print('  未找到有效的训练数据')
    else:
        print(f'GRPO 日志不存在：{args.grpo_log}')
    
    print('\n' + '=' * 60)
    print('曲线绘制完成！')
    print('=' * 60)


if __name__ == '__main__':
    main()
