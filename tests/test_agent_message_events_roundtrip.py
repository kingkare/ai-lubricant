"""事件帧折叠 == 写入时的投影（④ 的核心不变量）。

`ConversationTurnWriter` 每帧双写：折叠进 ``agent_messages`` 投影，又原样记一行
``agent_message_events``。读侧时间线（``timeline=True``）把帧按 ``seq`` 重放回
content/reasoning/tool_calls/tool_results，**必须**与投影逐字相同——否则刷新后看到
的与实时看到的就是两套东西（这正是本轮重构要根治的病根：存储形态≠显示形态）。

两侧跑的是同一段 ``apply_frame``，所以等价性是结构性的；这里用 stub CH 把这条钉死，
防止将来谁在 apply_frame 里塞了「只有 live 才执行」的副作用。

也覆盖两道降级：无帧 → 原样返回投影；帧与投影的 tool_call id 序列对不上（部分写
丢失）→ 判为不完整，保留投影（宁可退回今天的行为，也不显示截断后的正文）。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from agent import conversation_store
from agent.turn_writer import ConversationTurnWriter


class _StubCH:
    """只实现折叠路径与 writer 落库用到的方法，其余交给默认异常。

    帧按 add_message_event 的真实形状存：``item_json`` 是 JSON 字符串（生产里
    clickhouse 返回的就是字符串列，读侧必须自己解析——这里保持同一形状，才能
    测出「忘了解析」会把每条消息折成空正文）。
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.message_updates: list[dict[str, Any]] = []

    async def insert_message_event_row(self, row: list) -> None:
        (
            _id, conv_id, message_id, seq, item_type, item_json,
            event_kind, tool_name, subagent_id, turn_number, status, _created, version,
        ) = row
        self.frames.append({
            "conversation_id": conv_id, "message_id": message_id, "seq": seq,
            "item_type": item_type, "item_json": item_json, "version": version,
            "tool_name": tool_name, "subagent_id": subagent_id,
        })

    async def query(self, sql: str, params: dict):
        # _next_event_seq 的懒播种（表为空 → max(seq) 取不到行 → 从 0 起）。
        return []

    async def query_message_events(self, conversation_id, *, message_ids=None, limit=5000):
        ids = set(message_ids or [])
        rows = [f for f in self.frames if not ids or f["message_id"] in ids]
        return sorted(rows, key=lambda f: (f["message_id"], f["seq"]))


@pytest.fixture()
def stub(monkeypatch):
    client = _StubCH()
    monkeypatch.setattr(conversation_store, "_client", client)
    monkeypatch.setattr(conversation_store, "_ready", True)
    monkeypatch.setattr(conversation_store, "_evt_seq", {})
    # writer 的检查点走 update_message：这里只要不炸，投影内容由断言直接看 writer。
    async def _update(message_id, **fields):
        client.message_updates.append({"id": message_id, **fields})
    monkeypatch.setattr(conversation_store, "update_message", _update)
    return client


def _assistant_row(message_id: int, **over) -> dict:
    row = {
        "id": message_id, "conversation_id": "c1", "role": "assistant",
        "content": "", "tool_calls": None, "tool_results": None,
        "turn_number": 1, "status": "done", "error": None, "media": None,
        "model": "m", "usage": None, "reasoning": "", "subagents": None,
        "created_at": "2026-10-10 00:00:00.000",
    }
    row.update(over)
    return row


async def _drain(writer: ConversationTurnWriter, events: list[dict]) -> None:
    for ev in events:
        await writer.handle(ev)


@pytest.mark.asyncio
async def test_fold_reproduces_projection_with_interleaved_offsets(stub):
    """想一步、做一步：折叠重建的正文/思考/双偏移必须与写入时逐字相同。"""
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=101)
    await _drain(writer, [
        {"type": "reasoning", "text": "先看目录。"},
        {"type": "content", "text": "我先"},
        {"type": "content", "text": "列目录。"},
        {"type": "tool_call", "id": "call_a", "name": "file_list", "args": {"p": "."}},
        {"type": "reasoning", "text": "再看那个文件。"},
        {"type": "content", "text": "读它。"},
        {"type": "tool_call", "id": "call_b", "name": "file_read", "args": {"p": "a.txt"}},
        {"type": "tool_result", "id": "call_a", "index": 0, "data": "3 files"},
        {"type": "tool_result", "id": "call_b", "index": 1, "data": "hello"},
        {"type": "content", "text": "完毕"},
        {"type": "done", "usage": {"total_tokens": 42}},
    ])

    folded = await conversation_store._fold_event_timeline("c1", [
        _assistant_row(
            101,
            content=writer.content,
            reasoning=writer.reasoning,
            tool_calls=writer.tool_calls,
            tool_results=writer.tool_results,
        )
    ])
    assert len(folded) == 1
    got = folded[0]
    assert got["content"] == writer.content
    assert got["reasoning"] == writer.reasoning
    assert got["tool_results"] == writer.tool_results
    # 逐调用比对：id 序列 + 交错用的双偏移（三端切片代码就靠这两个数）。
    assert [c["id"] for c in got["tool_calls"]] == ["call_a", "call_b"]
    for a, b in zip(got["tool_calls"], writer.tool_calls):
        assert a["content_offset"] == b["content_offset"]
        assert a["reasoning_offset"] == b["reasoning_offset"]
        assert a["status"] == b["status"]


@pytest.mark.asyncio
async def test_fold_offsets_survive_emoji(stub):
    """偏移度量是 UTF-16（对齐 JS String.length）：含 emoji 的正文折叠后仍与投影同值。"""
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=102)
    await _drain(writer, [
        {"type": "content", "text": "好😀"},      # '😀' 占 2 个 UTF-16 单元
        {"type": "tool_call", "id": "c1", "name": "shell", "args": {}},
        {"type": "tool_result", "id": "c1", "index": 0, "data": "ok"},
    ])
    folded = await conversation_store._fold_event_timeline("c1", [
        _assistant_row(102, content=writer.content, reasoning=writer.reasoning,
                       tool_calls=writer.tool_calls, tool_results=writer.tool_results)
    ])
    assert folded[0]["tool_calls"][0]["content_offset"] == 3   # 1 + 2
    assert folded[0]["content"] == "好😀"


@pytest.mark.asyncio
async def test_question_merge_and_ask_user_restamp_fold_identically(stub):
    """ask_user：问题并入正文 + 候选用的 content_offset 重打，折叠侧必须复现同一套。"""
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=103)
    await _drain(writer, [
        {"type": "content", "text": "我要问一句："},
        {"type": "tool_call", "id": "q1", "name": "ask_user", "args": {}},
        {"type": "question", "data": {"question": "选 A 还是 B？"}},
        {"type": "tool_result", "id": "q1", "index": 0, "data": "A"},
    ])
    folded = await conversation_store._fold_event_timeline("c1", [
        _assistant_row(103, content=writer.content, reasoning=writer.reasoning,
                       tool_calls=writer.tool_calls, tool_results=writer.tool_results)
    ])
    assert folded[0]["content"] == writer.content
    assert "选 A 还是 B？" in folded[0]["content"]
    assert folded[0]["tool_calls"][0]["content_offset"] == writer.tool_calls[0]["content_offset"]


@pytest.mark.asyncio
async def test_subagent_reasoning_is_persisted_to_both_stores(stub):
    """① 子 agent 的思考：既要进投影 blob（唯一读路径），也要记帧。

    投影 blob 不被折叠覆盖（帧只重算 content/reasoning/tool_calls/tool_results），
    所以子 agent 卡片以投影为准；这里同时钉住「记了帧」，将来把 subagents 也纳入
    折叠时这条会提醒我们改两处。
    """
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=104)
    await _drain(writer, [
        {"type": "subagent_start", "subagent_id": "s1", "subagent_name": "侦察兵", "task": "查日志"},
        {"type": "subagent_reasoning", "subagent_id": "s1", "text": "先列目录再 grep。"},
        {"type": "subagent_content", "subagent_id": "s1", "text": "找到 3 条"},
        {"type": "subagent_end", "subagent_id": "s1", "summary": "3 条命中"},
    ])
    assert writer.subagents[0]["reasoning"] == "先列目录再 grep。"
    kinds = [f["item_type"] for f in stub.frames]
    assert "subagent_reasoning" in kinds
    checkpoint = stub.message_updates[-1]
    assert checkpoint["subagents"][0]["reasoning"] == "先列目录再 grep。"


@pytest.mark.asyncio
async def test_no_frames_keeps_projection_untouched(stub):
    """老数据/CH 故障：一条帧都没有 → 行原样返回（=迁移前的行为，不是空正文）。"""
    row = _assistant_row(200, content="旧正文", reasoning="旧思考",
                         tool_calls=[{"id": "x", "name": "shell", "status": "done"}])
    out = await conversation_store._fold_event_timeline("c1", [row])
    assert out[0]["content"] == "旧正文"
    assert out[0]["tool_calls"] == [{"id": "x", "name": "shell", "status": "done"}]


@pytest.mark.asyncio
async def test_incomplete_frames_fall_back_to_projection(stub):
    """帧集不完整（部分写丢失）→ id 序列对不上 → 保留投影，绝不显示截断后的正文。"""
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=105)
    await _drain(writer, [
        {"type": "content", "text": "一步"},
        {"type": "tool_call", "id": "call_a", "name": "shell", "args": {}},
        {"type": "tool_result", "id": "call_a", "index": 0, "data": "ok"},
    ])
    # 模拟投影里多了一次调用（帧没写全）。
    proj_calls = list(writer.tool_calls) + [{"id": "call_missing", "name": "shell", "status": "done"}]
    out = await conversation_store._fold_event_timeline("c1", [
        _assistant_row(105, content=writer.content + "二步", tool_calls=proj_calls)
    ])
    assert [c["id"] for c in out[0]["tool_calls"]] == ["call_a", "call_missing"]
    assert out[0]["content"] == writer.content + "二步"


@pytest.mark.asyncio
async def test_truncated_frame_query_falls_back_to_projection(stub, monkeypatch):
    """帧查询被截断（热轮几千帧撞 limit）→ 折叠出的正文比投影短 → 必须退投影。

    实测回归：一轮 4766 帧、页共享 5000 上限从 seq 尾部截掉最新一轮的帧——
    tool_call 恰好都在前面、id 闸门发现不了，只有正文长度暴露截断。不比对长度
    就把「截断后的正文」当真值写回，用户刷新后最后一段文本消失。
    """
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=108)
    await _drain(writer, [
        {"type": "reasoning", "text": "想。"},
        {"type": "tool_call", "id": "c1", "name": "shell", "args": {}},
        {"type": "tool_result", "id": "c1", "index": 0, "data": "ok"},
        {"type": "content", "text": "正文前半。"},
        {"type": "content", "text": "正文后半（在截断点之后，会被 limit 吃掉）。"},
    ])
    proj_content = writer.content
    proj_reasoning = writer.reasoning

    # 模拟截断：query_message_events 只回前 3 帧（tool_result 之后全丢）。
    async def _truncated(conversation_id, *, message_ids=None, limit=5000):
        rows = [f for f in stub.frames if not message_ids or f["message_id"] in set(message_ids or [])]
        return sorted(rows, key=lambda f: (f["message_id"], f["seq"]))[:3]

    monkeypatch.setattr(conversation_store._client, "query_message_events", _truncated)
    out = await conversation_store._fold_event_timeline("c1", [
        _assistant_row(108, content=proj_content, reasoning=proj_reasoning,
                       tool_calls=writer.tool_calls, tool_results=writer.tool_results)
    ])
    # 退投影：正文/思考都完整，不显示截断后的短文。
    assert out[0]["content"] == proj_content
    assert out[0]["reasoning"] == proj_reasoning


@pytest.mark.asyncio
async def test_user_rows_are_never_folded(stub):
    """user 行只有一条 user_input 帧，apply_frame 无该分支 → 折叠会把正文清空。

    这是读侧必须绕开的一种形状：闸门按 assistant 过滤，不是靠帧内容碰巧相等。
    """
    await conversation_store.add_message_event(
        "c1", message_id=300, item_type="user_input", item={"type": "user_input", "text": "你好"},
    )
    row = {"id": 300, "conversation_id": "c1", "role": "user", "content": "你好",
           "tool_calls": None, "tool_results": None, "reasoning": "", "status": "done"}
    out = await conversation_store._fold_event_timeline("c1", [row])
    assert out[0]["content"] == "你好"


@pytest.mark.asyncio
async def test_frame_row_shape_matches_columns_and_version_is_seq(stub):
    """version==seq（重投同 seq 收敛不重复），且 item_json 是可解析的原始事件。"""
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=106)
    await _drain(writer, [{"type": "content", "text": "甲"}, {"type": "content", "text": "乙"}])
    seqs = [f["seq"] for f in stub.frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for f in stub.frames:
        assert f["version"] == f["seq"]
        assert json.loads(f["item_json"])["type"] == "content"
    assert json.loads(stub.frames[1]["item_json"])["text"] == "乙"


@pytest.mark.asyncio
async def test_live_only_events_are_not_logged(stub):
    """实时态（turn_start/retry/run_state/…）驱动活 UI，不是对话内容，不入表。"""
    writer = ConversationTurnWriter(conversation_id="c1", assistant_msg_id=107)
    await _drain(writer, [
        {"type": "turn_start"},
        {"type": "retry", "attempt": 1},
        {"type": "run_state", "state": "running"},
        {"type": "approval_resolved", "confirmation_id": "x"},
        {"type": "content", "text": "真内容"},
    ])
    assert [f["item_type"] for f in stub.frames] == ["content"]


@pytest.mark.asyncio
async def test_llm_history_reads_projection_not_the_fold(stub):
    """永远不变的前提：思考不回灌模型。LLM 路径必须显式走 timeline=False。

    读路径分两族：显示端点传 timeline=True（折叠出交织时间线），LLM/retry/压缩/
    分页游标走默认 False（吃投影，reasoning 不进请求）。这里钉住默认值是 False，
    防将来有人把默认翻成 True 而让 reasoning 顺着折叠泄进上下文。
    """
    import inspect

    for fn in (conversation_store.get_messages, conversation_store.get_messages_page_by_user_turns):
        assert inspect.signature(fn).parameters["timeline"].default is False, fn.__name__
