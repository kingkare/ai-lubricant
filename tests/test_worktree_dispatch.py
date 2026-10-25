"""Worktree dispatch: a task on a worktree-mode environment gets shared-checkout
tags and an environment-scoped clone credential.

These are gateway-side units (no node involved). They lock down the contract the
node relies on: ``tags[workspace_mode]`` + ``tags[env_id]`` select the worktree
layout, and ``repo.repo_url`` carries the ENVIRONMENT's token rather than the
task's — that is what makes the shared checkout durable past any single task.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from user_platform import task_service as task_service_module
from user_platform.task_service import TaskService

ORIGIN = "http://127.0.0.1:8003"
CONTROL_TOKEN = "control-secret"
ENV_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "user_platform": [
                "user_platform.models_git",
                "user_platform.models_project",
                "user_platform.models_environment",
                "user_platform.models_task",
            ]
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def proxy_configured(monkeypatch):
    monkeypatch.setattr(task_service_module, "_git_proxy_origin", lambda: ORIGIN)
    monkeypatch.setattr(task_service_module, "_git_proxy_control_token", lambda: CONTROL_TOKEN)


@pytest.fixture(autouse=True)
def no_submodule_probe(monkeypatch):
    from user_platform import environment_service as env_service

    async def _empty():
        return []

    monkeypatch.setattr(env_service, "_project_submodule_paths", lambda _p: _empty())


def _task(task_id=uuid.uuid4()):
    return SimpleNamespace(
        id=task_id, user_id=uuid.uuid4(), provider="claude",
        config_snapshot={"env_mode": "shared", "env_id": str(ENV_ID)},
    )


@pytest.mark.asyncio
async def test_worktree_task_gets_env_tags_and_env_credential(tortoise_db):
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment
    from user_platform.models_git import GitIdentity
    from user_platform.models_project import Project

    identity = await GitIdentity.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), platform="github", access_token="ghp_tok"
    )
    project = await Project.create(
        id=PROJECT_ID, user_id=uuid.UUID(user_id), name="proj", platform="github",
        repo_url="https://github.com/o/r.git", git_identity_id=identity.id,
    )
    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=project.id, name="env",
        workspace_mode="worktree",
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    # The task binding lets _effective_git_identity_id find the identity.
    # That call is `await filter(...).order_by(...).first()` — so `first` is a
    # callable returning an awaitable, not the value itself.
    binding = SimpleNamespace(git_identity_id=identity.id, project_id=project.id)

    class _Query:
        def __init__(self, row):
            self._row = row

        async def first(self):
            return self._row

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            task_service_module.ProjectTask, "filter",
            lambda **kw: SimpleNamespace(order_by=lambda _f: _Query(binding)),
        )
        service = TaskService()
        req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}

        await service._apply_environment_workspace(req, task)

        tags = req["_session_tags"]
        assert tags["workspace_mode"] == "worktree"
        assert tags["env_id"] == str(env.id)
        # The URL carries the ENVIRONMENT's token, not the task's — that is what
        # lets the shared checkout outlive this task.
        assert f"{env.id}.main.rw." in req["repo"]["repo_url"]
        # Any direct credential the caller supplied was stripped: the node must
        # never end up with the identity's real token.
        assert "token" not in req["repo"]
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_shared_mode_task_keeps_task_scoped_clone(tortoise_db):
    """An environment in the default mode changes nothing."""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=PROJECT_ID, name="env", workspace_mode="shared",
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    monkeypatch = pytest.MonkeyPatch()
    try:
        service = TaskService()
        req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
        await service._apply_environment_workspace(req, task)
        assert "_session_tags" not in req
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_disabled_environment_refuses_worktree(tortoise_db):
    """A disabled env must NOT silently fall back to a per-task clone."""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=PROJECT_ID, name="env", workspace_mode="worktree", enabled=False,
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    monkeypatch = pytest.MonkeyPatch()
    try:
        service = TaskService()
        req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
        with pytest.raises(ValueError, match="environment_disabled"):
            await service._apply_environment_workspace(req, task)
    finally:
        monkeypatch.undo()


# ── external mode: adopt a directory that already exists on the node ────────


def _ext_env_kwargs(user_uuid, **overrides):
    """An external-path environment: no project, an adopted directory."""
    base = dict(
        id=uuid.uuid4(),
        user_id=user_uuid,
        node_id="node-1",
        project_id=None,
        name="env-ext",
        workspace_mode="external",
        external_path="/home/op/my-checkout",
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_external_env_created_task_cuts_worktree_from_imported_checkout(tortoise_db):
    """An ACTIVE task on an imported env cuts a worktree from the operator's tree.

    Whether to cut is decided by the SESSION SOURCE, not the env mode: a task the
    user creates gets an isolated worktree, while an adopted (synced) session
    keeps its original working dir. For an imported env the shared checkout IS the
    operator's directory, so the node cuts the worktree from it — no clone, and no
    credential is minted (the tree already has its own git state).
    """
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(**_ext_env_kwargs(uuid.UUID(user_id)))
    task = _task()  # no source_editor_session_id → created, not adopted
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    assert tags["workspace_mode"] == "worktree"
    assert tags["env_id"] == str(env.id)
    # The imported checkout is the shared checkout the worktree is cut from.
    assert tags["external_workdir"] == "/home/op/my-checkout"
    # No credential: the operator's tree carries its own git state.
    assert req["repo"]["repo_url"] == "https://github.com/o/r.git"


@pytest.mark.asyncio
async def test_external_env_adopted_session_keeps_original_dir(tortoise_db):
    """An ADOPTED session on an imported env must NOT cut a worktree.

    "同步为任务" reuses the node's original session, so its working directory must
    stay exactly where it was. The adopted marker is ``source_editor_session_id``.
    """
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(**_ext_env_kwargs(uuid.UUID(user_id)))
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.source_editor_session_id = "editor-sess-1"  # adopted
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    assert tags["workspace_mode"] == "external"
    assert tags["env_id"] == str(env.id)
    assert tags["external_workdir"] == "/home/op/my-checkout"


@pytest.mark.asyncio
async def test_external_env_windows_virtual_path_is_normalized(tortoise_db):
    """A Windows external_path is dispatched in native form, not the virtual one.

    The env page and the file browser store ``external_path`` in the virtual form
    (``/D:/code/x``). The node decides whether to adopt with ``filepath.IsAbs``,
    which on Windows is FALSE for that form — so the tag was silently ignored and
    the session fell back to a per-task clone (the "session pulls the repo again"
    bug). Dispatch must strip the virtual slash.
    """
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        **_ext_env_kwargs(uuid.UUID(user_id), external_path="/D:/code/ai-lubricant")
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    # A created task on an imported env → worktree cut from the native path.
    assert tags["workspace_mode"] == "worktree"
    # Native form: the node accepts it, so the session adopts the directory
    # instead of cloning.
    assert tags["external_workdir"] == "D:/code/ai-lubricant"
    assert not tags["external_workdir"].startswith("/")


@pytest.mark.asyncio
async def test_external_env_disabled_refuses(tortoise_db):
    """A disabled external env must refuse — not silently adopt then run."""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        **_ext_env_kwargs(uuid.UUID(user_id), enabled=False)
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    with pytest.raises(ValueError, match="environment_disabled"):
        await service._apply_environment_workspace(req, task)


@pytest.mark.asyncio
async def test_external_path_wins_over_worktree_mode(tortoise_db):
    """``external_path`` is the code directory; the mode label is not re-read here.

    A row that somehow carries both ``external_path`` and ``workspace_mode=
    "worktree"`` is refused at creation. If one exists (hand-edited / legacy),
    dispatch must not blow up: the path is the fixed fact, so the task cuts its
    worktree from that directory and clones nothing.
    """
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        **_ext_env_kwargs(uuid.UUID(user_id), workspace_mode="worktree")
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    assert tags["workspace_mode"] == "worktree"
    assert tags["external_workdir"] == "/home/op/my-checkout"
    # The operator's tree carries its own git state — no credential minted.
    assert req["repo"]["repo_url"] == "https://github.com/o/r.git"


@pytest.mark.asyncio
async def test_external_env_create_worktree_false_runs_in_place(tortoise_db):
    """「创建 worktree」关掉：导入环境不切，直接在操作者的目录里跑。"""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(**_ext_env_kwargs(uuid.UUID(user_id)))
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id), "create_worktree": False}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    assert tags["workspace_mode"] == "external"
    assert tags["env_id"] == str(env.id)
    assert tags["external_workdir"] == "/home/op/my-checkout"


@pytest.mark.asyncio
async def test_worktree_env_create_worktree_false_falls_back_to_task_clone(tortoise_db):
    """「创建 worktree」关掉且环境是托管 clone：不碰共享检出，任务自己 clone
    （历史行为）——不设任何 workspace 标签。"""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=PROJECT_ID, name="env", workspace_mode="worktree",
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id), "create_worktree": False}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)
    assert "_session_tags" not in req


@pytest.mark.asyncio
async def test_synced_session_ignores_create_worktree_true(tortoise_db):
    """同步会话不受开关影响：勾了也照样不切（原会话目录必须保持不变）。"""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(**_ext_env_kwargs(uuid.UUID(user_id)))
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.source_editor_session_id = "editor-sess-1"  # adopted
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id), "create_worktree": True}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    assert tags["workspace_mode"] == "external"


@pytest.mark.asyncio
async def test_provisioning_env_refuses_dispatch(tortoise_db):
    """A provisioning (clone in flight) env refuses dispatch — the clone belongs
    to the env side, and the node only cuts worktrees (no on-demand clone)."""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=PROJECT_ID, name="env",
        workspace_mode="worktree", provision_state="provisioning",
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    with pytest.raises(ValueError, match="environment_provisioning"):
        await service._apply_environment_workspace(req, task)


@pytest.mark.asyncio
async def test_failed_provision_env_refuses_dispatch(tortoise_db):
    """A failed env refuses dispatch with its own code — the environment page is
    where the failure is surfaced and retried, not the task path."""
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment

    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=PROJECT_ID, name="env",
        workspace_mode="worktree", provision_state="failed",
        provision_error="clone timeout",
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}
    with pytest.raises(ValueError, match="environment_provision_failed"):
        await service._apply_environment_workspace(req, task)


@pytest.mark.asyncio
async def test_provision_retry_resets_state_and_runs(tortoise_db, monkeypatch):
    """The env-page retry rebuilds the clone plan from the row and reruns it."""
    from user_platform import environment_service as env_service
    from user_platform.models_environment import TaskEnvironment
    from user_platform.models_git import GitIdentity
    from user_platform.models_project import Project

    user_id = str(uuid.uuid4())
    identity = await GitIdentity.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), platform="github", access_token="ghp_tok"
    )
    project = await Project.create(
        id=PROJECT_ID, user_id=uuid.UUID(user_id), name="proj", platform="github",
        repo_url="https://github.com/o/r.git", branch="main", git_identity_id=identity.id,
    )
    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=project.id, name="env",
        workspace_mode="worktree", provision_state="failed",
        provision_error="clone timeout",
    )

    calls: list[tuple] = []

    async def _manage(node, env_id, action, *, git=None, target_path=""):
        calls.append((node, env_id, action, git, target_path))
        return {}

    monkeypatch.setattr("user_platform.nodes_service.nodes_service.manage_environment", _manage)
    monkeypatch.setattr(task_service_module, "_git_proxy_origin", lambda: ORIGIN)
    monkeypatch.setattr(task_service_module, "_git_proxy_control_token", lambda: CONTROL_TOKEN)

    result = await env_service.provision_environment(user_id, str(env.id))
    assert result["provision_state"] == "provisioning"
    # The background task runs the clone with the row-derived plan.
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.02)
    assert len(calls) == 1
    node, env_id, action, git, target_path = calls[0]
    assert node == "node-1" and env_id == str(env.id) and action == "create"
    assert f"{env.id}.main.rw." in git["url"]  # environment-scoped signed URL
    assert git["branch"] == "main"
    assert target_path == ""  # worktree mode → node's envRepoDir


async def _fake_team_id(_user_id: str) -> str:
    """resolve_team_id 是协程；测试里给它一个确定值。"""
    return "team-1"


# ── 会话同步：派发前把待同步的环境资源补装 ──────────────────────────────────
#
# 环境的资源是「期望状态」，装到节点是另一回事，两者靠 revision 对账。之前只有
# 环境页上的「同步」按钮会补装，所以「在环境页加了插件但没点同步，直接开会话」
# 会让会话静默跑在缺能力的环境里。这两个用例锁住派发时的兜底行为。


@pytest.mark.asyncio
async def test_dispatch_syncs_environment_when_revision_is_ahead(tortoise_db, monkeypatch):
    """revision 领先 synced_revision = 有待同步的资源 → 派发前装一次。"""
    from user_platform import environment_service as env_service
    from user_platform.models_environment import TaskEnvironment

    user_id = str(uuid.uuid4())
    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        name="env", revision=3, synced_revision=1,
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}

    calls: list[tuple] = []

    async def _record(user, env_id, *, team_id, request_base_url=""):
        calls.append((user, env_id, team_id))
        return {}

    monkeypatch.setattr(env_service, "sync_environment", _record)
    monkeypatch.setattr("user_platform.deps.resolve_team_id", _fake_team_id)

    await TaskService()._sync_environment_for_dispatch(task)
    assert calls == [(user_id, str(env.id), "team-1")]


@pytest.mark.asyncio
async def test_dispatch_skips_sync_when_already_synced(tortoise_db, monkeypatch):
    """已同步就不该再走一轮网络往返——revision 机制存在的意义正在于此。"""
    from user_platform import environment_service as env_service
    from user_platform.models_environment import TaskEnvironment

    user_id = str(uuid.uuid4())
    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        name="env", revision=2, synced_revision=2,
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}

    calls: list[tuple] = []

    async def _record(*a, **kw):
        calls.append(a)
        return {}

    monkeypatch.setattr(env_service, "sync_environment", _record)
    await TaskService()._sync_environment_for_dispatch(task)
    assert calls == []


@pytest.mark.asyncio
async def test_dispatch_sync_failure_does_not_block(tortoise_db, monkeypatch):
    """同步失败不该让会话起不来：缺一个 skill 远好过一个跑不起来的任务。

    错误由 sync_environment 落到 last_sync_error，页面看得到；下次派发还会再试
    （revision 依旧领先）。
    """
    from user_platform import environment_service as env_service
    from user_platform.environment_service import EnvironmentError
    from user_platform.models_environment import TaskEnvironment

    user_id = str(uuid.uuid4())
    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        name="env", revision=5, synced_revision=0,
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}

    async def _boom(*a, **kw):
        raise EnvironmentError("failed_precondition", "节点离线")

    monkeypatch.setattr(env_service, "sync_environment", _boom)
    monkeypatch.setattr("user_platform.deps.resolve_team_id", _fake_team_id)

    # 不抛异常 = 不阻断派发。
    await TaskService()._sync_environment_for_dispatch(task)


@pytest.mark.asyncio
async def test_fresh_env_cuts_worktree_from_its_dir(tortoise_db):
    """A fresh env carries external_path → the task cuts from it, clones nothing.

    Under the unified model the code directory is a fact of the environment row
    (``external_path``), regardless of how it got there. ``fresh`` and
    ``external`` differ only at environment CREATION (platform clone vs operator's
    tree); at task dispatch both are "cut a worktree from this directory." So the
    fresh tag is gone — a new task gets ``workspace_mode=worktree`` + the path, and
    no clone credential is minted (the clone is env-creation's job, not the task's).
    """
    user_id = str(uuid.uuid4())
    from user_platform.models_environment import TaskEnvironment
    from user_platform.models_git import GitIdentity
    from user_platform.models_project import Project

    identity = await GitIdentity.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), platform="github", access_token="ghp_tok"
    )
    project = await Project.create(
        id=PROJECT_ID, user_id=uuid.UUID(user_id), name="proj", platform="github",
        repo_url="https://github.com/o/r.git", git_identity_id=identity.id,
    )
    env = await TaskEnvironment.create(
        id=ENV_ID, user_id=uuid.UUID(user_id), node_id="node-1",
        project_id=project.id, name="fresh-env",
        workspace_mode="fresh", external_path="/home/op/new-project",
    )
    task = _task()
    task.user_id = uuid.UUID(user_id)
    task.config_snapshot = {"env_mode": "shared", "env_id": str(env.id)}
    service = TaskService()
    req = {"repo": {"repo_url": "https://github.com/o/r.git"}, "extra": {}}

    await service._apply_environment_workspace(req, task)

    tags = req["_session_tags"]
    assert tags["workspace_mode"] == "worktree"
    assert tags["env_id"] == str(env.id)
    assert tags["external_workdir"] == "/home/op/new-project"
    # external_path present → the clone is env-creation's job; the task clones
    # nothing, so no environment-scoped credential is minted.
    assert req["repo"]["repo_url"] == "https://github.com/o/r.git"


def test_isolation_is_derived_from_the_directory():
    """隔离不是第二个选项——目录是谁的，配置就跟着谁（单一真相源）。"""
    from user_platform.models_environment import (
        WORKSPACE_MODE_EXTERNAL,
        WORKSPACE_MODE_FRESH,
        WORKSPACE_MODE_WORKTREE,
        resolve_isolated,
    )

    assert resolve_isolated(WORKSPACE_MODE_FRESH) is True
    assert resolve_isolated(WORKSPACE_MODE_WORKTREE) is True
    assert resolve_isolated(WORKSPACE_MODE_EXTERNAL) is False
