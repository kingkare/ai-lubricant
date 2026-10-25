"""环境会话的后台同步 worker。

会话的**显示副本**（元信息 + 内容尾部）平时靠「同步数据」按钮刷新，但那样只有
用户点开环境页才会更新——列表里「最后活动时间」和详情里的对话都会越来越旧。这个
worker 替所有环境跑对账，让副本自己追上节点。

与 :mod:`user_platform.notify_core` 的 outbox worker 同款形态：``main.py`` lifespan
里 ``start()`` 一次，进程内单例，退出时 ``stop()``。**没有 cron、没有配置表**——
同步只要"跟上"就够，不需要 cron 的时间语义。

**间隔按环境单独配**（环境详情页概览 tab 改，写 ``mc_task_environments``）：
``settings.env_session_sync_interval``（环境变量 ``AI_LUBRICANT_ENV_SESSION_SYNC_INTERVAL``）
是**全局默认值**，只给没有单独配的环境兜底；每个环境的 ``session_sync_interval``
是 ``NULL``（跟随默认）/ ``0``（本环境关闭）/ ``>0``（本环境专用秒数）。循环本身
按固定节拍（``_TICK_SECONDS``）醒来看一眼"谁到点了"，所以配 60 秒的环境不会被
默认 300 秒拖慢。全局默认 ≤0 时 worker 整个不开（纯网关部署）。

与手动「同步数据」的差别（刻意的）：

* **不下发资源**。资源下发是 revision 驱动的、属于派发时的事（``sync_environment``
  会真装 skill/plugin），定时跑没有意义还可能打断正在跑的任务。这里只对账 + 取内容。
* **内容只导活跃会话**。每个会话都拉一遍尾部会把节点压满，而多数会话是旧的、早就
  拉过了；只导「还在跑」和「刚结束不久」的，剩下的等用户点开详情时按需补。
* **不碰 ``last_synced_at``**。那是用户手动「同步数据」的时间（概览 tab 显示的），
  后台对账不该把它刷成"刚刚同步"，看起来像用户自己刚按过。

节点离线、数据库池没起来都是**运行常态**，一律跳过记日志、下轮再试；任何异常都不
能杀死循环（否则一次抖动就永久停更，比不跑还难查）。
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

# 进程内单例：start 幂等，重复调用不产生第二个循环。
_task: asyncio.Task | None = None
_stop = False

# 每个环境上次跑完的时刻（``time.monotonic()``），用来判断它"到点没有"。
# 刻意不进库：它是 worker 的调度节奏，不是用户操作的时间——落库就会被概览 tab
# 当成「用户刚同步过」显示出来。进程重启后清零，等价于每个环境重新等一个间隔，
# 这正是想要的（不重启的话副本本来就还新鲜）。
_last_run: dict[str, float] = {}

# 循环醒来检查「谁到点」的节拍。每轮只多一次读环境行的查询，很便宜；换来的好处
# 是单环境的间隔可以配得比全局默认更短。
_TICK_SECONDS = 30
_TICK_MIN = 5
_TICK_MAX = 60

# 内容导入的并发上限：串行会把一轮拖成分钟级，全放开又会把节点控制面压满。
# 与 sync_environment_sessions 里那段 import 用同一个数，口径一致。
_IMPORT_CONCURRENCY = 4

# 「刚结束不久」的判定：会话的最后活动时间在这个倍数 × 该环境实际间隔之内，
# 就再抓一次内容。一次间隔的余量保证「跑完最后一轮就结束」的会话也能被带走，
# 不留一条永远差一截的尾巴。
_RECENT_INTERVALS = 2


def _effective_interval(env_interval: int | None, default_interval: int) -> int:
    """这个环境实际该用多少秒间隔。

    ``NULL`` = 跟随全局默认；``0`` = 本环境关闭；``>0`` = 本环境专用。
    负数（不该存在——DB 允许但非法）按 0 处理：宁可不同步也不倒着重试。
    """
    value = int(default_interval) if env_interval is None else int(env_interval)
    return max(0, value)


async def start() -> None:
    """启动后台循环（幂等）。全局默认 ≤0 时不启动——纯网关部署不开这个 worker。"""
    global _task, _stop
    from .config import settings

    interval = int(getattr(settings, "env_session_sync_interval", 0) or 0)
    if interval <= 0:
        logger.debug("[env-sync] disabled (global interval<=0); worker not started")
        return
    if _task is not None and not _task.done():
        return  # already running
    _stop = False
    _last_run.clear()
    _task = asyncio.create_task(_loop(interval), name="env-session-sync-worker")
    logger.info("[env-sync] session sync worker started, default interval={}s", interval)


async def stop() -> None:
    """停循环并清状态，让之后的 start() 能干净重启（测试/热重载用）。"""
    global _task, _stop
    _stop = True
    task, _task = _task, None
    _last_run.clear()
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def _loop(default_interval: int) -> None:
    """按固定节拍检查"谁到点"；单轮的任何异常都不许逃出去。"""
    tick = max(_TICK_MIN, min(_TICK_MAX, min(default_interval or _TICK_SECONDS, _TICK_SECONDS)))
    while not _stop:
        try:
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            break
        if _stop:
            break
        try:
            await _run_once(default_interval)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — 一轮失败不能停掉整个循环
            logger.exception("[env-sync] a sync round failed; will retry next tick")


async def _run_once(default_interval: int) -> None:
    """对每个**到点**的环境：元信息对账 → 导活跃会话的内容尾部。

    ``default_interval`` 是全局默认（给 ``session_sync_interval`` 为 NULL 的环境兜底）；
    每个环境用自己的 ``session_sync_interval`` 覆盖。没到点的环境这一轮直接跳过。
    """
    from .models_environment import TaskEnvironment
    from .environment_service import (
        import_env_session_items,
        sync_environment_sessions,
    )
    from db import PostgresClient

    if not PostgresClient.pool:
        logger.debug("[env-sync] no db pool; skipping round")
        return

    # 只挑还能跑的环境：禁用的（enabled=false）不碰，没节点的没得同步。
    envs = await TaskEnvironment.filter(enabled=True).exclude(node_id="")
    if not envs:
        return

    now = time.monotonic()
    seen_ids: set[str] = set()
    synced = 0
    for env in envs:
        if _stop:
            break
        user_id = str(env.user_id)
        env_id = str(env.id)
        seen_ids.add(env_id)

        interval = _effective_interval(env.session_sync_interval, default_interval)
        if interval <= 0:
            continue  # 本环境显式关闭（0）
        last = _last_run.get(env_id)
        if last is not None and (now - last) < interval:
            continue  # 没到点

        try:
            # 1) 元信息对账：新会话进来、节点上没有的标 ended。这一步顺带把
            #    title/mode/effort/last_activity_at 刷新——列表显示的就是它们。
            result = await sync_environment_sessions(user_id, env_id)
        except Exception as exc:  # noqa: BLE001 — 单个环境失败不牵连别的
            # 失败也记一次「跑过」：否则一个离线节点会被每 tick 重试一次。
            _last_run[env_id] = now
            logger.debug("[env-sync] env {} reconcile failed: {}", env_id, exc)
            continue

        # 2) 内容尾部：只导活跃会话。刚对账完，账本里的 status/last_activity_at
        #    就是刚刷新的，直接用它筛。窗口按**本环境实际间隔**算，不按全局默认。
        active = _active_session_ids(result.get("sessions") or [], interval)
        if active:
            sem = asyncio.Semaphore(_IMPORT_CONCURRENCY)

            async def _one(sid: str) -> None:
                async with sem:
                    if _stop:
                        return
                    try:
                        await import_env_session_items(user_id, env_id, sid, mode="tail")
                    except Exception as exc:  # noqa: BLE001 — 单条失败不牵连
                        logger.debug("[env-sync] import {}:{} failed: {}", env_id, sid, exc)

            await asyncio.gather(*(_one(sid) for sid in active))
        _last_run[env_id] = now
        synced += 1

    # 顺手清掉已删除/已禁用环境的记录，避免字典随环境增删无限增长。
    for stale in set(_last_run) - seen_ids:
        _last_run.pop(stale, None)

    if synced:
        logger.debug("[env-sync] round done: {} environment(s) reconciled", synced)


def _active_session_ids(sessions: list[dict], interval: int) -> list[str]:
    """挑出该抓内容的会话 id：还在跑的，或刚结束不久的。

    只抓活跃的（不是全部）是刻意的：旧会话的内容早就拉过，每轮全拉一遍会把节点
    压满、把库撑大。用户点开旧会话详情时，那条链路会自己按需补（``import`` 接口
    是详情页的轮询与「查看历史」共用的入口）。
    """
    from datetime import datetime, timedelta, timezone

    # 「刚结束」的窗口：留 2 个间隔的余量，让「跑完最后一轮就退出」的会话也能被
    # 带走最后一轮输出，不留一条永远差一截的尾巴。
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(interval, 1) * _RECENT_INTERVALS)
    out: list[str] = []
    for s in sessions:
        sid = str(s.get("session_id") or "").strip()
        if not sid:
            continue
        if str(s.get("status") or "").lower() == "running":
            out.append(sid)
            continue
        # ended 的：最后活动时间在窗口内才再抓一次。
        raw = s.get("last_activity_at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(sid)
    return out