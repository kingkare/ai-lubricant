"""resolve_principal 真实链路：cookie → session_store → 用户行 → role。

test_realtime_gateway.py / test_realtime_ws_protocol.py 里这条链路全被
monkeypatch 掉了；这里用假 session store + 假 User 模型跑通分支逻辑本身：

- cookie 有效 + 用户 active → principal 带 role
- cookie 有效但用户被删/封/停用 → None（fail closed）
- 用户行是 admin → role=admin，任务频道走特权分支
- CDP client_id + 内部 token → owner user_id + role
- CDP token 错 → None（不落到 cookie 分支）
- 内部 token（无 client_id）→ service 身份
- 什么都没有 → None

不打真库（无 PG/Redis 的环境也能跑），只验分支与字段传递。
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest


class _FakeWS:
    """够 resolve_principal 用的最小 WebSocket 桩（只读 headers/query_params）。"""

    def __init__(self, headers: dict[str, str] | None = None, query: dict[str, str] | None = None):
        self.headers = headers or {}
        self.query_params = query or {}


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_data: Any,
    user_row: Any,
    cdp_owner: str | None = None,
    internal_token: str | None = None,
    admin_token_valid: bool = False,
):
    """把 resolve_principal 依赖的四个模块装成可控假货，返回捕获到的调用。"""
    captured: dict[str, Any] = {"cookie_set": None}

    # ── admin._current_user_cookie：ContextVar 桩 ──
    import contextvars

    fake_admin = types.ModuleType("admin")
    cv: contextvars.ContextVar[str | None] = contextvars.ContextVar("_current_user_cookie", default=None)
    fake_admin._current_user_cookie = cv
    monkeypatch.setitem(sys.modules, "admin", fake_admin)

    # ── user_platform.session ──
    fake_session_mod = types.ModuleType("user_platform.session")

    class _Store:
        async def get(self, name: str, cookie: str):
            captured["session_lookup"] = (name, cookie)
            return session_data

    fake_session_mod.session_store = _Store()
    fake_session_mod.USER_SESSION_COOKIE = "ai_lubricant_session"
    monkeypatch.setitem(sys.modules, "user_platform.session", fake_session_mod)

    # ── user_platform.models.User ──
    # 注意：不能替换 `user_platform.models` 模块对象——`from user_platform.models
    # import User` 走 sys.modules 缓存，换包属性不生效。直接 patch 真实类的
    # get_or_none（monkeypatch 会自动还原）。
    from user_platform.models import User as _RealUser

    async def fake_get_or_none(id: str):  # noqa: A002 — 对齐真实调用签名
        captured["user_lookup"] = id
        return user_row

    monkeypatch.setattr(_RealUser, "get_or_none", staticmethod(fake_get_or_none), raising=False)

    # ── mcp_plugin_store.get_cdp_client_owner_user_id ──
    import mcp_plugin_store

    async def fake_owner(client_id: str):
        captured["cdp_lookup"] = client_id
        return cdp_owner

    monkeypatch.setattr(mcp_plugin_store, "get_cdp_client_owner_user_id", fake_owner, raising=False)

    # ── AGENT_INTERNAL_TOKEN ──
    if internal_token is not None:
        monkeypatch.setenv("AGENT_INTERNAL_TOKEN", internal_token)
    else:
        monkeypatch.delenv("AGENT_INTERNAL_TOKEN", raising=False)

    # ── agent.api._is_valid_admin ──
    from agent import api as agent_api

    async def fake_admin_valid(_authorization: str | None) -> bool:
        return admin_token_valid

    monkeypatch.setattr(agent_api, "_is_valid_admin", fake_admin_valid, raising=False)

    return captured


# ── 用例 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cookie_有效_用户active_带_role(monkeypatch):
    from agent.realtime import resolve_principal

    user = types.SimpleNamespace(role="individual", is_deleted=False, is_blocked=False, status="active")
    session = types.SimpleNamespace(uid="u-42", cookie="ck", payload={})
    _install_stubs(monkeypatch, session_data=session, user_row=user)

    ws = _FakeWS(headers={"cookie": "ai_lubricant_session=ck; other=1"})
    got = await resolve_principal(ws)
    assert got == {"kind": "user", "id": "u-42", "role": "individual"}


@pytest.mark.asyncio
async def test_cookie_有效_但用户已删_fail_closed(monkeypatch):
    from agent.realtime import resolve_principal

    user = types.SimpleNamespace(role="individual", is_deleted=True, is_blocked=False, status="active")
    session = types.SimpleNamespace(uid="u-42", cookie="ck", payload={})
    _install_stubs(monkeypatch, session_data=session, user_row=user)

    ws = _FakeWS(headers={"cookie": "ai_lubricant_session=ck"})
    assert await resolve_principal(ws) is None


@pytest.mark.asyncio
async def test_cookie_有效_但用户被封_fail_closed(monkeypatch):
    from agent.realtime import resolve_principal

    user = types.SimpleNamespace(role="individual", is_deleted=False, is_blocked=True, status="active")
    session = types.SimpleNamespace(uid="u-42", cookie="ck", payload={})
    _install_stubs(monkeypatch, session_data=session, user_row=user)

    ws = _FakeWS(headers={"cookie": "ai_lubricant_session=ck"})
    assert await resolve_principal(ws) is None


@pytest.mark.asyncio
async def test_cookie_有效_但用户停用_fail_closed(monkeypatch):
    from agent.realtime import resolve_principal

    user = types.SimpleNamespace(role="individual", is_deleted=False, is_blocked=False, status="banned")
    session = types.SimpleNamespace(uid="u-42", cookie="ck", payload={})
    _install_stubs(monkeypatch, session_data=session, user_row=user)

    ws = _FakeWS(headers={"cookie": "ai_lubricant_session=ck"})
    assert await resolve_principal(ws) is None


@pytest.mark.asyncio
async def test_admin用户_role透传_任务频道走特权(monkeypatch):
    from agent.realtime import authorize, resolve_principal

    user = types.SimpleNamespace(role="admin", is_deleted=False, is_blocked=False, status="active")
    session = types.SimpleNamespace(uid="u-admin", cookie="ck", payload={})
    _install_stubs(monkeypatch, session_data=session, user_row=user)

    ws = _FakeWS(headers={"cookie": "ai_lubricant_session=ck"})
    principal = await resolve_principal(ws)
    assert principal == {"kind": "user", "id": "u-admin", "role": "admin"}

    # admin 的任务鉴权不该看 user_id 比对（get_task 特权分支）；这里用假 get_task
    # 断言 role 被一路传进去。
    from user_platform import task_service as _ts

    seen: dict = {}

    async def fake_get_task(user_id, task_id, *, role=None):
        seen.update(user_id=user_id, role=role)
        return {"id": task_id}

    orig = _ts.task_service.get_task
    _ts.task_service.get_task = fake_get_task  # type: ignore[method-assign]
    try:
        ok, _, _ = await authorize("task", "t1", principal)
    finally:
        _ts.task_service.get_task = orig  # type: ignore[method-assign]
    assert ok
    assert seen == {"user_id": "u-admin", "role": "admin"}


@pytest.mark.asyncio
async def test_cdp_client_id_加_内部token_解析_owner(monkeypatch):
    from agent.realtime import resolve_principal

    user = types.SimpleNamespace(role="individual", is_deleted=False, is_blocked=False, status="active")
    captured = _install_stubs(
        monkeypatch, session_data=None, user_row=user,
        cdp_owner="u-owner", internal_token="sekret",
    )

    ws = _FakeWS(headers={"x-cdp-client-id": "cli-1", "x-internal-token": "sekret"})
    got = await resolve_principal(ws)
    assert got == {"kind": "user", "id": "u-owner", "role": "individual", "client_id": "cli-1"}
    assert captured["cdp_lookup"] == "cli-1"


@pytest.mark.asyncio
async def test_cdp_token_错_直接_None_不落cookie分支(monkeypatch):
    """CDP 分支 token 错必须 return None：不能继续试 cookie（否则 client_id 参数
    会把别的身份通道当兜底）。"""
    from agent.realtime import resolve_principal

    user = types.SimpleNamespace(role="individual", is_deleted=False, is_blocked=False, status="active")
    session = types.SimpleNamespace(uid="u-42", cookie="ck", payload={})
    _install_stubs(
        monkeypatch, session_data=session, user_row=user,
        cdp_owner="u-owner", internal_token="right",
    )

    ws = _FakeWS(headers={"x-cdp-client-id": "cli-1", "x-internal-token": "wrong"})
    assert await resolve_principal(ws) is None


@pytest.mark.asyncio
async def test_内部token_无client_id_是_service身份(monkeypatch):
    from agent.realtime import resolve_principal

    _install_stubs(monkeypatch, session_data=None, user_row=None, internal_token="sekret")
    ws = _FakeWS(headers={"x-internal-token": "sekret"})
    got = await resolve_principal(ws)
    assert got == {"kind": "service", "id": None, "role": "admin"}


@pytest.mark.asyncio
async def test_内部token_未配置环境变量_拒绝(monkeypatch):
    from agent.realtime import resolve_principal

    _install_stubs(monkeypatch, session_data=None, user_row=None, internal_token=None)
    ws = _FakeWS(headers={"x-internal-token": "sekret"})
    assert await resolve_principal(ws) is None


@pytest.mark.asyncio
async def test_管理员Bearer_全量身份(monkeypatch):
    from agent.realtime import resolve_principal

    _install_stubs(monkeypatch, session_data=None, user_row=None, admin_token_valid=True)
    ws = _FakeWS(headers={"authorization": "Bearer whatever"})
    got = await resolve_principal(ws)
    assert got == {"kind": "user", "id": None, "role": "admin"}


@pytest.mark.asyncio
async def test_管理员Bearer_无效_回落None(monkeypatch):
    from agent.realtime import resolve_principal

    _install_stubs(monkeypatch, session_data=None, user_row=None, admin_token_valid=False)
    ws = _FakeWS(headers={"authorization": "Bearer whatever"})
    assert await resolve_principal(ws) is None


@pytest.mark.asyncio
async def test_什么都没有_None(monkeypatch):
    from agent.realtime import resolve_principal

    _install_stubs(monkeypatch, session_data=None, user_row=None)
    assert await resolve_principal(_FakeWS()) is None


# ── 转发闭环：conversation_hub → realtime（§10-2 agent 频道接入）──────────
#
# 这条是「多端一致」的接缝：网页/CDP 面板订阅 SSE 读 conversation_hub，网页
# 新客户端订阅 WS 读 realtime；两边必须看到**同一条序列**。契约是
# conversation_hub.publish 一处转发，15 个生产点零改动。


@pytest.mark.asyncio
async def test_conversation_hub_publish_转发到_realtime_subscriber():
    import asyncio

    from agent import conversation_hub, realtime

    realtime.reset()
    conversation_hub.reset()
    try:
        ws_queue = realtime.subscribe("agent", "conv-1")
        sse_queue = conversation_hub.subscribe("conv-1")

        event = {"type": "content", "text": "hi", "message_id": 7, "offset": 0}
        await conversation_hub.publish("conv-1", event)

        # WS 侧：信封带 channel/target/seq
        frame = await asyncio.wait_for(ws_queue.get(), 1)
        assert frame["type"] == "event"
        assert frame["channel"] == "agent" and frame["target"] == "conv-1"
        assert frame["seq"] == 1
        assert frame["event"] == event

        # SSE 侧：仍是裸事件（老消费者形状不变）
        assert await asyncio.wait_for(sse_queue.get(), 1) == event
    finally:
        realtime.reset()
        conversation_hub.reset()


@pytest.mark.asyncio
async def test_conversation_hub_seq_在_realtime_侧单调递增():
    import asyncio

    from agent import conversation_hub, realtime

    realtime.reset()
    conversation_hub.reset()
    try:
        q = realtime.subscribe("agent", "conv-2")
        for i in range(3):
            await conversation_hub.publish("conv-2", {"type": "content", "text": str(i)})
        seqs = [(await asyncio.wait_for(q.get(), 1))["seq"] for _ in range(3)]
        assert seqs == [1, 2, 3]
    finally:
        realtime.reset()
        conversation_hub.reset()


@pytest.mark.asyncio
async def test_无人订阅_realtime_时转发不报错():
    """SSE 有人看、WS 没人连 —— 转发仍要静默成功（转发是 publish 的第一句，
    不能因为没人听就抛，否则会打断 agent 主循环）。"""
    from agent import conversation_hub, realtime

    realtime.reset()
    conversation_hub.reset()
    try:
        await conversation_hub.publish("nobody", {"type": "content"})
        assert realtime.current_seq("agent", "nobody") == 1
    finally:
        realtime.reset()
        conversation_hub.reset()
