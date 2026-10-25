"""Environment-scoped git credentials: binding, lifecycle gate, delete safety.

Covers the stage-1 slice of the environment workspace plan:

* ``create_environment`` binds a project (ownership-checked) and derives the
  submodule allowlist from that project's ``.gitmodules``.
* The git proxy resolves an environment id to its project's repo/identity, and
  enforces ``enabled`` as a live gate — so disabling revokes access on the next
  request and re-enabling restores the SAME token, with nothing reissued.
* ``delete_environment`` never removes an ``external_path`` tree.

No node or control plane is involved except where a test says so; the DB is
in-memory SQLite with the environment/project/git models registered.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from user_platform import environment_service as env_service
from user_platform import nodes_service
from user_platform import task_service as task_service_module
from user_platform.environment_service import EnvironmentError

ORIGIN = "http://127.0.0.1:8003"
CONTROL_TOKEN = "control-secret"
NODE_ID = "node-1"


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
    """Keep project binding off the network; the probe has its own test below."""
    monkeypatch.setattr(
        env_service, "_project_submodule_paths", lambda _project: _empty()
    )


async def _empty():
    return []


async def _make_identity(user_id: str):
    from user_platform.models_git import GitIdentity

    return await GitIdentity.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), platform="github", access_token="ghp_tok"
    )


async def _make_project(user_id: str, identity_id=None, repo_url="https://github.com/o/r.git"):
    from user_platform.models_project import Project

    return await Project.create(
        id=uuid.uuid4(),
        user_id=uuid.UUID(user_id),
        name="proj",
        platform="github",
        repo_url=repo_url,
        git_identity_id=identity_id,
    )


# ── binding a project ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_binds_project_and_returns_it(tortoise_db):
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id)
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="env-a", project_id=str(project.id)
    )
    assert env["project_id"] == str(project.id)
    assert env["enabled"] is True
    assert env["external_path"] is None


@pytest.mark.asyncio
async def test_create_without_project_is_allowed(tortoise_db):
    """An environment used only for its HOME/resources keeps working."""
    env = await env_service.create_environment(
        str(uuid.uuid4()), node_id=NODE_ID, name="env-home-only"
    )
    assert env["project_id"] is None


@pytest.mark.asyncio
async def test_create_rejects_someone_elses_project(tortoise_db):
    owner_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    project = await _make_project(owner_id)
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            other_id, node_id=NODE_ID, name="stolen", project_id=str(project.id)
        )
    # Binding another user's project would hand this environment their git
    # identity at clone time, so ownership is checked, not trusted.
    assert exc.value.code == "not_found"


@pytest.mark.asyncio
async def test_create_rejects_project_without_repo(tortoise_db):
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id, repo_url="")
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            user_id, node_id=NODE_ID, name="no-repo", project_id=str(project.id)
        )
    assert exc.value.code == "invalid_argument"


# ── workspace mode ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_defaults_to_shared_mode(tortoise_db):
    """No mode given = the historic per-session clone, not a silent worktree."""
    env = await env_service.create_environment(
        str(uuid.uuid4()), node_id=NODE_ID, name="plain"
    )
    assert env["workspace_mode"] == "shared"


@pytest.mark.asyncio
async def test_create_worktree_requires_project(tortoise_db):
    """The shared checkout is cloned FROM the project — without one every task
    would fail at dispatch, so it is refused at creation instead."""
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="wt-no-proj", workspace_mode="worktree"
        )
    assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_worktree_with_project(tortoise_db):
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id)
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="wt", project_id=str(project.id),
        workspace_mode="worktree",
    )
    assert env["workspace_mode"] == "worktree"
    assert env["project_id"] == str(project.id)


@pytest.mark.asyncio
async def test_create_external_requires_absolute_path(tortoise_db):
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="ext-rel",
            workspace_mode="external", external_path="rel/checkout",
        )
    assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_external_requires_path(tortoise_db):
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="ext-nopath",
            workspace_mode="external",
        )
    assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_external_can_bind_project(tortoise_db):
    """An adopted tree is still a project's landing on a node.

    Binding is what files the environment under its project on the project page,
    so it is allowed — external mode simply never clones, which is why the
    binding mints no credential and requires no repo URL.
    """
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id)
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="ext-proj",
        workspace_mode="external", external_path="/home/op/repo",
        project_id=str(project.id),
    )
    assert env["workspace_mode"] == "external"
    assert env["external_path"] == "/home/op/repo"
    assert env["project_id"] == str(project.id)


@pytest.mark.asyncio
async def test_create_external_binds_project_without_repo(tortoise_db):
    """The tree is already on disk, so a repo URL is not needed to adopt it.

    Requiring one would refuse the valid "this existing checkout is that
    project's landing" case for a project whose remote is not configured here.
    """
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id, repo_url="")
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="ext-norepo",
        workspace_mode="external", external_path="/home/op/repo",
        project_id=str(project.id),
    )
    assert env["project_id"] == str(project.id)


@pytest.mark.asyncio
async def test_list_environments_filters_by_project(tortoise_db):
    """The project page lists the environments that landed for ITS project."""
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id)
    other = await _make_project(user_id, repo_url="https://github.com/o/other.git")

    await env_service.create_environment(
        user_id, node_id=NODE_ID, name="mine", project_id=str(project.id)
    )
    await env_service.create_environment(
        user_id, node_id=NODE_ID, name="theirs", project_id=str(other.id)
    )
    await env_service.create_environment(user_id, node_id=NODE_ID, name="unbound")

    rows = await env_service.list_environments(user_id, project_id=str(project.id))
    assert [r["name"] for r in rows] == ["mine"]


@pytest.mark.asyncio
async def test_create_external_succeeds_without_project(tortoise_db):
    env = await env_service.create_environment(
        str(uuid.uuid4()), node_id=NODE_ID, name="ext-ok",
        workspace_mode="external", external_path="/home/op/repo",
    )
    assert env["workspace_mode"] == "external"
    assert env["external_path"] == "/home/op/repo"
    assert env["project_id"] is None


@pytest.mark.asyncio
async def test_create_rejects_external_path_in_shared_mode(tortoise_db):
    """external_path only means something in external mode; storing it in
    shared mode would flip delete semantics for a tree nobody adopted."""
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="shared-extpath",
            external_path="/home/op/repo",
        )
    assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_rejects_unknown_mode(tortoise_db):
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="weird", workspace_mode="bogus"
        )
    assert exc.value.code == "invalid_argument"


# ── editor + isolation ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_records_provider_and_defaults_isolated(tortoise_db):
    """An environment names its editor and, by default, isolates its HOME.

    Isolation is the default because a platform-owned directory gets its own
    config — nothing it installs reaches the node operator's own setup.
    """
    env = await env_service.create_environment(
        str(uuid.uuid4()), node_id=NODE_ID, name="c", provider="claude"
    )
    assert env["provider"] == "claude"
    assert env["isolated"] is True


@pytest.mark.asyncio
async def test_isolation_follows_the_directory_not_a_second_choice(tortoise_db):
    """隔离不是用户选的第二个开关，是目录归属的推论。

    导入（external）= 目录是操作者自己的 → 配置跟着他的全局 → 不隔离；
    新建（fresh）= 目录归我们 → 自己一份配置 → 隔离。
    这是「导入/新建」与「隔离与否」其实是同一个决定的那条不变量。
    """
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id)

    imported = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="imported",
        workspace_mode="external", external_path="/home/op/repo",
    )
    assert imported["isolated"] is False

    fresh = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="fresh",
        workspace_mode="fresh", external_path="/home/op/new-project",
        project_id=str(project.id),
    )
    assert fresh["isolated"] is True


@pytest.mark.asyncio
async def test_create_fresh_requires_project(tortoise_db):
    """新建要把项目的仓库 clone 进去；没有项目就没有代码可拉。"""
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="fresh-no-proj",
            workspace_mode="fresh", external_path="/home/op/new-project",
        )
    assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_fresh_requires_absolute_path(tortoise_db):
    user_id = str(uuid.uuid4())
    project = await _make_project(user_id)
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            user_id, node_id=NODE_ID, name="fresh-rel",
            workspace_mode="fresh", external_path="relative/dir",
            project_id=str(project.id),
        )
    assert exc.value.code == "invalid_argument"


@pytest.mark.asyncio
async def test_create_rejects_unknown_provider(tortoise_db):
    """The provider decides which CLI runs, so a typo is refused rather than
    stored — otherwise the environment silently runs a different tool."""
    with pytest.raises(EnvironmentError) as exc:
        await env_service.create_environment(
            str(uuid.uuid4()), node_id=NODE_ID, name="bad", provider="emacs"
        )
    assert exc.value.code == "invalid_argument"


# ── lifecycle gate ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disable_then_reenable_flips_state(tortoise_db):
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    off = await env_service.update_environment(user_id, env["id"], enabled=False)
    assert off["enabled"] is False
    on = await env_service.update_environment(user_id, env["id"], enabled=True)
    assert on["enabled"] is True


@pytest.mark.asyncio
async def test_enabled_is_untouched_when_not_passed(tortoise_db):
    """A rename must not silently re-enable a disabled environment."""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await env_service.update_environment(user_id, env["id"], enabled=False)

    renamed = await env_service.update_environment(user_id, env["id"], name="e2")
    assert renamed["name"] == "e2"
    assert renamed["enabled"] is False


# ── delete safety ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_external_path_keeps_the_tree(tortoise_db, monkeypatch):
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="ext", workspace_mode="external",
        external_path="/home/op/my-project",
    )

    calls: list[tuple] = []
    monkeypatch.setattr(
        env_service, "get_environment", env_service.get_environment
    )
    async def _record(node_id, env_id, action):
        calls.append((node_id, env_id, action))
        return {}

    monkeypatch.setattr(nodes_service.nodes_service, "manage_environment", _record)

    result = await env_service.delete_environment(user_id, env["id"])
    # The operator's own checkout must survive: removing it would be data loss.
    assert calls == []
    assert result["kept_external_path"] == "/home/op/my-project"


@pytest.mark.asyncio
async def test_delete_managed_env_removes_the_directory(tortoise_db, monkeypatch):
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="managed")

    calls: list[tuple] = []
    async def _record(node_id, env_id, action):
        calls.append((node_id, env_id, action))
        return {}

    monkeypatch.setattr(nodes_service.nodes_service, "manage_environment", _record)

    result = await env_service.delete_environment(user_id, env["id"])
    assert calls == [(NODE_ID, env["id"], "remove")]
    assert result["kept_external_path"] is None


# ── proxy token for an environment ──────────────────────────────────────────

def test_environment_token_carries_env_id_and_rw():
    env_id = str(uuid.uuid4())
    token = task_service_module.environment_git_proxy_url(
        env_id, "https://github.com/o/r.git"
    )
    # .../<env_id>.main.rw.<sig>@<origin>/api/v1/public/nodes/git/r/o/r.git/
    assert f"{env_id}.main.rw." in token
    assert token.endswith("/api/v1/public/nodes/git/r/o/r.git/")
    assert token.startswith(f"http://{env_id}.main.rw.")


def test_environment_token_fails_closed_without_proxy_config(monkeypatch):
    monkeypatch.setattr(task_service_module, "_git_proxy_origin", lambda: "")
    with pytest.raises(ValueError, match="git_proxy_origin_missing"):
        task_service_module.environment_git_proxy_url(
            str(uuid.uuid4()), "https://github.com/o/r.git"
        )


def test_environment_token_signature_differs_from_task_token():
    """Same id, different scope semantics — the signature must still bind both."""
    ident = str(uuid.uuid4())
    env_sig = task_service_module._git_proxy_token(ident, "main", "rw").split(".")[-1]
    other_sig = task_service_module._git_proxy_token(ident, "main", "ro").split(".")[-1]
    assert env_sig != other_sig


def test_route_request_model_matches_create_environment():
    """CreateEnvReq 接受的每个参数，create_environment 都必须真的收。

    这是踩过的坑：隔离从独立参数改成由 workspace_mode 推导后，路由仍在传
    ``isolated=``，而服务函数已删掉该形参——建环境会直接 TypeError 500。字段
    漂移在类型系统里静默，只有真发一次请求才炸，所以在这里用签名对一次账。
    """
    import inspect

    from user_platform.routes_environment import CreateEnvReq
    from user_platform.environment_service import create_environment

    accepted = set(inspect.signature(create_environment).parameters)
    # node_id/name 之外的字段由 pydantic 模型声明；模型里有的，服务层必须能收。
    declared = set(CreateEnvReq.model_fields) - {"node_id", "name"}
    missing = declared - accepted
    assert not missing, f"路由声明了服务层不收的参数：{sorted(missing)}"


def test_isolation_is_not_a_request_field():
    """隔离不是请求参数——它是 workspace_mode 的推论，不该能从外面指定。"""
    from user_platform.routes_environment import CreateEnvReq

    assert "isolated" not in CreateEnvReq.model_fields


# ── 老行（加列之前建的）────────────────────────────────────────────────────
#
# workspace_mode / enabled / isolated 都是表建好之后才加的列，ADD COLUMN 给已有
# 行填的是 NULL。这一组锁住"老行必须跟新行一样可用"，因为线上真踩过：
# enabled=NULL 被当成"已禁用"、mode=NULL 让前端把共用环境当成 system 档。


@pytest.mark.asyncio
async def test_legacy_row_with_null_columns_is_still_usable(tortoise_db):
    """老行（三列全 NULL）不能被当成"已禁用"，且档位要能归一。

    用一个只有 `_env_dict` 会读的字段的最小对象代替真实行：造 NULL 的行要绕过
    ORM（字段上挂着 MaxLengthValidator，写 None 会被它拦下），而线上这些行是
    ALTER TABLE ADD COLUMN 留下的 NULL，唯一测得到的就是这段归一逻辑本身。
    """
    from types import SimpleNamespace

    from user_platform import environment_service as env_service

    row = SimpleNamespace(
        id=uuid.uuid4(),
        name="legacy",
        description=None,
        node_id=NODE_ID,
        project_id=None,
        enabled=None,
        external_path=None,
        workspace_mode=None,
        provider=None,
        submodule_paths=[],
        init_commands=[],
        revision=0,
        synced_revision=0,
        last_synced_at=None,
        last_sync_error=None,
        # 同样是 ALTER ADD COLUMN 留下的 NULL：老行没有单独配间隔，跟随全局默认。
        session_sync_interval=None,
        # provision_state 也是列后加的 NULL：老行（无 clone 可验证）按 ready 处理。
        provision_state=None,
        provision_error=None,
        provisioned_at=None,
    )
    data = env_service._env_dict(row)

    # enabled 为 NULL 时 `not None` 是 True——老代码据此报"已禁用"，派发直接拒绝。
    assert data["enabled"] is True
    # 档位与隔离必须同源：都按兜底后的 shared 算，不能一个 shared 一个 false。
    assert data["workspace_mode"] == "shared"
    assert data["isolated"] is True
    # 定时同步间隔的 NULL 必须原样报成 None（=跟随全局默认），不能塌成 0（=关闭）。
    assert data["session_sync_interval"] is None
    # provision_state 的 NULL 报成 ready：老行没有可准备的 clone，不该被派发闸门挡住。
    assert data["provision_state"] == "ready"


@pytest.mark.asyncio
async def test_duplicate_names_are_allowed(tortoise_db):
    """名字只是标签，id 才是身份——同名不该挡人。

    约束是「同用户+同节点+同名」，而列表按项目过滤：一个 project_id 为空的老环境
    在项目页永远看不见，却照样挡住建环境（线上 409「已有同名环境」而用户看不到它）。
    """
    user_id = str(uuid.uuid4())
    first = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="same"
    )
    second = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="same"
    )
    assert first["id"] != second["id"]
    rows = await env_service.list_environments(user_id)
    assert len([r for r in rows if r["name"] == "same"]) == 2


@pytest.mark.asyncio
async def test_rename_to_an_existing_name_is_allowed(tortoise_db):
    user_id = str(uuid.uuid4())
    await env_service.create_environment(user_id, node_id=NODE_ID, name="a")
    other = await env_service.create_environment(user_id, node_id=NODE_ID, name="b")
    renamed = await env_service.update_environment(user_id, other["id"], name="a")
    assert renamed["name"] == "a"


# ── 环境会话账本（读 + 与节点对账）─────────────────────────────────────────
#
# mc_ac_node_sessions 归控制面注册，数据进程故意不注册它的 Tortoise 模型——所以
# 这里的读写真走 raw SQL。曾经我用 Tortoise 模型查它，一调就
# ConfigurationError: default_connection ... cannot be None（线上 500 的根因）。
#
# 假池存在的唯一理由是驱动差异（asyncpg vs sqlite），不是被测行为：占位符形状、
# 时间戳类型。SQL 本身是真跑的——「冲突就跳过」「按环境筛」「只标不删」这些行为
# 都写在 SQL 里，不真跑就等于没测。生产 SQL 也因此刻意避开了 ``now()`` 和
# ``= ANY($n::text[])`` 这类 asyncpg 专属写法。

# 账本表照抄控制面的真实 schema（连接生产库核对过），包括它的最坏之处：除了
# ``editor_*``/``task_id``/``env_id`` 那几列可空，其余全是 NOT NULL 且**没有库级
# 默认值**——默认值写在 node_server 的模型层，DDL 里没有。曾经测试自建的表给这些
# 列都挂了 ``NOT NULL DEFAULT ''``，于是 INSERT 漏给六列照样过，一上生产就是
# NotNullViolation。测试表比被测 SQL 宽松，等于没测。
_SESSION_DDL = (
    "CREATE TABLE IF NOT EXISTS mc_ac_node_sessions ("
    " session_id VARCHAR(128) NOT NULL PRIMARY KEY,"
    " node_id VARCHAR(128) NOT NULL,"
    " project_id VARCHAR(128) NOT NULL,"
    " provider VARCHAR(64) NOT NULL,"
    " model VARCHAR(128) NOT NULL,"
    " mode VARCHAR(64) NOT NULL,"
    " effort VARCHAR(16) NOT NULL DEFAULT '',"
    " status VARCHAR(32) NOT NULL,"
    " applied_revision INTEGER NOT NULL,"
    " effective_revision INTEGER NOT NULL,"
    " created_at TIMESTAMP NOT NULL,"
    " updated_at TIMESTAMP NOT NULL,"
    " editor_id VARCHAR(128),"
    " editor_session_id VARCHAR(128),"
    " task_id VARCHAR(128),"
    " env_id VARCHAR(128),"
    # 会话标题来自节点对账；platform_name 是用户在平台改的名字，二者只进这
    # 一列各自的写者（对账写 title、改名接口写 platform_name），互不覆盖。
    " title VARCHAR(512) NOT NULL,"
    " platform_name VARCHAR(512) NOT NULL,"
    # 会话最后一次在节点上活动的时间（对账从节点扫描结果写入），可空——
    # 控制面派发建的行还没被对账过时就是 NULL。
    " last_activity_at TIMESTAMP,"
    # 会话历史的显示副本（导入接口写的 JSON 串：{items, head, window_lines}）。
    # 与生产同形：数据面 raw SQL 读写，控制面模型里没有这一列。
    " transcript TEXT)"
)


def _to_sqlite(sql: str, args: tuple) -> tuple[str, list]:
    """``$1`` 占位 → sqlite 的 ``?``，参数按出现顺序摊平（同一个 ``$n`` 可以重复出现）。"""
    import re as _re

    params: list = []

    def _take(match):
        value = args[int(match.group(1)) - 1]
        # sqlite3 的 datetime 适配器在 3.12 已弃用，索性自己转成文本。
        params.append(value.isoformat(" ") if isinstance(value, datetime) else value)
        return "?"

    return _re.sub(r"\$(\d+)", _take, sql), params


def _as_asyncpg_row(row: dict) -> dict:
    """sqlite 把时间戳读成字符串，asyncpg 读成 datetime——生产代码按 datetime 用。"""
    for key in ("created_at", "updated_at"):
        value = row.get(key)
        if isinstance(value, str) and value:
            row[key] = datetime.fromisoformat(value)
    return row


class _FakeLedgerConn:
    def __init__(self, conn):
        self._conn = conn

    async def fetch(self, sql, *args):
        query, params = _to_sqlite(sql, args)
        rows = await self._conn.execute_query_dict(query, params)
        return [_as_asyncpg_row(dict(r)) for r in rows]

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        # 列表分页的 COUNT(*) 走这里；取第一行第一列，空结果给 0。
        row = await self.fetchrow(sql, *args)
        if not row:
            return 0
        return next(iter(row.values()))

    async def execute(self, sql, *args):
        query, params = _to_sqlite(sql, args)
        changed, _rows = await self._conn.execute_query(query, params)
        return f"UPDATE {changed}"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


async def _fetch_ledger_row(env_id: str, session_id: str) -> dict | None:
    """直接从账本里捞一行（绕过环境归一化），给清理类断言用。"""
    import db as db_module

    if not db_module.PostgresClient.pool:
        return None
    async with db_module.PostgresClient.pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT session_id, env_id, task_id, title, platform_name, mode, effort "
            "FROM mc_ac_node_sessions WHERE session_id=$1 AND env_id=$2",
            session_id, env_id,
        )


async def _fake_ledger_pool(monkeypatch):
    """建出会话账本表，把假池装进 ``PostgresClient.pool``，返回底层连接供播种。

    连接从模型上取而不是 ``Tortoise.get_connection("user_platform")``：fixture 走
    ``db_url=`` 形态，连接别名是 ``default``，``user_platform`` 只是 app 名——拿
    别名去查连接必然 ConfigurationError。（顺带：池用 monkeypatch 装，测试结束
    自动还原，别把假池留给别的用例。）
    """
    import db as db_module
    from user_platform.models_environment import TaskEnvironment

    conn = TaskEnvironment._meta.db
    await conn.execute_script(_SESSION_DDL)
    monkeypatch.setattr(db_module.PostgresClient, "pool", _FakePool(_FakeLedgerConn(conn)))
    return conn


async def _seed_session(conn, session_id: str, env_id: str, status: str = "running"):
    """按控制面派发时写的字段形状播种一条会话（NOT NULL 的列全给值，env_id 指明归属）。"""
    await conn.execute_query(
        "INSERT INTO mc_ac_node_sessions "
        "(session_id, node_id, project_id, provider, model, mode, status, "
        " applied_revision, effective_revision, created_at, updated_at, env_id, "
        " title, platform_name) "
        "VALUES (?, ?, ?, 'claude', '', '', ?, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, "
        " '', '')",
        [session_id, NODE_ID, "", status, env_id],
    )


@pytest.mark.asyncio
async def test_list_env_sessions_reads_the_ledger(tortoise_db, monkeypatch):
    """列表读的是库里的账本，不是现场去问节点。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "s-1", env["id"])

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("列表不该去问节点")

    monkeypatch.setattr(
        nodes_service.nodes_service, "list_environment_sessions", _must_not_run
    )

    page = await env_service.list_environment_sessions(user_id, env["id"])
    rows = page["sessions"]
    assert [r["session_id"] for r in rows] == ["s-1"]
    assert page["total"] == 1
    assert rows[0]["status"] == "running"
    assert rows[0]["provider"] == "claude"
    assert rows[0]["synced_at"]  # 时间戳能取出来（asyncpg 给 datetime，不是字符串）


@pytest.mark.asyncio
async def test_list_env_sessions_is_scoped_to_the_environment(tortoise_db, monkeypatch):
    """别的环境的会话不能串进来。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    mine = await env_service.create_environment(user_id, node_id=NODE_ID, name="mine")
    other = await env_service.create_environment(user_id, node_id=NODE_ID, name="other")
    await _seed_session(conn, "s-mine", mine["id"])
    await _seed_session(conn, "s-other", other["id"])

    rows = (await env_service.list_environment_sessions(user_id, mine["id"]))["sessions"]
    assert [r["session_id"] for r in rows] == ["s-mine"]


@pytest.mark.asyncio
async def test_list_env_sessions_reads_local_when_the_pool_is_down(tortoise_db, monkeypatch):
    """池没起来时给空列表，不是 500——页面开得出来，只是没有历史。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    assert await env_service.list_environment_sessions(user_id, env["id"]) == {
        "sessions": [], "total": 0
    }


def _patch_transcript_node(monkeypatch, lines: list[str]) -> dict:
    """把「读会话记录」这条节点通路钉成给定行，返回 ``{"n", "lines"}`` 状态盒。

    ``n`` 是被调了几次（**导入**接口允许碰节点，**读**接口一次都不许碰——这正是
    "详情永远先读库"的可执行定义）；``lines`` 可在两次导入之间改写，模拟节点文件
    在轮询间隙里长出了新内容。
    """
    import json

    async def _home(*_a, **_k):
        return r"C:\Users\op"

    async def _none(*_a, **_k):
        return None

    async def _win(*_a, **_k):
        return True

    monkeypatch.setattr(nodes_service.nodes_service, "environment_home_path", _home)
    monkeypatch.setattr(nodes_service.nodes_service, "node_is_windows", _win)
    monkeypatch.setattr(nodes_service.nodes_service, "node_user_home", _none)

    box = {"n": 0, "lines": list(lines)}

    class _FakeClient:
        async def host_exec(self, *_a, **_k):
            box["n"] += 1
            return {"stdout": "\n".join(box["lines"]), "stderr": "", "exit_code": 0}

    from user_platform import node_client

    monkeypatch.setattr(node_client, "get_local_node_client", lambda: _FakeClient())
    return box


@pytest.mark.asyncio
async def test_get_session_transcript_reads_the_ledger_not_the_node(tortoise_db, monkeypatch):
    """详情永远先读库——历史是导入时留下的**副本**，读接口一趟节点都不发。

    这条钉住本次修的根本问题：以前 ``get_session_transcript`` 自己 host_exec 到节点
    现读 jsonl，节点控制面一超时（实测 WinError 121 信号灯超时）整个详情页就打不开。
    内容的事实来源仍是节点，但那是**导入**接口的事；读必须只认库。

    顺带钉住前后端契约（键名 ``items``）与规范条目形状（运行时同源的字段词表）。
    """
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    line = json.dumps({
        "type": "assistant",
        "timestamp": "2026-09-30T10:00:00Z",
        "message": {"content": [{"type": "text", "text": "你好"}]},
    })
    _patch_transcript_node(monkeypatch, [line])

    stored = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )
    assert [i["text"] for i in stored["after"]] == ["你好"]

    # 读接口不许再碰节点：把客户端换成"敢调就炸"。
    from user_platform import node_client

    def _boom():
        raise AssertionError("transcript 读接口不该碰节点")

    monkeypatch.setattr(node_client, "get_local_node_client", _boom)

    out = await env_service.get_session_transcript(user_id, env["id"], "sid-1")
    assert "items" in out and "messages" not in out
    # 规范条目 = 运行时 claudeMessageToItems 同源字段：status 是 SDK 词汇 done，
    # 带 logical_event_id/event_kind/phase，前端 itemToRootMessage 直接吃。
    item = out["items"][0]
    assert item == {
        "id": item["id"], "type": "agent_message", "text": "你好",
        "logical_event_id": "0", "event_kind": "message",
        "phase": "complete", "status": "done", "created_at": 1790762400,
    }
    # 消息条目没有原生稳定 id（这行连 uuid 都没有，解析器给的是 counter "0"），
    # 所以入库时换成内容指纹派生的 id——跨窗口不变，前端才不会重复渲染。
    assert str(item["id"]).startswith("env:")
    assert out["session"]["has_history"] is True
    assert out["total"] == 1
    # 1 行 < 请求窗口 10 → 文件已经读完，没有更早的的历史。
    assert out["head"] is True


@pytest.mark.asyncio
async def test_transcript_import_is_idempotent_across_windows(tortoise_db, monkeypatch):
    """同一窗口重导不产生重复；窗口变大时只补更早的那些。

    去重键是**内容指纹**，不是解析器给的 id——codex 的消息 id 是自增 counter，
    窗口从 10 行变 20 行时同一条消息会拿到不同 counter，拿 id 去重历史就整段重复。
    """
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    def _line(i: int) -> str:
        return json.dumps({
            "type": "assistant", "uuid": f"u{i}", "timestamp": 1790762400 + i,
            "message": {"content": [{"type": "text", "text": f"第{i}条"}]},
        })

    _patch_transcript_node(monkeypatch, [_line(i) for i in range(5)])
    first = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )
    assert len(first["after"]) == 5

    again = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )
    assert again["after"] == []          # 一行都不该新增
    assert again["total"] == 5           # 库里也没变多

    out = await env_service.get_session_transcript(user_id, env["id"], "sid-1", limit=60)
    assert [i["text"] for i in out["items"]] == [f"第{i}条" for i in range(5)]
    assert len({i["id"] for i in out["items"]}) == 5   # 没有重复气泡的隐患


@pytest.mark.asyncio
async def test_transcript_poll_appends_only_new_tail(tortoise_db, monkeypatch):
    """轮询增量：文件长出新的尾部 → 只返回新条目放进 ``after``，旧的不动。"""
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    def _line(i: int) -> str:
        return json.dumps({
            "type": "assistant", "uuid": f"u{i}", "timestamp": 1790762400 + i,
            "message": {"content": [{"type": "text", "text": f"第{i}条"}]},
        })

    calls = _patch_transcript_node(monkeypatch, [_line(i) for i in range(3)])
    await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )

    # 会话继续聊：节点上多出第 3、4 条。
    calls["lines"][:] = [_line(i) for i in range(5)]
    grown = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )
    assert [i["text"] for i in grown["after"]] == ["第3条", "第4条"]
    assert grown["before"] == []         # 轮询不该带出更早的
    assert grown["total"] == 5

    out = await env_service.get_session_transcript(user_id, env["id"], "sid-1", limit=60)
    assert [i["text"] for i in out["items"]] == [f"第{i}条" for i in range(5)]


@pytest.mark.asyncio
async def test_transcript_poll_window_growth_does_not_append_older(tortoise_db, monkeypatch):
    """回归：tail 轮询窗口随已存增长时，新捞进窗口的**更早历史**绝不能进 after。

    这是「会话详情越看越卡、几百条」的根因：窗口从 400 涨到 800/1200…，每轮把更早
    的行新捞进窗口；旧代码只在 ``mode == "history"`` 时按位置切分，tail 分支把这些
    更早的条目整个塞进 ``after``，前端按增量追加到列表尾部 → 每 2.5s 堆一段旧记录，
    几百条后 reduceItems + 重组件把页面拖死。修复后 tail 也按位置切分：左端归
    ``before``（前端轮询只读 after，忽略它），只有右端新输出进 ``after``。
    """
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    def _line(i: int) -> str:
        return json.dumps({
            "type": "assistant", "uuid": f"u{i}", "timestamp": 1790762400 + i,
            "message": {"content": [{"type": "text", "text": f"第{i}条"}]},
        })

    all_lines = [_line(i) for i in range(1200)]

    # 第一轮 tail（不给 window_lines → 走默认窗口）只够到尾部一段，已存 = 那一段。
    calls = _patch_transcript_node(monkeypatch, all_lines[-400:])
    first = await env_service.import_env_session_items(user_id, env["id"], "sid-1", mode="tail")
    first_after = len(first["after"])
    assert first_after > 0

    # 第二轮：文件一行没长，节点仍返回**整段**（模拟窗口变大后够到更多更早的行）。
    # 修复前：这些更早的行会全进 after，列表越堆越多。修复后：after 必须为空。
    calls["lines"][:] = all_lines
    grown = await env_service.import_env_session_items(user_id, env["id"], "sid-1", mode="tail")
    assert grown["after"] == []            # 一行新输出都没有
    assert grown["total"] == 1200          # 更早的进库了，但不该被当成增量
    # 更早的历史确实入库了（前端上翻 loadOlder 看得到），只是不冒充「新消息」。
    out = await env_service.get_session_transcript(user_id, env["id"], "sid-1", limit=500)
    assert out["total"] == 1200
    assert out["items"][-1]["text"] == "第1199条"
    assert out["has_more"] is True         # 前面还有更早的没返回（单次上限 500）


@pytest.mark.asyncio
async def test_transcript_history_loads_older_and_reports_head(tortoise_db, monkeypatch):
    """「查看历史」把窗口调大 → 更早的条目进 ``before``；读到文件头就不再假装还有。"""
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    def _line(i: int) -> str:
        return json.dumps({
            "type": "assistant", "uuid": f"u{i}", "timestamp": 1790762400 + i,
            "message": {"content": [{"type": "text", "text": f"第{i}条"}]},
        })

    all_lines = [_line(i) for i in range(10)]

    # 第一次只够到尾部 4 行（窗口小）→ head 必须是 False：前面还有更早的。
    calls = _patch_transcript_node(monkeypatch, all_lines[-4:])
    tail = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=4
    )
    assert [i["text"] for i in tail["after"]] == [f"第{i}条" for i in range(6, 10)]
    assert tail["head"] is False

    first_page = await env_service.get_session_transcript(
        user_id, env["id"], "sid-1", limit=4
    )
    assert first_page["has_more"] is False   # 库里已经翻到头了
    assert first_page["head"] is False       # 但节点上还有更早 → 该出「查看历史」按钮

    # 点「查看历史」：窗口自动变大，够到更早的 6 条，且这次读到文件头。
    calls["lines"][:] = all_lines
    hist = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="history"
    )
    assert [i["text"] for i in hist["before"]] == [f"第{i}条" for i in range(6)]
    assert hist["after"] == []
    assert hist["total"] == 10
    assert hist["head"] is True

    # 再到头之后继续点：如实说没有新东西，不要空转。
    again = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="history"
    )
    assert again["before"] == [] and again["after"] == []

    whole = await env_service.get_session_transcript(user_id, env["id"], "sid-1", limit=60)
    assert [i["text"] for i in whole["items"]] == [f"第{i}条" for i in range(10)]
    assert whole["head"] is True


@pytest.mark.asyncio
async def test_transcript_pages_the_copy_by_cursor(tortoise_db, monkeypatch):
    """库里副本的向上翻页用绝对下标游标；翻完 has_more=False，而不是报错。"""
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    _patch_transcript_node(monkeypatch, [
        json.dumps({
            "type": "assistant", "uuid": f"u{i}", "timestamp": 1790762400 + i,
            "message": {"content": [{"type": "text", "text": f"第{i}条"}]},
        }) for i in range(6)
    ])
    await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )

    page1 = await env_service.get_session_transcript(user_id, env["id"], "sid-1", limit=2)
    assert [i["text"] for i in page1["items"]] == ["第4条", "第5条"]
    assert page1["oldest_idx"] == 4 and page1["has_more"] is True

    page2 = await env_service.get_session_transcript(
        user_id, env["id"], "sid-1", limit=2, before_idx=page1["oldest_idx"]
    )
    assert [i["text"] for i in page2["items"]] == ["第2条", "第3条"]
    assert page2["oldest_idx"] == 2 and page2["has_more"] is True

    last = await env_service.get_session_transcript(
        user_id, env["id"], "sid-1", limit=2, before_idx=page2["oldest_idx"]
    )
    assert [i["text"] for i in last["items"]] == ["第0条", "第1条"]
    assert last["oldest_idx"] == 0 and last["has_more"] is False


@pytest.mark.asyncio
async def test_transcript_keeps_tool_frames_under_one_id(tortoise_db, monkeypatch):
    """工具开帧与闭帧必须**同 id**：前端 reduceItems 按 id 并成一张卡，
    否则那张卡会永远转圈。同时它们是两条独立条目（指纹不同）。
    """
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-1", env["id"])

    _patch_transcript_node(monkeypatch, [
        json.dumps({
            "type": "assistant", "uuid": "u1", "timestamp": 1790762400,
            "message": {"content": [{"type": "tool_use", "id": "toolu_9",
                                     "name": "Bash", "input": {"command": "ls"}}]},
        }),
        json.dumps({
            "type": "user", "uuid": "u2", "timestamp": 1790762401,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9",
                                     "content": "a.txt"}]},
        }),
    ])
    await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=10
    )

    out = await env_service.get_session_transcript(user_id, env["id"], "sid-1")
    assert [i["id"] for i in out["items"]] == ["toolu_9", "toolu_9"]
    assert [i["phase"] for i in out["items"]] == ["start", "complete"]
    assert out["items"][1]["output"] == "a.txt"


@pytest.mark.asyncio
async def test_transcript_import_survives_codex_counter_ids(tortoise_db, monkeypatch):
    """codex 消息没有原生 id，解析器给的是自增 counter——窗口变大时它会漂。

    这条钉住：**绝不能**把 counter 当返回 id 或去重键。窗口从 4 行放大到 8 行，
    同一条消息的 counter 变了，库里必须还是 4 条、且 id 不变。
    """
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="codex"
    )
    await _seed_session(conn, "sid-1", env["id"])

    def _line(i: int) -> str:
        return json.dumps({
            "type": "event_msg", "timestamp": f"2026-09-30T10:00:{i:02d}Z",
            "payload": {"type": "agent_message", "message": f"回答{i}"},
        })

    all_lines = [_line(i) for i in range(8)]
    calls = _patch_transcript_node(monkeypatch, all_lines[-4:])
    first = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=4
    )
    ids_before = [i["id"] for i in first["after"]]
    assert len(ids_before) == 4

    # 同样的 4 条，counter 全变（窗口从文件头数起）。
    calls["lines"][:] = all_lines[-4:]
    again = await env_service.import_env_session_items(
        user_id, env["id"], "sid-1", mode="tail", window_lines=4
    )
    assert again["after"] == []
    assert again["total"] == 4

    out = await env_service.get_session_transcript(user_id, env["id"], "sid-1")
    assert [i["id"] for i in out["items"]] == ids_before   # id 没被 counter 漂掉


@pytest.mark.asyncio
async def test_transcript_empty_ledger_shows_nothing_without_the_node(tortoise_db, monkeypatch):
    """没导过的会话：读接口给空列表 + head=False，不发节点请求。

    以前这条会直接去节点现读，于是"节点不可达"变成了详情页整块报错；现在页面照常
    打得开，只是没有历史，并由 ``head=False`` 让「查看历史」按钮成为唯一入口。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )
    await _seed_session(conn, "sid-none", env["id"])

    from user_platform import node_client

    def _boom():
        raise AssertionError("库里没内容时读接口也不该碰节点")

    monkeypatch.setattr(node_client, "get_local_node_client", _boom)

    out = await env_service.get_session_transcript(user_id, env["id"], "sid-none")
    assert out["items"] == []
    assert out["total"] == 0
    assert out["has_more"] is False
    assert out["head"] is False
    assert out["session"]["has_history"] is False


@pytest.mark.asyncio
async def test_sync_sessions_imports_content_only_when_asked(tortoise_db, monkeypatch):
    """「同步数据」把内容也拉一份；加减资源不该为每个会话多跑一趟节点。"""
    import json

    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", provider="claude"
    )

    _patch_node_sessions(monkeypatch, [{"session_id": "sid-a", "title": "甲"}])
    line = json.dumps({
        "type": "assistant", "uuid": "u1", "timestamp": 1790762400,
        "message": {"content": [{"type": "text", "text": "甲的内容"}]},
    })
    calls = _patch_transcript_node(monkeypatch, [line])

    await env_service.sync_environment_sessions(user_id, env["id"])
    assert calls["n"] == 0                       # 默认不导内容
    plain = await env_service.get_session_transcript(user_id, env["id"], "sid-a")
    assert plain["items"] == []

    await env_service.sync_environment_sessions(user_id, env["id"], include_content=True)
    assert calls["n"] == 1                       # 同步数据：每个会话一趟
    seeded = await env_service.get_session_transcript(user_id, env["id"], "sid-a")
    assert [i["text"] for i in seeded["items"]] == ["甲的内容"]


@pytest.mark.asyncio
async def test_sync_treats_the_node_as_authority(tortoise_db, monkeypatch):
    """对账以节点为准：节点报的补进来，节点没有的标为已结束（不删）。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    # 库里有一条节点已经不报的旧会话。
    await _seed_session(conn, "gone", env["id"])

    _patch_node_sessions(monkeypatch, [{"session_id": "fresh"}])

    result = await env_service.sync_environment_sessions(user_id, env["id"])
    by_id = {r["session_id"]: r for r in result["sessions"]}
    assert by_id["fresh"]["status"] == "running"   # 节点有的补进来
    assert by_id["gone"]["status"] == "ended"       # 节点没有的标结束
    assert len(by_id) == 2                          # 但没有删掉它
    assert result["changed"] == 2                   # 一条新增 + 一条标结束


@pytest.mark.asyncio
async def test_sync_is_idempotent(tortoise_db, monkeypatch):
    """同一份节点报告再对一次账，不该有任何改动。

    两个条件都要锁住，缺任何一个都会让「每次刷新白刷一批 updated_at」：

    * 节点仍在报的那条 —— 靠 upsert 末尾的 ``WHERE ... IS DISTINCT FROM``。没有它，
      每点一次同步所有在线会话的时间戳都被推成当下，页面顺序跟着乱跳。
    * 节点不再报、已经标过结束的那条 —— 靠结束更新里的 ``status IS DISTINCT FROM
      'ended'``。没有它，每次都把它重标一遍，``changed`` 永远不为 0。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "old", env["id"], status="ended")

    _patch_node_sessions(monkeypatch, [{"session_id": "s-1"}])

    first = await env_service.sync_environment_sessions(user_id, env["id"])
    assert first["changed"] == 1            # 只有 s-1 是新增；old 已经是 ended
    second = await env_service.sync_environment_sessions(user_id, env["id"])
    assert second["changed"] == 0           # 两条都跳到各自的条件外
    assert len(second["sessions"]) == 2


@pytest.mark.asyncio
async def test_sync_touches_only_this_environments_sessions(tortoise_db, monkeypatch):
    """对账不能把别的环境的会话标成结束——即便节点这次什么都没报。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    mine = await env_service.create_environment(user_id, node_id=NODE_ID, name="mine")
    other = await env_service.create_environment(user_id, node_id=NODE_ID, name="other")
    await _seed_session(conn, "s-other", other["id"])

    _patch_node_sessions(monkeypatch, [])

    await env_service.sync_environment_sessions(user_id, mine["id"])

    rows = (await env_service.list_environment_sessions(user_id, other["id"]))["sessions"]
    assert [r["status"] for r in rows] == ["running"]


@pytest.mark.asyncio
async def test_sync_ends_everything_when_the_node_reports_nothing(tortoise_db, monkeypatch):
    """节点一个会话都不认了（记录被清掉/换了 HOME），库里的全标结束。

    这条走的是「``seen`` 为空、不加 NOT IN」那条分支——曾经写成
    ``= ANY($2::text[])`` 时，空数组在 PG 上是假、在别处是语法错，正是要锁住的
    边界。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "a", env["id"])
    await _seed_session(conn, "b", env["id"])

    _patch_node_sessions(monkeypatch, [])

    result = await env_service.sync_environment_sessions(user_id, env["id"])
    assert {r["status"] for r in result["sessions"]} == {"ended"}
    assert len(result["sessions"]) == 2


@pytest.mark.asyncio
async def test_sync_refuses_when_the_node_is_unreachable(tortoise_db, monkeypatch):
    """节点连不上就报错，不能把「读不到」当成「已经结束」——那会把会话全标死。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "s-1", env["id"])

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        nodes_service.nodes_service, "list_environment_sessions", _boom
    )

    with pytest.raises(EnvironmentError):
        await env_service.sync_environment_sessions(user_id, env["id"])

    rows = (await env_service.list_environment_sessions(user_id, env["id"]))["sessions"]
    assert [r["status"] for r in rows] == ["running"]  # 原始记录没被误标


def _patch_node_sessions(monkeypatch, reported: list[dict]) -> None:
    """把节点上报的会话钉死成给定列表（对账的另一半输入）。"""
    async def _report(_node, _env, _provider, workdir="", days=7):
        return list(reported)

    monkeypatch.setattr(
        nodes_service.nodes_service, "list_environment_sessions", _report
    )



# ── 环境 HOME：只有隔离环境才有「环境目录」─────────────────────────────────
#
# 这一段锁的是一个会让会话列表恒空的真实回归。
#
# 服务端派发时按 resolve_isolated 决定 env_mode（shared 或 system，见
# task_service），节点据此选 HOME：system = 操作者真实 HOME，
# shared = <work_root>/envs/<env_id>。扫描也必须按同一个 resolve_isolated 选
# HOME——曾经它无条件返回 <work_root>/envs/<env_id>，于是节点升到会报 work_root
# 的版本之后，external 环境的扫描被打到「编辑器从没写过任何记录」的目录，
# 列表恒空（操作者 HOME 下明明有三十多条）。

_WORK_ROOT = r"C:\Users\op\agent-compose-node"


def _patch_node_caps(monkeypatch, *, work_root=_WORK_ROOT):
    """把节点能力桩成「报了/没报 work_root」两种状态。"""
    caps = {"work_root": work_root} if work_root else {}

    async def _fake(_node_id):
        return {"id": NODE_ID, "capabilities": caps}

    monkeypatch.setattr(nodes_service.nodes_service, "get_node_if_exists", _fake)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expect_env_dir",
    # external 是导入——目录是操作者自己的 checkout，编辑器用他的全局配置，
    # 也就是他的 HOME，根本没有环境目录这回事（空串 → 调用方回退操作者 HOME）。
    # 其余三档都是我们自己的目录，配置也归环境。
    [("external", False), ("shared", True), ("worktree", True), ("fresh", True)],
)
async def test_env_home_only_for_isolated_modes(tortoise_db, monkeypatch, mode, expect_env_dir):
    """隔离档给环境目录，external 给空串（调用方回退操作者 HOME）。"""
    _patch_node_caps(monkeypatch)
    user_id = str(uuid.uuid4())
    kwargs = {"workspace_mode": mode}
    if mode in ("external", "fresh"):
        kwargs["external_path"] = r"D:\code\proj"
    if mode in ("fresh", "worktree"):
        # 这两档要从项目仓库 clone，必须先绑项目（create 会直接拒）。
        kwargs["project_id"] = str((await _make_project(user_id)).id)
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", **kwargs
    )

    home = await nodes_service.nodes_service.environment_home_path(NODE_ID, env["id"])
    if expect_env_dir:
        assert home == f"{_WORK_ROOT}/envs/{env['id']}"
    else:
        assert home == ""  # external：调用方据此回退操作者 HOME，而不是空扫


@pytest.mark.asyncio
async def test_env_home_never_reported_means_fallback(tortoise_db, monkeypatch):
    """节点没报 work_root（旧程序）时给空串——宁可退到操作者 HOME，也不猜路径。"""
    _patch_node_caps(monkeypatch, work_root="")
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e", workspace_mode="shared"
    )
    assert await nodes_service.nodes_service.environment_home_path(NODE_ID, env["id"]) == ""


@pytest.mark.asyncio
async def test_env_home_refuses_env_from_another_node(tortoise_db, monkeypatch):
    """环境在 A 节点，别拿 A 的目录当 B 的去扫。"""
    _patch_node_caps(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id="node-elsewhere", name="e", workspace_mode="shared"
    )
    assert await nodes_service.nodes_service.environment_home_path(NODE_ID, env["id"]) == ""


@pytest.mark.asyncio
async def test_env_home_rejects_a_non_uuid_env_id(tortoise_db, monkeypatch):
    """目录名由 env_id 派生，非 UUID（或带路径分隔）的一律不拼。"""
    _patch_node_caps(monkeypatch)
    assert await nodes_service.nodes_service.environment_home_path(NODE_ID, "../../etc") == ""
    assert await nodes_service.nodes_service.environment_home_path(NODE_ID, "") == ""
    assert await nodes_service.nodes_service.environment_home_path(NODE_ID, "not-a-uuid") == ""


@pytest.mark.asyncio
async def test_scan_falls_back_to_the_operators_home_for_external(tortoise_db, monkeypatch):
    """不隔离环境的扫描读的是操作者 HOME——这条断言钉死「回退确实发生了」。

    external 档 environment_home_path 返回空，list_environment_sessions 必须因此
    改走 node_user_home，而不是拿空字符串去拼命令（那样命令照发、静默返回空，
    看起来像「节点上没有会话」，最难查）。
    """
    _patch_node_caps(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e",
        workspace_mode="external", external_path=r"D:\code\proj", provider="claude",
    )

    seen = {}

    async def _user_home(_node_id, _is_windows):
        seen["home"] = r"C:\Users\op"
        return r"C:\Users\op"

    class _Client:
        async def host_exec(self, node_id, command, **kwargs):
            seen["command"] = command
            return {"stdout": ""}

    async def _is_windows(_node_id):
        return False

    monkeypatch.setattr(nodes_service.nodes_service, "node_user_home", _user_home)
    monkeypatch.setattr(nodes_service, "get_local_node_client", lambda: _Client())
    monkeypatch.setattr(nodes_service.nodes_service, "node_is_windows", _is_windows)

    await nodes_service.nodes_service.list_environment_sessions(
        NODE_ID, env["id"], "claude", workdir=r"D:\code\proj"
    )

    assert seen.get("home") == r"C:\Users\op", seen.get("command", "")[:200]
    # 命令要真的以操作者 HOME 为根（不是空串、不是 envs/<id>）
    assert r"C:\Users\op" in seen["command"]
    assert "envs" not in seen["command"]


@pytest.mark.asyncio
async def test_node_user_home_wraps_powershell(tortoise_db, monkeypatch):
    """Windows 上取操作者 HOME 必须包 ``_powershell_command``。

    host_exec 默认走节点的本地 shell（Windows 上是 cmd.exe），裸发 PowerShell
    片段会被 cmd 当成路径解析，回 "The filename, directory name, or volume
    label syntax is incorrect."、stdout 为空——而这里又把异常和空输出都吞成空串，
    于是「节点没有 HOME」和「命令根本没跑对」看起来一模一样。实测踩过：不隔离
    环境的会话扫描拿不到兜底 HOME，列表恒空。
    """
    seen = {}

    class _Client:
        async def host_exec(self, node_id, command, **kwargs):
            seen["command"] = command
            return {"stdout": r"C:\Users\op"}

    monkeypatch.setattr(nodes_service, "get_local_node_client", lambda: _Client())

    home = await nodes_service.nodes_service.node_user_home(NODE_ID, True)
    assert home == r"C:\Users\op"
    # 必须走 -EncodedCommand（cmd.exe 里直接跑 PowerShell 语法必死）
    assert "-EncodedCommand" in seen["command"]


@pytest.mark.asyncio
async def test_node_user_home_returns_empty_on_failure(tortoise_db, monkeypatch):
    """节点连不上时给空串，调用方据此跳过扫描——不抛、不猜。"""

    class _Client:
        async def host_exec(self, node_id, command, **kwargs):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(nodes_service, "get_local_node_client", lambda: _Client())
    assert await nodes_service.nodes_service.node_user_home(NODE_ID, True) == ""


# ── 标题与平台会话名称（title / platform_name 两列的分权）───────────────────
#
# 对账只拥有 title（节点报什么写什么）；platform_name 的写者只有改名接口。
# 少了任何一边的保护，结果都是用户改的名字被下一次同步冲掉，或列表显示裸 uuid。


@pytest.mark.asyncio
async def test_sync_writes_last_activity_from_node_epoch(tortoise_db, monkeypatch):
    """节点报的 epoch（编辑器记录里最后一条的时间）写进 last_activity_at——
    列表展示的就是它，不是「行什么时候被对账过」（synced_at）。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    _patch_node_sessions(monkeypatch, [
        {"session_id": "s-1", "title": "a", "updated_at": 1790762400},
        {"session_id": "s-2", "title": "b"},  # 老扫描命令没给时间 → 留空
    ])
    result = await env_service.sync_environment_sessions(user_id, env["id"])
    by_id = {r["session_id"]: r for r in result["sessions"]}
    assert by_id["s-1"]["last_activity_at"] is not None
    assert by_id["s-1"]["last_activity_at"].startswith("2026")  # epoch → 可读时间
    assert by_id["s-2"]["last_activity_at"] is None

    # 列表接口也带得出来
    page = await env_service.list_environment_sessions(user_id, env["id"])
    listed = {r["session_id"]: r for r in page["sessions"]}
    assert listed["s-1"]["last_activity_at"] == by_id["s-1"]["last_activity_at"]


@pytest.mark.asyncio
async def test_list_sessions_paginates_by_offset(tortoise_db, monkeypatch):
    """列表分页：limit/offset 切片，total 是该环境总数（不受分页影响）。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    for i in range(5):
        await _seed_session(conn, f"s-{i}", env["id"])

    page1 = await env_service.list_environment_sessions(user_id, env["id"], limit=2, offset=0)
    assert len(page1["sessions"]) == 2
    assert page1["total"] == 5            # 总数不受分页影响

    page2 = await env_service.list_environment_sessions(user_id, env["id"], limit=2, offset=2)
    assert len(page2["sessions"]) == 2
    assert page2["total"] == 5

    # 两页不重叠、合起来是 5 条里的 4 条（不假设同秒写入的相对顺序）
    ids1 = {r["session_id"] for r in page1["sessions"]}
    ids2 = {r["session_id"] for r in page2["sessions"]}
    assert ids1 & ids2 == set()
    assert ids1 | ids2 <= {f"s-{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_sync_environment_covers_resources_and_sessions_together(tortoise_db, monkeypatch):
    """「同步数据」一次覆盖全量：下发技能/插件 + 重读清单 + 对账会话 + 记时间。

    用户口径：页面上只有一个同步按钮，拆成"资源/会话"两个入口让人自己拼不合理。
    这条锁住 sync_environment 的编排顺序与 last_synced_at 落库。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "gone", env["id"])

    # 节点报了一条新会话
    _patch_node_sessions(monkeypatch, [{"session_id": "fresh"}])

    # _resolved_specs 会被 resource_reference_service 走真链路（无引用 → 空集），
    # 节点 sync 也会因为没有控制面连接而失败——隔离档无技能时不调它（无技能/插件
    # 就跳过下发，这正是编排的一部分）。
    # 节点下发（隔离档即使期望集为空也要调——prune 语义：删掉最后一个技能时
    # 也得下发一次才能把它从节点上卸掉）。这里桩掉，测的是编排不是节点。
    synced: list[tuple] = []

    async def _fake_node_sync(node_id, env_id, skills, plugins):
        synced.append((node_id, env_id, skills, plugins))
        return {}
    monkeypatch.setattr(
        nodes_service.nodes_service, "sync_environment", _fake_node_sync)

    called = {"inventory": 0}

    async def _fake_inventory(user, env_id):
        called["inventory"] += 1
        return []
    monkeypatch.setattr(env_service, "refresh_environment_inventory", _fake_inventory)

    out = await env_service.sync_environment(user_id, env["id"], team_id="t")
    assert synced and synced[0][0] == NODE_ID      # 隔离档下发了（空集也下发）
    assert called["inventory"] == 1
    assert out["last_synced_at"] is not None        # 同步时间落库（概览 tab 显示它）

    # 会话也对账了：fresh 进来、gone 结束
    rows = (await env_service.list_environment_sessions(user_id, env["id"]))["sessions"]
    by_id = {r["session_id"]: r for r in rows}
    assert by_id["fresh"]["status"] == "running"
    assert by_id["gone"]["status"] == "ended"


@pytest.mark.asyncio
async def test_sync_writes_title_and_rename_survives_resync(tortoise_db, monkeypatch):
    """对账覆盖 title，但绝不碰 platform_name；display_name 平台名优先。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    _patch_node_sessions(monkeypatch, [{"session_id": "s-1", "title": "第一次的标题"}])
    first = await env_service.sync_environment_sessions(user_id, env["id"])
    by_id = {r["session_id"]: r for r in first["sessions"]}
    assert by_id["s-1"]["title"] == "第一次的标题"
    assert by_id["s-1"]["platform_name"] == ""
    assert by_id["s-1"]["display_name"] == "第一次的标题"   # 没改名 → 用同步来的

    renamed = await env_service.rename_environment_session(
        user_id, env["id"], "s-1", "我的名字"
    )
    assert renamed["platform_name"] == "我的名字"
    assert renamed["display_name"] == "我的名字"
    assert renamed["title"] == "第一次的标题"               # 底层标题还在

    # 节点后来报了个新标题：title 要跟上，platform_name 必须原地不动。
    _patch_node_sessions(monkeypatch, [{"session_id": "s-1", "title": "第二次的标题"}])
    second = await env_service.sync_environment_sessions(user_id, env["id"])
    by_id = {r["session_id"]: r for r in second["sessions"]}
    assert by_id["s-1"]["title"] == "第二次的标题"
    assert by_id["s-1"]["platform_name"] == "我的名字"
    assert by_id["s-1"]["display_name"] == "我的名字"


@pytest.mark.asyncio
async def test_rename_clearing_falls_back_to_title(tortoise_db, monkeypatch):
    """改名传空串 = 清除，回退显示同步来的标题——不是显示空字符串。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    _patch_node_sessions(monkeypatch, [{"session_id": "s-1", "title": "同步来的标题"}])
    await env_service.sync_environment_sessions(user_id, env["id"])
    await env_service.rename_environment_session(user_id, env["id"], "s-1", "自定义")
    cleared = await env_service.rename_environment_session(
        user_id, env["id"], "s-1", ""
    )
    assert cleared["platform_name"] == ""
    assert cleared["display_name"] == "同步来的标题"


@pytest.mark.asyncio
async def test_display_name_falls_back_without_title(tortoise_db, monkeypatch):
    """节点没给标题（旧输出/读不到）时兜底「会话 <短id>」——绝不裸 uuid。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    _patch_node_sessions(monkeypatch, [{"session_id": "01234567-rest", "title": ""}])
    result = await env_service.sync_environment_sessions(user_id, env["id"])
    row = result["sessions"][0]
    assert row["title"] == ""
    assert row["display_name"] == "会话 01234567"


@pytest.mark.asyncio
async def test_rename_refuses_a_session_from_another_environment(tortoise_db, monkeypatch):
    """改名按环境限权：别的环境的会话 id 不能借道这里改。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    mine = await env_service.create_environment(user_id, node_id=NODE_ID, name="mine")
    other = await env_service.create_environment(user_id, node_id=NODE_ID, name="other")
    await _seed_session(conn, "s-other", other["id"], status="ended")

    with pytest.raises(EnvironmentError) as exc:
        await env_service.rename_environment_session(
            user_id, mine["id"], "s-other", "越权"
        )
    assert exc.value.code == "not_found"


def test_clean_session_title():
    """TSV 生死线：标题里的 TAB/换行必须换成空格；占位标题按没有处理。"""
    from user_platform.environment_service import _clean_session_title as clean

    assert clean("  含\t制表\n换行  ") == "含 制表 换行"
    assert clean("x" * 500) == "x" * 200
    assert clean("") == ""
    assert clean("New session - 2026-05-12T14:59:45.372Z") == ""
    assert clean("new session") == ""          # 大小写不敏感
    assert clean("研究一下创空间") == "研究一下创空间"


def test_parse_editor_sessions_carries_the_title_column():
    """第 4 列是标题；旧 3 列输出按无标题；同 id 去重保留较新一条的标题。"""
    from user_platform.routes_nodes_files import _parse_editor_sessions as parse

    stdout = (
        "aaa\t1789000000\tD:\\code\\proj\t旧标题\n"
        "aaa\t1789000002\tD:\\code\\proj\t新标题\n"
        "bbb\t1789000001\tD:\\code\\proj\n"          # 旧客户端：只有 3 列
        "ccc\t1789000003\tD:\\code\\other\t别的项目\n"
    )
    rows = parse(stdout, "D:/code/proj")
    by_id = {r["session_id"]: r for r in rows}
    assert by_id["aaa"]["title"] == "新标题"
    assert by_id["bbb"]["title"] == ""
    assert "ccc" not in by_id                             # 别的项目被 workdir 筛掉


def test_parse_editor_sessions_carries_mode_and_effort_columns():
    """第 5/6 列是从会话记录读出的权限档与思考等级；思考等级归一（max→xhigh）。

    这两个值同步为任务时用来预填任务参数（不让用户重选）。旧 4 列输出按空处理。
    """
    from user_platform.routes_nodes_files import _parse_editor_sessions as parse

    stdout = (
        "aaa\t1789000000\tD:\\code\\proj\t标题\tacceptEdits\thigh\n"
        "bbb\t1789000001\tD:\\code\\proj\t标题\tbypassPermissions\tmax\n"   # max→xhigh
        "ccc\t1789000002\tD:\\code\\proj\t标题\n"                          # 旧 4 列：都空
    )
    by_id = {r["session_id"]: r for r in parse(stdout, "D:/code/proj")}
    assert by_id["aaa"]["mode"] == "acceptEdits"
    assert by_id["aaa"]["effort"] == "high"
    assert by_id["bbb"]["effort"] == "xhigh"       # claude 的 max 归一到平台词汇
    assert by_id["ccc"]["mode"] == "" and by_id["ccc"]["effort"] == ""


def test_normalize_effort_keeps_only_platform_vocabulary():
    """思考等级只认 low/medium/high/xhigh；max→xhigh；其它（含空）→ 空。"""
    from user_platform.routes_nodes_files import _normalize_effort

    assert _normalize_effort("max") == "xhigh"
    assert _normalize_effort("HIGH") == "high"
    assert _normalize_effort("medium") == "medium"
    assert _normalize_effort("bogus") == ""
    assert _normalize_effort("") == ""


@pytest.mark.asyncio
async def test_sync_persists_mode_and_effort_from_the_node_scan(tortoise_db, monkeypatch):
    """对账把扫描来的权限档/思考等级写进账本；空扫描值不覆盖已有值。

    权限档复用账本已有的 mode 列（控制面派发时也写），思考等级是新列 effort。
    两个都是「非空才写」——opencode 读不到/文件超限时留空，不该把用户已经看到的
    档位抹掉。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    _patch_node_sessions(monkeypatch, [
        {"session_id": "s-1", "mode": "acceptEdits", "effort": "high"},
        {"session_id": "s-2", "mode": "", "effort": ""},          # 读不到 → 不写
    ])
    await env_service.sync_environment_sessions(user_id, env["id"])

    row1 = await _fetch_ledger_row(env["id"], "s-1")
    assert row1["mode"] == "acceptEdits"
    assert row1["effort"] == "high"
    # 列表也把它们带出来（前端 adopt 弹框要预填）。
    sessions, _ = await env_service._read_env_sessions(env["id"])
    by_id = {s["session_id"]: s for s in sessions}
    assert by_id["s-1"]["mode"] == "acceptEdits" and by_id["s-1"]["effort"] == "high"
    assert by_id["s-2"]["mode"] == "" and by_id["s-2"]["effort"] == ""


@pytest.mark.asyncio
async def test_sync_does_not_blank_mode_when_the_scan_reports_none(tortoise_db, monkeypatch):
    """第二次对账没读到权限档时，不能把第一次读到的抹成空。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")

    _patch_node_sessions(monkeypatch, [{"session_id": "s-1", "mode": "plan", "effort": "low"}])
    await env_service.sync_environment_sessions(user_id, env["id"])
    # 第二次：扫描读不到（模拟文件超限/opencode），但行还在。
    _patch_node_sessions(monkeypatch, [{"session_id": "s-1", "mode": "", "effort": ""}])
    await env_service.sync_environment_sessions(user_id, env["id"])

    row = await _fetch_ledger_row(env["id"], "s-1")
    assert row["mode"] == "plan"          # 空扫描值没有覆盖
    assert row["effort"] == "low"


# ── 二次同步 = 采用成任务（adopt）───────────────────────────────────────────
#
# 第一段同步只搬元信息；要「编辑」某条会话必须给它平台身份（任务）。这里锁的是
# 采用的四条契约：payload 形状（续接提示、项目归属、空内容）、幂等、抢占释放、
# 无项目拒绝。create_task 太重，用假实现替身——测的是 adopt 自己的编排。


class _FakeTaskService:
    """替身：真建一行 Task（带上 source_editor_session_id），记下收到的 req。

    必须真建行：adopt 的幂等兜底是「账本 task_id 空了就按 Task.source_editor_session_id
    反查」——替身要是不落库，反查永远落空，测试就没锁到那条链路。
    """

    def __init__(self):
        self.calls: list[tuple[dict, bool]] = []
        # 每次 create_task 收到的 no_dispatch（adopt 免派发时是 True）。
        self.no_dispatch_calls: list[bool] = []
        self.error: Exception | None = None
        # True = 返回 {"id": ""}（不落库）：模拟「create 成功但没带回 id」，
        # adopt 内部走 EnvironmentError 分支——那是释放抢占的另一条路径。
        self.empty_id = False
        # adopt 把已导入历史灌进任务轨迹时收到的 (task_id, items)。
        self.seeded: list[tuple[str, list[dict]]] = []

    async def seed_history_from_items(self, task_id, items):
        self.seeded.append((str(task_id), list(items)))
        return len(items)

    async def create_task(self, user_id, req, *, defer_dispatch=False, no_dispatch=False):
        self.calls.append((dict(req), defer_dispatch))
        self.no_dispatch_calls.append(no_dispatch)
        if self.error is not None:
            raise self.error
        if self.empty_id:
            return {"id": "", "status": "pending"}
        from user_platform.models_task import Task

        row = await Task.create(
            id=uuid.uuid4(),
            user_id=uuid.UUID(user_id),
            kind="develop",
            title="占位",
            content=str(req.get("content") or ""),
            status="pending",
            provider=str(req.get("provider") or ""),
            node_id=str(req.get("node_id") or "") or None,
            workspace_key=f"task_{uuid.uuid4().hex}",
            source_editor_session_id=str(req.get("editor_session_id") or "") or None,
        )
        return {"id": str(row.id), "status": "pending"}


def _patch_task_service(monkeypatch) -> _FakeTaskService:
    import user_platform.task_service as ts_mod

    fake = _FakeTaskService()
    monkeypatch.setattr(ts_mod.task_service, "create_task", fake.create_task)
    monkeypatch.setattr(
        ts_mod.task_service, "seed_history_from_items", fake.seed_history_from_items
    )
    return fake


async def _adopt_target(monkeypatch, *, with_project=True):
    """建「环境 + 账本里的一条会话」，返回 (owner_id, env, fake_task_service)。

    必须带 ``provider``：adopt 会在 transcript 副本为空时先向节点导入一段历史再
    灌进任务轨迹，而导入要靠 provider 定位会话文件——没有 provider 就退化成
    「未知编辑器」，导入空转、任务轨迹也就空了。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    owner = str(uuid.uuid4())
    kwargs = {"provider": "claude"}
    if with_project:
        kwargs["project_id"] = str((await _make_project(owner)).id)
    env = await env_service.create_environment(owner, node_id=NODE_ID, name="e", **kwargs)
    await _seed_session(conn, "ext-1", env["id"])
    fake = _patch_task_service(monkeypatch)
    return owner, env, fake


@pytest.mark.asyncio
async def test_adopt_creates_a_waiting_task_with_resume_hint(tortoise_db, monkeypatch):
    """采用 payload 形状：空 content、editor_session_id、**免派发**、参数从两边来。

    与旧行为的两处关键变化（用户口径：同步时不做任何节点动作）：

    * 不再 ``defer_dispatch=True`` —— 改走 ``no_dispatch=True``，采用零节点往返
      （会话/工作区/运行时推迟到首条消息的懒派发）；
    * 不再把 ``resume_thread_id`` 放进 create req —— 它不落 config_snapshot，
      采用不派发就没用，改由懒派发从 ``source_editor_session_id`` 重建。
    * 会话模式/思考等级从**账本**取（不进请求体）；父 Key/模型/限制由用户选（进 req）。
    """
    owner, env, fake = await _adopt_target(monkeypatch)

    result = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1", parent_api_key_id=7, model_id="m-1"
    )
    assert result["reused"] is False
    assert fake.calls, "create_task 没被调用"
    req, defer = fake.calls[0]
    assert defer is False                  # 采用不派发（no_dispatch 才是它的语义）
    assert fake.no_dispatch_calls == [True]  # 零节点动作
    assert req["content"] == ""                # 空 = 交互式等待任务
    assert req["editor_session_id"] == "ext-1"
    assert req["env_id"] == env["id"]
    assert req["parent_api_key_id"] == 7
    assert req["model_id"] == "m-1"
    # 续接提示不再进 create req（用不上了），但 editor_session_id 是它重建的来源。
    assert "_session_tags" not in req
    # 模式/思考等级由服务端从账本填，不接受请求体里传。
    assert req["mode"] is None            # 账本该行没扫到权限档（_seed_session 不写）
    assert req["reasoning_effort"] is None
    assert req["extra"]["project_id"]          # 进项目任务列表的钥匙

    # 账本行已回填真实任务 id —— 再点一次是幂等返回（列表据此决定行点击是
    # 跳任务页还是再弹采用框）。
    again = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1", parent_api_key_id=7
    )
    assert again == {"task_id": result["task_id"], "reused": True}
    assert len(fake.calls) == 1            # 幂等：不重复建任务


@pytest.mark.asyncio
async def test_adopt_passes_user_params_and_reads_mode_effort_from_ledger(tortoise_db, monkeypatch):
    """采用的两类参数各走各的：用户选的进请求，会话模式/思考等级从账本读。

    权限档/思考等级是节点会话记录里读出来的（扫描时写进账本 mode/effort 列），
    采用时**从账本取**、不由用户选、也不进请求体；父 Key/模型映射/限制则由用户选。
    """
    owner, env, fake = await _adopt_target(monkeypatch)
    # 先让这条会话在账本里带上扫描来的 mode/effort（模拟同步时写入）。
    _patch_node_sessions(monkeypatch, [{
        "session_id": "ext-1", "title": "标题", "mode": "acceptEdits", "effort": "high",
    }])
    await env_service.sync_environment_sessions(owner, env["id"])

    result = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1",
        parent_api_key_id=7, model_id="m-1",
        models=["m-1", "m-2"], usage_limit={"max_requests": 5},
        rate_limit={"requests_per_minute": 10}, expires_at=1800000000,
        editor_provider_whitelist=["claude"], selection_strategy="intelligent",
    )
    assert result["reused"] is False
    req, _defer = fake.calls[0]
    # 从账本读的两个参数
    assert req["mode"] == "acceptEdits"
    assert req["reasoning_effort"] == "high"
    # 用户选的参数原样透传
    assert req["models"] == ["m-1", "m-2"]
    assert req["usage_limit"] == {"max_requests": 5}
    assert req["rate_limit"] == {"requests_per_minute": 10}
    assert req["expires_at"] == 1800000000
    assert req["editor_provider_whitelist"] == ["claude"]
    assert req["selection_strategy"] == "intelligent"


@pytest.mark.asyncio
async def test_redispatch_rebuilds_the_resume_thread_hint(tortoise_db):
    """重派发（懒派发/恢复）从 Task.source_editor_session_id 重建 resume 提示。

    这是「采用免派发」能成立的前提：resume 提示不落 config_snapshot，采用时不派发
    就没机会下发它；首条消息懒派发时必须从 Task 行把它补回来，否则节点开新会话、
    原上下文丢失。
    """
    from user_platform.models_task import Task
    from user_platform.task_service import _attach_resume_tag

    user_id = str(uuid.uuid4())
    adopted = await Task.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), kind="develop",
        title="t", content="", status="pending", provider="claude",
        workspace_key=f"task_{uuid.uuid4().hex}",
        source_editor_session_id="ext-uuid-1",
    )
    req: dict = {}
    _attach_resume_tag(req, adopted)
    assert req["_session_tags"]["resume_thread_id"] == "ext-uuid-1"

    # 不是采用来的任务：不加提示。
    plain = await Task.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), kind="develop",
        title="t", content="", status="pending", provider="claude",
        workspace_key=f"task_{uuid.uuid4().hex}",
    )
    req2: dict = {}
    _attach_resume_tag(req2, plain)
    assert "_session_tags" not in req2

    # 已有 tags 时合并而不是覆盖（workspace_mode 等由别处补，不能被抹掉）。
    req3: dict = {"_session_tags": {"workspace_mode": "worktree"}}
    _attach_resume_tag(req3, adopted)
    assert req3["_session_tags"] == {"workspace_mode": "worktree", "resume_thread_id": "ext-uuid-1"}


@pytest.mark.asyncio
async def test_adopt_is_idempotent_via_the_task_table(tortoise_db, monkeypatch):
    """账本 task_id 被清过（历史脏数据）时，靠 Task 表反查兜底，不重复建任务。"""
    owner, env, fake = await _adopt_target(monkeypatch)
    first = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1", parent_api_key_id=7
    )

    import db as db_module

    async with db_module.PostgresClient.pool.acquire() as conn:
        await conn.execute(
            "UPDATE mc_ac_node_sessions SET task_id='' WHERE session_id='ext-1'"
        )

    second = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1", parent_api_key_id=7
    )
    assert second == {"task_id": first["task_id"], "reused": True}
    assert len(fake.calls) == 1               # 没有第二次 create_task


@pytest.mark.asyncio
async def test_adopt_failure_releases_the_claim(tortoise_db, monkeypatch):
    """create_task 失败必须释放抢占，否则这条会话永远卡在占位符上。"""
    owner, env, fake = await _adopt_target(monkeypatch)
    fake.error = ValueError("parent_api_key_required")

    with pytest.raises(EnvironmentError):
        await env_service.adopt_environment_session(
            owner, env["id"], "ext-1", parent_api_key_id=7
        )

    # 释放后可以重试：这次成功（先把替身的报错关掉）。
    fake.error = None
    second = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1", parent_api_key_id=7
    )
    assert second["reused"] is False


@pytest.mark.asyncio
async def test_adopt_without_a_key_dispatches_keyless(tortoise_db, monkeypatch):
    """同步不逼用户选 Key：没给父 Key 就要放行无 Key 派发。

    否则 create_task 抛 parent_api_key_required，用户点了没反应——这条锁的就是
    「设为空」这个要求本身。
    """
    conn = await _fake_ledger_pool(monkeypatch)
    _owner, env, fake = await _adopt_target(monkeypatch)

    result = await env_service.adopt_environment_session(_owner, env["id"], "ext-1")

    assert result["reused"] is False
    req, _defer = fake.calls[0]
    assert req["parent_api_key_id"] is None
    assert req["_allow_keyless_dispatch"] is True
    # 选了 Key 时不该再开这个口子（守卫只对「留空」生效）
    await _seed_session(conn, "ext-2", env["id"])
    await env_service.adopt_environment_session(_owner, env["id"], "ext-2", parent_api_key_id=9)
    assert fake.calls[1][0]["_allow_keyless_dispatch"] is False


@pytest.mark.asyncio
async def test_batch_adopts_reports_per_session_outcome(tortoise_db, monkeypatch):
    """批量：一条失败（会话不在此环境）不能连累其余，页面要能分开报。"""
    owner, env, _fake = await _adopt_target(monkeypatch)
    conn = await _fake_ledger_pool(monkeypatch)
    await _seed_session(conn, "ext-2", env["id"])

    result = await env_service.adopt_environment_sessions(
        owner, env["id"], ["ext-1", "ext-2", "not-here"]
    )

    assert {a["session_id"] for a in result["adopted"]} == {"ext-1", "ext-2"}
    assert len({a["task_id"] for a in result["adopted"]}) == 2   # 两条各自一个任务
    assert [f["session_id"] for f in result["failed"]] == ["not-here"]
    assert result["failed"][0]["error"]


@pytest.mark.asyncio
async def test_adopt_refuses_an_environment_without_a_project(tortoise_db, monkeypatch):
    """任务必须归属项目——没有归属，「进项目任务列表」就无从谈起。"""
    _owner, env, _fake = await _adopt_target(monkeypatch, with_project=False)
    with pytest.raises(EnvironmentError) as exc:
        await env_service.adopt_environment_session(
            _owner, env["id"], "ext-1", parent_api_key_id=7
        )
    assert exc.value.code == "failed_precondition"



@pytest.mark.asyncio
async def test_adopt_releases_the_claim_on_internal_error(tortoise_db, monkeypatch):
    """EnvironmentError 分支的释放：create_task 没带回 id 时，抢占同样要放掉。

    释放有两条路径（ValueError 包装 / EnvironmentError 直抛），变异验证发现
    只测前者时后者的释放代码漏网——去掉它测试照样绿。这条补上那个洞。
    """
    owner, env, fake = await _adopt_target(monkeypatch)
    fake.empty_id = True

    with pytest.raises(EnvironmentError):
        await env_service.adopt_environment_session(
            owner, env["id"], "ext-1", parent_api_key_id=7
        )

    # 释放后可以重试：这次正常返回。
    fake.empty_id = False
    second = await env_service.adopt_environment_session(
        owner, env["id"], "ext-1", parent_api_key_id=7
    )
    assert second["reused"] is False


@pytest.mark.asyncio
async def test_adopt_seeds_the_task_conversation_with_imported_history(tortoise_db, monkeypatch):
    """「同步为任务」必须把已导入的历史灌进任务轨迹——否则任务页是空的。

    用户点「同步为任务」之前往往刚在环境详情看过对话；跳到任务页却一片空白，
    会被理解为"历史丢了"。这条钉住：adopt 时库里那份 transcript 副本被原样灌进
    任务的事件轨迹；库里还没有时先向节点导入一段再灌。
    """
    import json

    owner, env, fake = await _adopt_target(monkeypatch)

    _patch_transcript_node(monkeypatch, [
        json.dumps({
            "type": "assistant", "uuid": "u1", "timestamp": 1790762400,
            "message": {"content": [{"type": "text", "text": "历史消息"}]},
        }),
    ])
    await env_service.import_env_session_items(
        owner, env["id"], "ext-1", mode="tail", window_lines=10
    )

    result = await env_service.adopt_environment_session(owner, env["id"], "ext-1")
    assert result["reused"] is False
    assert fake.seeded, "seed_history_from_items 没被调用"
    seeded_task_id, seeded_items = fake.seeded[0]
    assert seeded_task_id == result["task_id"]
    assert [i["text"] for i in seeded_items] == ["历史消息"]


@pytest.mark.asyncio
async def test_adopt_imports_then_seeds_when_the_copy_is_empty(tortoise_db, monkeypatch):
    """库里还没这份历史时，adopt 先向节点要一段再灌——不让用户先手动同步一次。"""
    import json

    owner, env, fake = await _adopt_target(monkeypatch)
    box = _patch_transcript_node(monkeypatch, [
        json.dumps({
            "type": "assistant", "uuid": "u1", "timestamp": 1790762400,
            "message": {"content": [{"type": "text", "text": "临时拉的"}]},
        }),
    ])

    await env_service.adopt_environment_session(owner, env["id"], "ext-1")
    assert box["n"] >= 1                     # 确实去节点拉了一段
    assert [i["text"] for i in fake.seeded[0][1]] == ["临时拉的"]


@pytest.mark.asyncio
async def test_adopt_never_reseeds_a_task_that_already_has_history(tortoise_db, monkeypatch):
    """任务已经聊过就不许再灌——否则历史会被重复插进已有对话中间。

    「同步为任务」是幂等的（重复点返回同一任务），自愈补灌必须只在**空任务**上做。
    """
    import json

    owner, env, fake = await _adopt_target(monkeypatch)
    _patch_transcript_node(monkeypatch, [
        json.dumps({
            "type": "assistant", "uuid": "u1", "timestamp": 1790762400,
            "message": {"content": [{"type": "text", "text": "历史"}]},
        }),
    ])
    await env_service.import_env_session_items(
        owner, env["id"], "ext-1", mode="tail", window_lines=10
    )
    first = await env_service.adopt_environment_session(owner, env["id"], "ext-1")
    assert len(fake.seeded) == 1

    # 模拟用户在任务页发了一条消息 → 任务有对话了。
    from user_platform.models_task import TaskEvent

    await TaskEvent.create(
        id=uuid.uuid4(), task_id=uuid.UUID(first["task_id"]), seq=999,
        kind="item", event_type="user_input", logical_event_id="user:999",
        payload={"item": {"id": "user:999", "type": "user_input", "text": "我发的"}},
    )

    # 再点一次 adopt：幂等返回同一任务，且**不再**灌历史。
    again = await env_service.adopt_environment_session(owner, env["id"], "ext-1")
    assert again["reused"] is True
    assert again["task_id"] == first["task_id"]
    assert len(fake.seeded) == 1     # 没有第二次灌入


@pytest.mark.asyncio
async def test_delete_task_keeps_the_session_and_its_history_copy(tortoise_db, monkeypatch):
    """删任务**不删会话**：账本行、历史副本都留着，只清 task_id 这个引用。

    用户问的就是这个：会话是编辑器在节点上跑出来的事实，任务只是后来给它发的
    一个平台身份。删任务不该把会话连同它的历史副本一起带走——否则环境详情里
    那条会话就凭空消失了。清掉 task_id 后它回到「未同步」，可以重新同步。

    （直接跑 ``delete_task`` 会拽进 ReviewNodeLease 等一堆模型，这里只验证它
    最后那一步的清理 SQL 的性质——会话行和 transcript 都不动，只清 task_id。
    这条 SQL 就是这次改动的全部风险面。）
    """
    import json

    owner, env, _fake = await _adopt_target(monkeypatch)
    _patch_transcript_node(monkeypatch, [
        json.dumps({
            "type": "assistant", "uuid": "u1", "timestamp": 1790762400,
            "message": {"content": [{"type": "text", "text": "历史"}]},
        }),
    ])
    await env_service.import_env_session_items(owner, env["id"], "ext-1", mode="tail", window_lines=10)
    adopted = await env_service.adopt_environment_session(owner, env["id"], "ext-1")
    task_id = adopted["task_id"]

    # 模拟 delete_task 的清理那一步：清掉账本里指向这个任务的 task_id。
    import db as db_module

    async with db_module.PostgresClient.pool.acquire() as conn:
        await conn.execute(
            "UPDATE mc_ac_node_sessions SET task_id='' WHERE task_id=$1", task_id
        )

    # 会话行还在，历史副本还在，只有 task_id 引用被清掉。
    ledger = await _fetch_ledger_row(env["id"], "ext-1")
    assert ledger is not None, "会话账本行被删了——删任务不该删会话"
    assert str(ledger["task_id"] or "").strip() == ""
    copy = await env_service._read_transcript_store(env["id"], "ext-1")
    assert [i["text"] for i in copy["items"]] == ["历史"]


@pytest.mark.asyncio
async def test_adopt_recreates_when_the_cached_task_is_gone(tortoise_db, monkeypatch):
    """账本缓存指向一个已删除的任务 → 清掉死缓存，重新采用出新任务。

    否则那条会话永远显示「已同步」、点「打开任务」404，再点同步还复用死 id。
    """
    from user_platform.models_task import Task

    owner, env, fake = await _adopt_target(monkeypatch)
    # 账本里塞一个指向不存在任务的缓存（_adopt_target 已建好会话行 ext-1）。
    import db as db_module

    dead = str(uuid.uuid4())
    async with db_module.PostgresClient.pool.acquire() as conn:
        await conn.execute(
            "UPDATE mc_ac_node_sessions SET task_id=$1 WHERE session_id=$2",
            dead, "ext-1",
        )

    result = await env_service.adopt_environment_session(owner, env["id"], "ext-1")
    assert result["reused"] is False            # 不再复用死 id
    assert result["task_id"] != dead
    live = await Task.get_or_none(id=uuid.UUID(result["task_id"]))
    assert live is not None                     # 新任务真的存在
    # 账本缓存也换成了新任务。
    ledger = await _fetch_ledger_row(env["id"], "ext-1")
    assert str(ledger["task_id"]) == result["task_id"]


@pytest.mark.asyncio
async def test_seed_history_writes_task_event_rows(tortoise_db, monkeypatch):
    """真种子方法：把 NormalizedItem 落成 kind='item' 的 TaskEvent 行，
    形状与运行时事件一致（envelope 包裹 + canonical 字段透传）。

    ``_next_event_seq`` 走 PG 序列，sqlite 上没有 ``nextval``——按单调计数器打桩，
    这样测的是**行形状**（本用例的目的），序列本身有它自己的保证。
    """
    from user_platform.models_task import Task, TaskEvent
    from user_platform.task_service import task_service as real_ts

    counter = {"n": 0}

    async def _seq(_task_id):
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(real_ts, "_next_event_seq", _seq)

    user_id = str(uuid.uuid4())
    task = await Task.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), kind="develop",
        title="t", content="", status="pending", provider="claude",
        workspace_key=f"task_{uuid.uuid4().hex}",
    )

    items = [
        {"id": "env:aaa", "type": "user_input", "text": "问题",
         "logical_event_id": "env:aaa", "created_at": 1790762400},
        {"id": "toolu_1", "type": "tool_call", "title": "Bash", "tool_name": "Bash",
         "logical_event_id": "toolu_1", "event_kind": "tool", "phase": "complete",
         "status": "done", "output": "ok", "created_at": 1790762401},
        {"id": "env:bbb", "type": "agent_message", "text": "回答",
         "logical_event_id": "env:bbb", "event_kind": "message", "phase": "complete",
         "status": "done", "model": "claude-x", "created_at": 1790762402},
    ]
    written = await real_ts.seed_history_from_items(task.id, items)
    assert written == 3

    rows = await TaskEvent.filter(task_id=task.id).order_by("seq")
    assert len(rows) == 3
    seqs = [r.seq for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3   # 顺序 = 导入顺序
    # kind='item'（内联 payload，不进 ClickHouse）；canonical 字段透传。
    assert all(r.kind == "item" for r in rows)
    tool_row = rows[1]
    assert tool_row.logical_event_id == "toolu_1"
    payload = tool_row.payload
    assert payload["item"]["tool_name"] == "Bash"
    assert payload["event_kind"] == "tool" and payload["phase"] == "complete"
    # 读路径吃的形状：payload.item 是 NormalizedItem 本体。
    assert payload["item"]["type"] == "tool_call"
    # created_at 用条目自身时间戳（1790762401 = 2026-09-30T10:00:01Z），不是"现在"
    # ——否则整段历史在任务页都显示"刚刚"。列是朴素 TIMESTAMP，按朴素值比，
    # 别调 .timestamp()（那会把朴素值当本地时区解释，UTC+8 下必然偏 8 小时）。
    assert rows[1].created_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-30 10:00:01"


@pytest.mark.asyncio
async def test_seeded_history_is_renderable_by_the_task_page(tortoise_db, monkeypatch):
    """端到端：种子写进去的行，经 ``list_task_events``（任务页真读路径）原样吐回，
    形状就是前端 ``rowToItem``/``reduceItems`` 吃的那份——否则任务页照样空白。

    这条把"行写进去了"和"页面能渲染"之间那环补上：以前只验了写入，没验读出。
    """
    from user_platform.models_task import Task
    from user_platform.task_service import task_service as real_ts

    counter = {"n": 0}

    async def _seq(_task_id):
        counter["n"] += 1
        return counter["n"]

    monkeypatch.setattr(real_ts, "_next_event_seq", _seq)

    user_id = str(uuid.uuid4())
    task = await Task.create(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), kind="develop",
        title="t", content="", status="pending", provider="claude",
        workspace_key=f"task_{uuid.uuid4().hex}",
    )

    items = [
        {"id": "env:q", "type": "user_input", "text": "问题",
         "logical_event_id": "env:q", "created_at": 1790762400},
        {"id": "env:a", "type": "agent_message", "text": "回答",
         "logical_event_id": "env:a", "event_kind": "message", "phase": "complete",
         "status": "done", "model": "claude-x", "created_at": 1790762402},
    ]
    await real_ts.seed_history_from_items(task.id, items)

    # 任务页的真读路径——list_task_events 是 /events/history 背后的那个调用。
    page = await real_ts.list_task_events(user_id, str(task.id), limit=50)
    rows = page["rows"]
    assert len(rows) == 2
    # oldest-first（list_task_events 内部反转过），顺序 = 种子顺序。
    assert [r["payload"]["item"]["text"] for r in rows] == ["问题", "回答"]
    # 前端 rowToItem 只认这几样：payload.item（NormalizedItem）、
    # logical_event_id（id 兜底）、created_at（ISO 串，给消息时间）。
    for r in rows:
        assert isinstance(r["payload"], dict)
        assert isinstance(r["payload"].get("item"), dict)
        assert r["logical_event_id"]
        assert r["created_at"]            # ISO 串，不是 None
        assert r["kind"] == "item"        # 内联 payload，不进 ClickHouse
    # 工具卡合并的前提：id 稳定。两条消息各自唯一、互不撞。
    assert len({r["logical_event_id"] for r in rows}) == 2


# ── 环境资源：节点为准、单列表、操作即下发 ─────────────────────────────────────
#
# 用户口径："数据本身就是以节点为准，服务器只是一个副本，所有操作都是下发到节点
# 去处理的，服务器只是显示使用。" 曾有配置行(extras)/节点快照两块，现在合成一份：
# 隔离档 = 配置行 + 节点状态，不隔离档 = 系统环境那份（同一个 HOME 同一份数据）。

from user_platform.models_environment import (  # noqa: E402
    NodeSystemEnvEntry,
    TaskEnvironmentInstalled,
    TaskEnvironmentResource,
)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_detail_isolated_is_one_list_without_extras(tortoise_db):
    """隔离档：一个列表；extras 字段整个没了。状态按节点扫描 diff 出来。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await TaskEnvironmentResource.create(
        id=uuid.uuid4(), env_id=env["id"], kind="skill",
        resource_id=uuid.uuid4(), name="sk-1", version="1.0")
    await TaskEnvironmentResource.create(
        id=uuid.uuid4(), env_id=env["id"], kind="skill",
        resource_id=uuid.uuid4(), name="sk-2", version="")
    await TaskEnvironmentResource.create(
        id=uuid.uuid4(), env_id=env["id"], kind="mcp",
        resource_id=uuid.uuid4(), name="mcp-1", version="")
    # 节点只报了 sk-1：sk-2 是 pending（还没装上），mcp 永远是 config
    await TaskEnvironmentInstalled.create(
        id=uuid.uuid4(), env_id=env["id"], kind="skill", name="sk-1",
        version="1.0", reported_at=_utcnow())

    detail = await env_service.environment_detail(user_id, env["id"])
    assert "extras" not in detail
    states = {r["name"]: r["state"] for r in detail["resources"]}
    assert states == {"sk-1": "installed", "sk-2": "pending", "mcp-1": "config"}


@pytest.mark.asyncio
async def test_detail_external_reads_the_node_truth(tortoise_db, monkeypatch):
    """不隔离档：详情直接读系统环境那份（NodeSystemEnvEntry），不养第二份快照；
    removable 按 platform_managed——操作者本机装的只能看不能删。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e",
        workspace_mode="external", external_path="D:/code/x")

    async def _fake_node_detail(_user, _node):
        return {"resources": {
            "skill": [{"id": "e1", "name": "本机技能", "version": "", "platform_managed": False}],
            "plugin": [{"id": "e2", "name": "平台插件", "version": "2.0", "platform_managed": True}],
            "mcp": [],
        }}
    monkeypatch.setattr(
        "user_platform.system_env_service.system_env_detail", _fake_node_detail)

    detail = await env_service.environment_detail(user_id, env["id"])
    by_name = {r["name"]: r for r in detail["resources"]}
    assert by_name["平台插件"]["removable"] is True
    assert by_name["本机技能"]["removable"] is False
    assert all(r["state"] == "installed" for r in detail["resources"])


@pytest.mark.asyncio
async def test_add_isolated_writes_row_and_dispatches_now(tortoise_db, monkeypatch):
    """隔离档添加 = 写配置行 + **立即**下发（不再有"待同步"中间态）；
    MCP 是配置不是文件，没有下发步骤。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    rid = uuid.uuid4()

    async def _fake_visible(_user, _team, _kind):
        return [{"id": str(rid), "name": "技能A", "display_name": "技能A", "version": "1.0"}]
    monkeypatch.setattr(
        "user_platform.resource_reference_service.visible_references", _fake_visible)

    dispatched: list[str] = []

    async def _fake_dispatch(user, env_id, *, team_id, request_base_url=""):
        dispatched.append(env_id)
    monkeypatch.setattr(env_service, "_dispatch_and_refresh", _fake_dispatch)

    out = await env_service.add_environment_resource(
        user_id, env["id"], kind="skill", resource_id=str(rid), team_id="t")
    assert out["name"] == "技能A"
    assert dispatched == [env["id"]]

    mid = uuid.uuid4()

    async def _fake_visible_mcp(_user, _team, _kind):
        return [{"id": str(mid), "name": "m", "display_name": "m", "version": ""}]
    monkeypatch.setattr(
        "user_platform.resource_reference_service.visible_references", _fake_visible_mcp)
    await env_service.add_environment_resource(
        user_id, env["id"], kind="mcp", resource_id=str(mid), team_id="t")
    assert dispatched == [env["id"]]   # MCP 没触发下发


@pytest.mark.asyncio
async def test_add_external_installs_to_system_env(tortoise_db, monkeypatch):
    """不隔离档添加 = 走系统环境安装（同一个 HOME），不写环境配置行；
    MCP 在不隔离档没有下发通道（节点不写操作者 ~/.mcp.json），明确拒绝。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e",
        workspace_mode="external", external_path="D:/code/x")
    captured: dict = {}

    async def _fake_install(user, node_id, *, kind, resource_id, overwrite, team_id,
                            request_base_url=""):
        captured.update(node_id=node_id, kind=kind, resource_id=resource_id)
        return []
    monkeypatch.setattr("user_platform.system_env_service.install_to_system_env", _fake_install)

    rid = uuid.uuid4()
    await env_service.add_environment_resource(
        user_id, env["id"], kind="skill", resource_id=str(rid), team_id="t")
    assert captured == {"node_id": NODE_ID, "kind": "skill", "resource_id": str(rid)}
    assert await TaskEnvironmentResource.filter(env_id=env["id"]).count() == 0

    with pytest.raises(EnvironmentError):
        await env_service.add_environment_resource(
            user_id, env["id"], kind="mcp", resource_id=str(rid), team_id="t")


@pytest.mark.asyncio
async def test_remove_isolated_deletes_row_and_prunes(tortoise_db, monkeypatch):
    """隔离档删除 = 删配置行 + 立即下发 prune（节点按期望集把文件卸掉）。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    row = await TaskEnvironmentResource.create(
        id=uuid.uuid4(), env_id=env["id"], kind="plugin",
        resource_id=uuid.uuid4(), name="p-1", version="")
    dispatched: list[str] = []

    async def _fake_dispatch(user, env_id, *, team_id, request_base_url=""):
        dispatched.append(env_id)
    monkeypatch.setattr(env_service, "_dispatch_and_refresh", _fake_dispatch)

    out = await env_service.remove_environment_resource(user_id, env["id"], str(row.id))
    assert out["name"] == "p-1"
    assert dispatched == [env["id"]]
    assert await TaskEnvironmentResource.filter(id=row.id).count() == 0


@pytest.mark.asyncio
async def test_remove_external_uses_node_entry(tortoise_db, monkeypatch):
    """不隔离档的 entry_id 是 NodeSystemEnvEntry 的行，删除走系统环境卸载。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e",
        workspace_mode="external", external_path="D:/code/x")
    entry = await NodeSystemEnvEntry.create(
        id=uuid.uuid4(), node_id=NODE_ID, kind="skill", name="本机技能",
        platform_managed=True, reported_at=_utcnow())
    calls: dict = {}

    async def _fake_remove(user, node_id, *, kind, name):
        calls.update(node_id=node_id, kind=kind, name=name)
        return {"removed": True}
    monkeypatch.setattr("user_platform.system_env_service.remove_from_system_env", _fake_remove)

    out = await env_service.remove_environment_resource(user_id, env["id"], str(entry.id))
    assert calls == {"node_id": NODE_ID, "kind": "skill", "name": "本机技能"}
    assert out["deleted"] is True

    # 别的节点的行不能借这个环境删掉
    other = await NodeSystemEnvEntry.create(
        id=uuid.uuid4(), node_id="node-elsewhere", kind="skill", name="x",
        reported_at=_utcnow())
    with pytest.raises(EnvironmentError):
        await env_service.remove_environment_resource(user_id, env["id"], str(other.id))


@pytest.mark.asyncio
async def test_refresh_inventory_external_routes_to_system_env(tortoise_db, monkeypatch):
    """刷新副本按档位分流：不隔离档刷的是系统环境那份，不是环境快照。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="e",
        workspace_mode="external", external_path="D:/code/x")
    refreshed: list[str] = []

    async def _fake_refresh(user, node_id, **_kwargs):
        refreshed.append(node_id)
        return []
    monkeypatch.setattr("user_platform.system_env_service.refresh_system_env", _fake_refresh)

    await env_service.refresh_environment_inventory(user_id, env["id"])
    assert refreshed == [NODE_ID]


# ── 会话列表过滤 + 单会话同步 + 历史翻页 ───────────────────────────────────────
#
# 列表要能筛（编辑器/状态/关键词，服务端筛才不把分页数错位）；详情页的「同步」
# 只更新这一条；历史往上翻靠 skip_lines 把窗口整体往前挪，一直能翻到第一条。


@pytest.mark.asyncio
async def test_list_sessions_filters_by_provider_status_and_query(tortoise_db, monkeypatch):
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "s-claude", env["id"])
    await _seed_session(conn, "s-codex", env["id"], status="ended")
    # 标题靠对账写进来（_seed_session 只给空串）
    await conn.execute_query(
        "UPDATE mc_ac_node_sessions SET provider='codex', title='修个 bug' WHERE session_id='s-codex'"
    )

    by_provider = await env_service.list_environment_sessions(
        user_id, env["id"], provider="claude")
    assert [r["session_id"] for r in by_provider["sessions"]] == ["s-claude"]
    assert by_provider["total"] == 1          # 总数是**筛过之后**的，页脚才对得上

    by_status = await env_service.list_environment_sessions(
        user_id, env["id"], status="ended")
    assert [r["session_id"] for r in by_status["sessions"]] == ["s-codex"]

    by_query = await env_service.list_environment_sessions(
        user_id, env["id"], query="bug")
    assert [r["session_id"] for r in by_query["sessions"]] == ["s-codex"]

    # 关键词也能命中会话 id
    by_id = await env_service.list_environment_sessions(
        user_id, env["id"], query="claude")
    assert [r["session_id"] for r in by_id["sessions"]] == ["s-claude"]


@pytest.mark.asyncio
async def test_sync_one_session_updates_only_that_row(tortoise_db, monkeypatch):
    """详情页「同步」：只动这一条（标题/状态/最后活动时间），别的不碰。"""
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "s-1", env["id"])
    await _seed_session(conn, "s-2", env["id"])

    _patch_node_sessions(monkeypatch, [
        {"session_id": "s-1", "title": "新标题", "updated_at": 1790762400},
    ])
    out = await env_service.sync_environment_session(user_id, env["id"], "s-1")
    assert out["title"] == "新标题"
    assert out["status"] == "running"
    assert out["last_activity_at"] is not None

    # s-2 不在节点上报里，但**单条同步不该顺手把它标结束**（那是全量对账的事）
    rows = (await env_service.list_environment_sessions(user_id, env["id"]))["sessions"]
    by_id = {r["session_id"]: r for r in rows}
    assert by_id["s-2"]["status"] == "running"


@pytest.mark.asyncio
async def test_sync_one_session_marks_ended_when_node_dropped_it(tortoise_db, monkeypatch):
    conn = await _fake_ledger_pool(monkeypatch)
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e")
    await _seed_session(conn, "s-gone", env["id"])
    _patch_node_sessions(monkeypatch, [])   # 节点已经不报它了

    out = await env_service.sync_environment_session(user_id, env["id"], "s-gone")
    assert out["status"] == "ended"
    assert await env_service.list_environment_sessions(user_id, env["id"]) is not None


# 历史翻页的旧契约（读接口自己连节点、靠 tail_lines 窗口增长）已被
# "详情永远先读库" 取代，等价的语义由 test_transcript_history_loads_older_and_reports_head
# 与 test_transcript_pages_the_copy_by_cursor 钉住。


async def _fake_env(*_a, **_k):
    class _Row:
        id = "env-1"
        node_id = NODE_ID
        provider = "claude"
        workspace_mode = "shared"
        name = "e"
        external_path = None
        project_id = None
    return _Row()


async def _win(*_a, **_k):
    return True


async def _home(*_a, **_k):
    return r"C:\Users\op"


# ── 环境会话后台同步 worker ──────────────────────────────────────────────────
#
# 固定间隔（非 cron）把每个环境的会话副本对账到节点。测试覆盖两处有判断的逻辑：
# 「哪些会话该抓内容」（纯函数）与「一轮怎么编排」（stub 掉服务层调用）。


def test_active_session_ids_picks_running_and_recently_ended():
    from datetime import datetime, timedelta, timezone
    from user_platform.session_sync_worker import _active_session_ids
    now = datetime.now(timezone.utc)
    interval = 300
    recent = (now - timedelta(seconds=interval)).isoformat()
    old = (now - timedelta(days=3)).isoformat()
    sessions = [
        {"session_id": "running-1", "status": "running", "last_activity_at": old},
        {"session_id": "just-ended", "status": "ended", "last_activity_at": recent},
        {"session_id": "long-ended", "status": "ended", "last_activity_at": old},
        {"session_id": "no-ts", "status": "ended", "last_activity_at": None},
        {"session_id": "", "status": "running"},
    ]
    picked = _active_session_ids(sessions, interval)
    assert picked == ["running-1", "just-ended"]
    assert "long-ended" not in picked
    assert "" not in picked


@pytest.mark.asyncio
async def test_worker_round_reconciles_and_imports_active_sessions(tortoise_db, monkeypatch):
    from user_platform import session_sync_worker as worker
    import db as db_module
    monkeypatch.setattr(db_module.PostgresClient, "pool", object())  # 池存在（服务层已 stub）
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(user_id, node_id=NODE_ID, name="e", provider="claude")
    off = await env_service.create_environment(user_id, node_id=NODE_ID, name="off", provider="claude")
    await env_service.update_environment(user_id, off["id"], enabled=False)
    reconcile_calls = []
    import_calls = []
    async def _reconcile(uid, eid, **_kw):
        reconcile_calls.append(eid)
        return {"sessions": [
            {"session_id": "s-running", "status": "running", "last_activity_at": None},
            {"session_id": "s-old", "status": "ended", "last_activity_at": "2020-01-01T00:00:00+00:00"},
        ], "total": 2, "changed": 0}
    async def _import(uid, eid, sid, **_kw):
        import_calls.append((eid, sid))
        return {"before": [], "after": [], "total": 0, "head": True, "window_lines": 0, "unsupported": False}
    monkeypatch.setattr(env_service, "sync_environment_sessions", _reconcile)
    monkeypatch.setattr(env_service, "import_env_session_items", _import)
    await worker._run_once(300)
    assert reconcile_calls == [env["id"]]
    assert import_calls == [(env["id"], "s-running")]


@pytest.mark.asyncio
async def test_worker_round_survives_a_failing_environment(tortoise_db, monkeypatch):
    from user_platform import session_sync_worker as worker
    import db as db_module
    monkeypatch.setattr(db_module.PostgresClient, "pool", object())
    user_id = str(uuid.uuid4())
    await env_service.create_environment(user_id, node_id=NODE_ID, name="a", provider="claude")
    await env_service.create_environment(user_id, node_id=NODE_ID, name="b", provider="claude")
    calls = []
    async def _reconcile(uid, eid, **_kw):
        calls.append(eid)
        if len(calls) == 1:
            raise RuntimeError("node offline")
        return {"sessions": [], "total": 0, "changed": 0}
    monkeypatch.setattr(env_service, "sync_environment_sessions", _reconcile)
    await worker._run_once(300)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_update_environment_persists_session_sync_interval(tortoise_db):
    """概览 tab 的定时同步三态：0=关闭，N=专用间隔，不传=不改。"""
    user_id = str(uuid.uuid4())
    env = await env_service.create_environment(
        user_id, node_id=NODE_ID, name="interval-env", provider="claude"
    )
    row = await env_service.get_environment(user_id, env["id"])
    assert row.session_sync_interval is None  # 新环境跟随全局默认

    updated = await env_service.update_environment(
        user_id, env["id"], session_sync_interval=0
    )
    assert updated["session_sync_interval"] == 0

    updated = await env_service.update_environment(
        user_id, env["id"], session_sync_interval=60
    )
    assert updated["session_sync_interval"] == 60

    # 不传 = 不改（_UNSET sentinel），不能把 0 / None 混成一个意思。
    updated = await env_service.update_environment(user_id, env["id"], name="renamed")
    assert updated["name"] == "renamed"
    assert updated["session_sync_interval"] == 60

    # 显式 null = 恢复跟随全局默认（路由按 model_fields_set 决定传不传）。
    updated = await env_service.update_environment(
        user_id, env["id"], session_sync_interval=None
    )
    assert updated["session_sync_interval"] is None


def test_effective_interval_mapping():
    """NULL=全局默认，0=关闭，N=专用间隔。"""
    from user_platform.session_sync_worker import _effective_interval

    assert _effective_interval(None, 300) == 300   # 跟随默认
    assert _effective_interval(0, 300) == 0        # 本环境关闭
    assert _effective_interval(60, 300) == 60      # 本环境专用（可比默认更短）
    assert _effective_interval(-5, 300) == 0       # 非法负数按关闭处理


@pytest.mark.asyncio
async def test_worker_round_respects_per_env_interval(tortoise_db, monkeypatch):
    """配了间隔的环境：第一轮跑，第二轮没到点要跳过；配 0 的环境不跑。"""
    from user_platform import session_sync_worker as worker
    import db as db_module
    monkeypatch.setattr(db_module.PostgresClient, "pool", object())
    worker._last_run.clear()
    user_id = str(uuid.uuid4())
    every = await env_service.create_environment(user_id, node_id=NODE_ID, name="every", provider="claude")
    quiet = await env_service.create_environment(user_id, node_id=NODE_ID, name="quiet", provider="claude")
    await env_service.update_environment(user_id, every["id"], session_sync_interval=0)
    calls: list[str] = []

    async def _reconcile(uid, eid, **_kw):
        calls.append(eid)
        return {"sessions": [], "total": 0, "changed": 0}

    monkeypatch.setattr(env_service, "sync_environment_sessions", _reconcile)
    try:
        # 第一轮：enabled 的两个环境都跑（配 0 的跳过）。
        await worker._run_once(300)
        assert calls == [quiet["id"]]

        # 第二轮：没到点（间隔 300 > 0 没过），quiet 不再跑。
        calls.clear()
        await worker._run_once(300)
        assert calls == []

        # 关掉 quiet（=0）后：仍然不跑，且不产生新调度记录。
        await env_service.update_environment(user_id, quiet["id"], session_sync_interval=0)
        calls.clear()
        await worker._run_once(300)
        assert calls == []
    finally:
        worker._last_run.clear()


# ---------------------------------------------------------------------------
# 模型映射（model_mapping）：规范化校验 + 派发翻译
# ---------------------------------------------------------------------------

def test_normalize_model_mapping_claude_roles():
    """claude 映射：main=档名时不进平台模型校验值（它由 _normalize 单独判定）；
    slots 里合法模型收口、空值剔除。"""
    from user_platform.task_service import _mapping_model_values

    raw = {
        "main": "opus",
        "slots": {
            "sonnet": {"model": "gpt-5.2"},
            "opus": {"model": ""},
            "haiku": {"model": "haiku-4-5"},
            "fable": {"model": None},
        },
    }
    values = _mapping_model_values("claude", raw)
    # claude main=档名（opus）不进校验值，只剩槽里非空的：gpt-5.2, haiku-4-5
    assert values == ["gpt-5.2", "haiku-4-5"]


def test_migrate_old_claude_mapping_to_main_slots():
    """旧形状 roles/fallback → {main, slots}：main 取档名（不取 fallback 兜底名）。"""
    from user_platform.task_service import _migrate_model_mapping

    migrated = _migrate_model_mapping(
        "claude",
        {"fallback": "opus-callback", "roles": {"sonnet": "sonnet", "opus": "opus"}},
    )
    # 主模型是档名 opus（不是 fallback 的 opus-callback）
    assert migrated["main"] == "opus"
    assert migrated["slots"] == {"sonnet": {"model": "sonnet"}, "opus": {"model": "opus"}}


@pytest.mark.asyncio
async def test_normalize_model_mapping_rejects_model_outside_key(monkeypatch):
    """映射里的模型不在父 Key 可执行集合内 → 报错（映射必须显式正确）。"""
    from user_platform import task_service

    async def _fake_validate(key_id, models, active_model=None):
        # 真实实现只返回合法项；"not-allowed" 不在集合内 → 返回空，长度对不上即报错。
        kept = [m for m in models if m == "gpt-5.2"]
        return kept, (kept[0] if kept else (active_model or ""))

    monkeypatch.setattr(task_service, "_validate_models_for_key", _fake_validate)
    with pytest.raises(ValueError, match="model_not_available_for_key"):
        await task_service._normalize_model_mapping("claude", {"main": "not-allowed", "slots": {}}, 7)


@pytest.mark.asyncio
async def test_normalize_model_mapping_list_providers_shape(monkeypatch):
    """codex 目录 / opencode 模型表 / dsh 模型表 / gemini 单模型各自收口。"""
    from user_platform import task_service

    async def _fake_validate(key_id, models, active_model=None):
        # 与真实实现同口径：只留合法项，并按首次出现去重。
        legal = ["m1", "m2"]
        kept: list[str] = []
        for m in models:
            if m in legal and m not in kept:
                kept.append(m)
        return kept, (kept[0] if kept else (active_model or ""))

    monkeypatch.setattr(task_service, "_validate_models_for_key", _fake_validate)

    codex = await task_service._normalize_model_mapping(
        "codex",
        {
            "main": "m1",
            "slots": {
                "Fast": {"model": "m1"},
                "m2": {"model": "m2"},
            },
        },
        7,
    )
    assert codex == {"main": "m1", "slots": {"Fast": {"model": "m1"}, "m2": {"model": "m2"}}}

    oc = await task_service._normalize_model_mapping(
        "opencode", {"main": "m1", "slots": {"M": {"model": "m1"}}}, 7,
    )
    assert oc == {"main": "m1", "slots": {"M": {"model": "m1"}}}

    gem = await task_service._normalize_model_mapping("gemini", {"main": "m2", "slots": {}}, 7)
    assert gem == {"main": "m2", "slots": {}}
    # 空/None 一律 None（当没配）
    assert await task_service._normalize_model_mapping("claude", None, 7) is None


@pytest.mark.asyncio
async def test_apply_model_mapping_extra_keys_by_provider():
    """_apply_model_mapping 把映射翻译成节点 extra 键（claude 角色/codex catalog/dsh 列表）。"""
    from types import SimpleNamespace
    from user_platform.task_service import TaskService

    svc = TaskService.__new__(TaskService)

    task = SimpleNamespace(id="t1", provider="claude")
    extra: dict = {}
    model = await TaskService._apply_model_mapping(
        svc, task,
        {"main": "opus", "slots": {"sonnet": {"model": "m-son"}, "haiku": {"model": "m-hai"}}},
        "sk-test", extra, "m-default",
    )
    # 主模型 = 档名 opus（下发成 ANTHROPIC_MODEL），不再是 fallback 兜底
    assert model == "opus"
    assert extra["ANTHROPIC_MODEL"] == "opus"
    assert extra["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "m-son"
    assert extra["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "m-hai"
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in extra  # 没配的档不写

    task2 = SimpleNamespace(id="t2", provider="codex")
    extra2: dict = {}
    model2 = await TaskService._apply_model_mapping(
        svc, task2,
        {"main": "m-fb", "slots": {"Fast": {"model": "m1"}}},
        "sk-test", extra2, "m-default",
    )
    assert model2 == "m-fb"
    catalog = json.loads(extra2["CODEX_MODEL_CATALOG"])
    assert catalog == [{"name": "Fast", "model": "m1", "context_window": 0}]

    task3 = SimpleNamespace(id="t3", provider="dsh")
    extra3: dict = {}
    await TaskService._apply_model_mapping(
        svc, task3,
        {"main": "m-fb", "slots": {"M1": {"model": "m1"}, "M2": {"model": "m2"}}},
        "sk-test", extra3, "m-default",
    )
    rows = json.loads(extra3["DSH_MODELS"])
    assert [r["id"] for r in rows] == ["m1", "m2"]
    assert rows[0]["contextWindow"] == 0  # 元数据取不到时 0，节点侧回落默认

    # opencode：/model 菜单（OPENCODE_MODELS）+ OMO 槽表（OPENCODE_AGENT_CONFIG）。
    # OMO 自己就是 agents / categories 两张独立表，一起进 OPENCODE_AGENT_CONFIG。
    task5 = SimpleNamespace(id="t5", provider="opencode")
    extra5: dict = {}
    await TaskService._apply_model_mapping(
        svc, task5,
        {"main": "m-fb", "slots": {
            "sisyphus": {"model": "m1", "fallback_models": ["m2"]},
            "explore": {"model": "m2"},
        }, "categories": {
            "quick": {"model": "m2"},
        }},
        "sk-test", extra5, "m-default",
    )
    oc_models = json.loads(extra5["OPENCODE_MODELS"])
    assert oc_models["m1"]["name"] == "sisyphus"
    assert oc_models["m2"]["name"] == "quick"  # 两表的模型都进菜单
    omo = json.loads(extra5["OPENCODE_AGENT_CONFIG"])
    # OMO 槽值带 agent-compose/ provider 前缀，与节点写 opencode.json 的 provider id 一致。
    assert omo["agents"]["sisyphus"]["model"] == "agent-compose/m1"
    assert omo["agents"]["sisyphus"]["fallback_models"] == ["agent-compose/m2"]
    assert omo["agents"]["explore"]["model"] == "agent-compose/m2"
    assert omo["categories"]["quick"]["model"] == "agent-compose/m2"
    # agents / categories 是分开的两张表，不合并。
    assert "quick" not in omo["agents"]
    assert "sisyphus" not in omo["categories"]

    task4 = SimpleNamespace(id="t4", provider="gemini")
    extra4: dict = {}
    model4 = await TaskService._apply_model_mapping(
        svc, task4, {"main": "m2", "slots": {}}, "sk-test", extra4, "m-default"
    )
    assert model4 == "m2" and extra4 == {}


# ---------------------------------------------------------------------------
# 创建任务配置缓存（TaskCreateDefaults）：读写 + 兜底必填
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_defaults_roundtrip(tortoise_db):
    """缓存按 (用户, 项目, 编辑器) 存；同键再写是覆盖，不是插新行。"""
    from user_platform.task_service import task_service

    user_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())

    assert await task_service.get_create_defaults(user_id, project_id, "claude") == {}

    saved = await task_service.save_create_defaults(
        user_id, project_id, "claude",
        {"parent_api_key_id": 7, "reasoning_effort": "high", "fallback": "m1"},
    )
    assert saved["parent_api_key_id"] == 7

    # 读回一致
    got = await task_service.get_create_defaults(user_id, project_id, "claude")
    assert got["reasoning_effort"] == "high"

    # 同键覆盖（不是新增一行）
    await task_service.save_create_defaults(user_id, project_id, "claude", {"reasoning_effort": "low"})
    got2 = await task_service.get_create_defaults(user_id, project_id, "claude")
    assert got2 == {"reasoning_effort": "low"}

    from user_platform.models_task import TaskCreateDefaults
    rows = await TaskCreateDefaults.filter(user_id=uuid.UUID(user_id))
    assert len(rows) == 1

    # 不同 provider 是另一份
    await task_service.save_create_defaults(user_id, project_id, "codex", {"reasoning_effort": "medium"})
    assert (await task_service.get_create_defaults(user_id, project_id, "codex"))["reasoning_effort"] == "medium"
    assert (await task_service.get_create_defaults(user_id, project_id, "claude"))["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_create_defaults_no_project_uses_zero_uuid(tortoise_db):
    """无项目用零 UUID 占位：两次写同一份，不会因 NULL 不相等而插两行。"""
    from user_platform.task_service import task_service
    from user_platform.models_task import TaskCreateDefaults

    user_id = str(uuid.uuid4())
    await task_service.save_create_defaults(user_id, None, "claude", {"a": 1})
    await task_service.save_create_defaults(user_id, None, "claude", {"a": 2})
    rows = await TaskCreateDefaults.filter(user_id=uuid.UUID(user_id))
    assert len(rows) == 1
    assert (await task_service.get_create_defaults(user_id, None, "claude"))["a"] == 2


@pytest.mark.asyncio
async def test_model_mapping_fallback_required(monkeypatch):
    """主模型必填：codex 缺 main → 抛 model_mapping_fallback_required；claude 缺 main 默认落 opus 档。"""
    from user_platform import task_service

    async def _fake_validate(key_id, models, active_model=None):
        # 允许 opus（claude 默认档名）+ m1/m2（平台模型）。
        kept = [m for m in models if m in ("opus", "sonnet", "m1", "m2")]
        return kept, (kept[0] if kept else (active_model or ""))

    monkeypatch.setattr(task_service, "_validate_models_for_key", _fake_validate)

    # codex 没有 main（老 fallback 缺席）→ 必填报错
    with pytest.raises(ValueError, match="model_mapping_fallback_required"):
        await task_service._normalize_model_mapping("codex", {"catalog": [{"name": "N", "model": "m1"}]}, 7)
    # claude 没给 main 时默认落 opus 档（用户口径：claude 主模型就是 opus 档）
    ok_claude = await task_service._normalize_model_mapping("claude", {"roles": {"sonnet": "m1"}}, 7)
    assert ok_claude == {"main": "opus", "slots": {"sonnet": {"model": "m1"}}}

    # 有 main 就通过，且 slots 原样保留
    ok = await task_service._normalize_model_mapping(
        "claude", {"main": "sonnet", "slots": {"sonnet": {"model": "m2"}}}, 7
    )
    assert ok == {"main": "sonnet", "slots": {"sonnet": {"model": "m2"}}}
    # gemini 单模型即 main：非法值抛错；空串=没配映射（返回 None，由前端门禁拦）
    assert await task_service._normalize_model_mapping("gemini", {"main": "m1", "slots": {}}, 7) == {"main": "m1", "slots": {}}
    assert await task_service._normalize_model_mapping("gemini", {"main": "", "slots": {}}, 7) is None
    with pytest.raises(ValueError, match="model_not_available_for_key"):
        await task_service._normalize_model_mapping("gemini", {"main": "not-in-key", "slots": {}}, 7)


# ── task_stats：最新一条消息的用量，不是累计 ──────────────────────────────────

@pytest.mark.asyncio
async def test_task_stats_returns_latest_message_usage_not_cumulative(tortoise_db):
    """stats 端点返回**最新一条**带 usage 的消息，不是任务累计。

    口径（用户 2026-10-11）：用户看这个数想知道「这轮上下文用了多少」，累计值
    跑得越久越大、与当前这轮无关。断言取到的是最后一条（seq 最大的），不是求和。
    """
    from user_platform.models_task import Task, TaskEvent

    user_id = uuid.uuid4()
    task = await Task.create(
        id=uuid.uuid4(), user_id=user_id, kind="task", content="hi",
        provider="claude", status="pending",
    )
    # 三条 assistant 帧，各自带 usage；只有最后一条该被返回。
    for seq, (inp, out) in enumerate([(100, 10), (200, 20), (300, 30)], start=1):
        await TaskEvent.create(
            id=uuid.uuid4(), task_id=task.id, seq=seq,
            kind="item", event_type="agent_message", logical_event_id=f"m{seq}",
            payload={"item": {
                "id": f"m{seq}", "type": "agent_message", "text": "x",
                "usage": {"input": inp, "output": out, "cache_read": 5,
                          "cache_creation": 1, "total": inp + out + 6},
                "model": "claude-opus-5",
            }},
        )
    # 尾部一条没有 usage（工具调用）——必须跳过它继续往前找。
    await TaskEvent.create(
        id=uuid.uuid4(), task_id=task.id, seq=4,
        kind="item", event_type="tool_call", logical_event_id="t4",
        payload={"item": {"id": "t4", "type": "tool_call", "title": "Bash"}},
    )

    service = task_service_module.TaskService()
    stats = await service._latest_message_usage(task.id)
    assert stats["input_tokens"] == 300
    assert stats["output_tokens"] == 30
    assert stats["cache_read_tokens"] == 5
    assert stats["total_tokens"] == 336
    assert stats["model"] == "claude-opus-5"


@pytest.mark.asyncio
async def test_task_stats_empty_when_no_usage_anywhere(tortoise_db):
    """没有任何带 usage 的帧 → 空对象（前端显示「暂无」，不编造 0 累计）。"""
    from user_platform.models_task import Task, TaskEvent

    task = await Task.create(
        id=uuid.uuid4(), user_id=uuid.uuid4(), kind="task", content="hi",
        provider="claude", status="pending",
    )
    await TaskEvent.create(
        id=uuid.uuid4(), task_id=task.id, seq=1,
        kind="item", event_type="user_input", logical_event_id="u1",
        payload={"item": {"id": "u1", "type": "user_input", "text": "hi"}},
    )
    service = task_service_module.TaskService()
    assert await service._latest_message_usage(task.id) == {}
