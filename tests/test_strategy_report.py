"""测试策略报表 flow 测试（用户定版 2026-10-16）。

覆盖（``strategy.report`` 的口径，纯函数 + 能力面接线）：

* **用例段** —— 勾选数 / 自动化数（``exec_mode=automated``）/ 结果分布 / 执行率；
* **缺陷段** —— 总数 / **P0-P1 = priority 1+2**（用户定版，非严重度）/
  **平均·最长·最短解决时长**（``created_at → resolved_at``，只算已解决的）/
  最长那条 bug 的定位；无数据时三个时长给 ``None``（不是 0）；
* **延时段** —— 计划 ``end_time`` vs ``actual_end_time``（用户定版口径）：
  实际完成晚于计划 → ``delayed + basis=actual``；未填实际完成但已过计划结束 →
  ``basis=in_progress`` 按当前时间算已超期；未过期 → ``delayed=false``；
  无计划结束 → ``has_plan=false``；
* **能力面接线** —— ``strategy.report`` 注册且返回四段。

无 PG 依赖（sqlite::memory:，Tortoise 建表）。
运行：``python -m pytest tests/test_strategy_report.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.assets.models import TestCase  # noqa: E402
from testing_platform.defects import create_bug_from_failure  # noqa: E402
from testing_platform.strategy import (  # noqa: E402
    create_strategy,
    set_strategy_cases,
    strategy_report,
    update_strategy,
)
from testing_platform.strategy.report import (  # noqa: E402
    compute_strategy_report,
)

BASE = datetime(2026, 10, 16, 12, 0, 0)


@pytest_asyncio.fixture
async def tortoise_db():
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


async def _make_case(project_id, code: str) -> uuid.UUID:
    row = await TestCase.create(
        id=uuid.uuid4(), project_id=project_id, code=code, title=f"用例 {code}",
        confirm_source="doc", created_by=uuid.uuid4(),
    )
    return row.id


def _bug_doc(**overrides):
    """构造 ``list_bugs`` 输出形状的 bug 文档（纯函数不落库）。"""
    base = {
        "id": str(uuid.uuid4()),
        "code": "BUG-0001",
        "title": "下单 500",
        "status": "resolved",
        "severity": "critical",
        "priority": 1,
        "created_at": (BASE - timedelta(days=2)).isoformat(),
        "resolved_at": (BASE - timedelta(days=1)).isoformat(),
        "closed_at": None,
        "activated_count": 0,
        "escaped": False,
    }
    base.update(overrides)
    return base


# ── 1) 用例段：总数 / 自动化 / 执行率 ──────────────────────────
def test_case_stats():
    cases = [
        {"exec_mode": "automated", "result": "pass"},
        {"exec_mode": "automated", "result": "fail"},
        {"exec_mode": "manual", "result": "pass"},
        {"exec_mode": "manual", "result": "pending"},
        {"exec_mode": "hybrid", "result": "blocked"},
    ]
    s = compute_strategy_report({"cases": cases}, [], now=BASE)["cases"]
    assert s["total"] == 5
    assert s["automated"] == 2 and s["manual"] == 2 and s["hybrid"] == 1
    assert s["executed"] == 4 and s["execution_rate"] == 0.8
    assert s["pass_rate"] == 0.4
    assert s["by_result"]["pass"] == 2 and s["by_result"]["pending"] == 1


# ── 2) 缺陷段：P0-P1 + 三个解决时长 ────────────────────────────
def test_bug_stats_p0p1_and_durations():
    # 三条已解决：1天 / 2天 / 4天；一条 P2 未解决（priority 2 也算 P0-P1）
    bugs = [
        _bug_doc(priority=1, created_at=(BASE - timedelta(days=1)).isoformat(),
                 resolved_at=BASE.isoformat()),  # 1 天
        _bug_doc(priority=2, created_at=(BASE - timedelta(days=2)).isoformat(),
                 resolved_at=BASE.isoformat(), code="BUG-0002"),  # 2 天
        _bug_doc(priority=1, created_at=(BASE - timedelta(days=4)).isoformat(),
                 resolved_at=BASE.isoformat(), code="BUG-0003"),  # 4 天
        _bug_doc(priority=3, status="open", resolved_at=None, code="BUG-0004"),  # 未解决不算时长
    ]
    b = compute_strategy_report({}, bugs, now=BASE)["bugs"]
    assert b["total"] == 4
    assert b["p0_p1_count"] == 3, "priority 1+2（用户定版），P3 不算"
    assert b["resolved_count"] == 3
    # open_count = 未关闭/未驳回（含已解决未关闭的）——3 条 resolved(status=resolved) + 1 条 open
    assert b["open_count"] == 4
    # resolved 但已 closed 的不算 open
    b_closed = compute_strategy_report(
        {}, [_bug_doc(status="closed"), _bug_doc(status="open", resolved_at=None)], now=BASE)["bugs"]
    assert b_closed["open_count"] == 1 and b_closed["resolved_count"] == 1
    assert b["avg_resolve_seconds"] == round((1 + 2 + 4) / 3 * 86400, 2)
    assert b["max_resolve_seconds"] == 4 * 86400
    assert b["min_resolve_seconds"] == 1 * 86400
    assert b["slowest_bug"]["code"] == "BUG-0003", "最长的要能定位到那条 bug"

    # P0-P1 按 priority 而非 severity：critical 但 priority=3 不算
    b2 = compute_strategy_report({}, [_bug_doc(priority=3, severity="critical")], now=BASE)["bugs"]
    assert b2["p0_p1_count"] == 0


def test_bug_stats_empty_gives_none_not_zero():
    b = compute_strategy_report({}, [], now=BASE)["bugs"]
    assert b["total"] == 0
    assert b["avg_resolve_seconds"] is None
    assert b["max_resolve_seconds"] is None
    assert b["min_resolve_seconds"] is None
    assert b["slowest_bug"] is None


# ── 3) 延时段：三种判定 ─────────────────────────────────────────
def test_delay_actual_late():
    # 实际完成晚于计划 2 天 → delayed，basis=actual
    doc = {
        "end_time": (BASE - timedelta(days=2)).isoformat(),
        "actual_end_time": BASE.isoformat(),
        "delay_analysis": "环境阻塞",
    }
    d = compute_strategy_report(doc, [], now=BASE)["delay"]
    assert d["delayed"] is True and d["basis"] == "actual"
    assert d["delay_seconds"] == 2 * 86400
    assert d["delay_days"] == 2.0
    assert d["analysis"] == "环境阻塞"


def test_delay_in_progress_overdue():
    # 未填实际完成 + 计划已过 1 天 → basis=in_progress，按 now 算
    doc = {"end_time": (BASE - timedelta(days=1)).isoformat(), "actual_end_time": None}
    d = compute_strategy_report(doc, [], now=BASE)["delay"]
    assert d["delayed"] is True and d["basis"] == "in_progress"
    assert d["delay_seconds"] == 1 * 86400


def test_delay_not_delayed_and_no_plan():
    # 计划未到 → 不延期
    doc = {"end_time": (BASE + timedelta(days=3)).isoformat(), "actual_end_time": None}
    d = compute_strategy_report(doc, [], now=BASE)["delay"]
    assert d["delayed"] is False and d["basis"] is None

    # 无计划结束 → has_plan=false
    d2 = compute_strategy_report({"end_time": None}, [], now=BASE)["delay"]
    assert d2["has_plan"] is False and d2["delayed"] is False

    # 实际完成早于计划 → 不延期
    d3 = compute_strategy_report(
        {"end_time": (BASE + timedelta(days=1)).isoformat(),
         "actual_end_time": BASE.isoformat()}, [], now=BASE)["delay"]
    assert d3["delayed"] is False and d3["basis"] == "actual"


# ── 4) 能力面接线：strategy.report 走真库 ──────────────────────
@pytest.mark.asyncio
async def test_strategy_report_via_surface(tortoise_db):
    from testing_platform.surface import methods as surface

    assert surface.has_method("strategy.report")
    pid, uid = uuid.uuid4(), uuid.uuid4()
    vid = uuid.uuid4()

    c1 = await _make_case(pid, "TC-0001")
    c2 = await _make_case(pid, "TC-0002")
    strategy = await create_strategy(
        project_id=pid, name="回归", strategy_type="regression", version_id=vid,
        end_time=(BASE - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
        created_by=uid,
    )
    await set_strategy_cases(strategy.id, [c1, c2], exec_mode="automated")

    # 建两条 bug：一条 P1 已解决（1 天），一条 P2 未解决
    await create_bug_from_failure(
        title="P1 快修", project_id=pid, strategy_id=strategy.id, version_id=str(vid),
        priority=1, severity="major",
    )
    await create_bug_from_failure(
        title="P2 待修", project_id=pid, strategy_id=strategy.id, version_id=str(vid),
        priority=2, severity="minor",
    )
    bugs_before = await surface.call_method(
        "bug.list", {"project_id": str(pid), "strategy_id": str(strategy.id)})
    assert bugs_before["count"] == 2
    # 把第一条标已解决（服务层直接更新 resolved_at 模拟流转）
    from testing_platform.defects.models import TestBug

    first = await TestBug.filter(project_id=pid, title="P1 快修").first()
    first.resolved_at = BASE
    await first.save()

    r = await surface.call_method("strategy.report", {"strategy_id": str(strategy.id)})
    assert r["cases"]["total"] == 2
    assert r["cases"]["automated"] == 2, "set_cases 带 exec_mode=automated 应落到每行"
    assert r["bugs"]["total"] == 2
    assert r["bugs"]["p0_p1_count"] == 2, "P1+P2 都算 P0-P1"
    assert r["bugs"]["max_resolve_seconds"] is not None
    assert r["bugs"]["slowest_bug"]["code"] == first.code
    # 延期：计划已过 1 天、未填实际完成 → in_progress
    assert r["delay"]["delayed"] is True
    assert r["delay"]["basis"] == "in_progress"

    # 补实际完成时间 + 延期分析（走 update 变更门）
    await update_strategy(
        strategy.id, change_reason="补延期分析",
        values={"actual_end_time": BASE, "delay_analysis": "环境阻塞 1 天"}, created_by=uid,
    )
    r2 = await surface.call_method("strategy.report", {"strategy_id": str(strategy.id)})
    assert r2["delay"]["basis"] == "actual"
    assert r2["delay"]["analysis"] == "环境阻塞 1 天"
    assert r2["delay"]["delayed"] is True and r2["delay"]["delay_days"] == 1.0

    # strategy_report 直调（service 层）同口径
    direct = await strategy_report(strategy.id, now=BASE)
    assert direct["bugs"]["p0_p1_count"] == 2
