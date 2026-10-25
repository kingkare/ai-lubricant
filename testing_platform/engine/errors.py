"""引擎错误类型 + `on_error` 错误策略状态机（06 §5）。

策略枚举（06 §5 / design §19.7）：

| on_error | 行为 |
|---|---|
| retry(n) | 原地重试 n 次 |
| reinit   | 重跑传输层鉴权后再试一次 |
| evict    | 清理会话重开后再试一次 |
| skip     | 跳过该步，继续后续 |
| abort    | 终止整个场景 |
| fail     | 直接失败（默认） |

`ErrorPolicyState` 是**纯状态机**：输入一次失败 + 是否断言失败，
输出下一步动作与最终状态。不含 IO、不含 AI。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# ── 动作 / 策略枚举 ──────────────────────────────────────────────
Action = Literal["retry", "reinit", "evict", "skip", "abort", "fail"]
PolicyKind = Literal["retry", "reinit", "evict", "skip", "abort", "fail"]

POLICY_KINDS: tuple[PolicyKind, ...] = ("retry", "reinit", "evict", "skip", "abort", "fail")

# `retry(2)` / `retry` / `fail` —— 允许带括号次数
_POLICY_RE = re.compile(r"^\s*(?P<kind>[a-z_]+)\s*(?:\(\s*(?P<n>\d+)\s*\))?\s*$")


# ── 错误类型 ────────────────────────────────────────────────────
class EngineError(Exception):
    """引擎错误基类：所有可预期、可归类的执行错误都继承它。"""

    kind = "engine_error"

    def __init__(self, message: str, *, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": str(self), "details": self.details}


class ScenarioNotFoundError(EngineError):
    kind = "scenario_not_found"


class StepNotFoundError(EngineError):
    kind = "step_not_found"


class EnvCycleError(EngineError):
    """环境继承链出现环（06 §2.3 检测环）。"""

    kind = "env_cycle"


class EnvNotFoundError(EngineError):
    kind = "env_not_found"


class UndefinedVariableError(EngineError):
    """未定义变量（06 §2.2：不静默变字符串，直接报错）。"""

    kind = "undefined_variable"

    def __init__(self, path: str, *, step_key: str | None = None, scope: str | None = None) -> None:
        self.path = path
        self.step_key = step_key
        self.scope = scope
        where = f"（步骤 {step_key}）" if step_key else ""
        hint = f"（作用域 {scope}）" if scope else ""
        super().__init__(f"未定义变量 {path}{hint}{where}")


class TemplateError(EngineError):
    kind = "template"


class ConditionError(EngineError):
    kind = "condition"


class ToolNotRegisteredError(EngineError):
    """步骤类型对应的工具未注册（browser / mobile / custom 等 stub）。"""

    kind = "tool_not_registered"


class AssertionNotRegisteredError(EngineError):
    """后置校验类型未注册。"""

    kind = "assertion_not_registered"


class MethodNotRegisteredError(EngineError):
    """方法脚本（method_ref）未注册。"""

    kind = "method_not_registered"


class ToolExecutionError(EngineError):
    """工具内部执行失败（网络异常、超时、参数非法等）。"""

    kind = "tool_execution"


class StepParamsError(EngineError):
    """步骤参数非法（缺必填字段、类型不符）。"""

    kind = "step_params"


class CheckError(EngineError):
    """后置校验失败（断言不通过）。携带断言结果用于写 step_result。"""

    kind = "check_failed"

    def __init__(self, results: Any, *, step_key: str | None = None) -> None:
        if not isinstance(results, (list, tuple)):
            results = [results]
        self.results = list(results)
        self.step_key = step_key
        first = self.results[0] if self.results else None
        msg = getattr(first, "message", None) or "后置校验失败"
        super().__init__(msg)

    @property
    def result(self):
        return self.results[0] if self.results else None

    @property
    def failed_results(self) -> list:
        return [r for r in self.results if not getattr(r, "passed", False)]


class ScenarioAbort(EngineError):
    """场景被 abort 策略或 early_exit 显式终止。"""

    kind = "abort"


# ── 策略解析 ────────────────────────────────────────────────────
@dataclass(frozen=True)
class ErrorPolicy:
    """一次步骤的错误策略（可含重试次数）。"""

    kind: PolicyKind = "fail"
    retries: int = 0
    raw: Any = "fail"

    @staticmethod
    def parse(raw: Any) -> "ErrorPolicy":
        """把 `on_error` 字段解析为 ErrorPolicy。

        支持：None → fail；`"fail"` / `"retry(2)"` / `{"kind":"retry","retries":2}`。
        """
        if raw is None:
            return ErrorPolicy(kind="fail", retries=0, raw=raw)
        if isinstance(raw, ErrorPolicy):
            return raw
        if isinstance(raw, dict):
            kind = str(raw.get("kind", raw.get("on_error", "fail"))).strip().lower()
            retries = int(raw.get("retries", raw.get("times", raw.get("n", 0)) or 0))
            policy = _policy_from_parts(kind, retries)
            return ErrorPolicy(kind=policy.kind, retries=policy.retries, raw=raw)
        if isinstance(raw, str):
            m = _POLICY_RE.match(raw)
            if not m:
                raise StepParamsError(f"非法 on_error 策略：{raw!r}（合法值：{'|'.join(POLICY_KINDS)}(n)）")
            return _policy_from_parts(m.group("kind"), int(m.group("n") or 0), raw=raw)
        raise StepParamsError(f"非法 on_error 类型：{type(raw).__name__}")

    @property
    def retries_left(self) -> int:
        return max(0, self.retries)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "retries": self.retries}


def _policy_from_parts(kind: str, retries: int, raw: Any = None) -> ErrorPolicy:
    kind = kind.strip().lower()
    if kind == "rerun":  # 口径别名
        kind = "reinit"
    if kind not in POLICY_KINDS:
        raise StepParamsError(f"非法 on_error 策略：{kind!r}（合法值：{'|'.join(POLICY_KINDS)}(n)）")
    if kind != "retry":
        retries = 0
    return ErrorPolicy(kind=kind, retries=max(0, retries), raw=raw if raw is not None else kind)


# ── 状态机 ──────────────────────────────────────────────────────
@dataclass
class ErrorPolicyState:
    """错误策略状态机：跟踪重试 / reinit / evict 的消耗次数。

    用法::

        state = ErrorPolicyState(ErrorPolicy.parse("retry(2)"))
        while True:
            try:
                ...
            except Exception as exc:
                decision = state.on_error(exc, assertion_failed=is_check)
                if decision.action != "retry":
                    ...  # skip / abort / fail 终态
                # action == retry → 循环重试
    """

    policy: ErrorPolicy
    attempts: int = 0          # 已消耗的执行次数（含首次）
    failures: int = 0          # 已发生的失败次数
    reinit_used: bool = False
    evict_used: bool = False
    history: list[dict] = field(default_factory=list)

    # 重新初始化 / 清会话各最多一次（防止 reinit/evict 无限循环）
    MAX_REINIT = 1
    MAX_EVICT = 1

    @property
    def is_terminal(self) -> bool:
        return bool(self.history) and self.history[-1]["action"] in ("skip", "abort", "fail")

    @property
    def status_hint(self) -> str:
        """状态机当前失败应写入的状态（断言失败=fail，执行异常=error）。"""
        return self.history[-1].get("status_hint", "fail") if self.history else "pass"

    def begin_attempt(self) -> int:
        self.attempts += 1
        return self.attempts

    def on_error(self, exc: BaseException, *, assertion_failed: bool = False) -> "Decision":
        """记录一次失败，返回下一步动作。"""
        self.failures += 1
        kind = self.policy.kind
        status_hint = "fail" if assertion_failed else "error"
        error_kind = getattr(exc, "kind", None) or type(exc).__name__
        action: Action = "fail"
        remaining = 0

        if kind == "retry":
            remaining = self.policy.retries_left - (self.failures - 1)
            action = "retry" if remaining > 0 else "fail"
        elif kind == "reinit":
            if not self.reinit_used:
                self.reinit_used = True
                action = "reinit"
            else:
                action = "fail"
        elif kind == "evict":
            if not self.evict_used:
                self.evict_used = True
                action = "evict"
            else:
                action = "fail"
        elif kind == "skip":
            action = "skip"
        elif kind == "abort":
            action = "abort"
        elif kind == "fail":
            action = "fail"

        decision = Decision(
            action=action,
            status_hint=status_hint,
            attempt=self.attempts,
            failure=self.failures,
            retries_left=max(0, remaining if kind == "retry" else 0),
            policy=self.policy,
            error_kind=error_kind,
            error_message=str(exc),
            assertion_failed=assertion_failed,
        )
        self.history.append(decision.to_dict())
        return decision


@dataclass(frozen=True)
class Decision:
    """状态机的一次输出：接下来该干什么。"""

    action: Action
    status_hint: str            # 该次失败对应写入 step_result 的状态：fail / error
    attempt: int
    failure: int
    retries_left: int
    policy: ErrorPolicy
    error_kind: str
    error_message: str
    assertion_failed: bool

    @property
    def terminal(self) -> bool:
        return self.action in ("skip", "abort", "fail")

    @property
    def should_retry(self) -> bool:
        return self.action in ("retry", "reinit", "evict")

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "status_hint": self.status_hint,
            "attempt": self.attempt,
            "failure": self.failure,
            "retries_left": self.retries_left,
            "policy": self.policy.to_dict(),
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "assertion_failed": self.assertion_failed,
        }
