"""generation/orchestrator.py 端到端编排（25 号文 v4.1 §1.2 / P0）。

SQLite 内存库 + 注入假 LLM，走真落库路径，验证：
1. start_run → run_analysis（每需求独立、单失败不连坐）→ 门⓪
   req_review（建 question 行）→ 提点（写 proposal kind=testpoint，不写正式表）→ 停门①。
2. **铁律**：到门①之前 mc_test_testpoint 正式表零行（P0 验收）。
3. confirm_testpoints：提案 → 正式表（TP-号段连续 + 幂等重跑不重复）。
4. status 状态机推进：pending → analyzing → req_reviewing → points_proposed。
5. refresh_progress：progress 与事实同步。
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

import json
from typing import Any, Awaitable, Callable

from testing_platform.generation import orchestrator as orch
from testing_platform.generation import state as gs
from testing_platform.generation.models import TestGenRun, TestProposal, TestTestpoint
from testing_platform.generation.req_analysis import build_analysis_prompt
from testing_platform.generation.req_review import build_review_prompt
from testing_platform.generation.point_gen import build_point_prompt

# ── fixtures ──────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": [
            "testing_platform.assets.models",       # TestRequirement（含 analysis/review/gen_status）
            "testing_platform.generation.models",   # gen_run/testpoint/proposal/coverage
            "testing_platform.quality.models",      # TestQuestion（门⓪建问题行）
        ]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _analysis_payload(valid=True) -> dict:
    return {
        "inventory": {"flow": "full", "rules": "full", "roles": "full", "exception": "full", "nfr": "full"},
        "smells": [],
        "criteria": [
            {"id": "AC-1", "condition": "X", "expected": "Y", "testable": True,
             "source": "REQ-1:§2"} if valid else {"id": "AC-1", "condition": "", "expected": "", "testable": False}
        ],
        "register": [{"id": "G1", "kind": "fact", "source": "REQ-1:§2", "priority": "P2"}],
        "invariants": [{"rule": "恒真式", "source": "AC-1"}],
        "dependencies": [],
        "evidence_class": "static",
    }


def _review_payload() -> dict:
    return {
        "findings": [{"severity": "major", "evidence": "REQ-1:§2", "suggested_action": "补文档"}],
        "questions": [{"claim": "验证码有效期", "recommended_answer": "5分钟"}],
        "tsdr": [{"question": "Q1", "recommended": "A", "actual": "A", "rationale": "r"}],
        "verdict": "pass",
    }


def _points_payload() -> list:
    return [{
        "title": "TP-01 正常登录",
        "trigger": "输正确密码",
        "sample_data": {"user": "u1"},
        "verify_result": "登录成功",
        "impact": "可用性",
        "evidence": "AC-1",
        "coverage_axis": ["C01"],
        "surfaces": ["api", "web"],
        "design_method": "scene",
        "batch_tier": "A",
        "basis": "fact",
    }]


def _fake_llm(*outputs):
    """返回一个按调用顺序吐 outputs 的 async LLM 调用器。"""
    it = iter(outputs)
    async def call(_prompt):
        try:
            return next(it)
        except StopIteration as exc:
            raise RuntimeError("llm exhausted") from exc
    return call


async def _make_requirement(project_id, code="REQ-001", title="登录改造"):
    from testing_platform.assets.models import TestRequirement

    return await TestRequirement.create(
        project_id=project_id, code=code, title=title,
        description="登录功能", content={}, acceptance_criteria=[{"id": "AC-1", "text": "X→Y"}],
        gen_status="not_started",
    )


# ── tests ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_full_pipeline_p0(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="full")
    assert run.status == "pending"

    req = await _make_requirement(pid)
    reqs = [{"id": str(req.id), "content": "需求正文"}]

    # 环节1 分析
    llm = _fake_llm(_analysis_payload())
    res = await orch.run_analysis(run, llm_call=llm,
                                  prompt_builder=lambda c: build_analysis_prompt(c), requirements=reqs)
    assert res["analyzed"] == 1 and res["failed"] == []
    assert run.status == "req_reviewing"

    # 门⓪ 评审
    await TestGenRun.filter(id=run.id).update()  # refresh
    run = await TestGenRun.get(id=run.id)
    llm2 = _fake_llm(_review_payload())
    rev = await orch.run_requirement_review(run, llm_call=llm2,
                                           prompt_builder=lambda a: build_review_prompt(json.dumps(a)),
                                           requirements=[{"id": str(req.id), "analysis": _analysis_payload()}])
    assert rev["reviewed"] == 1

    # 提点
    run = await TestGenRun.get(id=run.id)
    await TestGenRun.filter(id=run.id).update(status="req_reviewed")  # 模拟人过了门⓪
    llm3 = _fake_llm(_points_payload())
    pts = await orch.run_point_proposal(run, llm_call=llm3,
                                        prompt_builder=lambda a, w: build_point_prompt(a, w),
                                        requirements=[{"id": str(req.id), "analysis": _analysis_payload()}])
    assert pts["created"] == 1
    run = await TestGenRun.get(id=run.id)
    assert run.status == "points_proposed"

    # ★ 铁律：门①前正式测试点表零行
    assert await TestTestpoint.all().count() == 0
    # 提案有一条 kind=testpoint
    proposals = await TestProposal.filter(run_id=run.id).all()
    assert len(proposals) == 1 and proposals[0].kind == "testpoint"

    # 门① 确认
    confirm = await orch.confirm_testpoints(run, proposal_ids=[proposals[0].id], decided_by=uuid.uuid4())
    assert confirm["confirmed"] == 1
    # 正式表现在有一条
    tps = await TestTestpoint.all()
    assert len(tps) == 1 and tps[0].status == "confirmed"
    assert tps[0].code.startswith("TP-")
    # 幂等：同一 proposal 再确认不重复建行
    again = await orch.confirm_testpoints(run, proposal_ids=[proposals[0].id], decided_by=uuid.uuid4())
    assert again["confirmed"] == 0
    assert await TestTestpoint.all().count() == 1


@pytest.mark.asyncio
async def test_single_requirement_failure_no_cascade(tortoise_db):
    """单需求分析失败不影响其他需求（失败不连坐）。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    r1 = await _make_requirement(pid, "REQ-A", "A")
    r2 = await _make_requirement(pid, "REQ-B", "B")
    reqs = [{"id": str(r1.id), "content": "a"}, {"id": str(r2.id), "content": "b"}]

    # 第一个返回有效，第二个返回无效 analysis（validate 拒）
    llm = _fake_llm(_analysis_payload(), _analysis_payload(valid=False))
    res = await orch.run_analysis(run, llm_call=llm,
                                  prompt_builder=lambda c: build_analysis_prompt(c), requirements=reqs)
    assert res["analyzed"] == 1
    assert len(res["failed"]) == 1
    assert res["failed"][0]["requirement_id"] == str(r2.id)


@pytest.mark.asyncio
async def test_progress_syncs_with_state(tortoise_db):
    """refresh_progress 与事实同步——progress.testpoints.proposed 与 proposal 计数对齐。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    req = await _make_requirement(pid)
    # 先写一条 proposal 再算进度
    await TestProposal.create(
        project_id=pid, run_id=run.id, kind="testpoint", payload={"title": "x"},
        source_agent="point_gen", evidence_class="assumption",
        review_state="machine_pending", status="proposed",
    )
    prog = await orch.refresh_progress(run)
    assert prog["testpoints"]["proposed"] >= 1


@pytest.mark.asyncio
async def test_status_transition_invalid_blocked(tortoise_db):
    """非法状态跳转被拒（如 done → analyzing）。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    await TestGenRun.filter(id=run.id).update(status="done")
    run = await TestGenRun.get(id=run.id)
    with pytest.raises(ValueError):
        # done 是终态，无出边
        await orch._advance_run(run, "analyzing")


# ── finalize_import：入库状态推进（缺陷评审修正 #5）────────────────
@pytest.mark.asyncio
async def test_finalize_import_advances_cases_confirmed_to_done(tortoise_db):
    """入库前推 cases_confirmed→importing、成功后 importing→done 并置需求 gen_status=imported。"""
    from testing_platform.assets.models import TestCase, TestRequirement

    pid = uuid.uuid4()
    req = await _make_requirement(pid)
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="full",
                               scope={"requirement_ids": [str(req.id)]})
    await TestGenRun.filter(id=run.id).update(status="cases_confirmed")
    run = await TestGenRun.get(id=run.id)

    tp = await TestTestpoint.create(
        project_id=pid, code="TP-0001", requirement_id=req.id, title="正常登录",
        surfaces=["api"], coverage_axis=["C01"], status="confirmed",
    )
    await TestProposal.create(
        run_id=run.id, project_id=pid, kind="case",
        payload={"title": "正常登录", "intent": "验证合法凭证登录成功", "surface": "api",
                 "steps": [], "expected": {"status_code": 200},
                 "testpoint_code": "TP-0001", "slot_key": "flow:TP-0001:api:1"},
        source_agent="case_gen_api", surface="api", evidence_class="inferred",
        review_state="scored", status="keep", slot_key="flow:TP-0001:api:1",
    )
    res = await orch.finalize_import(run)
    assert res["cases_imported"] == 1 and res["datasets_imported"] == 0

    run = await TestGenRun.get(id=run.id)
    assert run.status == "done"                       # 修正处：入库后收口 done
    assert any(d["stage"] == "import" for d in run.decisions)

    case = await TestCase.filter(project_id=pid).first()
    assert case is not None and case.requirement_id == req.id
    assert (await TestRequirement.get(id=req.id)).gen_status == "imported"

    # 幂等重入：不炸、不重复入库、终态不变
    res2 = await orch.finalize_import(run)
    assert res2["cases_imported"] == 0
    assert all(s["reason"] == "已入库（幂等跳过）" for s in res2["skipped"])
    assert (await TestGenRun.get(id=run.id)).status == "done"


@pytest.mark.asyncio
async def test_finalize_import_advances_cases_proposed_through_importing(tortoise_db):
    """门②判完即过门：cases_proposed 一路推 cases_confirmed→importing→done。"""
    pid = uuid.uuid4()
    req = await _make_requirement(pid)
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="full")
    await TestGenRun.filter(id=run.id).update(status="cases_proposed")
    run = await TestGenRun.get(id=run.id)

    await TestProposal.create(
        run_id=run.id, project_id=pid, kind="case",
        payload={"title": "无测试点引用", "intent": "验证", "surface": "web",
                 "steps": [], "expected": {"element_visible": True}},
        source_agent="case_gen_api", surface="web", evidence_class="inferred",
        review_state="scored", status="keep", slot_key="flow:TP-free:web:1",
    )
    res = await orch.finalize_import(run)
    assert res["cases_imported"] == 1
    assert (await TestGenRun.get(id=run.id)).status == "done"
    # 无 testpoint_code → 反查不出需求，gen_status 不动（不误置）
    _ = req


# ── 门⓪ 问题行持久化（推荐答案/round_no/gate 列）──────────────────────
@pytest.mark.asyncio
async def test_requirement_review_persists_question_fields(tortoise_db):
    """run_requirement_review 建的 TestQuestion 必须带 gate/round_no/recommended_answer
    ——前端「一次一问卡片」与预算计数依赖这三列持久化。"""
    from testing_platform.quality.models import TestQuestion

    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="full")
    req = await _make_requirement(pid)
    await TestGenRun.filter(id=run.id).update(status="req_reviewing")

    review = _review_payload()  # questions=[{"claim":"验证码有效期","recommended_answer":"5分钟"}]
    llm = _fake_llm(review)
    res = await orch.run_requirement_review(
        run, llm_call=llm,
        prompt_builder=lambda a: build_review_prompt(json.dumps(a)),
        requirements=[{"id": str(req.id), "analysis": _analysis_payload()}])
    assert res["reviewed"] == 1

    qs = await TestQuestion.filter(requirement_id=req.id).all()
    assert len(qs) == 1
    q = qs[0]
    assert q.gate == "req_review"                    # 持久化验证点
    assert q.recommended_answer == "5分钟"
    assert q.content == "验证码有效期"
    assert q.origin == "ai"
    assert q.status == "open"
    # context 带推荐答案副本 + gate 标记
    assert q.context.get("gate") == "req_review"
    assert q.context.get("recommended_answer") == "5分钟"
