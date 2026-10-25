"""generation/case_gen.py + orchestrator P1 追加（环节5/入库）端到端测试。

SQLite 内存库 + 注入假 LLM，走真落库路径，验证：
1. fanout_tasks：confirmed 测试点 × surfaces 展开（一点一端一任务）。
2. build_case_prompt：英文提示词关键词（六策略名 / persist before consult /
   applied_rules / 配比 ≥1+≥3）。
3. case_to_payload：slot_key 生成 + evidence_class 映射（fact→static 等）。
4. run_case_generation 端到端：假 LLM 出 2 条 case → proposal 落库、
   run 推进到 evaluating、单任务失败不连坐。
5. case.verdict 四判决（reject 缺 reason 报错、modify 换 payload）。
6. finalize_import：keep 的入 mc_test_case 带 surface、重跑幂等不重复、
   dataset 回填 dataset_id。
7. coverage.summary：轴×端矩阵与 lit 总数。
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

from testing_platform.assets.models import TestCase, TestDataset
from testing_platform.generation import case_gen
from testing_platform.generation import orchestrator as orch
from testing_platform.generation.models import TestGenRun, TestProposal, TestTestpoint
from testing_platform.surface import call_method
from testing_platform.surface.methods import MethodError


# ── fixtures ──────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": [
            "testing_platform.assets.models",       # TestCase/TestDataset/TestRequirement
            "testing_platform.generation.models",   # gen_run/testpoint/proposal/coverage
            "testing_platform.quality.models",      # TestQuestion（progress 聚合读）
        ]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _fake_llm(*outputs):
    """返回一个按调用顺序吐 outputs 的 async LLM 调用器。"""
    it = iter(outputs)
    async def call(_prompt):
        try:
            return next(it)
        except StopIteration as exc:
            raise RuntimeError("llm exhausted") from exc
    return call


async def _make_confirmed_testpoint(project_id, code="TP-0001", surfaces=("api", "web"),
                                    axes=("C01",), requirement_id=None):
    return await TestTestpoint.create(
        project_id=project_id,
        code=code,
        requirement_id=requirement_id,
        title="正常登录",
        trigger="输正确密码",
        sample_data='{"user": "u1"}',
        verify_result="登录成功",
        impact="可用性",
        evidence="AC-1",
        coverage_axis=list(axes),
        surfaces=list(surfaces),
        surfaces_decided_by="ai",
        design_method="scenario",
        batch_tier="A",
        basis="fact",
        status="confirmed",
        case_review_state="none",
    )


def _case_payload_llm(title="负向-密码错误被拒", surface="api", basis="fact"):
    return {
        "title": title,
        "surface": surface,
        "steps": [
            {"seq": 1, "action": "submit login with wrong password", "expected": "401 returned"},
            {"seq": 2, "action": "query login history", "expected": "failure recorded"},
        ],
        "expected": {"status_code": 401, "biz_code": "AUTH_FAILED",
                     "key_fields": ["error"], "response_time": "<1s"},
        "preconditions": ["账号 {{env.user}} 可用"],
        "basis": basis,
        "evidence": ["AC-1"],
        "scenario_tag": "negative",
        "applied_rules": [{"rule_id": "R-1", "disposition": "followed", "why": ""}],
    }


# ── 1. fanout_tasks ───────────────────────────────────────────────────
def test_fanout_tasks_expands_surfaces():
    tp = {"id": str(uuid.uuid4()), "code": "TP-0001", "title": "x",
          "status": "confirmed", "surfaces": ["api", "web"]}
    tasks = case_gen.fanout_tasks([tp])
    assert len(tasks) == 2
    assert {t["surface"] for t in tasks} == {"api", "web"}
    assert all(t["testpoint"]["code"] == "TP-0001" for t in tasks)
    # task_id 是 uuid 字符串且互不相同
    ids = {t["task_id"] for t in tasks}
    assert len(ids) == 2
    for tid in ids:
        uuid.UUID(tid)  # raises if not uuid


def test_fanout_skips_unconfirmed_and_empty_surfaces():
    tp_conf = {"id": "1", "code": "TP-1", "status": "confirmed", "surfaces": ["api"]}
    tp_draft = {"id": "2", "code": "TP-2", "status": "candidate", "surfaces": ["web"]}
    tp_empty = {"id": "3", "code": "TP-3", "status": "confirmed", "surfaces": []}
    tasks = case_gen.fanout_tasks([tp_conf, tp_draft, tp_empty, None, "junk"])
    assert len(tasks) == 1 and tasks[0]["surface"] == "api"


# ── 2. build_case_prompt ─────────────────────────────────────────────
def test_build_case_prompt_keywords():
    tp = {"id": "1", "code": "TP-0001", "title": "正常登录", "status": "confirmed",
          "surfaces": ["api"], "trigger": "t", "basis": "fact"}
    task = {"task_id": str(uuid.uuid4()), "testpoint": tp, "surface": "api"}
    prompt = case_gen.build_case_prompt(task, rules_block="R-1: no hardcode",
                                        history_slice="TC-0001 dup guard")
    # 六策略名
    for strategy in ("positive", "negative", "boundary", "equivalence-class",
                     "scenario", "state-transition"):
        assert strategy in prompt
    # 三纪律关键词
    assert "persist" in prompt and "consult" in prompt
    assert "NOMINATE ONLY" in prompt
    assert "reconcile" in prompt
    # applied_rules 契约 + 配比
    assert "applied_rules" in prompt
    assert "≥1" in prompt and "≥3" in prompt
    # 单测试点单端口径 + 断言体系 per 端
    assert "ONE surface" in prompt and "confirmed test point" in prompt
    assert "status_code" in prompt and "element_visible" in prompt
    assert "crash_log" in prompt and "reject_code" in prompt
    # 数据五作用域
    for scope in ("env", "auth", "local", "data", "global"):
        assert scope in prompt
    # rules_block / history_slice 注入
    assert "R-1: no hardcode" in prompt
    assert "TC-0001 dup guard" in prompt
    # 行为先、断言单独设计
    assert "BEHAVIOUR first" in prompt
    # surface 必须回显
    assert "api" in prompt


# ── 3. case_to_payload ────────────────────────────────────────────────
def test_case_to_payload_slot_key_and_evidence_map():
    tp = {"id": "1", "code": "TP-0007", "title": "t", "status": "confirmed",
          "surfaces": ["api"], "basis": "fact"}
    task = {"task_id": "tid-1", "testpoint": tp, "surface": "api"}
    payload = case_gen.case_to_payload(_case_payload_llm(), task)
    # slot_key：flow 轴（无 api_id）
    assert payload["slot_key"].startswith("flow:TP-0007:api:")
    assert payload["testpoint_code"] == "TP-0007"
    # evidence_class 映射：basis=fact → static
    assert payload["evidence_class"] == "static"
    # steps 归一
    assert [s["seq"] for s in payload["steps"]] == [1, 2]
    assert all("action" in s and "expected" in s for s in payload["steps"])
    # expected 补齐 api 断言字段
    for field in ("status_code", "biz_code", "key_fields", "response_time"):
        assert field in payload["expected"]
    # preconditions 归一为字符串列表
    assert payload["preconditions"] == ["账号 {{env.user}} 可用"]
    # applied_rules 归一
    assert payload["applied_rules"][0]["rule_id"] == "R-1"
    assert payload["applied_rules"][0]["disposition"] == "followed"


def test_case_to_payload_evidence_class_mapping():
    task = {"task_id": "t", "testpoint": {"code": "TP-1", "basis": "fact"}, "surface": "web"}
    # to_confirm → claimed
    p = case_gen.case_to_payload(_case_payload_llm(basis="to_confirm"), task)
    assert p["evidence_class"] == "claimed"
    # inferred → inferred
    p = case_gen.case_to_payload(_case_payload_llm(basis="inferred"), task)
    assert p["evidence_class"] == "inferred"
    # 缺省 → claimed
    p = case_gen.case_to_payload(_case_payload_llm(basis=""), task)
    assert p["evidence_class"] == "claimed"
    # api 轴 slot_key：带 api_id 时
    c = _case_payload_llm()
    c["api_id"] = "api-123"
    p = case_gen.case_to_payload(c, task)
    assert p["slot_key"].startswith("api:api-123:")


# ── 4. run_case_generation 端到端 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_run_case_generation_e2e(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="exec")
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)

    tp = await _make_confirmed_testpoint(pid)
    # 假 LLM：每个任务出 2 条 case
    def two_cases(_prompt):
        return [_case_payload_llm("case A"), _case_payload_llm("case B")]
    async def llm_call(prompt):
        return two_cases(prompt)

    res = await orch.run_case_generation(run, llm_call=llm_call)
    # 一个测试点 × 2 端 = 2 任务 × 2 case = 4 提案
    assert res["created"] == 4 and res["failed"] == []
    run = await TestGenRun.get(id=run.id)
    assert run.status == "evaluating"
    props = await TestProposal.filter(run_id=run.id, kind="case")
    assert len(props) == 4
    assert all(p.status == "proposed" for p in props)
    assert all(p.review_state == "machine_pending" for p in props)
    assert all(p.testpoint_id == tp.id for p in props)
    assert all(p.payload.get("testpoint_code") == "TP-0001" for p in props)
    # decisions 流水有一条 case_generation
    stages = [d.get("stage") for d in (run.decisions or [])]
    assert "case_generation" in stages
    # 正式用例表零写入（铁律：门②前不入库）
    assert await TestCase.all().count() == 0


@pytest.mark.asyncio
async def test_run_case_generation_scope_and_failure_no_cascade(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(
        project_id=pid, created_by=uuid.uuid4(), run_type="exec",
        scope={"requirement_ids": []},  # 空 scope → project 全量 confirmed
    )
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)

    await _make_confirmed_testpoint(pid, "TP-0001", surfaces=("api", "web"))
    # 两个任务：第一个 LLM 炸，第二个正常出 1 条
    calls = {"n": 0}
    async def llm_call(_prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("llm boom")
        return [_case_payload_llm("only case")]

    res = await orch.run_case_generation(run, llm_call=llm_call)
    assert res["created"] == 1
    assert len(res["failed"]) == 1
    assert res["failed"][0]["testpoint_code"] == "TP-0001"
    assert res["failed"][0]["surface"] == "api"
    # 失败不连坐：run 照常推进
    run = await TestGenRun.get(id=run.id)
    assert run.status == "evaluating"
    assert await TestProposal.filter(run_id=run.id, kind="case").count() == 1


@pytest.mark.asyncio
async def test_run_case_generation_scope_filters_by_requirement(tortoise_db):
    """scope.requirement_ids 过滤：命中范围的测试点才扇出。"""
    from testing_platform.assets.models import TestRequirement

    pid = uuid.uuid4()
    req = await TestRequirement.create(
        project_id=pid, code="REQ-001", title="R1", gen_status="not_started",
    )
    run = await orch.start_run(
        project_id=pid, created_by=uuid.uuid4(), run_type="full",
        scope={"requirement_ids": [str(req.id)]},
    )
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)

    await _make_confirmed_testpoint(pid, "TP-IN", requirement_id=req.id)
    await _make_confirmed_testpoint(pid, "TP-OUT")

    async def llm_call(_prompt):
        return [_case_payload_llm("one")]

    res = await orch.run_case_generation(run, llm_call=llm_call)
    # 只有 TP-IN（挂范围需求）× 2 端 = 2 任务
    assert res["created"] == 2
    props = await TestProposal.filter(run_id=run.id, kind="case")
    assert {p.payload["testpoint_code"] for p in props} == {"TP-IN"}


# ── 5. case.verdict ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_case_verdict_four_verdicts(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    props = []
    for verdict in ("keep", "modify", "reject", "defer"):
        p = await TestProposal.create(
            project_id=pid, run_id=run.id, kind="case",
            payload={"title": f"c-{verdict}", "surface": "api"},
            source_agent="case_gen", evidence_class="claimed",
            review_state="machine_pending", status="proposed",
        )
        props.append(p)

    decider = str(uuid.uuid4())
    # keep
    res = await call_method("case.verdict", {
        "proposal_id": str(props[0].id), "verdict": "keep", "decided_by": decider,
    })
    assert res["updated"] == 1
    # modify：换 payload
    res = await call_method("case.verdict", {
        "proposal_id": str(props[1].id), "verdict": "modify",
        "edited_payload": {"title": "edited", "surface": "web"},
        "decided_by": decider,
    })
    assert res["updated"] == 1
    # reject：必带 reason
    with pytest.raises(MethodError):
        await call_method("case.verdict", {"proposal_id": str(props[2].id), "verdict": "reject"})
    res = await call_method("case.verdict", {
        "proposal_id": str(props[2].id), "verdict": "reject", "reason": "幻觉引用",
    })
    assert res["updated"] == 1
    # defer：带 question_id
    qid = str(uuid.uuid4())
    res = await call_method("case.verdict", {
        "proposal_id": str(props[3].id), "verdict": "defer", "question_id": qid,
    })
    assert res["updated"] == 1

    await props[0].refresh_from_db()
    await props[1].refresh_from_db()
    await props[2].refresh_from_db()
    await props[3].refresh_from_db()
    assert props[0].status == "keep"
    assert props[1].status == "modify" and props[1].payload == {"title": "edited", "surface": "web"}
    assert props[2].status == "reject"
    assert props[2].provenance.get("reject_reason") == "幻觉引用"
    assert props[3].status == "defer" and str(props[3].question_id) == qid


@pytest.mark.asyncio
async def test_case_verdict_batch_and_invalid(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    p1 = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case", payload={"title": "a"},
        source_agent="case_gen", review_state="machine_pending", status="proposed",
    )
    p2 = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case", payload={"title": "b"},
        source_agent="case_gen", review_state="machine_pending", status="keep",  # 已判决
    )
    res = await call_method("case.verdict", {
        "proposal_ids": [str(p1.id), str(p2.id), str(uuid.uuid4())],
        "verdict": "keep", "decided_by": str(uuid.uuid4()),
    })
    assert res["updated"] == 1
    assert len(res["skipped"]) == 2  # p2 已判决 + 一个不存在的
    # 非法 verdict
    with pytest.raises(MethodError):
        await call_method("case.verdict", {"proposal_id": str(p1.id), "verdict": "maybe"})


# ── 6. finalize_import ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_finalize_import_cases_and_idempotent(tortoise_db):
    from testing_platform.assets.models import TestRequirement

    pid = uuid.uuid4()
    creator = uuid.uuid4()
    req = await TestRequirement.create(
        project_id=pid, code="REQ-001", title="登录改造", module="auth",
        gen_status="not_started",
    )
    tp = await _make_confirmed_testpoint(pid, "TP-0001", surfaces=("api",),
                                         requirement_id=req.id)
    run = await orch.start_run(project_id=pid, created_by=creator, run_type="exec")
    prop_keep = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case",
        payload={"title": "kept case", "surface": "api", "testpoint_code": "TP-0001",
                 "steps": [{"seq": 1, "action": "a", "expected": "e"}],
                 "expected": {"status_code": 200}, "preconditions": ["pc"],
                 "slot_key": "flow:TP-0001:api:v1"},
        source_agent="case_gen", surface="api", testpoint_id=tp.id,
        evidence_class="static", review_state="scored", status="keep",
    )
    prop_reject = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case",
        payload={"title": "rejected case"}, source_agent="case_gen",
        surface="api", testpoint_id=tp.id, evidence_class="claimed",
        review_state="scored", status="reject",
    )

    res = await orch.finalize_import(run)
    assert res["cases_imported"] == 1
    assert res["datasets_imported"] == 0
    # reject 的进 skipped
    assert any(s["id"] == str(prop_reject.id) for s in res["skipped"])
    # 正式表有一行，带 surface/testpoint_id/requirement_id
    cases = await TestCase.all()
    assert len(cases) == 1
    c = cases[0]
    assert c.code == "TC-0001" and c.code.startswith("TC-")
    assert c.surface == "api"
    assert c.testpoint_id == tp.id
    assert c.requirement_id == req.id
    assert c.origin == "ai_generated"
    assert c.status == "draft"
    assert c.confirm_source == "doc"
    assert c.confirm_ref.get("slot_key") == "flow:TP-0001:api:v1"
    assert str(c.created_by) == str(creator)
    assert c.steps == [{"seq": 1, "action": "a", "expected": "e"}]
    # 提案打 imported_at
    await prop_keep.refresh_from_db()
    assert prop_keep.provenance.get("imported_at")
    # 重跑幂等：不重复建行
    res2 = await orch.finalize_import(run)
    assert res2["cases_imported"] == 0
    assert await TestCase.all().count() == 1


@pytest.mark.asyncio
async def test_finalize_import_dataset_backfill(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="full")
    # dataset 提案（payload 带 code 作为引用键）
    ds_prop = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="dataset",
        payload={"code": "login-params", "schema": {"user": "string"},
                 "rows": [{"user": "u1"}, {"user": "u2"}]},
        source_agent="case_gen", review_state="scored", status="keep",
    )
    # 引用该数据集的 case 提案
    case_prop = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case",
        payload={"title": "param case", "surface": "api",
                 "dataset": "login-params"},
        source_agent="case_gen", surface="api",
        evidence_class="claimed", review_state="scored", status="modify",
    )

    res = await orch.finalize_import(run)
    assert res["datasets_imported"] == 1
    assert res["cases_imported"] == 1
    datasets = await TestDataset.all()
    assert len(datasets) == 1
    assert datasets[0].code.startswith("DS-")
    assert datasets[0].schema == {"user": "string"}
    assert datasets[0].rows == [{"user": "u1"}, {"user": "u2"}]
    # 回填 dataset_id
    cases = await TestCase.all()
    assert len(cases) == 1 and cases[0].dataset_id == datasets[0].id
    # 幂等
    res2 = await orch.finalize_import(run)
    assert res2["datasets_imported"] == 0 and res2["cases_imported"] == 0
    assert await TestDataset.all().count() == 1


# ── 7. coverage.summary ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_coverage_summary_matrix(tortoise_db):
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="exec")
    await _make_confirmed_testpoint(pid, "TP-0001", surfaces=("api", "web"),
                                    axes=("C01", "C03"))
    await _make_confirmed_testpoint(pid, "TP-0002", surfaces=("api",), axes=("C01",))

    res = await call_method("coverage.summary", {"run_id": str(run.id)})
    matrix = res["matrix"]
    # C01 × api = 2 个点，C01 × web = 1，C03 × api = 1，C03 × web = 1
    assert matrix["C01"]["api"] == 2
    assert matrix["C01"]["web"] == 1
    assert matrix["C03"]["api"] == 1 and matrix["C03"]["web"] == 1
    # lit = Σ count = 5
    assert res["lit"] == 5
    # gap：已出现轴 × 已声明端里没亮过的格（C03 没有 web? 有——C03 api+web 都亮）
    # 每个轴都覆盖了 api+web → gap 空
    assert res["gap"] == {}


@pytest.mark.asyncio
async def test_coverage_summary_gap_and_scope(tortoise_db):
    from testing_platform.assets.models import TestRequirement

    pid = uuid.uuid4()
    req = await TestRequirement.create(
        project_id=pid, code="REQ-001", title="R1", gen_status="not_started",
    )
    run = await orch.start_run(
        project_id=pid, created_by=uuid.uuid4(), run_type="full",
        scope={"requirement_ids": [str(req.id)]},
    )
    # 范围内：C01 只覆盖 api（web 是 gap）
    await _make_confirmed_testpoint(pid, "TP-IN", surfaces=("api",), axes=("C01",),
                                    requirement_id=req.id)
    # 范围外：不计入
    await _make_confirmed_testpoint(pid, "TP-OUT", surfaces=("app",), axes=("C02",))

    res = await call_method("coverage.summary", {"run_id": str(run.id)})
    matrix = res["matrix"]
    assert matrix == {"C01": {"api": 1}}
    assert res["lit"] == 1
    # gap：C01 声明了 web? 不——surfaces_seen 只含 api（TP-OUT 的 app 不进）
    # 已出现轴 C01 × 已声明端 {api} → C01/api 亮 → gap 空
    assert res["gap"] == {}

    # 无 scope run：project 全量——app 端进 surfaces_seen，C01 无 app → gap
    run2 = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="exec")
    res2 = await call_method("coverage.summary", {"run_id": str(run2.id)})
    m2 = res2["matrix"]
    assert m2["C01"]["api"] == 1
    assert m2["C02"]["app"] == 1
    # 已声明端 = {api, app}（surfaces_seen 只含出现过测试点的端，无 web）
    assert res2["surfaces"] == ["api", "app"]
    assert res2["gap"] == {"C01": ["app"], "C02": ["api"]}


# ── 4b. 默认历史切片 = 风格锚召回（§环节11，e2e 接线）─────────────
@pytest.mark.asyncio
async def test_run_case_generation_default_history_recall_golden(tortoise_db):
    """不传 history_provider 时，默认切片应含同模块 golden 用例（仿写先例）。"""
    from testing_platform.assets.models import TestCase, TestRequirement

    pid = uuid.uuid4()
    req = await TestRequirement.create(
        project_id=pid, code="REQ-H1", title="登录", module="auth",
        gen_status="gate1_passed",
    )
    golden_case = await TestCase.create(
        project_id=pid, code="TC-G1", title="golden 登录正向",
        intent="合法凭证登录成功", module="auth", origin="ai_generated",
        confirm_source="doc", confirm_ref={"golden": True},
        steps=[{"seq": 1, "action": "输正确密码", "expected": None}],
        expected={"status_code": 200},
        created_by=uuid.uuid4(),
    )
    run = await orch.start_run(
        project_id=pid, created_by=uuid.uuid4(), run_type="full",
        scope={"requirement_ids": [str(req.id)]},
    )
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)
    await _make_confirmed_testpoint(pid, "TP-H1", requirement_id=req.id)

    prompts: list[str] = []
    async def llm_call(prompt):
        prompts.append(prompt)
        return [_case_payload_llm("new case")]

    res = await orch.run_case_generation(run, llm_call=llm_call)
    assert res["created"] >= 1
    joined = "\n".join(prompts)
    assert "Golden cases" in joined                       # 风格锚段进了提示词
    assert "TC-G1" in joined and "golden 登录正向" in joined
    _ = golden_case


@pytest.mark.asyncio
async def test_run_case_generation_history_provider_override(tortoise_db):
    """显式传 history_provider 时不走默认召回（调用方全权接管）。"""
    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4(), run_type="exec")
    await TestGenRun.filter(id=run.id).update(status="points_proposed")
    run = await TestGenRun.get(id=run.id)
    await _make_confirmed_testpoint(pid)

    prompts: list[str] = []
    async def llm_call(prompt):
        prompts.append(prompt)
        return [_case_payload_llm("x")]

    async def custom(_task):
        return "CUSTOM SLICE MARKER"

    res = await orch.run_case_generation(run, llm_call=llm_call, history_provider=custom)
    assert res["created"] >= 1
    joined = "\n".join(prompts)
    assert "CUSTOM SLICE MARKER" in joined
    assert "Golden cases" not in joined                  # 默认召回未介入
