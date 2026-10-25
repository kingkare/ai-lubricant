"""场景引擎执行器测试（06 §19 YAML 示例的最小可运行版本）。

用手写最小场景跑通：1 个 http 步骤 + 1 个断言（post_checks）。

- http 工具用 monkeypatch 桩替换（不真发请求，返回固定 response）
- 断言：场景结果 status=pass/fail 正确、step_result 记录 expected/actual/diff
- 断言：local 变量在步骤间传递成功

运行：`python -m pytest tests/test_engine_executor.py -q`
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from testing_platform.engine import (
    DictScenarioLoader,
    ExecutionContext,
    ScenarioExecutor,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_FLAKY,
    STATUS_PASS,
    STEP_ERROR,
    STEP_FAIL,
    STEP_PASS,
    STEP_SKIPPED,
    ToolResult,
    ToolRegistry,
    UndefinedVariableError,
    VariableResolver,
    apply_assignment,
    eval_expression,
    interpolate_params,
    maybe_await,
    run_sync_or_async,
)
from testing_platform.engine.assertions import build_default_registry as build_assertions


def run(coro):
    return asyncio.run(coro)


# ── 场景文档（JSON 文档形式，见 06 §19 / design §19.1 的最小版）──
SCENARIO = {
    "id": "SCN-0001",
    "code": "SCN-0001",
    "name": "用户下单全流程（最小）",
    "local_vars": {"order_id": None},
    "steps": [
        {
            "step_key": "create_order",
            "seq": 10,
            "type": "request",
            "description": "创建订单",
            "params": {
                "method": "POST",
                "url": "/api/v1/orders",
                "headers": {"Content-Type": "application/json"},
                "body": {"product_id": "P001", "quantity": 1},
            },
            "post_checks": [
                {"type": "status_code", "expected": 201},
                {"type": "json_path", "path": "$.data.order_id", "operator": "not_null"},
            ],
            "post_cond": [
                {"set": "{{local.order_id}} = {{response.json.data.order_id}}"},
            ],
            "on_error": "retry(2)",
        },
        {
            "step_key": "verify_order",
            "seq": 20,
            "type": "request",
            "description": "查询订单（消费 local.order_id）",
            "params": {"method": "GET", "url": "/api/v1/orders/{{local.order_id}}"},
            "post_checks": [
                {"type": "json_path", "path": "$.data.status", "operator": "eq", "expected": "created"},
            ],
        },
    ],
}


class FakeHttp:
    """http 工具桩：按方法/URL 返回固定响应，并记录收到的请求。"""

    def __init__(self, *, create_status=201, verify_status=200, verify_body_status="created"):
        self.create_status = create_status
        self.verify_status = verify_status
        self.verify_body_status = verify_body_status
        self.calls: list[dict] = []
        self.fail_times = 0  # 前 N 次直接抛错（测 retry）

    async def __call__(self, step, ctx):
        params = step["params"]
        self.calls.append({"method": params.get("method"), "url": params.get("url"), "body": params.get("body")})
        if self.fail_times > 0:
            self.fail_times -= 1
            from testing_platform.engine import ToolExecutionError

            raise ToolExecutionError("模拟网络抖动")
        if params.get("method") == "POST":
            body = {"data": {"order_id": "ORD-1", "status": "created"}}
            status = self.create_status
        else:
            body = {"data": {"order_id": "ORD-1", "status": self.verify_body_status}}
            status = self.verify_status
        return ToolResult(response={"status": status, "headers": {}, "body": body})


def make_executor(loader, fake) -> ScenarioExecutor:
    from testing_platform.engine.tools import default_registry as default_tool_registry

    registry = ToolRegistry()
    registry.add(default_tool_registry().get("http"))
    registry.add(default_tool_registry().get("wait"))
    registry.add(default_tool_registry().get("assert"))
    registry.register("http", step_types=("request",))(fake)
    return ScenarioExecutor(loader, tools=registry, assertions=build_assertions())


# ── 1) 最小场景跑通：pass + 记录 expected/actual + local 传递 ──
def test_minimal_scenario_passes():
    fake = FakeHttp()
    executor = make_executor(DictScenarioLoader({"SCN-0001": SCENARIO}), fake)

    result = run(executor.execute("SCN-0001", ExecutionContext()))

    assert result.status == STATUS_PASS
    assert [s.step_key for s in result.steps] == ["create_order", "verify_order"]
    assert all(s.status == STEP_PASS for s in result.steps)

    # step_result 记录了 expected / actual
    create = result.steps[0]
    assert create.expected == [201, {"path": "$.data.order_id", "operator": "not_null"}]
    assert create.actual[0] == 201                       # status_code 断言的实际值
    assert create.actual[1] == "ORD-1"                   # json_path 断言命中 order_id
    assert create.diff is None                           # 全过 → 无 diff
    assert create.checks[0]["type"] == "status_code"

    # local 变量流转：post_cond 写入 → 下一步骤消费（url 已替换）
    assert result.local_vars["order_id"] == "ORD-1"
    assert fake.calls[0]["url"] == "/api/v1/orders"
    assert fake.calls[1]["url"] == "/api/v1/orders/ORD-1"
    assert result.summary == {"total": 2, "pass": 2, "fail": 0, "error": 0, "skipped": 0, "duration_ms": result.duration_ms}


# ── 2) 断言失败：场景 fail，step 记录 diff ─────────────────────
def test_assertion_failure_sets_fail_and_diff():
    fake = FakeHttp(create_status=500)  # status_code 期望 201 → 失败
    executor = make_executor(DictScenarioLoader({"SCN-0001": SCENARIO}), fake)

    result = run(executor.execute("SCN-0001", ExecutionContext()))

    assert result.status == STATUS_FAIL
    assert result.steps[0].status == STEP_FAIL
    assert result.steps[0].expected == 201
    assert result.steps[0].actual == 500
    assert result.steps[0].diff == {"status": 500, "expected": 201}
    # 第二步不再执行（fail 默认 → 终止场景）
    assert len(result.steps) == 1
    assert result.error["step_key"] == "create_order"


# ── 3) 未定义变量 → 报错（不静默变字符串）──────────────────────
def test_undefined_variable_errors():
    scenario = {
        "id": "SCN-UNDEF",
        "steps": [
            {
                "step_key": "bad",
                "seq": 10,
                "type": "request",
                "params": {"method": "GET", "url": "/api/{{local.missing}}"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            }
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-UNDEF": scenario}), FakeHttp())
    result = run(executor.execute("SCN-UNDEF", ExecutionContext()))
    assert result.status == STATUS_ERROR
    assert result.steps[0].status == STEP_ERROR
    assert result.steps[0].error["kind"] == "undefined_variable"


# ── 4) 错误策略 retry(2)：第二次成功 → step pass，场景 flaky ───
def test_retry_policy_then_flaky():
    fake = FakeHttp()
    fake.fail_times = 1  # 首次抛错
    executor = make_executor(DictScenarioLoader({"SCN-0001": SCENARIO}), fake)

    result = run(executor.execute("SCN-0001", ExecutionContext()))

    assert result.status == STATUS_FLAKY              # 重试后才过 → flaky（04 §6 决策 2）
    assert result.steps[0].status == STEP_PASS
    assert result.steps[0].attempt == 2


# ── 5) 未实现步骤类型（page/browser）→ 报「未注册」──────────
def test_unimplemented_step_type_reports_not_registered():
    scenario = {
        "id": "SCN-PAGE",
        "steps": [
            {
                "step_key": "open_page",
                "seq": 10,
                "type": "page",
                "params": {"action": "navigate", "url": "https://example.test"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            }
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-PAGE": scenario}), FakeHttp())
    result = run(executor.execute("SCN-PAGE", ExecutionContext()))
    assert result.status == STATUS_ERROR
    assert result.steps[0].error["kind"] == "tool_not_registered"
    assert "browser" in result.steps[0].error["message"]


# ── 6) 每步骤必须有后置校验（06 §4 校验门强制）────────────────
def test_missing_post_checks_rejected():
    scenario = {
        "id": "SCN-NOCHECK",
        "steps": [{"step_key": "s", "seq": 10, "type": "request", "params": {"method": "GET", "url": "/x"}}],
    }
    executor = make_executor(DictScenarioLoader({"SCN-NOCHECK": scenario}), FakeHttp())
    result = run(executor.execute("SCN-NOCHECK", ExecutionContext()))
    assert result.steps[0].status == STEP_ERROR
    assert result.steps[0].error["kind"] == "step_params"


def test_wait_step_needs_no_post_checks():
    """wait 步骤豁免 R5：无 post_checks 也能执行（require_post_checks=True 默认）。"""
    scenario = {
        "id": "SCN-WAIT",
        "steps": [{"step_key": "pause", "seq": 10, "type": "wait", "params": {"seconds": 0}}],
    }
    executor = make_executor(DictScenarioLoader({"SCN-WAIT": scenario}), FakeHttp())
    result = run(executor.execute("SCN-WAIT", ExecutionContext()))
    assert result.status == STATUS_PASS
    assert result.steps[0].status == STEP_PASS


# ── 7) check 独立断言步（assert 工具）─────────────────────────
def test_standalone_check_step():
    scenario = {
        "id": "SCN-CHECK",
        "steps": [
            {
                "step_key": "assert_value",
                "seq": 10,
                "type": "check",
                "params": {
                    "value": {"code": 0, "msg": "ok"},
                    "checks": [
                        {"type": "json_path", "path": "$.code", "operator": "eq", "expected": 0},
                        {"type": "json_path", "path": "$.msg", "operator": "eq", "expected": "ok"},
                    ],
                },
            }
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-CHECK": scenario}), FakeHttp())
    result = run(executor.execute("SCN-CHECK", ExecutionContext()))
    assert result.status == STATUS_PASS
    assert result.steps[0].type == "check"


# ── 8) disabled 步骤被跳过 ────────────────────────────────────
def test_disabled_step_skipped():
    scenario = {
        "id": "SCN-DISABLED",
        "steps": [
            {
                "step_key": "on",
                "seq": 10,
                "type": "request",
                "params": {"method": "GET", "url": "/a"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
            {
                "step_key": "off",
                "seq": 20,
                "enabled": False,
                "type": "request",
                "params": {"method": "GET", "url": "/b"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-DISABLED": scenario}), FakeHttp())
    result = run(executor.execute("SCN-DISABLED", ExecutionContext()))
    assert result.status == STATUS_PASS
    assert result.steps[1].status == STEP_SKIPPED
    assert result.steps[1].skipped_reason == "步骤已禁用"


# ── 9) early_exit：条件命中提前结束 ───────────────────────────
def test_early_exit_breaks_loop():
    scenario = {
        "id": "SCN-EXIT",
        "flow": {"early_exit": [{"after": "create_order", "when": "{{response.status}} != 201", "reason": "下单失败"}]},
        "steps": [
            {
                "step_key": "create_order",
                "seq": 10,
                "type": "request",
                "params": {"method": "GET", "url": "/a"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
            {
                "step_key": "never",
                "seq": 20,
                "type": "request",
                "params": {"method": "GET", "url": "/b"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
        ],
    }
    # verify_status=200 → 响应状态 200 != 201 → 触发 early_exit
    fake = FakeHttp(verify_status=200)
    executor = make_executor(DictScenarioLoader({"SCN-EXIT": scenario}), fake)
    result = run(executor.execute("SCN-EXIT", ExecutionContext()))
    assert result.status == STATUS_PASS  # 步骤本身通过；early_exit 只是提前结束
    assert len(result.steps) == 1
    assert result.early_exit["after"] == "create_order"


# ── 10) 前置场景：local 传递（01 §3.1）────────────────────────
def test_pre_scenario_local_passthrough():
    pre = {
        "id": "SCN-PRE",
        "local_vars": {"token": None},
        "steps": [
            {
                "step_key": "login",
                "seq": 10,
                "type": "request",
                "params": {"method": "GET", "url": "/login"},
                "post_checks": [{"type": "status_code", "expected": 200}],
                "post_cond": [{"set": "{{local.token}} = {{response.json.data.order_id}}"}],
            }
        ],
    }
    main = {
        "id": "SCN-MAIN",
        "flow": {"pre_scenarios": ["SCN-PRE"]},
        "local_vars": {"order_id": None},
        "steps": [
            {
                "step_key": "use_token",
                "seq": 10,
                "type": "request",
                "params": {"method": "GET", "url": "/orders/{{local.token}}"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            }
        ],
    }
    fake = FakeHttp()
    loader = DictScenarioLoader({"SCN-PRE": pre, "SCN-MAIN": main})
    executor = make_executor(loader, fake)
    ctx = ExecutionContext()
    result = run(executor.execute("SCN-MAIN", ctx))
    assert result.status == STATUS_PASS
    assert result.pre_scenarios == ["SCN-PRE"]
    assert result.local_vars["token"] == "ORD-1"
    assert fake.calls[0]["url"] == "/login"
    assert fake.calls[1]["url"] == "/orders/ORD-1"


# ── 变量系统 / 断言库 单元测试 ────────────────────────────────
def test_variable_type_preserved():
    r = VariableResolver({"env": {"port": 8080, "ratio": 1.5, "flag": True, "obj": {"k": [1, 2]}}})
    assert r.resolve("env.port") == 8080 and isinstance(r.resolve("env.port"), int)
    assert r.resolve("env.ratio") == 1.5
    assert r.resolve("env.flag") is True
    assert r.resolve("env.obj.k[1]") == 2
    # 整串单占位符 → 类型保持
    assert interpolate_params("{{env.port}}", r) == 8080
    # 混合串 → 拼字符串
    assert interpolate_params("http://x:{{env.port}}/a", r) == "http://x:8080/a"
    # 嵌套 dict 递归解析（保持结构）
    got = interpolate_params({"list": ["{{env.port}}", "{{env.flag}}"], "raw": 5}, r)
    assert got == {"list": [8080, True], "raw": 5}


def test_undefined_variable_raises_not_silent():
    r = VariableResolver({"env": {}})
    with pytest.raises(UndefinedVariableError):
        r.resolve("env.nope")
    with pytest.raises(UndefinedVariableError):
        r.interpolate("x={{env.nope}}")
    with pytest.raises(UndefinedVariableError):
        r.resolve("bogus.scope.x")  # 未知作用域同样报错


def test_module_level_interpolate_accepts_ctx_or_dict():
    """interpolate(template, ctx) 接受 ExecutionContext / 作用域 dict（06 §2.2）。"""
    from testing_platform.engine import interpolate

    ctx = ExecutionContext()
    ctx.resolver.set_scope("env", {"base_url": "https://x", "port": 8080})
    assert interpolate("{{env.base_url}}/api", ctx) == "https://x/api"
    assert interpolate("{{env.port}}", ctx) == 8080  # 类型保持
    assert interpolate("{{env.base_url}}/api", {"env": {"base_url": "https://y"}}) == "https://y/api"


@pytest.mark.asyncio
async def test_method_call_placeholder():
    """{{Class.method(a: local.x, k: 3)}} 调用占位符：走 resolver.method_caller（kwargs 绑定）。"""
    from testing_platform.engine.variables import VariableResolver, TemplateError

    r = VariableResolver({"local": {"n": 5, "s": "hi"}, "env": {"base_url": "https://x"}})
    calls: list[tuple[str, dict]] = []

    async def caller(method_ref: str, args: dict):
        calls.append((method_ref, args))
        return f"{method_ref}->{args}"

    r.method_caller = caller

    # 键参（变量名 / 字面量）
    assert await r.resolve_async("Crypto.sign(a: local.n, k: 3)") == "Crypto.sign->{'a': 5, 'k': 3}"
    # 位置参 → arg0 兜底命名（真正的形参校验在 method_channel）
    assert await r.resolve_async("Crypto.sign(local.s, k: 3)") == "Crypto.sign->{'k': 3, 'arg0': 'hi'}"
    # 字面量类型保持
    await r.resolve_async("add(a: 1, b: 2.5, t: true, n: null, s: \"x\")")
    assert calls[-1][1] == {"a": 1, "b": 2.5, "t": True, "n": None, "s": "x"}
    # 嵌套在字符串 / dict / list 里都能解
    assert await r.interpolate_async("pre-{{Crypto.sign(a: local.n)}}-post") == "pre-Crypto.sign->{'a': 5}-post"
    assert await r.interpolate_async({"u": "{{env.base_url}}", "l": ["{{local.n}}"]}) == {
        "u": "https://x",
        "l": [5],
    }
    # 作用域前缀不算调用（是取值路径）
    assert r.parse_method_call("env.base_url") is None
    # 未注入通道 → 可读错误（不静默）
    r2 = VariableResolver()
    with pytest.raises(TemplateError):
        await r2.resolve_async("Crypto.sign(a: 1)")


@pytest.mark.asyncio
async def test_method_call_in_step_params_and_post_cond():
    """端到端：步骤 params 与 post_cond 右侧都跑得动调用占位符。"""
    from testing_platform.engine.variables import aapply_assignment

    r = VariableResolver({"local": {"n": 5}})

    async def caller(method_ref: str, args: dict):
        return args["a"] * 2

    r.method_caller = caller
    assert await r.interpolate_async("{{Crypto.sign(a: local.n)}}") == 10
    await aapply_assignment("{{local.doubled}} = {{Crypto.sign(a: local.n)}}", r)
    assert r.resolve("local.doubled") == 10


def test_scope_priority_local_wins():
    r = VariableResolver({"env": {"k": "env"}, "data": {"k": "data"}, "local": {"k": "local"}})
    assert r.resolve("env.k") == "env"
    assert r.resolve("data.k") == "data"
    assert r.resolve("local.k") == "local"


def test_assignment_and_expression():
    r = VariableResolver({"response": {"json": {"data": {"order_id": "ORD-9"}}}})
    apply_assignment("{{local.order_id}} = {{response.json.data.order_id}}", r)
    assert r.resolve("local.order_id") == "ORD-9"
    assert eval_expression("{{local.order_id}} == 'ORD-9'", r) is True
    assert eval_expression("{{local.order_id}} != 'X'", r) is True
    assert eval_expression("{{local.order_id}} contains 'ORD'", r) is True
    assert eval_expression("1 > 0 and not false", r) is True


@pytest.mark.parametrize(
    "assertion_type,actual,params,expected_pass",
    [
        ("status_code", {"status": 200}, {"expected": 200}, True),
        ("status_code", {"status": 200}, {"expected": [200, 201]}, True),
        ("status_code", {"status": 500}, {"expected": 200}, False),
        ("json_path", {"body": {"a": {"b": 3}}}, {"path": "$.a.b", "operator": "eq", "expected": 3}, True),
        ("json_path", {"body": {"a": {"b": None}}}, {"path": "$.a.b", "operator": "not_null"}, False),
        ("json_path", {"body": {"a": {"b": None}}}, {"path": "$.a.b", "operator": "null"}, True),
        ("json_path", {"body": {"n": 5}}, {"path": "$.n", "operator": "gte", "expected": 5}, True),
        ("json_path", {"body": {"n": 5}}, {"path": "$.n", "operator": "lt", "expected": 5}, False),
        ("json_path", {"body": {"s": "hello world"}}, {"path": "$.s", "operator": "contains", "expected": "world"}, True),
        ("json_path", {"body": {"x": 1}}, {"path": "$.nope", "operator": "exists"}, False),
        ("json_path", {"body": '{"a": 1}'}, {"path": "$.a", "operator": "eq", "expected": 1}, True),  # body 是 JSON 串
        ("type_match", 42, {"expected_type": "integer"}, True),
        ("type_match", "42", {"expected_type": "integer"}, False),
        ("contains", "hello world", {"expected": "WORLD", "case_sensitive": False}, True),
        ("contains", "hello", {"expected": "WORLD"}, False),
        ("regex", "ORD-12345678", {"pattern": r"^ORD-\d{8}$"}, True),
        ("regex", "ORD-123", {"pattern": r"^ORD-\d{8}$"}, False),
    ],
)
def test_builtin_assertions(assertion_type, actual, params, expected_pass):
    reg = build_assertions()
    res = run(reg.evaluate(assertion_type, actual, params))
    assert res.passed is expected_pass, res.message


def test_assertion_not_registered_raises():
    reg = build_assertions()
    from testing_platform.engine import AssertionNotRegisteredError

    with pytest.raises(AssertionNotRegisteredError):
        run(reg.evaluate("no_such_assertion", 1, {}))


# ── 环境继承合并 + 环检测（06 §2.3）───────────────────────────
def test_merge_env_chain_parent_then_child_overrides():
    from testing_platform.engine import EnvNotFoundError, merge_env_chain

    envs = [
        {"id": "base", "parent_id": None, "vars": {"base_url": "https://b", "timeout": 30}},
        {"id": "staging", "parent_id": "base", "vars": {"timeout": 5}},
    ]
    merged = merge_env_chain(envs, "staging")
    assert merged == {"base_url": "https://b", "timeout": 5}  # 子覆盖父
    assert merge_env_chain(envs, "base") == {"base_url": "https://b", "timeout": 30}
    with pytest.raises(EnvNotFoundError):
        merge_env_chain(envs, "nope")


def test_merge_env_chain_detects_cycle():
    from testing_platform.engine import EnvCycleError, merge_env_chain

    envs = [
        {"id": "a", "parent_id": "b", "vars": {}},
        {"id": "b", "parent_id": "a", "vars": {}},
    ]
    with pytest.raises(EnvCycleError):
        merge_env_chain(envs, "a")


# ── 错误策略状态机（06 §5）────────────────────────────────────
def test_error_policy_state_machine():
    from testing_platform.engine import EngineError, ErrorPolicy, ErrorPolicyState

    # fail：首个失败即终态
    st = ErrorPolicyState(ErrorPolicy.parse("fail"))
    d = st.on_error(EngineError("x"))
    assert (d.action, d.terminal) == ("fail", True)

    # retry(2)：前 2 次失败仍 retry，第 3 次耗尽 → fail
    st = ErrorPolicyState(ErrorPolicy.parse("retry(2)"))
    assert st.on_error(EngineError("x")).action == "retry"
    assert st.on_error(EngineError("x")).action == "retry"
    assert st.on_error(EngineError("x")).action == "fail"

    # reinit：一次救援后耗尽
    st = ErrorPolicyState(ErrorPolicy.parse("reinit"))
    assert st.on_error(EngineError("x")).action == "reinit"
    assert st.on_error(EngineError("x")).action == "fail"

    # evict：一次清理后耗尽
    st = ErrorPolicyState(ErrorPolicy.parse("evict"))
    assert st.on_error(EngineError("x")).action == "evict"
    assert st.on_error(EngineError("x")).action == "fail"

    # skip / abort 直接终态
    assert ErrorPolicyState(ErrorPolicy.parse("skip")).on_error(EngineError("x")).action == "skip"
    assert ErrorPolicyState(ErrorPolicy.parse("abort")).on_error(EngineError("x")).action == "abort"

    # 断言失败 → status_hint=fail；执行异常 → status_hint=error
    st = ErrorPolicyState(ErrorPolicy.parse("fail"))
    assert st.on_error(EngineError("x"), assertion_failed=True).status_hint == "fail"
    st = ErrorPolicyState(ErrorPolicy.parse("fail"))
    assert st.on_error(EngineError("x"), assertion_failed=False).status_hint == "error"


def test_on_error_skip_continues_scenario():
    """on_error=skip：步骤跳过，后续步骤继续执行。"""
    scenario = {
        "id": "SCN-SKIP",
        "steps": [
            {
                "step_key": "first",
                "seq": 10,
                "type": "request",
                "on_error": "skip",
                "params": {"method": "GET", "url": "/fail"},
                "post_checks": [{"type": "status_code", "expected": 999}],  # 必失败
            },
            {
                "step_key": "second",
                "seq": 20,
                "type": "request",
                "params": {"method": "GET", "url": "/ok"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            },
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-SKIP": scenario}), FakeHttp())
    result = run(executor.execute("SCN-SKIP", ExecutionContext()))
    assert result.status == STATUS_PASS
    assert result.steps[0].status == STEP_SKIPPED
    assert result.steps[1].status == STEP_PASS


def test_on_error_abort_terminates_scenario():
    scenario = {
        "id": "SCN-ABORT",
        "steps": [
            {
                "step_key": "first",
                "seq": 10,
                "type": "request",
                "on_error": "abort",
                "params": {"method": "GET", "url": "/fail"},
                "post_checks": [{"type": "status_code", "expected": 999}],
            },
            {"step_key": "never", "seq": 20, "type": "request",
             "params": {"method": "GET", "url": "/ok"},
             "post_checks": [{"type": "status_code", "expected": 200}]},
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-ABORT": scenario}), FakeHttp())
    result = run(executor.execute("SCN-ABORT", ExecutionContext()))
    assert result.status == STATUS_FAIL
    assert result.error["kind"] == "aborted"
    assert len(result.steps) == 1
    assert result.steps[0].error["policy_action"] == "abort"


def test_on_error_retry_exhausted():
    """retry(1) 用尽 → step error（非断言失败），attempt 记录 2。"""
    fake = FakeHttp()
    fake.fail_times = 99  # 永远失败
    scenario = {
        "id": "SCN-RETRY",
        "steps": [
            {
                "step_key": "flaky",
                "seq": 10,
                "type": "request",
                "on_error": "retry(1)",
                "params": {"method": "GET", "url": "/x"},
                "post_checks": [{"type": "status_code", "expected": 200}],
            }
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-RETRY": scenario}), fake)
    result = run(executor.execute("SCN-RETRY", ExecutionContext()))
    assert result.status == STATUS_ERROR
    assert result.steps[0].status == STEP_ERROR
    assert result.steps[0].attempt == 2
    assert result.steps[0].error["kind"] == "tool_execution"
    assert fake.calls and len(fake.calls) == 2  # 首次 + 1 次重试


def test_design_yaml_shape_tool_and_assertions():
    """兼容 design §19.1 的交换格式：步骤用 `tool` + `assertions` 字段。"""
    scenario = {
        "id": "SCN-YAML",
        "local_vars": {"order_id": None},
        "steps": [
            {
                "id": "create_order",
                "tool": "http",
                "params": {"method": "POST", "url": "{{env.base_url}}/api/v1/orders"},
                "assertions": [
                    {"type": "status_code", "expected": 201},
                    {"type": "json_path", "path": "$.data.order_id", "operator": "not_null"},
                ],
                "post_cond": [{"set": "{{local.order_id}} = {{response.json.data.order_id}}"}],
                "on_error": "retry(2)",
            }
        ],
    }
    executor = make_executor(DictScenarioLoader({"SCN-YAML": scenario}), FakeHttp())
    ctx = ExecutionContext()
    ctx.resolver.set_scope("env", {"base_url": "https://api.example.com"})
    result = run(executor.execute("SCN-YAML", ctx))
    assert result.status == STATUS_PASS
    assert result.local_vars["order_id"] == "ORD-1"
    assert result.steps[0].checks[0]["type"] == "status_code"


def test_secret_variables_masked():
    """is_secret 变量在证据/结果里脱敏（06 §2.5）。"""
    from testing_platform.engine import Redactor

    ctx = ExecutionContext()
    ctx.register_secrets({"{{env.token}}": "sk-super-secret"})
    red = ctx.redactor()
    assert red.text("Bearer sk-super-secret") == "Bearer ***"
    assert red.obj({"Authorization": "sk-super-secret"}) == {"Authorization": "***"}
    assert red.text("nothing to hide") == "nothing to hide"


# ── 异步适配 ──────────────────────────────────────────────────
def test_maybe_await_adapts_sync_and_async():
    def sync_fn():
        return 1

    async def async_fn():
        return 2

    async def main():
        assert await maybe_await(sync_fn()) == 1          # 普通值
        assert await maybe_await(async_fn()) == 2          # awaitable
        assert await run_sync_or_async(sync_fn) == 1       # 同步函数 → 线程池
        assert await run_sync_or_async(async_fn) == 2      # 异步函数 → 直接 await

    run(main())


def test_engine_never_calls_llm():
    """确定性护栏：engine 包不 import 任何 agent / LLM 相关模块。"""
    import testing_platform.engine as engine_pkg

    src_dir = os.path.dirname(os.path.abspath(engine_pkg.__file__))
    forbidden = ("import openai", "import anthropic", "from agent", "import agent", "from providers")
    for name in os.listdir(src_dir):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src_dir, name), "r", encoding="utf-8") as fh:
            content = fh.read().lower()
        for token in forbidden:
            assert token not in content, f"engine/{name} 出现禁用依赖：{token!r}"


# ── 11) 结果落库：PgResultWriter 用假 PG 池验证 SQL 路径 ──────
class _FakeConn:
    def __init__(self, sink):
        self._sink = sink

    async def execute(self, *args):
        sql = args[0]
        params = args[1:]
        self._sink.append((sql, params))
        return "OK"


class _FakePool:
    def __init__(self, sink):
        self._sink = sink

    class _Ctx:
        def __init__(self, sink):
            self._sink = sink

        async def __aenter__(self):
            return _FakeConn(self._sink)

        async def __aexit__(self, *exc):
            return False

    def acquire(self):
        return _FakePool._Ctx(self._sink)


def test_pg_result_writer_writes_step_rows():
    from testing_platform.engine import PgResultWriter, ScenarioResult, StepResult

    db = pytest.importorskip("db")
    statements: list = []
    original = db.PostgresClient.pool
    db.PostgresClient.pool = _FakePool(statements)
    try:
        writer = PgResultWriter(run_id="11111111-1111-1111-1111-111111111111")
        result = ScenarioResult(
            scenario_id="SCN-0001",
            status="pass",
            steps=[
                StepResult(
                    step_key="create_order", seq=10, type="request", status="pass",
                    attempt=1, expected=[201], actual=[201], diff=None, duration_ms=5,
                    checks=[{"type": "status_code", "passed": True}],
                )
            ],
        )
        run(writer.write(result, ExecutionContext()))
    finally:
        db.PostgresClient.pool = original

    assert statements, "没有执行任何 INSERT"
    tables = [sql.split()[2] for sql, _ in statements if sql.strip().startswith("INSERT")]
    assert "mc_test_run_scenario" in tables
    assert "mc_test_step_result" in tables
    # 步骤行带 expected / actual / diff
    step_sql, step_params = next(
        (sql, params)
        for sql, params in statements
        if "mc_test_step_result" in sql and sql.strip().startswith("INSERT")
    )
    assert "$7::jsonb" in step_sql and "$9::jsonb" in step_sql
    assert step_params[6] == "[201]"          # expected JSONB
    assert step_params[7] == "[201]"          # actual JSONB
    assert step_params[8] == "null"           # diff JSONB（全过）
    assert step_params[9] == 5                # duration_ms


# ── 流程控制步骤（01 §4.2 扩展：if/else_if/else/end_if/break/end_scenario）──
def _cf_scenario(steps, local_vars=None):
    return {"id": "SCN-CF", "code": "SCN-CF", "local_vars": local_vars or {}, "steps": steps}


CF_STEPS = [
    {"step_key": "cond", "seq": 10, "type": "if", "params": {"condition": "{{local.flag}}"}},
    {"step_key": "then_hit", "seq": 20, "type": "request",
     "params": {"method": "GET", "url": "/then"},
     "post_checks": [{"type": "status_code", "expected": 200}]},
    {"step_key": "else_open", "seq": 30, "type": "else"},
    {"step_key": "else_hit", "seq": 40, "type": "request",
     "params": {"method": "GET", "url": "/else"},
     "post_checks": [{"type": "status_code", "expected": 200}]},
    {"step_key": "cond_end", "seq": 50, "type": "end_if"},
    {"step_key": "tail", "seq": 60, "type": "request",
     "params": {"method": "GET", "url": "/tail"},
     "post_checks": [{"type": "status_code", "expected": 200}]},
]


def test_control_flow_if_true_runs_then_branch():
    """if 命中 → then 分支执行、else 分支跳过、end_if 闭合后恢复正常。"""
    fake = FakeHttp()
    scenario = _cf_scenario(CF_STEPS, {"flag": True})
    executor = make_executor(DictScenarioLoader({"SCN-CF": scenario}), fake)

    result = run(executor.execute("SCN-CF", ExecutionContext()))

    assert result.status == STATUS_PASS
    by_key = {s.step_key: s.status for s in result.steps}
    assert by_key["cond"] == STEP_PASS                       # if 自身：命中=pass
    assert by_key["then_hit"] == STEP_PASS
    assert by_key["else_open"] == STEP_SKIPPED               # else 帧：未接管
    assert by_key["else_hit"] == STEP_SKIPPED
    assert by_key["cond_end"] == STEP_PASS                   # end_if 结构闭合
    assert by_key["tail"] == STEP_PASS                       # 块外步骤照常执行


def test_control_flow_if_false_takes_else_branch():
    """if 未命中 → then 跳过、else 分支执行。"""
    fake = FakeHttp()
    scenario = _cf_scenario(CF_STEPS, {"flag": False})
    executor = make_executor(DictScenarioLoader({"SCN-CF": scenario}), fake)

    result = run(executor.execute("SCN-CF", ExecutionContext()))

    by_key = {s.step_key: s.status for s in result.steps}
    assert by_key["cond"] == STEP_SKIPPED
    assert by_key["then_hit"] == STEP_SKIPPED
    assert by_key["else_hit"] == STEP_PASS
    assert by_key["tail"] == STEP_PASS
    assert result.status == STATUS_PASS


def test_control_flow_else_if_fallthrough_and_break():
    """else_if 链：前面命中则后续 else_if/else 全跳；break 跳到 end_if。"""
    steps = [
        {"step_key": "i", "seq": 10, "type": "if", "params": {"condition": "{{local.n}} == 1"}},
        {"step_key": "b1", "seq": 20, "type": "request", "params": {"method": "GET", "url": "/one"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
        {"step_key": "ei", "seq": 30, "type": "else_if", "params": {"condition": "{{local.n}} == 2"}},
        {"step_key": "b2", "seq": 40, "type": "request", "params": {"method": "GET", "url": "/two"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
        {"step_key": "brk", "seq": 50, "type": "break"},
        {"step_key": "b3", "seq": 60, "type": "request", "params": {"method": "GET", "url": "/after-break"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
        {"step_key": "ei2", "seq": 70, "type": "else_if", "params": {"condition": "{{local.n}} == 2"}},
        {"step_key": "b4", "seq": 80, "type": "request", "params": {"method": "GET", "url": "/skipped"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
        {"step_key": "en", "seq": 90, "type": "end_if"},
        {"step_key": "tail", "seq": 100, "type": "request", "params": {"method": "GET", "url": "/tail"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
    ]
    fake = FakeHttp()
    scenario = _cf_scenario(steps, {"n": 2})
    executor = make_executor(DictScenarioLoader({"SCN-CF": scenario}), fake)

    result = run(executor.execute("SCN-CF", ExecutionContext()))

    by_key = {s.step_key: s.status for s in result.steps}
    assert by_key["b1"] == STEP_SKIPPED          # n==1 不成立
    assert by_key["ei"] == STEP_PASS             # n==2 命中 else_if
    assert by_key["b2"] == STEP_PASS
    assert by_key["b3"] == STEP_SKIPPED          # break 跳过块内剩余
    assert by_key["ei2"] == STEP_SKIPPED         # 已有命中分支
    assert by_key["b4"] == STEP_SKIPPED
    assert by_key["en"] == STEP_PASS
    assert by_key["tail"] == STEP_PASS           # end_if 之后照常
    assert result.status == STATUS_PASS


def test_control_flow_end_scenario_stops_early():
    """end_scenario：立即结束场景，后续步骤不执行，场景判 pass + early_exit。"""
    steps = [
        {"step_key": "a", "seq": 10, "type": "request", "params": {"method": "GET", "url": "/a"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
        {"step_key": "stop", "seq": 20, "type": "end_scenario", "params": {"reason": "提前收尾"}},
        {"step_key": "b", "seq": 30, "type": "request", "params": {"method": "GET", "url": "/b"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
    ]
    fake = FakeHttp()
    scenario = _cf_scenario(steps)
    executor = make_executor(DictScenarioLoader({"SCN-CF": scenario}), fake)

    result = run(executor.execute("SCN-CF", ExecutionContext()))

    assert [s.step_key for s in result.steps] == ["a", "stop"]
    assert result.status == STATUS_PASS
    assert result.early_exit and result.early_exit["after"] == "stop"


def test_control_flow_condition_error_skips_branch_and_reports():
    """if 条件引用未定义变量 → 分支未命中（不炸主循环），错误挂在控制步骤上。"""
    steps = [
        {"step_key": "i", "seq": 10, "type": "if", "params": {"condition": "{{local.ghost}} == 1"}},
        {"step_key": "a", "seq": 20, "type": "request", "params": {"method": "GET", "url": "/a"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
        {"step_key": "e", "seq": 30, "type": "end_if"},
        {"step_key": "t", "seq": 40, "type": "request", "params": {"method": "GET", "url": "/t"},
         "post_checks": [{"type": "status_code", "expected": 200}]},
    ]
    fake = FakeHttp()
    scenario = _cf_scenario(steps)
    executor = make_executor(DictScenarioLoader({"SCN-CF": scenario}), fake)

    result = run(executor.execute("SCN-CF", ExecutionContext()))

    by_key = {s.step_key: s for s in result.steps}
    assert by_key["i"].status == STEP_SKIPPED
    assert by_key["i"].error is not None                     # 求值错误可见（不静默）
    assert by_key["a"].status == STEP_SKIPPED
    assert by_key["t"].status == STEP_PASS
