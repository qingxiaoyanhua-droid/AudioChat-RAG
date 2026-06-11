"""
混合持久化存储 — MySQL + Redis 合一

职责分层：
  MySQL  — source of truth，ACID 事务，保证核心数据不丢失
  Redis  — 高速缓存，承接实时读写，降低 MySQL 负载

读写路径：
  写操作：
    1. 写入 Redis（毫秒级，线程安全）
    2. 异步线程写入 MySQL（最终一致）
  读操作：
    1. 读 Redis（Cache-Aside）
    2. 未命中 → 读 MySQL → 回填 Redis

故障恢复：
  - Redis 故障 → 自动降级到 MySQL 直读直写
  - Redis 数据丢失 → 从 MySQL 批量恢复
  - MySQL 故障 → 不处理（MySQL 是 source of truth，MySQL 挂了系统不可用）

幂等保证：
  - approve_and_send: 检查 email_result.sent，幂等跳过
  - save: MySQL upsert，不会因重复写入报错
"""

from __future__ import annotations

import atexit
import json
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Iterator, Optional

from audiochat.workflow.cache_redis import RedisCache, RedisConfig
from audiochat.workflow.db_mysql import MySQLConfig, MySQLStore
from audiochat.workflow.state import Actor, AuditAction, EmailResult, TaskState, TaskStatus


# ============================================================================
# MySQL 配置（需要用户初始化时传入）
# ============================================================================


class HybridTaskStore:
    """
    MySQL + Redis 混合持久化任务存储

    设计原则：
      - Redis 承接所有实时读写（低延迟）
      - MySQL 作为 source of truth（高可靠）
      - 写操作：Redis + 异步 MySQL
      - 读操作：Cache-Aside（先 Redis，未命中读 MySQL 并回填）
      - 故障降级：Redis 不可用时自动降级到 MySQL

    幂等保证：
      - 所有写操作天然幂等（MySQL upsert + Redis SET）
      - approve_and_send 检查 email_result.sent
    """

    def __init__(
        self,
        mysql_config: Optional[MySQLConfig] = None,
        redis_config: Optional[RedisConfig] = None,
        async_write: bool = True,
        json_fallback_dir: str = "./workflow_tasks",
    ):
        self.mysql: Optional[MySQLStore] = None
        self.redis: Optional[RedisCache] = None
        self.async_write = async_write

        # 异步写队列（单生产者-单消费者模式）
        self._write_queue: queue.Queue = queue.Queue()
        self._write_thread: Optional[threading.Thread] = None
        self._running: bool = False

        # JSON 文件降级（MySQL/Redis 都不可用时）
        self._json_fallback_dir = Path(json_fallback_dir)
        self._json_lock = threading.RLock()

        # 初始化存储后端
        if mysql_config:
            self.mysql = MySQLStore(config=mysql_config)
            self.mysql.initialize()

        if redis_config:
            self.redis = RedisCache(config=redis_config)
            try:
                self.redis.initialize()
            except Exception as e:
                print(f"[HybridTaskStore] Redis 初始化失败，降级到 MySQL/JSON: {e}")
                self.redis = None

        # 启动异步写线程
        if self.async_write and self.mysql:
            self._start_async_writer()

        # 注册退出清理
        atexit.register(self.close)

    # =========================================================================
    # 异步写线程
    # =========================================================================

    def _start_async_writer(self) -> None:
        """启动后台异步写 MySQL 线程"""
        self._running = True
        self._write_thread = threading.Thread(
            target=self._async_write_loop,
            daemon=True,
            name="HybridStore-AsyncWriter",
        )
        self._write_thread.start()
        print(f"[HybridTaskStore] 异步写线程已启动（batch_size=50, flush_interval=2s）")

    def _async_write_loop(self) -> None:
        """
        后台异步写循环。

        策略：批量攒写，每 2 秒或队列满 50 条，写入 MySQL。
        用批量写入减少 MySQL 连接次数，提高吞吐。
        """
        batch: list[TaskState] = []
        last_flush_time = time.time()
        FLUSH_INTERVAL = 2.0     # 每 2 秒强制刷新
        BATCH_SIZE = 50

        while self._running or not self._write_queue.empty():
            try:
                # 带超时的队列读取，timeout 期间批量收集
                try:
                    state = self._write_queue.get(timeout=0.5)
                    batch.append(state)
                except queue.Empty:
                    pass

                # 触发刷新条件：时间到 或 批量满
                elapsed = time.time() - last_flush_time
                if batch and (elapsed >= FLUSH_INTERVAL or len(batch) >= BATCH_SIZE):
                    self._flush_batch(batch)
                    batch = []
                    last_flush_time = time.time()

            except Exception as e:
                print(f"[HybridTaskStore] 异步写异常: {e}\n{traceback.format_exc()}")
                time.sleep(1)

        # 退出前刷新剩余数据
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: list[TaskState]) -> None:
        """批量写入 MySQL"""
        if not self.mysql:
            return
        success = 0
        for state in batch:
            try:
                self.mysql.save(state)
                success += 1
            except Exception as e:
                print(f"[HybridTaskStore] MySQL 写入失败 task_id={state.task_id}: {e}")
        if success > 0:
            print(f"[HybridTaskStore] 批量写入 MySQL: {success}/{len(batch)} 条成功")

    def close(self) -> None:
        """优雅关闭：停止异步写线程，等待队列排空"""
        self._running = False
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=5)
        if self.mysql:
            self.mysql.close()
        if self.redis:
            self.redis.close()

    # =========================================================================
    # 公共 API（对外暴露的读写接口）
    # =========================================================================

    def save(self, state: TaskState) -> None:
        """
        保存任务状态（核心写接口）。

        路径：
          1. 写入 Redis（同步，毫秒级）
          2. 异步写入 MySQL（后台攒批写入）

        如果 Redis 不可用，只写 MySQL。
        如果 MySQL 也不可用，降级到 JSON 文件。
        """
        state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Step 1: 写 Redis（必须成功）
        if self.redis:
            ok = self.redis.set(state)
            if not ok:
                print(f"[HybridTaskStore] Redis 写入失败，降级")

        # Step 2: 写 MySQL（异步，最终一致）
        if self.mysql:
            if self.async_write:
                # 异步：放入队列，不阻塞
                try:
                    self._write_queue.put_nowait(state)
                except queue.Full:
                    # 队列满，改为同步写
                    self.mysql.save(state)
            else:
                # 同步：直接写（需要强一致时用）
                self.mysql.save(state)
        else:
            # MySQL 不可用，降级 JSON
            self._save_json(state)

    def load(self, task_id: str) -> Optional[TaskState]:
        """
        加载任务状态（Cache-Aside 模式）。

        路径：
          1. 读 Redis → 命中 → 返回
          2. 读 Redis → 未命中 → 读 MySQL → 回填 Redis → 返回
          3. Redis/MySQL 都不可用 → 读 JSON → 返回
        """
        # Step 1: 读 Redis
        if self.redis:
            state = self.redis.get(task_id)
            if state is not None:
                return state

        # Step 2: 读 MySQL（Redis 未命中）
        if self.mysql:
            state = self.mysql.load(task_id)
            if state is not None:
                # 回填 Redis
                if self.redis:
                    self.redis.set(state)
                return state

        # Step 3: JSON 降级
        return self._load_json(task_id)

    def delete(self, task_id: str) -> bool:
        """删除任务（同时删除 Redis + MySQL + JSON）"""
        deleted = False

        if self.redis:
            self.redis.delete(task_id)
        if self.mysql:
            deleted = self.mysql.delete(task_id)
        self._delete_json(task_id)

        return deleted

    def exists(self, task_id: str) -> bool:
        """检查任务是否存在"""
        if self.redis and self.redis.exists(task_id):
            return True
        if self.mysql and self.mysql.exists(task_id):
            return True
        return self._exists_json(task_id)

    # =========================================================================
    # 批量查询
    # =========================================================================

    def all_task_ids(self) -> list[str]:
        """
        返回所有任务 ID。

        策略：优先用 MySQL（source of truth），
        Redis 只用于实时索引加速。
        """
        if self.mysql:
            return self.mysql.all_task_ids()
        if self.redis:
            all_ids: set[str] = set()
            for status in TaskStatus:
                ids = self.redis.list_by_status(status, limit=10000)
                all_ids.update(ids)
            return list(all_ids)
        # JSON 降级
        return self._all_json_ids()

    def list_by_status(self, status: TaskStatus) -> list[TaskState]:
        """按状态查询任务（优先 Redis ZSET 索引）"""
        task_ids: list[str] = []

        if self.redis:
            ids = self.redis.list_by_status(status, limit=1000)
            task_ids.extend(ids)

        if self.mysql and len(task_ids) < 100:
            mysql_states = self.mysql.list_by_status(status)
            mysql_ids = {s.task_id for s in mysql_states}
            # 合并去重
            merged_ids = list(set(task_ids) | mysql_ids)
        elif self.mysql:
            return self.mysql.list_by_status(status)
        else:
            return self._list_by_status_json(status)

        # 逐个加载（先从 Redis 批量命中）
        results: list[TaskState] = []
        missing_ids: list[str] = []
        for tid in task_ids:
            if self.redis:
                s = self.redis.get(tid)
            else:
                s = None
            if s:
                results.append(s)
            else:
                missing_ids.append(tid)

        # 未命中部分从 MySQL 补
        if self.mysql:
            for tid in missing_ids:
                s = self.mysql.load(tid)
                if s:
                    results.append(s)
                    if self.redis:
                        self.redis.set(s)

        return results

    def list_pending_approval(self) -> list[TaskState]:
        """返回所有待审核任务（按创建时间正序）"""
        return self.list_by_status(TaskStatus.PENDING_APPROVAL)

    def list_recent(self, limit: int = 20) -> list[TaskState]:
        """
        返回最近 N 个任务（按更新时间倒序）。

        优先从 Redis recent ZSET 获取，未命中再从 MySQL 补充。
        """
        # Step 1: 从 Redis recent ZSET 拿
        recent_ids: list[str] = []
        if self.redis:
            recent_ids = self.redis.list_recent_ids(limit=limit)

        # Step 2: 逐条加载
        results: list[TaskState] = []
        missing_ids: list[str] = []
        for tid in recent_ids:
            if self.redis:
                s = self.redis.get(tid)
            else:
                s = None
            if s:
                results.append(s)
            else:
                missing_ids.append(tid)

        # Step 3: 未命中从 MySQL 补
        if self.mysql:
            mysql_states = self.mysql.list_recent(limit=limit + len(missing_ids))
            for s in mysql_states:
                if s.task_id not in [r.task_id for r in results]:
                    results.append(s)
                    if self.redis:
                        self.redis.set(s)

        # Step 4: 排序并截断
        results.sort(key=lambda s: s.updated_at or "", reverse=True)
        return results[:limit]

    # =========================================================================
    # 幂等操作
    # =========================================================================

    def approve_and_send(
        self,
        task_id: str,
        approver: str,
        sender_fn: Callable[[TaskState], EmailResult],
    ) -> dict:
        """
        幂等的 approve + 发送邮件。

        幂等保证：
          1. email_result.sent == True → 直接返回（不发第二封）
          2. MySQL upsert 确保并发安全
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

        # 幂等检查
        if state.email_result.sent:
            return {
                "ok": True,
                "idempotent": True,
                "message": "邮件已发送，跳过",
                "sent_at": state.email_result.sent_at,
                "sent_to": list(state.email_result.sent_to),
                "issue_urls": list(state.email_result.issue_urls),
            }

        # 正常流程
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
        """拒绝并触发重新生成"""
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

    # =========================================================================
    # 故障恢复
    # =========================================================================

    def recover_from_mysql(self) -> int:
        """
        从 MySQL 恢复 Redis 缓存。

        调用场景：
          - Redis 数据丢失（重启 flushdb 或 OOM）
          - Redis 重启后缓存为空
          - 系统首次启动
        """
        if not self.mysql or not self.redis:
            return 0

        task_ids = self.mysql.all_task_ids()
        recovered = self.redis.bulk_set([])
        total = 0

        # 分批从 MySQL 读取并回填 Redis
        BATCH = 100
        for i in range(0, len(task_ids), BATCH):
            batch_ids = task_ids[i:i + BATCH]
            states = self.mysql.bulk_recover_from_mysql(batch_ids)
            if states:
                n = self.redis.bulk_set(list(states.values()))
                total += n

        print(f"[HybridTaskStore] MySQL 恢复完成：{total}/{len(task_ids)} 条回填 Redis")
        return total

    def get_stats(self) -> dict:
        """返回存储统计（MySQL + Redis 双视角）"""
        mysql_stats = self.mysql.get_stats() if self.mysql else {}
        redis_health = self.redis.health_check() if self.redis else {}
        return {
            "mysql": mysql_stats,
            "redis": redis_health,
            "async_queue_size": self._write_queue.qsize() if self.async_write else 0,
        }

    # =========================================================================
    # JSON 降级（MySQL + Redis 都不可用时）
    # =========================================================================

    def _json_path(self, task_id: str) -> Path:
        self._json_fallback_dir.mkdir(parents=True, exist_ok=True)
        return self._json_fallback_dir / f"{task_id}.json"

    def _save_json(self, state: TaskState) -> None:
        with self._json_lock:
            path = self._json_path(state.task_id)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)
            tmp.replace(path)

    def _load_json(self, task_id: str) -> Optional[TaskState]:
        with self._json_lock:
            path = self._json_path(task_id)
            if not path.exists():
                return None
            with open(path, encoding="utf-8") as f:
                return TaskState.from_dict(json.load(f))

    def _delete_json(self, task_id: str) -> bool:
        with self._json_lock:
            path = self._json_path(task_id)
            if path.exists():
                path.unlink()
                return True
            return False

    def _exists_json(self, task_id: str) -> bool:
        return self._json_path(task_id).exists()

    def _all_json_ids(self) -> list[str]:
        self._json_fallback_dir.mkdir(parents=True, exist_ok=True)
        return [p.stem for p in self._json_fallback_dir.iterdir() if p.suffix == ".json"]

    def _list_by_status_json(self, status: TaskStatus) -> list[TaskState]:
        results = []
        for tid in self._all_json_ids():
            s = self._load_json(tid)
            if s and s.status == status:
                results.append(s)
        return results

    def clear_all(self) -> int:
        """清空所有数据（慎用，仅测试）"""
        count = 0
        if self.redis:
            self.redis.clear_all()
        if self.mysql:
            for tid in self.mysql.all_task_ids():
                self.mysql.delete(tid)
        for p in self._json_fallback_dir.glob("*.json"):
            p.unlink()
            count += 1
        return count
