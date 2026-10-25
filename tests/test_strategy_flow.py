"""测试策略域 flow 测试（用户定版 2026-10-14：冒烟/全量/回归/性能四类）。

覆盖：
* **四类 + 一版多策略** —— 四个 ``strategy_type`` 都能建；同一版本可挂多条策略
  （冒烟 + 回归并存），code 自增 ``STRAT-0001``；
* **枚举门** —— 越界 ``strategy_type`` 直接拒；
* **勾选差异同步** —— ``set_strategy_cases`` 新勾选建行、取消删行、**保留已有结果**；
* **分配 + 结果 + 汇总** —— 改派/改执行方式落库；录结果后 ``execution_rate`` /
  ``pass_rate`` 正确；
* **变更门** —— ``strategy.update`` 缺 ``change_reason`` 即拒（能力面必填校验）；
* **bug 两维度** —— ``list_bugs`` 按 ``version_id`` / ``strategy_id`` 分别收窄
  （缺陷归属 = 测试版本 × 测试策略）。

无 PG 依赖（sqlite::memory:，Tortoise 建表）。
运行：``python -m pytest tests/test_strategy_flow.py -q``
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

from testing_platform.assets.models import TestCase  # noqa: E402
from testing_platform.defects import (  # noqa: E402
    TestBug,
    create_bug_from_failure,
    list_bugs,
)
from testing_platform.strategy import (  # noqa: E402
    STRATEGY_TYPES,
    StrategyNotFoundError,
    TestStrategy,
    TestStrategyCase,
    assign_case,
    create_strategy,
    delete_strategy,
    get_strategy,
    list_strategies,
    record_case_result,
    set_strategy_cases,
    update_strategy,
)
from testing_platform.strategy.service import summarize  # noqa: E402
from testing_platform.assets.service import AssetValidationError  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:（策略 + 用例 + 缺陷三域表）。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.assets.models",
                "testing_platform.strategy.models",
                "testing_platform.defects.models",
            ],
        },
        use_tz=False,
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_case(project_id, code: str, title: str) -> uuid.UUID:
    row = await TestCase.create(
        id=uuid.uuid4(), project_id=project_id, code=code, title=title,
        confirm_source="doc", created_by=uuid.uuid4(),
    )
    return row.id


# ── 1) 四类都可建 + 一版多策略 + code 自增 ──────────────────────
@pytest.mark.asyncio
async def test_four_types_and_multiple_per_version(tortoise_db):
    pid, vid, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    for st in STRATEGY_TYPES:
        await create_strategy(project_id=pid, name=f"轮次 {st}", strategy_type=st,
                              version_id=vid, created_by=uid)

    assert await TestStrategy.all().count() == 4
    # 同一版本挂 4 条策略 —— 冒烟/全量/回归/性能并存是常态，不唯一约束
    rows = await list_strategies(pid, version_id=vid)
    assert len(rows) == 4
    assert {r["strategy_type"] for r in rows} == set(STRATEGY_TYPES)

    # code 按项目内 max+1（sqlite 同秒 created_at 排序不稳，按 code 字典序查）
    codes = sorted(r["code"] for r in rows)
    assert codes == [f"STRAT-{i:04d}" for i in range(1, 5)]

    # 列可按类型收窄
    assert len(await list_strategies(pid, strategy_type="smoke")) == 1
    with pytest.raises(AssetValidationError):
        await list_strategies(pid, strategy_type="perf")


# ── 2) 枚举门：越界类型拒建 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_strategy_type_enum_gate(tortoise_db):
    with pytest.raises(AssetValidationError):
        await create_strategy(project_id=uuid.uuid4(), name="非法", strategy_type="perf")
    assert await TestStrategy.all().count() == 0


# ── 3) 勾选差异同步：保留已有结果 ───────────────────────────────
@pytest.mark.asyncio
async def test_set_cases_diff_keeps_results(tortoise_db):
    pid, uid = uuid.uuid4(), uuid.uuid4()
    c1, c2, c3 = (_make_case(pid, f"TC-000{i}", f"用例{i}") for i in (1, 2, 3))
    c1, c2, c3 = await c1, await c2, await c3
    tester = uuid.uuid4()

    strategy = await create_strategy(project_id=pid, name="冒烟", strategy_type="smoke", created_by=uid)

    res = await set_strategy_cases(strategy.id, [c1, c2, c3], assignee_id=tester,
                                  exec_mode="manual", created_by=uid)
    assert sorted(res["added"]) == sorted([str(c1), str(c2), str(c3)])
    assert res["removed"] == []
    assert await TestStrategyCase.filter(strategy_id=strategy.id).count() == 3

    # 其中一条录了结果
    await record_case_result(strategy.id, c1, result="pass", executed_by=tester)

    # 重设：去掉 c3、新增不存在的 c4；**c1 的结果不能被抹掉**
    c4 = await _make_case(pid, "TC-0004", "用例4")
    res2 = await set_strategy_cases(strategy.id, [c1, c2, c4], created_by=uid)
    assert res2["removed"] == [str(c3)]
    assert res2["added"] == [str(c4)]
    assert res2["kept"] == 2

    doc = await get_strategy(strategy.id)
    by_case = {r["case_id"]: r for r in doc["cases"]}
    assert by_case[str(c1)]["result"] == "pass", "重设勾选必须保留已录结果"
    assert by_case[str(c1)]["assignee_id"] == str(tester), "改派状态也必须保留"
    assert by_case[str(c4)]["result"] == "pending", "新勾选用例默认待执行"


# ── 4) 分配 + 结果 + 汇总 ───────────────────────────────────────
@pytest.mark.asyncio
async def test_assign_record_and_summary(tortoise_db):
    pid, uid = uuid.uuid4(), uuid.uuid4()
    c1, c2, c3 = [await _make_case(pid, f"TC-000{i}", f"用例{i}") for i in (1, 2, 3)]
    a, b = uuid.uuid4(), uuid.uuid4()

    strategy = await create_strategy(project_id=pid, name="回归", strategy_type="regression", created_by=uid)
    await set_strategy_cases(strategy.id, [c1, c2, c3], created_by=uid)

    # 改派 + 改执行方式
    row = await assign_case(strategy.id, c1, assignee_id=a, exec_mode="automated")
    assert str(row.assignee_id) == str(a) and row.exec_mode == "automated"

    # 未勾选的用例改派 → 报错（先勾选再分配）
    with pytest.raises(StrategyNotFoundError):
        await assign_case(strategy.id, uuid.uuid4(), assignee_id=b)

    await record_case_result(strategy.id, c1, result="pass", executed_by=a)
    await record_case_result(strategy.id, c2, result="fail", note="金额算错", executed_by=b)

    doc = await get_strategy(strategy.id)
    s = doc["summary"]
    assert s == {
        "total": 3,
        "by_result": {"pending": 1, "pass": 1, "fail": 1, "blocked": 0, "skipped": 0},
        "executed": 2,
        "execution_rate": round(2 / 3, 4),
        "pass_rate": round(1 / 3, 4),
    }
    assert doc["cases"][1]["note"] == "金额算错"
    assert doc["cases"][1]["executed_at"], "结果录入时刻由服务端打点"

    # 越界结果拒录
    with pytest.raises(AssetValidationError):
        await record_case_result(strategy.id, c3, result="passed")

    # 空集合汇总不除零
    assert summarize([]) == {"total": 0, "by_result": {k: 0 for k in
                              ("pending", "pass", "fail", "blocked", "skipped")},
                             "executed": 0, "execution_rate": 0.0, "pass_rate": 0.0}


# ── 5) 变更门 + 版本自增 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_update_change_gate(tortoise_db):
    pid, uid = uuid.uuid4(), uuid.uuid4()
    v2 = uuid.uuid4()
    strategy = await create_strategy(project_id=pid, name="全量", strategy_type="full", created_by=uid)

    with pytest.raises(AssetValidationError):
        await update_strategy(strategy.id, change_reason="", values={"status": "done"})
    with pytest.raises(AssetValidationError):
        await update_strategy(strategy.id, change_reason="   ", values={"status": "done"})

    updated = await update_strategy(
        strategy.id, change_reason="本轮跑完，置已完成",
        values={"status": "done", "version_id": v2, "name": "全量测试 v2"}, created_by=uid,
    )
    assert updated.status == "done"
    assert str(updated.version_id) == str(v2)
    assert updated.version == 2, "update 后 version+1"

    # 不可改字段拒
    with pytest.raises(AssetValidationError):
        await update_strategy(strategy.id, change_reason="x", values={"code": "STRAT-9999"})

    # 删除：主行 + 勾选行一并清
    await set_strategy_cases(strategy.id, [await _make_case(pid, "TC-0009", "删前勾选")])
    await delete_strategy(strategy.id, created_by=uid)
    assert await TestStrategy.filter(id=strategy.id).count() == 0
    assert await TestStrategyCase.filter(strategy_id=strategy.id).count() == 0


# ── 6) bug 两维度：版本 × 策略 ──────────────────────────────────
@pytest.mark.asyncio
async def test_bug_version_and_strategy_dimensions(tortoise_db):
    pid, uid = uuid.uuid4(), uuid.uuid4()
    vid = uuid.uuid4()
    strategy = await create_strategy(project_id=pid, name="冒烟", strategy_type="smoke",
                                     version_id=vid, created_by=uid)
    other_strategy = await create_strategy(project_id=pid, name="性能", strategy_type="performance",
                                           created_by=uid)

    c2 = await _make_case(pid, "TC-0002", "退款")
    b_in = await create_bug_from_failure(title="退款金额错", project_id=pid, version_id=str(vid),
                                         strategy_id=strategy.id, severity="critical",
                                         case_refs=[str(c2)], user_id=uid)
    b_version_only = await create_bug_from_failure(title="版本内其它轮次的 bug", project_id=pid,
                                                   version_id=str(vid), user_id=uid)
    b_strategy_only = await create_bug_from_failure(title="无版本只挂策略", project_id=pid,
                                                    strategy_id=strategy.id, user_id=uid)
    b_free = await create_bug_from_failure(title="裸 bug", project_id=pid, user_id=uid)
    _ = other_strategy

    assert len({b_in, b_version_only, b_strategy_only, b_free}) == 4, "四维 bug 互不合并"

    # 维度一：按版本
    by_version = await list_bugs(pid, version_id=str(vid))
    assert {b["id"] for b in by_version} == {b_in, b_version_only}

    # 维度二：按策略
    by_strategy = await list_bugs(pid, strategy_id=strategy.id)
    assert {b["id"] for b in by_strategy} == {b_in, b_strategy_only}

    # 两维度交集 = 该版本该策略下的 bug
    both = await list_bugs(pid, version_id=str(vid), strategy_id=strategy.id)
    assert [b["id"] for b in both] == [b_in]
    assert both[0]["strategy_id"] == str(strategy.id)

    # 不过滤时全在
    assert await list_bugs(pid) and await TestBug.all().count() == 4


# ── 7) 能力面接线：strategy.* 方法可被前端调用 ──────────────────
@pytest.mark.asyncio
async def test_surface_methods_registered(tortoise_db):
    from testing_platform.surface import methods as surface

    for name in ("strategy.create", "strategy.list", "strategy.get", "strategy.update",
                 "strategy.delete", "strategy.set_cases", "strategy.assign_case",
                 "strategy.record_result", "strategy.remove_case", "strategy.cases"):
        assert surface.has_method(name), f"{name} 未注册"

    pid, uid = uuid.uuid4(), uuid.uuid4()
    case_id = await _make_case(pid, "TC-0007", "冒烟一条")

    doc = await surface.call_method("strategy.create", {
        "project_id": str(pid), "name": "冒烟", "strategy_type": "smoke",
        "start_time": "2026-10-20T09:00:00", "end_time": "2026-10-21T18:00:00",
        "owner_id": str(uid), "created_by": str(uid), "case_ids": [str(case_id)],
    })
    sid = doc["id"]
    assert doc["start_time"].startswith("2026-10-20")
    assert doc["summary"]["total"] == 1, "create 带 case_ids 应同步勾选"

    await surface.call_method("strategy.record_result", {
        "strategy_id": sid, "case_id": str(case_id), "result": "pass", "executed_by": str(uid),
    })
    got = await surface.call_method("strategy.get", {"strategy_id": sid})
    assert got["summary"]["pass_rate"] == 1.0
    assert got["status"] == "draft", "create 缺省 draft"

    # bug.create 收 strategy_id（第二维度入参）
    bug_ret = await surface.call_method("bug.create", {
        "project_id": str(pid), "title": "冒烟挂了", "version_id": None,
        "strategy_id": sid, "severity": "major",
    })
    bugs = await surface.call_method("bug.list", {"project_id": str(pid), "strategy_id": sid})
    assert bugs["count"] == 1 and bugs["bugs"][0]["id"] == bug_ret["bug_id"]

    # 变更门在能力面就是必填参数
    with pytest.raises(surface.MethodParamError):
        await surface.call_method("strategy.update", {"strategy_id": sid, "status": "done"})
    await surface.call_method("strategy.update", {
        "strategy_id": sid, "change_reason": "开跑", "status": "executing", "created_by": str(uid),
    })
    assert (await surface.call_method("strategy.get", {"strategy_id": sid}))["status"] == "executing"
