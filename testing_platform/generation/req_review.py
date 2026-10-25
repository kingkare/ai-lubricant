"""需求评审规程（design 25 · 门⓪）——纯规程 + 纯函数，零 DB / 零 LLM。

门⓪是「三个停顿点」之首：AI 评需求文档本身质量 → 人确认需求过关才往下。
三个产物：`requirement.review` JSONB（评审报告 + TSDR 决策记录）+ `mc_test_question`
（拷问式提问，round_no 计预算）+ 人确认动作。

模块职责：

- **提示词**（:func:`build_review_prompt`）：评审清单逐项 + 报告结构（发现带严重度+
  证据[原文位置]+建议动作；无证据不出现）+ 拷问式协议全套（一次一问 / 每问带
  recommended_answer / 能查库先查 / 挑战模糊回答 / 10-15 问预算封顶 / 结束出
  TSDR[每决定=问题/推荐/实际/理由]）+ 死锁出口（预算耗尽→未答问题自动 risk_accepted）。
- **单问追问**（:func:`build_question_prompt`）：对一条问题挑战模糊回答。
- **校验**（:func:`validate_review`）：每条发现必须带 evidence + 严重度枚举合法 +
  TSDR 决策记录四键齐全。
- **放行判定**（:func:`gate_review`）：evidence error → 不过；unanswered 且未
  risk_accepted 的 blocking 问题 → 阻塞（沿用现库 :func:`quality.service.assert_not_blocked`
  语义，返回 ``blocked_by``）；预算耗尽仍有未答 → 自动降级 risk_accepted 建议。

出处：design 25 门⓪正文 + skill-library-research R4（fugazi grill-me-qa 全套）+
评审二 #1（缺独立评审环节+人确认门，致命补）。

红线：AI 不裁判，框架裁判。`assert_not_blocked` 阻塞语义由本模块在内存态复刻
（不调现库 async/DB 版本——门⓪评审是 AI 产物落库前判，尚未入 question 表）。
"""
from __future__ import annotations

from typing import Any

from testing_platform.quality.errors import (
    SEVERITY_ERROR,
    ValidationResult,
)

# ── 评审清单六项（design 25 门⓪ 步骤1；各带子项说明）─────────────────────────
#: 每项 {id, name(英文键), label(中文定版名), desc, subitems}
REVIEW_CHECKLIST: tuple[dict[str, Any], ...] = (
    {
        "id": "c1", "name": "completeness", "label": "完整",
        "desc": "需求是否完整——缺角色/状态/失败路径/异常态 = finding。",
        "subitems": ("角色齐全", "状态完备", "失败路径", "异常态", "UX 态"),
    },
    {
        "id": "c2", "name": "testability", "label": "可测",
        "desc": "能否写 condition→expected；转不出的 AC = finding。",
        "subitems": ("AC 结构化", "可观察失败条件", "verify 方法"),
    },
    {
        "id": "c3", "name": "consistency", "label": "一致",
        "desc": "目标-功能-需求-AC 链是否对齐；断链/矛盾 = finding。",
        "subitems": ("目标功能链", "需求 AC 链", "术语一致"),
    },
    {
        "id": "c4", "name": "feasibility", "label": "可行",
        "desc": "依赖是否 ready；未 ready = finding。",
        "subitems": ("接口依赖", "数据依赖", "环境依赖"),
    },
    {
        "id": "c5", "name": "clarity", "label": "清晰",
        "desc": "坏味（模糊/绝对化/断言实现）；每条坏味 = finding。",
        "subitems": ("无模糊", "无绝对化", "无断实现"),
    },
    {
        "id": "c6", "name": "risk", "label": "风险",
        "desc": "P0/P1 是否标注且带验证；漏标/无验证 = finding。",
        "subitems": ("P0/P1 标注", "验证方法", "FailureMode（P0）"),
    },
)

# ── 拷问式协议常量（fugazi grill-me-qa 全套，design 25 门⓪ 步骤3）─────────────
#: 问预算上下限（background 发现不计入预算）
QUESTION_BUDGET_MIN = 10
QUESTION_BUDGET_MAX = 15

#: 评审发现严重度枚举（前三级 = error 拒入库；后两级 = warn 标记）
REVIEW_SEVERITIES: tuple[str, ...] = ("blocker", "critical", "major", "minor", "info")
_BLOCKING_SEVERITIES: frozenset[str] = frozenset({"blocker", "critical", "major"})

#: TSDR 决策记录每条必填的四键（design 25 门⓪ 步骤3 结束出 TSDR）
TSDR_REQUIRED_KEYS: tuple[str, ...] = ("question", "recommended", "actual", "rationale")

#: gate_review 返回的 status 三态
GATE_PASSED = "passed"
GATE_FAILED = "failed"
GATE_BLOCKED = "blocked"

#: 空串/字面 null 都不算「带证据」
_FALSY_EVIDENCE = ("", "none", "null", "n/a", "na", "-")


def _has_evidence(ev: Any) -> bool:
    """finding 的 evidence 是否算「带了证据」——原文位置/引用锚点。"""
    if ev is None:
        return False
    if isinstance(ev, (list, dict)):
        # 引用列表/对象——非空即算带了
        return bool(ev)
    s = str(ev).strip()
    return bool(s) and s.lower() not in _FALSY_EVIDENCE


def _is_answered(q: dict) -> bool:
    """问题是否已答/已收敛（status=resolved 或 resolution=risk_accepted 均算收敛）。"""
    status = str(q.get("status", "open") or "open").strip().lower()
    resolution = str(q.get("resolution", "") or "").strip().lower()
    return status == "resolved" or resolution == "risk_accepted"


# ══════════════════════════════════════════════════════════════════════════════
# 提示词（门⓪ 评审清单 + 报告结构 + 拷问式协议 + 死锁出口）
# ══════════════════════════════════════════════════════════════════════════════

_REVIEW_PROMPT_BODY = """\
You are the requirement-review agent (design 25, Gate 0 "Requirement Review").

PURPOSE
  Evaluate the QUALITY of the requirement analysis JSON itself — does it make the
  cut to proceed to test-point extraction? A human confirms your verdict; your
  job is to surface every real defect and to grill the gaps into a tight,
  decision-shaped question queue.

INPUT
  The analysis JSON (Stage 1 product) is provided below under ANALYSIS_JSON.

STEP 1 — WALK THE SIX-ITEM CHECKLIST. For every item, only emit a finding when
  you have evidence; never invent one.
  - completeness  — missing roles / states / failure paths / error states.
  - testability   — AC that cannot be written as condition -> expected.
  - consistency   — goals <-> features <-> requirements <-> AC breaks or
                    contradictions.
  - feasibility   — dependencies not ready.
  - clarity       — smells (vagueness / absoluteness / implementation-coupled).
  - risk          — P0/P1 unlabeled or missing verify / failure mode.

STEP 2 — REPORT STRUCTURE. Each finding MUST carry:
  - severity: blocker | critical | major | minor | info
  - evidence:  original-document or analysis-JSON position (section / line /
               JSON path). A finding WITHOUT evidence is forbidden and is
               itself an error.
  - suggested_action: what to change so the finding goes away.
  No evidence -> no finding. A finding without a position anchor is rejected.

STEP 3 — GRILL PROTOCOL (one question at a time, full set):
  - Ask ONE question at a time. Never a batch.
  - Every question carries a "recommended_answer" (a defensible default the
    human can accept or rebut).
  - If it can be answered by querying the knowledge base / catalog / API
    specs, query first — do not ask the human.
  - Challenge vague answers: "it depends" / "看情况" is NOT an answer. Push
    to a concrete decision.
  - Question budget is capped at 10-15 (background observations do not
    count toward the budget). Track each question's round_no.
  - When done, emit the TSDR (decision log). Every decision has four keys:
    {question, recommended, actual, rationale}.

STEP 4 — DEADLOCK EXIT. If the budget is exhausted and unanswered questions
  remain, mark each unanswered question resolution="risk_accepted" (proceed
  assuming the recommended answer; affected test points are tagged
  to_confirm). The gate is NOT blocked to death by unanswered questions.
  The human may later override a risk_accepted entry back to a real answer.

OUTPUT — a single JSON object:
  {
    "verdict": "pass" | "fail" | "conditional_pass",
    "findings": [
      {"id": "F-1", "checklist": "completeness",
       "severity": "blocker", "evidence": "analysis.register[RG-3] / PRD §2.4",
       "suggested_action": "add the failure path for expired-token retry"}
    ],
    "questions": [
      {"question_id": "Q-1", "round_no": 1,
       "claim": "the gap stated as a question",
       "recommended_answer": "a defensible default",
       "gate": "req_review", "blocking": true}
    ],
    "tsdr": [
      {"question": "...", "recommended": "...", "actual": "...", "rationale": "..."}
    ],
    "budget": {"used": <int>, "max": 15, "exhausted": <bool>}
  }

ANALYSIS_JSON
  <<<
@@ANALYSIS_JSON@@
>>>

Produce ONLY the JSON object described above.
"""

#: 提示词正文里 analysis JSON 的占位 token（用 replace 而非 format——正文含字面花括号）
_REVIEW_JSON_TOKEN = "@@ANALYSIS_JSON@@"


def build_review_prompt(analysis_json: str) -> str:
    """门⓪ 评审提示词：清单逐项 + 报告结构 + 拷问式协议全套 + 死锁出口规则。

    :param analysis_json: 环节1 产出的 analysis JSON 文本（嵌入提示词作为评审对象）。
    :returns: 完整提示词正文（英文）。
    """
    return _REVIEW_PROMPT_BODY.replace(_REVIEW_JSON_TOKEN, str(analysis_json or "").strip())


_QUESTION_PROMPT_BODY = """\
Grill the following requirement-review question. Push the receiver to a
CONCRETE decision — never accept a vague answer.

QUESTION
  id:               {question_id}
  claim:            {claim}
  recommended:      {recommended_answer}

GRILL RULES
  1. One question, one answer. Do not branch into sub-surveys.
  2. If the recommended_answer can be verified by querying the catalog / API
     specs / knowledge base, verify it yourself first and report the finding
     instead of asking.
  3. "It depends" / "看情况" / "maybe" is NOT an answer. Force the receiver to
     name the condition that disambiguates, and the default under that
     condition.
  4. If the receiver cannot answer within the budget, leave it open — do NOT
     fabricate. The gate's deadlock exit (resolution=risk_accepted) handles
     budget-exhausted questions; you do not.
  5. Record the answer and a one-line rationale; both land in the TSDR under
     {question_id}.

Reply with: the verified/forced answer OR "still_open" + what is missing.
"""


def build_question_prompt(question: dict) -> str:
    """单条追问提示词片段（挑战模糊回答用）。

    :param question: 问题行（至少含 question_id / claim / recommended_answer）。
    :returns: 提示词片段（英文）。
    """
    return _QUESTION_PROMPT_BODY.format(
        question_id=str(question.get("question_id") or "?"),
        claim=str(question.get("claim") or "").strip(),
        recommended_answer=str(question.get("recommended_answer") or "(none)").strip(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 评审产物校验
# ══════════════════════════════════════════════════════════════════════════════

def validate_review(review: dict) -> ValidationResult:
    """评审报告机器校验（design 25 门⓪）。

    - 每条 finding 必须带 evidence（无 → error，design 原话「无证据不出现」）。
    - severity 必须在 :data:`REVIEW_SEVERITIES` 枚举内。
    - TSDR 决策记录每条四键齐全（question / recommended / actual / rationale）。
    """
    res = ValidationResult()
    if not isinstance(review, dict):
        res.add("$.review", "RV0_not_dict", "review 必须是 JSON 对象", SEVERITY_ERROR)
        return res

    findings = review.get("findings")
    if not isinstance(findings, list):
        res.add("$.review.findings", "RV0_findings_required",
                "findings 缺失——评审必须产出 findings 数组（无发现则空数组+verdict=pass）",
                SEVERITY_ERROR)
        findings = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            res.add(f"$.review.findings[{i}]", "RV0_finding_not_object",
                    "每条 finding 必须是对象", SEVERITY_ERROR)
            continue
        if not _has_evidence(f.get("evidence")):
            res.add(f"$.review.findings[{i}].evidence", "RV1_finding_needs_evidence",
                    "每条发现必须带证据（原文位置 / analysis JSON path）；无证据不出现",
                    SEVERITY_ERROR,
                    hint="引用 §x.y / 行号 / $.analysis.register[RG-n] / PRD 段落")
        sev = str(f.get("severity", "") or "").strip().lower()
        if sev not in REVIEW_SEVERITIES:
            res.add(f"$.review.findings[{i}].severity", "RV2_severity_invalid",
                    f"severity 必须是 {list(REVIEW_SEVERITIES)} 之一，收到 {sev!r}",
                    SEVERITY_ERROR)

    tsdr = review.get("tsdr")
    if not isinstance(tsdr, list):
        res.add("$.review.tsdr", "RV3_tsdr_required",
                "TSDR 决策记录缺失（必须为数组，每决定含 question/recommended/actual/rationale）",
                SEVERITY_ERROR)
    else:
        for j, decision in enumerate(tsdr):
            if not isinstance(decision, dict):
                res.add(f"$.review.tsdr[{j}]", "RV3_tsdr_entry_not_object",
                        "TSDR 每条必须是对象", SEVERITY_ERROR)
                continue
            for key in TSDR_REQUIRED_KEYS:
                val = decision.get(key)
                if not _non_empty(val):
                    res.add(f"$.review.tsdr[{j}].{key}", "RV3_tsdr_key_missing",
                            f"TSDR 决策记录缺「{key}」（四键 question/recommended/actual/rationale 必须齐全）",
                            SEVERITY_ERROR)
    return res


def _non_empty(val: Any) -> bool:
    """TSDR 键值非空判定：接受非空字符串或真值非 None。"""
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (list, dict)):
        return bool(val)
    return bool(val)


# ══════════════════════════════════════════════════════════════════════════════
# 放行判定（沿用现库 assert_not_blocked 阻塞语义；死锁出口 risk_accepted）
# ══════════════════════════════════════════════════════════════════════════════

def gate_review(review: dict, questions: list[dict]) -> dict:
    """门⓪ 放行判定（design 25 门⓪ 步骤4 死锁出口 + 现库 assert_not_blocked 语义）。

    判定顺序：
      1. :func:`validate_review` 有 error → ``failed``（不过；evidence/schema 错误优先）。
      2. 预算耗尽（asked 数 >= :data:`QUESTION_BUDGET_MAX`）且仍有未答 → 全部未答
         自动降级 risk_accepted 建议（死锁出口）；这些不计入 blocked_by。
      3. 未答且 blocking 的 question 存在（未耗尽预算） → ``blocked``，返回
         ``blocked_by``（复刻 assert_not_blocked：open 疑问阻塞下游）。
      4. 否则 → ``passed``（有 risk 建议时 reason=passed_with_risk_accepted）。

    :returns: ``{passed, status, blocked_by, risk_accepted_suggestions, reason}``。
    """
    schema = validate_review(review)
    if not schema.valid:
        return {
            "passed": False,
            "status": GATE_FAILED,
            "blocked_by": [],
            "risk_accepted_suggestions": [],
            "reason": "evidence_or_schema_error",
            "errors": [i.to_dict() for i in schema.errors],
        }

    asked = questions if isinstance(questions, list) else []
    unanswered: list[dict] = [q for q in asked if isinstance(q, dict) and not _is_answered(q)]
    budget_exhausted = len(asked) >= QUESTION_BUDGET_MAX

    risk_suggestions: list = []
    blocking: list = []
    for q in unanswered:
        qid = q.get("question_id")
        is_blocking = bool(q.get("blocking", True))  # req_review 默认阻塞
        if budget_exhausted:
            # 死锁出口：未答问题自动降级 risk_accepted 建议
            risk_suggestions.append(qid)
        elif is_blocking:
            blocking.append(qid)

    if blocking:
        return {
            "passed": False,
            "status": GATE_BLOCKED,
            "blocked_by": blocking,
            "risk_accepted_suggestions": risk_suggestions,
            "reason": "blocking_questions_unanswered",
        }
    return {
        "passed": True,
        "status": GATE_PASSED,
        "blocked_by": [],
        "risk_accepted_suggestions": risk_suggestions,
        "reason": "passed_with_risk_accepted" if risk_suggestions else "passed",
    }


__all__ = [
    "REVIEW_CHECKLIST",
    "QUESTION_BUDGET_MIN",
    "QUESTION_BUDGET_MAX",
    "REVIEW_SEVERITIES",
    "TSDR_REQUIRED_KEYS",
    "GATE_PASSED",
    "GATE_FAILED",
    "GATE_BLOCKED",
    "build_review_prompt",
    "build_question_prompt",
    "validate_review",
    "gate_review",
]
