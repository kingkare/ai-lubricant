"""CDP 身份验证器的**服务端内存 + 推送**（订阅 → 立即回内存快照 → 每次翻转刷新内存再推）。

设计口径（用户定稿）：

- **服务端内存**保存每个 owner 的列表：每条 ``meta + code + expiresAt``（**绝对失效
  时刻，epoch 秒**——不是「剩余秒数」）。内存只存这些，不需要更多。
- ``subscribe(adapter, owner, domain)``：登记订阅后**立即**把内存里的这份数据推给该
  WS 连接（内存冷时先刷一次），客户端不用等下一次翻转才见数。
- 每个 owner 一条 asyncio 任务：睡到「最早失效点 + 一小段」醒来，**先刷新内存、再
  推**给该 owner 的全部订阅者。
- **订阅回帧与推送携带同一份时间**：两者都读同一份内存（同一 ``expiresAt``）；刷新是
  「先写内存、后推送」，所以任何时刻客户端看到的失效时刻都自洽。
- ``drop_adapter(adapter)``：WS 断连清订阅（ext_session 的 finally 调）。owner 无订阅
  者时停掉它的任务并清内存。

为什么用绝对失效时刻而不是剩余秒数：剩余秒要客户端从「收到那刻」自己倒计时，网络
延迟 + 两端时钟不一致会越读越偏；绝对时刻让客户端直接算 ``expiresAt - now``，且订阅
回帧与后续推送对同一期必然给出**同一个** ``expiresAt``。

推帧形态（无 reqId，走 background 的广播路径）::

    {"type": "totp_push", "entries": [{"id", "code", "expiresAt", ...meta}]}
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from loguru import logger

# 订阅表：adapter(=WS 连接) -> {"owner", "domain"}
_SUBS: dict[Any, dict] = {}
# 服务端内存：owner -> {"entries": [ {..meta, code, expiresAt} ]}（domain="" 的全量）
_MEM: dict[str, dict] = {}
# 每个 owner 一条刷新+推送任务
_PUMPS: dict[str, asyncio.Task] = {}
# 翻转点之后多久醒来（秒）：太早会推上一期的码
_TICK_LEAD = 0.15


def _send(adapter: Any, payload: dict) -> None:
    try:
        adapter.post_message(json.dumps(payload, ensure_ascii=False))
    except Exception:
        # 连接已断：drop_adapter 由断连钩子清理，这里静默。
        pass


def _filter(entries: list[dict], domain: str) -> list[dict]:
    """按订阅的 domain 过滤内存全量列表（空 = 全部）。复用存储层的匹配口径。"""
    if not domain:
        return entries
    import totp_store

    return [e for e in entries if totp_store.host_matches_domains(domain, e.get("domains") or [])]


def _min_expires(entries: list[dict]) -> float | None:
    exps = [float(e["expiresAt"]) for e in entries if e.get("expiresAt")]
    return min(exps) if exps else None


async def _refresh(owner: str) -> list[dict]:
    """刷新某 owner 的内存（先写内存，调用方随后推送）。返回新列表。"""
    import totp_store

    entries = await totp_store.list_with_codes(owner)
    _MEM[owner] = {"entries": entries}
    return entries


def _snapshot(owner: str, domain: str) -> list[dict]:
    """读内存并按 domain 过滤（订阅回帧与推送共用同一份内存 → 同一 expiresAt）。"""
    mem = _MEM.get(owner)
    entries = mem["entries"] if mem else []
    return _filter(entries, domain)


def _push_owner(owner: str, entries: list[dict]) -> None:
    """把一份内存列表按各订阅者的 domain 过滤后各推一份。"""
    for adapter, sub in list(_SUBS.items()):
        if sub.get("owner") != owner:
            continue
        _send(adapter, {"type": "totp_push", "entries": _filter(entries, sub.get("domain") or "")})


async def _pump(owner: str) -> None:
    """owner 级刷新循环：睡到最早失效点 → 刷新内存 → 推给该 owner 全部订阅者。"""
    while True:
        entries = _MEM.get(owner, {}).get("entries") or []
        nxt = _min_expires(entries)
        if nxt is None:
            await asyncio.sleep(30.0)  # 空表：一个周期后再看（别 1s 空转）
        else:
            delay = max(1.0, nxt - time.time() + _TICK_LEAD)
            await asyncio.sleep(delay)
        # 期间可能被 invalidate 清了内存：重读，若空说明有人会主动刷（见 invalidate）。
        entries = _MEM.get(owner, {}).get("entries") or []
        if not entries:
            continue
        try:
            entries = await _refresh(owner)
        except Exception:
            logger.exception("[totp-push] refresh failed, retry in 5s")
            await asyncio.sleep(5)
            continue
        _push_owner(owner, entries)


def _ensure_pump(owner: str) -> None:
    task = _PUMPS.get(owner)
    if task is None or task.done():
        _PUMPS[owner] = asyncio.get_running_loop().create_task(_pump(owner))


async def subscribe(adapter: Any, owner: str, domain: str = "") -> list[dict]:
    """登记订阅 → **立即**推一份内存快照 → 确保 owner 级刷新任务在跑。返回该快照。

    重复订阅同一连接 = 覆盖（换页签/换域名场景）。内存冷（该 owner 从没刷过）时先刷
    一次再推——这样「首次进入」也只需一次 DB 读（单查询），且与后续推送同源同时间。
    """
    unsubscribe(adapter)
    domain = domain or ""
    _SUBS[adapter] = {"owner": owner, "domain": domain}
    if owner not in _MEM:
        try:
            await _refresh(owner)
        except Exception:
            logger.exception("[totp-push] initial refresh failed")
    snapshot = _snapshot(owner, domain)
    _send(adapter, {"type": "totp_push", "entries": snapshot})
    _ensure_pump(owner)
    return snapshot


def unsubscribe(adapter: Any) -> None:
    sub = _SUBS.pop(adapter, None)
    if not sub:
        return
    owner = sub.get("owner")
    # 该 owner 再没订阅者：停任务、清内存（下次订阅冷启动重刷）。
    if owner and not any(s.get("owner") == owner for s in _SUBS.values()):
        task = _PUMPS.pop(owner, None)
        if task and not task.done():
            task.cancel()
        _MEM.pop(owner, None)


def drop_adapter(adapter: Any) -> None:
    """WS 断连清理（ext_session finally 调）。"""
    unsubscribe(adapter)


async def invalidate(owner: str) -> None:
    """增删改后：**先刷新内存、再推**给该 owner 的全部订阅者（由帧层在 save/delete 后调）。

    没有订阅者时只丢内存（下次订阅冷启动重刷），不起推送。这样「增删改 → 所有开着的
    popup 立刻看到新列表」，而不是干等下一次翻转。
    """
    if not any(s.get("owner") == owner for s in _SUBS.values()):
        _MEM.pop(owner, None)
        return
    try:
        entries = await _refresh(owner)
    except Exception:
        logger.exception("[totp-push] invalidate refresh failed")
        _MEM.pop(owner, None)
        return
    _push_owner(owner, entries)


def active_count() -> int:
    return len(_SUBS)
