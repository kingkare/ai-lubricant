"""任务层 + 评论域的 MCP 方法注册（26 号文）。

**与 ``gen_methods.py`` 的关系**：生成链（环节层）的方法在 ``gen_methods.py``；
本文件是**任务层**（创建/方案/执行/应用）与**评论域**的方法。两者互不覆盖。

注册的方法
----------

任务层（``task.*``）::

    task.list / task.get / task.create / task.plan.update / task.plan.confirm
    task.progress / task.transition / task.outputs.apply / task.outputs.revert

评论域（``comment.*``）::

    comment.list / comment.create / comment.reply / comment.resolve
    comment.react / comment.unreact

纪律
----
- **不 import 引擎/agent 运行时**（``agent_runtime``）——执行是调用方的事，
  本层只管状态与产物。
- 错误一律抛 :class:`MethodError`（带 ``code``），由 surface 层统一信封化。
- ``task.create`` 的 ``scope`` 形状随 ``type`` 变（26 号文 §1.6）——
  **不硬编码字段**，只透传给 service 层。
"""
from __future__ import annotations

from typing import Any, Mapping

from ..taskflow import service as _tf
from ..taskflow.models import TASK_TYPES
from .methods import MethodError, _uuid_of, register_method

# ── 任务层 ─────────────────────────────────────────────────────────


@register_method(
    "taskrun.list",
    kind="read",
    description="列任务（26 号文任务层）：按项目 + 类型/状态过滤。面板 tab 视图的数据源。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID"},
            "type": {"type": "string", "enum": list(TASK_TYPES)},
            "status": {"type": "string"},
            "limit": {"type": "integer", "description": "默认 50"},
        },
        "required": ["project_id"],
    },
    returns_schema={"type": "object", "properties": {"tasks": {"type": "array"}, "count": {"type": "integer"}}},
)
async def task_list(params: Mapping[str, Any]) -> dict:
    """列任务。"""
    rows = await _tf.list_tasks(
        params["project_id"],
        type=params.get("type"),
        status=params.get("status"),
        limit=int(params.get("limit") or 50),
    )
    return {"tasks": rows, "count": len(rows)}


@register_method(
    "taskrun.get",
    kind="read",
    description="任务详情（26 号文任务层）：含方案 plan / 进度 progress / 阻塞 blockers。",
    params_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "任务 UUID"}},
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def task_get(params: Mapping[str, Any]) -> dict:
    """任务详情。"""
    try:
        return await _tf.get_task(_uuid_of(params["task_id"], field_name="task_id"))
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.create",
    kind="write",
    description="创建任务（26 号文任务层，五段生命周期起点）。``type`` 决定 ``scope`` 形状："
                "explore=目的/方向/客户端/被探索配置 · gen_case=端/模块/功能 · "
                "gen_scenario=端/用例 · req_analysis=需求 · execution=范围/方案。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "type": {"type": "string", "enum": list(TASK_TYPES)},
            "title": {"type": "string"},
            "note": {"type": "string", "description": "说明"},
            "scope": {"type": "object", "description": "任务类型专属维度"},
            "env_id": {"type": "string", "description": "选定环境 UUID（可空，规划阶段可由 agent 搜）"},
            "resources": {"type": "object", "description": "{agent_count, tokens_limit}（成本可控）"},
            "refs": {"type": "object", "description": "{gen_run_id?} —— 环节层连接点"},
            "created_by": {"type": "string"},
        },
        "required": ["project_id", "type", "title"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "code": {"type": "string"}}},
)
async def task_create(params: Mapping[str, Any]) -> dict:
    """创建任务（draft 态）。"""
    try:
        return await _tf.create_task(
            project_id=params["project_id"],
            type=str(params["type"]),
            title=str(params["title"]),
            note=params.get("note"),
            scope=params.get("scope") or {},
            env_id=params.get("env_id"),
            resources=params.get("resources") or {},
            refs=params.get("refs") or {},
            created_by=params.get("created_by"),
        )
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.plan.update",
    kind="write",
    description="写/改方案（26 号文任务层「生成方案」阶段）：角色分工/执行步骤/资源计划/"
                "依赖检查/目的关注点。可反复改；``status_to='planned'`` 时同时推进到待确定。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "plan": {"type": "object"},
            "status_to": {"type": "string", "description": "可选：planning / planned"},
            "blockers": {"type": "array", "items": {"type": "object"}},
            "resources": {"type": "object"},
        },
        "required": ["task_id", "plan"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "plan_revision": {"type": "integer"}}},
)
async def task_plan_update(params: Mapping[str, Any]) -> dict:
    """写/改方案。"""
    try:
        return await _tf.update_plan(
            _uuid_of(params["task_id"], field_name="task_id"),
            plan=params["plan"],
            status_to=params.get("status_to"),
            blockers=params.get("blockers"),
            resources=params.get("resources"),
        )
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.plan.confirm",
    kind="write",
    description="确定方案（26 号文任务层「确定方案」阶段）：planned → executing，"
                "记确认人/时间。**方案未经确定不执行**。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "confirmed_by": {"type": "string"},
        },
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def task_plan_confirm(params: Mapping[str, Any]) -> dict:
    """确定方案 → 执行。"""
    try:
        return await _tf.confirm_plan(
            _uuid_of(params["task_id"], field_name="task_id"),
            confirmed_by=params.get("confirmed_by") or params["task_id"],
        )
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.progress",
    kind="write",
    description="更新执行进度（26 号文任务层「执行方案」阶段）：面板唯一渲染源——"
                "每个 agent 当前在做什么。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "progress": {"type": "object", "description": "{steps:[{role,state,detail,since}], ...}"},
        },
        "required": ["task_id", "progress"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def task_progress(params: Mapping[str, Any]) -> dict:
    """更新进度。"""
    try:
        return await _tf.set_progress(
            _uuid_of(params["task_id"], field_name="task_id"),
            progress=params["progress"],
        )
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.transition",
    kind="write",
    description="任务状态流转（26 号文）：到 'blocked' = **缺依赖等人**（凭据/环境/客户端"
                "不具备时停下，理由 4）；'completed'/'failed'/'cancelled' 收口。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "to": {
                "type": "string",
                "enum": ["planning", "planned", "executing", "blocked", "applying",
                         "completed", "failed", "cancelled"],
            },
            "reason": {"type": "string"},
            "blockers": {"type": "array", "items": {"type": "object"},
                         "description": "[{kind,name,why,how_to_fix}] —— 缺什么、为什么、怎么补"},
        },
        "required": ["task_id", "to"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def task_transition(params: Mapping[str, Any]) -> dict:
    """状态流转。"""
    try:
        return await _tf.transition(
            _uuid_of(params["task_id"], field_name="task_id"),
            to=str(params["to"]),
            reason=params.get("reason"),
            blockers=params.get("blockers"),
        )
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.outputs.apply",
    kind="write",
    description="应用方案产物（26 号文 §1.8 D-6）：把该任务产出的 staged 资产**翻正**"
                "（staged→false，之后全局可见）。**不是搬表，是翻标识**。",
    params_schema={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"applied": {"type": "object"}}},
)
async def task_outputs_apply(params: Mapping[str, Any]) -> dict:
    """应用产物。"""
    try:
        result = await _tf.apply_task_outputs(
            _uuid_of(params["task_id"], field_name="task_id")
        )
        return {"applied": result}
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "taskrun.outputs.revert",
    kind="write",
    description="回滚产物（26 号文 §1.8）：action='restage' 翻回未应用（可逆，默认）；"
                "action='delete' 物理删除该任务**未应用**的产物（不可逆；已应用的资产不动）。",
    params_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["restage", "delete"]},
        },
        "required": ["task_id"],
    },
    returns_schema={"type": "object", "properties": {"reverted": {"type": "object"}}},
)
async def task_outputs_revert(params: Mapping[str, Any]) -> dict:
    """回滚产物。"""
    try:
        result = await _tf.revert_task_outputs(
            _uuid_of(params["task_id"], field_name="task_id"),
            action=str(params.get("action") or "restage"),
        )
        return {"reverted": result}
    except _tf.TaskflowError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


# ── 评论域 ────────────────────────────────────────────────────────


@register_method(
    "comment.list",
    kind="read",
    description="列某实体（需求/测试点）下的评论（26 号文 §2.3）：含回复、表情、结论状态。"
                "历史全量留存。",
    params_schema={
        "type": "object",
        "properties": {
            "entity_type": {"type": "string", "enum": ["requirement", "testpoint"]},
            "entity_id": {"type": "string"},
            "entity_ref": {"type": "string", "description": "可选：只取挂在某个具体条目下的"},
            "limit": {"type": "integer", "description": "默认 100"},
        },
        "required": ["entity_type", "entity_id"],
    },
    returns_schema={"type": "object", "properties": {"comments": {"type": "array"}, "count": {"type": "integer"}}},
)
async def comment_list(params: Mapping[str, Any]) -> dict:
    """列评论。"""
    from ..comments import service as _cs

    try:
        rows = await _cs.list_comments(
            entity_type=str(params["entity_type"]),
            entity_id=_uuid_of(params["entity_id"], field_name="entity_id"),
            entity_ref=params.get("entity_ref"),
            limit=int(params.get("limit") or 100),
        )
        return {"comments": rows, "count": len(rows)}
    except _cs.CommentError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "comment.create",
    kind="write",
    description="发评论（26 号文 §2.3）：多人讨论、最终得出结论、历史留痕。",
    params_schema={
        "type": "object",
        "properties": {
            "entity_type": {"type": "string", "enum": ["requirement", "testpoint"]},
            "entity_id": {"type": "string"},
            "entity_ref": {"type": "string", "description": "可选：挂到实体上的某个具体条目"},
            "content": {"type": "string"},
            "created_by": {"type": "string"},
        },
        "required": ["entity_type", "entity_id", "content"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def comment_create(params: Mapping[str, Any]) -> dict:
    """发评论。"""
    from ..comments import service as _cs

    try:
        return await _cs.create_comment(
            entity_type=str(params["entity_type"]),
            entity_id=_uuid_of(params["entity_id"], field_name="entity_id"),
            entity_ref=params.get("entity_ref"),
            content=str(params["content"]),
            created_by=params.get("created_by"),
        )
    except _cs.CommentError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "comment.reply",
    kind="write",
    description="回复某条评论（26 号文 §2.3）：一层嵌套。",
    params_schema={
        "type": "object",
        "properties": {
            "parent_id": {"type": "string"},
            "content": {"type": "string"},
            "created_by": {"type": "string"},
        },
        "required": ["parent_id", "content"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}}},
)
async def comment_reply(params: Mapping[str, Any]) -> dict:
    """回复评论。"""
    from ..comments import service as _cs

    try:
        return await _cs.reply_comment(
            parent_id=_uuid_of(params["parent_id"], field_name="parent_id"),
            content=str(params["content"]),
            created_by=params.get("created_by"),
        )
    except _cs.CommentError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "comment.resolve",
    kind="write",
    description="收敛评论（26 号文 §2.3）：定结论（conclusion/agreed/rejected/need_work/"
                "duplicate）+ 可选补充说明。**历史保留**。",
    params_schema={
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "resolution": {"type": "string", "enum": ["conclusion", "agreed", "rejected",
                                                     "need_work", "duplicate"]},
            "resolution_detail": {"type": "object"},
            "resolved_by": {"type": "string"},
        },
        "required": ["comment_id", "resolution"],
    },
    returns_schema={"type": "object", "properties": {"id": {"type": "string"}, "status": {"type": "string"}}},
)
async def comment_resolve(params: Mapping[str, Any]) -> dict:
    """收敛评论。"""
    from ..comments import service as _cs

    try:
        return await _cs.resolve_comment(
            comment_id=_uuid_of(params["comment_id"], field_name="comment_id"),
            resolution=str(params["resolution"]),
            resolution_detail=params.get("resolution_detail") or {},
            resolved_by=params.get("resolved_by"),
        )
    except _cs.CommentError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "comment.react",
    kind="write",
    description="给评论加表情（ack/agree/disagree/question）。同一人同种表情幂等。",
    params_schema={
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["ack", "agree", "disagree", "question"]},
            "created_by": {"type": "string"},
        },
        "required": ["comment_id", "kind"],
    },
    returns_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
)
async def comment_react(params: Mapping[str, Any]) -> dict:
    """加表情。"""
    from ..comments import service as _cs

    try:
        await _cs.react(
            comment_id=_uuid_of(params["comment_id"], field_name="comment_id"),
            kind=str(params["kind"]),
            created_by=params.get("created_by"),
        )
        return {"ok": True}
    except _cs.CommentError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


@register_method(
    "comment.unreact",
    kind="write",
    description="取消表情（再点一次 = 取消）。",
    params_schema={
        "type": "object",
        "properties": {
            "comment_id": {"type": "string"},
            "kind": {"type": "string"},
            "created_by": {"type": "string"},
        },
        "required": ["comment_id", "kind"],
    },
    returns_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
)
async def comment_unreact(params: Mapping[str, Any]) -> dict:
    """取消表情。"""
    from ..comments import service as _cs

    try:
        await _cs.unreact(
            comment_id=_uuid_of(params["comment_id"], field_name="comment_id"),
            kind=str(params["kind"]),
            created_by=params.get("created_by"),
        )
        return {"ok": True}
    except _cs.CommentError as exc:
        raise MethodError(exc.message, code=exc.code) from exc


__all__ = [
    "task_list", "task_get", "task_create", "task_plan_update", "task_plan_confirm",
    "task_progress", "task_transition", "task_outputs_apply", "task_outputs_revert",
    "comment_list", "comment_create", "comment_reply", "comment_resolve",
    "comment_react", "comment_unreact",
]
