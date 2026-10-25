"""ConversationState 注册表（会话运行时状态内存态）+ run_state 事件映射。

锁住框架级修复的核心契约（见 agent/conversation_state.py 与 agent/run_state.py）：

1. ``update`` 改内存 → 写穿 → 推送，且字段去重（同值不重复推）。
2. ``load`` 对**已存在实例绝不打穿**：会话中途切 auto，同回合内存态立即变，
   DB 行不覆盖内存（历史根因：每轮 send 用 DB 行覆盖缓存）。
3. ``peek`` 在 ``update`` 后能看到未缓存会话的状态（审批 is_auto 热路径依赖）。
4. ``auto_approve`` 是 mode 的派生属性（切 auto 立即为真）。
5. ``reasoning_effort`` 三态：None（跟随默认）/ ""（关闭）/ 具体值；写穿 None 时
   pop 掉 key。
6. 写穿 chat_settings 从 DB 现读整块合并，不拿局部 blob 覆盖身份段。
7. ``register`` 幂等、``evict`` 回收、``sweep`` 跳过 busy/订阅中。
8. ``_stage_of`` 把 agent 事件映射成 11 态枚举。

不依赖 ClickHouse/WS：conversation_store 与 conversation_hub 用替身。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agent import conversation_state as cs
from agent import run_state as rs


@pytest.fixture(autouse=True)
def _clean_state():
    cs.reset()
    yield
    cs.reset()


def _patch_store(monkeypatch, store_updates: list[dict], conv_rows: dict | None = None):
    """替身 store：记录 update；get_conversation 返回 conv_rows 里的行（默认带 id）。"""
    from agent import conversation_store

    async def _update_conversation(conv_id, **fields):
        store_updates.append({"conv_id": conv_id, **fields})
        return {"id": conv_id}

    async def _get_conversation(conv_id):
        if conv_rows is not None and conv_id in conv_rows:
            return dict(conv_rows[conv_id])
        return {"id": conv_id, "chat_settings": {}}

    monkeypatch.setattr(conversation_store, "update_conversation", _update_conversation)
    monkeypatch.setattr(conversation_store, "get_conversation", _get_conversation)


def _patch_hub(monkeypatch, published: list[dict]):
    from agent import conversation_hub

    async def _publish(conv_id, event):
        published.append({"conv_id": conv_id, **event})

    monkeypatch.setattr(conversation_hub, "publish", _publish)


def test_stage_of_maps_all_statuses():
    cases = {
        "turn_start": cs.STATUS_SENDING,
        "reasoning": cs.STATUS_THINKING,
        "content": cs.STATUS_PROCESSING,
        "done": cs.STATUS_WAITING_INPUT,
        "aborted": cs.STATUS_ABORTED,
    }
    for etype, want in cases.items():
        got = rs._stage_of({"type": etype})
        assert got and got[0] == want, f"{etype} -> {got} want {want}"
    assert rs._stage_of({"type": "tool_call", "name": "web"}) == (cs.STATUS_EXECUTING, "web")
    assert rs._stage_of({"type": "confirmation_required"}) == (cs.STATUS_WAITING_APPROVAL, "")
    assert rs._stage_of({"type": "error", "message": "boom"}) == (cs.STATUS_ERROR, "boom")
    assert rs._stage_of({"type": "question", "data": {"question": "选哪个？"}})[0] == cs.STATUS_WAITING_INPUT
    # 无关事件不改状态
    assert rs._stage_of({"type": "tool_result"}) is None


def test_retry_status_carries_counter_and_countdown():
    status, info = rs._stage_of({"type": "retry", "attempt": 2, "max": 5, "delay_ms": 3000})
    assert status == cs.STATUS_RETRYING
    assert "2/5" in info and "3s" in info


def test_update_writes_through_and_publishes_once(monkeypatch):
    store_updates: list[dict] = []
    published: list[dict] = []
    _patch_store(monkeypatch, store_updates)
    _patch_hub(monkeypatch, published)

    async def scenario():
        conv = {"id": "c1", "model": "m1", "chat_settings": {"mode": "interact"}}
        await cs.load("c1", conv)
        await cs.update("c1", run_status=cs.STATUS_EXECUTING, run_status_info="web")
        # 同值再更新 → 去重，不重复写库/推送
        await cs.update("c1", run_status=cs.STATUS_EXECUTING, run_status_info="web")
        return cs.peek("c1")

    st = asyncio.run(scenario())
    assert st.run_status == cs.STATUS_EXECUTING
    assert len(store_updates) == 1, "同值不应重复写库"
    assert len(published) == 1, "同值不应重复推送"
    assert published[0]["type"] == "conversation_state"
    assert published[0]["state"]["run_status"] == cs.STATUS_EXECUTING


def test_load_does_not_punch_through_existing_instance(monkeypatch):
    """已存在的实例不被 DB 行覆盖——切 auto 后同回合内存态必须保持（历史根因）。"""
    _patch_store(monkeypatch, [], {"c1": {"id": "c1", "chat_settings": {"mode": "interact"}}})
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.load("c1", {"id": "c1", "chat_settings": {"mode": "interact"}})
        await cs.update("c1", mode="auto")
        # 模拟下一轮 send：传入一个「DB 里还是 interact」的旧行
        st = await cs.load("c1", {"id": "c1", "chat_settings": {"mode": "interact"}})
        return st

    st = asyncio.run(scenario())
    assert st.mode == "auto", "DB 旧行不应覆盖内存里的活状态"


def test_update_caches_uncached_conversation(monkeypatch):
    """update 一个没 load 过的会话也要缓存——审批 is_auto 的 peek 热路径依赖它。"""
    _patch_store(monkeypatch, [])
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.update("fresh", run_status=cs.STATUS_THINKING)
        return cs.peek("fresh")

    st = asyncio.run(scenario())
    assert st is not None and st.run_status == cs.STATUS_THINKING


def test_auto_approve_is_derived_from_mode(monkeypatch):
    _patch_store(monkeypatch, [])
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.load("c1", {"id": "c1", "chat_settings": {"mode": "interact"}})
        assert cs.peek("c1").auto_approve is False
        await cs.update("c1", mode="auto")
        assert cs.peek("c1").auto_approve is True
        await cs.update("c1", mode="interact")
        assert cs.peek("c1").auto_approve is False

    asyncio.run(scenario())


def test_reasoning_effort_three_state_and_write_through(monkeypatch):
    """None（跟随默认）/ ""（关闭）/ 具体值 三态；写穿 None 时从 chat_settings pop。"""
    store_updates: list[dict] = []
    rows = {"c1": {"id": "c1", "chat_settings": {"mode": "interact", "node_id": "n1"}}}
    _patch_store(monkeypatch, store_updates, rows)
    _patch_hub(monkeypatch, [])

    async def scenario():
        # 行里没有 reasoning_effort → 实例是 None（跟随默认）
        st = await cs.load("c1")
        assert st.reasoning_effort is None
        # 设为具体值
        await cs.update("c1", reasoning_effort="high")
        # 设为关闭（""）
        await cs.update("c1", reasoning_effort="")
        # 清空回 None
        await cs.update("c1", reasoning_effort=None)
        return cs.peek("c1")

    st = asyncio.run(scenario())
    assert st.reasoning_effort is None
    # 最后一次写穿：chat_settings 里不该有 reasoning_effort，且身份段 node_id 保留
    last = store_updates[-1]
    cs_json = last.get("chat_settings")
    assert cs_json is not None and "reasoning_effort" not in cs_json
    assert cs_json.get("node_id") == "n1", "身份段不能被运行时写穿抹掉"


def test_chat_settings_write_merges_from_db_not_local_blob(monkeypatch):
    """写穿 chat_settings 从 DB 现读合并：DB 里新出现的身份键不被本地旧快照覆盖。"""
    store_updates: list[dict] = []
    # 初始行：只有 mode；之后外部把 node_id 直写进 DB（模拟换标签页重绑）
    rows = {"c1": {"id": "c1", "chat_settings": {"mode": "interact"}}}
    _patch_store(monkeypatch, store_updates, rows)
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.load("c1")
        # 外部直写 DB 新增身份键
        rows["c1"]["chat_settings"] = {"mode": "interact", "node_id": "n9", "cdp_client_id": "x"}
        # 运行时 update 只改 mode
        await cs.update("c1", mode="auto")
        return cs.peek("c1")

    asyncio.run(scenario())
    last = store_updates[-1]
    assert last["chat_settings"]["mode"] == "auto"
    assert last["chat_settings"]["node_id"] == "n9", "从 DB 现读的合并必须保留外部新增的身份键"
    assert last["chat_settings"]["cdp_client_id"] == "x"


def test_register_is_idempotent(monkeypatch):
    _patch_store(monkeypatch, [])
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.update("c1", mode="auto")
        # register 一个「DB 里 mode=interact」的行：不能覆盖已 update 的活状态
        cs.register({"id": "c1", "chat_settings": {"mode": "interact"}})
        return cs.peek("c1")

    st = asyncio.run(scenario())
    assert st.mode == "auto", "register 对已存在实例不应覆盖"


def test_evict_removes_instance(monkeypatch):
    _patch_store(monkeypatch, [])
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.update("c1", mode="auto")
        assert cs.peek("c1") is not None
        cs.evict("c1")
        return cs.peek("c1")

    assert asyncio.run(scenario()) is None


def test_sweep_skips_busy_and_watching(monkeypatch):
    _patch_store(monkeypatch, [])
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.update("idle", mode="auto")
        await cs.update("busy", mode="auto")
        await cs.update("watched", mode="auto")
        # 把三个都推成「很旧」
        for cid in ("idle", "busy", "watched"):
            cs.peek(cid).last_touch = time.monotonic() - 10_000
        cs.set_busy("busy", True)
        # watched 通过 extra_busy 谓词模拟有人订阅
        evicted = cs.sweep(ttl_seconds=1800, extra_busy=lambda cid: cid == "watched")
        return evicted, cs.peek("idle"), cs.peek("busy"), cs.peek("watched")

    evicted, idle, busy, watched = asyncio.run(scenario())
    assert "idle" in evicted
    assert idle is None
    assert busy is not None and watched is not None


def test_sweep_keeps_fresh_instances(monkeypatch):
    _patch_store(monkeypatch, [])
    _patch_hub(monkeypatch, [])

    async def scenario():
        await cs.update("fresh", mode="auto")
        return cs.sweep(ttl_seconds=1800)

    assert asyncio.run(scenario()) == []


def test_restore_reads_new_columns_not_legacy_run_state():
    assert rs.restore({"run_status": "executing", "run_status_info": "web"}) == {
        "run_status": "executing", "run_status_info": "web",
    }
    assert rs.restore({"run_status": ""}) is None
    # 旧 run_state JSON 列已废弃，restore 不读它
    assert rs.restore({"run_state": '{"stage":"retrying"}'}) is None
