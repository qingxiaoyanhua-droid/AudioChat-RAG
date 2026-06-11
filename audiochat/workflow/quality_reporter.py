"""
AI 质量报告生成器

在 LLM 生成总结后，由 AI 驱动的质量评估，对总结和行动项进行多维度评分。
人的角色从"评估者"变成"决策者"——AI 做分析，人只看报告做判断。

RAGVUE 增强（v2）：
  - 新增严格忠实度检查（RAGVUE 2026 EACL Demo）
  - 原子断言分解 + 逐条验证 + 关键实体/时间严格匹配
  - 严重问题时自动降级 overall_pass，引导人工重点审核

Usage:
    from audiochat.workflow.quality_reporter import generate_quality_report
    report = generate_quality_report(task_state, llm, utterances=utterances)
"""

from __future__ import annotations

import re
from typing import Optional

from audiochat.workflow.state import TaskState, QualityReport


QUALITY_PROMPT_TEMPLATE = """你是一个专业的会议内容质量审核官。请对以下AI生成的会议总结进行严格的质量评估。

## 会议总结
{summary}

## 行动项
{action_items}

## 评分维度（每项 0-10 分，保留一位小数）

1. **总结完整性**（0-10）：是否覆盖了会议讨论的所有关键话题？有无重大遗漏？
2. **总结准确性**（0-10）：内容是否忠实于原始讨论？是否有臆测或错误？
3. **行动项可操作性**（0-10）：每个行动项是否具体、可执行、有人负责、有明确截止时间？
4. **格式规范性**（0-10）：结构是否清晰？层次是否分明？语言是否通顺？

## 输出格式（严格按以下格式输出，不要有任何其他内容）

[质量分-总结完整性] {X}/10
[质量分-总结准确性] {X}/10
[质量分-行动项可操作性] {X}/10
[质量分-格式规范性] {X}/10
[综合评分] {X}/10
[发现的问题]
- 问题1：...
- 问题2：（如果没有问题，写"无"）
[警告项]
- 警告1：...（格式不规范、行动项不够具体等；如无，写"无"）
[建议] PASS / NEED_REVIEW

判断标准：
- 综合评分 >= 7.0 且 无严重问题 → PASS（可直接确认）
- 综合评分 < 7.0 或 存在严重问题 → NEED_REVIEW（需重点审核）

严重问题包括但不限于：
- 行动项为空或极少（<2条有效行动项）
- 总结内容明显与会议无关
- 行动项无具体负责人或时间节点"""


def _parse_score_line(line: str, key: str) -> Optional[float]:
    """从一行中提取分数，例如 "[质量分-总结完整性] 7.5/10" -> 7.5"""
    match = re.search(rf"\[{re.escape(key)}\]\s*([\d.]+)", line)
    if match:
        return float(match.group(1))
    return None


def _parse_lines(raw: str) -> dict:
    """解析 AI 原始输出，提取各字段"""
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]

    result = {
        "summary_completeness": None,
        "summary_accuracy": None,
        "action_item_quality": None,
        "format_quality": None,
        "overall_score": None,
        "issues": [],
        "warnings": [],
        "overall_pass": False,
    }

    section = None  # None | "issues" | "warnings"

    for line in lines:
        if "[质量分-总结完整性]" in line:
            result["summary_completeness"] = _parse_score_line(line, "质量分-总结完整性")
        elif "[质量分-总结准确性]" in line:
            result["summary_accuracy"] = _parse_score_line(line, "质量分-总结准确性")
        elif "[质量分-行动项可操作性]" in line:
            result["action_item_quality"] = _parse_score_line(line, "质量分-行动项可操作性")
        elif "[质量分-格式规范性]" in line:
            result["format_quality"] = _parse_score_line(line, "质量分-格式规范性")
        elif "[综合评分]" in line:
            result["overall_score"] = _parse_score_line(line, "综合评分")
        elif "[发现的问题]" in line:
            section = "issues"
            continue
        elif "[警告项]" in line:
            section = "warnings"
            continue
        elif "[建议]" in line:
            result["overall_pass"] = "PASS" in line.upper()
        elif line.startswith("- "):
            content = line[2:]
            if "无" not in content:
                if section == "issues":
                    result["issues"].append(content)
                elif section == "warnings":
                    result["warnings"].append(content)

    return result


def _format_utterances_for_context(utterances: list) -> str:
    """将 utterances 列表格式化为纯文本上下文（用于忠实度检查）"""
    from audiochat.prompting import format_utterances
    return format_utterances(utterances, max_lines=500)


def check_faithfulness(
    summary: str,
    utterances: list,
    retrieved_contexts: Optional[list[str]] = None,
    llm=None,
) -> dict:
    """
    RAGVUE 风格的严格忠实度检查

    Args:
        summary: LLM 生成的总结文本
        utterances: 当前会议的 ASR utterances（作为源上下文）
        retrieved_contexts: RAG 检索到的历史上下文（可选）
        llm: LLM 实例（需有 generate_text 方法）

    Returns:
        dict：包含 faithfulness_result, issues, overall_pass
    """
    from audiochat.rag.faithfulness import FaithfulnessChecker

    checker = FaithfulnessChecker(llm=llm)
    source_context = _format_utterances_for_context(utterances)

    try:
        result = checker.check(
            generated=summary,
            source_context=source_context,
            retrieved_contexts=retrieved_contexts,
        )
        return {
            "faithfulness_result": result,
            "faithfulness_score": result.score,
            "faithfulness_total_claims": result.total_claims,
            "faithfulness_supported": result.supported,
            "faithfulness_hallucinated": result.partially_hallucinated + result.fully_hallucinated,
            "faithfulness_critical": tuple(result.critical_issues),
            "faithfulness_raw": result.raw_llm_output,
        }
    except Exception as exc:
        return {
            "faithfulness_result": None,
            "faithfulness_score": 0.0,
            "faithfulness_total_claims": 0,
            "faithfulness_supported": 0,
            "faithfulness_hallucinated": 0,
            "faithfulness_critical": (f"忠实度检查执行失败: {exc}",),
            "faithfulness_raw": str(exc),
        }


def generate_quality_report(
    state: TaskState,
    llm,
    *,
    utterances: Optional[list] = None,
    retrieved_contexts: Optional[list[str]] = None,
    summary_field: str = "summary",
    action_items_field: str = "action_items",
    enable_faithfulness: bool = True,
) -> QualityReport:
    """
    给定任务状态和 LLM，生成质量报告。

    Args:
        state: 任务状态，其中 summary 和 action_items 已由 LLM 生成
        llm: LLM 实例（如 FunAudioChatLLM），需要有 generate_text 方法
        utterances: ASR utterances 列表（用于忠实度检查，enable_faithfulness=True 时必须提供）
        retrieved_contexts: RAG 检索到的历史上下文（可选）
        summary_field: 用 state 的哪个字段作为总结文本
        action_items_field: 用 state 的哪个字段作为行动项列表
        enable_faithfulness: 是否启用 RAGVUE 忠实度检查

    Returns:
        QualityReport: AI 质量报告

    Raises:
        ValueError: 任务状态中没有 summary 或 action_items
    """
    summary = getattr(state, summary_field, "") or ""
    action_items = getattr(state, action_items_field, []) or []

    if not summary:
        raise ValueError(f"任务 {state.task_id} 缺少 summary 字段，无法生成质量报告")

    # ------------------------------------------------------------------
    # Step 1: 通用质量评分（原有逻辑）
    # ------------------------------------------------------------------
    action_items_text = (
        "\n".join([f"{i+1}. {item}" for i, item in enumerate(action_items)])
        if action_items
        else "（无行动项）"
    )

    prompt = QUALITY_PROMPT_TEMPLATE.format(
        summary=summary,
        action_items=action_items_text,
    )

    response = llm.generate_text(instruction=prompt)
    raw_output = response.text if hasattr(response, "text") else str(response)

    parsed = _parse_lines(raw_output)

    summary_score = _weighted_summary_score(parsed)
    action_item_score = parsed["action_item_quality"] or 0.0
    overall_score = parsed["overall_score"] or round((summary_score + action_item_score) / 2, 1)

    issues = []
    if not action_items or len(action_items) < 2:
        issues.append("行动项过少（少于2条），建议人工重点审核")
    for issue in parsed.get("issues", []):
        if "无" not in issue:
            issues.append(issue)

    warnings = [w for w in parsed.get("warnings", []) if "无" not in w]

    # ------------------------------------------------------------------
    # Step 2: RAGVUE 忠实度检查（新增）
    # ------------------------------------------------------------------
    faithfulness_data = {
        "faithfulness_score": 0.0,
        "faithfulness_total_claims": 0,
        "faithfulness_supported": 0,
        "faithfulness_hallucinated": 0,
        "faithfulness_critical": (),
        "faithfulness_raw": "",
    }

    if enable_faithfulness and utterances:
        print("[质量报告] RAGVUE 忠实度检查中...")
        faithfulness_data = check_faithfulness(
            summary=summary,
            utterances=utterances,
            retrieved_contexts=retrieved_contexts,
            llm=llm,
        )
        if faithfulness_data["faithfulness_total_claims"] > 0:
            fs = faithfulness_data["faithfulness_score"]
            total = faithfulness_data["faithfulness_total_claims"]
            supported = faithfulness_data["faithfulness_supported"]
            hallucinated = faithfulness_data["faithfulness_hallucinated"]
            print(f"  [忠实度] 得分 {fs:.1%}（{supported}/{total} supported, "
                  f"{hallucinated} hallucinated）")

            # 严重问题加入 issues
            for crit in faithfulness_data["faithfulness_critical"]:
                issues.append(f"【忠实度严重】{crit}")
        elif faithfulness_data["faithfulness_raw"]:
            print(f"  [忠实度] 检查失败: {faithfulness_data['faithfulness_raw'][:80]}")

    # ------------------------------------------------------------------
    # Step 3: 综合判断（忠实度加权影响最终结论）
    # ------------------------------------------------------------------
    # 忠实度低于 60% → 强制要求人工审核（即使其他分高）
    faith_ok = (
        faithfulness_data["faithfulness_score"] >= 0.6
        or faithfulness_data["faithfulness_total_claims"] == 0
    )

    overall_pass = (
        overall_score >= 7.0
        and len(issues) == 0
        and parsed.get("overall_pass", False)
        and faith_ok
    )

    # 忠实度差但综合分高 → 发警告
    if not faith_ok and overall_score >= 7.0:
        warnings.append(
            f"⚠️ 忠实度偏低（{faithfulness_data['faithfulness_score']:.0%}），"
            "建议重点审核内容准确性"
        )

    return QualityReport(
        summary_score=summary_score,
        action_item_score=action_item_score,
        issues=tuple(issues),
        warnings=tuple(warnings),
        overall_pass=overall_pass,
        overall_score=overall_score,
        raw_output=raw_output,
        # RAGVUE 忠实度字段
        faithfulness_score=faithfulness_data["faithfulness_score"],
        faithfulness_total_claims=faithfulness_data["faithfulness_total_claims"],
        faithfulness_supported=faithfulness_data["faithfulness_supported"],
        faithfulness_hallucinated=faithfulness_data["faithfulness_hallucinated"],
        faithfulness_critical=faithfulness_data["faithfulness_critical"],
        faithfulness_raw=faithfulness_data["faithfulness_raw"],
    )


def _weighted_summary_score(parsed: dict) -> float:
    """
    总结质量 = 完整性(40%) + 准确性(40%) + 格式(20%)
    """
    completeness = parsed.get("summary_completeness") or 0.0
    accuracy = parsed.get("summary_accuracy") or 0.0
    format_q = parsed.get("format_quality") or 0.0

    return round(completeness * 0.4 + accuracy * 0.4 + format_q * 0.2, 1)
