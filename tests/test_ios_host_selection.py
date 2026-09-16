"""iOS 宿主筛选：按**能力**（ios_mgmt）而非角色（ios_host）。

背景：此前 /resources/ios-hosts 三处过滤写死 `n.get("role") == "ios_host"`，
导致插着 iPhone 的普通执行节点「可见但不可驱动」——用户点「扫描设备」永远
拿到空列表，还收到一句提到 role=ios_host 的含糊提示。iOS 设备管理本质是
宿主能力（go-ios + usbmuxd 可达），任何上报 ios_mgmt=true 的节点都该入选。

这三处过滤此前零测试覆盖，是改动最容易漏掉的地方。
"""
import os
import sys

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from user_platform.routes_builtin_tools import _is_ios_host  # noqa: E402


def test_execution_node_with_capability_is_a_host():
    """核心断言：执行节点只要报了 ios_mgmt，就是合法宿主。"""
    node = {
        "node_id": "node-exec-1",
        "role": "execution",
        "capabilities": {"ios_mgmt": "true", "os": "windows"},
    }
    assert _is_ios_host(node) is True


def test_ios_host_role_with_capability_is_a_host():
    """独立的 node-ios 二进制（role=ios_host）同样入选，行为不变。"""
    node = {
        "node_id": "node-ios-1",
        "role": "ios_host",
        "capabilities": {"ios_mgmt": "true", "os": "darwin"},
    }
    assert _is_ios_host(node) is True


def test_node_without_capability_is_not_a_host():
    """缺能力标签的节点被排除——拦截来自能力，与角色无关。"""
    for role in ("execution", "management", "passive_management", "ios_host"):
        node = {"node_id": f"node-{role}", "role": role, "capabilities": {"os": "linux"}}
        assert _is_ios_host(node) is False, f"{role} 无 ios_mgmt 不应入选"


def test_role_alone_is_not_enough():
    """回归锁定：role=ios_host 但没报能力（旧节点/未升级）不入选。

    这条正是旧实现的判据，现在必须由能力决定。
    """
    node = {"node_id": "node-old", "role": "ios_host", "capabilities": {}}
    assert _is_ios_host(node) is False


def test_missing_capabilities_key_does_not_raise():
    """capabilities 缺失/为 None 时必须安全返回 False，不能让列表接口 500。"""
    assert _is_ios_host({"node_id": "n", "role": "execution"}) is False
    assert _is_ios_host({"node_id": "n", "role": "execution", "capabilities": None}) is False


def test_capability_value_must_be_exactly_true():
    """只有字符串 "true" 算数（与 _require_online_ios_host 的服务端口径一致）。"""
    for value in ("True", "1", "yes", "", "false"):
        node = {"node_id": "n", "role": "execution", "capabilities": {"ios_mgmt": value}}
        assert _is_ios_host(node) is False, f"{value!r} 不应被当作已启用"
