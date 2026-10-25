"""任务域 CRUD + 标签选例 + 启动（``02-task-model.md`` + design §15.10）。

全异步、纯 service 层函数（无类状态），供 executor/工作流与页面共用：

* **CRUD** —— :func:`create_task` / :func:`update_task` / :func:`enable_task`
  / :func:`disable_task`；``update_task`` 走变更门（必填 ``change_reason``，
  复用 assets 的 ``record_change`` 落 ``mc_test_change_record``，02 §7 注）。
* **标签选例** —— :func:`select_by_tags`（02 §3）：active 用例 ∩
  ``scenario_tags``（all 全含 / any 含其一 / none 都不含）∩ ``case_filter``
  （module/priority/automation_status）→ 经 case↔scenario 关联取场景集合
  **去重**（执行单位是场景，同场景多用例只跑一次）。
* **启动** —— :func:`run_task`（02 §8）：选例 → 建 :class:`TestRun`
  （status=pending，``scenario_ids``/``global_params`` 为本次快照）→ 返回
  run_id。**不真执行引擎**——执行由调用方接 engine（``run.execute``）异步推进。
* **定时解析** —— :func:`next_fire`（:mod:`.scheduler`，纯函数不挂 APScheduler）。

code 自增 ``TASK-0001`` / ``RUN-0001``，按 ``project_id`` 内 max+1
（与 assets ``_next_code`` 同款，字典序陷阱同避）。
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any, Mapping, Optional, Sequence

from tortoise.exceptions import DoesNotExist

from testing_platform.assets.models import (
    TestCase,
    TestCaseScenario,
    TestCaseTag,
    TestTag,
)
from testing_platform.assets.service import (
    AssetNotFoundError,
    AssetValidationError,
    ChangeReasonRequiredError,
    _jsonify,
    _next_code,
    _validate_enum,
    record_change,
)

from .models import (
    NOTIFY_CHANNELS,
    NOTIFY_ON,
    RUN_TRIGGERS,
    SCHEDULE_KINDS,
    TASK_EXEC_MODES,
    TASK_EXEC_TYPES,
    TASK_STATUSES,
    TestRun,
    TestRunScenario,
    TestStepResult,
    TestTask,
)
from .scheduler import next_fire

TASK_CODE_PREFIX = "TASK-"
RUN_CODE_PREFIX = "RUN-"

#: ``scenario_tags`` 合法键（02 §3）
TAG_SET_KEYS: tuple[str, ...] = ("all", "any", "none")

#: ``case_filter`` 合法键（02 §3：模块/优先级/状态）
CASE_FILTER_KEYS: tuple[str, ...] = ("module", "priority", "automation_status", "status")


class TaskNotFoundError(AssetNotFoundError):
    """任务/运行不存在（继承 assets 的 LookupError，能力面统一翻译）。"""


# ── 小工具（与 assets.service 同款）─────────────────────────────
def _as_uuid(value: Any, *, field: str) -> _uuid.UUID:
    if isinstance(value, _uuid.UUID):
        return value
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AssetValidationError(f"{field} 不是合法 UUID：{value!r}") from exc


def _maybe_uuid(value: Any, *, field: str) -> Optional[_uuid.UUID]:
    if value is None or value == "":
        return None
    return _as_uuid(value, field=field)


def _string_list(value: Any, *, field: str) -> list[str]:
    """JSONB 里的字符串列表：标量归一为单元素列表；非法即报错。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise AssetValidationError(f"{field} 必须是字符串列表：{value!r}")
            out.append(item)
        return out
    raise AssetValidationError(f"{field} 必须是字符串列表：{value!r}")


def _validate_schedule(schedule: Any, exec_type: str) -> Optional[dict]:
    """``exec_type=scheduled`` 时 ``schedule`` 必给且 kind 合法（02 §4）。"""
    if exec_type != "scheduled":
        return _jsonify(schedule) if schedule else None
    if not schedule or not isinstance(schedule, Mapping):
        raise AssetValidationError("exec_type=scheduled 需要 schedule 配置")
    kind = schedule.get("kind")
    if kind not in SCHEDULE_KINDS:
        raise AssetValidationError(
            f"schedule.kind={kind!r} 不在允许集合 {list(SCHEDULE_KINDS)} 内"
        )
    # 解析一遍：坏表达式在保存时就报，不等触发（next_fire 为纯函数无副作用）
    try:
        next_fire(schedule)
    except ValueError as exc:
        raise AssetValidationError(f"schedule 配置非法：{exc}") from exc
    return _jsonify(dict(schedule))


def _validate_notify(notify: Any) -> dict:
    """notify 声明校验（02 §5）：只存不发；on/channels 键在既有通道集合内。

    通道类型限平台既有能力（design §14.3 已核对：notify_dispatch 的
    钉钉/飞书/企微/webhook + notify_push 的 Expo 移动推送，**无 email**）——
    任务只声明"发给谁、什么时候发、发什么"，发送由集成层执行。
    """
    if not notify:
        return {}
    if not isinstance(notify, Mapping):
        raise AssetValidationError(f"notify 必须是对象：{notify!r}")
    clean = dict(notify)
    on = clean.get("on")
    if on is not None:
        on_list = _string_list(on, field="notify.on")
        unknown = [x for x in on_list if x not in NOTIFY_ON]
        if unknown:
            raise AssetValidationError(
                f"notify.on={unknown} 不在允许集合 {list(NOTIFY_ON)} 内"
            )
        clean["on"] = on_list
    channels = clean.get("channels")
    if channels is not None:
        if not isinstance(channels, (list, tuple)):
            raise AssetValidationError("notify.channels 必须是列表")
        for index, channel in enumerate(channels):
            if not isinstance(channel, Mapping) or not channel.get("type"):
                raise AssetValidationError(f"notify.channels[{index}] 缺少 type")
            if channel["type"] not in NOTIFY_CHANNELS:
                raise AssetValidationError(
                    f"notify.channels[{index}].type={channel['type']!r} 不在平台既有通道 "
                    f"{list(NOTIFY_CHANNELS)} 内（design §14.3：无 email，勿加）"
                )
        clean["channels"] = _jsonify(list(channels))
    clean["include"] = _string_list(clean.get("include"), field="notify.include")
    return clean


# ── 任务 CRUD ───────────────────────────────────────────────────
async def create_task(
    *,
    project_id: Any,
    title: str,
    created_by: Any,
    purpose: Optional[str] = None,
    scenario_tags: Optional[Mapping[str, Any]] = None,
    case_filter: Optional[Mapping[str, Any]] = None,
    exec_type: str = "manual",
    schedule: Optional[Mapping[str, Any]] = None,
    env_id: Optional[Any] = None,
    global_params: Optional[Mapping[str, Any]] = None,
    notify: Optional[Mapping[str, Any]] = None,
    exec_mode: str = "script_only",
    status: str = "draft",
    code: Optional[str] = None,
) -> TestTask:
    """建任务（02 §2）。``code`` 自动 ``TASK-xxxx``（按 project 自增）。"""
    if not title:
        raise AssetValidationError("任务 title 必填")
    exec_type = _validate_enum(exec_type, TASK_EXEC_TYPES, field="exec_type", required=True)
    exec_mode = _validate_enum(exec_mode, TASK_EXEC_MODES, field="exec_mode", required=False) or "script_only"
    status = _validate_enum(status, TASK_STATUSES, field="status", required=False) or "draft"
    return await TestTask.create(
        project_id=_as_uuid(project_id, field="project_id"),
        code=code or await _next_code(TestTask, project_id, TASK_CODE_PREFIX),
        title=title,
        purpose=purpose,
        scenario_tags=_normalize_scenario_tags(scenario_tags),
        case_filter=dict(case_filter or {}),
        exec_type=exec_type,
        schedule=_validate_schedule(schedule, exec_type),
        env_id=_maybe_uuid(env_id, field="env_id"),
        global_params=dict(global_params or {}),
        notify=_validate_notify(notify),
        exec_mode=exec_mode,
        status=status,
        created_by=_as_uuid(created_by, field="created_by"),
    )


def _normalize_scenario_tags(scenario_tags: Any) -> dict[str, list[str]]:
    """``scenario_tags`` 归一（02 §3）→ {all/any/none: [str]}。

    接受两种写法：显式组合 ``{all: [...], any: [...], none: [...]}``，或
    裸标签列表 ``["smoke", "payment"]``（等价于 all）。未知键拒绝。
    """
    if not scenario_tags:
        return {}
    if isinstance(scenario_tags, (list, tuple, str)):
        return {"all": _string_list(scenario_tags, field="scenario_tags")}
    if not isinstance(scenario_tags, Mapping):
        raise AssetValidationError(f"scenario_tags 必须是对象或列表：{scenario_tags!r}")
    unknown = [key for key in scenario_tags if key not in TAG_SET_KEYS]
    if unknown:
        raise AssetValidationError(
            f"scenario_tags 不允许键：{unknown}（可用：{list(TAG_SET_KEYS)}）"
        )
    return {
        key: _string_list(scenario_tags[key], field=f"scenario_tags.{key}")
        for key in TAG_SET_KEYS
        if scenario_tags.get(key)
    }


async def get_task(task_id: Any) -> TestTask:
    try:
        return await TestTask.get(id=_as_uuid(task_id, field="task_id"))
    except DoesNotExist:
        raise TaskNotFoundError(f"任务不存在：{task_id!r}") from None


async def list_tasks(project_id: Any, *, status: Optional[str] = None) -> list[TestTask]:
    qs = TestTask.filter(project_id=_as_uuid(project_id, field="project_id"))
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("code")


TASK_EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "purpose",
    "scenario_tags",
    "case_filter",
    "exec_type",
    "schedule",
    "env_id",
    "global_params",
    "notify",
    "exec_mode",
    "status",
)


async def update_task(
    task_id: Any,
    *,
    change_reason: Optional[str] = None,
    propagation: Optional[Mapping[str, Any]] = None,
    rollback_ref: Optional[Any] = None,
    **values: Any,
) -> TestTask:
    """改任务（02 §7 ``task.update``）——走变更门（design §15.12）。

    ``change_reason`` 必填；写 ``mc_test_change_record``（entity_type=task）
    并 ``version+1``。
    """
    if not change_reason or not str(change_reason).strip():
        raise ChangeReasonRequiredError(
            "task.update 必须提供 change_reason（design §15.12 变更门）"
        )
    task = await get_task(task_id)
    unknown = [key for key in values if key not in TASK_EDITABLE_FIELDS]
    if unknown:
        raise AssetValidationError(
            f"task 不允许修改字段：{unknown}（可改：{TASK_EDITABLE_FIELDS}）"
        )
    if not values:
        raise AssetValidationError("task update 未提供任何字段")

    # exec_type 可能在同一次 update 里被改：schedule 校验须用**生效后**的 exec_type
    effective_exec_type = values.get("exec_type", task.exec_type)
    if "exec_type" in values:
        effective_exec_type = _validate_enum(
            effective_exec_type, TASK_EXEC_TYPES, field="exec_type", required=True
        )

    field_changes: dict[str, dict[str, Any]] = {}
    for key, new_value in values.items():
        if key == "exec_type":
            new_value = effective_exec_type
        elif key == "exec_mode":
            new_value = _validate_enum(new_value, TASK_EXEC_MODES, field=key, required=True)
        elif key == "status":
            new_value = _validate_enum(new_value, TASK_STATUSES, field=key, required=True)
        elif key == "scenario_tags":
            new_value = _normalize_scenario_tags(new_value)
        elif key == "case_filter":
            bad = [k for k in (new_value or {}) if k not in CASE_FILTER_KEYS]
            if bad:
                raise AssetValidationError(f"case_filter 不允许键：{bad}（可用：{CASE_FILTER_KEYS}）")
            new_value = dict(new_value or {})
        elif key in {"global_params", "notify"}:
            new_value = _validate_notify(new_value) if key == "notify" else dict(new_value or {})
        elif key == "schedule":
            new_value = _validate_schedule(new_value, effective_exec_type)
        elif key == "env_id":
            new_value = _maybe_uuid(new_value, field=key)

        old_value = getattr(task, key)
        if old_value != new_value:
            field_changes[key] = {"from": _jsonify(old_value), "to": _jsonify(new_value)}
        setattr(task, key, new_value)

    # 生效态一致性：改成 scheduled 必须带得住 schedule（02 §4）
    if task.exec_type == "scheduled" and not task.schedule:
        raise AssetValidationError("exec_type=scheduled 需要 schedule 配置")

    task.version = int(task.version or 1) + 1
    await task.save()

    await record_change(
        "task",
        task.id,
        change_reason=str(change_reason).strip(),
        field_changes=field_changes,
        propagation=propagation,
        rollback_ref=rollback_ref,
    )
    return task


async def enable_task(task_id: Any, *, change_reason: Optional[str] = None) -> TestTask:
    """启用任务（draft/disabled → enabled）。"""
    return await update_task(task_id, change_reason=change_reason or "启用任务", status="enabled")


async def disable_task(task_id: Any, *, change_reason: Optional[str] = None) -> TestTask:
    """停用任务（enabled → disabled；定时/事件触发不再启动）。"""
    return await update_task(task_id, change_reason=change_reason or "停用任务", status="disabled")


# ── 标签选例（02 §3）────────────────────────────────────────────
def _case_matches_scenario_tags(case_tag_names: set[str], scenario_tags: Mapping[str, Sequence[str]]) -> bool:
    """all 全含 / any 含其一 / none 都不含（02 §3）。"""
    all_tags = scenario_tags.get("all") or ()
    if all_tags and not set(all_tags) <= case_tag_names:
        return False
    any_tags = scenario_tags.get("any") or ()
    if any_tags and not case_tag_names & set(any_tags):
        return False
    none_tags = scenario_tags.get("none") or ()
    if none_tags and case_tag_names & set(none_tags):
        return False
    return True


def _case_matches_filter(case: TestCase, module_tags: set[str], case_filter: Mapping[str, Any]) -> bool:
    """``case_filter``（02 §3）：module 走 **module 类标签**，priority/状态走列。

    ``automation_status`` 在此处是**附加收窄**；02 §3 的"∩ 已 automated"是
    候选集**硬门槛**（见 :func:`select_by_tags` 的候选查询），二者可叠加。
    """
    module = case_filter.get("module") or ()
    if module and not set(module) & module_tags:
        return False
    priority = case_filter.get("priority") or ()
    if priority and case.priority not in set(priority):
        return False
    automation_status = case_filter.get("automation_status") or ()
    if automation_status and case.automation_status not in set(automation_status):
        return False
    status = case_filter.get("status") or ()
    if status and case.status not in set(status):
        return False
    return True


async def select_by_tags(task: TestTask) -> list[dict[str, Any]]:
    """按标签选例（02 §3）→ **场景集合去重**。

    算法::

        candidates = 所有 active 用例
          ∩ 用例标签满足 scenario_tags（all 全含 / any 含其一 / none 都不含）
          ∩ 用例满足 case_filter（若给）
        → 经 case↔scenario 关联取场景集合（去重：执行单位是场景）

    返回场景 id 列表（str，去重保序）。一个 case 都没选中的任务返回空列表
    （调 :func:`run_task` 启动时空列表照样建 run——先见数后接引擎）。
    """
    scenario_tags = _normalize_scenario_tags(task.scenario_tags)
    case_filter = dict(task.case_filter or {})

    # 候选 = active 用例 ∩ 已 automated（02 §3 算法第 4 行：硬门槛，默认即只跑自动化的）
    candidates = await TestCase.filter(
        project_id=task.project_id,
        status="active",
        automation_status="automated",
    )
    if not candidates:
        return []

    # 用例标签一次取齐（N+1 免了：两张小表各自 in 查询）。
    # ``tag_by_id`` 拍出 id→(name, category)，供 compose 与 case_filter 分流。
    case_ids = [case.id for case in candidates]
    tag_links = await TestCaseTag.filter(case_id__in=case_ids)
    tag_ids = {link.tag_id for link in tag_links}
    tags_by_id: dict[Any, tuple[str, str]] = {}
    if tag_ids:
        all_tags = await TestTag.filter(id__in=list(tag_ids))
        tags_by_id = {tag.id: (tag.name, tag.category) for tag in all_tags}

    names_by_case: dict[Any, set[str]] = {case_id: set() for case_id in case_ids}
    module_by_case: dict[Any, set[str]] = {case_id: set() for case_id in case_ids}
    for link in tag_links:
        pair = tags_by_id.get(link.tag_id)
        if not pair:
            continue
        name, category = pair
        names_by_case[link.case_id].add(name)
        if category == "module":
            module_by_case[link.case_id].add(name)

    selected_cases = [
        case
        for case in candidates
        if _case_matches_scenario_tags(names_by_case[case.id], scenario_tags)
        and _case_matches_filter(case, module_by_case[case.id], case_filter)
    ]
    if not selected_cases:
        return []

    # case → scenario 关联取场景集合（02 §3 注：执行单位是场景，去重）
    scenario_links = await TestCaseScenario.filter(
        case_id__in=[case.id for case in selected_cases]
    ).order_by("case_id", "order", "id")

    seen: set[Any] = set()
    ordered: list[str] = []
    for link in scenario_links:
        if link.scenario_id not in seen:
            seen.add(link.scenario_id)
            ordered.append(str(link.scenario_id))
    return ordered


# ── 任务 → 运行（02 §8）────────────────────────────────────────
async def run_task(
    task_id: Any,
    *,
    trigger: str = "manual",
    env_id: Optional[Any] = None,
    global_params_override: Optional[Mapping[str, Any]] = None,
    scenario_ids_override: Optional[Sequence[Any]] = None,
) -> TestRun:
    """启动一次运行（02 §8）——**只建 run 行，不执行引擎**。

    选例（:func:`select_by_tags`）→ 建 :class:`TestRun`
    （status=pending，``scenario_ids`` / ``global_params`` 为**本次快照**）→
    返回 run 行。执行由调用方异步接 engine（逐场景跑、写 run_scenario、
    finalize summary、发通知、生成报告）。

    :param trigger: 触发方式（04 §1.1 manual/scheduled/event/api）。
    :param env_id: 覆盖任务环境（缺省用任务 ``env_id``）。
    :param global_params_override: 叠加全局参数（优先于任务配置）。
    :param scenario_ids_override: 跳过选例直接指定场景（事件触发增量用，v1 预留）。
    """
    task = await get_task(task_id)
    trigger = _validate_enum(trigger, RUN_TRIGGERS, field="trigger", required=True)

    if scenario_ids_override is not None:
        scenario_ids = [str(sid) for sid in scenario_ids_override]
    else:
        scenario_ids = await select_by_tags(task)

    params = dict(task.global_params or {})
    if global_params_override:
        params.update(dict(global_params_override))

    run = await TestRun.create(
        project_id=task.project_id,
        code=await _next_code(TestRun, task.project_id, RUN_CODE_PREFIX),
        task_id=task.id,
        trigger=trigger,
        env_id=_maybe_uuid(env_id, field="env_id") or task.env_id,
        global_params=_jsonify(params),
        scenario_ids=scenario_ids,
        status="pending",
    )
    return run


async def get_run(run_id: Any) -> TestRun:
    try:
        return await TestRun.get(id=_as_uuid(run_id, field="run_id"))
    except DoesNotExist:
        raise TaskNotFoundError(f"运行不存在：{run_id!r}") from None


async def list_runs(project_id: Any, *, task_id: Optional[Any] = None, status: Optional[str] = None) -> list[TestRun]:
    """按项目列运行（可按任务/状态收窄）。"""
    qs = TestRun.filter(project_id=_as_uuid(project_id, field="project_id"))
    if task_id is not None:
        qs = qs.filter(task_id=_as_uuid(task_id, field="task_id"))
    if status:
        qs = qs.filter(status=status)
    return await qs.order_by("-created_at")


# ── 运行层级查询（04 §1.2/§1.3：run → run_scenario → step_result）──
async def list_run_scenarios(run_id: Any) -> list[TestRunScenario]:
    """按 run 列场景级结果（04 §1.2），``started_at`` 倒序（最新在前）。

    执行记录详情页 run → run_scenario 层级取数用。``run_id`` 不存在抛
    :class:`TaskNotFoundError`（与 :func:`get_run` 一致，能力面归 not_found）。
    """
    run = await get_run(run_id)
    return await TestRunScenario.filter(run_id=run.id).order_by("-started_at")


async def list_step_results(run_scenario_id: Any) -> list[TestStepResult]:
    """按 run_scenario 列步骤级结果（04 §1.3），``seq`` 升序。

    ``seq`` 是 TEXT 列（引擎写 ``str(seq)``），字符串序会把 "10" 排在 "2"
    前，故按整型值排序（非整型/None 排最后，与 reports.service 同款）。
    """
    rows = await TestStepResult.filter(
        run_scenario_id=_as_uuid(run_scenario_id, field="run_scenario_id")
    )
    rows.sort(key=_step_seq_sort_key)
    return rows


def _step_seq_sort_key(row: TestStepResult) -> tuple[int, int]:
    """seq 排序键：可解析整型的排前（按数值），其余排最后。"""
    try:
        return (0, int(str(row.seq)))
    except (TypeError, ValueError):
        return (1, 0)


__all__ = [
    "CASE_FILTER_KEYS",
    "RUN_CODE_PREFIX",
    "RUN_TRIGGERS",
    "TAG_SET_KEYS",
    "TASK_CODE_PREFIX",
    "TASK_EDITABLE_FIELDS",
    "TASK_EXEC_MODES",
    "TASK_EXEC_TYPES",
    "TASK_STATUSES",
    "TaskNotFoundError",
    "create_task",
    "disable_task",
    "enable_task",
    "get_run",
    "get_task",
    "list_run_scenarios",
    "list_runs",
    "list_step_results",
    "list_tasks",
    "next_fire",
    "run_task",
    "select_by_tags",
    "update_task",
]
