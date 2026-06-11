"""
提示词（Prompting）模块

提供 ASR 结果格式化和 LLM 指令构建功能。
将 Utterance 列表转换为易于 LLM 理解的格式。

支持两种模式：
  - 总结模式（默认）：用户不指定 query，生成结构化会议纪要
  - 问答模式：用户指定 query，系统结合 RAG 检索回答用户问题
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from functools import lru_cache

from audiochat.asr.funasr_asr import Utterance


# ============================================================================
# 意图识别
# ============================================================================

class QueryIntent(str, Enum):
    """用户查询意图"""
    CURRENT_MEETING = "current_meeting"  # 问当前会议内容
    CROSS_MEETING = "cross_meeting"       # 查历史会议信息
    UNKNOWN = "unknown"                     # 无法判断（走默认 RAG）


# 规则模式匹配（零延迟兜底）
_CURRENT_MEETING_PATTERNS = [
    "这场会议", "这个音频", "刚才", "这段", "本次会议",
    "今天会上", "刚才说", "这段音频", "这段对话", "这个会议",
]

_CROSS_MEETING_PATTERNS = [
    "上次", "上次会议", "上周", "之前", "之前会议",
    "之前说过", "之前提到", "之前讨论", "什么时候", "之前决定",
    "之前说", "历史", "更早", "以前",
]


def _rule_based_intent(question: str) -> Optional[QueryIntent]:
    """规则快速判断（零延迟）"""
    for pat in _CURRENT_MEETING_PATTERNS:
        if pat in question:
            return QueryIntent.CURRENT_MEETING
    for pat in _CROSS_MEETING_PATTERNS:
        if pat in question:
            return QueryIntent.CROSS_MEETING
    return None


@lru_cache(maxsize=128)
def _cached_model_intent(question: str) -> str:
    """小模型意图分类（带 LRU 缓存，避免重复推理）"""
    prompt = (
        "判断用户问题属于哪个类别：\n"
        "- current_meeting：问当前会议/音频/录音的内容（如'这场会张三说了什么'、'第三个议题的结论'）\n"
        "- cross_meeting：问历史会议、过去的信息（如'上周任务进度'、'李四之前提到的方案'）\n"
        f"用户问题：\"{question}\"\n"
        "回答（仅一个词）："
    )
    try:
        from transformers import pipeline
        classifier = pipeline(
            "text-classification",
            model="Qwen/Qwen2.5-0.5B-Instruct",
            device_map="auto",
        )
        result = classifier(prompt, max_new_tokens=8)[0]
        label = result["label"].lower()
        if "current" in label:
            return "current_meeting"
        elif "cross" in label:
            return "cross_meeting"
        return "unknown"
    except Exception:
        return "unknown"


def classify_intent(question: str) -> QueryIntent:
    """
    意图分类：规则兜底 + 小模型精判

    策略：
      - 规则命中直接返回（~0ms）
      - 规则未命中，走小模型（~200ms，仅首次推理有开销）
      - 小模型不可用时，返回 UNKNOWN（走默认 RAG）

    Args:
        question: 用户原始问题

    Returns:
        QueryIntent 枚举
    """
    rule_result = _rule_based_intent(question)
    if rule_result is not None:
        return rule_result

    model_result = _cached_model_intent(question)
    if model_result == "current_meeting":
        return QueryIntent.CURRENT_MEETING
    elif model_result == "cross_meeting":
        return QueryIntent.CROSS_MEETING
    return QueryIntent.UNKNOWN


@dataclass
class IntentRoutingResult:
    """意图路由结果"""
    intent: QueryIntent
    confidence: float  # 0.0-1.0
    reasoning: str     # 判断理由（用于调试）


def route_and_retrieve(
    question: str,
    retriever,
    current_meeting_id: Optional[str] = None,
    current_transcription: Optional[str] = None,
    time_range: Optional[tuple[str, str]] = None,
) -> tuple[list, IntentRoutingResult]:
    """
    意图识别 + 检索执行

    流程：
      1. 意图分类（规则 + 小模型）
      2. 根据意图选择检索策略
      3. 执行检索返回上下文

    Args:
        question: 用户问题
        retriever: AudioChatRetriever 实例
        current_meeting_id: 当前会议 ID（用于"当前会议"意图）
        current_transcription: 当前会议转写（用于"当前会议"意图）
        time_range: 时间范围（用于"历史会议"意图）

    Returns:
        (contexts, routing_result)
    """
    intent = classify_intent(question)

    if intent == QueryIntent.CURRENT_MEETING:
        routing = IntentRoutingResult(
            intent=intent,
            confidence=0.95,
            reasoning="规则命中：含'这场会议'/'刚才'等词",
        )
        # 当前会议：只搜当前会议内部
        contexts = retriever.retrieve(
            question,
            k=3,
            meeting_id_filter=current_meeting_id,
        )
        return contexts, routing

    elif intent == QueryIntent.CROSS_MEETING:
        routing = IntentRoutingResult(
            intent=intent,
            confidence=0.90,
            reasoning="规则命中：含'上次'/'之前'/'上周'等词",
        )
        # 历史会议：用时间范围过滤
        contexts = retriever.retrieve(
            question,
            k=5,
            time_range=time_range,
        )
        return contexts, routing

    else:
        routing = IntentRoutingResult(
            intent=QueryIntent.UNKNOWN,
            confidence=0.5,
            reasoning="规则未命中，小模型分类为 unknown，走默认 RAG",
        )
        # 默认：全量检索（不过滤）
        contexts = retriever.retrieve(question, k=3)
        return contexts, routing


# ============================================================================
# Agentic RAG — 信息缺口分析 + 批量检索 + 上下文压缩
# ============================================================================

from dataclasses import dataclass, field
import json
import concurrent.futures


@dataclass
class InfoGap:
    """单个信息缺口"""
    info_type: str  # "task_progress" | "person_opinion" | "decision_history" | "requirement_evolution"
    topic: str      # 缺口主题描述
    sub_query: str  # 生成的子查询


@dataclass
class PlanningResult:
    """Agentic Planning 结果"""
    gaps: list[InfoGap]  # 识别出的信息缺口列表
    contexts: list      # 所有子查询的检索结果合并
    compressed_context: str  # 压缩后的上下文字符串
    planning_tokens: int  # planning 阶段消耗的 token（估算）


def analyze_info_gaps(
    transcription: str,
    meeting_id: str,
    max_gaps: int = 5,
) -> list[InfoGap]:
    """
    LLM 分析转写，识别需要补充的历史信息缺口（子查询生成）

    流程：
      1. LLM 读转写摘要，分析每段讨论是否需要历史背景
      2. 输出结构化 JSON：{info_type, topic, sub_query}
      3. 解析 JSON，构造 InfoGap 对象列表

    Args:
        transcription: 会议转写文本（可以是摘要版，节省 token）
        meeting_id: 当前会议 ID
        max_gaps: 最多生成多少个信息缺口（防止过多子查询）

    Returns:
        InfoGap 列表（空列表表示不需要补充历史信息）
    """
    prompt = f"""会议转写摘要：
{transcription}

当前会议 ID：{meeting_id}

分析这场会议的讨论内容，判断每个议题是否需要补充历史信息。
例如：
- 讨论"X模块开发" → 需要查"X模块上周进度"（task_progress）
- 讨论"Y需求方案" → 需要查"李四对Y需求的看法"（person_opinion）
- 做出某项决定 → 需要查"该决定的形成过程"（decision_history）

输出格式（必须严格是有效 JSON）：
{{
  "gaps": [
    {{"info_type": "task_progress", "topic": "X模块开发进度", "sub_query": "X模块上周的开发进度和遇到的问题"}},
    {{"info_type": "person_opinion", "topic": "李四对Y需求的看法", "sub_query": "李四之前对Y需求发表过什么意见"}}
  ]
}}

如果这场会议不需要补充任何历史信息（所有讨论都围绕当前会议内容），返回：
{{"gaps": []}}

规则：
- sub_query 必须是可以直接用于向量检索的自然语言问题
- 最多生成 {max_gaps} 个缺口
- 仅分析需要跨会议查询的内容，当前会议内部的信息不需要
回答（仅 JSON）："""

    try:
        from transformers import pipeline
        generator = pipeline(
            "text-generation",
            model="Qwen/Qwen2.5-0.5B-Instruct",
            device_map="auto",
            max_new_tokens=256,
        )
        output = generator(prompt, do_sample=False)[0]["generated_text"]
        # 提取 JSON 部分
        json_start = output.rfind("{")
        json_end = output.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            return []
        raw_json = output[json_start:json_end]
        data = json.loads(raw_json)
        gaps_raw = data.get("gaps", [])
        return [
            InfoGap(
                info_type=g.get("info_type", "unknown"),
                topic=g.get("topic", ""),
                sub_query=g.get("sub_query", ""),
            )
            for g in gaps_raw[:max_gaps]
            if g.get("sub_query")
        ]
    except Exception:
        return []


def _execute_single_query(
    retriever,
    gap: InfoGap,
    time_range: Optional[tuple[str, str]] = None,
) -> tuple[InfoGap, list]:
    """执行单个子查询（供并发调用）"""
    try:
        contexts = retriever.retrieve(gap.sub_query, k=3, time_range=time_range)
        return (gap, contexts)
    except Exception:
        return (gap, [])


def plan_and_retrieve(
    transcription: str,
    retriever,
    meeting_id: str,
    time_range: Optional[tuple[str, str]] = None,
    max_workers: int = 4,
) -> PlanningResult:
    """
    Agentic Planning 主流程：分析缺口 → 批量检索 → 压缩上下文

    流程：
      1. LLM 分析转写，识别信息缺口（生成子查询列表）
      2. 并发执行所有子查询（max_workers 个线程）
      3. 合并去重检索结果
      4. 上下文压缩（控制 token 数）

    Args:
        transcription: 会议转写文本
        retriever: AudioChatRetriever 实例
        meeting_id: 当前会议 ID
        time_range: 时间范围过滤
        max_workers: 并发检索线程数

    Returns:
        PlanningResult（含缺口列表、检索结果、压缩上下文）
    """
    # Step 1: 信息缺口分析（LLM 调用 1）
    gaps = analyze_info_gaps(transcription, meeting_id)

    if not gaps:
        return PlanningResult(
            gaps=[],
            contexts=[],
            compressed_context="",
            planning_tokens=0,
        )

    # Step 2: 并发批量检索
    all_contexts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_execute_single_query, retriever, gap, time_range)
            for gap in gaps
        ]
        for future in concurrent.futures.as_completed(futures):
            gap, ctxs = future.result()
            all_contexts.extend(ctxs)

    # Step 3: 去重（按 content 哈希）
    seen: set[str] = set()
    unique_contexts = []
    for ctx in all_contexts:
        content_hash = hash(ctx.content)
        if content_hash not in seen:
            seen.add(content_hash)
            unique_contexts.append(ctx)

    # Step 4: 上下文压缩
    compressed = compress_contexts(unique_contexts, max_tokens=2000)

    # 估算 planning 阶段 token（LLM 输出约 200 token + 并发检索开销）
    planning_tokens = 200 + len(gaps) * 50

    return PlanningResult(
        gaps=gaps,
        contexts=unique_contexts,
        compressed_context=compressed,
        planning_tokens=planning_tokens,
    )


def compress_contexts(contexts: list, max_tokens: int = 2000) -> str:
    """
    上下文压缩：将多个检索结果合并，控制在 max_tokens 以内

    策略：
      1. 按 relevance_score 排序
      2. 优先保留高分的 L2/L3 层内容（来自 HierarchicalRetriever）
      3. 超出 token 限制时，从低分开始截断

    Args:
        contexts: RetrievedContext 或 HierarchicalRetrievedContext 列表
        max_tokens: 最大 token 数（按中文字符估算，1 token ≈ 1.5 字符）
    """
    if not contexts:
        return ""

    # 排序：优先高 relevance_score
    sorted_contexts = sorted(contexts, key=lambda c: c.relevance_score, reverse=True)

    # 检查是否有 layer 字段（HierarchicalRetrievedContext）
    def get_layer(ctx) -> int:
        if hasattr(ctx, "layer"):
            layer_order = {"L3": 0, "L2": 1, "FALLBACK": 2, "L1": 3, "RAW": 4}
            return layer_order.get(getattr(ctx, "layer", ""), 99)
        return 99

    sorted_contexts.sort(key=get_layer)

    result_parts: list[str] = []
    current_len = 0
    max_chars = int(max_tokens * 1.5)

    for ctx in sorted_contexts:
        layer_tag = ""
        if hasattr(ctx, "layer") and ctx.layer:
            layer_tag = f"[{ctx.layer}] "

        entry = f"{layer_tag}{ctx.content}"
        entry_len = len(entry)

        if current_len + entry_len > max_chars:
            remaining = max_chars - current_len
            if remaining > 50:
                result_parts.append(entry[:remaining] + "...")
                current_len = max_chars
            break

        result_parts.append(entry)
        current_len += entry_len

    return "\n---\n".join(result_parts)


# ============================================================================
# ASR 结果格式化
# ============================================================================


def format_ms(ms: int) -> str:
    """将毫秒转换为 MM:SS.mmm 格式。"""
    if ms < 0:
        ms = 0
    seconds = ms / 1000.0
    minutes = int(seconds // 60)
    seconds = seconds - minutes * 60
    return f"{minutes:02d}:{seconds:06.3f}"


def format_utterances(utterances: list[Utterance], *, max_lines: int = 400) -> str:
    """将 Utterance 列表格式化为易读的文本。

    格式示例：
        [00:01.234-00:05.678] spk0: 你好
        [00:05.890-00:08.123] spk1: 大家好

    Args:
        utterances: Utterance 列表
        max_lines: 最大输出行数

    Returns:
        格式化后的文本字符串
    """
    lines: list[str] = []
    for u in utterances[:max_lines]:
        lines.append(
            f"[{format_ms(u.start_ms)}-{format_ms(u.end_ms)}] {u.speaker}: {u.text}".strip()
        )
    if len(utterances) > max_lines:
        lines.append(f"... (truncated, total_lines={len(utterances)})")
    return "\n".join(lines)


def extract_query_from_utterances(utterances: list[Utterance]) -> str:
    """从对话中提取检索查询（取最后一条 utterance）"""
    if utterances:
        return utterances[-1].text
    return ""


@dataclass
class LLMInstruction:
    """LLM 指令结构（支持问答模式和总结模式）"""
    prompt: str
    mode: str  # "qa" 或 "summary"
    contexts_used: bool  # 是否使用了 RAG 上下文


def build_agentic_instruction(
    utterances: list[Utterance],
    user_instruction: str,
    retriever,
    meeting_id: str,
    time_range: Optional[tuple[str, str]] = None,
    max_lines: int = 400,
) -> tuple[str, PlanningResult]:
    """
    Agentic RAG 指令构建（用于总结模式）

    流程：
      1. 格式转写文本
      2. Agentic Planning：分析信息缺口 → 批量检索 → 上下文压缩
      3. 构建含压缩上下文的 prompt

    Args:
        utterances: ASR 转写列表
        user_instruction: 用户指令（总结 prompt）
        retriever: AudioChatRetriever 实例
        meeting_id: 当前会议 ID
        time_range: 时间范围过滤
        max_lines: 最大保留行数

    Returns:
        (prompt字符串, PlanningResult)
    """
    diar_text = format_utterances(utterances, max_lines=max_lines)

    # Agentic Planning：分析缺口 → 批量检索 → 压缩
    planning_result = plan_and_retrieve(
        transcription=diar_text,
        retriever=retriever,
        meeting_id=meeting_id,
        time_range=time_range,
    )

    base_prompt = (
        "以下是对输入音频的分角色转写结果（speaker diarization + ASR，供你参考）：\n"
        f"{diar_text}\n\n"
    )

    # 如果有 Agentic RAG 上下文，加入 prompt
    if planning_result.compressed_context:
        base_prompt += (
            f"【相关历史背景（来自 Agentic RAG）】\n"
            f"{planning_result.compressed_context}\n\n"
        )

    base_prompt += (
        f"请基于以上分角色转写结果{'和历史背景' if planning_result.compressed_context else ''}，\n"
        f"完成用户要求：\n"
        f"{user_instruction}\n"
    )

    return base_prompt, planning_result


def build_llm_instruction(
    *,
    utterances: list[Utterance],
    user_instruction: str,
    max_lines: int = 400,
    retriever=None,
    enable_rag: bool = False,
    mode: str = "summary",
    meeting_id: Optional[str] = None,
    time_range: Optional[tuple[str, str]] = None,
) -> tuple[str, list]:
    """
    构建发送给 LLM 的完整指令。

    将 ASR 转写结果与用户指令整合，生成 LLM 可理解的完整提示词。

    新增参数（Phase 2 意图识别）：
      - meeting_id: 当前会议 ID，用于"当前会议"意图时精确过滤
      - time_range: 时间范围 tuple(start_iso, end_iso)，用于"历史会议"意图

    Returns:
        tuple[str, list]: (完整的LLM指令字符串, RAG检索结果列表，无RAG时为空列表)
    """
    diar_text = format_utterances(utterances, max_lines=max_lines)

    base_prompt = (
        "以下是对输入音频的分角色转写结果（speaker diarization + ASR，供你参考）：\n"
        f"{diar_text}\n\n"
    )

    # RAG 检索（如果启用）
    rag_used = False
    contexts = []
    if enable_rag and retriever is not None:
        if mode == "qa":
            # ===== QA 模式：意图识别 + 路由检索 =====
            # 规则兜底 + 小模型精判，根据意图选择不同的检索策略
            contexts, routing = route_and_retrieve(
                question=user_instruction,
                retriever=retriever,
                current_meeting_id=meeting_id,
                current_transcription=diar_text,
                time_range=time_range,
            )
            query = user_instruction
            if contexts:
                rag_prompt = retriever.build_rag_prompt(query, contexts)
                base_prompt += f"{rag_prompt}\n\n"
                rag_used = True
        else:
            # ===== 总结模式：取最后一句做检索（无意图识别）=====
            query = extract_query_from_utterances(utterances)
            contexts = retriever.retrieve(query, k=3)
            if contexts:
                rag_prompt = retriever.build_rag_prompt(query, contexts)
                base_prompt += f"{rag_prompt}\n\n"
                rag_used = True

    # 根据模式构建最终指令
    if mode == "qa":
        # ===== 问答模式 =====
        # 用户指定了具体问题（如"第三个发言人说了什么？"）
        base_prompt += (
            "请根据以上分角色转写结果，准确回答用户的问题。\n"
            f"用户问题：{user_instruction}\n\n"
            "要求：\n"
            "1. 直接给出答案，不要重复问题\n"
            "2. 如涉及时间或说话人，请引用具体内容（如 spk1 在 00:05 说的...）\n"
            "3. 如果无法从内容中确定答案，请明确说明\n"
            "请回答："
        )
    else:
        # ===== 总结模式（默认）=====
        # 用户不指定 query，生成结构化会议纪要
        base_prompt += (
            "请基于以上分角色转写结果完成用户要求。\n"
            f"用户要求：{user_instruction}"
        )

    return (base_prompt, contexts)


# 默认的总结 prompt（结构化会议纪要模板）
DEFAULT_SUMMARY_INSTRUCTION = """请生成结构化的会议纪要，包含以下部分：
1. 参会人员：列出所有发言人
2. 主要议题：总结讨论的核心主题
3. 进度汇报：各发言人报告的进展
4. 行动项：明确的任务、负责人和截止时间（如有）
5. 决策项：会议中达成的结论或决定（如有）

格式要求：
- 使用 Markdown 格式
- 结构清晰，层次分明
- 保留关键信息，精炼表达"""
