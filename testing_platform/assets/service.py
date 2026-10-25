"""资产域 CRUD 服务（``01-asset-model.md`` §1–§4 + design §15.12 变更门）。

全异步、纯 service 层函数（无类状态），供 designer/coder 工作流与页面共用：

* **code 自动生成** —— ``TC-0001`` / ``SCN-0001``，按 ``project_id`` 内现有
  同前缀 code 的 max+1（§2/§3「稳定 ID」）。
* **多对多链接** —— :func:`link_case_scenario`（§2.1，``is_primary`` 单主场景）、
  :func:`tag_case`（§2.2）。
* **组装** —— :func:`get_scenario_full` 把场景 + 步骤拼成引擎
  ``ScenarioLoader`` 需要的文档形态（steps 按 ``seq`` 排序、``local_vars``/``flow``
  展平）。这是 :class:`testing_platform.assets.loader.PgScenarioLoader` 的唯一读取路径。
* **变更门** —— 所有 ``update_*`` 必须带 ``change_reason``，否则抛
  :class:`ChangeReasonRequiredError`；通过则写 ``mc_test_change_record``
  （``field_changes`` = 字段级 diff），并递增 ``version``（design §15.12
  「唯一的版本机制」）。

枚举取值集中在 :mod:`testing_platform.assets.models`（``*_STATUSES`` 等），
不在此重复硬编码。
"""
from __future__ import annotations

import json as _json
import uuid as _uuid
from datetime import datetime as _dt, timezone as _tz
from typing import Any, Iterable, Mapping, Optional, Sequence

from tortoise.exceptions import DoesNotExist

from .models import (
    API_BODY_TYPES,
    API_IMPORT_SCHEDULE_KINDS,
    API_METHODS,
    API_PROTOCOLS,
    API_SOURCES,
    API_SOURCE_FORMATS,
    API_STATUSES,
    CASE_AUTOMATION_STATUSES,
    CASE_CONFIRM_SOURCES,
    CASE_ORIGINS,
    CASE_PRIORITIES,
    CASE_STATUSES,
    CASE_TEST_TYPES,
    CATALOG_TYPES,
    CHANGE_ENTITY_TYPES,
    CUSTOM_TOOL_SCOPES,
    CUSTOM_TOOL_STATUSES,
    ENV_VARIABLE_TYPES,
    ENVIRONMENT_STATUSES,
    METHOD_MODULE_STATUSES,
    REQUIREMENT_ATTACHMENT_KINDS,
    REQUIREMENT_ORIGINS,
    REQUIREMENT_STATUSES,
    SCENARIO_STATUSES,
    STEP_TYPES,
    TAG_CATEGORIES,
    TRANSPORT_KINDS,
    TRANSPORT_STATUSES,
    TestCase,
    TestCaseScenario,
    TestCaseTag,
    TestChangeRecord,
    TestCustomTool,
    TestCustomToolVersion,
    TestDataset,
    TestEnvVariable,
    TestEnvironment,
    TestMethod,
    TestMethodModule,
    TestApi,
    TestApiCase,
    TestApiFolder,
    TestApiImportJob,
    TestApiScenario,
    TestApiSource,
    TestCatalog,
    TestRequirement,
    TestRequirementAttachment,
    TestScenario,
    TestScenarioStep,
    TestTag,
    TestTransport,
)

CASE_CODE_PREFIX = "TC-"
SCENARIO_CODE_PREFIX = "SCN-"
REQUIREMENT_CODE_PREFIX = "REQ-"
API_CODE_PREFIX = "API-"

#: 关联表中「主场景」互斥：同一用例同时只能有一条 is_primary=true。
_PRIMARY_MARKER = "is_primary"


class AssetValidationError(ValueError):
    """入参不合法（枚举越界、缺必填、引用不存在）。

    继承 ``ValueError``：调用方可按标准值错误处理，无需再学一套新基类。
    """


class ChangeReasonRequiredError(AssetValidationError):
    """变更门未过：``update_*`` 未提供 ``change_reason``（design §15.12）。"""


class AssetNotFoundError(LookupError):
    """实体不存在。loader 负责翻译成引擎的 ``ScenarioNotFoundError``。"""


# ── 小工具 ──────────────────────────────────────────────────────
def _as_uuid(value: Any, *, field: str) -> _uuid.UUID:
    """把 str/UUID 归一为 UUID；非法即报错（不静默变 None）。"""
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AssetValidationError(f"{field} 不是合法 UUID：{value!r}") from exc


def _maybe_uuid(value: Any, *, field: str) -> Optional[_uuid.UUID]:
    if value is None or value == "":
        return None
    return _as_uuid(value, field=field)


def _validate_enum(value: Any, allowed: Sequence[str], *, field: str, required: bool) -> Any:
    if value is None:
        if required:
            raise AssetValidationError(f"{field} 必填（可选值：{'/'.join(allowed)}）")
        return None
    if value not in allowed:
        raise AssetValidationError(f"{field}={value!r} 不在允许集合 {list(allowed)} 内")
    return value


def _jsonify(value: Any) -> Any:
    """JSONB 可写形态：UUID/datetime 等转字符串，容器递归。"""
    if isinstance(value, (_uuid.UUID,)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    return str(value)


async def _next_code(model: Any, project_id: Any, prefix: str) -> str:
    """``prefix`` + 4 位递增（TC-0001 / SCN-0001），**按 project 内 max+1**。

    解析所有同前缀 code 取数值最大者再 +1 —— 不能用 ``order_by("-code")``，
    字典序下 ``TC-9999`` > ``TC-10000``，位数一涨就会撞号。
    """
    pid = _as_uuid(project_id, field="project_id")
    codes = await model.filter(project_id=pid).values_list("code", flat=True)
    numbers: list[int] = []
    for code in codes:
        if isinstance(code, str) and code.startswith(prefix):
            tail = code[len(prefix):]
            if tail.isdigit():
                numbers.append(int(tail))
    return f"{prefix}{(max(numbers) + 1) if numbers else 1:04d}"


# ── 标签（§1）────────────────────────────────────────────────────
async def create_tag(
    *,
    project_id: Any,
    name: str,
    category: str,
    code: Optional[str] = None,
    description: Optional[str] = None,
) -> TestTag:
    """建标签。``category`` 固定四类；``code`` 缺省取 ``name``（稳定 ID）。"""
    if not name:
        raise AssetValidationError("标签 name 必填")
    return await TestTag.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=code or name,
        name=name,
        category=_validate_enum(category, TAG_CATEGORIES, field="category", required=True),
        description=description,
    )


async def list_tags(project_id: Any, *, category: Optional[str] = None) -> list[TestTag]:
    """按项目列标签（可按 category 收窄）。"""
    qs = TestTag.filter(project_id=_as_uuid(project_id, field="project_id"))
    if category:
        qs = qs.filter(category=category)
    return await qs.order_by("category", "code")


# ── 用例（§2）────────────────────────────────────────────────────
async def create_case(
    *,
    project_id: Any,
    title: str,
    confirm_source: str,
    created_by: Any,
    intent: Optional[str] = None,
    preconditions: Optional[Any] = None,
    expected: Optional[Mapping[str, Any]] = None,
    steps: Optional[Sequence[Mapping[str, Any]]] = None,
    dataset_id: Optional[Any] = None,
    confirm_ref: Optional[Mapping[str, Any]] = None,
    origin: str = "manual",
    priority: Optional[str] = None,
    module: Optional[str] = None,
    feature: Optional[str] = None,
    version_id: Optional[Any] = None,
    requirement_id: Optional[Any] = None,
    test_type: Optional[str] = None,
    test_types: Optional[Any] = None,
    folder_id: Optional[Any] = None,
    automation_status: str = "not_automated",
    status: str = "draft",
) -> TestCase:
    """建用例（业务视角：数据 + 步骤说明 + 场景引用 + 标签）。

    ``confirm_source`` 必填（§2 创建依据，过确认门）；``code`` 自动 ``TC-xxxx``。
    ``steps`` 是自然语言测试步骤说明（业务视角），与场景的可执行步骤分层。
    ``module`` 是分组维度；``version_id`` 关联版本（图谱/用例按版本过滤）。
    ``preconditions`` 存**字符串列表**（一行一条；旧 dict/AI 产出由归一层兼容）。
    ``test_types`` 多值类型（新）；``test_type`` 旧单值保留兼容（两列并存，读侧 coalesce）。
    ``folder_id`` 指向 ``mc_test_catalog``（``type=case``）；裸 UUID 无 FK 约束。
    """
    if not title:
        raise AssetValidationError("用例 title 必填")
    return await TestCase.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=await _next_code(TestCase, project_id, CASE_CODE_PREFIX),
        title=title,
        intent=intent,
        preconditions=_clean_case_preconditions(preconditions),
        expected=dict(expected or {}),
        steps=_clean_case_steps(steps),
        dataset_id=_maybe_uuid(dataset_id, field="dataset_id"),
        confirm_source=_validate_enum(
            confirm_source, CASE_CONFIRM_SOURCES, field="confirm_source", required=True
        ),
        confirm_ref=dict(confirm_ref or {}),
        origin=_validate_enum(origin or "manual", CASE_ORIGINS, field="origin", required=False)
        or "manual",
        priority=_validate_enum(priority, CASE_PRIORITIES, field="priority", required=False)
        if priority is not None
        else None,
        module=module,
        feature=feature,
        version_id=_maybe_uuid(version_id, field="version_id"),
        requirement_id=_maybe_uuid(requirement_id, field="requirement_id"),
        test_type=_validate_enum(test_type, CASE_TEST_TYPES, field="test_type", required=False)
        if test_type is not None
        else None,
        test_types=_clean_case_test_types(test_types),
        folder_id=_maybe_uuid(folder_id, field="folder_id"),
        automation_status=_validate_enum(
            automation_status, CASE_AUTOMATION_STATUSES, field="automation_status", required=False
        )
        or "not_automated",
        status=_validate_enum(status, CASE_STATUSES, field="status", required=False) or "draft",
        created_by=_as_uuid(created_by, field="created_by"),
    )


async def get_case(case_id: Any) -> TestCase:
    try:
        return await TestCase.get(id=_as_uuid(case_id, field="case_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"用例不存在：{case_id!r}") from None


async def list_cases(project_id: Any, *, status: Optional[str] = None) -> list[TestCase]:
    qs = TestCase.filter(project_id=_as_uuid(project_id, field="project_id"))
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("code")


# ── 需求（主脊）─────────────────────────────────────────────────
def doc_to_text(content: Any) -> str:
    """把富文本正文（Tiptap/ProseMirror 文档 JSON）提取成**纯文本**。

    用作 ``description`` 的派生值：AI 生成提示词、列表预览、向后兼容都读它。
    不依赖编辑器——只按 ProseMirror 的 ``{type, content:[...], text}`` 结构递归取
    ``text`` 节点，块级节点之间补换行。空/异常输入回退空串（调用方据此保留旧值）。
    """
    if not isinstance(content, Mapping):
        return ""
    blocks: list[str] = []

    def walk(node: Any, out: list[str]) -> None:
        if not isinstance(node, Mapping):
            return
        if isinstance(node.get("text"), str):
            out.append(node["text"])
            return
        node_type = node.get("type")
        if node_type == "hardBreak":
            out.append("\n")
            return
        children = node.get("content")
        if isinstance(children, list):
            inner: list[str] = []
            for child in children:
                walk(child, inner)
            if node_type in ("paragraph", "heading", "listItem", "blockquote",
                             "codeBlock", "tableRow", "tableCell", "tableHeader"):
                blocks.append("".join(inner))
            else:
                out.append("".join(inner))
        elif node_type in ("paragraph", "heading", "listItem"):
            blocks.append("")

    root: list[str] = []
    walk(content, root)
    root_text = "".join(root).strip()
    if root_text:
        blocks.append(root_text)
    # 去掉空块、块间换行
    return "\n".join(b.strip() for b in blocks if b and b.strip()).strip()


def _content_projection(content: Any, fallback: Optional[str]) -> Optional[str]:
    """由 content 派生 description 纯文本；content 为空时回退 ``fallback``（旧值/前端传值）。"""
    if content is None:
        return fallback
    if isinstance(content, Mapping) and content.get("content"):
        text = doc_to_text(content)
        return text or None
    return fallback


async def create_requirement(
    *,
    project_id: Any,
    title: str,
    description: Optional[str] = None,
    content: Optional[Mapping[str, Any]] = None,
    mindmap: Optional[Mapping[str, Any]] = None,
    acceptance_criteria: Optional[Sequence[Any]] = None,
    priority: Optional[str] = None,
    module: Optional[str] = None,
    folder_id: Optional[Any] = None,
    version_id: Optional[Any] = None,
    origin: str = "manual",
    source_ref: Optional[Mapping[str, Any]] = None,
    status: str = "draft",
    elements: Optional[Mapping[str, Any]] = None,
    created_by: Any = None,
) -> TestRequirement:
    """建需求（主脊）。``code`` 自动 ``REQ-xxxx``（项目内自增）。

    ``content`` 为富文本正文（真相源）；``description`` 由其派生纯文本投影
    （content 非空时以 content 为准，否则用传入的 description）。
    ``folder_id`` 指向 ``mc_test_catalog``（``type=requirement``）；裸 UUID 无 FK 约束。
    ``elements`` 九要素结构化（26 号文 §2.1）——与富文本并存的结构化补充。
    """
    if not title:
        raise AssetValidationError("需求 title 必填")
    pid = _as_uuid(project_id, field="project_id")
    content_val = dict(content or {})
    return await TestRequirement.create(
        project_id=pid,
        code=await _next_code(TestRequirement, pid, REQUIREMENT_CODE_PREFIX),
        title=title,
        description=_content_projection(content, description),
        content=content_val,
        mindmap=dict(mindmap or {}),
        acceptance_criteria=list(acceptance_criteria or []),
        priority=_validate_enum(priority, CASE_PRIORITIES, field="priority", required=False)
        if priority is not None
        else None,
        module=module,
        folder_id=_maybe_uuid(folder_id, field="folder_id"),
        version_id=_maybe_uuid(version_id, field="version_id"),
        origin=_validate_enum(origin or "manual", REQUIREMENT_ORIGINS, field="origin", required=False)
        or "manual",
        source_ref=dict(source_ref or {}),
        status=_validate_enum(status, REQUIREMENT_STATUSES, field="status", required=False) or "draft",
        elements=dict(elements or {}),
        created_by=_maybe_uuid(created_by, field="created_by"),
    )


async def get_requirement(requirement_id: Any) -> TestRequirement:
    try:
        return await TestRequirement.get(id=_as_uuid(requirement_id, field="requirement_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"需求不存在：{requirement_id!r}") from None


async def list_requirements(
    project_id: Any, *, version_id: Optional[Any] = None, status: Optional[str] = None
) -> list[TestRequirement]:
    qs = TestRequirement.filter(project_id=_as_uuid(project_id, field="project_id"))
    if version_id:
        qs = qs.filter(version_id=_as_uuid(version_id, field="version_id"))
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("code")


REQUIREMENT_EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "content",
    "mindmap",
    "acceptance_criteria",
    "priority",
    "module",
    "folder_id",
    "version_id",
    "origin",
    "source_ref",
    "status",
    "elements",
)


async def update_requirement(
    requirement_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestRequirement:
    """改需求（变更门强制）。

    富文本正文 ``content`` 为真相源：写入时同步派生 ``description`` 纯文本投影
    （前端可不传 description；content 为空则沿用旧 description，避免清空 AI 读到的描述）。
    """
    requirement = await get_requirement(requirement_id)
    clean = _normalize_requirement_values(values)
    if "content" in clean:
        clean["description"] = _content_projection(clean["content"], requirement.description)
    return await _apply_update(
        requirement,
        entity_type="requirement",
        allowed=REQUIREMENT_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )


def _normalize_requirement_values(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "status":
            value = _validate_enum(value, REQUIREMENT_STATUSES, field=key, required=True)
        elif key == "priority" and value is not None:
            value = _validate_enum(value, CASE_PRIORITIES, field=key, required=True)
        elif key == "origin":
            value = _validate_enum(value or "manual", REQUIREMENT_ORIGINS, field=key, required=False) or "manual"
        elif key == "acceptance_criteria":
            value = list(value or [])
        elif key in ("content", "mindmap", "source_ref", "elements"):
            value = dict(value or {})
        elif key in ("version_id", "folder_id"):
            value = _maybe_uuid(value, field=key)
        out[key] = value
    return out


async def delete_requirement(requirement_id: Any) -> None:
    """删需求：把挂到它的产出（用例/场景/接口/缺陷）的 ``requirement_id`` 置空，再删需求行。

    不级联删产出——产出本身仍然有效，只是不再归属某个需求（前端显示为「未关联需求」）。
    """
    requirement = await get_requirement(requirement_id)
    rid = requirement.id
    await TestCase.filter(requirement_id=rid).update(requirement_id=None)
    await TestScenario.filter(requirement_id=rid).update(requirement_id=None)
    await TestTransport.filter(requirement_id=rid).update(requirement_id=None)
    try:
        from ..defects.models import TestBug as _B

        await _B.filter(requirement_id=rid).update(requirement_id=None)
    except Exception:  # noqa: BLE001 —— 缺陷域可选（表未建时忽略）
        pass
    # 附件行保留（文件本体在 OSS，不因删需求而删）——只解关联。
    try:
        await TestRequirementAttachment.filter(requirement_id=rid).update(requirement_id=None)
    except Exception:  # noqa: BLE001 —— 表未建时忽略
        pass
    await requirement.delete()


# ── 需求附件（需求 = 文档：Word/思维导图/图片/任意文件）─────────────
async def add_requirement_attachment(
    *,
    requirement_id: Any,
    name: str,
    url: str,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    kind: str = "file",
    source_ref: Optional[Mapping[str, Any]] = None,
    uploaded_by: Any = None,
) -> TestRequirementAttachment:
    """给需求挂一个附件（URL 由浏览器直传 OSS 得到，本层只存元数据）。"""
    if not url:
        raise AssetValidationError("附件 url 必填")
    req = await get_requirement(requirement_id)
    return await TestRequirementAttachment.create(
        requirement_id=req.id,
        project_id=req.project_id,
        name=name or "attachment",
        url=url,
        mime_type=mime_type,
        size_bytes=size_bytes,
        kind=_validate_enum(kind or "file", REQUIREMENT_ATTACHMENT_KINDS, field="kind", required=False)
        or "file",
        source_ref=dict(source_ref or {}),
        uploaded_by=_maybe_uuid(uploaded_by, field="uploaded_by"),
    )


async def list_requirement_attachments(requirement_id: Any) -> list[TestRequirementAttachment]:
    rid = _as_uuid(requirement_id, field="requirement_id")
    return await TestRequirementAttachment.filter(requirement_id=rid).order_by("created_at")


async def delete_requirement_attachment(attachment_id: Any) -> None:
    try:
        row = await TestRequirementAttachment.get(id=_as_uuid(attachment_id, field="attachment_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"附件不存在：{attachment_id!r}") from None
    await row.delete()


# ── 场景（§3）────────────────────────────────────────────────────
async def create_scenario(
    *,
    project_id: Any,
    name: str,
    purpose: Optional[str] = None,
    transport_id: Optional[Any] = None,
    env_id: Optional[Any] = None,
    version_id: Optional[Any] = None,
    requirement_id: Optional[Any] = None,
    folder_id: Optional[Any] = None,
    local_vars: Optional[Mapping[str, Any]] = None,
    flow: Optional[Mapping[str, Any]] = None,
    status: str = "draft",
    code: Optional[str] = None,
) -> TestScenario:
    """建场景（实现视角：步骤集合 + 局部变量）。``code`` 自动 ``SCN-xxxx``。

    ``exec_mode`` 三模**已停用**（用户定版：执行语义下沉到步骤类型
    ai_judge/agent_action/agent_workflow）——不再写入，列走模型默认值。

    ``folder_id`` 指向统一目录 ``mc_test_catalog(type=scenario)``（裸 UUID，无 FK）。
    """
    if not name:
        raise AssetValidationError("场景 name 必填")
    return await TestScenario.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=code or await _next_code(TestScenario, project_id, SCENARIO_CODE_PREFIX),
        name=name,
        purpose=purpose,
        transport_id=_maybe_uuid(transport_id, field="transport_id"),
        env_id=_maybe_uuid(env_id, field="env_id"),
        version_id=_maybe_uuid(version_id, field="version_id"),
        requirement_id=_maybe_uuid(requirement_id, field="requirement_id"),
        folder_id=_maybe_uuid(folder_id, field="folder_id"),
        local_vars=dict(local_vars or {}),
        flow=dict(flow or {}),
        status=_validate_enum(status, SCENARIO_STATUSES, field="status", required=False) or "draft",
    )


async def get_scenario(scenario_id: Any) -> TestScenario:
    """按 UUID 主键或 ``SCN-xxxx`` code 取场景（前置场景常以 code 引用）。"""
    lookup: dict[str, Any]
    try:
        lookup = {"id": _as_uuid(scenario_id, field="scenario_id")}
    except AssetValidationError:
        lookup = {"code": str(scenario_id)}
    try:
        return await TestScenario.get(**lookup)
    except DoesNotExist:
        raise AssetNotFoundError(f"场景不存在：{scenario_id!r}") from None


async def list_scenarios(
    project_id: Any,
    *,
    status: Optional[str] = None,
    folder_id: Optional[Any] = None,
) -> list[TestScenario]:
    """按项目列场景（可按 status / 目录 folder_id 收窄），code 升序。"""
    qs = TestScenario.filter(project_id=_as_uuid(project_id, field="project_id"))
    if status:
        qs = qs.filter(status=status)
    if folder_id:
        qs = qs.filter(folder_id=_as_uuid(folder_id, field="folder_id"))
    return await qs.order_by("code")


# ── 步骤（§4）────────────────────────────────────────────────────
async def create_steps(scenario_id: Any, steps: Sequence[Mapping[str, Any]]) -> list[TestScenarioStep]:
    """批量建步骤（§4）。

    每个 step 支持 ``step_key`` / ``seq`` / ``name`` / ``description`` / ``type``
    / ``params`` / ``post_checks`` / ``on_error`` / ``enabled``。

    * ``step_key`` 必填且场景内唯一（被 ``flow`` 引用）。
    * ``seq`` 缺省续接场景内现有最大 seq（步长 10）——顺序唯一真相，不靠插入顺序。
    * ``post_checks`` **必填 ≥1** 由校验器负责，本层不强制（模型层不重复规则）。
    """
    scenario = await get_scenario(scenario_id)
    if not steps:
        return []

    existing = await TestScenarioStep.filter(scenario_id=scenario.id).values_list("seq", "step_key")
    existing_seqs = [row[0] for row in existing if isinstance(row[0], int)]
    existing_keys = {str(row[1]) for row in existing if row[1] is not None}
    next_seq = (max(existing_seqs) if existing_seqs else 0) + 10

    seen_keys: set[str] = set()
    rows: list[TestScenarioStep] = []
    for index, raw in enumerate(steps):
        step = dict(raw)
        step_key = str(step.get("step_key") or "").strip()
        if not step_key:
            raise AssetValidationError(f"steps[{index}] 缺少 step_key")
        if step_key in seen_keys or step_key in existing_keys:
            raise AssetValidationError(f"step_key 重复：{step_key!r}（场景内必须唯一）")
        seen_keys.add(step_key)
        step_type = step.get("type")
        if step_type is None:
            raise AssetValidationError(f"steps[{index}]（{step_key}）缺少 type")
        _validate_enum(step_type, STEP_TYPES, field=f"steps[{index}].type", required=True)

        seq = step.get("seq")
        if seq is None:
            seq = next_seq
            next_seq += 10
        elif isinstance(seq, bool) or not isinstance(seq, int):
            raise AssetValidationError(f"steps[{index}].seq 必须是整数：{seq!r}")

        enabled = step.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AssetValidationError(f"steps[{index}].enabled 必须是布尔值：{enabled!r}")

        rows.append(
            TestScenarioStep(
                scenario_id=scenario.id,
                step_key=step_key,
                seq=int(seq),
                name=step.get("name"),
                description=step.get("description"),
                type=step_type,
                params=dict(step.get("params") or {}),
                pre_cond=list(step.get("pre_cond") or []),
                post_checks=list(step.get("post_checks") or []),
                post_cond=list(step.get("post_cond") or []),
                on_error=step.get("on_error"),
                enabled=enabled,
            )
        )

    new_seqs = [r.seq for r in rows]
    if len(set(new_seqs)) != len(new_seqs):
        raise AssetValidationError("steps 内 seq 重复（顺序唯一真相，必须互异）")
    clash = sorted(set(new_seqs) & set(existing_seqs))
    if clash:
        raise AssetValidationError(f"seq 与场景现有步骤冲突：{clash}（seq 是唯一顺序真相）")

    await TestScenarioStep.bulk_create(rows)
    # 步骤引用接口（params.api_id）→ 重建 场景↔接口 / 用例↔接口 反查索引。
    await sync_scenario_api_links(scenario.id)
    await sync_cases_of_scenario(scenario.id)
    # Tortoise 1.1.7 的 bulk_create 不返回实例；返回我们构造好的 rows
    # （已带 seq/step_key 等业务字段，供调用方免二次查询）。
    return rows


async def list_steps(scenario_id: Any) -> list[TestScenarioStep]:
    """场景步骤，按 ``seq`` 升序（唯一顺序真相）。"""
    scenario = await get_scenario(scenario_id)
    return await TestScenarioStep.filter(scenario_id=scenario.id).order_by("seq")


# ── 多对多链接（§2.1 / §2.2）─────────────────────────────────────
async def link_case_scenario(
    case_id: Any,
    scenario_id: Any,
    *,
    is_primary: bool = False,
    order: int = 0,
) -> TestCaseScenario:
    """用例↔场景关联（§2.1，幂等：重复链接返回既有行）。

    ``is_primary=True`` 时清掉同用例其它行的主场景标记——「默认执行」只允许一个。
    """
    case = await get_case(case_id)
    scenario = await get_scenario(scenario_id)
    if not isinstance(order, int):
        raise AssetValidationError(f"order 必须是整数：{order!r}")

    link = await TestCaseScenario.filter(case_id=case.id, scenario_id=scenario.id).first()
    if link is None:
        link = await TestCaseScenario.create(
            case_id=case.id,
            scenario_id=scenario.id,
            is_primary=bool(is_primary),
            order=order,
        )
    else:
        link.is_primary = bool(is_primary)
        link.order = order
        await link.save()

    if is_primary:
        await (
            TestCaseScenario.filter(case_id=case.id)
            .exclude(id=link.id)
            .update(**{_PRIMARY_MARKER: False})
        )
    # 绑定场景后 → 重建该用例的「覆盖接口」索引（沿场景↔接口派生）。
    await sync_case_api_links(case.id)
    return link


async def unlink_case_scenario(case_id: Any, scenario_id: Any) -> int:
    """解除用例↔场景关联，返回删除行数。"""
    case = await get_case(case_id)
    scenario = await get_scenario(scenario_id)
    deleted = await TestCaseScenario.filter(case_id=case.id, scenario_id=scenario.id).delete()
    # 解绑后 → 重建该用例的「覆盖接口」索引（可能少了一批接口）。
    await sync_case_api_links(case.id)
    return deleted


async def tag_case(case_id: Any, tag_id: Any) -> TestCaseTag:
    """用例↔标签（§2.2，幂等：重复打标返回既有行）。"""
    case = await get_case(case_id)
    try:
        tag = await TestTag.get(id=_as_uuid(tag_id, field="tag_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"标签不存在：{tag_id!r}") from None
    link, _created = await TestCaseTag.get_or_create(case_id=case.id, tag_id=tag.id)
    return link


async def untag_case(case_id: Any, tag_id: Any) -> int:
    case = await get_case(case_id)
    tag = await TestTag.get(id=_as_uuid(tag_id, field="tag_id"))
    return await TestCaseTag.filter(case_id=case.id, tag_id=tag.id).delete()


async def list_case_tags(case_id: Any) -> list[TestTag]:
    """用例的标签列表（供任务按 category 组合选例）。"""
    case = await get_case(case_id)
    links = await TestCaseTag.filter(case_id=case.id)
    tag_ids = [link.tag_id for link in links]
    if not tag_ids:
        return []
    return await TestTag.filter(id__in=tag_ids).order_by("category", "code")


async def list_case_scenarios(case_id: Any) -> list[TestCaseScenario]:
    case = await get_case(case_id)
    return await TestCaseScenario.filter(case_id=case.id).order_by("order", "scenario_id")


async def list_case_scenarios_full(case_id: Any) -> list[dict[str, Any]]:
    """用例↔场景关联 + 场景详情（§2.1）。

    :func:`list_case_scenarios` 只返链接行（``TestCaseScenario``）；本函数把
    关联行与 ``mc_test_scenario`` join 起来，每项同时携带链接语义
    （``id/case_id/scenario_id/is_primary/order``）与场景展示字段
    （``code/name/status``），供用例详情抽屉一次取全（免二次查场景列表）。
    场景已删时场景字段置 ``None``（关联行仍在，不报错）。
    """
    case = await get_case(case_id)
    links = await TestCaseScenario.filter(case_id=case.id).order_by("order", "scenario_id")
    if not links:
        return []
    scenario_ids = [link.scenario_id for link in links]
    scenarios = await TestScenario.filter(id__in=scenario_ids)
    by_id = {row.id: row for row in scenarios}
    documents: list[dict[str, Any]] = []
    for link in links:
        scenario = by_id.get(link.scenario_id)
        documents.append(
            {
                "id": str(link.id),
                "case_id": str(link.case_id),
                "scenario_id": str(link.scenario_id),
                "is_primary": bool(link.is_primary),
                "order": int(link.order or 0),
                "code": scenario.code if scenario else None,
                "name": scenario.name if scenario else None,
                "status": scenario.status if scenario else None,
            }
        )
    return documents


# ── 组装：引擎可读的场景文档 ─────────────────────────────────────
def _step_document(step: TestScenarioStep) -> dict[str, Any]:
    """步骤行 → 引擎 step dict（``executor._normalize_steps`` 可读）。

    只出 §4.1 的通用字段；``post_cond`` 不落列（本表无该字段），故不伪造。
    """
    return {
        "id": str(step.id),
        "step_key": step.step_key,
        "seq": step.seq,
        "name": step.name,
        "description": step.description or "",
        "type": step.type,
        "params": dict(step.params or {}),
        "pre_cond": list(step.pre_cond or []),
        "post_checks": list(step.post_checks or []),
        "post_cond": list(step.post_cond or []),
        "on_error": step.on_error,
        "enabled": bool(step.enabled),
    }


async def get_scenario_full(scenario_id: Any) -> dict[str, Any]:
    """场景 + 步骤 → 引擎 ``ScenarioLoader`` 需要的文档形态。

    * ``steps`` 按 ``seq`` 升序（顺序唯一真相）；
    * ``local_vars`` / ``flow`` 直接展平为 dict（JSONB 原样，供引擎读取）；
    * 另附 ``case_ids``（引用该场景的用例，§2.1 反向），供执行期填
      ``ExecutionContext.case_refs``。
    """
    scenario = await get_scenario(scenario_id)
    steps = await TestScenarioStep.filter(scenario_id=scenario.id).order_by("seq")
    links = await TestCaseScenario.filter(scenario_id=scenario.id).order_by("order")

    return {
        "id": str(scenario.id),
        "code": scenario.code,
        "project_id": str(scenario.project_id),
        "name": scenario.name,
        "purpose": scenario.purpose,
        "transport_id": str(scenario.transport_id) if scenario.transport_id else None,
        "env_id": str(scenario.env_id) if scenario.env_id else None,
        "folder_id": str(scenario.folder_id) if getattr(scenario, "folder_id", None) else None,
        "local_vars": dict(scenario.local_vars or {}),
        "flow": dict(scenario.flow or {}),
        "exec_mode": scenario.exec_mode,
        "status": scenario.status,
        "version": scenario.version,
        "case_ids": [str(link.case_id) for link in links],
        "steps": [_step_document(step) for step in steps],
    }


# ── 变更门（design §15.12）───────────────────────────────────────
async def record_change(
    entity_type: str,
    entity_id: Any,
    *,
    change_reason: str,
    created_by: Any = None,
    field_changes: Optional[Mapping[str, Any]] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
) -> TestChangeRecord:
    """写一条变更记录。``change_reason`` 为空即变更门拦截。``created_by`` = 变更人。"""
    _validate_enum(entity_type, CHANGE_ENTITY_TYPES, field="entity_type", required=True)
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError("change_reason 必填（一切变更 → 变更门，design 十八）")
    return await TestChangeRecord.create(
        entity_type=entity_type,
        entity_id=_as_uuid(entity_id, field="entity_id"),
        created_by=_maybe_uuid(created_by, field="created_by"),
        change_reason=str(change_reason).strip(),
        field_changes=_jsonify(dict(field_changes or {})),
        propagation=_jsonify(dict(propagation or {})),
        rollback_ref=_jsonify(rollback_ref) if rollback_ref is not None else None,
    )


async def _apply_update(
    instance: Any,
    *,
    entity_type: str,
    allowed: Iterable[str],
    values: Mapping[str, Any],
    change_reason: Optional[str],
    created_by: Any = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
) -> Any:
    """update 通用流程：变更门 → 白名单校验 → 赋值（先取 old 算 diff）→
    ``version+1`` → 落 ``mc_test_change_record``。``created_by`` = 变更人。"""
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            f"{entity_type}.update 必须提供 change_reason（design §15.12 变更门）"
        )
    allowed_set = set(allowed)
    unknown = [key for key in values if key not in allowed_set]
    if unknown:
        raise AssetValidationError(
            f"{entity_type} 不允许修改字段：{unknown}（可改：{sorted(allowed_set)}）"
        )
    if not values:
        raise AssetValidationError(f"{entity_type} update 未提供任何字段")

    # 字段级 diff：赋值前抓 old，只留真变了的字段（design §15.12 field_changes）
    field_changes: dict[str, dict[str, Any]] = {}
    for key, new_value in values.items():
        old_value = getattr(instance, key)
        if old_value != new_value:
            field_changes[key] = {"from": _jsonify(old_value), "to": _jsonify(new_value)}
        setattr(instance, key, new_value)

    instance.version = int(instance.version or 1) + 1
    await instance.save()

    await record_change(
        entity_type,
        instance.id,
        change_reason=change_reason,
        created_by=created_by,
        field_changes=field_changes,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )
    return instance


def _clean_case_steps(steps: Optional[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """用例步骤说明归一：每项 ``{seq, action, expected}``（自然语言，非可执行）。

    丢弃全空行；``seq`` 缺省按顺序补 1,2,3…；只保留这三个键（前端行模型对齐）。
    """
    if not steps:
        return []
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            continue
        action = str(raw.get("action") or "").strip()
        expected = str(raw.get("expected") or "").strip()
        if not action and not expected:
            continue
        seq = raw.get("seq")
        try:
            seq = int(seq) if seq is not None else len(out) + 1
        except (TypeError, ValueError):
            seq = len(out) + 1
        out.append({"seq": seq, "action": action, "expected": expected})
    return out


def _clean_case_preconditions(value: Any) -> list[str]:
    """前置条件归一为**字符串列表**（一行一条，2026-10-14 用户要「像步骤那样」）。

    列本就是 JSONB，只换值形状、无 DDL。**三兼容**：
    - ``list`` → 逐项 str、去空白、丢空串；
    - ``dict`` → 旧调用方/AI（``case_scan``、``skeleton``）仍可能给 ``{k: v}``，
      拍平成 ``"k: v"`` 列表（保信息不丢）；
    - ``str`` → 按换行拆；
    - 其它/None → ``[]``。
    """
    if value is None:
        return []
    if isinstance(value, Mapping):
        out: list[str] = []
        for key, val in value.items():
            text = str(val).strip() if val is not None else ""
            label = str(key).strip()
            line = f"{label}: {text}" if text else label
            if line.strip():
                out.append(line.strip())
        return out
    if isinstance(value, str):
        return [s.strip() for s in value.splitlines() if s.strip()]
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            if isinstance(item, Mapping):
                out.extend(_clean_case_preconditions(item))
                continue
            text = str(item).strip() if item is not None else ""
            if text:
                out.append(text)
        return out
    return []


def _clean_case_test_types(value: Any) -> list[str]:
    """多值类型归一 + 逐项枚举校验（非法值即拒）。None/空 → ``[]``。"""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in items:
        validated = _validate_enum(item, CASE_TEST_TYPES, field="test_types", required=True)
        if validated not in out:
            out.append(validated)
    return out


CASE_EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "intent",
    "preconditions",
    "expected",
    "steps",
    "dataset_id",
    "confirm_source",
    "confirm_ref",
    "origin",
    "priority",
    "module",
    "feature",
    "version_id",
    "requirement_id",
    "test_type",
    "test_types",
    "folder_id",
    "automation_status",
    "status",
)

SCENARIO_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "purpose",
    "transport_id",
    "env_id",
    "version_id",
    "requirement_id",
    "folder_id",
    "local_vars",
    "flow",
    "status",
)


async def update_case(
    case_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestCase:
    """改用例（§17.1 ``case.update``：必填 change_reason，触发传导）。"""
    case = await get_case(case_id)
    clean = _normalize_case_values(values)
    return await _apply_update(
        case,
        entity_type="case",
        allowed=CASE_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )


async def update_scenario(
    scenario_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestScenario:
    """改场景（§17.1 ``scenario.update``：记录原因；AI 改完须重过校验门 + 重跑）。"""
    scenario = await get_scenario(scenario_id)
    clean = _normalize_scenario_values(values)
    return await _apply_update(
        scenario,
        entity_type="scenario",
        allowed=SCENARIO_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )


def _normalize_case_values(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "confirm_source":
            value = _validate_enum(value, CASE_CONFIRM_SOURCES, field=key, required=True)
        elif key == "status":
            value = _validate_enum(value, CASE_STATUSES, field=key, required=True)
        elif key == "automation_status":
            value = _validate_enum(value, CASE_AUTOMATION_STATUSES, field=key, required=True)
        elif key == "priority" and value is not None:
            value = _validate_enum(value, CASE_PRIORITIES, field=key, required=True)
        elif key == "test_type" and value is not None:
            value = _validate_enum(value, CASE_TEST_TYPES, field=key, required=True)
        elif key == "origin":
            value = _validate_enum(value or "manual", CASE_ORIGINS, field=key, required=False) or "manual"
        elif key == "steps":
            value = _clean_case_steps(value)
        elif key == "preconditions":
            # 列表化（2026-10-14）：旧 dict / AI 产出由归一层兼容，统一存 string[]
            value = _clean_case_preconditions(value)
        elif key == "test_types":
            value = _clean_case_test_types(value)
        elif key in {"expected", "confirm_ref"}:
            value = dict(value or {})
        elif key in {"dataset_id", "version_id", "requirement_id", "folder_id"}:
            value = _maybe_uuid(value, field=key)
        out[key] = value
    return out


def _normalize_scenario_values(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "exec_mode":
            # 三模已停用（执行语义下沉到步骤类型）——静默丢弃，不写库、不报错。
            continue
        if key == "status":
            value = _validate_enum(value, SCENARIO_STATUSES, field=key, required=True)
        elif key in {"local_vars", "flow"}:
            value = dict(value or {})
        elif key in {"transport_id", "env_id", "version_id", "requirement_id", "folder_id"}:
            # folder_id 允许置 None（"" 或 null）= 归「未分组」（与 case/requirement 同语义）
            value = _maybe_uuid(value, field=key)
        out[key] = value
    return out


async def list_changes(
    entity_type: str,
    entity_id: Any,
) -> list[TestChangeRecord]:
    """某实体的变更历史（版本真相；可据此重建任意时刻状态）。"""
    return await TestChangeRecord.filter(
        entity_type=entity_type, entity_id=_as_uuid(entity_id, field="entity_id")
    ).order_by("-created_at")


# ── 传输层 / 接口管理（design §15.4）──────────────────────────
async def create_transport(
    *,
    project_id: Any,
    code: str,
    kind: str = "http",
    description: Optional[str] = None,
    module: Optional[str] = None,
    requirement_id: Optional[Any] = None,
    auth: Optional[Mapping[str, Any]] = None,
    common_headers: Optional[Mapping[str, Any]] = None,
    common_params: Optional[Mapping[str, Any]] = None,
    path_rewrite: Optional[Mapping[str, Any]] = None,
    status: str = "draft",
) -> TestTransport:
    """建传输层（接口管理）。``code`` 是稳定 ID（接口导入用 ``endpoint_<method>_<path>``）。"""
    if not code:
        raise AssetValidationError("传输层 code 必填")
    return await TestTransport.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=str(code),
        kind=_validate_enum(kind, TRANSPORT_KINDS, field="kind", required=False) or "http",
        description=description,
        module=module,
        requirement_id=_maybe_uuid(requirement_id, field="requirement_id"),
        auth=_jsonify(dict(auth or {})),
        common_headers=_jsonify(dict(common_headers or {})),
        common_params=_jsonify(dict(common_params or {})),
        path_rewrite=_jsonify(dict(path_rewrite or {})),
        status=_validate_enum(status, TRANSPORT_STATUSES, field="status", required=False) or "draft",
    )


async def get_transport(transport_id: Any) -> TestTransport:
    """按 UUID 主键或 ``code`` 取传输层。"""
    lookup: dict[str, Any]
    try:
        lookup = {"id": _as_uuid(transport_id, field="transport_id")}
    except AssetValidationError:
        lookup = {"code": str(transport_id)}
    try:
        return await TestTransport.get(**lookup)
    except DoesNotExist:
        raise AssetNotFoundError(f"传输层不存在：{transport_id!r}") from None


async def list_transports(
    project_id: Any, *, kind: Optional[str] = None, status: Optional[str] = None
) -> list[TestTransport]:
    """按项目列传输层（可按 kind/status 收窄），code 升序。"""
    qs = TestTransport.filter(project_id=_as_uuid(project_id, field="project_id"))
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("code")


TRANSPORT_EDITABLE_FIELDS: tuple[str, ...] = (
    "code",
    "kind",
    "description",
    "module",
    "requirement_id",
    "auth",
    "common_headers",
    "common_params",
    "path_rewrite",
    "status",
)


async def update_transport(
    transport_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestTransport:
    """改传输层（变更门强制）。"""
    transport = await get_transport(transport_id)
    clean = _normalize_transport_values(values)
    return await _apply_update(
        transport,
        entity_type="transport",
        allowed=TRANSPORT_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )


def _normalize_transport_values(values: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "kind":
            value = _validate_enum(value, TRANSPORT_KINDS, field=key, required=True)
        elif key == "status":
            value = _validate_enum(value, TRANSPORT_STATUSES, field=key, required=True)
        elif key in {"auth", "common_headers", "common_params", "path_rewrite"}:
            value = _jsonify(dict(value or {}))
        elif key == "requirement_id":
            value = _maybe_uuid(value, field=key)
        out[key] = value
    return out


async def delete_transport(transport_id: Any) -> int:
    """删除传输层，返回删除行数（0 = 不存在）。"""
    transport = await get_transport(transport_id)
    return await TestTransport.filter(id=transport.id).delete()


# ── 接口管理（mc_test_api / mc_test_api_folder）─────────────────
# 接口 = Apifox 式一等实体：method/path/params/headers/body/responses 全是
# **结构说明**（非具体值）；连接配置（鉴权/公共头/路径替换）归环境管，本表不存。
# **身份 = id（UUID），code 只是可读句柄**——接口分版本（version_id），同一
# 逻辑接口可跨版本多条，故 code 不做唯一键。

API_EDITABLE_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "method",
    "path",
    "protocol",
    "status",
    "description",
    "tags",
    "owner_id",
    "priority",
    "order",
    "params",
    "headers",
    "cookies",
    "body_type",
    "request_body",
    "responses",
    "requirement_id",
    "version_id",
    "folder_id",
)

FOLDER_EDITABLE_FIELDS: tuple[str, ...] = (
    "name",
    "parent_id",
    "order",
)


def _normalize_api_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """接口字段归一：枚举校验 + JSONB 归一 + UUID 引用转换。"""
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "method":
            value = _validate_enum(str(value).upper(), API_METHODS, field=key, required=True)
        elif key == "protocol":
            value = _validate_enum(value, API_PROTOCOLS, field=key, required=False) or "http"
        elif key == "status":
            value = _validate_enum(value, API_STATUSES, field=key, required=False) or "designing"
        elif key == "body_type":
            value = _validate_enum(value, API_BODY_TYPES, field=key, required=False) or "none"
        elif key == "source":
            value = _validate_enum(value, API_SOURCES, field=key, required=False) or "manual"
        elif key in {"params", "headers", "cookies", "responses", "tags"}:
            value = _jsonify(list(value or []))
        elif key in {"request_body", "source_ref"}:
            value = _jsonify(dict(value or {}))
        elif key in {"owner_id", "requirement_id", "version_id", "folder_id"}:
            value = _maybe_uuid(value, field=key)
        elif key == "priority":
            # P0-P3 可选；给了就必须在集合内（空/None = 不设）。
            if value not in (None, ""):
                value = _validate_enum(value, CASE_PRIORITIES, field=key, required=True)
        out[key] = value
    return out


async def _assert_folder_in_project(folder_id: Any, project_id: Any) -> None:
    """目录必须属于同一项目（防跨项目挂载）。"""
    folder = await TestApiFolder.get_or_none(id=_as_uuid(folder_id, field="folder_id"))
    if folder is None:
        raise AssetNotFoundError(f"接口目录不存在：{folder_id!r}")
    if str(folder.project_id) != str(_as_uuid(project_id, field="project_id")):
        raise AssetValidationError("接口目录与接口不属于同一项目")


async def create_api(
    *,
    project_id: Any,
    name: str,
    method: str,
    path: str,
    code: Optional[str] = None,
    folder_id: Optional[Any] = None,
    protocol: str = "http",
    status: str = "designing",
    description: Optional[str] = None,
    tags: Optional[Sequence[Any]] = None,
    owner_id: Optional[Any] = None,
    priority: Optional[str] = None,
    order: int = 0,
    params: Optional[Sequence[Mapping[str, Any]]] = None,
    headers: Optional[Sequence[Mapping[str, Any]]] = None,
    cookies: Optional[Sequence[Mapping[str, Any]]] = None,
    body_type: str = "none",
    request_body: Optional[Mapping[str, Any]] = None,
    responses: Optional[Sequence[Mapping[str, Any]]] = None,
    requirement_id: Optional[Any] = None,
    version_id: Optional[Any] = None,
    source: str = "manual",
    source_ref: Optional[Mapping[str, Any]] = None,
    created_by: Optional[Any] = None,
) -> TestApi:
    """建接口。``code`` 缺省走 ``API-`` 前缀自增（可读句柄，**非唯一键**）；身份是 id。"""
    if not name or not str(name).strip():
        raise AssetValidationError("接口 name 必填")
    pid = _as_uuid(project_id, field="project_id")
    if folder_id:
        await _assert_folder_in_project(folder_id, pid)
    # code 自增只在提供者为空时发生；同 code 不拒绝（身份是 id，code 可重复——
    # 接口分版本时同一逻辑接口会出现多条）。
    if not code:
        code = await _next_code(TestApi, pid, API_CODE_PREFIX)
    return await TestApi.create(
        project_id=pid,
        code=str(code),
        folder_id=_maybe_uuid(folder_id, field="folder_id"),
        name=str(name).strip(),
        method=_validate_enum(str(method).upper(), API_METHODS, field="method", required=True),
        path=str(path or "").strip() or "/",
        protocol=_validate_enum(protocol, API_PROTOCOLS, field="protocol", required=False) or "http",
        status=_validate_enum(status, API_STATUSES, field="status", required=False) or "designing",
        description=description,
        tags=_jsonify(list(tags or [])),
        owner_id=_maybe_uuid(owner_id, field="owner_id"),
        priority=priority or None,
        order=int(order or 0),
        params=_jsonify(list(params or [])),
        headers=_jsonify(list(headers or [])),
        cookies=_jsonify(list(cookies or [])),
        body_type=_validate_enum(body_type, API_BODY_TYPES, field="body_type", required=False) or "none",
        request_body=_jsonify(dict(request_body or {})),
        responses=_jsonify(list(responses or [])),
        requirement_id=_maybe_uuid(requirement_id, field="requirement_id"),
        version_id=_maybe_uuid(version_id, field="version_id"),
        source=_validate_enum(source, API_SOURCES, field="source", required=False) or "manual",
        source_ref=_jsonify(dict(source_ref or {})),
        created_by=_maybe_uuid(created_by, field="created_by"),
        updated_by=_maybe_uuid(created_by, field="created_by"),
    )


async def get_api(api_id: Any) -> TestApi:
    """按 UUID 主键取接口（**只认 id**；code 是句柄不是键，不参与查找）。"""
    try:
        return await TestApi.get(id=_as_uuid(api_id, field="api_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"接口不存在：{api_id!r}") from None


async def list_apis(
    project_id: Any,
    *,
    folder_id: Optional[Any] = None,
    method: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    keyword: Optional[str] = None,
) -> list[TestApi]:
    """按项目列接口（可按目录/方法/状态/标签/关键字收窄），目录内按 order、同级按 name。"""
    qs = TestApi.filter(project_id=_as_uuid(project_id, field="project_id"))
    if folder_id:
        qs = qs.filter(folder_id=_as_uuid(folder_id, field="folder_id"))
    if method:
        qs = qs.filter(method=str(method).upper())
    if status:
        qs = qs.filter(status=status)
    if keyword:
        kw = str(keyword).strip()
        if kw:
            qs = qs.filter(name__icontains=kw)
    rows = await qs.order_by("folder_id", "order", "name")
    if tag:
        want = str(tag).strip()
        rows = [r for r in rows if want in [str(t) for t in (r.tags or [])]]
    return rows


async def update_api(
    api_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    updated_by: Optional[Any] = None,
    **values: Any,
) -> TestApi:
    """改接口（变更门强制；``updated_by`` 记最后更新人，不进 diff）。"""
    api = await get_api(api_id)
    clean = _normalize_api_values(values)
    if "folder_id" in clean and clean["folder_id"]:
        await _assert_folder_in_project(clean["folder_id"], api.project_id)
    api.updated_by = _maybe_uuid(updated_by, field="updated_by")
    await api.save(update_fields=["updated_by"])
    clean.pop("updated_by", None)
    return await _apply_update(
        api,
        entity_type="api",
        allowed=API_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )


async def move_api(api_id: Any, *, folder_id: Optional[Any], order: int = 0) -> TestApi:
    """移动接口到目标目录（目录排序语义，不走变更门——不改变接口文档内容）。"""
    api = await get_api(api_id)
    if folder_id:
        await _assert_folder_in_project(folder_id, api.project_id)
    api.folder_id = _maybe_uuid(folder_id, field="folder_id")
    api.order = int(order or 0)
    await api.save(update_fields=["folder_id", "order"])
    return api


async def delete_api(api_id: Any) -> int:
    """删除接口，返回删除行数（0 = 不存在）。连带清掉关联索引行（防悬空）。"""
    api = await get_api(api_id)
    await TestApiScenario.filter(api_id=api.id).delete()
    await TestApiCase.filter(api_id=api.id).delete()
    return await TestApi.filter(id=api.id).delete()


# ── 接口目录（mc_test_api_folder）───────────────────────────────
async def create_folder(
    *,
    project_id: Any,
    name: str,
    parent_id: Optional[Any] = None,
    order: int = 0,
) -> TestApiFolder:
    """建目录（可嵌套）。父目录必须同项目。"""
    if not name or not str(name).strip():
        raise AssetValidationError("目录 name 必填")
    pid = _as_uuid(project_id, field="project_id")
    parent_uuid = _maybe_uuid(parent_id, field="parent_id")
    if parent_uuid is not None:
        parent = await TestApiFolder.get_or_none(id=parent_uuid)
        if parent is None:
            raise AssetNotFoundError(f"父目录不存在：{parent_id!r}")
        if str(parent.project_id) != str(pid):
            raise AssetValidationError("父目录与目录不属于同一项目")
    return await TestApiFolder.create(
        project_id=pid,
        parent_id=parent_uuid,
        name=str(name).strip(),
        order=int(order or 0),
    )


async def get_folder(folder_id: Any) -> TestApiFolder:
    try:
        return await TestApiFolder.get(id=_as_uuid(folder_id, field="folder_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"接口目录不存在：{folder_id!r}") from None


async def list_folders(project_id: Any) -> list[TestApiFolder]:
    """按项目列目录（parent_id, order, name 排序——前端组树）。"""
    return await TestApiFolder.filter(
        project_id=_as_uuid(project_id, field="project_id")
    ).order_by("parent_id", "order", "name")


async def update_folder(
    folder_id: Any,
    *,
    name: Optional[str] = None,
    parent_id: Optional[Any] = ...,  # type: ignore[assignment]
    order: Optional[int] = None,
) -> TestApiFolder:
    """改目录（改名/换父/排序）。换父做**防循环**校验（父链不得出现自身）。"""
    folder = await get_folder(folder_id)
    patch: dict[str, Any] = {}
    if name is not None:
        if not str(name).strip():
            raise AssetValidationError("目录 name 不能为空")
        patch["name"] = str(name).strip()
    if order is not None:
        patch["order"] = int(order)
    if parent_id is not ...:
        new_parent = _maybe_uuid(parent_id, field="parent_id")
        if new_parent is not None:
            if str(new_parent) == str(folder.id):
                raise AssetValidationError("目录不能把自己设为父目录")
            parent = await TestApiFolder.get_or_none(id=new_parent)
            if parent is None:
                raise AssetNotFoundError(f"父目录不存在：{new_parent!r}")
            if str(parent.project_id) != str(folder.project_id):
                raise AssetValidationError("父目录与目录不属于同一项目")
            # 防循环：沿新父链向上走，遇到自己即成环。
            cursor: Optional[TestApiFolder] = parent
            while cursor is not None:
                if str(cursor.id) == str(folder.id):
                    raise AssetValidationError("目录不能挂到自己的子孙目录下（会成环）")
                cursor = await TestApiFolder.get_or_none(id=cursor.parent_id) if cursor.parent_id else None
        patch["parent_id"] = new_parent
    if not patch:
        return folder
    for key, value in patch.items():
        setattr(folder, key, value)
    await folder.save()
    return folder


async def delete_folder(folder_id: Any) -> int:
    """删目录（级联删子目录；挂在其下的接口 folder_id 由 FK SET NULL 归根）。"""
    folder = await get_folder(folder_id)
    return await TestApiFolder.filter(id=folder.id).delete()


# ── 统一目录（mc_test_catalog：需求/用例/场景/接口共用一张，type 区分）────
# idiom 照 TestApiFolder 的一套（create/get/list/update/delete）；差别：
# ① 所有函数带 ``type`` 校验（父目录类型必须与子一致，防跨类型挂错）；
# ② 删目录时**显式**把挂的用例 folder_id 置 NULL（mc_test_case.folder_id 是裸
#    UUID 无 FK，DB 层不会自动 SET NULL）。

async def create_catalog(
    *,
    project_id: Any,
    type: str,
    name: str,
    parent_id: Optional[Any] = None,
    order: int = 0,
) -> TestCatalog:
    """建目录。``type`` 必须在 :data:`CATALOG_TYPES` 内；父目录必须同项目**同 type**。"""
    if not name or not str(name).strip():
        raise AssetValidationError("目录 name 必填")
    if type not in CATALOG_TYPES:
        raise AssetValidationError(f"目录 type={type!r} 不在允许集合 {list(CATALOG_TYPES)} 内")
    pid = _as_uuid(project_id, field="project_id")
    parent_uuid = _maybe_uuid(parent_id, field="parent_id")
    if parent_uuid is not None:
        parent = await TestCatalog.get_or_none(id=parent_uuid)
        if parent is None:
            raise AssetNotFoundError(f"父目录不存在：{parent_id!r}")
        if str(parent.project_id) != str(pid):
            raise AssetValidationError("父目录与目录不属于同一项目")
        if parent.type != type:
            raise AssetValidationError(
                f"父目录 type={parent.type!r} 与目录 type={type!r} 不一致（跨类型挂靠禁止）"
            )
    return await TestCatalog.create(
        project_id=pid,
        type=type,
        parent_id=parent_uuid,
        name=str(name).strip(),
        order=int(order or 0),
    )


async def get_catalog(folder_id: Any) -> TestCatalog:
    try:
        return await TestCatalog.get(id=_as_uuid(folder_id, field="folder_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"目录不存在：{folder_id!r}") from None


async def list_catalogs(project_id: Any, type: Optional[str] = None) -> list[TestCatalog]:
    """按项目（可选 type 收窄）列目录（parent_id, order, name 排序——前端组树）。"""
    qs = TestCatalog.filter(project_id=_as_uuid(project_id, field="project_id"))
    if type:
        if type not in CATALOG_TYPES:
            raise AssetValidationError(f"目录 type={type!r} 不在允许集合 {list(CATALOG_TYPES)} 内")
        qs = qs.filter(type=type)
    return await qs.order_by("parent_id", "order", "name")


async def update_catalog(
    folder_id: Any,
    *,
    name: Optional[str] = None,
    parent_id: Optional[Any] = ...,  # type: ignore[assignment]
    order: Optional[int] = None,
) -> TestCatalog:
    """改目录（改名/换父/排序）。换父做**防循环**校验 + 父目录**同 type** 校验。"""
    folder = await get_catalog(folder_id)
    patch: dict[str, Any] = {}
    if name is not None:
        if not str(name).strip():
            raise AssetValidationError("目录 name 不能为空")
        patch["name"] = str(name).strip()
    if order is not None:
        patch["order"] = int(order)
    if parent_id is not ...:
        new_parent = _maybe_uuid(parent_id, field="parent_id")
        if new_parent is not None:
            if str(new_parent) == str(folder.id):
                raise AssetValidationError("目录不能把自己设为父目录")
            parent = await TestCatalog.get_or_none(id=new_parent)
            if parent is None:
                raise AssetNotFoundError(f"父目录不存在：{new_parent!r}")
            if str(parent.project_id) != str(folder.project_id):
                raise AssetValidationError("父目录与目录不属于同一项目")
            if parent.type != folder.type:
                raise AssetValidationError(
                    f"父目录 type={parent.type!r} 与目录 type={folder.type!r} 不一致（跨类型挂靠禁止）"
                )
            # 防循环：沿新父链向上走，遇到自己即成环。
            cursor: Optional[TestCatalog] = parent
            while cursor is not None:
                if str(cursor.id) == str(folder.id):
                    raise AssetValidationError("目录不能挂到自己的子孙目录下（会成环）")
                cursor = await TestCatalog.get_or_none(id=cursor.parent_id) if cursor.parent_id else None
        patch["parent_id"] = new_parent
    if not patch:
        return folder
    for key, value in patch.items():
        setattr(folder, key, value)
    await folder.save()
    return folder


async def delete_catalog(folder_id: Any) -> int:
    """删目录（级联删子目录；挂的实体 ``folder_id`` 显式置 NULL 归根——无 FK 约束）。

    按 ``type`` 分支处理挂载实体（case→``mc_test_case`` / requirement→
    ``mc_test_requirement`` / scenario→``mc_test_scenario`` / bug→``mc_test_bug``）。
    """
    folder = await get_catalog(folder_id)
    # 收集本目录 + 全部子孙 id（BFS）
    ids = [folder.id]
    frontier = [folder.id]
    while frontier:
        children = await TestCatalog.filter(parent_id__in=frontier).values_list("id", flat=True)
        child_ids = list(children)
        child_ids = [c for c in child_ids if c not in ids]
        if not child_ids:
            break
        ids.extend(child_ids)
        frontier = child_ids
    if folder.type == "case":
        await TestCase.filter(folder_id__in=ids).update(folder_id=None)
    elif folder.type == "requirement":
        await TestRequirement.filter(folder_id__in=ids).update(folder_id=None)
    elif folder.type == "scenario":
        await TestScenario.filter(folder_id__in=ids).update(folder_id=None)
    elif folder.type == "bug":
        # 缺陷主表在 defects 域（跨包，且本包不被 defects 反向依赖，函数内导入零环）。
        from ..defects.models import TestBug

        await TestBug.filter(folder_id__in=ids).update(folder_id=None)
    return await TestCatalog.filter(id__in=ids).delete()


# ── 接口↔用例 / 接口↔场景 多对多（P5 直连）──────────────────────
# 步骤引用接口走 params.api_id；这两张关联表存**显式关联**，供接口详情
# 反查「哪些用例覆盖我 / 哪些场景在用我」（影响面）与用例侧聚合。
async def link_api_case(
    api_id: Any,
    case_id: Any,
    *,
    order: int = 0,
) -> TestApiCase:
    """接口↔用例关联（幂等：重复链接更新 order 返回既有行）。"""
    api = await get_api(api_id)
    case = await get_case(case_id)
    if not isinstance(order, int):
        raise AssetValidationError(f"order 必须是整数：{order!r}")
    link = await TestApiCase.filter(api_id=api.id, case_id=case.id).first()
    if link is None:
        return await TestApiCase.create(api_id=api.id, case_id=case.id, order=order)
    if link.order != order:
        link.order = order
        await link.save()
    return link


async def unlink_api_case(api_id: Any, case_id: Any) -> int:
    """解除接口↔用例关联，返回删除行数。"""
    api = await get_api(api_id)
    case = await get_case(case_id)
    return await TestApiCase.filter(api_id=api.id, case_id=case.id).delete()


async def list_api_cases(api_id: Any) -> list[TestCase]:
    """引用该接口的用例（按关联 order 排）。"""
    api = await get_api(api_id)
    links = await TestApiCase.filter(api_id=api.id).order_by("order", "case_id")
    if not links:
        return []
    case_ids = [link.case_id for link in links]
    rows = await TestCase.filter(id__in=case_ids)
    by_id = {row.id: row for row in rows}
    return [by_id[cid] for cid in case_ids if cid in by_id]


async def link_api_scenario(
    api_id: Any,
    scenario_id: Any,
    *,
    order: int = 0,
) -> TestApiScenario:
    """接口↔场景关联（幂等：重复链接更新 order 返回既有行）。"""
    api = await get_api(api_id)
    scenario = await get_scenario(scenario_id)
    if not isinstance(order, int):
        raise AssetValidationError(f"order 必须是整数：{order!r}")
    link = await TestApiScenario.filter(api_id=api.id, scenario_id=scenario.id).first()
    if link is None:
        return await TestApiScenario.create(api_id=api.id, scenario_id=scenario.id, order=order)
    if link.order != order:
        link.order = order
        await link.save()
    return link


async def unlink_api_scenario(api_id: Any, scenario_id: Any) -> int:
    """解除接口↔场景关联，返回删除行数。"""
    api = await get_api(api_id)
    scenario = await get_scenario(scenario_id)
    return await TestApiScenario.filter(api_id=api.id, scenario_id=scenario.id).delete()


async def list_api_scenarios(api_id: Any) -> list[TestScenario]:
    """引用该接口的场景（按关联 order 排；供接口详情「被哪些场景引用」面板）。"""
    api = await get_api(api_id)
    links = await TestApiScenario.filter(api_id=api.id).order_by("order", "scenario_id")
    if not links:
        return []
    scenario_ids = [link.scenario_id for link in links]
    rows = await TestScenario.filter(id__in=scenario_ids)
    by_id = {row.id: row for row in rows}
    return [by_id[sid] for sid in scenario_ids if sid in by_id]


# ── 关联索引：派生（真相源 = 步骤 params.api_id）──────────────────
# ``mc_test_api_scenario`` / ``mc_test_api_case`` 是**反查索引**（供接口详情
# 「被哪些场景/用例引用」与影响面），不是手工维护的关联。真相源是步骤里的
# ``params.api_id``；用例侧再沿「用例→场景→步骤」传递。每次步骤/绑定落库后
# 调同步，保持索引与真相一致。
def _api_id_of_step(step: Any) -> Optional[_uuid.UUID]:
    """步骤 ``params.api_id`` → UUID（无/非法返回 None，不建关联）。"""
    params = step.params if isinstance(getattr(step, "params", None), Mapping) else {}
    raw = params.get("api_id")
    if not raw:
        return None
    try:
        return _as_uuid(raw, field="api_id")
    except AssetValidationError:
        return None


async def sync_scenario_api_links(scenario_id: Any) -> list[_uuid.UUID]:
    """按步骤 ``params.api_id`` 重建 场景↔接口 索引（返回关联的 api_id 列表）。

    * 去重（一个场景多次引用同一接口只留一条，按首次出现定 ``order``）；
    * 只保留**真实存在**的接口（防悬空引用）；
    * 幂等：全删重建（索引可无损重算）。
    """
    scenario = await get_scenario(scenario_id)
    steps = await TestScenarioStep.filter(scenario_id=scenario.id).order_by("seq")
    wanted: list[_uuid.UUID] = []
    seen: set[str] = set()
    for step in steps:
        api_id = _api_id_of_step(step)
        if api_id is None or str(api_id) in seen:
            continue
        seen.add(str(api_id))
        wanted.append(api_id)
    if wanted:
        alive = set(await TestApi.filter(id__in=wanted).values_list("id", flat=True))
        wanted = [api_id for api_id in wanted if api_id in alive]
    await TestApiScenario.filter(scenario_id=scenario.id).delete()
    for order, api_id in enumerate(wanted):
        await TestApiScenario.create(api_id=api_id, scenario_id=scenario.id, order=order)
    return wanted


async def sync_case_api_links(case_id: Any) -> list[_uuid.UUID]:
    """按用例关联的场景的接口集合重建 用例↔接口 索引（返回 api_id 列表）。

    用例「覆盖」的接口 = 其所有关联场景所引用接口的并集（沿 场景↔接口 索引取）。
    """
    case = await get_case(case_id)
    scenario_ids = list(
        await TestCaseScenario.filter(case_id=case.id).values_list("scenario_id", flat=True)
    )
    wanted: list[_uuid.UUID] = []
    seen: set[str] = set()
    if scenario_ids:
        api_links = await TestApiScenario.filter(scenario_id__in=scenario_ids).order_by(
            "scenario_id", "order"
        )
        for link in api_links:
            if str(link.api_id) in seen:
                continue
            seen.add(str(link.api_id))
            wanted.append(link.api_id)
    await TestApiCase.filter(case_id=case.id).delete()
    for order, api_id in enumerate(wanted):
        await TestApiCase.create(api_id=api_id, case_id=case.id, order=order)
    return wanted


async def sync_cases_of_scenario(scenario_id: Any) -> None:
    """场景引用接口变化后，回灌到所有关联该场景的用例（重建用例↔接口索引）。"""
    scenario = await get_scenario(scenario_id)
    case_ids = list(
        await TestCaseScenario.filter(scenario_id=scenario.id).values_list("case_id", flat=True)
    )
    for case_id in case_ids:
        await sync_case_api_links(case_id)


async def list_scenario_apis(scenario_id: Any) -> list[TestApi]:
    """该场景引用的接口（按 order；供场景信息卡「引用接口」）。"""
    scenario = await get_scenario(scenario_id)
    links = await TestApiScenario.filter(scenario_id=scenario.id).order_by("order", "api_id")
    if not links:
        return []
    api_ids = [link.api_id for link in links]
    rows = await TestApi.filter(id__in=api_ids)
    by_id = {row.id: row for row in rows}
    return [by_id[aid] for aid in api_ids if aid in by_id]


async def list_case_apis(case_id: Any) -> list[TestApi]:
    """该用例覆盖的接口（沿关联场景派生；供用例详情「覆盖接口」）。"""
    case = await get_case(case_id)
    links = await TestApiCase.filter(case_id=case.id).order_by("order", "api_id")
    if not links:
        return []
    api_ids = [link.api_id for link in links]
    rows = await TestApi.filter(id__in=api_ids)
    by_id = {row.id: row for row in rows}
    return [by_id[aid] for aid in api_ids if aid in by_id]


# ── 方法脚本「类」/ 方法注册表（§5.1 重构：一个脚本 = 一个类）──────
#: ``mc_test_method_module`` 可改字段（update 白名单；变更门走 _apply_update）。
METHOD_MODULE_EDITABLE_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "description",
    "source",
    "async_native",
    "status",
)


async def create_method_module(
    *,
    code: str = "",
    name: Optional[str] = None,
    project_id: Optional[Any] = None,
    description: Optional[str] = None,
    source: str = "",
    async_native: bool = False,
    status: str = "draft",
) -> TestMethodModule:
    """建方法脚本「类」。``source`` 必须恰好一个类（AST 校验）。

    ``code`` / ``name`` 取源码里解析出的**类名**（约定 code=类名，运行期按类名
    取命名空间里的类）；``description`` 取类 docstring（= 类备注）。
    ``project_id=null`` = 全局内置类，有值 = 项目类。
    """
    parsed = _parse_source_or_raise(source)
    class_name = parsed.name if parsed else (str(code).strip() or "")
    if not class_name:
        raise AssetValidationError("方法类需要 code（=类名）或含类定义的 source")
    if parsed:
        code, name, description = parsed.name, parsed.name, parsed.docstring or None
    pid = _maybe_uuid(project_id, field="project_id")
    # 同作用域（同 project_id，含全局）类名唯一——重名会让运行期命名空间互相覆盖。
    if await TestMethodModule.filter(project_id=pid, code=class_name).exists():
        raise AssetValidationError(f"同类名的方法类已存在：{class_name!r}（同作用域内类名必须唯一）")
    module = await TestMethodModule.create(
        project_id=pid,
        code=class_name,
        name=name or class_name,
        description=description,
        source=source or "",
        async_native=bool(async_native),
        status=_validate_enum(
            status, METHOD_MODULE_STATUSES, field="status", required=False
        )
        or "draft",
    )
    await sync_methods_from_source(module)
    await module.refresh_from_db()
    return module


async def update_method_module(
    module_id: Any,
    *,
    change_reason: Optional[str] = None,
    source: Optional[str] = None,
    created_by: Any = None,
    **values: Any,
) -> TestMethodModule:
    """改方法类（变更门强制）。``source`` 变更 → AST 重解析并同步 code/name/
    description + 重建方法注册表（design §15.12 变更门 + §5.1）。``created_by`` = 变更人。"""
    module = await get_method_module(module_id)
    clean = dict(values)
    if source is not None:
        parsed = _parse_source_or_raise(source)
        if parsed:
            new_code = parsed.name
            same_scope = module.project_id
            clash = await TestMethodModule.filter(project_id=same_scope, code=new_code).exclude(
                id=module.id
            ).exists()
            if clash:
                raise AssetValidationError(
                    f"同类名的方法类已存在：{new_code!r}（同作用域内类名必须唯一）"
                )
            clean["source"] = source
            clean["code"] = new_code
            clean["name"] = new_code
            clean["description"] = parsed.docstring or None
    if not clean:
        raise AssetValidationError("method_module.update 未提供任何字段")
    updated = await _apply_update(
        module,
        entity_type="method_module",
        allowed=METHOD_MODULE_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        created_by=created_by,
    )
    if source is not None:
        await sync_methods_from_source(updated)
        await updated.refresh_from_db()
    return updated


def _parse_source_or_raise(source: Optional[str]):
    """解析方法脚本源码（一个类）。空 source → None（允许先建空类壳，后补代码）。

    语法错误 / 多类 / 非静态方法 → 转 :class:`AssetValidationError`（HTTP 层 422 可读）。
    """
    if not source or not str(source).strip():
        return None
    from ..method_source import MethodSourceError, parse_class_source

    try:
        return parse_class_source(source)
    except MethodSourceError as exc:
        raise AssetValidationError(str(exc)) from exc


async def sync_methods_from_source(module: TestMethodModule) -> list[TestMethod]:
    """按 ``module.source`` 的 AST 解析结果 upsert ``mc_test_method``，删旧方法。

    ``full_ref = <类名>.<方法名>``；``is_async`` / ``signature``（入参说明）/
    ``description``（方法 docstring）皆来自 AST。源码为空 → 清空该模块方法。
    """
    parsed = _parse_source_or_raise(module.source)
    want = {m.name: m for m in (parsed.methods if parsed else [])}
    existing = {row.name: row for row in await TestMethod.filter(module=module)}
    # 删：源码里已不存在的方法
    stale = [name for name in existing if name not in want]
    if stale:
        await TestMethod.filter(module=module, name__in=stale).delete()
    out: list[TestMethod] = []
    for name, pm in want.items():
        fields = {
            "full_ref": f"{module.code}.{name}",
            "signature": pm.signature,
            "is_async": pm.is_async,
            "description": pm.docstring or None,
        }
        row = existing.get(name)
        if row is None:
            out.append(await TestMethod.create(module=module, name=name, **fields))
        else:
            for key, value in fields.items():
                setattr(row, key, _jsonify(value) if key == "signature" else value)
            await row.save()
            out.append(row)
    return out


async def get_method_module(module_id: Any) -> TestMethodModule:
    """按 UUID 主键或 ``code``（=类名）取方法类。"""
    lookup: dict[str, Any]
    try:
        lookup = {"id": _as_uuid(module_id, field="module_id")}
    except AssetValidationError:
        lookup = {"code": str(module_id)}
    try:
        return await TestMethodModule.get(**lookup)
    except DoesNotExist:
        raise AssetNotFoundError(f"方法类不存在：{module_id!r}") from None


async def resolve_method_module(code: str, project_id: Any) -> TestMethodModule:
    """按 code（类名）解析：**先本项目，再全局**。同作用域类名已保证唯一。"""
    pid = _maybe_uuid(project_id, field="project_id")
    if pid is not None:
        found = await TestMethodModule.filter(project_id=pid, code=str(code)).first()
        if found is not None:
            return found
    global_row = await TestMethodModule.filter(project_id=None, code=str(code)).first()
    if global_row is None:
        raise AssetNotFoundError(f"方法类不存在：{code!r}")
    return global_row


async def list_method_modules(
    project_id: Optional[Any] = None,
) -> list[TestMethodModule]:
    """列方法类。``project_id=None`` 只列全局类；给定时列该项目 + 全局（§5.1）。"""
    qs = TestMethodModule.all()
    if project_id is not None:
        pid = _as_uuid(project_id, field="project_id")
        qs = qs.filter(models_q_project(pid))
    else:
        qs = qs.filter(project_id=None)
    return await qs.order_by("code")


async def delete_method_module(module_id: Any) -> int:
    """删方法类（级联删其方法注册行，FK ON DELETE CASCADE）。"""
    module = await get_method_module(module_id)
    return await TestMethodModule.filter(id=module.id).delete()


def models_q_project(pid: Any) -> Any:
    """``project_id = pid OR project_id IS NULL``（全局 + 本项目）。"""
    from tortoise.expressions import Q

    return Q(project_id=pid) | Q(project_id=None)


async def create_method(
    *,
    module_id: Any,
    name: str,
    signature: Optional[Mapping[str, Any]] = None,
    full_ref: Optional[str] = None,
    is_async: bool = False,
    description: Optional[str] = None,
) -> TestMethod:
    """在类内注册方法。``full_ref`` 缺省取 ``<类名>.<name>``（§5.1）。"""
    if not name:
        raise AssetValidationError("方法 name 必填")
    module = await get_method_module(module_id)
    return await TestMethod.create(
        module=module,
        name=str(name),
        full_ref=full_ref or f"{module.code}.{name}",
        signature=_jsonify(dict(signature or {})),
        is_async=bool(is_async),
        description=description,
    )


async def list_class_methods(module_id: Optional[Any] = None) -> list[TestMethod]:
    """列方法（可按类收窄），``full_ref`` 升序。

    原名 ``list_methods``——与 surface 的方法注册表投影 ``list_methods()`` 同名，
    在 surface 模块内被遮蔽导致 ``method.list`` 永远 TypeError，故改名。
    """
    qs = TestMethod.all()
    if module_id is not None:
        module = await get_method_module(module_id)
        qs = qs.filter(module=module)
    return await qs.order_by("full_ref")


# 向后兼容：老调用方（tests/test_assets_missing.py 等）继续可用
list_methods = list_class_methods


async def list_method_classes(
    project_id: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """类目录（对外检索主入口）：类 + 备注 + **展开方法**（full_ref/说明/入参说明）。

    一次查齐（module 表 + method 表各一查），服务方法库页「类列表直接展开看
    方法」与场景编辑器/AI 的方法目录检索。``project_id=None`` 只列全局类。
    """
    modules = await list_method_modules(project_id)
    if not modules:
        return []
    by_module: dict[Any, list[TestMethod]] = {}
    for row in await TestMethod.filter(module_id__in=[m.id for m in modules]).order_by("full_ref"):
        by_module.setdefault(row.module_id, []).append(row)

    def _method_doc(row: TestMethod) -> dict[str, Any]:
        """方法注册行 → JSON 文档（full_ref/说明/入参说明，检索展示用）。"""
        return {
            "id": str(row.id),
            "module_id": str(row.module_id),
            "name": row.name,
            "full_ref": row.full_ref,
            "signature": dict(row.signature or {}),
            "is_async": bool(row.is_async),
            "description": row.description,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    return [
        {
            "id": str(m.id),
            "code": m.code,
            "name": m.name,
            "description": m.description,
            "status": m.status,
            "version": m.version,
            "methods": [_method_doc(row) for row in by_module.get(m.id, [])],
        }
        for m in modules
    ]


# ── 数据集（§6 / design §15.4）─────────────────────────────────
async def create_dataset(
    *,
    project_id: Any,
    code: str,
    schema: Optional[Mapping[str, Any]] = None,
    rows: Optional[Any] = None,
) -> TestDataset:
    """建数据集（参数化数据：schema 定义字段类型，rows 是多组数据）。"""
    if not code:
        raise AssetValidationError("数据集 code 必填")
    return await TestDataset.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=str(code),
        schema=_jsonify(dict(schema or {})),
        rows=_jsonify(list(rows or [])),
    )


async def get_dataset(dataset_id: Any) -> TestDataset:
    """按 UUID 主键或 ``code`` 取数据集。"""
    lookup: dict[str, Any]
    try:
        lookup = {"id": _as_uuid(dataset_id, field="dataset_id")}
    except AssetValidationError:
        lookup = {"code": str(dataset_id)}
    try:
        return await TestDataset.get(**lookup)
    except DoesNotExist:
        raise AssetNotFoundError(f"数据集不存在：{dataset_id!r}") from None


async def list_datasets(project_id: Any) -> list[TestDataset]:
    """按项目列数据集，code 升序。"""
    qs = TestDataset.filter(project_id=_as_uuid(project_id, field="project_id"))
    return await qs.order_by("code")


DATASET_EDITABLE_FIELDS: tuple[str, ...] = ("code", "schema", "rows")


async def update_dataset(
    dataset_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestDataset:
    """改数据集（变更门强制）。"""
    dataset = await get_dataset(dataset_id)
    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key == "schema":
            value = _jsonify(dict(value or {}))
        elif key == "rows":
            value = _jsonify(list(value or []))
        clean[key] = value
    return await _apply_update(
        dataset,
        entity_type="dataset",
        allowed=DATASET_EDITABLE_FIELDS,
        values=clean,
        change_reason=change_reason,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )


async def delete_dataset(dataset_id: Any) -> int:
    """删数据集：把挂到它的用例 ``dataset_id`` 置空，再删数据集行。

    照 :func:`delete_requirement` idiom——不级联删用例（用例本体仍有效，
    只是不再绑定该份测试数据）。
    """
    dataset = await get_dataset(dataset_id)
    await TestCase.filter(dataset_id=dataset.id).update(dataset_id=None)
    return await TestDataset.filter(id=dataset.id).delete()


# ── 环境管理（design §15.4 / 05：分层继承 + 密钥脱敏）────────────
#: 密钥脱敏掩码（05 §5：读取接口脱敏）
SECRET_MASK = "***"


def mask_secret_value(value: Any) -> Any:
    """密钥变量读取时脱敏：字符串保留末 4 位，其余一律 ``***``。

    ``is_secret=true`` 的变量在**读取接口**返回脱敏形态（05 §5），
    引擎执行时由服务端注入真值（本函数只用于读面/日志面）。
    """
    if isinstance(value, str) and value:
        return f"{SECRET_MASK}{value[-4:]}"
    return SECRET_MASK


class EnvironmentCycleError(AssetValidationError):
    """环境继承成环：父链出现自身（05 §3「防循环继承」）。

    写入时校验（创建/修改 ``parent_id``），父链不得出现自身。
    """


class EnvironmentNotFoundError(AssetNotFoundError):
    """环境不存在。"""


ENVIRONMENT_EDITABLE_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "description",
    "parent_id",
    "is_default",
    "status",
)


async def create_environment(
    *,
    project_id: Any,
    code: str,
    name: str,
    parent_id: Optional[Any] = None,
    description: Optional[str] = None,
    is_default: bool = False,
    status: str = "active",
    created_by: Optional[Any] = None,
) -> TestEnvironment:
    """建环境（05 §2.1）。``parent_id`` 空 = 根基线，非空 = 环境组/具体环境。

    **防循环继承**：写入时从 ``parent_id`` 向上回溯，若父链出现
    新建环境自身即拒绝（本函数新建无 id，故只校验父链本身不重复 +
    父环境必须属于同一项目）。
    """
    pid = _as_uuid(project_id, field="project_id")
    if not code:
        raise AssetValidationError("环境 code 必填（dev / staging / prod_sim）")
    if not name:
        raise AssetValidationError("环境 name 必填")
    env_status = _validate_enum(status, ENVIRONMENT_STATUSES, field="status", required=False) or "active"

    parent: Optional[TestEnvironment] = None
    if parent_id not in (None, ""):
        parent = await get_environment(parent_id)
        if str(parent.project_id) != str(pid):
            raise AssetValidationError("环境父级必须属于同一项目（分层继承按项目隔离）")
        # 父链本身不允许重复（新建环境还没有 id，只要父链无环即可）
        await _assert_parent_chain_acyclic(parent, None)

    env = await TestEnvironment.create(
        project_id=pid,
        code=str(code),
        name=name,
        parent=parent,
        description=description,
        is_default=bool(is_default),
        status=env_status,
        created_by=_maybe_uuid(created_by, field="created_by"),
    )
    if is_default:
        await _clear_other_defaults(pid, exclude_id=env.id)
    return env


async def get_environment(env_id: Any) -> TestEnvironment:
    """按 UUID 主键或 ``code`` 取环境。"""
    lookup: dict[str, Any]
    try:
        lookup = {"id": _as_uuid(env_id, field="env_id")}
    except AssetValidationError:
        lookup = {"code": str(env_id)}
    try:
        return await TestEnvironment.get(**lookup)
    except DoesNotExist:
        raise EnvironmentNotFoundError(f"环境不存在：{env_id!r}") from None


async def list_environments(
    project_id: Any, *, status: Optional[str] = None, include_archived: bool = False
) -> list[TestEnvironment]:
    """按项目列环境（按 code 升序）。

    ``status=None`` 默认只列 ``active``；要含 archived 传 ``include_archived=True``
    或显式 ``status="archived"``。
    """
    pid = _as_uuid(project_id, field="project_id")
    qs = TestEnvironment.filter(project_id=pid)
    if status:
        qs = qs.filter(status=_validate_enum(status, ENVIRONMENT_STATUSES, field="status", required=True))
    elif not include_archived:
        qs = qs.filter(status="active")
    return await qs.order_by("code")


async def _assert_parent_chain_acyclic(env: TestEnvironment, target_id: Any) -> None:
    """从 ``env`` 沿 parent 链向上回溯，防成环（05 §3）。

    ``target_id`` 是待写入环境的 id（修改时是自身 id）——若父链上出现
    ``target_id`` 即 ``target_id`` 会变成自己的祖先 → 环。
    """
    seen: set[str] = set()
    cur: Optional[TestEnvironment] = env
    while cur is not None:
        cur_id = str(cur.id)
        if target_id is not None and cur_id == str(target_id):
            raise EnvironmentCycleError(
                f"环境继承成环被拒：父链出现自身 {target_id!r}（05 §3 防循环）"
            )
        if cur_id in seen:
            raise EnvironmentCycleError(f"环境继承父链本身成环：{cur_id!r}")
        seen.add(cur_id)
        if cur.parent_id is None:
            break
        try:
            cur = await TestEnvironment.get(id=cur.parent_id)
        except DoesNotExist:
            raise EnvironmentNotFoundError(
                f"环境 {cur_id!r} 的父环境不存在：{cur.parent_id!r}"
            ) from None


async def _validate_new_parent(env: TestEnvironment, parent_id: Any) -> Optional[TestEnvironment]:
    """更新 parent_id 时：解析父、校验同项目、校验不形成环。"""
    if parent_id in (None, ""):
        await _assert_parent_chain_acyclic(env, None)  # 只是清父，无环风险（仍校验父链完整）
        return None
    parent = await get_environment(parent_id)
    if str(parent.project_id) != str(env.project_id):
        raise AssetValidationError("环境父级必须属于同一项目（分层继承按项目隔离）")
    await _assert_parent_chain_acyclic(parent, str(env.id))
    return parent


async def update_environment(
    env_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestEnvironment:
    """改环境（05 §6 编辑）。**变更门**：``change_reason`` 必填。

    ``parent_id`` 变更会先做**防循环校验**（父链不得出现自身）。
    """
    env = await get_environment(env_id)
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "environment.update 必须提供 change_reason（design §15.12 变更门）"
        )

    # 白名单 + 归一化 + 防循环
    allowed_set = set(ENVIRONMENT_EDITABLE_FIELDS)
    unknown = [key for key in values if key not in allowed_set]
    if unknown:
        raise AssetValidationError(
            f"环境不允许修改字段：{unknown}（可改：{sorted(allowed_set)}）"
        )

    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key == "status":
            value = _validate_enum(value, ENVIRONMENT_STATUSES, field=key, required=False)
        elif key == "parent_id":
            parent = await _validate_new_parent(env, value)
            # Tortoise FK：setattr 时直接写 parent_id 列即可
            clean["parent_id"] = parent.id if parent else None
            continue
        elif key == "is_default":
            value = bool(value)
        clean[key] = value

    if not clean:
        raise AssetValidationError("environment.update 未提供任何字段")

    # 记旧默认，便于变更后重排
    was_default = bool(env.is_default)
    updated = await _apply_update(
        env,
        entity_type="environment",
        allowed=ENVIRONMENT_EDITABLE_FIELDS,
        values=clean,
        change_reason=str(change_reason).strip(),
        propagation=propagation,
        rollback_ref=rollback_ref,
    )
    if values.get("is_default") and not was_default:
        await _clear_other_defaults(env.project_id, exclude_id=env.id)
    return updated


async def _clear_other_defaults(project_id: Any, exclude_id: Any) -> None:
    """同项目内其它环境的 is_default 清掉（默认环境项目内唯一）。"""
    await (
        TestEnvironment.filter(project_id=_as_uuid(project_id, field="project_id"))
        .exclude(id=exclude_id)
        .update(is_default=False)
    )


async def delete_environment(env_id: Any) -> int:
    """删除环境（级联删变量；若作父级被引用则拒绝，需先改父）。"""
    env = await get_environment(env_id)
    children = await TestEnvironment.filter(parent_id=env.id).count()
    if children:
        raise AssetValidationError(
            f"环境 {env.code!r} 仍是 {children} 个子环境的父级（分层继承），"
            "请先迁移子环境的 parent_id 再删除"
        )
    return await TestEnvironment.filter(id=env.id).delete()


# ── 环境变量（design §15.4 / 05 §2.2）───────────────────────────
ENV_VARIABLE_EDITABLE_FIELDS: tuple[str, ...] = ("key", "value", "type", "is_secret", "purpose")


def _coerce_env_value(value: Any, var_type: str) -> Any:
    """按类型声明校验/归一化变量值（05 §4「保持原生类型」）。

    ``value`` 是 JSONB 原生值：int 就是 int、bool 就是 bool，绝不当字符串。
    字符串的 JSONB 编码/解码由 :class:`~testing_platform.assets.models.JSONValueField`
    负责（Tortoise 内置 JSONField 不支持顶层字符串）。
    """
    if value is None:
        return None
    if var_type == "string":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise AssetValidationError(f"string 类型变量值不合法：{value!r}")
        return str(value)
    if var_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssetValidationError(f"int 类型变量值必须是整数：{value!r}")
        return int(value)
    if var_type == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AssetValidationError(f"float 类型变量值必须是数字：{value!r}")
        return float(value)
    if var_type == "boolean":
        if not isinstance(value, bool):
            raise AssetValidationError(f"boolean 类型变量值必须是布尔：{value!r}")
        return bool(value)
    if var_type == "list":
        if not isinstance(value, (list, tuple)):
            raise AssetValidationError(f"list 类型变量值必须是列表：{value!r}")
        return list(value)
    if var_type in ("json", "dict"):  # dict 为历史存量别名，已并入 json（见 ENV_VARIABLE_TYPES）
        if not isinstance(value, Mapping):
            raise AssetValidationError(f"{var_type} 类型变量值必须是对象：{value!r}")
        return _jsonify(dict(value))
    raise AssetValidationError(f"未知变量类型：{var_type!r}")


async def create_env_variable(
    *,
    env_id: Any,
    key: str,
    value: Any = None,
    type: str = "string",
    purpose: Optional[str] = None,
    is_secret: bool = False,
) -> TestEnvVariable:
    """给环境建变量（05 §2.2）。``type`` 七类型之一；``value`` 保持原生类型。"""
    env = await get_environment(env_id)
    if not key:
        raise AssetValidationError("环境变量 key 必填")
    var_type = _validate_enum(type, ENV_VARIABLE_TYPES, field="type", required=True)
    return await TestEnvVariable.create(
        env=env,
        key=str(key),
        value=_coerce_env_value(value, var_type),
        type=var_type,
        purpose=purpose,
        is_secret=bool(is_secret),
    )


async def list_env_variables(env_id: Any, *, mask_secrets: bool = True) -> list[TestEnvVariable]:
    """列某环境的变量。``mask_secrets=True``（默认）时密钥值脱敏（05 §5）。"""
    env = await get_environment(env_id)
    rows = await TestEnvVariable.filter(env_id=env.id).order_by("key")
    if not mask_secrets:
        return rows
    for row in rows:
        if row.is_secret:
            row.value = mask_secret_value(row.value)
    return rows


async def update_env_variable(
    var_id: Any,
    *,
    change_reason: Optional[str] = None,
    **values: Any,
) -> TestEnvVariable:
    """改环境变量（05 §6 编辑变量）。``change_reason`` 可选（不强制变更门）。

    变量层变更只递增 ``version``（design §15.12 的变更门只约束环境本体；
    变量是叶子数据，按 §6「编辑变量」操作，不另写 change_record）。
    """
    try:
        var = await TestEnvVariable.get(id=_as_uuid(var_id, field="var_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"环境变量不存在：{var_id!r}") from None

    allowed_set = set(ENV_VARIABLE_EDITABLE_FIELDS)
    unknown = [key for key in values if key not in allowed_set]
    if unknown:
        raise AssetValidationError(
            f"环境变量不允许修改字段：{unknown}（可改：{sorted(allowed_set)}）"
        )
    if not values:
        raise AssetValidationError("环境变量 update 未提供任何字段")

    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key == "type":
            value = _validate_enum(value, ENV_VARIABLE_TYPES, field=key, required=True)
        elif key == "value":
            var_type = values.get("type") or var.type
            value = _coerce_env_value(value, var_type)
        clean[key] = value

    for key, new_value in clean.items():
        setattr(var, key, new_value)
    var.version = int(var.version or 1) + 1
    await var.save()
    return var


async def delete_env_variable(var_id: Any) -> int:
    """删除环境变量，返回删除行数（0 = 不存在）。"""
    try:
        var = await TestEnvVariable.get(id=_as_uuid(var_id, field="var_id"))
    except DoesNotExist:
        return 0
    return await TestEnvVariable.filter(id=var.id).delete()


async def _environment_chain(env_id: Any) -> list[TestEnvironment]:
    """环境继承链（根 → 叶，含自身），沿 ``parent_id`` 向上回溯（05 §3）。

    顺带做**防循环**校验（数据损坏兜底）：链上同一 id 出现两次即成环。
    """
    chain: list[TestEnvironment] = []
    seen: set[str] = set()
    cur: Optional[TestEnvironment] = await get_environment(env_id)
    while cur is not None:
        cur_id = str(cur.id)
        if cur_id in seen:
            raise EnvironmentCycleError(
                f"环境继承链成环：{' -> '.join(list(seen) + [cur_id])}（05 §3 防循环）"
            )
        seen.add(cur_id)
        chain.append(cur)
        if cur.parent_id is None:
            break
        try:
            cur = await TestEnvironment.get(id=cur.parent_id)
        except DoesNotExist:
            raise EnvironmentNotFoundError(
                f"环境 {cur_id!r} 的父环境不存在：{cur.parent_id!r}"
            ) from None
    chain.reverse()  # 根在前、叶在后（父先子后）
    return chain


async def resolve_environment_vars(
    env_id: Any,
    *,
    mask_secrets: bool = True,
) -> dict[str, Any]:
    """**递归继承合并**（05 §3 / 06 §2.3）：父先子后，子覆盖父。

    返回 ``{变量key: 值}`` —— 值保持**原生类型**（``{{env.port}}`` 是 int 就
    是 int）。读取密钥变量时默认脱敏（``mask_secrets=True``，05 §5）。
    """
    chain = await _environment_chain(env_id)
    merged: dict[str, Any] = {}
    for env in chain:  # 父 → 子
        for var in await TestEnvVariable.filter(env_id=env.id).order_by("key"):
            value = var.value
            if var.is_secret and mask_secrets:
                value = mask_secret_value(value)
            merged[var.key] = value
    return merged


async def get_env_chain_documents(env_id: Any) -> list[dict[str, Any]]:
    """环境继承链 → 引擎 ``envs`` 参数形态（根 → 叶，06 §2.3）。"""
    chain = await _environment_chain(env_id)
    return [
        {
            "id": str(env.id),
            "parent_id": str(env.parent_id) if env.parent_id else None,
            "vars": {
                v.key: (mask_secret_value(v.value) if v.is_secret else v.value)
                for v in await TestEnvVariable.filter(env_id=env.id)
            },
        }
        for env in chain
    ]


# ── 自定义工具（design §15.5：只拥有签名 + 指向）────────────────
CUSTOM_TOOL_EDITABLE_FIELDS: tuple[str, ...] = (
    "code",
    "scope",
    "purpose",
    "signature",
    "impl_ref",
    "status",
)


def _validate_custom_tool_impl_ref(impl_ref: Any) -> dict[str, Any]:
    """impl_ref 归一化：只能指向 mc_test_scenario 或 mc_test_method.full_ref。

    design §15.5「只拥有签名 + 指向，不拥有实现」——杜绝两处实现。
    形态：``{"kind": "scenario", "id": "..."}`` 或
    ``{"kind": "method", "full_ref": "base.crypto.sign"}``。
    """
    if not impl_ref:
        raise AssetValidationError("custom_tool impl_ref 必填（指向 scenario 或 method.full_ref）")
    ref = dict(impl_ref)
    kind = ref.get("kind")
    if kind not in ("scenario", "method"):
        raise AssetValidationError(
            f"impl_ref.kind 必须是 scenario 或 method，实际 {kind!r}（design §15.5）"
        )
    if kind == "scenario":
        if not ref.get("id"):
            raise AssetValidationError("impl_ref=scenario 必须带 id")
    else:
        if not ref.get("full_ref"):
            raise AssetValidationError("impl_ref=method 必须带 full_ref")
    return ref


async def create_custom_tool(
    *,
    project_id: Any,
    code: str,
    purpose: Optional[str] = None,
    signature: Optional[Mapping[str, Any]] = None,
    impl_ref: Optional[Mapping[str, Any]] = None,
    scope: str = "project",
    status: str = "draft",
) -> TestCustomTool:
    """建自定义工具（design §15.5）。``impl_ref`` 指向场景或方法（二选一）。"""
    if not code:
        raise AssetValidationError("自定义工具 code 必填")
    return await TestCustomTool.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=str(code),
        scope=_validate_enum(scope, CUSTOM_TOOL_SCOPES, field="scope", required=True),
        purpose=purpose,
        signature=_jsonify(dict(signature or {})),
        impl_ref=_jsonify(_validate_custom_tool_impl_ref(impl_ref)),
        status=_validate_enum(status, CUSTOM_TOOL_STATUSES, field="status", required=False) or "draft",
    )


async def get_custom_tool(tool_id: Any) -> TestCustomTool:
    """按 UUID 主键或 ``code`` 取自定义工具。"""
    lookup: dict[str, Any]
    try:
        lookup = {"id": _as_uuid(tool_id, field="tool_id")}
    except AssetValidationError:
        lookup = {"code": str(tool_id)}
    try:
        return await TestCustomTool.get(**lookup)
    except DoesNotExist:
        raise AssetNotFoundError(f"自定义工具不存在：{tool_id!r}") from None


async def list_custom_tools(
    project_id: Any,
    *,
    scope: Optional[str] = None,
    status: Optional[str] = None,
) -> list[TestCustomTool]:
    """按项目列自定义工具（可按 scope/status 收窄），code 升序。"""
    pid = _as_uuid(project_id, field="project_id")
    qs = TestCustomTool.filter(project_id=pid)
    if scope:
        qs = qs.filter(scope=scope)
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("code")


async def update_custom_tool(
    tool_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestCustomTool:
    """改自定义工具（design §15.5 / §15.12 变更门强制）。

    每次升级落一行 :class:`TestCustomToolVersion` 快照（snapshot = 升级后的完整文档）。
    """
    tool = await get_custom_tool(tool_id)
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "custom_tool.update 必须提供 change_reason（design §15.12 变更门）"
        )
    allowed_set = set(CUSTOM_TOOL_EDITABLE_FIELDS)
    unknown = [key for key in values if key not in allowed_set]
    if unknown:
        raise AssetValidationError(
            f"自定义工具不允许修改字段：{unknown}（可改：{sorted(allowed_set)}）"
        )

    clean: dict[str, Any] = {}
    for key, value in values.items():
        if key == "scope":
            value = _validate_enum(value, CUSTOM_TOOL_SCOPES, field=key, required=True)
        elif key == "status":
            value = _validate_enum(value, CUSTOM_TOOL_STATUSES, field=key, required=True)
        elif key == "impl_ref":
            value = _jsonify(_validate_custom_tool_impl_ref(value))
        elif key in {"signature", "purpose"}:
            value = _jsonify(dict(value)) if key == "signature" else value
        clean[key] = value

    if not clean:
        raise AssetValidationError("custom_tool.update 未提供任何字段")

    updated = await _apply_update(
        tool,
        entity_type="custom_tool",
        allowed=CUSTOM_TOOL_EDITABLE_FIELDS,
        values=clean,
        change_reason=str(change_reason).strip(),
        propagation=propagation,
        rollback_ref=rollback_ref,
    )
    await TestCustomToolVersion.create(
        tool=updated,
        version=updated.version,
        snapshot=_jsonify(_custom_tool_document(updated)),
        change_reason=str(change_reason).strip(),
    )
    return updated


def _custom_tool_document(tool: TestCustomTool) -> dict[str, Any]:
    """自定义工具行 → JSON 文档（供版本快照/协议层返回）。"""
    return {
        "id": str(tool.id),
        "code": tool.code,
        "project_id": str(tool.project_id),
        "scope": tool.scope,
        "purpose": tool.purpose,
        "signature": dict(tool.signature or {}),
        "impl_ref": dict(tool.impl_ref or {}),
        "status": tool.status,
        "version": tool.version,
    }


async def list_custom_tool_versions(tool_id: Any) -> list[TestCustomToolVersion]:
    """某工具的全部版本快照（旧 → 新）。"""
    tool = await get_custom_tool(tool_id)
    return await TestCustomToolVersion.filter(tool_id=tool.id).order_by("version")


# ── 接口导入任务（mc_test_api_import_job：可配置的定时导入）────────
# 一条任务 = 文档来源（spec_url/spec_text）+ 导入方式（source_format，含 apifox）
# + 节奏（schedule）。执行（抓 URL + 导入）在 import_jobs 执行层；此处只管 CRUD
# 与「下次运行时间」的推导。

_IMPORT_JOB_EDITABLE_FIELDS = frozenset({"name", "source_format", "spec_url", "spec_text", "schedule", "enabled"})


def _normalize_schedule(raw: Any) -> dict:
    """``schedule`` 归一：``kind`` 枚举校验 + 复用任务的 ``next_fire`` 纯解析校验。

    形态（与 :func:`testing_platform.tasks.scheduler.next_fire` 对齐）::

        {"kind": "manual"}                      # 仅手动（定时任务用这个之外的）
        {"kind": "cron", "expr": "0 2 * * *"}    # 标准 5 字段 cron
        {"kind": "interval", "minutes": 30}      # 每 30 分钟
        {"kind": "once", "at": "2026-10-01T09:00"}

    ``manual``/空 → 不定时。其余 ``next_fire`` 能解析即合法（解析失败时把异常
    信息带进 ``AssetValidationError``）。
    """
    if not isinstance(raw, Mapping):
        raise AssetValidationError("schedule 应为对象（{kind, expr/minutes/at}）")
    kind = str(raw.get("kind") or "manual")
    if kind not in API_IMPORT_SCHEDULE_KINDS:
        raise AssetValidationError(
            f"schedule.kind 取值不合法：{kind!r}（应选 {list(API_IMPORT_SCHEDULE_KINDS)}）"
        )
    out = dict(raw)
    out["kind"] = kind
    if kind != "manual":
        from ..tasks.scheduler import next_fire  # 延迟引入避免顶层循环

        try:
            # next_fire 用 naive datetime 比较；传 naive now 保持一致
            next_fire(out, now=_dt.now(_tz.utc).replace(tzinfo=None))
        except ValueError as exc:
            raise AssetValidationError(f"schedule 非法：{exc}") from exc
    return out


def _next_run_at(schedule: Mapping[str, Any], *, from_time: Optional[_dt] = None) -> Optional[_dt]:
    """按 schedule 算下次运行时间（复用 ``next_fire``；manual → None）。

    ``next_fire`` 返回 naive datetime，这里补 UTC tzinfo 与扫描口径一致。
    """
    if not schedule:
        return None
    kind = str(schedule.get("kind") or "manual")
    if kind == "manual":
        return None
    from ..tasks.scheduler import next_fire  # 延迟引入避免顶层循环

    base = from_time or _dt.now(_tz.utc)
    # next_fire 用 naive；base 去 tzinfo 比较，结果补回 UTC
    naive_base = base.replace(tzinfo=None)
    nxt = next_fire(schedule, now=naive_base)
    if nxt is None:
        return None
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=_tz.utc)
    return nxt


async def create_import_job(
    *,
    project_id: Any,
    name: str,
    source_format: str = "auto",
    spec_url: Optional[str] = None,
    spec_text: Optional[str] = None,
    schedule: Optional[Mapping[str, Any]] = None,
    enabled: bool = True,
    created_by: Optional[Any] = None,
) -> TestApiImportJob:
    """建导入任务。``schedule.kind != manual`` 时必须给 ``spec_url``（定时只能抓 URL）。"""
    clean_name = str(name or "").strip()
    if not clean_name:
        raise AssetValidationError("导入任务 name 不能为空")
    fmt = _validate_enum(source_format or "auto", API_SOURCE_FORMATS, field="source_format", required=False) or "auto"
    norm_schedule = _normalize_schedule(schedule or {"kind": "manual"})
    clean_url = str(spec_url).strip() if spec_url else None
    clean_text = str(spec_text).strip() if spec_text else None
    if norm_schedule["kind"] != "manual" and not clean_url:
        raise AssetValidationError("定时导入只能走 spec_url（文档 URL），spec_text 是手动导入专用")
    if norm_schedule["kind"] == "manual" and not clean_url and not clean_text:
        raise AssetValidationError("手动导入任务必须给 spec（spec_url 或 spec_text）")
    nxt = _next_run_at(norm_schedule) if enabled else None
    return await TestApiImportJob.create(
        project_id=_as_uuid(project_id, field="project_id"),
        name=clean_name,
        source_format=fmt,
        spec_url=clean_url,
        spec_text=clean_text,
        schedule=_jsonify(norm_schedule),
        enabled=enabled,
        next_run_at=nxt,
        created_by=_maybe_uuid(created_by, field="created_by"),
    )


async def get_import_job(job_id: Any) -> TestApiImportJob:
    """按 id 取导入任务。"""
    try:
        return await TestApiImportJob.get(id=_as_uuid(job_id, field="job_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"导入任务不存在：{job_id!r}") from None


async def list_import_jobs(project_id: Any) -> list[TestApiImportJob]:
    """按项目列导入任务（created_at 旧 → 新）。"""
    return await TestApiImportJob.filter(
        project_id=_as_uuid(project_id, field="project_id")
    ).order_by("created_at")


async def update_import_job(job_id: Any, values: Mapping[str, Any]) -> TestApiImportJob:
    """改导入任务（名字/方式/来源/节奏/启停）。schedule 变化后重算 next_run_at。"""
    job = await get_import_job(job_id)
    patch: dict[str, Any] = {}
    for key in ("name", "spec_url", "spec_text"):
        if key in values:
            value = values[key]
            if key == "name":
                clean = str(value or "").strip()
                if not clean:
                    raise AssetValidationError("导入任务 name 不能为空")
                patch["name"] = clean
            else:
                patch[key] = str(value).strip() if value else None
    if "source_format" in values:
        patch["source_format"] = (
            _validate_enum(
                values["source_format"] or "auto",
                API_SOURCE_FORMATS,
                field="source_format",
                required=False,
            )
            or "auto"
        )
    if "schedule" in values:
        patch["schedule"] = _jsonify(_normalize_schedule(values["schedule"] or {"kind": "manual"}))
    if "enabled" in values:
        patch["enabled"] = bool(values["enabled"])
    if not patch:
        return job
    for key, value in patch.items():
        setattr(job, key, value)
    # 定时任务必须有 spec_url（改完可能没有）
    schedule_map = job.schedule if isinstance(job.schedule, Mapping) else {}
    if str(schedule_map.get("kind") or "manual") != "manual" and not job.spec_url:
        raise AssetValidationError("定时导入只能走 spec_url（文档 URL），spec_text 是手动导入专用")
    job.next_run_at = _next_run_at(schedule_map) if job.enabled else None
    await job.save()
    return job


async def delete_import_job(job_id: Any) -> int:
    """删导入任务（物理删除；导入出的接口不受影响）。"""
    job = await get_import_job(job_id)
    return await TestApiImportJob.filter(id=job.id).delete()


async def mark_import_job_run(
    job_id: Any,
    *,
    status: str,
    message: Optional[str] = None,
    result: Optional[Mapping[str, Any]] = None,
) -> TestApiImportJob:
    """记录一次运行结果（执行层调）：last_run_at=now、last_status/message/result、重算 next_run_at。"""
    job = await get_import_job(job_id)
    job.last_run_at = _dt.now(_tz.utc)
    job.last_status = status if status in ("ok", "error", "running") else "error"
    job.last_message = (str(message)[:2000] if message else None)
    job.last_result = _jsonify(dict(result or {}))
    schedule_map = job.schedule if isinstance(job.schedule, Mapping) else {}
    job.next_run_at = _next_run_at(schedule_map) if job.enabled else None
    await job.save()
    return job


async def due_import_jobs(*, limit: int = 20) -> list[TestApiImportJob]:
    """到期的定时任务（enabled 且 next_run_at <= now），调度器扫描用。"""
    now = _dt.now(_tz.utc)
    return await TestApiImportJob.filter(enabled=True, next_run_at__lte=now).order_by("next_run_at").limit(limit)


# ── 接口来源（mc_test_api_source：一个接口可来自多个导入方式）────────
# 每次导入命中一条接口 → 登记/刷新一条来源（api_id × job_id 幂等）。
# 详情页据此展示「来源」列表 + 提供「同步」按钮（重抓该来源文档刷回来源态）。


def _api_source_document(row: TestApiSource) -> dict[str, Any]:
    """来源行 → JSON 文档（surface/前端用）。"""
    return {
        "id": str(row.id),
        "api_id": str(row.api_id),
        "project_id": str(row.project_id),
        "job_id": str(row.job_id) if row.job_id else None,
        "source_format": row.source_format,
        "spec_url": row.spec_url,
        "method": row.method,
        "path": row.path,
        "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
        "last_status": row.last_status,
        "last_message": row.last_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def record_api_sources(
    *,
    project_id: Any,
    job_id: Optional[Any],
    source_format: str,
    spec_url: Optional[str],
    endpoints: list[dict[str, Any]],
    synced: bool = True,
    message: Optional[str] = None,
    status: str = "ok",
) -> list[TestApiSource]:
    """把一次导入命中的接口登记为来源（幂等：同 api_id + job_id 刷新）。

    ``endpoints`` 形如 ``[{"method": ..., "path": ...}, ...]``（导入结果里的
    method+path 清单）。``synced=True`` 记 ``last_synced_at=now``；``status``
    非 ok 时不更新时间（同步失败）。
    """
    if not endpoints:
        return []
    pid = _as_uuid(project_id, field="project_id")
    jid = _maybe_uuid(job_id, field="job_id")
    fmt = source_format if source_format in API_SOURCE_FORMATS else "openapi3"
    now = _dt.now(_tz.utc)
    out: list[TestApiSource] = []
    for ep in endpoints:
        api = await TestApi.get_or_none(
            project_id=pid, method=str(ep.get("method") or "").upper(), path=str(ep.get("path") or "")
        )
        if api is None:
            continue  # 导入失败/被跳过 → 不登记来源
        # 唯一键（api_id, job_id）——job_id 为 null（无任务导入）时退化为每行一条
        existing = (
            await TestApiSource.get_or_none(api_id=api.id, job_id=jid) if jid else None
        )
        if existing is not None:
            existing.source_format = fmt
            existing.spec_url = spec_url
            existing.method = str(ep.get("method") or existing.method).upper()
            existing.path = str(ep.get("path") or existing.path)
            existing.last_status = status
            existing.last_message = message
            if synced:
                existing.last_synced_at = now
            await existing.save()
            out.append(existing)
        else:
            out.append(
                await TestApiSource.create(
                    api_id=api.id,
                    project_id=pid,
                    job_id=jid,
                    source_format=fmt,
                    spec_url=spec_url,
                    method=str(ep.get("method") or "").upper(),
                    path=str(ep.get("path") or ""),
                    last_synced_at=now if synced else None,
                    last_status=status,
                    last_message=message,
                )
            )
    return out


async def list_api_sources(api_id: Any) -> list[TestApiSource]:
    """一个接口的全部来源（按来源格式 + 创建时间排序，前端展示）。"""
    api = await get_api(api_id)
    return await TestApiSource.filter(api_id=api.id).order_by("source_format", "created_at")


async def get_api_source(source_id: Any) -> TestApiSource:
    """按 id 取一条来源。"""
    try:
        return await TestApiSource.get(id=_as_uuid(source_id, field="source_id"))
    except DoesNotExist:
        raise AssetNotFoundError(f"接口来源不存在：{source_id!r}") from None


async def delete_api_source(source_id: Any) -> int:
    """删一条来源（不再跟踪该导入方式对该接口的同步；接口本体不动）。"""
    row = await get_api_source(source_id)
    return await TestApiSource.filter(id=row.id).delete()


async def mark_api_source_synced(
    source_id: Any,
    *,
    status: str,
    message: Optional[str] = None,
) -> TestApiSource:
    """同步结果回写（执行层调）。"""
    row = await get_api_source(source_id)
    row.last_status = status if status in ("ok", "error") else "error"
    row.last_message = str(message)[:2000] if message else None
    if row.last_status == "ok":
        row.last_synced_at = _dt.now(_tz.utc)
    await row.save()
    return row


__all__ = [
    "API_CODE_PREFIX",
    "API_EDITABLE_FIELDS",
    "FOLDER_EDITABLE_FIELDS",
    "_api_source_document",
    "create_import_job",
    "delete_import_job",
    "delete_api_source",
    "due_import_jobs",
    "get_api_source",
    "get_import_job",
    "list_api_sources",
    "mark_api_source_synced",
    "record_api_sources",
    "list_import_jobs",
    "mark_import_job_run",
    "update_import_job",
    "AssetNotFoundError",
    "AssetValidationError",
    "CASE_CODE_PREFIX",
    "CASE_EDITABLE_FIELDS",
    "CUSTOM_TOOL_EDITABLE_FIELDS",
    "CUSTOM_TOOL_SCOPES",
    "CUSTOM_TOOL_STATUSES",
    "ChangeReasonRequiredError",
    "DATASET_EDITABLE_FIELDS",
    "ENV_VARIABLE_EDITABLE_FIELDS",
    "ENV_VARIABLE_TYPES",
    "ENVIRONMENT_EDITABLE_FIELDS",
    "ENVIRONMENT_STATUSES",
    "EnvironmentCycleError",
    "EnvironmentNotFoundError",
    "REQUIREMENT_CODE_PREFIX",
    "REQUIREMENT_EDITABLE_FIELDS",
    "SCENARIO_CODE_PREFIX",
    "SCENARIO_EDITABLE_FIELDS",
    "SECRET_MASK",
    "TRANSPORT_EDITABLE_FIELDS",
    "create_case",
    "create_custom_tool",
    "create_dataset",
    "create_env_variable",
    "create_environment",
    "create_method",
    "create_method_module",
    "delete_method_module",
    "list_method_classes",
    "resolve_method_module",
    "sync_methods_from_source",
    "update_method_module",
    "METHOD_MODULE_EDITABLE_FIELDS",
    "create_requirement",
    "create_scenario",
    "create_steps",
    "create_tag",
    "create_transport",
    "create_api",
    "create_folder",
    "delete_api",
    "delete_folder",
    "delete_env_variable",
    "delete_environment",
    "delete_requirement",
    "delete_transport",
    "get_api",
    "get_case",
    "get_custom_tool",
    "get_dataset",
    "get_env_chain_documents",
    "get_environment",
    "get_folder",
    "get_method_module",
    "get_requirement",
    "get_scenario",
    "get_scenario_full",
    "get_transport",
    "link_case_scenario",
    "link_api_case",
    "link_api_scenario",
    "list_apis",
    "list_api_cases",
    "list_api_scenarios",
    "list_case_apis",
    "list_scenario_apis",
    "sync_scenario_api_links",
    "sync_case_api_links",
    "sync_cases_of_scenario",
    "list_case_scenarios",
    "list_case_scenarios_full",
    "list_case_tags",
    "list_cases",
    "list_changes",
    "list_custom_tool_versions",
    "list_custom_tools",
    "list_datasets",
    "list_env_variables",
    "list_environments",
    "list_folders",
    "list_method_modules",
    "list_methods",
    "list_requirements",
    "list_scenarios",
    "list_steps",
    "list_tags",
    "list_transports",
    "mask_secret_value",
    "move_api",
    "record_change",
    "resolve_environment_vars",
    "update_requirement",
    "tag_case",
    "unlink_case_scenario",
    "unlink_api_case",
    "unlink_api_scenario",
    "untag_case",
    "update_api",
    "update_case",
    "update_custom_tool",
    "update_dataset",
    "update_env_variable",
    "update_environment",
    "update_folder",
    "update_scenario",
    "update_transport",
]
