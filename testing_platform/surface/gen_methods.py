"""门①/门② 能力面方法（25 号文 v4.1 门①②的 MCP/HTTP 入口）。

八个方法：
- ``testpoint.confirm`` —— 门①：选中的测试点提案 → 正式表（gen_run 编排器动作）
- ``proposal.list``     —— 门①②评审页数据源：按 run/kind/状态列提案
- ``genrun.get``        —— 进度轮询：run.status + progress（前端唯一渲染源）
- ``case.verdict``      —— 门②四判决：keep|modify|reject|defer 写回提案 status
- ``case.import``       —— 入库：门②判完的提案 → 正式表（编排器 finalize_import）
- ``coverage.summary``  —— 覆盖矩阵速览：confirmed 测试点的 轴×端 计数与空格
- ``case.reject_sink``  —— 门② REJECT 原因 → 问题库回流（§环节11 统一 sink）
- ``case.mark_golden``  —— 风格锚标记：confirm_ref.golden=true（§环节11，不新增列）

设计纪律：只接 generation.orchestrator，不复制逻辑；confirm 走编排器的幂等实现。
末尾两方法（缺陷链 P2）直连 generation.defect_flow 纯函数 + 模型，不碰编排器。
"""
from __future__ import annotations

import logging

from typing import Any, Mapping

from ..generation import orchestrator as gen_orch
from ..generation.models import TestGenRun, TestProposal, TestTestpoint
from ..generation.state import GEN_STATUS_LABELS, RUN_STATUS_LABELS
from .methods import MethodError, _uuid_of, register_method

logger = logging.getLogger(__name__)


# ── testpoint.confirm —— 门①（写）────────────────────────────────
@register_method(
    "testpoint.confirm",
    kind="write",
    description="**门①·需求确认**：选中的测试点提案 → 正式 ``mc_test_testpoint``"
                "（status=confirmed，TP- 号段）。幂等：重复确认不重复建行。"
                "gen_run 状态与 progress 随动。",
    params_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "gen_run UUID"},
            "proposal_ids": {"type": "array", "items": {"type": "string"},
                              "description": "要确认的测试点提案 id 列表"},
            "confirmed_by": {"type": "string", "description": "确认人 UUID"},
        },
        "required": ["run_id", "proposal_ids", "confirmed_by"],
    },
    returns_schema={
        "type": "object",
        "properties": {"confirmed": {"type": "integer"}, "skipped": {"type": "array"}},
    },
)
async def testpoint_confirm(params: Mapping[str, Any]) -> dict:
    run = await _get_run(params["run_id"])
    res = await gen_orch.confirm_testpoints(
        run,
        proposal_ids=params["proposal_ids"],
        decided_by=_uuid_of(params["confirmed_by"], field_name="confirmed_by"),
    )
    # 门①全确认后自动推进（用例细化→评审→停门②）；未全确认则不动。
    # 需要 LLM 时用 run.config 里的 api_key_id 重新解析（config 只存 id，不存明文）。
    if res.get("confirmed"):
        try:
            from ..agent_runtime import build_generation_config, run_chat as _dsh_chat
            from ..generation.advance import advance_after_gate1
            from ..generation.models import TestGenRun

            fresh = await TestGenRun.get(id=run.id)
            cfg_cfg = dict(fresh.config or {})
            gen_key = None
            if cfg_cfg.get("api_key_id"):
                from .chat import _resolve_api_key

                gen_key, _n = await _resolve_api_key(
                    int(cfg_cfg["api_key_id"]), str(fresh.created_by or "")
                )
            dsh_config = build_generation_config(api_key=gen_key, model=cfg_cfg.get("model"))

            async def _api_lookup(api_id: str):
                from ..assets.models import TestApi

                row = await TestApi.get_or_none(id=_uuid_of(api_id, field_name="api_id"))
                if row is None:
                    return None
                return {"id": str(row.id), "params": row.params or [],
                        "request_body": row.request_body or {}}

            async def _rules_provider(_r):
                from ..generation.models import TestRule

                rows = await TestRule.filter(project_id=run.project_id, kind="hard",
                                             enforcement="active").values()
                return list(rows)

            import asyncio

            asyncio.create_task(advance_after_gate1(
                fresh, dsh_chat=_dsh_chat, config=dsh_config,
                api_lookup=_api_lookup, rules_provider=_rules_provider,
            ))
        except Exception:  # noqa: BLE001 — 推进失败不阻断门①确认本身（run.decisions 由 advance 落）
            logger.exception("[testpoint.confirm] auto-advance skipped")
    return res


# ── proposal.list —— 门①②评审页数据源（读）──────────────────────
@register_method(
    "proposal.list",
    kind="read",
    description="**暂存区提案列表**（门①②工作台数据源）：按 run/kind/状态过滤，"
                "带 payload 全文、评审态、查重命中、双评意见。分页 limit/offset。",
    params_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "gen_run UUID（必填）"},
            "kind": {"type": "string", "enum": ["testpoint", "case", "dataset"]},
            "status": {"type": "string", "enum": ["proposed", "keep", "modify", "reject", "defer"]},
            "review_state": {"type": "string"},
            "limit": {"type": "integer", "description": "默认 50，最大 200"},
            "offset": {"type": "integer", "description": "默认 0"},
        },
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}, "count": {"type": "integer"},
                       "total": {"type": "integer"}},
    },
)
async def proposal_list(params: Mapping[str, Any]) -> dict:
    run = await _get_run(params["run_id"])
    qs = TestProposal.filter(run_id=run.id)
    if params.get("kind"):
        qs = qs.filter(kind=params["kind"])
    if params.get("status"):
        qs = qs.filter(status=params["status"])
    if params.get("review_state"):
        qs = qs.filter(review_state=params["review_state"])
    limit = min(int(params.get("limit") or 50), 200)
    offset = max(int(params.get("offset") or 0), 0)
    total = await qs.count()
    rows = await qs.order_by("created_at").offset(offset).limit(limit)
    return {"items": [_proposal_doc(r) for r in rows], "count": len(rows), "total": total}


# ── genrun.get —— 进度轮询（读）──────────────────────────────────
@register_method(
    "genrun.get",
    kind="read",
    description="**生成任务进度**（前端唯一渲染源）：重算并返回 run.status + progress"
                "（§3.3 结构：requirements/testpoints/cases/coverage/pending/budget）。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string"}, "status": {"type": "string"},
            "status_label": {"type": "string"}, "run_type": {"type": "string"},
            "scope": {"type": "object"},
            "config": {"type": "object"},
            "progress": {"type": "object"},
        },
    },
)
async def genrun_get(params: Mapping[str, Any]) -> dict:
    run = await _get_run(params["run_id"])
    progress = await gen_orch.refresh_progress(run)
    return {
        "run_id": str(run.id),
        "status": run.status,
        "status_label": RUN_STATUS_LABELS.get(run.status, run.status),
        "run_type": run.run_type,
        "scope": dict(run.scope or {}),
        "config": {k: v for k, v in (run.config or {}).items() if k != "api_key"},
        "gen_status_labels": dict(GEN_STATUS_LABELS),
        "progress": progress,
    }


# ── genrun.list —— 生成任务历史（读）──────────────────────────────
@register_method(
    "genrun.list",
    kind="read",
    description="**生成任务历史**：按项目列 gen_run（created_at 倒序）。前端"
                "「生成任务」独立列表页数据源；不带 progress（列表只要状态徽章，"
                "详情用 genrun.get 轮询）。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID（缺省=全部项目）"},
            "limit": {"type": "integer", "default": 50},
            "offset": {"type": "integer", "default": 0},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {
            "items": {"type": "array"},
            "count": {"type": "integer"},
            "total": {"type": "integer"},
        },
    },
)
async def genrun_list(params: Mapping[str, Any]) -> dict:
    limit = max(1, min(int(params.get("limit") or 50), 200))
    offset = max(0, int(params.get("offset") or 0))
    qs = TestGenRun.all()
    project_id = str(params.get("project_id") or "").strip()
    if project_id:
        qs = qs.filter(project_id=_uuid_of(project_id, field_name="project_id"))
    total = await qs.count()
    rows = await qs.order_by("-created_at").offset(offset).limit(limit)
    items = []
    for r in rows:
        progress = dict(r.progress or {})
        items.append({
            "run_id": str(r.id),
            "status": r.status,
            "status_label": RUN_STATUS_LABELS.get(r.status, r.status),
            "run_type": r.run_type,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            # 范围摘要：需求 id 数（列表页「范围」列，不带正文）
            "requirement_count": len((r.scope or {}).get("requirement_ids") or []),
            # 进度概要（列表徽章用：三段计数，详情页才拉全量）
            "progress_summary": {
                k: progress.get(k) for k in ("requirements", "testpoints", "cases")
                if k in progress
            },
        })
    return {"items": items, "count": len(items), "total": total}


# ── case.verdict —— 门②四判决（写）──────────────────────────────
_PROPOSAL_VERDICTS: tuple[str, ...] = ("keep", "modify", "reject", "defer")


@register_method(
    "case.verdict",
    kind="write",
    description="**门②·用例判决**：对 case/dataset 提案写回四判决之一"
                "（keep|modify|reject|defer）。``modify`` 必带 ``edited_payload``"
                "（就地替换 payload）；``reject`` 必带 ``reason``（存 "
                "``provenance.reject_reason``）；``defer`` 可带 ``question_id``"
                "（答完自动重新入评审）。支持单提案或批量 ``proposal_ids``。",
    params_schema={
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string", "description": "单提案 UUID（与 proposal_ids 二选一）"},
            "proposal_ids": {"type": "array", "items": {"type": "string"},
                             "description": "批量提案 UUID 列表（与 proposal_id 二选一）"},
            "verdict": {"type": "string", "enum": list(_PROPOSAL_VERDICTS),
                        "description": "四判决之一"},
            "edited_payload": {"type": "object", "description": "verdict=modify 时的替换 payload"},
            "reason": {"type": "string", "description": "verdict=reject 时的必带理由"},
            "question_id": {"type": "string", "description": "verdict=defer 时挂的问题 UUID"},
            "decided_by": {"type": "string", "description": "判决人 UUID"},
        },
        "required": ["verdict"],
    },
    returns_schema={
        "type": "object",
        "properties": {"updated": {"type": "integer"}, "skipped": {"type": "array"}},
    },
)
async def case_verdict(params: Mapping[str, Any]) -> dict:
    verdict = str(params["verdict"]).strip().lower()
    if verdict not in _PROPOSAL_VERDICTS:
        raise MethodError(
            f"verdict 非法：{verdict!r}（可选：{list(_PROPOSAL_VERDICTS)}）",
            code="invalid_params",
        )
    edited_payload = params.get("edited_payload")
    if verdict == "modify" and not isinstance(edited_payload, Mapping):
        raise MethodError("verdict=modify 必带 edited_payload（对象）", code="invalid_params")
    reason = str(params.get("reason") or "").strip()
    if verdict == "reject" and not reason:
        raise MethodError("verdict=reject 必带 reason（拒绝理由）", code="invalid_params")
    question_id = (
        _uuid_of(params["question_id"], field_name="question_id")
        if params.get("question_id") else None
    )
    decided_by = (
        _uuid_of(params["decided_by"], field_name="decided_by")
        if params.get("decided_by") else None
    )

    ids = list(params.get("proposal_ids") or [])
    if params.get("proposal_id"):
        ids.append(params["proposal_id"])
    if not ids:
        raise MethodError("proposal_id / proposal_ids 至少给一个", code="invalid_params")

    updated, skipped = 0, []
    for pid in ids:
        prop = await TestProposal.get_or_none(id=_uuid_of(pid, field_name="proposal_ids"))
        if prop is None:
            skipped.append({"id": str(pid), "reason": "提案不存在"})
            continue
        if prop.status not in ("proposed", "modify"):
            skipped.append({"id": str(pid), "reason": f"状态不可判决（{prop.status}）"})
            continue
        provenance = dict(prop.provenance or {})
        if verdict == "modify":
            prop.payload = dict(edited_payload)
        if verdict == "reject":
            provenance["reject_reason"] = reason
        if verdict == "defer":
            prop.question_id = question_id
        if decided_by is not None:
            provenance["decided_by"] = str(decided_by)
        prop.provenance = provenance
        prop.status = "modify" if verdict == "modify" else verdict
        await prop.save()
        updated += 1
    return {"updated": updated, "skipped": skipped}


# ── case.import —— 入库（execute）────────────────────────────────
@register_method(
    "case.import",
    kind="execute",
    description="**入库**：该 run 门②判完（keep/modify）的 case/dataset 提案 → 正式表"
                "（``mc_test_case`` TC- 号段 / ``mc_test_dataset`` DS- 号段），"
                "数据集引用回填 ``dataset_id``。幂等：已入库提案跳过。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "gen_run UUID"}},
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"cases_imported": {"type": "integer"},
                       "datasets_imported": {"type": "integer"},
                       "skipped": {"type": "array"}},
    },
)
async def case_import(params: Mapping[str, Any]) -> dict:
    run = await _get_run(params["run_id"])
    return await gen_orch.finalize_import(run)


# ── coverage.summary —— 覆盖矩阵速览（读）────────────────────────
@register_method(
    "coverage.summary",
    kind="read",
    description="**覆盖矩阵速览**：该 run 范围内 confirmed 测试点的 轴×端 计数矩阵"
                "（``{axis: {surface: count}}``）+ gap（只数已出现轴 × 已声明端）"
                "与 lit 总数。门①后/门②前看覆盖缺口用。",
    params_schema={
        "type": "object",
        "properties": {"run_id": {"type": "string", "description": "gen_run UUID"}},
        "required": ["run_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "matrix": {"type": "object",
                       "description": "{axis: {surface: count}}"},
            "lit": {"type": "integer", "description": "矩阵点亮总数（Σ count）"},
        },
    },
)
async def coverage_summary(params: Mapping[str, Any]) -> dict:
    run = await _get_run(params["run_id"])
    scope_ids = (run.scope or {}).get("requirement_ids") or []
    qs = TestTestpoint.filter(project_id=run.project_id, status="confirmed")
    if scope_ids:
        qs = qs.filter(requirement_id__in=[_uuid_of(x, field_name="scope") for x in scope_ids])
    rows = await qs.values("coverage_axis", "surfaces", "code")

    matrix: dict[str, dict[str, int]] = {}
    axes_seen: set[str] = set()
    surfaces_seen: set[str] = set()
    lit = 0
    for row in rows:
        axes = [str(a).strip() for a in (row.get("coverage_axis") or []) if str(a).strip()]
        surfaces = [str(s).strip() for s in (row.get("surfaces") or []) if str(s).strip()]
        surfaces_seen.update(surfaces)
        for axis in axes:
            axes_seen.add(axis)
            bucket = matrix.setdefault(axis, {})
            for surface in surfaces:
                bucket[surface] = bucket.get(surface, 0) + 1
                lit += 1
    # gap：已出现轴 × 已声明端里没亮过的格
    gaps: dict[str, list[str]] = {}
    for axis in sorted(axes_seen):
        missing = sorted(surfaces_seen - set(matrix.get(axis, {})))
        if missing:
            gaps[axis] = missing
    return {"matrix": matrix, "lit": lit, "gap": gaps,
            "surfaces": sorted(surfaces_seen)}


# ── helpers ──────────────────────────────────────────────────────
async def _get_run(run_id: Any) -> TestGenRun:
    run = await TestGenRun.get_or_none(id=_uuid_of(run_id, field_name="run_id"))
    if run is None:
        raise MethodError(f"gen_run 不存在：{run_id!r}", code="not_found")
    return run


def _proposal_doc(r: TestProposal) -> dict:
    return {
        "id": str(r.id),
        "run_id": str(r.run_id),
        "kind": r.kind,
        "payload": dict(r.payload or {}),
        "source_agent": r.source_agent,
        "surface": r.surface,
        "testpoint_id": str(r.testpoint_id) if r.testpoint_id else None,
        "evidence_class": r.evidence_class,
        "review_state": r.review_state,
        "review_round": r.review_round,
        "score": r.score,
        "score_breakdown": dict(r.score_breakdown or {}),
        "reviews": dict(r.reviews or {}),
        "dup_of_case_id": str(r.dup_of_case_id) if r.dup_of_case_id else None,
        "dup_score": r.dup_score,
        "dup_verdict": r.dup_verdict,
        "applied_rules": list(r.applied_rules or []),
        "rewrite_of": str(r.rewrite_of) if r.rewrite_of else None,
        "version": r.version,
        "slot_key": r.slot_key,
        "status": r.status,
        "provenance": dict(r.provenance or {}),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# ── testpoint.list —— 已确认测试点（导图/分组数据源，读）──────────
@register_method(
    "testpoint.list",
    kind="read",
    description="**已确认测试点列表**（导图与用例分组数据源）：三选一入口——``project_id``"
                "（项目级，测试点页用）/ ``run_id`` / ``requirement_ids``。带端归属、覆盖轴、批次。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID（项目级列表；与 run_id/requirement_ids 三选一）"},
            "run_id": {"type": "string", "description": "gen_run UUID（与 project_id/requirement_ids 三选一）"},
            "requirement_ids": {"type": "array", "items": {"type": "string"},
                                "description": "按需求过滤（可空=该 run 全部）"},
            "status": {"type": "string", "enum": ["confirmed", "generated"]},
            "limit": {"type": "integer", "description": "默认 200，最大 500"},
        },
    },
    returns_schema={
        "type": "object",
        "properties": {"items": {"type": "array"}, "count": {"type": "integer"}},
    },
)
async def testpoint_list(params: Mapping[str, Any]) -> dict:
    req_ids = list(params.get("requirement_ids") or [])
    project_id = None
    if params.get("project_id"):
        # 项目级入口（2026-10-22 加：测试点独立页需要；原方法只有 run_id/requirement_ids）
        project_id = _uuid_of(params["project_id"], field_name="project_id")
    elif params.get("run_id"):
        run = await _get_run(params["run_id"])
        project_id = run.project_id
        if not req_ids:
            req_ids = list((run.scope or {}).get("requirement_ids") or [])
    elif req_ids:
        # **按需求反查项目**（2026-10-22 加）：前端按 requirement_id 过滤时只会给
        # requirement_ids，没有 project_id/run_id——原实现直接报"至少给一个"，
        # 但错误信息声称 requirement_ids 单独可用，实际不可用。需求属于唯一项目，
        # 从测试点行的 requirement_id 反查出 project_id 即可（跨项目取并集）。
        uuids = [_uuid_of(x, field_name="requirement_ids[]") for x in req_ids]
        pids = await TestTestpoint.filter(requirement_id__in=uuids).values_list(
            "project_id", flat=True
        )
        unique = [str(p) for p in dict.fromkeys(pids)]
        if not unique:
            # 该需求下还没有测试点 → 空结果（不是错误）
            return {"items": [], "count": 0}
        if len(unique) == 1:
            project_id = unique[0]
        else:
            # 跨项目（罕见）→ 直接用 requirement_id 过滤，不锁项目
            rows = await TestTestpoint.filter(requirement_id__in=uuids).order_by("code").limit(
                min(int(params.get("limit") or 200), 500)
            )
            return {"items": [_testpoint_doc(r) for r in rows], "count": len(rows)}
    if project_id is None:
        raise MethodError(
            "project_id / run_id / requirement_ids 至少给一个", code="invalid_params"
        )
    qs = TestTestpoint.filter(project_id=project_id)
    if req_ids:
        qs = qs.filter(requirement_id__in=[_uuid_of(x, field_name="requirement_ids[]") for x in req_ids])
    if params.get("status"):
        qs = qs.filter(status=params["status"])
    limit = min(int(params.get("limit") or 200), 500)
    rows = await qs.order_by("code").limit(limit)
    return {"items": [_testpoint_doc(r) for r in rows], "count": len(rows)}


# ── testpoint.get —— 取单个测试点（读；测试点详情页用）───────────
@register_method(
    "testpoint.get",
    kind="read",
    description="取单个已确认测试点（业务字段全文；2026-10-22 测试点详情页新增）。",
    params_schema={
        "type": "object",
        "properties": {"testpoint_id": {"type": "string", "description": "测试点 UUID 或 TP-xxxx code"}},
        "required": ["testpoint_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}, "code": {"type": "string"}, "title": {"type": "string"}},
    },
)
async def testpoint_get(params: Mapping[str, Any]) -> dict:
    from ..generation.models import TestTestpoint as _TP

    raw = str(params["testpoint_id"]).strip()
    try:
        row = await _TP.get(id=_uuid_of(raw, field_name="testpoint_id"))
    except Exception:
        row = await _TP.get_or_none(code=raw)
    if row is None:
        raise MethodError(f"测试点不存在：{raw!r}", code="not_found")
    return _testpoint_doc(row)


def _testpoint_doc(r: TestTestpoint) -> dict:
    return {
        "id": str(r.id), "code": r.code,
        "requirement_id": str(r.requirement_id) if r.requirement_id else None,
        "title": r.title, "trigger": r.trigger,
        "sample_data": r.sample_data, "verify_result": r.verify_result,
        "impact": r.impact, "evidence": r.evidence,
        "coverage_axis": list(r.coverage_axis or []),
        "surfaces": list(r.surfaces or []),
        "surfaces_decided_by": r.surfaces_decided_by,
        "design_method": r.design_method, "batch_tier": r.batch_tier,
        "basis": r.basis, "status": r.status,
        "case_review_state": r.case_review_state,
    }


# ════════════════════════════════════════════════════════════════
# 缺陷链方法层（25 号文 v4.1 §环节9/§环节11；P2；surface/gen_methods.py 尾部）
# ════════════════════════════════════════════════════════════════

# ── case.reject_sink —— 门② REJECT 原因沉淀（写）─────────────────
@register_method(
    "case.reject_sink",
    kind="write",
    description="**门②·REJECT 原因沉淀**（§环节11 问题库回流）：读提案的拒绝理由 → "
                "用 defect_flow.make_sink_entry 造「问题库行」→ 写 ``mc_test_question``"
                "（统一 sink，origin=reject_reason 扩枚举、gate=sink 进 context）。"
                "``reason`` 缺省从提案 ``provenance.reject_reason`` 取（case.verdict "
                "reject 时落的）。幂等：同一提案同文已沉淀 → 跳过不重复写。",
    params_schema={
        "type": "object",
        "properties": {
            "proposal_id": {"type": "string", "description": "提案 UUID"},
            "reason": {"type": "string",
                       "description": "拒绝理由（缺省读提案 provenance.reject_reason）"},
            "decided_by": {"type": "string", "description": "判决人 UUID（进 context.source_ref）"},
        },
        "required": ["proposal_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "stored": {"type": "boolean"},
            "question_id": {"type": ["string", "null"],
                            "description": "沉淀行 UUID（stored=false 时 None）"},
            "skipped": {"type": "string",
                        "description": "跳过原因（stored=false 时有值）"},
        },
    },
)
async def case_reject_sink(params: Mapping[str, Any]) -> dict:
    """门② REJECT 原因 → 问题库（§环节11 统一 sink=``mc_test_question``）。"""
    from ..generation.defect_flow import make_sink_entry
    from ..quality.models import TestQuestion

    prop = await TestProposal.get_or_none(
        id=_uuid_of(params["proposal_id"], field_name="proposal_id")
    )
    if prop is None:
        raise MethodError(
            f"提案不存在：{params['proposal_id']!r}", code="not_found"
        )

    reason = str(params.get("reason") or "").strip()
    if not reason:
        provenance = dict(prop.provenance or {})
        reason = str(provenance.get("reject_reason") or "").strip()
    if not reason:
        raise MethodError(
            "reason 必填（提案 provenance 里也没有 reject_reason——先走 "
            "case.verdict verdict=reject 落理由）",
            code="invalid_params",
        )

    # 幂等：同一提案、同文 sink 行已存在 → 跳过（不重复沉淀）。
    # 按 origin 收窄（sink kind 就是 origin 值），别扫全项目 resolved 行。
    existing = await TestQuestion.filter(
        project_id=prop.project_id, origin="reject_reason",
    )
    for row in existing:
        row_ctx = dict(row.context or {})
        if (
            row_ctx.get("sink")
            and row.content == reason
            and str((row_ctx.get("source_ref") or {}).get("proposal_id") or "") == str(prop.id)
        ):
            return {"stored": False, "question_id": str(row.id),
                    "skipped": "已沉淀（幂等跳过）"}

    decided_by = params.get("decided_by")
    payload = dict(prop.payload or {})
    source_ref: dict[str, Any] = {
        "proposal_id": str(prop.id),
        "run_id": str(prop.run_id),
        "kind": prop.kind,
        "payload_title": payload.get("title"),
    }
    if decided_by:
        source_ref["decided_by"] = str(decided_by)

    entry = make_sink_entry(
        "reject_reason", reason,
        module=payload.get("module") or prop.surface,
        source_ref=source_ref,
    )
    entry.pop("gate", None)      # gate/module 只在 DDL 扩列里存在，走 ORM 创建会被
    entry.pop("module", None)    # Tortoise 静默丢弃——显式剥掉，元数据已在 context。
    entry.pop("kind", None)
    row = await TestQuestion.create(
        project_id=prop.project_id, **entry
    )
    return {"stored": True, "question_id": str(row.id), "skipped": None}


# ── question.sink —— 问题库回流通用沉淀（写，§环节11）────────────
@register_method(
    "question.sink",
    kind="write",
    description="**问题库回流通用沉淀**（§环节11 统一 sink=``mc_test_question``）：把"
                "四类写入者的原始记录批量转成 sink 行写库。``source`` ∈ gate2_reject"
                "（门② REJECT）/ gate1_reject（门①打回）/ bug_close（bug 结案）/"
                "rework_lesson（返工教训）/ pitfall / risk_scenario。条目形状经 "
                "defect_flow.make_sink_entries 归一（正文字段带别名回退，见其 docstring）。"
                "单条沉淀走 case.reject_sink；本方法是批量/全来源入口。",
    params_schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 UUID（sink 行归属）"},
            "source": {"type": "string",
                       "enum": ["gate2_reject", "gate1_reject", "bug_close",
                                "rework_lesson", "pitfall", "risk_scenario"],
                       "description": "写入者来源"},
            "items": {"type": "array", "description": "原始记录列表（每条 Mapping）"},
            "module": {"type": "string", "description": "批量兜底模块（条目缺 module 时用）"},
            "strict": {"type": "boolean", "default": True,
                       "description": "true=某条取不到正文整批抛错；false=跳过该条"},
        },
        "required": ["project_id", "source", "items"],
    },
    returns_schema={
        "type": "object",
        "properties": {"stored": {"type": "integer"}, "question_ids": {"type": "array"}},
    },
)
async def question_sink(params: Mapping[str, Any]) -> dict:
    """批量转 sink 行 → 写 ``mc_test_question``（§环节11 问题库回流）。"""
    from ..generation.defect_flow import make_sink_entries
    from ..quality.models import TestQuestion

    project_id = _uuid_of(params["project_id"], field_name="project_id")
    items = params.get("items") or []
    if not isinstance(items, (list, tuple)):
        raise MethodError("items 必须是数组", code="invalid_params")

    try:
        entries = make_sink_entries(
            str(params["source"]), list(items),
            module=params.get("module"),
            strict=bool(params.get("strict", True)),
        )
    except ValueError as exc:
        raise MethodError(str(exc), code="invalid_params") from exc

    ids = []
    for entry in entries:
        # gate/module/kind 只在 DDL 扩列/context 里，走 ORM 创建会被 Tortoise 静默
        # 丢弃——显式剥掉（与 case.reject_sink 同口径，元数据已在 context）。
        entry.pop("gate", None)
        entry.pop("module", None)
        entry.pop("kind", None)
        row = await TestQuestion.create(project_id=project_id, **entry)
        ids.append(str(row.id))
    return {"stored": len(ids), "question_ids": ids}


# ── explore.plan —— 探索门判定（读，§环节7）──────────────────────
@register_method(
    "explore.plan",
    kind="read",
    description="**探索门判定**（§环节7「按内容分，不按端」）：判这条用例要不要过"
                "探索门——api 框架类（参数类型/必填/格式，request+check 机械翻译可跑）"
                "不过门；字段规则/接口逻辑/接口安全过门；web/app 全部过门。返回"
                "``{required, reason, content_class}``；``api_doc_id`` 给了时附带"
                "api 试调清单（断言字段/业务码真实存在性）。",
    params_schema={
        "type": "object",
        "properties": {
            "case_payload": {"type": "object", "description": "用例/提案 payload"},
            "surface": {"type": "string", "description": "api|web|app|security（缺省读 payload.surface）"},
            "api_doc_id": {"type": "string", "description": "mc_test_api 行 UUID（给了附带试调清单）"},
        },
        "required": ["case_payload"],
    },
    returns_schema={
        "type": "object",
        "properties": {"required": {"type": "boolean"}, "reason": {"type": "string"},
                       "content_class": {"type": "string"}},
    },
)
async def explore_plan(params: Mapping[str, Any]) -> dict:
    """探索门判定（纯判定；``api_doc_id`` 给了附带试调清单）。"""
    from ..generation.explore_gate import gate_required, trial_call_plan

    payload = params.get("case_payload")
    if not isinstance(payload, Mapping):
        raise MethodError("case_payload 必须是对象", code="invalid_params")
    out = gate_required(payload, str(params.get("surface") or ""))
    api_doc_id = str(params.get("api_doc_id") or "").strip()
    if api_doc_id:
        from ..assets.models import TestApi

        row = await TestApi.get_or_none(id=_uuid_of(api_doc_id, field_name="api_doc_id"))
        if row is None:
            raise MethodError(f"接口不存在：{api_doc_id!r}", code="not_found")
        api_doc = {
            "params": row.params if isinstance(row.params, (list, dict)) else [],
            "request_body": row.request_body if isinstance(row.request_body, dict) else {},
        }
        out["trial_call_plan"] = trial_call_plan(payload, api_doc)
    return out


# ── explore.record —— 探索门状态记录（写，§环节7）────────────────
@register_method(
    "explore.record",
    kind="write",
    description="**探索门状态记录**（§环节7）：把一次探索结果落到用例 "
                "``verified_by_run`` 列（未探索/已探索/探索失败；未过门不进波次）。"
                "``outcome``=pass→已探索、fail→探索失败、其他→保持原状态。幂等："
                "同值重复写不改。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "description": "用例 UUID"},
            "outcome": {"type": "string", "description": "pass | fail | 其他（保持原状态）"},
            "note": {"type": "string", "description": "备注（进 confirm_ref.explore_note）"},
        },
        "required": ["case_id", "outcome"],
    },
    returns_schema={
        "type": "object",
        "properties": {"case_id": {"type": "string"},
                       "verified_by_run": {"type": "string"},
                       "changed": {"type": "boolean"}},
    },
)
async def explore_record(params: Mapping[str, Any]) -> dict:
    """探索结果 → ``mc_test_case.verified_by_run``（§环节7 状态列）。"""
    from ..assets.models import TestCase
    from ..generation.explore_gate import next_verify_state

    case = await TestCase.get_or_none(
        id=_uuid_of(params["case_id"], field_name="case_id")
    )
    if case is None:
        raise MethodError(f"用例不存在：{params['case_id']!r}", code="not_found")

    new_state = next_verify_state(case.verified_by_run, str(params.get("outcome") or ""))
    note = str(params.get("note") or "").strip()
    changed = (case.verified_by_run or "unexplored") != new_state or bool(note)
    if changed:
        confirm_ref = dict(case.confirm_ref or {})
        if note:
            confirm_ref["explore_note"] = note
        case.verified_by_run = new_state
        case.confirm_ref = confirm_ref
        await case.save(update_fields=["verified_by_run", "confirm_ref", "updated_at"])
    return {"case_id": str(case.id), "verified_by_run": new_state, "changed": changed}


# ── case.mark_golden —— 风格锚标记（写）──────────────────────────
@register_method(
    "case.mark_golden",
    kind="write",
    description="**风格锚标记**（§环节11「风格锚回灌」）：评审 keep 且分≥85 的用例，"
                "``mc_test_case`` 打 golden 标记——**不新增列**，落在 "
                "``confirm_ref.golden=true``（JSONB 现有列）+ ``origin`` 不动。"
                "下一轮同模块生成时子 agent「相关历史」切片优先召回 golden 用例"
                "（先查重再仿写）。幂等：已标记再标 = 不改（仍返回 marked=true）。",
    params_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "description": "用例 UUID"},
            "marked_by": {"type": "string", "description": "标记人 UUID"},
            "unmark": {"type": "boolean", "default": False,
                       "description": "true=撤掉 golden 标记"},
        },
        "required": ["case_id"],
    },
    returns_schema={
        "type": "object",
        "properties": {
            "marked": {"type": "boolean", "description": "标记后的 golden 状态"},
            "changed": {"type": "boolean", "description": "本次是否真的改了行（幂等=false）"},
        },
    },
)
async def case_mark_golden(params: Mapping[str, Any]) -> dict:
    """``confirm_ref.golden=true`` 方式打风格锚标记（不新增列）。"""
    from ..assets.models import TestCase

    case = await TestCase.get_or_none(
        id=_uuid_of(params["case_id"], field_name="case_id")
    )
    if case is None:
        raise MethodError(
            f"用例不存在：{params['case_id']!r}", code="not_found"
        )

    unmark = bool(params.get("unmark"))
    confirm_ref = dict(case.confirm_ref or {})
    currently = bool(confirm_ref.get("golden"))
    if currently == (not unmark):
        # 幂等：已是目标态不改行（changed=false，marked 返回当前状态）
        return {"marked": currently, "changed": False}

    if unmark:
        confirm_ref.pop("golden", None)
    else:
        confirm_ref["golden"] = True
        if params.get("marked_by"):
            confirm_ref["golden_by"] = str(params["marked_by"])
    case.confirm_ref = confirm_ref
    await case.save(update_fields=["confirm_ref", "updated_at"])
    return {"marked": bool(confirm_ref.get("golden")), "changed": True}
