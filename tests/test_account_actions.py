"""账号动作框架（server/admin.py）的单元契约。

覆盖：
1. ``_account_action_due``：daily 按每日时刻（daily_time，重启当天补跑）、
   minutes/hours 按间隔数字、未知单位不跑；
2. ``_account_action_config``：**只**从 spec 的 default 解析生效调度配置（无用户覆盖）；
3. ``_account_actions_meta``：spec 声明 + refresh_auth 自动合成（SCHEDULED_REFRESH
   决定默认 enabled）+ 每条带解析好的 config；
4. ``_run_account_action_once``：分发 refresh_auth / perform_action、_error 弹出不落库、
   失败不抛异常、执行结果记 _action_last_result；
5. ``_parse_daily_time``：HH:MM 解析与非法回落。
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
if str(_PROJ / "server") not in sys.path:
    sys.path.insert(0, str(_PROJ / "server"))

import admin
from providers.base import BaseProvider


def _run(coro):
    return asyncio.run(coro)


# ==================== _account_action_due ====================

def test_due_first_run_always():
    now = time.time()
    assert admin._account_action_due(0, {"frequency_unit": "daily"}, now) is True
    assert admin._account_action_due(0, {"frequency_unit": "minutes", "frequency_value": 30}, now) is True


def test_due_daily_time_of_day():
    from datetime import datetime
    # daily_time=10:00；现在 14:00，今天 10:00 已过
    now = datetime(2026, 9, 29, 14, 0, 0).timestamp()
    target_today = datetime(2026, 9, 29, 10, 0, 0).timestamp()
    # 上次在今天的 target 之前（昨天 12:00）→ 已过 target 且上次早于 target → due
    assert admin._account_action_due(target_today - 3600, {"frequency_unit": "daily", "daily_time": "10:00"}, now) is True
    # 上次在今天 target 之后（今天 12:00）→ 今天已跑过 → not due
    assert admin._account_action_due(target_today + 3600, {"frequency_unit": "daily", "daily_time": "10:00"}, now) is False
    # 现在是 09:00（target 之前）：首跑（last_run=0）→ due；昨天跑过 → 等到今天 10:00
    early = datetime(2026, 9, 29, 9, 0, 0).timestamp()
    assert admin._account_action_due(0, {"frequency_unit": "daily", "daily_time": "10:00"}, early) is True
    assert admin._account_action_due(datetime(2026, 9, 28, 14).timestamp(),
                                     {"frequency_unit": "daily", "daily_time": "10:00"}, early) is False


def test_parse_daily_time():
    assert admin._parse_daily_time("09:30") == (9, 30)
    assert admin._parse_daily_time("23:59") == (23, 59)
    assert admin._parse_daily_time(None) == (10, 0)       # 缺省 = 10:00（对齐原每日 10:00）
    assert admin._parse_daily_time("garbage") == (10, 0)  # 非法回落
    assert admin._parse_daily_time("25:99") == (10, 0)
    assert admin._parse_daily_time("7") == (10, 0)


def test_due_minutes_hours():
    now = time.time()
    assert admin._account_action_due(now - 31 * 60, {"frequency_unit": "minutes", "frequency_value": 30}, now) is True
    assert admin._account_action_due(now - 29 * 60, {"frequency_unit": "minutes", "frequency_value": 30}, now) is False
    assert admin._account_action_due(now - 7 * 3600, {"frequency_unit": "hours", "frequency_value": 6}, now) is True
    assert admin._account_action_due(now - 5 * 3600, {"frequency_unit": "hours", "frequency_value": 6}, now) is False
    assert admin._account_action_due(now - 10**9, {"frequency_unit": "weekly"}, now) is False


def test_interval_seconds():
    assert admin._account_action_interval_seconds({"frequency_unit": "daily"}) == 24 * 3600
    assert admin._account_action_interval_seconds({"frequency_unit": "minutes", "frequency_value": 30}) == 1800
    assert admin._account_action_interval_seconds({"frequency_unit": "hours", "frequency_value": 6}) == 21600
    assert admin._account_action_interval_seconds({"frequency_unit": "weekly"}) == 0


# ==================== _account_action_config（调度真相源 = spec default）====================

def test_config_resolves_from_spec_default_only():
    """生效配置只从 spec 的 default 解析；不再读用户覆盖（cfg 里没有 account_actions 概念）。"""
    action = {"id": "checkin", "default": {"enabled": True, "frequency_unit": "hours",
                                           "frequency_value": 12, "daily_time": "03:00"}}
    merged = admin._account_action_config(action)
    assert merged == {"enabled": True, "frequency_unit": "hours", "frequency_value": 12,
                      "daily_time": "03:00", "accounts": []}


def test_config_defaults_when_spec_empty():
    """spec 没给 default → 归一为 daily 10:00 / 全账号（与后端合成 refresh_auth 同口径）。"""
    merged = admin._account_action_config({"id": "x", "default": {}})
    assert merged == {"enabled": True, "frequency_unit": "daily", "frequency_value": 1,
                      "daily_time": "10:00", "accounts": []}


def test_config_default_disabled_stays_disabled():
    action = {"id": "x", "default": {"enabled": False, "frequency_unit": "daily", "frequency_value": 1}}
    merged = admin._account_action_config(action)
    assert merged["enabled"] is False


def test_config_normalizes_garbage_and_accounts():
    """非法单位回落 daily、非法数字回落 1、非法 daily_time 回落 10:00、accounts 去空。"""
    action = {"id": "x", "default": {"frequency_unit": "weekly", "frequency_value": "abc",
                                     "daily_time": "nonsense", "accounts": ["a", "", 1, None]}}
    merged = admin._account_action_config(action)
    assert merged["frequency_unit"] == "daily"
    assert merged["frequency_value"] == 1
    assert merged["daily_time"] == "10:00"
    assert merged["accounts"] == ["a"]   # 只有字符串项进得来（default.accounts 已过滤）


def test_meta_carries_resolved_config(monkeypatch):
    """_account_actions_meta 每条带解析好的 config（前端只读面板直接用，不再前端合并）。"""
    schema = {"account_actions": [
        {"id": "checkin", "label": "签到", "manual": True,
         "display": {"format": "badge"},
         "default": {"enabled": True, "frequency_unit": "hours", "frequency_value": 6}},
    ]}
    _patch_meta(monkeypatch, schema, _NoRefreshProvider)
    metas = {m["id"]: m for m in admin._account_actions_meta("p", {})}
    assert metas["checkin"]["config"] == {"enabled": True, "frequency_unit": "hours",
                                          "frequency_value": 6, "daily_time": "10:00", "accounts": []}


# ==================== _account_actions_meta ====================

class _RefreshProvider:
    """声明了 refresh_account_auth 的替身（不继承 BaseProvider：抽象方法太多）。"""
    PROVIDER_NAME = "has-refresh"

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def refresh_account_auth(self, account, cfg=None):
        return {}


class _NoRefreshProvider:
    """没覆盖 refresh_account_auth：类属性直接指向 BaseProvider 的 501 桩。"""
    PROVIDER_NAME = "no-refresh"
    refresh_account_auth = BaseProvider.refresh_account_auth

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _patch_meta(monkeypatch, schema, provider_class):
    monkeypatch.setattr(admin, "_provider_account_schema", lambda name, cfg: schema)
    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg=None: provider_class)


def test_meta_from_schema_and_refresh_synthesis(monkeypatch):
    schema = {"account_actions": [
        {"id": "checkin", "label": "每日签到", "manual": True,
         "display": {"format": "badge", "badge_field": "last_checkin"},
         "default": {"enabled": True, "frequency_unit": "daily", "frequency_value": 1}},
    ]}
    _patch_meta(monkeypatch, schema, _RefreshProvider)
    metas = {m["id"]: m for m in admin._account_actions_meta("p", {})}
    assert set(metas) == {"checkin", "refresh_auth"}
    assert metas["checkin"]["label"] == "每日签到"
    assert metas["checkin"]["manual"] is True
    assert metas["checkin"]["display"]["badge_field"] == "last_checkin"
    # SCHEDULED_REFRESH 未声明 → refresh_auth 默认不启用（只留手动按钮）
    assert metas["refresh_auth"]["default"]["enabled"] is False


def test_meta_scheduled_refresh_enables_default(monkeypatch):
    class _Scheduled(_RefreshProvider):
        SCHEDULED_REFRESH = True

    _patch_meta(monkeypatch, {}, _Scheduled)
    metas = {m["id"]: m for m in admin._account_actions_meta("p", {})}
    assert metas["refresh_auth"]["default"]["enabled"] is True


def test_meta_no_refresh_no_synthesis(monkeypatch):
    _patch_meta(monkeypatch, {}, _NoRefreshProvider)
    assert admin._account_actions_meta("p", {}) == []


def test_meta_schema_error_does_not_raise(monkeypatch):
    def boom(name, cfg):
        raise RuntimeError("schema 写挂了")
    monkeypatch.setattr(admin, "_provider_account_schema", boom)
    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg=None: _NoRefreshProvider)
    assert admin._account_actions_meta("p", {}) == []


# ==================== supports_refresh_auth（「刷新授权」按钮显隐真相源） ====================

def test_schema_supports_refresh_auth_flag(monkeypatch):
    """account-schema 响应带 supports_refresh_auth（渠道代码决定，非 auth_start 推断）。

    没实现 refresh_auth 的渠道（如静态密钥 inscode：声明了 device_code 却没该钩子）
    必须是 False——否则前端会显示一个点了就 400 的「刷新授权」按钮。
    """
    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg=None: _RefreshProvider)
    out = admin._normalize_account_schema("p", {}, {})
    assert out["supports_refresh_auth"] is True

    monkeypatch.setattr(admin, "_get_provider_class", lambda name, cfg=None: _NoRefreshProvider)
    out = admin._normalize_account_schema("p", {}, {})
    assert out["supports_refresh_auth"] is False

    # spec 加载炸了 → 保守 False（按钮不显示，不会拿 400 骗用户）
    def boom(name, cfg=None):
        raise RuntimeError("class 加载失败")
    monkeypatch.setattr(admin, "_get_provider_class", boom)
    out = admin._normalize_account_schema("p", {}, {})
    assert out["supports_refresh_auth"] is False


# ==================== _run_account_action_once ====================

class _ActionProvider:
    PROVIDER_NAME = "action-test"
    last_call: tuple | None = None

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def refresh_account_auth(self, account, cfg=None):
        return {"password": "new-token"}

    async def perform_account_action(self, action_id, account, cfg=None):
        type(self).last_call = (action_id, dict(account))
        if action_id == "checkin":
            return {"last_checkin": "已签到 +80", "last_checkin_status": "2026-09-28|ok|+80"}
        if action_id == "fail_badge":
            return {"last_checkin": "签到失败", "_error": "一个都没签成"}
        if action_id == "quota_full":
            # 1005 名额用完：带 _next_run_at（epoch 毫秒）= 恢复时刻
            return {"last_claim": "领取 0/1", "_next_run_at": 1_800_000_000_000}
        raise ValueError(f"未知动作 {action_id}")


def _patch_run(monkeypatch, provider=None, account=None):
    provider = provider or _ActionProvider(username="u1", password="old")
    account = account if account is not None else {"username": "u1", "password": "old"}
    monkeypatch.setattr(admin, "_provider_instance_for_account", lambda name, cfg, acc: provider)
    persisted: list[dict] = []

    async def _fake_read(name):
        return {"accounts": [account]}

    async def _fake_persist(name, acc):
        persisted.append(acc)

    monkeypatch.setattr(admin, "_read_provider_config", _fake_read)
    monkeypatch.setattr(admin, "_persist_account", _fake_persist)
    monkeypatch.setattr(admin, "_reload_account_into_pool", lambda *a, **k: None)
    monkeypatch.setattr(admin, "_normalize_account_proxy_ref", lambda acc, proxies: acc)
    monkeypatch.setattr(admin, "_read_proxies", lambda: [])
    monkeypatch.setattr(admin, "_provider_rpm", lambda cfg: 0)
    monkeypatch.setattr(admin, "_provider_extra", lambda name, cfg: {})
    return provider, account, persisted


def test_run_refresh_auth(monkeypatch):
    _, account, persisted = _patch_run(monkeypatch)
    result = _run(admin._run_account_action_once("p", "refresh_auth", account))
    assert result["ok"] is True
    assert result["merged"]["password"] == "new-token"
    assert persisted and persisted[0]["password"] == "new-token"
    run = admin._action_last_result[("p", "refresh_auth", "u1")]
    assert run["ok"] is True and run["error"] == ""


def test_run_perform_action(monkeypatch):
    provider, account, persisted = _patch_run(monkeypatch)
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert result["ok"] is True
    assert result["merged"]["last_checkin"] == "已签到 +80"
    assert provider.last_call[0] == "checkin"
    assert provider.last_call[1]["username"] == "u1"


def test_run_error_key_popped_not_persisted(monkeypatch):
    _, account, persisted = _patch_run(monkeypatch)
    result = _run(admin._run_account_action_once("p", "fail_badge", account))
    assert result["ok"] is False
    assert result["error"] == "一个都没签成"
    assert "_error" not in result["merged"], "_error 不能落库"
    assert "_error" not in persisted[0]
    assert result["merged"]["last_checkin"] == "签到失败", "徽标字段照常落库"
    run = admin._action_last_result[("p", "fail_badge", "u1")]
    assert run["ok"] is False and run["error"] == "一个都没签成"


def test_run_unknown_action_fails_without_raise(monkeypatch):
    _, account, persisted = _patch_run(monkeypatch)
    result = _run(admin._run_account_action_once("p", "nope", account))
    assert result["ok"] is False
    assert "nope" in result["error"]
    assert persisted == [], "失败不落库"
    run = admin._action_last_result[("p", "nope", "u1")]
    assert run["ok"] is False


def test_next_run_at_registered_and_not_persisted(monkeypatch):
    """spec 返回 _next_run_at（毫秒）：登记进内存补跑表（转秒），且不落库、不影响 ok。"""
    admin._action_next_run_at.pop(("p", "quota_full", "u1"), None)
    try:
        _, account, persisted = _patch_run(monkeypatch)
        result = _run(admin._run_account_action_once("p", "quota_full", account))
        assert result["ok"] is True, "带 _next_run_at 不算失败"
        assert "_next_run_at" not in result["merged"], "_next_run_at 是框架私有键，不落库"
        assert "_next_run_at" not in persisted[0]
        # 毫秒 → 秒
        assert admin._action_next_run_at[("p", "quota_full", "u1")] == 1_800_000_000.0
    finally:
        admin._action_next_run_at.pop(("p", "quota_full", "u1"), None)


def test_next_run_at_garbage_ignored(monkeypatch):
    """_next_run_at 非法值（非数字）静默忽略，不炸执行路径。"""
    class _Bad(_ActionProvider):
        async def perform_account_action(self, action_id, account, cfg=None):
            return {"last_claim": "x", "_next_run_at": "not-a-number"}
    admin._action_next_run_at.pop(("p", "checkin", "u1"), None)
    try:
        _, account, _ = _patch_run(monkeypatch, provider=_Bad(username="u1", password="old"))
        result = _run(admin._run_account_action_once("p", "checkin", account))
        assert result["ok"] is True
        assert ("p", "checkin", "u1") not in admin._action_next_run_at
    finally:
        admin._action_next_run_at.pop(("p", "checkin", "u1"), None)


def test_run_refresh_unsupported(monkeypatch):
    provider = _NoRefreshProvider(username="u1", password="old")
    _, account, persisted = _patch_run(monkeypatch, provider=provider)
    result = _run(admin._run_account_action_once("p", "refresh_auth", account))
    assert result["ok"] is False
    assert "未实现" in result["error"]
    assert persisted == []


# ==================== 签到结构化契约（_checkin_status/_checkin_detail） ====================

def _patch_pool(monkeypatch, cleared_calls: list):
    """钉住 ModelClientPool.get_provider_pool：返回带 clear_runtime_freeze_on_success 的桩池。"""
    class _Pool:
        def clear_runtime_freeze_on_success(self, username, model_id=None):
            cleared_calls.append(username)
            return True
    monkeypatch.setattr(admin.ModelClientPool, "get_provider_pool", classmethod(
        lambda cls, name: _Pool()))


class _CheckinProvider:
    """按新契约返回 _checkin_status/_checkin_detail 的签到渠道桩。"""
    PROVIDER_NAME = "checkin-test"

    def __init__(self, result: dict, **kwargs):
        self._result = result
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def perform_account_action(self, action_id, account, cfg=None):
        return dict(self._result)


def test_checkin_ok_formats_badge_and_unfreezes(monkeypatch):
    """签到成功：框架统一格式化徽标字段（覆盖 spec 自拼串）、记 status/detail、**解冻账号**。"""
    cleared = []
    _patch_pool(monkeypatch, cleared)
    provider = _CheckinProvider(
        {"last_checkin": "spec 自己拼的串（应被框架覆盖）",
         "_checkin_status": "ok", "_checkin_detail": "+80"}, username="u1", password="old")
    _, account, persisted = _patch_run(monkeypatch, provider=provider)
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert result["ok"] is True
    # 框架是徽标字段的最终权威：spec 自拼串被覆盖
    assert result["merged"]["last_checkin"] == "签到成功 +80"
    assert result["merged"]["last_checkin_status"].startswith(admin._today_key())
    assert "|ok|+80" in result["merged"]["last_checkin_status"]
    assert persisted[0]["last_checkin"] == "签到成功 +80"
    # 元数据键不落库
    assert "_checkin_status" not in result["merged"]
    assert "_checkin_detail" not in persisted[0]
    # 签到成功 → 解冻
    assert cleared == ["u1"]
    run = admin._action_last_result[("p", "checkin", "u1")]
    assert run["status"] == "ok" and run["detail"] == "+80"


def test_checkin_failed_does_not_unfreeze(monkeypatch):
    """签到失败：徽标记失败、记录原因，**不解冻**——失败不能证明账号有额度。"""
    cleared = []
    _patch_pool(monkeypatch, cleared)
    provider = _CheckinProvider({"_checkin_status": "failed", "_checkin_detail": "上游 500"},
                                username="u1", password="old")
    _, account, _ = _patch_run(monkeypatch, provider=provider)
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert result["ok"] is True, "签到失败不算动作执行失败（_error 才算）"
    assert result["merged"]["last_checkin"] == "签到失败: 上游 500"
    assert result["merged"]["last_checkin_status"].endswith("|error")
    assert cleared == [], "失败不解冻"
    run = admin._action_last_result[("p", "checkin", "u1")]
    assert run["status"] == "failed" and run["detail"] == "上游 500"


def test_checkin_skipped_does_not_unfreeze(monkeypatch):
    """跳过（今日已签/条件不满足）：记 skipped + 原因，**不解冻**——跳过不能证明账号有额度。"""
    cleared = []
    _patch_pool(monkeypatch, cleared)
    provider = _CheckinProvider({"_checkin_status": "skipped", "_checkin_detail": "今日已签到"},
                                username="u1", password="old")
    _, account, _ = _patch_run(monkeypatch, provider=provider)
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert result["ok"] is True
    assert result["merged"]["last_checkin"] == "今日已签到"
    assert result["merged"]["last_checkin_status"].endswith("|already")
    assert result["merged"]["checkin_runs"]["checkin"]["status"] == "skipped"
    assert cleared == [], "跳过不解冻"
    run = admin._action_last_result[("p", "checkin", "u1")]
    assert run["status"] == "skipped" and run["detail"] == "今日已签到"


def test_checkin_condition_gate_skips(monkeypatch):
    """条件门（checkin_condition 钩子）ok=False：不碰 perform_action，直接记 skipped。"""
    cleared = []
    _patch_pool(monkeypatch, cleared)
    calls = []

    class _Gated(_CheckinProvider):
        async def perform_account_action(self, action_id, account, cfg=None):
            calls.append(action_id)
            return {"_checkin_status": "ok", "_checkin_detail": "+80"}

        async def checkin_condition(self, action_id, account, cfg=None):
            return {"ok": False, "reason": "今日已签到"}

    _, account, _ = _patch_run(monkeypatch, provider=_Gated({"_checkin_status": "ok"},
                                                            username="u1", password="old"))
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert result["ok"] is True
    assert calls == [], "条件门拦下后不该执行 perform_action"
    assert result["merged"]["last_checkin"] == "今日已签到"
    assert cleared == [], "条件门跳过不解冻"


def test_checkin_condition_gate_passes_through(monkeypatch):
    """条件门 ok=True：照常执行 perform_action。"""
    calls = []

    class _Gated(_CheckinProvider):
        async def perform_account_action(self, action_id, account, cfg=None):
            calls.append(action_id)
            return {"_checkin_status": "ok", "_checkin_detail": "+80"}

        async def checkin_condition(self, action_id, account, cfg=None):
            return {"ok": True}

    cleared = []
    _patch_pool(monkeypatch, cleared)
    _, account, _ = _patch_run(monkeypatch, provider=_Gated({}, username="u1", password="old"))
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert calls == ["checkin"]
    assert result["ok"] is True
    assert cleared == ["u1"]


def test_checkin_no_status_keys_legacy_path(monkeypatch):
    """老 spec（不返回 _checkin_status）：走原有窄写路径，不解冻、不覆盖 spec 自拼串。"""
    cleared = []
    _patch_pool(monkeypatch, cleared)
    provider = _CheckinProvider({"last_checkin": "已签到 +80", "quota_text": "100"},
                                username="u1", password="old")
    _, account, persisted = _patch_run(monkeypatch, provider=provider)
    result = _run(admin._run_account_action_once("p", "checkin", account))
    assert result["ok"] is True
    assert result["merged"]["last_checkin"] == "已签到 +80", "老 spec 自拼串原样落库"
    assert cleared == [], "无结构化状态不解冻"
    run = admin._action_last_result[("p", "checkin", "u1")]
    assert "status" not in run


def test_checkin_display_fields_fallback():
    """徽标字段名取 spec 声明（badge_field/status_field）；schema 求值失败回落约定键。"""
    assert admin._checkin_display_fields("p", {}, "checkin") == ("last_checkin", "last_checkin_status")


# ==================== 调度器启动的全局状态 ====================

class _DoneTask:
    """已完成的任务替身：start() 的幂等保护用 done() 判定。"""
    def done(self):
        return True


def test_start_scheduler_sets_queue_and_pending(monkeypatch):
    """回归：首版 start 漏声明 global queue/pending，赋值成局部变量、模块全局仍是 None，
    loop 60s 后第一次 put_nowait 就炸（'NoneType' object has no attribute 'put_nowait'）。"""
    async def _noop():
        return None

    admin._account_action_task = None
    admin._account_action_worker_task = None
    admin._account_action_queue = None
    admin._account_action_pending = None
    monkeypatch.setattr(admin, "_account_action_loop", _noop)
    monkeypatch.setattr(admin, "_account_action_worker", _noop)

    _run(admin.start_account_action_scheduler())
    try:
        assert admin._account_action_queue is not None, "queue 必须写到模块全局"
        assert admin._account_action_pending is not None, "pending 必须写到模块全局"
        admin._account_action_pending.add(("p", "u", "a"))
        admin._account_action_queue.put_nowait({"provider_name": "p"})
        assert ("p", "u", "a") in admin._account_action_pending
    finally:
        admin._account_action_queue = None
        admin._account_action_pending = None
        admin._account_action_task = _DoneTask()
        admin._account_action_worker_task = _DoneTask()
        _run(admin.stop_account_action_scheduler())
    assert admin._account_action_queue is None
