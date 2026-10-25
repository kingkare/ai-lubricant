"""iOS 设备 5 态归一（两轴分离）单测。

覆盖 2026-10 状态机拆分：
- wda_state → init_state 5 态（installed 是「已初始化·待控制」）
- control_ready 只认 READY（装好≠能控）
- INSTALLED 是初始化轴终点：EXPIRED 不再映射成 failed（那会退回初始化异常）
"""
from __future__ import annotations

from user_platform.ios_device_reconcile import (
    apply_inventory_to_ios_block,
    control_ready_from_wda_state,
    init_state_from_wda_state,
)


def test_five_state_mapping():
    """proto 全名与短名都要吃，且映射符合两轴语义。"""
    cases = {
        # 初始化轴：未初始化 / 初始化中 / 已初始化
        "UNSPECIFIED": "uninitialized",
        "IOS_WDA_STATE_MISSING": "uninitialized",
        "IOS_WDA_STATE_PREPARING": "initializing",
        # 控制轴分界：已装（待控制） vs 控制就绪
        "INSTALLED": "installed",
        "IOS_WDA_STATE_INSTALLED": "installed",
        "READY": "control_ready",
        "IOS_WDA_STATE_READY": "control_ready",
        # 到期角标仍是「已初始化」，不是初始化异常
        "RENEWAL_DUE": "installed",
        "IOS_WDA_STATE_EXPIRED": "installed",
        # 只有真正的安装失败才是初始化异常
        "IOS_WDA_STATE_FAILED": "failed",
        "FAILED": "failed",
    }
    for raw, want in cases.items():
        got = init_state_from_wda_state(raw)
        assert got == want, f"{raw}: init_state={got}, want {want}"


def test_control_ready_only_for_ready():
    """control_ready 只认 READY——「已装好但没连上」不算能控。"""
    assert control_ready_from_wda_state("READY") is True
    assert control_ready_from_wda_state("IOS_WDA_STATE_READY") is True
    # INSTALLED / RENEWAL_DUE / EXPIRED 都是「装好了但此刻不能控」
    for state in ("INSTALLED", "RENEWAL_DUE", "EXPIRED", "PREPARING", "FAILED", "MISSING", ""):
        assert control_ready_from_wda_state(state) is False, f"{state} must not be control-ready"


def test_expired_is_not_initialization_failure():
    """EXPIRED 是 runner 起不来的原因，不是初始化失败——不能退回初始化异常。

    这正是用户确认的核心不变式：INSTALLED 是初始化轴终点，探测/到期只动控制轴。
    """
    assert init_state_from_wda_state("EXPIRED") == "installed"
    assert control_ready_from_wda_state("EXPIRED") is False
    # 对照：真的安装失败才是 failed
    assert init_state_from_wda_state("FAILED") == "failed"


def test_apply_inventory_sets_both_axes():
    """apply_inventory 一次算好 init_state + renewal_due + control_ready。"""
    entry = {
        "wda_state": "IOS_WDA_STATE_INSTALLED",
        "present": True,
        "profile_expires_at": "2026-11-01T00:00:00Z",
        "last_error": "runner control check: timeout",
    }
    block = apply_inventory_to_ios_block({}, entry)
    assert block["init_state"] == "installed"
    assert block["control_ready"] is False
    assert block["renewal_due"] is False
    # INSTALLED 保留 last_error（控制未验活的原因），但不代表设备坏
    assert block["last_error"] == "runner control check: timeout"


def test_apply_inventory_ready_sets_control_ready():
    entry = {"wda_state": "READY", "present": True}
    block = apply_inventory_to_ios_block({}, entry)
    assert block["init_state"] == "control_ready"
    assert block["control_ready"] is True
    assert block["renewal_due"] is False


def test_apply_inventory_renewal_due_badge():
    entry = {"wda_state": "RENEWAL_DUE", "present": True}
    block = apply_inventory_to_ios_block({}, entry)
    assert block["init_state"] == "installed"
    assert block["renewal_due"] is True
    assert block["control_ready"] is False
