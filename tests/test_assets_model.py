"""资产模型落库测试（``01-asset-model.md`` + design §15.12 变更门）。

覆盖：表建模 / code 自增 / 多对多链接（is_primary 单主）/ 变更门 +
``mc_test_change_record`` / ``PgScenarioLoader`` 喂 ``ScenarioExecutor``
跑通一个 http 桩场景。

无 PG 依赖（sqlite::memory:，Tortoise 建表）；无网络依赖（http 工具用桩替换）。
运行：``python -m pytest tests/test_assets_model.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.assets import (  # noqa: E402
    ChangeReasonRequiredError,
    PgScenarioLoader,
    create_case,
    create_scenario,
    create_steps,
    create_tag,
    get_scenario_full,
    link_case_scenario,
    list_case_scenarios,
    list_case_tags,
    list_changes,
    list_steps,
    record_change,
    tag_case,
    update_case,
    update_scenario,
)
from testing_platform.assets.models import TestChangeRecord  # noqa: E402
from testing_platform.engine import (  # noqa: E402
    ExecutionContext,
    ScenarioExecutor,
    ScenarioLoader,
    STATUS_ERROR,
    STATUS_PASS,
    STEP_PASS,
    ToolRegistry,
    ToolResult,
)
from testing_platform.engine.assertions import build_default_registry as build_assertions  # noqa: E402
from testing_platform.engine.tools import default_registry as default_tool_registry  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:，app label 用 ``models``（FK 串同款）。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.assets.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── http 工具桩（复刻 tests/test_engine_executor.py 的 FakeHttp 模式）──
class FakeHttp:
    """http 工具桩：POST 创建返回 201，GET 查询返回 200（复刻执行器测试）。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, step, ctx):
        params = step["params"]
        self.calls.append({"method": params.get("method"), "url": params.get("url")})
        status = 201 if params.get("method") == "POST" else 200
        return ToolResult(
            response={"status": status, "headers": {}, "body": {"data": {"order_id": "ORD-1", "status": "created"}}}
        )


def make_executor(loader, fake) -> ScenarioExecutor:
    registry = ToolRegistry()
    tools = default_tool_registry()
    registry.add(tools.get("http"))
    registry.add(tools.get("wait"))
    registry.add(tools.get("assert"))
    registry.register("http", step_types=("request",))(fake)
    return ScenarioExecutor(loader, tools=registry, assertions=build_assertions())


# ── 1) 建用例+场景+步骤 → get_scenario_full 组装正确（steps 按 seq 排序）──
@pytest.mark.asyncio
async def test_get_scenario_full_assembles_steps_sorted_by_seq(tortoise_db):
    pid = uuid.uuid4()
    me = uuid.uuid4()

    case = await create_case(
        project_id=pid,
        title="用户下单全流程",
        confirm_source="user_confirm",
        confirm_ref={"question_id": "q-1"},
        created_by=me,
        intent="验证下单闭环",
        preconditions={"user": "已登录"},
        expected={"status": 201, "order_id": "not_null"},
    )
    assert case.code == "TC-0001"
    assert case.confirm_source == "user_confirm"

    scenario = await create_scenario(
        project_id=pid,
        name="下单主流程",
        local_vars={"order_id": None, "retry_count": 0},
        flow={"pre_scenarios": ["SCN-LOGIN"], "early_exit": []},
    )
    assert scenario.code == "SCN-0001"

    # 故意乱序插入：seq=20 在前、seq=10 在后 —— 组装必须按 seq 排回
    steps = await create_steps(
        scenario.id,
        [
            {
                "step_key": "verify_order",
                "seq": 20,
                "type": "request",
                "description": "查询订单",
                "params": {"method": "GET", "url": "/api/v1/orders/{{local.order_id}}"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
            {
                "step_key": "create_order",
                "seq": 10,
                "type": "request",
                "description": "创建订单",
                "params": {"method": "POST", "url": "/api/v1/orders"},
                "post_checks": [
                    {"type": "status_code", "expected": 201},
                    {"type": "json_path", "path": "$.data.order_id", "operator": "not_null"},
                ],
            },
        ],
    )
    assert len(steps) == 2
    await link_case_scenario(case.id, scenario.id, is_primary=True, order=10)

    doc = await get_scenario_full(scenario.id)
    assert doc["code"] == "SCN-0001"
    assert [s["seq"] for s in doc["steps"]] == [10, 20]  # seq 唯一顺序真相
    assert [s["step_key"] for s in doc["steps"]] == ["create_order", "verify_order"]
    assert doc["steps"][0]["type"] == "request"
    assert doc["steps"][0]["post_checks"][0]["type"] == "status_code"
    assert doc["local_vars"] == {"order_id": None, "retry_count": 0}
    assert doc["flow"]["pre_scenarios"] == ["SCN-LOGIN"]
    assert doc["case_ids"] == [str(case.id)]  # 反向：该场景被用例引用
    # 未显式提供 seq 的步骤自动续排（场景内 max+10）
    auto = await create_steps(scenario.id, [{"step_key": "teardown", "type": "wait", "params": {"seconds": 1}}])
    assert auto[0].seq == 30

    db_steps = await list_steps(scenario.id)
    assert [s.seq for s in db_steps] == [10, 20, 30]


# ── 2) code 自增：TC-0001 → TC-0002；SCN-0001 → SCN-0002；按项目隔离 ──
@pytest.mark.asyncio
async def test_code_autoincrement_per_project(tortoise_db):
    pid1, pid2 = uuid.uuid4(), uuid.uuid4()
    me = uuid.uuid4()

    c1 = await create_case(project_id=pid1, title="一", confirm_source="debug", created_by=me)
    c2 = await create_case(project_id=pid1, title="二", confirm_source="debug", created_by=me)
    c_other = await create_case(project_id=pid2, title="别的项目", confirm_source="debug", created_by=me)
    assert (c1.code, c2.code, c_other.code) == ("TC-0001", "TC-0002", "TC-0001")

    s1 = await create_scenario(project_id=pid1, name="一")
    s2 = await create_scenario(project_id=pid1, name="二")
    s_other = await create_scenario(project_id=pid2, name="别的项目")
    assert (s1.code, s2.code, s_other.code) == ("SCN-0001", "SCN-0002", "SCN-0001")


# ── 3) 多对多链接 + is_primary（单主）+ 标签 ──
@pytest.mark.asyncio
async def test_link_case_scenario_and_tags(tortoise_db):
    pid = uuid.uuid4()
    me = uuid.uuid4()
    case = await create_case(project_id=pid, title="用例", confirm_source="doc", created_by=me)
    s1 = await create_scenario(project_id=pid, name="正常流")
    s2 = await create_scenario(project_id=pid, name="异常流")

    await link_case_scenario(case.id, s1.id, is_primary=True, order=10)
    # 第二个主场景 → 第一个自动让位（单主）
    await link_case_scenario(case.id, s2.id, is_primary=True, order=20)

    links = await list_case_scenarios(case.id)
    assert len(links) == 2
    primary = [l for l in links if l.is_primary]
    assert len(primary) == 1 and primary[0].scenario_id == s2.id

    # 幂等：重复链接不产生新行
    again = await link_case_scenario(case.id, s1.id, is_primary=False, order=15)
    assert again.id in {l.id for l in links}

    # 标签
    tag = await create_tag(project_id=pid, name="smoke", category="purpose")
    await tag_case(case.id, tag.id)
    await tag_case(case.id, tag.id)  # 幂等
    tags = await list_case_tags(case.id)
    assert [t.name for t in tags] == ["smoke"]

    # 场景反向 case_ids
    doc1 = await get_scenario_full(s1.id)
    doc2 = await get_scenario_full(s2.id)
    assert str(case.id) in doc1["case_ids"]
    assert str(case.id) in doc2["case_ids"]


# ── 4) 变更门：update 无 change_reason 抛错；有则写 change_record ──
@pytest.mark.asyncio
async def test_update_requires_change_reason(tortoise_db):
    pid = uuid.uuid4()
    me = uuid.uuid4()
    case = await create_case(project_id=pid, title="原标题", confirm_source="doc", created_by=me)
    scenario = await create_scenario(project_id=pid, name="原场景名")

    # 无 change_reason → 变更门拦截
    with pytest.raises(ChangeReasonRequiredError):
        await update_case(case.id, title="新标题")  # 缺 change_reason
    with pytest.raises(ChangeReasonRequiredError):
        await update_scenario(scenario.id, change_reason=None, name="新场景名")

    # 有 change_reason → 更新 + version 递增 + 写变更记录
    updated = await update_case(case.id, change_reason="标题不准确，改为业务口径", title="新标题")
    assert updated.title == "新标题"
    assert updated.version == 2

    updated_scn = await update_scenario(
        scenario.id,
        change_reason="场景改名，对齐实现用途",
        name="新场景名",
    )
    assert updated_scn.name == "新场景名"
    assert updated_scn.version == 2

    records = await list_changes("case", case.id)
    assert len(records) == 1
    assert records[0].change_reason == "标题不准确，改为业务口径"
    assert records[0].field_changes == {"title": {"from": "原标题", "to": "新标题"}}

    scn_records = await list_changes("scenario", scenario.id)
    assert len(scn_records) == 1
    assert scn_records[0].field_changes == {
        "name": {"from": "原场景名", "to": "新场景名"},
    }

    # 直接调 record_change 也要求 reason
    with pytest.raises(ChangeReasonRequiredError):
        await record_change("case", case.id, change_reason="  ")

    assert await TestChangeRecord.all().count() == 2


# ── 5) PgScenarioLoader 喂 ScenarioExecutor 跑通 http 桩场景 ──
@pytest.mark.asyncio
async def test_pg_loader_feeds_executor_http_stub(tortoise_db):
    pid = uuid.uuid4()
    scenario = await create_scenario(
        project_id=pid,
        name="下单主流程（引擎可执行）",
        local_vars={"order_id": None},
        flow={},
    )
    await create_steps(
        scenario.id,
        [
            {
                "step_key": "create_order",
                "seq": 10,
                "type": "request",
                "description": "创建订单",
                "params": {"method": "POST", "url": "/api/v1/orders"},
                "post_checks": [
                    {"type": "status_code", "expected": 201},
                    {"type": "json_path", "path": "$.data.order_id", "operator": "not_null"},
                ],
                # post_cond：步骤产出写局部变量（§3.1 局部变量流转机制）
                "post_cond": ["{{local.order_id}} = {{response.body.data.order_id}}"],
            },
            {
                "step_key": "verify_order",
                "seq": 20,
                "type": "request",
                "description": "查询订单（消费上一步写出的 local.order_id）",
                "params": {"method": "GET", "url": "/api/v1/orders/{{local.order_id}}"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
        ],
    )

    loader = PgScenarioLoader()
    # 协议可检查：是引擎 ScenarioLoader 的运行时实现
    assert isinstance(loader, ScenarioLoader)

    fake = FakeHttp()
    executor = make_executor(loader, fake)
    result = await executor.execute(scenario.code, ExecutionContext())

    assert result.status == STATUS_PASS
    assert result.scenario_code == "SCN-0001"
    assert [s.step_key for s in result.steps] == ["create_order", "verify_order"]
    assert all(s.status == STEP_PASS for s in result.steps)
    # DB 里声明的 local_vars 被展平后进 ctx（§3.1 声明 + 初值）；
    # 步骤 post_cond 写出的值覆盖初值（局部变量流转打通）
    assert result.local_vars == {"order_id": "ORD-1"}
    assert fake.calls[0]["url"] == "/api/v1/orders"
    # 第二步消费了第一步写出的局部变量（{{local.order_id}} 已插值）
    assert fake.calls[1]["url"] == "/api/v1/orders/ORD-1"

    # 场景不存在 → 引擎按 ScenarioNotFoundError 产出 status=error（不抛穿）
    missing = await executor.execute("SCN-9999", ExecutionContext())
    assert missing.status == STATUS_ERROR
    assert missing.error["kind"] == "scenario_not_found"


# ── 6) 关联表/变更表确实落到 schema（sqlite_master 实证）──
@pytest.mark.asyncio
async def test_asset_tables_registered(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {
        "mc_test_tag",
        "mc_test_case",
        "mc_test_case_tag",
        "mc_test_case_scenario",
        "mc_test_scenario",
        "mc_test_scenario_step",
        "mc_test_change_record",
    }
    assert expected <= names, f"缺表：{expected - names}"