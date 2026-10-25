"""用例生成规程（25 号文 v4.1 §环节5 用例细化；P1）。

**规程 + 纯函数**——不 import tortoise / LLM / ``generation`` 内其他模块，
唯一跨模块依赖是 :func:`.gates.make_slot_key`（幂等键口径与机器门同源，
任务书指定从这里 import）。DB 落库与 LLM 调用在编排器侧
（:func:`testing_platform.generation.orchestrator.run_case_generation`），
本文件不碰 ORM、不接网络，可直接单测。

职责边界（对照 25 号文 §环节5 四方分工表）：

========== ===================== ==========================================================
角色        谁                     本文件落位
========== ===================== ==========================================================
扇出        指挥中心               :func:`fanout_tasks`（confirmed 测试点 × surfaces → 任务）
用例细化    子 agent 群（一点一端一） :func:`build_case_prompt`（单测试点 + 单端提示词）
回执归一    机器门前的代码层       :func:`case_to_payload`（LLM 输出 → proposal.payload）
========== ===================== ==========================================================

铁律延续（§1.3）：本模块**只产 dict 不写库**——AI 产物一律经
:func:`case_to_payload` 归一后由编排器写 ``mc_test_proposal(kind=case)``，
门②确认后才进 ``mc_test_case``。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Iterable, Mapping, Sequence

from .gates import make_slot_key

__all__ = [
    "CASE_STRATEGIES",
    "SURFACE_ASSERTIONS",
    "DATA_SCOPES",
    "CASE_DISCIPLINES",
    "EVIDENCE_CLASS_MAP",
    "DEFAULT_EVIDENCE_CLASS",
    "fanout_tasks",
    "build_case_prompt",
    "case_to_payload",
    "map_evidence_class",
]

# ── 枚举常量（25 号文 §环节5）─────────────────────────────────────

#: 六种用例设计策略（§环节5 步骤1 原文六策略；英文 slug，与测试点
#: ``design_method`` 的蛇形命名同风格——提示词按此清单逐个要求）。
CASE_STRATEGIES: tuple[str, ...] = (
    "positive",            # 正向：验收标准直转的正常路径
    "negative",            # 负向：违反前置/约束被明确拒绝
    "boundary",            # 边界：边界值及其内外侧
    "equivalence-class",   # 等价类：有效/无效等价类各取代表
    "scenario",            # 场景：端到端业务流（跨步骤状态流转）
    "state-transition",    # 状态迁移：状态图路径/非法迁移
)

#: 每端断言体系（§环节5「断言体系 per 端」原文字段名）。
#: 键 = surface（api|web|app|security），值 = 该端必须设计的断言字段（有序）。
SURFACE_ASSERTIONS: dict[str, tuple[str, ...]] = {
    "api": ("status_code", "biz_code", "key_fields", "response_time"),
    "web": ("element_visible", "element_text", "url"),
    "app": ("foreground_state", "persisted_state", "crash_log"),
    "security": ("reject_code", "audit_trail", "data_invisible"),
}

#: 数据五作用域（25 号文 §2.7；与 :data:`.gates.FIVE_SCOPES` 同口径，
#: 此处独立定义保持本模块只依赖 gates 的一个符号）。
DATA_SCOPES: tuple[str, ...] = ("env", "auth", "local", "data", "global")

#: 配比下限（§环节5 步骤1「配比」原文：≥1 positive + ≥3 negative/boundary）。
MIN_POSITIVE = 1
MIN_NEGATIVE_BOUNDARY = 3

#: ``mc_test_proposal.evidence_class``（§2.3；机器门只认 executed/static 可判通过）。
PROPOSAL_EVIDENCE_CLASSES: tuple[str, ...] = ("executed", "static", "inferred", "claimed")

#: 测试点 ``basis``（§2.2 三分）→ 提案 ``evidence_class``（§2.3 四分）的映射。
#: 口径：fact=有据可查→static；inferred=推断→inferred；to_confirm/未知/缺省→claimed
#: （未验证的主张，机器门不得据此放行）。``assumption`` 归 claimed（同理论据不足）。
EVIDENCE_CLASS_MAP: dict[str, str] = {
    "fact": "static",
    "inferred": "inferred",
    "assumption": "claimed",
    "to_confirm": "claimed",
    "static": "static",
    "claimed": "claimed",
    "executed": "executed",
}

#: 映射缺省值（任务书口径：to_confirm / 默认 → claimed）。
DEFAULT_EVIDENCE_CLASS = "claimed"

#: 用例版三条纪律（§环节5 三条纪律的用例版；英文随提示词渲染）。
CASE_DISCIPLINES: tuple[dict[str, str], ...] = (
    {
        "id": "nominate_only",
        "text": "Discovering a gap beyond your assigned test point and surface: "
                "NOMINATE ONLY. Record it as a candidate line with a short title and "
                "the reason; never open a new task yourself — the command center "
                "decides what else gets generated.",
    },
    {
        "id": "persist_before_consult",
        "text": "Persist before consult: your cases are written to the proposal store "
                "BEFORE you ask a stronger reviewer anything, so a session "
                "interruption loses nothing. Budget at most 3 consultations per task; "
                "exceeding it is not retried.",
    },
    {
        "id": "reconcile_not_silent",
        "text": "On conflict, reconcile — never silently swap. If evidence contradicts "
                "what the test point says, raise the conflict with both sides and a "
                "suggested adjudicator; do not quietly rewrite either side.",
    },
)

#: LLM 输出契约的键（有序；提示词逐键说明）。
_CONTRACT_KEYS: tuple[str, ...] = (
    "title", "surface", "steps", "expected", "preconditions",
    "basis", "evidence", "scenario_tag", "applied_rules",
)

#: slot_key 变体片段最大长度（``mc_test_proposal.slot_key`` 是 VARCHAR(256)）。
_VARIANT_MAX_LEN = 64

_VARIANT_STRIP_RE = re.compile(r"[\s:]+")


def _json(value: Any, *, indent: int | None = None) -> str:
    """``json.dumps`` 的 UUID/datetime 安全版（测试点是 ORM ``values()`` 行，
    含 UUID 主键——普通 ``json.dumps`` 会炸）。"""
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


# ── 内部工具（纯函数）───────────────────────────────────────────


def _as_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _clean_surfaces(value: Any) -> list[str]:
    """surfaces 归一：去空白、去重、保序、丢空（只认四端之外的也保留，由机器门/人判）。"""
    out: list[str] = []
    for raw in _as_list(value):
        text = str(raw).strip()
        if text and text not in out:
            out.append(text)
    return out


def _variant_slug(value: Any) -> str:
    """slot_key 变体片段：去空白与冒号（避免与键分隔符撞），截断到定长。"""
    text = _VARIANT_STRIP_RE.sub("-", str(value or "").strip()).strip("-")
    return text[:_VARIANT_MAX_LEN] or "v1"


def map_evidence_class(raw: Any) -> str:
    """basis/evidence_class 原值 → ``mc_test_proposal.evidence_class``。

    ``fact`` → ``static``；``inferred`` → ``inferred``；
    ``to_confirm`` / ``assumption`` / 未知 / 缺省 → ``claimed``。
    已合法的事件类（``static``/``claimed``/``executed``）原样透传。
    """
    key = str(raw or "").strip().lower()
    if not key:
        return DEFAULT_EVIDENCE_CLASS
    mapped = EVIDENCE_CLASS_MAP.get(key)
    if mapped in PROPOSAL_EVIDENCE_CLASSES:
        return mapped
    return DEFAULT_EVIDENCE_CLASS


# ── 扇出（指挥中心那一半）──────────────────────────────────────


def fanout_tasks(testpoints: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """confirmed 测试点 × surfaces 展开成「一点一端一任务」。

    :param testpoints: confirmed 测试点 dict 列表（``mc_test_testpoint`` 行；
        ``id``/``code``/``surfaces`` 等键名与列名一致）。带 ``status`` 且非
        ``confirmed``/``generated`` 的行跳过（只细化已确认的点）。
    :returns: ``[{task_id, testpoint, surface}]``——``task_id`` 是 uuid 字符串
        （一次扇出一个稳定 id，供回执/日志引用），``testpoint`` 原 dict 引用，
        ``surface`` 是该点声明的一个端。

    幂等性由调用方保证（任务本身不带持久态）；纯展开，无 IO。surfaces 为空的
    点不产任务（无处扇出——端由门①确认时定，见 §2.2 ``surfaces_decided_by``）。
    """
    tasks: list[dict[str, Any]] = []
    for tp in testpoints or ():
        if not isinstance(tp, Mapping):
            continue
        status = str(tp.get("status") or "confirmed").strip().lower()
        if status not in ("confirmed", "generated"):
            continue
        for surface in _clean_surfaces(tp.get("surfaces")):
            tasks.append({
                "task_id": str(uuid.uuid4()),
                "testpoint": dict(tp),
                "surface": surface,
            })
    return tasks


# ── 提示词（子 agent 那一半）───────────────────────────────────


def _render_strategies() -> str:
    lines = []
    for i, name in enumerate(CASE_STRATEGIES, 1):
        lines.append(f"{i}. {name}")
    return "\n".join(lines)


def _render_assertions() -> str:
    lines = []
    for surface, fields in SURFACE_ASSERTIONS.items():
        lines.append(f"- {surface}: " + " + ".join(fields))
    return "\n".join(lines)


def _render_disciplines() -> str:
    return "\n".join(f"- {d['id']}: {d['text']}" for d in CASE_DISCIPLINES)


def _render_json_example(surface: str) -> str:
    fields = SURFACE_ASSERTIONS.get(surface, ())
    example = [{
        "title": "验证金额字段小于等于 0 被拒",
        "surface": surface,
        "steps": [
            {"seq": 1, "action": "以已登录态提交 amount=0 的创建订单请求",
             "expected": "请求被受理并返回明确拒绝"},
            {"seq": 2, "action": "查询订单列表",
             "expected": "无新订单落库"},
        ],
        "expected": {f: None for f in fields},
        "preconditions": ["存在可用账号 {{env.user}}", "订单服务可达"],
        "basis": "fact",
        "evidence": ["AC-12"],
        "scenario_tag": "boundary",
        "applied_rules": [{"rule_id": "R-1", "disposition": "followed", "why": ""}],
    }]
    return _json(example, indent=2)


def build_case_prompt(
    task: Mapping[str, Any],
    *,
    rules_block: str = "",
    history_slice: str = "",
) -> str:
    """单测试点 + 单端的用例细化提示词（英文正文）。

    :param task: :func:`fanout_tasks` 产出的一个任务（``{task_id, testpoint, surface}``）。
    :param rules_block: 该端/该需求生效的 active 规则正文（调用方查库后传入；
        空 = 本任务无 active 规则，提示词明说「无规则可违」）。
    :param history_slice: 已存在用例的摘要（避免重复造同一 slot；dispatch-by-reference
        时由调用方传路径/摘要，不传正文）。

    含：①**先写行为再单独设计断言**的口径②每端断言体系（:data:`SURFACE_ASSERTIONS`）
    ③六策略（:data:`CASE_STRATEGIES`）④配比 ≥1 positive + ≥3 negative/boundary
    ⑤数据五作用域标注（禁硬编码）⑥输出 JSON 数组契约（每例 9 键）
    ⑦三条纪律（:data:`CASE_DISCIPLINES`）。
    """
    tp = _as_dict(task.get("testpoint"))
    surface = str(task.get("surface") or "").strip()
    assertion_fields = SURFACE_ASSERTIONS.get(surface, ())

    sections = [
        (
            "You are a test-case refinement agent in the QA test-platform generation "
            "pipeline. You turn ONE confirmed test point into test cases for ONE "
            "surface. You write nothing else: no new test points, no scenarios, no "
            "executable scripts. Your only output is the JSON contract below."
        ),
        (
            "## Your assignment — one test point, one surface\n"
            f"- surface: {surface or '(unspecified)'}\n"
            f"- test point code: {tp.get('code') or '(none)'}\n"
            f"- test point:\n{_json(tp, indent=2)}\n"
            "Stay inside this test point and this surface. Anything you discover "
            "beyond them is a nomination, not a case (see nominate_only)."
        ),
        (
            "## Method — behaviour first, assertions designed separately\n"
            "1. Write the BEHAVIOUR first: what the system does, step by step, in "
            "natural language a human could follow.\n"
            "2. THEN, separately, design the ASSERTIONS for that behaviour using the "
            "assertion system of THIS surface. Do not fold the assertion into the "
            "step action; steps say what happens, the `expected` object says what is "
            "checked.\n"
            f"## Assertion system per surface (use the one for `{surface or 'your surface'}`)\n"
            f"{_render_assertions()}\n"
            f"For this task every field of {surface or 'your surface'} must appear as a "
            f"key in `expected`: {list(assertion_fields)}."
        ),
        (
            "## Design strategies — cover all six, in this order\n"
            f"{_render_strategies()}\n"
            "Walk the list top to bottom and emit cases for each strategy that "
            "applies to this test point. Skip a strategy only when it genuinely "
            "cannot apply — say so in the case's `why` if you skip one.\n"
            f"## Ratio (hard floor)\n"
            f"At least ≥{MIN_POSITIVE} positive case, and ≥{MIN_NEGATIVE_BOUNDARY} "
            "negative/boundary cases in total. More is fine; fewer fails the gate."
        ),
        (
            "## Data — five scopes only, never hardcode\n"
            f"Every value a case depends on must be written as a placeholder from the "
            f"five scopes: {', '.join('{{' + s + '.*}}' for s in DATA_SCOPES)}.\n"
            "No literal URLs, hosts, tokens, accounts or passwords anywhere in steps "
            "or expected — a literal fails the machine gate as unmaintainable.\n"
            "Preconditions carry the data the case needs, tagged by scope."
        ),
        (
            "## Rules in force for this task\n"
            f"{rules_block.strip() or '(no active rules for this task — nothing to violate)'}\n"
            "Every rule above must be answered in `applied_rules` with a disposition "
            "and a why. An undeclared rule counts as a violation."
        ),
        (
            "## Already existing cases for this test point (do not duplicate)\n"
            f"{history_slice.strip() or '(none — you are first)'}"
        ),
        (
            "## Output contract — JSON array, one object per case\n"
            f"Each case MUST carry exactly these {len(_CONTRACT_KEYS)} keys:\n"
            f"{json.dumps(list(_CONTRACT_KEYS), ensure_ascii=False)}\n"
            f"- title — one line, what is being verified\n"
            f"- surface — must be {surface!r}\n"
            f"- steps — array of {{seq, action, expected}}; seq starts at 1 and is "
            f"contiguous; action is the behaviour, expected is what that step shows\n"
            f"- expected — object keyed by the assertion fields of this surface "
            f"({list(assertion_fields)}); design each assertion separately\n"
            f"- preconditions — array of strings, data tagged by scope\n"
            f"- basis — fact | inferred | to_confirm (why you believe the expectation)\n"
            f"- evidence — array of test-point / acceptance-criteria ids you rely on\n"
            f"- scenario_tag — which strategy/axis this case serves\n"
            f"- applied_rules — array of {{rule_id, disposition, why}}; disposition is "
            f"followed | not_applicable | violated\n"
            f"Example case:\n{_render_json_example(surface)}\n"
            "Return ONLY the JSON array — no markdown fences, no prose."
        ),
        (
            "## Disciplines — violations are rejected by the machine gate\n"
            f"{_render_disciplines()}"
        ),
        (
            "## Hard limits\n"
            "- Do NOT invent requirements, parameters, endpoints or selectors not "
            "implied by the test point — hallucinated references fail the gate.\n"
            "- Do NOT write new test points or open new tasks (see nominate_only).\n"
            "- Do NOT hardcode data; every value goes through the five scopes."
        ),
    ]
    return "\n\n".join(sections)


# ── 回执归一（机器门前那一层）──────────────────────────────────


def _normalize_steps(raw: Any) -> list[dict[str, Any]]:
    """steps → ``[{seq, action, expected}]``；seq 从 1 连续补齐，action/expected 必填。"""
    out: list[dict[str, Any]] = []
    for i, item in enumerate(_as_list(raw), 1):
        if isinstance(item, Mapping):
            action = str(item.get("action") or item.get("description") or "").strip()
            expected = item.get("expected")
            if expected is None:
                expected = item.get("expect") or item.get("post_checks")
            seq = item.get("seq")
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                seq = i
        else:
            action, expected, seq = str(item).strip(), None, i
        if not action and not expected:
            continue
        out.append({"seq": seq, "action": action, "expected": expected})
    # seq 归一：按出现顺序重排为 1..n（LLM 可能乱序/跳号）
    for i, step in enumerate(out, 1):
        step["seq"] = i
    return out


def _normalize_expected(raw: Any, surface: str) -> dict[str, Any]:
    """expected → dict；补齐该端断言字段（缺的置 None，让契约显式可见）。"""
    if isinstance(raw, Mapping):
        expected = {str(k): v for k, v in raw.items()}
    elif isinstance(raw, str) and raw.strip():
        expected = {"summary": raw.strip()}
    else:
        expected = {}
    for field in SURFACE_ASSERTIONS.get(surface, ()):
        expected.setdefault(field, None)
    return expected


def _normalize_preconditions(raw: Any) -> list[str]:
    """preconditions → 字符串列表（``mc_test_case.preconditions`` 是一行一条）。"""
    out: list[str] = []
    for item in _as_list(raw):
        if isinstance(item, Mapping):
            text = str(item.get("text") or item.get("condition") or "").strip()
        else:
            text = str(item).strip()
        if text:
            out.append(text)
    return out


def _normalize_applied_rules(raw: Any) -> list[dict[str, Any]]:
    """applied_rules → ``[{rule_id, disposition, why}]``。

    同时保留 ``status`` 键（与 :func:`.gates.check_rule_receipt` 读取的键同名）——
    机器门按 ``status`` 判、门②回执按 ``disposition`` 展示，两处口径一个真相。
    """
    out: list[dict[str, Any]] = []
    for item in _as_list(raw):
        if not isinstance(item, Mapping):
            continue
        rule_id = str(item.get("rule_id") or item.get("id") or "").strip()
        if not rule_id:
            continue
        disposition = str(
            item.get("disposition") or item.get("status") or ""
        ).strip().lower()
        why = str(item.get("why") or item.get("reason") or "").strip()
        out.append({
            "rule_id": rule_id,
            "disposition": disposition,
            "why": why,
            "status": disposition,
        })
    return out


def case_to_payload(case: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    """LLM 输出的一条 case → 归一化 ``mc_test_proposal.payload``（kind=case）。

    :param case: LLM 返回的单个 case 对象（提示词 JSON 契约的 9 键）。
    :param task: :func:`fanout_tasks` 产出的任务（取 ``testpoint``/``surface``）。

    归一内容：

    - ``slot_key`` —— :func:`.gates.make_slot_key`（flow 轴：
      ``flow:{tp_code}:{surface}:{variant}``；带 ``api_id`` 时走 api 轴）。
    - ``evidence_class`` —— :func:`map_evidence_class`（fact→static、
      inferred→inferred、to_confirm/缺省→claimed）。
    - ``testpoint_code`` —— ``task["testpoint"]["code"]``（反查需求/模块用）。
    - ``steps``/``expected``/``preconditions``/``applied_rules`` 形态归一（见各 helper）。
    """
    case = _as_dict(case)
    tp = _as_dict(task.get("testpoint"))
    surface = str(task.get("surface") or case.get("surface") or "").strip()
    tp_code = str(tp.get("code") or "").strip()

    confirm_ref = case.get("confirm_ref")
    api_id = case.get("api_id") or (
        confirm_ref.get("api_id") if isinstance(confirm_ref, Mapping) else None
    )
    variant = (
        case.get("variant")
        or case.get("scenario_tag")
        or case.get("title")
        or task.get("task_id")
        or "v1"
    )
    basis_raw = case.get("basis")
    evidence_class_raw = case.get("evidence_class")
    payload: dict[str, Any] = {
        "title": str(case.get("title") or "").strip(),
        "surface": surface,
        "intent": str(case.get("intent") or case.get("description") or "").strip()
                  or str(case.get("title") or "").strip(),
        "steps": _normalize_steps(case.get("steps")),
        "expected": _normalize_expected(case.get("expected"), surface),
        "preconditions": _normalize_preconditions(case.get("preconditions")),
        "basis": str(basis_raw or tp.get("basis") or "").strip().lower(),
        "evidence": [str(e) for e in _as_list(case.get("evidence"))],
        "scenario_tag": str(case.get("scenario_tag") or "").strip(),
        "applied_rules": _normalize_applied_rules(case.get("applied_rules")),
        "testpoint_code": tp_code,
        "slot_key": make_slot_key(
            api_id,
            tp_code,
            surface,
            _variant_slug(variant),
        ),
        # evidence_class 只看本条 case 自带的 basis/evidence_class——不回退测试点
        # basis（测试点 basis=fact 不代表这条用例已被验证；缺省保守判 claimed）。
        "evidence_class": map_evidence_class(
            evidence_class_raw
            if str(evidence_class_raw or "").strip().lower() in PROPOSAL_EVIDENCE_CLASSES
            else basis_raw
        ),
    }
    return payload
