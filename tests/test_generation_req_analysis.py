"""需求分析 + 需求评审规程单元测试（design 25 环节1 + 门⓪）。

覆盖：
- validate_analysis 四标准各正反例（R1 三分齐全 / R2 ≥80% / R3 禁假精确 / R4 P0-P1 带 verify）
- derive_outline 五级大纲骨架
- analysis_to_register_questions 转换
- validate_review 证据缺失拒 + 严重度 + TSDR 四键
- gate_review 阻塞 / 放行 / 降级三态
- build_*_prompt 包含关键规程片段（预算封顶 / 推荐答案 / risk_accepted / TSDR / 一次一问）

纯函数，零 DB / 零 LLM。
"""
from __future__ import annotations

from testing_platform.generation.req_analysis import (
    ANALYSIS_STAGES,
    INVENTORY_KEYS,
    build_analysis_prompt,
    derive_outline,
    analysis_to_register_questions,
    validate_analysis,
)
from testing_platform.generation.req_review import (
    QUESTION_BUDGET_MAX,
    REVIEW_CHECKLIST,
    REVIEW_SEVERITIES,
    build_question_prompt,
    build_review_prompt,
    gate_review,
    validate_review,
)


# ── 测试夹具 ─────────────────────────────────────────────────────────────────

def _valid_analysis() -> dict:
    """四标准全过的 analysis 样例。"""
    return {
        "evidence_class": "claimed",
        "inventory": {k: "complete" for k in INVENTORY_KEYS},
        "smells": [],
        "criteria": [
            {"id": "AC-1", "feature": "登录", "condition": "有效凭据",
             "expected": "登录成功返回 token", "priority": "P1",
             "testable": True, "verify": "POST /login 期望 200+token",
             "evidence_class": "static"},
            {"id": "AC-2", "feature": "登录", "condition": "密码错误",
             "expected": "登录拒绝 401", "priority": "P2",
             "testable": True, "verify": None, "evidence_class": "static"},
        ],
        "register": [
            {"id": "RG-1", "kind": "fact", "claim": "登录页存在",
             "source": "PRD §2.1", "evidence_class": "static"},
            {"id": "RG-2", "kind": "assumption", "claim": "假定有验证码",
             "source": None, "evidence_class": "claimed"},
            {"id": "RG-3", "kind": "to_confirm", "claim": "session TTL 取值？",
             "source": None, "evidence_class": "claimed"},
        ],
        "invariants": [
            {"id": "INV-1", "statement": "token 不得明文入日志",
             "evidence_class": "static"},
        ],
        "dependencies": [
            {"id": "DEP-1", "module": "登录", "on": "user-service",
             "ready": True, "evidence_class": "static"},
        ],
    }


def _valid_review() -> dict:
    """validate_review 全过的评审报告样例。"""
    return {
        "verdict": "conditional_pass",
        "findings": [
            {"id": "F-1", "checklist": "completeness", "severity": "major",
             "evidence": "analysis.register[RG-3] / PRD §2.4",
             "suggested_action": "补 token 过期失败路径"},
        ],
        "tsdr": [
            {"question": "session TTL 默认值", "recommended": "30 分钟",
             "actual": "30 分钟", "rationale": "与现行网关一致"},
        ],
    }


# ── ANALYSIS_STAGES 常量 ─────────────────────────────────────────────────────

def test_analysis_stages_nine_steps():
    assert len(ANALYSIS_STAGES) == 9
    ids = [s["id"] for s in ANALYSIS_STAGES]
    assert ids == [f"s{i}" for i in range(1, 10)]
    # 九步定版名（design 25 环节1）
    labels = [s["label"] for s in ANALYSIS_STAGES]
    assert labels == ["结构盘点", "输入审计", "坏味扫描", "NFR八格",
                      "行为完备", "追溯对齐", "依赖网格", "登记表", "可测性检查"]
    for s in ANALYSIS_STAGES:
        assert s["desc"]  # 每步带说明


# ── 标准 1 · 三分齐全（register kind=fact 必须带 source）─────────────────────

def test_ra1_fact_without_source_rejected():
    a = _valid_analysis()
    a["register"].append({"id": "RG-BAD", "kind": "fact",
                          "claim": "无原文引用的事实", "source": None,
                          "evidence_class": "static"})
    r = validate_analysis(a)
    assert not r.valid
    assert any(i.rule == "RA1_register_fact_needs_source" for i in r.errors)


def test_ra1_fact_with_source_passes():
    a = _valid_analysis()
    # 三种 kind 各在位、fact 带 source —— 标准1 不报
    r = validate_analysis(a)
    assert not any(i.rule.startswith("RA1") for i in r.errors)


def test_ra1_register_kind_invalid():
    a = _valid_analysis()
    a["register"].append({"id": "RG-X", "kind": "guess", "claim": "x",
                          "source": None, "evidence_class": "claimed"})
    r = validate_analysis(a)
    assert any(i.rule == "RA1_register_kind_invalid" for i in r.errors)


def test_ra1_register_missing():
    a = _valid_analysis()
    del a["register"]
    r = validate_analysis(a)
    assert any(i.rule == "RA1_register_required" for i in r.errors)


# ── 标准 2 · ≥80% 结构化 ─────────────────────────────────────────────────────

def test_ra2_testable_ratio_below_min_rejected():
    a = _valid_analysis()
    a["criteria"] = [
        {"id": "AC-1", "condition": "c", "expected": "e", "priority": "P2",
         "testable": True, "verify": "v", "evidence_class": "static"},
        {"id": "AC-2", "condition": "c", "expected": "e", "priority": "P2",
         "testable": True, "verify": "v", "evidence_class": "static"},
        {"id": "AC-3", "condition": "", "expected": "e", "priority": "P2",
         "testable": False, "verify": None, "evidence_class": "claimed"},
        {"id": "AC-4", "condition": "", "expected": "e", "priority": "P2",
         "testable": False, "verify": None, "evidence_class": "claimed"},
        {"id": "AC-5", "condition": "", "expected": "e", "priority": "P2",
         "testable": False, "verify": None, "evidence_class": "claimed"},
    ]
    r = validate_analysis(a)
    assert not r.valid
    err = next(i for i in r.errors if i.rule == "RA2_testable_ratio_below_min")
    assert "AC-3" in err.msg and "AC-5" in err.msg  # 逐条列出转不出的


def test_ra2_testable_ratio_at_min_passes():
    a = _valid_analysis()
    # 5 条全 testable → 100% ≥ 80%
    a["criteria"] = [
        {"id": f"AC-{i}", "condition": "c", "expected": "e", "priority": "P2",
         "testable": True, "verify": "v", "evidence_class": "static"}
        for i in range(1, 6)
    ]
    r = validate_analysis(a)
    assert not any(i.rule.startswith("RA2") for i in r.errors)


def test_ra2_criteria_missing():
    a = _valid_analysis()
    del a["criteria"]
    r = validate_analysis(a)
    assert any(i.rule == "RA2_criteria_required" for i in r.errors)


# ── 标准 3 · 禁假精确（复用 validate_evidence_claim 文本规则）──────────────────

def test_ra3_bare_confidence_rejected():
    a = _valid_analysis()
    a["register"].append({"id": "RG-EV1", "kind": "to_confirm",
                          "claim": "该问题 85% confident 是锁竞争导致",
                          "source": None, "evidence_class": "claimed"})
    r = validate_analysis(a)
    assert any(i.rule == "EV1_no_bare_confidence_pct" for i in r.errors)


def test_ra3_vague_blast_radius_rejected():
    a = _valid_analysis()
    a["criteria"][0]["expected"] = "该改动影响很多用户"
    r = validate_analysis(a)
    assert any(i.rule == "EV2_no_vague_blast_radius" for i in r.errors)


def test_ra3_speculation_in_claimed_rejected():
    a = _valid_analysis()
    a["register"].append({"id": "RG-EV3", "kind": "to_confirm",
                          "claim": "超时可能是网络问题或者数据库问题",
                          "source": None, "evidence_class": "claimed"})
    r = validate_analysis(a)
    assert any(i.rule == "EV3_claimed_no_speculation" for i in r.errors)


def test_ra3_clean_text_passes():
    a = _valid_analysis()
    r = validate_analysis(a)
    assert not any(i.rule.startswith("EV") for i in r.errors)


# ── 标准 4 · P0-P1 带验证方法 ─────────────────────────────────────────────────

def test_ra4_p0_without_verify_rejected():
    a = _valid_analysis()
    a["criteria"].append({"id": "AC-P0", "condition": "x", "expected": "y",
                          "priority": "P0", "testable": True, "verify": None,
                          "evidence_class": "static"})
    r = validate_analysis(a)
    assert any(i.rule == "RA4_p0p1_needs_verify" for i in r.errors)


def test_ra4_p1_with_verify_passes():
    a = _valid_analysis()
    # AC-1 已是 P1 带 verify；追加一条 P1 带 verify
    a["criteria"].append({"id": "AC-P1b", "condition": "x", "expected": "y",
                          "priority": "P1", "testable": True,
                          "verify": "POST /x 期望 400", "evidence_class": "static"})
    r = validate_analysis(a)
    assert not any(i.rule == "RA4_p0p1_needs_verify" for i in r.errors)


def test_ra4_p2_without_verify_passes():
    a = _valid_analysis()
    a["criteria"].append({"id": "AC-P2", "condition": "x", "expected": "y",
                          "priority": "P2", "testable": True, "verify": None,
                          "evidence_class": "static"})
    r = validate_analysis(a)
    assert not any(i.rule == "RA4_p0p1_needs_verify" for i in r.errors)


# ── 顶层 evidence_class 申报 ──────────────────────────────────────────────────

def test_top_level_evidence_class_required():
    a = _valid_analysis()
    del a["evidence_class"]
    r = validate_analysis(a)
    assert any(i.rule == "RA0_evidence_class_required" for i in r.errors)


# ── 整体 ─────────────────────────────────────────────────────────────────────

def test_valid_analysis_passes_all_standards():
    r = validate_analysis(_valid_analysis())
    assert r.valid, [i.to_dict() for i in r.errors]


def test_non_dict_analysis_rejected():
    r = validate_analysis("not a dict")  # type: ignore[arg-type]
    assert not r.valid
    assert any(i.rule == "RA0_not_dict" for i in r.errors)


# ── derive_outline 五级大纲 ──────────────────────────────────────────────────

def test_derive_outline_structure():
    outline = derive_outline(_valid_analysis(), "v1 登录改造")
    assert outline["level"] == "root"
    assert outline["title"] == "v1 登录改造"
    assert outline["version"] is None  # 版本占位
    modules = outline["children"]
    assert len(modules) == 1
    assert modules[0]["level"] == "module"
    assert modules[0]["name"] == "登录"  # 从 dependencies[].module 推
    features = modules[0]["children"]
    assert features and features[0]["level"] == "feature"
    assert features[0]["name"] == "登录"  # 从 criteria feature 分组
    assert "AC-1" in features[0]["criteria_ids"]
    # 测试点占位空数组
    assert all(f["testpoints"] == [] for f in features)


def test_derive_outline_unassigned_module_when_no_deps():
    a = _valid_analysis()
    a["dependencies"] = []
    outline = derive_outline(a, "t")
    assert outline["children"][0]["name"] == "未分配"


def test_derive_outline_empty_analysis():
    outline = derive_outline({}, "空需求")
    assert outline["level"] == "root"
    assert outline["children"][0]["name"] == "未分配"
    assert outline["children"][0]["children"] == []  # 无 criteria → 无功能


def test_derive_outline_multi_module_grouping():
    a = _valid_analysis()
    a["dependencies"] = [
        {"id": "DEP-1", "module": "登录", "on": "u", "ready": True},
        {"id": "DEP-2", "module": "风控", "on": "r", "ready": True},
    ]
    a["criteria"] = [
        {"id": "AC-1", "feature": "登录", "module": "登录",
         "condition": "c", "expected": "e", "priority": "P2",
         "testable": True, "verify": "v", "evidence_class": "static"},
        {"id": "AC-2", "feature": "拦截", "module": "风控",
         "condition": "c", "expected": "e", "priority": "P2",
         "testable": True, "verify": "v", "evidence_class": "static"},
    ]
    outline = derive_outline(a, "t")
    mod_names = [m["name"] for m in outline["children"]]
    assert mod_names == ["登录", "风控"]
    assert outline["children"][0]["children"][0]["name"] == "登录"
    assert outline["children"][1]["children"][0]["name"] == "拦截"


# ── analysis_to_register_questions ───────────────────────────────────────────

def test_to_confirm_register_to_questions():
    qs = analysis_to_register_questions(_valid_analysis())
    # _valid_analysis 里只有 RG-3 是 to_confirm
    assert len(qs) == 1
    q = qs[0]
    assert q["question_id"] == "Q-RG-3"
    assert q["claim"] == "session TTL 取值？"
    assert q["recommended_answer"] is None
    assert q["source"] == "ai"
    assert q["gate"] == "req_review"
    assert q["status"] == "open"
    assert q["round_no"] is None
    assert q["register_ref"] == "RG-3"


def test_only_to_confirm_converted():
    a = _valid_analysis()
    # 加一条 fact 与一条 assumption —— 不应转问题
    a["register"].extend([
        {"id": "RG-F2", "kind": "fact", "claim": "另一个事实",
         "source": "PRD §1", "evidence_class": "static"},
        {"id": "RG-A2", "kind": "assumption", "claim": "另一个假设",
         "source": None, "evidence_class": "claimed"},
    ])
    qs = analysis_to_register_questions(a)
    assert len(qs) == 1  # 仍只有 RG-3


def test_question_id_when_no_register_id():
    a = {"register": [{"kind": "to_confirm", "claim": "无 id 的待确认"}]}
    qs = analysis_to_register_questions(a)
    assert len(qs) == 1
    assert qs[0]["question_id"] == "Q-1"
    assert qs[0]["register_ref"] == 0


def test_no_register_returns_empty():
    assert analysis_to_register_questions({}) == []
    assert analysis_to_register_questions({"register": []}) == []


# ── build_analysis_prompt ────────────────────────────────────────────────────

def test_build_analysis_prompt_embeds_doc_and_stages():
    p = build_analysis_prompt("REQ 正文片段")
    assert "REQ 正文片段" in p
    # 九步都进提示词
    assert "S1" in p and "S9" in p
    for label in ["structure_inventory", "gap_register", "testability_check"]:
        assert label in p
    # schema 键
    assert "evidence_class" in p
    assert "inventory" in p and "register" in p and "criteria" in p
    assert "invariants" in p and "dependencies" in p
    # 四标准关键片段
    assert "80%" in p
    assert "P0" in p and "P1" in p and "verify" in p
    assert "fact" in p and "to_confirm" in p and "source" in p


def test_build_analysis_prompt_stable_without_doc():
    # 不传正文也要能产出完整骨架（不抛 KeyError）
    p = build_analysis_prompt("")
    assert "S1" in p
    assert "@@ANALYSIS_SCHEMA_DOC@@" not in p  # 占位被清空


# ── REVIEW_CHECKLIST ─────────────────────────────────────────────────────────

def test_review_checklist_six_items():
    assert len(REVIEW_CHECKLIST) == 6
    labels = [c["label"] for c in REVIEW_CHECKLIST]
    assert labels == ["完整", "可测", "一致", "可行", "清晰", "风险"]
    for c in REVIEW_CHECKLIST:
        assert c["subitems"]  # 各带子项说明


# ── validate_review ──────────────────────────────────────────────────────────

def test_validate_review_missing_evidence_rejected():
    review = {"verdict": "fail", "findings": [
        {"id": "F-1", "severity": "major", "evidence": None,
         "suggested_action": "x"}], "tsdr": []}
    r = validate_review(review)
    assert not r.valid
    assert any(i.rule == "RV1_finding_needs_evidence" for i in r.errors)


def test_validate_review_invalid_severity():
    review = {"findings": [
        {"id": "F-1", "severity": "urgent", "evidence": "PRD §1"}],
        "tsdr": []}
    r = validate_review(review)
    assert any(i.rule == "RV2_severity_invalid" for i in r.errors)


def test_validate_review_all_severities_accepted():
    for sev in REVIEW_SEVERITIES:
        review = {"findings": [
            {"id": "F-1", "severity": sev, "evidence": "PRD §1"}],
            "tsdr": [{"question": "q", "recommended": "r",
                      "actual": "a", "rationale": "why"}]}
        assert validate_review(review).valid, sev


def test_validate_review_tsdr_missing_key():
    review = {"findings": [], "tsdr": [
        {"question": "q", "recommended": "r", "actual": "a"}]}  # 缺 rationale
    r = validate_review(review)
    assert any(i.rule == "RV3_tsdr_key_missing" and "rationale" in i.msg
               for i in r.errors)


def test_validate_review_tsdr_missing_entire_block():
    review = {"findings": [], "tsdr": []}
    # tsdr 是空数组但存在——validate_review 不报 RV3_tsdr_required（存在即合法）
    r = validate_review(review)
    assert not any(i.rule == "RV3_tsdr_required" for i in r.errors)


def test_validate_review_tsdr_absent_rejected():
    review = {"findings": []}
    r = validate_review(review)
    assert any(i.rule == "RV3_tsdr_required" for i in r.errors)


def test_validate_review_clean():
    r = validate_review(_valid_review())
    assert r.valid, [i.to_dict() for i in r.errors]


# ── gate_review 三态 ─────────────────────────────────────────────────────────

def test_gate_fails_on_evidence_error():
    review = {"findings": [{"severity": "major", "evidence": None}], "tsdr": []}
    g = gate_review(review, [])
    assert g["passed"] is False
    assert g["status"] == "failed"


def test_gate_blocked_on_unanswered_blocking_question():
    qs = [{"question_id": "Q-1", "status": "open", "blocking": True}]
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is False
    assert g["status"] == "blocked"
    assert g["blocked_by"] == ["Q-1"]


def test_gate_passes_when_all_resolved():
    qs = [{"question_id": "Q-1", "status": "resolved", "blocking": True}]
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is True
    assert g["status"] == "passed"
    assert g["blocked_by"] == []


def test_gate_passes_with_risk_accepted_resolution():
    qs = [{"question_id": "Q-1", "status": "open",
           "resolution": "risk_accepted", "blocking": True}]
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is True


def test_gate_deadlock_exit_risk_accepted_suggestions():
    # 预算耗尽（>=15 问）仍有未答 → 自动降级 risk_accepted 建议，放行
    qs = [{"question_id": f"Q-{i}", "status": "open", "blocking": True}
          for i in range(QUESTION_BUDGET_MAX)]
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is True
    assert g["status"] == "passed"
    assert len(g["risk_accepted_suggestions"]) == QUESTION_BUDGET_MAX
    assert g["reason"] == "passed_with_risk_accepted"
    assert g["blocked_by"] == []  # 死锁出口：不阻塞


def test_gate_below_budget_blocks():
    # 14 问未答（未到预算上限）→ 仍阻塞
    qs = [{"question_id": f"Q-{i}", "status": "open", "blocking": True}
          for i in range(QUESTION_BUDGET_MAX - 1)]
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is False
    assert g["status"] == "blocked"
    assert len(g["blocked_by"]) == QUESTION_BUDGET_MAX - 1


def test_gate_non_blocking_unanswered_does_not_block():
    qs = [{"question_id": "Q-1", "status": "open", "blocking": False}]
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is True
    assert g["blocked_by"] == []


def test_gate_deadlock_suggestions_only_for_unanswered():
    # 预算耗尽但部分已答 → 只未答的进 risk 建议列表
    qs = [{"question_id": f"Q-{i}", "status": "open", "blocking": True}
          for i in range(QUESTION_BUDGET_MAX)]
    qs[0]["status"] = "resolved"
    g = gate_review(_valid_review(), qs)
    assert g["passed"] is True
    assert len(g["risk_accepted_suggestions"]) == QUESTION_BUDGET_MAX - 1


# ── build_review_prompt ──────────────────────────────────────────────────────

def test_build_review_prompt_embeds_json_and_protocol():
    p = build_review_prompt('{"evidence_class": "claimed"}')
    assert '{"evidence_class": "claimed"}' in p
    # 拷问式协议全套
    assert "recommended_answer" in p
    assert "ONE question at a time" in p  # 一次一问
    assert "10-15" in p  # 预算封顶
    assert "TSDR" in p
    # 死锁出口
    assert "risk_accepted" in p
    assert "budget" in p and "exhausted" in p
    # 清单六项
    for item in REVIEW_CHECKLIST:
        assert item["name"] in p
    # 报告结构：无证据不出现
    assert "evidence" in p and "No evidence" in p or "no finding" in p.lower() \
        or "WITHOUT evidence" in p or "without a position anchor" in p


def test_build_review_prompt_clears_placeholder():
    p = build_review_prompt("")
    assert "@@ANALYSIS_JSON@@" not in p


# ── build_question_prompt ────────────────────────────────────────────────────

def test_build_question_prompt_challenges_vague():
    p = build_question_prompt({
        "question_id": "Q-9", "claim": "这条待确认",
        "recommended_answer": "推荐甲",
    })
    assert "Q-9" in p
    assert "这条待确认" in p
    assert "推荐甲" in p
    # 挑战模糊回答
    assert "看情况" in p or "It depends" in p or "NOT an answer" in p
    # 一次一问
    assert "One question" in p
    # 推荐答案
    assert "recommended" in p.lower()
