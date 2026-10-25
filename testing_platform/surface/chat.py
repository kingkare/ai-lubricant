"""测试平台对话后端 — dsh 驱动 + SSE（plans/10-dsh-integration.md §五 待做 3）。

路由（挂进 ``surface/api.py`` 的 ``router`` 前缀 ``/api/v1/testing`` 下）：

* ``POST /api/v1/testing/chat/send`` — 一轮对话，SSE 流式回事件
* ``GET  /api/v1/testing/chat/health`` — dsh 运行时探活（SDK 是否装好）

鉴权口径照 ``agent/api.py``：C 端用户走 session cookie、管理员走 Bearer，
两者皆无 → 401。密钥解析走平台 ``api_key_id``（前端**不碰明文 key**）。

SSE 形态与平台 ``/v1/chat/completions`` 兼容——每行 ``data: <json>``：
* 事件（``{"type": "assistant/message"|"tool/..."|"turn/end"|"session/status"}``）
  来自 dsh 的 ``on_notification``（经 ``notification_to_event`` 归一）；
* 终态 ``{"type": "done", "data": {"session_id", "final_response", ...}}``；
* ``{"type": "error", "data": {"error": ...}}``；
* ``data: [DONE]`` 收尾。
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import agent_runtime
from ..agent_runtime import DshRuntimeError

logger = logging.getLogger(__name__)

#: 子路由：相对前缀 ``/chat``，由 :mod:`surface.api` 的 ``router``（前缀
#: ``/api/v1/testing``）include —— 最终挂在 ``/api/v1/testing/chat/*``。
router = APIRouter(prefix="/chat", tags=["testing-chat"])

# ── 鉴权（照 agent/api.py：用户 session cookie / 管理员 Bearer）─────────
async def _resolve_caller(authorization: Optional[str]) -> str:
    """返回 user_id（C 端）或 None（管理员）；皆无 → 401。"""
    try:
        from admin import _current_user_cookie, _get_admin_session
        from user_platform.session import USER_SESSION_COOKIE, session_store

        cookie = _current_user_cookie.get(None)
        if cookie:
            data = await session_store.get(USER_SESSION_COOKIE, cookie)
            if data and data.uid:
                return data.uid
        if authorization and authorization.startswith("Bearer "):
            import time as _time

            admin = await _get_admin_session(authorization[7:].strip())
            if admin is not None and _time.time() - admin.get("ts", 0) < 3600:
                return ""  # admin = 空 user_id（平台级，看全平台 Key）
    except Exception:  # noqa: BLE001 — 鉴权路径不吞异常但也不误伤
        logger.debug("[testing-chat] caller resolve failed", exc_info=True)
    raise HTTPException(status_code=401, detail="未登录")


async def _resolve_api_key(api_key_id: int, caller: str) -> tuple[str, str]:
    """api_key_id → (明文 key, key 名)。用户只看得到自己的（+分组根 Key）。

    平台管理员（caller=""）放宽到全平台任意 key。解析逻辑复用 agent 的
    ``_resolve_caller_api_key``（同一份 user-platform session 语义）。
    """
    try:
        from agent.api import _resolve_caller_api_key

        row = await _resolve_caller_api_key(api_key_id, caller or None)
    except ImportError:
        from db import PostgresClient

        row = await PostgresClient.get_api_key_by_id(api_key_id)
    if not row:
        raise HTTPException(status_code=403, detail="无权使用该 API Key 或 Key 不存在")
    return str(row.get("key") or ""), str(row.get("name") or "")


# ── 请求体 ────────────────────────────────────────────────────────────────
class ChatSendRequest(BaseModel):
    api_key_id: int = Field(..., description="平台 API Key id（前端不碰明文 key）")
    model: Optional[str] = Field(None, description="推理模型；缺省 agent_runtime.DEFAULT_MODEL")
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="对话历史（可空；dsh 用 session_id 维持跨轮上下文）",
    )
    message: Optional[str] = Field(None, description="本轮用户消息（与 messages 二选一）")
    session_id: Optional[str] = Field(
        None,
        description="dsh session id；空则后端起新 session 并在 done 事件里回传",
    )
    project_id: Optional[str] = Field(
        None,
        description="当前测试项目 UUID —— 给 dsh 做上下文，工具调用时它就是 project_id",
    )
    chat_session_id: Optional[str] = Field(
        None,
        description="平台侧会话 id（续聊传）；空且有 project_id 时后端新建会话并在首事件回传",
    )
    persist: bool = Field(True, description="落库会话/消息（best-effort，失败不影响对话）")
    stream: bool = Field(True, description="恒 true（SSE）；false 保留但同样走 SSE 形态")


def _build_prompt(
    messages: list[dict[str, Any]],
    message: Optional[str],
    *,
    project_id: Optional[str] = None,
) -> str:
    """把 OpenAI 形态的历史 + 当前消息压成一段 prompt。

    dsh 用 session_id 记上下文，历史消息主要用于跨 session 的首轮 warm-up
    （首轮时带上最近若干轮）。不做角色 token 拼接 —— dsh 自己处理 system。

    ``project_id`` 前置成一行上下文：能力面的写类方法几乎都收 project_id，
    dsh 拿着它才能把工具调对目标项目。
    """
    parts: list[str] = []
    if project_id:
        parts.append(f"[上下文] 当前测试项目 project_id={project_id}（调用测试平台工具时用这个值）")
    for msg in messages[-6:]:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if isinstance(content, str) and content:
            parts.append(f"[{role}] {content}")
    if message:
        parts.append(message)
    return "\n".join(parts).strip()


@router.post("/send")
async def chat_send(request: ChatSendRequest, authorization: Optional[str] = Header(None)):
    """一轮对话：dsh 驱动 + on_notification → SSE 流（+ best-effort 落库）。

    持久化语义（``persist=True`` 且有 ``project_id`` 时）：
    * 首轮无 ``chat_session_id`` → 新建平台会话（首事件 ``{"type":"session","data":{id}}`` 回传 id）
    * 用户消息 → ``role=user`` 落库；assistant 文本 → ``role=assistant``；
      ``tool/call``/``tool/result`` → ``role=tool``（参数/结果摘要）
    * ``done`` 回写 ``dsh_session_id``（跨轮续接键）
    * 落库失败**绝不影响对话**（best-effort，记日志）
    """
    caller = await _resolve_caller(authorization)
    api_key, _key_name = await _resolve_api_key(request.api_key_id, caller)

    prompt = _build_prompt(request.messages, request.message, project_id=request.project_id)
    if not prompt:
        raise HTTPException(status_code=422, detail="message 或 messages 不能为空")

    config = agent_runtime.build_config(api_key=api_key, model=request.model)

    # ── 平台会话（best-effort）──
    chat_session_id: Optional[str] = None
    dsh_session_id: Optional[str] = request.session_id
    assistant_msg_id: Optional[str] = None
    assistant_text: str = ""
    if request.persist and request.project_id:
        try:
            from ..chat import service as chat_svc

            if request.chat_session_id:
                session = await chat_svc.get_session(request.chat_session_id)
                chat_session_id = str(session.id)
                dsh_session_id = session.dsh_session_id or request.session_id
            else:
                session = await chat_svc.create_session(
                    project_id=request.project_id,
                    model=request.model or agent_runtime.DEFAULT_MODEL,
                    api_key_id=request.api_key_id,
                    created_by=caller or None,
                )
                chat_session_id = str(session.id)
            await chat_svc.append_message(
                chat_session_id, role="user", content=request.message or _build_prompt(request.messages, None),
            )
            assistant_msg = await chat_svc.append_message(
                chat_session_id, role="assistant", content="", status="streaming",
            )
            assistant_msg_id = str(assistant_msg.id)
        except Exception:  # noqa: BLE001 — 落库失败不影响对话
            logger.exception("[testing-chat] persist setup failed (best-effort)")
            chat_session_id = None

    async def event_stream() -> AsyncIterator[str]:
        nonlocal dsh_session_id, assistant_text
        try:
            if chat_session_id is not None:
                yield f"data: {json.dumps({'type': 'session', 'data': {'id': chat_session_id}}, ensure_ascii=False)}\n\n"
            async for event in agent_runtime.stream_chat(
                prompt=prompt,
                config=config,
                session_id=dsh_session_id,
            ):
                if chat_session_id is not None:
                    await _persist_event(chat_session_id, event)
                    if event.get("type") == "done":
                        data = event.get("data") or {}
                        if data.get("session_id"):
                            dsh_session_id = str(data["session_id"])
                            await _safe_attach(chat_session_id, dsh_session_id)
                        if data.get("final_response"):
                            assistant_text = str(data["final_response"])
                            await _safe_update(assistant_msg_id, assistant_text, "done")
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            yield "data: [DONE]\n\n"
        except DshRuntimeError as exc:
            await _safe_update(assistant_msg_id, assistant_text, "error")
            err = {"type": "error", "data": {"error": str(exc)}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001 — SSE 兜底，不 500 掉整条流
            logger.exception("[testing-chat] stream failed")
            await _safe_update(assistant_msg_id, assistant_text, "error")
            err = {"type": "error", "data": {"error": f"{type(exc).__name__}: {exc}"}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


async def _persist_event(chat_session_id: str, event: dict[str, Any]) -> None:
    """把 dsh 事件落成消息（best-effort）。"""
    try:
        from ..chat import service as chat_svc

        etype = event.get("type") or ""
        data = event.get("data") or {}
        if etype == "tool/call":
            await chat_svc.append_tool_message(
                chat_session_id, tool_name=str(data.get("name") or ""),
                payload={"arguments": data.get("arguments")}, kind="call",
            )
        elif etype == "tool/result":
            await chat_svc.append_tool_message(
                chat_session_id, tool_name=str((data.get("message") or {}).get("source", {}).get("callId") or ""),
                payload={"result": str(data)[:2000]}, kind="result",
            )
    except Exception:  # noqa: BLE001
        logger.debug("[testing-chat] persist event failed", exc_info=True)


async def _safe_update(message_id: Optional[str], content: str, status: str) -> None:
    if not message_id:
        return
    try:
        from ..chat import service as chat_svc

        await chat_svc.update_message(message_id, content=content, status=status)
    except Exception:  # noqa: BLE001
        logger.debug("[testing-chat] update message failed", exc_info=True)


async def _safe_attach(chat_session_id: str, dsh_session_id: str) -> None:
    try:
        from ..chat import service as chat_svc

        await chat_svc.attach_dsh_session(chat_session_id, dsh_session_id)
    except Exception:  # noqa: BLE001
        logger.debug("[testing-chat] attach dsh session failed", exc_info=True)


@router.get("/health")
async def chat_health() -> dict[str, Any]:
    """探活：SDK 是否装好 + MCP patch 可否生成（不真起 dsh 子进程）。"""
    try:
        import deepseek_harness  # noqa: F401

        sdk_ok, sdk_err = True, None
    except Exception as exc:  # noqa: BLE001
        sdk_ok, sdk_err = False, f"{type(exc).__name__}: {exc}"
    return {
        "sdk_installed": sdk_ok,
        "sdk_error": sdk_err,
        "default_model": agent_runtime.DEFAULT_MODEL,
        "base_url": agent_runtime.DEFAULT_BASE_URL,
        "mcp_url": agent_runtime.DEFAULT_MCP_URL,
        "mcp_endpoint": "/api/v1/testing/mcp (POST JSON-RPC)",
        "dsh_home": agent_runtime.DEFAULT_DSH_HOME,
        "profile": agent_runtime.DEFAULT_PROFILE,
    }


# ── 会话持久化 REST（跨刷新/换设备续聊）───────────────────────────
@router.get("/sessions")
async def chat_list_sessions(
    project_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """列某测试项目的对话会话（最近消息在前）。"""
    await _resolve_caller(authorization)
    from ..chat import service as chat_svc

    rows = await chat_svc.list_sessions(project_id, status=status, limit=limit)
    return {"sessions": [_session_document(r) for r in rows], "count": len(rows)}


@router.get("/sessions/{session_id}")
async def chat_get_session(session_id: str, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    """取会话详情 + 全部消息（回放用）。"""
    await _resolve_caller(authorization)
    from ..chat import service as chat_svc

    session = await chat_svc.get_session(session_id)
    messages = await chat_svc.list_messages(session_id)
    return {
        "session": _session_document(session),
        "messages": [_message_document(m) for m in messages],
    }


@router.patch("/sessions/{session_id}")
async def chat_rename_session(
    session_id: str,
    payload: dict[str, Any],
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """重命名会话。"""
    await _resolve_caller(authorization)
    from ..chat import service as chat_svc

    session = await chat_svc.rename_session(session_id, str(payload.get("title") or ""))
    return {"session": _session_document(session)}


@router.delete("/sessions/{session_id}")
async def chat_delete_session(session_id: str, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    """删除会话及其消息（仅限误建）。"""
    await _resolve_caller(authorization)
    from ..chat import service as chat_svc

    await chat_svc.delete_session(session_id)
    return {"deleted": True}


def _session_document(session: Any) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "project_id": str(session.project_id),
        "title": session.title,
        "dsh_session_id": session.dsh_session_id,
        "model": session.model,
        "api_key_id": session.api_key_id,
        "message_count": session.message_count,
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
        "status": session.status,
        "created_at": session.created_at.isoformat() if session.created_at else None,
    }


def _message_document(msg: Any) -> dict[str, Any]:
    return {
        "id": str(msg.id),
        "seq": msg.seq,
        "role": msg.role,
        "content": msg.content,
        "tool_name": msg.tool_name,
        "tool_payload": dict(msg.tool_payload or {}),
        "status": msg.status,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


__all__ = ["router", "chat_send", "chat_health"]
