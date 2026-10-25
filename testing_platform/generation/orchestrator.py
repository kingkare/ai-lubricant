"""生成环节编排器（25 号文 v4.1 §1.2 / §环节0-4 / P0）。

职责边界（严格）：

- **本模块只做编排**：建 gen_run → 驱动状态机 → 落暂存提案 → 停在门；
  具体的 AI 调用（需求分析、提点）由调用方注入 ``llm_call``（async callable），
  本模块不 import LLM/agent 运行时。
- **不写正式表**：测试点/用例/数据集一律写 ``mc_test_proposal``；正式表只在门确认
  动作（``confirm_testpoints`` / ``confirm_cases``）里由本模块写。
- **不 import surface/methods.py**（热区，并发会话在改）；需要的能力（api_lookup、
  通知）全部注入。

P0 编排入口：

- :func:`start_run` —— 建 gen_run（pending），返回 run 文档
- :func:`run_analysis` —— 逐需求：调 LLM 出 analysis → validate_analysis（四标准）
  → 不过记 failed 停在"分析未过"；过了写 requirement.analysis + 状态推进
- :func:`run_requirement_review` —— 门⓪：调 LLM 出 review → validate_review →
  建 question 行 → 状态停在 req_reviewing（等人确认）
- :func:`run_point_proposal` —— 提点：调 LLM 出测试点 → 写 proposal(kind=testpoint)
  → 状态 points_proposed（门①）
- :func:`confirm_testpoints` —— 门①：proposal→confirmed 复制进 mc_test_testpoint
- :func:`refresh_progress` —— 重算 run.progress（调 state.compute_progress）

所有函数接 ``conn``/ORM 走 Tortoise 模型；测试用注入的 fake 数据走纯函数分支。
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from . import state as _state
from .models import (
    QUESTION_GATES,
    TestCoverageCell,
    TestGenRun,
    TestProposal,
    TestTestpoint,
)
from .req_analysis import validate_analysis
from .req_review import gate_review, validate_review

# GEN_STATUSES（需求级 12 态）定义在 state；TestQuestion（现库表，本包只扩列）在 quality。
GEN_STATUSES = _state.GEN_STATUSES

#: LLM 调用签名：prompt -> 解析后的 JSON dict（失败抛异常）。
LLMCall = Callable[[str], Awaitable[Any]]


# ── 工具 ─────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_uuid(value: Any) -> _uuid.UUID:
    return value if isinstance(value, _uuid.UUID) else _uuid.UUID(str(value))


async def _save_progress(run: TestGenRun) -> None:
    """重算并落 run.progress（所有写操作末尾调一次，保证进度与事实同步）。"""
    reqs = await _load_requirement_statuses(run)
    proposals = await TestProposal.filter(run_id=run.id).values(
        "kind", "status", "review_state", "reviews", "testpoint_id"
    )
    cells = await TestCoverageCell.filter(project_id=run.project_id).values("state")
    from ..quality.models import TestQuestion  # 现库表（quality，已扩列；本地 import 防启动环）

    questions: list[dict] = []
    if await _table_exists("mc_test_question"):
        questions = list(await TestQuestion.filter(project_id=run.project_id).values(
            "status", "round_no"
        ))
    run.progress = _state.compute_progress(
        requirements=reqs,
        proposals=list(proposals),
        cells=list(cells),
        questions=list(questions),
        budget=dict(run.budget or {}),
    )
    await run.save(update_fields=["progress", "updated_at"])


async def _table_exists(name: str) -> bool:
    from tortoise import Tortoise

    try:
        rows = await Tortoise.get_connection("user_platform").execute_query_dict(
            "SELECT 1 FROM information_schema.tables WHERE table_name = $1", [name]
        )
        return bool(rows)
    except Exception:  # noqa: BLE001 — sqlite 无 information_schema
        try:
            await Tortoise.get_connection("user_platform").execute_query_dict(
                f"PRAGMA table_info({name})"
            )
            return True
        except Exception:  # noqa: BLE001
            return False


async def _load_requirement_statuses(run: TestGenRun) -> list[dict]:
    """读需求 gen_status（现库列；未上线时回落"未开始"）。"""
    from ..assets.models import TestRequirement

    scope_ids = (run.scope or {}).get("requirement_ids") or []
    qs = TestRequirement.filter(project_id=run.project_id)
    if scope_ids:
        qs = qs.filter(id__in=[_as_uuid(x) for x in scope_ids])
    rows = await qs.values("id", "gen_status")
    return [{"id": str(r["id"]), "gen_status": r.get("gen_status") or "not_started"} for r in rows]


async def _set_requirement_status(requirement_id: Any, gen_status: str) -> None:
    from ..assets.models import TestRequirement

    await TestRequirement.filter(id=_as_uuid(requirement_id)).update(
        gen_status=gen_status
    )


async def _advance_run(run: TestGenRun, target: str) -> None:
    """状态机推进（非法转换抛错，保证 run 不会跳态）。"""
    if run.status != target:
        _state.assert_transition(run.status, target)
        run.status = target
        await run.save(update_fields=["status", "updated_at"])


def _requirement_text(req: Mapping[str, Any]) -> str:
    """需求行 → 分析提示词输入的纯文本。

    真 bug 修复（e2e 实测发现）：content 是 Tiptap dict（非 str），直接传会让提示词
    嵌 repr(dict)——模型拿不到可读正文。口径：description（纯文本投影）优先；
    缺失时递归抽 Tiptap text 节点；再拼 acceptance_criteria（[{id,text}] → "AC-x: text"）。
    """
    parts: list[str] = []
    desc = req.get("description")
    if isinstance(desc, str) and desc.strip():
        parts.append(desc)
    content = req.get("content")
    if isinstance(content, dict):
        def walk(node: Any) -> None:
            if isinstance(node, Mapping):
                if node.get("type") == "text" and node.get("text"):
                    parts.append(str(node["text"]))
                for child in node.get("content") or []:
                    walk(child)
        if not desc:
            walk(content)
            if parts:
                parts.append("")  # 段落分隔
    elif isinstance(content, str) and content.strip():
        parts.append(content)
    for ac in req.get("acceptance_criteria") or []:
        if isinstance(ac, Mapping):
            txt = ac.get("text") or ac.get("expected") or ""
            aid = ac.get("id") or ""
            if txt:
                parts.append(f"AC {aid}: {txt}" if aid else str(txt))
        elif ac:
            parts.append(str(ac))
    return "\n".join(p for p in parts if p).strip()


# ── 入口 ─────────────────────────────────────────────────────────
async def start_run(
    *,
    project_id: Any,
    created_by: Any,
    version_id: Any = None,
    run_type: str = "full",
    scope: Optional[Mapping[str, Any]] = None,
    parent_run_id: Any = None,
    config: Optional[Mapping[str, Any]] = None,
    budget: Optional[Mapping[str, Any]] = None,
) -> TestGenRun:
    """建 gen_run（pending）。``scope.requirement_ids`` 缺省=版本全部需求。"""
    if run_type not in _state.RUN_TYPES:
        raise ValueError(f"run_type 非法：{run_type!r}")
    return await TestGenRun.create(
        project_id=_as_uuid(project_id),
        version_id=_as_uuid(version_id) if version_id else None,
        run_type=run_type,
        parent_run_id=_as_uuid(parent_run_id) if parent_run_id else None,
        scope=dict(scope or {}),
        status="pending",
        progress={},
        budget=dict(budget or {}),
        decisions=[],
        config=dict(config or {}),
        created_by=_as_uuid(created_by),
    )


async def run_analysis(
    run: TestGenRun,
    *,
    llm_call: LLMCall,
    prompt_builder: Callable[[str], str],
    requirements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """环节1：逐需求分析。返回 {analyzed, failed: [{requirement_id, issues}]}。

    每个需求独立——单需求失败不影响其他（失败不连坐），run 继续。
    """
    await _advance_run(run, "analyzing")
    from ..assets.models import TestRequirement

    analyzed, failed = 0, []
    for req in requirements:
        rid = req["id"]
        await _set_requirement_status(rid, "analyzing")
        try:
            raw = await llm_call(prompt_builder(_requirement_text(req)))
            analysis = raw if isinstance(raw, dict) else {}
            verdict = validate_analysis(analysis)
            if not verdict.valid:
                failed.append({
                    "requirement_id": str(rid),
                    "issues": [i.to_dict() for i in verdict.errors],
                })
                await _set_requirement_status(rid, "not_started")  # 退回，停在"分析未过"
                continue
            await TestRequirement.filter(id=_as_uuid(rid)).update(
                analysis=analysis, gen_status="analyzing"
            )
            analyzed += 1
        except Exception as exc:  # noqa: BLE001 — 单需求失败不连坐
            failed.append({"requirement_id": str(rid), "issues": [{"msg": str(exc)}]})
            await _set_requirement_status(rid, "not_started")
    run.decisions = list(run.decisions or []) + [{
        "stage": "analysis", "analyzed": analyzed, "failed": len(failed), "at": _now().isoformat(),
    }]
    await run.save(update_fields=["decisions", "updated_at"])
    await _save_progress(run)
    if analyzed:
        await _advance_run(run, "req_reviewing")
    return {"analyzed": analyzed, "failed": failed}


async def run_requirement_review(
    run: TestGenRun,
    *,
    llm_call: LLMCall,
    prompt_builder: Callable[[str], str],
    requirements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """门⓪ 前置：调 LLM 出评审报告，建问题行，停在 req_reviewing 等人。"""
    from ..quality.models import TestQuestion  # 现库表（quality）
    from ..assets.models import TestRequirement

    reviewed, blocked = 0, []
    for req in requirements:
        rid = req["id"]
        analysis = req.get("analysis") or {}
        raw = await llm_call(prompt_builder(analysis))
        review = raw if isinstance(raw, dict) else {}
        verdict = validate_review(review)
        if not verdict.valid:
            blocked.append({"requirement_id": str(rid), "issues": [i.to_dict() for i in verdict.errors]})
            continue
        questions = []
        for q in review.get("questions") or []:
            questions.append(await TestQuestion.create(
                project_id=run.project_id,
                origin="ai",
                content=str(q.get("claim") or ""),
                context={"gate": "req_review", "recommended_answer": q.get("recommended_answer")},
                status="open",
                requirement_id=_as_uuid(rid),
                recommended_answer=q.get("recommended_answer"),
                round_no=q.get("round_no"),
                gate="req_review",
            ))
        gate = gate_review(review, [
            {"status": "answered" if q.status == "resolved" else "unanswered",
             "blocking": True, "id": str(q.id)} for q in questions
        ])
        await TestRequirement.filter(id=_as_uuid(rid)).update(
            review=review, gen_status="req_reviewing"
        )
        if gate.get("passed"):
            await _set_requirement_status(rid, "req_reviewed")
        reviewed += 1
    await _save_progress(run)
    return {"reviewed": reviewed, "blocked": blocked}


async def run_point_proposal(
    run: TestGenRun,
    *,
    llm_call: LLMCall,
    prompt_builder: Callable[[str, Mapping[str, Any]], str],
    requirements: Sequence[Mapping[str, Any]],
    direction_weights: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """环节3：提点 → 写 proposal(kind=testpoint)，停在 points_proposed（门①）。

    **不写正式测试点表**——门①确认动作才写（:func:`confirm_testpoints`）。
    """
    await _advance_run(run, "points_proposed")
    created, failed = 0, []
    for req in requirements:
        rid = req["id"]
        analysis = req.get("analysis") or {}
        if not analysis:
            continue
        try:
            raw = await llm_call(prompt_builder(analysis, direction_weights or {}))
            points = raw if isinstance(raw, list) else (raw or {}).get("points") or []
            for p in points:
                payload = dict(p)
                # 需求归属与草案号注入（前端分组"需求→测试点→用例"一条链靠这两键）
                payload.setdefault("requirement_id", str(rid))
                payload.setdefault("code", payload.get("code") or "")
                await TestProposal.create(
                    project_id=run.project_id,
                    run_id=run.id,
                    kind="testpoint",
                    payload=payload,
                    source_agent="point_gen",
                    evidence_class=p.get("basis") or "assumption",
                    review_state="machine_pending",
                    status="proposed",
                    testpoint_id=None,
                )
                created += 1
            await _set_requirement_status(rid, "gate1_pending")
        except Exception as exc:  # noqa: BLE001 — 单需求失败不连坐
            failed.append({"requirement_id": str(rid), "error": str(exc)})
    await _save_progress(run)
    return {"created": created, "failed": failed}


async def confirm_testpoints(
    run: TestGenRun,
    *,
    proposal_ids: Sequence[Any],
    decided_by: Any,
) -> dict[str, Any]:
    """门①：选中的测试点提案 → 正式表（status=confirmed）。

    幂等：重复确认同一提案不重复建行（slot_key 语义：testpoint 用
    (project_id, code) 唯一；此处 code 取 TP- 号段）。
    """
    from ..assets.models import TestCase  # noqa: F401 — 确认本模块与正式表同库

    confirmed, skipped = 0, []
    for pid in proposal_ids:
        prop = await TestProposal.get_or_none(id=_as_uuid(pid), run_id=run.id, kind="testpoint")
        # keep 是已确认的终态——再次确认不重复建行（幂等）
        if prop is None or prop.status not in ("proposed", "modify"):
            skipped.append({"id": str(pid), "reason": f"状态不可确认（{getattr(prop, 'status', 'missing')}）"})
            continue
        payload = dict(prop.payload or {})
        code = await _next_testpoint_code(run.project_id)
        await TestTestpoint.create(
            project_id=run.project_id,
            code=code,
            requirement_id=_as_uuid(payload.get("requirement_id")) if payload.get("requirement_id") else None,
            title=str(payload.get("title") or "")[:512],
            trigger=payload.get("trigger"),
            sample_data=payload.get("sample_data"),
            verify_result=payload.get("verify_result"),
            impact=payload.get("impact"),
            evidence=payload.get("evidence"),
            coverage_axis=list(payload.get("coverage_axis") or []),
            surfaces=list(payload.get("surfaces") or []),
            surfaces_decided_by="ai",
            design_method=payload.get("design_method"),
            batch_tier=payload.get("batch_tier"),
            basis=payload.get("basis") or "assumption",
            status="confirmed",
            case_review_state="none",
        )
        prop.status = "keep"
        prop.provenance = {**(prop.provenance or {}), "confirmed_by": str(decided_by), "confirmed_at": _now().isoformat()}
        await prop.save(update_fields=["status", "provenance", "updated_at"])
        confirmed += 1
    await _save_progress(run)
    return {"confirmed": confirmed, "skipped": skipped}


async def _next_testpoint_code(project_id: Any) -> str:
    """TP- 号段：一次取 max + 1（撞号由 unique_together 兜底）。"""
    rows = await TestTestpoint.filter(project_id=project_id).order_by("-code").limit(1).values("code")
    base = 0
    if rows:
        try:
            base = int(str(rows[0]["code"]).replace("TP-", "") or 0)
        except ValueError:
            base = 0
    return f"TP-{base + 1:04d}"


# ── 环节5 用例生成 / 入库（25 号文 v4.1 §环节5 / §3.1 importing；P1）──────


def _default_history_provider(run: TestGenRun) -> Callable[[Mapping[str, Any]], Awaitable[str]]:
    """「相关历史」切片默认生产者（§环节11 风格锚回灌 + 同测试点查重）。

    每任务两段：

    1. **golden 仿写先例**——同 module 的 ``mc_test_case`` 里被 ``case.mark_golden``
       标记的（``confirm_ref.golden=true``），:func:`recall.recall_golden` 召回 ≤5 条；
       模块从测试点的 ``requirement_id`` 反查（结果按需求缓存，一 run 一次查询/需求）。
    2. **同测试点已存在用例**——``mc_test_case.testpoint_id`` 命中 + 本 run 暂存区
       同测试点提案（重派/回炉重跑时防空转重复），供「先查重再仿写」。

    查库只读本模块外的表（assets），不写任何东西；召回/渲染纯函数在
    :mod:`.recall`。任何查询异常 → 返回空串（提示词走 "(none — you are first)"
    兜底，历史切片是加分项不是硬依赖，不许因此炸整轮生成）。
    """
    from .recall import build_history_slice, recall_golden

    module_cache: dict[str, Optional[str]] = {}

    async def _provider(task: Mapping[str, Any]) -> str:
        try:
            from ..assets.models import TestCase, TestRequirement

            tp = task.get("testpoint") or {}
            tp_id = tp.get("id")
            req_id = str(tp.get("requirement_id") or "")

            module: Optional[str] = None
            if req_id:
                if req_id not in module_cache:
                    rows = await TestRequirement.filter(id=_as_uuid(req_id)).values("module")
                    module_cache[req_id] = (rows[0].get("module") if rows else None)
                module = module_cache[req_id]

            case_fields = ("code", "title", "intent", "steps", "expected",
                           "preconditions", "module", "origin", "confirm_ref", "created_at")
            golden_rows: list[dict] = []
            if module:
                gs = TestCase.filter(project_id=run.project_id, module=module)
                golden_rows = await gs.order_by("-created_at").limit(100).values(*case_fields)
                golden_rows = recall_golden(golden_rows, module=module, limit=5)

            existing_rows: list[dict] = []
            if tp_id:
                existing_rows = await TestCase.filter(
                    project_id=run.project_id, testpoint_id=_as_uuid(tp_id),
                ).order_by("-created_at").limit(50).values(*case_fields)
            run_props = await TestProposal.filter(
                run_id=run.id, kind="case", testpoint_id=_as_uuid(tp_id) if tp_id else None,
            ).values("payload") if tp_id else []
            for p in run_props:
                pay = dict(p.get("payload") or {})
                existing_rows.append({"code": pay.get("slot_key") or "(staging)",
                                      "title": pay.get("title"),
                                      "intent": pay.get("intent")})

            return build_history_slice(golden_rows, existing_rows)
        except Exception:  # noqa: BLE001 — 历史切片是加分项，失败退空不炸整轮
            return ""

    return _provider


async def run_case_generation(
    run: TestGenRun,
    *,
    llm_call: LLMCall,
    rules_block_provider: Optional[Callable[[Mapping[str, Any]], Awaitable[str]]] = None,
    history_provider: Optional[Callable[[Mapping[str, Any]], Awaitable[str]]] = None,
) -> dict[str, Any]:
    """环节5 用例细化：confirmed 测试点 × surfaces 扇出 → 落 proposal(kind=case)。

    :param llm_call: ``async (prompt) -> JSON``——返回解析后的 JSON 数组
        （或 ``{"cases": [...]}``）。
    :param rules_block_provider: ``async (task) -> str``——该任务的 active 规则
        正文（查库注入；缺省空块）。
    :param history_provider: ``async (task) -> str``——已存在用例摘要
        （查库注入；缺省用 :func:`_default_history_provider`：同模块 golden 用例
        仿写先例 + 同测试点已存在用例查重，§环节11「风格锚回灌」）。
    :returns: ``{created, failed: [{task_id, testpoint_code, surface, error}]}``。

    状态机：``points_proposed → generating → evaluating``（回边 evaluating→generating
    返工重跑也走这里）。**失败不连坐**：单任务 LLM 失败/解析失败只记 failed，
    其余任务照常；run 不因单任务失败回退。

    run → 需求关联从 ``run.scope.requirement_ids`` 取（测试点 ``requirement_id``
    命中范围才纳入）；scope 无需求或范围全空 → project 全部 confirmed 测试点
    （exec/reopen 型 run 常无 scope）。**不写正式表**——门②确认后由
    :func:`finalize_import` 入库。
    """
    from .case_gen import build_case_prompt, case_to_payload, fanout_tasks

    if history_provider is None:
        history_provider = _default_history_provider(run)

    await _advance_run(run, "generating")

    # 测试点来源：scope.requirement_ids 命中的 confirmed 测试点；无则 project 全量
    scope_ids = (run.scope or {}).get("requirement_ids") or []
    qs = TestTestpoint.filter(project_id=run.project_id, status="confirmed")
    if scope_ids:
        req_uuids = [_as_uuid(x) for x in scope_ids]
        qs = qs.filter(requirement_id__in=req_uuids)
    tp_rows = await qs.values()

    created, failed = 0, []
    for task in fanout_tasks(tp_rows):
        tp = task["testpoint"]
        try:
            rules_block = ""
            if rules_block_provider is not None:
                rules_block = await rules_block_provider(task) or ""
            history_slice = ""
            if history_provider is not None:
                history_slice = await history_provider(task) or ""
            raw = await llm_call(build_case_prompt(
                task, rules_block=rules_block, history_slice=history_slice,
            ))
            cases = raw if isinstance(raw, list) else (raw or {}).get("cases") or []
            for c in cases:
                payload = case_to_payload(c, task)
                # 需求归属注入（前端分组"需求→测试点→用例"一条链）
                if tp.get("requirement_id"):
                    payload.setdefault("requirement_id", str(tp["requirement_id"]))
                await TestProposal.create(
                    project_id=run.project_id,
                    run_id=run.id,
                    kind="case",
                    payload=payload,
                    source_agent="case_gen",
                    surface=payload.get("surface") or task["surface"],
                    testpoint_id=_as_uuid(tp["id"]),
                    evidence_class=payload.get("evidence_class") or "claimed",
                    review_state="machine_pending",
                    status="proposed",
                    slot_key=payload.get("slot_key"),
                )
                created += 1
        except Exception as exc:  # noqa: BLE001 — 单任务失败不连坐
            failed.append({
                "task_id": task["task_id"],
                "testpoint_code": tp.get("code"),
                "surface": task["surface"],
                "error": str(exc),
            })
    run.decisions = list(run.decisions or []) + [{
        "stage": "case_generation", "created": created, "failed": len(failed),
        "at": _now().isoformat(),
    }]
    await run.save(update_fields=["decisions", "updated_at"])
    await _save_progress(run)
    if created:
        await _advance_run(run, "evaluating")
    return {"created": created, "failed": failed}


async def _next_case_code(project_id: Any) -> str:
    """TC- 号段：一次取 max + 1（撞号由 unique_together 兜底）。"""
    from ..assets.models import TestCase

    rows = await TestCase.filter(project_id=project_id).order_by("-code").limit(1).values("code")
    base = 0
    if rows:
        try:
            base = int(str(rows[0]["code"]).replace("TC-", "") or 0)
        except ValueError:
            base = 0
    return f"TC-{base + 1:04d}"


async def _next_dataset_code(project_id: Any) -> str:
    """DS- 号段：一次取 max + 1（撞号由 unique_together 兜底）。"""
    from ..assets.models import TestDataset

    rows = await TestDataset.filter(project_id=project_id).order_by("-code").limit(1).values("code")
    base = 0
    if rows:
        try:
            base = int(str(rows[0]["code"]).replace("DS-", "") or 0)
        except ValueError:
            base = 0
    return f"DS-{base + 1:04d}"


def _dataset_name_keys(payload: Mapping[str, Any], proposal_id: Any) -> list[str]:
    """数据集提案的名字键：payload 里可被用例引用的稳定标识（去重保序）。

    认 ``code`` / ``name`` / ``key`` / ``dataset_key``——用例 payload 用同一字符串
    引用（``dataset``/``dataset_code``/``dataset_name``/``dataset_ref``）。
    外加 proposal id 串（用例可直接引 ``dataset_proposal_id``）。
    """
    keys: list[str] = []
    for field in ("code", "name", "key", "dataset_key"):
        value = str(payload.get(field) or "").strip()
        if value and value not in keys:
            keys.append(value)
    pid = str(proposal_id)
    if pid not in keys:
        keys.append(pid)
    return keys


def _case_dataset_ref(payload: Mapping[str, Any]) -> str:
    """用例 payload 声明的数据集引用（名字键或 dataset 提案 id）。"""
    for field in ("dataset_code", "dataset", "dataset_name", "dataset_ref",
                  "dataset_proposal_id"):
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return ""


async def finalize_import(run: TestGenRun) -> dict[str, Any]:
    """入库事务（§3.1 importing）：门②判完的提案 → 正式表。

    - ``kind=case`` 且 ``status`` ∈ {keep, modify} 且未入库（``provenance.imported_at``
      缺失）→ ``mc_test_case``：TC- 号段、``origin=ai_generated``、``status=draft``、
      ``confirm_source=doc``、``confirm_ref={"slot_key": ...}``，``surface``/
      ``testpoint_id`` 带上（25 号文 v4.1 §2.1）。``requirement_id`` 从 payload 的
      ``testpoint_code`` 反查 ``mc_test_testpoint``；``module`` 从该需求取（可空则
      NULL）；``version_id`` 取 run.version_id（可空则 NULL）。
    - ``kind=dataset`` 同口径 → ``mc_test_dataset``（DS- 号段、schema/rows）。
    - 用例引用的数据集：**两遍**——先建数据集（记名字键→id），再建用例时按引用
      回填 ``dataset_id``（用例 payload 声明 ``dataset`` 等，见 :func:`_case_dataset_ref`）。
    - 入库后提案 ``provenance.imported_at`` 打点；重跑跳过已打点的（幂等）。
    - ``decisions`` 追加 ``{stage: "import", ...}``。
    - 状态推进（缺陷评审修正）：入库前 ``cases_proposed→cases_confirmed→importing``
      （门②判完即过门；已是 cases_confirmed/importing 则跳过，幂等重入不炸），
      成功后 ``importing→done``；有入库用例的需求 gen_status → ``imported``。

    :returns: ``{cases_imported, datasets_imported, skipped: [{id, reason}]}``。
    """
    from ..assets.models import TestCase, TestDataset, TestRequirement

    # ── 入库前进闸：cases_proposed →(门②判完) cases_confirmed → importing ──
    # 已是 importing（半途失败重试）/done（幂等重入）则不再推。
    if run.status in ("cases_proposed", "cases_confirmed"):
        await _advance_run(run, "cases_confirmed")
        await _advance_run(run, "importing")

    cases_imported, datasets_imported = 0, 0
    skipped: list[dict[str, Any]] = []
    # 取本 run 所有 case/dataset 提案（不限状态）：状态不在 keep/modify 或已入库的进 skipped
    props = await TestProposal.filter(
        run_id=run.id, kind__in=["case", "dataset"],
    ).order_by("created_at")

    runnable = []
    for prop in props:
        if prop.status not in ("keep", "modify"):
            skipped.append({"id": str(prop.id),
                            "reason": f"状态不可入库（{prop.status}）"})
            continue
        if dict(prop.provenance or {}).get("imported_at"):
            skipped.append({"id": str(prop.id), "reason": "已入库（幂等跳过）"})
            continue
        runnable.append(prop)

    # ── pass 1：数据集（建行 + 名字键→id 映射）────────────────────────
    dataset_by_key: dict[str, Any] = {}
    dataset_props = [p for p in runnable if p.kind == "dataset"]
    for prop in dataset_props:
        payload = dict(prop.payload or {})
        ds = await TestDataset.create(
            project_id=run.project_id,
            code=await _next_dataset_code(run.project_id),
            schema=payload.get("schema") or {},
            rows=payload.get("rows") or [],
        )
        for key in _dataset_name_keys(payload, prop.id):
            dataset_by_key.setdefault(key, ds.id)
        datasets_imported += 1
        prop.provenance = {**(prop.provenance or {}), "imported_at": _now().isoformat()}
        await prop.save(update_fields=["provenance", "updated_at"])

    # ── pass 2：用例（建行 + 数据集回填）──────────────────────────────
    creator = _as_uuid(run.created_by) if run.created_by else _uuid.UUID(int=0)
    for prop in runnable:
        if prop.kind != "case":
            continue
        payload = dict(prop.payload or {})
        tp_code = str(payload.get("testpoint_code") or "").strip()
        requirement_id, module = None, None
        if tp_code:
            tp_row = await TestTestpoint.filter(
                project_id=run.project_id, code=tp_code,
            ).values("requirement_id")
            if tp_row and tp_row[0].get("requirement_id"):
                requirement_id = tp_row[0]["requirement_id"]
                req_row = await TestRequirement.filter(id=requirement_id).values("module")
                if req_row:
                    module = req_row[0].get("module")
        ds_ref = _case_dataset_ref(payload)
        await TestCase.create(
            project_id=run.project_id,
            code=await _next_case_code(run.project_id),
            title=str(payload.get("title") or "")[:512],
            intent=payload.get("intent"),
            steps=payload.get("steps") or [],
            expected=payload.get("expected") or {},
            preconditions=payload.get("preconditions") or [],
            dataset_id=dataset_by_key.get(ds_ref) if ds_ref else None,
            confirm_source="doc",
            confirm_ref={"slot_key": payload.get("slot_key")},
            origin="ai_generated",
            module=module,
            version_id=run.version_id,
            requirement_id=requirement_id,
            surface=payload.get("surface") or prop.surface,
            testpoint_id=_as_uuid(prop.testpoint_id) if prop.testpoint_id else None,
            status="draft",
            created_by=creator,
        )
        cases_imported += 1
        prop.provenance = {**(prop.provenance or {}), "imported_at": _now().isoformat()}
        await prop.save(update_fields=["provenance", "updated_at"])

    run.decisions = list(run.decisions or []) + [{
        "stage": "import", "cases_imported": cases_imported,
        "datasets_imported": datasets_imported, "skipped": len(skipped),
        "at": _now().isoformat(),
    }]
    await run.save(update_fields=["decisions", "updated_at"])

    # ── 入库后收口：importing → done；有入库用例的需求 → gen_status=imported ──
    if run.status == "importing":
        await _advance_run(run, "done")
    imported_req_ids: set[Any] = set()
    for prop in runnable:
        if prop.kind != "case":
            continue
        tp_code = str((prop.payload or {}).get("testpoint_code") or "").strip()
        if not tp_code:
            continue
        tp_row = await TestTestpoint.filter(
            project_id=run.project_id, code=tp_code,
        ).values("requirement_id")
        if tp_row and tp_row[0].get("requirement_id"):
            imported_req_ids.add(tp_row[0]["requirement_id"])
    for req_id in imported_req_ids:
        await _set_requirement_status(req_id, "imported")

    await _save_progress(run)
    return {
        "cases_imported": cases_imported,
        "datasets_imported": datasets_imported,
        "skipped": skipped,
    }


async def refresh_progress(run: TestGenRun) -> dict:
    """外部（前端轮询/通知）取进度：重算并返回。"""
    await _save_progress(run)
    return dict(run.progress or {})


__all__ = [
    "LLMCall",
    "start_run",
    "run_analysis",
    "run_requirement_review",
    "run_point_proposal",
    "confirm_testpoints",
    "run_case_generation",
    "finalize_import",
    "refresh_progress",
]
