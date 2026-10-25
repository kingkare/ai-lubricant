"""Scheduled panel actions for account-backed tunnel kinds (ChmlFrp).

The "定时签到 / 同步积分" requirement: a periodic loop that walks every enabled
panel scheme and runs the actions whose schedule (declared in
:mod:`.tunnel_panel_spec`, the scheduling truth source) is due. It mirrors the
code-channel account-actions scheduler but is far simpler — schemes are few and
the actions are cheap, so one serial loop suffices (no queue/worker split).

Last-run bookkeeping lives in the scheme's own ``config`` under a ``_panel_runs``
map (``{action_id: epoch_seconds}``), so a restart resumes without re-firing
today's check-in. The narrow write never clobbers operator-set fields.

This module is owned by the standalone tunnel runtime service
(``tunnel_server``); the main API process must not import it.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from . import tunnel_panel_spec as panel

_TICK_SECONDS = 300  # 5 min cadence is plenty for daily/hourly schedules


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _due(last_run_epoch: int, action: dict, now: datetime) -> bool:
    """Is ``action`` due given its last run and declared default schedule?"""
    cfg = action.get("default") or {}
    if cfg.get("enabled") is False:
        return False
    unit = cfg.get("frequency_unit") or "daily"
    if not last_run_epoch:
        return True  # never ran
    last = datetime.fromtimestamp(last_run_epoch, tz=timezone.utc).replace(tzinfo=None)
    if unit == "daily":
        # natural-day + daily_time (default 10:00). Fires once the target time
        # for today has passed and we have not run since that target.
        hh, mm = (cfg.get("daily_time") or "10:00").split(":")
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        return now >= target and last < target
    if unit == "hours":
        return now - last >= timedelta(hours=int(cfg.get("frequency_value") or 1))
    if unit == "minutes":
        return now - last >= timedelta(minutes=int(cfg.get("frequency_value") or 1))
    return False


class TunnelPanelScheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run(), name="tunnel-panel-scheduler")

    async def stop(self) -> None:
        self.running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while self.running:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — a tick failure must not kill the loop
                logger.warning("[tunnel-panel] tick failed: {}", exc)
            await asyncio.sleep(_TICK_SECONDS)

    async def tick(self) -> None:
        """Run every due action across all enabled panel schemes."""
        from .models_tunnel import TunnelScheme
        from .tunnel_panel_service import execute_action

        panel_kinds = [k for k, s in panel.PANEL_SPECS.items() if s.get("has_panel")]
        if not panel_kinds:
            return
        schemes = await TunnelScheme.filter(enabled=True, kind__in=panel_kinds)
        now = _now()
        for scheme in schemes:
            config = dict(scheme.config or {})
            runs = dict(config.get("_panel_runs") or {})
            changed = False
            for action in panel.panel_spec_for(scheme.kind).get("actions") or []:
                action_id = action.get("id") or ""
                if not action_id:
                    continue
                if not _due(int(runs.get(action_id) or 0), action, now):
                    continue
                try:
                    result = await execute_action(action_id, config)
                except Exception as exc:  # noqa: BLE001 — one action failure is not fatal
                    logger.warning(
                        "[tunnel-panel] action {} on scheme {} failed: {}",
                        action_id, scheme.id, exc,
                    )
                    continue
                runs[action_id] = int(time.time())
                changed = True
                # Merge the action's narrow result fields back onto config.
                for key, value in (result or {}).items():
                    if key in ("ok", "error", "skipped") or value is None:
                        continue
                    config[key] = value
            if changed:
                config["_panel_runs"] = runs
                scheme.config = config
                await scheme.save(update_fields=["config", "updated_at"])


tunnel_panel_scheduler = TunnelPanelScheduler()
