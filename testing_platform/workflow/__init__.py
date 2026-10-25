"""工作流域（design §15.9 + §17.3）：7 角色工作流定义 + 契约校验 + 运行记录。

**关键哲学（design §6.2）**：平台**不绑定任何具体 agent**。7 个角色是**工作流
定义**（``input_schema → output_schema → tool_refs → contract``），LLM 角色由
**外部 agent 经能力面**跑；平台**只存定义 + 做契约校验**，**绝不内嵌 LLM 客户端**
（本包不 import ``agent/llm_bridge`` / ``providers``，无任何 LLM HTTP 调用）。

* :mod:`~testing_platform.workflow.models` —— Tortoise 模型（``mc_test_workflow_def``
  / ``mc_test_workflow_run``）+ 等价 DDL + ``create_tables_via_pg()``。
* :mod:`~testing_platform.workflow.defs` —— 7 条内置定义（常量数据，
  :func:`builtin_workflow_defs`）。
* :mod:`~testing_platform.workflow.contract` —— 确定性契约校验器
  （:func:`validate_output`；复用 quality 的 ``ValidationResult``）。
* :mod:`~testing_platform.workflow.service` —— 运行记录
  （``start_run`` / ``complete_run`` / ``mark_blocked`` / ``get_run`` / ``list_runs``）。

对外表面::

    from testing_platform.workflow import builtin_workflow_defs, validate_output, start_run
    defs = builtin_workflow_defs()                       # 7 角色定义
    res  = validate_output("coder", {"scenarios": [...]})  # 契约校验
    run  = await start_run("coder", {"design": {...}})     # 开运行
    run  = await complete_run(run.id, {"scenarios": [...]}) # 收口（过门才 done）
"""
from __future__ import annotations

from .contract import CUSTOM_TOOL_PREFIX, validate_output
from .defs import (
    CONFIRM_SOURCES,
    NATURES,
    ROLES,
    builtin_tool_refs,
    builtin_workflow_def,
    builtin_workflow_defs,
)
from .models import (
    DDL,
    WORKFLOW_NATURES,
    WORKFLOW_ROLES,
    WORKFLOW_RUN_STATUSES,
    TestWorkflowDef,
    TestWorkflowRun,
    create_tables_via_pg,
)
from .service import (
    WorkflowDefNotFoundError,
    WorkflowRunNotFoundError,
    complete_run,
    def_id_for_role,
    ensure_builtin_defs,
    fail_run,
    get_run,
    list_runs,
    mark_blocked,
    start_run,
)

__all__ = [
    "CONFIRM_SOURCES",
    "CUSTOM_TOOL_PREFIX",
    "DDL",
    "NATURES",
    "ROLES",
    "WORKFLOW_NATURES",
    "WORKFLOW_ROLES",
    "WORKFLOW_RUN_STATUSES",
    "TestWorkflowDef",
    "TestWorkflowRun",
    "WorkflowDefNotFoundError",
    "WorkflowRunNotFoundError",
    "builtin_tool_refs",
    "builtin_workflow_def",
    "builtin_workflow_defs",
    "complete_run",
    "create_tables_via_pg",
    "def_id_for_role",
    "ensure_builtin_defs",
    "fail_run",
    "get_run",
    "list_runs",
    "mark_blocked",
    "start_run",
    "validate_output",
]
