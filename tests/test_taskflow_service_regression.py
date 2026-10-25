"""任务层 service 回归测试（mock 化，**不连库**）。

聚焦集成时**真踩过并修过**的行为坑，不是类型检查：

1. ``apply_task_outputs`` 在「任务已在 applying」时**不能**把任务卡在 applying。
   旧实现 ``if can_transition(status, "applying")`` 整块跳过 → staged 翻了但状态
   永不落 completed（探索编排器 agent 报的坑，已修）。
2. ``assert_executable`` 只放行 ``executing``（各编排器共用一道护栏，
   不再各写各的）。

mock 手法照 ``tests/test_taskflow_explore.py``：替换 service 内的 ORM 读函数与
staging 翻转函数，不连库。
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from testing_platform.taskflow import service as tf


def _doc(status: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": status,
        "phase_status": "execute",
        "code": "TRUN-0001",
        "type": "explore",
    }


# service 在模块顶部 ``from .staging import apply_staged as _apply_staged`` 按值绑定，
# 所以必须 patch ``tf._apply_staged``（不是 ``staging.apply_staged``——后者改了
# service 里的旧引用不会更新，是真会踩的坑）。
_APPLY = "_apply_staged"


def _fake_run_get(status: str):
    """伪造 ``TestTaskRun.get_or_none`` 返回指定状态的一行。

    字段必须覆盖 ``service.task_run_document`` 读的全部属性，否则转换时会 AttributeError。
    """

    async def _get(*a, **kw):
        return SimpleNamespace(
            id=uuid.uuid4(),
            code="TRUN-0001",
            project_id=uuid.uuid4(),
            type="explore",
            status=status,
            phase_status="execute",
            title="验证",
            note="",
            scope={},
            plan={},
            plan_revision=0,
            resources={},
            progress={},
            blockers=[],
            refs={},
            env_id=None,
            created_by=None,
            confirmed_by=None,
            confirmed_at=None,
            started_at=None,
            finished_at=None,
            version=1,
            created_at=None,
            updated_at=None,
        )

    return _get


async def _fake_apply_staged(task_run_id):
    return {"mc_test_case": 3}


# ── 1. apply_task_outputs 不卡 applying ───────────────────────────


@pytest.mark.asyncio
async def test_apply_when_executing走完整两段(monkeypatch):
    """executing → applying → completed。"""
    seen: list[str] = []

    async def fake_transition(tid, *, to, **kw):
        seen.append(to)
        return _doc(to)

    monkeypatch.setattr(tf, _APPLY, _fake_apply_staged)
    monkeypatch.setattr(tf, "transition", fake_transition)
    monkeypatch.setattr(tf, "TestTaskRun", SimpleNamespace(get_or_none=_fake_run_get("executing")))

    await tf.apply_task_outputs(uuid.uuid4())
    assert seen == ["applying", "completed"], seen


@pytest.mark.asyncio
async def test_apply_when_already_applying仍补completed(monkeypatch):
    """**回归**：调用方已先 ``transition(applying)`` 过来时，必须补 completed。

    旧实现这里 ``can_transition("applying","applying")=False`` → 整块跳过 → 卡住。
    """
    seen: list[str] = []

    async def fake_transition(tid, *, to, **kw):
        seen.append(to)
        return _doc(to)

    monkeypatch.setattr(tf, _APPLY, _fake_apply_staged)
    monkeypatch.setattr(tf, "transition", fake_transition)
    monkeypatch.setattr(tf, "TestTaskRun", SimpleNamespace(get_or_none=_fake_run_get("applying")))

    await tf.apply_task_outputs(uuid.uuid4())
    assert seen == ["completed"], f"applying 态应只补 completed，实际 {seen}"


@pytest.mark.asyncio
async def test_apply_产物翻转总是执行即便状态不合(monkeypatch):
    """staged 翻转与状态流转解耦：已 completed 的任务再调，**翻转照做**、流转不动。"""
    flipped = False
    seen: list[str] = []

    async def fake_apply_staged(task_run_id):
        nonlocal flipped
        flipped = True
        return {"mc_test_case": 1}

    async def fake_transition(tid, *, to, **kw):
        seen.append(to)
        return _doc(to)

    monkeypatch.setattr(tf, _APPLY, fake_apply_staged)
    monkeypatch.setattr(tf, "transition", fake_transition)
    monkeypatch.setattr(tf, "TestTaskRun", SimpleNamespace(get_or_none=_fake_run_get("completed")))

    out = await tf.apply_task_outputs(uuid.uuid4())
    assert flipped is True, "产物翻转不应被状态判断跳过"
    assert seen == [], f"终态不该再流转，实际 {seen}"
    assert out == {"mc_test_case": 1}


# ── 2. assert_executable 护栏 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_只有executing可执行(monkeypatch):
    for status in ("draft", "planning", "planned", "blocked", "applying",
                   "completed", "failed", "cancelled"):
        monkeypatch.setattr(tf, "TestTaskRun", SimpleNamespace(get_or_none=_fake_run_get(status)))
        with pytest.raises(tf.TaskflowError) as exc:
            await tf.assert_executable(uuid.uuid4())
        assert exc.value.code == "invalid_state", status

    monkeypatch.setattr(tf, "TestTaskRun", SimpleNamespace(get_or_none=_fake_run_get("executing")))
    ok = await tf.assert_executable(uuid.uuid4())
    assert ok["status"] == "executing"


@pytest.mark.asyncio
async def test_不存在任务报not_found(monkeypatch):
    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(tf, "TestTaskRun", SimpleNamespace(get_or_none=_none))
    with pytest.raises(tf.TaskflowError) as exc:
        await tf.assert_executable(uuid.uuid4())
    assert exc.value.code == "not_found"


# ── 3. 空值护栏 ───────────────────────────────────────────────────


def test_as_uuid_拒空值():
    """防误删全表的第一道闸（staging._task_run_id 同款语义）。"""
    for bad in ("", None, "   "):
        with pytest.raises(tf.TaskflowError):
            tf._as_uuid(bad, field="task_id")


def test_as_uuid_接受合法值():
    u = uuid.uuid4()
    assert tf._as_uuid(u) == u
    assert tf._as_uuid(str(u)) == u
