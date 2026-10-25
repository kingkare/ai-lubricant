"""Prompt assets for the testing platform (methodology text fed to the Agent).

Kept as Markdown so they can be edited without touching code. ``load_prompt``
reads one by name; missing files raise (never silently empty).
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Load a prompt asset by name (without ``.md``).

    Raises ``FileNotFoundError`` when the asset is missing — a silently empty
    methodology would make the Agent improvise, which we never want.
    """
    path = _ROOT / f"{name}.md"
    return path.read_text(encoding="utf-8")


__all__ = ["load_prompt"]
