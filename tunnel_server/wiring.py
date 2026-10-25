"""Dependency wiring for the standalone tunnel runtime service."""
from __future__ import annotations

import asyncio
import contextlib
import json

from loguru import logger

from .config import settings
from .notify_listener import notify_listener


class RuntimeWorker:
    """One worker owning the runtime reconciler and local process supervisor."""

    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        # The supervisor/reconciler are imported only in this process. The main
        # API service must never import these executors during its lifespan.
        from user_platform.tunnel_supervisor import tunnel_supervisor
        from user_platform.tunnel_runtime_manager import (
            configure_lease,
            tunnel_runtime_reconciler,
        )
        from user_platform.tunnel_panel_scheduler import tunnel_panel_scheduler
        from user_platform.ddns_scheduler import ddns_scheduler

        configure_lease(
            owner=settings.instance_id,
            ttl_seconds=settings.lease_ttl_seconds,
        )
        await tunnel_supervisor.start()
        await tunnel_runtime_reconciler.start()
        # Panel scheduler: 定时签到 / 同步积分 for account-backed kinds (ChmlFrp).
        await tunnel_panel_scheduler.start()
        # DDNS: 定时公网 IP 探测 + DNS 同步（记录存 mc_ddns_records）。
        await ddns_scheduler.start()
        self._task = asyncio.create_task(self._run(), name="tunnel-runtime-worker")

    async def stop(self) -> None:
        self.running = False
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        from user_platform.tunnel_runtime_manager import tunnel_runtime_reconciler
        from user_platform.tunnel_supervisor import tunnel_supervisor
        from user_platform.tunnel_panel_scheduler import tunnel_panel_scheduler
        from user_platform.ddns_scheduler import ddns_scheduler

        await ddns_scheduler.stop()
        await tunnel_panel_scheduler.stop()
        await tunnel_runtime_reconciler.stop()
        await tunnel_supervisor.stop()

    async def wake(self, payload: dict | None = None) -> None:
        """Wake reconciliation after a committed main-service mutation."""
        self._wake.set()

    async def _run(self) -> None:
        from user_platform.tunnel_runtime_manager import reconcile_all

        while self.running:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=settings.reconcile_poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if not self.running:
                return
            try:
                await reconcile_all()
            except Exception:  # noqa: BLE001
                logger.exception("[tunnel-server] runtime reconcile failed")


runtime_worker = RuntimeWorker()
notify_listener._on_notify = runtime_worker.wake
