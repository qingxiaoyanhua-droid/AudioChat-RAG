"""
Redis 缓存层 — 实时任务状态的高速读写

职责：
  - 承接所有实时状态查询（毫秒级响应）
  - 高并发读写不打到 MySQL
  - MySQL 作为 source of truth，Redis 作为加速层
  - 故障时从 MySQL 恢复

Key 设计：
  - task:{task_id}  — 任务状态 JSON（TTL 1小时）
  - status:{status}  — 按状态索引的任务 ID 集合
  - recent:{yyyy-mm} — 按月索引的任务 ID 集合

一致性与持久化策略：
  - 写路径：双写（Redis + 异步 MySQL）
  - 读路径：Cache-Aside（先 Redis，未命中读 MySQL 回填）
  - 故障恢复：从 MySQL 批量读取回填 Redis
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional

import redis

from audiochat.workflow.state import TaskState, TaskStatus


# ============================================================================
# Key 规范
# ============================================================================

TASK_KEY_PREFIX = "task:"
STATUS_SET_PREFIX = "status:"
RECENT_SET_PREFIX = "recent:"
CACHE_TTL_SECONDS = 3600           # 1 小时过期


def _task_key(task_id: str) -> str:
    return f"{TASK_KEY_PREFIX}{task_id}"


def _status_set(status: TaskStatus) -> str:
    return f"{STATUS_SET_PREFIX}{status.value}"


def _recent_set() -> str:
    return f"{RECENT_SET_PREFIX}{time.strftime('%Y-%m')}"


def _status_to_str(status) -> str:
    if isinstance(status, TaskStatus):
        return status.value
    return str(status)


# ============================================================================
# Redis 配置
# ============================================================================


@dataclass
class RedisConfig:
    """Redis 连接配置"""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    socket_timeout: int = 5           # 5 秒超时
    socket_connect_timeout: int = 3    # 3 秒连接超时
    max_connections: int = 50
    retry_on_timeout: bool = True
    health_check_interval: int = 30   # 30 秒健康检查


class RedisCache:
    """
    Redis 缓存层

    核心设计：
      - task:{id} 存完整 JSON，TTL 1h
      - status:{status} 存 ZSET（按 updated_at 排序），便于范围查询
      - recent:yyyy-mm 存 ZSET（按 updated_at 排序），便于按月查询

    故障处理：
      - Redis 连接失败 → 降级到 MySQL 直接读写
      - Redis 数据丢失 → 通过 bulk_recover 批量从 MySQL 恢复
    """

    _local = threading.local()

    def __init__(self, config: Optional[RedisConfig] = None):
        self.config = config or RedisConfig()
        self._pool: Optional[redis.ConnectionPool] = None
        self._client: Optional[redis.Redis] = None
        self._dead: bool = False     # 标记 Redis 是否不可用

    def initialize(self) -> None:
        """初始化连接池"""
        self._pool = redis.ConnectionPool(
            host=self.config.host,
            port=self.config.port,
            db=self.config.db,
            password=self.config.password,
            socket_timeout=self.config.socket_timeout,
            socket_connect_timeout=self.config.socket_connect_timeout,
            max_connections=self.config.max_connections,
            retry_on_timeout=self.config.retry_on_timeout,
            health_check_interval=self.config.health_check_interval,
            decode_responses=True,
        )
        self._client = redis.Redis(connection_pool=self._pool)
        self._dead = False

    def close(self) -> None:
        """关闭连接池"""
        if self._client:
            self._client.close()
        if self._pool:
            self._pool.disconnect()

    def _is_dead(self) -> bool:
        """检查 Redis 是否已标记为不可用"""
        return self._dead

    def _mark_dead(self) -> None:
        """标记 Redis 不可用，后续操作降级到 MySQL"""
        if not self._dead:
            self._dead = True
            print(f"[RedisCache] Redis 已标记为不可用，降级到 MySQL 直读")

    def _try_connect(self) -> Optional[redis.Redis]:
        """获取 Redis 客户端，连接失败时降级"""
        if self._dead:
            return None
        if self._client is None:
            try:
                self.initialize()
            except Exception:
                self._mark_dead()
                return None
        return self._client

    # -------------------------------------------------------------------------
    # 读写操作
    # -------------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[TaskState]:
        """
        从 Redis 读取任务状态。

        Cache-Aside 模式：
          1. 查 Redis
          2. 命中 → 返回
          3. 未命中 → 返回 None（由调用方从 MySQL 读取并回填）
        """
        client = self._try_connect()
        if client is None:
            return None

        try:
            raw = client.get(_task_key(task_id))
            if raw is None:
                return None
            d = json.loads(raw)
            return TaskState.from_dict(d)
        except redis.RedisError as e:
            print(f"[RedisCache] 读取失败，降级: {e}")
            self._mark_dead()
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[RedisCache] JSON 解析失败: {e}")
            return None

    def set(self, state: TaskState) -> bool:
        """
        写入 Redis（高速路径）。

        操作：
          1. SET task:{id} JSON（TTL 1h）
          2. ZADD status:{status} task_id（按 updated_at 排序）
          3. ZADD recent:yyyy-mm task_id（按月索引）

        注意：这个方法只写 Redis，不写 MySQL。
        MySQL 由 HybridStore 在后台异步写入。
        """
        client = self._try_connect()
        if client is None:
            return False

        data = state.to_dict()
        task_key = _task_key(state.task_id)
        status_key = _status_set(state.status)
        recent_key = _recent_set()
        ts = time.time()

        pipe = client.pipeline(transaction=False)
        try:
            pipe.set(task_key, json.dumps(data, ensure_ascii=False), ex=CACHE_TTL_SECONDS)
            pipe.zadd(status_key, {state.task_id: ts})
            pipe.zadd(recent_key, {state.task_id: ts})
            pipe.execute()
            return True
        except redis.RedisError as e:
            print(f"[RedisCache] 写入失败，降级: {e}")
            self._mark_dead()
            return False

    def delete(self, task_id: str) -> bool:
        """从 Redis 删除任务状态"""
        client = self._try_connect()
        if client is None:
            return False

        try:
            # 找出当前 status 才能清理 status:{status} 集合
            raw = client.get(_task_key(task_id))
            if raw:
                d = json.loads(raw)
                status = _status_to_str(d.get("status", ""))
            else:
                status = None

            pipe = client.pipeline(transaction=False)
            pipe.delete(_task_key(task_id))
            if status:
                pipe.zrem(_status_set(TaskStatus(status)), task_id)
            pipe.zrem(_recent_set(), task_id)
            pipe.execute()
            return True
        except redis.RedisError as e:
            print(f"[RedisCache] 删除失败: {e}")
            self._mark_dead()
            return False

    def exists(self, task_id: str) -> bool:
        """检查 Redis 中是否存在"""
        client = self._try_connect()
        if client is None:
            return False
        try:
            return client.exists(_task_key(task_id)) > 0
        except redis.RedisError as e:
            print(f"[RedisCache] exists 失败: {e}")
            self._mark_dead()
            return False

    # -------------------------------------------------------------------------
    # 集合查询
    # -------------------------------------------------------------------------

    def list_by_status(self, status: TaskStatus, limit: int = 100) -> list[str]:
        """
        按状态查询任务 ID 列表。

        使用 ZRANGEBYSCORE 按 updated_at 倒序返回，
        支持分页（start=0, end=limit-1）。
        """
        client = self._try_connect()
        if client is None:
            return []

        try:
            key = _status_set(status)
            task_ids = client.zrevrange(key, 0, limit - 1)
            return task_ids
        except redis.RedisError as e:
            print(f"[RedisCache] list_by_status 失败: {e}")
            self._mark_dead()
            return []

    def list_recent_ids(self, limit: int = 20) -> list[str]:
        """返回最近 N 个任务 ID（按 updated_at 倒序）"""
        client = self._try_connect()
        if client is None:
            return []
        try:
            return client.zrevrange(_recent_set(), 0, limit - 1)
        except redis.RedisError as e:
            print(f"[RedisCache] list_recent_ids 失败: {e}")
            self._mark_dead()
            return []

    def bulk_set(self, states: list[TaskState]) -> int:
        """
        批量写入 Redis（Pipeline 模式）。

        用于 MySQL 恢复时批量回填 Redis，减少网络往返。
        """
        client = self._try_connect()
        if client is None:
            return 0

        pipe = client.pipeline(transaction=False)
        count = 0
        ts = time.time()
        for state in states:
            task_key = _task_key(state.task_id)
            data = state.to_dict()
            pipe.set(task_key, json.dumps(data, ensure_ascii=False), ex=CACHE_TTL_SECONDS)
            pipe.zadd(_status_set(state.status), {state.task_id: ts})
            pipe.zadd(_recent_set(), {state.task_id: ts})
            count += 1

        try:
            pipe.execute()
            return count
        except redis.RedisError as e:
            print(f"[RedisCache] bulk_set 失败: {e}")
            self._mark_dead()
            return 0

    def clear_all(self) -> bool:
        """清空所有缓存（仅测试用）"""
        client = self._try_connect()
        if client is None:
            return False
        try:
            keys = client.keys("task:*")
            if keys:
                client.delete(*keys)
            for k in client.keys("status:*"):
                client.delete(k)
            for k in client.keys("recent:*"):
                client.delete(k)
            return True
        except redis.RedisError as e:
            print(f"[RedisCache] clear_all 失败: {e}")
            self._mark_dead()
            return False

    def health_check(self) -> dict:
        """健康检查"""
        client = self._try_connect()
        if client is None:
            return {"status": "dead", "reason": "Redis connection failed"}
        try:
            client.ping()
            info = client.info("memory")
            return {
                "status": "ok",
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "connected_clients": info.get("connected_clients", 0),
            }
        except redis.RedisError as e:
            self._mark_dead()
            return {"status": "dead", "reason": str(e)}
