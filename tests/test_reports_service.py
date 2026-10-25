"""报告域服务测试（``03-report-model.md`` + design §15.11）。

覆盖：``generate_report`` 组装快照正确（overview 统计 / cases 用例投射 /
timing 响应耗时 / 证据 URI）+ ``get_report`` / ``list_reports`` + code 自增。

走一条完整链：任务 → run（标签选例）→ 场景/步骤/证据结果 → 报告快照。
无 PG 依赖（sqlite::memory:，Tortoise 建表）；无网络依赖。

运行：``python -m pytest tests/test_reports_service.py -q``
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

from testing_platform.assets.service import (  # noqa: E402
    create_case,
    create_scenario,
    link_case_scenario,
    tag_case,
)
from testing_platform.reports import generate_report, get_report, list_reports  # noqa: E402
from testing_platform.reports.models import TestReport  # noqa: E402
from testing_platform.reports.service import ReportNotFoundError  # noqa: E402
from testing_platform.tasks import create_task, run_task  # noqa: E402
from testing_platform.tasks.models import (  # noqa: E402
    TestEvidence,
    TestRunScenario,
    TestStepResult,
)


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:，app label 用 ``models``（三域同 app）。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.assets.models",
                "testing_platform.tasks.models",
                "testing_platform.reports.models",
                "testing_platform.defects.models",   # _collect_bugs 真采（P2 接线）
            ]
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 造一条"任务 → run → 结果"链，返回 (run, 通过场景行, 失败场景行) ──
async def seed_run(pid: _uuid.UUID, me: _uuid.UUID):
    from testing_platform.assets.models import TestTag

    # 同项目可能多次 seed：标签按 (project_id, code) 幂等
    tag_smoke, _ = await TestTag.get_or_create(
        project_id=pid, code="smoke", defaults={"name": "smoke", "category": "purpose"}
    )

    case_pass = await create_case(
        project_id=pid, title="下单闭环", confirm_source="debug", created_by=me,
        priority="P0", status="active", automation_status="automated",
    )
    case_fail = await create_case(
        project_id=pid, title="退款闭环", confirm_source="debug", created_by=me,
        priority="P1", status="active", automation_status="automated",
    )
    scn_pass = await create_scenario(project_id=pid, name="下单主流程")
    scn_fail = await create_scenario(project_id=pid, name="退款异常流")
    await tag_case(case_pass.id, tag_smoke.id)
    await tag_case(case_fail.id, tag_smoke.id)
    await link_case_scenario(case_pass.id, scn_pass.id, is_primary=True)
    await link_case_scenario(case_fail.id, scn_fail.id, is_primary=True)

    task = await create_task(
        project_id=pid,
        title="夜间回归",
        created_by=me,
        scenario_tags={"all": ["smoke"]},
        exec_type="scheduled",
        schedule={"kind": "cron", "expr": "0 2 * * *"},
        global_params={"test_version": "v2.3.1-rc2"},
    )
    run = await run_task(task.id, trigger="scheduled")
    # 同项目多次 seed 时选例会带上先前的 smoke 场景，故只断言本次两场景在内
    assert {str(scn_pass.id), str(scn_fail.id)} <= set(run.scenario_ids)

    # 引擎落库形态：run_scenario（场景级）+ step_result（步骤级）
    rs_pass = await TestRunScenario.create(
        run_id=run.id,
        scenario_id=str(scn_pass.id),
        case_refs=[str(case_pass.id)],
        exec_mode="script_only",
        status="pass",
        duration_ms=3200,
    )
    rs_fail = await TestRunScenario.create(
        run_id=run.id,
        scenario_id=str(scn_fail.id),
        case_refs=[str(case_fail.id)],
        exec_mode="script_only",
        status="fail",
        failure_kind="product_bug",
        duration_ms=1500,
    )
    await TestStepResult.create(
        run_scenario_id=rs_pass.id, step_key="create_order", seq="10", type="request",
        status="pass", attempt=1, duration_ms=120,
        expected={"status": 201}, actual={"status": 201},
    )
    await TestStepResult.create(
        run_scenario_id=rs_pass.id, step_key="verify_order", seq="20", type="request",
        status="pass", attempt=1, duration_ms=380,
        expected={"status": 200}, actual={"status": 200},
    )
    await TestStepResult.create(
        run_scenario_id=rs_fail.id, step_key="assert_refund", seq="10", type="check",
        status="fail", attempt=1, duration_ms=2100,
        expected={"body.x": 1}, actual={"body.x": 0},
        diff={"path": "body.x", "expected": 1, "actual": 0},
    )
    # 证据两种落点都要收：正式表 mc_test_evidence + 引擎内联 step_result.evidence
    await TestEvidence.create(
        run_scenario_id=rs_pass.id, step_seq=10, kind="log", uri="log_1.txt"
    )
    await TestEvidence.create(
        run_scenario_id=rs_pass.id, step_seq=20, kind="screenshot", uri="shot_1.png"
    )
    await TestStepResult.create(
        run_scenario_id=rs_fail.id, step_key="snapshot", seq="20", type="page",
        status="error", attempt=1, duration_ms=50,
        evidence=[{"kind": "trace", "uri": "trace_2.zip"}],
    )
    return run, rs_pass, rs_fail, case_pass, case_fail, scn_pass, scn_fail


# ── 1) generate_report：快照结构 + overview 统计 ─────────────────
@pytest.mark.asyncio
async def test_generate_report_snapshot_overview(tortoise_db):
    pid, me = _uuid.uuid4(), _uuid.uuid4()
    run, *_rest = await seed_run(pid, me)

    report = await generate_report(run.id)
    assert report.status == "ready"
    assert report.code == "RPT-0001"
    assert report.project_id == pid
    assert report.run_id == run.id
    assert report.generator == "auto"
    assert report.generated_at is not None

    snap = report.snapshot
    # 03 §2.1 顶层键齐全
    assert set(snap) >= {"code", "title", "test_version", "env", "overview", "timing", "cases", "bugs", "requirements"}
    assert snap["code"] == "RPT-0001"
    assert snap["title"].startswith("夜间回归 · ")
    assert snap["test_version"] == "v2.3.1-rc2"

    overview = snap["overview"]
    assert overview["total"] == 2
    assert overview["pass"] == 1
    assert overview["fail"] == 1
    assert overview["error"] == 0
    assert overview["flaky"] == 0
    assert overview["skipped"] == 0
    assert overview["duration_ms"] == 3200 + 1500
    assert overview["pass_rate"] == 0.5


# ── 2) cases 列表：run_scenario 经 case_refs 投射为用例条目 ──────
@pytest.mark.asyncio
async def test_generate_report_cases_projection(tortoise_db):
    pid, me = _uuid.uuid4(), _uuid.uuid4()
    run, rs_pass, rs_fail, case_pass, case_fail, scn_pass, scn_fail = await seed_run(pid, me)

    report = await generate_report(run.id)
    cases = report.snapshot["cases"]
    assert len(cases) == 2
    by_case = {c["case_code"]: c for c in cases}
    assert set(by_case) == {"TC-0001", "TC-0002"}

    passed = by_case[case_pass.code]
    assert passed["title"] == "下单闭环"
    assert passed["scenario"] == scn_pass.code          # SCN-0001
    assert passed["scenario_id"] == str(scn_pass.id)
    assert passed["status"] == "pass"
    assert passed["duration_ms"] == 3200
    # 步骤按 seq 升序；断言三件套齐
    assert [s["step_key"] for s in passed["steps"]] == ["create_order", "verify_order"]
    assert passed["steps"][0]["seq"] == 10 and passed["steps"][0]["type"] == "request"
    assert passed["steps"][0]["expected"] == {"status": 201}
    assert passed["steps"][0]["diff"] is None
    assert passed["evidence"] == ["log_1.txt", "shot_1.png"]

    failed = by_case[case_fail.code]
    assert failed["scenario"] == scn_fail.code
    assert failed["status"] == "fail"
    assert failed["steps"][0]["diff"] == {"path": "body.x", "expected": 1, "actual": 0}
    # 内联证据（引擎 step_result.evidence）也要进报告
    assert failed["evidence"] == ["trace_2.zip"]


# ── 3) timing：响应时间走 request 类，慢步骤 Top N 走全步骤 ──────
@pytest.mark.asyncio
async def test_generate_report_timing(tortoise_db):
    pid, me = _uuid.uuid4(), _uuid.uuid4()
    run, *_rest = await seed_run(pid, me)

    timing = (await generate_report(run.id)).snapshot["timing"]
    assert timing["avg_response_ms"] == 250          # (120 + 380) / 2，request 类
    assert timing["p95_response_ms"] == 380
    slowest = timing["slowest_steps"]
    assert slowest[0] == {"step": "assert_refund", "ms": 2100}
    assert len(slowest) == 4                          # 全部有耗时的步骤（≤ Top 5）


# ── 4) 场景未挂用例：仍出一条条目，运行不在报告里"隐身" ─────────
@pytest.mark.asyncio
async def test_generate_report_scenario_without_case(tortoise_db):
    from testing_platform.assets.service import create_scenario
    from testing_platform.tasks.models import TestRun

    pid, me = _uuid.uuid4(), _uuid.uuid4()
    scn = await create_scenario(project_id=pid, name="裸场景")
    run = await TestRun.create(
        project_id=pid, code="RUN-0009", task_id=None, trigger="manual",
        status="pending", scenario_ids=[str(scn.id)],
    )
    await TestRunScenario.create(
        run_id=run.id, scenario_id=str(scn.id), case_refs=None, status="pass", duration_ms=10
    )

    report = await generate_report(run.id)
    cases = report.snapshot["cases"]
    assert len(cases) == 1
    assert cases[0]["case_code"] is None
    assert cases[0]["scenario"] == scn.code
    assert report.snapshot["overview"]["total"] == 1


# ── 5) code 自增 RPT-0001（按 project 隔离）+ generator/title 覆盖 ──
@pytest.mark.asyncio
async def test_report_code_autoincrement_and_params(tortoise_db):
    pid, other, me = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
    run1, *_ = await seed_run(pid, me)
    run_other, *_ = await seed_run(other, me)

    r1 = await generate_report(run1.id)
    r2 = await generate_report(run1.id, generator="manual", title="人工复核版")
    r_other = await generate_report(run_other.id)

    assert (r1.code, r2.code, r_other.code) == ("RPT-0001", "RPT-0002", "RPT-0001")
    assert r1.generator == "auto" and r1.title.startswith("夜间回归")
    assert r2.generator == "manual" and r2.title == "人工复核版"
    # 同一次 run 允许多份报告（03 §5）
    assert r1.snapshot["code"] == "RPT-0001" and r2.snapshot["code"] == "RPT-0002"


# ── 6) get_report / list_reports ─────────────────────────────────
@pytest.mark.asyncio
async def test_get_and_list_reports(tortoise_db):
    pid, other, me = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
    run1, *_ = await seed_run(pid, me)
    run2, *_ = await seed_run(pid, me)
    run_other, *_ = await seed_run(other, me)

    first = await generate_report(run1.id)
    second = await generate_report(run2.id)
    await generate_report(run_other.id)

    fetched = await get_report(first.id)
    assert fetched.id == first.id
    assert fetched.snapshot == first.snapshot          # 生成时定版，读到的就是当时那份

    reports = await list_reports(pid)
    assert len(reports) == 2
    assert reports[0].code == "RPT-0002"              # 最新在前

    only_run = await list_reports(pid, run_id=run1.id)
    assert [r.id for r in only_run] == [first.id]
    ready = await list_reports(pid, status="ready")
    assert len(ready) == 2

    with pytest.raises(ReportNotFoundError):
        await get_report(_uuid.uuid4())


# ── 7) 快照定版：run 之后再改结果，报告不变（03 §6 决策 1）────────
@pytest.mark.asyncio
async def test_snapshot_is_frozen_at_generation(tortoise_db):
    pid, me = _uuid.uuid4(), _uuid.uuid4()
    run, rs_fail, *_rest = await seed_run(pid, me)

    report = await generate_report(run.id)
    assert report.snapshot["overview"]["fail"] == 1

    await rs_fail.delete()
    assert await TestRunScenario.filter(run_id=run.id).count() == 1

    again = await get_report(report.id)
    assert again.snapshot["overview"]["fail"] == 1     # 结果表被清理，快照保住真相


# ── 8) 表注册实证（sqlite_master）────────────────────────────────
@pytest.mark.asyncio
async def test_report_tables_registered(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {"mc_test_report", "mc_test_run", "mc_test_run_scenario", "mc_test_step_result", "mc_test_evidence"}
    assert expected <= names, f"缺表：{expected - names}"
    assert await TestReport.all().count() == 0


# ── 9) P2 接线：快照真采 bugs / requirements（不再空数组）────────
@pytest.mark.asyncio
async def test_snapshot_collects_bugs_and_requirements(tortoise_db):
    from testing_platform.assets.models import TestCase, TestRequirement

    pid, me = _uuid.uuid4(), _uuid.uuid4()
    req_a = await TestRequirement.create(project_id=pid, code="REQ-A01", title="下单主链", module="order")
    req_b = await TestRequirement.create(project_id=pid, code="REQ-B01", title="退款异常", module="refund")

    run, *_rest = await seed_run(pid, me)
    # seed 的用例没挂需求——把两条已存在的分别挂到两个需求上
    c_pass = await TestCase.get(project_id=pid, title="下单闭环")
    c_fail = await TestCase.get(project_id=pid, title="退款闭环")
    await TestCase.filter(id=c_pass.id).update(requirement_id=req_a.id)
    await TestCase.filter(id=c_fail.id).update(requirement_id=req_b.id)

    # 本 run 产生的缺陷（from_run_id 归集口径）+ 一条根因行
    from testing_platform.defects.models import TestBug, TestBugRootCause

    bug = await TestBug.create(
        project_id=pid, code="BUG-0001", title="退款金额算错",
        severity="critical", from_run_id=run.id,
    )
    await TestBugRootCause.create(
        bug_id=bug.id, hypothesis="退款用了原价没扣券",
        location="refund/service.py:88", classification="logic",
    )

    report = await generate_report(run.id)
    snap = report.snapshot

    # bugs 真采到，root_causes 形状对齐 developer 受众视图
    assert len(snap["bugs"]) == 1
    b = snap["bugs"][0]
    assert b["code"] == "BUG-0001" and b["severity"] == "critical"
    assert b["root_causes"][0]["hypothesis"] == "退款用了原价没扣券"
    assert b["root_causes"][0]["location"] == "refund/service.py:88"

    # requirements：全过的需求 covered，全挂的 not_covered
    reqs = {r["requirement_id"]: r for r in snap["requirements"]}
    assert reqs[str(req_a.id)]["coverage"] == "covered"
    assert reqs[str(req_b.id)]["coverage"] == "not_covered"
    assert reqs[str(req_b.id)]["module"] == "refund"

    # cases 带 requirement_id（product 受众 risks 的映射键）
    by_code = {c["case_code"]: c for c in snap["cases"]}
    assert by_code["TC-0001"]["requirement_id"] == str(req_a.id)


@pytest.mark.asyncio
async def test_snapshot_partial_coverage_when_mixed(tortoise_db):
    """同需求两条用例一 pass 一 fail → partial。"""
    from testing_platform.assets.models import TestCase, TestRequirement

    pid, me = _uuid.uuid4(), _uuid.uuid4()
    req = await TestRequirement.create(project_id=pid, code="REQ-P01", title="登录")
    run, *_rest = await seed_run(pid, me)
    await TestCase.filter(project_id=pid).update(requirement_id=req.id)

    report = await generate_report(run.id)
    reqs = {r["requirement_id"]: r for r in report.snapshot["requirements"]}
    assert reqs[str(req.id)]["coverage"] == "partial"
