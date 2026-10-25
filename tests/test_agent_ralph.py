"""Tests for Ralph mode helpers (agent/ralph.py) + goal verification.

Ralph is a driver-loop MODE (like goal), so these tests cover the pure pieces the
loop uses: prompt building, strict report parsing/validation, outcome
formatting, and the independent goal verifier. The round loop itself lives in
agent/api.py + agent/cdp_chat_service.py, driven by Guardian's state machine.
"""
import asyncio
import json
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.ralph import (
    build_round_prompt,
    extract_json,
    format_round_outcome,
    parse_round_report,
    verify_goal_completion,
)


# --- extract / parse -----------------------------------------------------------------

def test_extract_json_parses_pure_and_wrapped():
    assert extract_json('{"status": "complete"}')["status"] == "complete"
    assert extract_json('here is my report: {"status":"blocked"} done')["status"] == "blocked"


def test_extract_json_rejects_no_object():
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_round_report_requires_continue_next_steps():
    base = {"status": "continue", "summary": "working", "evidence": [], "nextSteps": [], "blocker": ""}
    with pytest.raises(ValueError):
        parse_round_report(json.dumps(base))
    ok = parse_round_report(json.dumps({**base, "nextSteps": ["do X"]}))
    assert ok["status"] == "continue"


def test_parse_round_report_requires_complete_evidence():
    base = {"status": "complete", "summary": "done", "evidence": [], "nextSteps": [], "blocker": ""}
    with pytest.raises(ValueError):
        parse_round_report(json.dumps(base))
    ok = parse_round_report(json.dumps({**base, "evidence": ["x works"]}))
    assert ok["status"] == "complete"


def test_parse_round_report_complete_rejects_leftover_next_steps():
    with pytest.raises(ValueError):
        parse_round_report(json.dumps({
            "status": "complete", "summary": "done",
            "evidence": ["x works"], "nextSteps": ["more"], "blocker": "",
        }))


def test_parse_round_report_blocked_needs_blocker():
    with pytest.raises(ValueError):
        parse_round_report(json.dumps(
            {"status": "blocked", "summary": "stuck", "evidence": [], "nextSteps": [], "blocker": ""}
        ))


def test_parse_round_report_rejects_unknown_status():
    with pytest.raises(ValueError):
        parse_round_report(json.dumps({"status": "maybe", "summary": "x"}))


def test_parse_round_report_requires_summary():
    with pytest.raises(ValueError):
        parse_round_report(json.dumps({"status": "continue", "summary": "", "nextSteps": ["a"]}))


# --- prompt -------------------------------------------------------------------------

def test_build_round_prompt_carries_objective_and_handoff():
    p1 = build_round_prompt("do the thing", None, 1, 8)
    assert "do the thing" in p1
    assert "round: 1 of 8" in p1.lower() or "1 of 8" in p1
    assert "none" in p1.lower()  # first round has no handoff

    handoff = {"status": "continue", "summary": "did half", "nextSteps": ["finish"]}
    p2 = build_round_prompt("do the thing", handoff, 2, 8)
    assert "did half" in p2
    assert "finish" in p2
    assert "2 of 8" in p2


def test_build_round_prompt_declares_workspace_as_truth():
    p = build_round_prompt("obj", None, 1, 4)
    assert "source of truth" in p.lower()


# --- outcome formatting ---------------------------------------------------------------

def test_format_round_outcome_complete():
    text = format_round_outcome("complete", 3, {"status": "complete", "summary": "shipped", "evidence": ["e1", "e2"]})
    assert "complete" in text
    assert "3" in text
    assert "shipped" in text
    assert "e1" in text


def test_format_round_outcome_blocked_includes_blocker():
    text = format_round_outcome("blocked", 2, {"status": "blocked", "summary": "stuck", "blocker": "need login"})
    assert "blocked" in text
    assert "need login" in text


def test_format_round_outcome_max_rounds_no_report():
    text = format_round_outcome("max_rounds", 16, None)
    assert "max_rounds" in text
    assert "16" in text


# --- goal verification ----------------------------------------------------------------

class FakeAgent:
    """Minimal stand-in for GenericAgent: spawn children whose verdicts are scripted."""

    def __init__(self, child_reports):
        self.child_reports = list(child_reports)
        self.spawned: list[str] = []

    async def spawn_subagent_async(self, task):
        self.spawned.append(task)
        text = self.child_reports.pop(0)

        if isinstance(text, Exception):
            class _CrashingTask:
                def __await__(self_inner):
                    raise text
                    yield  # pragma: no cover
            asyncio_task = _CrashingTask()
        else:
            async def _done():
                return [{"result": "CURRENT_TASK_DONE", "data": text}]
            asyncio_task = _done()

        class Handle:  # noqa: N801
            pass
        h = Handle()
        h.asyncio_task = asyncio_task
        return h


@pytest.mark.asyncio
async def test_verify_goal_completion_confirms():
    agent = FakeAgent([json.dumps({"verdict": "confirmed", "checked": ["re-ran"], "problems": [], "summary": "ok"})])
    out = await verify_goal_completion(agent, "ship login", ["login works"])
    assert out["verified"] is True
    assert "ship login" in agent.spawned[0]
    assert "login works" in agent.spawned[0]


@pytest.mark.asyncio
async def test_verify_goal_completion_refutes():
    agent = FakeAgent([json.dumps({"verdict": "refuted", "checked": [], "problems": ["step 3 not run"], "summary": "no"})])
    out = await verify_goal_completion(agent, "obj", ["evidence"])
    assert out["verified"] is False
    assert out["report"]["verdict"] == "refuted"


@pytest.mark.asyncio
async def test_verify_goal_completion_bad_json_means_not_verified():
    agent = FakeAgent(["garbage"])
    out = await verify_goal_completion(agent, "obj", [])
    assert out["verified"] is False
    assert "error" in out


@pytest.mark.asyncio
async def test_verify_goal_completion_child_crash_means_not_verified():
    agent = FakeAgent([RuntimeError("verifier died")])
    out = await verify_goal_completion(agent, "obj", [])
    assert out["verified"] is False
    assert "error" in out
