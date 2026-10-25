"""User-owned mobile push devices and per-device event routing.

This surface is deliberately separate from the generic webhook channel API:
mobile push tokens are secrets and a user must never be able to submit an
arbitrary channel target or inspect another user's token/device.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .deps import audit_user_action, get_current_user
from .models import User
from .notify_service import notify_service

router = APIRouter(prefix="/api/v1/users/notify/devices", tags=["user-platform-notify-devices"])


def _ok(data: Any = None, message: str = "") -> dict:
    return {"code": 0, "message": message, "data": data}


class RegisterDeviceReq(BaseModel):
    device_key: str
    push_token: str
    name: str | None = None
    default_name: str | None = None
    platform: str | None = None
    push_provider: str | None = "expo"
    app_version: str | None = None


class DevicePatchReq(BaseModel):
    name: str | None = None
    enabled: bool | None = None


class HeartbeatReq(BaseModel):
    push_token: str | None = None
    app_version: str | None = None


class ReplaceRulesReq(BaseModel):
    # {"task.ended": true} — in-app notify-center binding (simple mode default).
    app: dict[str, bool] | None = None
    # {"task.ended": [device_uuid]} — mobile_push device targeting (remote push).
    bindings: dict[str, list[str]] | None = None


@router.get("")
async def list_devices(user: User = Depends(get_current_user)) -> dict:
    """获取当前用户名下的移动推送设备列表（令牌脱敏）。"""
    return _ok(await notify_service.list_devices(str(user.id)))


@router.post("/register")
async def register_device(
    body: RegisterDeviceReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """注册（或就地更新）一台移动推送设备。

    ``device_key`` 为安装标识，重复注册只更新令牌等字段并重新启用；令牌原属其他账号
    时改绑到当前账号并禁用旧设备。``device_key``/``push_token`` 缺失返回 400。
    """
    try:
        result = await notify_service.register_device(
            str(user.id), body.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit_user_action(
        request, user, "notify_device.register",
        request_body={"device_key": body.device_key, "platform": body.platform},
        response={"device_id": result["device"]["id"]},
    )
    return _ok(result, "手机已注册")


@router.post("/{device_id}/heartbeat")
async def heartbeat_device(
    device_id: str, body: HeartbeatReq, user: User = Depends(get_current_user)
) -> dict:
    """设备心跳：上报应用版本并刷新令牌。

    设备不存在返回 404。
    """
    result = await notify_service.heartbeat_device(
        str(user.id), device_id, body.model_dump(exclude_none=True)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return _ok(result)


@router.patch("/{device_id}")
async def update_device(
    device_id: str, body: DevicePatchReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """更新设备名称或启用状态。

    设备不存在返回 404；写审计日志。
    """
    result = await notify_service.update_device(
        str(user.id), device_id, body.model_dump(exclude_none=True)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    await audit_user_action(
        request, user, "notify_device.update",
        request_body={"device_id": device_id, **body.model_dump(exclude_none=True)},
        response={"ok": True},
    )
    return _ok(result, "已保存")


@router.delete("/{device_id}")
async def delete_device(
    device_id: str, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """删除一台移动推送设备（连同其推送令牌）。

    设备不存在返回 404；写审计日志。
    """
    ok = await notify_service.delete_device(str(user.id), device_id)
    if not ok:
        raise HTTPException(status_code=404, detail="设备不存在")
    await audit_user_action(
        request, user, "notify_device.delete",
        request_body={"device_id": device_id}, response={"deleted": True},
    )
    return _ok({"deleted": True}, "已移除")


@router.post("/{device_id}/test")
async def test_device(
    device_id: str, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """Send one test push to a device — verifies token, credentials and the
    phone's permission state end-to-end."""
    from .notify_push import build_push_payload, send_expo_push

    devices = await notify_service.list_devices(str(user.id))
    device = next((d for d in devices if d["id"] == device_id), None)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    # Fetch the raw token through the service layer (masked DTOs never carry it).
    token = await notify_service.get_device_token(str(user.id), device_id)
    if not token:
        return {"code": -1, "message": "设备没有可用的推送令牌", "data": None}
    payload = build_push_payload(
        token,
        title="测试通知",
        body=f"如果你看到这条消息，说明「{device['name'] or '这台手机'}」可以正常收到通知。",
        data={"event_type": "test", "route": "/(tabs)/tasks"},
        severity="info",
    )
    result = await send_expo_push(payload)
    await audit_user_action(
        request, user, "notify_device.test",
        request_body={"device_id": device_id}, response={"ok": result.ok},
    )
    if not result.ok:
        return {"code": -1, "message": f"测试推送失败：{result.error}", "data": None}
    return _ok({"ok": True}, "测试推送已发送")


@router.get("/rules")
async def list_push_rules(user: User = Depends(get_current_user)) -> dict:
    """获取当前用户的通知规则（应用内开关 + 推送设备绑定）。"""
    return _ok(await notify_service.list_push_rules(str(user.id)))


@router.put("/rules")
async def replace_push_rules(
    body: ReplaceRulesReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """整体替换当前用户的通知规则。

    ``app`` 为事件类型的应用内开关，``bindings`` 为事件类型 → 推送设备集合；
    未提交的事件按空集处理。事件类型无效返回 400；写审计日志。
    """
    try:
        result = await notify_service.replace_push_rules(
            str(user.id), body.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit_user_action(
        request, user, "notify_device.rules_update",
        request_body={
            "app_event_types": list((body.app or {}).keys()),
            "push_event_types": list((body.bindings or {}).keys()),
        }, response={"ok": True},
    )
    return _ok(result, "通知规则已保存")
