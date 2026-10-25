"""Advisory repeat-tool-call guard (dsh ``repeat-tool-reminder``, ported).

The model sometimes hammers the *same tool with identical arguments* turn after
turn — rerunning a command whose output it didn't understand, re-probing a page,
or repeatedly hitting a denied call. Each attempt burns tokens and, for
``code_run``, re-opens the approval gate, yet makes no progress.

This module is deliberately *advisory only*: it never vetoes or rewrites a call.
After a call executes (``denied`` calls included — a model hammering a refused
tool is exactly the loop worth breaking) it advances a single per-run chain
keyed by ``(tool_name, canonical_args)`` and, when the run length hits a
configured threshold, returns a reminder string for the loop to inject as an
extra user-role message. A different call resets the chain to length 1.

Canonicalization deep-sorts object keys before serializing, so two argument
objects that differ only in property order count as the same call — matching
dsh's ``sortJsonValue``. Nothing here counts across runs: the guard lives on the
per-``run_task`` handler, so a fresh run (equivalently, a new human request)
starts from zero, which is dsh's "reset on user message" behavior for free.
"""
from __future__ import annotations

import json
from typing import Any

# Gentle tier fires at thresholds[0]; detailed tiers at the rest. Mirrors dsh's
# default [3, 5, 8].
DEFAULT_THRESHOLDS: tuple[int, ...] = (3, 5, 8)
# Cap on the argument preview quoted in the detailed reminder (the chain key
# always uses the full canonical string).
DEFAULT_ARGS_PREVIEW_CHARS = 500
# Tools exempt from tracking. ``update_working_checkpoint`` is a legitimate
# per-turn heartbeat the loop itself encourages, so repeats are not a loop.
DEFAULT_EXCLUDE: tuple[str, ...] = ("update_working_checkpoint",)

GENTLE_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the task is "
    "not complete, try a different approach or different arguments instead of "
    "repeating the call."
)


def _sort_json(value: Any) -> Any:
    """Deep key-sort so two argument objects differing only in key order match."""
    if isinstance(value, list):
        return [_sort_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _sort_json(value[k]) for k in sorted(value)}
    return value


def canonicalize_args(args: Any) -> str:
    """Canonical string form of a call's arguments: deep key-sort, then dump.

    ``sort_keys`` is redundant after ``_sort_json`` but harmless; ``default=str``
    keeps a stray non-JSON value (a Path, a set) from raising — the guard must
    never crash a turn just to count a call.
    """
    try:
        return json.dumps(_sort_json(args), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(args)


def _preview(canonical: str, cap: int) -> str:
    if len(canonical) <= cap:
        return canonical
    return f"{canonical[:cap]}… (+{len(canonical) - cap} more chars)"


def _detailed_reminder(tool_name: str, count: int, canonical: str, cap: int) -> str:
    return (
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- consecutive_calls: {count}\n"
        f"- arguments: {_preview(canonical, cap)}\n"
        "The repeated calls are not making progress. Do not call this tool with "
        "these exact arguments again. Inspect the latest result and choose a "
        "different action, different arguments, or finish the task if enough "
        "evidence has been gathered."
    )


class RepeatGuard:
    """Per-run repeat-call counter. One instance per ``agent_runner_loop`` run."""

    def __init__(
        self,
        thresholds: tuple[int, ...] = DEFAULT_THRESHOLDS,
        *,
        exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
        args_preview_chars: int = DEFAULT_ARGS_PREVIEW_CHARS,
    ) -> None:
        # Sorted, de-duplicated, all >= 2 — dsh's validateThresholds contract.
        cleaned = sorted({int(t) for t in thresholds if int(t) >= 2})
        self.thresholds: tuple[int, ...] = tuple(cleaned) or DEFAULT_THRESHOLDS
        self._threshold_set = set(self.thresholds)
        self.exclude = set(exclude)
        self.args_preview_chars = max(1, int(args_preview_chars))
        self._last_key: str | None = None
        self._last_count: int = 0

    def observe(self, tool_name: str, args: Any) -> str | None:
        """Advance the chain for one executed call; return a reminder, if any.

        Returns ``None`` on non-threshold counts and for excluded tools.
        """
        if tool_name in self.exclude:
            return None
        key = f"{tool_name}\x00{canonicalize_args(args)}"
        if self._last_key == key:
            self._last_count += 1
        else:
            self._last_key = key
            self._last_count = 1
        if self._last_count not in self._threshold_set:
            return None
        if self._last_count == self.thresholds[0]:
            return GENTLE_REMINDER
        return _detailed_reminder(tool_name, self._last_count, canonicalize_args(args), self.args_preview_chars)

    def reset(self) -> None:
        """Drop the chain (a fresh human request starts from zero)."""
        self._last_key = None
        self._last_count = 0


__all__ = ["RepeatGuard", "canonicalize_args", "GENTLE_REMINDER", "DEFAULT_THRESHOLDS"]