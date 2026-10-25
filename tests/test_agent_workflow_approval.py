"""工作流定义写操作（create/update/delete）的一次性审批 + 读/运行态自由读。

写定义会授权一段持久自动化（能执行 code_run / 写文件 / 驱动浏览器），故走
approval card；没挂协调器（无人值守）时 fail closed。list/get/run/runs/
run_status/cancel 是读或运行态，不进卡。
"""
import asyncio

import pytest

from agent.config import AgentConfig
from agent.tools import ToolContext, ToolRegistry
from agent.workflow_approval import WorkflowApprovalBatch, mutation_digest
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
    return ToolRegistry(ToolContext(config), agent_id=7, owner_user_id="u1")


def _batch(registry: ToolRegistry, emit, timeout: int = 24 * 60 * 60) -> WorkflowApprovalBatch:
    batch = WorkflowApprovalBatch(
        conversation_id="c1",
        agent_id=7,
        caller="u1",
        emit=emit,
        timeout_seconds=timeout,
    )
    registry.set_workflow_approval(batch)
    return batch


def _def(nodes: list | None = None) -> dict:
    return {"nodes": nodes if nodes is not None else [
        {"id": "fetch", "name": "抓取", "kind": "web", "operation": "read_page", "url": "http://x"},
    ]}


def _cap_call(name: str, args: dict) -> list[dict]:
    return [{"function": {"name": "capability_call", "arguments": {"name": name, "args": args}}}]


def _create_args(**over) -> dict:
    args = {"name": "daily_sweep", "label": "每日巡检", "definition": _def()}
    args.update(over)
    return args


def _update_args(**over) -> dict:
    args = {"def_id": 12, "definition": _def()}
    args.update(over)
    return args


# ---------------------------------------------------------------------------
# 无协调器：fail closed，写不落库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutations_without_coordinator_are_denied(registry, monkeypatch):
    """没挂协调器（无人值守）→ create/update/delete 全 denied，store 零调用。"""
    calls = {"create": 0, "update": 0, "delete": 0}

    def _bump(key):
        async def _run(*a, **k):
            calls[key] += 1
            return {"id": 1}
        return _run

    monkeypatch.setattr("agent.workflow_store.create_def", _bump("create"))
    monkeypatch.setattr("agent.workflow_store.update_def", _bump("update"))
    monkeypatch.setattr("agent.workflow_store.delete_def", _bump("delete"))

    async def fake_get_def(def_id, version=None):
        return {"id": def_id, "user_id": "u1", "name": "daily_sweep", "version": 3}

    monkeypatch.setattr("agent.workflow_store.get_def", fake_get_def)

    for name, args in [
        ("workflow.create", _create_args()),
        ("workflow.update", _update_args()),
        ("workflow.delete", {"def_id": 12}),
    ]:
        result = await registry.execute("capability_call", {"name": name, "args": args})
        assert result["status"] == "denied"
        assert result["code"] == "approval_required"
    assert calls == {"create": 0, "update": 0, "delete": 0}


# ---------------------------------------------------------------------------
# 预扫 → 卡片 → 放行 → 落库；一次性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_lands_after_allow(registry, monkeypatch):
    """放行后真正落库；卡片带 name/步数/摘要；授权一次性消费。"""
    events: list[dict] = []
    created = {}

    async def fake_create(name, description, definition, *, user_id, label="", retry_on_reboot=True):
        created.update(name=name, description=description, definition=definition,
                       user_id=user_id, label=label)
        return {"id": 101, "version": 1, "name": name, "label": label}

    monkeypatch.setattr("agent.workflow_store.create_def", fake_create)

    async def emit(event: dict) -> None:
        events.append(event)
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    args = _create_args(description="夜间跑")
    await batch.prepare(_cap_call("workflow.create", args))
    result = await registry.execute("capability_call", {"name": "workflow.create", "args": args})

    assert events and events[0]["type"] == "confirmation_required"
    assert events[0]["tool_name"] == "workflow"
    assert events[0]["shell_flavor"] == "workflow-mutation"
    assert events[0]["metadata"]["agent_id"] == 7
    entry = events[0]["metadata"]["workflow_ops"][0]
    assert entry["action"] == "create"
    assert entry["name"] == "daily_sweep"
    assert entry["label"] == "每日巡检"
    assert "1 步" in entry["node_summary"]
    assert entry["digest"] in events[0]["command"]
    assert result["status"] == "ok"
    assert created == {
        "name": "daily_sweep", "description": "夜间跑", "user_id": "u1",
        "label": "每日巡检", "definition": _def(),
    }

    # 授权一次性：第二次同样的 create 不再放行。
    again = await registry.execute("capability_call", {"name": "workflow.create", "args": args})
    assert again["status"] == "denied"


@pytest.mark.asyncio
async def test_create_denied_when_definition_changes_after_approval(registry, monkeypatch):
    """审批绑定定义摘要：裁决后偷换图 → 拒，不落库。"""

    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    await batch.prepare(_cap_call("workflow.create", _create_args()))
    tampered = _create_args(definition={"nodes": [{"id": "evil", "name": "偷换", "kind": "code_run", "code": "print(1)"}]})
    result = await registry.execute("capability_call", {"name": "workflow.create", "args": tampered})

    assert result["status"] == "denied"


@pytest.mark.asyncio
async def test_mutations_denied_when_user_rejects(registry, monkeypatch):
    """用户显式拒绝 → 抛 ApprovalDenied 中止本轮，而不是喂回 denied 让模型继续跑。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "deny")

    batch = _batch(registry, emit)
    with pytest.raises(ApprovalDenied):
        await batch.prepare(_cap_call("workflow.delete", {"def_id": 12}))


@pytest.mark.asyncio
async def test_mutations_timeout_aborts_turn(registry):
    """没人裁决 → 抛 ApprovalTimeout（本轮中止），而非降级 deny 让循环继续。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit, timeout=1)
    with pytest.raises(ApprovalTimeout):
        await asyncio.wait_for(
            batch.prepare(_cap_call("workflow.update", _update_args())), timeout=5
        )
    assert events and events[0]["type"] == "confirmation_required"
    action = approval_registry.get(events[0]["confirmation_id"])
    assert action is not None and action.resolved == "timeout"


@pytest.mark.asyncio
async def test_no_event_sink_fails_closed_even_in_auto(registry, monkeypatch):
    """emit=None（无人值守）时 auto_approve 不生效，fail closed 不变。"""
    batch = _batch(registry, emit=None)
    batch._auto_approve = True

    await batch.prepare(_cap_call("workflow.create", _create_args()))
    result = await registry.execute("capability_call", {"name": "workflow.create", "args": _create_args()})

    assert result["status"] == "denied"
    assert result["code"] == "approval_required"


@pytest.mark.asyncio
async def test_auto_approve_lands_without_card(registry, monkeypatch):
    """auto 模式：直接落库不弹卡。"""
    events: list[dict] = []
    created = {}

    async def fake_create(name, description, definition, *, user_id, label="", retry_on_reboot=True):
        created["name"] = name
        return {"id": 5, "version": 1, "name": name, "label": label}

    monkeypatch.setattr("agent.workflow_store.create_def", fake_create)

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit)
    batch._auto_approve = True

    await batch.prepare(_cap_call("workflow.create", _create_args()))
    result = await registry.execute("capability_call", {"name": "workflow.create", "args": _create_args()})

    assert events == []
    assert result["status"] == "ok"
    assert created["name"] == "daily_sweep"


# ---------------------------------------------------------------------------
# 预扫只捕 workflow 写动作
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_scan_ignores_reads_and_non_workflow(registry):
    """读/运行态与非 workflow 的 capability_call 不进卡。"""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit)
    await batch.prepare(
        _cap_call("workflow.run", {"def_id": 12})
        + _cap_call("workflow.runs", {"def_id": 12})
        + _cap_call("workflow.run_status", {"run_id": 99})
        + _cap_call("workflow.cancel", {"run_id": 99})
        + _cap_call("workflow.list", {})
        + _cap_call("workflow.get", {"def_id": 12})
        + [{"function": {"name": "capability_call", "arguments": {"name": "mail.search_messages", "args": {}}}}]
    )

    assert events == []
    assert batch._decisions == []


# ---------------------------------------------------------------------------
# update / delete：可见性、调度器重载/摘除
# ---------------------------------------------------------------------------


@pytest.fixture()
def _stub_scheduler(monkeypatch):
    sched = {"reloaded": [], "removed": []}

    class _S:
        def start(self):
            pass

        async def reload_def(self, def_id):
            sched["reloaded"].append(def_id)

        async def remove(self, def_id):
            sched["removed"].append(def_id)

    monkeypatch.setattr(
        "agent.workflow_scheduler.get_workflow_scheduler", lambda: _S(), raising=False
    )
    return sched


@pytest.mark.asyncio
async def test_update_saves_new_version_and_reloads_scheduler(registry, monkeypatch, _stub_scheduler):
    updated = {}

    async def fake_get_def(def_id, version=None):
        return {"id": def_id, "user_id": "u1", "name": "daily_sweep", "version": 3}

    async def fake_update(def_id, definition, description=None, label=None, retry_on_reboot=None):
        updated["def_id"] = def_id
        updated["definition"] = definition
        return {"id": def_id + 1, "version": 4, "name": "daily_sweep", "label": label or ""}

    monkeypatch.setattr("agent.workflow_store.get_def", fake_get_def)
    monkeypatch.setattr("agent.workflow_store.update_def", fake_update)

    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    args = _update_args(label="每日巡检V2")
    await batch.prepare(_cap_call("workflow.update", args))
    result = await registry.execute("capability_call", {"name": "workflow.update", "args": args})

    assert result["status"] == "ok"
    assert result["version"] == 4
    assert updated["def_id"] == 12
    assert _stub_scheduler["reloaded"] == [12]
    # 卡片补全了现有行名称。
    # （重新跑一次 prepare 以观察 entry；此处断言 store 更新即可，卡片断言见上一测试）


@pytest.mark.asyncio
async def test_update_of_foreign_workflow_is_error(registry, monkeypatch):
    """别人的 def → error（不弹卡也不落库）。"""

    async def fake_get_def(def_id, version=None):
        return {"id": def_id, "user_id": "someone_else", "name": "other", "version": 1}

    monkeypatch.setattr("agent.workflow_store.get_def", fake_get_def)

    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit)
    result = await registry.execute(
        "capability_call", {"name": "workflow.update", "args": _update_args()}
    )

    assert result["status"] == "error"
    assert "not found" in result["msg"]
    # 可见性在落库前拦截，不该弹卡。
    assert events == []


@pytest.mark.asyncio
async def test_delete_removes_scheduler_jobs(registry, monkeypatch, _stub_scheduler):
    deleted = {}

    async def fake_get_def(def_id, version=None):
        return {"id": def_id, "user_id": "u1", "name": "daily_sweep", "version": 3}

    async def fake_delete(def_id):
        deleted["def_id"] = def_id
        return True

    monkeypatch.setattr("agent.workflow_store.get_def", fake_get_def)
    monkeypatch.setattr("agent.workflow_store.delete_def", fake_delete)

    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    args = {"def_id": 12}
    await batch.prepare(_cap_call("workflow.delete", args))
    result = await registry.execute("capability_call", {"name": "workflow.delete", "args": args})

    assert result["status"] == "ok"
    assert result["deleted"] is True
    assert deleted["def_id"] == 12
    assert _stub_scheduler["removed"] == [12]


# ---------------------------------------------------------------------------
# 静态校验：空图 / 缺 name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_empty_graph_and_missing_name(registry, monkeypatch):
    async def emit(event: dict) -> None:
        if event.get("type") == "confirmation_required":
            approval_registry.resolve(event["confirmation_id"], "allow")

    batch = _batch(registry, emit)
    empty = _create_args(definition={"nodes": []})
    await batch.prepare(_cap_call("workflow.create", empty))
    result = await registry.execute("capability_call", {"name": "workflow.create", "args": empty})
    assert result["status"] == "error"
    assert "非空" in result["msg"]

    no_name = _create_args(name="  ")
    result2 = await registry.execute("capability_call", {"name": "workflow.create", "args": no_name})
    assert result2["status"] == "error"


# ---------------------------------------------------------------------------
# 运行态：runs / run_status / cancel 不进卡
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_state_actions_run_without_card(registry, monkeypatch):
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event)

    batch = _batch(registry, emit)

    async def fake_get_def(def_id, version=None):
        return {"id": def_id, "user_id": "u1", "name": "daily_sweep", "version": 3}

    async def fake_list_runs(def_id, limit=50):
        return [{"id": 7, "def_id": def_id, "status": "ok"}]

    async def fake_get_run(run_id, family_of=None):
        return {"id": run_id, "def_id": 12, "status": "running"}

    monkeypatch.setattr("agent.workflow_store.get_def", fake_get_def)
    monkeypatch.setattr("agent.workflow_store.list_runs", fake_list_runs)
    monkeypatch.setattr("agent.workflow_store.get_run", fake_get_run)

    runs = await registry.execute("capability_call", {"name": "workflow.runs", "args": {"def_id": 12}})
    status = await registry.execute("capability_call", {"name": "workflow.run_status", "args": {"run_id": 7}})
    cancel = await registry.execute("capability_call", {"name": "workflow.cancel", "args": {"run_id": 7}})

    assert runs["status"] == "ok" and runs["runs"][0]["id"] == 7
    assert status["status"] == "ok" and status["run"]["status"] == "running"
    assert cancel["status"] == "ok"
    assert events == []


def test_mutation_digest_changes_with_payload():
    base = {"name": "a", "definition": _def()}
    assert mutation_digest("create", base) == mutation_digest("create", dict(base))
    assert mutation_digest("create", base) != mutation_digest("create", {**base, "name": "b"})
    assert mutation_digest("create", base) != mutation_digest(
        "create", {**base, "definition": _def([{"id": "x", "name": "n", "kind": "cache", "action": "get", "key": "wf:k"}])}
    )
