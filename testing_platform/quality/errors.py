"""校验器结构化错误模型（07 §1：{path, rule, msg, severity}）。

「AI 不裁判，框架裁判」——校验器对每个入库资产跑确定性规则，
输出统一的 `ValidationIssue` / `ValidationResult`：

    ValidationIssue{path, rule, msg, severity(error|warn), hint}
    ValidationResult{valid, issues[]}

- `severity=error` → 拒入库
- `severity=warn`  → 入库但标记（07 §7 决策 2：可「忽略并存」，需记录理由）
本轮（v1）只产生 error 级问题；warn 规则留待后续实现。

文档出处：docs/ai-testing-platform/plans/07-validators-and-gates.md §1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

#: 问题严重级别（07 §1）
SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

#: 合法级别集合（防手滑传错）
_SEVERITIES = (SEVERITY_ERROR, SEVERITY_WARN)


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验问题。

    :param path: 资产文档内的定位路径（如 ``$.steps[1].params.url``）
    :param rule: 命中的规则编号（如 ``R5_post_checks_required``，对齐 07 §2）
    :param msg: 人类可读的问题描述（中文）
    :param severity: ``error``（拒入库）或 ``warn``（入库但标记）
    :param hint: 修复提示（可选，面向 AI/用户的动作建议）
    """

    path: str
    rule: str
    msg: str
    severity: str = SEVERITY_ERROR
    hint: Optional[str] = None

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITIES:
            object.__setattr__(self, "severity", SEVERITY_ERROR)

    def to_dict(self) -> dict:
        """JSON 形态（07 §1）：{path, rule, msg, severity, hint}。"""
        return {
            "path": self.path,
            "rule": self.rule,
            "msg": self.msg,
            "severity": self.severity,
            "hint": self.hint,
        }


@dataclass
class ValidationResult:
    """一次资产校验的汇总结果（07 §1：{valid, issues[]}）。

    ``valid`` 只取决于是否存在 error 级问题；warn 不阻断入库。
    """

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """是否存在 error 级问题；有 error 则资产不过门禁。"""
        return not any(issue.severity == SEVERITY_ERROR for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARN]

    def add(
        self,
        path: str,
        rule: str,
        msg: str,
        severity: str = SEVERITY_ERROR,
        hint: Optional[str] = None,
    ) -> "ValidationResult":
        self.issues.append(ValidationIssue(path=path, rule=rule, msg=msg, severity=severity, hint=hint))
        return self

    def merge(self, other: "ValidationResult") -> "ValidationResult":
        self.issues.extend(other.issues)
        return self

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "issues": [i.to_dict() for i in self.issues],
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ValidationResult(valid={self.valid}, issues={len(self.issues)})"


__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_WARN",
    "ValidationIssue",
    "ValidationResult",
]