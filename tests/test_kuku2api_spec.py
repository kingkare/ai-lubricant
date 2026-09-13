"""kuku2api spec（specs/kuku2api.py）的翻译契约。

spec 移植自 kuku2api（https://github.com/xinxinshuhao-create/kuku2api，单文件 Python 网关）——
本测试锁住移植层的行为契约，防止后续改框架钩子模型时悄悄破坏：

1. spec 能被 code_loader 加载（普通类 + 可识别钩子）；
2. Cookie 归一（kuku_cookies.json 的 {"cookies":[{name,value}]} 形态 / 裸串）与 BDUSS 提取；
3. 模型解析对齐 resolve_model（kuku/<name> 剥前缀、未知回退 auto）；
4. prompt 拍平对齐 build_prompt（system/assistant 中文标签拼段）；
5. init_auth 换三件套（userreport errno==0）→ 落库；Cookie 缺 BDUSS 直接 False；
6. stream_chat 全链路：sendmsg 建会话 → idallochstr / sessionswitch（容错）→ SSE 翻译；
   SSE 帧只认 TEXT_BLOCK_DELTA.delta，REPLY_END/DIALOGUE_END/ERROR 终止；
7. check_message 非空判定；fetch_models 静态快照（auto 裸名 + kuku/<id>）。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "kuku2api.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/kuku2api.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("kuku2api-test", _SOURCE)
    yield cls
    invalidate_cache("kuku2api-test")


def _module():
    """在 code_loader 的注入命名空间里 exec spec 源码，拿模块级 helper。

    模块级 helper（``_parse_sse_frames`` / ``_post_json``）用了注入名
    （``logger`` / ``HTTPException``），裸 exec 会 NameError，必须走真实命名空间。
    """
    import sys
    from types import ModuleType
    from providers.code_loader import _build_namespace
    ns = _build_namespace()
    ns["__name__"] = "kuku2api_spec_module"
    exec(compile(_SOURCE, str(_SPEC_PATH), "exec"), ns)
    mod = ModuleType("kuku2api_spec_module")
    mod.__dict__.update(ns)
    sys.modules["kuku2api_spec_module"] = mod
    return mod


class _StubChannel:
    """最小渠道 stub：模拟 _channel_get 读原始配置键。"""

    def __init__(self, **raw):
        self._raw = raw

    def __getattr__(self, name):
        return self._raw.get(name, "")


def _make_provider(channel_cls, *, cookies="BDUSS=abc; STOKEN=xyz", base_url="",
                   **extra):
    """池外实例（不挂渠道）；base_url 非空时挂 stub _channel。"""
    p = channel_cls(username="test", password="", cookies=cookies, **extra)
    if base_url:
        p._channel = _StubChannel(base_url=base_url)
    return p


class _FakeResponse:
    def __init__(self, status, text):
        self.status = status
        self._text = text

    async def text(self):
        return self._text


class _Ctx:
    """异步上下文管理器（session.post/get 返回它，包住 FakeResponse）。"""

    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeJsonSession:
    """POST/GET 都返回编排好的 JSON 文本；记录调用供断言。"""

    def __init__(self, responses=None, userreport=None):
        # responses: list[(status, text)] 供 post 依序消费；userreport: (status, text)
        self._responses = list(responses or [])
        self._userreport = userreport
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None, proxy=None, timeout=None):
        self.calls.append(("POST", url, json))
        resp = self._responses.pop(0) if self._responses else (200, "{}")
        return _Ctx(_FakeResponse(resp[0], resp[1]))

    def get(self, url, headers=None, proxy=None, timeout=None):
        self.calls.append(("GET", url, None))
        resp = self._userreport or (200, json.dumps({"errno": 0, "data": {}}))
        return _Ctx(_FakeResponse(resp[0], resp[1]))


# ==================== 加载与声明 ====================

def test_spec_loads_with_expected_hooks(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {
        "account_schema", "is_init", "init_auth", "check_auth", "health_check",
        "refresh_auth", "fetch_models", "stream_chat", "check_message",
    }.issubset(hooks)
    # 没写 non_stream_chat：框架自动聚合。
    assert "non_stream_chat" not in hooks


def test_class_flags(channel_cls):
    # 上游无多轮接续，历史拍平在 spec 内做。
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is False
    # 三件套会过期可自愈；Cookie 长期失效需重导。
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    # 上游网页端按自家浏览器头校验，必须关伪装头。
    assert channel_cls.APPLY_CLIENT_PRESET is False


def test_account_schema_declares_cookie_field(channel_cls):
    schema = channel_cls.account_schema()
    assert schema["provider_name"] == "kuku2api-test"
    keys = {f["key"] for f in schema["fields"]}
    assert "cookies" in keys
    # 无浏览器登录授权入口（Cookie 由 kuku_login.py 外部导出）。
    assert schema["auth_start"]["enabled"] is False


# ==================== Cookie 归一 / BDUSS ====================

def test_normalize_cookie_accepts_exported_json():
    mod = _module()
    raw = json.dumps({"cookies": [
        {"name": "BDUSS", "value": "v1"},
        {"name": "STOKEN", "value": "v2"},
        {"name": "", "value": "skip"},          # 无名跳过
        {"name": "NOVAL", "value": ""},          # 无值跳过
    ]})
    assert mod._normalize_cookie(raw) == "BDUSS=v1; STOKEN=v2"


def test_normalize_cookie_accepts_bare_string():
    mod = _module()
    assert mod._normalize_cookie(" BDUSS=v1; STOKEN=v2 ") == "BDUSS=v1; STOKEN=v2"
    # 无 '=' 的片段被丢弃
    assert mod._normalize_cookie("BDUSS=v1; junk") == "BDUSS=v1"
    # 完全无 name=value 时原样返回（让上游如实报错）
    assert mod._normalize_cookie("garbage") == "garbage"
    assert mod._normalize_cookie("") == ""


def test_extract_bduss():
    mod = _module()
    assert mod._extract_bduss("BDUSS=abc; STOKEN=x") == "abc"
    assert mod._extract_bduss("STOKEN=x; BDUSS=abc") == "abc"
    assert mod._extract_bduss("STOKEN=x") == ""
    assert mod._extract_bduss("") == ""


# ==================== 模型解析 / prompt ====================

def test_resolve_model_strips_prefix_and_falls_back(channel_cls):
    mod = _module()
    assert mod._resolve_model("kuku/glm-5.3") == "glm-5.3"
    assert mod._resolve_model("glm-5.3") == "glm-5.3"
    assert mod._resolve_model("auto") == "auto"
    # 未知模型回退 auto（上游静默降级，显式回退更清晰）
    assert mod._resolve_model("unknown-model") == "auto"
    assert mod._resolve_model("") == "auto"


def test_build_prompt_flattens_with_labels(channel_cls):
    mod = _module()
    # 单条直接取内容
    assert mod.build_prompt([{"role": "user", "content": "hi"}]) == "hi"
    # 空 → 兜底
    assert mod.build_prompt([]) == "你好"
    # 多条：system/assistant 加标签，user 原样
    msgs = [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    assert mod.build_prompt(msgs) == (
        "[系统指令] be nice\n\nq1\n\n[助手之前的回复] a1\n\nq2"
    )


def test_build_prompt_handles_block_content(channel_cls):
    mod = _module()
    msgs = [{"role": "user", "content": [{"type": "text", "text": "part1"},
                                         {"type": "text", "text": "part2"}]}]
    assert mod.build_prompt(msgs) == "part1\npart2"


# ==================== 认证：is_init / init_auth / refresh_auth ====================

def test_is_init_requires_bduss(channel_cls):
    assert channel_cls.is_init(_make_provider(channel_cls, cookies="BDUSS=x")) is True
    assert channel_cls.is_init(_make_provider(channel_cls, cookies="STOKEN=x")) is False
    assert channel_cls.is_init(_make_provider(channel_cls, cookies="")) is False


def test_init_auth_exchanges_tokens_and_persists(channel_cls, monkeypatch):
    mod = _module()
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    userreport = json.dumps({"errno": 0, "data": {"bdstoken": "bt", "uinfo": "ui", "uk": 42}})
    session = _FakeJsonSession(userreport=(200, userreport))
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)

    persisted = {}

    async def fake_persist(fields):
        persisted.update(fields)
        return True

    monkeypatch.setattr(p, "persist_account_fields", fake_persist)

    ok = asyncio.run(channel_cls.init_auth(p))
    assert ok is True
    assert p.bdstoken == "bt" and p.uinfo == "ui" and p.uk == 42
    assert persisted["bdstoken"] == "bt"
    # 换 token 的 userreport 端点不打三件套（此时还没有 token）
    assert "userreport" in session.calls[0][1]


def test_init_auth_false_without_bduss(channel_cls, monkeypatch):
    p = _make_provider(channel_cls, cookies="STOKEN=only", base_url="https://kuku.baidu.com")
    called = []
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: called.append(1))
    assert asyncio.run(channel_cls.init_auth(p)) is False
    assert called == []  # 无 BDUSS 时不该打上游


def test_init_auth_false_on_upstream_errno(channel_cls, monkeypatch):
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    session = _FakeJsonSession(userreport=(200, json.dumps({"errno": 1000})))
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)
    assert asyncio.run(channel_cls.init_auth(p)) is False


def test_refresh_auth_returns_narrowed_fields(channel_cls, monkeypatch):
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    userreport = json.dumps({"errno": 0, "data": {"bdstoken": "bt2", "uinfo": "ui2", "uk": 7}})
    session = _FakeJsonSession(userreport=(200, userreport))
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)

    fields = asyncio.run(channel_cls.spec.refresh_auth(p, {"cookies": p.cookies}, {}))
    assert fields["bdstoken"] == "bt2" and fields["uinfo"] == "ui2" and fields["uk"] == 7
    assert "token_ts" in fields
    # 回写实例
    assert p.bdstoken == "bt2" and p.uk == 7


def test_refresh_auth_empty_on_bad_cookie(channel_cls, monkeypatch):
    p = _make_provider(channel_cls, cookies="STOKEN=x", base_url="https://kuku.baidu.com")
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: _FakeJsonSession())
    assert asyncio.run(channel_cls.spec.refresh_auth(p, {}, {})) == {}


# ==================== check_message / fetch_models ====================

def test_check_message_requires_nonempty(channel_cls):
    p = _make_provider(channel_cls)
    assert asyncio.run(channel_cls.check_message(p, "auto", [])) is False
    assert asyncio.run(channel_cls.check_message(p, "auto",
                                                  [{"role": "user", "content": ""}])) is False
    assert asyncio.run(channel_cls.check_message(p, "auto",
                                                 [{"role": "user", "content": "hi"}])) is True


def test_fetch_models_static_snapshot(channel_cls):
    p = _make_provider(channel_cls)
    models = asyncio.run(channel_cls.spec.fetch_models(p))
    ids = [m["id"] for m in models]
    assert ids[0] == "auto"
    assert "kuku/glm-5.3" in ids
    assert "glm-5.3" not in ids  # auto 之外一律 kuku/ 前缀
    # 无重复
    assert len(ids) == len(set(ids))


# ==================== SSE 帧翻译 ====================

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def test_parse_sse_frames_text_block_delta():
    mod = _module()
    frames = mod._parse_sse_frames(_sse({"type": "TEXT_BLOCK_DELTA", "data": {"delta": "hi"}}))
    assert frames == [{"content": "hi", "thinking": "", "tool_calls": []}]
    # 空 delta / 非文本事件不产帧
    assert mod._parse_sse_frames(_sse({"type": "TEXT_BLOCK_DELTA", "data": {}})) == []
    assert mod._parse_sse_frames(_sse({"type": "OTHER", "data": {"delta": "x"}})) == []


def test_parse_sse_frames_terminators():
    mod = _module()
    for t in ("REPLY_END", "DIALOGUE_END", "ERROR"):
        frames = mod._parse_sse_frames(_sse({"type": t}))
        assert frames == [mod._DONE], t
    # 脏行 / 注释行忽略
    assert mod._parse_sse_frames(": keep-alive\n\n") == []
    assert mod._parse_sse_frames("data: not-json\n\n") == []


# ==================== 端到端：stream_chat（mock 上游）====================

def test_stream_chat_happy_path(channel_cls, monkeypatch):
    """sendmsg → idallochstr → sessionswitch → SSE；逐帧 content 产出。"""
    mod = _module()
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    # 预置三件套，跳过刷新
    p.bdstoken, p.uinfo, p.uk, p.token_ts = "bt", "ui", 1, 9e18

    sendmsg = json.dumps({"status": {"code": 0},
                          "data": {"session_id": "sess-1", "reply_id": "rep-1"}})
    session = _FakeJsonSession(responses=[
        (200, sendmsg),               # sendmsg
        (200, "{}"),                  # idallochstr
        (200, "{}"),                  # sessionswitch
    ])
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)

    sse_events = [
        _sse({"type": "TEXT_BLOCK_DELTA", "data": {"delta": "He"}}),
        _sse({"type": "TEXT_BLOCK_DELTA", "data": {"delta": "llo"}}),
        _sse({"type": "REPLY_END"}),
    ]

    captured = {}

    async def fake_send_sse(method, url, headers, **kw):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = kw.get("json")
        for ev in sse_events:
            yield ev

    monkeypatch.setattr(p, "send_sse_request", fake_send_sse)

    async def _collect():
        out = []
        async for f in channel_cls._do_stream_chat(p, "kuku/glm-5.3",
                                                   [{"role": "user", "content": "hi"}]):
            out.append(f)
        return out

    frames = asyncio.run(_collect())
    assert frames[0] == {}  # 上游接通通知
    assert "".join(f.get("content") or "" for f in frames) == "Hello"
    # 三步握手 URL
    urls = [c[1] for c in session.calls]
    assert any("/wenchain/genflowpro/sendmsg" in u for u in urls)
    assert any("/wenchain/genflow/idallochstr" in u for u in urls)
    assert any("/workspace/sessionswitch" in u for u in urls)
    # SSE URL + 头
    assert captured["method"] == "POST"
    assert "/wenchain/genflowpro/sse/getchatcontent" in captured["url"]
    assert captured["headers"]["Accept"] == "text/event-stream"
    # SSE body 带 session/reply
    assert captured["body"]["session_id"] == "sess-1" and captured["body"]["reply_id"] == "rep-1"
    # sendmsg body 模型名归一（kuku/glm-5.3 -> glm-5.3）
    sendmsg_body = next(c[2] for c in session.calls if "sendmsg" in c[1])
    assert sendmsg_body["data"]["model_name"] == "glm-5.3"


def test_stream_chat_handshake_errors_are_tolerated(channel_cls, monkeypatch):
    """idallochstr / sessionswitch 失败不阻断 SSE（对齐原项目容错）。"""
    mod = _module()
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    p.bdstoken, p.token_ts = "bt", 9e18

    sendmsg = json.dumps({"status": {"code": 0},
                          "data": {"session_id": "s", "reply_id": "r"}})
    session = _FakeJsonSession(responses=[
        (200, sendmsg),
        (500, "boom"),        # idallochstr 失败
        (500, "boom"),        # sessionswitch 失败
    ])
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)

    async def fake_send_sse(method, url, headers, **kw):
        yield _sse({"type": "TEXT_BLOCK_DELTA", "data": {"delta": "ok"}})
        yield _sse({"type": "DIALOGUE_END"})

    monkeypatch.setattr(p, "send_sse_request", fake_send_sse)

    async def _collect():
        return [f async for f in channel_cls._do_stream_chat(p, "auto", [{"role": "user", "content": "x"}])]

    frames = asyncio.run(_collect())
    assert "".join(f.get("content") or "" for f in frames) == "ok"


def test_stream_chat_raises_on_sendmsg_failure(channel_cls, monkeypatch):
    from fastapi import HTTPException
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    p.bdstoken, p.token_ts = "bt", 9e18
    session = _FakeJsonSession(responses=[(200, json.dumps({"status": {"code": 1}}))])
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)

    async def _collect():
        return [f async for f in channel_cls._do_stream_chat(p, "auto", [{"role": "user", "content": "x"}])]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_collect())
    assert "sendmsg" in str(exc.value.detail)


def test_stream_chat_requires_cookie(channel_cls, monkeypatch):
    from fastapi import HTTPException
    p = _make_provider(channel_cls, cookies="STOKEN=x", base_url="https://kuku.baidu.com")
    async def _collect():
        return [f async for f in channel_cls._do_stream_chat(p, "auto", [{"role": "user", "content": "x"}])]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_collect())
    assert exc.value.status_code == 401


def test_stream_chat_refreshes_when_token_missing(channel_cls, monkeypatch):
    """无三件套时先 userreport 换出，再走握手。"""
    p = _make_provider(channel_cls, base_url="https://kuku.baidu.com")
    # 无 bdstoken
    userreport = json.dumps({"errno": 0, "data": {"bdstoken": "bt", "uinfo": "ui", "uk": 5}})
    session = _FakeJsonSession(
        responses=[(200, json.dumps({"status": {"code": 0},
                                     "data": {"session_id": "s", "reply_id": "r"}}))],
        userreport=(200, userreport),
    )
    monkeypatch.setattr(p, "_make_session", lambda timeout=None: session)

    async def fake_persist(fields):
        return True
    monkeypatch.setattr(p, "persist_account_fields", fake_persist)

    async def fake_send_sse(method, url, headers, **kw):
        yield _sse({"type": "REPLY_END"})

    monkeypatch.setattr(p, "send_sse_request", fake_send_sse)

    async def _collect():
        return [f async for f in channel_cls._do_stream_chat(p, "auto", [{"role": "user", "content": "x"}])]

    frames = asyncio.run(_collect())
    assert frames[0] == {}
    assert p.bdstoken == "bt"  # 刷新后写入
    assert any(c[0] == "GET" for c in session.calls)  # userreport 走 GET
