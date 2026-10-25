"""探索任务的执行编排器（26 号文任务层）——把「规划 → 执行 → 应用」串起来。

职责边界（严格，照 :mod:`testing_platform.generation.orchestrator` 的做法）：

- **本模块只做编排**：依赖检查 → 状态流转 → 进度更新 → 应用收尾；
  具体的 AI 调用 / 真跑由调用方注入：

  - ``llm_call``：规划阶段的 LLM（``async (prompt) -> JSON dict``），不注入则用
    确定性兜底方案（无 LLM 也能测）。
  - ``execute_fn``：执行阶段真跑（``async (plan, ctx) -> dict``），agent/引擎在
    调用方那边；本模块不 import agent_runtime / 引擎。
  - ``env_resolver``：环境变量解析（缺省懒 import ``assets.service.
    resolve_environment_vars``——assets 是并发热区，照 generation/orchestrator
    的懒 import 手法，不顶层 import）。看依赖是否齐备只用**脱敏**结果
    （``mask_secrets=True``，只判键在不在，不碰密钥值）。
  - ``api_provider`` / ``apply_fn``：接口清单（mc_test_api）/ 产物应用（staged 翻正），
    缺省走既有 service，测试注入 fake。

状态机配合（:mod:`.models` ``can_transition``）：

- ``execute_explore`` 假设任务已被 ``confirm_plan`` 推进到 ``executing``
  （创建→规划→确定→执行 的「确定」一步是 surface 的 ``taskrun.plan.confirm`` 干的，
  不在本模块重复）。

  - 缺依赖 → ``executing → blocked``（合法边），**不硬跑**（26 号文理由 4）。
  - 成功 → ``executing → applying → completed``。
  - 失败 → ``executing → failed``（失败不连坐，收口不炸）。

依赖声明约定（scope 字段）：探索任务的 ``scope.required_env_vars`` 是一个**变量 key
列表**，声明「要跑这次探索，环境里必须配齐哪些变量」。缺任何一个 → blocker。当前
``taskflow/models.py`` 的 explore scope 维度（purpose/direction/client/target）尚未
包含该字段——这是任务层的接口缺口，本模块只按约定读它，不改 models（见报告）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from . import service as _tf

__all__ = [
    "plan_explore",
    "check_dependencies",
    "execute_explore",
]

#: 执行注入签名：``async (plan, ctx) -> dict``（真跑结果；ctx 里带 progress 回调）。
ExecuteFn = Callable[[dict, dict], Awaitable[dict]]

#: 环境变量解析注入签名：``async (env_id) -> {key: masked_value}``。
EnvResolver = Callable[[Any], Awaitable[Dict[str, Any]]]

#: 默认资源配置（task.resources 缺省时的兜底，26 号文 §1.5「成本可控」）。
DEFAULT_AGENT_COUNT: int = 2
DEFAULT_TOKENS_LIMIT: int = 4000

#: 探索目的 → 关注点（26 号文 §1.6 explore purpose；含用户口径的全称别名）。
FOCUS_BY_PURPOSE: Dict[str, str] = {
    "补用例": "哪些需求/模块还没有可信用例，优先补它们",
    "补齐用例": "哪些需求/模块还没有可信用例，优先补它们",
    "补需求": "哪些行为未文档化，把它们倒推回需求",
    "补齐需求": "哪些行为未文档化，把它们倒推回需求",
    "探索测试": "charter 驱动找缺陷，以发现真实故障为目标",
    "其他": "按 charter 自由探索，产出结构化发现",
}


def _default_env_resolver() -> EnvResolver:
    """懒 import assets.service.resolve_environment_vars（脱敏版，缺省解析器）。

    顶层不 import assets——assets 是并发热区（记忆「concurrent-sessions-
    overwrite-worktree」），照 generation/orchestrator 的函数内懒 import 手法。
    """

    async def _resolve(env_id: Any) -> Dict[str, Any]:
        from ..assets.service import resolve_environment_vars

        return await resolve_environment_vars(env_id, mask_secrets=True)

    return _resolve


async def _env_blockers(
    scope: Mapping[str, Any],
    env_id: Any,
    *,
    env_resolver: Optional[EnvResolver] = None,
) -> List[Dict[str, Any]]:
    """读 ``scope.required_env_vars`` + ``env_id``，算环境依赖 blockers（纯读）。"""
    required_vars: List[str] = list(scope.get("required_env_vars") or [])
    if not required_vars:
        return []

    blockers: List[Dict[str, Any]] = []

    if not env_id:
        blockers.append({
            "kind": "env",
            "name": "env_id",
            "why": "任务声明了必需变量但未选择环境",
            "how_to_fix": "创建任务时选定 env_id（或补选环境）",
        })
        return blockers

    resolve = env_resolver or _default_env_resolver()
    try:
        env_vars = await resolve(env_id)
    except Exception as exc:  # noqa: BLE001 — 解析失败即依赖不可用，转 blocker
        blockers.append({
            "kind": "env",
            "name": "env_resolve",
            "why": f"环境变量解析失败：{str(exc)[:200]}",
            "how_to_fix": "检查环境配置是否有效（env_id 是否指向存在的环境）",
        })
        return blockers

    env_keys = set(dict(env_vars or {}).keys())
    for var in required_vars:
        if var not in env_keys:
            blockers.append({
                "kind": "env",
                "name": var,
                "why": f"必需变量 {var!r} 未在环境（含继承链）中配置",
                "how_to_fix": f"在环境中添加变量 {var!r}",
            })
    return blockers


async def check_dependencies(task_run_id: Any, *, env_resolver: Optional[EnvResolver] = None) -> List[Dict[str, Any]]:
    """依赖检查（纯函数化：只读不写）——读 ``task.scope`` + ``task.env_id``。

    :returns: blockers 列表（``[{kind, name, why, how_to_fix}]``）。**空 = 可执行**。
    """
    task = await _tf.get_task(task_run_id)
    return await _env_blockers(
        task.get("scope") or {},
        task.get("env_id"),
        env_resolver=env_resolver,
    )


async def _list_apis(api_provider: Optional[Callable[[Mapping[str, Any]], Awaitable[List[dict]]]],
                     scope: Mapping[str, Any]) -> List[dict]:
    """api 端探索路径：从 mc_test_api 列接口（provider 注入；缺省给 scope 占位）。"""
    if api_provider is not None:
        try:
            return list(await api_provider(scope) or [])
        except Exception:  # noqa: BLE001 — 列接口失败不炸规划，退回占位
            return []
    return []


def _steps_for(scope: Mapping[str, Any], apis: List[dict]) -> List[dict]:
    """按 direction（api/web/app）拼探索路径清单。"""
    direction_list = scope.get("direction") or []
    if isinstance(direction_list, str):
        direction_list = [direction_list]
    steps: List[dict] = []
    if "api" in direction_list:
        steps.append({
            "type": "api",
            "action": "接口探索",
            "apis": apis,
            "target": scope.get("target"),
        })
    if "web" in direction_list or "app" in direction_list:
        steps.append({
            "type": "ui",
            "action": "界面探索",
            "modules": scope.get("modules") or [],
            "client": scope.get("client"),
            "target": scope.get("target"),
        })
    return steps


def _focus_for(scope: Mapping[str, Any]) -> str:
    purpose = str(scope.get("purpose") or "")
    return FOCUS_BY_PURPOSE.get(purpose, FOCUS_BY_PURPOSE.get("其他", ""))


def _resources_for(resources: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "agent_count": int(resources.get("agent_count") or DEFAULT_AGENT_COUNT),
        "tokens_limit": int(resources.get("tokens_limit") or DEFAULT_TOKENS_LIMIT),
    }


def _merge_plan(base: dict, llm: Any) -> dict:
    """把 LLM 规划的 dict 安全并回确定性底稿（只认 dict，其余字段保留底稿）。"""
    if not isinstance(llm, dict):
        return base
    out = dict(base)
    for key in ("focus", "steps", "roles"):
        if llm.get(key):
            out[key] = llm[key]
    for key in ("dependency_check", "resources"):
        if isinstance(llm.get(key), (dict, list)):
            out[key] = llm[key]
    return out


def _plan_prompt(scope: Mapping[str, Any]) -> str:
    """规划提示词（llm_call 输入）。英文提示词（记忆「prompts-written-in-english」）。"""
    import json as _json

    return (
        "You plan an exploration task for an AI testing platform.\n"
        "Scope: " + _json.dumps(dict(scope), ensure_ascii=False, default=str) + "\n"
        "Return JSON with roles {planner, executor, debugger}, steps (ordered list of "
        "exploration actions), focus (one sentence), dependency_check (list of missing "
        "dependencies) and resources {agent_count, tokens_limit}."
    )


async def plan_explore(
    task_run_id: Any,
    *,
    llm_call: Optional[Callable[[str], Awaitable[Any]]] = None,
    api_provider: Optional[Callable[[Mapping[str, Any]], Awaitable[List[dict]]]] = None,
    env_resolver: Optional[EnvResolver] = None,
) -> dict:
    """为探索任务生成方案（并落库 ``update_plan``，供确定/执行阶段使用）。

    方案内容（26 号文 §1.5 方案五件）：

    - ``roles``：planner（定探索路径）/ executor（真跑）/ debugger（判失败）
    - ``steps``：探索路径清单（api 端从 mc_test_api 列接口；web/app 端按 scope.modules）
    - ``dependency_check``：环境依赖 blockers（脱敏；``scope.required_env_vars`` 缺的在此）
    - ``focus``：按探索目的给关注点
    - ``resources``：agent_count / tokens_limit（从 task.resources 取，缺省给默认）

    :param llm_call: 可选，注入的 LLM（``async (prompt) -> JSON``）——给规划做细化，
        注入则调用；未注入或不返回 dict 则用确定性兜底方案。
    """
    task = await _tf.get_task(task_run_id)
    scope = task.get("scope") or {}
    env_id = task.get("env_id")

    blockers = await _env_blockers(scope, env_id, env_resolver=env_resolver)
    plan: dict = {
        "roles": {
            "planner": "确定探索路径",
            "executor": "实际执行探索",
            "debugger": "判断执行失败原因",
        },
        "steps": _steps_for(scope, await _list_apis(api_provider, scope)),
        "dependency_check": blockers,
        "focus": _focus_for(scope),
        "resources": _resources_for(task.get("resources") or {}),
        "env_id": str(env_id) if env_id else None,
    }

    if llm_call is not None:
        try:
            plan = _merge_plan(plan, await llm_call(_plan_prompt(scope)))
        except Exception:  # noqa: BLE001 — LLM 失败不连坐，退回确定性方案
            pass

    # 落库（方案是任务的一个属性、随任务生死，26 号文 §1.4 歧义记录 2）
    await _tf.update_plan(task_run_id, plan=plan)
    return plan


async def execute_explore(
    task_run_id: Any,
    *,
    execute_fn: ExecuteFn,
    env_resolver: Optional[EnvResolver] = None,
    apply_fn: Optional[Callable[[Any], Awaitable[Dict[str, int]]]] = None,
) -> dict:
    """执行探索（只编排：依赖闸 → 注入真跑 → 收尾）。

    流程：

    1. **前置依赖检查**：``check_dependencies`` 有 blocker → ``transition(blocked,
       blockers=...)`` 并立刻返回，**不硬跑**（26 号文理由 4：缺依赖停下来等人）。
    2. **执行**：拿方案，把 ``plan`` + ``ctx``（含 ``progress`` 回调）交给注入的
       ``execute_fn``；``execute_fn`` 每步调 ``ctx["progress"]({...})`` 即可周期性更新进度。
    3. **收尾**：成功 → ``applying → 应用产物 → completed``；失败 → ``failed``。

    :param execute_fn: ``async (plan, ctx) -> dict``——真跑（agent/引擎），调用方注入。
    :param apply_fn: 可选，替代 service.apply_task_outputs（测试注入 fake 免落库）。
    :returns: ``{"status": "blocked"|"completed"|"failed", ...}``。

    :raises TaskflowError: 任务不在 ``executing``（未走 confirm_plan / 已终态）——
        **先拦、不硬跑**，免得空耗一轮 execute_fn 才在收尾流转上炸。
    """
    # 0. 入口状态护栏：执行段只认 executing（confirm_plan 已把 planned 推进来）
    task = await _tf.get_task(task_run_id)
    status = task.get("status")
    if status != "executing":
        raise _tf.TaskflowError(
            f"探索任务须先确定方案（executing）再执行，当前状态：{status}",
            code="invalid_state",
        )

    # 1. 前置依赖检查（先于任何执行动作）
    blockers = await check_dependencies(task_run_id, env_resolver=env_resolver)
    if blockers:
        await _tf.transition(task_run_id, to="blocked", blockers=blockers)
        return {"status": "blocked", "blockers": blockers}

    # 2. 方案（plan_explore 落库 + 返回）
    plan = await plan_explore(task_run_id, env_resolver=env_resolver)

    async def _report_progress(step: Mapping[str, Any]) -> None:
        await _tf.set_progress(task_run_id, progress={"steps": [dict(step or {})]})

    ctx: dict = {
        "task_run_id": task_run_id,
        "progress": _report_progress,
        "env_id": plan.get("env_id"),
    }

    # 3. 真跑（注入）
    try:
        result = await execute_fn(plan, ctx)
    except Exception as exc:  # noqa: BLE001 — 失败不连坐，收口 finished_at
        await _tf.transition(task_run_id, to="failed", reason=str(exc))
        return {"status": "failed", "reason": str(exc), "error": str(exc)}

    # 4. 收尾：applying → 翻正 staged 产物 → completed
    await _tf.transition(task_run_id, to="applying")
    applied = await (apply_fn or _tf.apply_task_outputs)(task_run_id)
    await _tf.transition(task_run_id, to="completed")
    return {"status": "completed", "applied": applied, "result": result}