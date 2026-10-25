"""缺陷服务（defects）—— **测试域自持 bug，与 coding 平台彻底解耦**。

⚠️ **解耦改版（用户定版）**：bug 不再是 coding 的 ProjectIssue。本包读写自己的
``mc_test_bug`` 表（``project_id`` 直接是**测试项目 id**），**不需要任何 coding 项目桥**
（删除了旧的 ``_resolve_coding_project_id``）。测试缺陷与 coding 项目没有任何关系。

失败分流判 product_bug → 建 :class:`TestBug` + 挂来源执行/关联；diagnose 流程写根因
（根因门）；**状态机由本包自理**，走动作守卫表 :data:`BUG_ACTIONS`（禅道对齐）：
``open→confirmed→triaged→resolved→verified→closed``（``rejected`` 为
``resolution=willnotfix/bydesign`` 的终态），**reopen 是动作**（``activated_count+1``），
不是独立状态。
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from .models import (
    BUG_ACTIONS,
    BUG_LONG_LIFE_DAYS,
    BUG_PRIORITIES,
    BUG_RESOLUTIONS,
    BUG_SEVERITIES,
    BUG_STATUS_ALIASES,
    BUG_STATUSES,
    BUG_STATUS_LABELS,
    BUG_TYPES,
    ROOT_CAUSE_CLASSIFICATIONS,
    TERMINAL_NOT_EDITABLE,
    BugFieldRequiredError,
    BugTransitionNotAllowedError,
    DefectNotFoundError,
    DefectValidationError,
    RootCauseEvidenceRequiredError,
    TestBug,
    TestBugLog,
    TestBugPattern,
    TestBugRootCause,
    available_actions,
    reopen_target,
)

__all__ = [
    "apply_bug_action",
    "attach_root_cause",
    "create_bug_from_failure",
    "delete_bug",
    "find_bug_by_fingerprint",
    "get_bug",
    "list_bug_log",
    "list_bugs",
    "list_patterns",
    "update_bug",
    "upsert_pattern",
]

BUG_CODE_PREFIX = "BUG-"

#: ``update_bug`` 可改的非状态字段（状态只能走 apply_bug_action，防绕过守卫表）
_BUG_EDITABLE_FIELDS: tuple[str, ...] = (
    "title", "symptom", "summary", "resolution_note", "severity", "priority",
    "module", "assignee_id", "version_id", "strategy_id", "requirement_id",
    "tags", "repro", "case_refs", "scenario_refs", "scenario_id",
    "bug_type", "opened_build", "resolved_build", "duplicate_bug_id",
    "related_bug_ids", "deadline", "found_in", "escaped", "os", "browser",
    "hardware", "keywords", "mailto", "feedback_by", "sub_status", "color",
    "folder_id",
)

#: 动作可写字段 = 可编辑字段 + 动作专属字段（``resolution`` 只能经 resolve 动作写，
#: 不进 ``_BUG_EDITABLE_FIELDS``，防 update_bug 绕过状态机直接改解决方案）。
_BUG_ACTION_FIELDS: tuple[str, ...] = _BUG_EDITABLE_FIELDS + ("resolution",)

#: 需要 UUID 归一化的字段
_BUG_UUID_FIELDS: frozenset[str] = frozenset({
    "assignee_id", "version_id", "strategy_id", "requirement_id", "scenario_id",
    "duplicate_bug_id", "folder_id",
})

#: 需要 JSON 归一化的字段
_BUG_JSON_FIELDS: frozenset[str] = frozenset({
    "tags", "repro", "case_refs", "scenario_refs", "related_bug_ids", "mailto",
})


# ── 小工具 ──────────────────────────────────────────────────────
def _as_uuid(value: Any, *, field: str) -> _uuid.UUID:
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DefectValidationError(f"{field} 不是合法 UUID：{value!r}") from exc


def _maybe_uuid(value: Any) -> Optional[_uuid.UUID]:
    if value is None or value == "":
        return None
    return _as_uuid(value, field="id")


def _now() -> datetime:
    """朴素 UTC（与 mc_* 表 use_tz=False 的列匹配）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    """aware → naive（丢弃 tzinfo）；naive 原样返回。

    生产库 use_tz=False 读出的是朴素值，但测试 fixture / 某些驱动会带 tzinfo——
    派生位（超期/长期未决）做时间减法前必须归一，否则 naive-aware 相减抛
    ``TypeError``。
    """
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _normalize_status(value: Any) -> Optional[str]:
    """状态入参归一：接受旧别名（``fixed`` → ``resolved``），非法值抛。

    ``None`` / ``""`` → ``None``（调用方按可选处理）。
    """
    if value is None or value == "":
        return None
    status = str(value)
    status = BUG_STATUS_ALIASES.get(status, status)
    if status not in BUG_STATUSES:
        raise DefectValidationError(
            f"status={value!r} 不在允许集合 {list(BUG_STATUSES)} 内"
        )
    return status


def _jsonify(value: Any) -> Any:
    """JSONB 可写形态：UUID 等转字符串，容器递归。"""
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    return str(value)


def _id_refs(value: Any) -> list[str]:
    """case_refs / scenario_refs 归一为 str(id) 列表。"""
    if value is None:
        return []
    if isinstance(value, (str, _uuid.UUID)):
        return [str(value)]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v]
    raise DefectValidationError(f"引用列表不合法：{value!r}（应为 id 列表）")


def _coerce_datetime(value: Any, *, field: str) -> Optional[datetime]:
    """入参 → 朴素 datetime（ISO 字符串 / datetime / None）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError) as exc:
        raise DefectValidationError(f"{field} 不是合法时间：{value!r}（ISO 格式）") from exc
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


async def _next_code(project_id: Any) -> str:
    """``BUG-`` + 4 位递增，按 project 内 max+1。"""
    pid = _as_uuid(project_id, field="project_id")
    codes = await TestBug.filter(project_id=pid).values_list("code", flat=True)
    numbers: list[int] = []
    for code in codes:
        if isinstance(code, str) and code.startswith(BUG_CODE_PREFIX):
            tail = code[len(BUG_CODE_PREFIX):]
            if tail.isdigit():
                numbers.append(int(tail))
    return f"{BUG_CODE_PREFIX}{(max(numbers) + 1) if numbers else 1:04d}"


# ── 建 bug（测试域自持，不经过 coding project_service）────────────
async def create_bug_from_failure(
    run_scenario_id: Any = None,
    title: str = "",
    symptom: Optional[str] = None,
    fingerprint: Optional[str] = None,
    repro: Optional[Mapping[str, Any]] = None,
    case_refs: Any = None,
    scenario_refs: Any = None,
    user_id: Any = None,
    project_id: Any = None,
    *,
    from_run_id: Any = None,
    assignee_id: Any = None,
    priority: int = 2,
    tags: Any = None,
    summary: Optional[str] = None,
    scenario_id: Any = None,
    severity: str = "major",
    module: Optional[str] = None,
    version_id: Any = None,
    strategy_id: Any = None,
    requirement_id: Any = None,
    # ── 禅道对齐新增入参 ──
    bug_type: Optional[str] = None,
    resolution: Optional[str] = None,
    opened_build: Optional[str] = None,
    resolved_build: Optional[str] = None,
    duplicate_bug_id: Any = None,
    related_bug_ids: Any = None,
    deadline: Any = None,
    found_in: Optional[str] = None,
    escaped: bool = False,
    os: Optional[str] = None,
    browser: Optional[str] = None,
    hardware: Optional[str] = None,
    keywords: Optional[str] = None,
    mailto: Any = None,
    feedback_by: Optional[str] = None,
    sub_status: Optional[str] = None,
    color: Optional[str] = None,
    confirmed: bool = False,
    folder_id: Any = None,
) -> str:
    """失败分流判 ``product_bug`` → 建 ``mc_test_bug``。

    **去重**：``fingerprint`` 已有同签名 bug → 归并返回既有 ``bug_id``，不重复建。

    ``project_id`` 是**测试项目 id**（不再是 coding 项目）。
    ``run_scenario_id`` 可空——**手动录入缺陷**（无来源运行）时留空。
    ``confirmed=True`` 时状态直落 ``confirmed``（人工录的 bug 通常已认可）。
    """
    clean_title = (title or "").strip()
    if not clean_title:
        raise DefectValidationError("bug title 必填")
    if project_id is None:
        raise DefectValidationError("project_id 必填（测试项目 id）")

    # 去重：同指纹已有 bug → 归并。
    existing = await find_bug_by_fingerprint(fingerprint) if fingerprint else None
    if existing is not None:
        return existing

    tag_list = [str(t).strip() for t in tags if str(t).strip()] if isinstance(tags, list) else []
    if "test_discovered" not in tag_list:
        tag_list.insert(0, "test_discovered")

    if severity not in BUG_SEVERITIES:
        severity = "major"
    if priority not in BUG_PRIORITIES:
        priority = 2
    if bug_type is not None and bug_type not in BUG_TYPES:
        raise DefectValidationError(
            f"bug_type={bug_type!r} 不在允许集合 {list(BUG_TYPES)} 内"
        )
    if resolution is not None and resolution not in BUG_RESOLUTIONS:
        raise DefectValidationError(
            f"resolution={resolution!r} 不在允许集合 {list(BUG_RESOLUTIONS)} 内"
        )
    if found_in is not None and found_in not in ("test", "production", "feedback"):
        raise DefectValidationError(f"found_in={found_in!r} 非法（test/production/feedback）")

    bug = await TestBug.create(
        id=_uuid.uuid4(),
        code=await _next_code(project_id),
        project_id=_as_uuid(project_id, field="project_id"),
        title=clean_title,
        symptom=(symptom or "").strip() or None,
        status="confirmed" if confirmed else "open",
        severity=severity,
        priority=priority,
        summary=summary,
        resolution_note=None,
        resolution=resolution,
        fingerprint=fingerprint,
        repro=_jsonify(dict(repro)) if repro else {},
        case_refs=_id_refs(case_refs),
        scenario_refs=_id_refs(scenario_refs),
        scenario_id=_maybe_uuid(scenario_id),
        from_run_scenario_id=_maybe_uuid(run_scenario_id),
        from_run_id=_maybe_uuid(from_run_id),
        assignee_id=_maybe_uuid(assignee_id),
        created_by=_maybe_uuid(user_id),
        module=module,
        version_id=_maybe_uuid(version_id),
        strategy_id=_maybe_uuid(strategy_id),
        requirement_id=_maybe_uuid(requirement_id),
        tags=_jsonify(tag_list),
        # ── 禅道对齐新增列 ──
        bug_type=bug_type,
        confirmed=bool(confirmed),
        confirmed_at=_now() if confirmed else None,
        opened_build=opened_build,
        resolved_build=resolved_build,
        duplicate_bug_id=_maybe_uuid(duplicate_bug_id),
        related_bug_ids=_id_refs(related_bug_ids),
        deadline=_coerce_datetime(deadline, field="deadline"),
        found_in=found_in or "test",
        escaped=bool(escaped),
        os=os,
        browser=browser,
        hardware=hardware,
        keywords=keywords,
        mailto=_jsonify(list(mailto)) if isinstance(mailto, (list, tuple)) else [],
        feedback_by=feedback_by,
        sub_status=sub_status,
        color=color,
        folder_id=_maybe_uuid(folder_id),
    )
    await _write_log(
        bug.id, action="create", from_status=None, to_status=bug.status,
        actor=_maybe_uuid(user_id),
        extra={"title": clean_title, "bug_type": bug_type, "severity": severity},
    )
    return str(bug.id)


# ── 指纹查询（去重用）────────────────────────────────────────────
async def find_bug_by_fingerprint(fingerprint: Optional[str]) -> Optional[str]:
    """按失败指纹找既有 bug，命中返回 ``bug_id``（str），否则 ``None``。"""
    if not fingerprint:
        return None
    bug = await TestBug.get_or_none(fingerprint=fingerprint)
    return str(bug.id) if bug is not None else None


# ── 活动流（每次动作/更新记一行）────────────────────────────────
async def _write_log(
    bug_id: _uuid.UUID,
    *,
    action: str,
    from_status: Optional[str],
    to_status: Optional[str],
    actor: Optional[_uuid.UUID],
    comment: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> None:
    """写一行缺陷活动流。失败不抛——活动流是留痕，不能阻塞主流程。"""
    try:
        await TestBugLog.create(
            id=_uuid.uuid4(),
            bug_id=bug_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            comment=comment,
            extra=_jsonify(dict(extra)) if extra else {},
        )
    except Exception:  # noqa: BLE001 —— 留痕失败不阻塞主流程
        pass


async def list_bug_log(bug_id: Any) -> list[dict[str, Any]]:
    """读单个 bug 的活动流（时间线），按发生时刻升序。"""
    bid = _as_uuid(bug_id, field="bug_id")
    rows = await TestBugLog.filter(bug_id=bid).order_by("occurred_at")
    return [
        {
            "id": str(r.id),
            "bug_id": str(r.bug_id),
            "action": r.action,
            "from_status": r.from_status,
            "to_status": r.to_status,
            "actor": str(r.actor) if r.actor else None,
            "comment": r.comment,
            "extra": r.extra or {},
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]


# ── 状态流转（动作守卫表驱动，唯一入口）────────────────────────
async def apply_bug_action(
    bug_id: Any,
    action: str,
    *,
    actor_id: Any = None,
    comment: Optional[str] = None,
    **fields: Any,
) -> dict[str, Any]:
    """对 bug 执行一个流转动作（``BUG_ACTIONS`` 守卫表驱动，唯一入口）。

    动作 ∈ confirm / assign / triage / resolve / verify / close / reopen。
    **守卫**（禅道 ``isClickable`` 照搬，集中一张表）：
    from-status 不符抛 :class:`BugTransitionNotAllowedError`；
    ``required`` / ``conditional`` 必填未满足抛 :class:`BugFieldRequiredError`
    （``resolve`` 必填 ``resolution``；``fixed``→``resolved_build``、
    ``duplicate``→``duplicate_bug_id`` 条件必填）。

    副作用（按动作定义）：打时间戳（confirmed_at/resolved_at/verified_at/closed_at/
    assigned_at/activated_at）、``reopen`` 自增 ``activated_count`` 并清解决方案/
    各时间戳（confirmed 布尔位保留——重开不清人工认可）、``close`` 清 assignee。

    Returns:
        动作后的完整 bug 文档（同 :func:`get_bug`）。
    """
    bid = _as_uuid(bug_id, field="bug_id")
    spec = BUG_ACTIONS.get(action)
    if spec is None:
        raise DefectValidationError(
            f"未知动作 {action!r}（可选：{'/'.join(BUG_ACTIONS)}）"
        )
    bug = await TestBug.get_or_none(id=bid)
    if bug is None:
        raise DefectNotFoundError(f"bug 不存在：{bug_id!r}")

    current = bug.status
    if current not in spec.from_statuses:
        raise BugTransitionNotAllowedError(
            f"当前状态「{BUG_STATUS_LABELS.get(current, current)}」"
            f"不能执行「{spec.label}」（动作：{action}）",
            action=action,
            from_status=current,
            allowed_actions=available_actions(current, confirmed=bool(bug.confirmed)),
        )

    payload = dict(fields)
    # required 必填
    for req in spec.required:
        value = payload.get(req)
        if value is None or value == "":
            raise BugFieldRequiredError(
                f"动作「{spec.label}」必填字段：{req}", field=req
            )
    # conditional 条件必填（字段=值 时必填）
    for cond_field, cond_value, required_fields in spec.conditional:
        if payload.get(cond_field) == cond_value:
            for req in required_fields:
                value = payload.get(req)
                if value is None or value == "":
                    raise BugFieldRequiredError(
                        f"{cond_field}={cond_value} 时必填字段：{req}", field=req
                    )

    # 归一化 UUID 字段
    for key in list(payload):
        if key in _BUG_UUID_FIELDS:
            payload[key] = _maybe_uuid(payload[key])

    update_fields: list[str] = []
    # to_status（"" = 保持原状态；callable = 按 payload 分流）
    to_status = spec.to_status(payload) if callable(spec.to_status) else spec.to_status
    if to_status and to_status != current:
        bug.status = to_status
        update_fields.append("status")
    # sets_flags
    for flag, flag_value in spec.sets_flags:
        setattr(bug, flag, flag_value)
        update_fields.append(flag)
    # stamps（打服务端时间戳；不覆盖调用方显式传入的值）
    now = _now()
    for stamp in spec.stamps:
        if stamp not in payload:
            setattr(bug, stamp, now)
            update_fields.append(stamp)
    # clears（reopen 清 resolution/各时间戳等）——**总是清空**，且先从 payload 移除，
    # 免得动作字段回写又把值填回去（surface 包装层会把所有可选键都传成 None，
    # 用 "not in payload" 判空会漏清——这是曾经的 bug）。
    for clear_field in spec.clears:
        payload.pop(clear_field, None)
        setattr(bug, clear_field, None)
        update_fields.append(clear_field)
    # bumps（activated_count 自增）
    if spec.bumps:
        setattr(bug, spec.bumps, (getattr(bug, spec.bumps) or 0) + 1)
        update_fields.append(spec.bumps)

    # 动作附带字段（resolution/resolved_build/duplicate_bug_id/assignee_id/...）
    # 只写**非 None** 的——surface 包装层总是把所有可选键传全，None 代表"没填"
    # 而非"清空"（清空走 clears，不由调用方隐式触发）。
    for key, value in payload.items():
        if key in _BUG_ACTION_FIELDS and key not in ("status",) and value is not None:
            if key in _BUG_JSON_FIELDS:
                value = _id_refs(value) if key != "repro" else _jsonify(dict(value))
            setattr(bug, key, value)
            update_fields.append(key)

    # reopen 的目标态依赖 confirmed 布尔位（在 sets_flags 之前已读，须在动作后判定）
    if action == "reopen":
        target = reopen_target(confirmed=bool(bug.confirmed))
        if bug.status != target:
            bug.status = target
            update_fields.append("status")

    if actor_id is not None:
        actor = _maybe_uuid(actor_id)
        if action == "resolve" and bug.resolved_by is None:
            bug.resolved_by = actor
            update_fields.append("resolved_by")
        elif action == "verify" and bug.verified_by is None:
            bug.verified_by = actor
            update_fields.append("verified_by")
        elif action == "close" and bug.closed_by is None:
            bug.closed_by = actor
            update_fields.append("closed_by")
        bug.last_edited_by = actor
        update_fields.append("last_edited_by")

    update_fields.append("updated_at")
    await bug.save(update_fields=list(dict.fromkeys(update_fields)))
    await _write_log(
        bid, action=action, from_status=current, to_status=bug.status,
        actor=_maybe_uuid(actor_id), comment=comment,
        extra={k: _jsonify(v) for k, v in payload.items() if k in _BUG_ACTION_FIELDS},
    )
    return await get_bug(bid)


# ── 编辑（非状态字段，变更门）───────────────────────────────────
async def update_bug(
    bug_id: Any,
    values: Mapping[str, Any],
    *,
    change_reason: str,
    actor_id: Any = None,
) -> dict[str, Any]:
    """改 bug 的**非状态**可编辑字段。

    **变更门**：``change_reason`` 必填（照 assets ``_apply_update`` 的写法）。
    **status 传进来直接拒**——状态只能走 :func:`apply_bug_action`（防绕过守卫表）。
    **closed 拒编辑**（禅道 ``bug_model.php:412`` 守卫）。

    Returns:
        更新后的完整 bug 文档。
    """
    if not change_reason or not str(change_reason).strip():
        raise DefectValidationError("change_reason 必填（一切变更 → 变更门）")
    bid = _as_uuid(bug_id, field="bug_id")
    bug = await TestBug.get_or_none(id=bid)
    if bug is None:
        raise DefectNotFoundError(f"bug 不存在：{bug_id!r}")
    if bug.status in TERMINAL_NOT_EDITABLE:
        raise BugTransitionNotAllowedError(
            "已关闭的缺陷不可编辑（closed 终态守卫）",
            action="update",
            from_status=bug.status,
            allowed_actions=available_actions(bug.status, confirmed=bool(bug.confirmed)),
        )

    data = dict(values or {})
    if "status" in data:
        raise DefectValidationError("status 不能直接改——状态流转走 bug.transition（动作守卫表）")

    changed: dict[str, Any] = {}
    for key, value in data.items():
        if key not in _BUG_EDITABLE_FIELDS:
            raise DefectValidationError(
                f"字段 {key!r} 不可编辑（status 走 bug.transition；其余字段不在白名单）"
            )
        if key in _BUG_UUID_FIELDS:
            value = _maybe_uuid(value)
        elif key in _BUG_JSON_FIELDS:
            value = _id_refs(value) if key != "repro" else _jsonify(dict(value))
        elif key == "deadline":
            value = _coerce_datetime(value, field="deadline")
        elif key == "priority" and value is not None:
            if value not in BUG_PRIORITIES:
                raise DefectValidationError(f"priority={value!r} 越界（{list(BUG_PRIORITIES)}）")
        elif key == "severity" and value is not None:
            if value not in BUG_SEVERITIES:
                raise DefectValidationError(
                    f"severity={value!r} 不在允许集合 {list(BUG_SEVERITIES)} 内"
                )
        elif key == "bug_type" and value is not None:
            if value not in BUG_TYPES:
                raise DefectValidationError(
                    f"bug_type={value!r} 不在允许集合 {list(BUG_TYPES)} 内"
                )
        elif key == "found_in" and value is not None:
            if value not in ("test", "production", "feedback"):
                raise DefectValidationError(f"found_in={value!r} 非法（test/production/feedback）")
        setattr(bug, key, value)
        changed[key] = value

    if not changed:
        raise DefectValidationError("update_bug 未提供任何可编辑字段")

    if actor_id is not None:
        bug.last_edited_by = _maybe_uuid(actor_id)
        changed["last_edited_by"] = bug.last_edited_by
    changed["updated_at"] = _now()
    await bug.save(update_fields=list(changed))
    await _write_log(
        bid, action="update", from_status=bug.status, to_status=bug.status,
        actor=_maybe_uuid(actor_id), comment=str(change_reason).strip(),
        extra={k: _jsonify(v) for k, v in data.items()},
    )
    return await get_bug(bid)


# ── 删除（真删主表 + 根因 + 活动流）────────────────────────────
async def delete_bug(bug_id: Any) -> bool:
    """真删 bug（主表 + 根因行 + 活动流行）。不存在返回 ``False``。"""
    bid = _as_uuid(bug_id, field="bug_id")
    bug = await TestBug.get_or_none(id=bid)
    if bug is None:
        return False
    await TestBugRootCause.filter(bug_id=bid).delete()
    await TestBugLog.filter(bug_id=bid).delete()
    await bug.delete()
    return True


# ── 根因（1:N，过根因门）────────────────────────────────────────
async def attach_root_cause(bug_id: Any, root_cause: Mapping[str, Any]) -> TestBugRootCause:
    """给 bug 追加一条根因分析（1:N）。**根因门**：``evidence_chain`` 空则拒。"""
    bid = _as_uuid(bug_id, field="bug_id")
    data = dict(root_cause or {})

    evidence = data.get("evidence_chain")
    if isinstance(evidence, (str, _uuid.UUID)):
        evidence = [evidence]
    cleaned_evidence = [_jsonify(e) for e in evidence] if isinstance(evidence, (list, tuple, set)) else []
    cleaned_evidence = [e for e in cleaned_evidence if e not in (None, "", {}, [])]
    if not cleaned_evidence:
        raise RootCauseEvidenceRequiredError()

    classification = data.get("classification")
    if classification is not None and classification not in ROOT_CAUSE_CLASSIFICATIONS:
        raise DefectValidationError(
            f"classification={classification!r} 不在允许集合 {list(ROOT_CAUSE_CLASSIFICATIONS)} 内"
        )
    confidence = data.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise DefectValidationError(f"confidence 不是数值：{confidence!r}") from exc
        if not 0.0 <= confidence <= 1.0:
            raise DefectValidationError(f"confidence 越界（0.0-1.0）：{confidence!r}")

    return await TestBugRootCause.create(
        id=_uuid.uuid4(),
        bug_id=bid,
        location=data.get("location"),
        classification=classification,
        attributed_commit=data.get("attributed_commit"),
        hypothesis=data.get("hypothesis"),
        confidence=confidence,
        fix_suggestion=data.get("fix_suggestion"),
        healing_suggestion=data.get("healing_suggestion"),
        blast_radius=_jsonify(data.get("blast_radius") or []),
        evidence_chain=cleaned_evidence,
    )


# ── 读侧（bug + 根因组合）───────────────────────────────────────
def _root_cause_dict(rc: TestBugRootCause) -> dict[str, Any]:
    return {
        "id": str(rc.id),
        "bug_id": str(rc.bug_id),
        "location": rc.location,
        "classification": rc.classification,
        "attributed_commit": rc.attributed_commit,
        "hypothesis": rc.hypothesis,
        "confidence": rc.confidence,
        "fix_suggestion": rc.fix_suggestion,
        "healing_suggestion": rc.healing_suggestion,
        "blast_radius": rc.blast_radius or [],
        "evidence_chain": rc.evidence_chain or [],
        "created_at": rc.created_at.isoformat() if rc.created_at else None,
    }


def _bug_document(bug: TestBug, root_causes: Optional[list[TestBugRootCause]] = None) -> dict[str, Any]:
    status = bug.status
    confirmed = bool(bug.confirmed)
    # 派生位：超期（deadline < now 且未关闭/驳回）、长期未决（存活 > BUG_LONG_LIFE_DAYS 且未解决）
    now = _now()
    deadline = _naive(bug.deadline)
    created_at = _naive(bug.created_at)
    is_overdue = (
        deadline is not None
        and status not in ("closed", "rejected")
        and deadline < now
    )
    is_longlife = (
        created_at is not None
        and status not in ("resolved", "verified", "closed", "rejected")
        and (now - created_at).days >= BUG_LONG_LIFE_DAYS
    )
    return {
        "id": str(bug.id),
        "code": bug.code,
        "project_id": str(bug.project_id) if bug.project_id else None,
        "title": bug.title,
        "symptom": bug.symptom,
        "status": status,
        "severity": bug.severity,
        "priority": bug.priority,
        "summary": bug.summary,
        "resolution_note": bug.resolution_note,
        "resolution": bug.resolution,
        "bug_type": bug.bug_type,
        "fingerprint": bug.fingerprint,
        "repro": bug.repro or {},
        "case_refs": bug.case_refs or [],
        "scenario_refs": bug.scenario_refs or [],
        "scenario_id": str(bug.scenario_id) if bug.scenario_id else None,
        "from_run_scenario_id": str(bug.from_run_scenario_id) if bug.from_run_scenario_id else None,
        "from_run_id": str(bug.from_run_id) if bug.from_run_id else None,
        "assignee_id": str(bug.assignee_id) if bug.assignee_id else None,
        "created_by": str(bug.created_by) if bug.created_by else None,
        "module": bug.module,
        "version_id": str(bug.version_id) if bug.version_id else None,
        "strategy_id": str(bug.strategy_id) if bug.strategy_id else None,
        "requirement_id": str(bug.requirement_id) if bug.requirement_id else None,
        "tags": bug.tags or [],
        # ── 禅道对齐新增字段 ──
        "confirmed": confirmed,
        "confirmed_at": bug.confirmed_at.isoformat() if bug.confirmed_at else None,
        "activated_count": bug.activated_count or 0,
        "activated_at": bug.activated_at.isoformat() if bug.activated_at else None,
        "opened_build": bug.opened_build,
        "resolved_build": bug.resolved_build,
        "duplicate_bug_id": str(bug.duplicate_bug_id) if bug.duplicate_bug_id else None,
        "related_bug_ids": [str(x) for x in (bug.related_bug_ids or [])],
        "deadline": bug.deadline.isoformat() if bug.deadline else None,
        "resolved_at": bug.resolved_at.isoformat() if bug.resolved_at else None,
        "resolved_by": str(bug.resolved_by) if bug.resolved_by else None,
        "verified_at": bug.verified_at.isoformat() if bug.verified_at else None,
        "verified_by": str(bug.verified_by) if bug.verified_by else None,
        "closed_by": str(bug.closed_by) if bug.closed_by else None,
        "found_in": bug.found_in,
        "escaped": bool(bug.escaped),
        "os": bug.os,
        "browser": bug.browser,
        "hardware": bug.hardware,
        "keywords": bug.keywords,
        "mailto": bug.mailto or [],
        "feedback_by": bug.feedback_by,
        "sub_status": bug.sub_status,
        "color": bug.color,
        "assigned_at": bug.assigned_at.isoformat() if bug.assigned_at else None,
        "last_edited_by": str(bug.last_edited_by) if bug.last_edited_by else None,
        "folder_id": str(bug.folder_id) if bug.folder_id else None,
        # ── 派生（前端据此渲染动作菜单与筛选标记）──
        "available_actions": available_actions(status, confirmed=confirmed),
        "is_overdue": is_overdue,
        "is_longlife": is_longlife,
        # ── 关联 ──
        "root_causes": [_root_cause_dict(rc) for rc in (root_causes or [])],
        "created_at": bug.created_at.isoformat() if bug.created_at else None,
        "updated_at": bug.updated_at.isoformat() if bug.updated_at else None,
        "closed_at": bug.closed_at.isoformat() if bug.closed_at else None,
    }


async def list_bugs(
    project_id: Any,
    *,
    version_id: Any = None,
    strategy_id: Any = None,
    status: Any = None,
    status_in: Optional[list[str]] = None,
    severity: Optional[str] = None,
    priority: Optional[int] = None,
    bug_type: Optional[str] = None,
    resolution: Optional[str] = None,
    assignee_id: Any = None,
    found_in: Optional[str] = None,
    escaped: Optional[bool] = None,
    unconfirmed: Optional[bool] = None,
    overdue: Optional[bool] = None,
    longlife: Optional[bool] = None,
    module: Optional[str] = None,
    keyword: Optional[str] = None,
    folder_id: Any = None,
) -> list[dict[str, Any]]:
    """列测试项目下所有缺陷（``project_id`` 是测试项目 id）。

    可按 version_id / strategy_id 收窄——bug 归属 = 测试版本 × 测试策略
    两个维度（``mc_test_bug.strategy_id`` 加列见 models.py）。禅道对齐补全
    筛选维度：status / severity / priority / bug_type / resolution / assignee /
    found_in / escaped / unconfirmed / overdue / longlife / module / keyword；
    ``folder_id`` 按缺陷目录（mc_test_catalog type=bug，**精确匹配**——子树
    过滤由前端拿 catalogs 组树后做，与场景页同一口径）。
    """
    pid = _as_uuid(project_id, field="project_id")
    query = TestBug.filter(project_id=pid)
    if version_id is not None and version_id != "":
        query = query.filter(version_id=_as_uuid(version_id, field="version_id"))
    if strategy_id is not None and strategy_id != "":
        query = query.filter(strategy_id=_as_uuid(strategy_id, field="strategy_id"))
    if folder_id is not None and folder_id != "":
        query = query.filter(folder_id=_as_uuid(folder_id, field="folder_id"))
    if status is not None and status != "":
        query = query.filter(status=_normalize_status(status))
    if status_in:
        query = query.filter(status__in=[_normalize_status(s) for s in status_in])
    if severity:
        if severity not in BUG_SEVERITIES:
            raise DefectValidationError(f"severity={severity!r} 不在允许集合 {list(BUG_SEVERITIES)} 内")
        query = query.filter(severity=severity)
    if priority is not None:
        query = query.filter(priority=priority)
    if bug_type:
        if bug_type not in BUG_TYPES:
            raise DefectValidationError(f"bug_type={bug_type!r} 不在允许集合 {list(BUG_TYPES)} 内")
        query = query.filter(bug_type=bug_type)
    if resolution:
        if resolution not in BUG_RESOLUTIONS:
            raise DefectValidationError(
                f"resolution={resolution!r} 不在允许集合 {list(BUG_RESOLUTIONS)} 内"
            )
        query = query.filter(resolution=resolution)
    if assignee_id is not None and assignee_id != "":
        query = query.filter(assignee_id=_as_uuid(assignee_id, field="assignee_id"))
    if found_in:
        if found_in not in ("test", "production", "feedback"):
            raise DefectValidationError(f"found_in={found_in!r} 非法（test/production/feedback）")
        query = query.filter(found_in=found_in)
    if escaped is not None:
        query = query.filter(escaped=escaped)
    if unconfirmed:
        query = query.filter(confirmed=False)
    if module:
        query = query.filter(module=module)

    bugs = await query.order_by("-created_at")
    if not bugs:
        return []

    # 关键字搜索（title/symptom/code/keywords）
    if keyword:
        kw = keyword.strip().lower()
        bugs = [
            b for b in bugs
            if kw in (b.title or "").lower()
            or kw in (b.symptom or "").lower()
            or kw in (b.code or "").lower()
            or kw in (b.keywords or "").lower()
        ]

    now = _now()
    if overdue:
        bugs = [
            b for b in bugs
            if _naive(b.deadline) is not None
            and b.status not in ("closed", "rejected")
            and _naive(b.deadline) < now
        ]
    if longlife:
        bugs = [
            b for b in bugs
            if _naive(b.created_at) is not None
            and b.status not in ("resolved", "verified", "closed", "rejected")
            and (now - _naive(b.created_at)).days >= BUG_LONG_LIFE_DAYS
        ]

    ids = [b.id for b in bugs]
    causes = await TestBugRootCause.filter(bug_id__in=ids)
    causes_by_bug: dict[str, list[TestBugRootCause]] = {}
    for rc in causes:
        causes_by_bug.setdefault(str(rc.bug_id), []).append(rc)
    return [_bug_document(b, causes_by_bug.get(str(b.id), [])) for b in bugs]


async def get_bug(bug_id: Any) -> dict[str, Any]:
    """读单个 bug + 全部根因行。不存在抛 :class:`DefectNotFoundError`。"""
    bid = _as_uuid(bug_id, field="bug_id")
    bug = await TestBug.get_or_none(id=bid)
    if bug is None:
        raise DefectNotFoundError(f"bug 不存在：{bug_id!r}")
    causes = await TestBugRootCause.filter(bug_id=bid).order_by("created_at")
    return _bug_document(bug, causes)


# ── 缺陷模式沉淀（design §18「沉淀回流」）───────────────────────
async def upsert_pattern(
    symptom_pattern: str,
    root_cause_kind: str,
    *,
    prevention: Optional[str] = None,
    regression_template: Optional[Mapping[str, Any]] = None,
) -> TestBugPattern:
    """沉淀/命中缺陷模式：首次建，再次命中 ``hit_count +1``。"""
    clean_pattern = (symptom_pattern or "").strip()
    if not clean_pattern:
        raise DefectValidationError("symptom_pattern 必填（症状签名）")
    if root_cause_kind not in ROOT_CAUSE_CLASSIFICATIONS:
        raise DefectValidationError(
            f"root_cause_kind={root_cause_kind!r} 不在允许集合 {list(ROOT_CAUSE_CLASSIFICATIONS)} 内"
        )

    pattern = await TestBugPattern.get_or_none(symptom_pattern=clean_pattern)
    if pattern is None:
        return await TestBugPattern.create(
            id=_uuid.uuid4(),
            symptom_pattern=clean_pattern,
            root_cause_kind=root_cause_kind,
            prevention=prevention,
            regression_template=_jsonify(dict(regression_template)) if regression_template else {},
            hit_count=1,
        )
    pattern.root_cause_kind = root_cause_kind
    if prevention is not None:
        pattern.prevention = prevention
    if regression_template is not None:
        pattern.regression_template = _jsonify(dict(regression_template))
    pattern.hit_count = (pattern.hit_count or 0) + 1
    await pattern.save(update_fields=["root_cause_kind", "prevention",
                                      "regression_template", "hit_count", "updated_at"])
    return pattern


async def list_patterns(*, root_cause_kind: Optional[str] = None) -> list[TestBugPattern]:
    """列缺陷模式，按命中次数降序。"""
    query = TestBugPattern.all()
    if root_cause_kind is not None:
        if root_cause_kind not in ROOT_CAUSE_CLASSIFICATIONS:
            raise DefectValidationError(
                f"root_cause_kind={root_cause_kind!r} 不在允许集合 {list(ROOT_CAUSE_CLASSIFICATIONS)} 内"
            )
        query = query.filter(root_cause_kind=root_cause_kind)
    return await query.order_by("-hit_count")