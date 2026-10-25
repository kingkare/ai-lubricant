"""浏览器代理通道双槽契约（docs/browser-proxy-design.md §5.2）。

验证设计的关键断言：
- bind_proxy 不碰 ws_client：同一 client_id 允许「控制通道 + 代理通道」两条连接并存；
- apply_clients 吊销/轮换时双槽都断（否则代理出口还能用旧 token 出网）；
- unregister_proxy 只清代理槽，不影响控制通道。
"""
import hashlib
import json
import time

from mcp_builtin.cdp_bridge.TMWebDriver import TMWebDriver


class FakeClient:
    def __init__(self):
        self._user_id = self._client_id = self._token = None
        self.closed = []

    def close_auth(self, code, reason):
        self.closed.append((code, reason))


def client_row(token="secret", *, row_id=7, user_id=3, enabled=True):
    return {"id": row_id, "instance_key": str(row_id), "name": "Chrome", "user_ids": [user_id],
            "enabled": enabled, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "token_hint": "sec...ret"}


def make_driver(clients):
    return TMWebDriver(clients=clients, external_ws=True)


def test_control_and_proxy_slots_coexist():
    driver = make_driver([client_row()])
    ctrl = FakeClient()
    proxy = FakeClient()
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_client(ctrl, cfg)
    driver.bind_proxy(proxy, cfg)

    client = driver.get_context("7").clients["7"]
    assert client.ws_client is ctrl
    assert client.proxy_ws_client is proxy
    # 控制通道与代理通道各自绑定，互不覆盖。
    assert client.ws_client is not client.proxy_ws_client


def test_bind_proxy_does_not_preempt_control_slot():
    """代理通道重连不该把控制通道的连接顶掉。"""
    driver = make_driver([client_row()])
    ctrl = FakeClient()
    proxy = FakeClient()
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_client(ctrl, cfg)
    driver.bind_proxy(proxy, cfg)

    # 控制通道原连接还在。
    assert driver.get_context("7").clients["7"].ws_client is ctrl


def test_proxy_slot_single_connection():
    """同一 client 的代理通道仍单点：第二条代理连接会撞 already_connected。"""
    driver = make_driver([client_row()])
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_proxy(FakeClient(), cfg)
    try:
        driver.bind_proxy(FakeClient(), cfg)
        assert False, "第二条代理连接应被拒绝"
    except ValueError:
        pass


def test_proxy_slot_stale_preemption():
    """半开代理连接超时后可被抢占（与控制通道同语义）。"""
    driver = make_driver([client_row()])
    cfg = driver.authenticate_client("secret", "7")
    stale = FakeClient()
    driver.bind_proxy(stale, cfg)
    # 置为半开死连接。
    stale._last_in = time.time() - 60
    new = FakeClient()
    driver.bind_proxy(new, cfg)
    assert driver.get_context("7").clients["7"].proxy_ws_client is new


def test_apply_clients_revocation_disconnects_both_slots():
    """token 轮换/吊销：控制槽与代理槽必须都断。"""
    driver = make_driver([client_row()])
    ctrl = FakeClient()
    proxy = FakeClient()
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_client(ctrl, cfg)
    driver.bind_proxy(proxy, cfg)

    driver.apply_clients([client_row("new-secret", row_id=7)])
    # 吊销后整个 client 从 context 移除（既有行为）；关键断点是两槽的连接都被断开。
    assert "7" not in driver.get_context("7").clients
    # close code：代理通道用 4404 区分。
    assert (4403, "authorization revoked") in ctrl.closed or any(c == 4403 for c, _ in ctrl.closed)
    assert (4404, "authorization revoked") in proxy.closed or any(c == 4404 for c, _ in proxy.closed)


def test_unregister_proxy_keeps_control_slot():
    """代理通道断开不影响控制通道。"""
    driver = make_driver([client_row()])
    ctrl = FakeClient()
    proxy = FakeClient()
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_client(ctrl, cfg)
    driver.bind_proxy(proxy, cfg)
    driver.unregister_proxy(proxy)
    client = driver.get_context("7").clients["7"]
    assert client.ws_client is ctrl
    assert client.proxy_ws_client is None


def test_get_proxy_client_and_list():
    driver = make_driver([client_row(), client_row("other", row_id=8)])
    proxy = FakeClient()
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_proxy(proxy, cfg)
    assert driver.get_proxy_client("7") is proxy
    assert driver.get_proxy_client("8") is None
    assert driver.list_proxy_connected() == ["7"]


def test_snapshot_shows_proxy_connected():
    driver = make_driver([client_row()])
    cfg = driver.authenticate_client("secret", "7")
    driver.bind_proxy(FakeClient(), cfg)
    snap = driver.snapshot_contexts()
    client = snap[0]["clients"][0]
    assert client["connected"] is False  # 控制通道未连
    assert client["proxy_connected"] is True  # 代理通道已连