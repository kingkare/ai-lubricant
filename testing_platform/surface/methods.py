"""能力面 · 工具方法注册表（design §17.1「原子能力」）。

**本模块是测试域对外唯一出口的最底层**：每个方法是一个原子可调用单元，
MCP（:mod:`testing_platform.surface.mcp`）与 HTTP 接口
（:mod:`testing_platform.surface.api`）都只是它的**协议适配层**，不含业务逻辑。

设计约束（design §6.2「Agent 抽象」/ §17.1）：

* **只暴露已实现的模块** —— 每个方法都是对既有确定性模块的一层薄包装：
  ``knowledge.*`` → :mod:`testing_platform.knowledge`；
  ``script.validate`` → :mod:`testing_platform.quality`；
  ``case.*`` / ``scenario.*`` / ``tag.*`` → :mod:`testing_platform.assets`；
  ``task.*`` / ``run.list`` / ``run.get`` / ``run.scenarios`` / ``run.steps``
  → :mod:`testing_platform.tasks`；
  ``run.execute`` → :mod:`testing_platform.engine`（确定性执行）；
  ``report.*`` → :mod:`testing_platform.reports`；
  ``bug.create`` / ``bug.root_cause`` / ``bug.list`` → :mod:`testing_platform.defects`；
  ``strategy.*`` → :mod:`testing_platform.strategy`（策略 CRUD + 勾选 + 分配 + 结果）；
  ``workflow.*`` → :mod:`testing_platform.workflow`（定义 + 运行记录 + 契约门）；
  ``tool.list`` → 引擎工具注册表。
* **未实现的方法不注册**（下方 "待实现" 清单），调用方看到的就是真实能力边界。
* **查询类（list/get）方法** —— 前端页面取数用（``report.list`` /
  ``report.get`` / ``run.list`` / ``run.get`` / ``case.list`` / ``case.get`` /
  ``scenario.list`` / ``task.list`` / ``bug.list`` / ``knowledge.list``）
  均为 ``kind="read"`` 薄包装，按 ``project_id`` 过滤。
* **写类方法** —— 前端表单/Agent 落数据用（``case.create`` / ``case.update`` /
  ``scenario.create`` / ``scenario.update`` / ``scenario.create_steps`` /
  ``case.link_scenario`` / ``case.tag`` / ``tag.create`` / ``task.create`` /
  ``task.update`` / ``task.enable`` / ``task.disable`` / ``task.run`` /
  ``report.generate`` / ``bug.create`` / ``bug.root_cause`` /
  ``workflow.start`` / ``workflow.complete``）均为 ``kind="write"`` 薄包装；
  ``update_*`` 走变更门（``change_reason`` 必填），``run.execute`` 独为
  ``kind="execute"``（真跑引擎）。
* **全异步**：``handler`` 是 ``async def handler(params: Mapping) -> Any``。
* 每个方法声明 ``name / params_schema / returns_schema / kind / handler``；
  ``params_schema`` 是 **JSON Schema 子集**（既是入参校验依据，也直接充当
  MCP ``inputSchema``），``kind`` ∈ ``read | write | execute``。

调用约定::

    from testing_platform.surface import methods
    result = await methods.call_method("tool.list", {})     # 直接调用，异常抛穿
    spec   = methods.get_method("script.validate")

---- 待实现（design §17.1 已列，对应模块未建/未接线，故**不注册**）----

=================================================================  ==========
方法                                                                 等什么
=================================================================  ==========
``knowledge.profile``                                               测试画像模块（mc_test_profile 读写）
``case.confirm``                                                     用例确认门（用户确认流）
``scenario.generate``                                                场景生成工作流（LLM 角色，非确定性）
``tool.register``                                                    自定义工具评审流（进阶，custom_tool.* 已落 CRUD）
``run.status`` / ``run.result``                                      执行调度（mc_test_run 生命周期）
``bug.triage``                                                       失败分流接线（纯函数已在 defects.triage）
=================================================================  ==========
"""
from __future__ import annotations

import inspect
import json
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from loguru import logger

from ..assets import (
    AssetNotFoundError,
    AssetValidationError,
    API_BODY_TYPES,
    API_EDITABLE_FIELDS,
    API_METHODS,
    API_PROTOCOLS,
    API_SOURCES,
    API_SOURCE_FORMATS,
    API_STATUSES,
    CASE_EDITABLE_FIELDS,
    CASE_STATUSES,
    CUSTOM_TOOL_SCOPES,
    CUSTOM_TOOL_STATUSES,
    ChangeReasonRequiredError,
    DATASET_EDITABLE_FIELDS,
    ENV_VARIABLE_TYPES,
    ENVIRONMENT_STATUSES,
    REQUIREMENT_EDITABLE_FIELDS,
    SCENARIO_EDITABLE_FIELDS,
    SCENARIO_STATUSES,
    TAG_CATEGORIES,
    TRANSPORT_EDITABLE_FIELDS,
    TRANSPORT_KINDS,
    TRANSPORT_STATUSES,
    create_api,
    create_folder,
    create_import_job,
    delete_api,
    delete_folder,
    delete_import_job,
    get_api,
    get_folder,
    get_import_job,
    link_api_case,
    link_api_scenario,
    list_apis,
    list_api_cases,
    list_api_scenarios,
    list_case_apis,
    list_scenario_apis,
    list_folders,
    list_import_jobs,
    move_api,
    unlink_api_case,
    unlink_api_scenario,
    update_api,
    update_folder,
    update_import_job,
    # 统一目录（mc_test_catalog）
    CATALOG_TYPES,
    create_catalog,
    delete_catalog,
    get_catalog,
    list_catalogs,
    update_catalog,
    create_case,
    create_custom_tool,
    create_dataset,
    delete_dataset,
    create_env_variable,
    create_environment,
    create_method,
    create_method_module,
    create_requirement,
    add_requirement_attachment,
    create_scenario,
    create_steps,
    create_tag,
    create_transport,
    delete_env_variable,
    delete_environment,
    delete_method_module,
    delete_requirement,
    delete_requirement_attachment,
    delete_transport,
    get_case,
    get_env_chain_documents,
    get_environment,
    get_method_module,
    get_requirement,
    get_scenario_full,
    get_transport,
    link_case_scenario,
    list_case_scenarios_full,
    list_case_tags,
    list_cases,
    list_changes,
    list_class_methods,
    list_custom_tools,
    list_datasets,
    list_env_variables,
    list_environments,
    list_method_classes,
    list_method_modules,
    list_methods,
    list_requirement_attachments,
    list_requirements,
    list_scenarios,
    list_tags,
    list_transports,
    resolve_environment_vars,
    resolve_method_module,
    sync_methods_from_source,
    tag_case,
    unlink_case_scenario,
    update_case,
    update_custom_tool,
    update_dataset,
    update_env_variable,
    update_environment,
    update_method_module,
    update_requirement,
    update_scenario,
    update_transport,
)
from ..defects import (
    BUG_ACTIONS,
    BUG_BROWSER_LIST,
    BUG_FOUND_INS,
    BUG_OS_LIST,
    BUG_PRIORITIES,
    BUG_RESOLUTIONS,
    BUG_SEVERITIES,
    BUG_STATUSES,
    BUG_STATUS_LABELS,
    BUG_TYPES,
    FAILURE_KINDS,
    BugFieldRequiredError,
    BugTransitionNotAllowedError,
    DefectValidationError,
    RootCauseEvidenceRequiredError,
    apply_bug_action,
    attach_root_cause,
    create_bug_from_failure,
    delete_bug,
    get_bug,
    list_bug_log,
    list_bugs,
    update_bug,
)
from ..engine import (
    ExecutionContext,
    EngineError,
    ScenarioExecutor,
    default_assertion_registry,
    default_tool_registry,
)
from ..knowledge import (
    BUILTIN_ADAPTERS,
    CaseScanError,
    GraphifyAdapter,
    GraphifyNotInstalled,
    KnowledgePutError,
    SourceFetchError,
    fetch_file as knowledge_fetch_file,
    ingest_raw as knowledge_ingest_raw,
    list_items as knowledge_list_items,
    put_items as knowledge_put_items,
    scan_cases as knowledge_scan_cases,
)
from ..projects import (
    GIT_REF_TYPES,
    GIT_STATUSES,
    PROJECT_STATUSES,
    SOURCE_ADAPTERS,
    SOURCE_KINDS,
    SOURCE_STATUSES,
    SOURCE_SYNC_POLICIES,
    ProjectNotFoundError,
    ProjectValidationError,
    archive_project,
    create_git,
    create_project,
    create_release,
    create_source,
    create_version,
    delete_git,
    delete_source,
    delete_version,
    diff_releases,
    get_git,
    get_project,
    get_release,
    get_source,
    get_version,
    init_snapshot,
    list_gits,
    list_projects,
    list_releases,
    list_sources,
    list_versions,
    mark_source_synced,
    sync_all,
    update_git,
    update_project,
    update_source,
    update_version,
)
from ..quality import (
    CRAFT_SCOPES,
    CRAFT_TARGETS,
    CRAFT_TYPES,
    QUESTION_ORIGINS,
    QUESTION_RESOLUTIONS,
    QUESTION_STATUSES,
    QuestionBlockingError,
    ValidationResult,
    create_craft,
    list_craft,
    list_questions,
    load_craft,
    raise_question as question_raise_service,
    resolve_question as question_resolve_service,
    validate as validate_scenario,
)
from ..reports import (
    REPORT_GENERATORS,
    REPORT_STATUSES,
    ReportNotFoundError,
    generate_report,
    get_report,
    list_reports,
)
from ..strategy import (
    STRATEGY_CASE_RESULTS,
    STRATEGY_EXEC_MODES,
    STRATEGY_STATUSES,
    STRATEGY_TYPES,
    StrategyNotFoundError,
)
from ..tasks import (
    RUN_STATUSES,
    RUN_TRIGGERS,
    TASK_EDITABLE_FIELDS,
    TASK_EXEC_MODES,
    TASK_EXEC_TYPES,
    TASK_STATUSES,
    create_task,
    disable_task,
    enable_task,
    get_run,
    list_runs,
    list_tasks,
    run_task,
    update_task,
)
from ..tasks.service import list_run_scenarios, list_step_results  # 执行层级查询

# NOTE: ``..workflow`` 不在此处顶层导入 —— workflow.contract 反向依赖本模块的
# ``_type_ok``（契约校验复用 JSON Schema 类型谓词），顶层导入会成环；故在
# ``workflow.start`` / ``workflow.complete`` 的 handler 里懒加载。

# ── 方法性质（design §17.1 的「性质」列）────────────────────────
METHOD_KINDS: tuple[str, ...] = ("read", "write", "execute")


# ── 错误模型（协议层只认这三种，各域异常在 invoke 里归一）────────
class MethodError(Exception):
    """能力面错误基类：携带稳定的 ``code`` 供 MCP/HTTP 判定。"""

    code = "method_error"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "details": self.details}


class MethodNotFoundError(MethodError):
    """方法未注册（也不代表"以后会有"——未实现的方法本就不注册）。"""

    code = "method_not_found"


class MethodParamError(MethodError):
    """入参未过 ``params_schema``（必填缺失 / 类型不符 / 未知字段）。"""

    code = "invalid_params"


# ── 入参校验（JSON Schema 子集，无三方依赖）──────────────────────
_TYPE_PREDICATES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, (list, tuple)),
    "null": lambda v: v is None,
}


def _type_ok(value: Any, declared: Any) -> bool:
    """``declared`` 可是字符串或字符串列表（如 ``["string","null"]``）。"""
    names = declared if isinstance(declared, (list, tuple)) else [declared]
    return any(_TYPE_PREDICATES.get(str(n), lambda _v: True)(value) for n in names)


def validate_params(schema: Optional[Mapping[str, Any]], params: Mapping[str, Any]) -> None:
    """按简版 schema 校验入参；不过即抛 :class:`MethodParamError`。

    规则（够用即止）：``required`` 必填、``properties`` 类型/``enum`` 校验、
    未在 ``properties`` 声明的字段一律拒绝（``additionalProperties: true``
    可放行）——多余字段多是拼错，早报早好。
    """
    schema = schema or {}
    params = dict(params or {})

    missing = [key for key in schema.get("required") or () if key not in params]
    if missing:
        raise MethodParamError(
            f"缺少必填参数：{missing}", details={"missing": sorted(missing)}
        )

    props: Mapping[str, Any] = schema.get("properties") or {}
    allow_extra = bool(schema.get("additionalProperties", False))
    unknown = [key for key in params if key not in props]
    if unknown and not allow_extra:
        raise MethodParamError(
            f"未知参数：{sorted(unknown)}（可用：{sorted(props)}）",
            details={"unknown": sorted(unknown), "allowed": sorted(props)},
        )

    for key, value in params.items():
        spec = props.get(key)
        if not isinstance(spec, Mapping) or value is None:
            continue
        declared = spec.get("type")
        if declared is not None and not _type_ok(value, declared):
            raise MethodParamError(
                f"参数 {key!r} 类型不符：期望 {declared}，实际 {type(value).__name__}",
                details={"field": key, "expected": declared, "actual": type(value).__name__},
            )
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            raise MethodParamError(
                f"参数 {key!r} 取值非法：{value!r}（可选：{list(allowed)}）",
                details={"field": key, "enum": list(allowed)},
            )


# ── 方法描述 ────────────────────────────────────────────────────
@dataclass
class Method:
    """一个工具方法的声明 + 实现。

    :param name: 稳定名（``domain.verb``，MCP 工具名 / HTTP 路径段）
    :param kind: ``read | write | execute``（design §17.1「性质」列）
    :param params_schema: JSON Schema 子集（校验 + MCP ``inputSchema``）
    :param returns_schema: 返回结构简版说明（描述性，不做校验）
    :param handler: ``async def handler(params) -> Any``
    """

    name: str
    kind: str
    handler: Callable[[Mapping[str, Any]], Any]
    params_schema: dict = field(default_factory=dict)
    returns_schema: dict = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in METHOD_KINDS:
            raise ValueError(f"method {self.name!r} 的 kind 非法：{self.kind!r}（{METHOD_KINDS}）")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "params_schema": self.params_schema,
            "returns_schema": self.returns_schema,
        }


# ── 各域异常 → 能力面错误码（协议层免于认识每个域）───────────────
def _translate_error(exc: BaseException) -> MethodError:
    """把各域异常归一成 :class:`MethodError`（保留信息，不吞）。"""
    if isinstance(exc, MethodError):
        return exc
    if isinstance(exc, ChangeReasonRequiredError):
        return MethodError(str(exc), code="change_reason_required")
    if isinstance(exc, RootCauseEvidenceRequiredError):
        # 根因门：evidence_chain 空（自带 code=root_cause_evidence_required + details）
        return MethodError(str(exc), code=exc.code, details=exc.details)
    if isinstance(exc, QuestionBlockingError):
        # 阻塞门：实体被 open 疑问阻塞（自带 code=question_blocking + details）
        return MethodError(str(exc), code=exc.code, details=exc.details)
    if isinstance(exc, BugTransitionNotAllowedError):
        # 动作守卫未过（自带 code + allowed_actions）——**须在通用 DefectValidationError
        # 分支之前认领**，否则被塌缩成 invalid_request，前端拿不到可执行动作
        return MethodError(str(exc), code=exc.code, details=exc.details)
    if isinstance(exc, BugFieldRequiredError):
        # 条件必填未满足（resolve 必填 resolution；fixed→resolved_build；
        # duplicate→duplicate_bug_id）——同样须抢在通用分支之前
        return MethodError(str(exc), code=exc.code, details=exc.details)
    if isinstance(exc, DefectValidationError):  # ValueError 子类，缺陷入参不合法
        return MethodError(str(exc), code="invalid_request")
    if isinstance(exc, AssetNotFoundError):
        return MethodError(str(exc), code="not_found")
    if isinstance(exc, ReportNotFoundError):
        return MethodError(str(exc), code="not_found")
    if isinstance(exc, AssetValidationError):  # ValueError 子类，含枚举越界/缺必填
        return MethodError(str(exc), code="invalid_request")
    if isinstance(exc, (SourceFetchError, CaseScanError, KnowledgePutError)):
        return MethodError(str(exc), code="invalid_request")
    if isinstance(exc, GraphifyNotInstalled):
        return MethodError(str(exc), code="extractor_unavailable")
    if isinstance(exc, EngineError):
        return MethodError(str(exc), code="engine_error", details=exc.to_dict())
    if isinstance(exc, (ValueError, LookupError)):
        # LookupError 子类（WorkflowDefNotFoundError / WorkflowRunNotFoundError /
        # DefectNotFoundError 等）归 not_found；AssetNotFoundError/ReportNotFoundError
        # 已在上面单独认领。其余 ValueError 归 invalid_request（上游 create_issue 抛
        # ValueError 也走这里）。
        if isinstance(exc, LookupError) and not isinstance(exc, ValueError):
            return MethodError(str(exc), code="not_found")
        return MethodError(str(exc), code="invalid_request")
    return MethodError(f"{type(exc).__name__}: {exc}", code="internal_error")


# ── 注册表 ──────────────────────────────────────────────────────
class MethodRegistry:
    """工具方法注册表：``register`` 注册，``get`` 取，``invoke`` 执行。"""

    def __init__(self) -> None:
        self._methods: dict[str, Method] = {}

    def register(
        self,
        name: str,
        *,
        kind: str,
        params_schema: Optional[Mapping[str, Any]] = None,
        returns_schema: Optional[Mapping[str, Any]] = None,
        description: str = "",
    ):
        """``@register("knowledge.query", kind="read")`` 装饰器。"""

        def deco(func: Callable[[Mapping[str, Any]], Any]) -> Callable[[Mapping[str, Any]], Any]:
            doc = (func.__doc__ or "").strip()
            brief = doc.splitlines()[0] if doc else ""
            self.add(
                Method(
                    name=name,
                    kind=kind,
                    handler=func,
                    params_schema=dict(params_schema or {}),
                    returns_schema=dict(returns_schema or {}),
                    description=description or brief,
                )
            )
            return func

        return deco

    def add(self, method: Method) -> None:
        if method.name in self._methods:
            raise ValueError(f"方法重复注册：{method.name!r}")
        self._methods[method.name] = method

    def get(self, name: str) -> Method:
        try:
            return self._methods[name]
        except KeyError:
            raise MethodNotFoundError(
                f"方法未注册：{name!r}（已注册：{self.names() or ['无']}）"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._methods

    def names(self) -> list[str]:
        return sorted(self._methods)

    def all(self) -> list[Method]:
        return [self._methods[name] for name in self.names()]

    async def invoke(self, name: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """校验入参 → 执行 handler（同步/异步统一适配）→ 归一域异常。"""
        method = self.get(name)
        arguments = dict(params or {})
        validate_params(method.params_schema, arguments)
        try:
            outcome = method.handler(arguments)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome
        except MethodError:
            raise
        except BaseException as exc:  # noqa: BLE001 —— 归一成能力面错误（不吞信息）
            raise _translate_error(exc) from exc


# ── 进程默认注册表 ──────────────────────────────────────────────
_DEFAULT_REGISTRY: Optional[MethodRegistry] = None


def default_method_registry() -> MethodRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = MethodRegistry()
    return _DEFAULT_REGISTRY


def register_method(
    name: str,
    *,
    kind: str,
    params_schema: Optional[Mapping[str, Any]] = None,
    returns_schema: Optional[Mapping[str, Any]] = None,
    description: str = "",
):
    """模块级装饰器：注册方法到进程默认注册表（同 engine ``register_tool``）。"""
    return default_method_registry().register(
        name,
        kind=kind,
        params_schema=params_schema,
        returns_schema=returns_schema,
        description=description,
    )


def get_method(name: str) -> Method:
    return default_method_registry().get(name)


def has_method(name: str) -> bool:
    return default_method_registry().has(name)


def list_methods() -> list[Method]:
    return default_method_registry().all()


async def call_method(name: str, params: Optional[Mapping[str, Any]] = None) -> Any:
    """调用默认注册表里的方法（异常抛穿；协议层用 mcp/api 的封装）。"""
    return await default_method_registry().invoke(name, params)


# ── 序列化（协议层用；方法本身可返回富对象）──────────────────────
def to_jsonable(value: Any) -> Any:
    """把方法返回值转成 JSON 可写形态（UUID/datetime → str，``to_dict`` 对象展平）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (_uuid.UUID,)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_jsonable(to_dict())
    return str(value)


def _uuid_of(value: Any, *, field_name: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MethodParamError(
            f"{field_name} 不是合法 UUID：{value!r}", details={"field": field_name}
        ) from exc


def _normalize_bug_status(value: Any) -> str:
    """缺陷状态入参归一（``fixed`` 别名 → ``resolved``）；非法值抛 MethodParamError。"""
    from ..defects.service import _normalize_status

    try:
        return _normalize_status(value) or "open"
    except DefectValidationError as exc:
        raise MethodParamError(str(exc)) from exc


# ════════════════════════════════════════════════════════════════
# 已实现的工具方法（design §17.1）
# ════════════════════════════════════════════════════════════════

# ── knowledge.query —— 按锚点/kind 查 mc_knowledge_item（读）────
@register_method(
    "knowledge.query",
    kind="read",
    description="按项目 + 锚点/类型查知识条目（mc_knowledge_item）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "kind": {
                "type": "string",
                "description": "知识条目类型（rationale/component/dependency/rule/api/db_table/...）",
            },
            "key": {"type": "string", "description": "稳定键，精确匹配"},
            "status": {"type": "string", "enum": ["candidate", "confirmed", "stale"]},
            "anchor_id": {"type": "string", "description": "代码锚点 UUID"},
            "anchor": {
                "type": "object",
                "description": "按锚点定位（path/symbol/kind 任一或多个），命中锚点后取其知识条目",
            },
            "limit": {"type": "integer", "description": "返回上限，默认 50"},
            "offset": {"type": "integer", "description": "偏移，默认 0"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def knowledge_query(params: Mapping[str, Any]) -> dict:
    """按锚点/kind 查知识条目（09-knowledge-system.md §3/§4）。"""
    from ..knowledge.models import KnowledgeAnchor, KnowledgeItem

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    query = KnowledgeItem.filter(project_id=project_id)

    if params.get("kind"):
        query = query.filter(kind=params["kind"])
    if params.get("key"):
        query = query.filter(key=params["key"])
    if params.get("status"):
        query = query.filter(status=params["status"])
    if params.get("anchor_id"):
        query = query.filter(anchor_id=_uuid_of(params["anchor_id"], field_name="anchor_id"))

    anchor = params.get("anchor")
    if anchor:
        anchor_query = KnowledgeAnchor.filter(project_id=project_id)
        if isinstance(anchor, Mapping):
            if anchor.get("path"):
                anchor_query = anchor_query.filter(path=anchor["path"])
            if anchor.get("symbol"):
                anchor_query = anchor_query.filter(symbol=anchor["symbol"])
            if anchor.get("kind"):
                anchor_query = anchor_query.filter(kind=anchor["kind"])
        else:  # 字符串：按 path 或 symbol 命中其一
            from tortoise.expressions import Q

            anchor_query = anchor_query.filter(Q(path=str(anchor)) | Q(symbol=str(anchor)))
        anchor_ids = [row.id for row in await anchor_query]
        if not anchor_ids:
            return {"items": [], "count": 0}
        query = query.filter(anchor_id__in=anchor_ids)

    limit = int(params.get("limit", 50))
    offset = int(params.get("offset", 0))
    rows = await query.order_by("kind", "key").limit(limit).offset(offset)

    return {"items": [_item_document(row) for row in rows], "count": len(rows)}


def _item_document(row: Any) -> dict:
    """知识条目行 → JSON 文档（不含 extractor 指纹，那是溯源不是内容）。"""
    return {
        "id": str(row.id),
        "kind": row.kind,
        "key": row.key,
        "title": row.title,
        "content": dict(row.content or {}),
        "status": row.status,
        "confidence": row.confidence,
        "anchor_id": str(row.anchor_id) if row.anchor_id else None,
        "version": row.version,
    }


# ── knowledge.ingest —— 从源抽取结构化知识（写）────────────────
@register_method(
    "knowledge.ingest",
    kind="write",
    description="跑抽取器（Graphify）→ 映射 → 幂等 upsert 到 mc_knowledge_*（过知识门）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID（知识归属）"},
            "project_path": {"type": "string", "description": "被抽取的代码库根目录"},
            "venv": {"type": "string", "description": "隔离的 Graphify venv（缺省读 GRAPHIFY_VENV）"},
            "dry_run": {"type": "boolean", "description": "只抽取/映射不落库（默认 false）"},
        },
        "required": ["project_id", "project_path"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "anchors": {"type": "integer"},
            "edges": {"type": "integer"},
            "persisted": {"type": "boolean"},
        },
    },
)
async def knowledge_ingest(params: Mapping[str, Any]) -> dict:
    """抽取 → 映射 → 幂等入库（adapter ``run`` → ``parse`` → ``upsert``）。"""
    project_id = params["project_id"]
    adapter = GraphifyAdapter(venv=params.get("venv"))
    extraction = await adapter.run(params["project_path"])

    summary: dict[str, Any] = {
        "project_id": str(project_id),
        "project_path": extraction.project_path,
        "extractor_version": extraction.version,
        "duration_s": extraction.duration_s,
        "nodes": len(extraction.nodes),
        "anchors": extraction.graph.anchor_count,
        "rationales": extraction.graph.rationale_count,
        "edges": len(extraction.edges),
        "dropped_edges": extraction.graph.dropped_edges,
    }
    if params.get("dry_run"):
        summary["persisted"] = False
        return summary

    stats = await adapter.upsert(project_id, extraction.nodes, extraction.edges)
    summary["persisted"] = True
    summary["stats"] = {
        "created": stats.created,
        "updated": stats.updated,
        "unchanged": stats.unchanged,
        "edges_dropped": stats.edges_dropped,
        "idempotent_rerun": stats.is_idempotent_rerun,
    }
    return summary


# ── script.validate —— 校验场景（读）────────────────────────────
@register_method(
    "script.validate",
    kind="read",
    description="对一个场景文档跑确定性校验规则（07 校验门），返回 ValidationResult。",
    params_schema={
        "type": "object",
        "properties": {
            "scenario": {"type": "object", "description": "场景文档（含 steps/flow/local_vars）"},
            "methods": {
                "type": "array",
                "description": "已注册的方法引用白名单（rule 8）；缺省则不校验方法引用",
            },
            "unregistered_as_hallucination": {
                "type": "boolean",
                "description": "未注册步骤类型是否额外记 A4 幻觉（默认 false）",
            },
        },
        "required": ["scenario"],
    },
    returns_schema={
        "type": "object",
        "properties": {"valid": {"type": "boolean"}, "issues": {"type": "array"}},
    },
)
async def script_validate(params: Mapping[str, Any]) -> ValidationResult:
    """纯函数校验（无 IO）：工具/断言用引擎默认注册表，方法白名单由调用方给出。"""
    methods = params.get("methods")
    method_registry = {str(ref): True for ref in methods} if methods else None
    return validate_scenario(
        params["scenario"],
        default_tool_registry(),
        default_assertion_registry(),
        method_registry,
        unregistered_as_hallucination=bool(params.get("unregistered_as_hallucination", False)),
    )


# ── requirement.* —— 需求（主脊：版本下挂需求，需求下挂用例/场景/接口/缺陷）──
@register_method(
    "requirement.create",
    kind="write",
    description="建需求（主脊）。code 自动 REQ-xxxx。需求归属版本，用例/场景/接口/缺陷挂到它。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string", "description": "纯文本描述（content 非空时由后端派生覆盖）"},
            "content": {"type": "object", "description": "富文本正文（Tiptap/ProseMirror 文档 JSON）"},
            "mindmap": {"type": "object", "description": "思维导图树 {root:{id,text,children:[]}}"},
            "acceptance_criteria": {"type": "array", "description": "[{text}] 验收标准（可测判据）"},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "module": {"type": "string", "description": "功能模块（业务标签，非分组）"},
            "folder_id": {"type": "string", "description": "归属目录 UUID（mc_test_catalog type=requirement）"},
            "version_id": {"type": "string", "description": "归属版本 UUID"},
            "origin": {"type": "string", "enum": ["manual", "from_doc", "from_import"]},
            "source_ref": {"type": "object", "description": "溯源 {raw_id?, doc?, locator?}"},
            "status": {"type": "string", "enum": ["draft", "active", "deprecated", "archived"]},
            "elements": {"type": "object", "description": "九要素结构化 {business_rules, background, related_conflicts, related_history, users, constraints, unknown_items[]}"},
            "created_by": {"type": "string"},
        },
        "required": ["project_id", "title"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def requirement_create(params: Mapping[str, Any]) -> dict:
    row = await create_requirement(
        project_id=params["project_id"],
        title=params["title"],
        description=params.get("description"),
        content=params.get("content"),
        mindmap=params.get("mindmap"),
        acceptance_criteria=params.get("acceptance_criteria"),
        priority=params.get("priority"),
        module=params.get("module"),
        folder_id=params.get("folder_id"),
        version_id=params.get("version_id"),
        origin=params.get("origin", "manual"),
        source_ref=params.get("source_ref"),
        status=params.get("status", "draft"),
        elements=params.get("elements"),
        created_by=params.get("created_by"),
    )
    return _requirement_document(row)


@register_method(
    "requirement.list",
    kind="read",
    description="列需求（可按版本/状态收窄）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version_id": {"type": "string"},
            "status": {"type": "string", "enum": ["draft", "active", "deprecated", "archived"]},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def requirement_list(params: Mapping[str, Any]) -> dict:
    rows = await list_requirements(
        params["project_id"], version_id=params.get("version_id"), status=params.get("status")
    )
    return {"items": [_requirement_document(r) for r in rows], "count": len(rows)}


@register_method(
    "requirement.get",
    kind="read",
    description="取单个需求详情。",
    params_schema={
        "type": "object",
        "properties": {"requirement_id": {"type": "string"}},
        "required": ["requirement_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def requirement_get(params: Mapping[str, Any]) -> dict:
    row = await get_requirement(_uuid_of(params["requirement_id"], field_name="requirement_id"))
    return {"item": _requirement_document(row)}


@register_method(
    "requirement.update",
    kind="write",
    description="改需求：必须带 change_reason（变更门）。",
    params_schema={
        "type": "object",
        "properties": {
            "requirement_id": {"type": "string"},
            "change_reason": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "content": {"type": "object", "description": "富文本正文（Tiptap/ProseMirror 文档 JSON）"},
            "mindmap": {"type": "object", "description": "思维导图树 {root:{id,text,children:[]}}"},
            "acceptance_criteria": {"type": "array"},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "module": {"type": "string"},
            "folder_id": {"type": "string", "description": "归属目录 UUID（置空传 null 归根）"},
            "version_id": {"type": "string"},
            "origin": {"type": "string", "enum": ["manual", "from_doc", "from_import"]},
            "source_ref": {"type": "object"},
            "status": {"type": "string", "enum": ["draft", "active", "deprecated", "archived"]},
            "elements": {"type": "object", "description": "九要素结构化（26 号文 §2.1）"},
        },
        "required": ["requirement_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def requirement_update(params: Mapping[str, Any]) -> dict:
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "requirement.update 必须提供 change_reason（design §15.12 变更门）"
        )
    values = {k: v for k, v in params.items() if k in REQUIREMENT_EDITABLE_FIELDS}
    row = await update_requirement(
        _uuid_of(params["requirement_id"], field_name="requirement_id"),
        change_reason=str(change_reason).strip(),
        **values,
    )
    return {"item": _requirement_document(row)}


@register_method(
    "requirement.delete",
    kind="write",
    description="删需求（挂到它的用例/场景/接口/缺陷的 requirement_id 置空，不级联删）。",
    params_schema={
        "type": "object",
        "properties": {"requirement_id": {"type": "string"}},
        "required": ["requirement_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}}},
)
async def requirement_delete(params: Mapping[str, Any]) -> dict:
    await delete_requirement(_uuid_of(params["requirement_id"], field_name="requirement_id"))
    return {"deleted": True}


@register_method(
    "requirement.trace",
    kind="read",
    description="**需求追溯**：给一个需求，返回它挂的全部产出（用例/场景/接口/缺陷/关联知识条目）。"
                "用于需求详情与径向图谱。",
    params_schema={
        "type": "object",
        "properties": {"requirement_id": {"type": "string"}},
        "required": ["requirement_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "requirement": {"type": "object"},
            "cases": {"type": "array"},
            "scenarios": {"type": "array"},
            "transports": {"type": "array"},
            "bugs": {"type": "array"},
        },
    },
)
async def requirement_trace(params: Mapping[str, Any]) -> dict:
    """需求 → 挂的全部产出（按 requirement_id 反查各表）。"""
    rid = _uuid_of(params["requirement_id"], field_name="requirement_id")
    req = await get_requirement(rid)

    from ..assets.models import TestScenario as _S, TestTransport as _T
    from ..defects.models import TestBug as _B

    cases = await list_cases(req.project_id)
    scenarios = await _S.filter(project_id=req.project_id, requirement_id=rid)
    transports = await _T.filter(project_id=req.project_id, requirement_id=rid)
    bugs = await _B.filter(project_id=req.project_id, requirement_id=rid)

    return {
        "requirement": _requirement_document(req),
        "cases": [_case_document(c) for c in cases if str(c.requirement_id) == str(rid)],
        "scenarios": [_scenario_document(s) for s in scenarios],
        "transports": [_transport_document(t) for t in transports],
        "bugs": [
            {"id": str(b.id), "code": b.code, "title": b.title, "status": b.status,
             "severity": b.severity, "module": b.module}
            for b in bugs
        ],
    }


# ── requirement.attachment.* —— 需求附件（需求 = 文档：Word/导图/图片/文件）──
@register_method(
    "requirement.attachment.list",
    kind="read",
    description="列需求附件（按创建时间）。文件本体在 OSS，本表只存 URL 与元数据。",
    params_schema={
        "type": "object",
        "properties": {"requirement_id": {"type": "string"}},
        "required": ["requirement_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def requirement_attachment_list(params: Mapping[str, Any]) -> dict:
    rows = await list_requirement_attachments(params["requirement_id"])
    return {"items": [_attachment_document(r) for r in rows], "count": len(rows)}


@register_method(
    "requirement.attachment.add",
    kind="write",
    description="给需求挂附件（url 由浏览器直传 OSS 得到，本方法只落元数据）。",
    params_schema={
        "type": "object",
        "properties": {
            "requirement_id": {"type": "string"},
            "name": {"type": "string"},
            "url": {"type": "string"},
            "mime_type": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "kind": {"type": "string", "enum": ["file", "image", "docx", "xmind"]},
            "source_ref": {"type": "object"},
            "uploaded_by": {"type": "string"},
        },
        "required": ["requirement_id", "name", "url"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def requirement_attachment_add(params: Mapping[str, Any]) -> dict:
    row = await add_requirement_attachment(
        requirement_id=params["requirement_id"],
        name=params["name"],
        url=params["url"],
        mime_type=params.get("mime_type"),
        size_bytes=params.get("size_bytes"),
        kind=params.get("kind", "file"),
        source_ref=params.get("source_ref"),
        uploaded_by=params.get("uploaded_by"),
    )
    return {"item": _attachment_document(row)}


@register_method(
    "requirement.attachment.delete",
    kind="write",
    description="删需求附件（只删本表行，OSS 对象不动）。",
    params_schema={
        "type": "object",
        "properties": {"attachment_id": {"type": "string"}},
        "required": ["attachment_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}}},
)
async def requirement_attachment_delete(params: Mapping[str, Any]) -> dict:
    await delete_requirement_attachment(
        _uuid_of(params["attachment_id"], field_name="attachment_id")
    )
    return {"deleted": True}


# ── case.create —— 建用例（写，过确认门）────────────────────────
@register_method(
    "case.create",
    kind="write",
    description="建用例（业务视角：描述 + 步骤说明 + 场景引用 + 标签）；confirm_source 必填（确认门）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "confirm_source": {
                "type": "string",
                "enum": ["debug", "doc", "user_confirm"],
                "description": "创建依据（§5.1：调试/文档/用户确认）",
            },
            "created_by": {"type": "string", "description": "设计者 UUID（= 责任人）"},
            "intent": {"type": "string", "description": "描述/意图：一句话测什么"},
            "preconditions": {
                "type": ["array", "object"],
                "description": "前置条件（字符串列表，一行一条；旧 dict 兼容，归一为 list）",
                "items": {"type": "string"},
            },
            "expected": {"type": "object", "description": "业务预期（断言判定依据）"},
            "steps": {
                "type": "array",
                "description": "测试步骤说明（业务视角，非可执行）：[{seq, action, expected}]",
                "items": {"type": "object"},
            },
            "dataset_id": {"type": "string", "description": "测试数据 → mc_test_dataset（可空，按场景实际需要）"},
            "confirm_ref": {"type": "object", "description": "确认依据（question_id/文档/调试记录）"},
            "origin": {
                "type": "string",
                "enum": ["ai_generated", "manual", "from_history", "from_requirement"],
                "description": "来源：AI生成/人工/历史用例/需求生成",
            },
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "module": {"type": "string", "description": "所属模块（分组维度，design §20.1）"},
            "feature": {"type": "string", "description": "功能点"},
            "folder_id": {"type": "string", "description": "归属目录 → mc_test_catalog(type=case)"},
            "version_id": {"type": "string", "description": "关联版本 UUID（用例按版本过滤）"},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（挂到需求，主脊）"},
            "test_type": {
                "type": "string",
                "enum": [
                    "functional", "api", "ui", "ai", "performance", "exception", "security",
                    "smoke", "boundary", "permission", "compatibility",
                ],
                "description": "类型（旧单值，保留兼容；新前端写 test_types）",
            },
            "test_types": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "functional", "api", "ui", "ai", "performance", "exception", "security",
                        "smoke", "boundary", "permission", "compatibility",
                    ],
                },
                "description": "类型（多值；逐项枚举校验）",
            },
            "automation_status": {
                "type": "string",
                "enum": ["not_automated", "in_progress", "automated", "broken"],
            },
            "status": {
                "type": "string",
                "enum": ["draft", "pending_confirmation", "active", "deprecated", "archived"],
            },
        },
        "required": ["project_id", "title", "confirm_source", "created_by"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def case_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_case`（code 自动 ``TC-xxxx``）。"""
    case = await create_case(
        project_id=params["project_id"],
        title=params["title"],
        confirm_source=params["confirm_source"],
        created_by=params["created_by"],
        intent=params.get("intent"),
        preconditions=params.get("preconditions"),
        expected=params.get("expected"),
        steps=params.get("steps"),
        dataset_id=params.get("dataset_id"),
        confirm_ref=params.get("confirm_ref"),
        origin=params.get("origin", "manual"),
        priority=params.get("priority"),
        module=params.get("module"),
        feature=params.get("feature"),
        folder_id=params.get("folder_id"),
        version_id=params.get("version_id"),
        requirement_id=params.get("requirement_id"),
        test_type=params.get("test_type"),
        test_types=params.get("test_types"),
        automation_status=params.get("automation_status", "not_automated"),
        status=params.get("status", "draft"),
    )
    return _case_document(case)


def _case_document(case: Any) -> dict:
    return {
        "id": str(case.id),
        "code": case.code,
        "project_id": str(case.project_id),
        "title": case.title,
        "intent": case.intent,
        # 前置条件存字符串列表（一行一条；旧 dict 行由归一层兜住 → 这里也吐 list）
        "preconditions": _clean_case_preconditions(getattr(case, "preconditions", None)),
        "expected": dict(case.expected or {}),
        "steps": list(case.steps or []),
        "confirm_source": case.confirm_source,
        "confirm_ref": dict(case.confirm_ref or {}),
        "origin": case.origin,
        "priority": case.priority,
        "module": case.module,
        "feature": case.feature,
        # 测试数据（→ mc_test_dataset；数据区选中即回填，之前 document 漏了这一列）
        "dataset_id": str(case.dataset_id) if getattr(case, "dataset_id", None) else None,
        "version_id": str(case.version_id) if case.version_id else None,
        "requirement_id": str(case.requirement_id) if case.requirement_id else None,
        # 归属目录（→ mc_test_catalog type=case；裸 UUID）
        "folder_id": str(case.folder_id) if getattr(case, "folder_id", None) else None,
        "test_type": case.test_type,
        # 多值类型：读侧 coalesce（旧单值 test_type 补位）——前端也做同样兜底
        "test_types": _coalesce_test_types(case),
        "slot_key": getattr(case, "slot_key", None),
        "automation_status": case.automation_status,
        "status": case.status,
        "version": case.version,
        # ── 生成环节三列（25 号文 v4.1 §2.1；P2 UI 徽章数据源）────────
        "surface": getattr(case, "surface", None),
        "testpoint_id": str(case.testpoint_id) if getattr(case, "testpoint_id", None) else None,
        # 探索门状态（§环节7：未探索/已探索/探索失败；未过门不进波次）
        "verified_by_run": getattr(case, "verified_by_run", None),
        # 风格锚标记（§环节11：confirm_ref.golden，不新增列）
        "golden": bool((dict(case.confirm_ref or {})).get("golden")),
    }


def _coalesce_test_types(case: Any) -> list[str]:
    """``test_types``（新多值列）与 ``test_type``（旧单值列）合并——空数组回退旧值。"""
    types = getattr(case, "test_types", None) or []
    if types:
        return list(types)
    legacy = getattr(case, "test_type", None)
    return [legacy] if legacy else []


# 前置条件归一（service 层私有 helper；供 document 兼容旧 dict 行）
from ..assets.service import _clean_case_preconditions  # noqa: E402


# ── case.update —— 改用例（写，变更门强制）──────────────────────
@register_method(
    "case.update",
    kind="write",
    description="改用例：**必须**带 change_reason（变更门，design §15.12），并写 mc_test_change_record。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object", "description": "受影响下游 + 同步状态（用例→场景传导）"},
            "rollback_ref": {"description": "可回滚依据"},
            "title": {"type": "string"},
            "intent": {"type": "string"},
            "preconditions": {
                "type": ["array", "object"],
                "description": "前置条件（字符串列表；旧 dict 兼容）",
                "items": {"type": "string"},
            },
            "expected": {"type": "object"},
            "steps": {"type": "array", "description": "测试步骤说明 [{seq, action, expected}]"},
            "dataset_id": {"type": "string", "description": "测试数据 → mc_test_dataset（可空）"},
            "confirm_source": {"type": "string", "enum": ["debug", "doc", "user_confirm"]},
            "confirm_ref": {"type": "object"},
            "origin": {
                "type": "string",
                "enum": ["ai_generated", "manual", "from_history", "from_requirement"],
            },
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "test_type": {
                "type": "string",
                "enum": [
                    "functional", "api", "ui", "ai", "performance", "exception", "security",
                    "smoke", "boundary", "permission", "compatibility",
                ],
            },
            "test_types": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "functional", "api", "ui", "ai", "performance", "exception", "security",
                        "smoke", "boundary", "permission", "compatibility",
                    ],
                },
                "description": "类型（多值；逐项枚举校验）",
            },
            "module": {"type": "string"},
            "feature": {"type": "string"},
            "folder_id": {"type": "string", "description": "归属目录 → mc_test_catalog(type=case)"},
            "version_id": {"type": "string", "description": "关联版本 UUID（用例按版本过滤）"},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
            "automation_status": {
                "type": "string",
                "enum": ["not_automated", "in_progress", "automated", "broken"],
            },
            "status": {
                "type": "string",
                "enum": ["draft", "pending_confirmation", "active", "deprecated", "archived"],
            },
        },
        "required": ["case_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def case_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_case`；``change_reason`` 缺失即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "case.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    values = {key: value for key, value in params.items() if key in CASE_EDITABLE_FIELDS}
    case = await update_case(
        params["case_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **values,
    )
    return _case_document(case)


# ── scenario.create —— 建场景（写）──────────────────────────────
@register_method(
    "scenario.create",
    kind="write",
    description="建场景（实现视角：步骤集合 + 局部变量），可选一次写入 steps；返回组装好的场景全文。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "purpose": {"type": "string"},
            "transport_id": {"type": "string"},
            "env_id": {"type": "string"},
            "version_id": {"type": "string", "description": "关联版本 UUID"},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
            "folder_id": {"type": "string", "description": "归属目录 → mc_test_catalog(type=scenario)"},
            "local_vars": {"type": "object"},
            "flow": {"type": "object"},
            "exec_mode": {
                "type": "string",
                "enum": ["script_only", "hybrid", "ai_only"],
                "description": "已停用（执行语义下沉到步骤类型 ai_judge/agent_action/agent_workflow）；保留入参兼容旧调用方，传入即忽略。",
            },
            "status": {
                "type": "string",
                "enum": ["draft", "verified", "active", "broken", "archived"],
            },
            "code": {"type": "string"},
            "steps": {"type": "array", "description": "步骤数组（§4，可选，逐条过 create_steps 校验）"},
        },
        "required": ["project_id", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def scenario_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`create_scenario` + :func:`create_steps`（code 自动 ``SCN-xxxx``）。"""
    scenario = await create_scenario(
        project_id=params["project_id"],
        name=params["name"],
        purpose=params.get("purpose"),
        transport_id=params.get("transport_id"),
        env_id=params.get("env_id"),
        version_id=params.get("version_id"),
        requirement_id=params.get("requirement_id"),
        folder_id=params.get("folder_id"),
        local_vars=params.get("local_vars"),
        flow=params.get("flow"),
        status=params.get("status", "draft"),
        code=params.get("code"),
    )
    steps = params.get("steps")
    if steps:
        await create_steps(scenario.id, steps)
    return await get_scenario_full(scenario.id)


# ── scenario.get —— 取场景全文（读）────────────────────────────
@register_method(
    "scenario.get",
    kind="read",
    description="取场景全文（场景 + 按 seq 排好的步骤 + flow/local_vars + 反向 case_ids）。",
    params_schema={
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"},
        },
        "required": ["scenario_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "steps": {"type": "array"}}},
)
async def scenario_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.get_scenario_full`。"""
    return await get_scenario_full(params["scenario_id"])


# ── run.execute —— 执行测试（执行，确定性）──────────────────────
@register_method(
    "run.execute",
    kind="execute",
    description="确定性执行一个场景（引擎直接跑）；含 ai_judge/agent_action/agent_workflow 步骤时须带 api_key_id 注入 AI 通道。",
    params_schema={
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"},
            "envs": {"type": "array", "description": "环境继承链文档 [{\"id\",\"parent_id\",\"vars\"}]"},
            "exec_mode": {"type": "string", "enum": ["script_only", "hybrid", "ai_only"]},
            "case_refs": {"type": "array", "description": "本次执行关联的用例 id（写入结果）"},
            "dataset_row": {"type": "object", "description": "数据集当前行（{{data.*}}）"},
            "local": {"type": "object", "description": "初始局部变量"},
            "env": {"type": "object", "description": "初始环境变量（任务级覆盖，优先于场景 env_id）"},
            "api_key_id": {"type": "integer", "description": "平台 API Key id——AI/agent 步骤的推理通道；含此类步骤时必填"},
            "model": {"type": "string", "description": "AI 步骤用的推理模型（缺省用 key 默认模型）"},
            "caller": {"type": "string", "description": "发起用户 id（agent_action 按调用者身份跑）"},
        },
        "required": ["scenario_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"status": {"type": "string"}, "steps": {"type": "array"}},
    },
)
async def run_execute(params: Mapping[str, Any]) -> dict:
    """构造 ``ScenarioExecutor``（PG 场景加载器）执行并返回 ``ScenarioResult`` 文档。

    含 AI/agent 步骤时，用 ``api_key_id`` 解析出网关明文 key，构造
    :class:`~testing_platform.engine.ai.AICapability` 注入 ``ctx.ai``；
    含 script 步骤时，注入方法脚本通道 ``ctx.methods``（加载方法库类源码，
    同作用域全部类编译进同一命名空间支持类间互调）。
    """
    from ..assets import PgScenarioLoader
    from ..ai_channel import AICapability
    from ..method_channel import MethodCapability

    executor = ScenarioExecutor(PgScenarioLoader(), envs=params.get("envs") or [])
    ctx = ExecutionContext()
    ctx.exec_mode = params.get("exec_mode", "script_only")
    ctx.case_refs = list(params.get("case_refs") or [])
    ctx.dataset_row = dict(params.get("dataset_row") or {}) or None
    if params.get("local"):
        ctx.resolver.set_scope("local", params["local"])
    if params.get("env"):
        ctx.resolver.set_scope("env", params["env"])

    # AI/agent 通道：仅在给了 api_key_id 时注入（引擎本身不耦合 LLM）。
    api_key_id = params.get("api_key_id")
    if api_key_id:
        api_key, key_name = await _resolve_run_ai_key(api_key_id, params.get("caller"))
        ctx.ai = AICapability(
            api_key=api_key,
            model=str(params.get("model") or "claude-haiku-4-5-20251001"),
            caller=params.get("caller"),
            api_key_name=key_name,
        )

    # 方法脚本通道：方法类总可以加载（空库只是调用时报可读错）——始终注入。
    ctx.methods = MethodCapability(loader=_make_method_loader(params["scenario_id"]))
    # 调用占位符 {{Class.method(a: local.x)}}：把同一通道挂到解析器（执行器/工具共用）。
    ctx.resolver.method_caller = ctx.methods.call

    result = await executor.execute(params["scenario_id"], ctx)
    return result.to_dict()


# ── run.execute_waves —— run 级波次编排执行（执行，§环节8）────────
@register_method(
    "run.execute_waves",
    kind="execute",
    description="**run 级执行编排**（§环节8「按策略跑，出可信结果」）：选例四闸"
                "（探索门→A/B/C 波次→flaky 隔离区→并行准入）→ 逐波真跑（冒烟硬挂"
                "停后波；env_flaky 退避重试×N，断言失败/product_bug 不重试）→ "
                "结果四态（过/挂/判不了/没裁）+ run.summary。每场景真跑引擎并落"
                " ``mc_test_run_scenario``/``mc_test_step_result``（PgResultWriter）。",
    params_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "gen run UUID（run_task 产出；落结果挂它）"},
            "cases": {
                "type": "array",
                "description": "候选用例行 [{case_id, scenario_id, batch_tier?, "
                               "verified_by_run?, resource?, data_scope?}]；缺省由 "
                               "run.scenario_ids 自动展开（全部视为已探索）",
            },
            "require_explored": {"type": "boolean", "default": False,
                                 "description": "探索门闸（未过门不进波次；缺省关——"
                                 "存量 run 的场景无 verified 概念）"},
            "envs": {"type": "array"},
            "exec_mode": {"type": "string", "enum": ["script_only", "hybrid", "ai_only"]},
            "api_key_id": {"type": "integer"},
            "model": {"type": "string"},
            "caller": {"type": "string"},
            "retry_attempt_cap": {"type": "integer",
                                  "description": "单场景重试轮总闸（缺省 2）"},
        },
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"summary": {"type": "object"}, "wave_gates": {"type": "array"}},
    },
)
async def run_execute_waves(params: Mapping[str, Any]) -> dict:
    """run 级波次执行：select_runners 四闸 → run_waves 逐波 → run.summary 落库。"""
    from ..engine import run_orchestrator as _ro
    from ..engine.executor import ExecutionContext, PgResultWriter, ScenarioExecutor
    from ..tasks import get_run
    from ..tasks.models import TestRun
    from ..tasks.service import list_run_scenarios

    run = await get_run(_uuid_of(params["run_id"], field_name="run_id"))

    # ── 候选集：显式 cases 优先；缺省从 run.scenario_ids 展开（不挂用例的老任务）──
    cases = list(params.get("cases") or [])
    if not cases:
        cases = [
            {"case_id": f"run:{run.id}:scn:{sid}", "scenario_id": str(sid),
             "verified_by_run": "explored"}
            for sid in (run.scenario_ids or [])
        ]
    if not cases:
        raise MethodError("无候选场景（run.scenario_ids 空且未传 cases）",
                          code="invalid_params")

    # ── 选例四闸 ──
    selection = _ro.select_runners(
        cases, require_explored=bool(params.get("require_explored", False)),
    )

    # ── 单场景执行（真引擎 + PgResultWriter 落结果）──
    from ..assets import PgScenarioLoader

    exec_mode = str(params.get("exec_mode") or "script_only")
    writer = PgResultWriter(run_id=run.id)
    executor = ScenarioExecutor(PgScenarioLoader(), result_writer=writer,
                                 envs=list(params.get("envs") or []))

    api_key_id = params.get("api_key_id")
    ai = None
    api_key_name = None
    if api_key_id:
        api_key, api_key_name = await _resolve_run_ai_key(api_key_id, params.get("caller"))
        from ..ai_channel import AICapability

        ai = AICapability(
            api_key=api_key,
            model=str(params.get("model") or "claude-haiku-4-5-20251001"),
            caller=params.get("caller"),
            api_key_name=api_key_name,
        )
    from ..method_channel import MethodCapability

    async def _execute(scenario_id: Any, _kwargs: Mapping[str, Any]) -> dict:
        ctx = ExecutionContext()
        ctx.exec_mode = exec_mode
        if ai is not None:
            ctx.ai = ai
        ctx.methods = MethodCapability(loader=_make_method_loader(scenario_id))
        ctx.resolver.method_caller = ctx.methods.call
        result = await executor.execute(scenario_id, ctx)
        return result.to_dict()

    # ── 逐波执行 ──
    out = await _ro.run_waves(
        selection,
        execute_fn=_execute,
        exec_kwargs={"envs": params.get("envs") or []},
        retry_attempt_cap=int(params.get("retry_attempt_cap") or 2),
    )

    # ── 波次派生 failure_kind 回写（引擎 writer 落 status 不落 kind——§环节8
    # 失败四分类由编排层按重试/环境信号推导后补齐，四态徽章数据源）──
    from ..tasks.models import TestRunScenario

    kind_by_scenario = {
        str(r.get("scenario_id")): r.get("failure_kind")
        for r in out["results"] if r.get("failure_kind") and not r.get("retry_of")
    }
    if kind_by_scenario:
        for row in await list_run_scenarios(run.id):
            fk = kind_by_scenario.get(str(row.scenario_id))
            if fk and row.failure_kind != fk:
                await TestRunScenario.filter(id=row.id).update(failure_kind=fk)

    # ── run 收尾：status=done + summary 落库（执行编排是 run 生命周期的生产者）。
    # summary = 四态计数 + 波次闸门/拦截/隔离明细全量落库——执行页只读 run 行
    # 就能渲染波次时间线，不再回查编排返回。──
    run_summary = {
        **out["summary"],
        "wave_gates": out["wave_gates"],
        "held_out": selection.get("held_out") or [],
        "quarantined": selection.get("quarantined") or [],
        "skipped_retry": out["skipped_retry"],
    }
    await TestRun.filter(id=run.id).update(status="done", summary=run_summary)
    run_row = await get_run(run.id)
    scenarios = await list_run_scenarios(run.id)
    out["run"] = {"id": str(run_row.id), "status": run_row.status,
                  "scenario_rows": len(scenarios)}
    out["summary"] = run_summary
    return {
        "summary": out["summary"],
        "wave_gates": out["wave_gates"],
        "held_out": selection.get("held_out"),
        "quarantined": selection.get("quarantined"),
        "flaky_ledger": out["flaky_ledger"],
        "skipped_retry": out["skipped_retry"],
        "run": out["run"],
    }


def _make_method_loader(scenario_id: Any):
    """构造方法类加载器：绑场景所属项目，拉该作用域全部 active/verified 类源码。

    DB 只在这里访问（surface 层）；engine / method_channel 都不碰 DB。
    """
    from ..assets import list_method_modules
    from ..assets.service import get_scenario

    async def _load() -> list[tuple[str, str, int]]:
        try:
            scenario = await get_scenario(scenario_id)
            pid = scenario.project_id
        except Exception:  # 场景读不到 → 只兜底全局类（method_ref 指全局类仍可用）
            pid = None
        rows = await list_method_modules(pid)  # 给 pid → 本项目 + 全局；None → 只全局
        return [
            (row.code, row.source or "", int(row.version or 1))
            for row in rows
            if row.status in ("active", "verified")
        ]

    return _load


async def _resolve_run_ai_key(api_key_id: Any, caller: Any) -> tuple[str, str]:
    """api_key_id → (网关明文 key, key 名)。复用 agent 的权限解析口径。"""
    from ..engine.errors import EngineError

    try:
        from agent.api import _resolve_caller_api_key

        row = await _resolve_caller_api_key(int(api_key_id), caller or None)
    except ImportError:
        from db import PostgresClient

        row = await PostgresClient.get_api_key_by_id(int(api_key_id))
    except Exception as exc:  # noqa: BLE001 — HTTPException(403) 等统一转 EngineError
        raise EngineError(f"解析 AI 通道 API Key 失败：{exc}") from exc
    if not row or not row.get("key"):
        raise EngineError(f"API Key 不可用（id={api_key_id}）")
    return str(row["key"]), str(row.get("name") or "")


# ── tool.list —— 列已注册工具（读）──────────────────────────────
@register_method(
    "tool.list",
    kind="read",
    description="列出引擎已注册的工具（内置 http/wait/assert；未实现类型不在此列）。",
    params_schema={
        "type": "object",
        "properties": {
            "include_schemas": {"type": "boolean", "description": "是否带 params_schema（默认 true）"},
        },
    },
    returns_schema={"type": "object", "properties": {"tools": {"type": "array"}, "count": {"type": "integer"}}},
)
async def tool_list(params: Mapping[str, Any]) -> dict:
    """引擎 ``ToolRegistry`` 的只读投影（设计「对 AI 固定」的可见面）。"""
    registry = default_tool_registry()
    include_schemas = bool(params.get("include_schemas", True))
    tools: list[dict] = []
    for name in registry.names():
        tool = registry.get(name)
        item: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description or "",
            "step_types": list(tool.step_types),
        }
        if include_schemas:
            item["params_schema"] = dict(tool.params_schema or {})
        tools.append(item)
    return {"tools": tools, "count": len(tools)}


# ── 文档化（list/get 的模型行 → JSON 文档；id/时间统一 str）──────
def _iso(value: Any) -> Optional[str]:
    """datetime → ISO 字符串（None 透传）。"""
    return value.isoformat() if value is not None else None


def _scenario_document(scenario: Any) -> dict:
    """场景行 → 列表文档（轻量；全文走 ``scenario.get``）。"""
    return {
        "id": str(scenario.id),
        "code": scenario.code,
        "project_id": str(scenario.project_id),
        "name": scenario.name,
        "purpose": scenario.purpose,
        "exec_mode": scenario.exec_mode,
        "status": scenario.status,
        "version": scenario.version,
        "version_id": str(scenario.version_id) if scenario.version_id else None,
        "requirement_id": str(scenario.requirement_id) if scenario.requirement_id else None,
        "folder_id": str(scenario.folder_id) if getattr(scenario, "folder_id", None) else None,
    }


def _requirement_document(row: Any) -> dict:
    """需求行 → JSON 文档（``mc_test_requirement``，主脊）。"""
    return {
        "id": str(row.id),
        "code": row.code,
        "project_id": str(row.project_id),
        "version_id": str(row.version_id) if row.version_id else None,
        "title": row.title,
        "description": row.description,
        "content": dict(row.content or {}),
        "mindmap": dict(row.mindmap or {}),
        "acceptance_criteria": list(row.acceptance_criteria or []),
        "priority": row.priority,
        "module": row.module,
        "folder_id": str(row.folder_id) if row.folder_id else None,
        "origin": row.origin,
        "source_ref": dict(row.source_ref or {}),
        "status": row.status,
        # 生成环节产物（26 号文 §2.1）：九步分析产物（含 register 未知项）+
        # 门⓪评审报告 + 该需求在生成流程中的位置（需求节点徽章数据源）。
        # 2026-10-22 补序列化——之前这三列后端有、前端拿不到（需求页未知项无法渲染）。
        "analysis": dict(row.analysis or {}),
        "review": dict(row.review or {}) if hasattr(row, "review") else {},
        "gen_status": getattr(row, "gen_status", None),
        # 九要素结构化（26 号文 §2.1；2026-10-22 加列）
        "elements": dict(row.elements or {}),
        "version": row.version,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _attachment_document(row: Any) -> dict:
    """需求附件行 → JSON 文档（``mc_test_requirement_attachment``）。"""
    return {
        "id": str(row.id),
        "requirement_id": str(row.requirement_id) if row.requirement_id else None,
        "project_id": str(row.project_id) if row.project_id else None,
        "name": row.name,
        "url": row.url,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "kind": row.kind,
        "source_ref": dict(row.source_ref or {}),
        "uploaded_by": str(row.uploaded_by) if row.uploaded_by else None,
        "created_at": _iso(row.created_at),
    }


def _report_document(report: Any) -> dict:
    """报告行 → JSON 文档（快照原样携带，生成时定版）。"""
    return {
        "id": str(report.id),
        "code": report.code,
        "project_id": str(report.project_id),
        "run_id": str(report.run_id),
        "task_id": str(report.task_id) if report.task_id else None,
        "title": report.title,
        "test_version": report.test_version,
        "env_id": str(report.env_id) if report.env_id else None,
        "status": report.status,
        "generator": report.generator,
        "generated_at": _iso(report.generated_at),
        "created_at": _iso(report.created_at),
        "snapshot": dict(report.snapshot or {}),
    }


def _run_document(run: Any) -> dict:
    """运行行 → JSON 文档（``mc_test_run``，04 §1.1）。"""
    return {
        "id": str(run.id),
        "code": run.code,
        "project_id": str(run.project_id),
        "task_id": str(run.task_id) if run.task_id else None,
        "trigger": run.trigger,
        "env_id": str(run.env_id) if run.env_id else None,
        "global_params": dict(run.global_params or {}),
        "scenario_ids": list(run.scenario_ids or []),
        "status": run.status,
        "summary": dict(run.summary) if run.summary else None,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "created_at": _iso(run.created_at),
    }


def _run_scenario_document(row: Any) -> dict:
    """场景级结果行 → JSON 文档（``mc_test_run_scenario``，04 §1.2）。"""
    return {
        "id": str(row.id),
        "run_id": str(row.run_id) if row.run_id else None,
        "scenario_id": row.scenario_id,
        "case_refs": list(row.case_refs or []),
        "exec_mode": row.exec_mode,
        "status": row.status,
        "failure_kind": row.failure_kind,
        "duration_ms": row.duration_ms,
        "healing": dict(row.healing) if row.healing else None,
        "dataset_row": dict(row.dataset_row) if row.dataset_row else None,
        "started_at": _iso(row.started_at),
    }


def _step_result_document(row: Any) -> dict:
    """步骤级结果行 → JSON 文档（``mc_test_step_result``，04 §1.3）。"""
    return {
        "id": str(row.id),
        "run_scenario_id": str(row.run_scenario_id) if row.run_scenario_id else None,
        "step_key": row.step_key,
        "seq": row.seq,
        "type": row.type,
        "status": row.status,
        "attempt": row.attempt,
        "expected": row.expected,
        "actual": row.actual,
        "diff": row.diff,
        "duration_ms": row.duration_ms,
        "checks": row.checks,
        "evidence": row.evidence,
        "started_at": _iso(row.started_at),
    }


def _task_document(task: Any) -> dict:
    """任务行 → JSON 文档（``mc_test_task``，02 §2）。"""
    return {
        "id": str(task.id),
        "code": task.code,
        "project_id": str(task.project_id),
        "title": task.title,
        "purpose": task.purpose,
        "exec_type": task.exec_type,
        "exec_mode": task.exec_mode,
        "status": task.status,
        "schedule": dict(task.schedule) if task.schedule else None,
        "version": task.version,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
    }


def _tag_document(tag: Any) -> dict:
    """标签行 → JSON 文档（``mc_test_tag``，01 §1）。"""
    return {
        "id": str(tag.id),
        "code": tag.code,
        "project_id": str(tag.project_id),
        "name": tag.name,
        "category": tag.category,
        "description": tag.description,
    }


def _transport_document(transport: Any) -> dict:
    """传输层行 → JSON 文档（``mc_test_transport``，design §15.4 接口管理）。"""
    return {
        "id": str(transport.id),
        "code": transport.code,
        "project_id": str(transport.project_id),
        "kind": transport.kind,
        "description": transport.description,
        "module": transport.module,
        "auth": dict(transport.auth or {}),
        "common_headers": dict(transport.common_headers or {}),
        "common_params": dict(transport.common_params or {}),
        "path_rewrite": dict(transport.path_rewrite or {}),
        "status": transport.status,
        "requirement_id": str(transport.requirement_id) if transport.requirement_id else None,
        "version": transport.version,
        "created_at": _iso(transport.created_at),
        "updated_at": _iso(transport.updated_at),
    }


def _api_document(api: Any) -> dict:
    """接口行 → JSON 文档（``mc_test_api``，Apifox 式一等实体）。

    params/headers/request_body/responses 是**结构说明**（schema），原样透传；
    身份是 ``id``，``code`` 只是可读句柄（可空、可重复）。
    """
    return {
        "id": str(api.id),
        "code": api.code,
        "project_id": str(api.project_id),
        "folder_id": str(api.folder_id) if api.folder_id else None,
        "name": api.name,
        "method": api.method,
        "path": api.path,
        "protocol": api.protocol,
        "status": api.status,
        "description": api.description,
        "tags": list(api.tags or []),
        "owner_id": str(api.owner_id) if api.owner_id else None,
        "priority": api.priority,
        "order": api.order,
        "params": list(api.params or []),
        "headers": list(api.headers or []),
        "cookies": list(getattr(api, "cookies", None) or []),
        "body_type": api.body_type,
        "request_body": dict(api.request_body or {}),
        "responses": list(api.responses or []),
        "requirement_id": str(api.requirement_id) if api.requirement_id else None,
        "version_id": str(api.version_id) if api.version_id else None,
        "source": api.source,
        "source_ref": dict(api.source_ref or {}),
        "version": api.version,
        "created_by": str(api.created_by) if api.created_by else None,
        "created_at": _iso(api.created_at),
        "updated_by": str(api.updated_by) if api.updated_by else None,
        "updated_at": _iso(api.updated_at),
    }


def _api_folder_document(folder: Any) -> dict:
    """接口目录行 → JSON 文档（``mc_test_api_folder``）。"""
    return {
        "id": str(folder.id),
        "project_id": str(folder.project_id),
        "parent_id": str(folder.parent_id) if folder.parent_id else None,
        "name": folder.name,
        "order": folder.order,
        "created_at": _iso(folder.created_at),
        "updated_at": _iso(folder.updated_at),
    }


def _catalog_document(folder: Any) -> dict:
    """统一目录行 → JSON 文档（``mc_test_catalog``，带 ``type``）。"""
    return {
        "id": str(folder.id),
        "project_id": str(folder.project_id),
        "type": folder.type,
        "parent_id": str(folder.parent_id) if folder.parent_id else None,
        "name": folder.name,
        "order": folder.order,
        "created_at": _iso(folder.created_at),
        "updated_at": _iso(folder.updated_at),
    }


def _import_job_document(job: Any) -> dict:
    """导入任务行 → JSON 文档（``mc_test_api_import_job``，不含 spec_text 全文）。"""
    schedule = job.schedule if isinstance(job.schedule, dict) else {}
    last_result = job.last_result if isinstance(job.last_result, dict) else {}
    return {
        "id": str(job.id),
        "project_id": str(job.project_id),
        "name": job.name,
        "source_format": job.source_format,
        "spec_url": job.spec_url,
        "has_spec_text": bool(job.spec_text),
        "schedule": schedule,
        "enabled": bool(job.enabled),
        "next_run_at": _iso(job.next_run_at),
        "last_run_at": _iso(job.last_run_at),
        "last_status": job.last_status,
        "last_message": job.last_message,
        "last_result": last_result,
        "created_by": str(job.created_by) if job.created_by else None,
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
    }


def _method_module_document(module: Any, *, methods: Any = None) -> dict:
    """方法类行 → JSON 文档（``mc_test_method_module``，§5.1 重构）。

    ``methods`` 非 None 时附带展开方法清单（类目录检索用，避免再发一次请求）。
    """
    doc = {
        "id": str(module.id),
        "code": module.code,
        "project_id": str(module.project_id) if module.project_id else None,
        "name": module.name,
        "description": module.description,
        "source": module.source or "",
        "async_native": bool(module.async_native),
        "status": module.status,
        "version": module.version,
        "created_at": _iso(module.created_at),
        "updated_at": _iso(module.updated_at),
    }
    if methods is not None:
        doc["methods"] = [_method_document(m) for m in methods]
    return doc


def _method_document(method: Any) -> dict:
    """方法注册行 → JSON 文档（``mc_test_method``，§5.1）。"""
    return {
        "id": str(method.id),
        "module_id": str(method.module_id),
        "name": method.name,
        "full_ref": method.full_ref,
        "signature": dict(method.signature or {}),
        "is_async": bool(method.is_async),
        "description": method.description,
        "created_at": _iso(method.created_at),
        "updated_at": _iso(method.updated_at),
    }


def _change_document(row: Any) -> dict:
    """变更记录行 → JSON 文档（``mc_test_change_record``，design §15.12）。

    ``field_changes`` 是字段级 diff（``{字段: {from, to}}``）——源码变更即
    ``source.from`` / ``source.to`` 全量代码，供「变更历史」展示前后对比。
    """
    return {
        "id": str(row.id),
        "entity_type": row.entity_type,
        "entity_id": str(row.entity_id),
        "created_by": str(row.created_by) if row.created_by else None,
        "change_reason": row.change_reason,
        "field_changes": dict(row.field_changes or {}),
        "propagation": dict(row.propagation or {}),
        "rollback_ref": dict(row.rollback_ref) if row.rollback_ref else None,
        "created_at": _iso(row.created_at),
    }


def _dataset_document(dataset: Any) -> dict:
    """数据集行 → JSON 文档（``mc_test_dataset``，§6 / design §15.4）。"""
    return {
        "id": str(dataset.id),
        "code": dataset.code,
        "project_id": str(dataset.project_id),
        "schema": dict(dataset.schema or {}),
        "rows": list(dataset.rows or []),
        "version": dataset.version,
        "created_at": _iso(dataset.created_at),
        "updated_at": _iso(dataset.updated_at),
    }


def _case_scenario_document(link: Any) -> dict:
    """用例↔场景关联行 → JSON 文档（``mc_test_case_scenario``，01 §2.1）。"""
    return {
        "id": str(link.id),
        "case_id": str(link.case_id),
        "scenario_id": str(link.scenario_id),
        "is_primary": bool(link.is_primary),
        "order": int(link.order or 0),
    }


def _workflow_run_document(run: Any) -> dict:
    """工作流运行行 → JSON 文档（``mc_test_workflow_run``，design §15.9）。"""
    return {
        "id": str(run.id),
        "workflow_def_id": str(run.workflow_def_id),
        "run_id": str(run.run_id) if run.run_id else None,
        "input": dict(run.input or {}),
        "output": dict(run.output or {}),
        "status": run.status,
        "agent_runtime": run.agent_runtime,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


# ── report.list —— 列报告（读）──────────────────────────────────
@register_method(
    "report.list",
    kind="read",
    description="按项目列测试报告（可按 status/run_id 收窄），最新在前。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "status": {"type": "string", "enum": list(REPORT_STATUSES)},
            "run_id": {"type": "string", "description": "按来源运行过滤"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"reports": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def report_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.reports.list_reports`。"""
    rows = await list_reports(
        params["project_id"],
        status=params.get("status"),
        run_id=params.get("run_id"),
    )
    return {"reports": [_report_document(row) for row in rows], "count": len(rows)}


# ── report.get —— 取报告（读）───────────────────────────────────
@register_method(
    "report.get",
    kind="read",
    description="取单个报告（含生成时定版的 snapshot，直接读不回查 run）。",
    params_schema={
        "type": "object",
        "properties": {"report_id": {"type": "string", "description": "报告 UUID"}},
        "required": ["report_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}, "snapshot": {"type": "object"}},
    },
)
async def report_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.reports.get_report`（不存在 → not_found）。"""
    return _report_document(await get_report(params["report_id"]))


# ── report.view —— 受众视图（读，§环节10 报告四类受众）────────────
@register_method(
    "report.view",
    kind="read",
    description="**报告受众视图**（25 号文 §环节10「四类受众视图」）：把已存报告快照"
                "按受众裁剪——executor（执行者：步骤+证据）/ developer（研发：根因+"
                "SQL+关联 bug）/ product（产品：覆盖+风险，零技术细节）/ manager"
                "（管理者：结论先行 Go/No-Go+失败率+置信）。结论先行=返回 dict 第一"
                "键恒为 conclusion。",
    params_schema={
        "type": "object",
        "properties": {
            "report_id": {"type": "string", "description": "报告 UUID"},
            "audience": {"type": "string", "enum": ["executor", "developer", "product", "manager"],
                         "description": "受众视角"},
        },
        "required": ["report_id", "audience"],
    },
    returns_schema={
        "type": "object",
        "properties": {"conclusion": {"type": "object"}, "audience": {"type": "string"}},
    },
)
async def report_view(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.reports.audience_views.audience_view`（纯视图，不回查 run）。"""
    from ..reports.audience_views import AUDIENCES as _AUD, audience_view as _view

    audience = str(params.get("audience") or "").strip()
    if audience not in _AUD:
        raise MethodError(
            f"受众非法：{audience!r}（合法：{list(_AUD)}）", code="bad_request")
    report = await get_report(params["report_id"])
    out = _view(report, audience)
    return {**out, "audience": audience, "report_id": str(report.id)}


# ── release.judge —— 上线判定双条件门（读，§环节10）────────────────
@register_method(
    "release.judge",
    kind="read",
    description="**上线判定双条件门**（25 号文 §环节10）：共识 go **且** 置信≥0.9 **且** "
                "回滚触发器两栏（auto_rollback/manual_rollback）事前声明齐 → go；"
                "任一不满足 → no_go + 聚合理由逐条。成功口径=没回滚且没事故"
                "（success_criterion）。纯判定不落库——落库/触发由调用方处置。",
    params_schema={
        "type": "object",
        "properties": {
            "consensus": {"type": "string", "description": "go | no_go | abstain（仅 go 放行）"},
            "confidence": {"type": "number", "description": "0.0~1.0，≥0.9 才过"},
            "regression": {"type": "object", "description": "回归结果摘要（可选，仅回显不判定）"},
            "rollback_plan": {
                "type": "object",
                "properties": {"auto_rollback": {"type": "string"},
                               "manual_rollback": {"type": "string"}},
                "description": "回滚触发器两栏声明（硬条件：两栏都非空）",
            },
        },
        "required": ["consensus", "confidence"],
    },
    returns_schema={
        "type": "object",
        "properties": {"decision": {"type": "string"}, "passed": {"type": "boolean"},
                       "reasons": {"type": "array"}},
    },
)
async def release_judge(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.generation.waves.release_gate`（纯函数，输入非法抛错）。"""
    from ..generation.waves import release_gate as _gate

    confidence = params.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise MethodError(f"confidence 必须是数字（0.0~1.0），收到 {confidence!r}",
                          code="bad_request")
    try:
        return _gate(
            str(params.get("consensus") or ""),
            float(confidence),
            regression=params.get("regression"),
            rollback_plan=params.get("rollback_plan"),
        )
    except ValueError as exc:
        raise MethodError(str(exc), code="bad_request") from exc


# ── run.list —— 列运行（读）─────────────────────────────────────
@register_method(
    "run.list",
    kind="read",
    description="按项目列运行（可按 task_id/status 收窄），最新在前。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "task_id": {"type": "string", "description": "按任务过滤"},
            "status": {"type": "string", "enum": list(RUN_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"runs": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def run_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.list_runs`。"""
    rows = await list_runs(
        params["project_id"],
        task_id=params.get("task_id"),
        status=params.get("status"),
    )
    return {"runs": [_run_document(row) for row in rows], "count": len(rows)}


# ── run.get —— 取运行（读）──────────────────────────────────────
@register_method(
    "run.get",
    kind="read",
    description="取单次运行（状态/汇总/场景清单；场景级结果走 run 表关联查询）。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "运行 UUID"}},
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
    },
)
async def run_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.get_run`（不存在 → not_found）。"""
    return _run_document(await get_run(params["run_id"]))


# ── run.scenarios —— 列场景级结果（读）───────────────────────────
@register_method(
    "run.scenarios",
    kind="read",
    description="列一次运行下的场景级结果（run → run_scenario 层级，04 §1.2），最新在前。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "运行 UUID"}},
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"scenarios": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def run_scenarios(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.list_run_scenarios`（不存在 run → not_found）。"""
    rows = await list_run_scenarios(params["run_id"])
    return {"scenarios": [_run_scenario_document(row) for row in rows], "count": len(rows)}


# ── run.steps —— 列步骤级结果（读）───────────────────────────────
@register_method(
    "run.steps",
    kind="read",
    description="列一个场景结果下的步骤级结果（run_scenario → step_result 层级，04 §1.3），seq 升序。",
    params_schema={
        "type": "object",
        "properties": {"run_scenario_id": {"type": "string", "description": "场景结果 UUID"}},
        "required": ["run_scenario_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"steps": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def run_steps(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.list_step_results`。"""
    rows = await list_step_results(params["run_scenario_id"])
    return {"steps": [_step_result_document(row) for row in rows], "count": len(rows)}


# ── case.list —— 列用例（读）────────────────────────────────────
@register_method(
    "case.list",
    kind="read",
    description="按项目列用例（可按 status 收窄），code 升序。**分页**：默认 50 条，"
                "封闭上下文批次（17 号文 §6.3）靠 limit/offset 翻页，不全量 dump。"
                "``testpoint_id`` 收窄到某个测试点下的用例（测试点详情页/覆盖导图用，"
                "2026-10-22 加）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "status": {"type": "string", "enum": list(CASE_STATUSES)},
            "testpoint_id": {"type": "string", "description": "只看挂在该测试点下的用例"},
            "limit": {"type": "integer", "description": "返回上限，默认 50"},
            "offset": {"type": "integer", "description": "偏移，默认 0"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"cases": {"type": "array"}, "count": {"type": "integer"},
                       "total": {"type": "integer"}},
    },
)
async def case_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_cases`（分页在方法面做——service 层签名不动）。"""
    from ..assets.models import TestCase

    limit = int(params.get("limit", 50))
    offset = int(params.get("offset", 0))
    qs = TestCase.filter(project_id=_uuid_of(params["project_id"], field_name="project_id"))
    if params.get("status"):
        qs = qs.filter(status=params["status"])
    if params.get("testpoint_id"):
        qs = qs.filter(testpoint_id=_uuid_of(params["testpoint_id"], field_name="testpoint_id"))
    total = await qs.count()
    rows = await qs.order_by("code").limit(limit).offset(offset)
    return {"cases": [_case_document(row) for row in rows], "count": len(rows), "total": total}


# ── case.get —— 取用例（读）─────────────────────────────────────
@register_method(
    "case.get",
    kind="read",
    description="取单个用例（业务字段全文；关联场景走用例↔场景链接表）。",
    params_schema={
        "type": "object",
        "properties": {"case_id": {"type": "string", "description": "用例 UUID"}},
        "required": ["case_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}, "code": {"type": "string"}},
    },
)
async def case_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.get_case`（不存在 → not_found）。"""
    return _case_document(await get_case(params["case_id"]))


# ── scenario.list —— 列场景（读）────────────────────────────────
@register_method(
    "scenario.list",
    kind="read",
    description="按项目列场景（可按 status 收窄），code 升序；全文走 scenario.get。"
                "**分页**：默认 50 条（17 号文 §4.2 封闭上下文批次）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "status": {"type": "string", "enum": list(SCENARIO_STATUSES)},
            "folder_id": {"type": "string", "description": "目录 UUID（收窄到某目录）"},
            "limit": {"type": "integer", "description": "返回上限，默认 50"},
            "offset": {"type": "integer", "description": "偏移，默认 0"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"scenarios": {"type": "array"}, "count": {"type": "integer"},
                       "total": {"type": "integer"}},
    },
)
async def scenario_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_scenarios`（分页在方法面做）。"""
    from ..assets.models import TestScenario

    limit = int(params.get("limit", 50))
    offset = int(params.get("offset", 0))
    qs = TestScenario.filter(project_id=_uuid_of(params["project_id"], field_name="project_id"))
    if params.get("status"):
        qs = qs.filter(status=params["status"])
    if params.get("folder_id"):
        qs = qs.filter(folder_id=_uuid_of(params["folder_id"], field_name="folder_id"))
    total = await qs.count()
    rows = await qs.order_by("code").limit(limit).offset(offset)
    return {"scenarios": [_scenario_document(row) for row in rows],
            "count": len(rows), "total": total}


# ── task.list —— 列任务（读）────────────────────────────────────
@register_method(
    "task.list",
    kind="read",
    description="按项目列测试任务（可按 status 收窄），code 升序。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"tasks": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def task_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.list_tasks`。"""
    rows = await list_tasks(params["project_id"], status=params.get("status"))
    return {"tasks": [_task_document(row) for row in rows], "count": len(rows)}


# ── bug.list —— 列缺陷（读，可按 版本×策略 两维度收窄）──────────
@register_method(
    "bug.list",
    kind="read",
    description="列测试项目下缺陷（测试域自持 mc_test_bug，独立于 coding）；"
                "可按 version_id / strategy_id 收窄——bug 归属 = 测试版本 × 测试策略。"
                "禅道对齐补筛选维度：status（支持别名 fixed→resolved）/severity/priority/"
                "bug_type/resolution/assignee/found_in/escaped/unconfirmed/overdue/longlife/"
                "module/keyword。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "version_id": {"type": "string", "description": "按测试版本收窄"},
            "strategy_id": {"type": "string", "description": "按测试策略收窄"},
            "status": {"type": "string", "enum": list(BUG_STATUSES) + ["fixed"], "description": "按状态收窄（fixed 为别名→resolved）"},
            "status_in": {"type": "array", "items": {"type": "string"}, "description": "按多状态收窄"},
            "severity": {"type": "string", "enum": list(BUG_SEVERITIES)},
            "priority": {"type": "integer", "enum": list(BUG_PRIORITIES)},
            "bug_type": {"type": "string", "enum": list(BUG_TYPES)},
            "resolution": {"type": "string", "enum": list(BUG_RESOLUTIONS)},
            "assignee_id": {"type": "string", "description": "按指派人收窄"},
            "found_in": {"type": "string", "enum": list(BUG_FOUND_INS)},
            "escaped": {"type": "boolean", "description": "只看逃逸 bug"},
            "unconfirmed": {"type": "boolean", "description": "只看未确认（禅道 needconfirm）"},
            "overdue": {"type": "boolean", "description": "只看超期（deadline 已过且未关闭）"},
            "longlife": {"type": "boolean", "description": "只看长期未决（≥ longlife_days 未解决）"},
            "module": {"type": "string", "description": "按功能模块收窄"},
            "keyword": {"type": "string", "description": "关键字搜索（title/symptom/code/keywords）"},
            "folder_id": {"type": "string", "description": "按缺陷目录收窄（mc_test_catalog type=bug；精确匹配，子树过滤前端做）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"bugs": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def bug_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.list_bugs`（返回已是 JSON 文档列表）。"""
    rows = await list_bugs(
        params["project_id"],
        version_id=params.get("version_id"),
        strategy_id=params.get("strategy_id"),
        status=params.get("status"),
        status_in=params.get("status_in"),
        severity=params.get("severity"),
        priority=params.get("priority"),
        bug_type=params.get("bug_type"),
        resolution=params.get("resolution"),
        assignee_id=params.get("assignee_id"),
        found_in=params.get("found_in"),
        escaped=params.get("escaped"),
        unconfirmed=params.get("unconfirmed"),
        overdue=params.get("overdue"),
        longlife=params.get("longlife"),
        module=params.get("module"),
        keyword=params.get("keyword"),
        folder_id=params.get("folder_id"),
    )
    return {"bugs": rows, "count": len(rows)}


# ── knowledge.list —— 列知识条目（读）───────────────────────────
@register_method(
    "knowledge.list",
    kind="read",
    description="按项目列知识条目（可按 kind 收窄）；按锚点定位查内容用 knowledge.query。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "kind": {
                "type": "string",
                "description": "知识条目类型（rationale/component/dependency/rule/api/db_table/...）",
            },
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def knowledge_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.knowledge.list_items`。"""
    project_id = _uuid_of(params["project_id"], field_name="project_id")
    rows = await knowledge_list_items(project_id, kind=params.get("kind"))
    return {"items": [_item_document(row) for row in rows], "count": len(rows)}


# ════════════════════════════════════════════════════════════════
# 测试项目域（design test-project-definition.md §十）
# ════════════════════════════════════════════════════════════════

# ── project.create —— 建测试项目（写）───────────────────────────
@register_method(
    "project.create",
    kind="write",
    description="建测试项目（整个项目维度的独立实体，与 coding 零重合）。code 自动 TP-xxxx。"
                "可带项目级角色成员 testers/developers/product_managers（平台用户 id 列表）。",
    params_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "项目名称（必填）"},
            "description": {"type": "string"},
            "created_by": {"type": "string", "description": "创建者 UUID"},
            "status": {"type": "string", "enum": list(PROJECT_STATUSES)},
            "code": {"type": "string", "description": "显式 code（缺省 TP-xxxx）"},
            "coding_project_id": {
                "type": "string",
                "description": "关联的 coding 项目（桥，可选）——缺陷域 bug=ProjectIssue 经它解析",
            },
            "testers": {"type": "array", "description": "项目测试人员（平台用户 id 列表）"},
            "developers": {"type": "array", "description": "项目开发人员（平台用户 id 列表）"},
            "product_managers": {"type": "array", "description": "产品人员（平台用户 id 列表）"},
        },
        "required": ["name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def project_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.projects.create_project`。"""
    project = await create_project(
        name=params["name"],
        description=params.get("description"),
        created_by=params.get("created_by"),
        status=params.get("status", "active"),
        code=params.get("code"),
        coding_project_id=params.get("coding_project_id"),
        testers=params.get("testers"),
        developers=params.get("developers"),
        product_managers=params.get("product_managers"),
    )
    return _project_document(project)


# ── project.list —— 列测试项目（读）─────────────────────────────
@register_method(
    "project.list",
    kind="read",
    description="列测试项目（可按 status 收窄）。",
    params_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(PROJECT_STATUSES)},
        },
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def project_list(params: Mapping[str, Any]) -> dict:
    rows = await list_projects(status=params.get("status"))
    return {"items": [_project_document(row) for row in rows], "count": len(rows)}


# ── project.get —— 查测试项目（读）──────────────────────────────────
@register_method(
    "project.get",
    kind="read",
    description="查单个测试项目。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "测试项目 UUID"}},
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_get(params: Mapping[str, Any]) -> dict:
    row = await get_project(_uuid_of(params["project_id"], field_name="project_id"))
    return {"item": _project_document(row)}


# ── project.update —— 改测试项目（写，变更门）─────────────────────
@register_method(
    "project.update",
    kind="write",
    description="改测试项目。status/version 结构性变更必须给 change_reason（变更门）。"
                "角色成员字段（testers/developers/product_managers）传数组即覆盖，传空数组清空，不传则不动。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": list(PROJECT_STATUSES)},
            "version": {"type": "integer"},
            "coding_project_id": {
                "type": ["string", "null"],
                "description": "关联的 coding 项目（桥）；传 null 解绑；不传则不动",
            },
            "testers": {"type": "array", "description": "项目测试人员（平台用户 id 列表；覆盖）"},
            "developers": {"type": "array", "description": "项目开发人员（平台用户 id 列表；覆盖）"},
            "product_managers": {"type": "array", "description": "产品人员（平台用户 id 列表；覆盖）"},
            "change_reason": {"type": "string"},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_update(params: Mapping[str, Any]) -> dict:
    kwargs: dict[str, Any] = {
        "name": params.get("name"),
        "description": params.get("description"),
        "status": params.get("status"),
        "version": params.get("version"),
        "change_reason": params.get("change_reason"),
    }
    if "coding_project_id" in params:
        kwargs["coding_project_id"] = params.get("coding_project_id")
    # 角色成员：传了才动（含空数组=清空）；不传则 service 侧 ... 哨兵保持不动。
    for field_name in ("testers", "developers", "product_managers"):
        if field_name in params:
            kwargs[field_name] = params.get(field_name)
    row = await update_project(params["project_id"], **kwargs)
    return {"item": _project_document(row)}


# ── project.archive —— 归档测试项目（写）───────────────────────────
@register_method(
    "project.archive",
    kind="write",
    description="归档测试项目（status=archived，不物理删除；需 change_reason）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "change_reason": {"type": "string"},
        },
        "required": ["project_id", "change_reason"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_archive(params: Mapping[str, Any]) -> dict:
    row = await archive_project(params["project_id"], change_reason=params["change_reason"])
    return {"item": _project_document(row)}


# ── project.git.create —— 配 git（写）─────────────────────────────
@register_method(
    "project.git.create",
    kind="write",
    description="为测试项目配 git 仓库（1-N）。credential_ref 只存引用不存明文（env 变量名/identity id）。"
                "分支属于版本，不在此配——ref_value 可选，留空时自动探测默认分支。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string", "description": "仓库名（如 backend）"},
            "repo_url": {"type": "string", "description": "git 仓库 URL"},
            "ref_type": {"type": "string", "enum": list(GIT_REF_TYPES)},
            "ref_value": {"type": "string", "description": "分支名/tag/commit sha（可选：分支属于版本）"},
            "credential_ref": {"type": "object", "description": "凭据引用，如 {kind: env, name: GIT_TOKEN}"},
        },
        "required": ["project_id", "name", "repo_url"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def project_git_create(params: Mapping[str, Any]) -> dict:
    row = await create_git(
        project_id=params["project_id"],
        name=params["name"],
        repo_url=params["repo_url"],
        ref_type=params.get("ref_type"),
        ref_value=params.get("ref_value"),
        credential_ref=params.get("credential_ref"),
    )
    return _git_document(row)


# ── project.git.list —— 列 git 配置（读）───────────────────────────
@register_method(
    "project.git.list",
    kind="read",
    description="列测试项目的 git 配置（可按 status 收窄）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "status": {"type": "string", "enum": list(GIT_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def project_git_list(params: Mapping[str, Any]) -> dict:
    rows = await list_gits(_uuid_of(params["project_id"], field_name="project_id"), status=params.get("status"))
    return {"items": [_git_document(row) for row in rows], "count": len(rows)}


# ── project.git.update —— 改 git 配置（写）─────────────────────────
@register_method(
    "project.git.update",
    kind="write",
    description="改 git 配置（repo_url/ref/credential_ref/status）。",
    params_schema={
        "type": "object",
        "properties": {
            "git_id": {"type": "string"},
            "repo_url": {"type": "string"},
            "ref_type": {"type": "string", "enum": list(GIT_REF_TYPES)},
            "ref_value": {"type": "string"},
            "credential_ref": {"type": "object"},
            "status": {"type": "string", "enum": list(GIT_STATUSES)},
        },
        "required": ["git_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_git_update(params: Mapping[str, Any]) -> dict:
    row = await update_git(
        params["git_id"],
        repo_url=params.get("repo_url"),
        ref_type=params.get("ref_type"),
        ref_value=params.get("ref_value"),
        credential_ref=params.get("credential_ref"),
        status=params.get("status"),
    )
    return {"item": _git_document(row)}


# ── project.git.delete —— 删 git 配置（写）─────────────────────────
@register_method(
    "project.git.delete",
    kind="write",
    description="删除 git 配置。",
    params_schema={
        "type": "object",
        "properties": {"git_id": {"type": "string"}},
        "required": ["git_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}}},
)
async def project_git_delete(params: Mapping[str, Any]) -> dict:
    await delete_git(_uuid_of(params["git_id"], field_name="git_id"))
    return {"deleted": True}


# ── project.git.branches —— 列远端分支 + 默认分支（读）──────────────
@register_method(
    "project.git.branches",
    kind="read",
    description="列远端分支 + 探测默认分支（git ls-remote）。版本表单据此做**分支下拉**，"
                "默认选远端默认分支——根治「couldn't find remote ref main」（远端是 master 时）。",
    params_schema={
        "type": "object",
        "properties": {
            "git_id": {"type": "string", "description": "仓库配置 UUID"},
            "repo_url": {"type": "string", "description": "或直接给 repo_url（免配仓库先探测）"},
            "credential_ref": {"type": "object", "description": "配合 repo_url 用的凭据引用"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {
            "repo_url": {"type": "string"},
            "branches": {"type": "array"},
            "default_branch": {"type": "string"},
        },
    },
)
async def project_git_branches(params: Mapping[str, Any]) -> dict:
    from ..projects.git_source import list_remote_branches

    git_id = params.get("git_id")
    return await list_remote_branches(
        _uuid_of(git_id, field_name="git_id") if git_id else None,
        repo_url=params.get("repo_url"),
        credential_ref=params.get("credential_ref"),
    )


# ── project.source.create —— 配源（写，A/B 轨）──────────────────────
@register_method(
    "project.source.create",
    kind="write",
    description="配知识源（A 轨内置适配器 builtin / B 轨 agent 通道）。kind=prd|case|code；sync_policy 决定触发。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(SOURCE_KINDS)},
            "name": {"type": "string"},
            "adapter": {"type": "string", "enum": list(SOURCE_ADAPTERS)},
            "adapter_type": {"type": "string", "description": "excel|word|json|xmind|feishu|dingtalk|zentao|custom"},
            "config": {"type": "object", "description": "源配置（路径/URL/接口参数）"},
            "sync_policy": {"type": "string", "enum": list(SOURCE_SYNC_POLICIES)},
            "schedule": {"type": "object", "description": "定时配置（sync_policy=scheduled 时用）"},
        },
        "required": ["project_id", "kind", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def project_source_create(params: Mapping[str, Any]) -> dict:
    row = await create_source(
        project_id=params["project_id"],
        kind=params["kind"],
        name=params["name"],
        adapter=params.get("adapter", "builtin"),
        adapter_type=params.get("adapter_type", "custom"),
        config=params.get("config"),
        sync_policy=params.get("sync_policy", "manual"),
        schedule=params.get("schedule"),
    )
    return _source_document(row)


# ── project.source.list —— 列源配置（读）───────────────────────────
@register_method(
    "project.source.list",
    kind="read",
    description="列测试项目的源配置（可按 kind/adapter/status 收窄）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(SOURCE_KINDS)},
            "adapter": {"type": "string", "enum": list(SOURCE_ADAPTERS)},
            "status": {"type": "string", "enum": list(SOURCE_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def project_source_list(params: Mapping[str, Any]) -> dict:
    rows = await list_sources(
        _uuid_of(params["project_id"], field_name="project_id"),
        kind=params.get("kind"),
        adapter=params.get("adapter"),
        status=params.get("status"),
    )
    return {"items": [_source_document(row) for row in rows], "count": len(rows)}


# ── project.source.update —— 改源配置（写）─────────────────────────
@register_method(
    "project.source.update",
    kind="write",
    description="改源配置（轨道/类型/配置/同步策略/状态）。",
    params_schema={
        "type": "object",
        "properties": {
            "source_id": {"type": "string"},
            "name": {"type": "string"},
            "adapter": {"type": "string", "enum": list(SOURCE_ADAPTERS)},
            "adapter_type": {"type": "string"},
            "config": {"type": "object"},
            "sync_policy": {"type": "string", "enum": list(SOURCE_SYNC_POLICIES)},
            "schedule": {"type": "object"},
            "status": {"type": "string", "enum": list(SOURCE_STATUSES)},
        },
        "required": ["source_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_source_update(params: Mapping[str, Any]) -> dict:
    row = await update_source(
        params["source_id"],
        name=params.get("name"),
        adapter=params.get("adapter"),
        adapter_type=params.get("adapter_type"),
        config=params.get("config"),
        sync_policy=params.get("sync_policy"),
        schedule=params.get("schedule"),
        status=params.get("status"),
    )
    return {"item": _source_document(row)}


# ── project.source.delete —— 删源配置（写）─────────────────────────
@register_method(
    "project.source.delete",
    kind="write",
    description="删除源配置。",
    params_schema={
        "type": "object",
        "properties": {"source_id": {"type": "string"}},
        "required": ["source_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}}},
)
async def project_source_delete(params: Mapping[str, Any]) -> dict:
    await delete_source(_uuid_of(params["source_id"], field_name="source_id"))
    return {"deleted": True}


# ── project.code.init —— 拉取代码快照（写，确定性）─────────────────
@register_method(
    "project.code.init",
    kind="execute",
    description="拉取指定 git 配置的代码**快照**到本地工作区（clone 或 fetch+reset），供图谱抽取。",
    params_schema={
        "type": "object",
        "properties": {"git_id": {"type": "string", "description": "git 配置 UUID"}},
        "required": ["git_id"],
    },
    returns_schema={"type": "object", "properties": {"path": {"type": "string"}, "commit": {"type": "string"}}},
)
async def project_code_init(params: Mapping[str, Any]) -> dict:
    return await init_snapshot(params["git_id"])


# ── project.code.sync —— 同步项目下所有 git 快照（写，确定性）──────
@register_method(
    "project.code.sync",
    kind="execute",
    description="同步测试项目下所有 active git 配置的快照（幂等），返回每个仓库的 clone 结果。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"synced": {"type": "integer"}, "failed": {"type": "integer"}}},
)
async def project_code_sync(params: Mapping[str, Any]) -> dict:
    return await sync_all(params["project_id"])


# ── project.release.create —— 打版本（写）───────────────────────────
@register_method(
    "project.release.create",
    kind="write",
    description="打版本（人工填 version_code；git_tags/prd_versions/case_versions 是指针快照）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version_code": {"type": "string", "description": "人工版本号，如 v1.2"},
            "git_tags": {"type": "array", "description": "[{git_id, ref}]"},
            "prd_versions": {"type": "array", "description": "[{source_id, ref}]"},
            "case_versions": {"type": "array", "description": "[{source_id, ref}]"},
            "note": {"type": "string"},
            "created_by": {"type": "string"},
        },
        "required": ["project_id", "version_code"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version_code": {"type": "string"}}},
)
async def project_release_create(params: Mapping[str, Any]) -> dict:
    row = await create_release(
        project_id=params["project_id"],
        version_code=params["version_code"],
        git_tags=params.get("git_tags"),
        prd_versions=params.get("prd_versions"),
        case_versions=params.get("case_versions"),
        note=params.get("note"),
        created_by=params.get("created_by"),
    )
    return _release_document(row)


# ── project.release.list —— 列版本（读）──────────────────────────────
@register_method(
    "project.release.list",
    kind="read",
    description="列测试项目的版本指针快照。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def project_release_list(params: Mapping[str, Any]) -> dict:
    rows = await list_releases(_uuid_of(params["project_id"], field_name="project_id"))
    return {"items": [_release_document(row) for row in rows], "count": len(rows)}


# ── project.release.diff —— 版本间指针 diff（读，增量测试根基）──────
@register_method(
    "project.release.diff",
    kind="read",
    description="比较两个 release 的版本指针（git_tags/prd_versions/case_versions），报告 added/removed/changed。"
                "（release 表 deprecated：新流程用 project.version）",
    params_schema={
        "type": "object",
        "properties": {
            "base_release_id": {"type": "string"},
            "head_release_id": {"type": "string"},
        },
        "required": ["base_release_id", "head_release_id"],
    },
    returns_schema={"type": "object", "properties": {"diff": {"type": "object"}}},
)
async def project_release_diff(params: Mapping[str, Any]) -> dict:
    base = await get_release(_uuid_of(params["base_release_id"], field_name="base_release_id"))
    head = await get_release(_uuid_of(params["head_release_id"], field_name="head_release_id"))
    return {"diff": diff_releases(base, head)}


# ── project.version.create —— 建版本（写，一等实体）─────────────────
@register_method(
    "project.version.create",
    kind="write",
    description="建测试项目版本（一等实体）。首个版本自动 is_initial=true。"
                "repos=[{git_id, ref_type, ref, is_primary}]：每仓各自选分支/tag/commit，多仓多选；"
                "is_primary=true 的仓是主分支（代表该版本的生成状态）。"
                "可带版本级角色成员 testers/developers/product_managers。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version_code": {"type": "string", "description": "人工版本号，如 v1.2"},
            "name": {"type": "string"},
            "repos": {"type": "array", "description": "[{git_id, ref_type, ref, is_primary, resolved_commit}]"},
            "note": {"type": "string"},
            "created_by": {"type": "string"},
            "testers": {"type": "array", "description": "版本测试人员（平台用户 id 列表；空=继承项目）"},
            "developers": {"type": "array", "description": "版本开发人员（平台用户 id 列表；空=继承项目）"},
            "product_managers": {"type": "array", "description": "版本产品人员（平台用户 id 列表；空=继承项目）"},
        },
        "required": ["project_id", "version_code", "repos"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version_code": {"type": "string"}}},
)
async def project_version_create(params: Mapping[str, Any]) -> dict:
    row = await create_version(
        project_id=params["project_id"],
        version_code=params["version_code"],
        name=params.get("name"),
        repos=params["repos"],
        note=params.get("note"),
        created_by=params.get("created_by"),
        testers=params.get("testers"),
        developers=params.get("developers"),
        product_managers=params.get("product_managers"),
    )
    return _version_document(row)


# ── project.version.list —— 列版本（读）─────────────────────────────
@register_method(
    "project.version.list",
    kind="read",
    description="列测试项目的版本列表。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def project_version_list(params: Mapping[str, Any]) -> dict:
    rows = await list_versions(_uuid_of(params["project_id"], field_name="project_id"))
    return {"items": [_version_document(row) for row in rows], "count": len(rows)}


# ── project.version.get —— 取版本详情（读）──────────────────────────
@register_method(
    "project.version.get",
    kind="read",
    description="取单个版本详情。",
    params_schema={
        "type": "object",
        "properties": {"version_id": {"type": "string"}},
        "required": ["version_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_version_get(params: Mapping[str, Any]) -> dict:
    row = await get_version(_uuid_of(params["version_id"], field_name="version_id"))
    return {"item": _version_document(row)}


# ── project.version.update —— 改版本（写，变更门）────────────────────
@register_method(
    "project.version.update",
    kind="write",
    description="改版本。改了 repos（结构变更）走变更门（change_reason 必填）。"
                "角色成员字段传数组即覆盖（空数组清空），不传则不动。",
    params_schema={
        "type": "object",
        "properties": {
            "version_id": {"type": "string"},
            "name": {"type": "string"},
            "repos": {"type": "array", "description": "[{git_id, ref_type, ref, is_primary, resolved_commit}]"},
            "note": {"type": "string"},
            "testers": {"type": "array", "description": "版本测试人员（覆盖）"},
            "developers": {"type": "array", "description": "版本开发人员（覆盖）"},
            "product_managers": {"type": "array", "description": "版本产品人员（覆盖）"},
            "change_reason": {"type": "string"},
        },
        "required": ["version_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def project_version_update(params: Mapping[str, Any]) -> dict:
    kwargs: dict[str, Any] = {
        "version_id": _uuid_of(params["version_id"], field_name="version_id"),
        "name": params.get("name"),
        "repos": params.get("repos"),
        "note": params.get("note"),
        "change_reason": params.get("change_reason"),
    }
    for field_name in ("testers", "developers", "product_managers"):
        if field_name in params:
            kwargs[field_name] = params.get(field_name)
    row = await update_version(**kwargs)
    return {"item": _version_document(row)}


# ── project.version.roles —— 版本生效角色（读）──────────────────
@register_method(
    "project.version.roles",
    kind="read",
    description="取版本的**生效角色成员**：版本传值即覆盖，否则回退项目级（测试/开发/产品三类）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "version_id": {"type": "string", "description": "版本 UUID（可空=只取项目级）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "testers": {"type": "array"},
            "developers": {"type": "array"},
            "product_managers": {"type": "array"},
        },
    },
)
async def project_version_roles(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.projects.resolve_version_roles`。"""
    from ..projects import resolve_version_roles as _resolve

    version_id = params.get("version_id")
    if not version_id:
        project = await get_project(_uuid_of(params["project_id"], field_name="project_id"))
        return {
            "testers": list(project.testers or []),
            "developers": list(project.developers or []),
            "product_managers": list(project.product_managers or []),
        }
    return await _resolve(params["project_id"], version_id)


# ── project.version.delete —— 删版本（写）───────────────────────────
@register_method(
    "project.version.delete",
    kind="write",
    description="删版本。",
    params_schema={
        "type": "object",
        "properties": {"version_id": {"type": "string"}},
        "required": ["version_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}}},
)
async def project_version_delete(params: Mapping[str, Any]) -> dict:
    await delete_version(_uuid_of(params["version_id"], field_name="version_id"))
    return {"deleted": True}


# ── project.version.build_graph —— 构建版本图谱（执行）───────────────
@register_method(
    "project.version.build_graph",
    kind="execute",
    description="为版本构建代码图谱：逐仓 init_snapshot(ref_override) → Graphify → upsert(version_id)。"
                "**按步骤回报**：每仓 steps=[snapshot|extract|upsert]，单仓单步失败不阻断其余仓库。",
    params_schema={
        "type": "object",
        "properties": {
            "version_id": {"type": "string", "description": "版本 UUID"},
            "venv": {"type": "string", "description": "隔离 Graphify venv（缺省读 GRAPHIFY_VENV）"},
            "dry_run": {"type": "boolean", "description": "只抽取不落库"},
        },
        "required": ["version_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "anchors": {"type": "integer"},
            "edges": {"type": "integer"},
            "status": {"type": "string", "description": "done|partial|failed|no_repos"},
            "repos": {
                "type": "array",
                "description": "逐仓：steps=[{step, status, error?}]，failed_step=失败环节（前端弹框定位）",
            },
        },
    },
)
async def project_version_build_graph(params: Mapping[str, Any]) -> dict:
    """为版本逐仓构建代码图谱，**按步骤回报**（供前端弹框展示进度/定位失败环节）。

    每个仓库一条 steps：clone(拉快照) → extract(Graphify) → upsert(入库)；
    单仓某步失败 → 该步 status=failed + error（带 graphify 原始输出），
    **不阻断其余仓库**；全部失败才整体 failed。
    """
    from ..knowledge import GraphifyAdapter
    from ..projects.git_source import init_snapshot as _init_snapshot
    from ..projects.service import get_git

    version = await get_version(_uuid_of(params["version_id"], field_name="version_id"))
    vid = str(version.id)
    dry_run = bool(params.get("dry_run"))
    adapter = GraphifyAdapter(venv=params.get("venv"))

    total_anchors = 0
    total_edges = 0
    repo_summaries: list[dict[str, Any]] = []

    for repo in version.repos or []:
        git_id = repo.get("git_id")
        try:
            git_name = (await get_git(git_id)).name
        except Exception:  # noqa: BLE001 — 名字只用于展示
            git_name = str(git_id)[:8]

        entry: dict[str, Any] = {
            "git_id": str(git_id),
            "git_name": git_name,
            "requested_ref": repo.get("ref") or None,
            "ref": None,
            "commit": None,
            "snapshot_path": None,
            "anchors": 0,
            "edges": 0,
            "failed_step": None,
            "error": None,
            "steps": [],
        }
        repo_summaries.append(entry)

        def _step(name: str, status: str, detail: Optional[dict[str, Any]] = None) -> None:
            item: dict[str, Any] = {"step": name, "status": status}
            if detail:
                item.update(detail)
            entry["steps"].append(item)

        # ── ① 拉快照（clone/fetch + 子模块）──
        _step("snapshot", "running")
        try:
            snap = await _init_snapshot(git_id, ref_override=repo.get("ref"), version_id=vid)
        except Exception as exc:  # noqa: BLE001 — 带原始 git 输出给前端
            _step("snapshot", "failed", {"error": str(exc)})
            entry["failed_step"] = "snapshot"
            entry["error"] = str(exc)
            continue
        entry["ref"] = snap["ref"]
        entry["commit"] = snap["commit"]
        entry["snapshot_path"] = snap["path"]
        snap_detail: dict[str, Any] = {
            "path": snap["path"],
            "resolved_ref": snap["ref"],
            "fresh_clone": bool(snap.get("fresh_clone")),
        }
        if snap.get("ref_corrected"):
            snap_detail["ref_corrected"] = {"from": snap.get("requested_ref"), "to": snap["ref"]}
        if snap.get("submodule_warning"):
            snap_detail["submodule_warning"] = snap["submodule_warning"]
        _step("snapshot", "done", snap_detail)

        # ── ② Graphify 抽取 ──
        _step("extract", "running")
        try:
            extraction = await adapter.run(snap["path"])
        except Exception as exc:  # noqa: BLE001 — GraphifyRunError 自带 graphify 输出
            _step("extract", "failed", {"error": str(exc)})
            entry["failed_step"] = "extract"
            entry["error"] = str(exc)
            continue
        entry["anchors"] = extraction.graph.anchor_count
        entry["edges"] = len(extraction.edges)
        _step("extract", "done", {"anchors": extraction.graph.anchor_count, "edges": len(extraction.edges)})
        # 抽到了就算进总数（dry_run 不入库，但抽取量仍要回报）
        total_anchors += entry["anchors"]
        total_edges += entry["edges"]

        # ── ③ 入库（幂等 upsert，可重复构建）──
        if dry_run:
            _step("upsert", "skipped")
            continue
        _step("upsert", "running")
        try:
            stats = await adapter.upsert(
                snap["project_id"], extraction.nodes, extraction.edges,
                version_id=vid, repo_name=str(git_id),
            )
        except Exception as exc:  # noqa: BLE001
            _step("upsert", "failed", {"error": str(exc)})
            entry["failed_step"] = "upsert"
            entry["error"] = str(exc)
            continue
        _step("upsert", "done", {
            "created": stats.created, "updated": stats.updated,
            "unchanged": stats.unchanged, "edges_dropped": stats.edges_dropped,
        })
        # resolved commit（+ 被自愈的 ref）回写版本行：版本可复现 + 脏 ref 一次修好
        version.repos = [
            (
                {**r, "resolved_commit": snap["commit"], **({"ref": snap["ref"]} if snap.get("ref_corrected") else {})}
                if str(r.get("git_id")) == str(git_id) else r
            )
            for r in (version.repos or [])
        ]
        await version.save()

    ok = [e for e in repo_summaries if e["failed_step"] is None]
    failed = [e for e in repo_summaries if e["failed_step"] is not None]
    if not dry_run and ok:
        await update_version(version.id, status="graph_built")

    return {
        "version_id": vid,
        "version_code": version.version_code,
        "repos": repo_summaries,
        "anchors": total_anchors,
        "edges": total_edges,
        "persisted": not dry_run and bool(ok),
        # 整体状态：全成=done；部分成=partial；全败=failed；没仓=no_repos
        "status": (
            "no_repos" if not repo_summaries
            else "done" if not failed
            else "partial" if ok
            else "failed"
        ),
    }


# ── project.version.diff —— 两版本 diff（读）─────────────────────────
@register_method(
    "project.version.diff",
    kind="read",
    description="比较两个版本的仓库配置（repos），报告 added/removed/changed refs。"
                "用于增量测试：changed 的仓需要重新构建图谱。",
    params_schema={
        "type": "object",
        "properties": {
            "base_version_id": {"type": "string"},
            "head_version_id": {"type": "string"},
        },
        "required": ["base_version_id", "head_version_id"],
    },
    returns_schema={"type": "object", "properties": {"diff": {"type": "object"}}},
)
async def project_version_diff(params: Mapping[str, Any]) -> dict:
    base = await get_version(_uuid_of(params["base_version_id"], field_name="base_version_id"))
    head = await get_version(_uuid_of(params["head_version_id"], field_name="head_version_id"))

    def _norm(repos: list[dict]) -> dict[str, dict]:
        return {str(r["git_id"]): {"ref": r["ref"], "resolved_commit": r.get("resolved_commit")} for r in (repos or [])}

    b = _norm(base.repos or [])
    h = _norm(head.repos or [])
    changed = {k: {"from": b[k], "to": h[k]} for k in b.keys() & h.keys() if b[k] != h[k]}
    return {
        "added": sorted(h.keys() - b.keys()),
        "removed": sorted(b.keys() - h.keys()),
        "changed": changed,
        "changed_ids": sorted(changed.keys()),
    }


# ── project.graph.query —— 力导向图数据（读，阶段 5 前端）───────────
@register_method(
    "project.graph.query",
    kind="read",
    description="取测试项目的知识图谱力导向图数据：nodes（颜色=类型/透明度=可靠性）+ edges（锚点/条目之间）。"
                "传 version_id 则按版本过滤（图谱绑版本）；不传=全部版本合并视图。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version_id": {"type": "string", "description": "测试项目版本 UUID；按版本过滤图谱"},
            "limit": {"type": "integer", "description": "节点上限，默认 500"},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"nodes": {"type": "array"}, "edges": {"type": "array"}, "count": {"type": "integer"}}},
)
async def project_graph_query(params: Mapping[str, Any]) -> dict:
    from ..knowledge.models import KnowledgeAnchor, KnowledgeEdge

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    limit = int(params.get("limit", 500))
    # 版本过滤：图谱按版本生成/查询（见 knowledge/_version_indexes.py）
    anchor_qs = KnowledgeAnchor.filter(project_id=project_id)
    edge_qs = KnowledgeEdge.filter(project_id=project_id)
    if params.get("version_id"):
        vid = _uuid_of(params["version_id"], field_name="version_id")
        anchor_qs = anchor_qs.filter(version_id=vid)
        edge_qs = edge_qs.filter(version_id=vid)
    anchors = await anchor_qs.order_by("kind", "path", "symbol")
    if len(anchors) > limit:
        anchors = anchors[:limit]
    anchor_ids = {str(a.id) for a in anchors}

    # 只取两端都在节点集合内的边，避免悬挂引用
    edges = await edge_qs.order_by("from_id")
    nodes = [
        {
            "id": str(a.id),
            "kind": a.kind,
            "label": a.label or a.symbol or a.path,
            "path": a.path,
            "symbol": a.symbol,
            "reliability": _anchor_reliability(a),
            "confidence": 1.0,
        }
        for a in anchors
    ]
    kept_edges = [
        {
            "id": str(e.id),
            "source": str(e.from_id),
            "target": str(e.to_id),
            "relation": e.relation,
            "confidence": e.confidence,
        }
        for e in edges
        if str(e.from_id) in anchor_ids and str(e.to_id) in anchor_ids
    ]
    return {
        "nodes": nodes,
        "edges": kept_edges,
        "count": len(nodes),
        "meta": {"node_kinds": sorted({n["kind"] for n in nodes}), "edge_kinds": sorted({e["relation"] for e in kept_edges})},
    }


# ── project.radial_graph —— 需求主脊同心分层（读，图谱页）────────────
@register_method(
    "project.radial_graph",
    kind="read",
    description="**需求主脊径向图**：一次拉全项目分层数据——中心=项目，外圈=版本，再外=需求，"
                "最外=需求下的产出（用例/场景/接口/缺陷）。供图谱页同心圆渲染。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version_id": {"type": "string", "description": "只看某版本（可空=全版本）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "project": {"type": "object"},
            "versions": {"type": "array"},
            "requirements": {"type": "array"},
            "leaves": {"type": "array"},
        },
    },
)
async def project_radial_graph(params: Mapping[str, Any]) -> dict:
    """项目 → 版本 → 需求 → 产出（用例/场景/接口/缺陷）分层数据。"""
    from ..assets.models import TestScenario as _S, TestTransport as _T
    from ..defects.models import TestBug as _B

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    project = await get_project(project_id)
    version_filter = params.get("version_id")
    vf = _uuid_of(version_filter, field_name="version_id") if version_filter else None

    versions = await list_versions(project_id)
    if vf:
        versions = [v for v in versions if v.id == vf]
    version_ids = {str(v.id) for v in versions}

    reqs = await list_requirements(project_id)
    if vf:
        reqs = [r for r in reqs if r.version_id == vf]
    req_ids = {str(r.id) for r in reqs}

    # 产出（叶子）：只取挂在「本轮需求」上的
    cases = await list_cases(project_id)
    scenarios = await _S.filter(project_id=project_id)
    transports = await _T.filter(project_id=project_id)
    bugs = await _B.filter(project_id=project_id)

    leaves: list[dict[str, Any]] = []
    for c in cases:
        rid = str(c.requirement_id) if c.requirement_id else None
        if rid in req_ids:
            leaves.append({"id": str(c.id), "type": "case", "label": c.title,
                           "code": c.code, "requirement_id": rid})
    for s in scenarios:
        rid = str(s.requirement_id) if s.requirement_id else None
        if rid in req_ids:
            leaves.append({"id": str(s.id), "type": "scenario", "label": s.name,
                           "code": s.code, "requirement_id": rid})
    for t in transports:
        rid = str(t.requirement_id) if t.requirement_id else None
        if rid in req_ids:
            leaves.append({"id": str(t.id), "type": "transport", "label": t.description or t.code,
                           "code": t.code, "requirement_id": rid})
    for b in bugs:
        rid = str(b.requirement_id) if b.requirement_id else None
        if rid in req_ids:
            leaves.append({"id": str(b.id), "type": "bug", "label": b.title,
                           "code": b.code, "requirement_id": rid})

    return {
        "project": {"id": str(project.id), "code": project.code, "name": project.name},
        "versions": [
            {"id": str(v.id), "version_code": v.version_code, "name": v.name,
             "is_initial": bool(v.is_initial), "status": v.status}
            for v in versions
        ],
        "requirements": [
            {"id": str(r.id), "code": r.code, "title": r.title, "module": r.module,
             "priority": r.priority, "status": r.status,
             "version_id": str(r.version_id) if r.version_id else None}
            for r in reqs
        ],
        "leaves": leaves,
        "counts": {
            "versions": len(versions), "requirements": len(reqs), "leaves": len(leaves),
        },
    }


# ── 序列化辅助（项目域）──────────────────────────────────────────
def _project_document(row: Any) -> dict:
    return {
        "id": str(row.id),
        "code": row.code,
        "name": row.name,
        "description": row.description,
        "coding_project_id": str(row.coding_project_id) if row.coding_project_id else None,
        "status": row.status,
        "version": row.version,
        "testers": list(row.testers or []),
        "developers": list(row.developers or []),
        "product_managers": list(row.product_managers or []),
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _git_document(row: Any) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "name": row.name,
        "repo_url": row.repo_url,
        "ref_type": row.ref_type,
        "ref_value": row.ref_value,
        "credential_ref": dict(row.credential_ref or {}),
        "status": row.status,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _source_document(row: Any) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "kind": row.kind,
        "adapter": row.adapter,
        "adapter_type": row.adapter_type,
        "name": row.name,
        "config": dict(row.config or {}),
        "sync_policy": row.sync_policy,
        "schedule": dict(row.schedule or {}),
        "last_synced_at": _iso(row.last_synced_at) if row.last_synced_at else None,
        "last_sync_result": dict(row.last_sync_result or {}),
        "status": row.status,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _release_document(row: Any) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "version_code": row.version_code,
        "git_tags": list(row.git_tags or []),
        "prd_versions": list(row.prd_versions or []),
        "case_versions": list(row.case_versions or []),
        "note": row.note,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
    }


def _version_document(row: Any) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "version_code": row.version_code,
        "name": row.name,
        "repos": list(row.repos or []),
        "is_initial": bool(row.is_initial),
        "status": row.status,
        "testers": list(row.testers or []),
        "developers": list(row.developers or []),
        "product_managers": list(row.product_managers or []),
        "note": row.note,
        "created_by": str(row.created_by) if row.created_by else None,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _anchor_reliability(row: Any) -> str:
    """锚点可靠性（图谱透明度）：status 可来自 provenance 或默认 confirmed。

    - deterministic 代码抽取 → ``confirmed``（实色）
    - stale（内容 hash 变了没更新）→ ``stale``（红边）
    - 其余 → ``candidate``（半透明）
    """
    prov = dict(row.provenance or {})
    status = prov.get("status")
    if status == "stale":
        return "stale"
    if status in ("confirmed", "extracted"):
        return "confirmed"
    if status == "candidate":
        return "candidate"
    # 无 provenance 标记：Graphify 抽取的锚点默认 confirmed
    return "confirmed"


# ── source.fetch —— 拉源内容进 L1（写，双轨）────────────────────
@register_method(
    "source.fetch",
    kind="write",
    description="**双轨数据源入口**：A 轨（builtin）给 path，平台确定性解析（json/csv/excel/word/xmind/text）；"
                "B 轨（agent）由 agent 自己取内容后用 ingest_raw 把 bytes 交回。一律落 L1 原始层（幂等按 content_hash）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "source_id": {"type": "string", "description": "源配置 UUID（可空）"},
            "kind": {"type": "string", "description": "product|tech|test（默认 product）"},
            "adapter": {"type": "string", "enum": ["builtin", "agent"]},
            "adapter_type": {"type": "string", "description": "excel|csv|json|jsonl|word|markdown|xmind|text|feishu|..."},
            "path": {"type": "string", "description": "A 轨：服务端可见的文件路径"},
            "content_base64": {"type": "string", "description": "B 轨：agent 取回的内容（base64）"},
            "locator": {"type": "string", "description": "B 轨内容来源描述（URL/文档 id）"},
            "title": {"type": "string"},
        },
        "required": ["project_id", "adapter", "adapter_type"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "content_hash": {"type": "string"}}},
)
async def source_fetch(params: Mapping[str, Any]) -> dict:
    """A 轨文件 → :func:`fetch_file`；B 轨 agent 内容 → :func:`ingest_raw`。"""
    import base64 as _b64

    adapter = params["adapter"]
    common = {
        "project_id": params["project_id"],
        "source_id": params.get("source_id"),
        "kind": params.get("kind", "product"),
        "adapter": adapter,
        "adapter_type": params["adapter_type"],
        "title": params.get("title"),
        "provenance": {"via": "source.fetch", "adapter": adapter},
    }
    if adapter == "builtin":
        path = params.get("path")
        if not path:
            raise MethodParamError("A 轨（builtin）必须给 path")
        return await knowledge_fetch_file(
            project_id=common["project_id"],
            source_id=common["source_id"],
            kind=common["kind"],
            adapter_type=common["adapter_type"],
            path=str(path),
            title=params.get("title"),
            provenance=common["provenance"],
        )
    # B 轨：agent 取内容，平台只负责落 L1
    raw_b64 = params.get("content_base64")
    if not raw_b64:
        raise MethodParamError("B 轨（agent）必须给 content_base64（agent 自己取回内容再交进来）")
    try:
        data = _b64.b64decode(str(raw_b64), validate=False)
    except (ValueError, TypeError) as exc:
        raise MethodParamError(f"content_base64 解码失败：{exc}") from exc
    return await knowledge_ingest_raw(
        **{k: v for k, v in common.items() if k != "adapter"},
        adapter="agent",
        data=data,
        locator=params.get("locator"),
    )


# ── case.enumerate —— 机械枚举骨架用例（execute，0 token）───────
@register_method(
    "case.enumerate",
    kind="execute",
    description="**L1 机械枚举（0 token，秒级）**：读 ``mc_test_api`` 的 params/request_body "
                "结构说明 → 变换表（design/transform_table.py）产骨架 slot → 幂等落 "
                "``mc_test_case``（``slot_key`` 冲突即跳过）。**重跑第二次 created=0 全 skip**。"
                "不调 LLM——这是「几百接口零 token 出骨架」（17 号文 P0）的入口。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "api_ids": {
                "type": "array", "items": {"type": "string"},
                "description": "要枚举的接口 id 列表；空=该项目全部接口",
            },
            "created_by": {"type": "string", "description": "用例责任人 UUID"},
        },
        "required": ["project_id", "created_by"],
    },
    returns_schema={
        "type": "object",
        "properties": {"created": {"type": "integer"}, "skipped": {"type": "integer"},
                       "axes": {"type": "object"}, "per_api": {"type": "array"}},
    },
)
async def case_enumerate(params: Mapping[str, Any]) -> dict:
    """L1 机械枚举入口（包 :func:`testing_platform.design.skeleton.enumerate_skeleton_cases`）。"""
    from ..assets.models import TestApi
    from ..design.skeleton import enumerate_skeleton_cases

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    qs = TestApi.filter(project_id=project_id)
    if params.get("api_ids"):
        qs = qs.filter(id__in=[_uuid_of(x, field_name="api_ids[]") for x in params["api_ids"]])
    api_rows = [
        {
            "id": str(a.id), "method": a.method, "path": a.path, "name": a.name,
            "params": a.params or [], "request_body": a.request_body or {},
        }
        for a in await qs
    ]
    if not api_rows:
        return {"created": 0, "skipped": 0, "axes": {}, "per_api": [],
                "note": "没有匹配的接口（先导入/建接口再枚举）"}
    return await enumerate_skeleton_cases(
        project_id=project_id, api_rows=api_rows,
        created_by=_uuid_of(params["created_by"], field_name="created_by"),
    )


# ── case.scan —— 扫描用例源 → mc_test_case（写，确定性）──────────
@register_method(
    "case.scan",
    kind="write",
    description="**用例扫描**：读一份 L1 raw，确定性抽出用例并 upsert ``mc_test_case``。"
                "列映射可按源配（config.column_map），中英列名自动识别；幂等键 (source_id, external_key)。",
    params_schema={
        "type": "object",
        "properties": {
            "raw_id": {"type": "string", "description": "mc_knowledge_raw UUID"},
            "project_id": {"type": "string"},
            "created_by": {"type": "string", "description": "用例责任人 UUID（确认门）"},
            "column_map": {
                "type": "object",
                "description": "列名→规范字段映射（{\"用例标题\": \"title\", \"预期结果\": \"expected\"}）",
            },
        },
        "required": ["raw_id", "project_id", "created_by"],
    },
    returns_schema={
        "type": "object",
        "properties": {"extracted": {"type": "integer"}, "created": {"type": "integer"},
                       "updated": {"type": "integer"}, "unchanged": {"type": "integer"}},
    },
)
async def case_scan(params: Mapping[str, Any]) -> dict:
    return await knowledge_scan_cases(
        raw_id=params["raw_id"],
        project_id=params["project_id"],
        column_map=params.get("column_map"),
        created_by=params["created_by"],
    )


# ── case.generate —— AI 一键生成用例（execute，驱动 Agent）───────────
@register_method(
    "case.generate",
    kind="execute",
    description="**AI 生成用例（gen_run 编排器，25 号文 v4.1）**：建生成任务 → "
                "逐需求分析（九步+四标准）→ 门⓪需求评审（建问题行）→ 提点（写暂存）→ 停门①。"
                "**产物全部进暂存区（mc_test_proposal），正式用例表零写入**——门①确认后才建测试点。"
                "**异步长任务**：立即返回 gen_run_id；进度看 gen_run.progress（前端唯一渲染源）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "created_by": {"type": "string", "description": "发起者 UUID（用例责任人）"},
            "api_key_id": {"type": "integer", "description": "平台 API Key id（生成用；缺省按项目创建者解析）"},
            "model": {"type": "string", "description": "生成模型（缺省 TESTING_GEN_MODEL）"},
            "run_type": {
                "type": "string",
                "enum": ["full", "plan", "exec", "pre_submit_edit", "incremental", "reopen"],
                "description": "编排类型（缺省 full）",
            },
            "parent_run_id": {"type": "string", "description": "reopen 时的原 gen_run id"},
            "scope": {
                "type": "object",
                "description": "范围：{requirement_ids?（缺省=版本全部需求）, surfaces?}",
            },
            "requirement_id": {"type": "string", "description": "关联需求（单需求模式，可空）"},
            "instruction": {"type": "string", "description": "额外生成要求（自然语言，可空）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "gen_run_id": {"type": "string"},
            "status": {"type": "string"},
            "progress": {"type": "object"},
        },
    },
)
async def case_generate(params: Mapping[str, Any]) -> dict:
    """gen_run 编排器薄壳（25 号文 §1.2）：预检 → 建 gen_run → 后台驱动（分析→门⓪→提点）。

    同步秒回；产物进暂存区；正式用例表零写入（铁律）。
    """
    import asyncio
    import json as _json

    from ..agent_runtime import build_generation_config
    from ..assets.models import TestRequirement
    from ..generation import orchestrator as gen_orch
    from ..generation.models import TestGenRun
    from ..generation.req_analysis import build_analysis_prompt
    from ..generation.point_gen import build_point_prompt
    from ..generation.req_review import build_review_prompt

    project_id = params["project_id"]
    scope = dict(params.get("scope") or {})
    pid_uuid = _uuid_of(project_id, field_name="project_id")

    # ── 预检先行（沿用生成预检契约：不合格当场抛，不留脏数据）──
    requirement_ids = list(scope.get("requirement_ids") or [])
    if params.get("requirement_id"):
        requirement_ids.append(params["requirement_id"])
    if requirement_ids:
        rows = await TestRequirement.filter(
            project_id=pid_uuid, id__in=[_uuid_of(x, field_name="requirement_ids[]") for x in requirement_ids]
        ).values("id")
        found = {str(r["id"]) for r in rows}
        missing = [x for x in requirement_ids if x not in found]
        if missing:
            raise ValueError(f"requirement_id 无效（不存在）：{missing}")

    # 生成用网关配置（缺密钥显式报错）
    gen_key: Optional[str] = None
    if params.get("api_key_id"):
        from .chat import _resolve_api_key

        gen_key, _n = await _resolve_api_key(int(params["api_key_id"]), str(params.get("created_by") or ""))
    gen_config = build_generation_config(api_key=gen_key, model=params.get("model"))

    # ── 建 gen_run（pending）──
    run = await gen_orch.start_run(
        project_id=pid_uuid,
        created_by=_uuid_of(params.get("created_by") or str(_uuid.uuid4()), field_name="created_by"),
        version_id=scope.get("version_id"),
        run_type=params.get("run_type") or "full",
        scope={"requirement_ids": requirement_ids or None, **({"surfaces": scope["surfaces"]} if scope.get("surfaces") else {})},
        parent_run_id=params.get("parent_run_id"),
        config={"model": gen_config.model, "api_key_id": params.get("api_key_id"),
                "instruction": params.get("instruction")},
    )

    # ── 后台驱动（分析→门⓪→提点；产物进暂存区；失败落 run）──
    async def _drive() -> None:
        try:
            from ..agent_runtime import run_chat as _dsh_chat
            from ..agent_runtime import GenerationConfigError

            # LLM 调用器：走平台网关（dsh 单轮、cwd 封闭——每需求一次小调用）
            async def llm_call(prompt: str) -> Any:
                import json as _json

                out = await _dsh_chat(prompt=prompt, config=gen_config)
                text = str(out.get("final_response") or "")
                # 抽 JSON（模型可能带 ```json 围栏）
                text = text.strip()
                if text.startswith("```"):
                    text = text.strip("`")
                    if text.lower().startswith("json"):
                        text = text[4:]
                return _json.loads(text)

            # 环节1+2+3：分析 → 门⓪ → 提点（每需求独立，失败不连坐）
            requirements = await TestRequirement.filter(
                project_id=pid_uuid,
                **({"id__in": [_uuid_of(x, field_name="requirement_ids[]") for x in requirement_ids]} if requirement_ids else {}),
            ).values("id", "content", "description", "analysis", "acceptance_criteria")

            await gen_orch.run_analysis(
                run, llm_call=llm_call,
                prompt_builder=lambda content: build_analysis_prompt(content),
                requirements=[dict(r) for r in requirements],
            )
            run_fresh = await TestGenRun.get(id=run.id)
            await gen_orch.run_requirement_review(
                run_fresh, llm_call=llm_call,
                prompt_builder=lambda a: build_review_prompt(_json.dumps(a, ensure_ascii=False, default=str)),
                requirements=[dict(r) for r in requirements],
            )
            run_fresh = await TestGenRun.get(id=run.id)
            if run_fresh.status == "req_reviewing":
                await gen_orch.run_point_proposal(
                    run_fresh, llm_call=llm_call,
                    prompt_builder=lambda a, w: build_point_prompt(a, w),
                    requirements=[dict(r) for r in requirements],
                )
        except Exception as exc:  # noqa: BLE001 — 失败必须落 run，否则永远 pending
            logger.exception("[case.generate] gen_run drive failed")
            from tortoise import Tortoise

            await TestGenRun.filter(id=run.id).update(
                status="failed",
                decisions=(run.decisions or []) + [{"stage": "drive", "error": str(exc), "at": gen_orch._now().isoformat()}],
            )

    asyncio.create_task(_drive())
    return {
        "gen_run_id": str(run.id),
        "status": run.status,
        "progress": dict(run.progress or {}),
    }


# ── coverage.report —— module × C01-C14 覆盖矩阵（读）────────────
@register_method(
    "coverage.report",
    kind="read",
    description="**覆盖矩阵（0 token）**：module × C01-C14 轴聚合，缺口清单（接口适用机械轴"
                "计数为 0 的格）。数据源 = ``mc_test_case.confirm_ref.axis``（骨架枚举时写入）。"
                "治 17 号文断点 5：漏没漏从此可度量，不再只能信 AI。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "modules": {
                "type": "array", "items": {"type": "string"},
                "description": "参与统计的模块全集（不传=仅从用例里出现的模块）",
            },
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"matrix": {"type": "object"}, "gaps": {"type": "array"},
                       "covered_rate": {"type": "number"}, "modules": {"type": "array"}},
    },
)
async def coverage_report(params: Mapping[str, Any]) -> dict:
    """覆盖矩阵入口（包 :func:`testing_platform.design.coverage.build_matrix`）。"""
    from ..assets.models import TestCase
    from ..design.coverage import build_matrix

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    rows = await TestCase.filter(project_id=project_id).values("module", "confirm_ref")
    axis_records = [
        {"module": r.get("module"), "axis": (r.get("confirm_ref") or {}).get("axis")}
        for r in rows
        if (r.get("confirm_ref") or {}).get("axis")
    ]
    if not axis_records:
        return {
            "matrix": {}, "gaps": [], "covered_rate": 1.0,
            "modules": sorted({str(r.get("module")) for r in rows if r.get("module")}),
            "note": "还没有带 coverage_axis 的用例（先跑 case.enumerate）",
        }
    return build_matrix(axis_records, modules=params.get("modules"))


# ── source.list_raw —— 列 L1 原始快照（读）───────────────────────
@register_method(
    "source.list_raw",
    kind="read",
    description="列 L1 原始层快照（可按 source_id/kind 收窄），看每份内容的 hash/大小/解析情况。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "source_id": {"type": "string"},
            "kind": {"type": "string", "description": "product|tech|test"},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"items": {"type": "array"}, "count": {"type": "integer"}}},
)
async def source_list_raw(params: Mapping[str, Any]) -> dict:
    from ..knowledge.models import KnowledgeRaw

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    qs = KnowledgeRaw.filter(project_id=project_id)
    if params.get("source_id"):
        qs = qs.filter(source_id=_uuid_of(params["source_id"], field_name="source_id"))
    if params.get("kind"):
        qs = qs.filter(kind=params["kind"])
    rows = await qs.order_by("-fetched_at")
    return {"items": [_raw_document(row) for row in rows], "count": len(rows)}


def _raw_document(row: Any) -> dict:
    """L1 raw 行 → 文档（大字段 content/content_text 不回传，只给摘要）。"""
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "source_id": str(row.source_id) if row.source_id else None,
        "kind": row.kind,
        "adapter": row.adapter,
        "adapter_type": row.adapter_type,
        "locator": row.locator,
        "title": row.title,
        "media_type": row.media_type,
        "byte_size": row.byte_size,
        "content_hash": row.content_hash,
        "version": row.version,
        "status": row.status,
        "fetched_at": _iso(row.fetched_at),
    }


# ── knowledge.put —— agent 解析产物落 L2（写，B 轨落地）───────────
@register_method(
    "knowledge.put",
    kind="write",
    description="**B 轨落地**：agent 解析出的知识条目写进 L2（mc_knowledge_item），过锚点闸 + 可测闸。"
                "AI 产出一律 status=candidate、置信度钳到 0.95——升 confirmed 需人工确认（AI 不裁判）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "source_id": {"type": "string", "description": "来源的 mc_test_project_source UUID（可空）"},
            "raw_id": {"type": "string", "description": "内容出自哪份 L1 raw（写进 provenance）"},
            "agent": {"type": "string", "description": "产出者标识（dsh / analyst 工作流…）"},
            "items": {
                "type": "array",
                "description": "[{kind, key, title?, content, confidence?, anchors?:[{path,symbol,kind}], links?:[{to_kind,to_key,relation}]}]",
            },
        },
        "required": ["project_id", "items"],
    },
    returns_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "created": {"type": "integer"},
                       "gated": {"type": "array"}},
    },
)
async def knowledge_put(params: Mapping[str, Any]) -> dict:
    """agent 产出 → :func:`put_items`（幂等键 ``(project_id, kind, key)``）。"""
    prov: dict[str, Any] = {}
    if params.get("raw_id"):
        prov["raw_id"] = str(params["raw_id"])
    return await knowledge_put_items(
        project_id=params["project_id"],
        items=list(params["items"]),
        source_id=params.get("source_id"),
        provenance=prov or None,
        agent=params.get("agent"),
    )


# ── source.adapters —— 列内置适配器（读，前端表单用）──────────────
@register_method(
    "source.adapters",
    kind="read",
    description="列 A 轨内置适配器支持的格式（B 轨不解析，由 agent 自取），供前端源配置表单选项。",
    params_schema={"type": "object", "properties": {}},
    returns_schema={"type": "object", "properties": {"builtin": {"type": "array"}}},
)
async def source_adapters(params: Mapping[str, Any]) -> dict:
    return {
        "builtin": sorted({k: v for k, v in BUILTIN_ADAPTERS.items()}.keys()),
        "case_capable": ["excel", "csv", "json", "jsonl", "xmind"],
        "agent_note": "B 轨（adapter=agent）平台不写解析器；source.fetch 只收 agent 取回的内容，"
                      "解析后用 knowledge.put 落地（过锚点闸+可测闸）",
    }


# ── project.code.graph —— 代码源 → 图谱（execute，确定性全链路）────
@register_method(
    "project.code.graph",
    kind="execute",
    description="**图谱初始化/更新**：拉快照（幂等）→ Graphify 抽取 → 幂等入 mc_knowledge_* → 回写源水位。"
                "这是「初始化图谱 / 更新图谱」的实现，全程确定性不调 LLM。",
    params_schema={
        "type": "object",
        "properties": {
            "git_id": {"type": "string", "description": "git 配置 UUID"},
            "source_id": {"type": "string", "description": "关联的 code 源配置 UUID（可空，用于回写水位）"},
            "venv": {"type": "string", "description": "隔离 Graphify venv（缺省读 GRAPHIFY_VENV）"},
            "dry_run": {"type": "boolean", "description": "只抽取不落库"},
        },
        "required": ["git_id"],
    },
    returns_schema={"type": "object", "properties": {"anchors": {"type": "integer"}, "edges": {"type": "integer"}}},
)
async def project_code_graph(params: Mapping[str, Any]) -> dict:
    """clone/fetch → Graphify → upsert → 回写 last_synced_at。"""
    snap = await init_snapshot(params["git_id"])
    git = await get_git(_uuid_of(params["git_id"], field_name="git_id"))

    adapter = GraphifyAdapter(venv=params.get("venv"))
    extraction = await adapter.run(snap["path"])
    summary: dict[str, Any] = {
        "project_id": snap["project_id"],
        "git_id": str(git.id),
        "repo_url": git.repo_url,
        "snapshot_path": snap["path"],
        "commit": snap["commit"],
        "fresh_clone": snap["fresh_clone"],
        "extractor_version": extraction.version,
        "duration_s": extraction.duration_s,
        "nodes": len(extraction.nodes),
        "anchors": extraction.graph.anchor_count,
        "edges": len(extraction.edges),
        "dropped_edges": extraction.graph.dropped_edges,
        "persisted": False,
    }
    if params.get("dry_run"):
        return summary

    stats = await adapter.upsert(snap["project_id"], extraction.nodes, extraction.edges)
    summary["persisted"] = True
    summary["stats"] = {
        "created": stats.created, "updated": stats.updated,
        "unchanged": stats.unchanged, "edges_dropped": stats.edges_dropped,
        "idempotent_rerun": stats.is_idempotent_rerun,
    }
    if params.get("source_id"):
        # 回写 code 源的同步水位（供图谱新鲜度/stale 判定）
        await mark_source_synced(params["source_id"], result={
            "git_id": str(git.id), "commit": snap["commit"],
            "anchors": summary["anchors"], "edges": summary["edges"],
            "extractor_version": extraction.version,
        })
    return summary


# ════════════════════════════════════════════════════════════════
# 写类方法（design §17.1：前端表单 / Agent 落数据；update_* 走变更门）
# ════════════════════════════════════════════════════════════════

# ── task.create —— 建任务（写）──────────────────────────────────
@register_method(
    "task.create",
    kind="write",
    description="建测试任务（02 §2：标签选例配置 + 执行类型 + 通知声明）；code 自动 TASK-xxxx。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "title": {"type": "string"},
            "created_by": {"type": "string", "description": "创建者 UUID（= 责任人）"},
            "purpose": {"type": "string", "description": "任务用途（这次跑为了验证什么）"},
            "scenario_tags": {
                "type": ["object", "array"],
                "description": "标签选例组合 {all/any/none: [标签名]}（02 §3；也接受裸标签列表）",
            },
            "case_filter": {
                "type": "object",
                "description": "用例收窄 {module/priority/automation_status/status}",
            },
            "exec_type": {"type": "string", "enum": list(TASK_EXEC_TYPES)},
            "schedule": {
                "type": "object",
                "description": "定时配置（exec_type=scheduled 必给；kind ∈ cron/interval/once）",
            },
            "env_id": {"type": "string", "description": "环境 UUID（05 计划，先留占位）"},
            "global_params": {"type": "object", "description": "全局参数（所有场景共享）"},
            "notify": {"type": "object", "description": "通知声明（只存不发；on/channels）"},
            "exec_mode": {"type": "string", "enum": list(TASK_EXEC_MODES)},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
        },
        "required": ["project_id", "title", "created_by"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def task_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.create_task`（code 自动 ``TASK-xxxx``）。"""
    task = await create_task(
        project_id=params["project_id"],
        title=params["title"],
        created_by=params["created_by"],
        purpose=params.get("purpose"),
        scenario_tags=params.get("scenario_tags"),
        case_filter=params.get("case_filter"),
        exec_type=params.get("exec_type", "manual"),
        schedule=params.get("schedule"),
        env_id=params.get("env_id"),
        global_params=params.get("global_params"),
        notify=params.get("notify"),
        exec_mode=params.get("exec_mode", "script_only"),
        status=params.get("status", "draft"),
    )
    return _task_document(task)


# ── task.update —— 改任务（写，变更门强制）──────────────────────
@register_method(
    "task.update",
    kind="write",
    description="改测试任务：**必须**带 change_reason（变更门，design §15.12），写 mc_test_change_record。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object", "description": "受影响下游 + 同步状态（变更传导）"},
            "rollback_ref": {"description": "可回滚依据"},
            "title": {"type": "string"},
            "purpose": {"type": "string"},
            "scenario_tags": {"type": ["object", "array"]},
            "case_filter": {"type": "object"},
            "exec_type": {"type": "string", "enum": list(TASK_EXEC_TYPES)},
            "schedule": {"type": "object"},
            "env_id": {"type": "string"},
            "global_params": {"type": "object"},
            "notify": {"type": "object"},
            "exec_mode": {"type": "string", "enum": list(TASK_EXEC_MODES)},
            "status": {"type": "string", "enum": list(TASK_STATUSES)},
        },
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def task_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.update_task`；``change_reason`` 缺失即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "task.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    values = {key: value for key, value in params.items() if key in TASK_EDITABLE_FIELDS}
    task = await update_task(
        params["task_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **values,
    )
    return _task_document(task)


# ── task.enable / task.disable —— 启停任务（写，默认理由）────────
@register_method(
    "task.enable",
    kind="write",
    description="启用任务（draft/disabled → enabled；变更门默认理由「启用任务」）。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（缺省「启用任务」）"},
        },
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def task_enable(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.enable_task`。"""
    task = await enable_task(params["task_id"], change_reason=params.get("change_reason"))
    return _task_document(task)


@register_method(
    "task.disable",
    kind="write",
    description="停用任务（enabled → disabled；定时/事件触发不再启动）。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（缺省「停用任务」）"},
        },
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def task_disable(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.disable_task`。"""
    task = await disable_task(params["task_id"], change_reason=params.get("change_reason"))
    return _task_document(task)


# ── task.run —— 启动一次运行（写：只建 pending run）──────────────
@register_method(
    "task.run",
    kind="write",
    description="启动一次运行（02 §8）：标签选例 → 建 pending run 行（**不真执行引擎**，执行由 run.execute/调度接手）。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "trigger": {
                "type": "string",
                "enum": list(RUN_TRIGGERS),
                "description": "触发方式（默认 manual）",
            },
            "env_id": {"type": "string", "description": "覆盖任务环境（缺省用任务 env_id）"},
            "global_params_override": {
                "type": "object",
                "description": "叠加全局参数（优先于任务配置；本次快照）",
            },
            "scenario_ids_override": {
                "type": "array",
                "description": "跳过选例直接指定场景（事件触发增量用，v1 预留）",
            },
        },
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def task_run(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.run_task`（返回 pending run 文档）。"""
    run = await run_task(
        params["task_id"],
        trigger=params.get("trigger", "manual"),
        env_id=params.get("env_id"),
        global_params_override=params.get("global_params_override"),
        scenario_ids_override=params.get("scenario_ids_override"),
    )
    return _run_document(run)


# ── scenario.update —— 改场景（写，变更门强制）──────────────────
@register_method(
    "scenario.update",
    kind="write",
    description="改场景：**必须**带 change_reason（变更门）；AI 改完须重过校验门 + 重跑。",
    params_schema={
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object", "description": "受影响下游 + 同步状态"},
            "rollback_ref": {"description": "可回滚依据"},
            "name": {"type": "string"},
            "purpose": {"type": "string"},
            "transport_id": {"type": "string"},
            "env_id": {"type": "string"},
            "version_id": {"type": "string"},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
            "folder_id": {"type": "string", "description": "归属目录 → mc_test_catalog(type=scenario)；null 或空串 = 归「未分组」"},
            "local_vars": {"type": "object"},
            "flow": {"type": "object"},
            "exec_mode": {
                "type": "string",
                "enum": ["script_only", "hybrid", "ai_only"],
                "description": "已停用：保留入参兼容旧调用方，传入即忽略（不写库）。",
            },
            "status": {"type": "string", "enum": list(SCENARIO_STATUSES)},
        },
        "required": ["scenario_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def scenario_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_scenario`；``change_reason`` 缺失即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "scenario.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    values = {key: value for key, value in params.items() if key in SCENARIO_EDITABLE_FIELDS}
    scenario = await update_scenario(
        params["scenario_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **values,
    )
    return _scenario_document(scenario)


# ── scenario.create_steps —— 批量建步骤（写）────────────────────
@register_method(
    "scenario.create_steps",
    kind="write",
    description="给场景批量建步骤（§4：step_key 场景内唯一、seq 缺省续接 +10）；返回组装后的场景全文。",
    params_schema={
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"},
            "steps": {
                "type": "array",
                "description": "步骤数组（step_key/type 必填；params/post_checks/pre_cond/on_error/enabled 可选）",
            },
        },
        "required": ["scenario_id", "steps"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "steps": {"type": "array"}}},
)
async def scenario_create_steps(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_steps`，返回 ``get_scenario_full`` 全文。"""
    await create_steps(params["scenario_id"], params["steps"])
    return await get_scenario_full(params["scenario_id"])


# ── case.link_scenario —— 用例↔场景关联（写，幂等）──────────────
@register_method(
    "case.link_scenario",
    kind="write",
    description="用例↔场景关联（§2.1，幂等）：is_primary 单主场景——置主时清掉同用例其它主标记。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"},
            "is_primary": {"type": "boolean", "description": "是否主场景（默认 false）"},
            "order": {"type": "integer", "description": "关联排列顺序（默认 0）"},
        },
        "required": ["case_id", "scenario_id"],
    },
    returns_schema={"type": "object", "properties": {"case_id": {"type": "string"}, "scenario_id": {"type": "string"}}},
)
async def case_link_scenario(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.link_case_scenario`（幂等，返回关联行文档）。"""
    link = await link_case_scenario(
        params["case_id"],
        params["scenario_id"],
        is_primary=bool(params.get("is_primary", False)),
        order=int(params.get("order", 0)),
    )
    return _case_scenario_document(link)


# ── case.tag —— 用例打标签（写，幂等）───────────────────────────
@register_method(
    "case.tag",
    kind="write",
    description="给用例打标签（§2.2，幂等：重复打标返回既有关联）。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "tag_id": {"type": "string", "description": "标签 UUID"},
        },
        "required": ["case_id", "tag_id"],
    },
    returns_schema={"type": "object", "properties": {"case_id": {"type": "string"}, "tag_id": {"type": "string"}}},
)
async def case_tag(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.tag_case`（幂等）。"""
    link = await tag_case(params["case_id"], params["tag_id"])
    return {"case_id": str(link.case_id), "tag_id": str(link.tag_id)}


# ── case.scenarios —— 列用例↔场景关联（读，含场景详情）───────────
@register_method(
    "case.scenarios",
    kind="read",
    description="列某用例关联的场景（§2.1）：关联行 join 场景详情（code/name/status + is_primary/order），order 升序。",
    params_schema={
        "type": "object",
        "properties": {"case_id": {"type": "string", "description": "用例 UUID"}},
        "required": ["case_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"links": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def case_scenarios(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_case_scenarios_full`。"""
    rows = await list_case_scenarios_full(params["case_id"])
    return {"links": rows, "count": len(rows)}


# ── case.unlink_scenario —— 解除用例↔场景关联（写，幂等）─────────
@register_method(
    "case.unlink_scenario",
    kind="write",
    description="解除用例↔场景关联（§2.1，幂等：不存在不报错），返回删除行数。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"},
        },
        "required": ["case_id", "scenario_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def case_unlink_scenario(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.unlink_case_scenario`（幂等）。"""
    deleted = await unlink_case_scenario(params["case_id"], params["scenario_id"])
    return {"deleted": int(deleted)}


# ── case.tags —— 列用例已打标签（读）─────────────────────────────
@register_method(
    "case.tags",
    kind="read",
    description="列某用例已打标签（§2.2，join mc_test_tag），category/code 升序。",
    params_schema={
        "type": "object",
        "properties": {"case_id": {"type": "string", "description": "用例 UUID"}},
        "required": ["case_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"tags": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def case_tags(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_case_tags`。"""
    rows = await list_case_tags(params["case_id"])
    return {"tags": [_tag_document(row) for row in rows], "count": len(rows)}


# ── tag.create —— 建标签（写）───────────────────────────────────
@register_method(
    "tag.create",
    kind="write",
    description="建标签（§1：category 固定四类 purpose/priority/module/type；code 缺省取 name）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "category": {"type": "string", "enum": list(TAG_CATEGORIES)},
            "code": {"type": "string", "description": "稳定键（缺省取 name）"},
            "description": {"type": "string"},
        },
        "required": ["project_id", "name", "category"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def tag_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_tag`。"""
    tag = await create_tag(
        project_id=params["project_id"],
        name=params["name"],
        category=params["category"],
        code=params.get("code"),
        description=params.get("description"),
    )
    return _tag_document(tag)


# ── report.generate —— 生成报告（写，生成时定版）────────────────
@register_method(
    "report.generate",
    kind="write",
    description="为一次运行生成报告（03 §2.1）：聚合结果表组装快照并落库（生成时定版，status=ready）。",
    params_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "运行 UUID"},
            "title": {"type": "string", "description": "缺省取任务标题 + 日期，再退 run code"},
            "test_version": {"type": "string", "description": "缺省取 run.global_params.test_version/version"},
            "env_id": {"type": "string", "description": "缺省取 run.env_id"},
            "generator": {"type": "string", "enum": list(REPORT_GENERATORS), "description": "auto/manual（默认 auto）"},
        },
        "required": ["run_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def report_generate(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.reports.generate_report`（生成时快照，返回报告文档）。"""
    report = await generate_report(
        params["run_id"],
        title=params.get("title"),
        test_version=params.get("test_version"),
        env_id=params.get("env_id"),
        generator=params.get("generator", "auto"),
    )
    return _report_document(report)


# ── bug.create —— 从失败建 bug（写，测试域自持）─────────────────
@register_method(
    "bug.create",
    kind="write",
    description="建缺陷（测试域自持 mc_test_bug，独立于 coding；同 fingerprint 去重归并）。"
                "**可手动录入**：run_scenario_id 可空（无来源运行）。",
    params_schema={
        "type": "object",
        "properties": {
            "run_scenario_id": {"type": "string", "description": "来源执行行（mc_test_run_scenario.id，可空=手动录入）"},
            "title": {"type": "string"},
            "symptom": {"type": "string", "description": "症状描述"},
            "fingerprint": {"type": "string", "description": "失败指纹（跨 run 去重键；缺省不去重）"},
            "repro": {"type": "object", "description": "复现维度 steps/env/data/rate"},
            "case_refs": {"type": "array", "description": "关联用例 id 列表"},
            "scenario_refs": {"type": "array", "description": "关联场景 id 列表"},
            "scenario_id": {"type": "string", "description": "关联场景 id"},
            "severity": {"type": "string", "enum": list(BUG_SEVERITIES)},
            "user_id": {"type": "string", "description": "创建者（平台用户）"},
            "project_id": {"type": "string", "description": "测试项目 id"},
            "from_run_id": {"type": "string", "description": "来源运行（mc_test_run.id），可选"},
            "assignee_id": {"type": "string", "description": "指派给谁，可选"},
            "priority": {"type": "integer", "enum": list(BUG_PRIORITIES), "description": "1/2/3/4（默认 2）"},
            "tags": {"type": "array", "description": "追加标签（自动带 test_discovered 来源标记）"},
            "summary": {"type": "string"},
            "module": {"type": "string", "description": "所属功能模块（分组维度）"},
            "version_id": {"type": "string", "description": "关联版本 UUID（缺陷按版本过滤）"},
            "strategy_id": {"type": "string", "description": "关联测试策略 UUID（缺陷第二维度：版本×策略）"},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
            # ── 禅道对齐新增入参 ──
            "bug_type": {"type": "string", "enum": list(BUG_TYPES), "description": "缺陷类型"},
            "resolution": {"type": "string", "enum": list(BUG_RESOLUTIONS), "description": "解决方案（建时一般留空，走 bug.transition）"},
            "opened_build": {"type": "string", "description": "影响版本（发现时的构建/版本号）"},
            "resolved_build": {"type": "string", "description": "解决版本"},
            "duplicate_bug_id": {"type": "string", "description": "重复 bug id"},
            "related_bug_ids": {"type": "array", "description": "相关 bug id 列表"},
            "deadline": {"type": "string", "description": "处理期限（ISO 时间）"},
            "found_in": {"type": "string", "enum": list(BUG_FOUND_INS), "description": "发现阶段"},
            "escaped": {"type": "boolean", "description": "逃逸 bug（上线后才发现）"},
            "os": {"type": "string", "enum": list(BUG_OS_LIST)},
            "browser": {"type": "string", "enum": list(BUG_BROWSER_LIST)},
            "hardware": {"type": "string", "description": "硬件环境"},
            "keywords": {"type": "string", "description": "关键词"},
            "mailto": {"type": "array", "description": "抄送（账号 id 列表）"},
            "feedback_by": {"type": "string", "description": "反馈者"},
            "sub_status": {"type": "string", "description": "子状态"},
            "color": {"type": "string", "description": "标题色标"},
            "confirmed": {"type": "boolean", "description": "人工录入且已认可时置真 → 状态直落 confirmed"},
            "folder_id": {"type": "string", "description": "归属目录（mc_test_catalog type=bug 的目录 id，可空=未分组）"},
        },
        "required": ["title", "project_id"],
    },
    returns_schema={"type": "object", "properties": {"bug_id": {"type": "string"}}},
)
async def bug_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.create_bug_from_failure`（返回 bug_id）。"""
    bug_id = await create_bug_from_failure(
        params.get("run_scenario_id"),
        params["title"],
        params.get("symptom"),
        params.get("fingerprint"),
        repro=params.get("repro"),
        case_refs=params.get("case_refs"),
        scenario_refs=params.get("scenario_refs"),
        scenario_id=params.get("scenario_id"),
        severity=params.get("severity", "major"),
        user_id=params.get("user_id"),
        project_id=params["project_id"],
        from_run_id=params.get("from_run_id"),
        assignee_id=params.get("assignee_id"),
        priority=params.get("priority", 2),
        tags=params.get("tags"),
        summary=params.get("summary"),
        module=params.get("module"),
        version_id=params.get("version_id"),
        strategy_id=params.get("strategy_id"),
        requirement_id=params.get("requirement_id"),
        bug_type=params.get("bug_type"),
        resolution=params.get("resolution"),
        opened_build=params.get("opened_build"),
        resolved_build=params.get("resolved_build"),
        duplicate_bug_id=params.get("duplicate_bug_id"),
        related_bug_ids=params.get("related_bug_ids"),
        deadline=params.get("deadline"),
        found_in=params.get("found_in"),
        escaped=params.get("escaped", False),
        os=params.get("os"),
        browser=params.get("browser"),
        hardware=params.get("hardware"),
        keywords=params.get("keywords"),
        mailto=params.get("mailto"),
        feedback_by=params.get("feedback_by"),
        sub_status=params.get("sub_status"),
        color=params.get("color"),
        confirmed=params.get("confirmed", False),
        folder_id=params.get("folder_id"),
    )
    return {"bug_id": str(bug_id)}


# ── bug.get —— 取缺陷详情（读，含根因）──────────────────────────
@register_method(
    "bug.get",
    kind="read",
    description="取单个缺陷详情（含全部根因行）。",
    params_schema={
        "type": "object",
        "properties": {"bug_id": {"type": "string", "description": "缺陷 UUID"}},
        "required": ["bug_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def bug_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.get_bug`。"""
    return await get_bug(params["bug_id"])


# ── bug.root_cause —— 挂根因（写，过根因门）─────────────────────
@register_method(
    "bug.root_cause",
    kind="write",
    description="给 bug 追加根因分析（1:N）：**根因门**——evidence_chain 为空即拒绝（design §18）。",
    params_schema={
        "type": "object",
        "properties": {
            "bug_id": {"type": "string", "description": "缺陷 id（mc_test_bug.id）"},
            "root_cause": {
                "type": "object",
                "description": (
                    "根因字段（location/classification/attributed_commit/hypothesis/"
                    "confidence/fix_suggestion/healing_suggestion/blast_radius/"
                    "evidence_chain）；evidence_chain 必填非空（根因门）"
                ),
            },
        },
        "required": ["bug_id", "root_cause"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "evidence_chain": {"type": "array"}}},
)
async def bug_root_cause(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.attach_root_cause`（空 evidence_chain → 根因门拒）。"""
    from ..defects.service import _root_cause_dict

    row = await attach_root_cause(params["bug_id"], params["root_cause"])
    return _root_cause_dict(row)


# ── bug.update —— 改非状态字段（写，变更门 + closed 守卫）───────
@register_method(
    "bug.update",
    kind="write",
    description="改缺陷的非状态字段（禅道对齐）：**变更门**——change_reason 必填；"
                "**status 不能直接改**（状态流转走 bug.transition，动作守卫表）；"
                "closed 终态拒编辑。",
    params_schema={
        "type": "object",
        "properties": {
            "bug_id": {"type": "string", "description": "缺陷 UUID"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "title": {"type": "string"},
            "symptom": {"type": "string", "description": "症状描述"},
            "summary": {"type": "string"},
            "resolution_note": {"type": "string", "description": "解决备注（自由文本，与 resolution 枚举互补）"},
            "severity": {"type": "string", "enum": list(BUG_SEVERITIES)},
            "priority": {"type": "integer", "enum": list(BUG_PRIORITIES)},
            "module": {"type": "string", "description": "所属功能模块"},
            "assignee_id": {"type": "string", "description": "指派给（平台用户）"},
            "version_id": {"type": "string"},
            "strategy_id": {"type": "string"},
            "requirement_id": {"type": "string"},
            "tags": {"type": "array"},
            "repro": {"type": "object", "description": "复现维度 steps/env/data/rate"},
            "case_refs": {"type": "array"},
            "scenario_refs": {"type": "array"},
            "scenario_id": {"type": "string"},
            "bug_type": {"type": "string", "enum": list(BUG_TYPES)},
            "opened_build": {"type": "string"},
            "resolved_build": {"type": "string"},
            "duplicate_bug_id": {"type": "string"},
            "related_bug_ids": {"type": "array"},
            "deadline": {"type": "string", "description": "处理期限（ISO 时间）"},
            "found_in": {"type": "string", "enum": list(BUG_FOUND_INS)},
            "escaped": {"type": "boolean"},
            "os": {"type": "string", "enum": list(BUG_OS_LIST)},
            "browser": {"type": "string", "enum": list(BUG_BROWSER_LIST)},
            "hardware": {"type": "string"},
            "keywords": {"type": "string"},
            "mailto": {"type": "array"},
            "feedback_by": {"type": "string"},
            "sub_status": {"type": "string"},
            "color": {"type": "string"},
            "actor_id": {"type": "string", "description": "操作人（平台用户）"},
        },
        "required": ["bug_id", "change_reason"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def bug_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.update_bug`（变更门 + closed 守卫）。"""
    return await update_bug(
        params["bug_id"],
        {k: v for k, v in params.items()
         if k not in ("bug_id", "change_reason", "actor_id")},
        change_reason=params["change_reason"],
        actor_id=params.get("actor_id"),
    )


# ── bug.transition —— 动作流转（写，动作守卫表驱动）─────────────
@register_method(
    "bug.transition",
    kind="write",
    description="对缺陷执行状态流转动作（禅道对齐，动作守卫表驱动）："
                "confirm 确认 / assign 指派 / triage 分流 / resolve 解决 / "
                "verify 验证 / close 关闭 / reopen 重开。"
                "守卫：from-status 不符抛 bug_transition_not_allowed（带 allowed_actions）；"
                "resolve 必填 resolution（fixed→resolved_build、duplicate→duplicate_bug_id 条件必填，"
                "willnotfix/bydesign → rejected 终态）；reopen 自增 activated_count 并清解决方案/时间戳。",
    params_schema={
        "type": "object",
        "properties": {
            "bug_id": {"type": "string", "description": "缺陷 UUID"},
            "action": {
                "type": "string",
                "enum": list(BUG_ACTIONS),
                "description": "流转动作（confirm/assign/triage/resolve/verify/close/reopen）",
            },
            "resolution": {"type": "string", "enum": list(BUG_RESOLUTIONS), "description": "解决方案（resolve 必填）"},
            "resolved_build": {"type": "string", "description": "解决版本（resolution=fixed 必填）"},
            "duplicate_bug_id": {"type": "string", "description": "重复 bug id（resolution=duplicate 必填）"},
            "assignee_id": {"type": "string", "description": "指派给（assign 必填）"},
            "comment": {"type": "string", "description": "操作备注（落活动流）"},
            "actor_id": {"type": "string", "description": "操作人（平台用户）"},
        },
        "required": ["bug_id", "action"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def bug_transition(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.apply_bug_action`（返回动作后的完整文档）。"""
    return await apply_bug_action(
        params["bug_id"],
        params["action"],
        actor_id=params.get("actor_id"),
        comment=params.get("comment"),
        resolution=params.get("resolution"),
        resolved_build=params.get("resolved_build"),
        duplicate_bug_id=params.get("duplicate_bug_id"),
        assignee_id=params.get("assignee_id"),
    )


# ── bug.history —— 活动流时间线（读）────────────────────────────
@register_method(
    "bug.history",
    kind="read",
    description="读单个缺陷的活动流（mc_test_bug_log，时间线）：每次动作/更新一行，"
                "按发生时刻升序。供详情页时间线与周期复算。",
    params_schema={
        "type": "object",
        "properties": {"bug_id": {"type": "string", "description": "缺陷 UUID"}},
        "required": ["bug_id"],
    },
    returns_schema={"type": "object", "properties": {"entries": {"type": "array"}, "count": {"type": "integer"}}},
)
async def bug_history(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.list_bug_log`。"""
    entries = await list_bug_log(params["bug_id"])
    return {"entries": entries, "count": len(entries)}


# ── bug.enums —— 全枚举 + 中文标签（读，单一真相）───────────────
@register_method(
    "bug.enums",
    kind="read",
    description="缺陷域全枚举 + 中文标签（单一真相）：状态机/解决方案/缺陷类型/严重度/"
                "优先级/发现阶段/OS/浏览器 + 动作守卫表（action→from_statuses/required/"
                "conditional）。前端与 Agent 据此自适应，不硬编码。",
    params_schema={"type": "object", "properties": {}},
    returns_schema={"type": "object", "properties": {"enums": {"type": "object"}, "actions": {"type": "object"}}},
)
async def bug_enums(params: Mapping[str, Any]) -> dict:
    """包 defects 枚举常量 + 动作守卫表（纯读，无 DB）。"""
    from ..defects.models import (
        BUG_FOUND_IN_LABELS,
        BUG_RESOLUTION_LABELS,
        BUG_SEVERITY_LABELS,
        BUG_STATUS_ALIASES,
        BUG_TYPE_LABELS,
        BUG_LONG_LIFE_DAYS,
        TERMINAL_NOT_EDITABLE,
    )

    actions = {}
    for name, spec in BUG_ACTIONS.items():
        to = spec.to_status
        actions[name] = {
            "label": spec.label,
            "from_statuses": sorted(spec.from_statuses),
            "to_status": "runtime" if callable(to) else to,
            "required": list(spec.required),
            "conditional": [
                {"field": cf, "equals": cv, "required": list(rf)}
                for cf, cv, rf in spec.conditional
            ],
            "clears": list(spec.clears),
            "bumps": spec.bumps,
        }
    return {
        "enums": {
            "statuses": list(BUG_STATUSES),
            "status_labels": BUG_STATUS_LABELS,
            "status_aliases": BUG_STATUS_ALIASES,
            "resolutions": list(BUG_RESOLUTIONS),
            "resolution_labels": BUG_RESOLUTION_LABELS,
            "bug_types": list(BUG_TYPES),
            "bug_type_labels": BUG_TYPE_LABELS,
            "severities": list(BUG_SEVERITIES),
            "severity_labels": BUG_SEVERITY_LABELS,
            "priorities": list(BUG_PRIORITIES),
            "found_ins": list(BUG_FOUND_INS),
            "found_in_labels": BUG_FOUND_IN_LABELS,
            "os_list": list(BUG_OS_LIST),
            "browser_list": list(BUG_BROWSER_LIST),
            "long_life_days": BUG_LONG_LIFE_DAYS,
            "terminal_not_editable": sorted(TERMINAL_NOT_EDITABLE),
        },
        "actions": actions,
    }


# ── bug.cycle_report —— 周期报表（读）───────────────────────────
@register_method(
    "bug.cycle_report",
    kind="read",
    description="缺陷周期报表（20 号文 §4.5）：MTTR 平均修复时长/平均验证时长/总生命周期/"
                "重开率/超期/长期未决/逃逸率 + 按 severity/type/resolution/status/module/"
                "assignee/found_in 的分布（对标禅道 report->charts）。"
                "可按 version_id / strategy_id 收窄。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "version_id": {"type": "string", "description": "按测试版本收窄"},
            "strategy_id": {"type": "string", "description": "按测试策略收窄"},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"total": {"type": "integer"}, "metrics": {"type": "object"}}},
)
async def bug_cycle_report(params: Mapping[str, Any]) -> dict:
    """取数（list_bugs）→ compute_cycle_report 纯函数（reports/cycle.py）。"""
    from ..reports import compute_cycle_report

    bugs = await list_bugs(
        params["project_id"],
        version_id=params.get("version_id"),
        strategy_id=params.get("strategy_id"),
    )
    return compute_cycle_report(bugs)


# ── bug.delete —— 真删（写，主表+根因+活动流）───────────────────
@register_method(
    "bug.delete",
    kind="write",
    description="真删缺陷（主表 + 根因行 + 活动流行）。不可恢复。",
    params_schema={
        "type": "object",
        "properties": {"bug_id": {"type": "string", "description": "缺陷 UUID"}},
        "required": ["bug_id"],
    },
    returns_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
)
async def bug_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.defects.delete_bug`。"""
    ok = await delete_bug(params["bug_id"])
    return {"ok": ok}


# ── workflow.start —— 开工作流运行（写）─────────────────────────
@register_method(
    "workflow.start",
    kind="write",
    description="开一条工作流运行（§15.9）：status=pending；workflow_def_id 可传 role 名（如 coder → 内置定义）。",
    params_schema={
        "type": "object",
        "properties": {
            "workflow_def_id": {
                "type": "string",
                "description": "定义 UUID 或角色名（planner/analyst/designer/coder/executor/debugger/reporter）",
            },
            "input": {"type": "object", "description": "角色输入（原样落 input JSONB；缺省 {}）"},
            "linked_run_id": {"type": "string", "description": "关联的引擎执行运行（mc_test_run.id）"},
            "agent_runtime": {"type": "string", "description": "由哪个 runtime 驱动（部署选择，可空）"},
        },
        "required": ["workflow_def_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def workflow_start(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.workflow.start_run`（懒导入，避免与 contract 成环）。"""
    from ..workflow import start_run

    run = await start_run(
        params["workflow_def_id"],
        params.get("input") or {},
        linked_run_id=params.get("linked_run_id"),
        agent_runtime=params.get("agent_runtime"),
    )
    return _workflow_run_document(run)


# ── workflow.complete —— 收口产出（写，过契约校验）──────────────
@register_method(
    "workflow.complete",
    kind="write",
    description="工作流产出收口：先过契约校验（确定性，不调 LLM）→ done；不过 → failed 且问题落 output.issues。",
    params_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "工作流运行 UUID"},
            "output": {"type": "object", "description": "角色产出（过该角色 output_schema 契约校验）"},
        },
        "required": ["run_id", "output"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def workflow_complete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.workflow.complete_run`（契约过 → done，不过 → failed）。"""
    from ..workflow import complete_run

    run = await complete_run(params["run_id"], params.get("output") or {})
    return _workflow_run_document(run)


# ── workflow.get —— 取工作流运行状态（读，前端轮询生成进度）─────────
@register_method(
    "workflow.get",
    kind="read",
    description="取一条工作流运行的状态与产出（前端轮询 AI 生成用例进度用）。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "工作流运行 UUID"}},
        "required": ["run_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def workflow_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.workflow.get_run`。"""
    from ..workflow import get_run

    run = await get_run(params["run_id"])
    return _workflow_run_document(run)


# ════════════════════════════════════════════════════════════════
# 补齐资产缺失域（design §15.4 / 01 §5.1/§6）：接口管理（transport）/
# 方法脚本（method_module/method）/ 数据集（dataset）/ 标签列取 —— 前端缺的方法
# ════════════════════════════════════════════════════════════════

# ── tag.list —— 列标签（读）─────────────────────────────────────
@register_method(
    "tag.list",
    kind="read",
    description="按项目列标签（可按 category 收窄），供前端选例维度与打标用（01 §1）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "category": {"type": "string", "enum": list(TAG_CATEGORIES)},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"tags": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def tag_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_tags`。"""
    rows = await list_tags(params["project_id"], category=params.get("category"))
    return {"tags": [_tag_document(row) for row in rows], "count": len(rows)}


# ── transport.* —— 接口管理（design §15.4：鉴权/公共数据/路径替换）──
@register_method(
    "transport.list",
    kind="read",
    description="按项目列传输层（接口管理，design §15.4；可按 kind/status 收窄）。"
                "**分页**：默认 50 条——几百接口时不全量 dump（17 号文断点 4）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "kind": {"type": "string", "enum": list(TRANSPORT_KINDS)},
            "status": {"type": "string", "enum": list(TRANSPORT_STATUSES)},
            "limit": {"type": "integer", "description": "返回上限，默认 50"},
            "offset": {"type": "integer", "description": "偏移，默认 0"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"transports": {"type": "array"}, "count": {"type": "integer"},
                       "total": {"type": "integer"}},
    },
)
async def transport_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_transports`（分页在方法面做）。"""
    from ..assets.models import TestTransport

    limit = int(params.get("limit", 50))
    offset = int(params.get("offset", 0))
    qs = TestTransport.filter(project_id=_uuid_of(params["project_id"], field_name="project_id"))
    if params.get("kind"):
        qs = qs.filter(kind=params["kind"])
    if params.get("status"):
        qs = qs.filter(status=params["status"])
    total = await qs.count()
    rows = await qs.order_by("code").limit(limit).offset(offset)
    return {"transports": [_transport_document(row) for row in rows],
            "count": len(rows), "total": total}


@register_method(
    "transport.create",
    kind="write",
    description="建传输层/接口（design §15.4：kind + auth 登录流程 + 公共头/参数 + path_rewrite）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "code": {"type": "string", "description": "稳定 ID（如 endpoint_get_orders）"},
            "kind": {"type": "string", "enum": list(TRANSPORT_KINDS)},
            "description": {"type": "string", "description": "接口说明（可空，AI 可补全）"},
            "module": {"type": "string", "description": "分组模块（思维导图/树）"},
            "auth": {"type": "object", "description": "登录流程 + re_login_on（凭据只存引用）"},
            "common_headers": {"type": "object"},
            "common_params": {"type": "object"},
            "path_rewrite": {"type": "object", "description": "路径替换 {原前缀: 新前缀}"},
            "status": {"type": "string", "enum": list(TRANSPORT_STATUSES)},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
        },
        "required": ["project_id", "code"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def transport_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_transport`。"""
    transport = await create_transport(
        project_id=params["project_id"],
        code=params["code"],
        kind=params.get("kind", "http"),
        description=params.get("description"),
        module=params.get("module"),
        auth=params.get("auth"),
        common_headers=params.get("common_headers"),
        common_params=params.get("common_params"),
        path_rewrite=params.get("path_rewrite"),
        status=params.get("status", "draft"),
        requirement_id=params.get("requirement_id"),
    )
    return _transport_document(transport)


@register_method(
    "transport.get",
    kind="read",
    description="取单个传输层（UUID 或 code；场景引用 transport_id 后回查）。",
    params_schema={
        "type": "object",
        "properties": {"transport_id": {"type": "string", "description": "UUID 主键或 code"}},
        "required": ["transport_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def transport_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.get_transport`。"""
    return _transport_document(await get_transport(params["transport_id"]))


@register_method(
    "transport.update",
    kind="write",
    description="改传输层：**必须**带 change_reason（变更门，design §15.12）。",
    params_schema={
        "type": "object",
        "properties": {
            "transport_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object"},
            "rollback_ref": {"description": "可回滚依据"},
            "code": {"type": "string"},
            "kind": {"type": "string", "enum": list(TRANSPORT_KINDS)},
            "description": {"type": "string", "description": "接口说明"},
            "module": {"type": "string", "description": "分组模块"},
            "auth": {"type": "object"},
            "common_headers": {"type": "object"},
            "common_params": {"type": "object"},
            "path_rewrite": {"type": "object"},
            "status": {"type": "string", "enum": list(TRANSPORT_STATUSES)},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
        },
        "required": ["transport_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def transport_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_transport`；缺 ``change_reason`` 即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "transport.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    values = {key: value for key, value in params.items() if key in TRANSPORT_EDITABLE_FIELDS}
    transport = await update_transport(
        params["transport_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **values,
    )
    return _transport_document(transport)


@register_method(
    "transport.delete",
    kind="write",
    description="删除传输层（接口管理；返回删除行数，0=不存在不报错）。",
    params_schema={
        "type": "object",
        "properties": {"transport_id": {"type": "string", "description": "UUID 主键或 code"}},
        "required": ["transport_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def transport_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_transport`。"""
    deleted = await delete_transport(params["transport_id"])
    return {"deleted": int(deleted)}


@register_method(
    "transport.import_openapi",
    kind="write",
    description=(
        "导入 OpenAPI 3.0 规格（swagger/apifox）：**每个 endpoint 生成一条 transport**"
        "（kind=http，auth 留空待填）；同 spec 重复导入 → 更新而非重建（幂等）。"
    ),
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "归属项目 UUID"},
            "spec": {
                "type": ["object", "string"],
                "description": "OpenAPI 规格：已解析的 dict，或 JSON/YAML 文本（粘贴 spec 字符串入口）",
            },
            "spec_url": {
                "type": "string",
                "description": "规格 URL（与 spec 二选一；当前仅透传，未实现远程抓取）",
            },
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "transports": {"type": "array"},
            "count": {"type": "integer"},
            "codes": {"type": "array"},
        },
    },
)
async def transport_import_openapi(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.importers.import_openapi`（spec 或 spec_url 两种入口）。"""
    from ..importers import OpenAPIImportError, fetch_spec_url, import_openapi

    spec = params.get("spec")
    if spec is None and params.get("spec_url"):
        spec = await fetch_spec_url(str(params["spec_url"]))
    if spec is None:
        raise OpenAPIImportError(
            "必须提供 spec（dict 或 JSON/YAML 字符串）或 spec_url（远程抓取）"
        )
    rows = await import_openapi(spec, params["project_id"])
    return {
        "transports": [_transport_document(row) for row in rows],
        "count": len(rows),
        "codes": [row.code for row in rows],
    }


# ── transport.import_from_repo —— 从仓库扫接口（读快照→路由+OpenAPI→接口）──
@register_method(
    "transport.import_from_repo",
    kind="execute",
    description="**从仓库导入接口**：拉代码快照（幂等）→ 扫 HTTP 路由（go/java/python/js/ts）"
                "+ 扫 OpenAPI 规格文件 → 幂等 upsert 成接口（mc_test_transport）。dry_run=true 只扫不落库。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "归属项目 UUID"},
            "git_id": {"type": "string", "description": "仓库配置 UUID（与 version_id 二选一）"},
            "version_id": {"type": "string", "description": "版本 UUID（按版本选仓库分支；与 git_id 二选一）"},
            "dry_run": {"type": "boolean", "description": "只扫描不落库（预览扫出多少接口）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "routes": {"type": "integer"},
            "openapi_endpoints": {"type": "integer"},
            "transports": {"type": "array"},
            "scanned": {"type": "array"},
        },
    },
)
async def transport_import_from_repo(params: Mapping[str, Any]) -> dict:
    """扫快照（路由 + OpenAPI 规格）→ 接口。"""
    from ..importers import import_from_snapshot
    from ..projects.git_source import init_snapshot as _init_snapshot
    from ..projects.service import get_version

    project_id = params["project_id"]
    version_id = params.get("version_id")
    git_id = params.get("git_id")

    snapshots: list[str] = []
    if version_id:
        version = await get_version(_uuid_of(version_id, field_name="version_id"))
        for repo in version.repos or []:
            snap = await _init_snapshot(
                repo.get("git_id"), ref_override=repo.get("ref"), version_id=str(version.id)
            )
            snapshots.append(snap["path"])
    elif git_id:
        snap = await _init_snapshot(git_id)
        snapshots.append(snap["path"])
    else:
        raise ValueError("必须提供 git_id 或 version_id")

    total_routes = 0
    total_openapi = 0
    total_spec_files = 0
    transports: list[Any] = []
    scanned: list[dict[str, Any]] = []
    for path in snapshots:
        res = await import_from_snapshot(path, project_id, dry_run=bool(params.get("dry_run")))
        total_routes += res["routes"]
        total_openapi += res["openapi_endpoints"]
        total_spec_files += res["openapi_files"]
        scanned.extend(res["scanned"])
        transports.extend(res["transports"])

    # De-dupe across repos by code.
    by_code = {row.code: row for row in transports}
    return {
        "routes": total_routes,
        "openapi_files": total_spec_files,
        "openapi_endpoints": total_openapi,
        "transports": [_transport_document(row) for row in by_code.values()],
        "count": len(by_code),
        "dry_run": bool(params.get("dry_run")),
        "scanned": scanned,
    }


# ── transport.generate_from_repo —— AI 读仓库生成接口（execute，异步）────
@register_method(
    "transport.generate_from_repo",
    kind="execute",
    description="**AI 读仓库生成接口**：拉代码快照（幂等）→ 把 Agent 的工作目录设为快照 → "
                "驱动 Agent 读源码理解对外 HTTP 接口，经 MCP 调 transport.create/update 落库。"
                "**异步长任务**：立即返回 run_id，接口由 Agent 后台产出（轮询 workflow/run 状态）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "归属项目 UUID"},
            "version_id": {"type": "string", "description": "版本 UUID（按版本选仓库分支；与 git_id 二选一）"},
            "git_id": {"type": "string", "description": "仓库配置 UUID（与 version_id 二选一）"},
            "created_by": {"type": "string", "description": "发起者 UUID"},
            "api_key_id": {"type": "integer", "description": "平台 API Key id（生成用；缺省按项目创建者解析）"},
            "model": {"type": "string", "description": "生成模型（缺省 TESTING_GEN_MODEL）"},
            "instruction": {"type": "string", "description": "额外要求（自然语言，可空）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "status": {"type": "string"},
            "repos": {"type": "array"},
        },
    },
)
async def transport_generate_from_repo(params: Mapping[str, Any]) -> dict:
    """AI 读仓库生成接口（**全部重活进后台**，请求只做预检 + 开 run）。

    预检（同步、快）：版本有仓库、密钥可解析——任一失败**当场报错**（前端置灰/提示），
    不再让用户等几分钟快照后才收到一个被吞掉的异常。
    后台：逐仓 init_snapshot → build_generation_config(cwd=快照) → run_chat → 落库。
    **任何异常都 fail_run 留档**——run 永远不会卡在 pending，前端轮询拿得到原因。
    """
    import asyncio

    from ..agent_runtime import build_generation_config, run_chat
    from ..prompts import load_prompt
    from ..workflow import complete_run, fail_run, start_run

    project_id = params["project_id"]
    version_id = params.get("version_id")
    git_id = params.get("git_id")

    # ── 预检 ①：密钥（没 key 不开 run，立刻报错）──
    gen_key: Optional[str] = None
    if params.get("api_key_id"):
        from .chat import _resolve_api_key

        gen_key, _n = await _resolve_api_key(
            int(params["api_key_id"]), str(params.get("created_by") or "")
        )
    # 试构建（不落盘）；缺密钥抛 GenerationConfigError —— 前端据此置灰/提示配置不全
    build_generation_config(api_key=gen_key, model=params.get("model"))

    # ── 预检 ②：目标仓库存在（至少能定位到 git 配置）──
    if version_id:
        version = await get_version(_uuid_of(version_id, field_name="version_id"))
        if not (version.repos or []):
            raise ValueError("该版本没有配仓库，无法生成接口")
    elif not git_id:
        raise ValueError("必须提供 git_id 或 version_id")

    # ── 开 run（秒回；快照/Agent 全在后台）──
    run = await start_run(
        "analyst",
        {
            "project_id": project_id,
            "version_id": version_id,
            "git_id": git_id,
            "instruction": params.get("instruction"),
        },
        agent_runtime="dsh",
    )

    run_id = str(run.id)
    methodology = load_prompt("interface_generation")
    instruction = params.get("instruction")
    model = params.get("model")

    async def _drive() -> None:
        repo_reports: list[dict[str, Any]] = []
        try:
            # 拉快照（后台做，几分钟也不阻塞 HTTP 请求）
            snapshots: list[dict[str, Any]] = []
            if version_id:
                ver = await get_version(_uuid_of(version_id, field_name="version_id"))
                for repo in ver.repos or []:
                    snap = await init_snapshot(
                        repo.get("git_id"), ref_override=repo.get("ref"), version_id=str(ver.id)
                    )
                    snapshots.append(snap)
            else:
                snapshots.append(await init_snapshot(git_id))

            # 已有接口（幂等参考）
            existing: list[dict[str, Any]] = []
            try:
                for t in await list_transports(project_id):
                    existing.append({"code": t.code, "description": t.description, "module": t.module})
            except Exception:  # noqa: BLE001
                existing = []

            for snap in snapshots:
                # 每仓一个 Agent 回合：cwd = 该仓快照目录（Agent 直接读代码）
                config = build_generation_config(api_key=gen_key, model=model, cwd=snap["path"])
                prompt = _build_interface_prompt(
                    methodology=methodology,
                    project_id=project_id,
                    repo_path=snap["path"],
                    ref=snap.get("ref"),
                    commit=snap.get("commit"),
                    existing=existing,
                    instruction=instruction,
                )
                result = await run_chat(prompt=prompt, config=config)
                repo_reports.append({
                    "path": snap["path"], "ref": snap.get("ref"),
                    "events": len(result.get("events") or []),
                })
            await complete_run(
                run_id,
                {"project_id": project_id, "repos": repo_reports, "note": "agent-driven"},
            )
        except Exception as exc:  # noqa: BLE001 — 失败**必须**落 run，不静默
            logger.exception("[transport.generate_from_repo] agent run failed")
            await fail_run(run_id, exc)

    asyncio.create_task(_drive())
    return {
        "run_id": run_id,
        "status": run.status,
        "repos": [],
    }


def _build_interface_prompt(
    *,
    methodology: str,
    project_id: str,
    repo_path: str,
    ref: Optional[str],
    commit: Optional[str],
    existing: Sequence[Mapping[str, Any]],
    instruction: Optional[str],
) -> str:
    """把方法论 + 仓库上下文拼成 Agent 的生成提示词。"""
    parts = [
        methodology,
        "",
        "---",
        "## 本次任务",
        f"project_id = {project_id}",
        f"工作目录（仓库快照） = {repo_path}",
    ]
    if ref:
        parts.append(f"分支/ref = {ref}")
    if commit:
        parts.append(f"commit = {commit}")
    if existing:
        sample = ", ".join(str(e.get("code")) for e in existing[:40])
        parts.append(f"已有接口 {len(existing)} 条（code 示例：{sample}）——重复的用 transport.update 而非新建")
    if instruction:
        parts.append(f"额外要求：{instruction}")
    parts.append(
        "\n请先读仓库源码找出对外 HTTP 接口，再用 transport.create（已存在的用 transport.update）"
        "逐条落库；code 用 endpoint_<method>_<path 去斜杠> 保证幂等。"
    )
    return "\n".join(parts)


# ── method_module.* / method.* —— 方法库（§5.1 重构：一个脚本 = 一个类）────
@register_method(
    "method_module.create",
    kind="write",
    description="建方法类（§5.1：一个脚本=一个 Python 类，source 必含恰好一个类；"
    "code/名称取类名、备注取类 docstring，保存时 AST 解析静态方法自动登记）。",
    params_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "类名标识；给了 source 时可省略（取源码里的类名）"},
            "project_id": {"type": "string", "description": "项目 UUID；缺省为全局内置类"},
            "source": {"type": "string", "description": "Python 源码（含且仅含一个 class，方法全 @staticmethod）"},
            "async_native": {"type": "boolean", "description": "是否异步原生（§5.2）"},
            "status": {
                "type": "string",
                "enum": ["draft", "verified", "active", "deprecated", "archived"],
            },
        },
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def method_module_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_method_module`（source 走 AST 校验与同步）。"""
    module = await create_method_module(
        code=params.get("code", ""),
        project_id=params.get("project_id"),
        source=params.get("source", ""),
        async_native=params.get("async_native", False),
        status=params.get("status", "draft"),
    )
    return _method_module_document(module)


@register_method(
    "method_module.update",
    kind="write",
    description="改方法类（§5.1 / design §15.12 变更门：change_reason 必填）。"
    "带 source 即重跑 AST：同步类名/类备注并重建静态方法注册表（full_ref/入参说明随之更新）。",
    params_schema={
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "类 UUID 或 code（=类名）"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "source": {"type": "string", "description": "新 Python 源码（一个类，全静态方法）"},
            "status": {
                "type": "string",
                "enum": ["draft", "verified", "active", "deprecated", "archived"],
            },
            "async_native": {"type": "boolean"},
            "created_by": {"type": "string", "description": "变更人 UUID（变更历史留痕）"},
        },
        "required": ["module_id", "change_reason"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def method_module_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_method_module`（变更门 + AST 重同步）。"""
    module = await update_method_module(
        params["module_id"],
        change_reason=params["change_reason"],
        source=params.get("source"),
        created_by=params.get("created_by"),
        **{k: v for k, v in params.items() if k in {"status", "async_native"}},
    )
    return _method_module_document(module)


@register_method(
    "method_module.changes",
    kind="read",
    description="方法类的变更历史（design §15.12）：每条含变更人 / 时间 / 变更理由 / "
    "字段级前后 diff（``field_changes``，源码变更即 ``source.from`` / ``source.to`` 全量）。",
    params_schema={
        "type": "object",
        "properties": {"module_id": {"type": "string", "description": "类 UUID 或 code"}},
        "required": ["module_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"changes": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def method_module_changes(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_changes`（entity_type=method_module）。"""
    module = await get_method_module(params["module_id"])
    rows = await list_changes("method_module", module.id)
    return {"changes": [_change_document(r) for r in rows], "count": len(rows)}


@register_method(
    "method_module.delete",
    kind="write",
    description="删方法类（级联删其方法注册行，§5.1）。",
    params_schema={
        "type": "object",
        "properties": {"module_id": {"type": "string", "description": "类 UUID 或 code"}},
        "required": ["module_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "boolean"}}},
)
async def method_module_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_method_module`。"""
    await delete_method_module(params["module_id"])
    return {"deleted": True}


@register_method(
    "method_module.get",
    kind="read",
    description="取单个方法类（含源码 + 展开静态方法清单：full_ref/说明/入参说明，§5.1）。",
    params_schema={
        "type": "object",
        "properties": {"module_id": {"type": "string", "description": "类 UUID 或 code"}},
        "required": ["module_id"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def method_module_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.get_method_module` + 方法展开。"""
    module = await get_method_module(params["module_id"])
    methods = await list_class_methods(module.id)
    return {"item": _method_module_document(module, methods=methods)}


@register_method(
    "method_module.list",
    kind="read",
    description="列方法类（给 project_id 则含全局内置类 + 本项目类，§5.1；不再分 base/project）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID；缺省只列全局类"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"modules": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def method_module_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_method_modules`。"""
    rows = await list_method_modules(params.get("project_id"))
    return {"modules": [_method_module_document(row) for row in rows], "count": len(rows)}


@register_method(
    "method_module.classes",
    kind="read",
    description="**方法类目录**（对外检索查看）：每项含 类名/备注/code/状态 + 展开静态方法清单"
    "（full_ref/方法说明/入参说明 signature），一次查齐（§5.1）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID；缺省只列全局类"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"classes": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def method_module_classes(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_method_classes`。"""
    rows = await list_method_classes(params.get("project_id"))
    return {"classes": rows, "count": len(rows)}


@register_method(
    "method_module.parse",
    kind="read",
    description="预览源码解析结果（不落库）：类名/类备注/静态方法清单含入参说明——编辑器「保存前预检」用。",
    params_schema={
        "type": "object",
        "properties": {"source": {"type": "string", "description": "Python 源码（一个类）"}},
        "required": ["source"],
    },
    returns_schema={"type": "object", "properties": {"item": {"type": "object"}}},
)
async def method_module_parse(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.method_source.parse_class_source`（只读预览）。"""
    from ..method_source import parse_class_source

    parsed = parse_class_source(params["source"])
    return {
        "item": {
            "class_name": parsed.name,
            "docstring": parsed.docstring,
            "methods": [
                {
                    "name": m.name,
                    "is_async": m.is_async,
                    "docstring": m.docstring,
                    "signature": m.signature,
                }
                for m in parsed.methods
            ],
        }
    }


@register_method(
    "method.create",
    kind="write",
    description="在类内手工登记方法（一般用不到：保存源码即 AST 自动登记；full_ref 缺省 <类名>.<name>）。",
    params_schema={
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "类 UUID 或 code（=类名）"},
            "name": {"type": "string", "description": "方法名，如 sign"},
            "full_ref": {"type": "string", "description": "缺省 <类名>.<name>"},
            "signature": {"type": "object", "description": "入参说明 {params:[...], returns:{...}}（JSONB）"},
            "is_async": {"type": "boolean", "description": "同步/异步（§5.2）"},
            "description": {"type": "string"},
        },
        "required": ["module_id", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "full_ref": {"type": "string"}}},
)
async def method_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_method`。"""
    method = await create_method(
        module_id=params["module_id"],
        name=params["name"],
        signature=params.get("signature"),
        full_ref=params.get("full_ref"),
        is_async=params.get("is_async", False),
        description=params.get("description"),
    )
    return _method_document(method)


@register_method(
    "method.list",
    kind="read",
    description="列方法（可按 module_id=类收窄；full_ref 升序；带 说明+入参说明，供场景 method_ref 下拉/检索，§5.1）。",
    params_schema={
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "类 UUID 或 code；缺省列全部"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"methods": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def method_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_class_methods`（原 ``list_methods``，
    在本模块被方法注册表投影 :func:`list_methods` 遮蔽，故用新名）。"""
    rows = await list_class_methods(params.get("module_id"))
    return {"methods": [_method_document(row) for row in rows], "count": len(rows)}


# ── dataset.* —— 数据集（01 §6 / design §15.4）───────────────────
@register_method(
    "dataset.list",
    kind="read",
    description="按项目列数据集（code 升序，§6 / design §15.4）。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string", "description": "项目 UUID"}},
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"datasets": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def dataset_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_datasets`。"""
    rows = await list_datasets(params["project_id"])
    return {"datasets": [_dataset_document(row) for row in rows], "count": len(rows)}


@register_method(
    "dataset.create",
    kind="write",
    description="建数据集（§6：schema 定义字段类型，rows 是多组数据，供用例 {{data.*}} 参数化）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "code": {"type": "string"},
            "schema": {"type": "object", "description": "字段类型定义 {product_id: string, ...}"},
            "rows": {"type": "array", "description": "多组数据 [{...}, {...}]"},
        },
        "required": ["project_id", "code"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def dataset_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_dataset`。"""
    dataset = await create_dataset(
        project_id=params["project_id"],
        code=params["code"],
        schema=params.get("schema"),
        rows=params.get("rows"),
    )
    return _dataset_document(dataset)


@register_method(
    "dataset.update",
    kind="write",
    description="改数据集：**必须**带 change_reason（变更门，design §15.12）。",
    params_schema={
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "UUID 主键或 code"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object"},
            "rollback_ref": {"description": "可回滚依据"},
            "code": {"type": "string"},
            "schema": {"type": "object"},
            "rows": {"type": "array"},
        },
        "required": ["dataset_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def dataset_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_dataset`；缺 ``change_reason`` 即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "dataset.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    values = {key: value for key, value in params.items() if key in DATASET_EDITABLE_FIELDS}
    dataset = await update_dataset(
        params["dataset_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **values,
    )
    return _dataset_document(dataset)


@register_method(
    "dataset.delete",
    kind="write",
    description="删数据集（返回删除行数，0=不存在不报错）。挂到它的用例 dataset_id 置空（用例本体不删）。",
    params_schema={
        "type": "object",
        "properties": {"dataset_id": {"type": "string", "description": "UUID 主键或 code"}},
        "required": ["dataset_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def dataset_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_dataset`。"""
    deleted = await delete_dataset(params["dataset_id"])
    return {"deleted": int(deleted)}


# ════════════════════════════════════════════════════════════════
# 环境管理（design §15.4 / 05：分层继承 + 7 类型变量 + 密钥脱敏）
# ════════════════════════════════════════════════════════════════
def _env_document(env: Any) -> dict:
    """环境行 → JSON 文档（``mc_test_environment``，05 §2.1）。"""
    return {
        "id": str(env.id),
        "code": env.code,
        "name": env.name,
        "project_id": str(env.project_id),
        "parent_id": str(env.parent_id) if env.parent_id else None,
        "description": env.description,
        "is_default": bool(env.is_default),
        "status": env.status,
        "version": env.version,
        "created_by": str(env.created_by) if env.created_by else None,
        "created_at": _iso(env.created_at),
        "updated_at": _iso(env.updated_at),
    }


def _env_var_document(var: Any) -> dict:
    """环境变量行 → JSON 文档（``mc_test_env_variable``，05 §2.2）。

    密钥变量值由 service 层 :func:`list_env_variables` 已脱敏；
    这里只展平结构。
    """
    return {
        "id": str(var.id),
        "env_id": str(var.env_id),
        "key": var.key,
        "value": var.value,
        "type": var.type,
        "purpose": var.purpose,
        "is_secret": bool(var.is_secret),
        "version": var.version,
        "updated_at": _iso(var.updated_at),
    }


# ── env.list —— 列环境（读）─────────────────────────────────────
@register_method(
    "env.list",
    kind="read",
    description="按项目列测试环境（design §15.4 / 05：分层继承；可按 status 收窄），code 升序。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "status": {"type": "string", "enum": list(ENVIRONMENT_STATUSES)},
            "include_archived": {
                "type": "boolean",
                "description": "默认只列 active；true 则含 archived（05 §6：归档不删）",
            },
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"environments": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def env_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_environments`。"""
    rows = await list_environments(
        params["project_id"],
        status=params.get("status"),
        include_archived=bool(params.get("include_archived", False)),
    )
    return {"environments": [_env_document(row) for row in rows], "count": len(rows)}


# ── env.create —— 建环境（写，分层继承）─────────────────────────
@register_method(
    "env.create",
    kind="write",
    description="建测试环境（05 §2.1）：parent_id 空=根基线，非空=子环境（分层继承）。is_default 自动互斥。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "code": {"type": "string", "description": "稳定 ID，如 dev / staging / prod_sim"},
            "name": {"type": "string"},
            "parent_id": {"type": "string", "description": "父环境 UUID 或 code（null=根基线）"},
            "description": {"type": "string"},
            "is_default": {"type": "boolean", "description": "是否项目默认（自动清掉同项目其它默认）"},
            "status": {"type": "string", "enum": list(ENVIRONMENT_STATUSES)},
            "created_by": {"type": "string", "description": "创建者 UUID（人为管理）"},
        },
        "required": ["project_id", "code", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def env_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_environment`。"""
    env = await create_environment(
        project_id=params["project_id"],
        code=params["code"],
        name=params["name"],
        parent_id=params.get("parent_id"),
        description=params.get("description"),
        is_default=bool(params.get("is_default", False)),
        status=params.get("status", "active"),
        created_by=params.get("created_by"),
    )
    return _env_document(env)


# ── env.update —— 改环境（写，**变更门**强制）────────────────────
@register_method(
    "env.update",
    kind="write",
    description="改测试环境：**必须**带 change_reason（design §15.12 变更门）；parent_id 变更会做防循环校验。",
    params_schema={
        "type": "object",
        "properties": {
            "env_id": {"type": "string", "description": "UUID 主键或 code"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object"},
            "rollback_ref": {"description": "可回滚依据"},
            "code": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "parent_id": {"type": "string", "description": "新父环境（变更后做防循环校验）"},
            "is_default": {"type": "boolean"},
            "status": {"type": "string", "enum": list(ENVIRONMENT_STATUSES)},
        },
        "required": ["env_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def env_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_environment`；缺 ``change_reason`` 即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "env.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    env = await get_environment(params["env_id"])
    env_id = env.id
    await update_environment(
        env_id,
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **{key: value for key, value in params.items() if key not in {
            "env_id", "change_reason", "propagation", "rollback_ref"
        }},
    )
    return _env_document(await get_environment(env_id))


# ── env.delete —— 删环境（写）───────────────────────────────────
@register_method(
    "env.delete",
    kind="write",
    description="删除测试环境（仍作父级时拒绝；变量级联删；返回删除行数，0=不存在）。",
    params_schema={
        "type": "object",
        "properties": {"env_id": {"type": "string", "description": "UUID 主键或 code"}},
        "required": ["env_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def env_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_environment`。"""
    deleted = await delete_environment(params["env_id"])
    return {"deleted": int(deleted)}


# ── env.var.create —— 建环境变量（写，7 类型 + 密钥脱敏）─────────
@register_method(
    "env.var.create",
    kind="write",
    description="给环境建变量（05 §2.2）：type 7 类型之一（string/int/float/boolean/json/list/dict），is_secret 走脱敏，purpose 记用途说明。",
    params_schema={
        "type": "object",
        "properties": {
            "env_id": {"type": "string", "description": "UUID 主键或 code"},
            "key": {"type": "string", "description": "变量名（环境内唯一）"},
            "value": {"description": "变量值（保持原生类型，05 §4）"},
            "type": {"type": "string", "enum": list(ENV_VARIABLE_TYPES)},
            "purpose": {"type": "string", "description": "用途说明（可选，说明这个变量指向什么/单位是什么）"},
            "is_secret": {"type": "boolean", "description": "密钥变量（读取脱敏、执行注入）"},
        },
        "required": ["env_id", "key"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "key": {"type": "string"}}},
)
async def env_var_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_env_variable`。"""
    var = await create_env_variable(
        env_id=params["env_id"],
        key=params["key"],
        value=params.get("value"),
        type=params.get("type", "string"),
        is_secret=bool(params.get("is_secret", False)),
        purpose=params.get("purpose"),
    )
    # 回读以拿真值（service create 返回的实例 value 已是原生）
    return _env_var_document(var)


# ── env.var.update —— 改环境变量（写）────────────────────────────
@register_method(
    "env.var.update",
    kind="write",
    description="改环境变量（key/value/type/is_secret/purpose）；version 递增。",
    params_schema={
        "type": "object",
        "properties": {
            "var_id": {"type": "string", "description": "变量 UUID"},
            "change_reason": {"type": "string", "description": "变更理由（可选，落变更记录）"},
            "key": {"type": "string"},
            "value": {"description": "新值（按 type 校验）"},
            "type": {"type": "string", "enum": list(ENV_VARIABLE_TYPES)},
            "purpose": {"type": "string", "description": "用途说明（可选）"},
            "is_secret": {"type": "boolean"},
        },
        "required": ["var_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def env_var_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_env_variable`。"""
    var = await update_env_variable(
        params["var_id"],
        change_reason=params.get("change_reason"),
        **{key: value for key, value in params.items() if key in {
            "key", "value", "type", "is_secret", "purpose"
        }},
    )
    return _env_var_document(var)


# ── env.var.delete —— 删环境变量（写）───────────────────────────
@register_method(
    "env.var.delete",
    kind="write",
    description="删除环境变量，返回删除行数（0=不存在）。",
    params_schema={
        "type": "object",
        "properties": {"var_id": {"type": "string", "description": "变量 UUID"}},
        "required": ["var_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def env_var_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_env_variable`。"""
    deleted = await delete_env_variable(params["var_id"])
    return {"deleted": int(deleted)}


# ── env.var.list —— 列环境变量（读，密钥脱敏）────────────────────
@register_method(
    "env.var.list",
    kind="read",
    description="列某环境的变量（05 §2.2/§5）：key 升序；is_secret 值脱敏为 ***+末4位。",
    params_schema={
        "type": "object",
        "properties": {"env_id": {"type": "string", "description": "UUID 主键或 code"}},
        "required": ["env_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"variables": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def env_var_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_env_variables`（默认脱敏，05 §5）。"""
    rows = await list_env_variables(params["env_id"], mask_secrets=True)
    return {"variables": [_env_var_document(row) for row in rows], "count": len(rows)}


# ── env.vars.resolve —— 继承合并读（读，核心功能）────────────────
@register_method(
    "env.vars.resolve",
    kind="read",
    description="**递归继承合并**（05 §3 / 06 §2.3）：父先子后，子覆盖父。返回 {key: 值}（原生类型；密钥脱敏）。",
    params_schema={
        "type": "object",
        "properties": {
            "env_id": {"type": "string", "description": "UUID 主键或 code（从该环境向上合并）"},
            "mask_secrets": {
                "type": "boolean",
                "description": "密钥变量是否脱敏（默认 true，05 §5）",
            },
        },
        "required": ["env_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"vars": {"type": "object"}, "chain": {"type": "array"}},
    },
)
async def env_vars_resolve(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.resolve_environment_vars` + 链文档。"""
    mask = bool(params.get("mask_secrets", True))
    vars_dict = await resolve_environment_vars(params["env_id"], mask_secrets=mask)
    chain = await get_env_chain_documents(params["env_id"])
    return {"vars": vars_dict, "chain": chain}


# ════════════════════════════════════════════════════════════════
# 疑问域（design §15.2：流程一等公民，未解决阻塞下游）
# ════════════════════════════════════════════════════════════════
def _question_document(q: Any) -> dict:
    """疑问行 → JSON 文档（``mc_test_question``，§15.2）。"""
    return {
        "id": str(q.id),
        "project_id": str(q.project_id),
        "origin": q.origin,
        "raised_by": q.raised_by,
        "content": q.content,
        "context": dict(q.context or {}),
        "status": q.status,
        "resolution": q.resolution,
        "resolution_detail": dict(q.resolution_detail or {}),
        "resolved_at": _iso(q.resolved_at),
        "blocking_refs": list(q.blocking_refs or []),
        "created_at": _iso(q.created_at),
        "updated_at": _iso(q.updated_at),
    }


# ── question.raise —— 提出疑问（写，阻塞下游）───────────────────
@register_method(
    "question.raise",
    kind="write",
    description="提出疑问（design §15.2：未解决疑问阻塞下游）。origin 必填；blocking_refs 是被阻塞的用例/场景 id。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "origin": {"type": "string", "enum": list(QUESTION_ORIGINS)},
            "content": {"type": "string", "description": "疑问正文（这条疑问到底在问什么）"},
            "raised_by": {"type": "string", "description": "提出角色（analyst/designer/...）"},
            "context": {"type": "object", "description": "发现位置 + 关联实体"},
            "blocking_refs": {
                "type": "array",
                "description": "被阻塞的下游实体 id（用例/场景），未解决期间不得进下游",
            },
        },
        "required": ["project_id", "origin", "content"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def question_raise(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.quality.raise_question`。"""
    q = await question_raise_service(
        project_id=params["project_id"],
        origin=params["origin"],
        content=params["content"],
        context=params.get("context"),
        blocking_refs=params.get("blocking_refs"),
        raised_by=params.get("raised_by"),
    )
    return _question_document(q)


# ── question.resolve —— 收敛疑问（写，解除阻塞）─────────────────
@register_method(
    "question.resolve",
    kind="write",
    description="收敛疑问（design §15.2）：resolution 必填（debug/doc/user_confirm）+ detail 依据；状态 → resolved。",
    params_schema={
        "type": "object",
        "properties": {
            "question_id": {"type": "string"},
            "resolution": {"type": "string", "enum": list(QUESTION_RESOLUTIONS)},
            "resolution_detail": {"type": "object", "description": "收敛依据（调试记录/文档/用户确认）"},
        },
        "required": ["question_id", "resolution"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def question_resolve(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.quality.resolve_question`；已 resolved 再 resolve 即报错。"""
    q = await question_resolve_service(
        params["question_id"],
        resolution=params["resolution"],
        resolution_detail=params.get("resolution_detail"),
    )
    return _question_document(q)


# ── question.list —— 列疑问（读）────────────────────────────────
@register_method(
    "question.list",
    kind="read",
    description="按项目列疑问（可按 status 收窄），created_at 倒序；前端疑问 Tab 与用例⚠️标取数用。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "status": {"type": "string", "enum": list(QUESTION_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"questions": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def question_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.quality.list_questions`。"""
    rows = await list_questions(params["project_id"], status=params.get("status"))
    return {"questions": [_question_document(row) for row in rows], "count": len(rows)}


# ════════════════════════════════════════════════════════════════
# 自定义工具（design §15.5：只拥有签名 + 指向，不拥有实现）
# ════════════════════════════════════════════════════════════════
# :func:`tool.list` 已注册（引擎工具只读投影）；此处补 custom_tool.*

# ── custom_tool.list —— 列自定义工具（读）────────────────────────
@register_method(
    "custom_tool.list",
    kind="read",
    description="按项目列自定义工具（design §15.5：scope=project/team，可收窄），code 升序。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "scope": {"type": "string", "enum": list(CUSTOM_TOOL_SCOPES)},
            "status": {"type": "string", "enum": list(CUSTOM_TOOL_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"tools": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def custom_tool_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_custom_tools`。"""
    rows = await list_custom_tools(
        params["project_id"],
        scope=params.get("scope"),
        status=params.get("status"),
    )
    from ..assets.service import _custom_tool_document

    return {"tools": [_custom_tool_document(row) for row in rows], "count": len(rows)}


# ── custom_tool.create —— 建自定义工具（写）──────────────────────
@register_method(
    "custom_tool.create",
    kind="write",
    description="建自定义工具（design §15.5）：impl_ref 必须指向 scenario 或 method.full_ref（不拥有实现）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "code": {"type": "string", "description": "稳定 ID"},
            "scope": {"type": "string", "enum": list(CUSTOM_TOOL_SCOPES)},
            "purpose": {"type": "string", "description": "业务语义（这个工具解决什么问题）"},
            "signature": {"type": "object", "description": "输入输出契约（JSONB）"},
            "impl_ref": {
                "type": "object",
                "description": "实现指向：{kind:scenario,id} 或 {kind:method,full_ref}",
            },
            "status": {"type": "string", "enum": list(CUSTOM_TOOL_STATUSES)},
        },
        "required": ["project_id", "code", "impl_ref"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def custom_tool_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_custom_tool`。"""
    from ..assets.service import _custom_tool_document

    tool = await create_custom_tool(
        project_id=params["project_id"],
        code=params["code"],
        scope=params.get("scope", "project"),
        purpose=params.get("purpose"),
        signature=params.get("signature"),
        impl_ref=params.get("impl_ref"),
        status=params.get("status", "draft"),
    )
    return _custom_tool_document(tool)


# ── custom_tool.update —— 改自定义工具（写，变更门 + 版本快照）────
@register_method(
    "custom_tool.update",
    kind="write",
    description="改自定义工具：**必须**带 change_reason（变更门）；同时落一行版本快照（mc_test_custom_tool_version）。",
    params_schema={
        "type": "object",
        "properties": {
            "tool_id": {"type": "string", "description": "UUID 主键或 code"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "propagation": {"type": "object"},
            "rollback_ref": {"description": "可回滚依据"},
            "code": {"type": "string"},
            "scope": {"type": "string", "enum": list(CUSTOM_TOOL_SCOPES)},
            "purpose": {"type": "string"},
            "signature": {"type": "object"},
            "impl_ref": {"type": "object", "description": "新实现指向（scenario 或 method）"},
            "status": {"type": "string", "enum": list(CUSTOM_TOOL_STATUSES)},
        },
        "required": ["tool_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def custom_tool_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_custom_tool`；缺 ``change_reason`` 即变更门拦截。"""
    from ..assets.service import _custom_tool_document

    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "custom_tool.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    tool = await update_custom_tool(
        params["tool_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        **{key: value for key, value in params.items() if key not in {
            "tool_id", "change_reason", "propagation", "rollback_ref"
        }},
    )
    return _custom_tool_document(tool)


# ════════════════════════════════════════════════════════════════
# 沉淀域（design §15.6：约定/模板/样例/校验器，按需加载）
# ════════════════════════════════════════════════════════════════
def _craft_document(asset: Any) -> dict:
    """沉淀资产行 → JSON 文档（``mc_test_craft_asset``，§15.6）。"""
    return {
        "id": str(asset.id),
        "type": asset.type,
        "scope": asset.scope,
        "target": asset.target,
        "content": asset.content,
        "version": asset.version,
        "status": asset.status,
        "created_at": _iso(asset.created_at),
        "updated_at": _iso(asset.updated_at),
    }


# ── craft.list —— 列沉淀资产（读）───────────────────────────────
@register_method(
    "craft.list",
    kind="read",
    description="列沉淀资产（design §15.6：四件套 convention/template/sample/validator × scope × target）。",
    params_schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(CRAFT_TYPES)},
            "scope": {"type": "string", "enum": list(CRAFT_SCOPES)},
            "target": {"type": "string", "enum": list(CRAFT_TARGETS)},
            "status": {"type": "string"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"assets": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def craft_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.quality.list_craft`。"""
    rows = await list_craft(
        type=params.get("type"),
        scope=params.get("scope"),
        target=params.get("target"),
        status=params.get("status"),
    )
    return {"assets": [_craft_document(row) for row in rows], "count": len(rows)}


# ── craft.create —— 建沉淀资产（写）──────────────────────────────
@register_method(
    "craft.create",
    kind="write",
    description="建沉淀资产（design §15.6）：type/target 必填；content 结构化或文本；scope 决定加载层。",
    params_schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(CRAFT_TYPES)},
            "target": {"type": "string", "enum": list(CRAFT_TARGETS)},
            "scope": {"type": "string", "enum": list(CRAFT_SCOPES)},
            "content": {"description": "资产内容（结构化对象或文本，JSONB）"},
            "status": {"type": "string"},
        },
        "required": ["type", "target"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "type": {"type": "string"}}},
)
async def craft_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.quality.create_craft`。"""
    asset = await create_craft(
        type=params["type"],
        target=params["target"],
        scope=params.get("scope", "project"),
        content=params.get("content"),
        status=params.get("status", "active"),
    )
    return _craft_document(asset)


# ── craft.load —— 按需加载（读，按 type+target）─────────────────
@register_method(
    "craft.load",
    kind="read",
    description="**按需加载**沉淀资产（design §15.6）：按 type+target（+scope）取 active 资产，common→framework→project 排序。",
    params_schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(CRAFT_TYPES)},
            "target": {"type": "string", "enum": list(CRAFT_TARGETS)},
            "scope": {"type": "string", "enum": list(CRAFT_SCOPES)},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"assets": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def craft_load(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.quality.load_craft`（只返 active）。"""
    rows = await load_craft(
        type=params.get("type"),
        target=params.get("target"),
        scope=params.get("scope"),
    )
    return {"assets": [_craft_document(row) for row in rows], "count": len(rows)}


# ════════════════════════════════════════════════════════════════
# 阶段 3/4/5：补注册工作流断点 + 分析师编排 + 版本捕获 + 可靠性画像
# ════════════════════════════════════════════════════════════════

# ── scenario.generate —— coder 落地场景（写，过校验门）────────────
@register_method(
    "scenario.generate",
    kind="write",
    description="coder 角色落地：agent 产出场景 → **先过 script.validate 校验门**（确定性）→ 通过才建场景+步骤。"
                "校验不过 → 返回 issues 不落库（AI 不裁判，框架裁判）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "scenario": {
                "type": "object",
                "description": "场景文档：{name, purpose?, exec_mode?, local_vars?, flow?, steps:[...]}",
            },
            "created_by": {"type": "string", "description": "创建者 UUID"},
        },
        "required": ["project_id", "scenario", "created_by"],
    },
    returns_schema={
        "type": "object",
        "properties": {"valid": {"type": "boolean"}, "id": {"type": "string"}, "issues": {"type": "array"}},
    },
)
async def scenario_generate(params: Mapping[str, Any]) -> dict:
    """agent 场景文档 → ``script.validate``（工具白名单 = 已注册方法 + 引擎工具）→ 落库。"""
    from ..assets import create_scenario, create_steps

    scenario_doc = dict(params["scenario"] or {})
    steps = list(scenario_doc.get("steps") or [])

    # 校验门：整份场景文档跑确定性规则（含步骤断言必填/工具白名单）。
    # 白名单 = 引擎工具 ∪ 能力面方法名（agent 只能引用真实存在的东西）。
    tool_names = set(default_tool_registry().names())
    tool_names.update(m.name for m in list_methods())
    validation = validate_scenario(
        scenario_doc,
        default_tool_registry(),
        default_assertion_registry(),
        tool_names,
        unregistered_as_hallucination=True,
    )
    if not validation.valid:
        return {"valid": False, "id": None, "issues": [i.to_dict() for i in validation.issues]}

    scenario = await create_scenario(
        project_id=params["project_id"],
        name=scenario_doc.get("name") or scenario_doc.get("code") or "未命名场景",
        purpose=scenario_doc.get("purpose"),
        local_vars=scenario_doc.get("local_vars"),
        flow=scenario_doc.get("flow"),
        status=scenario_doc.get("status", "draft"),
    )
    if steps:
        await create_steps(scenario.id, steps)
    return {"valid": True, "id": str(scenario.id), "issues": []}


# ── bug.triage —— 失败分流（写，确定性）──────────────────────────
@register_method(
    "bug.triage",
    kind="write",
    description="失败分流（design §04 §4）：failure_kind + 重跑结果 → 处理动作（纯函数，不调 LLM）。"
                "product_bug 可顺带建 ProjectIssue(issue_type=bug)（fingerprint 去重）。",
    params_schema={
        "type": "object",
        "properties": {
            "failure_kind": {"type": "string", "enum": list(FAILURE_KINDS)},
            "retry_passed": {"type": "boolean", "description": "重跑一次即过 → 判 env_flaky"},
            "run_scenario_id": {"type": "string", "description": "来源执行行（指纹上下文）"},
            "error_signature": {"type": "string", "description": "错误签名（指纹用）"},
            "step_type": {"type": "string", "description": "失败步骤类型（指纹用）"},
            "response_diff": {"description": "响应 diff（指纹用）"},
            "create_bug": {"type": "boolean", "description": "product_bug 时是否顺带建 bug（默认 false）"},
            "title": {"type": "string", "description": "建 bug 时的标题"},
            "symptom": {"type": "string", "description": "建 bug 时的症状"},
            "user_id": {"type": "string"},
            "project_id": {"type": "string"},
            "run_id": {"type": "string"},
        },
        "required": ["failure_kind"],
    },
    returns_schema={
        "type": "object",
        "properties": {"action": {"type": "string"}, "counts_as_failure": {"type": "boolean"}},
    },
)
async def bug_triage(params: Mapping[str, Any]) -> dict:
    """确定性分流 → (可选)建 bug。返回动作 + 目标 + 指纹 + bug_id。"""
    from ..defects import classify_failure, compute_fingerprint, create_bug_from_failure

    outcome = classify_failure(params["failure_kind"], retry_passed=params.get("retry_passed"))
    result: dict[str, Any] = {
        "kind": outcome.kind,
        "action": outcome.action,
        "counts_as_failure": outcome.counts_as_failure,
        "target": outcome.target,
        "label": outcome.label,
    }

    rsid = params.get("run_scenario_id")
    fp = None
    if rsid:
        fp = compute_fingerprint(
            rsid,
            params.get("error_signature"),
            params.get("step_type"),
            params.get("response_diff"),
        )
        result["fingerprint"] = fp

    if outcome.kind == "product_bug" and params.get("create_bug"):
        bug_id = await create_bug_from_failure(
            rsid or str(_uuid.uuid4()),
            title=params.get("title") or "测试失败（待 triage 补全）",
            symptom=params.get("symptom") or params.get("error_signature") or "",
            fingerprint=fp,
            user_id=params.get("user_id"),
            project_id=params.get("project_id"),
            from_run_id=params.get("run_id"),
        )
        result["bug_id"] = bug_id
        result["created_bug"] = True

    return result


# ── bug.available_actions —— 某状态下可执行动作（读，纯函数）────
@register_method(
    "bug.available_actions",
    kind="read",
    description="查某状态下可执行的流转动作（动作守卫表 BUG_ACTIONS 的纯函数查询，"
                "无需 bug_id）。前端列表页批量渲染动作菜单、Agent 预判可执行动作时用。"
                "传 bug_id 则读实际状态与 confirmed 位（更准）。",
    params_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": list(BUG_STATUSES) + ["fixed"], "description": "状态（fixed 别名→resolved）"},
            "bug_id": {"type": "string", "description": "缺陷 UUID（传它则读实际状态，优先于 status 参数）"},
            "confirmed": {"type": "boolean", "description": "确认位（影响 reopen 目标态；纯 status 查询时用）"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"status": {"type": "string"}, "actions": {"type": "array"}},
    },
)
async def bug_available_actions(params: Mapping[str, Any]) -> dict:
    """按 status（或 bug_id 实际状态）查可执行动作。"""
    from ..defects import available_actions as _aa

    if params.get("bug_id"):
        doc = await get_bug(params["bug_id"])
        return {"status": doc["status"], "actions": _aa(doc["status"], confirmed=bool(doc["confirmed"]))}
    status = _normalize_bug_status(params.get("status", "open"))
    return {"status": status, "actions": _aa(status, confirmed=bool(params.get("confirmed", False)))}


# ── run.status —— 运行状态（读，薄包装）──────────────────────────
@register_method(
    "run.status",
    kind="read",
    description="查运行状态（mc_test_run 生命周期 + summary 摘要），轻量于 run.get。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def run_status(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.tasks.get_run`，只回状态 + 摘要。"""
    run = await get_run(_uuid_of(params["run_id"], field_name="run_id"))
    return {
        "id": str(run.id),
        "code": run.code,
        "project_id": str(run.project_id),
        "status": run.status,
        "summary": dict(run.summary or {}),
        "trigger": run.trigger,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
    }


# ── run.result —— 运行结果（读：运行 + 场景 + 步骤证据）───────────
@register_method(
    "run.result",
    kind="read",
    description="查运行完整结果：运行 + 每个场景 + 每步证据（design §04 §1）。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    returns_schema={"type": "object", "properties": {"run": {"type": "object"}, "scenarios": {"type": "array"}}},
)
async def run_result(params: Mapping[str, Any]) -> dict:
    """运行 + 场景 + 步骤证据，一次给齐（报告/调试器直接消费）。"""
    run = await get_run(_uuid_of(params["run_id"], field_name="run_id"))
    scenarios = await list_run_scenarios(run.id)
    scenario_docs = []
    for rs in scenarios:
        steps = await list_step_results(rs.id)
        scenario_docs.append({
            "id": str(rs.id),
            "scenario_id": str(rs.scenario_id),
            "scenario_code": rs.scenario_code,
            "status": rs.status,
            "failure_kind": rs.failure_kind,
            "duration_ms": rs.duration_ms,
            "steps": [_step_result_document(s) for s in steps],
        })
    return {"run": _run_document(run), "scenarios": scenario_docs}


# ── case.confirm —— 用例确认门（写，用户拍板）───────────────────
@register_method(
    "case.confirm",
    kind="write",
    description="**确认门**：把 pending_confirmation 的用例确认为 active（用户拍板），"
                "confirm_ref 记 confirmed_by/at。只有用户可调——agent 不得自行跨过确认门。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "confirmed_by": {"type": "string", "description": "确认人 UUID"},
            "note": {"type": "string", "description": "确认备注（可选）"},
        },
        "required": ["case_id", "confirmed_by"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def case_confirm(params: Mapping[str, Any]) -> dict:
    """pending_confirmation → active，confirm_ref 记确认人。非 pending 状态拒绝。"""
    from tortoise.exceptions import DoesNotExist

    from ..assets.models import TestCase

    case_id = _uuid_of(params["case_id"], field_name="case_id")
    try:
        case = await TestCase.get(id=case_id)
    except DoesNotExist:
        raise MethodError(f"用例不存在：{case_id!r}", code="not_found") from None
    if case.status != "pending_confirmation":
        raise MethodError(
            f"用例当前 status={case.status}，只有 pending_confirmation 可确认",
            code="invalid_request",
        )
    ref = dict(case.confirm_ref or {})
    ref["confirmed_by"] = str(params["confirmed_by"])
    if params.get("note"):
        ref["confirm_note"] = str(params["note"])
    case.status = "active"
    case.confirm_ref = ref
    await case.save()
    return {"id": str(case.id), "status": case.status}


# ── knowledge.profile —— 锚点测试画像（读，L4 纯派生）───────────
@register_method(
    "knowledge.profile",
    kind="read",
    description="锚点测试画像（design §15.1 L4）：该锚点承载哪些知识、被哪些条目覆盖、最近测试状态。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "anchor_id": {"type": "string"},
        },
        "required": ["project_id", "anchor_id"],
    },
    returns_schema={"type": "object", "properties": {"anchor": {"type": "object"}, "profile": {"type": "object"}}},
)
async def knowledge_profile(params: Mapping[str, Any]) -> dict:
    """派生画像：从 mc_knowledge_edge 反向聚合该锚点的知识/覆盖。"""
    from ..knowledge.models import KnowledgeAnchor, KnowledgeEdge, KnowledgeItem, TestProfile

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    anchor_id = _uuid_of(params["anchor_id"], field_name="anchor_id")
    try:
        anchor = await KnowledgeAnchor.get(project_id=project_id, id=anchor_id)
    except Exception as exc:
        raise MethodError(f"锚点不存在：{anchor_id!r}", code="not_found") from exc

    outgoing = await KnowledgeEdge.filter(project_id=project_id, from_kind="anchor", from_id=anchor_id)
    item_ids = [e.to_id for e in outgoing if e.to_kind == "item"]
    items = await KnowledgeItem.filter(id__in=item_ids) if item_ids else []
    incoming = await KnowledgeEdge.filter(project_id=project_id, to_kind="anchor", to_id=anchor_id)
    covering_item_ids = [e.from_id for e in incoming if e.from_kind == "item"]
    covering = await KnowledgeItem.filter(id__in=covering_item_ids) if covering_item_ids else []

    profile, _ = await TestProfile.get_or_create(
        project_id=project_id,
        anchor_id=anchor_id,
        defaults={
            "rule_ids": [], "component_ids": [], "bug_pattern_ids": [],
            "template_ids": [], "coverage_gaps": {}, "test_status": "green",
        },
    )
    return {
        "anchor": {
            "id": str(anchor.id),
            "kind": anchor.kind,
            "path": anchor.path,
            "symbol": anchor.symbol,
            "label": anchor.label,
            "content_hash": anchor.content_hash,
            "confidence": 1.0,
        },
        "profile": {
            "rule_ids": list(profile.rule_ids or []),
            "component_ids": list(profile.component_ids or []),
            "bug_pattern_ids": list(profile.bug_pattern_ids or []),
            "template_ids": list(profile.template_ids or []),
            "coverage_gaps": dict(profile.coverage_gaps or {}),
            "last_tested_at": _iso(profile.last_tested_at),
            "test_status": profile.test_status,
            "items": [_item_document(i) for i in items],
            "covering_items": [_item_document(i) for i in covering],
        },
    }


# ── tool.register —— 自定义工具注册（写，走评审）────────────────
@register_method(
    "tool.register",
    kind="write",
    description="注册自定义工具（design §15.5）：入 status=review，**不自封 active**——"
                "通过评审才升级（AI 不裁判：框架管门禁，人拍板）。impl_ref 指向场景或方法。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "code": {"type": "string", "description": "稳定 ID（custom:<code> 引用）"},
            "purpose": {"type": "string", "description": "业务语义（这个工具解决什么问题）"},
            "scope": {"type": "string", "enum": list(CUSTOM_TOOL_SCOPES)},
            "signature": {"type": "object", "description": "输入/输出契约（JSONB）"},
            "impl_ref": {
                "type": "object",
                "description": "实现指向：{kind:scenario,id} 或 {kind:method,full_ref}",
            },
        },
        "required": ["project_id", "code"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def tool_register(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_custom_tool`，强制 status=review。"""
    from ..assets.service import _custom_tool_document

    row = await create_custom_tool(
        project_id=params["project_id"],
        code=params["code"],
        scope=params.get("scope", "project"),
        purpose=params.get("purpose"),
        signature=params.get("signature"),
        impl_ref=params.get("impl_ref"),
        status="review",
    )
    return _custom_tool_document(row)


# ── analyst.prepare —— PRD agent 摄取的确定性前半段（阶段 3）───
@register_method(
    "analyst.prepare",
    kind="execute",
    description="**B 轨 analyst 工作流的平台侧**：取源内容 → L1 → 开工作流运行（pending）→"
                "给 agent 一段任务上下文（含 raw_id/源 id/字段契约）。agent 解析后调 knowledge.put 落地。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "source_id": {"type": "string", "description": "源配置 UUID（决定取哪份内容）"},
            "raw_id": {"type": "string", "description": "也可直接指定已存在的 L1 raw"},
            "adapter_type": {"type": "string", "description": "B 轨格式（feishu/dingtalk/zentao/custom）"},
            "agent": {"type": "string", "description": "建议驱动的 agent 名（dsh/analyst）"},
            "notes": {"type": "string", "description": "给 agent 的额外指令"},
        },
        "required": ["project_id", "source_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string"}, "raw_id": {"type": "string"}, "task": {"type": "object"}},
    },
)
async def analyst_prepare(params: Mapping[str, Any]) -> dict:
    """取内容（source.fetch）→ 开 analyst 工作流运行 → 组装 agent 任务上下文。"""
    from ..workflow import start_run

    project_id = params["project_id"]
    source_id = params["source_id"]

    if params.get("raw_id"):
        fetch_result: dict[str, Any] = {"raw_id": params["raw_id"], "created": False}
    else:
        fetch_result = await _analyst_fetch_source(project_id, source_id, params)

    run = await start_run(
        "analyst",
        {
            "project_id": project_id,
            "source_id": source_id,
            "raw_id": fetch_result.get("raw_id"),
            "notes": params.get("notes"),
        },
        agent_runtime=params.get("agent"),
    )

    task = {
        "role": "analyst",
        "run_id": str(run.id),
        "raw_id": fetch_result.get("raw_id"),
        "source_id": source_id,
        "contract": {
            "只抽事实": "不确定处进 gaps 而非编造",
            "可测闸": "rule 需 condition+expected；requirement 需 acceptance；api_endpoint 需 method+path",
            "置信度": "AI 产出 confidence ≤ 0.95，status 恒 candidate（升 confirmed 是人工决定）",
            "落地": "解析结果调 knowledge.put（items: kind/key/title/content/confidence/anchors/links）",
        },
        "gaps": "不确定处调 question.raise（origin=product_uncertainty|design_doubt）",
    }
    if fetch_result.get("error"):
        task["fetch_error"] = fetch_result["error"]
    return {"run_id": str(run.id), "raw_id": fetch_result.get("raw_id"), "task": task}


# ── analyst.commit —— analyst 自动收口（阶段 3 闭环）─────────────
@register_method(
    "analyst.commit",
    kind="write",
    description="**analyst 自动收口**：items 落地（knowledge.put，过闸）+ 用契约合法的产出"
                "（knowledge_model/gaps）收口 prepare 开的那条工作流运行。agent 不用手拼 workflow.complete。",
    params_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "analyst.prepare 返回的工作流运行 UUID"},
            "project_id": {"type": "string"},
            "items": {
                "type": "array",
                "description": "agent 解析出的知识条目（同 knowledge.put items）",
            },
            "gaps": {
                "type": "array",
                "description": "缺口（不确定处；工作流契约必填，可空数组）",
            },
            "source_id": {"type": "string"},
            "raw_id": {"type": "string"},
            "agent": {"type": "string", "description": "产出者（dsh/analyst…）"},
            "raise_questions": {
                "type": "boolean",
                "description": "gaps 是否顺带 question.raise 落库（origin=design_doubt，默认 false）",
            },
        },
        "required": ["run_id", "project_id", "items", "gaps"],
    },
    returns_schema={
        "type": "object",
        "properties": {"workflow_status": {"type": "string"}, "put": {"type": "object"}},
    },
)
async def analyst_commit(params: Mapping[str, Any]) -> dict:
    """knowledge.put + workflow.complete（契约合法产出由本方法合成）。"""
    from ..workflow import complete_run

    items = list(params["items"] or [])
    gaps = list(params["gaps"] or [])

    put_result = await knowledge_put_items(
        project_id=params["project_id"],
        items=items,
        source_id=params.get("source_id"),
        provenance={"raw_id": params.get("raw_id")} if params.get("raw_id") else None,
        agent=params.get("agent"),
    )

    # gaps → 疑问落库（一等公民；origin 缺 design_doubt，可后续 resolve）
    if params.get("raise_questions") and gaps:
        for gap in gaps:
            await question_raise_service(
                project_id=params["project_id"],
                origin="design_doubt",
                content=str(gap.get("content") if isinstance(gap, Mapping) else gap),
                context=gap if isinstance(gap, Mapping) else {"gap": gap},
                raised_by=params.get("agent") or "analyst",
            )

    # 契约合法产出（analyst output_schema 要 knowledge_model + gaps）
    output = {
        "knowledge_model": put_result.get("items") or [],
        "gaps": gaps,
        "put_summary": {
            "created": put_result.get("created"),
            "updated": put_result.get("updated"),
            "unchanged": put_result.get("unchanged"),
            "gated": put_result.get("gated"),
        },
    }
    run = await complete_run(params["run_id"], output)
    return {
        "workflow_status": run.status,
        "run_id": str(run.id),
        "put": {
            "created": put_result.get("created"),
            "updated": put_result.get("updated"),
            "unchanged": put_result.get("unchanged"),
            "gated": put_result.get("gated"),
        },
        "issues": (run.output or {}).get("issues") if run.status == "failed" else None,
    }


async def _analyst_fetch_source(project_id: Any, source_id: Any, params: Mapping[str, Any]) -> dict:
    """从源配置取内容：builtin 有 path 走 fetch_file；B 轨给 agent 可读提示。"""
    from ..projects import get_source

    try:
        source = await get_source(source_id)
    except Exception:
        source = None
    adapter_type = params.get("adapter_type")
    if source is not None:
        adapter_type = source.adapter_type or adapter_type

    if source is not None and source.adapter == "builtin":
        config = source.config or {}
        path = config.get("path")
        if path:
            return await knowledge_fetch_file(
                project_id=project_id,
                source_id=source_id,
                kind="product" if source.kind == "prd" else source.kind,
                adapter_type=adapter_type or "text",
                path=str(path),
                provenance={"via": "analyst.prepare", "source": source.name},
            )
        return {
            "raw_id": None,
            "created": False,
            "error": f"源 {source.name} 是 builtin 但 config.path 为空；"
                     "请先在源配置里给 path，或改用 agent 轨 + content_base64",
        }
    return {
        "raw_id": None,
        "created": False,
        "error": f"B 轨源（{adapter_type or 'agent'}）：平台不写解析器也不取内容，"
                 "由 agent 取回后调 source.fetch（content_base64）落 L1，再 knowledge.put 落地",
    }


# ── release.capture —— 版本指针自动采集（阶段 4）───────────────
@register_method(
    "release.capture",
    kind="write",
    description="**版本快照自动采集**：把当前各源水位（git ref / 源 last_synced_at + hash）"
                "组装成 release 指针快照——不用手填，点了就锁版（幂等：同版本返回既有）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "version_code": {"type": "string", "description": "人工版本号（如 v1.2）"},
            "note": {"type": "string"},
            "created_by": {"type": "string"},
        },
        "required": ["project_id", "version_code"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version_code": {"type": "string"}}},
)
async def release_capture(params: Mapping[str, Any]) -> dict:
    """读当前 git 水位 + 源水位 → create_release（同版本已存在 → 返回既有）。"""
    from ..projects import create_release, list_gits, list_sources
    from ..projects.models import TestProjectRelease

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    version_code = str(params["version_code"])

    existing = await TestProjectRelease.filter(project_id=project_id, version_code=version_code).first()
    if existing is not None:
        return _release_document(existing)

    gits = await list_gits(project_id, status="active")
    git_tags: list[dict[str, Any]] = [
        {"git_id": str(git.id), "ref": git.ref_value or "main", "repo": git.name}
        for git in gits
    ]

    sources = await list_sources(project_id)
    prd_versions: list[dict[str, Any]] = []
    case_versions: list[dict[str, Any]] = []
    for src in sources:
        result = src.last_sync_result or {}
        entry = {
            "source_id": str(src.id),
            "ref": result.get("content_hash") or result.get("commit")
                   or (src.last_synced_at.isoformat() if src.last_synced_at else "never"),
            "synced_at": _iso(src.last_synced_at),
        }
        if src.kind == "prd":
            prd_versions.append(entry)
        elif src.kind == "case":
            case_versions.append(entry)

    release = await create_release(
        project_id=project_id,
        version_code=version_code,
        git_tags=git_tags,
        prd_versions=prd_versions,
        case_versions=case_versions,
        note=params.get("note"),
        created_by=params.get("created_by"),
    )
    return _release_document(release)


# ── release.impact —— 版本变更影响面（读，增量测试根基）─────────
@register_method(
    "release.impact",
    kind="read",
    description="**增量测试**：比较两个 release，报告哪些源变了 → 受影响知识条目/锚点/用例，"
                "供回归选例（重测 changed 涉及的用例即可）。",
    params_schema={
        "type": "object",
        "properties": {
            "base_release_id": {"type": "string"},
            "head_release_id": {"type": "string"},
            "include_cases": {"type": "boolean", "description": "是否回填受影响用例（默认 true）"},
        },
        "required": ["base_release_id", "head_release_id"],
    },
    returns_schema={"type": "object", "properties": {"diff": {"type": "object"}, "impact": {"type": "object"}}},
)
async def release_impact(params: Mapping[str, Any]) -> dict:
    """diff_releases → 受影响知识条目（按 source_id）→ 锚点 → 用例。"""
    from ..knowledge.models import KnowledgeAnchor, KnowledgeItem
    from ..projects import diff_releases, get_release

    base = await get_release(_uuid_of(params["base_release_id"], field_name="base_release_id"))
    head = await get_release(_uuid_of(params["head_release_id"], field_name="head_release_id"))
    diff = diff_releases(base, head)

    changed_source_ids: set[str] = set()
    for section in ("git_tags", "prd_versions", "case_versions"):
        changed_source_ids.update(diff[section].get("changed_ids", []))
        changed_source_ids.update(diff[section].get("removed", []))

    affected_items: list[KnowledgeItem] = []
    if changed_source_ids:
        for item in await KnowledgeItem.filter(project_id=head.project_id):
            prov = item.provenance or {}
            src = prov.get("source_id") or (str(item.source_id) if item.source_id else None)
            if src in changed_source_ids:
                affected_items.append(item)

    anchor_ids = {str(i.anchor_id) for i in affected_items if i.anchor_id}
    anchor_rows = await KnowledgeAnchor.filter(id__in=list(anchor_ids)) if anchor_ids else []
    anchors = [
        {"id": str(a.id), "kind": a.kind, "path": a.path, "symbol": a.symbol, "label": a.label}
        for a in anchor_rows
    ]

    cases: list[dict[str, Any]] = []
    if params.get("include_cases", True):
        from ..assets.models import TestCase

        for c in await TestCase.filter(project_id=head.project_id):
            ref = c.confirm_ref or {}
            if ref.get("source_id") in changed_source_ids or ref.get("scan_scope") in {
                f"source:{sid}" for sid in changed_source_ids
            }:
                cases.append({
                    "id": str(c.id), "code": c.code, "title": c.title,
                    "priority": c.priority, "status": c.status,
                })

    return {
        "diff": diff,
        "impact": {
            "changed_source_ids": sorted(changed_source_ids),
            "items": [_item_document(i) for i in affected_items],
            "anchors": anchors,
            "cases": cases,
        },
    }


# ── reliability.report —— 可靠性仪表（读，阶段 5）───────────────
@register_method(
    "reliability.report",
    kind="read",
    description="可靠性仪表（阶段 5）：项目知识总览——confirmed/candidate/stale 分布、"
                "锚定率、确定性与 agent 产出比、待确认清单（candidate 队列）。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"summary": {"type": "object"}, "candidates": {"type": "array"}}},
)
async def reliability_report(params: Mapping[str, Any]) -> dict:
    """统计 knowledge_item 的 status/置信度分布 + candidate 清单（人工确认队列）。"""
    from ..knowledge.models import KnowledgeAnchor, KnowledgeItem

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    items = await KnowledgeItem.filter(project_id=project_id)
    anchors_count = await KnowledgeAnchor.filter(project_id=project_id).count()

    deterministic_extractors = ("graphify", "builtin")
    confirmed = candidate = stale = 0
    deterministic = agent = 0
    candidates: list[dict[str, Any]] = []
    for i in items:
        prov = i.provenance or {}
        if i.status == "confirmed":
            confirmed += 1
        elif i.status == "candidate":
            candidate += 1
            candidates.append({
                "id": str(i.id), "kind": i.kind, "key": i.key, "title": i.title,
                "confidence": i.confidence,
                "anchors": [str(i.anchor_id)] if i.anchor_id else [],
                "gates": dict((i.content or {}).get("_gate", {})),
            })
        elif i.status == "stale":
            stale += 1
        if prov.get("extractor") in deterministic_extractors:
            deterministic += 1
        else:
            agent += 1

    return {
        "summary": {
            "project_id": str(project_id),
            "items_total": len(items),
            "anchors": anchors_count,
            "confirmed": confirmed,
            "candidate": candidate,
            "stale": stale,
            "deterministic": deterministic,
            "agent_produced": agent,
            "anchor_rate": round(anchors_count / len(items), 3) if items else 0.0,
        },
        "candidates": candidates,
    }


__all__ = [
    "METHOD_KINDS",
    "Method",
    "MethodError",
    "MethodNotFoundError",
    "MethodParamError",
    "MethodRegistry",
    "call_method",
    "default_method_registry",
    "get_method",
    "has_method",
    "list_methods",
    "register_method",
    "to_jsonable",
    "validate_params",
]


# ── api.* —— 接口管理（mc_test_api，Apifox 式一等实体）──────────
# 身份 = id（UUID），code 只是可读句柄（可重复，接口分版本后同一逻辑接口多条）。
# params/headers/request_body/responses 是**结构说明**；连接配置归环境管，这里不碰。
@register_method(
    "api.list",
    kind="read",
    description="按项目列接口（可按目录/方法/状态/标签/关键字收窄）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "folder_id": {"type": "string", "description": "目录 UUID（收窄到某目录）"},
            "method": {"type": "string", "enum": list(API_METHODS)},
            "status": {"type": "string", "enum": list(API_STATUSES)},
            "tag": {"type": "string", "description": "标签（精确匹配 tags 里任一项）"},
            "keyword": {"type": "string", "description": "名称模糊匹配"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"apis": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_apis`。"""
    rows = await list_apis(
        params["project_id"],
        folder_id=params.get("folder_id"),
        method=params.get("method"),
        status=params.get("status"),
        tag=params.get("tag"),
        keyword=params.get("keyword"),
    )
    return {"apis": [_api_document(row) for row in rows], "count": len(rows)}


@register_method(
    "api.get",
    kind="read",
    description="按 id 取单个接口（**只认 id**，code 不是键）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string", "description": "接口 UUID"}},
        "required": ["api_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
)
async def api_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.get_api`。"""
    return _api_document(await get_api(params["api_id"]))


@register_method(
    "api.changes",
    kind="read",
    description="接口的变更历史（design §15.12）：每条含变更人 / 时间 / 变更理由 / "
    "字段级前后 diff（``field_changes``）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string", "description": "接口 UUID"}},
        "required": ["api_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"changes": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_changes(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_changes`（entity_type=api）。"""
    api = await get_api(params["api_id"])
    rows = await list_changes("api", api.id)
    return {"changes": [_change_document(r) for r in rows], "count": len(rows)}


# ── api.source.* —— 接口来源（绑定的导入方式；一接口多来源；可同步）──
@register_method(
    "api.source.list",
    kind="read",
    description="一个接口的全部来源（绑定的导入任务/方式/URL/上次同步时间与状态）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}},
        "required": ["api_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"sources": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_source_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_api_sources`。"""
    from ..assets import _api_source_document

    rows = await list_api_sources(params["api_id"])
    return {"sources": [_api_source_document(r) for r in rows], "count": len(rows)}


@register_method(
    "api.source.sync",
    kind="write",
    description=(
        "同步一条来源：重抓其绑定任务的文档 URL → 按该来源的方式重新导入 → "
        "刷新该接口的内容与来源的 last_synced。spec_url 缺失时报错。"
    ),
    params_schema={
        "type": "object",
        "properties": {"source_id": {"type": "string"}},
        "required": ["source_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"created": {"type": "integer"}, "updated": {"type": "integer"}, "format": {"type": "string"}},
    },
)
async def api_source_sync(params: Mapping[str, Any]) -> dict:
    """同步一条来源（重抓 → 重导入 → 回写 last_synced）。"""
    from ..assets import get_api_source, mark_api_source_synced
    from ..importers import OpenAPIImportError, fetch_spec_url, import_openapi_to_apis

    src = await get_api_source(params["source_id"])
    if not src.spec_url:
        raise OpenAPIImportError("该来源没有 spec_url（粘贴导入的来源无法同步）")
    try:
        spec = await fetch_spec_url(src.spec_url)
        result = await import_openapi_to_apis(
            spec,
            src.project_id,
            format=src.source_format,
            job_id=src.job_id,
            spec_url=src.spec_url,
        )
    except Exception as exc:  # noqa: BLE001 —— 失败也要落到来源状态上
        await mark_api_source_synced(src.id, status="error", message=f"{type(exc).__name__}: {exc}")
        raise
    await mark_api_source_synced(
        src.id,
        status="ok",
        message=f"新建 {result.get('created', 0)} · 更新 {result.get('updated', 0)}",
    )
    return {
        "created": result.get("created", 0),
        "updated": result.get("updated", 0),
        "format": result.get("format"),
    }


@register_method(
    "api.source.delete",
    kind="write",
    description="删除一条来源（不再跟踪该导入方式；接口本体不动）。",
    params_schema={
        "type": "object",
        "properties": {"source_id": {"type": "string"}},
        "required": ["source_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def api_source_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_api_source`。"""
    from ..assets import delete_api_source

    deleted = await delete_api_source(params["source_id"])
    return {"deleted": int(deleted)}


@register_method(
    "api.create",
    kind="write",
    description=(
        "建接口（mc_test_api）：method/path + 结构说明（params/headers/body/responses）+ "
        "目录/标签/责任人。code 缺省自增（API-xxxx，可读句柄非唯一键）；身份是 id。"
    ),
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string", "description": "接口名称（必填）"},
            "method": {"type": "string", "enum": list(API_METHODS)},
            "path": {"type": "string", "description": "/api/v1/orders/{id}"},
            "code": {"type": "string", "description": "可读句柄（缺省自增 API-xxxx）"},
            "folder_id": {"type": "string", "description": "目录 UUID"},
            "protocol": {"type": "string", "enum": list(API_PROTOCOLS)},
            "status": {"type": "string", "enum": list(API_STATUSES)},
            "description": {"type": "string", "description": "说明/备注（同一字段）"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "owner_id": {"type": "string", "description": "责任人 UUID"},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "order": {"type": "integer", "description": "目录内排序"},
            "params": {"type": "array", "description": "参数说明 [{name,in,type,required,description,example,default}]"},
            "headers": {"type": "array", "description": "请求头说明 [{name,type,required,description,example}]"},
            "cookies": {"type": "array", "description": "Cookie 说明 [{name,value,description,required}]（独立于 params/headers）"},
            "body_type": {"type": "string", "enum": list(API_BODY_TYPES)},
            "request_body": {"type": "object", "description": "请求体结构说明（schema 树 + 示例）"},
            "responses": {"type": "array", "description": "响应说明 [{status_code,name,content_type,schema,example}]"},
            "requirement_id": {"type": "string", "description": "关联需求 UUID（主脊）"},
            "version_id": {"type": "string", "description": "发布版本 UUID（与生产一致；暂不自动关联）"},
            "source": {"type": "string", "enum": list(API_SOURCES)},
            "source_ref": {"type": "object", "description": "溯源 {operationId?, file?, line?}"},
            "created_by": {"type": "string", "description": "创建人 UUID"},
        },
        "required": ["project_id", "name", "method", "path"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def api_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_api`。"""
    api = await create_api(
        project_id=params["project_id"],
        name=params["name"],
        method=params["method"],
        path=params["path"],
        code=params.get("code"),
        folder_id=params.get("folder_id"),
        protocol=params.get("protocol", "http"),
        status=params.get("status", "designing"),
        description=params.get("description"),
        tags=params.get("tags"),
        owner_id=params.get("owner_id"),
        priority=params.get("priority"),
        order=params.get("order", 0),
        params=params.get("params"),
        headers=params.get("headers"),
        body_type=params.get("body_type", "none"),
        request_body=params.get("request_body"),
        responses=params.get("responses"),
        requirement_id=params.get("requirement_id"),
        version_id=params.get("version_id"),
        source=params.get("source", "manual"),
        source_ref=params.get("source_ref"),
        created_by=params.get("created_by"),
    )
    return _api_document(api)


@register_method(
    "api.update",
    kind="write",
    description="改接口：**必须**带 change_reason（变更门，design §15.12）；updated_by 记最后更新人。",
    params_schema={
        "type": "object",
        "properties": {
            "api_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "updated_by": {"type": "string", "description": "最后更新人 UUID"},
            "propagation": {"type": "object"},
            "rollback_ref": {"description": "可回滚依据"},
            "code": {"type": "string"},
            "name": {"type": "string"},
            "method": {"type": "string", "enum": list(API_METHODS)},
            "path": {"type": "string"},
            "protocol": {"type": "string", "enum": list(API_PROTOCOLS)},
            "status": {"type": "string", "enum": list(API_STATUSES)},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "owner_id": {"type": "string"},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "order": {"type": "integer"},
            "params": {"type": "array"},
            "headers": {"type": "array"},
            "cookies": {"type": "array"},
            "body_type": {"type": "string", "enum": list(API_BODY_TYPES)},
            "request_body": {"type": "object"},
            "responses": {"type": "array"},
            "requirement_id": {"type": "string"},
            "version_id": {"type": "string"},
            "folder_id": {"type": "string"},
        },
        "required": ["api_id", "change_reason"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def api_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_api`；缺 ``change_reason`` 即变更门拦截。"""
    change_reason = params.get("change_reason")
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "api.update 必须提供 change_reason（design §15.12 变更门：变更必有因）"
        )
    values = {key: value for key, value in params.items() if key in API_EDITABLE_FIELDS}
    api = await update_api(
        params["api_id"],
        change_reason=str(change_reason).strip(),
        propagation=params.get("propagation"),
        rollback_ref=params.get("rollback_ref"),
        updated_by=params.get("updated_by"),
        **values,
    )
    return _api_document(api)


@register_method(
    "api.move",
    kind="write",
    description="移动接口到目标目录（排序语义，不走变更门——不改变接口文档内容）。",
    params_schema={
        "type": "object",
        "properties": {
            "api_id": {"type": "string"},
            "folder_id": {"type": ["string", "null"], "description": "目标目录 UUID（null = 归根）"},
            "order": {"type": "integer"},
        },
        "required": ["api_id", "folder_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "folder_id": {"type": ["string", "null"]}}},
)
async def api_move(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.move_api`。"""
    api = await move_api(
        params["api_id"], folder_id=params.get("folder_id"), order=params.get("order", 0)
    )
    return _api_document(api)


@register_method(
    "api.delete",
    kind="write",
    description="删除接口（返回删除行数，0=不存在不报错）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}},
        "required": ["api_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def api_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_api`。"""
    deleted = await delete_api(params["api_id"])
    return {"deleted": int(deleted)}


# ── api_folder.* —— 接口目录树 ─────────────────────────────────
@register_method(
    "api_folder.list",
    kind="read",
    description="按项目列接口目录（parent_id/order/name 排序，前端组树）。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"folders": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_folder_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_folders`。"""
    rows = await list_folders(params["project_id"])
    return {"folders": [_api_folder_document(row) for row in rows], "count": len(rows)}


@register_method(
    "api_folder.create",
    kind="write",
    description="建接口目录（可嵌套；父目录必须同项目）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "parent_id": {"type": "string", "description": "父目录 UUID（空 = 根）"},
            "order": {"type": "integer"},
        },
        "required": ["project_id", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
)
async def api_folder_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_folder`。"""
    folder = await create_folder(
        project_id=params["project_id"],
        name=params["name"],
        parent_id=params.get("parent_id"),
        order=params.get("order", 0),
    )
    return _api_folder_document(folder)


@register_method(
    "api_folder.update",
    kind="write",
    description="改目录（改名/换父/排序）；换父做防循环校验。",
    params_schema={
        "type": "object",
        "properties": {
            "folder_id": {"type": "string"},
            "name": {"type": "string"},
            "parent_id": {"type": ["string", "null"], "description": "新父（null = 归根）"},
            "order": {"type": "integer"},
        },
        "required": ["folder_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
)
async def api_folder_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_folder`。

    ``parent_id`` 显式判键（区分「没传」与「传 null 归根」）。
    """
    kwargs: dict[str, Any] = {}
    if "name" in params:
        kwargs["name"] = params["name"]
    if "parent_id" in params:
        kwargs["parent_id"] = params["parent_id"]
    if "order" in params:
        kwargs["order"] = params["order"]
    folder = await update_folder(params["folder_id"], **kwargs)
    return _api_folder_document(folder)


@register_method(
    "api_folder.delete",
    kind="write",
    description="删目录（级联删子目录；挂其下的接口 folder_id 由 FK SET NULL 归根，接口不删）。",
    params_schema={
        "type": "object",
        "properties": {"folder_id": {"type": "string"}},
        "required": ["folder_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def api_folder_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_folder`。"""
    deleted = await delete_folder(params["folder_id"])
    return {"deleted": int(deleted)}


# ── catalog.* —— 统一目录树（需求/用例/场景/接口共用一张表，type 区分）──
# idiom 照 api_folder.*；差别：入参/行文档多 ``type``；list 按 project+type 过滤。
@register_method(
    "catalog.list",
    kind="read",
    description="按项目列统一目录（可按 type 收窄：requirement|case|scenario|api；"
                "parent_id/order/name 排序，前端组树）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "type": {"type": "string", "enum": list(CATALOG_TYPES), "description": "目录类型（可省略=全部）"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"folders": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def catalog_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_catalogs`。"""
    rows = await list_catalogs(params["project_id"], params.get("type"))
    return {"folders": [_catalog_document(row) for row in rows], "count": len(rows)}


@register_method(
    "catalog.create",
    kind="write",
    description="建统一目录（可嵌套；父目录必须同项目**同 type**，跨类型挂靠拒绝）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "type": {"type": "string", "enum": list(CATALOG_TYPES), "description": "目录类型"},
            "name": {"type": "string"},
            "parent_id": {"type": "string", "description": "父目录 UUID（空 = 根）"},
            "order": {"type": "integer"},
        },
        "required": ["project_id", "type", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
)
async def catalog_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_catalog`。"""
    folder = await create_catalog(
        project_id=params["project_id"],
        type=params["type"],
        name=params["name"],
        parent_id=params.get("parent_id"),
        order=params.get("order", 0),
    )
    return _catalog_document(folder)


@register_method(
    "catalog.update",
    kind="write",
    description="改目录（改名/换父/排序）；换父做防循环 + 同 type 校验。",
    params_schema={
        "type": "object",
        "properties": {
            "folder_id": {"type": "string"},
            "name": {"type": "string"},
            "parent_id": {"type": ["string", "null"], "description": "新父（null = 归根）"},
            "order": {"type": "integer"},
        },
        "required": ["folder_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
)
async def catalog_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_catalog`。

    ``parent_id`` 显式判键（区分「没传」与「传 null 归根」）。
    """
    kwargs: dict[str, Any] = {}
    if "name" in params:
        kwargs["name"] = params["name"]
    if "parent_id" in params:
        kwargs["parent_id"] = params["parent_id"]
    if "order" in params:
        kwargs["order"] = params["order"]
    folder = await update_catalog(params["folder_id"], **kwargs)
    return _catalog_document(folder)


@register_method(
    "catalog.delete",
    kind="write",
    description="删目录（级联删子目录；挂其下的用例 folder_id 显式置 NULL 归根，用例不删）。",
    params_schema={
        "type": "object",
        "properties": {"folder_id": {"type": "string"}},
        "required": ["folder_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def catalog_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_catalog`。"""
    deleted = await delete_catalog(params["folder_id"])
    return {"deleted": int(deleted)}


@register_method(
    "api.import_openapi",
    kind="write",
    description=(
        "导入接口文档 → **mc_test_api**（全字段：method/path/params/headers/body/"
        "responses/tags；首个 tag 派生目录）。**支持五种方式**：auto 自动识别、"
        "OpenAPI 3.x、Swagger 2.0、Postman Collection、**apifox**、HAR 抓包——"
        "``format`` 显式指定（推荐，不猜）。幂等键 = project+method+path。"
        "**dry_run=true 只预览不落库**。``offset/limit`` 分批导入（手动导入进度："
        "前端按 total 分批调用并上报进度）。"
    ),
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "归属项目 UUID"},
            "spec": {
                "type": ["object", "string"],
                "description": "接口文档：已解析 dict 或 JSON/YAML 文本",
            },
            "spec_url": {"type": "string", "description": "规格 URL（与 spec 二选一）"},
            "format": {
                "type": "string",
                "enum": ["auto", "openapi3", "swagger2", "postman", "apifox", "har"],
                "description": "导入方式：显式指定（不猜）或 auto 自动识别；默认 auto",
            },
            "dry_run": {"type": "boolean", "description": "只预览不落库（默认 false）"},
            "offset": {"type": "integer", "description": "分批导入起始（配合 limit 做进度）"},
            "limit": {"type": "integer", "description": "分批导入批量（配合 offset）"},
            "created_by": {"type": "string", "description": "创建人 UUID"},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "created": {"type": "integer"},
            "updated": {"type": "integer"},
            "skipped": {"type": "integer"},
            "folders": {"type": "array", "description": "一级目录名（兼容口径）"},
            "folder_tree": {
                "type": "array",
                "description": "嵌套目录树（dry-run）：{name, path, count, total, children, endpoints}",
            },
            "ungrouped": {"type": "integer", "description": "无 tag 的接口数（dry-run）"},
            "format": {"type": "string", "description": "实际使用的格式"},
            "total": {"type": "integer", "description": "本次解析出的接口总数（进度分母）"},
            "endpoints": {"type": "array"},
        },
    },
)
async def api_import_openapi(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.importers.import_openapi_to_apis`（spec 或 spec_url）。"""
    from ..importers import OpenAPIImportError, fetch_spec_url, import_openapi_to_apis

    spec = params.get("spec")
    if spec is None and params.get("spec_url"):
        spec = await fetch_spec_url(str(params["spec_url"]))
    if spec is None:
        raise OpenAPIImportError("必须提供 spec（dict 或 JSON/YAML 字符串）或 spec_url（远程抓取）")
    offset = params.get("offset")
    limit = params.get("limit")
    return await import_openapi_to_apis(
        spec,
        params["project_id"],
        format=params.get("format"),
        dry_run=bool(params.get("dry_run")),
        offset=int(offset) if offset is not None else None,
        limit=int(limit) if limit is not None else None,
        created_by=params.get("created_by"),
    )


# ── api.import_job.* —— 导入任务（可配置的定时导入）────────────
@register_method(
    "api.import_job.list",
    kind="read",
    description="按项目列接口导入任务（created_at 旧 → 新）。",
    params_schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"jobs": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_import_job_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_import_jobs`。"""
    rows = await list_import_jobs(params["project_id"])
    return {"jobs": [_import_job_document(r) for r in rows], "count": len(rows)}


@register_method(
    "api.import_job.get",
    kind="read",
    description="取一条导入任务（前端轮询进度/状态用）。",
    params_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    returns_schema={"type": "object"},
)
async def api_import_job_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.get_import_job`。"""
    return _import_job_document(await get_import_job(params["job_id"]))


@register_method(
    "api.import_job.create",
    kind="write",
    description=(
        "建导入任务：文档来源（spec_url 或 spec_text）+ 导入方式（source_format，"
        "含 apifox）+ 节奏（schedule.kind=manual/hourly/daily/weekly）。定时任务"
        "必须走 spec_url。"
    ),
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name": {"type": "string"},
            "source_format": {"type": "string", "enum": list(API_SOURCE_FORMATS)},
            "spec_url": {"type": "string", "description": "文档 URL（定时任务必填）"},
            "spec_text": {"type": "string", "description": "文档原文（粘贴/上传；仅手动）"},
            "schedule": {
                "type": "object",
                "description": '{kind: manual|hourly|daily|weekly, time: "HH:MM", weekday: 0-6}',
            },
            "enabled": {"type": "boolean", "description": "默认 true；false = 暂停定时"},
            "created_by": {"type": "string"},
        },
        "required": ["project_id", "name"],
    },
    returns_schema={"type": "object"},
)
async def api_import_job_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.create_import_job`。"""
    job = await create_import_job(
        project_id=params["project_id"],
        name=params["name"],
        source_format=params.get("source_format") or "auto",
        spec_url=params.get("spec_url"),
        spec_text=params.get("spec_text"),
        schedule=params.get("schedule") or {"kind": "manual"},
        enabled=bool(params.get("enabled", True)),
        created_by=params.get("created_by"),
    )
    return _import_job_document(job)


@register_method(
    "api.import_job.update",
    kind="write",
    description="改导入任务（名/方式/来源/节奏/启停）。schedule 变化后重算 next_run_at。",
    params_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "name": {"type": "string"},
            "source_format": {"type": "string", "enum": list(API_SOURCE_FORMATS)},
            "spec_url": {"type": ["string", "null"]},
            "spec_text": {"type": ["string", "null"]},
            "schedule": {"type": "object"},
            "enabled": {"type": "boolean"},
        },
        "required": ["job_id"],
    },
    returns_schema={"type": "object"},
)
async def api_import_job_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.update_import_job`。"""
    values = {
        key: params[key]
        for key in ("name", "source_format", "spec_url", "spec_text", "schedule", "enabled")
        if key in params
    }
    return _import_job_document(await update_import_job(params["job_id"], values))


@register_method(
    "api.import_job.delete",
    kind="write",
    description="删导入任务（物理删除；导入出的接口不受影响）。",
    params_schema={
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    },
    returns_schema={"type": "object", "properties": {"deleted": {"type": "integer"}}},
)
async def api_import_job_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.delete_import_job`。"""
    deleted = await delete_import_job(params["job_id"])
    return {"deleted": int(deleted)}


@register_method(
    "api.import_job.run",
    kind="write",
    description=(
        "立即执行一条导入任务（服务端取文档 → 按 source_format 显式导入 → 记录 "
        "last_run/last_result）。dry_run=true 只预览。手动导入的分批进度由前端循环 "
        "api.import_openapi（offset/limit）驱动，本方法一次跑完。"
    ),
    params_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "created_by": {"type": "string"},
        },
        "required": ["job_id"],
    },
    returns_schema={"type": "object"},
)
async def api_import_job_run(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.importers.run_import_job`。"""
    from ..importers import run_import_job

    return await run_import_job(
        params["job_id"],
        dry_run=bool(params.get("dry_run")),
        created_by=params.get("created_by"),
    )


# ── api ↔ 用例 / 场景 直连（P5）────────────────────────────────
@register_method(
    "api.case.link",
    kind="write",
    description="接口↔用例关联（幂等：重复链接更新 order 返回既有行）。",
    params_schema={
        "type": "object",
        "properties": {
            "api_id": {"type": "string"},
            "case_id": {"type": "string"},
            "order": {"type": "integer"},
        },
        "required": ["api_id", "case_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}, "case_id": {"type": "string"}, "order": {"type": "integer"}},
    },
)
async def api_case_link(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.link_api_case`。"""
    link = await link_api_case(params["api_id"], params["case_id"], order=params.get("order", 0))
    return {
        "api_id": str(link.api_id),
        "case_id": str(link.case_id),
        "order": link.order,
    }


@register_method(
    "api.case.unlink",
    kind="write",
    description="解除接口↔用例关联（返回删除行数）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}, "case_id": {"type": "string"}},
        "required": ["api_id", "case_id"],
    },
    returns_schema={"type": "object", "properties": {"unlinked": {"type": "integer"}}},
)
async def api_case_unlink(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.unlink_api_case`。"""
    deleted = await unlink_api_case(params["api_id"], params["case_id"])
    return {"unlinked": int(deleted)}


@register_method(
    "api.cases",
    kind="read",
    description="引用该接口的用例列表（接口详情「被哪些用例覆盖」面板）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}},
        "required": ["api_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"cases": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_cases(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_api_cases`。"""
    rows = await list_api_cases(params["api_id"])
    return {"cases": [_case_document(row) for row in rows], "count": len(rows)}


@register_method(
    "api.scenario.link",
    kind="write",
    description="接口↔场景关联（幂等；场景步骤 params.api_id 之外再挂显式关联，供影响面反查）。",
    params_schema={
        "type": "object",
        "properties": {
            "api_id": {"type": "string"},
            "scenario_id": {"type": "string"},
            "order": {"type": "integer"},
        },
        "required": ["api_id", "scenario_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}, "scenario_id": {"type": "string"}, "order": {"type": "integer"}},
    },
)
async def api_scenario_link(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.link_api_scenario`。"""
    link = await link_api_scenario(params["api_id"], params["scenario_id"], order=params.get("order", 0))
    return {
        "api_id": str(link.api_id),
        "scenario_id": str(link.scenario_id),
        "order": link.order,
    }


@register_method(
    "api.scenario.unlink",
    kind="write",
    description="解除接口↔场景关联（返回删除行数）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}, "scenario_id": {"type": "string"}},
        "required": ["api_id", "scenario_id"],
    },
    returns_schema={"type": "object", "properties": {"unlinked": {"type": "integer"}}},
)
async def api_scenario_unlink(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.unlink_api_scenario`。"""
    deleted = await unlink_api_scenario(params["api_id"], params["scenario_id"])
    return {"unlinked": int(deleted)}


@register_method(
    "api.scenarios",
    kind="read",
    description="引用该接口的场景列表（接口详情「被哪些场景引用」面板）。",
    params_schema={
        "type": "object",
        "properties": {"api_id": {"type": "string"}},
        "required": ["api_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"scenarios": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def api_scenarios(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_api_scenarios`。"""
    rows = await list_api_scenarios(params["api_id"])
    return {"scenarios": [_scenario_document(row) for row in rows], "count": len(rows)}


# ── scenario.apis / case.apis —— 反向：我引用了哪些接口（读）──────
@register_method(
    "scenario.apis",
    kind="read",
    description=(
        "该场景引用的接口列表（沿步骤 params.api_id 派生的关联索引；"
        "供场景信息卡「引用接口」展示）。"
    ),
    params_schema={
        "type": "object",
        "properties": {"scenario_id": {"type": "string", "description": "UUID 主键或 SCN-xxxx code"}},
        "required": ["scenario_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"apis": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def scenario_apis(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_scenario_apis`。"""
    rows = await list_scenario_apis(params["scenario_id"])
    return {"apis": [_api_document(row) for row in rows], "count": len(rows)}


@register_method(
    "case.apis",
    kind="read",
    description=(
        "该用例覆盖的接口列表（沿「用例→场景→步骤 params.api_id」派生的关联索引；"
        "供用例详情「覆盖接口」展示）。"
    ),
    params_schema={
        "type": "object",
        "properties": {"case_id": {"type": "string"}},
        "required": ["case_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"apis": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def case_apis(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.assets.list_case_apis`。"""
    rows = await list_case_apis(params["case_id"])
    return {"apis": [_api_document(row) for row in rows], "count": len(rows)}


# ════════════════════════════════════════════════════════════════
# 测试策略域（strategy，用户定版 2026-10-14：冒烟/全量/回归/性能四类；
# 一轮 = 挂版本 + 勾选用例 + 分配人员 + 测试时间 + 测试结果）
# ════════════════════════════════════════════════════════════════

# ── strategy.create —— 建测试策略（写）──────────────────────────
@register_method(
    "strategy.create",
    kind="write",
    description="建测试策略（一轮测试：冒烟/全量/回归/性能四类，挂测试版本，"
                "可带用例勾选、测试时间、本轮测试/开发人员名单）。code 自动 STRAT-xxxx。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "name": {"type": "string", "description": "策略名称（必填）"},
            "strategy_type": {
                "type": "string",
                "enum": list(STRATEGY_TYPES),
                "description": "冒烟 smoke / 全量 full / 回归 regression / 性能 performance",
            },
            "version_id": {"type": "string", "description": "测试版本 UUID（mc_test_project_version）"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": ["draft", "pending"], "description": "缺省 draft"},
            "start_time": {"type": "string", "description": "计划开始时间（ISO）"},
            "end_time": {"type": "string", "description": "计划结束时间（ISO）"},
            "actual_end_time": {"type": "string", "description": "实际完成时间（ISO；报告延期分析用）"},
            "delay_analysis": {"type": "string", "description": "延期原因分析（人工填）"},
            "owner_id": {"type": "string", "description": "本轮负责人 UUID"},
            "testers": {"type": "array", "description": "本轮测试人员（平台用户 id 列表）"},
            "developers": {"type": "array", "description": "本轮开发人员（平台用户 id 列表）"},
            "created_by": {"type": "string", "description": "创建者 UUID"},
            "case_ids": {"type": "array", "description": "勾选用例 id 列表（可空，后配）"},
        },
        "required": ["project_id", "name"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def strategy_create(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.create_strategy`（返回完整详情，同 strategy.get）。"""
    from ..strategy import create_strategy, get_strategy

    strategy = await create_strategy(
        project_id=params["project_id"],
        name=params["name"],
        strategy_type=params.get("strategy_type", "smoke"),
        version_id=params.get("version_id"),
        description=params.get("description"),
        status=params.get("status", "draft"),
        start_time=params.get("start_time"),
        end_time=params.get("end_time"),
        actual_end_time=params.get("actual_end_time"),
        delay_analysis=params.get("delay_analysis"),
        owner_id=params.get("owner_id"),
        testers=params.get("testers"),
        developers=params.get("developers"),
        created_by=params.get("created_by"),
        case_ids=params.get("case_ids"),
    )
    return await get_strategy(strategy.id)


# ── strategy.update —— 改策略（写，变更门强制）──────────────────
@register_method(
    "strategy.update",
    kind="write",
    description="改测试策略：**必须**带 change_reason（变更门），写 mc_test_change_record。",
    params_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "change_reason": {"type": "string", "description": "变更理由（变更门必填）"},
            "name": {"type": "string"},
            "strategy_type": {"type": "string", "enum": list(STRATEGY_TYPES)},
            "version_id": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": list(STRATEGY_STATUSES)},
            "start_time": {"type": "string"},
            "end_time": {"type": "string"},
            "actual_end_time": {"type": "string", "description": "实际完成时间（ISO；报告延期分析用）"},
            "delay_analysis": {"type": "string", "description": "延期原因分析（人工填）"},
            "owner_id": {"type": "string"},
            "testers": {"type": "array", "description": "本轮测试人员（平台用户 id 列表；覆盖）"},
            "developers": {"type": "array", "description": "本轮开发人员（平台用户 id 列表；覆盖）"},
            "created_by": {"type": "string", "description": "变更人 UUID"},
        },
        "required": ["strategy_id", "change_reason"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "version": {"type": "integer"}}},
)
async def strategy_update(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.update_strategy`（返回完整详情，同 strategy.get）。"""
    from ..strategy import get_strategy, update_strategy

    values = {
        key: params[key]
        for key in ("name", "strategy_type", "version_id", "description", "status",
                    "start_time", "end_time", "actual_end_time", "delay_analysis",
                    "owner_id", "testers", "developers")
        if key in params
    }
    await update_strategy(
        params["strategy_id"],
        change_reason=params["change_reason"],
        values=values,
        created_by=params.get("created_by"),
    )
    return await get_strategy(params["strategy_id"])


# ── strategy.list —— 列测试策略（读）────────────────────────────
@register_method(
    "strategy.list",
    kind="read",
    description="列测试项目下测试策略（可按 version_id / strategy_type / status 收窄），带结果汇总。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "测试项目 UUID"},
            "version_id": {"type": "string", "description": "按测试版本收窄"},
            "strategy_type": {"type": "string", "enum": list(STRATEGY_TYPES)},
            "status": {"type": "string", "enum": list(STRATEGY_STATUSES)},
        },
        "required": ["project_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def strategy_list(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.list_strategies`。"""
    from ..strategy import list_strategies

    rows = await list_strategies(
        params["project_id"],
        version_id=params.get("version_id"),
        strategy_type=params.get("strategy_type"),
        status=params.get("status"),
    )
    return {"items": rows, "count": len(rows)}


# ── strategy.get —— 取策略详情（读，含勾选用例 + 汇总）──────────
@register_method(
    "strategy.get",
    kind="read",
    description="取单个测试策略详情（含勾选用例、每例分配与结果、结果汇总）。",
    params_schema={
        "type": "object",
        "properties": {"strategy_id": {"type": "string", "description": "策略 UUID"}},
        "required": ["strategy_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "cases": {"type": "array"}}},
)
async def strategy_get(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.get_strategy`。"""
    from ..strategy import get_strategy

    return await get_strategy(params["strategy_id"])


# ── strategy.delete —— 删策略（写）──────────────────────────────
@register_method(
    "strategy.delete",
    kind="write",
    description="删测试策略（真删：主行 + 勾选行；历史 run/bug 不动）。",
    params_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "created_by": {"type": "string"},
        },
        "required": ["strategy_id"],
    },
    returns_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
)
async def strategy_delete(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.delete_strategy`。"""
    from ..strategy import delete_strategy

    await delete_strategy(params["strategy_id"], created_by=params.get("created_by"))
    return {"ok": True}


# ── strategy.set_cases —— 勾选用例（写，全量差异同步）───────────
@register_method(
    "strategy.set_cases",
    kind="write",
    description="全量重设策略勾选的用例（差异同步）：新勾选建行、取消删行、"
                "已有行保留结果；可带默认 assignee_id / exec_mode。",
    params_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "case_ids": {"type": "array", "description": "勾选用例 id 全集（空=清空勾选）"},
            "assignee_id": {"type": "string", "description": "新勾选行的默认执行人（可空）"},
            "exec_mode": {"type": "string", "enum": list(STRATEGY_EXEC_MODES),
                          "description": "新勾选行的执行方式（缺省 manual）"},
            "created_by": {"type": "string"},
        },
        "required": ["strategy_id", "case_ids"],
    },
    returns_schema={
        "type": "object",
        "properties": {"added": {"type": "array"}, "removed": {"type": "array"}, "kept": {"type": "integer"}},
    },
)
async def strategy_set_cases(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.set_strategy_cases`。"""
    from ..strategy import set_strategy_cases

    return await set_strategy_cases(
        params["strategy_id"],
        params["case_ids"],
        assignee_id=params.get("assignee_id"),
        exec_mode=params.get("exec_mode"),
        created_by=params.get("created_by"),
    )


# ── strategy.assign_case —— 改派/改执行方式（写）────────────────
@register_method(
    "strategy.assign_case",
    kind="write",
    description="改策略内单条用例的执行人/执行方式（勾选行必须已存在）。",
    params_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "case_id": {"type": "string"},
            "assignee_id": {"type": "string", "description": "分配给谁（可空=不改）"},
            "exec_mode": {"type": "string", "enum": list(STRATEGY_EXEC_MODES)},
        },
        "required": ["strategy_id", "case_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def strategy_assign_case(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.assign_case`。"""
    from ..strategy import assign_case

    row = await assign_case(
        params["strategy_id"],
        params["case_id"],
        assignee_id=params.get("assignee_id"),
        exec_mode=params.get("exec_mode"),
    )
    return {"id": str(row.id), "assignee_id": str(row.assignee_id) if row.assignee_id else None,
            "exec_mode": row.exec_mode}


# ── strategy.record_result —— 录测试结果（写）───────────────────
@register_method(
    "strategy.record_result",
    kind="write",
    description="录策略内单条用例的测试结果（人工执行录入 / 自动化回写同一落点）；"
                "executed_at 服务端打点。",
    params_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "case_id": {"type": "string"},
            "result": {"type": "string", "enum": list(STRATEGY_CASE_RESULTS)},
            "note": {"type": "string", "description": "结果备注（人工执行留痕）"},
            "executed_by": {"type": "string", "description": "结果录入人 UUID"},
        },
        "required": ["strategy_id", "case_id", "result"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "result": {"type": "string"}}},
)
async def strategy_record_result(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.record_case_result`。"""
    from ..strategy import record_case_result

    row = await record_case_result(
        params["strategy_id"],
        params["case_id"],
        result=params["result"],
        note=params.get("note"),
        executed_by=params.get("executed_by"),
    )
    return {"id": str(row.id), "result": row.result}


# ── strategy.remove_case —— 取消勾选（写）───────────────────────
@register_method(
    "strategy.remove_case",
    kind="write",
    description="取消策略内单条用例的勾选（连同其分配与结果）。",
    params_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "case_id": {"type": "string"},
        },
        "required": ["strategy_id", "case_id"],
    },
    returns_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
)
async def strategy_remove_case(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.remove_case`。"""
    from ..strategy import remove_case

    await remove_case(params["strategy_id"], params["case_id"])
    return {"ok": True}


# ── strategy.cases —— 列勾选用例（读，轻量）─────────────────────
@register_method(
    "strategy.cases",
    kind="read",
    description="列策略勾选的用例行（勾选 + 分配 + 结果），轻量不带策略主体。",
    params_schema={
        "type": "object",
        "properties": {"strategy_id": {"type": "string"}},
        "required": ["strategy_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"cases": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def strategy_cases(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.get_strategy` 的用例段。"""
    from ..strategy import get_strategy

    doc = await get_strategy(params["strategy_id"])
    cases = doc["cases"]
    return {"cases": cases, "count": len(cases), "summary": doc["summary"]}


# ── strategy.report —— 策略统计报告（读）────────────────────────
@register_method(
    "strategy.report",
    kind="read",
    description="一轮测试的统计报告（用户定版 2026-10-16）：用例段（勾选数/自动化数/"
                "结果分布）+ 缺陷段（总数/P0-P1[priority 1-2]/平均·最长·最短解决时长）"
                "+ 延时段（计划 end_time vs actual_end_time，未收尾按当前时间算已超期）。",
    params_schema={
        "type": "object",
        "properties": {"strategy_id": {"type": "string", "description": "策略 UUID"}},
        "required": ["strategy_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "strategy": {"type": "object"},
            "cases": {"type": "object"},
            "bugs": {"type": "object"},
            "delay": {"type": "object"},
        },
    },
)
async def strategy_report(params: Mapping[str, Any]) -> dict:
    """包 :func:`testing_platform.strategy.strategy_report`。"""
    from ..strategy import strategy_report as _report

    return await _report(params["strategy_id"])

