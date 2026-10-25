"""场景/步骤校验器（07 §2 / §4：AI 不裁判，框架裁判）。

对每个**入库的资产文档**（场景 dict）跑确定性规则，产出 `ValidationResult`：
通过才入库，不过则返回结构化 `{path, rule, msg, severity, hint}`（07 §1）。

本轮实现**硬规则（error 级）**，与 07 文档逐条对齐：

- §2.1 结构：规则 1 必填 / 规则 2 JSONB 结构 / 规则 3 引用存在 / 规则 4 唯一性
- §2.2 准则：规则 5 后置校验必填 / 规则 6 校验类型已注册 / 规则 7 步骤类型合法 /
          规则 8 方法引用存在 / 规则 9 依赖白名单 / 规则 10 变量作用域前缀 /
          规则 11 变量定义在前 / 规则 12 环境变量不硬编码
- §2.3 语义：规则 19（局部变量定义序，与 11 合并）/ 规则 20 flow 引用存在 /
            规则 21 response/result 单步骤临时作用域
- §4 幻觉：A4 凭空造工具/方法

warn 级规则（14/15/16/17/18、A1-A3）本轮不做（07 §7 决策 2：warn 可忽略并存，
后置实现）。

**纯函数**：`validate(scenario, tools, assertions, methods=None)` 无 IO、无副作用，
输入场景 dict + 注册表 → 输出 `ValidationResult`（好测）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from .errors import SEVERITY_ERROR, ValidationResult
from .registry import ValidationRegistries

# ── 只读复用引擎变量枚举（07 §6 registry.py：变量前缀查询用同一份真相）──
try:  # pragma: no cover - 正常路径
    from ..engine.variables import KNOWN_SCOPES, split_template
except Exception:  # pragma: no cover - 引擎未初始化时降级（解耦，不影响运行）
    KNOWN_SCOPES = ("local", "data", "auth", "env", "global", "response", "result", "dataset")
    _PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

    def split_template(text: str) -> list[tuple[str, str]]:
        parts: list[tuple[str, str]] = []
        cursor = 0
        for m in _PLACEHOLDER_RE.finditer(text or ""):
            if m.start() > cursor:
                parts.append(("text", text[cursor : m.start()]))
            parts.append(("var", m.group(1).strip()))
            cursor = m.end()
        if cursor < len(text):
            parts.append(("text", text[cursor:]))
        return parts


# ── 常量（对齐 07 / 01 §4.2）─────────────────────────────────────
#: 07 §2.2 规则 7：步骤类型枚举（含 AI/agent 步骤 + 流程控制步骤，01 §4.2 扩展）
STEP_TYPES: frozenset[str] = frozenset(
    {
        "request", "page", "android", "ios", "wait", "script", "check",
        "sql", "ws", "custom",
        "ai_judge", "agent_action", "agent_workflow",
        # 流程控制（executor 区域栈解释，无工具、无 post_checks）。
        "if", "else_if", "else", "end_if", "break", "end_scenario",
        # 引用场景（递归跑子场景、共享 ctx）。
        "scenario_ref",
    }
)

#: 流程控制步骤（if/else_if/else/end_if/break/end_scenario）：结构性，豁免 R5
#: 后置校验要求，并需成块平衡（R22）。
CONTROL_FLOW_TYPES: frozenset[str] = frozenset(
    {"if", "else_if", "else", "end_if", "break", "end_scenario"}
)
#: 不要求后置校验（R5）的步骤类型：流程控制 + wait（无有意义产出）。
NO_CHECK_STEP_TYPES: frozenset[str] = CONTROL_FLOW_TYPES | frozenset({"wait"})
#: if 块边界类型：if 开块，end_if 闭块；else_if/else 是块内分支。
BLOCK_OPENERS = frozenset({"if"})
BLOCK_CLOSERS = frozenset({"end_if"})
BLOCK_BRANCHES = frozenset({"else_if", "else"})

#: 07 §4 A4 / §2.2 规则 9：自定义工具白名单前缀（custom:xxx 属框架自定义工具）
CUSTOM_TOOL_PREFIX = "custom:"

#: 07 §2.3 规则 21：单步骤临时作用域（{{response.*}} / {{result.*}}）
EPHEMERAL_SCOPES = frozenset({"response", "result"})

#: 07 §2.2 规则 12：凭据形似键的启发式正则（password/token/secret 等）。
#: 词边界防子串误报：`secretary`/`tokenizer`/`passwordless` 不许命中
#: （与 generation/gates.py 同口径；`my_password` 带 `_` 分隔仍算命中）。
_CREDENTIAL_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:password|passwd|pwd|token|secret|access_key|api_key|apikey|"
    r"authorization|bearer|client_id|client_secret)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: 07 §2.2 规则 12：字面 URL 启发式
_URL_RE = re.compile(r"https?://", re.IGNORECASE)

#: 07 §2.2 规则 2：步骤通用字段形状（引用 01 §4.1）
_REQUIRED_STEP_KEYS = ("step_key", "seq", "type", "tool")

#: 07 §2.2 规则 11/§2.3 规则 19：post_cond 赋值的等号切分
_ASSIGN_RE = re.compile(r"\s*=\s*")


# ── 工具函数（纯、无副作用）────────────────────────────────────
def _placeholder_paths(text: str) -> list[str]:
    """提取一段文本里所有 ``{{path}}`` 占位符的路径（复用引擎 split_template）。"""
    return [chunk for kind, chunk in split_template(text or "") if kind == "var"]


def _head(path: str) -> str:
    """变量路径的作用域头部：'env.base_url' -> 'env'；'x' -> 'x'。"""
    return path.split(".", 1)[0].strip()


def _is_scoped(path: str) -> bool:
    """路径是否带作用域前缀（含点号）；裸变量名（无前缀）不算作用域引用。"""
    return "." in path


def _root_key(local_path: str) -> Optional[str]:
    """取 ``{{local.order_id}}`` / ``{{local.order.id}}`` 的顶层键（'order_id'/'order'）。"""
    head, _, rest = local_path.partition(".")
    if not rest:
        return None
    m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rest)
    return m.group(0) if m else None


def _post_cond_rhs(statement: str) -> Optional[str]:
    """取赋值语句的右值文本（读取部分）；无等号返回 None。"""
    parts = _ASSIGN_RE.split(statement or "", maxsplit=1)
    return parts[1] if len(parts) > 1 else None


def _iter_strings(obj: Any, path: str):
    """递归遍历 JSON 结构，产出 (位置路径, 字符串值)。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _iter_strings(v, f"{path}[{i}]")


def _iter_strings_skipping(obj: Any, path: str, skip_keys: frozenset):
    """同 `_iter_strings`，但跳过 dict 中指定键（用于 params.checks/assertions）。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in skip_keys:
                continue
            yield from _iter_strings_skipping(v, f"{path}.{k}" if path else str(k), skip_keys)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _iter_strings_skipping(v, f"{path}[{i}]", skip_keys)


def _step_path(step: Mapping[str, Any], suffix: str = "") -> str:
    """构造步骤内路径：``$.steps[?step_key='create_order'].params.url``。"""
    key = step.get("step_key")
    base = f"$.steps[?step_key={key!r}]"
    return base + suffix


def _iter_checks(step: Mapping[str, Any]):
    """收集步骤的全部后置校验条目（post_checks / assertions / params.checks）。

    执行器（executor._post_checks）支持 `post_checks` 与 `assertions` 别名；
    check 步骤的断言写在 `params.checks`（tools.py assert_tool），一并校验。
    """
    for field in ("post_checks", "assertions"):
        for item in step.get(field) or ():
            yield item
    params = step.get("params")
    if isinstance(params, Mapping):
        for field in ("checks", "assertions"):
            for item in params.get(field) or ():
                yield item


def _post_cond_statements(step: Mapping[str, Any]) -> list[str]:
    """提取步骤的 post_cond / post_conditions 赋值语句原文。"""
    out: list[str] = []
    for field in ("post_cond", "post_conditions"):
        for item in step.get(field) or ():
            statement = item.get("set") if isinstance(item, Mapping) else item
            if isinstance(statement, str):
                out.append(statement)
    return out


def _normalized_steps(steps: Any, res: ValidationResult) -> list[dict]:
    """归一化步骤列表（补默认 step_key/seq，按 seq 排序），并做规则 2 结构检查。

    与 executor._normalize_steps 对齐：step_key 缺省取 id/step_{i}，seq 缺省
    (i+1)*10；同时保留原始 type（规则 7 只查显式声明的 type，交换格式的
    `tool` 字段由规则 9 / A4 校验，避免误伤）。
    """
    if steps is None:
        return []
    if not isinstance(steps, (list, tuple)):
        res.add("$.steps", "R2_schema", "steps 必须是数组（07 §2.1 规则 2）", SEVERITY_ERROR,
                hint="场景文档的 steps 应为一个步骤对象数组")
        return []
    out: list[dict] = []
    for idx, raw in enumerate(steps):
        path = f"$.steps[{idx}]"
        if not isinstance(raw, Mapping):
            res.add(path, "R2_schema", f"步骤 [{idx}] 必须是对象（07 §2.1 规则 2）", SEVERITY_ERROR)
            continue
        step = dict(raw)
        # 步骤必须能解析出类型（type 或 tool 至少其一，01 §4.1 / executor）
        if not step.get("type") and not step.get("tool"):
            res.add(path, "R2_schema", "步骤缺少 type 或 tool（01 §4.2 步骤类型必填）", SEVERITY_ERROR,
                    hint="请声明 type（request/page/...）或 tool（交换格式）之一")
        if step.get("step_key") in (None, ""):
            step["step_key"] = str(step.get("id") or f"step_{idx}")
        if step.get("seq") is None:
            step["seq"] = (idx + 1) * 10
        if "params" in step and step["params"] is not None and not isinstance(step["params"], Mapping):
            res.add(f"{path}.params", "R2_schema", "params 必须是对象（07 §2.1 规则 2）", SEVERITY_ERROR,
                    hint="步骤参数应以结构化对象表达（method/url/headers/body 等）")
        for field in ("post_checks", "assertions"):
            if field in step and step[field] is not None and not isinstance(step[field], (list, tuple)):
                res.add(f"{path}.{field}", "R2_schema", f"{field} 必须是数组（07 §2.1 规则 2）", SEVERITY_ERROR)
        out.append(step)
    out.sort(key=lambda s: (0, float(s.get("seq"))) if _is_numeric(s.get("seq")) else (1, str(s.get("seq"))))
    return out


def _is_numeric(value: Any) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


# ── 07 §2.1 结构 ───────────────────────────────────────────────
def _check_required_fields(scenario: Mapping[str, Any], res: ValidationResult) -> None:
    """07 §2.1 规则 1：必填字段 code/name 非空。"""
    code = scenario.get("code")
    if code in (None, ""):
        res.add("$.code", "R1_required_fields", "场景缺少 code（07 §2.1 规则 1：code/name 必填）", SEVERITY_ERROR,
                hint="为场景赋予稳定 ID，如 SCN-0001")
    name = scenario.get("name")
    if name in (None, ""):
        res.add("$.name", "R1_required_fields", "场景缺少 name（07 §2.1 规则 1：code/name 必填）", SEVERITY_ERROR,
                hint="用一句话描述场景用途（实现视角）")


def _check_references(scenario: Mapping[str, Any], res: ValidationResult) -> None:
    """07 §2.1 规则 3：引用存在——transport/env/dataset 的 id 非空即可。

    FK 真实性/未归档校验留给 service 层（校验器是纯函数，不查库）。
    """
    for field in ("transport_id", "env_id", "dataset_id"):
        value = scenario.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            res.add(f"$.{field}", "R3_reference_present",
                    f"{field} 引用了空 id（07 §2.1 规则 3）", SEVERITY_ERROR,
                    hint="引用必须指向真实且未归档的 transport/env/dataset；若可选字段留空请用 null")


def _check_step_uniqueness(steps: Sequence[Mapping[str, Any]], res: ValidationResult) -> None:
    """07 §2.1 规则 4：step_key 场景内唯一；seq 不重复。"""
    seen_keys: dict[str, int] = {}
    seen_seqs: dict[Any, int] = {}
    for i, step in enumerate(steps):
        key = str(step.get("step_key"))
        if key in seen_keys:
            res.add(f"$.steps[{i}].step_key", "R4_step_key_unique",
                    f"step_key 场景内重复：{key!r}（首次出现在 steps[{seen_keys[key]}]）",
                    SEVERITY_ERROR, hint="step_key 是 flow 引用的唯一标识，请改为唯一值")
        else:
            seen_keys[key] = i
        seq = step.get("seq")
        if seq in seen_seqs:
            res.add(f"$.steps[{i}].seq", "R4_seq_unique",
                    f"seq 不重复：{seq!r} 已被 steps[{seen_seqs[seq]}] 使用",
                    SEVERITY_ERROR, hint="seq 是执行顺序的唯一真相，请使用不同序号")
        else:
            seen_seqs[seq] = i


def _check_flow(flow: Any, step_keys: set[str], res: ValidationResult) -> None:
    """07 §2.3 规则 20：flow 引用的 step_key 存在；顺带规则 3 pre_scenarios 非空。"""
    if flow is None:
        return
    if not isinstance(flow, Mapping):
        res.add("$.flow", "R2_schema", "flow 必须是对象（07 §2.1 规则 2）", SEVERITY_ERROR,
                hint="flow 用对象承载 pre_scenarios / conditions / early_exit")
        return
    # 07 §2.1 规则 3：前置场景引用 id 非空
    pre = flow.get("pre_scenarios")
    if pre is not None and not isinstance(pre, (list, tuple)):
        res.add("$.flow.pre_scenarios", "R3_reference_present", "pre_scenarios 必须是数组（07 §2.1 规则 3）",
                SEVERITY_ERROR)
    elif pre:
        for i, item in enumerate(pre):
            ref = item.get("ref") if isinstance(item, Mapping) else item
            if ref in (None, "") or (isinstance(ref, str) and not ref.strip()):
                res.add(f"$.flow.pre_scenarios[{i}]", "R3_reference_present",
                        "前置场景引用 id 不能为空（07 §2.1 规则 3）", SEVERITY_ERROR)

    # 07 §2.3 规则 20：conditions 的 then/else 引用 step_key 存在
    conditions = flow.get("conditions")
    if conditions is not None and not isinstance(conditions, (list, tuple)):
        res.add("$.flow.conditions", "R2_schema", "flow.conditions 必须是数组（07 §2.1 规则 2）", SEVERITY_ERROR)
    elif conditions:
        for ci, cond in enumerate(conditions):
            if not isinstance(cond, Mapping):
                continue
            for branch in ("then", "else"):
                refs = cond.get(branch)
                if refs is None:
                    continue
                if isinstance(refs, str):
                    refs = [refs]
                for ri, ref in enumerate(refs or ()):
                    if ref not in step_keys:
                        res.add(f"$.flow.conditions[{ci}].{branch}[{ri}]", "R20_flow_step_exists",
                                f"flow 引用了不存在的 step_key：{ref!r}（07 §2.3 规则 20）",
                                SEVERITY_ERROR, hint="conditions 分支里的 step_key 必须是本场景 steps 之一")

    # 07 §2.3 规则 20：early_exit 的 after 引用存在
    early = flow.get("early_exit")
    if early is not None and not isinstance(early, (list, tuple)):
        res.add("$.flow.early_exit", "R2_schema", "flow.early_exit 必须是数组（07 §2.1 规则 2）", SEVERITY_ERROR)
    elif early:
        for ei, rule in enumerate(early):
            if not isinstance(rule, Mapping):
                continue
            after = rule.get("after") or rule.get("after_step")
            if after is not None and after not in step_keys:
                res.add(f"$.flow.early_exit[{ei}].after", "R20_flow_step_exists",
                        f"early_exit 引用了不存在的 step_key：{after!r}（07 §2.3 规则 20）",
                        SEVERITY_ERROR, hint="early_exit.after 必须是本场景 steps 之一")


# ── 07 §2.2 准则 ────────────────────────────────────────────────
def _check_scope_prefixes(obj: Any, path: str, res: ValidationResult) -> None:
    """07 §2.2 规则 10：变量作用域前缀合法（{{x}} 的 x 前缀 ∈ 已知作用域）。

    复用 engine.variables 的 KNOWN_SCOPES（local/data/auth/env/global/
    response/result + dataset 别名）；裸变量名（无前缀）按引擎语义合法。
    """
    for p, text in _iter_strings(obj, path):
        for vpath in _placeholder_paths(text):
            if not _is_scoped(vpath):
                continue  # 裸变量名（{{x}}）由解析器回退，合法
            head = _head(vpath)
            if head not in KNOWN_SCOPES:
                res.add(p, "R10_scope_prefix",
                        f"变量作用域前缀未知：{{{{{vpath}}}}}，前缀 {head!r} 不在已知作用域 {sorted(KNOWN_SCOPES)}（07 §2.2 规则 10）",
                        SEVERITY_ERROR,
                        hint="使用 {{local.*}}/{{data.*}}/{{auth.*}}/{{env.*}}/{{response.*}}/{{result.*}}/{{global.*}} 等已知前缀")


def _check_step_types(steps: Sequence[Mapping[str, Any]], res: ValidationResult,
                     unregistered_as_hallucination: bool = False) -> None:
    """07 §2.2 规则 7：步骤类型合法（01 §4.2 枚举，含流程控制扩展）。

    `type` 缺失（只有 tool 的交换格式）不判；工具名合法性交给规则 9 / A4。
    `unregistered_as_hallucination=True` 时对未注册类型追加 A4（07 §4 A4 加强）。
    """
    for i, step in enumerate(steps):
        stype = step.get("type")
        if not stype:
            continue
        if stype not in STEP_TYPES:
            res.add(f"$.steps[{i}].type", "R7_step_type_valid",
                    f"步骤类型不合法：{stype!r}（07 §2.2 规则 7：合法类型 {sorted(STEP_TYPES)}）",
                    SEVERITY_ERROR,
                    hint="type 必须是 request/page/android/ios/wait/script/check/sql/ws/custom 之一，或流程控制 if/else_if/else/end_if/break/end_scenario")
            if unregistered_as_hallucination:
                res.add(f"$.steps[{i}].type", "A4_fabricated_ref",
                        f"步骤类型 {stype!r} 未注册，疑似凭空构造（07 §4 A4）", SEVERITY_ERROR)


def _check_assertions(step: Mapping[str, Any], regs: ValidationRegistries, res: ValidationResult) -> None:
    """07 §2.2 规则 6：校验类型已注册（查 AssertionRegistry）。

    method/script 类校验（01 §4.4）不走断言注册表，其 method_ref 由规则 8 负责；
    规则 2 顺带检查 check 条目的结构（必须是对象或字符串、必须有 type）。
    """
    step_key = step.get("step_key")
    base = _step_path(step)
    for ci, check in enumerate(_iter_checks(step)):
        path = f"{base}.post_checks[{ci}]"
        if isinstance(check, str):
            ctype = check
        elif isinstance(check, Mapping):
            ctype = check.get("type")
            if not ctype:
                res.add(path, "R2_schema", "后置校验条目缺少 type（07 §2.1 规则 2）", SEVERITY_ERROR,
                        hint="每条 post_check 需声明 type（status_code/json_path/...）")
                continue
            method_ref = check.get("method_ref")
            if ctype in ("method", "script") and method_ref:
                _check_method_ref(method_ref, path, regs, res)
        else:
            res.add(path, "R2_schema", "后置校验条目必须是对象或字符串（07 §2.1 规则 2）", SEVERITY_ERROR)
            continue
        # method/script 类由规则 8 管；其余必须已注册（07 §2.2 规则 6）
        if ctype in ("method", "script"):
            continue
        if not regs.has_assertion(ctype):
            res.add(path, "R6_assertion_registered",
                    f"校验类型未注册：{ctype!r}（07 §2.2 规则 6；已注册：{regs.assertion_names or ['无']}）",
                    SEVERITY_ERROR,
                    hint="post_checks.type 必须是断言注册表中已登记的校验类型")


def _check_method_ref(ref: Any, path: str, regs: ValidationRegistries, res: ValidationResult) -> None:
    """07 §2.2 规则 8 + §4 A4：方法引用存在（method_ref 在方法注册表中）。

    方法注册表 v1 接口预留：传 dict / callable / 注册表对象都行（registry.py）。
    未提供方法注册表时返回 ``None``（跳过，无法判定）；提供且未注册 → 记两条：
    - 规则 8（07 §2.2 准则口径：method_ref 需登记）
    - A4（07 §4 幻觉口径：凭空造方法 → error）
    """
    if ref is None:
        return
    exists = regs.has_method(str(ref))
    if exists is None:
        return  # 未提供方法注册表 → 跳过（v1 引擎未接入方法注册表）
    if not exists:
        res.add(path, "R8_method_ref_exists",
                f"方法引用不存在：{ref!r}（07 §2.2 规则 8：method_ref 需在方法注册表中登记）",
                SEVERITY_ERROR,
                hint="method_ref 形如 base.crypto.sign，需先在方法脚本注册表中登记")
        res.add(path, "A4_fabricated_ref",
                f"方法不存在：{ref!r}（07 §4 A4：method_ref 不在任何注册表 → 凭空造方法）",
                SEVERITY_ERROR,
                hint="确认方法已登记，或改用已注册的方法引用")


def _check_step_refs(step: Mapping[str, Any], regs: ValidationRegistries, res: ValidationResult) -> None:
    """07 §2.2 规则 8 + §4 A4：步骤级引用统一入口。

    - `script` 步骤的顶层 `method_ref` / `params.method_ref` → 规则 8（方法存在）
    - custom 步骤的 `params.tool_ref` → 规则 9（白名单）
    - 显式 `tool` / 非 custom 步骤的 `tool_ref` → A4（凭空造工具）

    说明：本文件把「凭空造方法」归到规则 8（准则口径），「凭空造工具」归到 A4
    （幻觉口径）——两者都是 07 §4 A4「tool_ref/method_ref 不存在」的硬校验面，
    只是分别从准则与幻觉两个视角命名，避免同一引用重复报两条。
    """
    # 规则 8：步骤级 method_ref（script 步骤调方法，01 §4.3 / 06 §6.2）
    method_ref = step.get("method_ref")
    if not method_ref:
        params = step.get("params")
        if isinstance(params, Mapping):
            method_ref = params.get("method_ref")
    if method_ref:
        _check_method_ref(method_ref, _step_path(step, ".method_ref"), regs, res)
    # 规则 9 + A4：工具引用
    _check_tool_refs(step, regs, res)


def _check_tool_refs(step: Mapping[str, Any], regs: ValidationRegistries, res: ValidationResult) -> None:
    """07 §2.2 规则 9 + 07 §4 A4：工具引用只能来自注册表（或 custom: 前缀）。

    - custom 步骤的 `params.tool_ref` → 规则 9（注册工具或 custom: 前缀）
    - 显式 `tool` 字段（交换格式）不在注册表 → A4（凭空造工具）
    """
    step_key = step.get("step_key")
    params = step.get("params") or {}
    # 规则 9：custom 步骤的 tool_ref 白名单
    if step.get("type") == "custom":
        ref = params.get("tool_ref") if isinstance(params, Mapping) else step.get("tool_ref")
        if ref:
            if not (str(ref).startswith(CUSTOM_TOOL_PREFIX) or regs.has_tool(str(ref))):
                res.add(_step_path(step, ".params.tool_ref"), "R9_tool_whitelist",
                        f"自定义工具引用不在白名单：{ref!r}（07 §2.2 规则 9：只允许已注册工具或 {CUSTOM_TOOL_PREFIX!r} 前缀）",
                        SEVERITY_ERROR,
                        hint="tool_ref 指向已注册的自定义工具，或以 custom: 前缀声明框架自定义工具")
        return
    # 07 §4 A4：显式 tool / 非 custom 步骤出现 tool_ref → 必须已注册
    tool = step.get("tool")
    if tool and not regs.has_tool(str(tool)):
        res.add(_step_path(step, ".tool"), "A4_fabricated_ref",
                f"工具不存在：{tool!r}（07 §4 A4：tool_ref/method_ref 不在任何注册表 → 凭空造工具）",
                SEVERITY_ERROR, hint="tool 字段必须是已注册工具名（http/wait/assert 或自定义工具）")
    if isinstance(params, Mapping):
        ref = params.get("tool_ref") or step.get("tool_ref")
        if ref and not str(ref).startswith(CUSTOM_TOOL_PREFIX) and not regs.has_tool(str(ref)):
            res.add(_step_path(step, ".params.tool_ref"), "A4_fabricated_ref",
                    f"工具不存在：{ref!r}（07 §4 A4：tool_ref 不在注册表 → 凭空造工具）",
                    SEVERITY_ERROR, hint="tool_ref 必须指向已注册工具")


def _check_hardcoded_env(step: Mapping[str, Any], res: ValidationResult) -> None:
    """07 §2.2 规则 12：环境变量不硬编码（URL/账号/密钥形似值 → 应走 {{env.*}}）。

    启发式（确定性正则）：
    - params/args 中出现以 http(s):// 开头的字面量 → error
    - password/token/secret 等键的明文值（非 {{}} 模板）→ error
    """
    base = _step_path(step)
    params = step.get("params") or {}
    args = step.get("args") or {}
    for obj, label in ((params, "params"), (args, "args")):
        if not isinstance(obj, Mapping):
            continue
        # URL 字面量：去掉 {{}} 模板后仍以 http(s):// 开头
        for p, text in _iter_strings(obj, f"{base}.{label}"):
            cleaned = re.sub(r"\{\{[^{}]*\}\}", "", text).strip()
            if _URL_RE.match(cleaned):
                res.add(p, "R12_no_hardcoded_env",
                        f"检测到硬编码 URL：{text!r}（07 §2.2 规则 12：环境变量不硬编码）",
                        SEVERITY_ERROR,
                        hint=f"URL 应写成 {{{{env.base_url}}}}/path，由环境注入")
        # 凭据键明文值（递归进嵌套 headers 等结构）
        _check_credential_keys(obj, f"{base}.{label}", res)


def _check_credential_keys(obj: Any, path: str, res: ValidationResult) -> None:
    """递归检查 dict 中凭据形似键的明文值（规则 12 的账号/密钥部分）。"""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if _CREDENTIAL_KEY_RE.search(str(k)):
                if isinstance(v, str) and v and "{{" not in v:
                    res.add(f"{path}.{k}", "R12_no_hardcoded_env",
                            f"凭据字段 {k} 使用了明文值（07 §2.2 规则 12：环境变量不硬编码）",
                            SEVERITY_ERROR,
                            hint=f"{k} 应引用 {{{{env.*}}}}/{{{{auth.*}}}} 而不是写死")
            _check_credential_keys(v, f"{path}.{k}", res)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_credential_keys(v, f"{path}[{i}]", res)


# ── 07 §2.3 语义 ────────────────────────────────────────────────
def _iter_step_read_strings(step: Mapping[str, Any]):
    """遍历步骤中所有「读取变量」的字符串位置（含嵌套）。

    post_cond 赋值语句只取右值（LHS 是写目标，不算读取）；
    覆盖 params/args/post_checks/pre_cond/when/if/description。
    """
    prefix = _step_path(step)
    params = step.get("params")
    if isinstance(params, Mapping):
        yield from _iter_strings(params, f"{prefix}.params")
    args = step.get("args")
    if args is not None:
        yield from _iter_strings(args, f"{prefix}.args")
    for field in ("post_checks", "assertions", "pre_cond", "pre_conditions"):
        value = step.get(field)
        if value is not None:
            yield from _iter_strings(value, f"{prefix}.{field}")
    for field in ("when", "if", "description"):
        value = step.get(field)
        if value is not None:
            yield f"{prefix}.{field}", str(value)
    for field in ("post_cond", "post_conditions"):
        for i, item in enumerate(step.get(field) or ()):
            statement = item.get("set") if isinstance(item, Mapping) else item
            if not isinstance(statement, str):
                continue
            rhs = _post_cond_rhs(statement)
            if rhs is not None:
                yield f"{prefix}.{field}[{i}].set(右值)", rhs


def _check_local_defs(steps: Sequence[Mapping[str, Any]], local_vars: Any,
                      res: ValidationResult) -> None:
    """07 §2.2 规则 11 / §2.3 规则 19：变量定义在前（{{local.k}} 必须先有写入）。

    定义来源（按执行序）：
    1. 场景 `local_vars` 的声明键（01 §3.1：声明 + 初值）
    2. 先前步骤 `post_cond` 的 `set` 写目标（06 §2.4：步骤产出写入局部变量）

    两处合并实现（规则 19 与规则 11 同一检查，注释此处对齐 07 §2.3 规则 19）。
    """
    defined: set[str] = set()
    if isinstance(local_vars, Mapping):
        for key in local_vars:
            root = re.split(r"[.\[]", str(key), 1)[0]
            if root:
                defined.add(root)
    for step in steps:
        step_key = step.get("step_key")
        # 1) 先检查本步骤的所有读取
        for p, text in _iter_step_read_strings(step):
            for vpath in _placeholder_paths(text):
                if not _is_scoped(vpath):
                    continue
                head = _head(vpath)
                if head not in ("local",):  # 只有 local 需要存在性证明；data/auth/env 运行期注入
                    continue
                root = _root_key(vpath)
                if root and root not in defined:
                    res.add(p, "R11_local_defined",
                            f"局部变量未定义先用：{{{{{vpath}}}}}（07 §2.2 规则 11 / §2.3 规则 19：{root!r} 必须先有声明或写入）",
                            SEVERITY_ERROR,
                            hint=f"在 local_vars 中声明 {root}，或由先前步骤的 post_cond 写入 {{{{local.{root}}}}}")
        # 2) 本步骤 post_cond 的写入加入 defined（供后续步骤消费）
        for statement in _post_cond_statements(step):
            lhs = _post_cond_lhs(statement)
            if lhs and lhs.startswith("local."):
                root = _root_key(lhs)
                if root:
                    defined.add(root)
        _ = step_key


def _post_cond_lhs(statement: str) -> Optional[str]:
    """取赋值语句的左值占位符路径（写目标，如 'local.order_id'）。"""
    parts = _ASSIGN_RE.split(statement or "", maxsplit=1)
    if not parts:
        return None
    lhs = parts[0]
    paths = _placeholder_paths(lhs)
    return paths[0] if paths else None


def _iter_pre_exec_strings(step: Mapping[str, Any]):
    """遍历「工具执行前」就能读取到的字符串（params/args/pre_cond/when/if）。

    排除 params.checks/assertions（check 步骤的断言在工具执行后评估，
    response/result 可用）；这些由规则 6/8 单独校验。
    """
    prefix = _step_path(step)
    params = step.get("params")
    if isinstance(params, Mapping):
        yield from _iter_strings_skipping(params, f"{prefix}.params", frozenset({"checks", "assertions"}))
    args = step.get("args")
    if args is not None:
        yield from _iter_strings(args, f"{prefix}.args")
    for field in ("when", "if"):
        value = step.get(field)
        if value is not None:
            yield f"{prefix}.{field}", str(value)
    for field in ("pre_cond", "pre_conditions"):
        value = step.get(field)
        if value is not None:
            yield from _iter_strings(value, f"{prefix}.{field}")


def _check_ephemeral_placement(step: Mapping[str, Any], res: ValidationResult) -> None:
    """07 §2.3 规则 21：{{response.*}} / {{result.*}} 只能在本步骤后置校验使用。

    单步骤临时作用域（06 §2.1）：response/result 只存活于产生它们的步骤的
    post_checks / post_cond（执行后）。工具执行前的 params/args/pre_cond/when/if
    出现它们就是「跨步骤引用」→ error。
    """
    for p, text in _iter_pre_exec_strings(step):
        for vpath in _placeholder_paths(text):
            head = _head(vpath)
            if head in EPHEMERAL_SCOPES:
                res.add(p, "R21_ephemeral_placement",
                        f"{head} 是单步骤临时作用域：{{{{{vpath}}}}} 只能在本步骤的 post_checks/post_cond 中使用（07 §2.3 规则 21，跨步骤引用 → error）",
                        SEVERITY_ERROR,
                        hint="用 {{local.*}} 承接该步骤产出，再在后续步骤消费")


def _check_post_checks_required(step: Mapping[str, Any], res: ValidationResult) -> None:
    """07 §2.2 规则 5：后置校验必填（每个 enabled 步骤 post_checks ≥ 1）。

    check 步骤的断言写在 params.checks / params.assertions（executor 兼容），
    视为满足后置校验要求；disabled 步骤（enabled=false）跳过；
    流程控制步骤（if/else_if/else/end_if/break/end_scenario）是结构性步骤，
    不跑工具，豁免；wait 步骤无有意义产出，豁免。
    """
    if step.get("enabled", True) is False:
        return
    if step.get("type") in NO_CHECK_STEP_TYPES:
        return
    if step.get("post_checks") or step.get("assertions"):
        return
    params = step.get("params")
    if isinstance(params, Mapping) and (params.get("checks") or params.get("assertions")):
        return
    res.add(_step_path(step, ".post_checks"), "R5_post_checks_required",
            f"步骤 {step.get('step_key')} 缺少后置校验（07 §2.2 规则 5：每个 enabled 步骤 post_checks ≥ 1）",
            SEVERITY_ERROR,
            hint="为该步骤添加至少一条 post_checks（如 status_code/json_path）")


def _check_control_blocks(steps: Sequence[Mapping[str, Any]], res: ValidationResult) -> None:
    """流程控制成块合法性（01 §4.2 扩展：executor 区域栈解释的结构前提）。

    - if/else_if/else/end_if 配对平衡：end_if 比开块多 → error；扫完仍有开块 → error
    - else_if / else 必须在 if 块内（栈空即 error）
    - break 不做配对检查（执行期跳出当前块，栈空时等价结束场景）
    """
    stack: list[tuple[int, str]] = []  # (steps 索引, step_key)
    for i, step in enumerate(steps):
        stype = step.get("type")
        if stype in BLOCK_OPENERS:
            stack.append((i, str(step.get("step_key"))))
        elif stype in BLOCK_BRANCHES:
            if not stack:
                res.add(f"$.steps[{i}].type", "R22_block_balance",
                        f"{stype!r} 不在任何 if 块内（流程控制成块：else_if/else 必须跟在 if 分支后）",
                        SEVERITY_ERROR,
                        hint="else_if/else 前面需要一个 if 步骤，分支结束后用 end_if 闭合")
        elif stype in BLOCK_CLOSERS:
            if not stack:
                res.add(f"$.steps[{i}].type", "R22_block_balance",
                        f"end_if 没有对应的 if 块（多了一个闭合）",
                        SEVERITY_ERROR,
                        hint="删掉多余的 end_if，或在它前面补一个 if 步骤")
            else:
                stack.pop()
    for i, key in stack:
        res.add(f"$.steps[{i}].type", "R22_block_balance",
                f"if 块未闭合：{key!r} 的 if 缺少 end_if",
                SEVERITY_ERROR,
                hint="每个 if 块用 end_if 闭合，分支内步骤才会被正确跳过")


# ── 入口 ────────────────────────────────────────────────────────
def validate(
    scenario: Mapping[str, Any],
    tool_registry: Any = None,
    assertion_registry: Any = None,
    method_registry: Any = None,
    *,
    unregistered_as_hallucination: bool = False,
) -> ValidationResult:
    """纯函数校验一个场景文档（07 §1/§5 校验门）。

    :param scenario: 场景 dict（含 steps/flow/local_vars 等）
    :param tool_registry: 工具注册表（engine ToolRegistry / dict / None）
    :param assertion_registry: 断言类型注册表（engine AssertionRegistry / dict / None）
    :param method_registry: 方法注册表（规则 8；v1 可传 dict/callable/None）
    :param unregistered_as_hallucination: 未注册步骤类型是否额外记 A4（默认 False）
    :return: ValidationResult{valid, issues[]}
    """
    res = ValidationResult()
    if not isinstance(scenario, Mapping):
        res.add("$", "R2_schema", "场景必须是对象（07 §2.1 规则 2）", SEVERITY_ERROR)
        return res
    regs = ValidationRegistries(tools=tool_registry, assertions=assertion_registry, methods=method_registry)

    # 07 §2.1 结构
    _check_required_fields(scenario, res)
    _check_references(scenario, res)
    steps = _normalized_steps(scenario.get("steps"), res)
    _check_step_uniqueness(steps, res)
    _check_control_blocks(steps, res)  # 流程控制成块平衡（R22，executor 区域栈结构前提）
    step_keys = {str(s.get("step_key")) for s in steps}
    _check_flow(scenario.get("flow"), step_keys, res)

    # 07 §2.2 规则 10：变量作用域前缀（全场景扫描，含 flow）
    _check_scope_prefixes(scenario, "$", res)

    # 07 §2.2 / §2.3 / §4：逐步骤硬规则
    for step in steps:
        _check_step_types([step], res, unregistered_as_hallucination=unregistered_as_hallucination)
        _check_post_checks_required(step, res)
        _check_assertions(step, regs, res)
        _check_step_refs(step, regs, res)  # 规则 8 方法 + 规则 9/A4 工具
        _check_hardcoded_env(step, res)
        _check_ephemeral_placement(step, res)
    # 07 §2.2 规则 11 / §2.3 规则 19：依赖执行序，最后统一走
    _check_local_defs(steps, scenario.get("local_vars"), res)

    return res


__all__ = [
    "STEP_TYPES",
    "CONTROL_FLOW_TYPES",
    "NO_CHECK_STEP_TYPES",
    "CUSTOM_TOOL_PREFIX",
    "EPHEMERAL_SCOPES",
    "KNOWN_SCOPES",
    "validate",
]