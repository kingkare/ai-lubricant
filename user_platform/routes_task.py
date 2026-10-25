"""C-side task domain routes (``/api/v1/users/tasks``).

Mirrors the upstream task handler contract so the ported frontend behaves
identically. Storage + orchestration weaving lives in ``task_service``; the
VM/agent runtime is external (agent-compose) and disabled by default, so task
creation degrades to storage-only cleanly.

Scope of this first slice: task CRUD (list / info / create / update / stop /
delete / stats). The realtime ``stream``/``control`` (WebSocket) and
speech-to-text surfaces depend on the agent-compose runtime + external ASR and
are layered on later without blocking this core.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from .deps import audit_user_action, get_current_user, resolve_team_id
from .models import User
from .models_task import Task
from .node_client.errors import RPCError
from .resource_reference_service import (
    normalize_mcp_bindings,
    resolve_prompt_for_user,
)
from .task_service import task_service
from .task_service import _CLAUDE_RESERVED_MODEL_HINT

router = APIRouter(prefix="/api/v1/users/tasks", tags=["user-platform-tasks"])

_TASK_STATUSES = {"pending", "processing", "finished", "error"}


class TaskRepoReq(BaseModel):
    repo_url: str | None = None
    branch: str | None = None
    # 分支策略，对齐编辑器：default=跟随仓库默认分支；existing=checkout 已有分支
    # （branch 必填）；auto=首次初始化时从默认分支确定性创建 task/<task_id>。
    branch_mode: str | None = None


class TaskExtraConfig(BaseModel):
    project_id: str | None = None
    issue_id: str | None = None


class VMResource(BaseModel):
    core: int | None = 1
    memory: int | None = 1024
    life: int | None = None


class CreateTaskReq(BaseModel):
    # 可为空：有节点时建交互式等待任务，无节点时建存储态草稿。
    content: str = ""
    # 首条消息的端到端幂等键；旧客户端不传时服务端生成 UUID。
    client_message_id: str | None = Field(None, min_length=1, max_length=64)
    # The runtime is now a chosen agent-compose node (not a server-defined
    # image/host). ``node_id`` must be one the user's groups were granted; it is
    # occupied exclusively for the task's lifetime.
    node_id: str | None = None
    model_id: str | None = None
    git_identity_id: str | None = None
    repo: TaskRepoReq | None = None
    cli_name: str | None = None
    provider: str | None = None
    models: list[str] | None = None
    # 模型映射（per-provider 形状，见 task_service._normalize_model_mapping）。
    # 统一为 {main, slots}：main=主模型（claude 是档名别名如 opus，其余是平台模型）；
    # slots=编辑器原生用途槽，每槽 {model, fallback_models[]}（claude=四档，codex=目录,
    # opencode/dsh=模型表，gemini/cursor 无槽）。旧形状 roles/fallback/catalog/models
    # 读取时经 _migrate_model_mapping 归一。弹框不再传 models/model_id。
    model_mapping: dict | None = None
    environment: dict[str, str] | None = None
    # Execution-environment isolation tier. "isolated" (default/None) = throwaway
    # per-session home; "shared" = a named persistent home on the node (env_id
    # required) reused across tasks; "system" = the node operator's real home
    # (the node must have opted into system mode, else dispatch is refused).
    env_mode: str | None = None
    env_id: str | None = None
    env_name: str | None = None
    # 「创建 worktree」选项（默认是）。只对**新建任务**生效：开=从环境的代码目录
    # 切一个任务专属 worktree；关=不切——环境有现成目录（导入/fresh）就直接在环境
    # 目录里跑，没有（托管 clone 环境）就任务自己 clone 一份（历史行为）。
    # 同步为任务不受影响（它本来就不切）。
    create_worktree: bool | None = None
    # 共用/系统环境自带资源的激活子集（按环境清单里的名字）：仅激活列出的
    # 技能/插件，其余环境已装项本次任务不启用。空/缺失 = 激活环境全部
    # 已装项。skill 在节点侧真正生效；plugin 协议已下发但节点暂不消费。
    active_skills: list[str] | None = None
    active_plugins: list[str] | None = None
    mcp_config: list[dict] | None = None
    resource: VMResource | None = None
    extra: TaskExtraConfig | None = None
    system_prompt: str | None = None
    task_type: str | None = None
    sub_type: str | None = None
    # Provider/editor-defined mode id; empty means the editor's default.
    mode: str | None = None
    mode_label: str | None = None
    reasoning_effort: str | None = None
    task_role: str | None = None
    # Canonical Task key lineage. A runnable Task must choose a parent API Key;
    # a ``scope='task'`` child is minted from it and bound to this Task. Codex
    # Tasks additionally require ``expected_client_id`` + ``bootstrap_content``
    # so the first provider request can be matched against this Task only.
    parent_api_key_id: int | None = None
    usage_limit: dict | None = None
    expires_at: float | None = None
    # 子 Key 收窄参数，复用既有 api_keys 列（不加新 schema）。省略(None)=继承父级；
    # models 既写 models_snapshot(运行态菜单)又收窄子 Key 的 model_whitelist
    # （create_task 内部同源处理，不在请求体里重复声明）。editor_provider_whitelist
    # 由前端「可用编辑器」单选映射而来。rate_limit 不做父子约束（按单 Key 计数）。
    rate_limit: dict | None = None
    editor_provider_whitelist: list[str] | None = None
    selection_strategy: str | None = None
    expected_client_id: str | None = None
    bootstrap_content: str | None = None
    prompt_id: str | None = None


class UpdateTaskReq(BaseModel):
    title: str | None = None
    summary: str | None = None
    # Provider-defined permission/approval mode；下一条消息生效，不要求活跃运行时。
    mode: str | None = None
    mode_label: str | None = None
    reasoning_effort: str | None = None
    # 模型映射（详情页齿轮弹框整体替换）。形状与 CreateTaskReq.model_mapping 同源，
    # 校验收口在 task_service.update_task（_normalize_model_mapping）。
    model_mapping: dict | None = None
    # Resource config hot-update. ``resource_id``-bearing entries are resolved
    # to server-trusted wire specs before persistence; full wire specs from
    # authorized callers pass through. Secrets are never rendered back.
    # skill_config/plugin_config 已退役：资源归环境，任务不携带。
    mcp_config: list[dict] | None = None


class TaskAttachmentReq(BaseModel):
    url: str
    filename: str | None = None


class TaskMessageReq(BaseModel):
    content: str
    attachments: list[TaskAttachmentReq] | None = None
    # 端到端幂等键：同 ID 重复请求不会把同一句话投递/执行两遍；不传时服务端生成。
    client_message_id: str | None = Field(None, min_length=1, max_length=64)


class TaskBatchReq(BaseModel):
    # 批量停止/删除。去重 + 非空 + 上限在路由层校验，服务层只按 id 逐条处理，
    # 单条失败不中断其余——批量场景下一条不可停止/已删的 id 不应让整批回滚。
    task_ids: list[str]

    @model_validator(mode="after")
    def _normalize(self) -> "TaskBatchReq":
        seen: list[str] = []
        for tid in self.task_ids:
            tid = (tid or "").strip()
            if tid and tid not in seen:
                seen.append(tid)
        self.task_ids = seen
        return self


class TaskModelSwitchReq(BaseModel):
    model_id: str


class TaskRestartReq(BaseModel):
    # Historical Task contract: true resumes the provider conversation; false
    # starts with a clean runtime context while retaining the Task workspace and
    # its persisted resource/provider configuration.
    load_session: bool = True


async def _resolve_task_resource_configs(
    user: User, req: dict, request: Request
) -> dict:
    """Resolve the task's MCP bindings into server-trusted service bindings.

    只剩 MCP 一段：``mcp_config`` 里的 ``{resource_id}`` 绑定校验授权后转成
    ``{service_id}`` 形态，供 task_service 写 principal grants（派发时
    ``_principal_mcp_specs`` 现造安全 spec，密钥不下发节点）。越权 id 在写行
    之前就被拒（→ 403）。

    skill/plugin 那段已删：弹框不再配技能/插件（它们归环境管理），
    ``extra.skill_ids`` / ``plugin_ids`` 永远为空——原来每次都白跑一次
    ``resolve_team_id`` + 空循环，是创建任务的固定浪费。
    """
    mcp_explicit = req.get("mcp_config")
    if not isinstance(mcp_explicit, list) or not mcp_explicit:
        return {}
    team_id = await resolve_team_id(str(user.id))
    try:
        return {"mcp_config": await normalize_mcp_bindings(
            str(user.id), team_id, mcp_explicit,
        )}
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=f"资源未授权或配置无效: {exc}") from exc


def _audit_resource_counts(req: dict) -> dict:
    """Audit bodies never carry full wire specs — only per-kind counts."""
    out = dict(req)
    for key in ("mcp_config",):
        if key in out:
            out[key] = len(out[key] or []) if isinstance(out.get(key), list) else 0
    return out
@router.get("/legacy-session/{session_id}")
async def resolve_legacy_task_session(
    session_id: str, user: User = Depends(get_current_user)
) -> dict:
    """Resolve a deployed editor-session bookmark to its canonical Task."""
    query = Task.filter(source_editor_session_id=session_id, deleted_at=None)
    if user.role != "admin":
        query = query.filter(user_id=user.id)
    task = await query.order_by("-created_at").first()
    if task is None:
        raise HTTPException(status_code=404, detail="旧会话尚未迁移为任务")
    return {"task_id": str(task.id)}


@router.get("")
async def list_tasks(
    user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=200),
    # The frontend project tab sends ``size``; keep it as an alias for
    # ``page_size`` so both spellings work (``size`` wins when provided).
    size: int | None = Query(None, ge=1, le=200),
    project_id: str | None = Query(None),
    status: str | None = Query(None),
) -> dict:
    """分页查询当前用户的任务列表。

    支持 ``project_id``（须为合法 UUID）与 ``status``（逗号分隔，取值
    pending/processing/finished/error）过滤；``size`` 是 ``page_size`` 的别名，
    同时传时以 ``size`` 为准。返回 total/page/page_size/rows。
    """
    if project_id is not None:
        project_id = project_id.strip()
        if not project_id:
            project_id = None
        else:
            try:
                uuid.UUID(project_id)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="project_id 格式不正确")
    statuses: list[str] | None = None
    if status is not None:
        statuses = [item.strip().lower() for item in status.split(",") if item.strip()]
        invalid = sorted(set(statuses) - _TASK_STATUSES)
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的任务状态: {', '.join(invalid)}",
            )
        if not statuses:
            statuses = None
    result = await task_service.list_tasks(
        str(user.id),
        role=user.role,
        page=page,
        page_size=size or page_size,
        project_id=project_id,
        statuses=statuses,
    )
    return {
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "rows": result.rows,
    }


@router.get("/create-defaults")
async def get_create_defaults(
    project_id: str | None = Query(None),
    provider: str | None = Query(None),
    user: User = Depends(get_current_user),
) -> dict:
    """读「创建任务」的配置缓存（按 用户+项目+编辑器）。没存过则空 dict。"""
    payload = await task_service.get_create_defaults(str(user.id), project_id, provider or "")
    return {"defaults": payload}


@router.put("/create-defaults")
async def put_create_defaults(
    body: dict,
    project_id: str | None = Query(None),
    provider: str | None = Query(None),
    user: User = Depends(get_current_user),
) -> dict:
    """写「创建任务」的配置缓存（upsert）。body 就是表单态整包。"""
    payload = await task_service.save_create_defaults(str(user.id), project_id, provider or "", body)
    return {"defaults": payload}


@router.get("/{task_id}")
async def get_task(task_id: str, user: User = Depends(get_current_user)) -> dict:
    """查询单个任务的详情（含运行态、模型映射、资源绑定）；不存在或无权限返回 404。"""
    task = await task_service.get_task(str(user.id), task_id, role=user.role)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.post("")
async def create_task(
    body: CreateTaskReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """创建任务并返回持久化的任务记录（派发为异步，节点准备进度以「准备中」暴露）。

    写入前会规范化 reasoning_effort 与分支策略、把资源引用解析为服务端可信的
    绑定（未授权的资源 id 直接 403）、并把可选项目提示词前缀拼进 content。
    错误经 ValueError 归一映射为 400/403/409。响应可能含一次性明文 api_key，
    审计日志会脱敏。
    """
    payload = body.model_dump(exclude_none=True)
    if "reasoning_effort" in payload:
        effort = str(payload["reasoning_effort"] or "").strip().lower()
        if effort == "max":
            effort = "xhigh"
        if effort not in {"", "low", "medium", "high", "xhigh"}:
            raise HTTPException(status_code=400, detail="reasoning_effort 必须为 low/medium/high/xhigh 或空")
        payload["reasoning_effort"] = effort
    # 规范化分支策略：existing 必须带 branch；非 existing 忽略 branch。
    repo = payload.get("repo")
    if isinstance(repo, dict):
        mode = (repo.get("branch_mode") or "default").strip()
        branch = str(repo.get("branch") or "").strip()
        if mode not in ("default", "existing", "auto"):
            raise HTTPException(status_code=400, detail="不支持的分支策略")
        if mode == "existing" and not branch:
            raise HTTPException(status_code=400, detail="选择已有分支时必须填写分支名")
        repo["branch_mode"] = mode
        repo["branch"] = branch if mode == "existing" else ""
        payload["repo"] = repo
    # Resolve resource references to trusted wire specs before persistence, so
    # unauthorized ids never reach the row or the node dispatch payload.
    payload.update(await _resolve_task_resource_configs(user, payload, request))
    # Resolve an optional project prompt. The dispatch session has no prompt
    # slot, so — matching the webhook review path — the granted prompt's content
    # is prepended to the task body. ``prompt_id`` itself is not persisted.
    prompt_id = (payload.pop("prompt_id", None) or "").strip()
    if prompt_id:
        team_id = await resolve_team_id(str(user.id))
        provider = (payload.get("provider") or payload.get("cli_name") or "").strip().lower()
        prompt = await resolve_prompt_for_user(str(user.id), team_id, prompt_id, provider)
        if not prompt:
            raise HTTPException(status_code=403, detail="提示词未授权或不适用该执行客户端")
        prefix = str(prompt.get("content") or "").strip()
        if prefix:
            payload["content"] = f"{prefix}\n\n{payload.get('content', '')}".strip()
    try:
        # The node only acks after clone + resource sync + runtime start. Public
        # creation must not block on that slow phase: return the persisted task
        # immediately and expose its background progress as「准备中」.
        result = await task_service.create_task(str(user.id), payload, defer_dispatch=True)
    except ValueError as exc:
        reason = str(exc)
        detail_map = {
            "content_required": "缺少任务内容",
            "node_required": "请选择一个执行节点",
            "node_forbidden": "无权使用该节点（不在你所属分组的授权范围内）",
            "node_occupied": "该节点已被占用，请选择其它空闲节点",
            "node_system_env_disabled": "该节点未开启系统内置环境，请换一个节点或改用隔离/共用环境",
            "parent_api_key_required": "运行任务需要选择一个父 API Key",
            "parent_api_key_invalid": "父 API Key 不可用或已停用",
            "model_not_available_for_key": "所选模型不在父 API Key 的可用范围内",
            "model_mapping_fallback_required": "模型映射需要先选择一个模型",
            "不支持的选择策略": "不支持的 API Key 选择策略",
            "codex_bootstrap_required": "Codex 任务需预注册客户端实例与首条内容",
            # 私有仓库的 clone 只能经服务端 git proxy 鉴权。这两条是部署配置缺失，
            # 直说缺什么，而不是让用户面对一个要求手输账号密码的 git。
            "git_proxy_origin_missing": "私有仓库需要服务端 git 代理，但未配置控制面公网地址（AGENT_COMPOSE_NODE_SERVER_PUBLIC_URL），请联系管理员",
            "git_proxy_token_missing": "私有仓库需要服务端 git 代理，但未配置控制面共享令牌（NODE_CONTROL_TOKEN），请联系管理员",
        }
        # 子 Key 白名单越权消息是动态的（带越权项列表），消息本身即面向用户，
        # 直接透出。
        if reason.startswith((
            "子 Key 的模型白名单不能超出父 Key 范围",
            "子 Key 的编辑器客户端白名单不能超出父 Key 范围",
        )):
            detail = reason
        else:
            detail = detail_map.get(reason, "创建任务失败")
        status_code = (
            409 if reason == "node_occupied"
            else 403 if reason in ("node_forbidden", "parent_api_key_required")
            else 400
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    # The create response may include a one-time plaintext ``api_key``; the
    # audit log must not retain it. Resource wire specs are also redacted —
    # only per-kind counts are audited.
    masked_response = {k: v for k, v in result.items() if k != "api_key"}
    if "api_key" in result:
        masked_response["api_key"] = {**result["api_key"], "key": "<redacted>"}
    await audit_user_action(
        request, user, "task.create",
        request_body=_audit_resource_counts(payload), response=masked_response,
    )
    return result


# NOTE: ``PUT /stop`` MUST be declared before ``PUT /{task_id}``; otherwise the
# path param route captures "stop" as a task id.
@router.put("/stop")
async def stop_task(
    request: Request, task_id: str = Query(...), user: User = Depends(get_current_user)
) -> dict:
    """停止指定任务（通过 query 参数 ``task_id`` 传入）。任务不存在返回 404。

    注意：本路由必须声明在 ``PUT /{task_id}`` 之前，否则会被路径参数吞掉。
    """
    ok = await task_service.stop_task(str(user.id), task_id, role=user.role)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    await audit_user_action(
        request, user, "task.stop", request_body={"task_id": task_id}, response={"ok": True},
    )
    return {"ok": True}


@router.post("/batch-stop")
async def batch_stop_tasks(
    body: TaskBatchReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """批量停止任务（一次最多 100 个，去重后非空）。

    逐条处理，单条失败不影响其余：返回 ``{stopped, failed:[{id,error}]}``。
    """
    if not body.task_ids:
        raise HTTPException(status_code=400, detail="至少选择一个任务")
    if len(body.task_ids) > 100:
        raise HTTPException(status_code=400, detail="一次最多操作 100 个任务")

    stopped = 0
    failed: list[dict[str, str]] = []
    for task_id in body.task_ids:
        try:
            ok = await task_service.stop_task(str(user.id), task_id, role=user.role)
            if not ok:
                failed.append({"id": task_id, "error": "任务不存在"})
                continue
            stopped += 1
            await audit_user_action(
                request, user, "task.stop",
                request_body={"task_id": task_id, "batch": True},
                response={"ok": True},
            )
        except Exception as exc:  # noqa: BLE001 — 单条失败不影响批量其余项
            failed.append({"id": task_id, "error": str(exc) or "停止任务失败"})
    return {"stopped": stopped, "failed": failed}


@router.post("/batch-delete")
async def batch_delete_tasks(
    body: TaskBatchReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """批量删除任务（软删除，一次最多 100 个，去重后非空）。

    逐条处理，单条失败不影响其余：返回 ``{deleted, failed:[{id,error}]}``。
    """
    if not body.task_ids:
        raise HTTPException(status_code=400, detail="至少选择一个任务")
    if len(body.task_ids) > 100:
        raise HTTPException(status_code=400, detail="一次最多操作 100 个任务")

    deleted = 0
    failed: list[dict[str, str]] = []
    for task_id in body.task_ids:
        try:
            ok = await task_service.delete_task(str(user.id), task_id, role=user.role)
            if not ok:
                failed.append({"id": task_id, "error": "任务不存在"})
                continue
            deleted += 1
            await audit_user_action(
                request, user, "task.delete",
                request_body={"task_id": task_id, "batch": True},
                response={"deleted": True},
            )
        except Exception as exc:  # noqa: BLE001 — 单条失败不影响批量其余项
            failed.append({"id": task_id, "error": str(exc) or "删除任务失败"})
    return {"deleted": deleted, "failed": failed}


@router.put("/{task_id}")
async def update_task(
    task_id: str, body: UpdateTaskReq, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """更新任务的标题/摘要/模型映射/资源绑定等字段（仅覆盖传入的字段）。

    reasoning_effort 与资源引用解析同创建流程；模型映射校验失败返回 400，
    任务不存在返回 404。
    """
    fields = body.model_dump(exclude_none=True)
    if "reasoning_effort" in fields:
        effort = str(fields["reasoning_effort"] or "").strip().lower()
        if effort == "max":
            effort = "xhigh"
        if effort not in {"", "low", "medium", "high", "xhigh"}:
            raise HTTPException(status_code=400, detail="reasoning_effort 必须为 low/medium/high/xhigh 或空")
        fields["reasoning_effort"] = effort
    # Hot-update resource configs resolve the same way as create: resource_id
    # refs become trusted specs; unauthorized ids are rejected before persist.
    fields.update(await _resolve_task_resource_configs(user, fields, request))
    try:
        result = await task_service.update_task(
            str(user.id), task_id, fields, role=user.role
        )
    except ValueError as exc:
        # 模型映射校验（形状 / 父 Key 可执行集合 / 兜底必填）与创建同源，错误
        # 文案也沿用创建那套，别让详情页只看到一句笼统的「更新失败」。
        reason = str(exc)
        detail = {
            "model_not_available_for_key": "所选模型不在父 API Key 的可用范围内",
            "model_mapping_fallback_required": "模型映射需要先选择一个模型",
        }.get(reason, "更新任务失败")
        raise HTTPException(status_code=400, detail=detail) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    await audit_user_action(
        request, user, "task.update",
        request_body={"task_id": task_id, **_audit_resource_counts(fields)},
        response=result,
    )
    return result


@router.delete("/{task_id}")
async def delete_task(
    task_id: str, request: Request, user: User = Depends(get_current_user)
) -> dict:
    """删除指定任务（软删除，不可恢复；仅限本人名下任务）。任务不存在返回 404。"""
    ok = await task_service.delete_task(str(user.id), task_id, role=user.role)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    await audit_user_action(
        request, user, "task.delete", request_body={"task_id": task_id}, response={"deleted": True},
    )
    return {"deleted": True}


@router.post("/{task_id}/messages")
async def send_task_message(
    task_id: str,
    body: TaskMessageReq,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """向任务发送一条用户消息，必要时启动运行时并返回派发结果。

    支持附件与 ``client_message_id``（端到端幂等，重复请求不会重复执行）。
    ValueError 归一映射为 404/403/422/409/503；控制面 RPC 异常统一 503，
    客户端可重发。审计记录消息长度与附件数。
    """
    try:
        result = await task_service.send_task_message(
            str(user.id),
            task_id,
            body.content,
            attachments=[item.model_dump() for item in (body.attachments or [])],
            role=user.role,
            client_message_id=body.client_message_id,
        )
    except ValueError as exc:
        reason = str(exc)
        # claude_reserved_model_alias:<name> — split out the model name for the hint.
        if reason.startswith("claude_reserved_model_alias:"):
            name = reason.split(":", 1)[1]
            raise HTTPException(
                status_code=400,
                detail=_CLAUDE_RESERVED_MODEL_HINT.format(name=name),
            ) from exc
        status = (
            404 if reason == "task_not_found"
            else 403 if reason == "node_forbidden"
            else 503 if reason in ("node_server_unavailable", "task_api_key_unavailable")
            else 409 if reason in ("runtime_not_bound", "runtime_dispatch_failed")
            else 422 if reason == "content_required"
            else 409
        )
        detail = {
            "task_not_found": "任务不存在",
            "runtime_not_bound": "任务当前没有可用运行时",
            "node_required": "任务未绑定执行节点",
            "node_forbidden": "无权使用任务绑定的执行节点",
            "node_system_env_disabled": "该节点未开启系统内置环境，请换一个节点或改用隔离/共用环境",
            "node_server_unavailable": "节点控制面暂不可用，请确认节点服务已启动后重试",
            "task_api_key_unavailable": "任务 API Key 不可用，无法启动运行时",
            "task_not_startable": "当前任务状态不能启动运行时",
            "runtime_dispatch_conflict": "任务运行时启动冲突，请刷新后重试",
            "runtime_dispatch_failed": "任务运行时派发失败，请查看任务详情中的派发错误",
            "content_required": "消息不能为空",
            # 私有仓库的 clone 只能经服务端 git proxy 鉴权；缺配置时直说缺什么，
            # 而不是让用户面对一个要求手输账号密码的 git。
            "git_proxy_origin_missing": "私有仓库需要服务端 git 代理，但未配置控制面公网地址（AGENT_COMPOSE_NODE_SERVER_PUBLIC_URL），请联系管理员",
            "git_proxy_token_missing": "私有仓库需要服务端 git 代理，但未配置控制面共享令牌（NODE_CONTROL_TOKEN），请联系管理员",
        }.get(reason, "消息发送失败")
        raise HTTPException(status_code=status, detail=detail) from exc
    except RPCError as exc:
        # Any control-plane RPC not normalized to a ValueError above (e.g. a
        # second stale placement, or an unexpected code) must not surface as a
        # raw 500. Map to a retryable 503 so the client can re-send.
        raise HTTPException(
            status_code=503,
            detail=f"节点运行时暂不可用：{exc.message}",
        ) from exc
    await audit_user_action(
        request, user, "task.message",
        request_body={
            "task_id": task_id,
            "content_length": len(body.content or ""),
            "attachment_count": len(body.attachments or []),
            "client_message_id": body.client_message_id,
        },
        response=result,
    )
    return result


@router.post("/{task_id}/cancel")
async def cancel_task_turn(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """取消任务当前正在进行的这一轮对话（不停止任务本身）。任务不存在返回 404。"""
    try:
        result = await task_service.cancel_task_turn(str(user.id), task_id, role=user.role)
    except ValueError as exc:
        reason = str(exc)
        if reason == "task_not_found":
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if reason == "node_server_unavailable":
            raise HTTPException(
                status_code=503, detail="节点控制面暂不可用，请确认节点服务已启动后重试"
            ) from exc
        raise HTTPException(status_code=409, detail="任务当前没有可用运行时") from exc
    await audit_user_action(request, user, "task.cancel", request_body={"task_id": task_id}, response=result)
    return result


@router.post("/{task_id}/model")
async def switch_task_model(
    task_id: str,
    body: TaskModelSwitchReq,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """切换任务的主模型（claude 档别名会被拦截并给出提示），下一条消息生效。

    目标模型必须在所属 API Key 的可用范围内，否则返回 400；任务不存在返回 404。
    """
    try:
        result = await task_service.switch_task_model(
            str(user.id), task_id, body.model_id, role=user.role
        )
    except ValueError as exc:
        reason = str(exc)
        if reason.startswith("claude_reserved_model_alias:"):
            name = reason.split(":", 1)[1]
            raise HTTPException(
                status_code=400,
                detail=_CLAUDE_RESERVED_MODEL_HINT.format(name=name),
            ) from exc
        status = 404 if reason == "task_not_found" else 400
        detail = {
            "task_not_found": "任务不存在",
            "model_required": "请选择一个模型",
            "model_not_available_for_key": "目标模型当前不可用：不在所属 API Key 的可用范围内，或该模型暂无可用渠道供给",
            "parent_api_key_invalid": "任务 API Key 不可用，无法校验模型",
        }.get(reason, "切换模型失败")
        raise HTTPException(status_code=status, detail=detail) from exc
    await audit_user_action(
        request, user, "task.model_switch",
        request_body={"task_id": task_id, "model_id": body.model_id}, response=result,
    )
    return result


class TaskModelsAddReq(BaseModel):
    models: list[str]


@router.post("/{task_id}/models")
async def add_task_models(
    task_id: str,
    body: TaskModelsAddReq,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """向任务的模型映射追加可用模型（须在该任务 API Key 的可用范围内）。

    至少选择一个模型，模型超范围或被占用时返回 400；任务不存在返回 404。
    """
    try:
        result = await task_service.add_task_models(
            str(user.id), task_id, body.models, role=user.role
        )
    except ValueError as exc:
        reason = str(exc)
        if reason.startswith("claude_reserved_model_alias:"):
            name = reason.split(":", 1)[1]
            raise HTTPException(
                status_code=400,
                detail=_CLAUDE_RESERVED_MODEL_HINT.format(name=name),
            ) from exc
        status = 404 if reason == "task_not_found" else 400
        detail = {
            "task_not_found": "任务不存在",
            "model_required": "请至少选择一个模型",
            "model_not_available_for_key": "所选模型不在该 API Key 可用范围内",
            "parent_api_key_invalid": "任务 API Key 不可用，无法校验模型",
        }.get(reason, "添加模型失败")
        raise HTTPException(status_code=status, detail=detail) from exc
    await audit_user_action(
        request, user, "task.models.add",
        request_body={"task_id": task_id, "models": body.models}, response=result,
    )
    return result


@router.post("/{task_id}/start")
async def start_task_runtime(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """启动任务运行时（在已绑定节点上派发并启动会话）。

    未绑定节点、节点无权、节点控制面不可用、任务 Key 不可用、运行时冲突等
    分别映射为 400/403/503/409；任务不存在返回 404。
    """
    try:
        result = await task_service.start_task_runtime(
            str(user.id), task_id, role=user.role
        )
    except ValueError as exc:
        reason = str(exc)
        status = (
            404 if reason == "task_not_found"
            else 403 if reason == "node_forbidden"
            else 503 if reason in ("node_server_unavailable", "task_api_key_unavailable")
            else 409
        )
        detail = {
            "task_not_found": "任务不存在",
            "node_required": "任务未绑定执行节点",
            "node_forbidden": "无权使用任务绑定的执行节点",
            "node_system_env_disabled": "该节点未开启系统内置环境，请换一个节点或改用隔离/共用环境",
            "node_server_unavailable": "节点控制面暂不可用，请确认节点服务已启动后重试",
            "task_api_key_unavailable": "任务 API Key 不可用，无法启动运行时",
            "task_not_startable": "当前任务状态不能启动运行时",
            "runtime_dispatch_conflict": "任务运行时启动冲突，请刷新后重试",
            "runtime_dispatch_failed": "节点拒绝或未能启动任务运行时，请检查节点状态后重试",
        }.get(reason, "任务运行时启动失败")
        raise HTTPException(status_code=status, detail=detail) from exc
    await audit_user_action(
        request, user, "task.start", request_body={"task_id": task_id}, response=result,
    )
    return result


@router.post("/{task_id}/restart")
async def restart_task_runtime(
    task_id: str,
    request: Request,
    body: TaskRestartReq | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    """重启任务运行时。

    ``load_session=true``（默认）恢复 provider 会话继续对话；false 则丢弃运行
    时上下文、保留任务工作区与已持久化的资源配置重新开始。任务不存在返回
    404，无可用运行时返回 409。
    """
    load_session = body.load_session if body is not None else True
    try:
        result = await task_service.restart_task_runtime(
            str(user.id), task_id, load_session=load_session, role=user.role
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "task_not_found":
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if reason == "node_server_unavailable":
            raise HTTPException(
                status_code=503, detail="节点控制面暂不可用，请确认节点服务已启动后重试"
            ) from exc
        raise HTTPException(status_code=409, detail="任务当前没有可用运行时") from exc
    await audit_user_action(
        request, user, "task.restart",
        request_body={"task_id": task_id, "load_session": load_session}, response=result,
    )
    return result


@router.post("/{task_id}/api-key/disable")
async def disable_task_api_key(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """停用任务绑定的 API Key。任务不存在返回 404，任务没有可用 Key 返回 409。"""
    try:
        result = await task_service.disable_task_api_key(str(user.id), task_id, role=user.role)
    except ValueError as exc:
        reason = str(exc)
        status = 404 if reason == "task_not_found" else 409
        detail = "任务不存在" if status == 404 else "任务没有可用的 API Key"
        raise HTTPException(status_code=status, detail=detail) from exc
    await audit_user_action(
        request, user, "task.api-key.disable",
        request_body={"task_id": task_id}, response=result,
    )
    return result


@router.post("/{task_id}/api-key/rotate")
async def rotate_task_api_key(
    task_id: str,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """轮换任务的 API Key：签发新 Key 作废旧 Key，需要任务当前有可用运行时。

    任务不存在返回 404、无可用运行时返回 409。返回体含一次性明文 Key，
    审计日志已脱敏。
    """
    try:
        result = await task_service.rotate_task_api_key(str(user.id), task_id, role=user.role)
    except ValueError as exc:
        reason = str(exc)
        status = (
            404 if reason == "task_not_found"
            else 409 if reason == "runtime_not_bound"
            else 400
        )
        detail = (
            "任务不存在" if status == 404
            else "任务当前没有可用运行时，无法轮换 Key" if status == 409
            else "任务没有可用的 API Key"
        )
        raise HTTPException(status_code=status, detail=detail) from exc
    # The rotate response carries a one-time plaintext key; redact it from audit.
    masked = {k: v for k, v in result.items() if k != "key"}
    masked["key"] = "<redacted>"
    await audit_user_action(
        request, user, "task.api-key.rotate",
        request_body={"task_id": task_id}, response=masked,
    )
    return result


@router.get("/{task_id}/events/history")
async def list_task_events_history(
    task_id: str,
    before: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
) -> dict:
    """Replay persisted task events (conversation history, newest-first cursor).

    The live ``GET /{task_id}/events`` stream is fire-and-forget; this endpoint
    paginates the durable ``mc_task_events`` rows so a reconnecting client can
    show prior turns. ``before`` is an exclusive seq upper bound.
    """
    return await task_service.list_task_events(
        str(user.id), task_id, before=before, limit=limit, role=user.role
    )


@router.get("/{task_id}/events")
async def stream_task_events(task_id: str, user: User = Depends(get_current_user)):
    """订阅任务实时事件的 SSE 流（text/event-stream）。

    打开响应前先校验任务归属与运行时（任务不存在返回 404、无可用运行时返回
    409）。首帧补发持久化历史供回放，之后消费共享 follower 扇进的同一序列，
    慢消费者被摘（事件积压）时发 error 帧让页面重连拉历史。
    """
    import json as _json
    from fastapi.responses import StreamingResponse

    from agent import realtime
    from user_platform.task_service import acquire_task_follower, release_task_follower

    # Resolve ownership/runtime before opening the response so invalid tasks get
    # a normal HTTP status instead of a late error inside an SSE stream.
    # （与 task_events 的开头同两条检查，但这里不驱动生成器——驱动是共享
    # follower 的职责，本路由只消费它扇进 hub 的同一序列。）
    try:
        task = await task_service._owned_task_or_raise(str(user.id), task_id, user.role)
        await task_service._live_node_session_id(task)
    except ValueError as exc:
        reason = str(exc)
        status = 404 if reason == "task_not_found" else 409
        detail = "任务不存在" if status == 404 else "任务当前没有可用运行时"
        raise HTTPException(status_code=status, detail=detail) from exc

    # 与统一 WS 共享同一个驱动者（一个任务一条 node 流）：首帧持久化历史
    # （/events/history 最新页）供回放，之后增量走 hub——与 WS 订阅者同一条
    # 序列，不再有「谁发起谁看得到」。
    try:
        page = await task_service.list_task_events(
            str(user.id), task_id, limit=200, role=user.role
        )
        history = page.get("rows") or []
    except Exception:  # noqa: BLE001 — 历史读失败不阻断 live 流
        history = []

    await acquire_task_follower(task_id, str(user.id), user.role)
    queue = realtime.subscribe("task", task_id)

    async def event_generator():
        try:
            yield ": ok\n\n"
            for row in history:
                yield f"event: message\ndata: {_json.dumps(row, ensure_ascii=False)}\n\n"
            while True:
                frame = await queue.get()
                if frame.get("type") == "lagged":
                    # 慢消费者被摘：让页面自己重连拉历史，不喂半截流。
                    yield "event: error\ndata: {\"message\": \"事件积压，请刷新页面重试\"}\n\n"
                    return
                event = frame.get("event") or {}
                kind = event.get("kind")
                name = "agent_event" if kind == "structured" else kind or "message"
                yield f"event: {name}\ndata: {_json.dumps(event, ensure_ascii=False)}\n\n"
                if kind == "result":
                    return
        finally:
            realtime.unsubscribe("task", task_id, queue)
            release_task_follower(task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}/stats")
async def task_stats(task_id: str, user: User = Depends(get_current_user)) -> dict:
    """当前这条会话最新一条消息的 token 用量（不是任务累计）。

    口径与对话区进度圈一致：用户看这个数是想知道「这轮上下文用了多少」，
    累计值回答不了（跑得越久越大，跟当前这轮无关）。
    """
    return await task_service.task_stats(str(user.id), task_id, role=user.role)
