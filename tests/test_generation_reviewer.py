"""评审三道闸 · reviewer.py 纯函数 + review_flow.py 编排（25 号文 §环节6 P1）。

覆盖任务要求的五块：
1. machine_pass 复用 gates（mock api_lookup 缺参数 → machine_rejected；干净用例 → machine_pending）
2. merge_dual 五情形（agree×1 / split×2 / inconclusive×2）
3. score_dimensions 权重合成 + 任一维 0 封顶 69
4. ratchet_check 拒收（断言条数变少 / expected 层缺失；不丢断言则过）
5. review_flow 假 LLM 端到端（SQLite 内存库：状态迁移 + 重写版本链 + slot_key ":v2"）

纯函数部分不联网、不启 DB；编排部分走 SQLite 内存库 + 注入假 LLM（同
``test_generation_gates.py`` / ``test_generation_orchestrator.py`` 的形态）。
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation import reviewer as rv
from testing_platform.generation import review_flow as rf
from testing_platform.quality.errors import ValidationIssue

# ══════════════════════════════════════════════════════════════════
# 共享夹具：干净用例 + 假 api_lookup / 假 LLM / 假规则提供方
# ══════════════════════════════════════════════════════════════════

_API_1 = {
    "id": "API-1",
    "method": "POST",
    "path": "/api/v1/orders",
    "params": [
        {"name": "page", "in": "query", "type": "integer"},
        {"name": "size", "in": "query", "type": "integer"},
    ],
    "request_body": {"type": "object", "properties": {
        "product_id": {"type": "string"},
        "quantity": {"type": "integer"},
    }},
}


def _lookup_factory(docs: dict):
    async def _lookup(api_id: str):
        return docs.get(api_id)
    return _lookup


def _clean_case(**overrides) -> dict:
    """一个过了全部机器门的干净用例（同 test_generation_gates._clean_case）。"""
    base = {
        "api_id": "API-1",
        "intent": "校验下单接口在正常参数下返回 200（见 mc_test_api 契约）",
        "surface": "api",
        "evidence_class": "inferred",
        "scenario_tag": ["token_invalid", "cross_tenant"],
        "applied_rules": [
            {"rule_id": "HR-1", "status": "followed", "why": "已带越权场景"},
        ],
        "expected": {"status_code": 200, "key_fields": ["order_id"]},
        "params": [{"name": "page", "value": "{{env.page}}"}],
        "confirm_ref": {"api_id": "API-1"},
        "slot_key": "api:API-1:page:boundary:min",
    }
    base.update(overrides)
    return base


def _gate_input(case: dict) -> dict:
    return {"kind": "case", "payload": case,
            "surface": case.get("surface"), "evidence_class": case.get("evidence_class")}


def _rev(verdict: str, reason: str = "r") -> dict:
    return {"verdict": verdict, "reason": reason, "evidence_class": "static"}


def _llm(payload: dict):
    """假 LLM：忽略 prompt，吐固定 JSON 文本。"""
    async def call(_prompt: str) -> str:
        return json.dumps(payload, ensure_ascii=False)
    return call


async def _empty_rules(_run):
    return []


async def _boom_llm(_prompt):
    raise AssertionError("不该调用双评 LLM")


# ══════════════════════════════════════════════════════════════════
# 1. machine_pass（复用 gates）
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_machine_pass_missing_api_is_rejected():
    """mock api_lookup 缺参数（查不到引用的接口）→ machine_rejected。"""
    case = _clean_case(api_id="API-GHOST")
    state, issues = await rv.machine_pass(
        _gate_input(case),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[],
    )
    assert state == "machine_rejected"
    assert any(i.rule == "MG_H1_api_not_found" and i.severity == "error" for i in issues)


@pytest.mark.asyncio
async def test_machine_pass_param_missing_from_api_is_rejected():
    """参数名不在接口 params[]/request_body → MG_H2 error → machine_rejected。"""
    case = _clean_case(params=[{"name": "ghost_param", "value": 1}])
    state, issues = await rv.machine_pass(
        _gate_input(case),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[],
    )
    assert state == "machine_rejected"
    assert any(i.rule == "MG_H2_param_not_in_api" for i in issues)


@pytest.mark.asyncio
async def test_machine_pass_clean_case_is_pending():
    """干净用例（无 error）→ machine_pending，放行双评。"""
    state, issues = await rv.machine_pass(
        _gate_input(_clean_case()),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        existing_slot_keys=set(),
        active_hard_rules=[],
    )
    assert state == "machine_pending"
    assert not any(i.severity == "error" for i in issues)


# ══════════════════════════════════════════════════════════════════
# 2. merge_dual 五情形
# ══════════════════════════════════════════════════════════════════

def test_merge_dual_both_passed_agree():
    m = rv.merge_dual(_rev("passed"), _rev("passed"))
    assert m["consensus"] == "agree"
    assert m["a"]["verdict"] == "passed" and m["b"]["verdict"] == "passed"


def test_merge_dual_a_passed_b_failed_split():
    assert rv.merge_dual(_rev("passed"), _rev("failed"))["consensus"] == "split"


def test_merge_dual_a_failed_b_passed_split():
    assert rv.merge_dual(_rev("failed"), _rev("passed"))["consensus"] == "split"


def test_merge_dual_a_inconclusive_inconclusive():
    assert rv.merge_dual(_rev("inconclusive"), _rev("passed"))["consensus"] == "inconclusive"


def test_merge_dual_b_blocked_counts_inconclusive():
    """blocked 是弃权近邻 → 并入 inconclusive（文档「弃权=inconclusive 进人」）。"""
    assert rv.merge_dual(_rev("passed"), _rev("blocked"))["consensus"] == "inconclusive"


def test_merge_dual_never_arbitrates():
    """分歧不仲裁：split 结果原样带两份意见，不产生第三方裁决。"""
    m = rv.merge_dual(_rev("failed", "业务不对"), _rev("failed", "写得不对"))
    assert m["consensus"] == "split"
    assert set(m.keys()) == {"a", "b", "consensus"}
    assert m["a"]["reason"] == "业务不对" and m["b"]["reason"] == "写得不对"


# ══════════════════════════════════════════════════════════════════
# 3. score_dimensions 权重与封顶
# ══════════════════════════════════════════════════════════════════

def test_score_dimensions_weights_sum_to_100():
    assert rv.SCORE_DIMENSIONS == {
        "coverage": 25, "assertion": 25, "exception_ratio": 20,
        "executability": 15, "maintainability": 15,
    }
    assert sum(rv.SCORE_DIMENSIONS.values()) == 100


def test_score_dimensions_all_full_is_100():
    full = {k: 100 for k in rv.SCORE_DIMENSIONS}
    assert rv.score_dimensions(full) == {"total": 100, "capped": False}


def test_score_dimensions_weighted_composite():
    """仅 coverage=100 其余 0 → 加权 25 分（权重生效），零维封顶不抬高。"""
    r = rv.score_dimensions({"coverage": 100, "assertion": 0,
                             "exception_ratio": 0, "executability": 0,
                             "maintainability": 0})
    assert r == {"total": 25, "capped": True}


def test_score_dimensions_zero_dim_caps_at_69():
    """仅 coverage 一维 0、其余满分 → 加权 75 被封顶到 69（任一维 0 → 封顶 69）。"""
    r = rv.score_dimensions({"coverage": 0, "assertion": 100,
                             "exception_ratio": 100, "executability": 100,
                             "maintainability": 100})
    assert r == {"total": 69, "capped": True}


def test_score_dimensions_side_derivation():
    """A 侧过（coverage+exception=45 权重）B 侧挂（其余 0）→ 45。"""
    out = rv.score_dimensions(rv.derive_breakdown("passed", "failed"))
    assert out == {"total": 45, "capped": True}
    assert out["total"] < rv.REWORK_SCORE


def test_thresholds():
    assert (rv.PASS_SCORE, rv.REWORK_SCORE, rv.ZERO_DIM_CAP) == (85, 70, 69)


# ══════════════════════════════════════════════════════════════════
# 4. ratchet_check 拒收
# ══════════════════════════════════════════════════════════════════

def test_ratchet_rejects_fewer_assertions():
    old = {"expected": {"a": 1, "b": 2, "c": 3}}
    new = {"expected": {"a": 1, "b": 2}}
    assert rv.ratchet_check(old, new), "断言条数 3→2 必须拒收"


def test_ratchet_rejects_missing_expected_layer():
    v = rv.ratchet_check({"expected": {"status_code": 200}}, {"steps": []})
    assert any("expected" in s for s in v)


def test_ratchet_rejects_empty_rewrite():
    """重写者没吐 JSON（空 payload）→ expected 缺失 + 断言变少，两条都命中。"""
    v = rv.ratchet_check(_clean_case(), {})
    assert len(v) >= 2


def test_ratchet_accepts_equal_or_more_assertions():
    old = {"expected": {"a": 1, "b": 2}}
    assert rv.ratchet_check(old, {"expected": {"a": 1, "b": 2}}) == []
    assert rv.ratchet_check(old, {"expected": {"a": 1, "b": 2, "c": 3}}) == []


# ══════════════════════════════════════════════════════════════════
# 5. 双评提示词 / 解析器 / 回炉任务判定
# ══════════════════════════════════════════════════════════════════

def test_build_review_prompt_side_division():
    pa = rv.build_review_prompt("a", {"x": 1})
    pb = rv.build_review_prompt("b", {"x": 1})
    assert "COVERAGE" in pa and "ASSERTION SPECIFICITY" not in pa
    assert "ASSERTION SPECIFICITY" in pb and "TAUTOLOGY" not in pb
    assert '"verdict": "passed" | "failed" | "inconclusive"' in pa
    assert '"x": 1' in pa  # payload JSON 嵌入


def test_parse_review_output_json_fenced():
    raw = ('```json\n{"verdict": "failed", "reason": "断言只有 status=200", '
           '"evidence_class": "static"}\n```')
    p = rv.parse_review_output(raw)
    assert p["verdict"] == "failed"
    assert "status=200" in p["reason"]
    assert p["evidence_class"] == "static"


def test_parse_review_output_garbage_is_inconclusive():
    """解析不出 → inconclusive（弃权进人，宁缺毋滥）。"""
    assert rv.parse_review_output("I am not sure")["verdict"] == "inconclusive"
    assert rv.parse_review_output(None)["verdict"] == "inconclusive"


def test_rewrite_task_trigger_matrix():
    low = {"id": "P-1", "review_state": "scored", "score": 60.0}
    assert rv.rewrite_task(low, [])["proposal_id"] == "P-1"     # 低分

    rejected = {"id": "P-2", "review_state": "machine_rejected", "score": None}
    assert rv.rewrite_task(rejected, []) is not None            # 机器打回

    split = {"id": "P-3", "review_state": "scored", "score": 90.0,
             "reviews": {"consensus": "split"}}
    assert rv.rewrite_task(split, []) is not None               # 分歧

    ok = {"id": "P-4", "review_state": "scored", "score": 92.0,
          "reviews": {"consensus": "agree"}}
    assert rv.rewrite_task(ok, []) is None                      # 无需回炉


# ══════════════════════════════════════════════════════════════════
# 6. review_flow 端到端（SQLite 内存库 + 假 LLM）
# ══════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": [
            "testing_platform.generation.models",
        ]},
    )
    await Tortoise.generate_schemas(safe=True)
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_run(project_id):
    from testing_platform.generation.models import TestGenRun

    return await TestGenRun.create(
        project_id=project_id, run_type="full", status="evaluating",
        scope={}, progress={}, budget={}, decisions=[], config={},
    )


async def _make_case_proposal(run, **overrides):
    from testing_platform.generation.models import TestProposal

    fields = dict(
        run_id=run.id, project_id=run.project_id, kind="case",
        payload=_clean_case(),
        source_agent="case_gen_api",
        surface="api",
        evidence_class="inferred",
        review_state="machine_pending",
        review_round=0,
        version=1,
        slot_key="api:API-1:page:boundary:min",
        status="proposed",
    )
    fields.update(overrides)
    return await TestProposal.create(**fields)


@pytest.mark.asyncio
async def test_review_flow_end_to_end_scored(tortoise_db):
    """干净用例 + A/B 双 passed → 三道闸过 → review_state=scored + 五维 100。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    prop = await _make_case_proposal(run)

    counts = await rf.review_run_proposals(
        run,
        llm_a=_llm({"verdict": "passed", "reason": "覆盖齐全",
                    "evidence_class": "static"}),
        llm_b=_llm({"verdict": "passed", "reason": "断言具体可执行",
                    "evidence_class": "static"}),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )

    assert counts == {"scored": 1, "machine_rejected": 0, "split": 0, "inconclusive": 0, "errors": 0}

    row = await TestProposal.get(id=prop.id)
    assert row.review_state == "scored"
    assert row.reviews["consensus"] == "agree"
    assert row.score == 100.0
    assert row.score_breakdown == {
        "coverage": 100, "assertion": 100, "exception_ratio": 100,
        "executability": 100, "maintainability": 100,
    }


@pytest.mark.asyncio
async def test_review_flow_machine_rejected_skips_llm(tortoise_db):
    """幻觉引用（api 不存在）→ machine_rejected，不进双评（LLM 不会被调用）。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    prop = await _make_case_proposal(run, payload=_clean_case(api_id="API-GHOST"))

    counts = await rf.review_run_proposals(
        run,
        llm_a=_boom_llm, llm_b=_boom_llm,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )

    assert counts["machine_rejected"] == 1 and counts["scored"] == 0
    row = await TestProposal.get(id=prop.id)
    assert row.review_state == "machine_rejected"
    assert row.reviews["consensus"] is None
    assert any(i["rule"] == "MG_H1_api_not_found" for i in row.reviews["machine_issues"])
    assert row.score is None and row.score_breakdown == {}


@pytest.mark.asyncio
async def test_review_flow_split_consensus(tortoise_db):
    """A passed / B failed → consensus=split → 进人（scored + split 徽章），不设仲裁。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    prop = await _make_case_proposal(run)

    counts = await rf.review_run_proposals(
        run,
        llm_a=_llm({"verdict": "passed", "reason": "业务覆盖对"}),
        llm_b=_llm({"verdict": "failed", "reason": "断言只有 status=200"}),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )

    assert counts == {"scored": 1, "machine_rejected": 0, "split": 1, "inconclusive": 0, "errors": 0}
    row = await TestProposal.get(id=prop.id)
    assert row.review_state == "scored"          # 分歧进人（门②逐条）
    assert row.reviews["consensus"] == "split"   # 徽章从 consensus 出
    # 权重合成：B 侧三维归零 → 45（任一维 0，封顶不动）
    assert row.score == 45.0
    assert row.score_breakdown["assertion"] == 0
    _ = prop


@pytest.mark.asyncio
async def test_review_flow_inconclusive_consensus(tortoise_db):
    """B 弃权 → consensus=inconclusive → 进人（scored + inconclusive 徽章）。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    prop = await _make_case_proposal(run)

    counts = await rf.review_run_proposals(
        run,
        llm_a=_llm({"verdict": "passed"}),
        llm_b=_llm({"verdict": "inconclusive", "reason": "缺环境信息"}),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )
    assert counts["inconclusive"] == 1 and counts["scored"] == 1
    row = await TestProposal.get(id=prop.id)
    assert row.reviews["consensus"] == "inconclusive"
    _ = prop


@pytest.mark.asyncio
async def test_review_flow_second_pass_is_idempotent(tortoise_db):
    """第一趟把 machine_pending 消费光；第二趟不再命中（无双评调用、无新增行）。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    await _make_case_proposal(run)

    await rf.review_run_proposals(
        run, llm_a=_llm({"verdict": "passed"}), llm_b=_llm({"verdict": "passed"}),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )
    second = await rf.review_run_proposals(
        run, llm_a=_boom_llm, llm_b=_boom_llm,
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )
    assert second == {"scored": 0, "machine_rejected": 0, "split": 0, "inconclusive": 0, "errors": 0}
    assert await TestProposal.filter(run_id=run.id, kind="case").count() == 1


# ── apply_rework：重写版本链 + slot_key :v2 ─────────────────────────

@pytest.mark.asyncio
async def test_apply_rework_version_chain_and_slot_v2(tortoise_db):
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(run, review_state="need_rework",
                                       review_round=0, version=1)

    fixed = _clean_case(expected={"status_code": 200, "key_fields": ["order_id"],
                                  "business_code": "0000"})
    counts = await rf.apply_rework(run, _llm(fixed))

    assert counts == {"rewritten": 1, "ratchet_rejected": 0, "deferred": 0, "errors": 0}

    child = await TestProposal.get_or_none(rewrite_of=parent.id)
    assert child is not None
    # 版本链
    assert child.version == 2
    assert str(child.rewrite_of) == str(parent.id)
    assert child.review_round == 1
    # slot_key 带 :v2 后缀（§门② 步骤6 让位口径）
    assert child.slot_key == "api:API-1:page:boundary:min:v2"
    # 重写行重新进三道闸
    assert child.review_state == "machine_pending"
    assert child.status == "proposed"
    # 父行不动（原行保留，链上取最新——§6.2 回收协议）
    parent_after = await TestProposal.get(id=parent.id)
    assert parent_after.review_state == "need_rework"
    assert parent_after.version == 1


@pytest.mark.asyncio
async def test_apply_rework_ratchet_violation_marks_new_row_rejected(tortoise_db):
    """重写把断言改少了 → 棘轮拒收 → 新行 machine_rejected + round+1。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(
        run, review_state="machine_rejected", review_round=0, version=1,
        payload=_clean_case(expected={"a": 1, "b": 2, "c": 3}),
    )

    weaker = _clean_case(expected={"a": 1})
    counts = await rf.apply_rework(run, _llm(weaker))

    assert counts["ratchet_rejected"] == 1 and counts["rewritten"] == 0

    child = await TestProposal.get_or_none(rewrite_of=parent.id)
    assert child.review_state == "machine_rejected"
    assert child.review_round == 1
    assert child.version == 2
    assert child.slot_key.endswith(":v2")
    _ = parent


@pytest.mark.asyncio
async def test_apply_rework_defers_after_three_rounds(tortoise_db):
    """review_round 已达 3 → 不再重写，status=defer 进人。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(run, review_state="need_rework",
                                       review_round=3, version=3)

    counts = await rf.apply_rework(run, _boom_llm)

    assert counts == {"rewritten": 0, "ratchet_rejected": 0, "deferred": 1, "errors": 0}
    row = await TestProposal.get(id=parent.id)
    assert row.status == "defer"
    assert row.review_state == "need_rework"  # 评审态留痕不变（defer 是 status 列判决态）
    assert "转人工" in str(row.reviews.get("defer_reason"))


@pytest.mark.asyncio
async def test_apply_rework_idempotent_per_chain(tortoise_db):
    """同一提案已生成后继版本后，第二趟不重复重写（幂等）。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(run, review_state="need_rework",
                                       review_round=0, version=1)
    first = await rf.apply_rework(run, _llm(_clean_case()))
    second = await rf.apply_rework(run, _llm(_clean_case()))

    assert first["rewritten"] == 1
    assert second["rewritten"] == 0
    assert await TestProposal.filter(run_id=run.id, kind="case").count() == 2  # 父 + 一个后继
    _ = parent


def test_rewrite_prompt_lists_issues():
    """build_rewrite_prompt 把问题逐条列进提示词（英文契约 + 问题清单）。"""
    issue = ValidationIssue(path="$.expected", rule="WR1_weak_assertion",
                            msg="断言只有 status=200", severity="error",
                            hint="补 key_fields / 业务码")
    prompt = rf.build_rewrite_prompt({"api_id": "API-1"}, [issue])
    assert "ISSUES TO FIX" in prompt
    assert "WR1_weak_assertion" in prompt
    assert "断言只有 status=200" in prompt
    assert '"api_id": "API-1"' in prompt


# ── e2e 实测修正回归：批次单条隔离 / 重写稿归一（EV4 死循环根因）────

@pytest.mark.asyncio
async def test_review_flow_batch_isolates_proposal_llm_error(tortoise_db):
    """A 侧对第一条提案抛错 → 该条保持 machine_pending + review_error 留痕，
    第二条照常评完（一条网关超时不许杀整批——e2e 实测修正）。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    p1 = await _make_case_proposal(run, slot_key="api:API-1:page:boundary:p1")
    p2 = await _make_case_proposal(run, slot_key="api:API-1:page:boundary:p2")

    seen = {"n": 0}

    async def flaky_a(prompt: str):
        seen["n"] += 1
        if seen["n"] == 1:
            raise TimeoutError("gateway 429 no available account")
        return json.dumps({"verdict": "passed", "reason": "ok"})

    counts = await rf.review_run_proposals(
        run, llm_a=flaky_a, llm_b=_llm({"verdict": "passed"}),
        api_lookup=_lookup_factory({"API-1": _API_1}),
        hard_rules_provider=_empty_rules,
    )

    assert counts["errors"] == 1 and counts["scored"] == 1
    row1 = await TestProposal.get(id=p1.id)
    assert row1.review_state == "machine_pending"          # 可重跑续评（不假装通过）
    assert "TimeoutError" in str(row1.reviews.get("review_error"))
    assert row1.score is None
    row2 = await TestProposal.get(id=p2.id)
    assert row2.review_state == "scored" and row2.score == 100.0


@pytest.mark.asyncio
async def test_review_flow_batch_isolates_api_lookup_error(tortoise_db):
    """api_lookup（机器门依赖）抛错同样单条隔离，不杀批次。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    p1 = await _make_case_proposal(run, slot_key="api:API-1:page:boundary:p1")
    p2 = await _make_case_proposal(run, slot_key="api:API-1:page:boundary:p2")

    calls = {"n": 0}

    async def flaky_lookup(api_id: str):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db pool timeout")
        return _API_1

    counts = await rf.review_run_proposals(
        run, llm_a=_llm({"verdict": "passed"}), llm_b=_llm({"verdict": "passed"}),
        api_lookup=flaky_lookup,
        hard_rules_provider=_empty_rules,
    )
    assert counts["errors"] == 1
    row1 = await TestProposal.get(id=p1.id)
    assert row1.review_state == "machine_pending"
    row2 = await TestProposal.get(id=p2.id)
    assert row2.review_state == "scored"


@pytest.mark.asyncio
async def test_apply_rework_rederives_evidence_class_not_inherited(tortoise_db):
    """父稿 evidence_class=static（basis=fact），重写稿没声明 basis →
    子行 evidence_class 必须是 claimed（据新稿重算，不继承父集）。

    继承会把「直证带来源」义务带给重写稿 → EV4 逐串索证 → 重写死循环
    （e2e 实测 60.9% 打回率根因之一）。
    """
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(
        run, review_state="need_rework", evidence_class="static",
        payload=_clean_case(basis="fact", evidence_class="static"),
    )

    rewrite = _clean_case()  # 无 basis / evidence_class
    rewrite.pop("evidence_class", None)
    counts = await rf.apply_rework(run, _llm(rewrite))

    assert counts["rewritten"] == 1
    child = await TestProposal.get_or_none(rewrite_of=parent.id)
    assert child is not None
    assert child.evidence_class == "claimed"          # 不继承 static
    assert child.payload["evidence_class"] == "claimed"
    assert child.payload["basis"] == "claimed"


@pytest.mark.asyncio
async def test_apply_rework_normalizes_shape_from_loose_rewrite(tortoise_db):
    """重写 LLM 吐松形态（applied_rules=字符串、expected=列表、preconditions=单串）
    → 归一到 case 契约形态（dict/list[dict]），applied_rules 保留在行列上。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(
        run, review_state="need_rework",
        payload=_clean_case(expected={"a": 1, "b": 2}),
    )

    loose = _clean_case(
        expected=["下单返回 200", "响应体含 order_id", "业务码 0000"],  # 棘轮口径 3≥2
        applied_rules=[{"rule_id": "HR-1"}],                          # 缺 disposition/why
        preconditions="已登录租户A",                                    # 单串
        basis="inferred",
    )
    counts = await rf.apply_rework(run, _llm(loose))
    assert counts["rewritten"] == 1

    child = await TestProposal.get_or_none(rewrite_of=parent.id)
    assert isinstance(child.payload["expected"], dict)
    assert child.payload["expected"]["assert_1"] == "下单返回 200"
    assert child.payload["applied_rules"] == [
        {"rule_id": "HR-1", "disposition": "", "why": "", "status": ""},
    ]
    assert child.applied_rules == child.payload["applied_rules"]      # 行列取新稿
    assert child.payload["preconditions"] == ["已登录租户A"]
    assert child.payload["testpoint_code"] == ""                      # 父稿也空 → 空
    assert child.evidence_class == "inferred"                         # 据新稿 basis


@pytest.mark.asyncio
async def test_apply_rework_keeps_identity_fields_from_parent(tortoise_db):
    """重写稿不重述 surface/testpoint_code → 从父稿补身份字段（机器门/反查链不断）。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(
        run, review_state="need_rework", surface="web",
        payload=_clean_case(surface="web", testpoint_code="TP-9"),
    )

    bare = {"title": "重写版", "intent": "改后的意图",
            "expected": {"status_code": 200, "key_fields": ["order_id"]},
            "applied_rules": []}
    counts = await rf.apply_rework(run, _llm(bare))
    assert counts["rewritten"] == 1

    child = await TestProposal.get_or_none(rewrite_of=parent.id)
    assert child.surface == "web"                          # 行列本来带（prop 继承）
    assert child.payload["surface"] == "web"               # payload 归一也从父补
    assert child.payload["testpoint_code"] == "TP-9"
    # 断言层：子稿 2 叶 vs 父 2 叶 → 棘轮过；父无 expected 之外的断言不受影响


@pytest.mark.asyncio
async def test_apply_rework_rewrite_error_isolated(tortoise_db):
    """重写 LLM 抛错 → 单条计 errors、父行保持回炉态可下轮续跑、不建子行。"""
    from testing_platform.generation.models import TestProposal

    run = await _make_run(uuid.uuid4())
    parent = await _make_case_proposal(run, review_state="need_rework")

    async def boom(_prompt):
        raise TimeoutError("rewrite gateway down")

    counts = await rf.apply_rework(run, boom)
    assert counts == {"rewritten": 0, "ratchet_rejected": 0, "deferred": 0, "errors": 1}
    row = await TestProposal.get(id=parent.id)
    assert row.review_state == "need_rework"
    assert "TimeoutError" in str(row.reviews.get("rewrite_error"))
    assert await TestProposal.filter(run_id=run.id, kind="case").count() == 1
