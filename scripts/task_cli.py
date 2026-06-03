"""
任务管理 CLI — 替代前端审核界面

Usage:
    python scripts/task_cli.py list                          # 查看所有任务
    python scripts/task_cli.py list --status pending        # 只看待审核
    python scripts/task_cli.py show <task_id>               # 查看任务详情
    python scripts/task_cli.py approve <task_id>            # 确认并发送邮件
    python scripts/task_cli.py reject <task_id> <reason>    # 拒绝并给出修改意见
    python scripts/task_cli.py stats                        # 统计
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))

from audiochat.workflow import TaskState, TaskStore, TaskStatus, AuditAction
from audiochat.workflow.state import Actor
from actions.mail_dispatcher.gitea_sender import (
    dispatch_issues,
    summarize_results,
    GiteaIssuePayload,
)


def _load_store(args) -> TaskStore:
    return TaskStore(store_dir=args.store)


def _build_sender_fn(args) -> callable:
    """构建邮件发送函数（闭包注入参数）"""
    def send(state: TaskState) -> "EmailResult":
        from audiochat.workflow.state import EmailResult

        if state.email_result.sent:
            return EmailResult(sent=True)

        if not state.action_items:
            print("  [Gitea] 无 action_items，跳过邮件发送")
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

        from audiochat.workflow.state import EmailResult
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
# 命令实现
# ---------------------------------------------------------------------------

def cmd_list(args) -> None:
    store = _load_store(args)

    if args.status:
        try:
            status = TaskStatus(args.status)
        except ValueError:
            print(f"未知状态：{args.status}，可选值：{[s.value for s in TaskStatus]}")
            return
        tasks = store.list_by_status(status)
    else:
        tasks = store.list_recent(limit=args.limit)

    if not tasks:
        print("没有找到任务")
        return

    print(f"\n{'任务ID':<14} {'状态':<10} {'综合评分':<10} {'行动项':<6} {'会议标题':<28} {'更新时间'}")
    print("-" * 90)
    for t in tasks:
        status_short = {
            TaskStatus.PENDING_APPROVAL: "PENDING",
            TaskStatus.APPROVED: "APPROVED",
            TaskStatus.EMAIL_SENT: "SENT",
            TaskStatus.EMAIL_FAILED: "FAILED",
            TaskStatus.REJECTED: "REJECTED",
        }.get(t.status, t.status.value)

        if t.quality_report:
            score = f"{t.quality_report.overall_score:.1f}"
            pass_flag = "✅" if t.quality_report.overall_pass else "⚠️"
        else:
            score = "-"
            pass_flag = ""

        action_count = len(t.action_items) if t.action_items else 0

        print(f"{t.task_id:<14} {status_short:<10} {score:<3}{pass_flag} "
              f"{action_count:<6} {t.meeting_title[:26]:<28} {t.updated_at[:16]}")


def cmd_show(args) -> None:
    store = _load_store(args)
    state = store.load(args.task_id)

    if state is None:
        print(f"任务 {args.task_id} 不存在")
        return

    print(f"\n{'='*60}")
    print(f"任务 ID    : {state.task_id}")
    print(f"会议标题   : {state.meeting_title}")
    print(f"状态       : {state.status.value}")
    print(f"模式       : {state.mode}")
    print(f"创建时间   : {state.created_at}")
    print(f"更新时间   : {state.updated_at}")
    print(f"会议日期   : {state.meeting_date or '未知'}")
    print()

    if state.summary:
        print("--- 总结 ---")
        print(state.summary[:500] + ("..." if len(state.summary) > 500 else ""))
        print()

    if state.action_items:
        print(f"--- 行动项 ({len(state.action_items)}条) ---")
        for i, item in enumerate(state.action_items, 1):
            print(f"  {i}. {item}")
        print()

    if state.quality_report:
        print("--- AI 质量报告 ---")
        print(state.quality_report.display_summary())
        print()

    if state.email_result.sent:
        print(f"--- 邮件已发送 ---")
        print(f"  发送时间 : {state.email_result.sent_at}")
        print(f"  发送至   : {', '.join(state.email_result.sent_to) or '无'}")
        print(f"  Issue URL: {', '.join(state.email_result.issue_urls) or '无'}")
        if state.email_result.error:
            print(f"  错误     : {state.email_result.error}")
        print()

    if state.audit_trail:
        print(f"--- 审核记录 ({len(state.audit_trail)}条) ---")
        for record in state.audit_trail:
            icon = "✅" if record.action == AuditAction.APPROVE else "❌"
            print(f"  {icon} [{record.timestamp[:19]}] {record.actor.who} ({record.actor.role}): {record.action.value}")
            if record.comment:
                print(f"     意见: {record.comment}")
        print()

    print(f"{'='*60}\n")


def cmd_approve(args) -> None:
    store = _load_store(args)
    state = store.load(args.task_id)

    if state is None:
        print(f"任务 {args.task_id} 不存在")
        return

    if state.email_result.sent:
        print(f"任务 {args.task_id} 已经发送过，跳过（幂等返回）")
        print(f"  发送时间 : {state.email_result.sent_at}")
        print(f"  Issue URL: {', '.join(state.email_result.issue_urls)}")
        return

    result = store.approve_and_send(
        task_id=args.task_id,
        approver=args.approver or "cli",
        sender_fn=_build_sender_fn(args),
    )

    if not result["ok"]:
        print(f"操作失败：{result['error']}")
        if "status" in result:
            print(f"  当前状态：{result['status']}")
        sys.exit(1)

    if result.get("idempotent"):
        print(f"任务已发送（幂等跳过）：{args.task_id}")
    else:
        print(f"✅ 任务 {args.task_id} 已确认，邮件发送完成")
        print(f"   发送至 : {', '.join(result.get('sent_to', []))}")
        print(f"   Issue  : {', '.join(result.get('issue_urls', []))}")


def cmd_reject(args) -> None:
    store = _load_store(args)

    if not args.reason:
        print("拒绝操作必须提供修改意见（reason 参数）")
        sys.exit(1)

    result = store.reject_and_regenerate(
        task_id=args.task_id,
        rejector=args.rejector or "cli",
        comment=args.reason,
    )

    if not result["ok"]:
        print(f"操作失败：{result['error']}")
        if "status" in result:
            print(f"  当前状态：{result['status']}")
        sys.exit(1)

    print(f"❌ 任务 {args.task_id} 已拒绝")
    print(f"   原因：{result['rejection_reason']}")
    print(f"   状态：{result['status']}")
    print(f"   下一步：请重新生成总结内容并调用 LLM")


def cmd_stats(args) -> None:
    store = _load_store(args)
    stats = store.get_stats()
    print(f"\n{'='*40}")
    print(f"  总任务数  : {stats['total']}")
    print(f"  存储目录  : {stats['store_dir']}")
    print(f"  按状态分布:")
    for status, count in sorted(stats["by_status"].items()):
        print(f"    {status:<25} : {count}")
    print(f"{'='*40}\n")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(prog="task_cli", description="任务管理 CLI")
    parser.add_argument(
        "--store",
        default="./workflow_tasks",
        help="任务状态存储目录（默认 ./workflow_tasks）",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="列出任务")
    p_list.add_argument("--status", default=None, help="按状态过滤")
    p_list.add_argument("--limit", type=int, default=20, help="最多显示条数")

    # show
    p_show = sub.add_parser("show", help="查看任务详情")
    p_show.add_argument("task_id", help="任务 ID")

    # approve
    p_approve = sub.add_parser("approve", help="确认并发送邮件")
    p_approve.add_argument("task_id", help="任务 ID")
    p_approve.add_argument("--approver", default="cli", help="审批人名称")

    # reject
    p_reject = sub.add_parser("reject", help="拒绝并给出修改意见")
    p_reject.add_argument("task_id", help="任务 ID")
    p_reject.add_argument("reason", nargs="+", help="修改意见")
    p_reject.add_argument("--rejector", default="cli", help="拒绝人名称")

    # stats
    sub.add_parser("stats", help="统计信息")

    args = parser.parse_args()

    # 合并 reject 的 reason 参数
    if args.command == "reject":
        args.reason = " ".join(args.reason)

    # 分发
    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "approve": cmd_approve,
        "reject": cmd_reject,
        "stats": cmd_stats,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
