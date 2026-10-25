"""测试策略服务（strategy）——一轮测试的执行分配层（用户定版 2026-10-14）。

全异步、纯 service 层函数（无类状态），供能力面/前端页面共用：

* **CRUD** —— :func:`create_strategy` / :func:`update_strategy` / :func:`delete_strategy`；
  ``update_strategy`` 走变更门（必填 ``change_reason``，复用 assets 的
  ``record_change`` 落 ``mc_test_change_record``，entity_type="strategy"）。
* **用例勾选** —— :func:`set_strategy_cases`（勾选即建行，可同时带
  ``assignee_id``/``exec_mode``）；:func:`assign_case`（改派/改执行方式）；
  :func:`remove_case`（取消勾选）。
* **结果录入** —— :func:`record_case_result`（人工执行/自动化回写同一落点，
  ``result`` + ``note`` + ``executed_at``/``executed_by``）。
* **读** —— :func:`get_strategy`（策略 + 勾选用例 + 结果汇总）、
  :func:`list_strategies`（可按 version/strategy_type/status 收窄）、
  :func:`strategy_summary`（结果汇总：总数/通过/失败/阻塞/跳过/执行率）。

**bug 第二维度**：bug 归属 = 版本（``version_id``）× 策略（``strategy_id``）；
按策略过滤 bug 用 ``testing_platform.defects.list_bugs`` 后按
``strategy_id`` 过滤（bug 表加列见 defects/models.py）。

code 自增 ``STRAT-0001``，按 ``project_id`` 内 max+1（与 assets ``_next_code``
同款，字典序陷阱同避）。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from tortoise.exceptions import DoesNotExist

from testing_platform.assets.service import (
    AssetNotFoundError,
    AssetValidationError,
    _as_uuid,
    _maybe_uuid,
    _next_code,
    _validate_enum,
    record_change,
)

from .models import (
    STRATEGY_CASE_RESULTS,
    STRATEGY_EXEC_MODES,
    STRATEGY_STATUSES,
    STRATEGY_TYPES,
    TestStrategy,
    TestStrategyCase,
)

STRATEGY_CODE_PREFIX = "STRAT-"


class StrategyNotFoundError(AssetNotFoundError):
    """策略/策略用例行不存在（继承 assets 的 LookupError，能力面统一翻译）。"""


async def _get_strategy(strategy_id: Any) -> TestStrategy:
    sid = _as_uuid(strategy_id, field="strategy_id")
    try:
        return await TestStrategy.get(id=sid)
    except DoesNotExist as exc:
        raise StrategyNotFoundError(f"测试策略不存在：{strategy_id!r}") from exc


def _parse_dt(value: Any, *, field: str) -> Optional[datetime]:
    """ISO 字符串 / datetime / None → naive datetime（Tortoise use_tz=False）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise AssetValidationError(f"{field} 不是合法时间：{value!r}（ISO 格式）") from exc


def _normalize_id_list(value: Any) -> list[str]:
    """团队名单归一：UUID/str（单值或列表）→ 去重保序的 str(id) 列表；``None`` → 空列表。

    空存 ``[]`` 而非 null（与 ``JSONField(default=list)`` 一致，前端读恒为数组）。
    """
    if value is None:
        return []
    if isinstance(value, (str, _uuid.UUID)):
        seq: list[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        seq = list(value)
    else:
        raise AssetValidationError(f"人员名单不合法：{value!r}（应为 id 列表）")
    out: list[str] = []
    seen: set[str] = set()
    for raw in seq:
        if raw is None or raw == "":
            continue
        sid = str(_as_uuid(raw, field="member_id"))
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _strategy_document(
    strategy: TestStrategy,
    cases: Optional[Sequence[TestStrategyCase]] = None,
    *,
    summary: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "id": str(strategy.id),
        "code": strategy.code,
        "project_id": str(strategy.project_id),
        "version_id": str(strategy.version_id) if strategy.version_id else None,
        "name": strategy.name,
        "strategy_type": strategy.strategy_type,
        "description": strategy.description,
        "status": strategy.status,
        "start_time": strategy.start_time.isoformat() if strategy.start_time else None,
        "end_time": strategy.end_time.isoformat() if strategy.end_time else None,
        "actual_end_time": strategy.actual_end_time.isoformat() if strategy.actual_end_time else None,
        "delay_analysis": strategy.delay_analysis,
        "owner_id": str(strategy.owner_id) if strategy.owner_id else None,
        "testers": list(strategy.testers or []),
        "developers": list(strategy.developers or []),
        "created_by": str(strategy.created_by) if strategy.created_by else None,
        "created_at": strategy.created_at.isoformat() if strategy.created_at else None,
        "updated_at": strategy.updated_at.isoformat() if strategy.updated_at else None,
        "cases": [_case_document(c) for c in (cases or [])],
        "summary": dict(summary) if summary is not None else None,
    }


def _case_document(row: TestStrategyCase) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "strategy_id": str(row.strategy_id),
        "case_id": str(row.case_id),
        "assignee_id": str(row.assignee_id) if row.assignee_id else None,
        "exec_mode": row.exec_mode,
        "result": row.result,
        "note": row.note,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "executed_by": str(row.executed_by) if row.executed_by else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def summarize(
    cases: Sequence[Mapping[str, Any] | TestStrategyCase],
) -> dict[str, Any]:
    """结果汇总：总数/各结果计数/执行率/通过率（``result != pending`` 算已执行）。"""
    total = len(cases)
    counts = {key: 0 for key in STRATEGY_CASE_RESULTS}
    for c in cases:
        result = c.get("result") if isinstance(c, Mapping) else c.result
        counts[result] = counts.get(result, 0) + 1
    executed = total - counts.get("pending", 0)
    passed = counts.get("pass", 0)
    return {
        "total": total,
        "by_result": counts,
        "executed": executed,
        "execution_rate": round(executed / total, 4) if total else 0.0,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }


# ── CRUD ────────────────────────────────────────────────────────
async def create_strategy(
    *,
    project_id: Any,
    name: str,
    strategy_type: str = "smoke",
    version_id: Any = None,
    description: Optional[str] = None,
    status: str = "draft",
    start_time: Any = None,
    end_time: Any = None,
    actual_end_time: Any = None,
    delay_analysis: Optional[str] = None,
    owner_id: Any = None,
    testers: Any = None,
    developers: Any = None,
    created_by: Any = None,
    case_ids: Optional[Sequence[Any]] = None,
) -> TestStrategy:
    """建测试策略（一轮测试）。``case_ids`` 非空时同步勾选用例。

    ``testers``/``developers`` 是本轮团队名单（平台用户 id 列表；用例级执行人另在
    :class:`TestStrategyCase.assignee_id`）。``actual_end_time``/``delay_analysis``
    供报告页延期分析用（用户定版 2026-10-16）。
    """
    if not name or not str(name).strip():
        raise AssetValidationError("测试策略 name 必填")
    strategy_type = _validate_enum(strategy_type, STRATEGY_TYPES, field="strategy_type", required=True)
    status = _validate_enum(status, STRATEGY_STATUSES, field="status", required=False) or "draft"
    if status not in ("draft", "pending"):
        raise AssetValidationError(f"新建策略 status 只能是 draft/pending，不是 {status!r}")

    strategy = await TestStrategy.create(
        id=_uuid.uuid4(),
        code=await _next_code(TestStrategy, project_id, STRATEGY_CODE_PREFIX),
        project_id=_as_uuid(project_id, field="project_id"),
        version_id=_maybe_uuid(version_id, field="version_id"),
        name=str(name).strip(),
        strategy_type=strategy_type,
        description=description,
        status=status,
        start_time=_parse_dt(start_time, field="start_time"),
        end_time=_parse_dt(end_time, field="end_time"),
        actual_end_time=_parse_dt(actual_end_time, field="actual_end_time"),
        delay_analysis=(str(delay_analysis).strip() or None) if delay_analysis else None,
        owner_id=_maybe_uuid(owner_id, field="owner_id"),
        testers=_normalize_id_list(testers),
        developers=_normalize_id_list(developers),
        created_by=_maybe_uuid(created_by, field="created_by"),
    )
    if case_ids:
        await set_strategy_cases(strategy.id, case_ids, created_by=created_by)
    return strategy


async def update_strategy(
    strategy_id: Any,
    *,
    change_reason: str,
    values: Optional[Mapping[str, Any]] = None,
    created_by: Any = None,
) -> TestStrategy:
    """改策略（变更门：``change_reason`` 必填，落 ``mc_test_change_record``）。

    可改字段：name / strategy_type / version_id / description / status /
    start_time / end_time / owner_id / testers / developers。
    """
    strategy = await _get_strategy(strategy_id)
    values = dict(values or {})
    if not change_reason or not str(change_reason).strip():
        raise AssetValidationError("strategy.update 必须提供 change_reason（变更门）")
    allowed = ("name", "strategy_type", "version_id", "description", "status",
               "start_time", "end_time", "actual_end_time", "delay_analysis",
               "owner_id", "testers", "developers")
    unknown = [key for key in values if key not in allowed]
    if unknown:
        raise AssetValidationError(
            f"strategy 不允许修改字段：{unknown}（可改：{list(allowed)}）"
        )
    if not values:
        raise AssetValidationError("strategy.update 未提供任何字段")

    if "strategy_type" in values:
        values["strategy_type"] = _validate_enum(
            values["strategy_type"], STRATEGY_TYPES, field="strategy_type", required=True)
    if "status" in values:
        values["status"] = _validate_enum(
            values["status"], STRATEGY_STATUSES, field="status", required=True)
    if "version_id" in values:
        values["version_id"] = _maybe_uuid(values["version_id"], field="version_id")
    if "owner_id" in values:
        values["owner_id"] = _maybe_uuid(values["owner_id"], field="owner_id")
    if "start_time" in values:
        values["start_time"] = _parse_dt(values["start_time"], field="start_time")
    if "end_time" in values:
        values["end_time"] = _parse_dt(values["end_time"], field="end_time")
    if "actual_end_time" in values:
        values["actual_end_time"] = _parse_dt(values["actual_end_time"], field="actual_end_time")
    if "delay_analysis" in values:
        values["delay_analysis"] = (str(values["delay_analysis"]).strip() or None) if values["delay_analysis"] else None
    if "testers" in values:
        values["testers"] = _normalize_id_list(values["testers"])
    if "developers" in values:
        values["developers"] = _normalize_id_list(values["developers"])
    if "name" in values:
        name = str(values["name"] or "").strip()
        if not name:
            raise AssetValidationError("测试策略 name 必填")
        values["name"] = name

    field_changes: dict[str, dict[str, Any]] = {}
    for key, new_value in values.items():
        old_value = getattr(strategy, key)
        if old_value != new_value:
            field_changes[key] = {"from": old_value.isoformat() if isinstance(old_value, datetime) else old_value,
                                  "to": new_value.isoformat() if isinstance(new_value, datetime) else new_value}
        setattr(strategy, key, new_value)

    strategy.version = int(strategy.version or 1) + 1
    await strategy.save()

    await record_change(
        "strategy",
        strategy.id,
        change_reason=str(change_reason).strip(),
        created_by=created_by,
        field_changes=field_changes,
    )
    return strategy


async def delete_strategy(strategy_id: Any, *, created_by: Any = None) -> bool:
    """删策略（真删：主行 + 勾选行）。历史 run/bug 不动。"""
    strategy = await _get_strategy(strategy_id)
    await TestStrategyCase.filter(strategy_id=strategy.id).delete()
    await strategy.delete()
    if created_by:
        try:
            await record_change("strategy", strategy.id, change_reason="删除测试策略",
                                created_by=created_by)
        except AssetValidationError:
            pass  # 变更记录挂在已删实体上没有读侧；尽力而为
    return True


# ── 用例勾选 + 分配 ─────────────────────────────────────────────
async def set_strategy_cases(
    strategy_id: Any,
    case_ids: Sequence[Any],
    *,
    assignee_id: Any = None,
    exec_mode: Optional[str] = None,
    created_by: Any = None,
) -> dict[str, Any]:
    """全量重设策略勾选的用例（差异同步）：新勾选建行（可带默认
    ``assignee_id``/``exec_mode``），取消勾选删行，已有行保留各自结果/分配。

    返回 ``{"added": [...], "removed": [...], "kept": n}``。
    """
    strategy = await _get_strategy(strategy_id)
    sid = strategy.id
    wanted: list[_uuid.UUID] = []
    seen: set[_uuid.UUID] = set()
    for raw in case_ids:
        cid = _as_uuid(raw, field="case_ids[]")
        if cid not in seen:
            seen.add(cid)
            wanted.append(cid)

    existing_rows = await TestStrategyCase.filter(strategy_id=sid)
    existing = {row.case_id: row for row in existing_rows}

    mode = None
    if exec_mode is not None:
        mode = _validate_enum(exec_mode, STRATEGY_EXEC_MODES, field="exec_mode", required=True)
    assignee = _maybe_uuid(assignee_id, field="assignee_id")

    added: list[str] = []
    removed: list[str] = []
    for cid in wanted:
        if cid in existing:
            continue
        await TestStrategyCase.create(
            id=_uuid.uuid4(),
            strategy_id=sid,
            case_id=cid,
            assignee_id=assignee,
            exec_mode=mode or "manual",
        )
        added.append(str(cid))
    for cid, row in existing.items():
        if cid not in seen:
            await row.delete()
            removed.append(str(cid))

    await strategy.save(update_fields=["updated_at"])
    if (added or removed) and created_by is not None:
        try:
            await record_change(
                "strategy", sid,
                change_reason=f"勾选用例：+{len(added)} -{len(removed)}",
                created_by=created_by,
                field_changes={"cases": {"added": added, "removed": removed}},
            )
        except AssetValidationError:
            pass
    return {"added": added, "removed": removed, "kept": len(existing) - len(removed)}


async def assign_case(
    strategy_id: Any,
    case_id: Any,
    *,
    assignee_id: Any = None,
    exec_mode: Optional[str] = None,
    created_by: Any = None,
) -> TestStrategyCase:
    """改派/改执行方式（勾选行必须已存在——先勾选再分配）。"""
    sid = _as_uuid(strategy_id, field="strategy_id")
    cid = _as_uuid(case_id, field="case_id")
    row = await TestStrategyCase.get_or_none(strategy_id=sid, case_id=cid)
    if row is None:
        raise StrategyNotFoundError(f"该用例未勾选进策略：strategy={strategy_id!r} case={case_id!r}")
    if assignee_id is not None:
        row.assignee_id = _maybe_uuid(assignee_id, field="assignee_id")
    if exec_mode is not None:
        row.exec_mode = _validate_enum(exec_mode, STRATEGY_EXEC_MODES, field="exec_mode", required=True)
    await row.save(update_fields=["assignee_id", "exec_mode", "updated_at"])
    return row


async def remove_case(strategy_id: Any, case_id: Any) -> bool:
    """取消勾选单条用例（连同其分配与结果）。"""
    sid = _as_uuid(strategy_id, field="strategy_id")
    cid = _as_uuid(case_id, field="case_id")
    row = await TestStrategyCase.get_or_none(strategy_id=sid, case_id=cid)
    if row is None:
        raise StrategyNotFoundError(f"该用例未勾选进策略：strategy={strategy_id!r} case={case_id!r}")
    await row.delete()
    return True


# ── 结果录入 ────────────────────────────────────────────────────
async def record_case_result(
    strategy_id: Any,
    case_id: Any,
    *,
    result: str,
    note: Optional[str] = None,
    executed_by: Any = None,
) -> TestStrategyCase:
    """录测试结果（人工执行与自动化回写同一落点）。

    ``result`` 枚举见 :data:`STRATEGY_CASE_RESULTS`；``executed_at`` 由服务端打点。
    """
    sid = _as_uuid(strategy_id, field="strategy_id")
    cid = _as_uuid(case_id, field="case_id")
    row = await TestStrategyCase.get_or_none(strategy_id=sid, case_id=cid)
    if row is None:
        raise StrategyNotFoundError(f"该用例未勾选进策略：strategy={strategy_id!r} case={case_id!r}")
    if result not in STRATEGY_CASE_RESULTS:
        raise AssetValidationError(
            f"result={result!r} 不在允许集合 {list(STRATEGY_CASE_RESULTS)} 内"
        )
    row.result = result
    if note is not None:
        row.note = note
    # 平台 use_tz=False：落库统一 naive UTC（与 assets/task 同款口径）。
    row.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.executed_by = _maybe_uuid(executed_by, field="executed_by")
    await row.save(update_fields=["result", "note", "executed_at", "executed_by", "updated_at"])
    return row


# ── 读 ──────────────────────────────────────────────────────────
async def get_strategy(strategy_id: Any, *, with_cases: bool = True) -> dict[str, Any]:
    """读策略 + 勾选用例 + 结果汇总。不存在抛 :class:`StrategyNotFoundError`。"""
    strategy = await _get_strategy(strategy_id)
    cases: list[TestStrategyCase] = []
    if with_cases:
        cases = await TestStrategyCase.filter(strategy_id=strategy.id).order_by("created_at")
    return _strategy_document(strategy, cases, summary=summarize(cases))


async def strategy_report(strategy_id: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """一轮测试的统计报告（用户定版 2026-10-16）。

    用例段（勾选数/自动化数/结果分布）+ 缺陷段（总数/P0-P1/平均·最长·最短解决时长）
    + 延时段（计划 end_time vs actual_end_time，未收尾按当前时间算已超期）。
    取数：``get_strategy`` + ``defects.list_bugs(strategy_id=...)``；计算是纯函数
    :func:`testing_platform.strategy.report.compute_strategy_report`。
    """
    from ..defects import list_bugs
    from .report import compute_strategy_report

    doc = await get_strategy(strategy_id)
    bugs = await list_bugs(doc["project_id"], strategy_id=doc["id"])
    return compute_strategy_report(doc, bugs, now=now)


async def list_strategies(
    project_id: Any,
    *,
    version_id: Any = None,
    strategy_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """列项目下测试策略（可按 version/strategy_type/status 收窄），带轻量汇总。"""
    pid = _as_uuid(project_id, field="project_id")
    query = TestStrategy.filter(project_id=pid)
    if version_id is not None and version_id != "":
        query = query.filter(version_id=_as_uuid(version_id, field="version_id"))
    if strategy_type:
        if strategy_type not in STRATEGY_TYPES:
            raise AssetValidationError(
                f"strategy_type={strategy_type!r} 不在允许集合 {list(STRATEGY_TYPES)} 内")
        query = query.filter(strategy_type=strategy_type)
    if status:
        if status not in STRATEGY_STATUSES:
            raise AssetValidationError(
                f"status={status!r} 不在允许集合 {list(STRATEGY_STATUSES)} 内")
        query = query.filter(status=status)
    strategies = await query.order_by("-created_at")
    if not strategies:
        return []
    rows = await TestStrategyCase.filter(strategy_id__in=[s.id for s in strategies])
    by_strategy: dict[str, list[TestStrategyCase]] = {}
    for row in rows:
        by_strategy.setdefault(str(row.strategy_id), []).append(row)
    return [
        _strategy_document(s, by_strategy.get(str(s.id), []),
                           summary=summarize(by_strategy.get(str(s.id), [])))
        for s in strategies
    ]
