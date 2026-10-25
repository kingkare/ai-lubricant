"""变量系统：作用域链 + `{{}}` 模板解析（06 §2）。

作用域与优先级（06 §2.1）：

    local > data > auth > env > global      （近者优先）

两条铁律（06 §2.2）：

1. **类型保持**：`{{env.port}}` 是 int 就替换成 int，否则 `port > 8000` 会错。
2. **未定义即报错**：取不到就抛 `UndefinedVariableError`，绝不静默变空串。

本模块同时提供：

- `VariableResolver`：作用域链 + `resolve` / `interpolate`
- `eval_expression`：条件表达式求值（`{{a}} > 100` → bool），无 `eval`
- `merge_env_chain`：环境继承合并 + 环检测（06 §2.3）
- `Redactor`：敏感变量脱敏（06 §2.5）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .errors import (
    ConditionError,
    EnvCycleError,
    EnvNotFoundError,
    TemplateError,
    UndefinedVariableError,
)

# ── 作用域 ──────────────────────────────────────────────────────
#: 作用域优先级（近者优先，06 §2.1）——裸变量名（无前缀）按此顺序回退
SCOPE_PRIORITY: tuple[str, ...] = ("local", "data", "auth", "env", "global")

#: 单步骤临时作用域（06 §2.1：{{response.*}} / {{result.*}}）——有显式前缀，不参与裸名回退
EPHEMERAL_SCOPES: tuple[str, ...] = ("response", "result")

#: 变量作用域别名（{{dataset.x}} 等价 {{data.x}}，design §19.5 用 dataset）
SCOPE_ALIASES: dict[str, str] = {
    "dataset": "data",
    # {{result.x}} 与 {{response.x}} 同为「本步骤返回」——见 06 §2.1
}

#: 全部已知作用域前缀（含别名）——校验器规则 10 用同一份真相
KNOWN_SCOPES: tuple[str, ...] = SCOPE_PRIORITY + EPHEMERAL_SCOPES + tuple(SCOPE_ALIASES)

#: 单占位符：{{ path }}（允许内部空白）
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

#: 占位符语法：scope.a.b[0].c  —— 允许下标
_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")

#: 方法调用占位符：``{{Class.method(a, b, k: v)}}``（无作用域前缀、含调用括号）
#:   - ref   = ``Class.method``（同 script 步骤的 method_ref）
#:   - args  = 逗号分隔；支持位置参（``a``）与键参（``k: v``）
_METHOD_CALL_RE = re.compile(r"^\s*(?P<ref>[A-Za-z_][\w.]*[A-Za-z_])\s*\((?P<args>[^()]*)\)\s*$")


class _Missing:
    """哨兵：表示未取到值。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<MISSING>"


MISSING = _Missing()


# ── 模板辅助 ────────────────────────────────────────────────────
def split_template(template: str) -> list[tuple[str, str]]:
    """把模板拆成 [("text", s) | ("var", path), ...]。"""
    parts: list[tuple[str, str]] = []
    cursor = 0
    for m in _PLACEHOLDER_RE.finditer(template):
        if m.start() > cursor:
            parts.append(("text", template[cursor:m.start()]))
        parts.append(("var", m.group(1).strip()))
        cursor = m.end()
    if cursor < len(template):
        parts.append(("text", template[cursor:]))
    return parts


def is_single_placeholder(template: str) -> Optional[str]:
    """整串就是唯一一个占位符时返回其路径，否则 None。"""
    parts = split_template(template)
    if len(parts) == 1 and parts[0][0] == "var":
        return parts[0][1]
    return None


def _to_text(value: Any) -> str:
    """把变量值转成模板中的文本；容器类型无法有意义地拼接 → 报错。"""
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise TemplateError(
        f"变量值为 {type(value).__name__}，不能内插进混合字符串（请整串引用或改用 {{% raw %}} 语义）",
        details={"value_type": type(value).__name__},
    )


# ── 作用域链 ────────────────────────────────────────────────────
class VariableResolver:
    """变量作用域链：`local > data > auth > env > global`。

    - `resolve("env.base_url")` → 保持原类型的值（取不到报错）
    - `interpolate(template)` → 整串单占位符保类型；混合串拼字符串
    """

    def __init__(self, scopes: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self.scopes: dict[str, dict[str, Any]] = {name: {} for name in KNOWN_SCOPES}
        if scopes:
            for name, values in scopes.items():
                self.set_scope(name, values)
        # 敏感变量登记：{"env.SECRET": "<真值>"}（脱敏用）
        self.secrets: dict[str, str] = {}
        #: 方法调用占位符执行通道：``await caller(method_ref, args) -> Any``。
        #: 由 ``run.execute`` 注入 ``ctx.methods.call``；未注入（None）即不支持调用占位符。
        self.method_caller: Optional[Any] = None

    # -- 作用域管理 -------------------------------------------------
    @staticmethod
    def _canonical(scope: str) -> str:
        scope = (scope or "").strip()
        scope = SCOPE_ALIASES.get(scope, scope)
        if scope not in SCOPE_PRIORITY + EPHEMERAL_SCOPES:
            raise UndefinedVariableError(f"{scope}.*", scope=scope)
        return scope

    def set_scope(self, scope: str, values: Optional[Mapping[str, Any]]) -> None:
        self.scopes[self._canonical(scope)] = dict(values or {})

    def update_scope(self, scope: str, values: Optional[Mapping[str, Any]]) -> None:
        """增量合并进某作用域（不整体替换）。"""
        self.scopes[self._canonical(scope)].update(values or {})

    def set_local(self, key: str, value: Any) -> None:
        self.scopes["local"][key] = value

    def get_scope(self, scope: str) -> dict[str, Any]:
        return self.scopes[self._canonical(scope)]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: dict(values) for name, values in self.scopes.items()}

    # -- 解析 -------------------------------------------------------
    def resolve(self, path: str) -> Any:
        """按优先级解析 `scope.a.b`；未定义抛 `UndefinedVariableError`。"""
        value = self.lookup(path)
        if value is MISSING:
            raise UndefinedVariableError("{{%s}}" % path)
        return value

    def lookup(self, path: str) -> Any:
        """同 `resolve`，但未取到时返回 `MISSING` 哨兵（不抛）。

        - 显式作用域前缀（`env.x` / `response.json.a`）→ 精确取该作用域
        - 裸名（`x`，无已知前缀）→ 按 `local>data>auth>env>global` 回退
        - 前缀合法但变量不存在 → 返回 MISSING（`resolve` 再转成异常）
        """
        path = (path or "").strip()
        if not path:
            raise UndefinedVariableError("{{}}", scope="")
        head, _, rest = path.partition(".")
        canon = SCOPE_ALIASES.get(head.strip(), head.strip())
        if canon in SCOPE_PRIORITY + EPHEMERAL_SCOPES:
            root = self.scopes.get(canon) or {}
            if not rest:
                return root if root else MISSING
            return _dig(root, rest)
        # 裸名回退：按优先级取第一个命中的
        for name in SCOPE_PRIORITY:
            root = self.scopes.get(name) or {}
            if not rest:
                if path in root:
                    return root[path]
                continue
            if path in root:
                return root[path]
            found = _dig(root, path)
            if found is not MISSING:
                return found
        return MISSING

    def has(self, path: str) -> bool:
        try:
            return self.lookup(path) is not MISSING
        except UndefinedVariableError:
            return False

    # -- 模板内插 ---------------------------------------------------
    def interpolate(self, template: Any) -> Any:
        """解析模板。

        - 非字符串原样返回（dict/list 递归处理）
        - 整串单占位符 → 保持原类型
        - 混合串 → 拼接字符串
        """
        if isinstance(template, str):
            return self._interpolate_str(template)
        if isinstance(template, dict):
            return {k: self.interpolate(v) for k, v in template.items()}
        if isinstance(template, list):
            return [self.interpolate(v) for v in template]
        if isinstance(template, tuple):
            return tuple(self.interpolate(v) for v in template)
        return template

    def _interpolate_str(self, template: str) -> Any:
        parts = split_template(template)
        if not parts:
            return template
        if len(parts) == 1 and parts[0][0] == "var":
            return self.resolve(parts[0][1])  # 类型保持
        out: list[str] = []
        for kind, chunk in parts:
            if kind == "text":
                out.append(chunk)
            else:
                out.append(_to_text(self.resolve(chunk)))
        return "".join(out)

    # -- 异步内插（支持 {{Class.method(...)}} 调用占位符）-----------
    def parse_method_call(self, path: str) -> Optional[tuple[str, list[str]]]:
        """把 ``Class.method(a, b, k: v)`` 解析成 ``(ref, [原始参数串...])``；
        非调用语法返回 None。参数串后续各自按「值/裸变量/键参」解析。"""
        m = _METHOD_CALL_RE.match(path)
        if not m:
            return None
        ref = m.group("ref")
        # 作用域前缀（local/env/...）不算方法调用——那是取值路径不是 ref
        head = ref.partition(".")[0]
        if SCOPE_ALIASES.get(head, head) in SCOPE_PRIORITY + EPHEMERAL_SCOPES:
            return None
        raw_args = m.group("args").strip()
        args = _split_args(raw_args) if raw_args else []
        return ref, args

    async def resolve_async(self, path: str) -> Any:
        """异步求值单个占位符内容：先试方法调用，否则退回同步 `resolve`。"""
        call = self.parse_method_call(path)
        if call is None:
            return self.resolve(path)
        ref, arg_specs = call
        if self.method_caller is None:
            raise TemplateError(
                f"占位符 {{ {{{ref}(...)}} }} 需要方法调用通道，但本次执行未注入"
                "（run.execute 须加载方法库并注入 ctx.methods）",
                details={"method_ref": ref},
            )
        args = await self._bind_call_args(arg_specs)
        return await self.method_caller(ref, args)

    async def _bind_call_args(self, arg_specs: list[str]) -> dict[str, Any]:
        """把参数串解析成 kwargs：``k: v`` 显式命名；裸值按位置无法命名→要求方法用
        形参名匹配（这里位置参回退到 ``arg{i}``，交由 _bind_args 校验）。"""
        out: dict[str, Any] = {}
        positional: list[Any] = []
        for spec in arg_specs:
            name, sep, val = _split_kwarg(spec)
            if sep:
                out[name.strip()] = await self._resolve_operand(val.strip())
            else:
                positional.append(await self._resolve_operand(spec))
        # 位置参：方法脚本按形参名绑定，无名字则报缺参——这里按出现序补 argN 提示，
        # 真正的友好错误来自 method_channel._bind_args。
        for i, value in enumerate(positional):
            out.setdefault(f"arg{i}", value)
        return out

    async def _resolve_operand(self, operand: str) -> Any:
        """单个参数值：字面量（数字/布尔/null/带引号串）直接转；否则当变量/调用解析。"""
        literal = _parse_literal(operand)
        if literal is not MISSING:
            return literal
        return await self.resolve_async(operand)

    async def interpolate_async(self, template: Any) -> Any:
        """`interpolate` 的异步版：递归处理 dict/list，字符串里的占位符可含方法调用。"""
        if isinstance(template, str):
            return await self._interpolate_str_async(template)
        if isinstance(template, dict):
            return {k: await self.interpolate_async(v) for k, v in template.items()}
        if isinstance(template, list):
            return [await self.interpolate_async(v) for v in template]
        if isinstance(template, tuple):
            return tuple(await self.interpolate_async(v) for v in template)
        return template

    async def _interpolate_str_async(self, template: str) -> Any:
        parts = split_template(template)
        if not parts:
            return template
        if len(parts) == 1 and parts[0][0] == "var":
            return await self.resolve_async(parts[0][1])  # 类型保持
        out: list[str] = []
        for kind, chunk in parts:
            if kind == "text":
                out.append(chunk)
            else:
                out.append(_to_text(await self.resolve_async(chunk)))
        return "".join(out)

    # -- 敏感变量 ---------------------------------------------------
    def register_secret(self, path: str, value: Any) -> None:
        """登记敏感变量真值（用于日志/证据脱敏，06 §2.5）。"""
        if isinstance(value, str) and value:
            self.secrets[path] = value

    def redactor(self) -> "Redactor":
        return Redactor(self.secrets.values())


def _split_args(raw: str) -> list[str]:
    """按**顶层**逗号拆参数串（忽略引号/括号内的逗号）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in raw:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_kwarg(spec: str) -> tuple[str, bool, str]:
    """``k: v`` → ``(k, True, v)``；非键参 → ``("", False, spec)``（冒号必须在顶层）。"""
    depth = 0
    quote: str | None = None
    for i, ch in enumerate(spec):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            name = spec[:i].strip()
            if _NAME_RE.match(name):
                return name, True, spec[i + 1 :]
            return "", False, spec
    return "", False, spec


_NAME_RE = re.compile(r"^[A-Za-z_][\w]*$")


def _parse_literal(text: str) -> Any:
    """字面量：数字 / true|false / null / 带引号字符串 → 原生值；否则 MISSING。"""
    t = text.strip()
    if not t:
        return MISSING
    if (t[0] == t[-1]) and t[0] in ("'", '"') and len(t) >= 2:
        return t[1:-1]
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return MISSING


def _dig(root: Any, rest: str) -> Any:
    """沿 `a.b[0].c` 路径下钻；任一层缺失返回 MISSING。"""
    cur = root
    for seg in _SEGMENT_RE.finditer(rest):
        name, index = seg.group(1), seg.group(2)
        if index is not None:
            if not isinstance(cur, (list, tuple)):
                return MISSING
            i = int(index)
            if i >= len(cur):
                return MISSING
            cur = cur[i]
            continue
        if isinstance(cur, Mapping):
            if name not in cur:
                return MISSING
            cur = cur[name]
        elif isinstance(cur, (list, tuple)) and name.isdigit():
            i = int(name)
            if i >= len(cur):
                return MISSING
            cur = cur[i]
        else:
            return MISSING
    return cur


# ── 环境继承合并（06 §2.3）───────────────────────────────────────
def merge_env_chain(envs: Sequence[Mapping[str, Any]], env_id: Any) -> dict[str, Any]:
    """把环境继承链合并成一个扁平 dict（父先子后，子覆盖父）。

    `envs` 每项形如 `{"id":..., "parent_id":..., "vars": {...}}`。
    同 id 在链上出现两次 → `EnvCycleError`。
    """
    index = {e.get("id"): e for e in envs}
    chain: list[Mapping[str, Any]] = []
    seen: list[Any] = []
    cur = env_id
    while cur is not None:
        if cur in seen:
            raise EnvCycleError(f"环境继承链成环：{' -> '.join(str(x) for x in seen + [cur])}")
        seen.append(cur)
        env = index.get(cur)
        if env is None:
            raise EnvNotFoundError(f"环境不存在或不可见：{cur}")
        chain.append(env)
        cur = env.get("parent_id")
    merged: dict[str, Any] = {}
    for env in reversed(chain):  # 父先子后
        merged.update(env.get("vars") or {})
    return merged


# ── 敏感值脱敏（06 §2.5）────────────────────────────────────────
MASK = "***"


class Redactor:
    """把敏感变量真值从任意文本/结构里抹掉。"""

    def __init__(self, secrets: Iterable[str]) -> None:
        # 长值优先替换，避免短值先命中造成残留
        self._secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for s in self._secrets:
            if s in value:
                value = value.replace(s, MASK)
        return value

    def obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


# ── 条件表达式求值（无 eval）────────────────────────────────────
_TOKEN_RE = re.compile(
    r"""
    \{\{\s*[^{}]+?\s*\}\}          # {{path}}
  | "(?:[^"\\]|\\.)*"              # 双引号字符串
  | '(?:[^'\\]|\\.)*'              # 单引号字符串
  | -?\d+\.\d+                     # float
  | -?\d+                          # int
  | [A-Za-z_][A-Za-z0-9_]*         # 标识符 / 关键字 / 裸词
  | ==|!=|>=|<=|>|<                # 比较
  | \( | \)                        # 括号
    """,
    re.VERBOSE,
)

_CMP_OPS = ("==", "!=", ">=", "<=", ">", "<")
_WORD_OPS = ("in", "contains", "not_in")
_KEYWORDS = {"and", "or", "not", "true", "false", "null", "none", "True", "False", "None"}


def tokenize_expression(expr: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    text = expr.strip()
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ConditionError(f"条件表达式无法解析：{expr!r}（位置 {pos}：{text[pos:pos+10]!r}）")
        tokens.append(m.group(0))
        pos = m.end()
    return tokens


class _ExprParser:
    """递归下降：or → and → not → comparison → operand。"""

    def __init__(self, tokens: list[str], resolver: VariableResolver, expr: str) -> None:
        self.tokens = tokens
        self.i = 0
        self.resolver = resolver
        self.expr = expr

    # -- 工具 --
    def peek(self) -> Optional[str]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> str:
        tok = self.peek()
        if tok is None:
            raise ConditionError(f"条件表达式意外结束：{self.expr!r}")
        self.i += 1
        return tok

    def expect(self, tok: str) -> None:
        got = self.next()
        if got != tok:
            raise ConditionError(f"条件表达式期望 {tok!r}，得到 {got!r}：{self.expr!r}")

    # -- 语法 --
    def parse(self) -> Any:
        value = self.parse_or()
        if self.peek() is not None:
            raise ConditionError(f"条件表达式尾部有多余 token {self.peek()!r}：{self.expr!r}")
        return value

    def parse_or(self) -> Any:
        left = self.parse_and()
        while self.peek() == "or":
            self.next()
            right = self.parse_and()
            left = _truthy(left) or _truthy(right)
        return left

    def parse_and(self) -> Any:
        left = self.parse_not()
        while self.peek() == "and":
            self.next()
            right = self.parse_not()
            left = _truthy(left) and _truthy(right)
        return left

    def parse_not(self) -> Any:
        if self.peek() == "not":
            self.next()
            return not _truthy(self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        left = self.parse_operand()
        tok = self.peek()
        if tok in _CMP_OPS:
            self.next()
            right = self.parse_operand()
            return _compare(tok, left, right)
        if tok in _WORD_OPS:
            self.next()
            right = self.parse_operand()
            return _compare(tok, left, right)
        return left

    def parse_operand(self) -> Any:
        tok = self.next()
        if tok == "(":
            value = self.parse_or()
            self.expect(")")
            return value
        if tok.startswith("{{"):
            return self.resolver.resolve(tok[2:-2].strip())
        if tok.startswith('"') or tok.startswith("'"):
            return _unquote(tok)
        low = tok.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("null", "none"):
            return None
        if _is_number(tok):
            return float(tok) if ("." in tok) else int(tok)
        if tok in _KEYWORDS:
            raise ConditionError(f"条件表达式关键字位置非法：{tok!r}：{self.expr!r}")
        # 裸词：按字面量字符串处理（如 == created）
        return tok


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _unquote(tok: str) -> str:
    body = tok[1:-1]
    return body.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "null", "none")
    return bool(value)


def _compare(op: str, left: Any, right: Any) -> bool:
    try:
        if op == "==":
            return _eq(left, right)
        if op == "!=":
            return not _eq(left, right)
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == "in":
            return left in right
        if op == "not_in":
            return left not in right
        if op == "contains":
            if isinstance(left, str):
                return _to_text(right) in left
            if isinstance(left, (list, tuple, set, dict)):
                return right in left
            return False
    except TypeError as exc:
        raise ConditionError(f"条件比较类型不匹配：{left!r} {op} {right!r}（{exc}）") from exc
    raise ConditionError(f"未知比较运算符：{op}")


def _eq(left: Any, right: Any) -> bool:
    """宽松相等：数字跨 int/float 比较，字符串与数字不相等（不做隐式转换）。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right or left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def eval_expression(expr: Any, resolver: VariableResolver) -> bool:
    """求值条件表达式 → bool（06 §2.2 第 4 步）。

    - bool 原样返回；
    - 非字符串（int/list/...）按真值判断；
    - 字符串：无 `{{}}` 且无运算符时按真值判断，否则走表达式解析。
    """
    if isinstance(expr, bool):
        return expr
    if expr is None:
        return False
    if not isinstance(expr, str):
        return _truthy(expr)
    text = expr.strip()
    if not text:
        return True
    tokens = tokenize_expression(text)
    if not tokens:
        return True
    # 单个占位符 → 直接用其真值
    if len(tokens) == 1 and tokens[0].startswith("{{"):
        return _truthy(resolver.resolve(tokens[0][2:-2].strip()))
    # 单个字面量 → 真值判断
    if len(tokens) == 1 and tokens[0] not in _CMP_OPS:
        return _truthy(_ExprParser(tokens, resolver, text).parse())
    return _truthy(_ExprParser(tokens, resolver, text).parse())


# ── 变量赋值（post_cond 的 `set`）────────────────────────────────
_ASSIGN_RE = re.compile(r"^\s*\{\{\s*(?P<target>[^{}]+?)\s*\}\}\s*=\s*(?P<value>.*)$", re.DOTALL)


@dataclass
class Assignment:
    scope: str
    path: str
    raw_value: Any


def parse_assignment(statement: str) -> Assignment:
    """解析 `{{local.x}} = {{response.json.y}}`（01 §3.1 / 06 §2.4）。"""
    m = _ASSIGN_RE.match(statement or "")
    if not m:
        raise TemplateError(f"非法赋值语句（应形如 {{{{local.x}}}} = {{{{response.json.y}}}}）：{statement!r}")
    target = m.group("target").strip()
    scope, _, rest = target.partition(".")
    scope = SCOPE_ALIASES.get(scope.strip(), scope.strip())
    if scope not in SCOPE_PRIORITY:
        raise UndefinedVariableError("{{%s}}" % target, scope=scope)
    if not rest:
        raise TemplateError(f"赋值目标缺少变量名：{statement!r}")
    return Assignment(scope=scope, path=rest, raw_value=m.group("value").strip())


def apply_assignment(statement: str, resolver: VariableResolver) -> tuple[str, Any]:
    """执行一条 `set` 赋值，写回作用域，返回 (写入路径, 值)。"""
    asg = parse_assignment(statement)
    if asg.scope not in ("local", "auth", "global"):
        raise TemplateError(f"只允许写入 local / auth / global 作用域，收到 {asg.scope}")
    value = resolver.interpolate(asg.raw_value) if asg.raw_value != "" else None
    _assign_nested(resolver.get_scope(asg.scope), asg.path, value)
    return f"{asg.scope}.{asg.path}", value


async def aapply_assignment(statement: str, resolver: VariableResolver) -> tuple[str, Any]:
    """`apply_assignment` 的异步版——右侧支持 `{{Class.method(...)}}` 调用占位符。"""
    asg = parse_assignment(statement)
    if asg.scope not in ("local", "auth", "global"):
        raise TemplateError(f"只允许写入 local / auth / global 作用域，收到 {asg.scope}")
    value = await resolver.interpolate_async(asg.raw_value) if asg.raw_value != "" else None
    _assign_nested(resolver.get_scope(asg.scope), asg.path, value)
    return f"{asg.scope}.{asg.path}", value


def _assign_nested(scope: dict[str, Any], path: str, value: Any) -> None:
    """支持 `order_id` 与 `order.id` 两种写入路径。"""
    parts = path.split(".")
    cur: Any = scope
    for name in parts[:-1]:
        nxt = cur.get(name)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[name] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ── 步骤参数求值 ────────────────────────────────────────────────
def _as_resolver(ctx: Any) -> VariableResolver:
    """把 ctx / resolver / 普通 dict 统一成 VariableResolver。"""
    if isinstance(ctx, VariableResolver):
        return ctx
    resolver = getattr(ctx, "resolver", None)
    if isinstance(resolver, VariableResolver):
        return resolver
    if isinstance(ctx, Mapping):
        return VariableResolver(ctx)
    raise TemplateError(f"无法从 {type(ctx).__name__} 取得变量作用域（需要 ctx.resolver 或作用域 dict）")


def interpolate(template: Any, ctx: Any) -> Any:
    """模板替换（06 §2.2）：`interpolate("{{env.base_url}}/api", ctx)`。

    - `ctx` 可为 `ExecutionContext` / `VariableResolver` / 作用域 dict
    - 整串单占位符 → 保持原类型；混合串 → 拼接字符串；未定义 → 报错
    """
    return _as_resolver(ctx).interpolate(template)


def interpolate_params(params: Any, ctx: Any) -> Any:
    """递归解析步骤参数里的 `{{}}`（含 dict/list 嵌套）。"""
    return _as_resolver(ctx).interpolate(params)


async def ainterpolate_params(params: Any, ctx: Any) -> Any:
    """`interpolate_params` 的异步版——支持 `{{Class.method(a: local.x)}}` 调用占位符。

    执行器解析步骤 params / 断言参数时用它（方法通道由 ``run.execute`` 注入到
    ``resolver.method_caller``；未注入时调用占位符报可读错误，普通取值照常）。
    """
    return await _as_resolver(ctx).interpolate_async(params)


# ── 执行期上下文 ────────────────────────────────────────────────
@dataclass
class ExecutionContext:
    """一次场景执行期的共享上下文。

    - `resolver`：变量作用域链（local/data/auth/env/global + secrets）
    - `response` / `result`：当前步骤输出（{{response.*}} / {{result.*}}）
    - `now_ms()`：供内联代码/方法脚本使用
    """

    resolver: VariableResolver = field(default_factory=VariableResolver)
    response: Any = None
    result: Any = None
    current_step_key: Optional[str] = None
    current_step: Optional[dict] = None
    started_at_ms: int = field(default_factory=lambda: int(__import__("time").time() * 1000))
    # 结果落库附带信息（04 §1.2）——不参与变量解析
    case_refs: list = field(default_factory=list)
    dataset_row: Optional[dict] = None
    exec_mode: str = "script_only"
    #: AI/agent 通道（ai_judge / agent_action / agent_workflow 步骤用）。
    #: 引擎自身不耦合 LLM——通道由 ``run.execute`` 注入（与 ``result_writer`` 同模式）。
    #: 缺省 None：命中 AI 步骤时报可读错误，提示执行时必须带 api_key_id + model。
    ai: Any = None
    #: 方法脚本通道（script 步骤用，``ctx.methods.call(ref, args)``）。
    #: 与 ``ai`` 同款鸭子能力：引擎不 import method_channel，由 run.execute 注入。
    #: 缺省 None：命中 script 步骤时报可读错误，提示执行时须能加载方法库。
    methods: Any = None
    #: 引用场景执行器（``scenario_ref`` 步骤用）——``await ctx.scenario_runner(ref_id)``
    #: 递归跑子场景、共享 ctx（local 变量直传），返回 ScenarioResult。
    #: 由 ``ScenarioExecutor._execute_scenario`` 在每个非控制步骤前绑定（深度+栈同前
    #: 置场景机制，cycle guard 与 ``max_pre_scenario_depth`` 共用）。缺省 None：
    #: 命中 scenario_ref 步骤时报可读错误。
    scenario_runner: Any = None

    # -- 作用域快捷属性 --
    @property
    def local(self) -> dict[str, Any]:
        return self.resolver.scopes["local"]

    @property
    def data(self) -> dict[str, Any]:
        return self.resolver.scopes["data"]

    @property
    def auth(self) -> dict[str, Any]:
        return self.resolver.scopes["auth"]

    @property
    def env(self) -> dict[str, Any]:
        return self.resolver.scopes["env"]

    @property
    def global_vars(self) -> dict[str, Any]:
        return self.resolver.scopes["global"]

    def set_step_output(self, response: Any = None, result: Any = None) -> None:
        """写入当前步骤输出（{{response.*}} / {{result.*}}）。"""
        self.response = response
        self.result = result

    def now_ms(self) -> int:
        return int(__import__("time").time() * 1000)

    def redactor(self) -> "Redactor":
        return self.resolver.redactor()

    def register_secrets(self, mapping: Optional[Mapping[str, Any]]) -> None:
        """批量登记敏感变量（env 的 is_secret=true 变量）。"""
        if not mapping:
            return
        for key, value in mapping.items():
            self.resolver.register_secret(key, value)


def scope_report(resolver: VariableResolver) -> dict[str, list[str]]:
    """各作用域已有变量名（调试/报告用，值不外泄）。"""
    return {name: sorted(values.keys()) for name, values in resolver.scopes.items()}


__all__ = [
    "SCOPE_PRIORITY",
    "EPHEMERAL_SCOPES",
    "SCOPE_ALIASES",
    "KNOWN_SCOPES",
    "MISSING",
    "VariableResolver",
    "ExecutionContext",
    "Redactor",
    "MASK",
    "merge_env_chain",
    "eval_expression",
    "tokenize_expression",
    "split_template",
    "is_single_placeholder",
    "parse_assignment",
    "apply_assignment",
    "aapply_assignment",
    "interpolate",
    "interpolate_params",
    "ainterpolate_params",
    "scope_report",
]
