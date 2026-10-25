"""测试报告四类受众视图测试（25 号文 §环节 10 报告，P2）。

纯函数测试：用构造的假 report dict / 假 TestReport 行测，**不连 DB、不联网、
不启 Tortoise**。

覆盖：
- 结论先行——``conclusion`` 永远是返回 dict 的第一键（四种受众都验）
- executor 含 steps/evidence（每步 expected/actual/diff）+ 失败优先排序
- developer 含 root_causes / recommended_sql / recommended_logs / related_bug_ids
  / fix_verification / failing_steps
- product 含需求覆盖 + 风险清单 + 业务结论，**不含**技术细节（steps/evidence/
  SQL/日志/diff/根因）
- manager 含 go_no_go / fail_rate / pass_rate / confidence / trend /
  bug_severity_rollup，**不含**步骤细节
- 输入形态归一化（snapshot 本体 / 带 snapshot 键的行 dict / TestReport 行对象）
- audience_view 派发 + 非法受众抛 ValueError
- 空快照 / 全过 / 抖动跳过 的判定边界

运行：``python -m pytest tests/test_reports_audience.py -q``
"""
from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.reports.audience_views import (  # noqa: E402
    AUDIENCES,
    audience_view,
    developer_view,
    executor_view,
    manager_view,
    product_view,
)


# ── 假快照（03 §2.1 结构，字段名对齐 reports/service.py）─────────
def snapshot() -> dict[str, Any]:
    """构造一份 generate_report 形状的快照（20 用例：17 过 2 挂 1 错）。"""
    return {
        "code": "RPT-0007",
        "title": "登录改造回归 · 2026-10-20",
        "test_version": "build-1234",
        "env": "11111111-1111-1111-1111-111111111111",
        "overview": {
            "total": 20, "pass": 17, "fail": 2, "error": 1,
            "flaky": 0, "skipped": 0, "duration_ms": 5230,
            "pass_rate": 0.85,
        },
        "timing": {
            "avg_response_ms": 120, "p95_response_ms": 340,
            "slowest_steps": [{"step": "login_request", "ms": 340}],
        },
        "cases": [
            {
                "case_code": "TC-0001", "title": "正常登录",
                "scenario": "SCN-0001", "scenario_id": "22222222-2222-2222-2222-222222222222",
                "status": "pass", "duration_ms": 200,
                "requirement_id": "REQ-001",
                "steps": [
                    {"step_key": "login_request", "seq": 1, "type": "request", "status": "pass",
                     "attempt": 1, "duration_ms": 180,
                     "expected": {"status_code": 200}, "actual": {"status_code": 200}, "diff": None},
                ],
                "evidence": ["file://ev/login-pass.har"],
            },
            {
                "case_code": "TC-0002", "title": "密码错误应 401",
                "scenario": "SCN-0002", "scenario_id": "33333333-3333-3333-3333-333333333333",
                "status": "fail", "duration_ms": 260,
                "requirement_id": "REQ-001",
                "steps": [
                    {"step_key": "login_request", "seq": 1, "type": "request", "status": "fail",
                     "attempt": 2, "duration_ms": 240,
                     "expected": {"status_code": 401}, "actual": {"status_code": 500},
                     "diff": "status_code: expected 401, got 500"},
                ],
                "evidence": ["file://ev/login-fail.har", "file://ev/login-fail.png"],
            },
            {
                "case_code": "TC-0003", "title": "令牌刷新",
                "scenario": "SCN-0003", "scenario_id": "44444444-4444-4444-4444-444444444444",
                "status": "error", "duration_ms": 30,
                "requirement_id": "REQ-002",
                "steps": [
                    {"step_key": "refresh_request", "seq": 1, "type": "request", "status": "error",
                     "attempt": 1, "duration_ms": 30,
                     "expected": None, "actual": None, "diff": "ConnectionError: ECONNREFUSED"},
                ],
                "evidence": [],
            },
        ],
        "bugs": [
            {
                "id": "55555555-5555-5555-5555-555555555555",
                "code": "BUG-0001",
                "title": "登录接口对错误密码返回 500",
                "symptom": "错误密码应 401，实返 500",
                "status": "active", "severity": "critical", "module": "auth",
                "fingerprint": "deadbeef",
                "related_bug_ids": ["BUG-0000", "BUG-0009"],
                "repro": {"steps": ["POST /login with wrong password"], "rate": "10/10"},
                "resolved_at": "2026-10-20T09:00:00+00:00",
                "verified_at": "2026-10-20T10:00:00+00:00",
                "activated_count": 0,
                "root_causes": [
                    {
                        "id": "66666666-6666-6666-6666-666666666666",
                        "bug_id": "55555555-5555-5555-5555-555555555555",
                        "location": "auth/service.py:login:88",
                        "classification": "code_error",
                        "attributed_commit": "a1b2c3d",
                        "hypothesis": "未捕获 AuthError 冒泡为 500",
                        "confidence": 0.9,
                        "fix_suggestion": "在 login 外层 catch AuthError → 401",
                        "healing_suggestion": "统一异常映射表",
                        "blast_radius": ["auth", "session"],
                        "evidence_chain": [
                            {"kind": "log", "path": "logs/auth.log", "content": "AuthError traceback"},
                            {"kind": "sql", "statement": "SELECT count(*) FROM login_audit WHERE user_id = 42"},
                            {"kind": "commit", "hash": "a1b2c3d"},
                        ],
                    },
                ],
            },
        ],
        "requirements": [
            {"requirement_id": "REQ-001", "title": "登录改造", "coverage": "covered"},
            {"requirement_id": "REQ-002", "title": "令牌刷新", "coverage": "partial"},
            {"requirement_id": "REQ-003", "title": "多因子认证", "coverage": "not_covered"},
        ],
        "trend": {
            "pass_rate_series": [0.70, 0.80, 0.85],
            "previous_pass_rate": 0.80,
            "runs": 3,
        },
        "confidence": 0.92,
    }


class _FakeReportRow:
    """鸭子类型的 TestReport 行（只带 .snapshot 与行字段），测归一化路径。"""

    def __init__(self, snap: dict[str, Any]) -> None:
        self.snapshot = snap
        self.code = "RPT-0007"
        self.title = "登录改造回归 · 2026-10-20"
        self.test_version = "build-1234"
        self.status = "ready"
        self.generator = "auto"
        self.generated_at = "2026-10-20T10:00:00+00:00"
        self.env_id = "11111111-1111-1111-1111-111111111111"
        self.run_id = "77777777-7777-7777-7777-777777777777"
        self.task_id = "88888888-8888-8888-8888-888888888888"
        self.project_id = "99999999-9999-9999-9999-999999999999"


def _walk_keys(obj: Any, acc: set[str]) -> set[str]:
    """递归收集 dict 键名（测「不含技术细节」用）。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            acc.add(key)
            _walk_keys(value, acc)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_keys(item, acc)
    return acc


# 技术细节键——product/manager 视图里不该出现
_TECH_KEYS = {
    "steps", "step_key", "evidence", "slowest_steps", "diff",
    "expected", "actual", "recommended_sql", "recommended_logs",
    "root_causes", "hypothesis", "fix_suggestion", "failing_steps",
    "fingerprint", "location", "attributed_commit",
}


# ── 1) 结论先行：四种受众都把 conclusion 放首位 ─────────────────
@pytest.mark.parametrize("builder", [executor_view, developer_view, product_view, manager_view])
def test_conclusion_is_first_key_for_every_audience(builder):
    view = builder(snapshot())
    assert list(view.keys())[0] == "conclusion", "结论先行：conclusion 必须是第一键"
    concl = view["conclusion"]
    # 顶层可见的结论要素
    for key in ("verdict", "go_no_go", "headline", "pass_rate", "counts"):
        assert key in concl, f"结论缺要素 {key!r}"
    # 数据驱动（25 号文：结论必须有执行数）
    assert concl["counts"]["total"] == 20
    assert concl["counts"]["pass"] == 17
    assert concl["go_no_go"] == "No-Go"
    assert "No-Go" in concl["headline"]


# ── 2) executor：失败优先 + 每步输入输出 + 证据 ──────────────────
def test_executor_has_steps_and_evidence_and_leads_with_failures():
    view = executor_view(snapshot())

    assert view["audience"] == "executor"
    assert view["cases"], "executor 视图必须列出用例"
    # 失败/错误排在前面
    assert view["cases"][0]["status"] in ("fail", "error")
    assert view["cases"][1]["status"] in ("fail", "error")
    assert view["cases"][-1]["status"] == "pass"

    failed = view["failures"]
    assert len(failed) == 2, "快照用例清单是抽样（1 fail + 1 error），failures 应有 2 条"

    # 每步的输入输出（expected/actual/diff）+ 证据 URI
    step = view["cases"][0]["steps"][0]
    assert step["expected"] == {"status_code": 401}
    assert step["actual"] == {"status_code": 500}
    assert step["diff"]
    assert view["cases"][0]["evidence"] == ["file://ev/login-fail.har", "file://ev/login-fail.png"]

    # 响应耗时与慢步骤（执行者排性能问题用）
    assert view["timing"]["p95_response_ms"] == 340
    assert view["timing"]["slowest_steps"][0]["step"] == "login_request"


# ── 3) developer：根因 / 推荐 SQL / 关联历史 bug / 修复验证 ──────
def test_developer_has_rootcause_sql_and_related_bugs():
    view = developer_view(snapshot())

    assert view["audience"] == "developer"
    assert len(view["bugs"]) == 1
    bug = view["bugs"][0]

    # 根因定位
    assert bug["root_causes"][0]["hypothesis"] == "未捕获 AuthError 冒泡为 500"
    assert bug["root_causes"][0]["location"] == "auth/service.py:login:88"
    assert bug["root_causes"][0]["classification"] == "code_error"
    # 关联代码（归因提交）
    assert bug["root_causes"][0]["attributed_commit"] == "a1b2c3d"
    assert bug["root_causes"][0]["fix_suggestion"]

    # 推荐 SQL（从 evidence_chain 按 kind=sql 捞出）
    assert any("SELECT count(*) FROM login_audit" in sql for sql in bug["recommended_sql"])
    # 推荐日志
    assert any("logs/auth.log" in str(log) for log in bug["recommended_logs"])
    # 关联历史 bug
    assert bug["related_bug_ids"] == ["BUG-0000", "BUG-0009"]
    # 修复验证
    assert "fix_verification" in bug
    assert bug["fix_verification"]["verified_at"] == "2026-10-20T10:00:00+00:00"

    # 失败步骤的诊断面（辅助定位根因）
    assert view["failing_steps"], "developer 视图应带失败步骤"
    assert all(s["status"] in ("fail", "error") for s in view["failing_steps"])
    assert any(s["case_code"] == "TC-0003" for s in view["failing_steps"])


def test_developer_keeps_explicit_recommended_sql_field():
    snap = snapshot()
    snap["bugs"][0]["recommended_sql"] = ["SELECT 1"]
    bug = developer_view(snap)["bugs"][0]
    assert "SELECT 1" in bug["recommended_sql"]


def test_developer_uses_explicit_fix_verification_when_present():
    snap = snapshot()
    snap["bugs"][0]["fix_verification"] = {"seven_points": ["原路径复现过"], "passed": True}
    bug = developer_view(snap)["bugs"][0]
    assert bug["fix_verification"]["passed"] is True


# ── 4) product：覆盖 + 风险 + 业务结论，不含技术细节 ─────────────
def test_product_has_coverage_and_risks_without_tech_details():
    view = product_view(snapshot())

    assert view["audience"] == "product"
    cov = view["requirements_coverage"]
    assert (cov["total"], cov["covered"], cov["partial"], cov["not_covered"]) == (3, 1, 1, 1)
    uncovered = {r["requirement_id"] for r in cov["uncovered"]}
    assert uncovered == {"REQ-002", "REQ-003"}, "哪些需求没测/测不全要列出来"

    # 风险清单（业务口径：哪个需求挂了）
    assert len(view["risks"]) == 2
    assert {r["case_code"] for r in view["risks"]} == {"TC-0002", "TC-0003"}

    # 业务结论（一句摘要）
    assert view["business_summary"] == view["conclusion"]["headline"]

    # 不含技术细节：无 steps/evidence/SQL/日志/diff/根因
    keys = _walk_keys(view, set())
    leaked = keys & _TECH_KEYS
    assert not leaked, f"product 视图泄漏技术细节：{leaked}"
    text = json.dumps(view, ensure_ascii=False, default=str)
    assert "SELECT" not in text, "product 视图不该出现推荐 SQL"
    assert "login_request" not in text, "product 视图不该出现 step_key"


# ── 5) manager：Go/No-Go + 失败率 + 置信度 + 趋势，无步骤细节 ────
def test_manager_has_gonogo_failrate_confidence_trend():
    view = manager_view(snapshot())

    assert view["audience"] == "manager"
    # 顶层一眼可扫的结论数
    assert view["go_no_go"] == "No-Go"
    assert view["fail_rate"] == 0.15           # (2 fail + 1 error) / 20
    assert view["pass_rate"] == 0.85
    assert view["confidence"] == 0.92
    assert view["headline"]

    # 趋势
    assert view["trend"]["pass_rate_series"] == [0.70, 0.80, 0.85]
    assert view["trend"]["previous_pass_rate"] == 0.80

    # bug 只看规模/严重度分布，不看明细
    assert view["bug_severity_rollup"] == {"critical": 1}

    # 不含步骤细节
    keys = _walk_keys(view, set())
    leaked = keys & _TECH_KEYS
    assert not leaked, f"manager 视图泄漏步骤细节：{leaked}"


def test_manager_reads_confidence_from_release_gate():
    snap = snapshot()
    snap.pop("confidence")
    snap["release_gate"] = {"confidence": 0.55, "consensus": "no_go"}
    view = manager_view(snap)
    assert view["confidence"] == 0.55


# ── 6) 判定边界：全过 Go / 抖动跳过 Conditional / 挂 No-Go ──────
def test_verdict_go_when_everything_passed():
    snap = snapshot()
    snap["overview"] = {"total": 5, "pass": 5, "fail": 0, "error": 0,
                        "flaky": 0, "skipped": 0, "duration_ms": 10, "pass_rate": 1.0}
    snap["cases"] = []
    concl = manager_view(snap)["conclusion"]
    assert concl["verdict"] == "go"
    assert concl["go_no_go"] == "Go"
    assert concl["fail_rate"] == 0.0


def test_verdict_conditional_when_flaky_or_skipped():
    snap = snapshot()
    snap["overview"] = {"total": 5, "pass": 4, "fail": 0, "error": 0,
                        "flaky": 1, "skipped": 2, "duration_ms": 10, "pass_rate": 0.8}
    snap["cases"] = []
    concl = manager_view(snap)["conclusion"]
    assert concl["verdict"] == "conditional"
    assert concl["go_no_go"] == "Conditional"


def test_empty_snapshot_does_not_crash():
    """v1 快照可能缺区（bugs/requirements 未接线）——四视图都要容错。"""
    for builder in (executor_view, developer_view, product_view, manager_view):
        view = builder({})
        concl = view["conclusion"]
        assert list(view.keys())[0] == "conclusion"
        assert concl["counts"]["total"] == 0
        assert concl["verdict"] == "unknown"   # 数据驱动：没跑就没有结论可下
        assert concl["go_no_go"] == "No Data"
        assert concl["fail_rate"] == 0.0
        assert concl["confidence"] is None


# ── 7) 输入形态归一化：dict 快照 / 带 snapshot 的行 / TestReport 行 ─
def test_accepts_serialized_row_with_snapshot_key():
    row = {"id": "a", "code": "RPT-0007", "status": "ready", "snapshot": snapshot()}
    view = manager_view(row)
    assert view["meta"]["status"] == "ready"
    assert view["meta"]["code"] == "RPT-0007"
    assert view["fail_rate"] == 0.15


def test_accepts_tortoise_like_row_object():
    view = executor_view(_FakeReportRow(snapshot()))
    assert view["meta"]["status"] == "ready"
    assert view["meta"]["generator"] == "auto"
    assert view["meta"]["run_id"] == "77777777-7777-7777-7777-777777777777"
    assert view["cases"][0]["status"] == "fail"


def test_rejects_unsupported_report_type():
    with pytest.raises(TypeError):
        manager_view("not-a-report")
    with pytest.raises(TypeError):
        audience_view(None, "manager")


# ── 8) 派发入口 ─────────────────────────────────────────────────
@pytest.mark.parametrize("audience,builder", [
    ("executor", executor_view),
    ("developer", developer_view),
    ("product", product_view),
    ("manager", manager_view),
])
def test_audience_view_dispatches_to_builder(audience, builder):
    snap = snapshot()
    assert audience_view(snap, audience) == builder(snap)


@pytest.mark.parametrize("bad", ["", "leader", "QA", "Executor", "dev", None, 3])
def test_audience_view_rejects_unknown_audience(bad):
    with pytest.raises(ValueError):
        audience_view(snapshot(), bad)


def test_audience_enumeration_is_the_four_readers():
    assert set(AUDIENCES) == {"executor", "developer", "product", "manager"}


# ── 9) 视图不改入参（快照是定版真相，视图只读）───────────────────
def test_views_do_not_mutate_the_snapshot():
    snap = snapshot()
    before = copy.deepcopy(snap)
    for audience in AUDIENCES:
        audience_view(snap, audience)
    assert snap == before, "受众视图必须是纯函数，不得改写定版快照"
