"""统一实时网关 WS 协议端到端：真实 ASGI 握手跑固定消息流。

覆盖方案 §3 / §5 的连接层契约（test_realtime_gateway.py 覆盖的是函数层）：

- 连接即推 hello；未鉴权 close(4401)
- subscribe → snapshot（先登记后取快照，seq 就位）→ 后续 publish 扇 event
- send → ack；失败 → error（带原始 code，不静默）
- 每条 ref 必回一包；未知帧类型回 error
- ping → pong
- unsubscribe → ack，之后不再收事件
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import realtime

app = FastAPI()
app.include_router(realtime.router)

PRINCIPAL = {"kind": "user", "id": "u1", "role": "individual", "client_id": None}


@pytest.fixture(autouse=True)
def _clean():
    realtime.reset()
    yield
    realtime.reset()


@pytest.fixture()
def client(monkeypatch):
    async def fake_resolve(_ws):
        return dict(PRINCIPAL)

    monkeypatch.setattr(realtime, "resolve_principal", fake_resolve)
    # 默认放行 + 空快照 + noop 动作，各测试按需覆盖；不依赖 DB/CH。
    monkeypatch.setattr(realtime, "authorize", lambda c, t, p: _ok())
    monkeypatch.setattr(realtime, "snapshot", lambda c, t, p: _snap([]))
    with TestClient(app) as c:
        yield c


async def _ok():
    return True, 0, ""


async def _snap(data, seq=0):
    return data, seq


def _publish_in_portal(client, channel: str, target: str, event: dict) -> int:
    """在 TestClient 的事件循环里 publish——与 WS 处理器同一个 loop，否则队列不互通。"""
    return client.portal.call(realtime.publish, channel, target, event)


def _flood_in_portal(client, channel: str, target: str, count: int) -> int:
    """一个协程里连发 count 个事件，中间不交出控制权。

    ``publish`` 体内没有 await，所以整轮连发期间泵没机会排空队列——只有这样才能
    真灌满、触发溢出。逐条 portal.call 会每次让出 loop，泵边发边搬，队列永远填不满。
    """

    async def flood():
        last = 0
        for i in range(count):
            last = await realtime.publish(channel, target, {"type": "content", "text": str(i)})
        return last

    return client.portal.call(flood)


def test_连接即推_hello(client):
    with client.websocket_connect("/agent/realtime") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol"] == realtime.PROTOCOL_VERSION
        assert hello["heartbeat"] == realtime.HEARTBEAT_SECONDS
        assert hello["principal"] == {"kind": "user", "id": "u1"}


def test_未鉴权_close_4401(monkeypatch):
    async def deny(_ws):
        return None

    monkeypatch.setattr(realtime, "resolve_principal", deny)
    with pytest.raises(Exception) as ei:
        with TestClient(app).websocket_connect("/agent/realtime"):
            pass
    # code 在属性上，str(e) 是空的
    assert getattr(ei.value, "code", None) == 4401


def test_subscribe_收_snapshot_然后收_event(client):
    with client.websocket_connect("/agent/realtime") as ws:
        assert ws.receive_json()["type"] == "hello"

        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["ref"] == "s1"
        assert snap["channel"] == "agent" and snap["target"] == "c1"
        assert snap["data"] == [] and isinstance(snap["seq"], int)

        seq = _publish_in_portal(client, "agent", "c1", {"type": "content", "text": "hi"})
        ev = ws.receive_json()
        assert ev["type"] == "event"
        assert ev["seq"] == seq
        assert ev["event"] == {"type": "content", "text": "hi"}


def test_别的target的事件不进本订阅(client):
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        ws.receive_json()  # snapshot

        _publish_in_portal(client, "agent", "other", {"type": "content", "text": "x"})
        # 同 target 的下一发必须能收到，证明中间那发确实被过滤而不是连接死了
        _publish_in_portal(client, "agent", "c1", {"type": "content", "text": "mine"})
        ev = ws.receive_json()
        assert ev["type"] == "event" and ev["event"]["text"] == "mine"


def test_ping_回_pong(client):
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "ping", "ref": "p1"})
        pong = ws.receive_json()
        assert pong["type"] == "pong" and pong["ref"] == "p1"
        assert isinstance(pong["ts"], int)


def test_未知帧类型_回_error(client):
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "bogus", "ref": "b1"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["ref"] == "b1" and err["code"] == 422


def test_subscribe_缺字段_回_error(client):
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["ref"] == "s1" and err["code"] == 422


def test_无权限_subscribe_回_403_不建订阅(client, monkeypatch):
    async def deny(channel, target, principal):
        return False, 403, "无权访问该对话"

    monkeypatch.setattr(realtime, "authorize", deny)
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == 403
        assert realtime.watcher_count("agent", "c1") == 0, "鉴权失败不留订阅"


def test_snapshot_失败_回_error_且摘除订阅(client, monkeypatch):
    async def boom(channel, target, principal):
        return None, 3

    monkeypatch.setattr(realtime, "snapshot", boom)
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == 500
        assert realtime.watcher_count("agent", "c1") == 0, "快照失败要回滚订阅"


def test_send_成功_回_ack(client, monkeypatch):
    async def fake_dispatch(channel, target, action, payload, principal):
        return {"assistant_message_id": 7, "status": "streaming"}

    monkeypatch.setattr(realtime, "dispatch_action", fake_dispatch)
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "send", "ref": "r1", "channel": "agent", "target": "c1",
            "action": "send", "payload": {"content": "hi"},
        })
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ref"] == "r1"
        assert ack["data"] == {"assistant_message_id": 7, "status": "streaming"}


def test_send_业务错误_原样透传_code(client, monkeypatch):
    """409（本轮在跑）必须还是 409：客户端按 code 决定要不要等 done 再发。"""
    async def busy(channel, target, action, payload, principal):
        raise realtime._ActionError(409, "a task is already running for this conversation")

    monkeypatch.setattr(realtime, "dispatch_action", busy)
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "send", "ref": "r1", "channel": "agent", "target": "c1",
            "action": "send", "payload": {"content": "hi"},
        })
        err = ws.receive_json()
        assert err["type"] == "error" and err["ref"] == "r1" and err["code"] == 409


def test_send_未订阅的_target_也先过鉴权(client, monkeypatch):
    calls: list = []

    async def spy(channel, target, principal):
        calls.append((channel, target))
        return False, 403, "无权访问该对话"

    monkeypatch.setattr(realtime, "authorize", spy)
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "send", "ref": "r1", "channel": "agent", "target": "not-subscribed",
            "action": "send", "payload": {"content": "hi"},
        })
        assert ws.receive_json()["code"] == 403
    assert ("agent", "not-subscribed") in calls, "§4：send 未订阅的 target 也走 authorize"


def test_unsubscribe_之后不再收事件(client):
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        ws.receive_json()
        assert realtime.watcher_count("agent", "c1") == 1

        ws.send_json({"type": "unsubscribe", "ref": "u1", "channel": "agent", "target": "c1"})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["ref"] == "u1"
        assert realtime.watcher_count("agent", "c1") == 0

        # 再 publish 一发，连接不该收到事件；下一包必须是 pong 而不是 event
        _publish_in_portal(client, "agent", "c1", {"type": "content", "text": "x"})
        ws.send_json({"type": "ping", "ref": "p1"})
        assert ws.receive_json()["type"] == "pong"


def test_重复_subscribe_同一_target_回_resumed_ack(client):
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "subscribe", "ref": "s2", "channel": "agent", "target": "c1"})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["data"].get("resumed") is True
        assert realtime.watcher_count("agent", "c1") == 1, "重复订阅不再建队列"


def test_断线即连接级退订不留残余(client):
    import time

    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        ws.receive_json()
        assert realtime.watcher_count("agent", "c1") == 1
    # 出上下文 = 连接关闭。退订发生在服务端事件循环的 finally 里，与本线程的
    # 断言是两个方向跑的——直接断言会偶发抢跑，轮询等它收敛。
    deadline = time.monotonic() + 2.0
    while realtime.watcher_count("agent", "c1") != 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert realtime.watcher_count("agent", "c1") == 0, "断线必须连接级退订"


def test_慢消费者_lagged_帧真的到达客户端(client):
    """§3.3 lagged 必达：队列溢出后客户端必须收到 lagged，否则永远不会重拉快照。"""
    with client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "agent", "target": "c1"})
        ws.receive_json()

        # 连发灌满队列 → 触发溢出（publish 不 yield，泵没有插队机会）
        _flood_in_portal(client, "agent", "c1", realtime._QUEUE_SIZE)

        got_lagged = False
        for _ in range(4):
            frame = ws.receive_json()
            if frame.get("type") == "lagged" and frame.get("target") == "c1":
                got_lagged = True
                break
        assert got_lagged, "溢出必须收到 lagged"
        # 溢出后订阅者被摘掉，后续 publish 不再投递
        assert realtime.watcher_count("agent", "c1") == 0


# ── 频道门禁：未接入的频道必须明确 501，未知频道 422（不能误导成 500 读挂）──
# 用真实 snapshot（_WIRED_CHANNELS 门禁在里头），但放行 authorize——否则无库环境
# cdp 分支的 get_cdp_client_owner_user_id 会先回 403，测不到门禁本身。


@pytest.fixture()
def gated_client(monkeypatch):
    async def fake_resolve(_ws):
        return {"kind": "user", "id": "u1", "role": "individual", "client_id": "cli-1"}

    async def allow(_channel, _target, _principal):
        return True, 0, ""

    monkeypatch.setattr(realtime, "resolve_principal", fake_resolve)
    monkeypatch.setattr(realtime, "authorize", allow)
    with TestClient(app) as c:
        yield c


def test_未接入频道_subscribe_回_501_不建订阅(gated_client):
    """§10-3 未收编频道（cdp/device/terminal/voice）订阅要回明确的「尚未接入」，
    而不是落进 snapshot 的 None 分支被误报成「500 快照读取失败」。"""
    with gated_client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "cdp", "target": "cli-1"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["ref"] == "s1" and err["code"] == 501
        assert "尚未接入" in err["message"]
        assert realtime.watcher_count("cdp", "cli-1") == 0, "门禁失败不留订阅"


def test_未知频道_subscribe_回_422(gated_client):
    with gated_client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "ref": "s1", "channel": "bogus", "target": "x"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["code"] == 422 and "未知频道" in err["message"]


def test_未接入频道_send_回_501(gated_client):
    with gated_client.websocket_connect("/agent/realtime") as ws:
        ws.receive_json()
        ws.send_json({
            "type": "send", "ref": "r1", "channel": "device", "target": "dev-1",
            "action": "tap", "payload": {},
        })
        err = ws.receive_json()
        assert err["type"] == "error" and err["ref"] == "r1" and err["code"] == 501
