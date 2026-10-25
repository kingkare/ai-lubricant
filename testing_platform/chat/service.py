"""对话持久化服务 —— ``mc_test_chat_session`` / ``mc_test_chat_message``。

dsh 把上下文维持在自己的 ``dsh_home`` 里，平台只存**看得见的会话与消息**：
刷新页面、换设备、翻历史，都靠这里。跨轮续接靠 ``dsh_session_id``（每轮回写一次，
dsh 返回什么就存什么）。

设计约束（design 数据治理三铁律）：稳定 ID、显式状态机、版本 + 溯源。
消息 ``seq`` **会话内单调**（不复用 ``mc_test_change_record``——对话不是资产变更）。
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID as _UUID

from tortoise.exceptions import DoesNotExist
from tortoise.timezone import now as _tz_now

from .models import (
    CHAT_MESSAGE_STATUSES,
    CHAT_ROLES,
    CHAT_SESSION_STATUSES,
    TestChatMessage,
    TestChatSession,
)


class ChatNotFoundError(LookupError):
    """会话/消息不存在。"""


class ChatValidationError(ValueError):
    """入参不合法。"""


def _as_uuid(value: Any, *, field: str) -> _UUID:
    if isinstance(value, _UUID):
        return value
    try:
        return _UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ChatValidationError(f"{field} 不是合法 UUID：{value!r}") from exc


def _validate_enum(value: Any, allowed, *, field: str, required: bool) -> Any:
    if value is None:
        if required:
            raise ChatValidationError(f"{field} 必填（可选值：{'/'.join(allowed)}）")
        return None
    if value not in allowed:
        raise ChatValidationError(f"{field}={value!r} 不在允许集合 {list(allowed)} 内")
    return value


# ── 会话 ─────────────────────────────────────────────────────────
async def create_session(
    *,
    project_id: Any,
    model: str = "",
    api_key_id: Optional[int] = None,
    created_by: Any = None,
    title: Optional[str] = None,
    dsh_session_id: Optional[str] = None,
) -> TestChatSession:
    """建对话会话。``dsh_session_id`` 首轮跑完由 :func:`attach_dsh_session` 回写。"""
    return await TestChatSession.create(
        project_id=_as_uuid(project_id, field="project_id"),
        model=str(model or ""),
        api_key_id=int(api_key_id) if api_key_id is not None else None,
        created_by=_as_uuid(created_by, field="created_by") if created_by else None,
        title=(str(title)[:512] if title else None),
        dsh_session_id=dsh_session_id,
        status="active",
    )


async def get_session(session_id: Any) -> TestChatSession:
    try:
        return await TestChatSession.get(id=_as_uuid(session_id, field="session_id"))
    except DoesNotExist:
        raise ChatNotFoundError(f"对话会话不存在：{session_id!r}") from None


async def list_sessions(
    project_id: Any,
    *,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[TestChatSession]:
    """按项目列会话（最近消息在前）。"""
    qs = TestChatSession.filter(project_id=_as_uuid(project_id, field="project_id"))
    if status:
        qs = qs.filter(status=_validate_enum(status, CHAT_SESSION_STATUSES, field="status", required=True))
    return await qs.order_by("-last_message_at", "-created_at").limit(limit)


async def attach_dsh_session(session_id: Any, dsh_session_id: str) -> TestChatSession:
    """回写 dsh 侧 session id（跨轮续接的键）。"""
    session = await get_session(session_id)
    session.dsh_session_id = str(dsh_session_id)
    await session.save()
    return session


async def rename_session(session_id: Any, title: str) -> TestChatSession:
    session = await get_session(session_id)
    session.title = str(title or "")[:512] or None
    await session.save()
    return session


async def archive_session(session_id: Any) -> TestChatSession:
    """归档（不物理删除——消息留档）。"""
    session = await get_session(session_id)
    session.status = "archived"
    await session.save()
    return session


async def delete_session(session_id: Any) -> None:
    """删会话 + 其消息（仅限误建；design 数据治理「删：仅限误建，人工确认」）。"""
    session = await get_session(session_id)
    await TestChatMessage.filter(session_id=session.id).delete()
    await session.delete()


# ── 消息 ─────────────────────────────────────────────────────────
async def append_message(
    session_id: Any,
    *,
    role: str,
    content: str = "",
    tool_name: Optional[str] = None,
    tool_payload: Optional[dict] = None,
    status: str = "done",
) -> TestChatMessage:
    """追加一条消息（``seq`` = 会话内 max+1）。同时刷新会话水位。"""
    session = await get_session(session_id)
    seqs = await TestChatMessage.filter(session_id=session.id).values_list("seq", flat=True)
    seq = (max(seqs) + 1) if seqs else 1

    msg = await TestChatMessage.create(
        session_id=session.id,
        seq=seq,
        role=_validate_enum(role, CHAT_ROLES, field="role", required=True),
        content=str(content or ""),
        tool_name=str(tool_name) if tool_name else None,
        tool_payload=dict(tool_payload or {}),
        status=_validate_enum(status, CHAT_MESSAGE_STATUSES, field="status", required=False) or "done",
    )

    from tortoise.expressions import F

    session.message_count = (session.message_count or 0) + 1
    session.last_message_at = _tz_now()
    # 首条用户消息且没标题 → 拿它当标题（前端列表好看）
    if not session.title and role == "user" and content:
        session.title = str(content)[:120]
    await session.save()
    return msg


async def update_message(
    message_id: Any,
    *,
    content: Optional[str] = None,
    status: Optional[str] = None,
) -> TestChatMessage:
    """流式收尾用：assistant 消息先建 streaming，done 时回写 content/status。"""
    try:
        msg = await TestChatMessage.get(id=_as_uuid(message_id, field="message_id"))
    except DoesNotExist:
        raise ChatNotFoundError(f"消息不存在：{message_id!r}") from None
    if content is not None:
        msg.content = str(content)
    if status is not None:
        msg.status = _validate_enum(status, CHAT_MESSAGE_STATUSES, field="status", required=True)
    await msg.save()
    return msg


async def append_tool_message(
    session_id: Any,
    *,
    tool_name: str,
    payload: dict,
    kind: str = "call",
) -> TestChatMessage:
    """记一条工具调用/结果（role=tool）。``kind`` 只影响 payload 里的标记。"""
    return await append_message(
        session_id,
        role="tool",
        content="",
        tool_name=tool_name,
        tool_payload={"kind": kind, **dict(payload or {})},
    )


async def list_messages(
    session_id: Any,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> list[TestChatMessage]:
    """按会话列消息（seq 升序；``after_seq`` 做增量拉取）。"""
    session = await get_session(session_id)
    qs = TestChatMessage.filter(session_id=session.id)
    if after_seq:
        qs = qs.filter(seq__gt=int(after_seq))
    return await qs.order_by("seq").limit(limit)


__all__ = [
    "CHAT_MESSAGE_STATUSES",
    "CHAT_ROLES",
    "ChatNotFoundError",
    "ChatValidationError",
    "append_message",
    "append_tool_message",
    "archive_session",
    "attach_dsh_session",
    "create_session",
    "delete_session",
    "get_session",
    "list_messages",
    "list_sessions",
    "rename_session",
    "update_message",
]
