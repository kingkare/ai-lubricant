"""WDA 自动续签扫描器（任务 A2）。

每 6h 跑一轮：扫所有认领了 iOS 设备的资源，对到期窗口内（默认 14 天前）
的设备，校验节点在线 + wda_state 为 ready/renewal_due，派发 renew job。
失败可重试的（下载/签名临时故障）下一轮自然重试；不可重试的（ASC key 被删
等终端错误）发用户通知。

护栏：``data.ios.last_auto_renew_at`` PG 时间戳距今 < 12h 跳过（防失败风暴）；
``last_renew_job_id`` 的 snapshot 还在 running 也跳过（幂等）。snapshot 在
node_server 内存、重启即丢——所以主护栏靠 PG 时间戳，snapshot 只辅助。

本模块只做派发 + 回写；到期判定（READY→RENEWAL_DUE/EXPIRED）在节点
manager.go 的周期 Rescan 里做（A3），扫描器据此读 inventory 的 wda_state。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .node_client import NodeClient, get_node_client
from .notify_core import emit_notification

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 6 * 3600  # 6h
RENEW_GUARD_SECONDS = 12 * 3600  # 12h anti-storm
DEFAULT_RENEW_BEFORE_DAYS = 14

IOS_HOST_ROLE = "ios_host"


def _parse_iso(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        # 兼容 RFC3339 带纳秒/Z；fromisoformat 解析失败就 None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _needs_renew(expires_at: str, renew_before_days: int, now: datetime) -> bool:
    """到期判定：now + renew_before_days 天 > expires_at 即进续签窗口。

    已过期（now > exp）也算需要续签——节点侧 wdaState 会是 EXPIRED，但扫描器
    只在 ready/renewal_due 时派发（expired 走通知不自动重试，见 _scan_once）。
    """
    exp = _parse_iso(expires_at)
    if exp is None:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window_days = int(renew_before_days or DEFAULT_RENEW_BEFORE_DAYS) or DEFAULT_RENEW_BEFORE_DAYS
    return now + timedelta(days=window_days) > exp


async def _device_inventory_state(
    client: NodeClient, node_id: str, udid: str
) -> dict[str, Any] | None:
    """读节点 inventory 里该 UDID 的实时 wda_state / profile_expires_at。

    wda_state 归一化为短名（"ready"/"renewal_due"/"expired"/…）：proto 枚举经
    Connect JSON 是 "IOS_WDA_STATE_READY" 这样的全名，裁掉前缀再小写。
    """
    try:
        rep = await client.get_ios_devices(node_id)
    except Exception as exc:  # noqa: BLE001 — 节点离线/不可达本轮跳过
        logger.debug("[ios-auto-renew] inventory fetch failed node=%s: %s", node_id, exc)
        return None
    for d in (rep.get("devices") or []) if isinstance(rep, dict) else []:
        if (d.get("udid") or "") == udid:
            state = str(d.get("wda_state") or "")
            if state.startswith("IOS_WDA_STATE_"):
                state = state[len("IOS_WDA_STATE_"):]
            d = dict(d)
            d["wda_state"] = state.lower()
            return d
    return None


async def _dispatch_renew(
    resource: dict, ios_info: dict, *, signing_profile_id: int
) -> str | None:
    """派发一次 renew job，回写 last_renew_job_id + last_auto_renew_at。

    复用 renew_wda 的派发逻辑（artifact=None 复用设备已装产物）。owner 从
    resource 取（扫描器是平台行为，但派发要带 owner 鉴权取 signing profile）。
    """
    from .routes_ios import WdaJobRequest, renew_wda
    from .models import User  # noqa: F401 — 仅用于类型

    resource_id = int(resource.get("id") or 0)
    owner_user_id = str(resource.get("owner_user_id") or "")
    if not resource_id or not owner_user_id:
        return None

    # 构造一个最小 user 对象满足 renew_wda 的 Depends 签名（只用 id 字段）
    class _ScopedUser:
        id = owner_user_id
        role = "user"

    body = WdaJobRequest(
        # list_resources 返回扁平化 DTO（无 "data" 层）——此前读 resource["data"]
        # 恒为 {}，device_id 恒空 → renew_wda 400「设备缺少节点绑定信息」，
        # 自动续签每次都派发失败。直接读顶层。
        device_id=resource.get("device_id", "") or "",
        action="renew",
        signing_profile_id=signing_profile_id,
    )
    try:
        result = await renew_wda(resource_id, body, user=_ScopedUser())  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ios-auto-renew] renew dispatch failed resource=%s: %s", resource_id, exc
        )
        return None
    job_id = str(result.get("job_id") or "")
    if not job_id:
        return None

    # 回写 last_renew_job_id + last_auto_renew_at（best-effort）
    import builtin_tool_store

    payload = dict(ios_info)
    payload["last_renew_job_id"] = job_id
    payload["last_auto_renew_at"] = datetime.now(timezone.utc).isoformat()
    try:
        await builtin_tool_store.update_resource(resource_id, {"ios": payload})
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ios-auto-renew] write back last_renew_job_id failed resource=%s",
            resource_id,
            exc_info=True,
        )
    return job_id


#: 设备在线时允许「插上即续」的状态：到期窗口 + 已过期。过期后插线也该自动救
#: 回来，不等 6h 扫描、更不该只发通知让用户手动点（用户的诉求就是「每次连接
#: 自动签名」）。
_CONNECT_RENEW_STATES = ("renewal_due", "expired")


def _short_wda_state(value: object) -> str:
    """proto 全名 "IOS_WDA_STATE_READY" / 短名 "ready" → 小写短名。"""
    raw = str(value or "").strip()
    if raw.startswith("IOS_WDA_STATE_"):
        raw = raw[len("IOS_WDA_STATE_"):]
    return raw.lower()


async def renew_on_connect(
    resource: dict, ios_info: dict, *, now: datetime | None = None
) -> bool:
    """设备在线且签名到期/已过期 → 立即派发续签。返回是否派发成功。

    由对账循环（每 30s）在设备 present 时调用——手机一插上就把续签跑掉，不
    等 6h 扫描器。与扫描器共享同一道 12h 防风暴闸（``last_auto_renew_at``），
    两条路径不会重复派发。

    **预占闸**：先写 ``last_auto_renew_at`` 再派发。派发失败（会话过期/网络抖动/
    Apple 限流）时不会每 30s 重投——否则失败会变成对 Apple 的高频轰炸，正撞
    风控（-80045）。失败留给下一轮 12h 后或 6h 扫描器兜底。
    """
    import builtin_tool_store

    now = now or datetime.now(timezone.utc)
    if ios_info.get("auto_renew") is False:
        return False
    signing_profile_id = ios_info.get("signing_profile_id")
    if not signing_profile_id:
        return False
    if ios_info.get("present") is not True:
        return False
    if _short_wda_state(ios_info.get("wda_state")) not in _CONNECT_RENEW_STATES:
        return False
    last_at = _parse_iso(ios_info.get("last_auto_renew_at") or "")
    if last_at is not None and (now - last_at).total_seconds() < RENEW_GUARD_SECONDS:
        return False
    try:
        resource_id = int(resource.get("id") or 0)
    except (TypeError, ValueError):
        return False
    if not resource_id:
        return False

    payload = dict(ios_info)
    payload["last_auto_renew_at"] = now.isoformat()
    try:
        await builtin_tool_store.update_resource(resource_id, {"ios": payload})
    except Exception:  # noqa: BLE001 — 预占闸写失败就不派发（防风暴优先）
        logger.warning(
            "[ios-auto-renew] connect pre-claim write failed resource=%s",
            resource_id,
            exc_info=True,
        )
        return False

    job_id = await _dispatch_renew(
        resource, payload, signing_profile_id=int(signing_profile_id)
    )
    if job_id:
        logger.info(
            "[ios-auto-renew] connect-triggered renew job=%s resource=%s udid=%s state=%s",
            job_id, resource_id, ios_info.get("udid"), _short_wda_state(ios_info.get("wda_state")),
        )
    return bool(job_id)


async def _notify_failure(
    resource: dict, ios_info: dict, *, error_code: str, message: str, retryable: bool
) -> None:
    """不可重试的续签失败发用户通知；可重试的不打扰（下一轮自然重试）。"""
    if retryable:
        return
    owner_user_id = str(resource.get("owner_user_id") or "")
    # get_resource 返回扁平化 DTO（无 "data" 层）。
    device_name = resource.get("name") or ios_info.get("udid") or "设备"
    await emit_notification(
        "ios.wda.auto_renew_failed",
        params={
            "resource_id": resource.get("id"),
            "udid": ios_info.get("udid"),
            "error_code": error_code,
            "device_name": device_name,
        },
        owner_type="user",
        owner_id=owner_user_id or None,
        severity="error",
        kind="ios",
        source="ios-auto-renew",
        title="WDA 自动续签失败",
        message=f"iOS 设备 {device_name} WDA 自动续签失败：{error_code}。请检查签名配置。",
        detail=message,
        dedupe_key=f"ios-wda-auto-renew-failed:{resource.get('id')}",
        dedupe_window_seconds=RENEW_GUARD_SECONDS,
    )


async def _sync_completed_result(
    client: NodeClient, resource: dict, ios_info: dict
) -> None:
    """若 last_renew_job_id 的 snapshot 已完成，把 profile_expires_at 回写进
    device.data.ios（供下一轮判定 + 前端徽章）。"""
    import builtin_tool_store

    job_id = (ios_info.get("last_renew_job_id") or "").strip()
    node_id = (ios_info.get("node_id") or "").strip()
    if not job_id or not node_id:
        return
    try:
        snap = await client.get_ios_wda_job_status(node_id, job_id)
    except Exception:  # noqa: BLE001 — 节点离线/丢 snapshot 跳过
        return
    if not isinstance(snap, dict) or snap.get("status") != "completed":
        return
    new_exp = (snap.get("profile_expires_at") or "").strip()
    if not new_exp or new_exp == (ios_info.get("wda_profile_expires_at") or ""):
        return
    payload = dict(ios_info)
    payload["wda_profile_expires_at"] = new_exp
    try:
        await builtin_tool_store.update_resource(int(resource["id"]), {"ios": payload})
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ios-auto-renew] write back profile_expires_at failed resource=%s",
            resource.get("id"),
            exc_info=True,
        )


async def _scan_once(client: NodeClient) -> int:
    """跑一轮扫描，返回派发的 renew job 数。"""
    import builtin_tool_store
    from server import ios_store

    now = datetime.now(timezone.utc)
    resources = await builtin_tool_store.list_resources(
        owner_user_id=None, resource_type="device"
    )
    dispatched = 0
    for res in resources or []:
        # list_resources 返回扁平化 DTO（无 "data" 层）。此前读 res["data"] 恒为
        # {}，扫描器永远看不到任何设备 → 自动续签从不派发。
        data = res
        ios_info = data.get("ios") or {}
        if not isinstance(ios_info, dict):
            continue
        signing_profile_id = ios_info.get("signing_profile_id")
        # signing_profile_id 是唯一的硬前置（续期要用哪套配置）。到期时间**不**在这里
        # 当门禁——prepare 从不落 profile_expires_at，把它当硬前置会让扫描器对每台
        # 设备第一行就 continue，自动续签永不触发（线上现象：「续签不自动进行」）。
        # 到期时间改从节点 inventory 现读（见下），读到再判定 + 回写。
        if not signing_profile_id:
            continue
        if ios_info.get("auto_renew") is False:
            continue
        renew_before_days = int(ios_info.get("renew_before_days") or DEFAULT_RENEW_BEFORE_DAYS)

        node_id = (ios_info.get("node_id") or "").strip()
        udid = (ios_info.get("udid") or "").strip()
        if not node_id or not udid:
            continue

        # 节点 inventory 是 wda_state / profile_expires_at 的**真相源**（设备自报、
        # 持久）。先读它，既拿在线/状态，也拿到到期时间。
        inv = await _device_inventory_state(client, node_id, udid)
        if inv is None:
            # 节点离线/不可达，本轮跳过（不通知——下一轮节点回来再说）
            continue

        # 到期时间：inventory 现读优先；落库的 wda_profile_expires_at 兜底。读到
        # 就回写（前端徽章 + 下一轮判定都靠它）——这正是此前缺失的一环。
        inv_exp = str(inv.get("profile_expires_at") or "").strip()
        expires_at = inv_exp or ios_info.get("wda_profile_expires_at") or ""
        if inv_exp and inv_exp != (ios_info.get("wda_profile_expires_at") or ""):
            payload = dict(ios_info)
            payload["wda_profile_expires_at"] = inv_exp
            try:
                await builtin_tool_store.update_resource(int(res["id"]), {"ios": payload})
                ios_info = payload
            except Exception:  # noqa: BLE001 — 回写失败不影响本轮判定
                pass

        # 上一轮 renew 完成的 profile_expires_at 顺带回写（snapshot 路径）。
        await _sync_completed_result(client, res, ios_info)

        if not expires_at:
            # 设备还没上报描述文件到期（刚装/刚认领），本轮跳过。
            continue
        if not _needs_renew(expires_at, renew_before_days, now):
            continue
        # 防风暴：12h 内派过跳过
        last_at = _parse_iso(ios_info.get("last_auto_renew_at") or "")
        if last_at is not None and (now - last_at).total_seconds() < RENEW_GUARD_SECONDS:
            continue

        # 设备不在线（手机被拔/枚举不到）：本轮跳过，不派发。续期和初始化一样
        # 需要手机插着；离线时 renew_wda 的在线门禁也会 409，这里先跳过省一次
        # 必然失败的往返。插回来之后下一轮自然接上。
        if not inv.get("present"):
            logger.debug(
                "[ios-auto-renew] device offline, skipping resource=%s udid=%s",
                res.get("id"), udid,
            )
            continue
        wda_state = (inv.get("wda_state") or "").lower()
        # 只在 ready/renewal_due 自动续；expired 走失败通知不自动重试
        if wda_state not in ("ready", "renewal_due", ""):
            if wda_state == "expired":
                await _notify_failure(
                    res, ios_info,
                    error_code="profile_expired",
                    message="profile 已过期且自动续签未在过期前完成",
                    retryable=False,
                )
            continue

        # 校验 signing profile 还在（ASC key 可能被删）
        try:
            profile = await ios_store.get_signing_profile_secret(
                int(signing_profile_id), str(res.get("owner_user_id") or "")
            )
        except Exception:  # noqa: BLE001
            profile = None
        if not profile:
            await _notify_failure(
                res, ios_info,
                error_code="signing_profile_missing",
                message=f"签名配置 {signing_profile_id} 不存在或无权访问",
                retryable=False,
            )
            continue

        job_id = await _dispatch_renew(
            res, ios_info, signing_profile_id=int(signing_profile_id)
        )
        if job_id:
            dispatched += 1
            logger.info(
                "[ios-auto-renew] dispatched renew job=%s resource=%s udid=%s",
                job_id, res.get("id"), udid,
            )
    return dispatched


async def sync_loop() -> None:
    """后台循环：每 6h 一轮。仿 server/admin.py _auth_scanner_loop 结构。"""
    from .config import settings

    if not settings.enabled:
        logger.info("[ios-auto-renew] user-platform disabled; scanner idle")
        return
    logger.info("[ios-auto-renew] scanner started (interval=%ds)", SCAN_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            client = get_node_client()
            n = await _scan_once(client)
            if n:
                logger.info("[ios-auto-renew] scan dispatched %d renew job(s)", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 扫描器绝不能因为单轮异常退出
            logger.exception("[ios-auto-renew] scan round failed; retry in 5s")
            await asyncio.sleep(5)
