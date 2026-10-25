"""Public server/runtime metadata required by the upstream user portal.

The vendored frontend requests this endpoint before authentication to determine
SaaS/private edition behavior. It uses the original generated Go client, so the
response intentionally keeps upstream's ``{code, message, data}`` envelope
rather than the plain REST responses used by the endpoint-map adapter.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/server", tags=["user-platform-server"])


def _version() -> str:
    """当前服务端版本：读仓库根 ``VERSION`` 文件（publish 打升级包时写入 tag）。

    与 ``server_release_catalog.current_version()`` 同源同口径——两处都走这个
    文件，避免「版本端点说一个值、升级卡片说另一个值」。无该文件（开发机）
    回落 ``dev``。
    """
    from project_paths import version_file

    try:
        text = version_file().read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception:  # noqa: BLE001 - 缺失/不可读回落 dev，不影响启动
        pass
    return "dev"


@router.get("/config")
async def server_config() -> dict:
    """Return deployment metadata consumed during frontend bootstrap."""
    version = _version()
    return {
        "code": 0,
        "message": "",
        "data": {
            "current_version": version,
            "latest_version": version,
            "edition": "private",
            "region": (os.getenv("AI_LUBRICANT_REGION") or os.getenv("MONKEYCODE_REGION") or "cn").strip() or "cn",
        },
    }


@router.get("/version")
async def server_version() -> dict:
    """服务端自身版本（升级探测接口）。

    所有升级程序（含其他部署实例的探测）都读这个接口判断对端版本，而不是各自
    去猜环境变量或文件路径。版本来自镜像/包内的 ``VERSION`` 文件——产物自带，
    不会因为忘传环境变量而撒谎。

    返回裸 JSON（不带上游 ``{code,message,data}`` 信封）：这是本系统自己的升级
    链路内部契约，与上游前端兼容层无关。
    """
    version = _version()
    return {"version": version, "service": "ai-lubricant"}


@router.get("/client-ip")
async def server_client_ip(request: Request) -> dict:
    """Echo the caller's public IP.

    Replaces the vendored frontend's old call to the upstream
    ``monkeycode-ai.online/get-my-ip`` service: opening a VM port forward needs
    the browser's own IP for the whitelist, and the backend sees it on every
    request anyway (X-Forwarded-For first hop, else the direct peer).
    """
    from .deps import client_meta

    ip, _user_agent = client_meta(request)
    return {"code": 0, "message": "", "data": {"ip": ip or ""}}
