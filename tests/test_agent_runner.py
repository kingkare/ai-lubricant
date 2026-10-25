import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.runner import AgentTaskRunner, ensure_scheduler_running, scheduler_wanted
from agent.workflow_scheduler import get_workflow_scheduler


@pytest.fixture(autouse=True)
def _reset_runner():
    """Ensure AgentTaskRunner state is clean before/after each test."""
    AgentTaskRunner._started = False
    if hasattr(AgentTaskRunner, "_scheduler"):
        delattr(AgentTaskRunner, "_scheduler")
    yield
    AgentTaskRunner._started = False
    if hasattr(AgentTaskRunner, "_scheduler"):
        delattr(AgentTaskRunner, "_scheduler")


def test_start_calls_scheduler_start():
    with patch("agent.runner.get_workflow_scheduler") as mock_get:
        mock_instance = mock_get.return_value
        AgentTaskRunner.start()
        assert AgentTaskRunner._started is True
        mock_instance.start.assert_called_once()
    mock_get.assert_called_once()


def test_start_is_idempotent():
    with patch("agent.runner.get_workflow_scheduler") as mock_get:
        AgentTaskRunner.start()
        AgentTaskRunner.start()
        mock_get.assert_called_once()


@pytest.mark.asyncio
async def test_stop_calls_scheduler_shutdown():
    with patch("agent.runner.get_workflow_scheduler") as mock_get:
        mock_instance = mock_get.return_value
        AgentTaskRunner.start()
        await AgentTaskRunner.stop()
        mock_instance.shutdown.assert_called_once_with(wait=False)
        assert AgentTaskRunner._started is False


@pytest.mark.asyncio
async def test_stop_noop_when_not_started():
    await AgentTaskRunner.stop()
    assert AgentTaskRunner._started is False


# ── 闸门（scheduler_wanted）与运行期拉起（ensure_scheduler_running）──

class _FakeConn:
    def __init__(self, replies: list):
        self._replies = replies

    async def fetchrow(self, sql, *args):
        return self._replies.pop(0) if self._replies else None

    async def fetch(self, sql, *args):
        # scheduler_wanted 走 fetch（一次拿全部最新版本定义的 definition）。
        return self._replies.pop(0) if self._replies else []


class _FakePool:
    def __init__(self, replies: list):
        self._replies = replies

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(outer._replies)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_scheduler_wanted_false_without_pool(monkeypatch):
    from db import PostgresClient
    monkeypatch.setattr(PostgresClient, "pool", None, raising=False)
    assert await scheduler_wanted() is False


@pytest.mark.asyncio
async def test_scheduler_wanted_true_on_scheduled_workflow(monkeypatch):
    from db import PostgresClient
    rows = [{"definition": {"triggers": [
        {"id": "daily", "kind": "cron", "cron": "0 9 * * *", "enabled": True},
    ]}}]
    monkeypatch.setattr(PostgresClient, "pool", _FakePool([rows]), raising=False)
    assert await scheduler_wanted() is True


@pytest.mark.asyncio
async def test_scheduler_wanted_false_when_no_signals(monkeypatch):
    from db import PostgresClient
    # 库里只有禁用了 cron 的定义 → 不启动调度器。
    rows = [{"definition": {"triggers": [
        {"id": "off", "kind": "cron", "cron": "0 9 * * *", "enabled": False},
    ]}}]
    monkeypatch.setattr(PostgresClient, "pool", _FakePool([rows]), raising=False)
    assert await scheduler_wanted() is False


def test_ensure_scheduler_running_starts_once_on_intent():
    with patch("agent.runner.get_workflow_scheduler") as mock_get:
        ensure_scheduler_running(True)
        assert AgentTaskRunner._started is True
        mock_get.return_value.start.assert_called_once()
        ensure_scheduler_running(True)
        mock_get.assert_called_once()


@pytest.mark.parametrize("intent", [False, None])
def test_ensure_scheduler_running_noop_without_intent(intent):
    with patch("agent.runner.get_workflow_scheduler") as mock_get:
        ensure_scheduler_running(intent)
        mock_get.assert_not_called()
        assert AgentTaskRunner._started is False


def test_get_workflow_scheduler_is_process_singleton():
    import agent.workflow_scheduler as sched_mod

    sched_mod._singleton = None
    first = get_workflow_scheduler()
    second = get_workflow_scheduler()
    assert first is second
    first.start = MagicMock()
    with patch("agent.runner.get_workflow_scheduler", return_value=first) as mock_get:
        AgentTaskRunner.start()
        mock_get.assert_called_once()
        first.start.assert_called_once()
    sched_mod._singleton = None
