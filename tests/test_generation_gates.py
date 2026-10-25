"""生成环节机器门（25 号文 §环节6 第1闸，P0 最小版四查）。

每个 check 函数用构造 payload + 假 api_lookup / 集合测：
- 幻觉命中/放行（MG_H1 接口不存在 / MG_H2 参数名不在 params[]）
- 硬编码扫描各形态（占位符放行、字面 URL 拦截、Bearer 字面拦截、凭据字段明文）
- evidence 复用生效（intent/expected 跑 quality.evidence.validate_evidence_claim）
- 幂等撞键（MG_ID）
- 规则回执未声明=violated（MG_RR）+ followed/not_applicable 放行
- api 必覆盖场景缺失 warn（MG_MS）
- run_machine_gates 汇总态迁移（machine_pending ↔ machine_rejected）

不联网、不启 DB、不调 AI——api_lookup 用假的 async callable 注入。
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation.gates import (
    FIVE_SCOPES,
    MANDATORY_API_SCENARIOS,
    check_api_mandatory_scenarios,
    check_evidence,
    check_hardcoded,
    check_hallucination,
    check_idempotency,
    check_rule_receipt,
    make_slot_key,
    run_machine_gates,
)

# ── 假 api_lookup（注入，不 import tortoise）────────────────────
_API_1 = {
    "id": "API-1",
    "method": "POST",
    "path": "/api/v1/orders",
    "params": [
        {"name": "page", "in": "query", "type": "integer"},
        {"name": "size", "in": "query", "type": "integer"},
    ],
    "request_body": {"type": "object", "properties": {
        "product_id": {"type": "string"},
        "quantity": {"type": "integer"},
    }},
}


def _lookup_factory(docs: dict):
    async def _lookup(api_id: str):
        return docs.get(api_id)
    return _lookup


def _clean_case(**overrides):
    """一个过了全部机器门的干净用例（机器门侧无 error）。"""
    base = {
        "api_id": "API-1",
        "intent": "校验下单接口在正常参数下返回 200（见 mc_test_api 契约）",
        "surface": "api",
        "evidence_class": "inferred",  # 等级必须申报（EV0），inferred 免直证来源义务
        "scenario_tag": "token_invalid",  # 已声明一类；越权类由 scenario_tag 别名补
        "applied_rules": [
            {"rule_id": "HR-1", "status": "followed", "why": "已带越权场景"},
        ],
        "expected": {"status_code": 401, "key_fields": []},
        "params": [{"name": "page", "value": "{{env.page}}"}],
        "confirm_ref": {"api_id": "API-1"},
    }
    base.update(overrides)
    return base


# ── 1. 幻觉核验 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_hallucination_api_not_found_is_error():
    case = _clean_case(api_id="API-NOPE")
    issues = await check_hallucination(case, _lookup_factory({"API-1": _API_1}))
    assert any(i.rule == "MG_H1_api_not_found" and i.severity == "error" for i in issues)


@pytest.mark.asyncio
async def test_hallucination_param_not_in_api_is_error():
    case = _clean_case(params=[{"name": "ghost_param", "value": 1}])
    issues = await check_hallucination(case, _lookup_factory({"API-1": _API_1}))
    assert any(i.rule == "MG_H2_param_not_in_api" and i.severity == "error" for i in issues)


@pytest.mark.asyncio
async def test_hallucination_body_param_checked():
    """steps[].params.body 的顶层键也要核（请求体字段名）。"""
    case = _clean_case(steps=[{"type": "request", "params": {
        "method": "POST", "url": "{{env.base_url}}/orders",
        "body": {"nonexistent_field": 1},
    }}])
    issues = await check_hallucination(case, _lookup_factory({"API-1": _API_1}))
    assert any(i.rule == "MG_H2_param_not_in_api" for i in issues)


@pytest.mark.asyncio
async def test_hallucination_passes_for_valid_refs():
    case = _clean_case(
        params=[{"name": "page", "value": 1}, {"name": "product_id", "value": "P-1"}],
    )
    issues = await check_hallucination(case, _lookup_factory({"API-1": _API_1}))
    assert issues == []


@pytest.mark.asyncio
async def test_hallucination_no_api_ref_skipped():
    """无任何 api_id 引用时不核参数（不误报 body 内部键）。"""
    case = {"intent": "流程用例", "params": [{"name": "whatever", "value": 1}]}
    issues = await check_hallucination(case, _lookup_factory({}))
    assert issues == []


# ── 2. 硬编码扫描（数据五作用域）───────────────────────────────
def test_hardcoded_placeholder_passes():
    """{{env.*}}/{{data.*}}/{{local.*}}/{{auth.*}}/{{global.*}} 占位符放行。"""
    case = _clean_case(steps=[{"type": "request", "params": {
        "method": "GET",
        "url": "{{env.base_url}}/orders",
        "headers": {"Authorization": "{{auth.token}}"},
        "body": {"user": "{{data.username}}", "note": "{{local.note}}"},
    }}])
    assert check_hardcoded(case) == []


def test_hardcoded_literal_url_warns():
    case = _clean_case(steps=[{"type": "request", "params": {
        "url": "https://api.example.com/v1/orders",
    }}])
    issues = check_hardcoded(case)
    assert any(i.rule == "MG_HC_literal_url" and i.severity == "warn" for i in issues)


def test_hardcoded_literal_url_after_placeholder_still_warns():
    """混合串里残留字面 host（占位符抹掉后仍以 http(s):// 开头）也要拦。"""
    case = _clean_case(steps=[{"type": "request", "params": {
        "url": "{{env.scheme}}://api.example.com/v1/orders",
    }}])
    issues = check_hardcoded(case)
    assert any(i.rule == "MG_HC_literal_url" for i in issues)


def test_hardcoded_bearer_literal_warns():
    case = _clean_case(steps=[{"type": "request", "params": {
        "headers": {"Authorization": "Bearer abc123def456"},
    }}])
    issues = check_hardcoded(case)
    assert any(i.rule == "MG_HC_literal_bearer" and i.severity == "warn" for i in issues)


def test_hardcoded_bearer_placeholder_passes():
    case = _clean_case(steps=[{"type": "request", "params": {
        "headers": {"Authorization": "Bearer {{auth.token}}"},
    }}])
    assert check_hardcoded(case) == []


def test_hardcoded_credential_key_literal_warns():
    case = _clean_case(steps=[{"type": "request", "params": {
        "body": {"password": "SuperSecret123"},
    }}])
    issues = check_hardcoded(case)
    assert any(i.rule == "MG_HC_literal_credential" and i.severity == "warn" for i in issues)


def test_hardcoded_credential_key_placeholder_passes():
    case = _clean_case(steps=[{"type": "request", "params": {
        "body": {"password": "{{env.password}}"},
    }}])
    assert check_hardcoded(case) == []


def test_hardcoded_all_forms_are_warn_not_error():
    """设计口径=可维护性不合格（warn），不阻断入库。"""
    case = _clean_case(steps=[{"type": "request", "params": {
        "url": "http://localhost:8000/x",
        "headers": {"Authorization": "Bearer xyz"},
        "body": {"api_key": "k-123"},
    }}])
    issues = check_hardcoded(case)
    assert issues and all(i.severity == "warn" for i in issues)
    assert not any(i.severity == "error" for i in issues)


def test_hardcoded_skips_metadata_keys():
    """applied_rules/scenario_tag 等元数据键不参与字面量扫描。"""
    case = _clean_case(
        applied_rules=[{"rule_id": "HR-1", "status": "followed", "why": "https://ok.example.com"}],
        scenario_tag="token_invalid",
    )
    assert check_hardcoded(case) == []


# ── 3. 证据核验（复用 quality.evidence）────────────────────────
def test_evidence_reuse_flags_bare_confidence():
    """intent 文本过 validate_evidence_claim：裸百分数置信度 → error。"""
    case = _clean_case(intent="这个接口 85% confident 是并发问题", evidence_class="inferred")
    issues = check_evidence(case, "inferred")
    assert any(i.rule == "EV1_no_bare_confidence_pct" and i.severity == "error" for i in issues)


def test_evidence_reuse_direct_needs_source():
    """executed 级必须带来源（EV4）。"""
    case = _clean_case(intent="登录必挂", evidence_class="executed")
    issues = check_evidence(case, "executed")
    assert any(i.rule == "EV4_direct_needs_source" for i in issues)


def test_evidence_missing_class_is_error():
    """等级必须申报（EV0）。"""
    case = _clean_case(intent="正常结论")
    issues = check_evidence(case, None)
    assert any(i.rule == "EV0_class_required" for i in issues)


def test_evidence_clean_passes():
    case = _clean_case(
        intent="跑 pytest 输出：3 passed（tests/test_login.py:12-20）",
        evidence_class="executed",
    )
    assert check_evidence(case, "executed") == []


def test_evidence_scans_expected_texts():
    """expected 里的字符串字段也要过证据校验。"""
    case = _clean_case(
        intent="正常",
        evidence_class="inferred",
        expected={"status_code": 200, "note": "影响很多用例"},
    )
    issues = check_evidence(case, "inferred")
    assert any(i.rule == "EV2_no_vague_blast_radius" for i in issues)


# ── 4. 幂等查重 ────────────────────────────────────────────────
def test_slot_key_api_axis_shape():
    """api 轴：api:{id}:{param}:{axis}:{variant}（variant 打包三段）。"""
    k = make_slot_key("API-1", None, "api", "size:C03:min-1")
    assert k == "api:API-1:size:C03:min-1"


def test_slot_key_flow_axis_shape():
    """flow 轴：flow:{tp}:{surface}:{seq}。"""
    k = make_slot_key(None, "TP-01", "web", "10")
    assert k == "flow:TP-01:web:10"


def test_idempotency_dup_warns():
    case = _clean_case(slot_key="api:API-1:size:C03:min-1")
    issues = check_idempotency(case, {"api:API-1:size:C03:min-1"})
    assert any(i.rule == "MG_ID_dup_slot_key" and i.severity == "warn" for i in issues)


def test_idempotency_fresh_passes():
    case = _clean_case(slot_key="api:API-1:size:C03:min-1")
    assert check_idempotency(case, {"api:API-1:other:C01:valid"}) == []


def test_idempotency_computes_key_when_absent():
    """payload 无 slot_key 时现算（从 api_id + confirm_ref.slot）。"""
    case = _clean_case(confirm_ref={"api_id": "API-1", "slot": "size:C03:min-1"})
    issues = check_idempotency(case, {"api:API-1:size:C03:min-1"})
    assert any(i.rule == "MG_ID_dup_slot_key" for i in issues)


# ── 5. hard 规则回执 ───────────────────────────────────────────
def test_rule_receipt_undeclared_is_violation_error():
    """active+hard 规则未在 applied_rules 声明 → error（未声明=违规，坑#2）。"""
    case = _clean_case(applied_rules=[])
    issues = check_rule_receipt(case, [{"rule_id": "HR-1", "kind": "hard", "enforcement": "active"}])
    assert any(i.rule == "MG_RR_undeclared" and i.severity == "error" for i in issues)


def test_rule_receipt_violated_is_error():
    case = _clean_case(applied_rules=[{"rule_id": "HR-1", "status": "violated", "why": ""}])
    issues = check_rule_receipt(case, [{"rule_id": "HR-1"}])
    assert any(i.rule == "MG_RR_violated" and i.severity == "error" for i in issues)


def test_rule_receipt_followed_passes():
    case = _clean_case(applied_rules=[{"rule_id": "HR-1", "status": "followed", "why": "ok"}])
    assert check_rule_receipt(case, [{"rule_id": "HR-1"}]) == []


def test_rule_receipt_not_applicable_needs_why():
    case = _clean_case(applied_rules=[{"rule_id": "HR-1", "status": "not_applicable"}])
    issues = check_rule_receipt(case, [{"rule_id": "HR-1"}])
    assert any(i.rule == "MG_RR_na_no_why" and i.severity == "warn" for i in issues)


def test_rule_receipt_not_applicable_with_why_passes():
    case = _clean_case(applied_rules=[
        {"rule_id": "HR-1", "status": "not_applicable", "why": "本用例是查询接口无写操作"},
    ])
    assert check_rule_receipt(case, [{"rule_id": "HR-1"}]) == []


def test_rule_receipt_extra_declaration_allowed():
    """声明了规则集之外的回执不报错（不强制一一对应，只要求 hard 规则都被覆盖）。"""
    case = _clean_case(applied_rules=[
        {"rule_id": "HR-1", "status": "followed"},
        {"rule_id": "HR-99", "status": "followed"},
    ])
    assert check_rule_receipt(case, [{"rule_id": "HR-1"}]) == []


# ── 6. api 端必覆盖场景（Skill15）──────────────────────────────
def test_api_mandatory_scenarios_missing_warns():
    issues = check_api_mandatory_scenarios(_clean_case(scenario_tag=None), "api")
    rules = {i.rule for i in issues}
    assert "MG_MS_missing_token_invalid" in rules
    assert "MG_MS_missing_cross_tenant" in rules
    assert all(i.severity == "warn" for i in issues)


def test_api_mandatory_scenarios_declared_passes():
    case = _clean_case(scenario_tag=["token_invalid", "cross_tenant"])
    assert check_api_mandatory_scenarios(case, "api") == []


def test_api_mandatory_scenarios_alias_hit():
    """中文/连字符别名都算已声明（宽松匹配，防与未来 tag 词典强耦合）。"""
    case = _clean_case(scenario_tag="token失效")
    issues = check_api_mandatory_scenarios(case, "api")
    assert not any(i.rule == "MG_MS_missing_token_invalid" for i in issues)


def test_api_mandatory_scenarios_skips_non_api_surface():
    """Skill15 只约束 api 端；web/app 端不判。"""
    case = _clean_case(surface="web", scenario_tag=None)
    assert check_api_mandatory_scenarios(case, "web") == []


def test_mandatory_scenarios_table_shape():
    assert len(MANDATORY_API_SCENARIOS) == 2
    assert set(FIVE_SCOPES) == {"env", "auth", "local", "data", "global"}


# ── 7. run_machine_gates 汇总态迁移 ────────────────────────────
@pytest.mark.asyncio
async def test_run_machine_gates_rejected_on_error():
    """有 error（幻觉：接口不存在）→ machine_rejected。"""
    case = _clean_case(api_id="API-NOPE")
    out = await run_machine_gates(
        case,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[{"rule_id": "HR-1"}],
    )
    assert out["valid"] is False
    assert out["review_state"] == "machine_rejected"
    assert any(i.rule == "MG_H1_api_not_found" for i in out["issues"])


@pytest.mark.asyncio
async def test_run_machine_gates_pending_when_only_warns():
    """只有 warn（如必覆盖场景缺一类）→ machine_pending。"""
    case = _clean_case(scenario_tag="token_invalid")  # 缺越权 → 仅 warn
    out = await run_machine_gates(
        case,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[{"rule_id": "HR-1"}],
    )
    assert out["review_state"] == "machine_pending"
    assert out["valid"] is True
    assert any(i.rule == "MG_MS_missing_cross_tenant" for i in out["issues"])


@pytest.mark.asyncio
async def test_run_machine_gates_clean_pending():
    case = _clean_case(scenario_tag=["token_invalid", "cross_tenant"])
    out = await run_machine_gates(
        case,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[{"rule_id": "HR-1"}],
    )
    assert out["valid"] is True
    assert out["review_state"] == "machine_pending"
    assert out["issues"] == []


@pytest.mark.asyncio
async def test_run_machine_gates_unwrapped_proposal_form():
    """proposal 形态 {kind:"case", payload:{...}, surface, evidence_class} 兼容。"""
    proposal = {
        "kind": "case",
        "surface": "api",
        "evidence_class": "inferred",
        "payload": _clean_case(scenario_tag=["token_invalid", "cross_tenant"]),
    }
    out = await run_machine_gates(
        proposal,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[],
    )
    assert out["valid"] is True and out["review_state"] == "machine_pending"


@pytest.mark.asyncio
async def test_run_machine_gates_rule_undeclared_rejects():
    """hard 规则未声明 → error → machine_rejected。"""
    case = _clean_case(
        scenario_tag=["token_invalid", "cross_tenant"],
        applied_rules=[],
    )
    out = await run_machine_gates(
        case,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[{"rule_id": "HR-7"}],
    )
    assert out["valid"] is False
    assert out["review_state"] == "machine_rejected"
    assert any(i.rule == "MG_RR_undeclared" for i in out["issues"])


@pytest.mark.asyncio
async def test_run_machine_gates_idempotency_dup_warn_only():
    """撞 slot_key 只是 warn，不阻断。"""
    case = _clean_case(
        scenario_tag=["token_invalid", "cross_tenant"],
        slot_key="api:API-1:dup",
    )
    out = await run_machine_gates(
        case,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys={"api:API-1:dup"},
        active_hard_rules=[],
    )
    assert out["valid"] is True
    assert out["review_state"] == "machine_pending"
    assert any(i.rule == "MG_ID_dup_slot_key" for i in out["issues"])


# ── 3b. EV4 语义对象分级（e2e 实测修正：expected 逐叶索证误杀 AI 用例）──
def test_evidence_expected_values_exempt_from_ev4():
    """static 级：intent 带来源过 EV4；expected 的断言值（"account_status":
    "active" 这类，来源是需求不是 file:line）不得逐字符索证——旧口径 43.8%→60.9%
    打回率的根因。"""
    case = _clean_case(
        intent="合法凭证登录后账号状态为 active（见 mc_test_requirement 原文）",
        expected={"account_status": "active", "biz_code": "0000"},
    )
    assert check_evidence(case, "static") == []


def test_evidence_expected_still_checked_text_rules():
    """expected 只免 EV4——EV1 假精确/猜因等文本纪律照查，且不带 EV4。"""
    case = _clean_case(
        intent="正常流程（见 mc_test_api 契约）",
        expected={"note": "85% confident 是并发问题"},
    )
    issues = check_evidence(case, "static")
    ev_issues = [i for i in issues if i.path.startswith("$.expected")]
    assert any(i.rule == "EV1_no_bare_confidence_pct" for i in ev_issues)
    assert not any(i.rule == "EV4_direct_needs_source" for i in issues)


def test_evidence_intent_still_needs_source_at_static():
    """intent 层纪律不放松：static 级 intent 没来源 → EV4 照打。"""
    case = _clean_case(intent="登录肯定没问题", expected={"status_code": 200})
    issues = check_evidence(case, "static")
    assert any(i.rule == "EV4_direct_needs_source" and i.path == "$.intent"
               for i in issues)


# ── 3c. 凭据键子串误报（e2e 实测修正）────────────────────────────
def test_credential_key_word_boundary_kills_substring_false_positive():
    """`secretary`/`tokenizer`/`passwordless` 不是凭据键，不许命中。"""
    case = _clean_case(steps=[{"type": "request", "params": {"body": {
        "secretary_name": "Alice",
        "tokenizer_mode": "bpe",
        "passwordless_enabled": "true",
    }}}])
    issues = check_hardcoded(case)
    assert not any(
        i.rule == "MG_HC_literal_credential"
        and ("secretary" in str(i.path) or "tokenizer" in str(i.path)
             or "passwordless" in str(i.path))
        for i in issues
    )


def test_credential_key_underscore_delimited_still_flags():
    """`my_password`（`_` 分隔）仍是凭据键，明文值照报。"""
    case = _clean_case(steps=[{"type": "request", "params": {"body": {
        "my_password": "hunter2-real-secret",
    }}}])
    issues = check_hardcoded(case)
    assert any(i.rule == "MG_HC_literal_credential" and "my_password" in str(i.path)
               for i in issues)


# ── 3d. EV4 派生用例放宽（e2e 实测修正 #2：static 级派生链即来源）────
def test_evidence_static_intent_with_testpoint_provenance_passes():
    """static 级 intent 没带 file:line，但 payload 有测试点派生链
    （testpoint_code 由编排器从 confirmed mc_test_testpoint 机械注入，非 LLM 自由填）
    → 视为已满足来源义务，EV4 放行。"""
    case = _clean_case(
        intent="有效凭据触发git clone自动解析并成功执行",
        basis="fact",
        testpoint_code="TP-0001",
        evidence=["AC-1"],
    )
    issues = check_evidence(case, "static")
    assert not any(i.rule == "EV4_direct_needs_source" for i in issues), issues


def test_evidence_static_intent_no_provenance_still_needs_source():
    """static 级 intent 既没 file:line 又没派生链（无 testpoint_code/evidence）
    → EV4 照打（放宽只给真有来源链的用例，不空放行）。"""
    case = _clean_case(
        intent="有效凭据触发git clone自动解析并成功执行",
        basis="fact",
    )
    case.pop("testpoint_code", None)
    case.pop("evidence", None)
    issues = check_evidence(case, "static")
    assert any(i.rule == "EV4_direct_needs_source" and i.path == "$.intent"
               for i in issues)


def test_evidence_executed_intent_not_relaxed_by_provenance():
    """executed 级不放宽：声称真跑过必须给运行记录，测试点派生链不算运行来源。"""
    case = _clean_case(
        intent="跑过 git clone 成功",
        basis="fact",
        testpoint_code="TP-0001",
        evidence=["AC-1"],
    )
    issues = check_evidence(case, "executed")
    assert any(i.rule == "EV4_direct_needs_source" and i.path == "$.intent"
               for i in issues)
