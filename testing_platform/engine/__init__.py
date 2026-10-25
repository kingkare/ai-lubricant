"""AI 测试平台 · 场景引擎（确定性，不调 LLM）。

读场景 → 逐步骤执行（调工具）→ 跑后置校验 → 写结果（04 §2）。
引擎是**纯确定性**模块（06 §0）：AI 只能改场景，改完重新过校验门 + 重跑。
"""

from .aio import maybe_await, run_sync_or_async, now_ms
from .errors import (
    CheckError,
    Decision,
    EngineError,
    EnvCycleError,
    EnvNotFoundError,
    ErrorPolicy,
    ErrorPolicyState,
    ScenarioAbort,
    ScenarioNotFoundError,
    StepParamsError,
    ToolExecutionError,
    ToolNotRegisteredError,
    UndefinedVariableError,
)
from .variables import (
    ExecutionContext,
    Redactor,
    VariableResolver,
    SCOPE_PRIORITY,
    SCOPE_ALIASES,
    apply_assignment,
    aapply_assignment,
    ainterpolate_params,
    eval_expression,
    interpolate,
    interpolate_params,
    merge_env_chain,
)
from .assertions import (
    Assertion,
    AssertionRegistry,
    AssertionResult,
    AssertionNotRegisteredError,
    build_default_registry as build_default_assertion_registry,
    default_registry as default_assertion_registry,
    register_assertion,
)
from .tools import (
    Tool,
    ToolRegistry,
    ToolResult,
    build_default_registry as build_default_tool_registry,
    default_registry as default_tool_registry,
    register_tool,
)
from .executor import (
    DictScenarioLoader,
    PgResultWriter,
    ScenarioExecutor,
    ScenarioLoader,
    ScenarioResult,
    StepResult,
    STATUS_PASS,
    STATUS_FAIL,
    STATUS_ERROR,
    STATUS_FLAKY,
    STATUS_SKIPPED,
    STEP_PASS,
    STEP_FAIL,
    STEP_ERROR,
    STEP_SKIPPED,
)

__all__ = [
    # 异步适配
    "maybe_await",
    "run_sync_or_async",
    "now_ms",
    # 错误与错误策略
    "EngineError",
    "CheckError",
    "Decision",
    "ErrorPolicy",
    "ErrorPolicyState",
    "ScenarioAbort",
    "ScenarioNotFoundError",
    "StepParamsError",
    "ToolExecutionError",
    "ToolNotRegisteredError",
    "UndefinedVariableError",
    "EnvCycleError",
    "EnvNotFoundError",
    # 变量系统
    "ExecutionContext",
    "VariableResolver",
    "Redactor",
    "SCOPE_PRIORITY",
    "SCOPE_ALIASES",
    "apply_assignment",
    "aapply_assignment",
    "ainterpolate_params",
    "eval_expression",
    "interpolate",
    "interpolate_params",
    "merge_env_chain",
    # 断言库
    "Assertion",
    "AssertionRegistry",
    "AssertionResult",
    "AssertionNotRegisteredError",
    "build_default_assertion_registry",
    "default_assertion_registry",
    "register_assertion",
    # 工具注册表
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_default_tool_registry",
    "default_tool_registry",
    "register_tool",
    # 执行器
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

__version__ = "0.1.0"