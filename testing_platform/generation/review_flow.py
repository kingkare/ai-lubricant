"""评审三道闸 · DB 编排（25 号文 §环节6 / §第十部分 P1「三道闸」）。

对 ``mc_test_gen_run`` 下全部 ``kind=case`` 且 ``review_state=machine_pending``
的提案跑三道闸，并把结论写回 ``mc_test_proposal``：

    闸一 :func:`reviewer.machine_pass`（复用 gates 四查）
    闸二 llm_a / llm_b 双评 → :func:`reviewer.merge_dual`（**不设仲裁**）
    闸三 :func:`reviewer.score_dimensions` 五维分 → 写 reviews/score/score_breakdown/review_state

回炉编排（:func:`apply_rework`）：``machine_rejected`` / ``need_rework`` 提案 →
自写最小 :func:`build_rewrite_prompt` 重问 → **新行** proposal（``rewrite_of=原id``、
``version+1``、``slot_key+":vN"``）→ 棘轮不过 → 新行直接 ``machine_rejected`` 且
``review_round+1``；``review_round`` 达上限后 ``status=defer`` 进人（defer 复活走门②：
关联问题 answered → 提案重新 ``review_state=machine_pending``、``round+1``）。

状态写回口径（§2.3 ``PROPOSAL_REVIEW_STATES`` 只有五值，**split/inconclusive 不是
列值**——共识住在 ``reviews.consensus``，见 models.py §2.3 注与 state.compute_progress
的正交计数）：

- 机器闸有 error → ``machine_rejected``（不双评不打分）
- 双评出结论后：consensus ∈ {split, inconclusive} → ``scored``（**进人**，门②逐条；
  分数照存，UI 按 ``reviews.consensus`` 出 split/inconclusive 徽章、不给批量按钮）
- consensus=agree 且 total<70 → ``need_rework``（回边 evaluating→generating 回派）
- consensus=agree 且 total≥70 → ``scored``（≥85 才可整组保留，§门② 步骤2）

``in_duel`` 是「双评进行中」的过渡态；本编排单趟跑完 A/B 两侧，故不单独落该态
（若调用方要分两趟派发 A/B，可在两趟之间自写 ``in_duel``）。

依赖纪律（任务边界）：

- 只做 DB 编排：读写 ``mc_test_proposal``（本包 models，只读复用）；LLM 全部由
  调用方注入（``async callable``），本模块不 import LLM/agent 运行时。
- 不 import ``ai_channel``；重写裁决解析复用 :mod:`reviewer`。
- 不碰 orchestrator.py / gen_methods.py / case_gen.py（另一 agent 在写）、
  models.py / gates.py / state.py（只读复用）。
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping, Optional

from . import reviewer as rv
from .models import TestProposal

#: LLM 调用签名：prompt -> 原始输出（dict 或字符串；解析在 review_flow 内做）。
LLMCall = Callable[[str], Awaitable[Any]]

#: 重写轮数上限（任务口径「≤3轮后 defer 进人」；§环节6 第2闸 review_round+1 棘轮）。
MAX_REWRITE_ROUNDS = 3

#: :func:`apply_rework` 消费的评审态（机器打回 / 低分回炉）。
_REWORKABLE_STATES: tuple[str, ...] = ("machine_rejected", "need_rework")


def _issue_lines(issues: list[Any]) -> list[str]:
    """把问题（ValidationIssue / dict / str 混投）渲染成提示词行。"""
    lines: list[str] = []
    for i, issue in enumerate(issues or [], 1):
        if hasattr(issue, "to_dict"):
            issue = issue.to_dict()
        if isinstance(issue, Mapping):
            tag = issue.get("rule") or issue.get("severity") or "issue"
            lines.append(f"{i}. [{tag}] {issue.get('msg') or issue}")
        else:
            lines.append(f"{i}. {issue}")
    return lines


def build_rewrite_prompt(point_payload: Any, issues: list[Any]) -> str:
    """最小重写提示词（英文）：把触发回炉的问题逐条列给重写者。

    :param point_payload: 原提案 payload（dict）。
    :param issues: 触发回炉的问题（ValidationIssue / dict / 字符串混投）。
    :returns: 完整提示词正文（英文）。
    """
    issue_block = "\n".join(_issue_lines(issues)) or "(none)"
    return (
        "You are the case-rewrite agent (design 25, rework back-edge "
        "evaluating -> generating). Your previous case proposal was sent back "
        "for rework.\n\n"
        "ISSUES TO FIX (fix EVERY one; the ratchet rejects a rewrite that drops "
        "assertions, so keep the expected/assertion layer at least as strong as "
        "before):\n"
        f"{issue_block}\n\n"
        "PREVIOUS CASE JSON\n"
        "  <<<\n"
        f"{json.dumps(point_payload, ensure_ascii=False, default=str)}\n"
        "  >>>\n\n"
        "OUTPUT — a single JSON object: the FULL corrected case payload (same "
        "shape as PREVIOUS CASE JSON, with every issue fixed). Produce ONLY the "
        "JSON object."
    )


async def _load_slot_keys(run: Any) -> set[str]:
    """本 run 已有 slot_key 集合（幂等查重用；含本轮重写行的 ``:vN`` 键）。"""
    rows = await TestProposal.filter(run_id=run.id).values("slot_key")
    return {r["slot_key"] for r in rows if r.get("slot_key")}


async def _load_child_ids(run: Any) -> set[Any]:
    """本 run 已有后继版本的父 id 集合（rewrite_of 链）——用于 apply_rework 幂等。

    §6.2 回收协议：「对账冲突（同 slot 多版本）取 rewrite_of 链最新」。故已被
    重写过的提案不再重复重写（否则每调一次就多生一版）。
    """
    rows = await TestProposal.filter(run_id=run.id).values("rewrite_of")
    return {r["rewrite_of"] for r in rows if r.get("rewrite_of") is not None}


def _final_state(consensus: str, total: int) -> str:
    """双评共识 + 总分 → 落库 review_state（§2.3 五值内取值）。"""
    if consensus in ("split", "inconclusive"):
        return "scored"          # 分歧/弃权 → 进人（门②逐条），分数照存
    if total < rv.REWORK_SCORE:
        return "need_rework"     # 一致但低分 → 回炉（回边）
    return "scored"


# ══════════════════════════════════════════════════════════════════
# 闸一+闸二+闸三：整趟评审
# ══════════════════════════════════════════════════════════════════
async def review_run_proposals(
    run: Any,
    *,
    llm_a: LLMCall,
    llm_b: LLMCall,
    api_lookup: Callable[[str], Awaitable[Optional[dict]]],
    hard_rules_provider: Callable[[Any], Awaitable[list[dict]]],
    existing_slot_keys: Optional[set[str]] = None,
) -> dict[str, int]:
    """对该 run 全部 ``kind=case`` 且 ``review_state=machine_pending`` 的提案跑三道闸。

    :param run: ``TestGenRun`` 行（Tortoise 模型实例）。
    :param llm_a: A 侧评审 LLM（``async (prompt) -> dict|str``，只管业务对不对）。
    :param llm_b: B 侧评审 LLM（另一供应商，只管写得对不对）。
    :param api_lookup: ``async (api_id) -> dict|None``（幻觉核验注入，不查库）。
    :param hard_rules_provider: ``async (run) -> [{rule_id, ...}]``（active+hard 规则）。
    :param existing_slot_keys: 已存在 slot_key 集合；缺省自动查本 run 提案自取。
    :returns: ``{scored, machine_rejected, split, inconclusive, errors}`` 五个计数。
        ``errors``=单条评审异常（保持 machine_pending 可重跑续评，不打断批次）。

    计数口径（与 :func:`state.compute_progress` 一致——``scored`` 按
    ``review_state`` 计、``split``/``inconclusive`` 按 ``reviews.consensus`` 计，
    两者**正交可重叠**：一条 split 提案既是 scored（已过三道闸）也是 split）。
    """
    if existing_slot_keys is None:
        existing_slot_keys = await _load_slot_keys(run)
    active_hard_rules = list(await hard_rules_provider(run) or [])

    proposals = await TestProposal.filter(
        run_id=run.id, kind="case", review_state="machine_pending"
    )
    counts = {"scored": 0, "machine_rejected": 0, "split": 0, "inconclusive": 0,
              "errors": 0}

    for prop in proposals:
        # 单条隔离（e2e 实测修正）：一条提案的 LLM/api_lookup 异常不许打断整批
        # ——挂掉的提案保持 machine_pending（门②不会基于未评审用例开门，
        # advance_after_review 的 pending==0 口径兜底），错误记进 reviews 供排查，
        # 重跑本编排即可续评（幂等：只挑 machine_pending）。
        try:
            case_payload = dict(prop.payload or {})
            # gates.run_machine_gates 兼容两种形态，这里显式喂「提案形态」
            gate_input = {
                "kind": "case",
                "payload": case_payload,
                "surface": prop.surface,
                "evidence_class": prop.evidence_class,
            }
            # 幂等查重不该撞自己：把本提案的 slot_key 从"已有集合"里摘掉
            own_key = prop.slot_key
            keys_for_gate = (existing_slot_keys - {own_key}) if own_key else set(existing_slot_keys)

            state, issues = await rv.machine_pass(
                gate_input,
                api_lookup=api_lookup,
                existing_slot_keys=keys_for_gate,
                active_hard_rules=active_hard_rules,
            )
            machine_issues = [i.to_dict() for i in issues]

            if state == "machine_rejected":
                prop.review_state = "machine_rejected"
                prop.reviews = {"a": None, "b": None, "consensus": None,
                                "machine_issues": machine_issues}
                await prop.save(update_fields=["review_state", "reviews", "updated_at"])
                counts["machine_rejected"] += 1
                continue

            # ── 闸二：双评（互不见——各拿同一份 payload，各自 rubric）────────
            rev_a = rv.parse_review_output(await llm_a(rv.build_review_prompt("a", case_payload)))
            rev_b = rv.parse_review_output(await llm_b(rv.build_review_prompt("b", case_payload)))
            merged = rv.merge_dual(rev_a, rev_b)
            consensus = merged["consensus"]

            # ── 闸三：五维打分（双评结论 → 分项分 → 权重合成 + 封顶）─────────
            breakdown = rv.derive_breakdown(rev_a.get("verdict"), rev_b.get("verdict"))
            scored = rv.score_dimensions(breakdown)
            total = int(scored["total"])

            new_state = _final_state(consensus, total)
            prop.review_state = new_state
            prop.reviews = {**merged, "machine_issues": machine_issues}
            prop.score = float(total)
            prop.score_breakdown = breakdown
            await prop.save(update_fields=[
                "review_state", "reviews", "score", "score_breakdown", "updated_at",
            ])

            if new_state == "scored":
                counts["scored"] += 1
            if consensus == "split":
                counts["split"] += 1
            elif consensus == "inconclusive":
                counts["inconclusive"] += 1
        except Exception as exc:  # noqa: BLE001 — 单条失败隔离，见上注释
            counts["errors"] += 1
            try:
                prop.reviews = {**(prop.reviews or {}),
                                "review_error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                await prop.save(update_fields=["reviews", "updated_at"])
            except Exception:  # noqa: BLE001 — 记账也挂了就不再抛（批次优先）
                pass

    return counts


# ══════════════════════════════════════════════════════════════════
# 回炉：重写 → 新行版本链 → 棘轮
# ══════════════════════════════════════════════════════════════════
async def apply_rework(run: Any, llm_rewrite: LLMCall) -> dict[str, int]:
    """对 ``machine_rejected`` / ``need_rework`` 的 case 提案做一轮重写。

    每条提案（幂等：已有 rewrite_of 后继的提案跳过）：

    1. ``review_round >= ``:data:`MAX_REWRITE_ROUNDS` → 不再重写，
       ``status="defer"`` 进人（门②四判决之一；答完关联问题后由门②置回
       ``review_state=machine_pending``、``round+1`` 复活）。
    2. 否则 :func:`build_rewrite_prompt` → ``llm_rewrite`` → 新 payload。
    3. :func:`reviewer.ratchet_check` 棘轮比对：不过 → 新行 ``machine_rejected``
       （``review_round+1``，占掉一轮预算，下轮继续重写或 defer）。
    4. 棘轮过 → 新行 ``review_state=machine_pending``（重新进三道闸），
       ``rewrite_of=原id``、``version=原version+1``、
       ``slot_key=原slot_key+":vN"``（N=新版本号，§门② 步骤6 让位口径）。

    :returns: ``{rewritten, ratchet_rejected, deferred, errors}`` 四个计数
        （``errors``=重写 LLM 异常的单条数，提案保持回炉态下轮续跑）。
    """
    counts = {"rewritten": 0, "ratchet_rejected": 0, "deferred": 0, "errors": 0}
    proposals = await TestProposal.filter(
        run_id=run.id, kind="case",
        review_state__in=list(_REWORKABLE_STATES),
    )
    already_rewritten = await _load_child_ids(run)

    for prop in proposals:
        if prop.id in already_rewritten:
            continue  # 链上已有后继版本——回收协议取最新，本条不重复重写

        review_round = int(prop.review_round or 0)
        version = int(prop.version or 1) + 1

        if review_round >= MAX_REWRITE_ROUNDS:
            prop.status = "defer"
            prop.reviews = {**(prop.reviews or {}),
                            "defer_reason": f"重写轮数达上限 {MAX_REWRITE_ROUNDS}，转人工"}
            await prop.save(update_fields=["status", "reviews", "updated_at"])
            counts["deferred"] += 1
            continue

        old_payload = dict(prop.payload or {})
        issues = _rework_issues(prop)
        # 单条隔离（与评审批次同理）：一条重写挂掉不杀整批；提案保持回炉态，
        # 下轮 apply_rework 续跑（幂等：rewrite_of 链已建才跳过）。
        try:
            new_raw = await llm_rewrite(build_rewrite_prompt(old_payload, issues))
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            try:
                prop.reviews = {**(prop.reviews or {}),
                                "rewrite_error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                await prop.save(update_fields=["reviews", "updated_at"])
            except Exception:  # noqa: BLE001
                pass
            continue
        if isinstance(new_raw, Mapping):
            new_payload = dict(new_raw)
        elif isinstance(new_raw, str):
            new_payload = rv.extract_json_object(new_raw)
        else:
            new_payload = {}
        # 归一（e2e 实测修正）：重写 LLM 未必按 case 契约吐 applied_rules，
        # 且应据新稿重算 evidence_class 而非继承父集（继承会把 fact→static
        # 的"直证带来源"义务带给重写稿，造成 EV4 死循环）。steps/expected/
        # preconditions 只取新稿——不回退父集，否则会掩盖 ratchet 应捕捉的
        # 断言层回退（§环节6 第2闸 review_round+1 棘轮拒收口径）。
        new_payload = _normalize_rewrite_payload(new_payload, old_payload)

        violations = rv.ratchet_check(old_payload, new_payload)
        new_state = "machine_rejected" if violations else "machine_pending"
        if violations:
            counts["ratchet_rejected"] += 1
        else:
            counts["rewritten"] += 1

        await TestProposal.create(
            run_id=prop.run_id,
            project_id=prop.project_id,
            kind="case",
            payload=new_payload,
            source_agent="case_rewrite",
            surface=prop.surface,
            testpoint_id=prop.testpoint_id,
            evidence_class=new_payload.get("evidence_class") or "claimed",
            review_state=new_state,
            review_round=review_round + 1,
            rewrite_of=prop.id,
            version=version,
            applied_rules=list(new_payload.get("applied_rules") or []),
            slot_key=f"{prop.slot_key}:v{version}" if prop.slot_key else None,
            status="proposed",
            provenance={"rewrite_of": str(prop.id), "ratchet_violations": violations},
        )

    return counts


def _normalize_rewrite_payload(
    new_payload: Mapping[str, Any], old_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """把重写 LLM 吐回的原 payload 归一到 case 契约形态。

    只做**形态归一**，不做内容回退：``steps``/``expected``/``preconditions``
    一律取新稿（新稿没有就为空——棘轮会捕捉断言层回退并拒收），不让旧稿的
    断言层偷偷续命。旧 payload 只提供两样缺了会让机器门/棘轮无从判定的
    **身份字段**：``testpoint_code`` 与 ``surface``（重写者通常不重述它们）。

    归一项：

    - ``steps``/``preconditions`` —— :func:`case_gen._normalize_steps` 等价逻辑
      （``{seq, action, expected}``、seq 连续、空行剔除）。
    - ``expected`` —— 非 Mapping 的（字符串/列表）包成 dict；按端补齐
      ``SURFACE_ASSERTIONS`` 字段（缺置 None，契约显式可见）。
    - ``applied_rules`` —— :func:`case_gen._normalize_applied_rules` 等价逻辑
      （``{rule_id, disposition, why, status}``，无 rule_id 的条目剔除）。
    - ``evidence_class`` —— 据新稿 ``basis``/``evidence_class`` 重算
      （**不继承父集**：父稿 evidence_class=static 会把「直证带来源」义务带给
      重写稿，EV4 逐串索证 → 重写死循环）。
    - ``basis`` —— 小写化；缺省从 ``evidence_class`` 反推，再无则 ``claimed``。
    - 其余键（title/intent/evidence/scenario_tag/api_id/confirm_ref/variant）
      原样透传——重写者可以改内容，归一层不猜语义。
    """
    from .case_gen import (
        PROPOSAL_EVIDENCE_CLASSES,
        _normalize_applied_rules,
        _normalize_steps,
        map_evidence_class,
    )

    src = dict(new_payload or {})
    surface = str(src.get("surface") or old_payload.get("surface") or "").strip()

    basis = str(src.get("basis") or "").strip().lower()
    raw_class = src.get("evidence_class")
    evidence_class = (
        str(raw_class).strip().lower()
        if str(raw_class or "").strip().lower() in PROPOSAL_EVIDENCE_CLASSES
        else map_evidence_class(basis or raw_class)
    )
    if not basis:
        basis = {"static": "fact", "inferred": "inferred"}.get(evidence_class, "claimed")

    out: dict[str, Any] = dict(src)
    out["surface"] = surface
    out["basis"] = basis
    out["evidence_class"] = evidence_class
    out["steps"] = _normalize_steps(src.get("steps"))
    out["preconditions"] = _normalize_preconditions_list(src.get("preconditions"))
    out["expected"] = _normalize_expected_dict(src.get("expected"), surface)
    out["applied_rules"] = _normalize_applied_rules(src.get("applied_rules"))
    if not str(src.get("testpoint_code") or "").strip():
        out["testpoint_code"] = str(old_payload.get("testpoint_code") or "").strip()
    return out


def _normalize_preconditions_list(raw: Any) -> list[str]:
    """preconditions → 字符串列表（重写稿可能是 dict/str 混投）。"""
    from .case_gen import _as_list

    out: list[str] = []
    for item in _as_list(raw):
        if isinstance(item, Mapping):
            text = str(item.get("text") or item.get("condition") or "").strip()
        else:
            text = str(item).strip()
        if text:
            out.append(text)
    return out


def _normalize_expected_dict(raw: Any, surface: str) -> dict[str, Any]:
    """expected → dict；按端补齐断言字段（缺置 None，契约显式可见）。"""
    from .case_gen import SURFACE_ASSERTIONS

    if isinstance(raw, Mapping):
        expected = {str(k): v for k, v in raw.items()}
    elif isinstance(raw, str) and raw.strip():
        expected = {"summary": raw.strip()}
    elif isinstance(raw, (list, tuple)) and raw:
        # 列表形态：当成逐条断言（重写 LLM 常见变形），保留条目编号键
        expected = {f"assert_{i}": v for i, v in enumerate(raw, 1)}
    else:
        expected = {}
    # 补齐断言字段仅在「新稿确实有断言层」时做——空 expected 补成
    # {status_code: None, ...} 会让 ratchet 的 _has_expected 误判有断言，
    # 掩盖「重写丢断言层」这一最该拒收的情形（§环节6 第2闸）。
    if expected:
        for field in SURFACE_ASSERTIONS.get(surface, ()):
            expected.setdefault(field, None)
    return expected


def _rework_issues(prop: Any) -> list[Any]:
    """从提案里凑出「要修什么」：机器闸问题优先，其次双评意见/棘轮违规。"""
    reviews = prop.reviews or {}
    issues: list[Any] = list(reviews.get("machine_issues") or [])
    if not issues:
        for side in ("a", "b"):
            rev = reviews.get(side)
            if isinstance(rev, Mapping) and str(rev.get("reason") or "").strip():
                issues.append({"rule": f"review_{side}_{rev.get('verdict') or '?'}",
                               "msg": str(rev.get("reason"))})
    provenance = getattr(prop, "provenance", None) or {}
    for v in provenance.get("ratchet_violations") or []:
        issues.append({"rule": "ratchet", "msg": str(v)})
    if not issues:
        issues.append({"rule": "rework",
                       "msg": "评审未通过（无明细）——补齐断言层与异常/边界覆盖"})
    return issues


__all__ = [
    "LLMCall",
    "MAX_REWRITE_ROUNDS",
    "build_rewrite_prompt",
    "review_run_proposals",
    "apply_rework",
]