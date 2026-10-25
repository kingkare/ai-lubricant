"""断言库：注册表 + 内置断言（06 §4 / 01 §4.4）。

断言注册体结构（与校验器、执行器共用同一份真相）：

    {
      "type": "json_path",                       # 类型名
      "params_schema": {...},                    # 参数契约（JSON-schema 风格）
      "evaluate(actual, params) -> AssertionResult",
    }

`AssertionResult` 恒定携带 `passed` 与 `expected / actual / diff / message`，
执行器据此写 `mc_test_step_result`（预期 / 实际 / 差异，04 §1.3）。

内置断言（06 §4）：`status_code` / `json_path` / `contains` / `regex` / `type_match`。
`json_path` 的 operator：`eq` / `ne` / `not_null` / `null` / `exists` / `contains` /
`gte` / `gt` / `lte` / `lt`。

`evaluate` 允许同步或异步（`method` / `script` 类校验走方法脚本，06 §4），
框架统一用 `maybe_await` 适配（06 §5.2）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .aio import maybe_await
from .errors import AssertionNotRegisteredError

# ── 断言结果 ────────────────────────────────────────────────────
@dataclass
class AssertionResult:
    """一次后置校验的结果；字段与 `mc_test_step_result` 的 expected/actual/diff 对齐。"""

    assertion_type: str
    passed: bool
    expected: Any = None
    actual: Any = None
    diff: Any = None
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.assertion_type,
            "passed": self.passed,
            # 契约字段名 `pass`（与 06 §4 断言返回 {pass, expected, actual, message} 对齐）
            "pass": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "diff": self.diff,
            "message": self.message,
        }

    @staticmethod
    def ok(assertion_type: str, actual: Any, expected: Any = None, message: str = "") -> "AssertionResult":
        return AssertionResult(
            assertion_type=assertion_type, passed=True, expected=expected, actual=actual, message=message
        )

    @staticmethod
    def bad(
        assertion_type: str,
        actual: Any,
        expected: Any = None,
        *,
        diff: Any = None,
        message: str = "",
    ) -> "AssertionResult":
        if not message:
            message = f"[{assertion_type}] 断言失败：expected={expected!r}, actual={actual!r}"
        return AssertionResult(
            assertion_type=assertion_type, passed=False, expected=expected, actual=actual, diff=diff, message=message
        )


# ── 注册表 ──────────────────────────────────────────────────────
@dataclass
class Assertion:
    name: str
    params_schema: dict
    evaluate: Callable[[Any, dict], Any]


class AssertionRegistry:
    """断言类型注册表：`get(type)` 查注册体，`evaluate(type, actual, params)` 执行。"""

    def __init__(self, entries: Optional[dict[str, Assertion]] = None) -> None:
        self._assertions: dict[str, Assertion] = dict(entries or {})

    def register(
        self,
        name: Optional[str] = None,
        *,
        params_schema: Optional[dict] = None,
    ):
        """`@registry.register()` 装饰器；name 缺省取函数名。"""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            assert_name = name or func.__name__
            self._assertions[assert_name] = self._wrap(assert_name, func, params_schema or {})
            return func

        return deco

    @staticmethod
    def _wrap(name: str, func: Callable[..., Any], schema: dict) -> Assertion:
        async def evaluate(actual: Any, params: dict) -> Any:
            return await maybe_await(func(actual, params))

        return Assertion(name=name, params_schema=schema, evaluate=evaluate)

    def get(self, assertion_type: str) -> Assertion:
        try:
            return self._assertions[assertion_type]
        except KeyError:
            raise AssertionNotRegisteredError(
                f"校验类型未注册：{assertion_type!r}（已注册：{self.names() or ['无']}）"
            ) from None

    def names(self) -> list[str]:
        return sorted(self._assertions)

    def schema(self) -> dict[str, dict]:
        """导出 {type: params_schema}，供校验器/文档复用。"""
        return {name: entry.params_schema for name, entry in self._assertions.items()}

    async def evaluate(self, assertion_type: str, actual: Any, params: dict) -> AssertionResult:
        """执行一条断言；同步/异步断言统一适配（maybe_await）。"""
        entry = self.get(assertion_type)
        result = await entry.evaluate(actual, params or {})
        if isinstance(result, AssertionResult):
            result.assertion_type = assertion_type
            return result
        if isinstance(result, bool):
            return (
                AssertionResult.ok(assertion_type, actual)
                if result
                else AssertionResult.bad(assertion_type, actual)
            )
        if isinstance(result, dict) and "passed" in result:
            return AssertionResult(
                assertion_type=assertion_type,
                passed=bool(result.get("passed")),
                expected=result.get("expected"),
                actual=result.get("actual"),
                diff=result.get("diff"),
                message=result.get("message", ""),
            )
        raise TypeError(f"断言 {assertion_type} 返回了无法识别的结果：{type(result).__name__}")


# ── 共用工具 ────────────────────────────────────────────────────
def _fmt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 128 else text[:125] + "..."


def _safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _resolve_json_source(actual: Any) -> Any:
    """JSON 源：response dict 取 body/json（字符串自动解析），否则原样。"""
    if isinstance(actual, dict):
        body = actual.get("body", actual.get("json"))
        if body is None:
            return actual
        return body if not isinstance(body, str) else _safe_json_loads(body)
    if isinstance(actual, str):
        return _safe_json_loads(actual)
    return actual


def _jsonpath_get(doc: Any, path: str) -> tuple[bool, Any]:
    """轻量 JSONPath：`$.data.order_id` / `$.items[0].id`。返回 (found, value)。"""
    path = (path or "").strip()
    if path.startswith("$"):
        path = path[1:]
    if path.startswith("."):
        path = path[1:]
    cur: Any = doc
    if not path:
        return True, cur
    for name, index in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
        if index:
            if not isinstance(cur, (list, tuple)) or int(index) >= len(cur):
                return False, None
            cur = cur[int(index)]
            continue
        if isinstance(cur, dict):
            if name not in cur:
                return False, None
            cur = cur[name]
        elif isinstance(cur, (list, tuple)) and name.isdigit():
            if int(name) >= len(cur):
                return False, None
            cur = cur[int(name)]
        else:
            return False, None
    return True, cur


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _eq(left: Any, right: Any) -> bool:
    """数字跨 int/float 比较；bool/None 不做隐式转换；其余严格相等。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right or left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _extract_status(actual: Any) -> Any:
    if isinstance(actual, dict):
        return actual.get("status", actual.get("status_code"))
    return actual


# ── 内置断言实现（纯函数，注册在 build_default_registry）─────────
def _assert_status_code(actual: Any, params: dict) -> AssertionResult:
    expected = params.get("expected")
    status = _extract_status(actual)
    if expected is None:
        return AssertionResult.bad("status_code", status, expected, message="status_code 断言缺少 expected")
    if isinstance(expected, (list, tuple)):
        passed = status in list(expected)
    elif isinstance(expected, int) and not isinstance(expected, bool):
        passed = status == expected
    else:
        passed = str(status) == str(expected)
    if passed:
        return AssertionResult.ok("status_code", status, expected)
    return AssertionResult.bad("status_code", status, expected, diff={"status": status, "expected": expected})


def _assert_contains(actual: Any, params: dict) -> AssertionResult:
    expected = params.get("expected")
    case_sensitive = params.get("case_sensitive", True)
    if not case_sensitive and isinstance(expected, str) and isinstance(actual, str):
        passed = expected.lower() in actual.lower()
    elif isinstance(actual, str):
        passed = expected in actual
    elif isinstance(actual, (list, tuple, set)):
        passed = expected in actual
    elif isinstance(actual, dict):
        passed = expected in actual
    else:
        passed = False
    if passed:
        return AssertionResult.ok("contains", actual, expected)
    return AssertionResult.bad("contains", actual, expected, diff={"expected": expected})


def _assert_regex(actual: Any, params: dict) -> AssertionResult:
    pattern = params.get("pattern")
    if not pattern:
        return AssertionResult.bad("regex", actual, pattern, message="regex 断言缺少 pattern")
    haystack = actual if isinstance(actual, str) else str(actual)
    try:
        matched = re.search(pattern, haystack)
    except re.error as exc:
        return AssertionResult.bad(
            "regex", haystack, pattern, diff={"error": str(exc)}, message=f"正则非法：{exc}"
        )
    if matched:
        return AssertionResult.ok("regex", haystack[:512], pattern)
    return AssertionResult.bad("regex", haystack[:512], pattern, diff={"pattern": pattern})


def _assert_type_match(actual: Any, params: dict) -> AssertionResult:
    expected = str(params.get("expected_type", params.get("expected", ""))).strip().lower()
    actual_type = _type_name(actual)
    aliases = {
        "int": "integer",
        "bool": "boolean",
        "number": "float",
        "array": "list",
        "object": "dict",
        "str": "string",
    }
    expected = aliases.get(expected, expected)
    if expected == actual_type or (expected == "number" and actual_type in ("integer", "float")):
        return AssertionResult.ok("type_match", actual_type, expected)
    return AssertionResult.bad(
        "type_match",
        actual_type,
        expected,
        diff={"expected_type": expected, "actual_type": actual_type},
        message=f"[type_match] 类型不符：expected={expected}, actual={actual_type}",
    )


def _assert_json_path(actual: Any, params: dict) -> AssertionResult:
    """断言 JSON 字段；operator 缺省 = not_null。"""
    path = params.get("path")
    operator = params.get("operator", "not_null")
    expected = params.get("expected")
    if not path:
        return AssertionResult.bad("json_path", actual, None, message="json_path 断言缺少 path")

    doc = _resolve_json_source(actual)
    found, value = _jsonpath_get(doc, path)

    def bad(msg: str, diff: Any = None) -> AssertionResult:
        return AssertionResult.bad(
            "json_path",
            value if found else None,
            dict(path=path, operator=operator, expected=expected),
            diff=diff if diff is not None else {"path": path, "found": found, "value": _fmt(value)},
            message=f"[json_path] {path} {operator} 失败：{msg}",
        )

    if operator in ("exists", "not_null", "null"):
        if operator == "exists":
            if found:
                return AssertionResult.ok("json_path", value, dict(path=path, operator=operator))
            return bad("字段不存在")
        if operator == "null":
            if (not found) or value is None:
                return AssertionResult.ok("json_path", value, dict(path=path, operator=operator))
            return bad("字段非 null", {"path": path, "value": _fmt(value)})
        # not_null
        if found and value is not None:
            return AssertionResult.ok("json_path", value, dict(path=path, operator=operator))
        if not found:
            return bad("字段不存在或为 null", {"path": path, "found": found})
        return bad("字段为 null", {"path": path, "value": None})

    if not found:
        return bad("字段不存在")

    # design §19.6 契约用 "equals"/"not_equals"，代码历史用 "eq"/"ne"——
    # 两者都接受（等价别名），以文档词表为准（AI 生成按文档写）。
    if operator in ("eq", "equals"):
        if _eq(value, expected):
            return AssertionResult.ok("json_path", value, expected)
        return bad("值不相等", {"path": path, "expected": expected, "actual": value})
    if operator in ("ne", "not_equals"):
        if not _eq(value, expected):
            return AssertionResult.ok("json_path", value, expected)
        return bad("值不应相等", {"path": path, "expected": expected, "actual": value})
    if operator == "contains":
        if isinstance(value, str):
            passed = expected in value
        elif isinstance(value, (list, tuple, set)):
            passed = expected in value
        elif isinstance(value, dict):
            passed = expected in value
        else:
            passed = False
        if passed:
            return AssertionResult.ok("json_path", value, expected)
        return bad("不含期望元素", {"path": path, "expected": expected, "actual": value})
    if operator in ("gte", "gt", "lte", "lt"):
        try:
            if operator == "gte":
                passed = value >= expected
            elif operator == "gt":
                passed = value > expected
            elif operator == "lte":
                passed = value <= expected
            else:
                passed = value < expected
        except TypeError as exc:
            return bad(
                f"数值比较类型不匹配（{exc}）",
                {"path": path, "expected": expected, "actual": value},
            )
        if passed:
            return AssertionResult.ok("json_path", value, expected)
        return bad("数值比较不满足", {"path": path, "expected": expected, "actual": value})
    return bad(f"未知 operator {operator!r}")


# ── 默认注册表 ──────────────────────────────────────────────────
def build_default_registry() -> AssertionRegistry:
    """构建内置断言注册表（每次返回全新实例，便于测试隔离）。"""
    registry = AssertionRegistry()
    registry._assertions["status_code"] = registry._wrap(
        "status_code",
        _assert_status_code,
        {"type": "object", "properties": {"expected": {"type": ["integer", "array"]}}, "required": ["expected"]},
    )
    registry._assertions["json_path"] = registry._wrap(
        "json_path",
        _assert_json_path,
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "operator": {"enum": ["eq", "ne", "not_null", "null", "exists", "contains", "gte", "gt", "lte", "lt"]},
                "expected": {},
            },
            "required": ["path"],
        },
    )
    registry._assertions["contains"] = registry._wrap(
        "contains",
        _assert_contains,
        {"type": "object", "properties": {"expected": {}, "case_sensitive": {"type": "boolean"}}, "required": ["expected"]},
    )
    registry._assertions["regex"] = registry._wrap(
        "regex",
        _assert_regex,
        {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]},
    )
    registry._assertions["type_match"] = registry._wrap(
        "type_match",
        _assert_type_match,
        {"type": "object", "properties": {"expected_type": {"type": "string"}}, "required": ["expected_type"]},
    )
    return registry


# 进程级默认注册表（执行器默认使用；测试可传入自建实例）
_DEFAULT_REGISTRY: Optional[AssertionRegistry] = None


def default_registry() -> AssertionRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def register_assertion(name: Optional[str] = None, *, params_schema: Optional[dict] = None):
    """模块级装饰器：注册自定义断言到进程默认注册表。"""
    return default_registry().register(name, params_schema=params_schema)


async def evaluate_assertion(
    assertion_type: str,
    actual: Any,
    params: dict,
    registry: Optional[AssertionRegistry] = None,
) -> AssertionResult:
    """便捷入口：执行一条断言。"""
    return await (registry or default_registry()).evaluate(assertion_type, actual, params)


__all__ = [
    "AssertionResult",
    "Assertion",
    "AssertionRegistry",
    "AssertionNotRegisteredError",
    "build_default_registry",
    "default_registry",
    "register_assertion",
    "evaluate_assertion",
    "_jsonpath_get",
    "_resolve_json_source",
]
