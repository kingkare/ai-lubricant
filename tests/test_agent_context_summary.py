"""Tests for the LLM summarization pass layered on the GA trim (context_manager)."""
import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.context_manager import (
    DEFAULT_CONTEXT_WINDOW,
    TRIGGER_MULTIPLIER,
    _is_summary_message,
    _summary_message,
    _transcript,
    trim_messages_async,
)


def _mk_history(turns: int, chars: int = 3000) -> list[dict]:
    """system + alternating user/assistant turns of ~chars chars each."""
    msgs: list[dict] = [{"role": "system", "content": "sys"}]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"u{i} " + "x" * chars})
        msgs.append({"role": "assistant", "content": f"a{i} " + "y" * chars})
    return msgs


class FakeLLM:
    """Returns the summary STRING directly (the contract _summarize_chunk expects).

    The real loop's ``_make_summarizer`` wraps ``client.chat`` and extracts
    ``resp.content``; tests that bypass the wrapper must return a string.
    """
    def __init__(self, summary: str = "• goal: ship the login fix\n• blocker: none"):
        self.summary = summary
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools=None):
        self.calls.append(messages)
        return self.summary


def test_transcript_flattens_blocks_and_tool_calls():
    msgs = [
        {"role": "assistant", "tool_calls": [{"function": {"name": "code_run"}}]},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "user", "content": "plain"},
    ]
    text = _transcript(msgs)
    assert "[calls code_run]" in text
    assert "hello" in text
    assert "plain" in text


def test_summary_message_is_marked():
    m = _summary_message("bullet one")
    assert m["role"] == "user"
    assert "<history_summary>" in m["content"]
    assert _is_summary_message(m)
    assert not _is_summary_message({"role": "user", "content": "plain text"})


def test_no_summarizer_falls_back_to_plain_trim():
    msgs = _mk_history(40, chars=8000)
    before = len(msgs)
    asyncio.run(trim_messages_async(msgs, context_window=30000, summarizer=None))
    assert len(msgs) < before  # plain trim still ran


def test_under_cap_no_llm_call():
    msgs = _mk_history(5, chars=100)
    llm = FakeLLM()
    asyncio.run(trim_messages_async(msgs, context_window=DEFAULT_CONTEXT_WINDOW, summarizer=llm.chat))
    assert llm.calls == []  # nothing to trim → summarizer never invoked


def test_over_cap_summarizes_and_compacts():
    # Realistic message sizes so the trim can actually reach its target: cap =
    # window * 3 = 60k chars; 120 turns x 700 chars each = 84k.
    window = 20000
    cap = window * TRIGGER_MULTIPLIER
    msgs = _mk_history(120, chars=700)
    total0 = sum(len(m.get("content", "")) for m in msgs)
    assert total0 > cap
    llm = FakeLLM()
    asyncio.run(trim_messages_async(msgs, context_window=window, summarizer=llm.chat))

    assert llm.calls, "summarizer must be invoked on overflow"
    text = "".join(m.get("content", "") if isinstance(m.get("content"), str) else "" for m in msgs)
    assert "goal: ship the login fix" in text
    summaries = [m for m in msgs if _is_summary_message(m)]
    assert len(summaries) == 1
    # system prompt survived
    assert msgs[0]["role"] == "system"
    # net compaction: far fewer messages than the 241 we started with
    assert len(msgs) < 241
    # after compaction the char budget is back under the cap
    total = sum(
        len(m["content"]) if isinstance(m.get("content"), str)
        else sum(len(b.get("text", "")) for b in m.get("content", []) if isinstance(b, dict))
        for m in msgs
    )
    assert total <= cap


def test_summarizer_failure_falls_back_to_plain_trim():
    class Broken:
        async def chat(self, messages, tools=None):
            raise RuntimeError("upstream down")

    msgs = _mk_history(40, chars=8000)
    before = len(msgs)
    asyncio.run(trim_messages_async(msgs, context_window=30000, summarizer=Broken().chat))
    # Trim still ran (dropped messages), just without a summary block.
    assert len(msgs) < before
    assert not any(_is_summary_message(m) for m in msgs)


def test_summary_survives_a_second_trim():
    """A prior summary is not re-summarized when it is the only dropped content."""
    window = 20000
    msgs = _mk_history(30, chars=30000)
    llm = FakeLLM("first summary")
    asyncio.run(trim_messages_async(msgs, context_window=window, summarizer=llm.chat))
    calls_after_first = len(llm.calls)
    assert calls_after_first == 1

    # Simulate continued growth with fresh turns only.
    for i in range(6):
        msgs.append({"role": "user", "content": f"new{i} " + "z" * 30000})
    asyncio.run(trim_messages_async(msgs, context_window=window, summarizer=llm.chat))
    # The second pass dropped mostly fresh material → another call happened.
    # (If only the summary itself were dropped, no second call would fire.)
    assert len(llm.calls) >= 1


def test_small_drop_below_min_threshold_skips_summarizer():
    # Enough to be over cap, but the planned drop is tiny (< _SUMMARY_MIN_DROPPED
    # fresh messages) → no LLM call.
    window = 20000
    msgs = [{"role": "system", "content": "sys"}]
    # Six huge turns over cap…
    for i in range(6):
        msgs.append({"role": "user", "content": f"u{i} " + "x" * 30000})
    llm = FakeLLM()
    asyncio.run(trim_messages_async(msgs, context_window=window, summarizer=llm.chat))
    # MIN_MESSAGES_AFTER_TRIM=9 blocks popping 6-message history → no drop → no call.
    assert llm.calls == []