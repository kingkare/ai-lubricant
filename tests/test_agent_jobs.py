"""Tests for background jobs (agent/jobs.py + code_run background/operation)."""
import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.code_run_approval import CodeRunApprovalBatch
from agent.config import AgentConfig
from agent.jobs import JobRegistry
from agent.tools import ToolContext, ToolRegistry
from user_platform.node_client.approvals import approval_registry

# A command that prints a marker then blocks, so the job stays running.
_LONG_PY = "import time,sys; print('started', flush=True); time.sleep(30)"


def _registry(tmp_path) -> ToolRegistry:
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    config = AgentConfig(
        workspace_root=str(workspace),
        allowed_roots=[str(workspace), str(temp)],
    )
    return ToolRegistry(ToolContext(config), agent_id=7)


def _allow_registry(registry: ToolRegistry, code: str) -> None:
    """Install a code_run approval that auto-allows one call of ``code``."""
    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = CodeRunApprovalBatch(conversation_id="c1", agent_id=7, caller="u1", emit=emit)
    registry.set_code_run_approval(batch)


async def _wait_for(predicate, timeout=5.0):
    """Poll until predicate() is truthy or the timeout elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# --- JobRegistry unit -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_start_read_and_reap(tmp_path):
    reg = JobRegistry()
    job = await reg.start([sys.executable, "-u", "-c", "print('hello')"], cwd=str(tmp_path))
    assert job.job_id.startswith("job_")
    # Wait for the output to be drained BEFORE waiting for exit: the reap task
    # can record the exit code before the reader task flushes the buffer.
    assert await _wait_for(lambda: b"hello" in job.stdout)
    await _wait_for(lambda: not job.running)
    out = reg.read(job.job_id, "stdout")
    assert "hello" in out["new_output"]
    assert out["running"] is False
    assert out["exit_code"] == 0


@pytest.mark.asyncio
async def test_read_is_incremental(tmp_path):
    reg = JobRegistry()
    code = "import sys,time; print('one',flush=True); time.sleep(0.3); print('two',flush=True)"
    job = await reg.start([sys.executable, "-u", "-c", code], cwd=str(tmp_path))
    await _wait_for(lambda: b"one" in job.stdout)
    first = reg.read(job.job_id, "stdout")
    assert "one" in first["new_output"]
    await _wait_for(lambda: not job.running)
    second = reg.read(job.job_id, "stdout")
    # Second read returns only what arrived since the first.
    assert "two" in second["new_output"]
    assert "one" not in second["new_output"]


@pytest.mark.asyncio
async def test_unknown_job_id_is_error(tmp_path):
    reg = JobRegistry()
    assert reg.read("job_nope")["status"] == "error"
    assert (await reg.kill("job_nope"))["status"] == "error"


@pytest.mark.asyncio
async def test_kill_terminates_running_job(tmp_path):
    reg = JobRegistry()
    job = await reg.start([sys.executable, "-c", _LONG_PY], cwd=str(tmp_path))
    await _wait_for(lambda: b"started" in job.stdout)
    assert job.running
    out = await reg.kill(job.job_id)
    assert out["status"] == "ok"
    assert await _wait_for(lambda: not job.running)


@pytest.mark.asyncio
async def test_close_all_terminates_everything(tmp_path):
    reg = JobRegistry()
    j1 = await reg.start([sys.executable, "-c", _LONG_PY], cwd=str(tmp_path))
    j2 = await reg.start([sys.executable, "-c", _LONG_PY], cwd=str(tmp_path))
    await _wait_for(lambda: b"started" in j1.stdout and b"started" in j2.stdout)
    await reg.close_all()
    assert await _wait_for(lambda: not j1.running and not j2.running)


@pytest.mark.asyncio
async def test_list_reports_summaries(tmp_path):
    reg = JobRegistry()
    job = await reg.start([sys.executable, "-c", _LONG_PY], cwd=str(tmp_path))
    await _wait_for(lambda: b"started" in job.stdout)
    listing = reg.list()
    assert listing["count"] == 1
    assert listing["jobs"][0]["process_id"] == job.job_id
    await reg.close_all()


# --- code_run(background=true) + code_run operation=list/output/kill -----------------

@pytest.mark.asyncio
async def test_code_run_background_returns_process_id(tmp_path):
    registry = _registry(tmp_path)
    _allow_registry(registry, _LONG_PY)
    await registry._code_run_approval.prepare(
        [{"function": {"name": "code_run", "arguments": {"code": _LONG_PY, "type": "python"}}}]
    )
    result = await registry.execute("code_run", {"code": _LONG_PY, "type": "python", "background": True})

    assert result["status"] == "ok"
    assert result["background"] is True
    assert result["process_id"].startswith("job_")
    # The job is registered and (eventually) produces output.
    job = registry.jobs.get(result["process_id"])
    assert job is not None
    assert await _wait_for(lambda: b"started" in job.stdout)
    await registry.jobs.close_all()


@pytest.mark.asyncio
async def test_job_tool_output_and_kill(tmp_path):
    registry = _registry(tmp_path)
    _allow_registry(registry, _LONG_PY)
    await registry._code_run_approval.prepare(
        [{"function": {"name": "code_run", "arguments": {"code": _LONG_PY, "type": "python"}}}]
    )
    started = await registry.execute("code_run", {"code": _LONG_PY, "type": "python", "background": True})
    process_id = started["process_id"]
    job = registry.jobs.get(process_id)
    await _wait_for(lambda: b"started" in job.stdout)

    # Management goes through code_run(operation=...), NOT a separate tool, and
    # needs no approval (it runs no new code).
    out = await registry.execute("code_run", {"operation": "output", "process_id": process_id})
    assert out["status"] == "ok"
    assert "started" in out["new_output"]

    listing = await registry.execute("code_run", {"operation": "list"})
    assert listing["count"] == 1

    killed = await registry.execute("code_run", {"operation": "kill", "process_id": process_id})
    assert killed["status"] == "ok"
    assert await _wait_for(lambda: not job.running)


@pytest.mark.asyncio
async def test_job_output_requires_process_id(tmp_path):
    registry = _registry(tmp_path)
    out = await registry.execute("code_run", {"operation": "output"})
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_background_code_run_still_needs_approval(tmp_path):
    """background=true does not bypass the code_run approval gate."""
    registry = _registry(tmp_path)  # no approval coordinator → fail closed
    result = await registry.execute("code_run", {"code": _LONG_PY, "type": "python", "background": True})
    assert result["status"] == "denied"


@pytest.mark.asyncio
async def test_management_operations_need_no_approval(tmp_path):
    """operation=list/output/kill control already-approved processes — no gate.

    With NO approval coordinator installed, running code is denied but listing
    background processes still works: management executes no new code.
    """
    registry = _registry(tmp_path)  # no approval coordinator at all
    assert (await registry.execute("code_run", {"code": "print(1)"}))["status"] == "denied"

    listing = await registry.execute("code_run", {"operation": "list"})
    assert listing["status"] == "ok"
    assert listing["count"] == 0