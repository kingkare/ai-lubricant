"""Writable layout for native dependency binaries, data, configs and logs."""
from __future__ import annotations

import os
from pathlib import Path


def root() -> Path:
    """Return the writable native-runtime root.

    ``NATIVE_DEPS_ROOT`` is useful for supervisord/systemd deployments; desktop
    uses the same per-user directory as the existing launcher.
    """
    override = os.environ.get("NATIVE_DEPS_ROOT", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        try:
            from desktop.paths import user_data_dir

            path = user_data_dir() / "native-deps"
        except Exception:
            base = os.environ.get("XDG_DATA_HOME")
            path = (Path(base) if base else Path.home() / ".local" / "share") / "ai-lubricant" / "native-deps"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bin_dir(kind: str) -> Path:
    path = root() / "bin" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir(kind: str) -> Path:
    path = root() / "data" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    path = root() / "config"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
