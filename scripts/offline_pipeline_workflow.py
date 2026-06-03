"""
离线 Pipeline - 工作流模式（HITL / Human-in-the-Loop）

基于 offline_pipeline.py，额外支持：
  - 状态机持久化（每个任务一个 TaskState）
  - LLM 生成后停住，进入 PENDING_APPROVAL，等待人工确认
  - 确认后才发邮件；拒绝后带反馈重新生成

Usage:
    python scripts/offline_pipeline_workflow.py --audio <wav> \
        --funasr-model <path> --funaudiochat-model <path> \
        --workflow-store ./workflow_tasks --enable-workflow

    # 审核（命令行替代前端）：
    python scripts/task_cli.py list
    python scripts/task_cli.py approve <task_id>
    python scripts/task_cli.py reject <task_id> "修改意见..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "third_party"))
sys.path.insert(0, str(root / "third_party" / "CosyVoice"))


# ---------------------------------------------------------------------------
# 阶段函数（每个阶段独立，失败抛异常由外层统一处理）
# ---------------------------------------------------------------------------

def stage_asr(
    audio_path: str,
    args: argparse.Namespace,
    state,
    store,
) -> list:
    from audiochat.audio_io import ensure_mono_16k, slice_waveform
    from audiochat.asr.funasr_asr import FunASRTranscriber, Utterance

    from audiochat.workflow import TaskStatus
    state.status = TaskStatus.PENDING_ASR
    store.save(state)

    audio = ensure_mono_16k(audio_path)
    from audiochat.diarization.diarizer_3dspeaker import ThreeDSpeakerDiarizer
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
            audio.waveform, seg.start_s, seg.end_s, sample_rate=audio.sample_rate,
        )
        seg_start_ms = int(round(seg.start_s * 1000.0))
        spk = f"spk{seg.speaker}"
        uts, raw = transcriber.transcribe_segment(
            seg_audio, speaker=spk, segment_start_ms=seg_start_ms,
        )
        utterances.extend(uts)
        raw_asr_items.append(raw)

    utterances.sort(key=lambda u: (u.start_ms, u.end_ms, u.speaker))
    return utterances, raw_asr_items, diar_segments


def stage_correction(
    utterances: list,
    args: argparse.Namespace,
    state,
    store,
) -> list:
    from audiochat.workflow import TaskStatus

    if not args.enable_asr_correction:
        return utterances

    from audiochat.asr.asr_corrector import ASRPostCorrector
    from audiochat.asr.funasr_asr import Utterance

    corrector = ASRPostCorrector(
        model_path=args.corrector_model,
        device=args.funasr_device,
        enabled=True,
    )
    print(f"[ASR 校正] 已启用，模型：{args.corrector_model}")

    t0 = time.perf_counter()
    corrected = []
    for u in utterances:
        corrected_text = corrector.correct(u.text)
        corrected.append(Utterance(
            speaker=u.speaker,
            start_ms=u.start_ms,
            end_ms=u.end_ms,
            text=corrected_text,
        ))
    print(f"[ASR 校正] 完成，耗时：{time.perf_counter() - t0:.1f}s")
    return corrected


def stage_rag(
    utterances: list,
    args: argparse.Namespace,
    state,
    store,
):
    from audiochat.workflow import TaskStatus

    if not args.enable_rag:
        return

    meeting_id = args.meeting_id or Path(args.audio).stem

    from audiochat.rag.retriever import AudioChatRetriever
    from audiochat.rag.storage import MeetingMemoryStore
    from audiochat.prompting import DEFAULT_SUMMARY_INSTRUCTION

    storage = MeetingMemoryStore(persist_dir=args.rag_storage_dir)
    retriever = AudioChatRetriever(storage)

    if args.hierarchical_rag:
        from audiochat.rag.hierarchical_retriever import HierarchicalRetriever
        from audiochat.rag.memory_hierarchy import HierarchicalMeetingStore
        hier_store = HierarchicalMeetingStore(persist_dir=args.rag_storage_dir + "_hierarchical")
        hier_retriever = HierarchicalRetriever(
            hierarchical_store=hier_store,
            base_retriever=retriever,
        )
        retriever = hier_retriever
        print(f"[分层RAG] 已启用")
    else:
        print(f"[RAG] 已启用")

    if hasattr(retriever, '_warm_up'):
        retriever._warm_up()

    retriever.add_meeting_record(meeting_id, utterances)
    print(f"[RAG] 已保存 {len(utterances)} 条 utterance：{meeting_id}")
    return retriever, meeting_id


def stage_llm(
    utterances: list,
    args: argparse.Namespace,
    state,
    store,
    retriever=None,
) -> dict:
    """
    Returns a dict with keys: text, planning_result
    """
    from audiochat.workflow import TaskStatus
    from audiochat.prompting import (
        build_llm_instruction,
        build_agentic_instruction,
        DEFAULT_SUMMARY_INSTRUCTION,
    )
    from audiochat.llm.funaudiochat_llm import FunAudioChatLLM

    state.status = TaskStatus.PENDING_LLM
    store.save(state)

    user_instr = args.query if args.mode == "qa" else args.instruction
    if args.mode == "summary" and args.instruction == "请按 speaker 分组输出对话稿。":
        user_instr = DEFAULT_SUMMARY_INSTRUCTION

    # 解析 time_range 参数（格式：2026-01-01,2026-06-01）
    time_range: Optional[tuple[str, str]] = None
    if getattr(args, "time_range", None):
        parts = args.time_range.split(",")
        if len(parts) == 2:
            time_range = (parts[0].strip(), parts[1].strip())

    # Agentic RAG 模式：信息缺口分析 + 批量检索 + 上下文压缩
    planning_result = None
    if getattr(args, "agentic_rag", False) and args.mode == "summary" and retriever is not None:
        print(f"[Agentic RAG] 启用信息缺口分析 + 批量检索...")
        instruction, planning_result = build_agentic_instruction(
            utterances=utterances,
            user_instruction=user_instr,
            retriever=retriever,
            meeting_id=meeting_id,
            time_range=time_range,
        )
        if planning_result and planning_result.gaps:
            print(f"[Agentic RAG] 识别信息缺口 {len(planning_result.gaps)} 个，已批量检索")
        else:
            print(f"[Agentic RAG] 无需补充历史信息")
    else:
        instruction = build_llm_instruction(
            utterances=utterances,
            user_instruction=user_instr,
            max_lines=args.max_lines,
            retriever=retriever,
            enable_rag=args.enable_rag,
            mode=args.mode,
            meeting_id=meeting_id,
            time_range=time_range,
        )

    llm = FunAudioChatLLM(model_path=args.funaudiochat_model, device=args.llm_device)
    result = llm.generate_text(instruction=instruction)

    # 核心产物写入 state
    state.summary = result.text

    # 从总结文本中提取行动项（简单按行解析，以序号或关键词开头即为行动项）
    action_items = _extract_action_items(result.text)
    state.action_items = tuple(action_items)

    # Stage 5: AI 自动质量报告
    print("[Workflow] Stage 5/5: AI 质量评估...")
    report = _generate_and_save_report(state, llm)
    state.quality_report = report
    state.status = TaskStatus.PENDING_APPROVAL
    store.save(state)

    _print_task_header(state)
    print(result.text)
    print(f"\n{'='*60}")
    print(report.display_summary())
    print(f"{'='*60}\n")
    print(f"[Workflow] 任务 {state.task_id} 进入待审核状态。")
    print(f"  查看任务列表：python scripts/task_cli.py list")

    return {"text": result.text, "planning_result": planning_result}


def _extract_action_items(text: str) -> list[str]:
    """从总结文本中提取行动项（按行解析，以序号或关键词开头的行视为行动项）"""
    items = []
    action_keywords = ["行动项", "action", "任务", "下一步", "TODO", "待办", "负责人", "截止"]
    lines = text.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 以数字序号开头（如 "1. " "（1）" "【1】"）
        if re.match(r"^\d+[.、)）\]\：:]", line):
            items.append(line)
        # 以行动关键词开头
        elif any(line.startswith(kw) for kw in action_keywords):
            items.append(line)
    return items


def _generate_and_save_report(state, llm) -> "QualityReport":
    """调用质量报告生成器，带异常兜底"""
    from audiochat.workflow.quality_reporter import generate_quality_report
    try:
        report = generate_quality_report(state, llm)
        print(f"  [质量报告] 综合评分: {report.overall_score}/10  "
              f"建议: {'✅ PASS' if report.overall_pass else '⚠️ NEED_REVIEW'}")
        return report
    except Exception as exc:
        import sys
        print(f"  [质量报告] 生成失败: {exc}，跳过质量报告", file=sys.stderr)
        from audiochat.workflow.state import QualityReport
        return QualityReport(
            summary_score=0.0,
            action_item_score=0.0,
            issues=("质量报告生成失败，请人工审核",),
            warnings=(),
            overall_pass=False,
            overall_score=0.0,
            raw_output=f"生成失败: {exc}",
        )


def _print_task_header(state) -> None:
    print(f"\n{'='*60}")
    print(f"  任务已创建：task_id = {state.task_id}")
    print(f"  当前状态：{state.status.value}")
    print(f"  请使用 python scripts/task_cli.py approve {state.task_id} 确认发送")
    print(f"  或   python scripts/task_cli.py reject {state.task_id} <修改意见>")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline pipeline with HITL workflow support"
    )
    parser.add_argument("--audio", required=True, help="Input audio path")

    # 3D-Speaker
    parser.add_argument("--diar-device", default=None)
    parser.add_argument("--speaker-num", type=int, default=None)
    parser.add_argument("--model-cache-dir", default=None)
    parser.add_argument(
        "--speaker-embedding-model-path",
        default="/data/models/Voice/iic/speech_campplus_sv_zh-cn_16k-common/campplus_cn_en_common.pt",
    )
    parser.add_argument(
        "--vad-model-path",
        default="/data/models/Voice/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    )

    # FunASR
    parser.add_argument(
        "--funasr-model",
        default="/data/models/Voice/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    )
    parser.add_argument("--funasr-device", default="cuda:0")
    parser.add_argument(
        "--funasr-vad-model-path",
        default="/data/models/Voice/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    )
    parser.add_argument(
        "--funasr-punc-model-path",
        default="/data/models/Voice/iic/punc_ct-transformer_cn-en-common-vocab471067-large",
    )

    # Fun-Audio-Chat
    parser.add_argument(
        "--funaudiochat-model",
        default="~/llm/voice/Fun-Audio-Chat/pretrained_models/Fun-Audio-Chat-8B",
    )
    parser.add_argument("--llm-device", default=None)
    parser.add_argument("--instruction", default="请按 speaker 分组输出对话稿。")
    parser.add_argument("--max-lines", type=int, default=400)

    # 输出
    parser.add_argument("--output-dir", default="AudioChat_saves")

    # 模式
    parser.add_argument("--mode", default="summary", choices=["summary", "qa"])
    parser.add_argument("--query", default=None)

    # RAG
    parser.add_argument("--enable-rag", action="store_true")
    parser.add_argument("--hierarchical-rag", action="store_true")
    parser.add_argument("--agentic-rag", action="store_true", help="启用 Agentic RAG（信息缺口分析 + 批量检索）")
    parser.add_argument("--rag-storage-dir", default="./rag_storage")
    parser.add_argument("--meeting-id", default=None)
    parser.add_argument("--time-range", default=None, help="时间范围过滤，格式：2026-01-01,2026-06-01")

    # ASR 校正
    parser.add_argument("--enable-asr-correction", action="store_true")
    parser.add_argument("--corrector-model", default="Qwen/Qwen2.5-0.5B-Instruct")

    # 工作流
    parser.add_argument(
        "--workflow-store",
        default="./workflow_tasks",
        help="任务状态存储目录",
    )
    parser.add_argument(
        "--meeting-title",
        default=None,
        help="会议标题（用于邮件主题和 Gitea Issue 标题）",
    )
    parser.add_argument(
        "--meeting-date",
        default=None,
        help="会议日期（格式：YYYY-MM-DD）",
    )

    args = parser.parse_args()

    # ---------------------------------------------------------------------------
    # 初始化工作流
    # ---------------------------------------------------------------------------
    from audiochat.workflow import TaskState, TaskStore, TaskStatus
    from audiochat.workflow.state import Actor

    store = TaskStore(store_dir=args.workflow_store)

    meeting_title = args.meeting_title or Path(args.audio).stem
    state = TaskState.new(
        audio_path=args.audio,
        meeting_title=meeting_title,
        mode=args.mode,
    )
    state.meeting_date = args.meeting_date

    # 初始状态保存（持久化，从这一步开始支持断点恢复）
    store.save(state)
    print(f"[Workflow] 任务已创建：task_id={state.task_id}")

    # ---------------------------------------------------------------------------
    # 保存中间结果（diarization / ASR）
    # ---------------------------------------------------------------------------
    os.makedirs(args.output_dir, exist_ok=True)
    audio_basename = Path(args.audio).stem

    # Stage 1: ASR
    print("[Workflow] Stage 1/4: ASR 识别中...")
    utterances, raw_asr_items, diar_segments = stage_asr(args.audio, args, state, store)

    diarization_path = os.path.join(args.output_dir, f"{audio_basename}_diarization.json")
    asr_utterances_path = os.path.join(args.output_dir, f"{audio_basename}_asr_utterances.json")

    with open(diarization_path, "w", encoding="utf-8") as f:
        json.dump(
            {"audio": args.audio, "segments": [seg.__dict__ for seg in diar_segments]},
            f, ensure_ascii=False, indent=2,
        )
    with open(asr_utterances_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "audio": args.audio,
                "utterances": [u.__dict__ for u in utterances],
                "raw": raw_asr_items,
            },
            f, ensure_ascii=False, indent=2,
        )

    # Stage 2: ASR 校正（可选）
    print("[Workflow] Stage 2/4: ASR 后校正...")
    utterances = stage_correction(utterances, args, state, store)

    # Stage 3: RAG（可选）
    print("[Workflow] Stage 3/4: RAG 处理...")
    retriever = None
    meeting_id = None
    if args.enable_rag:
        retriever, meeting_id = stage_rag(utterances, args, state, store)

    # Stage 4: LLM 生成 + Stage 5: AI 质量报告（在 stage_llm 内部一次性完成）
    print("[Workflow] Stage 4/5: LLM 生成 + Stage 5/5: AI 质量评估...")
    llm_output = stage_llm(utterances, args, state, store, retriever=retriever)
    result_text = llm_output["text"]

    # 保存 LLM 回复
    llm_reply_path = os.path.join(args.output_dir, f"{audio_basename}_llm_reply.txt")
    with open(llm_reply_path, "w", encoding="utf-8") as f:
        f.write(result_text)
        f.write("\n")

    print(f"[Workflow] 完整流程结束，任务 {state.task_id} 已进入待审核状态。")
    print(f"  查看任务列表：python scripts/task_cli.py list")


if __name__ == "__main__":
    main()
