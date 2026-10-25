"""测试平台 · 执行编排规划层（25 号文「执行」环节，P2 环节7 落地位）。

事实源：``docs/ai-testing-platform/plans/25-case-generation-complete-design.md``
「## 环节 8 · 执行」（正文 350-368 行；任务书按交付顺序称「环节7」，正文里场景化是
环节 7、执行是环节 8——本模块实现的是**执行**环节的四件事：波次 / 隔离区 /
并行准入 / 失败四分类 + 结果四态映射）。

步骤链（正文 354 行原文顺序）：

    sql 红线闸 → 波次=A/B/C（冒烟硬挂停后波）→ flaky 隔离区先滤
    → 并行准入（数据范围不相交；设备一机一锁）→ 失败四分类（网络类退避重试、
      断言失败不重试、限流等待、flaky 抖动×2、Retry Budget 封顶）→ 熔断只跳依赖方

**这是规划器，不是执行器**。本模块只做「怎么跑」的决策（分波、停不停、重试不重试、
能不能并行、结果算哪一态），**不发起任何执行、不写库、不 import engine/tasks/ORM**；
真正的执行仍走 ``surface.methods.run_execute`` → :class:`ScenarioExecutor`。
纯标准库 + typing，可脱离 DB/AI 单测。

对外 API：

- :data:`RESULT_STATE_MAP` / :func:`result_state` — 全组合派生的结果四态映射（不加新列）。
- :data:`RETRY_POLICY` / :func:`retry_plan` — 失败四分类 → 重试策略。
- :func:`plan_waves` — 按测试点 ``batch_tier``（A/B/C）分波。
- :func:`wave_gate_decision` — 前波硬挂 → 停后波。
- :class:`FlakyLedger` — 滑动窗口 flaky 判定 + 隔离区名单。
- :func:`admission_check` — 设备锁 / 数据范围相交的并行准入分批。
- :func:`retry_budget_check` — Retry Budget 封顶。
- :func:`release_gate` — 上线判定双条件门（共识 go **且** 置信≥0.9，回滚触发器两栏事前声明齐）。

环节 10 的上线判定门（:func:`release_gate`）同样只是**纯判定**：不读回归表、不写
``mc_test_*``、不触发版本状态机；回归摘要与回滚计划由调用方取好后传进来。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableSet, Optional, Sequence

__all__ = [
    "STATE_PASS",
    "STATE_FAIL",
    "STATE_INCONCLUSIVE",
    "STATE_NOT_JUDGED",
    "RESULT_STATES",
    "HARD_FAIL_STATES",
    "WILDCARD",
    "RESULT_STATE_MAP",
    "SCENARIO_STATUSES",
    "FAILURE_KINDS",
    "DEFAULT_RESULT_STATE",
    "result_state",
    "is_hard_fail",
    "failure_ratio",
    "RETRY_POLICY",
    "RETRY_POLICY_ALIASES",
    "RETRY_ACTIONS",
    "UNKNOWN_FAILURE_KIND_POLICY",
    "retry_plan",
    "retry_budget_check",
    "BATCH_TIERS",
    "DEFAULT_BATCH_TIER",
    "GATE_WAVE",
    "plan_waves",
    "wave_gate_decision",
    "FlakyLedger",
    "admission_check",
    "RELEASE_DECISIONS",
    "CONSENSUS_GO",
    "CONFIDENCE_THRESHOLD",
    "ROLLBACK_COLUMNS",
    "SUCCESS_CRITERION",
    "release_gate",
]

# ==========================================================================
# 一、结果四态映射（正文 355-367 行表；全组合派生，不加新列）
# ==========================================================================

#: 四态字面量（正文表格用词，UI 徽章直接渲染，不做二次翻译）。
STATE_PASS = "过"
STATE_FAIL = "挂"
STATE_INCONCLUSIVE = "判不了"
STATE_NOT_JUDGED = "没裁"

#: 四态取值域（顺序即展示顺序）。
RESULT_STATES: tuple[str, ...] = (STATE_PASS, STATE_FAIL, STATE_INCONCLUSIVE, STATE_NOT_JUDGED)

#: 「硬挂」态——只有它参与失败率与缺陷链，也只有它能停后波。
HARD_FAIL_STATES: frozenset[str] = frozenset({STATE_FAIL})

#: 映射表通配位（``pass × 任意`` / ``flaky × 任意`` 这类「一侧任意」的行）。
WILDCARD = "*"

#: ``mc_test_run_scenario.status`` 取值（现库口径：tasks/models.py
#: ``SCENARIO_RESULT_STATUSES`` = pass/fail/error/flaky/skipped；正文另有「超时」，
#: 本表按「超时」列行收下 ``timeout``/``timed_out`` 两个别名，见歧义清单#1）。
SCENARIO_STATUSES: tuple[str, ...] = (
    "pass", "fail", "error", "flaky", "skipped", "timeout", "timed_out",
)

#: ``mc_test_run_scenario.failure_kind`` 取值（现库 ``FAILURE_KINDS`` 四分类）。
FAILURE_KINDS: tuple[str, ...] = ("script_issue", "product_bug", "case_outdated", "env_flaky")

#: 未列组合的保守兜底态——判不了 = 不进失败率、不产 bug，绝不冒判「挂」。
DEFAULT_RESULT_STATE = STATE_INCONCLUSIVE


def _row(statuses: Sequence[str], kinds: Sequence[Optional[str]], state: str) -> dict[tuple[str, Any], str]:
    out: dict[tuple[str, Any], str] = {}
    for st in statuses:
        for k in kinds:
            out[(st, k)] = state
    return out


#: 全组合派生映射表：``(status, failure_kind) -> 四态``。
#: 行序与正文表格一致；``failure_kind=None`` 表示库里的 NULL（未分流）。
#: 「任意」用 :data:`WILDCARD` 表达（查表时精确键优先，通配兜底）。
RESULT_STATE_MAP: dict[tuple[str, Any], str] = {
    # pass × 任意 → 过
    **_row(("pass",), (WILDCARD,), STATE_PASS),
    # fail/error × product_bug → 挂（进缺陷链）
    **_row(("fail", "error"), ("product_bug",), STATE_FAIL),
    # fail/error × env_flaky → 判不了（不算失败率、不产 bug；回隔离区）
    **_row(("fail", "error"), ("env_flaky",), STATE_INCONCLUSIVE),
    # fail/error × script_issue → 判不了（回场景化重写该场景）
    **_row(("fail", "error"), ("script_issue",), STATE_INCONCLUSIVE),
    # fail × case_outdated → 没裁（回门② REJECT 流）
    **_row(("fail",), ("case_outdated",), STATE_NOT_JUDGED),
    #   正文只给 fail × case_outdated；error × case_outdated 未列 → 保守判不了
    #   （走 DEFAULT_RESULT_STATE，不建行）
    # flaky × 任意 → 判不了（抖动，进隔离区重测）
    **_row(("flaky",), (WILDCARD,), STATE_INCONCLUSIVE),
    # skipped / 超时 → 没裁（不算失败率分母；failure_kind 任意）
    **_row(("skipped", "timeout", "timed_out"), (WILDCARD,), STATE_NOT_JUDGED),
    # fail/error × 未分流(NULL)：正文未给 kind → 保守判不了（显式建行，避免依赖默认值）
    **_row(("fail", "error"), (None,), STATE_INCONCLUSIVE),
}

#: 四态 → 是否计入失败率分母（正文 367 行「判不了/没裁不算失败率、不产 bug」）。
_COUNTED_STATES: frozenset[str] = frozenset({STATE_PASS, STATE_FAIL})


def _norm(value: Any) -> str:
    """状态/分类字面量归一：strip + 小写；空串/None → ``''``。"""
    if value is None:
        return ""
    return str(value).strip().lower()


def result_state(status: str, failure_kind: Optional[str] = None) -> str:
    """``(status, failure_kind)`` → 结果四态（过/挂/判不了/没裁）。

    - 两侧都精确命中 :data:`RESULT_STATE_MAP` → 该行的态；
    - 否则命中 ``(*, WILDCARD)`` 行（``pass``/``flaky``/``skipped``/超时 一类「kind 任意」）；
    - 仍未命中（未知 status、``error × case_outdated`` 这类正文未归属组合）→
      :data:`DEFAULT_RESULT_STATE`（**判不了**，保守：不冒判挂、不进失败率、不产 bug）。

    ``failure_kind`` 传空串等价 NULL。大小写/空白不敏感。
    """
    st = _norm(status)
    kind = _norm(failure_kind) or None
    exact = RESULT_STATE_MAP.get((st, kind))
    if exact is not None:
        return exact
    return RESULT_STATE_MAP.get((st, WILDCARD), DEFAULT_RESULT_STATE)


def is_hard_fail(item: Optional[Mapping[str, Any]] = None, *, state: Optional[str] = None) -> bool:
    """这一条是不是「硬挂」（四态 == 挂）。

    传 ``item``（结果行文档，含 ``status``/``failure_kind``，或已派生的 ``result_state``）
    或直接传 ``state``。缺 status 的行按未裁处理，不算硬挂。
    """
    resolved = _norm(state) if state is not None else ""
    if not resolved and item is not None:
        preset = item.get("result_state")
        if preset:
            resolved = _norm(preset)
        else:
            resolved = _norm(result_state(item.get("status", ""), item.get("failure_kind")))
    return resolved in HARD_FAIL_STATES


def failure_ratio(states: Iterable[str]) -> tuple[float, int, int]:
    """一组四态 → ``(挂占比, 挂数, 计入分母的数)``。

    分母只算「过/挂」（正文 367 行：判不了/没裁不进分母）。分母为 0 时占比 0.0。
    """
    fails = counted = 0
    for raw in states:
        st = _norm(raw)
        if st in _COUNTED_STATES:
            counted += 1
            if st == STATE_FAIL:
                fails += 1
    return (fails / counted if counted else 0.0), fails, counted


# ==========================================================================
# 二、失败四分类 → 重试策略（正文 354 行：网络类退避重试、断言失败不重试、
#     限流等待、flaky 抖动×2、Retry Budget 封顶）
# ==========================================================================

#: 合法 action 取值。
#: retry=再跑一次；stop=就此收口（进缺陷链/交人）；quarantine=进隔离区（下轮带重试跑）；
#: rework=回门② REJECT 流重造用例（用例本身过期，重跑无意义）。
RETRY_ACTIONS: tuple[str, ...] = ("retry", "stop", "quarantine", "rework")

#: 每类的重试策略。``max_attempts`` = **最多重试次数**（不含首次执行）；
#: ``on_exhausted`` = 用尽预算后的终态动作。
RETRY_POLICY: dict[str, dict[str, Any]] = {
    # 脚本/场景问题：重跑还是同一个错 → 不重试，交「回场景化重写该场景」
    "script_issue": {
        "retry": False,
        "action": "stop",
        "max_attempts": 0,
        "backoff": "none",
        "base_backoff_s": 0.0,
        "max_backoff_s": 0.0,
        "factor": 1.0,
        "on_exhausted": "stop",
        "why": "脚本问题重跑仍错，不重试（四态=判不了，回场景化重写）",
    },
    # 产品缺陷：确定性挂 → 不重试，直接进缺陷链
    "product_bug": {
        "retry": False,
        "action": "stop",
        "max_attempts": 0,
        "backoff": "none",
        "base_backoff_s": 0.0,
        "max_backoff_s": 0.0,
        "factor": 1.0,
        "on_exhausted": "stop",
        "why": "断言失败/产品缺陷不重试（四态=挂，进缺陷链）",
    },
    # 用例过期：不是执行问题，重跑浪费预算 → 门② REJECT 回炉
    "case_outdated": {
        "retry": False,
        "action": "rework",
        "max_attempts": 0,
        "backoff": "none",
        "base_backoff_s": 0.0,
        "max_backoff_s": 0.0,
        "factor": 1.0,
        "on_exhausted": "rework",
        "why": "用例过期不重试，直接进门② REJECT 回炉（四态=没裁）",
    },
    # 环境抖动：抖动重试×2，用尽进隔离区
    "env_flaky": {
        "retry": True,
        "action": "retry",
        "max_attempts": 2,
        "backoff": "fixed",
        "base_backoff_s": 1.0,
        "max_backoff_s": 10.0,
        "factor": 1.0,
        "on_exhausted": "quarantine",
        "why": "环境抖动重试×2（固定间隔），仍挂进隔离区重测",
    },
    # 网络类瞬时故障：指数退避×3
    "transient": {
        "retry": True,
        "action": "retry",
        "max_attempts": 3,
        "backoff": "exponential",
        "base_backoff_s": 1.0,
        "max_backoff_s": 60.0,
        "factor": 2.0,
        "on_exhausted": "quarantine",
        "why": "网络/瞬时故障指数退避重试×3，用尽进隔离区",
    },
    # 限流：正文「限流等待」——不是抖动，是必须等；给独立档（歧义清单#3）
    "rate_limited": {
        "retry": True,
        "action": "retry",
        "max_attempts": 2,
        "backoff": "fixed",
        "base_backoff_s": 15.0,
        "max_backoff_s": 120.0,
        "factor": 1.0,
        "on_exhausted": "stop",
        "why": "限流：长等待后重试（固定间隔），用尽收口交人，不判挂",
    },
}

#: 别名 → 策略键（上游/引擎可能报的是网络/超时/429 等原文词）。
RETRY_POLICY_ALIASES: dict[str, str] = {
    "network": "transient",
    "net": "transient",
    "connection": "transient",
    "connection_reset": "transient",
    "timeout": "transient",
    "5xx": "transient",
    "http_5xx": "transient",
    "flaky": "env_flaky",
    "env": "env_flaky",
    "environment": "env_flaky",
    "rate_limit": "rate_limited",
    "ratelimited": "rate_limited",
    "throttled": "rate_limited",
    "429": "rate_limited",
    "assertion": "product_bug",
    "assert_failed": "product_bug",
    "script": "script_issue",
    "case_expired": "case_outdated",
    "outdated": "case_outdated",
}

#: 未知分类的兜底策略（保守：不重试、直接收口，绝不无界重试）。
UNKNOWN_FAILURE_KIND_POLICY: dict[str, Any] = {
    "retry": False,
    "action": "stop",
    "max_attempts": 0,
    "backoff": "none",
    "base_backoff_s": 0.0,
    "max_backoff_s": 0.0,
    "factor": 1.0,
    "on_exhausted": "stop",
    "why": "未知失败分类：保守不重试，收口交人判读",
}


def resolve_failure_kind(failure_kind: Any) -> str:
    """失败分类归一（别名折叠）；未识别返回 ``''``。"""
    k = _norm(failure_kind)
    return RETRY_POLICY_ALIASES.get(k, k)


def _backoff_seconds(cfg: Mapping[str, Any], attempt: int) -> float:
    """第 ``attempt`` 次重试（attempt=0 是第一次重试）该等多久。"""
    mode = cfg.get("backoff", "none")
    base = float(cfg.get("base_backoff_s") or 0.0)
    cap = float(cfg.get("max_backoff_s") or 0.0)
    if mode == "none" or base <= 0:
        return 0.0
    if mode == "exponential":
        factor = float(cfg.get("factor") or 2.0)
        value = base * (factor ** max(attempt, 0))
    else:  # fixed
        value = base
    return round(min(value, cap) if cap > 0 else value, 3)


def retry_plan(failure_kind: Optional[str], attempt: int = 0) -> dict[str, Any]:
    """失败分类 + 已消耗的重试次数 → 下一步动作。

    参数 ``attempt``：**该用例本轮已消耗的重试次数**（首次失败传 0；一次重跑后再失败传 1）。
    返回 ``{action, backoff_s, max_attempts, kind, retry, state, reason}``：

    - ``action == "retry"`` 且 ``backoff_s > 0`` → 等 ``backoff_s`` 秒后重跑；
    - 预算用尽（``attempt >= max_attempts``）→ 落到 ``on_exhausted``
      （env_flaky/transient → ``quarantine``，其余 → ``stop``/``rework``）；
    - 不重试的分类（script_issue/product_bug/case_outdated/未知）→ 直接终态动作，
      ``backoff_s == 0.0``。

    ``state`` 是配套的结果四态（正文映射），方便调用方一处取齐写库/渲染。
    """
    key = resolve_failure_kind(failure_kind)
    cfg = RETRY_POLICY.get(key, UNKNOWN_FAILURE_KIND_POLICY)
    try:
        used = int(attempt or 0)
    except (TypeError, ValueError):
        used = 0
    if used < 0:
        used = 0
    max_attempts = int(cfg["max_attempts"])
    state = _FAILURE_STATE.get(key, DEFAULT_RESULT_STATE)

    if cfg["retry"] and used < max_attempts:
        action = "retry"
        backoff = _backoff_seconds(cfg, used)
        reason = f"{cfg['why']}（第 {used + 1}/{max_attempts} 次重试）"
    elif cfg["retry"]:
        action = str(cfg["on_exhausted"])
        backoff = 0.0
        reason = f"{cfg['why']}（重试预算 {max_attempts} 已用尽）"
    else:
        action = str(cfg["on_exhausted"])
        backoff = 0.0
        reason = str(cfg["why"])

    return {
        "action": action,
        "backoff_s": backoff,
        "max_attempts": max_attempts,
        "kind": key or _norm(failure_kind) or "unknown",
        "retry": bool(cfg["retry"]),
        "attempt": used,
        "state": state,
        "reason": reason,
    }


#: 失败分类 → 结果四态（正文表格里的 kind 侧取值；``transient``/``rate_limited`` 非库内
#: failure_kind，只是重试分类，缺省按最保守的判不了）。
_FAILURE_STATE: dict[str, str] = {
    "script_issue": STATE_INCONCLUSIVE,
    "product_bug": STATE_FAIL,
    "case_outdated": STATE_NOT_JUDGED,
    "env_flaky": STATE_INCONCLUSIVE,
    "transient": STATE_INCONCLUSIVE,
    "rate_limited": STATE_INCONCLUSIVE,
}


def retry_budget_check(used: int, cap: int) -> bool:
    """Retry Budget 封顶：还可不可能再消耗一次重试。

    ``used`` = 已消耗的重试次数，``cap`` = 预算上限（整轮/整个 run 共享）。
    ``True`` = 还有预算（允许重试）；``cap <= 0`` 或 ``used >= cap`` → ``False``。
    入参非法（非数字）按「无预算」保守处理。
    """
    try:
        u = int(used or 0)
        c = int(cap or 0)
    except (TypeError, ValueError):
        return False
    if c <= 0:
        return False
    return u < c


# ==========================================================================
# 三、执行波次（正文 354 行：波次=A/B/C，冒烟硬挂停后波）
# ==========================================================================

#: 波次取值（与测试点 ``batch_tier`` 同源：A 冒烟 / B 重点 / C 扩展）。
BATCH_TIERS: tuple[str, ...] = ("A", "B", "C")

#: 行上没写/写了未知 tier 时归哪波——**归 C**（任务书口径：扩展波兜底，不抢占冒烟位）。
DEFAULT_BATCH_TIER = "C"

#: 停波闸门看的是哪一波（正文：A 波冒烟硬挂 → 停后波）。
GATE_WAVE = "A"


def _tier_of(row: Mapping[str, Any]) -> str:
    raw = row.get("batch_tier")
    if raw is None:
        return DEFAULT_BATCH_TIER
    tier = str(raw).strip().upper()
    return tier if tier in BATCH_TIERS else DEFAULT_BATCH_TIER


def plan_waves(
    cases: Sequence[Mapping[str, Any]],
    *,
    require_explored: bool = False,
    held_out: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """按测试点 ``batch_tier``（A/B/C）分波，A 冒烟先跑。

    输入行需含 ``{case_id, batch_tier, scenario_id, requirement_id}``（多余字段原样带过）；
    ``batch_tier`` 缺失/空/未知取值一律归 **C**（大小写不敏感）。
    返回 ``[{"wave": "A"|"B"|"C", "items": [row, ...]}, ...]``，按 A→B→C 顺序，
    **空波不出现在结果里**（没有 B 例就不返回 B），波内保持入参顺序。
    输入按 ``case_id`` 去重（同 id 只留第一条），避免同一用例在两轮里各跑一遍。

    :param require_explored: 探索门闸（§环节7「未过门不进波次」）——``True`` 时只放行
        ``verified_by_run``=已探索（``explored``）的用例；未探索（含缺省）与探索失败的
        进 ``held_out``（给了才收集），不进任何波。缺省 ``False``=不过滤（存量调用方
        语义不变；执行编排接线后按开关打开）。
    :param held_out: 可选出参收集器——被探索门拦下的行进这里（含拦因），调用方
        展示「隔离区/待探索」用。
    """
    buckets: dict[str, list[dict[str, Any]]] = {tier: [] for tier in BATCH_TIERS}
    seen: set[str] = set()
    for row in cases or ():
        cid = str(row.get("case_id") or "")
        key = cid or f"__anon_{id(row)}"
        if key in seen:
            continue
        seen.add(key)
        if require_explored:
            state = str(row.get("verified_by_run") or "").strip().lower()
            if state != "explored":
                if held_out is not None:
                    held_out.append({**row, "held_reason": f"未过探索门（{state or '未探索'}）"})
                continue
        buckets[_tier_of(row)].append(dict(row))
    return [
        {"wave": tier, "items": buckets[tier], "count": len(buckets[tier])}
        for tier in BATCH_TIERS
        if buckets[tier]
    ]


def wave_gate_decision(wave_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """一波跑完后的闸门：要不要放行后一波。

    入参 = **该波**的结果行（调用方传 A 波结果即可；行含 ``status``/``failure_kind``
    或已派生的 ``result_state``）。口径（正文「冒烟硬挂停后波」）：

    - 任一结果四态 == **挂** → ``proceed=False``（硬挂占比 > 0 即停，不设阈值）；
    - **判不了/没裁不算硬挂** → 不拦后波（环境抖动、脚本问题、用例过期、skipped 都不能
      停冒烟门）；
    - 全过 / 空结果 → ``proceed=True``。

    返回 ``{proceed, reason, hard_fails, total, fail_ratio, states}``；``fail_ratio``
    的分母只算过/挂（与 :func:`failure_ratio` 同口径）。
    """
    states: list[str] = []
    for row in wave_results or ():
        preset = row.get("result_state")
        if preset:
            states.append(_norm(preset))
        else:
            states.append(result_state(row.get("status", ""), row.get("failure_kind")))
    ratio, hard_fails, counted = failure_ratio(states)

    if hard_fails == 0:
        return {
            "proceed": True,
            "reason": f"无硬挂（共 {len(states)} 条，其中判不了/没裁 {len(states) - counted} 条不计入），放行后波",
            "hard_fails": 0,
            "total": len(states),
            "fail_ratio": 0.0,
            "states": states,
        }
    return {
        "proceed": False,
        "reason": f"本波硬挂 {hard_fails}/{counted}（占比 {ratio:.2%} > 0），冒烟门未过，停后波",
        "hard_fails": hard_fails,
        "total": len(states),
        "fail_ratio": round(ratio, 6),
        "states": states,
    }


# ==========================================================================
# 四、flaky 判定与隔离区（正文 354 行：flaky 隔离区先滤；364 行 flaky → 判不了）
# ==========================================================================

#: 单用例最多保留的历史条数（滑动窗口的硬上限，防无界增长）。
MAX_HISTORY = 200


class FlakyLedger:
    """滑动窗口 flaky 判定器（**纯内存 dict**，不落库）。

    用法：每轮执行完对每条用例 :meth:`record` 一次结果（四态或引擎 status 均可），
    下一轮开跑前用 :meth:`quarantine_list` 取隔离区名单先滤掉，用 :meth:`is_flaky`
    决定是否按 ``env_flaky`` 档重试×2。调用方负责把 :meth:`to_dict` 快照写进
    ``mc_test_run.summary`` / 任务配置，进程重启后 :meth:`from_dict` 还原。

    抖动定义（歧义清单#4 的落地口径）：**「挂 → 重跑过」** 算一次抖动周期。

    - :meth:`is_flaky` = 近 ``window`` 次里「挂且随后翻回过」的次数 / 窗口内**挂的次数**
      ≥ ``threshold``（默认 0.3），且窗口内至少出现过 1 次挂。
      稳定过的用例（0 次挂）恒 False；一直挂从没翻回过（确定性缺陷）也恒 False——
      两类「不误伤」。
    - :meth:`quarantine_list` = 连续 flaky ≥ ``min_cycles`` 轮（默认 2）——落地为
      「窗口内抖动周期 ≥ 2」，即至少出现过两次 ``挂→过`` 翻转（P,F,P 这种单点毛刺
      算 flaky 可重试，但不进隔离区）。
    """

    def __init__(self, history: Optional[Mapping[str, Sequence[str]]] = None) -> None:
        self._hist: dict[str, list[str]] = {}
        if history:
            self.load_dict(history)

    # ── 写入 ────────────────────────────────────────────────
    def record(self, case_id: Any, result_state_: str) -> str:
        """记一次结果（四态字面量或引擎 status），返回归一后的四态。"""
        key = str(case_id or "")
        state = _to_state(result_state_)
        bucket = self._hist.setdefault(key, [])
        bucket.append(state)
        if len(bucket) > MAX_HISTORY:
            del bucket[: len(bucket) - MAX_HISTORY]
        return state

    def clear(self, case_id: Optional[Any] = None) -> None:
        """清历史：给 id 清单条，不给清全部。"""
        if case_id is None:
            self._hist.clear()
        else:
            self._hist.pop(str(case_id or ""), None)

    # ── 查询 ────────────────────────────────────────────────
    def states(self, case_id: Any, window: int = 10) -> list[str]:
        """近 ``window`` 次的四态序列（旧→新）。"""
        bucket = self._hist.get(str(case_id or ""), [])
        w = max(int(window or 0), 1)
        return list(bucket[-w:])

    def flaky_cycles(self, case_id: Any, window: int = 10) -> tuple[int, int]:
        """``(抖动周期数, 窗口内挂的次数)``；周期 = 一条挂的**下一条**是过。"""
        seq = self.states(case_id, window)
        fails = flips = 0
        for i, st in enumerate(seq):
            if st != STATE_FAIL:
                continue
            fails += 1
            if i + 1 < len(seq) and seq[i + 1] == STATE_PASS:
                flips += 1
        return flips, fails

    def flaky_ratio(self, case_id: Any, window: int = 10) -> float:
        """窗口内「挂→重跑过」占**挂的总次数**的比例（没有挂 → 0.0）。"""
        flips, fails = self.flaky_cycles(case_id, window)
        return flips / fails if fails else 0.0

    def is_flaky(self, case_id: Any, window: int = 10, threshold: float = 0.3) -> bool:
        """近 ``window`` 次里「挂→重跑过」占比 ≥ ``threshold`` → 判抖动。

        窗口外的历史不参与（滑动窗口）；从未挂过 → False；每次都挂没翻回过 → False
        （那是稳定挂/真缺陷，不是抖动）。
        """
        fails = self.flaky_cycles(case_id, window)[1]
        if fails <= 0:
            return False
        return self.flaky_ratio(case_id, window) >= float(threshold)

    def quarantine_list(self, window: int = 10, threshold: float = 0.3, min_cycles: int = 2) -> list[str]:
        """应进隔离区的用例（连续 flaky ≥ ``min_cycles`` 轮）。

        条件：:meth:`is_flaky` 为真 **且** 抖动周期数 ≥ ``min_cycles``。返回按 case_id
        排序（稳定输出，便于写库/断言）。
        """
        need = max(int(min_cycles or 1), 1)
        out: list[str] = []
        for cid in sorted(self._hist):
            cycles, _ = self.flaky_cycles(cid, window)
            if cycles >= need and self.is_flaky(cid, window, threshold):
                out.append(cid)
        return out

    def active_cases(self) -> list[str]:
        """记账在册的用例 id（排序）。"""
        return sorted(self._hist)

    # ── 快照 ────────────────────────────────────────────────
    def to_dict(self) -> dict[str, list[str]]:
        """持久化快照（调用方写 run/config；四态字符串列表）。"""
        return {cid: list(states) for cid, states in self._hist.items()}

    def load_dict(self, data: Mapping[str, Sequence[str]]) -> "FlakyLedger":
        """从快照恢复（累加语义；未知取值走 :func:`_to_state` 归一，兜底判不了）。"""
        for cid, states in (data or {}).items():
            bucket = self._hist.setdefault(str(cid), [])
            bucket.extend(_to_state(s) for s in (states or ()))
            if len(bucket) > MAX_HISTORY:
                del bucket[: len(bucket) - MAX_HISTORY]
        return self

    @classmethod
    def from_dict(cls, data: Mapping[str, Sequence[str]]) -> "FlakyLedger":
        return cls(data)

    def __contains__(self, case_id: object) -> bool:
        return str(case_id or "") in self._hist

    def __len__(self) -> int:
        return len(self._hist)


#: 引擎 status / 四态字面量 → 四态（记账时统一口径；未知值保守判不了）。
_STATE_BY_STATUS: dict[str, str] = {
    "pass": STATE_PASS,
    "fail": STATE_FAIL,          # 未分流的 fail 先按挂记账（后续可被覆盖不了：见注）
    "error": STATE_FAIL,
    "flaky": STATE_INCONCLUSIVE,
    "skipped": STATE_NOT_JUDGED,
    "timeout": STATE_NOT_JUDGED,
    "timed_out": STATE_NOT_JUDGED,
}


def _to_state(value: Any) -> str:
    raw = _norm(value)
    if raw in {_norm(s) for s in RESULT_STATES}:
        return raw
    if raw in _STATE_BY_STATUS:
        return _STATE_BY_STATUS[raw]
    return DEFAULT_RESULT_STATE


# ==========================================================================
# 五、并行准入（正文 354 行：数据范围不相交；设备一机一锁）
# ==========================================================================

#: 资源前缀口径（``device:xx`` 一机一锁；库/账号等同样按 ``kind:id`` 传）。
RESOURCE_SEP = ":"

#: 未声明 ``data_scope`` 的行用的哨兵值（哨兵与哨兵**不**互斥，见 admission 说明）。
_NO_SCOPE = "__no_scope__"


def _resource_of(row: Mapping[str, Any]) -> str:
    res = row.get("resource")
    if res is None:
        return ""
    return str(res).strip()


def _scope_of(row: Mapping[str, Any]) -> tuple[str, bool]:
    """``(归一 data_scope, 是否声明了范围)``；空/None → 哨兵 + False。"""
    raw = row.get("data_scope")
    scope = "" if raw is None else str(raw).strip()
    return (scope or _NO_SCOPE, bool(scope))


def admission_check(
    items: Sequence[Mapping[str, Any]],
    *,
    busy_resources: Optional[MutableSet[str]] = None,
    deferred_out: Optional[list[dict[str, Any]]] = None,
) -> list[list[dict[str, Any]]]:
    """把一批用例切成「批内可并行」的批次（批次之间按顺序串行派发）。

    互斥规则（保守 fail-closed，任何判不清的一律拆开）：

    1. **同 ``resource`` 互斥**——设备一机一锁：两条要同一把锁不能同批；
       锁已被 ``busy_resources`` 占着的条目本轮排不进任何批，进 ``deferred_out``
       （没有它调用方就丢用例了，见返回值说明）。
    2. **``data_scope`` 字符串相同 → 视为可能相交** → 拆到不同批次（不做区间推断，
       同名即冲突）。范围不同 → 视为不相交，可同批。
    3. 未声明 ``data_scope``（None/空串）→ 按「不占数据范围」处理，彼此及与已声明者
       都不算相交（哨兵值不同不互斥）。

    分批算法 = 顺序首适配（first-fit）：条目放进第一个不与批内任何条目冲突的批次，
    放不下就新开一批。批次数上界 = 条目数（最坏一条一批）。

    返回 ``list[list[dict]]``（批次列表，原行对象浅拷贝，不改入参）。
    ``busy_resources`` 里被占用而无法派发的条目**不会出现在批次里**——若传了
    ``deferred_out`` 列表则追加进去（附 ``admission_defer`` 原因），否则直接丢弃；
    调用方要是不想丢用例，必须传 ``deferred_out``。
    """
    busy = {str(r).strip() for r in (busy_resources or set()) if str(r).strip()}

    scheduled: list[list[dict[str, Any]]] = []
    held: list[tuple[dict[str, Any], int]] = []  # (row, 批内锁占用者下标) —— 同批同锁只留第一条
    deferred: list[dict[str, Any]] = []

    for row in items or ():
        item = dict(row)
        res = _resource_of(item)
        scope, _declared = _scope_of(item)

        if res and res in busy:
            copy = dict(item)
            copy["admission_defer"] = f"resource_busy:{res}"
            deferred.append(copy)
            continue

        placed = False
        for batch in scheduled:
            conflict = _batch_conflict(batch, resource=res, scope=scope)
            if not conflict:
                batch.append(item)
                placed = True
                break
        if not placed:
            scheduled.append([item])

    if deferred_out is not None:
        deferred_out.extend(deferred)
    elif deferred:
        # 没给出口也不能凭空吞掉用例：塞回末尾成独立批次，由调用方派发时自行判资源可用性。
        for copy in deferred:
            copy.pop("admission_defer", None)
            scheduled.append([copy])
    return scheduled


def _batch_conflict(
    batch: Sequence[Mapping[str, Any]],
    *,
    resource: str,
    scope: str,
) -> bool:
    """批内是否已有条目与新条目抢同一把锁 / 撞同一数据范围。"""
    for other in batch:
        other_res = _resource_of(other)
        if resource and other_res and other_res == resource:
            return True
        other_scope, _ = _scope_of(other)
        if scope != _NO_SCOPE and other_scope != _NO_SCOPE and scope == other_scope:
            return True
    return False


# ==========================================================================
# 六、上线判定双条件门（正文 379 行：共识 go 且置信≥0.9 才 go；回滚触发器
#     事前声明两栏（自动回/人来回）；成功=没回滚且没事故）
# ==========================================================================

#: ``decision`` 取值域——门只有两个出口，**没有「有条件 go」**。
RELEASE_DECISIONS: tuple[str, ...] = ("go", "no_go")

#: 共识通过的唯一字面量（大小写不敏感；``abstain``/None/空一律不放行——弃权不放行）。
CONSENSUS_GO = "go"

#: 置信下限（**含边界**：``>= 0.9`` 即过；正文「置信≥0.9」）。
CONFIDENCE_THRESHOLD = 0.9

#: 回滚触发器必须事前声明的两栏（正文「自动回/人来回」）；任一栏缺/空 → 不 go。
ROLLBACK_COLUMNS: tuple[str, ...] = ("auto_rollback", "manual_rollback")

#: 成功口径（正文原话）：门放行只是「可以上线」，**算不算成功**由上线后回看——
#: 没触发过回滚且没出过事故。本模块只把口径作为常量随判定结果带出，不做事后稽核。
SUCCESS_CRITERION = "no_rollback_and_no_incident"


def _rollback_column_filled(rollback_plan: Mapping[str, Any], column: str) -> bool:
    """某栏是否「存在且非空字符串」（strip 后非空；非字符串值按未声明处理）。"""
    raw = rollback_plan.get(column)
    if not isinstance(raw, str):
        return False
    return bool(raw.strip())


def release_gate(
    consensus: Optional[str],
    confidence: float,
    *,
    regression: Optional[Mapping[str, Any]] = None,
    rollback_plan: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """上线判定双条件门（25 号文环节 10，正文 379 行）。

    口径（严格，三条件是**合取**——任一不满足整体就是 no_go）：

    1. **共识 go**：``consensus`` 归一（strip + 小写）后 == ``"go"`` 才算通过；
       ``"abstain"`` / ``None`` / 空串 / 其他任何值都**不放行**（弃权不放行）。
    2. **置信 ≥ 0.9**（含边界；``confidence`` 必须落在 ``[0.0, 1.0]`` 闭区间）。
    3. **回滚触发器两栏事前声明齐**：``rollback_plan`` 里 ``auto_rollback``（自动回）
       与 ``manual_rollback``（人来回）都存在且非空字符串——事前声明是硬条件，
       没想好怎么退就不许上。

    三条全过 → ``{"decision": "go", "passed": True, "reasons": []}``；否则
    ``decision="no_go"`` 且 ``reasons`` 逐条写清缺哪条（聚合理由，给人看的）。

    参数：
        consensus: 共识结论字面量（``"go"``/``"no_go"``/``"abstain"``/None/…）。
        confidence: 置信度，0.0~1.0。
        regression: 回归结果摘要（可选）。门本身**不**用它做判定（双条件门只看
            共识+置信+回滚两栏），原样带进返回值供 UI/报告展示。
        rollback_plan: 回滚触发器两栏声明（可选；但两栏齐是 go 的硬条件）。

    返回::

        {
            "decision": "go" | "no_go",
            "passed": bool,             # 三条件合取
            "reasons": [str, ...],      # no_go 时的聚合理由，逐条人读
            "confidence_ok": bool,
            "consensus_ok": bool,
            "rollback_ok": bool,        # 两栏（auto_rollback/manual_rollback）都非空
            "success_criterion": "no_rollback_and_no_incident",
            "regression": {...} | None, # 入参原样带出（浅拷贝）
            "rollback_plan": {...} | None,
        }

    Raises:
        ValueError: ``confidence`` 越界（含非数字——不静默降级）或
            ``rollback_plan`` 不是 Mapping（给了就必须是 dict 形状）。
    """
    # ---- 非法输入先炸（不静默降级） --------------------------------------
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError(f"confidence 必须是数字，得到 {confidence!r}")
    conf = float(confidence)
    if not (0.0 <= conf <= 1.0):
        raise ValueError(f"confidence 越界 [0.0, 1.0]：{confidence!r}")
    if rollback_plan is not None and not isinstance(rollback_plan, Mapping):
        raise ValueError(f"rollback_plan 必须是 dict/Mapping，得到 {type(rollback_plan).__name__}")

    plan = rollback_plan or {}

    # ---- 三条件逐条判 ----------------------------------------------------
    normalized = str(consensus or "").strip().lower()
    consensus_ok = normalized == CONSENSUS_GO
    confidence_ok = conf >= CONFIDENCE_THRESHOLD
    rollback_ok = all(_rollback_column_filled(plan, col) for col in ROLLBACK_COLUMNS)

    reasons: list[str] = []
    if not consensus_ok:
        shown = str(consensus).strip() if consensus is not None else ""
        if not shown:
            reasons.append("共识未达成（consensus 缺失/为空）——弃权不放行")
        elif normalized == "abstain":
            reasons.append("共识为弃权（abstain）——弃权不放行")
        else:
            reasons.append(f"共识不是 go（consensus={shown!r}）——仅 consensus=go 可放行")
    if not confidence_ok:
        reasons.append(f"置信不足（confidence={conf} < {CONFIDENCE_THRESHOLD}）")
    if not rollback_ok:
        missing = [col for col in ROLLBACK_COLUMNS if not _rollback_column_filled(plan, col)]
        reasons.append("回滚触发器未事前声明齐——缺栏：" + "、".join(missing))

    passed = not reasons
    return {
        "decision": "go" if passed else "no_go",
        "passed": passed,
        "reasons": reasons,
        "confidence_ok": confidence_ok,
        "consensus_ok": consensus_ok,
        "rollback_ok": rollback_ok,
        "success_criterion": SUCCESS_CRITERION,
        "regression": dict(regression) if regression is not None else None,
        "rollback_plan": dict(plan) if rollback_plan is not None else None,
    }
