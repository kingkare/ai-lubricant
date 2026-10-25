"""倍率过滤规则（kind="multiplier"）：按积分倍率做数值比较，命中即保留。

契约要点：
1. 条目形态 {kind:"multiplier", op, value}；op ∈ {>, >=, <, <=, ==}，value 有限数字。
2. 命中即保留（与「只搜索」同款准入语义），不参与改名。
3. 倍率值 None（上游没声明、库里没手填）不命中——严格过滤，不臆造 1.0。
4. 多条倍率规则 OR：任一条满足即保留。
5. 维度组合：名称维度（改名/只搜索）与倍率维度是 AND——各自「配了才要求命中」，
   没配的维度放行。纯倍率规则能独立工作。
6. 坏 op/value 在 normalize 静默丢弃（运行时容错），保存侧 compile 显式报错。
7. 改名链与只搜索链都跳过倍率条目，不参与名称匹配。
"""
import pytest

from channel import (
    Channel,
    apply_model_id_rewrite_rules,
    compile_model_id_rewrite_rules,
    model_id_matches_search_rules,
    model_matches_multiplier_rules,
    normalize_model_id_rewrite_rules,
)


def _mult(op: str, value: float, **kw) -> dict:
    return {"kind": "multiplier", "op": op, "value": value, **kw}


# ── normalize ─────────────────────────────────────────────────────────────
def test_normalize_keeps_valid_multiplier_entry():
    entries = normalize_model_id_rewrite_rules([_mult(">=", 1.5, name="贵的不留")])
    assert entries == [{"kind": "multiplier", "name": "贵的不留", "enabled": True, "op": ">=", "value": 1.5}]


@pytest.mark.parametrize("entry", [
    {"kind": "multiplier", "op": "!=", "value": 1.0},      # op 不支持
    {"kind": "multiplier", "op": ">=", "value": "1.5"},    # value 字符串（保存侧校验拦，运行时丢）
    {"kind": "multiplier", "op": ">=", "value": True},     # bool 不是数字
    {"kind": "multiplier", "op": ">=", "value": None},     # 缺 value
    {"kind": "multiplier", "value": 1.0},                  # 缺 op
    {"kind": "multiplier"},                                 # 全缺
])
def test_normalize_drops_bad_multiplier_entries(entry):
    assert normalize_model_id_rewrite_rules([entry]) == []


def test_normalize_multiplier_disabled():
    entries = normalize_model_id_rewrite_rules([_mult("<", 2.0, enabled=False)])
    assert entries == [{"kind": "multiplier", "name": "", "enabled": False, "op": "<", "value": 2.0}]


# ── 命中判定 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("op,value,mult,expected", [
    (">=", 1.5, 1.5, True),
    (">=", 1.5, 1.4, False),
    (">", 1.5, 1.6, True),
    (">", 1.5, 1.5, False),
    ("<=", 0.5, 0.5, True),
    ("<=", 0.5, 0.6, False),
    ("<", 0.5, 0.4, True),
    ("<", 0.5, 0.5, False),
    ("==", 0.7, 0.7, True),
    ("==", 0.7, 0.71, False),
])
def test_multiplier_hit_by_op(op, value, mult, expected):
    assert model_matches_multiplier_rules(mult, [_mult(op, value)]) is expected


def test_multiplier_none_never_hits():
    """上游没声明、库里没手填 = None：不命中任何倍率规则（不臆造 1.0）。"""
    assert model_matches_multiplier_rules(None, [_mult(">=", 0)]) is False
    assert model_matches_multiplier_rules(None, [_mult("<=", 999)]) is False


def test_multiplier_multiple_rules_or():
    rules = [_mult(">=", 2.0), _mult("<", 0.1)]
    assert model_matches_multiplier_rules(2.5, rules) is True   # 命中第一条
    assert model_matches_multiplier_rules(0.05, rules) is True  # 命中第二条
    assert model_matches_multiplier_rules(1.0, rules) is False  # 两条都不满足


def test_multiplier_disabled_rule_ignored():
    assert model_matches_multiplier_rules(5.0, [_mult("<", 1.0, enabled=False)]) is False


def test_multiplier_no_rules_no_hit():
    assert model_matches_multiplier_rules(1.0, []) is False


def test_multiplier_rules_do_not_rename_or_search():
    """倍率条目不参与改名与只搜索链——名称判定完全无视它。"""
    rules = [_mult(">=", 1.0)]
    assert apply_model_id_rewrite_rules("owner/glm-5.2", rules) == "glm-5.2"
    assert model_id_matches_search_rules("glm-5.2", rules) is False


def test_mixed_rules_rename_still_works():
    """倍率规则与改名规则混排：改名照常、倍率不干扰。"""
    rules = [
        {"pattern": r"-latest$", "replacement": ""},
        _mult("<=", 1.0),
    ]
    assert apply_model_id_rewrite_rules("glm-5.2-latest", rules) == "glm-5.2"


# ── 保存侧校验 ────────────────────────────────────────────────────────────
def test_compile_rejects_bad_op():
    entries, errors = compile_model_id_rewrite_rules([_mult("!=", 1.0, name="坏规则")])
    assert any("倍率比较符不合法" in e for e in errors)


def test_compile_rejects_non_numeric_value():
    entries, errors = compile_model_id_rewrite_rules([{"kind": "multiplier", "op": ">=", "value": "abc", "name": "坏规则"}])
    assert any("倍率阈值必须是数字" in e for e in errors)


def test_compile_accepts_valid_multiplier():
    entries, errors = compile_model_id_rewrite_rules([_mult(">=", 0.5, name="便宜的留")])
    assert errors == []
    assert entries[0]["kind"] == "multiplier"


# ── Channel 维度组合 ──────────────────────────────────────────────────────
def test_channel_has_dimensions():
    ch = Channel("demo", {"model_id_rewrite_rules": [_mult(">=", 1.0)]})
    assert ch.has_multiplier_rules() is True
    assert ch.has_name_filter_rules() is False
    assert ch.has_model_id_rewrite_rules() is True  # 倍率规则也算「配了规则」


def test_channel_name_dimensions_only():
    ch = Channel("demo", {"model_id_rewrite_rules": [{"pattern": "^x", "replacement": "y"}]})
    assert ch.has_multiplier_rules() is False
    assert ch.has_name_filter_rules() is True


def test_channel_disabled_multiplier_not_counted():
    ch = Channel("demo", {"model_id_rewrite_rules": [_mult(">=", 1.0, enabled=False)]})
    assert ch.has_multiplier_rules() is False
    assert ch.has_model_id_rewrite_rules() is False


def test_channel_matches_multiplier_rules():
    ch = Channel("demo", {"model_id_rewrite_rules": [_mult("<=", 0.5)]})
    assert ch.matches_multiplier_rules(0.3) is True
    assert ch.matches_multiplier_rules(0.8) is False
    assert ch.matches_multiplier_rules(None) is False


# ── 同步准入：_is_regex_hit 的维度组合 ───────────────────────────────────
def _make_pool(rules):
    class _Pool:
        channel = Channel("prov", {"provider_name": "prov", "model_id_rewrite_rules": rules})
    return _Pool()


def test_is_regex_hit_multiplier_only(monkeypatch):
    """只配倍率规则：倍率满足即保留；没倍率值不命中；不满足的不保留。"""
    from rate_limiter import ModelClientPool

    monkeypatch.setattr(
        ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: _make_pool([_mult("<=", 0.5)]))
    )
    assert ModelClientPool._is_regex_hit("prov", "cheap", 0.3) is True
    assert ModelClientPool._is_regex_hit("prov", "expensive", 2.0) is False
    # 上游没声明倍率 → 不命中（严格过滤）
    assert ModelClientPool._is_regex_hit("prov", "unknown", None) is False


def test_is_regex_hit_name_only_unaffected_by_multiplier_arg(monkeypatch):
    """只配名称规则：倍率维度放行，判定完全看名称（与倍率无关）。"""
    from rate_limiter import ModelClientPool

    monkeypatch.setattr(
        ModelClientPool,
        "get_provider_pool",
        classmethod(lambda cls, name: _make_pool([{"pattern": "^glm", "replacement": "X", "search_only": True}])),
    )
    assert ModelClientPool._is_regex_hit("prov", "glm-5.2", None) is True
    assert ModelClientPool._is_regex_hit("prov", "gpt-4o", 0.1) is False


def test_is_regex_hit_both_dimensions_are_and(monkeypatch):
    """名称 + 倍率都配：两个维度都放行才命中。"""
    from rate_limiter import ModelClientPool

    monkeypatch.setattr(
        ModelClientPool,
        "get_provider_pool",
        classmethod(lambda cls, name: _make_pool([
            {"pattern": "^glm", "replacement": "X", "search_only": True},
            _mult("<=", 0.5),
        ])),
    )
    assert ModelClientPool._is_regex_hit("prov", "glm-5.2", 0.3) is True   # 名称命中 + 倍率满足
    assert ModelClientPool._is_regex_hit("prov", "glm-5.2", 2.0) is False  # 名称命中但倍率不满足
    assert ModelClientPool._is_regex_hit("prov", "gpt-4o", 0.3) is False   # 倍率满足但名称不命中


def test_is_regex_hit_rename_dimension_counts(monkeypatch):
    """普通改名规则改了名字也算名称维度命中（不配倍率时放行倍率维度）。"""
    from rate_limiter import ModelClientPool

    monkeypatch.setattr(
        ModelClientPool,
        "get_provider_pool",
        classmethod(lambda cls, name: _make_pool([{"pattern": "-latest$", "replacement": ""}])),
    )
    assert ModelClientPool._is_regex_hit("prov", "glm-5.2-latest", None) is True
    assert ModelClientPool._is_regex_hit("prov", "glm-5.2", None) is False
