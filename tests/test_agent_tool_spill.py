"""Tests for text-result spill (agent/tool_spill.py)."""
import os
import sys
from pathlib import Path

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.tool_spill import spill_result, spill_text

BUDGET = 15000


@pytest.fixture()
def spill_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workspace" / "spill"
    return d


def test_small_text_is_not_spilled(spill_dir: Path) -> None:
    assert spill_text("short", spill_dir=spill_dir, tool_name="code_run", budget=BUDGET) is None
    assert not spill_dir.exists()


def test_oversized_text_spills_with_locator(spill_dir: Path) -> None:
    out = spill_text("x" * 40000, spill_dir=spill_dir, tool_name="code_run", budget=BUDGET)
    assert out is not None
    assert "Saved to" in out
    # The saved file holds the FULL text, not the preview.
    saved = list(spill_dir.glob("code_run_*.txt"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "x" * 40000


def test_preview_fits_near_budget(spill_dir: Path) -> None:
    out = spill_text("y" * 40000, spill_dir=spill_dir, tool_name="code_run", budget=BUDGET)
    # Preview + notice stays at (or just under) the budget; downstream clamp
    # absorbs the elision-marker slack.
    assert len(out) <= BUDGET + 100


def test_file_read_is_never_spilled(spill_dir: Path) -> None:
    big = {"stdout": "z" * 40000}
    out = spill_result(big, spill_dir=spill_dir, tool_name="file_read", budget=BUDGET)
    assert out["stdout"] == big["stdout"]  # untouched
    assert not spill_dir.exists()


def test_dict_result_spills_only_large_fields(spill_dir: Path) -> None:
    out = spill_result(
        {"status": "ok", "exit_code": 0, "stdout": "a" * 40000},
        spill_dir=spill_dir, tool_name="code_run", budget=BUDGET,
    )
    assert out["status"] == "ok"
    assert out["exit_code"] == 0
    assert "Saved to" in out["stdout"]


def test_nested_dict_and_list_spill(spill_dir: Path) -> None:
    out = spill_result(
        {"data": {"big": "b" * 40000}, "items": ["c" * 40000]},
        spill_dir=spill_dir, tool_name="code_run", budget=BUDGET,
    )
    assert "Saved to" in out["data"]["big"]
    assert "Saved to" in out["items"][0]


def test_multiple_large_fields_produce_multiple_files(spill_dir: Path) -> None:
    spill_result(
        {"stdout": "a" * 40000, "stderr": "b" * 40000},
        spill_dir=spill_dir, tool_name="code_run", budget=BUDGET,
    )
    assert len(list(spill_dir.glob("code_run_*.txt"))) == 2


def test_unwritable_dir_falls_back_to_inline(tmp_path: Path) -> None:
    # A file where the spill dir should be → mkdir fails → keep inline (best-effort).
    blocker = tmp_path / "spill"
    blocker.write_text("not a dir", encoding="utf-8")
    out = spill_result(
        {"stdout": "a" * 40000},
        spill_dir=blocker, tool_name="code_run", budget=BUDGET,
    )
    assert out["stdout"] == "a" * 40000  # inline preserved
