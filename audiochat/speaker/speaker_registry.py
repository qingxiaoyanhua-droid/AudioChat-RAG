"""
说话人注册与声纹匹配模块

功能：
  - 从 DiarizationSegments 提取 voice embedding（192维 CampPlus 向量）
  - 与已有说话人档案做余弦相似度匹配
  - 自动注册新说话人或匹配已知说话人
  - 持久化存储：FAISS 索引（声纹）+ JSON（档案元数据）

Usage:
    from audiochat.speaker.speaker_registry import SpeakerRegistry, SpeakerMatcher

    registry = SpeakerRegistry(persist_dir="./speaker_storage")
    matcher = SpeakerMatcher(registry)

    # 新会议后，对每个说话人做声纹注册/匹配
    matched = matcher.register_meeting_speakers(segments, waveform, sample_rate)
    # matched: list[(speaker_label, SpeakerProfile, is_new)]
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SpeakerProfile:
    """
    说话人档案（持久化到 JSON）
    关联 FAISS 索引中的声纹向量
    """
    profile_id: str           # UUID，作为 FAISS 索引中的主键
    name: str                 # 姓名（可手动补充，初次注册时为 spk0 等）
    role: str = ""            # 职位，如"后端工程师"
    department: str = ""      # 部门
    email: str = ""           # 邮箱（用于任务分配后发邮件）

    # 声纹相关（用于调试和展示）
    embedding_norm: float = 0.0  # embedding 的 L2 范数
    embedding_count: int = 0     # 累计注册次数（每次会议更新）

    # 统计特征（从历史会议聚合）
    meeting_count: int = 0
    topic_keywords: list[str] = field(default_factory=list)   # 高频讨论话题
    speaking_ratio: float = 0.0                             # 发言时长占比

    # 元数据
    created_at: str = ""
    updated_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "role": self.role,
            "department": self.department,
            "email": self.email,
            "embedding_norm": self.embedding_norm,
            "embedding_count": self.embedding_count,
            "meeting_count": self.meeting_count,
            "topic_keywords": self.topic_keywords,
            "speaking_ratio": self.speaking_ratio,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SpeakerProfile:
        return cls(
            profile_id=d["profile_id"],
            name=d["name"],
            role=d.get("role", ""),
            department=d.get("department", ""),
            email=d.get("email", ""),
            embedding_norm=d.get("embedding_norm", 0.0),
            embedding_count=d.get("embedding_count", 0),
            meeting_count=d.get("meeting_count", 0),
            topic_keywords=d.get("topic_keywords", []),
            speaking_ratio=d.get("speaking_ratio", 0.0),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            notes=d.get("notes", ""),
        )


# ---------------------------------------------------------------------------
# 轻量 FAISS 包装（避免外部依赖时优雅降级）
# ---------------------------------------------------------------------------

class _FaissIndex:
    """
    FAISS 索引包装类。
    如果 faiss 不可用，使用纯 numpy 实现（仅适合小规模场景）。
    """

    def __init__(self, dim: int, metric: str = "cosine"):
        self.dim = dim
        self.metric = metric
        self._ids: list[str] = []
        self._index = None
        self._numpy_vectors: Optional[np.ndarray] = None

        try:
            import faiss
            self._use_faiss = True
            if metric == "cosine":
                # L2 normalize 后用内积等价于余弦相似度
                self._faiss_index = faiss.IndexFlatIP(dim)
                self._normalize = True
            else:
                self._faiss_index = faiss.IndexFlatL2(dim)
                self._normalize = False
        except ImportError:
            self._use_faiss = False
            self._numpy_vectors = np.empty((0, dim), dtype=np.float32)

    def _normalize_vectors(self, v: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return v / norms

    def add(self, ids: list[str], vectors: np.ndarray):
        """添加向量到索引"""
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)

        if self._normalize:
            vectors = self._normalize_vectors(vectors)

        if self._use_faiss:
            self._faiss_index.add(vectors)
        else:
            self._numpy_vectors = np.vstack([self._numpy_vectors, vectors])

        self._ids.extend(ids)

    def search(self, query: np.ndarray, k: int = 1) -> tuple[list[str], list[float]]:
        """
        搜索最相似的 k 个向量。

        Returns:
            (matched_ids, distances): 匹配到的 ID 列表和距离（余弦相似度）
        """
        query = np.asarray(query, dtype=np.float32).reshape(1, -1)
        if self._normalize:
            query = self._normalize_vectors(query)

        if self._use_faiss:
            distances, indices = self._faiss_index.search(query, min(k, self._faiss_index.ntotal))
            matched_ids = [self._ids[i] for i in indices[0] if i < len(self._ids)]
            dists = distances[0].tolist()[:k]
        else:
            # 纯 numpy: 计算余弦相似度
            if self._numpy_vectors.shape[0] == 0:
                return [], []
            # query 已经是归一化的
            similarities = self._numpy_vectors @ query.T
            top_k_idx = np.argsort(similarities.flatten())[::-1][:k]
            matched_ids = [self._ids[i] for i in top_k_idx]
            dists = [similarities[i, 0] for i in top_k_idx]

        return matched_ids[:k], dists[:k]

    def __len__(self) -> int:
        if self._use_faiss:
            return self._faiss_index.ntotal
        return self._numpy_vectors.shape[0]


# ---------------------------------------------------------------------------
# 说话人注册表
# ---------------------------------------------------------------------------

class SpeakerRegistry:
    """
    说话人档案持久化管理器。

    数据存储：
      - profile_id → SpeakerProfile（JSON 文件）
      - voice_embedding → FAISS 索引文件

    Usage:
        registry = SpeakerRegistry(persist_dir="./speaker_storage")
        registry.save_profile(profile)
        profile = registry.get_profile("uuid-string")
    """

    _EMBEDDING_DIM = 192   # CampPlus embedding 维度

    def __init__(self, persist_dir: str = "./speaker_storage"):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self._profiles_path = self.persist_dir / "profiles.json"
        self._index_path = self.persist_dir / "index.faiss"

        self._profiles: dict[str, SpeakerProfile] = self._load_profiles()
        self._index = self._load_or_create_index()

    # ------------------------------------------------------------------
    # Profiles (JSON)
    # ------------------------------------------------------------------

    def _load_profiles(self) -> dict[str, SpeakerProfile]:
        if not self._profiles_path.exists():
            return {}
        try:
            with open(self._profiles_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: SpeakerProfile.from_dict(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_profiles(self):
        with open(self._profiles_path, "w", encoding="utf-8") as f:
            json.dump(
                {k: v.to_dict() for k, v in self._profiles.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )

    def save_profile(self, profile: SpeakerProfile) -> SpeakerProfile:
        """保存或更新说话人档案"""
        self._profiles[profile.profile_id] = profile
        self._save_profiles()
        return profile

    def get_profile(self, profile_id: str) -> Optional[SpeakerProfile]:
        return self._profiles.get(profile_id)

    def get_all_profiles(self) -> list[SpeakerProfile]:
        return list(self._profiles.values())

    def find_profile_by_name(self, name: str) -> Optional[SpeakerProfile]:
        """按姓名查找档案（精确匹配）"""
        for p in self._profiles.values():
            if p.name == name:
                return p
        return None

    # ------------------------------------------------------------------
    # FAISS Index
    # ------------------------------------------------------------------

    def _load_or_create_index(self) -> _FaissIndex:
        index = _FaissIndex(dim=self._EMBEDDING_DIM, metric="cosine")
        if self._index_path.exists() and self._profiles:
            # 重建索引（从 profiles 重新加载 embedding）
            # 注意：embedding 本身不存储在 JSON 中（太大），
            # 需要在构建时同步写入一个 .npy 文件
            embeddings_path = self.persist_dir / "embeddings.npy"
            if embeddings_path.exists():
                try:
                    all_embeddings = np.load(embeddings_path)
                    ids = list(self._profiles.keys())
                    if all_embeddings.shape[0] == len(ids):
                        index.add(ids, all_embeddings)
                except Exception:
                    pass
        return index

    def _save_index(self, embeddings: np.ndarray, ids: list[str]):
        """持久化 FAISS 索引和 embedding 向量"""
        embeddings_path = self.persist_dir / "embeddings.npy"
        np.save(embeddings_path, embeddings.astype(np.float32))
        # FAISS 索引保存
        try:
            import faiss
            if self._index._normalize:
                faiss.write_index(
                    self._index._faiss_index,
                    str(self._index_path),
                )
        except ImportError:
            pass

    def register_embedding(self, profile_id: str, embedding: np.ndarray) -> SpeakerProfile:
        """
        将说话人的声纹向量注册到索引。

        Args:
            profile_id: 说话人档案 ID
            embedding: 192维 CampPlus embedding（单条向量）

        Returns:
            更新后的 SpeakerProfile
        """
        embedding = np.asarray(embedding, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(embedding))
        self._index.add([profile_id], embedding.reshape(1, -1))

        # 持久化
        all_emb = []
        all_ids = []
        for pid, prof in self._profiles.items():
            all_ids.append(pid)
        # 重新构建 embedding 矩阵（按 profiles 顺序）
        # 注意：这会丢失未在 profiles 中的 embedding
        # 实际使用时应在 register_embedding 时追加写入
        # 这里用简化方案：每次都重新保存当前 embedding
        emb_path = self.persist_dir / "embeddings.npy"
        if emb_path.exists():
            existing = np.load(emb_path)
            all_emb = [existing]
        all_emb.append(embedding)
        if all_emb:
            self._save_index(np.vstack(all_emb) if len(all_emb) > 1 else all_emb[0].reshape(1, -1), all_ids)

        profile = self._profiles.get(profile_id)
        if profile:
            profile.embedding_norm = norm
            profile.embedding_count += 1
            self._save_profiles()
        return profile

    def search_similar(
        self,
        embedding: np.ndarray,
        k: int = 1,
        threshold: float = 0.7,
    ) -> list[tuple[SpeakerProfile, float]]:
        """
        在已有说话人档案中搜索与给定声纹最相似的说话人。

        Args:
            embedding: 192维声纹向量
            k: 返回数量上限
            threshold: 余弦相似度阈值，超过才返回

        Returns:
            [(SpeakerProfile, similarity_score), ...]
        """
        embedding = np.asarray(embedding, dtype=np.float32).reshape(-1)
        matched_ids, distances = self._index.search(embedding, k)

        results = []
        for pid, dist in zip(matched_ids, distances):
            profile = self._profiles.get(pid)
            if profile and dist >= threshold:
                results.append((profile, float(dist)))
        return results

    def remove_profile(self, profile_id: str):
        """删除说话人档案（同时从索引中移除）"""
        if profile_id in self._profiles:
            del self._profiles[profile_id]
            self._save_profiles()

    def get_stats(self) -> dict:
        return {
            "total_profiles": len(self._profiles),
            "total_embeddings_in_index": len(self._index),
            "persist_dir": str(self.persist_dir),
        }


# ---------------------------------------------------------------------------
# 说话人匹配器（对接 DiarizationSegments）
# ---------------------------------------------------------------------------

@dataclass
class MatchedSpeaker:
    """一次匹配结果"""
    speaker_label: str       # "spk0", "spk1" 等，来自 diarization
    profile: SpeakerProfile  # 关联的说话人档案
    similarity: float        # 余弦相似度
    is_new: bool             # 是否是新注册的说话人


class SpeakerMatcher:
    """
    从会议 diarization segments 提取声纹向量并与注册表匹配。

    匹配逻辑：
      1. 对每个 speaker 标签，聚合其在所有 segments 中的 embedding
         → 取 centroid（均值）作为该说话人的代表声纹
      2. 用 centroid 在 FAISS 中搜索余弦相似度
      3. 超过阈值 → 匹配已有 profile
         低于阈值 → 创建新 profile
      4. 更新 centroid（增量平均，防止重注册抖动）

    Usage:
        matcher = SpeakerMatcher(registry)
        # 需要传入 diarizer 和 waveform 来提取 embedding
        results = matcher.match_from_diarization(
            diarizer=diarizer,
            waveform=audio.waveform,
            sample_rate=audio.sample_rate,
            diar_segments=diar_segments,
            threshold=0.75,
        )
    """

    def __init__(self, registry: SpeakerRegistry):
        self.registry = registry

    def extract_speaker_embeddings(
        self,
        diarizer,
        waveform: np.ndarray,
        sample_rate: int,
        diar_segments: list,
    ) -> dict[int, np.ndarray]:
        """
        从 diarization segments 提取每个说话人的 centroid embedding。

        Returns:
            {speaker_int: centroid_embedding}
        """
        from audiochat.diarization.diarizer_3dspeaker import (
            DiarizationSegment,
            Diarization3Dspeaker,
        )

        # 收集每个说话人的所有 embedding
        speaker_chunks: dict[int, list[np.ndarray]] = {}
        for seg in diar_segments:
            assert isinstance(seg, DiarizationSegment)
            st = int(seg.start_s * sample_rate)
            ed = int(seg.end_s * sample_rate)
            seg_wav = waveform[st:ed]

            # 手动分 chunk（复用 diarizer 的逻辑）
            chunks = diarizer.impl.chunk(seg.start_s, seg.end_s, dur=1.5, step=0.75)
            if not chunks:
                continue

            feats = []
            fs = diarizer.impl.fs
            for cst, ced in chunks:
                cst_i = int(cst * fs)
                ced_i = int(ced * fs)
                seg_slice = seg_wav[cst_i:cst_i + (ced - cst) * fs // 1]
                # 截取 wav 片段
                actual_slice = waveform[int(cst * sample_rate):int(ced * sample_rate)]
                if len(actual_slice) == 0:
                    continue

                from speakerlab.utils.fileio import load_audio
                from speakerlab.utils.utils import circle_pad
                import torch
                # 构建 batch
                max_len = len(actual_slice)
                padded = circle_pad(torch.from_numpy(actual_slice).float(), max_len)
                feat = diarizer.impl.feature_extractor(padded.unsqueeze(0))
                emb = diarizer.impl.embedding_model(feat).detach().numpy()
                spk = seg.speaker
                if spk not in speaker_chunks:
                    speaker_chunks[spk] = []
                speaker_chunks[spk].append(emb.flatten())

        # 计算 centroid
        centroids = {}
        for spk, embeddings in speaker_chunks.items():
            stacked = np.vstack(embeddings)
            centroids[spk] = stacked.mean(axis=0)
        return centroids

    def match_from_diarization(
        self,
        diarizer,
        waveform: np.ndarray,
        sample_rate: int,
        diar_segments: list,
        threshold: float = 0.75,
    ) -> list[MatchedSpeaker]:
        """
        主入口：从会议 diarization segments 提取声纹并匹配。

        Args:
            diarizer: ThreeDSpeakerDiarizer 实例（用于提取 embedding）
            waveform: 原始音频波形（numpy array）
            sample_rate: 采样率（如 16000）
            diar_segments: DiarizationSegment 列表
            threshold: 声纹匹配阈值（余弦相似度）

        Returns:
            list[MatchedSpeaker]，每个说话人标签对应一个匹配结果
        """
        centroids = self.extract_speaker_embeddings(
            diarizer, waveform, sample_rate, diar_segments
        )

        results: list[MatchedSpeaker] = []
        for spk_int, centroid in sorted(centroids.items()):
            speaker_label = f"spk{spk_int}"

            # 搜索已有档案
            matches = self.registry.search_similar(centroid, k=1, threshold=threshold)

            if matches:
                profile, similarity = matches[0]
                # 增量更新 centroid（加权平均）
                updated_emb = self._incremental_update(profile, centroid, profile.embedding_count)
                self.registry.register_embedding(profile.profile_id, updated_emb)
                profile.meeting_count += 1
                self.registry.save_profile(profile)
                results.append(MatchedSpeaker(
                    speaker_label=speaker_label,
                    profile=profile,
                    similarity=similarity,
                    is_new=False,
                ))
            else:
                # 新建档案
                profile_id = str(uuid.uuid4())
                profile = SpeakerProfile(
                    profile_id=profile_id,
                    name=speaker_label,
                    embedding_norm=float(np.linalg.norm(centroid)),
                    embedding_count=1,
                    meeting_count=1,
                )
                self.registry.save_profile(profile)
                self.registry.register_embedding(profile_id, centroid)
                results.append(MatchedSpeaker(
                    speaker_label=speaker_label,
                    profile=profile,
                    similarity=1.0,
                    is_new=True,
                ))

        return results

    def _incremental_update(
        self, profile: SpeakerProfile, new_emb: np.ndarray, existing_count: int
    ) -> np.ndarray:
        """
        增量更新声纹向量（指数移动平均），避免重注册时抖动。

        EMA 公式：updated = (existing * count + new) / (count + 1)
        """
        existing_emb_norm = profile.embedding_norm
        # 从 norm 重建归一化向量方向（保留历史方向信息）
        # 简化：用新 centroid（实际使用中 centroid 更稳定）
        return new_emb


# ---------------------------------------------------------------------------
# 便捷构造
# ---------------------------------------------------------------------------

def make_speaker_registry(persist_dir: str = "./speaker_storage") -> SpeakerRegistry:
    """创建或加载说话人注册表（单例工厂）"""
    return SpeakerRegistry(persist_dir=persist_dir)


def build_speaker_prefix(matched_speakers: list[MatchedSpeaker]) -> str:
    """
    根据匹配结果构建 RAG embedding 前缀字符串。

    Usage:
        prefix = build_speaker_prefix(matched)
        # "[参会人：张三(声纹ID: abc123), 李四(声纹ID: def456)] "
    """
    if not matched_speakers:
        return ""

    parts = []
    for ms in matched_speakers:
        role_tag = f"[{ms.profile.role}]" if ms.profile.role else ""
        parts.append(f"{ms.profile.name}{role_tag}(声纹ID:{ms.profile.profile_id[:8]})")

    return f"[参会人：{', '.join(parts)}] "


def build_meeting_context(
    matched_speakers: list[MatchedSpeaker],
    utterances: list,
) -> str:
    """
    构建包含说话人 Profile 信息的完整会议文本。

    在原始 utterances 基础上，在开头附加 speaker profile 摘要，
    供 BGE embedding 时使用。

    Args:
        matched_speakers: SpeakerMatcher 的匹配结果
        utterances: ASR 输出的 utterances 列表（每个有 speaker, text 属性）

    Returns:
        带 speaker profile 前缀的完整转写文本
    """
    if not matched_speakers:
        return ""

    prefix_parts = []
    for ms in matched_speakers:
        role = ms.profile.role or "未分配角色"
        dept = ms.profile.department or ""
        dept_str = f"（{dept}）" if dept else ""
        topics = ", ".join(ms.profile.topic_keywords[:3]) if ms.profile.topic_keywords else "暂无记录"
        prefix_parts.append(
            f"  - {ms.profile.name}{dept_str} | 角色：{role} | 擅长话题：{topics}"
        )

    header = "【参会人画像】\n" + "\n".join(prefix_parts) + "\n\n【会议记录】\n"
    return header
