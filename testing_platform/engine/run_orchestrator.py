"""run 级执行编排（25 号文 §环节8「按策略跑，出可信结果」——waves 接线层）。

**这一层是 waves 纯函数的生产调用方**——之前 plan_waves/admission_check/
retry_plan/FlakyLedger/result_state 全是「库就绪没人调」，执行页还是老路径。
本模块把它们接成一个可测的 run 级流程：

    select 用例（探索门闸 → 波次 A/B/C → flaky 隔离区先滤 → 并行准入）
    → 逐波执行（冒烟硬挂停后波；单场景失败按 retry_plan 分类处置）
    → 结果四态映射 + FlakyLedger 记账 → run.summary 汇总 + 状态推进

设计纪律（与 generation/orchestrator 同款）：

- **只做编排**：场景怎么跑是 :class:`ScenarioExecutor` 的事（引擎不耦合波次）；
  waves 纯函数不 import 引擎，本模块两边都只 import，不复制逻辑。
- **execute_fn 注入**：单场景执行是 ``async (scenario_id, ctx_kwargs) -> dict``
  （``ScenarioResult.to_dict()`` 形态），本模块不构造引擎实例——调用方
  （surface 的 run.run_task_now / 前端执行页）负责注入真实引擎或测试桩。
- **失败不连坐**：单场景引擎异常 → 按 ``retry_plan`` 分类处置（重试/隔离/停），
  不炸整轮；波闸只看结果四态，不看异常。
- **不写 run_scenario**：引擎侧 ``PgResultWriter`` 已做（调用方注入时）；
  本模块只算「该怎么跑」和「结果怎么判」，落库由引擎 writer + 调用方收尾。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Mapping, MutableSet, Optional, Sequence

from ..generation import waves as w

#: 单场景执行签名：``async (scenario_id, exec_kwargs) -> dict``（ScenarioResult.to_dict 形态）。
ExecuteFn = Callable[[Any, Mapping[str, Any]], Awaitable[dict]]

#: 承认的用例行形状（plan_waves 输入）：case_id + scenario_id 必带，其余可选。
#: ``verified_by_run``/``batch_tier``/``resource``/``data_scope`` 见 waves 各函数 docstring。


# ── 引擎错误 → failure_kind（§环节8「失败四分类」）─────────────────
#: 引擎异常的 kind（EngineError.kind / 异常类名）→ waves.FAILURE_KINDS 四类之一。
#: 无命中按**保守**判 script_issue（脚本/场景自身问题——脚本重写才能解决，
#: 重跑治不好）；真的环境问题靠 error dict 里的 env 显式标记走 env_flaky
#: （WILDCARD 兜底会把未知异常判成 case_outdated，那是"用例过期"语义，误判会
#: 把真 bug 送去过期流——这里显式兜底 script_issue 更安全）。
_ERROR_KIND_TO_FAILURE: dict[str, str] = {
    "scenario_not_found": "script_issue",
    "step_not_found": "script_issue",
    "env_cycle": "env_flaky",
    "env_not_found": "env_flaky",
    "undefined_variable": "script_issue",
    "template": "script_issue",
    "condition": "script_issue",
    "tool_not_registered": "script_issue",
    "assertion_not_registered": "script_issue",
    "method_not_registered": "script_issue",
    "tool_execution": "script_issue",
    "step_params": "script_issue",
    "abort": "env_flaky",
    "timeout": "env_flaky",
    "timeouterror": "env_flaky",
    "asyncio.timeouterror": "env_flaky",
}

#: error dict 里能显式标记环境的键（error["env"]=True 或 error["kind"]∈ env 类）。
_ENV_HINT_KEYS: frozenset[str] = frozenset({"env_error", "env_failure", "environment"})


def _error_to_failure_kind(error: Any) -> str:
    """引擎 error dict / 异常 → ``FAILURE_KINDS`` 四类之一（无命中保守 script_issue）。"""
    if error is None:
        return "script_issue"
    if isinstance(error, str):
        # 异常已被字符串化（_exec_one 的收敛形态）——按类名关键词粗判
        low = error.lower()
        if "timeout" in low or "connection" in low or "connect" in low:
            return "env_flaky"
        return "script_issue"
    if not isinstance(error, Mapping):
        return "script_issue"
    if any(error.get(k) for k in _ENV_HINT_KEYS):
        return "env_flaky"
    kind = str(error.get("kind") or "").strip().lower()
    if not kind:
        # 从消息里抽类名（_exec_one 收敛形态的 "TimeoutError: ..."）
        msg = str(error.get("message") or "").lower()
        kind = msg.split(":", 1)[0].strip() if ":" in msg else msg
    return _ERROR_KIND_TO_FAILURE.get(kind, "script_issue")


def select_runners(
    cases: Sequence[Mapping[str, Any]],
    *,
    require_explored: bool = True,
    ledger: Optional[w.FlakyLedger] = None,
    busy_resources: Optional[MutableSet[str]] = None,
) -> dict[str, Any]:
    """选例四闸（§环节8 步骤）：探索门 → 波次 → 隔离区 → 并行准入。

    :param cases: 候选用例行（``{case_id, scenario_id, batch_tier, verified_by_run,
        resource, data_scope}``；多余字段原样带过）。
    :param require_explored: 探索门闸开关（§环节7「未过门不进波次」；缺省开——
        执行编排是它的生产调用方）。
    :param ledger: flaky 账本（给了就把 ``quarantine_list`` 的用例先滤进隔离区）。
    :param busy_resources: 已被占的资源锁（admission 的 deferred 口径）。
    :returns: ``{waves, held_out, quarantined, deferred, batches}``——
        ``waves``=[{wave, batches: [[row,...],...], count}]（A→B→C，波内按 admission
        分的并行批）、``held_out``=探索门拦下、``quarantined``=flaky 隔离区、
        ``deferred``=资源锁占用的并行 defer。
    """
    held: list[dict] = []
    quarantined: list[dict] = []

    rows = list(cases or [])

    # ── 闸3：flaky 隔离区先滤（正文 354 行）——在探索门之后、波次之前 ──
    if ledger is not None:
        qset = set(ledger.quarantine_list())
        if qset:
            kept = []
            for row in rows:
                if str(row.get("case_id") or "") in qset:
                    quarantined.append({**row, "quarantine_reason": "flaky 隔离区（抖动≥2轮）"})
                else:
                    kept.append(row)
            rows = kept

    # ── 闸1：探索门（未过门不进波次）→ 闸2：波次 ──
    planned = w.plan_waves(rows, require_explored=require_explored, held_out=held)

    # ── 闸4：并行准入（波内分批）──
    deferred: list[dict] = []
    waves_out: list[dict] = []
    for wave in planned:
        batches = w.admission_check(wave["items"], busy_resources=busy_resources,
                                    deferred_out=deferred)
        waves_out.append({"wave": wave["wave"], "batches": batches,
                          "count": wave["count"]})
    return {"waves": waves_out, "held_out": held, "quarantined": quarantined,
            "deferred": deferred, "batches": waves_out}


async def run_waves(
    selection: Mapping[str, Any],
    *,
    execute_fn: ExecuteFn,
    exec_kwargs: Optional[Mapping[str, Any]] = None,
    retry_attempt_cap: Optional[int] = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """逐波执行 + 波闸 + 单场景失败分类处置（§环节8 步骤全文）。

    :param selection: :func:`select_runners` 的返回。
    :param execute_fn: 单场景执行（``async (scenario_id, kwargs) -> dict``）。
    :param exec_kwargs: 传给 execute_fn 的公共参数（envs/exec_mode/api_key_id 等，
        逐场景原样透传——数据行级别差异化由调用方在 execute_fn 闭包里做）。
    :param retry_attempt_cap: 重试轮上限（每场景；缺省用 waves.RETRY_POLICY 的
        max_attempts 口径，这里再给一道总闸防配置异常导致的无限循环）。
    :param sleep_fn: 退避等待（测试注入 asyncio.sleep 的假实现免真等）。
    :returns: ``{results, wave_gates, skipped_retry, states, summary}``——
        ``results``=[{...execute_fn 返回, result_state, failure_kind, wave, case_id,
        scenario_id, retry_of}]（含重试行，``retry_of`` 指回原 scenario_id）、
        ``wave_gates``=[wave_gate_decision 逐波]、``skipped_retry``=重试跳过清单、
        ``states``=四态计数、``summary``=run.summary 形态（total/pass/fail/error/
        flaky/skipped/duration_ms + inconclusive/not_judged）。
    """
    from ..generation.waves import (
        FlakyLedger,
        admission_check,
        plan_waves,
        result_state,
        retry_plan,
        wave_gate_decision,
    )

    ledger = FlakyLedger()
    attempts: dict[str, int] = {}          # scenario_id → 已消耗重试次数
    results: list[dict] = []
    wave_gates: list[dict] = []
    skipped_retry: list[dict] = []

    async def _exec_one(row: Mapping[str, Any], *, wave: str, retry_of: Optional[str]) -> dict:
        """单场景执行 + 四态派生。**条级隔离**：引擎抛穿也收敛成 error 结果行，
        不连坐同批其他场景（§环节8「熔断只跳依赖方」的最小面）。"""
        scenario_id = row.get("scenario_id") or row.get("case_id")
        try:
            raw = await execute_fn(scenario_id, dict(exec_kwargs or {}))
        except Exception as exc:  # noqa: BLE001 — 引擎层异常（非业务失败）收敛为行级 error
            return {
                "case_id": row.get("case_id"), "scenario_id": scenario_id,
                "wave": wave, "retry_of": retry_of,
                "status": "error", "failure_kind": None,
                "result_state": w.result_state("error", None),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
        out = dict(raw or {})
        st = str(out.get("status") or "")
        fk = out.get("failure_kind")
        if fk is None:
            # 引擎 ScenarioResult 不产 failure_kind（04 §4 分流是上游的事）。
            # 缺省时保守推导：error→按错误内容分类；fail→product_bug（断言挂
            # 默认真缺陷——不这么判，冒烟硬挂就永远停不了后波）；其余不推。
            if st == "error":
                fk = _error_to_failure_kind(out.get("error"))
            elif st == "fail":
                fk = "product_bug"
        out.update({
            "case_id": row.get("case_id"),
            "scenario_id": scenario_id,
            "wave": wave,
            "retry_of": retry_of,
            "result_state": result_state(st, fk),
            "failure_kind": fk,
        })
        return out

    for wave in selection.get("waves") or []:
        wave_name = wave.get("wave")
        for batch in wave.get("batches") or []:
            # 批内并行（admission 保证互不相交）；批间串行
            batch_rows = list(batch or [])
            if not batch_rows:
                continue
            raw_batch = await asyncio.gather(
                *(_exec_one(r, wave=wave_name, retry_of=None) for r in batch_rows)
            )
            results.extend(raw_batch)

        # ── 波闸（冒烟硬挂停后波）──
        gate = wave_gate_decision(results)  # 该波全部结果（含重试前的原行）
        wave_gates.append({"wave": wave_name, **gate})
        if not gate.get("proceed"):
            break

        # ── 失败分类处置：可重试的按 retry_plan 重跑一次（退避）──
        for res in [r for r in results if r.get("wave") == wave_name
                    and r.get("result_state") in ("挂", "判不了")]:
            if res.get("retry_of") is not None:
                continue  # 重试行本身不再重试（一轮封顶，防循环）
            plan = retry_plan(res.get("failure_kind"), attempts.get(str(res.get("scenario_id")), 0))
            if plan.get("action") != "retry":
                if plan.get("action") == "quarantine":
                    ledger.record(res.get("case_id"), res.get("result_state"))
                continue
            sid = str(res.get("scenario_id"))
            used = attempts.get(sid, 0)
            if retry_attempt_cap is not None and used >= retry_attempt_cap:
                skipped_retry.append({"scenario_id": sid, "reason": "retry_attempt_cap"})
                continue
            attempts[sid] = used + 1
            if plan.get("backoff_s"):
                await sleep_fn(float(plan["backoff_s"]))
            try:
                retried = await _exec_one(
                    {"case_id": res.get("case_id"), "scenario_id": res.get("scenario_id")},
                    wave=wave_name, retry_of=res.get("scenario_id"),
                )
                # 重试才过 → 引擎口径标 flaky（04 §6 决策 2）；仍挂 → 原结果为准
                if retried.get("result_state") == "过":
                    retried["status"] = "flaky"
                    retried["result_state"] = w.result_state("flaky", None)
                results.append(retried)
                ledger.record(retried.get("case_id"), retried.get("result_state"))
            except Exception:  # noqa: BLE001 — _exec_one 已条级收敛，此分支理论不可达；保底不炸
                continue

    # ── 汇总（run.summary 形态 + 四态计数）──
    states: dict[str, int] = {"过": 0, "挂": 0, "判不了": 0, "没裁": 0}
    for r in results:
        states[r["result_state"]] = states.get(r["result_state"], 0) + 1
    status_counts: dict[str, int] = {}
    total_ms = 0
    for r in results:
        st = str(r.get("status") or "")
        status_counts[st] = status_counts.get(st, 0) + 1
        total_ms += int(r.get("duration_ms") or 0)
    summary = {
        "total": len(results),
        "pass": status_counts.get("pass", 0),
        "fail": status_counts.get("fail", 0),
        "error": status_counts.get("error", 0),
        "flaky": status_counts.get("flaky", 0),
        "skipped": status_counts.get("skipped", 0),
        "duration_ms": total_ms,
        "states": states,
    }
    return {
        "results": results,
        "wave_gates": wave_gates,
        "skipped_retry": skipped_retry,
        "flaky_ledger": ledger.to_dict(),
        "summary": summary,
    }


__all__ = ["ExecuteFn", "select_runners", "run_waves"]
