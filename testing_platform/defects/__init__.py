"""缺陷域 —— **测试域自持 bug，与 coding 平台彻底解耦**（用户定版）。

⚠️ 旧设计（design §21.3「bug=ProjectIssue(issue_type=bug)，唯一实体，不建
``mc_test_bug``」）**已被推翻**：测试缺陷不与 coding 项目共享 issue 流、不需要
coding 项目桥。本包读写自己的 ``mc_test_bug`` 表（``project_id`` 直接是测试项目 id）。

* :mod:`~testing_platform.defects.models` —— Tortoise 模型 + 等价 DDL
  （``mc_test_bug`` / ``mc_test_bug_root_cause`` / ``mc_test_bug_pattern``）。
* :mod:`~testing_platform.defects.triage` —— 失败分流（纯函数/常量）。
* :mod:`~testing_platform.defects.service` —— 建 bug / 挂根因（过根因门）/ 读 / 沉淀。

对外表面::

    from testing_platform.defects import (
        classify_failure, compute_fingerprint,
        create_bug_from_failure, attach_root_cause, upsert_pattern,
    )

    outcome = classify_failure("product_bug")           # → 建 bug
    fp = compute_fingerprint(rsid, err, "request", diff)
    bug_id = await create_bug_from_failure(rsid, "下单 500", symptom, fp,
                                           project_id=test_project_id)
    await attach_root_cause(bug_id, {"classification": "null",
                                     "evidence_chain": [{"step": "create_order"}]})
"""
from __future__ import annotations

from .models import (
    BUG_ACTIONS,
    BUG_BROWSER_LIST,
    BUG_FOUND_INS,
    BUG_FOUND_IN_LABELS,
    BUG_LONG_LIFE_DAYS,
    BUG_OS_LIST,
    BUG_PRIORITIES,
    BUG_REJECT_RESOLUTIONS,
    BUG_RESOLUTIONS,
    BUG_RESOLUTION_LABELS,
    BUG_SEVERITIES,
    BUG_SEVERITY_LABELS,
    BUG_STATUSES,
    BUG_STATUS_ALIASES,
    BUG_STATUS_LABELS,
    BUG_TYPES,
    BUG_TYPE_LABELS,
    DDL,
    ROOT_CAUSE_CLASSIFICATIONS,
    TERMINAL_NOT_EDITABLE,
    BugAction,
    BugFieldRequiredError,
    BugTransitionNotAllowedError,
    DefectError,
    DefectNotFoundError,
    DefectValidationError,
    RootCauseEvidenceRequiredError,
    TestBug,
    TestBugLog,
    TestBugPattern,
    TestBugRootCause,
    available_actions,
    create_tables_via_pg,
    reopen_target,
)
from .service import (
    apply_bug_action,
    attach_root_cause,
    create_bug_from_failure,
    delete_bug,
    find_bug_by_fingerprint,
    get_bug,
    list_bug_log,
    list_bugs,
    list_patterns,
    update_bug,
    upsert_pattern,
)
from .triage import (
    ACTION_CREATE_BUG,
    ACTION_DEPRECATE_CASE,
    ACTION_HANDOFF,
    ACTION_MARK_FLAKY,
    ACTION_SELF_HEAL,
    FAILURE_KINDS,
    TRIAGE_OUTCOMES,
    TriageOutcome,
    classify_failure,
    compute_fingerprint,
)

__all__ = [
    "ACTION_CREATE_BUG",
    "ACTION_DEPRECATE_CASE",
    "ACTION_HANDOFF",
    "ACTION_MARK_FLAKY",
    "ACTION_SELF_HEAL",
    "BUG_ACTIONS",
    "BUG_BROWSER_LIST",
    "BUG_FOUND_INS",
    "BUG_FOUND_IN_LABELS",
    "BUG_LONG_LIFE_DAYS",
    "BUG_OS_LIST",
    "BUG_PRIORITIES",
    "BUG_REJECT_RESOLUTIONS",
    "BUG_RESOLUTIONS",
    "BUG_RESOLUTION_LABELS",
    "BUG_SEVERITIES",
    "BUG_SEVERITY_LABELS",
    "BUG_STATUSES",
    "BUG_STATUS_ALIASES",
    "BUG_STATUS_LABELS",
    "BUG_TYPES",
    "BUG_TYPE_LABELS",
    "BugAction",
    "BugFieldRequiredError",
    "BugTransitionNotAllowedError",
    "DDL",
    "DefectError",
    "DefectNotFoundError",
    "DefectValidationError",
    "FAILURE_KINDS",
    "ROOT_CAUSE_CLASSIFICATIONS",
    "RootCauseEvidenceRequiredError",
    "TERMINAL_NOT_EDITABLE",
    "TRIAGE_OUTCOMES",
    "TestBug",
    "TestBugLog",
    "TestBugPattern",
    "TestBugRootCause",
    "TriageOutcome",
    "apply_bug_action",
    "attach_root_cause",
    "available_actions",
    "classify_failure",
    "compute_fingerprint",
    "create_bug_from_failure",
    "create_tables_via_pg",
    "delete_bug",
    "find_bug_by_fingerprint",
    "get_bug",
    "list_bug_log",
    "list_patterns",
    "list_bugs",
    "reopen_target",
    "update_bug",
    "upsert_pattern",
]
