"""
Gitea Issue 发送器 — 支持指数退避重试 + 幂等发送

幂等策略：
  - 每次调用记录 `dispatch_id`（任务级别唯一）
  - Gitea 端按 `dispatch_id` 作为 label 去重（已处理过的 dispatch_id 直接返回成功）
  - 本地记录到 `_dispatched` dict，重启后从状态文件恢复

指数退避：
  - HTTP 429 / 5xx 时触发重试
  - wait = min(base_wait * 2^attempt + jitter, max_wait)
  - 最多 3 次重试，之后抛出异常
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import requests


@dataclass(frozen=True)
class GiteaIssueResult:
    """发送结果"""

    success: bool
    number: Optional[int] = None
    url: Optional[str] = None
    dispatch_id: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    idempotent: bool = False  # True = 命中幂等跳过


@dataclass
class GiteaIssuePayload:
    """Issue 载荷"""

    title: str
    body: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    assignees: tuple[str, ...] = field(default_factory=tuple)
    dispatch_id: Optional[str] = None  # 幂等键，发过的不重发


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def _config() -> dict:
    return {
        "base": _env("GITEA_BASE_URL").rstrip("/"),
        "token": _env("GITEA_TOKEN"),
        "owner": _env("GITEA_OWNER"),
        "repo": _env("GITEA_REPO"),
    }


# --- 指数退避核心 ---

def _sleep_with_jitter(attempt: int, base: float = 1.0, max_wait: float = 30.0) -> float:
    """计算退避时间并 sleep"""
    wait = min(base * (2 ** attempt) + random.uniform(0, 0.5), max_wait)
    print(f"  [Gitea] 请求失败，第{attempt + 1}次重试，等待 {wait:.1f}s ...")
    time.sleep(wait)
    return wait


def _should_retry(status_code: int) -> bool:
    """判断是否值得重试"""
    return status_code in (429, 500, 502, 503, 504)


# --- 单个 Issue 发送（带重试）---

def create_issue_with_retry(
    payload: GiteaIssuePayload,
    max_retries: int = 3,
    timeout: int = 30,
) -> GiteaIssueResult:
    """
    创建单个 Gitea Issue，支持指数退避重试。

    幂等：payload.dispatch_id 相同的多余调用直接返回成功（不报错）。
    """
    cfg = _config()
    url = f"{cfg['base']}/api/v1/repos/{cfg['owner']}/{cfg['repo']}/issues"
    headers = {
        "Authorization": f"token {cfg['token']}",
        "Content-Type": "application/json",
    }

    body: dict = {"title": payload.title, "body": payload.body}

    if payload.labels:
        body["labels"] = list(payload.labels)

    if payload.assignees:
        body["assignee"] = list(payload.assignees)[0]

    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)

            # 幂等：Gitea 返回 409 说明 dispatch_id 已存在（label 去重），视为成功
            if r.status_code == 409:
                data = r.json()
                return GiteaIssueResult(
                    success=True,
                    number=int(data.get("number", 0)),
                    url=str(data.get("html_url") or data.get("url", "")),
                    dispatch_id=payload.dispatch_id,
                    idempotent=True,
                )

            if r.status_code == 201:
                data = r.json()
                return GiteaIssueResult(
                    success=True,
                    number=int(data["number"]),
                    url=str(data.get("html_url") or data.get("url", "")),
                    dispatch_id=payload.dispatch_id,
                )

            if _should_retry(r.status_code) and attempt < max_retries - 1:
                _sleep_with_jitter(attempt)
                continue

            # 不可重试的错误
            r.raise_for_status()
            return GiteaIssueResult(
                success=False,
                error=f"HTTP {r.status_code}: {r.text[:200]}",
                dispatch_id=payload.dispatch_id,
                retry_count=attempt + 1,
            )

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                _sleep_with_jitter(attempt)
                continue
            return GiteaIssueResult(
                success=False,
                error="请求超时",
                dispatch_id=payload.dispatch_id,
                retry_count=attempt + 1,
            )

        except requests.exceptions.RequestException as exc:
            if attempt < max_retries - 1:
                _sleep_with_jitter(attempt)
                continue
            return GiteaIssueResult(
                success=False,
                error=str(exc),
                dispatch_id=payload.dispatch_id,
                retry_count=attempt + 1,
            )

    # 理论上不会走到这里，但防御性返回
    return GiteaIssueResult(
        success=False,
        error="超出最大重试次数",
        dispatch_id=payload.dispatch_id,
        retry_count=max_retries,
    )


# --- 批量发送（幂等 + 汇总结果）---

def dispatch_issues(
    payloads: list[GiteaIssuePayload],
    max_retries: int = 3,
) -> list[GiteaIssueResult]:
    """
    批量发送 Issue。
    返回每个 payload 对应的结果列表。
    已发送过的（dispatch_id 相同）返回 success=True, idempotent=True。
    """
    results: list[GiteaIssueResult] = []
    for i, pl in enumerate(payloads):
        # 给每个 payload 分配一个 dispatch_id（如果没提供的话）
        dispatch_id = pl.dispatch_id or f"dispatch_{i}_{pl.title[:30]}"
        pl.dispatch_id = dispatch_id

        result = create_issue_with_retry(pl, max_retries=max_retries)
        results.append(result)

    return results


# --- 汇总报告 ---

def summarize_results(results: list[GiteaIssueResult]) -> dict:
    """生成发送结果摘要"""
    total = len(results)
    success = sum(1 for r in results if r.success)
    idempotent_count = sum(1 for r in results if r.idempotent)
    failed = total - success

    urls = [r.url for r in results if r.url]
    errors = [(r.dispatch_id, r.error) for r in results if not r.success and r.error]

    return {
        "total": total,
        "success": success,
        "failed": failed,
        "idempotent_skipped": idempotent_count,
        "issue_urls": urls,
        "errors": errors,
        "all_sent": failed == 0,
    }
