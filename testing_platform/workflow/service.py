"""工作流运行记录服务（design §15.9 ``mc_test_workflow_run``）。

全异步、纯 service 层（与 ``assets.service`` / ``reports.service`` 同模式）：

* :func:`start_run` —— 建运行行（``status=pending``）。
* :func:`complete_run` —— **先过契约校验**（:func:`.contract.validate_output`，
  确定性、不调 LLM），过了 → ``done``；不过 → ``failed`` + 把问题落进
  ``output.issues``。
* :func:`mark_blocked` —— 疑问阻塞下游 → ``blocked_by_question``。
* :func:`get_run` / :func:`list_runs` —— 查询。

平台**不绑定任何具体 agent**（design §6.2）：本服务只记录运行与做契约门禁，
不派发 LLM、不起 agent。
"""
from __future__ import annotations

import uuid as _uuid
from typing import Any, Optional

from tortoise.exceptions import DoesNotExist

from .contract import validate_output
from .defs import builtin_workflow_defs
from .models import (
    WORKFLOW_RUN_STATUSES,
    WORKFLOW_ROLES,
    TestWorkflowDef,
    TestWorkflowRun,
)

__all__ = [
    "WorkflowDefNotFoundError",
    "WorkflowRunNotFoundError",
    "ensure_builtin_defs",
    "start_run",
    "complete_run",
    "mark_blocked",
    "get_run",
    "list_runs",
]


class WorkflowDefNotFoundError(LookupError):
    """工作流定义不存在。"""


class WorkflowRunNotFoundError(LookupError):
    """工作流运行不存在。"""


# ── 定义（7 内置）───────────────────────────────────────────────
def def_id_for_role(role: str) -> _uuid.UUID:
    """内置定义的确定性 UUID（由 role 派生，幂等 seed 用）。"""
    return _uuid.uuid5(_uuid.NAMESPACE_URL, f"mc_test_workflow_def:{role}")


async def ensure_builtin_defs() -> list[TestWorkflowDef]:
    """把 7 条内置定义幂等落库（按 role 唯一），返回定义行（ROLES 顺序）。

    内置定义是常量（:func:`.defs.builtin_workflow_defs`）；DB 里已存在的同名
    role 行**不覆盖**（尊重用户/上层改过的定义），只补缺。
    """
    out: list[TestWorkflowDef] = []
    for spec in builtin_workflow_defs():
        role = spec["role"]
        row = await TestWorkflowDef.filter(role=role).first()
        if row is None:
            row = await TestWorkflowDef.create(
                id=def_id_for_role(role),
                role=role,
                input_schema=spec.get("input_schema") or {},
                output_schema=spec.get("output_schema") or {},
                tool_refs=spec.get("tool_refs") or [],
                contract=spec.get("contract") or {},
                nature=spec.get("nature") or "llm",
            )
        out.append(row)
    return out


async def _resolve_def(workflow_def_id: Any) -> TestWorkflowDef:
    """定义定位：UUID/str → DB 行；role 名 → 内置定义（顺手补缺）。"""
    if isinstance(workflow_def_id, str) and workflow_def_id in WORKFLOW_ROLES:
        await ensure_builtin_defs()
        workflow_def_id = def_id_for_role(workflow_def_id)
    try:
        pk = _uuid.UUID(str(workflow_def_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkflowDefNotFoundError(f"工作流定义不存在：{workflow_def_id!r}") from exc
    try:
        return await TestWorkflowDef.get(id=pk)
    except DoesNotExist as exc:
        raise WorkflowDefNotFoundError(f"工作流定义不存在：{workflow_def_id!r}") from exc


# ── 运行记录 ────────────────────────────────────────────────────
async def start_run(
    workflow_def_id: Any,
    input: Any,
    *,
    linked_run_id: Any = None,
    agent_runtime: Optional[str] = None,
) -> TestWorkflowRun:
    """开一条工作流运行（``status=pending``）。

    :param workflow_def_id: 定义主键；也接受 role 名（如 ``"coder"``）→ 内置定义。
    :param input: 角色输入（原样落 ``input`` JSONB）。
    :param linked_run_id: 可选，关联的引擎执行运行（``mc_test_run`` 主键），
        落 ``run_id`` 列（注意：函数参数名与列名不同——列名 ``run_id`` 是
        design §15.9 的「关联执行运行」，本函数的运行主键自动生成）。
    :param agent_runtime: 由哪个 runtime 驱动（部署选择，本阶段可空）。
    """
    wdef = await _resolve_def(workflow_def_id)
    return await TestWorkflowRun.create(
        workflow_def_id=wdef.id,
        run_id=linked_run_id,
        input=dict(input or {}),
        output={},
        status="pending",
        agent_runtime=agent_runtime,
    )


async def _get_run(run_id: Any) -> TestWorkflowRun:
    try:
        return await TestWorkflowRun.get(id=_uuid.UUID(str(run_id)))
    except (ValueError, AttributeError, TypeError, DoesNotExist) as exc:
        raise WorkflowRunNotFoundError(f"工作流运行不存在：{run_id!r}") from exc


async def complete_run(run_id: Any, output: Any) -> TestWorkflowRun:
    """工作流产出收口：**先过契约校验**再定状态（design §6.2 门禁哲学）。

    * 契约过（``validate_output(role, output).valid``）→ ``status=done``，
      ``output`` = 产出本体。
    * 契约不过 → ``status=failed``，``output`` =
      ``{"submitted": <原产出>, "issues": [<ValidationIssue dict>…]}``
      （问题留档，供下游/前端读）。

    角色取自运行绑定的 ``workflow_def.role``。``status`` 直接覆盖为终态——
    ``running`` 由外部 agent 置（若有），本函数是收口点。
    """
    run = await _get_run(run_id)
    wdef = await _resolve_def(run.workflow_def_id)
    result = validate_output(wdef.role, dict(output or {}), workflow_def={
        "role": wdef.role,
        "output_schema": wdef.output_schema,
        "input_schema": wdef.input_schema,
        "tool_refs": wdef.tool_refs,
        "contract": wdef.contract,
        "nature": wdef.nature,
    })
    if result.valid:
        run.status = "done"
        run.output = dict(output or {})
    else:
        run.status = "failed"
        run.output = {
            "submitted": dict(output or {}),
            "issues": [i.to_dict() for i in result.issues],
        }
    await run.save()
    return run


async def mark_blocked(run_id: Any, question_id: Any) -> TestWorkflowRun:
    """疑问阻塞下游：``status=blocked_by_question``，并把 ``question_id`` 落进 ``output.blocked_by_question``。"""
    run = await _get_run(run_id)
    run.status = "blocked_by_question"
    out = dict(run.output or {})
    out["blocked_by_question"] = str(question_id)
    run.output = out
    await run.save()
    return run


async def get_run(run_id: Any) -> TestWorkflowRun:
    """按主键取运行行；不存在抛 :class:`WorkflowRunNotFoundError`。"""
    return await _get_run(run_id)


async def fail_run(run_id: Any, error: Any) -> TestWorkflowRun:
    """运行失败收口：``status=failed`` + ``output.error``（错误**必须留档**）。

    后台 Agent（``case.generate`` / ``transport.generate_from_repo``）的异常
    不能只写日志——否则 run 永远停在 pending，前端轮询到超时也拿不到原因。
    """
    run = await _get_run(run_id)
    run.status = "failed"
    out = dict(run.output or {})
    out["error"] = str(error)[:8000]
    run.output = out
    await run.save()
    return run


async def list_runs(
    *,
    workflow_def_id: Any = None,
    status: Optional[str] = None,
    linked_run_id: Any = None,
    limit: int = 100,
) -> list[TestWorkflowRun]:
    """列运行（可按定义/状态/关联执行运行过滤，``created_at`` 倒序）。"""
    if status is not None and status not in WORKFLOW_RUN_STATUSES:
        raise ValueError(f"status={status!r} 不在可选值 {WORKFLOW_RUN_STATUSES} 内")
    qs = TestWorkflowRun.all()
    if workflow_def_id is not None:
        qs = qs.filter(workflow_def_id=_uuid.UUID(str(workflow_def_id)))
    if status is not None:
        qs = qs.filter(status=status)
    if linked_run_id is not None:
        qs = qs.filter(run_id=_uuid.UUID(str(linked_run_id)))
    return await qs.order_by("-created_at").limit(limit)
