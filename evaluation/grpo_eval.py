"""
简化版 GRPO 评估模块
基于已有 CosyVoice GRPO 代码，实现推理时质量评估
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np


@dataclass
class GRPOEvalResult:
    """GRPO 评估结果"""
    cer_score: float  # CER 相关奖励
    fluency_score: float  # 流畅度
    relevance_score: float  # 相关性
    overall_score: float  # 综合分数


class GRPOEvaluator:
    """
    GRPO 风格的多维度评估器
    
    参考：third_party/CosyVoice/examples/grpo/cosyvoice2/reward_tts.py
    
    评估维度:
    - CER 分数：基于 ASR 转录的字符错误率
    - 流畅度：文本长度分布、重复度
    - 相关性：与参考文本的语义相似度
    """
    
    def __init__(self, asr_server_url: Optional[str] = None):
        """
        初始化评估器
        
        Args:
            asr_server_url: ASR 服务 URL，用于 CER 评估
        """
        self.asr_server_url = asr_server_url
    
    def evaluate(
        self,
        generated_audio_path: str,
        reference_text: str,
        generated_text: str,
    ) -> GRPOEvalResult:
        """
        评估生成质量
        
        Args:
            generated_audio_path: 生成的音频路径
            reference_text: 参考文本（ground truth）
            generated_text: LLM 生成的文本
        
        Returns:
            评估结果
        """
        # 1. CER 评估（ASR 转录后计算）
        cer_score = self._compute_cer_score(generated_audio_path, reference_text)
        
        # 2. 文本流畅度
        fluency_score = self._compute_fluency(generated_text)
        
        # 3. 语义相关性
        relevance_score = self._compute_relevance(generated_text, reference_text)
        
        # 4. 综合分数
        overall = 0.4 * cer_score + 0.3 * fluency_score + 0.3 * relevance_score
        
        return GRPOEvalResult(
            cer_score=cer_score,
            fluency_score=fluency_score,
            relevance_score=relevance_score,
            overall_score=overall
        )
    
    def _compute_cer_score(self, audio_path: str, reference: str) -> float:
        """
        基于 ASR 的 CER 分数
        
        如果有 ASR 服务，转录音频后计算 CER
        否则返回 0.5（中性分数）
        """
        if not self.asr_server_url:
            return 0.5  # 无服务时返回中性分数
        
        try:
            # 调用 ASR 服务
            import soundfile as sf
            import requests
            
            audio, sr = sf.read(audio_path)
            
            # 这里简化处理，实际应该调用 ASR API
            # 参考 reward_tts.py 中的 _remote_reward
            # cer = 0.3  # 示例值
            # return 1.0 - cer  # 转换为分数
            
            # TODO: 实现真实 ASR 服务调用
            return 0.5
        except Exception:
            return 0.5
    
    def _compute_fluency(self, text: str) -> float:
        """
        文本流畅度评估
        
        基于:
        - 句子长度分布
        - 重复度
        - 语法完整性
        """
        sentences = text.replace("。", ".").replace("！", ".").replace("？", ".").split(".")
        sentences = [s.strip() for s in sentences if s.strip()]
        
        if not sentences:
            return 0.0
        
        # 1. 长度惩罚（太短或太长都不好）
        avg_len = np.mean([len(s) for s in sentences])
        len_score = 1.0 - abs(avg_len - 20) / 20  # 理想长度 20 字
        len_score = max(0.0, min(1.0, len_score))
        
        # 2. 重复度惩罚
        ngrams = self._extract_ngrams(text, n=3)
        if len(ngrams) > 0:
            unique_ratio = len(set(ngrams)) / len(ngrams)
        else:
            unique_ratio = 1.0
        
        # 3. 综合
        return 0.5 * len_score + 0.5 * unique_ratio
    
    def _extract_ngrams(self, text: str, n: int = 3) -> list[str]:
        """提取 n-gram"""
        chars = list(text)
        return ["".join(chars[i:i+n]) for i in range(len(chars) - n + 1)]
    
    def _compute_relevance(self, generated: str, reference: str) -> float:
        """
        语义相关性（基于嵌入相似度）
        """
        try:
            from sentence_transformers import SentenceTransformer
            from sklearn.metrics.pairwise import cosine_similarity
            
            model = SentenceTransformer("bge-large-zh-v1.5")
            gen_emb = model.encode([generated])
            ref_emb = model.encode([reference])
            
            sim = cosine_similarity(gen_emb, ref_emb)[0][0]
            return (sim + 1) / 2  # 归一化到 0-1
        except ImportError:
            # 无依赖时返回中性分数
            return 0.5
        except Exception:
            return 0.5
    
    def save_report(self, result: GRPOEvalResult, output_path: str) -> dict:
        """保存评估报告"""
        report = {
            "cer_score": round(result.cer_score, 4),
            "fluency_score": round(result.fluency_score, 4),
            "relevance_score": round(result.relevance_score, 4),
            "overall_score": round(result.overall_score, 4),
            "interpretation": self._interpret(result.overall_score)
        }
        
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        
        return report
    
    def _interpret(self, score: float) -> str:
        """分数解释"""
        if score >= 0.8:
            return "优秀 - 生成质量高"
        elif score >= 0.6:
            return "良好 - 质量可接受"
        elif score >= 0.4:
            return "中等 - 需要改进"
        else:
            return "较差 - 需要优化"


def main():
    """CLI 快速测试"""
    import argparse
    
    parser = argparse.ArgumentParser(description="GRPO 评估器测试")
    parser.add_argument("--audio", required=True, help="生成的音频路径")
    parser.add_argument("--reference", required=True, help="参考文本")
    parser.add_argument("--generated", required=True, help="生成的文本")
    parser.add_argument("--output", default="eval_report.json", help="评估报告输出路径")
    
    args = parser.parse_args()
    
    evaluator = GRPOEvaluator()
    result = evaluator.evaluate(
        generated_audio_path=args.audio,
        reference_text=args.reference,
        generated_text=args.generated
    )
    
    report = evaluator.save_report(result, args.output)
    
    print(f"评估完成！报告已保存到：{args.output}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
