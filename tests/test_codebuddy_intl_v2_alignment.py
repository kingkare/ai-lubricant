"""WorkBuddy intl 档案（specs/codebuddy_code_channel.py · DEPLOY_REGION="intl"）出站整形契约。

2026-10-20 合并定版：CN/Intl 拆分版已删除（备份 deleted_backups/
codebuddy-specs-consolidated-20261020/），本测试由原 intl_v2 专属契约改指向统一版的
intl 档案（加载时把顶部 DEPLOY_REGION 行替换为 "intl"，锁的是 global/ch-28 形态）。

覆盖四件套（治 14018 Credits exhausted）：
1. prompt_cache_key 注入：按账号隔离、会话内稳定、已带不覆盖（治 14018 的根因——
   跨协议白名单 OPENAI_FROM_ANTHROPIC/RESPONSES_PAYLOAD_KEYS 不含该键，每请求全量
   计费烧 ~17 倍积分；注入后同会话命中上游前缀缓存 credit≈0.02 vs 0.34）。
2. 设备稳定头 X-Machine-ID/X-Session-ID：按 uid 派生、跨重启恒定、账号间互异、uid 空不伪造。
3. 会话头族 X-Conversation-*/X-Request-ID/X-Trace-ID/X-B3-*：三件套恒发（CPA 补丁实测：
   Global 域缺一即 11128）、规范带连字符 v4 形态、入站同形态才透传。
4. 可选 X-Device-Token（有值才发）+ payload 的 max_completion_tokens 翻译、image_url
   字符串→对象归一、reasoning_effort 补发不回归。

模块级 helper + 原始 spec 类静态钩子走独立 exec（鸭子类型 stub，monkeypatch 对模块
开关天然生效），真 loader（load_code_provider_class）只做 smoke：确保编辑后的 spec
仍能被框架 exec + 识别钩子。不依赖 PG/Redis。specs/ 在 .gitignore 内、由使用者自持，
文件不存在时整个模块跳过。
"""
from __future__ import annotations

import hashlib
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from providers.code_loader import invalidate_cache, load_code_provider_class

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "codebuddy_code_channel.py"
_CHANNEL = "codebuddy-intl-v2-align-test"

if not _SPEC_PATH.exists():
    pytest.skip(f"{_SPEC_PATH.name} 不存在（使用者自持）", allow_module_level=True)

# 统一版默认 DEPLOY_REGION="cn"；本文件锁 intl 档案行为，加载前拧开关（行锚定替换）。
import re as _re

_RAW_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")
_SWITCH_RE = _re.compile(r'^DEPLOY_REGION = "cn"$', _re.M)
assert _SWITCH_RE.search(_RAW_SOURCE), "统一版必须保留独立一行 DEPLOY_REGION = \"cn\""
_SOURCE = _SWITCH_RE.sub('DEPLOY_REGION = "intl"', _RAW_SOURCE, count=1)


@pytest.fixture(scope="module")
def mod():
    """独立 exec 出模块对象（拿模块级 helper 与原始 CodeBuddyIntlChannel 类）。

    与 code_loader 的 exec 命名空间是同一套定义，但这里挂在真模块对象上——
    monkeypatch.setattr(mod, ...) 改模块级开关（如 INJECT_PROMPT_CACHE_KEY）对原始类
    静态钩子天然生效（钩子内 INJECT_PROMPT_CACHE_KEY 按调用时查本模块 globals）。
    源码来自 _SOURCE（DEPLOY_REGION 已拧到 intl）。
    """
    module = types.ModuleType("codebuddy_intl_v2_alignment_module")
    module.__file__ = str(_SPEC_PATH)
    exec(compile(_SOURCE, f"<codebuddy-intl>", "exec"), module.__dict__)
    return module


@pytest.fixture(scope="module")
def channel_cls():
    """真 code_loader 加载的适配器类——仅用于 smoke（钩子识别 / 类标志 / schema）。

    适配器 payload/headers 方法是框架驱动层（签名与 spec 钩子不同），逻辑断言不调它，
    走 mod 上的原始类静态钩子。"""
    cls = load_code_provider_class(_CHANNEL, _SOURCE)
    yield cls
    invalidate_cache(_CHANNEL)


def _stub(**extra):
    """池外实例 stub：模块级 helper 与静态钩子首参 p 均鸭子类型。base_url 故意配错
    （接入点写死设计下它不参与寻址）。device_token 默认空（优雅降级语义）。"""
    values = dict(
        base_url="https://misconfigured.example/v2", api_key="tok", username="tester",
        user_id="uid-1", enterprise_id="", domain="", device_token="", proxy=None,
    )
    values.update(extra)
    return SimpleNamespace(**values)


# ==================== 0. smoke（真 loader） ====================

def test_spec_loads_with_expected_hooks(channel_cls):
    """编辑后的 spec 仍能被框架 exec + 识别全部关键钩子（catch exec/钩子检测回归）。"""
    assert {
        "account_schema", "headers", "payload", "init_auth",
        "begin_device_flow", "poll_device_flow", "refresh_auth", "fetch_models",
        "stream_chat", "base_url", "build_url",
    }.issubset(channel_cls._spec_hooks)


def test_class_flags(channel_cls):
    """类标志与对齐前一致（接入点写死 / token 自愈 / 多轮整发）。"""
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is True
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    assert channel_cls.APPLY_CLIENT_PRESET is False
    assert channel_cls.REQUIRES_BASE_URL is False


def test_account_schema_and_fields_include_device_token(channel_cls):
    """device_token 进 account_schema（前端表单可填）+ ACCOUNT_FIELDS（热更新可回填）。"""
    schema = channel_cls.account_schema()
    keys = {f["key"]: f for f in schema["fields"]}
    assert "device_token" in keys
    assert keys["device_token"]["type"] == "password"
    assert keys["device_token"]["secret"] is True
    assert "device_token" in channel_cls.ACCOUNT_FIELDS


# ==================== 1. prompt_cache_key（模块级 helper） ====================

def test_cache_key_account_isolated(mod):
    """不同 uid → 缓存键不同（跨账号绝不碰撞，防命中对方前缀缓存泄露对话）。"""
    a = mod._cache_key("uid-aaaa", "conv-1")
    b = mod._cache_key("uid-bbbb", "conv-1")
    assert a != b
    assert a.startswith(mod.CACHE_KEY_PREFIX + "-")
    assert "-aaaa" in a
    assert "-bbbb" in b


def test_cache_key_session_stable_and_varying(mod):
    """同账号同会话稳定；同账号不同会话不同；空会话仍注入（保留账号隔离段，不复用）。"""
    assert mod._cache_key("uid-1", "conv-1") == mod._cache_key("uid-1", "conv-1")
    assert mod._cache_key("uid-1", "conv-1") != mod._cache_key("uid-1", "conv-2")
    empty = mod._cache_key("uid-1", "")
    assert empty.startswith(mod.CACHE_KEY_PREFIX + "-")
    assert empty != mod._cache_key("uid-1", "conv-1")


def test_inject_cache_key_body_precedence_and_immutable(mod):
    """优先级：body 已带非空→不覆盖；body conversation_id→作会话源；回落入参。
    且不改原 body（出站 body 与请求日志共享 dict，改拷贝不改原）。"""
    existing = {"prompt_cache_key": "client-own", "messages": []}
    got = mod._inject_prompt_cache_key(dict(existing), "u", "c")
    assert got["prompt_cache_key"] == "client-own"

    from_body = {"conversation_id": "conv-body"}
    got = mod._inject_prompt_cache_key(from_body, "u", "c-kwargs")
    assert got["prompt_cache_key"] == mod._cache_key("u", "conv-body")
    assert "prompt_cache_key" not in from_body  # 原对象未污染

    got = mod._inject_prompt_cache_key({"messages": []}, "u", "c-kwargs")
    assert got["prompt_cache_key"] == mod._cache_key("u", "c-kwargs")

    got = mod._inject_prompt_cache_key({"messages": []}, "u", "")
    assert got["prompt_cache_key"] == mod._cache_key("u", "")


# ==================== 2. 设备稳定头（模块级 helper） ====================

def test_stable_device_id_deterministic(mod):
    """按 uid 稳定派生、账号间互异、用途间不同，且与手算 sha256("wb2a:"+purpose+":"+uid) 一致。"""
    assert mod._stable_device_id("u1", "machine") == mod._stable_device_id("u1", "machine")
    assert mod._stable_device_id("u1", "machine") != mod._stable_device_id("u2", "machine")
    assert mod._stable_device_id("u1", "machine") != mod._stable_device_id("u1", "session")
    assert mod._stable_device_id("u1", "machine") == hashlib.sha256(b"wb2a:machine:u1").hexdigest()[:36]


def test_headers_device_ids_present_and_stable(mod):
    """原始类 headers 注入 X-Machine-ID/X-Session-ID，与手算一致且跨调用稳定（不漂移）。"""
    cls = mod.CodeBuddyIntlChannel
    p = _stub(user_id="uid-1")
    h1 = cls.headers(p, {}, {})
    h2 = cls.headers(p, {}, {})
    assert h1["X-Machine-ID"] == mod._stable_device_id("uid-1", "machine")
    assert h1["X-Session-ID"] == mod._stable_device_id("uid-1", "session")
    assert h1["X-Machine-ID"] == h2["X-Machine-ID"]


def test_headers_device_ids_absent_when_no_uid(mod):
    """uid 空 → 不注入设备头、不伪造（改发 X-No-User-Id，社区匿名同语义）。"""
    h = mod.CodeBuddyIntlChannel.headers(_stub(user_id=""), {}, {})
    assert "X-Machine-ID" not in h
    assert "X-Session-ID" not in h
    assert h["X-No-User-Id"] == "1"


# ==================== 3. 会话头族（模块级 helper + 原始类 headers） ====================

import re as _re

_UUID_HYPHEN_RE = _re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def test_conversation_headers_family_shape(mod):
    """helper：三件套恒发且为带连字符 v4；message-id==X-Request-ID；span=hex[:16]；
    root/trace=conv-req；sampled=1；X-B3-TraceId 恒 32 hex（无连字符，B3 合法）。"""
    h = mod._conversation_headers({}, {"client_request_id": "a" * 32})
    for key in ("X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID"):
        assert _UUID_HYPHEN_RE.match(h[key]), (key, h[key])  # 带连字符 UUID 形态
    assert h["X-Conversation-Message-ID"] == h["X-Request-ID"]
    msg_hex = h["X-Conversation-Message-ID"].replace("-", "")
    assert len(msg_hex) == 32
    assert h["X-B3-SpanId"] == msg_hex[:16]
    assert len(h["X-B3-TraceId"]) == 32 and "-" not in h["X-B3-TraceId"]
    assert h["X-Root-Request-ID"] == h["X-Conversation-Request-ID"]
    assert h["X-Trace-ID"] == h["X-Conversation-Request-ID"]
    assert h["X-B3-Sampled"] == "1"


def test_conversation_headers_no_nonstandard_trace(mod):
    """刻意不发 b3 / traceparent / x-b3-parentspanid（社区不发，框架非标头不抄）。"""
    h = mod._conversation_headers({}, {})
    for key in ("b3", "traceparent", "X-B3-Parentspanid", "x-b3-parentspanid"):
        assert key not in h


def test_conversation_headers_passthrough_uuid_session(mod):
    """入站 client_session_id 是规范 UUID → X-Conversation-ID 透传它（真客户端会话沿用）。"""
    sess = "01a0627a-3ef1-72f2-8900-38c31be2f087"
    h = mod._conversation_headers({}, {"client_session_id": sess})
    assert h["X-Conversation-ID"] == sess
    assert h["X-Conversation-Request-ID"] == sess
    assert h["X-Root-Request-ID"] == sess
    assert h["X-Trace-ID"] == sess


def test_conversation_headers_always_sent_no_passthrough(mod):
    """无入站透传 → 三件套仍恒发（CPA 补丁实测：Global 缺任一即 11128），且都是 v4 形态。"""
    h = mod._conversation_headers({}, {})
    for key in ("X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID"):
        assert _UUID_HYPHEN_RE.match(h[key]), (key, h.get(key))
    # 三者互不相同（每请求现造）
    assert len({h["X-Conversation-ID"], h["X-Conversation-Request-ID"], h["X-Conversation-Message-ID"]}) == 3


def test_conversation_headers_non_uuid_session_not_passed_through(mod):
    """入站 client_session_id 非 UUID（如 opencode ses_xxx）→ 不透传，现造 v4（防渠道校验
    收到非 UUID 形态、也保证 X-B3-TraceId 派生合法）。"""
    h = mod._conversation_headers({}, {"client_session_id": "ses_abc123"})
    assert h["X-Conversation-ID"] != "ses_abc123"
    assert _UUID_HYPHEN_RE.match(h["X-Conversation-ID"])
    assert len(h["X-B3-TraceId"]) == 32 and "-" not in h["X-B3-TraceId"]


def test_headers_attribution_present(mod):
    """归属头存在，去掉「网关特征」的 client/agentPurpose 空值；X-Product 保留 SaaS。"""
    h = mod.CodeBuddyIntlChannel.headers(_stub(), {}, {})
    assert h["X-Agent-Purpose"] == "conversation"
    assert h["X-IDE-Name"] == "WorkBuddy"
    assert h["X-IDE-Type"] == "WorkBuddy"
    assert h["X-IDE-Version"]
    assert h["X-Product"] == "SaaS"


# ==================== 4. 可选 X-Device-Token ====================

def test_headers_device_token_optional(mod):
    """device_token 空 → 不发；非空 → 发。"""
    cls = mod.CodeBuddyIntlChannel
    assert "X-Device-Token" not in cls.headers(_stub(device_token=""), {}, {})
    assert cls.headers(_stub(device_token="dt-123"), {}, {})["X-Device-Token"] == "dt-123"


def test_billing_headers_device_token_conditional(mod):
    """billing 域：有 device_token 才注入 X-Device-Token（社区 BillingHeaders 同语义）。"""
    assert mod._billing_headers(_stub(device_token="dt-abc"))["X-Device-Token"] == "dt-abc"
    assert "X-Device-Token" not in mod._billing_headers(_stub(device_token=""))


# ==================== 5. payload 归一（模块级 helper + 原始类 payload） ====================

def test_translate_max_completion_tokens(mod):
    """max_completion_tokens → max_tokens；显式优先；非正整数/布尔/字符串不译；无键原样返回。"""
    got = mod._translate_max_completion_tokens({"max_completion_tokens": 1000})
    assert got["max_tokens"] == 1000 and "max_completion_tokens" not in got

    got = mod._translate_max_completion_tokens({"max_completion_tokens": 1000, "max_tokens": 50})
    assert got["max_tokens"] == 50 and "max_completion_tokens" not in got  # 显式优先

    for bad in (0, -5, "x", True, 1.5):
        got = mod._translate_max_completion_tokens({"max_completion_tokens": bad})
        assert "max_tokens" not in got and "max_completion_tokens" not in got

    sentinel = {"model": "hy3"}
    assert mod._translate_max_completion_tokens(sentinel) is sentinel  # 无键原样（同对象）


def test_normalize_image_url(mod):
    """image_url 字符串→对象；对象/无图不动；不污染原对象。"""
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": "https://x/y.png"},
    ]}]}
    got = mod._normalize_image_url(body)
    assert got["messages"][0]["content"][1]["image_url"] == {"url": "https://x/y.png"}
    assert body["messages"][0]["content"][1]["image_url"] == "https://x/y.png"  # 原对象未改

    obj = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "u", "detail": "high"}}]}]}
    assert mod._normalize_image_url(obj) is obj  # 已对象形态原样

    plain = {"messages": [{"role": "user", "content": "hi"}]}
    assert mod._normalize_image_url(plain) is plain  # 无图原样


def test_payload_injects_cache_key_e2e(mod):
    """端到端：payload 注入按账号隔离的稳定键；同会话幂等；不污染原 base。"""
    cls = mod.CodeBuddyIntlChannel
    p = _stub(user_id="uid-1")
    base = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    kwargs = {"_base_payload": base, "client_session_id": "sess-x"}
    out1 = cls.payload(p, "openai", "hy3", base["messages"], True, kwargs)
    k1 = out1["prompt_cache_key"]
    assert k1 == mod._cache_key("uid-1", "sess-x")
    # 幂等：二次过钩子（重试路径）键不变
    out2 = cls.payload(p, "openai", "hy3", out1["messages"], True,
                       {"_base_payload": out1, "client_session_id": "sess-x"})
    assert out2["prompt_cache_key"] == k1
    assert "prompt_cache_key" not in base  # 原 base 不被污染


def test_payload_cache_key_account_isolated_e2e(mod):
    """两账号同一会话 → 缓存键不同（账号隔离，防跨号命中对方缓存）。"""
    cls = mod.CodeBuddyIntlChannel
    base = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}]}
    kwargs = {"_base_payload": base, "client_session_id": "same-conv"}
    a = cls.payload(_stub(user_id="uid-a"), "openai", "hy3", base["messages"], True, kwargs)
    b = cls.payload(_stub(user_id="uid-b"), "openai", "hy3", base["messages"], True, kwargs)
    assert a["prompt_cache_key"] != b["prompt_cache_key"]


def test_payload_cache_key_switch_off(mod, monkeypatch):
    """开关 INJECT_PROMPT_CACHE_KEY=False → 不注入（独立模块 globals 可被 monkeypatch）。"""
    monkeypatch.setattr(mod, "INJECT_PROMPT_CACHE_KEY", False)
    cls = mod.CodeBuddyIntlChannel
    base = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}]}
    out = cls.payload(_stub(), "openai", "hy3", base["messages"], True,
                      {"_base_payload": base})
    assert "prompt_cache_key" not in out


def test_payload_reasoning_effort_still_injected(mod):
    """原有 reasoning_effort 补发不回归；缓存键同时注入。"""
    cls = mod.CodeBuddyIntlChannel
    base = {"model": "hy3", "messages": [{"role": "user", "content": "hi"}]}
    out = cls.payload(_stub(), "openai", "hy3", base["messages"], True,
                      {"_base_payload": base, "reasoning_effort": "high"})
    assert out["reasoning_effort"] == "high"
    assert "prompt_cache_key" in out


def test_payload_translates_max_completion_tokens_e2e(mod):
    """端到端：max_completion_tokens 被翻译为 max_tokens。"""
    cls = mod.CodeBuddyIntlChannel
    base = {"model": "hy3", "max_completion_tokens": 4096,
            "messages": [{"role": "user", "content": "hi"}]}
    out = cls.payload(_stub(), "openai", "hy3", base["messages"], True,
                      {"_base_payload": base})
    assert out["max_tokens"] == 4096
    assert "max_completion_tokens" not in out
