"""缺陷周期报表（20 号文 §4.5 / §8.4）—— **纯函数，无 DB**。

禅道给了 bugs / leftBugs / escapedBugs 三数（300 §4.2b）但**没落地 MTTR**
（``fixedRate`` / ``bugLiveDays`` 语言包里被注释掉）。这里补上，并把禅道
``report->charts`` 的 16 个维度变成可直接消费的输出（20 号文 §4.5）。

输入是 :func:`testing_platform.defects.service.list_bugs` 产出的 bug 文档列表
（``_bug_document`` 的字段形状），所以本模块**无 DB、无副作用**，便于单测与
报表复用（报告域可直接把它算出的 ``bug_cycle`` 快照塞进 snapshot）。

对外表面::

    from testing_platform.reports import compute_cycle_report
    cycle = compute_cycle_report(bugs)          # → {mttr_seconds, ...}
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from ..defects.models import BUG_LONG_LIFE_DAYS

__all__ = ["compute_cycle_report"]


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
    """两个时刻的秒差（start/end 缺任一 → None）。"""
    a, b = _parse_time(start), _parse_time(end)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds())


def _avg(values: Sequence[float]) -> Optional[float]:
    """均值（空集合 → None，避免除零；并按 git 惯例保留两位）。"""
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _distribution(bugs: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    """按某个字段做计数分布（值为 None → ``"(未填)"``）。"""
    counts: dict[str, int] = {}
    for bug in bugs:
        value = bug.get(key)
        label = str(value) if value not in (None, "") else "(未填)"
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def compute_cycle_report(
    bugs: Sequence[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    longlife_days: int = BUG_LONG_LIFE_DAYS,
) -> dict[str, Any]:
    """算缺陷周期报表（20 号文 §4.5 + 禅道 16 图维度）。

    Args:
        bugs: ``_bug_document`` 形状的 bug 文档列表（已按版本/策略收窄）。
        now: 报表基准时刻（测试用固定时间戳；缺省为当前 UTC）。
        longlife_days: 长期未决阈值（对标 ``$config->bug->longlife = 7``）。

    Returns:
        dict，含指标 ``metrics``（MTTR/平均验证/总生命周期/重开率/超期/逃逸率）
        与 ``distribution``（按 severity/bug_type/resolution/status/module/
        assignee/found_in 的计数分布，对标禅道 ``report->charts``）。
        均值类指标无数据时为 ``None``（不是 0）。
    """
    base = now or datetime.now(timezone.utc).replace(tzinfo=None)
    bugs = list(bugs or [])
    total = len(bugs)

    # MTTR（平均修复时长）：只算有 resolved_at 的
    mttr = _avg(
        [s for s in (
            _seconds_between(bug.get("created_at"), bug.get("resolved_at"))
            for bug in bugs
        ) if s is not None]
    )
    # 平均验证时长：resolved_at → verified_at
    verify = _avg(
        [s for s in (
            _seconds_between(bug.get("resolved_at"), bug.get("verified_at"))
            for bug in bugs
        ) if s is not None]
    )
    # 总生命周期：created_at → closed_at（只算已关闭的）
    lifecycle = _avg(
        [s for s in (
            _seconds_between(bug.get("created_at"), bug.get("closed_at"))
            for bug in bugs
        ) if s is not None]
    )

    reopened = sum(1 for bug in bugs if (bug.get("activated_count") or 0) > 0)
    escaped = sum(1 for bug in bugs if bug.get("escaped"))
    overdue = sum(1 for bug in bugs if bug.get("is_overdue"))
    longlife = sum(1 for bug in bugs if bug.get("is_longlife"))
    unconfirmed = sum(1 for bug in bugs if not bug.get("confirmed"))

    # 逃逸率 = 逃逸 bug 数 / 总 bug 数（越低说明测试越有效）
    escape_rate = round(escaped / total, 4) if total else None
    reopen_rate = round(reopened / total, 4) if total else None

    return {
        "total": total,
        "metrics": {
            "mttr_seconds": mttr,              # 平均修复时长（禅道没落地，我们补）
            "avg_verify_seconds": verify,      # 平均验证时长
            "avg_lifecycle_seconds": lifecycle,  # 总生命周期（创建→关闭）
            "reopen_count": reopened,          # 重开过的 bug 数
            "reopen_rate": reopen_rate,        # 重开率（返工率信号）
            "overdue_count": overdue,          # 超期（deadline < now 且未关闭）
            "longlife_count": longlife,        # 长期未决（存活 ≥ longlife_days 未解决）
            "longlife_days": longlife_days,
            "escaped_count": escaped,          # 逃逸 bug（上线后才发现）
            "escape_rate": escape_rate,        # 逃逸率
            "unconfirmed_count": unconfirmed,  # 未确认
            "active_count": sum(
                1 for b in bugs if b.get("status") not in ("closed", "rejected")
            ),
        },
        "distribution": {
            "severity": _distribution(bugs, "severity"),
            "bug_type": _distribution(bugs, "bug_type"),
            "resolution": _distribution(bugs, "resolution"),
            "status": _distribution(bugs, "status"),
            "module": _distribution(bugs, "module"),
            "assignee": _distribution(bugs, "assignee_id"),
            "found_in": _distribution(bugs, "found_in"),
        },
    }
