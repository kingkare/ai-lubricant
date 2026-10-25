"""失败分流（triage）—— design §04 §4 + §18。

把执行引擎的 ``failure_kind``（``script_issue`` / ``product_bug`` /
``case_outdated`` / ``env_flaky``）映射到处理动作，并计算失败指纹用于去重。

**纯函数 / 常量**——本模块无副作用、无 DB、无 LLM：分流判据由确定性优先级
给出（design §04 §4「重跑一次 → 比对知识库预期 → 判失败层 → 不确定才交 AI」），
本模块只做**静态映射**（最后一步 AI 归类在上游完成，传入已是确定 kind）。

对外表面::

    outcome = classify_failure("product_bug")
    fp = compute_fingerprint(run_scenario_id, error_sig, step_type, diff)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .models import DefectValidationError

#: 引擎 ``failure_kind`` 取值（design §04 §1.2 / §04 §4 分流）
FAILURE_KINDS: tuple[str, ...] = (
    "script_issue",  # 定位/时序/断言写法问题
    "product_bug",   # 断言合理但系统行为不符
    "case_outdated",  # 需求已变，预期过时
    "env_flaky",     # 重跑即过
)

# 处理动作（design §04 §4 表「处理」列）
ACTION_SELF_HEAL = "self_heal"        # 自愈（hybrid 改场景/步骤，不改被测代码）
ACTION_HANDOFF = "handoff"            # 转人工（自愈预算耗尽 / 不确定）
ACTION_CREATE_BUG = "create_bug"      # 建 ProjectIssue(issue_type=bug) + ext
ACTION_DEPRECATE_CASE = "deprecate_case"  # 标用例 deprecated
ACTION_MARK_FLAKY = "mark_flaky"      # 标 run_scenario flaky，不计失败


@dataclass(frozen=True)
class TriageOutcome:
    """分流结果——``failure_kind`` → 处理动作的纯值对象。

    ``counts_as_failure``：是否计入失败（``env_flaky`` / ``script_issue`` /
    ``case_outdated`` 不计产品失败，只有 ``product_bug`` 计）。
    ``target``：动作作用对象（scenario / issue / case / run_scenario）。
    """

    kind: str
    action: str
    counts_as_failure: bool
    target: str
    label: str


#: 静态分流映射（design §04 §4 表）。改这张表 = 改分流策略。
TRIAGE_OUTCOMES: dict[str, TriageOutcome] = {
    "script_issue": TriageOutcome(
        kind="script_issue",
        action=ACTION_SELF_HEAL,
        counts_as_failure=False,  # 脚本层问题，非产品失败
        target="scenario",
        label="脚本问题（定位/时序/断言写法）→ 自愈或转人工",
    ),
    "product_bug": TriageOutcome(
        kind="product_bug",
        action=ACTION_CREATE_BUG,
        counts_as_failure=True,  # 真失败——建 bug
        target="bug",
        label="产品缺陷（断言合理但系统行为不符）→ 建测试域缺陷（mc_test_bug）",
    ),
    "case_outdated": TriageOutcome(
        kind="case_outdated",
        action=ACTION_DEPRECATE_CASE,
        counts_as_failure=False,  # 预期过时，非产品失败
        target="case",
        label="用例过时（需求已变，预期过时）→ 标用例 deprecated",
    ),
    "env_flaky": TriageOutcome(
        kind="env_flaky",
        action=ACTION_MARK_FLAKY,
        counts_as_failure=False,  # 重跑即过，不计失败
        target="run_scenario",
        label="环境抖动（重跑即过）→ 标 flaky，不计失败",
    ),
}


def classify_failure(kind: str, *, retry_passed: bool | None = None) -> TriageOutcome:
    """把引擎 ``failure_kind`` 映射到处理动作。

    纯函数 / 常量表查找——无副作用。

    Args:
        kind: ``FAILURE_KINDS`` 之一。
        retry_passed: 可选——重跑一次的结果（design §04 §4 判据顺序 #1）。
            ``True`` 表示重跑即过：无论 kind 为何，统一判 ``env_flaky``
            （确定性优先：重跑过的失败不计产品失败）。
            ``None`` / ``False``：按 ``kind`` 静态映射。

    Returns:
        :class:`TriageOutcome`。

    Raises:
        DefectValidationError: ``kind`` 不在 :data:`FAILURE_KINDS` 内。
    """
    if kind not in FAILURE_KINDS:
        raise DefectValidationError(
            f"未知 failure_kind={kind!r}（可选值：{'/'.join(FAILURE_KINDS)}）"
        )
    # 判据顺序 #1（确定性优先）：重跑即过 → 统一判 flaky。
    if retry_passed is True:
        return TRIAGE_OUTCOMES["env_flaky"]
    return TRIAGE_OUTCOMES[kind]


# ── 失败指纹（去重）─────────────────────────────────────────────
def _canonical(value: Any) -> str:
    """归一为可比对字符串：去多余空白，UUID/datetime 不特殊处理（按 str）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, Mapping):
        return json.dumps(
            {str(k): _canonical(v) for k, v in value.items()},
            sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"),
        )
    if isinstance(value, (list, tuple, set)):
        return json.dumps(
            [_canonical(v) for v in value],
            ensure_ascii=False, default=str, separators=(",", ":"),
        )
    return str(value)


def _extract_diff_paths(node: Any, out: list[str] | None = None) -> list[str]:
    """从响应 diff 中抽取"差异路径"特征（不含值，只含哪里不同）。

    支持的 diff 形态（断言库产出）：
    * ``{"path": "$.data.id", "expected": ..., "actual": ...}`` → 取 ``path``
    * ``[{"path": ...}, ...]`` → 取每条 ``path``
    * ``{"$.data.id": {"expected": ..., "actual": ...}, ...}`` → 取键（路径）
    退化：无可识别路径 → 返回空列表，调用方走整体 JSON 兜底。
    """
    if out is None:
        out = []
    if isinstance(node, Mapping):
        if "path" in node and ("expected" in node or "actual" in node):
            out.append(str(node["path"]))
        elif node and all(
            isinstance(v, Mapping) and ("expected" in v or "actual" in v)
            for v in node.values()
        ):
            out.extend(str(k) for k in node)
        else:
            for v in node.values():
                _extract_diff_paths(v, out)
    elif isinstance(node, (list, tuple, set)):
        for v in node:
            _extract_diff_paths(v, out)
    return out


def _diff_feature(response_diff: Any) -> str:
    """响应 diff 的"特征"——优先取差异路径集，退化到整体归一 JSON。

    只取**哪里不同**（路径），不取**差在哪里**（值），让同一处断言失败的
    不同响应值归并到同一指纹（design §04 §4 去重语义）。
    """
    if response_diff is None:
        return ""
    paths = _extract_diff_paths(response_diff)
    if paths:
        # 去重 + 排序，稳定指纹。
        return "p:" + ",".join(sorted(set(paths)))
    # 退化：无可识别路径 → 整体归一（仍确定性）。
    return "j:" + _canonical(response_diff)


def compute_fingerprint(
    run_scenario_id: Any,
    error_signature: Any,
    step_type: Any,
    response_diff: Any,
) -> str:
    """计算失败指纹（去重用）。

    design §04 §4：``hash(错误签名 + 步骤类型 + 响应 diff 特征)``——相同指纹
    归并到同一 bug，不重复建 issue。

    .. note::
        ``run_scenario_id`` 是**调用上下文**（来源执行行），**不参与哈希**——
        否则跨 run 去重失效（同一场景多次执行会产生不同 run_scenario_id，
        若混入哈希则每次都判为新 bug，与"归并到同一 bug"的语义矛盾）。
        来源执行记录在 :class:`~testing_platform.defects.models.TestBug`
        的 ``from_run_scenario_id`` 字段，与指纹分离。指纹须**跨 run 稳定**。

    Args:
        run_scenario_id: 来源执行行 id（仅上下文，不进哈希）。
        error_signature: 错误签名（断言失败信息 / 异常类型+消息归一串）。
        step_type: 失败步骤的类型（``request``/``page``/...，§4.2）。
        response_diff: 响应 diff（断言库产出，dict/list/str 皆可）。

    Returns:
        sha256 hex（64 字符）——与 ``mc_test_bug.fingerprint`` 列宽对齐。
    """
    payload = "|".join(
        (
            _canonical(error_signature),
            _canonical(step_type),
            _diff_feature(response_diff),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ACTION_DEPRECATE_CASE",
    "ACTION_CREATE_BUG",
    "ACTION_HANDOFF",
    "ACTION_MARK_FLAKY",
    "ACTION_SELF_HEAL",
    "FAILURE_KINDS",
    "TRIAGE_OUTCOMES",
    "TriageOutcome",
    "classify_failure",
    "compute_fingerprint",
]
