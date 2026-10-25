"""REST surface for the DDNS manager.

Two levels under ``/api/v1/users``, all gated to the logged-in user and
owner-scoped (``user_id``):

* **Provider pool** — ``/ddns-providers``: reusable credential profiles
  (one 阿里云 account / Cloudflare token). Created first.
* **Records** — ``/ddns-records``: DDNS tasks under a provider (domain +
  sub-domains + IP source). ``GET /ddns-providers/{id}/records`` is the
  expanded-table view; ``POST /ddns-records/{id}/sync`` runs one sync inline
  (the same code path the tunnel_server scheduler uses, just on demand).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .deps import audit_user_action, get_current_user
from .models import User
from . import ddns_service
from .ddns_service import DdnsServiceError

router = APIRouter(prefix="/api/v1/users", tags=["user-platform-ddns"])


def _to_http(exc: DdnsServiceError) -> HTTPException:
    code = {
        "not_found": 404,
        "invalid_argument": 400,
        "in_use": 409,
    }.get(exc.code, 400)
    return HTTPException(status_code=code, detail=exc.message)


# ── provider pool ─────────────────────────────────────────────────────────


class ProviderReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    provider: str
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    config: dict | None = None
    enabled: bool | None = None


@router.get("/ddns-providers")
async def list_providers(user: User = Depends(get_current_user)) -> list[dict]:
    """获取当前用户的 DDNS 供应商配置列表（凭据已脱敏）。"""
    return await ddns_service.list_providers(str(user.id))


@router.post("/ddns-providers")
async def create_provider(
    body: ProviderReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """创建 DDNS 供应商配置（可复用的凭据档案）。

    ``config`` 携带云厂商凭据（如阿里云 AK/SK、Cloudflare token），落库后不回显；
    审计日志只记名称/类型，不记凭据。
    """
    try:
        row = await ddns_service.create_provider(
            str(user.id),
            name=body.name, provider=body.provider, config=body.config, enabled=body.enabled,
        )
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    await audit_user_action(
        request, user, "ddns.provider.create",
        # Never audit-log raw credentials; the service response is already masked.
        request_body={"name": body.name, "provider": body.provider, "enabled": body.enabled},
        response={"id": row["id"]},
    )
    return row


@router.put("/ddns-providers/{provider_id}")
async def update_provider(
    provider_id: str, body: ProviderUpdate, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """更新 DDNS 供应商配置（名称/凭据/启用状态）。

    目标不存在返回 404；凭据不进审计日志。
    """
    try:
        row = await ddns_service.update_provider(
            str(user.id), provider_id,
            name=body.name, config=body.config, enabled=body.enabled,
        )
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    await audit_user_action(
        request, user, "ddns.provider.update",
        request_body={"name": body.name, "enabled": body.enabled},
        response={"id": row["id"]},
    )
    return row


@router.get("/ddns-providers/{provider_id}/records")
async def list_provider_records(
    provider_id: str, user: User = Depends(get_current_user)
) -> list[dict]:
    """Every DDNS record under one provider (expanded-table child rows)."""
    return await ddns_service.list_records(str(user.id), provider_id=provider_id)


@router.delete("/ddns-providers/{provider_id}")
async def delete_provider(
    provider_id: str, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """删除 DDNS 供应商配置。

    目标不存在返回 404；其下仍有 DDNS 记录时返回 409（须先删记录）。
    """
    try:
        result = await ddns_service.delete_provider(str(user.id), provider_id)
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    await audit_user_action(
        request, user, "ddns.provider.delete",
        request_body={"provider_id": provider_id}, response={"deleted": True},
    )
    return result


# ── records ───────────────────────────────────────────────────────────────


class RecordReq(BaseModel):
    provider_id: str
    name: str = Field(..., min_length=1, max_length=128)
    ip_source: str = "server"
    node_id: str | None = None
    # 选填探测代理：手填 URL（http/https/socks5），不走代理池；仅 server/node_proxy 生效。
    proxy: str | None = None
    config: dict = Field(default_factory=dict)
    interval_seconds: int = Field(default=120, ge=30, le=86400)
    enabled: bool = True


class RecordUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    ip_source: str | None = None
    node_id: str | None = None
    proxy: str | None = None
    config: dict | None = None
    interval_seconds: int | None = Field(default=None, ge=30, le=86400)
    enabled: bool | None = None


@router.get("/ddns-records")
async def list_records(user: User = Depends(get_current_user)) -> list[dict]:
    """All of the caller's DDNS records (flat overview)."""
    return await ddns_service.list_records(str(user.id))


@router.post("/ddns-records")
async def create_record(
    body: RecordReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """创建一条 DDNS 记录（域名 + 子域 + IP 来源）。

    凭据与厂商类型取自所属供应商配置；供应商不存在返回 404，已停用或
    ``ip_source``/``config`` 形状不合法返回 400。
    """
    try:
        row = await ddns_service.create_record(
            str(user.id),
            provider_id=body.provider_id, name=body.name, ip_source=body.ip_source,
            node_id=body.node_id, proxy=body.proxy, config=body.config,
            interval_seconds=body.interval_seconds, enabled=body.enabled,
        )
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    await audit_user_action(
        request, user, "ddns.record.create",
        request_body=body.model_dump(exclude_none=True), response={"id": row["id"]},
    )
    return row


@router.put("/ddns-records/{record_id}")
async def update_record(
    record_id: str, body: RecordUpdate, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """更新一条 DDNS 记录（名称/IP 来源/代理/间隔等）。

    目标不存在返回 404；IP 来源或 config 形状不合法返回 400。
    """
    try:
        row = await ddns_service.update_record(
            str(user.id), record_id,
            name=body.name, ip_source=body.ip_source, node_id=body.node_id,
            proxy=body.proxy, config=body.config,
            interval_seconds=body.interval_seconds, enabled=body.enabled,
        )
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    await audit_user_action(
        request, user, "ddns.record.update",
        request_body=body.model_dump(exclude_none=True), response={"id": row["id"]},
    )
    return row


@router.delete("/ddns-records/{record_id}")
async def delete_record(
    record_id: str, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """删除一条 DDNS 记录。

    目标不存在返回 404。
    """
    try:
        result = await ddns_service.delete_record(str(user.id), record_id)
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    await audit_user_action(
        request, user, "ddns.record.delete",
        request_body={"record_id": record_id}, response={"deleted": True},
    )
    return result


@router.post("/ddns-records/{record_id}/sync")
async def sync_record(
    record_id: str, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """Run one sync inline (same path as the tunnel_server scheduler)."""
    try:
        row = await ddns_service.get_row(str(user.id), record_id)
    except DdnsServiceError as exc:
        raise _to_http(exc) from exc
    from .ddns_sync import run_record

    result = await run_record(row)
    await audit_user_action(
        request, user, "ddns.record.sync",
        request_body={"record_id": record_id},
        response={"ok": bool(result.get("ok")), "status": result.get("last_status")},
    )
    return result
