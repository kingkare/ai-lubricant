"""User-owned task execution environments.

An *environment* is what a user picks before creating a task. It decides which
HOME the editor runs against, and — for the shared tier — carries a reusable set
of skill/MCP/plugin references the user maintains over time.

Three tiers, one table
----------------------
``system`` (the node operator's real HOME) and ``isolated`` (a throwaway
per-session HOME) need no row: they are resolved from the tier name alone.
``isolated`` in particular is the *initial* environment — a clean slate every
run — so there is nothing to manage or sync. Only ``shared`` has rows here.

Why two truth sources
---------------------
The configured resource set (this module's tables) is the desired state; what a
node reports on disk is the observed state. Neither derives from the other:
disk carries no ``resource_id``/manifest version, and the ledger cannot know
what an operator installed by hand from a maintenance shell. :func:`environment_detail`
diffs them so the page can label every entry 已安装 / 待安装 / 多余.

Install vs. activate
--------------------
Editing an environment's resources is a real install/uninstall on the node
(:func:`sync_environment`). A *task* removing a resource is only a per-run
activation change — it never touches the environment's files, so one task
dropping a skill cannot uninstall it for other tasks on the same environment.

No credentials, ever
--------------------
MCP entries hold only a reference. A task mints its own MCP token from its own
principal at dispatch and the node writes it to that task's private state root,
so environments never store, and shared HOMEs never see, a credential.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from .models_environment import (
    ENV_PROVIDERS,
    ENV_RESOURCE_KINDS,
    ENV_RESOURCE_MCP,
    ENV_RESOURCE_PLUGIN,
    ENV_RESOURCE_SKILL,
    ENV_TIER_ISOLATED,
    ENV_TIER_SHARED,
    ENV_TIER_SYSTEM,
    ENV_TIERS,
    PROVISION_FAILED,
    PROVISION_PROVISIONING,
    PROVISION_READY,
    WORKSPACE_MODE_EXTERNAL,
    WORKSPACE_MODE_FRESH,
    WORKSPACE_MODE_SHARED,
    WORKSPACE_MODE_WORKTREE,
    WORKSPACE_MODES,
    resolve_isolated,
    TaskEnvironment,
    TaskEnvironmentInstalled,
    TaskEnvironmentResource,
)
from .models_project import Project

# 用于「客户端没碰这个字段」与「客户端显式把它设成 null」的区分。PATCH 语义：
# 字段缺席 ≠ 字段为 null（前者不改，后者是「跟随全局默认」）。
_UNSET = object()


class EnvironmentError(Exception):
    """Domain failure with a stable code the routes map onto an HTTP status."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _uuid(value: str, *, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise EnvironmentError("invalid_argument", f"{field} 不是合法 UUID")


def _is_absolute_node_path(value: str) -> bool:
    """A node-side absolute path, on either platform.

    The value decides a working directory on the node's disk, so a relative
    path (resolved against whatever the node's cwd happens to be) or one that
    escapes via ``..`` must be refused here, not discovered by the session.
    Windows paths are ``D:\\...``; POSIX paths start with ``/``. The virtual
    ``/D:/...`` form the file browser uses is accepted too.
    """
    raw = (value or "").strip()
    if not raw or "\x00" in raw:
        return False
    posixish = raw.replace("\\", "/")
    if any(part == ".." for part in posixish.split("/")):
        return False
    if posixish.startswith("/"):
        if re.match(r"^/[A-Za-z]:/", posixish):  # virtual Windows form /C:/...
            return True
        return not raw.startswith("//")  # UNC stays invalid in the virtual form
    return bool(re.match(r"^[A-Za-z]:[/\\]", raw))  # D:\... or D:/...


def node_native_path(value: str) -> str:
    """Convert a node-side path into the form the node's own check accepts.

    ``external_path`` is stored in the virtual form the file browser and repo
    scan speak (``/D:/code/x``). The node decides whether to adopt a directory
    with ``filepath.IsAbs``, which on Windows is **false** for the virtual form
    (a leading slash makes it drive-relative) — so the node silently ignored the
    adoption tag and fell back to a per-task clone, which is exactly the
    "session pulls the repo again" bug. Strip the virtual leading slash so the
    tag carries a path the node accepts (``D:/code/x``).

    POSIX paths (``/home/x``) and already-native Windows paths (``D:\\x``) pass
    through unchanged: only the ``/X:/...`` virtual form is rewritten. Pair with
    :func:`_is_absolute_node_path`, which validates the input first.
    """
    raw = (value or "").strip()
    if re.match(r"^/[A-Za-z]:", raw):
        return raw[1:]
    return raw


async def _read_checkout_branch(node_id: str, path: str) -> str:
    """读节点上某个 checkout **实际**所在的分支（导入环境的「当前分支」显示）。

    用 ``host_exec`` 跑 ``git -C <path> rev-parse --abbrev-ref HEAD``，不新增 proto、
    不需要重打包节点。节点离线 / 目录不是 git 仓库 / 命令失败都返回空串——这只是
    详情页的一个展示字段，不该因为读不到就让环境详情打不开。
    """
    if not node_id or not _is_absolute_node_path(path):
        return ""
    from .node_client import get_local_node_client

    client = get_local_node_client()
    try:
        # Windows 用 PowerShell 原生命令，POSIX 直接 git；路径经 shlex/引号处理。
        from .routes_nodes_files import _powershell_command, _ps_path_assignment
        import shlex

        if str(path).startswith("/") and re.match(r"^/[A-Za-z]:/", path.replace("\\", "/")):
            # 虚拟 Windows 形式 /C:/... → 原生 C:\...
            native = path.replace("/", "\\").lstrip("\\")
            command = _powershell_command(
                _ps_path_assignment(native, "p")
                + "& git -C $p rev-parse --abbrev-ref HEAD 2>$null"
            )
        elif re.match(r"^[A-Za-z]:[/\\]", path):
            native = path.replace("/", "\\")
            command = _powershell_command(
                _ps_path_assignment(native, "p")
                + "& git -C $p rev-parse --abbrev-ref HEAD 2>$null"
            )
        else:
            command = f"git -C {shlex.quote(path)} rev-parse --abbrev-ref HEAD 2>/dev/null"
        result = await client.host_exec(
            node_id, command, cwd="", timeout_ms=15_000, max_output_bytes=8192
        )
    except Exception as exc:  # noqa: BLE001 — 展示字段，读不到就留空
        logger.info("[env] branch read failed for {}:{}: {}", node_id, path, exc)
        return ""
    out = str(result.get("stdout") or "").strip().splitlines()
    branch = out[-1].strip() if out else ""
    # detached HEAD 时 git 返回 "HEAD"，不是有意义的显示值。
    return "" if branch in ("", "HEAD") else branch


def normalize_tier(value: str | None) -> str:
    """Normalize a tier name, defaulting to ``isolated``.

    Empty means isolated on purpose: a caller that never picked a tier gets the
    clean-slate behaviour, which is also what every pre-environment task did.
    """
    tier = str(value or "").strip().lower() or ENV_TIER_ISOLATED
    if tier not in ENV_TIERS:
        raise EnvironmentError("invalid_argument", f"不支持的环境类型 {tier}")
    return tier


def _env_dict(row: TaskEnvironment) -> dict[str, Any]:
    # 工作区模式先归一，再拿它推隔离——两个字段必须同源。曾经这里一个兜
    # ``shared``、另一个兜空串，于是老行（workspace_mode 为 NULL）会显示成
    # 「shared」却被算成「不隔离」，选中它时前端把档位切成 system，紧接着那条
    # 「非 shared 就清空 env_id」的规则把用户刚选的环境抹掉。
    mode = str(row.workspace_mode or WORKSPACE_MODE_SHARED)
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description or None,
        "node_id": row.node_id,
        "project_id": str(row.project_id) if row.project_id else None,
        # Lifecycle gate, enforced live by the git proxy: disabling revokes this
        # environment's git credential on its next request, and re-enabling
        # restores the same token (nothing is reissued).
        #
        # ``is not False`` rather than the truthy value: ``enabled`` is a column
        # added after the table existed, so rows predating it hold NULL — and
        # ``not None`` is True in Python, which used to report every legacy
        # environment as disabled (and made dispatch refuse it outright).
        "enabled": row.enabled is not False,
        # Non-empty means the working tree is a directory the node did NOT create,
        # so deleting the environment unbinds instead of removing it.
        "external_path": row.external_path or None,
        # How a session's working tree is materialized: shared (per-session
        # clone) / worktree (shared checkout, per-task worktree) / external
        # (run inside external_path) / fresh (clone into external_path).
        "workspace_mode": mode,
        # Which editor this environment runs. Empty only for rows predating the
        # field; the page renders it as the environment's "编辑器".
        "provider": str(row.provider or ""),
        # Isolated = the editor's own HOME/config (skills, plugins, login state)
        # lives in this environment's directory; false = it runs against the node
        # operator's real HOME. **Derived from the workspace mode**, not a second
        # user-facing choice — see models_environment.resolve_isolated.
        "isolated": resolve_isolated(mode),
        "submodule_paths": list(row.submodule_paths or []),
        "init_commands": list(row.init_commands or []),
        "revision": int(row.revision or 0),
        "synced_revision": int(row.synced_revision or 0),
        # A configured-but-unsynced environment is the normal state right after an
        # edit (and the whole state while its node is offline), so the page needs
        # to distinguish it from a synced one rather than showing a silent lie.
        "needs_sync": int(row.revision or 0) > int(row.synced_revision or 0),
        "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
        "last_sync_error": row.last_sync_error or None,
        # 代码目录（clone）准备状态：建环境这一侧异步跑，页面据此显示
        # 准备中/失败+重试/就绪。老行为 NULL（列后加）按 ready 处理。
        "provision_state": str(row.provision_state or PROVISION_READY),
        "provision_error": row.provision_error or None,
        "provisioned_at": row.provisioned_at.isoformat() if row.provisioned_at else None,
        # 本环境会话后台同步间隔（秒）：NULL=跟随全局默认，0=关闭，>0=专用间隔。
        # 概览 tab 可改，服务端返回原始值，前端区分「跟随默认」和「显式关闭」。
        "session_sync_interval": row.session_sync_interval,
        # 全局默认间隔（秒）。前端在 session_sync_interval 为 NULL 时要显示
        # 「跟随默认（每 N 分钟）」——只有这个值才能把「跟随默认」翻译成人能看懂
        # 的秒数，否则用户不知道默认到底是多少。随环境详情一起返回，省一次请求。
        "session_sync_default": _global_sync_default(),
    }


def _global_sync_default() -> int:
    """全局默认同步间隔（秒）；settings 读不到时兜底 300。"""
    try:
        from .config import settings

        return max(0, int(getattr(settings, "env_session_sync_interval", 0) or 0))
    except Exception:  # noqa: BLE001 — 配置异常不该让环境详情打不开
        return 300


def _resource_dict(row: TaskEnvironmentResource) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "resource_id": str(row.resource_id),
        "name": row.name or "",
        "version": row.version or "",
    }


# ── environment CRUD ─────────────────────────────────────────────────────────


async def list_environments(
    user_id: str, *, node_id: str = "", project_id: str = ""
) -> list[dict]:
    """List the caller's shared environments, optionally narrowed.

    Two callers, two filters:

    * The task-creation flow passes ``node_id``: an environment's HOME lives on
      the node it was created on, so an environment from another node cannot
      host this task.
    * The project workspace passes ``project_id``: an environment is a project's
      landing on a node, so that page lists the ones belonging to the project it
      is showing.

    Both empty lists everything the caller owns.
    """
    query = TaskEnvironment.filter(user_id=_uuid(user_id, field="user_id"))
    if str(node_id or "").strip():
        query = query.filter(node_id=str(node_id).strip())
    if str(project_id or "").strip():
        query = query.filter(project_id=_uuid(project_id, field="project_id"))
    rows = await query.order_by("name")
    return [_env_dict(row) for row in rows]


async def get_environment(user_id: str, env_id: str) -> TaskEnvironment:
    row = await TaskEnvironment.get_or_none(
        id=_uuid(env_id, field="env_id"), user_id=_uuid(user_id, field="user_id")
    )
    if row is None:
        raise EnvironmentError("not_found", "环境不存在")
    return row


async def create_environment(
    user_id: str,
    *,
    node_id: str,
    name: str,
    description: str | None = None,
    project_id: str | None = None,
    workspace_mode: str | None = None,
    external_path: str | None = None,
    provider: str | None = None,
    init_commands: list[str] | None = None,
) -> dict:
    """Create an environment. The id is database-generated.

    An environment is three things fixed together: which node it lands on, which
    editor runs there, and where its working directory comes from. **Whether the
    editor's config is isolated is NOT a fourth choice** — it follows from the
    directory's owner (:func:`models_environment.resolve_isolated`):

    * ``external``（导入）— the directory already belongs to the node operator,
      so their global config is used. ``external_path`` is required.
    * ``fresh``（新建）— the directory is ours to create; the project's repo is
      cloned into ``external_path`` and the editor gets its own config.
      ``external_path`` + ``project_id`` are required.
    * ``shared``/``worktree`` — the historic shapes; platform-owned directory,
      isolated config.

    The id — not the name — is what tasks carry and what the node derives its
    local directory from, so renaming later never moves anything on disk.

    ``project_id`` binds the environment to a project, which is what gives it a
    repository and a git identity to clone with. It is optional so an
    environment can still be created for its HOME/resources alone (the historic
    shape, and what every pre-existing row is).

    ``provider`` is the editor (claude / codex / opencode).
    """
    node = str(node_id or "").strip()
    label = str(name or "").strip()
    if not node:
        raise EnvironmentError("invalid_argument", "node_id 不能为空")
    if not label:
        raise EnvironmentError("invalid_argument", "环境名不能为空")
    user_uuid = _uuid(user_id, field="user_id")
    # 名字不参与唯一性：id 才是环境的身份（节点目录按 id 派生），名字只是标签。
    # 曾经这里按「同节点同名」拒绝，但列表是按项目过滤的——一个 project_id 为空的
    # 老环境会挡住用户，而用户在那个页面上根本看不见它。重名不再挡人，页面各自
    # 靠 id/节点区分。

    mode = str(workspace_mode or "").strip().lower() or WORKSPACE_MODE_SHARED
    if mode not in WORKSPACE_MODES:
        raise EnvironmentError("invalid_argument", f"未知的工作区模式：{mode}")
    adopted = (external_path or "").strip()
    if mode in (WORKSPACE_MODE_EXTERNAL, WORKSPACE_MODE_FRESH):
        # 两条路都要一个目录路径：导入 = 用这个已有的目录；新建 = 把代码 clone
        # 到这个（还不存在的）目录。
        if not adopted:
            raise EnvironmentError(
                "invalid_argument",
                "采用已有目录时必须给出目录路径（external_path）"
                if mode == WORKSPACE_MODE_EXTERNAL
                else "新建时必须给出要落到哪个目录（external_path）",
            )
        if not _is_absolute_node_path(adopted):
            raise EnvironmentError("invalid_argument", "目录必须是绝对路径")
        if mode == WORKSPACE_MODE_FRESH and not project_id:
            # 新建要把项目的仓库 clone 进去；没有项目就没有代码可拉。
            raise EnvironmentError("invalid_argument", "新建环境必须先绑定项目")
    elif adopted:
        raise EnvironmentError(
            "invalid_argument", "external_path 只在导入/新建模式下有意义"
        )
    elif mode == WORKSPACE_MODE_WORKTREE and not project_id:
        # The shared checkout is cloned FROM the project; without one there is
        # nothing to clone and every task would fail at dispatch.
        raise EnvironmentError("invalid_argument", "worktree 模式必须先绑定项目")

    # The editor. Refused when unknown rather than stored: this decides which
    # CLI runs and which config paths it reads, so a typo would silently produce
    # an environment that runs a different tool than the one asked for.
    editor = str(provider or "").strip().lower()
    if editor and editor not in ENV_PROVIDERS:
        raise EnvironmentError("invalid_argument", f"不支持的编辑器：{editor}")
    # 隔离与否不是用户选的第二个开关，是目录归属的推论——单一真相源。
    env_isolated = resolve_isolated(mode)

    bound_project: uuid.UUID | None = None
    submodule_paths: list[str] = []
    project_repo_url = ""
    project_branch = ""
    project_has_git_identity = False
    if project_id:
        bound_project = _uuid(project_id, field="project_id")
        # Ownership is checked here rather than trusted from the request: binding
        # an environment to someone else's project would hand this environment
        # that project's git identity at clone time.
        project = await Project.get_or_none(id=bound_project, user_id=user_uuid)
        if project is None:
            raise EnvironmentError("not_found", "项目不存在或无权访问")
        project_repo_url = str(project.repo_url or "").strip()
        project_branch = str(project.branch or "").strip()
        project_has_git_identity = getattr(project, "git_identity_id", None) is not None
        # An adopted tree is already on disk and is never cloned, so it needs no
        # repository URL and mints no credential. The binding is what files the
        # environment under its project on the project page — requiring a repo
        # here would refuse a perfectly valid "this existing checkout is that
        # project's landing" case.
        if mode == WORKSPACE_MODE_EXTERNAL:
            pass
        elif not project_repo_url:
            raise EnvironmentError(
                "invalid_argument", "该项目未配置仓库地址，无法作为环境的代码来源"
            )
        else:
            # Derived once, at bind time, from the project's own .gitmodules — the
            # git proxy reads it to decide whether an ``r/<path>`` request is a
            # submodule this environment's repo actually declared.
            try:
                submodule_paths = await _project_submodule_paths(project)
            except Exception:  # noqa: BLE001 — a probe failure must not block creation
                logger.exception("[env] submodule allowlist build failed for project {}", bound_project)

    row = await TaskEnvironment.create(
        id=uuid.uuid4(),
        user_id=user_uuid,
        node_id=node,
        project_id=bound_project,
        name=label,
        description=(description or "").strip() or None,
        workspace_mode=mode,
        external_path=adopted or None,
        provider=editor,
        isolated=env_isolated,
        submodule_paths=submodule_paths,
        init_commands=[str(c) for c in (init_commands or []) if str(c).strip()],
    )
    # 建环境时把目录/代码准备好，让第一个任务不必等 clone（分钟级）。四种模式分治：
    #   external（导入）——目录是操作者已有的，节点一个字节都不能动：不 mkdir、不 clone。
    #   fresh（新建）  ——目录归我们但还不存在：clone 进用户选定的 external_path。
    #   worktree      ——clone 进环境的共享检出（envRepoDir），任务从它切 worktree。
    #   shared（历史） ——只建目录（mkdir），不 clone（历史形态，任务自带 clone）。
    # 节点离线不是错误：行已落库，目录/代码在首次派发或下次同步时补。
    #
    # 私有仓库走 environment_git_proxy_url（签名 URL，凭据由 proxy 持有），公共仓库
    # 直接 clone。与 task 侧 `_apply_environment_workspace` 同口径。
    #
    # **clone 是环境这一侧的事，异步跑**：行落库后立即返回，clone 由后台任务执行
    # （成功 ready / 失败 failed+原因，页面显示、可重试——见 provision_environment）。
    # 无东西可准备的（external=操作者的目录；shared=历史形态；没绑项目）建好即 ready。
    # proxy 未配置且是私有仓库 → 直接 failed：任务派发不再兜底 clone（节点只切不
    # clone），这个环境就是备不好，报出来比留给派发时炸强。
    git_spec: dict | None = None
    target_path = ""
    needs_provision = False
    provision_skip_reason = ""
    if mode == WORKSPACE_MODE_EXTERNAL:
        provision_skip_reason = "external"  # 采用已有目录：什么都不建
    elif mode in (WORKSPACE_MODE_FRESH, WORKSPACE_MODE_WORKTREE):
        if not project_id:
            # 建环境校验已挡（worktree/fresh 必须绑项目）；老代码路径防御。
            provision_skip_reason = "no-project"
        elif not project_repo_url:
            provision_skip_reason = "no-repo"
        else:
            from .task_service import environment_git_proxy_url

            clone_url = project_repo_url
            if project_repo_url.startswith(("https://", "http://")) and project_has_git_identity:
                try:
                    clone_url = environment_git_proxy_url(str(row.id), project_repo_url)
                except ValueError as exc:
                    # proxy 未配置且是私有仓库：环境备不好，直接置 failed+原因。
                    # 不静默——静默留下的口子正是「建环境时没 clone、任务派发时
                    # 再补」的兜底，而这个兜底已经删了（节点只切不 clone）。
                    await TaskEnvironment.filter(id=row.id).update(
                        provision_state=PROVISION_FAILED,
                        provision_error=f"git 代理未配置：{exc}",
                    )
                    return _env_dict(await TaskEnvironment.get(id=row.id))
            git_spec = {"url": clone_url, "branch": project_branch}
            if mode == WORKSPACE_MODE_FRESH:
                # fresh：clone 进用户选定的目录；worktree 留空 = 节点用 envRepoDir。
                target_path = adopted
            needs_provision = True
    if needs_provision:
        await TaskEnvironment.filter(id=row.id).update(provision_state=PROVISION_PROVISIONING)
        asyncio.create_task(
            _provision_environment_task(
                str(row.id), node, git_spec, target_path,
            ),
            name=f"env-provision-{row.id}",
        )
        return _env_dict(await TaskEnvironment.get(id=row.id))
    if provision_skip_reason:
        logger.debug("[env] provision skipped for {} ({}), state stays ready", row.id, provision_skip_reason)
    return _env_dict(row)


async def _provision_environment_task(
    env_id: str, node_id: str, git_spec: dict | None, target_path: str
) -> None:
    """后台准备环境代码目录，结果写回环境行。建环境、重试、重启扫描共用这一份。

    节点侧 ``manageEnvironment(create)`` 幂等（目录已存在即跳过 clone），所以重跑
    安全。失败只记状态不抛出——这条 task 没有调用方。
    """
    from .nodes_service import nodes_service

    try:
        await nodes_service.manage_environment(
            node_id, env_id, "create", git=git_spec, target_path=target_path
        )
    except Exception as exc:  # noqa: BLE001 — 后台 task，失败落到行上给人看
        # 错误文本可能带签名 URL（git 代理 token 在 query 里），先洗再入库。
        reason = _redact_provision_error(exc)
        logger.warning("[env] provision failed for {}: {}", env_id, reason)
        await TaskEnvironment.filter(id=env_id).update(
            provision_state=PROVISION_FAILED, provision_error=reason
        )
        return
    row = await TaskEnvironment.get_or_none(id=env_id)
    if row is None:
        # clone 跑完环境已被删除：别把行写回来，但节点上可能刚 mkdir/clone 出目录。
        # 删环境的 manage_environment(remove) 与本 task 的 create 竞争，尽力收尾。
        try:
            await nodes_service.manage_environment(node_id, env_id, "remove")
        except Exception:  # noqa: BLE001
            logger.warning("[env] provision orphan cleanup failed for {}", env_id)
        return
    await TaskEnvironment.filter(id=env_id).update(
        provision_state=PROVISION_READY,
        provision_error=None,
        provisioned_at=datetime.now(timezone.utc),
    )
    logger.info("[env] provisioned {}", env_id)


def _redact_provision_error(exc: Exception) -> str:
    """准备失败的文本，洗掉凭据：签名 clone URL 的 token 在 query 里。"""
    text = str(exc) or exc.__class__.__name__
    text = re.sub(r"://[^/@\s]*@", "://", text)  # scheme://user:token@ → scheme://
    text = re.sub(r"[?&](token|access_token|sign|sig|auth)=[^&\s]+", r"?[redacted]", text, flags=re.I)
    return text[:500]


async def _env_git_spec(row: TaskEnvironment) -> tuple[dict | None, str]:
    """从环境行重建 clone 计划：建环境与重试共用一份，别长出口径差异。

    返回 ``(git_spec|None, target_path)``。``None`` = 没有东西可 clone（external /
    shared / 没绑项目 / 项目没仓库）。私有仓库走环境级签名 URL（proxy 未配置
    时 raise ValueError，由调用方转成 failed 状态）。
    """
    mode = str(row.workspace_mode or "").strip().lower()
    if mode not in (WORKSPACE_MODE_FRESH, WORKSPACE_MODE_WORKTREE):
        return None, ""
    if row.project_id is None:
        return None, ""
    project = await Project.get_or_none(id=row.project_id)
    repo_url = str((project.repo_url if project else "") or "").strip()
    if not repo_url:
        return None, ""
    clone_url = repo_url
    if repo_url.startswith(("https://", "http://")) and getattr(project, "git_identity_id", None) is not None:
        from .task_service import environment_git_proxy_url

        clone_url = environment_git_proxy_url(str(row.id), repo_url)
    git_spec = {"url": clone_url, "branch": str((project.branch if project else "") or "").strip()}
    target_path = ""
    if mode == WORKSPACE_MODE_FRESH:
        # 原生形式：节点用 filepath.IsAbs 判目录，虚拟形式在 Windows 上会被判成
        # 相对路径（与派发 external_workdir 同口径）。
        target_path = node_native_path(str(row.external_path or "").strip())
    return git_spec, target_path


async def provision_environment(user_id: str, env_id: str) -> dict:
    """重试环境准备（页面「重试」按钮的落点）。

    重新从环境行重建 clone 计划并跑一次——节点侧幂等，目录已在就是秒回。
    已在准备中（provisioning）不重复起，直接返回当前状态。
    """
    row = await TaskEnvironment.get_or_none(id=_uuid(env_id, field="env_id"), user_id=_uuid(user_id))
    if row is None:
        raise EnvironmentError("not_found", "环境不存在")
    if str(row.provision_state or PROVISION_READY) == PROVISION_PROVISIONING:
        return _env_dict(row)
    git_spec, target_path = await _env_git_spec(row)
    if git_spec is None:
        # 没有可 clone 的东西：直接判定就绪（external/shared/无项目都走这里）。
        await TaskEnvironment.filter(id=row.id).update(
            provision_state=PROVISION_READY, provision_error=None,
            provisioned_at=datetime.now(timezone.utc),
        )
        return _env_dict(await TaskEnvironment.get(id=row.id))
    await TaskEnvironment.filter(id=row.id).update(provision_state=PROVISION_PROVISIONING)
    asyncio.create_task(
        _provision_environment_task(str(row.id), str(row.node_id), git_spec, target_path),
        name=f"env-provision-{row.id}",
    )
    return _env_dict(await TaskEnvironment.get(id=row.id))


async def recover_pending_provisions() -> int:
    """服务启动：把「准备中」的环境重跑一遍（进程重启丢了在途 task）。

    迁移把存量 worktree/fresh 行也标成 provisioning，所以这里同时是给老环境做
    首次验证/补 clone 的入口——节点 clone 幂等，目录已在就是秒回。返回重新起跑
    的环境数。
    """
    rows = await TaskEnvironment.filter(provision_state=PROVISION_PROVISIONING)
    started = 0
    for row in rows:
        try:
            git_spec, target_path = await _env_git_spec(row)
        except ValueError as exc:
            await TaskEnvironment.filter(id=row.id).update(
                provision_state=PROVISION_FAILED,
                provision_error=_redact_provision_error(exc),
            )
            continue
        if git_spec is None:
            await TaskEnvironment.filter(id=row.id).update(
                provision_state=PROVISION_READY, provision_error=None,
                provisioned_at=datetime.now(timezone.utc),
            )
            continue
        asyncio.create_task(
            _provision_environment_task(str(row.id), str(row.node_id), git_spec, target_path),
            name=f"env-provision-{row.id}",
        )
        started += 1
    if started:
        logger.info("[env] provision recovery restarted {} environment(s)", started)
    return started


async def _project_submodule_paths(project: Any) -> list[str]:
    """Same-host submodule paths the project's ``.gitmodules`` declares.

    Mirrors ``TaskService._persist_submodule_allowlist``: fetch ``.gitmodules``
    through the project's own git identity, keep only submodules whose host
    matches the parent's (a different host cannot be served by the proxy), and
    return the host-relative paths.

    Best-effort by design: a repo without submodules, an unreachable host, or a
    malformed file all yield ``[]``, which only means the proxy refuses ``r/``
    requests for submodules — the main clone is unaffected.
    """
    import base64 as _base64
    from urllib.parse import urlparse

    from . import git_clients
    from .git_service import git_service
    from .project_service import _derive_full_name, _resolve_submodule_url, parse_gitmodules

    repo_url = str(project.repo_url or "").strip()
    identity_id = getattr(project, "git_identity_id", None)
    if not repo_url or identity_id is None:
        return []
    identity = await git_service.load_identity_for_read(str(identity_id))
    if identity is None or not identity.access_token:
        return []
    platform = (identity.platform or "").lower()
    if not git_clients.supports_platform(platform):
        return []
    full_name = _derive_full_name(repo_url)
    if not full_name:
        return []
    try:
        blob = await git_clients.fetch_blob(
            platform, full_name, git_service._build_repo_options(identity),
            path=".gitmodules", ref=str(project.branch or ""),
        )
    except git_clients.GitClientError:
        return []
    if blob is None or not blob.content:
        return []
    try:
        text = _base64.b64decode(blob.content, validate=True).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return []

    parent = urlparse(repo_url)
    parent_authority = f"{parent.scheme.lower()}://{parent.netloc.lower()}"
    paths: list[str] = []
    for section in parse_gitmodules(text):
        parsed = urlparse(_resolve_submodule_url(repo_url, section["url"]))
        if f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" != parent_authority:
            continue
        sub_path = (parsed.path or "").strip("/")
        if sub_path:
            paths.append(sub_path)
    return sorted(set(paths))


async def update_environment(
    user_id: str,
    env_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    init_commands: list[str] | None = None,
    session_sync_interval: int | None | object = _UNSET,
) -> dict:
    """Rename / re-describe / enable / disable. Does not touch the node.

    ``enabled`` is a pure state flip: the git proxy re-reads this row on every
    request, so disabling takes effect immediately and re-enabling restores the
    SAME token. Nothing on the node has to change, which is why this stays a
    metadata-only operation.

    ``session_sync_interval``: ``_UNSET`` = client didn't touch the field;
    ``None`` = client explicitly reset it to "follow the global default";
    ``0`` = turn this environment's background session sync off;
    ``>0`` = override with this many seconds.
    """
    row = await get_environment(user_id, env_id)
    if name is not None:
        label = str(name).strip()
        if not label:
            raise EnvironmentError("invalid_argument", "环境名不能为空")
        # 改名同样不做重名校验：名字只是标签，身份是 id。见 create_environment。
        row.name = label
    if description is not None:
        row.description = str(description).strip() or None
    if enabled is not None:
        row.enabled = bool(enabled)
    if init_commands is not None:
        row.init_commands = [str(c) for c in init_commands if str(c).strip()]
    if session_sync_interval is not _UNSET:
        # None = 跟随全局默认；0 = 关闭；>0 = 专用间隔。
        if session_sync_interval is not None:
            value = int(session_sync_interval)
            if value < 0:
                raise EnvironmentError("invalid_argument", "定时同步间隔不能为负数")
            row.session_sync_interval = value
        else:
            row.session_sync_interval = None
    await row.save()
    return _env_dict(row)


async def delete_environment(user_id: str, env_id: str) -> dict:
    """Delete an environment: remove its directory on the node, then its rows.

    The node call comes first so a failure leaves the row (and its id) intact —
    dropping the row while the directory survives would orphan real disk with no
    way to reach it from the UI.

    An ``external_path`` environment is the exception: its working tree is a
    directory the node did NOT create (the operator's own checkout), so the
    directory is never removed — only the binding is. Deleting a user's own
    working tree because they detached an environment from it would be data
    loss, and unrecoverable.
    """
    row = await get_environment(user_id, env_id)
    external = bool((row.external_path or "").strip())

    if not external:
        from .nodes_service import nodes_service

        try:
            await nodes_service.manage_environment(row.node_id, str(row.id), "remove")
        except Exception as exc:  # noqa: BLE001
            raise EnvironmentError(
                "failed_precondition", f"节点未能删除环境目录：{exc}"
            ) from exc

    await TaskEnvironmentResource.filter(env_id=row.id).delete()
    await TaskEnvironmentInstalled.filter(env_id=row.id).delete()
    await row.delete()
    return {
        "deleted": True,
        "id": str(row.id),
        # Reported so the UI can say the checkout was left in place rather than
        # silently implying it was cleaned up.
        "kept_external_path": row.external_path if external else None,
    }


# ── resource set (the user's configured skill/MCP/plugin list) ────────────────


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().lower()
    if kind not in ENV_RESOURCE_KINDS:
        raise EnvironmentError("invalid_argument", f"不支持的资源类型 {kind}")
    return kind


async def list_environment_resources(user_id: str, env_id: str) -> list[dict]:
    row = await get_environment(user_id, env_id)
    rows = await TaskEnvironmentResource.filter(env_id=row.id).order_by("kind", "name")
    return [_resource_dict(item) for item in rows]


async def add_environment_resource(
    user_id: str, env_id: str, *, kind: str, resource_id: str, team_id: str,
    request_base_url: str = "",
) -> dict:
    """Add one granted resource to an environment — **installed on the node now**.

    The reference must be visible to this user (same rule tasks follow), so an
    environment cannot smuggle in a resource the user was never granted.

    节点是唯一真相，平台只是副本：所以这里不只是写一行配置，而是立刻把变更下发
    到节点（隔离档 sync_environment 按期望集 prune，不隔离档走系统环境安装），
    然后重读节点事实。下发失败就把错误抛出去——不留在"待同步"里骗人。
    """
    row = await get_environment(user_id, env_id)
    kind = _normalize_kind(kind)
    rid = _uuid(resource_id, field="resource_id")
    mode = str(row.workspace_mode or "").strip().lower()

    if not resolve_isolated(mode):
        # 不隔离档：环境的 HOME 就是操作者 HOME，跟节点系统环境面板同一份数据。
        # 走系统环境安装路径（它会自己重读节点），不碰 TaskEnvironmentResource
        # ——那份配置行对不隔离档没有意义（资源本来就活在操作者 HOME 里）。
        if kind == ENV_RESOURCE_MCP:
            # 节点不写操作者本机 MCP 配置（~/.mcp.json 是编辑器自己的），所以
            # 这里没有"下发安装"这一步；本机已有的 MCP 自动生效。
            raise EnvironmentError(
                "invalid_argument",
                "不隔离环境的 MCP 跟随节点操作者本机配置（~/.mcp.json），平台不下发；"
                "本机已有的会自动生效",
            )
        from .system_env_service import install_to_system_env

        await install_to_system_env(
            user_id, str(row.node_id), kind=kind, resource_id=str(rid),
            overwrite=False, team_id=team_id, request_base_url=request_base_url,
        )
        return {"kind": kind, "resource_id": str(rid), "state": "installed"}

    from .resource_reference_service import visible_references

    allowed = {item["id"]: item for item in await visible_references(user_id, team_id, kind)}
    reference = allowed.get(str(rid))
    if reference is None:
        # 双轨兜底：装环境存的是裸 UUID（TaskEnvironmentResource.resource_id），
        # 新表（统一资源池）引用在此按可见性识别。MCP 不走池引用——任务 MCP
        # 链按 owned_entity_id 落 mcp_services grant，池引用没有归属服务。
        if kind != "mcp":
            from .resource_reference_service import _team_is_admin

            import resource_store

            is_admin = await _team_is_admin(user_id, team_id)
            # skill 通道混排：单 skill + 存量 skills 集合 + plugin 容器（带 entries）。
            types = ("skill", "skills", "plugin") if kind == "skill" else (kind,)
            for t in types:
                v2_rows = await resource_store.visible_references(
                    user_id, team_id, resource_type=t, is_admin=is_admin,
                )
                hit = next((row for row in v2_rows if str(row["id"]) == str(rid)), None)
                if hit is not None:
                    reference = hit
                    break
    if reference is None:
        raise EnvironmentError("permission_denied", "该资源未授权给你，无法加入环境")
    if await TaskEnvironmentResource.filter(env_id=row.id, kind=kind, resource_id=rid).exists():
        raise EnvironmentError("conflict", "该资源已在环境中")

    created = await TaskEnvironmentResource.create(
        id=uuid.uuid4(),
        env_id=row.id,
        kind=kind,
        resource_id=rid,
        name=str(reference.get("display_name") or reference.get("name") or ""),
        version=str(reference.get("version") or ""),
    )
    row.revision = int(row.revision or 0) + 1
    await row.save()
    # MCP 不是文件（节点只读 ~/.mcp.json，从不写它），所以没有"下发安装"这一步；
    # 它由任务派发时按 principal 现解析合并——配置行就是它的全部事实。
    if kind != ENV_RESOURCE_MCP:
        await _dispatch_and_refresh(user_id, env_id, team_id=team_id,
                                    request_base_url=request_base_url)
    return _resource_dict(created)


async def remove_environment_resource(
    user_id: str, env_id: str, entry_id: str, *, team_id: str = ""
) -> dict:
    """Remove one resource from the environment — **uninstalled on the node now**.

    节点为准，按档位分流（entry_id 的空间不同）：

    * 不隔离档：列表来自系统环境（``NodeSystemEnvEntry``，同一个 HOME 同一份
      数据），entry_id 是它的行——卸的是节点上的文件（只有平台装的可卸，
      操作者本机装的节点会拒绝，错误照实抛）。
    * 隔离档：entry_id 是环境配置行，删行后立即按期望集 prune 下发到节点。

    MCP（配置行）两边都不动文件——它由任务派发时解析，删行即可。
    """
    row = await get_environment(user_id, env_id)
    mode = str(row.workspace_mode or "").strip().lower()

    if not resolve_isolated(mode):
        from .models_environment import NodeSystemEnvEntry
        from .system_env_service import remove_from_system_env

        node_entry = await NodeSystemEnvEntry.get_or_none(
            id=_uuid(entry_id, field="entry_id")
        )
        if node_entry is None or str(node_entry.node_id) != str(row.node_id):
            raise EnvironmentError("not_found", "该资源不在此环境的节点上")
        if node_entry.kind == ENV_RESOURCE_MCP:
            raise EnvironmentError(
                "invalid_argument", "MCP 跟随节点操作者本机配置，平台不下发删除"
            )
        kind, name = node_entry.kind, node_entry.name
        await remove_from_system_env(user_id, str(row.node_id), kind=kind, name=name)
        return {"deleted": True, "kind": kind, "name": name}

    entry = await TaskEnvironmentResource.get_or_none(
        id=_uuid(entry_id, field="entry_id"), env_id=row.id
    )
    if entry is None:
        raise EnvironmentError("not_found", "该资源不在环境中")
    kind, name = entry.kind, entry.name or ""
    await entry.delete()
    row.revision = int(row.revision or 0) + 1
    await row.save()
    if kind != ENV_RESOURCE_MCP:
        await _dispatch_and_refresh(user_id, env_id, team_id=team_id)
    return {"deleted": True, "id": str(entry.id), "kind": kind, "name": name}


async def _dispatch_and_refresh(
    user_id: str, env_id: str, *, team_id: str, request_base_url: str = ""
) -> None:
    """把环境的期望集下发到节点，然后重读节点事实。失败就抛，不留"待同步"。"""
    await sync_environment(
        user_id, env_id, team_id=team_id, request_base_url=request_base_url
    )


# ── sync + inventory (desired state → node, node → observed state) ────────────


async def _resolved_specs(
    user_id: str, team_id: str, env_row: TaskEnvironment, *, request_base_url: str
) -> tuple[list[dict], list[dict]]:
    """Resolve the environment's skill/plugin references into node wire specs.

    MCP is deliberately excluded: it is config, not a file, and materializing it
    into a shared HOME would write one task's credentials where other tasks read.
    """
    from .resource_reference_service import resolve_reference_specs

    entries = await TaskEnvironmentResource.filter(env_id=env_row.id)
    by_kind: dict[str, list[dict]] = {ENV_RESOURCE_SKILL: [], ENV_RESOURCE_PLUGIN: []}
    for entry in entries:
        if entry.kind in by_kind:
            by_kind[entry.kind].append({"resource_id": str(entry.resource_id)})

    skills = await resolve_reference_specs(
        user_id, team_id, ENV_RESOURCE_SKILL, by_kind[ENV_RESOURCE_SKILL],
        request_base_url=request_base_url,
    ) if by_kind[ENV_RESOURCE_SKILL] else []
    plugins = await resolve_reference_specs(
        user_id, team_id, ENV_RESOURCE_PLUGIN, by_kind[ENV_RESOURCE_PLUGIN],
        request_base_url=request_base_url,
    ) if by_kind[ENV_RESOURCE_PLUGIN] else []
    return skills, plugins


async def sync_environment(
    user_id: str, env_id: str, *, team_id: str, request_base_url: str = "",
    include_sessions: bool = True, import_content: bool = False,
) -> dict:
    """一次性同步这个环境的**全部**数据：技能/插件/MCP 清单 + 会话记录。

    页面上只有一个「同步数据」按钮——拆成"同步资源/同步会话"两个入口让用户自己
    拼是不合理的，两边本来就是同一份节点事实：

    * 隔离档：把配置的 skill/plugin 下发安装到节点（按期望集 prune，不多不少）
    * 两档都做：重读节点事实（清单。不隔离档读系统环境那份）刷新显示副本
    * 两档都做：与节点对账会话账本（节点报它实际跑了哪些会话）

    同步时间落在 ``last_synced_at``，概览 tab 展示它。MCP 不参与文件下发（节点
    从不写 ~/.mcp.json），它只是清单里的一类，读回来即可。

    ``import_content`` 是把会话**内容**也拉一份显示副本——只有「同步数据」按钮
    要它（路由显式传 True）。加减资源走 :func:`_dispatch_and_refresh`，那要的是
    清单被节点接受，不必为每个会话多跑一趟节点。
    """
    row = await get_environment(user_id, env_id)
    if resolve_isolated(str(row.workspace_mode or "")):
        skills, plugins = await _resolved_specs(
            user_id, team_id, row, request_base_url=request_base_url
        )
        from .nodes_service import nodes_service

        target_revision = int(row.revision or 0)
        try:
            await nodes_service.sync_environment(
                row.node_id, str(row.id), skills, plugins
            )
        except Exception as exc:  # noqa: BLE001
            # 落库而不是只抛：装了个寂寞（用户点了同步但能力没装上）是最难查的
            # 失败形态，页面要能显示它。
            row.last_sync_error = str(exc)[:1000]
            await row.save()
            raise EnvironmentError("failed_precondition", f"同步失败：{exc}") from exc
        row.synced_revision = target_revision

    row.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.last_sync_error = None
    await row.save()
    # 资源清单：重读节点事实（显示副本），失败不该让整个同步失败——会话可能同步
    # 成功了，而清单只是暂时读不到节点。
    try:
        await refresh_environment_inventory(user_id, env_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("[env] inventory refresh after sync failed for {}: {}", env_id, exc)
    # 会话账本对账：和资源一起同步，页面上会话列表与资源列表同时是最新。
    # import_content：内容副本也一起拉（只有「同步数据」按钮会传 True）。
    if include_sessions:
        try:
            await sync_environment_sessions(
                user_id, env_id, include_content=import_content
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("[env] session sync failed for {}: {}", env_id, exc)
    return _env_dict(row)


async def refresh_environment_inventory(user_id: str, env_id: str) -> list[dict]:
    """重读节点事实，更新显示副本。

    两档数据源不同（详情页读哪张表，这里就刷哪张）：

    * 隔离档（fresh/shared/worktree）：节点 ``inspect_environment`` 扫该环境
      HOME 的 ``.agents/{skills,plugins}``，整表替换 ``TaskEnvironmentInstalled``
      （快照——节点上删掉的平台这边必须跟着消失）。
    * 不隔离档（external）：环境的 HOME 就是操作者 HOME，事实在
      ``NodeSystemEnvEntry``（跟节点系统环境面板同一份），走 ``refresh_system_env``
      ——不再往环境快照里写第二份（两份副本各刷各的必然对不上）。
    """
    from .models_environment import resolve_isolated
    from .node_client import get_local_node_client

    row = await get_environment(user_id, env_id)
    mode = str(row.workspace_mode or "").strip().lower()
    if not resolve_isolated(mode):
        from .system_env_service import refresh_system_env

        await refresh_system_env(user_id, str(row.node_id))
        return []

    try:
        result = await get_local_node_client().inspect_environment(row.node_id, str(row.id))
    except Exception as exc:  # noqa: BLE001 — 节点不可达是运行常态，报可诊断的错
        raise EnvironmentError("failed_precondition", f"扫描节点环境目录失败：{exc}") from exc

    observed = [item for item in (result.get("installed") or []) if isinstance(item, dict)]
    # 去重必须按 **(kind, name)**——表上的唯一键就是 (env_id, kind, name)，不含
    # provider。同一个名字被多个来源报到两次时留第一条，不去重会撞唯一键抛 500。
    deduped: dict[tuple[str, str], dict] = {}
    for item in observed:
        kind = str(item.get("kind") or "").strip() or ENV_RESOURCE_SKILL
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        deduped.setdefault((kind, name), item)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await TaskEnvironmentInstalled.filter(env_id=row.id).delete()
    for item in deduped.values():
        await TaskEnvironmentInstalled.create(
            id=uuid.uuid4(),
            env_id=row.id,
            kind=str(item.get("kind") or "").strip() or ENV_RESOURCE_SKILL,
            name=str(item.get("name") or "").strip(),
            version=str(item.get("version") or ""),
            reported_at=now,
        )
    return list(deduped.values())


async def environment_detail(user_id: str, env_id: str) -> dict:
    """Environment + its configured set.

    Single list of resources: for isolated environments, each configured skill/plugin
    is labelled ``installed`` (node saw it) or ``pending`` (not on disk yet).
    For system (external) environments, we read directly from NodeSystemEnvEntry —
    the operator's HOME inventory — so there is no "extras" split; database and node
    are the same source of truth. MCP rows are always ``config`` — they are never
    on disk, so "installed" does not apply to them.

    No more "configured vs extra" split: all operations go to the node immediately,
    and we refresh right after to show reality. The page shows what the node has,
    period.
    """
    row = await get_environment(user_id, env_id)
    mode = str(row.workspace_mode or "").strip().lower()
    is_isolated = resolve_isolated(mode)

    if not is_isolated:
        # 不隔离档：直接读系统环境表（跟节点系统环境面板同一个 HOME 同一份数据），
        # 不再自己养 TaskEnvironmentInstalled 快照。环境自己的 MCP 配置行（任务
        # 派发时解析，不落盘）也并进同一个列表——它们是"这个环境的资源"，只是
        # 生效时机在派发。
        from .system_env_service import system_env_detail

        try:
            node_detail = await system_env_detail(user_id, str(row.node_id or ""))
        except Exception as exc:  # noqa: BLE001 — 节点不可用/未授权时详情页仍要能开
            logger.info("[env] system env detail unavailable for {}: {}", row.node_id, exc)
            node_detail = {"resources": {}}

        mcp_rows = {
            str(e.name): e
            for e in await TaskEnvironmentResource.filter(env_id=row.id, kind=ENV_RESOURCE_MCP)
        }
        resources: list[dict] = [
            _resource_dict(e) | {"state": "config", "removable": True}
            for e in mcp_rows.values()
        ]
        seen_mcp = set(mcp_rows)
        for kind in (ENV_RESOURCE_SKILL, ENV_RESOURCE_PLUGIN, ENV_RESOURCE_MCP):
            for entry in node_detail.get("resources", {}).get(kind, []) or []:
                if kind == ENV_RESOURCE_MCP and str(entry.get("name") or "") in seen_mcp:
                    continue  # 平台配置行已覆盖同名 MCP，不重复列
                resources.append({
                    "id": str(entry.get("id") or ""),
                    "kind": kind,
                    "resource_id": "",
                    "name": str(entry.get("name") or ""),
                    "version": str(entry.get("version") or ""),
                    "state": "installed",
                    # platform_managed = 平台装的（可卸）；操作者本机装的只能看
                    "removable": bool(entry.get("platform_managed"))
                    and kind in (ENV_RESOURCE_SKILL, ENV_RESOURCE_PLUGIN),
                })
        detail_dict = _env_dict(row)
        detail_dict["resources"] = resources
        # 导入（external）环境：显示节点上该 checkout **实际**所在的分支（读检出，
        # 不是项目配置的分支——操作者可能已经在本地切走了）。其它不隔离档（system）
        # 没有 checkout，空串。
        detail_dict["checkout_branch"] = (
            await _read_checkout_branch(str(row.node_id or ""), str(row.external_path or ""))
            if mode == WORKSPACE_MODE_EXTERNAL
            else ""
        )
        return detail_dict

    # 隔离档：列表 = 配置行 + 状态从节点扫描结果取（installed/pending）。
    # extras/"未配置"整个删掉——所有操作即下发，不存在"节点上有但平台没配"的显示。
    configured = await TaskEnvironmentResource.filter(env_id=row.id).order_by("kind", "name")
    installed = await TaskEnvironmentInstalled.filter(env_id=row.id)

    observed: dict[tuple[str, str], TaskEnvironmentInstalled] = {
        (item.kind, item.name): item for item in installed
    }
    resources: list[dict] = []
    for entry in configured:
        item = _resource_dict(entry)
        if entry.kind == ENV_RESOURCE_MCP:
            # MCP is resolved per task (with that task's own token), never a file.
            item["state"] = "config"
            resources.append(item)
            continue
        key = (entry.kind, entry.name)
        if key in observed:
            item["state"] = "installed"
            item["installed_version"] = observed[key].version or ""
        else:
            item["state"] = "pending"
        resources.append(item)

    detail = _env_dict(row)
    detail["resources"] = resources
    return detail


# ── environment sessions (list + reconcile) ──────────────────────────────────
#
# 会话 tab 读的是库，不是节点：页面要的是「这个环境上跑过哪些会话」，那是历史，
# 不是一次实时查询。库里的记录从节点同步过来——节点是事实来源（会话真的跑在它
# 上面），服务器记录只是发过的指令。对账时节点上报的补进来、服务器有而节点没有
# 的标为已结束，绝不反向覆盖。


async def list_environment_sessions(
    user_id: str, env_id: str, *, limit: int = 50, offset: int = 0,
    provider: str = "", status: str = "", query: str = "",
) -> dict:
    """这个环境上跑过的会话（服务器记录），按最近活动倒序——**分页 + 过滤**。

    列表是滚动翻页的：每页只多拉 ``limit`` 条，``total`` 让页脚能说"还有多少"。
    过滤三项：``provider``（编辑器）、``status``（running/ended）、``query``
    （标题/平台名/会话 id 关键词）——会话一多要能筛，不是从几十条里翻。

    直接发 raw SQL 而不是用 Tortoise 模型：``mc_ac_node_sessions`` 由控制面
    （``node_server``）注册，数据进程**故意**不注册它（见 user_platform/database.py
    的说明）——注册了会在两个进程里各持一份模型定义。这里跟
    ``node_client/session_bindings.py`` 同款，走共享池读。
    """
    row = await get_environment(user_id, env_id)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    sessions, total = await _read_env_sessions(
        str(row.id), limit=limit, offset=offset,
        provider=provider, status=status, query=query,
    )
    return {"sessions": sessions, "total": total}


def _affected_rowcount(tag: str) -> int:
    """从驱动的状态标签里取受影响行数（asyncpg ``INSERT 1`` / ``UPDATE 3``）。

    按空格后取数，别用 ``endswith("1")``——那会把 ``UPDATE 11`` 当成 1。
    """
    try:
        return int(str(tag or "").rsplit(" ", 1)[-1])
    except (IndexError, ValueError):
        return 0


def _clean_session_title(raw: str) -> str:
    """把节点报来的标题收成能安全展示的一行。

    - 换行/制表符换空格（列表是 TSV 输出的，标题里的 TAB 会把列挤错位）；
    - 截到 200 字（列是 VARCHAR(512)，但没人想在列表里看一整段 prompt）；
    - opencode 把占位标题当没有——`New session - 2026-05-12T14:59:45.372Z`
      这类对用户没有信息量，留着还不如落兜底名。
    """
    text = re.sub(r"[\t\r\n]+", " ", str(raw or "")).strip()
    if re.match(r"^New session( |\s*$| - )", text, re.IGNORECASE):
        return ""
    return text[:200]


def _session_display_name(platform_name: str, title: str, session_id: str) -> str:
    """列表显示名：平台改过名就用它，否则用节点同步来的标题。

    兜底刻意给「会话 <短id>」而不是裸 uuid（第 1 条要求）——完整 id 仍在第二行的
    小字里，排查用。
    """
    if platform_name:
        return platform_name
    if title:
        return title
    return f"会话 {str(session_id)[:8]}"


def _iso_time(value) -> str | None:
    """时间列统一成 ISO 串。asyncpg 给 datetime，sqlite/别的驱动可能直接给
    字符串——展示层只认字符串，别让调用方猜。"""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def _read_env_sessions(
    env_id: str, *, limit: int | None = None, offset: int = 0,
    provider: str = "", status: str = "", query: str = "",
) -> tuple[list[dict], int]:
    """按 env_id 读会话账本（raw SQL），返回（当前页, 该环境总数）。

    分页在 SQL 里做：列表只在滚动到时多拉一页，不给整表。池不可用时返回空列表。
    过滤三项（编辑器/状态/关键词）同样进 SQL——在 Python 里筛会把分页数错位。
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        return [], 0
    # 动态条件：$1 固定是 env_id，过滤参数从 $2 起递增，LIMIT/OFFSET 接在最后。
    where = ["env_id=$1"]
    params: list = [env_id]
    idx = 2
    prov = str(provider or "").strip()
    if prov:
        where.append(f"provider=${idx}")
        params.append(prov)
        idx += 1
    st = str(status or "").strip()
    if st:
        where.append(f"status=${idx}")
        params.append(st)
        idx += 1
    q = str(query or "").strip()
    if q:
        # LOWER(...) LIKE LOWER(...) 而不是 ILIKE：ILIKE 是 PG 专有，测试用 sqlite
        # 真跑这段 SQL 时会直接语法错——而"真跑"正是这段逻辑唯一有意义的验证方式。
        where.append(
            f"(LOWER(title) LIKE LOWER(${idx}) OR LOWER(platform_name) LIKE LOWER(${idx})"
            f" OR LOWER(session_id) LIKE LOWER(${idx}))"
        )
        params.append(f"%{q}%")
        idx += 1
    where_sql = " AND ".join(where)
    async with PostgresClient.pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM mc_ac_node_sessions WHERE {where_sql}", *params
        )
        base = (
            "SELECT session_id, provider, status, updated_at, title, platform_name, "
            " task_id, last_activity_at, model, mode, effort "
            f"FROM mc_ac_node_sessions WHERE {where_sql} ORDER BY updated_at DESC"
        )
        if limit is None:
            rows = await conn.fetch(base, *params)
        else:
            sql = f"{base} LIMIT ${idx} OFFSET ${idx + 1}"
            params.extend([int(limit), int(offset)])
            rows = await conn.fetch(sql, *params)
    out = []
    for r in rows:
        # NULL 容错：这两列若由控制面先建（它按模型加列、写的是 NULL），读出来是
        # None 而不是 ''。
        title = str(r["title"] or "").strip()
        platform_name = str(r["platform_name"] or "").strip()
        session_id = str(r["session_id"])
        activity = r["last_activity_at"]
        out.append({
            "session_id": session_id,
            "provider": str(r["provider"] or ""),
            # 三个都给：title/platform_name 让页面能区分「同步来的」和「用户改的」
            # （改名输入框要预填的是 platform_name，占位提示用的是 title）。
            "title": title,
            "platform_name": platform_name,
            "display_name": _session_display_name(platform_name, title, session_id),
            "status": "running" if str(r["status"] or "").lower() in ("running", "active") else "ended",
            "synced_at": _iso_time(r["updated_at"]),
            # 会话在节点上最后一次活动的时间（编辑器记录里最后一条的时间）——列表
            # 展示的是它，不是"行什么时候被对账过"。控制面派发、还没被对账的行为空。
            "last_activity_at": _iso_time(activity),
            # 控制面派发时写的模型名；节点扫描来的会话这条为空（详情页顶部只读
            # 输入区显示的模型以会话历史里最后一条助手消息的 model 为准）。
            "model": str(r["model"] or "").strip(),
            # 非空 = 这条已被二次同步成平台任务，页面据此决定行点击是跳转还是弹采用框。
            "task_id": str(r["task_id"] or "").strip(),
            # 权限档/思考等级：从节点会话记录读出（同步为任务时预填任务参数，不让
            # 用户重选）。opencode 读不到 → 空串，前端显示「编辑器默认」/「默认」。
            "mode": str(r["mode"] or "").strip(),
            "effort": str(r["effort"] or "").strip(),
        })
    return out, int(total or 0)


async def sync_environment_sessions(
    user_id: str, env_id: str, *, include_content: bool = False
) -> dict:
    """与节点对账：节点上报它实际的会话，服务器据此更新记录。

    **节点为准**：节点多出来的补进库，库里有而节点没有的标为已结束（不删——历史
    要留）。返回更新后的列表 + 本次改动条数。

    范围限制（都是「不要全量」来的）：只扫该环境绑定的项目、近 7 天；元信息每次
    都对账，**消息内容**只在 ``include_content`` 时导入一份尾部副本——那是
    「同步数据」按钮的事，加减资源时不重复跑（每次多 N 趟节点往返）。

    同样走 raw SQL：``mc_ac_node_sessions`` 归控制面注册，数据进程不注册它。
    """
    from datetime import datetime, timezone

    from db import PostgresClient

    row = await get_environment(user_id, env_id)
    env_key = str(row.id)
    provider = str(row.provider or "").strip().lower()

    # 读节点上编辑器的真实会话记录。节点只读不写——会话历史是编辑器自己的事。
    from .nodes_service import nodes_service

    try:
        reported = await nodes_service.list_environment_sessions(
            row.node_id, env_key, provider,
            workdir=str(row.external_path or "").strip(), days=7,
        )
    except Exception as exc:  # noqa: BLE001
        raise EnvironmentError("failed_precondition", f"节点不可用：{exc}") from exc

    if not PostgresClient.pool:
        raise EnvironmentError("failed_precondition", "数据库连接池不可用")

    # 保留 tzinfo：账本两列都是 timestamptz，给 asyncpg 一个 naive datetime 会被
    # 当成本地时间解读，而这些值本该是 UTC（控制面写的就是 UTC）。
    now = datetime.now(timezone.utc)
    seen = [str(item.get("session_id") or "").strip() for item in reported]
    seen = [s for s in seen if s]

    def _affected(tag: str) -> int:
        return _affected_rowcount(tag)

    changed = 0
    async with PostgresClient.pool.acquire() as conn:
        for item in reported:
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            # 每一列都得写：这张表是控制面建的，除了 editor_id 那几个可空列，其余
            # 全是 NOT NULL 且**没有库级默认值**（默认值在它的模型层）。少给一列就
            # 是 NotNullViolation——而对账扫的是磁盘上编辑器自己的会话记录，那些
            # 归属信息（哪个任务、什么模型）我们根本不知道，只能留空。
            #
            # 只写我们确实知道的：环境绑的项目 + 会话属于哪个环境 + 标题。冲突时也
            # 不碰这些（下面的 SET 只改 env_id/provider/title/status/updated_at），
            # 所以控制面派发时写好的 task_id/model 不会被对账抹掉。
            #
            # ``platform_name`` 明确不进 INSERT 的值（给 ''）也绝不进 SET：那是用户
            # 在平台起的名字，写者只有改名接口。对账只拥有 ``title``。
            #
            # ``last_activity_at`` 是节点报的**会话最后一次活动**（编辑器记录里最后
            # 一条的时间），不是"行什么时候被碰的"（那是 updated_at，派发也会写）。
            # 列表页展示的就是它。节点没给（旧扫描命令）就留 NULL。
            title = _clean_session_title(str(item.get("title") or ""))
            # 权限档/思考等级从会话记录读出（扫描命令的第 5/6 列）。与 platform_name
            # 同一纪律：**只在非空时写，空值永不覆盖已有值**——空只说明这次没读到
            # （opencode 读不到/文件超限），不代表会话真回到了默认。
            mode_meta = str(item.get("mode") or "").strip()[:64]
            effort = str(item.get("effort") or "").strip()[:16]
            try:
                activity_epoch = int(item.get("updated_at") or 0)
            except (TypeError, ValueError):
                activity_epoch = 0
            activity = (
                datetime.fromtimestamp(activity_epoch, tz=timezone.utc)
                if activity_epoch > 0 else None
            )
            tag = await conn.execute(
                "INSERT INTO mc_ac_node_sessions "
                "(session_id, node_id, project_id, task_id, editor_id, editor_session_id, "
                " provider, model, mode, status, env_id, title, platform_name, "
                " applied_revision, effective_revision, created_at, updated_at, "
                " last_activity_at, effort) "
                "VALUES ($1, $2, $3, '', '', '', $4, '', $5, 'running', $6, $7, '', "
                " 0, 0, $8, $8, $9, $10) "
                "ON CONFLICT (session_id) DO UPDATE "
                "SET env_id=EXCLUDED.env_id, provider=EXCLUDED.provider, "
                "    title=EXCLUDED.title, status='running', updated_at=$8, "
                "    last_activity_at=EXCLUDED.last_activity_at, "
                "    mode=CASE WHEN EXCLUDED.mode <> '' THEN EXCLUDED.mode "
                "             ELSE mc_ac_node_sessions.mode END, "
                "    effort=CASE WHEN EXCLUDED.effort <> '' THEN EXCLUDED.effort "
                "              ELSE mc_ac_node_sessions.effort END "
                "WHERE mc_ac_node_sessions.status IS DISTINCT FROM 'running' "
                "   OR mc_ac_node_sessions.env_id IS DISTINCT FROM EXCLUDED.env_id "
                "   OR mc_ac_node_sessions.title IS DISTINCT FROM EXCLUDED.title "
                # 只有时间变新了也要写：同一会话持续在聊，标题/状态都没动，
                # 不加这条对账就会把"最新活动时间"冻在第一次写入的那一刻。
                "   OR mc_ac_node_sessions.last_activity_at IS DISTINCT "
                "      FROM EXCLUDED.last_activity_at "
                # 权限档/思考等级变了才动（且只在非空时判，空扫描值不该触发重写）。
                "   OR (EXCLUDED.mode <> '' AND mc_ac_node_sessions.mode IS DISTINCT FROM EXCLUDED.mode) "
                "   OR (EXCLUDED.effort <> '' AND mc_ac_node_sessions.effort IS DISTINCT FROM EXCLUDED.effort)",
                session_id, row.node_id, str(row.project_id or ""),
                str(item.get("provider") or provider or ""), mode_meta, env_key, title, now,
                activity, effort,
            )
            changed += _affected(tag)
        # 库里有、节点上没有的 = 已经结束。只改状态，不删行。
        #
        # 时间戳与「不在列表里」都用可移植的写法，而不是 ``now()`` 和
        # ``= ANY($n::text[])``：后两者是 asyncpg/PG 专属，测试里就没法用 sqlite
        # 真跑这段 SQL（而真跑才有意义——冲突跳过、按环境筛、只标不删，都在 SQL 里）。
        # ``seen`` 是近 7 天该环境的会话数，展开成占位符列表的规模完全可控。
        stale_sql = (
            "UPDATE mc_ac_node_sessions SET status='ended', updated_at=$1 "
            "WHERE env_id=$2 AND status IS DISTINCT FROM 'ended'"
        )
        stale_params: list = [now, env_key]
        if seen:
            stale_sql += " AND session_id NOT IN (" + ", ".join(
                f"${n}" for n in range(3, 3 + len(seen))
            ) + ")"
            stale_params.extend(seen)
        # 节点一个会话都没报时不加 NOT IN：条件恒真，该环境的会话全标结束。
        tag = await conn.execute(stale_sql, *stale_params)
        changed += _affected(tag)

    # 内容副本：对刚对账到的会话各拉一段尾部入库，这样「同步数据」之后详情页
    # 首屏就有东西可看（而不是空库 + 等轮询）。并发限 4——逐个串行会把同步
    # 拖成分钟级，全放开又会把节点控制面压满。失败只记日志：对账已经成功，
    # 内容拉不到不该让整次同步变红。
    if include_content and seen:
        import asyncio as _asyncio

        semaphore = _asyncio.Semaphore(4)

        async def _one(sid: str) -> None:
            async with semaphore:
                try:
                    await import_env_session_items(user_id, env_key, sid, mode="tail")
                except Exception as exc:  # noqa: BLE001 — 单条失败不牵连别的会话
                    logger.info("[env] transcript import failed for {}:{}", sid, exc)

        await _asyncio.gather(*(_one(sid) for sid in seen))

    sessions, total = await _read_env_sessions(env_key, limit=50, offset=0)
    return {
        # 返回第一页（重列后页面回到顶部是自然的）+ 总数，让页脚一次就对。
        "sessions": sessions,
        "total": total,
        "changed": changed,
    }


async def sync_environment_session(
    user_id: str, env_id: str, session_id: str
) -> dict:
    """**单独**同步一条会话：重扫节点上该编辑器的会话记录，只更新这一行的
    标题/状态/最后活动时间——不动其他行。顺带把内容副本拉一段尾部入库。

    详情页的「同步」按钮走这里：用户在看某条会话时它可能还在节点上继续跑，
    点一下把最新状态 + 最新内容都拉过来。节点不再报这条 = 它结束了，标 ended
    （不删，历史要留）。
    """
    from datetime import datetime, timezone

    from db import PostgresClient

    row = await get_environment(user_id, env_id)
    env_key = str(row.id)
    session_key = str(session_id or "").strip()
    if not session_key:
        raise EnvironmentError("invalid_argument", "session_id 不能为空")
    provider = str(row.provider or "").strip().lower()

    from .nodes_service import nodes_service

    try:
        reported = await nodes_service.list_environment_sessions(
            row.node_id, env_key, provider,
            workdir=str(row.external_path or "").strip(), days=7,
        )
    except Exception as exc:  # noqa: BLE001
        raise EnvironmentError("failed_precondition", f"节点不可用：{exc}") from exc

    if not PostgresClient.pool:
        raise EnvironmentError("failed_precondition", "数据库连接池不可用")

    now = datetime.now(timezone.utc)
    hit = next(
        (item for item in reported
         if str(item.get("session_id") or "").strip() == session_key),
        None,
    )
    async with PostgresClient.pool.acquire() as conn:
        if hit is None:
            # 节点上没有了：这条结束了。只改状态，不删行。
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET status='ended', updated_at=$2 "
                "WHERE session_id=$1",
                session_key, now,
            )
        else:
            title = _clean_session_title(str(hit.get("title") or ""))
            mode_meta = str(hit.get("mode") or "").strip()[:64]
            effort = str(hit.get("effort") or "").strip()[:16]
            try:
                activity_epoch = int(hit.get("updated_at") or 0)
            except (TypeError, ValueError):
                activity_epoch = 0
            activity = (
                datetime.fromtimestamp(activity_epoch, tz=timezone.utc)
                if activity_epoch > 0 else None
            )
            # 与全量对账同一条 upsert：platform_name 绝不进 SET（写者只有改名接口）。
            await conn.execute(
                "INSERT INTO mc_ac_node_sessions "
                "(session_id, node_id, project_id, task_id, editor_id, editor_session_id, "
                " provider, model, mode, status, env_id, title, platform_name, "
                " applied_revision, effective_revision, created_at, updated_at, "
                " last_activity_at, effort) "
                "VALUES ($1, $2, $3, '', '', '', $4, '', $5, 'running', $6, $7, '', "
                " 0, 0, $8, $8, $9, $10) "
                "ON CONFLICT (session_id) DO UPDATE "
                "SET env_id=EXCLUDED.env_id, provider=EXCLUDED.provider, "
                "    title=EXCLUDED.title, status='running', updated_at=$8, "
                "    last_activity_at=EXCLUDED.last_activity_at, "
                "    mode=CASE WHEN EXCLUDED.mode <> '' THEN EXCLUDED.mode "
                "             ELSE mc_ac_node_sessions.mode END, "
                "    effort=CASE WHEN EXCLUDED.effort <> '' THEN EXCLUDED.effort "
                "              ELSE mc_ac_node_sessions.effort END "
                "WHERE mc_ac_node_sessions.status IS DISTINCT FROM 'running' "
                "   OR mc_ac_node_sessions.env_id IS DISTINCT FROM EXCLUDED.env_id "
                "   OR mc_ac_node_sessions.title IS DISTINCT FROM EXCLUDED.title "
                "   OR mc_ac_node_sessions.last_activity_at IS DISTINCT "
                "      FROM EXCLUDED.last_activity_at "
                "   OR (EXCLUDED.mode <> '' AND mc_ac_node_sessions.mode IS DISTINCT FROM EXCLUDED.mode) "
                "   OR (EXCLUDED.effort <> '' AND mc_ac_node_sessions.effort IS DISTINCT FROM EXCLUDED.effort)",
                session_key, row.node_id, str(row.project_id or ""),
                str(hit.get("provider") or provider or ""), mode_meta, env_key, title, now,
                activity, effort,
            )

    # 内容副本：这一条会话的尾部一并入库，详情页「同步」按钮一次拿到状态 + 内容。
    try:
        await import_env_session_items(user_id, env_key, session_key, mode="tail")
    except Exception as exc:  # noqa: BLE001 — 元信息已更新，内容拉不到不该让同步失败
        logger.info("[env] transcript import failed for {}:{}", session_key, exc)

    sessions, _total = await _read_env_sessions(env_key)
    for item in sessions:
        if item["session_id"] == session_key:
            return item
    raise EnvironmentError("not_found", "该会话不在此环境（先与节点同步一次）")


# ── 会话历史：显示副本 ────────────────────────────────────────────────────
#
# 节点是会话内容的**唯一来源**（编辑器自己的 jsonl / sqlite），库里
# ``mc_ac_node_sessions.transcript`` 只是同步/导入时留下的**显示副本**：
# ``{"items": [...], "head": bool, "window_lines": int}``。
#
# 详情页因此**永远只读库**——节点控制面一抖（host_exec 超时）不该让详情页打不开。
# 想拿到新内容只有两条路，且都先写库再把新条目还给调用方：
#
# * ``import_env_session_items(mode="tail")`` —— 轮询/同步用，读节点尾部一段；
# * ``import_env_session_items(mode="history")`` —— 用户点「查看历史」，把窗口
#   调大去够更早的行。
#
# 顺序保证靠两件事实：节点记录只在**末尾追加**（jsonl append-only / sqlite 只增），
# 因此「窗口」永远是文件的一段**后缀**；已存条目是**连续的一段文件区间**。所以
# 合并 = ``existing 中已滑出窗口的（更早） + 本次窗口的顺序输出``，两端都不需要排序。

_TAIL_WINDOW_DEFAULT = 400      # 轮询/同步：读尾部 400 行（够渲染，也够几分钟轮询）
_TAIL_WINDOW_MAX = 6000         # 「查看历史」的窗口上限：45MB 级文件再大就撞 host_exec 输出上限
_HISTORY_GROW = 800             # 每次点「查看历史」往前多够多少行

def _empty_transcript() -> dict[str, Any]:
    """空副本每次现造——共享一个常量会暴露同一个 list 实例，被原地 append 就污染全局。"""
    return {"items": [], "head": False, "window_lines": 0}


async def _read_transcript_store(env_id: str, session_id: str) -> dict:
    """读账本里那条会话的显示副本。**只读库**，不碰节点。"""
    from db import PostgresClient

    if not PostgresClient.pool:
        return {"items": [], "head": False, "window_lines": 0}
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT transcript FROM mc_ac_node_sessions "
            "WHERE session_id=$1 AND env_id=$2",
            session_id, env_id,
        )
    raw = row["transcript"] if row is not None else None
    if not raw:
        return _empty_transcript()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):  # 半截/手改坏的 JSON 当没导入过，别让详情页 500
        return _empty_transcript()
    if not isinstance(data, dict):
        return _empty_transcript()
    items = data.get("items")
    return {
        "items": [it for it in items if isinstance(it, dict)] if isinstance(items, list) else [],
        "head": bool(data.get("head")),
        "window_lines": int(data.get("window_lines") or 0),
    }


async def _write_transcript_store(
    env_id: str, session_id: str, *, items: list[dict], head: bool, window_lines: int,
) -> None:
    """写回显示副本。命中 0 行 = 这条会话不在本环境，说明调用方没先对账过。"""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise EnvironmentError("failed_precondition", "数据库连接池不可用")
    payload = json.dumps(
        {"items": items, "head": bool(head), "window_lines": int(window_lines)},
        ensure_ascii=False,
    )
    async with PostgresClient.pool.acquire() as conn:
        tag = await conn.execute(
            "UPDATE mc_ac_node_sessions SET transcript=$1 "
            "WHERE session_id=$2 AND env_id=$3",
            payload, session_id, env_id,
        )
    if _affected_rowcount(tag) == 0:
        raise EnvironmentError("not_found", "该会话不在此环境（先与节点同步一次）")


async def import_env_session_items(
    user_id: str,
    env_id: str,
    session_id: str,
    *,
    mode: str = "tail",
    window_lines: int = 0,
) -> dict:
    """读节点一段会话记录 → **先入库** → 返回本次新增的条目。

    前端轮询（``mode="tail"``，约 2.5s 一次）与「查看历史」（``mode="history"``）
    都走这里，所以它既是增量导入的唯一入口，也是把内容放进库的唯一写者。

    返回 ``{before, after, total, head, window_lines, unsupported}``：
    ``before`` 是**比已存最老条目还早**的新条目（历史加载，调用方前置）、
    ``after`` 是比已存最新条目还新的新条目（轮询增量，调用方追加）。两者互斥——
    已存条目是连续一段文件区间，任何新条目只会出现在这一段的左边或右边。

    未知编辑器不报错，回 ``unsupported`` 让详情页降级成只显信息。
    """
    from .nodes_service import nodes_service
    from .routes_nodes_files import (
        _item_fingerprint,
        _parse_transcript_lines,
        _session_transcript_command,
        _stable_item_id,
    )
    from .node_client import get_local_node_client

    row = await get_environment(user_id, env_id)
    env_key = str(row.id)
    session_key = str(session_id or "").strip()
    if not session_key:
        raise EnvironmentError("invalid_argument", "session_id 不能为空")
    provider = str(row.provider or "").strip().lower()

    store = await _read_transcript_store(env_key, session_key)
    existing: list[dict] = store["items"]

    # 窗口。规则有两条，都是为了**不丢内容**：
    #
    # 1. tail 轮询的窗口必须**盖住已存的那一段**（+一段余量）。节点文件在两次轮询
    #    之间长出的行数如果超过窗口，中间那段既不在已存里、也不在新窗口里，就会
    #    被永久跳过。让窗口随已存增长，新窗口永远与已存重叠。
    # 2. history 要够更早，所以比已存窗口再大一段；窗口只增不减，否则轮询过一次
    #    后 history 反而够不着更早的行了。
    stored_lines = int(store["window_lines"] or 0)
    if int(window_lines or 0) > 0:
        window = int(window_lines)
    elif mode == "history":
        window = min(stored_lines + _HISTORY_GROW, _TAIL_WINDOW_MAX)
    else:
        # tail：窗口盖住已存区间 + 一段余量，保证两次轮询之间文件长出的内容
        # 不会落在「已存之后、新窗口之前」的缝里被永久跳过（见上方不变量 1）。
        # 但窗口增长本身**不是**列表越堆越多的原因——那来自下方 tail 分支把窗口
        # 新暴露的更早历史当成 after 追加（已按位置切分修掉）。窗口随已存增长
        # 只保证不丢中间段，代价是多解析几行，收敛在 _TAIL_WINDOW_MAX。
        window = min(max(_TAIL_WINDOW_DEFAULT, stored_lines + _TAIL_WINDOW_DEFAULT),
                     _TAIL_WINDOW_MAX)
    window = max(1, window)
    at_max = window >= _TAIL_WINDOW_MAX
    if mode == "history" and (store["head"] or at_max):
        # 已经读到文件最早（或窗口撞到上限还没读到头）——如实回没有新东西，
        # 不假装还能继续往前。前端据此收掉「查看历史」按钮。
        return {"before": [], "after": [], "total": len(existing),
                "head": store["head"], "window_lines": store["window_lines"],
                "unsupported": False}

    home = await nodes_service.environment_home_path(row.node_id, env_key)
    is_windows = await nodes_service.node_is_windows(row.node_id)
    if not home:
        home = await nodes_service.node_user_home(row.node_id, is_windows)
    if not home:
        raise EnvironmentError("failed_precondition", "无法定位该环境的 HOME（节点未上报）")

    command = _session_transcript_command(
        provider, is_windows=is_windows, home=home, session_id=session_key,
        tail_lines=window,
    )
    if command is None:
        return {"before": [], "after": [], "total": len(existing),
                "head": True, "window_lines": store["window_lines"],
                "unsupported": True}

    try:
        result = await get_local_node_client().host_exec(
            row.node_id, command, cwd="", timeout_ms=60_000, max_output_bytes=16_000_000
        )
    except Exception as exc:  # noqa: BLE001 — 节点不可达是常态，别冒成 500
        raise EnvironmentError("failed_precondition", f"读取会话历史失败：{exc}") from exc

    stdout = str(result.get("stdout") or "")
    raw_lines = [ln for ln in stdout.splitlines() if ln.strip()]
    # limit 交给行数而**不是**截断条目：窗口内一条 assistant 帧能出多条 item，
    # 拿 window 当条目上限会把窗口尾部截掉。
    parsed, _cap = _parse_transcript_lines(stdout, limit=max(window * 4, 200), provider=provider)

    # 改写稳定 id（工具用裸 call_id，消息用内容指纹），再按指纹去重。
    normalized: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for item in parsed:
        fp = _item_fingerprint(item)
        if fp in seen:
            continue
        seen.add(fp)
        stable = dict(item)
        stable["id"] = _stable_item_id(item, fp)
        normalized.append((fp, stable))
    existing_fps = {_item_fingerprint(it) for it in existing}

    # 窗口是文件的一段后缀 → 已存里落在窗口**之外**的一定更早，保序留在最前；
    # 窗口内的按文件顺序输出。这样合并结果天然连续，不用按时间戳排序（秒级时间
    # 做不稳排，工具开/闭帧还会撞同一秒）。
    window_fps = {fp for fp, _ in normalized}
    older = [it for it in existing if _item_fingerprint(it) not in window_fps]
    merged = older + [it for _, it in normalized]

    added = [it for fp, it in normalized if fp not in existing_fps]
    # 位置切分对 tail / history **都**成立（不能只给 history）：tail 的窗口随已存增长，
    # 每轮都可能把更早的行新捞进窗口——那些是「更早的历史」，不是新输出。曾把它们
    # 全塞进 after，前端按增量追加到列表尾部：几百条旧记录堆到底部、reduceItems +
    # 重组件把详情页拖死。按位置切开后，左端（更早）归 before（前端轮询只读 after、
    # 忽略 before，且它们已按正确位置入库，上翻 loadOlder 自然能看到）。
    if existing and added:
        # 已存是连续区间 → 新条目只可能出现在它的左端或右端（不可能钻进中间）。
        # 用「在 merged 中的位置」判断，比时间戳稳得多：一次遍历拿到每个条目的下标，
        # existing 的位置区间即 [first, last]，区间外的新条目按左右分到 before/after。
        positions: dict[int, int] = {}          # id(item) -> merged 下标
        existing_positions: list[int] = []
        for i, it in enumerate(merged):
            positions[id(it)] = i
            if _item_fingerprint(it) in existing_fps:
                existing_positions.append(i)
        first_existing = min(existing_positions)
        last_existing = max(existing_positions)
        before, after = [], []
        for it in added:
            idx = positions[id(it)]
            if idx < first_existing:
                before.append(it)
            elif idx > last_existing:
                after.append(it)
            # 落在 existing 中间理论上不会发生（existing 连续）；真发生就既不前也不后，
            # 属于并发导入的竞争，交给下一轮对齐，不在这里猜。
    else:
        before, after = [], added

    # 窗口只增不减；读满窗口而文件比窗口长 → 还没到最早。
    head = bool(store["head"]) or (len(raw_lines) < window)
    await _write_transcript_store(
        env_key, session_key, items=merged, head=head,
        window_lines=max(store["window_lines"], window),
    )
    return {
        "before": before, "after": after, "total": len(merged),
        "head": head, "window_lines": window, "unsupported": False,
    }


async def get_session_transcript(
    user_id: str,
    env_id: str,
    session_id: str,
    *,
    limit: int = 60,
    before_idx: int | None = None,
) -> dict:
    """读一条会话的**已导入历史**（会话详情页用）——**只读库，绝不碰节点**。

    这是详情页"永远先读库"的那一步：节点抖动、会话刚同步没内容、编辑器不认，
    这里都只回库里的东西（空就空），不会像以前那样直接去节点现读、一超时就 500。
    要拉新内容由调用方显式调 :func:`import_env_session_items`。

    ``before_idx`` 是调用方**已持有的最老条目**的绝对下标（独占上界），于是本页取
    ``items[before_idx-limit : before_idx]``；不传则取最新一段。返回带 ``total`` /
    ``oldest_idx`` / ``has_more``（前面还有）/ ``head``（节点文件是否已读到最早，
    决定要不要显示「查看历史」）。

    items 与任务对话同形状（``user_input``/``agent_message``/``reasoning``/
    ``tool_call``，工具开闭帧同 id 供 ``reduceItems`` 合并），详情页复用任务渲染管线。
    """
    row = await get_environment(user_id, env_id)
    env_key = str(row.id)
    session_key = str(session_id or "").strip()
    provider = str(row.provider or "").strip().lower()

    store = await _read_transcript_store(env_key, session_key)
    items: list[dict] = store["items"]
    total = len(items)

    size = max(1, min(int(limit or 60), 500))
    end = total if before_idx is None else max(0, min(int(before_idx), total))
    start = max(0, end - size)

    meta = _session_meta(row, session_key, provider)
    meta["has_history"] = total > 0
    # 这一串记录里最后出现的模型名——顶部只读输入区显示"这个会话用的什么模型"。
    for item in reversed(items):
        if item.get("model"):
            meta["model"] = str(item["model"])
            break

    return {
        "session": meta,
        "items": items[start:end],
        "total": total,
        "oldest_idx": start,
        "has_more": start > 0,
        # head=True = 节点文件的最早一条已在库里，「查看历史」没有更多可加载。
        # 空库且从未导过 → False（先给按钮），由首次 tail 导入去校准。
        "head": bool(store["head"]) if total else False,
    }


def _session_meta(row: TaskEnvironment, session_id: str, provider: str) -> dict:
    """会话详情页的元信息块：归属（环境/节点/编辑器）+ 目录来源。"""
    mode = str(row.workspace_mode or "")
    return {
        "session_id": session_id,
        "env_id": str(row.id),
        "env_name": row.name,
        "node_id": row.node_id,
        "provider": provider,
        "workspace_mode": mode,
        "isolated": resolve_isolated(mode),
        "external_path": row.external_path or None,
    }


async def rename_environment_session(
    user_id: str, env_id: str, session_id: str, platform_name: str
) -> dict:
    """写平台会话名称（用户改名）。**唯一**写 ``platform_name`` 的入口。

    节点对账只拥有 ``title``；这条写在 ``platform_name`` 上，所以改名天然活得过
    任何后续对账。空串 = 清除改名，列表回退显示节点同步来的标题。会话必须真的挂
    在这个环境上（env_id 一致才允许写）——别处的会话 id 不能借道这里改名。
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise EnvironmentError("failed_precondition", "数据库连接池不可用")
    name = str(platform_name or "").strip()[:200]
    session_key = str(session_id or "").strip()
    if not session_key:
        raise EnvironmentError("invalid_argument", "session_id 不能为空")

    # 归属校验走环境行：确认这个环境是 caller 的，而不是 caller 指哪改哪。
    row = await get_environment(user_id, env_id)
    env_key = str(row.id)
    async with PostgresClient.pool.acquire() as conn:
        tag = await conn.execute(
            "UPDATE mc_ac_node_sessions SET platform_name=$1 "
            "WHERE session_id=$2 AND env_id=$3",
            name, session_key, env_key,
        )
    if _affected_rowcount(tag) == 0:
        raise EnvironmentError("not_found", "该会话不在此环境（先与节点同步一次）")
    sessions, _ = await _read_env_sessions(env_key)
    for item in sessions:
        if item["session_id"] == session_key:
            return item
    raise EnvironmentError("not_found", "该会话不在此环境")



# 账本 task_id 的瞬时占位值：一次采用正在进行中。不是任务 id，绝不返回给页面。
_ADOPT_CLAIM = "__adopting__"


async def _seed_task_history_if_blank(
    user_id: str, env_id: str, session_key: str, task_id: str
) -> None:
    """把会话的已导入历史灌进任务轨迹——**只在任务还是空的时候**。

    采用外部编辑器会话时，那条会话的对话早就发生了，平台只是刚给它一个任务身份。
    不灌的话任务详情页一片空白，用户会以为"同步为任务"把历史弄丢了。

    两个要点：

    * **只在空任务上灌**：本修复之前采用出来的任务都是空的（老代码只建壳），用户
      再点一次 adopt 会走 ``reused`` 早返回——这里顺手把它补上（自愈）。反过来，
      已经聊过的任务绝不能再灌：那会把历史重复插进已有对话中间。
    * **库里没有就先向节点要一段**：用户不必先手动同步一次。

    best-effort：任何失败只记日志——历史显示不全，好过采用本身失败。
    """
    from .models_task import TaskEvent

    try:
        task_pk = _uuid(task_id, field="task_id")
        if await TaskEvent.filter(task_id=task_pk).exists():
            return  # 已经有对话（用户在任务页发过消息），绝不重复灌
        store = await _read_transcript_store(env_id, session_key)
        if not store["items"]:
            await import_env_session_items(user_id, env_id, session_key, mode="tail")
            store = await _read_transcript_store(env_id, session_key)
        if not store["items"]:
            return  # 节点上也没有内容（会话被清理/编辑器不认），如实留空
        from .task_service import task_service

        await task_service.seed_history_from_items(task_pk, store["items"])
    except Exception as exc:  # noqa: BLE001 — 种子失败不该让采用失败
        logger.info("[env] seed task history failed for {}: {}", task_id, exc)


async def adopt_environment_session(
    user_id: str,
    env_id: str,
    session_id: str,
    *,
    parent_api_key_id: int | None = None,
    model_id: str | None = None,
    model_mapping: dict | None = None,
    models: list[str] | None = None,
    usage_limit: dict | None = None,
    rate_limit: dict | None = None,
    expires_at: float | None = None,
    editor_provider_whitelist: list[str] | None = None,
    selection_strategy: str | None = None,
) -> dict:
    """二次同步：把一条外部编辑器会话**采用成平台任务**。

    为什么需要第二次同步：第一段同步只搬会话元信息（id/标题/状态）——它不花什么
    成本，列表才有东西可看。要「编辑」某条会话就得给它一个平台身份：任务。任务
    才有 Key/模型/事件流/计费归属，也才会出现在项目任务列表里。

    四条关键约束：

    1. **桥接 id 空间**。平台任务的 ledger session_id 就是 ``mc_tasks.id``，而扫上来
       的外部会话是编辑器自己的 uuid——两套 id。链接落在 ``Task.source_editor_session_id``
       （legacy-session 那条既有链路），账本 ``task_id`` 只是这一页的跳转缓存。
    2. **续接原会话**。``_session_tags["resume_thread_id"]`` 让节点把编辑器 thread
       指回原会话。这个 tag **不落 config_snapshot**，所以采用**不做任何派发**
       （``no_dispatch``，见 4）——续接的提示由首条消息的懒派发从
       ``Task.source_editor_session_id`` 重建（见 ``_dispatch_persisted_task_runtime``）。
    3. **不跑首轮**。``content`` 留空——create_task 明确支持空内容的交互式等待任务。
       把标题当 content 传进去会变成真的第一条消息，采用一次就烧一轮。
    4. **参数来源分两类**（用户口径）：会话模式（权限）与思考等级**从节点会话同步**
       （账本 ``mode``/``effort`` 列，扫描时从会话记录读出），不进请求体；父 API Key、
       模型映射列表、API Key 限制由**用户选择**，从请求体透传。没给父 Key 仍走
       ``_allow_keyless_dispatch``，平台不铸子 Key、不下发 llm 配置，节点上的编辑器
       用它自己的登录态跑。

    幂等：同一条会话重复采用返回同一个任务（先看账本 task_id，再看 Task 表，
    两处都命中不了才新建）；并发双击由账本上的原子抢占挡住，不会出现两个任务。
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise EnvironmentError("failed_precondition", "数据库连接池不可用")

    row = await get_environment(user_id, env_id)
    env_key = str(row.id)
    session_key = str(session_id or "").strip()
    if not session_key:
        raise EnvironmentError("invalid_argument", "session_id 不能为空")
    # 任务必须归属项目——「进项目任务列表」的前提。环境没绑项目就没有归属可言。
    if row.project_id is None:
        raise EnvironmentError(
            "failed_precondition", "该环境未绑定项目，先把环境挂到项目下再同步会话"
        )

    async with PostgresClient.pool.acquire() as conn:
        ledger = await conn.fetchrow(
            "SELECT session_id, provider, status, title, platform_name, task_id, "
            " mode, effort "
            "FROM mc_ac_node_sessions WHERE session_id=$1 AND env_id=$2",
            session_key, env_key,
        )
        if ledger is None:
            raise EnvironmentError("not_found", "该会话不在此环境（先与节点同步一次）")

        existing_task = str(ledger["task_id"] or "").strip()
        if existing_task and existing_task != _ADOPT_CLAIM:
            # 跳转缓存可能指向一个已删除的任务（老版本删任务不清账本）：验证一下，
            # 死了就清掉缓存，继续走下面的新建——别把死 id 当「已同步」返回。
            from .models_task import Task

            still_there = await Task.get_or_none(
                id=_uuid(existing_task, field="task_id"), deleted_at=None
            )
            if still_there is not None:
                await _seed_task_history_if_blank(user_id, env_key, session_key, existing_task)
                return {"task_id": existing_task, "reused": True}
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET task_id='' "
                "WHERE session_id=$1 AND env_id=$2",
                session_key, env_key,
            )
        if existing_task == _ADOPT_CLAIM:
            raise EnvironmentError("failed_precondition", "该会话正在同步中，请稍后刷新")

    # 账本 task_id 不是唯一真相：它是这一页的跳转缓存，清过库/老行都可能空着。
    # 权威链接是 Task.source_editor_session_id（legacy-session 那条既有链路用的
    # 就是它），所以空的时候按它反查一次，别重复建任务。
    from .models_task import Task

    adopted = (
        await Task.filter(source_editor_session_id=session_key, deleted_at=None)
        .order_by("-created_at")
        .first()
    )
    if adopted is not None:
        async with PostgresClient.pool.acquire() as conn:
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET task_id=$1 "
                "WHERE session_id=$2 AND env_id=$3 AND (task_id IS NULL OR task_id='')",
                str(adopted.id), session_key, env_key,
            )
        await _seed_task_history_if_blank(user_id, env_key, session_key, str(adopted.id))
        return {"task_id": str(adopted.id), "reused": True}

    async with PostgresClient.pool.acquire() as conn:
        # 原子抢占：只有一个请求能把空位填上占位符，双击的另一个拿到 0 行 → 上面
        # 的「正在同步中」分支。占位符不是任务 id，成功/失败都要覆盖掉。
        claimed = _affected_rowcount(
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET task_id=$1 "
                "WHERE session_id=$2 AND env_id=$3 "
                "AND (task_id IS NULL OR task_id='')",
                _ADOPT_CLAIM, session_key, env_key,
            )
        )
        if not claimed:
            raise EnvironmentError("failed_precondition", "该会话正在同步中，请稍后刷新")

    try:
        provider = str(ledger["provider"] or row.provider or "").strip().lower()
        display = _session_display_name(
            str(ledger["platform_name"] or ""), str(ledger["title"] or ""), session_key
        )
        from .task_service import task_service

        created = await task_service.create_task(
            user_id,
            {
                # 空 content = 交互式等待任务（见 docstring 第 3 条）。
                "content": "",
                "node_id": str(row.node_id or ""),
                "provider": provider,
                "cli_name": provider,
                "env_id": env_key,
                "env_name": row.name,
                # 会话模式（权限）/思考等级：**从节点会话同步来**（账本列），不由用户选。
                "mode": str(ledger["mode"] or "").strip() or None,
                "reasoning_effort": str(ledger["effort"] or "").strip() or None,
                # 用户选的：父 Key / 模型映射 / 限制。
                "parent_api_key_id": parent_api_key_id,
                "model_id": (model_id or None),
                "model_mapping": model_mapping or None,
                "models": models if models else None,
                "usage_limit": usage_limit,
                "rate_limit": rate_limit,
                "expires_at": expires_at,
                "editor_provider_whitelist": editor_provider_whitelist,
                "selection_strategy": selection_strategy,
                # 没选 Key 也要能同步：让 create_task 放行无 Key（见其守卫）。
                "_allow_keyless_dispatch": not parent_api_key_id,
                "extra": {"project_id": str(row.project_id)},
                # 外部会话 uuid —— 落 Task.source_editor_session_id，既是 legacy-session
                # 那条既有反查链路的键，也是懒派发时重建 resume_thread_id 的来源。
                "editor_session_id": session_key,
            },
            # 采用**不做任何节点动作**：只入库成任务 + 设参数。会话/工作区/运行时
            # 全部推迟到首条消息的懒派发，那时才带着（从 source_editor_session_id
            # 重建的）resume 提示去续接原会话。让「同步为任务」秒级完成。
            no_dispatch=True,
        )
        task_id = str(created.get("id") or created.get("task_id") or "").strip()
        if not task_id:
            raise EnvironmentError("failed_precondition", "同步失败：未返回任务 id")
        task_pk = _uuid(task_id, field="task_id")

        # 任务名用会话显示名补齐：content 为空 → create_task 那侧派生不出标题，
        # 列表里会是个无名任务。
        from .models_task import Task

        await Task.filter(id=task_pk).update(title=display[:120])

        # 把已导入的历史灌进任务的对话轨迹：否则用户点「同步为任务」后看到的
        # 是一片空白（任务才刚建，事件流里什么都没有），以为历史丢了。
        await _seed_task_history_if_blank(user_id, env_key, session_key, task_id)

        async with PostgresClient.pool.acquire() as conn:
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET task_id=$1 "
                "WHERE session_id=$2 AND env_id=$3 AND task_id=$4",
                task_id, session_key, env_key, _ADOPT_CLAIM,
            )
    except EnvironmentError:
        # 释放抢占，否则这条会话永远卡在占位符上，再也同步不了。
        async with PostgresClient.pool.acquire() as conn:
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET task_id='' "
                "WHERE session_id=$1 AND env_id=$2 AND task_id=$3",
                session_key, env_key, _ADOPT_CLAIM,
            )
        raise
    except Exception as exc:
        # 同上释放；create_task 的 ValueError（parent_api_key_required 等）在这里
        # 包成 EnvironmentError——调用方（路由/前端）只认这一种错误类型，裸
        # ValueError 会被当成 500。
        async with PostgresClient.pool.acquire() as conn:
            await conn.execute(
                "UPDATE mc_ac_node_sessions SET task_id='' "
                "WHERE session_id=$1 AND env_id=$2 AND task_id=$3",
                session_key, env_key, _ADOPT_CLAIM,
            )
        raise EnvironmentError("failed_precondition", f"同步失败：{exc}") from exc

    return {"task_id": task_id, "reused": False}


async def adopt_environment_sessions(
    user_id: str,
    env_id: str,
    session_ids: list[str],
    *,
    parent_api_key_id: int | None = None,
    model_id: str | None = None,
    model_mapping: dict | None = None,
    models: list[str] | None = None,
    usage_limit: dict | None = None,
    rate_limit: dict | None = None,
    expires_at: float | None = None,
    editor_provider_whitelist: list[str] | None = None,
    selection_strategy: str | None = None,
) -> dict:
    """批量二次同步：把选中的多条会话一次都采用成平台任务。

    逐条走 :func:`adopt_environment_session`（它自带幂等与抢占），一条失败不连累
    其余——用户勾了 10 条里有 1 条环境没绑项目，不该让另外 9 条白点。返回按
    ``adopted`` / ``failed`` 分组，页面据此决定 toast 的口吻。

    参数一批共用：会话模式/思考等级仍逐条从各自会话的账本行读（不共用），
    父 Key/模型/限制是用户在弹框里选的一次性选择。
    """
    # 用户选的那批参数对整批一致；mode/effort 由每条会话各自的账本行决定。
    params = {
        "parent_api_key_id": parent_api_key_id,
        "model_id": model_id,
        "model_mapping": model_mapping,
        "models": models,
        "usage_limit": usage_limit,
        "rate_limit": rate_limit,
        "expires_at": expires_at,
        "editor_provider_whitelist": editor_provider_whitelist,
        "selection_strategy": selection_strategy,
    }
    adopted: list[dict] = []
    failed: list[dict] = []
    for raw in session_ids or []:
        session_key = str(raw or "").strip()
        if not session_key:
            continue
        try:
            result = await adopt_environment_session(user_id, env_id, session_key, **params)
        except EnvironmentError as exc:
            failed.append({"session_id": session_key, "error": exc.message})
            continue
        adopted.append({"session_id": session_key, **result})
    return {"adopted": adopted, "failed": failed}
