"""源同步调度器 —— ``sync_policy=scheduled`` 的 cron / ``webhook`` 触发（阶段 2 遗留）。

设计（``test-project-definition.md`` §六 同步触发）：**每种源配自己的触发，不一刀切**——
手动（页面点）/ 定时（cron）/ git push webhook / 按需（导入后立即触发）。

本模块只做两件事：

1. :func:`find_due_sources` —— 扫 ``mc_test_project_source`` 里 ``sync_policy=scheduled``
   且**到点**的源（复用 :func:`tasks.scheduler.next_fire`，纯函数，不引 croniter）。
2. :func:`run_source_sync` —— 对单个源执行同步（A 轨文件 → fetch_file；code 源 →
   代码快照 + Graphify 图谱）；**同步结果回写水位** ``last_synced_at`` / ``last_sync_result``。

后台进程（:class:`SourceSyncScheduler`）用共享 asyncio 任务循环跑；挂载闸门 =
库里存在 ``sync_policy='scheduled'`` 的源。**启动不阻断主服务**（挂载失败只记日志）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from tortoise.timezone import now as _tz_now

from .projects import mark_source_synced
from .projects.models import TestProjectSource
from .tasks.scheduler import next_fire

logger = logging.getLogger(__name__)

#: 扫描间隔（秒）。比到点粒度粗一点——cron 到点后最多晚这么久跑。
DEFAULT_POLL_SECONDS = 60


# ── 到点扫描 ────────────────────────────────────────────────────────────
def _naive(dt: datetime) -> datetime:
    """Tortoise 在 use_tz=False 下回朴素 datetime；比较前统一去 tz。"""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


async def find_due_sources(
    *, now: Optional[datetime] = None
) -> list[TestProjectSource]:
    """列出**已到点**的定时源。

    判据：``sync_policy='scheduled'`` 且 ``schedule`` 从**水位**起算的下一个触发点
    已经过去。水位 = ``last_synced_at``，从未同步则用 ``created_at``——
    新源不会立刻触发（"每天 9 点"的源建在 10 点，首个触发点是明天 9 点，符合 cron 语义）。
    """
    current = _naive(now or _tz_now())
    candidates = await TestProjectSource.filter(sync_policy="scheduled", status="active")
    due: list[TestProjectSource] = []
    for src in candidates:
        schedule = dict(src.schedule or {})
        if not schedule:
            continue
        watermark = src.last_synced_at or src.created_at
        if watermark is None:
            continue
        try:
            fire_at = next_fire(schedule, now=_naive(watermark))
        except ValueError:
            logger.warning("[source-sync] 源 %s schedule 非法：%r", src.id, schedule)
            continue
        if fire_at is None:
            continue
        if _naive(fire_at) > current:
            continue  # 下一个触发点还没到
        due.append(src)
    return due


# ── 单源同步（A 轨文件 / code 图谱）─────────────────────────────────────
async def run_source_sync(
    source: TestProjectSource, *, venv: Optional[str] = None
) -> dict[str, Any]:
    """执行一个源的同步，回写水位。**确定性、不调 LLM**。

    * A 轨（builtin）+ ``config.path`` 存在 → ``fetch_file`` 拉进 L1。
    * kind=code → 代码快照 + Graphify（读源的 ``config.git_id`` 关联的 git 配置）。
    * B 轨（agent）→ 无法由平台取内容，只记 ``skipped``（agent 自己取）。
    """
    result: dict[str, Any] = {"source_id": str(source.id), "kind": source.kind, "adapter": source.adapter}
    try:
        if source.adapter == "builtin" and source.config.get("path"):
            from .knowledge.source_fetch import fetch_file

            fetched = await fetch_file(
                project_id=source.project_id,
                source_id=source.id,
                kind="product" if source.kind == "prd" else source.kind,
                adapter_type=source.adapter_type or "text",
                path=str(source.config["path"]),
                provenance={"via": "scheduled", "schedule": dict(source.schedule or {})},
            )
            result.update({"action": "fetch_file", "raw_id": fetched.get("id"), "created": fetched.get("created")})
        elif source.kind == "code":
            # code 源走代码快照 + 图谱（git_id 在 config 里）
            git_id = source.config.get("git_id")
            if git_id:
                from .projects import init_snapshot
                from .knowledge import GraphifyAdapter

                snap = await init_snapshot(git_id)
                adapter = GraphifyAdapter(venv=venv)
                extraction = await adapter.run(snap["path"])
                stats = await adapter.upsert(
                    source.project_id, extraction.nodes, extraction.edges
                )
                result.update({
                    "action": "code_graph",
                    "commit": snap["commit"],
                    "anchors": extraction.graph.anchor_count,
                    "persisted": stats.created + stats.updated,
                })
            else:
                result.update({"action": "skipped", "reason": "code 源 config.git_id 为空"})
        elif source.kind == "openapi":
            # openapi 源：定时重新拉 spec URL → 幂等导入接口（同 code 走确定性同步）。
            url = source.config.get("url")
            if url:
                from .importers import fetch_spec_url, import_openapi

                text = await fetch_spec_url(str(url))
                rows = await import_openapi(text, source.project_id)
                result.update({"action": "import_openapi", "endpoints": len(rows)})
            else:
                result.update({"action": "skipped", "reason": "openapi 源 config.url 为空"})
        else:
            result.update({
                "action": "skipped",
                "reason": "B 轨（agent）由 agent 自己取内容，平台不取",
            })
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 — 单源失败不拖垮整批（同 sync_all 语义）
        result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # 回写水位（无论成败都记 —— 失败也算"同步过"，否则到点会疯狂重试）
    await mark_source_synced(source.id, result=result, status=source.status)
    return result


async def sync_due_sources(*, now: Optional[datetime] = None) -> dict[str, Any]:
    """扫到点的定时源并逐个同步。返回 ``{"due": n, "results": [...]}``。"""
    due = await find_due_sources(now=now)
    results: list[dict[str, Any]] = []
    for src in due:
        results.append(await run_source_sync(src))
    return {"due": len(due), "results": results}


# ── 后台 worker ─────────────────────────────────────────────────────────
class SourceSyncScheduler:
    """进程内源同步调度器（单例 asyncio 任务，仿 ``agent.runner`` 的挂载语义）。"""

    _task: Optional[asyncio.Task] = None

    @classmethod
    async def wanted(cls) -> bool:
        """启动闸门：库里存在 ``sync_policy='scheduled'`` 的 active 源。"""
        try:
            return await TestProjectSource.filter(sync_policy="scheduled", status="active").exists()
        except Exception:  # noqa: BLE001 — 表不存在/DB 抖动 → 按未启用
            logger.debug("[source-sync] wanted() 失败", exc_info=True)
            return False

    @classmethod
    async def start(cls, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        if cls._task is not None and not cls._task.done():
            return
        cls._task = asyncio.create_task(cls._loop(poll_seconds))
        logger.info("[source-sync] scheduler started (poll=%ss)", poll_seconds)

    @classmethod
    async def stop(cls) -> None:
        task, cls._task = cls._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                logger.debug("[source-sync] scheduler stopped", exc_info=True)

    @classmethod
    async def _loop(cls, poll_seconds: float) -> None:
        while True:
            await asyncio.sleep(poll_seconds)
            try:
                out = await sync_due_sources()
                if out["due"]:
                    ok = sum(1 for r in out["results"] if r.get("ok"))
                    logger.info("[source-sync] 同步 %s 个源（成功 %s）", out["due"], ok)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单轮失败不杀循环
                logger.exception("[source-sync] 同步轮失败")


async def maybe_start() -> bool:
    """按闸门决定是否启动（main 启动链路里调；**不阻断主服务**）。"""
    if await SourceSyncScheduler.wanted():
        await SourceSyncScheduler.start()
        return True
    return False


_started = False


async def ensure_started() -> bool:
    """惰性自启（幂等）：首次有人用源同步接口时把后台轮询拉起来。

    不依赖 main.py 的 lifespan —— 测试平台是独立能力域，启动挂点不往主服务里塞。
    闸门同 :func:`maybe_start`：库里没有定时源就什么都不做。
    """
    global _started
    if _started:
        return True
    started = await maybe_start()
    if started:
        _started = True
    return started


async def stop() -> None:
    global _started
    _started = False
    await SourceSyncScheduler.stop()


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "SourceSyncScheduler",
    "find_due_sources",
    "maybe_start",
    "run_source_sync",
    "stop",
    "sync_due_sources",
]
