"""上线判定双条件门（25 号文环节 10，waves.release_gate）测试。

不联网、不启 DB、不调 AI——纯标准库函数。
口径：共识 go **且** 置信≥0.9（含边界）**且** 回滚触发器两栏（auto_rollback/
manual_rollback）事前声明齐，三条件合取；任一不满足 → no_go + 聚合理由逐条；
弃权/None/空一律不放行；confidence 越界 / rollback_plan 非 dict → ValueError。
"""
from __future__ import annotations

import os
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

import pytest  # noqa: E402

from testing_platform.generation.waves import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    CONSENSUS_GO,
    RELEASE_DECISIONS,
    ROLLBACK_COLUMNS,
    SUCCESS_CRITERION,
    admission_check,
    release_gate,
    result_state,
    wave_gate_decision,
)

# 合法 go 输入的基线参数（各用例按需覆盖单字段）。


def _go_kwargs() -> dict:
    return {
        "consensus": "go",
        "confidence": 0.9,
        "rollback_plan": {
            "auto_rollback": "错误率>5% 持续5分钟",
            "manual_rollback": "值班人一键回退按钮",
        },
    }


# ── 1. 全满足 → go ────────────────────────────────────────────────


def test_full_pass_returns_go():
    """三条件全满足：go + passed + 空 reasons + 三 ok 全 True。"""
    out = release_gate(**_go_kwargs())
    assert out["decision"] == "go"
    assert out["passed"] is True
    assert out["reasons"] == []
    assert out["consensus_ok"] is True
    assert out["confidence_ok"] is True
    assert out["rollback_ok"] is True
    assert out["success_criterion"] == SUCCESS_CRITERION == "no_rollback_and_no_incident"


def test_confidence_boundary_exactly_09_passes():
    """含边界：confidence == 0.9 恰好过（>= 而非 >）。"""
    assert release_gate(**_go_kwargs())["decision"] == "go"
    assert release_gate(**{**_go_kwargs(), "confidence": 0.90001})["decision"] == "go"
    assert release_gate(**{**_go_kwargs(), "confidence": 1.0})["decision"] == "go"


def test_consensus_case_and_whitespace_insensitive():
    """共识归一大小写/空白不敏感。"""
    for raw in ("GO", "Go", " go ", "  GO  "):
        out = release_gate(**{**_go_kwargs(), "consensus": raw})
        assert out["decision"] == "go", raw
        assert out["consensus_ok"] is True


def test_regression_and_plan_echoed_back():
    """regression 不参与判定，但原样带出（浅拷贝，不改入参）。"""
    reg = {"must": 3, "should": 5, "sample": 2}
    plan = _go_kwargs()["rollback_plan"]
    out = release_gate(**_go_kwargs(), regression=reg)
    assert out["regression"] == reg
    assert out["regression"] is not reg
    assert out["rollback_plan"] == plan
    assert out["rollback_plan"] is not plan


# ── 2. 共识维：弃权/None/空/别的值都不放行 ─────────────────────────


def test_consensus_abstain_blocks():
    """abstain（任意大小写）= 弃权不放行。"""
    for raw in ("abstain", "ABSTAIN", " Abstain "):
        out = release_gate(**{**_go_kwargs(), "consensus": raw})
        assert out["decision"] == "no_go"
        assert out["passed"] is False
        assert out["consensus_ok"] is False
        assert len(out["reasons"]) == 1
        assert "弃权" in out["reasons"][0]


def test_consensus_none_or_empty_blocks():
    """None / 空串 / 纯空白 = 共识缺失，不放行。"""
    for raw in (None, "", "   "):
        out = release_gate(**{**_go_kwargs(), "consensus": raw})
        assert out["decision"] == "no_go"
        assert out["consensus_ok"] is False
        assert "缺失" in out["reasons"][0] or "为空" in out["reasons"][0]


def test_consensus_no_go_or_garbage_blocks():
    """no_go / 其他任意字面量都不是 go。"""
    for raw in ("no_go", "NO-GO", "no go", "gogo", "pass", "pending"):
        out = release_gate(**{**_go_kwargs(), "consensus": raw})
        assert out["decision"] == "no_go"
        assert out["consensus_ok"] is False
        assert "go" in out["reasons"][0]  # 理由里写明仅 consensus=go 可放行


# ── 3. 置信维：< 0.9 拦下（哪怕共识 go 且两栏齐） ──────────────────


@pytest.mark.parametrize("conf", [0.0, 0.5, 0.89, 0.899999])
def test_confidence_below_threshold_blocks(conf):
    """共识 go + 两栏齐，但置信 < 0.9 → no_go，理由逐条点名置信。"""
    out = release_gate(**{**_go_kwargs(), "confidence": conf})
    assert out["decision"] == "no_go"
    assert out["passed"] is False
    assert out["confidence_ok"] is False
    assert out["consensus_ok"] is True and out["rollback_ok"] is True
    assert len(out["reasons"]) == 1
    assert "置信不足" in out["reasons"][0]


# ── 4. 回滚两栏维：缺任一栏/空值都不放行 ──────────────────────────


@pytest.mark.parametrize(
    "plan",
    [
        None,  # 整个没传
        {},  # 空 dict
        {"auto_rollback": "错误率>5%"},  # 缺 manual_rollback
        {"manual_rollback": "人来回"},  # 缺 auto_rollback
        {"auto_rollback": "", "manual_rollback": "人来回"},  # auto 空串
        {"auto_rollback": "   ", "manual_rollback": "人来回"},  # auto 纯空白
        {"auto_rollback": "x", "manual_rollback": None},  # manual None
        {"auto_rollback": "x", "manual_rollback": ""},  # manual 空串
    ],
)
def test_rollback_plan_incomplete_blocks(plan):
    """两栏任一缺失/空 → no_go；reasons 里逐个点名缺哪栏。"""
    out = release_gate(**{**_go_kwargs(), "rollback_plan": plan})
    assert out["decision"] == "no_go"
    assert out["passed"] is False
    assert out["rollback_ok"] is False
    assert len(out["reasons"]) == 1
    assert "回滚触发器未事前声明齐" in out["reasons"][0]
    assert ("auto_rollback" in out["reasons"][0]) or ("manual_rollback" in out["reasons"][0])


def test_rollback_columns_both_missing_named():
    """两栏全缺：理由把两个栏名都点出来。"""
    out = release_gate(**{**_go_kwargs(), "rollback_plan": {}})
    missing = out["reasons"][0]
    assert "auto_rollback" in missing and "manual_rollback" in missing


def test_rollback_column_non_string_value_not_filled():
    """「非空字符串」口径：非字符串值一律按未声明处理（fail-closed，同准入闸风格）。"""
    bad = release_gate(**{**_go_kwargs(), "rollback_plan": {"auto_rollback": 1, "manual_rollback": "x"}})
    assert bad["rollback_ok"] is False and bad["decision"] == "no_go"
    ok = release_gate(**{**_go_kwargs(), "rollback_plan": {"auto_rollback": "错误率>5%", "manual_rollback": "值班人确认"}})
    assert ok["rollback_ok"] is True and ok["decision"] == "go"


# ── 5. 多条件同时不满足：reasons 聚合逐条 ─────────────────────────


def test_all_three_fail_aggregates_all_reasons():
    """共识弃权 + 置信低 + 两栏全缺：三条理由一条不少，顺序 = 共识→置信→回滚。"""
    out = release_gate(consensus="abstain", confidence=0.42, rollback_plan={})
    assert out["decision"] == "no_go"
    assert out["passed"] is False
    assert len(out["reasons"]) == 3
    assert "弃权" in out["reasons"][0]
    assert "置信不足" in out["reasons"][1]
    assert "auto_rollback" in out["reasons"][2] and "manual_rollback" in out["reasons"][2]
    assert out["consensus_ok"] is False
    assert out["confidence_ok"] is False
    assert out["rollback_ok"] is False


def test_two_fail_aggregates_two_reasons():
    """共识 no_go + 无回滚计划：两条理由。"""
    out = release_gate("no_go", 0.95, rollback_plan=None)
    assert len(out["reasons"]) == 2
    assert out["decision"] == "no_go"


# ── 6. 非法输入：ValueError，不静默降级 ───────────────────────────


@pytest.mark.parametrize("conf", [-0.01, 1.01, 2.0, -1.0, 100])
def test_confidence_out_of_range_raises(conf):
    """越界（哪怕只差一点点）→ ValueError。"""
    with pytest.raises(ValueError, match="越界"):
        release_gate(**{**_go_kwargs(), "confidence": conf})


@pytest.mark.parametrize("conf", [None, "0.95", object(), [0.9]])
def test_confidence_non_numeric_raises(conf):
    """非数字（含数字字符串）→ ValueError，不 try-parse 救场。"""
    with pytest.raises(ValueError, match="必须是数字"):
        release_gate(**{**_go_kwargs(), "confidence": conf})


@pytest.mark.parametrize("plan", ["两栏都写了", ["auto_rollback", "manual_rollback"], 42, ("a", "b")])
def test_rollback_plan_non_mapping_raises(plan):
    """rollback_plan 给了但不是 Mapping → ValueError。"""
    with pytest.raises(ValueError, match="rollback_plan"):
        release_gate(**{**_go_kwargs(), "rollback_plan": plan})


def test_valueerror_even_when_other_conditions_also_bad():
    """非法输入优先炸：共识弃权 + 置信越界也是 ValueError（校验先于判定）。"""
    with pytest.raises(ValueError):
        release_gate(consensus=None, confidence=7.7)


# ── 7. 常量与既有函数零回归 ───────────────────────────────────────


def test_module_constants():
    """常量口径与正文一致。"""
    assert RELEASE_DECISIONS == ("go", "no_go")
    assert CONSENSUS_GO == "go"
    assert CONFIDENCE_THRESHOLD == 0.9
    assert ROLLBACK_COLUMNS == ("auto_rollback", "manual_rollback")
    assert SUCCESS_CRITERION == "no_rollback_and_no_incident"


def test_no_regression_on_existing_pure_functions():
    """既有函数签名/行为不被本次新增影响（抽查各段代表）。"""
    assert result_state("fail", "product_bug") == "挂"
    assert wave_gate_decision([{"result_state": "过"}])["proceed"] is True
    assert wave_gate_decision([{"result_state": "挂"}])["proceed"] is False
    batches = admission_check([{"case_id": "c1", "resource": "device:1"}, {"case_id": "c2", "resource": "device:1"}])
    assert len(batches) == 2  # 同锁拆两批
    import testing_platform.generation.waves as waves_mod

    assert "release_gate" in waves_mod.__all__  # 已挂进对外 API
