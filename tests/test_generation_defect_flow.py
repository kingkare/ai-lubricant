"""缺陷链方法层（25 号文 v4.1 §环节9 六步 + §环节11 回流 sink；P2）测试。

纯函数部分不碰 DB（:mod:`testing_platform.generation.defect_flow` 不 import
tortoise）；两个 surface 方法（``case.reject_sink`` / ``case.mark_golden``）走
SQLite 内存库真写，模式照 ``tests/test_gen_methods.py``。

覆盖清单：

1. 六段单：缺段 error / 七段齐 valid / 优先级与枚举越界 / steps 形状 warn；
2. ``inherit_steps``：dict 项补 seq、字符串项包裹、嵌套 payload、空入参；
3. ``dedup_against_open``：命中/阈值/降序/空文本防护；
4. 八步分析：**无日志禁断根因**（error）/ 有日志放行 / 缺段只 warn /
   非法证据等级 error / 慢查询五维提示；
5. ``severity_consistency`` + ``consistency_rate``（空样本 rate=None、
   mismatches 清单、畸形项剔分母）；
6. 修复验证七点：全 true 无 issue / 缺项 error / false 给 warn；
7. ``regression_tiers`` 三档互斥 + 需求一跳 + 同模块抽测；
8. ``make_sink_entry`` 行形状（origin=kind / status=resolved / 不阻塞 /
   context 带 gate/module/source_ref）+ 非法 kind/空文本抛错；
9. ``case.reject_sink`` 真写（含 provenance 回退、幂等、缺理由报错）；
10. ``case.mark_golden`` 真写（confirm_ref.golden、幂等、撤标）；
11. 镜像常量与缺陷域真值一致（防漂移）。

运行：``python -m pytest tests/test_generation_defect_flow.py -q``
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

from testing_platform.generation import defect_flow as df
from testing_platform.quality.errors import SEVERITY_ERROR, SEVERITY_WARN


# ════════════════════════════════════════════════════════════════
# 1) 六段单（Skill9 / §环节9 第②步）
# ════════════════════════════════════════════════════════════════

def _full_draft(**overrides):
    draft = {
        "title": "下单接口返回 500",
        "steps_to_reproduce": [
            {"seq": 1, "action": "以已登录态提交 amount=100 的订单", "expected": "创建成功"},
            {"seq": 2, "action": "查询订单详情", "expected": "返回 200"},
        ],
        "expected": "订单创建成功且详情可查",
        "actual": "创建接口返回 500，日志 NullPointerException",
        "environment": "test 环境 / order-service v1.2.3",
        "priority": 1,
        "reproducibility": "always",
    }
    draft.update(overrides)
    return draft


def test_bug_template_fields_are_seven_with_six_body_sections():
    """任务清单七字段；「六段」= 去 title 的六段正文（歧义记录 1）。"""
    assert df.BUG_TEMPLATE_FIELDS == (
        "title", "steps_to_reproduce", "expected", "actual",
        "environment", "priority", "reproducibility",
    )
    assert df.BUG_BODY_SECTIONS == df.BUG_TEMPLATE_FIELDS[1:]
    assert len(df.BUG_BODY_SECTIONS) == 6
    assert set(df.BUG_TEMPLATE_LABELS) == set(df.BUG_TEMPLATE_FIELDS)


def test_validate_bug_draft_full_is_valid():
    res = df.validate_bug_draft(_full_draft())
    assert res.valid, [i.to_dict() for i in res.issues]
    assert res.errors == []


@pytest.mark.parametrize("field", list(df.BUG_TEMPLATE_FIELDS))
def test_validate_bug_draft_missing_section_is_error(field):
    """任一七段缺失 → BG1 error（缺段=不得建单）。"""
    draft = _full_draft()
    draft.pop(field)
    res = df.validate_bug_draft(draft)
    assert not res.valid
    rules = {i.rule for i in res.errors}
    assert "BG1_section_required" in rules
    assert any(i.path == f"$.{field}" for i in res.errors)


def test_validate_bug_draft_blank_and_empty_container_count_as_missing():
    """空白串 / 空列表 / None 都算缺段。"""
    for empty in ("   ", [], None):
        res = df.validate_bug_draft(_full_draft(actual=empty))
        assert not res.valid
        assert any(i.path == "$.actual" for i in res.errors)


def test_validate_bug_draft_priority_out_of_range_is_error():
    """priority 越界（整数但不在 1-4）→ BG3 error，不是缺段。"""
    res = df.validate_bug_draft(_full_draft(priority=9))
    assert not res.valid
    assert any(i.rule == "BG3_priority_value" for i in res.errors)
    # 0 也算「填了」，只是越界（_filled 的 False 语义不吞 0）
    res0 = df.validate_bug_draft(_full_draft(priority=0))
    assert any(i.rule == "BG3_priority_value" for i in res0.errors)


def test_validate_bug_draft_text_steps_warn_but_valid():
    """整段文本复现步骤：能用（valid）但 BG2 warn 建议结构化。"""
    res = df.validate_bug_draft(_full_draft(steps_to_reproduce="1. 打开页面 2. 点提交"))
    assert res.valid
    assert any(i.rule == "BG2_steps_shape" and i.severity == SEVERITY_WARN
               for i in res.issues)


def test_validate_bug_draft_bad_enums_warn():
    """severity / reproducibility 越界只 warn（文档未给枚举，宽容）。"""
    res = df.validate_bug_draft(_full_draft(severity="huge", reproducibility="mostly"))
    assert res.valid
    rules = {i.rule for i in res.warnings}
    assert {"BG4_severity_value", "BG5_reproducibility_value"} <= rules


def test_inherit_steps_from_case_doc():
    """复现步骤继承用例 steps：补 seq / 兼容别名 / 跳过空项。"""
    case_doc = {"steps": [
        {"action": "提交订单", "expected": "200"},
        {"seq": 5, "text": "查询详情", "expected": "有记录"},
        "纯字符串步骤",
        {"action": "   "},
        {"description": "第三条"},
    ]}
    steps = df.inherit_steps(case_doc)
    assert [s["action"] for s in steps] == ["提交订单", "查询详情", "纯字符串步骤", "第三条"]
    assert steps[0]["seq"] == 1                      # 缺 seq 按位置补
    assert steps[1]["seq"] == 5                      # 显式 seq 保留
    assert steps[2] == {"seq": 3, "action": "纯字符串步骤", "expected": None}
    # 不改输入
    assert "seq" not in case_doc["steps"][0]


def test_inherit_steps_reads_nested_payload_and_handles_garbage():
    """提案 payload 套娃也能读；None/非列表/无 steps → []。"""
    assert df.inherit_steps({"payload": {"steps": ["a", "b"]}}) == [
        {"seq": 1, "action": "a", "expected": None},
        {"seq": 2, "action": "b", "expected": None},
    ]
    assert df.inherit_steps(None) == []
    assert df.inherit_steps({}) == []
    assert df.inherit_steps({"steps": "not-a-list"}) == []
    assert df.inherit_steps({"steps": []}) == []


def test_inherit_steps_output_passes_bug_draft_validation():
    """继承出来的 steps 直接喂六段单 → 不再是缺段。"""
    steps = df.inherit_steps({"steps": ["打开页面", "点提交"]})
    draft = _full_draft(steps_to_reproduce=steps)
    res = df.validate_bug_draft(draft)
    assert res.valid


# ════════════════════════════════════════════════════════════════
# 2) 建单前查重（§环节9 第①步）
# ════════════════════════════════════════════════════════════════

def test_dedup_against_open_hits_and_sorts_desc():
    """同因 bug 命中 → suspect_same，按 score 降序；无关 bug 不进。"""
    bugs = [
        {"bug_id": "BUG-0001", "title": "下单接口返回 500 空指针异常",
         "symptom": "创建订单时服务端抛空指针"},
        {"bug_id": "BUG-0002", "title": "下单接口返回 500 空指针异常复现",
         "symptom": "创建订单时服务端抛空指针"},
        {"bug_id": "BUG-0003", "title": "登录页样式错位", "symptom": "CSS 渲染问题"},
    ]
    hits = df.dedup_against_open(
        "下单接口返回 500 空指针异常", "创建订单时服务端抛空指针", bugs,
    )
    assert [h["bug_id"] for h in hits] == ["BUG-0001", "BUG-0002"]
    assert all(h["verdict"] == df.VERDICT_SUSPECT_SAME for h in hits)
    assert hits[0]["score"] == pytest.approx(1.0)
    # BUG-0002 只有「标题路」够 0.85（拼接路被多出的两字稀释到 0.78）——
    # 这条钉住 max() 语义（见函数 docstring 的分数口径）。
    assert hits[1]["score"] == pytest.approx(0.8571, abs=1e-3)
    assert hits[0]["score"] >= hits[1]["score"] >= 0.85
    assert "BUG-0003" not in [h["bug_id"] for h in hits]


def test_dedup_against_open_threshold_boundary():
    """阈值可调：卡 1.0 时只有完全同文命中；高于任何可能分 → 空。"""
    bugs = [{"bug_id": "B1", "title": "登录失败", "symptom": ""}]
    assert df.dedup_against_open("登录失败", "", bugs, threshold=1.0)
    assert df.dedup_against_open("登录失败", "", bugs, threshold=1.01) == []


def test_dedup_against_open_uses_desc_too():
    """标题完全不同但症状同文 → 拼接路仍命中（只比标题会漏）。"""
    bugs = [{"bug_id": "B1", "title": "订单服务崩溃",
             "symptom": "创建订单时服务端抛 NullPointerException"}]
    # 先自证「标题路」确实不命中，否则这条测试没有区分力
    from testing_platform.generation.dedup import trigram_similarity

    assert trigram_similarity("新建订单失败", "订单服务崩溃") < 0.5
    hits = df.dedup_against_open(
        "新建订单失败", "创建订单时服务端抛 NullPointerException", bugs, threshold=0.5,
    )
    assert hits and hits[0]["bug_id"] == "B1"


def test_dedup_against_open_empty_text_never_hits():
    """空标题空症状 → []；空行跳过（防「都空 → 1.0」假命中）。"""
    bugs = [{"bug_id": "B1", "title": "", "symptom": ""}]
    assert df.dedup_against_open("", "", bugs) == []
    assert df.dedup_against_open("", "", [{"bug_id": "B2", "title": "真实 bug"}]) == []


# ════════════════════════════════════════════════════════════════
# 3) 八步分析（Skill7 / §环节9 第④步；无日志禁断根因）
# ════════════════════════════════════════════════════════════════

def _eight_step_report(**overrides):
    report = {
        "symptom": {"text": "下单 500", "evidence_class": "executed"},
        "impact_modules": {"text": "order/payment", "evidence_class": "static"},
        "recommended_logs": {"text": "order-service 日志 traceId=abc", "evidence_class": "inferred"},
        "recommended_sql": {"text": "SELECT * FROM t_order WHERE id=?", "evidence_class": "inferred"},
        "related_bugs": {"text": "BUG-0001", "evidence_class": "static"},
        "regression_scope": {"text": "order 模块全量", "evidence_class": "inferred"},
        "risks": {"text": "并发下单重复扣款", "evidence_class": "inferred"},
        "review_points": {"text": "复核订单状态机", "evidence_class": "claimed"},
        "root_cause": {"text": "order.get() 返回 null 未判空", "evidence_class": "static"},
        "log_evidence": [{"source": "order-service.log:88", "line": "NullPointerException"}],
    }
    report.update(overrides)
    return report


def test_eight_step_skeleton_is_documented_order():
    """八步顺序 = 文档原文顺序（Skill7）。"""
    assert [k for k, _ in df.EIGHT_STEP_SKELETON] == [
        "symptom", "impact_modules", "recommended_logs", "recommended_sql",
        "related_bugs", "regression_scope", "risks", "review_points",
    ]
    assert df.EIGHT_STEP_FIELDS == tuple(k for k, _ in df.EIGHT_STEP_SKELETON)
    assert len(df.EIGHT_STEP_SKELETON) == 8


def test_eight_step_full_report_is_valid():
    res = df.validate_eight_step(_eight_step_report())
    assert res.valid, [i.to_dict() for i in res.issues]
    assert res.errors == []


def test_eight_step_root_cause_without_logs_is_error():
    """**无日志禁断根因**：root_cause 有、log_evidence 空 → error。"""
    res = df.validate_eight_step(_eight_step_report(log_evidence=[]))
    assert not res.valid
    err = [i for i in res.errors if i.rule == "E7_root_cause_needs_logs"]
    assert len(err) == 1
    assert "无日志禁断根因" in err[0].msg
    assert err[0].path == "$.root_cause"


def test_eight_step_root_cause_with_logs_passes():
    """有日志证据 → 放行（同报告只差 log_evidence）。"""
    res = df.validate_eight_step(_eight_step_report())
    assert res.valid


def test_eight_step_no_root_cause_needs_no_logs():
    """不下根因结论时，不需要日志证据（分析中途草稿可过）。"""
    report = _eight_step_report()
    report.pop("root_cause")
    report["log_evidence"] = []
    res = df.validate_eight_step(report)
    assert res.valid


def test_eight_step_missing_segments_are_warnings_only():
    """缺段 = warn（分析没走完不该被机器门拒）。"""
    res = df.validate_eight_step({"symptom": {"text": "x", "evidence_class": "executed"}})
    assert res.valid
    missing = {i.path for i in res.warnings if i.rule == "E7_step_missing"}
    assert len(missing) == 7  # 除 symptom 外七段


def test_eight_step_invalid_evidence_class_is_error():
    """段申报非法等级 → error（纪律违规，不是没填）。"""
    report = _eight_step_report()
    report["symptom"] = {"text": "下单 500", "evidence_class": "guesswork"}
    res = df.validate_eight_step(report)
    assert not res.valid
    assert any(i.rule == "E7_evidence_class_invalid" and i.path == "$.symptom.evidence_class"
               for i in res.errors)


def test_eight_step_missing_evidence_class_warns():
    """填了段却没申报等级 → warn（该教不该拦）。"""
    report = _eight_step_report()
    report["symptom"] = "下单 500"  # 纯文本，无申报
    res = df.validate_eight_step(report)
    assert res.valid
    assert any(i.rule == "E7_step_class_missing" and i.path == "$.symptom"
               for i in res.warnings)


def test_eight_step_custom_shape_segments_count_as_filled():
    """段用自定义键（非 text/detail）也算填了，不误报缺段。"""
    report = _eight_step_report()
    report["recommended_logs"] = {"trace_ids": ["abc", "def"], "evidence_class": "static"}
    res = df.validate_eight_step(report)
    assert not any(i.rule == "E7_step_missing" and i.path == "$.recommended_logs"
                   for i in res.issues)

    # 只有元数据键（没内容）→ 仍算缺段
    report["recommended_logs"] = {"evidence_class": "static"}
    res2 = df.validate_eight_step(report)
    assert any(i.rule == "E7_step_missing" and i.path == "$.recommended_logs"
               for i in res2.warnings)


def test_eight_step_accepts_evidence_class_aliases():
    """normalize_evidence_class 的近邻写法（direct/claim/conjecture）都认。"""
    report = _eight_step_report()
    report["symptom"] = {"text": "下单 500", "evidence_class": "direct"}
    report["risks"] = {"text": "重复扣款", "evidence_class": "conjecture"}
    res = df.validate_eight_step(report)
    assert not any(i.rule == "E7_evidence_class_invalid" for i in res.issues)


def test_eight_step_slow_query_needs_five_dimensions():
    """推荐SQL 像慢查询诊断却没展开五维 → warn；展开后不再报。"""
    res = df.validate_eight_step(_eight_step_report(
        recommended_sql={"text": "这条 SQL 慢查询超时 3s，需要优化", "evidence_class": "inferred"},
    ))
    assert res.valid
    assert any(i.rule == "E7_sql_slow_query_dims" for i in res.warnings)

    res2 = df.validate_eight_step(_eight_step_report(
        recommended_sql={"text": "慢查询：看执行计划走全表扫；给 (status, created_at) 建索引；"
                                 "JOIN 改小表驱动；ORDER BY 加 LIMIT",
                         "evidence_class": "inferred"},
    ))
    assert not any(i.rule == "E7_sql_slow_query_dims" for i in res2.issues)


def test_slow_query_dimensions_are_five():
    """Skill11 五维常量。"""
    assert df.SLOW_QUERY_DIMENSIONS == ("执行计划", "索引", "JOIN", "ORDER BY", "LIMIT")


# ════════════════════════════════════════════════════════════════
# 4) 定级一致率（§环节9 第③步反馈环）
# ════════════════════════════════════════════════════════════════

def test_severity_consistency_normalizes_case_and_space():
    assert df.severity_consistency("Major", " major ") is True
    assert df.severity_consistency("major", "critical") is False
    # 任一侧空 → 无信号，不算一致
    assert df.severity_consistency("major", "") is False
    assert df.severity_consistency(None, "major") is False


def test_consistency_rate_counts_and_lists_mismatches():
    pairs = [
        ("major", "major"),
        ("critical", "major"),
        ("minor", "minor"),
        ("trivial", "major"),
    ]
    out = df.consistency_rate(pairs)
    assert out["total"] == 4
    assert out["rate"] == 0.5
    assert out["mismatches"] == [
        {"ai": "critical", "final": "major"},
        {"ai": "trivial", "final": "major"},
    ]


def test_consistency_rate_empty_is_none_not_zero():
    """没样本 rate=None（歧义记录 6：不是 0% 一致）。"""
    out = df.consistency_rate([])
    assert out == {"rate": None, "total": 0, "mismatches": []}
    assert df.consistency_rate(None)["rate"] is None


def test_consistency_rate_drops_malformed_pairs():
    """长度不为 2 的项剔出分母（脏数据不拉低一致率）。"""
    out = df.consistency_rate([("major", "major"), ("major",), "x", None])
    assert out["total"] == 1
    assert out["rate"] == 1.0
    assert out["malformed"] == 3


def test_consistency_rate_all_match():
    out = df.consistency_rate([("blocker", "blocker"), ("minor", "minor")])
    assert out["rate"] == 1.0 and out["mismatches"] == []


# ════════════════════════════════════════════════════════════════
# 5) 修复验证七点（§环节9 第⑥步）
# ════════════════════════════════════════════════════════════════

def test_fix_verify_checklist_is_seven():
    assert len(df.FIX_VERIFY_CHECKLIST) == 7
    assert len(df.FIX_VERIFY_FIELDS) == 7
    assert df.FIX_VERIFY_FIELDS[0] == "original_path_reproduced"
    assert df.FIX_VERIFY_FIELDS[-1] == "closing_evidence"
    for item in df.FIX_VERIFY_CHECKLIST:
        assert set(item) == {"key", "label", "hint"} and item["hint"]


def test_validate_fix_verify_all_true_is_clean():
    res = df.validate_fix_verify({k: True for k in df.FIX_VERIFY_FIELDS})
    assert res.valid and res.issues == []


def test_validate_fix_verify_missing_point_is_error():
    answers = {k: True for k in df.FIX_VERIFY_FIELDS}
    answers.pop("failing_test_first")
    res = df.validate_fix_verify(answers)
    assert not res.valid
    assert any(i.rule == "FV1_answer_required"
               and i.path == "$.failing_test_first" for i in res.errors)


def test_validate_fix_verify_non_bool_is_error():
    answers = {k: True for k in df.FIX_VERIFY_FIELDS}
    answers["minimal_change"] = "大概吧"
    res = df.validate_fix_verify(answers)
    assert not res.valid
    assert any(i.rule == "FV1_answer_required" and i.path == "$.minimal_change"
               for i in res.errors)


def test_validate_fix_verify_false_is_warn_with_hint():
    """七点没全 true 不拦（valid），但每条 false 给 warn + 该点 hint。"""
    answers = {k: True for k in df.FIX_VERIFY_FIELDS}
    answers["regression_scope_defined"] = False
    res = df.validate_fix_verify(answers)
    assert res.valid
    warn = [i for i in res.warnings if i.rule == "FV2_not_satisfied"]
    assert len(warn) == 1
    assert "regression_tiers" in warn[0].hint


def test_validate_fix_verify_empty_reports_all_seven():
    res = df.validate_fix_verify({})
    assert not res.valid
    assert len([i for i in res.errors if i.rule == "FV1_answer_required"]) == 7


# ════════════════════════════════════════════════════════════════
# 6) 回归三档（Skill17 / §环节10）
# ════════════════════════════════════════════════════════════════

def _case_index():
    return [
        # 直命变更文件 → must
        {"case_id": "TC-01", "module": "order", "requirement_id": "REQ-1",
         "files": ["src/order/create.py"]},
        # 同需求、别的模块 → suggested（一跳）
        {"case_id": "TC-02", "module": "payment", "requirement_id": "REQ-1",
         "files": ["src/payment/pay.py"]},
        # 同模块、别的需求 → sample（抽测）
        {"case_id": "TC-03", "module": "order", "requirement_id": "REQ-2",
         "files": ["src/order/list.py"]},
        # 同需求且同模块 → suggested（需求信号更强）
        {"case_id": "TC-04", "module": "order", "requirement_id": "REQ-1",
         "files": ["src/order/other.py"]},
        # 无关
        {"case_id": "TC-05", "module": "user", "requirement_id": "REQ-9",
         "files": ["src/user/login.py"]},
    ]


def test_regression_tiers_three_buckets_are_mutually_exclusive():
    tiers = df.regression_tiers(["src/order/create.py"], _case_index())
    assert tiers == {
        "must": ["TC-01"],
        "suggested": ["TC-02", "TC-04"],
        "sample": ["TC-03"],
    }
    # 三档互斥
    all_ids = tiers["must"] + tiers["suggested"] + tiers["sample"]
    assert len(all_ids) == len(set(all_ids))
    assert "TC-05" not in all_ids


def test_regression_tiers_path_suffix_and_backslash_tolerated():
    """仓库相对路径 vs 模块相对路径（后缀命中）；Windows 反斜杠归一。"""
    index = [{"case_id": "TC-01", "module": "m", "requirement_id": "R",
              "files": ["order/create.py"]}]
    assert df.regression_tiers(["src\\order\\create.py"], index)["must"] == ["TC-01"]
    assert df.regression_tiers(["./src/order/create.py"], index)["must"] == ["TC-01"]


def test_regression_tiers_no_direct_hit_returns_empty():
    """没有直命用例 → 三档全空（不凭模块瞎猜）。"""
    tiers = df.regression_tiers(["src/unknown/file.py"], _case_index())
    assert tiers == {"must": [], "suggested": [], "sample": []}


def test_regression_tiers_empty_inputs():
    assert df.regression_tiers([], _case_index()) == {"must": [], "suggested": [], "sample": []}
    assert df.regression_tiers(["a.py"], []) == {"must": [], "suggested": [], "sample": []}
    assert df.regression_tiers(None, None) == {"must": [], "suggested": [], "sample": []}


def test_regression_tiers_must_wins_over_sample_same_module():
    """同模块里直命的用例只进 must，不会同时进 sample。"""
    tiers = df.regression_tiers(
        ["a.py"],
        [{"case_id": "TC-01", "module": "m", "requirement_id": "R", "files": ["a.py"]},
         {"case_id": "TC-02", "module": "m", "requirement_id": "R2", "files": ["b.py"]}],
    )
    assert tiers["must"] == ["TC-01"] and tiers["sample"] == ["TC-02"]


def test_regression_tiers_rows_without_module_still_enter_must():
    """没 module/requirement 的行只能靠 files 进 must。"""
    tiers = df.regression_tiers(
        ["a.py"], [{"case_id": "TC-01", "files": ["a.py"]}],
    )
    assert tiers["must"] == ["TC-01"]
    assert tiers["suggested"] == [] and tiers["sample"] == []


# ════════════════════════════════════════════════════════════════
# 7) 回流 sink 行形状（§环节11）
# ════════════════════════════════════════════════════════════════

def test_sink_kinds_are_five():
    assert df.SINK_KINDS == (
        "reject_reason", "rework_lesson", "bug_rootcase", "pitfall", "risk_scenario",
    )


def test_make_sink_entry_row_shape():
    entry = df.make_sink_entry(
        "reject_reason", "断言太弱，只有 status=200",
        module="order",
        source_ref={"proposal_id": uuid.UUID(int=1), "decided_by": "u1"},
    )
    # 模型真实列
    assert entry["origin"] == "reject_reason"
    assert entry["content"] == "断言太弱，只有 status=200"
    assert entry["status"] == "resolved"
    assert entry["resolution"] is None
    assert entry["blocking_refs"] == []
    assert entry["raised_by"] is None
    # 元数据进 context（gate/module 没有真列，见歧义记录 9）
    assert entry["context"] == {
        "kind": "reject_reason",
        "gate": "sink",
        "sink": True,
        "module": "order",
        "source_ref": {"proposal_id": str(uuid.UUID(int=1)), "decided_by": "u1"},
    }
    # 顶层扩展键也给了（扩列后 raw 写路径用）
    assert entry["gate"] == "sink" and entry["module"] == "order"


def test_make_sink_entry_is_not_blocking():
    """回流是已定案记录：status=resolved + 空 blocking_refs（不阻塞下游）。"""
    entry = df.make_sink_entry("pitfall", "测试环境时间戳是 UTC")
    assert entry["status"] == "resolved"
    assert entry["blocking_refs"] == []


def test_make_sink_entry_rejects_bad_kind_and_empty_text():
    with pytest.raises(ValueError):
        df.make_sink_entry("nonsense", "x")
    with pytest.raises(ValueError):
        df.make_sink_entry("pitfall", "   ")
    with pytest.raises(ValueError):
        df.make_sink_entry("pitfall", None)


def test_make_sink_entry_optional_fields_default_none():
    entry = df.make_sink_entry("risk_scenario", "大促并发下单")
    assert entry["context"]["module"] is None
    assert entry["context"]["source_ref"] is None


def test_severity_constants_match_defect_domain():
    """镜像常量与缺陷域真值一致（防漂移）。"""
    from testing_platform.defects.models import BUG_PRIORITIES, BUG_SEVERITIES

    assert df.SEVERITY_VALUES == BUG_SEVERITIES
    assert df.PRIORITY_VALUES == BUG_PRIORITIES


# ════════════════════════════════════════════════════════════════
# 8) surface 方法真写（SQLite；模式照 tests/test_gen_methods.py）
# ════════════════════════════════════════════════════════════════

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


async def _seed_proposal(*, provenance=None):
    from testing_platform.generation import orchestrator as orch
    from testing_platform.generation.models import TestProposal

    pid = uuid.uuid4()
    run = await orch.start_run(project_id=pid, created_by=uuid.uuid4())
    prop = await TestProposal.create(
        project_id=pid, run_id=run.id, kind="case",
        payload={"title": "验证金额为 0 被拒", "module": "order"},
        surface="api", evidence_class="inferred",
        review_state="scored", status="reject",
        provenance=dict(provenance or {}),
    )
    return prop


@pytest.mark.asyncio
async def test_case_reject_sink_writes_question_row(tortoise_db):
    """reject_sink 真写：origin=reject_reason、status=resolved、context 带来源。"""
    from testing_platform.quality.models import TestQuestion
    from testing_platform.surface import call_method

    prop = await _seed_proposal()
    decided_by = str(uuid.uuid4())
    res = await call_method("case.reject_sink", {
        "proposal_id": str(prop.id),
        "reason": "断言只有 status=200，判不了业务",
        "decided_by": decided_by,
    })
    assert res["stored"] is True and res["question_id"]
    row = await TestQuestion.get(id=uuid.UUID(res["question_id"]))
    assert row.project_id == prop.project_id
    assert row.origin == "reject_reason"
    assert row.content == "断言只有 status=200，判不了业务"
    assert row.status == "resolved"
    assert row.blocking_refs == []
    ctx = dict(row.context)
    assert ctx["gate"] == "sink" and ctx["sink"] is True
    assert ctx["module"] == "order"
    assert ctx["source_ref"]["proposal_id"] == str(prop.id)
    assert ctx["source_ref"]["decided_by"] == decided_by


@pytest.mark.asyncio
async def test_case_reject_sink_falls_back_to_provenance_reason(tortoise_db):
    """reason 缺省读提案 provenance.reject_reason（case.verdict 落的）。"""
    from testing_platform.quality.models import TestQuestion
    from testing_platform.surface import call_method

    prop = await _seed_proposal(provenance={"reject_reason": "与 TC-0003 重复"})
    res = await call_method("case.reject_sink", {"proposal_id": str(prop.id)})
    assert res["stored"] is True
    row = await TestQuestion.get(id=uuid.UUID(res["question_id"]))
    assert row.content == "与 TC-0003 重复"


@pytest.mark.asyncio
async def test_case_reject_sink_is_idempotent(tortoise_db):
    """同一提案同文再沉淀 → stored=false，不重复建行。"""
    from testing_platform.quality.models import TestQuestion
    from testing_platform.surface import call_method

    prop = await _seed_proposal()
    first = await call_method("case.reject_sink", {
        "proposal_id": str(prop.id), "reason": "理由 X",
    })
    second = await call_method("case.reject_sink", {
        "proposal_id": str(prop.id), "reason": "理由 X",
    })
    assert first["stored"] is True
    assert second["stored"] is False and second["question_id"] == first["question_id"]
    assert await TestQuestion.all().count() == 1


@pytest.mark.asyncio
async def test_case_reject_sink_missing_reason_errors(tortoise_db):
    """既没 reason 也没 provenance.reject_reason → invalid_params。"""
    from testing_platform.surface import call_method
    from testing_platform.surface.methods import MethodError

    prop = await _seed_proposal()
    with pytest.raises(MethodError) as exc:
        await call_method("case.reject_sink", {"proposal_id": str(prop.id)})
    assert exc.value.code == "invalid_params"


@pytest.mark.asyncio
async def test_case_reject_sink_unknown_proposal_not_found(tortoise_db):
    from testing_platform.surface import call_method
    from testing_platform.surface.methods import MethodError

    with pytest.raises(MethodError) as exc:
        await call_method("case.reject_sink", {
            "proposal_id": str(uuid.uuid4()), "reason": "x",
        })
    assert exc.value.code == "not_found"


async def _seed_case(**overrides):
    from testing_platform.assets.models import TestCase

    values = dict(
        project_id=uuid.uuid4(),
        code=f"TC-{uuid.uuid4().hex[:4]}",
        title="验证登录成功",
        confirm_source="doc",
        confirm_ref={"slot_key": "api:x:v1"},
        origin="ai_generated",
        created_by=uuid.uuid4(),
        status="draft",
    )
    values.update(overrides)
    return await TestCase.create(**values)


@pytest.mark.asyncio
async def test_case_mark_golden_writes_confirm_ref(tortoise_db):
    """mark_golden 真写：confirm_ref.golden=true + golden_by，origin 不动。"""
    from testing_platform.assets.models import TestCase
    from testing_platform.surface import call_method

    case = await _seed_case()
    marked_by = str(uuid.uuid4())
    res = await call_method("case.mark_golden", {
        "case_id": str(case.id), "marked_by": marked_by,
    })
    assert res == {"marked": True, "changed": True}
    fresh = await TestCase.get(id=case.id)
    assert fresh.confirm_ref["golden"] is True
    assert fresh.confirm_ref["golden_by"] == marked_by
    assert fresh.confirm_ref["slot_key"] == "api:x:v1"  # 原有键保留
    assert fresh.origin == "ai_generated"               # 不新增列、不改 origin


@pytest.mark.asyncio
async def test_case_mark_golden_is_idempotent(tortoise_db):
    """已 golden 再标 → changed=false（不写库）。"""
    from testing_platform.surface import call_method

    case = await _seed_case()
    await call_method("case.mark_golden", {"case_id": str(case.id), "marked_by": str(uuid.uuid4())})
    again = await call_method("case.mark_golden", {"case_id": str(case.id), "marked_by": str(uuid.uuid4())})
    assert again == {"marked": True, "changed": False}


@pytest.mark.asyncio
async def test_case_mark_golden_unmark(tortoise_db):
    """unmark=true 撤标；再撤幂等。"""
    from testing_platform.assets.models import TestCase
    from testing_platform.surface import call_method

    case = await _seed_case(confirm_ref={"golden": True, "golden_by": "u"})
    res = await call_method("case.mark_golden", {"case_id": str(case.id), "unmark": True})
    assert res == {"marked": False, "changed": True}
    fresh = await TestCase.get(id=case.id)
    assert "golden" not in fresh.confirm_ref

    again = await call_method("case.mark_golden", {"case_id": str(case.id), "unmark": True})
    assert again == {"marked": False, "changed": False}


@pytest.mark.asyncio
async def test_case_mark_golden_unknown_case_not_found(tortoise_db):
    from testing_platform.surface import call_method
    from testing_platform.surface.methods import MethodError

    with pytest.raises(MethodError) as exc:
        await call_method("case.mark_golden", {"case_id": str(uuid.uuid4())})
    assert exc.value.code == "not_found"
