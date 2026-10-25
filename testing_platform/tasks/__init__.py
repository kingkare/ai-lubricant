"""任务域（``02-task-model.md``）：任务的落库、标签选例与启动。

本包是 AI 测试平台**任务侧**的一等事实源（design §15.10）：

* :mod:`~testing_platform.tasks.models` —— Tortoise 模型 + 等价 DDL：
  ``mc_test_task``（02 §2）与运行四表 ``mc_test_run`` /
  ``mc_test_run_scenario`` / ``mc_test_step_result`` / ``mc_test_evidence``
  （design §15.7 / 04 §1.1–§1.4；后三者与引擎 ``PgResultWriter`` 同列）。
* :mod:`~testing_platform.tasks.service` —— 异步 CRUD + 标签选例
  （02 §3）+ :func:`run_task`（选例 → 建 run 行，不真执行引擎）。
* :mod:`~testing_platform.tasks.scheduler` —— :func:`next_fire` 把
  cron/interval/once 解析为下次触发时间的**纯函数**（不挂 APScheduler）。

对外表面::

    from testing_platform.tasks import create_task, select_by_tags, run_task
    task = await create_task(project_id=pid, title="夜间回归", created_by=me,
                             scenario_tags={"all": ["smoke"]}, exec_type="scheduled",
                             schedule={"kind": "cron", "expr": "0 2 * * *"})
    scenarios = await select_by_tags(task)          # 标签选例 → 场景集合（去重）
    run = await run_task(task.id, trigger="manual") # 建 pending run 行
"""
from __future__ import annotations

from .models import (
    DDL,
    EVIDENCE_KINDS,
    FAILURE_KINDS,
    NOTIFY_CHANNELS,
    NOTIFY_ON,
    RUN_STATUSES,
    RUN_TRIGGERS,
    SCHEDULE_KINDS,
    SCENARIO_RESULT_STATUSES,
    TASK_EXEC_MODES,
    TASK_EXEC_TYPES,
    TASK_STATUSES,
    TestEvidence,
    TestRun,
    TestRunScenario,
    TestStepResult,
    TestTask,
    create_tables_via_pg,
)
from .scheduler import next_fire
from .service import (
    CASE_FILTER_KEYS,
    RUN_CODE_PREFIX,
    TAG_SET_KEYS,
    TASK_CODE_PREFIX,
    TASK_EDITABLE_FIELDS,
    TaskNotFoundError,
    create_task,
    disable_task,
    enable_task,
    get_run,
    get_task,
    list_runs,
    list_tasks,
    run_task,
    select_by_tags,
    update_task,
)

__all__ = [
    "CASE_FILTER_KEYS",
    "DDL",
    "EVIDENCE_KINDS",
    "FAILURE_KINDS",
    "NOTIFY_CHANNELS",
    "NOTIFY_ON",
    "RUN_CODE_PREFIX",
    "RUN_STATUSES",
    "RUN_TRIGGERS",
    "SCHEDULE_KINDS",
    "SCENARIO_RESULT_STATUSES",
    "TAG_SET_KEYS",
    "TASK_CODE_PREFIX",
    "TASK_EDITABLE_FIELDS",
    "TASK_EXEC_MODES",
    "TASK_EXEC_TYPES",
    "TASK_STATUSES",
    "TaskNotFoundError",
    "TestEvidence",
    "TestRun",
    "TestRunScenario",
    "TestStepResult",
    "TestTask",
    "create_task",
    "create_tables_via_pg",
    "disable_task",
    "enable_task",
    "get_run",
    "get_task",
    "list_runs",
    "list_tasks",
    "next_fire",
    "run_task",
    "select_by_tags",
    "update_task",
]
