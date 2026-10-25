"""iOS 状态模型收敛：初始化 5 态（两轴分离）+ 在线轴，以及下发的在线门禁。

用户定的模型是**正交轴**，不是一条 7 态的链（2026-10 状态机拆分定版）：

    初始化状态：待初始化 / 初始化中 / 已初始化·待控制 / 控制就绪 / 初始化异常
    在线状态：  在线 / 离线

proto 的 wda_state 有 8 个值。INSTALLED（已初始化·待控制）= runner 已装、
lockdown 级可装 ipa，但控制链路未连；READY（控制就绪）= 控制链路已连（节点
deviceControlOn）。RENEWAL_DUE/EXPIRED 是「已初始化」上的到期角标（runner 起不
来的原因），**不是初始化异常**——INSTALLED 是初始化轴终点，探测/到期只动控制轴，
绝不退回「未初始化」。收敛在服务端做一次（_init_state_from_wda_state），前后端
都读归一值，避免两边各写一套 switch 各自漂移。

在线轴的定义是「go-ios 扫得到」（proto NodeIosDevice.present），刻意**不是**
device_control_online：守护 WS 要 WDA 装好之后才起来，拿它当门禁会让第一次
初始化永远开始不了。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)


# ── 初始化状态归一化 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "wda_state,expected",
    [
        ("missing", "uninitialized"),
        ("unspecified", "uninitialized"),
        ("MISSING", "uninitialized"),
        # proto 全名（节点经 Connect JSON 回的就是这种）
        ("IOS_WDA_STATE_MISSING", "uninitialized"),
        ("IOS_WDA_STATE_PREPARING", "initializing"),
        ("preparing", "initializing"),
        ("installed", "installed"),
        ("IOS_WDA_STATE_INSTALLED", "installed"),
        ("ready", "control_ready"),
        ("IOS_WDA_STATE_READY", "control_ready"),
        # renewal_due / expired 是「已初始化」上的到期角标（runner 起不来的原因），
        # 不是初始化异常——绝不退回 failed（那会退回初始化轴）。
        ("renewal_due", "installed"),
        ("IOS_WDA_STATE_RENEWAL_DUE", "installed"),
        ("expired", "installed"),
        ("IOS_WDA_STATE_EXPIRED", "installed"),
        ("failed", "failed"),
        # 未知/空 → 待初始化（宁可让用户看到「待初始化」，也不要显示一个假状态）
        ("", "uninitialized"),
        (None, "uninitialized"),
        ("something_new", "uninitialized"),
    ],
)
def test_init_state_normalization(wda_state, expected):
    from user_platform.routes_builtin_tools import _init_state_from_wda_state

    assert _init_state_from_wda_state(wda_state) == expected


def test_init_state_is_exactly_five_values():
    """收敛后只能有 5 个值（installed/control_ready 是 2026-10 拆分新增）。"""
    from user_platform.routes_builtin_tools import _INIT_STATE_BY_WDA_STATE

    assert set(_INIT_STATE_BY_WDA_STATE.values()) == {
        "uninitialized",
        "initializing",
        "installed",
        "control_ready",
        "failed",
    }
    # proto 的 8 个 wda_state 每个都要有映射，漏一个会静默落到默认值。
    assert set(_INIT_STATE_BY_WDA_STATE) == {
        "unspecified",
        "missing",
        "preparing",
        "installed",
        "ready",
        "renewal_due",
        "expired",
        "failed",
    }


# ── present 落到 ios 块 + 归一值一起写 ────────────────────────────────────────


class _FakeClient:
    def __init__(self, inventories: dict[str, dict], fail_nodes: set[str] | None = None):
        self.inventories = inventories
        self.fail_nodes = fail_nodes or set()
        self.calls: list[str] = []

    async def get_ios_devices(self, node_id: str) -> dict:
        self.calls.append(node_id)
        if node_id in self.fail_nodes:
            from user_platform.node_client import NodeServerUnavailable

            raise NodeServerUnavailable(f"node {node_id} unreachable")
        return self.inventories.get(node_id, {"node_id": node_id, "devices": []})


@pytest.fixture
def merge(monkeypatch):
    from user_platform import node_client, routes_builtin_tools
    from user_platform.routes_builtin_tools import _merge_ios_inventory

    def _run(resources, inventories, fail_nodes=None):
        client = _FakeClient(inventories, fail_nodes)
        monkeypatch.setattr(routes_builtin_tools, "get_node_client", lambda: client, raising=False)
        monkeypatch.setattr(node_client, "get_node_client", lambda: client, raising=False)
        asyncio.run(_merge_ios_inventory(resources))
        return client

    return _run


def _ios_resource(resource_id: int, device_id: str, node_id: str, **ios_extra) -> dict:
    ios = {"udid": "UDID-1", "node_id": node_id, "wda_state": "missing"}
    ios.update(ios_extra)
    return {"id": resource_id, "device_id": device_id, "ios": ios, "platform": "ios"}


def test_present_lands_in_ios_block(merge):
    """在线轴：present 必须同步到 ios 块，前端才有判据。

    匹配按 **udid**（稳定身份）：节点未认领时 device_id 会为空/变，按 device_id
    匹配会漏掉「服务端有行、节点丢了认领」的设备。故清单条目带 udid。
    """
    resources = [_ios_resource(36, "dev_X", "node-a")]
    merge(resources, {"node-a": {"devices": [{"udid": "UDID-1", "device_id": "dev_X", "wda_state": "missing", "present": True}]}})

    assert resources[0]["ios"]["present"] is True


def test_present_false_is_a_real_value_not_skipped(merge):
    """present=False 是有效值（手机被拔了），不能被「空值不覆盖」那条规则吃掉。"""
    resources = [_ios_resource(36, "dev_X", "node-a", present=True)]
    merge(resources, {"node-a": {"devices": [{"udid": "UDID-1", "device_id": "dev_X", "present": False}]}})

    assert resources[0]["ios"]["present"] is False, "False 必须落下去，不能被当成空值跳过"


def test_present_none_does_not_overwrite(merge):
    """字段缺失（None）才跳过——那是节点还没填全，不是「离线」。"""
    resources = [_ios_resource(36, "dev_X", "node-a", present=True)]
    merge(resources, {"node-a": {"devices": [{"udid": "UDID-1", "device_id": "dev_X", "present": None}]}})

    assert resources[0]["ios"]["present"] is True


def test_init_state_and_renewal_flag_written(merge):
    """归一值 + 到期角标一次算好，前端直接用。"""
    resources = [_ios_resource(36, "dev_X", "node-a")]
    merge(resources, {"node-a": {"devices": [{"udid": "UDID-1", "device_id": "dev_X", "wda_state": "renewal_due"}]}})

    ios = resources[0]["ios"]
    assert ios["init_state"] == "installed"
    assert ios["renewal_due"] is True


def test_init_state_installed_for_expired(merge):
    """expired 是「已初始化」上的 runner 起不来原因，不是初始化失败。"""
    resources = [_ios_resource(36, "dev_X", "node-a")]
    merge(resources, {"node-a": {"devices": [{"udid": "UDID-1", "device_id": "dev_X", "wda_state": "expired"}]}})

    ios = resources[0]["ios"]
    assert ios["init_state"] == "installed"
    assert ios["control_ready"] is False
    assert ios["renewal_due"] is False


def test_matches_by_udid_even_when_device_id_changed(merge):
    """核心回归：节点侧 device_id 变了（认领丢失后重认领/换凭据），按 udid 仍能匹配。

    旧实现按 device_id 匹配，此时会漏 → 设备恒显示离线（用户报的「插着却离线」）。
    """
    resources = [_ios_resource(36, "dev_OLD", "node-a")]
    merge(resources, {"node-a": {"devices": [{"udid": "UDID-1", "device_id": "dev_NEW", "present": True}]}})

    assert resources[0]["ios"]["present"] is True, "device_id 变了也要按 udid 匹配上"


def test_node_unreachable_keeps_online_axis(merge):
    """节点不可达 ≠ 手机被拔了：不能把在线设备标成离线。"""
    resources = [_ios_resource(36, "dev_X", "node-a", present=True, wda_state="ready")]
    merge(resources, {}, fail_nodes={"node-a"})

    assert resources[0]["ios"]["present"] is True


# ── iOS 宿主选择：按角色，不按自报能力标签 ─────────────────────────────────────
#
# 线上现象（用户报）：插着 iPhone 的 Windows 执行节点，扫描弹框只剩「没有节点上报
# iOS 扫描能力」——因为 _is_ios_host 写死了 ``capabilities.ios_mgmt == "true"``。
# 那个标签是节点**启动时**探测 usbmuxd 得到的一次性结论，Apple 移动设备服务晚一步
# 就绪，标签就整个进程生命周期缺失，一台能扫的机器被一个易失字段判死。
# 新口径：执行节点/ios_host 一律算宿主（节点侧真的没设备栈时会确切回错，由
# per-host 报错呈现）；管理节点与纯展示行排除。


@pytest.mark.parametrize(
    "node,expected",
    [
        # 核心回归：Windows 执行节点没上报 ios_mgmt（标签丢了）也必须是宿主。
        ({"node_id": "n1", "node_role": "execution", "capabilities": {}}, True),
        # 报了标签的照常通过（向后兼容）。
        ({"node_role": "execution", "capabilities": {"ios_mgmt": "true"}}, True),
        ({"node_role": "ios_host", "capabilities": {"ios_mgmt": "true"}}, True),
        # 管理节点（含被动管理）没有设备栈，不该收到 iOS 帧。
        ({"node_role": "management", "capabilities": {"ios_mgmt": "true"}}, False),
        ({"node_role": "passive_management", "capabilities": {}}, False),
        # 纯展示用的管理节点上下文行也不入选。
        ({"node_role": "execution", "display_only": True, "capabilities": {}}, False),
    ],
)
def test_ios_host_selected_by_role_not_capability_label(node, expected):
    from user_platform.routes_builtin_tools import _is_ios_host

    assert _is_ios_host(node) is expected


def test_ios_host_gate_is_source_locked():
    """锁定：_is_ios_host 不得再读 capabilities.ios_mgmt 作为准入条件。"""
    import inspect

    from user_platform.routes_builtin_tools import _is_ios_host

    src = inspect.getsource(_is_ios_host)
    assert 'get("ios_mgmt")' not in src, "宿主选择不得再依赖自报的 ios_mgmt 标签"


# ── 在线门禁 ──────────────────────────────────────────────────────────────────


def _patch_gate(monkeypatch, client):
    from user_platform import node_client, routes_builtin_tools

    monkeypatch.setattr(routes_builtin_tools, "get_node_client", lambda: client, raising=False)
    monkeypatch.setattr(node_client, "get_node_client", lambda: client, raising=False)


def test_gate_blocks_offline_device(monkeypatch):
    """离线设备被 409 拦住，而不是让一个必然失败的 job 跑到流水线深处。"""
    from fastapi import HTTPException

    from user_platform.routes_builtin_tools import _ensure_ios_device_online

    client = _FakeClient({"node-a": {"devices": [{"udid": "UDID-1", "present": False}]}})
    _patch_gate(monkeypatch, client)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_ensure_ios_device_online({"node_id": "node-a", "udid": "UDID-1"}, action="初始化"))

    assert exc.value.status_code == 409
    assert "不在线" in exc.value.detail


def test_gate_passes_online_device(monkeypatch):
    from user_platform.routes_builtin_tools import _ensure_ios_device_online

    client = _FakeClient({"node-a": {"devices": [{"udid": "UDID-1", "present": True}]}})
    _patch_gate(monkeypatch, client)

    # 不抛异常即通过
    asyncio.run(_ensure_ios_device_online({"node_id": "node-a", "udid": "UDID-1"}, action="初始化"))


def test_gate_blocks_when_node_unreachable(monkeypatch):
    """节点联系不上时宁可拦住——不能对着一个不可达的节点派发。"""
    from fastapi import HTTPException

    from user_platform.routes_builtin_tools import _ensure_ios_device_online

    client = _FakeClient({}, fail_nodes={"node-a"})
    _patch_gate(monkeypatch, client)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_ensure_ios_device_online({"node_id": "node-a", "udid": "UDID-1"}, action="初始化"))

    assert exc.value.status_code == 409


def test_gate_requires_binding_info(monkeypatch):
    """没有 node_id/udid 绑定信息是 400，不是 409——那是配置缺失不是设备离线。"""
    from fastapi import HTTPException

    from user_platform.routes_builtin_tools import _ensure_ios_device_online

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_ensure_ios_device_online({"node_id": "", "udid": ""}, action="初始化"))

    assert exc.value.status_code == 400
