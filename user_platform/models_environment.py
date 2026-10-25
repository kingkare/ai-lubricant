"""Tortoise models for user-owned task execution environments.

An *environment* is what a user picks before creating a task. It decides which
HOME the editor runs against, and (for the shared tier) carries a reusable set
of skill/MCP/plugin references so a user does not re-pick them per task.

Three tiers exist, but only ONE of them needs a database row:

* ``system``   — the editor runs against the node operator's real HOME, using
  whatever the operator already installed there. No environment row: there is
  nothing to *configure*. What the node observes there IS recorded, per node, in
  :class:`NodeSystemEnvEntry` — otherwise a system-env task's real capability set
  would stay invisible to both the server and the user.
* ``isolated`` — a throwaway per-session HOME. This is the *initial* environment
  (a clean slate every run), so there is nothing to manage or sync.
* ``shared``   — a named, persistent HOME reused across tasks. This is the only
  tier with a configured resource set here: it has an id, a user-facing name, and
  a resource list the user maintains over time.

Two truth sources, on purpose
-----------------------------
:class:`TaskEnvironment` + :class:`TaskEnvironmentResource` are the *desired*
state (what the user configured, shown and edited server-side).
:class:`TaskEnvironmentInstalled` mirrors what a node actually reported on disk
for that environment. Neither can be derived from the other — disk carries no
``resource_id``/manifest version, and the ledger cannot know what someone
installed by hand from a maintenance shell — so the environment page diffs them
to show 已安装 / 待安装 / 多余.

Credentials are deliberately absent
-----------------------------------
MCP entries store only a *reference*, never a token. Skills and plugins are
files that live in the environment HOME; MCP is config, so it is never
materialized into a shared HOME at all. Each task mints its own MCP token from
its own principal at dispatch time and writes it to that task's private
``stateRoot`` — so two tasks sharing an environment never share or overwrite
each other's credentials.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# Environment tiers. Only ``shared`` has rows in these tables; the other two are
# resolved directly from the tier name at dispatch time.
ENV_TIER_SYSTEM = "system"
ENV_TIER_ISOLATED = "isolated"
ENV_TIER_SHARED = "shared"
ENV_TIERS = (ENV_TIER_SYSTEM, ENV_TIER_ISOLATED, ENV_TIER_SHARED)

# Resource kinds an environment can carry. Skills/plugins are files synced into
# the environment HOME; MCP is config resolved per task (never written there).
ENV_RESOURCE_SKILL = "skill"
ENV_RESOURCE_MCP = "mcp"
ENV_RESOURCE_PLUGIN = "plugin"
ENV_RESOURCE_KINDS = (ENV_RESOURCE_SKILL, ENV_RESOURCE_MCP, ENV_RESOURCE_PLUGIN)

# How a session's working tree is materialized. ``shared`` is the historic
# per-session clone; ``worktree`` shares one checkout across the environment's
# tasks. Kept as plain strings (not a DB enum) so a node that predates the
# feature simply ignores the tag it does not know.
WORKSPACE_MODE_SHARED = "shared"
WORKSPACE_MODE_WORKTREE = "worktree"

# ── provision_state：平台管理的代码目录（clone）备好了没有 ────────────────────
# clone 属于**建环境**这一侧（异步跑、页面可见、失败可重试）；任务派发只切
# worktree 不 clone，非 ready 的环境直接拒——环境没备好就不该接任务。
PROVISION_READY = "ready"
PROVISION_PROVISIONING = "provisioning"
PROVISION_FAILED = "failed"
PROVISION_STATES = (PROVISION_READY, PROVISION_PROVISIONING, PROVISION_FAILED)
WORKSPACE_MODE_EXTERNAL = "external"
# 「新建」：目录是全新的，由节点 clone 到用户指定的路径。与 ``external``（导入 =
# 用节点上已有的目录）构成同一个概念的两面——见 :func:`resolve_isolated`。
WORKSPACE_MODE_FRESH = "fresh"
WORKSPACE_MODES = (
    WORKSPACE_MODE_SHARED,
    WORKSPACE_MODE_WORKTREE,
    WORKSPACE_MODE_EXTERNAL,
    WORKSPACE_MODE_FRESH,
)


def resolve_isolated(workspace_mode: str) -> bool:
    """这个环境是否隔离，由目录是谁的直接决定——不是第二个独立选项。

    环境就是「一个工作目录 + 一套编辑器配置」，目录的归属决定了配置的归属：

    * ``fresh``（新建）—— 目录由我们 clone 出来，配置也给我们自己一套（隔离）。
    * ``external``（导入）—— 目录是节点操作者自己的 checkout，配置跟着他的全局
      走（不隔离）；把操作者的代码配成一套独立配置才别扭。
    * ``shared``/``worktree``（历史形态）—— 平台托管的目录，配置也归环境（隔离）。

    单一真相源：建环境时不必传隔离，读环境时也不必再问。字段本身保留只为历史
    数据，任何新写入都以本函数为准。
    """
    mode = str(workspace_mode or "").strip().lower()
    if mode == WORKSPACE_MODE_EXTERNAL:
        return False
    return mode != ""  # shared / worktree / fresh 都隔离

# Editor providers an environment can be built for. Mirrors the node runtime's
# runner set; an environment is "one editor's landing on one node", so this is
# what decides which CLI actually runs and which config paths it reads.
# 与节点 runner 集合一致（nodes/runtime/javascript/src/runners）与前端
# SUPPORTED_EDITORS（nodes/types.ts）同口径——少一个就会让该编辑器的环境建不出来。
ENV_PROVIDERS = ("claude", "codex", "gemini", "opencode", "cursor", "dsh")


class TaskEnvironment(Model):
    """One user-owned environment: a project's landing on one node.

    An environment is the durable half of the old "shared tier": it owns the
    working tree, the resource set, and the git credential scope, while sessions
    come and go on top of it. ``node_id`` pins it to the node whose disk holds
    the checkout — the resource set is portable data, but an installed toolchain
    and a cloned repo are not.

    ``project_id`` is what the environment is *of*. It is nullable only so rows
    predating this field keep working; an environment without a project has no
    repository, which is exactly the pre-project behavior.

    ``enabled`` is the lifecycle gate, and it is enforced live rather than by
    invalidating anything: the git proxy checks this row on every request (see
    ``node_server.git_proxy._resolve``), so disabling an environment revokes its
    git access immediately and re-enabling restores it — no token reissue.
    """

    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField()
    node_id = fields.CharField(max_length=128)
    # The project this environment is a landing of. NULL = no repository bound
    # (legacy rows, and environments used only for their HOME/resources).
    project_id = fields.UUIDField(null=True)
    name = fields.CharField(max_length=255)
    description = fields.TextField(null=True)
    # Lifecycle gate. False revokes this environment's git credential and
    # refuses new sessions; the checkout on disk is left alone.
    enabled = fields.BooleanField(default=True)
    # How a session's working tree is materialized on the node:
    #   ``shared``   — the session clones its own tree (the historic behavior).
    #   ``worktree`` — the environment holds ONE durable checkout and each task
    #                  gets a git worktree cut from it, so the object database
    #                  (and the clone transfer) is paid for once per environment
    #                  instead of once per task.
    #   ``external`` — the environment adopts a directory that already exists on
    #                  the node (``external_path``); sessions run directly in it
    #                  and nothing is cloned. Only meaningful together with
    #                  ``external_path``; mutually exclusive with ``worktree``,
    #                  which would have to fetch into the operator's tree.
    # ``shared``/``worktree`` need a bound project; ``external`` does not (the
    # tree is already there, carrying its own git state).
    workspace_mode = fields.CharField(max_length=16, default=WORKSPACE_MODE_SHARED)
    # Set when the environment's working tree is a directory the node did NOT
    # create (a path the operator already had). Delete then unbinds instead of
    # removing the tree — deleting a user's own checkout would be data loss.
    external_path = fields.TextField(null=True)
    # The editor this environment runs: claude / codex / opencode. Empty for
    # legacy rows (predating the field); an environment without a provider is
    # resolved by the task at dispatch, but the environment page can only manage
    # a single editor's config, so newly created rows always carry one.
    provider = fields.CharField(max_length=32, default="")
    # Whether the editor's own HOME/config (skills, plugins, login state) is
    # isolated into the environment's own directory (true) or runs against the
    # node operator's real HOME (false).
    #
    # **Derived, never chosen**: a stored copy of :func:`resolve_isolated`'s
    # answer, written at create time so old rows stay readable. Every read path
    # recomputes it (the field exists for the historic three-tier data, which
    # stored the tier instead of the reason).
    isolated = fields.BooleanField(default=True)
    # Same-host submodule paths the project's ``.gitmodules`` declares, in
    # ``owner/repo.git`` form. Derived once at bind time and read by the git
    # proxy to decide whether an ``r/<path>`` request is a submodule this
    # environment's repo actually declared — the proxy never trusts a
    # node-supplied list. Mirrors ``mc_tasks.config_snapshot.submodule_paths``.
    submodule_paths = fields.JSONField(default=list)
    # Per-environment init commands (dependency links, config copies). Run once
    # when the working tree is first prepared; see the plan doc §5.
    init_commands = fields.JSONField(default=list)
    # 平台管理的代码目录（worktree 模式的 envs/<id>/repo、fresh 模式的 external_path
    # clone）备好了没有。clone 是**建环境**这一侧的事、异步跑：
    #   provisioning — clone 在途（建环境后、重试后）
    #   ready        — 目录可用（external/shared 没有可准备的，建好即 ready）
    #   failed       — clone 失败，原因在 provision_error，可重试
    # 派发闸门按它判定：非 ready 的环境不接任务（节点只切不 clone，见
    # prepareWorktree）——clone 属于环境，任务路径不再兜底。
    provision_state = fields.CharField(max_length=16, default="ready")
    provision_error = fields.TextField(null=True)
    provisioned_at = fields.DatetimeField(null=True)
    # Bumped on every resource-set edit. The node reports back the revision it
    # last synced so the page can say "配置已改，待同步".
    revision = fields.BigIntField(default=0)
    synced_revision = fields.BigIntField(default=0)
    last_synced_at = fields.DatetimeField(null=True)
    # Last sync error text (redacted), surfaced on the environment page so a
    # failed install is visible instead of silently missing.
    last_sync_error = fields.TextField(null=True)
    # 本环境的会话后台同步间隔（秒）。NULL = 跟随全局默认
    # （``settings.env_session_sync_interval``）；0 = 本环境关闭定时同步；
    # >0 = 本环境专用间隔（覆盖全局）。纯显示副本的对账节奏，改动立即生效，
    # 不影响节点、不影响手动「同步数据」按钮。
    session_sync_interval = fields.IntField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_task_environments"
        # 名字不再唯一：id 才是身份（节点目录按 id 派生），名字只是标签。曾经的
        # (user_id, node_id, name) 唯一约束会跟「按项目过滤的列表」打架——用户会被
        # 一个自己看不见的环境挡住（旧行的 project_id 为空，在任何项目页都不出现）。
        # 历史索引由启动迁移删除（见 database._migrate_environment_defaults）。
        indexes = (("user_id",), ("node_id",), ("project_id",))


class TaskEnvironmentResource(Model):
    """A skill/MCP/plugin the user added to an environment (desired state).

    Stores a *reference* (``resource_id`` into ``mc_resource_references``), not a
    resolved wire spec — the same rule tasks already follow. Resolution to a full
    spec (and, for MCP, minting a token) happens per dispatch, so no credential
    is ever persisted here.
    """

    id = fields.UUIDField(pk=True)
    env_id = fields.UUIDField()
    kind = fields.CharField(max_length=16)
    resource_id = fields.UUIDField()
    # Display snapshot so the page can render without re-resolving every row.
    name = fields.CharField(max_length=255, default="")
    version = fields.CharField(max_length=64, default="")
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mc_task_environment_resources"
        unique_together = (("env_id", "kind", "resource_id"),)
        indexes = (("env_id", "kind"),)


class TaskEnvironmentInstalled(Model):
    """What a node last reported as physically present in an environment HOME.

    This is the *observed* side of the diff. It is replaced wholesale by each
    inventory report, so a row here means "the node saw this on disk at
    ``reported_at``" — including things installed by hand, which show up as
    extras with no matching :class:`TaskEnvironmentResource`.

    MCP never appears here: it is not a file, so there is nothing on disk to
    inventory.
    """

    id = fields.UUIDField(pk=True)
    env_id = fields.UUIDField()
    kind = fields.CharField(max_length=16)
    # Directory name on the node, which is what the runtime activates by name.
    name = fields.CharField(max_length=255)
    version = fields.CharField(max_length=64, default="")
    reported_at = fields.DatetimeField()

    class Meta:
        table = "mc_task_environment_installed"
        unique_together = (("env_id", "kind", "name"),)
        indexes = (("env_id",),)


class NodeSystemEnvEntry(Model):
    """One resource a node reported present in its operator's real HOME.

    The system tier (``env_mode="system"``) has no environment row — it *is* the
    operator's HOME — so this table is keyed by ``node_id`` rather than
    ``env_id``: one node, one operator HOME. It exists because the providers
    already discover whatever the operator installed there (claude reads
    ``~/.claude/skills``, others read ``~/.agents/{skills,plugins}``, gemini
    ``~/.gemini/extensions``, MCP ``~/.mcp.json``), yet none of it was visible to
    the server — a system-env task silently got capabilities nobody could see.

    Replaced wholesale by each inventory report, like
    :class:`TaskEnvironmentInstalled`: a row means "the node saw this at
    ``reported_at``".

    ``platform_managed`` separates what we installed from what the operator did.
    It is the authorization boundary for removal: the node refuses to delete
    anything absent from its own manifest, so an operator's hand-installed
    resource can be shown but never removed through the API.

    Unlike the shared-tier table this DOES carry MCP rows — display only, because
    writing the operator's MCP config would leak per-task credentials into a
    directory other things read.

    Identity is (node_id, kind, provider, name): the node reports MCP entries
    keyed (provider, name) on purpose — the same server name in two editors'
    configs is two capabilities, because each runtime loads its own definition
    — so ``provider`` must be part of the unique key or such a report dies on
    insert. File kinds can never collide across providers (the node merges
    same-name files into one row), so the wider key costs nothing there.
    """

    id = fields.UUIDField(pk=True)
    node_id = fields.CharField(max_length=128)
    kind = fields.CharField(max_length=16)  # skill | plugin | mcp
    name = fields.CharField(max_length=255)
    version = fields.CharField(max_length=64, default="")
    # Provider whose discovery path this was found under — i.e. which editor's
    # own config/directory declared it. Empty only for the .agents standard tree
    # (no single owner); ~/.mcp.json is claude's project-level config, so MCP
    # rows always carry an editor here.
    provider = fields.CharField(max_length=32, default="")
    # Editors that would actually LOAD this entry in a system-env session,
    # comma-joined ("" for rows reported by a node predating the field). The
    # node owns this fact — it knows the scan targets and the runtime read
    # rules — and the console groups its per-editor tabs by it.
    readers = fields.CharField(max_length=64, default="")
    # HOME-relative path, so the console can explain where an entry came from.
    path = fields.CharField(max_length=512, default="")
    # Best-effort human description (SKILL.md frontmatter / plugin manifest);
    # empty is normal — MCP configs carry none.
    description = fields.CharField(max_length=1024, default="")
    platform_managed = fields.BooleanField(default=False)
    # Set once a local resource has been archived into the platform library, so
    # the UI can show 已入库 and other nodes/environments can reuse it.
    archived_reference_id = fields.UUIDField(null=True)
    reported_at = fields.DatetimeField()

    class Meta:
        table = "mc_node_system_env_entries"
        unique_together = (("node_id", "kind", "provider", "name"),)
        indexes = (("node_id",),)
