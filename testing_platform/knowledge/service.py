"""知识域查询服务（``09-knowledge-system.md`` §4.6 统一查询）。

薄查询层：按 ``project_id`` 列知识条目 / 代码锚点，供能力面
（``knowledge.list``）与后续画像/影响分析复用。全异步，返回 Tortoise
模型行——序列化为 JSON 文档由调用方（surface :func:`_item_document`）负责。
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID as _UUID

from .models import (
    KNOWLEDGE_SOURCE_KINDS,
    KNOWLEDGE_SOURCE_STATUSES,
    KNOWLEDGE_SOURCE_TYPES,
    KNOWLEDGE_SYNC_POLICIES,
    KnowledgeAnchor,
    KnowledgeItem,
    KnowledgeSource,
)


def _as_uuid(value: Any, *, field: str) -> _UUID:
    if isinstance(value, _UUID):
        return value
    try:
        return _UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} 不是合法 UUID：{value!r}") from exc


def _maybe_uuid(value: Any) -> Optional[_UUID]:
    if value is None or value == "":
        return None
    return _as_uuid(value, field="project_id")


def _validate_enum(value: Any, allowed, *, field: str, required: bool) -> Any:
    if value is None:
        if required:
            raise ValueError(f"{field} 必填（可选值：{'/'.join(allowed)}）")
        return None
    if value not in allowed:
        raise ValueError(f"{field}={value!r} 不在允许集合 {list(allowed)} 内")
    return value


async def list_items(
    project_id: Any,
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
) -> list[KnowledgeItem]:
    """按项目列知识条目（可按 ``kind`` / ``status`` 收窄），按 kind/key 排序。"""
    qs = KnowledgeItem.filter(project_id=project_id)
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("kind", "key")


async def list_anchors(project_id: Any) -> list[KnowledgeAnchor]:
    """按项目列代码锚点，按 kind/path/symbol 排序。"""
    return await KnowledgeAnchor.filter(project_id=project_id).order_by("kind", "path", "symbol")


# ── 知识源（design §15.1）───────────────────────────────────────
async def create_source(
    *,
    kind: str,
    name: str,
    source_type: str,
    location: Optional[dict] = None,
    sync_policy: str = "on_demand",
    project_id: Optional[Any] = None,
    status: str = "active",
) -> KnowledgeSource:
    """建知识源（design §15.1）。``kind/source_type/sync_policy`` 走枚举校验。"""
    if not name:
        raise ValueError("知识源 name 必填")
    return await KnowledgeSource.create(
        kind=_validate_enum(kind, KNOWLEDGE_SOURCE_KINDS, field="kind", required=True),
        name=str(name),
        source_type=_validate_enum(
            source_type, KNOWLEDGE_SOURCE_TYPES, field="source_type", required=True
        ),
        location=dict(location or {}),
        sync_policy=_validate_enum(
            sync_policy, KNOWLEDGE_SYNC_POLICIES, field="sync_policy", required=False
        )
        or "on_demand",
        project_id=_maybe_uuid(project_id),
        status=_validate_enum(
            status, KNOWLEDGE_SOURCE_STATUSES, field="status", required=False
        )
        or "active",
    )


async def list_sources(
    project_id: Optional[Any] = None,
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
) -> list[KnowledgeSource]:
    """列知识源（可按 project_id/kind/status 收窄），按 kind/name 排序。"""
    qs = KnowledgeSource.all()
    if project_id is not None:
        qs = qs.filter(project_id=_as_uuid(project_id, field="project_id"))
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("kind", "name")


async def get_source(source_id: Any) -> KnowledgeSource:
    from tortoise.exceptions import DoesNotExist

    try:
        return await KnowledgeSource.get(id=_as_uuid(source_id, field="source_id"))
    except DoesNotExist:
        raise LookupError(f"知识源不存在：{source_id!r}") from None


__all__ = [
    "create_source",
    "get_source",
    "list_anchors",
    "list_items",
    "list_sources",
]