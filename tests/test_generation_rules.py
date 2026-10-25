"""用户规则机制（25 §7 + 2.6 mc_test_rule）测试。

纯函数测试——不建库、不联网、不依赖 PG、不 import tortoise。
覆盖：作用域筛选 / 渲染块指令与编号 / shadow 留痕 / 计数聚合 / 豁免过期。

运行：``python -m pytest tests/test_generation_rules.py -q``
"""

from __future__ import annotations

import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from testing_platform.generation.rules import (  # noqa: E402
    DECLARATION_INSTRUCTION,
    DEFAULT_ENFORCEMENT,
    ENFORCEMENTS,
    RULE_KINDS,
    SCOPES,
    bump_counters,
    is_waived,
    new_rule,
    render_rules_block,
    rules_for,
    shadow_log_entry,
    waive_entry,
)


# ── 常量 ─────────────────────────────────────────────────────


def test_enums_match_table_schema():
    assert RULE_KINDS == ("hard", "soft")
    assert SCOPES == ("global", "module", "requirement", "surface")
    assert ENFORCEMENTS == ("active", "shadow", "disabled")
    assert DEFAULT_ENFORCEMENT == "shadow"


# ── rules_for：作用域筛选 ─────────────────────────────────────


def _rule(scope, *, kind="hard", enforcement="active", target_id=None,
          target_type=None, rid="r"):
    return {
        "id": rid,
        "content": f"{rid}-content",
        "kind": kind,
        "scope": scope,
        "target_type": target_type,
        "target_id": target_id,
        "enforcement": enforcement,
        "hits": 0,
        "violations": 0,
    }


def test_global_always_matches_regardless_of_context():
    g = _rule("global", rid="g1")
    # 不传任何上下文，global 仍然命中
    assert rules_for([g]) == [g]
    assert rules_for([g], module="pay", requirement_id="req-9", surface="api") == [g]


def test_module_scope_matches_by_target_id():
    r_pay = _rule("module", target_id="pay", rid="m1")
    r_order = _rule("module", target_id="order", rid="m2")
    selected = rules_for([r_pay, r_order], module="pay")
    assert [r["id"] for r in selected] == ["m1"]


def test_module_scope_without_target_id_is_whole_scope():
    # 只挂 module 层、不点名实体 → 该层全域生效
    r_any = _rule("module", rid="m3")  # target_id=None
    assert rules_for([r_any], module="pay") == [r_any]
    assert rules_for([r_any], module="order") == [r_any]
    # 没传 module → 不命中
    assert rules_for([r_any]) == []


def test_requirement_scope_matches_by_target_id():
    r = _rule("requirement", target_id="req-9", rid="r1")
    assert rules_for([r], requirement_id="req-9") == [r]
    assert rules_for([r], requirement_id="req-1") == []
    assert rules_for([r]) == []


def test_surface_scope_matches_by_target_id():
    r = _rule("surface", target_id="api", rid="s1")
    assert rules_for([r], surface="api") == [r]
    assert rules_for([r], surface="web") == []


def test_multi_scope_union():
    g = _rule("global", rid="g")
    m = _rule("module", target_id="pay", rid="m")
    req = _rule("requirement", target_id="req-9", rid="req")
    surf = _rule("surface", target_id="api", rid="surf")
    selected = rules_for(
        [g, m, req, surf], module="pay", requirement_id="req-9", surface="api"
    )
    assert {r["id"] for r in selected} == {"g", "m", "req", "surf"}


def test_disabled_excluded():
    active = _rule("global", enforcement="active", rid="a")
    shadow = _rule("global", enforcement="shadow", rid="s")
    disabled = _rule("global", enforcement="disabled", rid="d")
    selected = rules_for([active, shadow, disabled])
    assert {r["id"] for r in selected} == {"a", "s"}


def test_shadow_kept_and_active_kept():
    active = _rule("global", enforcement="active", rid="a")
    shadow = _rule("global", enforcement="shadow", rid="s")
    selected = rules_for([active, shadow])
    assert {r["id"] for r in selected} == {"a", "s"}
    assert all(r["enforcement"] in ("active", "shadow") for r in selected)


def test_target_type_mismatch_rejected():
    # target_type 填了但不等于 scope → 防御行写歪，不命中
    bad = _rule("module", target_type="requirement", target_id="pay", rid="bad")
    assert rules_for([bad], module="pay") == []


def test_input_not_mutated_and_copies_returned():
    g = _rule("global", rid="g")
    snapshot = dict(g)
    out = rules_for([g])
    assert out == [snapshot]
    # 改返回值不影响原对象
    out[0]["hits"] = 999
    assert g["hits"] == 0
    assert g == snapshot


# ── render_rules_block ───────────────────────────────────────


def test_render_block_contains_instruction_and_numbering():
    rules = [
        _rule("global", kind="hard", rid="H1", target_id=None),
        _rule("global", kind="soft", rid="S1", target_id=None),
    ]
    block = render_rules_block(rules)
    assert DECLARATION_INSTRUCTION in block
    assert "Not declaring = violation" in block
    # 规则 content 原文出现并编号
    assert "1. [H1] H1-content" in block
    assert "1. [S1] S1-content" in block
    # hard / soft 分节
    assert "Hard rules" in block
    assert "Soft rules" in block


def test_render_block_separates_hard_and_soft_sections():
    rules = [
        _rule("global", kind="soft", rid="S1"),
        _rule("global", kind="hard", rid="H1"),
        _rule("global", kind="hard", rid="H2"),
    ]
    block = render_rules_block(rules)
    hard_idx = block.index("Hard rules")
    soft_idx = block.index("Soft rules")
    assert hard_idx < soft_idx
    # hard 节里两个硬规则编号 1/2
    assert "1. [H1]" in block and "2. [H2]" in block


def test_render_block_empty_rules():
    block = render_rules_block([])
    assert DECLARATION_INSTRUCTION in block
    assert "No project rules" in block


# ── shadow_log_entry ──────────────────────────────────────────


def test_shadow_log_entry_structure_hard_violated():
    rule = _rule("module", kind="hard", target_id="pay", rid="r7")
    case = {"case_ref": "case-42", "other": 1}
    entry = shadow_log_entry(rule, case, violated=True)
    assert entry["rule_id"] == "r7"
    assert entry["mode"] == "shadow"
    assert entry["violated"] is True
    assert entry["would_block"] is True  # hard + violated
    assert entry["case_ref"] == "case-42"
    assert "ts" in entry and isinstance(entry["ts"], str)


def test_shadow_log_entry_soft_never_blocks():
    rule = _rule("module", kind="soft", target_id="pay", rid="s")
    entry = shadow_log_entry(rule, {"id": "c"}, violated=True)
    assert entry["would_block"] is False  # soft 永远不拦
    assert entry["violated"] is True


def test_shadow_log_entry_not_violated():
    rule = _rule("global", kind="hard", rid="h")
    entry = shadow_log_entry(rule, {"case_ref": "c"}, violated=False)
    assert entry["violated"] is False
    assert entry["would_block"] is False


def test_shadow_log_entry_case_ref_fallback():
    rule = _rule("global", rid="h")
    entry = shadow_log_entry(rule, {"proposal_id": "p1"}, violated=False)
    assert entry["case_ref"] == "p1"


# ── bump_counters ─────────────────────────────────────────────


def test_bump_counters_aggregates_hits_and_violations():
    rules = [_rule("global", rid="r1"), _rule("global", rid="r2")]
    executions = [
        {"rule_id": "r1", "status": "pass"},
        {"rule_id": "r1", "status": "violated"},
        {"rule_id": "r2", "status": "pass"},
        {"rule_id": "r2", "status": "pass"},
        {"rule_id": "r2", "status": "violated"},
    ]
    deltas = dict((rid, (h, v)) for rid, h, v in bump_counters(rules, executions))
    assert deltas["r1"] == (2, 1)
    assert deltas["r2"] == (3, 1)


def test_bump_counters_skipped_not_counted_as_hit():
    rules = [_rule("global", rid="r1")]
    executions = [
        {"rule_id": "r1", "status": "skipped"},
        {"rule_id": "r1", "status": "pass"},
    ]
    deltas = bump_counters(rules, executions)
    assert deltas == [("r1", 1, 0)]  # skipped 不计命中


def test_bump_counters_unknown_rule_ignored():
    rules = [_rule("global", rid="r1")]
    executions = [
        {"rule_id": "r1", "status": "pass"},
        {"rule_id": "ghost", "status": "violated"},  # 不在 rules 里
    ]
    assert bump_counters(rules, executions) == [("r1", 1, 0)]


def test_bump_counters_empty_executions():
    rules = [_rule("global", rid="r1")]
    assert bump_counters(rules, []) == []


def test_bump_counters_deterministic_order():
    rules = [_rule("global", rid="r3"), _rule("global", rid="r1"), _rule("global", rid="r2")]
    executions = [
        {"rule_id": "r3", "status": "pass"},
        {"rule_id": "r1", "status": "pass"},
        {"rule_id": "r2", "status": "pass"},
    ]
    deltas = bump_counters(rules, executions)
    assert [rid for rid, _, _ in deltas] == ["r1", "r2", "r3"]


# ── new_rule ──────────────────────────────────────────────────


def test_new_rule_defaults_to_shadow():
    r = new_rule("不许硬编码", "hard", "module", target_id="pay")
    assert r["enforcement"] == "shadow"
    assert r["kind"] == "hard"
    assert r["scope"] == "module"
    assert r["target_type"] == "module"  # 缺省随 scope
    assert r["target_id"] == "pay"
    assert r["hits"] == 0 and r["violations"] == 0
    assert "created_at" in r


def test_new_rule_global_has_no_target():
    r = new_rule("全项目禁真实手机号", "hard", "global")
    assert r["scope"] == "global"
    assert r["enforcement"] == "shadow"


def test_new_rule_rejects_bad_kind_and_scope():
    with pytest.raises(ValueError):
        new_rule("x", "fatal", "global")
    with pytest.raises(ValueError):
        new_rule("x", "hard", "everywhere")


# ── waive_entry / is_waived ───────────────────────────────────


def test_waive_entry_structure():
    w = waive_entry("r1", "case-1", "alice", "已知技术债，下迭代修", "2099-01-01T00:00:00+00:00")
    assert w["rule_id"] == "r1"
    assert w["case_ref"] == "case-1"
    assert w["waived_by"] == "alice"
    assert w["status"] == "active"
    assert w["expires"] == "2099-01-01T00:00:00+00:00"


def test_is_waived_matches_unexpired():
    waivers = [waive_entry("r1", "case-1", "a", "debt", "2099-01-01T00:00:00+00:00")]
    assert is_waived(waivers, "r1", "case-1", now="2026-10-18T00:00:00+00:00") is True


def test_is_waived_expired_not_waived():
    waivers = [waive_entry("r1", "case-1", "a", "debt", "2025-01-01T00:00:00+00:00")]
    assert is_waived(waivers, "r1", "case-1", now="2026-10-18T00:00:00+00:00") is False


def test_is_waived_different_case_not_waived():
    waivers = [waive_entry("r1", "case-1", "a", "debt", "2099-01-01T00:00:00+00:00")]
    assert is_waived(waivers, "r1", "case-2", now="2026-10-18T00:00:00+00:00") is False
    assert is_waived(waivers, "r2", "case-1", now="2026-10-18T00:00:00+00:00") is False


def test_is_waived_empty_expires_is_permanent():
    waivers = [waive_entry("r1", "case-1", "a", "debt", None)]
    assert is_waived(waivers, "r1", "case-1", now="2099-01-01T00:00:00+00:00") is True


def test_is_waived_revoked_status_not_active():
    w = waive_entry("r1", "case-1", "a", "debt", "2099-01-01T00:00:00+00:00")
    w["status"] = "revoked"
    assert is_waived([w], "r1", "case-1", now="2026-10-18T00:00:00+00:00") is False


def test_is_waived_now_defaults_to_current_time():
    # 不传 now，未来豁免生效
    waivers = [waive_entry("r1", "case-1", "a", "debt", "2099-01-01T00:00:00+00:00")]
    assert is_waived(waivers, "r1", "case-1") is True
    # 已过期豁免不生效
    expired = [waive_entry("r1", "case-1", "a", "debt", "2000-01-01T00:00:00+00:00")]
    assert is_waived(expired, "r1", "case-1") is False


def test_is_waived_z_suffix_iso_handled():
    # ISO 末尾带 Z 也要能解析
    waivers = [waive_entry("r1", "case-1", "a", "debt", "2099-01-01T00:00:00Z")]
    assert is_waived(waivers, "r1", "case-1", now="2026-10-18T00:00:00+00:00") is True
