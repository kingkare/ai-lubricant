"""缺陷闭环 flow 测试（解耦改版：测试域自持 ``mc_test_bug``，与 coding 无关）。

覆盖：
* fingerprint 去重 —— 同指纹建两次 → 同 bug_id，主表只一行；
* 根因门 —— ``evidence_chain`` 空抛结构化错误，有则写入；
* 缺陷模式沉淀 —— 首建 hit_count=1，再命中 +1；
* 失败分流映射 —— ``failure_kind`` → 处理动作（纯函数）；
* 表注册实证 —— ``mc_test_bug`` / root_cause / pattern 三表存在，**且不依赖
  ``mc_project_issues``**（测试缺陷与 coding 解耦的证明）。

无 PG 依赖（sqlite::memory:，Tortoise 建表）。**不再 mock project_service**——
bug 落测试域自己的表，不经 coding 平台。
运行：``python -m pytest tests/test_defects_flow.py -q``
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

from testing_platform.defects import (  # noqa: E402
    DefectValidationError,
    RootCauseEvidenceRequiredError,
    TestBug,
    TestBugPattern,
    TestBugRootCause,
    attach_root_cause,
    classify_failure,
    compute_fingerprint,
    create_bug_from_failure,
    find_bug_by_fingerprint,
    get_bug,
    list_bugs,
    upsert_pattern,
)
from testing_platform.defects.triage import (  # noqa: E402
    ACTION_CREATE_BUG,
    ACTION_DEPRECATE_CASE,
    ACTION_MARK_FLAKY,
    ACTION_SELF_HEAL,
    FAILURE_KINDS,
)


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:（仅缺陷域三表，不注册平台 issue 表）。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.defects.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _make_bug(fingerprint="FP-" + "x" * 8, **overrides):
    """建一个 bug（跑一遍完整 create 路径），返回 (bug_id, uid, pid)。"""
    uid, pid = uuid.uuid4(), uuid.uuid4()
    run_sid = uuid.uuid4()
    bug_id = await create_bug_from_failure(
        run_sid,
        overrides.get("title", "下单接口 500"),
        overrides.get("symptom", "POST /orders 返回 500"),
        fingerprint,
        repro={"steps": ["POST /orders"], "env": "staging", "data": {"amount": -1}, "rate": 1},
        case_refs=["TC-0001"],
        scenario_refs=["SCN-0001"],
        user_id=uid,
        project_id=pid,
        **{k: v for k, v in overrides.items() if k in ("from_run_id", "assignee_id")},
    )
    return bug_id, uid, pid


# ── 1) fingerprint 去重：同指纹两次 → 同 bug_id，主表只一行 ──
@pytest.mark.asyncio
async def test_fingerprint_dedup_creates_one_bug(tortoise_db):
    uid, pid, fp = uuid.uuid4(), uuid.uuid4(), "FP-" + "a" * 8

    first = await create_bug_from_failure(
        uuid.uuid4(), "下单 500", "POST /orders → 500", fp,
        repro={"env": "staging"}, case_refs=["TC-0001"], scenario_refs=["SCN-0001"],
        user_id=uid, project_id=pid,
    )
    second = await create_bug_from_failure(
        uuid.uuid4(), "下单 500（再次）", "POST /orders → 500", fp,
        repro={"env": "staging"}, case_refs=["TC-0001"], scenario_refs=["SCN-0001"],
        user_id=uid, project_id=pid,
    )

    assert first == second, "同 fingerprint 必须归并到同一 bug"
    assert await TestBug.all().count() == 1, "主表只应有一行（去重）"

    # 指纹查询直接暴露
    assert await find_bug_by_fingerprint(fp) == first
    assert await find_bug_by_fingerprint("FP-not-exist") is None

    # 不同指纹 → 新 bug
    other = await create_bug_from_failure(
        uuid.uuid4(), "退款 400", "POST /refunds → 400", "FP-" + "b" * 8,
        user_id=uid, project_id=pid,
    )
    assert other != first
    assert await TestBug.all().count() == 2


# ── 2) attach_root_cause：evidence_chain 空 → 抛结构化错误；有 → 成功 ──
@pytest.mark.asyncio
async def test_attach_root_cause_evidence_gate(tortoise_db):
    bug_id, _, _ = await _make_bug(fingerprint="FP-" + "c" * 8)

    # 根因门：无证据链 → 拒绝
    with pytest.raises(RootCauseEvidenceRequiredError) as ei:
        await attach_root_cause(bug_id, {"classification": "logic", "hypothesis": "空口断言"})
    assert ei.value.code == "root_cause_evidence_required"
    assert ei.value.field == "evidence_chain"
    assert ei.value.details["code"] == "root_cause_evidence_required"
    assert await TestBugRootCause.all().count() == 0, "被拒时不应落任何根因行"

    # 空列表 / 全空条目 同样被拒
    for bad in ({}, {"evidence_chain": []}, {"evidence_chain": [None, "", {}, []]}):
        with pytest.raises(RootCauseEvidenceRequiredError):
            await attach_root_cause(bug_id, bad)

    # 有证据链 → 写入（1:N，可迭代）
    rc1 = await attach_root_cause(bug_id, {
        "location": "services/order.py:88",
        "classification": "logic",
        "attributed_commit": "abc1234",
        "hypothesis": "金额校验在负数分支放行",
        "confidence": 0.9,
        "fix_suggestion": "amount <= 0 时抛 400",
        "healing_suggestion": "断言 expected 400 而非 500",
        "blast_radius": ["orders", "payments"],
        "evidence_chain": [{"step": "create_order", "assertion": "status_code=201"}],
    })
    rc2 = await attach_root_cause(bug_id, {
        "classification": "boundary",
        "hypothesis": "边界重测",
        "evidence_chain": [{"evidence": "log: ValueError"}],
    })
    assert rc1.bug_id == uuid.UUID(bug_id)
    assert rc1.confidence == 0.9
    assert await TestBugRootCause.filter(bug_id=uuid.UUID(bug_id)).count() == 2, "根因 1:N 可迭代"

    # classification 越界 → 结构化校验错
    with pytest.raises(DefectValidationError):
        await attach_root_cause(bug_id, {
            "classification": "made_up",
            "evidence_chain": [{"ref": "x"}],
        })


# ── 3) upsert_pattern：首建 hit_count=1，再命中 +1 ──
@pytest.mark.asyncio
async def test_upsert_pattern_hit_count(tortoise_db):
    p1 = await upsert_pattern("断言 status_code 不符 / 5xx", "logic",
                              prevention="上线前跑冒烟",
                              regression_template={"scenario": "SCN-5xx"})
    assert p1.hit_count == 1

    p2 = await upsert_pattern("断言 status_code 不符 / 5xx", "logic")
    assert p2.id == p1.id, "同症状签名必须 upsert 同一行"
    assert p2.hit_count == 2
    assert p2.prevention == "上线前跑冒烟", "未提供 prevention 时保留既有值"
    assert p2.regression_template == {"scenario": "SCN-5xx"}

    p3 = await upsert_pattern("断言 status_code 不符 / 5xx", "logic")
    assert p3.hit_count == 3
    assert await TestBugPattern.all().count() == 1

    # 第二个不同签名 → 独立行 hit_count=1
    q = await upsert_pattern("null 引用", "null")
    assert q.hit_count == 1 and q.id != p3.id
    assert await TestBugPattern.all().count() == 2

    # 非法根因分类 → 校验错
    with pytest.raises(DefectValidationError):
        await upsert_pattern("未知症状", "not_a_kind")
    with pytest.raises(DefectValidationError):
        await upsert_pattern("   ", "logic")


# ── 4) classify_failure 映射正确（纯函数/常量）──
@pytest.mark.asyncio
async def test_classify_failure_mapping():
    assert set(FAILURE_KINDS) == {"script_issue", "product_bug", "case_outdated", "env_flaky"}

    pb = classify_failure("product_bug")
    assert (pb.action, pb.counts_as_failure, pb.target) == (ACTION_CREATE_BUG, True, "bug")

    si = classify_failure("script_issue")
    assert (si.action, si.counts_as_failure, si.target) == (ACTION_SELF_HEAL, False, "scenario")

    co = classify_failure("case_outdated")
    assert (co.action, co.counts_as_failure, co.target) == (ACTION_DEPRECATE_CASE, False, "case")

    ef = classify_failure("env_flaky")
    assert (ef.action, ef.counts_as_failure, ef.target) == (ACTION_MARK_FLAKY, False, "run_scenario")

    # 只有 product_bug 计失败
    assert [classify_failure(k).counts_as_failure for k in FAILURE_KINDS] == [False, True, False, False]

    # 判据顺序 #1（确定性优先）：重跑即过 → 统一判 flaky
    assert classify_failure("product_bug", retry_passed=True).action == ACTION_MARK_FLAKY

    # 未知 kind → 校验错
    with pytest.raises(DefectValidationError):
        classify_failure("who_knows")


# ── 5) 表注册实证：三表在，且**不依赖 mc_project_issues** ──
@pytest.mark.asyncio
async def test_defect_tables_registered_without_coding_coupling(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}

    expected = {"mc_test_bug", "mc_test_bug_root_cause", "mc_test_bug_pattern", "mc_test_bug_log"}
    assert expected <= names, f"缺表：{expected - names}"
    # 解耦证明：测试缺陷不落 coding 的 issue 表（此 fixture 根本没注册该表）。
    assert "mc_project_issues" not in names


# ── 6) 读侧组合：get_bug / list_bugs 返回 bug + 根因 ──
@pytest.mark.asyncio
async def test_get_bug_and_list_bugs_combine(tortoise_db):
    bug_id, uid, pid = await _make_bug(fingerprint="FP-" + "d" * 8)
    await attach_root_cause(bug_id, {
        "classification": "null",
        "hypothesis": "空指针",
        "evidence_chain": [{"step": "verify_order", "log": "TypeError"}],
    })

    bug = await get_bug(bug_id)
    assert bug is not None
    assert bug["status"] == "open"
    assert bug["fingerprint"] == "FP-" + "d" * 8
    assert bug["case_refs"] == ["TC-0001"]
    assert bug["from_run_scenario_id"] is not None
    assert len(bug["root_causes"]) == 1
    assert bug["root_causes"][0]["classification"] == "null"
    assert "test_discovered" in bug["tags"]

    bugs = await list_bugs(pid)
    assert len(bugs) == 1
    assert bugs[0]["id"] == bug_id

    # 不存在 → 抛错
    with pytest.raises(Exception):
        await get_bug(uuid.uuid4())


# ── 7) 失败指纹：稳定 + 跨 run_scenario 归并（去重语义）──
@pytest.mark.asyncio
async def test_compute_fingerprint_stable_across_runs():
    rsid_a, rsid_b = uuid.uuid4(), uuid.uuid4()  # 同一场景的两次执行
    diff = [{"path": "$.data.status", "expected": "created", "actual": "error"}]

    fp_a = compute_fingerprint(rsid_a, "AssertionError status_code", "request", diff)
    fp_b = compute_fingerprint(rsid_b, "AssertionError status_code", "request", diff)

    assert fp_a == fp_b, "指纹必须跨 run 稳定（run_scenario_id 不进哈希），否则去重失效"
    assert fp_a != compute_fingerprint(rsid_a, "AssertionError status_code", "page", diff)
    assert fp_a != compute_fingerprint(rsid_a, "ValueError", "request", diff)
    assert fp_a != compute_fingerprint(rsid_a, "AssertionError status_code", "request",
                                       [{"path": "$.data.total"}])
    assert len(fp_a) == 64  # sha256 hex，与 mc_test_bug.fingerprint 列宽对齐
