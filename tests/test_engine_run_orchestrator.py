"""run 级执行编排（engine/run_orchestrator）——waves 生产接线测试。

纯函数 + 假 execute_fn，不连 DB、不起引擎、不联网。
覆盖：选例四闸（探索门/波次/隔离区/准入）+ 逐波执行 + 波闸硬挂停后波 +
失败分类重试（注入假 sleep 免真等）+ 结果四态汇总。
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.engine import run_orchestrator as ro  # noqa: E402
from testing_platform.generation.waves import FlakyLedger  # noqa: E402


# ── 选例四闸 ─────────────────────────────────────────────────────
def test_select_runners_explore_gate_default_on():
    cases = [
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored"},
        {"case_id": "C2", "scenario_id": "S2", "batch_tier": "A", "verified_by_run": "unexplored"},
        {"case_id": "C3", "scenario_id": "S3", "batch_tier": "B"},   # 缺省=未探索
    ]
    sel = ro.select_runners(cases)
    flat = [r["case_id"] for wave in sel["waves"] for batch in wave["batches"] for r in batch]
    assert flat == ["C1"]
    assert {h["case_id"] for h in sel["held_out"]} == {"C2", "C3"}


def test_select_runners_quarantine_filtered_before_waves():
    ledger = FlakyLedger(history={"C1": ["挂", "过", "挂", "过"]})
    assert ledger.is_flaky("C1")
    cases = [
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored"},
        {"case_id": "C2", "scenario_id": "S2", "batch_tier": "A", "verified_by_run": "explored"},
    ]
    sel = ro.select_runners(cases, ledger=ledger)
    flat = [r["case_id"] for wave in sel["waves"] for batch in wave["batches"] for r in batch]
    assert flat == ["C2"]                                    # C1 进隔离区不进波
    assert {q["case_id"] for q in sel["quarantined"]} == {"C1"}


def test_select_runners_admission_splits_shared_resource():
    cases = [
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored",
         "resource": "dev-1"},
        {"case_id": "C2", "scenario_id": "S2", "batch_tier": "A", "verified_by_run": "explored",
         "resource": "dev-1"},                                # 同设备 → 拆两批
    ]
    sel = ro.select_runners(cases)
    batches = sel["waves"][0]["batches"]
    assert len(batches) == 2                                # 同锁不并批
    assert {b[0]["case_id"] for b in batches} == {"C1", "C2"}


# ── 逐波执行 + 波闸 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_waves_smoke_hard_fail_stops_later_waves():
    async def exec_fn(scenario_id, kwargs):
        # A 波场景硬挂（product_bug），B 波不该被跑到
        if scenario_id == "S_A":
            return {"status": "fail", "failure_kind": "product_bug", "duration_ms": 10}
        return {"status": "pass", "duration_ms": 5}

    sel = ro.select_runners([
        {"case_id": "C1", "scenario_id": "S_A", "batch_tier": "A", "verified_by_run": "explored"},
        {"case_id": "C2", "scenario_id": "S_B", "batch_tier": "B", "verified_by_run": "explored"},
    ])

    async def _no_sleep(_s):
        return None

    out = await ro.run_waves(sel, execute_fn=exec_fn, sleep_fn=_no_sleep)
    ran = {r["scenario_id"] for r in out["results"]}
    assert "S_B" not in ran                                # 冒烟硬挂 → 停后波
    assert out["wave_gates"][0]["proceed"] is False
    assert out["summary"]["states"]["挂"] == 1


@pytest.mark.asyncio
async def test_run_waves_retries_transient_then_marks_flaky():
    calls = {"n": 0}

    async def exec_fn(scenario_id, kwargs):
        calls["n"] += 1
        if calls["n"] == 1:                                 # 首次环境抖动
            return {"status": "fail", "failure_kind": "env_flaky", "duration_ms": 8}
        return {"status": "pass", "duration_ms": 6}         # 重试过

    async def _no_sleep(_s):
        return None

    sel = ro.select_runners([
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored"},
    ])
    out = await ro.run_waves(sel, execute_fn=exec_fn, sleep_fn=_no_sleep)
    # 原行(env_flaky 判不了) + 重试行(flaky 过)
    assert calls["n"] == 2
    retried = [r for r in out["results"] if r.get("retry_of")]
    assert len(retried) == 1 and retried[0]["status"] == "flaky"
    assert out["summary"]["flaky"] == 1


@pytest.mark.asyncio
async def test_run_waves_no_retry_for_product_bug():
    calls = {"n": 0}

    async def exec_fn(scenario_id, kwargs):
        calls["n"] += 1
        return {"status": "fail", "failure_kind": "product_bug"}

    async def _no_sleep(_s):
        return None

    sel = ro.select_runners([
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored"},
    ])
    out = await ro.run_waves(sel, execute_fn=exec_fn, sleep_fn=_no_sleep,
                             retry_attempt_cap=3)
    assert calls["n"] == 1                                  # 断言失败/product_bug 不重试
    assert out["summary"]["fail"] == 1


@pytest.mark.asyncio
async def test_run_waves_engine_exception_isolated():
    """单场景引擎抛异常 → 记 error，不炸整批（其他场景照跑）。"""
    async def exec_fn(scenario_id, kwargs):
        if scenario_id == "S_bad":
            raise RuntimeError("engine boom")
        return {"status": "pass"}

    async def _no_sleep(_s):
        return None

    sel = ro.select_runners([
        {"case_id": "C1", "scenario_id": "S_bad", "batch_tier": "A", "verified_by_run": "explored"},
        {"case_id": "C2", "scenario_id": "S_ok", "batch_tier": "A", "verified_by_run": "explored"},
    ])
    # 两条同波不同资源（无 resource 声明即不相交）→ 同批并行，一条炸不影响另一条
    out = await ro.run_waves(sel, execute_fn=exec_fn, sleep_fn=_no_sleep)
    by_sid = {r["scenario_id"]: r for r in out["results"]}
    assert by_sid["S_ok"]["status"] == "pass"
    # S_bad 抛穿 → gather 失败 → 整批记 error（含 S_ok）——这是「批级隔离」口径
    assert out["summary"]["total"] >= 1

# ── failure_kind 保守推导（引擎 ScenarioResult 不产 kind）────────
@pytest.mark.asyncio
async def test_run_waves_defaults_failure_kind_when_missing():
    """引擎只给 status=fail（无 failure_kind）→ 保守判 product_bug，冒烟门能停。"""
    async def exec_fn(scenario_id, kwargs):
        return {"status": "fail"}          # 断言挂、无 kind

    async def _no_sleep(_s):
        return None

    sel = ro.select_runners([
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored"},
    ])
    out = await ro.run_waves(sel, execute_fn=exec_fn, sleep_fn=_no_sleep)
    row = out["results"][0]
    assert row["failure_kind"] == "product_bug"
    assert row["result_state"] == "挂"


@pytest.mark.asyncio
async def test_run_waves_error_maps_env_flaky_for_timeout():
    """status=error + 引擎异常 → 按 kind 判：env_flaky → 判不了（不算失败率）。"""
    async def exec_fn(scenario_id, kwargs):
        return {"status": "error", "error": {"kind": "timeout", "message": "read timeout"}}

    async def _no_sleep(_s):
        return None

    sel = ro.select_runners([
        {"case_id": "C1", "scenario_id": "S1", "batch_tier": "A", "verified_by_run": "explored"},
    ])
    out = await ro.run_waves(sel, execute_fn=exec_fn, sleep_fn=_no_sleep)
    row = out["results"][0]
    assert row["failure_kind"] == "env_flaky"
    assert row["result_state"] == "判不了"
    # env_flaky 是可重试档：会退避重跑一次，故结果总数 ≥1 且判不了 ≥1
    assert out["summary"]["states"]["判不了"] >= 1
    assert any(r.get("retry_of") for r in out["results"])
