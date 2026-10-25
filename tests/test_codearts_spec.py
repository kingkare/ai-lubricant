"""CodeArts spec 的授权契约（本机回调 + ticket 轮询双通道）。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
锁的是这次框架重构后 spec 侧的四条行为：
1. begin_device_flow 的回调端口来自框架 auth_context，不再写死 0；
2. user_code 不再塞说明文字（前端会渲染成「验证码：」）；
3. handle_loopback_callback 两阶段认领：ticket_id 对不上不认领、secret 对不上不换码；
4. poll_device_flow 接受可选 auth_context 尾参。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "codearts_code_channel.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/codearts_code_channel.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("codearts-test", _SOURCE)
    yield cls
    invalidate_cache("codearts-test")


def _run(coro):
    return asyncio.run(coro)


async def _drain_stream(gen):
    """消费异步生成器（stream_chat 是 async generator，不能 asyncio.run）。"""
    frames = []
    async for frame in gen:
        frames.append(frame)
    return frames


def _run_stream(gen):
    return asyncio.run(_drain_stream(gen))


def test_declares_expected_auth_surface(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {"init_auth", "check_auth", "fetch_models", "stream_chat", "refresh_auth",
            "perform_action", "account_schema", "begin_device_flow", "poll_device_flow",
            "handle_loopback_callback"}.issubset(hooks)
    # presence-triggered 方法都注入到适配器（admin 靠 getattr / vars 探测）
    for name in ("begin_device_flow", "poll_device_flow", "handle_loopback_callback",
                 "refresh_account_auth", "perform_account_action"):
        assert name in vars(channel_cls), name
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    # 2026-09-29 实测上游支持原生 function calling（tools 进、tool_calls 出、
    # role=tool 回传被模型读到），已从 tools-as-prompt 切回原生路径。
    assert channel_cls.TOOLS_AS_PROMPT is False
    assert channel_cls.REQUIRES_BASE_URL is False


def test_account_schema_declares_device_code_mode(channel_cls):
    schema = channel_cls.account_schema()
    assert schema["provider_name"] == "codearts-test"
    assert schema["auth_start"]["enabled"] is True
    assert schema["auth_start"]["mode"] == "device_code"
    # loopback 完成方式：上游只回调 127.0.0.1:{port}，前端据此显示「补投回调」输入框
    assert schema["auth_start"]["completion"] == "loopback"
    keys = {f["key"] for f in schema["fields"]}
    # 刷新凭证必需的两个字段必须在表单里（否则手动建号无法续期）
    assert {"refresh_token", "code_verifier"}.issubset(keys)


def test_begin_device_flow_uses_port_from_auth_context(channel_cls):
    """回调端口跟框架给的浏览器 origin 走，不再是渠道写死的 0。默认 vscode-codebot 身份。"""
    from urllib.parse import parse_qs, urlsplit

    p = channel_cls(username="u", password="")
    ctx = {"origin": "http://127.0.0.1:8001", "port": 8001, "state": "s1",
           "callback_url": "http://127.0.0.1:8001/admin/providers/codearts-test/accounts/auth/callback"}
    result = _run(p.begin_device_flow(ctx))

    assert result["task_type"] == "device_code"
    query = parse_qs(urlsplit(result["auth_url"]).query)
    assert query["port"] == ["8001"]
    # 默认身份 = vscode-codebot：SHA-256 / UUID ticket / 显式 auth_callback_url
    assert query["code_challenge_method"] == ["SHA-256"]
    assert query["uri_scheme"] == ["vscode-codebot"]
    assert query["client_id"] == ["vscode-codebot"]
    assert query["plugin-name"] == ["snap_vscode"]
    assert query["theme"] == ["2"]
    # auth_callback_url 里塞了 state：portal 若原样带回来，后端直接按 state 命中会话
    assert query["auth_callback_url"] == ["http://127.0.0.1:8001/oauth/callback?state=s1"]
    assert len(query["ticket_id"][0]) == 36   # UUID4 含 dash
    # 第二阶段换码要用同一个 port 拼 redirect_uri
    assert result["poll_params"]["port"] == 8001
    assert len(result["poll_params"]["code_verifier"]) >= 43
    # user_code 不塞说明文字：前端会把它渲染成「验证码：xxx」
    assert result["user_code"] == ""
    assert result["message"]


def test_begin_device_flow_without_context_degrades_to_port_zero(channel_cls):
    """没有 auth_context（老调用方）时不炸，退回 port=0 纯轮询形态。"""
    from urllib.parse import parse_qs, urlsplit

    p = channel_cls(username="u", password="")
    result = _run(p.begin_device_flow(None))
    query = parse_qs(urlsplit(result["auth_url"]).query)
    assert query["port"] == ["0"]
    # port=0 时不带显式回调地址（and port 短路，避免拼成 127.0.0.1:0）
    assert "auth_callback_url" not in query


def test_begin_device_flow_codearts_profile_uses_legacy_shape(channel_cls):
    """auth_client=codearts 走旧身份：S256 / 32-hex ticket / 不带 auth_callback_url。"""
    from urllib.parse import parse_qs, urlsplit

    p = channel_cls(username="u", password="", auth_client="codearts")
    result = _run(p.begin_device_flow({"port": 8001}))
    query = parse_qs(urlsplit(result["auth_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["uri_scheme"] == ["codearts"]
    assert query["client_id"] == ["codearts"]
    assert query["plugin-name"] == ["snap_AIIDE"]
    assert query["theme"] == ["dark"]
    assert "auth_callback_url" not in query
    assert len(query["ticket_id"][0]) == 32   # 32 hex


def test_account_schema_declares_auth_client_default_vscode(channel_cls):
    """account_schema 声明 auth_client 下拉，默认 vscode-codebot，保留 codearts。"""
    schema = channel_cls.account_schema()
    field = next(f for f in schema["fields"] if f["key"] == "auth_client")
    assert field["type"] == "select"
    assert field["default_value"] == "vscode-codebot"
    assert {opt["value"] for opt in field["options"]} == {"vscode-codebot", "codearts"}
    # 字段要能挂成实例属性并随授权落库，否则刷新时读不到身份
    assert "auth_client" in channel_cls.ACCOUNT_FIELDS


def test_loopback_first_stage_claims_by_ticket_id(channel_cls):
    p = channel_cls(username="u", password="")
    poll_params = {"ticket_id": "t" * 32, "secret": "local", "code_verifier": "v", "port": 8001}

    claimed = _run(p.handle_loopback_callback(
        {"secret": "portal-secret",
         "redirect": f"https://codearts.huaweicloud.com/portal/next?ticket_id={'t' * 32}"},
        poll_params))
    assert claimed["status"] == "claimed"
    # portal 下发的真 secret 回填进轮询参数（ticket 轮询必须用它）
    assert claimed["poll_params"]["secret"] == "portal-secret"
    assert claimed["poll_params"]["portal_secret"] == "portal-secret"
    assert claimed["redirect"].endswith("t" * 32)


def test_loopback_first_stage_ignores_other_sessions_ticket(channel_cls):
    """redirect 里的 ticket_id 不是本会话的就不认领（多会话并发时不能抢别人的回调）。"""
    p = channel_cls(username="u", password="")
    outcome = _run(p.handle_loopback_callback(
        {"secret": "portal-secret", "redirect": "https://portal.example/next?ticket_id=someone-else"},
        {"ticket_id": "t" * 32, "secret": "local", "port": 8001}))
    assert outcome is None


def test_loopback_second_stage_requires_matching_portal_secret(channel_cls):
    """两阶段形态：经过 stage-1（portal_secret_saved 非空）后，code 回调的 secret 必须
    与回填值相等才换码——防别的会话抢回调。直回调形态（无 portal_secret_saved）在
    下一条用例里单独覆盖。"""
    p = channel_cls(username="u", password="")
    # 经历过 stage-1：portal_secret_saved 已回填，回调 secret 对不上 → 不换
    outcome = _run(p.handle_loopback_callback(
        {"code": "auth-code", "secret": "wrong-secret"},
        {"ticket_id": "t" * 32, "portal_secret": "portal-secret", "code_verifier": "v", "port": 8001}))
    assert outcome is None


def test_loopback_direct_callback_exchanges_code_without_secret_handshake(channel_cls, monkeypatch):
    """vscode-codebot 直回调形态：portal 登录完直接带 code 回来，没有 secret 握手。
    portal_secret_saved 为空时只要 code 在就换码——这是「回调来了就处理」的简单模型，
    单会话场景；不再强求 portal_secret 握手。"""
    captured: dict = {}

    class FakeResponse:
        status = 200

        async def text(self):
            return ('{"user_id":"uid","user_name":"neo","domain_id":"did",'
                    '"refresh_token":"rt","credentials":{"access_key_id":"AK",'
                    '"secret_access_key":"SK","security_token":"ST",'
                    '"expiration":"2030-01-01T00:00:00Z"}}')

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            captured["url"] = url
            captured["form"] = dict(data or {})
            return FakeResponse()

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="u", password="")   # 默认 vscode-codebot 身份
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    # 没有 portal_secret_saved（直回调），回调只带 code → 直接换码
    outcome = _run(p.handle_loopback_callback(
        {"code": "auth-code"},
        {"ticket_id": "t" * 32, "code_verifier": "verifier-value", "port": 8001}))

    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["user_name"] == "neo"
    assert outcome["account_data"]["auth_client"] == "vscode-codebot"
    assert captured["form"]["code"] == "auth-code"
    assert captured["form"]["client_id"] == "vscode-codebot"


def test_loopback_second_stage_exchanges_code(channel_cls, monkeypatch):
    """secret 对得上就用 authorization_code 换凭证，redirect_uri 用登录时那个 port。"""
    captured: dict = {}

    class FakeResponse:
        status = 200

        async def text(self):
            return (
                '{"user_id":"uid","user_name":"neo","domain_id":"did",'
                '"refresh_token":"rt","credentials":{"access_key_id":"AK",'
                '"secret_access_key":"SK","security_token":"ST",'
                '"expiration":"2030-01-01T00:00:00Z"}}'
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            captured["url"] = url
            captured["form"] = dict(data or {})
            captured["headers"] = dict(headers or {})
            return FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    outcome = _run(p.handle_loopback_callback(
        {"code": "auth-code", "secret": "portal-secret"},
        {"ticket_id": "t" * 32, "portal_secret": "portal-secret",
         "code_verifier": "verifier-value", "port": 8001}))

    assert outcome["status"] == "authorized"
    data = outcome["account_data"]
    assert data["user_name"] == "neo"
    assert data["access_key_id"] == "AK"
    assert data["refresh_token"] == "rt"
    assert data["code_verifier"] == "verifier-value"   # 刷新凭证要用登录时那份
    assert data["auth_client"] == "vscode-codebot"     # 身份随号落库，刷新时同源
    assert captured["form"]["grant_type"] == "authorization_code"
    assert captured["form"]["code"] == "auth-code"
    assert captured["form"]["client_id"] == "vscode-codebot"   # 与 authorize 必须一致
    assert captured["form"]["redirect_uri"] == "http://127.0.0.1:8001/oauth/callback"
    assert captured["headers"]["DPoP"]                  # RFC 9449 证明头必带
    assert "/v1/oauth2/tokens" in captured["url"]


def test_poll_device_flow_accepts_optional_auth_context(channel_cls):
    """扫描器按签名传 auth_context；spec 收下但不依赖它（形态兼容即可）。"""
    import inspect

    hook = channel_cls._spec_hooks["poll_device_flow"]
    params = list(inspect.signature(hook).parameters)
    assert params[:2] == ["p", "poll_params"]
    assert "auth_context" in params


def _fake_sts_token(user_name: str = "hid_42z_q_f4hqn5gzr") -> str:
    """造一个形态贴合华为云 STS token 的串：base64(头部字节 + JSON 载荷 + 签名)。"""
    import base64 as _b64
    import json as _json

    payload = _json.dumps({
        "access": "HST3WMZYH7Q8S1Q473B8",
        "issued_at": 1788338040512,
        "methods": ["token"],
        "user": {
            "domain": {"id": "129423727a434ed9b9be5024793a9e5a", "name": user_name},
            "id": "004740bbe5ff44f99d605cd23e91c122",
            "name": user_name,
            "user_type": 17,
        },
    }, separators=(",", ":")).encode("utf-8")
    raw = b"\x82\ncn-north-4P\xda" + payload + b"\x8aU(\xca\x89\xd5F\x90 e\xe8^\x8e\xbc_"
    return _b64.urlsafe_b64encode(raw).decode("ascii")


def test_identity_recovered_from_sts_token_when_exchange_omits_user_name(channel_cls, monkeypatch):
    """直回调换码响应不带 user_name → 从 STS token 载荷解出账号名，不再落占位名。

    回归：admin 的 _finalize_authorized_auth 缺 user_name 时会用「{渠道名}-user」
    建号（实测落成 codearts-agent-user），账号列表里根本认不出是哪个华为云账号。
    """
    import json as _json

    token = _fake_sts_token()
    captured: dict = {}

    class FakeResponse:
        status = 200

        async def text(self):
            # 上游只回凭证，不回 user_id / user_name / domain_id
            return _json.dumps({
                "refresh_token": "rt",
                "credentials": {
                    "access_key_id": "AK", "secret_access_key": "SK",
                    "security_token": token, "expiration": "2030-01-01T00:00:00Z",
                },
            })

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            captured["form"] = dict(data or {})
            return FakeResponse()

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    outcome = _run(p.handle_loopback_callback(
        {"code": "auth-code"},
        {"ticket_id": "t" * 32, "code_verifier": "verifier-value", "port": 8001}))

    data = outcome["account_data"]
    assert data["user_name"] == "hid_42z_q_f4hqn5gzr"      # 从 token 补出来的
    assert data["user_id"] == "004740bbe5ff44f99d605cd23e91c122"
    assert data["domain_id"] == "129423727a434ed9b9be5024793a9e5a"
    assert data["security_token"] == token


def test_upstream_user_name_wins_over_token_payload(channel_cls, monkeypatch):
    """ticket 通道回了 user_name 就用它，不被 token 载荷覆盖。"""
    import json as _json

    token = _fake_sts_token("from-token")

    class FakeResponse:
        status = 200

        async def text(self):
            return _json.dumps({
                "user_name": "from-upstream", "user_id": "uid-upstream",
                "refresh_token": "rt",
                "credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                                "security_token": token, "expiration": "2030-01-01T00:00:00Z"},
            })

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, *_a, **_kw): return FakeResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    outcome = _run(p.handle_loopback_callback(
        {"code": "c"}, {"code_verifier": "v", "port": 8001}))
    assert outcome["account_data"]["user_name"] == "from-upstream"
    assert outcome["account_data"]["user_id"] == "uid-upstream"


def test_malformed_security_token_does_not_break_authorization(channel_cls, monkeypatch):
    """token 不是预期格式时只是补不出账号名，不能抛异常阻断落库。"""
    import json as _json

    class FakeResponse:
        status = 200

        async def text(self):
            return _json.dumps({
                "credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                                "security_token": "not-a-valid-sts-token",
                                "expiration": "2030-01-01T00:00:00Z"},
            })

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, *_a, **_kw): return FakeResponse()
        # 两条身份接口都失败（网络/上游抖动）——不阻断授权
        def request(self, *_a, **_kw):
            raise RuntimeError("upstream down")
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    outcome = _run(p.handle_loopback_callback(
        {"code": "c"}, {"code_verifier": "v", "port": 8001}))
    assert outcome["status"] == "authorized"
    assert "user_name" not in outcome["account_data"]


class _IdentityResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def text(self):
        import json as _json
        return _json.dumps(self._payload)

    async def __aenter__(self): return self
    async def __aexit__(self, *_a): return False


def _identity_session(*, exchange_payload, identity_reply, seen):
    """换码 POST + 身份 GET 双工的假 session。identity_reply 可以是 dict 或异常。"""
    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            seen.setdefault("posts", []).append(url)
            return _IdentityResp(exchange_payload)

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("gets", []).append(url)
            if isinstance(identity_reply, Exception):
                raise identity_reply
            return _IdentityResp(identity_reply)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    return FakeSession()


_EXCHANGE_NO_IDENTITY = {
    "refresh_token": "rt",
    "credentials": {"access_key_id": "AK", "secret_access_key": "SK",
                    # 真实 STS token 形态：非 base64(JSON)，本地解不出身份
                    "security_token": "opaque-not-decodable",
                    "expiration": "2030-01-01T00:00:00Z"},
}


def test_identity_looked_up_from_current_user_when_token_payload_has_none(channel_cls, monkeypatch):
    """换码不带身份 + token 载荷解不出（codearts 真实形态）→ 用新凭证查 current/user。

    实测现网换码响应 keys=[credentials, refresh_token]，token 载荷也不是
    纯 base64(JSON)，两条本地路径都拿不到身份，必须问上游要。
    """
    seen: dict = {}
    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _identity_session(exchange_payload=_EXCHANGE_NO_IDENTITY,
                                          identity_reply={"user_id": "uid-1",
                                                          "user_name": "huawei-acct",
                                                          "domain_id": "did-1",
                                                          "domain_name": "acct"},
                                          seen=seen))
    outcome = _run(p.handle_loopback_callback(
        {"code": "c"}, {"code_verifier": "v", "port": 8001}))

    data = outcome["account_data"]
    assert data["user_name"] == "huawei-acct"
    assert data["user_id"] == "uid-1"
    assert data["domain_id"] == "did-1"
    assert data["security_token"] == "opaque-not-decodable"
    # 必须带 snap-manager 前缀（漏掉上游回 APIG.0101）；只在登录路径查一次
    assert len(seen["gets"]) == 1
    assert "/snap-manager/v1/current/user" in seen["gets"][0]


def test_identity_falls_back_to_caller_identity(channel_cls, monkeypatch):
    """current/user 挂了 → caller-identity 兜底，用户名取 principal_urn 的 :user: 尾段。"""
    seen: dict = {}
    calls = {"n": 0}

    def reply(_method, url, **_kw):
        calls["n"] += 1
        seen.setdefault("gets", []).append(url)
        if "current/user" in url:
            return _IdentityResp({"error": "boom"}, status=500)
        return _IdentityResp({"account_id": "acct-9", "principal_id": "pid-9",
                              "principal_urn": "iam::acct-9:user:zhangsan"})

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            return _IdentityResp(_EXCHANGE_NO_IDENTITY)
        request = staticmethod(reply)
        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())
    outcome = _run(p.handle_loopback_callback(
        {"code": "c"}, {"code_verifier": "v", "port": 8001}))

    data = outcome["account_data"]
    assert data["user_name"] == "zhangsan"
    assert data["user_id"] == "pid-9"
    assert data["domain_id"] == "acct-9"
    assert calls["n"] == 2


def test_identity_lookup_shares_the_portal_toggle_client_id(channel_cls, monkeypatch):
    """身份查询与换码同身份——vscode-codebot 形态下不额外引 client 分叉。"""
    seen: dict = {}
    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _identity_session(exchange_payload=_EXCHANGE_NO_IDENTITY,
                                          identity_reply={"user_name": "x", "user_id": "y"},
                                          seen=seen))
    _run(p.handle_loopback_callback({"code": "c"}, {"code_verifier": "v", "port": 8001}))
    assert "/snap-manager/v1/current/user" in seen["gets"][0]
    assert seen["gets"][0].startswith("https://snap-access.cn-north-4.myhuaweicloud.com")


def test_refresh_path_does_not_hit_identity_endpoints(channel_cls, monkeypatch):
    """刷新路径不查身份（身份不随刷新变），避免每日定时刷新平白多打上游。"""
    seen: dict = {}
    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _identity_session(exchange_payload=_EXCHANGE_NO_IDENTITY,
                                          identity_reply={"user_name": "x"},
                                          seen=seen))
    _run(p.refresh_account_auth({"refresh_token": "rt", "code_verifier": "cv"}))
    # 刷新路径不查身份（只允许签到附带的那发 ops/delivery 非身份 GET）
    identity_gets = [u for u in seen.get("gets", [])
                     if "current/user" in u or "caller-identity" in u]
    assert identity_gets == [], f"刷新路径不该调任何身份接口: {identity_gets}"


# ==================== 模型三路发现 + 福利（免费套餐）路由 ====================

_SNAP = "https://snap-access.cn-north-4.myhuaweicloud.com"
_OPENGW = "https://opengw.developer.huaweicloud.com"


def _spec_globals(cls):
    """spec 命名空间（模块级状态随类缓存存活，测试间要手动清）。"""
    return cls.spec.fetch_models.__globals__


def _three_way_session(*, seen, benefit_fail=False, builtin_payload=None, balance_payload=None,
                       benefit_gone=False, center_mixed_case=False):
    """三路发现的假 session：按 URL 分发 agent-center / builtin / 福利三组端点。"""
    builtin_payload = builtin_payload if builtin_payload is not None else {"builtinModels": []}
    balance_payload = balance_payload if balance_payload is not None else {
        "error_code": "0000", "result": {"total_tokens": 10000000, "used_tokens": 0}}
    # agent-center 真机形态：model_name 是展示名、model_alias 才是聊天路由注册名，
    # 两者对部分模型**不同形**（GLM-5.3 vs glm-5.3 / DeepSeek-V4.1-Flash vs deepseek-v4.1-flash）。
    if center_mixed_case:
        center_models = [
            {"model_name": "GLM-5.2", "model_alias": "GLM-5.2",
             "model_parameters": {"context_window": 128000}},
            {"model_name": "GLM-5.3", "model_alias": "glm-5.3",
             "model_parameters": {"context_window": 128000}},
            {"model_name": "DeepSeek-V4.1-Flash", "model_alias": "deepseek-v4.1-flash",
             "model_parameters": {"context_window": 128000}},
            {"model_name": "GLM-5.2-ArkTS-SPARK", "model_alias": "glm-5.2-sft-harmony",
             "model_parameters": {}},
        ]
    else:
        center_models = [{"model_name": "GLM-5.2", "model_alias": "GLM-5.2",
                         "model_parameters": {"context_window": 128000}}]

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            seen.setdefault("posts", []).append(url)
            seen.setdefault("claim_bodies", []).append(data)
            if benefit_gone:
                return _IdentityResp({"error_code": "4000",
                                      "error_msg": "invalid channel, group not found",
                                      "result": None})
            return _IdentityResp({"error_code": "0000"})

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault(f"{method.lower()}s", []).append(url)
            if method.upper() == "POST" and "benefit/claim" in url:
                # 领取走 session.request（_signed_json_cred 统一入口）；
                # 断言 body 是字面量 {}（写成空体会被上游当非法请求）
                seen.setdefault("claim_bodies", []).append(data)
                seen.setdefault("claim_headers", []).append(dict(headers or {}))
                if benefit_gone:
                    return _IdentityResp({"error_code": "4000",
                                          "error_msg": "invalid channel, group not found",
                                          "result": None})
                return _IdentityResp({"error_code": "0000"})
            if "useragents" in url:
                return _IdentityResp({"agents": [{"agent_id": "ag1", "is_primary_agent": True}]})
            if "agents/detail" in url:
                return _IdentityResp({"gpts": {"models": center_models}})
            if "model/builtin" in url:
                if isinstance(builtin_payload, Exception):
                    raise builtin_payload
                return _IdentityResp(builtin_payload)
            if "gateway/config" in url:
                if benefit_gone:
                    # 实测 2026-10-21：三条 opengw 端点对所有账号都回这个体（HTTP 200）
                    return _IdentityResp({"error_code": "4000",
                                          "error_msg": "invalid channel, group not found",
                                          "result": None})
                if benefit_fail:
                    return _IdentityResp({"error_code": "E-BUSY"}, status=502)
                return _IdentityResp({"error_code": "0000", "result": {"base_url": "", "models": [
                    {"model_id": "glm-5.3-flash", "model_name": "GLM-5.3-Flash",
                     "context_window": 128000, "max_tokens": 8192},
                    {"model_id": "deepseek-v4-flash-0731", "model_name": "DS4F",
                     "context_window": 64000, "max_tokens": 4096}]}})
            if "tokens/balance" in url:
                if benefit_gone:
                    return _IdentityResp({"error_code": "4000",
                                          "error_msg": "invalid channel, group not found",
                                          "result": None})
                return _IdentityResp(balance_payload)
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    return FakeSession()


def _acct(cls, username="acct-A"):
    return cls(username=username, password="",
               access_key_id="AK", secret_access_key="SK", security_token="ST")


def test_fetch_models_merges_three_sources_and_marks_benefit(channel_cls, monkeypatch):
    """三路合并：agent-center + 内置 + 福利；福利模型带 benefit 标记、ID 保持小写。"""
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))
    models = _run(p.fetch_upstream_model_list())

    ids = {m["id"] for m in models}
    assert "GLM-5.2" in ids                     # agent-center（大写注册名）
    assert "glm-5.3-flash" in ids               # 福利（小写）
    assert "deepseek-v4-flash-0731" in ids
    flag = {m["id"]: m.get("benefit") for m in models}
    assert flag["glm-5.3-flash"] is True
    assert flag["deepseek-v4-flash-0731"] is True
    assert not flag["GLM-5.2"]                  # 内置模型不带福利标记
    # 领取必须发字面量 {}（写成空体会被上游当非法请求）
    assert seen["claim_bodies"] and seen["claim_bodies"][0] == b"{}"


def test_fetch_models_keeps_benefit_models_when_gateway_fails(channel_cls, monkeypatch):
    """福利网关临时故障 → 沿用上一轮目录合并，否则框架对账会把已入库的福利模型删掉。"""
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))
    first = _run(p.fetch_upstream_model_list())
    assert "glm-5.3-flash" in {m["id"] for m in first}

    # 第二轮：福利网关 502
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen, benefit_fail=True))
    second = _run(p.fetch_upstream_model_list())
    ids = {m["id"] for m in second}
    assert "glm-5.3-flash" in ids and "deepseek-v4-flash-0731" in ids
    assert any(m.get("benefit") for m in second)


def test_stream_chat_sends_maas_type_only_for_benefit_models(channel_cls, monkeypatch):
    """福利模型聊天必须带 maas_type: benefit 且计入签名（签名前写入）；内置模型不带。"""
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls, "acct-B")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))
    _run(p.fetch_upstream_model_list())         # 先让目录有结论

    captured: dict = {}

    async def fake_sse(method, url, headers, **kw):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(p, "send_sse_request", fake_sse)
    _run_stream(p._do_stream_chat("glm-5.3-flash", [{"role": "user", "content": "hi"}]))
    assert captured["headers"].get("maas_type") == "benefit"
    # 关键：maas_type 必须**计入 SignedHeaders**（签名前写入）。上游按 SignedHeaders
    # 列表重算签名，头在签名后补会被当成未签名字段拒。
    auth = captured["headers"].get("Authorization") or ""
    signed = auth.split("SignedHeaders=")[1].split(",")[0] if "SignedHeaders=" in auth else ""
    assert "maas_type" in signed.split(";"), f"maas_type 未计入签名: {auth[:120]}"
    assert "host" in signed.split(";")

    _run_stream(p._do_stream_chat("GLM-5.2", [{"role": "user", "content": "hi"}]))
    assert "maas_type" not in captured["headers"]


def test_quota_exhausted_502_remapped_to_403(channel_cls, monkeypatch):
    """上游 502 + 「insufficient quota」→ 对调用方改判 403，且**不重试**。

    403 让调用方把「上游额度不足」与「上游故障 502」区分开；额度不足重试也恢复不了，
    不该被当成临时故障反复打。
    """
    from fastapi import HTTPException

    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls, "acct-403")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))

    calls = {"n": 0}

    async def fake_sse(method, url, headers, **kw):
        calls["n"] += 1
        raise HTTPException(status_code=502, detail="insufficient quota")
        yield  # pragma: no cover - 使其成为 async generator

    monkeypatch.setattr(p, "send_sse_request", fake_sse)
    with pytest.raises(HTTPException) as ei:
        _run_stream(p._do_stream_chat("GLM-5.2", [{"role": "user", "content": "hi"}]))
    assert ei.value.status_code == 403
    assert "insufficient quota" in str(ei.value.detail)
    assert calls["n"] == 1, "额度不足不该重试"


def test_quota_429_and_401_not_remapped(channel_cls, monkeypatch):
    """429（限流）/401（凭证）不改判 403——429 要留给退避重试，401 走凭证自愈重试。"""
    from fastapi import HTTPException

    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()

    seen: dict = {}
    p = _acct(channel_cls, "acct-429")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))

    async def fake_sse_429(method, url, headers, **kw):
        raise HTTPException(status_code=429, detail="quota limit exceeded")
        yield  # pragma: no cover - 使其成为 async generator

    monkeypatch.setattr(p, "send_sse_request", fake_sse_429)
    with pytest.raises(HTTPException) as ei:
        _run_stream(p._do_stream_chat("GLM-5.2", [{"role": "user", "content": "hi"}]))
    assert ei.value.status_code == 429, "429 必须原样保留（限流退避）"


def test_stream_chat_seed_cleared_after_benefit_retired(channel_cls, monkeypatch):
    """免费套餐下线后种子清空：冷启动不再给任何模型猜着带 maas_type 头。

    旧行为是「宁可多带一次头」——那时福利模型转正后仍可用。套餐整体下线后福利模型
    一律 404/4004，再带头只会把死模型拖进聊天流程、还每请求白打一发 claim。
    故 _SEED_BENEFIT_MODELS 置空，冷启动目录空时对 former-seed 模型也不带头。
    """
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls, "acct-C")                          # 该账号从未跑过发现
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))

    captured: dict = {}

    async def fake_sse(method, url, headers, **kw):
        captured["headers"] = dict(headers or {})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(p, "send_sse_request", fake_sse)
    # former-seed 模型：套餐在时该带头，套餐没了不该带
    _run_stream(p._do_stream_chat("glm-5.3-flash", [{"role": "user", "content": "hi"}]))
    assert "maas_type" not in captured["headers"], "套餐下线后 former-seed 不该再带头"
    # 也不再补 claim（POST 是纯噪声）
    assert not any("benefit/claim" in u for u in seen.get("posts", [])), \
        "former-seed 模型不该触发福利领取"
    # 内置模型照旧不带头
    _run_stream(p._do_stream_chat("GLM-5.2", [{"role": "user", "content": "hi"}]))
    assert "maas_type" not in captured["headers"]


def test_fetch_models_drops_benefit_when_gateway_retired(channel_cls, monkeypatch):
    """opengw 回 invalid channel / group not found → 福利目录清空、不回退上一轮。

    这是 2026-10 实测：三条福利端点对所有账号都回 {"error_code":"4000","error_msg":
    "invalid channel, group not found"}。若仍按「网关瞬态故障」沿用上一轮福利目录，
    框架对账会把已死的福利模型重新入库，用户选到就是 404/4004。下线 ≠ 故障，必须区分。
    """
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls, "acct-gone")
    # 第一轮：福利网关正常（有模型）
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))
    first = _run(p.fetch_upstream_model_list())
    assert "glm-5.3-flash" in {m["id"] for m in first}

    # 第二轮：福利网关回 invalid channel（下线）
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen, benefit_gone=True))
    second = _run(p.fetch_upstream_model_list())
    ids = {m["id"] for m in second}
    assert "glm-5.3-flash" not in ids, "套餐下线必须清空福利目录，不能沿用上一轮"
    assert not any(m.get("benefit") for m in second)
    # 目录里登记 benefit_gone，聊天据此不带头
    entry = g["_BENEFIT_CATALOG"][g["_account_key"](p)]
    assert entry.get("benefit_gone") is True


def test_transport_error_not_misread_as_quota(channel_cls):
    """分块流中断（ClientPayloadError ... Not enough data ...）不得判成额度耗尽。

    实测踩过：上游把流式响应中途掐断 → aiohttp 抛 ClientPayloadError，detail 含
    "Not enough data for satisfy transfer length header"。裸 "not enough" 关键词把它
    误判成额度耗尽 → 改判 403 → 账号被当「额度耗尽」永久冻结。那是瞬态传输故障，
    该走退避重试。_is_quota_exhausted 必须先否决传输层形态。
    """
    g = _spec_globals(channel_cls)
    blip = ("https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions "
            "HTTP client error: ClientPayloadError: Response payload is not completed: "
            "<TransferEncodingError: 400, message='Not enough data for satisfy transfer length header.'>")
    assert g["_is_transport_error"](blip) is True
    assert g["_is_quota_exhausted"](blip) is False, "传输层中断绝不能判成额度耗尽"
    # 真额度文案仍要命中
    assert g["_is_quota_exhausted"]("insufficient quota") is True
    assert g["_is_quota_exhausted"]("not enough quota") is True
    assert g["_is_quota_exhausted"]("额度已用完") is True


def test_agent_center_model_id_is_alias(channel_cls, monkeypatch):
    """agent-center 用 model_alias 作 id（聊天路由注册名），不能用 model_name。

    实测：model_name（GLM-5.3 / DeepSeek-V4.1-Flash / OpenPangu-2.0-Pro）打 v2 路由
    一律 InferHub.002002009.404 not registered；model_alias（glm-5.3 / deepseek-v4.1-flash
    / openpangu-2.0-pro，与内置那路 model_id 一致）才 200。早先用 model_name 是 52 次
    not-registered 的来源。展示名仍取 model_name。
    """
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls, "acct-case")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen, center_mixed_case=True))
    models = _run(p.fetch_upstream_model_list())
    by_id = {m["id"]: m for m in models}
    # 路由 id 用 alias（小写），不是 name（大写）
    assert "glm-5.3" in by_id, f"应收录 alias 名 glm-5.3，实得 {sorted(by_id)}"
    assert "GLM-5.3" not in by_id
    assert by_id["glm-5.3"]["name"] == "GLM-5.3"      # 展示名仍是 model_name
    assert by_id["glm-5.3"]["upstream_id"] == "glm-5.3"


def test_balance_queried_once_per_discovery(channel_cls, monkeypatch):
    """额度/余额只读查询随发现跑一次，便于核对免费套餐是否真在生效。"""
    seen: dict = {}
    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen,
                                           balance_payload={"error_code": "0000", "result": {
                                               "total_tokens": 10000000, "used_tokens": 123}}))
    _run(p.fetch_upstream_model_list())
    balance_hits = [u for u in seen["gets"] if "tokens/balance" in u]
    assert len(balance_hits) == 1



def test_source_keeps_protocol_landmarks():
    """协议关键串在位（防重构时把上游端点/签名算法改跑偏）。"""
    for token in ("/api/v2/chat/completions", "snap-manager/v1/login/ticket",
                  "/v1/oauth2/tokens", "SDK-HMAC-SHA256", "dpop+jwt",
                  "agent-center/agents/useragents", "/oauth/callback"):
        assert token in _SOURCE, token
    # port 不再写死 0
    assert '"port": 0' not in _SOURCE
    assert 'ctx.get("port")' in _SOURCE
    # 两种身份都在，且默认是 vscode-codebot（对齐官方 VSCode 插件 authorize 形态）
    for token in ("snap_vscode", "26.8.203", "snap_AIIDE", "auth_callback_url"):
        assert token in _SOURCE, token
    assert '_DEFAULT_AUTH_PROFILE = "vscode-codebot"' in _SOURCE


# ==================== 凭证续期（自动刷新必须真的刷新） ====================

def test_sts_expiry_field_declared_in_schema_so_save_keeps_it(channel_cls):
    """security_token_expires_at_ms 必须在 schema 里。

    账号保存是整行替换（upsert_provider_account_async: accounts[i] = account），
    schema 外的键被静默丢弃 → 该字段永远落不了库 → _credentials_expiring 恒 False →
    提前续期永不触发，体检就停在「认证已过期，等待自动刷新」。
    """
    fields = {f["key"]: f for f in channel_cls.account_schema()["fields"]}
    assert "security_token_expires_at_ms" in fields
    assert fields["security_token_expires_at_ms"]["type"] == "datetime"
    # 它也必须在 ACCOUNT_FIELDS 里（否则不会挂成实例属性，_apply_fields 读不到）
    assert "security_token_expires_at_ms" in channel_cls.ACCOUNT_FIELDS


def _refresh_recovery_session(*, seen, new_token, probe_fails_on):
    """凭据过期：老 token 打 agent-center 必失败，续期换新 token 后成功。"""
    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            seen.setdefault("posts", []).append(url)
            # 换新 STS 凭证（新 token 区别于过期的）
            return _IdentityResp({
                "refresh_token": "rt-refreshed",
                "credentials": {"access_key_id": "AK-NEW", "secret_access_key": "SK-NEW",
                                "security_token": new_token,
                                "expiration": "2030-01-01T00:00:00Z"},
            })

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("gets", []).append(url)
            if "useragents" in url:
                # 只有拿着过期 token 才失败——续期后 token 变了自然就通过
                if (headers or {}).get("X-Security-Token") == probe_fails_on:
                    return _IdentityResp({"message": "token expired"}, status=401)
                return _IdentityResp({"agents": [{"agent_id": "ag1", "is_primary_agent": True}]})
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    return FakeSession()


def test_init_auth_self_heals_by_refreshing_when_probe_fails(channel_cls, monkeypatch):
    """体检失败必须**真的续期**，不能只报「等待自动刷新」。

    框架 init_all/check_account 对 SUPPORTS_TOKEN_AUTO_REFRESH 渠道走 init_auth(True)，
    期望这一步主动刷新。旧实现只探测不刷新 → 凭证过期后账号永远停在
    「认证已过期，等待自动刷新」，而那一步永远不发生。
    """
    seen: dict = {}
    p = channel_cls(username="acct-A", password="",
                    access_key_id="AK", secret_access_key="SK",
                    security_token="ST-EXPIRED",
                    refresh_token="rt-old", code_verifier="verifier",
                    # 未来时间：非 force 的 _ensure_token 不会提前刷新，逼出探测失败这条路径
                    security_token_expires_at_ms=int(time.time() * 1000) + 3600_000)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _refresh_recovery_session(seen=seen, new_token="ST-FRESH",
                                                  probe_fails_on="ST-EXPIRED"))
    persisted: dict = {}

    async def fake_persist(self, fields):
        persisted.update(fields)
        return True

    monkeypatch.setattr(type(p), "persist_account_fields", fake_persist)

    assert _run(p.init_auth(True)) is True
    # 关键断言：确实打过续期（POST 换票），且新凭证落回实例
    assert seen.get("posts"), "体检失败却没走任何续期调用——正是「等待自动刷新」不刷新"
    assert p.security_token == "ST-FRESH"
    assert persisted.get("security_token") == "ST-FRESH"
    # 探测先失败后成功：至少两发 agent-center（第一次探测 + 续期后重探）
    probes = [u for u in seen.get("gets", []) if "useragents" in u]
    assert len(probes) == 2, f"应探测两次（失败→续期→重探），实际 {len(probes)}"


def test_init_auth_gives_up_after_one_refresh_attempt(channel_cls, monkeypatch):
    """续期后仍失败（refresh_token 已作废）→ 返回 False，不无限重试。"""
    seen: dict = {}

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            seen.setdefault("posts", []).append(url)
            return _IdentityResp({"error": "invalid refresh token"}, status=400)

        def request(self, method, url, headers=None, data=None, **_kw):
            return _IdentityResp({"message": "expired"}, status=401)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="acct-A", password="",
                    access_key_id="AK", secret_access_key="SK",
                    security_token="ST-EXPIRED", refresh_token="rt-dead",
                    code_verifier="verifier",
                    security_token_expires_at_ms=int(time.time() * 1000) + 3600_000)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    assert _run(p.init_auth(True)) is False
    # 只试一次续期，不循环
    assert len(seen.get("posts", [])) == 1


# ==================== 边缘 WAF 拦截 ≠ 凭证失效（防烧号） ====================
# 上游边缘层（Cloudflare）的 403 是 HTML 页、请求根本没到达华为云业务网关；华为云真拒绝
# 是 JSON（error_code: APIG.0301）。两者状态码相同但语义相反：前者要当瞬态故障，后者
# 才是凭证失效。误判的代价是拿单次有效的 refresh_token 去"自愈"——白白烧号，
# 下次刷新必报 the refresh token has been used，账号只能重新网页授权。

_WAF_HTML = ("<html><head><title>403 Forbidden</title></head>"
             "<body><center><h1>403 Forbidden</h1></center>"
             "<hr><center>cloudflare</center></body></html>")


def _waf_probe_session(*, status: int = 403, body: str = _WAF_HTML):
    """agent-center 探测固定回边缘 403（HTML），且任何 POST 都要炸——续期一旦发生就
    该被这个假 session 抓到（拿单次 refresh 救边缘拦截正是要防的事）。"""
    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            raise AssertionError("边缘拦截不该触发凭证续期（会烧掉单次 refresh_token）")

        def request(self, method, url, headers=None, data=None, **_kw):
            return _IdentityResp({"message": body}, status=status)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    return FakeSession()


def test_check_auth_treats_edge_waf_403_as_valid(channel_cls, monkeypatch):
    """check_auth 遇边缘 WAF 的 403 HTML → 回 True（凭证是好的，是边缘限流）。

    回 False 会让框架把账号标「认证已过期」并冻结，且 call 侧 init_auth 会紧跟着
    拿单次 refresh_token 强制刷新——拿账号唯一的刷新机会救一个边缘拦截，纯亏。
    实测该形态下真实凭证仍能正常聊天，判失效纯属误伤。
    """
    p = channel_cls(username="acct-WAF", password="",
                    access_key_id="AK", secret_access_key="SK",
                    security_token="ST", refresh_token="rt",
                    code_verifier="verifier",
                    # 未来时间：非 force 的 _ensure_token 不提前刷新，逼出探测路径
                    security_token_expires_at_ms=int(time.time() * 1000) + 3600_000)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _waf_probe_session())
    assert _run(p.check_auth()) is True


def test_check_auth_still_reports_huawei_json_rejection(channel_cls, monkeypatch):
    """对照：华为云真拒绝（APIG.0301 x-auth-token not found）→ 仍回 False 交上层续期。

    这是与上一条的分界线：有 error_code/APIG 标志的是业务拒绝，不是边缘拦截。
    上一条若误吞了真拒绝，账号就永远停在失效态没人救。
    """
    p = channel_cls(username="acct-DEAD", password="",
                    access_key_id="AK", secret_access_key="SK",
                    security_token="ST-BAD", refresh_token="rt",
                    code_verifier="verifier",
                    security_token_expires_at_ms=int(time.time() * 1000) + 3600_000)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _waf_probe_session(status=401,
                                           body='{"error_code":"APIG.0301",'
                                                '"error_msg":"x-auth-token not found"}'))
    assert _run(p.check_auth()) is False


def test_init_auth_does_not_burn_token_on_edge_waf_403(channel_cls, monkeypatch):
    """init_auth 探测遇边缘 403 → 回 True 且**零续期**。

    假 session 的 post 一被调用就 AssertionError：只要实现仍走「探测失败即强制
    刷新」，这条就会红。锁的是不烧号，比锁返回值更硬。
    """
    p = channel_cls(username="acct-WAF2", password="",
                    access_key_id="AK", secret_access_key="SK",
                    security_token="ST", refresh_token="rt",
                    code_verifier="verifier",
                    security_token_expires_at_ms=int(time.time() * 1000) + 3600_000)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _waf_probe_session())
    assert _run(p.init_auth(True)) is True


def test_stream_chat_waf_403_does_not_refresh_or_retry(channel_cls, monkeypatch):
    """聊天遇边缘 403（首帧前）→ 直接抛，不刷新不重试。

    刷新一次 = 烧一个单次 refresh_token；重发一次 = 再撞一次 WAF。两者都纯亏。
    """
    from fastapi import HTTPException

    g = _spec_globals(channel_cls)
    g["_BENEFIT_CATALOG"].clear()
    g["_BENEFIT_CLAIMED"].clear()
    p = _acct(channel_cls, "acct-WAF-chat")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen={}))

    force_calls: list = []

    async def spy_ensure_token(_p, force: bool = False):
        # 非 force 的常规保障放行（等价于原实现的无操作）；force 一出现即烧号，抓现行
        if force:
            force_calls.append(force)
            raise AssertionError("边缘 403 不该触发凭证刷新（会烧掉单次 refresh_token）")

    monkeypatch.setitem(g, "_ensure_token", spy_ensure_token)

    async def fake_sse(method, url, headers, **kw):
        # 生产形态：base.send_sse_request 对非 200 直接
        # HTTPException(status=response.status, detail=响应体原文)，HTML 原样进 detail
        raise HTTPException(status_code=403, detail=_WAF_HTML)
        yield  # pragma: no cover - 使其成为 async generator

    monkeypatch.setattr(p, "send_sse_request", fake_sse)
    with pytest.raises(HTTPException) as ei:
        _run_stream(p._do_stream_chat("GLM-5.2", [{"role": "user", "content": "hi"}]))
    # v2：WAF 边缘 403 改判 502（上游故障语义），避免命中渠道规则「403→冻到今天」误伤好号。
    assert ei.value.status_code == 502
    assert force_calls == [], "边缘 403 绝不允许走到强制续期"


def test_identity_lookup_timeout_does_not_drop_exchanged_credentials(
        channel_cls, monkeypatch):
    """身份查询单条限时：挂住的端点超时即放弃，**已换到的凭证照常返回**。

    换码跑在回调路径上，框架只给 handle_loopback_callback 30s。没有限时，一个
    挂住的身份端点把整轮回调拖过 30s → 框架 wait_for 判超时丢弃，用户看到 400，
    实际换码早已成功、code 已被消耗——凭证凭空蒸发。锁：限时后走另一条/放弃，
    account_data 照常带回凭证。
    """
    import asyncio as _asyncio

    g = _spec_globals(channel_cls)
    monkeypatch.setitem(g, "_IDENTITY_LOOKUP_TIMEOUT", 0.2)
    seen: dict = {}

    class _HangResp:
        status = 200

        async def __aenter__(self):
            await _asyncio.sleep(30)   # 远超 0.2s 限时，模拟挂死的身份端点
            return self

        async def text(self):
            return "{}"

        async def __aexit__(self, *_a):
            return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            seen.setdefault("posts", []).append(url)
            return _IdentityResp(_EXCHANGE_NO_IDENTITY)

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("gets", []).append(url)
            return _HangResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    started = time.monotonic()
    outcome = _run(p.handle_loopback_callback(
        {"code": "c"}, {"code_verifier": "v", "port": 8001}))
    elapsed = time.monotonic() - started

    assert outcome["status"] == "authorized"
    # 关键：换码出的凭证原样带回，没有因为身份查询挂死而整轮丢失
    data = outcome["account_data"]
    assert data["refresh_token"] == "rt"
    assert data["security_token"] == "opaque-not-decodable"
    # 身份没拿到（两条路都挂）不阻断，账号名留给后续补全/手改
    assert not data.get("user_name")
    # 两条身份路都试过（current/user + caller-identity），且总耗时被限时压住
    assert len(seen["gets"]) == 2
    assert elapsed < 5, f"身份查询未按限时放弃（耗时 {elapsed:.1f}s），回调会被框架 30s 掐掉"


# ==================== DPoP 私钥持久化（refresh_token 绑 jkt 修复） ====================

def _b64u(raw: bytes) -> str:
    import base64 as _b64
    return _b64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pub_jwk_of(pem: str) -> dict:
    """从 PEM 私钥推出 DPoP 头里应有的 EC 公钥 JWK（x/y base64url 无填充）。"""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pub = load_pem_private_key(pem.encode("ascii"), password=None).public_key().public_numbers()
    return {"kty": "EC", "crv": "P-256",
            "x": _b64u(pub.x.to_bytes(32, "big")),
            "y": _b64u(pub.y.to_bytes(32, "big"))}


def _proof_header_jwk(proof: str) -> dict:
    import base64 as _b64
    import json as _json
    seg = proof.split(".")[0]
    seg += "=" * (-len(seg) % 4)   # 补回被 strip 的 padding
    return _json.loads(_b64.urlsafe_b64decode(seg))["jwk"]


def test_dpop_private_key_field_declared_so_save_keeps_it(channel_cls):
    """dpop_private_key 必须在 schema + ACCOUNT_FIELDS 里。

    账号保存是整行替换（upsert_provider_account_async: accounts[i] = account），
    schema 外的键被静默丢弃 → 私钥落不了库 → 下次刷新 refresh_token 绑的公钥指纹
    对不上 → STS5.1806 invalid jkt → 账号 1 小时后只能重新网页授权（自愈链路断裂）。
    """
    fields = {f["key"]: f for f in channel_cls.account_schema()["fields"]}
    assert "dpop_private_key" in fields
    assert fields["dpop_private_key"]["type"] == "password"
    assert fields["dpop_private_key"].get("secret") is True
    # 也必须在 ACCOUNT_FIELDS 里（否则不会挂成实例属性，_apply_fields / getattr 都读不到）
    assert "dpop_private_key" in channel_cls.ACCOUNT_FIELDS


def test_begin_device_flow_carries_valid_dpop_private_key(channel_cls):
    """授权启动生成一把可加载的 EC P-256 私钥，并放进 poll_params 透传到换码那步。"""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    p = channel_cls(username="u", password="")
    result = _run(p.begin_device_flow({"port": 8001}))
    pem = result["poll_params"].get("dpop_private_key")
    assert pem and "BEGIN PRIVATE KEY" in pem
    # 必须真能加载——刷新时 _dpop_proof 会 load_pem_private_key，加载失败直接炸
    load_pem_private_key(pem.encode("ascii"), password=None)


def test_loopback_exchange_uses_and_persists_session_dpop_key(channel_cls, monkeypatch):
    """直回调换码：DPoP 头用 poll_params 那把私钥签；account_data 原样落库供刷新复用。

    回归 refresh_token 绑 jkt 的根因——旧实现 _dpop_proof 每次现造新私钥，换码用 A、
    刷新用 B，公钥指纹对不上 → invalid jkt。换码与刷新必须用**同一把**。
    """
    captured: dict = {}

    class FakeResponse:
        status = 200

        async def text(self):
            return ('{"user_id":"uid","user_name":"neo","domain_id":"did",'
                    '"refresh_token":"rt","credentials":{"access_key_id":"AK",'
                    '"secret_access_key":"SK","security_token":"ST",'
                    '"expiration":"2030-01-01T00:00:00Z"}}')

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            captured["headers"] = dict(headers or {})
            return FakeResponse()

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = channel_cls(username="u", password="")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    flow = _run(p.begin_device_flow({"port": 8001}))
    poll_key = flow["poll_params"]["dpop_private_key"]

    outcome = _run(p.handle_loopback_callback(
        {"code": "auth-code"},
        {"ticket_id": flow["poll_params"]["ticket_id"],
         "portal_secret": "",   # 直回调形态：无 secret 握手
         "code_verifier": flow["poll_params"]["code_verifier"],
         "dpop_private_key": poll_key,
         "port": 8001}))

    assert outcome["status"] == "authorized"
    # 1. 落库的私钥 = 会话里那把（不能另造一把，否则刷新时 jkt 对不上）
    assert outcome["account_data"]["dpop_private_key"] == poll_key
    # 2. DPoP 头里的公钥 JWK = poll_key 推出的公钥（证明签的就是这把，不是临时造的）
    proof = captured["headers"]["DPoP"]
    assert _proof_header_jwk(proof) == _pub_jwk_of(poll_key)


def test_refresh_reuses_stored_dpop_key_not_a_fresh_one(channel_cls, monkeypatch):
    """refresh_auth 用账号里那把 dpop 私钥签 DPoP，不另造一把。

    这是 invalid jkt 的核心修复点：刷新必须复用换码时那把私钥。另造一把 →
    STS5.1806 invalid jkt → refresh_token 失效 → 账号 1h 后只能重新授权。
    """
    captured: dict = {}

    class FakeResponse:
        status = 200

        async def text(self):
            return ('{"refresh_token":"rt2","credentials":{"access_key_id":"AK",'
                    '"secret_access_key":"SK","security_token":"ST",'
                    '"expiration":"2030-01-01T00:00:00Z"}}')

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            captured["headers"] = dict(headers or {})
            return FakeResponse()

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    flow = _run(channel_cls(username="u", password="").begin_device_flow({"port": 8001}))
    stored_pem = flow["poll_params"]["dpop_private_key"]

    p = channel_cls(username="u", password="",
                     refresh_token="rt", code_verifier="cv",
                     dpop_private_key=stored_pem)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    fields = _run(p.refresh_account_auth(
        {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": stored_pem}))

    # 1. DPoP 头里的公钥 = 账号里那把的公钥（证明复用而非另造）
    assert _proof_header_jwk(captured["headers"]["DPoP"]) == _pub_jwk_of(stored_pem)
    # 2. 回传字段带回同一把（保证落库后下次刷新还能复用）
    assert fields.get("dpop_private_key") == stored_pem


def test_invalid_jkt_maps_to_400_reauth_hint(channel_cls, monkeypatch):
    """refresh_token 绑的公钥指纹对不上 → 400（非 502）+ 提示重新网页登录授权。

    这是凭据不可恢复（不是上游故障）：refresh-auth 路径禁用 401/403（会被前端登录态
    拦截器当管理员登录过期踢出控制台），invalid jkt 归一成 400 更合适。
    """
    import fastapi

    class FakeResponse:
        status = 400

        async def text(self):
            return ('{"error_code":"STS5.1806","error_msg":'
                    '"invalid refresh token: \'invalid jkt: abc123\'"}')

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            return FakeResponse()

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    flow = _run(channel_cls(username="u", password="").begin_device_flow({"port": 8001}))
    pem = flow["poll_params"]["dpop_private_key"]
    p = channel_cls(username="u", password="",
                    refresh_token="rt", code_verifier="cv", dpop_private_key=pem)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    with pytest.raises(fastapi.HTTPException) as exc:
        _run(p.refresh_account_auth(
            {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": pem}))
    assert exc.value.status_code == 400
    assert "重新" in str(exc.value.detail)


def test_refresh_token_used_maps_to_400_with_rotation_hint(channel_cls, monkeypatch):
    """refresh_token 已被消耗（单次有效，轮换结果丢失）→ 400，且文案说清是轮换问题而非 jkt。

    回归 2026-09-29 实测：STS5.1806 是通用码，「has been used」和「invalid jkt」
    是两种病因——旧实现按码匹配把前者也报成 DPoP 私钥不匹配，误导排查方向。
    """
    import fastapi

    class FakeResponse:
        status = 400

        async def text(self):
            return ('{"error_code":"STS5.1806","error_msg":'
                    '"invalid refresh token: \'the refresh token has been used\'"}')

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            return FakeResponse()

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    flow = _run(channel_cls(username="u", password="").begin_device_flow({"port": 8001}))
    pem = flow["poll_params"]["dpop_private_key"]
    p = channel_cls(username="u", password="",
                    refresh_token="rt", code_verifier="cv", dpop_private_key=pem)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    with pytest.raises(fastapi.HTTPException) as exc:
        _run(p.refresh_account_auth(
            {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": pem}))
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "单次有效" in detail or "已失效" in detail
    assert "invalid jkt" not in detail, "has been used 不该报成 jkt 不匹配"


# ==================== 每日积分签到（PromptCenterService /v1/ops/*） ====================

def _json_body(data):
    """fake session 收到的 POST body 是 json.dumps 后的 bytes，断言前解码回 dict。"""
    import json as _json
    if isinstance(data, (bytes, bytearray)):
        return _json.loads(data.decode("utf-8"))
    return data


def _ops_session(*, seen, delivery, claim_resp, confirm_resp):
    """签到假 session：按 URL 分发 delivery / claim / confirm（都走 _signed_json_cred）。"""
    class FakeSession:
        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("calls", []).append((method.upper(), url, data))
            if "ops/delivery" in url:
                return _IdentityResp(delivery)
            if "ops/claim" in url:
                return _IdentityResp(claim_resp)
            if "ops/confirm" in url:
                return _IdentityResp(confirm_resp)
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    return FakeSession()


def _delivery_eligible_one():
    """活动列表：campaignId=1 可领 + 11 AUTO_GRANT（不该领）+ 4 已 CONFIRMED（不该领）。"""
    return {"code": 0, "message": "ok", "data": {"channel": "WEB", "items": [
        {"campaignId": 1, "title": "每日签到领1000 积分", "type": "USER_LOGIN",
         "benefitAmount": 1000, "claimable": True, "status": None},
        {"campaignId": 11, "title": "Web端和Agent对话1次", "type": "CUSTOM",
         "benefitAmount": 88, "claimable": False, "status": None},
        {"campaignId": 4, "title": "新用户注册送 4000 积分", "type": "NEW_USER_REGISTER",
         "benefitAmount": 4000, "claimable": False, "status": "CONFIRMED"},
    ]}}


def _credit_checkin_globals(channel_cls):
    return _spec_globals(channel_cls)


def test_credit_checkin_claims_eligible_manual_claims(channel_cls, monkeypatch):
    """只领 ELIGIBLE+claimable 的 MANUAL_CLAIM 活动：claim→confirm，AUTO_GRANT/CONFIRMED 跳过。"""
    seen: dict = {}
    p = _acct(channel_cls, "acct-A")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _ops_session(seen=seen, delivery=_delivery_eligible_one(),
                                     claim_resp={"code": 0, "message": "ok",
                                                 "data": {"status": "CLAIMED",
                                                          "totalAmount": 1000,
                                                          "campaignTitle": "每日签到领1000 积分"}},
                                     confirm_resp={"code": 0, "message": "ok",
                                                   "data": {"status": "CONFIRMED",
                                                            "totalAmount": 1000}}))

    r = _run(_credit_checkin_globals(channel_cls)["_credit_daily_checkin"](p, "AK", "SK", "ST"))

    assert r["status"] == "ok"
    assert r["total"] == 1000
    calls = seen["calls"]
    assert any("ops/delivery" in u for (_m, u, _d) in calls)
    claim_calls = [_json_body(d) for (m, u, d) in calls if "ops/claim" in u]
    assert len(claim_calls) == 1, "只该给 campaignId=1 发 claim"
    assert claim_calls[0]["campaignId"] == "1"
    assert any("ops/confirm" in u for (_m, u, _d) in calls)
    # 幂等键带毫秒时间戳（重复领取靠它幂等）
    assert claim_calls[0]["idempotentKey"].startswith("claim_1_")
    assert r["details"][0]["status"] == "ok"
    assert r["details"][0]["amount"] == 1000


def test_credit_checkin_noop_when_nothing_claimable(channel_cls, monkeypatch):
    """没有任何 ELIGIBLE 活动时：不发 claim，返回 noop（不算失败）。"""
    seen: dict = {}
    d = _delivery_eligible_one()
    d["data"]["items"][0]["claimable"] = False
    d["data"]["items"][0]["status"] = "CONFIRMED"
    p = _acct(channel_cls, "acct-A")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _ops_session(seen=seen, delivery=d,
                                     claim_resp={"code": 0}, confirm_resp={"code": 0}))

    r = _run(_credit_checkin_globals(channel_cls)["_credit_daily_checkin"](p, "AK", "SK", "ST"))

    assert r["status"] == "noop"
    assert r["total"] == 0
    assert not [data for (m, u, data) in seen["calls"] if "ops/claim" in u]


def test_credit_checkin_treats_40001_as_not_yet(channel_cls, monkeypatch):
    """上游回 40001（尚未到达权益刷新时间）：不算失败，也不发 confirm。"""
    seen: dict = {}
    p = _acct(channel_cls, "acct-A")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _ops_session(seen=seen, delivery=_delivery_eligible_one(),
                                     claim_resp={"code": 40001,
                                                 "message": "尚未到达权益刷新时间，暂时无法领取"},
                                     confirm_resp={"code": 0}))

    r = _run(_credit_checkin_globals(channel_cls)["_credit_daily_checkin"](p, "AK", "SK", "ST"))

    assert r["status"] == "noop"     # 没领成功 → 不算 ok
    assert r["details"][0]["status"] == "not_yet"
    assert not [data for (m, u, data) in seen["calls"] if "ops/confirm" in u]


def test_credit_checkin_delivery_failure_returns_error(channel_cls, monkeypatch):
    """delivery 拉取失败（HTTP 404）：返回 error，不抛异常。"""
    class BadSession:
        def request(self, method, url, headers=None, data=None, **_kw):
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    p = _acct(channel_cls, "acct-A")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: BadSession())

    r = _run(_credit_checkin_globals(channel_cls)["_credit_daily_checkin"](p, "AK", "SK", "ST"))
    assert r["status"] == "error"


def test_perform_action_credit_checkin_returns_badge_fields(channel_cls, monkeypatch):
    """credit_checkin 动作：换出新 STS 凭证后领积分，徽标字段回传给 admin 落库。"""
    seen: dict = {}

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            return _IdentityResp({"refresh_token": "rt-new",
                                  "credentials": {"access_key_id": "AK-NEW",
                                                  "secret_access_key": "SK-NEW",
                                                  "security_token": "ST-NEW",
                                                  "expiration": "2030-01-01T00:00:00Z"}})

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("calls", []).append((method.upper(), url, data))
            if "ops/delivery" in url:
                return _IdentityResp(_delivery_eligible_one())
            if "ops/claim" in url:
                return _IdentityResp({"code": 0, "message": "ok",
                                      "data": {"status": "CLAIMED", "totalAmount": 1000,
                                               "campaignTitle": "每日签到领1000 积分"}})
            if "ops/confirm" in url:
                return _IdentityResp({"code": 0, "message": "ok",
                                      "data": {"status": "CONFIRMED", "totalAmount": 1000}})
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    flow = _run(channel_cls(username="u", password="").begin_device_flow({"port": 8001}))
    pem = flow["poll_params"]["dpop_private_key"]
    p = channel_cls(username="acct-A", password="",
                    refresh_token="rt-old", code_verifier="cv", dpop_private_key=pem)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    fields = _run(p.perform_account_action(
        "credit_checkin",
        {"refresh_token": "rt-old", "code_verifier": "cv", "dpop_private_key": pem}))

    # 凭证刷新字段照旧
    assert fields["access_key_id"] == "AK-NEW"
    assert fields["refresh_token"] == "rt-new"
    # 签到徽标字段也回传（供 admin 落库 + 账号列表展示）
    assert fields.get("last_credit_checkin") == "已领 +1000"
    parts = (fields.get("last_credit_checkin_status") or "").split("|")
    assert len(parts) == 3, "状态形如 日期|ok|+1000"
    assert parts[1] == "ok" and parts[2] == "+1000"
    # 确实发起了签到
    assert any("ops/claim" in u for (_m, u, _d) in seen.get("calls", []))


def test_refresh_auth_includes_credit_checkin(channel_cls, monkeypatch):
    """refresh_auth = 复合入口：换新凭证 + 积分签到一起做（与 credit_checkin 动作同实现）。"""
    seen: dict = {}

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            seen.setdefault("calls", []).append(("POST", url, data))
            return _IdentityResp({"refresh_token": "rt-new",
                                  "credentials": {"access_key_id": "AK-NEW",
                                                  "secret_access_key": "SK-NEW",
                                                  "security_token": "ST-NEW",
                                                  "expiration": "2030-01-01T00:00:00Z"}})

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("calls", []).append((method.upper(), url, data))
            if "ops/delivery" in url:
                return _IdentityResp(_delivery_eligible_one())
            if "ops/claim" in url:
                return _IdentityResp({"code": 0, "message": "ok",
                                      "data": {"status": "CLAIMED", "totalAmount": 1000,
                                               "campaignTitle": "每日签到领1000 积分"}})
            if "ops/confirm" in url:
                return _IdentityResp({"code": 0, "message": "ok",
                                      "data": {"status": "CONFIRMED", "totalAmount": 1000}})
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    flow = _run(channel_cls(username="u", password="").begin_device_flow({"port": 8001}))
    pem = flow["poll_params"]["dpop_private_key"]
    p = channel_cls(username="acct-A", password="",
                    refresh_token="rt-old", code_verifier="cv", dpop_private_key=pem)
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    fields = _run(p.refresh_account_auth(
        {"refresh_token": "rt-old", "code_verifier": "cv", "dpop_private_key": pem}))

    assert fields["access_key_id"] == "AK-NEW"
    assert fields["refresh_token"] == "rt-new"
    assert fields.get("last_credit_checkin") == "已领 +1000", "刷新授权复合入口顺带签到"
    assert any("ops/claim" in u for (_m, u, _d) in seen.get("calls", []))


def test_credit_checkin_skipped_when_already_done_today(channel_cls, monkeypatch):
    """当日已领（last_credit_checkin_status=今天|ok|+1000）：判定为已领，跳过签到。"""
    g = _credit_checkin_globals(channel_cls)
    p = _acct(channel_cls, "acct-A")
    today = g["_today_key"]()

    # 今天已领 → 判已领
    p.last_credit_checkin_status = f"{today}|ok|+1000"
    assert g["_today_credit_done"](p) is True

    # 昨天领的 / 今天报错的 → 判未领（允许重试）
    p.last_credit_checkin_status = f"{today}|error"
    assert g["_today_credit_done"](p) is False
    p.last_credit_checkin_status = "2020-01-01|ok|+1000"
    assert g["_today_credit_done"](p) is False
    p.last_credit_checkin_status = ""
    assert g["_today_credit_done"](p) is False

    # credit_checkin 动作在已领时直接跳过：不打 ops、也不浪费一次 STS 刷新
    seen: dict = {}

    class FakeSession:
        def post(self, url, headers=None, data=None, **_kw):
            return _IdentityResp({"refresh_token": "rt-new",
                                  "credentials": {"access_key_id": "AK-NEW",
                                                  "secret_access_key": "SK-NEW",
                                                  "security_token": "ST-NEW",
                                                  "expiration": "2030-01-01T00:00:00Z"}})

        def request(self, method, url, headers=None, data=None, **_kw):
            seen.setdefault("calls", []).append((method.upper(), url, data))
            return _IdentityResp({}, status=404)

        async def __aenter__(self): return self
        async def __aexit__(self, *_a): return False

    flow = _run(channel_cls(username="u", password="").begin_device_flow({"port": 0}))
    pem = flow["poll_params"]["dpop_private_key"]
    p = channel_cls(username="acct-A", password="",
                    refresh_token="rt-old", code_verifier="cv", dpop_private_key=pem,
                    last_credit_checkin_status=f"{today}|ok|+1000")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k: FakeSession())

    # refresh_auth 复合入口在已领时直接跳过签到：不打 ops、凭证照常刷新
    fields = _run(p.refresh_account_auth(
        {"refresh_token": "rt-old", "code_verifier": "cv", "dpop_private_key": pem}))

    assert fields["access_key_id"] == "AK-NEW"    # 凭证照常刷新
    assert "last_credit_checkin" not in fields    # 已领 → 不回写徽标（保留原值）
    assert not [u for (_m, u, _d) in seen.get("calls", []) if "ops/" in u]


def test_credit_checkin_badge_fields_declared(channel_cls):
    """签到徽标字段必须在 schema + ACCOUNT_FIELDS + metadata_badges 里（整行替换会丢 schema 外键）；
    签到作为独立账号动作（credit_checkin）声明在 account_actions 里。"""
    fields = {f["key"]: f for f in channel_cls.account_schema()["fields"]}
    assert "last_credit_checkin" in fields
    assert fields["last_credit_checkin"].get("readonly") is True
    assert "last_credit_checkin_status" in fields
    assert {"last_credit_checkin", "last_credit_checkin_status"}.issubset(
        set(channel_cls.ACCOUNT_FIELDS))
    assert "last_credit_checkin" in channel_cls.account_schema()["metadata_badges"]
    actions = {a["id"]: a for a in channel_cls.account_schema()["account_actions"]}
    assert actions["credit_checkin"]["display"]["badge_field"] == "last_credit_checkin"
    assert actions["credit_checkin"]["display"]["status_field"] == "last_credit_checkin_status"
    # refresh_auth 是复合入口（已含签到，调度默认每天 10:00），
    # credit_checkin 动作只做「签到」按钮、不单独调度以免重复打上游。
    assert actions["credit_checkin"]["default"] == {"enabled": False,
                                                    "frequency_unit": "daily",
                                                    "frequency_value": 1}
    # sync_quota：额度动作（value 展示 quota_text，默认每 6 小时调度刷新展示）
    assert actions["sync_quota"]["display"]["value_field"] == "quota_text"
    assert actions["sync_quota"]["default"]["frequency_unit"] == "hours"
    assert actions["sync_quota"]["default"]["frequency_value"] == 6
    assert "quota_text" in fields, "额度字段必须在 schema 里（整行替换会丢）"
    assert "quota_text" in set(channel_cls.ACCOUNT_FIELDS)
    assert "quota_text" in channel_cls.account_schema()["metadata_badges"]
    # 积分余额字段（snap-manager /v1/statistics/plugin 实时拉，token 用完时回退显示）
    assert "credit_text" in fields, "积分字段必须在 schema 里（整行替换会丢）"
    assert "credit_text" in set(channel_cls.ACCOUNT_FIELDS)
    assert "credit_text" in channel_cls.account_schema()["metadata_badges"]


def test_sync_quota_writes_quota_text(channel_cls, monkeypatch):
    """sync_quota 动作：换出新凭证后拉余额，quota_text 写回（账号列表第二行显示它）。"""
    g = _credit_checkin_globals(channel_cls)
    p = _acct(channel_cls, "acct-A")

    async def fake_refresh(p_, rt, cv, dpk):
        return {"access_key_id": "AK", "secret_access_key": "SK", "security_token": "ST"}

    async def fake_balance(p_, ak, sk, token):
        assert (ak, sk, token) == ("AK", "SK", "ST")
        return {"balance": 1234567}

    saved = {n: g[n] for n in ("_refresh_credentials", "_fetch_benefit_balance", "_fetch_package_credit")}
    g["_refresh_credentials"] = fake_refresh
    g["_fetch_benefit_balance"] = fake_balance
    # 积分套餐不参与这两条用例 → 桩成空 dict（真实实现会发签名请求，测试里绝不能真打）
    g["_fetch_package_credit"] = lambda *a, **k: asyncio.sleep(0, result={})
    try:
        fields = _run(channel_cls.perform_account_action(
            p, "sync_quota",
            {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": "pem"}))
    finally:
        for n, fn in saved.items():
            g[n] = fn

    assert fields["quota_text"] == "1,234,567 tokens"


def test_format_benefit_quota_real_huawei_shape(channel_cls):
    """锁住 2026-09-30 实测的华为云 opengw /user/tokens/balance 真实返回形态：
    {create_time, daily_token_limit, daily_tokens_used, expire_time}。
    显示必须是「今日 X / Y（剩 Z）」，且绝不把毫秒时间戳等噪声拼进去。"""
    g = _credit_checkin_globals(channel_cls)
    fmt = g["_format_benefit_quota"]
    # 2026-09-30 真实返回（含 total_balance/total_quota）：优先显示「剩余 X / Y」
    real_full = {"channel": "codearts", "create_time": 1788251747904,
                 "daily_token_limit": 10000000, "daily_tokens_used": 10019652,
                 "monthly_token_limit": 0, "monthly_tokens_used": 30215488,
                 "total_balance": 2325571, "total_quota": 10000000, "expire_time": 0,
                 "used_amount": 10019652, "user_id": "u"}
    assert fmt(real_full) == "剩余 2,325,571 / 10,000,000 tokens"
    # 余额为 0（额度耗尽）：也要如实显示 0，不能当空值吞掉
    assert fmt({"total_balance": 0, "total_quota": 10000000}) == "剩余 0 / 10,000,000 tokens"
    # 只有 total_balance（无总额度）
    assert fmt({"total_balance": 4275394}) == "剩余 4,275,394 tokens"
    # 无 total_* 时回退 daily 形态
    real = {"create_time": 1788251747904, "daily_token_limit": 10000000,
            "daily_tokens_used": 1956851, "expire_time": 0}
    assert fmt(real) == "今日 1,956,851 / 10,000,000（剩 8,043,149）"
    # 只有上限
    assert fmt({"daily_token_limit": 10000000}) == "每日额度 10,000,000 tokens"
    # 纯噪声键（时间戳/id）→ 空，绝不显示
    assert fmt({"create_time": 1788251747904, "expire_time": 0}) == ""
    # 兜底：像额度的单键
    assert fmt({"balance": 1234567}) == "1,234,567 tokens"
    # 嵌套 dict / list 兜底
    assert fmt({"result": {"remaining": 5000, "total": 10000}}) == "remaining 5,000 · total 10,000"
    assert fmt({"items": [{"used": 1}, {"left": 2}]}) == "used 1 · left 2"
    # 无数值
    assert fmt({"error_code": "0", "message": "ok"}) == ""
    assert fmt(None) == "" and fmt({}) == ""

def test_format_package_credit_real_huawei_shape(channel_cls):
    """锁住 2026-09-30 实测的 snap-manager /v1/statistics/plugin 真实返回形态
    （VSCode 插件 SubscriptionService.getSubscriptionInfo 同源端点，HTTP 200）：
    metrics[] 里 usageTotalPackageCredit / usageBasicPackageCredit / usageBonusPackageCredit
    各含 package_credit_amount/used/remain，package 段带 package_name_cn。
    显示必须是「积分 剩 X / Y（基础 A + 赠送 B） · 套餐名」。"""
    g = _credit_checkin_globals(channel_cls)
    fmt = g["_format_package_credit"]
    real = {
        # 真机实测顶层还有 start_date/end_date（套餐有效期），end_date 是要展示的「积分过期时间」
        "start_date": "2026-09-29", "end_date": "2026-10-29",
        "package": {"package_name_cn": "体验版", "spec_code": "codearts.agent.individual.trial",
                    "status": "NORMAL", "is_credit_package": True},
        "metrics": [
            {"name": "usageTotalPackageCredit", "package_credit_amount": 5500.0,
             "package_credit_used": 0.0, "package_credit_remain": 5500.0},
            {"name": "usageBasicPackageCredit", "package_credit_amount": 500.0,
             "package_credit_used": 0.0, "package_credit_remain": 500.0},
            {"name": "usageBonusPackageCredit", "package_credit_amount": 5000.0,
             "package_credit_used": 0.0, "package_credit_remain": 5000.0},
        ],
    }
    assert fmt(real) == "积分 剩 5,500 / 5,500（基础 500 + 赠送 5,000） · 体验版 · 有效期至 2026-10-29"
    # remain 缺失 → 用 amount - used 兜底
    no_remain = {"end_date": "2026-10-29",
                 "package": {"package_name_cn": "体验版"},
                 "metrics": [{"name": "usageTotalPackageCredit",
                              "package_credit_amount": 5500.0, "package_credit_used": 1000.0}]}
    assert fmt(no_remain) == "积分 剩 4,500 / 5,500 · 体验版 · 有效期至 2026-10-29"
    # 没有 end_date：不显示「有效期至」这一段（拿不到不猜）
    no_end = {"metrics": [{"name": "usageTotalPackageCredit",
                           "package_credit_remain": 300.0, "package_credit_amount": 500.0}]}
    assert fmt(no_end) == "积分 剩 300 / 500"
    # 无 total 指标 / 无 metrics → 空（拿不到不写展示字段）
    assert fmt({"metrics": [{"name": "usageBasicPackageCredit", "package_credit_amount": 500.0}]}) == ""
    assert fmt({"package": {}}) == ""
    assert fmt({}) == "" and fmt(None) == ""


def test_credit_checkin_accumulates_credit_total(channel_cls, monkeypatch):
    """签到领取成功后本地累加 credit_total（上游 PromptCenterService 无积分余额接口）。

    只有真领到（status=ok 且 total>0）才累加；noop/失败不动 credit_total。
    """
    g = _credit_checkin_globals(channel_cls)
    p = _acct(channel_cls, "acct-A")

    async def fake_refresh(p_, rt, cv, dpk):
        return {"access_key_id": "AK", "secret_access_key": "SK", "security_token": "ST"}

    async def fake_checkin(p_, ak, sk, token):
        return {"status": "ok", "total": 2000, "details": []}

    saved = {n: g[n] for n in ("_refresh_credentials", "_credit_daily_checkin")}
    g["_refresh_credentials"] = fake_refresh
    g["_credit_daily_checkin"] = fake_checkin
    try:
        fields = _run(channel_cls.perform_account_action(
            p, "credit_checkin",
            {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": "pem",
             "credit_total": 5000}))

        # noop（今日无可领）：不动累计值
        p2 = _acct(channel_cls, "acct-B")

        async def fake_checkin_noop(p_, ak, sk, token):
            return {"status": "noop", "total": 0, "details": []}

        g["_credit_daily_checkin"] = fake_checkin_noop
        fields2 = _run(channel_cls.perform_account_action(
            p2, "credit_checkin",
            {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": "pem",
             "credit_total": 5000}))
    finally:
        for n, fn in saved.items():
            g[n] = fn

    assert fields["credit_total"] == 7000
    assert fields["last_credit_checkin"] == "已领 +2000"
    assert "credit_total" not in fields2


def test_sync_quota_balance_failure_keeps_credentials(channel_cls, monkeypatch):
    g = _credit_checkin_globals(channel_cls)
    p = _acct(channel_cls, "acct-A")

    async def fake_refresh(p_, rt, cv, dpk):
        return {"access_key_id": "AK", "secret_access_key": "SK", "security_token": "ST"}

    async def fake_balance(p_, ak, sk, token):
        return {}  # 上游字段名对不上 → _format_benefit_quota 返回空串

    saved = {n: g[n] for n in ("_refresh_credentials", "_fetch_benefit_balance", "_fetch_package_credit")}
    g["_refresh_credentials"] = fake_refresh
    g["_fetch_benefit_balance"] = fake_balance
    # 积分套餐不参与这两条用例 → 桩成空 dict（真实实现会发签名请求，测试里绝不能真打）
    g["_fetch_package_credit"] = lambda *a, **k: asyncio.sleep(0, result={})
    try:
        fields = _run(channel_cls.perform_account_action(
            p, "sync_quota",
            {"refresh_token": "rt", "code_verifier": "cv", "dpop_private_key": "pem"}))
    finally:
        for n, fn in saved.items():
            g[n] = fn

    assert "quota_text" not in fields
    assert fields["access_key_id"] == "AK"


# ==================== 原生 function calling（TOOLS_AS_PROMPT=False，2026-09-29 实测定版） ====================

def _chat_sse_capture(monkeypatch, p):
    """装一个假 send_sse_request：抓下 payload，yield 一发 [DONE] 收尾。"""
    import json as _json
    captured: dict = {}

    async def fake_sse(method, url, headers, **kw):
        captured["headers"] = dict(headers or {})
        captured["payload"] = _json.loads(
            (kw.get("data") or b"{}").decode("utf-8"))
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(p, "send_sse_request", fake_sse)
    return captured


def test_stream_chat_payload_carries_native_tools(channel_cls, monkeypatch):
    """原生路径：请求体带 tools；不再注入 tool_function 围栏 system。"""
    seen: dict = {}
    p = _acct(channel_cls, "acct-D")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))
    captured = _chat_sse_capture(monkeypatch, p)
    tools = [{"type": "function", "function": {
        "name": "get_time", "description": "取当前时间",
        "parameters": {"type": "object", "properties": {}}}}]

    _run_stream(p._do_stream_chat("GLM-5.2",
                                   [{"role": "user", "content": "hi"}], tools=tools))

    payload = captured["payload"]
    assert payload.get("tools") == tools, "原生路径必须把 tools 放进请求体"
    # 不再有「工具说明进 system」的旧路径产物
    sys_msgs = [m for m in payload["messages"] if m.get("role") == "system"]
    assert not any("tool_function" in str(m.get("content", "")) for m in sys_msgs)

    # 不带 tools：payload 不带该键（tools_for_payload(None) → 不写）
    captured.clear()
    _run_stream(p._do_stream_chat("GLM-5.2", [{"role": "user", "content": "hi"}]))
    assert "tools" not in captured["payload"]


def test_stream_chat_history_keeps_native_tool_structure(channel_cls, monkeypatch):
    """历史轮的 assistant.tool_calls / role=tool 结构原样保留，不拍平成文本。"""
    seen: dict = {}
    p = _acct(channel_cls, "acct-E")
    monkeypatch.setattr(type(p), "_make_session", lambda self, *a, **k:
                        _three_way_session(seen=seen))
    captured = _chat_sse_capture(monkeypatch, p)
    tools = [{"type": "function", "function": {
        "name": "get_time", "description": "取当前时间",
        "parameters": {"type": "object", "properties": {}}}}]
    history = [
        {"role": "user", "content": "现在几点？"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "07:42"},
    ]

    _run_stream(p._do_stream_chat("GLM-5.2", history, tools=tools))

    msgs = captured["payload"]["messages"]
    tc = (msgs[1].get("tool_calls") or [{}])[0]
    assert tc.get("id") == "call_1", f"历史 tool_calls 被拍平了: {msgs[1]}"
    assert msgs[2].get("role") == "tool"
    assert msgs[2].get("tool_call_id") == "call_1"


def test_parse_sse_event_forwards_native_tool_calls(channel_cls):
    """上游回的标准 delta.tool_calls 分片原样转发（聚合交给框架 _merge_tool_calls）。"""
    g = _spec_globals(channel_cls)
    snapshot = [""]
    event = ('data: {"choices":[{"index":0,"delta":{"tool_calls":[{'
             '"index":0,"id":"call_abc","type":"function",'
             '"function":{"name":"get_time","arguments":"{}"}}]},'
             '"finish_reason":null}],"usage":null}')
    frames = g["_parse_sse_event"](None, event, snapshot)
    tc_frames = [f for f in frames if f.get("tool_calls")]
    assert tc_frames, f"tool_calls 帧被丢弃了: {frames}"
    tc = tc_frames[0]["tool_calls"][0]
    assert tc["id"] == "call_abc"
    assert tc["function"]["name"] == "get_time"
    assert tc["function"]["arguments"] == "{}"


def test_attach_display_fields_extracts_desc_and_multiplier(channel_cls):
    """_attach_display_fields：上游 model_desc / credit[].ratio_display → 行顶层
    _model_desc / cost_multiplier；credit 为空数组或无 ratio_display 不塞倍率键。"""
    g = _spec_globals(channel_cls)
    rows = [
        {"id": "glm-5.2", "raw": {"model_desc": "基于GLM-5.2增训鸿蒙代码与开发知识",
                                  "credit": [{"ratio_display": "0.7x", "ratio": "0.05"}]}},
        {"id": "qwen3-vl", "raw": {"model_desc": "多模态模型", "credit": []}},
        {"id": "no-raw", "raw": {}},
    ]
    out = g["_attach_display_fields"](rows)
    assert out[0]["_model_desc"] == "基于GLM-5.2增训鸿蒙代码与开发知识"
    assert out[0]["cost_multiplier"] == 0.7
    assert out[1]["_model_desc"] == "多模态模型"
    assert "cost_multiplier" not in out[1]
    assert "_model_desc" not in out[2]


def test_merge_model_lists_fills_display_keys_across_sources(channel_cls):
    """说明只在内置那路（model_desc），agent-center 那路没有——合并要「谁有取谁」，
    不能先到先得把后到那路的说明整抹掉。各路先过 _attach_display_fields（fetch_models
    真实顺序），再合并。"""
    g = _spec_globals(channel_cls)
    center = g["_attach_display_fields"]([{"id": "GLM-5.2", "raw": {"model_name": "GLM-5.2"}}])
    builtin = g["_attach_display_fields"]([{"id": "glm-5.2", "raw": {"model_desc": "旗舰模型", "credit": [{"ratio_display": "0.7x"}]}}])
    merged = g["_merge_model_lists"](center, builtin)
    assert len(merged) == 1
    assert merged[0]["_model_desc"] == "旗舰模型"
    assert merged[0]["cost_multiplier"] == 0.7


def test_codearts_declares_model_info_columns(channel_cls):
    """account_schema 必须声明说明/倍率两列，管理端模型 tab 才渲染「其他信息」。"""
    schema = channel_cls.account_schema()
    cols = {c["key"]: c for c in schema.get("model_info_columns", [])}
    assert "_model_desc" in cols
    assert cols["cost_multiplier"]["format"] == "multiplier"
