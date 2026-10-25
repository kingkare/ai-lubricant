"""C-side 一级工具资源路由（CDP 客户端 / 邮箱服务 / 设备控制）。

资源直接归属当前用户；不再存在 builtin_tool_instances 或 instance_id API。
Storage only —— 不触碰模型请求主链路。

路由注册顺序约束：所有静态子路径（/resources/cdp-clients、/resources/mail-services、
/resources/devices、/resources/device-pairing-codes 及其子路径）必须注册在
/resources/{resource_id} 之前——FastAPI 按注册顺序匹配，动态段在前会把
"cdp-clients" 当 resource_id 解析整数失败而 422。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .deps import get_current_user
from .models import User

router = APIRouter(prefix="/api/v1/users/builtin-tools", tags=["user-platform-builtin-tools"])


class CreateCdpClientReq(BaseModel):
    name: str


class CreateMailAccountReq(BaseModel):
    display_name: str
    mailbox_type: str = "TempMail"
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    secret_key: str | None = None
    mail_suffix: str | None = None
    proxy_id: str | None = None
    enabled: bool = True


class CreateMailAddressReq(BaseModel):
    address: str
    source_address: str | None = None


class MailQueryReq(BaseModel):
    address: str | None = None
    keyword: str | None = None
    limit: int = 20
    offset: int = 0


class MintPairingCodeReq(BaseModel):
    label: str = ""


class CdpClientAgentsReq(BaseModel):
    agent_ids: list[int] = []


class UpdateResourceReq(BaseModel):
    data: dict[str, Any] = {}
    expected_revision: int | None = None


class IssueTokenReq(BaseModel):
    target_type: str
    target_id: str | None = None
    display_token: bool = False
    expires_at: datetime | None = None


async def _owned_resource(resource_id: int, user: User, *, resource_type: str | None = None) -> dict:
    import builtin_tool_store as store

    resource = await store.get_resource(resource_id, owner_user_id=str(user.id))
    if resource is None or (resource_type and resource.get("resource_type") != resource_type):
        raise HTTPException(status_code=404, detail="资源不存在")
    return resource


async def _resync_builtin(name: str) -> None:
    from mcp_runtime.configuration import resync_builtin_service
    try:
        await resync_builtin_service(name)
    except Exception:
        from loguru import logger
        logger.warning("[builtin-tools] resync '{}' failed (data is persisted)", name)


async def _resync_resource(resource_type: str) -> None:
    await _resync_builtin(
        "cdp-bridge" if resource_type == "cdp_client"
        else "mail" if resource_type in ("mail_account", "mail_address")
        else "device-control"
    )


async def _validate_mail_proxy_id(value: dict) -> None:
    """校验邮箱服务的出站代理绑定：proxy_id 非空必须命中代理池，否则 400。

    与账号出站代理同一口径（不能把出站绑到一个不存在的条目上，查询时会静默降级
    直连而无人察觉）。空/None = 直连，直接放行。直接读 CONFIG_FILE 的代理池全量
    （管理端与用户端同源），不做归属过滤——绑定引用 id 而非凭据，不外泄地址。
    """
    proxy_id = str(value.get("proxy_id") or "").strip()
    if not proxy_id:
        return
    import admin
    from proxy_utils import find_proxy
    proxies = admin._read_proxies()
    if find_proxy(proxies, proxy_id) is None:
        raise HTTPException(status_code=400, detail=f"代理 {proxy_id} 不存在，请先在代理池中添加")


# ── 一级资源 CRUD ─────────────────────────────────────────────────────────────

@router.get("/resources")
async def list_resources(resource_type: str | None = None, user: User = Depends(get_current_user)) -> dict:
    """列出当前用户名下的工具资源，可用 ``resource_type`` 过滤（cdp_client/mail_account/mail_address/device）。"""
    import builtin_tool_store as store
    return {"resources": await store.list_resources(resource_type, owner_user_id=str(user.id))}


@router.post("/resources")
async def create_resource(body: UpdateResourceReq, user: User = Depends(get_current_user)) -> dict:
    """新建一条工具资源。

    ``data.resource_type``（或 detail_type）必须是 cdp_client / mail_account /
    mail_address / device 之一，否则返回 400。创建后触发对应内置 MCP 服务的
    重同步（失败只记日志，不影响落库结果）。
    """
    resource_type = str(body.data.get("resource_type") or body.data.get("detail_type") or "")
    if resource_type not in {"cdp_client", "mail_account", "mail_address", "device"}:
        raise HTTPException(status_code=400, detail="不支持的资源类型")
    import builtin_tool_store as store
    value = {k: v for k, v in body.data.items() if k not in ("resource_type", "detail_type")}
    row = await store.create_resource(str(user.id), resource_type, value)
    await _resync_resource(resource_type)
    return row


# ── CDP 客户端资源 ────────────────────────────────────────────────────────────

@router.get("/resources/cdp-clients")
async def list_cdp_clients_resource(user: User = Depends(get_current_user)) -> dict:
    """列出当前用户的 CDP 浏览器客户端，并叠加内存里的实时连接状态与已打开页面数。"""
    import builtin_tool_store as store
    resources = await store.list_resources("cdp_client", owner_user_id=str(user.id))
    try:
        from mcp_builtin.cdp_bridge import server as cdp_server
        contexts = cdp_server.get_driver().snapshot_contexts()
    except Exception:
        contexts = []
    live_by_id: dict[str, dict] = {}
    for context in contexts or []:
        for live in context.get("clients") or []:
            live_by_id[str(live.get("client_id") or "")] = live
    for resource in resources:
        # live 快照按客户端的对外身份（新=随机串、存量=行 id）建键。
        live = live_by_id.get(store.cdp_client_id_of(resource)) or {}
        resource["connected"] = bool(live.get("connected"))
        resource["pages"] = live.get("pages") or []
    return {"clients": resources}


@router.post("/resources/cdp-clients")
async def create_cdp_client_resource(body: CreateCdpClientReq, user: User = Depends(get_current_user)) -> dict:
    """新建一个 CDP 浏览器客户端，返回客户端记录与一次性连接 token（仅在此时明文返回）。"""
    import builtin_tool_store as store
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="客户端名称必填")
    client, token = await store.create_cdp_client(str(user.id), body.name.strip())
    await _resync_builtin("cdp-bridge")
    return {"client": client, "token": token}


@router.post("/resources/cdp-clients/{resource_id}/rotate-token")
async def rotate_cdp_client_resource(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """轮换 CDP 客户端连接 token（旧 token 立即失效），返回新 token。仅限本人的 cdp_client。"""
    await _owned_resource(resource_id, user, resource_type="cdp_client")
    import builtin_tool_store as store
    try:
        client, token = await store.rotate_cdp_token(resource_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="客户端不存在") from exc
    await _resync_builtin("cdp-bridge")
    return {"client": client, "token": token}


@router.post("/resources/cdp-clients/{resource_id}/revoke-token")
async def revoke_cdp_client_resource(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """吊销 CDP 客户端连接 token（不再能建立连接），不删除客户端本身。"""
    await _owned_resource(resource_id, user, resource_type="cdp_client")
    import builtin_tool_store as store
    client = await store.revoke_cdp_token(resource_id)
    if client is None:
        raise HTTPException(status_code=404, detail="客户端不存在")
    await _resync_builtin("cdp-bridge")
    return {"client": client}


# ── CDP 客户端「可操作 agent」（权限单轨化：数据存 agent 侧 mcp_grants） ──────────
#
# agent 侧的 cdp_client_id grant 才是「谁能驱动这个浏览器」的唯一真相源。这两个端点
# 只是把那个真相源按「浏览器」视角聚合/写回，读写的都是 mcp_grants，不在 cdp_client
# 资源行上存 agent 列表（旧 agent_ids 字段已废弃移除）。

@router.get("/resources/cdp-clients/{resource_id}/agents")
async def list_cdp_client_agents(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """该浏览器可授权给哪些 agent（当前用户可见的），含已授权标记。"""
    import mcp_plugin_store as store
    await _owned_resource(resource_id, user, resource_type="cdp_client")
    from .deps import resolve_team_id
    team_id = await resolve_team_id(str(user.id))
    agents = await store.list_agents_authorized_for_cdp_client(
        str(resource_id), caller=str(user.id), team_id=team_id,
    )
    return {"agents": agents}


@router.put("/resources/cdp-clients/{resource_id}/agents")
async def set_cdp_client_agents(
    resource_id: int, body: CdpClientAgentsReq, user: User = Depends(get_current_user),
) -> dict:
    """全量替换该浏览器可用的 agent 集（写 agent 侧 mcp_grants 的 cdp_client_id 行）。"""
    import mcp_plugin_store as store
    await _owned_resource(resource_id, user, resource_type="cdp_client")
    from .deps import resolve_team_id
    team_id = await resolve_team_id(str(user.id))
    try:
        agents = await store.set_cdp_client_agents(
            str(resource_id), body.agent_ids, caller=str(user.id), team_id=team_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"agents": agents}


# ── 邮箱服务资源 ──────────────────────────────────────────────────────────────

@router.get("/resources/mail-services")
async def list_mail_services(user: User = Depends(get_current_user)) -> dict:
    """列出当前用户的邮箱服务（mail_account），并把同一账号下的所有地址挂到 ``addresses`` 字段。

    绑定了出站代理的账户额外带 ``proxy_name``（代理池只暴露 id/name/mode，不含地址凭据，
    与 /api/v1/teams/proxies 同口径），供列表直接显示人类可读名而非 hash id。
    """
    import builtin_tool_store as store
    accounts = await store.list_resources("mail_account", owner_user_id=str(user.id))
    addresses = await store.list_resources("mail_address", owner_user_id=str(user.id))
    pool: list[dict] | None = None  # 惰性读代理池（多数账户不绑代理，避免每次读盘）
    for account in accounts:
        account["resource_id"] = int(account["id"])
        account["addresses"] = addresses
        proxy_id = str(account.get("proxy_id") or "").strip()
        if not proxy_id:
            continue
        if pool is None:
            import admin
            pool = admin._read_proxies()
        from proxy_utils import find_proxy
        entry = find_proxy(pool, proxy_id)
        account["proxy_name"] = (entry.get("name") or entry.get("id") or "") if entry else ""
    return {"services": accounts}


@router.post("/resources/mail-services")
async def create_mail_service(body: CreateMailAccountReq, user: User = Depends(get_current_user)) -> dict:
    """新建一个邮箱服务账号（名称必填；可带 provider 类型、base_url、凭据等配置）。"""
    import builtin_tool_store as store
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="邮箱服务名称必填")
    value = body.model_dump(exclude_none=True)
    await _validate_mail_proxy_id(value)
    row = await store.create_resource(str(user.id), "mail_account", value)
    row["resource_id"] = int(row["id"])
    row["addresses"] = []
    await _resync_builtin("mail")
    return row


@router.delete("/resources/mail-services/{resource_id}")
async def delete_mail_service(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """删除一个邮箱服务（不可恢复），仅限本人名下的 mail_account；随后重同步 mail 服务。"""
    import builtin_tool_store as store
    await _owned_resource(resource_id, user, resource_type="mail_account")
    deleted = await store.delete_resource(resource_id, owner_user_id=str(user.id))
    await _resync_builtin("mail")
    return {"deleted": deleted}


@router.post("/resources/mail-services/{resource_id}/addresses")
async def create_mail_service_address(resource_id: int, body: CreateMailAddressReq, user: User = Depends(get_current_user)) -> dict:
    """为指定邮箱服务新增一个可用地址（可选记录来源地址）。"""
    account = await _owned_resource(resource_id, user, resource_type="mail_account")
    import builtin_tool_store as store
    value = body.model_dump(exclude_none=True)
    value["parent_resource_id"] = resource_id
    value["account_resource_id"] = resource_id
    row = await store.create_resource(str(user.id), "mail_address", value)
    await _resync_builtin("mail")
    return row


@router.post("/resources/mail-services/{resource_id}/query")
async def query_mail_service(resource_id: int, body: MailQueryReq, user: User = Depends(get_current_user)) -> dict:
    """查询指定邮箱服务收件箱的邮件（可按 address/keyword 过滤，支持 limit/offset 分页）。"""
    await _owned_resource(resource_id, user, resource_type="mail_account")
    import builtin_tool_store as store
    try:
        return await store.mail_query_for_resource(
            resource_id, address=body.address or "", keyword=body.keyword or "",
            limit=body.limit, offset=body.offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── 设备控制资源 ──────────────────────────────────────────────────────────────

@router.get("/resources/devices")
async def list_devices_resource(user: User = Depends(get_current_user)) -> dict:
    """列出当前用户的设备资源，并叠加内存实时状态（在线、能力、最后在线）。

    platform 三级推导：节点上报的 ``device_info.platform`` 最权威，其次看
    ``ios`` 块（iOS 认领时补写的身份），最后按有无 node_id 兜底。在线设备的
    ``last_seen_at`` 取内存心跳值，离线则用落库的断连时刻。iOS 设备还会合并
    节点 inventory 的 WDA 状态（见 ``_merge_ios_inventory``）。
    """
    import builtin_tool_store as store
    resources = await store.list_resources("device", owner_user_id=str(user.id))
    try:
        from mcp_builtin.device_control import driver as dc_driver
        live_rows = dc_driver.get_driver().snapshot_devices()
    except Exception:
        live_rows = []
    live_by_id = {str(r.get("device_id") or ""): r for r in live_rows or []}
    from datetime import datetime, timezone
    for resource in resources:
        live = live_by_id.get(str(resource.get("device_id") or "")) or {}
        info = live.get("device_info") or resource.get("device_info") or {}
        resource["device_info"] = info
        # platform 三级推导：节点上报的 device_info.platform 最权威；其次看
        # resource.data.ios 块（iOS 认领时补写的身份，见 _adopt_claimed_ios_resource）；
        # 最后按有无 node_id 兜底。
        #
        # 此前只看 device_info.node_id —— 而 iOS 认领走的是通用配对端点，建出来的行
        # 既无 platform 也无 node_id，于是 iPhone 被判成 android，掉进 Android 列表，
        # 前端按 platform==="ios" 过滤时找不到它（「找不到该设备的资源 id」）。
        ios_block = resource.get("ios") if isinstance(resource.get("ios"), dict) else {}
        resource["platform"] = str(
            info.get("platform")
            or ("ios" if ios_block.get("node_id") or ios_block.get("udid") else "")
            or ("ios" if info.get("node_id") else "android")
        )
        resource["online"] = bool(live)
        resource["capabilities"] = live.get("capabilities") or []
        # 在线设备的「最后在线」用内存里心跳刷新的 last_seen（epoch 秒，每帧都 touch），
        # 离线设备没 live，落库的 last_seen_at（断连时刻）兜底——两层拼接出准点的时间。
        live_last_seen = live.get("last_seen")
        if isinstance(live_last_seen, (int, float)):
            resource["last_seen_at"] = datetime.fromtimestamp(
                live_last_seen, timezone.utc,
            ).isoformat(timespec="seconds")
        # last_error / last_error_at 由 _resource_value 把 data JSONB 扁平化到顶层，
        # 已自带，无需在此再拼。node_id 同理。
        node_id = str(info.get("node_id") or "")
        if node_id:
            resource["node_id"] = node_id

    await _merge_ios_inventory(resources)
    return {"devices": resources}


# iOS 设备状态字段表 / 归一化 / 合并逻辑已提到 ios_device_reconcile（自动扫描对账
# 模块共用，避免循环依赖）。这里保留旧名做别名，兼容既有调用与测试。
from .ios_device_reconcile import (  # noqa: E402
    IOS_INVENTORY_FIELDS as _IOS_INVENTORY_FIELDS,
    apply_inventory_to_ios_block as _apply_inventory_to_ios_block,
    control_ready_from_wda_state as _control_ready_from_wda_state,
    decide_reconcile as _decide_reconcile,
    init_state_from_wda_state as _init_state_from_wda_state,
    _INIT_STATE_BY_WDA_STATE,
)


async def _merge_ios_inventory(resources: list[dict]) -> None:
    """把节点 inventory 的 WDA 状态合并进各 iOS 设备的 data.ios 块（就地改）。

    按 node_id 分组，每节点只取一次 inventory（避免逐设备 N+1）。节点离线或查不到
    时**保留资源里已有的值**——不能把上一次已知状态覆盖成空，否则界面会在节点抖动
    时闪回「待初始化」。

    匹配键用 **udid**（稳定身份）而非 device_id：节点侧未认领时 device_id 为空，
    按 device_id 匹配会让「服务端有行、节点丢了认领」的设备永远匹配不上 → 恒显示
    离线。udid 认领与否都在，能正确刷新在线轴。
    """
    from .node_client import get_node_client, NodeServerUnavailable, RPCError
    from loguru import logger

    # udid -> resource，只挑 iOS 设备（有 ios 块 + ios.udid）。
    targets: dict[str, dict] = {}
    by_node: dict[str, list[str]] = {}
    for r in resources:
        ios_block = r.get("ios") if isinstance(r.get("ios"), dict) else None
        if not ios_block:
            continue
        node_id = str(ios_block.get("node_id") or r.get("node_id") or "").strip()
        udid = str(ios_block.get("udid") or "").strip()
        if not node_id or not udid:
            continue
        targets[udid] = r
        by_node.setdefault(node_id, []).append(udid)

    if not targets:
        return

    client = get_node_client()
    for node_id, udids in by_node.items():
        try:
            inv = await client.get_ios_devices(node_id)
        except (NodeServerUnavailable, RPCError) as exc:
            # 节点不可达：保留资源里已有的 wda_state（上一次已知），只记日志。
            # 在线轴同理保留——节点离线不代表手机被拔了，不能把在线设备标成离线。
            logger.debug("[ios-devices] inventory unavailable for {}: {}", node_id, exc)
            continue
        for dev in inv.get("devices") or []:
            resource = targets.get(str(dev.get("udid") or "").strip())
            if resource is None:
                continue
            ios_block = dict(resource.get("ios") or {})
            _apply_inventory_to_ios_block(ios_block, dev)
            resource["ios"] = ios_block


@router.post("/resources/device-pairing-codes")
async def create_device_pairing_resource(body: MintPairingCodeReq, user: User = Depends(get_current_user)) -> dict:
    """签发一个设备配对码（一次性，绑定当前用户名下的设备资源）。

    返回配对码与 TTL（秒）；配对码由 device-control 协议兑换一次后即建出
    直连资源行。Redis 不可用返回 503，迁移未完成返回 501。
    """
    # Pairing codes are scoped to the user's device resources in the store; the
    # device-control protocol accepts the code once and creates a direct resource.
    import mcp_builtin.device_control.store as dc_store
    try:
        code, ttl = await dc_store.mint_pairing_code_for_owner(str(user.id), body.label)
    except AttributeError:
        raise HTTPException(status_code=501, detail="设备配对资源迁移尚未完成")
    except dc_store.RedisUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"code": code, "ttl": ttl}


# ── iOS device management (capability ios_mgmt) ─────────────────────────────────
# Hosts are selected by *role*, not by a self-reported capability label. The
# ios_mgmt label is a boot-time probe (usbmuxd reachability at node start) whose
# negative result is sticky for the process lifetime — one unlucky register and
# the label vanishes until the node restarts, silently hiding a perfectly good
# host. The real verdict comes from the frames themselves: a node without the
# iOS stack error-acks with "iOS device management is not enabled on this host",
# which the scan UI already surfaces per-host. So: any execution/ios_host node
# the user can reach is a candidate host; management (grouping-only) rows are
# excluded.
def _is_ios_host(node: dict) -> bool:
    """True when a list_my_nodes row may serve iOS device management."""
    if node.get("display_only"):
        return False
    role = str(node.get("node_role") or node.get("role") or "")
    return role not in ("management", "passive_management")


@router.get("/resources/ios-hosts")
async def list_ios_hosts(user: User = Depends(get_current_user)) -> dict:
    """List available iOS host nodes the user can access."""
    from .nodes_service import nodes_service

    my_nodes = (await nodes_service.list_my_nodes(user.id)).get("nodes") or []
    hosts = [n for n in my_nodes if _is_ios_host(n)]
    return {"ios_hosts": hosts}


@router.post("/resources/ios-hosts/all/scan")
async def scan_all_ios_devices(user: User = Depends(get_current_user)) -> dict:
    """Ask every online iOS host to refresh its device inventory.

    Discovery is asynchronous on the node (the ack only means the frame was
    accepted). The frontend follows this with a short polling read of the
    aggregate inventory; this endpoint returns per-host dispatch failures so a
    broken host does not hide successful scans elsewhere.
    """
    import asyncio

    from .node_client import get_node_client, NodeServerUnavailable, RPCError
    from .nodes_service import nodes_service

    my_nodes = (await nodes_service.list_my_nodes(user.id)).get("nodes") or []
    hosts = [n for n in my_nodes if _is_ios_host(n)]
    client = get_node_client()

    async def _one(host: dict) -> dict:
        node_id = str(host.get("node_id") or "")
        result = {"node_id": node_id, "dispatched": False, "error": None}
        if not host.get("online"):
            result["error"] = "节点离线"
            return result
        if not await nodes_service.user_can_use_node(user.id, node_id):
            result["error"] = "无权访问该节点"
            return result
        try:
            await client.ios_discover(node_id)
            result["dispatched"] = True
        except (NodeServerUnavailable, RPCError) as exc:
            result["error"] = str(exc) or exc.__class__.__name__
        return result

    return {"hosts": await asyncio.gather(*(_one(h) for h in hosts))}


@router.get("/resources/ios-hosts/all/devices")
async def list_all_ios_devices(user: User = Depends(get_current_user)) -> dict:
    """Aggregate device inventory across every ios_host the user can access.

    Returns per-node results so one offline host doesn't hide the rest:
    ``{"hosts": [{"node_id","node_name","online","devices":[...],"error":?}, ...],
       "devices": [...]}`` — the flat ``devices`` list is the union with node_id/
    node_name stamped on each entry for the scan UI's grouping.
    """
    import asyncio

    from .node_client import get_node_client, NodeServerUnavailable, RPCError
    from .nodes_service import nodes_service

    my_nodes = (await nodes_service.list_my_nodes(user.id)).get("nodes") or []
    hosts = [n for n in my_nodes if _is_ios_host(n)]

    async def _one(host: dict) -> dict:
        node_id = str(host.get("node_id") or "")
        entry = {
            "node_id": node_id,
            "node_name": host.get("name") or node_id,
            "online": bool(host.get("online")),
            "devices": [],
            "error": None,
        }
        if not entry["online"]:
            entry["error"] = "节点离线"
            return entry
        if not await nodes_service.user_can_use_node(user.id, node_id):
            entry["error"] = "无权访问该节点"
            return entry
        try:
            inv = await get_node_client().get_ios_devices(node_id)
        except (NodeServerUnavailable, RPCError) as exc:
            entry["error"] = str(exc) or exc.__class__.__name__
            return entry
        devices = []
        for d in inv.get("devices") or []:
            stamped = dict(d)
            stamped["node_id"] = node_id
            stamped["node_name"] = entry["node_name"]
            devices.append(stamped)
        entry["devices"] = devices
        return entry

    results = await asyncio.gather(*(_one(h) for h in hosts))
    flat: list[dict] = []
    for r in results:
        flat.extend(r["devices"])
    return {"hosts": results, "devices": flat}


@router.post("/resources/ios-hosts/{node_id}/scan")
async def scan_ios_devices(node_id: str, user: User = Depends(get_current_user)) -> dict:
    """Ask one online iOS host to refresh its asynchronous inventory."""
    from .node_client import get_node_client, NodeServerUnavailable, RPCError
    from .nodes_service import nodes_service

    if not await nodes_service.user_can_use_node(user.id, node_id):
        raise HTTPException(status_code=403, detail="您无权访问此节点")
    try:
        return await get_node_client().ios_discover(node_id)
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RPCError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc


@router.get("/resources/ios-hosts/{node_id}/devices")
async def list_ios_devices(node_id: str, user: User = Depends(get_current_user)) -> dict:
    """Retrieve the cached device inventory for one ios_host node."""
    from .nodes_service import nodes_service
    from .node_client import get_node_client, NodeServerUnavailable, RPCError

    if not await nodes_service.user_can_use_node(user.id, node_id):
        raise HTTPException(status_code=403, detail="您无权访问此节点")

    client = get_node_client()
    try:
        return await client.get_ios_devices(node_id)
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RPCError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail=exc.message) from exc
        if exc.code == "permission_denied":
            raise HTTPException(status_code=403, detail=exc.message) from exc
        if exc.code == "failed_precondition":
            raise HTTPException(status_code=412, detail=exc.message) from exc
        raise HTTPException(status_code=500, detail=exc.message) from exc


@router.post("/resources/ios-claims")
async def claim_ios_device(
    body: dict,
    user: User = Depends(get_current_user),
) -> dict:
    """Claim an iOS device: mint a pairing code, dispatch claim frame, wait for ack.

    iOS 认领复用 Android 的配对端点（``POST /mcp/device-control/pair``）：节点拿到
    配对码后自己兑换，服务端在那一刻只建出一条**通用 device 资源行**——它不知道
    这是 iPhone，所以既没有 ``platform`` 也没有 ``ios`` 块。后果是前端按
    ``platform === "ios"`` 过滤时它掉进 Android 列表，``data.ios`` 缺失又让 WDA
    派发（routes_ios.start_wda_job 要求 node_id/udid）必然 400。

    这里在 ack 之后把 iOS 身份补写回去，并把 ``resource_id`` 返回给调用方——前端
    claimOne 期待的就是它，此前返回体里没有，导致「接入并初始化」拿到 undefined。
    """
    from .nodes_service import nodes_service
    from .node_client import get_node_client, NodeServerUnavailable, RPCError
    import mcp_builtin.device_control.store as dc_store
    from server import builtin_tool_store

    node_id = (body.get("node_id") or "").strip()
    udid = (body.get("udid") or "").strip()
    label = (body.get("label") or "").strip()
    # force=重新配对：修复「节点仍持有认领、服务端已无行」的孤儿（删除时节点离线、
    # 或认领中途失败留下的半成品）。先 force-release 再走正常认领。
    force = bool(body.get("force"))
    if not node_id or not udid:
        raise HTTPException(status_code=400, detail="node_id and udid are required")

    if not await nodes_service.user_can_use_node(user.id, node_id):
        raise HTTPException(status_code=403, detail="您无权访问此节点")

    if force:
        # 孤儿修复：节点可能仍持该 UDID 的认领（dev.claimed=true），此时正常 Claim
        # 会撞 "already claimed"。先 force-release（删节点凭据 + devices.json 条目）
        # 把节点打回未认领。忽略 not_found / 节点离线——后面的认领会给出真正的错误。
        #
        # 竞态安全：节点 Release 在锁内先改 claimed=false 并同步移除 devices.json
        # 条目、**然后**才 ack；我们 await 到 ack 后才派发 Claim，此刻必然未认领。
        # device_id 传空串走 udid 匹配（孤儿的 device_id 可能与已知的不同）。
        try:
            await get_node_client().ios_release_device(node_id, "", udid, delete_credential=True)
        except (NodeServerUnavailable, RPCError):
            pass

    # Conflict check: is this UDID already claimed by anyone?
    #
    # 自愈：查到同 UDID 的行时，先核对节点侧是否真的还持有认领——「服务端有行、
    # 节点无认领」是脏状态（节点重启/换版本/被释放后行没清），此时应删掉陈旧行让
    # 本次认领继续，而不是拿一个 409 把用户永久挡在门外。
    existing = await builtin_tool_store.list_resources(
        owner_user_id=None, resource_type="device"
    )
    for res in existing:
        # list_resources 返回 _resource_value 扁平化后的 DTO（无 "data" 层）。
        # 此前读 res["data"] 恒为 {}，冲突检查形同虚设——同一台 iPhone 能被
        # 重复认领。
        ios_info = res.get("ios") or {}
        if ios_info.get("udid") != udid:
            continue
        stale = await _ios_row_is_stale(res, udid)
        if stale:
            # 陈旧行：删掉它（顺带释放节点残留），继续本次认领。
            await _release_ios_claim(
                str(ios_info.get("node_id") or "").strip(),
                str(res.get("device_id") or "").strip(),
                udid,
            )
            try:
                await builtin_tool_store.delete_resource(int(res["id"]))
                await _resync_builtin("device-control")
                from loguru import logger

                logger.info(
                    "[ios-claim] deleted stale row id=%s udid=%s (node holds no claim)",
                    res.get("id"), udid,
                )
            except Exception:  # noqa: BLE001 — 删失败则保守 409（别建重复行）
                raise HTTPException(status_code=409, detail=f"UDID {udid} 已被认领")
            break
        # 节点仍持认领：真的被占，保持 409。
        raise HTTPException(status_code=409, detail=f"UDID {udid} 已被认领")

    # Mint a one-time pairing code
    try:
        code, ttl = await dc_store.mint_pairing_code_for_owner(str(user.id), label)
    except dc_store.RedisUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Dispatch the claim frame
    client = get_node_client()
    try:
        result = await client.ios_claim_device(node_id, udid, label, code)
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RPCError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail=exc.message) from exc
        if exc.code == "permission_denied":
            raise HTTPException(status_code=403, detail=exc.message) from exc
        if exc.code == "failed_precondition":
            raise HTTPException(status_code=412, detail=exc.message) from exc
        if exc.code == "deadline_exceeded":
            raise HTTPException(status_code=504, detail=exc.message) from exc
        raise HTTPException(status_code=500, detail=exc.message) from exc

    # 把刚配对的资源行认成 iOS。节点在 ack 之前已经 reportLocked 上报了新清单
    # （manager.Claim 末尾），所以这里读 inventory 能拿到它刚写入的 device_id
    # ——就是配对端点签发的那个，和资源行的 device_id 同源。
    resource_id = await _adopt_claimed_ios_resource(
        node_id=node_id, udid=udid, owner_user_id=str(user.id), label=label
    )
    return {**result, "resource_id": resource_id}


async def _adopt_claimed_ios_resource(
    *, node_id: str, udid: str, owner_user_id: str, label: str
) -> int | None:
    """把节点刚认领的 iPhone 对应的资源行标记成 iOS，返回 resource_id。

    节点的 device_id（配对端点签发）与资源行的 device_id 同源，用它把两边接上。
    补写 ``ios`` 块（udid/node_id）与 ``platform``：

    - ``platform`` 让 /resources/devices 把它归到 iOS 桶（否则落 android）；
    - ``ios.node_id`` / ``ios.udid`` 是 WDA 派发的硬前置
      （routes_ios.start_wda_job 缺一即 400「设备缺少 iOS 节点绑定信息」）；
    - ``wda_state`` 初值 missing，让设备卡片显示「待初始化」而不是「离线」。

    best-effort：认领本身已成功（凭据已下发到节点），补写失败不该让整个操作报错，
    只记日志——用户重新扫描时 inventory 里已有 claimed 标记。
    """
    from server import builtin_tool_store
    from .node_client import get_node_client
    from loguru import logger

    try:
        inv = await get_node_client().get_ios_devices(node_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ios-claim] read inventory for adopt failed: {}", exc)
        return None

    target = next(
        (d for d in (inv.get("devices") or []) if str(d.get("udid") or "") == udid),
        None,
    )
    if target is None or not target.get("device_id"):
        logger.warning("[ios-claim] no inventory entry for udid {} on {}", udid, node_id)
        return None

    device_id = str(target["device_id"])
    rows = await builtin_tool_store.list_resources(
        owner_user_id=owner_user_id, resource_type="device"
    )
    row = next((r for r in rows if str(r.get("device_id") or "") == device_id), None)
    if row is None:
        logger.warning("[ios-claim] no resource row for device_id {}", device_id)
        return None

    # list_resources 已把 data JSONB 展平到顶层（_resource_value），所以 ios 块
    # 直接读 row["ios"]，不是 row["data"]["ios"]。
    ios_payload = dict(row.get("ios") or {})
    ios_payload.update({
        "udid": udid,
        "node_id": node_id,
        # missing = 已认领但还没初始化 WDA。设备卡片据此显示「待初始化」，
        # 与「离线」（present=false / 节点断连）区分开。
        "wda_state": ios_payload.get("wda_state") or "missing",
    })
    await builtin_tool_store.update_resource(
        int(row["id"]),
        {"ios": ios_payload, "platform": "ios", "name": label or row.get("name") or udid},
    )
    return int(row["id"])


async def ios_device_is_present(node_id: str, udid: str) -> bool:
    """这台手机此刻是否被宿主节点通过 usbmuxd 枚举到（= 在线）。

    在线轴的定义就是「go-ios 扫得到」：手机插着、被系统识别，就算在线；拔了就
    不算。刻意不看 device_control_online（守护 WS）——守护要 WDA 装好之后才起来，
    拿它当门禁会让第一次初始化永远开始不了。

    节点不可达时返回 False（宁可拦住，也不要对着一个联系不上的节点派发）。
    """
    from .node_client import get_node_client, NodeServerUnavailable, RPCError

    node_id = (node_id or "").strip()
    udid = (udid or "").strip()
    if not node_id or not udid:
        return False
    try:
        inv = await get_node_client().get_ios_devices(node_id)
    except (NodeServerUnavailable, RPCError):
        return False
    for d in (inv.get("devices") or []) if isinstance(inv, dict) else []:
        if str(d.get("udid") or "").strip() == udid:
            return bool(d.get("present"))
    return False


async def _ensure_ios_device_online(ios_info: dict, *, action: str) -> None:
    """下发前的在线门禁：离线直接 409，不让请求走到节点再以 device_not_present 失败。

    用户在界面上看到的是「设备离线」，那就应该在这里被拦住并说清楚，而不是让
    一个必然失败的 job 跑到流水线深处。
    """
    node_id = str((ios_info or {}).get("node_id") or "").strip()
    udid = str((ios_info or {}).get("udid") or "").strip()
    if not node_id or not udid:
        raise HTTPException(status_code=400, detail="设备缺少 iOS 节点绑定信息")
    if not await ios_device_is_present(node_id, udid):
        raise HTTPException(
            status_code=409,
            detail=f"设备不在线，无法{action}。请确认 iPhone 已连接宿主节点并被识别后重试。",
        )


@router.post("/resources/{resource_id}/ios-config")
async def save_ios_device_config(
    resource_id: int, body: dict, user: User = Depends(get_current_user)
) -> dict:
    """保存 iOS 设备的初始化配置，并在设备在线时下发给宿主节点。

    这是「初始化配置」唯一的落点——此前 NodeIosConfigureDevice 这条 RPC 端到端
    都在（proto → node_server → node_client → 节点 manager），却没有 HTTP 路由和
    任何调用方；前端那个 bundle id 输入框是纯局部 state，只在 prepare 时当一次性
    参数用，不落库、不生效。

    body: {
      signing_profile_id?: int, wda_bundle_id?: str, xctest_config_name?: str,
      transport?: "usb"|"network", renew_before_days?: int, auto_renew?: bool,
    }

    离线时**只落库不下发**，等设备上线后由节点侧等待段接上；在线则立刻下发。
    """
    from .node_client import get_node_client, NodeServerUnavailable, RPCError
    import builtin_tool_store as store

    resource = await _owned_resource(resource_id, user, resource_type="device")
    # _owned_resource → get_resource 返回扁平化 DTO（无 "data" 层）。
    ios_info = dict(resource.get("ios") or {})
    node_id = str(ios_info.get("node_id") or "").strip()
    udid = str(ios_info.get("udid") or "").strip()
    device_id = str(resource.get("device_id") or "").strip()
    if not node_id or not udid:
        raise HTTPException(status_code=400, detail="此设备缺少 iOS 节点绑定信息")

    # 收集本次要写的配置（只写显式传了的键，避免把未提交的字段清空）。
    patch: dict[str, object] = {}
    if "signing_profile_id" in body:
        raw = body.get("signing_profile_id")
        patch["signing_profile_id"] = int(raw) if raw not in (None, "") else None
    if "wda_bundle_id" in body:
        patch["wda_bundle_id"] = str(body.get("wda_bundle_id") or "").strip()
    if "xctest_config_name" in body:
        patch["xctest_config_name"] = str(body.get("xctest_config_name") or "").strip()
    if "transport" in body:
        transport = str(body.get("transport") or "").strip().lower()
        if transport not in ("", "usb", "network"):
            raise HTTPException(status_code=400, detail="transport 必须是 usb|network")
        patch["transport"] = transport
    if "renew_before_days" in body:
        try:
            days = int(body.get("renew_before_days") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="renew_before_days 必须是整数")
        patch["renew_before_days"] = max(days, 0) or 14
    if "auto_renew" in body:
        patch["auto_renew"] = bool(body.get("auto_renew"))

    if not patch:
        raise HTTPException(status_code=400, detail="没有需要保存的配置项")

    # config_revision 单调递增：节点按 revision 幂等（manager.go ConfigureDevice），
    # 重放/乱序的旧配置不会把设备回滚。
    revision = int(ios_info.get("config_revision") or 0) + 1
    patch["config_revision"] = revision
    ios_info.update(patch)

    online = await ios_device_is_present(node_id, udid)
    # 离线也保存：配置是「意愿」，下发是「动作」。用户拔着线配好、插上就生效，
    # 不该逼他一直插着才能改配置。
    await store.update_resource(resource_id, {"ios": ios_info})

    if not online:
        return {
            "saved": True,
            "dispatched": False,
            "config_revision": revision,
            "detail": "配置已保存。设备当前不在线，接入宿主节点后会自动生效。",
        }

    client = get_node_client()
    try:
        await client.ios_configure_device(
            node_id,
            device_id,
            udid,
            revision,
            transport=str(patch.get("transport") or ""),
            wda_bundle_id=str(patch.get("wda_bundle_id") or ""),
            xctest_config_name=str(patch.get("xctest_config_name") or ""),
            auto_prepare=False,
            renew_before_days=int(patch.get("renew_before_days") or 14),
        )
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RPCError as exc:
        if exc.code == "failed_precondition":
            raise HTTPException(status_code=412, detail=exc.message) from exc
        if exc.code == "deadline_exceeded":
            raise HTTPException(status_code=504, detail=exc.message) from exc
        raise HTTPException(status_code=500, detail=exc.message) from exc

    return {"saved": True, "dispatched": True, "config_revision": revision}


@router.post("/resources/{resource_id}/runner-control")
async def ios_runner_control(resource_id: int, body: dict, user: User = Depends(get_current_user)) -> dict:
    """Start/stop/restart the persistent device-control runner loop on a claimed
    iOS device (no re-claim, no credential change).

    body: {"action": "start"|"stop"|"restart"}。装 runner 走既有 WDA job
    （prepare/renew/reinstall，见 routes_ios.py）；这里只管常驻守护循环。
    """
    from .node_client import get_node_client, NodeServerUnavailable, RPCError

    action = (body.get("action") or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="action 必须是 start|stop|restart")

    resource = await _owned_resource(resource_id, user, resource_type="device")
    # _owned_resource → get_resource 返回扁平化 DTO（无 "data" 层）。
    data = resource
    ios_info = data.get("ios") or {}
    node_id = ios_info.get("node_id") or ""
    udid = ios_info.get("udid") or ""
    device_id = data.get("device_id") or ""

    if not node_id or not udid:
        raise HTTPException(status_code=400, detail="此设备缺少 iOS 节点绑定信息")

    client = get_node_client()
    try:
        return await client.ios_runner_control(node_id, device_id, udid, action)
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RPCError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail=exc.message) from exc
        if exc.code == "permission_denied":
            raise HTTPException(status_code=403, detail=exc.message) from exc
        if exc.code == "failed_precondition":
            raise HTTPException(status_code=412, detail=exc.message) from exc
        if exc.code == "deadline_exceeded":
            raise HTTPException(status_code=504, detail=exc.message) from exc
        raise HTTPException(status_code=500, detail=exc.message) from exc


async def _release_ios_claim(node_id: str, device_id: str, udid: str) -> tuple[bool, str | None]:
    """让宿主节点丢掉对这台 iPhone 的认领（删凭据 + devices.json 条目）。

    best-effort，**绝不抛**：返回 ``(released, error_message)``。节点离线/RPC 失败
    返回 ``(False, 原因)``，由调用方决定是否继续（删除路径继续删行，只是把未释放
    的原因透出给用户，好让他上线后重新扫描并「重新配对」）。

    为什么要抽出来：``delete_resource`` 与 ``release_ios_device`` 都需要这一步。
    此前 ``delete_resource`` 只删服务端行、从不通知节点，节点侧凭据残留 → 扫描仍报
    ``claimed`` 而服务端已无行 → 「找不到该设备的资源 id」且无法重新接入（孤儿）。
    """
    from .node_client import get_node_client, NodeServerUnavailable, RPCError

    if not node_id or not udid:
        return False, "设备缺少节点绑定信息"
    try:
        await get_node_client().ios_release_device(node_id, device_id, udid, delete_credential=True)
        return True, None
    except NodeServerUnavailable as exc:
        return False, str(exc) or "节点离线"
    except RPCError as exc:
        return False, getattr(exc, "message", None) or str(exc)


async def _ios_row_is_stale(res: dict, udid: str) -> bool:
    """判断一条 iOS 资源行是否「陈旧」——节点侧已无该设备的认领。

    认领路径冲突检查的自愈依据：节点清单里该 UDID ``claimed=false``（节点不认它了）
    或清单里**根本没有**该 UDID（节点可达但没这台设备）→ 陈旧，可删行让本次认领继续。

    **保守**：节点不可达 / 读清单失败 → 返回 False（当作不陈旧，保持 409）——宁可拦，
    也不要在拿不到真相时误删一行好记录。
    """
    from .node_client import get_node_client, NodeServerUnavailable, RPCError

    ios_info = res.get("ios") if isinstance(res.get("ios"), dict) else {}
    node_id = str(ios_info.get("node_id") or "").strip()
    if not node_id or not udid:
        # 缺绑定信息：无法核对，保守当作不陈旧。
        return False
    try:
        inv = await get_node_client().get_ios_devices(node_id)
    except (NodeServerUnavailable, RPCError):
        return False
    for dev in (inv.get("devices") or []) if isinstance(inv, dict) else []:
        if str(dev.get("udid") or "").strip() == udid:
            # 节点知道这台设备：认领还在 → 不陈旧；认领没了 → 陈旧。
            return not bool(dev.get("claimed"))
    # 节点可达但清单里没有这台设备：行是陈旧的。
    return True


@router.post("/resources/{resource_id}/release-device")
async def release_ios_device(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """Release a claimed iOS device: dispatch release frame, stop the node-side goroutine."""
    resource = await _owned_resource(resource_id, user, resource_type="device")
    # _owned_resource → get_resource 返回扁平化 DTO（无 "data" 层）。
    data = resource
    ios_info = data.get("ios") or {}
    device_id = data.get("device_id") or ""
    udid = ios_info.get("udid") or ""
    node_id = ios_info.get("node_id") or ""

    if not node_id or not udid:
        raise HTTPException(
            status_code=400, detail="此设备不是 iOS 设备或缺少节点绑定信息"
        )

    # best-effort 释放（节点离线/RPC 失败不阻断后面的 revoke）。
    await _release_ios_claim(node_id, device_id, udid)

    # Revoke the device resource (same as the existing Android path)
    from server import builtin_tool_store

    await builtin_tool_store.revoke_device(resource_id)
    await _resync_builtin("device-control")
    return {"resource_id": resource_id, "device_id": device_id}


@router.post("/resources/{resource_id}/revoke-device")
async def revoke_device_resource(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """解除设备资源配对：清 token 并让在线连接收到 close 4003。"""
    await _owned_resource(resource_id, user, resource_type="device")
    import builtin_tool_store as store

    device = await store.revoke_device(resource_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    await _resync_builtin("device-control")
    return {"device": device}


# ── 设备远程控制命令（网页「查看设备」操作面板用）──────────────────────────────
# Android 与 iOS 走**同一条** device-control WS（spec v0，同一 driver 注册表）：
# Android 是 App 自己拨号，iOS 是宿主节点替配对的 iPhone 拨号，所以这里不分平台。
# 鉴权走会话 cookie + 资源归属；MCP 工具面（device_control_plugin）按 MCP token
# 反查设备是另一条独立路径，两者都汇到 driver.call。

class DeviceCommandReq(BaseModel):
    cmd: str
    args: dict[str, Any] = {}
    timeout_ms: int | None = None


# 设备报错码（spec §12）→ 用户可读的引导语。code 本身仍拼在消息前，便于排障。
_DEVICE_ERROR_HINTS = {
    "unsupported": "设备不支持该操作",
    "bad_args": "参数不合法",
    "stale_node": "屏幕内容已变化，请刷新屏幕后重试",
    "not_found": "目标不存在",
    "timeout": "设备响应超时",
    "cancelled": "已取消",
    "duplicate_request": "重复请求",
    "overloaded": "设备繁忙，请稍后重试",
    "permission_denied": "设备端权限未开启（如无障碍服务）",
    "not_ready": "设备端尚未就绪，请稍后重试",
    "device_error": "设备执行失败",
}


@router.post("/resources/{resource_id}/device-command")
async def device_command(resource_id: int, body: DeviceCommandReq, user: User = Depends(get_current_user)) -> dict:
    """向一台已配对的设备下发一条 device-control 命令并等结果。

    cmd 必须在协议词汇表（spec §8）内；能力集由设备 register 时声明，driver
    自己 gate（未声明的命令设备端根本不会收到）。截图类结果（get_screen_state
    的 screenshot.b64）base64 JPEG 原样透传，前端拼 data URL 显示。
    """
    from mcp_builtin.device_control import driver as dc_driver
    from mcp_builtin.device_control import protocol as proto

    resource = await _owned_resource(resource_id, user, resource_type="device")
    device_id = str(resource.get("device_id") or "")
    if not device_id:
        raise HTTPException(status_code=409, detail="设备尚未完成配对，无法下发命令")
    if body.cmd not in proto.COMMANDS:
        raise HTTPException(status_code=400, detail=f"不支持的命令：{body.cmd}")
    driver = dc_driver.get_driver()
    ctx = driver.get(device_id) if driver is not None else None
    if ctx is None or ctx.closed:
        raise HTTPException(status_code=409, detail="设备离线，连上服务端后才能操作")
    try:
        data = await ctx.call(body.cmd, body.args or {}, body.timeout_ms)
    except dc_driver.DeviceOffline:
        raise HTTPException(status_code=409, detail="设备连接已断开，请稍后重试") from None
    except proto.DeviceError as exc:
        hint = _DEVICE_ERROR_HINTS.get(exc.code, "设备执行失败")
        message = f"{hint}：{exc.message}" if exc.message else hint
        raise HTTPException(status_code=502, detail=f"[{exc.code}] {message}") from exc
    return {"cmd": body.cmd, "data": data}


# ── 通用资源 CRUD（动态 {resource_id}）——必须注册在所有静态子路径之后，
# 否则 FastAPI 会把 "cdp-clients"/"mail-services" 等当作 resource_id 解析而 422。
@router.get("/resources/{resource_id}")
async def get_resource(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """按 id 取一条工具资源详情，仅限本人名下；不存在或越权返回 404。"""
    return await _owned_resource(resource_id, user)


@router.patch("/resources/{resource_id}")
@router.put("/resources/{resource_id}")
async def update_resource(resource_id: int, body: UpdateResourceReq, user: User = Depends(get_current_user)) -> dict:
    """整体覆盖资源的 ``data`` 配置（PATCH/PUT 同路径，行为一致）。

    ``expected_revision`` 传入时做乐观锁：版本不匹配返回 409（说明配置已被他人改动）。
    写成功后重同步对应的内置 MCP 服务；资源不存在返回 404。
    """
    import builtin_tool_store as store
    existing = await _owned_resource(resource_id, user)
    if str(existing.get("resource_type") or "") == "mail_account" and "proxy_id" in body.data:
        await _validate_mail_proxy_id(body.data)
    try:
        row = await store.update_resource(
            resource_id, body.data, owner_user_id=str(user.id), expected_revision=body.expected_revision
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    await _resync_resource(str(existing.get("resource_type") or ""))
    return row


@router.delete("/resources/{resource_id}")
async def delete_resource(resource_id: int, user: User = Depends(get_current_user)) -> dict:
    """删除资源。iOS 设备「删除即释放」——先通知宿主节点丢掉认领（删凭据），再删行。

    此前只删服务端行、从不通知节点：节点 devices.json + 凭据残留，下次扫描仍报
    ``claimed``，而服务端已无行 → 设备既接不进来也初始化不了（「找不到该设备的
    资源 id」）。这里补上释放，让删除后能重新扫描接入。

    判据用 ``ios.udid`` **且** ``ios.node_id`` 非空（release 需要的正是这对），
    不用 ``platform=="ios"``——platform 只在 list_devices_resource 里算，且裸行
    可能带 platform 而无 udid。android/cdp/mail 三种类型完全走原路径。

    节点离线时**仍删行**，但返回 ``released:false`` + ``release_error``，前端据此
    告警（否则会再造一个孤儿）。
    """
    import builtin_tool_store as store

    existing = await _owned_resource(resource_id, user)  # 扁平化 DTO（无 "data" 层）
    resource_type = str(existing.get("resource_type") or "")

    released: bool | None = None
    release_error: str | None = None
    if resource_type == "device":
        ios_info = existing.get("ios") if isinstance(existing.get("ios"), dict) else {}
        udid = str(ios_info.get("udid") or "").strip()
        node_id = str(ios_info.get("node_id") or "").strip()
        if udid and node_id:
            released, release_error = await _release_ios_claim(
                node_id, str(existing.get("device_id") or ""), udid
            )

    deleted = await store.delete_resource(resource_id, owner_user_id=str(user.id))
    await _resync_resource(resource_type)
    return {"deleted": deleted, "released": released, "release_error": release_error}


# ── 外部 MCP 接入说明与 CDP connection info ───────────────────────────────────

_BUILTIN_MCP_SERVICE_NAMES = {"cdp": "cdp-bridge", "mail": "mail", "device": "device-control"}


@router.get("/mcp-integration/{tool_kind}")
async def mcp_integration_metadata(tool_kind: str, user: User = Depends(get_current_user)) -> dict:
    """返回内置工具（cdp/mail/device）对应的 MCP 服务的接入说明。

    包括服务名、显示名、SSE 地址（含带 token 的完整 URL）、暴露的工具列表与
    运行状态；tool_kind 非法返回 400，内置服务不存在返回 404。
    """
    import mcp_plugin_store
    from mcp_runtime.configuration import _manifest_for_service
    from mcp_runtime.sse_gateway import _tools_for_service

    service_name = _BUILTIN_MCP_SERVICE_NAMES.get(str(tool_kind).lower())
    if service_name is None:
        raise HTTPException(status_code=400, detail="不支持的内置工具类型")
    service = await mcp_plugin_store.get_service_by_name(service_name)
    if service is None:
        raise HTTPException(status_code=404, detail="内置 MCP 服务不存在")
    from mcp.api import _runtime_public_base
    base = _runtime_public_base()
    manifest = _manifest_for_service(service)
    try:
        tools = _tools_for_service(service_name)
    except Exception:
        tools = []
    return {
        "tool_kind": tool_kind,
        "service_name": service_name,
        "display_name": service.get("display_name") or manifest.get("display_name") or service_name,
        "description": service.get("description") or manifest.get("description") or "",
        "transport": "sse",
        "sse_url": f"{base}/mcp/{service_name}/sse",
        "token_url": f"{base}/mcp/{service_name}/sse?token=<TOKEN>",
        "tools": tools,
        "resources": [], "actions": [], "views": [],
        "runtime_status": service.get("runtime_status"),
        "enabled": bool(service.get("enabled", True)),
    }


@router.get("/cdp/connection-info")
async def cdp_connection_info(user: User = Depends(get_current_user)) -> dict:
    """返回 Chrome 扩展接入 CDP 桥所需的连接信息（WS 会话地址与扩展下载地址）。"""
    from mcp.api import _runtime_public_base
    base = _runtime_public_base()
    ws_base = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    return {
        "ws_session_url": f"{ws_base}/mcp/cdp-bridge/session",
        "extension_download_url": "/mcp/services/cdp-bridge/assets/chrome-extension/download",
        "extension_file_name": "cdp-bridge-extension.zip",
    }
