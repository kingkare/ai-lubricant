"""InkStone 渠道 spec（specs/inkstone_code_channel.py）的契约测试。

锁住新模型的三条核心链路，防后续改框架钩子模型时悄悄破坏：
1. spec 能被 code_loader 加载（普通类 + 可识别钩子）+ 类级开关/账号字段声明；
2. **登录由工作流负责**——spec 不做登录（无 refresh_auth、无 site 委托）；
   token 从 Redis 读（``_read_redis_token`` 双键防御 + refresh_token 联动）；
3. 服务端会话聊天（SSE 两形态解析、单条 query 提取、chat_id 跨轮复用、聊天只读 token 不触发登录）。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "inkstone_code_channel.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/inkstone_code_channel.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("inkstone-test", _SOURCE)
    yield cls
    invalidate_cache("inkstone-test")


@pytest.fixture
def module():
    """每测一份全新 spec 模块（测试会改写模块级 JdbcClient，须隔离）。"""
    return _module()


def _module():
    """加载 spec 模块本身（拿模块级 helper 与模块级 ``JdbcClient`` 名）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("inkstone_spec_module", _SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_provider(channel_cls, *, username="user@test.com", password="pw", **extra):
    return channel_cls(username=username, password=password, _api_key="", **extra)


class _StubChannel:
    def __init__(self, **raw):
        self._raw = raw

    def get(self, key, default=None):
        return self._raw.get(key, default)


def _make_jwt(exp: int) -> str:
    """造一个带 exp 的 JWT（HS512 头 + 任意 payload + 假签名；本 spec 不验签只解 exp）。"""
    head = base64.urlsafe_b64encode(json.dumps({"alg": "HS512"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{head}.{body}.fakesig"


# ==================== 加载与声明 ====================

def test_spec_loads_with_expected_hooks(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {
        "account_schema", "is_init", "init_auth", "check_auth", "health_check",
        "fetch_models", "stream_chat", "headers", "base_url",
    }.issubset(hooks)
    # 不做登录：refresh_auth 不在钩子里
    assert "refresh_auth" not in hooks


def test_spec_class_flags(channel_cls):
    # 上游按 chat_id 自管历史 → 不发多轮 messages
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is False
    # 登录由工作流负责，spec 不自愈 token / 不纳入账号动作调度
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is False
    assert channel_cls.SCHEDULED_REFRESH is False
    # 域名写死兜底 → 不要求必填渠道地址
    assert channel_cls.REQUIRES_BASE_URL is False
    # 自带浏览器 UA → 关平台伪装头
    assert channel_cls.APPLY_CLIENT_PRESET is False


def test_account_fields_declared(channel_cls):
    # 登录站点字段已删（site 概念下线）；refresh_token 联动支持
    assert channel_cls.ACCOUNT_FIELDS == ("access_token", "refresh_token", "token_expires_at")


def test_account_schema_shape(channel_cls):
    schema = channel_cls.account_schema()
    keys = {f["key"] for f in schema["fields"]}
    assert {"username", "access_token", "refresh_token", "token_expires_at"}.issubset(keys)
    # 站点相关字段已删
    assert not ({"password", "login_site_id", "login_site_token", "login_client_id"} & keys)
    assert schema["auth_start"]["enabled"] is False  # 纯手填，无设备码/回调
    # 不再有 refresh_auth 账号动作（续期交工作流）
    assert schema.get("account_actions") == []


# ==================== 认证判定（不做登录）====================

def test_is_init_token_present(channel_cls):
    """有 access_token 即视为已配置。"""
    p = _make_provider(channel_cls)
    p.access_token = "tok"
    assert channel_cls.is_init(p) is True


def test_is_init_only_username(channel_cls):
    """没 token 但有账户名（等登录工作流写 Redis 补）也算已配置。"""
    p = _make_provider(channel_cls)
    p.access_token = ""
    assert channel_cls.is_init(p) is True


def test_init_auth_no_autologin(channel_cls):
    """init_auth 只从 Redis 刷 token，绝不触发登录。"""
    import time
    p = _make_provider(channel_cls)
    p.access_token = _make_jwt(int(time.time()) + 999999)
    assert asyncio.run(channel_cls.init_auth(p, is_check=True)) is True
    # 无 token → False（不抛；等登录工作流补）
    p.access_token = ""
    assert asyncio.run(channel_cls.init_auth(p, is_check=True)) is False
    assert asyncio.run(channel_cls.init_auth(p, is_check=False)) is False


# ==================== Redis token 读取（双键 + refresh 联动）====================

class _FakeRedis:
    """记调用顺序、按 table 返回。模拟 JdbcClient.redis.get。"""

    def __init__(self, table):
        self.table = table
        self.calls = []

    async def get(self, key):
        self.calls.append(key)
        return self.table.get(key)


class _FakeJdbc:
    def __init__(self, table):
        self.redis = _FakeRedis(table)


def test_read_redis_token_json_object(module):
    mod = module
    table = {"inkstone:token:u": json.dumps({
        "access_token": "tok-1", "refresh_token": "rt-1", "token_expires_at": "2026-10-25T00:00:00+00:00",
    })}
    mod.JdbcClient = _FakeJdbc(table)
    out = asyncio.run(mod._read_redis_token("u"))
    assert out == {"access_token": "tok-1", "refresh_token": "rt-1",
                   "token_expires_at": "2026-10-25T00:00:00+00:00"}
    # 主键命中即止，不兜底读 wf: 键
    assert mod.JdbcClient.redis.calls == ["inkstone:token:u"]


def test_read_redis_token_raw_string(module):
    mod = module
    mod.JdbcClient = _FakeJdbc({"inkstone:token:u": "raw-tok"})
    out = asyncio.run(mod._read_redis_token("u"))
    assert out == {"access_token": "raw-tok"}


def test_read_redis_token_falls_back_to_wf_key(module):
    """主键 miss → 兜底读 wf: 形态（docstring 口径）。"""
    mod = module
    table = {"wf:inkstone:token:u": json.dumps({"access_token": "wf-tok"})}
    mod.JdbcClient = _FakeJdbc(table)
    out = asyncio.run(mod._read_redis_token("u"))
    assert out == {"access_token": "wf-tok"}
    assert mod.JdbcClient.redis.calls == ["inkstone:token:u", "wf:inkstone:token:u"]


def test_read_redis_token_empty(module):
    mod = module
    mod.JdbcClient = _FakeJdbc({})
    assert asyncio.run(mod._read_redis_token("u")) == {}


def test_read_redis_token_no_jdbc(module):
    """Redis 不可用 → 返回 {} 不抛。"""
    mod = module
    mod.JdbcClient = None
    assert asyncio.run(mod._read_redis_token("u")) == {}


class _P:
    """桩 provider：带账户字段属性 + redis_prefix + _make_session/send_sse_request 之外都不用。"""

    def __init__(self, username="u", access_token="", refresh_token="", token_expires_at=""):
        self.username = username
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at


def test_sync_token_carries_refresh_and_expires(module):
    mod = module
    import time
    new_tok = _make_jwt(int(time.time()) + 13 * 86400)
    table = {"inkstone:token:u": json.dumps({
        "access_token": new_tok, "refresh_token": "rt-2", "token_expires_at": "2026-10-25T00:00:00+00:00",
    })}
    mod.JdbcClient = _FakeJdbc(table)
    p = _P(username="u")
    asyncio.run(mod._sync_token(p))
    assert p.access_token == new_tok
    assert p.refresh_token == "rt-2"
    assert p.token_expires_at == "2026-10-25T00:00:00+00:00"


def test_sync_token_derives_expires_from_jwt_when_missing(module):
    mod = module
    import time
    new_exp = int(time.time()) + 13 * 86400
    new_tok = _make_jwt(new_exp)
    mod.JdbcClient = _FakeJdbc({"inkstone:token:u": json.dumps({"access_token": new_tok})})
    p = _P(username="u")
    asyncio.run(mod._sync_token(p))
    assert p.access_token == new_tok
    from datetime import datetime, timezone
    assert p.token_expires_at == datetime.fromtimestamp(new_exp, tz=timezone.utc).replace(microsecond=0).isoformat()


def test_sync_token_keeps_existing_when_redis_empty(module):
    mod = module
    mod.JdbcClient = _FakeJdbc({})
    p = _P(username="u", access_token="old", refresh_token="oldrt", token_expires_at="old-exp")
    asyncio.run(mod._sync_token(p))
    assert p.access_token == "old"
    assert p.refresh_token == "oldrt"
    assert p.token_expires_at == "old-exp"


def test_sync_token_no_username_is_noop(module):
    mod = module
    mod.JdbcClient = _FakeJdbc({"inkstone:token:u": json.dumps({"access_token": "x"})})
    p = _P(username="")
    asyncio.run(mod._sync_token(p))
    assert p.access_token == ""


# ==================== SSE 帧解析（两形态）====================

def test_parse_sse_frame_with_event_line():
    mod = _module()
    frame = 'event: message\ndata: {"type":"answer","delta":"你好"}\n\n'
    event, data = mod._parse_sse_frame(frame)
    assert event == "message"
    assert data["type"] == "answer" and data["delta"] == "你好"


def test_parse_sse_frame_fallback_fields():
    """实测形态：thinking/answer 直接是字段（无 type/delta）。"""
    mod = _module()
    frame = 'event: message\ndata: {"cid":485682,"thinking":"用户问："}\n\n'
    event, data = mod._parse_sse_frame(frame)
    assert event == "message"
    assert data["thinking"] == "用户问："


def test_parse_sse_frame_bad_json_is_tolerated():
    mod = _module()
    frame = "event: workflow_finished\ndata: {broken\n\n"
    event, data = mod._parse_sse_frame(frame)
    assert event == "workflow_finished"
    assert data == {}  # 不崩


# ==================== 单条 query 提取 ====================

def test_latest_query_takes_last_user_text():
    mod = _module()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": [{"type": "text", "text": "第二问"}]},
    ]
    assert mod._latest_query(msgs) == "第二问"


def test_latest_query_falls_back_to_last_non_system():
    mod = _module()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "content": "工具结果"},
    ]
    assert mod._latest_query(msgs) == "工具结果"


def test_latest_query_empty_messages():
    mod = _module()
    assert mod._latest_query([]) == ""
    assert mod._latest_query(None) == ""


# ==================== fetch_models ====================

def test_fetch_models_shapes(channel_cls):
    """models 接口返回形态映射成框架模型条目。"""
    mod = _module()
    p = _make_provider(channel_cls)
    p._channel = _StubChannel(base_url="https://discovery.intern-ai.org.cn")
    # fetch_models 会先 _sync_token；测试环境 JdbcClient 拿真类但 redis=None → no-op
    mod.JdbcClient = _FakeJdbc({})

    class _Resp:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def json(self):
            return {"code": 0, "data": {"models": [
                {"name": "GLM-5.2", "tags": [], "icon": ""},
                {"name": "Qwen3.8-Max"},
                {"name": "GLM-5.2"},  # 重复应被去重
            ]}}

    class _Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def get(self, *a, **k):
            return _Resp()

    p._make_session = lambda *a, **k: _Session()
    models = asyncio.run(mod.InkstoneChannel.fetch_models(p))
    ids = [m["id"] for m in models]
    assert ids == ["GLM-5.2", "Qwen3.8-Max"]
    assert models[0]["name"] == "GLM-5.2"


def test_fetch_models_http_error_sets_last_error(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls)
    p._channel = _StubChannel(base_url="https://x")
    mod.JdbcClient = _FakeJdbc({})

    class _Resp:
        status = 401
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def text(self):
            return "unauthorized"

    class _Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def get(self, *a, **k):
            return _Resp()

    p._make_session = lambda *a, **k: _Session()
    models = asyncio.run(mod.InkstoneChannel.fetch_models(p))
    assert models == []
    assert "401" in p._last_fetch_models_error


# ==================== 聊天：请求体组装 ====================

def test_stream_chat_payload_and_chat_id_reuse(channel_cls):
    """stream_chat 用单条 query + chat_id；同 client_session_id 第二轮复用 chat_id。

    且**不触发登录**——token 死了就 401 交框架；续期由登录工作流负责。
    """
    mod = _module()
    p = _make_provider(channel_cls, username="u", password="pw")
    p.access_token = _make_jwt(__import__("time").time() + 999999)
    p._channel = _StubChannel(base_url="https://discovery.intern-ai.org.cn")
    # stream_chat 会先 _sync_token；桩 Redis 为空 → 保留 p.access_token
    mod.JdbcClient = _FakeJdbc({})

    new_chat_calls = []

    async def _fake_new_chat(pp):
        new_chat_calls.append(1)
        return 485682

    mod._new_chat = _fake_new_chat

    sent_bodies = []

    async def _fake_sse(method, url, headers, **kw):
        sent_bodies.append(kw.get("json"))
        # 上游 SSE 帧：workflow_started → message(answer) → message_end
        yield 'event: workflow_started\ndata: {"cid":485682}\n\n'
        yield 'event: message\ndata: {"type":"answer","delta":"科学"}\n\n'
        yield 'event: message_end\ndata: {"cid":485682}\n\n'

    p.send_sse_request = _fake_sse

    msgs = [{"role": "user", "content": "什么是科学计算"}]
    kwargs = {"client_session_id": "sess-1"}

    async def _collect():
        return [f async for f in mod.InkstoneChannel.stream_chat(p, "GLM-5.2", msgs, **kwargs)]

    frames = asyncio.run(_collect())
    # 首帧空字典（上游已接通）
    assert frames[0] == {}
    # 有正文帧
    assert any(f.get("content") == "科学" for f in frames)
    # 请求体：单条 query、带 chat_id、无 messages 数组
    body = sent_bodies[0]
    assert body["query"] == "什么是科学计算"
    assert body["chat_id"] == 485682
    assert body["model"] == "GLM-5.2"
    assert "messages" not in body
    # 首轮 new_chat 只调一次（chat_id 复用走 redis）
    assert len(new_chat_calls) == 1
