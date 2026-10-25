"""WorkBuddy intl 档案 · 签到活动闸门 + 领取类（trial / gift / compensation / plan）。

2026-10-08 补：对齐社区 workbuddy（mmqz/cpa-multi-plugins）billing.go / checkin.go 实测形态。
2026-10-20 合并定版：原指向拆分版 intl_v2，现改指向统一版 codebuddy_code_channel.py 的
intl 档案（加载时把顶部 DEPLOY_REGION 行替换为 "intl"；签到前置闸门/领取类仅 intl 档案开）。
覆盖范围：
1. 签到活动状态双端点 OR-merge（_fetch_checkin_status / _parse_checkin_status /
   _merge_checkin_status）：缺 today_checked_in 是「证据不足」不是「未签」、checkin_dates
   含今日覆盖已签、CST 记账。
2. 前置活动闸门（_do_checkin）：活动未开启 / 今日已签 → 短路不打 daily-checkin；
   状态查不到 → fail-open 照常签。
3. 领取类（_claim_trial / _claim_bonus / _is_already_claimed / _has_trial_pack）：
   幂等靠业务码（14051 etc.），trial 包名预检短路，裸「体验」不误判。
4. 套餐档位（_fetch_payment_type）。
5. 当日完成判定（_today_checkin_done）接受 inactive，抑制「活动没开却每 30 分钟重试」。

模块级独立 exec + 鸭子类型 stub，monkeypatch 对模块级 helper/静态钩子天然生效。
不依赖 PG/Redis；specs/ 在 .gitignore 内、文件不存在时整模块跳过。
"""
from __future__ import annotations

import asyncio
import re
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "codebuddy_code_channel.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/codebuddy_code_channel.py 不存在（使用者自持）", allow_module_level=True)

# 统一版默认 DEPLOY_REGION="cn"；本文件锁 intl 档案（签到前置闸门+领取类），加载前拧开关。
_RAW_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")
_SWITCH_RE = re.compile(r'^DEPLOY_REGION = "cn"$', re.M)
assert _SWITCH_RE.search(_RAW_SOURCE), '统一版必须保留独立一行 DEPLOY_REGION = "cn"'
_SOURCE = _SWITCH_RE.sub('DEPLOY_REGION = "intl"', _RAW_SOURCE, count=1)


@pytest.fixture(scope="module")
def mod():
    module = types.ModuleType("codebuddy_intl_v2_billing_module")
    module.__file__ = str(_SPEC_PATH)
    exec(compile(_SOURCE, "<codebuddy-intl>", "exec"), module.__dict__)
    return module


def _p(**extra):
    vals = dict(base_url="https://x", api_key="tok", username="tester", user_id="uid-1",
                enterprise_id="", domain="www.workbuddy.ai", device_token="", proxy=None,
                _last_balance=None, _balance_checked_at=0, credit_remain=0)
    vals.update(extra)
    s = SimpleNamespace(**vals)
    return s


# ==================== 1. 活动状态解析 / 合并 ====================

def test_parse_checkin_status_missing_today_is_ambiguous(mod):
    s, has_today = mod._parse_checkin_status({"active": False}, "2026-10-08")
    assert s["today_checked_in"] is False
    assert has_today is False
    assert s["active"] is False


def test_parse_checkin_status_calendar_overrides_absent_today(mod):
    s, has_today = mod._parse_checkin_status(
        {"active": True, "checkin_dates": ["2026-10-07", "2026-10-08"]}, "2026-10-08")
    assert s["today_checked_in"] is True
    assert has_today is True


def test_parse_checkin_status_camel_keys(mod):
    s, has_today = mod._parse_checkin_status(
        {"active": True, "todayCheckedIn": True, "dailyCredit": 50,
         "activityName": "expert", "season": 2}, "2026-10-08")
    assert s["active"] is True and s["today_checked_in"] is True and has_today is True
    assert s["daily_credit"] == 50 and s["season"] == 2


def test_parse_checkin_status_string_bool_tolerated(mod):
    s, _ = mod._parse_checkin_status({"active": "true"}, "2026-10-08")
    assert s["active"] is True


def test_merge_checkin_status_or_semantics(mod):
    base = {"active": False, "today_checked_in": False, "has_today_evidence": False,
            "checkin_dates": [], "daily_credit": 0}
    merged = mod._merge_checkin_status(base, {"active": True, "today_checked_in": True,
                                              "daily_credit": 30})
    assert merged["active"] is True and merged["today_checked_in"] is True
    assert merged["daily_credit"] == 30  # base 缺值 → 回填


# ==================== 2. 前置活动闸门（_do_checkin）====================

def test_do_checkin_shortcircuits_when_activity_inactive(mod, monkeypatch):
    async def fake_fetch_status(p):
        return {"active": False, "today_checked_in": False, "has_today_evidence": True}

    async def fake_billing(p, method, path, body=None, timeout_s=None):
        raise AssertionError("daily-checkin 不该被调用（活动未开启已短路）")

    async def fake_query_credit(p):
        return 42

    monkeypatch.setattr(mod, "_fetch_checkin_status", fake_fetch_status)
    monkeypatch.setattr(mod, "_billing_json", fake_billing)
    monkeypatch.setattr(mod, "_query_credit", fake_query_credit)
    result = asyncio.run(mod._do_checkin(_p()))
    assert result["status"] == "inactive"
    assert result["credit_remain"] == 42


def test_do_checkin_shortcircuits_when_today_checked_in(mod, monkeypatch):
    async def fake_fetch_status(p):
        return {"active": True, "today_checked_in": True, "has_today_evidence": True}

    async def fake_billing(p, method, path, body=None, timeout_s=None):
        raise AssertionError("daily-checkin 不该被调用（今日已签已短路）")

    async def fake_query_credit(p):
        return 42

    monkeypatch.setattr(mod, "_fetch_checkin_status", fake_fetch_status)
    monkeypatch.setattr(mod, "_billing_json", fake_billing)
    monkeypatch.setattr(mod, "_query_credit", fake_query_credit)
    result = asyncio.run(mod._do_checkin(_p()))
    assert result["status"] == "already"


def test_do_checkin_fail_open_when_status_unknown(mod, monkeypatch):
    async def fake_fetch_status(p):
        return None

    async def fake_billing(p, method, path, body=None, timeout_s=None):
        return {}

    async def fake_query_credit(p):
        return 42

    monkeypatch.setattr(mod, "_fetch_checkin_status", fake_fetch_status)
    monkeypatch.setattr(mod, "_billing_json", fake_billing)
    monkeypatch.setattr(mod, "_query_credit", fake_query_credit)
    result = asyncio.run(mod._do_checkin(_p()))
    assert result["status"] == "ok"


def test_do_checkin_business_no_plan_is_inactive(mod, monkeypatch):
    """上游 explicit「没有签到计划」→ inactive（不露出裸业务错误）。"""
    async def fake_fetch_status(p):
        return None  # 状态查不到，走 daily-checkin 拿业务码

    def raise_no_plan(p, method, path, body=None, timeout_s=None):
        raise mod._CheckinError("该账号没有签到计划", 10001)

    async def fake_query_credit(p):
        return 42

    monkeypatch.setattr(mod, "_fetch_checkin_status", fake_fetch_status)
    monkeypatch.setattr(mod, "_billing_json", raise_no_plan)
    monkeypatch.setattr(mod, "_query_credit", fake_query_credit)
    result = asyncio.run(mod._do_checkin(_p()))
    assert result["status"] == "inactive"


# ==================== 3. 领取类 ====================

def test_has_trial_pack_matches_english_and_chinese(mod):
    assert mod._has_trial_pack({"Response": {"Data": {"Accounts": [
        {"PackageName": "CodeBuddy One-time Free 2-Week Pro Plan Trial"}]}}})
    assert mod._has_trial_pack({"Response": {"Data": {"Accounts": [
        {"PackageName": "专家加油包"}]}}})
    assert mod._has_trial_pack({"Response": {"Data": {"Accounts": [
        {"PackageName": "专家体验包"}]}}})


def test_has_trial_pack_bare_experience_not_matched(mod):
    # CN 免费档包名「体验版」绝不能判成已领 trial（社区 A-18 教训）。
    assert not mod._has_trial_pack({"Response": {"Data": {"Accounts": [
        {"PackageName": "CodeBuddy 个人体验版"}]}}})


def test_is_already_claimed_variants(mod):
    assert mod._is_already_claimed("has applied trial", 14051)
    assert mod._is_already_claimed("already claimed", 12345)
    assert mod._is_already_claimed("已领取", 999)
    assert not mod._is_already_claimed("签到活动未开启或已过期", 10001)


def test_is_no_checkin_plan(mod):
    assert mod._is_no_checkin_plan("没有签到计划")
    assert mod._is_no_checkin_plan("no check-in plan")
    assert not mod._is_no_checkin_plan("签到活动未开启或已过期")


def test_claim_trial_repeated_14051_is_already(mod, monkeypatch):
    async def fake_user_resource(p):
        return {"Response": {"Data": {"Accounts": []}}}

    def raise_checkin_error(p, method, path, body=None, timeout_s=None):
        raise mod._CheckinError("has applied trial", 14051)

    monkeypatch.setattr(mod, "_fetch_user_resource", fake_user_resource)
    monkeypatch.setattr(mod, "_billing_json", raise_checkin_error)
    result = asyncio.run(mod._claim_trial(_p()))
    assert result["status"] == "already"
    assert "加油包" in result["badge"]


def test_claim_trial_shortcircuit_when_pack_present(mod, monkeypatch):
    async def fake_user_resource(p):
        return {"Response": {"Data": {"Accounts": [
            {"PackageName": "CodeBuddy One-time Free 2-Week Pro Plan Trial"}]}}}

    async def no_post(p, method, path, body=None, timeout_s=None):
        raise AssertionError("已有 trial 包，不该再 POST /billing/ide/trial")

    monkeypatch.setattr(mod, "_fetch_user_resource", fake_user_resource)
    monkeypatch.setattr(mod, "_billing_json", no_post)
    result = asyncio.run(mod._claim_trial(_p()))
    assert result["status"] == "already"


def test_claim_trial_success_refreshes_credit(mod, monkeypatch):
    async def fake_user_resource(p):
        return {"Response": {"Data": {"Accounts": [
            {"PackageName": "Bonus Pack", "CycleCapacityRemain": 250}]}}}

    async def fake_post(p, method, path, body=None, timeout_s=None):
        return {}

    monkeypatch.setattr(mod, "_fetch_user_resource", fake_user_resource)
    monkeypatch.setattr(mod, "_billing_json", fake_post)
    result = asyncio.run(mod._claim_trial(_p()))
    assert result["status"] == "ok"
    assert result["credit_remain"] == 250


def test_claim_bonus_success_reads_credit_field(mod, monkeypatch):
    async def fake_post(p, method, path, body=None, timeout_s=None):
        return {"credit": 50}

    async def fake_user_resource(p):
        return {"Response": {"Data": {"Accounts": [
            {"CycleCapacityRemain": 100}]}}}

    monkeypatch.setattr(mod, "_billing_json", fake_post)
    monkeypatch.setattr(mod, "_fetch_user_resource", fake_user_resource)
    result = asyncio.run(mod._claim_bonus(_p(), mod.CLAIM_GIFT_PATHS, "成长礼包"))
    assert result["status"] == "ok"
    assert "50" in result["badge"]
    assert result["credit_remain"] == 100


# ==================== 4. 套餐档位 / 余额聚合 / 当日完成 ====================

def test_fetch_payment_type(mod, monkeypatch):
    async def fake(p, method, path, body=None, timeout_s=None):
        return {"paymentType": "free"}
    monkeypatch.setattr(mod, "_billing_json", fake)
    assert asyncio.run(mod._fetch_payment_type(_p())) == "free"


def test_fetch_payment_type_failure_returns_empty(mod, monkeypatch):
    async def fake(p, method, path, body=None, timeout_s=None):
        raise mod._CheckinError("x", 1)
    monkeypatch.setattr(mod, "_billing_json", fake)
    assert asyncio.run(mod._fetch_payment_type(_p())) == ""


def test_sum_credit_prefers_cycle_capacity(mod):
    data = {"Response": {"Data": {"Accounts": [
        {"CycleCapacityRemain": 30},
        {"CycleCapacityRemain": -5},   # 负值钳 0
        {"CapacityRemain": 20},        # 无 Cycle → 回退 Capacity
    ]}}}
    assert mod._sum_credit(data) == 50


def test_today_checkin_done_accepts_inactive(mod):
    p = _p()
    p.last_checkin_status = mod._today_key() + "|inactive"
    assert mod._today_checkin_done(p) is True
    p.last_checkin_status = mod._today_key() + "|fail"
    assert mod._today_checkin_done(p) is False


def test_badge_text_inactive(mod):
    assert mod._badge_text("inactive", None) == "签到活动未开启"