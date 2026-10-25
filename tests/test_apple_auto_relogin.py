"""自动重登 + 插线续签触发：纯逻辑单测（不碰 Apple、不碰库）。

覆盖：
- password_crypto 加解密闭环 + 跨域 AAD 隔离 + 坏 blob 报错
- renew_on_connect 的门禁矩阵（状态/在线/signing_profile/防风暴闸/派发失败不重投）
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from user_platform.apple_signing import password_crypto as pc
from user_platform import ios_auto_renew as auto_renew


TEST_KEY = "test-apple-signing-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def _apple_key(monkeypatch):
    """保证每个用例都有稳定密钥（同款 pattern 见 test_browser_password_manager）。

    浏览器键也一并设上：跨域隔离用例要两边都能加密，且必须证明**同源的键**
    因 AAD/派生域不同而互不相通。
    """
    monkeypatch.setenv("APPLE_SIGNING_PASSWORD_KEY", TEST_KEY)
    monkeypatch.setenv("BROWSER_PASSWORD_KEY", TEST_KEY)


# ── password_crypto ──────────────────────────────────────────────────────────


def test_password_roundtrip_and_no_plaintext():
    blob = pc.encrypt_apple_password("P@ssw0rd-中文")
    assert blob.startswith("v1:")
    assert "P@ssw0rd" not in blob
    assert pc.decrypt_apple_password(blob) == "P@ssw0rd-中文"


def test_password_domain_isolated_from_browser_crypto():
    """同一把派生键源下，浏览器密码 blob 不能在本模块解开（AAD 域分离）。"""
    from server import browser_password_crypto as bpc

    blob = bpc.encrypt_password("same-secret")
    with pytest.raises(Exception):
        pc.decrypt_apple_password(blob)
    reverse = pc.encrypt_apple_password("same-secret")
    with pytest.raises(Exception):
        bpc.decrypt_password(reverse)


def test_password_bad_blob_shape_rejected():
    for bad in ("", "v1:onlyonefield", "v9:abc:def", "not-a-blob"):
        with pytest.raises(ValueError):
            pc.decrypt_apple_password(bad)


def test_password_key_rotation_detected(monkeypatch):
    """密钥轮换后旧密文解不开（InvalidTag）——不静默，调用方据此判「需重登」。"""
    blob = pc.encrypt_apple_password("rotated-secret")
    monkeypatch.setenv("APPLE_SIGNING_PASSWORD_KEY", "different-key-now-9876543210")
    with pytest.raises(Exception):
        pc.decrypt_apple_password(blob)


# ── renew_on_connect ────────────────────────────────────────────────────────


def _row(ios: dict) -> dict:
    return {"id": 1, "device_id": "dev-1", "ios": ios}


def _ios(**over) -> dict:
    base = {
        "present": True,
        "wda_state": "renewal_due",
        "signing_profile_id": 7,
        "auto_renew": True,
        "last_auto_renew_at": "",
        "profile_expires_at": "2026-10-01T00:00:00Z",
    }
    base.update(over)
    return base


def test_connect_renew_dispatches_when_due(monkeypatch):
    calls: list[tuple] = []

    async def fake_dispatch(resource, ios_info, *, signing_profile_id):
        calls.append((resource["id"], signing_profile_id, ios_info.get("last_auto_renew_at")))
        return "job-1"

    class _Store:
        async def update_resource(self, rid, payload):
            calls.append(("writeback", rid, payload["ios"]["last_auto_renew_at"]))

    import builtin_tool_store

    monkeypatch.setattr(auto_renew, "_dispatch_renew", fake_dispatch)
    monkeypatch.setattr(builtin_tool_store, "update_resource", _Store().update_resource)

    ok = asyncio.run(auto_renew.renew_on_connect(_row(_ios()), _ios()))
    assert ok is True
    # 预占闸先写库，再派发
    assert calls[0][0] == "writeback"
    assert calls[1][0] == 1 and calls[1][1] == 7


@pytest.mark.parametrize(
    "over",
    [
        {"present": False},                    # 设备离线
        {"wda_state": "ready"},                # 未到期
        {"wda_state": "preparing"},            # 初始化中
        {"signing_profile_id": None},           # 没绑签名配置
        {"auto_renew": False},                  # 用户关掉自动续
    ],
)
def test_connect_renew_skips(over, monkeypatch):
    async def boom(*a, **k):  # pragma: no cover — 被调用即说明门禁失效
        raise AssertionError("_dispatch_renew should not be called")

    monkeypatch.setattr(auto_renew, "_dispatch_renew", boom)
    ios = _ios(**over)
    assert asyncio.run(auto_renew.renew_on_connect(_row(ios), ios)) is False


def test_connect_renew_expired_state_also_triggers(monkeypatch):
    """已过期（expired）插线也该自动救回来——这正是用户诉求。"""
    async def fake_dispatch(*a, **k):
        return "job-x"

    class _Store:
        async def update_resource(self, rid, payload):
            pass

    import builtin_tool_store

    monkeypatch.setattr(auto_renew, "_dispatch_renew", fake_dispatch)
    monkeypatch.setattr(builtin_tool_store, "update_resource", _Store().update_resource)
    ios = _ios(wda_state="IOS_WDA_STATE_EXPIRED")  # proto 全名也要认
    assert asyncio.run(auto_renew.renew_on_connect(_row(ios), ios)) is True


def test_connect_renew_pre_claim_blocks_retry_storm(monkeypatch):
    """预占闸：派发失败也把 last_auto_renew_at 写下去，下一轮直接跳过。"""
    import builtin_tool_store
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

    async def failing_dispatch(*a, **k):
        return None

    written: dict = {}

    class _Store:
        async def update_resource(self, rid, payload):
            written.update(payload["ios"])

    monkeypatch.setattr(auto_renew, "_dispatch_renew", failing_dispatch)
    monkeypatch.setattr(builtin_tool_store, "update_resource", _Store().update_resource)
    ios = _ios()
    assert asyncio.run(auto_renew.renew_on_connect(_row(ios), ios, now=now)) is False
    assert written.get("last_auto_renew_at")

    # 同一轮内再调：闸门生效，连派发都不该发生
    assert asyncio.run(auto_renew.renew_on_connect(_row(written), written, now=now)) is False


def test_connect_renew_guard_window_blocks(monkeypatch):
    import builtin_tool_store
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

    async def boom(*a, **k):  # pragma: no cover
        raise AssertionError("should be blocked by the 12h guard")

    monkeypatch.setattr(auto_renew, "_dispatch_renew", boom)
    ios = _ios(last_auto_renew_at=(now - timedelta(hours=1)).isoformat())
    assert asyncio.run(auto_renew.renew_on_connect(_row(ios), ios, now=now)) is False


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
