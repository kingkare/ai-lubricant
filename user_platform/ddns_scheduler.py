"""Scheduled DDNS syncs, owned by the standalone tunnel_server process.

Mirrors :mod:`.tunnel_panel_scheduler`: one serial loop that walks every enabled
record and runs the ones whose ``interval_seconds`` has elapsed. Records are few
and each sync is cheap, so a single loop suffices (no queue/worker split).

Last-run bookkeeping lives in the record's own ``last_sync_at`` column (written
by :func:`ddns_sync.run_record`), so a restart resumes without re-firing a sync
that just ran. A record that never synced runs on the first tick.

This module is owned by the standalone tunnel runtime service
(``tunnel_server``); the main API process must not import it (the "sync now"
route calls :func:`ddns_sync.run_record` directly instead).
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from loguru import logger

_TICK_SECONDS = 30  # finer than the panel scheduler: default interval is 120s


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DdnsScheduler:
    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._run(), name="ddns-scheduler")

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
                logger.warning("[ddns] tick failed: {}", exc)
            await asyncio.sleep(_TICK_SECONDS)

    async def tick(self) -> None:
        """Run every due record once. Serial: records are few, syncs are cheap.

        A record whose provider profile is disabled (or missing) is skipped
        rather than run: running it would only fail and spam the failure
        notification on every tick.
        """
        from .ddns_sync import run_record
        from .models_ddns import DdnsProvider, DdnsRecord

        rows = await DdnsRecord.filter(enabled=True).order_by("created_at")
        if not rows:
            return
        disabled = {
            str(p.id) for p in await DdnsProvider.filter(enabled=False)
        }
        now = _now()
        for row in rows:
            if str(row.provider_id) in disabled:
                continue
            interval = int(row.interval_seconds or 120)
            last = row.last_sync_at
            if last is not None:
                elapsed = (now - last).total_seconds()
                if elapsed < interval:
                    continue
            try:
                await run_record(row)
            except Exception as exc:  # noqa: BLE001 — one record failure is not fatal
                logger.warning("[ddns] record {} sync failed: {}", row.id, exc)


ddns_scheduler = DdnsScheduler()
