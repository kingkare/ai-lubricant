"""校验器（07 §2/§4）测试。

人造场景 dict 纯函数测试——不建库、不联网、不依赖 PG。
覆盖本轮实现的每条硬规则：合法场景全过 + 每条规则至少 1 正 1 反。

运行：``python -m pytest tests/test_quality_validators.py -q``
"""

from __future__ import annotations

import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from testing_platform.engine.assertions import build_default_registry as build_assertion_registry
from testing_platform.engine.tools import build_default_registry as build_tool_registry
from testing_platform.quality import (
    ValidationResult,
    ValidationIssue,
    validate,
)


# ── 默认注册表（内置 3 工具 / 5 断言）─────────────────────────
@pytest.fixture
def tools():
    return build_tool_registry()


@pytest.fixture
def assertions():
    return build_assertion_registry()


# ── 合法场景（通过所有规则）──────────────────────────────────────
def _valid_scenario() -> dict:
    """一份能通过全部硬规则的合法场景（每条规则都满足）。"""
    return {
        "code": "SCN-0001",
        "name": "用户下单全流程",
        "transport_id": "tr-001",
        "env_id": "env-001",
        "local_vars": {"order_id": None, "token": None},
        "flow": {
            "pre_scenarios": ["SCN-LOGIN"],
            "conditions": [
                {"if": "{{data.vip}} == true", "then": ["create_order"], "else": ["verify_order"]}
            ],
            "early_exit": [
                {"after": "verify_order", "when": "{{response.status}} == 401"}
            ],
        },
        "steps": [
            {
                "step_key": "create_order",
                "seq": 10,
                "type": "request",
                "description": "创建订单",
                "params": {
                    "method": "POST",
                    "url": "{{env.base_url}}/api/v1/orders",
                    "headers": {"Authorization": "{{auth.token}}"},
                    "body": {"product_id": "{{data.product_id}}", "quantity": 1},
                },
                "post_checks": [
                    {"type": "status_code", "expected": 201},
                    {"type": "json_path", "path": "$.data.order_id", "operator": "not_null"},
                ],
                "post_cond": [
                    {"set": "{{local.order_id}} = {{response.json.data.order_id}}"},
                ],
            },
            {
                "step_key": "verify_order",
                "seq": 20,
                "type": "request",
                "description": "查询订单",
                "params": {
                    "method": "GET",
                    "url": "{{env.base_url}}/api/v1/orders/{{local.order_id}}",
                },
                "post_checks": [
                    {"type": "json_path", "path": "$.data.status", "operator": "eq", "expected": "created"},
                ],
            },
        ],
    }


def _rules(result: ValidationResult) -> list[str]:
    """取结果中的规则编号列表（按出现顺序）。"""
    return [i.rule for i in result.issues]


# ── 合法场景：valid=True, issues=[] ──────────────────────────────
def test_valid_scenario_passes(tools, assertions):
    """合法场景：所有规则通过，valid=True 且 issues 为空（每条规则的正例）。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert result.valid is True
    assert result.issues == []
    assert result.to_dict() == {"valid": True, "issues": []}


# ── 07 §2.1 规则 1：必填字段 ────────────────────────────────────
def test_rule1_missing_required_fields(tools, assertions):
    """规则 1 反例：code/name 缺失 → error。"""
    scn = _valid_scenario()
    scn["code"] = ""
    del scn["name"]
    result = validate(scn, tools, assertions)
    assert not result.valid
    rules = _rules(result)
    assert rules.count("R1_required_fields") == 2  # code + name 各一条


def test_rule1_positive_present(tools, assertions):
    """规则 1 正例：code/name 非空 → 不触发 R1。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R1_required_fields" not in _rules(result)


# ── 07 §2.1 规则 2：JSONB 结构 ───────────────────────────────────
def test_rule2_schema_violations(tools, assertions):
    """规则 2 反例：params 非对象 / post_checks 非数组 / steps 非数组。"""
    scn = _valid_scenario()
    scn["steps"][0]["params"] = "not-an-object"  # params 必须是对象
    scn["steps"][0]["post_checks"] = {"type": "status_code"}  # post_checks 必须是数组
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R2_schema" in _rules(result)


def test_rule2_steps_not_list(tools, assertions):
    """规则 2 反例：steps 不是数组。"""
    scn = _valid_scenario()
    scn["steps"] = {"step": {}}
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R2_schema" in _rules(result)


def test_rule2_positive_shape(tools, assertions):
    """规则 2 正例：合法结构 → 不触发 R2。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R2_schema" not in _rules(result)


# ── 07 §2.1 规则 3：引用存在 ────────────────────────────────────
def test_rule3_empty_reference(tools, assertions):
    """规则 3 反例：transport_id 引用空 id。"""
    scn = _valid_scenario()
    scn["transport_id"] = ""
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R3_reference_present" in _rules(result)


def test_rule3_empty_pre_scenario_ref(tools, assertions):
    """规则 3 反例：前置场景引用空 id。"""
    scn = _valid_scenario()
    scn["flow"]["pre_scenarios"] = [""]  # 空引用
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R3_reference_present" in _rules(result)


def test_rule3_positive(tools, assertions):
    """规则 3 正例：引用 id 非空。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R3_reference_present" not in _rules(result)


# ── 07 §2.1 规则 4：step_key / seq 唯一 ──────────────────────────
def test_rule4_duplicate_step_key(tools, assertions):
    """规则 4 反例：step_key 场景内重复。"""
    scn = _valid_scenario()
    scn["steps"][1]["step_key"] = "create_order"  # 与第一步重复
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R4_step_key_unique" in _rules(result)


def test_rule4_duplicate_seq(tools, assertions):
    """规则 4 反例：seq 不重复被违反。"""
    scn = _valid_scenario()
    scn["steps"][1]["seq"] = 10  # 与第一步相同
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R4_seq_unique" in _rules(result)


def test_rule4_positive_unique(tools, assertions):
    """规则 4 正例：step_key / seq 唯一。"""
    result = validate(_valid_scenario(), tools, assertions)
    rules = _rules(result)
    assert "R4_step_key_unique" not in rules
    assert "R4_seq_unique" not in rules


# ── 07 §2.2 规则 5：后置校验必填 ────────────────────────────────
def test_rule5_missing_post_checks(tools, assertions):
    """规则 5 反例：enabled 步骤缺 post_checks。"""
    scn = _valid_scenario()
    del scn["steps"][0]["post_checks"]
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R5_post_checks_required" in _rules(result)


def test_rule5_disabled_step_exempt(tools, assertions):
    """规则 5 正例：disabled 步骤不要求 post_checks。"""
    scn = _valid_scenario()
    del scn["steps"][1]["post_checks"]
    scn["steps"][1]["enabled"] = False
    result = validate(scn, tools, assertions)
    assert "R5_post_checks_required" not in _rules(result)


def test_rule5_check_step_with_params_checks(tools, assertions):
    """规则 5 正例：check 步骤把断言写在 params.checks（executor 兼容）也算满足。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "check",
        # value 缺省取当前 response；checks 在工具执行后评估（executor defer_checks）
        "params": {"value": "{{data.body}}", "checks": [{"type": "status_code", "expected": 201}]},
    }
    result = validate(scn, tools, assertions)
    assert "R5_post_checks_required" not in _rules(result)


def test_rule5_wait_step_exempt(tools, assertions):
    """规则 5 正例：wait 步骤无有意义产出 → 豁免 post_checks。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "pause",
        "seq": 10,
        "type": "wait",
        "params": {"seconds": 1},
    }
    result = validate(scn, tools, assertions)
    assert "R5_post_checks_required" not in _rules(result)


# ── 07 §2.2 规则 6：校验类型已注册 ───────────────────────────────
def test_rule6_unregistered_assertion(tools, assertions):
    """规则 6 反例：post_check.type 未注册。"""
    scn = _valid_scenario()
    scn["steps"][0]["post_checks"][0] = {"type": "totally_made_up", "expected": 1}
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R6_assertion_registered" in _rules(result)


def test_rule6_registered_assertion(tools, assertions):
    """规则 6 正例：post_check.type 已注册（status_code/json_path）。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R6_assertion_registered" not in _rules(result)


def test_rule6_method_check_exempt(tools, assertions):
    """规则 6 正例：method/script 类校验不走断言注册表（method_ref 由规则 8 管）。"""
    scn = _valid_scenario()
    scn["steps"][0]["post_checks"] = [
        {"type": "method", "method_ref": "base.assert.amount_match"}
    ]
    # 未提供方法注册表 → 规则 8 跳过；规则 6 也豁免 method 类
    result = validate(scn, tools, assertions)
    rules = _rules(result)
    assert "R6_assertion_registered" not in rules
    assert "R8_method_ref_exists" not in rules  # 无方法注册表 → 跳过


# ── 07 §2.2 规则 7：步骤类型合法 ────────────────────────────────
def test_rule7_invalid_step_type(tools, assertions):
    """规则 7 反例：未知步骤类型。"""
    scn = _valid_scenario()
    scn["steps"][0]["type"] = "frobnicate"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R7_step_type_valid" in _rules(result)


def test_rule7_valid_types(tools, assertions):
    """规则 7 正例：10 种合法类型（request/wait/check 等都在枚举内）。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R7_step_type_valid" not in _rules(result)


# ── 07 §2.2 规则 8：方法引用存在 ────────────────────────────────
def test_rule8_unknown_method_ref(tools, assertions):
    """规则 8 反例：method_ref 不在方法注册表（同时记 A4）。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "script",
        "method_ref": "base.nonexistent.method",
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    methods = {"base.crypto.sign": {"signature": {}}}
    result = validate(scn, tools, assertions, method_registry=methods)
    assert not result.valid
    rules = _rules(result)
    assert "R8_method_ref_exists" in rules
    assert "A4_fabricated_ref" in rules  # 凭空造方法


def test_rule8_known_method_ref(tools, assertions):
    """规则 8 正例：method_ref 在方法注册表中（dict 形态）。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "script",
        "method_ref": "base.crypto.sign",
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    methods = {"base.crypto.sign": {"signature": {}}}
    result = validate(scn, tools, assertions, method_registry=methods)
    assert "R8_method_ref_exists" not in _rules(result)


def test_rule8_callable_method_registry(tools, assertions):
    """规则 8 正例：方法注册表传 callable 也能查（接口预留形态）。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "script",
        "method_ref": "base.crypto.sign",
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    result = validate(scn, tools, assertions, method_registry=lambda ref: ref == "base.crypto.sign")
    assert "R8_method_ref_exists" not in _rules(result)


def test_rule8_no_registry_skipped(tools, assertions):
    """规则 8 边界：未提供方法注册表 → 跳过（v1 引擎未接入方法注册表，不误判）。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "script",
        "method_ref": "base.crypto.sign",
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    result = validate(scn, tools, assertions)  # 不传 method_registry
    rules = _rules(result)
    assert "R8_method_ref_exists" not in rules
    assert "A4_fabricated_ref" not in rules


# ── 07 §2.2 规则 9：依赖白名单 ──────────────────────────────────
def test_rule9_custom_tool_not_whitelisted(tools, assertions):
    """规则 9 反例：custom 步骤 tool_ref 既未注册也无 custom: 前缀。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "custom",
        "params": {"tool_ref": "ghost_tool"},
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R9_tool_whitelist" in _rules(result)


def test_rule9_custom_prefix_allowed(tools, assertions):
    """规则 9 正例：custom: 前缀的 tool_ref 合法（框架自定义工具）。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "custom",
        "params": {"tool_ref": "custom:my_tool"},
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    result = validate(scn, tools, assertions)
    assert "R9_tool_whitelist" not in _rules(result)


def test_rule9_registered_tool_allowed(tools, assertions):
    """规则 9 正例：已注册工具作为 tool_ref 合法。"""
    scn = _valid_scenario()
    scn["steps"][0] = {
        "step_key": "create_order",
        "seq": 10,
        "type": "custom",
        "params": {"tool_ref": "http"},  # 内置已注册工具
        "post_checks": [{"type": "status_code", "expected": 200}],
    }
    result = validate(scn, tools, assertions)
    assert "R9_tool_whitelist" not in _rules(result)


# ── 07 §2.2 规则 10：变量作用域前缀 ─────────────────────────────
def test_rule10_unknown_scope_prefix(tools, assertions):
    """规则 10 反例：{{foo.bar}} 前缀未知。"""
    scn = _valid_scenario()
    scn["steps"][0]["params"]["url"] = "{{nonsense.thing}}/path"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R10_scope_prefix" in _rules(result)


def test_rule10_known_scopes(tools, assertions):
    """规则 10 正例：所有已知作用域前缀都合法。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R10_scope_prefix" not in _rules(result)


def test_rule10_bare_variable_allowed(tools, assertions):
    """规则 10 边界：裸变量名（无前缀，如 {{x}}）由解析器回退，不触发 R10。"""
    scn = _valid_scenario()
    # 裸变量名允许（解析器按优先级回退）；这里只验证不报 R10
    scn["local_vars"]["bare"] = "value"
    scn["steps"][0]["params"]["headers"]["X-Custom"] = "{{bare}}"
    result = validate(scn, tools, assertions)
    assert "R10_scope_prefix" not in _rules(result)


# ── 07 §2.2 规则 11 / §2.3 规则 19：变量定义在前 ────────────────
def test_rule11_local_used_before_definition(tools, assertions):
    """规则 11/19 反例：{{local.k}} 在声明/写入前使用。"""
    scn = _valid_scenario()
    # 让第二步读取一个从未声明也从未被 post_cond 写入的 local 变量
    scn["steps"][1]["params"]["url"] = "{{env.base_url}}/orders/{{local.never_set}}"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R11_local_defined" in _rules(result)


def test_rule11_local_declared_in_local_vars(tools, assertions):
    """规则 11/19 正例：local_vars 声明的变量可被后续步骤消费。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R11_local_defined" not in _rules(result)


def test_rule11_local_set_by_prior_post_cond(tools, assertions):
    """规则 11/19 正例：先前步骤 post_cond 写入的 local 可被后续步骤消费。"""
    scn = _valid_scenario()
    # 第一步 post_cond 写 {{local.computed}}；第二步消费它
    scn["steps"][0]["post_cond"].append(
        {"set": "{{local.computed}} = {{response.json.data.order_id}}"}
    )
    scn["steps"][1]["params"]["headers"] = {"X-Order": "{{local.computed}}"}
    result = validate(scn, tools, assertions)
    assert "R11_local_defined" not in _rules(result)


# ── 07 §2.2 规则 12：环境变量不硬编码 ───────────────────────────
def test_rule12_hardcoded_url(tools, assertions):
    """规则 12 反例：params 里出现 http(s):// 字面量。"""
    scn = _valid_scenario()
    scn["steps"][0]["params"]["url"] = "https://api.example.com/orders"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R12_no_hardcoded_env" in _rules(result)


def test_rule12_hardcoded_credential(tools, assertions):
    """规则 12 反例：password 键明文值。"""
    scn = _valid_scenario()
    scn["steps"][0]["params"]["body"]["password"] = "hunter2"  # 明文，无 {{}}
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R12_no_hardcoded_env" in _rules(result)


def test_rule12_templated_url_allowed(tools, assertions):
    """规则 12 正例：URL 走 {{env.*}} 模板。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R12_no_hardcoded_env" not in _rules(result)


def test_rule12_templated_credential_allowed(tools, assertions):
    """规则 12 正例：凭据字段走 {{auth.*}}/{{env.*}} 模板（含 {{}} 即豁免）。"""
    scn = _valid_scenario()
    scn["steps"][0]["params"]["body"]["password"] = "{{env.password}}"
    result = validate(scn, tools, assertions)
    assert "R12_no_hardcoded_env" not in _rules(result)


# ── 07 §2.3 规则 20：flow 引用存在 ──────────────────────────────
def test_rule20_flow_step_ref_missing(tools, assertions):
    """规则 20 反例：flow.conditions.then 引用不存在的 step_key。"""
    scn = _valid_scenario()
    scn["flow"]["conditions"][0]["then"] = ["ghost_step"]
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R20_flow_step_exists" in _rules(result)


def test_rule20_early_exit_ref_missing(tools, assertions):
    """规则 20 反例：flow.early_exit.after 引用不存在的 step_key。"""
    scn = _valid_scenario()
    scn["flow"]["early_exit"][0]["after"] = "ghost_step"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R20_flow_step_exists" in _rules(result)


def test_rule20_positive(tools, assertions):
    """规则 20 正例：flow 引用的 step_key 都存在。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R20_flow_step_exists" not in _rules(result)


# ── 07 §2.3 规则 21：response/result 单步骤临时作用域 ──────────
def test_rule21_cross_step_response_in_params(tools, assertions):
    """规则 21 反例：下一步骤 params 引用 {{response.*}}（跨步骤）。"""
    scn = _valid_scenario()
    # 第二步 params 直接引用第一步的 response → 跨步骤临时作用域
    scn["steps"][1]["params"]["url"] = "{{env.base_url}}/orders/{{response.json.data.order_id}}"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "R21_ephemeral_placement" in _rules(result)


def test_rule21_response_in_post_cond_allowed(tools, assertions):
    """规则 21 正例：{{response.*}} 在本步骤 post_cond 右值里合法。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "R21_ephemeral_placement" not in _rules(result)


def test_rule21_response_in_post_checks_allowed(tools, assertions):
    """规则 21 正例：{{response.*}} 在本步骤 post_checks 里合法（本步骤产出）。"""
    scn = _valid_scenario()
    scn["steps"][0]["post_checks"].append(
        {"type": "json_path", "path": "$.data.order_id", "operator": "eq",
         "expected": "{{response.json.data.order_id}}"}  # 本步骤 post_check 引用本步骤 response
    )
    result = validate(scn, tools, assertions)
    assert "R21_ephemeral_placement" not in _rules(result)


# ── 07 §4 A4：凭空造工具/方法 ───────────────────────────────────
def test_a4_fabricated_tool(tools, assertions):
    """A4 反例：显式 tool 字段引用未注册工具（凭空造工具）。"""
    scn = _valid_scenario()
    scn["steps"][0]["tool"] = "teleport"  # 不在任何注册表
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "A4_fabricated_ref" in _rules(result)


def test_a4_fabricated_tool_ref(tools, assertions):
    """A4 反例：非 custom 步骤的 params.tool_ref 引用未注册工具。"""
    scn = _valid_scenario()
    scn["steps"][0]["params"]["tool_ref"] = "imaginary_tool"
    result = validate(scn, tools, assertions)
    assert not result.valid
    assert "A4_fabricated_ref" in _rules(result)


def test_a4_no_fabrication(tools, assertions):
    """A4 正例：所有引用都来自注册表。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert "A4_fabricated_ref" not in _rules(result)


# ── 多规则组合：单个问题步骤可同时触发多条规则 ─────────────────
def test_multiple_rules_combined(tools, assertions):
    """组合反例：一个步骤同时违反 规则5 + 规则7 + 规则10。"""
    scn = {
        "code": "SCN-X",
        "name": "组合反例",
        "steps": [
            {
                "step_key": "bad",
                "seq": 10,
                "type": "frobnicate",  # 规则 7
                "params": {"url": "{{nonsense.x}}"},  # 规则 10
                # 无 post_checks → 规则 5
            }
        ],
    }
    result = validate(scn, tools, assertions)
    assert not result.valid
    rules = _rules(result)
    assert "R5_post_checks_required" in rules
    assert "R7_step_type_valid" in rules
    assert "R10_scope_prefix" in rules


# ── ValidationResult / ValidationIssue 数据结构 ──────────────────
def test_validation_result_structures(tools, assertions):
    """结构化错误 JSON 形态（07 §1：{path, rule, msg, severity, hint}）。"""
    scn = {"code": "", "name": ""}  # 缺 code/name
    result = validate(scn, tools, assertions)
    assert result.valid is False
    issue = result.issues[0]
    assert isinstance(issue, ValidationIssue)
    d = issue.to_dict()
    assert set(d.keys()) == {"path", "rule", "msg", "severity", "hint"}
    assert d["severity"] == "error"
    assert d["rule"] == "R1_required_fields"
    assert d["path"] == "$.code"
    # to_dict 外层 {valid, issues[]}
    out = result.to_dict()
    assert out["valid"] is False
    assert isinstance(out["issues"], list)
    assert out["issues"][0]["rule"] == "R1_required_fields"


def test_empty_issues_valid(tools, assertions):
    """无问题 → valid=True，to_dict {valid:True, issues:[]}。"""
    result = validate(_valid_scenario(), tools, assertions)
    out = result.to_dict()
    assert out == {"valid": True, "issues": []}


# ── registry.py 适配层：dict / callable / 引擎注册表 都能查 ──────
def test_registry_dict_form(tools, assertions):
    """registry.py 适配：传 dict 形态的注册表也能工作。"""
    scn = _valid_scenario()
    scn["steps"][0]["post_checks"][0] = {"type": "bogus", "expected": 1}
    # 用 dict 形态的断言注册表（只含 status_code）
    result = validate(scn, tools, {"status_code": {}})
    assert not result.valid
    # json_path 不在 dict 注册表里 → 触发 R6（第二步的 json_path 也未注册）
    assert "R6_assertion_registered" in _rules(result)


def test_registry_engine_objects(tools, assertions):
    """registry.py 适配：直接传引擎 ToolRegistry / AssertionRegistry 实例。"""
    result = validate(_valid_scenario(), tools, assertions)
    assert result.valid


def test_registry_assertion_missing_has_method(tools, assertions):
    """registry.py 适配：AssertionRegistry 没有 .has()，适配层用 .names() 兜底。"""
    # 引擎 AssertionRegistry 只有 get/names，没有 has；验证适配层正确处理
    assert not hasattr(assertions, "has")
    result = validate(_valid_scenario(), tools, assertions)
    assert result.valid  # 适配层成功查询已注册的 status_code/json_path


# ── 流程控制成块合法性（R22_block_balance）+ 控制步骤 R5 豁免 ──
def test_control_flow_if_else_end_if_valid(tools, assertions):
    """完整 if/else/end_if 块 + 控制步骤豁免 R5 → 合法。"""
    scn = {
        "code": "SCN-CF", "name": "条件分支",
        "local_vars": {"flag": None},
        "steps": [
            {"step_key": "i", "seq": 10, "type": "if", "params": {"condition": "{{local.flag}}"}},
            {"step_key": "a", "seq": 20, "type": "request",
             "params": {"method": "GET", "url": "{{env.base_url}}/a"},
             "post_checks": [{"type": "status_code", "expected": 200}]},
            {"step_key": "e", "seq": 30, "type": "else"},
            {"step_key": "b", "seq": 40, "type": "request",
             "params": {"method": "GET", "url": "{{env.base_url}}/b"},
             "post_checks": [{"type": "status_code", "expected": 200}]},
            {"step_key": "en", "seq": 50, "type": "end_if"},
        ],
    }
    result = validate(scn, tools, assertions)
    rules = {i.rule for i in result.issues}
    assert "R22_block_balance" not in rules, [i.msg for i in result.issues if i.rule == "R22_block_balance"]
    assert "R5_post_checks_required" not in rules, "控制步骤应豁免 R5"


def test_control_flow_unmatched_end_if_errors(tools, assertions):
    """没有对应 if 的 end_if → R22。"""
    scn = {
        "code": "SCN-CF", "name": "坏块",
        "steps": [
            {"step_key": "a", "seq": 10, "type": "request",
             "params": {"method": "GET", "url": "{{env.base_url}}/a"},
             "post_checks": [{"type": "status_code", "expected": 200}]},
            {"step_key": "en", "seq": 20, "type": "end_if"},
        ],
    }
    result = validate(scn, tools, assertions)
    assert any(i.rule == "R22_block_balance" for i in result.issues), [i.msg for i in result.issues]


def test_control_flow_unclosed_if_errors(tools, assertions):
    """if 缺 end_if → R22。"""
    scn = {
        "code": "SCN-CF", "name": "未闭合",
        "steps": [
            {"step_key": "i", "seq": 10, "type": "if", "params": {"condition": "true"}},
            {"step_key": "a", "seq": 20, "type": "request",
             "params": {"method": "GET", "url": "{{env.base_url}}/a"},
             "post_checks": [{"type": "status_code", "expected": 200}]},
        ],
    }
    result = validate(scn, tools, assertions)
    assert any(i.rule == "R22_block_balance" for i in result.issues)


def test_control_flow_bare_else_without_if_errors(tools, assertions):
    """else 不在 if 块内 → R22。"""
    scn = {
        "code": "SCN-CF", "name": "裸 else",
        "steps": [
            {"step_key": "e", "seq": 10, "type": "else"},
            {"step_key": "a", "seq": 20, "type": "request",
             "params": {"method": "GET", "url": "{{env.base_url}}/a"},
             "post_checks": [{"type": "status_code", "expected": 200}]},
            {"step_key": "en", "seq": 30, "type": "end_if"},
        ],
    }
    result = validate(scn, tools, assertions)
    assert any(i.rule == "R22_block_balance" for i in result.issues)
