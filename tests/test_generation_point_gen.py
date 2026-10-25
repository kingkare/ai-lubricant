"""提点规程（25 号文环节 3）——纯函数 + 提示词契约，不联网、不启 DB、不调 AI。

覆盖：
1. ``from_criteria``：每条可测验收标准 ≥1 正常点 + ≥1 异常点，basis/evidence 引用正确；
2. ``from_invariants``：每条恒真式 1 个 C09 数据一致性点，basis=fact；
3. ``from_register``：kind=to_confirm 且带 question_id → 挂起占位点；
4. ``auto_axes``：接口方向三轴（C12/C03/C04）；
5. ``self_check``：漏恒真式/漏风险方向报 error；
6. ``assign_batches``：比例 + 风险降序 + 不修改输入；
7. ``build_point_prompt``：含六规程、输出 JSON 契约关键词、纪律；
8. 机械铺轴说明不进 agent 提示词（调度器专属）。
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation.point_gen import (  # noqa: E402
    API_MANDATORY_AXES,
    MECHANICAL_AXES_NOTE,
    POINT_SOURCES,
    assign_batches,
    auto_axes,
    build_point_prompt,
    from_criteria,
    from_invariants,
    from_register,
    self_check,
)
from testing_platform.quality.errors import SEVERITY_ERROR, ValidationResult  # noqa: E402


# ── 1. criteria 直转 ─────────────────────────────────────────────

def test_from_criteria_two_points_per_entry_with_fact_basis():
    pts = from_criteria([{"id": "AC-1", "condition": "订单金额大于 0", "expected": "订单创建成功"}])

    assert len(pts) == 2  # ≥1 正常 + ≥1 异常
    normal, negative = pts

    assert normal["title"] == "验证订单金额大于 0的正常路径"
    assert normal["coverage_axis"] == ["C01"]
    assert normal["basis"] == "fact"
    assert normal["evidence"] == ["AC-1"]

    assert negative["title"] == "验证订单金额大于 0失败被拒"
    assert negative["coverage_axis"] == ["C02"]
    assert negative["basis"] == "fact"
    assert negative["evidence"] == ["AC-1"]


def test_from_criteria_all_five_fields_present():
    pts = from_criteria([{"id": "AC-2", "condition": "手机号合法", "expected": "短信下发"}])
    for point in pts:
        for key in ("title", "trigger", "sample_data", "verify_result", "impact"):
            assert key in point, key
        assert point["design_method"]
        assert point["batch_tier"]
        assert point["source"] == "criteria_direct"


def test_from_criteria_skips_untestable_and_empty():
    pts = from_criteria([
        {"id": "AC-3", "condition": "性能要好", "testable": False},
        {"id": "AC-4", "condition": ""},
        {"id": "AC-5", "condition": "库存扣减成功"},
    ])
    assert [p["evidence"][0] for p in pts] == ["AC-5", "AC-5"]
    assert len(pts) == 2


def test_from_criteria_falls_back_to_generated_ac_id():
    pts = from_criteria([{"condition": "登录成功", "expected": "返回 token"}])
    assert pts[0]["evidence"] == ["AC-1"]


def test_from_criteria_multiple_entries_scale_up():
    pts = from_criteria([
        {"id": "AC-1", "condition": "a"},
        {"id": "AC-2", "condition": "b"},
    ])
    assert len(pts) == 4
    assert [p["evidence"][0] for p in pts] == ["AC-1", "AC-1", "AC-2", "AC-2"]


# ── 2. invariants 直转 ───────────────────────────────────────────

def test_from_invariants_one_c09_point_each_fact_basis():
    pts = from_invariants([{"id": "INV-1", "statement": "库存扣减后不小于零"}])

    assert len(pts) == 1
    point = pts[0]
    assert point["coverage_axis"] == ["C09"]
    assert point["basis"] == "fact"
    assert point["evidence"] == ["INV-1"]
    assert "INV-1" in point["verify_result"]
    assert point["source"] == "invariants_direct"


def test_from_invariants_multiple_and_default_id():
    pts = from_invariants([
        {"id": "INV-1", "statement": "余额守恒"},
        {"statement": "状态单调推进"},
    ])
    assert [p["evidence"][0] for p in pts] == ["INV-1", "INV-2"]


# ── 3. register 挂起占位 ─────────────────────────────────────────

def test_from_register_pending_placeholder():
    pts = from_register([{"kind": "to_confirm", "question_id": "Q-77", "claim": "并发上限未定义"}])

    assert len(pts) == 1
    point = pts[0]
    assert point["basis"] == "to_confirm"
    assert point["evidence"] == ["Q-77"]
    assert "Q-77" in point["title"]
    assert point["source"] == "register_placeholder"


def test_from_register_filters_non_pending_and_missing_question():
    pts = from_register([
        {"kind": "fact", "question_id": "Q-1"},          # 不是 to_confirm
        {"kind": "to_confirm"},                          # 缺 question_id
        {"kind": "TO_CONFIRM", "question_id": "Q-2"},    # 大小写不敏感
    ])
    assert len(pts) == 1
    assert pts[0]["evidence"] == ["Q-2"]


# ── 4. 机械轴先铺 ────────────────────────────────────────────────

def test_auto_axes_api_direction_three_axes():
    assert auto_axes("支付接口", True) == ["C12", "C03", "C04"]
    assert auto_axes("支付接口", True) == list(API_MANDATORY_AXES)


def test_auto_axes_api_keyword_direction():
    # 代码先行：文本本身点到接口也算接口方向
    assert auto_axes("订单 HTTP 接口", False) == ["C12", "C03", "C04"]
    assert auto_axes("api 校验", False) == ["C12", "C03", "C04"]


def test_auto_axes_non_api_direction_empty():
    assert auto_axes("页面交互流畅度", False) == []


# ── 5. 自检全覆盖 ────────────────────────────────────────────────

def test_self_check_ok_when_all_covered():
    pts = [
        {"title": "t1", "coverage_axis": ["C09"], "evidence": ["INV-1"]},
        {"title": "t2", "coverage_axis": ["C02"], "evidence": ["D1"]},
    ]
    res = self_check(pts, [{"id": "INV-1", "statement": "余额守恒"}], [{"id": "D1", "name": "支付风险"}])
    assert isinstance(res, ValidationResult)
    assert res.valid
    assert res.issues == []


def test_self_check_missing_invariant_is_error():
    res = self_check([], [{"id": "INV-1", "statement": "余额守恒"}], [])
    assert not res.valid
    assert [i.rule for i in res.errors] == ["R_point_invariant_uncovered"]
    assert res.errors[0].severity == SEVERITY_ERROR
    assert "余额守恒" in res.errors[0].msg


def test_self_check_missing_risk_direction_is_error():
    res = self_check([{"title": "t", "coverage_axis": ["C01"]}], [], [{"id": "D1", "name": "并发"}])
    assert not res.valid
    assert [i.rule for i in res.errors] == ["R_point_direction_uncovered"]


def test_self_check_invariant_point_without_c09_axis_fails():
    # 引了 INV id 但没有 C09 轴 → 不算「直测」
    res = self_check(
        [{"title": "t", "coverage_axis": ["C01"], "evidence": ["INV-1"]}],
        [{"id": "INV-1", "statement": "余额守恒"}],
        [],
    )
    assert not res.valid


# ── 6. 分批 ─────────────────────────────────────────────────────

def _points_with_risk(n: int):
    return [{"title": f"t{i}", "risk": {"impact": i, "probability": 1}} for i in range(1, n + 1)]


def test_assign_batches_ratio_and_descending_order():
    assigned = assign_batches(_points_with_risk(10), smoke_ratio=0.2)

    assert len(assigned) == 10
    tiers = [p["batch_tier"] for p in assigned]
    assert tiers.count("A") == 2  # 20% of 10
    assert tiers.count("B") == 4
    assert tiers.count("C") == 4
    # 降序：t10..t1
    assert [p["title"] for p in assigned] == [f"t{i}" for i in range(10, 0, -1)]
    assert [p["title"] for p in assigned if p["batch_tier"] == "A"] == ["t10", "t9"]


def test_assign_batches_does_not_mutate_input():
    src = _points_with_risk(4)
    snapshot = [dict(p) for p in src]
    assign_batches(src, smoke_ratio=0.5)
    assert src == snapshot
    assert "batch_tier" not in src[0]


def test_assign_batches_single_point_gets_smoke():
    assigned = assign_batches([{"title": "only"}])
    assert assigned[0]["batch_tier"] == "A"


def test_assign_batches_empty_input():
    assert assign_batches([]) == []


def test_assign_batches_uses_impact_times_probability():
    # 顶层 impact/probability 数值 → 乘积；否则默认 3×3 中性档
    assigned = assign_batches(
        [
            {"title": "high", "impact": 5, "probability": 5},
            {"title": "mid", "risk": {"impact": 3, "probability": 2}},
            {"title": "neutral"},  # 无分数 → 3×3 = 9，排在 mid(6) 之后
        ],
        smoke_ratio=1 / 3,
    )
    assert [p["title"] for p in assigned] == ["high", "neutral", "mid"]


def test_assign_batches_text_impact_not_treated_as_zero():
    # impact 是五字段的说明文字（非数值）→ 走默认 3×3，不塌成 0
    assigned = assign_batches(
        [
            {"title": "described", "impact": "资损"},
            {"title": "explicit_low", "impact": 1, "probability": 1},
        ],
        smoke_ratio=0.5,
    )
    assert [p["title"] for p in assigned] == ["described", "explicit_low"]


def test_assign_batches_stable_within_tier_on_tie():
    assigned = assign_batches(
        [{"title": "a", "impact": 3, "probability": 3}, {"title": "b", "impact": 3, "probability": 3}],
        smoke_ratio=0.5,
    )
    assert [p["title"] for p in assigned] == ["a", "b"]


def test_assign_batches_ratio_clamped():
    assigned = assign_batches(_points_with_risk(10), smoke_ratio=1.5)
    assert all(p["batch_tier"] == "A" for p in assigned)


# ── 7. 提示词契约 ────────────────────────────────────────────────

def _prompt() -> str:
    return build_point_prompt(
        {
            "requirement": {"id": "REQ-1", "title": "支付改造"},
            "criteria": [{"id": "AC-1", "condition": "金额大于 0", "expected": "创建订单"}],
            "invariants": [{"id": "INV-1", "statement": "余额守恒"}],
            "register": [{"kind": "to_confirm", "question_id": "Q-1", "claim": "并发上限未定义"}],
        },
        {"payment": {"impact": 5, "probability": 4, "weight": 0.9}},
    )


def test_build_point_prompt_mentions_all_six_procedures():
    prompt = _prompt()
    for proc in POINT_SOURCES:
        assert proc["id"] in prompt, proc["id"]
    assert "IN THIS ORDER" in prompt


def test_build_point_prompt_contains_json_contract_keys():
    prompt = _prompt()
    for key in (
        "title", "trigger", "sample_data", "verify_result", "impact", "evidence",
        "coverage_axis", "surfaces", "design_method", "batch_tier", "basis",
    ):
        assert key in prompt, key
    assert "JSON array" in prompt
    # 依据三分
    for kind in ("fact", "assumption", "to_confirm"):
        assert kind in prompt, kind


def test_build_point_prompt_contains_input_slice_and_weights():
    prompt = _prompt()
    assert "AC-1" in prompt
    assert "INV-1" in prompt
    assert "Q-1" in prompt
    assert '"payment"' in prompt


def test_build_point_prompt_contains_three_disciplines():
    prompt = _prompt()
    assert "nominate_only" in prompt
    assert "persist_before_consult" in prompt
    assert "reconcile_conflicts" in prompt
    # 纪律正文关键句
    assert "NOMINATE ONLY" in prompt
    assert "Persist before consulting" in prompt
    assert "never silently swap" in prompt


def test_mechanical_axes_note_is_not_in_prompt():
    """「接口相关方向必出 C12/C03/C04 轴」是调度器说明，不进 agent 提示词。"""
    prompt = _prompt()
    assert "must emit" not in prompt.lower()
    assert MECHANICAL_AXES_NOTE not in prompt
    # 机械三轴只作为轴词表出现，不是强制铺轴指令
    assert "C12" in prompt
    assert "must produce C12" not in prompt


def test_build_point_prompt_empty_inputs_still_valid():
    prompt = build_point_prompt({}, {})
    assert "test-point extraction agent" in prompt
    for proc in POINT_SOURCES:
        assert proc["id"] in prompt


def test_point_sources_constant_shape():
    assert len(POINT_SOURCES) == 6
    assert [p["id"] for p in POINT_SOURCES] == [
        "criteria_direct",
        "invariants_direct",
        "risk_generator_expand",
        "register_placeholder",
        "five_fields_annotated",
        "self_coverage",
    ]
    for proc in POINT_SOURCES:
        assert proc["name"] and proc["desc"]
