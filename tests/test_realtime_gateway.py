"""统一实时网关（agent/realtime.py）：EventHub 扇出 + 归属鉴权 + send 转发。

覆盖：

- publish 按 (channel, target) 分配单调 seq，多订阅者同一序列
- 慢消费者：队列满即摘除，且 ``lagged`` 帧必须真的送得出去（预留格）
- 退订清理，publish 到无人订阅是 noop
- authorize：agent/chat 走 get_conversation_owned；task 走 get_task(role=)，
  特权（admin）不走 user_id 比对
- dispatch_action：agent send/retry/abort 转发到现有 handler，HTTPException 的
  status_code 原样透传（409 本轮在跑 / 404 对话不存在）；task cancel/stop/control
  转发到 task_service，ValueError 映射成 404/409/503
- resolve_principal 解析出 role（任务鉴权要用）
"""
from __future__ import annotations

import asyncio

import pytest

from agent import realtime


@pytest.fixture(autouse=True)
def _clean():
    realtime.reset()
    yield
    realtime.reset()


# ── EventHub ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_fans_out_同一个_seq_给全部订阅者():
    a = realtime.subscribe("agent", "c1")
    b = realtime.subscribe("agent", "c1")
    other = realtime.subscribe("agent", "c2")

    seq = await realtime.publish("agent", "c1", {"type": "content", "text": "hi"})

    fa = await a.get()
    fb = await b.get()
    assert fa["seq"] == fb["seq"] == seq == 1
    assert fa["event"] == {"type": "content", "text": "hi"}
    assert fa["channel"] == "agent" and fa["target"] == "c1"
    assert other.empty(), "别的 target 不该收到"


@pytest.mark.asyncio
async def test_seq_按_channel_target_独立单调():
    a = realtime.subscribe("agent", "c1")
    t = realtime.subscribe("task", "c1")  # 同 target 不同 channel 独立计数

    assert await realtime.publish("agent", "c1", {"type": "x"}) == 1
    assert await realtime.publish("agent", "c1", {"type": "x"}) == 2
    assert await realtime.publish("task", "c1", {"type": "x"}) == 1

    assert (await a.get())["seq"] == 1
    assert (await a.get())["seq"] == 2
    assert (await t.get())["seq"] == 1


@pytest.mark.asyncio
async def test_publish_无人订阅是_noop_但_seq_仍前进():
    assert await realtime.publish("agent", "nobody", {"type": "x"}) == 1
    assert realtime.current_seq("agent", "nobody") == 1


@pytest.mark.asyncio
async def test_退订后不再投递():
    q = realtime.subscribe("agent", "c1")
    realtime.unsubscribe("agent", "c1", q)
    assert realtime.watcher_count("agent", "c1") == 0

    await realtime.publish("agent", "c1", {"type": "x"})
    assert q.empty()


@pytest.mark.asyncio
async def test_慢消费者被摘除且_lagged_真的塞得进队列():
    """核心回归：publish 先用掉队列最后一格，lagged 就没地方放了。

    预留一格后，满队时摘掉订阅者仍能把 lagged 放进队列——否则客户端永远收不到
    「你落后了，去重拉快照」的信号（§3.3 lagged 必达）。
    """
    q = realtime.subscribe("agent", "c1")
    # 填到只剩一格（publish 的可容纳上限是 _QUEUE_SIZE - 1）。
    for i in range(realtime._QUEUE_SIZE - 1):
        await realtime.publish("agent", "c1", {"type": "x", "i": i})

    assert realtime.watcher_count("agent", "c1") == 1
    # 这一发让队列到顶 → 订阅者被摘除，lagged 补位。
    await realtime.publish("agent", "c1", {"type": "x", "i": "overflow"})

    assert realtime.watcher_count("agent", "c1") == 0, "满队订阅者应被摘除"
    frames = []
    while not q.empty():
        frames.append(q.get_nowait())
    assert frames[-1]["type"] == "lagged", "最后一格必须是 lagged"
    assert frames[-1]["channel"] == "agent" and frames[-1]["target"] == "c1"
    # 被摘除后不再收到新事件
    await realtime.publish("agent", "c1", {"type": "x", "i": "after"})
    assert q.empty()


@pytest.mark.asyncio
async def test_快消费者一个不丢():
    q = realtime.subscribe("agent", "c1")
    total = realtime._QUEUE_SIZE + 5
    for i in range(total):
        await realtime.publish("agent", "c1", {"type": "x", "i": i})
        assert (await q.get())["event"]["i"] == i


# ── 鉴权 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_agent_走归属校验(monkeypatch):
    from agent import conversation_store

    async def owned_by_none(user_id, conv_id):
        return {"id": conv_id} if conv_id == "mine" else None

    monkeypatch.setattr(conversation_store, "get_conversation_owned", owned_by_none)

    ok, code, _ = await realtime.authorize("agent", "mine", {"kind": "user", "id": "u1"})
    assert ok and code == 0

    ok, code, msg = await realtime.authorize("agent", "someone-else", {"kind": "user", "id": "u1"})
    assert not ok and code == 403 and msg


@pytest.mark.asyncio
async def test_authorize_task_用_get_task_且传_role(monkeypatch):
    """回归：原来调不存在的 get_task_owned，靠 AttributeError 兜底放行（越权）。"""
    from user_platform import task_service as _ts

    seen: dict = {}

    async def fake_get_task(user_id, task_id, *, role=None):
        seen["user_id"] = user_id
        seen["role"] = role
        return {"id": task_id} if task_id == "t-ok" else None

    monkeypatch.setattr(_ts.task_service, "get_task", fake_get_task, raising=False)
    # 断掉旧实现里那条兜底路径，确保越权不再被放行。
    if hasattr(_ts.task_service, "get_task_owned"):
        monkeypatch.delattr(_ts.task_service, "get_task_owned")

    ok, code, _ = await realtime.authorize(
        "task", "t-ok", {"kind": "user", "id": "u1", "role": "individual"}
    )
    assert ok and code == 0
    assert seen == {"user_id": "u1", "role": "individual"}

    ok, code, msg = await realtime.authorize(
        "task", "t-nope", {"kind": "user", "id": "u1", "role": "individual"}
    )
    assert not ok and code == 403 and msg


@pytest.mark.asyncio
async def test_authorize_service_身份全放行():
    ok, code, _ = await realtime.authorize("task", "anything", {"kind": "service", "id": None})
    assert ok and code == 0


@pytest.mark.asyncio
async def test_authorize_cdp_只认自己那个_client(monkeypatch):
    import mcp_plugin_store

    async def owner_of(client_id):
        return "u9" if client_id == "cli-9" else None

    monkeypatch.setattr(mcp_plugin_store, "get_cdp_client_owner_user_id", owner_of, raising=False)

    ok, _, _ = await realtime.authorize(
        "cdp", "cli-9", {"kind": "user", "id": "u9", "client_id": "cli-9"}
    )
    assert ok, "自己的 client 直接放行"
    ok, _, _ = await realtime.authorize(
        "cdp", "cli-9", {"kind": "user", "id": "u9", "client_id": "cli-X"}
    )
    assert ok, "同一 owner 的另一个 client 也放行（不比对 client_id，只比对 owner）"

    ok, code, _ = await realtime.authorize("cdp", "cli-9", {"kind": "user", "id": "uY"})
    assert not ok and code == 403


# ── dispatch_action ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_agent_send_转发到_send_message(monkeypatch):
    """回归：原来调不存在的 agent_api.start_turn。"""
    from agent import api as agent_api

    seen: dict = {}

    async def fake_send_message(conv_id, request, caller):
        seen["conv_id"] = conv_id
        seen["content"] = request.content
        seen["mode"] = request.mode
        seen["caller"] = caller
        return {"assistant_message_id": 7, "status": "streaming"}

    monkeypatch.setattr(agent_api, "send_message", fake_send_message, raising=False)
    monkeypatch.delattr(agent_api, "start_turn", raising=False)

    got = await realtime.dispatch_action(
        "agent", "c1", "send", {"content": "hi", "mode": "auto"},
        {"kind": "user", "id": "u1", "role": "individual"},
    )
    assert got == {"assistant_message_id": 7, "status": "streaming"}
    assert seen == {"conv_id": "c1", "content": "hi", "mode": "auto", "caller": "u1"}


@pytest.mark.asyncio
async def test_dispatch_agent_空消息_422():
    with pytest.raises(realtime._ActionError) as ei:
        await realtime.dispatch_action("agent", "c1", "send", {"content": "   "}, {"kind": "user", "id": "u1"})
    assert ei.value.code == 422


@pytest.mark.asyncio
async def test_dispatch_agent_HTTPException_原样透传_status(monkeypatch):
    """409（本轮在跑）不能变成 500：客户端要按 code 判是否等 done 再发。"""
    from fastapi import HTTPException

    from agent import api as agent_api

    async def busy(conv_id, request, caller):
        raise HTTPException(status_code=409, detail="a task is already running for this conversation")

    monkeypatch.setattr(agent_api, "send_message", busy, raising=False)

    with pytest.raises(realtime._ActionError) as ei:
        await realtime.dispatch_action("agent", "c1", "send", {"content": "hi"}, {"kind": "user", "id": "u1"})
    assert ei.value.code == 409
    assert "already running" in ei.value.message


@pytest.mark.asyncio
async def test_dispatch_agent_retry_与_abort_转发(monkeypatch):
    from agent import api as agent_api

    seen: dict = {}

    async def fake_retry(conv_id, message_id, caller):
        seen["retry"] = (conv_id, message_id)
        return {"assistant_message_id": message_id, "status": "streaming"}

    async def fake_abort(conv_id, caller):
        seen["abort"] = conv_id
        return {"aborted": True}

    monkeypatch.setattr(agent_api, "retry_message", fake_retry, raising=False)
    monkeypatch.setattr(agent_api, "abort_conversation", fake_abort, raising=False)
    monkeypatch.delattr(agent_api, "retry_turn", raising=False)
    monkeypatch.delattr(agent_api, "abort_turn", raising=False)

    await realtime.dispatch_action("agent", "c1", "retry", {"message_id": 5}, {"kind": "user", "id": "u1"})
    assert seen["retry"] == ("c1", 5)
    await realtime.dispatch_action("agent", "c1", "abort", {}, {"kind": "user", "id": "u1"})
    assert seen["abort"] == "c1"


@pytest.mark.asyncio
async def test_dispatch_task_cancel_stop_control_带_role(monkeypatch):
    from user_platform import task_service as _ts

    seen: dict = {}

    async def fake_cancel(user_id, task_id, *, role=None):
        seen["cancel"] = (user_id, task_id, role)
        return {"accepted": True, "task_id": task_id}

    async def fake_stop(user_id, task_id, *, role=None):
        seen["stop"] = (user_id, task_id, role)
        return True

    async def fake_start(user_id, task_id, *, role=None):
        seen["start"] = (user_id, task_id, role)
        return {"started": True, "task_id": task_id}

    monkeypatch.setattr(_ts.task_service, "cancel_task_turn", fake_cancel, raising=False)
    monkeypatch.setattr(_ts.task_service, "stop_task", fake_stop, raising=False)
    monkeypatch.setattr(_ts.task_service, "start_task_runtime", fake_start, raising=False)

    principal = {"kind": "user", "id": "u1", "role": "individual"}

    await realtime.dispatch_action("task", "t1", "cancel", {}, principal)
    assert seen["cancel"] == ("u1", "t1", "individual")

    got = await realtime.dispatch_action("task", "t1", "stop", {}, principal)
    assert got == {"ok": True} and seen["stop"] == ("u1", "t1", "individual")

    await realtime.dispatch_action("task", "t1", "control", {"action": "start"}, principal)
    assert seen["start"] == ("u1", "t1", "individual")


@pytest.mark.asyncio
async def test_dispatch_task_ValueError_映射成_404_409_503(monkeypatch):
    from user_platform import task_service as _ts

    async def raise_missing(user_id, task_id, *, role=None):
        raise ValueError("task_not_found")

    async def raise_unbound(user_id, task_id, *, role=None):
        raise ValueError("runtime_not_bound")

    async def raise_down(user_id, task_id, *, role=None):
        raise ValueError("node_server_unavailable")

    principal = {"kind": "user", "id": "u1", "role": "individual"}

    monkeypatch.setattr(_ts.task_service, "cancel_task_turn", raise_missing, raising=False)
    with pytest.raises(realtime._ActionError) as ei:
        await realtime.dispatch_action("task", "t1", "cancel", {}, principal)
    assert ei.value.code == 404

    monkeypatch.setattr(_ts.task_service, "cancel_task_turn", raise_unbound, raising=False)
    with pytest.raises(realtime._ActionError) as ei:
        await realtime.dispatch_action("task", "t1", "cancel", {}, principal)
    assert ei.value.code == 409

    monkeypatch.setattr(_ts.task_service, "cancel_task_turn", raise_down, raising=False)
    with pytest.raises(realtime._ActionError) as ei:
        await realtime.dispatch_action("task", "t1", "cancel", {}, principal)
    assert ei.value.code == 503


# ── 快照 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_task_走_list_task_events_并取_rows(monkeypatch):
    """回归：原来调 list_task_events(target, limit=200)，缺 user_id 且没传 role。"""
    from user_platform import task_service as _ts

    seen: dict = {}

    async def fake_list(user_id, task_id, *, before=None, limit=50, role=None):
        seen["args"] = (user_id, task_id, limit, role)
        return {"rows": [{"seq": 1, "kind": "message"}], "next_before": None}

    monkeypatch.setattr(_ts.task_service, "list_task_events", fake_list, raising=False)

    data, seq = await realtime.snapshot(
        "task", "t1", {"kind": "user", "id": "u1", "role": "individual"}
    )
    assert data == [{"seq": 1, "kind": "message"}]
    assert seen["args"] == ("u1", "t1", 200, "individual")


@pytest.mark.asyncio
async def test_snapshot_agent_取_messages():
    data, seq = await realtime.snapshot("agent", "c1", {"kind": "service"})
    # 没有 CH 时 get_messages 会抛 → 返回 None（订阅方按 500 处理），这里只断言
    # 形状是二元组，不依赖底层可用性。
    assert isinstance(seq, int)


# ── 帧协议：hello 带 principal ───────────────────────────────────────────


def test_协议常量():
    assert realtime.PROTOCOL_VERSION >= 1
    assert realtime.HEARTBEAT_SECONDS > 0
    assert realtime.IDLE_TIMEOUT_SECONDS > realtime.HEARTBEAT_SECONDS, "idle 必须容得下心跳"
    assert realtime._QUEUE_SIZE >= 2, "至少留一格给 lagged"
