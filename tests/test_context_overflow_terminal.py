"""上下文/输入超限的**终端归因**契约测试。

被测的正是「网关必须报窗口超大、而不是 429 重试耗尽」这个用户诉求：

- 输入超过模型上限、上游报错（哪怕被中继降级成 5xx、丢 code、用自己方言写文案）
  之后，重试耗尽时终端必须归因成超限 code，并由 `_terminal_client_error` 转成
  **400 + 压缩提示**——而不是 429 `retry_exhausted`（那会诱导客户端退避重试一个
  必然再失败的请求，并渲染成「API Error: Request rejected (429)」）。
- 真瞬时故障（5xx/网络/超时/空响应）必须留在 429 `retry_exhausted`，照常短退避重试。
- 客户端能据 400 里的锚点文案 + `compact`/`suggested_action` 字段触发自动压缩。

绕开 endpoint 层的 config store / DB 依赖，只测纯函数 `_terminal_429_from_last_error`
与 `_terminal_client_error`。
"""

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from rate_limiter import NoAvailableAccountError


@pytest.fixture(autouse=True)
def _overflow_switch_on(monkeypatch):
    monkeypatch.setattr(main.config.Config, "context_overflow_not_retryable_enabled", lambda: True)


def _overflow_code(last_error):
    return main._terminal_429_from_last_error(last_error, max_retries=3)[0]


# ── 终端归因：上游超限（含降级/丢 code/方言）→ 超限 code，绝不 retry_exhausted ──

def test_terminal_maps_prompt_too_long():
    assert _overflow_code(HTTPException(400, {"error": {"message": "prompt is too long", "code": "prompt_too_long"}})) == "prompt_too_long"


def test_terminal_maps_downgraded_5xx_with_context_window_text():
    # 中继把 400 降级成 502、丢 code，只剩文案——旧代码这里会一路归 retry_exhausted。
    code = _overflow_code(HTTPException(502, "This model's maximum context window length is 202752 tokens"))
    assert code == "context_length_exceeded"
    assert code != "retry_exhausted"


def test_terminal_maps_chinese_relay_dialect():
    code = _overflow_code(HTTPException(500, "上下文超限：输入 token 过多，请压缩对话历史"))
    assert code == "context_length_exceeded"
    assert code != "retry_exhausted"


def test_terminal_maps_plain_string_exception():
    # 渠道 spec 直接抛原生异常（非 HTTPException），文案里的超限特征也要接住。
    assert _overflow_code(ValueError("Request too large for this model")) == "context_length_exceeded"


def test_terminal_maps_incomplete_stream_carrying_upstream_overflow():
    from providers.base import IncompleteStreamError

    wrapped = IncompleteStreamError("upstream stream ended before [DONE]", "upstream_incomplete_stream",
                                    upstream_error=HTTPException(502, "prompt is too long"))
    assert _overflow_code(wrapped) == "prompt_too_long"


# ── 真瞬时故障必须留在 429 重试通道 ────────────────────────────────

@pytest.mark.parametrize("last_error", [
    HTTPException(503, "upstream boom"),
    HTTPException(502, "Service Temporarily Unavailable"),
    HTTPException(400, "The 'x/y' model is not supported."),
    ConnectionError("reset by peer"),
    None,
])
def test_terminal_keeps_transient_failures_on_retry_exhausted(last_error):
    code, _ = main._terminal_429_from_last_error(last_error, max_retries=3)
    assert code in {"retry_exhausted", "no_available_account"}


def test_terminal_maps_429_to_rate_limit_not_overflow():
    code, _ = main._terminal_429_from_last_error(HTTPException(429, "Too many requests"), 3)
    assert code == "rate_limit_exceeded"


def test_terminal_explicit_business_code_passes_through():
    # API-Key 级失败：detail dict 带 code 时原样透传（不被文案嗅探改写）。
    err = HTTPException(429, {"kind": "api_key_quota_exhausted", "code": "api_key_quota_exhausted"})
    assert _overflow_code(err) == "api_key_quota_exhausted"


def test_terminal_selection_error_keeps_its_reason():
    err = NoAvailableAccountError(status_code=429, detail="模型 'm1' 所有账号不可用：账号处于冷却期 9",
                                  code="no_available_account", reasons={"account_cooldown": 9})
    assert _overflow_code(err) == "no_available_account"


def test_terminal_selection_error_with_window_reason_is_400_code():
    # 选路窗口过滤命中 → NoAvailableAccountError 带 context_too_large，终端转 400。
    err = NoAvailableAccountError(
        status_code=429,
        detail="模型 'm1' 所有账号不可用：预估输入超过模型上下文窗口 1；账号处于冷却期 9",
        code="context_too_large", reasons={"context_window_exceeded": 1, "account_cooldown": 9},
    )
    code, status = main._terminal_429_from_last_error(err, 3)
    assert code == "context_too_large"
    exc = main._terminal_client_error(code, 3, status)
    assert exc.status_code == 400


# ── _terminal_client_error：400（超限，附压缩提示）vs 429（可重试） ─────────

def test_terminal_client_error_overflow_is_400_with_compact_signal():
    exc = main._terminal_client_error("context_length_exceeded", 3, 502)
    assert exc.status_code == 400
    err = exc.detail["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "context_length_exceeded"
    # 机器可读压缩信号（第一方客户端据此触发/提示压缩）
    assert err["compact"] is True
    assert err["suggested_action"] == "compact"
    # 人类/第三方客户端触发器：文案必须含 Claude Code 认的锚点
    assert "context window" in err["message"].lower()
    assert err["retryable"] is False


@pytest.mark.parametrize("code", ["context_too_large", "prompt_too_long", "input_too_long",
                                  "model_context_limit", "max_tokens_exceeded", "context_length_exceeded"])
def test_terminal_client_error_all_overflow_codes_are_400(code):
    assert main._terminal_client_error(code, 0, None).status_code == 400


def test_terminal_client_error_prompt_too_long_message_matches_claude_code_trigger():
    err = main._terminal_client_error("prompt_too_long", 0, None).detail["error"]
    assert "prompt is too long" in err["message"].lower()


# ── 具体窗口大小回显：客户端据此知道该压到多少 ──────────────────────────────

def test_terminal_client_error_echoes_context_window():
    exc = main._terminal_client_error("context_length_exceeded", 3, 502, context_window=200000)
    err = exc.detail["error"]
    # 窗口大小附在锚点文案之后（锚点不能丢，否则客户端不触发压缩）
    assert "context window" in err["message"].lower()
    assert "200000 tokens" in err["message"]
    assert err["context_window"] == 200000


def test_terminal_client_error_without_window_keeps_message_clean():
    err = main._terminal_client_error("context_too_large", 0, None).detail["error"]
    assert "context_window" not in err
    assert "tokens)" not in err["message"]


def test_terminal_429_from_last_error_extracts_upstream_window():
    # 中继把 400 降级成 502，但原文带真实上限——终端必须把它抽出来回显。
    err = HTTPException(502, "This model's maximum context length is 202752 tokens. Please reduce.")
    window = main._overflow_window_from_last_error(err)
    assert window == 202752
    code, status = main._terminal_429_from_last_error(err, 3)
    exc = main._terminal_client_error(code, 3, status, context_window=window)
    assert exc.status_code == 400
    assert "202752 tokens" in exc.detail["error"]["message"]


def test_terminal_429_from_last_error_window_none_when_absent():
    assert main._overflow_window_from_last_error(HTTPException(500, "boom")) is None
    assert main._overflow_window_from_last_error(None) is None


def test_terminal_client_error_transient_stays_429_with_retry_after():
    exc = main._terminal_client_error("retry_exhausted", 3, 503)
    assert exc.status_code == 429
    assert exc.detail["kind"] == "retry_exhausted"
    assert exc.detail["retry_after"] == 1
    assert "compact" not in exc.detail


def test_terminal_client_error_no_available_account_stays_429():
    exc = main._terminal_client_error("no_available_account", 0, None)
    assert exc.status_code == 429
    # 不可重试的账号类错误不挂 Retry-After（重试无意义）
    assert not exc.detail.get("retry_after")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
