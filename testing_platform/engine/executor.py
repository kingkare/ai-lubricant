"""场景执行器（06 §3 步骤执行 / 04 §2 执行流程）。

引擎是**纯确定性**的：读场景 → 逐步骤执行（调工具）→ 跑后置校验 → 错误策略
→ 写结果。**不调 LLM、不接 agent**（06 §7）——AI 只能改场景，改完重过校验门 + 重跑。

主循环（04 §2）::

    for scn in scenarios:
        ctx = {**ctx0, local: scn.local_vars}
        for pre in scn.flow.pre_scenarios:  execute(pre, ctx)      # 前置场景，local 传递
        for step in scn.steps (order by seq):
            # 流程控制步骤（if/else_if/else/end_if/break/end_scenario）走区域栈解释，
            # 落在未命中分支里的普通步骤直接 skip（01 §4.2 扩展）
            if not eval_conditions(step, ctx): continue
            await step.run(ctx)                 # 类型化执行（工具分发）
            await run_post_checks(step, ctx)    # 后置校验（类型化）
            apply_post_cond(step, ctx)          # 写局部变量
            if early_exit_hit(ctx): break
        write run_scenario(...)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from .aio import maybe_await
from .assertions import AssertionRegistry, AssertionResult, default_registry as default_assertion_registry
from .errors import (
    CheckError,
    Decision,
    EngineError,
    ErrorPolicy,
    ErrorPolicyState,
    ScenarioNotFoundError,
    StepParamsError,
)
from .tools import (
    CONTROL_FLOW_TYPES,
    NO_CHECK_STEP_TYPES,
    ToolRegistry,
    ToolResult,
    default_registry as default_tool_registry,
    resolve_tool_for_step,
)
from .variables import (
    ExecutionContext,
    ainterpolate_params,
    aapply_assignment,
    apply_assignment,
    eval_expression,
    merge_env_chain,
)

# 场景状态（04 §1.2）
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"
STATUS_FLAKY = "flaky"
STATUS_SKIPPED = "skipped"

# 步骤状态（04 §1.3）
STEP_PASS = "pass"
STEP_FAIL = "fail"
STEP_ERROR = "error"
STEP_SKIPPED = "skipped"


# ── 场景加载 ────────────────────────────────────────────────────
@runtime_checkable
class ScenarioLoader(Protocol):
    """场景加载器：从 DB / 内存文档取场景（含步骤 + flow + local_vars）。"""

    async def load_scenario(self, scenario_id: Any) -> Mapping[str, Any]:
        ...


class DictScenarioLoader:
    """内存场景加载器（单测 / 调试 / 前置场景解析用）。"""

    def __init__(self, scenarios: Mapping[Any, Mapping[str, Any]] | Iterable[Mapping[str, Any]]) -> None:
        if isinstance(scenarios, Mapping):
            self._by_id = {str(k): dict(v) for k, v in scenarios.items()}
        else:
            self._by_id = {}
            for doc in scenarios:
                doc = dict(doc)
                key = doc.get("id") or doc.get("code")
                self._by_id[str(key)] = doc

    async def load_scenario(self, scenario_id: Any) -> Mapping[str, Any]:
        try:
            return self._by_id[str(scenario_id)]
        except KeyError:
            raise ScenarioNotFoundError(f"场景不存在：{scenario_id!r}") from None

    def add(self, scenario: Mapping[str, Any]) -> None:
        doc = dict(scenario)
        self._by_id[str(doc.get("id") or doc.get("code"))] = doc


# ── 结果模型（对应 mc_test_step_result / mc_test_run_scenario）──
@dataclass
class StepResult:
    """步骤级结果（04 §1.3：expected/actual/diff/duration_ms）。"""

    step_key: str
    seq: Any
    type: str
    status: str
    attempt: int = 1
    expected: Any = None
    actual: Any = None
    diff: Any = None
    duration_ms: int = 0
    description: str = ""
    checks: list[dict] = field(default_factory=list)
    error: Optional[dict] = None
    evidence: list[dict] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "step_key": self.step_key,
            "seq": self.seq,
            "type": self.type,
            "status": self.status,
            "attempt": self.attempt,
            "expected": self.expected,
            "actual": self.actual,
            "diff": self.diff,
            "duration_ms": self.duration_ms,
            "description": self.description,
            "checks": self.checks,
            "error": self.error,
            "evidence": self.evidence,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class ScenarioResult:
    """场景级结果（04 §1.2）。"""

    scenario_id: Any
    scenario_code: Optional[str] = None
    status: str = STATUS_PASS
    duration_ms: int = 0
    steps: list[StepResult] = field(default_factory=list)
    local_vars: dict = field(default_factory=dict)
    error: Optional[dict] = None
    early_exit: Optional[dict] = None
    pre_scenarios: list[Any] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status in (STATUS_PASS, STATUS_FLAKY)

    @property
    def summary(self) -> dict:
        counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_ERROR: 0, STATUS_SKIPPED: 0}
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return {
            "total": len(self.steps),
            "pass": counts.get(STATUS_PASS, 0),
            "fail": counts.get(STATUS_FAIL, 0),
            "error": counts.get(STATUS_ERROR, 0),
            "skipped": counts.get(STATUS_SKIPPED, 0),
            "duration_ms": self.duration_ms,
        }

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "scenario_code": self.scenario_code,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "local_vars": self.local_vars,
            "error": self.error,
            "early_exit": self.early_exit,
            "pre_scenarios": self.pre_scenarios,
            "steps": [s.to_dict() for s in self.steps],
        }


# ── 结果落库（可选，服务端进程内直接写 PG，04 §6 决策 4）────────
_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS mc_test_run_scenario (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID,
    scenario_id TEXT NOT NULL,
    case_refs JSONB,
    exec_mode VARCHAR(32),
    status VARCHAR(16) NOT NULL,
    failure_kind VARCHAR(32),
    duration_ms INT,
    healing JSONB,
    dataset_row JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mc_test_step_result (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_scenario_id UUID REFERENCES mc_test_run_scenario(id),
    step_key TEXT NOT NULL,
    seq TEXT,
    type VARCHAR(32),
    status VARCHAR(16),
    attempt INT,
    expected JSONB,
    actual JSONB,
    diff JSONB,
    duration_ms INT,
    checks JSONB,
    evidence JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class PgResultWriter:
    """把场景/步骤结果写 PG（复用 `from db import PostgresClient`）。

    表 `mc_test_run_scenario` / `mc_test_step_result` 若不存在则自动创建
    （bootstrap DDL，与 04 §1.2 / 04 §1.3 对齐；正式迁移可覆盖此 DDL）。
    写库失败不阻断执行判定，只记 warning。
    """

    def __init__(self, run_id: Any = None, run_scenario_id: Any = None, *, auto_create_tables: bool = True) -> None:
        self.run_id = run_id
        self.run_scenario_id = run_scenario_id
        self.auto_create_tables = auto_create_tables

    async def write(self, result: ScenarioResult, ctx: ExecutionContext) -> None:
        try:
            # 首选：主服务 asyncpg 池（服务进程内）。独立脚本/测试没有池时
            # 回退 Tortoise ORM（模型在 tasks/models.py，与 bootstrap DDL 同列），
            # 保证「执行→落库→报告」链路在任何宿主都通（04 §2）。
            from db import PostgresClient  # 服务端进程内

            pool = getattr(PostgresClient, "pool", None)
            if pool is not None:
                await self._write_asyncpg(result, ctx, pool)
            else:
                await self._write_tortoise(result, ctx)
        except Exception as exc:  # 落库失败不影响执行判定
            import logging

            logging.getLogger(__name__).warning("[engine] 结果落库失败：%s", exc)

    async def _write_asyncpg(self, result: ScenarioResult, ctx: ExecutionContext, pool: Any) -> None:
        from db import PostgresClient  # 局部 import：本方法作用域内也要用

        redactor = ctx.redactor()
        import uuid as _uuid

        def jb(value: Any) -> str:
            return PostgresClient._dumps(redactor.obj(value))

        def uid(value: Any) -> Any:
            """UUID 列：合法 uuid 转成 UUID 对象，否则原样（兼容 TEXT）。"""
            return _uuid.UUID(value) if _is_uuid(value) else value

        run_scenario_id = self.run_scenario_id or str(_uuid.uuid4())
        async with pool.acquire() as conn:
            if self.auto_create_tables:
                await conn.execute(_BOOTSTRAP_DDL)
            await conn.execute(
                """
                INSERT INTO mc_test_run_scenario
                    (id, run_id, scenario_id, case_refs, exec_mode, status, failure_kind, duration_ms, dataset_row)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                """,
                uid(run_scenario_id),
                uid(self.run_id) if self.run_id else None,
                str(result.scenario_id),
                jb(ctx.case_refs or []),
                ctx.exec_mode,
                result.status,
                None,
                result.duration_ms,
                jb(ctx.dataset_row),
            )
            for step in result.steps:
                await conn.execute(
                    """
                    INSERT INTO mc_test_step_result
                        (run_scenario_id, step_key, seq, type, status, attempt,
                         expected, actual, diff, duration_ms, checks, evidence)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,$11::jsonb,$12::jsonb)
                    """,
                    uid(run_scenario_id),
                    step.step_key,
                    str(step.seq),
                    step.type,
                    step.status,
                    step.attempt,
                    jb(step.expected),
                    jb(step.actual),
                    jb(step.diff),
                    step.duration_ms,
                    jb(step.checks),
                    jb(step.evidence),
                )

    async def _write_tortoise(self, result: ScenarioResult, ctx: ExecutionContext) -> None:
        """Tortoise 回退：写 tasks/models.py 的 TestRunScenario/TestStepResult。"""
        import uuid as _uuid

        from ..tasks.models import TestRunScenario, TestStepResult

        run_scenario_id = self.run_scenario_id or str(_uuid.uuid4())
        # run_id 列 NOT NULL——无 run 归属时用哨兵 UUID（独立调试执行场景）。
        run_uuid = (
            _uuid.UUID(str(self.run_id))
            if self.run_id and _is_uuid(str(self.run_id))
            else _uuid.UUID(int=0)
        )
        await TestRunScenario.create(
            id=run_scenario_id if _is_uuid(run_scenario_id) else _uuid.uuid4(),
            run_id=run_uuid,
            scenario_id=str(result.scenario_id),
            case_refs=list(ctx.case_refs or []),
            exec_mode=ctx.exec_mode,
            status=result.status,
            duration_ms=result.duration_ms,
            dataset_row=ctx.dataset_row or {},
        )
        for step in result.steps:
            await TestStepResult.create(
                run_scenario_id=run_scenario_id if _is_uuid(run_scenario_id) else None,
                step_key=step.step_key,
                seq=str(step.seq),
                type=step.type,
                status=step.status,
                attempt=step.attempt,
                expected=step.expected if isinstance(step.expected, (dict, list)) else {"value": step.expected},
                actual=step.actual if isinstance(step.actual, (dict, list)) else {"value": step.actual},
                diff=step.diff if isinstance(step.diff, (dict, list)) else {"value": step.diff},
                duration_ms=step.duration_ms,
            )


def _is_uuid(value: Any) -> bool:
    import uuid

    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


# ── 执行器 ──────────────────────────────────────────────────────
class ScenarioExecutor:
    """确定性场景执行器。

    :param loader: 场景加载器（DB 适配器或 `DictScenarioLoader`）
    :param tools: 工具注册表（缺省用内置 http/wait/assert）
    :param assertions: 断言注册表（缺省用内置 5 种）
    :param envs: 环境链文档（`{"id","parent_id","vars"}`），用于 `{{env.*}}` 合并
    :param require_post_checks: 是否强制「每步骤至少一条后置校验」（06 §4）
    :param mark_flaky_on_retry: 重试后才通过 → 场景标 flaky（04 §6 决策 2）
    """

    def __init__(
        self,
        loader: ScenarioLoader,
        *,
        tools: Optional[ToolRegistry] = None,
        assertions: Optional[AssertionRegistry] = None,
        envs: Optional[Sequence[Mapping[str, Any]]] = None,
        result_writer: Optional[PgResultWriter] = None,
        require_post_checks: bool = True,
        mark_flaky_on_retry: bool = True,
        max_pre_scenario_depth: int = 8,
    ) -> None:
        self.loader = loader
        self.tools = tools or default_tool_registry()
        self.assertions = assertions or default_assertion_registry()
        self.envs = list(envs or [])
        self.result_writer = result_writer
        self.require_post_checks = require_post_checks
        self.mark_flaky_on_retry = mark_flaky_on_retry
        self.max_pre_scenario_depth = max_pre_scenario_depth

    # ── 入口 ────────────────────────────────────────────────────
    async def execute(self, scenario_id: Any, ctx: ExecutionContext) -> ScenarioResult:
        """执行一个场景（含前置场景）。返回 ScenarioResult，不抛业务异常。"""
        return await self._execute_scenario(scenario_id, ctx, depth=0, stack=[])

    async def execute_many(self, scenario_ids: Iterable[Any], ctx: ExecutionContext) -> list[ScenarioResult]:
        """顺序执行多个场景（04 §6 决策 1：默认串行；每行数据独立 ctx 由调用方保证）。"""
        results: list[ScenarioResult] = []
        for scenario_id in scenario_ids:
            results.append(await self.execute(scenario_id, ctx))
        return results

    # ── 单场景 ──────────────────────────────────────────────────
    async def _execute_scenario(
        self,
        scenario_id: Any,
        ctx: ExecutionContext,
        *,
        depth: int,
        stack: list[Any],
    ) -> ScenarioResult:
        started = time.monotonic()
        try:
            doc = await maybe_await(self.loader.load_scenario(scenario_id))
        except EngineError as exc:
            return ScenarioResult(
                scenario_id=scenario_id, status=STATUS_ERROR, error=exc.to_dict(),
                duration_ms=_ms(started),
            )

        doc = dict(doc)
        result = ScenarioResult(
            scenario_id=doc.get("id", scenario_id),
            scenario_code=doc.get("code"),
            status=STATUS_PASS,
        )

        if depth > self.max_pre_scenario_depth:
            result.status = STATUS_ERROR
            result.error = {"kind": "pre_scenario_depth", "message": "前置场景嵌套过深"}
            result.duration_ms = _ms(started)
            return result

        # 局部变量：声明 + 初值（01 §3.1）
        local_vars = dict(doc.get("local_vars") or {})
        for key, value in local_vars.items():
            if key not in ctx.resolver.get_scope("local"):
                ctx.resolver.set_local(key, value)

        # 场景默认环境（可被任务覆盖，05 §7）：任务 ctx 已合并的 env 优先
        if doc.get("env_id") is not None and not ctx.env:
            merged = self._load_env(doc.get("env_id"))
            ctx.resolver.set_scope("env", merged)

        flow = doc.get("flow") or {}
        steps = self._normalize_steps(doc.get("steps") or [])
        if not steps:
            result.status = STATUS_SKIPPED
            result.duration_ms = _ms(started)
            return result

        # 条件分支（design §19.3）：算出本场景实际启用的 step_key 集合
        enabled_keys = self._resolve_flow_conditions(flow, ctx)

        # 前置场景：先跑，local_vars 传递（01 §3.1 / 04 §2）
        pre_refs = list(flow.get("pre_scenarios") or doc.get("pre_scenarios") or [])
        for ref in pre_refs:
            ref_id = ref.get("ref") if isinstance(ref, Mapping) else ref
            if ref_id in stack:
                result.status = STATUS_ERROR
                result.error = {"kind": "pre_scenario_cycle", "message": f"前置场景成环：{ref_id}"}
                result.duration_ms = _ms(started)
                return result
            pre_result = await self._execute_scenario(
                ref_id, ctx, depth=depth + 1, stack=stack + [scenario_id]
            )
            result.pre_scenarios.append(ref_id)
            if not pre_result.passed:
                result.status = pre_result.status
                result.error = {
                    "kind": "pre_scenario_failed",
                    "message": f"前置场景 {ref_id} 未通过（{pre_result.status}）",
                    "pre_scenario": ref_id,
                }
                result.steps = pre_result.steps
                result.duration_ms = _ms(started)
                return result

        early_exit_rules = list(flow.get("early_exit") or doc.get("early_exit") or [])
        aborted = False
        # 流程控制区域栈（01 §4.2 扩展：if/else_if/else/end_if/break/end_scenario）。
        # 每帧对应一个打开的 if 块：``active``=当前分支是否执行、``matched``=本块是否已有命中分支。
        branch_stack: list[dict[str, bool]] = []

        # scenario_ref 步骤通道：递归执行子场景（共享 ctx → local 变量父子直传，
        # 与前置场景同机制）。每个非控制步骤执行前重新绑定，避免子场景跑完后父层
        # 继续用子层闭包导致 depth/stack 错位。
        async def _referenced_runner(ref: Any) -> "ScenarioResult":
            guard = {str(x) for x in (*stack, scenario_id)}
            if str(ref) in guard:
                return ScenarioResult(
                    scenario_id=ref,
                    status=STATUS_ERROR,
                    error={"kind": "pre_scenario_cycle", "message": f"引用场景成环：{ref}"},
                )
            return await self._execute_scenario(ref, ctx, depth=depth + 1, stack=[*stack, scenario_id])

        index = 0
        while index < len(steps):
            step = steps[index]
            index += 1
            step_type = str(step.get("type") or "")

            # ── 流程控制步骤：区域栈解释，不分发工具 ──────────────────
            if step_type in CONTROL_FLOW_TYPES:
                if self._apply_control_flow(step_type, step, ctx, branch_stack, result):
                    break  # end_scenario / 顶层 break → 场景提前结束
                continue

            # ── 普通步骤：先看是否落在未命中的分支里 ──────────────────
            if not all(frame["active"] for frame in branch_stack):
                result.steps.append(self._skipped_result(step, reason="条件分支未命中"))
                continue
            if step.get("enabled", True) is False:
                result.steps.append(self._skipped_result(step, reason="步骤已禁用"))
                continue
            if enabled_keys is not None and step["step_key"] not in enabled_keys:
                result.steps.append(self._skipped_result(step, reason="条件分支未选中"))
                continue
            if not self._eval_step_conditions(step, ctx):
                result.steps.append(self._skipped_result(step, reason="步骤前置条件不满足"))
                continue

            # scenario_ref 步骤通道：逐步骤绑定当前层的递归执行器（子场景跑完后
            # 父循环下一行会重新绑定，保证 depth/stack 始终属于当前层）。
            ctx.scenario_runner = _referenced_runner
            step_result = await self._run_step(step, ctx)
            result.steps.append(step_result)

            if step_result.status == STEP_SKIPPED:
                continue

            if step_result.status in (STEP_FAIL, STEP_ERROR):
                # abort 策略 → 终止整个场景
                if step_result.error and step_result.error.get("policy_action") == "abort":
                    aborted = True
                    result.status = (
                        STATUS_FAIL if step_result.status == STEP_FAIL else STATUS_ERROR
                    )
                    result.error = {
                        "kind": "aborted",
                        "message": f"步骤 {step['step_key']} 触发 abort 策略",
                        "step_key": step["step_key"],
                    }
                    break
                result.status = (
                    STATUS_FAIL if step_result.status == STEP_FAIL else STATUS_ERROR
                )
                result.error = {
                    "kind": step_result.error.get("kind") if step_result.error else "step_failed",
                    "message": (step_result.error or {}).get("message", "步骤失败"),
                    "step_key": step["step_key"],
                }
                break

            # early_exit（04 §2：if early_exit_hit(ctx): break）
            hit = self._early_exit_hit(early_exit_rules, step, ctx)
            if hit is not None:
                result.early_exit = hit
                break

        # 重试后才通过 → flaky（04 §6 决策 2）
        if self.mark_flaky_on_retry and result.status == STATUS_PASS:
            if any(s.status == STEP_PASS and s.attempt > 1 for s in result.steps):
                result.status = STATUS_FLAKY

        if aborted and result.status == STATUS_PASS:
            result.status = STATUS_ERROR

        result.local_vars = dict(ctx.resolver.get_scope("local"))
        result.duration_ms = _ms(started)

        if self.result_writer is not None:
            await self.result_writer.write(result, ctx)
        return result

    # ── 单步骤 ──────────────────────────────────────────────────
    async def _run_step(self, step: dict, ctx: ExecutionContext) -> StepResult:
        """执行一个步骤：工具分发 → 后置校验 → 写局部变量 → 错误策略。"""
        started = time.monotonic()
        step_key = step["step_key"]
        ctx.current_step_key = step_key
        ctx.current_step = step

        policy = ErrorPolicy.parse(step.get("on_error"))
        state = ErrorPolicyState(policy)
        checks_spec = self._post_checks(step)
        last_result: Optional[StepResult] = None

        while True:
            attempt = state.begin_attempt()
            ctx.set_step_output(None, None)
            ctx.resolver.set_scope("response", {})
            ctx.resolver.set_scope("result", {})
            try:
                tool_name = resolve_tool_for_step(step, self.tools)
                # 参数求值（06 §2.2）：变量解析在此发生；异步版支持 {{Class.method(...)}}
                resolved_step = dict(step)
                resolved_step["params"] = await ainterpolate_params(
                    step.get("params") or {}, ctx.resolver
                )

                # 06 §4：每步骤至少一条后置校验（校验门强制）。
                # 流程控制（执行器解释）与 wait（无有意义产出）豁免；
                # `check` 步骤的断言在 params.checks 里（由 assert 工具带回），
                # 因此不需要步骤级 post_checks。
                if (
                    self.require_post_checks
                    and step.get("type") not in NO_CHECK_STEP_TYPES
                    and not checks_spec
                ):
                    has_param_checks = bool(
                        (step.get("params") or {}).get("checks")
                        or (step.get("params") or {}).get("assertions")
                    )
                    if not has_param_checks:
                        raise StepParamsError(
                            f"步骤 {step_key} 缺少后置校验（06 §4：每步骤至少一条）",
                            details={"step_key": step_key, "type": step.get("type")},
                        )

                tool_result = await self.tools.run(tool_name, resolved_step, ctx)
                self._bind_outputs(tool_result, ctx)

                deferred = bool(tool_result.meta.get("defer_checks"))
                effective_checks = checks_spec
                if deferred:
                    effective_checks = list(tool_result.meta.get("checks") or checks_spec)

                check_results = await self._run_post_checks(
                    effective_checks, step, ctx, tool_result
                )

                # 后置校验全过 → 写局部变量（post_cond；右侧可含 {{Class.method(...)}}）
                for cond in self._post_conds(step):
                    statement = cond.get("set") if isinstance(cond, Mapping) else cond
                    if statement:
                        await aapply_assignment(statement, ctx.resolver)

                return StepResult(
                    step_key=step_key,
                    seq=step.get("seq"),
                    type=str(step.get("type")),
                    status=STEP_PASS,
                    attempt=attempt,
                    expected=[c["expected"] for c in check_results] or None,
                    actual=[c["actual"] for c in check_results] or None,
                    diff=[c["diff"] for c in check_results if not c["passed"]] or None,
                    duration_ms=_ms(started),
                    description=step.get("description", ""),
                    checks=check_results,
                    evidence=list(tool_result.evidence),
                )
            except CheckError as exc:
                last_result = self._failed_result(step, attempt, exc.results, started, kind="check_failed")
                decision = state.on_error(exc, assertion_failed=True)
                last_result.error = self._error_payload(exc, decision)
            except EngineError as exc:
                last_result = self._failed_result(step, attempt, [], started, kind=exc.kind)
                decision = state.on_error(exc, assertion_failed=False)
                last_result.error = self._error_payload(exc, decision)
            except Exception as exc:  # 未预期异常 → 归为 error，交错误策略
                wrapped = EngineError(f"{type(exc).__name__}: {exc}")
                last_result = self._failed_result(step, attempt, [], started, kind="internal")
                decision = state.on_error(wrapped, assertion_failed=False)
                last_result.error = self._error_payload(wrapped, decision)

            if decision.action in ("retry", "reinit", "evict"):
                if decision.action == "reinit":
                    await self._reinit_transport(ctx)
                elif decision.action == "evict":
                    await self._evict_session(ctx)
                continue

            if decision.action == "skip":
                last_result.status = STEP_SKIPPED
                last_result.skipped_reason = "on_error=skip"
                return last_result

            # abort / fail → 返回失败结果（abort 由调用方终止场景）
            return last_result

    # ── 后置校验 ────────────────────────────────────────────────
    async def _run_post_checks(
        self,
        checks: Sequence[Any],
        step: dict,
        ctx: ExecutionContext,
        tool_result: ToolResult,
    ) -> list[dict]:
        """跑完所有后置校验；任一条不过 → 抛 CheckError（携带全部结果）。"""
        results: list[AssertionResult] = []
        for check in checks:
            spec = dict(check) if isinstance(check, Mapping) else {"type": str(check)}
            check_type = spec.get("type")
            if not check_type:
                raise StepParamsError(f"后置校验缺少 type：{spec!r}")
            # 断言参数里的 {{}} 先求值（01 §4.4 示例 expected: "{{data.amount}}"）
            params = await ainterpolate_params(
                {k: v for k, v in spec.items() if k != "type"}, ctx.resolver
            )
            actual = await self._check_subject(check_type, spec, ctx, tool_result)
            result = await self.assertions.evaluate(check_type, actual, params)
            results.append(result)

        if any(not r.passed for r in results):
            raise CheckError(results, step_key=step.get("step_key"))
        return [r.to_dict() for r in results]

    async def _check_subject(
        self, check_type: str, spec: Mapping[str, Any], ctx: ExecutionContext, tool_result: ToolResult
    ) -> Any:
        """决定断言作用在什么「实际值」上。"""
        if "actual" in spec:
            return await ainterpolate_params(spec["actual"], ctx.resolver)
        if check_type == "status_code":
            return ctx.response
        if check_type == "json_path":
            return ctx.response
        if check_type == "type_match":
            path = spec.get("path")
            if path:
                from .assertions import _jsonpath_get, _resolve_json_source

                found, value = _jsonpath_get(_resolve_json_source(ctx.response), str(path))
                return value if found else None
            return ctx.response
        # contains / regex 等字符串类：默认取响应体（dict 取 body）
        response = ctx.response
        if isinstance(response, dict):
            return response.get("body", response)
        if response is not None:
            return response
        return ctx.result

    # ── 步骤输出绑定 ────────────────────────────────────────────
    def _bind_outputs(self, tool_result: ToolResult, ctx: ExecutionContext) -> None:
        """把工具产物写进 {{response.*}} / {{result.*}}（单步骤生命周期）。

        - http 工具返回 {status, headers, body} → {{response.status}} 等
        - body 为 JSON 字符串时自动解析进 {{response.json.*}}
        - 工具自报产物 → {{result.*}}
        """
        ctx.set_step_output(tool_result.response, tool_result.result)

        # {{response.*}}
        response_scope: dict[str, Any] = {}
        resp = tool_result.response
        if isinstance(resp, Mapping):
            response_scope.update(resp)
            if "json" not in response_scope:
                from .assertions import _resolve_json_source

                response_scope["json"] = _resolve_json_source(resp)
        elif resp is not None:
            response_scope["value"] = resp
            response_scope["body"] = resp
            from .assertions import _resolve_json_source

            response_scope["json"] = _resolve_json_source(resp)
        ctx.resolver.set_scope("response", response_scope)

        # {{result.*}}
        res = tool_result.result
        if isinstance(res, Mapping):
            result_scope = dict(res)
        elif res is not None:
            result_scope = {"value": res}
        else:
            result_scope = {}
        ctx.resolver.set_scope("result", result_scope)

    # ── 步骤归一化 ──────────────────────────────────────────────
    @staticmethod
    def _normalize_steps(steps: Sequence[Mapping[str, Any]]) -> list[dict]:
        """归一化 + 按 seq 排序（seq 是唯一顺序真相，01 §4.1）。

        步骤既可声明 `type`（01 §4.2 存储形态：request/page/…），也可只写
        `tool`（design §19.1 交换格式：http/browser/…）——两者缺一即报错。
        """
        normalized: list[dict] = []
        for index, raw in enumerate(steps):
            step = dict(raw)
            step["step_key"] = str(step.get("step_key") or step.get("id") or f"step_{index}")
            if step.get("seq") is None:
                step["seq"] = (index + 1) * 10
            step["type"] = str(step.get("type") or step.get("tool") or "")
            if not step["type"]:
                raise StepParamsError(f"步骤 {step['step_key']} 缺少 type/tool（01 §4.2）")
            normalized.append(step)
        normalized.sort(key=lambda s: _seq_key(s["seq"]))
        return normalized

    @staticmethod
    def _post_checks(step: Mapping[str, Any]) -> list:
        """后置校验：`post_checks` 优先，兼容 design 的 `assertions` 别名。"""
        checks = step.get("post_checks")
        if checks is None:
            checks = step.get("assertions")
        return list(checks or [])

    @staticmethod
    def _post_conds(step: Mapping[str, Any]) -> list:
        conds = step.get("post_cond")
        if conds is None:
            conds = step.get("post_conditions")
        return list(conds or [])

    # ── 条件 / early_exit ───────────────────────────────────────
    def _apply_control_flow(
        self,
        step_type: str,
        step: Mapping[str, Any],
        ctx: ExecutionContext,
        stack: list[dict[str, bool]],
        result: ScenarioResult,
    ) -> bool:
        """区域栈解释一条流程控制步骤（01 §4.2 扩展）。

        - ``if``：求值 ``params.condition`` → 命中则进入活跃分支；
        - ``else_if``：挂最近 ``if`` 帧，前面分支都未命中且父作用域活跃时才求值；
        - ``else``：前面都未命中时活跃；
        - ``end_if``：闭合最近 ``if`` 帧；
        - ``break``：跳出当前 ``if`` 块（剩余步骤跳过直到其 ``end_if``；顶层 break 结束场景）；
        - ``end_scenario``：立即结束整个场景（记 ``early_exit``，场景判定 pass）。

        控制步骤是**结构性**的：忽略 ``enabled``（禁用判断块请禁用块内步骤），
        也不参与 ``flow.conditions`` 的 enabled_keys 过滤（否则栈会失衡）。

        :returns: True 表示场景应立即结束（end_scenario / 顶层 break）。
        """
        params = step.get("params") or {}
        condition = params.get("condition")
        if condition is None:
            condition = step.get("when")
        step_key = str(step.get("step_key"))

        cond_error: Optional[dict] = None

        def eval_cond(expr: Any) -> bool:
            nonlocal cond_error
            if expr is None or (isinstance(expr, str) and not expr.strip()):
                return True
            try:
                return eval_expression(expr, ctx.resolver)
            except EngineError as exc:
                # 未定义变量/非法表达式：控制步骤在 _run_step 的 try/except 之外，
                # 不炸主循环；按未命中处理，把错误挂到该控制步骤的结果上可见。
                cond_error = exc.to_dict()
                return False

        if step_type == "if":
            hit = all(f["active"] for f in stack) and eval_cond(condition)
            stack.append({"active": hit, "matched": hit})
            result.steps.append(self._control_result(step, hit, cond_error))
            return False

        if step_type in ("else_if", "else"):
            if not stack:
                result.steps.append(
                    self._skipped_result(step, reason=f"{step_type} 缺少对应的 if 块")
                )
                return False
            frame = stack[-1]
            parent_active = all(f["active"] for f in stack[:-1])
            if step_type == "else_if":
                hit = parent_active and not frame["matched"] and eval_cond(condition)
                if hit:
                    frame["matched"] = True
                frame["active"] = hit
            else:  # else：父作用域活跃且前面都没命中 → 接管
                frame["active"] = parent_active and not frame["matched"]
                frame["matched"] = True
            result.steps.append(self._control_result(step, frame["active"], cond_error))
            return False

        if step_type == "end_if":
            if stack:
                stack.pop()
            result.steps.append(self._control_result(step, True))
            return False

        if step_type == "break":
            # 可选 condition 门：条件不满足就不跳（继续留在当前分支）
            if condition is not None and not eval_cond(condition):
                result.steps.append(self._control_result(step, True, cond_error))
                return False
            if stack:
                stack[-1]["active"] = False
                stack[-1]["matched"] = True
                result.steps.append(self._control_result(step, True))
                return False
            # 顶层 break：没有可跳出的块 → 等价结束场景剩余步骤
            result.early_exit = {"after": step_key, "reason": "break", "when": condition}
            result.steps.append(self._control_result(step, True))
            return True

        # end_scenario：立即结束整个场景
        result.early_exit = {
            "after": step_key,
            "reason": params.get("reason") or "",
            "when": condition,
        }
        result.steps.append(self._control_result(step, True))
        return True

    @staticmethod
    def _control_result(
        step: Mapping[str, Any], hit: bool, error: Optional[dict] = None
    ) -> StepResult:
        """流程控制步骤的结果行（不跑工具；命中=pass / 未命中=skipped）。

        ``error``（如条件求值失败）挂到该行，便于报告里定位分支为什么没命中。
        """
        return StepResult(
            step_key=str(step["step_key"]),
            seq=step.get("seq"),
            type=str(step.get("type")),
            status=STEP_PASS if hit else STEP_SKIPPED,
            description=step.get("description", ""),
            skipped_reason=None if hit else "条件分支未命中",
            error=error,
        )

    def _eval_step_conditions(self, step: Mapping[str, Any], ctx: ExecutionContext) -> bool:
        """步骤级前置条件：`pre_cond` / `when` / `if`。"""
        conds = step.get("pre_cond") or step.get("pre_conditions")
        if conds:
            for cond in conds:
                expr = cond.get("assert") if isinstance(cond, Mapping) else cond
                if expr and not eval_expression(expr, ctx.resolver):
                    return False
        for key in ("when", "if"):
            expr = step.get(key)
            if expr is None:
                continue
            if isinstance(expr, Mapping):  # {"if":..., "then":[...], "else":[...]} 由 flow.conditions 处理
                continue
            if not eval_expression(expr, ctx.resolver):
                return False
        return True

    def _resolve_flow_conditions(self, flow: Mapping[str, Any], ctx: ExecutionContext) -> Optional[set]:
        """flow.conditions → 本场景实际启用的 step_key 集合；无条件时返回 None。"""
        conditions = flow.get("conditions")
        if not conditions:
            return None
        enabled: set[str] = set()
        for cond in conditions:
            branch = cond.get("then") if eval_expression(cond.get("if"), ctx.resolver) else cond.get("else")
            enabled.update(str(k) for k in (branch or []))
        return enabled

    @staticmethod
    def _early_exit_hit(rules: Sequence[Mapping[str, Any]], step: Mapping[str, Any], ctx: ExecutionContext) -> Optional[dict]:
        for rule in rules:
            after = rule.get("after") or rule.get("after_step")
            if after is not None and str(after) != step["step_key"]:
                continue
            when = rule.get("when")
            if when is None or eval_expression(when, ctx.resolver):
                return {
                    "after": step["step_key"],
                    "reason": rule.get("reason", ""),
                    "when": when,
                }
        return None

    # ── 错误策略副作用 ──────────────────────────────────────────
    async def _reinit_transport(self, ctx: ExecutionContext) -> None:
        """reinit：重跑传输层鉴权。骨架里清空 auth 作用域（真鉴权由传输层适配器接管）。"""
        ctx.resolver.set_scope("auth", {})

    async def _evict_session(self, ctx: ExecutionContext) -> None:
        """evict：清理会话重开。骨架里清空 response/result（真会话由驱动接管）。"""
        ctx.resolver.set_scope("response", {})
        ctx.resolver.set_scope("result", {})

    # ── 环境合并 ────────────────────────────────────────────────
    def _load_env(self, env_id: Any) -> dict[str, Any]:
        if not self.envs:
            return {}
        return merge_env_chain(self.envs, env_id)

    # ── 结果构造 ────────────────────────────────────────────────
    def _skipped_result(self, step: Mapping[str, Any], *, reason: str) -> StepResult:
        return StepResult(
            step_key=step["step_key"],
            seq=step.get("seq"),
            type=str(step.get("type")),
            status=STEP_SKIPPED,
            description=step.get("description", ""),
            skipped_reason=reason,
        )

    def _failed_result(
        self,
        step: Mapping[str, Any],
        attempt: int,
        check_results: Sequence[AssertionResult],
        started: float,
        *,
        kind: str,
    ) -> StepResult:
        checks = [r.to_dict() for r in check_results]
        failed = [r for r in check_results if not r.passed]
        first = failed[0] if failed else None
        return StepResult(
            step_key=step["step_key"],
            seq=step.get("seq"),
            type=str(step.get("type")),
            status=STEP_FAIL if kind == "check_failed" else STEP_ERROR,
            attempt=attempt,
            expected=first.expected if first else ([c["expected"] for c in checks] or None),
            actual=first.actual if first else ([c["actual"] for c in checks] or None),
            diff=(first.diff if first else None) or ([c["diff"] for c in failed] or None),
            duration_ms=_ms(started),
            description=step.get("description", ""),
            checks=checks,
        )

    @staticmethod
    def _error_payload(exc: BaseException, decision: Decision) -> dict:
        payload = exc.to_dict() if isinstance(exc, EngineError) else {"kind": "internal", "message": str(exc)}
        payload["policy"] = decision.policy.to_dict()
        payload["policy_action"] = decision.action
        payload["attempt"] = decision.attempt
        return payload


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _seq_key(seq: Any) -> tuple[int, Any]:
    """seq 排序键：数字优先，非数字退化为字符串序（不抛）。"""
    if isinstance(seq, bool):
        return (0, str(seq))
    if isinstance(seq, (int, float)):
        return (0, float(seq))
    text = str(seq)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


__all__ = [
    "ScenarioExecutor",
    "ScenarioLoader",
    "DictScenarioLoader",
    "ScenarioResult",
    "StepResult",
    "PgResultWriter",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_ERROR",
    "STATUS_FLAKY",
    "STATUS_SKIPPED",
    "STEP_PASS",
    "STEP_FAIL",
    "STEP_ERROR",
    "STEP_SKIPPED",
]
