"""mcp_sdk.CdpClient 单测：方法名→工具名映射、参数透传、token 透传、holder 注入。

底层 MCPManager._sse_call 是进程内直调 sse_gateway._handle_rpc 的唯一出口，这里整体
打桩，只验证 SDK 把方法调用折成了正确的 tools/call 信封。
"""
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.mcp_client import MCPManager  # noqa: E402
from mcp_sdk import CdpClient  # noqa: E402


def _capture(monkeypatch, calls, *, holder_seen, content=None):
    """把 MCPManager._gateway_rpc 换成记录 (service, tool, args) 的替身。

    只打桩最底层的网关调用，让真实的 _sse_call 跑起来——它负责 set
    current_cdp_holder ContextVar（SDK 复用 MCP 的 tab 租约语义就靠这条）。
    """
    async def fake_gateway_rpc(service_name, method, params, token):
        calls.append((service_name, params.get("name"), dict(params.get("arguments") or {})))
        holder_seen.append(_current_holder())
        return {"content": content if content is not None else [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(MCPManager, "_gateway_rpc", staticmethod(fake_gateway_rpc))


def _current_holder() -> str:
    from mcp_runtime.plugin_loader import current_cdp_holder
    return current_cdp_holder.get("")


@pytest.mark.asyncio
async def test_get_tabs_maps_to_browser_get_tabs(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    client = CdpClient(token="tok-1")
    result = await client.get_tabs()

    assert calls == [("cdp-bridge", "browser_get_tabs", {})]
    assert result == [{"type": "text", "text": "ok"}]
    # SDK 的 holder 与 agent 的 agent:{uuid} 区分开，但仍是一个非空租约。
    assert holders and holders[0].startswith("cdp_sdk:")


@pytest.mark.asyncio
async def test_execute_js_passes_script_and_flags(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    client = CdpClient()
    await client.execute_js("document.title", no_monitor=True)

    assert calls == [("cdp-bridge", "browser_execute_js", {
        "script": "document.title",
        "switch_tab_id": "",
        "no_monitor": True,
    })]


@pytest.mark.asyncio
async def test_navigate_and_screenshot(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    client = CdpClient()
    await client.navigate("https://example.com")
    await client.screenshot(tab_id="t1", path="workspace/x.png")

    assert calls[0] == ("cdp-bridge", "browser_navigate", {"url": "https://example.com"})
    assert calls[1] == ("cdp-bridge", "browser_screenshot", {"tab_id": "t1", "path": "workspace/x.png"})


@pytest.mark.asyncio
async def test_token_forwarded_into_service_tokens(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    client = CdpClient(token="secret-tok")
    # token 就是 MCP service_tokens 那套口径。
    assert client._mgr._service_tokens["cdp-bridge"] == "secret-tok"


@pytest.mark.asyncio
async def test_call_escape_hatch(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    client = CdpClient()
    await client.call("browser_network_get", {"tab_id": "t1"})

    assert calls == [("cdp-bridge", "browser_network_get", {"tab_id": "t1"})]


@pytest.mark.asyncio
async def test_network_start_get_stop(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    client = CdpClient()
    await client.network_start(url_pattern="api")
    await client.network_get()
    await client.network_stop()

    assert [c[1] for c in calls] == [
        "browser_network_start", "browser_network_get", "browser_network_stop",
    ]
    assert calls[0][2] == {"tab_id": "", "url_pattern": "api"}


@pytest.mark.asyncio
async def test_close_releases_holder(monkeypatch):
    calls, holders = [], []
    _capture(monkeypatch, calls, holder_seen=holders)
    released = []

    class _FakeMgr:
        lease_id = "cdp_sdk:test"

        async def close(self):
            released.append(self.lease_id)

    client = CdpClient()
    client._mgr = _FakeMgr()
    await client.close()
    assert released == ["cdp_sdk:test"]
