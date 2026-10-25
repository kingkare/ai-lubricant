"""评审三道闸 · 纯函数层（25 号文 §环节6 第2-3 闸）。

「机器规则 → 双评 → 五维打分（棘轮）」里，机器规则那一闸已由
:mod:`testing_platform.generation.gates` 落地（P0 四查 + 两复用）；本模块负责
后两闸的**纯逻辑**：机器闸的调用封装、双评分工提示词与小解析器、双评合并
（不设仲裁）、五维打分合成（棘轮封顶）、棘轮比对、回炉任务判定。

依赖纪律（任务边界）：

- **纯函数 + 注入 llm**：不 import tortoise、不接 ORM、不 import ``ai_channel``
  （评审裁决的 JSON 解析器自写，很小）。机器闸复用通过
  ``from .gates import run_machine_gates``（同包只读）完成。
- LLM 全部由调用方注入（``async callable``），本模块只拼提示词、解析裁决文本。
- 只 import ``quality.errors``（ValidationIssue 形态）+ 同包 ``gates``。

双评 → 五维打分的**衔接歧义裁定**（25 号文未给评分模型注入位）：

    环节6 第2闸的双评输出契约固定为 ``{"verdict","reason","evidence_class"}``，
    没有分项分；第3闸又要求分项分合成总分并「<70→need_rework」。本 3 文件边界内
    没有第三家评分模型（review_flow 只注入 ``llm_a/llm_b``），故用**确定性派生**
    :func:`derive_breakdown` 把双评结论映射到五维分：A 侧管
    ``coverage``/``exception_ratio``（业务对不对），B 侧管
    ``assertion``/``executability``/``maintainability``（写得对不对），
    结论 passed→100 / failed→0 / 弃权→0。任一维 0 即触发 :func:`score_dimensions`
    的 69 封顶（棘轮）。这是文档「分项分」在本边界内的最小忠实落地，评分模型日后
    可无缝替换 ``derive_breakdown`` 的输入端。
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from .gates import run_machine_gates as _run_machine_gates
from ..quality.errors import ValidationIssue

# ── 五维权重与阈值（25 号文 §环节6 第2闸原文：覆盖25/断言25/异常流20/可执行15/可维护15）──
#: 五维权重常量。键名与 proposal.score_breakdown（§2.3：coverage/assert/exception/
#: executable/maintainable，v4 用全名）一致；权重之和 = 100。
SCORE_DIMENSIONS: dict[str, int] = {
    "coverage": 25,        # 覆盖维度（1正 N反配额 / 恒真式直测 / 边界齐全）
    "assertion": 25,       # 断言维度（三层断言具体，禁只有 status=200）
    "exception_ratio": 20, # 异常流/边界比例（≥3 异常/边界）
    "executability": 15,   # 可执行（步骤/请求/设备序列真实可跑）
    "maintainability": 15, # 可维护（五作用域归位，无硬编码/无幻觉引用）
}

#: 门②「整组保留前置」的总分线（25 号文 §门② 步骤2：≥85）。
PASS_SCORE = 85
#: 打分回炉线（25 号文 §环节6 第2闸：<70→need_rework）。
REWORK_SCORE = 70
#: 任一维 0 分的总分封顶值（棘轮的「软上限」——有一维崩了总分不许好看）。
ZERO_DIM_CAP = 69

#: 双评裁决取值域（passed/failed/inconclusive；blocked 是旧三态的弃权近邻，并入 inconclusive）。
_REVIEW_VERDICTS: tuple[str, ...] = ("passed", "failed", "inconclusive", "blocked")

# ── 双评分工 rubric（25 号文 §环节6 第2闸：各半职责、互不见、必须一致）──────────
#: A 评只看**业务对不对**：覆盖/恒真式/边界齐全。
REVIEW_A_RUBRIC: str = (
    "You are reviewer A. Your ONLY job is to judge whether the case is "
    "BUSINESS-CORRECT (is the right thing being tested):\n"
    "- COVERAGE: does it hit the required positive case plus the >=3 "
    "exception/boundary quota (1 positive + >=3 abnormal/boundary per feature)?\n"
    "- TAUTOLOGY CHECK: no mere restatement of a functional point as a test "
    "('the system works normally') — every case must assert something falsifiable.\n"
    "- BOUNDARY COMPLETENESS: boundary values / invalid inputs / error paths are "
    "present where the requirement implies them.\n"
    "Do NOT judge code/style quality (that is reviewer B's job)."
)

#: B 评只看**写得对不对**：可执行/断言具体/无幻觉引用/五作用域。
REVIEW_B_RUBRIC: str = (
    "You are reviewer B. Your ONLY job is to judge whether the case is "
    "WRITTEN-CORRECT (is the thing being tested written testably):\n"
    "- EXECUTABILITY: steps/request/device sequence actually run on the engine; "
    "no dangling references, no invented tool actions.\n"
    "- ASSERTION SPECIFICITY: assertions are concrete (field/value/business-code, "
    "response-time where applicable) — 'status=200 only' is NOT acceptable.\n"
    "- NO HALLUCINATED REFERENCES: every api_id / param / selector must be a real "
    "artifact; a reference to something that does not exist is an automatic fail.\n"
    "- FIVE SCOPES: literal URLs/tokens/credentials must be placeholders in one of "
    "the five scopes (env/auth/local/data/global); hard-coded values fail.\n"
    "Do NOT judge business coverage (that is reviewer A's job)."
)

#: 双评提示词正文（A/B 注入不同的 rubric；输出契约固定三字段）。
_REVIEW_PROMPT_BODY = """\
@@RUBRIC@@

INPUT
  The case proposal payload (Stage 5 product) is provided below under CASE_JSON.
  Review it against YOUR assigned concern ONLY.

OUTPUT — a single JSON object with exactly these keys:
  {
    "verdict": "passed" | "failed" | "inconclusive",
    "reason": "short concrete reason; cite the JSON path / field that fails",
    "evidence_class": "executed" | "static" | "inferred" | "claimed"
  }

RULES
  - "passed"  = every item under your concern holds.
  - "failed"  = at least one item under your concern is violated; name it.
  - "inconclusive" = you cannot decide without more context; say what is missing.
  - verdict belongs ONLY to your concern; do not drift into the other side.

CASE_JSON
  <<<
@@CASE_JSON@@
>>>

Produce ONLY the JSON object described above.
"""


# ── 提示词 / 小解析器 ──────────────────────────────────────────────
def build_review_prompt(side: str, proposal_payload: Any) -> str:
    """双评提示词（英文）：A 管业务对不对、B 管写得对不对。

    :param side: ``"a"`` 或 ``"b"``（其它值回落到 A 的 rubric）。
    :param proposal_payload: case payload（dict 或可 JSON 序列化的对象）。
    :returns: 完整提示词正文（英文）。
    """
    rubric = REVIEW_B_RUBRIC if str(side).strip().lower() == "b" else REVIEW_A_RUBRIC
    case_json = json.dumps(proposal_payload, ensure_ascii=False, default=str)
    return (_REVIEW_PROMPT_BODY
            .replace("@@RUBRIC@@", rubric)
            .replace("@@CASE_JSON@@", case_json))


def extract_json_object(text: str) -> dict:
    """从 LLM 文本里摘出第一个 JSON 对象（容忍 ```json 围栏与前后杂讯）。

    双评裁决与重写 payload 的文本解析共用此入口（自写，不 import ai_channel）。
    """
    t = str(text or "").strip()
    if not t:
        return {}
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001 — 解析失败走兜底摘括
        pass
    brace = re.search(r"\{.*\}", t, re.S)
    if brace:
        try:
            obj = json.loads(brace.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _norm_verdict(raw: Any) -> str:
    """裁决归一：lower + blocked→inconcluded（旧三态「弃权」近邻并入）。"""
    v = str(raw or "").strip().lower()
    if v not in _REVIEW_VERDICTS:
        return "inconclusive"
    return "inconclusive" if v == "blocked" else v


def parse_review_output(raw: Any) -> dict:
    """双评裁决小解析器（自写，不 import ai_channel）。

    接受 dict（直接读）或字符串（可能是 JSON / ```json 围栏 / 前后杂讯）。
    非法/缺 verdict → ``inconclusive``（弃权进人，宁缺毋滥）。返回
    ``{"verdict", "reason", "evidence_class"}``。
    """
    if isinstance(raw, Mapping):
        data = dict(raw)
    elif isinstance(raw, str):
        data = extract_json_object(raw)
    else:
        data = {}
    verdict = _norm_verdict(data.get("verdict"))
    return {
        "verdict": verdict,
        "reason": str(data.get("reason") or data.get("opinion") or "").strip(),
        "evidence_class": data.get("evidence_class"),
    }


# ── 机器闸（复用 gates，同包）────────────────────────────────────
async def machine_pass(
    proposal: Mapping[str, Any],
    *,
    api_lookup,
    existing_slot_keys,
    active_hard_rules,
) -> tuple[str, list[ValidationIssue]]:
    """机器闸一：调 :func:`gates.run_machine_gates` → 有 error 记 machine_rejected。

    :param proposal: proposal 形态（``{kind, payload, surface, evidence_class}``
        或裸 case dict，二者 :func:`gates.run_machine_gates` 都兼容）。
    :returns: ``(review_state, issues)``——``machine_pending`` 或 ``machine_rejected``。
    """
    result = await _run_machine_gates(
        proposal,
        api_lookup=api_lookup,
        existing_slot_keys=existing_slot_keys,
        active_hard_rules=active_hard_rules,
    )
    state = str(result.get("review_state") or "machine_pending")
    issues = list(result.get("issues") or [])
    return state, issues


# ── 双评合并（不设仲裁）──────────────────────────────────────────
def merge_dual(rev_a: Mapping[str, Any], rev_b: Mapping[str, Any]) -> dict:
    """双评结论合并（25 号文 §环节6 第2闸：**分歧不自动仲裁**）。

    - 都 passed → ``agree``
    - 任一 inconclusive/blocked → ``inconclusive``（弃权/被阻挡进人）
    - 否则（任一 failed）→ ``split``（两份意见一起进门②给人看）

    :returns: ``{"a": {...}, "b": {...}, "consensus": "agree|split|inconclusive"}``。
    """
    a = dict(rev_a)
    b = dict(rev_b)
    va = _norm_verdict(a.get("verdict"))
    vb = _norm_verdict(b.get("verdict"))
    if va == "passed" and vb == "passed":
        consensus = "agree"
    elif va == "inconclusive" or vb == "inconclusive":
        consensus = "inconclusive"
    else:
        consensus = "split"
    return {"a": a, "b": b, "consensus": consensus}


# ── 双评 → 五维分 的确定性衔接（歧义裁定，见模块 docstring）────────
#: A 侧（业务对不对）管的维度。
_A_DIMS: tuple[str, ...] = ("coverage", "exception_ratio")
#: B 侧（写得对不对）管的维度。
_B_DIMS: tuple[str, ...] = ("assertion", "executability", "maintainability")


def derive_breakdown(verdict_a: Any, verdict_b: Any) -> dict:
    """双评结论 → 五维分项分（确定性派生，0-100，无评分模型的暂代）。

    - A 侧 passed → ``coverage/exception_ratio`` = 100；否则 0。
    - B 侧 passed → ``assertion/executability/maintainability`` = 100；否则 0。
    - 弃权按 0（该侧未被证明过，不白给分）。
    """
    va = _norm_verdict(verdict_a)
    vb = _norm_verdict(verdict_b)
    breakdown: dict[str, int] = {}
    for dim in _A_DIMS:
        breakdown[dim] = 100 if va == "passed" else 0
    for dim in _B_DIMS:
        breakdown[dim] = 100 if vb == "passed" else 0
    return breakdown


def _safe_num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def score_dimensions(breakdown: Mapping[str, Any]) -> dict:
    """五维分项 × 权重合成总分（§环节6 第2闸 + 棘轮封顶）。

    - ``total = round(Σ(dim_score × weight) / 100)``（权重和=100）。
    - **任一维 0 → 封顶 69**（:data:`ZERO_DIM_CAP`；有一维崩了总分不许好看）。

    :returns: ``{"total": int, "capped": bool}``。
    """
    total = sum(_safe_num(breakdown.get(dim)) * SCORE_DIMENSIONS[dim]
                for dim in SCORE_DIMENSIONS)
    total = round(total / 100.0)
    capped = any(_safe_num(breakdown.get(dim)) == 0 for dim in SCORE_DIMENSIONS)
    if capped and total > ZERO_DIM_CAP:
        total = ZERO_DIM_CAP
    return {"total": int(total), "capped": capped}


# ── 棘轮（§环节6 第2闸：新版断言数<旧版拒收）──────────────────────
def _leaf_count(node: Any) -> int:
    """递归计数 `expected` 层的非空断言叶（每条非空断言 = 1）。"""
    if isinstance(node, Mapping):
        return sum(_leaf_count(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return sum(_leaf_count(v) for v in node)
    if node is None or node == "" or node == [] or node == {}:
        return 0
    return 1


def _count_assertions(payload: Mapping[str, Any]) -> int:
    """payload 的断言条数：``expected`` 层非空叶 + 显式 ``assertions[]`` 条目。"""
    n = _leaf_count(payload.get("expected"))
    extra = payload.get("assertions")
    if isinstance(extra, (list, tuple)):
        n += sum(1 for a in extra if a not in (None, "", []))
    return int(n)


def _has_expected(payload: Mapping[str, Any]) -> bool:
    exp = payload.get("expected")
    return exp not in (None, "", [], {})


def ratchet_check(old_payload: Mapping[str, Any], new_payload: Mapping[str, Any]) -> list[str]:
    """棘轮比对（§环节6 第2闸：review_round+1 后**新版断言数<旧版拒收**）。

    :returns: 违规项列表（空列表 = 棘轮通过）。每条是人读的违规说明。
    """
    violations: list[str] = []
    if _has_expected(old_payload) and not _has_expected(new_payload):
        violations.append("旧版存在 expected 断言层，新版缺失——棘轮拒收（不许删断言层）")
    old_n = _count_assertions(old_payload)
    new_n = _count_assertions(new_payload)
    if new_n < old_n:
        violations.append(f"断言条数从 {old_n} 降到 {new_n}——棘轮拒收")
    return violations


# ── 回炉任务判定 ────────────────────────────────────────────────
def rewrite_task(
    proposal: Mapping[str, Any],
    issues: list[Any],
    consensus: Optional[str] = None,
) -> Optional[dict]:
    """判定是否需要回炉（rejected/split/inconclusive/低分 → 回炉任务）。

    :param proposal: proposal 行 dict（读 ``id``/``review_state``/``score``，
        必要时读 ``reviews.consensus``）。
    :param issues: 触发回炉的问题列表（机器闸 Issue / 评审意见，原样透传给回炉）。
    :param consensus: 双评共识（若已单独取出）；None 时从 proposal.reviews 读。
    :returns: 回炉任务 dict（``{proposal_id, reason, consensus, issues}``）；
        无需回炉时返回 ``None``。
    """
    if not isinstance(proposal, Mapping):
        return None
    review_state = str(proposal.get("review_state") or "").strip().lower()
    score = proposal.get("score")
    reviews = proposal.get("reviews")
    ct = consensus or (reviews.get("consensus") if isinstance(reviews, Mapping) else None)
    ct = str(ct or "").strip().lower() or None

    reasons: list[str] = []
    if review_state == "machine_rejected":
        reasons.append("machine_rejected：机器闸打回")
    if ct in ("split", "inconclusive"):
        reasons.append(f"双评共识={ct}（分歧/弃权进人）")
    if score is not None and float(score) < REWORK_SCORE:
        reasons.append(f"五维分 {score} < {REWORK_SCORE}（低分回炉）")
    if not reasons:
        return None

    ser_issues: list[Any] = []
    for it in issues or []:
        if hasattr(it, "to_dict"):
            ser_issues.append(it.to_dict())
        else:
            ser_issues.append(it)
    return {
        "proposal_id": proposal.get("id"),
        "reason": "；".join(reasons),
        "consensus": ct or review_state,
        "issues": ser_issues,
    }


__all__ = [
    "SCORE_DIMENSIONS",
    "PASS_SCORE",
    "REWORK_SCORE",
    "ZERO_DIM_CAP",
    "REVIEW_A_RUBRIC",
    "REVIEW_B_RUBRIC",
    "build_review_prompt",
    "parse_review_output",
    "extract_json_object",
    "machine_pass",
    "merge_dual",
    "derive_breakdown",
    "score_dimensions",
    "ratchet_check",
    "rewrite_task",
]