"""User-facing task-environment routes.

An environment is what a user picks before creating a task: it decides which HOME
the editor runs against and, for the shared tier, carries a reusable skill/MCP/
plugin set. These routes are the *user* surface (``/api/v1/teams/environments``) —
environments are user-owned, not administered from the platform console.

Only the shared tier has rows: ``system`` and ``isolated`` are resolved from the
tier name at dispatch time, and ``isolated`` is the initial clean-slate
environment with nothing to manage.

Editing an environment's resources is a real install/uninstall on the node
(``POST .../sync``). A *task* deselecting a resource only changes what that run
activates and never touches these rows — see ``task_service._build_session``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from . import environment_service
from .deps import get_current_team_id, get_current_user
from .environment_service import EnvironmentError
from .models import User
from . import system_env_service

router = APIRouter(prefix="/api/v1/teams", tags=["user-platform-environments"])


_STATUS_BY_CODE = {
    "invalid_argument": 400,
    "permission_denied": 403,
    "not_found": 404,
    "conflict": 409,
    "failed_precondition": 412,
}


async def _guard(coro):
    """Map domain errors onto HTTP status codes.

    Node-transport failures are surfaced as 503 rather than 500: an offline node
    is an expected operational state for environment work (the ledger still
    holds the configuration), not a server bug.
    """
    from .node_client import NodeServerUnavailable
    from .node_client.errors import RPCError
    from .system_env_service import SystemEnvError

    try:
        return await coro
    except EnvironmentError as exc:
        raise HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 400), detail=exc.message)
    except SystemEnvError as exc:
        # 不隔离档的 add/remove 复用系统环境服务（install/remove），它抛
        # SystemEnvError；映射与 EnvironmentError 同口径（404/403/400）。
        raise HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 400), detail=exc.message)
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"节点控制面不可用：{exc}")
    except RPCError as exc:
        raise HTTPException(status_code=502, detail=f"节点调用失败：{exc}")


class CreateEnvReq(BaseModel):
    node_id: str
    name: str
    description: str | None = None
    # Binds the environment to a project, which is what gives it a repository
    # and a git identity. Optional: an environment can still exist for its
    # HOME/resources alone (the historic shape), and an external-path
    # environment needs no project (the tree carries its own git state).
    project_id: str | None = None
    # The working tree is a directory the node did NOT create (an operator's own
    # checkout). Deleting the environment then unbinds instead of removing it.
    # Required when workspace_mode is "external"; refused otherwise. Also
    # required for "fresh" — it is where the clone lands.
    external_path: str | None = None
    # The working directory, i.e. ONE decision (not two):
    #   "external" — 导入：用节点上已有的目录（操作者的），配置跟着全局
    #                → 不隔离。需要 external_path，不 clone。
    #   "fresh"    — 新建：把项目 clone 到指定目录，配置自己一份 → 隔离。
    #                需要 external_path + project_id。
    #   "shared" / "worktree" — 历史形态（平台托管目录，隔离）。
    # 「是否隔离」由它推导（models_environment.resolve_isolated），不是第二个
    # 用户可选项——目录是谁的，配置就跟着谁。
    workspace_mode: str | None = None
    # The editor this environment runs: claude / codex / opencode.
    provider: str | None = None
    # Commands run once when the working tree is first prepared (dependency
    # links, config copies).
    init_commands: list[str] | None = None


class UpdateEnvReq(BaseModel):
    name: str | None = None
    description: str | None = None
    # Lifecycle gate. Disabling revokes the environment's git credential on its
    # next request and refuses new sessions; re-enabling restores the same token.
    enabled: bool | None = None
    init_commands: list[str] | None = None
    # 本环境的会话后台同步间隔（秒）。0 = 关闭，>0 = 专用间隔，缺省 = 跟随全局默认。
    # 注意与 enabled 不同，这里的 None 是「不修改」，不是「跟随默认」——前端要把
    # 「恢复跟随默认」和「关闭」区分开：前者不传本字段，后者传 0。
    session_sync_interval: int | None = None


class AddEnvResourceReq(BaseModel):
    kind: str
    resource_id: str


@router.get("/environments")
async def list_environments(
    node_id: str | None = None,
    project_id: str | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    """The caller's shared environments, optionally narrowed.

    Two callers, two filters: the task-creation flow passes ``node_id`` (an
    environment's HOME lives on the node it was created on, so environments from
    other nodes cannot host the task), while the project workspace passes
    ``project_id`` (an environment is a project's landing on a node).
    """
    return {
        "environments": await _guard(
            environment_service.list_environments(
                str(user.id), node_id=node_id or "", project_id=project_id or ""
            )
        )
    }


@router.post("/environments")
async def create_environment(body: CreateEnvReq, user: User = Depends(get_current_user)) -> dict:
    """Create an environment (id is database-generated)."""
    return await _guard(
        environment_service.create_environment(
            str(user.id),
            node_id=body.node_id,
            name=body.name,
            description=body.description,
            project_id=body.project_id,
            workspace_mode=body.workspace_mode,
            external_path=body.external_path,
            provider=body.provider,
            init_commands=body.init_commands,
        )
    )


@router.get("/environments/{env_id}")
async def get_environment(env_id: str, user: User = Depends(get_current_user)) -> dict:
    """Environment + configured resource set + node inventory, already diffed.

    Each configured skill/plugin is ``installed`` or ``pending``; things the node
    reported with no configured counterpart are ``extra`` (installed by hand).
    MCP rows are ``config`` — never on disk, resolved per task.
    """
    return await _guard(environment_service.environment_detail(str(user.id), env_id))


@router.patch("/environments/{env_id}")
async def update_environment(
    env_id: str, body: UpdateEnvReq, user: User = Depends(get_current_user)
) -> dict:
    """Rename / re-describe / enable / disable.

    The id (and the node-side directory) never moves. ``enabled`` is enforced
    live by the git proxy rather than by invalidating anything, so disabling
    revokes this environment's git access on its next request and re-enabling
    restores the same token.
    """
    return await _guard(
        _update_environment(user.id, env_id, body)
    )


async def _update_environment(user_id, env_id: str, body: UpdateEnvReq) -> dict:
    """Translate the patch body into a service call.

    ``session_sync_interval`` is tri-state in the model (null/0/>0) and the
    service must also tell "client didn't touch the field" apart from "client
    explicitly reset it to null". ``model_fields_set`` distinguishes them:
    absent ⇒ pass the service's sentinel (don't touch); present (even as null)
    ⇒ pass the value through, where null means "follow global default".
    """
    kwargs: dict = {
        "name": body.name,
        "description": body.description,
        "enabled": body.enabled,
        "init_commands": body.init_commands,
    }
    if "session_sync_interval" in body.model_fields_set:
        kwargs["session_sync_interval"] = body.session_sync_interval
    return await environment_service.update_environment(str(user_id), env_id, **kwargs)


@router.delete("/environments/{env_id}")
async def delete_environment(env_id: str, user: User = Depends(get_current_user)) -> dict:
    """Delete the environment.

    Its node-side directory is removed too — unless the environment points at an
    ``external_path`` (a checkout the node did not create), in which case only
    the binding is dropped and the directory is left untouched.
    """
    return await _guard(environment_service.delete_environment(str(user.id), env_id))


@router.get("/environments/{env_id}/resources")
async def list_environment_resources(env_id: str, user: User = Depends(get_current_user)) -> dict:
    """列环境已配置的资源集合（skill/MCP/plugin）。"""
    return {
        "resources": await _guard(
            environment_service.list_environment_resources(str(user.id), env_id)
        )
    }


@router.post("/environments/{env_id}/resources")
async def add_environment_resource(
    env_id: str,
    body: AddEnvResourceReq,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """Add a granted skill/MCP/plugin to the environment — installed on the node
    immediately (not "待同步 until the next sync"). The node is the source of
    truth; the page refreshes right after to show reality.
    """
    base = str(request.base_url).rstrip("/")
    return await _guard(
        environment_service.add_environment_resource(
            str(user.id), env_id, kind=body.kind, resource_id=body.resource_id,
            team_id=team_id, request_base_url=base,
        )
    )


@router.delete("/environments/{env_id}/resources/{entry_id}")
async def remove_environment_resource(
    env_id: str,
    entry_id: str,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """Remove a resource from the environment — uninstalled on the node
    immediately (prune for isolated, file-delete for system tier)."""
    return await _guard(
        environment_service.remove_environment_resource(
            str(user.id), env_id, entry_id, team_id=team_id
        )
    )


@router.post("/environments/{env_id}/sync")
async def sync_environment(
    env_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """Install the configured set onto the node — the real install/uninstall.

    This is the page's one 「同步数据」 button: it also reconciles the session
    ledger AND imports a tail copy of each session's conversation
    (``import_content=True``), so the session tab has something to show right
    after a sync instead of an empty list until someone opens a detail page.

    ``request_base_url`` is needed because a mirrored skill resolves to a
    server-hosted archive URL the node fetches back from us.
    """
    base = str(request.base_url).rstrip("/")
    return await _guard(
        environment_service.sync_environment(
            str(user.id), env_id, team_id=team_id, request_base_url=base,
            import_content=True,
        )
    )


@router.post("/environments/{env_id}/provision")
async def provision_environment(
    env_id: str, user: User = Depends(get_current_user)
) -> dict:
    """重试环境准备（clone）——环境页「重试」按钮的落点。

    clone 归环境这一侧（异步、页面可见、失败可重试）；任务派发不再兜底 clone，
    所以失败的环境必须在环境页补。已在准备中时幂等返回当前状态。
    """
    return await _guard(environment_service.provision_environment(str(user.id), env_id))


@router.post("/environments/{env_id}/inventory")
async def refresh_environment_inventory(
    env_id: str, user: User = Depends(get_current_user)
) -> dict:
    """Re-read what the node has on disk for this environment."""
    return {
        "installed": await _guard(
            environment_service.refresh_environment_inventory(str(user.id), env_id)
        )
    }


@router.get("/environments/{env_id}/sessions")
async def list_environment_sessions(
    env_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    provider: str = Query(""),
    status: str = Query(""),
    q: str = Query(""),
    user: User = Depends(get_current_user),
) -> dict:
    """This environment's sessions, read from OUR ledger — not from the node.

    The page wants history, not a live query, so the list is served from what a
    sync already recorded. Run the sync route to refresh it from the node.

    Paginated + filterable: ``provider`` / ``status`` / ``q`` (title, platform
    name or session id substring). ``total`` counts the FILTERED set, so the
    footer agrees with what the list is showing.
    """
    return await _guard(
        environment_service.list_environment_sessions(
            str(user.id), env_id, limit=limit, offset=offset,
            provider=provider, status=status, query=q,
        )
    )


@router.post("/environments/{env_id}/sessions/{session_id}/sync")
async def sync_environment_session(
    env_id: str, session_id: str, user: User = Depends(get_current_user)
) -> dict:
    """Re-read ONE session from the node (the detail page's 同步 button).

    Reconciles just this row's title/status/last-activity — the rest of the list
    is untouched. A session the node no longer reports is marked ended.
    """
    return await _guard(
        environment_service.sync_environment_session(str(user.id), env_id, session_id)
    )


@router.post("/environments/{env_id}/sessions/sync")
async def sync_environment_sessions(
    env_id: str, user: User = Depends(get_current_user)
) -> dict:
    """Reconcile the session ledger against the node.

    The node is authoritative: it reports the sessions its editor actually has
    (read from the editor's own records — we never write session state), and the
    ledger follows — new ones are added, ones the node no longer has are marked
    ended rather than deleted.
    """
    return await _guard(
        environment_service.sync_environment_sessions(str(user.id), env_id)
    )


class RenameSessionReq(BaseModel):
    # 平台会话名称：空串 = 清除改名，回退显示节点同步来的标题。
    platform_name: str = ""


class AdoptSessionReq(BaseModel):
    """同步为任务：会话模式/思考等级从节点会话同步来（不进请求体），父 Key/模型/限制由用户选。

    没给父 Key 仍可同步（``_allow_keyless_dispatch``）：节点上的编辑器用它自己的登录态跑。
    """
    parent_api_key_id: int | None = None
    model_id: str | None = None
    # 模型映射统一 {main, slots}：main=主模型（claude=档名如 opus，其余=平台模型），
    # slots=用途槽 {model, fallback_models[]}；读取时经 _migrate_model_mapping 兼容旧形状。
    model_mapping: dict | None = None
    # 模型映射列表（可选模型白名单，跑空=继承父 Key 全量）。
    models: list[str] | None = None
    # API Key 限制（不传=继承父 Key）。
    usage_limit: dict | None = None
    rate_limit: dict | None = None
    expires_at: float | None = None
    editor_provider_whitelist: list[str] | None = None
    selection_strategy: str | None = None


class AdoptSessionsReq(BaseModel):
    """批量采用：勾中多少条就一次提交多少条。字段语义同 AdoptSessionReq。"""
    session_ids: list[str]
    parent_api_key_id: int | None = None
    model_id: str | None = None
    model_mapping: dict | None = None
    models: list[str] | None = None
    usage_limit: dict | None = None
    rate_limit: dict | None = None
    expires_at: float | None = None
    editor_provider_whitelist: list[str] | None = None
    selection_strategy: str | None = None


@router.patch("/environments/{env_id}/sessions/{session_id}")
async def rename_environment_session(
    env_id: str,
    session_id: str,
    body: RenameSessionReq,
    user: User = Depends(get_current_user),
) -> dict:
    """Set the platform-side display name for one synced session.

    This is the ONLY writer of ``platform_name`` — the node reconcile never
    touches it, so a user rename survives every later sync. Empty clears the
    override and the row falls back to the node-synced title.
    """
    return await _guard(
        environment_service.rename_environment_session(
            str(user.id), env_id, session_id, body.platform_name
        )
    )


@router.post("/environments/{env_id}/sessions/{session_id}/adopt")
async def adopt_environment_session(
    env_id: str,
    session_id: str,
    body: AdoptSessionReq,
    user: User = Depends(get_current_user),
) -> dict:
    """Second-stage sync: turn one synced session into a platform task.

    Stage 1 (``/sync``) brings in the cheap metadata so the list has something to
    show; stage 2 gives ONE session a platform identity — a Task with its own
    key/model/event trail, which is what makes it show up in the project's task
    list. **No node action at all**: the task row + params are persisted and the
    runtime is dispatched lazily on the first message. Idempotent: re-adopting
    returns the same task.

    Session mode (permission) and reasoning effort come from the node session
    (the ledger's ``mode``/``effort``, read from the session record at scan
    time) — never from the request. Parent key / model list / key limits are the
    user's choices and come from the body.
    """
    return await _guard(
        environment_service.adopt_environment_session(
            str(user.id),
            env_id,
            session_id,
            parent_api_key_id=body.parent_api_key_id,
            model_id=body.model_id,
            model_mapping=body.model_mapping,
            models=body.models,
            usage_limit=body.usage_limit,
            rate_limit=body.rate_limit,
            expires_at=body.expires_at,
            editor_provider_whitelist=body.editor_provider_whitelist,
            selection_strategy=body.selection_strategy,
        )
    )


@router.post("/environments/{env_id}/sessions/adopt-batch")
async def adopt_environment_sessions(
    env_id: str,
    body: AdoptSessionsReq,
    user: User = Depends(get_current_user),
) -> dict:
    """Batch second-stage sync: adopt the selected sessions into tasks at once.

    Each one is adopted independently — one bad session (e.g. environment without
    a project) must not waste the user's selection of the other nine. The response
    groups them as ``adopted`` / ``failed`` so the page can say what happened.
    """
    return await _guard(
        environment_service.adopt_environment_sessions(
            str(user.id),
            env_id,
            body.session_ids,
            parent_api_key_id=body.parent_api_key_id,
            model_id=body.model_id,
            model_mapping=body.model_mapping,
            models=body.models,
            usage_limit=body.usage_limit,
            rate_limit=body.rate_limit,
            expires_at=body.expires_at,
            editor_provider_whitelist=body.editor_provider_whitelist,
            selection_strategy=body.selection_strategy,
        )
    )


@router.get("/environments/{env_id}/sessions/{session_id}/transcript")
async def get_session_transcript(
    env_id: str,
    session_id: str,
    limit: int = Query(60, ge=1, le=500),
    before_idx: int | None = Query(None, ge=0),
    user: User = Depends(get_current_user),
) -> dict:
    """Session detail: the session's own info + a window of its IMPORTED history.

    **Reads our ledger only — never the node.** The node is the content's
    authority, but the detail page must open even when the node control plane is
    unreachable; what it shows is the copy a previous sync/import stored.
    ``before_idx`` pages backward through that copy. To pull NEW content the
    caller explicitly hits ``/import`` — that is the only node-touching route.
    """
    return await _guard(
        environment_service.get_session_transcript(
            str(user.id), env_id, session_id, limit=limit, before_idx=before_idx
        )
    )


class ImportTranscriptReq(BaseModel):
    # "tail" = poll for new content at the end (the detail page's live loop);
    # "history" = widen the window to reach older turns (the 查看历史 button).
    mode: str = "tail"
    # 0 = 让服务端按 mode 定窗口（tail 用默认段、history 用"已存窗口 + 一段"）。
    window_lines: int = 0


@router.post("/environments/{env_id}/sessions/{session_id}/import")
async def import_env_session_transcript(
    env_id: str,
    session_id: str,
    body: ImportTranscriptReq,
    user: User = Depends(get_current_user),
) -> dict:
    """Read the node's session record → **store it** → return what is new.

    This is the only route that touches the node for conversation content, and
    it always writes the ledger BEFORE answering, so the reader (the DB-only
    transcript route) sees the same items the caller just got. ``before`` /
    ``after`` split the new items into "older than what we had" (history load,
    prepend) and "newer" (tail poll, append).
    """
    return await _guard(
        environment_service.import_env_session_items(
            str(user.id), env_id, session_id,
            mode=body.mode, window_lines=body.window_lines,
        )
    )


# ── system environment (the node operator's real HOME) ────────────────────────
# Node-addressed, not environment-addressed: the system tier has no environment
# row, so these hang off /nodes/{node_id} instead of /environments/{env_id}.
# Semantics differ from the shared tier on purpose — see system_env_service's
# module docstring (read-first, incremental writes, ownership-scoped removal).


class InstallSystemEnvReq(BaseModel):
    kind: str
    resource_id: str
    # Whether to replace an entry that already exists on disk. Defaults to false
    # because the existing copy may be the operator's own work; the UI asks.
    overwrite: bool = False


class ArchiveSystemEnvReq(BaseModel):
    kind: str
    name: str


async def _guard_system_env(coro):
    """Same mapping as :func:`_guard`, for :class:`SystemEnvError` codes."""
    from .node_client import NodeServerUnavailable
    from .node_client.errors import RPCError
    from .system_env_service import SystemEnvError

    try:
        return await coro
    except SystemEnvError as exc:
        raise HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 400), detail=exc.message)
    except NodeServerUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"节点控制面不可用：{exc}")
    except RPCError as exc:
        raise HTTPException(status_code=502, detail=f"节点调用失败：{exc}")


@router.get("/nodes/{node_id}/system-env")
async def get_system_env(node_id: str, user: User = Depends(get_current_user)) -> dict:
    """The stored inventory of the node operator's HOME, grouped by kind.

    Reads the snapshot, not the node, so the panel opens instantly and still shows
    the last known state while the node is offline.
    """
    return await _guard_system_env(system_env_service.system_env_detail(str(user.id), node_id))


@router.post("/nodes/{node_id}/system-env/refresh")
async def refresh_system_env(
    node_id: str, provider: str | None = None, user: User = Depends(get_current_user)
) -> dict:
    """Re-scan the operator's HOME on the node and replace the stored snapshot."""
    return {
        "installed": await _guard_system_env(
            system_env_service.refresh_system_env(
                str(user.id), node_id, provider=provider or ""
            )
        )
    }


@router.post("/nodes/{node_id}/system-env/install")
async def install_system_env_resource(
    node_id: str,
    body: InstallSystemEnvReq,
    request: Request,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """Install one granted platform resource into the operator's HOME.

    Incremental: nothing else on disk is touched, and an existing same-named entry
    is skipped unless ``overwrite`` — the response reports which happened.
    """
    base = str(request.base_url).rstrip("/")
    return {
        "touched": await _guard_system_env(
            system_env_service.install_to_system_env(
                str(user.id),
                node_id,
                kind=body.kind,
                resource_id=body.resource_id,
                overwrite=body.overwrite,
                team_id=team_id,
                request_base_url=base,
            )
        )
    }


@router.post("/nodes/{node_id}/system-env/archive")
async def archive_system_env_resource(
    node_id: str,
    body: ArchiveSystemEnvReq,
    user: User = Depends(get_current_user),
    team_id: str = Depends(get_current_team_id),
) -> dict:
    """Archive a locally installed resource into the platform library.

    The node packs the directory and uploads it; the server stores it as a mirror
    and creates a team reference, making the resource reusable on other nodes and
    in shared environments.
    """
    return await _guard_system_env(
        system_env_service.archive_system_env_resource(
            str(user.id), node_id, kind=body.kind, name=body.name, team_id=team_id
        )
    )


@router.delete("/nodes/{node_id}/system-env/{kind}/{name}")
async def remove_system_env_resource(
    node_id: str, kind: str, name: str, user: User = Depends(get_current_user)
) -> dict:
    """Node-level uninstall: delete the files and drop the platform-side record.

    Only platform-installed entries qualify; the node refuses anything absent from
    its own manifest, so an operator's hand-installed resource can be listed but
    never removed here.
    """
    return await _guard_system_env(
        system_env_service.remove_from_system_env(str(user.id), node_id, kind=kind, name=name)
    )
