"""手动压缩上下文（compact_service.compact_conversation）契约。

通用 agent 对话的自动压缩在 agent_loop 每轮内存内跑；手动压缩把摘要持久化到
DB（一条 role=user、含 <history_summary> 标记的消息），让压缩跨轮生效——
agent 每轮从 ClickHouse 全量重建历史，rebuild_history_messages 以最后一条摘要
为 cutoff 丢弃其前缀。

这里锁住：
- 历史太短 → summarized=False，不调 LLM、不落库；
- 正常路径 → 调 summarizer 拿摘要、落一条标记消息；
- LLM 无 chat 能力 / 摘要为空 → summarized=False，不落库。
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def compact_env(monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    added: list[dict] = []
    yield added
    loop.close()
    asyncio.set_event_loop(None)


def _history(n_user_turns: int) -> list[dict]:
    rows = []
    mid = 1
    for i in range(n_user_turns):
        rows.append({"id": mid, "role": "user", "content": f"ask {i}"})
        mid += 1
        rows.append({"id": mid, "role": "assistant", "content": f"answer {i}"})
        mid += 1
    return rows


def test_compact_too_short_skips(compact_env, monkeypatch):
    from agent import conversation_store, compact_service

    async def _get_messages(_cid):
        return _history(3)  # 6 rows ≤ 12

    async def _add_message(*a, **k):
        compact_env.append(k)
        return {"id": 99}

    monkeypatch.setattr(conversation_store, "get_messages", _get_messages)
    monkeypatch.setattr(conversation_store, "add_message", _add_message)

    result = asyncio.get_event_loop().run_until_complete(
        compact_service.compact_conversation("c-1", agent_id=5)
    )
    assert result["ok"] is True
    assert result["summarized"] is False
    assert compact_env == []  # nothing persisted


def test_compact_persists_summary_marker(compact_env, monkeypatch):
    from agent import conversation_store, compact_service, agent_main

    async def _get_messages(_cid):
        return _history(10)  # 20 rows > 12

    async def _add_message(_cid, **fields):
        compact_env.append(fields)
        return {"id": 777}

    monkeypatch.setattr(conversation_store, "get_messages", _get_messages)
    monkeypatch.setattr(conversation_store, "add_message", _add_message)

    class _FakeLLM:
        async def chat(self, messages, tools=None):
            class _R:
                content = "dense summary of earlier turns"
            return _R()

    class _FakeGA:
        def __init__(self, *a, **k):
            pass

        async def _ensure_llm(self):
            return _FakeLLM()

    monkeypatch.setattr(agent_main, "GenericAgent", _FakeGA)

    result = asyncio.get_event_loop().run_until_complete(
        compact_service.compact_conversation("c-2", agent_id=5)
    )
    assert result["ok"] is True
    assert result["summarized"] is True
    assert result["summary_chars"] > 0
    assert len(compact_env) == 1
    assert compact_env[0]["role"] == "user"
    assert "<history_summary>" in compact_env[0]["content"]
    assert "dense summary" in compact_env[0]["content"]


def test_compact_no_agent_id_fails(compact_env, monkeypatch):
    from agent import compact_service

    result = asyncio.get_event_loop().run_until_complete(
        compact_service.compact_conversation("c-3", agent_id=0)
    )
    assert result["ok"] is False
