"""方法库四生成器 + 规则注入（25 号文环节5 步骤2-3 / §7）测试。

纯函数测试——不建库、不联网、不依赖 PG、不调 AI。
覆盖：端配方 / pairwise 全对覆盖 / boundary 各类型（含分页·限流两轴）/
negative 四类别 / state-machine 全状态·全迁移·非法迁移 / 规则注入。

运行：``python -m pytest tests/test_generation_method_lib.py -q``
"""
from __future__ import annotations

import os
import sys
from itertools import combinations

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.design.transform_table import (  # noqa: E402
    INJECTION_SAMPLES,
    OVERLONG_SAMPLE,
    param_variants,
)
from testing_platform.generation.method_lib import (  # noqa: E402
    NEGATIVE_CATEGORIES,
    STRATEGY_LABELS,
    SURFACE_ORDER,
    SURFACE_RECIPES,
    boundary_values,
    inject_rules_block,
    negative_payloads,
    pairwise_combinations,
    recipe_for_surface,
    state_machine_paths,
)
from testing_platform.generation.rules import DECLARATION_INSTRUCTION  # noqa: E402

# ══════════════════════════════════════════════════════════════════
# 端配方（环节5 步骤2：六策略 + 断言层 + 必出轴 + 数据五作用域）
# ══════════════════════════════════════════════════════════════════

EXPECTED_ASSERTION_LAYERS = {
    "api": ["status_code", "biz_code", "key_fields", "response_time"],
    "web": ["element_visible", "element_text", "url"],
    "app": ["foreground_state", "persisted_state", "crash_log"],
    "security": ["reject_code", "audit_trail", "data_invisible"],
}

EXPECTED_MANDATORY_AXES = {
    "api": ["C01", "C02", "C03", "C04", "C11", "C12"],
    "security": ["C04", "C14"],
}


def test_surface_recipes_covers_four_surfaces_only():
    assert set(SURFACE_RECIPES) == {"api", "web", "app", "security"}
    assert SURFACE_ORDER == ("api", "web", "app", "security")


@pytest.mark.parametrize("surface", SURFACE_ORDER)
def test_recipe_assertion_layers_per_surface(surface):
    """断言层逐端正确（25 号文环节5 步骤2 原文四类）。"""
    assert recipe_for_surface(surface)["assertion_layers"] == EXPECTED_ASSERTION_LAYERS[surface]


@pytest.mark.parametrize("surface", SURFACE_ORDER)
def test_recipe_mandatory_axes_per_surface(surface):
    """必出轴：api 六轴 / security 权限+安全；web·app 只出基础四轴。"""
    axes = recipe_for_surface(surface)["mandatory_axes"]
    if surface in EXPECTED_MANDATORY_AXES:
        assert axes == EXPECTED_MANDATORY_AXES[surface]
    else:
        assert axes == ["C01", "C02", "C03", "C12"]
    assert all(a.startswith("C") and a[1:].isdigit() for a in axes)


@pytest.mark.parametrize("surface", SURFACE_ORDER)
def test_recipe_strategies_are_subset_of_six(surface):
    """六策略子集（且 id 都能在 STRATEGY_LABELS 里取到中文原文）。"""
    strategies = recipe_for_surface(surface)["strategies"]
    assert set(strategies) <= set(STRATEGY_LABELS)
    assert strategies == list(dict.fromkeys(strategies))  # 无重复
    assert len(strategies) <= 6


def test_recipe_labels_match_design_doc_六策略原文():
    """策略中文名逐字对齐环节5 步骤2 原文。"""
    assert [STRATEGY_LABELS[k] for k in
            ("positive", "negative", "boundary", "equivalence", "scenario", "state_transition")] == \
        ["正向", "反向", "边界值", "等价类", "场景法", "状态与流程"]


@pytest.mark.parametrize("surface", SURFACE_ORDER)
def test_recipe_data_scopes_hint_is_the_five_scopes(surface):
    """数据五作用域提示（§2.7：环境/鉴权/局部/数据集/全局）。"""
    assert recipe_for_surface(surface)["data_scopes_hint"] == "env/auth/local/data/global"


def test_api_recipe_declares_skill15_mandatory_scenarios():
    """api 端 Token 失效与越权必覆盖（环节5 步骤2；词表与 gates 同源）。"""
    assert recipe_for_surface("api")["mandatory_scenarios"] == ["token_invalid", "cross_tenant"]


def test_security_recipe_has_no_positive_strategy():
    """安全端配方不做正向策略（其语义是"拒绝/审计/不可见"）。"""
    assert "positive" not in recipe_for_surface("security")["strategies"]


def test_recipe_for_surface_tolerates_case_and_aliases():
    assert recipe_for_surface(" API ")["label"] == "接口端"
    assert recipe_for_surface("mobile")["assertion_layers"] == EXPECTED_ASSERTION_LAYERS["app"]
    assert recipe_for_surface("h5")["assertion_layers"] == EXPECTED_ASSERTION_LAYERS["web"]


def test_recipe_for_surface_unknown_raises_no_silent_fallback():
    with pytest.raises(KeyError):
        recipe_for_surface("desktop")
    with pytest.raises(KeyError):
        recipe_for_surface("")


def test_recipe_returns_copy_mutating_it_does_not_poison_constant():
    r = recipe_for_surface("api")
    r["mandatory_axes"].append("C99")
    r["strategies"] = ["nope"]
    assert "C99" not in SURFACE_RECIPES["api"]["mandatory_axes"]
    assert SURFACE_RECIPES["api"]["strategies"] != ["nope"]


# ══════════════════════════════════════════════════════════════════
# 生成器一：pairwise（全对覆盖）
# ══════════════════════════════════════════════════════════════════


def _assert_all_pairs_covered(params, combos):
    """每条参数值对 ``(名i,值i)×(名j,值j)`` 至少在一条组合里同时出现。"""
    domains = {p["name"]: p["values"] for p in params}
    for a, b in combinations(domains, 2):
        for va in domains[a]:
            for vb in domains[b]:
                hit = [c for c in combos
                       if c["values"].get(a) == va and c["values"].get(b) == vb]
                assert hit, f"未覆盖参数对：{a}={va} × {b}={vb}"


def test_pairwise_three_params_two_three_values_within_budget():
    """3 参数 2-3 值 → 组合数 ≤12 且每对覆盖（任务口径）。"""
    params = [
        {"name": "browser", "values": ["chrome", "firefox", "safari"]},
        {"name": "os", "values": ["win", "mac"]},
        {"name": "net", "values": ["4g", "wifi"]},
    ]
    combos = pairwise_combinations(params)
    assert 0 < len(combos) <= 12
    _assert_all_pairs_covered(params, combos)
    # 不爆全组合（3×2×2=12）
    assert len(combos) <= 12


def test_pairwise_three_params_three_values_each():
    params = [{"name": n, "values": [1, 2, 3]} for n in ("a", "b", "c")]
    combos = pairwise_combinations(params)
    assert len(combos) <= 12
    _assert_all_pairs_covered(params, combos)


def test_pairwise_does_not_explode_with_many_params():
    """6 参数 × 4 值：全组合 4096，pairwise 应在数十条量级。"""
    params = [{"name": f"p{i}", "values": [1, 2, 3, 4]} for i in range(6)]
    combos = pairwise_combinations(params)
    assert len(combos) < 4 ** 6 / 4
    _assert_all_pairs_covered(params, combos)


def test_pairwise_every_value_appears_at_least_once():
    params = [
        {"name": "a", "values": [1, 2, 3, 4, 5]},
        {"name": "b", "values": ["x", "y"]},
        {"name": "c", "values": [True, False]},
    ]
    combos = pairwise_combinations(params)
    for p in params:
        seen = {repr(c["values"].get(p["name"])) for c in combos}
        assert {repr(v) for v in p["values"]} <= seen, f"{p['name']} 有值没被探到"


def test_pairwise_combos_are_full_assignments():
    """每条组合都是完整参数集（可直接执行，不留空洞）。"""
    params = [
        {"name": "a", "values": [1, 2, 3]},
        {"name": "b", "values": ["x", "y"]},
        {"name": "c", "values": [1, 2, 3, 4]},
    ]
    names = {p["name"] for p in params}
    for c in pairwise_combinations(params):
        assert set(c["values"]) == names


def test_pairwise_deterministic_and_deduped():
    params = [
        {"name": "a", "values": [1, 2]},
        {"name": "b", "values": ["x", "y"]},
        {"name": "c", "values": [True, False, None]},
    ]
    first = pairwise_combinations(params)
    second = pairwise_combinations(params)
    assert first == second                      # 可重跑（slot 幂等的根基）
    keys = [tuple(sorted(c["values"].items(), key=lambda kv: repr(kv[0]))) for c in first]
    assert len(keys) == len(set(keys))          # 无两条一样的组合


def test_pairwise_pairs_field_lists_value_pairs():
    params = [
        {"name": "a", "values": [1, 2]},
        {"name": "b", "values": ["x", "y"]},
    ]
    combos = pairwise_combinations(params)
    for c in combos:
        assert len(c["pairs"]) == 1             # 2 参数 → 1 对
        (pair,) = c["pairs"]
        [[na, va], [nb, vb]] = pair
        assert {na, nb} == {"a", "b"}
        assert c["values"][na] == va and c["values"][nb] == vb


def test_pairwise_reads_enum_values_and_single_value_params():
    """值来源兼容 enum_values / 单值 example（单值只作固定值参与）。"""
    params = [
        {"name": "status", "enum_values": ["new", "paid"]},
        {"name": "channel", "values": ["app", "web", "api"]},
        {"name": "version", "example": "v2"},
    ]
    combos = pairwise_combinations(params)
    _assert_all_pairs_covered(
        [{"name": p["name"], "values": p.get("values") or p.get("enum_values") or [p.get("example")]}
         for p in params],
        combos,
    )
    assert all(c["values"]["version"] == "v2" for c in combos)


def test_pairwise_single_param_gives_one_combo_per_value():
    combos = pairwise_combinations([{"name": "a", "values": [1, 2, 3]}])
    assert [c["values"] for c in combos] == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_pairwise_empty_and_dirty_inputs():
    assert pairwise_combinations([]) == []
    assert pairwise_combinations(None) == []
    assert pairwise_combinations([{}, {"name": ""}, {"name": "x"}]) == []  # 无名/无值都跳
    assert pairwise_combinations(["not-a-dict"]) == []


def test_pairwise_duplicate_param_name_raises():
    with pytest.raises(ValueError):
        pairwise_combinations([
            {"name": "a", "values": [1, 2]},
            {"name": "a", "values": [3, 4]},
        ])


def test_pairwise_dedupes_repeated_values():
    combos = pairwise_combinations([
        {"name": "a", "values": [1, 1, 2]},
        {"name": "b", "values": ["x", "x"]},
    ])
    assert all(len(set(repr(v) for v in c["values"].values())) for c in combos)
    seen = sorted({repr(c["values"]["a"]) for c in combos})
    assert seen == ["1", "2"]


# ══════════════════════════════════════════════════════════════════
# 生成器二：boundary-value（边界变体）
# ══════════════════════════════════════════════════════════════════


def _variants(spec):
    return [v["variant"] for v in boundary_values(spec)]


def _by_variant(spec):
    return {v["variant"]: v for v in boundary_values(spec)}


def test_boundary_integer_has_min_and_max_bodies_plus_out_of_range():
    """integer：min-1 / min / max / max+1 / 0 / negative / non-numeric。"""
    got = set(_variants({"name": "qty", "type": "integer",
                         "min_value": 1, "max_value": 10}))
    assert {"min-1", "min", "max", "max+1", "zero", "negative", "non-numeric"} <= got
    m = _by_variant({"name": "qty", "type": "integer", "min_value": 1, "max_value": 10})
    assert m["min"]["value"] == 1 and m["max"]["value"] == 10
    assert m["min-1"]["value"] == 0 and m["max+1"]["value"] == 11
    assert m["min"]["expect_status"] == 200 and m["min-1"]["expect_status"] == 400
    assert m["min"]["axis"] == "C03"


def test_boundary_integer_without_bounds_still_probes_zero_negative_nonnumeric():
    got = set(_variants({"name": "n", "type": "integer"}))
    assert {"zero", "negative", "non-numeric", "min-1", "max+1"} <= got
    assert "min" not in got and "max" not in got   # 没给界就不编造边界本体


def test_boundary_string_variants_complete():
    """string：empty / over-max / under-min / special-chars / injection。"""
    got = set(_variants({"name": "s", "type": "string"}))
    assert {"empty", "over-max", "under-min", "special-chars", "injection"} <= got
    m = _by_variant({"name": "s", "type": "string"})
    assert m["over-max"]["value"] == OVERLONG_SAMPLE
    assert m["injection"]["axis"] == "C14"


def test_boundary_enum_each_legal_value_plus_illegal():
    got = _variants({"name": "e", "type": "string", "enum_values": ["a", "b", "c"]})
    assert [g for g in got if g.startswith("valid:")] == ["valid:a", "valid:b", "valid:c"]
    assert "invalid" in got
    m = _by_variant({"name": "e", "type": "string", "enum_values": ["a", "b"]})
    assert m["invalid"]["expect_status"] == 400


def test_boundary_datetime_variants():
    got = set(_variants({"name": "at", "type": "datetime"}))
    assert {"malformed", "future", "past"} <= got


def test_boundary_array_variants():
    got = set(_variants({"name": "ids", "type": "array"}))
    assert {"empty", "null", "over-max-items"} <= got


def test_boundary_reuses_transform_table_kernel():
    """复用不重造：未命中补轴时，基础变体与 param_variants 逐字一致。"""
    spec = {"name": "amount", "type": "number", "min_value": 0, "max_value": 99,
            "example": 10}
    base = param_variants(spec)
    got = boundary_values(spec)
    # 基础部分原样在前（本模块只在尾部增补 min/max 本体）
    assert got[: len(base)] == base
    assert [v["variant"] for v in got[len(base):]] == ["min", "max"]


def test_boundary_pagination_axis_present():
    """分页轴（R5：transform_table 没有的轴）。"""
    got = set(_variants({"name": "page", "type": "integer"}))
    assert {"page-zero", "page-negative", "page-non-numeric", "page-over-max"} <= got
    m = _by_variant({"name": "page", "type": "integer"})
    assert all(v["param"] == "page" for v in m.values())
    assert m["page-over-max"]["axis"] == "C03"
    assert m["page-zero"]["expect_status"] == 400


def test_boundary_pagination_supersedes_generic_numeric_probes():
    """命中分页轴 → 通用数值探测由专用变体接管（不同值双测/断言打架）。"""
    m = _by_variant({"name": "page", "type": "integer", "min_value": 1, "max_value": 20})
    for dropped in ("zero", "negative", "non-numeric", "min-1", "max+1"):
        assert dropped not in m
    assert "min" in m and "max" in m          # 边界本体仍要（页码范围）


def test_boundary_page_size_axis_present():
    got = set(_variants({"name": "page_size", "type": "integer"}))
    assert {"page-size-zero", "page-size-negative", "page-size-one", "page-size-over-max"} <= got
    m = _by_variant({"name": "page_size", "type": "integer"})
    assert m["page-size-one"]["value"] == 1
    assert m["page-size-over-max"]["value"] == 10000


def test_boundary_rate_limit_axis_present():
    """限流轴（R5 另一条我们没有的轴）。"""
    m = _by_variant({"name": "qps", "type": "integer", "min_value": 1, "max_value": 100})
    assert {"rate-burst", "rate-sustained"} <= set(m)
    assert m["rate-burst"]["expect_status"] == 429
    assert m["rate-burst"]["axis"] == "C05"
    assert m["rate-sustained"]["axis"] == "C05"


def test_boundary_extras_do_not_collide_with_generic_probes():
    """限流轴是叠加（不接管数值探测），但不能出现同 variant 重名。"""
    got = _variants({"name": "batch_size", "type": "integer", "max_value": 50})
    assert len(got) == len(set(got))
    assert "rate-burst" in got and "max+1" in got


def test_boundary_axes_come_from_coverage_axis_vocabulary():
    valid_axes = {f"C{i:02d}" for i in range(1, 15)}
    for spec in ({"name": "s", "type": "string"}, {"name": "page", "type": "integer"},
                 {"name": "qps", "type": "integer"}, {"name": "e", "enum": ["x"]}):
        for v in boundary_values(spec):
            assert v["axis"] in valid_axes, v


def test_boundary_entry_shape_is_transform_table_shape():
    for v in boundary_values({"name": "p", "type": "integer", "max_value": 5}):
        assert set(v) == {"variant", "axis", "expect_status", "label", "value", "param"}


def test_boundary_empty_and_dirty_spec():
    assert boundary_values({}) == []
    assert boundary_values(None) == []
    assert boundary_values("not-a-dict") == []
    assert boundary_values({"type": "integer"}) == []      # 无名参数产不出 slot


# ══════════════════════════════════════════════════════════════════
# 生成器三：negative（负向载荷库）
# ══════════════════════════════════════════════════════════════════


def test_negative_categories_are_the_four_declared():
    assert NEGATIVE_CATEGORIES == ("injection", "auth", "malformed", "concurrency")


def test_negative_injection_five_subkinds_two_each():
    """injection：SQL/XSS/path-traversal/SSRF/command-injection 各 2 条。"""
    ps = negative_payloads("injection")
    by = {}
    for p in ps:
        by.setdefault(p["category"], []).append(p)
    assert set(by) == {"sql", "xss", "path_traversal", "ssrf", "command_injection"}
    assert {k: len(v) for k, v in by.items()} == {k: 2 for k in by}
    assert all(p["axis"] == "C14" for p in ps)


def test_negative_injection_merges_transform_table_samples():
    """与 INJECTION_SAMPLES 合并：三条原样在列（不重造载荷）。"""
    payloads = {p["payload"] for p in negative_payloads("injection")}
    for sample in INJECTION_SAMPLES:
        assert sample in payloads


def test_negative_auth_covers_the_four_declared_kinds():
    ps = negative_payloads("auth")
    kinds = {p["category"] for p in ps}
    assert kinds == {"auth_bypass", "expired_token", "cross_tenant", "no_token"}
    assert all(p["axis"] == "C04" for p in ps)
    by = {p["category"]: p["expect_status"] for p in ps}
    assert by["no_token"] == 401 and by["cross_tenant"] == 403


def test_negative_malformed_covers_the_three_declared_kinds():
    ps = negative_payloads("malformed")
    assert {p["category"] for p in ps} == {"bad_json", "wrong_content_type", "oversized"}
    assert all(p["axis"] == "C12" for p in ps)
    statuses = {p["expect_status"] for p in ps}
    assert {400, 415} <= statuses


def test_negative_concurrency_covers_race_and_duplicate_submit():
    ps = negative_payloads("concurrency")
    assert {p["category"] for p in ps} == {"race", "duplicate_submit"}
    assert {p["axis"] for p in ps} == {"C05", "C11"}
    assert all(p["expect_status"] == 409 for p in ps)


def test_negative_entry_shape_and_no_mutation_between_calls():
    for cat in NEGATIVE_CATEGORIES:
        ps = negative_payloads(cat)
        assert ps
        for p in ps:
            assert set(p) == {"category", "payload", "label", "axis", "expect_status"}
            assert isinstance(p["expect_status"], int)
            assert p["payload"]
        assert ps == negative_payloads(cat)     # 返回副本，常量不被污染
        ps[0]["payload"] = "MUTATED"
        assert negative_payloads(cat)[0]["payload"] != "MUTATED"


def test_negative_category_aliases_and_unknown():
    assert negative_payloads("AUTH") == negative_payloads("auth")
    assert negative_payloads("sql-injection") == negative_payloads("injection")
    assert negative_payloads("concurrent") == negative_payloads("concurrency")
    with pytest.raises(ValueError):
        negative_payloads("performance")
    with pytest.raises(ValueError):
        negative_payloads("")


# ══════════════════════════════════════════════════════════════════
# 生成器四：state-machine（状态迁移路径）
# ══════════════════════════════════════════════════════════════════

SM_STATES = ["draft", "review", "done"]
SM_TRANSITIONS = [
    {"from": "draft", "to": "review", "event": "submit"},
    {"from": "review", "to": "done", "event": "approve", "guard": "两人通过"},
    {"from": "review", "to": "draft", "event": "reject"},
]


def _paths_of(kind, paths):
    return [p for p in paths if p["kind"] == kind]


def test_state_machine_covers_all_states():
    paths = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    covered = {p["path"][0] for p in _paths_of("all_states", paths)}
    assert covered == set(SM_STATES)


def test_state_machine_covers_all_transitions():
    paths = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    legal = {(t["from"], t["to"]) for t in SM_TRANSITIONS}
    got = {(p["from"], p["to"]) for p in _paths_of("all_transitions", paths)}
    assert got == legal
    for p in _paths_of("all_transitions", paths):
        assert p["expect"] == "reachable"
        assert p["path"] == [p["from"], p["to"]]


def test_state_machine_carries_event_and_guard():
    paths = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    approve = [p for p in _paths_of("all_transitions", paths) if p["to"] == "done"][0]
    assert approve["event"] == "approve" and approve["guard"] == "两人通过"


def test_state_machine_illegal_transitions_include_terminal_backward_move():
    """非法迁移含 ``done→draft``（任务点名的经典 bug）且 expect=unreachable。"""
    paths = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    illegal = {(p["from"], p["to"]) for p in _paths_of("illegal_transition", paths)}
    assert ("done", "draft") in illegal
    assert ("draft", "done") in illegal          # 跨级前进也是非法
    legal = {(t["from"], t["to"]) for t in SM_TRANSITIONS}
    assert not (illegal & legal)                 # 合法迁移绝不进非法集
    for p in _paths_of("illegal_transition", paths):
        assert p["expect"] == "unreachable" and p["axis"] == "C02"


def test_state_machine_guard_condition_probes():
    """带守卫的迁移额外一条"守卫不满足"路径（R5 守卫条件测试）。"""
    paths = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    guards = _paths_of("guard_blocked", paths)
    assert len(guards) == 1
    assert guards[0]["from"] == "review" and guards[0]["expect"] == "blocked"
    assert guards[0]["guard"] == "两人通过"


def test_state_machine_without_guards_emits_no_guard_probes():
    paths = state_machine_paths(["a", "b"], [{"from": "a", "to": "b"}])
    assert _paths_of("guard_blocked", paths) == []


def test_state_machine_tolerates_key_aliases_and_drops_dirty_edges():
    paths = state_machine_paths(
        ["a", "b"],
        [
            {"source": "a", "target": "b", "trigger": "go"},   # 别名键
            {"from": "a", "to": "ghost"},                      # 指向未声明状态 → 跳
            {"to": "b"},                                       # 缺 from → 跳
            "not-a-dict",
        ],
    )
    edges = {(p["from"], p["to"]) for p in _paths_of("all_transitions", paths)}
    assert edges == {("a", "b")}
    assert _paths_of("illegal_transition", paths)[0]["path"] == ["b", "a"]


def test_state_machine_empty_states_returns_nothing():
    assert state_machine_paths([], []) == []
    assert state_machine_paths(None, None) == []


def test_state_machine_self_loop_counts_as_legal_transition():
    """自环（a→a）若在迁移表里则算合法迁移；缺来自不算非法迁移。"""
    paths = state_machine_paths(["a"], [{"from": "a", "to": "a", "event": "save"}])
    assert [(p["kind"], p["from"], p["to"]) for p in paths] == [
        ("all_states", "a", "a"),
        ("all_transitions", "a", "a"),
    ]


def test_state_machine_states_without_transitions_only_get_state_probes():
    """孤立状态（无任何迁移）只产全状态覆盖探针，不产迁移/非法迁移。"""
    paths = state_machine_paths(["only"], [])
    assert [p["kind"] for p in paths] == ["all_states"]


def test_state_machine_paths_entry_shape():
    for p in state_machine_paths(SM_STATES, SM_TRANSITIONS):
        assert set(p) == {"kind", "path", "from", "to", "event", "guard",
                          "expect", "axis", "label"}
        assert p["label"] and p["path"]


def test_state_machine_is_deterministic():
    a = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    b = state_machine_paths(SM_STATES, SM_TRANSITIONS)
    assert a == b


# ══════════════════════════════════════════════════════════════════
# §7 规则注入
# ══════════════════════════════════════════════════════════════════


def test_inject_rules_block_keeps_prompt_and_appends_rules():
    out = inject_rules_block("写一条登录用例", [
        {"id": "R1", "content": "断言必须落到业务码", "kind": "hard"},
        {"id": "R2", "content": "标题用『当…则…』句式", "kind": "soft"},
    ])
    assert out.startswith("写一条登录用例")
    assert "## Project rules" in out
    assert "[R1] 断言必须落到业务码" in out
    assert "[R2] 标题用『当…则…』句式" in out
    assert "### Hard rules" in out and "### Soft rules" in out


def test_inject_rules_block_carries_the_undeclared_is_violation_instruction():
    """未声明=违规（提示词按仓库惯例写英文，见 memory: prompts-written-in-english）。"""
    out = inject_rules_block("prompt", [{"id": "R1", "content": "x", "kind": "hard"}])
    assert DECLARATION_INSTRUCTION in out
    assert "Not declaring = violation." in out


def test_inject_rules_block_empty_rules_still_emits_receipt_contract():
    """无生效规则也要拼块——回执契约不因空规则集消失。"""
    out = inject_rules_block("正文", [])
    assert "## Project rules" in out
    assert DECLARATION_INSTRUCTION in out
    assert "No project rules are currently in effect" in out


def test_inject_rules_block_empty_prompt_returns_block_only():
    out = inject_rules_block("", [{"id": "R1", "content": "x", "kind": "hard"}])
    assert out.startswith("## Project rules")


def test_inject_rules_block_separates_with_single_blank_line():
    out = inject_rules_block("line\n", [{"id": "R1", "content": "x", "kind": "hard"}])
    assert "\n\n\n" not in out
    out2 = inject_rules_block("line", [{"id": "R1", "content": "x", "kind": "hard"}])
    assert "\n\n## Project rules" in out2


def test_inject_rules_block_renders_what_rules_for_selected():
    """作用域筛选归 rules.rules_for，本函数只管渲染拼接（不重复过滤）。"""
    from testing_platform.generation.rules import rules_for

    rows = [
        {"id": "G", "content": "全局规则", "kind": "hard", "scope": "global",
         "enforcement": "active"},
        {"id": "S", "content": "api 端专属", "kind": "soft", "scope": "surface",
         "target_type": "surface", "target_id": "api", "enforcement": "active"},
        {"id": "W", "content": "web 端专属", "kind": "soft", "scope": "surface",
         "target_type": "surface", "target_id": "web", "enforcement": "active"},
        {"id": "D", "content": "停用规则", "kind": "hard", "scope": "global",
         "enforcement": "disabled"},
    ]
    out = inject_rules_block("p", rules_for(rows, surface="api"))
    assert "[G] 全局规则" in out and "[S] api 端专属" in out
    assert "web 端专属" not in out and "停用规则" not in out
