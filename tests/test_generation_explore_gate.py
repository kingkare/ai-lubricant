"""场景化探索门·按内容分级（25 号文 v4.1 §环节7；P2）测试。

纯函数层（:mod:`testing_platform.generation.explore_gate` 不 import tortoise）
——全部用例构造 payload + api_doc 即可，不联网、不启 DB、不调 AI。

覆盖清单：

1. ``gate_required`` web/app 一律过门（ui_flow）；security 端过门；
2. ``gate_required`` api 免门：参数类型/必填/格式/可行性框架类
   （status_code-only expected、框架类 scenario_tag、覆盖轴 C03/C12）；
3. ``gate_required`` api 过门：业务码/字段规则/越权/token/状态迁移/
   幂等/幂等 slot_key 前缀——内容分不按端；
4. 判定只吃结构化信号 + reason 可解释（含 content_class 越界防回归）；
5. ``VERIFY_STATES`` 三态与中文标签映射（未探索/已探索/探索失败）；
6. ``normalize_verify_state``：中文/英文/别名/空/未知 → 归一；
7. ``next_verify_state``：pass→explored、fail→failed、previous 非法按
   unexplored、无结论保持 previous；
8. ``trial_call_plan``：biz_code 查无→blocking、key_fields 契约无→blocking、
   status_code 未声明→blocking、参数名不在契约→blocking；契约齐备全命中→
   非阻塞；api_doc 为空→blocking=False 且只列 status_code/观测项；
   response_time/param_type 不作打回依据；
9. ``change_package``：包号稳定（乱序/重放/重复事件同号）、item 形状、
   ``requires_human_sign`` 恒 True、纯字符串事件、畸形项剔除；
10. 镜像常量防漂移：SURFACE_ASSERTION_FIELDS == case_gen.SURFACE_ASSERTIONS、
    安全 tag 别名 ⊇ gates._TOKEN_INVALID_TAGS ∪ gates._CROSS_TENANT_TAGS。

运行：``python -m pytest tests/test_generation_explore_gate.py -q``
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation import case_gen
from testing_platform.generation import gates as gate_mod
from testing_platform.generation.explore_gate import (
    CONTENT_CLASSES,
    FRAMEWORK_CLASS,
    SURFACE_ASSERTION_FIELDS,
    VERIFY_STATES,
    VERIFY_STATE_LABELS,
    change_package,
    gate_required,
    next_verify_state,
    normalize_verify_state,
    trial_call_plan,
)

# ── 造 payload 的小工厂 ──────────────────────────────────────────────


def _payload(**overrides):
    """一条最普通的 api 正向用例（expected 只有 status_code → 框架类）。"""
    base = {
        "title": "合法参数创建订单成功",
        "surface": "api",
        "intent": "合法参数创建订单",
        "steps": [
            {"seq": 1, "action": "提交创建订单请求", "expected": "请求受理"},
        ],
        "expected": {"status_code": 200, "biz_code": None, "key_fields": None,
                     "response_time": None},
        "preconditions": ["存在可用账号 {{env.user}}"],
        "scenario_tag": "positive",
        "slot_key": "api:11111111-1111-1111-1111-111111111111:amount:C01:valid",
    }
    base.update(overrides)
    return base


_API_DOC = {
    "method": "POST",
    "path": "/api/v1/orders",
    "params": [
        {"name": "amount", "in": "query", "type": "number", "required": True},
        {"name": "page", "in": "query", "type": "integer", "required": False},
    ],
    "request_body": {
        "schema": {"properties": {"sku_id": {"type": "string"},
                                  "count": {"type": "integer"}}},
    },
    "responses": [
        {"status_code": 200, "name": "ok",
         "schema": {"properties": {"code": {"type": "integer"},
                                   "data": {"properties": {"order_id": {"type": "string"},
                                                           "status": {"type": "string"}}}}},
         "example": {"code": 0, "data": {"order_id": "o-1", "status": "created"}}},
        {"status_code": 400, "name": "bad request",
         "example": {"code": 40001, "message": "参数不合法"}},
    ],
}


# ════════════════════════════════════════════════════════════════
# 1) web / app / security 端一律过门
# ════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", ["web", "app"])
def test_ui_surfaces_always_required(surface):
    """§环节7 第三行：web/app 全部过门（选择器/流程全是推测）——内容再框架类也一样。"""
    payload = _payload(surface=surface, expected={
        **{k: None for k in SURFACE_ASSERTION_FIELDS[surface]}, "status_code": 200,
    })
    out = gate_required(payload, surface)
    assert out["required"] is True
    assert out["content_class"] == "ui_flow"
    assert "选择器/流程全是推测" in out["reason"]


def test_security_surface_required():
    """第四端 security：内容就是「接口安全」那一行 → 过门、content_class=security。"""
    payload = _payload(surface="security", expected={
        "reject_code": "403", "audit_trail": "有审计", "data_invisible": True,
    })
    out = gate_required(payload, "security")
    assert out["required"] is True
    assert out["content_class"] == "security"


# ════════════════════════════════════════════════════════════════
# 2) api 免门（框架测试：参数类型/必填/格式/可行性）
# ════════════════════════════════════════════════════════════════

def test_api_status_code_only_not_required():
    """§环节7 第一行：只有 status_code 断言的参数校验类 → 不过门。"""
    out = gate_required(_payload(), "api")
    assert out["required"] is False
    assert out["content_class"] == FRAMEWORK_CLASS
    assert "契约可定" in out["reason"]


@pytest.mark.parametrize("tag", ["positive", "negative", "boundary",
                                 "equivalence-class", "missing", "over-max",
                                 "malformed", "非数字"])
def test_api_framework_tags_not_required(tag):
    """框架类 scenario_tag（六策略参数面 + 变换表变体名）→ 不过门。"""
    out = gate_required(_payload(scenario_tag=tag), "api")
    assert out["required"] is False, out["reason"]
    assert out["content_class"] == FRAMEWORK_CLASS


@pytest.mark.parametrize("axis", ["C01", "C02", "C03", "C12"])
def test_api_framework_axes_not_required(axis):
    """覆盖轴 C01/C02/C03/C12（边界值/输入校验/正常/异常的参数面）→ 不过门。"""
    payload = _payload(
        scenario_tag="",
        slot_key=f"api:11111111-1111-1111-1111-111111111111:amount:{axis}:valid",
    )
    out = gate_required(payload, "api")
    assert out["required"] is False, out["reason"]


def test_api_framework_text_not_required():
    """断言文本只有参数类型/必填/格式词 → 不过门（不靠 tag 也认得出）。"""
    payload = _payload(
        scenario_tag="",
        title="amount 传非数字被拒",
        steps=[{"seq": 1, "action": "提交 amount=abc 的创建订单请求",
                "expected": "返回 400，提示格式错误"}],
        expected={"status_code": 400, "biz_code": None, "key_fields": None,
                  "response_time": None},
    )
    out = gate_required(payload, "api")
    assert out["required"] is False, out["reason"]


# ════════════════════════════════════════════════════════════════
# 3) api 过门（字段规则 / 接口逻辑 / 接口安全）
# ════════════════════════════════════════════════════════════════

def test_api_biz_code_required():
    """业务码断言是推测的 → 过门（接口逻辑）。"""
    payload = _payload(expected={"status_code": 200, "biz_code": "ORDER_CREATED",
                                 "key_fields": None, "response_time": None})
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "logic"
    assert "biz_code" in out["reason"]


def test_api_key_fields_required():
    """key_fields 字段断言是推测的 → 过门（字段规则）。"""
    payload = _payload(expected={"status_code": 200, "biz_code": None,
                                 "key_fields": {"data.order_id": "非空"},
                                 "response_time": None})
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "field_rule"


@pytest.mark.parametrize("tag", ["token_expired", "cross_tenant", "越权", "no-token"])
def test_api_security_tags_required(tag):
    """Token 失效/越权 scenario_tag → 过门（接口安全；别名集与机器门同源）。"""
    payload = _payload(scenario_tag=tag,
                       expected={"status_code": 401, "biz_code": None,
                                 "key_fields": None, "response_time": None})
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "security"


def test_api_auth_slot_key_required():
    """通用轴 slot_key ``auth:{api_id}:{variant}`` → 过门（接口安全）。"""
    payload = _payload(scenario_tag="", slot_key="auth:11111111-1111-1111-1111-111111111111:expired-token")
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "security"


def test_api_security_text_required():
    """断言文本含越权/无 token → 过门（即使 tag/expected 都是框架类）。"""
    payload = _payload(
        scenario_tag="",
        title="无 token 访问被拒",
        steps=[{"seq": 1, "action": "不带 token 请求订单列表",
                "expected": "返回 401 未授权"}],
    )
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "security"


@pytest.mark.parametrize("tag", ["scenario", "state-transition", "idempotency", "concurrency"])
def test_api_logic_tags_required(tag):
    """场景法/状态迁移/幂等/并发 → 过门（接口逻辑）。"""
    out = gate_required(_payload(scenario_tag=tag), "api")
    assert out["required"] is True
    assert out["content_class"] == "logic"


def test_api_idem_slot_key_required():
    """通用轴 slot_key ``idem:{api_id}:{variant}`` → 过门（接口逻辑）。"""
    payload = _payload(scenario_tag="", slot_key="idem:11111111-1111-1111-1111-111111111111:repeat-submit")
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "logic"


def test_api_state_transition_text_required():
    """状态迁移文本（先…再…回退）→ 过门。"""
    payload = _payload(
        scenario_tag="",
        title="订单取消后状态回退",
        steps=[{"seq": 1, "action": "取消已支付订单", "expected": "订单状态回退为待支付"}],
    )
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "logic"


def test_api_field_rule_text_required():
    """字段规则文本（计算/精度/默认值）→ 过门（字段规则）。"""
    payload = _payload(
        scenario_tag="",
        title="订单金额按折扣计算",
        steps=[{"seq": 1, "action": "提交带折扣的订单", "expected": "金额按九折计算保留两位小数"}],
    )
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "field_rule"


def test_api_no_signal_falls_back_to_logic():
    """歧义记录 1：无任何框架类正面证据 → 保守按接口逻辑过门。"""
    payload = _payload(scenario_tag="", slot_key="", expected={},
                       title="某个动作")
    out = gate_required(payload, "api")
    assert out["required"] is True
    assert out["content_class"] == "logic"
    assert "保守" in out["reason"]


def test_preconditions_not_in_corpus():
    """preconditions 写「已登录 token」不构成安全断言（语料只取断言面）。"""
    payload = _payload(preconditions=["已登录且 {{auth.token}} 有效"])
    out = gate_required(payload, "api")
    assert out["required"] is False
    assert out["content_class"] == FRAMEWORK_CLASS


def test_content_class_in_closed_set():
    """content_class 永远落在五值闭集里（防回归）。"""
    for payload in (_payload(), _payload(scenario_tag="token_expired"),
                    _payload(surface="web"), _payload(surface="app"),
                    _payload(surface="security", expected={"reject_code": "x"})):
        surf = payload["surface"]
        out = gate_required(payload, surf)
        assert out["content_class"] in CONTENT_CLASSES
    assert set(CONTENT_CLASSES) == {"framework", "field_rule", "logic",
                                    "security", "ui_flow"}


# ════════════════════════════════════════════════════════════════
# 4) 三态与归一
# ════════════════════════════════════════════════════════════════

def test_verify_states_and_labels():
    """三态常量 + 中文标签映射（25 号文原词）。"""
    assert VERIFY_STATES == ("unexplored", "explored", "failed")
    assert VERIFY_STATE_LABELS == {"unexplored": "未探索", "explored": "已探索",
                                   "failed": "探索失败"}


@pytest.mark.parametrize("raw,expected_state", [
    ("unexplored", "unexplored"),
    ("未探索", "unexplored"),
    ("not_explored", "unexplored"),
    ("pending", "unexplored"),
    ("", "unexplored"),
    (None, "unexplored"),
    ("totally-unknown", "unexplored"),
    ("explored", "explored"),
    ("已探索", "explored"),
    ("verified", "explored"),
    ("passed", "explored"),
    ("failed", "failed"),
    ("explore_failed", "failed"),
    ("探索失败", "failed"),
    ("已探索（试调通过）", "explored"),   # 带修饰的中文按前缀归
])
def test_normalize_verify_state(raw, expected_state):
    assert normalize_verify_state(raw) == expected_state


@pytest.mark.parametrize("previous,outcome,expected_state", [
    (None, "pass", "explored"),
    ("unexplored", "pass", "explored"),
    ("failed", "pass", "explored"),          # 复探成功可翻案
    (None, "fail", "failed"),
    ("explored", "fail", "failed"),          # 已探索再探失败也落失败
    ("whatever-junk", "pass", "explored"),   # previous 非法不影响结论
    ("whatever-junk", "fail", "failed"),
    ("explored", "skipped", "explored"),     # 无结论保持 previous
    (None, "skipped", "unexplored"),
    ("failed", "", "failed"),
])
def test_next_verify_state(previous, outcome, expected_state):
    assert next_verify_state(previous, outcome) == expected_state


# ════════════════════════════════════════════════════════════════
# 5) api 试调清单
# ════════════════════════════════════════════════════════════════

def _kinds(plan):
    return [a["kind"] for a in plan["assertions"]]


def test_trial_call_plan_biz_code_unknown_blocks():
    """§环节7「业务码真实存在，不存在打回」：契约里查不到 biz_code → blocking。"""
    payload = _payload(expected={"status_code": 200, "biz_code": "ORDER_DONE_XYZ",
                                 "key_fields": None, "response_time": None})
    plan = trial_call_plan(payload, _API_DOC)
    biz = [a for a in plan["assertions"] if a["kind"] == "biz_code"]
    assert biz and biz[0]["verifiable"] is True and biz[0]["blocking"] is True
    assert plan["blocking"] is True


def test_trial_call_plan_biz_code_known_not_blocking():
    """契约 example 里有的业务码 → 可核验且非阻塞。"""
    payload = _payload(expected={"status_code": 200, "biz_code": "0",
                                 "key_fields": None, "response_time": None})
    plan = trial_call_plan(payload, _API_DOC)
    biz = [a for a in plan["assertions"] if a["kind"] == "biz_code"]
    assert biz and biz[0]["blocking"] is False and biz[0]["verifiable"] is True


def test_trial_call_plan_key_field_unknown_blocks():
    """断言字段在契约响应里不存在 → blocking（打回）。"""
    payload = _payload(expected={"status_code": 200, "biz_code": None,
                                 "key_fields": {"data.refund_id": "非空"},
                                 "response_time": None})
    plan = trial_call_plan(payload, _API_DOC)
    kf = [a for a in plan["assertions"] if a["kind"] == "key_field"]
    assert kf and kf[0]["blocking"] is True


def test_trial_call_plan_key_field_known_ok():
    """断言字段 data.order_id 在契约 schema 里 → 非阻塞。"""
    payload = _payload(expected={"status_code": 200, "biz_code": None,
                                 "key_fields": {"data.order_id": "非空"},
                                 "response_time": None})
    plan = trial_call_plan(payload, _API_DOC)
    kf = [a for a in plan["assertions"] if a["kind"] == "key_field"]
    assert kf and kf[0]["blocking"] is False and kf[0]["verifiable"] is True


def test_trial_call_plan_status_code_undeclared_blocks():
    """expected.status_code 不在契约 responses 里 → blocking。"""
    payload = _payload(expected={"status_code": 418, "biz_code": None,
                                 "key_fields": None, "response_time": None})
    plan = trial_call_plan(payload, _API_DOC)
    sc = [a for a in plan["assertions"] if a["kind"] == "status_code"]
    assert sc and sc[0]["blocking"] is True
    assert "418" in sc[0]["note"]


def test_trial_call_plan_status_code_declared_ok():
    payload = _payload()
    plan = trial_call_plan(payload, _API_DOC)
    sc = [a for a in plan["assertions"] if a["kind"] == "status_code"]
    assert sc and sc[0]["blocking"] is False


def test_trial_call_plan_param_name_unknown_blocks():
    """引用了契约里没有的参数名 → blocking（打回）。"""
    payload = _payload(steps=[
        {"seq": 1, "action": "提交创建订单",
         "params": {"body": {"sku_id": "{{data.sku}}", "coupon": "X"}}},
    ])
    plan = trial_call_plan(payload, _API_DOC)
    params = [a for a in plan["assertions"] if a["kind"] == "param"]
    by_name = {a["target"]: a for a in params}
    assert by_name["sku_id"]["blocking"] is False
    assert by_name["coupon"]["blocking"] is True
    assert plan["blocking"] is True


def test_trial_call_plan_param_type_never_blocks():
    """歧义记录 4：非数字/超长本是框架类变体载荷——param_type 只记录不打回。"""
    payload = _payload(steps=[
        {"seq": 1, "action": "提交创建订单",
         "params": {"query": {"amount": "abc"}}},
    ])
    plan = trial_call_plan(payload, _API_DOC)
    types = [a for a in plan["assertions"] if a["kind"] == "param_type"]
    assert types and all(a["blocking"] is False for a in types)
    assert types[0]["verifiable"] is True
    assert "number" in types[0]["note"]


def test_trial_call_plan_response_time_never_blocks():
    """response_time 阈值契约无口径 → verifiable=False、blocking=False。"""
    payload = _payload(expected={"status_code": 200, "biz_code": None,
                                 "key_fields": None, "response_time": 500})
    plan = trial_call_plan(payload, _API_DOC)
    rt = [a for a in plan["assertions"] if a["kind"] == "response_time"]
    assert rt and rt[0]["verifiable"] is False and rt[0]["blocking"] is False
    assert plan["blocking"] is False


def test_trial_call_plan_no_api_doc_not_blocking():
    """api_doc 可空：字段/业务码无从核 → blocking=False 但清单仍列出。"""
    payload = _payload(expected={"status_code": 200, "biz_code": "0",
                                 "key_fields": {"data.order_id": "非空"},
                                 "response_time": None})
    plan = trial_call_plan(payload, None)
    assert plan["blocking"] is False
    kinds = _kinds(plan)
    assert "status_code" in kinds and "biz_code" in kinds and "key_field" in kinds
    assert all(a["verifiable"] is False for a in plan["assertions"])


def test_trial_call_plan_empty_payload_yields_empty():
    """payload 无任何断言 → 空清单、非阻塞（不凭空造核验点）。"""
    plan = trial_call_plan({"title": "x"}, None)
    assert plan == {"assertions": [], "blocking": False}


def test_trial_call_plan_expected_free_text_listed():
    """expected 里的自由文本（非契约字段键）→ 列为 expected_text 观测项。"""
    payload = _payload(expected={"status_code": 200, "biz_code": None,
                                 "key_fields": None, "response_time": None,
                                 "summary": "订单创建成功且可查询"})
    plan = trial_call_plan(payload, _API_DOC)
    texts = [a for a in plan["assertions"] if a["kind"] == "expected_text"]
    assert texts and texts[0]["target"] == "summary"


def test_trial_call_plan_security_fields_listed_not_blocking():
    """security 端断言字段（reject_code 等）→ 只列观测项，不静态核。"""
    payload = _payload(expected={"reject_code": "AUTH_401", "audit_trail": "有记录",
                                 "data_invisible": True, "status_code": 403,
                                 "biz_code": None, "key_fields": None,
                                 "response_time": None})
    plan = trial_call_plan(payload, None)
    listed = {a["target"] for a in plan["assertions"] if a["kind"] == "expected_text"}
    assert {"reject_code", "audit_trail", "data_invisible"} <= listed
    assert plan["blocking"] is False


# ════════════════════════════════════════════════════════════════
# 6) 待审变更包
# ════════════════════════════════════════════════════════════════

_EVENTS = [
    {"case_id": "c-1", "scenario_id": "s-1", "step_seq": 2,
     "reason": "登录按钮选择器失效", "failure_kind": "script_issue",
     "evidence": ["shot-002.png"], "suggestion": "改用文本定位"},
    {"case_id": "c-1", "scenario_id": "s-1", "step_seq": 5,
     "reason": "断言字段 order_no 不存在", "failure_kind": "script_issue",
     "screenshot": "shot-005.png"},
    {"case_id": "c-2", "seq": 1, "message": "业务码 50000 真调不存在",
     "error": None},
]


def test_change_package_shape():
    out = change_package(_EVENTS)
    assert out["requires_human_sign"] is True
    assert out["count"] == 3
    assert len(out["items"]) == 3
    assert out["package_id"].startswith("chg-")
    assert len(out["package_id"]) == len("chg-") + 16
    first = out["items"][0]
    assert first["seq"] == 1
    assert first["case_id"] == "c-1"
    assert first["step_seq"] == 2
    assert first["reason"] == "登录按钮选择器失效"
    assert first["evidence"] == ["shot-002.png"]
    assert out["items"][1]["evidence"] == ["shot-005.png"]  # screenshot 别名收进 evidence
    assert out["items"][2]["step_seq"] == 1                 # seq 别名也认


def test_change_package_stable_id_regardless_of_order():
    """包号与事件到达顺序无关（幂等：离线批量重写同批事件同号）。"""
    a = change_package(_EVENTS)
    b = change_package(list(reversed(_EVENTS)))
    assert a["package_id"] == b["package_id"]


def test_change_package_dedup_and_replay():
    """完全重复的事件只留一条；重放同批事件包号不变。"""
    once = change_package(_EVENTS)
    replay = change_package(_EVENTS + _EVENTS)
    assert replay["count"] == once["count"] == 3
    assert replay["package_id"] == once["package_id"]


def test_change_package_different_content_different_id():
    a = change_package(_EVENTS)
    b = change_package(_EVENTS[:-1])
    assert a["package_id"] != b["package_id"]


def test_change_package_string_events_and_garbage():
    """纯字符串事件按 reason 收；畸形项（None/空对象/空串）剔除。"""
    out = change_package([
        "步骤 3 页面白屏",
        {"reason": ""},          # 无任何内容 → 剔
        None,                    # 非 Mapping → 剔
        42,                      # 非 Mapping 非字符串 → 剔
        {"step_seq": "x", "reason": "空壳"},  # seq 非数字保字符串
    ])
    assert out["count"] == 2
    assert out["items"][0]["reason"] == "步骤 3 页面白屏"
    assert out["items"][1]["step_seq"] == "x"


def test_change_package_empty():
    out = change_package([])
    assert out["count"] == 0
    assert out["items"] == []
    assert out["requires_human_sign"] is True


# ════════════════════════════════════════════════════════════════
# 7) 镜像常量防漂移
# ════════════════════════════════════════════════════════════════

def test_surface_assertion_fields_mirror_case_gen():
    """SURFACE_ASSERTION_FIELDS 与 case_gen.SURFACE_ASSERTIONS 逐端相等。"""
    assert SURFACE_ASSERTION_FIELDS == case_gen.SURFACE_ASSERTIONS


def test_security_tags_mirror_gates():
    """安全 tag 别名集 ⊇ 机器门的两类必覆盖场景别名（Token 失效 + 越权）。"""
    from testing_platform.generation.explore_gate import _SECURITY_TAGS
    merged = set(gate_mod._TOKEN_INVALID_TAGS) | set(gate_mod._CROSS_TENANT_TAGS)
    assert merged <= _SECURITY_TAGS
