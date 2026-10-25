"""工具注册表 + 内置工具骨架（06 §3 / 01 §4.2）。

步骤按 `type` 分发到工具：

| 步骤 type | 工具 |
|---|---|
| `request` | `http` |
| `wait`    | `wait` |
| `check`   | `assert`（独立断言步） |
| `ai_judge` | `ai_judge`（网关 LLM 判定，经 `ctx.ai` 通道） |
| `agent_action` | `agent_action`（驱动平台 Agent 跑一轮） |
| `agent_workflow` | `agent_workflow`（开工作流运行） |
| `page`    | `browser` —— **不实现**，stub 报「未注册」 |
| `android` / `ios` | `device_control` —— **不实现**，stub |
| `custom`  | `tool_ref` 指向的自定义工具 —— **不实现**，stub |

工具用 `@register_tool` 装饰器注册；每个工具是
`async def run(step, ctx) -> ToolResult`（也可写同步，框架用 maybe_await 适配）。

`ToolResult` 统一承载：
- `response`：供 `{{response.*}}` 引用（http 返回 {status, headers, body}）
- `result`：供 `{{result.*}}` 引用（工具自报的额外产物）
- `evidence`：证据（04 §1.4，截图/日志/网络包等）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .aio import maybe_await
from .errors import StepParamsError, ToolExecutionError, ToolNotRegisteredError
from .variables import ExecutionContext, eval_expression

# ── 工具结果 ────────────────────────────────────────────────────
@dataclass
class ToolResult:
    """一次工具调用的产物。"""

    response: Any = None                     # → {{response.*}}
    result: Any = None                       # → {{result.*}}
    evidence: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @staticmethod
    def of(response: Any = None, result: Any = None, **meta: Any) -> "ToolResult":
        return ToolResult(response=response, result=result, meta=meta)


# ── 注册表 ──────────────────────────────────────────────────────
@dataclass
class Tool:
    name: str
    params_schema: dict
    run: Callable[[dict, ExecutionContext], Any]
    description: str = ""
    #: 该工具覆盖的步骤类型（step.type → tool）
    step_types: tuple[str, ...] = ()


class ToolRegistry:
    """工具注册表：`register_tool` 注册，`get(name)` 取，`run(name, step, ctx)` 执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: Optional[str] = None,
        *,
        params_schema: Optional[dict] = None,
        description: str = "",
        step_types: Optional[tuple[str, ...]] = None,
    ):
        """`@register_tool("http")` 装饰器。"""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or func.__name__
            doc = (func.__doc__ or "").strip()
            brief = doc.splitlines()[0] if doc else ""
            self._tools[tool_name] = Tool(
                name=tool_name,
                params_schema=params_schema or {},
                run=func,
                description=description or brief,
                step_types=tuple(step_types or ()),
            )
            return func

        return deco

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotRegisteredError(
                f"工具未注册：{name!r}（已注册：{self.names() or ['无']}）"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def for_step_type(self, step_type: str) -> Optional[Tool]:
        for tool in self._tools.values():
            if step_type in tool.step_types:
                return tool
        return None

    async def run(self, tool_name: str, step: dict, ctx: ExecutionContext) -> ToolResult:
        """执行工具（同步/异步统一适配），并把产物归一为 ToolResult。"""
        tool = self.get(tool_name)
        outcome = await maybe_await(tool.run(step, ctx))
        if outcome is None:
            return ToolResult()
        if isinstance(outcome, ToolResult):
            return outcome
        if isinstance(outcome, dict):
            if {"response", "result", "evidence", "meta"} & set(outcome):
                return ToolResult(
                    response=outcome.get("response"),
                    result=outcome.get("result"),
                    evidence=list(outcome.get("evidence") or []),
                    meta=dict(outcome.get("meta") or {}),
                )
            return ToolResult(response=outcome)
        return ToolResult(response=outcome)


# ── 进程默认注册表 ──────────────────────────────────────────────
# 注意：装饰器在模块导入期间就执行注册，因此这里只建**空**注册表，
# 内置工具在导入各 *Tool 函数时由装饰器逐个注册进来；
# `build_default_registry()` 另建全新注册表供测试隔离使用。
_DEFAULT_REGISTRY: Optional[ToolRegistry] = None


def default_registry() -> ToolRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ToolRegistry()
    return _DEFAULT_REGISTRY


def register_tool(
    name: Optional[str] = None,
    *,
    params_schema: Optional[dict] = None,
    description: str = "",
    step_types: Optional[tuple[str, ...]] = None,
):
    """模块级装饰器：注册工具到进程默认注册表。"""
    return default_registry().register(
        name, params_schema=params_schema, description=description, step_types=step_types
    )


# ── 内置工具：http ──────────────────────────────────────────────
@register_tool(
    "http",
    step_types=("request",),
    params_schema={
        "type": "object",
        "properties": {
            "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
            "url": {"type": "string"},
            "headers": {"type": "object"},
            "body": {},
            "params": {"type": "object"},
            "timeout": {"type": "number"},
            "transport": {"type": "object"},   # 传输层合并结果（common_headers / path_rewrite）
        },
        "required": ["method", "url"],
    },
)
async def http_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """HTTP 请求工具：method / url / headers / body → {status, headers, body}。

    `params` 已被执行器解析过变量；这里只做请求构造与发送。
    `httpx` 存在时真发请求；缺失时抛 ToolExecutionError（骨架不静默）。
    """
    params = step.get("params") or {}
    method = str(params.get("method", "GET")).upper()
    url = params.get("url")
    if not url:
        raise StepParamsError("http 步骤缺少 params.url")
    headers = dict(params.get("headers") or {})
    body = params.get("body")
    query = params.get("params")
    timeout = float(params.get("timeout", 30))

    # 传输层公共头（auth 注入后由执行器写入 ctx.auth，这里合并 common_headers）
    transport = params.get("transport") or {}
    for key, value in (transport.get("common_headers") or {}).items():
        headers.setdefault(key, value)
    if transport.get("path_rewrite") and isinstance(url, str):
        url = _apply_path_rewrite(url, transport["path_rewrite"])

    request = {
        "method": method,
        "url": url,
        "headers": headers,
        "params": query,
        "body": body,
        "timeout": timeout,
    }
    started = time.monotonic()
    status, resp_headers, resp_body = await _send_http(request)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    response = {
        "status": status,
        "headers": resp_headers,
        "body": resp_body,
        "elapsed_ms": elapsed_ms,
    }
    return ToolResult(
        response=response,
        result={"status": status, "elapsed_ms": elapsed_ms},
        evidence=[{"kind": "network", "meta": {**request, "status": status}}],
        meta={"elapsed_ms": elapsed_ms},
    )


def _apply_path_rewrite(url: str, rules: dict) -> str:
    for prefix, replacement in rules.items():
        if prefix in url:
            return url.replace(prefix, str(replacement), 1)
    return url


async def _send_http(request: dict) -> tuple[int, dict, Any]:
    """真正发送 HTTP。用 httpx（异步）；未安装则报错（不静默返回假数据）。"""
    try:
        import httpx  # 局部 import：只在真正发请求时依赖
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise ToolExecutionError(
            "http 工具需要 httpx（异步 HTTP 客户端），当前环境未安装",
            details={"url": request["url"]},
        ) from exc

    body = request.get("body")
    kwargs: dict[str, Any] = {
        "headers": request.get("headers") or None,
        "params": request.get("params") or None,
        "timeout": request.get("timeout", 30),
    }
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        kwargs["content"] = body if isinstance(body, (bytes, str)) else str(body)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(request["method"], request["url"], **kwargs)
    except Exception as exc:  # 网络异常统一包成 ToolExecutionError
        raise ToolExecutionError(f"http 请求失败：{exc}", details={"url": request["url"]}) from exc

    text = resp.text
    parsed: Any = text
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = text
    return resp.status_code, dict(resp.headers), parsed


# ── 内置工具：wait ──────────────────────────────────────────────
@register_tool(
    "wait",
    step_types=("wait",),
    params_schema={
        "type": "object",
        "properties": {
            "seconds": {"type": "number"},
            "until_cond": {"type": "string"},   # 轮询条件，满足即返回
            "interval": {"type": "number"},
            "timeout": {"type": "number"},
        },
    },
)
async def wait_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """等待工具：`seconds` 固定等待；`until_cond` 轮询条件（默认 0.5s 间隔，超时 30s）。"""
    params = step.get("params") or {}
    seconds = params.get("seconds")
    until = params.get("until_cond") or params.get("until")
    started = time.monotonic()

    if until:
        interval = float(params.get("interval", 0.5))
        timeout = float(params.get("timeout", 30))
        last_error: Optional[Exception] = None
        while True:
            try:
                if eval_expression(until, ctx.resolver):
                    elapsed = int((time.monotonic() - started) * 1000)
                    return ToolResult.of(result={"waited_ms": elapsed, "satisfied": True})
            except Exception as exc:  # 条件暂不可求值 → 继续等（变量可能尚未产出）
                last_error = exc
            if time.monotonic() - started >= timeout:
                raise ToolExecutionError(
                    f"wait 条件超时（{timeout}s 未满足）：{until!r}",
                    details={"until": until, "last_error": str(last_error) if last_error else None},
                )
            await asyncio.sleep(interval)

    delay = float(seconds if seconds is not None else 0)
    if delay < 0:
        raise StepParamsError(f"wait.seconds 不能为负：{delay}")
    await asyncio.sleep(delay)
    elapsed = int((time.monotonic() - started) * 1000)
    return ToolResult.of(result={"waited_ms": elapsed, "satisfied": True})


# ── 内置工具：assert（独立断言步）───────────────────────────────
@register_tool(
    "assert",
    step_types=("check",),
    params_schema={
        "type": "object",
        "properties": {
            "checks": {"type": "array"},          # 断言列表（与 post_checks 同构）
            "value": {},                          # 被断言的实际值（可选，缺省用当前 response）
        },
    },
)
async def assert_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """独立断言步：对 `params.value`（缺省取当前 response）跑 `params.checks`。

    断言执行与失败上抛交给执行器（执行器持有断言注册表与 CheckError 语义），
    这里只把「被断言的实际值」整理好返回，并声明 `defer_checks=True`。
    """
    params = step.get("params") or {}
    actual = params.get("value", ctx.response)
    checks = params.get("checks") or step.get("post_checks") or []
    if not checks:
        raise StepParamsError("check 步骤必须提供 params.checks（或步骤级 post_checks）")
    return ToolResult(
        response=actual,
        result={"checks": checks},
        meta={"defer_checks": True, "actual": actual, "checks": checks},
    )


# ── 方法脚本工具（script 步骤 → ctx.methods 通道，06 §6.2）───────
def _require_methods(ctx: ExecutionContext):
    methods = getattr(ctx, "methods", None)
    if methods is None:
        raise ToolExecutionError(
            "该步骤是方法脚本步骤（script），但本次执行未注入方法通道——"
            "run.execute 需从方法库加载类源码并注入 ctx.methods。"
        )
    return methods


@register_tool(
    "script",
    step_types=("script",),
    params_schema={
        "type": "object",
        "properties": {
            "method_ref": {"type": "string", "description": "方法引用 <类名>.<静态方法>（如 Crypto.sign）"},
            "args": {"type": "object", "description": "与静态方法形参对齐的入参（{{变量}} 已解析）"},
        },
    },
)
async def script_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """执行方法脚本：``method_ref`` 定位类的静态方法 → 用 ``args`` 调用 → 返回值进 ``{{result.*}}``。

    能力经 ``ctx.methods`` 注入（与 ``ctx.ai`` 同范式）——引擎不 import 方法库/DB，
    只见鸭子协议。``method_ref`` 可在步骤顶层或 ``params`` 里；``args`` 未被执行器
    插值（执行器只插 ``params``），这里补一次异步插值（args 里可再含调用占位符）。
    """
    from .variables import ainterpolate_params

    params = step.get("params") or {}
    method_ref = step.get("method_ref") or params.get("method_ref")
    if not method_ref:
        raise StepParamsError("script 步骤缺少 method_ref（06 §6.2：method_ref 指向方法脚本）")
    methods = _require_methods(ctx)
    raw_args = step.get("args") or params.get("args") or {}
    args = await ainterpolate_params(dict(raw_args), ctx.resolver)
    outcome = await methods.call(str(method_ref), args)
    # 方法返回值是本步骤核心产物：同时进 {{response.*}}（json_path 断言默认作用面）
    # 与 {{result.*}}（06 §6.2 约定），两边一致省得记差异。
    return ToolResult(response=outcome, result=outcome)


# ── 未实现步骤类型的 stub ───────────────────────────────────────
#: 明确「不实现」的步骤类型 → 工具名（调用即报未注册，不静默降级）
#: 流程控制步骤类型（01 §4.2 扩展：判断/跳出/结束场景）。
#: **由执行器主循环直接解释**（区域栈 if/else_if/else/end_if + break/end_scenario），
#: 不分发工具、不需要后置校验——命中与否决定后续步骤是否执行。
CONTROL_FLOW_TYPES: frozenset[str] = frozenset(
    {"if", "else_if", "else", "end_if", "break", "end_scenario"}
)

#: 不要求后置校验（R5）的步骤类型：流程控制（执行器解释，不跑工具）+ wait
#: （只等时间/轮询，产出无意义，断言它没意义）。其余步骤一律要求 ≥1 断言。
NO_CHECK_STEP_TYPES: frozenset[str] = CONTROL_FLOW_TYPES | frozenset({"wait"})

UNIMPLEMENTED_STEP_TYPES: dict[str, str] = {
    "page": "browser",
    "mobile": "device_control",
    "android": "device_control",
    "ios": "device_control",
    "custom": "__custom_tool__",
    "sql": "db",
    "ws": "ws",
}


def resolve_tool_for_step(step: dict, registry: Optional[ToolRegistry] = None) -> str:
    """把步骤解析为工具名。

    优先级：
    1. 步骤显式给了 `tool` 字段（design §19.1 的交换格式）且该工具已注册 → 直接用它
    2. 步骤 `type` 映射到工具（01 §4.2）
    3. 未实现/未注册的类型 → 返回其工具名，调用时统一报「未注册」

    `custom` 类型取 `params.tool_ref`；`script` 类型分发到 ``script`` 工具
    （方法脚本经 ``ctx.methods`` 通道执行，06 §6.2——执行期由 run.execute 注入能力）。
    """
    registry = registry or default_registry()
    # 流程控制步骤由执行器主循环直接解释，不分发工具（执行器已在调用前短路，
    # 这里兜底给出可读错误，防止误把控制流当普通步骤丢进工具注册表）。
    if step.get("type") in CONTROL_FLOW_TYPES:
        from .errors import StepParamsError

        raise StepParamsError(
            f"流程控制步骤 {step.get('type')!r} 由执行器解释，不应分发到工具"
        )
    explicit_tool = step.get("tool")
    if explicit_tool and registry.has(str(explicit_tool)):
        return str(explicit_tool)

    step_type = step.get("type")
    if step_type == "custom":
        tool_ref = (step.get("params") or {}).get("tool_ref") or step.get("tool_ref")
        if not tool_ref:
            raise StepParamsError("custom 步骤缺少 params.tool_ref")
        return tool_ref
    tool = registry.for_step_type(step_type)
    if tool is not None:
        return tool.name
    # 未注册 → 若属于已知未实现类型，返回其工具名以给出更明确的报错
    return UNIMPLEMENTED_STEP_TYPES.get(step_type, str(step_type))


# ── AI / agent 步骤工具（真执行，经 ctx.ai 通道）─────────────────
#: 引擎不耦合 LLM：通道由 run.execute 注入 ExecutionContext.ai（见 engine/ai.py）。
#: 命中而 ctx.ai 为空 → 报可读错误（提示执行需带 api_key_id + model），不假装通过。


def _require_ai(ctx: ExecutionContext):
    ai = getattr(ctx, "ai", None)
    if ai is None:
        raise ToolExecutionError(
            "该步骤是 AI/agent 步骤，但本次执行未注入 AI 通道——"
            "run.execute 需带 api_key_id（+ 可选 model）才能执行 ai_judge/agent_action/agent_workflow。"
        )
    return ai


@register_tool(
    "ai_judge",
    step_types=("ai_judge",),
    params_schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "判断提示词（可含 {{}}，执行器已解析）"},
            "expected": {"description": "期望结论（可空）"},
            "context_refs": {"description": "上下文引用（可空）"},
        },
        "required": ["prompt"],
    },
)
async def ai_judge_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """AI 判断步骤：交给网关 LLM 判定，返回 {passed, reason}。

    判定结果作为步骤 actual，由步骤的 post_checks 机械裁定（AI 不直接判 pass/fail）。
    """
    ai = _require_ai(ctx)
    params = step.get("params") or {}
    prompt = params.get("prompt")
    if not prompt:
        raise StepParamsError("ai_judge 步骤缺少 params.prompt")
    verdict = await ai.judge(
        str(prompt), expected=params.get("expected"), context=params.get("context_refs"),
    )
    return ToolResult(
        response=verdict,
        result=verdict,
        evidence=[{
            "kind": "ai_judge",
            "meta": {
                "prompt": str(prompt)[:500],
                "verdict": verdict.get("verdict"),   # 四态：passed/failed/inconclusive/blocked
                "passed": verdict.get("passed"),     # 旧位兼容
                "evidence_class": verdict.get("evidence_class"),
            },
        }],
        meta={"ai_judge": True, "verdict": verdict.get("verdict")},
    )


@register_tool(
    "agent_action",
    step_types=("agent_action",),
    params_schema={
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "给 Agent 的指令"},
            "agent_id": {"type": "integer", "description": "平台 Agent id（可空，缺省用默认）"},
        },
        "required": ["instruction"],
    },
)
async def agent_action_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """Agent 操作步骤：驱动一个平台 Agent 跑一轮任务，返回其产出。"""
    ai = _require_ai(ctx)
    params = step.get("params") or {}
    instruction = params.get("instruction")
    if not instruction:
        raise StepParamsError("agent_action 步骤缺少 params.instruction")
    agent_id = params.get("agent_id")
    try:
        agent_id = int(agent_id) if agent_id not in (None, "") else None
    except (TypeError, ValueError):
        raise StepParamsError(f"agent_action.agent_id 非法：{agent_id!r}")
    outcome = await ai.act(str(instruction), agent_id=agent_id)
    return ToolResult(
        response=outcome,
        result=outcome,
        evidence=[{"kind": "agent_action", "meta": {"instruction": str(instruction)[:500]}}],
        meta={"agent_action": True},
    )


@register_tool(
    "agent_workflow",
    step_types=("agent_workflow",),
    params_schema={
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string", "description": "工作流定义 UUID 或 role 名（planner/coder/...）"},
            "input_vars": {"type": "object", "description": "工作流输入（原样落 input）"},
        },
        "required": ["workflow_id"],
    },
)
async def agent_workflow_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """Agent 工作流步骤：开一条工作流运行，返回 run_id/status。"""
    ai = _require_ai(ctx)
    params = step.get("params") or {}
    workflow_id = params.get("workflow_id")
    if not workflow_id:
        raise StepParamsError("agent_workflow 步骤缺少 params.workflow_id")
    outcome = await ai.run_workflow(str(workflow_id), params.get("input_vars"))
    return ToolResult(
        response=outcome,
        result=outcome,
        evidence=[{"kind": "agent_workflow", "meta": {"workflow_id": str(workflow_id)}}],
        meta={"agent_workflow": True},
    )


@register_tool(
    "scenario_ref",
    step_types=("scenario_ref",),
    params_schema={
        "type": "object",
        "properties": {
            "scenario_ref": {"type": "string", "description": "被引用场景的 UUID 或 code（SCN-xxxx）"},
        },
        "required": ["scenario_ref"],
    },
)
async def scenario_ref_tool(step: dict, ctx: ExecutionContext) -> ToolResult:
    """引用场景步骤：递归跑一个子场景、共享 ctx（local 直传），返回子场景结果摘要。

    子场景与前置场景（``flow.pre_scenarios``）同机制——``ctx`` 全程共享，local 变量
    在父/子之间直传；cycle guard 与 ``max_pre_scenario_depth`` 共用。执行通道
    ``ctx.scenario_runner`` 由 ``ScenarioExecutor._execute_scenario`` 逐步骤绑定
    （每个非控制步骤前刷新当前层的 depth/stack，避免子场景闭合后栈错位）。
    子场景结果写进 ``{{response.status}}`` / ``{{response.summary}}`` 供后置校验
    断言（如 ``json_path`` ``status`` == ``pass``）。
    """
    runner = getattr(ctx, "scenario_runner", None)
    if runner is None:
        raise ToolExecutionError(
            "引用场景步骤需要执行器注入 scenario_runner 通道——"
            "（顶层 run.execute 已注入；独立调试执行器须 ScenarioExecutor 自身触发）"
        )
    params = step.get("params") or {}
    ref = params.get("scenario_ref")
    if not ref:
        raise StepParamsError("scenario_ref 步骤缺少 params.scenario_ref")
    sub = await runner(ref)
    summary = {
        "status": getattr(sub, "status", None),
        "summary": getattr(sub, "summary", None),
        "scenario": str(ref),
        "early_exit": getattr(sub, "early_exit", None),
    }
    return ToolResult(
        response=summary,
        result=summary,
        evidence=[{"kind": "scenario_ref", "meta": summary}],
        meta={"scenario_ref": True},
    )


def build_default_registry() -> ToolRegistry:
    """内置工具注册表（http / wait / assert + AI/agent 三类）。"""
    registry = ToolRegistry()
    registry.add(
        Tool(
            name="http",
            params_schema={},
            run=http_tool,
            description="HTTP 请求",
            step_types=("request",),
        )
    )
    registry.add(
        Tool(
            name="wait",
            params_schema={},
            run=wait_tool,
            description="等待",
            step_types=("wait",),
        )
    )
    registry.add(
        Tool(
            name="assert",
            params_schema={},
            run=assert_tool,
            description="独立断言步",
            step_types=("check",),
        )
    )
    registry.add(
        Tool(
            name="ai_judge",
            params_schema={},
            run=ai_judge_tool,
            description="AI 判断（网关 LLM 判定，经 ctx.ai 通道）",
            step_types=("ai_judge",),
        )
    )
    registry.add(
        Tool(
            name="agent_action",
            params_schema={},
            run=agent_action_tool,
            description="Agent 操作（驱动平台 Agent 跑一轮任务）",
            step_types=("agent_action",),
        )
    )
    registry.add(
        Tool(
            name="agent_workflow",
            params_schema={},
            run=agent_workflow_tool,
            description="Agent 工作流（开工作流运行）",
            step_types=("agent_workflow",),
        )
    )
    registry.add(
        Tool(
            name="scenario_ref",
            params_schema={},
            run=scenario_ref_tool,
            description="引用场景（递归跑子场景，共享 ctx，local 直传）",
            step_types=("scenario_ref",),
        )
    )
    registry.add(
        Tool(
            name="script",
            params_schema={},
            run=script_tool,
            description="方法脚本（加载类的静态方法，经 ctx.methods 通道执行，06 §6.2）",
            step_types=("script",),
        )
    )
    return registry


__all__ = [
    "ToolResult",
    "Tool",
    "ToolRegistry",
    "default_registry",
    "register_tool",
    "build_default_registry",
    "http_tool",
    "wait_tool",
    "assert_tool",
    "ai_judge_tool",
    "agent_action_tool",
    "agent_workflow_tool",
    "script_tool",
    "resolve_tool_for_step",
    "UNIMPLEMENTED_STEP_TYPES",
    "CONTROL_FLOW_TYPES",
    "NO_CHECK_STEP_TYPES",
]
