import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from user_platform.node_client import tools as node_tools
from user_platform.node_client.approvals import ApprovalDenied, approval_registry


class ToolBag:
    def __init__(self):
        self.tools = {}

    def register(self, name, func, schema):
        self.tools[name] = func


@pytest.mark.asyncio
async def test_node_shell_exec_uses_terminal_executor_with_bound_cwd_and_terminal_id(monkeypatch):
    bag = ToolBag()
    calls = []

    async def executor(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "exit_code": 0, "output": "ok"}

    monkeypatch.setattr(node_tools, "_audit", lambda *args, **kwargs: asyncio.sleep(0))
    batch = node_tools.register_node_shell_exec(
        bag,
        "node-1",
        "user-1",
        "conv-1",
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="powershell",
        executor=executor,
    )

    await batch.prepare([{"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "Get-Location"})}}])
    result = await bag.tools["node_shell_exec"]({"command": "Get-Location", "cwd": "/ignored"})

    assert result["status"] == "ok"
    assert calls == [{"node_id": "node-1", "terminal_id": "term-1", "command": "Get-Location", "cwd": "/work"}]


@pytest.mark.asyncio
async def test_node_shell_exec_refuses_without_terminal_id(monkeypatch):
    bag = ToolBag()
    monkeypatch.setattr(node_tools, "_audit", lambda *args, **kwargs: asyncio.sleep(0))
    batch = node_tools.register_node_shell_exec(bag, "node-1", None, "conv-1")
    await batch.prepare([{"function": {"name": "node_shell_exec", "arguments": {"command": "pwd"}}}])

    result = await bag.tools["node_shell_exec"]({"command": "pwd"})

    assert result["status"] == "error"
    assert result["code"] == "terminal_unavailable"


@pytest.mark.asyncio
async def test_node_shell_exec_batches_dangerous_commands_all_or_nothing(monkeypatch):
    bag = ToolBag()
    emitted = []
    executed = []

    async def emit(event):
        emitted.append(event)

    async def executor(**kwargs):
        executed.append(kwargs["command"])
        return {"status": "ok", "exit_code": 0, "output": kwargs["command"]}

    monkeypatch.setattr(node_tools, "_audit", lambda *args, **kwargs: asyncio.sleep(0))
    batch = node_tools.register_node_shell_exec(
        bag,
        "node-1",
        "user-1",
        "conv-1",
        emit=emit,
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="posix",
        executor=executor,
    )
    tool_calls = [
        {"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "rm a"})}},
        {"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "mkdir b"})}},
    ]
    prepare_task = asyncio.create_task(batch.prepare(tool_calls))
    await asyncio.sleep(0.01)

    assert len(emitted) == 1
    event = emitted[0]
    assert event["type"] == "confirmation_required"
    assert event["commands"] == ["rm a", "mkdir b"]
    assert event["command"] == "rm a\nmkdir b"
    assert event["node_id"] == "node-1"
    assert event["shell_flavor"] == "posix"

    approval_registry.resolve(event["confirmation_id"], "allow")
    await prepare_task

    r1 = await bag.tools["node_shell_exec"]({"command": "rm a"})
    r2 = await bag.tools["node_shell_exec"]({"command": "mkdir b"})
    assert [r1["status"], r2["status"]] == ["ok", "ok"]
    assert executed == ["rm a", "mkdir b"]


@pytest.mark.asyncio
async def test_node_shell_exec_batch_deny_skips_all_commands(monkeypatch):
    bag = ToolBag()
    emitted = []
    executed = []

    async def emit(event):
        emitted.append(event)

    async def executor(**kwargs):
        executed.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(node_tools, "_audit", lambda *args, **kwargs: asyncio.sleep(0))
    batch = node_tools.register_node_shell_exec(
        bag,
        "node-1",
        "user-1",
        "conv-1",
        emit=emit,
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="posix",
        executor=executor,
    )
    tool_calls = [{"function": {"name": "node_shell_exec", "arguments": {"command": "rm a"}}}]
    prepare_task = asyncio.create_task(batch.prepare(tool_calls))
    await asyncio.sleep(0.01)
    approval_registry.resolve(emitted[0]["confirmation_id"], "deny")
    with pytest.raises(ApprovalDenied):
        await prepare_task

    # The batch is aborted before agent_loop dispatches any tool call.
    assert executed == []


@pytest.mark.asyncio
async def test_node_shell_exec_auto_allow_keys_skip_approval_and_flag_audit(monkeypatch):
    """A command in the caller's auto-allow set runs directly (no approval card)
    and is audited with auto_allowed=True. Dangerous structure still needs approval
    even if a key is allow-listed."""
    bag = ToolBag()
    executed = []
    audit_calls = []

    async def executor(**kwargs):
        executed.append(kwargs["command"])
        return {"status": "ok", "exit_code": 0, "output": kwargs["command"]}

    async def audit(*args, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(node_tools, "_audit", audit)
    batch = node_tools.register_node_shell_exec(
        bag,
        "node-1",
        "user-1",
        "conv-1",
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="posix",
        executor=executor,
        auto_allow_keys={"rm"},
    )

    # rm normally needs confirmation; with the key allow-listed it runs directly.
    await batch.prepare([{"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "rm x"})}}])
    result = await bag.tools["node_shell_exec"]({"command": "rm x"})
    assert result["status"] == "ok"
    assert executed == ["rm x"]
    assert audit_calls[0] == {"auto_allowed": True}

    # A dangerous structure (compound/redirect) still requires approval even
    # though "rm" (and "ls") are allow-listed — the gate is independent.
    emitted = []

    async def emit(event):
        emitted.append(event)

    bag2 = ToolBag()
    monkeypatch.setattr(node_tools, "_audit", audit)
    batch2 = node_tools.register_node_shell_exec(
        bag2,
        "node-1",
        "user-1",
        "conv-1",
        emit=emit,
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="posix",
        executor=executor,
        auto_allow_keys={"rm", "ls"},
    )
    prepare_task = asyncio.create_task(
        batch2.prepare(
            [{"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "ls; rm x"})}}]
        )
    )
    await asyncio.sleep(0.01)
    # ls; rm x cannot be split into provably-safe segments, so an approval is raised.
    assert len(emitted) == 1
    assert emitted[0]["type"] == "confirmation_required"
    approval_registry.resolve(emitted[0]["confirmation_id"], "deny")
    with pytest.raises(ApprovalDenied):
        await prepare_task


@pytest.mark.asyncio
async def test_node_shell_auto_approve_runs_dangerous_chain_without_card(monkeypatch):
    """auto 模式：多段链/危险结构一律直接执行，不弹审批卡。

    这是「全部审批通过」的核心断言——连 ``ls; rm x`` 这种在普通模式下必弹卡的
    组合都不弹（审计仍记 auto_allowed=True）。
    """
    bag = ToolBag()
    audited: list[dict] = []
    emitted: list[dict] = []
    calls: list[dict] = []

    async def audit(node_id, caller, conversation_id, command, result, shell_flavor, *, auto_allowed):
        audited.append({"command": command, "auto_allowed": auto_allowed})

    async def executor(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "exit_code": 0, "output": "done"}

    async def emit(event):
        emitted.append(event)

    monkeypatch.setattr(node_tools, "_audit", audit)
    batch = node_tools.register_node_shell_exec(
        bag,
        "node-1",
        "user-1",
        "conv-1",
        emit=emit,
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="posix",
        executor=executor,
        auto_approve=True,
    )

    await batch.prepare(
        [{"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "ls; rm x"})}}]
    )
    result = await bag.tools["node_shell_exec"]({"command": "ls; rm x"})

    assert emitted == []           # 没有 confirmation_required
    assert result["status"] == "ok"
    assert calls and calls[0]["command"] == "ls; rm x"
    assert audited and audited[-1]["auto_allowed"] is True


@pytest.mark.asyncio
async def test_node_shell_auto_approve_without_event_sink_still_blocks(monkeypatch):
    """emit=None（无人值守）时 auto_approve 不生效：照常挂起等裁决，超时 fail closed。

    没有人（也没有事件流）在场时，auto_approve 不能让命令静默执行——这是
    「无人值守必须有人裁决」底线在本模式下的体现。
    """
    from user_platform.node_client.approvals import ApprovalTimeout

    bag = ToolBag()
    executed: list[dict] = []
    monkeypatch.setattr(node_tools, "_audit", lambda *a, **k: asyncio.sleep(0))

    async def executor(**kwargs):
        executed.append(kwargs)
        return {"status": "ok"}

    batch = node_tools.register_node_shell_exec(
        bag,
        "node-1",
        "user-1",
        "conv-1",
        emit=None,
        terminal_id="term-1",
        cwd="/work",
        shell_flavor="posix",
        executor=executor,
        auto_approve=True,
        timeout_seconds=1,
    )

    with pytest.raises(ApprovalTimeout):
        await asyncio.wait_for(
            batch.prepare(
                [{"function": {"name": "node_shell_exec", "arguments": json.dumps({"command": "rm x"})}}]
            ),
            timeout=5,
        )
    assert executed == []
