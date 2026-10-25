"""测试平台 · 生成环节包（25 号文 v4.1 第二部分起）。

子模块：

- :mod:`testing_platform.generation.models` —— 数据模型（六表：``mc_test_gen_run``
  /``mc_test_testpoint``/``mc_test_proposal``/``mc_test_coverage_cell``/
  ``mc_test_rule`` + ``mc_test_question`` 扩列）+ Postgres 方言 DDL + 枚举常量。
- :mod:`testing_platform.generation.rules` —— 用户规则机制的纯函数层
  （作用域筛选 / 渲染提示词块 / shadow 留痕 / 计数聚合 / 豁免过期）。**不**由本
  ``__init__`` 重导出——它有自己的稳定对外 API（见其 ``__all__``），直接
  ``from testing_platform.generation.rules import ...`` 即可，避免枚举常量名冲突。

本 ``__init__`` 只按交付契约导出**模型类 + DDL + :func:`ensure_question_columns`**
及与表对齐的枚举常量（供整合步骤/调用方一处取齐）。
"""
from __future__ import annotations

from .models import (  # noqa: F401
    COVERAGE_CELL_STATES,
    DDL,
    GEN_RUN_BUDGET_STATES,
    GEN_RUN_GATE_STATUSES,
    GEN_RUN_STATUSES,
    GEN_RUN_TYPES,
    PROPOSAL_CONSENSUS,
    PROPOSAL_EVIDENCE_CLASSES,
    PROPOSAL_KINDS,
    PROPOSAL_REVIEW_STATES,
    PROPOSAL_RULE_DISPOSITIONS,
    PROPOSAL_STATUSES,
    QUESTION_GATES,
    QUESTION_RESOLUTIONS_GENERATION,
    RULE_DEFAULT_ENFORCEMENT,
    RULE_ENFORCEMENTS,
    RULE_KINDS,
    RULE_SCOPES,
    TESTPOINT_BATCH_TIERS,
    TESTPOINT_BASES,
    TESTPOINT_CASE_REVIEW_STATES,
    TESTPOINT_DESIGN_METHOD_LABELS,
    TESTPOINT_DESIGN_METHODS,
    TESTPOINT_STATUSES,
    TESTPOINT_SURFACES,
    TESTPOINT_SURFACES_DECIDED_BY,
    TestCoverageCell,
    TestGenRun,
    TestProposal,
    TestRule,
    TestTestpoint,
    ensure_question_columns,
)

__all__ = [
    "DDL",
    "TestGenRun",
    "TestTestpoint",
    "TestProposal",
    "TestCoverageCell",
    "TestRule",
    "ensure_question_columns",
    # 枚举常量（与表对齐）
    "GEN_RUN_TYPES",
    "GEN_RUN_STATUSES",
    "GEN_RUN_GATE_STATUSES",
    "GEN_RUN_BUDGET_STATES",
    "TESTPOINT_SURFACES",
    "TESTPOINT_SURFACES_DECIDED_BY",
    "TESTPOINT_DESIGN_METHODS",
    "TESTPOINT_DESIGN_METHOD_LABELS",
    "TESTPOINT_BATCH_TIERS",
    "TESTPOINT_BASES",
    "TESTPOINT_STATUSES",
    "TESTPOINT_CASE_REVIEW_STATES",
    "PROPOSAL_KINDS",
    "PROPOSAL_EVIDENCE_CLASSES",
    "PROPOSAL_REVIEW_STATES",
    "PROPOSAL_STATUSES",
    "PROPOSAL_RULE_DISPOSITIONS",
    "PROPOSAL_CONSENSUS",
    "COVERAGE_CELL_STATES",
    "RULE_KINDS",
    "RULE_SCOPES",
    "RULE_ENFORCEMENTS",
    "RULE_DEFAULT_ENFORCEMENT",
    "QUESTION_GATES",
    "QUESTION_RESOLUTIONS_GENERATION",
]
