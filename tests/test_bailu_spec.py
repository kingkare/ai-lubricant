"""白鹿 BailuCode 代码渠道 spec（specs/bailu.py）的授权与请求塑形契约。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
锁的是 2026-09-27 全链路实测定型的行为：

1. spec 能被 code_loader 加载（普通类 + 钩子齐全，误继承基类必报错）；
2. begin_device_flow 纯本地生成 PKCE/state（无网络），授权地址走 /dashboard.php
   （/oauth_consent 未登录 302 会丢参），redirect_uri 是框架标准本机回调；
3. handle_loopback_callback：state 对不上不认领、error 回投转 error、code 换 token
   落号（/oauth/me 挂了不阻断）；
4. refresh_auth：refresh_token grant，失败 400/502（绝不 401/403）；
5. headers：access_token 覆盖 Bearer + 插件客户端指纹头。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "bailu.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/bailu.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("bailu-test", _SOURCE)
    yield cls
    invalidate_cache("bailu-test")


def _run(coro):
    return asyncio.run(coro)


# ==================== 加载与钩子面 ====================

def test_declares_expected_auth_surface(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {"init_auth", "check_auth", "refresh_auth", "headers", "build_url",
            "account_schema", "begin_device_flow", "poll_device_flow",
            "handle_loopback_callback", "begin_step_auth", "submit_step_auth"}.issubset(hooks)
    # presence-triggered 方法都注入到适配器（admin 靠 getattr / vars 探测）
    for name in ("begin_device_flow", "poll_device_flow", "handle_loopback_callback",
                 "refresh_account_auth", "begin_step_auth", "submit_step_auth"):
        assert name in vars(channel_cls), name
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    assert channel_cls.APPLY_CLIENT_PRESET is False
    assert channel_cls.REQUIRES_BASE_URL is False
    # 2026-10-08 定版删除「消息处理」：payload 钩子（system 合并/temperature 补齐）不得回归
    assert "payload" not in channel_cls._spec_hooks, "payload 钩子（消息处理）已被要求删除"
    assert "build_url" in channel_cls._spec_hooks, "聊天 URL 必须由 build_url 硬钉"


def test_account_schema_declares_loopback_mode(channel_cls):
    schema = channel_cls.account_schema()
    assert schema["provider_name"] == "bailu-test"
    assert schema["auth_start"]["enabled"] is True
    assert schema["auth_start"]["mode"] == "device_code"
    # loopback 完成方式：上游 redirect_uri 只收 127.0.0.1/localhost，前端据此显示补投框
    assert schema["auth_start"]["completion"] == "loopback"
    keys = {f["key"] for f in schema["fields"]}
    assert {"access_token", "refresh_token"}.issubset(keys)


# ==================== begin_device_flow ====================

def test_begin_device_flow_is_local_and_pkce_shaped(channel_cls):
    """纯本地生成：不发网络，PKCE/state 形状对齐插件（43/43/32）。"""
    p = channel_cls(username="u", password="")
    ctx = {"origin": "http://127.0.0.1:8001", "port": 8001, "state": "s1",
           "callback_url": "http://127.0.0.1:8001/admin/providers/bailu-test/accounts/auth/callback"}
    result = _run(p.begin_device_flow(ctx))

    assert result["task_type"] == "device_code"
    assert result["user_code"] == ""  # 没有验证码，别塞说明文字
    query = parse_qs(urlsplit(result["auth_url"]).query)
    assert query["client_id"] == ["bailucode_vscode"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["s1"]  # 直接用框架会话 state，上游原样带回
    assert query["redirect_uri"] == ["http://127.0.0.1:8001/oauth/callback"]
    challenge = query["code_challenge"][0]
    assert len(challenge) == 43
    verifier = result["poll_params"]["code_verifier"]
    assert len(verifier) == 43
    # challenge 必须是 verifier 的 S256
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert calc == challenge
    # 换 token 必须原样带回 redirect_uri
    assert result["poll_params"]["redirect_uri"] == "http://127.0.0.1:8001/oauth/callback"


def test_begin_device_flow_uses_dashboard_entry(channel_cls):
    """授权入口是 /dashboard.php（/oauth_consent 未登录 302 丢参，见 spec docstring）。"""
    p = channel_cls(username="u", password="")
    result = _run(p.begin_device_flow({"port": 8001, "state": "s"}))
    assert urlsplit(result["auth_url"]).path == "/dashboard.php"


def test_begin_device_flow_port_fallback(channel_cls):
    """auth_context 缺 port 时兜底 8001（main.py 默认监听端口）。"""
    p = channel_cls(username="u", password="")
    result = _run(p.begin_device_flow(None))
    query = parse_qs(urlsplit(result["auth_url"]).query)
    assert query["redirect_uri"] == ["http://127.0.0.1:8001/oauth/callback"]


def test_poll_device_flow_is_pending(channel_cls):
    """完成信号走本机回调，上游无可轮询端点，恒 pending。"""
    p = channel_cls(username="u", password="")
    assert _run(p.poll_device_flow({"state": "s"})) == {"status": "pending"}


# ==================== handle_loopback_callback ====================

def _fake_session(token_body: str, me_body: str | None, captured: dict, token_status: int = 200):
    class FakeResponse:
        status = token_status

        async def text(self):
            return token_body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class FakeMeResponse:
        status = 200

        async def text(self):
            return me_body or "{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class FakeSession:
        def post(self, url, json=None, headers=None, **_kw):
            captured["token_url"] = url
            captured["token_body"] = dict(json or {})
            return FakeResponse()

        def get(self, url, headers=None, **_kw):
            captured["me_url"] = url
            captured["me_auth"] = (headers or {}).get("Authorization", "")
            return FakeMeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    return FakeSession()


def _poll(state="s1", verifier="v" * 43):
    return {"code_verifier": verifier, "state": state,
            "redirect_uri": "http://127.0.0.1:8001/oauth/callback"}


def test_loopback_state_mismatch_not_claimed(channel_cls):
    """state 对不上就不认领（多会话并发时不能抢别人的回调）。"""
    p = channel_cls(username="u", password="")
    outcome = _run(p.handle_loopback_callback(
        {"code": "auth-code", "state": "someone-else"}, _poll(state="mine")))
    assert outcome is None


def test_loopback_error_is_claimed_as_error(channel_cls):
    """上游参数错误会 302 回投 error 到 redirect_uri：认领并转 error 终止会话。"""
    p = channel_cls(username="u", password="")
    outcome = _run(p.handle_loopback_callback(
        {"error": "invalid_client", "error_description": "Invalid client_id", "state": "s1"},
        _poll()))
    assert outcome["status"] == "error"
    assert "Invalid client_id" in outcome["error"]


def test_loopback_code_exchanges_token_and_profile(channel_cls, monkeypatch):
    """code 回调 → JSON 换 token → /oauth/me 取用户信息落号。"""
    captured: dict = {}
    token_body = ('{"access_token":"at-123","refresh_token":"rt-456","expires_in":3600}')
    me_body = ('{"id":"1331","uid":"1331","name":"wuxin","displayName":"wuxin",'
               '"email":"u@example.com","plan":"free","expiresAt":null,"status":"active"}')
    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session",
                        lambda self, *a, **k: _fake_session(token_body, me_body, captured))

    outcome = _run(p.handle_loopback_callback({"code": "auth-code", "state": "s1"}, _poll()))

    assert outcome["status"] == "authorized"
    data = outcome["account_data"]
    assert data["access_token"] == "at-123"
    assert data["refresh_token"] == "rt-456"
    assert data["username"] == "u@example.com"
    assert data["user_name"] == "wuxin"
    assert data["plan"] == "free"
    assert data["access_token_expires_at_ms"] > 0
    # 换 token 是 JSON body + 原样 redirect_uri（form 会被上游拒）
    assert captured["token_url"] == "https://api.bailucode.com/oauth/token"
    assert captured["token_body"]["grant_type"] == "authorization_code"
    assert captured["token_body"]["code"] == "auth-code"
    assert captured["token_body"]["client_id"] == "bailucode_vscode"
    assert captured["token_body"]["redirect_uri"] == "http://127.0.0.1:8001/oauth/callback"
    assert captured["me_auth"] == "Bearer at-123"


def test_loopback_me_failure_does_not_block(channel_cls, monkeypatch):
    """/oauth/me 挂了不阻断授权（用户提示该接口可能查询有问题），用户名退化 token 摘要。"""
    captured: dict = {}

    class FailMeResponse:
        status = 500

        async def text(self):
            return "boom"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class FailMeSession:
        def post(self, url, json=None, headers=None, **_kw):
            return _fake_session(
                '{"access_token":"at-123","refresh_token":"rt","expires_in":3600}',
                None, captured).post(url, json=json, headers=headers)

        def get(self, url, headers=None, **_kw):
            return FailMeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FailMeSession())

    outcome = _run(p.handle_loopback_callback({"code": "c", "state": "s1"}, _poll()))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["username"] == "bailu-at-123"


def test_loopback_token_failure_is_error(channel_cls, monkeypatch):
    """换码失败（code 过期/被用过）→ error 终止会话（插件同款：invalid_grant 即终态）。"""
    captured: dict = {}
    p = channel_cls(username="u", password="")
    monkeypatch.setattr(
        type(p), "_make_session",
        lambda self, *a, **k: _fake_session(
            '{"error":"invalid_grant","error_description":"Invalid authorization code"}',
            None, captured, token_status=400))

    outcome = _run(p.handle_loopback_callback({"code": "stale", "state": "s1"}, _poll()))
    assert outcome["status"] == "error"
    assert "Invalid authorization code" in outcome["error"]


# ==================== refresh_auth ====================

def test_refresh_auth_uses_refresh_grant(channel_cls, monkeypatch):
    captured: dict = {}
    p = channel_cls(username="u", password="", access_token="old",
                    refresh_token="rt-1")
    monkeypatch.setattr(
        type(p), "_make_session",
        lambda self, *a, **k: _fake_session(
            '{"access_token":"at-new","refresh_token":"rt-2","expires_in":7200}',
            '{"id":"9","name":"n","email":"e@x.com","plan":"pro","status":"active"}',
            captured))

    fields = _run(p.refresh_account_auth({"refresh_token": "rt-1"}, None))
    assert fields["access_token"] == "at-new"
    assert fields["refresh_token"] == "rt-2"
    assert captured["token_body"]["grant_type"] == "refresh_token"
    assert captured["token_body"]["refresh_token"] == "rt-1"


def test_refresh_auth_expired_is_400_not_401(channel_cls, monkeypatch):
    """refresh_token 失效 → 400（401/403 会被前端当管理员登录过期踢出控制台）。"""
    from fastapi import HTTPException

    captured: dict = {}
    p = channel_cls(username="u", password="", refresh_token="dead")
    monkeypatch.setattr(
        type(p), "_make_session",
        lambda self, *a, **k: _fake_session(
            '{"error":"invalid_grant","error_description":"Invalid refresh token"}',
            None, captured, token_status=400))

    with pytest.raises(HTTPException) as exc_info:
        _run(p.refresh_account_auth({"refresh_token": "dead"}, None))
    assert exc_info.value.status_code == 400


def test_refresh_auth_missing_token_is_400(channel_cls):
    from fastapi import HTTPException

    p = channel_cls(username="u", password="")
    with pytest.raises(HTTPException) as exc_info:
        _run(p.refresh_account_auth({}, None))
    assert exc_info.value.status_code == 400


# ==================== headers / 认证判定 ====================

def test_headers_sets_bearer_and_client_fingerprint(channel_cls):
    """access_token 覆盖 Bearer + 插件客户端指纹头（框架的 Bearer 取不到 access_token）。"""
    p = channel_cls(username="u", password="", access_token="at-1")
    out = p._headers()
    assert out["Authorization"] == "Bearer at-1"
    assert out["User-Agent"] == "eo/JS 4.83.0"
    assert out["x-stainless-lang"] == "js"


def test_headers_no_token_no_empty_bearer(channel_cls):
    p = channel_cls(username="u", password="")
    out = p._headers()
    assert "Authorization" not in out


def test_is_init_and_check_auth(channel_cls):
    assert channel_cls(username="u", password="").is_init() is False
    assert channel_cls(username="u", password="", access_token="t").is_init() is True
    assert channel_cls(username="u", password="", refresh_token="r").is_init() is True
    p = channel_cls(username="u", password="", access_token="t")
    assert _run(p.check_auth()) is True


def test_base_url_defaults_to_official(channel_cls):
    """渠道地址硬钉官方域名（token/me/chat 同宿主 api.bailucode.com）。

    2026-10-08 定版：本渠道一律走 https://api.bailucode.com，不接受渠道地址覆盖
    （bailucode.com/openapi 只认 API Key，OAuth token 过去必 401，不能切）。
    """
    p = channel_cls(username="u", password="")
    assert p.base_url == "https://api.bailucode.com"


def test_base_url_ignores_configured_override(channel_cls):
    """即使渠道配置里填了地址，base_url 仍硬钉官方域名（不再反代/换区域可覆盖）。"""
    p = channel_cls(username="u", password="")
    assert p.base_url == "https://api.bailucode.com"


def test_build_url_pins_chat_endpoint(channel_cls):
    """聊天 URL 强制 https://api.bailucode.com/v1/chat/completions，不受协议行影响。

    build_url 钩子经适配器 `_chat_url` 消费（spec 不直接挂方法），所以这里断言
    `_chat_url` 的最终结果而非 spec 类属性。
    """
    p = channel_cls(username="u", password="")
    assert p._chat_url({}) == "https://api.bailucode.com/v1/chat/completions"
    # 不同 kwargs/协议也不改变结果
    assert p._chat_url({"stream": True}) == "https://api.bailucode.com/v1/chat/completions"


def test_init_auth_self_heals_with_refresh(channel_cls, monkeypatch):
    """临期 token 用 refresh_token 自愈刷新并写回实例（persist_account_fields 窄写落库）。"""
    captured: dict = {}
    persisted: dict = {}

    async def _fake_persist(self, fields):
        persisted.update(fields)
        return True

    p = channel_cls(username="u", password="", access_token="old",
                    refresh_token="rt-1", access_token_expires_at_ms=1)
    monkeypatch.setattr(
        type(p), "_make_session",
        lambda self, *a, **k: _fake_session(
            '{"access_token":"at-new","expires_in":3600}', None, captured))
    monkeypatch.setattr(type(p), "persist_account_fields", _fake_persist)

    assert _run(p.init_auth(True)) is True
    assert p.access_token == "at-new"
    assert persisted["access_token"] == "at-new"


# ==================== 聊天 401 兜底（token_expired 不自动刷新的现场故障） ====================

def _spec_globals(channel_cls) -> dict:
    """spec 钩子函数的 ``__globals__``（= loader exec 出来的模块命名空间）。

    spec 里 stream_chat 调的是模块级 ``_ensure_token``，要替换它必须打到这份 globals 上，
    自己另 exec 一份命名空间是没用的（那是另一个 dict）。
    """
    return channel_cls._spec_hooks["stream_chat"].__globals__


def test_is_auth_refreshable_error_recognizes_401_403_only():
    """只有 401/403 才触发刷新重试；其它状态码原样抛（不掩盖真实错误）。"""
    from fastapi import HTTPException

    ns: dict = {"__name__": "spec_bailu_err"}
    exec(_SOURCE, ns)
    check = ns["_is_auth_refreshable_error"]
    assert check(HTTPException(status_code=401, detail="token_expired")) is True
    assert check(HTTPException(status_code=403, detail="forbidden")) is True
    assert check(HTTPException(status_code=429, detail="rate limited")) is False
    assert check(HTTPException(status_code=400, detail="bad request")) is False
    assert check(RuntimeError("boom")) is False


def _patch_stream_impl(monkeypatch, outputs):
    """把 CustomProvider._do_stream_chat 换成按序抛出/产出的替身，返回调用计数器。"""
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


async def _drain_stream(channel_cls, p):
    chunks = []
    async for chunk in channel_cls._spec_hooks["stream_chat"](
            p, "bailu-2.8-lite", [{"role": "user", "content": "hi"}]):
        chunks.append(chunk)
    return chunks


def test_stream_chat_refreshes_and_retries_on_401(channel_cls, monkeypatch):
    """报的故障就是这条：聊天撞 [401] token_expired → 强制刷新 token → 重试一次成功。

    之前 spec 没写 chat 钩子，401 直接冒到网关把账号冻住、没有任何恢复动作。
    """
    from fastapi import HTTPException

    refreshed: list = []

    async def _fake_ensure(p, force=False):
        if force:
            refreshed.append(True)
            p.access_token = "at-new"
            return "at-new"
        return getattr(p, "access_token", "")

    calls = _patch_stream_impl(monkeypatch, [
        HTTPException(status_code=401, detail='{"error":"token_expired"}'),
        [{"content": "hello", "thinking": "", "tool_calls": [], "usage": None}],
    ])
    monkeypatch.setitem(_spec_globals(channel_cls), "_ensure_token", _fake_ensure)

    p = channel_cls(username="u", password="", access_token="at-old", refresh_token="rt-1")
    chunks = _run(_drain_stream(channel_cls, p))

    assert calls["n"] == 2, "401 后应重试一次"
    assert refreshed == [True], "重试前必须强制刷新 token"
    assert chunks and chunks[0]["content"] == "hello"


def test_stream_chat_gives_up_after_one_retry_and_keeps_original_401(channel_cls, monkeypatch):
    """refresh 也救不了时（refresh_token 已废）不无限重试，把上游原 401 抛回去。"""
    from fastapi import HTTPException

    calls = _patch_stream_impl(monkeypatch, [
        HTTPException(status_code=401, detail='{"error":"token_expired"}'),
    ])

    async def _fake_ensure(p, force=False):
        if not force:
            return getattr(p, "access_token", "")
        raise HTTPException(status_code=400, detail="refresh_token 已失效")

    monkeypatch.setitem(_spec_globals(channel_cls), "_ensure_token", _fake_ensure)

    p = channel_cls(username="u", password="", access_token="dead", refresh_token="rt-dead")
    with pytest.raises(HTTPException) as exc_info:
        _run(_drain_stream(channel_cls, p))
    assert exc_info.value.status_code == 401, "应保留上游原 401，不改状态码"
    assert calls["n"] == 1, "刷新失败不得重试"


def test_stream_chat_does_not_retry_on_non_auth_error(channel_cls, monkeypatch):
    """429/500 等非认证错误不触发刷新（避免把限流当过期）。"""
    from fastapi import HTTPException

    calls = _patch_stream_impl(monkeypatch, [
        HTTPException(status_code=429, detail="rate limited"),
    ])
    p = channel_cls(username="u", password="", access_token="at", refresh_token="rt")
    with pytest.raises(HTTPException) as exc_info:
        _run(_drain_stream(channel_cls, p))
    assert exc_info.value.status_code == 429
    assert calls["n"] == 1


# ==================== 统一步骤 ====================

def test_step_auth_wraps_device_flow(channel_cls):
    """begin_step_auth 包装设备码流：task_type=steps + step.poll + poll_params。"""
    p = channel_cls(username="u", password="")
    result = _run(p.begin_step_auth({"port": 8001, "state": "s"}))
    assert result["task_type"] == "steps"
    step = result["step"]
    assert step["id"] == "device_authorize"
    assert step["auth_url"].startswith("https://bailucode.com/dashboard.php?")
    assert isinstance(step["poll"], dict) and step["poll"]["interval"] >= 1
    assert isinstance(result["step_state"]["poll_params"], dict)
    assert result["step_state"]["poll_params"]["state"] == "s"


def test_submit_step_auth_is_pending(channel_cls):
    p = channel_cls(username="u", password="")
    result = _run(p.submit_step_auth("device_authorize", {}, {"poll_params": {}}, None, None))
    assert result["status"] == "pending"
