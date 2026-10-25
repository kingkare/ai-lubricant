"""AutoClaw spec（specs/autoclaw.py）的翻译契约。

spec 移植自 autoclaw2api（https://github.com/ZFXing-lite/autoclaw2api，Go 网关）——本测试
锁住移植层的行为契约，防止后续改框架钩子模型时悄悄破坏：
1. spec 能被 code_loader 加载（普通类 + 可识别钩子）；
2. 类级开关（token 自动续期 / 定时刷新 / 关伪装头 / 渠道地址可空）；
3. 模型归一对齐 Go NormalizeModel（agent 目标名透传 / 后端模型 → openclaw + 头）；
4. relay base 归一对齐 Go RelayBase（ws→http、去 query、截断 /autoclaw-cloud、proxy 形态）；
5. 错误分类对齐 Go Classify（410000/unauthorized → 401、402/余额不足 → 402、429 透传）；
6. 凭证 JSON 解析（autoclaw-*.json 全文 → 拆字段，camelCase/snake_case 双认）；
7. relay 事件流转换（agent:stream 累积快照前缀差分 / chat 事件 toolCalls+usage / error 处置）；
8. relay 通道模型经 x-openclaw-model 头指定、message 必须是字符串；
9. 双通道端到端：OpenAI 端点出站形态 + NotEnabled 回退 relay。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "autoclaw.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/autoclaw.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("autoclaw-test", _SOURCE)
    yield cls
    invalidate_cache("autoclaw-test")


def _make_provider(channel_cls, *, password: str = "", **extra):
    """造一个池外实例（不挂渠道），需要的字段全部 kwargs 注入。"""
    return channel_cls(username="test", password=password, _api_key=password, **extra)


def _module():
    """加载 spec 模块本身（拿模块级 helper；与 code_loader 的 exec 命名空间同一套定义）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("autoclaw_spec_module", _SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def _patch_namespace(channel_cls, **overrides):
    """把假函数塞进 spec 函数的 exec 命名空间（钩子运行时按 globals 查名）。"""
    hook = channel_cls._spec_hooks.get("stream_chat") or channel_cls._spec_hooks.get("refresh_auth")
    for name, fn in overrides.items():
        hook.__globals__[name] = fn


# ==================== 加载与声明 ====================

def test_spec_loads_with_expected_hooks(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {
        "account_schema", "is_init", "init_auth", "check_auth", "health_check",
        "refresh_auth", "perform_action", "fetch_models", "stream_chat",
    }.issubset(hooks)


def test_class_flags(channel_cls):
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is True
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    # 沙箱网关只认用户 Bearer token，必须关掉伪装头。
    assert channel_cls.APPLY_CLIENT_PRESET is False
    # userapi 基址按 region 官方域名兜底，渠道地址可留空。
    assert channel_cls.REQUIRES_BASE_URL is False


def test_account_schema_declares_device_code_loopback(channel_cls):
    schema = channel_cls.account_schema()
    assert schema["provider_name"] == "autoclaw-test"
    # 短信（cn）授权已接入：device_code + loopback 补投；global 走手动填凭证。
    assert "device_code" in schema["add_methods"]
    assert schema["auth_start"]["mode"] == "device_code"
    assert schema["auth_start"]["completion"] == "loopback"
    keys = {f["key"] for f in schema["fields"]}
    assert {"password", "refresh_token", "region", "phone", "device_id", "quota_text",
            "sandbox_id", "sandbox_endpoint"} <= keys


def test_account_fields_cover_runtime_state(channel_cls):
    fields = set(channel_cls.ACCOUNT_FIELDS)
    assert {"refresh_token", "expires_at", "device_id", "region",
            "sandbox_id", "sandbox_endpoint", "sandbox_end_ts", "quota_text"} <= fields


def test_region_is_first_field_and_select(channel_cls):
    """region 必须是首个字段的下拉框：cn/global 决定授权方式与域名，文本框没人看得懂。

    前端按 section 首次出现顺序分组、组内保持后端数组顺序，故 fields[0] 即「供应商字段」
    区第一行（对齐 codebuddy.py 的 region 写法）。
    """
    fields = channel_cls.account_schema()["fields"]
    assert fields[0]["key"] == "region"
    region = fields[0]
    assert region["type"] == "select"
    assert region["default_value"] == "cn"
    assert {opt["value"] for opt in region["options"]} == {"cn", "global"}
    # 每个 option 都要有中文标签（前端直接渲染 label，不渲染裸值）
    assert all(opt.get("label") for opt in region["options"])


# ==================== 凭证 JSON 解析 ====================

def _credential_json() -> str:
    return json.dumps({
        "accessToken": "Bearer at-xyz",
        "refreshToken": "rt-xyz",
        "expiresAt": int(time.time()) + 3600,
        "userId": 12345,
        "userName": "张三",
        "phone": "138****8000",
        "deviceId": "dev-1",
        "region": "global",
        "sandboxId": "sb-1",
        "sandboxEndpoint": "wss://gw.example.com/autoclaw-cloud/ws?sandbox_id=sb-1&port=1",
        "endTimestamp": int(time.time()) + 7200,
    })


def test_credential_json_parses_into_fields(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password=_credential_json())
    assert mod._maybe_parse_credential_json(p) is True
    assert p.password == "Bearer at-xyz"
    assert p.refresh_token == "rt-xyz"
    assert p.user_id == "12345"
    assert p.region == "global"
    assert p.device_id == "dev-1"
    assert p.sandbox_id == "sb-1"
    # expiresAt 有值就保留，不覆盖成 28 天
    assert int(p.expires_at) > int(time.time())


# ==================== 写死的 cn / global 凭证 ====================

def test_hardcoded_credential_used_when_account_blank(channel_cls):
    """账号页 password 留空 → 按 region 套用写死凭证（cn/global 各一份）。"""
    mod = _module()
    saved = dict(mod.HARDCODED_CREDENTIALS)
    mod.HARDCODED_CREDENTIALS = {
        "cn": {"accessToken": "Bearer hard-cn", "refreshToken": "rt-cn",
               "deviceId": "dev-cn", "region": "cn"},
        "global": {"accessToken": "Bearer hard-gl", "refreshToken": "rt-gl",
                   "deviceId": "dev-gl", "userName": "g@example.com", "region": "global"},
    }
    try:
        p = _make_provider(channel_cls, password="")   # 不传 region → 默认 cn
        assert mod._maybe_parse_credential_json(p) is True
        assert p.password == "Bearer hard-cn"
        assert p.refresh_token == "rt-cn"
        assert p.device_id == "dev-cn"   # device_id 必须写死，refresh 才对得上

        g = _make_provider(channel_cls, password="", region="global")
        assert mod._maybe_parse_credential_json(g) is True
        assert g.password == "Bearer hard-gl"
        assert g.region == "global"
    finally:
        mod.HARDCODED_CREDENTIALS = saved


def test_hardcoded_credential_unset_is_empty(channel_cls):
    """常量为空占位（未配置）→ 不认凭证，账号视为未授权。"""
    mod = _module()
    saved = dict(mod.HARDCODED_CREDENTIALS)
    mod.HARDCODED_CREDENTIALS = {"cn": {"accessToken": ""}, "global": {"accessToken": ""}}
    try:
        p = _make_provider(channel_cls, password="")
        assert mod._hardcoded_credential(p) == {}
        assert mod._maybe_parse_credential_json(p) is False
    finally:
        mod.HARDCODED_CREDENTIALS = saved


def test_pasted_credential_beats_hardcoded(channel_cls):
    """账号页粘贴的凭证永远优先，写死常量不覆盖它。"""
    mod = _module()
    saved = dict(mod.HARDCODED_CREDENTIALS)
    mod.HARDCODED_CREDENTIALS = {"cn": {"accessToken": "Bearer hard-cn"}}
    try:
        p = _make_provider(channel_cls, password="Bearer typed-token")
        assert mod._maybe_parse_credential_json(p) is False
        assert p.password == "Bearer typed-token"
    finally:
        mod.HARDCODED_CREDENTIALS = saved


def test_check_auth_accepts_hardcoded_credential(channel_cls):
    """账号页留空但写死凭证已配 → check_auth/health_check/is_init 都认。"""
    # 类钩子在自己的 exec 命名空间里跑，常量要打到那里才生效
    ns = channel_cls._spec_hooks["check_auth"].__globals__
    saved = ns["HARDCODED_CREDENTIALS"]
    ns["HARDCODED_CREDENTIALS"] = {"cn": {"accessToken": "Bearer hard-cn"}, "global": {}}
    try:
        p = _make_provider(channel_cls, password="")
        assert _run(channel_cls.check_auth(p)) is True
        assert _run(channel_cls.health_check(p)) is True
        assert channel_cls.is_init(p) is True

        blank = _make_provider(channel_cls, password="", region="global")
        assert _run(channel_cls.check_auth(blank)) is False   # global 那份是空的
    finally:
        ns["HARDCODED_CREDENTIALS"] = saved


def test_credential_json_without_token_is_noop(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password='{"foo": 1}')
    assert mod._maybe_parse_credential_json(p) is False


def test_bearer_value_idempotent():
    mod = _module()
    assert mod._bearer_value("Bearer abc") == "Bearer abc"
    assert mod._bearer_value("abc") == "Bearer abc"
    assert mod._strip_bearer("Bearer abc") == "abc"
    assert mod._strip_bearer("abc") == "abc"


# ==================== 模型归一（对齐 Go NormalizeModel 向量）===================

def test_normalize_model_vectors():
    mod = _module()
    cases = [
        ("openclaw", "openclaw", ""),
        ("openclaw/default", "openclaw/default", ""),
        ("openclaw/designer", "openclaw/designer", ""),
        ("agent:analyst", "agent:analyst", ""),
        ("glm-5.3-flash", "openclaw", "glm-5.3-flash"),
        ("glm-5.2", "openclaw", "glm-5.2"),
        ("", "openclaw", ""),
    ]
    for raw, model, xmodel in cases:
        assert mod._normalize_model(raw) == (model, xmodel), raw


# ==================== relay base 归一（对齐 Go RelayBase 向量）===================

def test_relay_base_normalization_vectors():
    mod = _module()
    cases = [
        ("https://sb1.example.com", "https://sb1.example.com/autoclaw-cloud"),
        ("https://sb1.example.com/autoclaw-cloud", "https://sb1.example.com/autoclaw-cloud"),
        ("wss://sb1.example.com/", "https://sb1.example.com/autoclaw-cloud"),
        ("https://sb1.example.com/autoclaw-cloud/", "https://sb1.example.com/autoclaw-cloud"),
        ("https://sb1.example.com/foo", "https://sb1.example.com/foo/autoclaw-cloud"),
        ("wss://autoglm-api.zhipuai.cn/autoclaw-cloud/ws?sandbox_id=sb1&port=29000&path=/ws",
         "https://autoglm-api.zhipuai.cn/autoclaw-cloud"),
        ("https://sb.example.com/autoclaw-cloud/ws?x=1&y=2", "https://sb.example.com/autoclaw-cloud"),
        ("ws://h.example.com/autoclaw-cloud/ws?p=1", "http://h.example.com/autoclaw-cloud"),
    ]
    for raw, want in cases:
        assert mod._relay_base(raw) == want, raw


def test_relay_proxy_and_bare_base():
    mod = _module()
    endpoint = "wss://autoglm-api.zhipuai.cn/autoclaw-cloud/ws?sandbox_id=sb1&port=1"
    proxy = mod._relay_proxy_base(endpoint, "sb1")
    assert proxy == "https://autoglm-api.zhipuai.cn/autoclaw-cloud/proxy/sb1"
    assert mod._relay_bare_base(proxy) == "https://autoglm-api.zhipuai.cn/autoclaw-cloud"
    assert mod._relay_bare_base("https://gw/autoclaw-cloud") == "https://gw/autoclaw-cloud"


def test_sign_headers_shape():
    mod = _module()
    headers = mod._sign_headers("Bearer at-1")
    sign = headers["X-Auth-Sign"]
    assert len(sign) == 32 and all(c in "0123456789abcdef" for c in sign)
    assert headers["X-Auth-Appid"] == "100003"
    assert headers["X-Product"] == "autoclaw"
    assert headers["X-Version"] == "1.18.5"
    assert headers["Authorization"] == "Bearer at-1"
    # WAF 要求：overseasv1 系端点必须带浏览器 UA + Origin/Referer，否则 405
    assert "Mozilla/5.0" in headers["User-Agent"]
    assert headers["Origin"] == "https://autoclaw.z.ai"
    assert headers["Referer"] == "https://autoclaw.z.ai/web/"
    # 无 token 时（send-code 类）不带 Authorization
    assert "Authorization" not in mod._sign_headers("")


# ==================== 错误分类（对齐 Go Classify 向量）===================

def test_classify_upstream_error_vectors():
    mod = _module()

    def status_of(exc):
        return exc.status_code

    # 410000 业务码 / unauthorized 文案 → 会话死亡 401
    assert status_of(mod._classify_upstream_error(200, 410000, "Not logged in")) == 401
    assert status_of(mod._classify_upstream_error(200, 0, "Unauthorized web bridge request")) == 401
    # 余额不足 → 402
    assert status_of(mod._classify_upstream_error(402, 0, "insufficient credit")) == 402
    assert status_of(mod._classify_upstream_error(200, 0, "积分不足")) == 402
    # 429 透传
    assert status_of(mod._classify_upstream_error(429, 0, "rate limit")) == 429
    # 其余透传
    assert status_of(mod._classify_upstream_error(404, 0, "no route")) == 404
    assert status_of(mod._classify_upstream_error(500, 0, "boom")) == 500


def test_endpoint_not_enabled_detection():
    mod = _module()
    assert mod._is_endpoint_not_enabled(HTTPException(404, "no route")) is True
    assert mod._is_endpoint_not_enabled(HTTPException(501, "not implemented")) is True
    assert mod._is_endpoint_not_enabled(
        HTTPException(400, "chatCompletions endpoint is not enabled")) is True
    assert mod._is_endpoint_not_enabled(HTTPException(400, "invalid model")) is False
    assert mod._is_endpoint_not_enabled(HTTPException(500, "boom")) is False


def test_maybe_credit_error_upgrades_llm_failed_when_balance_zero(monkeypatch):
    """502「LLM request failed」且余额 0 → 402（积分不足）。

    回归（2026-09-30 载体 E2E）：积分耗尽时，上游把任何模型名都报成
    ``HTTP 502 AutoClaw 会话错误: LLM request failed.``，2~3 秒即返——和真正的上游
    崩溃共用一条文案。报 502 会让框架反复重试、不冻结账号；402 才对（框架冻结、
    恢复后自动解冻）。
    """
    mod = _module()

    async def fake_points(_p):
        return 0
    monkeypatch.setattr(mod, "_fetch_points", fake_points)

    class _P:
        pass

    upgraded = _run(mod._maybe_credit_error(_P(), HTTPException(
        502, "AutoClaw 会话错误: LLM request failed.")))
    assert upgraded.status_code == 402
    assert "积分不足" in str(upgraded.detail)


def test_maybe_credit_error_keeps_502_when_balance_positive(monkeypatch):
    """余额 > 0 仍报 502（真上游崩，不是积分）——别误冻账号。"""
    mod = _module()

    async def fake_points(_p):
        return 400
    monkeypatch.setattr(mod, "_fetch_points", fake_points)

    class _P:
        pass

    kept = _run(mod._maybe_credit_error(_P(), HTTPException(
        502, "AutoClaw 会话错误: LLM request failed.")))
    assert kept.status_code == 502


def test_maybe_credit_error_skips_balance_probe_for_other_errors(monkeypatch):
    """只在「LLM request failed」这条模糊文案上查余额，其余 502 不动。"""
    mod = _module()

    called = {"n": 0}

    async def fake_points(_p):
        called["n"] += 1
        return 0
    monkeypatch.setattr(mod, "_fetch_points", fake_points)

    class _P:
        pass

    kept = _run(mod._maybe_credit_error(_P(), HTTPException(502, "别的东西崩了")))
    assert kept.status_code == 502
    assert called["n"] == 0, "非 LLM-failed 文案不应查余额"


def test_maybe_credit_error_keeps_502_when_balance_probe_fails(monkeypatch):
    """查余额本身失败 → 保留原 502，不能把网络抖动误判成积分不足把账号冻了。"""
    mod = _module()

    async def fake_points(_p):
        raise RuntimeError("probe net down")
    monkeypatch.setattr(mod, "_fetch_points", fake_points)

    class _P:
        pass

    kept = _run(mod._maybe_credit_error(_P(), HTTPException(
        502, "AutoClaw 会话错误: LLM request failed.")))
    assert kept.status_code == 502


# ==================== relay 事件流转换 ====================

def _sse_frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps({'event': event, 'payload': payload}, ensure_ascii=False)}\n\n"


def test_relay_feed_agent_stream_prefix_diff():
    """累积快照 delta → 前缀差分出增量（对齐 Go emitText）。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "Hello"}))
    assert [f["content"] for f in out] == ["Hello"]
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "Hello world"}))
    assert [f["content"] for f in out] == [" world"]
    # 重复快照不重复发
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "Hello world"})) == []


def test_relay_feed_filters_other_runs():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r-other", "type": "text", "delta": "noise"})) == []


def test_relay_first_content_window_advances_on_any_run_event():
    """首字超时看的是「本 run 是否还在推进」，不是「是否已吐字」。

    回归（2026-09-30 载体 E2E）：6700 字符的安装命令在 planning / 工具阶段能静默
    超过 90s，旧实现只认内容帧 → 三次重试全被判「首字超时」，而同账号短 prompt 秒回。
    现在归属命中的**任何**本 run 事件（phase / 工具 / 正文）都刷新 last_run_event_at，
    首字窗口以它为锚滑动；另设绝对上限防推进帧无限续命。
    """
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    assert state["last_run_event_at"] == 0.0, "新建时未收到任何本 run 事件"

    # phase（planning）不产帧，但算推进
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "phase", "phase": "planning"})) == []
    after_phase = state["last_run_event_at"]
    assert after_phase > 0.0, "phase 事件应刷新首字时钟"

    # 工具阶段同样算推进（Windows 时钟粒度 ~15ms，故用哨兵值判定，不靠 sleep）
    state["last_run_event_at"] = 1.0
    mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "tool_call", "name": "exec", "input": {"cmd": "wc -c"}}))
    assert state["last_run_event_at"] > 1.0, "工具阶段应推进首字时钟"

    # 归属未命中的帧**不得**推进（否则别的 run 的事件能把我们的首字窗口续命）
    before = state["last_run_event_at"]
    mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r-other", "type": "phase", "phase": "planning"}))
    assert state["last_run_event_at"] == before, "非本 run 事件不应刷新首字时钟"

    # 网关心跳（无归属信息）同理
    mod._relay_feed(state, _sse_frame("gateway:event", {"event": "health", "payload": {"x": 1}}))
    assert state["last_run_event_at"] == before, "心跳帧不应刷新首字时钟"

    # 绝对上限存在（推进帧不能无限续命）
    assert mod.RELAY_FIRST_CONTENT_MAX > mod.RELAY_FIRST_CONTENT_TIMEOUT


def test_relay_first_content_error_message_tracks_event_window():
    """首字超时报错文案说「本 run 无任何事件」，与判定口径一致。"""
    import inspect
    mod = _module()
    src = inspect.getsource(mod._stream_via_relay)
    assert "RELAY_FIRST_CONTENT_MAX" in src
    assert "本 run 无任何事件" in src
    # 滑动锚：last_run_event_at 必须参与判定
    assert "since_evt" in src


def test_relay_feed_session_match_wins_even_with_foreign_runid():
    """回归（用户报「relay 事件流无归属事件」）：

    我方 sessionKey 是 aclaw-{uuid4[:16]}、每次请求唯一——事件里出现它就只有一种解释：
    这是本次请求的事件。上游若在事件上盖了自己的 runId（≠ send 响应的 runId），
    不能因此丢弃；session 命中即可归属。旧逻辑要求「且事件没带 runId」，
    会把我们自己的事件全丢 → 一帧不吐 → 报「无归属事件」。
    """
    mod = _module()
    state = mod._new_relay_state("agent_send_run", "aclaw-abc123")
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "agent_mui8ka5x_d79bcj",   # 上游自己的 runId，与 send 响应不同
        "sessionKey": "agent:main:aclaw-abc123",
        "type": "text", "delta": "hi"}))
    assert [f["content"] for f in out] == ["hi"]

    # to 字段同理（部分事件用 to 承载 sessionKey）
    state2 = mod._new_relay_state("r-send", "aclaw-xyz")
    out2 = mod._relay_feed(state2, _sse_frame("agent:stream", {
        "runId": "r-upstream", "to": "agent:main:aclaw-xyz",
        "type": "text", "delta": "yo"}))
    assert [f["content"] for f in out2] == ["yo"]

    # 既不带我方 session 也不带我方能认的 runId → 仍必须丢弃（噪音帧）
    state3 = mod._new_relay_state("r-send", "aclaw-xyz")
    assert mod._relay_feed(state3, _sse_frame("agent:stream", {
        "runId": "r-other", "sessionKey": "agent:main:someone-else",
        "type": "text", "delta": "noise"})) == []
    # 丢弃时留样本供报错诊断
    assert state3["dropped"], "归属未命中应留诊断样本"


def test_relay_no_attribution_error_includes_diagnostics():
    """「无归属事件」报错必须带上我方 key + 收到的事件样本，否则无从定位。"""
    import asyncio
    import inspect
    mod = _module()
    src = inspect.getsource(mod._stream_via_relay)
    assert "收到的事件" in src and "dropped" in src
    assert "sessionKey={session_key!r}" in src


def test_relay_feed_chat_event_tool_calls_usage_done():
    """chat 事件：message.content 前缀差分 + toolCalls 帧 + final usage + done。"""
    mod = _module()
    state = mod._new_relay_state("r1", "main")
    out = mod._relay_feed(state, _sse_frame("agent", {
        "runId": "r1", "stream": "tool",
        "data": {"phase": "start", "name": "sql_query", "toolCallId": "t-1", "args": {"x": 1}}}))
    assert len(out) == 1
    tc = out[0]["tool_calls"][0]
    assert tc["id"] == "t-1" and tc["function"]["name"] == "sql_query"
    assert json.loads(tc["function"]["arguments"]) == {"x": 1}
    out = mod._relay_feed(state, _sse_frame("chat", {
        "runId": "r1", "sessionKey": "agent:main:main", "state": "final",
        "message": {"content": "Hello world"},
        "usage": {"input": 9, "output": 7}}))
    assert "".join(f.get("content") or "" for f in out) == "Hello world"
    done = out[-1]
    assert done["done"] is True
    # 有工具调用 → finish=tool_calls（让编码客户端正确执行工具）
    assert done["finish_reason"] == "tool_calls"
    assert done["usage"] == {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}


def test_relay_feed_done_without_usage_falls_to_stop():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    out = mod._relay_feed(state, _sse_frame("agent:stream", {"runId": "r1", "type": "done"}))
    assert out[-1]["done"] is True
    assert out[-1]["finish_reason"] == "stop"
    assert state["done"] is True
    # done 之后的帧全部丢弃
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "late"})) == []


def test_relay_feed_error_before_content_raises():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    with pytest.raises(HTTPException) as excinfo:
        mod._relay_feed(state, _sse_frame("agent:stream", {
            "runId": "r1", "type": "error", "message": "LLM request failed"}))
    assert "LLM request failed" in str(excinfo.value.detail)


def test_relay_feed_error_after_content_emits_text_and_finish():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    mod._relay_feed(state, _sse_frame("agent:stream", {"runId": "r1", "type": "text", "delta": "partial"}))
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "error", "message": "mid-stream failure"}))
    assert "mid-stream failure" in out[0]["content"]
    assert out[-1]["done"] is True


# ==================== relay 通道请求构造 ====================

def test_relay_send_request_backend_model_header(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="Bearer at-1")
    body, headers = mod._relay_send_request(p, "glm-5.3-flash", [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ], {})
    # message 必须是字符串（对象会被上游拒）
    assert isinstance(body["args"][0]["message"], str)
    assert "[system]" in body["args"][0]["message"]
    assert "[user]" in body["args"][0]["message"]
    # 后端模型经头指定，body 不带 model
    assert headers["x-openclaw-model"] == "glm-5.3-flash"
    assert "model" not in body["args"][0]
    assert headers["Authorization"] == "Bearer at-1"
    # sessionKey 每请求随机
    _, headers2 = mod._relay_send_request(p, "glm-5.2", [{"role": "user", "content": "hi"}], {})
    assert body["args"][0]["sessionKey"] != _relay_send_request_session(mod, p)


# ==================== relay 通道客户端工具注入 ====================

def test_relay_send_request_injects_client_tools_into_transcript(channel_cls):
    """客户端声明了 tools 时，工具说明必须并进 transcript（沙箱 agent 才知道有 get_weather）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="Bearer at-1")
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Get weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}]
    body, _ = mod._relay_send_request(p, "glm-5.3-flash", [
        {"role": "user", "content": "北京天气怎么样"},
    ], {"tools": tools})
    msg = body["args"][0]["message"]
    assert "get_weather" in msg          # 工具名进了 transcript
    assert "tool_function" in msg        # 围栏约定也进了


def test_relay_send_request_flattens_tool_history_when_tools_declared(channel_cls):
    """历史轮的原生 tool_calls/tool 结果拍成文本（避免触发上游自己的 function-calling）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="Bearer at-1")
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                       "required": ["city"]}}}]
    body, _ = mod._relay_send_request(p, "glm-5.3-flash", [
        {"role": "user", "content": "北京天气"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{\"city\":\"北京\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "晴 25 度"},
    ], {"tools": tools})
    msg = body["args"][0]["message"]
    assert "晴 25 度" in msg               # 历史工具结果拍进文本
    assert '"tool_calls"' not in msg      # 原生结构被扁平化


def test_relay_tool_use_folded_to_thinking_when_client_declares_tools(channel_cls):
    """客户端声明了 tools 时，上游的 tool_use（read/exec）折进 thinking，不发给客户端。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x", client_tools=[{"type":"function","function":{"name":"get_weather"}}])
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "tool_use", "name": "exec", "input": {"command": "ls"},
        "sessionKey": "aclaw-x"}))
    assert out and "tool_calls" not in out[0]
    assert "thinking" in out[0] and "exec" in out[0]["thinking"]
    assert state["had_tools"] is False   # 没发 tool_calls 给客户端


def test_relay_finish_reason_fence_when_tools_declared(channel_cls):
    """客户端声明了 tools 且正文出现 ```tool_function 围栏 → finish=tool_calls。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x", client_tools=[{"type":"function","function":{"name":"get_weather"}}])
    mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text",
        "delta": "```tool_function\n{\"name\":\"get_weather\",\"arguments\":{\"city\":\"北京\"}}\n```",
        "sessionKey": "aclaw-x"}))
    assert mod._relay_finish_reason(state) == "tool_calls"


def test_relay_finish_reason_plain_answer_stops(channel_cls):
    """没有围栏、也没有 had_tools → finish=stop。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x", client_tools=None)
    mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "北京今天晴天",
        "sessionKey": "aclaw-x"}))
    assert mod._relay_finish_reason(state) == "stop"


def test_relay_error_after_content_is_marked(channel_cls):
    """已吐内容后的错误文案带标记前缀，finish_reason 不谎报 tool_calls。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    mod._relay_feed(state, _sse_frame("agent:stream", {"runId":"r1","type":"text","delta":"我查一下"}))
    out = mod._relay_feed(state, _sse_frame("agent:stream", {"runId":"r1","type":"error","message":"LLM request failed."}))
    assert "[AutoClaw 上游错误]" in out[0]["content"]
    assert out[-1]["done"] is True
    assert out[-1].get("finish_reason") == "stop"


def _relay_send_request_session(mod, p):
    body, _ = mod._relay_send_request(p, "glm-5.2", [{"role": "user", "content": "hi"}], {})
    return body["args"][0]["sessionKey"]


def test_relay_send_request_agent_target_no_header(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="at-1")
    body, headers = mod._relay_send_request(p, "openclaw", [{"role": "user", "content": "hi"}], {})
    assert "x-openclaw-model" not in headers


def test_relay_transcript_empty_messages_raises(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="at-1")
    with pytest.raises(HTTPException):
        mod._relay_transcript([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}])


# ==================== 双通道端到端 ====================

_OPENAI_SSE = (
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"he"},"finish_reason":null}],"usage":null}\n\n'
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":null}],"usage":null}\n\n'
    'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)


def _ready_provider(channel_cls, *, password="Bearer at-1", **extra):
    p = _make_provider(channel_cls, password=password, **extra)
    now = int(time.time())
    p.expires_at = str(now + 7200)          # token 新鲜，不触发 refresh
    p.refresh_token = "rt-1"
    p.device_id = "dev-1"
    p.sandbox_id = "sb1"
    p.sandbox_endpoint = "https://gw.example.com/autoclaw-cloud/proxy/sb1"
    p.sandbox_end_ts = str(now + 7200)
    return p


async def _collect_stream(channel_cls, p, model="glm-5.3-flash", messages=None):
    out = []
    async for f in channel_cls._do_stream_chat(
            p, model, messages or [{"role": "user", "content": "hi"}]):
        out.append(f)
    return out


def test_stream_chat_openai_endpoint_end_to_end(channel_cls):
    """通道 1 全链路：出站 URL/模型头 + 标准 OpenAI SSE → 统一帧（content/usage/finish/done）。"""
    p = _ready_provider(channel_cls)

    async def fake_send(method, url, headers, **kw):
        assert url == "https://gw.example.com/autoclaw-cloud/proxy/sb1/v1/chat/completions"
        assert headers["Authorization"] == "Bearer at-1"
        # 后端模型 → body model=openclaw + x-openclaw-model 头
        assert headers["x-openclaw-model"] == "glm-5.3-flash"
        assert kw["json"]["model"] == "openclaw"
        assert kw["json"]["stream"] is True
        for line in _OPENAI_SSE.split("\n\n"):
            if line:
                yield line + "\n\n"

    p.send_sse_request = fake_send
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())

    frames = [f for f in _run(_collect_stream(channel_cls, p)) if isinstance(f, dict)]
    assert frames[0] == {}  # 首帧空字典：上游接通信号
    contents = "".join(f.get("content") or "" for f in frames)
    assert contents == "hello"
    usage_frame = next(f for f in frames if isinstance(f.get("usage"), dict))
    assert usage_frame["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert any(f.get("finish_reason") == "stop" for f in frames)
    assert any(f.get("done") for f in frames)


async def _noop():
    return None


def test_stream_chat_agent_target_model_no_header(channel_cls):
    """agent 目标名：body 原样透传、无 x-openclaw-model 头。"""
    p = _ready_provider(channel_cls)

    async def fake_send(method, url, headers, **kw):
        assert "x-openclaw-model" not in headers
        assert kw["json"]["model"] == "openclaw/default"
        yield 'data: {"id":"c","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    p.send_sse_request = fake_send
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())
    frames = [f for f in _run(_collect_stream(channel_cls, p, model="openclaw/default")) if isinstance(f, dict)]
    assert "".join(f.get("content") or "" for f in frames) == "ok"


class _FakeResp:
    def __init__(self, status=200, text="", json_obj=None, chunks=b""):
        self.status = status
        self._text = text
        self._json = json_obj
        self._chunks = chunks

    async def text(self):
        # _relay_json 读 text 再 json.loads：json_obj 优先序列化
        if self._text:
            return self._text
        return json.dumps(self._json) if self._json is not None else ""

    async def json(self):
        return self._json

    @property
    def content(self):
        return self

    async def iter_any(self):
        yield self._chunks

    async def readany(self):
        """模拟 aiohttp StreamReader.readany：一次给一块，随后 b""（EOF）。"""
        chunk, self._chunks = self._chunks, b""
        return chunk


class _FakeCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, get_resp=None, post_resp=None, request_resp=None):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self._request_resp = request_resp
        self.get_urls = []
        self.post_calls = []
        self.request_calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.get_urls.append(url)
        return _FakeCtx(self._get_resp)

    def post(self, url, headers=None, json=None, **kw):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return _FakeCtx(self._post_resp)

    def request(self, method, url, headers=None, json=None, params=None, timeout=None, **kw):
        self.request_calls.append({"method": method, "url": url, "json": json, "params": params})
        return _FakeCtx(self._request_resp)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_RELAY_EVENTS = (
    _sse_frame("agent:stream", {"runId": "r1", "type": "text", "delta": "Hello"}).encode()
    + _sse_frame("agent:stream", {"runId": "r1", "type": "text", "delta": "Hello world"}).encode()
    + _sse_frame("agent:stream", {"runId": "r1", "type": "done", "usage": {"input": 3, "output": 2}}).encode()
)


def test_stream_chat_falls_back_to_relay_on_not_enabled(channel_cls):
    """通道 1 报 404（端点未启用）→ 自动回退 relay bridge：WS 跳过、events+send 走通。"""
    p = _ready_provider(channel_cls)

    async def not_enabled_send(method, url, headers, **kw):
        raise HTTPException(404, "chatCompletions endpoint is not enabled")
        yield  # pragma: no cover

    p.send_sse_request = not_enabled_send

    events_resp = _FakeResp(status=200, chunks=_RELAY_EVENTS)
    send_resp = _FakeResp(status=200, json_obj={"ok": True, "data": {"runId": "r1"}})
    session = _FakeSession(get_resp=events_resp, post_resp=send_resp)
    p._make_session = lambda timeout=None: session

    async def no_ws(pp, bare):
        return None

    _patch_namespace(channel_cls, _device_online_ws=no_ws, _wake_sandbox=lambda *a, **k: _noop())

    frames = [f for f in _run(_collect_stream(channel_cls, p)) if isinstance(f, dict)]
    # relay 通道走通：events GET + agent/send
    assert session.get_urls == ["https://gw.example.com/autoclaw-cloud/proxy/sb1/api/events"]
    assert session.post_calls[0]["url"] == "https://gw.example.com/autoclaw-cloud/proxy/sb1/api/electron/agent/send"
    assert session.post_calls[0]["headers"]["x-openclaw-model"] == "glm-5.3-flash"
    # 前缀差分 + usage + done
    contents = "".join(f.get("content") or "" for f in frames)
    assert contents == "Hello world"
    done = frames[-1]
    assert done["done"] is True
    assert done["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_relay_planning_phase_is_not_attributed_content(channel_cls):
    """回归（用户报「无归属事件」且 14s 就失败）：

    agent 的 planning 阶段先发 type=phase/phase=planning（**无文本增量**），随后才吐字。
    这类帧必须被认作「归属通过但暂无内容」——不能算 emitted，也不能被当成噪音丢弃，
    否则流一结束就报「无归属事件」。同时验证真实帧形态（gateway:status + agent:stream）。
    """
    mod = _module()
    state = mod._new_relay_state("agent_muiifrgc_o3ilqg", "aclaw-4fbe33b39d994bd2")
    # gateway:status 是噪音帧（payload 是字符串，不是 dict）→ 丢弃且不记 dropped
    assert mod._relay_feed(state, 'data: {"event":"gateway:status","payload":"connected"}\n\n') == []
    assert state["dropped"] == []
    # planning 阶段：归属匹配、无内容 → 返回空帧，但**不记 dropped**（它不是"无归属"）
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "agent_muiifrgc_o3ilqg", "type": "phase", "phase": "planning",
        "sessionKey": "aclaw-4fbe33b39d994bd2"}))
    assert out == []
    assert state["dropped"] == []
    assert state["emitted"] is False
    # 随后真正吐字 → 正常产出
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "agent_muiifrgc_o3ilqg", "type": "text", "delta": "hi",
        "sessionKey": "aclaw-4fbe33b39d994bd2"}))
    assert [f["content"] for f in out] == ["hi"]
    assert state["emitted"] is True


def test_relay_session_key_matches_without_agent_main_prefix(channel_cls):
    """回归：真实帧里 sessionKey 是**裸的** aclaw-xxx（不是 agent:main:aclaw-xxx）。

    归属用「我方 key 是事件 sessionKey 的子串」判定，两种形态都要命中。
    """
    mod = _module()
    state = mod._new_relay_state("r-send", "aclaw-4fbe33b39d994bd2")
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "agent_muiifrgc_o3ilqg", "type": "text", "delta": "yo",
        "sessionKey": "aclaw-4fbe33b39d994bd2"}))
    assert [f["content"] for f in out] == ["yo"]


def test_relay_event_stream_never_cancels_reads(channel_cls):
    """回归（生产「无归属事件」，事件流总在最后一字节后**恰好 10.0s 整**报 EOF）：

    旧实现拿 `wait_for(readany(), timeout=sock_read=10)` 当「单次读静默计时器」，注释
    以为超时只是 continue。实际上取消一个进行中的读会终结底层异步生成器——
    ProxyManager 把响应包成 OutboundResponse（readany = 生成器 __anext__），
    生成器被终结后每次读都 StopAsyncIteration → 恒返回 b"" → 被当成「上游 EOF」。
    日志铁证：19:56:13.565 tick → 19:56:23.647 EOF、17:26:01.191 planning → 17:26:11.190
    EOF，都是最后一字节后 10.0s；而 agent 规划静默十几秒是正常的。

    新实现必须：读泵常驻（只在收尾取消）、超时只作用于队列等待、aiohttp 超时只做
    死连接兜底，静默判定一律用时间差。
    """
    mod = _module()
    import inspect
    src = inspect.getsource(mod._stream_via_relay)
    assert "readany(), timeout" not in src, "绝不能取消底层读（会终结生成器）"
    assert "_pump_events" in src and "queue.get()" in src
    assert mod.RELAY_FIRST_CONTENT_TIMEOUT >= 30.0
    assert mod.RELAY_SOCK_READ_TIMEOUT >= 120.0
    assert mod.RELAY_POLL_SECONDS < mod.RELAY_IDLE_FINISH_SECONDS


def test_relay_feed_unwraps_gateway_event_envelope(channel_cls):
    """gateway:event 是信封帧：真实事件嵌在 payload={event,payload} 里。

    内层若是我们的 agent 事件族，必须照常归属分发（别当噪音丢）；内层是
    health/tick/presence 这类心跳才记噪音——否则报错会长成
    「收到的事件：event=gateway:event run=- sess=- to=-」，看着像归属判错。
    """
    mod = _module()
    state = mod._new_relay_state("agent_run_1", "aclaw-abc123")
    out = mod._relay_feed(state, _sse_frame("gateway:event", {
        "event": "agent:stream",
        "payload": {"runId": "agent_run_1", "type": "text", "delta": "hi",
                    "sessionKey": "agent:main:aclaw-abc123"}}))
    assert [f["content"] for f in out] == ["hi"]
    assert state["emitted"] is True

    # 心跳内层（health/tick/presence）→ 记噪音、不产出、不占 dropped
    hb = mod._new_relay_state("agent_run_1", "aclaw-abc123")
    assert mod._relay_feed(hb, _sse_frame("gateway:event", {
        "event": "health", "payload": {"ok": True, "ts": 1790510173260}})) == []
    assert mod._relay_feed(hb, _sse_frame("gateway:event", {
        "event": "tick", "payload": {"ts": 1790510173260}})) == []
    assert hb["dropped"] == []
    # 噪音按**内层**事件名归类（报错时直接看到 health/tick，而不是外层信封名）
    assert hb["noise_kinds"] == {"health": 1, "tick": 1}


def test_relay_feed_heartbeat_without_attribution_is_noise_not_dropped(channel_cls):
    """不带任何归属字段的事件（网关广播的心跳/状态）算噪音，不占 dropped 样本——
    这样「无归属事件」报错里剩下的样本才是真·别人的 run 事件。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "type": "presence", "payload": {"host": "AutoClaw Server Mode"}})) == []
    assert state["dropped"] == []
    assert state["noise_kinds"].get("agent:stream") == 1
    # 带 runId/sessionKey 却没对上 → 才是真「归属未命中」，留样本
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "agent_other", "sessionKey": "agent:main:aclaw-other",
        "type": "text", "delta": "noise"})) == []
    assert state["dropped"]


def test_stream_chat_global_goes_straight_to_relay(channel_cls):
    """region=global 只走 relay bridge，不试 OpenAI 端点。

    2026-09-19 实测（live 账号 + 官方 bundle 对照）：官方网页/桌面端 bundle 里根本没有
    /v1/chat/completions 与 /v1/models，沙箱网关对它们稳定 404 {"ok":false,"error":
    "Not found"}——先试它只会白撞一次 404（用户看到的「notfound」就是它）还拖慢首字。
    """
    p = _ready_provider(channel_cls)
    p.region = "global"

    async def must_not_call(method, url, headers, **kw):
        raise AssertionError(f"global 不应尝试 OpenAI 端点: {url}")
        yield  # pragma: no cover

    p.send_sse_request = must_not_call

    events_resp = _FakeResp(status=200, chunks=_RELAY_EVENTS)
    send_resp = _FakeResp(status=200, json_obj={"ok": True, "data": {"runId": "r1"}})
    session = _FakeSession(get_resp=events_resp, post_resp=send_resp)
    p._make_session = lambda timeout=None: session

    async def no_ws(pp, bare):
        return None

    _patch_namespace(channel_cls, _device_online_ws=no_ws, _wake_sandbox=lambda *a, **k: _noop())

    frames = [f for f in _run(_collect_stream(channel_cls, p)) if isinstance(f, dict)]
    contents = "".join(f.get("content") or "" for f in frames)
    assert contents == "Hello world"
    # relay 通道走通：events GET + agent/send，全程没碰 /v1/chat/completions
    assert session.get_urls == ["https://gw.example.com/autoclaw-cloud/proxy/sb1/api/events"]
    assert session.post_calls[0]["url"] == "https://gw.example.com/autoclaw-cloud/proxy/sb1/api/electron/agent/send"


def test_stream_chat_propagates_non_fallback_errors(channel_cls):
    """通道 1 非 NotEnabled 错误（如 401）原样上抛，不回退 relay。"""
    p = _ready_provider(channel_cls)

    async def unauthorized_send(method, url, headers, **kw):
        raise HTTPException(401, "Unauthorized web bridge request")
        yield  # pragma: no cover

    p.send_sse_request = unauthorized_send
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())
    with pytest.raises(HTTPException) as excinfo:
        _run(_collect_stream(channel_cls, p))
    assert excinfo.value.status_code == 401


def test_stream_chat_requires_credential(channel_cls):
    p = _make_provider(channel_cls, password="")
    with pytest.raises(HTTPException) as excinfo:
        _run(_collect_stream(channel_cls, p))
    assert "access_token" in str(excinfo.value.detail)


# ==================== refresh_auth 编排 ====================

def test_refresh_auth_orchestrates_composite(channel_cls):
    """refresh_auth = 复合入口：token + 积分 + 沙箱 + 签到一次做完（与独立动作同实现）。"""
    p = _ready_provider(channel_cls)
    p.expires_at = "0"  # 强制临期 → 触发 refresh

    async def fake_userapi_json(pp, method, path, **kw):
        if path.endswith("/userapi/v1/refresh"):
            return {"access_token": "Bearer at-new", "refresh_token": "rt-new"}
        if path.endswith("/agent-assetmgr/api/v2/wallets"):
            return {"total_balance": "12345"}
        raise AssertionError(f"unexpected path {path}")

    async def fake_ensure_sandbox(pp):
        pp.sandbox_id = "sb2"
        pp.sandbox_endpoint = "https://gw/autoclaw-cloud/proxy/sb2"
        return "sb2", "https://gw/autoclaw-cloud/proxy/sb2"

    async def fake_checkin_fields(pp):
        return {"last_checkin": "已签到 +400", "last_checkin_status": "2026-09-28|ok|+400"}

    hook = channel_cls._spec_hooks["refresh_auth"]
    names = ("_userapi_json", "_ensure_sandbox", "_checkin_fields", "_today_checkin_done")
    saved = {name: hook.__globals__.get(name) for name in names}
    hook.__globals__["_userapi_json"] = fake_userapi_json
    hook.__globals__["_ensure_sandbox"] = fake_ensure_sandbox
    hook.__globals__["_checkin_fields"] = fake_checkin_fields
    hook.__globals__["_today_checkin_done"] = lambda pp: False
    try:
        fields = _run(hook(p, {"password": "Bearer at-old"}))
    finally:
        for name, fn in saved.items():
            hook.__globals__[name] = fn

    assert p.password == "Bearer at-new"
    assert p.refresh_token == "rt-new"
    assert fields["password"] == "Bearer at-new"
    assert fields["quota_text"] == "积分 12,345"
    assert fields["sandbox_id"] == "sb2"
    assert fields["last_checkin"] == "已签到 +400", "刷新授权是复合入口，顺带签到"


def test_refresh_auth_steps_fail_independently(channel_cls):
    """任一步失败不阻断其余：token 挂了积分/沙箱/签到照常跑。"""
    p = _ready_provider(channel_cls)
    p.expires_at = "0"

    async def failing_refresh(pp, method, path, **kw):
        if path.endswith("/userapi/v1/refresh"):
            raise HTTPException(401, "session dead")
        if path.endswith("/agent-assetmgr/api/v2/wallets"):
            return {"total_balance": 7}
        raise AssertionError(path)

    async def ok_sandbox(pp):
        return "sb1", "https://gw/autoclaw-cloud/proxy/sb1"

    async def fake_checkin_fields(pp):
        return {"last_checkin": "已签到", "last_checkin_status": "2026-09-28|already"}

    hook = channel_cls._spec_hooks["refresh_auth"]
    names = ("_userapi_json", "_ensure_sandbox", "_checkin_fields", "_today_checkin_done")
    saved = {name: hook.__globals__.get(name) for name in names}
    hook.__globals__["_userapi_json"] = failing_refresh
    hook.__globals__["_ensure_sandbox"] = ok_sandbox
    hook.__globals__["_checkin_fields"] = fake_checkin_fields
    hook.__globals__["_today_checkin_done"] = lambda pp: False
    try:
        fields = _run(hook(p, {}))
    finally:
        for name, fn in saved.items():
            hook.__globals__[name] = fn

    assert fields["quota_text"] == "积分 7"
    assert fields["sandbox_id"] == "sb1"
    assert fields["last_checkin"] == "已签到"


# ==================== 账号动作（perform_action）====================

def test_account_schema_declares_actions(channel_cls):
    """account_schema 声明两个独立动作：checkin（徽标，仅手动——refresh_auth 已含签到）
    与 sync_quota（值，默认每 6 小时调度刷新额度）。manual=True（账号行出手动按钮）。"""
    schema = channel_cls.account_schema()
    actions = {a["id"]: a for a in schema["account_actions"]}
    assert set(actions) == {"checkin", "sync_quota"}
    assert actions["checkin"]["display"]["badge_field"] == "last_checkin"
    # refresh_auth 是复合入口（含签到，调度默认每天 10:00），
    # checkin 动作只做「签到」按钮、不单独调度以免重复打上游。
    assert actions["checkin"]["default"] == {"enabled": False, "frequency_unit": "daily",
                                             "frequency_value": 1}
    assert actions["sync_quota"]["display"]["value_field"] == "quota_text"
    assert actions["sync_quota"]["default"]["frequency_unit"] == "hours"
    assert actions["sync_quota"]["default"]["frequency_value"] == 6
    assert actions["checkin"]["manual"] is True
    assert actions["sync_quota"]["manual"] is True
    assert "perform_action" in channel_cls._spec_hooks


def test_perform_action_sync_quota_only(channel_cls):
    """sync_quota 动作只拉积分写 quota_text，不碰签到/沙箱。"""
    p = _ready_provider(channel_cls)
    g = channel_cls._spec_hooks["perform_action"].__globals__

    async def fake_ensure_token(pp):
        return True

    async def fake_points(pp):
        return 12345

    saved = {name: g.get(name) for name in ("_ensure_access_token", "_fetch_points")}
    g["_ensure_access_token"] = fake_ensure_token
    g["_fetch_points"] = fake_points
    try:
        fields = _run(channel_cls.perform_account_action(p, "sync_quota", {"password": "t"}, None))
    finally:
        for name, fn in saved.items():
            g[name] = fn

    assert fields == {"quota_text": "积分 12,345"}
    assert "last_checkin" not in fields


def test_perform_action_checkin_once_per_day(channel_cls):
    """checkin 动作当日只跑一次：第二次执行看到 last_checkin_status 就跳过。"""
    p = _ready_provider(channel_cls)
    g = channel_cls._spec_hooks["perform_action"].__globals__
    calls: list[str] = []

    async def fake_ensure_token(pp):
        return True

    async def fake_checkin_fields(pp):
        calls.append(pp.username)
        fields = {"last_checkin": "已签到", "last_checkin_status": f"{g['_today_key']()}|already"}
        for k, v in fields.items():
            setattr(pp, k, v)
        return fields

    saved = {name: g.get(name) for name in ("_ensure_access_token", "_checkin_fields")}
    g["_ensure_access_token"] = fake_ensure_token
    g["_checkin_fields"] = fake_checkin_fields
    try:
        fields1 = _run(channel_cls.perform_account_action(p, "checkin", {"password": "t"}, None))
        fields2 = _run(channel_cls.perform_account_action(p, "checkin", {"password": "t"}, None))
    finally:
        for name, fn in saved.items():
            g[name] = fn

    assert len(calls) == 1                       # 当日第二次不再打上游
    assert fields1["last_checkin"] == "已签到"
    assert fields2 == {}                         # 跳过时不覆盖（返回空 dict）


def test_perform_action_checkin_fail_sets_error(channel_cls):
    """签到一个都没成：徽标字段照写，但带 _error（admin 记执行失败、不落库该键）。"""
    p = _ready_provider(channel_cls)
    g = channel_cls._spec_hooks["perform_action"].__globals__

    async def fake_ensure_token(pp):
        return True

    async def fake_checkin_fields(pp):
        return {"last_checkin": "签到失败", "last_checkin_status": f"{g['_today_key']()}|fail"}

    saved = {name: g.get(name) for name in ("_ensure_access_token", "_checkin_fields")}
    g["_ensure_access_token"] = fake_ensure_token
    g["_checkin_fields"] = fake_checkin_fields
    try:
        fields = _run(channel_cls.perform_account_action(p, "checkin", {"password": "t"}, None))
    finally:
        for name, fn in saved.items():
            g[name] = fn

    assert fields["last_checkin"] == "签到失败"
    assert "_error" in fields, "失败必须带 _error 让 admin 记执行结果"
    assert "last_checkin" in fields and "last_checkin_status" in fields


def test_perform_action_unknown_id_raises(channel_cls):
    p = _ready_provider(channel_cls)
    with pytest.raises(ValueError):
        _run(channel_cls.perform_account_action(p, "nope", {"password": "t"}, None))


# ==================== 每日签到 ====================

def test_checkin_tasks_match_go():
    """对齐 checkin.go checkinTasks 的 4 个任务（与 autoclaw_proxy.py 同一份）。"""
    mod = _module()
    assert mod.CHECKIN_TASKS == (
        "daily_signin", "daily_inspiration_center",
        "newbie_cloud_lobster", "newbie_local_lobster",
    )


def test_task_headers_match_checkin_go(channel_cls):
    """签到/余额用 commonHeaders 一套：小写 authorization、X-Client-Type: pc、X-Lang: en。

    与 userapi 那套（大写 Authorization + Origin/Referer）**不是同一个**——混用会被上游
    以 500 task user_id required 或 410000 拒。
    """
    mod = _module()
    p = _make_provider(channel_cls, password="Bearer tok-1")
    headers = mod._task_headers(p)
    assert headers["authorization"] == "Bearer tok-1"
    assert "Authorization" not in headers
    assert headers["X-Client-Type"] == "pc"
    assert headers["X-Lang"] == "en"
    assert headers["X-Auth-Sign"] and headers["X-Auth-Appid"] == mod.APP_ID
    assert "Accept" not in headers          # checkin.go 显式 delete(Accept)
    assert "Origin" not in headers and "Referer" not in headers


def test_do_checkin_counts_code_zero_as_done(channel_cls):
    """签到判定口径 = 官方 bundle 的 `ok: code===0`：不抛异常就算该任务已受理。

    data 里有没有 success/already_completed 字段都不影响判定——只看 code（_task_json
    已把 code!=0 转异常）。这正是「显示成功但其实没签上」的修复点。
    """
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    seen: list[str] = []

    async def fake_task_json(_p, _method, path, *, json_body=None, params=None):
        assert path == mod.TASK_COMPLETE_PATH
        task = json_body["task_id"]
        seen.append(task)
        if task == "daily_signin":
            return {"reward_points": 400}              # 只有分数字段
        if task == "daily_inspiration_center":
            return {}                                  # 空 data，也应算已受理
        if task == "newbie_cloud_lobster":
            raise mod.HTTPException(status_code=502, detail="boom")
        return {"points": 0}                           # 别名键、无奖励

    saved = mod._task_json
    mod._task_json = fake_task_json
    try:
        result = _run(mod._do_checkin(p))
    finally:
        mod._task_json = saved

    assert result["done"] == 3                         # code==0 的三个全算完成
    assert result["failed"] == 1
    assert result["reward"] == 400                     # 只累加 reward_points 别名
    assert set(result["details"]) == set(seen)


def test_do_checkin_task_list_drives_ids(channel_cls):
    """任务 id 优先取上游 task-list（静态表会过期）；count/unlimited 型跳过。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")

    async def fake_task_json(_p, _method, path, *, json_body=None, params=None):
        if path == mod.TASK_LIST_PATH:
            # _task_json 已剥掉外层信封，这里返回的就是 data 本体
            return [
                {"task_id": "daily_signin", "task_type": "daily", "client_triggered": True},
                {"task_id": "invite_friends", "task_type": "count"},
                {"task_id": "upgrade_pc_app", "task_type": "unlimited"},
                {"task_id": "daily_inspiration_center", "task_type": "daily", "client_triggered": True},
            ]
        raise AssertionError(path)

    saved = mod._task_json
    mod._task_json = fake_task_json
    try:
        ids = _run(mod._checkin_task_ids(p))
    finally:
        mod._task_json = saved

    assert ids == ["daily_signin", "daily_inspiration_center"]


def test_checkin_task_ids_falls_back_to_static_on_error(channel_cls):
    """拉不到 task-list 时回退静态表，不影响签到本身。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")

    async def boom(_p, _method, path, **kw):
        raise mod.HTTPException(status_code=502, detail="task-list 挂了")

    saved = mod._task_json
    mod._task_json = boom
    try:
        ids = _run(mod._checkin_task_ids(p))
    finally:
        mod._task_json = saved

    assert ids == list(mod.CHECKIN_TASKS)


def test_checkin_fields_write_badge_and_status(channel_cls):
    """签到字段：last_checkin 徽标 + last_checkin_status 当日幂等标记。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")

    async def fake_do_checkin(_p):
        return {"reward": 400, "done": 2, "failed": 0, "details": {}}

    saved = mod._do_checkin
    mod._do_checkin = fake_do_checkin
    try:
        fields = _run(mod._checkin_fields(p))
        done = mod._today_checkin_done(p)
    finally:
        mod._do_checkin = saved

    assert fields["last_checkin"] == "已签到 +400"
    assert fields["last_checkin_status"].startswith(mod._today_key() + "|ok|+400")
    assert done is True          # 签完当日幂等判断即为真 → 不再重签


def test_today_checkin_done_rejects_stale_and_failed(channel_cls):
    """当日幂等判断：昨天的记录 / 今天的失败记录都不算已签。"""
    mod = _module()
    g = channel_cls._spec_hooks["refresh_auth"].__globals__
    today = mod._today_key()
    p = _make_provider(channel_cls, password="t")

    p.last_checkin_status = "2020-01-01|ok|+400"
    assert g["_today_checkin_done"](p) is False
    p.last_checkin_status = f"{today}|fail"
    assert g["_today_checkin_done"](p) is False
    p.last_checkin_status = f"{today}|ok|+400"
    assert g["_today_checkin_done"](p) is True
    p.last_checkin_status = f"{today}|already"
    assert g["_today_checkin_done"](p) is True


def test_badge_text_variants(channel_cls):
    mod = _module()
    g = channel_cls._spec_hooks["refresh_auth"].__globals__
    assert g["_badge_text"]("ok", 400) == "已签到 +400"
    assert g["_badge_text"]("already", None) == "已签到"
    assert g["_badge_text"]("session_dead", None) == "签到失败 (会话)"
    assert g["_badge_text"]("fail", None) == "签到失败"


def test_refresh_auth_checkin_skipped_when_already_done(channel_cls):
    """refresh_auth 复合入口里签到当日只跑一次：第二次刷新看到 last_checkin_status 就跳过。"""
    p = _ready_provider(channel_cls)
    p.expires_at = "0"

    async def fake_userapi_json(pp, method, path, **kw):
        if path.endswith("/userapi/v1/refresh"):
            return {"access_token": "Bearer at-new", "refresh_token": "rt-new"}
        if path.endswith("/agent-assetmgr/api/v2/wallets"):
            return {"total_balance": "12345"}
        raise AssertionError(f"unexpected path {path}")

    calls: list[str] = []

    async def fake_task_json(_p, _method, path, *, json_body=None, params=None):
        calls.append(json_body["task_id"])
        return {"already_completed": True}

    async def ok_sandbox(pp):
        return "sb1", "https://gw/autoclaw-cloud/proxy/sb1"

    g = channel_cls._spec_hooks["refresh_auth"].__globals__
    saved = {n: g.get(n) for n in ("_userapi_json", "_task_json", "_ensure_sandbox")}
    g["_userapi_json"] = fake_userapi_json
    g["_task_json"] = fake_task_json
    g["_ensure_sandbox"] = ok_sandbox
    try:
        fields1 = _run(channel_cls._spec_hooks["refresh_auth"](p, {"password": "Bearer at-old"}))
        first = len(calls)
        fields2 = _run(channel_cls._spec_hooks["refresh_auth"](p, {"password": "Bearer at-old"}))
    finally:
        for n, fn in saved.items():
            g[n] = fn

    assert first == len(_module().CHECKIN_TASKS)   # 第一轮全跑
    assert len(calls) == first                      # 第二轮一次都不再打（当日已签）
    assert fields1["last_checkin"] == "已签到"       # 全部 already → 无得分
    assert "last_checkin" not in fields2            # 跳过时不覆盖


# ==================== 沙箱并发去重（对齐官方 in-flight 去重）====================

def test_relay_proxy_base_of_sandbox_endpoint():
    """签名辅助：sandbox_endpoint（wss 带 query）→ proxy base。"""
    mod = _module()
    assert mod._relay_proxy_base(
        "wss://gw/autoclaw-cloud/ws?sandbox_id=sb-new", "sb-new"
    ) == "https://gw/autoclaw-cloud/proxy/sb-new"


def test_ensure_sandbox_concurrent_creates_only_one(channel_cls):
    """缓存未命中时并发调用只 list/apply 一次——否则会建出多个沙箱（各自独立计费资源）。

    官方客户端用 promise 缓存（web bundle 的 he / Ce）防这个；这里用按账号的
    asyncio.Lock 等价实现（双检锁：等锁期间缓存已被前一个请求填好就直接返回）。

    用 _module() 的**独立命名空间**跑：channel_cls 的共享命名空间会被别的用例
    （_patch_namespace 只覆盖不还原）污染成 noop。
    """
    mod = _module()
    p = _make_provider(channel_cls, password="t")     # 无 sandbox 缓存 → 走网络路径
    calls: list[str] = []

    async def fake_userapi_json(pp, method, path, **kw):
        calls.append(f"{method} {path}")
        if path.endswith("/sandbox/apply"):
            await asyncio.sleep(0.05)                  # 放大并发窗口，逼出重复申请
            return {}
        if path.endswith("/sandbox/list"):
            if any("apply" in c for c in calls):
                return {"sandbox_list": [{
                    "sandbox_id": "sb-new", "end_timestamp": int(time.time()) + 3600,
                    "sandbox_endpoint": "wss://gw/autoclaw-cloud/ws?sandbox_id=sb-new"}]}
            return {"sandbox_list": []}
        raise AssertionError(path)

    saved = mod._userapi_json
    mod._userapi_json = fake_userapi_json
    try:
        async def run_all():
            return await asyncio.gather(*[mod._ensure_sandbox(p) for _ in range(5)])
        results = _run(run_all())
    finally:
        mod._userapi_json = saved

    assert all(r == ("sb-new", "https://gw/autoclaw-cloud/proxy/sb-new") for r in results)
    assert sum(1 for c in calls if "apply" in c) == 1          # 只申请一次
    # 列表只由第一个请求拉（apply 前后各一次）——其余 4 个等锁后命中缓存直接返回
    assert sum(1 for c in calls if c.endswith(mod.SANDBOX_LIST_PATH)) == 2


def test_ensure_sandbox_applies_official_default_name(channel_cls):
    """apply 的沙箱名用官方同款（bundle 常量 Ot / 桌面端 Ih，实测建出来就是 "AutoClaw"）。"""
    mod = _module()
    assert mod.DEFAULT_SANDBOX_NAME == "AutoClaw"

    p = _make_provider(channel_cls, password="t")
    bodies: list[dict] = []

    async def fake_userapi_json(pp, method, path, **kw):
        if path.endswith("/sandbox/apply"):
            bodies.append(kw.get("json_body") or {})
            return {}
        if path.endswith("/sandbox/list"):
            return {"sandbox_list": [{
                "sandbox_id": "sb-a", "end_timestamp": int(time.time()) + 3600,
                "sandbox_endpoint": "wss://gw/autoclaw-cloud/ws?sandbox_id=sb-a"}]} if bodies else {"sandbox_list": []}
        raise AssertionError(path)

    saved = mod._userapi_json
    mod._userapi_json = fake_userapi_json
    try:
        _run(mod._ensure_sandbox(p))
    finally:
        mod._userapi_json = saved

    assert bodies == [{"sandbox_name": "AutoClaw"}]
    assert p.sandbox_id == "sb-a"


def test_wake_sandbox_dedups_within_window(channel_cls):
    """60 秒窗口内的重复 wake（含同刻并发）只真发一次。

    官方客户端每请求都 wake（保活 10 分钟）；我们做短窗去重——省往返，也避免并发重复
    wake 撞上游的 apply_business_busy_cooldown（实测放大 403 比例）。
    """
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    posts: list[dict] = []

    async def fake_relay_json(pp, method, url, **kw):
        posts.append({"method": method, "url": url, "json": kw.get("json_body")})
        return {}

    saved = mod._relay_json
    mod._relay_json = fake_relay_json
    mod._WAKE_AT.clear()
    try:
        async def run_all():
            await asyncio.gather(*[mod._wake_sandbox(p, "sb1", "https://gw/autoclaw-cloud")
                                   for _ in range(5)])
        _run(run_all())
        assert len(posts) == 1
        assert posts[0]["url"] == "https://gw/autoclaw-cloud/v1/sandboxes/sb1/wake"
        # body 是纯控制面参数，不含任何聊天内容
        assert set(posts[0]["json"]) == {"access_hold_ms", "reason", "apply_business_busy_cooldown"}
        assert posts[0]["json"]["access_hold_ms"] == mod.WAKE_HOLD_MS

        # 窗口内的后续调用（新事件循环也一样）继续跳过
        _run(mod._wake_sandbox(p, "sb1", "https://gw/autoclaw-cloud"))
        assert len(posts) == 1
    finally:
        mod._relay_json = saved
        mod._WAKE_AT.clear()


def test_wake_sandbox_sends_again_after_window(channel_cls):
    """窗口过期后重新真发；wake 失败不占用窗口（下一个请求能重试）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    posts: list[str] = []
    fail_next = False

    async def fake_relay_json(pp, method, url, **kw):
        nonlocal fail_next
        posts.append(url)
        if fail_next:
            fail_next = False
            raise mod.HTTPException(status_code=502, detail="wake boom")
        return {}

    saved = mod._relay_json
    mod._relay_json = fake_relay_json
    mod._WAKE_AT.clear()
    try:
        _run(mod._wake_sandbox(p, "sb1", "https://gw/autoclaw-cloud"))
        assert len(posts) == 1

        # 失败不记窗口：先清窗口再让 wake 失败 → 下一个请求照常重试
        mod._WAKE_AT.clear()
        fail_next = True
        with pytest.raises(HTTPException):
            _run(mod._wake_sandbox(p, "sb1", "https://gw/autoclaw-cloud"))
        assert len(posts) == 2
        assert mod._WAKE_AT == {}          # 失败没写窗口
        _run(mod._wake_sandbox(p, "sb1", "https://gw/autoclaw-cloud"))
        assert len(posts) == 3

        # 把窗口拨到过期 → 允许再发
        for k in list(mod._WAKE_AT):
            mod._WAKE_AT[k] -= mod.WAKE_DEDUP_SECONDS + 1
        _run(mod._wake_sandbox(p, "sb1", "https://gw/autoclaw-cloud"))
        assert len(posts) == 4
    finally:
        mod._relay_json = saved
        mod._WAKE_AT.clear()


# ==================== fetch_models ====================

def test_fetch_models_fallback_static_without_credential(channel_cls):
    p = _make_provider(channel_cls, password="")
    models = _run(channel_cls.fetch_upstream_model_list(p))
    ids = [m["id"] for m in models]
    assert "openclaw" in ids and "glm-5.3-flash" in ids and "zai_auto" in ids
    assert p._last_fetch_models_error


def test_fetch_models_dynamic_from_sandbox(channel_cls):
    p = _ready_provider(channel_cls)

    session = _FakeSession(request_resp=_FakeResp(status=200, json_obj={
        "object": "list", "data": [{"id": "openclaw"}, {"id": "openclaw/designer"}]}))
    p._make_session = lambda timeout=None: session
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())

    models = _run(channel_cls.fetch_upstream_model_list(p))
    assert [m["id"] for m in models] == ["openclaw", "openclaw/designer"]
    assert p._last_fetch_models_error == ""
    assert session.request_calls[0]["url"].endswith("/v1/models")


# ==================== is_init / check_auth ====================

def test_is_init_and_check_auth(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="")
    assert channel_cls.is_init(p) is False
    assert _run(channel_cls.check_auth(p)) is False
    p2 = _make_provider(channel_cls, password=_credential_json())
    assert channel_cls.is_init(p2) is True
    assert _run(channel_cls.check_auth(p2)) is True
    # 未解析的 JSON（init_auth 未跑）也认已配置
    p3 = _make_provider(channel_cls, password='{"accessToken": "at-1"}')
    assert _run(channel_cls.check_auth(p3)) is True


# ==================== 登录授权（短信 / Google）====================

def test_begin_device_flow_sms_mode(channel_cls):
    """cn + 手机号：发短信、poll_params 带 login=sms + phone，并给出短信交互提示。"""
    p = _ready_provider(channel_cls)
    p.phone = "13800001234"
    sent = {}

    async def fake_send_code(pp, phone):
        sent["phone"] = phone

    _patch_namespace(channel_cls, _sms_send_code=fake_send_code)
    result = _run(channel_cls.begin_device_flow(p))
    assert sent["phone"] == "13800001234"
    assert result["task_type"] == "device_code"
    assert result["poll_params"]["login"] == "sms"
    assert result["poll_params"]["phone"] == "13800001234"
    # 交互提示：补投框要填的是验证码，不是回调 URL
    assert "6 位" in result["replay_hint"]
    assert result["replay_label"]
    assert result["polling_hint"]
    # 短信流程没有要打开的网页——返回 auth_url 会把用户误导去点官网首页
    assert not result.get("auth_url")
    assert not result.get("verification_uri")


def test_begin_device_flow_requires_phone_for_cn(channel_cls):
    """cn 无手机号：明确报错引导填手机号（站内会话没有可粘贴的回调 URL）。"""
    p = _ready_provider(channel_cls)
    p.phone = ""
    with pytest.raises(HTTPException) as excinfo:
        _run(channel_cls.begin_device_flow(p))
    assert "手机号" in str(excinfo.value.detail)


def test_begin_device_flow_global_waits_for_credential_paste(channel_cls):
    """global：不再报错，改为「网页登录 + 凭证补投」的等待态（有完成路径才返回 auth_url）。

    旧实现直接 400，把用户丢在死胡同里——海外区等于无法授权。现在返回 pending 设备码态，
    提示文案直接给出控制台命令，用户照着做即可。
    """
    p = _ready_provider(channel_cls)
    p.phone = ""
    p.region = "global"

    async def fail_send(pp, phone):
        raise AssertionError("global 不应发短信")

    _patch_namespace(channel_cls, _sms_send_code=fail_send)
    result = _run(channel_cls.begin_device_flow(p))

    assert result["task_type"] == "device_code"
    assert result["poll_params"]["login"] == "credential"
    assert result["poll_params"]["region"] == "global"
    # 用户要先在官网登录才能拿到 localStorage，现在有完成路径了，返回它不再误导
    assert "autoclaw.z.ai" in result["auth_url"]
    # 文案必须给可照抄的动作：一句全浏览器可用的 JS + 三步指引
    msg = result["message"]
    # 真形态（对照 2026-09-19 live bundle）：access token 平铺在 autoclaw.web.authToken，
    # loginInfo/userInfo 是可能缺失/非法的 JSON 字符串，取不到时要能诊断（曾被反馈
    # 「执行显示的是空」看不出原因）。
    # 交付方式**不能是 prompt()**：浏览器一旦勾过「阻止此页面创建更多对话框」，prompt 会
    # 静默失败只返回 undefined（实测症状：console 只有 undefined、什么都不显示）。
    # 改为浮层 textarea + execCommand('copy') + console.log 三保险。
    assert "prompt(" not in msg                                 # 不再用会被静默屏蔽的 prompt
    assert "createElement('textarea')" in msg                   # 浮层文本框交付
    assert "execCommand('copy')" in msg                         # 自动复制到剪贴板
    assert "console.log(" in msg                                # console 同步打印（prompt 被屏蔽也能拿到）
    assert "localStorage" in msg and "sessionStorage" in msg    # 两种存储都扫（token 可能在任一）
    assert "没找到凭证" in msg                                   # 空状态诊断，而非静默全空
    assert "origin=" in msg and "localStorage 键：" in msg       # 诊断输出 origin + 全部键名
    # 裸 copy() 只在 Chrome DevTools 控制台有，Firefox/地址栏/书签里会 ReferenceError，不能用
    assert "copy()" not in msg
    assert result["replay_hint"] and result["replay_label"] and result["polling_hint"]


def test_credential_copy_js_roundtrip(channel_cls):
    """_CREDENTIAL_COPY_JS 的输出必须是服务端 _credential_doc_from_payload 直接认的形态。

    用 Node 把 spec 里的 JS 真执行一遍（localStorage 按 live bundle 的键名），浮层输出
    反喂给服务端识别链路：能建号才算闭环。
    """
    mod = _module()
    js = getattr(mod, "_CREDENTIAL_COPY_JS", "")
    assert js, "spec 未定义 _CREDENTIAL_COPY_JS"
    # 静态保真：JS 必须是单行（多行会被 admin 前端折成一行，用户复制到的就是断的）
    assert "\n" not in js
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("无 node：跳过 JS 实执行")
    store = json.dumps({
        "autoclaw.web.authToken": "Bearer eyJ.WEB",
        "autoclaw.web.refreshToken": "eyJ.REF",
        "autoclaw.web.deviceId": "646d6b4c-96a4",
        "autoclaw.web.userId": "1487730d",
        "autoclaw.web.userInfo": json.dumps({"user_id": "1487730d", "user_name": "wu xin"}),
        "autoclaw.web.loginInfo": json.dumps({
            "access_token": "Bearer eyJ.LOGIN", "refresh_token": "eyJ.REF",
            "user_id": "1487730d", "user_name": "wu xin"}),
        "autoclaw.web.runtimeSandboxId": "sb_23a6",
    })
    # 不用 prompt 交付（浏览器一旦勾过「阻止此页面创建更多对话框」，prompt 会静默失败只返回
    # undefined——实测症状就是 console 只有 undefined）；JS 走浮层 textarea + execCommand('copy')。
    # document / console 需要桩掉，IIFE 的返回值就是交付内容。
    script = (
        "const S=" + store + ";const ks=Object.keys(S);"
        "const L=new Proxy({length:ks.length,key:i=>ks[i]??null,getItem:k=>S[k]??null},"
        "{get(t,k){if(k in t)return t[k];return S[k]}});"
        "const location={origin:'https://autoclaw.z.ai'};"
        "const localStorage=L;const sessionStorage={length:0,key:()=>null,getItem:()=>null};"
        "const document={createElement:()=>({value:'',style:{},focus(){},select(){},"
        "set onkeydown(f){}}),body:{appendChild(){}},execCommand:()=>true};"
        "const console={log(){}};"
        "process.stdout.write(String(" + js + "))"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    doc = mod._credential_doc_from_payload(out.stdout.strip())
    # JS 在客户端就定死优先级：accessToken 取 authToken（网页每次 refresh 都刷新、
    # 网页自己调 userapi 用的就是它），loginInfo.access_token 只是登录那一刻的快照、
    # 网页 token 刷新过后就陈旧。故两个键都在时，结果必须是 authToken 那个。
    assert doc["accessToken"] == "Bearer eyJ.WEB"
    assert doc["refreshToken"] == "eyJ.REF"
    assert doc["deviceId"] == "646d6b4c-96a4"
    assert doc["userName"] == "wu xin"


def test_begin_device_flow_global_never_sends_sms_even_with_phone(channel_cls):
    """global 填了手机号也一样走凭证补投：海外短信登录未验证，不能悄悄走没测过的路。"""
    p = _ready_provider(channel_cls)
    p.phone = "13800001234"
    p.region = "global"

    async def fail_send(pp, phone):
        raise AssertionError("global 不应发短信")

    _patch_namespace(channel_cls, _sms_send_code=fail_send)
    result = _run(channel_cls.begin_device_flow(p))
    assert result["poll_params"]["login"] == "credential"


def test_sms_login_body_shape(channel_cls):
    """agent-login：无斜杠路径、code 必须 int、platform=web。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    calls = {}

    async def fake_userapi(pp, method, path, **kw):
        calls.update({"path": path, "body": kw.get("json_body")})
        return {"access_token": "Bearer at-new", "refresh_token": "rt-new",
                "user_id": "u1", "user_name": "张三", "phone": "13800001234"}

    hook = mod._sms_login
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        account = _run(mod._sms_login(p, "13800001234", "123456"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert calls["path"] == "/userapi/v1/agent-login"  # 带尾斜杠会 307
    assert calls["body"]["code"] == 123456 and isinstance(calls["body"]["code"], int)
    assert calls["body"]["platform"] == "web"
    assert account["password"] == "Bearer at-new"
    assert account["user_id"] == "u1"
    assert int(account["expires_at"]) > int(time.time())


def test_sms_login_rejects_non_numeric_code(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="")
    with pytest.raises(HTTPException) as excinfo:
        _run(mod._sms_login(p, "13800001234", "abc123"))
    assert "验证码" in str(excinfo.value.detail)


def test_google_oauth_exchange_body_shape(channel_cls):
    """google-oauth-login：免验证码（无 rid），body 对齐官方 web 端 google 分支。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    calls = {}

    async def fake_userapi(pp, method, path, **kw):
        calls.update({"path": path, "body": kw.get("json_body")})
        return {"access_token": "at-g", "refresh_token": "rt-g",
                "user_id": "u-g", "user_name": "g@example.com"}

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        account = _run(mod._google_oauth_exchange(p, "code-x", "state-x"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert calls["path"] == "/userapi/overseasv1/google-oauth-login"
    body = calls["body"]
    assert body["code"] == "code-x" and body["state"] == "state-x"
    # navigate_uri 跟随 region 的 userapi 域名（测试实例默认 region=cn）
    assert body["navigate_uri"] == "https://autoglm-acceleration-api.zhipuai.cn/userapi/oauth/google/callback"
    assert body["source_id"] == "web"
    assert body["flow_type"] == "web" and body["client_type"] == "web"
    assert account["password"] == "at-g"
    assert account["region"] == "cn"  # 测试实例默认 region=cn（落库时由会话 region 覆盖）


def test_google_oauth_exchange_navigate_uri_follows_region(channel_cls):
    """region=global 的账号用海外域名换 token——写死域名会让它「登录成功但识别不出来」。"""
    mod = _module()
    p = _make_provider(channel_cls, password="", region="global")
    p.device_id = "dev-1"
    seen = {}

    async def fake_userapi(pp, method, path, **kw):
        seen["uri"] = kw.get("json_body", {}).get("navigate_uri")
        return {"access_token": "at-g", "refresh_token": "rt-g"}

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        _run(mod._google_oauth_exchange(p, "code-x", "state-x"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert seen["uri"] == "https://autoglm-api.autoglm.ai/userapi/oauth/google/callback"


def test_google_oauth_exchange_retries_other_state_shape(channel_cls):
    """官方 state 末段为空时会省略尾 '_'：首投失败换另一种形态重试。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    tried: list[str] = []

    async def fake_userapi(pp, method, path, **kw):
        tried.append(kw.get("json_body", {}).get("state"))
        if len(tried) == 1:
            raise HTTPException(status_code=400, detail="state 不匹配")
        return {"access_token": "at-g", "refresh_token": "rt-g"}

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        account = _run(mod._google_oauth_exchange(p, "code-x", "state-x_"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert tried == ["state-x_", "state-x"]
    assert account["password"] == "at-g"


def test_google_oauth_exchange_does_not_retry_on_401(channel_cls):
    """401 = code 已被消费/授权过期，换 state 形态也救不回来，不能重复打上游。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    tried: list[str] = []

    async def fake_userapi(pp, method, path, **kw):
        tried.append(kw.get("json_body", {}).get("state"))
        raise HTTPException(status_code=401, detail="code 已失效")

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        with pytest.raises(HTTPException) as excinfo:
            _run(mod._google_oauth_exchange(p, "code-x", "state-x_"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert excinfo.value.status_code == 401
    assert tried == ["state-x_"]  # 只投一次


def test_loopback_sms_claims_and_persists_phone(channel_cls):
    """补投 code=数字 → 短信换 token → account_data 带 phone。"""
    p = _ready_provider(channel_cls)

    async def fake_sms_login(pp, phone, code):
        assert phone == "13800001234" and code == "654321"
        return {"password": "Bearer at-1", "refresh_token": "rt-1",
                "user_id": "u1", "user_name": "张三", "phone": "13800001234",
                "expires_at": "1", "device_id": "dev-1", "region": "cn"}

    _patch_namespace(channel_cls, _sms_login=fake_sms_login)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "654321"}, {"login": "sms", "phone": "13800001234"}))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["phone"] == "13800001234"


def test_loopback_google_claims_code_without_state(channel_cls):
    """google 会话只粘到 code（没 state）也必须认领——否则海外登录成功却一直 pending。"""
    p = _ready_provider(channel_cls)
    seen = {}

    async def fake_exchange(pp, code, state):
        seen["code"] = code
        seen["state"] = state
        return {"password": "at-g", "refresh_token": "rt-g", "user_id": "u-g",
                "expires_at": "1", "device_id": "dev-1", "region": "global"}

    _patch_namespace(channel_cls, _google_oauth_exchange=fake_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "4/0AY-code"}, {"login": "google"}))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["password"] == "at-g"
    assert seen == {"code": "4/0AY-code", "state": ""}


def test_loopback_sms_session_not_hijacked_by_state_param(channel_cls):
    """sms 会话误粘带 state 的 URL：按会话流程走短信，不能被打进 google 换 token。"""
    p = _ready_provider(channel_cls)
    called: list[str] = []

    async def fake_sms_login(pp, phone, code):
        called.append("sms")
        return {"password": "Bearer at-1", "refresh_token": "rt-1", "user_id": "u1",
                "expires_at": "1", "device_id": "dev-1", "region": "cn"}

    async def fake_exchange(pp, code, state):
        called.append("google")
        raise AssertionError("sms 会话不应走 google 换 token")

    _patch_namespace(channel_cls, _sms_login=fake_sms_login,
                     _google_oauth_exchange=fake_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "654321", "state": "leaked"}, {"login": "sms", "phone": "13800001234"}))
    assert outcome["status"] == "authorized"
    assert called == ["sms"]


def test_loopback_unknown_login_marker_not_claimed(channel_cls):
    """认不出的会话标记（既非 sms 也非 google）→ 不认领，交下一个候选会话。"""
    p = _ready_provider(channel_cls)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "whatever"}, {"login": "something-else"}))
    assert outcome is None


def test_loopback_google_exchanges_code_state(channel_cls):
    """补投 Google 回调 URL（code+state）→ 免验证码换 token。"""
    p = _ready_provider(channel_cls)

    async def fake_exchange(pp, code, state):
        assert code == "4/0AY" and state == "st-1"
        return {"password": "at-g", "refresh_token": "rt-g", "user_id": "u-g",
                "user_name": "g@example.com", "expires_at": "1",
                "device_id": "dev-1", "region": "global"}

    _patch_namespace(channel_cls, _google_oauth_exchange=fake_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "4/0AY", "state": "st-1"}, {"login": "google"}))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["user_id"] == "u-g"


def test_loopback_error_returns_error_status(channel_cls):
    """换 token 失败 → {"status": "error"}（会话终止、前端显示原因），不静默。"""
    p = _ready_provider(channel_cls)

    async def failing_exchange(pp, code, state):
        raise HTTPException(400, "code 已过期")

    _patch_namespace(channel_cls, _google_oauth_exchange=failing_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "x", "state": "y"}, {"login": "google"}))
    assert outcome["status"] == "error"
    assert "过期" in outcome["error"]


# ==================== 补投凭证识别（localStorage / JSON / 裸 token）====================

def test_credential_payload_from_localstorage_json():
    """官网控制台 copy(localStorage.getItem('autoclaw.web')) → 严格 JSON，直接认。"""
    mod = _module()
    doc = mod._credential_doc_from_payload(json.dumps({
        "accessToken": "Bearer at-ls", "refreshToken": "rt-ls",
        "deviceId": "dev-ls", "userId": "u-ls", "region": "global",
    }))
    assert doc["accessToken"] == "Bearer at-ls"
    assert doc["refreshToken"] == "rt-ls"


def test_credential_payload_drills_into_nested_json():
    """整份 localStorage dump / 带外层 key 的形态：递归下钻找到凭证对象。"""
    mod = _module()
    # {"autoclaw.web": {...}} —— 直接嵌对象
    doc = mod._credential_doc_from_payload(json.dumps({
        "autoclaw.web": {"accessToken": "Bearer at-n", "refreshToken": "rt-n"},
        "some.other.key": "noise",
    }))
    assert doc["accessToken"] == "Bearer at-n"
    # {"autoclaw.web": "<JSON 字符串>"} —— localStorage 存的是字符串
    inner = json.dumps({"accessToken": "Bearer at-s", "deviceId": "dev-s"})
    doc = mod._credential_doc_from_payload(json.dumps({"autoclaw.web": inner}))
    assert doc["accessToken"] == "Bearer at-s"
    assert doc["deviceId"] == "dev-s"


def test_credential_payload_from_flat_namespaced_keys(channel_cls):
    """AutoClaw 网页真形态：localStorage 平铺键 + 嵌套 JSON 字符串混排（实测 dump）。

    关键点：**没有顶层 accessToken**——access token 藏在 ``autoclaw.web.loginInfo``
    这个 JSON 字符串里；refreshToken/deviceId/userId 是顶层平铺键；还有键名里嵌 JWT 的
    （``oauthNavigateUri.zai.<JWT>``）。识别必须**合并**各处字段，不能「找到第一个含
    token 的对象就返回」（那样只会拿到 loginInfo、丢掉 deviceId，device_id 被自动重生成
    会导致 refresh 续不上期）。

    另：``authToken``（顶层平铺，网页每次调 userapi 都带它、每次 refresh 都刷新）必须
    **压过** ``loginInfo.access_token``（只在登录响应里写一次，网页刷新过就陈旧）。
    用陈旧 token 建号会稳定 401 / code=410000（"This feature is available after logging in"）。
    """
    mod = _module()
    payload = json.dumps({
        "autoclaw.web.userId": "1487730d62f74187a181e8ad9859febd",
        "autoclaw.web.oauthResult": json.dumps(
            {"ok": True, "provider": "google", "msg": "登录成功", "id": "e8aa5fd1"}),
        "autoclaw.web.runtimeSandboxId": "sb_23a6f18afe234c42b14c3be857498295",
        "autoclaw.web.refreshToken": "Bearer rt-FLAT",
        "autoclaw.web.oauthNavigateUri.zai.eyJ2IjoxLCJwIjoiemFpIn0.abc":
            json.dumps({"navigateUri": "https://autoclaw.z.ai/web/?webOAuthCallback=zai"}),
        "autoclaw.web.authToken": "Bearer auth-WEB",
        "autoclaw.web.loginInfo": json.dumps({
            "sub_id": "108", "user_id": "14877", "user_name": "wu xin",
            "access_token": "Bearer at-LOGIN", "refresh_token": "Bearer rt-LOGIN"}),
        "autoclaw.web.userInfo": json.dumps({
            "id": 147417, "user_phone": "", "user_name": "wu xin",
            "email": "wuxin1903@gmail.com", "config": {"vm_init": False}}),
        "autoclaw.web.deviceId": "646d6b4c-96a4-4138-bd5b-3ba159fc836a",
        "autoclaw.web.riskAgreementRead": "1",
    })
    doc = mod._credential_doc_from_payload(payload)
    # access token 取自 authToken（网页会话 token，实时且网页自己就用它）；
    # loginInfo.access_token 是登录那一刻的快照，refresh 后陈旧，不抢首选
    assert doc["accessToken"] == "Bearer auth-WEB"
    # refreshToken/deviceId/userId 取自顶层平铺键（先到先得，平铺键是实时状态）
    assert doc["refreshToken"] == "Bearer rt-FLAT"
    assert doc["deviceId"] == "646d6b4c-96a4-4138-bd5b-3ba159fc836a"
    assert doc["userId"] == "1487730d62f74187a181e8ad9859febd"
    assert doc["userName"] == "wu xin"
    # runtimeSandboxId 刻意不映射成 sandboxId（运行时沙箱 ≠ 持久沙箱，拼错 proxy base 全挂）
    assert "sandboxId" not in doc
    # 未识别的键（riskAgreementRead 等）不进凭证 doc
    assert "riskAgreementRead" not in doc

    # 端到端：合并出的 doc 真能建号，且 deviceId 用的是 dump 里的（refresh 才续得上期）
    p = _make_provider(channel_cls, password="")
    assert mod._apply_credential_doc(p, doc) is True
    assert p.password == "Bearer auth-WEB"
    # refresh_token 落库前统一剥掉 Bearer：上游 refresh 请求要的是裸 token，
    # 带着前缀会稳定续期失败（dump 里 refreshToken 的写法不统一）
    assert p.refresh_token == "rt-FLAT"
    assert p.device_id == "646d6b4c-96a4-4138-bd5b-3ba159fc836a"
    assert p.user_name == "wu xin"

    # 包裹引号（控制台字符串回显形态 '{"..."}'）也要认
    assert mod._credential_doc_from_payload("'" + payload + "'") == doc


def test_credential_payload_auth_token_fallback(channel_cls):
    """dump 里没有 loginInfo、只有 authToken → 照样建号（authToken 本就是首选）。"""
    mod = _module()
    doc = mod._credential_doc_from_payload(json.dumps({
        "autoclaw.web.authToken": "Bearer auth-ONLY",
        "autoclaw.web.deviceId": "dev-2",
    }))
    assert doc["accessToken"] == "Bearer auth-ONLY"
    assert doc["deviceId"] == "dev-2"


def test_credential_payload_auth_token_wins_regardless_of_key_order():
    """authToken 覆盖 loginInfo 不依赖 JSON 键序（平铺键可能排在任何位置）。

    只靠别名表「先到先得」时，loginInfo 排在 authToken 前面就会赢——而陈旧 token
    正是 410000 的根因。故必须单独再扫一趟 authToken 并覆盖。
    """
    mod = _module()
    # authToken 排在 loginInfo **之后**（先到先得会把陈旧的 loginInfo 留下）
    payload = json.dumps({
        "autoclaw.web.loginInfo": json.dumps({"access_token": "Bearer at-STALE"}),
        "autoclaw.web.authToken": "Bearer auth-FRESH",
    })
    assert mod._credential_doc_from_payload(payload)["accessToken"] == "Bearer auth-FRESH"



def test_credential_payload_from_js_object_literals():
    """控制台直接复制对象（单引号/无引号键）不是合法 JSON → JS 字面量兜底。"""
    mod = _module()
    doc = mod._credential_doc_from_payload(
        "{accessToken: 'Bearer at-js', refreshToken: \"rt-js\", deviceId: 'dev-js'}")
    assert doc["accessToken"] == "Bearer at-js"
    assert doc["refreshToken"] == "rt-js"
    # 带命名空间前缀的键名（控制台展开 localStorage 时常见）同样归一
    doc = mod._credential_doc_from_payload(
        "{autoclaw.web.accessToken: 'Bearer at-ns', autoclaw.web.deviceId: 'dev-ns'}")
    assert doc["accessToken"] == "Bearer at-ns"
    assert doc["deviceId"] == "dev-ns"


def test_credential_payload_bare_token():
    mod = _module()
    token = "eyJhbGciOiJIUzI1NiJ9" + "x" * 40
    assert mod._credential_doc_from_payload(token)["accessToken"] == token
    assert mod._credential_doc_from_payload(f"Bearer {token}")["accessToken"] == f"Bearer {token}"


def test_credential_payload_never_mistakes_callback_url_for_token():
    """回调 URL / 短文本不能被当成裸 token——否则会把垃圾写进 access_token。"""
    mod = _module()
    for raw in (
        "http://127.0.0.1:8001/oauth/callback?code=abc&secret=s1",
        "https://autoglm-api.autoglm.ai/userapi/oauth/google/callback?state=st&code=4/0AY",
        "123456",           # 短信验证码
        "", "   ", None,
        "not a url",
    ):
        assert mod._credential_doc_from_payload(raw) == {}, raw


def test_loopback_credential_paste_creates_account(channel_cls):
    """海外区主路径：粘凭证 JSON（params 为空，原文走 callback_url）→ 自动建号。"""
    p = _ready_provider(channel_cls)
    p.region = "global"
    raw = json.dumps({
        "accessToken": "Bearer at-gl", "refreshToken": "rt-gl",
        "deviceId": "dev-gl", "userId": "u-gl", "userName": "g@example.com",
        "region": "global",
    })
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential", "region": "global"}, raw))

    assert outcome["status"] == "authorized"
    account = outcome["account_data"]
    assert account["password"] == "Bearer at-gl"
    assert account["refresh_token"] == "rt-gl"
    assert account["device_id"] == "dev-gl"
    assert account["user_id"] == "u-gl"
    assert account["region"] == "global"


def test_loopback_credential_paste_without_session_marker_still_claimed(channel_cls):
    """凭证 JSON 的 base64 '=' 会被 query 解析切碎 → params 空也必须能认（靠原文）。"""
    p = _ready_provider(channel_cls)
    raw = json.dumps({"accessToken": "Bearer at-x", "refreshToken": "rt-x"})
    outcome = _run(channel_cls.handle_loopback_callback(p, {}, {}, raw))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["password"] == "Bearer at-x"


def test_loopback_credential_paste_takes_region_from_session(channel_cls):
    """凭证块（尤其 localStorage dump）**不含** region 键 → 用会话发起时选的区域补位。

    缺 region 会落到 schema 默认 cn，把海外 token 送到大陆域名 → 稳定 410000。
    """
    p = _ready_provider(channel_cls)
    p.region = "cn"      # 会话未改过区域（schema 默认）
    raw = json.dumps({"accessToken": "Bearer at-gl", "refreshToken": "rt-gl"})
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential", "region": "global"}, raw))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["region"] == "global"


def test_loopback_credential_paste_keeps_region_from_credential(channel_cls):
    """凭证里明确写了 region → 不被会话值覆盖（凭证是更权威的自我描述）。"""
    p = _ready_provider(channel_cls)
    p.region = "global"
    raw = json.dumps({"accessToken": "Bearer at-cn", "refreshToken": "rt-cn",
                      "region": "cn"})
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential", "region": "global"}, raw))
    assert outcome["account_data"]["region"] == "cn"


def test_loopback_credential_paste_fills_sandbox(channel_cls):
    """凭证块没有 sandbox_id/endpoint → 补投时拉一次沙箱列表回填，别让账号裸奔。

    没有沙箱的账号聊天会去拼一个空 proxy base，表现为「授权成功但一发消息就报错」。
    """
    p = _ready_provider(channel_cls)
    p.sandbox_id = ""
    p.sandbox_endpoint = ""
    p.sandbox_end_ts = ""
    p.region = "global"

    async def fake_userapi_json(pp, method, path, **kw):
        assert path.endswith("/agentdr/v2/assistant/sandbox/list")
        return {"sandbox_list": [{
            "sandbox_id": "sb-gl", "end_timestamp": int(time.time()) + 3600,
            "sandbox_endpoint": "wss://gw.example.com/autoclaw-cloud/ws?sandbox_id=sb-gl",
        }]}

    hook = channel_cls._spec_hooks.get("stream_chat") or channel_cls._spec_hooks.get("refresh_auth")
    saved = hook.__globals__["_userapi_json"]
    hook.__globals__["_userapi_json"] = fake_userapi_json
    try:
        raw = json.dumps({"accessToken": "Bearer at-gl", "refreshToken": "rt-gl"})
        outcome = _run(channel_cls.handle_loopback_callback(
            p, {}, {"login": "credential", "region": "global"}, raw))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert outcome["status"] == "authorized"
    account = outcome["account_data"]
    assert account["sandbox_id"] == "sb-gl"
    assert account["sandbox_endpoint"] == "https://gw.example.com/autoclaw-cloud/proxy/sb-gl"


def test_loopback_credential_paste_sandbox_failure_still_authorizes(channel_cls):
    """沙箱拉取失败不该让整次授权失败——请求路径会再试，账号先建出来。"""
    p = _ready_provider(channel_cls)
    p.sandbox_id = ""
    p.sandbox_endpoint = ""
    p.sandbox_end_ts = ""
    p.region = "global"

    async def failing_userapi_json(pp, method, path, **kw):
        raise HTTPException(502, "沙箱服务抖动")

    hook = channel_cls._spec_hooks.get("stream_chat") or channel_cls._spec_hooks.get("refresh_auth")
    saved = hook.__globals__["_userapi_json"]
    hook.__globals__["_userapi_json"] = failing_userapi_json
    try:
        raw = json.dumps({"accessToken": "Bearer at-gl", "refreshToken": "rt-gl"})
        outcome = _run(channel_cls.handle_loopback_callback(
            p, {}, {"login": "credential", "region": "global"}, raw))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["password"] == "Bearer at-gl"
    assert outcome["account_data"]["sandbox_id"] == ""


def test_loopback_credential_session_rejects_unrecognized_paste(channel_cls):
    """等凭证的会话粘了别的东西 → 明确报错终止，别让前端一直 pending。"""
    p = _ready_provider(channel_cls)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential"}, "just some random text"))
    assert outcome["status"] == "error"
    assert "凭证" in outcome["error"]


def test_loopback_sms_accepts_bare_code(channel_cls):
    """前端占位符提示「输入 6 位验证码」→ 用户直接敲 123456，必须认。"""
    p = _ready_provider(channel_cls)
    seen = {}

    async def fake_sms_login(pp, phone, code):
        seen["phone"], seen["code"] = phone, code
        return {"password": "Bearer at-1", "refresh_token": "rt-1", "user_id": "u1",
                "expires_at": "1", "device_id": "dev-1", "region": "cn"}

    _patch_namespace(channel_cls, _sms_login=fake_sms_login)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "sms", "phone": "13800001234"}, "654321"))
    assert outcome["status"] == "authorized"
    assert seen == {"phone": "13800001234", "code": "654321"}
