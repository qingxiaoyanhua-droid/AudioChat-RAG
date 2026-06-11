"""
说话人模块

提供说话人注册、声纹匹配、人物画像管理功能。
"""

from audiochat.speaker.speaker_registry import (
    SpeakerRegistry,
    SpeakerMatcher,
    SpeakerProfile,
    MatchedSpeaker,
    make_speaker_registry,
    build_speaker_prefix,
    build_meeting_context,
)

__all__ = [
    "SpeakerRegistry",
    "SpeakerMatcher",
    "SpeakerProfile",
    "MatchedSpeaker",
    "make_speaker_registry",
    "build_speaker_prefix",
    "build_meeting_context",
]
