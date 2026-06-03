"""
任务审核 API 服务 — FastAPI

提供 REST 接口供前端调用，替代 CLI。

Endpoints:
  GET  /tasks                  任务列表（支持 ?status= 过滤）
  GET  /tasks/<task_id>        任务详情
  GET  /tasks/pending          待审核任务列表
  POST /tasks/<task_id>/approve   确认并发送邮件
  POST /tasks/<task_id>/reject    拒绝并给出修改意见
  GET  /tasks/stats             统计信息

Usage:
    pip install fastapi uvicorn
    python scripts/api_server.py --port 8000 --store ./workflow_tasks
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from audiochat.workflow import TaskState, TaskStore, TaskStatus, AuditAction
from audiochat.workflow.state import Actor
from actions.mail_dispatcher.gitea_sender import (
    dispatch_issues,
    summarize_results,
    GiteaIssuePayload,
)


# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------

class ApproveRequest(BaseModel):
    approver: str = Field(default="api_user", description="审批人")


class RejectRequest(BaseModel):
    rejector: str = Field(default="api_user", description="拒绝人")
    reason: str = Field(..., description="修改意见")


class TaskResponse(BaseModel):
    task_id: str
    meeting_title: str
    status: str
    mode: str
    summary: str
    action_items: list[str]
    email_sent: bool
    email_result: dict | None
    audit_trail: list[dict]
    created_at: str
    updated_at: str
    rejection_reason: str | None


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

def create_app(store: TaskStore) -> FastAPI:
    app = FastAPI(
        title="DiTX-Clerk Workflow API",
        description="Human-in-the-Loop 任务审核接口",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/tasks", response_model=list[TaskResponse])
    def list_tasks(status: str | None = None, limit: int = 20):
        """任务列表"""
        if status:
            try:
                st = TaskStatus(status)
            except ValueError:
                raise HTTPException(400, f"未知状态：{status}")
            tasks = store.list_by_status(st)
        else:
            tasks = store.list_recent(limit=limit)

        return [_state_to_resp(t) for t in tasks]

    @app.get("/tasks/pending", response_model=list[TaskResponse])
    def list_pending():
        """待审核任务列表"""
        tasks = store.list_pending_approval()
        return [_state_to_resp(t) for t in tasks]

    @app.get("/tasks/stats")
    def stats():
        """统计信息"""
        return store.get_stats()

    @app.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str):
        """任务详情"""
        state = store.load(task_id)
        if state is None:
            raise HTTPException(404, f"任务 {task_id} 不存在")
        return _state_to_resp(state)

    @app.post("/tasks/{task_id}/approve", response_model=dict)
    def approve_task(task_id: str, body: ApproveRequest):
        """确认任务并发送邮件"""
        state = store.load(task_id)
        if state is None:
            raise HTTPException(404, f"任务 {task_id} 不存在")

        if state.email_result.sent:
            return {
                "ok": True,
                "idempotent": True,
                "message": "邮件已发送，跳过",
                "sent_at": state.email_result.sent_at,
                "issue_urls": list(state.email_result.issue_urls),
            }

        result = store.approve_and_send(
            task_id=task_id,
            approver=body.approver,
            sender_fn=_build_sender_fn(),
        )

        if not result["ok"]:
            raise HTTPException(409, result["error"])

        return {
            "ok": True,
            "idempotent": result.get("idempotent", False),
            "sent_at": result.get("sent_at"),
            "issue_urls": result.get("issue_urls", []),
            "sent_to": result.get("sent_to", []),
        }

    @app.post("/tasks/{task_id}/reject", response_model=dict)
    def reject_task(task_id: str, body: RejectRequest):
        """拒绝任务"""
        state = store.load(task_id)
        if state is None:
            raise HTTPException(404, f"任务 {task_id} 不存在")

        result = store.reject_and_regenerate(
            task_id=task_id,
            rejector=body.rejector,
            comment=body.reason,
        )

        if not result["ok"]:
            raise HTTPException(409, result["error"])

        return result

    return app


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _state_to_resp(state: TaskState) -> TaskResponse:
    er = state.email_result
    return TaskResponse(
        task_id=state.task_id,
        meeting_title=state.meeting_title,
        status=state.status.value,
        mode=state.mode,
        summary=state.summary,
        action_items=list(state.action_items),
        email_sent=er.sent,
        email_result={
            "sent": er.sent,
            "sent_at": er.sent_at,
            "sent_to": list(er.sent_to),
            "issue_urls": list(er.issue_urls),
            "error": er.error,
            "retry_count": er.retry_count,
        } if er.sent or er.error else None,
        audit_trail=[
            {
                "action": r.action.value,
                "actor": {"who": r.actor.who, "role": r.actor.role},
                "timestamp": r.timestamp,
                "comment": r.comment,
            }
            for r in state.audit_trail
        ],
        created_at=state.created_at,
        updated_at=state.updated_at,
        rejection_reason=state.rejection_reason,
    )


def _build_sender_fn() -> callable:
    from audiochat.workflow.state import EmailResult

    def send(state: TaskState) -> EmailResult:
        if state.email_result.sent:
            return EmailResult(sent=True)

        if not state.action_items:
            return EmailResult(sent=True)

        payloads = []
        for i, item in enumerate(state.action_items):
            title = f"[行动项] {item}"
            body = f"""## 会议信息
- 任务 ID: {state.task_id}
- 会议: {state.meeting_title}
- 日期: {state.meeting_date or '未知'}

## 行动项
{item}

---
由 DiTX-Clerk 自动生成"""
            payloads.append(GiteaIssuePayload(
                title=title,
                body=body,
                labels=(f"task:{state.task_id}", "auto-dispatch"),
                dispatch_id=f"{state.task_id}_action_{i}",
            ))

        results = dispatch_issues(payloads)
        summary = summarize_results(results)
        sent_to = [r.url for r in results if r.url]
        issue_urls = [r.url for r in results if r.url]
        error = None if summary["all_sent"] else json.dumps(summary["errors"], ensure_ascii=False)

        return EmailResult(
            sent=summary["all_sent"],
            sent_to=tuple(sent_to),
            issue_urls=tuple(issue_urls),
            error=error,
        )

    return send


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="DiTX-Clerk Workflow API Server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--store",
        default="./workflow_tasks",
        help="任务状态存储目录",
    )
    args = parser.parse_args()

    import json

    store = TaskStore(store_dir=args.store)
    app = create_app(store)

    print(f"启动 API 服务：http://{args.host}:{args.port}")
    print(f"  任务列表  : GET  http://{args.host}:{args.port}/tasks")
    print(f"  待审核    : GET  http://{args.host}:{args.port}/tasks/pending")
    print(f"  任务详情  : GET  http://{args.host}:{args.port}/tasks/<task_id>")
    print(f"  确认发送  : POST http://{args.host}:{args.port}/tasks/<task_id>/approve")
    print(f"  拒绝修改  : POST http://{args.host}:{args.port}/tasks/<task_id>/reject")
    print(f"  统计      : GET  http://{args.host}:{args.port}/tasks/stats")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
