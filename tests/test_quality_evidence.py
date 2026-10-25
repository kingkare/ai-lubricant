"""证据分级校验器 + ai_judge 四态裁决（24 号文 Skill6 落地件）。

规则来源：skill-library-research.md R9（ADR-105 / evidence-classification）。
本文件全部纯函数，零 DB、零 LLM。
"""
from __future__ import annotations

from testing_platform.ai_channel import _parse_judge, _normalize_verdict
from testing_platform.quality.evidence import (
    ADVERSARIAL_CLASSES,
    EVIDENCE_CLAIMED,
    EVIDENCE_EXECUTED,
    EVIDENCE_INFERRED,
    EVIDENCE_STATIC,
    GATING_CLASSES,
    enforce_three_part_reasoning,
    gates,
    normalize_evidence_class,
    validate_evidence_claim,
)

# ── 等级归一与门禁语义 ──────────────────────────────────────────

def test_class_aliases_normalise():
    assert normalize_evidence_class("EXECUTED") == EVIDENCE_EXECUTED
    assert normalize_evidence_class("direct") == EVIDENCE_STATIC      # evidence-classification 词汇
    assert normalize_evidence_class("conjecture") == EVIDENCE_CLAIMED  # ADR-105 第四级折入 claimed
    assert normalize_evidence_class("to_confirm") == EVIDENCE_CLAIMED
    assert normalize_evidence_class("瞎写") is None


def test_only_executed_static_gate():
    assert gates(EVIDENCE_EXECUTED) and gates(EVIDENCE_STATIC)
    assert not gates(EVIDENCE_INFERRED) and not gates(EVIDENCE_CLAIMED)
    assert not gates(None)
    assert EVIDENCE_INFERRED in ADVERSARIAL_CLASSES
    assert set(GATING_CLASSES) == {"executed", "static"}


# ── 文本级禁用模式 ─────────────────────────────────────────────

def test_bare_confidence_percentage_rejected():
    r = validate_evidence_claim("这个缺陷 85% confident 是并发导致的", evidence_class="inferred")
    assert not r.valid
    assert any(i.rule == "EV1_no_bare_confidence_pct" for i in r.errors)
    # 中文形态也拦
    r2 = validate_evidence_claim("置信度有90%，主流程会崩", evidence_class="inferred")
    assert any(i.rule == "EV1_no_bare_confidence_pct" for i in r2.errors)


def test_vague_blast_radius_rejected():
    r = validate_evidence_claim("改这个函数影响很多用例", evidence_class="inferred")
    assert any(i.rule == "EV2_no_vague_blast_radius" for i in r.errors)
    r2 = validate_evidence_claim("this affects many flows", evidence_class="inferred")
    assert any(i.rule == "EV2_no_vague_blast_radius" for i in r2.errors)
    # 具体数放行
    ok = validate_evidence_claim("影响 19 个用例", evidence_class="inferred")
    assert not any(i.rule == "EV2_no_vague_blast_radius" for i in ok.issues)


def test_claimed_no_speculation():
    r = validate_evidence_claim("超时可能是网络问题或者数据库问题", evidence_class="claimed")
    assert any(i.rule == "EV3_claimed_no_speculation" for i in r.errors)
    # claimed 的正确姿势：陈述观察 + 验证路径，不猜因
    ok = validate_evidence_claim(
        "观察到登录接口 P99=12s；requires verification via curl -w 计时采样",
        evidence_class="claimed",
    )
    assert ok.valid


def test_direct_class_requires_source():
    r = validate_evidence_claim("登录必挂", evidence_class="executed")
    assert not r.valid
    assert any(i.rule == "EV4_direct_needs_source" for i in r.errors)
    ok = validate_evidence_claim("跑 pytest 输出：3 passed（tests/test_login.py:12-20）", evidence_class="executed")
    assert ok.valid


def test_line_range_too_wide_warns():
    r = validate_evidence_claim("详见 service.py:1-500 整段", evidence_class="static")
    assert r.valid  # warn 不阻断
    assert any(i.rule == "EV5_reference_too_wide" for i in r.warnings)


def test_class_missing_or_unknown_is_error():
    r = validate_evidence_claim("随便一句", evidence_class=None)
    assert any(i.rule == "EV0_class_required" for i in r.errors)


# ── 推理三段式 ─────────────────────────────────────────────────

def test_three_part_reasoning_shape():
    full = enforce_three_part_reasoning(
        {"observation": "库存扣减无行锁", "implication": "并发会超卖", "risk": "未覆盖双线程同时下单"}
    )
    assert full.valid
    partial = enforce_three_part_reasoning({"observation": "无锁"})
    assert not any(i.severity == "error" for i in partial.issues)  # 缺段=warn
    assert len(partial.warnings) == 2


# ── ai_judge 四态（防假红：工具故障≠产品失败）─────────────────

def test_parse_judge_new_format():
    v = _parse_judge('{"verdict": "inconclusive", "observation": "证据不足", "implication": "", "risk": "缺响应体", "evidence_class": "inferred"}')
    assert v["verdict"] == "inconclusive"
    assert v["passed"] is False          # inconclusive 不假装通过
    assert v["evidence_class"] == "inferred"


def test_parse_judge_legacy_two_state_still_works():
    assert _parse_judge('{"passed": true, "reason": "ok"}')["verdict"] == "passed"
    assert _parse_judge('{"passed": false, "reason": "no"}')["verdict"] == "failed"


def test_parse_judge_unparseable_is_inconclusive_not_failed():
    """旧版把解析失败兜成 passed=False=产品失败=假红；新版落 inconclusive。"""
    v = _parse_judge("抱歉我需要更多信息才能判断")
    assert v["verdict"] == "inconclusive"
    assert v["passed"] is False
    assert v["risk"]  # 说明为什么裁不了


def test_parse_judge_blocked_from_keywords():
    assert _parse_judge('{"verdict": "blocked", "observation": "环境未启动"}')["verdict"] == "blocked"


def test_normalize_verdict_unknown_conservative():
    assert _normalize_verdict("weird-value") == "inconclusive"
    assert _normalize_verdict("env") == "blocked"
    assert _normalize_verdict("") == "inconclusive"
