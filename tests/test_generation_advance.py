"""generation/advance.py 门①→门②自动推进接线测试。

验证 25 号文 v4.1 的规矩：门①确认后（全部需求过门①）自动进用例细化→评审→停门②；
未全确认不推进。
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

from testing_platform.generation import advance as adv
from testing_platform.generation import orchestrator as orch
from testing_platform.generation.models import TestGenRun, TestProposal, TestTestpoint
from testing_platform.generation.case_gen import build_case_prompt, case_to_payload, fanout_tasks


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": [
            "testing_platform.assets.models",
            "testing_platform.generation.models",
            "testing_platform.quality.models",
        ]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_req(pid, gen_status="gate1_passed", code="REQ-1"):
    from testing_platform.assets.models import TestRequirement

    return await TestRequirement.create(
        project_id=pid, code=code, title="t", description="d", content={},
        acceptance_criteria=[], gen_status=gen_status,
    )


class _FakeDsh:
    """假 dsh run_chat：按提示词类型返回对应 JSON。"""

    def __init__(self, points=1, cases=1):
        self._points, self._cases = points, cases

    async def __call__(self, *, prompt, config):
        import json as _json

        if "ONE analysis slice ONE risk direction" in prompt:  # 提点
            return {"final_response": _json.dumps([
                {"title": "TP-A", "trigger": "t", "verify_result": "v", "evidence": "AC-1",
                 "coverage_axis": ["C01"], "surfaces": ["api"], "batch_tier": "A", "basis": "fact"}
                for _ in range(self._points)
            ])}
        if "behaviour first" in prompt or "BEHAVIOUR FIRST" in prompt.upper():  # 用例生成
            cases = [{
                "title": "TC-A", "surface": "api", "steps": [{"seq": 1, "action": "a", "expected": "e"}],
                "expected": {"status_code": 200, "biz_code": "0", "key_fields": ["id"], "response_time": 2000},
                "basis": "fact", "evidence": "AC-1", "applied_rules": [],
            } for _ in range(self._cases)]
            return {"final_response": _json.dumps(cases)}
        if "verdict" in prompt:  # 双评（reviewer 提示词）
            return {"final_response": _json.dumps(
                {"verdict": "passed", "reason": "ok", "evidence_class": "static"})}
        return {"final_response": "{}"}


async def _api_lookup(_api_id):
    return None


@pytest.mark.asyncio
async def test_advance_after_gate1_all_passed(tortoise_db):
    """全需求过门① → 用例生成+评审+落 cases_proposed。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    # 模拟真实时序：pending→…→points_proposed（门①停在这）
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)
    r1 = await _make_req(pid, "gate1_passed", "R1")
    await _make_req(pid, "gate1_passed", "R2")
    # 门①确认过的测试点（真实流程由 confirm_testpoints 建）——用例扇出的输入
    await TestTestpoint.create(
        project_id=pid, code="TP-0001", requirement_id=r1.id, title="TP-A",
        trigger="t", verify_result="v", evidence="AC-1",
        coverage_axis=["C01"], surfaces=["api"], surfaces_decided_by="ai",
        design_method="scene", batch_tier="A", basis="fact", status="confirmed",
        case_review_state="none",
    )

    res = await adv.advance_after_gate1(
        run, dsh_chat=_FakeDsh(points=1, cases=1), config=None,
        api_lookup=_api_lookup,
    )
    assert res["advanced"] is True
    fresh = await TestGenRun.get(id=run.id)
    assert fresh.status == "cases_proposed"
    # 提案有 kind=case 落库
    cases = await TestProposal.filter(run_id=run.id, kind="case").count()
    assert cases >= 1


@pytest.mark.asyncio
async def test_advance_blocked_when_not_all_gate1(tortoise_db):
    """有需求未过门① → 不推进。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)
    await _make_req(pid, "gate1_passed", "R1")
    await _make_req(pid, "analyzing", "R2")  # 未过

    res = await adv.advance_after_gate1(
        run, dsh_chat=_FakeDsh(), config=None, api_lookup=_api_lookup,
    )
    assert res["advanced"] is False
    assert "门①" in res["reason"]


@pytest.mark.asyncio
async def test_advance_after_review_no_pending(tortoise_db):
    """无 machine_pending → evaluating→cases_proposed。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    await TestGenRun.filter(id=run.id).update(status="evaluating")
    run = await TestGenRun.get(id=run.id)
    res = await adv.advance_after_review(run)
    assert res["advanced"] is True
    fresh = await TestGenRun.get(id=run.id)
    assert fresh.status == "cases_proposed"


@pytest.mark.asyncio
async def test_advance_after_review_with_pending(tortoise_db):
    """还有 machine_pending → 不推进。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    await TestGenRun.filter(id=run.id).update(status="evaluating")
    run = await TestGenRun.get(id=run.id)
    await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case", payload={"title": "x"},
        review_state="machine_pending", status="proposed",
    )
    res = await adv.advance_after_review(run)
    assert res["advanced"] is False and res["pending"] == 1
