"""
RAGVUE 风格的严格忠实度检查器

论文：RAGVUE — EACL 2026 Demo
  https://aclanthology.org/2026.eacl-demo.35.pdf

核心思想：
  1. 将 LLM 生成的回答分解为原子断言（atomic claims）
  2. 对每个断言，判断其在原始上下文中是否可找到
  3. 对关键实体（人名/地点/组织）和时间表达（年份/日期）强制精确匹配
  4. 三档判断：supported / partially_hallucinated / fully_hallucinated

使用方式：
    from audiochat.rag.faithfulness import FaithfulnessChecker, FaithfulnessResult
    result = checker.check(
        generated=summary_text,
        source_context=transcript_text,
        retrieved_contexts=[ctx.content for ctx in contexts]  # 可选
    )
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class Claim:
    """单个原子断言"""
    text: str               # 断言原文
    is_key_entity: bool     # 是否含有关键实体（人名/时间/数字）
    is_temporal: bool       # 是否含时间表达
    verdict: str            # supported / partially_hallucinated / fully_hallucinated
    reason: str             # 判断理由
    evidence: Optional[str]  # 支持证据（从上下文中找到的相关句子）


@dataclass
class FaithfulnessResult:
    """
    忠实度检查结果

    评分逻辑（RAGVUE 风格）：
      - 每个 supported 断言 = +1 分
      - 每个 partially_hallucinated = +0 分（警告）
      - 每个 fully_hallucinated = -1 分（严重问题）
      - score = (supported - fully_hallucinated) / total
      - 当含关键实体/时间表达的断言为 hallucinated 时，提升问题等级
    """
    total_claims: int
    supported: int
    partially_hallucinated: int
    fully_hallucinated: int
    score: float           # 0.0 ~ 1.0，越高越好
    claims: list[Claim]
    warnings: list[str]    # 需要人工审核的警告（如关键实体被 hallucinated）
    critical_issues: list[str]  # 严重问题（直接导致 score 大幅下降）
    raw_llm_output: str   # LLM 原始输出（便于调试）

    def to_dict(self) -> dict:
        return {
            "total_claims": self.total_claims,
            "supported": self.supported,
            "partially_hallucinated": self.partially_hallucinated,
            "fully_hallucinated": self.fully_hallucinated,
            "score": round(self.score, 3),
            "warnings": self.warnings,
            "critical_issues": self.critical_issues,
            "claims": [
                {
                    "text": c.text,
                    "verdict": c.verdict,
                    "reason": c.reason,
                    "is_key_entity": c.is_key_entity,
                    "is_temporal": c.is_temporal,
                }
                for c in self.claims
            ],
        }

    def display_summary(self) -> str:
        """人类可读的忠实度摘要"""
        lines = [
            f"  忠实度得分   : {self.score:.1%}",
            f"  断言总数     : {self.total_claims} 条",
            f"    ✅ supported        : {self.supported} 条",
            f"    ⚠️ partially       : {self.partially_hallucinated} 条",
            f"    ❌ hallucinated    : {self.fully_hallucinated} 条",
        ]
        if self.critical_issues:
            lines.append("  严重问题     :")
            for issue in self.critical_issues:
                lines.append(f"    - {issue}")
        if self.warnings:
            lines.append("  警告项       :")
            for w in self.warnings:
                lines.append(f"    - {w}")
        return "\n".join(lines)


# ============================================================================
# 核心检查器
# ============================================================================

class FaithfulnessChecker:
    """
    RAGVUE 风格严格忠实度检查器

    实现要点：
      - 单次 LLM call 完成断言分解 + 逐条判断（RAGVUE 原文设计）
      - 严格检查关键实体（人名/地点/组织）和时间表达
      - 支持有 RAG 上下文（retrieved_contexts）和无 RAG 上下文（仅 source_context）两种模式
      - 发现严重问题时（如关键实体被 hallucinated），提升问题等级
    """

    # 时间表达式正则（用于识别含时间的关键断言）
    _TEMPORAL_PATTERNS = [
        r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}",  # 2026-06-03 / 2026年6月
        r"\d{4}年",                             # 2026年
        r"\d+天", r"\d+周", r"\d+月", r"\d+年",  # 3天 / 上周 / 三个月 / 两年
        r"今天|明天|昨天|上周|下周|本月|本月",
        r"周一|周二|周三|周四|周五|周六|周日",
        r"\d{1,2}点\d{0,2}分",                  # 14:30
    ]

    # 数字/金额正则（用于识别含关键数值的断言）
    _NUMBER_PATTERNS = [
        r"\d+%", r"百分之\d+",     # 百分比
        r"\d+\.\d+[万/亿/千]",     # 金额 3.5万
        r"第[一二三四五六七八九十\d]+",  # 第一 / 第3
        r"\d+个", r"\d+条",        # 数量
    ]

    def __init__(self, llm=None):
        """
        Args:
            llm: LLM 实例（需要有 generate_text 方法），
                 不传则内部懒加载 transformers pipeline
        """
        self._llm = llm

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def check(
        self,
        generated: str,
        source_context: str,
        retrieved_contexts: Optional[list[str]] = None,
    ) -> FaithfulnessResult:
        """
        检查生成文本的忠实度

        Args:
            generated: LLM 生成的文本（会议总结/回答）
            source_context: 原始上下文（当前会议的 ASR 转写）
            retrieved_contexts: 检索到的历史上下文（RAG 增强时使用）

        Returns:
            FaithfulnessResult: 包含断言列表、得分、警告和严重问题
        """
        combined_context = self._build_context(source_context, retrieved_contexts)

        # Step 1: 分解断言
        claims_text = self._decompose_claims(generated)

        # Step 2: 逐条判断（单次 LLM call）
        verdicts_text = self._judge_claims(claims_text, combined_context, generated)

        # Step 3: 解析结果       
        return self._parse_verdicts(
            verdicts_text=verdicts_text,
            claims_text=claims_text,
            generated=generated,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _build_context(
        self, source_context: str, retrieved_contexts: Optional[list[str]]
    ) -> str:
        """拼接检索上下文，RAG 增强的上下文放在前面（最新最相关的先看）"""
        parts = []
        if retrieved_contexts:
            parts.append("【检索到的相关背景】")
            for i, ctx in enumerate(retrieved_contexts, 1):
                parts.append(f"[{i}] {ctx}")
            parts.append("")
        parts.append("【当前会议转写】")
        parts.append(source_context)
        return "\n".join(parts)

    def _decompose_claims(self, text: str) -> str:
        """
        将生成文本分解为原子断言（atomic claims）
        每个断言应该是一个完整的、独立的、可验证的陈述句。
        """
        prompt = f"""请将以下会议总结分解为原子断言（atomic claims）。
每条断言必须：
1. 是一个完整的陈述句
2. 只包含一个核心事实
3. 可以用"是/否"来判断对错

示例：
原文：张三和李四在6月3日的会议上决定采用微服务架构，预计3个月内完成。
分解：
1. 张三在6月3日的会议上参加了讨论
2. 李四在6月3日的会议上参加了讨论
3. 会议决定采用微服务架构
4. 预计完成时间是3个月内

请分解以下文本：
---
{text}
---
分解结果（每条一行，编号）："""

        response = self._llm_generate(prompt)
        return response

    def _judge_claims(
        self,
        claims_text: str,
        context: str,
        original_generated: str,
    ) -> str:
        """
        逐条判断每个断言是否在上下文中得到支持（RAGVUE 单次 call 风格）

        严格判断规则：
        - 对于人名、时间表达（年份/日期/时间段）、数字/金额，必须精确匹配
        - 如果上下文没有提到某个人名，但断言说他参加了会议 → hallucinated
        - 如果上下文说"6月"，但断言说"6月3日" → partial（上下文部分支持）
        - 如果上下文根本没有相关讨论，但断言说"会议决定..." → hallucinated
        """
        prompt = f"""你是一个严格的事实核查员。请逐一判断以下每条断言是否被【参考上下文】所支持。

【重要判断标准】
- 对于人名（张三、李四、spk0等）：上下文必须明确提到该人，才算支持
- 对于时间表达（日期、月份、年份、时间段）：必须精确匹配或从上下文可合理推断
- 对于数字/金额/百分比：必须与上下文一致
- "会议决定..." "大家同意..." "已确认..." 等结论性语句：上下文必须有明确的对应记录
- 如果上下文没有相关信息 → fully_hallucinated（捏造）

判断格式（严格按此格式输出，不要有任何其他内容）：
格式：序号|判断|理由
其中判断 = supported / partially_hallucinated / fully_hallucinated

【断言列表】
{claims_text}

【参考上下文】
{context}

【原始生成文本】
{original_generated}

请逐条判断："""

        response = self._llm_generate(prompt)
        return response

    def _parse_verdicts(
        self,
        verdicts_text: str,
        claims_text: str,
        generated: str,
    ) -> FaithfulnessResult:
        """解析 LLM 判断结果，构建 FaithfulnessResult"""
        claims: list[Claim] = []
        warnings: list[str] = []
        critical_issues: list[str] = []

        # 解析每条断言及其判断
        supported = 0
        partially = 0
        fully = 0

        lines = [l.strip() for l in verdicts_text.strip().split("\n") if l.strip()]

        for line in lines:
            if "|" not in line:
                continue

            parts = line.split("|")
            if len(parts) < 3:
                continue

            try:
                idx_part = parts[0].strip()
                verdict_part = parts[1].strip()
                reason_part = "|".join(parts[2:]).strip()

                # 提取断言文本（对应行）
                idx = int(re.sub(r"[^0-9]", "", idx_part)) - 1
                all_claim_lines = [
                    l.strip() for l in claims_text.split("\n")
                    if l.strip() and re.match(r"^\d+[\.\)、]", l.strip())
                ]
                claim_text = all_claim_lines[idx] if idx < len(all_claim_lines) else line

                # 检查是否含关键实体/时间
                is_key_entity = self._has_key_entity(claim_text)
                is_temporal = self._has_temporal(claim_text)

                # 判断
                verdict = verdict_part.lower()
                if "supported" in verdict:
                    supported += 1
                    v = "supported"
                elif "partially" in verdict or "partial" in verdict:
                    partially += 1
                    v = "partially_hallucinated"
                else:
                    fully += 1
                    v = "fully_hallucinated"

                claims.append(Claim(
                    text=claim_text,
                    is_key_entity=is_key_entity,
                    is_temporal=is_temporal,
                    verdict=v,
                    reason=reason_part,
                    evidence=None,
                ))

                # 严重问题检测
                if is_key_entity and v == "fully_hallucinated":
                    critical_issues.append(
                        f"关键实体被捏造：\"{claim_text[:30]}...\""
                    )
                elif is_temporal and v == "fully_hallucinated":
                    critical_issues.append(
                        f"时间信息被捏造：\"{claim_text[:30]}...\""
                    )
                elif v == "partially_hallucinated":
                    warnings.append(
                        f"部分失实：\"{claim_text[:40]}...\" → {reason_part[:60]}"
                    )

            except (ValueError, IndexError):
                continue

        # 计算得分
        total = supported + partially + fully
        if total == 0:
            score = 0.0
        else:
            # RAGVUE 评分：supported 加分，fully hallucinated 扣分
            score = (supported - fully) / max(total, 1)

        return FaithfulnessResult(
            total_claims=len(claims),
            supported=supported,
            partially_hallucinated=partially,
            fully_hallucinated=fully,
            score=max(0.0, score),
            claims=claims,
            warnings=warnings,
            critical_issues=critical_issues,
            raw_llm_output=verdicts_text,
        )

    def _has_key_entity(self, text: str) -> bool:
        """检测文本是否含有关键实体（人名spkX、时间、数字）"""
        has_spk = bool(re.search(r"spk\d+", text))
        has_number = any(re.search(pat, text) for pat in self._NUMBER_PATTERNS)
        # 检测中文人名（2-4个汉字）或英文名
        has_name = bool(re.search(r"[\u4e00-\u9fff]{2,4}(?:总|总|经理|总|总|工|师|责|责|管|助|理|人|长|员)", text))
        return has_spk or has_number or has_name

    def _has_temporal(self, text: str) -> bool:
        """检测文本是否含有时间表达式"""
        return any(re.search(pat, text) for pat in self._TEMPORAL_PATTERNS)

    def _llm_generate(self, prompt: str) -> str:
        """统一 LLM 调用入口"""
        if self._llm is not None:
            response = self._llm.generate_text(instruction=prompt)
            return response.text if hasattr(response, "text") else str(response)

        # 懒加载 fallback：使用本地 transformers pipeline
        try:
            from transformers import pipeline
            generator = pipeline(
                "text-generation",
                model="Qwen/Qwen2.5-0.5B-Instruct",
                device_map="auto",
                max_new_tokens=512,
            )
            output = generator(prompt, do_sample=False)[0]["generated_text"]
            return output[len(prompt):].strip()
        except Exception as exc:
            return f"[ERROR: LLM unavailable: {exc}]"
