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


# ---------------------------------------------------------------------------
# SOP 沉淀触发器（被 approve_and_send 调用）
# ---------------------------------------------------------------------------

def _try_precipitate_sop(state: TaskState, store_dir: str) -> None:
    """
    会议 APPROVED 后，检查是否满足 SOP 沉淀条件。

    沉淀条件：
      1. 质量报告综合评分 >= 7.0
      2. 同一会议类型出现 2+ 次（L1 索引）
      3. 会议类型不是 standup / brainstorm

    沉淀流程：
      1. 调用 LLM 提取 SOP 结构（from summary）
      2. 打印 HITL 确认提示
      3. 用户确认（y/Y）则写入 L3
    """
    if state.quality_report is None:
        print("[SOP沉淀] 无质量报告，跳过沉淀检查")
        return
    if state.quality_report.overall_score < 7.0:
        print(f"[SOP沉淀] 质量评分 {state.quality_report.overall_score:.1f} < 7.0，跳过沉淀检查")
        return

    try:
        from audiochat.rag.memory_hierarchy import HierarchicalMeetingStore, L3SOPEntry, classify_meeting_type
        from audiochat.rag.memory_hierarchy import MeetingType
    except ImportError:
        print("[SOP沉淀] RAG 模块未安装，跳过沉淀检查")
        return

    hier_store = HierarchicalMeetingStore(persist_dir=store_dir + "_hierarchical")

    # 从 summary 推断会议类型
    meeting_type = classify_meeting_type(state.summary or "")

    # 排除 standup / brainstorm
    excluded = {MeetingType.STANDUP, MeetingType.BRAINSTORM}
    if meeting_type in excluded:
        print(f"[SOP沉淀] 会议类型 {meeting_type.value} 不适合沉淀，跳过")
        return

    # 统计同类会议出现次数
    same_type_count = 0
    primary_type = meeting_type.value
    for other_id, l1_entry in hier_store.l1_entries.items():
        if other_id == state.task_id:
            continue
        if any(t == primary_type for t in l1_entry.meeting_types):
            same_type_count += 1

    if same_type_count < 1:
        print(f"[SOP沉淀] 同类会议（{primary_type}）首次出现，不满足 2+ 次条件，跳过")
        return

    # LLM 提取 SOP 结构
    sop_type_map = {
        MeetingType.INTERVIEW: "interview",
        MeetingType.WEEKLY_REVIEW: "weekly_review",
        MeetingType.REQUIREMENT_REVIEW: "requirement_review",
        MeetingType.RETROSPECTIVE: "retrospective",
        MeetingType.ONE_ON_ONE: "one_on_one",
    }
    sop_type = sop_type_map.get(meeting_type, meeting_type.value)

    sop_structure = _extract_sop_with_llm(state.summary or "", sop_type)

    # 打印 HITL 确认
    print("\n" + "=" * 60)
    print(f"【SOP 沉淀建议】会议类型: {sop_type}")
    print(f"综合评分: {state.quality_report.overall_score:.1f}/10")
    print(f"同类会议出现次数: {same_type_count + 1}")
    print(f"\nSOP 结构预览:")
    print(f"  类型: {sop_structure.get('sop_type', sop_type)}")
    print(f"  关键步骤: {len(sop_structure.get('key_steps', []))} 步")
    print(f"  前置条件: {len(sop_structure.get('preconditions', []))} 条")
    print(f"  失败案例: {len(sop_structure.get('failure_cases', []))} 条")
    print("=" * 60)
    print("SOP沉淀建议: ")
    print(sop_structure.get("content", ""))
    print("=" * 60)
    try:
        confirm = input("是否写入 L3 SOP？（确认请输入 y/Y，否则跳过）: ").strip()
    except (EOFError, OSError):
        confirm = ""

    if confirm.lower() not in ("y",):
        print("[SOP沉淀] 用户取消，跳过写入 L3")
        return

    # 写入 L3
    from datetime import datetime
    sop_entry = L3SOPEntry(
        sop_type=sop_structure.get("sop_type", sop_type),
        content=sop_structure.get("content", ""),
        key_steps=sop_structure.get("key_steps", []),
        preconditions=sop_structure.get("preconditions", []),
        failure_cases=sop_structure.get("failure_cases", []),
        meeting_id=state.task_id,
        meeting_types=[primary_type],
        verified=False,
        timestamp=datetime.now().isoformat(),
    )
    doc_id = hier_store.add_l3_sop(sop_entry)
    print(f"[SOP沉淀] 已写入 L3，doc_id={doc_id}")


# ---------------------------------------------------------------------------
# SOP 提取 Prompt（用于 LLM 调用）
# ---------------------------------------------------------------------------

SOP_EXTRACTION_PROMPT = """你是一个会议 SOP 提炼专家。请根据以下会议总结，提取标准操作流程（SOP）。

## 会议总结
{summary}

## 任务
请以 JSON 格式输出 SOP 结构，包含以下字段：
{{
    "sop_type": "sop类型（如 interview, weekly_review, requirement_review）",
    "content": "SOP 核心描述（自由文本，100-300字）",
    "key_steps": ["步骤1", "步骤2", "..."],
    "preconditions": ["前置条件1", "前置条件2", "..."],
    "failure_cases": ["常见失败案例1", "常见失败案例2", "..."]
}}

## 要求
- key_steps 列出 3-8 个关键步骤
- preconditions 列出 2-5 个前置条件
- failure_cases 列出 2-5 个常见失败案例
- 内容必须基于提供的会议总结，不得凭空编造
- 输出仅包含 JSON，不要有其他文字
"""


def _extract_sop_with_llm(summary: str, sop_type: str) -> dict:
    """调用 LLM 从会议总结中提取 SOP 结构"""
    try:
        from audiochat.llm.funaudiochat_llm import FunAudioChatLLM
        import os
    except ImportError as exc:
        print(f"[SOP沉淀] LLM 模块导入失败: {exc}，使用默认结构")
        return _default_sop_structure(sop_type)

    model_path = os.environ.get(
        "FUNAUDIOCHAT_MODEL",
        "/data/models/Voice/FunAudioLLM/Fun-Audio-Chat-8B",
    )

    try:
        llm = FunAudioChatLLM(model_path=model_path, device="cuda:0")
        prompt = SOP_EXTRACTION_PROMPT.format(summary=summary)
        result = llm.generate_text(instruction=prompt)
        text = result.text.strip()

        # 尝试从输出中提取 JSON
        import json as _json
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("{") or line.startswith("```json"):
                if line.startswith("```"):
                    line = line[7:].strip()
                try:
                    return _json.loads(line if line.startswith("{") else text)
                except _json.JSONDecodeError:
                    pass

        # 尝试解析整个文本
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            print(f"[SOP沉淀] LLM 输出无法解析为 JSON，使用默认结构")
            return _default_sop_structure(sop_type)

    except Exception as exc:
        print(f"[SOP沉淀] LLM 调用失败: {exc}，使用默认结构")
        return _default_sop_structure(sop_type)


def _default_sop_structure(sop_type: str) -> dict:
    """SOP 提取失败时的默认结构"""
    return {
        "sop_type": sop_type,
        "content": f"从会议总结中提取的 {sop_type} 标准操作流程（详细内容见会议总结）",
        "key_steps": ["准备会议材料", "进行会议讨论", "确认行动项", "整理会议记录"],
        "preconditions": ["会议召集人已确认参会人员", "议程已提前分发"],
        "failure_cases": ["会议超时未形成结论", "行动项未指定负责人"],
    }


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

        # SOP 沉淀检查（APPROVED 触发）
        _try_precipitate_sop(state, self.store_dir)

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
