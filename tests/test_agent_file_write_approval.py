"""越出沙箱的 file_write/file_patch 必须经过一次性审批，且默认 fail closed。

工作区内的正常写入不受影响（不弹卡、不打断），只有撞到 allowed_roots /
denied_patterns 的写入才进审批；memory/ 子树是结构性强拒，不走审批也不放开。
"""
import asyncio

import pytest

from agent.config import AgentConfig
from agent.file_write_approval import FileWriteApprovalBatch
from agent.tools import ToolContext, ToolRegistry
from user_platform.node_client.approvals import (
    ApprovalDenied,
    ApprovalTimeout,
    approval_registry,
)


@pytest.fixture()
def registry(tmp_path) -> ToolRegistry:
    workspace = tmp_path / "workspace"
    temp = tmp_path / "temp"
    workspace.mkdir()
    temp.mkdir()
    config = AgentConfig(
        workspace_root=str(workspace),
        allowed_roots=[str(workspace), str(temp)],
        denied_patterns=[r"\.env$"],
    )
    return ToolRegistry(ToolContext(config), agent_id=7)


def _batch(registry: ToolRegistry, emit, timeout: int = 24 * 60 * 60) -> FileWriteApprovalBatch:
    batch = FileWriteApprovalBatch(
        conversation_id="c1",
        agent_id=7,
        caller="u1",
        emit=emit,
        security=registry.context.security,
        timeout_seconds=timeout,
    )
    registry.set_file_write_approval(batch)
    return batch


def _write_call(path: str, content: str = "x") -> list[dict]:
    return [{"function": {"name": "file_write", "arguments": {"path": path, "content": content}}}]


def _patch_call(path: str, old: str = "a", new: str = "b") -> list[dict]:
    return [{"function": {
        "name": "file_patch",
        "arguments": {"path": path, "old_content": old, "new_content": new},
    }}]


@pytest.fixture()
def outside(tmp_path):
    """一个落在 allowed_roots 之外的落点。"""
    target = tmp_path / "outside" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


# ---------------------------------------------------------------------------
# 工作区内：不打扰
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_write_needs_no_approval(registry, tmp_path):
    """工作区内写入不弹卡——文件写入是 Agent 的日常，不该每次都打断用户。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    _batch(registry, emit)
    result = await registry.execute("file_write", {"path": "notes/a.txt", "content": "hi"})

    assert result["status"] == "ok"
    assert events == []


@pytest.mark.asyncio
async def test_workspace_patch_needs_no_approval(registry, tmp_path):
    await registry.execute("file_write", {"path": "a.txt", "content": "alpha beta"})
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    _batch(registry, emit)
    result = await registry.execute(
        "file_patch", {"path": "a.txt", "old_content": "alpha", "new_content": "ALPHA"}
    )

    assert result["status"] == "ok"
    assert events == []


# ---------------------------------------------------------------------------
# 撞边界：fail closed 与一次性授权
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outside_write_without_coordinator_is_denied(registry, outside):
    """没挂协调器（无人值守）→ denied，且文件绝不落盘。"""
    result = await registry.execute("file_write", {"path": str(outside), "content": "x"})

    assert result["status"] == "denied"
    assert result["code"] == "approval_required"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_outside_write_denied_when_no_event_sink(registry, outside):
    """没有事件出口（无人值守）→ 预扫直接判 deny，写入不落盘。"""
    batch = _batch(registry, emit=None)
    await batch.prepare(_write_call(str(outside), "x"))

    result = await registry.execute("file_write", {"path": str(outside), "content": "x"})

    assert result["status"] == "denied"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_outside_write_denied_pattern_is_denied(registry, tmp_path):
    """命中 denied_patterns（.env）即使在工作区内也要审批。"""
    result = await registry.execute("file_write", {"path": "secrets/.env", "content": "K=1"})

    assert result["status"] == "denied"
    assert not (tmp_path / "workspace" / "secrets" / ".env").exists()


@pytest.mark.asyncio
async def test_outside_write_lands_once_after_allow(registry, outside):
    """放行后真正落盘；同一次授权只覆盖一次执行。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    await batch.prepare(_write_call(str(outside), "approved body"))
    first = await registry.execute("file_write", {"path": str(outside), "content": "approved body"})

    assert events and events[0]["type"] == "confirmation_required"
    assert events[0]["tool_name"] == "file_write"
    assert events[0]["metadata"]["agent_id"] == 7
    assert first["status"] == "ok"
    assert outside.read_text(encoding="utf-8") == "approved body"

    # 授权是一次性的：同一轮里第二次同样的写入不再放行（决策已被消费）。
    second = await registry.execute("file_write", {"path": str(outside), "content": "approved body"})
    assert second["status"] == "denied"


@pytest.mark.asyncio
async def test_outside_write_denied_when_content_changes_after_approval(registry, outside):
    """审批绑定全文：裁决后换正文 → canonical 对不上 → 拒绝（防偷换载荷）。"""

    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    await batch.prepare(_write_call(str(outside), "original"))
    result = await registry.execute("file_write", {"path": str(outside), "content": "tampered"})

    assert result["status"] == "denied"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_outside_write_denied_when_user_rejects(registry, outside):
    """用户显式拒绝 → 抛 ApprovalDenied 中止本轮，而不是把 denied 喂回模型继续跑。"""

    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "deny")

    batch = _batch(registry, emit)

    with pytest.raises(ApprovalDenied):
        await batch.prepare(_write_call(str(outside), "x"))
    assert not outside.exists()


@pytest.mark.asyncio
async def test_outside_write_timeout_aborts_turn(registry, outside):
    """没人裁决 → 抛 ApprovalTimeout（本轮中止），而不是降级成 deny 让循环继续。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)  # 只收事件，不裁决 —— 模拟人不在

    batch = _batch(registry, emit, timeout=1)

    with pytest.raises(ApprovalTimeout):
        await asyncio.wait_for(batch.prepare(_write_call(str(outside), "x")), timeout=5)

    assert events and events[0]["type"] == "confirmation_required"
    action = approval_registry.get(events[0]["confirmation_id"])
    assert action is not None and action.resolved == "timeout"


@pytest.mark.asyncio
async def test_outside_patch_requires_approval_and_lands(registry, outside):
    outside.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    await batch.prepare(_patch_call(str(outside), "beta", "BETA"))
    result = await registry.execute(
        "file_patch", {"path": str(outside), "old_content": "beta", "new_content": "BETA"}
    )

    assert result["status"] == "ok"
    assert outside.read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


@pytest.mark.asyncio
async def test_card_lists_what_writes_where_and_why(registry, outside):
    """卡片必须说清「写到哪、为什么越界」，否则用户无法裁决。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "deny")

    batch = _batch(registry, emit)
    with pytest.raises(ApprovalDenied):
        await batch.prepare(_write_call(str(outside), "body"))

    card = events[0]
    entry = card["metadata"]["writes"][0]
    assert entry["path"] == str(outside)
    assert entry["mode"] == "overwrite"
    assert "outside allowed roots" in entry["reason"]
    assert entry["preview"] == "body"
    # 卡片的正文行是审批哈希的依据，必须带上内容摘要。
    assert entry["content_sha256"] in card["command"]


# ---------------------------------------------------------------------------
# memory/ 是结构性强拒，不属于「可审批的边界」
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_write_stays_hard_rejected_even_with_approval(registry, tmp_path):
    """memory/ 只读区：挂了协调器也不弹卡、不放行（长期记忆只走 start_long_term_update）。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    _batch(registry, emit)
    result = await registry.execute(
        "file_write", {"path": "memory/global_mem.txt", "content": "x"}
    )

    assert result["status"] == "error"
    assert "memory/" in result["msg"]
    # 不是「需要审批」，是压根不该走审批。
    assert result.get("code") != "approval_required"
    assert events == []


@pytest.mark.asyncio
async def test_batch_ignores_workspace_writes_when_preparing(registry, outside):
    """一轮里混合写入：只把越界的那条放进卡片，工作区内的照常直接执行。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    await batch.prepare(
        _write_call("notes/inside.txt", "in") + _write_call(str(outside), "out")
    )

    assert len(events) == 1
    assert len(events[0]["metadata"]["writes"]) == 1
    assert events[0]["metadata"]["writes"][0]["path"] == str(outside)

    inside = await registry.execute("file_write", {"path": "notes/inside.txt", "content": "in"})
    outside_result = await registry.execute("file_write", {"path": str(outside), "content": "out"})
    assert inside["status"] == "ok"
    assert outside_result["status"] == "ok"


@pytest.mark.asyncio
async def test_zero_timeout_never_expires_for_file_write():
    """approval_timeout_seconds=0 表示不过期：注册表不给到期时刻，reap 也不碰它。"""
    action = approval_registry.create(
        conversation_id="c-never",
        node_id="",
        tool_name="file_write",
        command="note.txt (overwrite 1B) sha256:deadbeef — is outside allowed roots",
        requester="u1",
        timeout_seconds=0,
    )
    try:
        assert action.expires_at == 0
        assert action.is_expired() is False
        assert action not in approval_registry.reap_expired()
        assert action.resolved is None
    finally:
        approval_registry.remove(action.confirmation_id)


# ---------------------------------------------------------------------------
# auto 执行模式：审批全部自动放行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_approve_writes_outside_without_card(registry, outside):
    """auto 模式：越界写入预扫直接记 allow，不弹卡直接落盘。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit)
    batch._auto_approve = True

    await batch.prepare(_write_call(str(outside), "auto"))
    result = await registry.execute("file_write", {"path": str(outside), "content": "auto"})

    assert events == []
    assert result["status"] == "ok"
    assert outside.read_text(encoding="utf-8") == "auto"


@pytest.mark.asyncio
async def test_auto_approve_without_event_sink_still_denies(registry, outside):
    """emit=None（无人值守）时 auto_approve 不生效，fail closed 不变。"""
    batch = _batch(registry, emit=None)
    batch._auto_approve = True

    await batch.prepare(_write_call(str(outside), "x"))
    result = await registry.execute("file_write", {"path": str(outside), "content": "x"})

    assert result["status"] == "denied"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_auto_approve_still_hard_rejects_memory_writes(registry, tmp_path):
    """memory/ 是结构性只读区：即使 auto 模式也由工具层硬拒，不走审批。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit)
    batch._auto_approve = True

    await batch.prepare(_write_call("memory/long_term.md", "remember this"))
    result = await registry.execute("file_write", {"path": "memory/long_term.md", "content": "remember this"})

    assert result["status"] == "error"
    assert "memory/" in result["msg"]
    assert events == []
