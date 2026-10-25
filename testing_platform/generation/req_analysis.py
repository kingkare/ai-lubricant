"""需求分析规程（design 25 · 环节1）——纯规程 + 纯函数，零 DB / 零 LLM。

三个停顿点之一的前置产物机器面。职责边界：

- **提示词**（:func:`build_analysis_prompt`）：九步固定管线指令 + 输出 JSON schema
  + 四条入库标准硬规则段。正文英文（项目惯例 prompts-written-in-english），
  schema 键名英文与 design 25 环节1 的 analysis JSON 对齐。
- **校验**（:func:`validate_analysis`）：四标准逐条机器判——
  三分齐全 / 验收 ≥80% 结构化 / 禁假精确 / P0-P1 带验证方法。
  不合格 = 停留「分析未过」（调用方据此不推进 gen_status）。
- **大纲**（:func:`derive_outline`）：Skill5 五级大纲骨架（根=版本占位 → 模块 →
  功能 → 测试点占位空数组 → 步骤/预期占位），门①②后由调用方逐层回填。
- **问题行转换**（:func:`analysis_to_register_questions`）：register 里
  kind=to_confirm 的条目 → `mc_test_question` 行构造；register 只留 question_id 引用
  （design 25 §2.4，跨需求聚合扫表不再扫 JSONB）。

九步管线出处：codexqa `requirement-analyzer` 固定链（skill-library-research R3），
环节1 步骤定义 = design 25 环节1 正文。
四标准出处：同上 + evidence-classification 禁用模式（quality/evidence.py 已机器化）。

红线：**AI 不裁判，框架裁判**（07 §1）。AI 申报 evidence_class，本模块只做确定性判。
"""
from __future__ import annotations

from typing import Any, Optional

from testing_platform.quality.errors import (
    SEVERITY_ERROR,
    ValidationResult,
)
from testing_platform.quality.evidence import (
    EVIDENCE_CLASSES,
    EVIDENCE_CLAIMED,
    normalize_evidence_class,
    validate_evidence_claim,
)

# ── 九步固定管线（design 25 环节1；codexqa requirement-analyzer 固定链）────────
#: 每步 {id, name(英文键), label(中文定版名), desc}
ANALYSIS_STAGES: tuple[dict[str, str], ...] = (
    {
        "id": "s1", "name": "structure_inventory", "label": "结构盘点",
        "desc": "盘点需求每个结构块的完整度（goals/actors/features/data/constraints 五键），"
                "每键判 complete / thin / missing。",
    },
    {
        "id": "s2", "name": "input_audit", "label": "输入审计",
        "desc": "审计每条输入/字段的来源、可信度与可比性；陈旧或不可比的源禁止标「已对齐」。",
    },
    {
        "id": "s3", "name": "smell_scan", "label": "坏味扫描",
        "desc": "扫描措辞坏味与质量坏味（模糊、绝对化、断言实现、缺失败路径），逐条记 smells[]。",
    },
    {
        "id": "s4", "name": "nfr_grid", "label": "NFR八格",
        "desc": "非功能需求八格盘点（性能/可靠/安全/可维护/兼容/可用/合规/可测），缺格标 missing。",
    },
    {
        "id": "s5", "name": "behavior_completeness", "label": "行为完备",
        "desc": "补全成功路径、失败路径、异常态与 UX 态；缺失败路径=不完备，记入 register。",
    },
    {
        "id": "s6", "name": "traceability_alignment", "label": "追溯对齐",
        "desc": "目标↔功能↔需求↔验收标准链对齐；断链与矛盾逐条记入 register（冲突保两方观点+建议拍板人）。",
    },
    {
        "id": "s7", "name": "dependency_grid", "label": "依赖网格",
        "desc": "依赖项盘点（接口/数据/环境/服务），每条标 ready 与否；未 ready 的依赖进 register。",
    },
    {
        "id": "s8", "name": "gap_register", "label": "登记表",
        "desc": "唯一一份缺口/冲突登记表，三分：fact（必须带原文 source）/ assumption / to_confirm。"
                "禁止散列多个清单（codexqa：★唯一一份）。",
    },
    {
        "id": "s9", "name": "testability_check", "label": "可测性检查",
        "desc": "验收标准转 condition→expected；转不出的标 testable=false（不可测）。"
                "P0/P1 必须带 verify 验证方法；无可观察失败条件的条目禁止写成可执行 P0。",
    },
)

# ── schema 常量（与 build_analysis_prompt 里的 JSON schema 同源）───────────────
#: inventory 五键（结构盘点 s1 的五个结构块）
INVENTORY_KEYS: tuple[str, ...] = ("goals", "actors", "features", "data", "constraints")
#: inventory 每键完整度枚举
INVENTORY_STATES: tuple[str, ...] = ("complete", "thin", "missing")

#: register 三分（s8 的 kind 枚举）
REGISTER_KINDS: tuple[str, ...] = ("fact", "assumption", "to_confirm")

#: P0/P1 必须带 verify 验证方法（四标准之四；P0 另需 FailureMode，提示词层教）
PRIORITIES_REQUIRING_VERIFY: tuple[str, ...] = ("P0", "P1")

#: 验收标准可测占比下限（四标准之二：≥80%）
TESTABLE_RATIO_MIN = 0.8

#: 无 evidence_class 申报时文本扫描的兜底等级（分析产物天然是 claimed）
_FALLBACK_EVIDENCE_CLASS = EVIDENCE_CLAIMED

#: 空串/字面 null 都不算「带 source」
_FALSY_SOURCE = ("", "none", "null", "n/a", "na", "-")


def _has_source(src: Any) -> bool:
    """register fact 条目的 source 是否算「带了引用」。"""
    if src is None:
        return False
    s = str(src).strip()
    return bool(s) and s.lower() not in _FALSY_SOURCE


# ══════════════════════════════════════════════════════════════════════════════
# 提示词（环节1 九步 + schema + 四标准硬规则）
# ══════════════════════════════════════════════════════════════════════════════

_ANALYSIS_PROMPT_BODY = """\
You are the requirement-analysis agent (design 25, Stage 1 "Requirement Analysis").

PURPOSE
  Translate the requirement document into a structured analysis JSON.
  The downstream generation flow consumes ONLY this JSON — it never re-reads
  the raw PRD. Be exhaustive, and be honest about gaps instead of papering
  over them.

PROCEDURE — run the nine fixed stages IN ORDER. Do not skip, merge or reorder
  stages. Each stage's findings land in the JSON output below.

  S1  structure_inventory  — Inventory every structural block of the
      requirement (goals / actors / features / data / constraints, the five
      inventory keys). Grade each key: "complete" | "thin" | "missing".
  S2  input_audit          — Audit every input/field: source, trustworthiness,
      comparability. Stale or incomparable sources must NOT be marked
      "aligned"; flag them in register[].
  S3  smell_scan           — Scan for wording and quality smells (vagueness,
      absoluteness, implementation-coupled wording, missing failure paths).
      Record each in smells[].
  S4  nfr_grid             — Non-functional requirement eight-cell grid:
      performance / reliability / security / maintainability / compatibility
      / usability / compliance / testability. A cell with no coverage is
      "missing" and goes to register[].
  S5  behavior_completeness— Complete the behavior model: happy path, failure
      paths, error states, UX states. A feature with no failure path is
      incomplete — register[] it.
  S6  traceability_alignment — Align the chain goals <-> features <->
      requirements <-> acceptance criteria. Every break or contradiction in
      the chain goes to register[] (keep BOTH viewpoints for conflicts and
      name a suggested decision owner).
  S7  dependency_grid      — Inventory dependencies (APIs / data /
      environments / services); mark each ready or not. Not-ready
      dependencies go to dependencies[] AND register[].
  S8  gap_register         — Produce THE single gap-and-conflict register
      (register[]). Exactly one register — never scatter multiple lists.
      Three-way classification, every entry MUST carry "kind":
        fact       — verifiable claim, MUST carry "source" quoting the
                     original document (section / line / quote anchor);
        assumption — working assumption you had to make;
        to_confirm — open question for the human (becomes a question row).
  S9  testability_check   — Convert every acceptance criterion into
      condition -> expected. One you cannot convert: mark "testable": false.
      Every P0/P1 criterion MUST carry "verify" (verification method with an
      observable failure condition). An item with no observable failure
      condition must never be written as an executable P0.

OUTPUT — a single JSON object, exactly this shape (key names are fixed):

  {
    "evidence_class": "executed|static|inferred|claimed",   // top-level self-declaration
    "inventory": {                                          // S1, all five keys required
      "goals": "complete|thin|missing",
      "actors": "complete|thin|missing",
      "features": "complete|thin|missing",
      "data": "complete|thin|missing",
      "constraints": "complete|thin|missing"
    },
    "smells": [                                             // S3
      {"id": "SM-1", "where": "section anchor", "name": "smell name",
       "severity": "low|medium|high", "fix": "suggested fix"}
    ],
    "criteria": [                                           // S9
      {"id": "AC-1", "module": "module name (optional)", "feature": "feature name",
       "condition": "precondition / trigger", "expected": "observable expected outcome",
       "priority": "P0|P1|P2|P3",
       "testable": true,                 // false = could not convert (S9)
       "verify": "verification method",  // REQUIRED for P0/P1; null otherwise
       "evidence_class": "executed|static|inferred|claimed"}
    ],
    "register": [                                           // S8 — the ONLY register
      {"id": "RG-1", "kind": "fact|assumption|to_confirm",
       "claim": "the gap / conflict / open question",
       "source": "original-document anchor",   // REQUIRED when kind=fact
       "evidence_class": "executed|static|inferred|claimed"}
    ],
    "invariants": [                                         // always-true statements, S5/S6
      {"id": "INV-1", "statement": "invariant that must hold",
       "evidence_class": "executed|static|inferred|claimed"}
    ],
    "dependencies": [                                       // S7
      {"id": "DEP-1", "module": "owning module", "on": "what it depends on",
       "ready": true, "evidence_class": "executed|static|inferred|claimed"}
    ]
  }

HARD RULES — admission standards (machine-checked; failing any keeps the
  requirement at "analysis not passed"):

  1. THREE-WAY REGISTER COMPLETE. Every register[] entry carries kind in
     {fact, assumption, to_confirm}. Every kind=fact entry carries a non-empty
     "source" quoting the original document. A fact without a source is an
     error.
  2. AC STRUCTURED >= 80%. At least 80% of criteria[] must be testable
     (testable=true with both condition and expected filled). Below the bar
     the analysis is rejected; the unconvertible items are listed back to you.
  3. NO FAKE PRECISION. Bare confidence percentages ("85% confident" /
     "置信度90%"), vague blast radius ("影响很多用户" / "affects many"), and
     speculation inside to_confirm claims ("可能是X或Y") are all errors.
     State the observation and the verification path instead; declare
     evidence_class per claim.
  4. P0/P1 CARRY VERIFICATION. Every priority=P0 or P1 criterion must carry a
     non-empty "verify" method. P0 additionally needs a failure mode in the
     verify text. No observable failure condition = not testable = never P0.

ADDITIONAL DISCIPLINE
  - TBD is forbidden: if you do not know, it is a register[] entry with
    kind=to_confirm, not a guess.
  - Do not invent scores, percentages or ratings the document does not state.
  - Per-claim evidence_class is mandatory vocabulary; use it on every entry.

REQUIREMENT DOCUMENT TO ANALYZE
  <<<
@@ANALYSIS_SCHEMA_DOC@@
>>>

Produce ONLY the JSON object described above. No prose before or after.
"""

#: 提示词正文里需求正文的占位 token（用 replace 而非 format——正文含字面 JSON 花括号）
_ANALYSIS_DOC_TOKEN = "@@ANALYSIS_SCHEMA_DOC@@"


def build_analysis_prompt(analysis_schema_doc: str) -> str:
    """环节1 分析提示词：九步指令 + 输出 JSON schema + 四条入库标准硬规则段。

    :param analysis_schema_doc: 需求正文/验收标准文本（嵌入提示词作为分析对象；
        传空串时提示词骨架仍完整，调用方可另行拼装输入段）。
    :returns: 完整提示词正文（英文；schema 键名与 design 25 analysis JSON 一致）。
    """
    return _ANALYSIS_PROMPT_BODY.replace(
        _ANALYSIS_DOC_TOKEN, str(analysis_schema_doc or "").strip()
    )


# ══════════════════════════════════════════════════════════════════════════════
# 四标准机器判
# ══════════════════════════════════════════════════════════════════════════════

def validate_analysis(analysis: dict) -> ValidationResult:
    """四条入库标准逐条机器判（design 25 环节1；不合格=停留「分析未过」）。

    1. 三分齐全     —— register 每条 kind ∈ {fact, assumption, to_confirm}；
                       kind=fact 必须带 source 引用（error）。
    2. ≥80% 结构化  —— criteria testable 占比 < 80% → error，且逐条列出转不出的。
    3. 禁假精确     —— 裸百分数 / 模糊影响面 / 猜原因（复用
                       :func:`quality.evidence.validate_evidence_claim` 文本规则）。
    4. P0-P1 带验证方法 —— P0/P1 无 verify → error。

    :returns: ``ValidationResult``（valid=False 即「分析未过」；issue.path 可定位）。
    """
    res = ValidationResult()

    if not isinstance(analysis, dict):
        res.add("$.analysis", "RA0_not_dict",
                "analysis 必须是 JSON 对象", SEVERITY_ERROR)
        return res

    # 纪律之根：顶层证据等级必须申报（EV0 同源；申报了才谈得上分级义务）
    raw_cls = analysis.get("evidence_class")
    top_cls = normalize_evidence_class(raw_cls)
    if top_cls is None:
        res.add("$.analysis.evidence_class", "RA0_evidence_class_required",
                f"analysis 必须申报顶层 evidence_class 且为 {list(EVIDENCE_CLASSES)} 之一，收到 {raw_cls!r}",
                SEVERITY_ERROR,
                hint="声明 executed/static/inferred/claimed")

    register = analysis.get("register")
    criteria = analysis.get("criteria")

    # ── 标准 1 · 三分齐全 ─────────────────────────────────────────────────────
    if not isinstance(register, list) or not register:
        res.add("$.analysis.register", "RA1_register_required",
                "登记表（register）缺失或为空——三分齐全标准不满足",
                SEVERITY_ERROR,
                hint="产出唯一一份 register，每条 kind=fact|assumption|to_confirm")
    else:
        for i, entry in enumerate(register):
            if not isinstance(entry, dict):
                res.add(f"$.analysis.register[{i}]", "RA1_register_entry_not_object",
                        "register 每条必须是对象", SEVERITY_ERROR)
                continue
            kind = str(entry.get("kind", "") or "").strip()
            if kind not in REGISTER_KINDS:
                res.add(f"$.analysis.register[{i}].kind", "RA1_register_kind_invalid",
                        f"register 条目 kind 必须是 {list(REGISTER_KINDS)} 之一，收到 {kind!r}",
                        SEVERITY_ERROR)
            if kind == "fact" and not _has_source(entry.get("source")):
                res.add(f"$.analysis.register[{i}].source", "RA1_register_fact_needs_source",
                        "register 中 kind=fact 的条目必须带 source（原文位置/引用锚点）",
                        SEVERITY_ERROR,
                        hint="引用原文 §x.y / 行号 / 原句；引不出就降级为 assumption")

    # ── 标准 2 · 验收 ≥80% 结构化 ────────────────────────────────────────────
    if not isinstance(criteria, list) or not criteria:
        res.add("$.analysis.criteria", "RA2_criteria_required",
                "criteria 缺失或为空——可测占比无法计算（≥80% 标准不满足）",
                SEVERITY_ERROR,
                hint="把验收标准逐条转 condition→expected 填入 criteria[]")
    else:
        dict_criteria = [c for c in criteria if isinstance(c, dict)]
        untestable: list[str] = []
        for i, c in enumerate(criteria):
            if not isinstance(c, dict):
                res.add(f"$.analysis.criteria[{i}]", "RA2_criteria_entry_not_object",
                        "criteria 每条必须是对象", SEVERITY_ERROR)
                continue
            cond = str(c.get("condition", "") or "").strip()
            expected = str(c.get("expected", "") or "").strip()
            if c.get("testable") is False or not cond or not expected:
                untestable.append(str(c.get("id") or f"criteria[{i}]"))
        if dict_criteria:
            ratio = (len(dict_criteria) - len(untestable)) / len(dict_criteria)
            if ratio < TESTABLE_RATIO_MIN:
                res.add("$.analysis.criteria", "RA2_testable_ratio_below_min",
                        f"验收标准可测占比 {len(dict_criteria) - len(untestable)}/{len(dict_criteria)}"
                        f"={ratio:.0%} < {TESTABLE_RATIO_MIN:.0%}；转不出的：{'、'.join(untestable) or '（无 id）'}",
                        SEVERITY_ERROR,
                        hint="补 condition/expected；确实转不出的保持 testable=false 并记入 register")

    # ── 标准 3 · 禁假精确（文本规则，复用 evidence.py）────────────────────────
    _scan_forbidden_precision(analysis, top_cls, res)

    # ── 标准 4 · P0-P1 带验证方法 ────────────────────────────────────────────
    if isinstance(criteria, list):
        for i, c in enumerate(criteria):
            if not isinstance(c, dict):
                continue
            priority = str(c.get("priority", "") or "").strip().upper()
            if priority in PRIORITIES_REQUIRING_VERIFY and not _has_source(c.get("verify")):
                res.add(f"$.analysis.criteria[{i}].verify", "RA4_p0p1_needs_verify",
                        f"priority={priority} 的验收标准必须带 verify（可执行验证方法+可观察失败条件）",
                        SEVERITY_ERROR,
                        hint="写明 前置/激励/预期/证据/VerifyMethod；P0 另需 FailureMode")

    return res


#: 标准 3（禁假精确）只查这三条禁用模式；EV4（直证带来源）/EV5（行号宽度）/EV0（等级申报）
#: 属别的义务，不并入本条标准（task/design 明列标准3 = 裸百分数·模糊影响面·猜原因）。
_FORBIDDEN_PRECISION_RULES: tuple[str, ...] = (
    "EV1_no_bare_confidence_pct",
    "EV2_no_vague_blast_radius",
    "EV3_claimed_no_speculation",
)


def _scan_forbidden_precision(analysis: dict, top_cls: Optional[str], res: ValidationResult) -> None:
    """对 criteria/register/invariants 的申报文本跑 EV1/EV2/EV3 禁用模式（禁假精确）。

    - 条目自带 evidence_class → 用之；否则回退顶层申报；再否则兜底 claimed
      （分析产物天然是「待验证」——claimed 不触发 EV4 来源义务，只查禁用模式）。
    - 只取 :data:`_FORBIDDEN_PRECISION_RULES` 三条规则，其余 EV* 不并入本条标准；
      保留原规则号与定位 path。
    """
    texts: list[tuple[str, str, Any]] = []
    for c in analysis.get("criteria") or []:
        if isinstance(c, dict):
            base = f"$.analysis.criteria[{c.get('id') or '?'}]"
            texts.append((f"{base}.condition", str(c.get("condition", "") or ""), c.get("evidence_class")))
            texts.append((f"{base}.expected", str(c.get("expected", "") or ""), c.get("evidence_class")))
    for entry in analysis.get("register") or []:
        if isinstance(entry, dict):
            texts.append((f"$.analysis.register[{entry.get('id') or '?'}].claim",
                          str(entry.get("claim", "") or ""), entry.get("evidence_class")))
    for inv in analysis.get("invariants") or []:
        if isinstance(inv, dict):
            texts.append((f"$.analysis.invariants[{inv.get('id') or '?'}].statement",
                          str(inv.get("statement", "") or ""), inv.get("evidence_class")))

    for path, text, ec in texts:
        if not text.strip():
            continue
        cls = normalize_evidence_class(ec) or top_cls or _FALLBACK_EVIDENCE_CLASS
        sub = validate_evidence_claim(text, evidence_class=cls, path=path)
        for issue in sub.issues:
            if issue.rule in _FORBIDDEN_PRECISION_RULES:
                res.add(path, issue.rule, issue.msg, issue.severity, issue.hint)


# ══════════════════════════════════════════════════════════════════════════════
# Skill5 · PRD 转大纲（五级骨架；门①②后由调用方逐层回填）
# ══════════════════════════════════════════════════════════════════════════════

#: 测试点节点的占位形态（门①回填时每个测试点长这样；步骤/预期为其内部占位）
TESTPOINT_PLACEHOLDER: dict[str, Any] = {
    "title": None,      # 门①确认后回填
    "surfaces": [],     # 门①人改端回填
    "steps": [],        # 步骤占位（导图叶层展开即步骤+预期）
    "expected": None,   # 预期占位
}

_UNASSIGNED_MODULE = "未分配"


def derive_outline(analysis: dict, requirement_title: str) -> dict:
    """Skill5 五级大纲骨架：根（版本占位）→ 模块 → 功能 → 测试点占位空数组 → 步骤/预期占位。

    - 模块：从 dependencies[].module 推（保序去重）；inventory 值若带 modules 列表也收；
      两者皆无 → ["未分配"]。
    - 功能：criteria 按 feature 字段分组（无 feature 归「默认功能」）；criterion.module
      能对上模块名则挂该模块，否则挂首个模块（无依赖时即「未分配」）。
    - 测试点：每功能 testpoints=[] 占位空数组；步骤/预期占位形态见
      :data:`TESTPOINT_PLACEHOLDER`（门①②后由调用方逐层回填）。

    纯函数：不改 analysis，只读。
    """
    root: dict[str, Any] = {
        "level": "root",
        "title": str(requirement_title or "").strip(),
        "version": None,  # 版本占位——调用方回填 version_id/版本名
        "children": [],
    }
    if not isinstance(analysis, dict):
        return root

    modules = _derive_modules(analysis)
    criteria = analysis.get("criteria")
    by_module = _group_criteria_by_module(criteria, modules)

    for module_name in modules:
        module_node: dict[str, Any] = {"level": "module", "name": module_name, "children": []}
        for feature_name, feature_criteria in _group_features(by_module.get(module_name, [])):
            module_node["children"].append({
                "level": "feature",
                "name": feature_name,
                "criteria_ids": [c.get("id") for c in feature_criteria if c.get("id")],
                # 测试点占位空数组——门①确认后回填 TESTPOINT_PLACEHOLDER 形态
                "testpoints": [],
            })
        root["children"].append(module_node)
    return root


def _derive_modules(analysis: dict) -> list[str]:
    """模块名来源：dependencies[].module（保序去重）+ inventory 值里的 modules 列表（防御性）。

    皆无 → ["未分配"]。inventory 五键的值是完整度枚举（complete/thin/missing），
    不产模块名；仅当值是带 modules 列表的 dict 时才收（schema 容错）。
    """
    modules: list[str] = []
    for dep in analysis.get("dependencies") or []:
        if isinstance(dep, dict):
            m = dep.get("module")
            if isinstance(m, str) and m.strip() and m.strip() not in modules:
                modules.append(m.strip())
    inventory = analysis.get("inventory")
    if isinstance(inventory, dict):
        for key in INVENTORY_KEYS:
            val = inventory.get(key)
            if isinstance(val, dict):
                for m in val.get("modules") or []:
                    if isinstance(m, str) and m.strip() and m.strip() not in modules:
                        modules.append(m.strip())
    return modules or [_UNASSIGNED_MODULE]


def _group_criteria_by_module(criteria: Any, modules: list[str]) -> dict[str, list[dict]]:
    """criteria 按 module 字段归到模块；对不上模块名/未写 → 归首个模块（即「未分配」兜底）。"""
    by: dict[str, list[dict]] = {m: [] for m in modules}
    fallback = modules[0] if modules else _UNASSIGNED_MODULE
    if not isinstance(criteria, list):
        return by
    for c in criteria:
        if not isinstance(c, dict):
            continue
        m = c.get("module")
        target = m.strip() if isinstance(m, str) and m.strip() in by else fallback
        by.setdefault(target, []).append(c)
    return by


def _group_features(criteria: list[dict]) -> list[tuple[str, list[dict]]]:
    """功能分组：criterion.feature（缺省「默认功能」），保序。"""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for c in criteria:
        feat = str(c.get("feature") or "").strip() or "默认功能"
        if feat not in groups:
            groups[feat] = []
            order.append(feat)
        groups[feat].append(c)
    return [(name, groups[name]) for name in order]


# ══════════════════════════════════════════════════════════════════════════════
# register → mc_test_question 行转换（design 25 §2.4：register 只存 question_id 引用）
# ══════════════════════════════════════════════════════════════════════════════

def analysis_to_register_questions(analysis: dict) -> list[dict]:
    """register 里 kind=to_confirm 的条目 → 问题行构造列表（纯函数）。

    每行形态（对齐 design 25 §2.4 扩列 + §门⓪）：
      {question_id, claim, recommended_answer=None, source="ai",
       gate="req_review", status="open", round_no=None, register_ref}

    - ``question_id``：register 条目有 id → ``Q-<id>``；否则按序 ``Q-1/Q-2…``
    - ``register_ref``：源 register 条目的 id（无 id 用下标）——调用方据此把
      register 里该条替换成 ``{"question_id": …}`` 引用（register 只留引用）。
    - ``round_no``：留给门⓪评审的预算计数（analysis 阶段不占预算）。
    """
    questions: list[dict] = []
    register = analysis.get("register") if isinstance(analysis, dict) else None
    if not isinstance(register, list):
        return questions
    for i, entry in enumerate(register):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind", "") or "").strip() != "to_confirm":
            continue
        reg_id = entry.get("id")
        questions.append({
            "question_id": f"Q-{reg_id}" if reg_id else f"Q-{len(questions) + 1}",
            "claim": str(entry.get("claim", "") or "").strip(),
            "recommended_answer": None,
            "source": "ai",
            "gate": "req_review",
            "status": "open",
            "round_no": None,
            "register_ref": reg_id if reg_id else i,
        })
    return questions


__all__ = [
    "ANALYSIS_STAGES",
    "INVENTORY_KEYS",
    "INVENTORY_STATES",
    "REGISTER_KINDS",
    "PRIORITIES_REQUIRING_VERIFY",
    "TESTABLE_RATIO_MIN",
    "TESTPOINT_PLACEHOLDER",
    "build_analysis_prompt",
    "validate_analysis",
    "derive_outline",
    "analysis_to_register_questions",
]
