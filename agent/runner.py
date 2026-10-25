"""后台调度器生命周期管理。

实际调度逻辑由 APScheduler（workflow_scheduler 内）处理，本模块仅负责
在 main.py lifespan 中启停它。

定时任务（prompt/script task）面已删除：cron 触发统一走工作流的「启动方式
（triggers）」，定义在 workflow_defs.definition 里。
"""
from __future__ import annotations

import asyncio

from loguru import logger

from agent.workflow_scheduler import get_workflow_scheduler


async def scheduler_wanted() -> bool:
    """启动闸门：库里存在声明了启用 cron 启动方式的工作流定义。

    启动方式（triggers）随 definition 保存（triggers[].kind='cron' 且 enabled）；
    旧的 schedule_enabled 列已不再是事实源。判定不了（表不存在/DB 抖动）按未启用
    处理——启动后没有 cron job，但不会崩。
    """
    from db import PostgresClient

    pool = getattr(PostgresClient, "pool", None)
    if not pool:
        return False
    try:
        from agent.workflow_scheduler import _cron_triggers_of

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT definition FROM workflow_defs d
                WHERE d.id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY COALESCE(user_id, ''), name ORDER BY version DESC
                        ) AS rn FROM workflow_defs
                    ) t WHERE t.rn = 1
                )
                """
            )
        return any(_cron_triggers_of(r["definition"]) for r in rows)
    except Exception as e:  # noqa: BLE001 — 表不存在/DB 抖动时按未启用处理
        logger.warning(f"查询定时任务调度意图失败: {e}")
        return False


class AgentTaskRunner:
    """Class-level singleton，管理工作流调度器的生命周期。"""

    _started: bool = False

    @classmethod
    def start(cls) -> None:
        """启动调度器（幂等）。闸门判定在 main.py lifespan（scheduler_wanted）；
        运行期意图（挂 cron）走 workflow.* schedule 端点，同样幂等 start。

        用进程内共享单例（get_workflow_scheduler）而非新建——REST 路径拿到的
        必须是同一个实例，否则各自注册的 job 互不可见。
        """
        if cls._started:
            return
        scheduler = get_workflow_scheduler()
        scheduler.start()
        cls._started = True
        cls._scheduler = scheduler
        logger.info("工作流调度器启动")

    @classmethod
    async def stop(cls) -> None:
        """停止调度器。"""
        if not cls._started:
            return
        if hasattr(cls, "_scheduler"):
            # wait=False：关闭时不阻塞等待正在执行的运行跑完。wait=True 是同步
            # 阻塞，会占住事件循环，连 lifespan 里的超时兜底都无法中断。
            cls._scheduler.shutdown(wait=False)
        cls._started = False
        logger.info("工作流调度器已停止")


def ensure_scheduler_running(requested_enabled: bool | None) -> None:
    """运行期兜底：某处「开了调度意图」后，确保调度器已在跑（免重启）。

    只幂等 start，不反向 shutdown——停机归 lifespan 下次启动的闸门判定。
    """
    if not requested_enabled:
        return
    try:
        AgentTaskRunner.start()
    except Exception as e:  # noqa: BLE001 — 拉起失败不阻断调用方主流程
        logger.warning(f"拉起工作流调度器失败: {e}")
