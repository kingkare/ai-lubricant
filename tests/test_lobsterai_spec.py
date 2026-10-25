"""LobsterAI spec 本地 smoke tests（积分同步与「复合刷新」契约）。

specs/ 是可复制的本地源料目录，在干净部署中可能不存在；缺省时跳过。

背景（2026-10-10 现网投诉）：积分实际已耗尽（profile-summary 实返
totalCreditsRemaining=0），页面仍挂着上一轮的 400/496。根因是三处叠加：
1. ``refresh_auth`` 只续 token 不刷积分——而管理端「刷新账号信息」按钮把它当复合
   刷新（token+签到+额度）用；
2. 账号动作撞 token 过期窗口时不同步积分；
3. 动作内部自愈出的新 token 若不走返回值交回 admin，会被 admin 落库的旧账号行
   快照覆盖回去（旧 token 赢）。
"""

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "lobsterai_code_channel.py"
if not SPEC_PATH.exists():
    pytest.skip("local LobsterAI spec source is not present", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("lobster_spec_under_test", SPEC_PATH)
assert _spec and _spec.loader
lobster = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lobster)

OLD_TOKEN = "old.accessToken.value"
NEW_TOKEN = "NEW.accessToken.value"


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _Routes:
    """按 URL 关键字路由的假 session 工厂；记录调用顺序供断言。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def _pick(self, url):
        for key, value in self.routes.items():
            if key in url:
                return value
        raise AssertionError(f"未预期的 URL: {url}")

    def get(self, url, **kw):
        self.calls.append("GET " + url)
        status, payload = self._pick(url)
        return _Ctx(_Resp(status, payload))

    def post(self, url, **kw):
        self.calls.append("POST " + url)
        status, payload = self._pick(url)
        return _Ctx(_Resp(status, payload))


class _SessionCtx:
    """模拟 ProxyManager facade 的 async with 用法。"""

    def __init__(self, routes: _Routes):
        self._routes = routes

    async def __aenter__(self):
        return self._routes

    async def __aexit__(self, *a):
        return False


class _P:
    """最小 provider 桩：账号字段当实例属性 + 恒可用的假 session。"""

    PROVIDER_NAME = "ch-42"
    base_url = ""

    def __init__(self, account: dict, routes: _Routes):
        self._routes = routes
        self.username = account.get("username", "")
        self.access_token = account.get("access_token", "")
        self.refresh_token = account.get("refresh_token", "")
        self.access_token_expires_at_ms = account.get("access_token_expires_at_ms", 0)
        self.uuid = account.get("uuid", "")
        self.first_keyfrom = account.get("first_keyfrom", "")
        self.latest_keyfrom = account.get("latest_keyfrom", "")
        self.user_id = account.get("user_id", "")
        self.uid = account.get("uid", "")
        self._channel = {}
        self.persisted: list[dict] = []

    def _make_session(self, timeout=None):
        return _SessionCtx(self._routes)

    def _channel_get(self, key, default=""):
        return default

    async def persist_account_fields(self, fields):
        self.persisted.append(dict(fields))
        return True


# 账号行：token 已过期 → 触发自愈路径。
EXPIRED_ACCOUNT = {
    "username": "186****8362",
    "access_token": OLD_TOKEN,
    "refresh_token": "rt-1",
    "access_token_expires_at_ms": int(time.time() * 1000) - 60_000,
    "uuid": "u-1", "first_keyfrom": "1", "latest_keyfrom": "1",
    "user_id": "urs-x", "uid": "116279",
    "quota_text": "496.08", "credits": 496.075548,
}

# token 未过期的同款账号（验证不打无谓 refresh）。
FRESH_ACCOUNT = {**EXPIRED_ACCOUNT,
                 "access_token": NEW_TOKEN,
                 "access_token_expires_at_ms": int(time.time() * 1000) + 86_400_000}

ROUTES = {
    "/api/auth/refresh": (200, {"code": 0, "message": "success", "data": {
        "accessToken": NEW_TOKEN, "refreshToken": "rt-2", "expiresIn": 2592000}}),
    # 额度已耗尽：profile-summary 正常 200，字段归 0（2026-10-10 实测形态）。
    "/api/user/profile-summary": (200, {"code": 0, "message": "success",
                                        "data": {"totalCreditsRemaining": 0}}),
    "/api/client-activities/slot": (200, {"code": 0, "data": {
        "slotState": "available",
        "activity": {"activityCode": "daily_bonus", "configRevision": 3}}}),
    "/context": (200, {"code": 0, "data": {"state": {"claimedToday": True}, "actions": []}}),
    "api-overmind": (200, {"code": 0, "data": {"value": {"version": "2026.10.1"}}}),
}


def test_quota_zero_is_synced_as_zero():
    """totalCreditsRemaining=0 必须如实刷成 '0'——0 是有效值（额度耗尽），不能当异常/缺失。"""
    routes = _Routes(ROUTES)
    out = asyncio.run(lobster._sync_quota(_P(EXPIRED_ACCOUNT, routes), dict(EXPIRED_ACCOUNT)))
    assert out["quota_text"] == "0"
    assert out["credits"] == 0.0


def test_sync_quota_self_healed_token_travels_via_return_value():
    """自愈出的新 token 必须随动作返回值交回 admin：admin 用「旧账号行 ∪ 返回值」整行
    窄写落库，不走返回值就会被旧快照覆盖回去（spec 自己 persist 同样会被覆盖）。"""
    routes = _Routes(ROUTES)
    p = _P(EXPIRED_ACCOUNT, routes)
    out = asyncio.run(lobster.LobsterAIChannel.perform_action(p, "sync_quota", dict(EXPIRED_ACCOUNT), {}))
    assert out["access_token"] == NEW_TOKEN
    assert out["refresh_token"] == "rt-2"
    assert out["quota_text"] == "0"
    assert p.persisted == [], "动作不得自行 persist（admin 统一落库）"


def test_checkin_already_signed_still_syncs_quota():
    """「已签到」早退分支也要带回 quota_text：签到按钮常被当「看余额」用。
    跳过按新契约记 _checkin_status=skipped（框架据此不解冻账号）。"""
    routes = _Routes(ROUTES)
    out = asyncio.run(lobster.LobsterAIChannel.perform_action(
        _P(EXPIRED_ACCOUNT, routes), "checkin", dict(EXPIRED_ACCOUNT), {}))
    assert out["_checkin_status"] == "skipped"
    assert "已签" in out["_checkin_detail"]
    assert out["quota_text"] == "0"
    assert out["access_token"] == NEW_TOKEN


def test_refresh_auth_is_composite_token_checkin_quota():
    """「刷新账号信息」按钮 = refresh_auth 复合入口：新 token + 签到 + 额度一次带回，
    且 token / profile-summary 各只打一次上游。"""
    routes = _Routes(ROUTES)
    p = _P(EXPIRED_ACCOUNT, routes)
    out = asyncio.run(lobster.LobsterAIChannel.refresh_auth(p, dict(EXPIRED_ACCOUNT), {}))
    assert out["access_token"] == NEW_TOKEN
    assert "_checkin_status" in out
    assert out["quota_text"] == "0"
    assert p.persisted == []
    assert len([c for c in routes.calls if "/api/auth/refresh" in c]) == 1
    assert len([c for c in routes.calls if "/api/user/profile-summary" in c]) == 1


def test_refresh_auth_still_syncs_quota_when_checkin_fails():
    """签到查询撞上游故障：签到状态记 failed，但额度照样刷出来（复合刷新不因签到失败缩水）。"""
    routes = _Routes({**ROUTES,
                      "/api/client-activities/slot": (500, {"code": 50000, "message": "boom"})})
    out = asyncio.run(lobster.LobsterAIChannel.refresh_auth(_P(EXPIRED_ACCOUNT, routes),
                                                    dict(EXPIRED_ACCOUNT), {}))
    assert out["_checkin_status"] == "failed"
    assert out["quota_text"] == "0"
    assert out["access_token"] == NEW_TOKEN


def test_sync_quota_skips_refresh_when_token_valid():
    """token 未过期：不刷新、返回值不带 token 字段（避免无谓覆盖），额度照常同步。"""
    routes = _Routes(ROUTES)
    out = asyncio.run(lobster._sync_quota(_P(FRESH_ACCOUNT, routes), dict(FRESH_ACCOUNT)))
    assert not [c for c in routes.calls if "/api/auth/refresh" in c]
    assert "access_token" not in out
    assert out["quota_text"] == "0"


def test_sync_quota_error_uses_message_field():
    """统一信封错误文案取 message（实测字段名），msg 兼容——不是只有 msg。"""
    routes = _Routes({**ROUTES,
                      "/api/user/profile-summary": (200, {"code": 40100, "message": "会话失效"})})
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(lobster._sync_quota(_P(FRESH_ACCOUNT, routes), dict(FRESH_ACCOUNT)))
    assert "会话失效" in exc_info.value.detail


# ==================== 思考禁用信号：出站必须省略，绝不显式下发 ====================

def _payload(base):
    kw = {"_base_payload": base}
    lobster.LobsterAIChannel.payload(None, "chat", "glm-5.3-flash", [], True, kw)
    return kw["_base_payload"]


def test_payload_strips_explicit_thinking_disable():
    """显式关闭思考的取值必须删键（2026-10-10 实测：下发 none/minimal/off 会被龙虾
    判成「该模型始终思考，不支持关闭思考」50201；省略则 200）。覆盖三条链路可能落下的
    关闭形态：openai reasoning_effort、responses reasoning.effort、anthropic thinking。"""
    for disable in ("none", "minimal", "off", "NONE"):
        body = _payload({"model": "m", "reasoning_effort": disable})
        assert "reasoning_effort" not in body, body
    body = _payload({"model": "m", "thinking": {"type": "disabled"}})
    assert "thinking" not in body
    body = _payload({"model": "m", "enable_thinking": False})
    assert "enable_thinking" not in body
    body = _payload({"model": "m", "reasoning": {"effort": "none", "max_tokens": 100}})
    assert "reasoning" not in body


def test_payload_keeps_real_effort_and_normalizes_xhigh():
    """真实档位保留；OpenAI 的 xhigh 归一成龙虾认的 max；medium/low/high/max 原样。"""
    assert _payload({"model": "m", "reasoning_effort": "high"})["reasoning_effort"] == "high"
    assert _payload({"model": "m", "reasoning_effort": "low"})["reasoning_effort"] == "low"
    assert _payload({"model": "m", "reasoning_effort": "medium"})["reasoning_effort"] == "medium"
    assert _payload({"model": "m", "reasoning_effort": "xhigh"})["reasoning_effort"] == "max"
    assert _payload({"model": "m", "reasoning_effort": "max"})["reasoning_effort"] == "max"


def test_payload_still_normalizes_tool_choice():
    """原有的 tool_choice 归一无回归：空/none 删键、对象形态保留。"""
    assert "tool_choice" not in _payload({"model": "m", "tool_choice": "none"})
    assert "tool_choice" not in _payload({"model": "m", "tool_choice": ""})
    assert "tool_choice" not in _payload({"model": "m", "tool_choice": None})
    obj = {"type": "function", "function": {"name": "x"}}
    assert _payload({"model": "m", "tool_choice": obj})["tool_choice"] == obj


def test_payload_missing_base_is_safe():
    """缺 _base_payload / 非 dict：安全返回 None（不炸）。"""
    assert lobster.LobsterAIChannel.payload(None, "chat", "m", [], True, {}) is None
    assert lobster.LobsterAIChannel.payload(
        None, "chat", "m", [], True, {"_base_payload": None}) is None


# ==================== 流内错误状态码分类 ====================

def test_sse_error_status_classifies_request_shape_as_400():
    """请求形态/模型能力类业务码（50201 该模型始终思考、40300 未知模型）报 400：
    换账号无用，且 400 不命中「502 → 冻结账号到今天」规则（2026-10-10 现网一次
    thinking-off 请求把账号冻了 26374 秒）。额度类（40201）仍报 429（可换账号，冻 60s）。"""
    assert lobster._sse_error_status(50201) == 400
    assert lobster._sse_error_status(40300) == 400
    assert lobster._sse_error_status(40201) == 429
    assert lobster._sse_error_status(None) == 429
    assert lobster._sse_error_status("not-a-code") == 429


def test_parse_chunk_proxy_error_uses_classified_status():
    """proxy_error 形态：按业务码分类状态码（429 换账号 / 400 换模型）。"""
    with pytest.raises(HTTPException) as quota:
        lobster.LobsterAIChannel.parse_chunk(
            None, 'event:error\ndata:{"type":"proxy_error","message":"额度不足","code":40201}')
    assert quota.value.status_code == 429
    with pytest.raises(HTTPException) as shape:
        lobster.LobsterAIChannel.parse_chunk(
            None, 'event:error\ndata:{"type":"proxy_error","message":"该模型始终思考","code":50201}')
    assert shape.value.status_code == 400


def test_parse_chunk_error_shape_defers_to_framework():
    """{"error":{...}} 形态返回 None：框架 _check_openai_sse_error 先于本钩子认领
    （它把 code:50201 映射成 502，所以那种形态靠 payload 钩子从源头抹掉关闭信号根治）。"""
    frame = ('event:error\ndata:{"error":{"message":"该模型始终思考",'
             '"type":"upstream_error","code":50201}}')
    assert lobster.LobsterAIChannel.parse_chunk(None, frame) is None
    # 正常帧也返回 None（回落框架标准解析）
    assert lobster.LobsterAIChannel.parse_chunk(
        None, 'data:{"choices":[{"delta":{"content":"hi"}}]}') is None


# ==================== tool_calls arguments 修复（50204 根治）====================

def _repair_one(raw_args):
    """修复单个 arguments 并返回新值（走写时复制路径）。"""
    msgs = [{"role": "assistant", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "x", "arguments": raw_args}}]}]
    out = lobster._repair_tool_arguments(msgs)
    return out[0]["tool_calls"][0]["function"]["arguments"]


def test_repair_truncated_arguments_from_real_incident():
    """真实事故样本（request_log 554393）：agent 坏历史里的截断 arguments 被每次请求重放，
    Qwen code 模型严格校验 → 400 → 龙虾包 200+error 帧 code=50204 → 框架映射 502 →
    命中「502 冻结到今天」冻号 5.5h。修复后必须是合法 JSON。"""
    # 截断在数组里、缺收尾 }：可恢复 → 保留内容补全
    out = _repair_one('{"completed": \n["拿到 def_id=2 真实定义", "确认平台代码位置"]')
    assert json.loads(out) == {"completed": ["拿到 def_id=2 真实定义", "确认平台代码位置"]}
    # 转义中截断：补上收尾引号+大括号 → 保住已写入的内容（比整条丢成 {} 更好）
    out = _repair_one('{"code": "import re, os\\nroot = r\\"D:\\\\x\\"\\nenv = open(')
    assert json.loads(out) == {"code": "import re, os\nroot = r\"D:\\x\"\nenv = open("}
    # 合法历史零改动
    assert _repair_one('{"a": 1, "b": [1, 2]}') == '{"a": 1, "b": [1, 2]}'


def test_repair_degenerates_unrecoverable_to_empty_object():
    """括号错配/尾随垃圾等不可恢复的 → 退化成 '{}'（等价无参数调用），绝不 50204。"""
    assert json.loads(_repair_one('{"a": [1, 2}')) == {}
    assert json.loads(_repair_one('{"a": 1} trailing garbage')) == {}
    assert json.loads(_repair_one("")) == {}
    assert json.loads(_repair_one("not json at all")) == {}


def test_repair_dict_arguments_to_json_string():
    """少数客户端直接塞对象：上游要字符串 → 序列化过去。"""
    out = _repair_one({"k": "值"})
    assert isinstance(out, str) and json.loads(out) == {"k": "值"}


def test_repair_is_copy_on_write():
    """写时复制（对齐框架 strip_assistant_reasoning_content）：改动只落在新列表，
    原列表与其余消息对象原样保留引用——messages 会被同一请求的其它候选/渠道重试复用，
    原地改会跨重试泄漏。无改动时返回**原列表对象**（零开销短路）。"""
    bad = {"id": "c", "type": "function",
           "function": {"name": "x", "arguments": '{"a": [1, 2'}}
    good_call = {"id": "g", "type": "function",
                 "function": {"name": "y", "arguments": '{"ok":1}'}}
    msgs = [
        {"role": "user", "content": "hi"},           # 非 assistant：原样保留引用
        {"role": "assistant", "tool_calls": [bad, good_call]},
        {"role": "tool", "tool_call_id": "g", "content": "r"},
    ]
    out = lobster._repair_tool_arguments(msgs)
    assert out is not msgs, "有改动时必须返回新列表"
    assert out[0] is msgs[0], "未改动的消息原样保留引用"
    assert out[2] is msgs[2]
    repaired = out[1]["tool_calls"][0]["function"]["arguments"]
    json.loads(repaired)
    assert bad["function"]["arguments"] == '{"a": [1, 2', "原对象不被污染"
    assert out[1]["tool_calls"][1] is good_call, "同消息里合法的 tool_call 原样保留引用"
    # 无改动：返回原列表对象
    plain = [{"role": "user", "content": "hi"}]
    assert lobster._repair_tool_arguments(plain) is plain
    assert lobster._repair_tool_arguments(None) is None


def test_payload_hook_repairs_bad_arguments_end_to_end():
    """payload 钩子（出站定型处）把修复后的列表写回出站 body：坏历史进、合法出。"""
    tc = {"id": "c", "type": "function",
          "function": {"name": "x", "arguments": '{"a": [1, 2'}}
    kw = {"_base_payload": {"model": "m",
                            "messages": [{"role": "assistant", "tool_calls": [tc]}]}}
    lobster.LobsterAIChannel.payload(None, "chat", "m", [], True, kw)
    out = kw["_base_payload"]["messages"][0]["tool_calls"][0]["function"]["arguments"]
    json.loads(out)  # 必须合法


def test_50204_classified_as_request_shape_error():
    """50204（function.arguments 非法 JSON）归入请求形态类 → 400：换账号无用、不冻号。"""
    assert lobster._sse_error_status(50204) == 400
