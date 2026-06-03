"""
训练监控脚本
记录 GPU 显存、损失值、学习率等指标，并绘制曲线
"""

import os
import re
import json
import time
import subprocess
import matplotlib.pyplot as plt
from datetime import datetime
from pathlib import Path


class TrainingMonitor:
    """训练监控器"""
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.metrics = {
            "steps": [],
            "loss": [],
            "learning_rate": [],
            "epoch": [],
            "gpu_memory": [],
            "gpu_util": [],
            "timestamp": []
        }
    
    def get_gpu_info(self):
        """获取 GPU 信息"""
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True
            )
            lines = result.stdout.strip().split("\n")
            gpu_info = []
            for line in lines:
                parts = line.split(", ")
                if len(parts) == 2:
                    gpu_info.append({
                        "memory": float(parts[0]),
                        "util": float(parts[1])
                    })
            return gpu_info
        except Exception as e:
            print(f"获取 GPU 信息失败：{e}")
            return []
    
    def parse_log_line(self, line: str) -> dict:
        """解析训练日志行"""
        # 匹配格式：{'loss': 2.345, 'learning_rate': 1e-5, 'epoch': 1.5}
        match = re.search(r"\{'loss': ([\d.]+),.*'learning_rate': ([\d.e-]+),.*'epoch': ([\d.]+)\}", line)
        if match:
            return {
                "loss": float(match.group(1)),
                "learning_rate": float(match.group(2)),
                "epoch": float(match.group(3))
            }
        return None
    
    def monitor_training(self, log_file: str, interval: int = 10):
        """
        实时监控训练
        
        Args:
            log_file: 训练日志文件路径
            interval: GPU 采样间隔（秒）
        """
        print(f"开始监控训练：{log_file}")
        print(f"GPU 采样间隔：{interval}秒")
        
        last_pos = 0
        
        while True:
            try:
                # 读取新的日志行
                with open(log_file, 'r', encoding='utf-8') as f:
                    f.seek(last_pos)
                    new_lines = f.readlines()
                    last_pos = f.tell()
                
                # 解析日志
                for line in new_lines:
                    metrics = self.parse_log_line(line)
                    if metrics:
                        self.metrics["loss"].append(metrics["loss"])
                        self.metrics["learning_rate"].append(metrics["learning_rate"])
                        self.metrics["epoch"].append(metrics["epoch"])
                        self.metrics["steps"].append(len(self.metrics["steps"]) + 1)
                        self.metrics["timestamp"].append(datetime.now().isoformat())
                        
                        print(f"Step {self.metrics['steps'][-1]}: "
                              f"loss={metrics['loss']:.4f}, "
                              f"lr={metrics['learning_rate']:.2e}, "
                              f"epoch={metrics['epoch']:.2f}")
                
                # 记录 GPU 信息
                gpu_info = self.get_gpu_info()
                if gpu_info:
                    avg_memory = sum(g["memory"] for g in gpu_info) / len(gpu_info)
                    avg_util = sum(g["util"] for g in gpu_info) / len(gpu_info)
                    self.metrics["gpu_memory"].append(avg_memory)
                    self.metrics["gpu_util"].append(avg_util)
                
                time.sleep(interval)
                
            except KeyboardInterrupt:
                print("\n监控停止")
                break
            except FileNotFoundError:
                print(f"日志文件不存在：{log_file}")
                time.sleep(5)
    
    def save_metrics(self, output_file: str = None):
        """保存指标到 JSON"""
        if output_file is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = f"logs/training_metrics_{timestamp}.json"
        
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(self.metrics, f, indent=2, ensure_ascii=False)
        
        print(f"指标已保存：{output_file}")
        return output_file
    
    def plot_curves(self, output_dir: str = "saves/figures"):
        """绘制训练曲线"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if not self.metrics["steps"]:
            print("没有数据可绘制")
            return
        
        fig, axes = plt.subplots(2, 2, figsize=(16, 10))
        
        # 1. Loss 曲线
        axes[0, 0].plot(self.metrics["steps"], self.metrics["loss"], 'b-', linewidth=2, label='Loss')
        axes[0, 0].set_xlabel('Step')
        axes[0, 0].set_ylabel('Loss')
        axes[0, 0].set_title('Training Loss')
        axes[0, 0].legend()
        axes[0, 0].grid(True, alpha=0.3)
        
        # 2. Learning Rate 曲线
        axes[0, 1].plot(self.metrics["steps"], self.metrics["learning_rate"], 'r-', linewidth=2, label='Learning Rate')
        axes[0, 1].set_xlabel('Step')
        axes[0, 1].set_ylabel('Learning Rate')
        axes[0, 1].set_title('Learning Rate Schedule')
        axes[0, 1].legend()
        axes[0, 1].grid(True, alpha=0.3)
        
        # 3. GPU 显存使用
        if self.metrics["gpu_memory"]:
            gpu_steps = list(range(1, len(self.metrics["gpu_memory"]) + 1))
            axes[1, 0].plot(gpu_steps, self.metrics["gpu_memory"], 'g-', linewidth=2, label='GPU Memory')
            axes[1, 0].set_xlabel('Sample')
            axes[1, 0].set_ylabel('GPU Memory (MB)')
            axes[1, 0].set_title('GPU Memory Usage')
            axes[1, 0].legend()
            axes[1, 0].grid(True, alpha=0.3)
        
        # 4. GPU 利用率
        if self.metrics["gpu_util"]:
            gpu_steps = list(range(1, len(self.metrics["gpu_util"]) + 1))
            axes[1, 1].plot(gpu_steps, self.metrics["gpu_util"], 'orange', linewidth=2, label='GPU Util')
            axes[1, 1].set_xlabel('Sample')
            axes[1, 1].set_ylabel('GPU Utilization (%)')
            axes[1, 1].set_title('GPU Utilization')
            axes[1, 1].legend()
            axes[1, 1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = f"{output_dir}/training_curves_{timestamp}.png"
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        print(f"曲线已保存：{output_path}")
        plt.close()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='训练监控')
    parser.add_argument('--log_file', type=str, default='logs/sft_train.log', help='训练日志文件')
    parser.add_argument('--interval', type=int, default=10, help='GPU 采样间隔（秒）')
    
    args = parser.parse_args()
    
    monitor = TrainingMonitor()
    
    try:
        monitor.monitor_training(args.log_file, args.interval)
    finally:
        # 保存指标和曲线
        monitor.save_metrics()
        monitor.plot_curves()


if __name__ == '__main__':
    main()
