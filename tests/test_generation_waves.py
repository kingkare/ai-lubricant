"""执行编排规划层（25 号文「执行」环节）测试：四态映射 / 重试策略 / 波次 / 闸门 /
flaky 隔离区 / 并行准入 / 重试预算。

不联网、不启 DB、不调 AI——纯标准库函数 + 内存 ledger。
"""
from __future__ import annotations

import os
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation.waves import (  # noqa: E402
    DEFAULT_RESULT_STATE,
    RESULT_STATE_MAP,
    STATE_FAIL,
    STATE_INCONCLUSIVE,
    STATE_NOT_JUDGED,
    STATE_PASS,
    FlakyLedger,
    admission_check,
    failure_ratio,
    is_hard_fail,
    plan_waves,
    result_state,
    retry_budget_check,
    retry_plan,
    wave_gate_decision,
)

# ── 1. result_state 全组合 + 未知组合 ─────────────────────────────


def test_result_state_full_matrix():
    """25 号文正文表格逐行：每个已列组合都命中正确四态。"""
    assert result_state("pass", None) == STATE_PASS
    assert result_state("pass", "product_bug") == STATE_PASS  # pass × 任意
    assert result_state("fail", "product_bug") == STATE_FAIL
    assert result_state("error", "product_bug") == STATE_FAIL
    assert result_state("fail", "env_flaky") == STATE_INCONCLUSIVE
    assert result_state("error", "env_flaky") == STATE_INCONCLUSIVE
    assert result_state("fail", "script_issue") == STATE_INCONCLUSIVE
    assert result_state("error", "script_issue") == STATE_INCONCLUSIVE
    assert result_state("fail", "case_outdated") == STATE_NOT_JUDGED
    assert result_state("flaky", None) == STATE_INCONCLUSIVE  # flaky × 任意
    assert result_state("flaky", "product_bug") == STATE_INCONCLUSIVE
    assert result_state("skipped", None) == STATE_NOT_JUDGED
    assert result_state("timeout", None) == STATE_NOT_JUDGED


def test_result_state_unlisted_combinations_fall_back_conservative():
    """未列组合保守判不了：未知 status、error×case_outdated、NULL kind 的 fail/error。"""
    assert result_state("fail", None) == DEFAULT_RESULT_STATE == STATE_INCONCLUSIVE
    assert result_state("error", None) == STATE_INCONCLUSIVE
    assert result_state("error", "case_outdated") == STATE_INCONCLUSIVE  # 正文未列
    assert result_state("pass", "garbage") == STATE_PASS  # 通配行兜底
    assert result_state("weird_status", "product_bug") == STATE_INCONCLUSIVE
    assert result_state("", "") == STATE_INCONCLUSIVE


def test_result_state_case_and_space_insensitive():
    """大小写/空白不敏感（库里是自由文本 VARCHAR）。"""
    assert result_state(" PASS ") == STATE_PASS
    assert result_state("Fail", "Product_Bug") == STATE_FAIL
    assert result_state("fail", "") == STATE_INCONCLUSIVE  # 空串等价 NULL


def test_result_state_map_is_explicit_for_every_failure_kind():
    """表里四个 failure_kind 都有 pass 通配覆盖；挂只由 product_bug 产生。"""
    hard = [(st, k) for (st, k), v in RESULT_STATE_MAP.items() if v == STATE_FAIL]
    assert hard == [("fail", "product_bug"), ("error", "product_bug")]


def test_is_hard_fail_and_failure_ratio():
    """判不了/没裁不算硬挂、不进失败率分母。"""
    assert is_hard_fail(state=STATE_FAIL) is True
    assert is_hard_fail(state=STATE_INCONCLUSIVE) is False
    assert is_hard_fail({"status": "fail", "failure_kind": "env_flaky"}) is False
    assert is_hard_fail({"status": "fail", "failure_kind": "product_bug"}) is True
    assert is_hard_fail({"result_state": STATE_NOT_JUDGED}) is False

    states = [STATE_PASS, STATE_FAIL, STATE_INCONCLUSIVE, STATE_NOT_JUDGED, STATE_PASS]
    ratio, fails, counted = failure_ratio(states)
    assert (fails, counted) == (1, 3)  # 分母只算过/挂
    assert abs(ratio - 1 / 3) < 1e-9
    assert failure_ratio([STATE_INCONCLUSIVE, STATE_NOT_JUDGED])[2] == 0


# ── 2. retry_plan 各分类 ──────────────────────────────────────────


def test_retry_plan_product_bug_no_retry():
    """产品缺陷/断言失败：不重试，直接停（进缺陷链）。"""
    plan = retry_plan("product_bug", 0)
    assert plan["action"] == "stop"
    assert plan["retry"] is False
    assert plan["backoff_s"] == 0.0
    assert plan["max_attempts"] == 0
    assert plan["state"] == STATE_FAIL


def test_retry_plan_script_issue_no_retry():
    """脚本问题：不重试，回场景化重写（四态判不了）。"""
    plan = retry_plan("script_issue", 3)
    assert plan["action"] == "stop"
    assert plan["retry"] is False
    assert plan["state"] == STATE_INCONCLUSIVE


def test_retry_plan_case_outdated_goes_rework():
    """用例过期：不重试，进门② REJECT 回炉。"""
    plan = retry_plan("case_outdated", 0)
    assert plan["action"] == "rework"
    assert plan["retry"] is False
    assert plan["state"] == STATE_NOT_JUDGED


def test_retry_plan_env_flaky_two_jitter_retries_then_quarantine():
    """环境抖动：固定间隔重试×2，用尽进隔离区。"""
    p0 = retry_plan("env_flaky", 0)
    p1 = retry_plan("env_flaky", 1)
    p2 = retry_plan("env_flaky", 2)
    assert (p0["action"], p1["action"]) == ("retry", "retry")
    assert (p0["max_attempts"], p1["max_attempts"]) == (2, 2)
    assert p0["backoff_s"] > 0 and p0["backoff_s"] == p1["backoff_s"]  # 固定间隔
    assert p2["action"] == "quarantine"
    assert p2["backoff_s"] == 0.0
    assert p2["state"] == STATE_INCONCLUSIVE


def test_retry_plan_transient_exponential_backoff_times_three():
    """网络类瞬时故障：指数退避（1s→2s→4s），×3 后隔离。"""
    b = [retry_plan("transient", a)["backoff_s"] for a in range(3)]
    assert b == [1.0, 2.0, 4.0]
    assert retry_plan("transient", 2)["action"] == "retry"
    assert retry_plan("transient", 3)["action"] == "quarantine"


def test_retry_plan_rate_limited_waits_longer():
    """限流：长等待档，非抖动；用尽收口不判挂。"""
    p = retry_plan("rate_limited", 0)
    assert p["action"] == "retry"
    assert p["backoff_s"] >= 10.0
    assert retry_plan("rate_limited", 2)["action"] == "stop"


def test_retry_plan_unknown_kind_is_conservative():
    """未知分类：不重试、收口交人，绝不做无界重试。"""
    for bad in ("", None, "alien_failure"):
        p = retry_plan(bad, 0)
        assert p["action"] == "stop"
        assert p["retry"] is False
        assert p["max_attempts"] == 0


def test_retry_plan_aliases():
    """上游词别名折叠：network/timeout→transient，429→rate_limited。"""
    assert retry_plan("network", 0)["max_attempts"] == 3
    assert retry_plan("timeout", 0)["max_attempts"] == 3
    assert retry_plan("429", 0)["backoff_s"] >= 10.0
    assert retry_plan("flaky", 2)["action"] == "quarantine"


def test_retry_budget_check():
    """Retry Budget 封顶。"""
    assert retry_budget_check(0, 5) is True
    assert retry_budget_check(4, 5) is True
    assert retry_budget_check(5, 5) is False
    assert retry_budget_check(1, 0) is False  # 无预算
    assert retry_budget_check(0, None) is False


# ── 3. plan_waves 三波 + 无 tier 归 C ─────────────────────────────


def test_plan_waves_three_tiers_in_order():
    """A/B/C 三波按序返回；缺 B 波不出空波。"""
    rows = [
        {"case_id": "c1", "batch_tier": "A", "scenario_id": "s1", "requirement_id": "r1"},
        {"case_id": "c2", "batch_tier": "C", "scenario_id": "s2", "requirement_id": "r1"},
        {"case_id": "c3", "batch_tier": "A", "scenario_id": "s3", "requirement_id": "r2"},
        {"case_id": "c4", "batch_tier": "B", "scenario_id": "s4", "requirement_id": "r2"},
    ]
    waves = plan_waves(rows)
    assert [w["wave"] for w in waves] == ["A", "B", "C"]
    assert [i["case_id"] for i in waves[0]["items"]] == ["c1", "c3"]
    assert [i["case_id"] for i in waves[1]["items"]] == ["c4"]
    assert [i["case_id"] for i in waves[2]["items"]] == ["c2"]
    assert all(w["count"] == len(w["items"]) for w in waves)


def test_plan_waves_no_tier_falls_to_c():
    """无 batch_tier（缺失/None/空串/未知值）一律归 C。"""
    rows = [
        {"case_id": "c1", "scenario_id": "s1"},
        {"case_id": "c2", "batch_tier": None},
        {"case_id": "c3", "batch_tier": "  "},
        {"case_id": "c4", "batch_tier": "Z"},
    ]
    waves = plan_waves(rows)
    assert [w["wave"] for w in waves] == ["C"]
    assert {i["case_id"] for i in waves[0]["items"]} == {"c1", "c2", "c3", "c4"}


def test_plan_waves_empty_and_dedup():
    """空输入返回空；同 case_id 去重（只跑一遍）。"""
    assert plan_waves([]) == []
    rows = [
        {"case_id": "dup", "batch_tier": "A", "scenario_id": "s1"},
        {"case_id": "dup", "batch_tier": "A", "scenario_id": "s2"},
    ]
    waves = plan_waves(rows)
    assert len(waves) == 1
    assert len(waves[0]["items"]) == 1
    assert waves[0]["items"][0]["scenario_id"] == "s1"


def test_plan_waves_lowercase_tier_accepted():
    """大小写不敏感（小写 a/b/c 也归对应波）。"""
    waves = plan_waves([{"case_id": "c", "batch_tier": "a", "scenario_id": "s"}])
    assert waves[0]["wave"] == "A"


# ── 4. wave_gate_decision ─────────────────────────────────────────


def test_wave_gate_hard_fail_stops_next_wave():
    """A 波出现挂（fail×product_bug）→ 停后波。"""
    results = [
        {"status": "pass"},
        {"status": "fail", "failure_kind": "product_bug"},
        {"status": "pass"},
    ]
    gate = wave_gate_decision(results)
    assert gate["proceed"] is False
    assert gate["hard_fails"] == 1
    assert "停后波" in gate["reason"]


def test_wave_gate_inconclusive_and_not_judged_do_not_stop():
    """判不了/没裁不算硬挂：环境抖动/脚本问题/用例过期/skipped 全放行。"""
    results = [
        {"status": "fail", "failure_kind": "env_flaky"},
        {"status": "error", "failure_kind": "script_issue"},
        {"status": "fail", "failure_kind": "case_outdated"},
        {"status": "skipped"},
        {"status": "flaky"},
        {"status": "timeout"},
    ]
    gate = wave_gate_decision(results)
    assert gate["proceed"] is True
    assert gate["hard_fails"] == 0


def test_wave_gate_all_pass_proceeds():
    """全过 → 继续后波。"""
    gate = wave_gate_decision([{"status": "pass"}, {"status": "pass"}])
    assert gate["proceed"] is True
    assert gate["fail_ratio"] == 0.0


def test_wave_gate_empty_and_preset_state():
    """空结果放行；调用方已派生 result_state 的行直接可用。"""
    assert wave_gate_decision([])["proceed"] is True
    assert wave_gate_decision([{"result_state": STATE_FAIL}])["proceed"] is False
    assert wave_gate_decision([{"result_state": STATE_INCONCLUSIVE}])["proceed"] is True


# ── 5. FlakyLedger ────────────────────────────────────────────────


def test_flaky_ledger_detects_jitter_and_quarantines():
    """挂→过反复翻转 ≥2 轮 → 判 flaky 且进隔离区。"""
    ledger = FlakyLedger()
    for st in ["fail", "pass", "fail", "pass", "fail", "pass"]:
        ledger.record("c1", st)
    assert ledger.is_flaky("c1") is True
    assert "c1" in ledger.quarantine_list()


def test_flaky_ledger_stable_case_not_hurt():
    """稳定过 / 稳定挂都不误伤。"""
    ledger = FlakyLedger()
    for _ in range(6):
        ledger.record("stable_ok", "pass")
        ledger.record("always_fail", "fail")
    assert ledger.is_flaky("stable_ok") is False
    assert ledger.is_flaky("always_fail") is False  # 确定性缺陷不是抖动
    assert ledger.quarantine_list() == []


def test_flaky_ledger_single_blip_is_flaky_but_not_quarantined():
    """单点毛刺（P,F,P）算 flaky 可重试，但不满连续 ≥2 轮，不进隔离区。"""
    ledger = FlakyLedger()
    for st in ["pass", "fail", "pass", "pass", "pass"]:
        ledger.record("c1", st)
    assert ledger.is_flaky("c1") is True
    assert ledger.quarantine_list() == []


def test_flaky_ledger_sliding_window_ignores_old_history():
    """窗口外历史不参与：早期抖动被稳定记录滑出后不再判 flaky。"""
    ledger = FlakyLedger()
    for st in ["fail", "pass", "fail", "pass"]:
        ledger.record("c1", st)
    assert ledger.is_flaky("c1", window=10) is True
    for _ in range(10):
        ledger.record("c1", "pass")
    assert ledger.is_flaky("c1", window=10) is False


def test_flaky_ledger_threshold_and_engine_status():
    """threshold 生效；直接记引擎 status 也归一成四态。"""
    ledger = FlakyLedger()
    for st in ["fail", "fail", "pass"]:
        ledger.record("c1", st)
    assert ledger.is_flaky("c1", window=10, threshold=0.3) is True
    assert ledger.is_flaky("c1", window=10, threshold=0.6) is False  # 1 挂翻回过 / 2 挂 = 0.5

    ledger.record("c2", "skipped")
    ledger.record("c2", "fail")
    ledger.record("c2", "pass")
    assert ledger.is_flaky("c2") is True


def test_flaky_ledger_roundtrip_snapshot():
    """快照往返：to_dict/from_dict 保持判定一致（调用方负责持久化）。"""
    ledger = FlakyLedger()
    for st in ["fail", "pass", "fail", "pass"]:
        ledger.record("c1", st)
    restored = FlakyLedger.from_dict(ledger.to_dict())
    assert restored.is_flaky("c1") is True
    assert restored.quarantine_list() == ledger.quarantine_list()
    assert restored.states("c1") == ledger.states("c1")
    assert "c1" in restored and len(restored) == 1


# ── 6. admission_check ────────────────────────────────────────────


def test_admission_device_exclusive_same_resource_splits():
    """设备一机一锁：同 resource 不能同批。"""
    items = [
        {"case_id": "c1", "resource": "device:phone1"},
        {"case_id": "c2", "resource": "device:phone1"},
        {"case_id": "c3", "resource": "device:phone2"},
    ]
    batches = admission_check(items)
    assert len(batches) == 2
    assert {i["case_id"] for i in batches[0]} == {"c1", "c3"}
    assert [i["case_id"] for i in batches[1]] == ["c2"]


def test_admission_no_resource_all_parallel():
    """无设备要求的纯接口用例 → 一个批次全并行。"""
    items = [{"case_id": f"c{i}", "resource": None, "data_scope": f"ds{i}"} for i in range(4)]
    batches = admission_check(items)
    assert len(batches) == 1
    assert len(batches[0]) == 4


def test_admission_same_data_scope_splits():
    """data_scope 字符串相同 → 可能相交 → 拆不同批次（保守）。"""
    items = [
        {"case_id": "c1", "data_scope": "order:1001"},
        {"case_id": "c2", "data_scope": "order:1001"},
        {"case_id": "c3", "data_scope": "order:1002"},
    ]
    batches = admission_check(items)
    assert len(batches) == 2
    assert {i["case_id"] for i in batches[0]} == {"c1", "c3"}
    assert [i["case_id"] for i in batches[1]] == ["c2"]


def test_admission_busy_resource_deferred():
    """资源已被占：条目派不进任何批，进 deferred_out（不静默丢用例）。"""
    deferred: list[dict] = []
    items = [
        {"case_id": "c1", "resource": "device:phone1"},
        {"case_id": "c2", "resource": "device:phone2"},
        {"case_id": "c3", "resource": "device:phone1"},
    ]
    batches = admission_check(items, busy_resources={"device:phone1"}, deferred_out=deferred)
    assert [i["case_id"] for i in batches[0]] == ["c2"]
    assert {d["case_id"] for d in deferred} == {"c1", "c3"}
    assert all("admission_defer" in d for d in deferred)


def test_admission_combined_device_and_scope_conflicts():
    """设备不同但数据范围相同 → 仍拆批（两个约束都要满足）。"""
    items = [
        {"case_id": "c1", "resource": "device:p1", "data_scope": "ds"},
        {"case_id": "c2", "resource": "device:p2", "data_scope": "ds"},
        {"case_id": "c3", "resource": "device:p3", "data_scope": "other"},
    ]
    batches = admission_check(items)
    assert len(batches) == 2
    assert {i["case_id"] for i in batches[0]} == {"c1", "c3"}
    assert [i["case_id"] for i in batches[1]] == ["c2"]


def test_admission_without_deferred_out_still_returns_items():
    """没传 deferred_out 时被占用条目以独立批次返回，不凭空消失。"""
    items = [
        {"case_id": "c1", "resource": "device:p1"},
        {"case_id": "c2", "resource": "device:p2"},
    ]
    batches = admission_check(items, busy_resources={"device:p1"})
    flat = [i["case_id"] for b in batches for i in b]
    assert sorted(flat) == ["c1", "c2"]


def test_admission_empty_items():
    assert admission_check([]) == []


# ── 7. 端到端：一轮执行编排的决策串联 ─────────────────────────────


def test_planning_pipeline_end_to_end():
    """波次 → 熔断 → 隔离 → 准入 的决策链一次走通（不真执行）。"""
    cases = [
        {"case_id": "cA1", "batch_tier": "A", "resource": "device:p1", "data_scope": "ds1"},
        {"case_id": "cA2", "batch_tier": "A", "resource": "device:p1", "data_scope": "ds2"},
        {"case_id": "cB1", "batch_tier": "B", "resource": "device:p2", "data_scope": "ds1"},
    ]
    waves = plan_waves(cases)
    assert [w["wave"] for w in waves] == ["A", "B"]

    smoke = wave_gate_decision([
        {"status": "fail", "failure_kind": "product_bug", "case_id": "cA1"},
        {"status": "pass", "case_id": "cA2"},
    ])
    assert smoke["proceed"] is False  # 冒烟硬挂 → B 波不跑

    ledger = FlakyLedger()
    for st in ["fail", "pass", "fail", "pass"]:
        ledger.record("cB1", st)
    assert "cB1" in ledger.quarantine_list()  # 隔离区先滤

    quarantine = set(ledger.quarantine_list())
    schedulable = [c for c in cases if c["case_id"] not in quarantine]
    batches = admission_check(schedulable, busy_resources=set())
    # cA1/cA2 共用 device:p1 → 一机一锁拆两批；cB1 已进隔离区不参与
    assert [sorted(i["case_id"] for i in b) for b in batches] == [["cA1"], ["cA2"]]


# ── plan_waves 探索门闸（§环节7「未过门不进波次」）──────────────
def test_plan_waves_require_explored_filters_unverified():
    rows = [
        {"case_id": "C1", "batch_tier": "A", "verified_by_run": "explored"},
        {"case_id": "C2", "batch_tier": "A", "verified_by_run": "unexplored"},
        {"case_id": "C3", "batch_tier": "B", "verified_by_run": "failed"},
        {"case_id": "C4", "batch_tier": "C"},                       # 缺省列=未探索
    ]
    held: list[dict] = []
    waves = plan_waves(rows, require_explored=True, held_out=held)
    flat = [r["case_id"] for w in waves for r in w["items"]]
    assert flat == ["C1"]                                          # 只放行已探索
    assert {h["case_id"] for h in held} == {"C2", "C3", "C4"}
    assert all(h["held_reason"].startswith("未过探索门") for h in held)


def test_plan_waves_require_explored_false_keeps_all():
    """缺省不过滤：存量调用方语义不变（不带 verified 的也照常进波）。"""
    rows = [
        {"case_id": "C1", "batch_tier": "A"},
        {"case_id": "C2", "batch_tier": "A", "verified_by_run": "failed"},
    ]
    waves = plan_waves(rows)
    flat = [r["case_id"] for w in waves for r in w["items"]]
    assert flat == ["C1", "C2"]
