"""任务层 API（26 号文 §1.5）—— MCP 方法注册。

与 25 号文的边界：
- **生成链**（环节层）仍走 ``gen_methods.py``（门①②、进度、确认等）。
- **任务层**新增 ``taskflow/methods.py``（创建任务 / 看方案 / 确定方案 / 执行进度 / 应用产物）。
- 两者通过 ``refs.gen_run_id`` 连接（一个执行类任务内部跑生成链时，挂上那个 run id）。

对外表面（``__all__``）
----------------------

====================================  =========================================
:func:`list_tasks`                  列表（按类型/状态过滤）
:func:`get_task`                    详情（含方案 + 进度）
:func:`create_task`                 创建（用户填维度表单）
:func:`update_plan`                 更新方案（可编辑后确认）
:func:`confirm_plan`                确定方案 → 进 executing
:func:`start_execution`             启动执行（标记 started_at）
:func:`stop_execution`              停止执行（blocked / cancelled）
:func:`apply_staged`                应用产物（翻标识）
:func:`revert_staged`               回滚产物（翻回去或删除）
====================================  =========================================

依赖纪律
--------
不 import ORM 模型（只读 params，写 SQL），与 ``staging.py`` 同一风格。
需要环节层能力（如生成用例）时由调用方注入 ``llm_call`` 或 ``gen_run_id``。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from .models import TASK_TYPES, TASK_RUN_STATUSES, can_transition

# ── 工具 ───────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: Any) -> _uuid.UUID:
    """UUID 归一；空值抛错。"""
    if isinstance(value, _uuid.UUID):
        return value
    if isinstance(value, str):
        return _uuid.UUID(str(value).strip())
    raise ValueError(f"无效 UUID：{value!r}")


def _as_optional_uuid(value: Any) -> Optional[_uuid.UUID]:
    """可选 UUID；空串→None。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _as_uuid(value)


def _next_code(conn: Any, prefix: str, project_id: Any) -> str:
    """项目内自增发号（TRUN-xxxx）。幂等：并发会话不会覆盖。

    正则取 code 尾部的数字部分取最大，+1 后补零到四位。
    ``code`` 形如 ``TRUN-0001``——正则锚死前缀，避免匹配到别的前缀。
    """
    row = conn.fetchrow(
        r"SELECT MAX(CAST(SUBSTRING(code FROM '\w+-(\d+)' AS INTEGER))) AS max_num "
        "FROM mc_test_task_run WHERE project_id = $1 AND code LIKE $2",
        str(project_id), f"{prefix}-%",
    )
    max_num = int(row["max_num"] or 0)
    num = max_num + 1
    code = f"{prefix}-{num:04d}"
    return code


async def list_tasks(
    conn: Any,
    *,
    project_id: Any,
    type: Optional[str] = None,
    status: Optional[str] = None,
    phase_status: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """任务列表（面板 tab 视图）。

    :param project_id: 项目 ID。
    :param type: 任务类型（五类之一，缺省不限）。
    :param status: 状态（九态之一，缺省不限）。
    :param phase_status: 五段之一（create/plan/confirm/execute/apply，缺省不限）。
    :param limit: 上限（默认 50）。
    :returns: 行数组（不含完整方案，详情用 get_task）。
    """
    pid = str(_as_uuid(project_id))
    where = ["project_id = $1"]
    args: list[Any] = [pid]
    if type:
        where.append("type = $%d" % len(args) + 1)
        args.append(type)
    if status:
        where.append("status = $%d" % len(args) + 1)
        args.append(status)
    if phase_status:
        where.append("phase_status = $%d" % len(args) + 1)
        args.append(phase_status)
    sql = f'SELECT * FROM mc_test_task_run WHERE {" AND ".join(where)} ORDER BY created_at DESC LIMIT $%d' % len(args) + 1
    rows = await conn.fetch(sql, *args, limit)
    return [dict(r) for r in rows]


async def get_task(conn: Any, task_id: Any) -> dict:
    """任务详情（含方案 + 进度）。

    :returns: 单行 + plan + progress（JSONB 已解析为 dict）。
    """
    tid = str(_as_uuid(task_id))
    row = await conn.fetchval(
        "SELECT to_jsonb(t) FROM mc_test_task_run t WHERE t.id = $1", tid
    )
    if not row:
        raise KeyError(f"任务不存在：{tid!r}")
    return dict(row)


async def create_task(
    conn: Any,
    *,
    project_id: Any,
    type: str,
    title: str,
    note: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    env_id: Optional[Any] = None,
    created_by: Optional[Any] = None,
) -> dict:
    """创建任务（用户填维度表单）。

    :param project_id: 项目 ID。
    :param type: 任务类型（五类之一）。
    :param title: 标题。
    :param note: 说明（创建时的自由文本）。
    :param scope: 任务类型专属维度（见 :data:`STATUS_LABELS_BY_TYPE`）。
    :param env_id: 选定的环境（裸 UUID）。
    :param created_by: 创建者 ID（用户）。
    :returns: 新建任务的行（带新发的 code）。
    """
    # 校验
    if type not in TASK_TYPES:
        raise ValueError(f"未知任务类型：{type!r}（合法：{TASK_TYPES}）")
    pid = str(_as_uuid(project_id))
    eid = str(_as_optional_uuid(env_id))
    cid = str(_as_optional_uuid(created_by)) or ""

    # 发号（幂等）
    code = await _next_code(conn, "TRUN", pid)

    now = _now().isoformat()
    new_id = _uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO mc_test_task_run (
            id, code, project_id, type, status, phase_status,
            title, note, scope, plan, resources, progress, blockers, refs,
            env_id, created_by, created_at, updated_at
        ) VALUES ($1, $2, $3, $4, 'draft', 'create', $5, $6, $7, '{}', '{}', '{}', '[]', '{}', $8, $9, $10, $10)
        """,
        new_id, code, pid, type, title, note or "",
        scope or {}, eid, cid, now,
    )
    return await get_task(conn, new_id)


async def update_plan(
    conn: Any,
    task_id: Any,
    *,
    plan: Mapping[str, Any],
    confirmed_by: Optional[Any] = None,
) -> dict:
    """更新方案（可编辑后确认）。

    :param task_id: 任务 ID。
    :param plan: 方案内容（角色分工 / 执行步骤 / 资源计划 / 依赖检查 / 目的关注点）。
    :param confirmed_by: 确认人（确定方案时传）。
    :returns: 更新后的行。
    """
    tid = str(_as_uuid(task_id))
    revision = await conn.fetchval(
        "SELECT plan_revision FROM mc_test_task_run WHERE id = $1", tid
    )
    new_rev = (revision or 0) + 1
    now = _now().isoformat()
    cby = str(_as_optional_uuid(confirmed_by))
    await conn.execute(
        """
        UPDATE mc_test_task_run SET
            plan = $1, plan_revision = $2, updated_at = $3,
            confirmed_by = $4, confirmed_at = CASE WHEN $4 IS NOT NULL THEN $3 ELSE confirmed_at END
        WHERE id = $5
        """,
        plan, new_rev, now, cby, tid,
    )
    return await get_task(conn, _uuid.UUID(tid))


async def confirm_plan(conn: Any, task_id: Any) -> dict:
    """确定方案 → 进 executing（用户点击「确定方案」按钮）。

    :returns: 更新后的行。
    """
    tid = str(_as_uuid(task_id))
    now = _now().isoformat()
    await conn.execute(
        """
        UPDATE mc_test_task_run SET
            status = 'executing', phase_status = 'execute',
            started_at = $1, updated_at = $1
        WHERE id = $2 AND status IN ('planning','planned')
        """,
        now, tid,
    )
    return await get_task(conn, _uuid.UUID(tid))


async def start_execution(conn: Any, task_id: Any) -> dict:
    """启动执行（标记 started_at，用于日志时间戳）。

    与 confirm_plan 的区别：confirm_plan 是**用户动作**（确定方案）；
    start_execution 是**系统动作**（agent 开始干活）。
    """
    tid = str(_as_uuid(task_id))
    now = _now().isoformat()
    await conn.execute(
        """
        UPDATE mc_test_task_run SET started_at = $1 WHERE id = $2
        """,
        now, tid,
    )
    return await get_task(conn, _uuid.UUID(tid))


async def stop_execution(
    conn: Any,
    task_id: Any,
    *,
    status: str = "blocked",
    reason: Optional[str] = None,
) -> dict:
    """停止执行（blocked 或 cancelled）。

    :param status: 目标状态（blocked | cancelled）。
    :param reason: 停止原因（记录到 blockers 里）。
    """
    tid = str(_as_uuid(task_id))
    now = _now().isoformat()
    blocker = {"kind": "manual", "message": reason or ""} if reason else {}
    await conn.execute(
        """
        UPDATE mc_test_task_run SET
            status = $1, phase_status = $1,
            blockers = COALESCE(blockers::jsonb, '[]') || $2,
            updated_at = $3
        WHERE id = $4 AND status = 'executing'
        """,
        status, blocker, now, tid,
    )
    return await get_task(conn, _uuid.UUID(tid))


async def apply_staged(conn: Any, task_id: Any) -> dict[str, int]:
    """应用产物（翻标识：staged → false，之后全局可见）。

    这是 26 号文 §1.2「应用方案」那一步的存储实现。
    """
    from . import staging

    tid = str(_as_uuid(task_id))
    result = await staging.apply_staged(conn, tid)
    return result


async def revert_staged(
    conn: Any,
    task_id: Any,
    *,
    action: str = "restage",
) -> dict[str, int]:
    """回滚产物（翻回去或删除）。

    :param action: :data:`APPLY_ACTIONS` 之一（restage | delete）。
    """
    from . import staging

    tid = str(_as_uuid(task_id))
    result = await staging.revert_staged(conn, tid, action=action)
    return result


async def count_staged(conn: Any, task_id: Any) -> dict[str, int]:
    """该任务产出的未应用产物计数（面板徽章数据源）。"""
    from . import staging

    tid = str(_as_uuid(task_id))
    return await staging.count_staged(conn, tid)


__all__ = [
    "TASK_TYPES",
    "TASK_RUN_STATUSES",
    "can_transition",
    "list_tasks",
    "get_task",
    "create_task",
    "update_plan",
    "confirm_plan",
    "start_execution",
    "stop_execution",
    "apply_staged",
    "revert_staged",
    "count_staged",
]
