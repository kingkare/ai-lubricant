"""Tests for the dotenv write-back EBUSY fallback.

In docker compose the ./.env is bind-mounted as a single file into /app/.env;
``os.replace`` onto the mount point raises ``[Errno 16] Device or resource
busy``. The auto-generated-secret loop (NODE_CONTROL_TOKEN and friends) only
works when the write-back falls back to in-place truncating rewrite.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)


@pytest.mark.parametrize("module_name", ["server.dotenv_loader", "node_server.dotenv_loader"])
def test_update_env_vars_falls_back_when_replace_is_busy(tmp_path, monkeypatch, module_name):
    import importlib

    module = importlib.import_module(module_name)
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nB=old\n", encoding="utf-8")

    real_replace = os.replace

    def busy_replace(src, dst, *args, **kwargs):  # noqa: ANN002, ANN003
        if str(dst) == str(env_file):
            raise OSError(16, "Device or resource busy")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(module.os, "replace", busy_replace)
    module.update_env_vars({"B": "new", "C": "added"}, path=env_file)

    text = env_file.read_text(encoding="utf-8")
    assert "A=1" in text
    assert "B=new" in text
    assert "C=added" in text
    # temp files cleaned up
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("module_name", ["server.dotenv_loader", "node_server.dotenv_loader"])
def test_update_env_vars_normal_replace_still_atomic(tmp_path, monkeypatch, module_name):
    import importlib

    module = importlib.import_module(module_name)
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n", encoding="utf-8")
    module.update_env_vars({"A": "2"}, path=env_file)
    assert env_file.read_text(encoding="utf-8").strip() == "A=2"


def test_node_server_env_file_resolves_repo_dotenv():
    """node_server 的 ENV_FILE 候选在单仓布局下必须指向仓库根 .env（/app/.env）。"""
    from node_server import dotenv_loader

    assert dotenv_loader.ENV_FILE.name == ".env"
    assert dotenv_loader.ENV_FILE.parent.name in ("ai-lubricant", "node_server") or (
        dotenv_loader.ENV_FILE.parent == Path(__file__).resolve().parent.parent
    )
