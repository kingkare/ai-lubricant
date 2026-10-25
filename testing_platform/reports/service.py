"""报告域服务（``03-report-model.md`` §2/§4 + design §15.11）。

全异步、纯 service 层函数：

* :func:`generate_report` —— 读 ``mc_test_run`` + ``mc_test_run_scenario`` +
  ``mc_test_step_result`` + ``mc_test_evidence`` → 组装 03 §2.1 JSON 快照
  （overview / timing / cases / bugs / requirements）→ 落
  :class:`~testing_platform.reports.models.TestReport`（status=ready）。
* :func:`get_report` / :func:`list_reports` —— 查询。

**快照是生成时定版（03 §6 决策 1）**：报告不实时聚合——结果表可能被清理，
快照保住当时真相，``archived`` 后仍可查看。本模块只在 :func:`generate_report`
时刻读一次结果表，``get_report`` 直接返回已存快照，**不回查** run 表。

维度接线现状（03 §2 来源列）：

======  ==========================================================
case    ``mc_test_run_scenario`` 经 ``case_refs`` 投影 → ``mc_test_case``
bugs    测试域自持 ``mc_test_bug``——按 ``from_run_id``/``from_run_scenario_id``
        归集本 run 产生的缺陷（含 root_causes，developer 受众用）
req     **测试域** ``mc_test_requirement``（§20.1 用例 ``requirement_id`` 主脊列）：
        本 run 触及的需求 + covered/partial/not_covered 覆盖度
        （not_covered = 本 run 内该需求无一条通过的用例，不是"没测过"）
======  ==========================================================
"""
from __future__ import annotations

import math
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from tortoise.exceptions import DoesNotExist

from testing_platform.assets.models import TestCase, TestScenario
from testing_platform.assets.service import AssetValidationError, _next_code
from testing_platform.tasks.models import (
    TestEvidence,
    TestRun,
    TestRunScenario,
    TestStepResult,
)

from .models import REPORT_STATUSES, TestReport

REPORT_CODE_PREFIX = "RPT-"

#: 报告「慢步骤 Top N」条数（03 §3 耗时分析）
SLOWEST_STEPS_LIMIT = 5

#: 步骤状态 → overview 计数键（03 §2.1 overview）
_COUNT_KEYS: tuple[str, ...] = ("pass", "fail", "error", "flaky", "skipped")


class ReportNotFoundError(LookupError):
    """报告/运行不存在。"""


# ── 生成（03 §2.1 / §4 服务端聚合 + 快照落库）────────────────────
async def generate_report(
    run_id: Any,
    *,
    title: Optional[str] = None,
    test_version: Optional[str] = None,
    env_id: Optional[Any] = None,
    generator: str = "auto",
) -> TestReport:
    """为一次运行生成报告（``report.generate``）——**生成时快照，不实时聚合**。

    读 run / run_scenario / step_result / evidence → 组装 03 §2.1 JSON 快照
    → 落 ``mc_test_report``（``status=ready``）。组装在插入前完成，失败即不
    留半成品行（``generating`` 状态留给未来异步生成器）。

    :param run_id: 运行 id。
    :param title: 报告标题（缺省取任务标题 + 日期，再退到 run code）。
    :param test_version: 被测版本（缺省取 ``run.global_params.test_version/version``）。
    :param env_id: 环境（缺省取 ``run.env_id``）。
    :param generator: ``auto`` / ``manual``（03 §1）。
    """
    if generator not in ("auto", "manual"):
        raise AssetValidationError(f"generator={generator!r} 不在可选值 ('auto', 'manual') 内")

    run = await _get_run(run_id)
    scenario_rows = await TestRunScenario.filter(run_id=run.id).order_by("started_at", "id")
    step_rows = await _load_steps([row.id for row in scenario_rows])
    evidence_rows = await _load_evidence([row.id for row in scenario_rows])

    generated_at = datetime.now(timezone.utc)
    effective_version = test_version or _version_from_global_params(run.global_params)
    effective_env = env_id or run.env_id
    effective_title = title or await _default_title(run, generated_at)
    code = await _next_code(TestReport, run.project_id, REPORT_CODE_PREFIX)

    snapshot = {
        "code": code,
        "title": effective_title,
        "test_version": effective_version,
        "env": str(effective_env) if effective_env else None,
        "overview": _build_overview(run, scenario_rows),
        "timing": _build_timing(step_rows),
        "cases": await _build_cases(scenario_rows, step_rows, evidence_rows),
        "bugs": await _collect_bugs(run, scenario_rows),
        "requirements": await _collect_requirements(run, scenario_rows),
    }

    return await TestReport.create(
        project_id=run.project_id,
        code=code,
        run_id=run.id,
        task_id=run.task_id,
        title=effective_title,
        test_version=effective_version,
        env_id=_uuid.UUID(str(effective_env)) if effective_env else None,
        status="ready",
        snapshot=snapshot,
        generated_at=generated_at,
        generator=generator,
    )


async def _get_run(run_id: Any) -> TestRun:
    try:
        return await TestRun.get(id=_uuid.UUID(str(run_id)))
    except DoesNotExist:
        raise ReportNotFoundError(f"运行不存在：{run_id!r}") from None
    except (ValueError, TypeError, AttributeError) as exc:
        raise AssetValidationError(f"run_id 不是合法 UUID：{run_id!r}") from exc


async def _load_steps(scenario_ids: Sequence[Any]) -> dict[Any, list[TestStepResult]]:
    """run_scenario_id → 步骤结果（按 seq 升序，seq 非整型则排在最后）。"""
    grouped: dict[Any, list[TestStepResult]] = {sid: [] for sid in scenario_ids}
    if not scenario_ids:
        return grouped
    rows = await TestStepResult.filter(run_scenario_id__in=list(scenario_ids))
    for row in rows:
        grouped.setdefault(row.run_scenario_id, []).append(row)
    for rows_of_scenario in grouped.values():
        rows_of_scenario.sort(key=_seq_sort_key)
    return grouped


def _seq_sort_key(row: TestStepResult) -> tuple[int, int]:
    try:
        return (0, int(str(row.seq)))
    except (TypeError, ValueError):
        return (1, 0)


async def _load_evidence(scenario_ids: Sequence[Any]) -> dict[Any, list[TestEvidence]]:
    """run_scenario_id → 证据行。"""
    grouped: dict[Any, list[TestEvidence]] = {sid: [] for sid in scenario_ids}
    if not scenario_ids:
        return grouped
    rows = await TestEvidence.filter(run_scenario_id__in=list(scenario_ids)).order_by("step_seq", "id")
    for row in rows:
        grouped.setdefault(row.run_scenario_id, []).append(row)
    return grouped


# ── overview（03 §2.1）──────────────────────────────────────────
def _build_overview(run: TestRun, scenario_rows: Sequence[TestRunScenario]) -> dict[str, Any]:
    """概览统计 —— 计数取自 run_scenario（执行单位是场景，design §15.10 决策 1）。

    ``run.summary`` 存在时其 ``duration_ms`` 优先（引擎 finalize 落的权威总耗时）。
    """
    counts = dict.fromkeys(_COUNT_KEYS, 0)
    duration = 0
    for row in scenario_rows:
        if row.status in counts:
            counts[row.status] += 1
        duration += int(row.duration_ms or 0)

    summary = run.summary if isinstance(run.summary, Mapping) else {}
    if isinstance(summary.get("duration_ms"), (int, float)):
        duration = int(summary["duration_ms"])

    total = len(scenario_rows)
    return {
        "total": total,
        "pass": counts["pass"],
        "fail": counts["fail"],
        "error": counts["error"],
        "flaky": counts["flaky"],
        "skipped": counts["skipped"],
        "duration_ms": duration,
        "pass_rate": round(counts["pass"] / total, 4) if total else 0.0,
    }


# ── timing（03 §2 响应时间 / §3 耗时分析）───────────────────────
def _build_timing(step_groups: Mapping[Any, list[TestStepResult]]) -> dict[str, Any]:
    """响应时间 —— ``avg``/``p95`` 取 request 类步骤耗时（03 §2）；慢步骤 Top N 取全步骤。"""
    all_steps = [step for rows in step_groups.values() for step in rows]
    request_durations = [
        int(step.duration_ms)
        for step in all_steps
        if step.type == "request" and isinstance(step.duration_ms, int)
    ]

    avg: Optional[int] = None
    p95: Optional[int] = None
    if request_durations:
        avg = round(sum(request_durations) / len(request_durations))
        p95 = _percentile(request_durations, 0.95)

    slowest = sorted(
        (
            {"step": step.step_key, "ms": int(step.duration_ms)}
            for step in all_steps
            if isinstance(step.duration_ms, int)
        ),
        key=lambda item: item["ms"],
        reverse=True,
    )[:SLOWEST_STEPS_LIMIT]

    return {"avg_response_ms": avg, "p95_response_ms": p95, "slowest_steps": slowest}


def _percentile(values: Sequence[int], quantile: float) -> int:
    """最近秩法（nearest-rank）：``ceil(q * n)`` 位。"""
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[min(index, len(ordered) - 1)]


# ── cases（03 §2 用例列表：run_scenario 经 case_refs 投影）──────
async def _build_cases(
    scenario_rows: Sequence[TestRunScenario],
    step_groups: Mapping[Any, list[TestStepResult]],
    evidence_groups: Mapping[Any, list[TestEvidence]],
) -> list[dict[str, Any]]:
    """场景结果 → 用例条目（03 §2.1 cases；用例级结果由场景结果投影得出）。"""
    cases: list[dict[str, Any]] = []
    for scenario_row in scenario_rows:
        scenario_code = await _resolve_scenario_code(scenario_row.scenario_id)
        steps = [_step_document(step) for step in step_groups.get(scenario_row.id, [])]
        evidence = _evidence_uris(scenario_row, step_groups, evidence_groups)

        case_refs = list(scenario_row.case_refs or [])
        if not case_refs:
            # 场景未挂用例（直接跑场景）——仍列出，避免运行在报告里"隐身"
            cases.append(
                {
                    "case_code": None,
                    "title": None,
                    "scenario": scenario_code,
                    "scenario_id": str(scenario_row.scenario_id),
                    "status": scenario_row.status,
                    "duration_ms": scenario_row.duration_ms,
                    "steps": steps,
                    "evidence": evidence,
                }
            )
            continue

        for case_ref in case_refs:
            code, case_title, requirement_id = await _resolve_case(case_ref)
            cases.append(
                {
                    "case_code": code,
                    "title": case_title,
                    "scenario": scenario_code,
                    "scenario_id": str(scenario_row.scenario_id),
                    "status": scenario_row.status,
                    "duration_ms": scenario_row.duration_ms,
                    "steps": steps,
                    "evidence": list(evidence),
                    # 用例→需求映射（§20.1 主脊列；product 受众 risks/coverage 用）
                    "requirement_id": requirement_id,
                }
            )
    return cases


def _step_document(step: TestStepResult) -> dict[str, Any]:
    """步骤结果行 → 03 §2.1 case.steps 条目（断言三件套来自 step_result）。"""
    seq: Any = step.seq
    try:
        seq = int(str(seq))
    except (TypeError, ValueError):
        pass
    return {
        "step_key": step.step_key,
        "seq": seq,
        "type": step.type,
        "status": step.status,
        "attempt": step.attempt,
        "duration_ms": step.duration_ms,
        "expected": step.expected,
        "actual": step.actual,
        "diff": step.diff,
    }


def _evidence_uris(
    scenario_row: TestRunScenario,
    step_groups: Mapping[Any, list[TestStepResult]],
    evidence_groups: Mapping[Any, list[TestEvidence]],
) -> list[str]:
    """证据 URI —— 主源 ``mc_test_evidence.uri``；回退引擎内联在 step_result.evidence 的项。"""
    uris: list[str] = [row.uri for row in evidence_groups.get(scenario_row.id, []) if row.uri]
    seen = set(uris)
    for step in step_groups.get(scenario_row.id, []):
        for item in step.evidence or []:
            uri = item.get("uri") if isinstance(item, Mapping) else None
            if uri and uri not in seen:
                seen.add(uri)
                uris.append(uri)
    return uris


async def _resolve_scenario_code(scenario_id: Any) -> Optional[str]:
    """``scenario_id``（TEXT：UUID 或 SCN code）→ 场景 code；解析不了回退原值。"""
    try:
        return (await TestScenario.get(id=_uuid.UUID(str(scenario_id)))).code
    except (DoesNotExist, ValueError, TypeError, AttributeError):
        text = str(scenario_id)
        try:
            return (await TestScenario.get(code=text)).code
        except DoesNotExist:
            return text


async def _resolve_case(case_ref: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``case_ref``（UUID 或 TC code）→ (code, title, requirement_id)；解析不了回退 (原值, None, None)。

    requirement_id 由 ``TestCase.requirement_id`` 直接读（§20.1 用例挂需求的主脊列），
    报告 cases 与 requirements_coverage 都依赖它做覆盖映射（之前漏了 → developer/
    product 两受众视图拿不到 case↔需求映射）。
    """
    try:
        case = await TestCase.get(id=_uuid.UUID(str(case_ref)))
        return case.code, case.title, str(case.requirement_id) if case.requirement_id else None
    except (DoesNotExist, ValueError, TypeError, AttributeError):
        pass
    try:
        case = await TestCase.get(code=str(case_ref))
        return case.code, case.title, str(case.requirement_id) if case.requirement_id else None
    except DoesNotExist:
        return str(case_ref), None, None


async def _default_title(run: TestRun, generated_at: datetime) -> str:
    """标题缺省：``{任务标题} · {日期}`` → ``测试报告 · {run code}``。"""
    date_text = generated_at.strftime("%Y-%m-%d")
    if run.task_id:
        from testing_platform.tasks.models import TestTask

        try:
            task = await TestTask.get(id=run.task_id)
            return f"{task.title} · {date_text}"
        except DoesNotExist:
            pass
    return f"测试报告 · {run.code}"


def _version_from_global_params(global_params: Any) -> Optional[str]:
    """被测版本缺省源：``run.global_params`` 的 ``test_version`` / ``version``。"""
    if not isinstance(global_params, Mapping):
        return None
    for key in ("test_version", "version"):
        value = global_params.get(key)
        if value:
            return str(value)
    return None


# ── bugs / requirements（03 §2：未接则空数组）───────────────────
async def _collect_bugs(run: TestRun, scenario_rows: Sequence[TestRunScenario]) -> list[dict[str, Any]]:
    """关联 Bug（03 §2）——按 ``from_run_id`` + ``from_run_scenario_id`` 归集本 run 产生的缺陷。

    bug 文档形状复用 :func:`testing_platform.defects.service._bug_document`（含
    root_causes，developer 受众视图直接读）。查询失败/无 bug → 空数组（不炸报告）。
    """
    from testing_platform.defects.models import TestBug, TestBugRootCause
    from testing_platform.defects.service import _bug_document

    scenario_ids = [row.id for row in scenario_rows if row.id is not None]
    clauses = []
    if run.id is not None:
        clauses.append({"from_run_id": run.id})
    for sid in scenario_ids:
        clauses.append({"from_run_scenario_id": sid})
    if not clauses:
        return []

    bugs: list[TestBug] = []
    seen: set[str] = set()
    try:
        for cond in clauses:
            for b in await TestBug.filter(**cond):
                key = str(b.id)
                if key not in seen:
                    seen.add(key)
                    bugs.append(b)
    except Exception:  # noqa: BLE001 — 缺陷域表结构缺列/连接异常时报告不留空失败，退空
        return []

    if not bugs:
        return []
    ids = [b.id for b in bugs]
    causes: dict[str, list] = {}
    try:
        for rc in await TestBugRootCause.filter(bug_id__in=ids):
            causes.setdefault(str(rc.bug_id), []).append(rc)
    except Exception:  # noqa: BLE001
        pass
    return [_bug_document(b, causes.get(str(b.id))) for b in bugs]


async def _collect_requirements(run: TestRun, scenario_rows: Sequence[TestRunScenario]) -> list[dict[str, Any]]:
    """关联需求（03 §6 决策 3）——**本次运行触及**的需求 + 覆盖度。

    归集口径（按 run 自己的结果，不拉全量需求当分母）：

    - 需求全集 = 本 run 各场景 ``case_refs`` → ``TestCase.requirement_id`` 命中集；
    - 每个需求取其下本 run 出现的用例结果，``covered``=全部 pass /
      ``partial``=有 pass 有挂 / ``not_covered``=没有任何通过用例（全挂或无用例）。
      ——「not_covered」这里指**本 run 内该需求没有一条通过的用例**（不是"没测过"，
      没测过的需求本就不进报告集合）。
    - 未挂需求的用例（requirement_id 为空）不进需求集合。

    返回 ``[{requirement_id, code, title, module, coverage, cases:[{case_code,status}]}]``。
    """
    from testing_platform.assets.models import TestRequirement

    case_refs: list[Any] = []
    for row in scenario_rows:
        case_refs.extend(list(row.case_refs or []))
    if not case_refs:
        return []

    # 去重解析 case_ref → (requirement_id, case_code, status)
    req_cases: dict[str, list[dict[str, Any]]] = {}
    for row in scenario_rows:
        for case_ref in (row.case_refs or []):
            code, _title, req_id = await _resolve_case(case_ref)
            if not req_id:
                continue
            req_cases.setdefault(req_id, []).append(
                {"case_code": code, "status": row.status}
            )
    if not req_cases:
        return []

    out: list[dict[str, Any]] = []
    for req_id, cs in req_cases.items():
        req = await TestRequirement.get_or_none(id=_safe_uuid(req_id))
        statuses = [c["status"] for c in cs]
        passed = sum(1 for s in statuses if s == "pass")
        if passed == len(statuses):
            coverage = "covered"
        elif passed > 0:
            coverage = "partial"
        else:
            coverage = "not_covered"
        out.append({
            "requirement_id": req_id,
            "code": req.code if req else None,
            "title": req.title if req else None,
            "module": req.module if req else None,
            "coverage": coverage,
            "cases": cs,
        })
    return out


def _safe_uuid(value: Any) -> Any:
    """尽力把字符串转 UUID，失败原样回传（get_or_none 会因非法 id 抛，调用方兜底）。"""
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return value


# ── 查询 ────────────────────────────────────────────────────────
async def get_report(report_id: Any) -> TestReport:
    """取报告（直接读已存快照，不回查 run —— 生成时定版）。"""
    try:
        return await TestReport.get(id=_uuid.UUID(str(report_id)))
    except DoesNotExist:
        raise ReportNotFoundError(f"报告不存在：{report_id!r}") from None
    except (ValueError, TypeError, AttributeError) as exc:
        raise AssetValidationError(f"report_id 不是合法 UUID：{report_id!r}") from exc


async def list_reports(
    project_id: Any,
    *,
    status: Optional[str] = None,
    run_id: Optional[Any] = None,
) -> list[TestReport]:
    """按项目列报告（可按状态/运行收窄），最新在前。"""
    if status is not None and status not in REPORT_STATUSES:
        raise AssetValidationError(
            f"status={status!r} 不在可选值 {list(REPORT_STATUSES)} 内"
        )
    try:
        qs = TestReport.filter(project_id=_uuid.UUID(str(project_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise AssetValidationError(f"project_id 不是合法 UUID：{project_id!r}") from exc
    if status:
        qs = qs.filter(status=status)
    if run_id is not None:
        qs = qs.filter(run_id=_uuid.UUID(str(run_id)))
    return await qs.order_by("-generated_at", "-code")


__all__ = [
    "REPORT_CODE_PREFIX",
    "ReportNotFoundError",
    "generate_report",
    "get_report",
    "list_reports",
]
