"""定时解析（02 §4）——**纯函数**，不真起 APScheduler。

只把任务的 ``schedule`` JSONB 解析成"下次触发时间"，把调度器的挂载留给集成层
（``apscheduler`` 已在平台依赖里，但本包不引入运行时状态，便于单测与复用）。

支持的 ``schedule`` 形态（02 §4）::

    {"kind": "cron",     "expr": "0 2 * * *"}         # 每天 2 点
    {"kind": "interval", "minutes": 30}               # 每 30 分钟
    {"kind": "once",     "at": "2026-10-01T09:00:00"} # 一次性

cron 是标准 5 字段（分 时 日 月 周），支持 ``*`` / ``a`` / ``a-b`` / ``a,b`` /
``*/step`` / ``a-b/step``；周字段 0=周日…6=周六（7 亦作周日）。日/周同时受限时
按经典 cron 语义取 **或**（任一匹配即触发）。

本模块**不依赖 croniter**（平台未装），自带最小解析器。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

#: 搜索上限：一次性/间隔以外的 cron 若 5 年内无匹配，视为永不触发。
_CRON_HORIZON_DAYS = 366 * 5


def next_fire(
    schedule: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """把 ``schedule`` 解析为**下次触发时间**（无触发则 ``None``）。

    :param schedule: 任务 ``schedule`` JSONB（``None``/空 → 无定时，返回 ``None``）。
    :param now: 基准时刻（缺省 ``datetime.now()``；显式传入即为纯函数）。
    :raises ValueError: ``schedule`` 形态非法（未知 kind / 缺字段 / cron 表达式坏）。
    """
    if not schedule:
        return None
    if not isinstance(schedule, Mapping):
        raise ValueError(f"schedule 必须是对象：{schedule!r}")

    base = now or datetime.now()
    kind = schedule.get("kind")
    if kind == "interval":
        return _next_interval(schedule, base)
    if kind == "once":
        return _next_once(schedule, base)
    if kind == "cron":
        return _next_cron(schedule, base)
    raise ValueError(f"未知 schedule.kind：{kind!r}（可选：cron/interval/once）")


# ── interval ────────────────────────────────────────────────────
def _next_interval(schedule: Mapping[str, Any], now: datetime) -> datetime:
    """至少给一个 ``minutes`` / ``seconds`` / ``hours``（02 §4 以 minutes 为准）。"""
    parts = {
        key: _optional_number(schedule.get(key), f"schedule.{key}")
        for key in ("minutes", "seconds", "hours")
    }
    if not any(key in schedule for key in parts):
        raise ValueError("schedule.kind=interval 需要 minutes（或 seconds/hours）")
    delta = timedelta(
        minutes=parts["minutes"], seconds=parts["seconds"], hours=parts["hours"]
    )
    if delta <= timedelta(0):
        raise ValueError("schedule 间隔必须为正（minutes/seconds/hours）")
    return now + delta


# ── once ────────────────────────────────────────────────────────
def _next_once(schedule: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    """``at`` 是 ISO8601；已过则返回 ``None``（一次性任务不再触发）。"""
    raw = schedule.get("at")
    if not raw:
        raise ValueError("schedule.kind=once 需要 at（ISO8601 时间）")
    try:
        at = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"schedule.at 不是合法 ISO8601 时间：{raw!r}") from exc
    return at if at > now else None


# ── cron ────────────────────────────────────────────────────────
def _next_cron(schedule: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    expr = schedule.get("expr")
    if not expr or not str(expr).strip():
        raise ValueError("schedule.kind=cron 需要 expr（5 字段 cron 表达式）")
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式须为 5 字段（分 时 日 月 周）：{expr!r}")
    minutes, hours, doms, months, dows = (_parse_cron_field(f, lo, hi) for f, lo, hi in zip(
        fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)
    ))
    dom_restricted = fields[2].strip() != "*"
    dow_restricted = fields[4].strip() != "*"
    dows = {0 if d == 7 else d for d in dows}  # 7 归一为周日

    # 从下一分钟起找第一个匹配（跨不匹配的日/小时整段跳过）。
    cur = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = cur + timedelta(days=_CRON_HORIZON_DAYS)
    while cur <= end:
        if not _day_ok(cur, doms, months, dows, dom_restricted, dow_restricted):
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if cur.hour not in hours:
            cur = (cur + timedelta(hours=1)).replace(minute=0)
            continue
        if cur.minute not in minutes:
            cur = cur + timedelta(minutes=1)
            continue
        return cur
    return None


def _day_ok(
    moment: datetime,
    doms: set[int],
    months: set[int],
    dows: set[int],
    dom_restricted: bool,
    dow_restricted: bool,
) -> bool:
    if moment.month not in months:
        return False
    cron_dow = (moment.weekday() + 1) % 7  # Python 周一=0 → cron 周日=0
    if dom_restricted and dow_restricted:
        return moment.day in doms or cron_dow in dows
    if dom_restricted:
        return moment.day in doms
    if dow_restricted:
        return cron_dow in dows
    return True


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """解析单个 cron 字段 → 取值集合。"""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron 字段含空项：{field!r}")
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise ValueError(f"cron 步长非法：{raw_step!r}") from exc
            if step <= 0:
                raise ValueError(f"cron 步长必须为正：{step}")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = _cron_int(a, field), _cron_int(b, field)
        else:
            value = _cron_int(part, field)
            if step != 1:
                start, end = value, hi
            else:
                values.add(value)
                continue
        if start > end:
            raise ValueError(f"cron 区间倒置：{part!r}")
        values.update(range(start, end + 1, step))
    return values


def _cron_int(text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"cron 字段含非整数：{text!r}（字段 {field!r}）") from exc


def _optional_number(value: Any, field: str) -> float:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字：{value!r}")
    return float(value)


__all__ = ["next_fire"]
