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
# SOP 沉淀逻辑（approve 成功后触发）
# ---------------------------------------------------------------------------

def _try_precipitate_sop(state: TaskState) -> None:
    """
    检查是否需要沉淀 SOP。

    触发条件（来自 GA 的 No Execution, No Memory 原则）：
      1. 同一类会议出现 2+ 次（L1 计数）
      2. 总结质量达到阈值（overall_score >= 7.0）
      3. 会议类型不是 standup / brainstorm（一次性讨论不沉淀 SOP）

    沉淀流程：
      LLM 从总结中抽取 SOP 结构 → 打印 HITL 确认 → 用户确认后写入 L3
    """
    if not state.summary:
        return

    report = state.quality_report
    if report is None or report.overall_score < 7.0:
        return

    from audiochat.rag.memory_hierarchy import classify_meeting_type, HierarchicalMeetingStore, MeetingType
    meeting_type = classify_meeting_type(state.summary)
    if meeting_type in (MeetingType.STANDUP, MeetingType.BRAINSTORM, MeetingType.UNKNOWN):
        return

    hier_store = HierarchicalMeetingStore(persist_dir="./rag_storage_hierarchical")
    same_type_entries = [
        e for e in hier_store.get_all_l1_entries()
        if any(mt in e.meeting_types for mt in meeting_type.value.split())
    ]
    if len(same_type_entries) < 2:
        print(f"\n[SOP 沉淀] 同类型会议（{meeting_type.value}）目前仅 {len(same_type_entries)} 次，"
              f"不足 2 次，暂不沉淀")
        return

    print(f"\n[SOP 沉淀] 检测到同类会议 ≥2 次，质量达标，正在抽取 SOP...")
    sop_entry = _extract_sop_from_summary(state, meeting_type)
    if sop_entry is None:
        return

    print(f"\n{'='*60}")
    print(f"[SOP 沉淀建议] 是否将以下 SOP 写入 L3？")
    print(f"  类型: {sop_entry.sop_type}")
    print(f"  标题: {sop_entry.content[:80]}...")
    print(f"  步骤: {' | '.join(sop_entry.key_steps[:5])}")
    if sop_entry.failure_cases:
        print(f"  常见失败: {sop_entry.failure_cases[0]}")
    print(f"{'='*60}")
    confirm = input("  确认写入 L3？(y/N): ").strip().lower()
    if confirm == "y":
        hier_store.add_l3_sop(sop_entry)
        print(f"  ✅ SOP 已写入 L3（{sop_entry.sop_type}）")
    else:
        print(f"  ⏭️  跳过")


def _extract_sop_from_summary(state: TaskState, meeting_type) -> "L3SOPEntry | None":
    """
    用 LLM 从会议总结中抽取 SOP 结构。

    Returns:
        L3SOPEntry 或 None（抽取失败时）
    """
    from audiochat.rag.memory_hierarchy import L3SOPEntry, MeetingType

    sop_type_map = {
        MeetingType.WEEKLY_REVIEW: "weekly_review",
        MeetingType.REQUIREMENT_REVIEW: "requirement_review",
        MeetingType.INTERVIEW: "interview",
        MeetingType.RETROSPECTIVE: "retrospective",
        MeetingType.ONE_ON_ONE: "one_on_one",
    }
    sop_type = sop_type_map.get(meeting_type, "general")

    prompt = f"""从以下会议总结中抽取可复用的 SOP（标准操作流程）。

会议总结：
{state.summary[:2000]}

请按以下 JSON 格式输出（只输出 JSON，不要有其他内容）：
{{
  "content": "SOP 的简要描述（一句话）",
  "preconditions": ["前置条件1", "前置条件2"],
  "key_steps": ["步骤1", "步骤2", "步骤3"],
  "failure_cases": ["常见失败案例1", "常见失败案例2"]
}}
"""

    try:
        from audiochat.llm.funaudiochat_llm import FunAudioChatLLM
        llm = FunAudioChatLLM(
            model_path="~/llm/voice/Fun-Audio-Chat/pretrained_models/Fun-Audio-Chat-8B",
            device="cuda:0",
        )
        result = llm.generate_text(instruction=prompt)
        text = result.text.strip()

        import re as _re
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group())

        from datetime import datetime
        return L3SOPEntry(
            sop_type=sop_type,
            content=data.get("content", ""),
            preconditions=data.get("preconditions", []),
            key_steps=data.get("key_steps", []),
            failure_cases=data.get("failure_cases", []),
            meeting_types=[meeting_type.value],
            verified=False,
            meeting_id=state.task_id,
            timestamp=datetime.now().isoformat(),
        )
    except Exception:
        return None


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

    # SOP 沉淀检查（在 approve 成功后自动触发）
    _try_precipitate_sop(state)


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
