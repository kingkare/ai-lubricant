"""任务域服务测试（``02-task-model.md`` + design §15.10）。

覆盖：code 自增 / CRUD / 标签选例算法（all/any/none）+ case_filter → 场景去重 /
``run_task`` 建 run 行。无 PG 依赖（sqlite::memory:，Tortoise 建表）；无网络依赖。

运行：``python -m pytest tests/test_tasks_service.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid as _uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:，app label 用 ``tasks.models``（含 assets 模型）。"""
    from tortoise import Tortoise

    # 先 init assets 模型（任务依赖用例/场景关联表）
    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.assets.models", "testing_platform.tasks.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 1) code 自增 TASK-0001 / RUN-0001（按 project 隔离）──────────
@pytest.mark.asyncio
async def test_code_autoincrement_per_project(tortoise_db):
    from testing_platform.tasks import create_task, run_task

    pid1, pid2 = _uuid.uuid4(), _uuid.uuid4()
    me = _uuid.uuid4()

    t1 = await create_task(project_id=pid1, title="一", created_by=me)
    t2 = await create_task(project_id=pid1, title="二", created_by=me)
    t_other = await create_task(project_id=pid2, title="别的项目", created_by=me)
    assert (t1.code, t2.code, t_other.code) == ("TASK-0001", "TASK-0002", "TASK-0001")

    r1 = await run_task(t1.id, trigger="manual")
    r2 = await run_task(t1.id, trigger="scheduled")
    r_other = await run_task(t_other.id, trigger="api")
    assert (r1.code, r2.code, r_other.code) == ("RUN-0001", "RUN-0002", "RUN-0001")


# ── 2) CRUD + 状态机 + exec_type/schedule 校验 ──────────────────
@pytest.mark.asyncio
async def test_crud_and_status_machine(tortoise_db):
    from testing_platform.tasks import (
        TaskNotFoundError,
        create_task,
        disable_task,
        enable_task,
        get_task,
        list_tasks,
        update_task,
    )

    pid = _uuid.uuid4()
    me = _uuid.uuid4()

    task = await create_task(
        project_id=pid,
        title="原标题",
        created_by=me,
        purpose="验证什么",
        scenario_tags={"all": ["smoke"]},
        case_filter={},
        exec_type="scheduled",
        schedule={"kind": "cron", "expr": "0 2 * * *"},
        global_params={"version": "v1.0"},
        notify={"on": ["fail"], "channels": [{"type": "feishu", "webhook_ref": "x"}]},
        status="draft",
    )
    assert task.status == "draft"
    assert task.version == 1

    enabled = await enable_task(task.id, change_reason="上线")
    assert enabled.status == "enabled"
    assert enabled.version == 2

    disabled = await disable_task(enabled.id, change_reason="暂停")
    assert disabled.status == "disabled"
    assert disabled.version == 3

    updated = await update_task(
        disabled.id,
        change_reason="改配置",
        title="新标题",
        scenario_tags={"any": ["payment", "order"]},
    )
    assert updated.title == "新标题"
    assert updated.scenario_tags == {"any": ["payment", "order"]}
    assert updated.version == 4

    with pytest.raises(TaskNotFoundError):
        await get_task(_uuid.uuid4())

    tasks = await list_tasks(pid)
    assert len(tasks) == 1
    assert tasks[0].title == "新标题"

    # 坏 schedule 保存时即报错
    with pytest.raises(Exception):
        await create_task(
            project_id=pid,
            title="坏 cron",
            created_by=me,
            exec_type="scheduled",
            schedule={"kind": "cron", "expr": "bad"},
        )


# ── 3) 标签选例算法：all/any/none（02 §3）───────────────────────
@pytest.mark.asyncio
async def test_select_by_tags_all_any_none(tortoise_db):
    from testing_platform.assets.service import (
        create_case,
        create_scenario,
        create_tag,
        link_case_scenario,
        tag_case,
    )
    from testing_platform.tasks import create_task, select_by_tags

    pid = _uuid.uuid4()
    me = _uuid.uuid4()

    # 造用例 + 标签 + 场景
    tag_smoke = await create_tag(project_id=pid, name="smoke", category="purpose")
    tag_payment = await create_tag(project_id=pid, name="payment", category="module")
    tag_order = await create_tag(project_id=pid, name="order", category="module")
    tag_wip = await create_tag(project_id=pid, name="wip", category="purpose")

    case1 = await create_case(project_id=pid, title="TC1", confirm_source="debug", created_by=me, status="active", automation_status="automated")
    case2 = await create_case(project_id=pid, title="TC2", confirm_source="debug", created_by=me, status="active", automation_status="automated")
    case3 = await create_case(project_id=pid, title="TC3", confirm_source="debug", created_by=me, status="active", automation_status="automated")
    case4 = await create_case(project_id=pid, title="TC4", confirm_source="debug", created_by=me, status="active", automation_status="automated")

    s1 = await create_scenario(project_id=pid, name="SCN1")
    s2 = await create_scenario(project_id=pid, name="SCN2")
    s3 = await create_scenario(project_id=pid, name="SCN3")

    # TC1: smoke+payment → SCN1
    await tag_case(case1.id, tag_smoke.id)
    await tag_case(case1.id, tag_payment.id)
    await link_case_scenario(case1.id, s1.id, is_primary=True)

    # TC2: smoke+order → SCN2
    await tag_case(case2.id, tag_smoke.id)
    await tag_case(case2.id, tag_order.id)
    await link_case_scenario(case2.id, s2.id, is_primary=True)

    # TC3: payment+order → SCN3
    await tag_case(case3.id, tag_payment.id)
    await tag_case(case3.id, tag_order.id)
    await link_case_scenario(case3.id, s3.id, is_primary=True)

    # TC4: wip（被 none 过滤）→ SCN4（未挂，不影响）
    await tag_case(case4.id, tag_wip.id)

    # all: [smoke] → TC1/TC2 → SCN1/SCN2
    task1 = await create_task(
        project_id=pid,
        title="all smoke",
        created_by=me,
        scenario_tags={"all": ["smoke"]},
        exec_type="manual",
    )
    scenarios1 = await select_by_tags(task1)
    assert set(scenarios1) == {str(s1.id), str(s2.id)}

    # any: [payment, order] → TC1/TC2/TC3 → SCN1/SCN2/SCN3
    task2 = await create_task(
        project_id=pid,
        title="any module",
        created_by=me,
        scenario_tags={"any": ["payment", "order"]},
        exec_type="manual",
    )
    scenarios2 = await select_by_tags(task2)
    assert set(scenarios2) == {str(s1.id), str(s2.id), str(s3.id)}

    # none: [wip] → 排除 TC4 → SCN1/SCN2/SCN3
    task3 = await create_task(
        project_id=pid,
        title="none wip",
        created_by=me,
        scenario_tags={"none": ["wip"]},
        exec_type="manual",
    )
    scenarios3 = await select_by_tags(task3)
    assert set(scenarios3) == {str(s1.id), str(s2.id), str(s3.id)}

    # all+any 组合：all=[smoke] ∩ any=[payment,order] → TC1(smoke+payment)/TC2(smoke+order)
    task4 = await create_task(
        project_id=pid,
        title="all+any",
        created_by=me,
        scenario_tags={"all": ["smoke"], "any": ["payment", "order"]},
        exec_type="manual",
    )
    scenarios4 = await select_by_tags(task4)
    assert set(scenarios4) == {str(s1.id), str(s2.id)}


# ── 4) case_filter：module/priority/automation_status/status ────
@pytest.mark.asyncio
async def test_case_filter_module_priority(tortoise_db):
    from testing_platform.assets.service import (
        create_case,
        create_scenario,
        create_tag,
        link_case_scenario,
        tag_case,
    )
    from testing_platform.tasks import create_task, select_by_tags

    pid = _uuid.uuid4()
    me = _uuid.uuid4()

    tag_payment = await create_tag(project_id=pid, name="payment", category="module")
    tag_high = await create_tag(project_id=pid, name="P0", category="priority")

    case1 = await create_case(
        project_id=pid, title="TC1", confirm_source="debug", created_by=me,
        priority="P0", status="active", automation_status="automated",
    )
    case2 = await create_case(
        project_id=pid, title="TC2", confirm_source="debug", created_by=me,
        priority="P1", status="active", automation_status="automated",
    )
    case3 = await create_case(
        project_id=pid, title="TC3", confirm_source="debug", created_by=me,
        priority="P0", status="active", automation_status="automated",
    )

    s1 = await create_scenario(project_id=pid, name="SCN1")
    s2 = await create_scenario(project_id=pid, name="SCN2")

    await tag_case(case1.id, tag_payment.id)
    await tag_case(case1.id, tag_high.id)
    await link_case_scenario(case1.id, s1.id, is_primary=True)

    await tag_case(case2.id, tag_payment.id)
    await link_case_scenario(case2.id, s2.id, is_primary=True)

    await tag_case(case3.id, tag_payment.id)
    await link_case_scenario(case3.id, s1.id, is_primary=True)  # 同场景多引用

    # module=payment → TC1/TC2/TC3 → SCN1/SCN2（去重）
    task = await create_task(
        project_id=pid,
        title="module payment",
        created_by=me,
        scenario_tags={},
        case_filter={"module": ["payment"]},
        exec_type="manual",
    )
    scenarios = await select_by_tags(task)
    assert set(scenarios) == {str(s1.id), str(s2.id)}

    # module=payment ∩ priority=P0 → TC1/TC3 → SCN1（去重）
    task2 = await create_task(
        project_id=pid,
        title="module+priority",
        created_by=me,
        scenario_tags={},
        case_filter={"module": ["payment"], "priority": ["P0"]},
        exec_type="manual",
    )
    scenarios2 = await select_by_tags(task2)
    assert set(scenarios2) == {str(s1.id)}


# ── 5) run_task：选例 → 建 pending run 行（不真执行引擎）──────────
@pytest.mark.asyncio
async def test_run_task_creates_pending_run(tortoise_db):
    from testing_platform.assets.service import (
        create_case,
        create_scenario,
        create_tag,
        link_case_scenario,
        tag_case,
    )
    from testing_platform.tasks import create_task, run_task, select_by_tags

    pid = _uuid.uuid4()
    me = _uuid.uuid4()

    tag_smoke = await create_tag(project_id=pid, name="smoke", category="purpose")
    case1 = await create_case(project_id=pid, title="TC1", confirm_source="debug", created_by=me, status="active", automation_status="automated")
    case2 = await create_case(project_id=pid, title="TC2", confirm_source="debug", created_by=me, status="active", automation_status="automated")
    s1 = await create_scenario(project_id=pid, name="SCN1")
    s2 = await create_scenario(project_id=pid, name="SCN2")

    await tag_case(case1.id, tag_smoke.id)
    await tag_case(case2.id, tag_smoke.id)
    await link_case_scenario(case1.id, s1.id, is_primary=True)
    await link_case_scenario(case2.id, s2.id, is_primary=True)

    task = await create_task(
        project_id=pid,
        title="夜间回归",
        created_by=me,
        scenario_tags={"all": ["smoke"]},
        global_params={"test_version": "v1.0"},
        env_id=_uuid.uuid4(),
        exec_type="scheduled",
        schedule={"kind": "interval", "minutes": 30},
    )

    run = await run_task(task.id, trigger="scheduled")
    assert run.task_id == task.id
    assert run.trigger == "scheduled"
    assert run.status == "pending"
    assert run.global_params == {"test_version": "v1.0"}
    assert set(run.scenario_ids) == {str(s1.id), str(s2.id)}

    # 覆盖参数
    override_pid = _uuid.uuid4()
    run2 = await run_task(task.id, trigger="manual", env_id=override_pid)
    assert run2.env_id == override_pid


# ── 6) 通知校验：通道类型限平台既有能力（design §14.3）───────────
@pytest.mark.asyncio
async def test_notify_channel_validation(tortoise_db):
    from testing_platform.tasks import create_task

    pid = _uuid.uuid4()
    me = _uuid.uuid4()

    # 合法通道
    task = await create_task(
        project_id=pid,
        title="OK",
        created_by=me,
        notify={
            "on": ["always", "fail"],
            "channels": [
                {"type": "feishu", "webhook_ref": "x"},
                {"type": "dingtalk", "webhook_ref": "y"},
                {"type": "wecom", "webhook_ref": "z"},
                {"type": "webhook", "url": "https://hook"},
                {"type": "mobile_push", "token_ref": "t"},
            ],
        },
    )
    assert task.notify["on"] == ["always", "fail"]
    assert len(task.notify["channels"]) == 5

    # email 不在平台既有通道（design §14.3 已核对：无 email）
    with pytest.raises(Exception):
        await create_task(
            project_id=pid,
            title="Bad",
            created_by=me,
            notify={"on": ["fail"], "channels": [{"type": "email", "address": "a@b.com"}]},
        )


# ── 7) 定时解析：next_fire（纯函数）───────────────────────────────
@pytest.mark.asyncio
async def test_next_fire_pure_function():
    from datetime import datetime, timezone
    from testing_platform.tasks import next_fire

    base = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)

    # interval
    assert next_fire({"kind": "interval", "minutes": 30}, now=base) == datetime(2026, 10, 1, 10, 30, 0, tzinfo=timezone.utc)
    assert next_fire({"kind": "interval", "hours": 2}, now=base) == datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    # once：未来
    future = datetime(2026, 10, 2, 9, 0, 0, tzinfo=timezone.utc)
    assert next_fire({"kind": "once", "at": future.isoformat()}, now=base) == future

    # once：已过 → None
    past = datetime(2026, 9, 30, 9, 0, 0, tzinfo=timezone.utc)
    assert next_fire({"kind": "once", "at": past.isoformat()}, now=base) is None

    # cron：每天 2 点
    cron_schedule = {"kind": "cron", "expr": "0 2 * * *"}
    next_cron = next_fire(cron_schedule, now=base)
    assert next_cron.hour == 2
    assert next_cron.minute == 0

    # 空 schedule → None
    assert next_fire(None, now=base) is None
    assert next_fire({}, now=base) is None


# ── 8) 硬门槛：非 automated / 非 active 用例不进候选（02 §3 算法第 4 行）──
@pytest.mark.asyncio
async def test_selection_requires_active_and_automated(tortoise_db):
    from testing_platform.assets.service import create_case, create_scenario, link_case_scenario
    from testing_platform.tasks import create_task, select_by_tags

    pid = _uuid.uuid4()
    me = _uuid.uuid4()

    auto_case = await create_case(
        project_id=pid, title="自动化", confirm_source="debug", created_by=me,
        status="active", automation_status="automated",
    )
    manual_case = await create_case(
        project_id=pid, title="手工", confirm_source="debug", created_by=me,
        status="active", automation_status="not_automated",
    )
    draft_case = await create_case(
        project_id=pid, title="草稿", confirm_source="debug", created_by=me,
        status="draft", automation_status="automated",
    )

    s_auto = await create_scenario(project_id=pid, name="SCN-auto")
    s_manual = await create_scenario(project_id=pid, name="SCN-manual")
    s_draft = await create_scenario(project_id=pid, name="SCN-draft")
    await link_case_scenario(auto_case.id, s_auto.id, is_primary=True)
    await link_case_scenario(manual_case.id, s_manual.id, is_primary=True)
    await link_case_scenario(draft_case.id, s_draft.id, is_primary=True)

    # 无标签/无筛选：只剩 active + automated 的用例
    task = await create_task(project_id=pid, title="硬门槛", created_by=me, exec_type="manual")
    scenarios = await select_by_tags(task)
    assert scenarios == [str(s_auto.id)]


# ── 9) 表注册实证（sqlite_master）────────────────────────────────
@pytest.mark.asyncio
async def test_tables_registered(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {
        "mc_test_task",
        "mc_test_run",
        "mc_test_run_scenario",
        "mc_test_step_result",
        "mc_test_evidence",
    }
    assert expected <= names, f"缺表：{expected - names}"


