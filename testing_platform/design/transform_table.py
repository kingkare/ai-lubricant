"""测试设计常量——变换表与覆盖轴（18 号文 §3.2/§3.4 的代码化）。

**纯常量 + 纯函数**，无 DB、无 LLM、无 import 平台其他模块——
机械枚举器（``enumerator.py``）与将来的 ``case.enumerate`` 能力面方法都从这取数。

覆盖三件事：
1. ``COVERAGE_AXES``——C01-C14 覆盖轴（治 17 号文断点 5：coverage_gaps 无标准表）；
2. ``TRANSFORMS_BY_TYPE``——参数类型 → 机械变体（治 17 号文断点 2：
   AI 连参数都不知道的问题；数据源是 ``mc_test_api`` 的
   ``params``/``request_body`` 结构说明）；
3. ``param_variants`` / ``slot_key``——骨架用例的幂等键生成（17 号文 §3.3）。
"""
from __future__ import annotations

from typing import Any, Mapping

# ── 覆盖轴（18 号文 §3.4，《18个Skills》原文 C01-C14）────────────
# 每条轴：code / name / applies_to（api=接口契约基准 / rule=业务规则基准 / flow=业务流基准）
COVERAGE_AXES: tuple[dict[str, str], ...] = (
    {"code": "C01", "name": "正常流", "applies_to": "api|flow"},
    {"code": "C02", "name": "异常流", "applies_to": "api|rule|flow"},
    {"code": "C03", "name": "边界值", "applies_to": "api|rule"},
    {"code": "C04", "name": "权限", "applies_to": "api"},
    {"code": "C05", "name": "并发", "applies_to": "api|flow"},
    {"code": "C06", "name": "重试", "applies_to": "api|flow"},
    {"code": "C07", "name": "审计", "applies_to": "rule|flow"},
    {"code": "C08", "name": "会话恢复", "applies_to": "flow"},
    {"code": "C09", "name": "数据一致性", "applies_to": "api|rule|flow"},
    {"code": "C10", "name": "上下游依赖", "applies_to": "api|flow"},
    {"code": "C11", "name": "幂等性", "applies_to": "api"},
    {"code": "C12", "name": "输入校验", "applies_to": "api|rule"},
    {"code": "C13", "name": "状态转换", "applies_to": "rule|flow"},
    {"code": "C14", "name": "安全约束", "applies_to": "api|rule|flow"},
)

AXIS_BY_CODE: dict[str, dict[str, str]] = {a["code"]: a for a in COVERAGE_AXES}

#: 通用轴——每个接口都适用的鉴权/幂等变体（17 号文 §4.4「全接口通用」行）
UNIVERSAL_AXES: tuple[dict[str, str], ...] = (
    {"axis": "C04", "variant": "no-token", "expect_status": 401, "label": "无 token"},
    {"axis": "C04", "variant": "expired-token", "expect_status": 401, "label": "过期 token"},
    {"axis": "C04", "variant": "cross-tenant", "expect_status": 403, "label": "越权 id"},
    {"axis": "C11", "variant": "repeat-submit", "expect_status": 409, "label": "重复提交"},
)

# ── 变换表（18 号文 §3.2）───────────────────────────────────────
# 键 = 参数类型（mc_test_api.params[].type / request_body schema 的 type）；
# 值 = 机械变体列表。每个变体 = 一条骨架用例 slot。
# expect: 预期 HTTP 状态码（三层断言的 status_code 层；biz_code/key_fields 由 AI/规则补）。
_V = lambda variant, axis, expect, label: {
    "variant": variant, "axis": axis, "expect_status": expect, "label": label,
}

TRANSFORMS_BY_TYPE: dict[str, tuple[dict[str, str], ...]] = {
    "required": (
        _V("valid", "C01", 200, "合法值"),
        _V("missing", "C12", 400, "缺失"),
        _V("empty", "C12", 400, "空串"),
        _V("null", "C12", 400, "null"),
    ),
    "integer": (
        _V("valid", "C01", 200, "合法值"),
        _V("min-1", "C03", 400, "下边界外"),
        _V("max+1", "C03", 400, "上边界外"),
        _V("non-numeric", "C12", 400, "非数字"),
        _V("zero", "C03", 400, "零值"),
        _V("negative", "C03", 400, "负数"),
    ),
    "number": (
        _V("valid", "C01", 200, "合法值"),
        _V("min-1", "C03", 400, "下边界外"),
        _V("max+1", "C03", 400, "上边界外"),
        _V("non-numeric", "C12", 400, "非数字"),
    ),
    "string": (
        _V("valid", "C01", 200, "合法值"),
        _V("empty", "C12", 400, "空串"),
        _V("over-max", "C03", 400, "超长"),
        _V("under-min", "C03", 400, "过短"),
        _V("special-chars", "C12", 400, "特殊字符"),
        _V("injection", "C14", 400, "注入串"),
    ),
    "enum": (
        _V("valid", "C01", 200, "合法枚举值"),   # 注：每个合法值各展开一条（见 param_variants）
        _V("invalid", "C12", 400, "非法枚举值"),
    ),
    "boolean": (
        _V("true", "C01", 200, "true"),
        _V("false", "C01", 200, "false"),
        _V("string-typed", "C12", 400, "字符串类型的布尔"),
    ),
    "array": (
        _V("valid", "C01", 200, "合法数组"),
        _V("empty", "C12", 400, "空数组"),
        _V("null", "C12", 400, "null"),
        _V("over-max-items", "C03", 400, "超量"),
    ),
    "uuid": (
        _V("valid", "C01", 200, "合法 UUID"),
        _V("malformed", "C12", 400, "格式错误"),
        _V("not-found", "C02", 404, "不存在的 id"),
    ),
    "datetime": (
        _V("valid", "C01", 200, "合法时间"),
        _V("malformed", "C12", 400, "格式错误"),
        _V("future", "C03", 400, "未来时间"),
        _V("past", "C03", 400, "过去时间"),
    ),
}

#: 缺省变换（type 不在表里 / object / 未知类型）——只出合法 + 缺失两档
DEFAULT_TRANSFORMS: tuple[dict[str, str], ...] = (
    _V("valid", "C01", 200, "合法值"),
    _V("missing", "C12", 400, "缺失"),
)

#: 变换表没有覆盖的参数类型归一（JSON schema type → 变换表键）
_TYPE_ALIASES: dict[str, str] = {
    "int": "integer", "long": "integer", "short": "integer",
    "float": "number", "double": "number", "decimal": "number",
    "str": "string", "text": "string",
    "bool": "boolean",
    "list": "array", "set": "array",
    "date": "datetime", "date-time": "datetime", "timestamp": "datetime",
    "id": "uuid", "uuid4": "uuid",
}

# 注入串样例（injection 变体的固定载荷——确定性，不让 AI 现造）
INJECTION_SAMPLES: tuple[str, ...] = (
    "' OR '1'='1",
    "<script>alert(1)</script>",
    "../../etc/passwd",
)

#: 禁止公式表达式（B 实证：不许产 "a".repeat(256) 这类）——超长变体的固定载荷
OVERLONG_SAMPLE = "x" * 300


def normalize_type(raw: Any) -> str:
    """参数类型归一：JSON schema type / 随意写法 → 变换表键。"""
    t = str(raw or "").strip().lower()
    return _TYPE_ALIASES.get(t, t)


def _param_kind(param: Mapping[str, Any]) -> str:
    """一个参数的变换键：有枚举值走 enum，否则按归一类型。"""
    if param.get("enum_values") or param.get("enum"):
        return "enum"
    t = normalize_type(param.get("type"))
    if param.get("required") and not t:
        return "required"
    return t or "string"


def _extract_min_max(param: Mapping[str, Any]) -> tuple[Any, Any]:
    """从参数说明抽 min/max（顶层或 schema 约束键）。"""
    return param.get("min_value", param.get("minimum")), param.get("max_value", param.get("maximum"))


def param_variants(param: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一个参数说明 → 骨架变体列表（18 号文基准一的生成单元）。

    输入是 ``mc_test_api.params[]`` 的一项（结构说明）：
    ``{name, in, type, required, description, example, default}``。
    输出每项：``{variant, axis, expect_status, label, value, param}``。
    纯函数——不发请求、不写库、不调 AI。
    """
    name = str(param.get("name") or "").strip()
    if not name:
        return []
    kind = _param_kind(param)
    transforms = TRANSFORMS_BY_TYPE.get(kind, DEFAULT_TRANSFORMS)
    lo, hi = _extract_min_max(param)
    enum_values = param.get("enum_values") or param.get("enum") or []
    valid = param.get("example", param.get("default"))

    out: list[dict[str, Any]] = []
    for t in transforms:
        variant = t["variant"]
        value: Any = None
        if variant == "valid":
            # enum 类型：每个合法值各展开一条（18 号文 §3.2 变换表）
            if kind == "enum" and isinstance(enum_values, (list, tuple)) and enum_values:
                for ev in enum_values:
                    out.append({
                        "variant": f"valid:{ev}", "axis": t["axis"],
                        "expect_status": t["expect_status"], "label": f"{t['label']}={ev}",
                        "value": ev, "param": name,
                    })
                continue
            value = valid
        elif variant == "min-1":
            value = (lo - 1) if isinstance(lo, (int, float)) else -1
        elif variant == "max+1":
            value = (hi + 1) if isinstance(hi, (int, float)) else 999999
        elif variant in ("over-max",):
            value = OVERLONG_SAMPLE
        elif variant == "under-min":
            value = "x"
        elif variant == "injection":
            value = INJECTION_SAMPLES[0]
        elif variant == "missing":
            value = _MISSING  # 哨兵：请求时剔除该参数
        elif variant == "null":
            value = None
        elif variant == "empty":
            value = [] if kind == "array" else ""
        elif variant == "zero":
            value = 0
        elif variant == "negative":
            value = -1
        elif variant == "non-numeric":
            value = "abc"
        elif variant == "malformed":
            value = "not-a-" + kind
        elif variant == "invalid":
            value = "__invalid_enum__"
        elif variant == "string-typed":
            value = "true"
        elif variant == "empty-array":
            value = []
        elif variant == "over-max-items":
            value = ["x"] * 1000
        elif variant == "future":
            value = "2099-01-01T00:00:00Z"
        elif variant == "past":
            value = "2000-01-01T00:00:00Z"
        elif variant == "not-found":
            value = "00000000-0000-0000-0000-000000000000"
        else:
            value = valid
        out.append({
            "variant": variant, "axis": t["axis"],
            "expect_status": t["expect_status"], "label": t["label"],
            "value": value, "param": name,
        })
    return out


class _Missing:
    """缺参哨兵——repr 友好，请求构造时据此剔除该参数。"""

    def __repr__(self) -> str:  # pragma: no cover
        return "<missing>"


_MISSING = _Missing()


def slot_key(*, api_id: str, param: str, axis: str, variant: str) -> str:
    """骨架用例的幂等键（17 号文 §3.3：``api:{code}:{param}:{class}:{variant}``）。

    - 参数轴：``api:{api_id}:{param}:{axis}:{variant}``
    - 通用轴：``auth:{api_id}:{variant}`` / ``idem:{api_id}:{variant}``
    重跑生成 = 冲突即跳过（bulk_create ignore_conflicts）——冗余从存储层根治。
    """
    if param == "__auth__":
        return f"auth:{api_id}:{variant}"
    if param == "__idem__":
        return f"idem:{api_id}:{variant}"
    return f"api:{api_id}:{param}:{axis}:{variant}"


def universal_variants() -> list[dict[str, Any]]:
    """全接口通用轴的变体（无 token/过期/越权/重复提交）——每接口 4 条。"""
    return [
        {
            "variant": u["variant"], "axis": u["axis"],
            "expect_status": u["expect_status"], "label": u["label"],
            "param": "__auth__" if u["axis"] == "C04" else "__idem__",
            "value": None,
        }
        for u in UNIVERSAL_AXES
    ]


def enumerate_api(api: Mapping[str, Any]) -> dict[str, Any]:
    """一个接口（mc_test_api 行）→ 全部骨架 slot。

    返回 ``{api_id, slots: [...], axes: {C01: n, ...}}``。
    纯函数——这是 17 号文 L1「机械枚举」的核心（0 token，秒级）。

    兼容两种 ``request_body`` 形状（openapi_api_importer 存 ``{"schema": {...}}``，
    手工编辑可能直接存 schema 树）：``body["schema"]["properties"]`` 优先，
    退 ``body["properties"]``。
    """
    api_id = str(api.get("id") or "")
    slots: list[dict[str, Any]] = []

    # ① 参数轴：params[]（query/path/cookie）+ request_body 顶层属性
    params = [p for p in (api.get("params") or []) if isinstance(p, Mapping)]
    for p in params:
        slots.extend(param_variants(p))
    body = api.get("request_body") or {}
    if not isinstance(body, Mapping):
        body = {}
    props = body.get("schema", {}).get("properties") if isinstance(body.get("schema"), Mapping) else None
    if not isinstance(props, Mapping):
        props = body.get("properties")
    if isinstance(props, Mapping):
        for pname, pschema in props.items():
            if isinstance(pschema, Mapping):
                slots.extend(param_variants({**pschema, "name": pname, "in": "body"}))

    # ② 通用轴（每接口固定 4 条）
    slots.extend(universal_variants())

    # ③ 幂等键 + 轴统计（重跑生成=冲突跳过的根基；覆盖度报表的输入）
    axes: dict[str, int] = {}
    for s in slots:
        s["key"] = slot_key(api_id=api_id, param=s["param"], axis=s["axis"], variant=s["variant"])
        axes[s["axis"]] = axes.get(s["axis"], 0) + 1

    return {"api_id": api_id, "slots": slots, "axes": axes}


__all__ = [
    "COVERAGE_AXES", "AXIS_BY_CODE", "UNIVERSAL_AXES", "TRANSFORMS_BY_TYPE",
    "DEFAULT_TRANSFORMS", "INJECTION_SAMPLES", "OVERLONG_SAMPLE",
    "normalize_type", "param_variants", "slot_key", "universal_variants", "enumerate_api",
]
