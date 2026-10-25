"""报告域（``03-report-model.md``）：报告实体与生成时快照。

本包是 AI 测试平台**报告侧**的一等事实源（design §15.11）：

* :mod:`~testing_platform.reports.models` —— Tortoise 模型 + 等价 DDL
  （``mc_test_report``，03 §1）。
* :mod:`~testing_platform.reports.service` —— :func:`generate_report`
  读 run/run_scenario/step_result/evidence 组装 03 §2.1 JSON 快照并落库
  （**生成时定版**，03 §6 决策 1）；:func:`get_report` /
  :func:`list_reports` 查询。

对外表面::

    from testing_platform.reports import generate_report, get_report
    report = await generate_report(run.id)          # 组装快照 → 落 mc_test_report
    snapshot = report.snapshot                       # 已定版 JSON，无二次聚合
"""
from __future__ import annotations

from .models import (
    DDL,
    REPORT_GENERATORS,
    REPORT_STATUSES,
    TestReport,
    create_tables_via_pg,
)
from .cycle import compute_cycle_report
from .service import (
    REPORT_CODE_PREFIX,
    ReportNotFoundError,
    generate_report,
    get_report,
    list_reports,
)

__all__ = [
    "DDL",
    "REPORT_CODE_PREFIX",
    "REPORT_GENERATORS",
    "REPORT_STATUSES",
    "ReportNotFoundError",
    "TestReport",
    "compute_cycle_report",
    "create_tables_via_pg",
    "generate_report",
    "get_report",
    "list_reports",
]