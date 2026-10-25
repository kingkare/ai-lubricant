"""SiteServer.list_clients —— 浏览器发现 + owner 过滤（安全边界）。"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from mcp_builtin import site_server as ss  # noqa: E402
from mcp_builtin.site_server import SiteServer  # noqa: E402


def _patch_driver(monkeypatch, contexts):
    """driver.snapshot_contexts() 返回给定结构。"""
    class _Driver:
        def snapshot_contexts(self):
            return contexts
    monkeypatch.setattr(ss, "getattr", getattr, raising=False)
    import mcp_builtin.cdp_bridge.server as cdp
    monkeypatch.setattr(cdp, "driver", _Driver(), raising=False)


def _patch_owned(monkeypatch, owned_ids, calls=None):
    import builtin_tool_store

    async def _fake_list(resource_type=None, owner_user_id=None, enabled=None, *, include_secrets=False):
        assert resource_type == "cdp_client"
        if calls is not None:
            calls.append(owner_user_id)
        return [{"id": i} for i in owned_ids]

    monkeypatch.setattr(builtin_tool_store, "list_resources", _fake_list)


def test_list_clients_filters_to_owner(monkeypatch):
    calls = []
    _patch_owned(monkeypatch, [11], calls)
    _patch_driver(monkeypatch, [{"clients": [
        {"client_id": "11", "name": "A", "connected": True, "pages": [{"id": "p1", "url": "u"}]},
        {"client_id": "99", "name": "foreign", "connected": True, "pages": []},
    ]}])
    out = asyncio.run(SiteServer.list_clients("user-1"))
    ids = [c["client_id"] for c in out]
    assert ids == ["11"]          # 99 不在 owner 名下 → 不列
    assert out[0]["connected"] is True
    assert calls == ["user-1"]    # owner 透传给 list_resources


def test_list_clients_empty_when_no_owned(monkeypatch):
    _patch_owned(monkeypatch, [])
    _patch_driver(monkeypatch, [])
    assert asyncio.run(SiteServer.list_clients("user-x")) == []


def test_list_clients_offline_client_still_listed(monkeypatch):
    """owner 名下有但当前不在线 → 仍列出（connected=False）。"""
    _patch_owned(monkeypatch, [11, 12])
    _patch_driver(monkeypatch, [{"clients": [{"client_id": "11", "connected": True, "pages": []}]}])
    out = asyncio.run(SiteServer.list_clients("user-1"))
    by_id = {c["client_id"]: c for c in out}
    assert by_id["11"]["connected"] is True
    assert by_id["12"]["connected"] is False   # 12 未在线但仍列出
