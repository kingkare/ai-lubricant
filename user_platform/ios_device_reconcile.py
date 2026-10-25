"""iOS 设备自动扫描对账（服务端定时任务，30s 一轮）。

## 为什么需要它

「添加 iOS 设备」= ``POST /resources/ios-claims``（``routes_builtin_tools.claim_ios_device``）
**一次做两件事**：① 建服务端资源行；② 给宿主节点签发凭据（认领）。这两半会脱钩：

- 节点重启/换版本/被释放后，节点侧 ``devices.json`` + 凭据可能没了，但服务端行还在；
- 服务端行还在 → 再次添加撞 409「已被认领」，而设备又因为节点侧无认领永远显示「离线」
  （``_merge_ios_inventory`` 曾按 device_id 匹配，未认领时 device_id 为空 → 匹配不上）。

本模块每 30s 对每个有 iOS 设备的节点做一次对账，把两边收敛到一致。节点自身每 30s
也会自报一次清单（``manager.ReportInterval``），服务端读的是那份缓存清单。

## 铁律：只在「节点明确说未认领」时删行

对账有三种判定（见 :func:`decide_reconcile`）：

- 节点清单里有该 UDID 且 ``claimed=true`` → 更新字段（正常）。
- 节点清单里有该 UDID 但 ``claimed=false`` → **删行**（脏状态：服务端有行、节点无认领）。
- 节点清单里**没有**该 UDID（拔线/扫不到）→ 保留行、标 ``present=false``（离线）。

「扫不到」**绝不删行**——节点重启/抖动瞬间清单可能不全，误删会把好设备干掉。只有节点
**明确**回 ``claimed=false`` 才是「这半边的添加没了」，才收敛。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .node_client import NodeClient, NodeServerUnavailable  # noqa: F401

logger = logging.getLogger(__name__)

#: 对账周期。节点自报也是 30s，同频即可（插上手机 ≤30s 自动在线）。
RECONCILE_INTERVAL_SECONDS = 30

# iOS 设备状态字段：以**节点 inventory** 为准（设备自报，持久且随重连重放），
# 而不是易失的 job 快照。前端据此渲染「初始化中 42% / 就绪 / 失败」，服务端重启
# 后依然可见——这正是「初始化后台化」的关键。
#
# present 是**在线轴**的唯一依据：节点能否通过 usbmuxd 枚举到这台手机。刻意不用
# device_control_online（守护 WS）判在线——守护要 WDA 装好之后才起来，用它当门禁
# 会让第一次初始化永远开始不了。
IOS_INVENTORY_FIELDS = (
    "wda_state",
    "wda_progress",
    "wda_stage",
    "profile_expires_at",
    "last_error",
    "present",
)

# proto wda_state → 前端要的 5 态。用户的模型是**两个正交轴**：
#   初始化状态：待初始化 / 初始化中 / 已初始化·待控制 / 控制就绪 / 初始化异常
#   在线状态：  在线 / 离线（present）
# INSTALLED（已初始化·待控制）= runner 已装、lockdown 级可装 ipa，但控制链路还没起
# （待信任/待起 runner）。READY（控制就绪）= 控制链路已连（节点 deviceControlOn）。
# RENEWAL_DUE 不是独立态，是「已初始化」上的一个到期角标；EXPIRED 也不是独立态，
# 而是 runner 起不来的原因之一（初始化轴不变，仍算已初始化）。在这里收敛一次，
# 前后端都读归一值，避免两边各写一套 switch 各自漂移。
_INIT_STATE_BY_WDA_STATE = {
    "unspecified": "uninitialized",
    "missing": "uninitialized",
    "preparing": "initializing",
    "installed": "installed",          # 已初始化，待控制
    "ready": "control_ready",          # 控制就绪
    "renewal_due": "installed",        # 已初始化 + 待续签角标
    "expired": "installed",            # 已初始化 + runner 起不来的原因（仍算已初始化）
    "failed": "failed",
}

# 「控制就绪」徽章只给真正 READY 的设备；renewal_due 虽也映射 installed，但它的
# 续签角标由 renewal_due 布尔位单独透出，这里不复用。
_CONTROL_READY_STATES = {"ready"}


def control_ready_from_wda_state(wda_state: object) -> bool:
    """控制轴是否就绪：仅 READY（设备控制链路已连）。

    INITIALIZED 只表示装好了，不代表能控——这是两轴分离的核心。
    """
    raw = str(wda_state or "").strip().upper()
    if raw.startswith("IOS_WDA_STATE_"):
        raw = raw[len("IOS_WDA_STATE_"):]
    return raw.lower() in _CONTROL_READY_STATES


def init_state_from_wda_state(wda_state: object) -> str:
    """把 proto 的 wda_state 归一成 5 态之一。未知值按「待初始化」处理。

    节点可能回 "IOS_WDA_STATE_READY"（proto 全名）或 "ready"（短名），两种都吃。
    """
    raw = str(wda_state or "").strip().upper()
    if raw.startswith("IOS_WDA_STATE_"):
        raw = raw[len("IOS_WDA_STATE_"):]
    return _INIT_STATE_BY_WDA_STATE.get(raw.lower(), "uninitialized")


def apply_inventory_to_ios_block(ios_block: dict, entry: dict) -> dict:
    """把节点清单条目里已知字段合并进 ``ios`` 块（就地改并返回）。

    - 空值不覆盖：节点刚重连、清单还没填全时，别把已知状态抹掉。
    - ``present`` 是 bool，``False`` 是**有效值**（手机被拔了），必须落下去，
      所以它单独判 None 而不是判 falsy。
    - 归一化 ``init_state`` + 到期角标 + ``control_ready``（控制轴），一次算好
      给前端直接用。
    """
    for field in IOS_INVENTORY_FIELDS:
        if field not in entry:
            continue
        value = entry.get(field)
        if value is None:
            continue
        if field != "present" and value == "":
            continue
        ios_block[field] = value
    if "wda_state" in ios_block:
        ios_block["init_state"] = init_state_from_wda_state(ios_block.get("wda_state"))
        ios_block["renewal_due"] = (
            str(ios_block.get("wda_state") or "").strip().upper().endswith("RENEWAL_DUE")
        )
        ios_block["control_ready"] = control_ready_from_wda_state(ios_block.get("wda_state"))
    return ios_block


def decide_reconcile(entry: dict | None) -> str:
    """纯函数：给定节点清单里这台设备的条目，决定怎么处置服务端行。

    - ``None``（清单里没有：拔线/扫不到）→ ``"offline"``（保留行、标 present=false）。
    - 有且 ``claimed`` 为假 → ``"delete"``（脏状态：服务端有行、节点无认领）。
    - 有且 ``claimed`` 为真 → ``"update"``（正常，合并字段）。
    """
    if entry is None:
        return "offline"
    if not entry.get("claimed"):
        return "delete"
    return "update"


def _index_inventory(devices: list[dict]) -> dict[str, dict]:
    """节点清单 → {udid: entry}。udid 是稳定身份（认领与否都在）。"""
    out: dict[str, dict] = {}
    for d in devices or []:
        udid = str(d.get("udid") or "").strip()
        if udid:
            out[udid] = d
    return out


async def reconcile_once(client: NodeClient) -> dict[str, int]:
    """跑一轮对账，返回 ``{"updated", "deleted", "offline", "skipped_nodes"}``。

    按行里出现过的 ``ios.node_id`` 分组（不遍历全平台节点），每节点读一次清单
    （避免 N+1）。节点不可达 → 整节点跳过（节点离线不代表设备没了，绝不因此删）。
    """
    import builtin_tool_store

    resources = await builtin_tool_store.list_resources(
        owner_user_id=None, resource_type="device"
    )
    # node_id -> [row]，只挑有 ios.node_id 的行。
    by_node: dict[str, list[dict]] = {}
    for r in resources or []:
        ios_block = r.get("ios") if isinstance(r.get("ios"), dict) else None
        if not ios_block:
            continue
        node_id = str(ios_block.get("node_id") or "").strip()
        if not node_id:
            continue
        by_node.setdefault(node_id, []).append(r)

    stats = {"updated": 0, "deleted": 0, "offline": 0, "skipped_nodes": 0}

    for node_id, rows in by_node.items():
        try:
            inv = await client.get_ios_devices(node_id)
        except Exception as exc:  # noqa: BLE001 — 节点离线/不可达：整节点跳过
            logger.debug("[ios-reconcile] inventory unavailable for %s: %s", node_id, exc)
            stats["skipped_nodes"] += 1
            continue
        entries = _index_inventory(inv.get("devices") if isinstance(inv, dict) else [])

        for row in rows:
            row_id = row.get("id")
            if row_id is None:
                continue
            ios_block = dict(row.get("ios") or {})
            udid = str(ios_block.get("udid") or "").strip()
            entry = entries.get(udid) if udid else None
            decision = decide_reconcile(entry)

            try:
                if decision == "delete":
                    # 脏状态：服务端有行、节点无认领。删行让用户重新添加。
                    # 顺带通知节点释放（若有残留）——best-effort。
                    await _release_if_claimed(client, node_id, row, udid)
                    await builtin_tool_store.delete_resource(int(row_id))
                    stats["deleted"] += 1
                    logger.info(
                        "[ios-reconcile] deleted stale row id=%s udid=%s (node has no claim)",
                        row_id, udid,
                    )
                elif decision == "update":
                    apply_inventory_to_ios_block(ios_block, entry)
                    await builtin_tool_store.update_resource(int(row_id), {"ios": ios_block})
                    stats["updated"] += 1
                    # 插上手机自动续签：对账 30s 一轮，设备 present 且在到期窗口
                    # （renewal_due / expired）里立即派发 renew，不等 6h 扫描器。
                    # 12h 防风暴闸在 renew_on_connect 内，失败也不重投。
                    if ios_block.get("present") is True:
                        try:
                            from .ios_auto_renew import renew_on_connect

                            if await renew_on_connect(row, ios_block):
                                stats["renew_triggered"] = stats.get("renew_triggered", 0) + 1
                        except Exception:  # noqa: BLE001 — 续签派发失败不影响对账
                            logger.debug(
                                "[ios-reconcile] connect renew skipped row id=%s",
                                row_id,
                                exc_info=True,
                            )
                else:  # offline
                    if ios_block.get("present") is not False:
                        ios_block["present"] = False
                        await builtin_tool_store.update_resource(int(row_id), {"ios": ios_block})
                    stats["offline"] += 1
            except Exception:  # noqa: BLE001 — 单行失败不影响其余
                logger.warning(
                    "[ios-reconcile] row id=%s reconcile failed", row_id, exc_info=True
                )

    if stats["updated"] or stats["deleted"]:
        logger.info("[ios-reconcile] round done: %s", stats)
    return stats


async def _release_if_claimed(
    client: NodeClient, node_id: str, row: dict, udid: str
) -> None:
    """删行前顺带让节点释放（best-effort，忽略失败）。

    这里节点已报 ``claimed=false``，通常无需释放；但若行里还留着 device_id
    （节点侧状态与行不同步的边角），补一刀无害。
    """
    device_id = str(row.get("device_id") or "").strip()
    if not udid:
        return
    try:
        await client.ios_release_device(node_id, device_id, udid, delete_credential=True)
    except Exception:  # noqa: BLE001 — 释放失败不影响删行
        pass


async def sync_loop() -> None:
    """后台循环：每 30s 一轮。仿 ios_auto_renew.sync_loop 结构。"""
    from .config import settings

    if not settings.enabled:
        logger.info("[ios-reconcile] user-platform disabled; scanner idle")
        return
    logger.info("[ios-reconcile] scanner started (interval=%ds)", RECONCILE_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)
            from .node_client import get_node_client

            await reconcile_once(get_node_client())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 单轮异常绝不能让扫描器退出
            logger.exception("[ios-reconcile] round failed; retry in 5s")
            await asyncio.sleep(5)
