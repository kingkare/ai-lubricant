"""agent 侧「网关报上下文超限 → 自己压缩后重发」的行为测试。

网关把超限归一成 400 + 固定 code/message + `compact` 标记（见
tests/test_context_overflow_terminal.py）；这里测**客户端那一半**：我们的 agent
拿到这个信号后不再把本轮判死，而是紧急压缩历史、重发同一轮。

覆盖三点：
- `_overflow_error_code` 认得两条错误形态（网关直连的 HTTPException 信封、
  HTTP 路径的 _LLMHttpError 文本）与三种触发源（code / compact 标记 / 文案锚点）。
- 超限 → 压缩 → 重发，本轮最终成功；事件里出现 compact_status(trigger=overflow)。
- 压缩救不了（连续超限）时按预算硬上限 raise，绝不无限压。
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.agent_loop import BaseHandler, _overflow_error_code, agent_runner_loop


def _response(content="", tool_calls=None, usage=None, finish_reason=None, reasoning=""):
    return SimpleNamespace(
        content=content, tool_calls=tool_calls or [], usage=usage,
        finish_reason=finish_reason, reasoning=reasoning,
    )


class _Handler(BaseHandler):
    def __init__(self):
        super().__init__(tools_registry=AsyncMock())

    async def dispatch(self, tool_name, args, response, index=0, tool_num=1):  # pragma: no cover
        return SimpleNamespace(data="", next_prompt=None, should_exit=False)


async def _collect(*args, **kwargs):
    return [item async for item in agent_runner_loop(*args, **kwargs)]


def _overflow_detail():
    return {"error": {
        "message": "Your input exceeds this model's maximum context window. Please reduce the length of the conversation.",
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
        "compact": True,
        "suggested_action": "compact",
    }}


# ── 识别：_overflow_error_code ───────────────────────────────────

def test_detects_gateway_http_exception_envelope():
    assert _overflow_error_code(HTTPException(400, _overflow_detail())) == "context_length_exceeded"


def test_detects_entry_precheck_code_context_too_large():
    err = HTTPException(400, {"error": {"message": "Your input exceeds the context window of this model.",
                                       "type": "invalid_request_error", "code": "context_too_large"}})
    assert _overflow_error_code(err) == "context_too_large"


def test_detects_prompt_too_long_variant():
    err = HTTPException(400, {"error": {"message": "prompt is too long", "code": "prompt_too_long"}})
    assert _overflow_error_code(err) == "prompt_too_long"


def test_detects_http_bridge_error_by_message_text():
    """LLMBridge（HTTP 路径）只带 status + message 文本，靠锚点识别。"""
    class _BridgeErr(Exception):
        def __init__(self):
            super().__init__("HTTP 400: Your input exceeds this model's maximum context window.")
            self.status_code = 400
            self.message = "Your input exceeds this model's maximum context window."

    assert _overflow_error_code(_BridgeErr()) == "context_too_large"


def test_detects_compact_flag_without_code():
    err = HTTPException(400, {"error": {"message": "上游方言", "compact": True}})
    assert _overflow_error_code(err) == "context_too_large"


@pytest.mark.parametrize("err", [
    HTTPException(429, {"kind": "retry_exhausted", "code": "retry_exhausted", "message": "Request failed after retries."}),
    HTTPException(401, {"error": {"message": "Invalid API key", "code": "api_key_invalid"}}),
    HTTPException(400, {"error": {"message": "The 'x/y' model is not supported.", "type": "invalid_request_error"}}),
    ConnectionError("reset by peer"),
    ValueError("json decode failed"),
])
def test_non_overflow_errors_are_not_treated_as_overflow(err):
    assert _overflow_error_code(err) is None


# ── 行为：超限 → 压缩 → 重发成功 ─────────────────────────────────

def _fat_history(n=40, chunk=4000):
    """足量的历史，让紧急压缩确实有东西可砍（字符预算口径远超 trim 触发线）。"""
    messages = [{"role": "system", "content": "You are a test agent."}]
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"turn{i} " + ("x" * chunk)})
    return messages


@pytest.mark.asyncio
async def test_overflow_triggers_compaction_and_resends(monkeypatch):
    monkeypatch.setattr("agent.agent_loop.asyncio.sleep", AsyncMock(return_value=None))
    seen: list[int] = []
    events: list[dict] = []
    state = {"failed_once": False}

    class _Client:
        max_retries = 0

        async def stream_chat(self, messages, tools=None):
            seen.append(len(messages))
            if not state["failed_once"]:
                state["failed_once"] = True
                raise HTTPException(400, _overflow_detail())
            yield _response("recovered")

    async def on_event(ev):
        events.append(ev)

    outputs = await _collect(
        _Client(), "You are a test agent.", "继续", _Handler(),
        tools_schema=[], verbose=False, auto_compact=True,
        initial_messages=_fat_history(), on_event=on_event,
    )

    # 打了两次：第一次超限、压缩后第二次成功
    assert len(seen) == 2, "压缩后应重发同一轮"
    # 历史被压缩：第二次看到的消息条数显著减少
    assert seen[1] < seen[0] * 0.9, f"expected shrink, got {seen[0]} → {seen[1]}"
    # 本轮成功收尾（loop 的产出是 {"result": ..., "data": 正文}）
    assert any(isinstance(o, dict) and o.get("data") == "recovered" for o in outputs), outputs
    compact_events = [e for e in events if e.get("type") == "compact_status" and e.get("trigger") == "overflow"]
    assert [e["status"] for e in compact_events] == ["started", "ended"]


@pytest.mark.asyncio
async def test_overflow_gives_up_after_budget(monkeypatch):
    """压缩救不了（每次都超限）→ 按 _OVERFLOW_MAX_COMPACTS 预算硬上限 raise，不无限压。"""
    import agent.agent_loop as al
    monkeypatch.setattr(al.asyncio, "sleep", AsyncMock(return_value=None))
    seen: list[int] = []
    attempts = {"n": 0}

    class _Client:
        max_retries = 0

        async def stream_chat(self, messages, tools=None):
            attempts["n"] += 1
            seen.append(len(messages))
            raise HTTPException(400, _overflow_detail())
            yield _response("never")  # pragma: no cover

    with pytest.raises(HTTPException):
        await _collect(
            _Client(), "You are a test agent.", "hi", _Handler(),
            tools_schema=[], verbose=False, auto_compact=True,
            initial_messages=_fat_history(),
        )

    assert attempts["n"] == al._OVERFLOW_MAX_COMPACTS + 1


@pytest.mark.asyncio
async def test_overflow_not_compacted_when_auto_compact_off():
    """auto_compact=False = 调用方显式退出自动压缩（手动 /compact 仍可用）→ 直接抛。"""
    attempts = {"n": 0}

    class _Client:
        max_retries = 0

        async def stream_chat(self, messages, tools=None):
            attempts["n"] += 1
            raise HTTPException(400, _overflow_detail())
            yield _response("never")  # pragma: no cover

        async def chat(self, messages, tools=None):
            raise HTTPException(400, _overflow_detail())

    with pytest.raises(HTTPException):
        await _collect(
            _Client(), "sys", "hi", _Handler(),
            tools_schema=[], verbose=False, auto_compact=False,
            initial_messages=_fat_history(n=12),
        )

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_retry_exhausted_429_is_not_compacted(monkeypatch):
    """429 retry_exhausted 走退避重试，不该被误当超限去压历史。"""
    import agent.agent_loop as al
    monkeypatch.setattr(al.asyncio, "sleep", AsyncMock(return_value=None))
    attempts = {"n": 0}
    events: list[dict] = []

    class _Client:
        max_retries = 1

        async def stream_chat(self, messages, tools=None):
            attempts["n"] += 1
            raise HTTPException(429, {"kind": "retry_exhausted", "code": "retry_exhausted",
                                      "message": "Request failed after retries."})
            yield _response("never")  # pragma: no cover

    async def on_event(ev):
        events.append(ev)

    with pytest.raises(HTTPException):
        await _collect(
            _Client(), "sys", "hi", _Handler(),
            tools_schema=[], verbose=False, auto_compact=True,
            initial_messages=_fat_history(n=12), on_event=on_event,
        )

    # 按 429 退避重试（1 次额外），且从未触发压缩
    assert attempts["n"] == 2
    assert not [e for e in events if e.get("type") == "compact_status"]
    retry_events = [e for e in events if e.get("type") == "retry"]
    assert retry_events, "429 应发 retry 事件"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
