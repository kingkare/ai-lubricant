"""任务层 · ORM 行为测试（sqlite::memory:，**不连 PG**）。

验的是**真行为**不是类型：创建 → 写方案 → 确定 → 执行 → 阻塞/恢复 → 收口。

照 ``tests/test_assets_env_question.py`` 的既有做法：``@pytest_asyncio.fixture``
+ 测试显式取 ``tortoise_db`` 参数 + 每个测试 ``@pytest.mark.asyncio``。
只注册本轮新增的两个模型模块，不牵连热区。

**staging 翻转不在这里**：``staging.py`` 用 PG 专有 SQL（``information_schema`` /
``$1`` 占位符），sqlite 跑不了——由 ``test_taskflow_service_regression.py``（mock）覆盖。
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from testing_platform.taskflow import service as tf
from testing_platform.taskflow.models import TestTaskRun


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，只注册 taskflow + comments 模型模块。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.taskflow.models",
                "testing_platform.comments.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _mk(**kw):
    return await tf.create_task(
        project_id=kw.pop("project_id", uuid.uuid4()),
        type=kw.pop("type", "explore"),
        title=kw.pop("title", "验证用探索任务"),
        scope=kw.pop("scope", {"purpose": "fill_cases", "direction": ["api"]}),
        created_by=kw.pop("created_by", uuid.uuid4()),
        **kw,
    )


@pytest.mark.asyncio
async def test_创建后是草稿态(tortoise_db):
    created = await _mk()
    assert created["status"] == "draft"
    assert created["phase_status"] == "create"
    assert created["code"].startswith("TRUN-")
    assert created["scope"]["purpose"] == "fill_cases"


@pytest.mark.asyncio
async def test_写方案进待确定段(tortoise_db):
    created = await _mk()
    planned = await tf.update_plan(
        created["id"],
        plan={"steps": [{"role": "planner"}], "focus": ["补用例"]},
        status_to="planned",
    )
    assert planned["status"] == "planned"
    assert planned["phase_status"] == "confirm"
    assert planned["plan_revision"] == 1
    assert planned["plan"]["focus"] == ["补用例"]


@pytest.mark.asyncio
async def test_确定方案进执行段(tortoise_db):
    created = await _mk()
    await tf.update_plan(created["id"], plan={}, status_to="planned")
    pid = uuid.uuid4()
    confirmed = await tf.confirm_plan(created["id"], confirmed_by=pid)
    assert confirmed["status"] == "executing"
    assert confirmed["phase_status"] == "execute"
    assert confirmed["confirmed_by"] == str(pid)
    assert confirmed["started_at"]


@pytest.mark.asyncio
async def test_进度可更新(tortoise_db):
    created = await _mk()
    progressed = await tf.set_progress(
        created["id"], progress={"steps": [{"role": "executor", "state": "running"}]}
    )
    assert progressed["progress"]["steps"][0]["state"] == "running"


@pytest.mark.asyncio
async def test_终态无出边(tortoise_db):
    created = await _mk()
    await tf.update_plan(created["id"], plan={}, status_to="planned")
    await tf.confirm_plan(created["id"], confirmed_by=uuid.uuid4())
    # executing → applying → completed（completed 不是 executing 的直接出边）
    await tf.transition(created["id"], to="applying")
    await tf.transition(created["id"], to="completed")
    with pytest.raises(tf.TaskflowError) as exc:
        await tf.transition(created["id"], to="planning")
    assert exc.value.code == "invalid_transition"


@pytest.mark.asyncio
async def test_blocked_记阻塞项并可恢复(tortoise_db):
    created = await _mk()
    await tf.update_plan(created["id"], plan={}, status_to="planned")
    await tf.confirm_plan(created["id"], confirmed_by=uuid.uuid4())

    blocked = await tf.transition(
        created["id"],
        to="blocked",
        blockers=[{"kind": "credential", "name": "admin_password", "why": "缺登录账号"}],
    )
    assert blocked["status"] == "blocked"
    assert blocked["blockers"][0]["name"] == "admin_password"

    resumed = await tf.transition(created["id"], to="executing")
    assert resumed["status"] == "executing"


@pytest.mark.asyncio
async def test_执行入口护栏只放行executing(tortoise_db):
    created = await _mk()
    with pytest.raises(tf.TaskflowError) as exc:
        await tf.assert_executable(created["id"])
    assert exc.value.code == "invalid_state"

    await tf.update_plan(created["id"], plan={}, status_to="planned")
    await tf.confirm_plan(created["id"], confirmed_by=uuid.uuid4())
    ok = await tf.assert_executable(created["id"])
    assert ok["status"] == "executing"


@pytest.mark.asyncio
async def test_方案可迭代且版本号递增(tortoise_db):
    created = await _mk()
    await tf.update_plan(created["id"], plan={"v": 1})
    second = await tf.update_plan(created["id"], plan={"v": 2})
    assert second["plan_revision"] == 2
    assert second["plan"]["v"] == 2


@pytest.mark.asyncio
async def test_列表按类型过滤(tortoise_db):
    pid = uuid.uuid4()
    await _mk(project_id=pid, type="explore", title="探索 A")
    await _mk(project_id=pid, type="gen_case", title="用例 B")
    explores = await tf.list_tasks(pid, type="explore")
    cases = await tf.list_tasks(pid, type="gen_case")
    assert explores and all(r["type"] == "explore" for r in explores)
    assert cases and all(r["type"] == "gen_case" for r in cases)


@pytest.mark.asyncio
async def test_任务行可查库(tortoise_db):
    created = await _mk()
    row = await TestTaskRun.get_or_none(id=uuid.UUID(created["id"]))
    assert row is not None
    assert row.type == "explore"
    assert row.code.startswith("TRUN-")


@pytest.mark.asyncio
async def test_不存在的任务报not_found(tortoise_db):
    with pytest.raises(tf.TaskflowError) as exc:
        await tf.get_task(uuid.uuid4())
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_未知类型被拒(tortoise_db):
    with pytest.raises(tf.TaskflowError) as exc:
        await _mk(type="bogus")
    assert exc.value.code == "invalid_type"
