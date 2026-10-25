"""源同步触发路由 —— 手动 / 到点扫描 / git push webhook。

* ``POST /api/v1/testing/source/{source_id}/sync``     手动触发（页面「同步」按钮）
* ``POST /api/v1/testing/source/sync-due``             扫描并同步**已到点**的定时源
* ``POST /api/v1/testing/source/{source_id}/webhook``  git push webhook（仿 ``webhook_review_worker``）

webhook 端点对源配置做**幂等触发**：同一次 push（同 commit）重复回调不会重复建 L1
行（``mc_knowledge_raw`` 按 content_hash 幂等）。鉴权：webhook 用源的 ``config.webhook_secret``
（配了就校验 ``X-Webhook-Secret`` 头；没配 = 平台内网调用，不拦）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException

logger = logging.getLogger(__name__)

#: 子路由：相对前缀 ``/source``，由 :mod:`surface.api` include。
router = APIRouter(prefix="/source", tags=["testing-source-sync"])


async def _load_source(source_id: str):
    from ..projects import get_source
    from ..projects.service import ProjectValidationError

    try:
        return await get_source(source_id)
    except ProjectValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


async def _ensure_scheduler() -> None:
    """首次调用源同步接口时惰性拉起后台轮询（幂等、无定时源时什么都不做）。"""
    try:
        from ..scheduler import ensure_started

        await ensure_started()
    except Exception:  # noqa: BLE001 — 调度器起不来不影响本次手动同步
        logger.debug("[testing-source-sync] scheduler ensure failed", exc_info=True)


@router.post("/{source_id}/sync")
async def sync_source_now(
    source_id: str,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """手动触发一个源的同步（sync_policy 无关——手动优先于配置）。"""
    # 复用能力面鉴权（cookie / 管理员 Bearer）
    from .chat import _resolve_caller

    await _resolve_caller(authorization)
    source = await _load_source(source_id)
    from ..scheduler import run_source_sync

    result = await run_source_sync(source)
    if not result.get("ok"):
        # 同步失败 → 422 + 明细（调用方决定重试）
        raise HTTPException(status_code=422, detail=result)
    return {"ok": True, "result": result}


@router.post("/sync-due")
async def sync_due_now(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    """扫描**已到点**的定时源并同步（也可由外部 cron 调它兜底）。"""
    from .chat import _resolve_caller

    await _resolve_caller(authorization)
    await _ensure_scheduler()
    from ..scheduler import sync_due_sources

    return await sync_due_sources()


@router.post("/{source_id}/webhook")
async def source_webhook(
    source_id: str,
    request_payload: dict[str, Any] | None = None,
    x_webhook_secret: Optional[str] = Header(None),
) -> dict[str, Any]:
    """git push / 外部系统回调 → 触发该源同步。

    鉴权：源 ``config.webhook_secret`` 配了则必须匹配 ``X-Webhook-Secret``。
    响应恒 200（webhook 语义：失败也回 200 + 明细，避免上游疯狂重试）。
    """
    source = await _load_source(source_id)
    expected = (source.config or {}).get("webhook_secret")
    if expected and str(x_webhook_secret or "") != str(expected):
        raise HTTPException(status_code=401, detail="webhook secret 不匹配")

    from ..scheduler import run_source_sync

    result = await run_source_sync(source)
    return {"ok": bool(result.get("ok")), "result": result}


__all__ = ["router"]
