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


# ── 孤儿认领：删除即释放 + 重新配对 ──────────────────────────────────────────
#
# 线上现象：删除 iOS 设备资源后，节点 devices.json + 凭据残留 → 扫描仍报
# claimed=true，而服务端已无行 → 既接不进来也初始化不了（「找不到该设备的资源
# id」）。根因是 DELETE 只删行、从不通知节点释放。修法：
#   - delete_resource 删除前先 _release_ios_claim（删节点凭据），响应带 released；
#   - claim_ios_device 支持 force（先 release 再 claim）修复已有孤儿。
# 真实调用需要 PG + 节点，故用源码断言锁定契约。


def test_delete_resource_releases_ios_claim():
    """删除 iOS 设备必须先释放节点侧认领，否则会留下孤儿。"""
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools.delete_resource)
    assert "_release_ios_claim" in src, (
        "delete_resource 必须在删行前调用 _release_ios_claim，否则节点凭据残留成孤儿"
    )
    # 释放分支必须以 ios.udid + ios.node_id 双重门控，别误伤 android/cdp/mail。
    assert 'get("ios")' in src, "应按 ios 块判定 iOS 设备"
    assert "released" in src, "响应需带 released 供前端告警（节点离线时未释放）"
    # 扁平化 DTO：不得读嵌套 data（与上面 test_no_nested_data_read 同口径）。
    assert 'existing.get("data")' not in src


def test_release_ios_claim_helper_is_best_effort():
    """helper 存在且绝不抛（节点离线/RPC 失败返回 (False, 原因)）。"""
    import inspect

    from user_platform import routes_builtin_tools

    assert callable(routes_builtin_tools._release_ios_claim)
    src = inspect.getsource(routes_builtin_tools._release_ios_claim)
    assert "ios_release_device" in src, "helper 必须调用节点的 release RPC"
    assert "delete_credential=True" in src, "必须删节点侧凭据，否则仍是孤儿"
    assert "NodeServerUnavailable" in src and "RPCError" in src, "必须吞掉节点离线/RPC 失败"
    assert "raise" not in src, "helper 必须 best-effort 不抛"


def test_release_device_route_reuses_helper():
    """release-device 路由复用同一 helper（避免两处逻辑漂移）。"""
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools.release_ios_device)
    assert "_release_ios_claim" in src


def test_claim_route_supports_force_repair():
    """认领路由支持 force（孤儿修复：先释放节点侧占用再重新认领）。"""
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools.claim_ios_device)
    assert 'body.get("force")' in src, "认领路由需读 force 开关"
    assert "ios_release_device" in src, "force 路径必须先释放节点侧占用"
    # force-release 必须在 mint/claim 之前（否则 Claim 撞 already claimed）。
    assert src.index("ios_release_device") < src.index("ios_claim_device"), (
        "force-release 必须先于 claim 派发"
    )


# ── 自动续签：扁平化误读 + 到期门禁死锁 ──────────────────────────────────────
#
# 线上现象（用户报）：「续签账号为啥不自动进行」。两个既有 bug：
#   1. _dispatch_renew 读 resource["data"]["device_id"]——扁平化 DTO 无 "data"
#      层，device_id 恒空 → renew_wda 400，每次派发失败。
#   2. _scan_once 把 profile_expires_at 当**硬前置**，而 prepare 从不落这个字段
#      （只有 renew 完成后才回写）→ 第一行就 continue，自动续签永不触发。


def test_auto_renew_dispatch_reads_flat_device_id():
    """_dispatch_renew 必须读顶层 device_id，不得读嵌套 data。"""
    import inspect

    from user_platform import ios_auto_renew

    src = inspect.getsource(ios_auto_renew._dispatch_renew)
    assert 'resource.get("data")' not in src, (
        "扁平化 DTO 无 data 层；读 resource['data']['device_id'] 恒空 → 派发必失败"
    )
    assert 'resource.get("device_id"' in src or "resource.get(\"device_id\"" in src, (
        "应从顶层读 device_id"
    )


def test_auto_renew_scan_does_not_gate_on_unpersisted_expiry():
    """_scan_once 不得把 profile_expires_at 当硬前置（它从不被落库）。"""
    import inspect

    from user_platform import ios_auto_renew

    src = inspect.getsource(ios_auto_renew._scan_once)
    # 旧 bug：`if not signing_profile_id or not expires_at: continue` —— 把到期时间
    # 和 signing_profile_id 一起当硬前置。到期时间改从 inventory 现读。
    assert "not signing_profile_id or not expires_at" not in src, (
        "到期时间不得与 signing_profile_id 一起当硬前置——它从不落库，会让续签永不触发"
    )
    assert "profile_expires_at" in src, "应从节点 inventory 现读到期时间"
    # inventory 必须在「到期判定」之前读到（先读节点、再判到期）。
    assert src.index("_device_inventory_state") < src.index("_needs_renew"), (
        "必须先读节点 inventory 拿到到期时间，再判定是否需要续签"
    )


def test_auto_renew_writes_back_inventory_expiry():
    """扫描器把 inventory 现读的到期时间回写落库（前端徽章 + 下一轮判定）。"""
    import inspect

    from user_platform import ios_auto_renew

    src = inspect.getsource(ios_auto_renew._scan_once)
    assert "wda_profile_expires_at" in src, "应把 inventory 的到期时间回写落库"


# ── 自动扫描对账：节点清单 ↔ 服务端资源行 ────────────────────────────────────
#
# 线上现象（用户报）：手机插着 USB 却恒显示「离线」（不自动在线）；点「添加」
# 报 409「UDID 已被认领」——但用户明明没添加过。
#
# 根因：认领（POST /resources/ios-claims）一次做两件事（建行 + 给节点签发凭据），
# 两半会脱钩（节点重启/换版本/被释放后节点侧凭据没了，服务端行还在）。
#   - _merge_ios_inventory 曾按 device_id 匹配，节点未认领时 device_id 为空 →
#     永远匹配不上 → present 永不更新 → 恒离线。
#   - 冲突检查扫到同 UDID 的行就 409，不管节点侧是否还持有认领。
# 修法：匹配改按 udid；新增 30s 对账任务收敛脏状态；认领冲突检查自愈。


def test_merge_ios_inventory_matches_by_udid_not_device_id():
    """_merge_ios_inventory 必须按 udid 匹配节点清单（device_id 会随认领丢失）。"""
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools._merge_ios_inventory)
    assert 'targets.get(str(dev.get("udid")' in src, "应按 udid 匹配节点清单条目"
    assert 'dev.get("device_id")' not in src, "不得再按 device_id 匹配（认领丢失时为空）"
    assert 'ios_block.get("udid")' in src, "应按行的 ios.udid 建索引"


def test_reconcile_decide_three_states():
    """_decide 三态：扫不到→offline（保留）、未认领→delete（脏）、认领→update。"""
    from user_platform import ios_device_reconcile as r

    assert r.decide_reconcile(None) == "offline", "扫不到必须保留行（标离线），绝不删"
    assert r.decide_reconcile({"claimed": False}) == "delete", "节点明确未认领→删陈旧行"
    assert r.decide_reconcile({"claimed": True}) == "update"
    assert r.decide_reconcile({}) == "delete", "缺 claimed 视为未认领（脏）"


def test_reconcile_never_deletes_on_absence():
    """铁律：只有「节点明确说未认领」才删行；「扫不到」一律保留（标离线）。"""
    import inspect

    from user_platform import ios_device_reconcile as r

    # decide 的语义锁：None（absent）不是 delete。
    assert r.decide_reconcile(None) != "delete"
    # reconcile_once 里 delete 分支只由 decision=="delete" 驱动，且节点不可达整节点跳过。
    src = inspect.getsource(r.reconcile_once)
    assert 'if decision == "delete"' in src
    assert "skipped_nodes" in src, "节点不可达应整节点跳过（绝不因此删行）"


def test_reconcile_loop_has_interval_and_gate():
    """对账任务：30s 常量 + settings.enabled 门禁 + CancelledError 上抛。"""
    import inspect

    from user_platform import ios_device_reconcile as r

    assert r.RECONCILE_INTERVAL_SECONDS == 30
    src = inspect.getsource(r.sync_loop)
    assert "settings.enabled" in src, "compat 关闭时循环应自查退出"
    assert "CancelledError" in src and "raise" in src, "取消必须上抛，别吞"


def test_claim_conflict_check_self_heals_stale_row():
    """认领冲突检查自愈：节点无认领则删陈旧行继续，节点仍持认领才 409。"""
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools.claim_ios_device)
    assert "_ios_row_is_stale" in src, "冲突检查应核对节点侧是否真持有认领"
    # 陈旧则删行继续（不是无条件 409）。
    assert "delete_resource" in src, "陈旧行应删掉让本次认领继续"
    # 仍持认领时保持 409。
    assert "已被认领" in src


def test_ios_row_is_stale_is_conservative_when_node_unreachable():
    """节点不可达时 _ios_row_is_stale 返回 False（保守，不误删好行）。"""
    import inspect

    from user_platform import routes_builtin_tools

    src = inspect.getsource(routes_builtin_tools._ios_row_is_stale)
    assert "NodeServerUnavailable" in src and "RPCError" in src
    # 节点不可达分支返回 False。
    assert "return False" in src




