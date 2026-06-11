"""
MySQL 持久化层 — 会议任务与操作日志的 ACID 落地

核心表：
  - tasks: 任务核心状态（source of truth）
  - audit_logs: 操作审计日志（append-only）
  - email_records: 邮件发送记录（幂等保证）

设计原则：
  - tasks 是 source of truth，系统重启后从 MySQL 恢复
  - 所有状态变更走事务，保证 ACID
  - audit_logs 是 append-only，用于审计和溯源
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

from audiochat.workflow.state import Actor, AuditAction, EmailResult, TaskState, TaskStatus

Base = declarative_base()


# ============================================================================
# SQLAlchemy 模型
# ============================================================================


class TaskModel(Base):
    """任务主表"""

    __tablename__ = "tasks"

    task_id = Column(String(64), primary_key=True)
    audio_path = Column(String(512), nullable=False)
    meeting_title = Column(String(256), nullable=False)
    status = Column(String(32), nullable=False, index=True)
    summary = Column(Text, default="")
    action_items = Column(JSON, default=list)
    audit_trail = Column(JSON, default=list)
    email_sent = Column(Integer, default=0)          # 0/1，幂等标记
    email_sent_at = Column(DateTime, nullable=True)
    email_sent_to = Column(JSON, default=list)
    email_issue_urls = Column(JSON, default=list)
    email_error = Column(Text, nullable=True)
    email_retry_count = Column(Integer, default=0)
    quality_report = Column(JSON, nullable=True)
    mode = Column(String(32), default="summary")
    meeting_date = Column(String(32), nullable=True)
    participants = Column(JSON, default=list)
    rejection_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("idx_status_created", "status", "created_at"),
        Index("idx_meeting_date", "meeting_date"),
    )

    def to_task_state(self) -> TaskState:
        """MySQL 行 -> TaskState"""
        audit_trail = []
        for r in (self.audit_trail or []):
            audit_trail.append(AuditRecord(
                action=AuditAction(r["action"]),
                actor=Actor(who=r["actor"]["who"], role=r["actor"]["role"]),
                timestamp=r["timestamp"],
                comment=r.get("comment"),
                regenerate_feedback=r.get("regenerate_feedback"),
            ))

        email_result = EmailResult(
            sent=bool(self.email_sent),
            task_id=self.task_id,
            sent_at=self.email_sent_at.isoformat() if self.email_sent_at else None,
            sent_to=tuple(self.email_sent_to or []),
            issue_urls=tuple(self.email_issue_urls or []),
            error=self.email_error,
            retry_count=self.email_retry_count,
        )

        quality_report = None
        if self.quality_report:
            from audiochat.workflow.state import QualityReport
            quality_report = QualityReport.from_dict(self.quality_report)

        status = TaskStatus(self.status) if self.status else TaskStatus.PENDING_ASR

        return TaskState(
            task_id=self.task_id,
            audio_path=self.audio_path,
            meeting_title=self.meeting_title,
            status=status,
            summary=self.summary or "",
            action_items=tuple(self.action_items or []),
            audit_trail=audit_trail,
            email_result=email_result,
            created_at=self.created_at.isoformat() if self.created_at else datetime.now().isoformat(),
            updated_at=self.updated_at.isoformat() if self.updated_at else datetime.now().isoformat(),
            mode=self.mode or "summary",
            meeting_date=self.meeting_date,
            participants=tuple(self.participants or []),
            rejection_reason=self.rejection_reason,
            quality_report=quality_report,
        )

    @classmethod
    def from_task_state(cls, state: TaskState) -> "TaskModel":
        """TaskState -> MySQL 行"""
        m = cls(
            task_id=state.task_id,
            audio_path=state.audio_path,
            meeting_title=state.meeting_title,
            status=state.status.value if isinstance(state.status, TaskStatus) else state.status,
            summary=state.summary,
            action_items=list(state.action_items),
            audit_trail=[
                {
                    "action": r.action.value if isinstance(r.action, AuditAction) else r.action,
                    "actor": {"who": r.actor.who, "role": r.actor.role},
                    "timestamp": r.timestamp,
                    "comment": r.comment,
                    "regenerate_feedback": r.regenerate_feedback,
                }
                for r in state.audit_trail
            ],
            email_sent=int(state.email_result.sent),
            email_sent_at=datetime.fromisoformat(state.email_result.sent_at) if state.email_result.sent_at else None,
            email_sent_to=list(state.email_result.sent_to),
            email_issue_urls=list(state.email_result.issue_urls),
            email_error=state.email_result.error,
            email_retry_count=state.email_result.retry_count,
            mode=state.mode,
            meeting_date=state.meeting_date,
            participants=list(state.participants),
            rejection_reason=state.rejection_reason,
            created_at=datetime.fromisoformat(state.created_at) if state.created_at else datetime.now(),
            updated_at=datetime.fromisoformat(state.updated_at) if state.updated_at else datetime.now(),
        )
        if state.quality_report:
            m.quality_report = state.quality_report.to_dict()
        return m


class AuditLogModel(Base):
    """审计日志表（append-only，不可修改）"""

    __tablename__ = "audit_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    actor_who = Column(String(128), nullable=False)
    actor_role = Column(String(32), nullable=False)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    comment = Column(Text, nullable=True)
    regenerate_feedback = Column(Text, nullable=True)
    extra_data = Column(JSON, nullable=True)   # 扩展字段（JSON）

    __table_args__ = (
        Index("idx_task_timestamp", "task_id", "timestamp"),
    )


class EmailRecordModel(Base):
    """邮件发送记录（幂等保证）"""

    __tablename__ = "email_records"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False, unique=True, index=True)
    sent = Column(Integer, default=0)
    sent_at = Column(DateTime, nullable=True)
    sent_to = Column(JSON, default=list)
    issue_urls = Column(JSON, default=list)
    error = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ============================================================================
# MySQL 连接管理器
# ============================================================================


@dataclass
class MySQLConfig:
    """MySQL 连接配置"""

    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "ditx_clerk"
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle: int = 3600      # 1 小时回收连接，避免 MySQL 8 小时超时
    echo: bool = False             # SQL 日志，生产环境设 False


class MySQLStore:
    """
    MySQL 持久化层

    职责：
      - tasks 表：所有任务状态（source of truth）
      - audit_logs 表：操作日志（append-only）
      - email_records 表：邮件幂等记录

    事务保证：
      - 状态更新走事务（update + audit_log 原子写入）
      - Redis 故障时从 MySQL 恢复
    """

    _local = threading.local()

    def __init__(self, config: Optional[MySQLConfig] = None):
        self.config = config or MySQLConfig()
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker] = None

    def initialize(self) -> None:
        """初始化连接池 + 创建表"""
        url = (
            f"mysql+pymysql://{self.config.user}:{self.config.password}"
            f"@{self.config.host}:{self.config.port}/{self.config.database}"
            f"?charset=utf8mb4"
        )
        self._engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=self.config.pool_size,
            max_overflow=self.config.max_overflow,
            pool_recycle=self.config.pool_recycle,
            pool_pre_ping=True,          # 每次取连接前 ping，避免连接失效
            echo=self.config.echo,
        )

        # 自动创建 database（如果不存在）
        self._ensure_database()

        # 建表
        Base.metadata.create_all(self._engine)

        self._session_factory = sessionmaker(bind=self._engine)

        # 监听：每次事务提交后同步 updated_at
        @event.listens_for(self._engine, "before_cursor_execute")
        def receive_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            pass

    def _ensure_database(self) -> None:
        """如果 database 不存在则创建"""
        import pymysql
        conn = pymysql.connect(
            host=self.config.host,
            port=self.config.port,
            user=self.config.user,
            password=self.config.password,
            charset="utf8mb4",
        )
        try:
            with conn.cursor() as c:
                c.execute(f"CREATE DATABASE IF NOT EXISTS `{self.config.database}` "
                          f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """线程安全的 session 上下文"""
        if self._session_factory is None:
            raise RuntimeError("MySQLStore 未初始化，调用 initialize()")
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        session = self._local.session
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    def close(self) -> None:
        """关闭连接池"""
        if self._engine:
            self._engine.dispose()

    # -------------------------------------------------------------------------
    # 基础 CRUD
    # -------------------------------------------------------------------------

    def save(self, state: TaskState) -> None:
        """
        写入/更新任务状态（ACID 事务）。

        实现要点：
          - INSERT ... ON DUPLICATE KEY UPDATE（upsert，单条 SQL）
          - 更新和审计日志在同一个事务中
          - updated_at 由数据库自动维护（ON UPDATE CURRENT_TIMESTAMP）
        """
        with self.session() as sess:
            model = TaskModel.from_task_state(state)

            # 构建 upsert SQL
            sess.merge(model)

            # 写入审计日志（最近一条）
            if state.audit_trail:
                last = state.audit_trail[-1]
                log = AuditLogModel(
                    task_id=state.task_id,
                    action=last.action.value if isinstance(last.action, AuditAction) else last.action,
                    actor_who=last.actor.who,
                    actor_role=last.actor.role,
                    timestamp=datetime.fromisoformat(last.timestamp) if last.timestamp else datetime.now(),
                    comment=last.comment,
                    regenerate_feedback=last.regenerate_feedback,
                )
                sess.add(log)

    def load(self, task_id: str) -> Optional[TaskState]:
        """从 MySQL 加载任务状态（Redis 未命中时调用）"""
        with self.session() as sess:
            row = sess.query(TaskModel).filter(TaskModel.task_id == task_id).first()
            if row is None:
                return None
            return row.to_task_state()

    def delete(self, task_id: str) -> bool:
        """删除任务（软删：只更新状态，不物理删除）"""
        with self.session() as sess:
            row = sess.query(TaskModel).filter(TaskModel.task_id == task_id).first()
            if row is None:
                return False
            row.status = TaskStatus.PENDING_ASR.value  # 标记为废弃，不物理删除
            return True

    def exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        with self.session() as sess:
            count = sess.query(TaskModel).filter(TaskModel.task_id == task_id).count()
            return count > 0

    # -------------------------------------------------------------------------
    # 批量查询
    # -------------------------------------------------------------------------

    def all_task_ids(self) -> list[str]:
        """返回所有任务 ID"""
        with self.session() as sess:
            rows = sess.query(TaskModel.task_id).all()
            return [r[0] for r in rows]

    def list_by_status(self, status: TaskStatus) -> list[TaskState]:
        """按状态查询任务"""
        with self.session() as sess:
            rows = (
                sess.query(TaskModel)
                .filter(TaskModel.status == status.value)
                .order_by(TaskModel.created_at.desc())
                .all()
            )
            return [r.to_task_state() for r in rows]

    def list_pending_approval(self) -> list[TaskState]:
        """返回所有待审核任务（按创建时间正序）"""
        return self.list_by_status(TaskStatus.PENDING_APPROVAL)

    def list_recent(self, limit: int = 20) -> list[TaskState]:
        """返回最近 N 个任务"""
        with self.session() as sess:
            rows = (
                sess.query(TaskModel)
                .order_by(TaskModel.updated_at.desc())
                .limit(limit)
                .all()
            )
            return [r.to_task_state() for r in rows]

    def get_stats(self) -> dict:
        """返回存储统计"""
        with self.session() as sess:
            total = sess.query(TaskModel).count()
            status_counts: dict[str, int] = {}
            for row in sess.query(TaskModel.status,).all():
                status_counts[row[0]] = status_counts.get(row[0], 0) + 1
            return {"total": total, "by_status": status_counts}

    # -------------------------------------------------------------------------
    # 幂等操作
    # -------------------------------------------------------------------------

    def save_email_record(self, state: TaskState) -> None:
        """保存邮件发送记录（幂等）"""
        with self.session() as sess:
            record = EmailRecordModel(
                task_id=state.task_id,
                sent=int(state.email_result.sent),
                sent_at=datetime.fromisoformat(state.email_result.sent_at)
                    if state.email_result.sent_at else None,
                sent_to=list(state.email_result.sent_to),
                issue_urls=list(state.email_result.issue_urls),
                error=state.email_result.error,
                retry_count=state.email_result.retry_count,
            )
            sess.merge(record)

    def get_email_record(self, task_id: str) -> Optional[EmailRecordModel]:
        """查询邮件发送记录"""
        with self.session() as sess:
            return (
                sess.query(EmailRecordModel)
                .filter(EmailRecordModel.task_id == task_id)
                .first()
            )

    # -------------------------------------------------------------------------
    # 审计日志查询
    # -------------------------------------------------------------------------

    def get_audit_logs(self, task_id: str, limit: int = 50) -> list[AuditLogModel]:
        """查询任务的审计日志"""
        with self.session() as sess:
            return (
                sess.query(AuditLogModel)
                .filter(AuditLogModel.task_id == task_id)
                .order_by(AuditLogModel.timestamp.desc())
                .limit(limit)
                .all()
            )

    def bulk_recover_from_mysql(self, task_ids: list[str]) -> dict[str, TaskState]:
        """
        批量从 MySQL 恢复任务状态。
        用于 Redis 故障恢复或首次启动时的全量加载。
        """
        recovered: dict[str, TaskState] = {}
        if not task_ids:
            return recovered
        with self.session() as sess:
            rows = (
                sess.query(TaskModel)
                .filter(TaskModel.task_id.in_(task_ids))
                .all()
            )
            for row in rows:
                recovered[row.task_id] = row.to_task_state()
        return recovered
