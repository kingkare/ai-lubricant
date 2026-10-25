"""Workflow engine: definition validation, variable resolution, DAG execution.

A workflow is a **saved, versioned, agent-independent graph**: it belongs to a
user (not an agent), declares its own inputs with typed sources, and draws a
standard workflow graph — typed steps connected by edges, with conditional
routing living on the **edges** (gateway semantics), not on the nodes.

Design commitments, each load-bearing:

- **Agent-independent.** A workflow is owned by ``user_id``; nothing is
  inherited from an agent binding. Every step names its own resources: the
  browser client_ids it may drive, the MCP service+method it calls, the agent
  a ``run_session`` step launches. Executors resolve authorization from the
  run's owner identity, not from any agent.
- **Standard workflow graph.** ``nodes`` + ``edges``. An edge may carry a
  ``when`` predicate; a node runs when **at least one** incoming edge is
  active (its source succeeded and the predicate holds). Routing is a
  property of the transition — the same node can be reached from a yes and a
  no branch — which the old per-node ``when``/``depends_on`` model could not
  express. Node-level ``when``/``depends_on`` are rejected at save time.
- **Declarative, statically validated.** The whole graph is checked before a
  run starts — unique ids, acyclic edges, declared inputs, and every ``$ref``
  resolving to an *ancestor* node's declared ``output_schema`` field. A graph
  that references a variable it cannot see is rejected at save time.
- **Typed steps.** Seven step kinds — ``code_run`` / ``file_read`` /
  ``file_write`` / ``web`` / ``run_mcp`` / ``run_session`` / ``cache``. The
  engine treats each node's extra fields as opaque params (deep-resolved
  against the context) and delegates to an executor; it only knows the kind's
  *minimum* required params for static validation.
- **Variables are validated outputs.** ``$node.field`` is legal only because
  ``node`` declared ``output_schema`` containing ``field``, and the node's
  actual output passed that schema at run time.
- **Failures localize.** A failed node deactivates its outgoing edges; an
  unrelated branch keeps running. A ``when`` that cannot be evaluated is
  simply not taken — it never throws and never takes down the graph.

Inputs have **sources**: ``custom`` is a static value (overridable per run
via ``args``), ``account_list`` resolves at run start to the credential
account list matching a URL — account metadata only, never passwords.

Launch methods (``triggers``) declare how a workflow is *started* — ``manual``
(the UI run button), ``cron`` (scheduled; each trigger carries its own cron
expression and args), ``agent`` (launched from ``workflow.run``). Each trigger's
``args`` become the run's base ``$args.*``; an explicitly-passed args object
overrides them key by key. The same graph thus behaves differently per launch
method (a nightly full sweep vs an ad-hoc probe) without duplicating it.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from loguru import logger

# Step kinds. Each kind maps to one executor; the engine only knows the
# minimum required params (for static validation) and treats the rest as an
# opaque, context-resolved param bag.
NODE_KINDS = ("code_run", "file_read", "file_write", "web", "run_mcp", "run_session", "cache", "http", "select", "foreach")
# The only cache keys a workflow may touch start with this prefix (the remark
# in the editor states it; the store layer enforces it again at run time).
CACHE_KEY_PREFIX = "wf:"
# Input declaration types (入参类型): how the run panel renders the field and
# how the typed value round-trips. The last four carry **resource semantics** —
# the value is not free-form data but a pick from a platform-managed list:
# ``model`` = a gateway model name; ``account`` = credential username(s) for the
# declared ``site_url``; ``cdp_client`` = a connected browser's client_id;
# ``proxy`` = a proxy-pool entry id (outbound routing for an ``http`` step).
INPUT_TYPES = ("string", "number", "boolean", "json", "array",
               "model", "account", "cdp_client", "proxy")
# Resource-typed inputs (type carries what the value selects, not its JSON shape).
_RESOURCE_INPUT_TYPES = ("model", "account", "cdp_client", "proxy")
# Launch-method kinds (启动方式): only ``cron`` is user-declared. ``manual``
# (the UI run button) and ``agent`` (launched via workflow.run) are implicit —
# always available, never stored, drawn as fixed nodes above「开始」.
TRIGGER_KINDS = ("cron",)
# Trigger param sources: how a trigger supplies the run's inputs.
# ``agent`` = the launching agent passes them (``value`` is a fallback for the
# unattended cron fire); ``custom`` = a static JSON value merged into $args;
# ``account_list`` = resolve the credential account list matching a URL
# (metadata only, never passwords).
TRIGGER_PARAM_SOURCES = ("agent", "custom", "account_list")
# When the 结束 notification fires (通知时机). Failure retry is configured
# separately (failure.agent_id), so notification timing lives only here now.
NOTIFY_WHEN = ("success", "failed", "always")# Cap on a notification template (chars) — a runaway template is a bug, not data.
MAX_NOTIFY_TEMPLATE_CHARS = 2000
# Node fields the engine owns; everything else on a node is that kind's params.
_ENGINE_FIELDS = frozenset({"id", "name", "kind", "foreach", "output_schema", "position", "note", "description"})
# Default cap on concurrent nodes in one topological layer.
DEFAULT_MAX_PARALLEL = 8
# Bounded output size per node stored in the run record (chars); large payloads
# are the caller's job to spill, the engine only bounds what it persists.
MAX_NODE_OUTPUT_CHARS = 20000


class WorkflowDefError(ValueError):
    """A workflow definition failed static validation."""


# ──────────────────────────────────────────────────────────────────────────────
# Reference grammar: $args.x | $input.x | $node.field | $item.field | $node
# ──────────────────────────────────────────────────────────────────────────────

_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)")
# Template interpolation: {...} where ... is a $ref (allows a bare ref in a
# string, e.g. "account {$item.account}").
_TEMPLATE_RE = re.compile(r"\{(\$[^{}]+)\}")

# Reference roots that are not node ids.
_VAR_ROOTS = ("args", "input", "item")


@dataclass
class RefPath:
    """A parsed ``$root.a.b`` reference."""
    root: str
    path: list[str]

    @property
    def text(self) -> str:
        return "$" + self.root + "".join(f".{p}" for p in self.path)


def parse_ref(expr: str) -> RefPath:
    m = _REF_RE.fullmatch(expr.strip())
    if not m:
        raise WorkflowDefError(f"not a reference: {expr!r}")
    path = [p for p in m.group(2).split(".") if p]
    return RefPath(root=m.group(1), path=path)


def find_refs(text: str) -> list[RefPath]:
    """Every ``$ref`` appearing in ``text`` (bare refs and template slots)."""
    out: list[RefPath] = []
    if not isinstance(text, str):
        return out
    for m in _REF_RE.finditer(text):
        path = [p for p in m.group(2).split(".") if p]
        out.append(RefPath(root=m.group(1), path=path))
    return out


@dataclass
class WorkflowContext:
    """Run-time variable scope: declared inputs, launch overrides, and each
    finished node's output."""
    inputs: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Current foreach element (set while expanding a foreach node).
    item: Any = None

    def resolve(self, ref: RefPath) -> Any:
        """Resolve a reference, raising :class:`WorkflowDefError` on a bad path."""
        if ref.root == "args":
            cur: Any = self.args
        elif ref.root == "input":
            cur = self.inputs
        elif ref.root == "item":
            cur = self.item
        else:
            entry = self.nodes.get(ref.root)
            if entry is None:
                raise WorkflowDefError(f"unknown node in reference: {ref.text}")
            if entry.get("status") != "ok":
                raise WorkflowDefError(f"node {ref.root!r} did not succeed ({entry.get('status')})")
            cur = entry.get("output")
        for seg in ref.path:
            if isinstance(cur, dict):
                if seg not in cur:
                    raise WorkflowDefError(f"{ref.text}: missing field {seg!r}")
                cur = cur[seg]
            elif isinstance(cur, list):
                try:
                    cur = cur[int(seg)]
                except (ValueError, IndexError):
                    raise WorkflowDefError(f"{ref.text}: bad list index {seg!r}") from None
            else:
                raise WorkflowDefError(f"{ref.text}: cannot descend into {type(cur).__name__}")
        return cur

    def resolve_str(self, expr: str) -> Any:
        """Resolve a value that is either a bare ``$ref`` or a template string."""
        expr = expr.strip()
        m = _REF_RE.fullmatch(expr)
        if m:
            return self.resolve(parse_ref(expr))
        return interpolate(expr, self)

    def snapshot(self) -> dict[str, Any]:
        return {"inputs": self.inputs, "args": self.args, "nodes": self.nodes}


def interpolate(template: str, ctx: WorkflowContext) -> str:
    """Substitute ``{$ref}`` slots in a string; a non-string passes through."""
    if not isinstance(template, str):
        return template

    def sub(m: re.Match) -> str:
        value = ctx.resolve(parse_ref(m.group(1)))
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return "" if value is None else str(value)

    return _TEMPLATE_RE.sub(sub, template)


def resolve_value(value: Any, ctx: WorkflowContext) -> Any:
    """Deeply resolve a node's parameter value (dicts/lists/strings with refs)."""
    if isinstance(value, dict):
        return {k: resolve_value(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, ctx) for v in value]
    if isinstance(value, str) and ("$" in value):
        return ctx.resolve_str(value)
    return value


# ──────────────────────────────────────────────────────────────────────────────
# Predicate grammar: a small closed set — no arbitrary expressions.
#   $x == 'ok' | $x != 1 | $x in ['a','b'] | $x >= 3 | exists($x)
#   not <pred> | <pred> and <pred> | <pred> or <pred>
# ──────────────────────────────────────────────────────────────────────────────

# Longest-first so ">=" is tried before ">" (a bare ">" would split ">=" wrongly).
_OPS = ("==", "!=", ">=", "<=", ">", "<")


def _split_top(expr: str, sep: str) -> list[str]:
    """Split on ``sep`` at the top level (not inside quotes/brackets)."""
    out: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    start = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and expr.startswith(sep, i):
            out.append(expr[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    out.append(expr[start:])  # remainder after the last separator
    return out


def _eval_operand(token: str, ctx: WorkflowContext) -> Any:
    s = token.strip()
    if not s:
        raise WorkflowDefError("empty operand in predicate")
    if len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]:
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_eval_operand(part, ctx) for part in _split_top(inner, ",")]
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    if s.startswith("$"):
        return ctx.resolve(parse_ref(s))
    return s  # bare word → string


def eval_predicate(expr: str, ctx: WorkflowContext) -> bool:
    """Evaluate an edge ``when`` predicate. Raises :class:`WorkflowDefError` on a
    malformed predicate or an unresolvable reference — the CALLER decides how to
    treat that (the engine treats it as "not taken", never fatal)."""
    if not isinstance(expr, str) or not expr.strip():
        return True
    e = expr.strip()

    for sep in (" or ", " and "):
        parts = _split_top(e, sep)
        if len(parts) > 1:
            results = [eval_predicate(p, ctx) for p in parts]
            return any(results) if sep == " or " else all(results)

    if e.lower().startswith("not "):
        return not eval_predicate(e[4:], ctx)

    if e.lower().startswith("exists(") and e.endswith(")"):
        try:
            ctx.resolve(parse_ref(e[len("exists("):-1].strip()))
            return True
        except WorkflowDefError:
            return False

    for op in _OPS:
        parts = _split_top(e, op)
        if len(parts) == 2:
            left = _eval_operand(parts[0], ctx)
            right = _eval_operand(parts[1], ctx)
            if op == "==":
                return left == right
            if op == "!=":
                return left != right
            try:
                if op == ">=":
                    return left >= right
                if op == "<=":
                    return left <= right
                if op == ">":
                    return left > right
                if op == "<":
                    return left < right
            except TypeError as exc:
                raise WorkflowDefError(f"predicate type error in {e!r}: {exc}") from None

    parts = _split_top(e, " in ")
    if len(parts) == 2:
        left = _eval_operand(parts[0], ctx)
        right = _eval_operand(parts[1], ctx)
        try:
            return left in right
        except TypeError as exc:
            raise WorkflowDefError(f"predicate type error in {e!r}: {exc}") from None

    raise WorkflowDefError(f"unsupported predicate: {e!r}")


# ──────────────────────────────────────────────────────────────────────────────
# Static validation
# ──────────────────────────────────────────────────────────────────────────────

_KIND_MIN_PARAMS: dict[str, tuple[str, ...]] = {
    "code_run": ("code",),
    "file_read": ("path",),
    "file_write": ("path", "content"),
    "web": ("operation",),
    "run_mcp": ("service", "method"),
    "run_session": ("sop",),
    "cache": ("action", "key"),
    "http": ("url",),
    "select": (),
    "foreach": ("items",),
}

# A web step driving `fill_credential` names its credential kind via
# `credential_kind` — the natural field name `kind` is owned by the engine
# (node type) and is stripped from the param bag, so it can never reach the
# MCP call. Validated here so a misnamed step fails at save time, not mid-run.
_CREDENTIAL_KIND_FIELD = "credential_kind"

_CACHE_ACTIONS = ("get", "set", "delete")
# http 步骤允许的方法（大写）。
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")


def _param_text(node: dict) -> str:
    """The node's param bag as JSON text — every string field may carry refs."""
    body = {k: v for k, v in node.items() if k not in ("position",)}
    return json.dumps(body, ensure_ascii=False)


def _schema_path_ok(schema: Any, path: list[str]) -> bool:
    """Whether ``path`` is addressable in a (JSON-schema subset) output_schema."""
    cur = schema
    for seg in path:
        if not isinstance(cur, dict):
            return False
        if cur.get("type") == "array" or "items" in cur:
            cur = cur.get("items") or {}
        props = cur.get("properties")
        if not isinstance(props, dict) or seg not in props:
            # additionalProperties true → we cannot disprove; accept.
            if cur.get("additionalProperties") is True or cur.get("type") not in ("object", None):
                return True
            return False
        cur = props[seg]
    return True


def validate_inputs(defn: dict) -> dict[str, dict]:
    """Validate the declared inputs table; return it keyed by input key.

    ``inputs`` is optional (absent/empty = no declared inputs, ``$input.*`` is
    then illegal). Each entry is ``{key, type?, note?, options?, default?,
    required?}`` — a **declaration** of what the workflow expects (the 入参说明
    the 开始 node edits). ``type`` drives how the run panel renders the field and
    how the value round-trips (a string the user typed stays a string, a number
    stays a number).

    The three **属性** fields shape the run panel and the run-time contract:
    - ``options`` — a non-empty list of allowed scalar values; the run panel
      renders a dropdown (「选择项」). The value must be one of them.
    - ``default`` — the value the run panel pre-fills (user may override). Must
      be compatible with ``type`` and, when ``options`` is set, one of them.
    - ``required`` — the run must supply a value; a run that leaves it blank
      (and has no default) fails up front.

    How an input is *supplied* lives on the trigger (``triggers[].params``),
    not here: the same workflow can take its accounts from a cron sweep or a
    manual override without duplicating the declaration.
    """
    raw = defn.get("inputs")
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise WorkflowDefError("inputs must be an array")
    out: dict[str, dict] = {}
    for spec in raw:
        if not isinstance(spec, dict):
            raise WorkflowDefError("each input must be an object")
        key = str(spec.get("key") or "").strip()
        if not key or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise WorkflowDefError(f"input key must be an identifier: {key!r}")
        if key in out:
            raise WorkflowDefError(f"duplicate input key: {key}")
        if key in _VAR_ROOTS:
            raise WorkflowDefError(f"input key {key!r} collides with a reserved reference root")
        if spec.get("source") is not None:
            raise WorkflowDefError(
                f"input {key!r}: 'source' no longer exists here; how an input is supplied "
                f"is configured on each 启动方式 (triggers[].params)"
            )
        type_ = str(spec.get("type") or "string").strip().lower()
        if type_ not in INPUT_TYPES:
            raise WorkflowDefError(f"input {key!r}: unknown type {type_!r} (expected one of {INPUT_TYPES})")

        # options（可选项）：给了就必须是非空的标量列表（下拉用）。json/array
        # 不适合做下拉，直接拒——避免把对象塞进 option value。
        options = spec.get("options")
        norm_options: list[Any] = []
        if options is not None:
            if not isinstance(options, list) or not options:
                raise WorkflowDefError(f"input {key!r}: 'options' must be a non-empty array")
            if type_ in ("json", "array"):
                raise WorkflowDefError(f"input {key!r}: 'options' cannot be used with type {type_!r}")
            for o in options:
                if o is None or isinstance(o, (dict, list)):
                    raise WorkflowDefError(f"input {key!r}: each option must be a scalar (string/number/boolean)")
                norm_options.append(o)
            # 去重（按字符串化口径，1 与 "1" 视为同一项）
            seen: set[str] = set()
            deduped: list[Any] = []
            for o in norm_options:
                k = str(o)
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(o)
            norm_options = deduped

        entry: dict[str, Any] = {"key": key, "type": type_, "note": str(spec.get("note") or "")}

        # account 类型绑站点：账号列表按这个 url 的域名解析（同一图两个账号入参可各指不同站点）。
        if type_ == "account":
            site = str(spec.get("site_url") or "").strip()
            if site:
                entry["site_url"] = site

        # default（默认值）：按声明类型矫正；有 options 时必须命中其一。
        # 资源型（model/account/cdp_client）的 options 由平台在运行时提供，声明里
        # 一般不给；给了（人工钉选的子集）才做命中校验。
        if "default" in spec and spec.get("default") is not None:
            try:
                dv = _coerce_one(key, type_, spec.get("default"))
            except WorkflowDefError:
                raise
            if norm_options and type_ not in _RESOURCE_INPUT_TYPES \
                    and not any(_same_scalar(dv, o) for o in norm_options):
                raise WorkflowDefError(
                    f"input {key!r}: default {spec.get('default')!r} is not one of options {norm_options}"
                )
            entry["default"] = dv

        if norm_options:
            entry["options"] = norm_options
        if spec.get("required") is not None:
            entry["required"] = bool(spec.get("required"))

        out[key] = entry
    return out


def _same_scalar(a: Any, b: Any) -> bool:
    """两个标量是否同一项（1 == "1"、True == "true" 视为同一，供 options 比对）。"""
    if a == b:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).strip().lower() == str(b).strip().lower()
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _coerce_one(key: str, type_: str, raw: Any) -> Any:
    """按声明类型矫正单个值（default 校验与 coerce_inputs 共用同一套规则）。"""
    if raw is None:
        return None
    if type_ == "string":
        return raw if isinstance(raw, str) else str(raw)
    if type_ == "number":
        if isinstance(raw, bool):
            raise WorkflowDefError(f"input {key!r}: expected a number, got a boolean")
        if isinstance(raw, (int, float)):
            return raw
        try:
            s = str(raw).strip()
            return int(s) if re.fullmatch(r"[+-]?\d+", s) else float(s)
        except ValueError:
            raise WorkflowDefError(f"input {key!r}: expected a number, got {raw!r}") from None
    if type_ == "boolean":
        if isinstance(raw, bool):
            return raw
        low = str(raw).strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise WorkflowDefError(f"input {key!r}: expected a boolean, got {raw!r}")
    if type_ == "json":
        return raw
    if type_ == "array":
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                raise WorkflowDefError(f"input {key!r}: expected a JSON array") from None
            if not isinstance(parsed, list):
                raise WorkflowDefError(f"input {key!r}: expected a JSON array")
            return parsed
        raise WorkflowDefError(f"input {key!r}: expected a JSON array, got {type(raw).__name__}")
    # 资源型入参：值是「从平台列表里挑的东西」，落到标量/标量列表形态。
    if type_ in ("model", "cdp_client", "proxy"):
        # 模型名 / client_id / proxy_config_id 都是字符串；dict 形态（options 项）取其 id/name。
        if isinstance(raw, dict):
            return str(raw.get("id") or raw.get("model_name") or raw.get("client_id")
                       or raw.get("name") or "")
        return raw if isinstance(raw, str) else str(raw)
    if type_ == "account":
        # 单值（fixed/random/扇出每遍）= username 字符串；all = username 列表。
        # 账号对象形态（{username,...}）就地取 username，与运行弹框传的结构兼容。
        def _name_of(item: Any) -> str:
            if isinstance(item, dict):
                return str(item.get("username") or item.get("name") or "")
            return item if isinstance(item, str) else str(item)
        if isinstance(raw, list):
            return [_name_of(x) for x in raw]
        if isinstance(raw, str):
            # 运行弹框可能把整张列表塞成一个 JSON 串——能解成数组就解。
            stripped = raw.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except ValueError:
                    return raw
                if isinstance(parsed, list):
                    return [_name_of(x) for x in parsed]
            return raw
        return _name_of(raw)
    return raw


def coerce_inputs(defn: dict, values: dict) -> dict:
    """Coerce ``values`` against the declared inputs table, key by key.

    The declaration's ``type`` is the contract: a ``number`` reaching the engine
    as a string (cron JSON, an Agent passing ``"7"``) becomes an int/float, a
    ``boolean`` becomes a real bool. Undeclared keys pass through untouched —
    ``$args`` carries extra args — and missing keys are simply absent so the
    launch method's own default still applies.

    Raises :class:`WorkflowDefError` when a value genuinely can't be read as its
    declared type (``"abc"`` for a number); that is a caller error, not a
    default-worthy blank.
    """
    table = validate_inputs(defn)
    out: dict[str, Any] = {}
    for key, raw in values.items():
        spec = table.get(key)
        if spec is None:
            out[key] = raw
            continue
        t = spec.get("type") or "string"
        if raw is None:
            # 显式 null 就是调用方给的值，原样保留（丢掉会让 $input.<key> 变成
            # "missing field"，那是引用错误而不是意图）。
            out[key] = None
            continue
        coerced = _coerce_one(key, t, raw)
        opts = spec.get("options")
        if opts and not any(_same_scalar(coerced, o) for o in opts):
            raise WorkflowDefError(
                f"input {key!r}: value {raw!r} is not one of options {opts}"
            )
        out[key] = coerced
    return out


def apply_input_defaults(defn: dict, values: dict) -> dict:
    """Fill declared ``default`` values for keys the caller left out, then enforce
    ``required``. Returns a new dict (the caller's ``values`` is not mutated).

    Runs after :func:`coerce_inputs` at run-start: a blank optional input with a
    default becomes that default; a blank ``required`` input with no default
    raises so the run fails up front with a clear message (rather than an
    in-graph "missing field" deep in the DAG). Explicitly-passed values — even
    falsy ones like ``0``/``""``/``False`` — are left as the caller set them.
    """
    table = validate_inputs(defn)
    out = dict(values)
    missing_required: list[str] = []
    for key, spec in table.items():
        if key in out and out[key] is not None:
            continue
        if "default" in spec:
            out[key] = spec["default"]
        elif spec.get("required"):
            missing_required.append(key)
    if missing_required:
        raise WorkflowDefError(
            f"missing required input(s): {', '.join(missing_required)}"
        )
    return out


def validate_triggers(defn: dict) -> list[dict]:
    """Validate the launch-method (启动方式) declarations; return them normalized.

    Only **cron** triggers are declared — ``manual`` (UI 运行按钮) and ``agent``
    (workflow.run) are implicit and always available. Each cron trigger carries
    its own ``params``: how the run's inputs are supplied.

    Each entry: ``{id, kind: "cron", label?, cron, enabled?, params?}``
    - ``cron`` required
    - ``params``: ``{source: "agent"|"custom"|"account_list", ...}``
      - ``account_list`` → ``url`` (whose site's accounts are resolved at fire
        time; metadata only, never passwords) + ``multiple`` (true = 整张图按账号
        扇出 N 遍，每条 run 的 ``$input.accounts`` = 单个账号；false = 一条 run，
        ``$input.accounts`` = 数组)
      - ``custom`` → ``value`` (a JSON object merged into ``$args``; an
        explicitly-passed args still overrides key by key)
    """
    raw = defn.get("triggers")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkflowDefError("triggers must be an array")
    out: list[dict] = []
    seen: set[str] = set()
    for i, spec in enumerate(raw):
        if not isinstance(spec, dict):
            raise WorkflowDefError(f"trigger #{i} must be an object")
        tid = str(spec.get("id") or "").strip()
        if not tid or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tid):
            raise WorkflowDefError(f"trigger id must be an identifier: {tid!r}")
        if tid in seen:
            raise WorkflowDefError(f"duplicate trigger id: {tid}")
        seen.add(tid)
        kind = str(spec.get("kind") or "cron").strip().lower()
        if kind not in TRIGGER_KINDS:
            raise WorkflowDefError(
                f"trigger {tid!r}: unknown kind {kind!r} (only {TRIGGER_KINDS} is declared — "
                f"manual/agent 启动是隐式的，不用配置)"
            )
        cron = str(spec.get("cron") or "").strip()
        if not cron:
            raise WorkflowDefError(f"trigger {tid!r}: needs a cron expression")
        # params：该启动方式怎么供入参。
        params = spec.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise WorkflowDefError(f"trigger {tid!r}: params must be an object")
        source = str(params.get("source") or "custom").strip().lower()
        if source not in TRIGGER_PARAM_SOURCES:
            raise WorkflowDefError(
                f"trigger {tid!r}: params.source must be one of {TRIGGER_PARAM_SOURCES}"
            )
        norm_params: dict[str, Any] = {"source": source}
        if source == "account_list":
            url = str(params.get("url") or "").strip()
            if not url:
                raise WorkflowDefError(f"trigger {tid!r}: params.source=account_list needs a url")
            norm_params["url"] = url
            norm_params["multiple"] = bool(params.get("multiple"))
        else:
            # custom / agent：静态值（object）。agent 来源时它只是定时触发（无 agent）
            # 的兜底——真被 Agent 启动时 Agent 传的 args 覆盖。
            value = params.get("value")
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise WorkflowDefError(f"trigger {tid!r}: params.value must be an object")
            norm_params["value"] = value
        out.append({
            "id": tid,
            "kind": kind,
            "label": str(spec.get("label") or ""),
            "cron": cron,
            "enabled": spec.get("enabled") is not False,
            "params": norm_params,
        })
    return out


def validate_end_failure(defn: dict) -> tuple[dict, dict]:
    """Validate the 结束 (end) terminal config — success/failure handling merged.

    ``end`` — the notification: ``{notify_template?, notify_channels?,
    notify_when?, notify_params?}``. Notification reuses the platform's channels
    (``mc_notify_channels``); ``notify_when`` decides when it fires —
    ``success`` (ok/partial only) | ``failed`` (failed only) | ``always``
    (default: both). The template is the message body (supports the
    ``{$workflow.*}``/``{$run.*}``/``{$failed.*}`` placeholders).

    ``failure`` — the failure handling, merged into the end panel: only
    ``{agent_id?}``. A non-null ``agent_id`` = hand the failure context to that
    agent, which investigates and decides whether to re-run (the workflow does
    not loop). Null/absent = no retry. The old ``action`` field is rejected —
    notification timing lives in ``notify_when`` now.
    """
    end = defn.get("end")
    if end is None:
        end = {}
    if not isinstance(end, dict):
        raise WorkflowDefError("end must be an object")
    channels = end.get("notify_channels")
    if channels is None:
        channels = []
    if not isinstance(channels, list) or any(not isinstance(c, str) or not c.strip() for c in channels):
        raise WorkflowDefError("end.notify_channels must be an array of channel ids (strings)")
    params = end.get("notify_params")
    if params is not None and not isinstance(params, dict):
        raise WorkflowDefError("end.notify_params must be an object")
    template = str(end.get("notify_template") or "")
    if len(template) > MAX_NOTIFY_TEMPLATE_CHARS:
        raise WorkflowDefError(f"end.notify_template too long (max {MAX_NOTIFY_TEMPLATE_CHARS} chars)")
    notify_when = str(end.get("notify_when") or "always").strip().lower()
    if notify_when not in NOTIFY_WHEN:
        raise WorkflowDefError(f"end.notify_when must be one of {NOTIFY_WHEN}")
    end_norm = {
        "notify_template": template,
        "notify_channels": [str(c).strip() for c in channels if str(c).strip()],
        "notify_when": notify_when,
        "notify_params": params if isinstance(params, dict) else {},
    }

    failure = defn.get("failure")
    if failure is None:
        failure = {}
    if not isinstance(failure, dict):
        raise WorkflowDefError("failure must be an object")
    if failure.get("action") is not None:
        raise WorkflowDefError(
            "failure.action no longer exists: whether to notify is decided by "
            "end.notify_when (成功/失败/都通知), and the failure panel only configures "
            "the retry agent (failure.agent_id)"
        )
    agent_id = failure.get("agent_id")
    if agent_id is not None:
        try:
            agent_id = int(agent_id)
        except (TypeError, ValueError):
            raise WorkflowDefError("failure.agent_id must be a number (or null = 不重试)") from None
    failure_norm = {"agent_id": agent_id}
    return end_norm, failure_norm


def _edges_of(defn: dict, by_id: dict[str, dict]) -> list[dict]:
    """Normalized edge list: ``[{source, target, when}]`` with defaults filled."""
    raw = defn.get("edges")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkflowDefError("edges must be an array")
    edges: list[dict] = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            raise WorkflowDefError(f"edge #{i} must be an object")
        source = str(e.get("source") or "").strip()
        target = str(e.get("target") or "").strip()
        if source not in by_id:
            raise WorkflowDefError(f"edge #{i}: source {source!r} is not a node")
        if target not in by_id:
            raise WorkflowDefError(f"edge #{i}: target {target!r} is not a node")
        if source == target:
            raise WorkflowDefError(f"edge #{i}: cannot point a node at itself ({source})")
        when = str(e.get("when") or "").strip() or ""
        edges.append({"source": source, "target": target, "when": when})
    return edges


def _ancestors(node_id: str, incoming: dict[str, list[str]]) -> set[str]:
    """Transitive source closure of ``node_id`` along incoming edges."""
    seen: set[str] = set()
    stack = list(incoming.get(node_id, []))
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(incoming.get(nid, []))
    return seen


def validate_def(defn: dict, *, allow_empty: bool = False) -> None:
    """Full static validation of a workflow definition. Raises WorkflowDefError.

    ``allow_empty=True`` lets a node-less shell through — the create-form path
    ("先建壳、再进详情页填节点图") saves ``{nodes: []}`` before any node exists.
    Update and run never pass it, so an empty graph can never be saved-as-real
    or executed; only the initial draft shell is tolerated.
    """
    if not isinstance(defn, dict):
        raise WorkflowDefError("definition must be an object")
    inputs = validate_inputs(defn)
    validate_triggers(defn)
    validate_end_failure(defn)

    nodes = defn.get("nodes")
    if not isinstance(nodes, list):
        raise WorkflowDefError("definition must have a 'nodes' array")
    if not nodes:
        if allow_empty:
            return
        raise WorkflowDefError("definition must have a non-empty 'nodes' array")

    by_id: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise WorkflowDefError("each node must be an object")
        nid = str(node.get("id") or "").strip()
        if not nid:
            raise WorkflowDefError("every node needs a non-empty 'id'")
        if nid in by_id:
            raise WorkflowDefError(f"duplicate node id: {nid}")
        if nid in _VAR_ROOTS:
            raise WorkflowDefError(f"node id {nid!r} collides with a reserved reference root")
        # 步骤名（中文，必填）：流程图与节点一览都靠它给人看；id 只是机器键。
        if not str(node.get("name") or "").strip():
            raise WorkflowDefError(f"node {nid}: needs a non-empty 'name' (步骤中文名)")
        kind = str(node.get("kind") or "").strip().lower()
        if kind not in NODE_KINDS:
            raise WorkflowDefError(f"node {nid}: unknown kind {kind!r} (expected one of {NODE_KINDS})")
        # Routing moved to edges — a node carrying the old fields is a stale graph.
        if node.get("depends_on") is not None:
            raise WorkflowDefError(f"node {nid}: 'depends_on' no longer exists; routing lives on edges")
        if node.get("when") is not None:
            raise WorkflowDefError(f"node {nid}: 'when' no longer exists; put the condition on the edge")
        by_id[nid] = node

    edges = _edges_of(defn, by_id)
    incoming: dict[str, list[str]] = {}
    for e in edges:
        incoming.setdefault(e["target"], []).append(e["source"])
    _check_acyclic(incoming, set(by_id))

    # Kind-specific minimum params.
    for nid, node in by_id.items():
        kind = node["kind"]
        for name in _KIND_MIN_PARAMS[kind]:
            if not str(node.get(name) or "").strip():
                raise WorkflowDefError(f"node {nid}: kind={kind} needs a non-empty {name!r}")
        if kind == "cache":
            action = str(node.get("action") or "").strip().lower()
            if action not in _CACHE_ACTIONS:
                raise WorkflowDefError(f"node {nid}: cache action must be one of {_CACHE_ACTIONS}")
            key = str(node.get("key") or "").strip()
            if not key.startswith(CACHE_KEY_PREFIX):
                raise WorkflowDefError(
                    f"node {nid}: cache key must start with the fixed prefix {CACHE_KEY_PREFIX!r} "
                    f"(got {key!r})"
                )
        if kind == "run_session":
            agent = node.get("agent_id")
            if agent not in (None, "", "any") and not str(agent).strip().lstrip("-").isdigit():
                raise WorkflowDefError(f"node {nid}: run_session agent_id must be a number, 'any', or empty")
        if kind == "http":
            method = str(node.get("method") or "GET").strip().upper()
            if method not in _HTTP_METHODS:
                raise WorkflowDefError(f"node {nid}: http method must be one of {list(_HTTP_METHODS)}")
        if kind == "select":
            cases = node.get("cases")
            if cases is None:
                cases = []
            if not isinstance(cases, list):
                raise WorkflowDefError(f"node {nid}: select 'cases' must be an array")
            for i, case in enumerate(cases):
                if not isinstance(case, dict):
                    raise WorkflowDefError(f"node {nid}: select case #{i} must be an object")
                when = case.get("when")
                if when is not None and not isinstance(when, str):
                    raise WorkflowDefError(f"node {nid}: select case #{i} 'when' must be a string")
                if not isinstance(case.get("set"), dict) or not case.get("set"):
                    raise WorkflowDefError(f"node {nid}: select case #{i} needs a non-empty 'set' object")
            if cases and node.get("default") is None:
                raise WorkflowDefError(
                    f"node {nid}: select with cases needs a 'default' "
                    "(no case matched would otherwise yield nothing downstream)"
                )
            if node.get("default") is not None and not isinstance(node.get("default"), dict):
                raise WorkflowDefError(f"node {nid}: select 'default' must be an object")
        if kind == "web":
            op = str(node.get("operation") or "").strip().lower()
            # `fill_credential` needs a credential kind, but its natural field
            # name ``kind`` is the engine's own node-type field (stripped from
            # the param bag), so a workflow uses ``credential_kind`` instead.
            # Validate at save time — a misnamed step used to pass the UI and
            # only fail at run time with a confusing MCP TypeError.
            if op == "fill_credential":
                ck = str(node.get(_CREDENTIAL_KIND_FIELD) or "").strip().lower()
                if ck not in ("otp", "password"):
                    raise WorkflowDefError(
                        f"node {nid}: web operation {op!r} needs "
                        f"{_CREDENTIAL_KIND_FIELD!r} = 'otp' | 'password' "
                        "(the field name 'kind' is the node type and cannot carry it)"
                    )

    # Reference validation: every ref must resolve to a declared input, an
    # ancestor node (or args/item).
    for nid, node in by_id.items():
        anc = _ancestors(nid, incoming)
        for ref in find_refs(_param_text(node)):
            if ref.root == "item":
                if not _in_foreach_scope(nid, incoming, by_id):
                    raise WorkflowDefError(f"node {nid}: uses $item but is not inside a foreach")
                continue
            if ref.root == "args":
                continue
            if ref.root == "input":
                if ref.path and ref.path[0] not in inputs:
                    raise WorkflowDefError(f"node {nid}: reference {ref.text} names an undeclared input")
                continue
            if ref.root not in by_id:
                raise WorkflowDefError(f"node {nid}: reference {ref.text} names unknown node {ref.root!r}")
            if ref.root not in anc:
                raise WorkflowDefError(
                    f"node {nid}: reference {ref.text} is not an ancestor "
                    f"(no edge path leads from {ref.root!r})"
                )
            # Schema-path check where the target declared a schema.
            target_schema = by_id[ref.root].get("output_schema")
            if target_schema and ref.path and not _schema_path_ok(target_schema, ref.path):
                raise WorkflowDefError(
                    f"node {nid}: reference {ref.text} — field not in {ref.root!r}.output_schema"
                )

    # Edge ``when`` predicates: refs must resolve against the source's world.
    for i, e in enumerate(edges):
        if not e["when"]:
            continue
        anc = _ancestors(e["source"], incoming) | {e["source"]}
        for ref in find_refs(e["when"]):
            if ref.root in ("args", "item", "input"):
                if ref.root == "input" and ref.path and ref.path[0] not in inputs:
                    raise WorkflowDefError(f"edge #{i}: reference {ref.text} names an undeclared input")
                continue
            if ref.root not in by_id:
                raise WorkflowDefError(f"edge #{i}: reference {ref.text} names unknown node {ref.root!r}")
            if ref.root not in anc:
                raise WorkflowDefError(
                    f"edge #{i}: condition may only reference {e['source']!r} or its ancestors"
                )


def _in_foreach_scope(node_id: str, incoming: dict[str, list[str]], by_id: dict[str, dict]) -> bool:
    """True if any ancestor is a foreach-node (so $item is meaningful)."""
    for anc in _ancestors(node_id, incoming):
        if str(by_id[anc].get("kind") or "").lower() == "foreach" or by_id[anc].get("foreach"):
            return True
    return False


def _check_acyclic(incoming: dict[str, list[str]], node_ids: set[str]) -> None:
    """DFS over the edge graph; a back edge to a visiting node is a cycle."""
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(nid: str) -> None:
        s = state.get(nid)
        if s == 1:
            return
        if s == 0:
            raise WorkflowDefError(f"cycle detected involving node {nid!r}")
        state[nid] = 0
        for src in incoming.get(nid, []):
            visit(src)
        state[nid] = 1

    for nid in node_ids:
        visit(nid)


def topo_layers(defn: dict) -> list[list[str]]:
    """Node ids grouped into dependency layers (each layer may run in parallel)."""
    by_id = {n["id"]: n for n in defn["nodes"]}
    incoming: dict[str, list[str]] = {}
    for e in _edges_of(defn, by_id):
        incoming.setdefault(e["target"], []).append(e["source"])
    done: set[str] = set()
    layers: list[list[str]] = []
    remaining = set(by_id)
    while remaining:
        layer = sorted(nid for nid in remaining if all(d in done for d in incoming.get(nid, [])))
        if not layer:
            raise WorkflowDefError("dependency cycle prevents scheduling")
        layers.append(layer)
        done.update(layer)
        remaining.difference_update(layer)
    return layers


def node_params(node: dict) -> dict[str, Any]:
    """A node's executor param bag: everything the engine does not own.

    ``select``'s ``when`` predicates are stripped here: they carry bare ``$item``
    refs in *predicate* grammar (``$item.x == 'y'``), which ``resolve_value``
    would try to resolve as a string template and choke on. ``run_select`` reads
    them from the raw node instead; only the ``set``/``default`` values are
    interpolated (so those may carry ``{$ref}`` slots).
    """
    params = {k: v for k, v in node.items() if k not in _ENGINE_FIELDS}
    if str(node.get("kind") or "") == "select" and isinstance(params.get("cases"), list):
        params["cases"] = [
            {k: v for k, v in (c or {}).items() if k != "when"} for c in params["cases"]
        ]
    return params


# ──────────────────────────────────────────────────────────────────────────────
# Node executors (dependency-injected)
# ──────────────────────────────────────────────────────────────────────────────

class NodeExecutors(Protocol):
    """The heavy steps the engine delegates. One method per step kind.
    Implementations must be best-effort about their own transport and raise on
    genuine failure."""

    async def run_code_run(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_file_read(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_file_write(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_web(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_mcp(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_session(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_cache(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_http(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...
    async def run_select(self, node: dict, ctx: WorkflowContext, params: dict) -> Any: ...


@dataclass
class NodeResult:
    node_id: str
    status: str  # ok | failed | skipped
    output: Any = None
    error: str = ""
    decision: str = ""  # which edge/foreach path was taken, for replay
    duration_ms: int = 0


def _bound_output(value: Any) -> Any:
    """Bound what a node persists: drop oversized blobs to a size note."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= MAX_NODE_OUTPUT_CHARS:
        return value
    return {
        "_truncated": True,
        "_chars": len(text),
        "preview": text[:MAX_NODE_OUTPUT_CHARS],
    }


class WorkflowRunner:
    """Executes a validated definition against a context, one layer at a time."""

    def __init__(
        self,
        defn: dict,
        *,
        executors: NodeExecutors,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        on_node_done: Optional[Callable[[NodeResult], Awaitable[None]]] = None,
    ) -> None:
        validate_def(defn)
        self.defn = defn
        self.by_id = {n["id"]: n for n in defn["nodes"]}
        self.edges = _edges_of(defn, self.by_id)
        self.incoming: dict[str, list[dict]] = {}
        for e in self.edges:
            self.incoming.setdefault(e["target"], []).append(e)
        self.executors = executors
        self.max_parallel = max(1, int(max_parallel))
        self.on_node_done = on_node_done
        self.results: dict[str, NodeResult] = {}
        # 出边邻接表：遍历（foreach）节点 = 它 + 它的全部下游，按元素各跑一遍。
        self.outgoing: dict[str, list[str]] = {}
        for e in self.edges:
            self.outgoing.setdefault(e["source"], []).append(e["target"])

    def _descendants(self, nid: str) -> set[str]:
        """nid 的全部下游（沿出边可达，不含自身）。遍历体 = foreach 头 + 其后代。"""
        seen: set[str] = set()
        stack = list(self.outgoing.get(nid, []))
        while stack:
            child = stack.pop()
            if child in seen:
                continue
            seen.add(child)
            stack.extend(self.outgoing.get(child, []))
        return seen

    def _loop_body_nodes(self) -> set[str]:
        """所有「在某个顶层 foreach 节点的下游里」的节点。

        顶层调度跳过它们（由 foreach 节点自己逐元素驱动），避免无 $item 地跑第二遍。
        顶层 = 不被其它 foreach 节点包含的 foreach 节点。
        """
        headers = [nid for nid, n in self.by_id.items()
                   if str(n.get("kind") or "").lower() == "foreach"]
        top = [h for h in headers if not any(h in self._descendants(o) for o in headers if o != h)]
        body: set[str] = set()
        for h in top:
            body |= self._descendants(h)
        return body

    async def run(self, ctx: WorkflowContext) -> dict[str, Any]:
        """Run every node; return ``{status, nodes, context}``.

        ``status`` is ``ok`` when all nodes succeeded, ``partial`` when at least
        one failed/was skipped but the run completed, ``failed`` when every
        reachable node failed.

        Loop bodies (a foreach node's downstream) are NOT scheduled here — the
        foreach node's run drives its whole subtree once per element.
        """
        body_nodes = self._loop_body_nodes()
        for layer in topo_layers(self.defn):
            sem = asyncio.Semaphore(self.max_parallel)

            async def _one(nid: str) -> None:
                if nid in body_nodes:
                    return  # 由 foreach 节点逐元素驱动，不在此独立调度
                async with sem:
                    res = await self._run_node(nid, ctx)
                self.results[nid] = res
                # Publish the node's output into the shared context so downstream
                # nodes can resolve ``$nid...`` references. Written AFTER the node
                # completes so a sibling never reads a half-built value; a foreach
                # node writes its per-item collected list.
                ctx.nodes[nid] = {"status": res.status, "output": res.output}
                if self.on_node_done:
                    try:
                        await self.on_node_done(res)
                    except Exception:  # noqa: BLE001 - logging must not break the run
                        logger.debug("[workflow] on_node_done failed for %s", nid, exc_info=True)

            await asyncio.gather(*[_one(nid) for nid in layer])

        statuses = {r.status for r in self.results.values()}
        if statuses <= {"ok"}:
            overall = "ok"
        elif statuses == {"failed", "skipped"}:
            overall = "failed"
        else:
            overall = "partial"
        return {
            "status": overall,
            "nodes": {nid: _result_dict(r) for nid, r in self.results.items()},
            "context": ctx.snapshot(),
        }

    async def _run_node(self, nid: str, ctx: WorkflowContext) -> NodeResult:
        import time
        node = self.by_id[nid]
        started = time.monotonic()

        # Gate 1: edge activation. A node with incoming edges runs when at
        # least one edge is active (source ok + predicate holds); a node with
        # no incoming edges is a root and always runs. A predicate that cannot
        # be evaluated is simply not taken (never fatal).
        # 依赖状态读 ctx.nodes（循环体内它带本元素的产出），回落 self.results。
        incoming = self.incoming.get(nid) or []
        if incoming:
            active = False
            reasons: list[str] = []
            for e in incoming:
                dep_res = self.results.get(e["source"])
                dep_status = (ctx.nodes.get(e["source"]) or {}).get("status") or (
                    dep_res.status if dep_res else None)
                if dep_status != "ok":
                    reasons.append(f"{e['source']} not ok")
                    continue
                if e["when"]:
                    try:
                        taken = eval_predicate(e["when"], ctx)
                    except WorkflowDefError as exc:
                        logger.warning("[workflow] edge %s->%s when %r not evaluable: %s",
                                       e["source"], nid, e["when"], exc)
                        taken = False
                    if not taken:
                        reasons.append(f"when {e['when']!r} = false")
                        continue
                active = True
                break
            if not active:
                return NodeResult(nid, "skipped", decision="; ".join(reasons) or "no active edge",
                                  duration_ms=_ms(started))

        # 遍历节点：它 + 它的全部下游，按 items 各跑一遍。
        if str(node.get("kind") or "").lower() == "foreach":
            return await self._run_loop(nid, node, ctx, started)

        try:
            output = await self._dispatch(node, ctx, item=ctx.item)
        except Exception as exc:  # noqa: BLE001 - a node failure is data, not a crash
            logger.warning("[workflow] node %s failed: %s", nid, exc)
            return NodeResult(nid, "failed", error=f"{type(exc).__name__}: {exc}",
                              duration_ms=_ms(started))
        return NodeResult(nid, "ok", output=_bound_output(output),
                          duration_ms=_ms(started))

    async def _run_loop(self, nid: str, node: dict, ctx: WorkflowContext, started: float) -> NodeResult:
        """遍历节点：把「它 + 全部下游」这个子图按 items 各跑一遍。

        ``$item`` 绑定当前元素，对体内所有节点可见（不只 foreach 头本身）。
        items 由节点的 ``items`` 表达式解析（``$fetch.list`` / ``$input.accounts``…）。
        循环结束后，体内每个节点的输出 = 各元素产出的**列表**——循环外用
        ``$bodynode`` 读到数组。嵌套遍历（体内再放一个 foreach 节点）自然递归。
        """
        try:
            items = ctx.resolve_str(str(node["items"]))
        except WorkflowDefError as exc:
            return NodeResult(nid, "failed", error=f"foreach resolve: {exc}", duration_ms=_ms(started))
        # 标量（只挑了一个账号的 account 入参 = username 字符串、单个对象）就当
        # 单元素遍历——「遍历一个参数」本含此意，报「不是列表」只会让人以为坏了。
        if items is None:
            return NodeResult(nid, "failed", error="foreach target resolved to nothing",
                              duration_ms=_ms(started))
        if not isinstance(items, list):
            items = [items]

        body_nodes = {nid} | self._descendants(nid)
        sub = {"nodes": [self.by_id[n] for n in body_nodes],
               "edges": [e for e in self.edges if e["source"] in body_nodes and e["target"] in body_nodes]}
        body_layers = topo_layers(sub)

        collected: dict[str, list[Any]] = {n: [] for n in body_nodes}
        collected_status: dict[str, list[str]] = {n: [] for n in body_nodes}
        # 体内节点的报错原文：折叠后只留第一条（+条数），否则循环里失败无从诊断。
        collected_errors: dict[str, list[str]] = {n: [] for n in body_nodes}
        sem = asyncio.Semaphore(self.max_parallel)

        async def _run_element(element: Any) -> None:
            # 本元素作用域：复制 pre-loop 的节点产出，避免跨元素串味。
            item_ctx = WorkflowContext(inputs=ctx.inputs, args=ctx.args,
                                      nodes=dict(ctx.nodes), item=element)
            for layer in body_layers:
                async with sem:
                    for ln in layer:
                        if ln == nid:
                            out, st = None, "ok"  # 头本身只解析 items，产出由循环汇总
                        else:
                            res = await self._run_node(ln, item_ctx)
                            out, st = res.output, res.status
                            if res.error:
                                collected_errors[ln].append(f"[{element}] {res.error}")
                        item_ctx.nodes[ln] = {"status": st, "output": out}
                        collected[ln].append(out)
                        collected_status[ln].append(st)

        await asyncio.gather(*[_run_element(el) for el in items])

        # 发布：体内节点（含头）输出 = 各元素列表；头由 run() 统一发布。
        header_res: NodeResult | None = None
        for n in body_nodes:
            oks = sum(1 for s in collected_status[n] if s == "ok")
            st = "ok" if oks == len(items) else ("failed" if oks == 0 else "ok")
            errs = collected_errors[n]
            res = NodeResult(
                n, st, output=_bound_output(collected[n]),
                error=(errs[0] + (f"（另有 {len(errs) - 1} 条同类错误）" if len(errs) > 1 else "")
                       if errs else ""),
                decision=f"foreach over {len(items)} items ({oks} ok)",
                duration_ms=_ms(started),
            )
            if n == nid:
                header_res = res
                continue  # 头的发布/回调交给 run()
            self.results[n] = res
            ctx.nodes[n] = {"status": st, "output": res.output}
            if self.on_node_done:
                try:
                    await self.on_node_done(res)
                except Exception:  # noqa: BLE001
                    logger.debug("[workflow] on_node_done failed for %s", n, exc_info=True)
        assert header_res is not None
        return header_res

    async def _dispatch(self, node: dict, ctx: WorkflowContext, item: Any) -> Any:
        """Resolve a node's params against the context and run its executor."""
        kind = node["kind"].lower()
        # A foreach element is scoped to this node only.
        scoped = WorkflowContext(inputs=ctx.inputs, args=ctx.args, nodes=ctx.nodes, item=item)
        params = resolve_value(node_params(node), scoped)
        ex = self.executors
        if kind == "code_run":
            out = await ex.run_code_run(node, scoped, params)
        elif kind == "file_read":
            out = await ex.run_file_read(node, scoped, params)
        elif kind == "file_write":
            out = await ex.run_file_write(node, scoped, params)
        elif kind == "web":
            out = await ex.run_web(node, scoped, params)
        elif kind == "run_mcp":
            out = await ex.run_mcp(node, scoped, params)
        elif kind == "run_session":
            out = await ex.run_session(node, scoped, params)
        elif kind == "cache":
            out = await ex.run_cache(node, scoped, params)
        elif kind == "http":
            out = await ex.run_http(node, scoped, params)
        elif kind == "select":
            out = await ex.run_select(node, scoped, params)
        else:  # pragma: no cover - validate_def rejects unknown kinds
            raise WorkflowDefError(f"unhandled node kind: {kind}")

        # Output schema validation (deterministic gate — the variable's birth check).
        schema = node.get("output_schema")
        if schema:
            errs = validate_output(out, schema)
            if errs:
                raise WorkflowDefError(f"output_schema violated: {'; '.join(errs)}")
        return out


def _ms(started: float) -> int:
    import time
    return int((time.monotonic() - started) * 1000)


def _result_dict(r: NodeResult) -> dict[str, Any]:
    return {
        "status": r.status,
        "output": r.output,
        "error": r.error,
        "decision": r.decision,
        "duration_ms": r.duration_ms,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Output schema validation (JSON-schema subset — the variable's birth check)
# ──────────────────────────────────────────────────────────────────────────────

def _type_ok(value: Any, declared: Any) -> bool:
    if isinstance(declared, list):
        return any(_type_ok(value, d) for d in declared)
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "null":
        return value is None
    return True


def validate_output(value: Any, schema: dict) -> list[str]:
    """Validate ``value`` against a JSON-schema subset; return error strings."""
    errs: list[str] = []
    _validate_into(value, schema, "$", errs)
    return errs


def _validate_into(value: Any, schema: Any, path: str, errs: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    declared = schema.get("type")
    if declared is not None and not _type_ok(value, declared):
        errs.append(f"{path}: expected {declared}, got {type(value).__name__}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []) or []:
            if req not in value:
                errs.append(f"{path}: missing required {req!r}")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in value:
                _validate_into(value[key], sub, f"{path}.{key}", errs)
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, el in enumerate(value):
                _validate_into(el, items, f"{path}[{i}]", errs)


__all__ = [
    "NODE_KINDS",
    "CACHE_KEY_PREFIX",
    "INPUT_TYPES",
    "coerce_inputs",
    "TRIGGER_KINDS",
    "TRIGGER_PARAM_SOURCES",
    "NOTIFY_WHEN",
    "WorkflowDefError",
    "WorkflowContext",
    "WorkflowRunner",
    "NodeExecutors",
    "NodeResult",
    "RefPath",
    "parse_ref",
    "find_refs",
    "interpolate",
    "resolve_value",
    "eval_predicate",
    "validate_def",
    "validate_inputs",
    "apply_input_defaults",
    "validate_triggers",
    "validate_end_failure",
    "validate_output",
    "topo_layers",
    "node_params",
]
