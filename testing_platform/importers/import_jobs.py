"""接口导入任务执行器 + 定时调度（``mc_test_api_import_job``）。

一条导入任务 = 「文档来源（spec_url / spec_text）+ 导入方式（source_format，含
apifox）+ 节奏（schedule）」。本模块负责两件事：

* **执行**（:func:`run_import_job`）：取文档（URL 走服务端抓取；文本直接用）→
  调 :func:`import_openapi_to_apis`（**显式 format，不猜**）→ 写 ``last_*``。
  手动导入的**分批进度**由前端循环调 ``api.import_openapi``（带 offset/limit）
  驱动，这里只负责一次跑完。
* **调度**（:func:`start_import_scheduler` / :func:`stop_import_scheduler`）：
  asyncio 后台循环，周期扫 ``due_import_jobs()``（enabled 且 next_run_at 到期），
  串行执行（同 tick 内一个失败不影响其余），每次跑完由 service 重算 next_run_at。

``schedule.kind=manual`` 的任务永远不会被调度器拾起（``next_run_at`` 为空），只能
手动 run。定时任务必须走 ``spec_url``（``spec_text`` 是浏览器里的粘贴内容，服务端
无会话拿不到）——建任务时已在 service 层拦住。

⚠️ 并发纪律：本模块只做「读任务→执行→写状态」，不碰接口数据以外的表；接口写入
全部走既有 ``import_openapi_to_apis``（幂等键 project+method+path）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..assets.service import (
    AssetNotFoundError,
    due_import_jobs,
    get_import_job,
    mark_import_job_run,
)
from .openapi_api_importer import import_openapi_to_apis
from .openapi_importer import OpenAPIImportError, fetch_spec_url

logger = logging.getLogger(__name__)

#: 调度扫描周期（秒）。next_run_at 精确到分钟级，60s 足够。
POLL_INTERVAL_SECONDS = 60.0

#: 单任务执行超时（秒）——URL 抓取 30s + 导入本身。
RUN_TIMEOUT_SECONDS = 300.0

# ── 调度器单例 ──────────────────────────────────────────────────
_task: Optional[asyncio.Task] = None
_stopping = False


async def resolve_job_spec(job: Any) -> Any:
    """取任务文档：URL 优先（服务端抓取），否则 spec_text。"""
    if getattr(job, "spec_url", None):
        return await fetch_spec_url(str(job.spec_url))
    text = getattr(job, "spec_text", None)
    if not text:
        raise OpenAPIImportError("导入任务既没有 spec_url 也没有 spec_text")
    return text


async def run_import_job(
    job_id: Any,
    *,
    dry_run: bool = False,
    created_by: Optional[Any] = None,
) -> dict:
    """立即执行一条导入任务（服务端一次跑完）。

    流程：取任务 → 取文档（URL 抓取或原文）→ 按 ``source_format`` 显式导入 →
    写 ``last_run_at/last_status/last_message/last_result`` 并重算 ``next_run_at``。

    ``dry_run=True`` 只预览（不写任务状态）。返回导入结果 dict。
    """
    job = await get_import_job(job_id)
    try:
        spec = await resolve_job_spec(job)
        result = await asyncio.wait_for(
            import_openapi_to_apis(
                spec,
                job.project_id,
                format=job.source_format,
                dry_run=dry_run,
                created_by=created_by or job.created_by,
                job_id=job.id,
                spec_url=job.spec_url,
            ),
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 —— 任何失败都要落到任务状态上
        if not dry_run:
            await mark_import_job_run(job.id, status="error", message=f"{type(exc).__name__}: {exc}")
        raise
    if not dry_run:
        summary = f"新建 {result.get('created', 0)} · 更新 {result.get('updated', 0)}"
        await mark_import_job_run(
            job.id,
            status="ok",
            message=summary,
            result={k: v for k, v in result.items() if k != "endpoints"},
        )
    return result


# ── 后台调度 ────────────────────────────────────────────────────
async def _tick() -> int:
    """一轮扫描：执行所有到期任务，返回执行条数（单条失败不阻断其余）。"""
    jobs = await due_import_jobs()
    done = 0
    for job in jobs:
        # 先把 next_run_at 推到下一轮，避免执行慢导致同一轮重复拾取
        try:
            await mark_import_job_run(job.id, status="running", message="执行中")
        except AssetNotFoundError:
            continue  # 任务已被删除
        try:
            await run_import_job(job.id)
            done += 1
        except Exception as exc:  # noqa: BLE001 —— 调度循环必须吞异常继续
            logger.warning("[api-import] 任务 %s 执行失败：%s", job.id, exc)
    return done


async def _loop(interval: float) -> None:
    logger.info("[api-import] 导入任务调度器启动（周期 %ss）", interval)
    while not _stopping:
        try:
            await asyncio.sleep(interval)
            n = await _tick()
            if n:
                logger.info("[api-import] 本轮执行导入任务 %d 条", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 循环不能死
            logger.exception("[api-import] 调度轮异常")


async def start_import_scheduler(poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
    """启动后台调度循环（main.py lifespan 调；重复调用幂等）。"""
    global _task, _stopping  # noqa: PLW0603
    if _task is not None and not _task.done():
        return
    _stopping = False
    _task = asyncio.create_task(_loop(poll_interval), name="api-import-scheduler")


async def stop_import_scheduler() -> None:
    """停止调度循环。"""
    global _task, _stopping  # noqa: PLW0603
    _stopping = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


__all__ = [
    "resolve_job_spec",
    "run_import_job",
    "start_import_scheduler",
    "stop_import_scheduler",
]
