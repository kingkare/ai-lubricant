"""mcp_builtin/site_server.py（SiteServer：静态方法类，浏览器操作原语）测试。

覆点：
- ContextVar 路由（current_token 设到 client_id）
- open_tab：开 tab → 等注册 → 返回 session_key；as_default 记常驻 tab
- default_tab：无则开、有则复用
- run_js：执行 JS → 解析 {status:success,data} → 返回值；非 success 抛 RuntimeError；
  用/建常驻 tab；可显式指定 tab_id
- list_tabs / close_tab（幂等）
- navigate：经 JS location.href
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from contextvars import ContextVar

import pytest


def _install_fake_cdp(monkeypatch, *, open_tab_id="100", exec_data="OK", exec_status="success",
                      known_tabs=None, calls=None):
    calls = calls if calls is not None else {}

    async def fake_open_tab(url="", new_window=False, active=True):
        calls.setdefault("opens", []).append({"url": url, "active": active})
        calls["open_url"] = url
        return json.dumps({"status": "success", "tab": {"id": int(open_tab_id)}})

    async def fake_get_tabs():
        tabs = known_tabs if known_tabs is not None else [{"id": str(open_tab_id)}]
        return json.dumps({"tabs": tabs})

    async def fake_exec_js(script, switch_tab_id="", no_monitor=False):
        calls["exec"] = {"script": script, "no_monitor": no_monitor}
        calls["exec_token"] = cdp_srv.current_token.get("")
        return json.dumps({"status": exec_status, "js_return": exec_data})

    async def fake_close_tab(tab_id=""):
        calls.setdefault("closes", []).append(tab_id)
        return json.dumps({"status": "success"})

    cdp_pkg = types.ModuleType("mcp_builtin.cdp_bridge")
    cdp_srv = types.ModuleType("mcp_builtin.cdp_bridge.server")
    cdp_srv.current_token = ContextVar("current_token", default="")
    cdp_srv.browser_open_tab = fake_open_tab
    cdp_srv.browser_get_tabs = fake_get_tabs
    cdp_srv.browser_execute_js = fake_exec_js
    cdp_srv.browser_close_tab = fake_close_tab
    cdp_pkg.server = cdp_srv
    tmd = types.ModuleType("mcp_builtin.cdp_bridge.TMWebDriver")
    tmd.current_session_id = ContextVar("current_session_id", default="")
    cdp_pkg.TMWebDriver = tmd

    monkeypatch.setitem(sys.modules, "mcp_builtin.cdp_bridge", cdp_pkg)
    monkeypatch.setitem(sys.modules, "mcp_builtin.cdp_bridge.server", cdp_srv)
    monkeypatch.setitem(sys.modules, "mcp_builtin.cdp_bridge.TMWebDriver", tmd)
    return calls


@pytest.fixture(autouse=True)
def _clear_default_tabs():
    from mcp_builtin.site_server import _DEFAULT_TABS
    _DEFAULT_TABS.clear()
    yield
    _DEFAULT_TABS.clear()


def test_open_tab_registers_and_returns_session_key(monkeypatch):
    from mcp_builtin.site_server import SiteServer, _DEFAULT_TABS
    calls = _install_fake_cdp(monkeypatch)
    sk = asyncio.run(SiteServer.open_tab("cdp-1", "https://x.example"))
    assert sk == "cdp-1:100"
    assert calls["open_url"] == "https://x.example"
    assert _DEFAULT_TABS["cdp-1"] == "cdp-1:100"   # as_default=True 记常驻


def test_open_tab_not_default(monkeypatch):
    from mcp_builtin.site_server import SiteServer, _DEFAULT_TABS
    _install_fake_cdp(monkeypatch)
    asyncio.run(SiteServer.open_tab("cdp-1", as_default=False))
    assert "cdp-1" not in _DEFAULT_TABS


def test_default_tab_opens_once_then_reuses(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    calls = _install_fake_cdp(monkeypatch)
    a = asyncio.run(SiteServer.default_tab("cdp-1"))
    b = asyncio.run(SiteServer.default_tab("cdp-1"))
    assert a == b == "cdp-1:100"
    assert len(calls.get("opens", [])) == 1   # 只开一次


def test_run_js_returns_value_and_routes(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    calls = _install_fake_cdp(monkeypatch, exec_data={"verify_param": "V" * 300})
    out = asyncio.run(SiteServer.run_js("cdp-7", "return solve()"))
    assert out == {"verify_param": "V" * 300}
    assert calls["exec"]["no_monitor"] is True
    assert calls["exec"]["script"] == "return solve()"
    assert calls["exec_token"] == "cdp-7"   # ContextVar 路由到 client_id


def test_run_js_uses_default_tab_without_explicit_tab(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    calls = _install_fake_cdp(monkeypatch)
    asyncio.run(SiteServer.run_js("cdp-1", "return 1"))
    assert len(calls.get("opens", [])) == 1   # 无 tab → 自动开常驻 tab


def test_run_js_explicit_tab_skips_auto_open(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    calls = _install_fake_cdp(monkeypatch)
    asyncio.run(SiteServer.run_js("cdp-1", "return 1", tab_id="cdp-1:999"))
    assert "opens" not in calls   # 显式 tab 不再开


def test_run_js_failure_raises(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    _install_fake_cdp(monkeypatch, exec_status="error")
    with pytest.raises(RuntimeError, match="run_js 失败"):
        asyncio.run(SiteServer.run_js("cdp-1", "return 1"))


def test_list_tabs(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    _install_fake_cdp(monkeypatch, known_tabs=[{"id": "100"}, {"id": "cdp-1:200"}])
    tabs = asyncio.run(SiteServer.list_tabs("cdp-1"))
    assert tabs == {"cdp-1:100", "cdp-1:200"}


def test_close_tab_default_and_idempotent(monkeypatch):
    from mcp_builtin.site_server import SiteServer, _DEFAULT_TABS
    calls = _install_fake_cdp(monkeypatch)
    asyncio.run(SiteServer.open_tab("cdp-1"))
    asyncio.run(SiteServer.close_tab("cdp-1"))
    assert calls["closes"] == ["cdp-1:100"]
    assert "cdp-1" not in _DEFAULT_TABS
    # 幂等：常驻已清，再 close 不再调
    calls.pop("closes", None)
    asyncio.run(SiteServer.close_tab("cdp-1"))
    assert "closes" not in calls


def test_navigate_via_js(monkeypatch):
    from mcp_builtin.site_server import SiteServer
    calls = _install_fake_cdp(monkeypatch)
    asyncio.run(SiteServer.navigate("cdp-1", "https://zcode.z.ai"))
    assert "location.href" in calls["exec"]["script"]
    assert "https://zcode.z.ai" in calls["exec"]["script"]
