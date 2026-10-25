"""生成流程自动推进驱动（25 号文 v4.1 门①→环节5→门②链的接线层）。

设计里的规矩：门①确认后"该需求全确认 → gen_status=已过门① → 自动进环节5"。
编排器（orchestrator.py）只做单段（分析/评审/提点/用例），**跨段推进在这里**：

- :func:`advance_after_gate1` —— 门①确认后调用：若该 run 的需求全部已过门①，
  自动起用例细化（run_case_generation）→ 评审跑批（review_run_proposals）→ 停门②
- :func:`advance_after_review` —— 评审跑批后调用：无 machine_pending 且无需回炉的
  提案时推进 cases_proposed（等人门②判决）

LLM/api_lookup/rules 全部注入（与 case.generate 的 _drive 同模式：dsh 单轮适配器）。
失败落 run.status=failed + decisions（不静默）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from . import orchestrator as gen_orch
from . import state as _state
from .models import TestGenRun, TestProposal
from .review_flow import review_run_proposals

LLMCall = Callable[[str], Awaitable[Any]]


async def _reqs_all_past_gate1(run: TestGenRun) -> bool:
    """该 run 范围内是否所有需求都已过门①（全确认才自动推进——按需放行）。"""
    reqs = await gen_orch._load_requirement_statuses(run)
    if not reqs:
        return False  # 无范围（空 run）不自动推进
    return all(r["gen_status"] in ("gate1_passed", "case_generating", "case_reviewing",
                                   "gate2_pending", "gate2_passed", "imported")
               for r in reqs)


def _make_llm_call(dsh_chat, config) -> LLMCall:
    """dsh 单轮 → JSON 的适配器（与 case.generate._drive 同法）。"""
    import json as _json

    async def call(prompt: str) -> Any:
        out = await dsh_chat(prompt=prompt, config=config)
        text = str(out.get("final_response") or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        return _json.loads(text)

    return call


async def advance_after_gate1(
    run: TestGenRun,
    *,
    dsh_chat: Any,
    config: Any,
    api_lookup: Callable[[str], Awaitable[Optional[dict]]],
    rules_provider: Optional[Callable[[Any], Awaitable[list[dict]]]] = None,
) -> dict[str, Any]:
    """门①确认后调用（testpoint.confirm 末尾按需触发）。

    需求全部已过门① → 用例细化 → 评审跑批 → 停 cases_proposed（门②）。
    有未过门①的需求 → 不动（等其余需求确认完）。
    """
    if not await _reqs_all_past_gate1(run):
        return {"advanced": False, "reason": "仍有需求未过门①"}

    llm = _make_llm_call(dsh_chat, config)

    async def _fail(exc: Exception) -> dict:
        await TestGenRun.filter(id=run.id).update(
            status="failed",
            decisions=(run.decisions or []) + [{"stage": "advance_gate1", "error": str(exc)}],
        )
        return {"advanced": True, "failed": str(exc)}

    try:
        case_res = await gen_orch.run_case_generation(run, llm_call=llm)
    except Exception as exc:  # noqa: BLE001 — 失败落 run
        return await _fail(exc)

    try:
        rules = list(await rules_provider(run) or []) if rules_provider else []

        async def _hard_rules(_r: Any) -> list[dict]:
            return rules

        review_res = await review_run_proposals(
            run, llm_a=llm, llm_b=llm,  # P1 单模型双评；P2 换异供应商注入
            api_lookup=api_lookup,
            hard_rules_provider=_hard_rules,
        )
    except Exception as exc:  # noqa: BLE001
        return await _fail(exc)

    # 评审跑批后落门②（无 machine_pending 才推进 cases_proposed）
    gate2 = await advance_after_review(run)
    return {"advanced": True, "case_generation": case_res, "review": review_res, "gate2": gate2}


async def advance_after_review(run: TestGenRun) -> dict[str, Any]:
    """评审跑批后调用：无待评审提案时推进 evaluating → cases_proposed（停门②）。"""
    pending = await TestProposal.filter(
        run_id=run.id, kind="case", review_state="machine_pending"
    ).count()
    if pending == 0 and run.status == "evaluating":
        _state.assert_transition(run.status, "cases_proposed")
        run.status = "cases_proposed"
        await run.save(update_fields=["status", "updated_at"])
        return {"advanced": True}
    return {"advanced": False, "pending": pending}


__all__ = ["advance_after_gate1", "advance_after_review"]
