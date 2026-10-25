"""MiniMax Code 代码渠道 spec（specs/minimax_code_channel.py）的适配器契约。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
锁的是 2026-10-02 逆向 + 真机实测定型的行为（官方 @mavis/oauth-core + account-identity.js）：

1. spec 能被 code_loader 加载（普通类 + 钩子齐全，误继承基类必报错）；
2. 类级开关：SUPPORTS_TOKEN_AUTO_REFRESH / SCHEDULED_REFRESH / SUPPORTS_MULTI_MESSAGES
   = True，REQUIRES_BASE_URL / APPLY_CLIENT_PRESET = False；presence-triggered 方法
   （begin_device_flow / poll_device_flow / refresh_account_auth）都注入到适配器；
3. 设备码登录：begin_device_flow 申请码（PKCE + 固定 client_id/scope/audience），
   poll_device_flow 认 400+authorization_pending 与 200+status=pending 两条 pending，
   成功回填账号字段；
4. refresh_auth：refresh_token grant，失败 400/502（绝不 401/403）；
5. headers：删 x-api-key、挂 Bearer、补 anthropic-version；build_url = {base}/messages。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "minimax_code_channel.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/minimax_code_channel.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("minimax-code-test", _SOURCE)
    yield cls
    invalidate_cache("minimax-code-test")


def _run(coro):
    return asyncio.run(coro)


# ==================== 假 HTTP 层（按 URL 子串路由） ====================


class _FakeResponse:
    def __init__(self, status=200, text="{}"):
        self.status = status
        self._text = text

    async def text(self):
        return self._text


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _fake_session(routes: dict, captured: dict):
    class FakeSession:
        def get(self, url, headers=None, **_kw):
            captured["last_get"] = url
            captured["last_get_headers"] = dict(headers or {})
            for key, resp in routes.items():
                if key in url:
                    return _Ctx(resp() if callable(resp) else resp)
            return _Ctx(_FakeResponse(404, "{}"))

        def post(self, url, json=None, data=None, headers=None, **_kw):  # noqa: A002
            captured["last_post"] = url
            captured["last_post_headers"] = dict(headers or {})
            # data 可能是 JSON 字符串（渲染层签名请求发原始 body），别 dict() 它
            captured["last_form"] = data if isinstance(data, str) else dict(data or {})
            # 按 URL 记录 form：refresh_auth 会连续打 oauth2/token + get_membership_info，
            # 两个 POST 的 data 类型不同（dict form vs 字符串 body），last_form 会互相覆盖。
            captured.setdefault("posts_by_url", {})
            # 用 URL 子串做键，调用方按端点名定位
            for key in routes:
                if key in url:
                    captured["posts_by_url"][key] = captured["last_form"]
                    break
            for key, resp in routes.items():
                if key in url:
                    return _Ctx(resp() if callable(resp) else resp)
            return _Ctx(_FakeResponse(404, "{}"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    return FakeSession()


def _device_code_body(**over):
    body = {
        "device_code": "dc_1", "user_code": "6Q8R-RQM9",
        "verification_uri": "https://account.minimax.cn/oauth-authorize",
        "verification_uri_complete": "https://account.minimax.cn/oauth-authorize?user_code=6Q8R-RQM9",
        "expires_in": 300, "interval": 3,
    }
    body.update(over)
    return json.dumps(body)


def _token_body(**over):
    body = {"access_token": "mmoat_new", "refresh_token": "mmort_new",
            "token_type": "Bearer", "expires_in": 3600, "scope": "agent.default"}
    body.update(over)
    return json.dumps(body)


# ==================== 加载与钩子面 ====================


def test_spec_declares_expected_hooks(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {"account_schema", "begin_device_flow", "poll_device_flow", "refresh_auth",
            "is_init", "init_auth", "check_auth", "fetch_models", "base_url",
            "build_url", "headers", "payload", "stream_chat", "non_stream_chat"}.issubset(hooks)
    # presence-triggered 方法注入到适配器（admin 靠 getattr / vars 探测能力）
    for name in ("begin_device_flow", "poll_device_flow", "refresh_account_auth",
                 "perform_account_action"):
        assert name in vars(channel_cls), name


def test_class_flags(channel_cls):
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is True
    assert channel_cls.REQUIRES_BASE_URL is False
    assert channel_cls.APPLY_CLIENT_PRESET is False


def test_account_schema_poll_completion(channel_cls):
    schema = channel_cls.account_schema()
    assert schema["provider_name"] == "minimax-code-test"
    assert schema["display_name"] == "MiniMax Code"
    assert schema["auth_start"]["enabled"] is True
    assert schema["auth_start"]["mode"] == "device_code"
    # 标准设备码流：纯轮询完成（不是 loopback）
    assert schema["auth_start"]["completion"] == "poll"
    assert "device_code" in schema["add_methods"]
    keys = {f["key"] for f in schema["fields"]}
    assert {"access_token", "refresh_token", "region"}.issubset(keys)


# ==================== 账号动作：签到 + 额度 ====================


def test_account_schema_declares_checkin_and_sync_quota(channel_cls):
    schema = channel_cls.account_schema()
    actions = {a["id"]: a for a in schema["account_actions"]}
    assert {"checkin", "sync_quota"} <= set(actions)
    assert actions["checkin"]["display"]["badge_field"] == "last_checkin"
    assert actions["sync_quota"]["display"]["value_field"] == "quota_text"
    keys = {f["key"] for f in schema["fields"]}
    assert {"quota_text", "plan", "last_checkin"} <= keys
    assert "quota_text" in schema["metadata_badges"]


def test_perform_account_action_sync_quota(channel_cls, monkeypatch):
    """读积分余额（真机：主值在 op_credit_summary.total_remaining_amount），经适配器走 perform_account_action。"""
    captured: dict = {}
    routes = {"get_membership_info": _FakeResponse(200, json.dumps({
        "base_resp": {"status_code": 0, "status_msg": "success"},
        "plan_name": "", "has_token_plan": False, "expires_at": 0,
        "opcredit_balance": 0,   # 迁移后废弃字段，恒 0
        "op_credit_summary": {"total_remaining_amount": "1591.61",
                              "purchased_remaining_amount": "0",
                              "free_remaining_amount": "1591.61"},
        "subscription_type": "none",
    }))}
    p = channel_cls(username="u", password="", access_token="at-1", region="cn")
    monkeypatch.setattr(type(p), "_make_session",
                        lambda self, *a, **k: _fake_session(routes, captured))

    fields = _run(p.perform_account_action("sync_quota", {"access_token": "at-1"}, None))
    assert fields["quota_text"] == "积分 1591.61"
    assert fields["quota_balance"] == "1591.61"
    assert fields["plan"] == "无套餐"
    assert "get_membership_info" in captured["last_post"]
    # 签名头齐全（renderer 签名是 agent 域请求的硬要求）
    h = captured["last_post_headers"]
    assert h["Authorization"] == "Bearer at-1"
    assert len(h["yy"]) == 32 and len(h["x-signature"]) == 32
    assert h["x-timestamp"].isdigit()


def test_perform_account_action_sync_quota_no_token_is_400(channel_cls):
    from fastapi import HTTPException

    p = channel_cls(username="u", password="")
    with pytest.raises(HTTPException) as ei:
        _run(p.perform_account_action("sync_quota", {}, None))
    assert ei.value.status_code == 400


def test_perform_account_action_unknown_is_400(channel_cls):
    from fastapi import HTTPException

    p = channel_cls(username="u", password="", access_token="at-1")
    with pytest.raises(HTTPException) as ei:
        _run(p.perform_account_action("no_such_action", {"access_token": "at-1"}, None))
    assert ei.value.status_code == 400


def test_perform_account_action_checkin_idempotent_same_day(channel_cls, monkeypatch):
    """当日已签（last_checkin_status 含今日|ok）→ 跳过，不打上游。"""
    import time as _time
    captured: dict = {}
    p = channel_cls(username="u", password="", access_token="at-1", region="cn")
    monkeypatch.setattr(type(p), "_make_session",
                        lambda self, *a, **k: _fake_session({}, captured))

    today = _time.strftime("%Y-%m-%d", _time.localtime())
    fields = _run(p.perform_account_action(
        "checkin", {"access_token": "at-1", "last_checkin_status": f"{today}|ok|+20"}, None))
    assert fields == {}


# ==================== 设备码登录 ====================


def test_begin_device_flow(channel_cls, monkeypatch):
    captured: dict = {}
    routes = {"oauth2/device/code": _FakeResponse(200, _device_code_body())}
    p = channel_cls(username="u", password="", region="cn")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: _fake_session(routes, captured))

    flow = _run(p.begin_device_flow(None))
    assert flow["task_type"] == "device_code"
    assert flow["user_code"] == "6Q8R-RQM9"
    assert flow["poll_params"]["device_code"] == "dc_1"
    assert flow["poll_params"]["code_verifier"]
    # 申请表单固定参数（oauth-core contracts 逐字）
    form = captured["last_form"]
    assert captured["last_post"].endswith("/oauth2/device/code")
    assert form["client_id"] == "mcode-public"
    assert form["scope"] == "agent.default"
    assert form["audience"] == "agent-backend"
    assert form["code_challenge_method"] == "S256"
    # PKCE：challenge 必须是 verifier 的 S256
    verifier = flow["poll_params"]["code_verifier"]
    import base64
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert calc == form["code_challenge"]


def test_poll_pending_400_and_200(channel_cls, monkeypatch):
    """实测 400+authorization_pending 与 oauth-core 兼容的 200+status=pending 都算等待。"""
    for status, body in ((400, '{"error":"authorization_pending"}'),
                         (200, '{"status":"pending"}')):
        captured: dict = {}
        routes = {"oauth2/token": _FakeResponse(status, body)}
        p = channel_cls(username="u", password="", region="cn")
        monkeypatch.setattr(type(p), "_make_session",
                            lambda self, *a, **k: _fake_session(routes, captured))
        out = _run(p.poll_device_flow({"device_code": "dc", "code_verifier": "v",
                                       "poll_key": "device_code"}, None))
        assert out == {"status": "pending"}


def test_poll_authorized_returns_account_data(channel_cls, monkeypatch):
    captured: dict = {}
    routes = {"oauth2/token": _FakeResponse(200, _token_body())}
    p = channel_cls(username="tester", password="", region="cn")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: _fake_session(routes, captured))

    out = _run(p.poll_device_flow({"device_code": "dc", "code_verifier": "v",
                                   "poll_key": "device_code"}, None))
    assert out["status"] == "authorized"
    ad = out["account_data"]
    assert ad["access_token"] == "mmoat_new"
    assert ad["refresh_token"] == "mmort_new"
    assert captured["last_form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert captured["last_form"]["code_verifier"] == "v"


# ==================== 刷新 ====================


def test_refresh_auth_success(channel_cls, monkeypatch):
    captured: dict = {}
    routes = {
        "oauth2/token": _FakeResponse(200, _token_body()),
        # refresh_auth 现在顺带同步额度（UI「刷新账号信息」语义 = token+账号信息+额度）
        "get_membership_info": _FakeResponse(200, json.dumps({
            "base_resp": {"status_code": 0},
            "op_credit_summary": {"total_remaining_amount": "1591.61"},
            "plan_name": "", "has_token_plan": False,
        })),
    }
    p = channel_cls(username="u", password="", refresh_token="mmort_old", region="cn")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: _fake_session(routes, captured))

    fields = _run(p.refresh_account_auth({"refresh_token": "mmort_old"}, None))
    assert fields["access_token"] == "mmoat_new"
    # 顺带同步到的额度也回来了
    assert fields["quota_text"] == "积分 1591.61"
    # token 换发用的是 form（dict）——membership POST 用字符串 body，别被它盖掉
    token_form = captured["posts_by_url"]["oauth2/token"]
    assert token_form["grant_type"] == "refresh_token"
    assert token_form["client_id"] == "mcode-public"
    assert token_form["audience"] == "agent-backend"


def test_refresh_auth_dead_is_400(channel_cls, monkeypatch):
    from fastapi import HTTPException

    captured: dict = {}
    routes = {"oauth2/token": _FakeResponse(400, '{"error":"invalid_grant"}')}
    p = channel_cls(username="u", password="", refresh_token="dead", region="cn")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: _fake_session(routes, captured))
    with pytest.raises(HTTPException) as ei:
        _run(p.refresh_account_auth({"refresh_token": "dead"}, None))
    assert ei.value.status_code == 400


def test_refresh_auth_never_propagates_401_403(channel_cls, monkeypatch):
    """上游 401 也不能透传 401——凭据被拒按「死」映射成 400 叫重新授权。"""
    from fastapi import HTTPException

    captured: dict = {}
    routes = {"oauth2/token": _FakeResponse(401, '{"error":"invalid refresh token"}')}
    p = channel_cls(username="u", password="", refresh_token="rt", region="cn")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: _fake_session(routes, captured))
    with pytest.raises(HTTPException) as ei:
        _run(p.refresh_account_auth({"refresh_token": "rt"}, None))
    # 401/403 绝不透传（会被管理端全局拦截器当成管理员登录过期踢出控制台）
    assert ei.value.status_code == 400


# ==================== 请求塑形（走真适配器） ====================


def test_headers_bearer_and_no_x_api_key(channel_cls):
    """框架对 anthropic 协议默认发 x-api-key；上游只认 Bearer——钩子必须删掉它。"""
    p = channel_cls(username="u", password="", access_token="mmoat_live", region="cn")
    out = p._headers("anthropic")
    assert out["Authorization"] == "Bearer mmoat_live"
    assert "x-api-key" not in out
    assert out["anthropic-version"] == "2023-06-01"
    assert out["User-Agent"] == "MiniMaxCode"


def test_base_url_and_chat_url(channel_cls):
    p = channel_cls(username="u", password="", region="cn")
    assert p.base_url == "https://agent.minimax.cn/mavis/api/v1/llm/v1"
    # 出站地址必须 {base}/messages（不能是 /v1/v1/messages 双段）
    assert p._chat_url() == "https://agent.minimax.cn/mavis/api/v1/llm/v1/messages"


def test_base_url_intl_region(channel_cls):
    p = channel_cls(username="u", password="", region="intl")
    assert p.base_url == "https://agent.minimax.io/mavis/api/v1/llm/v1"


def test_base_url_configured_override(channel_cls):
    p = channel_cls(username="u", password="", base_url="https://proxy.example.com/mavis/api/v1/llm/v1")
    assert p._chat_url() == "https://proxy.example.com/mavis/api/v1/llm/v1/messages"


def test_fetch_models_builtin_snapshot_no_network(channel_cls, monkeypatch):
    def _boom(self, *a, **k):
        raise AssertionError("fetch_models 不应打网络（上游 /models 实测 503）")

    p = channel_cls(username="u", password="", access_token="t", region="cn")
    monkeypatch.setattr(type(p), "_make_session", _boom)
    models = _run(p.fetch_upstream_model_list())
    ids = {m["id"] for m in models}
    assert {"MiniMax-M2.7", "MiniMax-M3", "MiniMax-M3.1-Flash-Preview"} <= ids


def test_is_init_and_check_auth(channel_cls):
    assert channel_cls(username="u", password="").is_init() is False
    assert channel_cls(username="u", password="", access_token="t").is_init() is True
    assert channel_cls(username="u", password="", refresh_token="r").is_init() is True
    p = channel_cls(username="u", password="", access_token="t")
    assert _run(p.check_auth()) is True


# ==================== 聊天 401 兜底 ====================


def _spec_globals(channel_cls) -> dict:
    """spec 钩子的 ``__globals__``（= loader exec 的模块命名空间）；替换 _ensure_token 打这里。"""
    return channel_cls._spec_hooks["stream_chat"].__globals__


def _patch_stream_impl(monkeypatch, outputs):
    from providers import custom as custom_mod

    calls = {"n": 0}

    async def fake(self, model_id, messages, **kwargs):
        item = outputs[min(calls["n"], len(outputs) - 1)]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        for chunk in item:
            yield chunk

    monkeypatch.setattr(custom_mod.CustomProvider, "_do_stream_chat", fake)
    return calls


async def _drain(channel_cls, p):
    chunks = []
    async for chunk in channel_cls._spec_hooks["stream_chat"](
            p, "MiniMax-M2.7", [{"role": "user", "content": "hi"}]):
        chunks.append(chunk)
    return chunks


def test_stream_chat_refreshes_and_retries_on_401(channel_cls, monkeypatch):
    from fastapi import HTTPException

    refreshed: list = []

    async def _fake_ensure(p, force=False):
        if force:
            refreshed.append(True)
            p.access_token = "mmoat_new"
            return "mmoat_new"
        return getattr(p, "access_token", "")

    calls = _patch_stream_impl(monkeypatch, [
        HTTPException(status_code=401, detail='{"error":"invalid access token"}'),
        [{"content": "hello", "thinking": "", "tool_calls": [], "usage": None}],
    ])
    monkeypatch.setitem(_spec_globals(channel_cls), "_ensure_token", _fake_ensure)

    p = channel_cls(username="u", password="", access_token="mmoat_old", refresh_token="mmort_1")
    chunks = _run(_drain(channel_cls, p))
    assert calls["n"] == 2, "401 后应重试一次"
    assert refreshed == [True], "重试前必须强制刷新 token"
    assert chunks and chunks[0]["content"] == "hello"


def test_stream_chat_gives_up_after_one_retry_keeps_401(channel_cls, monkeypatch):
    from fastapi import HTTPException

    calls = _patch_stream_impl(monkeypatch, [
        HTTPException(status_code=401, detail='{"error":"invalid access token"}'),
    ])

    async def _fake_ensure(p, force=False):
        if not force:
            return getattr(p, "access_token", "")
        raise HTTPException(status_code=400, detail="refresh_token 已失效")

    monkeypatch.setitem(_spec_globals(channel_cls), "_ensure_token", _fake_ensure)
    p = channel_cls(username="u", password="", access_token="dead", refresh_token="dead")
    with pytest.raises(HTTPException) as ei:
        _run(_drain(channel_cls, p))
    assert ei.value.status_code == 401
    assert calls["n"] == 1


def test_stream_chat_does_not_retry_on_non_auth_error(channel_cls, monkeypatch):
    from fastapi import HTTPException

    calls = _patch_stream_impl(monkeypatch, [HTTPException(status_code=429, detail="rate limited")])
    p = channel_cls(username="u", password="", access_token="t", refresh_token="r")
    with pytest.raises(HTTPException) as ei:
        _run(_drain(channel_cls, p))
    assert ei.value.status_code == 429
    assert calls["n"] == 1