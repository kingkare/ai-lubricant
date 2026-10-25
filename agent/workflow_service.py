"""Workflow service: glue between the engine (agent/workflow.py), persistence
(agent/workflow_store.py), and the step executors (agent/workflow_exec.py).

One public entry point, :func:`start_and_run`:

1. resolve the def (versioned), validate it (again — defense in depth),
2. resolve the declared ``inputs`` (custom values overridable per-run via
   ``args``; ``account_list`` → the credential account list matching a URL —
   metadata only, never passwords),
3. create a ``workflow_runs`` row,
4. spawn a background task that runs the DAG, persisting each node's result
   (+ the variable snapshot) as it finishes, then marking the run terminal.

Runs are fire-and-forget — callers poll ``GET /workflows/{def_id}/runs/{run_id}``
for progress. Strong references are kept in :data:`_TASKS` so asyncio does not
collect the task mid-flight.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from loguru import logger

from agent import workflow_store
from agent.workflow import WorkflowContext, WorkflowRunner, validate_def, validate_inputs

# run_id → running asyncio.Task (kept strongly referenced until done).
_TASKS: dict[int, asyncio.Task] = {}


def _loads(value: Any) -> Any:
    """asyncpg returns JSONB as str unless a codec is installed — accept both."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


async def _build_executors(def_row: dict, caller: str | None):
    """Wire WorkflowStepExecutors for one run.

    A workflow is agent-independent: the executors resolve from the run's owner
    identity, not from an agent binding. ``owner`` = the def's user_id (None =
    admin). Every run gets its own workspace dir (data/workflows/<run>/) so
    code_run/file_write land outside any agent's tree.
    """
    from agent.workflow_exec import WorkflowStepExecutors

    owner = def_row.get("user_id")
    return WorkflowStepExecutors(
        owner=str(owner) if owner else None,
        workspace_root=f"data/workflows/{def_row.get('id')}",
    )


async def _resolve_accounts(owner: str | None, url: str) -> list[dict]:
    """Credential account list for a site URL — metadata only, never passwords.

    Site-domain match against ``browser_passwords`` (owner-scoped). This is the
    single resolution path for ``account_list`` triggers.
    """
    from db import PostgresClient
    from urllib.parse import urlparse

    host = urlparse(url or "").hostname or ""
    if not owner or not host or PostgresClient.pool is None:
        return []
    async with PostgresClient.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, domain, username, url FROM browser_passwords "
            "WHERE owner_user_id=$1 AND domain = $2 ORDER BY updated_at DESC",
            str(owner), host,
        )
    return [{"id": int(r["id"]), "domain": r["domain"],
             "username": r["username"], "url": r["url"] or ""} for r in rows]


async def _resolve_accounts_for(owner: str | None, url: str) -> list[dict]:
    """账号列表解析（唯一入口，供 _resolve_inputs 与扇出路径共用）。"""
    return await _resolve_accounts(owner, url)


def _account_site_url(declared_spec: dict, params: dict) -> str:
    """账号入参的站点 url：优先入参声明（site_url），回落启动方式（旧 account_list）。"""
    return str(declared_spec.get("site_url") or params.get("url") or "").strip()


async def _resolve_inputs(definition: dict, trigger: dict | None, owner: str | None,
                          caller: str | None, args: dict, single_account: dict | None = None,
                          accounts_override: list | None = None) -> dict:
    """Build the run's ``$input.*`` scope from the launch method's params.

    Iteration lives in the **graph** (a `foreach` node), not here. The launch
    method only seeds static values:

    - no trigger (manual / implicit agent) → declared inputs seeded from
      ``args`` key by key, so the run panel's typed form still populates
      ``$input.*`` even without a launch method selected;
    - a trigger with ``params.value`` (custom/agent) → that object as the
      base, overridden key by key by explicit ``args``;
    - a trigger with legacy ``params.source=account_list`` (+ ``url``/
      ``multiple``) → the account list resolved into every ``account``-typed
      input (``multiple=true`` fans out into N independent runs, one per
      account; ``multiple=false`` supplies the whole list once). Legacy
      shapes are kept for backward compatibility with saved graphs.

    Engine-supplied account values are exempt from type coercion (they are
    already the right shape); everything the caller supplies is coerced.
    """
    from agent.workflow import (
        validate_triggers, validate_inputs, coerce_inputs, apply_input_defaults,
    )

    resolved: dict[str, Any] = {}
    declared = validate_inputs(definition)
    trigger_id = trigger.get("id") if trigger else None
    spec = next((t for t in validate_triggers(definition) if t["id"] == trigger_id), None) if trigger_id else None

    # account 类入参的「显式白名单/单账号」（运行弹框）：调用方已解析好，直接用它。
    accounts_override_list = list(accounts_override) if accounts_override is not None else None
    # 只挑了一个（或扇出的当遍）= 单账号语义：username 标量，而不是单元素列表。
    _single = single_account if single_account is not None else (
        accounts_override_list[0] if accounts_override_list is not None and len(accounts_override_list) == 1 else None
    )
    # 引擎自己灌的键（account 类/旧复数 accounts）：跳过类型矫正——值已是引擎给的
    # 正确形状（扇出单跑的 accounts 是单对象，声明成 array 会被 _coerce_one 误拒）。
    engine_supplied: set[str] = set()

    def _finish() -> dict:
        # 扇出/弹框挑账号的当次运行：把当前账号（username 标量）灌进所有未被
        # 显式供值的 account 类声明入参——图里声明 account 就能直接用，
        # 不必为扇出再配一遍供值方式（用户口径：开始的入参是 account）。
        if _single is not None:
            for k, dspec in declared.items():
                if dspec.get("type") == "account" and k not in resolved and k not in args:
                    resolved[k] = str(_single.get("username") or "")
                    engine_supplied.add(k)
        coerced = {k: v for k, v in resolved.items() if k not in engine_supplied}
        out = coerce_inputs(definition, coerced)
        out.update({k: v for k, v in resolved.items() if k in engine_supplied})
        return apply_input_defaults(definition, out)

    if spec is None:
        # 未选启动方式（手动/Agent 隐式启动）：声明入参直接由 args 按键填充，
        # 这样运行面板按入参表填的值会落到 $input.*（而不只停在 $args）。
        for k in declared:
            if k in args:
                resolved[k] = args[k]
        # account 类：args 没带且声明了 site_url 时按 all 解析兜底（定时扇出的
        # 单跑形态由 single_account 给，走不到这里）。
        for k, dspec in declared.items():
            if k in resolved or dspec.get("type") != "account":
                continue
            if accounts_override_list is not None:
                resolved[k] = _account_value_for(accounts_override_list, single=_single)
                engine_supplied.add(k)
            elif dspec.get("site_url"):
                accounts = await _resolve_accounts(owner, dspec["site_url"])
                if accounts:
                    resolved[k] = _account_value_for(accounts, single=single_account)
                    engine_supplied.add(k)
        return _finish()

    params = spec.get("params") or {}

    source = params.get("source") or "custom"
    if source in ("custom", "agent"):
        # custom 与 agent 运行时同构（value 打底、args 逐键覆盖）；区别是语义——
        # agent 来源时 args 来自启动方 Agent，value 只是定时触发的兜底。
        value = params.get("value") or {}
        if isinstance(value, dict):
            resolved.update(value)
        for k in declared:
            if k in args:
                resolved[k] = args[k]
        return _finish()

    # ── 旧 account_list 形状（兼容）─────────────────────────────────────────
    # 站点优先取声明侧 site_url，回落 trigger 的 url（旧定义 url 在 trigger 上）。
    # 旧形状按**声明的键名**灌值：声明成 ``accounts``（array）→ 原始账号对象/对象
    # 数组（与重构前逐字一致，历史 fixture 依赖）；声明成 ``account`` 类型 → username
    # 形态（新语义）。两者都由 mode 决定单跑 vs 整体。
    def _site(dspec: dict) -> str:
        return str(dspec.get("site_url") or params.get("url") or "").strip()

    for k, dspec in declared.items():
        if k in args:
            resolved[k] = args[k]
        vtype = dspec.get("type")
        if vtype == "account":
            if accounts_override_list is not None:
                resolved[k] = _account_value_for(accounts_override_list, single=_single)
            elif params.get("multiple"):
                accounts = await _resolve_accounts(owner, _site(dspec))
                resolved[k] = _account_value_for(accounts, single=single_account)
            else:
                accounts = await _resolve_accounts(owner, _site(dspec))
                resolved[k] = _account_value_for(accounts)
            engine_supplied.add(k)
        elif k == "accounts":
            # 引擎固定的复数键（旧 fixture 的写法）：值就是解析出的账号本身。
            if accounts_override_list is not None:
                resolved[k] = (accounts_override_list[0] if _single is not None
                               else accounts_override_list)
            else:
                accounts = await _resolve_accounts(owner, _site(dspec))
                resolved[k] = (single_account if single_account is not None else accounts)
            engine_supplied.add(k)
    # 旧形状的隐式契约：即便没声明 accounts，引擎也发一个 $input.accounts
    # （重构前每条 account_list 触发都这么喂；历史图有直接引用它的）。
    if "accounts" not in resolved:
        if accounts_override_list is not None:
            resolved["accounts"] = (accounts_override_list[0] if _single is not None
                                    else accounts_override_list)
        else:
            accounts = await _resolve_accounts(owner, params.get("url") or "")
            resolved["accounts"] = (single_account if single_account is not None else accounts)
        engine_supplied.add("accounts")
    return _finish()


def _account_value_for(
    accounts: list, *, single: dict | None = None,
) -> Any:
    """把解析出的账号列表转成 account 类入参的值（username 形态）。

    - ``single``（扇出的当遍/调用方只挑了一个）→ 那个 username（标量）；
    - 其余（整体一次）→ 全部 username 列表。
    列表解析为空 → 返回 None（当作「没给值」，让 default/required 校验接手，
    不假装有一个空选择）。
    """
    names = [str(a.get("username") or "") for a in accounts if isinstance(a, dict) and a.get("username")]
    if single is not None:
        return str(single.get("username") or "")
    return names if names else None


def _resolve_accounts_nowait(owner: str | None, url: str) -> list[dict]:
    """Async wrapper kept separate so the fan-out path can await it cleanly."""
    return owner, url  # placeholder — real impl is _resolve_accounts (async)



async def start_and_run(
    def_id: int,
    *,
    args: dict[str, Any],
    version: Optional[int] = None,
    trigger: str = "manual",
    caller: str | None = None,
    cron: str = "",
    trigger_id: str | None = None,
) -> int:
    """Validate, record, and launch a workflow run. Returns the (first) run_id.

    ``trigger_id`` names one of the def's declared launch methods (cron); its
    ``params`` resolve into ``$input.*`` — and an ``account_list`` trigger with
    ``multiple=true`` fans the **whole graph out** into one run per account
    (each run's ``$input.accounts`` = a single account). The return is the first
    run's id; :func:`start_and_run_multi` reports all of them.
    """
    run_ids = await start_and_run_multi(
        def_id, args=args, version=version, trigger=trigger,
        caller=caller, cron=cron, trigger_id=trigger_id,
    )
    return run_ids[0] if run_ids else 0


async def start_and_run_multi(
    def_id: int,
    *,
    args: dict[str, Any],
    version: Optional[int] = None,
    trigger: str = "manual",
    caller: str | None = None,
    cron: str = "",
    trigger_id: str | None = None,
    single_account_only: bool = False,
    pick_accounts: Optional[list[str]] = None,
) -> list[int]:
    """:func:`start_and_run` but returns **every** launched run id (fan-out aware).

    ``single_account_only``（运行弹框「只跑一个账号测试」用）：account_list 扇出
    路径只跑**第一个**账号——$input.accounts = 单个账号对象，与逐账号扇出单跑的
    格式完全一致（测的就是定时实际会喂的形状）。

    ``pick_accounts``（运行弹框「选具体账号」用）：按 username 白名单过滤解析出的
    账号列表——扇出只跑选中的这几个（一个选中项 = 一条运行），整体一次则
    $input.accounts = 选中账号的数组。与 ``single_account_only`` 同时给时先取
    白名单再截首个。"""
    def_row = await workflow_store.get_def(def_id, version=version)
    if def_row is None:
        raise ValueError(f"workflow {def_id} (version={version}) not found")
    definition = _loads(def_row.get("definition"))
    validate_def(definition)

    if not isinstance(args, dict):
        raise ValueError("args must be an object")

    # 归属校验：非 owner 的 caller 走不到这里（API 层已校验），这里防御性再拦一次。
    owner = def_row.get("user_id")
    if owner is not None and caller is not None and str(owner) != str(caller):
        raise ValueError("workflow belongs to another user")
    owner_str = str(owner) if owner else (str(caller) if caller else None)

    # 解析选中的启动方式（只有 cron 是声明式的）。
    spec = None
    if trigger_id:
        from agent.workflow import validate_triggers

        spec = next((t for t in validate_triggers(definition or {}) if t["id"] == trigger_id), None)
        if spec is None:
            raise ValueError(f"workflow {def_id} has no launch method {trigger_id!r}")
        if not spec["enabled"]:
            raise ValueError(f"启动方式 {trigger_id!r} 已停用")
        if not cron:
            cron = spec.get("cron") or ""

    params = (spec or {}).get("params") or {}
    want = {str(u).strip() for u in (pick_accounts or []) if str(u).strip()}
    declared = validate_inputs(definition)

    # 扇出：source=account_list + multiple=true → 拆成 N 条独立运行，每条一个账号。
    legacy_fanout = params.get("source") == "account_list" and params.get("multiple")

    if legacy_fanout:
        fanout_site = ""
        for dspec in declared.values():
            if dspec.get("type") == "account" and dspec.get("site_url"):
                fanout_site = dspec["site_url"]
                break
        accounts = await _resolve_accounts(owner_str, fanout_site or params.get("url") or "")
        if not accounts:
            raise ValueError("密钥账号列表未解析到任何账号（站点 url 无匹配账号）")
        if want:
            # 弹框选了具体账号：只跑这些（白名单顺序按账号列表原序）。
            accounts = [a for a in accounts if str(a.get("username") or "") in want]
            if not accounts:
                raise ValueError("所选账号不在该站点的账号列表里（可能已被删除）")
        # 「只跑一个账号测试」：拿第一个，格式与扇出单跑一致（单账号对象）。
        if single_account_only:
            accounts = accounts[:1]
        run_ids: list[int] = []
        for account in accounts:
            rid = await _start_single(
                def_row, definition, args, trigger=trigger, caller=caller, cron=cron,
                trigger_id=trigger_id, single_account=account,
            )
            run_ids.append(rid)
        return run_ids
    # 运行弹框指定了账号白名单 / 「只跑一个」但非扇出：整体一次，只灌选中数组。
    if want or single_account_only:
        site = ""
        for dspec in declared.values():
            if dspec.get("type") == "account":
                site = _account_site_url(dspec, params)
                break
        accounts = await _resolve_accounts(owner_str, site or params.get("url") or "")
        if not accounts:
            raise ValueError("密钥账号列表未解析到任何账号（站点 url 无匹配账号）")
        if want:
            accounts = [a for a in accounts if str(a.get("username") or "") in want]
            if not accounts:
                raise ValueError("所选账号不在该站点的账号列表里（可能已被删除）")
        if single_account_only:
            accounts = accounts[:1]
        return [await _start_single(
            def_row, definition, args, trigger=trigger, caller=caller, cron=cron,
            trigger_id=trigger_id, single_account=None, accounts_override=accounts,
        )]

    return [await _start_single(
        def_row, definition, args, trigger=trigger, caller=caller, cron=cron,
        trigger_id=trigger_id, single_account=None,
    )]


async def _start_single(
    def_row: dict, definition: dict, args: dict[str, Any], *,
    trigger: str, caller: str | None, cron: str,
    trigger_id: str | None, single_account: dict | None,
    accounts_override: list | None = None,
) -> int:
    """Create one ``workflow_runs`` row and spawn its background DAG task."""
    run_id = await workflow_store.start_run(def_row["id"], int(def_row["version"]), trigger, args)
    task = asyncio.create_task(
        _execute(
            def_row, definition, run_id, args, trigger=trigger, caller=caller,
            cron=cron, trigger_id=trigger_id, single_account=single_account,
            accounts_override=accounts_override,
        ),
        name=f"workflow-run-{run_id}",
    )
    _TASKS[run_id] = task
    task.add_done_callback(lambda _t, rid=run_id: _TASKS.pop(rid, None))
    return run_id


async def _execute(
    def_row: dict,
    definition: dict,
    run_id: int,
    args: dict[str, Any],
    *,
    trigger: str,
    caller: str | None,
    cron: str,
    trigger_id: str | None = None,
    single_account: dict | None = None,
    accounts_override: list | None = None,
) -> None:
    """Run the DAG, record everything, then fire the 结束/失败 终态钩子."""
    owner = def_row.get("user_id")
    owner_str = str(owner) if owner else (str(caller) if caller else None)
    executors = await _build_executors(def_row, caller)
    inputs = await _resolve_inputs(definition, {"id": trigger_id} if trigger_id else None,
                                   owner_str, caller, args, single_account=single_account,
                                   accounts_override=accounts_override)
    ctx = WorkflowContext(inputs=inputs, args=args)

    async def on_node_done(res) -> None:
        payload = {
            "status": res.status,
            "output": res.output,
            "error": res.error,
            "decision": res.decision,
            "duration_ms": res.duration_ms,
        }
        # Progressively persist the variable snapshot with each node so a crash
        # leaves at most the in-flight node unrecorded.
        payload["_context"] = {"inputs": ctx.inputs, "args": ctx.args, "nodes": ctx.nodes}
        try:
            await workflow_store.record_node(run_id, res.node_id, payload)
        except Exception:  # noqa: BLE001 - persistence must not kill the run
            logger.warning("[workflow] record_node failed run=%s node=%s",
                           run_id, res.node_id, exc_info=True)

    runner = WorkflowRunner(definition, executors=executors, on_node_done=on_node_done)
    result: dict[str, Any]
    try:
        result = await runner.run(ctx)
        await workflow_store.finish_run(run_id, result["status"], error="")
        logger.info("[workflow] run %s finished: %s", run_id, result["status"])
    except Exception as exc:  # noqa: BLE001 - engine bug / validation drift
        logger.exception("[workflow] run %s crashed", run_id)
        await workflow_store.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        result = {"status": "failed", "nodes": {}, "error": str(exc)}

    try:
        await _fire_terminal(def_row, definition, run_id, result, owner_str)
    except Exception:  # noqa: BLE001 — terminal side effects must never fail a finished run
        logger.exception("[workflow] terminal hook failed run=%s", run_id)


async def _fire_terminal(
    def_row: dict, definition: dict, run_id: int, result: dict, owner_str: str | None,
) -> None:
    """End notification / failure retry after a run reaches a terminal state.

    The merged 结束 config decides both: ``notify_when`` (success/failed/always)
    gates the notification, and ``failure.agent_id`` (when set) hands the failure
    context to an agent on a failed run. Everything here is best-effort: a dead
    webhook or a crashed agent must not surface.
    """
    from agent.workflow import validate_end_failure

    end, failure = validate_end_failure(definition or {})
    status = result.get("status")
    failed_node, failed_error = _first_failure(result)
    template = end.get("notify_template") or ""
    is_failed = status == "failed"
    is_success = status in ("ok", "partial")  # cancelled/interrupted 不是真正的完成/失败，任何时机都不报

    when = end.get("notify_when") or "always"
    should_notify = bool(
        template
        and (end.get("notify_channels") or [])
        and ((when == "success" and is_success) or (when == "failed" and is_failed) or when == "always")
    )
    if should_notify:
        text = _render_notify(template, def_row, run_id, status, failed_node, failed_error)
        await _dispatch_notify(owner_str, end["notify_channels"], text, run_id,
                               status, failed_node, failed_error,
                               extra_params=end.get("notify_params") or {})

    # Agent 重试：失败 + 配了重试 Agent → 把失败上下文交给它自行判断是否重跑。
    if is_failed and failure.get("agent_id"):
        await _retry_with_agent(failure["agent_id"], def_row, run_id, failed_node, failed_error)


def _first_failure(result: dict) -> tuple[str, str]:
    """First failed node (id, error) in a run result, for the notify/retry text."""
    for nid, res in (result.get("nodes") or {}).items():
        if isinstance(res, dict) and res.get("status") == "failed":
            return nid, str(res.get("error") or "")
    return "", str(result.get("error") or "")


def _render_notify(template: str, def_row: dict, run_id: int, status: str,
                   failed_node: str, failed_error: str) -> str:
    """Substitute the fixed placeholder set into the notification template.

    Deliberately a tiny literal-replace (not the graph's ``$ref`` resolver):
    the template addresses run metadata, not graph variables, and must never
    blow up on an unknown placeholder.
    """
    subs = {
        "{$workflow.name}": str(def_row.get("name") or ""),
        "{$workflow.label}": str(def_row.get("label") or ""),
        "{$run.id}": str(run_id),
        "{$run.status}": str(status or ""),
        "{$run.trigger}": "",  # trigger is on the run row; not carried into the hook
        "{$run.error}": failed_error,
        "{$failed.node}": failed_node,
        "{$failed.error}": failed_error,
    }
    out = template
    for k, v in subs.items():
        out = out.replace(k, v)
    return out


async def _dispatch_notify(
    owner_str: str | None, channels: list[str], text: str, run_id: int,
    status: str, failed_node: str, failed_error: str,
    extra_params: dict | None = None,
) -> None:
    """Fan a terminal notification out to the workflow owner's selected channels.

    The payload always carries run_id/status/failed_node/failed_error; a legacy
    definition's ``notify_params`` is merged in when present (new definitions no
    longer expose that field in the editor).
    """
    if not owner_str:
        return  # 无归属（管理员建的流）→ 没有渠道可归属，跳过
    from user_platform.notify_dispatch import dispatch_to_channels

    payload = dict(extra_params or {})
    payload.setdefault("run_id", run_id)
    payload.setdefault("status", status)
    if failed_node:
        payload.setdefault("failed_node", failed_node)
        payload.setdefault("failed_error", failed_error)
    await dispatch_to_channels(
        owner_str, channels,
        event_type="workflow.run.finished",
        text=text,
        payload=payload,
        event_ref_id=str(run_id),
    )


async def _retry_with_agent(
    agent_id: Any, def_row: dict, run_id: int, failed_node: str, failed_error: str,
) -> None:
    """Hand the failure to an agent to investigate and decide whether to re-run."""
    if not agent_id:
        return
    try:
        from agent.agent_main import GenericAgent

        sop = (
            f"工作流「{def_row.get('label') or def_row.get('name')}」的 run #{run_id} 失败。\n"
            f"失败步骤={failed_node or '未知'}；错误={failed_error or '无'}。\n"
            "请排查原因；确认可以修复时，用 capability_call(name=\"workflow.run\", "
            f"args={{\"def_id\": {def_row.get('id')}}}) 重跑。不要盲目重试。"
        )
        agent = GenericAgent(agent_id=int(agent_id))
        await agent.run_task(sop)
    except Exception:  # noqa: BLE001 — retry agent is best-effort, never breaks the run
        logger.exception("[workflow] failure-retry agent run failed (def=%s)", def_row.get("id"))


def cancel_run(run_id: int) -> bool:
    """Cancel an in-flight run (the engine's per-node asyncio tasks get cancelled
    with it). Returns False when the run already finished."""
    task = _TASKS.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def shutdown() -> None:
    """Cancel every in-flight run (process shutdown)."""
    for task in list(_TASKS.values()):
        task.cancel()
    _TASKS.clear()


__all__ = ["start_and_run", "start_and_run_multi", "cancel_run", "shutdown", "_resolve_inputs"]
