"""缺陷链方法层（25 号文 v4.1 §环节9 六步 + §环节11 回流 sink；P2）。

**本模块是纯函数 + 注入**：不 import tortoise、不查库、不调 LLM。所有「既有
数据」（开放 bug 列表、用例文档、报告、变更文件、用例索引）由调用方查库后
传进来——与 :mod:`dedup` / :mod:`coverage` / :mod:`gates` 同款纪律，因此单测
不需要 PG、不需要 Tortoise 初始化。

落位对照（25 号文第八部分「18 Skill 落位总表」）：

=====  ==============================================================  ==========
Skill  正文                                                            本模块
=====  ==============================================================  ==========
9      缺陷规范化——§环节9 第②步「规范化六段单（复现步骤继承用例 steps）」  :func:`BUG_TEMPLATE_FIELDS` / :func:`validate_bug_draft` / :func:`inherit_steps`
—      §环节9 第①步「建单前查重（pg_trgm≥0.85 提示疑似同因）」           :func:`dedup_against_open`（复用 :func:`dedup.trigram_similarity`）
7      Bug分析——§环节9 第④步八步全文（**无日志禁断根因**）              :func:`EIGHT_STEP_SKELETON` / :func:`validate_eight_step`
—      §环节9 第③步「AI 定级 vs 人工改级一致率记账（反馈环）」            :func:`severity_consistency` / :func:`consistency_rate`
—      §环节9 第⑥步「修复验证七点」                                     :func:`FIX_VERIFY_CHECKLIST` / :func:`validate_fix_verify`
17     回归范围——§环节10「必测/建议测/抽测」三级                        :func:`regression_tiers`
—      §环节11「问题库回流」统一 sink=``mc_test_question``               :data:`SINK_KINDS` / :func:`make_sink_entry`
=====  ==============================================================  ==========

**不碰的东西**（任务边界，也是并发会话热区）：缺陷状态机与守卫表在
:mod:`testing_platform.defects.models`（``BUG_ACTIONS``/``BUG_SEVERITIES``/
``BUG_PRIORITIES``），建单/根因门在 :mod:`testing_platform.defects.service`
（``create_bug_from_failure``/``attach_root_cause``），分流在
:mod:`testing_platform.defects.triage`，能力面包装在
:mod:`testing_platform.surface.methods`（``bug.triage``）。本模块只补
**方法层的校验与算法**，不重复实现上面任何一条。

为什么本模块不 import ``defects.models``
----------------------------------------
``defects/models.py`` 顶部 ``from tortoise import fields``——import 它就把整个
ORM 拉进来了，违背「纯函数、无 DB」定位。所以 ``BUG_SEVERITIES``/
``BUG_PRIORITIES`` 在本模块**镜像了一份字面量**（``SEVERITY_VALUES``/
``PRIORITY_VALUES``）。漂移风险由 :func:`validate_bug_draft` 的宽容策略兜住
（越界只 warn 不 error），并在 ``tests/test_generation_defect_flow.py`` 里钉
「两处常量集合相等」的对照断言。

歧义记录（按文档 + 任务裁定）
-----------------------------
1. **「六段单」对七个字段名**：任务给的清单是
   ``title/steps_to_reproduce/expected/actual/environment/priority/reproducibility``
   ——七个名字。§环节9 第②步叫「六段单」，故裁定 **title 是单头、其余六项才是
   「六段」**（:data:`BUG_BODY_SECTIONS` 是去掉 title 的六段视图）。校验器把
   **七个都当必填**（title 缺失本来也过不了
   :func:`defects.service.create_bug_from_failure`）。
2. **八步里没有「根因」这一格**：:data:`EIGHT_STEP_SKELETON` 是文档点名的八步
   （现象→影响模块→推荐日志→推荐SQL→关联历史bug→回归范围→风险→复核点），
   而「无日志禁断根因」管的是八步**推出来的结论**。所以根因不是第九步，而是
   报告上的两个附加键 ``root_cause`` / ``log_evidence``——
   :func:`validate_eight_step` 只在 ``root_cause`` 非空时要求 ``log_evidence``
   非空。
3. **段缺 = warn、申报非法 = error**：八步是「分析骨架」，缺格说明还没分析完
   （提示词负责教），机器门不该因为没填满就拒（否则分析中途的草稿永远过不了）；
   但**申报了非法的证据等级是纪律违规**，直接 error。等级复用
   :func:`testing_platform.quality.evidence.normalize_evidence_class`
   （executed/static/inferred/claimed，容忍 direct/claim/conjecture 等近邻写法）。
4. **「修复验证七点」文档只列了六个**：§环节9 第⑥步原文
   「修复验证七点（原路径复现过/有先失败的测试/改动最小/边界过/回归划哪/关闭
   证据）」——括号里六条。第七条补 **``related_issues_swept``「同类问题已排查
   干净」**：它正好接 §环节9 第④步的「关联历史 bug」与 :func:`defects.service
   .upsert_pattern` 的缺陷模式沉淀，且不引入文档没有新概念。**这是补的，不是
   文档原文**，改动前先读这条。
5. **``regression_tiers`` 的「一跳」怎么算**：§环节10 原文
   「release.impact diff（变更代码→入口方法→关联用例一跳）」——但 ``diff`` 在
   ``project.release.diff`` 那边，本函数只拿得到 ``changed_paths`` 与
   ``case_index``。库里唯一可用的「跳」是**需求主脊**（用例带 ``requirement_id``，
   一条需求横跨多模块）。故裁定：must=文件直命；suggested=与任一 must 用例**同
   需求但不同模块**（经需求一跳）；sample=与任一 must 用例**同模块**的其余用例。
   三档互斥，优先级 must > suggested > sample（同需求且同模块的用例进 suggested，
   它是更强的信号）。返回值是三串**排好序的 case_id**。
6. **``consistency_rate`` 空输入 ``rate=None``**：没样本就不是「0% 一致」。
   ``total=0`` 时 ``rate=None``，渲染方显示「—」，别显示 0%。
7. **sink 的 kind 落在哪一列**：§环节11 说「统一 sink=已有 ``mc_test_question``
   表（扩展 kind…）**不改表结构只扩枚举**」。``mc_test_question`` 没有 ``kind``
   列，但有 ``origin VARCHAR(32)`` 且**无 CHECK 约束**——现库已有先例
   （:func:`generation.orchestrator.run_requirement_review` 直接写
   ``origin="ai"``，而 ``QUESTION_ORIGINS`` 只有两个值）。故裁定
   **``origin`` = sink kind**（扩枚举面），``raised_by`` 留给「谁标的角色」，
   ``decided_by`` 这种 36 字符 UUID **进不了 ``raised_by``（VARCHAR(32)）**、
   只能进 ``context``。
8. **sink 行 ``status="resolved"``**：回流是**已定案的记录**，不是待答疑问。
   ``status="open"`` 会被 :func:`generation.state.compute_progress` 计入
   ``pending.unanswered``（红点/提问预算），把「沉淀」误报成「等人回答」。
   ``resolution`` 留空（沉淀没走 debug/doc/user_confirm 的收敛路径）。
9. **顶层 ``gate`` 键今天写不进库**：``gate``/``requirement_id``/``round_no``
   等六列只在 **raw DDL 扩列**里存在
   （:data:`generation.models._QUESTION_NEW_COLUMNS` +
   :func:`generation.models.ensure_question_columns`），
   ``quality.models.TestQuestion`` **没有声明这些字段**；Tortoise 的
   ``_set_kwargs`` 对未声明的 kwarg 静默跳过，所以
   ``TestQuestion.create(gate="sink")`` 不会报错、但也**不会落库**。
   :func:`make_sink_entry` 因此把 gate/module/source_ref 一律塞进 ``context``
   （真列，一定落），同时在顶层保留 ``gate`` 键给扩列后的 raw 写路径。
   → **读 sink 元数据请认 ``context``**，别认顶层列。
10. **文档 kind 字面量 vs 落库枚举**：25 号文写 ``reason/reject``（带斜杠），
   任务定版成 ``reject_reason``（既有测试 ``test_sink_kinds_are_five`` 对
   :data:`SINK_KINDS` 做**全等**断言，加第六个值会红）。五类**已齐**，不缺；
   25 号文的写法由 :func:`canonicalize_sink_kind` 在入侧归一
   （:data:`SINK_DOC_KIND_ALIASES`），落库一律存定版拼法。
11. **写入器 kind 由 source 决定、条目可覆盖**：文档那四个写入者
   （门② REJECT / 门① 打回 / bug 结案 / 返工教训）映射到
   :data:`SINK_SOURCE_KINDS` 的默认 kind——门② 与门① **两个来源共用
   ``reject_reason``**（文档只给了这一个 kind），落在哪道门记在
   ``context.source_ref.from_gate``。``pitfall``/``risk_scenario`` 不在四个
   写入者里但 kind 列了，给直写入口（写不了就是死枚举）。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from ..quality.errors import SEVERITY_ERROR, SEVERITY_WARN, ValidationResult
from ..quality.evidence import normalize_evidence_class
from .dedup import DUP_THRESHOLD_MERGE, normalize_text, trigram_similarity

__all__ = [
    # 缺陷规范化（Skill9 / §环节9 第②步）
    "BUG_TEMPLATE_FIELDS",
    "BUG_TEMPLATE_LABELS",
    "BUG_BODY_SECTIONS",
    "SEVERITY_VALUES",
    "PRIORITY_VALUES",
    "REPRODUCIBILITY_VALUES",
    "validate_bug_draft",
    "inherit_steps",
    # 建单前查重（§环节9 第①步）
    "VERDICT_SUSPECT_SAME",
    "dedup_against_open",
    # 八步分析（Skill7 / §环节9 第④步）
    "EIGHT_STEP_SKELETON",
    "EIGHT_STEP_FIELDS",
    "EIGHT_STEP_LABELS",
    "ROOT_CAUSE_FIELDS",
    "SLOW_QUERY_DIMENSIONS",
    "validate_eight_step",
    # 定级一致率（§环节9 第③步反馈环）
    "severity_consistency",
    "consistency_rate",
    # 修复验证（§环节9 第⑥步）
    "FIX_VERIFY_CHECKLIST",
    "FIX_VERIFY_FIELDS",
    "validate_fix_verify",
    # 回归三档（Skill17 / §环节10）
    "regression_tiers",
    # 问题库回流（§环节11）
    "SINK_KINDS",
    "SINK_GATE",
    "make_sink_entry",
    "SINK_SOURCES",
    "SINK_SOURCE_KINDS",
    "SINK_DOC_KIND_ALIASES",
    "canonicalize_sink_kind",
    "make_sink_entries",
]

# ── 镜像的缺陷域字面量（不 import defects.models，见模块头「为什么不 import」）─
#: 严重度（镜像 ``defects.models.BUG_SEVERITIES``，测试钉相等）
SEVERITY_VALUES: tuple[str, ...] = ("blocker", "critical", "major", "minor", "trivial")
#: 优先级（镜像 ``defects.models.BUG_PRIORITIES``，1-4 对齐禅道 pri）
PRIORITY_VALUES: tuple[int, ...] = (1, 2, 3, 4)
#: 复现率取值（文档未给枚举 → 本模块定档；越界只 warn）
REPRODUCIBILITY_VALUES: tuple[str, ...] = ("always", "sometimes", "once", "unknown")


# ════════════════════════════════════════════════════════════════
# §环节9 第①步 · 建单前查重（pg_trgm≥0.85 提示疑似同因）
# ════════════════════════════════════════════════════════════════

#: 命中处置字面量（§环节9「疑似同因，并单留痕」；与 :mod:`dedup` 的
#: ``merge``/``keep_both`` 分工不同——那边是**用例**查重，这边是**开放 bug**
#: 查重，建单前的「像」只到「疑似同因」，并单与否由人判，故不复用 merge。）
VERDICT_SUSPECT_SAME = "suspect_same"

#: bug 行的标题/正文字段别名（``mc_test_bug.title`` / ``symptom``；
#: 手动建单或前端草稿可能用 name/description）
_BUG_TITLE_KEYS: tuple[str, ...] = ("title", "name", "summary")
_BUG_DESC_KEYS: tuple[str, ...] = ("symptom", "description", "desc", "detail")
_BUG_ID_KEYS: tuple[str, ...] = ("bug_id", "id", "code")


def _pick(row: Mapping[str, Any], keys: Sequence[str]) -> str:
    """按别名顺序取第一个非空字段（转字符串）。"""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def dedup_against_open(
    title: str,
    desc: str,
    open_bugs: Sequence[Mapping[str, Any]],
    *,
    threshold: float = DUP_THRESHOLD_MERGE,
) -> list[dict[str, Any]]:
    """建单前对**开放 bug** 查重（§环节9 第①步：pg_trgm≥0.85 提示疑似同因）。

    :param title: 新 bug 标题。
    :param desc: 新 bug 症状/正文（``mc_test_bug.symptom`` 口径）。
    :param open_bugs: 开放 bug 列表，调用方查库后传入（每行
        ``{bug_id|id|code, title|name, symptom|description}``）。
    :param threshold: 命中阈值，缺省 :data:`dedup.DUP_THRESHOLD_MERGE`（0.85，
        文档唯一给定的数值）。
    :returns: ``[{bug_id, title, score, verdict: "suspect_same"}, ...]``
        按 score **降序**（同分按标题稳定）；无命中 ``[]``。

    **分数口径（歧义记录·补充）**：取两路相似度的 ``max``——
    ``sim(title, bug.title)`` 与 ``sim(title+desc, bug.title+symptom)``。
    只比标题会漏「人换了写法说同一件事」；只比拼接文本会被长症状稀释标题信号。
    取 ``max`` = 任一路命中即疑似同因（建单前提示是人看的，宁多勿漏）。

    **空文本不查重**：标题与症状都空 → ``[]``；某开放 bug 两字段都空 → 跳过该行。
    否则按 :func:`dedup.trigram_similarity` 的「都无可比内容 → 1.0」造出假命中
    （与 :func:`dedup.find_duplicates` 同款防护）。
    """
    cand_title = title or ""
    cand_desc = desc or ""
    if not normalize_text(cand_title) and not normalize_text(cand_desc):
        return []
    cand_both = f"{cand_title} {cand_desc}".strip()

    hits: list[dict[str, Any]] = []
    for row in open_bugs or ():
        if not isinstance(row, Mapping):
            continue
        row_title = _pick(row, _BUG_TITLE_KEYS)
        row_desc = _pick(row, _BUG_DESC_KEYS)
        if not normalize_text(row_title) and not normalize_text(row_desc):
            continue  # 空行不参与查重（假命中防护）
        score_title = trigram_similarity(cand_title, row_title)
        score_both = trigram_similarity(cand_both, f"{row_title} {row_desc}".strip())
        score = max(score_title, score_both)
        if score >= threshold:
            hits.append({
                "bug_id": _pick(row, _BUG_ID_KEYS) or None,
                "title": row_title,
                "score": score,
                "verdict": VERDICT_SUSPECT_SAME,
            })
    hits.sort(key=lambda h: (-h["score"], h["title"], str(h["bug_id"])))
    return hits


# ════════════════════════════════════════════════════════════════
# §环节9 第②步 · 规范化六段单（Skill9，复现步骤继承用例 steps）
# ════════════════════════════════════════════════════════════════

#: 缺陷单字段清单（title 是单头，其余六项 = :data:`BUG_BODY_SECTIONS`）。
BUG_TEMPLATE_FIELDS: tuple[str, ...] = (
    "title",
    "steps_to_reproduce",
    "expected",
    "actual",
    "environment",
    "priority",
    "reproducibility",
)

#: 中文标签（校验消息与 UI 表头共用）
BUG_TEMPLATE_LABELS: dict[str, str] = {
    "title": "标题",
    "steps_to_reproduce": "复现步骤",
    "expected": "预期结果",
    "actual": "实际结果",
    "environment": "环境",
    "priority": "优先级",
    "reproducibility": "复现率",
}

#: 「六段」= 去掉单头 title 的六段正文（歧义记录 1）
BUG_BODY_SECTIONS: tuple[str, ...] = BUG_TEMPLATE_FIELDS[1:]


def _filled(value: Any) -> bool:
    """段是否填了：字符串看 strip，容器看长度，数字/布尔看非 None。

    ``0`` 与 ``False`` 算**填了**（priority=0 是越界不是缺失，缺失由后面的
    取值校验管）。
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def inherit_steps(case_doc: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """从用例文档继承复现步骤（§环节9 第②步「复现步骤继承用例 steps」）。

    用例的步骤本来就在 ``mc_test_case.steps``（每项 ``{seq, action, expected}``，
    见 ``assets/models.py:TestCase.steps``）——建 bug 时不该让人再打一遍。

    宽容入参（提案 payload 会把用例正文套在 ``payload`` 里，正式行不会）：
    ``case_doc["steps"]`` 或 ``case_doc["payload"]["steps"]``，取第一个非空的。

    :returns: 规范成 ``[{seq, action, expected}]`` 的**新 list**（不改输入）：

        - dict 项 → 补 ``seq``（缺则按序号）、``action``（兼容
          ``text``/``description``/``step``）、``expected``（缺则 ``None``）；
        - 纯字符串项 → 包成 ``{seq, action: 字符串, expected: None}``；
        - 空白项跳过；``case_doc`` 为 None / 无 steps / steps 非列表 → ``[]``。
    """
    if not isinstance(case_doc, Mapping):
        return []
    raw = case_doc.get("steps")
    if not raw:
        nested = case_doc.get("payload")
        raw = nested.get("steps") if isinstance(nested, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []

    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, Mapping):
            action = item.get("action")
            if action in (None, ""):
                action = item.get("text") or item.get("description") or item.get("step") or ""
            if not str(action).strip():
                continue
            seq = item.get("seq")
            steps.append({
                "seq": seq if isinstance(seq, int) and not isinstance(seq, bool) else index,
                "action": str(action),
                "expected": item.get("expected"),
            })
        elif isinstance(item, str):
            if not item.strip():
                continue
            steps.append({"seq": index, "action": item, "expected": None})
        # 其他标量（int/dict 以外）直接丢——不是步骤
    return steps


def validate_bug_draft(draft: Optional[Mapping[str, Any]]) -> ValidationResult:
    """规范化六段单校验（§环节9 第②步；Skill9 的机器面）。

    :param draft: 缺陷草稿（键 = :data:`BUG_TEMPLATE_FIELDS`）。
    :returns: :class:`ValidationResult`——``errors`` 非空即**不得建单**。

    规则（每条一个规则号，供前端定位）：

    * ``BG1_section_required`` **error** —— 七字段任一缺失/空白。
      复现步骤先用 :func:`inherit_steps` 从用例继承再传进来
      （``hint`` 里就这么教）。
    * ``BG2_steps_shape`` **warn** —— steps_to_reproduce 给了非列表
      （一大段文本：能用，但八步分析与「原路径复现过」都要按步走，建议结构化）。
    * ``BG3_priority_value`` **error** —— priority 给了整数但不在 1-4
      （镜像 ``BUG_PRIORITIES``；越界是脏数据，不是缺段）。
    * ``BG4_severity_value`` **warn** —— ``severity``/``ai_severity``（可选键，
        不在七段里但 :func:`severity_consistency` 要用）越出
      :data:`SEVERITY_VALUES`。
    * ``BG5_reproducibility_value`` **warn** —— reproducibility 越出
      :data:`REPRODUCIBILITY_VALUES`（取值由本模块定档，见 :data:`SINK_KINDS`
      同级的「文档未给枚举」处理）。
    """
    res = ValidationResult()
    data = dict(draft or {})

    for field in BUG_TEMPLATE_FIELDS:
        label = BUG_TEMPLATE_LABELS[field]
        if not _filled(data.get(field)):
            res.add(
                f"$.{field}", "BG1_section_required",
                f"六段单缺「{label}」段（{field}）——缺陷单必须七项齐",
                SEVERITY_ERROR,
                hint=(
                    "复现步骤别重打：用 inherit_steps(case_doc) 继承用例 steps"
                    if field == "steps_to_reproduce" else
                    f"补「{label}」正文"
                ),
            )

    steps_value = data.get("steps_to_reproduce")
    if _filled(steps_value) and not isinstance(steps_value, (list, tuple)):
        res.add(
            "$.steps_to_reproduce", "BG2_steps_shape",
            "复现步骤建议用步骤列表（[{seq, action, expected}]），现在是整段文本",
            SEVERITY_WARN,
            hint="用 inherit_steps(case_doc) 从用例继承成结构化步骤",
        )

    priority = data.get("priority")
    if isinstance(priority, int) and not isinstance(priority, bool):
        if priority not in PRIORITY_VALUES:
            res.add(
                "$.priority", "BG3_priority_value",
                f"priority={priority!r} 越界（允许 {list(PRIORITY_VALUES)}，对齐禅道 pri）",
                SEVERITY_ERROR,
                hint="1=最高…4=最低",
            )

    for key in ("severity", "ai_severity", "final_severity"):
        if key not in data:
            continue
        value = data.get(key)
        if value in (None, "") or not isinstance(value, str):
            continue
        if value.strip().lower() not in SEVERITY_VALUES:
            res.add(
                f"$.{key}", "BG4_severity_value",
                f"severity={value!r} 不在允许集合 {list(SEVERITY_VALUES)} 内",
                SEVERITY_WARN,
                hint="blocker/critical/major/minor/trivial",
            )

    repro = data.get("reproducibility")
    if isinstance(repro, str) and repro.strip():
        if repro.strip().lower() not in REPRODUCIBILITY_VALUES:
            res.add(
                "$.reproducibility", "BG5_reproducibility_value",
                f"reproducibility={repro!r} 不在建议取值 {list(REPRODUCIBILITY_VALUES)} 内",
                SEVERITY_WARN,
                hint="always/sometimes/once/unknown",
            )

    return res


# ════════════════════════════════════════════════════════════════
# §环节9 第④步 · 八步分析（Skill7；无日志禁断根因）
# ════════════════════════════════════════════════════════════════

#: Skill7 八步骨架（顺序要紧——分析就按这个次序走）。
#: 每项 ``(字段键, 中文段名)``。
EIGHT_STEP_SKELETON: tuple[tuple[str, str], ...] = (
    ("symptom", "现象"),
    ("impact_modules", "影响模块"),
    ("recommended_logs", "推荐日志"),
    ("recommended_sql", "推荐SQL"),
    ("related_bugs", "关联历史bug"),
    ("regression_scope", "回归范围"),
    ("risks", "风险"),
    ("review_points", "复核点"),
)

#: 八步字段键（顺序同 :data:`EIGHT_STEP_SKELETON`）
EIGHT_STEP_FIELDS: tuple[str, ...] = tuple(key for key, _ in EIGHT_STEP_SKELETON)

#: 段名速查
EIGHT_STEP_LABELS: dict[str, str] = dict(EIGHT_STEP_SKELETON)

#: 八步**推出来的结论**两键（不在八步里，见歧义记录 2）
ROOT_CAUSE_FIELDS: tuple[str, str] = ("root_cause", "log_evidence")

#: Skill11 慢查询诊断五维（§环节9 第④步「推荐 SQL」段内展开）。
#: 只做**提示级**校验（命中慢查询语汇却没提任何一维 → warn）。
SLOW_QUERY_DIMENSIONS: tuple[str, ...] = (
    "执行计划", "索引", "JOIN", "ORDER BY", "LIMIT",
)

#: 段内「元数据键」（不算内容）：申报等级的各种写法
_SEGMENT_META_KEYS: frozenset[str] = frozenset(
    {"evidence_class", "evidence", "class", "evidenceLevel"}
)


def _segment_view(value: Any) -> tuple[bool, Optional[str]]:
    """一个段的值 → ``(是否填了, 申报的证据等级原文)``。

    段可以是：纯文本/列表（没申报等级）、或
    ``{"text": ..., "evidence_class": "executed"}``（申报了）。
    填没填 = **除元数据键外的任意键有内容**（兼容 ``{"trace_ids": [...],
    "evidence_class": "static"}`` 这类自定义形状——只认 text/detail 会在
    这里漏判）。
    """
    if isinstance(value, Mapping):
        declared = None
        content = False
        for key, item in value.items():
            if key in _SEGMENT_META_KEYS:
                if declared is None and item not in (None, ""):
                    declared = str(item)
            elif _filled(item):
                content = True
        return content, declared
    return _filled(value), None


def validate_eight_step(report: Optional[Mapping[str, Any]]) -> ValidationResult:
    """八步分析校验（§环节9 第④步；Skill7 的机器面）。

    :param report: 分析报告。八步各键见 :data:`EIGHT_STEP_FIELDS`，值为纯文本/
        列表或 ``{text, evidence_class}``；外加 ``root_cause`` / ``log_evidence``。
    :returns: :class:`ValidationResult`——**有 error 就不得下根因结论**。

    规则：

    * ``E7_root_cause_needs_logs`` **error** —— ``root_cause`` 非空而
      ``log_evidence`` 空 → 消息含 **「无日志禁断根因」**（文档硬红线：
      §环节9 第④步「**无日志禁断根因**」，与 :func:`defects.service
      .attach_root_cause` 的根因门同源，那里查 ``evidence_chain`` 非空，
      这里查分析阶段的日志证据）。
    * ``E7_evidence_class_invalid`` **error** —— 段申报了证据等级但
      :func:`normalize_evidence_class` 认不出来（纪律违规）。
    * ``E7_step_missing`` **warn** —— 八步某段没填（分析没走完；提示补，不拒）。
    * ``E7_step_class_missing`` **warn** —— 段填了却没申报等级（该教不该拦，
      见歧义记录 3）。
    * ``E7_sql_slow_query_dims`` **warn** —— ``recommended_sql`` 像是慢查询诊断
      （出现 慢/slow/超时/timeout/性能/perf）却没提 :data:`SLOW_QUERY_DIMENSIONS`
      任何一维（Skill11 五维没展开）。
    """
    res = ValidationResult()
    data = dict(report or {})

    # ── 硬红线：无日志禁断根因 ──────────────────────────────────
    has_root_cause = _filled(data.get("root_cause"))
    has_log_evidence = _filled(data.get("log_evidence"))
    if has_root_cause and not has_log_evidence:
        res.add(
            "$.root_cause", "E7_root_cause_needs_logs",
            "无日志禁断根因：root_cause 已下结论但 log_evidence 为空",
            SEVERITY_ERROR,
            hint="先做 §环节9 第⑤步日志诊断（traceId→分页≤20→跨服务→定位代码行），"
                 "把日志片段/查询结果填进 log_evidence；拿不到日志就把结论降级为 "
                 "evidence_class=claimed 并写明验证路径",
        )

    # ── 八步逐段 ────────────────────────────────────────────────
    for field, label in EIGHT_STEP_SKELETON:
        filled, declared = _segment_view(data.get(field))
        if declared is not None:
            if normalize_evidence_class(declared) is None:
                res.add(
                    f"$.{field}.evidence_class", "E7_evidence_class_invalid",
                    f"「{label}」段申报的证据等级非法：{declared!r}",
                    SEVERITY_ERROR,
                    hint="只能是 executed/static/inferred/claimed",
                )
        elif filled:
            res.add(
                f"$.{field}", "E7_step_class_missing",
                f"「{label}」段没申报证据等级",
                SEVERITY_WARN,
                hint="给这段结论标 executed/static/inferred/claimed",
            )
        if not filled:
            res.add(
                f"$.{field}", "E7_step_missing",
                f"八步分析缺「{label}」段（{field}）",
                SEVERITY_WARN,
                hint="按 Skill7 八步逐段补全（现象→影响模块→推荐日志→推荐SQL→"
                     "关联历史bug→回归范围→风险→复核点）",
            )

    # ── Skill11 慢查询五维（提示级）──────────────────────────────
    _, sql_declared = _segment_view(data.get("recommended_sql"))
    sql_text = data.get("recommended_sql")
    sql_body = (
        " ".join(str(v) for v in sql_text.values()) if isinstance(sql_text, Mapping)
        else " ".join(str(v) for v in sql_text) if isinstance(sql_text, (list, tuple))
        else str(sql_text or "")
    )
    if sql_declared is not None or (sql_body and sql_body.strip()):
        lowered = sql_body.lower()
        looks_slow = any(tok in lowered for tok in ("慢", "slow", "超时", "timeout", "性能", "perf"))
        if looks_slow and not any(dim.lower() in lowered for dim in SLOW_QUERY_DIMENSIONS):
            res.add(
                "$.recommended_sql", "E7_sql_slow_query_dims",
                "推荐SQL 段像在做慢查询诊断，但没展开 Skill11 五维（执行计划/索引/"
                "JOIN/ORDER BY/LIMIT）",
                SEVERITY_WARN,
                hint="五维逐条给建议，别只贴一条 SQL",
            )

    return res


# ════════════════════════════════════════════════════════════════
# §环节9 第③步 · AI 定级 vs 人工改级一致率（反馈环）
# ════════════════════════════════════════════════════════════════

def severity_consistency(ai_severity: Any, final_severity: Any) -> bool:
    """AI 定级与人工终态是否一致（§环节9 第③步「一致率记账」的单条判定）。

    归一 = strip + 小写（``Major`` 与 ``major`` 算一致）。
    **任一侧空白 → False**：AI 没定级就不是「和人不矛盾」，是无信号，
    计入不一致才不让反馈环虚高（空对空也算 False，见
    :func:`consistency_rate`——调用方本该把没定级的样本剔出去再算）。
    """
    a = str(ai_severity or "").strip().lower()
    b = str(final_severity or "").strip().lower()
    if not a or not b:
        return False
    return a == b


def consistency_rate(pairs: Optional[Sequence[Any]]) -> dict[str, Any]:
    """定级一致率统计（§环节9 第③步的反馈环账本）。

    :param pairs: ``(ai_severity, final_severity)`` 二元组序列。长度不为 2 的
        项、非列表项 → 计入 ``malformed`` 并**剔出分母**（脏数据不该拉低一致率）。
    :returns: ``{"rate": float|None, "total": int, "mismatches": [{"ai","final"}]}``

        - ``total``：参与统计的样本数（已剔畸形项）；
        - ``mismatches``：**不一致的样本清单**（不只给数——反馈环要看
          「AI 把什么判轻了/判重了」才叫得出教训）；
        - ``rate = (total - len(mismatches)) / total``，保留 4 位小数；
          ``total == 0`` 时 ``rate=None``（歧义记录 6：没样本不是 0% 一致）。
    """
    valid: list[Any] = []
    malformed = 0
    for pair in pairs or ():
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            valid.append(pair)
        else:
            malformed += 1

    mismatches: list[dict[str, Any]] = []
    for ai_value, final_value in valid:
        if not severity_consistency(ai_value, final_value):
            mismatches.append({
                "ai": None if ai_value in (None, "") else str(ai_value),
                "final": None if final_value in (None, "") else str(final_value),
            })

    total = len(valid)
    rate = None if total == 0 else round((total - len(mismatches)) / total, 4)
    out: dict[str, Any] = {"rate": rate, "total": total, "mismatches": mismatches}
    if malformed:
        out["malformed"] = malformed
    return out


# ════════════════════════════════════════════════════════════════
# §环节9 第⑥步 · 修复验证七点
# ════════════════════════════════════════════════════════════════

#: 修复验证七点（文档原列六点 + 第七点，见歧义记录 4）。
#: 每项 ``{key, label, hint}``：``hint`` 是「这点是 false 时该做什么」。
FIX_VERIFY_CHECKLIST: tuple[dict[str, str], ...] = (
    {
        "key": "original_path_reproduced",
        "label": "原路径复现过",
        "hint": "按原复现步骤再跑一遍（同环境同数据），不能只说「应该好了」",
    },
    {
        "key": "failing_test_first",
        "label": "有先失败的测试",
        "hint": "补一条修复前红、修复后绿的测试，否则这次修的东西没有回归保护",
    },
    {
        "key": "minimal_change",
        "label": "改动最小",
        "hint": "顺手重构请另开单，混在一起会让回滚判断失效",
    },
    {
        "key": "boundary_passed",
        "label": "边界过",
        "hint": "把该功能点的边界/异常用例跑一遍，不只跑原用例",
    },
    {
        "key": "regression_scope_defined",
        "label": "回归划哪",
        "hint": "用 regression_tiers(changed_paths, case_index) 划必测/建议测/抽测三档",
    },
    {
        "key": "related_issues_swept",
        "label": "同类问题已排查干净",
        "hint": "按 fingerprint/症状签名扫同因 bug（缺陷模式 upsert_pattern），"
             "确认不是只修了一个实例",
    },
    {
        "key": "closing_evidence",
        "label": "关闭证据",
        "hint": "关闭要带证据（执行结果/日志/截图），并回链 run/场景 id",
    },
)

#: 七点字段键
FIX_VERIFY_FIELDS: tuple[str, ...] = tuple(item["key"] for item in FIX_VERIFY_CHECKLIST)

#: key → 条目（校验消息用）
_FIX_VERIFY_INDEX: dict[str, dict[str, str]] = {
    item["key"]: item for item in FIX_VERIFY_CHECKLIST
}


def validate_fix_verify(answers: Optional[Mapping[str, Any]]) -> ValidationResult:
    """修复验证七点校验（§环节9 第⑥步）。

    :param answers: ``{七点键: bool}``——**七点都必须是 bool**。
    :returns: :class:`ValidationResult`。

    * ``FV1_answer_required`` **error** —— 缺任一点，或值不是 bool
      （「差不多」不是答案，要么 true 要么 false）。
    * ``FV2_not_satisfied`` **warn** —— 某点是 ``False``：给出该点的 ``hint``
      说明。**七点没全 true 不拦关闭**（拦了就会逼人把 false 写成 true），
      但每条 warn 都要在关闭说明里回应——这是 §环节11 回流的原料。
    """
    res = ValidationResult()
    data = dict(answers or {})

    for key in FIX_VERIFY_FIELDS:
        item = _FIX_VERIFY_INDEX[key]
        present = key in data
        value = data.get(key)
        if not present or not isinstance(value, bool):
            res.add(
                f"$.{key}", "FV1_answer_required",
                f"修复验证七点缺「{item['label']}」的布尔答案（{key}）"
                + ("" if present else "，或值不是 true/false"),
                SEVERITY_ERROR,
                hint=item["hint"],
            )
            continue
        if value is False:
            res.add(
                f"$.{key}", "FV2_not_satisfied",
                f"「{item['label']}」未满足",
                SEVERITY_WARN,
                hint=item["hint"],
            )
    return res


# ════════════════════════════════════════════════════════════════
# §环节10 · 回归三档（Skill17 必测/建议测/抽测）
# ════════════════════════════════════════════════════════════════

def _path_forms(path: Any) -> set[str]:
    """路径归一成可比对形态（斜杠统一、去 ``./``、小写——Windows 分隔符兼容）。"""
    text = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not text:
        return set()
    if text.startswith("./"):
        text = text[2:]
    return {text.lower()}


def _path_hit(changed: str, declared: str) -> bool:
    """变更路径与用例声明的文件是否算同一处。

    精确相等 **或** 一方是另一方的路径后缀（``a/b/c.py`` vs ``b/c.py``）——
    ``release.impact`` 给的仓库相对路径和用例里写的模块相对路径本来就差前缀。
    **不做 basename 匹配**（不同目录同名文件会误命中）。
    """
    if not changed or not declared:
        return False
    if changed == declared:
        return True
    return changed.endswith("/" + declared) or declared.endswith("/" + changed)


def regression_tiers(
    changed_paths: Optional[Sequence[Any]],
    case_index: Optional[Sequence[Mapping[str, Any]]],
) -> dict[str, list[str]]:
    """变更文件 → 回归三档（§环节10 / Skill17「必测/建议测/抽测」）。

    :param changed_paths: 变更文件路径列表。
    :param case_index: 用例索引行，每行
        ``{case_id, module, requirement_id, files: [...]}``（调用方查库后传入）。
    :returns: ``{"must": [case_id...], "suggested": [...], "sample": [...]}``——
        三档**互斥**、各自按 case_id 升序；没关联上的用例不出现在任何一档。

    分档（歧义记录 5）：

    * **must 必测** = ``files`` 命中任一变更路径（覆盖变更的直接功能）。
    * **suggested 建议测** = 与任一 must 用例**同 requirement_id 但不同 module**
      ——经需求主脊一跳到的其它模块（本函数拿不到代码调用图，需求是索引里
      唯一可用的「跳」）。
    * **sample 抽测** = 与任一 must 用例**同 module** 的其余用例（同模块抽样，
      优先级最低：must > suggested > sample）。

    ``module``/``requirement_id``/``case_id`` 为空的行只能靠 ``files`` 进 must
    （没有模块/需求就没有「关联」可言）。``changed_paths`` 为空 → 三档全空。
    """
    empty = {"must": [], "suggested": [], "sample": []}
    paths = {p for p in (_path_forms(cp) for cp in (changed_paths or ())) for p in p}
    rows = [r for r in (case_index or ()) if isinstance(r, Mapping)]
    if not paths or not rows:
        return empty

    def _case_id(row: Mapping[str, Any]) -> Optional[str]:
        value = row.get("case_id", row.get("id"))
        if value in (None, ""):
            return None
        return str(value)

    must_ids: set[str] = set()
    for row in rows:
        cid = _case_id(row)
        if cid is None:
            continue
        declared = {p for p in (_path_forms(f) for f in (row.get("files") or ())) for p in p}
        if declared and any(_path_hit(c, d) for c in paths for d in declared):
            must_ids.add(cid)
    if not must_ids:
        return empty  # 没有直命用例 → 无从定义「一跳/同模块」

    anchor_modules: set[str] = set()
    anchor_requirements: set[str] = set()
    for row in rows:
        cid = _case_id(row)
        if cid in must_ids:
            module = row.get("module")
            requirement = row.get("requirement_id")
            if module not in (None, ""):
                anchor_modules.add(str(module))
            if requirement not in (None, ""):
                anchor_requirements.add(str(requirement))

    suggested_ids: set[str] = set()
    sample_ids: set[str] = set()
    for row in rows:
        cid = _case_id(row)
        if cid is None or cid in must_ids:
            continue
        module = row.get("module")
        requirement = row.get("requirement_id")
        same_module = module not in (None, "") and str(module) in anchor_modules
        same_requirement = requirement not in (None, "") and str(requirement) in anchor_requirements
        if same_requirement and not same_module:
            suggested_ids.add(cid)  # 一跳：同需求、别的模块
        elif same_requirement and same_module:
            suggested_ids.add(cid)  # 同需求信号更强，归建议测（歧义记录 5 的优先级）
        elif same_module:
            sample_ids.add(cid)     # 同模块其余 → 抽测

    return {
        "must": sorted(must_ids),
        "suggested": sorted(suggested_ids),
        "sample": sorted(sample_ids),
    }


# ════════════════════════════════════════════════════════════════
# §环节11 · 问题库回流 sink（统一 sink=已有 mc_test_question）
# ════════════════════════════════════════════════════════════════

#: 回流五种 kind（§环节11「扩展 kind」；任务定版字面量）。
#: 文档原文写「reason/reject」，任务定版成单个 ``reject_reason``——取任务定版。
SINK_KINDS: tuple[str, ...] = (
    "reject_reason",    # 门② REJECT 原因 / 门① 打回原因
    "rework_lesson",    # 返工教训（review_round 回派的因）
    "bug_rootcase",     # bug 结案时的根因
    "pitfall",          # 踩过的坑（日志/环境/数据）
    "risk_scenario",    # 风险场景（当时没测到的那类）
)

#: sink 行的「出自哪道门」取值——**不是** §2.4 的 ``req_review``/
#: ``point_confirm``，沉淀不属于任何道门，单开一个值（歧义记录 9）。
SINK_GATE = "sink"


def _jsonify(value: Any) -> Any:
    """JSONB 可写形态（UUID/datetime 等 → str，容器递归）。

    与 ``defects.service._jsonify`` 同语义——这里独立实现，不 import 缺陷域。
    """
    if isinstance(value, Mapping):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    if isinstance(value, (int, float, bool, str)) or value is None:
        return value
    return str(value)


def make_sink_entry(
    kind: str,
    text: str,
    *,
    module: Optional[str] = None,
    source_ref: Optional[Any] = None,
) -> dict[str, Any]:
    """造一条「问题库行」（§环节11 统一 sink；**写库由调用方做**）。

    :param kind: :data:`SINK_KINDS` 之一，越界抛 :class:`ValueError`。
    :param text: 沉淀正文（REJECT 理由 / 教训 / 根因 / 坑 / 风险场景）。
    :param module: 模块归属（文档要「按 module 列索引」，但 ``mc_test_question``
        **没有 module 列**——只能进 ``context``，见歧义记录 7/9）。
    :param source_ref: 来源引用（dict 或字符串；如
        ``{proposal_id, run_id, decided_by}``）。UUID 会被转成字符串。
    :returns: ``mc_test_question`` 行形状的 dict（键 = 模型真实列名），调用方
        ``TestQuestion.create(project_id=..., **entry)`` 即可落库。

    列映射（歧义记录 7/8/9）：

    * ``origin`` = ``kind``（``VARCHAR(32)`` 无 CHECK，扩枚举即此列；
      现库已有 ``origin="ai"`` 的先例）——**读侧按它分拣 sink 种类**。
    * ``content`` = text（必填非空）。
    * ``raised_by`` = None ——它是「提出角色」``VARCHAR(32)``，装不下 36 字符
      UUID，所以 ``decided_by`` 请放 ``source_ref``（进 context）。
    * ``status`` = ``"resolved"`` ——沉淀不是待答疑问，``open`` 会被
      :func:`generation.state.compute_progress` 计入 ``pending.unanswered``。
    * ``resolution`` = None、``blocking_refs`` = ``[]`**——沉淀绝不阻塞下游**
      （现库 ``assert_not_blocked`` 只认 ``status=open`` 且 refs 命中的行）。
    * ``context`` = ``{"kind","gate":"sink","sink":True,"module","source_ref"}``
      ——``gate``/``module`` 都在这（顶层 ``gate`` 键同时给出，供扩列后的
      raw 写路径用；Tortoise 模型没这列，走 ORM 会被静默丢弃）。
    """
    clean_kind = str(kind or "").strip()
    if clean_kind not in SINK_KINDS:
        raise ValueError(
            f"sink kind={kind!r} 不在允许集合 {list(SINK_KINDS)} 内"
        )
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("sink text 必填（沉淀进问题库的那句话到底是什么）")

    normalized_ref = _jsonify(source_ref) if source_ref not in (None, "") else None
    normalized_module = str(module).strip() if module not in (None, "") else None

    context = {
        "kind": clean_kind,
        "gate": SINK_GATE,
        "sink": True,
        "module": normalized_module,
        "source_ref": normalized_ref,
    }
    return {
        "origin": clean_kind,          # 扩枚举面（读侧分拣键）
        "raised_by": None,             # 角色名，别塞 UUID（VARCHAR(32)）
        "content": clean_text,
        "context": context,
        "status": "resolved",
        "resolution": None,
        "resolution_detail": {},
        "blocking_refs": [],
        # 顶层扩展键：DB 扩列（generation.models._QUESTION_NEW_COLUMNS）后由
        # raw 写路径认领；走 Tortoise 模型会被静默丢弃（歧义记录 9）。
        "gate": SINK_GATE,
        "module": normalized_module,
        "kind": clean_kind,
    }


# ── 回流写入器（§环节11 四类「写入者」→ 统一 sink 条目）────────────
#
# §环节11 只说了**谁**往问题库里写（门② REJECT 原因 / 门① 打回原因 / bug 结案 /
# 返工教训），没说**怎么批量转**。``make_sink_entry`` 是单条造形，调用方（
# :func:`surface.gen_methods.case_reject_sink`）得自己认字段、自己拼 trace。
# 下面的写入器把那四类原始记录**统一转成可入库条目**：来源决定 kind，条目里的
# 正文/模块/引用按别名顺序取（与 ``case_reject_sink`` 现有取数口径一致——理由
# 优先、回退 ``provenance.reject_reason``，模块优先、回退 ``payload.module`` /
# ``surface``）。**纯函数，不写库**，返回 dict 列表交给调用方。

#: 25 号文点名的四类写入者 + 另两个 kind 的直写入口（**键 = source，值 = kind**）。
#:
#: * ``gate2_reject``/``gate1_reject`` 是**两个来源、一个 kind**（文档把「门②
#:   REJECT 原因」和「门① 打回原因」写成两个写入者，但 kind 只给了
#:   ``reason/reject`` 一个）——落在哪道门由 ``context.source_ref.from_gate`` 记，
#:   不去动 ``context.gate``（那个键已被 ``SINK_GATE="sink"`` 占了，歧义记录 9）。
#: * ``bug_close`` 对应「bug 结案」——结案时沉淀的是**根因**（§环节9 第④步的产物）。
#: * ``pitfall``/``risk_scenario`` 不在文档那四个写入者里，但 kind 列了，
#:   执行波次/回归复盘要写它们，故一并给入口（写不了就是死枚举）。
SINK_SOURCE_KINDS: dict[str, str] = {
    "gate2_reject": "reject_reason",    # 门② 提案 REJECT 的理由
    "gate1_reject": "reject_reason",    # 门① 需求评审打回的理由
    "bug_close": "bug_rootcase",        # bug 结案时的根因
    "rework_lesson": "rework_lesson",   # 返工教训（review_round 回派）
    "pitfall": "pitfall",               # 踩过的坑
    "risk_scenario": "risk_scenario",   # 当时没测到的那类风险
}

#: 合法的 source 名单（= :data:`SINK_SOURCE_KINDS` 的键集，给报错和测试用）。
SINK_SOURCES: tuple[str, ...] = tuple(SINK_SOURCE_KINDS)

#: **文档字面量 → 落库枚举**的对照。25 号文写的是 ``reason/reject``（带斜杠），
#: 本库定版成 ``reject_reason``（见上面 SINK_KINDS 的注释与既有测试
#: ``test_sink_kinds_are_five``——那行断言是**全等**，加第六个值就会红）。
#: 所以这里做的是**入侧别名归一**，不是扩表：文档那五类都能写，落库存的是
#: 定版拼法。另四类文档名与库内拼法本就一致，收在表里纯粹为了让读侧一处查全。
SINK_DOC_KIND_ALIASES: dict[str, str] = {
    "reason/reject": "reject_reason",
    "reason": "reject_reason",
    "reject": "reject_reason",
    "reject_reason": "reject_reason",
    "bug_rootcase": "bug_rootcase",
    "rework_lesson": "rework_lesson",
    "pitfall": "pitfall",
    "risk_scenario": "risk_scenario",
}

#: 各 kind 的正文别名（按优先级；只收标量值，容器不当正文用）。
_SINK_TEXT_KEYS: dict[str, tuple[str, ...]] = {
    "reject_reason": ("reason", "reject_reason", "sendback_reason", "comment"),
    "bug_rootcase": ("root_cause", "rootcause", "root_case", "conclusion"),
    "rework_lesson": ("lesson", "rework_lesson", "takeaway", "reason"),
    "pitfall": ("pitfall", "trap", "issue"),
    "risk_scenario": ("scenario", "risk_scenario", "risk"),
}
#: 通用兜底正文键（kind 专属别名都没命中时按这个顺序找）。
_SINK_TEXT_TAIL: tuple[str, ...] = ("text", "content", "detail", "note", "message")

#: 模块归属别名（``mc_test_question`` 没有 module 列，值进 ``context.module``）。
_SINK_MODULE_KEYS: tuple[str, ...] = ("module", "module_name", "module_path", "surface")

#: 来源 trace 里保留的引用键（其余键一律不进 sink 行，免得把整张原表灌进 JSONB）。
_SINK_TRACE_KEYS: tuple[str, ...] = (
    "proposal_id", "case_id", "bug_id", "code", "run_id", "version_id",
    "requirement_id", "testpoint_id", "round_no", "from_gate", "gate",
    "decided_by", "rejected_by", "reviewed_by", "reported_by", "title",
)


def canonicalize_sink_kind(value: Any) -> str:
    """把 25 号文的 kind 字面量归一成落库枚举（歧义记录 10）。

    :param value: ``"reason/reject"`` / ``"reject"`` / ``"bug_rootcase"`` /
        已定版的 ``"reject_reason"`` 均可，大小写与首尾空白不敏感。
    :returns: :data:`SINK_KINDS` 里的那一个值。
    :raises ValueError: 归一不出五类之一（不做「猜一个最像的」——猜错就是脏数据）。
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"sink kind 不能为空（允许值 {list(SINK_DOC_KIND_ALIASES)}）")
    lowered = raw.lower()
    for alias, kind in SINK_DOC_KIND_ALIASES.items():
        if lowered == alias.lower():
            return kind
    raise ValueError(
        f"未知 sink kind={value!r}——25 号文五类为 reason/reject、bug_rootcase、"
        f"rework_lesson、pitfall、risk_scenario（落库枚举 {list(SINK_KINDS)}）"
    )


def _sink_view(item: Mapping[str, Any]) -> dict[str, Any]:
    """条目 + 惯常嵌套袋子的扁平视图（顶层键优先）。

    真实原始行长这样：提案把拒绝理由藏在 ``provenance.reject_reason``、模块藏在
    ``payload.module``（:func:`surface.gen_methods.case_reject_sink` 的取数口径
    就是先顶层后这两个袋子）；bug 行多为平铺。这里把 ``provenance``/``payload``/
    ``resolution_detail`` 三个 Mapping 袋子**浅展开**进来（同名键以顶层为准），
    下面的正文/模块/trace 查找就都只看一层。
    """
    view: dict[str, Any] = dict(item)
    for bag in ("provenance", "payload", "resolution_detail"):
        nested = item.get(bag)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                if value not in (None, ""):
                    view.setdefault(str(key), value)
    return view


def _sink_text_keys(kind: str, fallback_kinds: Sequence[str] = ()) -> list[str]:
    """kind 的正文别名序列：专属别名 → 兜底 kind 的别名 → 通用尾巴。"""
    keys = list(_SINK_TEXT_KEYS.get(kind) or ())
    for other in fallback_kinds:
        keys += [k for k in (_SINK_TEXT_KEYS.get(other) or ()) if k not in keys]
    keys += [k for k in _SINK_TEXT_TAIL if k not in keys]
    return keys


def _sink_text(
    kind: str, view: Mapping[str, Any], fallback_kinds: Sequence[str] = ()
) -> str:
    """按别名顺序取沉淀正文；容器值不算正文（别把 dict 印进 content）。

    ``fallback_kinds`` 只在**条目覆盖了 kind** 时给（见 ``make_sink_entries``）：
    一条 bug 根因记录被标成 ``pitfall``，正文仍写在 ``root_cause`` 列——来源 kind
    的别名要能被认，否则覆盖功能就是鸡肋。
    """
    for key in _sink_text_keys(kind, fallback_kinds):
        value = view.get(key)
        if value is None or isinstance(value, (Mapping, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _sink_module(view: Mapping[str, Any], default: Optional[str]) -> Optional[str]:
    """模块归属：条目（含袋子）自己的别名 → 批量兜底值（都没有就 None）。"""
    for key in _SINK_MODULE_KEYS:
        value = view.get(key)
        if value not in (None, "") and not isinstance(value, (Mapping, list, tuple, set)):
            text = str(value).strip()
            if text:
                return text
    if default not in (None, ""):
        text = str(default).strip()
        return text or None
    return None


def _sink_trace(
    source: str, item: Mapping[str, Any], view: Mapping[str, Any]
) -> dict[str, Any]:
    """来源 trace：这条沉淀出自哪条 case / 哪次 reject / 哪个 bug。

    ``written_by`` 记的是**写入器来源**（比 kind 更细：门① 还是门②），
    ``from_gate`` 记条目自带的门名（``gate`` 键也归一到它——``context.gate``
    已经被 ``SINK_GATE="sink"`` 占用，两条口径不能互相盖，歧义记录 9）。
    调用方自己塞的 ``source_ref`` / ``ref`` 会**浅合并**进来，同名的显式引用键
    （``proposal_id`` 等）覆盖它，``source``/``written_by`` 两个键一律以本函数
    归一出的来源为准——调用方塞的袋子不能冒充写入器。

    **主键回退**：调用方直接把查库行塞进来时（提案行/bug 行），行主键叫 ``id``
    而不是 ``proposal_id``/``bug_id``——门来源把它认成 ``proposal_id``、
    ``bug_close`` 认成 ``bug_id``（``rework_lesson`` 等的行主键不是用例 id，
    **不猜**，要 trace 就显式给 ``case_id``）。
    """
    trace: dict[str, Any] = {}
    merged: dict[str, Any] = {}
    for bag in ("source_ref", "ref"):
        value = item.get(bag)
        if isinstance(value, Mapping):
            merged.update({str(k): v for k, v in value.items()})
    for key in _SINK_TRACE_KEYS:
        value = view.get(key)
        if value not in (None, ""):
            merged[str(key)] = value
    if "gate" in merged and "from_gate" not in merged:
        merged["from_gate"] = merged.pop("gate")
    else:
        merged.pop("gate", None)
    raw_id = item.get("id")
    if raw_id not in (None, ""):
        if source in ("gate2_reject", "gate1_reject"):
            merged.setdefault("proposal_id", raw_id)
        elif source == "bug_close":
            merged.setdefault("bug_id", raw_id)
    trace.update(merged)
    # 本函数的归一结果最后盖——调用方塞的 source_ref 不能冒充写入器来源。
    trace["source"] = source
    trace["written_by"] = source
    return trace


def make_sink_entries(
    source: str,
    items: Sequence[Mapping[str, Any]],
    *,
    module: Optional[str] = None,
    strict: bool = True,
) -> list[dict[str, Any]]:
    """把 §环节11 的四类写入者记录**批量**转成 ``mc_test_question`` 可入库条目。

    :param source: :data:`SINK_SOURCES` 之一——``gate2_reject``（门② REJECT 原因）/
        ``gate1_reject``（门① 打回原因）/ ``bug_close``（bug 结案）/
        ``rework_lesson``（返工教训），外加 ``pitfall`` / ``risk_scenario`` 两个
        直写入口。大小写与首尾空白不敏感。
    :param items: 原始记录（调用方查库后传进来，本函数**不查库**）。每条形状见下。
    :param module: 批量兜底模块——条目自己没带 ``module`` 时用这个（一整批打回
        常属同一模块，逐条写太累）。
    :param strict: 某条取不到正文时：``True``（缺省）整批抛 :class:`ValueError`
        并指出第几条，``False`` 跳过该条。**没有静默丢数据的中间态**——要么全有
        要么报错，宽松模式是显式选的。
    :returns: ``list[dict]``，每项都是 :func:`make_sink_entry` 的行形状
        （``origin``=kind、``content``=正文、``status="resolved"``、
        ``blocking_refs=[]``、``context={kind,gate:"sink",sink:True,module,
        source_ref}``）。写库由调用方做：
        ``entry.pop("gate"/"module"/"kind")`` 后 ``TestQuestion.create(
        project_id=..., **entry)``（那三个顶层键没有真列，走 ORM 会被丢弃）。
    :raises ValueError: ``source`` 越界、某条不是 Mapping、或（strict 下）
        某条取不到正文。

    **kind 可被条目覆盖**：条目里的 ``sink_kind``（顶层或 ``provenance`` 袋子里）
    经 :func:`canonicalize_sink_kind` 归一后优先于来源默认值（一批 bug 结案里
    顺手记条 ``pitfall`` 是合法诉求）。归一失败直接抛，不猜。
    **顶层 ``kind`` 不当作覆盖键**——提案/用例行自己有 ``kind`` 列（那是
    ``case/proposal`` 的分类，不是回流种类），撞名不认。

    **正文字段别名**（先 kind 专属、再通用尾巴 ``text`` / ``content`` / ``detail`` /
    ``note`` / ``message``）：

    * ``reject_reason`` → ``reason`` / ``reject_reason`` / ``sendback_reason`` / ``comment``
    * ``bug_rootcase``  → ``root_cause`` / ``rootcause`` / ``root_case`` / ``conclusion``
    * ``rework_lesson`` → ``lesson`` / ``rework_lesson`` / ``takeaway`` / ``reason``
    * ``pitfall``       → ``pitfall`` / ``trap`` / ``issue``
    * ``risk_scenario`` → ``scenario`` / ``risk_scenario`` / ``risk``

    条目形状示例（门② 打回）::

        {"reason": "断言太弱，只有 status=200", "proposal_id": UUID(...),
         "run_id": ..., "module": "order", "decided_by": ..., "gate": "gate2"}

    → ``origin="reject_reason"``，``context.source_ref`` = ``{source:"gate2_reject",
    written_by:"gate2_reject", proposal_id:"...", run_id:"...", from_gate:"gate2",
    decided_by:"..."}``（模块走 ``context.module``）。bug 结案：``{"bug_id":...,
    "root_cause":"库存缓存未失效", "module":"order"}`` → ``origin="bug_rootcase"``；
    返工教训：``{"lesson":"并发场景漏测", "round_no":2, "case_id":...}`` →
    ``origin="rework_lesson"``。
    """
    clean_source = str(source or "").strip().lower()
    if clean_source not in SINK_SOURCE_KINDS:
        raise ValueError(
            f"sink source={source!r} 不在允许集合 {list(SINK_SOURCES)} 内"
            f"（§环节11 写入者：门② REJECT / 门① 打回 / bug 结案 / 返工教训）"
        )
    default_kind = SINK_SOURCE_KINDS[clean_source]

    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(items or ()):
        if item is None:
            if strict:
                raise ValueError(f"items[{idx}] 是 None——回流条目至少得是个 Mapping")
            continue
        if not isinstance(item, Mapping):
            raise ValueError(
                f"items[{idx}] 类型={type(item).__name__}，回流条目必须是 Mapping"
            )

        view = _sink_view(item)
        raw_kind = view.get("sink_kind")
        kind = default_kind if raw_kind in (None, "") else canonicalize_sink_kind(raw_kind)
        # 条目覆盖了 kind 时，来源默认 kind 的正文别名仍算数（正文常留在原列）。
        fallback = () if kind == default_kind else (default_kind,)

        text = _sink_text(kind, view, fallback)
        if not text:
            if strict:
                raise ValueError(
                    f"items[{idx}]（source={clean_source}, kind={kind}）取不到沉淀正文："
                    f"别名 {_sink_text_keys(kind, fallback)} 里全为空"
                    "——拒绝理由到底写在哪一列？"
                )
            continue

        entries.append(
            make_sink_entry(
                kind,
                text,
                module=_sink_module(view, module),
                source_ref=_sink_trace(clean_source, item, view),
            )
        )
    return entries
