"""
工作流状态与任务定义 — Human-in-the-Loop 支持

状态流转：
  PENDING_ASR → PENDING_LLM → PENDING_APPROVAL → APPROVED → EMAIL_SENT
                                                   ↘ REJECTED → PENDING_LLM（重新生成）

幂等设计：
  - email_sent=True 标记已发送，重复调用直接返回成功（不发第二封）
  - 每次操作都有 timestamp + actor，记录谁在什么时间做了什么
  - 所有状态变更持久化到 JSON，重启可恢复
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    """任务状态枚举"""

    PENDING_ASR = "pending_asr"
    PENDING_LLM = "pending_llm"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EMAIL_SENT = "email_sent"
    EMAIL_FAILED = "email_failed"


class AuditAction(str, Enum):
    """人工审核操作"""

    APPROVE = "approve"
    REJECT = "reject"


@dataclass
class Actor:
    """操作者"""

    who: str          # 如 "前端用户 Alice" 或 "系统自动"
    role: str        # "user" | "system"


@dataclass
class AuditRecord:
    """单条审核记录"""

    action: AuditAction
    actor: Actor
    timestamp: str
    comment: Optional[str] = None   # reject 时填写修改意见
    regenerate_feedback: Optional[str] = None  # reject 时传给 LLM 的反馈


@dataclass
class EmailResult:
    """邮件发送结果（幂等标记载体）"""

    sent: bool = False
    task_id: Optional[str] = None
    sent_at: Optional[str] = None
    sent_to: tuple[str, ...] = field(default_factory=tuple)
    issue_urls: tuple[str, ...] = field(default_factory=tuple)
    error: Optional[str] = None
    retry_count: int = 0


@dataclass
class QualityReport:
    """
    AI 自动质量报告 — 在 LLM 生成总结后，由 AI 驱动的质量评估。

    人的角色从"评估者"变成"决策者"：
      AI 做分析 → 人只看报告做判断
    """

    summary_score: float = 0.0        # 总结质量分 0-10
    action_item_score: float = 0.0     # 行动项质量分 0-10
    issues: tuple[str, ...] = field(default_factory=tuple)       # 发现的问题
    warnings: tuple[str, ...] = field(default_factory=tuple)      # 警告项
    overall_pass: bool = False          # 是否建议直接通过
    overall_score: float = 0.0         # 综合质量分 0-10
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    raw_output: str = ""               # AI 原始输出（便于调试/人工复核）

    def to_dict(self) -> dict:
        return {
            "summary_score": self.summary_score,
            "action_item_score": self.action_item_score,
            "issues": list(self.issues),
            "warnings": list(self.warnings),
            "overall_pass": self.overall_pass,
            "overall_score": self.overall_score,
            "generated_at": self.generated_at,
            "raw_output": self.raw_output,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QualityReport":
        return cls(
            summary_score=d.get("summary_score", 0.0),
            action_item_score=d.get("action_item_score", 0.0),
            issues=tuple(d.get("issues", [])),
            warnings=tuple(d.get("warnings", [])),
            overall_pass=d.get("overall_pass", False),
            overall_score=d.get("overall_score", 0.0),
            generated_at=d.get("generated_at", datetime.now().isoformat()),
            raw_output=d.get("raw_output", ""),
        )

    def display_summary(self) -> str:
        """格式化报告摘要（用于 CLI/前端展示）"""
        lines = []
        lines.append(f"  总结质量   : {self.summary_score:.1f}/10")
        lines.append(f"  行动项质量 : {self.action_item_score:.1f}/10")
        lines.append(f"  综合评分   : {self.overall_score:.1f}/10")
        if self.issues:
            lines.append(f"  发现问题   :")
            for issue in self.issues:
                lines.append(f"    - {issue}")
        if self.warnings:
            lines.append(f"  警告项     :")
            for w in self.warnings:
                lines.append(f"    - {w}")
        status = "✅ 建议通过" if self.overall_pass else "⚠️ 需重点审核"
        lines.append(f"  AI 建议    : {status}")
        return "\n".join(lines)


@dataclass
class TaskState:
    """
    完整任务状态，包含所有元数据和审核记录。
    序列化/反序列化用 dict（方便 JSON 存储）， dataclass 仅用于代码中的类型标注。
    """

    task_id: str
    audio_path: str
    meeting_title: str
    status: TaskStatus

    # 核心产物
    summary: str = ""
    action_items: tuple[str, ...] = field(default_factory=tuple)

    # 审核链路
    audit_trail: list[AuditRecord] = field(default_factory=list)

    # 幂等标记
    email_result: EmailResult = field(default_factory=EmailResult)

    # 时间戳
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 元数据
    mode: str = "summary"
    meeting_date: Optional[str] = None
    participants: tuple[str, ...] = field(default_factory=tuple)

    # 拒绝原因（最近的 reject 记录）
    rejection_reason: Optional[str] = None

    # 邮件发送失败重试次数上限（超过后不再重试，进入 EMAIL_FAILED）
    max_email_retries: int = 3

    # AI 质量报告（LLM 生成总结后自动生成，人工审核前先过这一关）
    quality_report: Optional[QualityReport] = None

    def to_dict(self) -> dict:
        """序列化：dataclass -> dict（用于 JSON 持久化）"""
        return {
            "task_id": self.task_id,
            "audio_path": self.audio_path,
            "meeting_title": self.meeting_title,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "summary": self.summary,
            "action_items": list(self.action_items),
            "audit_trail": [
                {
                    "action": r.action.value if isinstance(r.action, Enum) else r.action,
                    "actor": {"who": r.actor.who, "role": r.actor.role},
                    "timestamp": r.timestamp,
                    "comment": r.comment,
                    "regenerate_feedback": r.regenerate_feedback,
                }
                for r in self.audit_trail
            ],
            "email_result": {
                "sent": self.email_result.sent,
                "task_id": self.email_result.task_id,
                "sent_at": self.email_result.sent_at,
                "sent_to": list(self.email_result.sent_to),
                "issue_urls": list(self.email_result.issue_urls),
                "error": self.email_result.error,
                "retry_count": self.email_result.retry_count,
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "mode": self.mode,
            "meeting_date": self.meeting_date,
            "participants": list(self.participants),
            "rejection_reason": self.rejection_reason,
            "max_email_retries": self.max_email_retries,
            "quality_report": self.quality_report.to_dict() if self.quality_report else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TaskState:
        """反序列化：dict -> TaskState"""
        audit_trail = []
        for r in d.get("audit_trail", []):
            audit_trail.append(AuditRecord(
                action=AuditAction(r["action"]),
                actor=Actor(who=r["actor"]["who"], role=r["actor"]["role"]),
                timestamp=r["timestamp"],
                comment=r.get("comment"),
                regenerate_feedback=r.get("regenerate_feedback"),
            ))

        er = d.get("email_result", {})
        email_result = EmailResult(
            sent=er.get("sent", False),
            task_id=er.get("task_id"),
            sent_at=er.get("sent_at"),
            sent_to=tuple(er.get("sent_to", [])),
            issue_urls=tuple(er.get("issue_urls", [])),
            error=er.get("error"),
            retry_count=er.get("retry_count", 0),
        )

        return cls(
            task_id=d["task_id"],
            audio_path=d["audio_path"],
            meeting_title=d["meeting_title"],
            status=TaskStatus(d["status"]),
            summary=d.get("summary", ""),
            action_items=tuple(d.get("action_items", [])),
            audit_trail=audit_trail,
            email_result=email_result,
            created_at=d.get("created_at", datetime.now().isoformat()),
            updated_at=d.get("updated_at", datetime.now().isoformat()),
            mode=d.get("mode", "summary"),
            meeting_date=d.get("meeting_date"),
            participants=tuple(d.get("participants", [])),
            rejection_reason=d.get("rejection_reason"),
            max_email_retries=d.get("max_email_retries", 3),
            quality_report=QualityReport.from_dict(d["quality_report"])
            if d.get("quality_report")
            else None,
        )

    def add_audit(self, action: AuditAction, actor: Actor, comment: Optional[str] = None) -> None:
        """追加审核记录"""
        self.audit_trail.append(AuditRecord(
            action=action,
            actor=actor,
            timestamp=datetime.now().isoformat(),
            comment=comment,
            regenerate_feedback=comment,
        ))
        self.updated_at = datetime.now().isoformat()

        if action == AuditAction.REJECT:
            self.rejection_reason = comment
            self.status = TaskStatus.REJECTED
        elif action == AuditAction.APPROVE:
            self.status = TaskStatus.APPROVED

    def mark_email_sent(self, sent_to: list[str], issue_urls: list[str]) -> None:
        """标记邮件已发送（幂等）"""
        self.email_result.sent = True
        self.email_result.sent_at = datetime.now().isoformat()
        self.email_result.sent_to = tuple(sent_to)
        self.email_result.issue_urls = tuple(issue_urls)
        self.email_result.error = None
        self.status = TaskStatus.EMAIL_SENT
        self.updated_at = datetime.now().isoformat()

    def mark_email_failed(self, error: str) -> None:
        """标记邮件发送失败"""
        self.email_result.sent = False
        self.email_result.error = error
        self.email_result.retry_count += 1
        if self.email_result.retry_count >= self.max_email_retries:
            self.status = TaskStatus.EMAIL_FAILED
        self.updated_at = datetime.now().isoformat()

    @staticmethod
    def new(audio_path: str, meeting_title: str, mode: str = "summary") -> TaskState:
        """创建新任务（工厂方法）"""
        return TaskState(
            task_id=uuid.uuid4().hex[:12],
            audio_path=audio_path,
            meeting_title=meeting_title,
            status=TaskStatus.PENDING_ASR,
            mode=mode,
        )
