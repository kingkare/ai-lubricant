"""测试策略域 —— 一轮测试的执行分配层（用户定版 2026-10-14）。

四类策略（``strategy_type``）：冒烟测试 / 全量测试 / 回归测试 / 性能测试。
一轮 = 挂测试版本 + 勾选用例 + 分配人员（人工或自动执行）+ 测试时间 + 测试结果。
**缺陷的第二维度**：bug 归属 = 测试版本（``version_id``）× 测试策略
（``mc_test_bug.strategy_id``）。

* :mod:`~testing_platform.strategy.models` —— Tortoise 模型 + 等价 DDL
  （``mc_test_strategy`` / ``mc_test_strategy_case``）。
* :mod:`~testing_platform.strategy.service` —— CRUD / 勾选用例 / 分配 / 结果录入 / 汇总。

对外表面::

    from testing_platform.strategy import create_strategy, list_strategies

    s = await create_strategy(project_id=pid, name="v1.2 冒烟", strategy_type="smoke",
                              version_id=vid, start_time="2026-10-20T09:00:00")
    await set_strategy_cases(s.id, [case_id_1, case_id_2], assignee_id=tester_id)
    await record_case_result(s.id, case_id_1, result="pass", executed_by=tester_id)
"""
from __future__ import annotations

from .models import (
    DDL,
    STRATEGY_CASE_RESULTS,
    STRATEGY_EXEC_MODES,
    STRATEGY_STATUSES,
    STRATEGY_TYPES,
    TestStrategy,
    TestStrategyCase,
    create_tables_via_pg,
)
from .report import STRATEGY_P0_P1_PRIORITIES, compute_strategy_report
from .service import (
    STRATEGY_CODE_PREFIX,
    StrategyNotFoundError,
    assign_case,
    create_strategy,
    delete_strategy,
    get_strategy,
    list_strategies,
    record_case_result,
    remove_case,
    set_strategy_cases,
    strategy_report,
    summarize,
    update_strategy,
)

__all__ = [
    "DDL",
    "STRATEGY_CASE_RESULTS",
    "STRATEGY_CODE_PREFIX",
    "STRATEGY_EXEC_MODES",
    "STRATEGY_P0_P1_PRIORITIES",
    "STRATEGY_STATUSES",
    "STRATEGY_TYPES",
    "StrategyNotFoundError",
    "TestStrategy",
    "TestStrategyCase",
    "assign_case",
    "compute_strategy_report",
    "create_strategy",
    "create_tables_via_pg",
    "delete_strategy",
    "get_strategy",
    "list_strategies",
    "record_case_result",
    "remove_case",
    "set_strategy_cases",
    "strategy_report",
    "summarize",
    "update_strategy",
]
