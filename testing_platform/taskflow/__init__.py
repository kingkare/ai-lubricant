"""任务层（26 号文）——「一次具体执行」的载体 + 暂存标识。

与 25 号文（环节/方法层）的关系：**两层，不冲突、不泛化**（D-1 已裁）。
任务层管「用户在哪个阶段看到什么」，环节层管「这一阶段内部怎么跑」。
连接点：``TestTaskRun.refs.gen_run_id`` → ``mc_test_gen_run``。
"""
from .models import (
    DDL,
    PHASE_OF_STATUS,
    STATUS_LABELS_BY_TYPE,
    TERMINAL_TASK_STATUSES,
    TASK_PHASES,
    TASK_PHASE_LABELS,
    TASK_RUN_STATUSES,
    TASK_RUN_STATUS_LABELS,
    TASK_TRANSITIONS,
    TASK_TYPES,
    TASK_TYPE_LABELS,
    WAITING_TASK_STATUSES,
    TestTaskRun,
    can_transition,
    create_tables_via_pg,
    explore_scope_required,
    is_terminal,
    is_waiting_on_human,
    phase_of,
)
from .staging import (
    APPLY_ACTIONS,
    STAGE_COLUMNS,
    STAGED_TABLES,
    apply_staged,
    count_staged,
    ensure_stage_columns,
    revert_staged,
)

__all__ = [
    # 取值域 / 标签
    "DDL",
    "TASK_TYPES",
    "TASK_TYPE_LABELS",
    "TASK_PHASES",
    "TASK_PHASE_LABELS",
    "TASK_RUN_STATUSES",
    "TASK_RUN_STATUS_LABELS",
    "TASK_TRANSITIONS",
    "PHASE_OF_STATUS",
    "STATUS_LABELS_BY_TYPE",
    "TERMINAL_TASK_STATUSES",
    "WAITING_TASK_STATUSES",
    # 纯函数
    "can_transition",
    "is_terminal",
    "is_waiting_on_human",
    "phase_of",
    "explore_scope_required",
    # ORM
    "TestTaskRun",
    "create_tables_via_pg",
    # 暂存标识
    "STAGED_TABLES",
    "STAGE_COLUMNS",
    "APPLY_ACTIONS",
    "ensure_stage_columns",
    "apply_staged",
    "revert_staged",
    "count_staged",
]
