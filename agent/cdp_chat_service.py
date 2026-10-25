"""Token-scoped agent endpoints for the cdp-bridge webpage chat panel.

These endpoints are called only by mcp_runtime (via X-Internal-Token), not by browsers.
The CDP identity is injected by the authenticated runtime connection; chat frames
never carry credentials. Endpoints enforce:
- CDP client exists / enabled
- requested agent is authorized for the client (agent 侧 cdp_client_id grant)
- client identity is coherent and conversations remain client-owned

网页对话由 CDP 客户端驱动（会话池按 client_id 隔离）；可操作 agent 由 agent 侧
grants 决定（绑定 principal 带该 cdp_client_id param 的 agent）。这与 agent 侧
绑定的 MCP 用户相互独立。

会话归属按 client_id，不按 session_key：session_key 里的 tabId 随标签页关闭失效，
拿它做归属会让用户换标签页后既看不到也续不了自己的历史（数据仍在，只是被滤掉）。
历史属于这个客户端（这个浏览器），不属于某个已经不存在的标签页；客户端之间仍严格隔离。

场景预置走 ``agent.scene_context``：建会话时把稳定身份键（client_id + tab_id）一次性
写进 conversation system_prompt，后续轮次不再重写。当前页面 URL/标题是易变内容，
不进提示词——模型需要时用 web operation=read_page/tabs 现取，避免沿用上一轮的旧快照。
换标签页续聊时 ``_rebind_conv_scene`` 把场景段重绑到本轮所在标签页，避免模型照着
一个已经关掉的 tab_id 操作。
"""
from __future__ import annotations

import asyncio
import json
import os
import traceback
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from loguru import logger

from agent.api import (
    _build_tool_registry,
    _CONV_TASKS,
    _CONV_STOPS,
    _stream_headers,
    _require_ch,
    _normalize_reasoning_effort,
    _reconcile_orphaned_streaming,
    _SUPERVISOR_HINT,
)
from user_platform.node_client.approvals import ApprovalDenied, ApprovalTimeout
from agent.config import AgentConfig
from agent.context_manager import rebuild_history_messages
from agent.turn_writer import ConversationTurnWriter
from agent import conversation_hub, conversation_store, conversation_state, scene_context, run_state


router = APIRouter(prefix="/agent/client", tags=["agent-client"])


def _require_internal(authorization: str | None, internal_token: str | None) -> None:
    expected = os.environ.get("AGENT_INTERNAL_TOKEN", "")
    if not expected or not internal_token or internal_token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


async def _require_chat_client(client_id: str | None) -> list[int]:
    """校验发起对话的 CDP 客户端存在且已启用，返回其**可操作** agent_ids。

    网页对话改由 CDP 客户端驱动：门槛是「client 启用且可操作 agent 非空」，
    不再依赖 mcp_users.chat_enabled。返回集 = 绑定 principal 带该 cdp_client_id
    param 的 agent（唯一真相源，见 mcp_plugin_store.list_agents_for_cdp_client）。
    与面板 chat_list_agents 同源，避免「列表里没有但仍能发消息」。
    """
    import mcp_plugin_store

    cid = (client_id or "").strip()
    if not cid:
        raise HTTPException(status_code=401, detail="missing cdp client identity")
    # 存在性探针（None=客户端不存在/已停用），与「存在但没授权任何 agent」区分开：
    # 前者 401（身份无效），后者 403（无权操作）。
    if await mcp_plugin_store.get_cdp_client_agent_ids(cid) is None:
        raise HTTPException(status_code=401, detail="invalid cdp client identity")
    agents = await mcp_plugin_store.list_agents_for_cdp_client(cid)
    agent_ids = [int(a["id"]) for a in agents]
    if not agent_ids:
        raise HTTPException(status_code=403, detail="no operable agent for this client")
    return agent_ids


async def _require_client_agent(agent_ids: list[int], agent_id: int) -> dict:
    from db import PostgresClient

    agent = await PostgresClient.get_agent(agent_id)
    if not agent or not agent.get("enabled"):
        raise HTTPException(status_code=404, detail="agent not found")
    if agent_id not in agent_ids:
        raise HTTPException(status_code=403, detail="agent is not allowed for this client")
    return agent


class ClientGoalConfig(BaseModel):
    """goal 模式配置，与 agent.api.GoalConfig 同形。"""

    objective: str = ""
    budget_seconds: int = Field(default=900, ge=60)
    max_turns: int | None = Field(default=None, ge=1)


class ClientRalphConfig(BaseModel):
    """ralph 模式配置，与 agent.api.RalphConfig 同形。"""

    objective: str = ""
    max_rounds: int = Field(default=16, ge=1, le=64)


class ClientSendMessageRequest(BaseModel):
    agent_id: int
    content: str = Field(..., min_length=1)
    tab_id: int | str | None = None
    url: str = ""
    title: str = ""
    conversation_id: str = ""
    max_turns: int | None = Field(default=None, ge=1)
    # 对话级覆盖（面板输入区选择）。None = 本轮不改，沿用会话已存值/Agent 默认。
    model: str | None = None
    reasoning_effort: str | None = None
    # auto：与 interact 同执行流程，但审批全部自动放行（不弹审批卡）。
    # supervisor：监督模式（主 agent 不下场干活，只拆解→派子→验收→返工）。
    mode: Literal["interact", "plan", "goal", "ralph", "auto", "supervisor"] = "interact"
    goal_config: ClientGoalConfig | None = None
    ralph_config: ClientRalphConfig | None = None


class ClientCreateConversationRequest(BaseModel):
    agent_id: int
    title: str = ""


class ClientApprovalRequest(BaseModel):
    """审批裁决，与 agent.api.ApprovalRequest 同形。command_hash 防止批准到被改写的代码。"""

    result: str  # "allow" | "deny"
    command_hash: str = ""


@router.get("/agents")
async def client_list_agents(
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """面板可选 Agent 列表：该 CDP 客户端（经 grants）可操作的启用 Agent。

    仅 mcp_runtime 经 X-Internal-Token 调用；客户端不存在/停用 → 401，
    未授权任何 agent → 403。
    """
    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    import mcp_plugin_store

    agents = await mcp_plugin_store.list_agents_for_cdp_client(client_id)
    return {"agents": agents, "client": {"id": client_id}}


@router.get("/models")
async def client_list_models(
    agent_id: int,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """面板模型下拉：该 agent 绑定的网关 key 白名单内可用模型（含自定义组）。

    CDP 无登录 caller：不能走 _resolve_caller_api_key 的归属校验，直接按 agent
    记录里的 main_api_key_id 取明文 key，再按该 key 的白/黑名单过滤模型列表。
    过滤/说明拼装口径与 agent/api.py:chat_available_models 一致。
    """
    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    agent_ids = await _require_chat_client(client_id)
    agent = await _require_client_agent(agent_ids, int(agent_id))

    from db import PostgresClient
    from agent.api import _model_description
    from rate_limiter import ModelClientPool

    default_model = (agent.get("main_model") or agent.get("model") or "").strip()
    api_key_id = agent.get("main_api_key_id")

    # 取该 key 的明文：复用 gateway key 解析，caller=None 走管理员上下文全量查找。
    api_key = None
    if api_key_id and PostgresClient.pool:
        from agent.api import _resolve_caller_api_key

        try:
            resolved = await _resolve_caller_api_key(int(api_key_id), None)
            api_key = resolved["key"]
        except Exception:  # noqa: BLE001 — key 不可用时不阻断面板，退化为不带 key 的目录
            api_key = None

    # 没绑 key / key 不可用时也走同一条收口：拿到的是未按 key 收窄但已按可用性与
    # 内部 id 过滤的目录，绝不把 agent 行里的 default_model 原样回显——那个字段
    # 不受渠道禁用与 key 名单约束，直接回显会让面板列出跑不通的模型。
    resp = await ModelClientPool.get_models_response(api_key=api_key)
    out: list[dict] = []
    for m in resp.get("data", []) if isinstance(resp, dict) else []:
        item = dict(m)
        item["description"] = _model_description(item)
        out.append(item)

    # default_model 只在它确实出现在过滤后的列表里才回给面板，避免下拉预选一个
    # 已被禁用或已被 key 拉黑的模型。
    if default_model and not any(m.get("id") == default_model for m in out):
        default_model = ""
    return {"data": out, "default_model": default_model}


@router.get("/skills")
async def client_list_skills(
    agent_id: int,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """面板 `/` 技能列表：与用户端 GET /agent/agents/{id}/skills 同口径。

    运行时聚合 SOP 目录 + 生效 MCP 服务，只回名字/描述；正文在 turn 组装时
    按需内联。CDP 无登录 caller，鉴权靠 internal token + client 可操作集。
    """
    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    agent_ids = await _require_chat_client(client_id)
    agent = await _require_client_agent(agent_ids, int(agent_id))

    from agent.skill_slash import build_catalog

    catalog = await build_catalog(agent_id, mcp_user_id=agent.get("mcp_user_id"))
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


def _sign_event_media(event: dict) -> None:
    """Add short-lived signed URLs to transient CDP media (question + tool_result).

    The URL is attached only to the SSE event sent to the extension. Persisted
    media parts are signed again when conversation history is read, so an
    expiring URL is never stored as durable message data.

    Handles both shapes (与 _collect_attachment_media 对称)：
    - 结果自身是附件（``kind=attachment`` 挂在 ``data``）—— show_file 形状。
    - 结果带 ``media`` 列表（每项一个附件形状）—— ask_user 形状。
    """
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    candidates: list[Any] = [data]
    if isinstance(data.get("media"), list):
        candidates.extend(data["media"])
    try:
        import attachment_signing
    except Exception:  # noqa: BLE001 - signing is optional for the panel
        return
    for item in candidates:
        if not isinstance(item, dict) or item.get("kind") != "attachment":
            continue
        attachment_id = item.get("id") or item.get("attachment_id")
        try:
            attachment_id = int(attachment_id)
        except (TypeError, ValueError):
            continue
        if attachment_id <= 0:
            continue
        try:
            signed = attachment_signing.sign_attachment_url(attachment_id, "content")
        except Exception:  # noqa: BLE001 - a missing signing key is a soft fallback
            signed = None
        if signed:
            item["content_url"] = signed


async def _run_cdp_conversation_turn(
    *,
    conv: dict,
    conv_id: str,
    assistant_msg_id: int,
    agent: dict,
    scene,
    agent_id: int,
    user_input: str,
    mode: str | None,
    goal_config,
    ralph_config,
    max_turns: int | None,
    effective_effort: str | None,
    conv_model: str,
    client_id: str = "",
    resume_from: dict | None = None,
) -> dict[str, Any]:
    """CDP 网页对话一轮 agent 运行：事件走 conversation_hub 广播，立即返回。

    client_send_message 与 client_retry_message 共用：前者新建 user+assistant 占位后
    调用，后者复用既有失败 assistant 消息（reset 为 streaming）后调用。实时事件由
    订阅该会话的各条通道消费（本会话的发起标签页、同 client 的其它标签页、控制台、
    手机），而不再是本次响应的单消费者。

    ``resume_from``（重试续跑）：传入被重试 assistant 消息的**未清空**行。续跑把
    它已完成的工具调用与结果原样保留——既作为模型上下文（模型看到自己做到哪了，
    不会把 30 个 web 调用从头重跑），也作为 ``ConversationTurnWriter`` 的种子
    （收尾落库时旧进度 + 新进度一起写回，不会丢）。非续跑路径行为不变。
    """
    # 一个 turn 的事件流 → 投影 + 事件日志的唯一实现（见 agent/turn_writer.py）。
    # CDP 原先自己抄了一份 on_event/checkpoint 累积器，与 agent/api.py 已经漂移：
    # 完全没有子 agent 聚合（卡片刷新即丢、checkpoint 也不写 subagents）。改接
    # writer 后 CDP 与 Web 走同一段代码，两处缺陷一并消失。
    # 本回合的协作式停止信号：注入 tools.context 后由 agent_loop 每轮检查、
    # code_run 等子进程时赛跑。abort 端点置位它 + task.cancel() 双保险。
    stop_event = asyncio.Event()
    _CONV_STOPS[conv_id] = stop_event

    # conversation_id 不再经事件流首帧下发：send 的 JSON 响应直接携带它，
    # 面板/网关凭它登记会话订阅，事件序列里不再需要这一帧。

    # 网页对话没有 C 端 session，只有 X-Cdp-Client-Id：反查客户端所属用户，
    # 作为本轮 AI 副作用（如建定时任务）的归属人与 ask_user 附件的认领人。
    # 失败留 None——add_job 兜底会按 agent_id 查 agents.user_id；都拿不到才落
    # NULL（用户态不可见）。在 on_event 之前解析：question 事件的附件认领要用。
    client_owner: str | None = None
    if client_id:
        try:
            import mcp_plugin_store
            client_owner = await mcp_plugin_store.get_cdp_client_owner_user_id(client_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"解析 CDP 客户端 {client_id} owner 失败: {exc}")

    # 解析完 client_owner 之后再建 writer：附件认领归属人（client_owner）与瞬态
    # 签名器（CDP 独有：媒体直链只进 SSE 不落库）都是构造参数。
    writer = ConversationTurnWriter(
        conversation_id=conv_id,
        assistant_msg_id=assistant_msg_id,
        resume_from=resume_from,
        attachment_owner=client_owner,
        sign_media=_sign_event_media,
        turn_number=0,
    )

    async def on_event(event: dict):
        """一轮事件 → 累积/记帧/落库统一交给 writer，这里只做广播与运行状态翻译。

        与 agent/api.py 完全同一段实现（原先各抄一份已漂移）。writer.handle 返回已
        富化（message_id + offset）的帧，发起标签页与旁观端消费同一条序列。
        """
        out = await writer.handle(event)
        await conversation_hub.publish(conv_id, out)
        # 会话运行状态：把 agent 事件翻译成状态枚举，更新 ConversationState
        # （改内存 → 写穿 run_status/run_status_info → 推 conversation_state 事件）。
        # 三端（CDP/Web/App）据此同步底栏。高频 delta 由 update 的字段去重短路。
        try:
            from agent import run_state as _run_state
            await _run_state.apply(conv_id, out)
        except Exception as exc:  # noqa: BLE001 — 状态显示失败绝不影响对话
            logger.debug("[cdp] run_state failed conv=%s: %s", conv_id, exc)

    async def run_agent():
        ga = None
        try:
            from agent.agent_main import GenericAgent
            from agent.agent_loop import agent_runner_loop, BaseHandler as _BH

            agent_system_prompt = agent.get("system_prompt") or ""
            # Agent LLM 只走网关。配置注入（Phase 2.1）：agent 行已由 _require_client_agent
            # 读过（SELECT *），传 agent_row 让 _ensure_config 直接派生 config（含
            # main_api_key_id/main_model），不再每轮重复 SELECT。owner/网关列齐全。
            # scene=：让 _ensure_resources 把 cdp-bridge 的完整方法/参数说明内联进
            # 首轮系统提示，而不是只给一个还要 file_read 的 SOP 指针。
            # conversation_id=：会话血缘透传 → request_logs.session_id，日志页按会话串请求。
            ga = GenericAgent(
                agent_id=agent_id,
                tools=_build_tool_registry(agent_id, owner_user_id=client_owner),
                scene=scene,
                conversation_id=str(conv_id or ""),
                agent_row=agent,
            )
            config = await ga._ensure_config()
            # 轮数上限的唯一真相源：请求级 max_turns（本次覆盖）> Agent 行 max_turns
            #（config 已解析：行没配 → DEFAULT_MAX_TURNS）> 固定默认。handler 与所有
            # agent_runner_loop 调用都用这个变量，杜绝「handler 与 loop 两套 or 链、
            # 值可能不一致」的 bug。
            eff_max_turns = max_turns or config.max_turns

            # 场景段建会话时已写进 conv.system_prompt，这里直接读，不再每轮重拼。
            effective_system_prompt = (conv.get("system_prompt") or "").strip() or agent_system_prompt or ""

            # 历史重建走 rebuild_history_messages：把落库的展示态 tool_calls
            # 归一成 OpenAI 线形状并补出配对的 role=tool，与在途轮次同形。
            history = await conversation_store.get_messages(conv_id)
            if resume_from is not None:
                # 续跑：不跳过被重试的那条 assistant——它已完成的 tool_calls/结果
                # 要作为上下文回带给模型（否则模型看不到自己做到哪了，从头重跑）。
                # rebuild 把这条当「已完成轮次」重放（只 replay done 的调用），末尾
                # 补一条 user「继续」指令——agent_runner_loop 在给了 initial_messages
                # 时不会再自动追加 user_input，这条必须显式加，否则模型没有续跑信号。
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

            ga.set_event_sink(on_event)
            llm = await ga._ensure_llm()
            # 对话级覆盖：复用主 Agent 网关 key，仅换模型/思考等级（fork 保留 key，
            # 计费/归属仍归主 key）。口径与 agent/api.py send_message 一致。
            if conv_model and conv_model != getattr(llm, "model", None):
                llm = llm.fork(model=conv_model)
            if effective_effort is not None:
                llm = llm.fork(
                    thinking_enabled=bool(effective_effort),
                    reasoning_effort=effective_effort,
                )
            effective_model = getattr(llm, "model", "") or ""
            # 技能激活（/名字 标记）：与用户端 agent/api.py 同口径——只看当前这条
            # 消息（不扫历史），命中后把技能基础信息（名字、说明、正文位置）追加
            # 到这条用户消息本身；不进 system prompt、不逐轮注入。失败只降级为
            # 无技能，不阻断网页对话。
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
                logger.warning("[skill] activation failed agent=%s conv=%s: %s", agent_id, conv_id, exc)
            tools, resource_block = await ga._ensure_resources()
            # 协作式停止信号注入：必须在这里（_ensure_resources 之后）——它会经
            # _ensure_tools 新建 ToolRegistry/ToolContext，早设会被那次重建抹掉。
            # 之后到 loop 结束再无 context 重建点。
            tools.context.stop_event = stop_event
            tools.context.conversation_id = conv_id
            # auto 执行模式：会话运行时状态的派生属性。审批批次传**取值回调**而非快照
            # bool —— 回合中途用户切 auto，同回合后续审批立即走 auto（历史 bug 是这里
            # 收 bool，切档只影响下一回合）。实例已在 send 前 load/入列，这里兜底：
            # 已在内存则直接返回、不打穿（send 前 update 进去的活状态就是依据）。
            _cstate = await conversation_state.load(conv_id, conv)
            if (mode or "").strip().lower() and (mode or "").strip().lower() != _cstate.mode:
                await conversation_state.update(conv_id, mode=mode)
                _cstate = conversation_state.peek(conv_id) or _cstate
            auto_approve = _cstate.auto_approve
            is_auto = lambda: bool((conversation_state.peek(conv_id) or _cstate).auto_approve)
            # 越界写入审批：与用户端 agent/api.py 同口径，且与 code_run 的开关解耦——
            # 「浏览器对话不能用 code_run」不代表「可以静默越界写文件」，所以无论
            # browser_code_run_enabled 是否打开，file_write/file_patch 的沙箱边界都走
            # 审批卡（工作区内写入不经过它）。
            from agent.file_write_approval import FileWriteApprovalBatch

            file_write_batch = FileWriteApprovalBatch(
                conversation_id=conv_id,
                agent_id=int(agent_id or 0),
                caller=client_owner,
                emit=on_event,
                security=tools.context.security,
                timeout_seconds=int(getattr(config, "approval_timeout_seconds", 0) or 0),
                auto_approve=is_auto,
            )
            tools.set_file_write_approval(file_write_batch)

            # 工作流定义写审批：与用户端 agent/api.py 同口径——create/update/delete
            # 弹一张卡（图能执行 code_run/写文件/驱动浏览器，改图=授权持久自动化）。
            # 读与运行态不经过它；无人值守入口不挂协调器 → fail closed。
            from agent.workflow_approval import WorkflowApprovalBatch

            workflow_batch = WorkflowApprovalBatch(
                conversation_id=conv_id,
                agent_id=int(agent_id or 0),
                caller=client_owner,
                emit=on_event,
                timeout_seconds=int(getattr(config, "approval_timeout_seconds", 0) or 0),
                auto_approve=is_auto,
            )
            tools.set_workflow_approval(workflow_batch)

            # code_run 审批协调器：与用户端 agent/api.py send_message 同口径。
            # Agent 级开关（agents.browser_code_run_enabled，默认关）没开时不挂
            # 协调器，code_run 直接 denied 并在文案里提示开启配置——开关的语义是
            # 「浏览器对话根本不能用 code_run」，不是「能用但要审批」。
            code_run_batch = None
            if bool(agent.get("browser_code_run_enabled")):
                from agent.code_run_approval import CodeRunApprovalBatch

                approval_timeout = int(getattr(config, "approval_timeout_seconds", 0) or 0)
                code_run_batch = CodeRunApprovalBatch(
                    conversation_id=conv_id,
                    agent_id=int(agent_id or 0),
                    # requester 落 CDP 客户端所属用户；拿不到留 None（审计里显示未知发起人）。
                    caller=client_owner,
                    emit=on_event,
                    timeout_seconds=approval_timeout,
                    auto_approve=is_auto,
                )
                tools.set_code_run_approval(code_run_batch)
            else:
                tools.set_code_run_denial(
                    "code_run 在浏览器对话中被禁用：该 Agent 未开启「允许浏览器执行 code_run」。"
                    "请用户在用户端 Agent 配置页（安全设置）打开该开关后再重试；"
                    "在此之前请改用其它方式完成任务，不要反复调用 code_run。"
                )

            async def _prepare_tool_batch(tool_calls: list[dict]) -> None:
                if code_run_batch is not None:
                    await code_run_batch.prepare(tool_calls)
                await file_write_batch.prepare(tool_calls)
                await workflow_batch.prepare(tool_calls)

            # 注入 GA 的 L1 memory index / [Available Capabilities] / SOP 指引，
            # 与用户端 agent/api.py send_message 同口径——CDP 路径此前漏了，
            # 导致 agent 没见过 browser_sop.md，跳转页面只会直接 browser_navigate。
            if resource_block:
                effective_system_prompt = f"{effective_system_prompt}\n\n{resource_block}".strip()
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})

            # GA 执行模式：plan/goal 与用户端 Agent 聊天页同源（agent/api.py）。
            mode_eff = (mode or "interact").lower()
            goal_state_file = ""
            if mode_eff == "plan":
                from agent.plan_mode import PlanModeManager

                plan_mgr = PlanModeManager(int(agent_id or 0))
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
                if goal is None or not (goal.objective or "").strip():
                    raise HTTPException(400, "goal mode requires goal_config.objective")
                from agent.guardian import Guardian

                guardian = Guardian(agent_id=int(agent_id or 0))
                await guardian.start_goal_mode(
                    objective=goal.objective,
                    budget_seconds=goal.budget_seconds,
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
                if ralph is None or not (ralph.objective or "").strip():
                    raise HTTPException(400, "ralph mode requires ralph_config.objective")
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
                # 监督模式：与用户端 agent/api.py 同源，红线常量共用防漂移；
                # 不加驱动循环（spawn 前台一次性，模型按红线自己拆解/派子/验收）。
                effective_system_prompt = (effective_system_prompt or "") + _SUPERVISOR_HINT
                if initial_messages and initial_messages[0].get("role") == "system":
                    initial_messages[0]["content"] = effective_system_prompt
                else:
                    initial_messages.insert(0, {"role": "system", "content": effective_system_prompt})

            handler = _BH(tools_registry=tools, max_turns=eff_max_turns)

            final_content = ""
            async for item in agent_runner_loop(
                llm,
                system_prompt=effective_system_prompt,
                user_input=user_input,
                handler=handler,
                tools_schema=tools.get_schema(),
                max_turns=eff_max_turns,
                on_event=on_event,
                on_tool_batch=_prepare_tool_batch,
                initial_messages=initial_messages if initial_messages else None,
                auto_compact=getattr(config, "auto_compact", True),
            ):
                final_content = item.get("data", final_content)

            # Goal 模式：由 Guardian 驱动续跑，直到它给出一次收尾提示。
            if mode_eff == "goal":
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
                        on_tool_batch=_prepare_tool_batch,
                        initial_messages=None,
                        auto_compact=getattr(config, "auto_compact", True),
                    ):
                        final_content = item.get("data", final_content)
                    if wrapped_up:
                        break
                # Independent verification before marking done (see agent/api.py
                # for rationale). A fresh agent re-checks the wrap-up claims by
                # attempting to refute them; refuted → the problems are appended
                # to the final message so the user sees the goal was not
                # independently confirmed.
                try:
                    from agent.ralph import verify_goal_completion
                    ver = await verify_goal_completion(
                        ga, goal.objective, [str(final_content or "")],
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
                except Exception:  # noqa: BLE001 — verification must never block completion
                    pass
                try:
                    await guardian.mark_goal_done(budget_exhausted=wrapped_up)
                    from agent.file_memory import agent_root

                    state = await guardian.get_goal_status()
                    if state and goal_state_file:
                        gpath = agent_root(int(agent_id)) / goal_state_file
                        gpath.parent.mkdir(parents=True, exist_ok=True)
                        gpath.write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
                except Exception:  # noqa: BLE001 — 收尾镜像失败不影响对话结果
                    pass

            # Ralph 模式：由 Guardian 驱动「每轮全新」的有限轮次（见 agent/api.py
            # 同名分支的说明）。每轮一个新 handler —— 不继承任何上下文，只有目标
            # 和上一轮的结构化交接（这是 ralph 与 goal 的本质差别）。
            if mode_eff == "ralph":
                ralph_rounds = 0
                ralph_report: dict | None = None
                ralph_outcome = "max_rounds"
                while True:
                    step = await guardian.next_ralph_round()
                    if step is None:
                        break
                    ralph_rounds = step["round"]
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
                        on_tool_batch=_prepare_tool_batch,
                        initial_messages=None,
                        auto_compact=getattr(config, "auto_compact", True),
                    ):
                        round_text = item.get("data", round_text)
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
                    await guardian.record_ralph_report(ralph_report)
                    if ralph_report["status"] in ("complete", "blocked"):
                        ralph_outcome = ralph_report["status"]
                        break
                try:
                    from agent.ralph import format_round_outcome
                    final_content = (
                        (final_content or "") + "\n\n"
                        + format_round_outcome(ralph_outcome, ralph_rounds, ralph_report)
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await guardian.mark_ralph_done(outcome=ralph_outcome)
                    from agent.file_memory import agent_root

                    state = await guardian.get_ralph_state()
                    if state and goal_state_file:
                        gpath = agent_root(int(agent_id)) / goal_state_file
                        gpath.parent.mkdir(parents=True, exist_ok=True)
                        gpath.write_text(json.dumps(state, ensure_ascii=False, default=str), encoding="utf-8")
                except Exception:  # noqa: BLE001 — 收尾镜像失败不影响对话结果
                    pass

            # ask_user 退出时 agent_runner_loop yield 的 data 是 {status:"question",...}
            # dict 而非正文文本——直接写 content 会把整个 dict 落进字符串列。与
            # agent/api.py 同口径：正文用累积全文；EXITED-question 的问题文本已由
            # writer 的 question 分支并入正文，dict 在这里只作废。
            final_text = final_content if isinstance(final_content, str) else ""
            await conversation_store.update_message(
                assistant_msg_id,
                content=writer.content or final_text,
                tool_calls=writer.tool_calls or None,
                tool_results=writer.tool_results or None,
                media=writer.media or None,
                status="done",
                model=effective_model,
                usage=writer.usage or None,
                reasoning=writer.reasoning,
                # 子 agent 卡片随消息落库：CDP 原先根本不聚合，刷新后子 agent 全丢。
                subagents=writer.subagents or None,
            )
            await conversation_hub.publish(conv_id, {"type": "end"})
        except asyncio.CancelledError:
            await conversation_store.update_message(
                assistant_msg_id, status="error", error="已中止", reasoning=writer.reasoning,
                media=writer.media or None, subagents=writer.subagents or None,
            )
            # 走 on_event 同一条链路（publish + run_state.apply）：aborted 映射
            # STATUS_ABORTED，随后的 end 收敛到 WAITING_INPUT。直接 publish 裸
            # 事件会绕过 run_state，状态徽章停在「处理中」。收尾是尽力而为。
            try:
                await on_event({"type": "aborted", "message": "已中止"})
                await on_event({"type": "end"})
            except Exception:  # noqa: BLE001 — 收尾失败不能盖掉中止本身
                pass
        except ApprovalDenied:
            # 用户主动拒绝不是故障：正常收尾，不发 error，也绝不把 denied 工具结果
            # 喂回模型继续跑。审批卡已由面板自己翻成「已拒绝」。on_tool_batch 在
            # tool_call 事件之前抛，所以没有悬空的 running 工具卡。
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
                assistant_msg_id, status="error", error=error_text, reasoning=writer.reasoning,
                media=writer.media or None, subagents=writer.subagents or None,
            )
            await conversation_hub.publish(conv_id, {"type": "error", "message": error_text})
            await conversation_hub.publish(conv_id, {"type": "end"})
        finally:
            # A chat turn owns CDP tabs through its per-agent MCP manager. Release
            # deterministically on normal completion, abort, or task failure; TTL
            # remains the fallback for a hard process crash.
            try:
                manager = getattr(ga, "_mcp_manager", None)
                if manager is not None:
                    await manager.close()
            except Exception:
                pass

    task = asyncio.create_task(run_agent())
    _CONV_TASKS[conv_id] = task
    # busy 标记：回合进行中禁止空闲 sweep 回收实例（turn 内审批读活状态依赖它）。
    conversation_state.set_busy(conv_id, True)
    # _CONV_TASKS 的摘除跟着任务生命周期（旧实现挂在 SSE generator 的 finally，
    # 现在 POST 不再持有连接）。
    task.add_done_callback(
        lambda t, cid=conv_id: _on_turn_done(cid, t)
    )
    return {"conversation_id": conv_id, "assistant_message_id": assistant_msg_id, "status": "streaming"}


def _on_turn_done(conv_id: str, t: asyncio.Task) -> None:
    """回合任务结束：摘除 _CONV_TASKS（本任务仍是当前任务时才摘）+ 清 busy 标记。"""
    if _CONV_TASKS.get(conv_id) is t:
        _CONV_TASKS.pop(conv_id, None)
    # 停止信号与任务同生命周期回收，避免 _CONV_STOPS 随会话数增长泄漏。
    _CONV_STOPS.pop(conv_id, None)
    conversation_state.set_busy(conv_id, False)


@router.post("/messages")
async def client_send_message(
    request: ClientSendMessageRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    x_cdp_client_alias: str | None = Header(None, alias="X-Cdp-Client-Alias"),
    x_cdp_session_key: str | None = Header(None, alias="X-Cdp-Session-Key"),
    authorization: str | None = Header(None),
):
    """Create (or continue) a conversation and stream agent events as SSE.

    Each event is `data: <json>\\n\\n`. The first event may be
    `{"type":"conversation","conversation_id":...}` so the extension can
    keep the conversation for subsequent turns / abort.
    """
    _require_ch()
    from db import PostgresClient

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    client_alias = (x_cdp_client_alias or "").strip()
    session_key = (x_cdp_session_key or "").strip()
    if not client_id or not session_key or not session_key.startswith(f"{client_id}:"):
        raise HTTPException(status_code=403, detail="invalid cdp client/session identity")
    agent_ids = await _require_chat_client(client_id)
    agent = await _require_client_agent(agent_ids, int(request.agent_id))

    # 场景由服务端按入参归一（client_id + tab_id 是稳定身份键），提示词只在建会话时
    # 写一次；当前页面 URL/标题是易变内容，不进提示词，模型需要时用 web read_page/tabs 现取。
    scene = scene_context.normalize(cdp={
        "client_id": client_id,
        "session_key": session_key,
        "client_alias": client_alias,
    })

    # 本轮生效的对话级覆盖：面板显式给了就落库，没给就沿用会话已存值。
    turn_model = (request.model or "").strip()
    turn_effort: str | None = (
        _normalize_reasoning_effort(request.reasoning_effort)
        if request.reasoning_effort is not None
        else None
    )

    conv_id = (request.conversation_id or "").strip()
    if conv_id:
        conv = await conversation_store.get_conversation(conv_id)
        if not conv:
            raise HTTPException(status_code=404, detail="conversation not found")
        if conv.get("agent_id") != request.agent_id:
            raise HTTPException(status_code=403, detail="conversation agent mismatch")
        ownership = _require_conv_owned_by_client(conv, client_id)
        # system_prompt 不逐轮重写：场景身份键建会话时已写定。
        # 分两条写路径（身份段 / 运行时状态），都在本轮 await 完：
        #   1. 换标签页的场景身份段重绑 —— 直写 DB（身份段不进注册表，见
        #      conversation_state 模块注释）。
        #   2. model/mode/思考等级 —— 会话注册表唯一写入口（内存 → 写穿 → 广播）。
        # 顺序上重绑先行：注册表写穿会从 DB 现读 chat_settings 合并，读到的已是
        # 重绑后的新场景键，不会互相覆盖。
        rebind_fields: dict[str, Any] = {}
        ownership = _rebind_conv_scene(
            conv, ownership,
            client_id=client_id,
            session_key=session_key,
            client_alias=client_alias,
            update_fields=rebind_fields,
        )
        if rebind_fields:
            await conversation_store.update_conversation(conv_id, **rebind_fields)

        _cstate = await conversation_state.load(conv_id, conv)
        if turn_model and turn_model != _cstate.model:
            await conversation_state.update(conv_id, model=turn_model)
        if turn_effort is not None:
            await conversation_state.update(conv_id, reasoning_effort=turn_effort)
        if request.mode and request.mode != _cstate.mode:
            await conversation_state.update(conv_id, mode=request.mode)
        if turn_model:
            conv["model"] = turn_model
    else:
        chat_settings: dict[str, Any] = scene_context.persist(scene)
        if turn_effort is not None:
            chat_settings["reasoning_effort"] = turn_effort
        conv = await conversation_store.create_conversation(
            title=f"CDP:{request.title or request.url or '网页对话'}"[:60],
            system_prompt=scene_context.append_prompt(agent.get("system_prompt"), scene),
            model=turn_model or agent.get("model") or "",
            agent_id=request.agent_id,
            chat_settings=chat_settings,
            user_id=str(agent.get("user_id")) if agent.get("user_id") else None,
        )
        conv_id = conv["id"]
        ownership = chat_settings
        # 建会话即入列注册表（= 实例添加的显式时机）。
        conversation_state.register(conv)

    # 本轮 fork 用的思考等级/模型：以注册表活状态为准（本轮显式覆盖已 update 进去；
    # 存过 reasoning_effort 才生效，None = 跟随 agent 默认）。
    _cstate = conversation_state.peek(conv_id)
    if turn_effort is not None:
        effective_effort = turn_effort
    elif _cstate is not None and _cstate.reasoning_effort is not None:
        effective_effort = _normalize_reasoning_effort(_cstate.reasoning_effort)
    else:
        effective_effort = None
    conv_model = ((_cstate.model if _cstate else "") or (conv.get("model") or "")).strip()

    existing = _CONV_TASKS.get(conv_id)
    if existing and not existing.done():
        raise HTTPException(status_code=409, detail="a task is already running for this conversation")

    # 用户消息只存用户真正说的话：页面 URL/标题不再前置进正文（那会把易变快照
    # 写进历史，且下一轮就过期）。模型要当前页面时用 web operation=read_page/tabs 现取。
    user_msg = await conversation_store.add_message(conv_id, "user", request.content)
    assistant_msg = await conversation_store.add_message(conv_id, "assistant", "", status="streaming")
    assistant_msg_id = assistant_msg["id"]
    await conversation_store.touch_conversation(conv_id)

    return await _run_cdp_conversation_turn(
        conv=conv,
        conv_id=conv_id,
        assistant_msg_id=assistant_msg_id,
        agent=agent,
        scene=scene,
        agent_id=int(request.agent_id),
        user_input=request.content,
        mode=request.mode,
        goal_config=request.goal_config,
        ralph_config=request.ralph_config,
        max_turns=request.max_turns,
        effective_effort=effective_effort,
        conv_model=conv_model,
        client_id=client_id,
    )


@router.post("/conversations/{conv_id}/messages/{message_id}/retry")
async def client_retry_message(
    conv_id: str,
    message_id: int,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    x_cdp_client_alias: str | None = Header(None, alias="X-Cdp-Client-Alias"),
    x_cdp_session_key: str | None = Header(None, alias="X-Cdp-Session-Key"),
    authorization: str | None = Header(None),
):
    """重试一条失败的 assistant 消息：复位同 id 消息为 streaming，用其前一条
    user 消息内容重跑一轮 agent。与 agent/api.py retry_message 同口径：只允许
    最后一条 assistant 消息，mode 固定 interact。
    """
    _require_ch()

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    client_alias = (x_cdp_client_alias or "").strip()
    session_key = (x_cdp_session_key or "").strip()
    if not client_id or not session_key or not session_key.startswith(f"{client_id}:"):
        raise HTTPException(status_code=403, detail="invalid cdp client/session identity")
    agent_ids = await _require_chat_client(client_id)

    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    agent_id = conv.get("agent_id")
    agent = await _require_client_agent(agent_ids, int(agent_id or 0))
    ownership = _require_conv_owned_by_client(conv, client_id)

    existing = _CONV_TASKS.get(conv_id)
    if existing and not existing.done():
        raise HTTPException(status_code=409, detail="a task is already running for this conversation")

    # 定位目标消息：assistant、error/streaming、且是最后一条 assistant
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
    if target.get("status") not in ("error", "streaming"):
        raise HTTPException(status_code=400, detail="只能重试失败或中断的消息")
    if last_assistant_id != int(message_id):
        raise HTTPException(status_code=400, detail="只能重试最后一条 assistant 消息")

    user_input = ""
    for msg in history:
        if msg["id"] == int(message_id):
            break
        if msg.get("role") == "user":
            user_input = msg.get("content") or ""
    if not user_input.strip():
        raise HTTPException(status_code=400, detail="无法重试：上一条用户消息为空")

    # 续跑而非重放：这条 assistant 已经跑完的 tool_calls/结果保留不清空，重试时
    # 作为模型上下文回传（模型接着中断处做），收尾再把新进度追加到同一条。旧的
    # 「上一条 user 消息」只用于拼接给模型的「继续」指令文本，不清空已做的工作。
    resume_note = "请从刚才中断的地方继续完成任务（前面的工具调用与结果仍在上下文中）。"

    # 复位同一条 assistant 消息为 streaming，但**保留**已完成的正文/工具调用/结果，
    # 让续跑不从头再来。model/usage/reasoning 由本轮重跑覆盖。
    await conversation_store.update_message(
        int(message_id),
        status="streaming",
        error=None,
    )
    await conversation_store.touch_conversation(conv_id)

    # 重试沿用会话已存的模型/思考等级/模式（mode 是会话状态，切了 auto 就该在
    # 重试时生效，而不是硬编码 interact 把用户又拽回审批）。从会话注册表读活状态。
    _cstate = await conversation_state.load(conv_id, conv)
    effective_effort = (
        _normalize_reasoning_effort(_cstate.reasoning_effort)
        if _cstate.reasoning_effort is not None
        else None
    )
    conv_model = (_cstate.model or (conv.get("model") or "")).strip()
    resume_mode = _cstate.mode or "interact"

    # 换标签页后重试同一条消息：场景段要重绑到本轮所在标签页，否则模型照着旧
    # tab_id 去操作一个已经关掉的标签页。与 client_send_message 续会话分支同口径。
    rebind_fields: dict[str, Any] = {}
    _rebind_conv_scene(
        conv, ownership,
        client_id=client_id,
        session_key=session_key,
        client_alias=client_alias,
        update_fields=rebind_fields,
    )
    if rebind_fields:
        await conversation_store.update_conversation(conv_id, **rebind_fields)

    scene = scene_context.normalize(cdp={
        "client_id": client_id,
        "session_key": session_key,
        "client_alias": client_alias,
    })

    return await _run_cdp_conversation_turn(
        conv=conv,
        conv_id=conv_id,
        assistant_msg_id=int(message_id),
        agent=agent,
        scene=scene,
        agent_id=int(agent_id or 0),
        user_input=resume_note,
        mode=resume_mode,
        goal_config=None,
        ralph_config=None,
        max_turns=None,
        effective_effort=effective_effort,
        conv_model=conv_model,
        client_id=client_id,
        resume_from=target,
    )


@router.patch("/conversations/{conv_id}/settings")
async def client_update_conversation_settings(
    conv_id: str,
    body: dict,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """立即生效的对话级设置：mode / model / reasoning_effort。

    用户一改就写进会话注册表并**立即作用于在跑/后续的轮次**——mode/model/思考等级
    不是「下一次发送才生效」的请求参数，是会话状态。切到 auto 时还要把本会话在途
    的 pending 审批一次性放行（用户已经在说「别问了」，不可能等下一轮）。

    写入只经会话注册表 ``conversation_state.update``（改内存 → 写穿 DB → 广播），
    不再直改 chat_settings——绕过内存态正是「切 auto 要等下一轮」的根因。
    """
    _require_ch()
    from user_platform.node_client.approvals import approval_registry

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)

    # 运行时状态统一经注册表写：内存 + 写穿 + 广播，单一写入口。
    runtime: dict[str, Any] = {}
    if "mode" in body:
        mode = str(body.get("mode") or "").strip().lower()
        if mode:
            runtime["mode"] = mode
    if "model" in body:
        model = str(body.get("model") or "").strip()
        if model:
            runtime["model"] = model
    if "reasoning_effort" in body:
        runtime["reasoning_effort"] = _normalize_reasoning_effort(body.get("reasoning_effort"))

    _cstate = await conversation_state.load(conv_id, conv)
    if runtime:
        st = await conversation_state.update(conv_id, **runtime)
        if st is not None:
            _cstate = st

    # 切到 auto：把本会话在途 pending 审批一次性放行——立即生效，不等下一轮。
    # 放行后逐条广播 approval_resolved，让已挂卡的各端把旧卡翻成「已允许」。
    released = 0
    if _cstate.mode == "auto":
        from user_platform.node_client.approvals import publish_approval_resolved

        for action in approval_registry.allow_pending(conv_id):
            await publish_approval_resolved(
                conv_id, action.confirmation_id, "allow", tool_name=action.tool_name,
            )
            released += 1

    return {
        "ok": True,
        "mode": _cstate.mode,
        "model": _cstate.model,
        "reasoning_effort": _cstate.reasoning_effort,
        "released_approvals": released,
    }


@router.post("/conversations/{conv_id}/abort")
async def client_abort_conversation(
    conv_id: str,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """中止本会话正在运行的一轮 agent（协作式停止 + cancel）。

    无活任务时对账残留 streaming 占位，返回 aborted=false。归属只校验 client_id。
    """
    _require_ch()

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)
    task = _CONV_TASKS.get(conv_id)
    if task and not task.done():
        ev = _CONV_STOPS.get(conv_id)
        if ev is not None:
            ev.set()
        task.cancel()
        return {"aborted": True}
    # 没活任务却残留 streaming 占位消息时（进程重启/任务句柄丢失），对账掉它，
    # 否则面板那个「思考中」气泡会一直转。
    await _reconcile_orphaned_streaming(conv_id)
    return {"aborted": False, "detail": "no running task"}


@router.get("/conversations/{conv_id}/approvals")
async def client_list_approvals(
    conv_id: str,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """本会话未裁决的审批（面板刷新/换标签页后恢复审批卡用）。

    与 agent/api.py 的 list_approvals 同口径，但鉴权走 internal token + CDP
    客户端身份。审批是纯事件态、不入库，事件一旦错过就再无来源——面板靠这个
    端点在载入会话时把审批卡恢复成可操作态。
    """
    _require_ch()
    from user_platform.node_client.approvals import approval_registry

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)
    approval_registry.reap_expired()
    pending = [a.to_dict() for a in approval_registry.list_pending(conv_id)]
    return {"approvals": pending}


@router.post("/conversations/{conv_id}/approvals/{confirmation_id}")
async def client_resolve_approval(
    conv_id: str,
    confirmation_id: str,
    body: ClientApprovalRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """批准或拒绝 code_run 的一次性审批。

    与 agent/api.py 的 resolve_approval 同口径，但鉴权走 internal token + CDP
    客户端身份（网页对话没有 C 端 session/cookie）。会话归属由
    _require_conv_owned_by_client 把住；code_run 的 action.node_id=="" 跳过节点门。
    """
    _require_ch()
    from user_platform.node_client.approvals import approval_registry

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)

    action = approval_registry.get(confirmation_id)
    if action is None or action.conversation_id != conv_id:
        raise HTTPException(status_code=404, detail="confirmation not found")
    if action.resolved is not None:
        raise HTTPException(status_code=409, detail=f"confirmation already {action.resolved}")
    # 过期（但还没被 reap）的审批：返回 409「已过期」而不是放行。不加这道闸，
    # 客户端已把卡翻成「已过期」、晚到的一次点击仍能把过期审批批下来。
    if action.is_expired():
        raise HTTPException(status_code=409, detail="confirmation already expired")
    # code_run 在服务端本地执行、node_id=""；CDP 路径不绑节点，归属已由 client_id 把住。
    # 若 action 带了 node_id（理论不会），fail-closed 拒绝——CDP 客户端不该能放行节点命令。
    if action.node_id:
        raise HTTPException(status_code=403, detail="无权操作该节点")
    if not body.command_hash or body.command_hash != action.command_hash:
        raise HTTPException(status_code=409, detail="command hash mismatch")

    result = (body.result or "").strip().lower()
    if result not in {"allow", "deny"}:
        raise HTTPException(status_code=400, detail="result must be allow or deny")

    resolved = approval_registry.resolve(confirmation_id, result)
    if resolved is None:
        raise HTTPException(status_code=409, detail="confirmation already resolved")
    # 广播给本会话全部订阅者：别的标签页/控制台/手机上挂着的同一张卡也要翻状态，
    # 否则它们一直停在 pending，直到流结束被误判过期。
    from user_platform.node_client.approvals import publish_approval_resolved
    await publish_approval_resolved(
        conv_id, confirmation_id, result, tool_name=resolved.tool_name,
    )
    return {"ok": True, "confirmation_id": confirmation_id, "result": result}


def _conv_ownership(conv: dict) -> dict:
    """从对话行取出 chat_settings（兼容字符串 JSON），归一成 dict。"""
    ownership = conv.get("chat_settings") or {}
    if isinstance(ownership, str):
        try:
            ownership = json.loads(ownership)
        except (TypeError, ValueError, json.JSONDecodeError):
            ownership = {}
    return ownership if isinstance(ownership, dict) else {}


def _require_conv_owned_by_client(conv: dict, client_id: str) -> dict:
    """校验会话属于本 CDP 客户端，返回归一后的 chat_settings。

    归属只认 client_id：session_key 里的 tabId 随标签页关闭失效，用它做归属会让
    换标签页后既看不到也续不了自己的历史。客户端之间仍严格隔离。
    """
    ownership = _conv_ownership(conv)
    if ownership.get("cdp_client_id") != client_id:
        raise HTTPException(status_code=403, detail="conversation does not belong to this client")
    return ownership


def _rebind_conv_scene(
    conv: dict,
    ownership: dict,
    *,
    client_id: str,
    session_key: str,
    client_alias: str,
    update_fields: dict[str, Any],
) -> dict:
    """换标签页续聊时把会话的场景段重绑到本轮所在标签页。

    场景段（含 tab_id）建会话时一次性写进 system_prompt。跨标签页续聊时旧 tab_id
    指向一个已经关掉的标签页，模型会照着它去操作——所以这里用当前 session_key 重解
    场景，替换掉已写入的旧段，并把 chat_settings 的 cdp_session_key/alias 更新。

    改动合并进调用方的 ``update_fields``（与模型/思考等级的更新共用一次写库）。
    返回可能已被替换的 ownership dict。
    """
    old_scene = scene_context.from_chat_settings(ownership)
    new_scene = scene_context.normalize(cdp={
        "client_id": client_id,
        "session_key": session_key,
        "client_alias": client_alias,
    })
    if new_scene is None or new_scene == old_scene:
        return ownership

    system_prompt = scene_context.replace_prompt(
        conv.get("system_prompt"), old_scene, new_scene,
    )
    update_fields["system_prompt"] = system_prompt
    conv["system_prompt"] = system_prompt

    # persist() 只给场景键；其余 chat_settings（reasoning_effort 等）原样保留。
    settings = dict(update_fields.get("chat_settings") or ownership)
    settings.update(scene_context.persist(new_scene))
    update_fields["chat_settings"] = settings
    return settings


def _iso_value(value: Any) -> str | None:
    """ClickHouse DateTime64 → ISO 字符串（面板 created_at 用）。"""
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _conv_summary(conv: dict) -> dict:
    """列表用的对话摘要：只透出面板需要的字段，不带内部归属明细。"""
    def _iso(value: Any) -> str | None:
        if value is None:
            return None
        # naive datetime（ClickHouse DateTime64，语义 UTC）补 Z，前端才能按本地时区
        # 显示；否则 `new Date()` 按本地解析，列表时间差 8 小时。
        try:
            if value.tzinfo is None:
                return value.isoformat() + "Z"
            return value.isoformat()
        except AttributeError:
            return str(value)

    return {
        "id": conv.get("id"),
        "title": conv.get("title") or "",
        "agent_id": conv.get("agent_id"),
        "updated_at": _iso(conv.get("updated_at")),
        "created_at": _iso(conv.get("created_at")),
        # 会话级对话参数：面板载入时恢复「当前模式/模型/思考等级」的显示。
        # mode 是会话状态（切换即生效），不是请求参数。
        "mode": _conv_ownership(conv).get("mode") or "interact",
        "model": conv.get("model") or "",
        # 会话运行状态（会话级「最新状态」）：面板加载时据此恢复底栏显示。
        # 返回 {run_status, run_status_info}，None = 上一轮已正常完结（静息态）。
        # 字段名沿用 run_state 以保前端兼容；底层读 run_status/run_status_info 列。
        "run_state": run_state.restore(conv),
    }


@router.get("/conversations")
async def client_list_conversations(
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
    limit: int = 50,
) -> dict:
    """列出本 CDP 客户端名下的会话，最近更新在前。

    归属按 client_id，不按 session_key：Chrome 的 tabId 随标签页关闭失效，按
    session_key 过滤会让关掉标签页的历史会话彻底查不到（数据仍在，只是被滤掉）。
    历史属于这个客户端（这个浏览器），不属于某个已经不存在的标签页。
    """
    _require_ch()

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    rows = await conversation_store.list_conversations_for_cdp_client(
        client_id, limit=max(1, min(limit, 200))
    )
    return {"conversations": [_conv_summary(r) for r in rows]}


@router.post("/conversations")
async def client_create_conversation(
    request: ClientCreateConversationRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    x_cdp_client_alias: str | None = Header(None, alias="X-Cdp-Client-Alias"),
    x_cdp_session_key: str | None = Header(None, alias="X-Cdp-Session-Key"),
    authorization: str | None = Header(None),
) -> dict:
    """显式新建一个空会话，绑定当前页面（client_id+session_key）。"""
    _require_ch()

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    client_alias = (x_cdp_client_alias or "").strip()
    session_key = (x_cdp_session_key or "").strip()
    if not client_id or not session_key or not session_key.startswith(f"{client_id}:"):
        raise HTTPException(status_code=403, detail="invalid cdp client/session identity")
    agent_ids = await _require_chat_client(client_id)
    agent = await _require_client_agent(agent_ids, int(request.agent_id))

    # 场景段必须在这里写入：send_message 不再每轮重写 system_prompt，若这里留空，
    # 经本端点建出来的会话会永远没有场景（模型不知道自己在驱动哪个浏览器/标签页）。
    scene = scene_context.normalize(cdp={
        "client_id": client_id,
        "session_key": session_key,
        "client_alias": client_alias,
    })
    conv = await conversation_store.create_conversation(
        title=f"CDP:{request.title or '新对话'}"[:60],
        system_prompt=scene_context.append_prompt(agent.get("system_prompt"), scene),
        model=agent.get("model") or "",
        agent_id=request.agent_id,
        chat_settings=scene_context.persist(scene),
        user_id=str(agent.get("user_id")) if agent.get("user_id") else None,
    )
    # 建会话即入列注册表（= 实例添加的显式时机）。
    conversation_state.register(conv)
    return {"conversation": _conv_summary(conv)}


@router.get("/conversations/{conv_id}")
async def client_get_conversation(
    conv_id: str,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
    cursor: int | None = None,
    limit: int = 7,
) -> dict:
    """取一个会话的消息历史，供面板切换会话时回放。归属只校验 client_id。

    不校验 session_key：换标签页后要能回看这个浏览器的历史（tabId 随标签页关闭失效）。

    **按用户轮次分页**：默认回最近 ``limit`` 个用户轮次的消息；带 ``cursor``（上一页
    返回的 ``page.next_cursor``，即较新一页最早的 user message id）则取更早的一页。
    面板据此做「滚到顶部加载更早」的翻页，不再一次性把整段历史灌给前端。
    """
    _require_ch()

    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)
    # 载入即对账：进程重启/断线后残留的 streaming 占位（本进程无活任务才翻）
    # 翻成 error，否则刷新面板会看到一条永远转圈、且「刷新进度/停止」都无从下手
    # 的空壳。与 api.py get_conversation_meta 同口径。
    await _reconcile_orphaned_streaming(conv_id)
    page = await conversation_store.get_messages_page_by_user_turns(
        conv_id, limit=max(1, min(int(limit), 50)), cursor=cursor, timeline=True,
    )
    messages = page.get("messages", [])
    from user_platform.node_client.approvals import approval_registry
    return {
        "conversation": _conv_summary(conv),
        "messages": [
            {
                # id 必须回：面板重试要按 id 定位消息，回放要按 id 去重/续看。
                "id": m.get("id"),
                "role": m.get("role"),
                "content": m.get("content") or "",
                "tool_calls": m.get("tool_calls") or [],
                "tool_results": m.get("tool_results") or [],
                # media：ask_user 附件的回放载体（attachment part 经 _sign_media_urls
                # 带免登录签名直链，面板无需登录态即可内联渲染图片）。
                "media": m.get("media") or [],
                "status": m.get("status") or "done",
                "error": m.get("error") or "",
                "created_at": _iso_value(m.get("created_at")),
                # model：本轮实际生效的模型，气泡头显示用（与 web 端同口径）。
                "model": m.get("model") or "",
                # reasoning：思考过程，折叠块渲染（与 web/app 历史回放同口径）。
                # 之前漏了这个字段，CDP 载入历史永远看不到思考，只有 web/app 有。
                "reasoning": m.get("reasoning") or "",
                # usage：本轮 token 用量。原先没回，面板 mapLoadedMessages 读到的
                # 恒为 undefined → 刷新/切会话后用量环归 0（live done 时有值、
                # 载入历史就丢）。服务端消息行本就存了 usage，这里透出即可。
                "usage": m.get("usage") or None,
                # subagents：子 agent 卡片（含各自思考 reasoning）。CDP 原先既不聚合
                # 也不透出这两个环节——子 agent 跑完一整轮，刷新后面板一片空白。
                "subagents": m.get("subagents") or [],
            }
            for m in messages
        ],
        # 翻页：滚到顶部加载更早。next_cursor 为空表示已到最早、没有更多。
        "page": page.get("page", {"next_cursor": None, "has_more": False}),
        # 未裁决的审批（刷新/换标签页后面板据此把审批卡恢复成可操作态）。审批是
        # 纯事件态、不入库，事件一旦错过就再无来源，必须在这里按会话回带。
        "pending_approvals": [a.to_dict() for a in approval_registry.list_pending(conv_id)],
    }


@router.post("/conversations/{conv_id}/attachments")
async def client_upload_conversation_attachment(
    conv_id: str,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """CDP 面板上传附件：落到该会话 Agent 的工作区，返回相对路径。

    与 web 端 ``POST /agent/conversations/{id}/attachments`` 同口径（同一套
    ``_attachment_target``：剥目录 + 白名单字符 + uuid 前缀 + 防穿越）。区别只在
    鉴权：扩展面板没有登录态（web 那个走 get_agent_caller 的 bearer key），这里
    靠 internal token + CDP 客户端归属校验。上传的文件路径随消息正文一起发出，
    Agent 用 file 工具读取——与 web 完全一致的语义。

    请求体是原始文件字节（面板把 File 读成 base64 后由网关解码回字节转发），
    不落任何临时文件。
    """
    _require_ch()
    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)

    from agent.api import _attachment_target, _read_attachment_body

    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)

    raw = await _read_attachment_body(request)
    agent_id = conv.get("agent_id")
    if agent_id:
        from agent.file_memory import agent_root
        from db import PostgresClient

        workspace_root = str(agent_root(int(agent_id)))
        agent_row = await PostgresClient.get_agent(agent_id)
        explicit = str((agent_row or {}).get("workspace_root") or "").strip()
        if explicit and explicit != "agent/workspace":
            workspace_root = explicit
    else:
        workspace_root = "agent/workspace"

    relative, target = _attachment_target(workspace_root, conv_id, filename)
    target.write_bytes(raw)
    return {"path": relative, "filename": target.name, "size": len(raw)}


@router.post("/conversations/{conv_id}/compact")
async def client_compact_conversation(
    conv_id: str,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
    x_cdp_client_id: str | None = Header(None, alias="X-Cdp-Client-Id"),
    authorization: str | None = Header(None),
) -> dict:
    """CDP 面板手动压缩上下文：LLM 摘要历史 → 落 <history_summary> 标记消息。

    与 web 端 POST /agent/conversations/{id}/compact 同口径，区别只在鉴权
    （internal token + client 归属）。供 CDP usage-pop 的「压缩」按钮与
    /compact 文本拦截调用。压缩产物落库，后续轮次 rebuild 以最后一条摘要为
    cutoff，跨轮生效。
    """
    _require_ch()
    _require_internal(authorization, x_internal_token)
    client_id = (x_cdp_client_id or "").strip()
    await _require_chat_client(client_id)
    conv = await conversation_store.get_conversation(conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    _require_conv_owned_by_client(conv, client_id)
    from db import PostgresClient

    agent_id = conv.get("agent_id") or 0
    agent_row = await PostgresClient.get_agent(agent_id) if agent_id else None
    from agent.compact_service import compact_conversation as _compact

    return await _compact(conv_id, agent_id=agent_id, agent_row=agent_row)
