"""Tests for the advisory repeat-tool-call guard (agent/repeat_guard.py)."""
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.repeat_guard import (
    GENTLE_REMINDER,
    RepeatGuard,
    canonicalize_args,
)


def test_canonicalize_ignores_key_order() -> None:
    a = canonicalize_args({"type": "python", "code": "ls"})
    b = canonicalize_args({"code": "ls", "type": "python"})
    assert a == b


def test_canonicalize_sorts_nested_keys() -> None:
    assert canonicalize_args({"x": {"b": 1, "a": 2}}) == canonicalize_args({"x": {"a": 2, "b": 1}})


def test_canonicalize_treats_different_values_as_different() -> None:
    assert canonicalize_args({"code": "ls"}) != canonicalize_args({"code": "pwd"})


def test_gentle_reminder_at_first_threshold_only() -> None:
    g = RepeatGuard()
    assert g.observe("code_run", {"code": "ls"}) is None            # 1
    assert g.observe("code_run", {"code": "ls"}) is None            # 2
    assert g.observe("code_run", {"code": "ls"}) == GENTLE_REMINDER  # 3 == thresholds[0]


def test_key_order_swap_does_not_reset_chain() -> None:
    g = RepeatGuard()
    g.observe("code_run", {"code": "ls", "type": "python"})
    g.observe("code_run", {"type": "python", "code": "ls"})
    # 3rd hit (same canonical args, reordered) → gentle tier.
    assert g.observe("code_run", {"code": "ls", "type": "python"}) == GENTLE_REMINDER


def test_detailed_reminder_names_tool_count_and_args() -> None:
    g = RepeatGuard()
    for _ in range(4):
        g.observe("web", {"operation": "read_page", "url": "http://x"})
    msg = g.observe("web", {"url": "http://x", "operation": "read_page"})  # 5th
    assert msg is not None and msg != GENTLE_REMINDER
    assert "tool: web" in msg
    assert "consecutive_calls: 5" in msg
    assert "http://x" in msg


def test_different_call_resets_chain() -> None:
    g = RepeatGuard()
    g.observe("code_run", {"code": "ls"})
    g.observe("code_run", {"code": "ls"})
    # Different args → count restarts at 1, below threshold.
    assert g.observe("code_run", {"code": "pwd"}) is None


def test_denied_calls_still_count() -> None:
    # The guard observes by (name, args) before knowing the outcome, so a model
    # hammering a denied call advances the chain the same as any other call.
    g = RepeatGuard()
    for _ in range(2):
        g.observe("code_run", {"code": "rm -rf /"})
    assert g.observe("code_run", {"code": "rm -rf /"}) == GENTLE_REMINDER


def test_excluded_tool_is_transparent() -> None:
    g = RepeatGuard()
    for _ in range(10):
        assert g.observe("update_working_checkpoint", {"goal": "x"}) is None


def test_reset_clears_chain() -> None:
    g = RepeatGuard()
    g.observe("code_run", {"code": "ls"})
    g.observe("code_run", {"code": "ls"})
    g.reset()
    assert g.observe("code_run", {"code": "ls"}) is None  # back to count 1


def test_custom_thresholds_escalate_from_configured_first() -> None:
    g = RepeatGuard(thresholds=(2, 4))
    assert g.observe("web", {"url": "u"}) is None       # 1
    assert g.observe("web", {"url": "u"}) == GENTLE_REMINDER  # 2 == first threshold
    assert g.observe("web", {"url": "u"}) is None       # 3
    assert "consecutive_calls: 4" in g.observe("web", {"url": "u"})  # 4


def test_unsorted_duplicate_thresholds_are_normalized() -> None:
    # thresholds[0] is the gentle tier regardless of the input order.
    g = RepeatGuard(thresholds=(8, 3, 5, 3))
    g.observe("web", {"url": "u"})
    g.observe("web", {"url": "u"})
    assert g.observe("web", {"url": "u"}) == GENTLE_REMINDER


def test_invalid_thresholds_fall_back_to_defaults() -> None:
    # All < 2 are rejected → cleaned set empty → default (3, 5, 8).
    g = RepeatGuard(thresholds=(0, 1))
    assert g.thresholds == (3, 5, 8)


def test_canonicalize_never_raises_on_non_json() -> None:
    from pathlib import Path

    # A stray non-JSON value must not crash a turn just to count a call.
    assert isinstance(canonicalize_args({"p": Path("/tmp/x")}), str)
    assert isinstance(canonicalize_args({"s": {1, 2}}), str)