"""``POST /resources/{resource_id}/device-command`` 的行为契约测试。

这是网页「查看设备」远程操作面板的唯一服务端入口，钉住的都是会真出问题的分支：

- 路由注册顺序：静态尾段 ``device-command`` 必须能命中自己的 handler，而不是被
  ``/resources/{resource_id}`` 通配吞掉（历史上 422 事故同一形态）。
- 归属 + 在线闸门：不是本人的设备 404；设备没在 driver 里（离线）409，绝不把命令
  发到一条不存在的连接上干等超时。
- 命令白名单：不在 spec §8 词汇表里的 cmd 直接 400，不冒充「设备不支持」。
- 设备答复 ok=false 要转成 502 并带上可读引导语（前端 toast 直接用 detail）。

driver / store 都打替身：命令的真实往返由一个自动应答的假 socket 提供，
不落库、不连手机。
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp_builtin.device_control import protocol as proto
from user_platform import routes_builtin_tools
from user_platform.deps import get_current_user
from user_platform.models import User

DEVICE_ROW = {
    "id": 42,
    "resource_type": "device",
    "device_id": "dev_pixel7",
    "platform": "android",
    "enabled": True,
}

PATH = "/api/v1/users/builtin-tools/resources/42/device-command"


class AutoReplySocket:
    """把 call 帧记下来，并按预置结果当场 deliver —— 模拟一台会答话的设备。"""

    def __init__(self, *, ok=True, data=None, error=None) -> None:
        self.sent: list[dict] = []
        self.reply_ok = ok
        self.reply_data = data
        self.reply_error = error
        self.ctx = None

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)
        if frame.get("type") != proto.TYPE_CALL or self.ctx is None:
            return
        request_id = frame["request_id"]
        # 同一次请求的 loop 里投递结果（call_soon 让 _dispatch 先 await 上 future）。
        asyncio.get_running_loop().call_soon(
            self.ctx.deliver, request_id, self.reply_ok, self.reply_data, self.reply_error
        )

    async def close(self, code: int, reason: str) -> None:  # pragma: no cover
        pass


class FakeDriver:
    """只实现路由用到的 get()；None = 这台设备当前没有活连接。"""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    def get(self, device_id: str):
        return self._ctx


def _make_ctx(socket: AutoReplySocket, capabilities: list[str] | None = None):
    from mcp_builtin.device_control.driver import DeviceContext

    ctx = DeviceContext(
        device_id=DEVICE_ROW["device_id"],
        session_id="ses_test",
        token_hash="hash_test",
        capabilities=capabilities if capabilities is not None else list(proto.COMMANDS),
        device_info={"platform": "android"},
        send=socket.send,
        close=socket.close,
        resource_id=DEVICE_ROW["id"],
    )
    socket.ctx = ctx
    return ctx


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(routes_builtin_tools.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        id=uuid.uuid4(), email="t@example.com", password_hash="x", role="user"
    )
    return TestClient(app)


def _owned(get_resource_impl):
    """把路由内部的 ``store.get_resource`` 换成替身（路由里是函数内 import）。"""
    return patch("builtin_tool_store.get_resource", new=AsyncMock(side_effect=get_resource_impl))


def test_device_command_route_is_not_shadowed_by_wildcard():
    """尾段静态路由必须存在且顺序正确（命中它才可能是 401/409，而不是 422）。"""
    paths = [getattr(r, "path", "") for r in routes_builtin_tools.router.routes]
    cmd_path = "/api/v1/users/builtin-tools/resources/{resource_id}/device-command"
    wildcard = "/api/v1/users/builtin-tools/resources/{resource_id}"
    assert cmd_path in paths
    assert paths.index(cmd_path) < paths.index(wildcard)


def test_screenshot_command_passes_device_data_through(client):
    """正常往返：driver 的命令结果原样出现在响应的 data 里。"""
    shot = {"format": "jpeg", "w": 315, "h": 700, "b64": "AAAA"}
    socket = AutoReplySocket(data={"screen": {"w": 1080, "h": 2400}, "screenshot": shot})
    ctx = _make_ctx(socket)

    with _owned(lambda *a, **k: DEVICE_ROW), patch(
        "mcp_builtin.device_control.driver.get_driver", return_value=FakeDriver(ctx)
    ):
        resp = client.post(PATH, json={"cmd": "get_screen_state", "args": {"include_screenshot": True}})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cmd"] == "get_screen_state"
    assert body["data"]["screenshot"] == shot
    # 下发到设备的帧必须是协议帧形态（cmd + args 透传）。
    call = next(f for f in socket.sent if f["type"] == proto.TYPE_CALL)
    assert call["cmd"] == "get_screen_state"
    assert call["args"] == {"include_screenshot": True}


def test_offline_device_is_refused(client):
    """设备不在线（driver 里没有活连接）→ 409，不发命令。"""
    with _owned(lambda *a, **k: DEVICE_ROW), patch(
        "mcp_builtin.device_control.driver.get_driver", return_value=FakeDriver(None)
    ):
        resp = client.post(PATH, json={"cmd": "tap", "args": {"x": 10, "y": 20}})

    assert resp.status_code == 409, resp.text
    assert "离线" in resp.json()["detail"]


def test_unknown_command_is_rejected_before_touching_device(client):
    """不在协议词汇表里的 cmd → 400（服务端自己就该拦住，别浪费一次往返）。"""
    socket = AutoReplySocket(data={})
    ctx = _make_ctx(socket)

    with _owned(lambda *a, **k: DEVICE_ROW), patch(
        "mcp_builtin.device_control.driver.get_driver", return_value=FakeDriver(ctx)
    ):
        resp = client.post(PATH, json={"cmd": "rm_rf", "args": {}})

    assert resp.status_code == 400, resp.text
    assert socket.sent == [], "白名单外的命令不得下发到设备"


def test_device_error_maps_to_502_with_hint(client):
    """设备回 ok=false → 502 + 该错误码的引导语（前端直接展示 detail）。"""
    socket = AutoReplySocket(
        ok=False, error={"code": proto.ERR_STALE_NODE, "message": "node not in latest tree"}
    )
    ctx = _make_ctx(socket)

    with _owned(lambda *a, **k: DEVICE_ROW), patch(
        "mcp_builtin.device_control.driver.get_driver", return_value=FakeDriver(ctx)
    ):
        resp = client.post(PATH, json={"cmd": "tap", "args": {"node_id": "node_gone"}})

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert "stale_node" in detail
    assert "刷新屏幕" in detail, "应带可读引导语而不是裸错误码"


def test_foreign_device_is_404(client):
    """归属校验：store 按 owner 过滤后查不到 → 404（不泄露存在性）。"""
    with _owned(lambda *a, **k: None), patch(
        "mcp_builtin.device_control.driver.get_driver", return_value=FakeDriver(None)
    ):
        resp = client.post(PATH, json={"cmd": "tap", "args": {"x": 1, "y": 1}})

    assert resp.status_code == 404, resp.text
