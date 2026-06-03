"""
任务持久化存储 — JSON 文件后端

所有任务状态持久化到 `tasks/` 目录，每个 task_id 一个 JSON 文件。
重启后可恢复，支持幂等操作。

文件结构：
  <store_dir>/
    <task_id1>.json   # 一个任务一个文件
    <task_id2>.json
    ...
"""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import Iterator, Optional

from audiochat.workflow.state import TaskState, TaskStatus


class TaskStore:
    """
    线程安全的 JSON 文件存储。
    每个任务存一个 JSON 文件，读写原子化（写用临时文件 + rename）。
    """

    def __init__(self, store_dir: str = "./workflow_tasks"):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # --- 基础 CRUD ---

    def save(self, state: TaskState) -> None:
        """保存任务状态（原子写：临时文件 + rename）"""
        with self._lock:
            path = self._path(state.task_id)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
            tmp.replace(path)

    def load(self, task_id: str) -> Optional[TaskState]:
        """加载任务状态，文件不存在返回 None"""
        with self._lock:
            path = self._path(task_id)
            if not path.exists():
                return None
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return TaskState.from_dict(d)

    def delete(self, task_id: str) -> bool:
        """删除任务，返回是否实际删除了文件"""
        with self._lock:
            path = self._path(task_id)
            if path.exists():
                path.unlink()
                return True
            return False

    def exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        return self._path(task_id).exists()

    # --- 批量查询 ---

    def all_task_ids(self) -> list[str]:
        """返回所有任务 ID（不含 .tmp）"""
        return [p.stem for p in self.store_dir.iterdir() if p.suffix == ".json"]

    def list_by_status(self, status: TaskStatus) -> list[TaskState]:
        """按状态查询任务"""
        results = []
        for tid in self.all_task_ids():
            state = self.load(tid)
            if state is not None and state.status == status:
                results.append(state)
        return results

    def list_pending_approval(self) -> list[TaskState]:
        """返回所有待审核任务（按创建时间正序）"""
        tasks = self.list_by_status(TaskStatus.PENDING_APPROVAL)
        tasks.sort(key=lambda s: s.created_at)
        return tasks

    def list_recent(self, limit: int = 20) -> list[TaskState]:
        """返回最近 N 个任务（按更新时间倒序）"""
        all_ids = self.all_task_ids()
        states = []
        for tid in all_ids:
            s = self.load(tid)
            if s is not None:
                states.append(s)
        states.sort(key=lambda s: s.updated_at, reverse=True)
        return states[:limit]

    # --- 幂等操作封装 ---

    def approve_and_send(
        self,
        task_id: str,
        approver: str,
        sender_fn,  # Callable[[TaskState], EmailResult]
    ) -> dict:
        """
        幂等的 approve + 发送邮件。

        幂等逻辑：
          1. 任务不存在 → 报错
          2. email_result.sent == True → 直接返回（不发第二封）
          3. 状态不是 PENDING_APPROVAL → 报错
          4. 正常流程 → 发邮件 → 标记 sent=True → 返回结果
        """
        state = self.load(task_id)
        if state is None:
            return {"ok": False, "error": f"任务 {task_id} 不存在"}

        if state.status not in (TaskStatus.PENDING_APPROVAL, TaskStatus.APPROVED):
            return {
                "ok": False,
                "error": f"当前状态不允许确认：{state.status.value}",
                "status": state.status.value,
            }

        # 幂等检查：已经发送过了，直接返回
        if state.email_result.sent:
            return {
                "ok": True,
                "idempotent": True,
                "message": "邮件已发送，跳过",
                "sent_at": state.email_result.sent_at,
                "sent_to": list(state.email_result.sent_to),
                "issue_urls": list(state.email_result.issue_urls),
            }

        # 正常发送流程
        from audiochat.workflow.state import Actor, AuditAction
        state.status = TaskStatus.APPROVED
        state.add_audit(
            action=AuditAction.APPROVE,
            actor=Actor(who=approver, role="user"),
        )
        self.save(state)

        result = sender_fn(state)
        if result.sent:
            state.mark_email_sent(
                sent_to=list(result.sent_to),
                issue_urls=list(result.issue_urls),
            )
        else:
            state.mark_email_failed(result.error or "未知错误")
        self.save(state)

        return {
            "ok": result.sent,
            "idempotent": False,
            "sent_at": state.email_result.sent_at,
            "sent_to": list(state.email_result.sent_to),
            "issue_urls": list(state.email_result.issue_urls),
            "error": result.error,
        }

    def reject_and_regenerate(
        self,
        task_id: str,
        rejector: str,
        comment: str,
    ) -> dict:
        """
        拒绝并触发重新生成（reject 后状态回到 PENDING_LLM）。
        """
        state = self.load(task_id)
        if state is None:
            return {"ok": False, "error": f"任务 {task_id} 不存在"}

        if state.status not in (
            TaskStatus.PENDING_APPROVAL,
            TaskStatus.REJECTED,
            TaskStatus.APPROVED,
        ):
            return {
                "ok": False,
                "error": f"当前状态不允许拒绝：{state.status.value}",
                "status": state.status.value,
            }

        from audiochat.workflow.state import Actor, AuditAction
        state.add_audit(
            action=AuditAction.REJECT,
            actor=Actor(who=rejector, role="user"),
            comment=comment,
        )
        state.status = TaskStatus.REJECTED
        self.save(state)

        return {
            "ok": True,
            "task_id": task_id,
            "status": state.status.value,
            "rejection_reason": comment,
            "message": "已拒绝，请重新处理",
        }

    # --- 内部工具 ---

    def _path(self, task_id: str) -> Path:
        return self.store_dir / f"{task_id}.json"

    def get_stats(self) -> dict:
        """返回存储统计"""
        all_ids = self.all_task_ids()
        counts: dict[str, int] = {}
        for tid in all_ids:
            s = self.load(tid)
            if s is not None:
                key = s.status.value
                counts[key] = counts.get(key, 0) + 1
        return {
            "total": len(all_ids),
            "by_status": counts,
            "store_dir": str(self.store_dir),
        }

    def clear_all(self) -> int:
        """清空所有任务（慎用，仅测试用）"""
        count = 0
        with self._lock:
            for p in self.store_dir.glob("*.json"):
                p.unlink()
                count += 1
        return count
