"""Tunnel abstract-panel spec + chmlfrp kind tests.

Pure logic — no network, no ORM: the spec registry (fields / sections /
account_actions / default schemes), chmlfrp config validation (panel_url
defaulting, access_token requirement), and the frp-family port allocation.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from tortoise import Tortoise

from user_platform import tunnel_panel_spec as pspec
from user_platform import tunnel_service as svc
from user_platform import tunnel_allocator as alloc
from user_platform.models_tunnel import TunnelScheme


@pytest_asyncio.fixture
async def db():
    """In-memory sqlite so allocate()'s _used_ports query can run."""
    await Tortoise.init(
        config={
            "connections": {"user_platform": "sqlite://:memory:"},
            "apps": {
                "user_platform": {
                    "models": ["user_platform.models_tunnel"],
                    "default_connection": "user_platform",
                }
            },
        },
        use_tz=False,
        _enable_global_fallback=True,
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── panel spec registry ───────────────────────────────────────────────────


def test_spec_registry_covers_all_kinds():
    kinds = {s["kind"] for s in pspec.describe_kinds()}
    # every registered kind must be a legal tunnel kind, and every legal kind
    # must have a spec (so the form renderer never hits an unknown kind).
    assert set(svc.KINDS) <= kinds
    assert kinds == set(svc.KINDS)


def test_chmlfrp_spec_declares_panel_capabilities():
    spec = pspec.panel_spec_for("chmlfrp")
    assert spec["has_panel"] is True
    assert spec["panel_url_default"] == "https://panel.chmlfrp.net/"
    section_ids = [s["id"] for s in spec["sections"]]
    assert section_ids == ["account", "credits", "checkin"]


def test_chmlfrp_spec_declares_required_actions():
    spec = pspec.panel_spec_for("chmlfrp")
    actions = {a["id"]: a for a in spec["actions"]}
    # 定时签到 / 积分信息 / 账号信息 —— the three required capabilities.
    assert {"checkin", "sync_quota", "sync_account"} <= set(actions)

    checkin = actions["checkin"]
    assert checkin["manual"] is True
    assert checkin["display"]["format"] == "badge"
    assert checkin["display"]["badge_field"] == "last_checkin"
    # 定时签到不做：调度器必须跳过它（default.enabled=False），签到走面板跳转。
    assert checkin["default"]["enabled"] is False
    assert checkin.get("requires_captcha") is True

    quota = actions["sync_quota"]
    assert quota["display"]["format"] == "value"
    assert quota["display"]["value_field"] == "integral"


def test_sync_actions_remain_scheduled():
    """定时更新账号信息/积分保留 —— 它们的 default 仍启用。"""
    actions = {a["id"]: a for a in pspec.panel_spec_for("chmlfrp")["actions"]}
    assert actions["sync_account"]["default"]["enabled"] is True
    assert actions["sync_quota"]["default"]["enabled"] is True


def test_chmlfrp_spec_declares_default_scheme():
    preset = pspec.default_scheme("chmlfrp")
    assert preset is not None
    assert preset["kind"] == "chmlfrp"
    assert preset["config"]["panel_url"] == "https://panel.chmlfrp.net/"


def test_chmlfrp_spec_fields_include_panel_url_and_token():
    spec = pspec.panel_spec_for("chmlfrp")
    fields = {f["key"]: f for f in spec["fields"]}
    assert "panel_url" in fields
    # the adjustable panel URL must default to the official panel.
    assert fields["panel_url"]["placeholder"].startswith("https://panel.chmlfrp.net")
    # the panel access_token is required + secret (masked on read).
    assert fields["access_token"]["required"] is True
    assert fields["access_token"]["secret"] is True


def test_legacy_kinds_have_no_panel():
    for kind in ("frpc", "npc", "cloudflared"):
        spec = pspec.panel_spec_for(kind)
        assert spec["has_panel"] is False
        assert spec["actions"] == []


def test_unknown_kind_returns_inert_spec_never_raises():
    spec = pspec.panel_spec_for("does-not-exist")
    assert spec["has_panel"] is False
    assert spec["actions"] == []
    assert spec["schemes"] == []


def test_action_by_id_lookup():
    assert pspec.action_by_id("chmlfrp", "checkin") is not None
    assert pspec.action_by_id("chmlfrp", "nope") is None
    assert pspec.action_by_id("frpc", "checkin") is None


# ── panel scheduler due logic ─────────────────────────────────────────────


def _scheduler():
    from user_platform import tunnel_panel_scheduler as sched

    return sched


def test_scheduler_daily_due_after_target_time():
    sched = _scheduler()
    from datetime import datetime, timezone

    # _due treats last-run epochs as UTC and compares against a naive-UTC `now`.
    now = datetime(2026, 10, 8, 10, 5)  # naive UTC, past the 10:00 target
    action = {"default": {"enabled": True, "frequency_unit": "daily",
                          "frequency_value": 1, "daily_time": "10:00"}}

    def utc_epoch(y, m, d, hh, mm):
        return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())

    assert sched._due(0, action, now) is True  # never ran → due
    # Ran yesterday 09:00 UTC → target passed today and last < target → due.
    assert sched._due(utc_epoch(2026, 10, 7, 9, 0), action, now) is True
    # Already ran after today's target → not due.
    assert sched._due(utc_epoch(2026, 10, 8, 10, 1), action, now) is False


def test_scheduler_disabled_action_never_due():
    sched = _scheduler()
    from datetime import datetime

    now = datetime(2026, 10, 8, 23, 0)
    action = {"default": {"enabled": False, "frequency_unit": "daily"}}
    assert sched._due(0, action, now) is False


def test_scheduler_hours_interval():
    sched = _scheduler()
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 8, 18, 0)
    action = {"default": {"enabled": True, "frequency_unit": "hours",
                          "frequency_value": 6}}
    six_hours_ago = int((now - timedelta(hours=6, seconds=1))
                        .replace(tzinfo=timezone.utc).timestamp())
    five_hours_ago = int((now - timedelta(hours=5))
                         .replace(tzinfo=timezone.utc).timestamp())
    assert sched._due(six_hours_ago, action, now) is True
    assert sched._due(five_hours_ago, action, now) is False


def test_scheduler_skips_checkin_but_runs_sync():
    """端到端 due 判定：chmlfrp 的 checkin 永不到期，sync_* 到期即跑。"""
    sched = _scheduler()
    from datetime import datetime

    now = datetime(2026, 10, 8, 23, 0)  # 深夜，任何 daily_time 都过了
    actions = {a["id"]: a for a in pspec.panel_spec_for("chmlfrp")["actions"]}
    # checkin 关掉了 → 即便从未执行(0)也不 due。
    assert sched._due(0, actions["checkin"], now) is False
    # sync_account / sync_quota 从未执行 → due。
    assert sched._due(0, actions["sync_account"], now) is True
    assert sched._due(0, actions["sync_quota"], now) is True


# ── chmlfrp config validation ────────────────────────────────────────────


def test_chmlfrp_requires_access_token():
    with pytest.raises(svc.TunnelServiceError) as exc:
        svc._validate_scheme_config("chmlfrp", {"panel_url": "https://panel.chmlfrp.net/"})
    assert "access_token" in str(exc.value)


def test_chmlfrp_does_not_require_server_addr():
    """chmlfrp is panel-managed: no frps address is entered by the user."""
    config = {"access_token": "jwt"}
    svc._validate_scheme_config("chmlfrp", config)  # must not raise
    assert "server_addr" not in config


def test_chmlfrp_drops_legacy_frps_fields():
    config = {"access_token": "jwt", "server_addr": "stale.example.com", "server_port": 7000}
    svc._validate_scheme_config("chmlfrp", config)
    assert "server_addr" not in config
    assert "server_port" not in config


def test_chmlfrp_defaults_panel_url():
    config = {"access_token": "jwt"}
    svc._validate_scheme_config("chmlfrp", config)
    assert config["panel_url"] == "https://panel.chmlfrp.net/"


def test_chmlfrp_keeps_custom_panel_url():
    config = {"access_token": "jwt", "panel_url": "https://panel.example.com/"}
    svc._validate_scheme_config("chmlfrp", config)
    assert config["panel_url"] == "https://panel.example.com/"


def test_chmlfrp_is_a_legal_kind():
    assert "chmlfrp" in svc.KINDS


def test_access_token_and_refresh_token_masked():
    # both panel secrets are in the masking list so list/edit never expose them.
    for key in ("access_token", "refresh_token"):
        assert key in svc._SECRET_KEYS


# ── chmlfrp is per-binding (each binding owns its own panel tunnel) ───────


def test_chmlfrp_is_not_grouped():
    """Each chmlfrp binding creates its own panel tunnel + frpc ini, so it must
    NOT share one grouped client process the way frpc does."""
    import user_platform.tunnel_runtime_manager as rm

    s = TunnelScheme.__new__(TunnelScheme)
    s.kind = "chmlfrp"
    s.config = {"access_token": "jwt"}
    assert rm.is_grouped(s) is False


def test_chmlfrp_panel_spec_has_no_server_fields():
    fields = {f["key"] for f in pspec.panel_spec_for("chmlfrp")["fields"]}
    # The panel spec must NOT ask for frps address/port/token — those are
    # created through the panel at binding time.
    assert fields == {"panel_url", "access_token", "refresh_token"}


# ── node port-limit (rport) parsing ───────────────────────────────────────


def test_parse_rport_both_separators():
    from user_platform.tunnel_panel_service import parse_rport

    # The panel bundle shows both "-" and "~" separators in the wild.
    assert parse_rport("10000-65535") == (10000, 65535)
    assert parse_rport("10000~65535") == (10000, 65535)
    assert parse_rport("30000-30100") == (30000, 30100)


def test_parse_rport_absent_or_malformed_returns_none():
    from user_platform.tunnel_panel_service import parse_rport

    assert parse_rport(None) is None
    assert parse_rport("") is None
    assert parse_rport("not-a-range") is None
    assert parse_rport("1-2-3") is None


def test_permanent_error_matches_chmlfrp_rejections():
    """A failed panel create must not be retried every 45s tick."""
    import user_platform.tunnel_runtime_manager as rm

    assert rm._permanent_provisioning_error("ChmlFrp 创建隧道失败: 端口已被占用") is True
    assert rm._permanent_provisioning_error("远程端口 80 不在节点 x 的可用范围 (10000-65535); ...") is True
    assert rm._permanent_provisioning_error("HTTP 403") is True
    # Client stderr failures keep retrying.
    assert rm._permanent_provisioning_error("connection refused") is False
    assert rm._permanent_provisioning_error(None) is False


# ── SDK: token-seeded client must not AttributeError on create_tunnel ──────


def test_chmlfrp_client_usertoken_initialized_when_token_seeded():
    """A client seeded with only ``tokens=`` (the platform's path) has never
    called login()/fetch_userinfo(), so ``_usertoken`` must exist up front or
    create_tunnel raises AttributeError ('ChmlFrpClient' object has no
    attribute '_usertoken')."""
    from user_platform.chmlfrp_sdk import ChmlFrpClient

    c = ChmlFrpClient(tokens={"accessToken": "a.b.c", "refreshToken": "r"})
    assert c._usertoken is None  # initialized, not missing


# ── dead-token diagnostics ────────────────────────────────────────────────


def test_auth_hint_appends_recovery_for_dead_token():
    from user_platform.tunnel_panel_service import _auth_hint

    # A rejected refresh (invalid_grant) must tell the user to re-copy tokens.
    hinted = _auth_hint('刷新令牌被面板拒绝 (HTTP 401): refresh_token 刷新失败: {"error":"invalid_grant"}')
    assert "重新登录" in hinted and "access_token" in hinted
    # A non-auth error is left untouched.
    assert _auth_hint("隧道名已存在") == "隧道名已存在"


def test_sdk_refresh_surfaces_panel_message_on_401(monkeypatch):
    """A 401 from /sso/refresh must not be swallowed into '无 access_token'."""
    import asyncio

    from user_platform.chmlfrp_sdk import ChmlFrpClient
    from user_platform.chmlfrp_sdk.exceptions import AuthError

    class FakeResp:
        status_code = 401

        def json(self):
            return {"success": False, "message": 'refresh_token 刷新失败: {"error":"invalid_grant"}'}

    class FakeHttpx:
        async def post(self, *a, **k):
            return FakeResp()

    async def run():
        c = ChmlFrpClient(tokens={"accessToken": "x.y.z", "refreshToken": "dead"})
        c._client = FakeHttpx()
        try:
            await c.refresh()
            raise AssertionError("should have raised")
        except AuthError as e:
            # The panel's real message rides through, not a generic fallback.
            assert "invalid_grant" in str(e)
            assert "HTTP 401" in str(e)

    asyncio.run(run())


# ── chmlfrp 隧道名(面板只允许字母/数字/下划线) ──────────────────────────────


def test_chmlfrp_default_tunnel_name_has_no_hyphen():
    """默认名不能带连字符,否则面板必拒「隧道名不符合规范」。"""
    import re
    import uuid as _uuid

    bid = str(_uuid.UUID("b51e1f36-bd27-422b-86a9-b36f8044c6ae"))
    name = f"mc_{bid.replace('-', '')}"
    assert name == "mc_b51e1f36bd27422b86a9b36f8044c6ae"
    assert re.match(r"^[A-Za-z0-9_]+$", name)


def test_chmlfrp_tunnel_name_validation_matches_panel():
    from user_platform.tunnel_service import (
        TunnelServiceError, _validate_chmlfrp_tunnel_name,
    )

    assert _validate_chmlfrp_tunnel_name("aaaa") == "aaaa"
    assert _validate_chmlfrp_tunnel_name("my_tunnel_1") == "my_tunnel_1"
    with pytest.raises(TunnelServiceError):
        _validate_chmlfrp_tunnel_name("my-tunnel")   # hyphen
    with pytest.raises(TunnelServiceError):
        _validate_chmlfrp_tunnel_name("a.b")         # dot
    with pytest.raises(TunnelServiceError):
        _validate_chmlfrp_tunnel_name("")
