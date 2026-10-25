"""QoderWork spec 本地契约与协议 smoke tests。

specs/ 是可复制的本地源料目录，在干净部署中可能不存在；这种情况下跳过，
避免源料未粘贴时影响其它项目测试。
"""

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


SPEC_PATH = Path(__file__).resolve().parents[1] / "specs" / "qoderwork.py"
if not SPEC_PATH.exists():
    pytest.skip("local QoderWork spec source is not present", allow_module_level=True)

_spec = importlib.util.spec_from_file_location("qoderwork_spec_under_test", SPEC_PATH)
assert _spec and _spec.loader
qoderwork = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qoderwork)


def test_qoder_encoding_round_trips_upstream_vectors():
    cases = [
        b"",
        b"a",
        b"hi",
        b"hello world",
        b'{"model":"qmodel_preview","stream":true}',
        bytes(range(16)),
        "中文 UTF-8 内容 ✓".encode(),
    ]
    for raw in cases:
        encoded = qoderwork._qoder_encode(raw)
        assert qoderwork._qoder_decode(encoded) == raw
        assert "=" not in encoded
        assert all(char in qoderwork.QODER_B64_ALPHABET or char == "$" for char in encoded)


def test_qoder_encoding_known_vectors():
    assert qoderwork._qoder_encode(b"hi") == "$HzP"
    assert qoderwork._qoder_encode(bytes(range(16))) == "R_wlRf$$d&NBopnG__To_fpg"


def test_begin_device_flow_builds_qoder_pkce_url_without_network():
    result = asyncio.run(qoderwork.QoderWorkChannel.begin_device_flow(SimpleNamespace()))
    assert result["task_type"] == "device_code"
    assert result["interval"] >= 1
    assert result["poll_params"]["nonce"]
    assert len(result["poll_params"]["verifier"]) == 64

    query = parse_qs(urlsplit(result["auth_url"]).query)
    assert query["challenge_method"] == ["S256"]
    assert query["client_id"] == [qoderwork.QODER_CLIENT_ID]
    assert query["redirect_uri"] == [qoderwork.QODER_REDIRECT_URI]
    assert len(query["challenge"][0]) == 43
    assert query["nonce"] == [result["poll_params"]["nonce"]]


def test_realm_detected_from_channel_address():
    """realm 由渠道地址判定：.com.cn → 国内（带 redirect_uri/带横线 nonce），
    qoder.sh → 国际（不带 redirect_uri/32 位 hex nonce）。"""
    cn = qoderwork.QoderWorkChannel.begin_device_flow
    # 国内：base_url 含 qoder.com.cn
    r_cn = asyncio.run(cn(SimpleNamespace(base_url="https://gateway.qoder.com.cn")))
    q_cn = parse_qs(urlsplit(r_cn["auth_url"]).query)
    assert q_cn["redirect_uri"] == [qoderwork.QODER_REDIRECT_URI]
    assert q_cn["client_id"] == [qoderwork.QODER_CLIENT_ID]
    assert "-" in r_cn["poll_params"]["nonce"]          # 带横线 uuid
    assert r_cn["poll_params"]["realm"] == "cn"

    # 国际：base_url 含 qoder.sh
    r_intl = asyncio.run(cn(SimpleNamespace(base_url="https://api1.qoder.sh")))
    q_intl = parse_qs(urlsplit(r_intl["auth_url"]).query)
    assert "redirect_uri" not in q_intl                    # 国际版不带 redirect_uri
    assert q_intl["client_id"] == [qoderwork.QODER_REALMS["intl"]["client_id"]]
    assert "-" not in r_intl["poll_params"]["nonce"]       # 32 位 hex
    assert r_intl["poll_params"]["realm"] == "intl"
    # openapi 基址随 realm 切
    assert qoderwork._openapi_base(SimpleNamespace(base_url="https://api1.qoder.sh")) \
        == qoderwork.QODER_REALMS["intl"]["openapi"]
    assert qoderwork._openapi_base(SimpleNamespace(base_url="https://gateway.qoder.com.cn")) \
        == qoderwork.QODER_OPENAPI


class _CosyProvider:
    access_token = "dt-test"
    refresh_token = "drt-test"
    password = "drt-test"
    uid = "uid-test"
    nickname = "Qoder Tester"
    machine_id = "machine-test"
    machine_token = "machine-token-test"
    machine_type = "machine-type-test"


def test_cosy_headers_have_expected_signature_shape_and_fields():
    provider = _CosyProvider()
    url = "https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1"
    headers = qoderwork._cosy_headers(provider, body="", url=url, sse=False)

    assert headers["authorization"].startswith("Bearer COSY.")
    payload_b64, signature = headers["authorization"][len("Bearer COSY."):].rsplit(".", 1)
    payload = json.loads(base64.b64decode(payload_b64))
    assert payload["cosyVersion"] == qoderwork.COSY_VERSION
    assert len(signature) == 32
    assert headers["cosy-user"] == provider.uid
    assert headers["accept-encoding"] == "identity"
    assert "cache-control" not in headers

    chat_headers = qoderwork._cosy_headers(
        provider,
        body="encoded-body",
        url="https://gateway.qoder.com.cn/algo/chat",
        sse=True,
        model_key="qmodel_preview",
    )
    assert chat_headers["cache-control"] == "no-cache"
    assert chat_headers["x-model-key"] == "qmodel_preview"
    assert chat_headers["x-model-source"] == "system"


def test_agent_body_preserves_messages_tools_and_latest_prompt():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "最后一条问题"},
    ]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    body = qoderwork._build_agent_body("qmodel_preview", messages, tools)
    assert body["messages"] == messages
    assert body["tools"] == tools
    assert body["chat_context"]["text"]["text"] == "最后一条问题"
    # 没要思考 → 关思考（平台约定：关闭 = 省略 reasoning_effort）
    assert body["model_config"] == {"key": "qmodel_preview", "is_reasoning": False}
    assert "source" not in body["model_config"]
    assert "parameters" not in body
    assert body["business"]["begin_at"] > 0


def test_agent_body_reasoning_effort_opens_thinking():
    messages = [{"role": "user", "content": "hi"}]

    # 要了档位 → is_reasoning=true + source=system + parameters
    body = qoderwork._build_agent_body(
        "qmodel_preview", messages, None, reasoning_effort="Medium"
    )
    assert body["model_config"] == {
        "key": "qmodel_preview", "is_reasoning": True, "source": "system",
    }
    assert body["chat_context"]["extra"]["modelConfig"] == body["model_config"]
    assert body["parameters"] == {"enable_thinking": True, "reasoning_effort": "medium"}

    # 框架值域的 high / max 上游不认，压到 xhigh（降档会静默变成 medium）
    for effort in ("high", "max", "MAX"):
        body = qoderwork._build_agent_body(
            "qmodel_preview", messages, None, reasoning_effort=effort
        )
        assert body["parameters"]["reasoning_effort"] == "xhigh", effort
        assert body["model_config"]["is_reasoning"] is True


def test_agent_body_disabled_or_invalid_effort_keeps_thinking_off():
    messages = [{"role": "user", "content": "hi"}]

    # none / minimal / 未给 / 空串 → 关思考，不注入 parameters
    for effort in (None, "", "none", "minimal", "bogus"):
        body = qoderwork._build_agent_body(
            "qmodel_preview", messages, None, reasoning_effort=effort
        )
        assert body["model_config"] == {"key": "qmodel_preview", "is_reasoning": False}, effort
        assert "parameters" not in body, effort

    # model_key 为空时即使要了档位也不开（上游没有 key 无从谈起）
    body = qoderwork._build_agent_body("", messages, None, reasoning_effort="low")
    assert body["model_config"] == {"key": "", "is_reasoning": False}
    assert "parameters" not in body


def test_nested_sse_emits_reasoning_tools_usage_finish_and_done():
    inner = {
        "choices": [{
            "delta": {
                "content": "hello",
                "reasoning_content": "thinking",
                "tool_calls": [{"index": 0, "id": "call-1", "type": "function"}],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    event = "data:" + json.dumps({"body": json.dumps(inner)}) + "\n\n"
    frames = qoderwork._iter_nested_chunk_frames(event)
    assert {"content": "hello", "thinking": "thinking", "tool_calls": inner["choices"][0]["delta"]["tool_calls"]} in frames
    assert {"usage": inner["usage"]} in frames
    assert {"finish_reason": "tool_calls"} in frames

    done = 'data:' + json.dumps({"body": "[DONE]"}) + "\n\n"
    assert {"done": True} in qoderwork._iter_nested_chunk_frames(done)


def test_nested_sse_drops_empty_content_keys():
    """上游 delta 同时带 content/reasoning_content，空串键不得进帧（防块类型误判）。"""
    # 纯思考帧：content 是空串，只该出 thinking
    inner = {"choices": [{"delta": {"content": "", "reasoning_content": "想"}}]}
    event = "data:" + json.dumps({"body": json.dumps(inner)}) + "\n\n"
    frames = qoderwork._iter_nested_chunk_frames(event)
    assert frames == [{"thinking": "想"}]

    # 纯正文帧：reasoning_content 是空串，只该出 content
    inner = {"choices": [{"delta": {"content": "答", "reasoning_content": ""}}]}
    event = "data:" + json.dumps({"body": json.dumps(inner)}) + "\n\n"
    frames = qoderwork._iter_nested_chunk_frames(event)
    assert frames == [{"content": "答"}]

    # 两个键都空 → 不产帧
    inner = {"choices": [{"delta": {"content": "", "reasoning_content": ""}}]}
    event = "data:" + json.dumps({"body": json.dumps(inner)}) + "\n\n"
    assert qoderwork._iter_nested_chunk_frames(event) == []


def test_model_name_normalization_and_static_mapping():
    assert qoderwork._normalize_model_name(" Qwen3.8_Max--Preview ") == "qwen3.8-max-preview"
    assert qoderwork._resolve_model_key("qwen3.7-max") == "qmodel_latest"
    qoderwork._DYNAMIC_MODEL_MAP["custom-model"] = "upstream-custom"
    assert qoderwork._resolve_model_key("CUSTOM-MODEL") == "upstream-custom"


def test_qoder_channel_declares_login_refresh_and_readonly_status():
    channel = qoderwork.QoderWorkChannel
    schema = channel.account_schema()
    assert channel.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel.SCHEDULED_REFRESH is True
    assert channel.APPLY_CLIENT_PRESET is False
    assert schema["auth_start"] == {
        "enabled": True,
        "label": "网页登录授权",
        "mode": "device_code",
        "description": "打开 Qoder 授权页完成登录，服务端自动轮询并回填 dt-/drt-。",
    }
    fields = {field["key"]: field for field in schema["fields"]}
    assert fields["quota_remaining"]["readonly"] is True
    assert fields["last_checkin"]["readonly"] is True
    assert "machine_token" in channel.ACCOUNT_FIELDS


class _FakeQoder:
    """_daily_checkin / _sync_quota 需要读写的账号字段最小载体。"""

    access_token = "dt-test"
    proxy = None
    last_checkin_date = ""
    last_checkin = ""
    machine_id = "m"
    machine_token = "mt"
    machine_type = "mty"
    plan_name = ""
    pro_trial_status = ""
    quota_total = 0
    quota_used = 0
    quota_remaining = 0
    quota_exceeded = False
    quota_text = ""
    credit_total = 0
    checkin_reward_credits = 0
    checkin_streak_days = 0
    checkin_total_days = 0


def _patch_openapi(monkeypatch, responses):
    """用一组 (status, dict, text) 依次应答 _openapi_response 的每次调用。"""
    calls = iter(responses)

    async def _fake(p, method, path, *, token="", json_body=None, params=None):
        del p, method, path, token, json_body, params
        return next(calls)

    monkeypatch.setattr(qoderwork, "_openapi_response", _fake)


def _patch_desktop(monkeypatch, responses):
    """用一组 (status, dict, text) 依次应答 _desktop_openapi_response（活动平台）。"""
    calls = iter(responses)

    async def _fake(p, method, path, *, token="", json_body=None):
        del p, method, path, token, json_body
        return next(calls)

    monkeypatch.setattr(qoderwork, "_desktop_openapi_response", _fake)


def _campaign(cid="cid-1", status="CLAIMABLE", amount=100, action="CLAIM_BENEFIT", kind="CREDITS"):
    return {"campaignId": cid, "campaignKey": "act-test", "actionType": action,
            "claimStatus": status, "benefit": {"kind": kind, "amount": amount}}


def test_checkin_claims_daily_campaign_credits(monkeypatch):
    """活动平台 CLAIMABLE：POST claim 成功后写「今日已签到 +100 Credits」。"""
    p = _FakeQoder()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": True,
               "campaigns": [_campaign()]}, "{}"),
        (200, {"status": "CLAIMED", "replayed": False, "grantId": "g1",
               "benefit": {"kind": "CREDITS", "amount": 100}}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert p.last_checkin == "今日已签到 +100 Credits"
    assert p.last_checkin_date == qoderwork._today_str()
    assert p.checkin_reward_credits == 100
    assert p.credit_total == 100               # 累计积分随新领累加
    assert p.checkin_streak_days == 1          # 首签 → 连续 1 天
    assert p.checkin_total_days == 1


def test_checkin_reward_and_cumulative_tracked_across_days(monkeypatch):
    """次日再领：今日奖励恒为当日额度，累计与连续天数正确累加。"""
    p = _FakeQoder()
    p.credit_total = 100
    p.checkin_streak_days = 1
    p.checkin_total_days = 1
    p.last_checkin_date = qoderwork._yesterday_str()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": True,
               "campaigns": [_campaign()]}, "{}"),
        (200, {"status": "CLAIMED", "replayed": False,
               "benefit": {"kind": "CREDITS", "amount": 100}}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert p.checkin_reward_credits == 100
    assert p.credit_total == 200
    assert p.checkin_streak_days == 2
    assert p.checkin_total_days == 2


def test_checkin_backfills_cumulative_when_history_missing(monkeypatch):
    """历史运行只写了 last_checkin_date 没写累计/连续 → 首次重跑按今日奖励自愈补记。"""
    p = _FakeQoder()
    p.credit_total = 0
    p.checkin_streak_days = 0
    p.last_checkin_date = qoderwork._today_str()   # 今天已签过，但累计还是 0
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": True,
               "campaigns": [_campaign(status="CLAIMED")]}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert p.credit_total == 100                # 累计至少覆盖今日额度
    assert p.checkin_reward_credits == 100
    assert p.checkin_streak_days == 1           # streak 未记 → 补 1（明天才能正确 +1）
    assert p.checkin_total_days == 0            # 历史天数不可考，不自造


def test_checkin_rerun_same_day_does_not_double_count(monkeypatch):
    """同一天重跑：累计/连续/总天数都不重复累加。"""
    p = _FakeQoder()
    p.credit_total = 200
    p.checkin_streak_days = 2
    p.checkin_total_days = 2
    p.last_checkin_date = qoderwork._today_str()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": True,
               "campaigns": [_campaign(status="CLAIMED")]}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert p.credit_total == 200
    assert p.checkin_reward_credits == 100
    assert p.checkin_streak_days == 2
    assert p.checkin_total_days == 2


def test_checkin_already_claimed_campaign(monkeypatch):
    """活动平台 CLAIMED：如实记「今日已签（+100 Credits…）」，不重复 POST。"""
    p = _FakeQoder()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": True,
               "campaigns": [_campaign(status="CLAIMED")]}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert p.last_checkin == "今日已签（+100 Credits，明日 10:00 后再来）"
    assert p.last_checkin_date == qoderwork._today_str()


def test_derived_machine_identity_is_stable_per_uid_and_no_exe():
    """机器身份纯代码派生：同 uid 稳定、跨 uid 不同；字段非空且定长（无需 runtime-info）。"""
    a1 = qoderwork._derived_machine_identity("uid-a")
    a2 = qoderwork._derived_machine_identity("uid-a")
    b = qoderwork._derived_machine_identity("uid-b")
    assert a1 == a2, "同 uid 必须派生出同一份身份（稳定性）"
    assert a1 != b, "不同 uid 必须派生出不同身份"
    assert a1["machineToken"] and a1["machineType"] and a1["machineCode"], "三个头必须非空"
    assert len(a1["machineToken"]) == 88 and len(a1["machineType"]) == 18 and len(a1["machineCode"]) == 18
    # 纯代码，绝不 spawn 外部 exe
    assert not hasattr(qoderwork, "_find_runtime_info"), "不得再依赖官方客户端的 runtime-info 组件"
    assert "subprocess" not in Path(qoderwork.__file__).read_text(encoding="utf-8")


def test_campaign_headers_include_derived_identity():
    """活动平台出站头带 cosy-clienttype + 派生的 machinetoken/type/code。"""
    p = _FakeQoder()
    p.uid = "uid-xyz"
    ident = qoderwork._campaign_identity(p)
    headers = qoderwork._desktop_headers("dt-test", ident)
    assert headers["cosy-clienttype"] == "10"
    assert headers["cosy-machinetoken"] == ident["machineToken"]
    assert headers["cosy-machinetype"] == ident["machineType"]
    assert headers["cosy-machinecode"] == ident["machineCode"]
    assert "cosy-machineid" not in headers  # 派生身份不发 machineid（发错不如不发）
    # QODER_UMID=0 时退回「不带机器头」（国内站够用）
    os.environ["QODER_UMID"] = "0"
    try:
        assert qoderwork._campaign_identity(p) == {}
    finally:
        os.environ.pop("QODER_UMID", None)


def test_checkin_same_person_blocked_not_marked_signed(monkeypatch):
    """服务端按人去重：同机器其他账号本轮已领 -> BLOCKED，不盖「今日已签」。"""
    p = _FakeQoder()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": True,
               "campaigns": [_campaign()]}, "{}"),
        (200, {"status": "BLOCKED",
               "failureCode": "SAME_PERSON_ALREADY_CLAIMED",
               "benefit": {"kind": "CREDITS", "amount": 100}}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert "同人已领" in p.last_checkin
    assert p.last_checkin_date == ""          # 不误标「今日已签」，明日仍会重试


def test_checkin_empty_campaign_list_not_marked_signed(monkeypatch):
    """活动平台返回空列表（缺 cosy-clienttype 或不在定向内）：不误标「今日已签」。"""
    p = _FakeQoder()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": False, "claimable": False, "campaigns": []}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert "未下发活动" in p.last_checkin
    assert p.last_checkin_date == ""


def test_checkin_view_details_campaign_not_treated_as_claim(monkeypatch):
    """VIEW_DETAILS 类活动没有奖励可领，不算签到（列表非空但无可领项）。"""
    p = _FakeQoder()
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "claimable": False,
               "campaigns": [_campaign(action="VIEW_DETAILS")]}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert "暂无可领取活动" in p.last_checkin
    assert p.last_checkin_date == ""


def test_checkin_falls_back_to_legacy_when_campaigns_unavailable(monkeypatch):
    """活动平台 404（本区域无该接口）时回退旧 sash 签到：DISABLED 如实记录。"""
    p = _FakeQoder()
    _patch_desktop(monkeypatch, [
        (404, {}, '{"errorCode":"NotFound"}'),
    ])
    _patch_openapi(monkeypatch, [
        (200, {"campaignKey": "cn_daily_check_in_legacy", "status": "DISABLED",
               "rewardCredits": 100, "currentStreakDays": 0,
               "totalClaimDays": 0, "totalRewardCredits": 0}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert p.last_checkin == "签到未开放（DISABLED）"
    assert p.last_checkin_date == ""
    assert p.checkin_state == "DISABLED"
    assert p.checkin_streak_days == 0         # streak 保持 0，不塌成 None


def test_legacy_checkin_claimable_claims_then_marks(monkeypatch):
    """旧接口兜底路径：CLAIMABLE 时发起 claim，成功后写「今日已签到 +N 积分」。"""
    p = _FakeQoder()
    _patch_openapi(monkeypatch, [
        (200, {"status": "CLAIMABLE", "currentStreakDays": 1}, "{}"),
        (200, {"data": {"result": "CLAIMED", "rewardCredits": 50}}, "{}"),
    ])
    asyncio.run(qoderwork._legacy_daily_checkin(p))
    assert p.last_checkin == "今日已签到 +50 积分"
    assert p.last_checkin_date == qoderwork._today_str()


def test_legacy_checkin_stale_question_mark_reprobed_today(monkeypatch):
    """旧版本留下的「今日已签（连续 ? 天）」即使带着当日日期也必须重查改写。"""
    p = _FakeQoder()
    p.last_checkin_date = qoderwork._today_str()
    p.last_checkin = "今日已签（连续 ? 天）"
    _patch_desktop(monkeypatch, [
        (200, {"showCampaign": True, "campaigns": [_campaign(status="CLAIMED")]}, "{}"),
    ])
    asyncio.run(qoderwork._daily_checkin(p))
    assert "今日已签" in p.last_checkin
    assert p.last_checkin_date == qoderwork._today_str()


def test_opt_int_keeps_none_for_unknown_but_coerces_zero():
    assert qoderwork._opt_int("") is None
    assert qoderwork._opt_int(None) is None
    assert qoderwork._opt_int(0) == 0           # 0 是有效值，不能塌成 None
    assert qoderwork._opt_int(3.0) == 3
    assert qoderwork._opt_int("x") is None


def test_refresh_auth_keeps_going_when_only_learning_expiry(monkeypatch):
    """学过期时间的刷新失败（drt 抖动/已失效）不该中断签到+积分同步。"""
    calls = []

    async def _boom(p, *, force=False, persist=True, learn_expiry=False):  # noqa: ARG001
        calls.append(learn_expiry)
        raise qoderwork.HTTPException(status_code=401, detail="drt 失效")

    async def _fake_sync(p):
        p.quota_text = "剩余 100 / 总 100（已用 0）"
        p.quota_remaining = 100
        p.quota_total = 100
        p.quota_used = 0
        p.quota_exceeded = False

    async def _noop(*_args, **_kwargs):
        return {}

    for name in ("_claim_pro_trial", "_daily_checkin", "_sync_plan"):
        monkeypatch.setattr(qoderwork, name, _noop)
    monkeypatch.setattr(qoderwork, "_sync_quota", _fake_sync)
    monkeypatch.setattr(qoderwork, "_fetch_userinfo", _noop)
    monkeypatch.setattr(qoderwork, "_ensure_dt", _boom)
    monkeypatch.setattr(qoderwork, "_ensure_machine_fields", lambda _p: False)

    p = _FakeQoder()
    p.access_token = "dt-old"          # 已知 dt，但过期时间未知
    p.access_token_expires_at_ms = 0
    p.uid = "uid"
    p.nickname = "n"

    snapshot = asyncio.run(qoderwork.QoderWorkChannel.refresh_auth(p, {"quota_remaining": 50}))
    assert calls == [True]                       # 确实带 learn_expiry 请求
    assert snapshot["quota_text"] == "剩余 100 / 总 100（已用 0）"  # 同步未被中断


def test_format_quota_text_covers_zero_and_partial():
    assert qoderwork._format_quota_text(0, 0, 0, True) == "无可用积分（Free 套餐不含积分）"
    assert qoderwork._format_quota_text(0, 0, 0, False) == "无可用积分"
    assert qoderwork._format_quota_text(300, 150, 150, False) == "剩余 150 / 总 300（已用 150）"
    assert qoderwork._format_quota_text(300, 300, 0, True) == "积分已用尽（总 300）"


def test_ensure_dt_learns_expiry_when_requested(monkeypatch):
    """learn_expiry=True（管理端动作）：dt 过期时间未知（毫秒=0）时刷新一次把
    expires_in 拉回来；默认（聊天/模型热路径）不因「未知」去打 refresh 端点。"""
    import time as _time

    def _mk_provider():
        return SimpleNamespace(
            username="u", access_token="dt-old", refresh_token="drt-old",
            access_token_expires_at_ms=0, password="drt-old", proxy=None,
        )

    async def _fake_refresh_dt(p, refresh_token):  # noqa: ARG001
        p.access_token = "dt-new"
        p.access_token_expires_at_ms = int(_time.time() * 1000) + 86400_000
        return True

    monkeypatch.setattr(qoderwork, "_refresh_dt", _fake_refresh_dt)
    monkeypatch.setattr(qoderwork, "_ensure_machine_fields", lambda _p: False)

    # 热路径（默认）：未知过期时间不触发刷新。
    hot = _mk_provider()
    assert asyncio.run(qoderwork._ensure_dt(hot, persist=False)) is False

    # 管理端动作：学到真实过期时间，第二次不再刷新。
    p = _mk_provider()
    assert asyncio.run(qoderwork._ensure_dt(p, persist=False, learn_expiry=True)) is True
    assert p.access_token == "dt-new"
    assert asyncio.run(qoderwork._ensure_dt(p, persist=False, learn_expiry=True)) is False


def test_envelope_error_403_with_business_code_raises_instead_of_swallowing():
    """回归：上游把权益错误塞进 HTTP 200 的 SSE 信封（statusCodeValue=403 +
    body {"code":"112",...}）。旧实现只读 error/body，把它当普通帧丢掉 → 干等
    60s 后被对端掐断 → 报成传输层 ClientPayloadError/502 → 冻结整个渠道，
    真实原因（要付费）全丢。这里必须产帧抛错，且状态码取信封的 HTTP 状态 403
    （不是业务码 112，否则 400~600 之外会被框架退化成 502）。"""
    # 与现网 ClickHouse router_response_body 抓到的字节形态一致。
    envelope = {
        "headers": {"Content-Type": ["application/json"]},
        "body": json.dumps({
            "code": "112",
            "message": json.dumps({"pricingUrl": "https://qoder.com.cn/pricing?client=qoder"}),
        }),
        "statusCodeValue": 403,
        "statusCode": "FORBIDDEN",
    }
    event = "data:" + json.dumps(envelope, ensure_ascii=False) + "\n\n"
    frames = qoderwork._iter_nested_chunk_frames(event)

    assert len(frames) == 1, "错误帧必须产出（旧实现产出空列表 = 静默吞掉）"
    err = frames[0]["_upstream_error"]
    assert err["code"] == 403, f"状态码应取信封 HTTP 状态，而非业务码 112：{err}"
    assert "code=112" in err["message"]
    assert "qoder.com.cn/pricing" in err["message"]

    # 走框架抛出路径：status 403 是明确的 4xx，detail 保留真实原因。
    from providers.custom import CustomProvider

    with pytest.raises(Exception) as excinfo:
        CustomProvider._raise_upstream_error(err)
    assert getattr(excinfo.value, "status_code", None) == 403
    assert "code=112" in str(getattr(excinfo.value, "detail", ""))


def test_envelope_success_frames_still_parse_normally():
    """statusCodeValue=200（或缺省）不改动原有解析行为；非 200 必须报错。"""
    inner = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
    for status_value in (200, "200", None):
        envelope = {"body": json.dumps(inner), "statusCodeValue": status_value}
        event = "data:" + json.dumps(envelope) + "\n\n"
        frames = qoderwork._iter_nested_chunk_frames(event)
        assert {"content": "ok"} in frames, f"status={status_value} 不该破坏正常帧"

    envelope = {"body": json.dumps(inner), "statusCodeValue": 403}
    event = "data:" + json.dumps(envelope) + "\n\n"
    frames = qoderwork._iter_nested_chunk_frames(event)
    assert any("_upstream_error" in frame for frame in frames), "403 必须报错"
    assert not any("content" in frame for frame in frames), "错误帧不该再吐正文"


def test_static_model_map_covers_routed_names_and_stays_cnr_catalog_aligned():
    """静态表是动态映射为空时（进程重启后 fetch_models 未跑）的唯一屏障，
    缺项 = 上游 key 原样透传 = 静默不出帧。"""
    # 现网实际路由过的名字
    assert qoderwork._resolve_model_key("qwen3.8-flash") == "qfmodel"
    assert qoderwork._resolve_model_key("qwen3.8-max") == "qmodel_38max"
    # 官方 CN 目录（qoder2api-hub qoder_catalog_cn.json）里的每个上游 key
    # 都必须有静态名字能解析到，否则该模型在动态映射为空时必然透传失败。
    official_keys = {
        "auto", "qmodel_38max", "qfmodel", "qmodel_latest", "qmodel", "q37fmodel",
        "dmodel", "dfmodel", "gmodel", "gfmodel", "gm51model",
        "kmodel_latest", "kmodel", "mmodel",
    }
    covered = set(qoderwork._STATIC_MODEL_MAP.values())
    missing = official_keys - covered
    assert not missing, f"官方 CN 目录这些 key 无静态兜底：{sorted(missing)}"


def test_model_display_fields_uses_real_cosy_fields():
    """COSY model/list 无 description 字段，「其他信息」列只能用响应里真实存在的字段拼：
    倍率 = price_factor（此前只在 metadata 里，行顶层没有 → 前端读不到）、
    说明 = context_config 最大档 + is_reasoning/is_vl + is_free/is_new 标记。"""
    item = {
        "key": "ultimate", "display_name": "Ultimate", "enable": True,
        "is_vl": True, "is_reasoning": True, "is_free": False, "is_new": False,
        "context_config": {"1M": {"token_count": 1000000},
                           "200K": {"token_count": 200000, "is_default": True},
                           "400K": {"token_count": 400000}},
        "max_input_tokens": 1000000, "price_factor": 2.0,
    }
    max_ctx, _default = qoderwork._context_metadata(item)
    out = qoderwork._model_display_fields(item, item["display_name"], max_ctx)
    assert out["cost_multiplier"] == 2.0
    assert out["_model_name"] == "Ultimate"
    # 上下文取最大档（不是默认档），能力位思考+视觉都在
    assert out["_model_desc"] == "上下文 1M 思考·视觉"

    # 无思考、非免费新品：标记不硬塞；倍率 0.3 照实
    plain = {"key": "efficient", "display_name": "Efficient", "is_vl": True,
             "is_reasoning": False, "max_input_tokens": 200000, "price_factor": 0.3}
    max_ctx2, _ = qoderwork._context_metadata(plain)
    out2 = qoderwork._model_display_fields(plain, "Efficient", max_ctx2)
    assert out2["_model_desc"] == "上下文 200K 视觉"
    assert out2["cost_multiplier"] == 0.3

    # 免费 + 新品标记（实测 Cantus/Sonus 带 is_free/is_new）
    free_new = {"key": "cmodel", "display_name": "Cantus", "is_free": True, "is_new": True,
                "max_input_tokens": 200000, "price_factor": 0.0}
    mc3, _ = qoderwork._context_metadata(free_new)
    out3 = qoderwork._model_display_fields(free_new, "Cantus", mc3)
    assert "免费" in out3["_model_desc"] and "新品" in out3["_model_desc"]
    # 0 是合法倍率（免费档），不能当 falsy 丢掉
    assert out3["cost_multiplier"] == 0.0

    # price_factor 缺失 → 不塞倍率键（列留空，不臆造 1.0）
    bare = qoderwork._model_display_fields({"display_name": "X"}, "X", 0)
    assert "cost_multiplier" not in bare


def test_qoderwork_declares_model_info_columns():
    """account_schema 必须声明 名称+说明+倍率 三列，管理端模型 tab 才渲染「其他信息」。"""
    schema = qoderwork.QoderWorkChannel.account_schema()
    cols = {c["key"]: c for c in schema.get("model_info_columns", [])}
    assert {"_model_name", "_model_desc", "cost_multiplier"} <= set(cols)
    assert cols["cost_multiplier"]["format"] == "multiplier"
