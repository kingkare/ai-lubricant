"""conversation_hub：进程内会话事件扇出。

覆盖：多订阅者同序列、慢消费者丢弃不阻塞生产者、退订清理、进程重启即空。
"""
from __future__ import annotations

import asyncio

import pytest

from agent import conversation_hub


@pytest.fixture(autouse=True)
def _clean():
    conversation_hub.reset()
    yield
    conversation_hub.reset()


@pytest.mark.asyncio
async def test_publish_fans_out_to_all_subscribers():
    a = conversation_hub.subscribe("c1")
    b = conversation_hub.subscribe("c1")
    other = conversation_hub.subscribe("c2")

    await conversation_hub.publish("c1", {"type": "content", "text": "hi"})

    assert await a.get() == {"type": "content", "text": "hi"}
    assert await b.get() == {"type": "content", "text": "hi"}
    assert other.empty()
    assert conversation_hub.watcher_count("c1") == 2
    assert conversation_hub.is_watching("c1")


@pytest.mark.asyncio
async def test_unsubscribe_removes_watcher():
    a = conversation_hub.subscribe("c1")
    conversation_hub.unsubscribe("c1", a)
    assert not conversation_hub.is_watching("c1")
    # 退订后 publish 不报错、不投递
    await conversation_hub.publish("c1", {"type": "content"})
    assert a.empty()


@pytest.mark.asyncio
async def test_slow_subscriber_overflows_without_blocking(monkeypatch):
    """慢消费者满队即丢最旧 delta，但订阅者保留并补发 reset：publish 永不阻塞，
    agent 主循环不受假死客户端拖累；丢帧后客户端能收到 reset 信号去重拉补全。

    工作区分支保留订阅者（不摘除）：只丢最旧 delta 腾位，并在下一帧前补一帧
    reset（节流窗口可调）。快订阅者一个不丢；慢订阅者队列满后保留、reset 在前。
    """
    # reset 节流窗口归零：测试里溢出后下一帧就补 reset，不等 3s。
    monkeypatch.setattr(conversation_hub, "_RESET_MIN_INTERVAL_S", 0.0)
    slow = conversation_hub.subscribe("c1")
    fast = conversation_hub.subscribe("c1")
    total = conversation_hub._QUEUE_SIZE + 5
    got = []
    for i in range(total):
        await conversation_hub.publish("c1", {"type": "content", "text": str(i)})
        got.append((await fast.get())["text"])

    assert got == [str(i) for i in range(total)], "持续消费的订阅者不该丢任何事件"
    # 慢订阅者保留（不摘除）：溢出只丢最旧 delta，不踢连接。
    assert conversation_hub.watcher_count("c1") == 2, "满队只丢 delta，订阅者保留"

    # 溢出已登记 dirty；下一帧 publish 触发补发 reset，挤进慢队列（reset 在前）。
    await conversation_hub.publish("c1", {"type": "content", "text": "tail"})
    drained = []
    while not slow.empty():
        drained.append(slow.get_nowait())
    types = [e.get("type") for e in drained]
    assert "reset" in types, "丢帧后必须补发 reset 信号，客户端据此重拉补全"
    assert types[0] == "reset", "reset 必须排在队首，优先于后续 delta/终态"
    assert drained[-1].get("text") == "tail", "最新事件仍要送达（丢最旧、保最新）"
    # 慢订阅者保留但队列被 reset+ 新事件覆盖，不再是满的 256 帧。
    assert len(drained) >= 1, "慢订阅者至少收到一帧 reset"


@pytest.mark.asyncio
async def test_publish_to_empty_conversation_is_noop():
    await conversation_hub.publish("nobody", {"type": "content"})


@pytest.mark.asyncio
async def test_events_response_snapshot_first_then_live_until_end(monkeypatch):
    """GET /agent/conversations/{id}/events 的 SSE 生成器契约：首帧快照、随后增量、
    end 收口后流关闭。断言快照内容来自 conversation_store.get_messages，事件
    序列与 hub 发布顺序一致，end 之后生成器 StopAsyncIteration。"""
    import json as _json
    from agent import api as agent_api

    conversation_hub.reset()

    async def fake_get_messages(conv_id, *, timeline=False):
        # 显示端点走 timeline=True（事件帧折叠）；LLM/种子路径走默认 False。
        assert timeline is True, "SSE 首帧快照属于显示路径，必须折叠时间线"
        return [
            {"id": 1, "role": "user", "content": "hi"},
            {"id": 2, "role": "assistant", "content": "", "status": "streaming"},
        ]

    monkeypatch.setattr(agent_api.conversation_store, "get_messages", fake_get_messages)

    resp = agent_api._conversation_events_response("c1")
    agen = resp.body_iterator

    first = await agen.__anext__()
    snap = _json.loads(first[len("data: "):])
    assert snap["type"] == "snapshot"
    assert [m["id"] for m in snap["messages"]] == [1, 2]

    await conversation_hub.publish("c1", {"type": "content", "text": "x", "message_id": 2, "offset": 0})
    await conversation_hub.publish("c1", {"type": "end"})

    content_frame = await asyncio.wait_for(agen.__anext__(), 2)
    ev = _json.loads(content_frame[len("data: "):])
    assert ev["type"] == "content" and ev["text"] == "x"

    end_frame = await asyncio.wait_for(agen.__anext__(), 2)
    end_ev = _json.loads(end_frame[len("data: "):])
    assert end_ev["type"] == "end"

    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()

    conversation_hub.reset()
