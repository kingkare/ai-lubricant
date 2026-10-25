"""评论域服务（26 号文 §2.3）——需求/测试点上的讨论串。

**为什么需要**（用户原话）：让不同的人进行评论，最终得出一个结论，
并且**将评论的历史记录下来**。

对外函数
--------

====================================  =========================================
:func:`list_comments`                 列某实体下的评论（含回复计数）
:func:`create_comment`                发评论
:func:`reply_comment`                 回复（一层嵌套）
:func:`resolve_comment`               收敛/定结论
:func:`react` / :func:`unreact`       表情
====================================  =========================================

纪律：
- **只增不删**——「历史记录下来」= 永不物理删除。
- 不 import ``assets`` / ``generation`` 的模型（挂载对象用 entity_type+entity_id，
  裸 UUID 不加 FK——防包间循环依赖）。
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (
    COMMENT_ENTITY_TYPES,
    REACTION_KINDS,
    RESOLUTIONS,
    TestComment,
    TestCommentReaction,
)

__all__ = [
    "CommentError",
    "list_comments",
    "create_comment",
    "reply_comment",
    "resolve_comment",
    "react",
    "unreact",
    "comment_document",
]


class CommentError(Exception):
    """评论域校验失败。"""

    def __init__(self, message: str, *, code: str = "comment_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _as_uuid(value: Any, *, field: str) -> Any:
    import uuid as _uuid

    if isinstance(value, _uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        raise CommentError(f"{field} 不能为空", code="invalid_params")
    try:
        return _uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise CommentError(f"无效 {field}：{value!r}", code="invalid_params") from exc


def comment_document(c: TestComment) -> dict:
    """评论行 → JSON 文档。"""
    return {
        "id": str(c.id),
        "project_id": str(c.project_id),
        "entity_type": c.entity_type,
        "entity_id": str(c.entity_id),
        "entity_ref": c.entity_ref,
        "parent_id": str(c.parent_id) if c.parent_id else None,
        "content": c.content,
        "status": c.status,
        "resolution": c.resolution,
        "resolution_detail": dict(c.resolution_detail or {}),
        "resolved_by": str(c.resolved_by) if c.resolved_by else None,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        "created_by": str(c.created_by) if c.created_by else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


async def list_comments(
    *,
    entity_type: str,
    entity_id: Any,
    entity_ref: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """列某实体下的评论（时间正序；``entity_ref`` 可选收窄到某条目）。

    :raises CommentError: 未知 entity_type / entity_id 无效。
    """
    if entity_type not in COMMENT_ENTITY_TYPES:
        raise CommentError(
            f"未知 entity_type：{entity_type!r}（合法：{COMMENT_ENTITY_TYPES}）",
            code="invalid_entity_type",
        )
    eid = _as_uuid(entity_id, field="entity_id")
    qs = TestComment.filter(entity_type=entity_type, entity_id=eid)
    if entity_ref:
        qs = qs.filter(entity_ref=entity_ref)
    rows = await qs.order_by("created_at").limit(max(1, int(limit)))
    return [comment_document(c) for c in rows]


async def create_comment(
    *,
    entity_type: str,
    entity_id: Any,
    entity_ref: Optional[str] = None,
    content: str,
    created_by: Optional[Any] = None,
    parent_id: Optional[Any] = None,
) -> dict:
    """发评论（或回复——``parent_id`` 给了就是回复）。"""
    if entity_type not in COMMENT_ENTITY_TYPES:
        raise CommentError(
            f"未知 entity_type：{entity_type!r}（合法：{COMMENT_ENTITY_TYPES}）",
            code="invalid_entity_type",
        )
    eid = _as_uuid(entity_id, field="entity_id")
    text = str(content or "").strip()
    if not text:
        raise CommentError("评论内容不能为空", code="invalid_params")
    if parent_id:
        parent = await TestComment.get_or_none(id=_as_uuid(parent_id, field="parent_id"))
        if parent is None:
            raise CommentError(f"父评论不存在：{parent_id}", code="not_found")
        # 继承实体的挂载（防止回复挂到别的实体上）
        entity_type = parent.entity_type
        eid = parent.entity_id
        entity_ref = parent.entity_ref
    comment = await TestComment.create(
        entity_type=entity_type,
        entity_id=eid,
        entity_ref=entity_ref,
        parent_id=_as_uuid(parent_id, field="parent_id") if parent_id else None,
        content=text,
        # 未登录评论的兜底 author：不另立「匿名用户」实体，直接复用调用方传入的
        # created_by；调用方必须给（前端传当前登录用户），否则挂 entity_id 占位
        # （由 surface 层强制——见 taskflow_methods.comment_create 的 created_by 可选性）。
        created_by=_as_uuid(created_by, field="created_by") if created_by else eid,
    )
    return comment_document(comment)


async def reply_comment(*, parent_id: Any, content: str, created_by: Optional[Any] = None) -> dict:
    """回复某条评论（一层嵌套；继承父评论的实体挂载）。"""
    parent = await TestComment.get_or_none(id=_as_uuid(parent_id, field="parent_id"))
    if parent is None:
        raise CommentError(f"父评论不存在：{parent_id}", code="not_found")
    return await create_comment(
        entity_type=parent.entity_type,
        entity_id=parent.entity_id,
        entity_ref=parent.entity_ref,
        content=content,
        created_by=created_by,
        parent_id=parent.id,
    )


async def resolve_comment(
    *,
    comment_id: Any,
    resolution: str,
    resolution_detail: Optional[dict] = None,
    resolved_by: Optional[Any] = None,
) -> dict:
    """收敛评论：定结论 + 补充说明。历史保留（status 翻 resolved，行不删）。"""
    if resolution not in RESOLUTIONS:
        raise CommentError(
            f"未知结论：{resolution!r}（合法：{RESOLUTIONS}）", code="invalid_resolution"
        )
    c = await TestComment.get_or_none(id=_as_uuid(comment_id, field="comment_id"))
    if c is None:
        raise CommentError(f"评论不存在：{comment_id}", code="not_found")
    c.status = "resolved"
    c.resolution = resolution
    c.resolution_detail = dict(resolution_detail or {})
    if resolved_by:
        c.resolved_by = _as_uuid(resolved_by, field="resolved_by")
    from datetime import datetime, timezone

    c.resolved_at = datetime.now(timezone.utc)
    await c.save(update_fields=["status", "resolution", "resolution_detail", "resolved_by",
                                "resolved_at", "updated_at"])
    return comment_document(c)


async def react(*, comment_id: Any, kind: str, created_by: Any) -> dict:
    """加表情（同一人同种表情幂等——唯一约束兜底）。"""
    if kind not in REACTION_KINDS:
        raise CommentError(f"未知表情：{kind!r}（合法：{REACTION_KINDS}）", code="invalid_kind")
    cid = _as_uuid(comment_id, field="comment_id")
    if await TestComment.get_or_none(id=cid) is None:
        raise CommentError(f"评论不存在：{comment_id}", code="not_found")
    await TestCommentReaction.get_or_create(
        comment_id=cid,
        kind=kind,
        created_by=_as_uuid(created_by, field="created_by"),
    )
    return {"ok": True}


async def unreact(*, comment_id: Any, kind: str, created_by: Any) -> dict:
    """取消表情。"""
    from tortoise.exceptions import DoesNotExist as _DNE

    cid = _as_uuid(comment_id, field="comment_id")
    try:
        row = await TestCommentReaction.get(
            comment_id=cid,
            kind=kind,
            created_by=_as_uuid(created_by, field="created_by"),
        )
        await row.delete()
    except _DNE:
        pass
    return {"ok": True}
