"""节点载体路由。

挂在管理端 ``/api/v1/admin`` 下，与节点控制面/创空间同前缀（前端「节点列表 →
部署」弹框的所有调用都在一个资源路径下）。权限与节点入驻一致：``_require_admin``
——载体部署会把节点的入驻凭据交给第三方环境（ModelScope / AutoClaw 沙箱），
属管理员级操作。

与创空间的面板差异：创空间的部署是「推送 + 用户点上线」，请求内能返回；载体的
部署要等节点真拨回来（分钟级），所以 **deploy 只受理、不阻塞**——立刻返回并在
后台跑，进度写 ``NodeCarrierBinding.progress``，前端轮询 ``GET …/carrier``。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import carrier_service
from .carrier_loader import CarrierSpecError, invalidate_cache, load_carrier_class
from .deps import get_current_user
from .models import User

admin_router = APIRouter(prefix="/api/v1/admin", tags=["user-platform-carrier"])


async def _require_admin(user: User = Depends(get_current_user)) -> User:
    """Gate carrier endpoints to platform administrators（口径同 routes_studio）。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


class CarrierDeployReq(BaseModel):
    carrier: str
    config: dict | None = None
    server_url: str = ""
    role: str = "execution"
    keepalive_policy: str = "on_demand"
    keepalive_interval_minutes: int = 8


class CarrierPolicyReq(BaseModel):
    keepalive_policy: str
    keepalive_interval_minutes: int | None = None
    on_demand_cooldown_seconds: int | None = None


class CarrierSpecReq(BaseModel):
    source: str
    label: str = ""
    enabled: bool = True


def _guard(exc: Exception) -> HTTPException:
    if isinstance(exc, (carrier_service.CarrierError, CarrierSpecError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


# ── 载体目录（前端动态渲染 Tab）────────────────────────────────────────────────

@admin_router.get("/node-carriers")
async def list_node_carriers(_: object = Depends(_require_admin)) -> dict:
    """可用载体列表（MANIFEST + 已实现钩子）。前端据此渲染「安装方式」Tab。"""
    try:
        return {"carriers": await carrier_service.list_carriers()}
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.get("/node-carriers/specs")
async def list_carrier_specs(_: object = Depends(_require_admin)) -> dict:
    """载体 spec 源码台账（管理端编辑页用）。"""
    from .models_carrier import NodeCarrierSpec

    rows = await NodeCarrierSpec.all().order_by("name")
    return {"specs": [{
        "name": r.name, "label": r.label, "enabled": r.enabled, "builtin": r.builtin,
        "sha1": r.sha1[:12], "load_error": r.load_error or "",
        "source_len": len(r.source or ""),
        "updated_at": r.updated_at.isoformat() if r.updated_at else "",
    } for r in rows]}


@admin_router.get("/node-carriers/specs/{name}")
async def get_carrier_spec(name: str, _: object = Depends(_require_admin)) -> dict:
    """取单个载体 spec 源码（管理端编辑页用）。载体不存在 404。"""
    from .models_carrier import NodeCarrierSpec

    row = await NodeCarrierSpec.get_or_none(name=name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"载体 {name} 不存在")
    return {"name": row.name, "label": row.label, "enabled": row.enabled,
            "builtin": row.builtin, "sha1": row.sha1[:12],
            "load_error": row.load_error or "", "source": row.source}


@admin_router.put("/node-carriers/specs/{name}")
async def put_carrier_spec(
    name: str, body: CarrierSpecReq, _: object = Depends(_require_admin),
) -> dict:
    """新增/覆盖载体源码。**先校验能加载**再落库（否则会把坏源码写进生产）。"""
    from .models_carrier import NodeCarrierSpec

    try:
        cls = load_carrier_class(name, body.source)
    except CarrierSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    manifest = dict(getattr(cls, "MANIFEST", {}) or {})
    label = body.label.strip() or str(manifest.get("label") or name)
    import hashlib

    sha1 = hashlib.sha1(body.source.encode("utf-8")).hexdigest()
    row = await NodeCarrierSpec.get_or_none(name=name)
    if row is None:
        await NodeCarrierSpec.create(
            name=name, label=label, source=body.source,
            enabled=body.enabled, sha1=sha1, builtin=False,
        )
    else:
        row.label, row.source = label, body.source
        row.enabled, row.sha1, row.load_error = body.enabled, sha1, ""
        await row.save()
    invalidate_cache(name)
    return {"name": name, "label": label, "sha1": sha1[:12], "ok": True}


# ── 部署 / 进度 / 唤醒 / 策略 / 解绑 ───────────────────────────────────────────

@admin_router.post("/nodes/{node_id}/carrier/deploy")
async def deploy_carrier(
    node_id: str, body: CarrierDeployReq, _: object = Depends(_require_admin),
) -> dict:
    """受理部署：立刻返回，后台跑 provision→bootstrap→等拨回。

    不阻塞的原因：等节点拨回可到 600s，HTTP 请求不该挂那么久；进度写 binding，
    前端轮询 ``GET /nodes/{id}/carrier``。
    """
    if body.keepalive_policy not in ("off", "on_demand", "timed"):
        raise HTTPException(status_code=400, detail="keepalive_policy 取值非法")

    # 先确认载体存在且能加载（早失败，别等后台任务里才炸）
    try:
        await carrier_service.load_spec(body.carrier)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)

    async def _run():
        try:
            result = await carrier_service.deploy_carrier_node(
                node_id, body.carrier,
                config=body.config or {},
                server_url=body.server_url,
                role=body.role,
                keepalive_policy=body.keepalive_policy,
                keepalive_interval_minutes=body.keepalive_interval_minutes,
            )
            if not result.get("ok"):
                from loguru import logger

                logger.warning("[carrier] deploy {} failed at {}: {}",
                               node_id, result.get("stage"), result.get("error"))
        except Exception:  # noqa: BLE001 - 后台任务异常只落日志（进度行已记 failed）
            from loguru import logger

            logger.exception("[carrier] deploy {} crashed", node_id)

    asyncio.create_task(_run(), name=f"carrier-deploy-{node_id}")
    return {"node_id": node_id, "carrier": body.carrier, "started": True,
            "note": "已受理，轮询 GET /nodes/{id}/carrier 看进度"}


@admin_router.get("/nodes/{node_id}/carrier")
async def get_carrier(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """部署进度 + 宿主状态 + 保活策略（面板主轮询口）。"""
    try:
        return await carrier_service.get_carrier_status(node_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.post("/nodes/{node_id}/carrier/keepalive")
async def keepalive_carrier(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """手动「立即唤醒」（不论策略，随时可点）。"""
    try:
        return await carrier_service.keepalive_now(node_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.post("/nodes/{node_id}/carrier/policy")
async def set_carrier_policy(
    node_id: str, body: CarrierPolicyReq, _: object = Depends(_require_admin),
) -> dict:
    """改保活策略（定时 / 按需 / 关闭）+ 间隔。"""
    from .models_carrier import NodeCarrierBinding

    if body.keepalive_policy not in ("off", "on_demand", "timed"):
        raise HTTPException(status_code=400, detail="keepalive_policy 取值非法")
    binding = await NodeCarrierBinding.get_or_none(node_id=node_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="该节点没有载体绑定")
    binding.keepalive_policy = body.keepalive_policy
    if body.keepalive_interval_minutes is not None:
        # 下限 1 分钟；上限 1440（一天）——比这更长的间隔没有保活意义
        binding.keepalive_interval_minutes = max(1, min(int(body.keepalive_interval_minutes), 1440))
    if body.on_demand_cooldown_seconds is not None:
        binding.on_demand_cooldown_seconds = max(60, int(body.on_demand_cooldown_seconds))
    await binding.save()
    return {"node_id": node_id, "ok": True,
            "keepalive_policy": binding.keepalive_policy,
            "keepalive_interval_minutes": binding.keepalive_interval_minutes,
            "on_demand_cooldown_seconds": binding.on_demand_cooldown_seconds}


@admin_router.post("/nodes/{node_id}/carrier/teardown")
async def teardown_carrier(node_id: str, _: object = Depends(_require_admin)) -> dict:
    """解绑载体（不清理远端资产）。"""
    try:
        return await carrier_service.teardown_carrier(node_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


# ════════════════════════════════════════════════════════════════════════════
# 虚拟管理节点：不建真实节点，只存一行绑定（合成 id mgr-<uuid>）
# ════════════════════════════════════════════════════════════════════════════

class ManagerCreateReq(BaseModel):
    """新建虚拟管理节点。

    ``spec_name`` 决定它的 spec（入参 / 子节点入驻 tab / 详情页 tab）；``source``
    非空表示自定义贴码（此时 spec_name 固定 "custom"）。``config`` 是**管理节点级**
    参数（如创空间 token）——子节点不存凭据，部署/保活时从管理节点继承。
    """
    name: str = ""
    spec_name: str = ""
    config: dict | None = None
    keepalive_policy: str = "on_demand"
    keepalive_interval_minutes: int = 8
    source: str = ""


class ManagerUpdateReq(BaseModel):
    name: str | None = None
    config: dict | None = None
    source: str | None = None
    keepalive_policy: str | None = None
    keepalive_interval_minutes: int | None = None


class ManagerChildReq(BaseModel):
    """在虚拟管理节点下加一个执行节点（只填节点级字段，token 从管理节点继承）。"""
    node_name: str = ""
    server_address: str = ""
    config: dict | None = None


class CarrierPreviewReq(BaseModel):
    """贴一段载体代码 → 校验 + 返回 MANIFEST（不落库）。"""
    source: str


@admin_router.get("/node-managers")
async def list_node_managers(_: object = Depends(_require_admin)) -> dict:
    """列虚拟管理节点（含 spec label、子节点数与 id 列表）。"""
    try:
        return {"managers": await carrier_service.list_node_managers()}
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.post("/node-managers")
async def create_node_manager(
    body: ManagerCreateReq, _: object = Depends(_require_admin),
) -> dict:
    """新建虚拟管理节点（不调控制面，即建即用）。"""
    try:
        return await carrier_service.create_node_manager(
            name=body.name, spec_name=body.spec_name, config=body.config or {},
            keepalive_policy=body.keepalive_policy,
            keepalive_interval_minutes=body.keepalive_interval_minutes,
            source=body.source,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.get("/node-managers/{manager_id}")
async def get_node_manager(manager_id: str, _: object = Depends(_require_admin)) -> dict:
    """查单个虚拟管理节点详情（spec/配置/保活策略/子节点）。"""
    try:
        return await carrier_service.get_node_manager(manager_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.put("/node-managers/{manager_id}")
async def update_node_manager(
    manager_id: str, body: ManagerUpdateReq, _: object = Depends(_require_admin),
) -> dict:
    """改名称/参数/代码/保活策略（代码改完即热更新）。"""
    try:
        return await carrier_service.update_node_manager(
            manager_id, name=body.name, config=body.config, source=body.source,
            keepalive_policy=body.keepalive_policy,
            keepalive_interval_minutes=body.keepalive_interval_minutes,
        )
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.delete("/node-managers/{manager_id}")
async def delete_node_manager(manager_id: str, _: object = Depends(_require_admin)) -> dict:
    """删虚拟管理节点（有子节点时拒绝）。"""
    try:
        return await carrier_service.delete_node_manager(manager_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.get("/node-managers/{manager_id}/children")
async def list_manager_children(manager_id: str, _: object = Depends(_require_admin)) -> dict:
    """子节点列表 + 监控摘要（在线态 + 保活记录）。"""
    try:
        return {"children": await carrier_service.list_manager_children(manager_id)}
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.post("/node-managers/{manager_id}/children")
async def add_manager_child(
    manager_id: str, body: ManagerChildReq, _: object = Depends(_require_admin),
) -> dict:
    """加子节点：onboard → 写 bootstrap → 用管理节点的载体 provisioning（后台跑）。"""
    try:
        return await carrier_service.add_manager_child(
            manager_id, node_name=body.node_name,
            server_address=body.server_address, config=body.config or {})
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.delete("/node-managers/{manager_id}/children/{child_id}")
async def delete_manager_child(
    manager_id: str, child_id: str, _: object = Depends(_require_admin),
) -> dict:
    """删子节点：撤销控制面节点 + 清载体绑定 + 清创空间账本（若有）。"""
    try:
        return await carrier_service.delete_manager_child(manager_id, child_id)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.get("/node-carriers/specs/{name}/options")
async def get_spec_field_options(
    name: str, key: str, _: object = Depends(_require_admin),
) -> dict:
    """解析某字段的 options_source → 统一 ``{options: [{label,value}], error}``。

    前端不认识创空间等具体实现：字段声明 options_source，服务端注册表把它映射到
    真实函数（如 studio.hardware → list_studio_options）。
    """
    try:
        return await carrier_service.resolve_spec_options(name, key)
    except Exception as exc:  # noqa: BLE001
        raise _guard(exc)


@admin_router.post("/node-carriers/preview")
async def preview_carrier_source(
    body: CarrierPreviewReq, _: object = Depends(_require_admin),
) -> dict:
    """贴一段载体代码 → 校验 + 返回 MANIFEST（前端据此渲染参数表单）。不落库。"""
    try:
        cls = load_carrier_class("preview", body.source)
    except CarrierSpecError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    manifest = dict(cls.MANIFEST)
    from .carrier_loader import hook_names

    manifest["hooks"] = list(hook_names(cls))
    manifest["default_keepalive"] = manifest.get("default_keepalive") or {
        "policy": "on_demand", "interval_minutes": 8,
    }
    return {"ok": True, "manifest": manifest}

