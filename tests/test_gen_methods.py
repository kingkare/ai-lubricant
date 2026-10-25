"""gen_methods（门①②/进度方法）端到端测试。

SQLite 内存库 + 编排器真路径：
1. testpoint.confirm 门①入口（幂等）。
2. proposal.list 分页与过滤（门①②工作台数据源）。
3. genrun.get 进度轮询（重算 progress + 标签）。
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

from testing_platform.generation import orchestrator as orch
from testing_platform.generation.models import TestGenRun, TestProposal
from testing_platform.surface import call_method
from testing_platform.surface.methods import MethodError


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


async def _seed_run_with_proposals(n: int = 3):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    props = []
    for i in range(n):
        p = await TestProposal.create(
            project_id=pid, run_id=run.id, kind="testpoint",
            payload={"title": f"TP-{i:02d}", "surfaces": ["api"]},
            source_agent="point_gen", evidence_class="assumption",
            review_state="machine_pending", status="proposed",
        )
        props.append(p)
    return run, props


@pytest.mark.asyncio
async def test_testpoint_confirm_gate(tortoise_db):
    """门①入口：确认选中提案 → 正式表；幂等重确认不重复。"""
    run, props = await _seed_run_with_proposals(2)
    res = await call_method("testpoint.confirm", {
        "run_id": str(run.id), "proposal_ids": [str(p.id) for p in props], "confirmed_by": str(uuid.uuid4()),
    })
    assert res["confirmed"] == 2
    again = await call_method("testpoint.confirm", {
        "run_id": str(run.id), "proposal_ids": [str(props[0].id)], "confirmed_by": str(uuid.uuid4()),
    })
    assert again["confirmed"] == 0  # 幂等


@pytest.mark.asyncio
async def test_proposal_list_pagination_and_filter(tortoise_db):
    run, props = await _seed_run_with_proposals(3)
    res = await call_method("proposal.list", {"run_id": str(run.id), "kind": "testpoint"})
    assert res["total"] == 3 and res["count"] == 3
    page = await call_method("proposal.list", {"run_id": str(run.id), "limit": 2, "offset": 0})
    assert page["count"] == 2 and page["total"] == 3
    none = await call_method("proposal.list", {"run_id": str(run.id), "kind": "case"})
    assert none["total"] == 0


@pytest.mark.asyncio
async def test_genrun_get_progress(tortoise_db):
    run, _ = await _seed_run_with_proposals(1)
    res = await call_method("genrun.get", {"run_id": str(run.id)})
    assert res["run_id"] == str(run.id)
    assert res["status"] == run.status
    assert "status_label" in res and res["status_label"]
    # progress 是重算过的结构（五段）
    assert set(res["progress"].keys()) >= {"requirements", "testpoints", "cases", "coverage", "budget"}
    # scope/config 回传（前端范围展示 + 模型名显示；config 不得泄漏 key）
    assert res["scope"] == dict(run.scope or {})
    assert "config" in res and "api_key" not in res["config"]


@pytest.mark.asyncio
async def test_genrun_missing_errors(tortoise_db):
    with pytest.raises(MethodError):
        await call_method("genrun.get", {"run_id": str(uuid.uuid4())})


# ── genrun.list —— 生成任务历史（读，独立列表页数据源）────────────
@pytest.mark.asyncio
async def test_genrun_list_returns_items_and_total(tortoise_db):
    run, _ = await _seed_run_with_proposals(1)
    res = await call_method("genrun.list", {"project_id": str(run.project_id)})
    assert res["total"] >= 1 and res["count"] >= 1
    item = next(x for x in res["items"] if x["run_id"] == str(run.id))
    assert item["status"] == run.status
    assert item["status_label"]
    assert item["run_type"] == run.run_type
    assert item["requirement_count"] == 0            # scope.requirement_ids 空
    assert "progress_summary" in item                # 列表徽章用概要（可空 dict）
    # 倒序：新 run 在前
    res2 = await call_method("genrun.list", {"project_id": str(run.project_id)})
    ids = [x["run_id"] for x in res2["items"]]
    assert ids == sorted(ids, key=ids.index)          # 顺序稳定即可（单 run 无从比较）


@pytest.mark.asyncio
async def test_genrun_list_scopes_by_project(tortoise_db):
    run, _ = await _seed_run_with_proposals(0)
    other_pid = uuid.uuid4()
    res = await call_method("genrun.list", {"project_id": str(other_pid)})
    assert res["total"] == 0                          # 别的项目不串
    # 不带 project_id = 全量
    res_all = await call_method("genrun.list", {})
    assert res_all["total"] >= 1


# ── question.sink —— 问题库回流通用沉淀（写，§环节11）────────────
@pytest.mark.asyncio
async def test_question_sink_stores_batch(tortoise_db):
    pid = str(uuid.uuid4())
    res = await call_method("question.sink", {
        "project_id": pid,
        "source": "gate2_reject",
        "items": [
            {"id": str(uuid.uuid4()), "reason": "断言只有 status=200，缺业务码",
             "module": "auth"},
            {"id": str(uuid.uuid4()), "reason": "越权场景没覆盖", "module": "auth"},
        ],
    })
    assert res["stored"] == 2 and len(res["question_ids"]) == 2

    from testing_platform.quality.models import TestQuestion
    rows = await TestQuestion.filter(project_id=_uuid_of(pid)).all()
    assert len(rows) == 2
    for row in rows:
        assert row.origin == "reject_reason"          # sink kind 落 origin
        assert row.status == "resolved"
        ctx = dict(row.context or {})
        assert ctx.get("gate") == "sink" and ctx.get("sink") is True
        assert ctx.get("module") == "auth"
    # 非法来源拒绝
    with pytest.raises(MethodError):
        await call_method("question.sink", {
            "project_id": pid, "source": "not_a_source", "items": [{"reason": "x"}],
        })


def _uuid_of(v: str):
    import uuid as _u
    return _u.UUID(v)


# ── explore.plan / explore.record —— 探索门（§环节7）──────────────
@pytest.mark.asyncio
async def test_explore_plan_content_based_gate(tortoise_db):
    """api 框架类不过门；字段规则/逻辑/安全过门；web 一律过门（按内容不按端）。"""
    framework = {"title": "参数校验", "intent": "缺 page 参数返回 400（mc_test_api 契约）",
                 "expected": {"status_code": 400}, "scenario_tag": "param_missing"}
    res = await call_method("explore.plan", {"case_payload": framework, "surface": "api"})
    assert res["required"] is False and res["content_class"] == "framework"

    web_case = {"title": "登录后按钮可见", "intent": "点登录",
                "expected": {"element_visible": True}}
    res2 = await call_method("explore.plan", {"case_payload": web_case, "surface": "web"})
    assert res2["required"] is True and res2["content_class"] == "ui_flow"

    sec_case = {"title": "token 失效", "intent": "过期 token 调接口拒绝",
                "expected": {"status_code": 401}, "scenario_tag": "token_expired"}
    res3 = await call_method("explore.plan", {"case_payload": sec_case, "surface": "api"})
    assert res3["required"] is True


@pytest.mark.asyncio
async def test_explore_record_flips_verified_state(tortoise_db):
    """outcome=pass → 已探索；fail → 探索失败；重复同值不写（幂等）。"""
    from testing_platform.assets.models import TestCase

    pid = uuid.uuid4()
    case = await TestCase.create(
        project_id=pid, code="TC-E1", title="登录成功",
        confirm_source="doc", created_by=uuid.uuid4(),
    )
    assert (case.verified_by_run or "unexplored") == "unexplored"

    r1 = await call_method("explore.record", {"case_id": str(case.id), "outcome": "pass",
                                              "note": "dsh 真跑一遍通过"})
    assert r1["verified_by_run"] == "explored" and r1["changed"] is True
    row = await TestCase.get(id=case.id)
    assert row.verified_by_run == "explored"
    assert row.confirm_ref.get("explore_note") == "dsh 真跑一遍通过"

    r2 = await call_method("explore.record", {"case_id": str(case.id), "outcome": "pass"})
    assert r2["changed"] is False                       # 同值幂等不写

    r3 = await call_method("explore.record", {"case_id": str(case.id), "outcome": "fail"})
    assert r3["verified_by_run"] == "failed"
    with pytest.raises(MethodError):
        await call_method("explore.record", {"case_id": str(uuid.uuid4()), "outcome": "pass"})
