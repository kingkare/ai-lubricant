"""测试点提点规程（25 号文环节 3：测试点提取·含分派）。

**规程 + 纯函数**——不 import tortoise / LLM / ``generation`` 内其他模块，
只 import ``testing_platform.quality.errors`` 与标准库。DB 落库（写
``mc_test_proposal(kind=testpoint)``）与子 agent 调用在调度器侧，本文件不碰。

职责边界（对照 25 号文环节 3 四方分工表）：

========== ===================== ==========================================================
角色        谁                     本文件落位
========== ===================== ==========================================================
风险定调    指挥中心（思考）        输入侧——``direction_weight`` 权重表在此渲染进提示词
提点        子 agent 群（每方向一） :func:`build_point_prompt`（提点子 agent 提示词）
                                 + :data:`POINT_SOURCES`（提点六规程）
                                 + :func:`from_criteria` / :func:`from_invariants` /
                                   :func:`from_register`（机械直转，代码先行那半）
轴端标注    代码先行+AI 补语义     :func:`auto_axes`（"接口方向必出 C12/C03/C04"）；
                                 AI 判端（basis=assumption）留在提示词里
完整性复核  另一供应商轻模型        :func:`self_check` 的两条机器自检（恒真式直测/风险有点）
分派        指挥中心               :func:`assign_batches`（A 冒烟/B 重点/C 扩展）
========== ===================== ==========================================================

测试点字段 = 25 号文 2.2 ``mc_test_testpoint``：``title / trigger / sample_data /
verify_result / impact / evidence``（五字段+依据）、``coverage_axis``（数组）、
``surfaces``（数组）、``design_method``、``batch_tier``、``basis``
（``fact`` / ``assumption`` / ``to_confirm`` 三分）。
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping, Sequence

from ..quality.errors import ValidationResult

__all__ = [
    "POINT_SOURCES",
    "build_point_prompt",
    "from_criteria",
    "from_invariants",
    "from_register",
    "auto_axes",
    "self_check",
    "assign_batches",
]

# ── 枚举常量（25 号文 2.2 / 环节 3）──────────────────────────────

#: ``basis`` 依据类型三分（2.2：事实 fact / 推断 assumption / 待确认 to_confirm）
BASIS_FACT = "fact"
BASIS_ASSUMPTION = "assumption"
BASIS_TO_CONFIRM = "to_confirm"
BASIS_KINDS: tuple[str, ...] = (BASIS_FACT, BASIS_ASSUMPTION, BASIS_TO_CONFIRM)

#: ``batch_tier``（2.2：A 冒烟 / B 重点 / C 扩展）
BATCH_A = "A"  # 冒烟
BATCH_B = "B"  # 重点
BATCH_C = "C"  # 扩展
BATCH_TIERS: tuple[str, ...] = (BATCH_A, BATCH_B, BATCH_C)

#: 冒烟批默认占比（25 号文环节 3 分派：A=冒烟批）
DEFAULT_SMOKE_RATIO = 0.2

#: ``design_method`` 合法集（2.2：等价类/边界值/判定表/状态迁移/场景法/错误推测/正交）
DESIGN_METHODS: tuple[str, ...] = (
    "等价类", "边界值", "判定表", "状态迁移", "场景法", "错误推测", "正交",
)

#: 覆盖轴 C01-C14（18 号文 §3.4 原文；与 ``design/transform_table.COVERAGE_AXES`` 同源，
#: 这里**不 import** design 包——本模块只依赖 quality.errors + 标准库的硬约束）
AXIS_NORMAL = "C01"
AXIS_EXCEPTION = "C02"
AXIS_BOUNDARY = "C03"
AXIS_PERMISSION = "C04"
AXIS_CONSISTENCY = "C09"
AXIS_INPUT_VALIDATION = "C12"

#: 接口相关方向必出的机械轴（25 号文环节 3「轴端标注：接口方向必出 C12/C03/C04」；
#: 顺序即文档口径）
API_MANDATORY_AXES: tuple[str, ...] = (AXIS_INPUT_VALIDATION, AXIS_BOUNDARY, AXIS_PERMISSION)

#: 风险打分缺省值——impact×probability 各 5 档里的中间档（3×3）。
#: 「或默认」的默认（25 号文环节 3 分派：按风险分排 A/B/C；没带分数的点取中性档，
#: 不垫底也不置顶）。
DEFAULT_RISK_IMPACT = 3
DEFAULT_RISK_PROBABILITY = 3

# ── 提点六规程（25 号文环节 3 分工表「提点」行）───────────────────
# id 稳定（提示词/回执/进度渲染引用）；desc 取文档原文口径。
POINT_SOURCES: tuple[dict[str, str], ...] = (
    {
        "id": "criteria_direct",
        "name": "验收标准直转",
        "desc": "criteria 直转（每条可测验收标准至少 1 个正常点 + 1 个异常点）",
    },
    {
        "id": "invariants_direct",
        "name": "恒真式直测",
        "desc": "invariants 直转（每条恒真式必得一个 C09 数据一致性直测点）",
    },
    {
        "id": "risk_generator_expand",
        "name": "高风险方向生成器展开",
        "desc": "高风险方向按生成器展开（边界组合/状态图路径/判定表行）",
    },
    {
        "id": "register_placeholder",
        "name": "登记表挂起占位",
        "desc": "register 待确认项 → 挂起占位点（basis=to_confirm，答案回来自动激活）",
    },
    {
        "id": "five_fields_annotated",
        "name": "五字段+轴+方法+批次",
        "desc": "每点五字段（title/trigger/sample_data/verify_result/impact）"
                "+ coverage_axis + surfaces + design_method + batch_tier 标注齐",
    },
    {
        "id": "self_coverage",
        "name": "自检全覆盖",
        "desc": "自检全覆盖（恒真式每条直测、风险方向每条有点，缺的补齐再交）",
    },
)

#: 提点提示词里描述「机械先铺轴」的说明段（**只给调度器看**：这是说明不是提点指令，
#: 不进 agent 提示词——25 号文环节 3 分派由调度器执行这一半）
MECHANICAL_AXES_NOTE = (
    '机械先铺（调度器说明，不进 agent 提示词）：接口相关方向必出 C12/C03/C04 轴。'
)

#: 提点版三条纪律正文（25 号文环节 5 三条纪律的提点版；英文随提示词渲染）
POINT_DISCIPLINES: tuple[dict[str, str], ...] = (
    {
        "id": "nominate_only",
        "text": "Discovering a new test point beyond your assigned direction: "
                "NOMINATE ONLY. Record it as a candidate with a short title and the "
                "reason; never open a new batch yourself — the command center decides "
                "when to dispatch more point agents.",
    },
    {
        "id": "persist_before_consult",
        "text": "Persist before consulting: write your points to the proposal store "
                "BEFORE asking a stronger reviewer anything, so a session interruption "
                "loses nothing. Budget at most 3 consultations per task; exceeding it "
                "is not retried.",
    },
    {
        "id": "reconcile_conflicts",
        "text": "On conflict, reconcile — never silently swap. If evidence contradicts "
                "what the analysis says, raise the conflict with both sides and a "
                "suggested adjudicator; do not quietly rewrite either side.",
    },
)

# ── 内部工具 ────────────────────────────────────────────────────


def _as_str(value: Any) -> str:
    """条目转可读文本：标量直转；dict 优先取 text/condition/content 等常用键。"""
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("text", "condition", "content", "claim", "statement", "name", "title"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _make_point(
    *,
    title: str,
    trigger: str,
    sample_data: Any,
    verify_result: str,
    impact: str,
    evidence: Sequence[str],
    coverage_axis: Sequence[str],
    surfaces: Sequence[str] = (),
    design_method: str = "等价类",
    batch_tier: str = BATCH_C,
    basis: str = BASIS_FACT,
    source: str = "",
    direction: str = "",
) -> dict[str, Any]:
    """按 2.2 ``mc_test_testpoint`` 形状造一个测试点 dict（proposal.payload 同形）。"""
    point: dict[str, Any] = {
        "title": title,
        "trigger": trigger,
        "sample_data": sample_data,
        "verify_result": verify_result,
        "impact": impact,
        "evidence": list(evidence),
        "coverage_axis": list(dict.fromkeys(coverage_axis)),  # 去重保序
        "surfaces": list(surfaces),
        "design_method": design_method,
        "batch_tier": batch_tier,
        "basis": basis,
    }
    if source:
        point["source"] = source
    if direction:
        point["direction"] = direction
    return point


def _ac_id(item: Mapping[str, Any], index: int) -> str:
    """验收标准条目的 id：无 id 回退 ``AC-<n>``（提示词契约里说死这个回退口径）。"""
    if item.get("id") is not None:
        return str(item["id"])
    return f"AC-{index + 1}"


def _is_testable(item: Mapping[str, Any]) -> bool:
    """可测性判定——analysis 环节九步「可测性检查」的回执字段，缺省视为可测。"""
    return item.get("testable", True) is not False


def _point_covers_axis(point: Mapping[str, Any], axis: str) -> bool:
    axes = point.get("coverage_axis") or ()
    if isinstance(axes, str):
        axes = (axes,)
    return axis in axes


def _evidence_mentions(point: Mapping[str, Any], ref: str) -> bool:
    """点的 evidence 是否引用了某条 analysis id。"""
    evidence = point.get("evidence") or ()
    if isinstance(evidence, str):
        evidence = (evidence,)
    return any(str(e) == ref for e in evidence)


def _point_matches_ref(point: Mapping[str, Any], ref: str) -> bool:
    """恒真式/风险方向的直测判定：evidence 引 id 或 direction 字段指名。"""
    return _evidence_mentions(point, ref) or str(point.get("direction") or "") == ref


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _risk_score(point: Mapping[str, Any]) -> float:
    """风险分：payload 里 impact×probability 或默认中性档（3×3）。

    取值口径（按优先级）：
    1. ``risk_score``——已是算好的总分（若带 impact/probability 仍以乘积为准）；
    2. ``risk.impact``/``risk.probability``（嵌套 risk 对象）；
    3. 顶层 ``impact``/``probability``——两者皆数值时才用
       （``impact`` 五字段里是说明文字，非数值时回退默认，不当 0 处理）；
    4. 默认 ``DEFAULT_RISK_IMPACT × DEFAULT_RISK_PROBABILITY``。
    """
    nested = point.get("risk")
    impact = _as_number(nested.get("impact") if isinstance(nested, Mapping) else None)
    probability = _as_number(
        nested.get("probability") if isinstance(nested, Mapping) else None
    )
    if impact is None:
        impact = _as_number(point.get("impact"))
    if probability is None:
        probability = _as_number(point.get("probability"))
    if impact is not None and probability is not None:
        return impact * probability
    if point.get("risk_score") is not None:
        score = _as_number(point["risk_score"])
        if score is not None:
            return score
    return DEFAULT_RISK_IMPACT * DEFAULT_RISK_PROBABILITY


# ── 机械直转（代码先行那半）──────────────────────────────────────


def from_criteria(criteria: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """规程一：每条可测验收标准 → ≥1 正常点 + ≥1 异常点。

    标题模板（文档口径）：``验证<condition>的正常路径`` / ``验证<condition>失败被拒``。
    ``basis=fact``，``evidence`` 引 AC id；``coverage_axis`` 默认 ``[C01]`` / ``[C02]``。
    ``testable=false`` 的条目跳过（analysis 九步「可测性检查」：转不出条件→预期的不可测）。
    每条可产一个点对（≥1 的下限即 1 对），重复调用结果稳定。
    """
    points: list[dict[str, Any]] = []
    for idx, item in enumerate(criteria or ()):
        if not isinstance(item, Mapping):
            continue
        if not _is_testable(item):
            continue
        condition = _as_str(item.get("condition") or item.get("text"))
        if not condition:
            continue
        expected = _as_str(item.get("expected"))
        ac_id = _ac_id(item, idx)
        evidence = [ac_id]
        points.append(
            _make_point(
                title=f"验证{condition}的正常路径",
                trigger=f"满足验收标准 {ac_id}：{condition}",
                sample_data={"acceptance": expected or "按验收标准预期输出"},
                verify_result=expected or "行为与验收标准预期一致",
                impact="验收标准未按预期达成",
                evidence=evidence,
                coverage_axis=[AXIS_NORMAL],
                design_method="等价类",
                batch_tier=BATCH_B,
                basis=BASIS_FACT,
                source="criteria_direct",
            )
        )
        points.append(
            _make_point(
                title=f"验证{condition}失败被拒",
                trigger=f"违反验收标准 {ac_id}：{condition}",
                sample_data={"acceptance": "违反上述条件的输入/状态"},
                verify_result="被明确拒绝且错误信息可读，不落脏数据",
                impact="违反条件未被拦截，脏数据或错误结果流出",
                evidence=evidence,
                coverage_axis=[AXIS_EXCEPTION],
                design_method="错误推测",
                batch_tier=BATCH_B,
                basis=BASIS_FACT,
                source="criteria_direct",
            )
        )
    return points


def from_invariants(invariants: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """规程二：每条恒真式 → 1 个 C09 数据一致性直测点（``basis=fact``）。

    恒真式「每条必得直接测试」（24 号文 Skill2 ★不变式；25 号文环节 3 规程二），
    直测=断言该不变式在操作前后/跨视图上仍然成立，不是抽样推断。
    """
    points: list[dict[str, Any]] = []
    for idx, item in enumerate(invariants or ()):
        if not isinstance(item, Mapping):
            continue
        statement = _as_str(
            item.get("statement") or item.get("text") or item.get("invariant")
        )
        if not statement:
            continue
        inv_id = str(item.get("id") if item.get("id") is not None else f"INV-{idx + 1}")
        points.append(
            _make_point(
                title=f"验证恒真式：{statement}",
                trigger=f"{inv_id} 相关数据被任一操作改动后",
                sample_data={"invariant": statement, "id": inv_id},
                verify_result=f"恒真式 {inv_id} 直接断言成立（{statement}），跨视图/跨表一致",
                impact="恒真式失守——数据一致性被破坏",
                evidence=[inv_id],
                coverage_axis=[AXIS_CONSISTENCY],
                design_method="场景法",
                batch_tier=BATCH_B,
                basis=BASIS_FACT,
                source="invariants_direct",
            )
        )
    return points


def from_register(register: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """规程四：register 中 ``kind=to_confirm`` 且带 ``question_id`` 的条目 → 挂起占位点。

    占位点 ``basis=to_confirm``（门①红徽章），``evidence`` 引 ``question_id``；
    问题答完后由门①激活（to_confirm→fact），激活不在本函数职责内。
    只引 id 不抄 question 正文——问题库才是正文真相源（2.4）。
    ``kind`` 缺省或非 ``to_confirm``、缺 ``question_id`` 的条目跳过（不产假挂起）。
    """
    points: list[dict[str, Any]] = []
    for item in register or ():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "").strip().lower() != BASIS_TO_CONFIRM:
            continue
        question_id = item.get("question_id")
        if question_id is None or str(question_id).strip() == "":
            continue
        claim = _as_str(item.get("claim") or item.get("content") or item.get("text"))
        points.append(
            _make_point(
                title=f"[待确认 {question_id}] {claim or '登记表待确认项'}",
                trigger=f"问题 {question_id} 被回答后激活",
                sample_data={"question_id": str(question_id)},
                verify_result="按问题答案定义预期（答案未到，先挂起）",
                impact="待确认项未验证即放行，存在假设性缺口",
                evidence=[str(question_id)],
                coverage_axis=[],
                design_method="场景法",
                batch_tier=BATCH_C,
                basis=BASIS_TO_CONFIRM,
                source="register_placeholder",
            )
        )
    return points


def auto_axes(direction: str, has_api: bool) -> list[str]:
    """轴端标注·机械先铺：接口相关方向必出 ``C12/C03/C04``。

    - ``has_api=True``，或 ``direction`` 文本本身点到接口（api/接口/HTTP/REST/endpoint…），
      → 返回 ``["C12", "C03", "C04"]``（顺序即文档口径）；
    - 纯非接口方向 → ``[]``（其余轴由提点 agent 按语义判定，basis=assumption）。

    这是「代码先行+AI 补语义」分工里代码那一半（25 号文环节 3 轴端标注行）。
    """
    if has_api:
        return list(API_MANDATORY_AXES)
    text = (direction or "").lower()
    if any(k in text for k in ("api", "接口", "http", "rest", "endpoint", "接口相关")):
        return list(API_MANDATORY_AXES)
    return []


def self_check(
    points: Sequence[Mapping[str, Any]] | None,
    invariants: Sequence[Mapping[str, Any]] | None,
    risk_directions: Sequence[Mapping[str, Any]] | None,
) -> ValidationResult:
    """规程六·机器半（完整性复核的确定性两条）：恒真式全覆盖 / 风险方向有点。

    - 恒真式：每条恒真式必须有直测点（evidence 引其 id，或 coverage_axis 含 C09 且
      evidence 引了它）——缺=error（24 号文 Skill3 自检 4 条之二）；
    - 风险方向：每个风险方向至少一个点归属它（``direction`` 指名或 evidence 引其 id）
      ——缺=error（24 号文 Skill3 自检 4 条之三「每风险有场景」的提点层）。
    空 points 对非空 invariants/risk_directions 一律报错。
    """
    result = ValidationResult()
    pts = [p for p in (points or ()) if isinstance(p, Mapping)]

    for idx, item in enumerate(invariants or ()):
        if not isinstance(item, Mapping):
            continue
        statement = _as_str(
            item.get("statement") or item.get("text") or item.get("invariant")
        )
        if not statement:
            continue
        ref = str(item.get("id") if item.get("id") is not None else f"INV-{idx + 1}")
        hit = any(
            _point_matches_ref(p, ref) and _point_covers_axis(p, AXIS_CONSISTENCY)
            for p in pts
        )
        if not hit:
            result.add(
                f"$.invariants[{idx}]",
                "R_point_invariant_uncovered",
                f"恒真式 {ref}「{statement}」没有 C09 数据一致性直测点",
                hint="提点规程二：每条恒真式必得一个 C09 直测点（basis=fact）",
            )

    for idx, item in enumerate(risk_directions or ()):
        if not isinstance(item, Mapping):
            continue
        name = _as_str(item.get("name") or item.get("direction") or item.get("title"))
        if not name:
            continue
        ref = str(item.get("id") if item.get("id") is not None else name)
        hit = any(_point_matches_ref(p, ref) for p in pts)
        if not hit:
            result.add(
                f"$.risk_directions[{idx}]",
                "R_point_direction_uncovered",
                f"风险方向「{name}」没有归属测试点",
                hint="提点规程三/六：高风险方向按生成器展开产点，自检全覆盖",
            )

    return result


def assign_batches(
    points: Sequence[Mapping[str, Any]] | None,
    smoke_ratio: float = DEFAULT_SMOKE_RATIO,
) -> list[dict[str, Any]]:
    """分派：按风险分降序排 A 冒烟（默认 20%）/B 重点/C 扩展——纯排序分配。

    - 风险分取 :func:`_risk_score`（payload impact×probability 或默认中性档 3×3）；
    - 输入**不被修改**（返回新 dict 列表，只多一个 ``batch_tier``）；
    - 输出按风险分**降序**、批内保持输入原序（稳定排序）；
    - 比例口径：``n_A = max(1, round(n × smoke_ratio))``（有点则冒烟批至少 1）；
      剩余均分 B/C，B 吃余数（B = ceil(剩余/2)）；
    - 空输入 → 空输出；``smoke_ratio`` 夹在 [0, 1]。
    """
    src = [p for p in (points or ()) if isinstance(p, Mapping)]
    n = len(src)
    if n == 0:
        return []

    ratio = min(max(float(smoke_ratio), 0.0), 1.0)
    ranked = sorted(
        src,
        key=lambda p: _risk_score(p),  # sorted 稳定：同分保输入原序
        reverse=True,
    )

    n_a = min(n, max(1, int(round(n * ratio))))
    rest = n - n_a
    n_b = int(math.ceil(rest / 2))
    assigned: list[dict[str, Any]] = []
    for i, point in enumerate(ranked):
        if i < n_a:
            tier = BATCH_A
        elif i < n_a + n_b:
            tier = BATCH_B
        else:
            tier = BATCH_C
        row = dict(point)
        row["batch_tier"] = tier
        assigned.append(row)
    return assigned


# ── 提点子 agent 提示词 ─────────────────────────────────────────


def _render_procedures() -> str:
    """提点六规程（有序编号，英文正文）。"""
    lines = []
    for i, proc in enumerate(POINT_SOURCES, 1):
        lines.append(f"{i}. {proc['name']} ({proc['id']}): {proc['desc']}")
    return "\n".join(lines)


def _render_disciplines() -> str:
    return "\n".join(
        f"- {d['id']}: {d['text']}" for d in POINT_DISCIPLINES
    )


def _render_json_example() -> str:
    example = [
        {
            "title": "验证金额字段小于等于 0 被拒",
            "trigger": "提交支付金额 amount=0 或 amount=-1",
            "sample_data": {"amount": "0"},
            "verify_result": "接口返回 400，业务码标识金额非法，订单未创建",
            "impact": "非正金额被放行会导致资损",
            "evidence": ["AC-12", "INV-3"],
            "coverage_axis": ["C12", "C03"],
            "surfaces": ["api"],
            "design_method": "边界值",
            "batch_tier": "B",
            "basis": "fact",
        }
    ]
    return json.dumps(example, ensure_ascii=False, indent=2)


def build_point_prompt(
    analysis_slice: Mapping[str, Any],
    direction_weight: Mapping[str, Any],
) -> str:
    """提点子 agent 提示词（英文正文）。

    :param analysis_slice: 本方向的 analysis 切片——``register`` /
        ``invariants`` / ``criteria`` 三桶 + 可选 ``requirement``；
        派发**传路径不传正文**（dispatch-by-reference）时由调度器替换成引用。
    :param direction_weight: 风险定调产出的权重表——``{direction_name: {impact,
        probability, weight, note?}}``；指挥中心「影响×概率」打分的结果在此。

    含：①提点六规程顺序（:data:`POINT_SOURCES`）②输出 JSON 契约（每点 12 键）
    ③三条纪律（:data:`POINT_DISCIPLINES`）。**不含**调度器说明段——
    "接口相关方向必出 C12/C03/C04 轴"是给调度器的机械铺轴说明（:data:`MECHANICAL_AXES_NOTE`），
    按文档口径不进 agent 提示词；agent 自判端与自判轴走语义（basis=assumption）。
    """
    slice_dict = dict(analysis_slice or {})
    weight_dict = dict(direction_weight or {})

    criteria = slice_dict.get("criteria") or []
    invariants = slice_dict.get("invariants") or []
    register = slice_dict.get("register") or []
    requirement = slice_dict.get("requirement") or {}

    contract_keys = [
        "title", "trigger", "sample_data", "verify_result", "impact", "evidence",
        "coverage_axis", "surfaces", "design_method", "batch_tier", "basis",
    ]

    sections = [
        (
            "You are a test-point extraction agent in the QA test-platform generation "
            "pipeline. You extract TEST POINTS — the unit for case writing, human "
            "confirmation and coverage accounting — from one analysis slice of one "
            "requirement, for ONE assigned risk direction. You write nothing else: no "
            "test cases, no scenarios. Your only output is the JSON contract below."
        ),
        (
            "## Your assignment\n"
            f"- requirement: {json.dumps(requirement, ensure_ascii=False) if requirement else '(see slice)'}\n"
            f"- risk direction weights (impact x probability from the command center):\n"
            f"{json.dumps(weight_dict, ensure_ascii=False, indent=2)}\n"
            "- Work the direction with the highest weight for YOUR slice; mention the "
            "other directions only if a point belongs there (then tag it, do not "
            "expand it)."
        ),
        (
            "## Input — analysis slice (three buckets, already reviewed at gate 0)\n"
            f"- criteria (acceptance criteria):\n"
            f"{json.dumps(criteria, ensure_ascii=False, indent=2)}\n"
            f"- invariants (invariants that must always hold):\n"
            f"{json.dumps(invariants, ensure_ascii=False, indent=2)}\n"
            f"- register (conflict/gap registry):\n"
            f"{json.dumps(register, ensure_ascii=False, indent=2)}"
        ),
        (
            "## Extraction procedure — the six rules, IN THIS ORDER\n"
            f"{_render_procedures()}\n"
            "Rules 1/2/4 are mechanical mirrors of the slice; rule 3 is your thinking "
            "work — for high-weight directions, expand with the generators "
            "(boundary combinations / state-graph paths / decision-table rows). "
            "Rule 5 annotates every point you emit, yourself included. Rule 6 is a "
            "self-check you run BEFORE returning: every invariant must have a direct "
            "C09 point, every risk direction must have at least one point — if yours "
            "is missing one that belongs to your direction, add it now."
        ),
        (
            "## Basis is a three-way classification — never blur it\n"
            f"- ``fact``: supported by a cited analysis entry id.\n"
            f"- ``assumption``: you inferred it (surface judgement, expected value, "
            f"edge guess) — no analysis entry says it.\n"
            f"- ``to_confirm``: unresolvable from the slice; must map to a question "
            f"the human can answer.\n"
            f"Valid basis values: {BASIS_KINDS[0]} | {BASIS_KINDS[1]} | "
            f"{BASIS_KINDS[2]}. No fourth value, no empty."
        ),
        (
            "## Output contract — JSON array, one object per test point\n"
            f"Each point MUST carry exactly these {len(contract_keys)} keys:\n"
            f"{json.dumps(contract_keys, ensure_ascii=False)}\n"
            f"- title/trigger/sample_data/verify_result/impact — the five fields\n"
            f"- evidence — array of analysis entry ids (criteria id, invariant id, "
            f"register/question id). Cite ids, never quote raw text. Empty only when "
            f"basis is not fact.\n"
            f"- coverage_axis — array of axis codes from C01..C14 (normal flow C01, "
            f"exception C02, boundary C03, permission C04, consistency C09, input "
            f"validation C12, ...). One point may span several axes.\n"
            f"- surfaces — array subset of [api, web, app, security]; empty if you "
            f"cannot decide.\n"
            f"- design_method — one of {list(DESIGN_METHODS)}\n"
            f"- batch_tier — one of {list(BATCH_TIERS)} (A=smoke, B=focus, C=extended). "
            f"Propose; the dispatcher re-ranks by risk.\n"
            f"- basis — fact | assumption | to_confirm\n"
            f"Example point:\n{_render_json_example()}\n"
            f"Return ONLY the JSON array — no markdown fences, no prose."
        ),
        (
            "## Disciplines — violations are rejected by the machine gate\n"
            f"{_render_disciplines()}"
        ),
        (
            "## Hard limits\n"
            "- Do NOT invent requirements, parameters, endpoints or selectors not "
            "implied by the slice — hallucinated references fail the gate.\n"
            "- Do NOT write cases or scenarios; extraction stops at test points.\n"
            "- Do NOT self-open batches or recurse into new directions (see "
            "nominate_only discipline)."
        ),
    ]
    return "\n\n".join(sections)
