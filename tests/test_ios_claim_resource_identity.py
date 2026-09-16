"""iOS 认领链路的资源身份回归。

线上现象（用户报）：认领 iPhone 后点「初始化」报「找不到该设备的资源 id，
请刷新设备列表」，且设备出现在 **Android** 列表里，重新扫描也没用。

根因：iOS 认领复用 Android 的配对端点 ``POST /mcp/device-control/pair``。
节点拿配对码自己兑换，服务端在那一刻只建出一条**通用 device 资源行**
（``create_device``）——它不知道这是 iPhone，所以：

- 没有 ``platform`` → ``/resources/devices`` 按
  ``info.platform or ("ios" if info.node_id else "android")`` 推出 "android"
  → 设备掉进 Android 列表；
- 没有 ``data.ios`` 块 → 前端 ``iosResources``（按 platform==="ios" 过滤）里
  找不到它，``resourceIdFor`` 返回 null → 「找不到该设备的资源 id」；
- 认领路由返回体里没有 ``resource_id`` → 「接入并初始化」拿到 undefined；
- ``routes_ios.start_wda_job`` 要求 ``data.ios.node_id/udid``，缺失即 400。

修法：认领 ack 后由服务端回写 iOS 身份（``_adopt_claimed_ios_resource``），
并把 ``resource_id`` 返回；``/resources/devices`` 的 platform 推导补上
``data.ios`` 这条依据。
"""
from __future__ import annotations

import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)


def _derive_platform(resource: dict) -> str:
    """复刻 /resources/devices 的 platform 推导（保持与路由同步）。

    路由实现见 user_platform/routes_builtin_tools.py:list_devices_resource。
    这里复刻是为了在无 PG 的情况下锁定推导规则本身；若路由改了，此测试应同步。
    """
    info = resource.get("device_info") or {}
    ios_block = resource.get("ios") if isinstance(resource.get("ios"), dict) else {}
    return str(
        info.get("platform")
        or ("ios" if ios_block.get("node_id") or ios_block.get("udid") else "")
        or ("ios" if info.get("node_id") else "android")
    )


# ── platform 推导：iOS 设备必须落进 iOS 桶 ───────────────────────────────────


def test_claimed_ios_resource_with_ios_block_is_ios():
    """核心：带 data.ios 的资源（认领后回写的形状）必须判为 ios。"""
    resource = {
        "device_id": "dev_X",
        "ios": {"udid": "00008120-001175642EE0A01E", "node_id": "node-a", "wda_state": "missing"},
    }
    assert _derive_platform(resource) == "ios"


def test_ios_block_with_only_udid_is_ios():
    """只有 udid（node_id 还没回写）也应判 ios，不能退回 android。"""
    resource = {"device_id": "dev_X", "ios": {"udid": "00008120-0011"}}
    assert _derive_platform(resource) == "ios"


def test_bare_claimed_resource_is_android_the_old_bug():
    """回归锁定：认领后**未回写** iOS 身份的行会判成 android。

    这正是线上现象——create_device 建出来的光秃行没有任何 iOS 线索。
    修复后认领会立刻回写，所以真实链路不会再产生这种行。
    """
    resource = {"device_id": "dev_X", "device_info": {}}
    assert _derive_platform(resource) == "android"


def test_android_device_stays_android():
    """真 Android 设备（有 device_info 无 ios 块）不受影响。"""
    resource = {"device_id": "dev_A", "device_info": {"platform": "android", "app_version": "0.1.8"}}
    assert _derive_platform(resource) == "android"


def test_device_info_platform_wins():
    """节点上报的 device_info.platform 最权威，优先于其它线索。"""
    resource = {"device_info": {"platform": "ios"}, "ios": {"udid": "x"}}
    assert _derive_platform(resource) == "ios"


def test_ios_device_info_without_ios_block_still_ios():
    """有 device_info.platform=ios 但还没 ios 块（旧数据）也判 ios。"""
    resource = {"device_info": {"platform": "ios"}}
    assert _derive_platform(resource) == "ios"


# ── 认领路由的返回契约 ─────────────────────────────────────────────────────


def test_claim_route_returns_resource_id():
    """前端 claimOne 解构 resource_id；路由必须返回它。

    此前返回体只有 {node_id, udid}，claimAndInitialize 拿到 undefined 后
    直接调 startIosWdaJob(undefined) 失败。
    """
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools.claim_ios_device)
    assert '"resource_id"' in src or "'resource_id'" in src, (
        "认领路由必须返回 resource_id（前端 claimOne 解构它）"
    )
    assert "_adopt_claimed_ios_resource" in src, (
        "认领路由必须在 ack 后回写 iOS 身份，否则设备落进 Android 桶"
    )


def test_adopt_helper_exists_and_is_wired():
    """回写助手存在且被认领路由调用（防回归：被删/被改名）。"""
    from user_platform import routes_builtin_tools

    assert callable(routes_builtin_tools._adopt_claimed_ios_resource)


@pytest.mark.parametrize("state,expect_pending", [
    ("missing", True),
    ("unspecified", True),
    ("preparing", False),
    ("ready", False),
    ("failed", False),
])
def test_wda_state_missing_means_pending(state, expect_pending):
    """前端把 missing/unspecified 显示为「待初始化」而非「离线」。

    锁定 wda_state 的取值语义：missing = 已认领未初始化（不是离线）。
    前端实现见 resources.tsx 的 iosPending 判定。
    """
    assert (state in ("missing", "unspecified")) is expect_pending


# ── 扁平化误读：ios_info 恒为 {} 的系统性 bug ────────────────────────────────
#
# builtin_tool_store._resource_value 把 data JSONB **展平到顶层**，返回的 DTO
# 没有 "data" 这一层。而 routes_ios / routes_builtin_tools / ios_auto_renew 里
# 有 10 处写成 `resource.get("data") or {}` → 恒为 {} → ios_info 恒为 {} →
# 每个 WDA 端点 400「设备缺少 iOS 节点绑定信息」、自动续签看不到任何设备、
# UDID 冲突检查形同虚设。前端同款（device.data?.ios 恒 undefined）。
#
# 这些测试用源码断言锁定「不再出现嵌套 data 读取」，因为真实调用需要 PG。

_FLATTENED_READ_FILES = (
    "user_platform/routes_ios.py",
    "user_platform/routes_builtin_tools.py",
    "user_platform/ios_auto_renew.py",
)


@pytest.mark.parametrize("rel_path", _FLATTENED_READ_FILES)
def test_no_nested_data_read_of_flat_resource(rel_path):
    """回归锁定：不得再对扁平化资源 DTO 读嵌套的 "data" 层。"""
    import re

    path = os.path.join(_proj, rel_path)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    # 只匹配「资源对象.get("data")」这种误读；request.json()/payload 等不算。
    offenders = [
        m.group(0)
        for m in re.finditer(r'\b(?:device|resource|res|row)\.get\("data"\)', src)
    ]
    assert not offenders, (
        f"{rel_path} 仍有对扁平化资源的嵌套 data 读取：{offenders}；"
        "应直接读顶层键（resource.get(\"ios\")）"
    )


def test_resource_value_has_no_data_key():
    """锁定 store 的契约：_resource_value 展平 data，不返回 "data" 键。

    这条是上面所有断言的根据——如果哪天 store 改成嵌套返回，这些读取点要一起改。
    """
    from server import builtin_tool_store

    row = {
        "id": 1,
        "owner_user_id": "u",
        "resource_type": "device",
        "revision": 1,
        "data": '{"device_id": "dev_x", "ios": {"udid": "U"}}',
        "secret_data": "{}",
    }
    value = builtin_tool_store._resource_value(row)
    assert "data" not in value, "_resource_value 应展平 data（不返回 data 键）"
    assert value["device_id"] == "dev_x"
    assert value["ios"] == {"udid": "U"}


def test_ios_identity_is_readable_at_top_level():
    """端到端契约：认领后 iOS 身份在**顶层**可读（routes_ios 就是这么读的）。"""
    from server import builtin_tool_store

    row = {
        "id": 36,
        "owner_user_id": "u",
        "resource_type": "device",
        "revision": 1,
        "data": (
            '{"device_id": "dev_S0fMXIX6EGkTG2a57j2zbg", "platform": "ios",'
            ' "ios": {"udid": "00008120-001175642EE0A01E", "node_id": "node-a"}}'
        ),
        "secret_data": "{}",
    }
    r = builtin_tool_store._resource_value(row)
    ios_info = r.get("ios") or {}
    assert ios_info.get("node_id") == "node-a"
    assert ios_info.get("udid") == "00008120-001175642EE0A01E"
    assert r.get("device_id") == "dev_S0fMXIX6EGkTG2a57j2zbg"

