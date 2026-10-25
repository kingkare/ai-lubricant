"""测试策略报表（``strategy.report`` 的取数口径）—— **纯函数，无 DB**。

用户定版 2026-10-16：策略列表加「报告」入口，一页统计本轮测试的产出。口径：

======  =========================================================
指标     口径
======  =========================================================
用例数    本轮勾选的 ``mc_test_strategy_case`` 行数
自动化用例数 ``exec_mode == automated``（人工/混合另给，三者相加=用例数）
bug 数    该策略下的缺陷（``mc_test_bug.strategy_id``；版本×策略第二维度）
P0/P1 数   ``priority ∈ (1, 2)``（用户定版：按优先级字段，非严重度）
解决时长   ``created_at → resolved_at``（对齐缺陷域 MTTR，只算已解决/关闭的）
平均/最长/最短 同上集合的均值 / 最大值 / 最小值（附最长那条 bug）
延期     计划 ``end_time`` vs ``actual_end_time``；未填实际完成时间且
        已过 end_time → 按当前时间算「已超期」（``basis=in_progress``）
======  =========================================================

MTTR/重开率/逃逸率等缺陷域自身的周期指标仍归
:func:`testing_platform.reports.compute_cycle_report`（本模块不重算，只取
用户点名的几项），两者输入同为 ``list_bugs`` 产出的 bug 文档。

用法::

    report = compute_strategy_report(strategy_doc, bugs, now=...)
    # → {"cases": {...}, "bugs": {...}, "delay": {...}}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

__all__ = ["compute_strategy_report", "STRATEGY_P0_P1_PRIORITIES"]

#: P0/P1 判定集合（用户定版 2026-10-16：按 ``priority`` 字段取最高两档）。
STRATEGY_P0_P1_PRIORITIES: frozenset[int] = frozenset({1, 2})

#: 视为「已解决」的状态（解决时长口径；与缺陷域 resolved/verified/closed 对齐）。
_RESOLVED_STATUSES: frozenset[str] = frozenset({"resolved", "verified", "closed"})


def _parse_time(value: Any) -> Optional[datetime]:
    """ISO 字符串 / datetime / None → 朴素 datetime（无法解析返回 None）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _seconds_between(start: Any, end: Any) -> Optional[int]:
    """两个时刻的秒差（start/end 缺任一 → None；负数按 0 收）。"""
    a, b = _parse_time(start), _parse_time(end)
    if a is None or b is None:
        return None
    return max(int((b - a).total_seconds()), 0)


def _count_by(bugs: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    """按字段计数分布（空值归 ``(未填)``）。"""
    counts: dict[str, int] = {}
    for bug in bugs:
        value = bug.get(key)
        label = str(value) if value not in (None, "") else "(未填)"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _case_stats(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """用例段统计：总数 / 执行方式三档 / 结果分布 / 执行率 / 通过率。"""
    total = len(cases)
    by_mode = {"manual": 0, "automated": 0, "hybrid": 0}
    by_result: dict[str, int] = {}
    for row in cases:
        mode = str(row.get("exec_mode") or "manual")
        if mode in by_mode:
            by_mode[mode] += 1
        else:
            by_mode[mode] = by_mode.get(mode, 0) + 1
        result = str(row.get("result") or "pending")
        by_result[result] = by_result.get(result, 0) + 1
    pending = by_result.get("pending", 0)
    passed = by_result.get("pass", 0)
    executed = total - pending
    return {
        "total": total,
        "automated": by_mode["automated"],
        "manual": by_mode["manual"],
        "hybrid": by_mode["hybrid"],
        "by_exec_mode": by_mode,
        "by_result": by_result,
        "executed": executed,
        "execution_rate": round(executed / total, 4) if total else 0.0,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }


def _bug_stats(bugs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """缺陷段统计：总数 / P0-P1 / 分布 / 解决时长（平均·最长·最短）。

    解决时长 = ``created_at → resolved_at``，只算有 ``resolved_at`` 的（=已解决）；
    一条都没有时三个时长指标给 ``None``（不是 0——0 会被读成「秒解」）。
    """
    total = len(bugs)
    p0_p1 = sum(
        1 for b in bugs
        if isinstance(b.get("priority"), int) and b["priority"] in STRATEGY_P0_P1_PRIORITIES
    )
    durations: list[tuple[int, Mapping[str, Any]]] = []
    for b in bugs:
        secs = _seconds_between(b.get("created_at"), b.get("resolved_at"))
        if secs is not None:
            durations.append((secs, b))
    values = [s for s, _ in durations]
    avg_seconds = round(sum(values) / len(values), 2) if values else None
    if durations:
        longest_secs, longest_bug = max(durations, key=lambda kv: kv[0])
        shortest_secs = min(values)
        slowest_bug = {
            "id": str(longest_bug.get("id")),
            "code": longest_bug.get("code"),
            "title": longest_bug.get("title"),
            "seconds": longest_secs,
        }
    else:
        longest_secs = shortest_secs = None
        slowest_bug = None
    return {
        "total": total,
        "p0_p1_count": p0_p1,
        "open_count": sum(1 for b in bugs if b.get("status") not in ("closed", "rejected")),
        "resolved_count": sum(1 for b in bugs if str(b.get("status") or "") in _RESOLVED_STATUSES),
        "by_priority": _count_by(bugs, "priority"),
        "by_severity": _count_by(bugs, "severity"),
        "by_status": _count_by(bugs, "status"),
        "avg_resolve_seconds": avg_seconds,
        "max_resolve_seconds": longest_secs,
        "min_resolve_seconds": shortest_secs,
        "slowest_bug": slowest_bug,
    }


def _delay_stats(
    strategy: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """延时段：计划 ``end_time`` vs ``actual_end_time``（未填则按当前时间算已超期）。

    - ``basis=actual``  —— 有实际完成时间，延期 = 实际 − 计划
    - ``basis=in_progress`` —— 无实际完成时间且已过计划结束，延期 = 现在 − 计划（仍在跑）
    - 未过期 / 无计划结束 → ``delayed=false``，``delay_seconds=null``
    """
    plan_end = _parse_time(strategy.get("end_time"))
    actual_end = _parse_time(strategy.get("actual_end_time"))
    analysis = strategy.get("delay_analysis")
    if plan_end is None:
        return {
            "has_plan": False, "delayed": False, "basis": None,
            "plan_end_time": None, "actual_end_time": None, "reference_time": None,
            "delay_seconds": None, "delay_days": None, "analysis": analysis,
        }
    if actual_end is not None:
        basis, reference = "actual", actual_end
    elif plan_end <= now:
        # 没有实际完成时间，但计划已结束——说明这一轮逾期未收尾，按当前时间计已超期。
        basis, reference = "in_progress", now
    else:
        basis, reference = None, None
    delay_seconds = (
        max(int((reference - plan_end).total_seconds()), 0)
        if reference is not None
        else None
    )
    return {
        "has_plan": True,
        "delayed": bool(delay_seconds and delay_seconds > 0),
        "basis": basis,
        "plan_end_time": plan_end.isoformat(),
        "actual_end_time": actual_end.isoformat() if actual_end else None,
        "reference_time": reference.isoformat() if reference else None,
        "delay_seconds": delay_seconds,
        "delay_days": round(delay_seconds / 86400, 2) if delay_seconds else None,
        "analysis": analysis,
    }


def compute_strategy_report(
    strategy: Mapping[str, Any],
    bugs: Sequence[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """算一轮测试的策略报表。

    Args:
        strategy: :func:`testing_platform.strategy.service.get_strategy` 的输出
            （含 ``cases`` / ``summary`` / ``end_time`` / ``actual_end_time`` /
            ``delay_analysis``）。
        bugs: 该策略下的 bug 文档列表（``list_bugs(project_id, strategy_id=...)``）。
        now: 报表基准时刻（测试注入固定时间戳；缺省当前 UTC）。

    Returns:
        ``{"strategy": 摘要, "cases": {...}, "bugs": {...}, "delay": {...}}``。
    """
    base = now or datetime.now(timezone.utc).replace(tzinfo=None)
    cases = list(strategy.get("cases") or [])
    return {
        "strategy": {
            "id": strategy.get("id"),
            "code": strategy.get("code"),
            "name": strategy.get("name"),
            "strategy_type": strategy.get("strategy_type"),
            "status": strategy.get("status"),
            "version_id": strategy.get("version_id"),
            "start_time": strategy.get("start_time"),
            "end_time": strategy.get("end_time"),
            "actual_end_time": strategy.get("actual_end_time"),
            "delay_analysis": strategy.get("delay_analysis"),
            "owner_id": strategy.get("owner_id"),
            "testers": list(strategy.get("testers") or []),
            "developers": list(strategy.get("developers") or []),
        },
        "cases": _case_stats(cases),
        "bugs": _bug_stats(list(bugs or [])),
        "delay": _delay_stats(strategy, now=base),
        "generated_at": base.isoformat(),
    }
