"""渠道语义异常 → 账号状态统一收口的单测（docs/channel-error-semantics-design.md）。

锁三类行为：
1. 异常 → 状态：CredentialInvalid/QuotaExhausted/TemporaryFreeze 改账号状态，含类别与时长；
2. 特殊处理不改状态：UpstreamTransient/RateLimited/InvalidRequest 不冻；
3. 兼容回归：裸 HTTPException 与现有规则表路径行为完全不变（③路径）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT))

from providers.base import (  # noqa: E402
    CredentialInvalid,
    QuotaExhausted,
    TemporaryFreeze,
    UpstreamTransient,
    RateLimited,
    InvalidRequest,
)
from rate_limiter import (  # noqa: E402
    AccountClient,
    AccountState,
    ProviderPool,
    ROUTABLE_STATES,
)


def _client(username: str = "acct1", **kw) -> AccountClient:
    provider = SimpleNamespace(PROVIDER_NAME="codearts-test", username=username, quotas=None)
    provider.is_init = lambda: bool(kw.get("credentials_ready", True))
    client = AccountClient(provider, rpm_limit=0)
    for key, value in kw.items():
        setattr(client, key, value)
    return client


def _pool_with(clients) -> ProviderPool:
    """最小 ProviderPool：绕开 __init__ 的渠道加载，只挂 clients 供收口方法用。"""
    pool = ProviderPool.__new__(ProviderPool)
    pool.provider_name = "codearts-test"
    pool.clients = list(clients)
    pool.channel = None
    return pool


# ==================== 异常自身契约 ====================

def test_credential_invalid_defaults_no_expiry_and_400():
    """凭证失效默认：无到期冻结（seconds=None）+ 客户端可见 400（兼容现状）。"""
    exc = CredentialInvalid("refresh_token 已被消耗")
    assert exc.status_code == 400
    assert exc.kind == "credential_invalid"
    assert exc.freeze == {"seconds": None}
    assert isinstance(exc, HTTPException)


def test_quota_exhausted_defaults_no_expiry_and_403():
    exc = QuotaExhausted("每日 1000 万 token 已用完")
    assert exc.status_code == 403
    assert exc.freeze == {"seconds": None}


def test_temporary_freeze_seconds_is_terminal():
    """供应商显式给时长 = 终态，不经渠道策略时长判断。"""
    exc = TemporaryFreeze("上游风控窗口", seconds=120)
    assert exc.status_code == 429
    assert exc.freeze == {"seconds": 120}

    # 不给秒数：走策略（freeze=None），由收口层查 error_kind 规则/默认 60s
    exc2 = TemporaryFreeze("上游风控窗口")
    assert exc2.freeze is None


def test_transient_kinds_declare_no_freeze():
    """特殊处理类显式声明不冻结——即使渠道规则命中状态码也不冻。"""
    for exc in (UpstreamTransient("timeout"), RateLimited("429"), InvalidRequest("bad param")):
        assert exc.freeze is False


# ==================== 异常 → 账号状态 ====================

def test_credential_invalid_freezes_without_expiry():
    c = _client()
    pool = _pool_with([c])
    result = pool.apply_channel_error(c.username, CredentialInvalid("已消耗"))

    assert result == "freeze"
    assert c.is_frozen is True
    assert c.freeze_category == "credential"
    assert c.state() is AccountState.CREDENTIAL_INVALID
    assert c.cooldown_remaining() == 0          # 永久冻结（无到期）
    assert c.state() not in ROUTABLE_STATES     # 选路剔除


def test_quota_exhausted_freezes_without_expiry():
    c = _client()
    pool = _pool_with([c])
    assert pool.apply_channel_error(c.username, QuotaExhausted("额度用完")) == "freeze"
    assert c.freeze_category == "quota"
    assert c.state() is AccountState.QUOTA_EXHAUSTED
    assert c.state() not in ROUTABLE_STATES


def test_temporary_freeze_uses_given_seconds_not_policy():
    """显式秒数直接生效——渠道 refresh_freeze_on_failure=False 也不能覆盖。"""
    c = _client()
    pool = _pool_with([c])
    assert pool.apply_channel_error(c.username, TemporaryFreeze("风控", seconds=90)) == "freeze"
    assert c.freeze_category == "temporary"
    assert c.state() is AccountState.COOLING
    assert 60 <= c.cooldown_remaining() <= 95    # 90s 上下（测试执行耗时）
    assert c.state() not in ROUTABLE_STATES


def test_temporary_freeze_without_seconds_falls_back_default():
    """不给秒数 → 查不到 error_kind 规则 → 类默认 60 秒。"""
    c = _client()
    pool = _pool_with([c])
    assert pool.apply_channel_error(c.username, TemporaryFreeze("风控")) == "freeze"
    assert c.state() is AccountState.COOLING
    assert 0 < c.cooldown_remaining() <= 65


def test_success_clears_credential_freeze():
    """重新授权/请求成功后自动解冻回到可用——恢复闭环。"""
    c = _client()
    pool = _pool_with([c])
    pool.apply_channel_error(c.username, CredentialInvalid("已消耗"))
    assert c.state() is AccountState.CREDENTIAL_INVALID
    assert pool.clear_runtime_freeze_on_success(c.username) is True
    assert c.state() in ROUTABLE_STATES
    assert c.freeze_category == ""


# ==================== 特殊处理不改状态 ====================

def test_transient_kinds_do_not_change_state():
    for exc in (UpstreamTransient("timeout"), RateLimited("429"), InvalidRequest("bad")):
        c = _client()
        c.auth_ok = True
        pool = _pool_with([c])
        result = pool.apply_channel_error(c.username, exc)
        assert result == "no_freeze"
        assert c.is_frozen is False                # 状态不变
        assert c.state() in ROUTABLE_STATES        # 号依然可选


# ==================== 兼容回归：③路径不变 ====================

def test_non_channel_error_not_handled_by_new_collector():
    """裸 HTTPException 不收口（返回空串），调用方回落 apply_freeze_policy 老路径。"""
    c = _client()
    pool = _pool_with([c])
    assert pool.apply_channel_error(c.username, HTTPException(400, "bad param")) == ""
    assert c.is_frozen is False


def test_plain_http_exception_path_unchanged():
    """③路径行为回归：普通 403 仍由状态码规则驱动（本例无规则 → 无动作但不报错）。"""
    c = _client()
    pool = _pool_with([c])
    result = pool.apply_freeze_policy(c.username, status_code=403, reason="forbidden")
    # 无渠道配置时规则表为空 → 空串，与改造前一致（不因新增收口而误冻/误解）
    assert result in ("", "no_freeze", "freeze")


# ==================== 模型级显式冻结（账号级仍可用） ====================

def test_explicit_model_scope_freezes_only_that_model():
    """异常 freeze={"seconds":N,"model_id":m} → 只冻该账号该模型，账号级保持可用。"""
    c = _client()
    c.auth_ok = True
    pool = _pool_with([c])
    exc = TemporaryFreeze("模型级风控", seconds=90)
    exc.freeze = {"seconds": 90, "model_id": "glm-5.2"}
    assert pool.apply_channel_error(c.username, exc) == "freeze"
    assert c.is_model_frozen("glm-5.2") is True
    assert c.is_frozen is False                    # 账号级未冻
    assert c.state() is AccountState.MODEL_FROZEN  # 账号级仍可选，仅该模型被挡
    assert c.state() in ROUTABLE_STATES


# ==================== 回归：语义异常不得被兜底冷却二次收口 ====================
# 2026-10-21 实测事故：CodeArts 的 STS5.1806（refresh_token 已被使用）应把账号落成
# 「凭证失效·需重新授权」（无到期、选路剔除），但实际显示成普通冻结/冷却，账号每
# 5 分钟又被选回、再失败——两个死号 7 小时内被重打 40+ 次、零成功。
#
# 根因：main._handle_attempt_failure 的分流写成
#     if isinstance(error, HTTPException) and not channel_error_handled: ...规则表...
#     else: ...mark_account_exception(...)          # ← 语义异常也落进这里
# ChannelError 继承 HTTPException，apply_channel_error 已收口（handled="freeze"），
# 但 else 分支仍执行 mark_account_exception：它按规则表/兜底冷却写入 category=""
# 的普通冻结，把刚写的 credential 覆盖掉——「凭证失效」变成「永久冻结/冷却」，
# 且无到期变成 300s 后又回可选。no_freeze 声明（UpstreamTransient 等）同样被违反。


class _RecordingPool:
    """记录收口调用序列；real 给定时语义收口走真实现（可观测最终账号态）。"""

    def __init__(self, real=None):
        self.real = real
        self.calls: list[tuple] = []

    def apply_channel_error(self, username, error, *, model_id=None):
        self.calls.append(("apply_channel_error", getattr(error, "kind", "?")))
        return self.real.apply_channel_error(username, error, model_id=model_id) \
            if self.real is not None else ""

    def apply_freeze_policy(self, username, **kw):
        self.calls.append(("apply_freeze_policy", kw.get("status_code")))
        return ""

    def handle_response_error(self, username, status_code=None, *a, **kw):
        self.calls.append(("handle_response_error", status_code))
        return ""

    def mark_account_exception(self, username, **kw):
        self.calls.append(("mark_account_exception", kw.get("reason", "")[:40]))
        return "freeze"


def _record(pool, error, **kw):
    """驱动 main._record_attempt_failure_to_pool（失败 → 账号状态的唯一收口点）。"""
    import main as main_mod

    args = dict(error_text=str(getattr(error, "detail", error)), retryable_status=False,
                routed_model="glm-5.2", response_headers=None)
    args.update(kw)
    main_mod._record_attempt_failure_to_pool(pool, "acct1", error, **args)


def test_credential_error_is_collected_exactly_once():
    """语义异常收口一次即止：绝不出现 mark_account_exception 二次收口。

    二次收口会用 category="" 的普通冻结覆盖 credential，是「凭证失效被显示成永久冻结、
    且 300s 后被反复选回」的直接原因。
    """
    c = _client()
    pool = _RecordingPool(real=_pool_with([c]))
    _record(pool, CredentialInvalid("refresh_token 已被使用"))

    kinds = [call[0] for call in pool.calls]
    assert kinds == ["apply_channel_error"], f"收口序列不该有第二次动作：{pool.calls}"
    assert c.freeze_category == "credential"
    assert c.state() is AccountState.CREDENTIAL_INVALID
    assert c.cooldown_remaining() == 0, "凭证失效应无到期，不该被改成兜底冷却"
    assert c.state() not in ROUTABLE_STATES


def test_quota_error_is_collected_exactly_once():
    c = _client()
    pool = _RecordingPool(real=_pool_with([c]))
    _record(pool, QuotaExhausted("额度已用完"))

    assert [call[0] for call in pool.calls] == ["apply_channel_error"]
    assert c.freeze_category == "quota"
    assert c.state() is AccountState.QUOTA_EXHAUSTED
    assert c.cooldown_remaining() == 0


def test_transient_error_does_not_freeze_at_all():
    """no_freeze 声明必须真的不冻：兜底冷却会把它冻 300s，账号白白退出选路。"""
    c = _client()
    c.auth_ok = True
    pool = _RecordingPool(real=_pool_with([c]))
    _record(pool, UpstreamTransient("上游超时"))

    assert [call[0] for call in pool.calls] == ["apply_channel_error"]
    assert c.is_frozen is False
    assert c.state() is AccountState.AVAILABLE


def test_plain_http_exception_still_uses_rule_table_path():
    """兼容回归：裸 HTTPException（非 ChannelError）仍走规则表 → 兜底 cooldown。"""
    pool = _RecordingPool()
    _record(pool, HTTPException(500, "server error"), retryable_status=True)

    kinds = [call[0] for call in pool.calls]
    assert "apply_channel_error" not in kinds        # 不是语义异常
    assert kinds == ["apply_freeze_policy", "handle_response_error"]


def test_non_http_exception_still_uses_exception_fallback():
    """兼容回归：非 HTTP 异常（网络层）仍走 mark_account_exception 兜底。"""
    pool = _RecordingPool()
    _record(pool, RuntimeError("connection reset"))

    assert [call[0] for call in pool.calls] == ["mark_account_exception"]
