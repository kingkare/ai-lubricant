"""Workflow launch-method scheduler (independent APScheduler singleton).

A workflow declares its **launch methods** (启动方式) in the definition:
``triggers: [{id, kind: manual|cron|agent, cron?, args?, enabled?}]``. Each cron
trigger carries its own args — a nightly sweep and an hourly probe of the same
graph diverge only by what they pass to ``$args.*``.

The scheduler registers one APS job per enabled ``cron`` trigger of every
workflow's latest version. Job id encodes ``(def_id, trigger_id)`` so two
triggers of one workflow coexist and edits replace precisely. Other kinds don't
schedule: ``manual`` fires from the UI run button, ``agent`` fires from
``workflow.run`` — both resolve their trigger's args at fire time (see
:mod:`agent.workflow_service`).

Independent singleton, separate from AgentScheduler. One process, one instance
(:func:`get_workflow_scheduler`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from db import PostgresClient


def build_trigger(expr: str):
    """将调度表达式转换为 APScheduler 触发器。

    支持：
    - 标准 cron（5 位）：'0 9 * * 1-5'
    - repeat 关键字：daily/weekday/weekly/monthly/hourly/once
    - every_N：every_30m / every_2h / every_1d
    """
    e = expr.strip().lower()

    if e == "daily":
        return CronTrigger(hour=0, minute=0)
    if e == "weekday":
        return CronTrigger(day_of_week="mon-fri", hour=0, minute=0)
    if e == "weekly":
        return CronTrigger(day_of_week="mon", hour=0, minute=0)
    if e == "monthly":
        return CronTrigger(day=1, hour=0, minute=0)
    if e == "hourly":
        return CronTrigger(minute=0)
    if e == "once":
        # 立即触发一次（5 秒后）
        from datetime import datetime, timedelta
        return DateTrigger(run_date=datetime.now() + timedelta(seconds=5))

    if e.startswith("every_"):
        suffix = e[6:]
        unit = suffix[-1]
        try:
            value = int(suffix[:-1])
        except ValueError:
            raise ValueError(f"Invalid every_N expression: {expr}")
        if unit == "h":
            return IntervalTrigger(hours=value)
        elif unit == "m":
            return IntervalTrigger(minutes=value)
        elif unit == "d":
            return IntervalTrigger(days=value)
        raise ValueError(f"Invalid every_N unit '{unit}': {expr}")

    # 默认当作 cron 表达式
    return CronTrigger.from_crontab(expr)


logger = logging.getLogger(__name__)

_singleton: "WorkflowScheduler | None" = None

# Job id namespace: "wf-<def_id>-<trigger_id>". trigger_id 已在 validate_triggers
# 限成标识符，拼进 job id 不会引入分隔符歧义。
_WF_JOB_PREFIX = "wf-"


def _job_def_id(job_id: str) -> int | None:
    """``wf-<def_id>-<trigger_id>`` → def_id；解析不出返回 None。

    def_id 是纯数字且在第一段，所以按前缀后**第一个**分隔符切（trigger_id 无
    破折号，历史实现用 rsplit 只是为了配对前缀，这里取整数值必须用 partition）。
    """
    if not job_id.startswith(_WF_JOB_PREFIX):
        return None
    head, sep, _tid = job_id[len(_WF_JOB_PREFIX):].partition("-")
    if not sep:
        return None
    try:
        return int(head)
    except ValueError:
        return None


def get_workflow_scheduler() -> "WorkflowScheduler":
    global _singleton
    if _singleton is None:
        _singleton = WorkflowScheduler()
    return _singleton


def _job_listener(event: JobExecutionEvent) -> None:
    if event.exception:
        logger.error("[workflow-scheduler] job %s error: %s", event.job_id, event.exception)
    else:
        logger.info("[workflow-scheduler] job %s fired", event.job_id)


async def _fire(def_id: int, trigger_id: str) -> None:
    """One scheduled fire: load the def's latest version + owner, kick a run
    with **that trigger's** args (启动方式各自带参)."""
    from agent import workflow_service, workflow_store
    from agent.workflow import validate_triggers

    if not PostgresClient.pool:
        return
    def_row = await workflow_store.get_def(def_id)
    if def_row is None:
        return
    definition = def_row.get("definition") or {}
    if isinstance(definition, str):
        try:
            import json
            definition = json.loads(definition)
        except ValueError:
            definition = {}
    trigger = next((t for t in validate_triggers(definition or {}) if t["id"] == trigger_id), None)
    if trigger is None or not trigger["enabled"] or trigger["kind"] != "cron":
        # Trigger deleted/disabled between fires; reload will unregister it.
        return
    logger.info("[workflow-scheduler] firing workflow def=%s trigger=%s", def_id, trigger_id)
    try:
        # 无人值守运行以 **def 的 owner** 身份跑（不是 admin）：run_session 挑该
        # owner 的 agent、web 驱动该 owner 的浏览器、MCP 走该 owner 的用户身份 token。
        # 这是工作流「与 agent 解耦」在 cron 侧的落点——归属是用户，不是 agent。
        await workflow_service.start_and_run_multi(
            def_id,
            args={},
            version=int(def_row["version"]),
            trigger=f"cron:{trigger_id}",
            caller=(str(def_row["user_id"]) if def_row.get("user_id") else None),
            cron=str(trigger.get("cron") or ""),
            trigger_id=trigger_id,
        )
    except Exception as exc:  # noqa: BLE001 - one bad fire must not stop the schedule
        logger.exception("[workflow-scheduler] workflow %s fire failed: %s", def_id, exc)
    finally:
        # Record last_run_at so the runs list reflects scheduler activity.
        try:
            await _stamp_last_run(def_id)
        except Exception:  # noqa: BLE001
            pass


async def _stamp_last_run(def_id: int) -> None:
    if not PostgresClient.pool:
        return
    async with PostgresClient.pool.acquire() as conn:
        await conn.execute(
            "UPDATE workflow_defs SET last_run_at=now() WHERE id=$1", def_id,
        )


async def _compute_next_run(cron: str) -> Any:
    """Next fire timestamp for display, or None on a one-shot / invalid expr."""
    from datetime import datetime, timezone
    try:
        trig = build_trigger(cron)
        nxt = trig.get_next_fire_time(None, datetime.now(timezone.utc))
        return nxt.replace(tzinfo=None) if nxt else None
    except Exception:  # noqa: BLE001
        return None


def _cron_triggers_of(definition: Any) -> list[dict]:
    """A definition's enabled cron triggers (best-effort; bad defs schedule nothing)."""
    if isinstance(definition, str):
        try:
            import json
            definition = json.loads(definition)
        except ValueError:
            return []
    if not isinstance(definition, dict):
        return []
    try:
        from agent.workflow import validate_triggers
        return [t for t in validate_triggers(definition) if t["kind"] == "cron" and t["enabled"]]
    except Exception:  # noqa: BLE001 — 校验不过的定义不挂任何 job
        return []


class WorkflowScheduler:
    """APS wrapper for workflow cron triggers (one job per enabled cron trigger)."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(job_defaults={"coalesce": True, "max_instances": 1})
        self._scheduler.add_listener(_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self._started = False

    def start(self) -> None:
        if self._scheduler.running:
            return
        self._scheduler.start()
        self._started = True
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.reload())
        except RuntimeError:  # pragma: no cover - no loop yet
            asyncio.ensure_future(self.reload())
        logger.info("[workflow-scheduler] started")

    def shutdown(self, wait: bool = True) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)

    async def reload(self) -> None:
        """Reload every latest-version workflow's cron triggers from the DB."""
        pool = getattr(PostgresClient, "pool", None)
        if not pool or not self._scheduler.running:
            return
        # Latest version per (user_id, name) — a def's id stays the same across
        # versions, but names are only unique per owner now (workflows are
        # user-owned), so grouping by name alone could grab another owner's row.
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.* FROM workflow_defs d
                JOIN (
                    SELECT user_id, name, MAX(version) AS version FROM workflow_defs GROUP BY user_id, name
                ) m ON m.user_id IS NOT DISTINCT FROM d.user_id
                   AND m.name = d.name AND m.version = d.version
                """
            )
        # Remove jobs whose (def, trigger) no longer exists/enabled.
        wanted: dict[str, tuple[int, str, str]] = {}
        for row in rows:
            for t in _cron_triggers_of(row["definition"]):
                job_id = f"{_WF_JOB_PREFIX}{int(row['id'])}-{t['id']}"
                wanted[job_id] = (int(row["id"]), t["id"], t["cron"])
        for job in list(self._scheduler.get_jobs()):
            if not job.id.startswith(_WF_JOB_PREFIX):
                continue
            if job.id not in wanted:
                self._scheduler.remove_job(job.id)
        for job_id, (def_id, trigger_id, cron) in wanted.items():
            await self._upsert_job(job_id, def_id, trigger_id, cron)

    async def _upsert_job(self, job_id: str, def_id: int, trigger_id: str, cron: str) -> None:
        try:
            trig = build_trigger(cron)
        except Exception:  # noqa: BLE001
            logger.warning("[workflow-scheduler] bad cron for def %s trigger %s: %r", def_id, trigger_id, cron)
            return
        nxt = await _compute_next_run(cron)
        self._scheduler.add_job(
            _fire, trigger=trig,
            id=job_id,
            kwargs={"def_id": def_id, "trigger_id": trigger_id},
            replace_existing=True,
        )
        if PostgresClient.pool:
            async with PostgresClient.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE workflow_defs SET next_run_at=$2 WHERE id=$1",
                    def_id, nxt,
                )

    async def reload_def(self, def_id: int) -> None:
        """Re-register one workflow's triggers (after a save).

        **家族寻址**：保存 = INSERT 新版本行（新 id），而全量 ``reload()`` 用的是
        「家族最新行 id」给 job 命名。若这里按传入的（可能已过期的）def_id 命名，
        同一个 cron 会同时挂着 ``wf-<旧id>-trig`` 与 ``wf-<新id>-trig`` 两个 job
        =**定时重复触发**。所以：定义读最新版本、job 用最新行 id 命名、并清掉
        同家族所有版本行 id 的旧 job。
        """
        if not self._scheduler.running:
            return
        pool = getattr(PostgresClient, "pool", None)
        if not pool:
            return
        _FAMILY_IDS = (
            "SELECT d.id FROM workflow_defs d "
            "JOIN (SELECT user_id, name FROM workflow_defs WHERE id=$1) f "
            "ON d.user_id IS NOT DISTINCT FROM f.user_id AND d.name = f.name"
        )
        async with pool.acquire() as conn:
            latest = await conn.fetchrow(
                "SELECT id, definition FROM workflow_defs WHERE id IN (" + _FAMILY_IDS + ") "
                "ORDER BY version DESC LIMIT 1",
                def_id,
            )
            family_ids = {int(r["id"]) for r in await conn.fetch(_FAMILY_IDS, def_id)}
        family_ids.add(int(def_id))  # 锚点行已被删时也要能清掉它的残留 job
        wanted: dict[str, tuple[int, str, str]] = {}
        if latest is not None:
            lid = int(latest["id"])
            for t in _cron_triggers_of(latest["definition"]):
                wanted[f"{_WF_JOB_PREFIX}{lid}-{t['id']}"] = (lid, t["id"], t["cron"])
        for job in list(self._scheduler.get_jobs()):
            if not job.id.startswith(_WF_JOB_PREFIX):
                continue
            # 清掉「本家族任一版本行」名下的旧 job（含改名/停用/删掉的触发器）。
            owner = _job_def_id(job.id)
            if owner in family_ids and job.id not in wanted:
                self._scheduler.remove_job(job.id)
        for job_id, (d, tid, cron) in wanted.items():
            await self._upsert_job(job_id, d, tid, cron)

    async def remove(self, def_id: int) -> None:
        """Remove every job of one workflow (deleted / all triggers off) — **家族寻址**：
        传入任一版本行 id，同家族所有版本行名下的 job 都清掉。"""
        if not self._scheduler.running:
            return
        pool = getattr(PostgresClient, "pool", None)
        family_ids = {int(def_id)}
        if pool:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT d.id FROM workflow_defs d "
                    "JOIN (SELECT user_id, name FROM workflow_defs WHERE id=$1) f "
                    "ON d.user_id IS NOT DISTINCT FROM f.user_id AND d.name = f.name",
                    def_id,
                )
            family_ids.update(int(r["id"]) for r in rows)
        for job in list(self._scheduler.get_jobs()):
            owner = _job_def_id(job.id)
            if owner is not None and owner in family_ids:
                self._scheduler.remove_job(job.id)


__all__ = ["get_workflow_scheduler", "WorkflowScheduler", "build_trigger"]
