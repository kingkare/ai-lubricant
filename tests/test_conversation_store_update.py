"""update_message / update_conversation 的非 Nullable 列归一。

调用方表达「清空字段」的写法是传 None（两条重试路径都传 model=None），而
agent_messages / agent_conversations 除 error / llm_model_id 等少数字段外全部是
非 Nullable 列。显式传了 None 的键是**存在**的，msg.get(k, default) 的默认值不
参与，None 一路带进 insert 行，ClickHouse 报

    DataError: Invalid None value in non-Nullable column, column name: `model`

回归背景：CDP 面板点重试 → /agent/client/.../messages/{id}/retry →
update_message(model=None) → 500。
"""
from __future__ import annotations

import pytest

from agent import conversation_store


class _RowClient:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.inserted: list[tuple[str, list]] = []

    async def query(self, sql: str, params: dict):
        return self.rows

    async def insert_message_row(self, row: list) -> None:
        self.inserted.append(("message", row))

    async def insert_conversation_row(self, row: list) -> None:
        self.inserted.append(("conversation", row))


def _msg_row() -> dict:
    return {
        "id": 651, "conversation_id": "c1", "role": "assistant", "content": "old",
        "tool_calls": "", "tool_results": "", "turn_number": 2, "status": "error",
        "error": "boom", "media": "", "model": "opus", "usage": "",
        "reasoning": "", "created_at": "2026-09-28 09:00:00.000",
    }


@pytest.mark.asyncio
async def test_update_message_none_model_becomes_empty_string(monkeypatch):
    """重试路径的 model=None / error=None / usage=None 必须按列类型归一落库。"""
    client = _RowClient([_msg_row()])
    monkeypatch.setattr(conversation_store, "_client", client)
    monkeypatch.setattr(conversation_store, "_ready", True)

    # 与 cdp_chat_service.client_retry_message / agent/api.py retry_message 同参。
    await conversation_store.update_message(
        651, content="", tool_calls=None, tool_results=None, status="streaming",
        error=None, model=None, usage=None, reasoning="",
    )

    kind, row = client.inserted[0]
    assert kind == "message"
    # 列序与 insert_message_row 的 insert_message_row([...]) 一致。
    assert row[3] == ""           # content：非 Nullable，None 归一成 ""
    assert row[6] == 2            # turn_number：未传保持原值
    assert row[7] == "streaming"  # status
    assert row[8] is None         # error 是 Nullable(String)，None 合法
    assert row[10] == ""          # model：非 Nullable，None 必须归一成 ""


@pytest.mark.asyncio
async def test_update_conversation_none_model_becomes_empty_string(monkeypatch):
    conv_row = {
        "id": "c1", "title": "t", "system_prompt": "s", "model": "opus",
        "llm_model_id": None, "status": "active", "kind": "agent",
        "agent_id": 1, "user_id": "u", "cdp_client_id": None,
        "chat_settings": "", "created_at": "2026-09-28 09:00:00.000",
        "updated_at": "2026-09-28 09:00:00.000",
    }
    client = _RowClient([conv_row])
    monkeypatch.setattr(conversation_store, "_client", client)
    monkeypatch.setattr(conversation_store, "_ready", True)

    await conversation_store.update_conversation("c1", model=None)

    kind, row = client.inserted[0]
    assert kind == "conversation"
    # insert_conversation_row 列序：id, title, system_prompt, model, ...
    assert row[1] == "t"
    assert row[3] == ""           # model：非 Nullable，None 归一成 ""
    assert row[4] is None         # llm_model_id 是 Nullable，None 合法
