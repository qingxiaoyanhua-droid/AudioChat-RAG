"""
GRPO 多维度奖励函数
"""

from __future__ import annotations

from typing import List

from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity


class GRPORewardFunction:
    """GRPO 多维度奖励函数"""
    
    def __init__(self, embedder_model: str = None):
        print("[GRPO] 加载嵌入模型（使用 ModelScope）...")
        from modelscope import snapshot_download
        
        if embedder_model is None:
            model_dir = snapshot_download(
                "AI-ModelScope/bge-large-zh-v1.5",
                cache_dir="~/.cache/modelscope"
            )
        else:
            model_dir = embedder_model
        
        self.embedder = SentenceTransformer(model_dir)
        print(f"[GRPO] 模型加载成功：{model_dir}")
        
        self.weights = {"accuracy": 0.4, "fluency": 0.3, "relevance": 0.3}
    
    def compute_reward(self, generated: str, reference: str, query: str) -> float:
        accuracy_reward = self._accuracy_reward(generated, reference)
        fluency_reward = self._fluency_reward(generated)
        relevance_reward = self._relevance_reward(generated, query)
        
        return (
            self.weights["accuracy"] * accuracy_reward +
            self.weights["fluency"] * fluency_reward +
            self.weights["relevance"] * relevance_reward
        )
    
    def _accuracy_reward(self, generated: str, reference: str) -> float:
        if not reference:
            return 0.5
        
        gen_emb = self.embedder.encode([generated])
        ref_emb = self.embedder.encode([reference])
        
        sim = cosine_similarity(gen_emb, ref_emb)[0][0]
        return (sim + 1) / 2
    
    def _fluency_reward(self, generated: str) -> float:
        if not generated:
            return 0.0
        
        ngrams = self._extract_ngrams(generated, n=3)
        
        if len(ngrams) == 0:
            return 0.0
        
        unique_ratio = len(set(ngrams)) / len(ngrams)
        
        length = len(generated)
        if length < 10:
            length_penalty = 0.5
        elif length > 500:
            length_penalty = 0.7
        else:
            length_penalty = 1.0
        
        return unique_ratio * length_penalty
    
    def _relevance_reward(self, generated: str, query: str) -> float:
        if not query:
            return 0.5
        
        gen_emb = self.embedder.encode([generated])
        query_emb = self.embedder.encode([query])
        
        sim = cosine_similarity(gen_emb, query_emb)[0][0]
        return (sim + 1) / 2
    
    def _extract_ngrams(self, text: str, n: int = 3) -> List[str]:
        chars = list(text)
        return ["".join(chars[i:i+n]) for i in range(len(chars) - n + 1)]
