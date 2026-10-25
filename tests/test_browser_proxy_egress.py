"""数据面（Phase 3）：浏览器当代理节点的出口请求契约。

覆盖：
- to_manager_config / _normalize_proxy_item 的 browser 模式；
- resolve_proxy_fields 拒 browser 自用（防自环）；
- BrowserConnectManager.request 的帧下发 + head/chunk/end 回传（模拟扩展）；
- 并发闸、超时、错误路径；
- _route_proxy_binary 的帧头解析。
"""
import asyncio
import hashlib
import json

import pytest

from mcp_builtin.cdp_bridge.TMWebDriver import TMWebDriver
import mcp_builtin.browser_proxy as bp
import mcp_builtin.cdp_bridge.server as cdp_server
import providers.proxy_manager as pm_mod


class FakeProxyWS:
    """模拟代理通道 adapter：记录下发帧；测试再手动喂回帧。"""

    def __init__(self):
        self.sent = []
        self._user_id = self._client_id = self._token = None
        self._last_in = 0.0

    def post_message(self, payload):
        self.sent.append(json.loads(payload))

    def send_message(self, payload):
        self.sent.append(json.loads(payload))

    def close_auth(self, code, reason):
        pass


def client_row(token="secret", *, row_id=7):
    return {"id": row_id, "instance_key": str(row_id), "name": "Chrome", "user_ids": [3],
            "enabled": True, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "token_hint": "sec...ret"}


@pytest.fixture()
def driver_with_proxy():
    driver = TMWebDriver(clients=[client_row()], external_ws=True)
    ws = FakeProxyWS()
    driver.bind_proxy(ws, driver.authenticate_client("secret", "7"))
    orig = cdp_server.driver
    cdp_server.driver = driver
    try:
        yield driver, ws
    finally:
        cdp_server.driver = orig


# ── 配置模型 ────────────────────────────────────────────────────────────────

def test_to_manager_config_browser_mode():
    from proxy_utils import to_manager_config
    cfg = to_manager_config({"id": "p9", "mode": "browser", "client_id": "7", "name": "家里 Chrome"})
    assert cfg == {"id": "p9", "mode": "browser", "client_id": "7"}


def test_normalize_proxy_item_browser_requires_client_id():
    import server.admin as admin
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        admin._normalize_proxy_item({"name": "x", "mode": "browser"})
    out = admin._normalize_proxy_item({"name": "x", "mode": "browser", "client_id": "7"})
    assert out["mode"] == "browser" and out["client_id"] == "7"
    assert out["url"] == "" and out["username"] == ""


def test_proxy_ids_differ_per_browser_client():
    import server.admin as admin
    a = admin._normalize_proxy_item({"name": "same", "mode": "browser", "client_id": "7"})
    b = admin._normalize_proxy_item({"name": "same", "mode": "browser", "client_id": "8"})
    assert a["id"] != b["id"]  # client_id 入 seed，同名不同浏览器不撞 id


def test_resolve_proxy_fields_rejects_browser():
    """节点自身下载不能用 browser 模式（节点没有浏览器通道，同 node 模式自环判定）。"""
    import asyncio as aio
    from node_server.shared_store import ProxyResolveError

    async def fake_read_main_config():
        return {"proxies": [{"id": "p9", "name": "b", "mode": "browser", "client_id": "7"}]}

    import node_server.shared_store as ss
    orig = ss.read_main_config
    ss.read_main_config = fake_read_main_config
    try:
        with pytest.raises(ProxyResolveError):
            aio.run(ss.resolve_proxy_fields("p9"))
    finally:
        ss.read_main_config = orig


# ── 数据面往返（模拟扩展回帧）──────────────────────────────────────────────

def test_browser_egress_roundtrip(driver_with_proxy):
    driver, ws = driver_with_proxy
    mgr = bp.get_browser_manager()

    async def run():
        task = asyncio.create_task(mgr.request(
            "7", method="GET", url="https://api.ipify.org", headers={"X-Test": "1"},
        ))
        # 等 proxy_request 帧发出
        for _ in range(50):
            await asyncio.sleep(0.02)
            if ws.sent:
                break
        req = ws.sent[-1]
        assert req["type"] == "proxy_request"
        proxy_id = req["proxyId"]
        assert req["method"] == "GET" and req["headers"]["X-Test"] == "1"
        # 模拟扩展回帧：head → 2 个 chunk → end
        bp.deliver_proxy_head(proxy_id, 200, [["content-type", "text/plain"]])
        bp.deliver_proxy_chunk(proxy_id, b"hello ")
        bp.deliver_proxy_chunk(proxy_id, b"world")
        bp.deliver_proxy_end(proxy_id)
        resp = await task
        assert resp.status == 200
        assert resp.headers == [["content-type", "text/plain"]]
        chunks = [c async for c in resp.iter_chunks()]
        assert b"".join(chunks) == b"hello world"
        await resp.close()
        # exchange 已从 pending 清掉
        assert proxy_id not in mgr._pending
        assert mgr._inflight.get("7", 0) == 0

    asyncio.run(run())


def test_browser_egress_error_frame(driver_with_proxy):
    driver, ws = driver_with_proxy
    mgr = bp.get_browser_manager()

    async def run():
        task = asyncio.create_task(mgr.request("7", method="GET", url="https://x.test/"))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if ws.sent:
                break
        proxy_id = ws.sent[-1]["proxyId"]
        bp.deliver_proxy_error(proxy_id, "fetch failed: net::ERR_NAME_NOT_RESOLVED")
        with pytest.raises(ConnectionError, match="fetch failed"):
            await task
        assert proxy_id not in mgr._pending

    asyncio.run(run())


def test_browser_egress_offline_client(driver_with_proxy):
    driver, ws = driver_with_proxy
    mgr = bp.get_browser_manager()

    async def run():
        driver.unregister_proxy(ws)
        with pytest.raises(ConnectionError, match="未连接"):
            await mgr.request("7", method="GET", url="https://x.test/")

    asyncio.run(run())


def test_browser_egress_concurrency_gate(driver_with_proxy):
    driver, ws = driver_with_proxy
    mgr = bp.get_browser_manager()

    async def run():
        tasks = []
        for i in range(bp._PER_CLIENT_CONCURRENCY):
            t = asyncio.create_task(mgr.request("7", method="GET", url=f"https://x.test/{i}"))
            tasks.append(t)
        # 等 4 个帧都发出
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(ws.sent) >= bp._PER_CLIENT_CONCURRENCY:
                break
        # 第 5 个被并发闸拒绝
        with pytest.raises(ConnectionError, match="并发已满"):
            await mgr.request("7", method="GET", url="https://x.test/5")
        # 清理：让挂起的请求超时路径不卡测试——直接喂 error 收尾
        for frame in ws.sent:
            bp.deliver_proxy_error(frame["proxyId"], "cancelled by test")
        for t in tasks:
            try:
                await t
            except ConnectionError:
                pass
        assert mgr._inflight.get("7", 0) == 0

    asyncio.run(run())


def test_browser_egress_body_b64(driver_with_proxy):
    driver, ws = driver_with_proxy
    mgr = bp.get_browser_manager()

    async def run():
        task = asyncio.create_task(mgr.request(
            "7", method="POST", url="https://x.test/e", body=b"payload-bytes",
        ))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if ws.sent:
                break
        req = ws.sent[-1]
        import base64 as b64
        assert b64.b64decode(req["bodyB64"]) == b"payload-bytes"
        bp.deliver_proxy_error(req["proxyId"], "test cleanup")
        try:
            await task
        except ConnectionError:
            pass

    asyncio.run(run())


# ── 二进制帧头解析 ──────────────────────────────────────────────────────────

def test_route_proxy_binary_parses_id_and_payload():
    import mcp_runtime.sse_gateway as g

    received = []
    orig = bp.deliver_proxy_chunk
    bp.deliver_proxy_chunk = lambda pid, data: received.append((pid, data))
    try:
        pid = "bp_abc123"
        payload = b"\x00\x01\x02binary"
        idb = pid.encode()
        frame = bytes([len(idb)]) + idb + payload
        adapter = FakeProxyWS()
        g._route_proxy_binary(frame, adapter)
        assert received == [(pid, payload)]
        # 畸形帧不崩
        g._route_proxy_binary(b"", adapter)
        g._route_proxy_binary(b"\x05ab", adapter)  # idLen 超界
    finally:
        bp.deliver_proxy_chunk = orig


# ── ProxyManager browser 分支 ───────────────────────────────────────────────

def test_proxy_manager_routes_browser_mode(monkeypatch):
    from providers.proxy_manager import ProxyManager

    class FakeBrowserMgr:
        def __init__(self):
            self.calls = []

        async def request(self, client_id, *, method, url, headers, body, timeout=None):
            self.calls.append((client_id, method, url))

            class R:
                status = 201
                headers = {"x-a": "b"}

                async def iter_chunks(self):
                    yield b"ok"

                async def close(self):
                    pass
            return R()

    fbm = FakeBrowserMgr()
    pm = ProxyManager()
    from providers.proxy_manager import set_browser_manager
    pm_mod._shared_manager = pm  # 让 set_browser_manager/get_proxy_manager 命中它
    set_browser_manager(fbm)
    try:
        cfg = {"id": "pb1", "mode": "browser", "client_id": "7", "version": 1}
        pm.update_proxy_config(cfg)

        async def run():
            resp = await pm.request(url="https://api.ipify.org", proxy_config_id="pb1", method="GET")
            assert resp.status == 201
            chunks = [c async for c in resp.iter_any()]
            assert b"".join(chunks) == b"ok"
            await resp.__aexit__(None, None, None)
            assert fbm.calls == [("7", "GET", "https://api.ipify.org")]

        asyncio.run(run())
    finally:
        pm_mod._shared_manager = None