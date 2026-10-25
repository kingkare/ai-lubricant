from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Depends, Header, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from agent.config import AgentConfig, DEFAULT_MAX_TURNS
from agent.context_manager import rebuild_history_messages
from agent.turn_writer import ConversationTurnWriter
from agent.memory import MemorySystem
from agent.tools import ToolContext, ToolRegistry
from agent import conversation_hub, conversation_state, conversation_store, realtime, scene_context, run_state
# 审批超时中止本轮的信号异常（仅 stdlib 依赖，模块级导入无循环风险）。
from user_platform.node_client.approvals import ApprovalDenied, ApprovalTimeout

if TYPE_CHECKING:
    from agent.agent_main import GenericAgent


router = APIRouter(prefix="/agent", tags=["agent"])

_REASONING_EFFORTS = {"", "low", "medium", "high", "xhigh"}

# 审批等待时长的可配置区间。0 = 不过期（一直等人裁决）；下限 60s 是为了避免
# 配出「卡片还没看到就过期」的值；上限 7 天纯属兜底——审批挂起会钉住 asyncio
# task + SSE 连接 + 绑定的节点 PTY，且注册表是纯内存的，重启即丢。
_APPROVAL_TIMEOUT_MIN_SECONDS = 60
_APPROVAL_TIMEOUT_MAX_SECONDS = 7 * 24 * 60 * 60


def _validate_approval_timeout(value: int | None) -> None:
    """校验 approval_timeout_seconds：0 或 [60, 7 天]，越界抛 400。"""
    if value is None:
        return
    seconds = int(value)
    if seconds == 0:
        return
    if seconds < _APPROVAL_TIMEOUT_MIN_SECONDS or seconds > _APPROVAL_TIMEOUT_MAX_SECONDS:
        raise HTTPException(
            status_code=400,
            detail="approval_timeout_seconds 必须为 0（不过期）或 60 秒到 7 天之间",
        )


def _normalize_reasoning_effort(value: str | None) -> str:
    effort = (value or "").strip().lower()
    if effort == "max":
        effort = "xhigh"
    if effort not in _REASONING_EFFORTS:
        raise HTTPException(status_code=400, detail="reasoning_effort 必须为 low/medium/high/xhigh 或空")
    return effort

# 附件上传上限与「按文本发给模型」的 mime 白名单。
_ATTACHMENT_LIMIT = 10 << 20  # 10 MiB
_TEXTUAL_MIMES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
}


async def _read_attachment_body(request: Request) -> bytes:
    """读取原始附件字节，超限即 413，空则 400。"""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="附件内容为空")
    if len(raw) > _ATTACHMENT_LIMIT:
        raise HTTPException(status_code=413, detail="附件不能超过 10 MiB")
    return raw


def _attachment_target(root: str, conv_id: str, filename: str) -> tuple[str, Path]:
    """把 root/.chat-attachments/<conv_id>/<uuid>-<safe> 解析成 (相对路径, 绝对路径)。

    剥目录 + 白名单字符 + uuid 前缀，并校验最终路径落在 root 内（防穿越）。
    """
    safe_name = Path(filename.replace("\\", "/")).name
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in safe_name)
    if safe_name in ("", ".", ".."):
        safe_name = "attachment.bin"
    safe_conv = "".join(ch for ch in conv_id if ch.isalnum() or ch in ("-", "_")) or "conv"
    relative = f".chat-attachments/{safe_conv}/{uuid.uuid4().hex}-{safe_name}"
    base = Path(root).resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="非法的附件路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    return relative, target


# ── Agent → 用户 文件/图片展示（outbound media）──────────────────────────
# _media_part_from_result / _collect_attachment_media 已移到 agent.context_manager，
# 与定时任务路径（agent/scheduler.py 的 on_event）共用，避免两边各抄一份漂移。
# 这里直接 import 使用，不再重复定义。


# ── 调用者鉴权 ────────────────────────────────────────────────────────
# 一套 /agent/* 接口同时服务两类调用者：
#   - C 端用户（user_platform session cookie）：get_agent_caller 返回其 user_id(str)，
#     数据按 user_id 隔离（只看/管自己的 + 平台公共 Agent）。
#   - 管理员（/admin/login 换取的 Bearer token）：返回 None，不过滤，保持旧行为。
# 两者都拿不到 → 401。

async def _resolve_user_from_session() -> str | None:
    """从 user_platform C 端 session cookie 解析当前用户 id；无则 None。"""
    try:
        from admin import _current_user_cookie
        from user_platform.session import session_store, USER_SESSION_COOKIE
        cookie = _current_user_cookie.get(None)
        if not cookie:
            return None
        data = await session_store.get(USER_SESSION_COOKIE, cookie)
        return data.uid if data and data.uid else None
    except Exception:
        return None


async def _is_valid_admin(authorization: str | None) -> bool:
    """校验应急管理员 Bearer token（复用 admin session 存储）。"""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    try:
        import time
        from admin import _get_admin_session
        session = await _get_admin_session(authorization[7:])
        return bool(session and time.time() < session["expires_at"])
    except Exception:
        return False


async def get_agent_caller(authorization: str | None = Header(None)) -> str | None:
    """返回 user_id(str)=用户模式；None=管理员模式。两者皆无 → 401。"""
    uid = await _resolve_user_from_session()
    if uid:
        return uid
    if await _is_valid_admin(authorization):
        return None
    raise HTTPException(status_code=401, detail="未登录")


async def _resolve_agent_owner(caller: str | None) -> str:
    """create_agent 的 owner 落库值。

    caller 为 uid（C 端用户或经 C 端 session 登录的管理员）时直接用它；caller=None
    （仅应急管理员 Bearer）时解析到一个真实 admin user id 作为 owner，避免落库
    user_id=NULL 变成全员可见的平台 Agent。团队共享改由显式 is_team_shared 表达。
    """
    if caller is not None:
        return caller
    from user_platform.deps import _ensure_emergency_admin
    admin = await _ensure_emergency_admin()
    return str(admin.id)


async def _resolve_team_id_safe(user_id: str) -> str | None:
    """解析用户所属 team；失败返回 None（不阻断建 agent）。"""
    try:
        from user_platform.deps import resolve_team_id
        return await resolve_team_id(user_id)
    except Exception:
        return None


def _require_ch() -> None:
    """对话存储依赖 ClickHouse；未启用 -> 503 明确错误，不进 handler 抛 500。"""
    if not conversation_store.is_ready():
        raise HTTPException(
            status_code=503,
            detail="ClickHouse 未启用，Agent/聊天对话存储不可用",
        )

TaskStatus = Literal["queued", "running", "completed", "failed", "aborted"]


class AgentTaskRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    api_key: str | None = None
    model: str | None = None
    max_turns: int | None = Field(default=None, ge=1)
    workspace_root: str | None = None
    allowed_roots: list[str] | None = None
    denied_patterns: list[str] | None = None
    system_prompt: str = ""


class AgentTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    result: list[dict[str, Any]] | None = None
    error: str | None = None


class AgentStatusResponse(BaseModel):
    tasks: dict[str, int]


class ToolsSchemaResponse(BaseModel):
    tools: list[dict[str, Any]]


class SopCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    content: str = ""


class SopUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    enabled: bool | None = None
    expected_revision: int | None = None


class MemoryResponse(BaseModel):
    l1_insights: list[dict[str, Any]]
    l2_facts: list[dict[str, Any]]
    l4_archive: list[dict[str, Any]]


_TASKS: dict[str, AgentTaskResponse] = {}
_TASK_HANDLES: dict[str, asyncio.Task] = {}
_AGENT_CONFIG = AgentConfig()

# supervisor 模式的常驻红线（api.py 与 cdp_chat_service.py 两个注入点共用同一份，
# 防止两处文案漂移）。只内联红线关键句 + SOP 路径——SOP 正文（5KB）不进 prompt，
# 让模型 file_read；本仓库 agent 侧无 prompt cache，正文常驻是纯开销。
_SUPERVISOR_HINT = (
    "\n\n[Supervisor Mode] You supervise; you do NOT do the work. Never perform the "
    "task yourself — no file_write/file_patch, no side-effect code_run, no browser "
    "actions. Only read-only probing is allowed (file_read, web read_page, read-only "
    "inspect). Your loop: decompose the user's goal → spawn_subagent (role: "
    "explore|verify|build, self-contained task brief) → verify its evidence → rework "
    "via a new spawn if it fails. Delegation is your default action, not a last "
    "resort. Read memory/sop/supervisor_sop.md first for the full SOP."
)


def _build_tool_registry(agent_id: int | None = None, owner_user_id: str | None = None) -> ToolRegistry:
    """Build the fixed GA tool registry (GA atomic tools + capability_call).

    owner_user_id 传当前发起者（C 端 uid）：capability_call 建定时任务时用它落归属，
    否则任务 user_id 为 NULL、用户态列表看不到。
    """
    return ToolRegistry(ToolContext(AgentConfig()), agent_id=agent_id, owner_user_id=owner_user_id)


@router.post("/task", response_model=AgentTaskResponse)
async def create_task(request: AgentTaskRequest) -> AgentTaskResponse:
    """创建一次性 Agent 任务：立即返回 task_id（status=queued），后台异步执行。

    任务不经对话/事件流，结果只落在 GET /task/{task_id} 轮询的进程内任务表。
    """
    task_id = uuid.uuid4().hex
    task_state = AgentTaskResponse(task_id=task_id, status="queued")
    _TASKS[task_id] = task_state
    _TASK_HANDLES[task_id] = asyncio.create_task(_run_agent_task(task_id, request))
    return task_state


@router.get("/task/{task_id}", response_model=AgentTaskResponse)
async def get_task(task_id: str) -> AgentTaskResponse:
    """查询一次性任务状态；task_id 未找到 → 404。"""
    task_state = _TASKS.get(task_id)
    if task_state is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task_state


@router.post("/task/{task_id}/abort", response_model=AgentTaskResponse)
async def abort_task(task_id: str) -> AgentTaskResponse:
    """中止一次性任务：cancel 后台协程，queued/running 翻成 aborted（已结束的不改）。"""
    task_state = _TASKS.get(task_id)
    if task_state is None:
        raise HTTPException(status_code=404, detail="task not found")

    handle = _TASK_HANDLES.get(task_id)
    if handle is not None and not handle.done():
        handle.cancel()

    if task_state.status in {"queued", "running"}:
        task_state.status = "aborted"
        task_state.result = None
        task_state.error = "task aborted"
    return task_state


@router.get("/status", response_model=AgentStatusResponse)
async def get_status() -> AgentStatusResponse:
    """汇总本进程内一次性任务的计数（total/queued/running/active/completed/failed/aborted）。"""
    counts = {status: 0 for status in ("queued", "running", "completed", "failed", "aborted")}
    for task in _TASKS.values():
        counts[task.status] += 1
    counts["total"] = len(_TASKS)
    counts["active"] = counts["queued"] + counts["running"]
    ordered = {
        "total": counts["total"],
        "queued": counts["queued"],
        "running": counts["running"],
        "active": counts["active"],
        "completed": counts["completed"],
        "failed": counts["failed"],
        "aborted": counts["aborted"],
    }
    return AgentStatusResponse(tasks=ordered)


@router.get("/tools/schema", response_model=ToolsSchemaResponse)
async def get_tools_schema() -> ToolsSchemaResponse:
    """返回 Agent 工具注册表全量 schema（GA 原子工具 + capability_call）。"""
    return ToolsSchemaResponse(tools=_build_tool_registry().get_schema())


@router.get("/sops")
async def list_sops(caller: str | None = Depends(get_agent_caller)) -> dict:
    """列出 SOP 资源目录（含停用条目）并附每条正文；写操作仅管理员。"""
    from agent.sop_service import get_content, list_catalog

    rows = await list_catalog(include_disabled=True)
    for row in rows:
        try:
            row["content"] = await get_content(row["id"])
        except FileNotFoundError:
            row["content"] = ""
    return {"sops": rows}


@router.post("/sops")
async def create_sop(request: SopCreateRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """新建 SOP 资源。仅管理员可用（带 caller 的用户调用 → 403）。"""
    if caller is not None:
        raise HTTPException(403, "SOP 资源管理仅管理员可用")
    from agent.sop_service import create_sop as create

    return await create(request.name, request.description, request.content)


@router.patch("/sops/{sop_id}")
async def update_sop(sop_id: str, request: SopUpdateRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """更新 SOP（revision 乐观锁，冲突 409；内容校验失败 400）。仅管理员可用。"""
    if caller is not None:
        raise HTTPException(403, "SOP 资源管理仅管理员可用")
    from agent.sop_service import update_sop as update

    try:
        return await update(sop_id, request.model_dump(exclude_unset=True))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409 if "revision" in str(exc).lower() else 400, str(exc)) from exc


@router.delete("/sops/{sop_id}")
async def delete_sop(sop_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """删除 SOP 资源；仅管理员可用，未找到 → 404。"""
    if caller is not None:
        raise HTTPException(403, "SOP 资源管理仅管理员可用")
    from agent.sop_service import delete_sop as remove

    try:
        deleted = await remove(sop_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "SOP not found")
    return {"status": "ok"}


@router.get("/skills")
async def get_skills(category: str | None = None, limit: int = 100) -> dict:
    """Deprecated legacy endpoint; the platform Skill catalog was removed."""
    return {"skills": []}


@router.get("/memory", response_model=MemoryResponse)
async def get_memory(limit: int = 50) -> MemoryResponse:
    """Compatibility diagnostic view over GA files plus legacy L4 archive."""
    from agent import file_memory as fm

    memory = MemorySystem()
    l4_archive = await memory.get_l4_archive(limit=min(limit, 50))
    return MemoryResponse(
        l1_insights=[{"content": ""}],
        l2_facts=[{"content": ""}],
        l4_archive=l4_archive,
    )


@router.get("/config")
async def get_config() -> dict[str, Any]:
    """读取 Agent 主配置快照（内存态 AgentConfig）。"""
    return _AGENT_CONFIG.to_dict()


@router.post("/config")
async def update_config(config_update: dict[str, Any]) -> dict[str, Any]:
    """合并更新 Agent 主配置（仅覆盖传入键）。

    调度总开关已下线：调度意图只来自 per-agent 开关与任务创建/启用。
    """
    global _AGENT_CONFIG
    merged = _AGENT_CONFIG.to_dict()
    merged.update(config_update)
    _AGENT_CONFIG = AgentConfig.from_dict(merged)
    # 调度闸门不再有主配置总开关（agent.scheduler_enabled 已删）：调度意图只来自
    # per-agent 开关（/agents CRUD 里 _ensure_scheduler_running）和任务创建/启用。
    return _AGENT_CONFIG.to_dict()


# ---------------------------------------------------------------------------
# Agent CRUD endpoints
# ---------------------------------------------------------------------------

class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str | None = None
    description: str = ""
    system_prompt: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    enabled: bool = True
    memory_enabled: bool = True
    skill_auto_learn: bool = True
    scheduler_enabled: bool = False
    # Empty/legacy default → runtime computes the GA-style per-Agent dir
    # ``data/agents/<id>/``. Set explicitly only for node-bound workspaces.
    workspace_root: str = ""
    allowed_roots: list[str] = []
    denied_patterns: list[str] = ["/etc/", "/var/", ".env", ".git/", "node_modules/"]
    guardian_enabled: bool = False
    guardian_interval: int = 300
    autonomous_enabled: bool = False
    thinking_enabled: bool = False
    reasoning_effort: str = ""
    # agent 层 429 自动重试次数（仅瞬时码；0=关闭）。见 AgentConfig.llm_retry_429。
    llm_retry_429: int = 2
    # 需确认工具挂起等人裁决的上限（秒）；0=不过期。见 AgentConfig.approval_timeout_seconds。
    approval_timeout_seconds: int = 24 * 60 * 60
    # 上下文压缩：auto_compact 开着则每轮自动压缩历史；compact_threshold 是前端
    # token 卡占比达到该值时弹框提醒压缩的阈值（0~1）。见 AgentConfig.auto_compact。
    auto_compact: bool = True
    compact_threshold: float = 0.8
    # 浏览器（CDP 网页对话）能否执行 code_run。默认关：CDP 路径不挂审批协调器，
    # code_run 直接 denied 并提示开启本配置。用户端聊天页不受影响（照旧走审批）。
    browser_code_run_enabled: bool = False
    # 网关秘钥绑定：agent LLM 走网关 dispatch_entry（计费/限流/归属闭环）。
    # 主 Agent 必须选（网关 api_keys.id + 模型名）；子 Agent 可空（跟随主）。
    main_api_key_id: int | None = None
    main_model: str = ""
    subagent_api_key_id: int | None = None
    subagent_model: str = ""
    # 子 Agent 岗位/难度 → 模型映射（{"roles":{...},"categories":{...}}，值是网关
    # 模型 id）。空 = 未配 → 运行时逐层回落，行为与改动前一致。
    subagent_model_map: dict = {}
    # 定时任务默认 key/模型：无人值守跑批常要换个更便宜/更稳的模型，且不该借用
    # subagent_*（那是「主 agent 派生的并行子任务」）。留空则回退主 Agent。
    scheduled_api_key_id: int | None = None
    scheduled_model: str = ""
    mcp_user_id: int | None = None
    # 团队共享：true 时同 team 成员可见可用；false 仅 owner 可见。
    is_team_shared: bool = False


class UpdateAgentRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    max_turns: int | None = None
    enabled: bool | None = None
    memory_enabled: bool | None = None
    skill_auto_learn: bool | None = None
    scheduler_enabled: bool | None = None
    workspace_root: str | None = None
    allowed_roots: list[str] | None = None
    denied_patterns: list[str] | None = None
    guardian_enabled: bool | None = None
    guardian_interval: int | None = None
    autonomous_enabled: bool | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None
    llm_retry_429: int | None = None
    approval_timeout_seconds: int | None = None
    auto_compact: bool | None = None
    compact_threshold: float | None = None
    browser_code_run_enabled: bool | None = None
    main_api_key_id: int | None = None
    main_model: str | None = None
    subagent_api_key_id: int | None = None
    subagent_model: str | None = None
    subagent_model_map: dict | None = None
    scheduled_api_key_id: int | None = None
    scheduled_model: str | None = None
    mcp_user_id: int | None = None
    is_team_shared: bool | None = None


async def _validate_agent_gateway_key(api_key_id: int, caller: str | None) -> None:
    """校验 api_key_id 对该 Agent owner 可用（自有/分组系统 key）；不可用 → 抛 HTTP。

    复用聊天发送链路的解析器：个人 Agent（caller=用户）只能绑自己名下或所在分组
    授权的网关 key；平台 Agent（caller=None，管理员建）走全量查找。绑定不可用的 key
    会让 Agent 运行时报错，故在写库前就挡掉。
    """
    await _resolve_caller_api_key(api_key_id, caller)


async def _create_agent_principal_for(owner: str | None, name: str) -> int:
    """按 Agent 归属建 usage_type=agent 的 principal：平台 Agent 建平台级（owner=NULL），
    用户 Agent 建 user-owned。建 Agent 与启动对账共用这一条路径。"""
    import mcp_plugin_store

    if owner is None:
        principal = await mcp_plugin_store.create_platform_mcp_principal(
            f"agent-{name}",
            description=f"agent principal for {name}",
            enabled=True,
            chat_enabled=False,
            usage_type="agent",
        )
        return int(principal["id"])
    return await mcp_plugin_store.create_agent_mcp_principal(
        str(owner), f"agent-{name}", description=f"agent principal for {name}",
    )


async def reconcile_agent_mcp_principals() -> dict[str, int]:
    """启动时幂等对账：每个 Agent 都应绑定一条 usage_type='agent' 的 principal。

    需要补建/改绑的情形（含历史数据）：
      - mcp_user_id 为 NULL（老 Agent 从未建过）；
      - 指向的 principal 不存在（被删）；
      - 指向的 principal 存在但 usage_type != 'agent'（早期绑了 external principal）。

    错绑的旧 principal 在改绑后若没有别处引用，就地删除——避免留孤儿 principal
    占位且继续出现在用户侧 MCP 用户列表里。并发/重复执行安全：新建后用条件 UPDATE
    落绑，仅在当前 old 值未被并发改写时才删旧 principal。
    """
    from db import PostgresClient
    import mcp_plugin_store

    created = rebound = removed = kept = 0
    if not PostgresClient.pool:
        return {"agents": 0, "created": 0, "rebound": 0, "removed": 0, "already_ok": 0}

    async with PostgresClient.pool.acquire() as conn:
        agents = await conn.fetch("SELECT id, name, user_id, mcp_user_id FROM agents ORDER BY id")
    for row in agents:
        agent_id = int(row["id"])
        current = row["mcp_user_id"]
        ok = False
        if current is not None:
            bound = await mcp_plugin_store.get_mcp_principal(int(current))
            ok = bound is not None and bound.get("usage_type") == "agent"
        if ok:
            kept += 1
            continue
        new_id = await _create_agent_principal_for(row["user_id"], str(row["name"]))
        async with PostgresClient.pool.acquire() as conn:
            bound_id = await conn.fetchval(
                """
                UPDATE agents SET mcp_user_id=$2, updated_at=now()
                WHERE id=$1 AND (mcp_user_id IS NULL OR mcp_user_id=$3)
                RETURNING mcp_user_id
                """,
                agent_id, new_id, current,
            )
        if bound_id is None:
            # 并发下别的写入赢了（如用户同时在建 Agent）：丢弃本次新建，避免孤儿。
            await mcp_plugin_store.delete_mcp_principal(new_id)
            continue
        created += 1
        if current is not None and current != new_id:
            rebound += 1
            # 旧 principal 仅当无任何 agent 引用、且没有外部接入授权时才删——
            # 避免误删用户正在用的 external 身份（有 mcp_grants 授权说明在用）。
            async with PostgresClient.pool.acquire() as conn:
                in_use = await conn.fetchval(
                    "SELECT 1 FROM agents WHERE mcp_user_id=$1 AND id<>$2 LIMIT 1",
                    int(current), agent_id,
                )
            if in_use is None:
                granted = await mcp_plugin_store.list_services_for_mcp_user(int(current))
                if not granted:
                    await mcp_plugin_store.delete_mcp_principal(int(current))
                    removed += 1
    summary = {"agents": len(agents), "created": created, "rebound": rebound,
               "removed": removed, "already_ok": kept}
    if created:
        logger.info("[agent] startup principal reconcile: {}", summary)
    return summary


@router.post("/agents")
async def create_agent(request: CreateAgentRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """创建 Agent（敏感写操作，多重校验与副作用）。

    - 必填网关 key + 模型（main_api_key_id/main_model），形成计费/限流/归属闭环；
      子 Agent/定时 key 可选，写库前按 owner 校验 key 可用性。
    - 自动创建 usage_type=agent 的 MCP principal 并绑定（失败 500）；落库失败回滚
      该 principal 避免孤儿。
    - owner 恒为真实 uid；团队共享由 is_team_shared+team_id 表达。
    - 附带初始化 Agent 私有记忆文件，并签发 MCP 身份 token（明文只随本次响应返回，
      token 表只存哈希）。
    """
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    # 主 Agent 必须绑定可用网关 key + 模型（LLM 走网关，形成计费/限流/归属闭环）。
    if not request.main_api_key_id:
        raise HTTPException(400, "请选择主 Agent 的网关 API Key")
    if not (request.main_model or "").strip():
        raise HTTPException(400, "请选择主 Agent 的模型")
    await _validate_agent_gateway_key(request.main_api_key_id, caller)
    if request.subagent_api_key_id:
        await _validate_agent_gateway_key(request.subagent_api_key_id, caller)
    if request.scheduled_api_key_id:
        await _validate_agent_gateway_key(request.scheduled_api_key_id, caller)
    _validate_approval_timeout(request.approval_timeout_seconds)
    # 岗位/难度 → 模型映射归一 + 校验：空 map = 未配 → 存 '{}'（运行时逐层回落）。
    # 非空时每个模型值必须落在被使用 key 的可执行集合内（子 key 已绑用子 key，否则
    # 主 key——与运行时 fork/专用 key 分支同源）；非法值直接抛错，不静默丢。
    from agent.agent_main import _normalize_subagent_model_map
    normalized_map = _normalize_subagent_model_map(request.subagent_model_map)
    _map_values = [v for block in normalized_map.values() for v in block.values() if v]
    if _map_values:
        from user_platform.task_service import _validate_models_for_key
        await _validate_models_for_key(
            request.subagent_api_key_id or request.main_api_key_id,
            _map_values,
            active_model=_map_values[0],
        )
    subagent_model_map_json = json.dumps(normalized_map, ensure_ascii=False)
    # mcp_user_id 创建时由后端自动建一个 usage_type=agent 的 principal 并绑定，
    # 不再接受请求传入（用户在 Agent 详情页配置该 principal 的权限）。
    # 平台 Agent（caller=None）建平台级 principal（owner=NULL）。
    auto_principal_id: int | None = None
    try:
        auto_principal_id = await _create_agent_principal_for(caller, request.name)
    except Exception as exc:  # noqa: BLE001
        # MCP 配置主体是 Agent MCP 管理的必要组成，创建失败不能留下一个永远空白、
        # 无法管理 MCP 的 Agent。
        logger.exception("[agent] auto-create mcp principal failed for %s", request.name)
        raise HTTPException(500, f"初始化 Agent MCP 配置失败: {exc}") from exc
    # owner 永远是真实 uid（caller 为 uid 时用它；应急 Bearer 管理员解析真实 admin），
    # 不再写 user_id NULL（NULL 曾被当作平台 Agent 全员共享，与多用户隔离冲突）。
    # 团队共享由显式 is_team_shared + team_id 表达。
    owner_uid = await _resolve_agent_owner(caller)
    team_id = await _resolve_team_id_safe(owner_uid)
    async with PostgresClient.pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO agents(
                    name, display_name, description, api_key, model, system_prompt, max_turns,
                    enabled, memory_enabled, skill_auto_learn, scheduler_enabled,
                    workspace_root, allowed_roots, denied_patterns,
                    guardian_enabled, guardian_interval, autonomous_enabled,
                    thinking_enabled, reasoning_effort, llm_retry_429, mcp_user_id, user_id,
                    main_api_key_id, main_model, subagent_api_key_id, subagent_model,
                    subagent_model_map,
                    approval_timeout_seconds, team_id, is_team_shared,
                    scheduled_api_key_id, scheduled_model, browser_code_run_enabled,
                    auto_compact, compact_threshold
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26::jsonb,$27,$28,$29,$30,$31,$32,$33,$34,$35)
                RETURNING *
                """,
                request.name, request.display_name or request.name, request.description,
                "", request.main_model, request.system_prompt, request.max_turns,
                request.enabled, request.memory_enabled, request.skill_auto_learn,
                request.scheduler_enabled,
                request.workspace_root, request.allowed_roots, request.denied_patterns,
                request.guardian_enabled, request.guardian_interval, request.autonomous_enabled,
                request.thinking_enabled, request.reasoning_effort, request.llm_retry_429,
                auto_principal_id, owner_uid,
                request.main_api_key_id, request.main_model,
                request.subagent_api_key_id, request.subagent_model or "",
                subagent_model_map_json,
                request.approval_timeout_seconds,
                team_id, request.is_team_shared,
                request.scheduled_api_key_id, request.scheduled_model or "",
                request.browser_code_run_enabled,
                request.auto_compact, request.compact_threshold,
            )
        except Exception as e:
            # Agent 落库失败：回滚刚建的 principal，避免孤儿 principal。
            if auto_principal_id is not None:
                try:
                    import mcp_plugin_store as _store
                    await _store.delete_mcp_principal(auto_principal_id)
                except Exception:  # noqa: BLE001
                    pass
            raise HTTPException(500, str(e))
    agent_id = row["id"]
    result = await _agent_row_to_dict(row)
    # GA memory is Agent-private and file-backed. Initialise it immediately so
    # the first conversation has L1/L2 files and built-in SOP pointers even
    # before SOP selection.
    try:
        from agent.file_memory import ensure_agent_memory
        ensure_agent_memory(agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory] initialize Agent files failed agent=%s: %s", agent_id, exc)
    # 为 agent 签发一个身份 token，作为其启动时连内部 MCP runtime 的环境变量。
    # 明文只此一次返回（token 表只存哈希）；失败不阻断建 agent。
    try:
        import builtin_tool_store
        _token_row, mcp_token = await builtin_tool_store.issue_token(
            "agent", str(agent_id), display_token=False,
        )
        result["mcp_token"] = mcp_token
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp] issue agent token failed agent=%s: %s", agent_id, exc)
    _ensure_scheduler_running(request.scheduler_enabled)
    return result


@router.get("/agents")
async def list_agents(caller: str | None = Depends(get_agent_caller)) -> list[dict]:
    """列出当前调用者可见的 Agent：管理员看全部；用户看自己创建的 + 团队共享的。"""
    from db import PostgresClient
    if not PostgresClient.pool:
        return []
    # caller=None（管理员）看全部；caller=用户 看自己创建的 + 团队共享的 Agent。
    team_id = await _resolve_team_id_safe(caller) if caller else None
    rows = await PostgresClient.list_agents_for_caller(caller, team_id)
    return [await _agent_row_to_dict(row) for row in rows]


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """查询单个 Agent 详情（含会话/技能/SOP 统计）。

    可见性：自己创建的 + 团队共享；看不到别人的私有 Agent（404）。
    """
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agents WHERE id=$1", agent_id)
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    # 可见性：自己创建的 + 团队共享（is_team_shared 且 team_id 匹配调用者团队）；
    # 看不到别人的私有 Agent。改/删权限在 update/delete 里按 owner 校验。
    await _require_agent_access(agent_id, caller)
    result = await _agent_row_to_dict(row)
    # Add stats
    async with PostgresClient.pool.acquire() as conn:
        result["stats"] = {
            "conversations": await conn.fetchval(
                "SELECT COUNT(*) FROM agent_conversations WHERE agent_id=$1", agent_id
            ) or 0,
            "insights": 0,
            "facts": 0,
            "sops": 0,
            # L3 learned-skill text count (agent.memory legacy table), matching
            # the "L3 技能" label in the agent manager UI.
            "skills": await conn.fetchval(
                "SELECT COUNT(*) FROM agent_skills WHERE agent_id=$1", agent_id
            ) or 0,
            # scheduled_tasks 计数已删（定时任务面下线，cron 统一走工作流）。
        }
    try:
        from agent.sop_service import list_for_agent

        result["stats"]["sops"] = len((await list_for_agent(agent_id)).get("selected_ids", []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent] SOP stats unavailable agent=%s: %s", agent_id, exc)
    return result


@router.get("/agents/{agent_id}/skills")
async def get_agent_skills(agent_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """技能目录（对话输入框 `/` 弹层用）：运行时聚合 SOP 目录 + 生效 MCP 服务。

    只回名字/描述，不回正文——正文由模型在命中技能时按需 file_read（见
    skill_slash.collect_activated_skills，只注入基础信息到当前消息，不内联全文）。
    鉴权与 GET /agent/agents/{id}/sops 同口径（owner 或平台 agent）。
    """
    from db import PostgresClient
    from agent.skill_slash import build_catalog

    row = await PostgresClient.get_agent(agent_id)
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    owner = row.get("user_id")
    if caller is not None and owner is not None and str(owner) != str(caller):
        raise HTTPException(404, f"Agent {agent_id} not found")
    catalog = await build_catalog(agent_id, mcp_user_id=row.get("mcp_user_id"))
    return {"skills": [
        {
            "kind": it.kind,
            "name": it.name,
            "display_name": it.display_name or it.name,
            "description": it.description,
            "enabled": it.enabled,
        }
        for it in catalog.items
    ]}


@router.get("/agents/{agent_id}/sops")
async def get_agent_sops(agent_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """Read-only view of the SOPs in effect for one Agent.

    Effective SOPs = built-in catalog SOPs (auto-effective for every Agent) +
    custom catalog SOPs explicitly bound to this Agent + Agent-private distilled
    SOPs under ``memory/sop/``. Per-Agent binding is a backend capability; the
    resource page's install interaction is not wired yet.
    """
    from db import PostgresClient
    from agent.sop_service import list_for_agent

    row = await PostgresClient.get_agent(agent_id)
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    owner = row.get("user_id")
    if caller is not None and owner is not None and str(owner) != str(caller):
        raise HTTPException(404, f"Agent {agent_id} not found")
    return await list_for_agent(agent_id)


@router.get("/agents/by-mcp-user/{principal_id}")
async def get_agent_by_mcp_user(principal_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """反查 principal → Agent：返回绑定该 mcp_user_id 的 agent_id（无则 null）。

    供 MCP 用户管理弹框「进入 Agent 详情」使用。ownership：用户只能反查自己的 Agent；
    管理员可查平台 Agent。
    """
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, user_id FROM agents WHERE mcp_user_id=$1", int(principal_id))
    if not row:
        return {"agent_id": None}
    # 归属/共享校验：owner 或同团队共享可反查；否则视作不存在（不泄露 agent 存在性）。
    try:
        await _require_agent_access(int(row["id"]), caller)
    except HTTPException:
        return {"agent_id": None}
    return {"agent_id": int(row["id"])}


@router.get("/agents/{agent_id}/mcp-diagnostics")
async def get_agent_mcp_diagnostics(agent_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """会话诊断：这个 Agent 实际生效的 MCP 服务 + 绑定的 principal + params + token 类型。

    供 AgentConversation 右上角「查看详情」弹框用——回答用户「这个会话到底挂了哪些
    MCP 工具、以什么身份调用」。只读，不含明文 token。
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM agents WHERE id=$1", agent_id)
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    # 可见性：同 get_agent——owner 或团队共享成员可见；别人的私有 Agent → 404。
    await _require_agent_access(agent_id, caller)

    import mcp_plugin_store
    import builtin_tool_store
    from agent.mcp_client import resolve_effective_services

    agent_d = await _agent_row_to_dict(row)

    # principal 信息 + params（params 含 param_key→param_value，param_value 是资源 id）。
    principal_id = agent_d.get("mcp_user_id")
    principal: dict | None = None
    params: list[dict] = []
    if principal_id is not None:
        principal = await mcp_plugin_store.get_mcp_user(int(principal_id), mask_token=True)
        try:
            params = await mcp_plugin_store.list_principal_params(int(principal_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent] diagnostics list_principal_params failed: %s", exc)
            params = []
    param_by_key = {p["param_key"]: p["param_value"] for p in params if p.get("param_key")}

    # 有效服务完全由 principal 授权推导（agent 不再自带 mcp_servers）。
    try:
        effective = await resolve_effective_services(principal_id)
    except Exception as exc:  # noqa: BLE001 — 解析失败不阻断，返回空 + 错误原因
        logger.warning("[agent] diagnostics resolve_effective_services failed: %s", exc)
        effective = []

    # 内置服务 → 该服务鉴权用的 param_key。单一来源在 sse_gateway（鉴权热路径），
    # 这里导入复用，避免与网关判权用的映射漂移。
    from mcp_runtime.sse_gateway import _BUILTIN_SERVICE_PARAM_KEY as builtin_param_key

    services: list[dict] = []
    for svc in effective:
        svc_id = svc.get("id")
        name = svc.get("name")
        tools_cache = svc.get("tools_cache") or []
        try:
            auth = await mcp_plugin_store.get_service_auth(int(svc_id)) if svc_id is not None else None
        except Exception:
            auth = None
        # 该 principal 在本服务下能操控的资源（按 param 绑定的 builtin_tool_resources）。
        resources: list[dict] = []
        pkey = builtin_param_key.get(name or "")
        if pkey:
            bound_value = param_by_key.get(pkey)
            if bound_value and bound_value.isdigit():
                try:
                    resource = await builtin_tool_store.get_resource(int(bound_value))
                except Exception:
                    resource = None
                if resource:
                    resources.append({
                        "id": int(resource["id"]),
                        "kind": resource.get("resource_type"),
                        "name": resource.get("name") or resource.get("display_name") or str(resource["id"]),
                        "enabled": bool(resource.get("enabled", True)),
                    })
        services.append({
            "id": svc_id, "name": name,
            "display_name": svc.get("display_name") or name,
            "kind": svc.get("kind"),
            "builtin": bool(svc.get("builtin")),
            "enabled": bool(svc.get("enabled")),
            "auth_enabled": bool(auth and auth.get("auth_enabled")),
            # 本服务能操控的资源（cdp→浏览器客户端、mail→邮箱账户）；无则空。
            "param_key": pkey,
            "resources": resources,
            # 工具列表：tools_cache 里的 name + description（不回传 input_schema 全量，太长）。
            # 备用——按需求方法在工具处展示，这里仍返回供需要时取用。
            "tools": [
                {"name": t.get("name"), "description": t.get("description") or ""}
                for t in tools_cache if isinstance(t, dict) and t.get("name")
            ] if isinstance(tools_cache, list) else [],
        })

    return {
        "agent_id": int(agent_id),
        "agent_name": agent_d.get("display_name") or agent_d.get("name"),
        "agent_owner_user_id": agent_d.get("user_id"),
        "mcp_user_id": principal_id,
        "principal": principal,
        "params": params,
        # 本次会话调用 MCP 的身份类型：绑了 principal→identity token；否则走 service token。
        "token_kind": "identity" if principal_id is not None else "service",
        "effective_services": services,
    }


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: int, request: UpdateAgentRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """部分更新 Agent 配置（仅更新传入字段）。

    权限：仅 owner 可改（管理员除外）。网关 key 绑定显式传入才更新（允许 null 解绑）；
    subagent_model_map 非空时按生效子 key 校验模型可执行集合；mcp_user_id 不支持改绑。
    """
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")

    # 编辑权限：仅 owner 可改自己的 Agent；不再有「平台 Agent 任意人可改」的口径
    # （user_id IS NULL 的历史行已迁移归属）。删除权限在 delete_agent 里同样按 owner。
    if caller is not None:
        owned = await PostgresClient.get_agent_owned(caller, agent_id)
        if owned is None:
            raise HTTPException(404, f"Agent {agent_id} not found")

    # Build dynamic SET clause from non-None fields
    updates = []
    params: list[Any] = [agent_id]
    param_idx = 2

    # 网关秘钥绑定：显式出现在请求里才更新。写库前校验 key 对该 Agent owner 可用。
    # owner=None 表示平台 Agent（走管理员全量查找）；否则用其 owner 校验归属。
    owner = None if caller is None else caller
    if request.main_api_key_id is not None:
        await _validate_agent_gateway_key(request.main_api_key_id, owner)
    if request.subagent_api_key_id is not None:
        await _validate_agent_gateway_key(request.subagent_api_key_id, owner)
    if request.scheduled_api_key_id is not None:
        await _validate_agent_gateway_key(request.scheduled_api_key_id, owner)
    # MCP 服务授权由绑定 principal 管理，Agent 本身不再保存服务清单。
    # mcp_user_id 由建 Agent 时后端自动绑定，详情页不允许换绑——用户通过通用权限
    # 编辑器配置该已绑定 principal 的权限，而非改绑别的 principal。
    # （request.mcp_user_id 即使传入也忽略。）
    _validate_approval_timeout(request.approval_timeout_seconds)

    field_map = {
        "display_name": str, "description": str,
        "system_prompt": str, "max_turns": int, "enabled": bool, "memory_enabled": bool,
        "skill_auto_learn": bool, "scheduler_enabled": bool, "workspace_root": str,
        "allowed_roots": list, "denied_patterns": list, "guardian_enabled": bool,
        "guardian_interval": int, "autonomous_enabled": bool,
        "thinking_enabled": bool, "reasoning_effort": str, "llm_retry_429": int,
        "approval_timeout_seconds": int, "browser_code_run_enabled": bool,
        "auto_compact": bool, "compact_threshold": float,
        "main_model": str, "subagent_model": str, "scheduled_model": str,
    }
    for field_name, field_type in field_map.items():
        value = getattr(request, field_name, None)
        if value is not None:
            updates.append(f"{field_name}=${param_idx}")
            params.append(value)
            param_idx += 1

    # 网关 key id：显式出现在请求里才更新（允许传 null 解绑，子 Agent 回退到跟随主）。
    for key_field in ("main_api_key_id", "subagent_api_key_id", "scheduled_api_key_id"):
        if key_field in request.model_fields_set:
            updates.append(f"{key_field}=${param_idx}")
            params.append(getattr(request, key_field))
            param_idx += 1

    # 岗位/难度 → 模型映射：显式出现才更新（允许传 {} 清空回退旧行为）。非空时校验
    # 每个模型值落在「生效的子 key」可执行集合内——key 可能本次没改，故现读一行解析
    # 生效 key（子 key 已绑用子 key，否则主 key），与运行时同源。
    if "subagent_model_map" in request.model_fields_set:
        from agent.agent_main import _normalize_subagent_model_map
        normalized_map = _normalize_subagent_model_map(request.subagent_model_map)
        _map_values = [v for block in normalized_map.values() for v in block.values() if v]
        if _map_values:
            from user_platform.task_service import _validate_models_for_key
            async with PostgresClient.pool.acquire() as conn:
                cur = await conn.fetchrow(
                    "SELECT main_api_key_id, subagent_api_key_id FROM agents WHERE id=$1",
                    agent_id,
                )
            eff_sub = request.subagent_api_key_id if "subagent_api_key_id" in request.model_fields_set else (cur["subagent_api_key_id"] if cur else None)
            eff_main = request.main_api_key_id if "main_api_key_id" in request.model_fields_set else (cur["main_api_key_id"] if cur else None)
            await _validate_models_for_key(
                eff_sub or eff_main, _map_values, active_model=_map_values[0],
            )
        updates.append(f"subagent_model_map=${param_idx}::jsonb")
        params.append(json.dumps(normalized_map, ensure_ascii=False))
        param_idx += 1

    # 团队共享开关：仅 owner 可切换（owner 校验已在上面通过）。
    if request.is_team_shared is not None:
        updates.append(f"is_team_shared=${param_idx}")
        params.append(bool(request.is_team_shared))
        param_idx += 1

    # mcp_user_id 不再支持 update 改绑（建 Agent 时自动绑定，详情页只配权限）。

    if not updates:
        raise HTTPException(400, "No fields to update")

    updates.append("updated_at=now()")
    set_clause = ", ".join(updates)
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE agents SET {set_clause} WHERE id=$1 RETURNING *", *params
        )
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    _ensure_scheduler_running(request.scheduler_enabled)
    return await _agent_row_to_dict(row)


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """删除 Agent（级联清理 SOP 绑定等派生数据）。用户模式只能删自己创建的。"""
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    # 用户模式：只能删自己创建的 Agent。
    if caller is not None:
        if await PostgresClient.get_agent_owned(caller, agent_id) is None:
            raise HTTPException(404, f"Agent {agent_id} not found")
    async with PostgresClient.pool.acquire() as conn:
        result = await conn.execute("DELETE FROM agents WHERE id=$1", agent_id)
    if result.endswith("0"):
        raise HTTPException(404, f"Agent {agent_id} not found")
    try:
        from user_platform.models_skill import AgentSopBinding

        await AgentSopBinding.filter(agent_id=agent_id).delete()
    except Exception as exc:  # noqa: BLE001 - main Agent deletion remains authoritative
        logger.warning("[agent] delete SOP bindings failed agent=%s: %s", agent_id, exc)
    return {"ok": True, "msg": f"Agent {agent_id} deleted"}


@router.post("/agents/{agent_id}/toggle")
async def toggle_agent(agent_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """切换 Agent 启用/停用。用户模式只能切换自己创建的 Agent。"""
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    # 用户模式：只能切换自己创建的 Agent。
    if caller is not None and await PostgresClient.get_agent_owned(caller, agent_id) is None:
        raise HTTPException(404, f"Agent {agent_id} not found")
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE agents SET enabled = NOT enabled, updated_at=now() "
            "WHERE id=$1 RETURNING id, enabled", agent_id
        )
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return {"id": row["id"], "enabled": row["enabled"]}


@router.get("/agents/{agent_id}/memory")
async def get_agent_memory(
    agent_id: int, limit: int = 50, caller: str | None = Depends(get_agent_caller)
) -> MemoryResponse:
    """读取 Agent 私有记忆（L1 洞察 / L2 事实 / L4 归档）。

    记忆是 Agent 私有数据：仅 owner 或同团队共享成员可读。
    """
    from db import PostgresClient
    # 记忆是 Agent 私有数据：仅 owner（或同团队共享 agent 的成员）可读。
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    await _require_agent_access(agent_id, caller)
    from agent import file_memory as fm

    fm.ensure_agent_memory(agent_id)
    memory = MemorySystem(agent_id=agent_id)
    l4 = await memory.get_l4_archive(limit=limit)
    return MemoryResponse(
        l1_insights=[{"content": fm.read_l1(agent_id)}],
        l2_facts=[{"content": fm.read_l2(agent_id)}],
        l4_archive=l4,
    )


async def _agent_row_to_dict(row) -> dict:
    """Convert an agent DB row to a JSON-serializable dict."""
    d = dict(row)
    # Serialize datetime fields
    for key in ("created_at", "updated_at"):
        if key in d and d[key] is not None:
            d[key] = d[key].isoformat()
    # Mask API key in responses
    if "api_key" in d and d["api_key"]:
        key = d["api_key"]
        if len(key) > 8:
            d["api_key_masked"] = key[:4] + "****" + key[-4:]
        else:
            d["api_key_masked"] = "****"
        # 不向前端回传明文 key
        d.pop("api_key", None)
    # 网关秘钥绑定回显：主/子 Agent 各自的 api_key_id + 模型名（秘钥值不回传，
    # 只回 id 供选择器回显，脱敏名由前端按 listUsableKeys 匹配展示）。
    d["main_api_key_id"] = d.get("main_api_key_id")
    d["main_model"] = d.get("main_model") or d.get("model") or ""
    d["subagent_api_key_id"] = d.get("subagent_api_key_id")
    d["subagent_model"] = d.get("subagent_model") or ""
    # 岗位/难度 → 模型映射回显：asyncpg 把 JSONB 返回为 JSON 文本，归一成 dict 防
    # 前端拿到 str。空 map = 未配，前端按 {} 渲染。
    from agent.agent_main import _normalize_subagent_model_map as _norm_map
    d["subagent_model_map"] = _norm_map(d.get("subagent_model_map"))
    # 定时任务默认绑定回显（空 = 跟随主 Agent）。
    d["scheduled_api_key_id"] = d.get("scheduled_api_key_id")
    d["scheduled_model"] = d.get("scheduled_model") or ""
    return d



async def _run_agent_task(task_id: str, request: AgentTaskRequest) -> None:
    from agent.agent_main import GenericAgent

    task_state = _TASKS[task_id]
    task_state.status = "running"
    try:
        config = AgentConfig.from_dict(request.model_dump(exclude_none=True, exclude={"prompt", "system_prompt", "max_turns"}))
        result = await GenericAgent(config, tools=_build_tool_registry()).run_task(
            request.prompt,
            system_prompt=request.system_prompt,
            max_turns=request.max_turns,
        )
        task_state.status = "completed"
        task_state.result = result
        task_state.error = None
    except asyncio.CancelledError:
        task_state.status = "aborted"
        task_state.result = None
        task_state.error = "task aborted"
        raise
    except Exception as exc:
        task_state.status = "failed"
        task_state.result = None
        task_state.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()


# ── 对话管理 ─────────────────────────────────────────────────────────

_CONV_TASKS: dict[str, asyncio.Task] = {}
# 协作式停止信号：abort 端点先置位 → agent_loop 每轮调 AI 前/code_run 等子进程时
# 检查它 → 命中即杀子进程树并 raise。与 _CONV_TASKS 同生命周期（_on_turn_done 清理）。
# cancel() 仍作为兜底：置位管「查得到就停」，cancel 管「卡在 await 里的那次」。
_CONV_STOPS: dict[str, asyncio.Event] = {}


async def _reconcile_orphaned_streaming(conv_id: str) -> None:
    """把「没有活任务却仍标记 streaming 的最后一条 assistant 消息」翻成 error。

    发生场景：进程重启 / run_agent 被 GC / SSE 客户端断开后任务丢失，但
    assistant 占位消息从未走到结尾的 update_message(status="done")。结果就是
    刷新页面看到一条永远在「思考中…」的空壳。这里在发送新消息或读取会话
    元数据时对账：只要本进程没有该会话的活任务，就把残留 streaming 标记为
    已中断，前端回灌时不再无限转圈，也不会卡住新一轮发送的 409 检查。
    """
    # 本进程仍有该会话的活任务 → 那条 streaming 是**真的**在跑，绝不能翻成 error
    # （否则刷新一次就把正在执行的回复标记成失败）。docstring 的「没有活任务」前提
    # 必须显式守住。
    live = _CONV_TASKS.get(conv_id)
    if live is not None and not live.done():
        return
    try:
        messages = await conversation_store.get_messages(conv_id)
    except Exception:  # noqa: BLE001 — 对账失败不能阻断主流程
        return
    # 找最后一条 assistant 且 status=streaming 的消息
    orphan_id = None
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        if msg.get("status") == "streaming":
            orphan_id = msg.get("id")
        break
    if orphan_id is None:
        return
    await conversation_store.update_message(
        orphan_id,
        status="error",
        error="任务未正常结束（进程重启或连接中断，已自动标记为中断）",
    )


class CreateConversationRequest(BaseModel):
    title: str = "新对话"
    system_prompt: str = ""
    model: str = ""
    llm_model_id: int | None = None
    agent_id: int | None = None
    node_id: str | None = None
    context: Literal["marketplace_admin"] | None = None
    reasoning_effort: str | None = None


class UpdateConversationRequest(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    llm_model_id: int | None = None
    reasoning_effort: str | None = None
    # mode 是会话状态：一改即生效（后续轮/重试都沿用），不是单次请求参数。
    mode: str | None = None


class BatchDeleteConversationsRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=100)


class TerminalContext(BaseModel):
    """Per-turn terminal snapshot from the node terminal page.

    ``cwd`` is the directory the operator is currently looking at — it is also
    the working directory agent commands run in, so the model never picks it.
    ``terminal_id`` binds this turn to the PTY the browser is attached to, so an
    approved command runs in the terminal the operator can actually see.
    ``recent_lines`` is a tail of the xterm scrollback. All are transient
    context for this turn only — never persisted into the system_prompt.
    """
    cwd: str = ""
    terminal_id: str = ""
    recent_lines: list[str] = Field(default_factory=list)


class GoalConfig(BaseModel):
    """Optional configuration for goal mode (GA chapter6.6)."""
    objective: str = Field(..., min_length=1)
    budget_seconds: int = Field(..., ge=60)
    max_turns: int | None = Field(default=None, ge=1)


class RalphConfig(BaseModel):
    """Optional configuration for ralph mode (fresh child per round)."""
    objective: str = Field(..., min_length=1)
    max_rounds: int = Field(default=16, ge=1, le=64)


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)
    max_turns: int | None = Field(default=None, ge=1)
    terminal_context: TerminalContext | None = None
    reasoning_effort: str | None = None
    # GA chapter6 execution modes. interact = one-shot Q&A (default).
    # plan = complex-task planning (plan_sop, plan.md). goal = open objective +
    # time budget driven by Guardian until budget/turns exhausted.
    # ralph = open objective driven round by round; each round is a FRESH bounded
    # agent loop carrying only the objective + the previous round's structured
    # report, until the report says complete/blocked or the round cap is hit.
    # auto = 与 interact 同样的执行流程，但审批全部自动放行：node_shell_exec /
    # code_run / 越界 file_write 三类审批卡都不弹，直接执行（审计仍记录）。
    # 等价于 Claude Code 的 bypassPermissions / Codex 的 danger-full-access，
    # 是用户显式选择的高权限模式。
    # supervisor = 监督模式：主 agent 不下场干活，只拆解 → 派子 agent → 按证据验收
    # → 返工（红线内联见 _SUPERVISOR_HINT，全文见 memory/sop/supervisor_sop.md）。
    # 等价于 GA 的 Conductor 模式 / 角色强制：委派是唯一合法动作。
    mode: Literal["interact", "plan", "goal", "ralph", "auto", "supervisor"] = "interact"
    goal_config: GoalConfig | None = None
    ralph_config: RalphConfig | None = None


class WorkflowDefRequest(BaseModel):
    """Workflow definition create/update. ``definition`` is the declarative graph
    (see agent/workflow.py); it is statically validated on save.

    ``name`` is the machine id (unique per owner); ``label`` is the Chinese
    display name the UI shows everywhere (required — the frontend gates it, and
    the store persists it as given). A workflow is an independent artifact
    owned by the caller — no agent binding."""
    name: str = Field(..., min_length=1, max_length=200)
    label: str = Field(default="", max_length=200)
    description: str = ""
    definition: dict[str, Any]
    # 服务重启时在跑的 run：默认重跑一遍（新的 run 记录，旧记录标 interrupted）。
    # 非幂等工作流（重复写文件/调接口）可显式关掉。
    retry_on_reboot: bool = True


class WorkflowUpdateRequest(BaseModel):
    definition: dict[str, Any]
    label: str | None = None
    description: str | None = None
    retry_on_reboot: bool | None = None


class WorkflowRunRequest(BaseModel):
    """Manual run: launch args exposed to the graph as $args.*.

    ``trigger_id`` 命名定义里声明的一个启动方式（triggers）：其 args 打底，
    这里显式给的 args 逐键覆盖。不传 = 默认 manual（无打底）。

    ``single_account_only``（运行弹框「只跑一个账号测试」）：account_list 扇出
    路径只跑**第一个**账号，$input.accounts = 单个账号对象（与定时逐账号扇出的
    单跑格式一致）——给用户测试定时调用、不用真跑 N 遍。

    ``pick_accounts``（运行弹框「选具体账号」）：account_list 按 username 白名单
    过滤解析出的账号——扇出只跑选中的账号，整体一次 $input.accounts = 选中数组。
    """
    args: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    trigger_id: str | None = None
    single_account_only: bool = False
    pick_accounts: list[str] | None = None


def _stream_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Agent-SSE-Version": "2",
    }


# ── 对话端点 ────────────────────────────────────────────────────────

async def _provision_marketplace_mcp(agent_id: int, caller: str | None) -> None:
    """Grant the reserved marketplace MCP only from the admin conversation flow."""
    if caller is not None:
        # C-side admin sessions resolve to a user id; verify the actual role.
        from user_platform.models import User

        user = await User.get_or_none(id=caller)
        if user is None or user.role != "admin" or user.is_deleted or user.is_blocked:
            raise HTTPException(403, "市场管理 Agent 仅管理员可用")
    from db import PostgresClient
    import mcp_plugin_store

    row = await PostgresClient.get_agent(agent_id)
    if not row or row.get("user_id") is not None:
        raise HTTPException(403, "市场管理只能使用管理员创建的平台 Agent")
    principal_id = row.get("mcp_user_id")
    if principal_id is None:
        # Agent principal 由服务启动对账保证；请求热路径不再隐式创建数据。
        raise HTTPException(503, "Agent MCP principal 尚未初始化，请重启服务完成启动对账")
    service = await mcp_plugin_store.get_service_by_name("marketplace-status")
    if not service or not service.get("enabled"):
        raise HTTPException(503, "市场管理 MCP 不可用")
    current = set(await mcp_plugin_store.list_services_for_mcp_user(int(principal_id)))
    if int(service["id"]) not in current:
        current.add(int(service["id"]))
        await mcp_plugin_store.set_services_for_mcp_user(int(principal_id), sorted(current))


@router.post("/conversations")
async def create_conversation(request: CreateConversationRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """创建 Agent 对话（落库 + 注册进会话注册表）。

    - 节点对话必须选 Agent，且调用者须持有该节点绑定（403）。
    - 用户模式只能用自己的/团队共享的 Agent 开对话；市场管理对话会预置市场 MCP。
    - system_prompt/model 缺省跟随 Agent 主配置；场景预置（scene_context）一次性
      写进 system_prompt 并持久化 chat_settings。
    """
    _require_ch()
    from db import PostgresClient

    title = request.title
    system_prompt = request.system_prompt
    model = request.model
    llm_model_id = request.llm_model_id

    if request.node_id and not request.agent_id:
        raise HTTPException(status_code=400, detail="节点 AI 对话必须选择 Agent")

    # 用户模式：只能用自己的 Agent 或团队共享的 Agent 开对话；别人的私有 Agent → 404。
    # 不再放行 user_id IS NULL 的历史「平台 Agent」（那会让任意人拿它开对话、共享其记忆）。
    if caller is not None and request.agent_id:
        await _require_agent_access(int(request.agent_id), caller)

    if request.context == "marketplace_admin":
        if not request.agent_id:
            raise HTTPException(400, "市场管理对话必须选择 Agent")
        await _provision_marketplace_mcp(request.agent_id, caller)

    if request.node_id:
        if not await _caller_authorized_for_node(caller, request.node_id):
            raise HTTPException(status_code=403, detail="无权操作该节点")
        agent_row = await PostgresClient.get_agent(request.agent_id) if request.agent_id else None
        if not agent_row or not agent_row.get("enabled", True):
            raise HTTPException(status_code=400, detail="所选 Agent 不可用")
        if not agent_row.get("main_api_key_id") or not agent_row.get("main_model"):
            raise HTTPException(status_code=400, detail="所选 Agent 未配置网关 API Key 或模型")

    # 模型跟随 Agent 的主网关模型（main_model）；对话级不再引用旧 agent_llm_models。
    # request.model 若显式给出则作为对话级覆盖（须是该 Agent 网关 key 允许的模型名）。
    if request.agent_id and (not system_prompt or not model or not title):
        agent = await PostgresClient.get_agent(request.agent_id)
        if agent:
            system_prompt = system_prompt or agent.get("system_prompt") or ""
            model = model or agent.get("main_model") or ""
            title = title or agent.get("display_name") or agent.get("name") or "新对话"

    # 场景预置统一走 scene_context：按入口入参（node_id / context）归一出场景，
    # 稳定身份键 + 场景规则一次性写进对话 system_prompt，后续轮次不再重复追加。
    # 易变的每轮事实（终端 cwd/最近输出）不在这里，见 send_message 的 terminal_context。
    # node 场景顺带解析宿主 shell（POSIX/PowerShell），让场景规则告知模型命令语法。
    node_flavor = ""
    if request.node_id:
        node_flavor = await _node_shell_flavor(request.node_id)
    scene = scene_context.normalize(
        node_id=request.node_id, context=request.context, node_shell_flavor=node_flavor or None,
    )
    system_prompt = scene_context.append_prompt(system_prompt, scene)

    chat_settings: dict[str, Any] = scene_context.persist(scene)
    if request.reasoning_effort is not None:
        chat_settings["reasoning_effort"] = _normalize_reasoning_effort(request.reasoning_effort)
    conv = await conversation_store.create_conversation(
        title=title,
        system_prompt=system_prompt,
        model=model,
        agent_id=request.agent_id,
        llm_model_id=llm_model_id,
        user_id=caller,
        chat_settings=chat_settings or None,
    )
    # 建会话即入列注册表（= 实例添加的显式时机）。
    conversation_state.register(conv)
    return conv


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    agent_id: int | None = None,
    node_id: str | None = None,
    search: str | None = None,
    paged: bool = False,
    caller: str | None = Depends(get_agent_caller),
) -> Any:
    """列出当前调用者的 Agent 对话（kind='agent'）。

    paged=false 简单列表；paged=true 游标分页，支持 agent_id/node_id/search 过滤
    （游标非法 → 400）。
    """
    _require_ch()
    if not paged:
        return await conversation_store.list_conversations_for_caller(caller, limit=limit, kind="agent")
    try:
        return await conversation_store.list_conversations_for_caller_paged(
            caller, limit=limit, kind="agent", cursor=cursor, agent_id=agent_id,
            node_id=node_id, search=search,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/conversations/{conv_id}/meta")
async def get_conversation_meta(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """读取对话元数据（刷新页面恢复会话用）。

    顺手对账残留 streaming 占位（翻成 error），并归一 run_state 供前端恢复
    「上次在重试/报错」的显示。
    """
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    # 刷新页面读会话元数据时顺手对账残留 streaming：进程重启/断线后留下的
    # 永久 streaming 占位消息会被翻成 error，前端回灌不再无限转圈。
    await _reconcile_orphaned_streaming(conv_id)
    # 会话运行状态：库内存的是 JSON 串，这里归一成对象再透出（None = 上一轮已
    # 正常完结），前端据此恢复「上次在重试/报错」的显示，而不是只有一个转圈。
    conv["run_state"] = run_state.restore(conv)
    return conv


@router.get("/conversations/{conv_id}/messages")
async def list_conversation_messages(
    conv_id: str,
    limit: int = Query(7, ge=1, le=50),
    cursor: int | None = None,
    caller: str | None = Depends(get_agent_caller),
) -> dict:
    """按用户轮取对话消息分页（timeline 视图，游标翻页）。"""
    _require_ch()
    if await conversation_store.get_conversation_owned(caller, conv_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return await conversation_store.get_messages_page_by_user_turns(conv_id, limit=limit, cursor=cursor, timeline=True)


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """取一条对话的完整快照（元数据 + 全量消息历史）。"""
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    messages = await conversation_store.get_messages(conv_id, timeline=True)
    return {"conversation": conv, "messages": messages}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """删除对话（破坏性操作）。

    归属校验后删除；运行中的会话任务先协作式停止再 cancel；回收会话注册表实例。
    """
    _require_ch()
    # 归属校验：用户只能删自己的对话
    if await conversation_store.get_conversation_owned(caller, conv_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    # 如果有正在运行的任务，先取消（协作式停止 + cancel，与 abort 端点同口径）
    task = _CONV_TASKS.pop(conv_id, None)
    if task and not task.done():
        ev = _CONV_STOPS.pop(conv_id, None)
        if ev is not None:
            ev.set()
        task.cancel()
    else:
        _CONV_STOPS.pop(conv_id, None)
    deleted = await conversation_store.delete_conversation(conv_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation not found")
    # 回收会话注册表实例（与删除同生命周期点，否则内存泄漏）。
    conversation_state.evict(conv_id)
    return {"deleted": True}


async def _batch_delete_conversations(conv_ids: list[str], caller: str | None) -> dict:
    """批量删除会话：逐条做归属校验后删除，未找到/无权的 id 一律归入 not_found，
    不区分两者以免泄漏存在性（与单删的 get_conversation_owned→404 口径一致）。
    运行中的会话任务同样要先取消（与单删一致）。
    """
    deleted = 0
    not_found: list[str] = []
    for conv_id in conv_ids:
        if await conversation_store.get_conversation_owned(caller, conv_id) is None:
            not_found.append(conv_id)
            continue
        task = _CONV_TASKS.pop(conv_id, None)
        if task and not task.done():
            ev = _CONV_STOPS.pop(conv_id, None)
            if ev is not None:
                ev.set()
            task.cancel()
        else:
            _CONV_STOPS.pop(conv_id, None)
        if await conversation_store.delete_conversation(conv_id):
            deleted += 1
            conversation_state.evict(conv_id)
        else:
            not_found.append(conv_id)
    return {"deleted": deleted, "not_found": not_found}


@router.post("/conversations/batch-delete")
async def batch_delete_conversations(
    request: BatchDeleteConversationsRequest, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """批量删除 Agent 对话：未找到/无权的 id 一律归入 not_found（不区分两者）。"""
    _require_ch()
    return await _batch_delete_conversations(request.ids, caller)


@router.patch("/conversations/{conv_id}")
async def update_conversation(conv_id: str, request: UpdateConversationRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """部分更新对话。

    title/system_prompt 等内容字段走 store；mode/reasoning_effort/model 等运行时
    状态经会话注册表统一写（内存 → 写穿 → 广播，当轮立即生效）。切到 auto 会把
    本会话在途 pending 审批一次性放行并广播 approval_resolved。
    """
    _require_ch()
    current = await conversation_store.get_conversation_owned(caller, conv_id)
    if current is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    fields = {k: v for k, v in request.model_dump(exclude_none=True).items()}
    from agent import conversation_state
    # 运行时状态（mode/reasoning_effort/model）经会话注册表统一写（内存 → 写穿 →
    # 广播，切 auto 同轮立即生效）；title/system_prompt 等内容字段仍走 store。
    runtime_patch: dict[str, Any] = {}
    if "reasoning_effort" in fields:
        runtime_patch["reasoning_effort"] = _normalize_reasoning_effort(fields.pop("reasoning_effort"))
    if "mode" in fields:
        mode = str(fields.pop("mode") or "").strip().lower()
        if mode:
            runtime_patch["mode"] = mode
    if "model" in fields:
        runtime_patch["model"] = fields.pop("model")
    if not fields and not runtime_patch:
        raise HTTPException(status_code=400, detail="no fields to update")
    conv: dict | None = None
    if fields:
        conv = await conversation_store.update_conversation(conv_id, **fields)
        if not conv:
            raise HTTPException(status_code=404, detail="conversation not found")
    if runtime_patch:
        st = await conversation_state.update(conv_id, **runtime_patch)
        # 回填最新运行时值进返回体：PATCH 后前端即读即显，不能拿 store 的旧行。
        if conv is None:
            conv = await conversation_store.get_conversation(conv_id)
        if conv is not None and st is not None:
            conv = dict(conv)
            conv["model"] = st.model
            cs = conv.get("chat_settings")
            cs = dict(cs) if isinstance(cs, dict) else {}
            cs["mode"] = st.mode
            if st.reasoning_effort is None:
                cs.pop("reasoning_effort", None)
            else:
                cs["reasoning_effort"] = st.reasoning_effort
            conv["chat_settings"] = cs
    # 切到 auto：把本会话在途 pending 审批一次性放行（立即生效，不等下一轮）。
    # 逐条广播 approval_resolved，让已挂卡的各端把旧卡翻成「已允许」。
    if runtime_patch.get("mode") == "auto":
        from user_platform.node_client.approvals import (
            approval_registry, publish_approval_resolved,
        )
        for action in approval_registry.allow_pending(conv_id):
            await publish_approval_resolved(
                conv_id, action.confirmation_id, "allow", tool_name=action.tool_name,
            )
    return conv


@router.post("/conversations/{conv_id}/compact")
async def compact_conversation(
    conv_id: str, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """手动压缩上下文：LLM 摘要历史 → 落一条 <history_summary> 标记消息。

    供 web Agent/聊天页 token 卡的「压缩」按钮与 /compact 调用。压缩产物落库，
    后续轮次 rebuild 历史时以最后一条摘要为 cutoff（见 context_manager），
    让压缩跨轮生效。鉴权走会话归属（get_conversation_owned）。
    """
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    from agent.compact_service import compact_conversation as _compact

    return await _compact(conv_id, agent_id=conv.get("agent_id") or 0)


@router.post("/conversations/{conv_id}/attachments")
async def upload_agent_conversation_attachment(
    conv_id: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    caller: str | None = Depends(get_agent_caller),
) -> dict:
    """把一个文件落到该 Agent 的工作区，供运行时用 file 工具读取。

    浏览器不选路径：服务端剥掉目录、加 uuid 前缀，统一收进
    ``<workspace_root>/.chat-attachments/<conv_id>/``。返回的相对路径可直接
    追加到消息正文，Agent 用 read_file 之类的工具读。
    """
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")

    raw = await _read_attachment_body(request)
    agent_id = conv.get("agent_id")
    if agent_id:
        # GA-style default is the per-Agent directory. Respect an explicit
        # non-legacy DB workspace for future node binding; otherwise compute
        # data/agents/<id>/ so uploads land where file_read can see them.
        from agent.file_memory import agent_root
        from db import PostgresClient

        workspace_root = str(agent_root(int(agent_id)))
        agent_row = await PostgresClient.get_agent(agent_id)
        explicit = str((agent_row or {}).get("workspace_root") or "").strip()
        if explicit and explicit != "agent/workspace":
            workspace_root = explicit
    else:
        # Non-Agent conversations retain the legacy shared fallback.
        workspace_root = "agent/workspace"

    relative, target = _attachment_target(workspace_root, conv_id, filename)
    target.write_bytes(raw)
    return {"path": relative, "filename": target.name, "size": len(raw)}


@router.post("/chat/conversations/{conv_id}/attachments")
async def upload_chat_conversation_attachment(
    conv_id: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    caller: str | None = Depends(get_agent_caller),
) -> dict:
    """把一个文件落到非 Agent 会话的兜底工作区，并按类型给出可直接发给 LLM 的形态。

    聊天没有 Agent 工作区也没有文件工具，所以内容必须随消息一起进模型：
    图片回 ``data_url``（OpenAI ``image_url`` content part），文本类回 ``text``，
    其余二进制只回落盘路径（模型读不到内容，前端只在正文标注文件名）。
    """
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")

    raw = await _read_attachment_body(request)
    relative, target = _attachment_target("agent/workspace", conv_id, filename)
    target.write_bytes(raw)

    mime = (
        (request.headers.get("content-type") or "").split(";")[0].strip()
        or mimetypes.guess_type(target.name)[0]
        or "application/octet-stream"
    )
    result: dict[str, Any] = {"path": relative, "filename": target.name, "size": len(raw), "mime": mime}
    if mime.startswith("image/"):
        result["data_url"] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    elif mime.startswith("text/") or mime in _TEXTUAL_MIMES:
        try:
            result["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            # 声称是文本但解不出 UTF-8：当二进制处理，不猜编码。
            pass
    return result


def _apply_subagent_event(subagents: list[dict[str, Any]], etype: str, event: dict) -> None:
    """子 agent 事件聚合——已统一到 ``agent.turn_writer``，本模块不再使用。

    保留为 shim 以兼容可能的外部引用（测试 monkeypatch 等）；直接转发。
    """
    from agent.turn_writer import _apply_subagent_event as _impl
    _impl(subagents, etype, event)


async def _start_conversation_turn(
    *,
    conv: dict,
    conv_id: str,
    assistant_msg_id: int,
    user_input: str,
    caller: str | None,
    mode: str | None,
    goal_config: Any,
    ralph_config: Any = None,
    terminal_context: Any,
    max_turns: int | None,
    turn_reasoning_effort: str | None,
    resume_from: dict | None = None,
) -> dict[str, Any]:
    """异步启动一轮 agent 运行，事件走 conversation_hub 广播，立即返回占位结果。

    send_message 与 retry_message 共用本函数：前者新建 user+assistant 占位后调用，
    后者复用既有失败 assistant 消息（reset 为 streaming）后调用。事件消费改由
    GET /conversations/{id}/events（或外部中继）接管，三端同一条序列。

    ``resume_from``（重试续跑）：传入被重试 assistant 消息的**未清空**行。续跑把
    它已完成的工具调用与结果原样保留——既作为模型上下文（模型看到自己做到哪了，
    不会把一堆工具调用从头重跑），也作为 ``ConversationTurnWriter`` 的种子（收尾
    落库时旧进度 + 新进度一起写回）。非续跑路径行为不变。
    """
    from db import PostgresClient

    # 一个 turn 的事件流 → 投影 + 事件日志的唯一实现。原先 api 与 cdp 各抄一份
    # collected_* 累积逻辑并已漂移（CDP 漏 subagent 聚合、checkpoint 漏 subagents），
    # 现统一到 ConversationTurnWriter；本地 collected_* 一律取消，引用改 writer.*。
    writer = ConversationTurnWriter(
        conversation_id=conv_id,
        assistant_msg_id=assistant_msg_id,
        resume_from=resume_from,
        attachment_owner=caller,
        turn_number=0,
    )
    # 本回合的协作式停止信号：注入 tools.context 后由 agent_loop 每轮检查、
    # code_run 等子进程时赛跑。abort 端点置位它 + task.cancel() 双保险。
    stop_event = asyncio.Event()
    _CONV_STOPS[conv_id] = stop_event
    # （resume_from 的种子已并入 ConversationTurnWriter 构造函数。）

    async def on_event(event: dict):
        """一轮事件 → 累积/记帧/落库统一交给 writer，这里只做广播与运行状态翻译。

        ``writer.handle`` 返回已富化（message_id + offset）的帧，发起方与旁观方
        消费同一条序列；内容累积、事件日志记帧、投影节流落库都在 writer 内。
        """
        out = await writer.handle(event)
        await conversation_hub.publish(conv_id, out)
        # 会话运行状态：翻译成状态枚举并更新 ConversationState（改内存 → 写穿
        # run_status/run_status_info → 推 conversation_state 事件），与 cdp 同口径。
        try:
            from agent import run_state as _run_state
            await _run_state.apply(conv_id, out)
        except Exception as exc:  # noqa: BLE001 — 状态显示失败绝不影响对话
            logger.debug("[run_state] failed conv=%s: %s", conv_id, exc)

    async def run_agent():
        agent = None
        try:
            from agent.agent_main import GenericAgent
            from agent.agent_loop import agent_runner_loop, BaseHandler as _BH

            # Agent 配置一律由 GenericAgent 按 agent_id 从 DB 加载（含网关秘钥绑定
            # main_api_key_id/main_model 与 owner），不再手工预建 config —— 预建 config
            # 会让 _ensure_config 提前返回、跳过 DB 加载，导致网关列/owner 缺失。
            agent_id = conv.get("agent_id")
            agent_system_prompt = ""
            agent_row = None
            if agent_id:
                agent_row = await PostgresClient.get_agent(agent_id)
                if agent_row:
                    agent_system_prompt = agent_row.get("system_prompt") or ""
            # system_prompt：对话级覆盖优先，否则用 Agent 当前配置
            effective_system_prompt = conv.get("system_prompt") or agent_system_prompt or ""

            # 构建历史消息上下文。
            # rebuild_history_messages 把落库的展示态 tool_calls 归一成 OpenAI 线
            # 形状并补出配对的 role=tool，与在途轮次同形；旧的非标准 tool_results
            # 字段在那里被消费掉，不再上行。
            history = await conversation_store.get_messages(conv_id)
            if resume_from is not None:
                # 续跑：不跳过被重试的那条 assistant——它已完成的 tool_calls/结果
                # 要作为上下文回带给模型（否则模型看不到自己做到哪了，从头重跑）。
                # 末尾补一条 user「继续」指令——agent_runner_loop 在给了
                # initial_messages 时不会再自动追加 user_input，这条必须显式加。
                initial_messages = rebuild_history_messages(
                    history,
                    skip_msg_id=None,
                    system_prompt=effective_system_prompt,
                )
                initial_messages.append({"role": "user", "content": user_input})
            else:
                initial_messages = rebuild_history_messages(
                    history,
                    skip_msg_id=assistant_msg_id,
                    system_prompt=effective_system_prompt,
                )

            # 终端上下文作为本轮 system 上下文注入（不污染持久 system_prompt）：
            # 让 AI 感知管理员当前所在目录与最近终端输出，回答更贴合现场。仅本轮生效，
            # 不写入消息表，刷新或下一轮不带 terminal_context 时自然消失。
            tc = terminal_context
            if tc and (tc.cwd or tc.recent_lines):
                ctx_lines = ["[当前终端上下文]"]
                if tc.cwd:
                    ctx_lines.append(f"当前目录：{tc.cwd}")
                if tc.recent_lines:
                    shown = tc.recent_lines[-20:]
                    ctx_lines.append("最近终端输出：")
                    ctx_lines.extend(shown)
                initial_messages.append({"role": "system", "content": "\n".join(ctx_lines)})

            # run_as_caller=caller：网关 key 权限按当前发起用户校验（无权 → 发送失败），
            # 而非按 Agent owner。平台 Agent 对所有用户可见但各自用自己的身份跑。
            # scene= 从 chat_settings 还原（建会话时 persist 写入），让场景绑定的 MCP
            # 服务把完整方法/参数说明内联进本轮 system_prompt，首轮不必先 file_read SOP。
            scene = scene_context.from_chat_settings(conv.get("chat_settings"))
            agent = GenericAgent(
                agent_id=agent_id,
                tools=_build_tool_registry(agent_id, owner_user_id=caller),
                run_as_caller=caller,
                scene=scene,
                conversation_id=conv_id,
                # 配置注入（Phase 2.1）：agent_row 已读过，传进去跳过 _ensure_config
                # 的重复 SELECT。行缺列（旧库未迁移）时 _apply_agent_row 按 dict.get
                # 兜底，与 DB 懒加载路径同一实现。
                agent_row=agent_row,
            )
            # 配置先解析：审批超时是 per-Agent 配置，下面建 node shell / code_run 两个
            # 审批批次时都要用。_ensure_config 幂等（已解析则直接返回缓存）。
            config = await agent._ensure_config()
            # 轮数上限的唯一真相源：请求级 max_turns（本次覆盖）> Agent 行 max_turns
            #（config 已解析：行没配 → DEFAULT_MAX_TURNS）> 固定默认。handler 与 loop
            # 都用这个变量，杜绝历史上「两处 or 链表达式不同、值可能不一致」的 bug。
            eff_max_turns = max_turns or config.max_turns
            # 技能激活（/名字 标记）：只看**当前这条消息**（不扫历史），命中后把技能
            # 的基础信息（名字、说明、正文位置）追加到这条用户消息本身——用了才
            # 生效、下一条不提就自然结束；不进 system prompt、不逐轮注入。
            # initial_messages 里最后一条 user 就是本轮输入（send_message 已先落库，
            # rebuild 时跳过了 assistant 占位）。失败只降级为无技能，绝不阻断发送。
            try:
                from agent.skill_slash import (
                    append_to_last_user,
                    build_skill_message_suffix,
                    collect_activated_skills,
                )

                activated = await collect_activated_skills(
                    agent_id=agent_id,
                    mcp_user_id=getattr(config, "mcp_user_id", None),
                    current_text=user_input,
                )
                append_to_last_user(initial_messages, build_skill_message_suffix(activated))
            except Exception as exc:  # noqa: BLE001 — 技能推导失败不影响本轮发送
                logger.warning("[skill] activation failed agent=%s: %s", agent_id, exc)
            approval_timeout = int(getattr(config, "approval_timeout_seconds", 0) or 0)
            # auto 执行模式：会话运行时状态的派生属性。审批批次传**取值回调**而非快照
            # bool —— 回合中途用户切 auto，同回合后续审批立即走 auto（历史 bug 是这里
            # 收 bool，切档只影响下一回合）。ConversationState 在 send/retry 前 load。
            from agent import conversation_state
            _cstate = await conversation_state.load(conv_id, conv)
            if (mode or "").strip().lower() and (mode or "").strip().lower() != _cstate.mode:
                await conversation_state.update(conv_id, mode=mode)
                _cstate = conversation_state.peek(conv_id) or _cstate
            auto_approve = _cstate.auto_approve
            is_auto = lambda: bool((conversation_state.peek(conv_id) or _cstate).auto_approve)
            node_id = ((conv.get("chat_settings") or {}).get("node_id") or "").strip()
            node_shell_batch = None
            if node_id:
                from user_platform.node_client.tools import register_node_shell_exec

                # 终端绑定：命令注入浏览器当前连着的那个 PTY，工作目录用页面正在看的
                # 目录（模型不自己选 cwd）。没有 terminal_id 时工具直接报错，不退回
                # 独立 host_exec —— 否则用户看不到命令与输出，违背「像用户输入一样」。
                tctx = terminal_context
                shell_flavor = await _node_shell_flavor(node_id)
                # Persistent authorization is owned by this execution node and is
                # shared by every caller. A lookup failure is fail-closed: use no
                # allow-list and keep the normal one-time confirmation flow.
                auto_allow_keys: set[str] | None = None
                try:
                    from user_platform.shell_approval_service import list_node_auto_allow_keys

                    auto_allow_keys = await list_node_auto_allow_keys(node_id, shell_flavor)
                except Exception:  # noqa: BLE001 — policy must never break sending
                    auto_allow_keys = None
                node_shell_batch = register_node_shell_exec(
                    agent._tools,
                    node_id,
                    caller,
                    conv_id,
                    emit=on_event,
                    terminal_id=(tctx.terminal_id if tctx else "") or "",
                    cwd=(tctx.cwd if tctx else "") or "",
                    shell_flavor=shell_flavor,
                    auto_allow_keys=auto_allow_keys,
                    timeout_seconds=approval_timeout,
                    auto_approve=is_auto,
                )
            # 注入事件出口：子 agent spawn 时把 subagent_* 事件透传回主对话 SSE 流。
            agent.set_event_sink(on_event)
            llm = await agent._ensure_llm()
            # 对话级模型覆盖（conv.model 与 Agent 主模型不同）：复用主 Agent 的网关 key，
            # 仅换模型名 —— fork 保留同一 api_key，计费/归属仍归主 key。
            conv_model = conv.get("model")
            if conv_model and conv_model != getattr(llm, "model", None):
                llm = llm.fork(model=conv_model)
            if turn_reasoning_effort is not None:
                llm = llm.fork(
                    thinking_enabled=bool(turn_reasoning_effort),
                    reasoning_effort=turn_reasoning_effort,
                )
            # 本轮实际生效的模型，随 assistant 消息落库供历史回放标注
            effective_model = getattr(llm, "model", "") or ""
            tools, resource_block = await agent._ensure_resources()
            # 协作式停止信号注入：必须在这里（_ensure_resources 之后）——它会经
            # _ensure_tools 新建 ToolRegistry/ToolContext，早设会被那次重建抹掉。
            # 之后到 loop 结束再无 context 重建点。
            tools.context.stop_event = stop_event
            tools.context.conversation_id = conv_id
            from agent.code_run_approval import CodeRunApprovalBatch
            from agent.file_write_approval import FileWriteApprovalBatch
            from agent.workflow_approval import WorkflowApprovalBatch

            code_run_batch = CodeRunApprovalBatch(
                conversation_id=conv_id,
                agent_id=int(agent_id or 0),
                caller=caller,
                emit=on_event,
                timeout_seconds=approval_timeout,
                auto_approve=is_auto,
            )
            tools.set_code_run_approval(code_run_batch)
            # 越界写入审批：file_write/file_patch 撞 allowed_roots / denied_patterns
            # 时弹一张卡（工作区内写入不经过它，行为不变）。与 code_run 同一批预扫，
            # 合并进 _prepare_tool_batch；无人值守入口不挂它 → fail closed。
            file_write_batch = FileWriteApprovalBatch(
                conversation_id=conv_id,
                agent_id=int(agent_id or 0),
                caller=caller,
                emit=on_event,
                security=tools.context.security,
                timeout_seconds=approval_timeout,
                auto_approve=is_auto,
            )
            tools.set_file_write_approval(file_write_batch)
            # 工作流定义写审批：create/update/delete 弹一张卡（图能执行 code_run/
            # 写文件/驱动浏览器）。读与运行态（list/get/run/runs/run_status/cancel）
            # 不进卡。无人值守入口不挂它 → fail closed。
            workflow_batch = WorkflowApprovalBatch(
                conversation_id=conv_id,
                agent_id=int(agent_id or 0),
                caller=caller,
                emit=on_event,
                timeout_seconds=approval_timeout,
                auto_approve=is_auto,
            )
            tools.set_workflow_approval(workflow_batch)
            if resource_block:
                effective_system_prompt = f"{effective_system_prompt}\n\n{resource_block}".strip()
            # The history was assembled before resource initialization. Refresh or
            # insert its system message so the first LLM call sees the Skill index.
            if effective_system_prompt:
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})
            # show_file merged into ask_user; no longer a standalone tool.
            handler = _BH(tools_registry=tools, max_turns=eff_max_turns)

            # GA chapter6 execution modes.
            mode_eff = (mode or "interact").lower()
            goal_state_file = ""
            if mode_eff == "plan":
                from agent.plan_mode import PlanModeManager
                plan_mgr = PlanModeManager(int(agent_id or 0))
                # Key the plan by conversation id so it persists across turns
                # instead of fragmenting per user message.
                plan_session = plan_mgr.create(
                    task_name=f"conv-{conv_id}",
                    objective=user_input.strip() or "complex task",
                )
                plan_hint = (
                    "\n\n[Plan Mode] Read memory/sop/plan_sop.md and follow the "
                    "five-phase flow (explore → plan → user-confirm via ask_user → "
                    f"execute → verify with a subagent). The plan file is "
                    f"{plan_session.plan_path}; "
                    "file_read it to resume, fill in steps, and mark them "
                    "[ ]/[D]/[P]/[✓]/[✗]/[FIX] as you progress."
                )
                effective_system_prompt = (effective_system_prompt or "") + plan_hint
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})
            elif mode_eff == "goal":
                goal = goal_config
                if goal is None:
                    raise HTTPException(400, "goal mode requires goal_config")
                from agent.guardian import Guardian
                guardian = Guardian(agent_id=int(agent_id or 0))
                await guardian.start_goal_mode(
                    objective=goal.objective,
                    budget_seconds=goal.budget_seconds,
                    # goal 模式的阶段上限与内层轮数上限：请求级 goal.max_turns 优先，
                    # 否则沿用本轮统一值（请求级 > Agent 行 > 固定默认）。
                    max_turns=goal.max_turns or eff_max_turns,
                )
                goal_hint = (
                    f"\n\n[Goal Mode] Objective: {goal.objective}. Budget: "
                    f"{goal.budget_seconds}s. Read memory/sop/goal_mode_sop.md. "
                    f"Alternate creation/inspection/improvement phases; execute "
                    f"meaningful work each turn, do not merely report progress; "
                    f"if blocked ask the user via ask_user."
                )
                effective_system_prompt = (effective_system_prompt or "") + goal_hint
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})
                goal_state_file = "temp/goal_state.json"

            elif mode_eff == "ralph":
                ralph = ralph_config
                if ralph is None:
                    raise HTTPException(400, "ralph mode requires ralph_config")
                from agent.guardian import Guardian as _Guardian
                guardian = _Guardian(agent_id=int(agent_id or 0))
                await guardian.start_ralph_mode(
                    objective=ralph.objective,
                    max_rounds=ralph.max_rounds,
                )
                ralph_hint = (
                    f"\n\n[Ralph Mode] Objective: {ralph.objective}. Round cap: "
                    f"{ralph.max_rounds}. You are driven round-by-round as a fresh "
                    f"worker; each round carries only the objective and the previous "
                    f"round's structured handoff. End every round with the strict JSON "
                    f"report described in your round prompt (continue/complete/blocked). "
                    f"The workspace is the source of truth — confirm any handoff "
                    f"against it before acting."
                )
                effective_system_prompt = (effective_system_prompt or "") + ralph_hint
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})
                goal_state_file = "temp/ralph_state.json"

            elif mode_eff == "supervisor":
                # 监督模式：只注入红线（_SUPERVISOR_HINT 常量与 cdp 链路共用，防漂移），
                # 不加 Guardian、不加驱动循环——spawn_subagent 是前台一次性，模型按
                # 红线自己拆解/派子/验收即可；循环等暴露了需求再做（Phase 2）。
                effective_system_prompt = (effective_system_prompt or "") + _SUPERVISOR_HINT
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})

            async def _prepare_tool_batch(tool_calls: list[dict]) -> None:
                await code_run_batch.prepare(tool_calls)
                await file_write_batch.prepare(tool_calls)
                await workflow_batch.prepare(tool_calls)
                if node_shell_batch is not None:
                    await node_shell_batch.prepare(tool_calls)

            final_content = ""
            async for item in agent_runner_loop(
                llm,
                system_prompt=effective_system_prompt,
                user_input=user_input,
                handler=handler,
                tools_schema=tools.get_schema(),
                max_turns=eff_max_turns,
                on_event=on_event,
                initial_messages=initial_messages if initial_messages else None,
                on_tool_batch=_prepare_tool_batch,
                auto_compact=config.auto_compact,
            ):
                final_content = item.get("data", final_content)

            # Goal mode: drive continuation prompts from Guardian until the budget
            # is exhausted (next_goal_prompt returns the wrap-up, then None).
            if mode_eff == "goal":
                # Drive Guardian continuations until it emits one wrap-up prompt.
                # Each continuation is a fresh bounded Agent loop; Guardian owns
                # the overall goal budget/phase count.
                wrapped_up = False
                while True:
                    cont = await guardian.next_goal_prompt()
                    if cont is None:
                        break
                    wrapped_up = "[GOAL MODE WRAP-UP]" in cont
                    async for item in agent_runner_loop(
                        llm,
                        system_prompt=effective_system_prompt,
                        user_input=cont,
                        handler=handler,
                        tools_schema=tools.get_schema(),
                        max_turns=eff_max_turns,
                        on_event=on_event,
                        initial_messages=None,
                        on_tool_batch=_prepare_tool_batch,
                        auto_compact=config.auto_compact,
                    ):
                        final_content = item.get("data", final_content)
                    if wrapped_up:
                        break
                # Independent verification before marking done: a fresh agent (no
                # inherited context) re-checks the wrap-up's claims and attempts to
                # refute them. Refuted → the verifier's problems are appended to the
                # final message so the user sees the goal was not independently
                # confirmed; failure to verify → not verified (never silently done).
                try:
                    from agent.ralph import verify_goal_completion
                    ver = await verify_goal_completion(
                        agent, goal.objective, [str(final_content or "")],
                    )
                    if ver.get("verified"):
                        final_content = (final_content or "") + (
                            "\n\n[Goal verification] Verified by an independent agent."
                        )
                    else:
                        problems = (ver.get("report") or {}).get("problems") or []
                        if not problems and ver.get("error"):
                            problems = [ver["error"]]
                        detail = " ".join(problems[:5]) or "the independent verifier could not confirm the claims."
                        final_content = (final_content or "") + (
                            "\n\n[Goal verification] NOT verified by an independent agent: " + detail
                        )
                except Exception:  # noqa: BLE001 - verification must never block goal completion
                    pass
                try:
                    await guardian.mark_goal_done(budget_exhausted=wrapped_up)
                    # Mirror the final state after status transition.
                    from agent.file_memory import agent_root
                    import json as _json
                    state = await guardian.get_goal_status()
                    if state:
                        gpath = agent_root(int(agent_id)) / "temp" / "goal_state.json"
                        gpath.parent.mkdir(parents=True, exist_ok=True)
                        gpath.write_text(_json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass

            # Ralph mode: drive FRESH rounds from Guardian until the round cap is
            # hit (next_ralph_round returns None) or the report says complete or
            # blocked. Each round is its own bounded agent loop with a FRESH
            # handler — no inherited context, only objective + previous handoff
            # (ralph's defining trait vs goal, which reuses one handler).
            if mode_eff == "ralph":
                from agent.guardian import Guardian as _Guardian
                ralph_guard = _Guardian(agent_id=int(agent_id or 0))
                ralph_rounds = 0
                ralph_report: dict | None = None
                ralph_outcome = "max_rounds"
                while True:
                    step = await ralph_guard.next_ralph_round()
                    if step is None:
                        break
                    ralph_rounds = step["round"]
                    # Fresh handler per round — ralph's whole point: a child that
                    # inherits no prior context, only objective + structured handoff.
                    round_handler = _BH(
                        tools_registry=tools,
                        max_turns=eff_max_turns,
                    )
                    round_text = ""
                    async for item in agent_runner_loop(
                        llm,
                        system_prompt=effective_system_prompt,
                        user_input=step["prompt"],
                        handler=round_handler,
                        tools_schema=tools.get_schema(),
                        max_turns=eff_max_turns,
                        on_event=on_event,
                        initial_messages=None,
                        on_tool_batch=_prepare_tool_batch,
                        auto_compact=config.auto_compact,
                    ):
                        round_text = item.get("data", round_text)
                    # Parse + validate the strict report; a bad one ends the loop
                    # loudly rather than inventing a status.
                    try:
                        from agent.ralph import parse_round_report
                        ralph_report = parse_round_report(str(round_text or ""))
                    except ValueError as exc:
                        ralph_outcome = "round_failed"
                        ralph_report = None
                        final_content = (
                            (final_content or "")
                            + f"\n\n[Ralph] Round {step['round']} failed to produce a valid report: {exc}"
                        )
                        break
                    await ralph_guard.record_ralph_report(ralph_report)
                    if ralph_report["status"] in ("complete", "blocked"):
                        ralph_outcome = ralph_report["status"]
                        break
                # Terminal outcome summary appended to the final message.
                try:
                    from agent.ralph import format_round_outcome
                    final_content = (
                        (final_content or "") + "\n\n"
                        + format_round_outcome(ralph_outcome, ralph_rounds, ralph_report)
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await ralph_guard.mark_ralph_done(outcome=ralph_outcome)
                    from agent.file_memory import agent_root
                    import json as _json
                    state = await ralph_guard.get_ralph_state()
                    if state:
                        gpath = agent_root(int(agent_id)) / "temp" / "ralph_state.json"
                        gpath.parent.mkdir(parents=True, exist_ok=True)
                        gpath.write_text(_json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass

            # 更新 assistant 消息为完成状态，并持久化工具调用供历史回放。
            # content 用累积全文（writer.content）而非只取末轮 final_content：
            # 多轮工具调用中模型的中间叙述也是用户要看的历史，末轮只有最终回答。
            await conversation_store.update_message(
                assistant_msg_id,
                content=writer.content or final_content,
                tool_calls=writer.tool_calls or None,
                tool_results=writer.tool_results or None,
                media=writer.media or None,
                status="done",
                model=effective_model,
                usage=writer.usage or None,
                reasoning=writer.reasoning,
                subagents=writer.subagents or None,
            )
            await conversation_hub.publish(conv_id, {"type": "end"})
        except asyncio.CancelledError:
            await conversation_store.update_message(
                assistant_msg_id,
                media=writer.media or None,
                status="error",
                error="已中止",
                reasoning=writer.reasoning,
                subagents=writer.subagents or None,
            )
            # 事件走 on_event 同一条链路（publish + run_state.apply）：aborted 映射
            # STATUS_ABORTED，随后的 end 收敛到 WAITING_INPUT——直接 publish 裸事件
            # 会绕过 run_state，状态徽章停在「处理中」。CancelledError 重入时收尾
            # 是尽力而为，不阻断 abort 流程。
            try:
                await on_event({"type": "aborted", "message": "已中止"})
                await on_event({"type": "end"})
            except Exception:  # noqa: BLE001 — 收尾失败不能盖掉中止本身
                pass
        except ApprovalDenied:
            # 用户主动拒绝不是故障：正常结束本轮，不发 error 事件，也绝不把 denied
            # 工具结果喂回模型继续跑。审批卡已由 resolveApproval 翻成「已拒绝」。
            # on_tool_batch 在 tool_call 事件之前抛出，所以这里没有悬空的 running 工具卡；
            # 已流出的前置正文仍完整落库。
            await conversation_store.update_message(
                assistant_msg_id,
                content=writer.content,
                tool_calls=writer.tool_calls or None,
                tool_results=writer.tool_results or None,
                media=writer.media or None,
                status="done",
                model=effective_model,
                usage=writer.usage or None,
                reasoning=writer.reasoning,
                subagents=writer.subagents or None,
            )
            await conversation_hub.publish(conv_id, {"type": "end"})
        except ApprovalTimeout as timeout_exc:
            # 审批超时：本轮就此结束，不把「被拒绝」喂回模型跑下一轮。已流出的正文/
            # 工具调用由 writer 的检查点落过库，刷新后能看到停在哪一步。
            error_text = str(timeout_exc)
            await conversation_store.update_message(
                assistant_msg_id,
                content=writer.content,
                tool_calls=writer.tool_calls or None,
                tool_results=writer.tool_results or None,
                media=writer.media or None,
                status="error",
                error=error_text,
                reasoning=writer.reasoning,
                subagents=writer.subagents or None,
            )
            await conversation_hub.publish(conv_id, {"type": "error", "message": error_text})
            await conversation_hub.publish(conv_id, {"type": "end"})
        except Exception as e:
            error_text = "".join(traceback.format_exception_only(type(e), e)).strip()
            await conversation_store.update_message(
                assistant_msg_id,
                media=writer.media or None,
                status="error",
                error=error_text,
                reasoning=writer.reasoning,
                subagents=writer.subagents or None,
            )
            await conversation_hub.publish(conv_id, {"type": "error", "message": error_text})
            await conversation_hub.publish(conv_id, {"type": "end"})
        finally:
            try:
                manager = getattr(agent, "_mcp_manager", None)
                if manager is not None:
                    await manager.close()
            except Exception:
                pass

    task = asyncio.create_task(run_agent())
    _CONV_TASKS[conv_id] = task
    # busy 标记：回合进行中禁止空闲 sweep 回收实例（turn 内审批读活状态依赖它）。
    conversation_state.set_busy(conv_id, True)
    # 旧实现里 _CONV_TASKS 由 SSE event_generator 的 finally 摘除；现在事件改走
    # conversation_hub 扇出、POST 不再持有连接，摘除必须跟着任务生命周期走。
    task.add_done_callback(
        lambda t, cid=conv_id: _on_turn_done(cid, t)
    )
    return {"assistant_message_id": assistant_msg_id, "status": "streaming"}


def _on_turn_done(conv_id: str, t: asyncio.Task) -> None:
    """回合任务结束：摘除 _CONV_TASKS（本任务仍是当前任务时才摘）+ 清 busy 标记。"""
    if _CONV_TASKS.get(conv_id) is t:
        _CONV_TASKS.pop(conv_id, None)
    # 停止信号与任务同生命周期回收，避免 _CONV_STOPS 随会话数增长泄漏。
    _CONV_STOPS.pop(conv_id, None)
    conversation_state.set_busy(conv_id, False)


def _conversation_events_response(conv_id: str) -> StreamingResponse:
    """对话事件订阅流：首帧权威快照，其后实时增量，终态自动收口。

    - 首帧 ``snapshot``：``get_messages`` 的全量权威状态（快照即权威，客户端渲染
      以它为基底）。
    - 之后逐帧转发 conversation_hub 广播的实时事件（content/reasoning 带 offset，
      工具卡按 id 幂等）。
    - 终态帧（done/error）之后补一帧 ``end`` 并收口——客户端据此重订阅拿下一轮。
      连接的存续、重连、历史补拉全部是客户端的职责，服务端不记任何订阅状态。

    快照在**订阅之后**读取：快照已含的增量靠 offset 去重（前缀性质），快照没含的
    在环形缓冲里一定还有……不，本模块刻意不缓存事件——缺的段由客户端整页重拉。
    """

    async def event_stream():
        queue = conversation_hub.subscribe(conv_id)
        try:
            messages = await conversation_store.get_messages(conv_id, timeline=True)
            # 首帧快照：连接此刻的权威状态。订阅在读取之前完成，两者之间到达的增量
            # 靠 offset 与快照前缀对齐，不会出现「订阅到了但快照里没有」的缺口。
            yield f"data: {json.dumps({'type': 'snapshot', 'messages': messages}, ensure_ascii=False)}\n\n"
            while True:
                event = await queue.get()
                if event.get("type") == "end":
                    # 一轮收口：关连接。发起方的读循环就此退出（loading 落定）；
                    # 旁观方重订阅拿下一轮（快照先给全量，无状态可丢）。
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            conversation_hub.unsubscribe(conv_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_stream_headers())


@router.get("/conversations/{conv_id}/events")
async def stream_conversation_events(
    conv_id: str,
    caller: str | None = Depends(get_agent_caller),
) -> StreamingResponse:
    """订阅一条对话的实时事件流（多订阅者：发起方与旁观方同一条序列）。"""
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_events_response(conv_id)


@router.post("/conversations/{conv_id}/messages")
async def send_message(conv_id: str, request: SendMessageRequest, caller: str | None = Depends(get_agent_caller)):
    """发送一条用户消息并启动一轮 agent 运行（立即返回，事件走 SSE 订阅）。

    落 user 消息 + streaming assistant 占位后异步执行；已有运行中任务 → 409；
    对账残留 streaming 占位。mode/reasoning_effort 是会话状态，发送时落进会话
    注册表，重试/后续轮沿用。首条消息自动用输入截断做标题。
    """
    _require_ch()
    from db import PostgresClient

    # 检查对话存在 + 归属
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    node_id = ((conv.get("chat_settings") or {}).get("node_id") or "").strip()
    # 运行时状态（mode/reasoning_effort）统一经会话注册表写：改内存 + 写穿 DB + 广播，
    # 单一写入口（不再直改 chat_settings——绕过内存态正是「切 auto 要等下一轮才生效」
    # 的根因）。本轮显式给了 reasoning_effort 就落库，否则沿用会话已存值。
    from agent import conversation_state
    _cstate = await conversation_state.load(conv_id, conv)
    if request.reasoning_effort is not None:
        turn_reasoning_effort = _normalize_reasoning_effort(request.reasoning_effort)
        await conversation_state.update(conv_id, reasoning_effort=turn_reasoning_effort)
    else:
        turn_reasoning_effort = (
            _normalize_reasoning_effort(_cstate.reasoning_effort)
            if _cstate.reasoning_effort is not None
            else None
        )
    # mode 是会话状态：发送时把本轮 mode 落进注册表，重试/后续轮沿用。否则前端即使
    # PATCH 了、这里又被 request.mode 默认值 interact 覆盖回去。
    if request.mode and request.mode != _cstate.mode:
        await conversation_state.update(conv_id, mode=request.mode)
    if node_id and not await _caller_authorized_for_node(caller, node_id):
        raise HTTPException(status_code=403, detail="无权操作该节点")

    # 检查是否已有运行中的任务
    existing = _CONV_TASKS.get(conv_id)
    if existing and not existing.done():
        raise HTTPException(status_code=409, detail="a task is already running for this conversation")
    # 本进程没有该会话的活任务，却可能残留一条 streaming 的 assistant 占位消息
    # （进程重启或连接中断造成）。对账成 error，避免刷新后永远「思考中」并解除
    # 下一轮发送被残留状态卡住的情况。
    await _reconcile_orphaned_streaming(conv_id)

    # 保存用户消息
    user_msg = await conversation_store.add_message(conv_id, "user", request.content)
    # 创建 assistant 占位消息
    assistant_msg = await conversation_store.add_message(conv_id, "assistant", "", status="streaming")
    assistant_msg_id = assistant_msg["id"]

    # 刷新对话时间
    await conversation_store.touch_conversation(conv_id)

    # 首次对话自动用用户输入截断做标题
    if conv.get("title") == "新对话":
        title = request.content[:30] + ("..." if len(request.content) > 30 else "")
        await conversation_store.update_conversation(conv_id, title=title)

    return await _start_conversation_turn(
        conv=conv,
        conv_id=conv_id,
        assistant_msg_id=assistant_msg_id,
        user_input=request.content,
        caller=caller,
        mode=request.mode,
        goal_config=request.goal_config,
        ralph_config=request.ralph_config,
        terminal_context=request.terminal_context,
        max_turns=request.max_turns,
        turn_reasoning_effort=turn_reasoning_effort,
    )


@router.post("/conversations/{conv_id}/messages/{message_id}/retry")
async def retry_message(
    conv_id: str,
    message_id: int,
    caller: str | None = Depends(get_agent_caller),
):
    """重试一条失败的 assistant 消息：复位同 id 消息为 streaming，用其前一条 user
    消息内容重跑一轮 agent。只允许重试最后一条 assistant 消息（避免重放历史中间
    轮破坏上下文）；mode 固定 interact，不带 terminal_context/goal（goal 模式的
    失败轮不支持重试，请重新发起）。
    """
    _require_ch()

    # 校验对话存在 + 归属
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    node_id = ((conv.get("chat_settings") or {}).get("node_id") or "").strip()
    if node_id and not await _caller_authorized_for_node(caller, node_id):
        raise HTTPException(status_code=403, detail="无权操作该节点")

    # 检查是否已有运行中的任务
    existing = _CONV_TASKS.get(conv_id)
    if existing and not existing.done():
        raise HTTPException(status_code=409, detail="a task is already running for this conversation")
    await _reconcile_orphaned_streaming(conv_id)

    # 定位目标消息：必须是 assistant、status in {error,streaming}、且是最后一条 assistant
    history = await conversation_store.get_messages(conv_id)
    target = None
    last_assistant_id = None
    for msg in history:
        if msg.get("role") == "assistant":
            last_assistant_id = msg["id"]
            if msg["id"] == int(message_id):
                target = msg
    if target is None:
        raise HTTPException(status_code=404, detail="message not found in this conversation")
    if target.get("role") != "assistant":
        raise HTTPException(status_code=400, detail="只能重试 assistant 消息")
    if target.get("status") not in ("error", "streaming"):
        raise HTTPException(status_code=400, detail="只能重试失败或中断的消息")
    if last_assistant_id != int(message_id):
        raise HTTPException(status_code=400, detail="只能重试最后一条 assistant 消息")

    # 取前一条 user 消息，拼成续跑指令文本（旧的「上一条 user」不清空已做的工作）。
    prior_user = ""
    for msg in history:
        if msg["id"] == int(message_id):
            break
        if msg.get("role") == "user":
            prior_user = msg.get("content") or ""
    if not prior_user.strip():
        raise HTTPException(status_code=400, detail="无法重试：上一条用户消息为空")
    user_input = "请从刚才中断的地方继续完成任务（前面的工具调用与结果仍在上下文中）。"

    # 复位同一条 assistant 消息为 streaming，但**保留**已完成的正文/工具调用/结果，
    # 让续跑不从头再来。model/usage/reasoning 由本轮重跑覆盖。
    await conversation_store.update_message(
        int(message_id),
        status="streaming",
        error=None,
    )
    await conversation_store.touch_conversation(conv_id)

    # 重试沿用会话已存的 reasoning_effort / mode（mode 是会话状态，切了 auto 就该
    # 在重试时生效，而不是硬编码 interact 把用户又拽回审批）。从会话注册表读活状态。
    from agent import conversation_state
    _cstate = await conversation_state.load(conv_id, conv)
    turn_reasoning_effort = (
        _normalize_reasoning_effort(_cstate.reasoning_effort)
        if _cstate.reasoning_effort is not None
        else None
    )
    resume_mode = _cstate.mode or "interact"

    return await _start_conversation_turn(
        conv=conv,
        conv_id=conv_id,
        assistant_msg_id=int(message_id),
        user_input=user_input,
        caller=caller,
        mode=resume_mode,
        goal_config=None,
        terminal_context=None,
        max_turns=None,
        turn_reasoning_effort=turn_reasoning_effort,
        resume_from=target,
    )



# node_shell_exec 命中需确认命令时，工具在 approval_registry 上挂起 future；
# 这两个端点让管理员批准/拒绝，并支持断线重连后查 pending。


async def _caller_authorized_for_node(caller: str | None, node_id: str) -> bool:
    """管理员（caller=None）可用任意节点；用户模式必须持有该节点绑定。"""
    if not node_id:
        return False
    if caller is None:
        return True  # 管理员
    try:
        from user_platform.nodes_service import nodes_service
        binding = await nodes_service.user_can_use_node(str(caller), node_id)
        return binding is not None
    except Exception:  # noqa: BLE001 — 授权失败按拒绝处理
        return False


async def _node_shell_flavor(node_id: str) -> str:
    """Derive the terminal shell from trusted node telemetry, failing closed."""
    try:
        from user_platform.node_client.client import get_local_node_client
        from user_platform.shell_approval_service import infer_node_shell_flavor

        rows = await get_local_node_client().list_nodes()
        node = next((row for row in rows if row.get("node_id") == node_id), None)
        return infer_node_shell_flavor(node)
    except Exception:  # noqa: BLE001 — unknown shell must use the conservative policy
        return "unknown"


async def _node_available_for_host_exec(node_id: str) -> bool:
    """Re-check approval, liveness and terminal capability before approval.

    Agent commands now run inside the browser-attached PTY, so the relevant
    capability is ``terminal`` (interactive shell), not ``host_exec``.
    """
    try:
        from user_platform.node_client.client import get_local_node_client

        rows = await get_local_node_client().list_nodes()
        node = next((row for row in rows if row.get("node_id") == node_id), None)
        if not node or node.get("status") != "approved" or not node.get("online"):
            return False
        return str((node.get("capabilities") or {}).get("terminal", "")).lower() == "true"
    except Exception:  # noqa: BLE001 — control-plane failure must fail closed
        return False


class ApprovalRequest(BaseModel):
    result: str  # "allow" | "deny"
    command_hash: str = ""


@router.post("/conversations/{conv_id}/approvals/{confirmation_id}")
async def resolve_approval(
    conv_id: str,
    confirmation_id: str,
    body: ApprovalRequest,
    caller: str | None = Depends(get_agent_caller),
) -> dict:
    """裁决一条一次性审批（allow/deny）。

    会话归属 + command_hash 比对（防止批准到被改写的命令）双闸门；已裁决/过期 →
    409。节点审批还需调用者仍持有节点绑定、节点在线且支持终端。裁决后广播给本
    会话全部订阅者，让其他端挂着的同一张卡跟着翻状态。
    """
    _require_ch()
    from user_platform.node_client.approvals import approval_registry

    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")

    action = approval_registry.get(confirmation_id)
    if action is None or action.conversation_id != conv_id:
        raise HTTPException(status_code=404, detail="confirmation not found")
    if action.resolved is not None:
        raise HTTPException(status_code=409, detail=f"confirmation already {action.resolved}")
    # 过期（尚未被 reap）的审批不能被放行：晚到的一次点击不该把已过期审批批下来。
    if action.is_expired():
        raise HTTPException(status_code=409, detail="confirmation already expired")

    # Re-validate the caller is still authorized for this node (a binding may
    # have been revoked since the conversation started) and that the command the
    # admin is approving is the one that produced the prompt (hash match).
    #
    # 节点门只对真正绑了节点的审批（node_shell_exec）生效。code_run 在服务端本地
    # 执行、没有节点，建 action 时 node_id="";此时归属已由上面的
    # get_conversation_owned + action.conversation_id == conv_id 把住，跳过节点校验。
    # 否则 _caller_authorized_for_node 里 `if not node_id: return False` 会让每个
    # code_run 审批都吃 403，卡片点不动、只能干等超时。
    if action.node_id:
        if not await _caller_authorized_for_node(caller, action.node_id):
            raise HTTPException(status_code=403, detail="无权操作该节点")
        if not await _node_available_for_host_exec(action.node_id):
            raise HTTPException(status_code=409, detail="节点已离线、未批准或不支持宿主机命令")
    if not body.command_hash or body.command_hash != action.command_hash:
        raise HTTPException(status_code=409, detail="command hash mismatch")

    result = (body.result or "").strip().lower()
    if result not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail="result must be allow or deny")

    resolved = approval_registry.resolve(confirmation_id, result)
    if resolved is None:
        # Lost a race with a concurrent resolve / timeout between the get() and
        # resolve() above.
        raise HTTPException(status_code=409, detail="confirmation already resolved")
    # 广播给本会话全部订阅者，让别的端挂着的同一张卡跟着翻状态。
    from user_platform.node_client.approvals import publish_approval_resolved
    await publish_approval_resolved(
        conv_id, confirmation_id, result, tool_name=resolved.tool_name,
    )
    return {"ok": True, "confirmation_id": confirmation_id, "result": result}


@router.get("/conversations/{conv_id}/approvals")
async def list_approvals(
    conv_id: str, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """Pending confirmations for a conversation (reconnect recovery)."""
    _require_ch()
    from user_platform.node_client.approvals import approval_registry

    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    approval_registry.reap_expired()
    pending = [a.to_dict() for a in approval_registry.list_pending(conv_id)]
    return {"approvals": pending}


@router.post("/conversations/{conv_id}/abort")
async def abort_conversation(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """中止对话正在运行的一轮 agent（协作式停止 + cancel 双保险）。

    无活任务时对账残留 streaming 占位，返回 aborted=false。
    """
    _require_ch()
    if await conversation_store.get_conversation_owned(caller, conv_id) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    task = _CONV_TASKS.get(conv_id)
    if task and not task.done():
        # 协作式停止先于 cancel：置位后，正在跑的 code_run 子进程会立刻被杀树、
        # agent_loop 下一轮不再调 AI；cancel 兜底打断卡在 await 里的那次。
        ev = _CONV_STOPS.get(conv_id)
        if ev is not None:
            ev.set()
        task.cancel()
        return {"aborted": True}
    # 本进程没有活任务却可能残留一条 streaming 占位消息（进程重启/任务句柄
    # 丢失）。点暂停时对账掉它，前端那个「思考中」气泡才不会永远转圈。
    await _reconcile_orphaned_streaming(conv_id)
    return {"aborted": False, "detail": "no running task"}


# ── 聊天页（操练场/聊天）对话管理 ──────────────────────────────────────
# 复用 agent_conversations/agent_messages，以 kind='chat' 与 Agent 会话隔离。
# 仅做持久化 CRUD；流式发送由前端直连 /v1/* 端点，不跑 agent_runner_loop。

class CreateChatConversationRequest(BaseModel):
    title: str = "新对话"
    system_prompt: str = ""
    model: str = ""
    chat_settings: dict | None = None


class UpdateChatConversationRequest(BaseModel):
    title: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    chat_settings: dict | None = None


class AppendChatMessageRequest(BaseModel):
    role: str = Field(..., min_length=1)
    content: str = ""
    status: str = "done"
    error: str | None = None
    media: list | None = None
    model: str = ""
    usage: dict | None = None


async def _get_chat_conversation_or_404(conv_id: str, caller: str | None) -> dict:
    _require_ch()
    conv = await conversation_store.get_conversation_owned(caller, conv_id)
    if not conv or conv.get("kind") != "chat":
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.post("/chat/conversations")
async def create_chat_conversation(request: CreateChatConversationRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """新建操练场/聊天对话（kind='chat'，与 Agent 会话隔离，仅持久化）。"""
    _require_ch()
    return await conversation_store.create_conversation(
        title=request.title,
        system_prompt=request.system_prompt,
        model=request.model,
        kind="chat",
        chat_settings=request.chat_settings,
        user_id=caller,
    )


@router.get("/chat/conversations")
async def list_chat_conversations(
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
    paged: bool = False,
    caller: str | None = Depends(get_agent_caller),
) -> Any:
    """列出当前调用者的聊天对话（kind='chat'）。paged=true 时走游标分页。"""
    _require_ch()
    if not paged:
        return await conversation_store.list_conversations_for_caller(caller, limit=limit, kind="chat")
    try:
        return await conversation_store.list_conversations_for_caller_paged(
            caller, limit=limit, kind="chat", cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/chat/conversations/{conv_id}/meta")
async def get_chat_conversation_meta(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """读取聊天对话元数据（kind='chat'，归属只校验调用者）。"""
    _require_ch()
    return await _get_chat_conversation_or_404(conv_id, caller)


@router.get("/chat/conversations/{conv_id}/messages")
async def list_chat_conversation_messages(
    conv_id: str,
    limit: int = Query(7, ge=1, le=50),
    cursor: int | None = None,
    caller: str | None = Depends(get_agent_caller),
) -> dict:
    """按用户轮取聊天对话消息分页（timeline 视图，游标翻页）。"""
    _require_ch()
    await _get_chat_conversation_or_404(conv_id, caller)
    return await conversation_store.get_messages_page_by_user_turns(conv_id, limit=limit, cursor=cursor, timeline=True)


@router.get("/chat/conversations/{conv_id}")
async def get_chat_conversation(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """取一条聊天对话的完整快照（元数据 + 全量消息历史）。"""
    _require_ch()
    conv = await _get_chat_conversation_or_404(conv_id, caller)
    messages = await conversation_store.get_messages(conv_id, timeline=True)
    return {"conversation": conv, "messages": messages}


@router.delete("/chat/conversations/{conv_id}")
async def delete_chat_conversation(conv_id: str, caller: str | None = Depends(get_agent_caller)) -> dict:
    """删除聊天对话并回收其会话注册表实例。"""
    _require_ch()
    await _get_chat_conversation_or_404(conv_id, caller)
    deleted = await conversation_store.delete_conversation(conv_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="conversation not found")
    conversation_state.evict(conv_id)
    return {"deleted": True}


@router.post("/chat/conversations/batch-delete")
async def batch_delete_chat_conversations(
    request: BatchDeleteConversationsRequest, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """批量删除聊天对话：未找到/无权的 id 一律归入 not_found（不区分两者）。"""
    _require_ch()
    return await _batch_delete_conversations(request.ids, caller)


@router.patch("/chat/conversations/{conv_id}")
async def update_chat_conversation(conv_id: str, request: UpdateChatConversationRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """部分更新聊天对话（标题/系统提示词/模型/chat_settings）。"""
    _require_ch()
    await _get_chat_conversation_or_404(conv_id, caller)
    fields = {k: v for k, v in request.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    conv = await conversation_store.update_conversation(conv_id, **fields)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.post("/chat/conversations/{conv_id}/messages")
async def append_chat_message(conv_id: str, request: AppendChatMessageRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """追加一条聊天消息（前端驱动发送的落库入口）。

    首条用户消息自动取输入截断做标题；聊天会话无会话级流，实时信号就是
    「消息变了」——广播 message_appended 让旁观端重拉历史。
    """
    conv = await _get_chat_conversation_or_404(conv_id, caller)
    msg = await conversation_store.add_message(
        conv_id, request.role, request.content, status=request.status, error=request.error,
        media=request.media, model=request.model, usage=request.usage,
    )
    await conversation_store.touch_conversation(conv_id)
    # 首条用户消息自动取标题
    if conv.get("title") == "新对话" and request.role == "user" and request.content:
        title = request.content[:30] + ("..." if len(request.content) > 30 else "")
        await conversation_store.update_conversation(conv_id, title=title)
    # 聊天会话没有会话级流（/chat/send 是无状态 POST-SSE，不绑 conv_id），所以
    # 这里的实时信号就是「消息变了」：旁观端收到后重拉历史即可，形态与快照对齐。
    await realtime.publish("chat", conv_id, {"type": "message_appended", "message": msg})
    return msg


@router.delete("/chat/conversations/{conv_id}/messages/{message_id}")
async def delete_chat_message(conv_id: str, message_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """删除一条聊天消息（软删）。前端驱动聊天重试失败轮时用来清掉残留的
    error assistant 消息，使历史回放与重发上下文干净。
    """
    await _get_chat_conversation_or_404(conv_id, caller)
    deleted = await conversation_store.delete_message(int(message_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="message not found")
    await realtime.publish("chat", conv_id, {"type": "message_deleted", "message_id": int(message_id)})
    return {"deleted": True}


# ── 聊天发送（服务端内部调主链路，前端不碰 api key 明文）────────────────
# 前端传 api_key_id（整数），后端校验该 key 属于当前用户后取明文，
# 用它调 main.dispatch_entry 走完整主链路（限流/计费/日志归属）。
# 用户模式：只能用自己名下的 key；越权 key_id → 403。

class ChatSendRequest(BaseModel):
    api_key_id: int
    model: str = Field(..., min_length=1)
    messages: list[dict] = Field(..., min_length=1)
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None


class ChatMediaRequest(BaseModel):
    api_key_id: int
    mode: Literal["image", "video", "tts"]
    model: str = Field(..., min_length=1)
    prompt: str | None = None          # image / video
    input: str | None = None           # tts
    size: str | None = None
    n: int | None = None
    seconds: int | None = None         # video
    voice: str | None = None           # tts
    response_format: str | None = None
    speed: float | None = None         # tts


async def _resolve_group_system_key(api_key_id: int, caller: str) -> dict | None:
    """在调用者所在分组绑定的系统 key 里找 api_key_id（平台代配 key 兜底）。

    系统 key 的 user_id 为 NULL（不属于个人），靠 group_ids 授权给分组成员。
    compat 层未启用 / 无绑定 / 越权 id → None，交由调用方按 403 处理。任何
    import/查询失败都吞成 None，不影响自有 key 的既有路径。
    """
    try:
        from user_platform.routes import resolve_system_api_key_for_user
    except Exception:
        return None
    try:
        rows = await resolve_system_api_key_for_user(str(caller))
    except Exception:
        return None
    for row in rows:
        if row.get("id") == api_key_id:
            return row
    return None


async def _resolve_caller_derived_key(api_key_id: int, caller: str) -> dict | None:
    """通过根 Key 的授权解析 task/editor/copy 派生 Key。

    分组系统 Key 的 ``user_id`` 为 NULL；由它派生的子 Key 也没有个人 owner，且
    分组授权查询只返回根 Key，因此普通的 owner / group 两条路径都无法命中子 Key。
    这里沿 ``parent_id`` 上溯到根，再用完全相同的个人归属/分组授权规则校验根；
    通过后返回子 Key 本身，使子 Key 自己的模型范围与 disabled 状态继续生效。
    """
    from db import PostgresClient

    child = await PostgresClient.get_api_key_by_id(api_key_id)
    if not child or not child.get("parent_id"):
        return None

    root = child
    # 写入侧会把派生链拍平到根；循环上限仅防御历史脏数据或意外环。
    seen = {int(child["id"])}
    for _ in range(8):
        parent_id = root.get("parent_id")
        if not parent_id:
            break
        parent_id = int(parent_id)
        if parent_id in seen:
            return None
        seen.add(parent_id)
        parent = await PostgresClient.get_api_key_by_id(parent_id)
        if not parent:
            return None
        root = parent
    else:
        return None

    root_id = int(root["id"])
    if str(root.get("user_id") or "") == str(caller):
        return child
    if await _resolve_group_system_key(root_id, caller):
        return child
    return None


async def _caller_is_admin(caller: str) -> bool:
    """C 端 session 调用者是否为平台管理员（``User.role == "admin"``）。

    只做「是否放宽到全平台 Key」的判定：查不到 / 查询异常一律按**非**管理员处理，
    失败时收紧而不是放宽。
    """
    try:
        from user_platform.models import User

        user = await User.get_or_none(id=caller)
    except Exception:
        return False
    if user is None:
        return False
    return bool(
        user.role == "admin"
        and not getattr(user, "is_deleted", False)
        and not getattr(user, "is_blocked", False)
    )


async def _resolve_caller_api_key(api_key_id: int, caller: str | None) -> dict:
    """按调用者解析 api_key_id → 含明文 key 的行；无权/不存在 → 403。

    普通用户依次检查个人 Key、分组系统根 Key，以及根 Key 对调用者已授权的
    task/editor/copy 派生 Key；以上都不命中且调用者是平台管理员（role==admin）时，
    放宽到全平台任意 Key。caller=None（应急管理员 Bearer）本就没有用户域，直接
    按 id 全平台取。

    管理员判定放在用户域查找**之后**：管理员用自己的 Key 走原路径，不多付一次
    User 查询；只有越出用户域时才查 role。
    """
    from db import PostgresClient
    if caller is None:
        # 应急管理员模式：按 id 直接取，不必全量 list_api_keys 再线性找。
        row = await PostgresClient.get_api_key_by_id(api_key_id)
    else:
        row = await PostgresClient.get_api_key_by_id_for_user(api_key_id, caller)
        if not row:
            row = await _resolve_group_system_key(api_key_id, caller)
        if not row:
            row = await _resolve_caller_derived_key(api_key_id, caller)
        if not row and await _caller_is_admin(caller):
            row = await PostgresClient.get_api_key_by_id(api_key_id)
    if not row or not row.get("key"):
        raise HTTPException(status_code=403, detail="无权使用该 API Key")
    if row.get("disabled"):
        raise HTTPException(status_code=403, detail="该 API Key 已禁用")
    return row


def _humanize_context_window(tokens: int | None) -> str:
    """把上下文 token 数格式化成人类可读的窗口大小（如 1M / 200K）。"""
    try:
        n = int(tokens or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1_000_000:
        value = n / 1_000_000
        return (f"{value:.1f}".rstrip("0").rstrip(".")) + "M"
    if n >= 1_000:
        return f"{n // 1000}K"
    return str(n)


def _model_description(m: dict) -> str:
    """从模型元数据合成一段人类可读的说明（上下文窗口 + 能力标签）。

    模型元数据没有独立的 description 字段，可读信息分散在 name/上下文/能力/模态
    里。这里把它们拼成聊天与 Agent 选择器要展示的「模型 ID 下方说明」。
    """
    parts: list[str] = []
    if m.get("type") == "model_group":
        parts.append("自定义模型组")
    ctx = _humanize_context_window(m.get("max_context_tokens"))
    if ctx:
        parts.append(f"{ctx} 上下文")
    if m.get("function_calling"):
        parts.append("工具调用")
    if m.get("is_thinking") or m.get("auto_thinking"):
        parts.append("思考")
    outputs = m.get("output_modalities") or []
    modality_labels = {"image": "图片", "video": "视频", "audio": "语音"}
    extra_out = [modality_labels[o] for o in outputs if o in modality_labels]
    if extra_out:
        parts.append("生成" + "/".join(extra_out))
    inputs = m.get("input_modalities") or []
    if any(i in ("image", "video", "audio") for i in inputs):
        parts.append("多模态输入")
    return " · ".join(parts)


@router.get("/chat/models")
async def chat_available_models(api_key_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """按 api_key_id 的白/黑名单过滤后的可用模型列表（含自定义 model_group）。

    每条附带合成的 ``description``：上下文窗口 + 能力标签，供前端在模型 ID 下方
    展示（元数据无独立说明字段，故在此拼装）。
    """
    key_row = await _resolve_caller_api_key(api_key_id, caller)
    from rate_limiter import ModelClientPool
    # 白/黑名单与自定义模型成员裁剪都由 get_models_response 收口，这里只补 description。
    resp = await ModelClientPool.get_models_response(api_key=key_row["key"])
    all_models = resp.get("data", []) if isinstance(resp, dict) else []
    out = []
    for m in all_models:
        if not m.get("id"):
            continue
        item = dict(m)
        item["description"] = _model_description(item)
        out.append(item)
    return {"object": "list", "data": out}


@router.get("/agent-llm-models")
async def list_agent_llm_models_for_caller(caller: str | None = Depends(get_agent_caller)) -> list[dict]:
    """Agent 创建/编辑时可选的 LLM 模型库存（仅启用项，脱敏）。

    仅返回 id/model_name/display_name/config 名称等展示字段，绝不回传 api_key
    等密钥。用户和管理员都能读（共享的 LLM 基础设施），依赖 get_agent_caller 拦未登录。
    """
    from db import PostgresClient
    if not PostgresClient.pool:
        return []
    rows = await PostgresClient.list_agent_llm_models(enabled_only=True)
    out = []
    for r in rows:
        out.append({
            "id": r.get("id"),
            "llm_config_id": r.get("llm_config_id"),
            "model_name": r.get("model_name"),
            "display_name": r.get("display_name"),
            "config_name": r.get("config_name"),
            "enabled": r.get("enabled"),
            "config_enabled": r.get("config_enabled"),
        })
    return out


@router.get("/available-mcp")
async def list_available_mcp(caller: str | None = Depends(get_agent_caller)) -> dict:
    """Agent 编辑器可选的 MCP 服务，按来源分组返回。

    三类：
      - builtin：内置服务（cdp-bridge/mail），用户登录即可挂（受服务本身 enabled 门禁）。
      - admin：管理端配置且 enabled 的服务，user_id IS NULL；用户需被 mcp_service_users 授权。
      - upstream：当前用户自配的 kind=sse 服务（user_id=自己）。

    每项字段：{id, name, display_name, source, kind, tool_count, enabled, description}。
    管理员模式（caller=None）不区分个人 upstream，admin 段返回全部管理端服务。

    场景保留服务（marketplace-status / issue-workflow / review-result）不在选择器里
    露出：它们只由对应场景的对话临时获权（见 ``agent_main._collect_service_tokens``
    的 scene.capabilities 门禁），普通 Agent 不应能勾选。
    """
    import mcp_plugin_store

    # 场景保留：只在对应场景的对话里临时签发身份，不进 MCP 选择器。
    reserved_scene_services = {"marketplace-status", "issue-workflow", "review-result"}

    async def _summarize(svc: dict, source: str) -> dict:
        tools_cache = svc.get("tools_cache") or []
        return {
            "id": svc.get("id"),
            "name": svc.get("name"),
            "display_name": svc.get("display_name") or svc.get("name"),
            "source": source,
            "kind": svc.get("kind") or ("builtin" if svc.get("builtin") else "custom"),
            "tool_count": len(tools_cache) if isinstance(tools_cache, list) else 0,
            "enabled": bool(svc.get("enabled")),
            "description": svc.get("description") or "",
        }

    try:
        all_services = await mcp_plugin_store.list_services()
    except Exception as exc:  # noqa: BLE001 — DB 不可用不阻断 Agent 编辑器打开
        logger.warning("[agent] available-mcp list_services failed: %s", exc)
        return {"builtin": [], "admin": [], "upstream": []}

    builtin_items: list[dict] = []
    admin_items: list[dict] = []
    upstream_items: list[dict] = []

    # 注：mcp_service_users 表的 user_id 引用 mcp_users.id（INTEGER），与 C 端 session 的
    # user_platform user_id（UUID）不是同一个用户模型。此处 admin 段不做按用户授权过滤，
    # 返回所有 user_id IS NULL 且 enabled 的管理端服务；per-user 授权在此处无意义。
    for svc in all_services:
        name = str(svc.get("name") or "")
        if name in reserved_scene_services:
            continue
        is_builtin = bool(svc.get("builtin")) or (svc.get("kind") or "stdio") == "builtin"
        owner = svc.get("user_id")
        if is_builtin:
            builtin_items.append(await _summarize(svc, "builtin"))
            continue
        if owner is None:
            admin_items.append(await _summarize(svc, "admin"))
        elif caller is None or owner == caller:
            # 用户模式只看自己；管理员模式看所有个人 upstream。
            upstream_items.append(await _summarize(svc, "upstream"))

    return {"builtin": builtin_items, "admin": admin_items, "upstream": upstream_items}


@router.post("/chat/send")
async def chat_send(request: ChatSendRequest, caller: str | None = Depends(get_agent_caller)):
    """文本对话：服务端用 api_key_id 对应的明文 key 调主链路 dispatch_entry。"""
    key_row = await _resolve_caller_api_key(request.api_key_id, caller)
    api_key = key_row["key"]
    api_key_name = key_row.get("name") or ""

    import main as _main
    import config as _config_mod

    body: dict[str, Any] = {
        "model": request.model,
        "messages": request.messages,
        "stream": request.stream,
    }
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.reasoning_effort is not None:
        effort = _normalize_reasoning_effort(request.reasoning_effort)
        if effort:
            body["reasoning_effort"] = effort

    provider_whitelist, provider_blacklist = await _config_mod.Config.get_api_key_provider_filter(api_key)
    dispatch = await _main.dispatch_entry(
        endpoint="/v1/chat/completions",
        body=body,
        headers=None,
        api_key=api_key,
        api_key_name=api_key_name,
        provider_whitelist=provider_whitelist,
        provider_blacklist=provider_blacklist,
        request_protocol="openai",
        chat_method="chat",
        client_type="user-chat",
    )
    stream_gen = dispatch["generator"]

    if dispatch["stream"]:
        async def event_stream():
            try:
                async for chunk in stream_gen:
                    if isinstance(chunk, dict) and "_last_route_info" in chunk:
                        continue
                    yield chunk
            except Exception as e:
                err = _main._openai_stream_error_chunk(str(e)) if hasattr(_main, "_openai_stream_error_chunk") else f"data: {json.dumps({'error': {'message': str(e)}}, ensure_ascii=False)}\n\n"
                yield err
                yield "data: [DONE]\n\n"
        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_stream_headers())

    result = await _main._collect_non_stream_result(stream_gen)
    if isinstance(result, dict):
        result.pop("_last_route_info", None)
        result.pop("_first_token_ms", None)
    return result


@router.post("/chat/media")
async def chat_media(request: ChatMediaRequest, caller: str | None = Depends(get_agent_caller)):
    """图片 / 视频 / 语音生成：服务端用 api_key_id 对应明文 key 调主链路。"""
    key_row = await _resolve_caller_api_key(request.api_key_id, caller)
    api_key = key_row["key"]
    api_key_name = key_row.get("name") or ""

    import main as _main
    import config as _config_mod

    body: dict[str, Any] = {"model": request.model}
    if request.mode in ("image", "video"):
        if not request.prompt:
            raise HTTPException(status_code=400, detail="缺少 prompt 参数")
        body["prompt"] = request.prompt
        if request.size:
            body["size"] = request.size
        if request.n is not None:
            body["n"] = request.n
        if request.mode == "video" and request.seconds is not None:
            body["seconds"] = request.seconds
        body["response_format"] = request.response_format or "url"
        operation = request.mode
    else:  # tts
        if not request.input:
            raise HTTPException(status_code=400, detail="缺少 input 参数")
        body["input"] = request.input
        body["voice"] = request.voice or "alloy"
        body["response_format"] = request.response_format or "mp3"
        if request.speed is not None:
            body["speed"] = request.speed
        operation = "tts_generation"

    endpoint_map = {
        "image": "/v1/images/generations",
        "video": "/v1/videos/generations",
        "tts": "/v1/audio/speech",
    }
    provider_whitelist, provider_blacklist = await _config_mod.Config.get_api_key_provider_filter(api_key)
    dispatch = await _main.dispatch_entry(
        endpoint=endpoint_map[request.mode],
        body=body,
        headers=None,
        api_key=api_key,
        api_key_name=api_key_name,
        provider_whitelist=provider_whitelist,
        provider_blacklist=provider_blacklist,
        operation=operation,
        client_type="user-chat",
    )
    result = dispatch["result"]
    # TTS 二进制结果转 base64 交给前端播放；image/video 返回 dict（含 url/b64）
    if request.mode == "tts":
        import base64 as _b64
        if isinstance(result, (bytes, bytearray)):
            audio_b64 = _b64.b64encode(bytes(result)).decode("ascii")
            return {"audio": audio_b64, "content_type": _main._audio_content_type(request.response_format or "mp3")}
        if isinstance(result, dict):
            return result
        return {"audio": None, "raw": result}
    return result




def _ensure_scheduler_running(requested_enabled: bool | None) -> None:
    """见 agent.runner.ensure_scheduler_running：开了调度意图就幂等拉起调度器。

    仍被 /agents CRUD 的 per-agent 定时开关调用（scheduler_enabled）。工作流的 cron
    走独立调度器，不经过这里。
    """
    from agent.runner import ensure_scheduler_running

    ensure_scheduler_running(requested_enabled)


# ---------------------------------------------------------------------------
# Workflow endpoints (user-scoped: a workflow is an **independent artifact**
# owned by a user. An agent — like the UI and cron — can only *launch* a run;
# how the run executes is decided by the workflow's own steps, each of which
# names its own resources (browser clients, MCP service, target agent).
# ---------------------------------------------------------------------------

async def _resolve_workflow_access(def_id: int, caller: str | None) -> dict:
    """Load a workflow def and verify the caller **owns** it.

    Ownership is the whole rule now (there is no owning agent to delegate to):
    the def is reachable by its ``user_id`` owner, and by an admin
    (caller=None). Someone else's workflow reads as 404 — no existence oracle.
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise HTTPException(status_code=503, detail="database unavailable")
    async with PostgresClient.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM workflow_defs WHERE id=$1", def_id)
    if not row:
        raise HTTPException(status_code=404, detail="workflow not found")
    d = dict(row)
    owner = d.get("user_id")
    if caller is not None and owner is not None and str(owner) != str(caller):
        raise HTTPException(status_code=404, detail="workflow not found")
    return d


@router.get("/workflows")
async def list_workflows(caller: str | None = Depends(get_agent_caller)) -> list[dict]:
    """Workflows the caller owns (latest version each), with node counts."""
    from agent import workflow_store

    rows = await workflow_store.list_defs(caller)
    out = []
    for r in rows:
        item = dict(r)
        defn = item.get("definition")
        if isinstance(defn, str):
            try:
                item["definition"] = json.loads(defn)
            except ValueError:
                pass
        for key in ("created_at", "updated_at", "next_run_at", "last_run_at"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        item["node_count"] = len((item.get("definition") or {}).get("nodes") or [])
        out.append(item)
    return out


@router.post("/workflows")
async def create_workflow(request: WorkflowDefRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """Create a workflow owned by the caller."""
    from agent import workflow_store

    try:
        row = await workflow_store.create_def(
            request.name, request.description, request.definition,
            user_id=await _resolve_agent_owner(caller) if caller is not None else None,
            label=request.label,
            retry_on_reboot=request.retry_on_reboot,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        if "WorkflowDefError" in type(exc).__name__ or "definition" in detail.lower():
            raise HTTPException(status_code=400, detail=f"definition invalid: {detail}")
        raise HTTPException(status_code=500, detail=detail)
    return {"id": row["id"], "version": row["version"], "name": row["name"], "label": row.get("label", "")}


class WorkflowImportRequest(BaseModel):
    """JSON 文件导入：整包搬运可移植字段（不含 id/版本/归属/运行记录）。"""
    name: str = Field(..., min_length=1, max_length=200)
    label: str = Field(default="", max_length=200)
    description: str = ""
    definition: dict[str, Any]
    retry_on_reboot: bool = True


@router.post("/workflows/import")
async def import_workflow(request: WorkflowImportRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """导入一个工作流 JSON → 在调用者名下新建 v1（后端做全图静态校验）。"""
    return await create_workflow(
        WorkflowDefRequest(
            name=request.name, label=request.label, description=request.description,
            definition=request.definition, retry_on_reboot=request.retry_on_reboot,
        ),
        caller=caller,
    )


@router.get("/workflows/export/{def_id}")
async def export_workflow(def_id: int, version: int | None = None, caller: str | None = Depends(get_agent_caller)) -> dict:
    """导出为可移植 JSON（kind/name/label/description/definition）。

    归属、版本、定时、运行记录**不可移植**——换环境就是换所有者与运行身份；
    导入端据此新建 v1。
    """
    from agent import workflow_store

    await _resolve_workflow_access(def_id, caller)
    row = await workflow_store.get_def(def_id, version=version)
    if not row:
        raise HTTPException(status_code=404, detail="workflow version not found")
    return {
        "kind": "workflow",
        "name": row["name"],
        "label": row.get("label") or "",
        "description": row.get("description") or "",
        "definition": row["definition"],
    }


# 静态子路径必须在 /workflows/{def_id} 之前声明：def_id 是 int 路径参数，
# "options"/"accounts"/"export" 落到那条路由会 422 而不是命中这里。
@router.get("/workflows/options")
async def workflow_options(caller: str | None = Depends(get_agent_caller)) -> dict:
    """步骤编辑器的一次性选项包：agent 列表（run_session）、MCP 服务+方法
    （run_mcp）、浏览器客户端（web）、cache 固定前缀。

    全部按调用者身份取，不与 agent 相关：工作流是用户资产。
    """
    from db import PostgresClient

    agents: list[dict] = []
    services: list[dict] = []
    browsers: list[dict] = []
    if PostgresClient.pool:
        team_id = await _resolve_team_id_safe(caller) if caller else None
        for row in await PostgresClient.list_agents_for_caller(caller, team_id):
            agents.append({
                "id": row["id"],
                "name": row.get("name") or "",
                "display_name": row.get("display_name") or row.get("name") or "",
                "enabled": bool(row.get("enabled", True)),
            })
        import mcp_plugin_store

        try:
            all_services = await mcp_plugin_store.list_services()
        except Exception as exc:  # noqa: BLE001 — MCP 不可用不该拦编辑器打开
            logger.warning("[workflow] options list_services failed: %s", exc)
            all_services = []
        enabled_services = [s for s in all_services if s.get("enabled")]
        try:
            usable = await mcp_plugin_store.usable_service_ids(
                user_id=caller, services=enabled_services,
            )
        except Exception:  # noqa: BLE001
            usable = {int(s["id"]) for s in enabled_services if s.get("id") is not None}
        for svc in enabled_services:
            sid = svc.get("id")
            if sid is None or int(sid) not in usable:
                continue
            methods = []
            cache = svc.get("tools_cache") or []
            if isinstance(cache, list):
                for m in cache:
                    if isinstance(m, dict) and m.get("name"):
                        methods.append({
                            "name": m["name"],
                            "description": m.get("description") or "",
                            "input_schema": m.get("input_schema") or {},
                        })
            services.append({
                "name": svc.get("name") or "",
                "display_name": svc.get("display_name") or svc.get("name") or "",
                "description": svc.get("description") or "",
                "methods": methods,
            })
        try:
            import builtin_tool_store

            for resource in await builtin_tool_store.list_resources(
                "cdp_client", owner_user_id=caller, enabled=True,
            ):
                cid = builtin_tool_store.cdp_client_id_of(resource)
                if cid:
                    browsers.append({
                        "client_id": cid,
                        "name": resource.get("name") or cid,
                        "enabled": bool(resource.get("enabled", True)),
                    })
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] options cdp clients failed: %s", exc)

    from agent.workflow import CACHE_KEY_PREFIX, NODE_KINDS

    # 模型库存（model 类入参的候选）：与 Agent 编辑器同源（脱敏、仅启用）。
    models: list[dict] = []
    if PostgresClient.pool:
        try:
            for r in await PostgresClient.list_agent_llm_models(enabled_only=True):
                models.append({
                    "id": r.get("id"),
                    "model_name": r.get("model_name") or "",
                    "display_name": r.get("display_name") or r.get("model_name") or "",
                    "config_name": r.get("config_name") or "",
                })
        except Exception:  # noqa: BLE001 — 模型列表拉不到不该拦编辑器
            logger.warning("[workflow] options models failed", exc_info=True)

    # 代理池（http 步骤 / proxy 入参）：按归属过滤——非管理员只见到 owner_user_id
    # 为自己的条目（url/凭据从不暴露，只回 id/name/mode）。拉不到就空列表，不拦编辑器。
    proxies: list[dict] = []
    try:
        import admin as _admin

        all_proxies = _admin._read_proxies()
        for p in all_proxies:
            if caller is not None and str(p.get("owner_user_id") or "") not in ("", caller):
                continue
            proxies.append({
                "id": str(p.get("id") or ""),
                "name": str(p.get("name") or ""),
                "mode": str(p.get("mode") or "network"),
            })
    except Exception:  # noqa: BLE001 — 代理池读不到不该拦编辑器
        logger.warning("[workflow] options proxies failed", exc_info=True)

    return {
        "node_kinds": list(NODE_KINDS),
        "cache_key_prefix": CACHE_KEY_PREFIX,
        "agents": agents,
        "mcp_services": services,
        "browsers": browsers,
        "models": models,
        "proxies": proxies,
    }


@router.get("/workflows/accounts")
async def workflow_account_options(url: str = Query(...), caller: str | None = Depends(get_agent_caller)) -> dict:
    """入参来源「密钥账户列表」预览：按 url 的站点返回账号列表。

    只回账号元数据（id/domain/username/url），**绝不回密码**——密码的解密出口
    是浏览器密码管理页的二次鉴权路径，工作流拿不到也不该拿。
    """
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail="url 无效（无法解析站点）")
    if caller is None:
        return {"ok": True, "accounts": []}
    import browser_password_store

    rows = await browser_password_store.list_passwords(str(caller), domain=host)
    return {
        "ok": True,
        "accounts": [
            {"id": r["id"], "domain": r["domain"], "username": r["username"], "url": r.get("url") or ""}
            for r in rows
        ],
    }


@router.get("/workflows/notify-channels")
async def workflow_notify_channels(caller: str | None = Depends(get_agent_caller)) -> list[dict]:
    """结束节点「通知方式」的候选：当前用户（含其团队）的启用通知渠道。

    只回 meta（id/name/kind），**绝不回 webhook_url/secret**。渠道的新建/编辑在
    通知设置页做，这里只给多选。
    """
    if caller is None:
        return []
    from user_platform.notify_dispatch import list_owner_channels

    return await list_owner_channels(str(caller))


@router.get("/workflows/{def_id}")
async def get_workflow(def_id: int, version: int | None = None, caller: str | None = Depends(get_agent_caller)) -> dict:
    """读取工作流定义；不传 version 取最新版，传了取指定历史版本。"""
    from agent import workflow_store

    await _resolve_workflow_access(def_id, caller)
    row = await workflow_store.get_def(def_id, version=version)
    if not row:
        raise HTTPException(status_code=404, detail="workflow version not found")
    return row


@router.patch("/workflows/{def_id}")
async def update_workflow(def_id: int, request: WorkflowUpdateRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """保存工作流定义（产生新版本）；启动方式（triggers，含定时）随定义一起保存。

    保存后幂等重载该 def 的调度 job，避免「编辑完定时没生效」；重载失败不影响保存。
    """
    from agent import workflow_store

    await _resolve_workflow_access(def_id, caller)
    try:
        row = await workflow_store.update_def(
            def_id, request.definition, description=request.description, label=request.label,
            retry_on_reboot=request.retry_on_reboot,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # 启动方式（triggers，含定时）随定义一起保存。保存后幂等重载该 def 的 job——
    # 调度器仍按旧 job 注册着（reload_def 才会指向最新定义），否则「编辑完定时没生效」。
    try:
        from agent.workflow_scheduler import get_workflow_scheduler

        sched = get_workflow_scheduler()
        sched.start()
        await sched.reload_def(def_id)
    except Exception:  # noqa: BLE001 - 重载失败不影响保存本身
        pass
    return {"id": row["id"], "version": row["version"], "name": row["name"], "label": row.get("label", "")}


@router.delete("/workflows/{def_id}")
async def delete_workflow(def_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """删除工作流（破坏性操作）：删定义行并按家族口径清掉调度器里全部版本名的 cron job。"""
    from agent import workflow_store

    await _resolve_workflow_access(def_id, caller)
    ok = await workflow_store.delete_def(def_id)
    # 行删了，调度器里注册着的 cron job 不会自己消失——不摘掉就是「删掉的工作流继续按
    # 定时点火」（点火时定义已不存在，只剩错误日志）。remove 走家族口径，
    # 同工作流所有版本行名下的 job 一起清。
    try:
        from agent.workflow_scheduler import get_workflow_scheduler

        await get_workflow_scheduler().remove(def_id)
    except Exception:  # noqa: BLE001 - 摘 job 失败不影响删除本身
        pass
    return {"deleted": bool(ok)}


@router.post("/workflows/{def_id}/run")
async def run_workflow_endpoint(def_id: int, request: WorkflowRunRequest, caller: str | None = Depends(get_agent_caller)) -> dict:
    """手动跑一次工作流。后台执行，立即返回 run_id；进度走 GET /workflows/{def_id}/runs/{run_id}。

    ``trigger_id`` 命名定义里声明的某个启动方式（cron）：其 ``params`` 解析成本次的
    ``$input.*``。``account_list`` + ``multiple=true`` 会**整图按账号扇出 N 遍**——
    此时返回多条 ``run_ids``（``run_id`` 取第一条，兼容旧前端）。
    """
    from agent import workflow_store, workflow_service

    access = await _resolve_workflow_access(def_id, caller)
    try:
        run_ids = await workflow_service.start_and_run_multi(
            def_id,
            args=request.args,
            version=request.version,
            trigger="manual",
            caller=caller,
            trigger_id=request.trigger_id,
            single_account_only=request.single_account_only,
            pick_accounts=request.pick_accounts,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "run_id": run_ids[0] if run_ids else 0,
        "run_ids": run_ids,
        "def_id": def_id,
        "version": access.get("version"),
    }


@router.get("/workflows/{def_id}/runs")
async def list_workflow_runs(def_id: int, limit: int = 50, caller: str | None = Depends(get_agent_caller)) -> list[dict]:
    """列出该工作流的运行记录（最近在前，limit 上限截断）。"""
    from agent import workflow_store

    await _resolve_workflow_access(def_id, caller)
    return await workflow_store.list_runs(def_id, limit=limit)


@router.get("/workflows/{def_id}/runs/{run_id}")
async def get_workflow_run(def_id: int, run_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """单条运行记录全字段：节点级日志（status/output/error/decision/duration）+
    变量快照。前端拿 run_id 渲染节点时间线。"""
    from agent import workflow_store

    await _resolve_workflow_access(def_id, caller)
    run = await workflow_store.get_run(run_id, family_of=def_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/workflows/{def_id}/runs/{run_id}/cancel")
async def cancel_workflow_run(def_id: int, run_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """取消在跑的 run（本进程的 asyncio task）。跨进程/已结束的 run 返回 cancelled=false。"""
    from agent import workflow_service

    await _resolve_workflow_access(def_id, caller)
    return {"run_id": run_id, "cancelled": workflow_service.cancel_run(run_id)}


class WorkflowScheduleRequest(BaseModel):
    """兼容旧前端：定时已改为定义内的「启动方式（triggers）」。此请求体不再读取。"""
    cron_expression: str | None = None
    enabled: bool = True


@router.post("/workflows/{def_id}/reload-schedule")
async def reload_workflow_schedule(def_id: int, caller: str | None = Depends(get_agent_caller)) -> dict:
    """按定义里声明的启动方式重载该工作流的定时 job（保存定义后调用）。

    定时配置本身随 definition 一起保存（PATCH /workflows/{def_id}），本端点只负责
    让调度器**立刻**反映最新定义——保存后幂等重载，避免「编辑完定时没生效」。
    """
    await _resolve_workflow_access(def_id, caller)
    from agent.workflow_scheduler import get_workflow_scheduler

    sched = get_workflow_scheduler()
    sched.start()
    await sched.reload_def(def_id)
    return {"def_id": def_id, "reloaded": True}


@router.patch("/workflows/{def_id}/schedule")
async def set_workflow_schedule(
    def_id: int, request: WorkflowScheduleRequest, caller: str | None = Depends(get_agent_caller),
) -> dict:
    """已废弃：定时改为定义内的启动方式。保留端点只为旧前端不 404——不做任何写入，
    返回提示，真正改定时请保存 definition（PATCH /workflows/{def_id}）。"""
    await _resolve_workflow_access(def_id, caller)
    raise HTTPException(
        status_code=410,
        detail="定时已改为定义内的「启动方式（triggers）」，请保存工作流定义来改定时",
    )


# ---------------------------------------------------------------------------
# Guardian mode endpoints (Goal Mode + Autonomous Mode)
# ---------------------------------------------------------------------------

class GoalModeRequest(BaseModel):
    objective: str = Field(..., min_length=1)
    budget_seconds: int = Field(..., ge=60)
    max_turns: int = 50


class AutonomousRequest(BaseModel):
    enabled: bool


async def _require_agent_access(agent_id: int, caller: str | None) -> None:
    """goal-mode / autonomous 等端点的归属校验：owner 或同团队共享成员可操作。

    管理员（caller=None）放行。非 owner 且非同团队共享 → 404。
    """
    if caller is None:
        return
    from db import PostgresClient
    if not PostgresClient.pool:
        raise HTTPException(503, "Database not available")
    row = await PostgresClient.get_agent(agent_id)
    if not row:
        raise HTTPException(404, f"Agent {agent_id} not found")
    owner = row.get("user_id")
    if owner is not None and str(owner) != str(caller):
        is_shared = bool(row.get("is_team_shared"))
        agent_team = row.get("team_id")
        caller_team = await _resolve_team_id_safe(caller)
        if not (is_shared and agent_team is not None and caller_team is not None
                and str(agent_team) == str(caller_team)):
            raise HTTPException(404, f"Agent {agent_id} not found")


@router.post("/agents/{agent_id}/goal-mode")
async def start_goal_mode(
    agent_id: int, request: GoalModeRequest, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """Start a goal-mode run for the agent."""
    await _require_agent_access(agent_id, caller)
    from agent.guardian import Guardian
    guardian = Guardian(agent_id=agent_id)
    try:
        return await guardian.start_goal_mode(
            objective=request.objective,
            budget_seconds=request.budget_seconds,
            max_turns=request.max_turns,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/agents/{agent_id}/goal-mode/stop")
async def stop_goal_mode(
    agent_id: int, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """Stop active goal mode for the agent."""
    await _require_agent_access(agent_id, caller)
    from agent.guardian import Guardian
    guardian = Guardian(agent_id=agent_id)
    stopped = await guardian.stop_goal_mode()
    return {"agent_id": agent_id, "stopped": stopped}


@router.get("/agents/{agent_id}/goal-mode/status")
async def goal_mode_status(
    agent_id: int, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """Get the latest goal-mode state for the agent."""
    await _require_agent_access(agent_id, caller)
    from agent.guardian import Guardian
    guardian = Guardian(agent_id=agent_id)
    status = await guardian.get_goal_status()
    return status or {"agent_id": agent_id, "status": "idle"}


@router.post("/agents/{agent_id}/autonomous")
async def toggle_autonomous(
    agent_id: int, request: AutonomousRequest, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """Enable or disable autonomous mode for the agent."""
    await _require_agent_access(agent_id, caller)
    from agent.guardian import Guardian
    guardian = Guardian(agent_id=agent_id)
    try:
        return await guardian.set_autonomous_enabled(request.enabled)
    except RuntimeError as e:
        raise HTTPException(404, str(e))


@router.get("/agents/{agent_id}/autonomous/status")
async def autonomous_status(
    agent_id: int, caller: str | None = Depends(get_agent_caller)
) -> dict:
    """Get autonomous mode status for the agent."""
    await _require_agent_access(agent_id, caller)
    from agent.guardian import Guardian
    guardian = Guardian(agent_id=agent_id)
    return await guardian.get_autonomous_status()


# ── 附件资源访问（agent 产出文件的外部访问层）──────────────────────
# show_file 工具产出 attachment_id（工作区文件）或 url（公网）；消息 media part
# 只存 id/url；各端用登录态在这里解析成可下载/预览 URL。鉴权收口在 attachment_store
# .get_for_owner：按 owner_user_id 精确匹配，未认领/非本人/已 purge → 404/410。
# to_public_dict 已带 renewable 字段，不暴露物理路径。

@router.get("/attachments/{attachment_id}")
async def get_attachment_meta_endpoint(
    attachment_id: int, caller: str | None = Depends(get_agent_caller),
) -> dict:
    """附件元信息（不含文件字节）。purged 返回 410；过期仍可读元信息以便续期。"""
    if caller is None:
        raise HTTPException(status_code=403, detail="仅登录用户可访问附件")
    import attachment_store
    row = await attachment_store.get_for_owner(attachment_id, caller, include_purged=True)
    if not row:
        raise HTTPException(status_code=404, detail="附件不存在或无权访问")
    if row.get("status") == "purged":
        raise HTTPException(status_code=410, detail="附件已被清理")
    d = attachment_store.to_public_dict(row)
    d["download_url"] = f"/agent/attachments/{attachment_id}/content"
    d["thumbnail_url"] = f"/agent/attachments/{attachment_id}/thumbnail"
    # 同时签发短时签名 URL，前端/mobile 优先用签名 URL 访问，不再依赖登录态。
    import attachment_signing
    signed_content = attachment_signing.sign_attachment_url(attachment_id, "content")
    signed_thumb = attachment_signing.sign_attachment_url(attachment_id, "thumbnail")
    if signed_content:
        d["content_url"] = signed_content
    if signed_thumb:
        d["thumbnail_url"] = signed_thumb
    return d


@router.get("/attachments/{attachment_id}/content")
async def get_attachment_content_endpoint(
    attachment_id: int,
    caller: str | None = Depends(get_agent_caller),
    exp: int | None = Query(None),
    sig: str | None = Query(None),
):
    """流式返回附件文件内容。图片/PDF/文本 inline 预览，其余下载。

    鉴权优先级：带 ``exp``+``sig`` 且签名校验通过 → 签名 URL 即凭证，免登录态，
    按 id 取行；否则回退到登录态 owner 校验（旧客户端/管理端兼容）。
    """
    import attachment_store
    import attachment_signing
    signed_ok = exp is not None and attachment_signing.verify_attachment_token(attachment_id, "content", int(exp), sig)
    if signed_ok:
        row = await attachment_store.get_active_by_id(attachment_id)
        if not row:
            # 签名有效但附件不存在/已清理：与登录态 404 一致，不暴露归属。
            raise HTTPException(status_code=404, detail="附件不存在或无权访问")
    else:
        if caller is None:
            raise HTTPException(status_code=403, detail="仅登录用户可访问附件")
        row = await attachment_store.get_for_owner(attachment_id, caller)
        if not row:
            raise HTTPException(status_code=404, detail="附件不存在或无权访问")
    status = row.get("status")
    if status == "purged":
        raise HTTPException(status_code=410, detail="附件已被清理")
    if status == "expired":
        raise HTTPException(status_code=410, detail="附件已过期，需续期后访问")
    path = attachment_store.object_path(row)
    if not path:
        raise HTTPException(status_code=410, detail="附件内容不可用（可能已被清理）")
    import os as _os
    if not _os.path.isfile(path):
        raise HTTPException(status_code=410, detail="附件文件不存在")
    mime = row.get("mime_type") or "application/octet-stream"
    name = row.get("name") or f"attachment-{attachment_id}"
    # 媒体安全：SVG 一律强制下载（同源 inline SVG 可执行脚本/引用外部资源）；
    # 声称 image/* 的文件按 magic-byte 校验真实类型，不符则回退 octet-stream
    # + attachment，避免扩展名/MIME 欺骗触发 inline 渲染。
    svg = attachment_store.is_svg_attachment(row, path)
    detected = attachment_store.detect_image_kind(path)
    if svg:
        return FileResponse(
            path,
            media_type="image/svg+xml",
            filename=name,
            content_disposition_type="attachment",  # type: ignore[arg-type]
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=300",
            },
        )
    declared_image = (mime or "").startswith("image/")
    if declared_image and detected and detected != "image/svg+xml":
        mime = detected
    elif declared_image and (not detected or detected == "image/svg+xml"):
        # 声称是图但字节不是 raster：不 inline，按二进制下载。
        mime = "application/octet-stream"
    inline_prefixes = ("image/", "text/", "application/pdf", "application/json")
    disposition = "inline" if any(mime.startswith(p) for p in inline_prefixes) else "attachment"
    headers = {"X-Content-Type-Options": "nosniff"}
    if disposition == "inline" and mime.startswith("image/"):
        headers["Cache-Control"] = "private, max-age=300"
    return FileResponse(
        path,
        media_type=mime,
        filename=name,
        content_disposition_type=disposition,  # type: ignore[arg-type]
        headers=headers,
    )


@router.get("/attachments/{attachment_id}/thumbnail")
async def get_attachment_thumbnail_endpoint(
    attachment_id: int,
    caller: str | None = Depends(get_agent_caller),
    exp: int | None = Query(None),
    sig: str | None = Query(None),
):
    """图片附件缩略：直接回原图 inline（够轻量）；非图片返回 404，前端自然不渲染。

    与 content 端点同：签名 URL 验证通过即放行（免登录态），否则回退 owner 校验。
    缩略图签名与 content 不可互换（域分离 kind 区分）。
    """
    import attachment_store
    import attachment_signing
    signed_ok = exp is not None and attachment_signing.verify_attachment_token(attachment_id, "thumbnail", int(exp), sig)
    if signed_ok:
        row = await attachment_store.get_active_by_id(attachment_id)
        if not row:
            raise HTTPException(status_code=404, detail="附件不存在或无权访问")
    else:
        if caller is None:
            raise HTTPException(status_code=403, detail="仅登录用户可访问附件")
        row = await attachment_store.get_for_owner(attachment_id, caller)
        if not row:
            raise HTTPException(status_code=404, detail="附件不存在或无权访问")
    status = row.get("status")
    if status == "purged":
        raise HTTPException(status_code=410, detail="附件已被清理")
    if status == "expired":
        raise HTTPException(status_code=410, detail="附件已过期，需续期后访问")
    mime = row.get("mime_type") or ""
    if not mime.startswith("image/"):
        raise HTTPException(status_code=404, detail="非图片附件无缩略图")
    path = attachment_store.object_path(row)
    if not path:
        raise HTTPException(status_code=410, detail="附件内容不可用")
    import os as _os
    if not _os.path.isfile(path):
        raise HTTPException(status_code=410, detail="附件文件不存在")
    # SVG 与非 raster 不给缩略 inline：前者同源可执行，后者扩展名/MIME 可能欺骗；
    # 前端 raster-only 预览取不到图时自然回退附件卡，不强行 inline 任何内容。
    if attachment_store.is_svg_attachment(row, path):
        raise HTTPException(status_code=404, detail="非图片附件无缩略图")
    detected = attachment_store.detect_image_kind(path)
    if not detected or detected == "image/svg+xml":
        raise HTTPException(status_code=404, detail="非图片附件无缩略图")
    return FileResponse(
        path,
        media_type=detected,
        content_disposition_type="inline",  # type: ignore[arg-type]
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post("/attachments/{attachment_id}/renew")
async def renew_attachment_endpoint(
    attachment_id: int, caller: str | None = Depends(get_agent_caller),
) -> dict:
    """续期 +30 天（仅 owner）。purged 返回 410 不可续；active/expired 都可续。"""
    if caller is None:
        raise HTTPException(status_code=403, detail="仅登录用户可续期附件")
    import attachment_store
    # 带 include_purged 取本人附件：purged 也能命中以便返回 410 而非 404。
    owned = await attachment_store.get_for_owner(attachment_id, caller, include_purged=True)
    if not owned:
        raise HTTPException(status_code=404, detail="附件不存在或无权访问")
    if owned.get("status") == "purged":
        raise HTTPException(status_code=410, detail="附件已被清理，无法续期")
    row = await attachment_store.renew(attachment_id, caller)
    if not row:
        # 理论上不会到这（上面已确认 owner 且非 purged），兜底防越权。
        raise HTTPException(status_code=403, detail="无权续期该附件")
    d = attachment_store.to_public_dict(row)
    d["renewable"] = True
    return d
