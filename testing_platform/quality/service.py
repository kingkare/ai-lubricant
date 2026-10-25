"""质量域服务（``design §15.2`` 疑问 + ``§15.6`` 沉淀）—— 流程一等公民的落库。

两个"流程一等公民"域的 CRUD（全异步、纯函数、无类状态）：

* **疑问（§15.2）** —— :func:`raise_question` / :func:`resolve_question` /
  :func:`list_questions` / :func:`get_question`。核心约束：**未解决疑问阻塞
  下游**——:func:`assert_not_blocked` 供用例/场景进下游前调用（带 open
  疑问即拒）。
* **沉淀（§15.6）** —— :func:`create_craft` / :func:`load_craft`（按
  type+target 读，供 AI 按需加载）/ :func:`list_craft` / :func:`update_craft`。

枚举取值集中在 :mod:`testing_platform.quality.models`（``*_STATUSES`` 等），
不在此重复硬编码。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from tortoise.exceptions import DoesNotExist

from .models import (
    CRAFT_SCOPES,
    CRAFT_STATUSES,
    CRAFT_TARGETS,
    CRAFT_TYPES,
    QUESTION_ORIGINS,
    QUESTION_RESOLUTIONS,
    QUESTION_STATUSES,
    CraftNotFoundError,
    CraftValidationError,
    QuestionBlockingError,
    QuestionNotFoundError,
    QuestionValidationError,
    TestCraftAsset,
    TestQuestion,
)

__all__ = [
    "assert_not_blocked",
    "create_craft",
    "get_question",
    "list_craft",
    "list_questions",
    "load_craft",
    "open_questions_for",
    "raise_question",
    "resolve_question",
    "update_craft",
]


# ── 小工具 ──────────────────────────────────────────────────────
def _as_uuid(value: Any, *, field: str) -> _uuid.UUID:
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise QuestionValidationError(f"{field} 不是合法 UUID：{value!r}") from exc


def _maybe_uuid(value: Any) -> Optional[_uuid.UUID]:
    if value is None or value == "":
        return None
    return _as_uuid(value, field="project_id")


def _validate_enum(value: Any, allowed: Sequence[str], *, field: str, required: bool) -> Any:
    if value is None:
        if required:
            raise QuestionValidationError(f"{field} 必填（可选值：{'/'.join(allowed)}）")
        return None
    if value not in allowed:
        raise QuestionValidationError(f"{field}={value!r} 不在允许集合 {list(allowed)} 内")
    return value


def _jsonify(value: Any) -> Any:
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    return str(value)


def _ref_list(value: Any) -> list[str]:
    """blocking_refs 归一为 str(id) 列表（标量包一层）。"""
    if value is None:
        return []
    if isinstance(value, (str, _uuid.UUID)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v]
    raise QuestionValidationError(f"blocking_refs 不合法：{value!r}（应为 id 列表）")


# ── 疑问（§15.2）────────────────────────────────────────────────
async def raise_question(
    *,
    project_id: Any,
    origin: str,
    content: str,
    context: Optional[Mapping[str, Any]] = None,
    blocking_refs: Any = None,
    raised_by: Optional[str] = None,
) -> TestQuestion:
    """提出疑问（design §15.2：``question.raise``）。

    ``origin`` 必填（product_uncertainty / design_doubt）；``content`` 必填。
    ``blocking_refs`` 是被阻塞的下游实体（用例/场景）id 列表——open 期间
    这些下游不得进入下一步（§15.2「未解决疑问阻塞下游」）。
    """
    if not content or not str(content).strip():
        raise QuestionValidationError("疑问 content 必填（这条疑问到底在问什么）")
    return await TestQuestion.create(
        project_id=_as_uuid(project_id, field="project_id"),
        origin=_validate_enum(origin, QUESTION_ORIGINS, field="origin", required=True),
        raised_by=raised_by,
        content=str(content).strip(),
        context=_jsonify(dict(context or {})),
        status="open",
        resolution=None,
        resolution_detail={},
        resolved_at=None,
        blocking_refs=_ref_list(blocking_refs),
    )


async def get_question(question_id: Any) -> TestQuestion:
    try:
        return await TestQuestion.get(id=_as_uuid(question_id, field="question_id"))
    except DoesNotExist:
        raise QuestionNotFoundError(f"疑问不存在：{question_id!r}") from None


async def list_questions(
    project_id: Any,
    *,
    status: Optional[str] = None,
    origin: Optional[str] = None,
) -> list[TestQuestion]:
    """按项目列疑问（可按 status/origin 收窄），最新在前。"""
    qs = TestQuestion.filter(project_id=_as_uuid(project_id, field="project_id"))
    if status:
        qs = qs.filter(status=_validate_enum(status, QUESTION_STATUSES, field="status", required=True))
    if origin:
        qs = qs.filter(origin=_validate_enum(origin, QUESTION_ORIGINS, field="origin", required=True))
    return await qs.order_by("-created_at")


async def resolve_question(
    question_id: Any,
    *,
    resolution: str,
    resolution_detail: Optional[Mapping[str, Any]] = None,
) -> TestQuestion:
    """收敛疑问（design §15.2：``question.resolve``）。

    ``resolution`` 必填（debug/doc/user_confirm）+ ``resolution_detail``
    记依据。收敛后状态 → resolved，解除对 ``blocking_refs`` 下游的阻塞。
    已 resolved 的疑问再 resolve 即报错（状态机单向）。
    """
    question = await get_question(question_id)
    if question.status == "resolved":
        raise QuestionValidationError(f"疑问 {question_id!r} 已收敛，不可重复 resolve")
    question.resolution = _validate_enum(
        resolution, QUESTION_RESOLUTIONS, field="resolution", required=True
    )
    question.resolution_detail = _jsonify(dict(resolution_detail or {}))
    question.status = "resolved"
    question.resolved_at = datetime.now(timezone.utc)
    await question.save()
    return question


async def open_questions_for(project_id: Any, ref_id: Any) -> list[TestQuestion]:
    """某下游实体（用例/场景）上仍 open 的疑问列表（阻塞判定用）。"""
    pid = _as_uuid(project_id, field="project_id")
    target = str(ref_id)
    rows = await TestQuestion.filter(project_id=pid, status="open")
    return [row for row in rows if target in [str(r) for r in (row.blocking_refs or [])]]


async def assert_not_blocked(project_id: Any, ref_id: Any) -> None:
    """**阻塞门**（design §15.2）：带 open 疑问的下游实体不得进执行。

    用例/场景进入脚本生成或执行前调用；命中即抛 :class:`QuestionBlockingError`
    （携带被阻塞实体 + open 疑问 id 列表，供调用方提示先收敛疑问）。
    """
    blocking = await open_questions_for(project_id, ref_id)
    if blocking:
        raise QuestionBlockingError(ref_id, [q.id for q in blocking])


# ── 沉淀四件套（§15.6）──────────────────────────────────────────
async def create_craft(
    *,
    type: str,
    target: str,
    scope: str = "project",
    content: Optional[Any] = None,
    status: str = "active",
) -> TestCraftAsset:
    """建沉淀资产（design §15.6）。

    ``type``（convention/template/sample/validator）+ ``target``
    （scenario/case/tool）必填；``scope``（common/framework/project）
    决定加载层。``content`` 结构化或文本（JSONB）。
    """
    craft_type = _validate_enum(type, CRAFT_TYPES, field="type", required=True)
    craft_target = _validate_enum(target, CRAFT_TARGETS, field="target", required=True)
    craft_scope = _validate_enum(scope, CRAFT_SCOPES, field="scope", required=False) or "project"
    craft_status = _validate_enum(status, CRAFT_STATUSES, field="status", required=False) or "active"

    payload = content if isinstance(content, Mapping) else {"text": content}
    return await TestCraftAsset.create(
        type=craft_type,
        scope=craft_scope,
        target=craft_target,
        content=_jsonify(dict(payload or {})),
        status=craft_status,
    )


async def get_craft(craft_id: Any) -> TestCraftAsset:
    try:
        return await TestCraftAsset.get(id=_as_uuid(craft_id, field="craft_id"))
    except DoesNotExist:
        raise CraftNotFoundError(f"沉淀资产不存在：{craft_id!r}") from None


async def list_craft(
    *,
    type: Optional[str] = None,
    scope: Optional[str] = None,
    target: Optional[str] = None,
    status: Optional[str] = None,
) -> list[TestCraftAsset]:
    """列沉淀资产（按 type/scope/target/status 任意收窄）。"""
    qs = TestCraftAsset.all()
    if type:
        qs = qs.filter(type=_validate_enum(type, CRAFT_TYPES, field="type", required=True))
    if scope:
        qs = qs.filter(scope=_validate_enum(scope, CRAFT_SCOPES, field="scope", required=True))
    if target:
        qs = qs.filter(target=_validate_enum(target, CRAFT_TARGETS, field="target", required=True))
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("scope", "type", "id")


async def load_craft(
    *,
    type: Optional[str] = None,
    target: Optional[str] = None,
    scope: Optional[str] = None,
) -> list[TestCraftAsset]:
    """**按需加载**（design §15.6：``craft.load``）——AI 按当前上下文取相关层。

    只返回 ``status=active`` 的资产（draft/deprecated/archived 不进加载面）；
    可按 type+target（+scope）收窄，``common → framework → project`` 分层排序
    （通用在前，项目特有在后，后加载的覆盖前层）。
    """
    qs = TestCraftAsset.filter(status="active")
    if type:
        qs = qs.filter(type=_validate_enum(type, CRAFT_TYPES, field="type", required=True))
    if target:
        qs = qs.filter(target=_validate_enum(target, CRAFT_TARGETS, field="target", required=True))
    if scope:
        qs = qs.filter(scope=_validate_enum(scope, CRAFT_SCOPES, field="scope", required=True))
    rows = await qs
    order = {name: idx for idx, name in enumerate(CRAFT_SCOPES)}
    return sorted(rows, key=lambda r: (order.get(r.scope, 99), r.type, str(r.id)))


async def update_craft(
    craft_id: Any,
    *,
    content: Optional[Any] = None,
    status: Optional[str] = None,
    scope: Optional[str] = None,
) -> TestCraftAsset:
    """改沉淀资产（内容/状态/层）。改动即递增 version。"""
    craft = await get_craft(craft_id)
    changed = False
    if content is not None:
        payload = content if isinstance(content, Mapping) else {"text": content}
        craft.content = _jsonify(dict(payload))
        changed = True
    if status is not None:
        craft.status = _validate_enum(status, CRAFT_STATUSES, field="status", required=True)
        changed = True
    if scope is not None:
        craft.scope = _validate_enum(scope, CRAFT_SCOPES, field="scope", required=True)
        changed = True
    if not changed:
        raise CraftValidationError("craft.update 未提供任何字段")
    craft.version = int(craft.version or 1) + 1
    await craft.save()
    return craft
