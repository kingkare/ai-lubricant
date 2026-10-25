"""Focused contracts for CDP client auth protocol 2 and user/client/page ownership."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from mcp.configuration import RESOURCE_ADAPTERS, ResourceContext
from mcp_builtin.cdp_bridge.TMWebDriver import TMWebDriver
from mcp_builtin.cdp_bridge import server as cdp_server
from mcp_runtime.builtin_plugins import cdp_bridge_plugin
from mcp_runtime.plugin_loader import PluginContext, current_request_token
from mcp_runtime.sse_gateway import (
    _authenticate_ext_frame,
    _chat_identity,
    _handle_chat_frame,
    _owned_chat_page,
    _route_ext_frame,
)
import mcp_runtime.sse_gateway as sse_gateway
import mcp_runtime.configuration as runtime_config
import mcp_plugin_store
from fastapi import HTTPException

from agent import cdp_chat_service, scene_context


class FakeClient:
    def __init__(self):
        self._user_id = self._client_id = self._token = None
        self.closed = []
        self.sent = []

    def send_message(self, payload): self.sent.append(json.loads(payload))
    def close_auth(self, code, reason): self.closed.append((code, reason))


class FakeWebSocket:
    def __init__(self): self.frames = []
    async def send_text(self, raw): self.frames.append(json.loads(raw))


def client_row(token="secret", *, row_id=7, user_id=3, enabled=True):
    # user_ids 是「可操作用户」列表（方向翻转后）；driver 只用 token_hash + instance_key。
    return {"id": row_id, "instance_key": str(row_id), "name": "Chrome", "user_ids": [user_id],
            "enabled": enabled, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "token_hint": "sec...ret"}


def test_driver_authenticates_hash_and_builds_client_pages():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    client = FakeClient()
    config = driver.authenticate_client("secret", "7")
    assert config and config["client_id"] == "7"
    driver.bind_client(client, config)
    driver.ingest_message(json.dumps({"type": "tabs_update", "tabs": [{"id": 12, "url": "https://example.test", "title": "x"}]}), client)
    # 会话池按 client_id 隔离：context key = client_id。
    ctx = driver.get_context("7")
    assert set(ctx.clients) == {"7"}
    assert set(ctx.clients["7"].pages) == {"7:12"}
    assert driver.get_all_sessions("7")[0]["id"] == "7:12"


def test_authenticated_ext_ready_is_accepted_as_compatibility_alias():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    client = FakeClient(); driver.bind_client(client, driver.authenticate_client("secret"))
    driver.ingest_message(json.dumps({"type": "ext_ready", "client_id": "evil", "tabs": [
        {"id": 12, "url": "https://example.test"},
    ]}), client)
    assert set(driver.get_context("7").sessions) == {"7:12"}


def test_composite_session_resolution_routes_commands_to_owning_client():
    rows = [client_row("secret-a", row_id=7), client_row("secret-b", row_id=8)]
    driver = TMWebDriver(clients=rows, external_ws=True)
    clients = {}
    for token, client_id in (("secret-a", "7"), ("secret-b", "8")):
        client = FakeClient()
        driver.bind_client(client, driver.authenticate_client(token))
        driver.ingest_message(json.dumps({"type": "tabs_update", "tabs": [
            {"id": 12, "url": f"https://{client_id}.example.test"},
        ]}), client)
        clients[client_id] = client

    # 会话池按 client_id 隔离，token= 语义现在是 client_id。
    page, raw_tab_id = driver.raw_tab_id("8:12", token="8")
    assert page.id == "8:12"
    assert page.ws_client is clients["8"]
    assert raw_tab_id == 12


def test_all_tab_targeted_tools_use_canonical_resolver(monkeypatch):
    calls = []

    class Driver:
        def raw_tab_id(self, session_id, token=None):
            calls.append((session_id, token))
            return type("Page", (), {"id": "client-a:12"})(), 12

        def get_context(self, token):
            return type("Context", (), {"default_session_id": None})()

    page, raw_tab_id = cdp_server._resolve_tab(Driver(), "client-a:12", 3)
    assert page.id == "client-a:12"
    assert raw_tab_id == 12
    assert calls == [("client-a:12", 3)]

    source = Path(cdp_server.__file__).read_text(encoding="utf-8")
    # server.py 的 Python 函数名保持原名（对外 advertised name 由 cdp_bridge_plugin
    # 的 _TOOLS 决定，已改成 watch/read/unwatch_network）；这里查的是源文件里的
    # 函数定义，故仍用 browser_network_*。
    for tool in ("browser_focus_tab", "browser_batch", "browser_network_start", "browser_network_get", "browser_network_stop", "browser_screenshot"):
        body = source.split(f"async def {tool}", 1)[1].split("\n@mcp.tool()", 1)[0]
        assert "_extension_command(" in body or "_resolve_tab(" in body


def test_resolve_session_implicit_falls_back_when_default_is_dead():
    """隐式 resolve_session(None) 遇到 default 页已死时退到别的活跃页，而不是抛「未连接」。

    这正是 duck.ai 收割报 ``浏览器 1 收割异常: ValueError: 会话ID … 未连接 · 43ms`` 的根因：
    browser_open_tab 要经 default 页中继 chrome.tabs.create，而 default 恰好是刚关掉的
    duck.ai 页时，在 resolve_session 直接抛 → open_tab 43ms 秒死 → duckai「换页重试」永远
    开不出新页 → 归一 502。这里必须退到仍在线的另一页把 tab 开出来。
    """
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    client = FakeClient()
    driver.bind_client(client, driver.authenticate_client("secret"))
    driver.ingest_message(json.dumps({"type": "tabs_update", "tabs": [
        {"id": 1944576787, "url": "https://duck.ai/", "title": "duck"},
        {"id": 42, "url": "https://other.test/", "title": "other"},
    ]}), client)
    ctx = driver.get_context("7")
    # 把 default 指到那张 duck.ai 页，再模拟它被关掉/回收（mark_disconnected）。
    ctx.default_session_id = "7:1944576787"
    ctx.sessions["7:1944576787"].mark_disconnected()

    # 隐式（不指名 session_id）：default 死了 → 退到仍活跃的 7:42，不抛。
    resolved = driver.resolve_session(token="7")
    assert resolved.id == "7:42"

    # 显式指名死页：仍如实抛「未连接」，好让上层（duckai _harvest_on_page）走换页重试。
    with pytest.raises(ValueError, match="未连接"):
        driver.resolve_session("7:1944576787", token="7")


def test_execute_js_does_not_hijack_active_tab_when_pinned_tab_is_dead():
    """钉了具体页（ContextVar/实参）而该页已死时必须抛，不能静默劫持 active[0] 跑到别的页。

    否则 duckai「钉死本次的页，杜绝串台」的契约被撕——收割到别的账号/站点的头。
    """
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    client = FakeClient()
    driver.bind_client(client, driver.authenticate_client("secret"))
    driver.ingest_message(json.dumps({"type": "tabs_update", "tabs": [
        {"id": 1944576787, "url": "https://duck.ai/", "title": "duck"},
        {"id": 42, "url": "https://other.test/", "title": "other"},
    ]}), client)
    from mcp_builtin.cdp_bridge.TMWebDriver import current_session_id
    ctx = driver.get_context("7")
    ctx.sessions["7:1944576787"].mark_disconnected()
    tok = current_session_id.set("7:1944576787")
    try:
        # 钉的死页 + 还有另一张活跃页：旧逻辑会静默用 active[0]（串台），新逻辑抛。
        with pytest.raises(ValueError, match="未连接"):
            driver.execute_js("return 1", token="7")
    finally:
        current_session_id.reset(tok)


def test_same_token_rejects_second_active_connection():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    config = driver.authenticate_client("secret")
    driver.bind_client(FakeClient(), config)
    with pytest.raises(ValueError, match="already connected"):
        driver.bind_client(FakeClient(), config)


def test_stale_connection_is_preempted_by_new_one():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    config = driver.authenticate_client("secret")
    first = FakeClient(); driver.bind_client(first, config)
    # 在任连接已长时间无入站帧 = 半开死连接/被遗忘的孤儿 socket：新连接直接抢占
    # 槽位并踢掉旧连接，而不是把新连接卡在 already_connected 里等服务端空闲读超时。
    first._last_in = time.time() - 60
    second = FakeClient()
    bound = driver.bind_client(second, config)
    assert bound.ws_client is second
    assert first.closed == [(4403, "slot preempted by new connection")]
    # 旧连接被 unregister：新连接的槽位不被晚到的旧 finally 抹掉。
    assert driver.get_context("7").clients["7"].ws_client is second


def test_reload_revocation_disconnects_live_client():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    client = FakeClient(); driver.bind_client(client, driver.authenticate_client("secret"))
    driver.apply_clients([])
    assert client.closed == [(4403, "authorization revoked")]
    assert "7" not in driver.get_context("7").clients


def test_reload_rotation_disconnects_live_client():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    client = FakeClient(); driver.bind_client(client, driver.authenticate_client("secret"))
    rotated = client_row("new-secret")
    driver.apply_clients([rotated])
    assert client.closed == [(4403, "authorization revoked")]
    assert driver.authenticate_client("secret") is None
    assert driver.authenticate_client("new-secret")["client_id"] == "7"


def test_auth_protocol_2_and_legacy_first_frame_rejection():
    async def scenario():
        driver = TMWebDriver(clients=[client_row()], external_ws=True)
        ws, adapter = FakeWebSocket(), FakeClient()
        assert await _authenticate_ext_frame(json.dumps({"type": "auth", "protocol": 2, "token": "secret", "client_id": "7"}), ws, driver, adapter)
        assert ws.frames[-1]["type"] == "auth_ok"
        ws2, adapter2 = FakeWebSocket(), FakeClient()
        assert not await _authenticate_ext_frame(json.dumps({"type": "ext_ready", "token": "secret"}), ws2, driver, adapter2)
        assert ws2.frames[-1]["type"] == "auth_error"
    asyncio.run(scenario())


def test_cdp_client_create_returns_plaintext_once_and_persists_hash(monkeypatch):
    adapter = RESOURCE_ADAPTERS["shared"]
    ctx = ResourceContext({"id": 2}, {"cardinality": "collection", "storage": {"configType": "cdp_client", "handler": "cdp-client"}})
    saved = {}
    async def create(service_id, config_type, value, **kwargs):
        saved.update(service_id=service_id, config_type=config_type, value=value, **kwargs)
        return {"id": 9, "service_id": service_id, "config_type": config_type, "instance_key": "9", "revision": 1,
                "token_hash": kwargs["token_hash"], "token_hint": kwargs["token_hint"], **value}
    monkeypatch.setattr(mcp_plugin_store, "generate_runtime_token", lambda: "cdp_once")
    monkeypatch.setattr(mcp_plugin_store, "create_runtime_config", create)
    monkeypatch.setattr(mcp_plugin_store, "get_mcp_user", lambda user_id: _coro({"id": user_id, "enabled": True}))
    monkeypatch.setattr(mcp_plugin_store, "recompute_service_users_from_resources", lambda service_id: _coro([4]))
    # agent 授权已单轨化到 agent 侧 grants：cdp_client 创建不再接受 agent_ids，
    # 传入也直接丢弃（见 SharedConfigAdapter._validate_users_agents）。
    result = asyncio.run(adapter.create(ctx, {"name": "Chrome", "user_ids": [4], "agent_ids": [1], "enabled": True}))
    assert result["token"] == "cdp_once"
    assert "token_hash" not in result
    assert result["client_id"] == "9"
    assert saved["token_hash"] == hashlib.sha256(b"cdp_once").hexdigest()
    assert saved["token_hint"] == "cdp_once...once"
    assert saved["value"].get("token") is None
    assert "agent_ids" not in saved["value"]


def test_plugin_driver_config_keeps_enabled_clients_with_token_hash():
    cfg = cdp_bridge_plugin._driver_config({"clients": [
        client_row(user_id="4"),
        {**client_row("other", row_id=8), "enabled": False},
    ]}, True, set())
    assert len(cfg["clients"]) == 1
    assert cfg["clients"][0]["instance_key"] == "7"


def test_tool_wrapper_routes_token_to_client_id(monkeypatch):
    # _wrap 现在用 driver 自带的 authenticate_client（纯内存 hash 查找）把请求 token
    # 解析成 client_id，与浏览器扩展 WS 握手走同一条路径；不再用预计算的明文映射表。
    async def tool():
        return json.dumps({"ok": True, "client_id": cdp_server.current_token.get()})

    driver = TMWebDriver(clients=[client_row("mcp-token")], external_ws=True)
    monkeypatch.setattr(cdp_server, "driver", driver)

    context = PluginContext("cdp-bridge")
    request_token = current_request_token.set("mcp-token")
    try:
        result = asyncio.run(cdp_bridge_plugin._wrap(tool)({}, context))
    finally:
        current_request_token.reset(request_token)
    # client_row 默认 instance_key="7" → 解析出的 client_id 为 "7"。
    assert result == {"ok": True, "client_id": "7"}


def test_fill_credential_is_advertised_and_routed_to_the_content_script():
    """The three extension layers of fill_credential must stay wired:
    the plugin advertises `browser_fill_credential`, background.js forwards the
    command to the tab, and content.js fills it in place (never returning the value)."""
    advertised = {name: fn for name, fn, _desc, _params in cdp_bridge_plugin._TOOLS}
    assert advertised.get("browser_fill_credential") == "browser_fill_credential"
    assert "browser_fill_credential" in cdp_bridge_plugin._load_cdp_bridge_tools()

    bridge_dir = Path(cdp_server.__file__).with_name("tmwd_cdp_bridge")
    background = bridge_dir.joinpath("background.js").read_text(encoding="utf-8")
    assert "msg.cmd === 'fill_credential'" in background
    assert "chrome.tabs.sendMessage(Number(tabId), msg)" in background

    content = bridge_dir.joinpath("content.js").read_text(encoding="utf-8")
    assert "msg.cmd !== 'fill_credential'" in content
    assert "function handleFillCredential" in content
    assert "function findOtpField" in content
    # the password value is resolved inside the extension from its own store
    assert "sendPwdRequest('pwd_get'" in content


def test_fill_credential_receipt_never_carries_a_secret():
    """`build_fill_receipt` is the one gate between the extension and the model.

    Even when the extension echoes back the value it typed (a code / a password),
    no secret-shaped key may survive into the receipt the model sees. This is the
    invariant that lets fill_credential run without an approval gate."""
    secret_shaped = {
        "ok": True, "filled": True,
        # hostile: a buggy extension nests the value inside `field`
        "field": {"id": "otp", "type": "tel", "value": "123456", "code": "123456"},
        # hostile: the extension echoes the value it typed
        "code": "123456", "password": "hunter2", "value": "123456",
        "secret": "JBSWY3DPEHPK3PXP", "secret_enc": "deadbeef",
    }
    otp = cdp_server.build_fill_receipt("otp", {"issuer": "GitHub", "account": "me", "expiresAt": 1700000021}, secret_shaped)
    assert otp["filled"] is True and otp["kind"] == "otp"
    assert otp["entry"] == {"issuer": "GitHub", "account": "me"}
    assert otp["expiresAt"] == 1700000021
    # the field survives but its secret-shaped keys do not (recursive scrub)
    assert otp["field"] == {"id": "otp", "type": "tel"}
    for key in ("code", "password", "value", "secret", "secret_enc"):
        assert key not in otp
        assert key not in otp.get("field", {})

    pwd = cdp_server.build_fill_receipt(
        "password", {"username": "me@x.test", "domain": "x.test"}, secret_shaped)
    assert pwd["filled"] is True and pwd["kind"] == "password"
    assert pwd["entry"] == {"username": "me@x.test", "domain": "x.test"}
    for key in ("code", "password", "value", "secret", "secret_enc"):
        assert key not in pwd

    # failure path: an error reason survives, still no secret.
    fail = cdp_server.build_fill_receipt("otp", None, {"ok": False, "filled": False, "error": "tab not reachable", "code": "999999"})
    assert fail["status"] == "error" and fail["filled"] is False
    assert fail["reason"] == "tab not reachable"
    assert "code" not in fail


def test_extension_contract_authenticates_before_tabs_and_removes_legacy_token():
    background = Path(cdp_server.__file__).with_name("tmwd_cdp_bridge").joinpath("background.js").read_text(encoding="utf-8")
    auth_send = "socket.send(JSON.stringify({ type: 'auth', protocol: 2, token: bridgeConfig.clientToken }))"
    assert auth_send in background
    assert background.index(auth_send) < background.index("await sendReadyFrames(socket)")
    assert "data.type === 'auth_ok' && data.protocol === 2" in background
    assert "type: 'tabs_update'" in background
    assert "chrome.storage.local.remove('bridgeToken')" in background
    assert "clientToken: usableClientToken(stored.bridgeToken)" not in background
    assert "reprovisionRequired = !nextConfig.clientToken" in background
    assert "const CONNECTION_TIMEOUT_MS = 10000" in background
    assert "const AUTH_RESPONSE_TIMEOUT_MS = 10000" in background
    assert "code: 'connection_timeout'" in background
    assert "code: 'auth_timeout'" in background
    assert "clearConnectionTimer();" in background
    assert "clearAuthTimer();" in background


def test_ext_frame_router_replies_pong_and_routes_driver_frames():
    async def scenario():
        driver = TMWebDriver(clients=[client_row()], external_ws=True)
        ws, adapter = FakeWebSocket(), FakeClient()
        driver.bind_client(adapter, driver.authenticate_client("secret"))
        await _route_ext_frame(json.dumps({"type": "ping"}), ws, adapter, driver)
        assert ws.frames[-1] == {"type": "pong", "protocol": 2}
        await _route_ext_frame(json.dumps({
            "type": "tabs_update",
            "tabs": [{"id": 12, "url": "https://example.test"}],
        }), ws, adapter, driver)
        assert set(driver.get_context("7").sessions) == {"7:12"}
        assert len(ws.frames) == 1

    asyncio.run(scenario())


def test_ext_frame_router_forwards_chat_frames(monkeypatch):
    async def scenario():
        ws, adapter = FakeWebSocket(), FakeClient()
        adapter._user_id, adapter._client_id, adapter._client_alias = 3, "7", "Chrome"
        monkeypatch.setattr(mcp_plugin_store, "list_agents_for_cdp_client", lambda cid: _coro([{"id": 5}]))
        await _route_ext_frame(json.dumps({"type": "chat_list_agents", "reqId": "r1"}), ws, adapter, None)
        assert ws.frames[-1] == {"type": "chat_agents", "reqId": "r1", "agents": [{"id": 5}]}

    asyncio.run(scenario())


def test_extension_detects_half_open_socket_and_refreshes_chat_panel():
    bridge_dir = Path(cdp_server.__file__).with_name("tmwd_cdp_bridge")
    background = bridge_dir.joinpath("background.js").read_text(encoding="utf-8")
    assert "const PONG_TIMEOUT_MS = 60000" in background
    assert "lastFrameAt = Date.now()" in background
    assert "Date.now() - lastFrameAt > PONG_TIMEOUT_MS" in background
    assert "const dead = markDisconnected();" in background
    assert "function markDisconnected" in background
    assert "failPendingChatRequests(" in background
    assert "连接已断开，消息未发出；连上后重试即可" in background

    panel = bridge_dir.joinpath("content.js").read_text(encoding="utf-8")
    assert 'id="refreshChat"' in panel
    assert "onRefreshChat" in panel
    assert "data.type === 'error'" in panel

    gateway_src = Path(sse_gateway.__file__).read_text(encoding="utf-8")
    # 帧类型分发收敛成一次 parse（原先 ping/chat 各解析一次，加 net_event 会变三次）。
    assert "def _frame_type" in gateway_src
    assert 'frame_type == "ping"' in gateway_src
    assert '"pong"' in gateway_src



    adapter = FakeClient()
    adapter._user_id, adapter._client_id, adapter._client_alias = 3, "7", "Work Chrome"
    identity = _chat_identity(adapter, {
        "tabId": 12,
        "token": "attacker-token",
        "user_id": 99,
        "client_id": "evil",
        "client_name": "Evil",
        "session_key": "evil:1",
    })
    assert identity == {
        "user_id": 3,
        "client_id": "7",
        "client_alias": "Work Chrome",
        "session_key": "7:12",
    }


def test_chat_frame_lists_agents_by_connected_client(monkeypatch):
    async def scenario():
        ws, adapter = FakeWebSocket(), FakeClient()
        adapter._user_id, adapter._client_id, adapter._client_alias = 3, "7", "Chrome"
        # 网页对话可选 agent 现按连接的 CDP 客户端 agent_ids 决定，不再按 mcp_user 反推。
        monkeypatch.setattr(mcp_plugin_store, "list_agents_for_cdp_client", lambda cid: _coro([{"id": 5}]))
        await _handle_chat_frame(json.dumps({
            "type": "chat_list_agents", "reqId": "r1", "token": "untrusted",
        }), ws, adapter)
        assert ws.frames[-1] == {"type": "chat_agents", "reqId": "r1", "agents": [{"id": 5}]}

    asyncio.run(scenario())


def test_notify_agents_changed_pushes_frame_only_to_connected_clients():
    """可操作 agent 列表变更后要主动推给已连接的浏览器，否则面板一直显示旧列表。"""
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    connected = FakeClient()
    driver.bind_client(connected, driver.authenticate_client("secret"))

    posted = []
    # post_message 只有 _ExtWsAdapter 有；假客户端补上以记录广播（fire-and-forget 语义）。
    connected.post_message = posted.append

    assert driver.notify_agents_changed() == 1
    assert json.loads(posted[-1]) == {"type": "chat_agents_changed", "protocol": 2}

    # 连接断开（ws_client 释放）后不再推：广播必须有活连接才发。
    driver.unregister_client(connected)
    assert driver.notify_agents_changed() == 0
    assert len(posted) == 1


def test_resync_builtin_service_notifies_cdp_clients(monkeypatch):
    """resync_builtin_service('cdp-bridge') 成功后要通知已连接客户端重拉 agent 列表。"""
    import builtin_tool_store

    async def get_service_by_name(name):
        return {"id": 2, "name": name, "env_template": {}, "enabled": True}
    async def service_env(_sid): return {}
    async def service_auth(_sid): return {"auth_enabled": False, "allowed_tokens": set(), "allowed_user_ids": set()}
    async def configs(_sid, config_type, *, include_secrets=False): return []
    async def by_key(_sid, config_type, key, *, include_secrets=False):
        return {"id": 9, "instance_key": key, "external_ws": True} if config_type == "cdp_driver" else None
    async def cdp_clients(): return []
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_service_by_name", get_service_by_name)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_service_env", service_env)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_service_auth", service_auth)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "list_runtime_configs", configs)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_runtime_config_by_key", by_key)
    monkeypatch.setattr(builtin_tool_store, "cdp_driver_clients", cdp_clients)

    class _FakeRegistry:
        def get(self, name): return object()
        async def update_builtin_config(self, name, **kwargs): return object()

    monkeypatch.setattr("mcp_runtime.registry.registry", _FakeRegistry())

    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    monkeypatch.setattr(cdp_server, "get_driver", lambda: driver)
    calls = []
    monkeypatch.setattr(driver, "notify_agents_changed", lambda: calls.append("cdp-bridge") or 0)

    # cdp-bridge 配置变更 → 调一次 driver.notify_agents_changed
    assert asyncio.run(runtime_config.resync_builtin_service("cdp-bridge")) is True
    assert calls == ["cdp-bridge"]
    # 其它服务（mail）不触发 cdp-bridge 的 agent 变更通知
    assert asyncio.run(runtime_config.resync_builtin_service("mail")) is True
    assert calls == ["cdp-bridge"]


def test_reload_service_core_notifies_cdp_clients(monkeypatch):
    """管理端资源 API（改 cdp_client 的 agent_ids）走 reload_service_core 这条链，
    也必须推 agents 变更通知，否则面板不刷新。"""
    from mcp_runtime import admin_api

    async def get_service(_sid):
        return {"id": 2, "name": "cdp-bridge", "builtin": True, "kind": "builtin", "enabled": True}
    async def service_env(_sid): return {}
    async def service_auth(_sid): return {"auth_enabled": True, "allowed_tokens": set(), "allowed_user_ids": set()}
    async def configs(_sid, config_type, *, include_secrets=False): return []
    async def by_key(_sid, config_type, key, *, include_secrets=False):
        return {"id": 9, "instance_key": key, "external_ws": True} if config_type == "cdp_driver" else None
    async def cdp_clients(): return []
    monkeypatch.setattr(admin_api.mcp_plugin_store, "get_service", get_service)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_service_env", service_env)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_service_auth", service_auth)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "list_runtime_configs", configs)
    monkeypatch.setattr(runtime_config.mcp_plugin_store, "get_runtime_config_by_key", by_key)
    import builtin_tool_store
    monkeypatch.setattr(builtin_tool_store, "cdp_driver_clients", cdp_clients)

    class _Plugin:
        tools = []
    class _FakeRegistry:
        def get(self, name): return _Plugin()
        async def update_builtin_config(self, name, **kwargs): return _Plugin()
    monkeypatch.setattr(admin_api.registry, "get", _FakeRegistry().get)
    monkeypatch.setattr(admin_api.registry, "update_builtin_config", _FakeRegistry().update_builtin_config)
    async def mark_loaded(*_a, **_k): return None
    monkeypatch.setattr(admin_api.mcp_plugin_store, "mark_runtime_status", mark_loaded)

    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    monkeypatch.setattr(cdp_server, "get_driver", lambda: driver)
    calls = []
    monkeypatch.setattr(driver, "notify_agents_changed", lambda: calls.append("reload") or 0)

    result = asyncio.run(admin_api.reload_service_core(2))
    assert result["ok"] is True
    assert calls == ["reload"]


def test_agent_list_refresh_contracts():
    """面板列表更新链路：切 picker 重拉 + 服务端推送触发刷新，两端都要在。"""
    bridge_dir = Path(cdp_server.__file__).with_name("tmwd_cdp_bridge")

    panel = bridge_dir.joinpath("content.js").read_text(encoding="utf-8")
    assert "async function showPicker(refresh)" in panel
    # 切换 Agent 按钮必须带 refresh（否则面板开着时永远显示旧列表）
    assert "showPicker(true)" in panel
    assert "chat_agents_changed" in panel
    assert "async function onAgentsChanged()" in panel
    assert "await refreshAgents();" in panel

    background = bridge_dir.joinpath("background.js").read_text(encoding="utf-8")
    # 推送帧没有 reqId，必须走广播分支而不是 reqId 映射
    assert "chat_agents_changed" in background
    assert "function broadcastChatAgentsChanged" in background
    assert "void broadcastChatAgentsChanged();" in background


def test_owned_chat_page_requires_same_authenticated_client():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    owner = FakeClient()
    driver.bind_client(owner, driver.authenticate_client("secret"))
    driver.ingest_message(json.dumps({"type": "tabs_update", "tabs": [{"id": 12, "url": "https://example.test"}]}), owner)
    assert _owned_chat_page(driver, owner, "7:12") is not None

    impostor = FakeClient()
    impostor._user_id, impostor._client_id = 3, "7"
    assert _owned_chat_page(driver, impostor, "7:12") is None
    assert _owned_chat_page(driver, owner, "7:99") is None


# ── 会话归属：按 CDP 客户端，不按标签页 ────────────────────────────────

def _cdp_conv(*, client_id="7", tab_id="12", base_prompt="你是助手。", **extra_settings):
    """构造一条 CDP 会话行，system_prompt 里已写入建会话时的场景段。"""
    scene = scene_context.normalize(cdp={
        "client_id": client_id, "session_key": f"{client_id}:{tab_id}",
    })
    settings = {**scene_context.persist(scene), **extra_settings}
    return {
        "id": "conv-1",
        "agent_id": 5,
        "system_prompt": scene_context.append_prompt(base_prompt, scene),
        "chat_settings": settings,
    }


def test_conversation_stays_owned_after_its_tab_closes():
    """归属按 client_id：换标签页后仍能读回自己的历史（tabId 已失效）。"""
    conv = _cdp_conv(tab_id="12")
    # 同一浏览器、另一个标签页 —— 旧实现在这里 403。
    assert cdp_chat_service._require_conv_owned_by_client(conv, "7") is conv["chat_settings"]


def test_other_client_still_cannot_read_the_conversation():
    """收窄到 client_id 不能打破客户端之间的隔离。"""
    conv = _cdp_conv(client_id="7")
    with pytest.raises(HTTPException) as excinfo:
        cdp_chat_service._require_conv_owned_by_client(conv, "8")
    assert excinfo.value.status_code == 403


def test_conversation_without_cdp_owner_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        cdp_chat_service._require_conv_owned_by_client({"chat_settings": {}}, "7")
    assert excinfo.value.status_code == 403


def test_continuing_from_another_tab_rebinds_scene_to_the_live_tab():
    """续聊时场景段必须指向本轮所在标签页，否则模型操作一个已关闭的 tab。"""
    conv = _cdp_conv(tab_id="12", reasoning_effort="high")
    update_fields = {}

    ownership = cdp_chat_service._rebind_conv_scene(
        conv, conv["chat_settings"],
        client_id="7", session_key="7:99", client_alias="Chrome",
        update_fields=update_fields,
    )

    # 提示词里的 tab_id 换成新的（复合格式，与 web 工具的 tab 入参同形），
    # 旧的不再残留，基础提示词保留。
    assert "tab_id: 7:99" in update_fields["system_prompt"]
    assert "tab_id: 7:12" not in update_fields["system_prompt"]
    assert update_fields["system_prompt"].startswith("你是助手。")
    # conv 内存态同步更新：本轮 agent 直接读 conv["system_prompt"]。
    assert conv["system_prompt"] == update_fields["system_prompt"]
    # 归属键跟着换，其余 chat_settings 原样保留。
    assert ownership["cdp_session_key"] == "7:99"
    assert ownership["reasoning_effort"] == "high"
    assert update_fields["chat_settings"] is ownership


def test_continuing_in_the_same_tab_writes_nothing():
    """同标签页续聊不该产生一次多余的会话写库。"""
    conv = _cdp_conv(tab_id="12")
    before = dict(conv)
    update_fields = {}

    ownership = cdp_chat_service._rebind_conv_scene(
        conv, conv["chat_settings"],
        client_id="7", session_key="7:12", client_alias="7",
        update_fields=update_fields,
    )

    assert update_fields == {}
    assert ownership is conv["chat_settings"]
    assert conv["system_prompt"] == before["system_prompt"]


def test_rebind_preserves_a_pending_chat_settings_update():
    """场景重绑与本轮思考等级更新共用一次写库，不能互相覆盖。"""
    conv = _cdp_conv(tab_id="12")
    # 调用方已把本轮 reasoning_effort 放进 update_fields（send_message 的既有逻辑）。
    pending = {**conv["chat_settings"], "reasoning_effort": "low"}
    update_fields = {"chat_settings": pending}

    ownership = cdp_chat_service._rebind_conv_scene(
        conv, conv["chat_settings"],
        client_id="7", session_key="7:99", client_alias="7",
        update_fields=update_fields,
    )

    assert ownership["reasoning_effort"] == "low"
    assert ownership["cdp_session_key"] == "7:99"


async def _coro(value):
    return value


def test_chat_stream_posts_path_not_full_url(monkeypatch):
    """回归：_agent_internal_request 接收路径（内部拼 _AGENT_INTERNAL_BASE）。
    若传完整 URL 会拼成 http://127.0.0.1:8001http://127.0.0.1:8001/... →
    InvalidUrlClientError。该异常跑在 create_task 里、done_callback 不取，
    asyncio 静默吞掉 → 面板永远等不到回帧（表现为「点了没反应」）。
    """
    async def scenario():
        ws = FakeWebSocket()
        seen = {}
        async def fake_request(method, path, identity, *, json_body=None):
            seen["method"], seen["path"] = method, path
            return 400, {"detail": "boom"}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_stream(
            {"reqId": "r1", "agentId": 5, "content": "hi", "conversationId": ""},
            {}, {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert seen.get("path", "").startswith("/"), f"path 必须以 / 开头: {seen.get('path')}"
        assert "http" not in str(seen.get("path", ""))
        # 失败分支必须回可读的 chat_error，而不是静默吞掉
        assert ws.frames and ws.frames[-1]["type"] == "chat_error"
        assert "boom" in ws.frames[-1]["message"]
    asyncio.run(scenario())


def test_chat_stream_exception_becomes_visible_error(monkeypatch):
    """_agent_internal_request 抛异常（连接拒绝 / 超时 / 无效 URL 等）时，
    _post_chat_turn 必须回 chat_error——跑在 create_task 里不取异常就静默，
    面板等不到任何帧。"""
    async def scenario():
        import aiohttp

        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            raise aiohttp.ClientConnectionError("connection refused")
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_stream(
            {"reqId": "r2", "agentId": 5, "content": "hi", "conversationId": ""},
            {}, {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert ws.frames and ws.frames[-1]["type"] == "chat_error"
        assert "网络异常" in ws.frames[-1]["message"]
    asyncio.run(scenario())


def test_old_panel_v0_gets_refresh_hint_not_silent_hang(monkeypatch):
    """panel_v=0 的旧 content script 不认 chat_send_ok → 回一条明确的 chat_error
    提示刷新扩展，而不是无声挂住让 sending 永远锁死。"""
    async def scenario():
        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            return 200, {"conversation_id": "c1", "assistant_message_id": 42}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        monkeypatch.setattr(sse_gateway, "_ensure_conversation_pump", lambda *_a, **_kw: None)
        await sse_gateway._post_chat_turn(
            "/agent/client/messages",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            {"agent_id": 5, "content": "hi"},
            "r3", ws, conv_hint="", panel_v=0,
        )
        assert ws.frames[0]["type"] == "chat_error"
        assert "刷新" in ws.frames[0]["message"]
    asyncio.run(scenario())


def test_new_panel_v2_gets_send_ok_ack(monkeypatch):
    """panel_v=2 的新 content script 认 chat_send_ok → 正常回 ack。"""
    async def scenario():
        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            return 200, {"conversation_id": "c1", "assistant_message_id": 42}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        monkeypatch.setattr(sse_gateway, "_ensure_conversation_pump", lambda *_a, **_kw: None)
        await sse_gateway._post_chat_turn(
            "/agent/client/messages",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            {"agent_id": 5, "content": "hi"},
            "r4", ws, conv_hint="", panel_v=2,
        )
        assert ws.frames[0]["type"] == "chat_send_ok"
        assert ws.frames[0]["conversationId"] == "c1"
        assert ws.frames[0]["assistantMessageId"] == 42
    asyncio.run(scenario())


# ── 审批恢复：刷新/换标签页后审批卡必须能重新取回 ────────────────────────

def test_load_conversation_frame_carries_pending_approvals(monkeypatch):
    async def scenario():
        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            assert method == "GET" and path == "/agent/client/conversations/c-1"
            return 200, {
                "conversation": {"id": "c-1"},
                "messages": [{"id": 1, "role": "user", "content": "hi", "status": "done"}],
                "pending_approvals": [{
                    "confirmation_id": "abc", "tool_name": "code_run",
                    "command_hash": "h", "metadata": {"runs": [{"code": "print(1)"}]},
                }],
            }
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_load_conversation(
            {"reqId": "r", "conversationId": "c-1"},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert ws.frames and ws.frames[-1]["type"] == "chat_conversation_loaded"
        loaded = ws.frames[-1]
        assert loaded["messages"]
        assert loaded["pending_approvals"] and loaded["pending_approvals"][0]["confirmation_id"] == "abc"
    asyncio.run(scenario())


def test_load_conversation_frame_empty_pending_when_none(monkeypatch):
    async def scenario():
        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            return 200, {"conversation": {"id": "c-2"}, "messages": []}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_load_conversation(
            {"reqId": "r", "conversationId": "c-2"},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert ws.frames[-1]["pending_approvals"] == []
    asyncio.run(scenario())


def test_cdp_has_get_approvals_endpoint():
    import inspect
    src = inspect.getsource(cdp_chat_service)
    assert '@router.get("/conversations/{conv_id}/approvals")' in src
    assert "client_list_approvals" in src
    assert "approval_registry.list_pending" in src


def test_upload_attachment_frame_proxies_raw_bytes(monkeypatch):
    """chat_upload_attachment：base64 解码 → POST 原始字节 → 回投 chat_attachment_uploaded。"""
    import base64

    async def scenario():
        ws = FakeWebSocket()
        posted = {}

        async def fake_request(method, path, identity, *, json_body=None, raw_body=None, timeout_s=30):
            posted["method"], posted["path"], posted["body"] = method, path, raw_body
            return 200, {"path": ".chat-attachments/c-1/abc-shot.png", "filename": "abc-shot.png", "size": 5}

        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_upload_attachment(
            {"reqId": "r", "conversationId": "c-1", "filename": "shot.png",
             "content": base64.b64encode(b"hello").decode()},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert posted["method"] == "POST"
        assert "/agent/client/conversations/c-1/attachments" in posted["path"]
        assert "filename=shot.png" in posted["path"]
        assert posted["body"] == b"hello"
        assert ws.frames[-1]["type"] == "chat_attachment_uploaded"
        assert ws.frames[-1]["path"] == ".chat-attachments/c-1/abc-shot.png"
        assert ws.frames[-1]["size"] == 5
    asyncio.run(scenario())


def test_upload_attachment_frame_rejects_bad_and_oversize(monkeypatch):
    """非法 base64 与超限帧都要回可读的 chat_error，而不是静默。"""
    async def scenario():
        ws = FakeWebSocket()
        called = []

        async def fake_request(*args, **kwargs):
            called.append(1)
            return 200, {}

        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        # 非法 base64
        await sse_gateway._proxy_chat_upload_attachment(
            {"reqId": "r", "conversationId": "c-1", "filename": "a.png", "content": "!!not-b64!!"},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert ws.frames[-1]["type"] == "chat_error"
        assert "base64" in ws.frames[-1]["message"]
        # 缺字段
        await sse_gateway._proxy_chat_upload_attachment(
            {"reqId": "r2", "conversationId": "c-1", "filename": "", "content": "aGk="},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert ws.frames[-1]["type"] == "chat_error"
        # 全程不该碰主服务
        assert called == []
    asyncio.run(scenario())


def test_cdp_has_upload_attachment_endpoint():
    import inspect
    src = inspect.getsource(cdp_chat_service)
    assert '@router.post("/conversations/{conv_id}/attachments")' in src
    assert "client_upload_conversation_attachment" in src
    # 复用 web 端同一套落盘与防穿越逻辑
    assert "_attachment_target" in src
    assert "_require_conv_owned_by_client" in src


def test_compact_frame_proxies_to_main_service(monkeypatch):
    """chat_compact：POST 转发到主服务，回投 chat_compacted 帧。"""
    async def scenario():
        ws = FakeWebSocket()
        called = {}

        async def fake_request(method, path, identity, *, json_body=None, raw_body=None, timeout_s=30):
            called["method"], called["path"] = method, path
            return 200, {"ok": True, "summarized": True, "summary_chars": 321}

        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_compact(
            {"reqId": "r", "conversationId": "c-9"},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert called["method"] == "POST"
        assert called["path"] == "/agent/client/conversations/c-9/compact"
        assert ws.frames[-1]["type"] == "chat_compacted"
        assert ws.frames[-1]["ok"] is True
        assert ws.frames[-1]["summarized"] is True
        assert ws.frames[-1]["summaryChars"] == 321
    asyncio.run(scenario())


def test_compact_frame_requires_conversation_id(monkeypatch):
    async def scenario():
        ws = FakeWebSocket()
        called = []

        async def fake_request(*a, **k):
            called.append(1)
            return 200, {}

        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._proxy_chat_compact(
            {"reqId": "r", "conversationId": ""},
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert ws.frames[-1]["type"] == "chat_error"
        assert called == []
    asyncio.run(scenario())


def test_cdp_has_compact_endpoint():
    import inspect
    src = inspect.getsource(cdp_chat_service)
    assert '@router.post("/conversations/{conv_id}/compact")' in src
    assert "client_compact_conversation" in src


def test_cdp_get_conversation_payload_carries_model_and_pending():
    import inspect
    src = inspect.getsource(cdp_chat_service)
    assert '"model": m.get("model")' in src
    assert "approval_registry.list_pending" in src


# ── 模式透传：auto 免审批必须能到服务端 ──────────────────────────────

def test_chat_stream_forwards_auto_mode(monkeypatch):
    """面板切「自动」→ 网关必须把 mode=auto 带给服务端。

    auto 在服务端等价于 code_run / file_write 全部自动放行（不弹审批卡）。
    网关白名单曾漏掉 auto，把它静默丢成 interact，用户以为免审却仍被审核。
    """
    async def scenario():
        ws = FakeWebSocket()
        seen = {}
        async def fake_request(method, path, identity, *, json_body=None):
            seen["body"] = json_body or {}
            return 200, {"conversation_id": "c1", "assistant_message_id": 1}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        monkeypatch.setattr(sse_gateway, "_ensure_conversation_pump", lambda *_a, **_kw: None)
        await sse_gateway._proxy_chat_stream(
            {"reqId": "r", "agentId": 5, "content": "hi", "conversationId": "", "mode": "auto"},
            {}, {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert seen["body"].get("mode") == "auto", seen["body"]
    asyncio.run(scenario())


def test_chat_stream_forwards_all_known_modes(monkeypatch):
    """interact/plan/goal/auto 四个都要能透传；未知值才丢。"""
    async def scenario():
        for m in ("interact", "plan", "auto"):
            ws = FakeWebSocket()
            seen = {}
            async def fake_request(method, path, identity, *, json_body=None):
                seen["body"] = json_body or {}
                return 200, {"conversation_id": "c1", "assistant_message_id": 1}
            monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
            monkeypatch.setattr(sse_gateway, "_ensure_conversation_pump", lambda *_a, **_kw: None)
            await sse_gateway._proxy_chat_stream(
                {"reqId": "r", "agentId": 5, "content": "hi", "conversationId": "", "mode": m},
                {}, {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
                ws,
            )
            assert seen["body"].get("mode") == m, (m, seen["body"])
        # 未知 mode 不下发（服务端按默认 interact 走）
        ws = FakeWebSocket()
        seen = {}
        async def fake_request2(method, path, identity, *, json_body=None):
            seen["body"] = json_body or {}
            return 200, {"conversation_id": "c1", "assistant_message_id": 1}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request2)
        monkeypatch.setattr(sse_gateway, "_ensure_conversation_pump", lambda *_a, **_kw: None)
        await sse_gateway._proxy_chat_stream(
            {"reqId": "r", "agentId": 5, "content": "hi", "conversationId": "", "mode": "rm -rf"},
            {}, {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            ws,
        )
        assert "mode" not in seen["body"]
    asyncio.run(scenario())


# ── 重试 = 续跑，不是从头重放 ────────────────────────────────────────────

def test_retry_resumes_instead_of_replaying():
    """重试必须续跑：被重试 assistant 已完成的 tool_calls 原样保留（作模型上下文 +
    收尾种子），而不是清空后从头重放。这锁住「点重试又从头跑 30 个 web 调用」的回归。

    _run_cdp_conversation_turn 的 resume_from 分支：
    - resume_from 传给 ConversationTurnWriter（旧正文/工具卡/结果的种子现在住在
      agent/turn_writer.py，api 与 cdp 共用一份——原先两处各抄一份已漂移）；
    - 历史重建 skip_msg_id=None（不跳过被重试那条，让模型看到自己做到哪了）；
    - initial_messages 末尾显式补一条 user「继续」。
    """
    import inspect

    from agent import turn_writer

    src = inspect.getsource(cdp_chat_service)
    writer_src = inspect.getsource(turn_writer)

    # 续跑种子：调用点把 resume_from 交给 writer，writer 里真正做回填。
    assert "resume_from: dict | None = None" in src
    assert "resume_from=resume_from," in src
    assert 'self.content = str(resume_from.get("content") or "")' in writer_src
    assert "self.tool_calls = [dict(c) for c in old_calls]" in writer_src

    # 历史重建：续跑不跳过被重试的那条。
    assert "skip_msg_id=None," in src

    # 续跑时末尾显式补「继续」指令（agent_loop 给了 initial_messages 就不会自动追加 user_input）。
    assert 'initial_messages.append({"role": "user", "content": user_input})' in src

    # retry 端点：只复位 status=streaming + 清 error，保留正文/工具调用/结果。
    assert 'status="streaming"' in src and "error=None" in src


def test_retry_endpoint_does_not_wipe_content():
    """client_retry_message 的 update_message 只翻状态，不再把 content/tool_calls
    置空——否则续跑语义被清空操作破坏。"""
    import inspect

    src = inspect.getsource(cdp_chat_service.client_retry_message)
    # 旧实现清空 content="" / tool_calls=None；现在必须不出现（只 status + error）。
    assert 'content=""' not in src
    assert "tool_calls=None" not in src
    assert "tool_results=None" not in src


# ── 发送受理链路：进程内派发 + 机器可读错误码（「发送受理超时」根因）──────────

def test_agent_internal_request_prefers_inproc_asgi(monkeypatch):
    """_agent_internal_request 优先走进程内 ASGI 直连，不落回环 socket。

    回归背景：/agent/client/* 与 sse_gateway 同进程，走回环 HTTP 在事件循环繁忙时
    sock_connect 超时（日志里的 ConnectionTimeoutError to host 127.0.0.1:8001），
    把「发送受理」拖过面板看门狗 → 面板误判失败、服务端其实已在跑。
    """
    async def scenario():
        import os
        monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "tok")
        called = {}

        class FakeResponse:
            status_code = 200
            def json(self): return {"ok": True}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def request(self, method, path, headers=None, **kw):
                called["method"], called["path"] = method, path
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(sse_gateway, "_get_asgi_app", lambda: object())

        def _boom(*a, **kw):
            raise AssertionError("不应回退回环 HTTP")
        monkeypatch.setattr(sse_gateway, "_agent_internal_request_http", _boom)

        status, data = await sse_gateway._agent_internal_request(
            "POST", "/agent/client/messages",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            json_body={"agent_id": 5, "content": "hi"},
        )
        assert (status, data) == (200, {"ok": True})
        assert called["path"] == "/agent/client/messages"
    asyncio.run(scenario())


def test_agent_internal_request_falls_back_to_http_when_inproc_unavailable(monkeypatch):
    """拿不到进程内 app（_get_asgi_app 返回 None）→ 回退回环 HTTP。"""
    async def scenario():
        monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "tok")
        monkeypatch.setattr(sse_gateway, "_get_asgi_app", lambda: None)
        seen = {}

        async def fake_http(method, path, headers, timeout_s, json_body, raw_body):
            seen["path"] = path
            return 200, {"ok": True}

        monkeypatch.setattr(sse_gateway, "_agent_internal_request_http", fake_http)
        status, data = await sse_gateway._agent_internal_request(
            "GET", "/agent/client/conversations",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
        )
        assert (status, data) == (200, {"ok": True})
        assert seen["path"] == "/agent/client/conversations"
    asyncio.run(scenario())


def test_agent_internal_request_inproc_failure_falls_back(monkeypatch):
    """进程内派发抛异常 → 回退回环，不让面板吃瘪。"""
    async def scenario():
        monkeypatch.setenv("AGENT_INTERNAL_TOKEN", "tok")
        monkeypatch.setattr(sse_gateway, "_get_asgi_app", lambda: object())

        async def boom_asgi(*a, **kw):
            raise RuntimeError("inproc exploded")
        monkeypatch.setattr(sse_gateway, "_agent_internal_request_asgi", boom_asgi)

        async def fake_http(method, path, headers, timeout_s, json_body, raw_body):
            return 201, {"via": "http"}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request_http", fake_http)

        status, data = await sse_gateway._agent_internal_request(
            "GET", "/agent/client/conversations",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
        )
        assert (status, data) == (201, {"via": "http"})
    asyncio.run(scenario())


def test_post_chat_turn_409_emits_turn_running_code(monkeypatch):
    """409（本轮执行中）→ chat_error 带 code=turn_running。面板据此区分「上一轮
    还在跑」与真失败，改为接管这一轮而不是报错（点重试永远没用的根因之一）。"""
    async def scenario():
        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            return 409, {"detail": "a task is already running for this conversation"}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._post_chat_turn(
            "/agent/client/messages",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            {"agent_id": 5, "content": "hi"},
            "r9", ws, conv_hint="c1", panel_v=2,
        )
        assert ws.frames[0]["type"] == "chat_error"
        assert ws.frames[0]["code"] == "turn_running"
    asyncio.run(scenario())


def test_post_chat_turn_403_emits_forbidden_code(monkeypatch):
    async def scenario():
        ws = FakeWebSocket()
        async def fake_request(method, path, identity, *, json_body=None):
            return 403, {"detail": "agent is not allowed for this client"}
        monkeypatch.setattr(sse_gateway, "_agent_internal_request", fake_request)
        await sse_gateway._post_chat_turn(
            "/agent/client/messages",
            {"client_id": "7", "client_alias": "Chrome", "session_key": "7:12", "user_id": 3},
            {"agent_id": 5, "content": "hi"},
            "r10", ws, conv_hint="c1", panel_v=2,
        )
        assert ws.frames[0]["code"] == "forbidden"
    asyncio.run(scenario())


def test_content_js_forwards_error_code_and_reconciles():
    """content.js 契约：sendChat 透传 chat_error.code；受理超时走对账而非直接判死；
    409 turn_running 触发接管。防回归（这些是「点重试没用」的修复点）。"""
    src = (Path(__file__).resolve().parent.parent
           / "mcp_builtin" / "cdp_bridge" / "tmwd_cdp_bridge" / "content.js").read_text(encoding="utf-8")
    # 错误码透传
    assert "code: frame.code || ''" in src
    # 受理看门狗放宽 + 超时改为对账
    assert "const SEND_ACK_TIMEOUT_MS = 30000;" in src
    assert "reconcileAfterAckTimeout" in src
    assert "async function reconcileAfterAckTimeout(assistant)" in src
    # 409 turn_running 接管（send 与 retry 两条路）
    assert "ack.code === 'turn_running'" in src
    assert "ev.code === 'turn_running'" in src
    # onRetry 回查时识别服务端仍在 streaming → 接管而非「找不到可重试的消息」
    assert "rows[i].status === 'streaming'" in src
