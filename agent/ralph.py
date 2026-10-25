"""Ralph mode helpers + goal verification.

Ralph is a **mode** (like goal), not a model-facing tool: the API/chat-service
driver loop wakes the agent once per round, each round runs as a *fresh*
bounded agent loop carrying only the immutable objective and the previous
round's structured handoff, and the round's final text is parsed+validated as a
strict report (``continue``/``complete``/``blocked``). State lives in
``agent_goal_states`` (``mode='ralph'``), mirroring goal mode exactly — see
``agent/guardian.py`` for the state machine.

This module is pure: prompt building, report parsing/validation, outcome
formatting, and the independent goal-completion verifier. It spawns nothing
itself — the driver loop owns the rounds, so a round is an ordinary agent loop
the same way a goal round is.

A fresh child per round avoids the long-run context pollution of one continuing
agent; the handoff is the only thing that crosses the round boundary, and the
workspace is the source of truth. Bad reports never silently pass — they end
the loop with ``round_failed`` / ``verification_failed`` rather than inventing
a result.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Loop caps — operation-class scoped. A browser/dev-server round is far costlier
# than a code edit, so the ceiling is tighter than dsh's 256.
DEFAULT_MAX_ROUNDS = 16
# Bound on the serialized handoff we carry between rounds, matching dsh's cap.
DEFAULT_MAX_HANDOFF_CHARS = 16384

# Strict-JSON schema for a round report. Mirrors dsh's reportSchema; the
# per-status invariants (continue needs nextSteps, complete needs evidence,
# blocked needs a blocker) are enforced in :func:`_validate_report`.
_REPORT_SCHEMA_HINT = (
    'Return strict JSON: {"status":"continue|complete|blocked",'
    '"summary":"<one line>",'
    '"evidence":["<concrete fact>...],'
    '"nextSteps":["<next action>...],'
    '"blocker":""}. '
    "status=complete requires non-empty evidence and empty nextSteps/blocker; "
    "continue requires non-empty nextSteps and empty blocker; "
    "blocked requires a non-empty blocker."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ValueError("empty report")
    try:
        return json.loads(text)
    except ValueError:
        m = _JSON_RE.search(text)
        if not m:
            raise ValueError("no JSON object in report")
        return json.loads(m.group(0))


def _norm_str(value: Any) -> str:
    return str(value or "").strip()


def _norm_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v or "").strip() for v in value if str(v or "").strip() != ""]


def _validate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Enforce the strict per-status invariants; raise on any violation."""
    if not isinstance(report, dict):
        raise ValueError("report is not an object")
    status = _norm_str(report.get("status"))
    if status not in ("continue", "complete", "blocked"):
        raise ValueError(f"invalid status: {status!r}")
    out: dict[str, Any] = {
        "status": status,
        "summary": _norm_str(report.get("summary")),
        "evidence": _norm_list(report.get("evidence")),
        "nextSteps": _norm_list(report.get("nextSteps")),
        "blocker": str(report.get("blocker") or "").strip(),
    }
    if not out["summary"]:
        raise ValueError("summary must be non-empty")
    if status == "continue":
        if not out["nextSteps"]:
            raise ValueError("continue needs non-empty nextSteps")
        if out["blocker"]:
            raise ValueError("continue needs empty blocker")
    elif status == "complete":
        if not out["evidence"]:
            raise ValueError("complete needs non-empty evidence")
        if out["nextSteps"]:
            raise ValueError("complete needs empty nextSteps")
        if out["blocker"]:
            raise ValueError("complete needs empty blocker")
    else:  # blocked
        if not out["blocker"]:
            raise ValueError("blocked needs a non-empty blocker")
    serialized = json.dumps(out, ensure_ascii=False)
    if len(serialized) > DEFAULT_MAX_HANDOFF_CHARS:
        raise ValueError(
            f"report exceeds {DEFAULT_MAX_HANDOFF_CHARS} chars ({len(serialized)})"
        )
    return out


def parse_round_report(text: str) -> dict[str, Any]:
    """Parse a round's final text into a validated report; raise on any failure."""
    return _validate_report(extract_json(text))


def build_round_prompt(
    objective: str,
    previous: dict[str, Any] | None,
    round_index: int,
    max_rounds: int,
) -> str:
    """The prompt for one Ralph round (a fresh bounded agent loop)."""
    prior = "(none — this is the first round)" if previous is None else json.dumps(
        previous, ensure_ascii=False
    )
    return "\n\n".join([
        "You are one fresh worker in a foreground Ralph loop. You receive no parent "
        "conversation and no prior child session.",
        f"Immutable objective:\n{objective}",
        f"Ralph round: {round_index} of {max_rounds}.",
        "The shared workspace and its current state are the long-term memory and "
        "source of truth. Inspect them before acting, preserve existing work, "
        "perform concrete in-scope work, and verify what you change. Treat the "
        "previous report only as a bounded handoff — confirm it against the "
        "workspace, do not trust it verbatim.",
        f"Previous structured handoff:\n{prior}",
        _REPORT_SCHEMA_HINT,
    ])


def format_round_outcome(outcome: str, rounds_started: int, report: dict[str, Any] | None) -> str:
    """Human-readable terminal text for the Ralph run (appended to the message)."""
    lines = [f"[Ralph] Loop ended: {outcome} (rounds started: {rounds_started})"]
    if report:
        lines.append(f"Final report summary: {report.get('summary', '')}")
        if report.get("evidence"):
            lines.append("Evidence: " + "; ".join(report["evidence"][:8]))
        if report.get("blocker"):
            lines.append(f"Blocker: {report['blocker']}")
    return "\n".join(lines)


# --- Goal verification ----------------------------------------------------------

_VERIFICATION_PROMPT = (
    "You are an independent verifier. Another agent claimed a goal is complete. "
    "Your job is to REFUTE the claim, not to confirm it — a verifier asked to "
    "agree will agree. Re-run or re-check the objective's acceptance evidence "
    "yourself; only return confirmed if every claimed piece of evidence holds up "
    "under your own check.\n\n"
    "Objective:\n{objective}\n\n"
    "Claimed evidence:\n{evidence}\n\n"
    "Return strict JSON: {{\"verdict\":\"confirmed|refuted\","
    "\"checked\":[\"<what you independently verified>\"],"
    "\"problems\":[\"<concrete discrepancy>\"],"
    "\"summary\":\"<one line>\"}}. verdict=confirmed requires empty problems and "
    "non-empty checked; refuted requires at least one problem."
)


def _validate_verification(report: dict[str, Any]) -> dict[str, Any]:
    verdict = _norm_str(report.get("verdict"))
    if verdict not in ("confirmed", "refuted"):
        raise ValueError(f"invalid verdict: {verdict!r}")
    out = {
        "verdict": verdict,
        "checked": _norm_list(report.get("checked")),
        "problems": _norm_list(report.get("problems")),
        "summary": _norm_str(report.get("summary")),
    }
    if verdict == "confirmed" and out["problems"]:
        raise ValueError("confirmed requires empty problems")
    if verdict == "confirmed" and not out["checked"]:
        raise ValueError("confirmed requires non-empty checked")
    if verdict == "refuted" and not out["problems"]:
        raise ValueError("refuted requires at least one problem")
    return out


def _child_final_text(outputs: list[dict[str, Any]]) -> str:
    """Extract a child run's final text from its run_task output list."""
    for item in reversed(outputs or []):
        if isinstance(item, dict) and item.get("result") == "CURRENT_TASK_DONE":
            data = item.get("data")
            if isinstance(data, str) and data.strip():
                return data
    if outputs:
        last = outputs[-1]
        if isinstance(last, dict):
            return str(last.get("data") or "")
    return ""


async def verify_goal_completion(
    agent,
    objective: str,
    evidence: list[str],
) -> dict[str, Any]:
    """Spawn a fresh verifier that tries to refute a goal-complete claim.

    Returns ``{"verified": bool, "report": {...}}``. ``verified`` is True only
    when the verifier independently confirmed. Any failure (child crash, bad
    JSON, invariant violation) returns ``verified=False`` with an ``error`` —
    the goal is NOT marked complete on a failed verification.

    This one *does* spawn (via ``agent.spawn_subagent_async``), unlike the round
    helpers above: verification is a single side-check the driver loop calls
    once, not a round it drives.
    """
    evidence_text = "\n".join(f"- {e}" for e in evidence) or "(none provided)"
    prompt = _VERIFICATION_PROMPT.format(objective=objective, evidence=evidence_text)
    try:
        handle = await agent.spawn_subagent_async(task=prompt)
        try:
            outputs = await handle.asyncio_task
        finally:
            try:
                getattr(agent, "sub_agents", []).remove(handle)
            except (ValueError, AttributeError):
                pass
        report = _validate_verification(extract_json(_child_final_text(outputs)))
    except Exception as exc:  # noqa: BLE001 - verification failure ⇒ not complete
        logger.warning("[goal-verify] verification failed: %s", exc)
        return {"verified": False, "error": str(exc)}
    return {"verified": report["verdict"] == "confirmed", "report": report}


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_HANDOFF_CHARS",
    "build_round_prompt",
    "parse_round_report",
    "format_round_outcome",
    "verify_goal_completion",
]