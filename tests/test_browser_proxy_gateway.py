"""代理通道端点契约：鉴权、帧路由、与控制通道并存（docs/browser-proxy-design.md §5）。

覆盖：
- _authenticate_proxy_frame 复用 auth protocol 2 但绑代理槽，不碰控制槽；
- _route_proxy_frame 只认 ping/proxy_*，proxy_status 落 _PROXY_STATUS；
- 只读 MCP 工具 browser_proxy_status 的授权域约束（多台未指定→拒绝，绝不 bound[0]）。
"""
import asyncio
import hashlib
import json

import pytest

import mcp_runtime.sse_gateway as sse_gateway
from mcp_builtin.cdp_bridge.TMWebDriver import TMWebDriver


class FakeClient:
    def __init__(self):
        self._user_id = self._client_id = self._token = None
        self.closed = []
        self.sent = []

    def send_message(self, payload): self.sent.append(json.loads(payload))
    def post_message(self, payload): self.sent.append(json.loads(payload))
    def close_auth(self, code, reason): self.closed.append((code, reason))


class FakeWebSocket:
    def __init__(self): self.frames = []
    async def send_text(self, raw): self.frames.append(json.loads(raw))


def client_row(token="secret", *, row_id=7, user_id=3, enabled=True):
    return {"id": row_id, "instance_key": str(row_id), "name": "Chrome", "user_ids": [user_id],
            "enabled": enabled, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "token_hint": "sec...ret"}


def test_authenticate_proxy_frame_binds_proxy_slot_only():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    ws, adapter = FakeWebSocket(), FakeClient()
    ok = asyncio.run(
        sse_gateway._authenticate_proxy_frame(
            json.dumps({"type": "auth", "protocol": 2, "token": "secret", "client_id": "7"}), ws, driver, adapter)
    )
    assert ok
    client = driver.get_context("7").clients["7"]
    assert client.proxy_ws_client is adapter
    assert client.ws_client is None  # 控制槽未被占用
    assert ws.frames[-1]["type"] == "auth_ok"


def test_authenticate_proxy_frame_rejects_bad_token():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    ws, adapter = FakeWebSocket(), FakeClient()
    ok = asyncio.run(
        sse_gateway._authenticate_proxy_frame(
            json.dumps({"type": "auth", "protocol": 2, "token": "wrong", "client_id": "7"}), ws, driver, adapter)
    )
    assert not ok
    assert ws.frames[-1]["type"] == "auth_error"


def test_route_proxy_frame_pong_and_status():
    sse_gateway._PROXY_STATUS.clear()
    adapter = FakeClient()
    adapter._client_id = "7"
    ws = FakeWebSocket()
    

    asyncio.run(sse_gateway._route_proxy_frame(json.dumps({"type": "ping"}), ws, adapter))
    assert ws.frames[-1]["type"] == "pong"

    status = {"applied": True, "activeProfileId": "pf1", "activeName": "Clash", "activeType": "fixed",
              "appliedBy": "admin", "leaseExpiresAt": 123, "levelOfControl": "controlled_by_this_extension"}
    asyncio.run(sse_gateway._route_proxy_frame(json.dumps({"type": "proxy_status", "status": status}), ws, adapter))
    got = sse_gateway.get_proxy_status("7")
    assert got and got["applied"] is True and got["active_name"] == "Clash"


def test_route_proxy_frame_ignores_business_frames():
    """代理通道不处理 chat_/pwd_/cdp 等业务帧（隔离的关键）。"""
    sse_gateway._PROXY_STATUS.clear()
    adapter = FakeClient()
    adapter._client_id = "7"
    ws = FakeWebSocket()
    asyncio.run(
        sse_gateway._route_proxy_frame(json.dumps({"type": "chat_send", "reqId": "x"}), ws, adapter)
    )
    assert ws.frames == []  # 无回帧
    assert sse_gateway.get_proxy_status("7") is None


def test_push_proxy_activate_includes_profile_definition():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    proxy = FakeClient()
    driver.bind_proxy(proxy, driver.authenticate_client("secret", "7"))
    import mcp_builtin.cdp_bridge.server as cdp_server
    orig = cdp_server.driver
    cdp_server.driver = driver
    try:
        prof = {"id": "pf1", "name": "Clash", "type": "fixed", "config": {"host": "127.0.0.1", "port": 7890}}
        ok = asyncio.run(
            sse_gateway.push_proxy_activate("7", profile=prof, lease_ms=60000)
        )
        assert ok
        frame = proxy.sent[-1]
        assert frame["type"] == "proxy_ctl" and frame["action"] == "activate"
        assert frame["profiles"][0]["id"] == "pf1"
        assert frame["leaseMs"] == 60000
    finally:
        cdp_server.driver = orig


def test_push_proxy_activate_returns_false_when_no_proxy_connection():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    import mcp_builtin.cdp_bridge.server as cdp_server
    orig = cdp_server.driver
    cdp_server.driver = driver
    try:
        ok = asyncio.run(
            sse_gateway.push_proxy_activate("7", profile={"id": "pf1"}, lease_ms=0)
        )
        assert ok is False
    finally:
        cdp_server.driver = orig