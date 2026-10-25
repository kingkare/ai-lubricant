"""渠道自定义出站体量检查（选路阶段）——check_message 带 reason + 请求上下文下放。

背景（2026-10-11）：腾讯 IMA 渠道对 qa 的 question 字段有 122880 字节（120KiB）硬截断
（黑盒实测：122880B 过、122881B 拒；与模型/token/max_tokens 无关）。spec 自管 stream_chat
的渠道，框架现有三层上下文控制都管不到。定稿：**选路阶段**让渠道检测方法（check_message）
拿到请求数据（messages/tools/tool_choice，经任务级 ContextVar 下放），渠道自己复现出站体
判定「这条请求发到我这儿装不装得下」，不满足即拒绝该候选；全部候选都拒时收口成
context_too_large(400) 而非误导性的 quota_exhausted(429)。渠道没定义检测逻辑走原逻辑。

本文件锁三件事：
1. 适配器归一：钩子返回 bool / (bool, reason) 都正确消费；老三参钩子照常；request_ctx
   只传给容得下第 4 参的钩子（签名探测，绝不先多传撞 TypeError 重放）；
2. manager 消费：check_message_with_reason 带出的 reason 进 LimitDecision（reason 空回退
   旧 provider_message_quota_exceeded）；不继承 BaseProvider 的 provider（测试桩）走
   getattr 兜底退回 check_message；
3. 收口码回归钉住：context_window_exceeded → context_too_large（429 覆盖路径不再吞掉它）。
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.code_loader import load_code_provider_class, invalidate_cache
from rate_limiter import (
    ModelClientPool,
    NoAvailableAccountError,
    begin_routing_request_ctx,
    end_routing_request_ctx,
    current_routing_request_ctx,
    _dominant_reason_code,
)
from limits.manager import LimitManager
from limits.rules import LimitDecision


# ==================== 1. 适配器归一（spec 钩子返回值消费） ====================

_SPEC_4ARG = '''
class CodeChannel:
    async def check_message(p, model_id, messages, request_ctx=None):
        ctx = request_ctx or {}
        if ctx.get("tools"):
            return (False, "context_window_exceeded")
        return True
'''

_SPEC_3ARG = '''
class CodeChannel:
    async def check_message(p, model_id, messages):
        return bool(messages)
'''


@pytest.fixture(scope="module")
def cls4():
    cls = load_code_provider_class("ctx4-test", _SPEC_4ARG)
    yield cls
    invalidate_cache("ctx4-test")


@pytest.fixture(scope="module")
def cls3():
    cls = load_code_provider_class("ctx3-test", _SPEC_3ARG)
    yield cls
    invalidate_cache("ctx3-test")


def _mk(cls, cookies="k=v"):
    return cls(username="u", password="", cookies=cookies)


@contextmanager
def _ctx(mapping):
    token = begin_routing_request_ctx(mapping)
    try:
        yield
    finally:
        end_routing_request_ctx(token)


def test_hook_true_normalizes_to_true(cls4):
    p = _mk(cls4)
    assert _run(p.check_message("m", [{"role": "user", "content": "hi"}])) is True
    assert _run(p.check_message_with_reason("m", [{"role": "user", "content": "hi"}])) == (True, "")


def test_hook_tuple_reason_passes_through(cls4):
    """钩子带 (False, reason) → 新方法透传 reason；check_message 同判 False（bool 身份保住）。"""
    p = _mk(cls4)
    with _ctx({"tools": [{"name": "Bash"}]}):
        assert _run(p.check_message_with_reason("m", [{"role": "user", "content": "hi"}])) \
            == (False, "context_window_exceeded")
        assert _run(p.check_message("m", [{"role": "user", "content": "hi"}])) is False


def test_ctx_only_passed_to_4arg_hook(cls3, cls4):
    """request_ctx 只传给容得下第 4 参的钩子；老三参钩子照常三参调用。"""
    p3 = _mk(cls3)
    msgs = [{"role": "user", "content": "hi"}]
    with _ctx({"tools": ["x"]}):
        assert _run(p3.check_message("m", msgs)) is True   # 老钩子收到 msgs，判 True
        # 验证老钩子没被多传参（若多传会 TypeError → 适配器不会吞掉）
    p4 = _mk(cls4)
    with _ctx({"tools": ["x"]}):
        assert _run(p4.check_message_with_reason("m", msgs)) == (False, "context_window_exceeded")
        assert _run(p4.check_message("m", msgs)) is False            # 新钩子拿到 ctx 里的 tools


def test_no_ctx_set_falls_back_to_old_signature(cls4):
    """没 set ctx（admin 只读预检、单测）→ None → 按老签名调用，新钩子拿不到 ctx。"""
    assert current_routing_request_ctx() is None
    p = _mk(cls4)
    assert _run(p.check_message("m", [{"role": "user", "content": "hi"}])) is True


def test_check_message_keeps_bool_identity(cls4):
    """check_message 身份回归：恒等返回 bool（spec 测试与 provider_state 依赖它）。"""
    p = _mk(cls4)
    r = _run(p.check_message("m", []))
    assert r is True or r is False


def _run(coro):
    return asyncio.run(coro)


# ==================== 2. manager 消费 reason ====================

class _ReasonProvider:
    """最小 provider：check_message_with_reason 可编程，其余全放行。"""

    def __init__(self, result):
        self._result = result

    def is_init(self):
        return True

    async def check_message(self, model_id, messages=None):
        return True

    async def check_message_with_reason(self, model_id, messages=None):
        return self._result


class _StubAccountClient:
    def __init__(self, provider, username="u1"):
        self.username = username
        self.provider = provider
        self.disabled = False
        self.is_frozen = False
        self._requests = []
        self._token_usages = []

    def cooldown_remaining(self, now=None):
        return 0

    def is_model_frozen(self, model_id):
        return False

    def sync_provider_quota(self):
        return None


def _decision(provider):
    acct = _StubAccountClient(provider)
    return asyncio.run(LimitManager.account_available_locked(acct, "m1", [{"role": "user", "content": "hi"}]))


def test_manager_carries_channel_reason():
    d = _decision(_ReasonProvider((False, "context_window_exceeded")))
    assert d.allowed is False
    assert d.reason == "context_window_exceeded"


def test_manager_falls_back_to_legacy_reason_when_empty():
    d = _decision(_ReasonProvider((False, "")))
    assert d.allowed is False
    assert d.reason == "provider_message_quota_exceeded"


def test_manager_getattr_fallback_for_plain_provider():
    """不继承 BaseProvider 的 provider（测试桩）没有新方法 → 退回 check_message（bool）。"""

    class _Plain:
        def is_init(self):
            return True

        async def check_message(self, model_id, messages=None):
            return False

    d = _decision(_Plain())
    assert d.allowed is False
    assert d.reason == "provider_message_quota_exceeded"


# ==================== 3. 收口码回归（context_window_exceeded 不被 429 覆盖） ====================

def test_dominant_reason_maps_to_context_too_large():
    """选路收口：context_window_exceeded 为主因 → context_too_large（400 语义）。"""
    code, snap = _dominant_reason_code({"context_window_exceeded": 3})
    assert code == "context_too_large"
    assert snap == {"context_window_exceeded": 3}


def test_context_window_beats_lesser_reasons():
    code, _ = _dominant_reason_code(
        {"context_window_exceeded": 2, "account_cooldown": 1})
    assert code == "context_too_large"


def test_no_available_error_carries_context_code():
    """NoAvailableAccountError 带 context_too_large code（供终端 400 收口）。"""
    with pytest.raises(NoAvailableAccountError) as exc:
        ModelClientPool._raise_no_available(
            "m1", None, {"context_window_exceeded": 2, "account_cooldown": 9},
            code_override="context_too_large")
    assert exc.value.code == "context_too_large"
    assert exc.value.status_code == 429  # 构造时统一 429；400 由 main 终端收口转
