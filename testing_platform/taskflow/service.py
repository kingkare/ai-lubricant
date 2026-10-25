"""任务层服务（26 号文 §1.4–§1.5）——基于 Tortoise ORM。

只做编排/校验/状态流转；**不碰 LLM、不跑 agent**（那是调用方注入的事）。
``methods.py``（surface 层）调这里，或前端 ``callTestingMethod`` 经 surface 调这里。

状态流转护栏在 :mod:`.models`（``can_transition``），本模块只负责落库。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from tortoise.exceptions import DoesNotExist

from .models import (
    PHASE_OF_STATUS,
    TASK_TYPES,
    TestTaskRun,
    can_transition,
)
from .staging import (
    STAGED_TABLES,
    apply_staged as _apply_staged,
    count_staged as _count_staged,
    revert_staged as _revert_staged,
)

__all__ = [
    "TaskflowError",
    "list_tasks",
    "get_task",
    "create_task",
    "update_plan",
    "confirm_plan",
    "transition",
    "set_progress",
    "apply_task_outputs",
    "revert_task_outputs",
    "count_staged_outputs",
    "task_run_document",
]


class TaskflowError(Exception):
    """任务层校验失败（未知类型/非法流转/任务不存在）。"""

    def __init__(self, message: str, *, code: str = "taskflow_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _as_uuid(value: Any, *, field: str = "id") -> _uuid.UUID:
    if isinstance(value, _uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        raise TaskflowError(f"{field} 不能为空", code="invalid_params")
    try:
        return _uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise TaskflowError(f"无效 {field}：{value!r}", code="invalid_params") from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def task_run_document(run: TestTaskRun) -> dict:
    """任务行 → JSON 文档（面板渲染源）。"""
    return {
        "id": str(run.id),
        "code": run.code,
        "project_id": str(run.project_id),
        "type": run.type,
        "status": run.status,
        "phase_status": PHASE_OF_STATUS.get(run.status, run.phase_status),
        "title": run.title,
        "note": run.note,
        "scope": dict(run.scope or {}),
        "plan": dict(run.plan or {}),
        "plan_revision": run.plan_revision,
        "resources": dict(run.resources or {}),
        "progress": dict(run.progress or {}),
        "blockers": list(run.blockers or []),
        "refs": dict(run.refs or {}),
        "env_id": str(run.env_id) if run.env_id else None,
        "created_by": str(run.created_by) if run.created_by else None,
        "confirmed_by": str(run.confirmed_by) if run.confirmed_by else None,
        "confirmed_at": run.confirmed_at.isoformat() if run.confirmed_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "version": run.version,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


async def _next_code(project_id: Any) -> str:
    """项目内自增 TRUN-xxxx（用 count+1 试探，冲突则递增重试几次）。"""
    pid = _as_uuid(project_id, field="project_id")
    base = await TestTaskRun.filter(project_id=pid).count()
    n = base + 1
    for _ in range(20):
        code = f"TRUN-{n:04d}"
        if not await TestTaskRun.filter(project_id=pid, code=code).exists():
            return code
        n += 1
    raise TaskflowError("任务发号冲突过多", code="code_exhausted")


async def list_tasks(
    project_id: Any,
    *,
    type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """任务列表（面板按类型/状态过滤）。"""
    pid = _as_uuid(project_id, field="project_id")
    qs = TestTaskRun.filter(project_id=pid)
    if type:
        qs = qs.filter(type=type)
    if status:
        qs = qs.filter(status=status)
    rows = await qs.order_by("-created_at").limit(max(1, int(limit)))
    return [task_run_document(r) for r in rows]


async def get_task(task_id: Any) -> dict:
    tid = _as_uuid(task_id)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        raise TaskflowError(f"任务不存在：{tid}", code="not_found")
    return task_run_document(run)


async def create_task(
    *,
    project_id: Any,
    type: str,
    title: str,
    note: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    env_id: Optional[Any] = None,
    resources: Optional[Mapping[str, Any]] = None,
    refs: Optional[Mapping[str, Any]] = None,
    created_by: Optional[Any] = None,
) -> dict:
    """创建任务（draft 态）。type 决定 scope 形状（26 号文 §1.6）。"""
    if type not in TASK_TYPES:
        raise TaskflowError(f"未知任务类型：{type!r}（合法：{TASK_TYPES}）", code="invalid_type")
    pid = _as_uuid(project_id, field="project_id")
    code = await _next_code(pid)
    run = await TestTaskRun.create(
        code=code,
        project_id=pid,
        type=type,
        status="draft",
        phase_status="create",
        title=title,
        note=note or "",
        scope=dict(scope or {}),
        plan={},
        resources=dict(resources or {}),
        progress={},
        blockers=[],
        refs=dict(refs or {}),
        env_id=_as_uuid(env_id, field="env_id") if env_id else None,
        created_by=_as_uuid(created_by, field="created_by") if created_by else pid,
    )
    return task_run_document(run)


def _transition_or_raise(run: TestTaskRun, to: str) -> None:
    """校验流转合法，非法抛错（不直接改状态——调用方 save）。

    **同步函数**（不是 async）：内部无 await。早期写成 ``async def`` 而三处调用
    都漏了 ``await`` → 护栏静默失效（协程从未执行），非法流转被放行。
    2026-10-22 由行为测试（test_taskflow_e2e.test_终态无出边）暴露并修正。
    """
    if not can_transition(run.status, to):
        raise TaskflowError(
            f"非法流转：{run.status} → {to}", code="invalid_transition"
        )


async def update_plan(
    task_id: Any,
    *,
    plan: Mapping[str, Any],
    status_to: Optional[str] = None,
    blockers: Optional[Iterable[Mapping[str, Any]]] = None,
    resources: Optional[Mapping[str, Any]] = None,
) -> dict:
    """写/改方案（生成方案阶段 agent 调；确定方案前可反复改）。

    :param status_to: 可选，同时把任务推进到该态（如 planning→planned）。
    :param blockers: 可选，覆盖写依赖清单（进 blocked 时填）。
    """
    tid = _as_uuid(task_id)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        raise TaskflowError(f"任务不存在：{tid}", code="not_found")
    run.plan = dict(plan or {})
    run.plan_revision = (run.plan_revision or 0) + 1
    if resources is not None:
        run.resources = dict(resources)
    if blockers is not None:
        run.blockers = [dict(b) for b in blockers]
    if status_to:
        _transition_or_raise(run, status_to)
        run.status = status_to
        run.phase_status = PHASE_OF_STATUS.get(status_to, run.phase_status)
    await run.save()
    return task_run_document(run)


async def confirm_plan(task_id: Any, *, confirmed_by: Any) -> dict:
    """确定方案（planned → executing），记录确认人/时间。"""
    tid = _as_uuid(task_id)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        raise TaskflowError(f"任务不存在：{tid}", code="not_found")
    _transition_or_raise(run, "executing")
    run.status = "executing"
    run.phase_status = "execute"
    run.confirmed_by = _as_uuid(confirmed_by, field="confirmed_by")
    run.confirmed_at = _now()
    run.started_at = run.started_at or _now()
    await run.save()
    return task_run_document(run)


async def transition(
    task_id: Any,
    *,
    to: str,
    reason: Optional[str] = None,
    blockers: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict:
    """通用状态流转（执行进度/阻塞/应用/完成/失败/取消）。

    ``to='blocked'`` 时把 ``blockers`` + ``reason`` 写进 blockers 列（理由 4 停下等人）。
    ``to='completed'`` 记 finished_at。
    """
    tid = _as_uuid(task_id)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        raise TaskflowError(f"任务不存在：{tid}", code="not_found")
    _transition_or_raise(run, to)
    run.status = to
    run.phase_status = PHASE_OF_STATUS.get(to, run.phase_status)
    if to == "blocked":
        if blockers is not None:
            run.blockers = [dict(b) for b in blockers]
        elif reason:
            run.blockers = list(run.blockers or []) + [{"kind": "runtime", "message": reason}]
        run.resume_from = "executing"
    if to in ("completed", "failed", "cancelled"):
        run.finished_at = _now()
        if to == "failed":
            run.fail_from_phase = run.phase_status
    if to == "executing":
        run.started_at = run.started_at or _now()
    await run.save()
    return task_run_document(run)


async def assert_executable(task_id: Any) -> dict:
    """**执行入口护栏**（2026-10-22 加，探索编排器报的缺口）：这个任务现在能不能被执行。

    判据：状态必须是 ``executing``（= 方案已被用户确定）。
    ``draft``/``planning`` = 方案还没出或还没确定；``planned`` = 等人确认；
    终态 = 已结束；``blocked`` = 缺依赖等人。

    :raises TaskflowError: 不可执行（``code="invalid_state"``）。
    :returns: 任务文档（可执行时）。
    """
    tid = _as_uuid(task_id)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        raise TaskflowError(f"任务不存在：{tid}", code="not_found")
    if run.status != "executing":
        raise TaskflowError(
            f"任务当前状态 {run.status!r} 不可执行（须先确定方案进入 executing）",
            code="invalid_state",
        )
    return task_run_document(run)


async def set_progress(task_id: Any, *, progress: Mapping[str, Any]) -> dict:
    """更新执行进度（面板唯一渲染源；每步 agent 状态）。"""
    tid = _as_uuid(task_id)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        raise TaskflowError(f"任务不存在：{tid}", code="not_found")
    run.progress = dict(progress or {})
    await run.save()
    return task_run_document(run)


# ── 应用/回滚产物（D-6：翻标识，不搬表）─────────────────────────

async def apply_task_outputs(task_id: Any) -> dict[str, int]:
    """应用该任务的 staged 产物（staged→false，全局可见）+ 收尾流转。

    **流转与翻转解耦**（2026-10-22 修，探索编排器报的坑）：staged 翻转**总是执行**；
    状态收尾看当前态——``executing`` → 经 applying 进 completed；已在 ``applying``
    （调用方先显式 transition 过来的，如探索编排器）→ 只补 completed；
    其它态不动流转（面板拿到的仍是正确的当前态）。
    """
    tid = _as_uuid(task_id)
    result = await _apply_staged(tid)
    run = await TestTaskRun.get_or_none(id=tid)
    if run is None:
        return result
    if run.status == "executing":
        await transition(tid, to="applying")
        await transition(tid, to="completed")
    elif run.status == "applying":
        await transition(tid, to="completed")
    return result


async def revert_task_outputs(task_id: Any, *, action: str = "restage") -> dict[str, int]:
    """回滚该任务产物（restage 翻回 / delete 删除，仅未应用的）。"""
    tid = _as_uuid(task_id)
    return await _revert_staged(tid, action=action)


async def count_staged_outputs(task_id: Any) -> dict[str, int]:
    """该任务未应用产物计数（面板徽章）。"""
    tid = _as_uuid(task_id)
    return await _count_staged(tid)
