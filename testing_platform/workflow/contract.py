"""工作流产出**契约校验器**（design §6.2「平台只做契约校验」）。

**确定性门禁，绝不调 LLM**——产出由外部 agent 经能力面跑，平台在这里对产出
跑确定性校验：

* **结构校验**：``output`` 过角色 ``output_schema`` 的 JSON Schema 子集
  （``required`` / ``type`` / ``enum`` + 嵌套 ``properties``）。类型判断**复用**
  :func:`testing_platform.surface.methods._type_ok`（不重造）。
* **coder 硬门**（design §17.3 契约「只用已注册工具」）：产出场景的每个步骤
  其 ``tool_ref`` 必须是**已注册工具**或 ``custom:`` 前缀——从引擎工具注册表
  （``default_tool_registry()``）查询，命中 :data:`~..quality.validators.A4`
  口径（凭空造工具）。
* **designer 硬门**（确认门）：产出的每条 case 必须有 ``confirm_source``
  （``debug``/``doc``/``user_confirm`` 之一）。
* 其余角色只做 ``output_schema`` 结构校验。

错误形态**复用** :class:`testing_platform.quality.errors.ValidationResult` /
:class:`~testing_platform.quality.errors.ValidationIssue`（path/rule/msg/severity），
与资产校验器同一套门禁模型。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from ..quality.errors import SEVERITY_ERROR, ValidationIssue, ValidationResult
from ..surface.methods import _type_ok  # 复用 JSON Schema 子集的类型谓词
from .defs import CONFIRM_SOURCES, builtin_workflow_def


#: 规则编号前缀（工作流域专属，与 07 validators 的 R*/A4 区分）
_RULE = "WF"

# ── JSON Schema 子集遍历（required/type/enum + 嵌套 properties）───
def _schema_path(base: str, key: str) -> str:
    """JSON 定位路径（与 errors.py 的 ``$.steps[1].params.url`` 口径一致）。"""
    if isinstance(key, int):
        return f"{base}[{key}]"
    return f"{base}.{key}" if base else f"$.{key}"


def _check_schema(
    value: Any,
    schema: Mapping[str, Any],
    path: str,
    res: ValidationResult,
) -> None:
    """递归按简版 JSON Schema 校验 ``value``，把问题追加进 ``res``。

    支持：``type``（字符串或列表，如 ``["string","null"]``）、``required``
    （object 下）、``properties``（object 下递归）、``items``（array 下递归）、
    ``enum``、``additionalProperties``（放行多余字段与否）。
    """
    if not isinstance(schema, Mapping):
        return
    if value is None:
        return

    # type
    declared = schema.get("type")
    if declared is not None and not _type_ok(value, declared):
        res.add(
            path,
            f"{_RULE}_type",
            f"产出类型不符：期望 {declared!r}，实际 {type(value).__name__}",
            SEVERITY_ERROR,
        )
        return

    # enum
    allowed = schema.get("enum")
    if allowed and value not in allowed:
        res.add(
            path,
            f"{_RULE}_enum",
            f"取值非法：{value!r}（可选：{list(allowed)}）",
            SEVERITY_ERROR,
        )

    # object → required + properties 递归
    if isinstance(value, Mapping) and isinstance(schema.get("properties"), Mapping):
        props = schema["properties"]
        missing = [k for k in schema.get("required") or () if k not in value]
        for key in missing:
            res.add(_schema_path(path, key), f"{_RULE}_required", f"缺少必填字段：{key}", SEVERITY_ERROR)
        allow_extra = bool(schema.get("additionalProperties", True))
        if not allow_extra:
            for key in value:
                if key not in props:
                    res.add(_schema_path(path, key), f"{_RULE}_unknown", f"未知字段：{key}", SEVERITY_ERROR)
        for key, child in value.items():
            child_schema = props.get(key)
            if isinstance(child_schema, Mapping):
                _check_schema(child, child_schema, _schema_path(path, key), res)

    # array → items 递归
    if isinstance(value, (list, tuple)) and isinstance(schema.get("items"), Mapping):
        for idx, child in enumerate(value):
            _check_schema(child, schema["items"], _schema_path(path, idx), res)


def _schema_valid(schema: Optional[Mapping[str, Any]], output: Any) -> ValidationResult:
    """把产出按 ``output_schema`` 跑一遍结构校验，返回结果。"""
    res = ValidationResult()
    if schema and isinstance(schema, Mapping):
        _check_schema(output, schema, "$", res)
    return res


# ── 工具注册表查询（coder 硬门，只读查）──────────────────────────
def _known_tool_refs() -> tuple[Callable[[str], bool], list[str]]:
    """返回 ``(has, names)``——引擎默认工具注册表的只读投影。

    不 import 引擎顶层包（避免拉 executor 等重链），只读
    :mod:`testing_platform.engine.tools` 的默认注册表。
    """
    try:
        from ..engine.tools import default_registry

        registry = default_registry()
        names = registry.names()
        return registry.has, names
    except ImportError as exc:  # pragma: no cover - 极瘦链，正常不会发生
        return lambda _name: False, [f"<unavailable: {exc}>"]


#: 自定义工具白名单前缀（与 quality.validators.CUSTOM_TOOL_PREFIX 同口径）
CUSTOM_TOOL_PREFIX = "custom:"


def _check_coder_tool_refs(output: Mapping[str, Any], res: ValidationResult) -> None:
    """coder 硬门：每个场景步骤的 ``tool_ref`` 必须已注册或 ``custom:`` 前缀。

    design §17.3 coder 契约「只用已注册工具」——AI 只能引用引擎里真实存在
    的工具，凭空造工具一律拒（规则 ``A4_fabricated_ref`` 口径）。
    """
    has_tool, known = _known_tool_refs()
    scenarios = output.get("scenarios") if isinstance(output, Mapping) else None
    if not isinstance(scenarios, (list, tuple)):
        return
    for s_idx, scenario in enumerate(scenarios):
        if not isinstance(scenario, Mapping):
            continue
        steps = scenario.get("steps")
        if not isinstance(steps, (list, tuple)):
            continue
        for sp_idx, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            ref = step.get("tool_ref")
            if not ref:
                res.add(
                    f"$.scenarios[{s_idx}].steps[{sp_idx}].tool_ref",
                    "A4_fabricated_ref",
                    "场景步骤缺少 tool_ref（coder 契约：步骤必须引用工具）",
                    SEVERITY_ERROR,
                    hint="为步骤指定 tool_ref（已注册工具或 custom: 前缀）",
                )
                continue
            ref_str = str(ref)
            if ref_str.startswith(CUSTOM_TOOL_PREFIX) or has_tool(ref_str):
                continue
            res.add(
                f"$.scenarios[{s_idx}].steps[{sp_idx}].tool_ref",
                "A4_fabricated_ref",
                f"工具不存在：{ref_str!r}（coder 契约：只用已注册工具；已注册：{known}）",
                SEVERITY_ERROR,
                hint="tool_ref 指向已注册工具，或以 custom: 前缀声明框架自定义工具",
            )


def _check_designer_confirm_source(output: Mapping[str, Any], res: ValidationResult) -> None:
    """designer 硬门：每条 case 必须带 ``confirm_source``（确认门）。

    design §15.2 / §17.1 ``case.create``：用例「须带 confirm_source，过确认门」。
    """
    cases = output.get("cases") if isinstance(output, Mapping) else None
    if not isinstance(cases, (list, tuple)):
        # 结构校验会报 cases 缺失/类型错，这里只管已有逐条 case 的确认来源
        return
    for idx, case in enumerate(cases):
        if not isinstance(case, Mapping):
            continue
        source = case.get("confirm_source")
        if source in CONFIRM_SOURCES:
            continue
        res.add(
            f"$.cases[{idx}].confirm_source",
            f"{_RULE}_confirm_source",
            f"用例缺少合法 confirm_source：{source!r}（须为 {list(CONFIRM_SOURCES)} 之一）",
            SEVERITY_ERROR,
            hint="用例必须有确认来源（debug/doc/user_confirm），否则不进确认门",
        )


# ── 公开入口 ─────────────────────────────────────────────────────
def validate_output(role: str, output: dict, *, workflow_def: Optional[Mapping[str, Any]] = None) -> ValidationResult:
    """校验某角色的**产出**（工作流产出级门禁，不调 LLM）。

    :param role: ``planner|analyst|designer|coder|executor|debugger|reporter``。
    :param output: 该角色的产出 dict。
    :param workflow_def: 可选，覆盖内置定义（如从 DB ``mc_test_workflow_def``
        行取，含自定义 ``output_schema``）。缺省用 :mod:`.defs` 的内置定义。
    :return: :class:`ValidationResult` —— ``valid`` 只看 error 级问题。
    """
    wf = workflow_def if isinstance(workflow_def, Mapping) and workflow_def else builtin_workflow_def(role)
    if wf is None:
        res = ValidationResult()
        res.add("$", f"{_RULE}_unknown_role", f"未知角色：{role!r}", SEVERITY_ERROR)
        return res

    res = _schema_valid(wf.get("output_schema"), output)

    if role == "coder":
        _check_coder_tool_refs(output if isinstance(output, Mapping) else {}, res)
    if role == "designer":
        _check_designer_confirm_source(output if isinstance(output, Mapping) else {}, res)

    return res


__all__ = [
    "CUSTOM_TOOL_PREFIX",
    "validate_output",
    "ValidationIssue",
    "ValidationResult",
]