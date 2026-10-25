"""审批去重 + 切 auto 立即放行 + mode 会话状态。

锁住用户反馈的三个行为：
1. 同一个待裁决命令反复请求 → 复用一张卡，不再堆一万张（问题 4）。
2. 切到 auto → 在途 pending 审批一次性放行，不等下一轮（问题 5）。
3. mode 是会话状态：send 时落进 chat_settings、retry 沿用（问题 5）。

不依赖 Postgres/ClickHouse：审批注册表是纯内存。
"""

from __future__ import annotations

import asyncio

import pytest

from user_platform.node_client.approvals import ApprovalRegistry, hash_command


def test_same_command_reuses_one_pending_action():
    """同一会话里完全相同的未裁决命令，create 复用同一个 action。

    模型每轮都会重新发起同一批 tool_call；不复用就会每次 uuid4 新卡 → 面板按
    confirmationId 去重永远判新 → 同一个审批点堆出一万张卡。
    """
    async def scenario():
        reg = ApprovalRegistry()
        a1 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        a2 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        assert a1 is a2, "相同命令必须复用同一 action"
        assert a1.confirmation_id == a2.confirmation_id
        # 不同命令 → 不同 action（不能误合并）
        a3 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(2)", requester="u1",
        )
        assert a3 is not a1
        # 不同会话 → 不同 action
        a4 = reg.create(
            conversation_id="c2", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        assert a4 is not a1
    asyncio.run(scenario())


def test_resolved_command_creates_fresh_action():
    """已裁决的命令再次请求是新的一次授权，必须新建（不能复用已裁决的）。"""
    async def scenario():
        reg = ApprovalRegistry()
        a1 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        reg.resolve(a1.confirmation_id, "allow")
        a2 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        assert a2 is not a1, "已裁决后同命令应新建一张待办"
        assert a2.resolved is None
    asyncio.run(scenario())


def test_allow_pending_wakes_all_in_flight():
    """allow_pending 把某会话所有未裁决待办一次性 allow，唤醒各自的 future。"""
    async def scenario():
        reg = ApprovalRegistry()
        a1 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        a2 = reg.create(
            conversation_id="c1", node_id="", tool_name="file_write",
            command="write a.txt", requester="u1",
        )
        # 别的会话的 pending 不能被动
        a3 = reg.create(
            conversation_id="other", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        released = reg.allow_pending("c1")
        assert len(released) == 2, "allow_pending 返回被放行的 action 列表（供广播）"
        assert {a.confirmation_id for a in released} == {a1.confirmation_id, a2.confirmation_id}
        # 两个 future 都已被唤醒为 allow
        assert await asyncio.wait_for(a1.future, timeout=1) == "allow"
        assert await asyncio.wait_for(a2.future, timeout=1) == "allow"
        # 别的会话的没被动
        assert a3.resolved is None
        assert not a3.future.done()
    asyncio.run(scenario())


def test_allow_pending_idempotent_after_resolve():
    """已裁决的不再被 allow_pending 重复放行（返回空列表）。"""
    async def scenario():
        reg = ApprovalRegistry()
        a1 = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        reg.resolve(a1.confirmation_id, "deny")
        assert reg.allow_pending("c1") == []
    asyncio.run(scenario())


def test_list_pending_excludes_resolved_and_expired():
    """#4/#5：刷新后审批卡不再「复活」——list_pending 只回真待办。

    已裁决（resolved）与已过期的 action 不该再以 pending 出现在会话载入的
    pending_approvals 里，否则用户刚批准、一刷新卡片又变回待审批（三端同根）。
    """
    async def scenario():
        reg = ApprovalRegistry()
        live = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1",
        )
        done = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(2)", requester="u1",
        )
        reg.resolve(done.confirmation_id, "allow")
        # 已过期但尚未被 reap 的：timeout_seconds 负数 → 创建即过期
        old = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(3)", requester="u1", timeout_seconds=-1,
        )
        pending_ids = [a.confirmation_id for a in reg.list_pending("c1")]
        assert live.confirmation_id in pending_ids, "未裁决的必须还在"
        assert done.confirmation_id not in pending_ids, "已裁决的必须被过滤"
        assert old.confirmation_id not in pending_ids, "已过期的必须被过滤"
    asyncio.run(scenario())


def test_resolve_refuses_expired():
    """#5：过期审批不能被放行——晚到的一次点击返回 None（端点回 409）。"""
    async def scenario():
        reg = ApprovalRegistry()
        expired = reg.create(
            conversation_id="c1", node_id="", tool_name="code_run",
            command="print(1)", requester="u1", timeout_seconds=-1,
        )
        assert reg.resolve(expired.confirmation_id, "allow") is None, \
            "已过期审批 resolve 必须被拒"
        assert expired.resolved is None, "不写入 resolved，留给 reap 判 timeout"
    asyncio.run(scenario())


def test_retry_reads_mode_from_conversation_settings():
    """retry 端点不再硬编码 interact：mode 从会话注册表读活状态。

    这是「切了 auto 还让我审批」的核心——重试被打回 interact。mode 是会话状态，
    重试必须沿用会话已存的 mode。写入口收敛后，读写都走 conversation_state。
    """
    import inspect
    from agent import cdp_chat_service

    src = inspect.getsource(cdp_chat_service.client_retry_message)
    # 不能再出现硬编码 mode="interact"
    assert 'mode="interact"' not in src
    # 必须从会话注册表读 mode（load + _cstate.mode），不再直读 chat_settings 快照
    assert "conversation_state.load" in src
    assert "_cstate.mode" in src


def test_send_persists_mode_into_chat_settings():
    """send 时把 mode 落进会话状态（会话状态，不是单次请求参数）。

    写入口收敛后：mode 经 conversation_state.update 写（内存→写穿 DB→广播），
    不再直改 chat_settings blob。
    """
    import inspect
    from agent import cdp_chat_service

    src = inspect.getsource(cdp_chat_service.client_send_message)
    assert 'conversation_state.update' in src and "mode=request.mode" in src


def test_cdp_settings_endpoint_exists_and_releases():
    """PATCH /conversations/{id}/settings 存在，且切 auto 时放行在途审批。"""
    import inspect
    from agent import cdp_chat_service

    src = inspect.getsource(cdp_chat_service.client_update_conversation_settings)
    assert "allow_pending" in src
    assert "reasoning_effort" in src and "mode" in src
