"""测试报告四类受众视图（25 号文 §环节 10 报告部分，P2）。

25 号文要求报告「**结论先行、分级呈现、四类受众视图**」——同一份
:class:`~testing_platform.reports.models.TestReport` 快照按受众裁剪/重排出
四个不同侧重面：

================  ===========================================================
``executor``     执行者/QA：挂了什么、为什么挂、怎么复现、每步输入输出与证据
``developer``    研发：根因定位、关联代码、推荐 SQL、关联历史 bug、修复验证
``product``      产品/需求：需求覆盖了没、哪些没测、风险清单、业务结论
``manager``      管理者：结论先行——能不能上（Go/No-Go）、失败率、置信度、趋势
================  ===========================================================

本模块是**纯函数、无 DB、无副作用**（同 :mod:`~testing_platform.reports.cycle`
的约定）：输入是 ``TestReport`` 行（Tortoise 对象）或其 dict（snapshot 本体
或带 ``snapshot`` 键的序列化行），输出是按受众裁剪后的 dict。结论
（``conclusion``）永远在返回 dict 的**首位**（结论先行，25 号文 §环节 10）。

对外表面::

    from testing_platform.reports.audience_views import audience_view
    view = audience_view(report, "manager")    # → {conclusion, ...}
    # 或直接调四个构建器：
    from testing_platform.reports.audience_views import manager_view
    view = manager_view(report)
"""
from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "executor_view",
    "developer_view",
    "product_view",
    "manager_view",
    "audience_view",
]

#: 受众白名单（25 号文 §环节 10 四类受众）
AUDIENCES: tuple[str, ...] = ("executor", "developer", "product", "manager")

#: 失败/错误态——结论判定与「失败优先」排序用（对齐 overview 计数键）
_FAILURE_STATES: frozenset[str] = frozenset({"fail", "error"})


# ── 输入归一化：TestReport 行 / snapshot dict / 序列化行 → (snapshot, row) ──
def _coerce_snapshot(report: Any) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """把 ``TestReport`` 行或 dict 归一为 (snapshot_dict, row_meta)。

    接受三种形态：
    1. Tortoise ``TestReport`` 对象（有 ``.snapshot`` 属性）—— row_meta 取行字段；
    2. 序列化行 dict（含 ``snapshot`` 键）—— snapshot=report["snapshot"]；
    3. snapshot 本体 dict（含 overview/cases 等键，无 ``snapshot`` 键）—— 自身即快照。

    其他类型抛 ``TypeError``。
    """
    if isinstance(report, Mapping):
        snap = report.get("snapshot")
        if isinstance(snap, Mapping):
            return dict(snap), report
        return dict(report), report

    # ORM 行（TestReport 实例）
    snap = getattr(report, "snapshot", None)
    if isinstance(snap, Mapping):
        row = {
            "code": getattr(report, "code", None),
            "title": getattr(report, "title", None),
            "test_version": getattr(report, "test_version", None),
            "status": getattr(report, "status", None),
            "generator": getattr(report, "generator", None),
            "generated_at": getattr(report, "generated_at", None),
            "env_id": getattr(report, "env_id", None),
            "run_id": getattr(report, "run_id", None),
            "task_id": getattr(report, "task_id", None),
            "project_id": getattr(report, "project_id", None),
        }
        return dict(snap), row

    raise TypeError(
        f"report 必须是 TestReport 行或 Mapping，收到 {type(report).__name__}"
    )


def _build_meta(snapshot: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """行级字段优先、快照内字段兜底，凑出受众视图公用 meta。"""
    meta: dict[str, Any] = {}
    # 快照内自带（03 §2.1：code/title/test_version/env）
    for key in ("code", "title", "test_version", "env"):
        meta[key] = snapshot.get(key)
    # 行级覆盖（TestReport 行字段更权威）
    for key in (
        "code", "title", "test_version", "status", "generator",
        "generated_at", "env_id", "run_id", "task_id", "project_id",
    ):
        val = row.get(key) if isinstance(row, Mapping) else None
        if val is not None:
            meta[key] = val
    return meta


# ── 结论先行：从 overview 派生 Go/No-Go + 一句摘要 ─────────────────
def _confidence_of(snapshot: Mapping[str, Any], row: Mapping[str, Any]) -> Any:
    """置信度——读 snapshot/row 上的 confidence / release_gate.confidence。

    25 号文 §环节 10 上线判定：共识 go 且置信 ≥ 0.9 才 go。v1 snapshot 无此字段
    时返回 ``None``（视图层不编造精度）。
    """
    for src in (row, snapshot):
        if not isinstance(src, Mapping):
            continue
        for key in ("confidence", "release_confidence"):
            if src.get(key) is not None:
                return src.get(key)
        gate = src.get("release_gate")
        if isinstance(gate, Mapping) and gate.get("confidence") is not None:
            return gate.get("confidence")
    return None


def _verdict(total: int, fail: int, error: int, flaky: int, skipped: int) -> str:
    """Go/No-Go/Conditional 判定（25 号文 §环节 10 双条件门的结论侧）。

    - 无任何执行数 → ``unknown``（数据驱动：没跑就没有结论可下）
    - 有失败或错误 → ``no_go``
    - 0 失败 0 错误 但有抖动/跳过 → ``conditional``（需人工确认）
    - 0 失败 0 错误 且 无抖动/跳过 → ``go``
    """
    if not total:
        return "unknown"
    if fail or error:
        return "no_go"
    if flaky or skipped:
        return "conditional"
    return "go"


_VERDICT_LABEL = {"go": "Go", "no_go": "No-Go", "conditional": "Conditional", "unknown": "No Data"}


def _build_conclusion(snapshot: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """结论行——结论先行，置于每个受众视图返回 dict 的首位。

    数据驱动（25 号文 §环节 10 报告：结论必须有执行数）：所有数来自 overview，
    不写无来源的散文。
    """
    overview = snapshot.get("overview") if isinstance(snapshot.get("overview"), Mapping) else {}
    total = int(overview.get("total") or 0)
    passed = int(overview.get("pass") or 0)
    fail = int(overview.get("fail") or 0)
    error = int(overview.get("error") or 0)
    flaky = int(overview.get("flaky") or 0)
    skipped = int(overview.get("skipped") or 0)
    duration_ms = int(overview.get("duration_ms") or 0)

    pass_rate = overview.get("pass_rate")
    fail_rate = round((fail + error) / total, 4) if total else 0.0

    verdict = _verdict(total, fail, error, flaky, skipped)
    label = _VERDICT_LABEL[verdict]
    if verdict == "unknown":
        headline = "No Data：本次运行无执行用例，无法给出上线结论"
    elif verdict == "go":
        headline = f"Go：{total} 用例全过，通过率 {round((passed / total * 100) if total else 0, 1)}%"
    elif verdict == "no_go":
        headline = (
            f"No-Go：{fail + error} 项失败/错误（失败 {fail} · 错误 {error}），建议拦截上线"
        )
    else:
        headline = (
            f"Conditional：通过 {passed}/{total}，另有抖动 {flaky} · 跳过 {skipped}，需人工确认"
        )

    # 顶层 blocker（失败/错误用例，最多 5 条）—— 管理者与执行者都关心
    blockers: list[dict[str, Any]] = []
    for case in snapshot.get("cases") or []:
        if case.get("status") in _FAILURE_STATES:
            blockers.append({
                "case_code": case.get("case_code"),
                "title": case.get("title"),
                "status": case.get("status"),
                "scenario": case.get("scenario"),
                "duration_ms": case.get("duration_ms"),
            })
        if len(blockers) >= 5:
            break

    return {
        "verdict": verdict,                 # 机器可读
        "go_no_go": label,                 # 人可读 Go/No-Go/Conditional
        "headline": headline,              # 一句摘要（25 号文 §环节 10）
        "pass_rate": pass_rate,
        "fail_rate": fail_rate,
        "counts": {
            "total": total, "pass": passed, "fail": fail,
            "error": error, "flaky": flaky, "skipped": skipped,
        },
        "duration_ms": duration_ms,
        "confidence": _confidence_of(snapshot, row),
        "top_blockers": blockers,
    }


# ── 受众公用骨架：结论永远在首位 ─────────────────────────────────
def _base_view(snapshot: Mapping[str, Any], row: Mapping[str, Any], audience: str) -> dict[str, Any]:
    view: dict[str, Any] = {
        "conclusion": _build_conclusion(snapshot, row),   # 结论先行
        "audience": audience,
        "meta": _build_meta(snapshot, row),
    }
    return view


# ── 用例工具 ─────────────────────────────────────────────────────
def _case_is_failure(case: Mapping[str, Any]) -> bool:
    return case.get("status") in _FAILURE_STATES


def _sort_failures_first(cases: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """失败/错误用例排在前面（同态内保持原顺序）。"""
    return sorted(cases, key=lambda c: (0 if _case_is_failure(c) else 1,))


# ── 1) 执行者视图：挂了什么 / 怎么复现 / 每步输入输出与证据 ───────
def executor_view(report: Any) -> dict[str, Any]:
    """执行者/QA 视图——关注失败用例、复现步骤、每步 expected/actual/diff 与证据。

    含：``conclusion``（结论先行）+ ``cases``（失败优先，带完整 steps/evidence）
    + ``failures``（仅失败子集，便于直接复现）+ ``timing``（响应耗时与慢步骤）。
    """
    snapshot, row = _coerce_snapshot(report)
    view = _base_view(snapshot, row, "executor")

    all_cases = list(snapshot.get("cases") or [])
    ordered = _sort_failures_first(all_cases)

    # 执行者要每步的输入输出与证据——原样保留 steps（含 expected/actual/diff）
    # 与 evidence URI 列表。
    view["cases"] = [
        {
            "case_code": c.get("case_code"),
            "title": c.get("title"),
            "scenario": c.get("scenario"),
            "status": c.get("status"),
            "duration_ms": c.get("duration_ms"),
            "steps": list(c.get("steps") or []),
            "evidence": list(c.get("evidence") or []),
        }
        for c in ordered
    ]
    view["failures"] = [c for c in view["cases"] if _case_is_failure(c)]

    timing = snapshot.get("timing") if isinstance(snapshot.get("timing"), Mapping) else {}
    view["timing"] = {
        "avg_response_ms": timing.get("avg_response_ms"),
        "p95_response_ms": timing.get("p95_response_ms"),
        "slowest_steps": list(timing.get("slowest_steps") or []),
    }
    return view


# ── 2) 研发视图：根因 / 关联代码 / 推荐 SQL / 关联历史 bug / 修复验证 ─
def _extract_recommended(chain: list[Any], kinds: tuple[str, ...], text_keys: tuple[str, ...]) -> list[Any]:
    """从 evidence_chain 按类别捞推荐项（SQL/日志等）。

    evidence_chain 项是任意 JSON——``kind`` 命中则取首个非空文本键的值。
    """
    out: list[Any] = []
    for item in chain or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind", "")).lower() not in kinds:
            continue
        for key in text_keys:
            val = item.get(key)
            if val not in (None, ""):
                out.append(val)
                break
    return out


def _developer_bug(bug: Mapping[str, Any]) -> dict[str, Any]:
    """裁剪 bug 文档为研发关心的面：根因 + 推荐 SQL/日志 + 关联 bug + 修复验证。"""
    root_causes = [
        {
            "location": rc.get("location"),
            "classification": rc.get("classification"),
            "hypothesis": rc.get("hypothesis"),
            "confidence": rc.get("confidence"),
            "attributed_commit": rc.get("attributed_commit"),
            "fix_suggestion": rc.get("fix_suggestion"),
            "healing_suggestion": rc.get("healing_suggestion"),
            "blast_radius": list(rc.get("blast_radius") or []),
            "evidence_chain": list(rc.get("evidence_chain") or []),
        }
        for rc in (bug.get("root_causes") or [])
    ]

    # 推荐 SQL/日志：优先 bug 顶层显式字段，回退到 root_cause.evidence_chain 按类别捞
    recommended_sql = list(bug.get("recommended_sql") or [])
    recommended_logs = list(bug.get("recommended_logs") or [])
    for rc in (bug.get("root_causes") or []):
        if isinstance(rc, Mapping):
            chain = rc.get("evidence_chain") or []
            recommended_sql += _extract_recommended(
                chain, ("sql", "query", "recommended_sql"), ("statement", "sql", "text", "content")
            )
            recommended_logs += _extract_recommended(
                chain, ("log", "trace", "recommended_log"), ("path", "line", "text", "content")
            )

    # 修复验证七点（25 号文 §环节 9 ⑥）：bug 文档若带 fix_verification 则透传，
    # 否则用可得的 verified_at/resolved_at/repro 拼最小验证态。
    fix_verification = bug.get("fix_verification")
    if not isinstance(fix_verification, Mapping):
        fix_verification = {
            "verified_at": bug.get("verified_at"),
            "resolved_at": bug.get("resolved_at"),
            "resolved_build": bug.get("resolved_build"),
            "repro": bug.get("repro"),
            "activated_count": bug.get("activated_count"),
        }

    return {
        "code": bug.get("code"),
        "title": bug.get("title"),
        "severity": bug.get("severity"),
        "status": bug.get("status"),
        "module": bug.get("module"),
        "symptom": bug.get("symptom"),
        "fingerprint": bug.get("fingerprint"),
        "related_bug_ids": list(bug.get("related_bug_ids") or []),
        "root_causes": root_causes,
        "recommended_sql": recommended_sql,
        "recommended_logs": recommended_logs,
        "fix_verification": fix_verification,
    }


def developer_view(report: Any) -> dict[str, Any]:
    """研发视图——根因定位 / 关联代码 / 推荐 SQL / 关联历史 bug / 修复验证。

    含：``conclusion``（结论先行）+ ``bugs``（根因+SQL+日志+关联 bug+修复验证）
    + ``failing_steps``（失败用例的 expected/actual/diff，辅助根因定位）。
    """
    snapshot, row = _coerce_snapshot(report)
    view = _base_view(snapshot, row, "developer")

    view["bugs"] = [
        _developer_bug(bug)
        for bug in (snapshot.get("bugs") or [])
        if isinstance(bug, Mapping)
    ]

    # 失败步骤的诊断面（actual/diff）——研发定位根因用
    failing_steps: list[dict[str, Any]] = []
    for case in snapshot.get("cases") or []:
        if not _case_is_failure(case):
            continue
        for step in (case.get("steps") or []):
            failing_steps.append({
                "case_code": case.get("case_code"),
                "title": case.get("title"),
                "scenario": case.get("scenario"),
                "step_key": step.get("step_key"),
                "seq": step.get("seq"),
                "type": step.get("type"),
                "status": step.get("status"),
                "expected": step.get("expected"),
                "actual": step.get("actual"),
                "diff": step.get("diff"),
            })
    view["failing_steps"] = failing_steps
    return view


# ── 3) 产品/需求视图：需求覆盖 / 没测的 / 风险清单 / 业务结论 ──────
def product_view(report: Any) -> dict[str, Any]:
    """产品/需求视图——需求覆盖、风险清单、业务结论（不关心技术细节）。

    含：``conclusion``（结论先行，业务摘要即 headline）+ ``requirements_coverage``
    + ``risks``（失败用例按需求归集）。**不含** steps/evidence/SQL/日志/diff。
    """
    snapshot, row = _coerce_snapshot(report)
    view = _base_view(snapshot, row, "product")

    reqs = list(snapshot.get("requirements") or [])
    covered = sum(1 for r in reqs if isinstance(r, Mapping) and r.get("coverage") == "covered")
    partial = sum(1 for r in reqs if isinstance(r, Mapping) and r.get("coverage") == "partial")
    not_covered = sum(1 for r in reqs if isinstance(r, Mapping) and r.get("coverage") == "not_covered")
    view["requirements_coverage"] = {
        "total": len(reqs),
        "covered": covered,
        "partial": partial,
        "not_covered": not_covered,
        "uncovered": [
            {
                "requirement_id": r.get("requirement_id") or r.get("id"),
                "title": r.get("title"),
                "coverage": r.get("coverage"),
            }
            for r in reqs
            if isinstance(r, Mapping) and r.get("coverage") in ("not_covered", "partial", None)
        ],
    }

    # 风险清单——失败/错误用例挂到需求维度（产品能看懂「哪个需求挂了」）
    risks: list[dict[str, Any]] = []
    for case in snapshot.get("cases") or []:
        if not _case_is_failure(case):
            continue
        risks.append({
            "case_code": case.get("case_code"),
            "title": case.get("title"),
            "scenario": case.get("scenario"),
            "status": case.get("status"),
            "requirement_id": case.get("requirement_id"),
        })
    view["risks"] = risks

    # 业务结论——复用 conclusion.headline，产品视角不重复技术数
    view["business_summary"] = view["conclusion"]["headline"]
    return view


# ── 4) 管理者视图：结论先行 Go/No-Go / 失败率 / 置信度 / 趋势 ─────
def _bug_severity_rollup(bugs: list[Any]) -> dict[str, int]:
    """按 severity 汇总 bug 数（管理者只看规模，不看明细）。"""
    rollup: dict[str, int] = {}
    for bug in bugs:
        if not isinstance(bug, Mapping):
            continue
        sev = str(bug.get("severity") or "(未填)")
        rollup[sev] = rollup.get(sev, 0) + 1
    return dict(sorted(rollup.items(), key=lambda kv: (-kv[1], kv[0])))


def manager_view(report: Any) -> dict[str, Any]:
    """管理者视图——结论先行：Go/No-Go、失败率、置信度、趋势、一句摘要。

    含：``conclusion``（结论先行，顶层即 verdict/go_no_go/headline/pass_rate/
    fail_rate/confidence）+ ``bug_severity_rollup`` + ``trend``。**不含** 步骤细节。
    """
    snapshot, row = _coerce_snapshot(report)
    view = _base_view(snapshot, row, "manager")

    # 趋势——v1 snapshot 无此区，透传调用方注入的 trend（历史 pass_rate 序列等）
    trend = snapshot.get("trend") if isinstance(snapshot.get("trend"), Mapping) else {}
    view["trend"] = dict(trend)

    # bug 规模 rollup——管理者看缺陷规模与严重度分布，不看明细
    view["bug_severity_rollup"] = _bug_severity_rollup(list(snapshot.get("bugs") or []))

    # 顶层冗余字段，方便管理者一眼扫：把 conclusion 的几关键数提到视图顶层
    concl = view["conclusion"]
    view["go_no_go"] = concl["go_no_go"]
    view["headline"] = concl["headline"]
    view["pass_rate"] = concl["pass_rate"]
    view["fail_rate"] = concl["fail_rate"]
    view["confidence"] = concl["confidence"]
    return view


# ── 派发入口 ─────────────────────────────────────────────────────
def audience_view(report: Any, audience: str) -> dict[str, Any]:
    """按受众派发——25 号文 §环节 10 四类受众视图的唯一入口。

    Args:
        report: ``TestReport`` 行或其 dict（snapshot 本体 / 带 ``snapshot`` 键的行）。
        audience: ``executor`` / ``developer`` / ``product`` / ``manager``。

    Returns:
        受众视图 dict，``conclusion`` 永远在首位（结论先行）。

    Raises:
        ValueError: ``audience`` 不在四类受众内。
    """
    if audience not in AUDIENCES:
        raise ValueError(
            f"audience={audience!r} 不在四类受众 {list(AUDIENCES)} 内"
        )
    if audience == "executor":
        return executor_view(report)
    if audience == "developer":
        return developer_view(report)
    if audience == "product":
        return product_view(report)
    return manager_view(report)
