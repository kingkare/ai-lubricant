"""Text-result spill store (dsh ``spill-policy``, ported).

The agent loop runs every tool result through ``ToolRegistry._clamp``, which
head/tail-truncates long strings so the model's context budget survives a huge
``code_run`` log or a full MCP response. That *elision* throws away the middle
of the output — frequently the exact bytes the failure is in.

This module gives the clamp an alternative: when a text blob is large enough to
be elided anyway, save the **full** text to a file inside the agent workspace and
return a bounded head/tail preview plus the file path. The model can then read
the missing middle on demand with ``file_read`` instead of living with an
irrecoverable loss.

Best-effort by contract (mirrors dsh): no session owner, no writable directory,
or a write error → return ``None`` and let the caller keep its inline text. A
spill failure must never turn a successful tool call into an error or hide the
result.

``file_read`` is skipped (dsh skips its ``read`` tool): reading a file is
precisely the operation that produces huge logs, and spilling those would loop
``read → spill → read again``.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

# Subdirectory (under the agent workspace root) holding spilled tool results.
SPILL_DIRNAME = "spill"
# Tools whose results are never spilled (see module docstring for file_read).
SPILL_EXCLUDE = frozenset({"file_read"})
# Head share of the preview; the tail keeps the exit code / trailing error.
_PREVIEW_HEAD_RATIO = 0.6


def _preview(text: str, cap: int) -> str:
    """Head/tail preview within ``cap`` chars, noting how much was elided."""
    head = int(cap * _PREVIEW_HEAD_RATIO)
    tail = cap - head
    dropped = len(text) - cap
    return f"{text[:head]}\n...[elided {dropped} chars; head+tail kept]...\n{text[-tail:]}"


def spill_text(
    text: str,
    *,
    spill_dir: str | Path,
    tool_name: str,
    budget: int,
    source_id: str = "",
) -> str | None:
    """Persist ``text`` and return a preview + locator, or ``None`` to keep inline.

    ``budget`` bounds the returned replacement (preview + notice together).
    ``source_id`` (e.g. a call id) makes the filename stable for one call;
    falls back to a random hex suffix. Returns ``None`` on any storage failure.
    """
    if not isinstance(text, str) or len(text) <= budget:
        return None
    try:
        d = Path(spill_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = f"{tool_name}_{source_id or uuid.uuid4().hex[:8]}.txt"
        path = d / name
        path.write_text(text, encoding="utf-8", errors="replace")
    except OSError:
        return None

    notice = (
        f"\n\n[{len(text)} chars in full result. Saved to {path} — "
        "file_read that path if the omitted middle matters.]"
    )
    # Reserve room for the notice so the replacement itself stays in budget.
    preview_cap = max(512, budget - len(notice) - 1)
    return _preview(text, preview_cap) + notice


def _spill_field(
    value: Any,
    *,
    spill_dir: str | Path,
    tool_name: str,
    budget: int,
    source_id: str,
) -> Any:
    """Recursively replace oversized strings with previews+locators."""
    if isinstance(value, str):
        return spill_text(
            value,
            spill_dir=spill_dir,
            tool_name=tool_name,
            budget=budget,
            source_id=source_id,
        ) or value
    if isinstance(value, dict):
        return {
            key: _spill_field(
                item,
                spill_dir=spill_dir,
                tool_name=tool_name,
                budget=budget,
                source_id=source_id,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _spill_field(
                item,
                spill_dir=spill_dir,
                tool_name=tool_name,
                budget=budget,
                source_id=source_id,
            )
            for item in value
        ]
    return value


def spill_result(
    result: Any,
    *,
    spill_dir: str | Path,
    tool_name: str,
    budget: int,
    source_id: str = "",
) -> Any:
    """Spill oversized strings in ``result``; keep small/foreign values as-is.

    Excluded tools are returned untouched. On storage failure individual fields
    fall back to their original text (the clamp still bounds them downstream).
    """
    if tool_name in SPILL_EXCLUDE:
        return result
    try:
        return _spill_field(
            result,
            spill_dir=spill_dir,
            tool_name=tool_name,
            budget=budget,
            source_id=source_id,
        )
    except OSError:  # noqa: BLE001 - best-effort; keep inline text
        return result


__all__ = ["SPILL_DIRNAME", "SPILL_EXCLUDE", "spill_result", "spill_text"]