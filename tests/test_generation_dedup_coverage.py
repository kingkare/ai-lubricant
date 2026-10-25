"""历史查重 + 覆盖补全（25 号文 §环节4/§环节5 步骤6/§2.3/§2.5；P1「查重」+「覆盖统计」）。

SQLite 内存库 + Tortoise，模式照 ``tests/test_generation_orchestrator.py``：

1. dedup 纯函数：三元组相似度（同文本=1.0/完全不同=低/相似对>0.85）/
   verdict_for 三档/find_duplicates 降序+阈值/run_dedup 聚合（merge+both+clean=checked）；
2. coverage 纯函数：build_coverage_matrix 铺格（一条多点多端）/self_check_requirements
   四条各正反（漏 C01 报 CC1、漏 C02 报 CC2、恒真式无 C09 报 CC3、风险无场景报 CC3、
   同题重复报 CC4）/coverage_gaps 只报适用轴；
3. sync_coverage_cells 落库 + 幂等（两次同结果）+ na 必填 evidence + lit 优先于 na。

不联网、不调 AI、不启 PG。
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

from testing_platform.generation import coverage as cov
from testing_platform.generation.dedup import (
    DUP_THRESHOLD_KEEP_BOTH,
    DUP_THRESHOLD_MERGE,
    find_duplicates,
    normalize_text,
    run_dedup,
    trigram_similarity,
    verdict_for,
)
from testing_platform.generation.models import TestCoverageCell, TestGenRun
from testing_platform.quality.errors import SEVERITY_ERROR


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.generation.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 1. normalize_text ─────────────────────────────────────────────

def test_normalize_text_strips_punct_case_and_brackets():
    """中英混排：小写 + 去标点空白 + 去括号注 + 全角折叠。"""
    assert normalize_text("Verify  Login（含 OAuth）Returns Token!") == "verifyloginreturnstoken"
    # 空白/标点剥除后仅剩字母数字
    assert normalize_text("  Hello, World_123! ") == "helloworld123"
    # 括号注整体剥掉（不是只删括号）
    assert normalize_text("测试支付【备注：内部】流程") == "测试支付流程"
    # 幂等：再跑一遍不变
    once = normalize_text("登录（含 OAuth）返回 Token")
    assert normalize_text(once) == once


def test_normalize_text_none_and_non_str():
    assert normalize_text(None) == ""
    assert normalize_text(123) == "123"


# ── 2. trigram_similarity ─────────────────────────────────────────

def test_trigram_identical_is_one():
    """同文本=1.0；仅大小写/标点/括号注不同也判 1.0（LLM 换写法仍同一条）。"""
    assert trigram_similarity("验证登录返回 Token", "验证登录返回 Token") == 1.0
    assert trigram_similarity(
        "验证登录（含 OAuth）返回 Token！", "验证登录返回 token",
    ) == 1.0
    # 空对空：都无可比内容 → 1.0
    assert trigram_similarity("", "") == 1.0
    assert trigram_similarity("  （）  ", "…") == 1.0


def test_trigram_different_is_low():
    """完全不同的中文文本相似度低（< 0.2）。"""
    score = trigram_similarity("验证登录成功返回令牌", "删除订单并级联清理库存")
    assert score < 0.2
    # 一方空一方非空 → 0.0
    assert trigram_similarity("验证登录", "") == 0.0


def test_trigram_similar_pair_above_threshold():
    """相似对（长标题尾部小改，主体三元组共享）> 0.85。

    三元组 Jaccard 对中文长句敏感：22 字标题里插 1 字会打断 3 个三元组，
    短标题的同义改写落 [0.6,0.85)（keep_both 档）；这里取「长标题 + 尾部追加」
    这种主体高度重叠的改写，钉死 > 0.85 的 merge 档。
    """
    a = "验证用户在有效期内使用正确账号密码登录成功后返回有效令牌并写入审计日志"
    b = "验证用户在有效期内使用正确账号密码登录成功后返回有效令牌并写入审计日志记录"
    score = trigram_similarity(a, b)
    assert score > DUP_THRESHOLD_MERGE
    assert 0.0 < score <= 1.0


# ── 3. verdict_for 三档 ───────────────────────────────────────────

@pytest.mark.parametrize("score,expected", [
    (1.0, "merge"),
    (0.95, "merge"),
    (0.85, "merge"),               # 边界归上档（闭区间，§环节9 pg_trgm≥0.85）
    (0.849, "keep_both"),
    (0.7, "keep_both"),
    (0.6, "keep_both"),            # 边界归上档
    (0.599, None),
    (0.0, None),
    (None, None),                  # 没查重 → 独立
])
def test_verdict_for_three_bands(score, expected):
    assert verdict_for(score) == expected


# ── 4. find_duplicates ────────────────────────────────────────────

_EXISTING = [
    {"id": "C-1", "title": "验证用户使用正确账号密码登录成功返回有效令牌"},
    {"id": "C-2", "title": "删除订单并级联清理库存"},
    {"id": "C-3", "title": "查询订单列表分页返回全部字段"},
]


def test_find_duplicates_descending_and_threshold():
    """命中按 score 降序；低于 threshold 的不出现。"""
    cand = {"id": "C-9", "title": "验证用户使用正确账号密码登录成功返回有效令牌"}
    hits = find_duplicates(cand, _EXISTING)
    assert [h["case_id"] for h in hits] == ["C-1"]
    assert hits[0]["score"] == 1.0

    # threshold 调低：不相似的仍在带外
    cand2 = {"id": "C-9", "title": "验证用户使用正确账号密码登录成功并返回有效令牌"}
    hits2 = find_duplicates(cand2, _EXISTING, threshold=DUP_THRESHOLD_KEEP_BOTH)
    assert hits2 and hits2[0]["case_id"] == "C-1"
    assert all(h["score"] >= DUP_THRESHOLD_KEEP_BOTH for h in hits2)
    # 降序
    assert [h["score"] for h in hits2] == sorted((h["score"] for h in hits2), reverse=True)


def test_find_duplicates_self_match_excluded():
    """候选用例带自己的历史行查重，不把自己算成命中。"""
    cand = {"id": "C-1", "title": "验证用户使用正确账号密码登录成功返回有效令牌"}
    assert find_duplicates(cand, _EXISTING) == []


def test_find_duplicates_no_hit_returns_empty():
    assert find_duplicates({"id": "C-9", "title": "全新的边界场景"}, _EXISTING) == []


def test_find_duplicates_empty_title_never_hits():
    """空标题候选/空标题既有行都不参与查重（否则空串 vs 空串按相似度 1.0 造假命中）。"""
    assert find_duplicates({"title": ""}, _EXISTING) == []
    assert find_duplicates({"title": "   （） "}, _EXISTING) == []
    # 候选有标题、既有行全是空标题 → 也不命中
    empty_rows = [{"id": "X-1", "title": ""}, {"id": "X-2", "title": "【】"}]
    assert find_duplicates({"id": "C-9", "title": "验证登录"}, empty_rows) == []


def test_find_duplicates_ignores_testpoint_id_for_self_exclusion():
    """自匹配排除只看 case_id/id/code，不看 testpoint_id（否则同测试点两条不同
    标题的用例会被误当自匹配跳过、漏掉真重复）。"""
    existing = [
        {"case_id": "C-1", "testpoint_id": "TP-9", "title": "验证用户使用正确账号密码登录成功返回有效令牌"},
    ]
    # 候选与被查重行共享 testpoint_id，但 case_id 不同 → 仍是真重复，应命中
    cand = {"case_id": "C-77", "testpoint_id": "TP-9",
            "title": "验证用户使用正确账号密码登录成功返回有效令牌"}
    hits = find_duplicates(cand, existing)
    assert [h["case_id"] for h in hits] == ["C-1"]


# ── 5. run_dedup 聚合 ─────────────────────────────────────────────

def test_run_dedup_aggregation_sums_to_checked():
    proposals = [
        {"id": "P-1", "title": "验证用户使用正确账号密码登录成功返回有效令牌"},  # → merge
        {"id": "P-2", "title": "删除订单并级联清理库存"},                        # → merge
        {"id": "P-3", "title": "查询订单列表分页返回全部字段"},                  # → merge
        {"id": "P-4", "title": "并发重复提交同一笔支付"},                        # → clean
    ]
    res = run_dedup(proposals, _EXISTING)
    assert res["checked"] == 4
    assert res["dup_merge"] == 3
    assert res["dup_both"] == 0
    assert res["clean"] == 1
    # 恒等：三档相加 == 查过数
    assert res["dup_merge"] + res["dup_both"] + res["clean"] == res["checked"]

    # 每提案带三键；merge 的写 dup_of_case_id + score + verdict
    merged = [p for p in res["proposals"] if p["id"] == "P-1"][0]
    assert merged["dup_of_case_id"] == "C-1"
    assert merged["dup_score"] >= DUP_THRESHOLD_MERGE
    assert merged["dup_verdict"] == "merge"

    clean = [p for p in res["proposals"] if p["id"] == "P-4"][0]
    assert clean["dup_of_case_id"] is None
    assert clean["dup_score"] == 0.0
    assert clean["dup_verdict"] is None


def test_run_dedup_keep_both_band_reachable():
    """keep_both 档可达：召回 0.6、verdict 分档（相似但非同）。"""
    existing = [{"id": "C-1", "title": "验证用户使用正确账号密码登录成功返回有效令牌"}]
    # 相似但有一处差异 → 落在 [0.6, 0.85)
    cand = [{"id": "P-1", "title": "验证用户使用正确账号密码登录成功返回有效令牌并记录日志"}]
    res = run_dedup(cand, existing)
    assert res["checked"] == 1
    score = res["proposals"][0]["dup_score"]
    assert DUP_THRESHOLD_KEEP_BOTH <= score < DUP_THRESHOLD_MERGE
    assert res["dup_both"] == 1 and res["dup_merge"] == 0
    assert res["proposals"][0]["dup_verdict"] == "keep_both"
    # 另立也记参考用例 id
    assert res["proposals"][0]["dup_of_case_id"] == "C-1"


def test_run_dedup_does_not_mutate_input():
    proposals = [{"id": "P-1", "title": "删除订单并级联清理库存"}]
    run_dedup(proposals, _EXISTING)
    assert "dup_score" not in proposals[0]  # 输入未被改写


# ── 6. build_coverage_matrix ──────────────────────────────────────

def test_build_coverage_matrix_multi_axis_multi_surface():
    """一条点多轴多端 → 每个 (axis, surface) 组合都挂该点 code；去重保序。"""
    points = [
        {"code": "TP-1", "status": "confirmed",
         "coverage_axis": ["C01", "C03"], "surfaces": ["api", "web"]},
        {"code": "TP-2", "status": "confirmed",
         "coverage_axis": ["C02"], "surfaces": ["api"]},
    ]
    m = cov.build_coverage_matrix(points)
    assert m["C01"]["api"] == ["TP-1"]
    assert m["C01"]["web"] == ["TP-1"]
    assert m["C03"]["api"] == ["TP-1"]
    assert m["C02"]["api"] == ["TP-2"]
    # 未覆盖格不出现
    assert "C04" not in m


def test_build_coverage_matrix_skips_placeholder_and_dedups():
    """占位点（to_confirm，无轴）不入矩阵；同点重复挂同格去重。"""
    points = [
        {"code": "TP-1", "coverage_axis": ["C01"], "surfaces": ["api"]},
        {"code": "TP-1", "coverage_axis": ["C01"], "surfaces": ["api"]},
        {"code": "TP-P", "status": "to_confirm", "coverage_axis": [], "surfaces": []},
    ]
    m = cov.build_coverage_matrix(points)
    assert m == {"C01": {"api": ["TP-1"]}}


# ── 7. coverage_gaps 只报适用轴 ───────────────────────────────────

def test_coverage_gaps_only_reports_mandatory_axes():
    """空矩阵 → 只报各端必出轴的空格；非适用轴/未知端不报。"""
    gaps = cov.coverage_gaps({})
    by_key = {(g["axis"], g["surface"]): g["reason"] for g in gaps}
    # 端→必出轴（MINIMAL_MANDATORY_AXES）逐个有缺
    assert ("C01", "api") in by_key
    assert ("C11", "api") in by_key and ("C12", "api") in by_key
    assert ("C01", "web") in by_key and ("C02", "web") in by_key
    assert ("C01", "app") in by_key and ("C08", "app") in by_key
    assert ("C04", "security") in by_key and ("C14", "security") in by_key
    # 非适用轴不报：C05（并发）不在任何端必出集
    assert not any(g["axis"] == "C05" for g in gaps)
    assert all(g["reason"] == "无测试点" for g in gaps)


def test_coverage_gaps_filled_cell_not_reported():
    """已点亮的格不报空。"""
    m = cov.build_coverage_matrix([
        {"code": "TP-1", "coverage_axis": ["C01"], "surfaces": ["api"]},
    ])
    gaps = cov.coverage_gaps(m)
    assert ("C01", "api") not in {(g["axis"], g["surface"]) for g in gaps}
    # 但同端其他必出轴仍报空
    assert ("C03", "api") in {(g["axis"], g["surface"]) for g in gaps}


# ── 8. self_check_requirements 四条正反 ───────────────────────────

def _good_points():
    """四条全过的测试点集：C01+C02 齐、恒真式有 C09 直测、风险方向有场景、无同题。"""
    return [
        {"code": "TP-1", "title": "正常登录", "coverage_axis": ["C01"],
         "surfaces": ["api"], "basis": "fact"},
        {"code": "TP-2", "title": "密码错误被拒", "coverage_axis": ["C02"],
         "surfaces": ["api"], "basis": "fact"},
        {"code": "TP-3", "title": "验证恒真式 INV-1", "coverage_axis": ["C09"],
         "surfaces": ["api"], "basis": "fact", "evidence": ["INV-1"]},
        {"code": "TP-4", "title": "并发登录场景", "coverage_axis": ["C05"],
         "surfaces": ["web"], "basis": "fact", "direction": "并发风险"},
    ]


def test_self_check_all_four_pass():
    res = cov.self_check_requirements("REQ-1", _good_points())
    assert res.valid is True
    assert res.issues == []


def test_self_check_missing_c01_reports_cc1():
    pts = _good_points()
    pts = [p for p in pts if "C01" not in p["coverage_axis"]]
    res = cov.self_check_requirements("REQ-1", pts)
    rules = {i.rule for i in res.errors}
    assert "CC1_missing_happy" in rules
    assert all(i.severity == SEVERITY_ERROR for i in res.issues)


def test_self_check_missing_c02_reports_cc2():
    pts = [p for p in _good_points() if "C02" not in p["coverage_axis"]]
    res = cov.self_check_requirements("REQ-1", pts)
    assert "CC2_missing_negative" in {i.rule for i in res.errors}


def test_self_check_invariant_without_c09_reports_cc3():
    pts = [p for p in _good_points() if p.get("code") != "TP-3"] + [
        {"code": "TP-3", "title": "恒真式无直测", "coverage_axis": ["C01"],
         "surfaces": ["api"], "basis": "fact", "evidence": ["INV-1"]},
    ]
    res = cov.self_check_requirements("REQ-1", pts)
    assert "CC3_invariant_not_direct" in {i.rule for i in res.errors}


def test_self_check_direction_without_scenario_reports_cc3():
    """风险方向只有占位点（to_confirm）→ 无场景，报 CC3_direction_uncovered。"""
    pts = [
        {"code": "TP-1", "title": "正常登录", "coverage_axis": ["C01"],
         "surfaces": ["api"], "basis": "fact"},
        {"code": "TP-2", "title": "异常被拒", "coverage_axis": ["C02"],
         "surfaces": ["api"], "basis": "fact"},
        {"code": "TP-3", "title": "验证恒真式 INV-1", "coverage_axis": ["C09"],
         "surfaces": ["api"], "basis": "fact", "evidence": ["INV-1"]},
        {"code": "TP-P", "title": "[待确认 Q1] 并发登录", "coverage_axis": [],
         "surfaces": [], "basis": "to_confirm", "direction": "并发风险"},
    ]
    res = cov.self_check_requirements("REQ-1", pts)
    assert "CC3_direction_uncovered" in {i.rule for i in res.errors}


def test_self_check_duplicate_titles_report_cc4():
    """同需求 + 归一后同标题 → CC4_duplicate_testpoint。"""
    pts = _good_points() + [
        {"code": "TP-5", "title": "正常登录（复核）", "coverage_axis": ["C01"],
         "surfaces": ["api"], "basis": "fact"},
    ]
    res = cov.self_check_requirements("REQ-1", pts)
    cc4 = [i for i in res.errors if i.rule == "CC4_duplicate_testpoint"]
    assert len(cc4) == 1
    assert "TP-5" in cc4[0].path or "testpoints[4]" in cc4[0].path


def test_self_check_same_title_different_requirement_not_duplicate():
    """标题相同但 requirement_id 不同 → 不算重复（按需求分组）。"""
    a = _good_points()
    b = [dict(p, requirement_id="REQ-2") for p in _good_points()]
    res = cov.self_check_requirements("REQ-1", a + b)
    assert "CC4_duplicate_testpoint" not in {i.rule for i in res.errors}


def test_self_check_empty_points_reports_all():
    res = cov.self_check_requirements("REQ-1", [])
    rules = {i.rule for i in res.errors}
    assert {"CC1_missing_happy", "CC2_missing_negative"} <= rules


# ── 9. sync_coverage_cells 落库 + 幂等 ────────────────────────────

def _lit_matrix():
    return cov.build_coverage_matrix([
        {"code": "TP-1", "coverage_axis": ["C01"], "surfaces": ["api"]},
    ])


@pytest.mark.asyncio
async def test_sync_coverage_cells_three_states_and_idempotent(tortoise_db):
    pid = uuid.uuid4()
    rid = uuid.uuid4()
    run = await TestGenRun.create(project_id=pid, run_type="full", status="pending")

    matrix = _lit_matrix()
    gaps = [g for g in cov.coverage_gaps(matrix) if g["surface"] == "api"]
    na = {("C04", "api"): "该接口无权限维度，见 REQ-1 §3 角色说明"}

    res1 = await cov.sync_coverage_cells(run, rid, matrix, gaps, na_evidence=na)
    assert res1["lit"] == 1
    assert res1["gap"] >= 1
    assert res1["na"] == 1
    total = res1["lit"] + res1["gap"] + res1["na"]
    assert res1["created"] == total
    assert res1["updated"] == 0

    # 落库事实：三态在表里
    rows = await TestCoverageCell.filter(requirement_id=rid).all()
    states = {(r.axis, r.surface): r.state for r in rows}
    assert states[("C01", "api")] == "lit"
    assert states[("C04", "api")] == "na"
    na_row = [r for r in rows if r.state == "na"][0]
    assert na_row.evidence and "角色说明" in na_row.evidence
    gap_rows = [r for r in rows if r.state == "gap"]
    assert gap_rows and all(r.evidence is None for r in gap_rows)

    # 幂等：第二次同输入 → created=0、updated=总数、状态不变
    res2 = await cov.sync_coverage_cells(run, rid, matrix, gaps, na_evidence=na)
    assert res2["created"] == 0
    assert res2["updated"] == total
    assert res2["lit"] == res1["lit"] and res2["gap"] == res1["gap"] and res2["na"] == res1["na"]
    rows2 = await TestCoverageCell.filter(requirement_id=rid).all()
    assert {(r.axis, r.surface): r.state for r in rows2} == states
    # 唯一键保证格数不翻倍
    assert len(rows2) == len(rows)


@pytest.mark.asyncio
async def test_sync_coverage_cells_na_requires_evidence(tortoise_db):
    """§2.5 硬约束：na 格 evidence 为空 → 响亮失败，不静默 no-op。"""
    pid, rid = uuid.uuid4(), uuid.uuid4()
    run = await TestGenRun.create(project_id=pid, run_type="full", status="pending")
    with pytest.raises(ValueError, match="验证依据"):
        await cov.sync_coverage_cells(run, rid, {}, [], na_evidence={("C04", "api"): "   "})


@pytest.mark.asyncio
async def test_sync_coverage_cells_lit_overrides_na(tortoise_db):
    """lit 优先：既有测试点点亮的格，na 声明被覆盖并记 na_overridden。"""
    pid, rid = uuid.uuid4(), uuid.uuid4()
    run = await TestGenRun.create(project_id=pid, run_type="full", status="pending")
    matrix = _lit_matrix()  # C01/api lit
    res = await cov.sync_coverage_cells(
        run, rid, matrix, [], na_evidence={("C01", "api"): "误标不适用"},
    )
    assert res["na"] == 0
    assert res["na_overridden"] == [{"axis": "C01", "surface": "api", "reason": "lit 优先"}]
    row = await TestCoverageCell.get(requirement_id=rid, axis="C01", surface="api")
    assert row.state == "lit"


@pytest.mark.asyncio
async def test_sync_coverage_cells_na_evidence_accepts_string_key(tortoise_db):
    """na_evidence 收 "axis|surface" 字符串键形态（调用方便利）。"""
    pid, rid = uuid.uuid4(), uuid.uuid4()
    run = await TestGenRun.create(project_id=pid, run_type="full", status="pending")
    res = await cov.sync_coverage_cells(
        run, rid, {}, [], na_evidence={"C04|api": "接口无权限维度"},
    )
    assert res["na"] == 1
    row = await TestCoverageCell.get(requirement_id=rid, axis="C04", surface="api")
    assert row.state == "na" and row.evidence == "接口无权限维度"


@pytest.mark.asyncio
async def test_sync_coverage_cells_requires_project_id(tortoise_db):
    """run 无 project_id 且未显式传参 → 响亮失败。"""
    with pytest.raises(ValueError, match="project_id"):
        await cov.sync_coverage_cells(None, uuid.uuid4(), {}, [])
