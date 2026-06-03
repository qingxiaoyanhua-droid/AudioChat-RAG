"""
离线 Pipeline - 文本输出模式（S2T）

完整流程：音频 -> 说话人分离（Diarization）-> ASR 识别 -> LLM 文本生成
输出：带有说话人标签和时间戳的转写文本 + LLM 的文本回复

Usage:
    python scripts/offline_pipeline.py --audio <wav> --funasr-model <path> --funaudiochat-model <path>

Example:
    python scripts/offline_pipeline.py \
        --audio examples/2speakers_example.wav \
        --funasr-model /data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch \
        --funaudiochat-model /data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B \
        --output-dir AudioChat_saves
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "third_party"))
sys.path.insert(0, str(root / "third_party" / "CosyVoice"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline: 3D-Speaker diarization -> FunASR ASR -> Fun-Audio-Chat LLM"
    )
    parser.add_argument("--audio", required=True, help="Input audio path")

    # 3D-Speaker diarization
    parser.add_argument("--diar-device", default=None, help="cuda / cuda:0 / cpu")
    parser.add_argument(
        "--speaker-num", type=int, default=None, help="Oracle speaker num"
    )
    parser.add_argument(
        "--model-cache-dir", default=None, help="Optional model cache dir"
    )
    parser.add_argument(
        "--speaker-embedding-model-path",
        default="/data/models/Voice/iic/speech_campplus_sv_zh-cn_16k-common/campplus_cn_en_common.pt",
        help="Local path to speaker embedding model (.pt file)",
    )
    parser.add_argument(
        "--vad-model-path",
        default="/data/models/Voice/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        help="Local path to VAD model directory",
    )

    # FunASR ASR
    parser.add_argument(
        "--funasr-model",
        default="/data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    )
    parser.add_argument("--funasr-device", default="cuda:0")
    parser.add_argument(
        "--funasr-vad-model-path",
        default="/data/models/Voice/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        help="Local path to FunASR VAD model directory",
    )
    parser.add_argument(
        "--funasr-punc-model-path",
        default="/data/models/Voice/iic/punc_ct-transformer_cn-en-common-vocab471067-large",
        help="Local path to FunASR punctuation model directory",
    )

    # Fun-Audio-Chat
    parser.add_argument(
        "--funaudiochat-model",
        default="~/llm/voice/Fun-Audio-Chat/pretrained_models/Fun-Audio-Chat-8B",
    )

    parser.add_argument("--llm-device", default=None)
    parser.add_argument("--instruction", default="请按 speaker 分组输出对话稿。")
    parser.add_argument("--max-lines", type=int, default=400)

    parser.add_argument("--output-dir", default="AudioChat_saves")

    # 工作模式
    parser.add_argument(
        "--mode",
        default="summary",
        choices=["summary", "qa"],
        help="summary: 生成结构化会议纪要；qa: 回答用户自定义问题（需配合 --query 使用）"
    )
    parser.add_argument(
        "--query",
        default=None,
        help="用户自定义问题（如'第三个发言人的技术方案是什么？'），配合 --mode qa 使用"
    )

    # RAG
    parser.add_argument("--enable-rag", action="store_true", help="启用 RAG 检索")
    parser.add_argument("--hierarchical-rag", action="store_true",
                        help="启用 GA 风格分层 RAG（L1路由 + L2/L3分层检索）")
    parser.add_argument("--rag-storage-dir", default="./rag_storage", help="RAG 存储目录")
    parser.add_argument("--meeting-id", default=None, help="会议 ID（用于存储/检索）")

    # ASR 后校正
    parser.add_argument(
        "--enable-asr-correction",
        action="store_true",
        help="启用 ASR 后校正（基于 Qwen Prompt 纠错，增加约 1-2s 延迟）"
    )
    parser.add_argument(
        "--corrector-model",
        default="Qwen/Qwen2.5-0.5B-Instruct",
        help="ASR 后校正模型路径"
    )

    args = parser.parse_args()

    from audiochat.audio_io import ensure_mono_16k, slice_waveform
    from audiochat.asr.funasr_asr import FunASRTranscriber
    from audiochat.diarization.diarizer_3dspeaker import ThreeDSpeakerDiarizer
    from audiochat.llm.funaudiochat_llm import FunAudioChatLLM
    from audiochat.prompting import build_llm_instruction

    # 初始化 ASR 后校正器（懒加载，关闭时零开销）
    corrector = None
    if args.enable_asr_correction:
        from audiochat.asr.asr_corrector import ASRPostCorrector
        corrector = ASRPostCorrector(
            model_path=args.corrector_model,
            device=args.funasr_device,
            enabled=True,
        )
        print(f"[ASR 校正] 已启用，模型：{args.corrector_model}")

    # 初始化 RAG
    retriever = None
    if args.enable_rag:
        from audiochat.rag.retriever import AudioChatRetriever
        from audiochat.rag.storage import MeetingMemoryStore

        storage = MeetingMemoryStore(persist_dir=args.rag_storage_dir)
        retriever = AudioChatRetriever(storage)

        if args.hierarchical_rag:
            from audiochat.rag.hierarchical_retriever import HierarchicalRetriever
            from audiochat.rag.memory_hierarchy import HierarchicalMeetingStore

            hier_store = HierarchicalMeetingStore(persist_dir=args.rag_storage_dir + "_hierarchical")
            hier_retriever = HierarchicalRetriever(
                hierarchical_store=hier_store,
                base_retriever=retriever,  # fallback 到原有扁平检索
            )
            retriever = hier_retriever
            print(f"[分层RAG] 已启用，存储目录（分层）：{args.rag_storage_dir}_hierarchical")
        else:
            print(f"[RAG] 已启用，存储目录：{args.rag_storage_dir}")

        if hasattr(retriever, '_warm_up'):
            retriever._warm_up()  # 预热，消除冷启动

    audio = ensure_mono_16k(args.audio)

    diarizer = ThreeDSpeakerDiarizer(
        device=args.diar_device,
        model_cache_dir=args.model_cache_dir,
        speaker_embedding_model_path=args.speaker_embedding_model_path,
        vad_model_path=args.vad_model_path,
    )
    diar_segments = diarizer.diarize(
        audio.waveform, wav_fs=audio.sample_rate, speaker_num=args.speaker_num
    )

    transcriber = FunASRTranscriber(
        model=args.funasr_model,
        device=args.funasr_device,
        vad_model=args.funasr_vad_model_path,
        punc_model=args.funasr_punc_model_path,
    )

    utterances = []
    raw_asr_items = []
    for seg in diar_segments:
        seg_audio = slice_waveform(
            audio.waveform, seg.start_s, seg.end_s, sample_rate=audio.sample_rate
        )
        seg_start_ms = int(round(seg.start_s * 1000.0))
        spk = f"spk{seg.speaker}"
        uts, raw = transcriber.transcribe_segment(
            seg_audio, speaker=spk, segment_start_ms=seg_start_ms
        )
        utterances.extend(uts)
        raw_asr_items.append(raw)

    utterances.sort(key=lambda u: (u.start_ms, u.end_ms, u.speaker))

    # ASR 后校正（如果启用）
    if corrector is not None:
        import time
        t0 = time.perf_counter()
        corrected_utterances = []
        for u in utterances:
            corrected_text = corrector.correct(u.text)
            # frozen dataclass 不能直接修改字段，需要重建对象
            from audiochat.asr.funasr_asr import Utterance
            corrected_utterances.append(Utterance(
                speaker=u.speaker,
                start_ms=u.start_ms,
                end_ms=u.end_ms,
                text=corrected_text,
            ))
        utterances = corrected_utterances
        print(f"[ASR 校正] 完成，耗时：{time.perf_counter() - t0:.1f}s")

    # 保存到 RAG（如果启用）—— 使用校正后的 utterances
    if args.enable_rag and retriever is not None:
        meeting_id = args.meeting_id or Path(args.audio).stem
        retriever.add_meeting_record(meeting_id, utterances)
        print(f"[RAG] 已保存 {len(utterances)} 条 utterance 到会议记录：{meeting_id}")

    os.makedirs(args.output_dir, exist_ok=True)

    # 从音频路径提取文件名作为中间文件前缀，避免不同音频的处理结果覆盖
    audio_basename = Path(args.audio).stem
    diarization_path = os.path.join(
        args.output_dir, f"{audio_basename}_diarization.json"
    )
    asr_utterances_path = os.path.join(
        args.output_dir, f"{audio_basename}_asr_utterances.json"
    )
    llm_reply_path = os.path.join(args.output_dir, f"{audio_basename}_llm_reply.txt")

    with open(diarization_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "audio": args.audio,
                "segments": [seg.__dict__ for seg in diar_segments],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    with open(asr_utterances_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "audio": args.audio,
                "utterances": [u.__dict__ for u in utterances],
                "raw": raw_asr_items,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # 构建 LLM 指令
    # 模式选择：qa 模式用 query，summary 模式用 instruction
    user_instr = args.query if args.mode == "qa" else args.instruction
    
    # 如果是 summary 模式但没指定 instruction，使用默认的会议纪要模板
    if args.mode == "summary" and args.instruction == "请按 speaker 分组输出对话稿。":
        from audiochat.prompting import DEFAULT_SUMMARY_INSTRUCTION
        user_instr = DEFAULT_SUMMARY_INSTRUCTION

    instruction = build_llm_instruction(
        utterances=utterances,
        user_instruction=user_instr,
        max_lines=args.max_lines,
        retriever=retriever,
        enable_rag=args.enable_rag,
        mode=args.mode,
    )

    llm = FunAudioChatLLM(model_path=args.funaudiochat_model, device=args.llm_device)
    result = llm.generate_text(instruction=instruction)

    with open(llm_reply_path, "w", encoding="utf-8") as f:
        f.write(result.text)
        f.write("\n")

    print(result.text)


if __name__ == "__main__":
    main()
