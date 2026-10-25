"""节点升级「直连」语义回归。

历史缺陷（2026-10-20 实测）：升级弹窗选「直连（不使用代理）」会提交空字符串，
而服务端把空串解释成「没选 → 回落到节点常驻绑定的出口代理」，节点端在帧里
三个 proxy 字段全空时还会再回落到 connect 时推送的代理快照。结果是操作者
明明选了直连，下载实际仍走绑定代理，在只有直连可用的链路上表现为 EOF。

现在客户端提交 ``DIRECT_PROXY_ID`` 哨兵，服务端把它解析成显式
``proxy_mode="direct"``——非空字段让节点端不再回落，同时也能把「直连」
和「没选」区分开。

另外锁定探测并发：两个资产的探测若串行，20s 超时会叠成 40s 的空窗。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import node_release_catalog
import node_upgrade_targets
from node_upgrade_targets import DIRECT_PROXY_ID
from user_platform import nodes_service as nodes_service_module
from user_platform.nodes_service import NodesService

NODE_ID = "n-direct-1"

NODE = {
    "node_id": NODE_ID,
    "role": "execution",
    "proxy_config_id": "px-bound",
    "capabilities": {"os": "windows", "arch": "amd64"},
}

RELEASE = {
    "version": "20261020-2319",
    "assets": [
        {
            "role": "runtime",
            "platform": "any",
            "arch": "any",
            "version": "20261020-2319",
            "download_url": "https://github.com/o/r/releases/download/v/node-runtime.tar.gz",
            "digest": "sha256:aaa",
        },
        {
            "role": "execution",
            "platform": "windows",
            "arch": "amd64",
            "version": "20261020-2319",
            "download_url": "https://github.com/o/r/releases/download/v/node-execution-windows-amd64.exe",
            "digest": "sha256:bbb",
        },
    ],
}

BOUND_FIELDS = {"proxy_mode": "network", "proxy_url": "http://10.0.0.9:7890"}


def _service() -> NodesService:
    service = NodesService()
    service.get_node_if_exists = AsyncMock(return_value=dict(NODE))
    return service


def _client() -> AsyncMock:
    client = AsyncMock()
    client.upgrade_node = AsyncMock(return_value={"accepted": True})
    client.set_node_last_proxy = AsyncMock(return_value={"ok": True})
    return client


@pytest.fixture()
def upgrade_env(monkeypatch: pytest.MonkeyPatch):
    """打桩目录/探测/代理解析/控制面客户端，返回 (service, client, probes)。"""
    monkeypatch.setattr(
        node_release_catalog, "get_latest_release", AsyncMock(return_value=RELEASE)
    )
    probes: list[str] = []

    async def fake_probe(url: str, fields: dict) -> None:
        probes.append(url)
        assert fields  # 每个 target 都必须带明确的 proxy 决策

    service = _service()
    client = _client()
    monkeypatch.setattr(nodes_service_module, "get_local_node_client", lambda: client)

    async def fake_resolve(proxy_id: str) -> dict:
        assert proxy_id == "px-bound" or proxy_id == "px-explicit"
        return dict(BOUND_FIELDS)

    monkeypatch.setattr(node_upgrade_targets, "resolve_proxy", fake_resolve)
    monkeypatch.setattr(node_upgrade_targets, "probe_asset", fake_probe)
    return service, client, probes


@pytest.mark.asyncio
async def test_direct_sentinel_forces_direct_and_ignores_binding(upgrade_env) -> None:
    """选「直连」= 哨兵：不下发绑定代理，帧里显式 direct，记忆落空串。"""
    service, client, probes = upgrade_env

    await service.upgrade_node(NODE_ID, proxy_config_id=DIRECT_PROXY_ID)

    runtime_target = client.upgrade_node.await_args.kwargs["runtime_target"]
    node_target = client.upgrade_node.await_args.kwargs["node_target"]
    for target in (runtime_target, node_target):
        # 关键：不是三字段全空（那会触发节点端回落常驻代理快照）。
        # 控制面在 runtime_upgrade_node 里用 .get(..., "") 归一，缺省即空串。
        assert target["proxy_mode"] == "direct"
        assert target.get("proxy_url", "") == ""
        assert target.get("proxy_url_prefix", "") == ""
    # 探测也确实按直连走（同一份 proxy_fields）。
    assert len(probes) == 2
    # 「上次成功升级」口径：空串=直连，绝不把哨兵写进代理 id 列。
    client.set_node_last_proxy.assert_awaited_once_with(NODE_ID, "")


@pytest.mark.asyncio
async def test_explicit_proxy_id_wins_over_binding(upgrade_env) -> None:
    service, client, _ = upgrade_env

    await service.upgrade_node(NODE_ID, proxy_config_id="px-explicit")

    runtime_target = client.upgrade_node.await_args.kwargs["runtime_target"]
    assert runtime_target["proxy_mode"] == "network"
    assert runtime_target["proxy_url"] == BOUND_FIELDS["proxy_url"]
    client.set_node_last_proxy.assert_awaited_once_with(NODE_ID, "px-explicit")


@pytest.mark.asyncio
async def test_empty_choice_still_falls_back_to_binding(upgrade_env) -> None:
    """向后兼容：旧的空串语义不变——回落到节点常驻绑定。"""
    service, client, _ = upgrade_env

    await service.upgrade_node(NODE_ID, proxy_config_id="")

    runtime_target = client.upgrade_node.await_args.kwargs["runtime_target"]
    assert runtime_target["proxy_mode"] == "network"
    client.set_node_last_proxy.assert_awaited_once_with(NODE_ID, "px-bound")


@pytest.mark.asyncio
async def test_asset_probes_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个资产并发探测：串行会让 20s 超时叠成 40s 的静默窗口。

    门闩只在两个探测都「已开始」时打开；串行实现里第一个探测会等满超时，
    测试因此失败。
    """
    monkeypatch.setattr(
        node_release_catalog, "get_latest_release", AsyncMock(return_value=RELEASE)
    )
    service = _service()
    client = _client()
    monkeypatch.setattr(nodes_service_module, "get_local_node_client", lambda: client)

    gate = asyncio.Event()
    started: list[str] = []

    async def slow_probe(url: str, fields: dict) -> None:
        started.append(url)
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=2)

    async def fake_resolve(proxy_id: str) -> dict:
        return {}

    monkeypatch.setattr(node_upgrade_targets, "resolve_proxy", fake_resolve)
    monkeypatch.setattr(node_upgrade_targets, "probe_asset", slow_probe)

    await service.upgrade_node(NODE_ID, proxy_config_id="")

    assert len(started) == 2


@pytest.mark.asyncio
async def test_resolve_proxy_recognizes_sentinel_without_config_read() -> None:
    """哨兵不是代理池 id：必须在读配置之前就解析成 direct。

    resolve_proxy 读配置会依赖 PG（测试环境未初始化），能返回本身就证明
    它没有走那条分支。
    """
    assert await node_upgrade_targets.resolve_proxy(DIRECT_PROXY_ID) == {
        "proxy_mode": "direct"
    }
    # 空串保持原语义：未指定（由调用方决定是否回落）。
    assert await node_upgrade_targets.resolve_proxy("") == {}
