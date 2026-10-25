"""探索任务编排器（taskflow/explore_orchestrator.py）纯函数单测（26 号文）。

**不需要 PG 基线**——不碰 ORM、不连库；只注入 fake（照 generation 纯函数测试风格，
对应记忆「tests-need-postgres-baseline」里被豁免的那类纯逻辑测试）：

- ``service.get_task / transition / set_progress / update_plan / apply_task_outputs``
  全部 monkeypatch 成内存 fake（记录调用、不落库）；
- 环境变量解析走注入的 ``env_resolver``（不 import assets.service）；
- 产物翻正方向直接测 ``staging`` 的 SQL 生成（fake conn，验证 unstage/restage/delete
  三种标识翻转）。

覆盖点：

1. check_dependencies：缺变量 → 有 blocker；齐 → 空。
2. blocked 语义：execute_explore 有 blocker 时**先 transition 到 blocked 且不调
   execute_fn**（fake execute_fn 断言未被调用）。
3. 状态流转护栏：completed → planning 必须抛 TaskflowError。
4. apply/revert 的标识翻转方向（fake conn 断言 SQL）。
"""
from __future__ import annotations

import uuid

import pytest

from testing_platform.taskflow import explore_orchestrator as orch
from testing_platform.taskflow import service as tf_service
from testing_platform.taskflow import staging
from testing_platform.taskflow.models import can_transition


# ── fakes ────────────────────────────────────────────────────────────────

class _FakeConn:
    """staging 的 fake conn：只记录 SQL + 参数，返回空结果。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, list]] = []
        self.scripts: list[str] = []

    async def execute_query(self, sql: str, params=None):
        self.executed.append((sql, list(params or [])))
        return (1, [])

    async def execute_query_dict(self, sql: str, params=None):
        self.executed.append((sql, list(params or [])))
        # to_regclass / information_schema 探测 → ok；count(*) → 1
        if "to_regclass" in sql or "information_schema" in sql:
            return [{"ok": True}]
        return [{"n": 1}]

    async def execute_script(self, sql: str):
        self.scripts.append(sql)
        return None


def _task_doc(**over):
    base = {
        "id": str(uuid.uuid4()),
        "code": "TRUN-0001",
        "project_id": str(uuid.uuid4()),
        "type": "explore",
        "status": "executing",
        "phase_status": "execute",
        "title": "探索登录",
        "scope": {"purpose": "补用例", "direction": ["api"], "client": "http",
                  "target": {"api_target": []}},
        "env_id": None,
        "resources": {},
        "plan": {},
        "progress": {},
        "blockers": [],
    }
    base.update(over)
    return base


def _patch_service(monkeypatch, task_doc=None, transition_to_raise=None):
    """把 service 的 DB 触碰点换成内存 fake，返回（调用记录, 状态）。"""
    doc = dict(task_doc or _task_doc())
    calls: dict = {"get": 0, "transition": [], "progress": [], "plan": [], "apply": 0}
    state = {"doc": doc}

    async def fake_get_task(_tid):
        calls["get"] += 1
        return dict(state["doc"])

    async def fake_transition(_tid, *, to, reason=None, blockers=None):
        if transition_to_raise and to == transition_to_raise[0]:
            raise tf_service.TaskflowError(transition_to_raise[1], code="invalid_transition")
        calls["transition"].append({"to": to, "reason": reason, "blockers": blockers})
        state["doc"]["status"] = to
        return dict(state["doc"])

    async def fake_set_progress(_tid, *, progress):
        calls["progress"].append(dict(progress or {}))
        return dict(state["doc"])

    async def fake_update_plan(_tid, *, plan, status_to=None, blockers=None, resources=None):
        calls["plan"].append(dict(plan or {}))
        return dict(state["doc"])

    async def fake_apply(_tid):
        calls["apply"] += 1
        return {"mc_test_case": 2}

    monkeypatch.setattr(tf_service, "get_task", fake_get_task)
    monkeypatch.setattr(tf_service, "transition", fake_transition)
    monkeypatch.setattr(tf_service, "set_progress", fake_set_progress)
    monkeypatch.setattr(tf_service, "update_plan", fake_update_plan)
    monkeypatch.setattr(tf_service, "apply_task_outputs", fake_apply)
    return calls, state


def _env_resolver(vars_map: dict):
    async def _r(_env_id):
        return dict(vars_map)
    return _r


def _make_table_exists(val):
    async def _f(_name):
        return val
    return _f


def _make_column_exists(val):
    async def _f(_table, _col):
        return val
    return _f


def _patch_staging_conn(monkeypatch):
    """给 staging 装 fake conn + 表/列探测（免 PG）。"""
    conn = _FakeConn()
    monkeypatch.setattr(staging, "_table_exists", _make_table_exists(True))
    monkeypatch.setattr(staging, "_column_exists", _make_column_exists(True))

    async def fake_conn():
        return conn

    monkeypatch.setattr(staging, "_conn", fake_conn)
    return conn


# ── 1. check_dependencies ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_dependencies_missing_var_blocks(monkeypatch):
    doc = _task_doc(scope={"purpose": "补用例", "direction": ["api"],
                           "required_env_vars": ["BASE_URL", "API_KEY"]},
                    env_id=str(uuid.uuid4()))
    _patch_service(monkeypatch, task_doc=doc)
    blockers = await orch.check_dependencies(
        doc["id"], env_resolver=_env_resolver({"BASE_URL": "https://x"}))
    assert [b["name"] for b in blockers] == ["API_KEY"]
    assert all(b["kind"] == "env" and b["why"] and b["how_to_fix"] for b in blockers)


@pytest.mark.asyncio
async def test_check_dependencies_all_present_empty(monkeypatch):
    doc = _task_doc(scope={"purpose": "补用例", "direction": ["api"],
                           "required_env_vars": ["BASE_URL", "API_KEY"]},
                    env_id=str(uuid.uuid4()))
    _patch_service(monkeypatch, task_doc=doc)
    blockers = await orch.check_dependencies(
        doc["id"], env_resolver=_env_resolver({"BASE_URL": "u", "API_KEY": "k"}))
    assert blockers == []


@pytest.mark.asyncio
async def test_check_dependencies_no_required_vars_empty(monkeypatch):
    # scope 未声明 required_env_vars → 不产生环境 blocker
    doc = _task_doc(env_id=None)
    _patch_service(monkeypatch, task_doc=doc)
    assert await orch.check_dependencies(doc["id"], env_resolver=_env_resolver({})) == []


@pytest.mark.asyncio
async def test_check_dependencies_no_env_id_blocks(monkeypatch):
    doc = _task_doc(scope={"purpose": "补用例", "direction": ["api"],
                           "required_env_vars": ["BASE_URL"]},
                    env_id=None)
    _patch_service(monkeypatch, task_doc=doc)
    blockers = await orch.check_dependencies(doc["id"], env_resolver=_env_resolver({}))
    assert [b["name"] for b in blockers] == ["env_id"]


@pytest.mark.asyncio
async def test_check_dependencies_resolver_failure_blocks(monkeypatch):
    doc = _task_doc(scope={"purpose": "补用例", "direction": ["api"],
                           "required_env_vars": ["BASE_URL"]},
                    env_id=str(uuid.uuid4()))
    _patch_service(monkeypatch, task_doc=doc)

    async def _boom(_env_id):
        raise RuntimeError("env not found")

    blockers = await orch.check_dependencies(doc["id"], env_resolver=_boom)
    assert blockers and blockers[0]["name"] == "env_resolve"


# ── 2. blocked 语义 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_explore_blocked_skips_execute_fn(monkeypatch):
    doc = _task_doc(status="executing",
                    scope={"purpose": "补用例", "direction": ["api"],
                           "required_env_vars": ["API_KEY"]},
                    env_id=str(uuid.uuid4()))
    calls, state = _patch_service(monkeypatch, task_doc=doc)

    called = {"n": 0}

    async def fake_execute(_plan, _ctx):
        called["n"] += 1
        return {"ok": True}

    out = await orch.execute_explore(
        doc["id"], execute_fn=fake_execute, env_resolver=_env_resolver({}))

    # 先 blocked、且**不硬跑**
    assert called["n"] == 0
    assert out["status"] == "blocked"
    assert out["blockers"]
    assert state["doc"]["status"] == "blocked"
    # 第一次 transition 就是 blocked，blockers 带进去了
    assert calls["transition"][0]["to"] == "blocked"
    assert calls["transition"][0]["blockers"]
    # 没走到 executing / applying / completed / failed
    assert [t["to"] for t in calls["transition"]] == ["blocked"]
    assert calls["apply"] == 0


@pytest.mark.asyncio
async def test_execute_explore_success_path(monkeypatch):
    doc = _task_doc(status="executing",
                    scope={"purpose": "补用例", "direction": ["api"],
                           "required_env_vars": ["BASE_URL"]},
                    env_id=str(uuid.uuid4()))
    calls, state = _patch_service(monkeypatch, task_doc=doc)

    async def fake_execute(_plan, ctx):
        # 执行期调一次进度回调
        await ctx["progress"]({"role": "executor", "state": "running"})
        return {"found": ["defect-1"]}

    out = await orch.execute_explore(
        doc["id"], execute_fn=fake_execute, env_resolver=_env_resolver({"BASE_URL": "u"}))

    assert out["status"] == "completed"
    assert out["result"] == {"found": ["defect-1"]}
    assert out["applied"] == {"mc_test_case": 2}
    assert calls["progress"], "执行期应至少落一次进度"
    # 收尾：applying → 翻正 → completed
    assert [t["to"] for t in calls["transition"]] == ["applying", "completed"]
    assert state["doc"]["status"] == "completed"
    assert calls["apply"] == 1


@pytest.mark.asyncio
async def test_execute_explore_failure_path(monkeypatch):
    doc = _task_doc(status="executing",
                    scope={"purpose": "探索测试", "direction": ["web"],
                           "required_env_vars": []},
                    env_id=None)
    calls, state = _patch_service(monkeypatch, task_doc=doc)

    async def fake_execute(_plan, _ctx):
        raise RuntimeError("engine blew up")

    out = await orch.execute_explore(
        doc["id"], execute_fn=fake_execute, env_resolver=_env_resolver({}))

    assert out["status"] == "failed"
    assert "engine blew up" in out["reason"]
    assert [t["to"] for t in calls["transition"]] == ["failed"]
    assert calls["apply"] == 0
    assert state["doc"]["status"] == "failed"


@pytest.mark.asyncio
async def test_execute_explore_uses_injected_apply_fn(monkeypatch):
    # apply_fn 注入时不碰 service.apply_task_outputs（免 DB）
    doc = _task_doc(status="executing", env_id=None,
                    scope={"purpose": "补用例", "direction": ["api"]})
    calls, _ = _patch_service(monkeypatch, task_doc=doc)

    async def fake_execute(_plan, _ctx):
        return {"ok": 1}

    async def fake_apply(_tid):
        return {"mc_test_case": 5}

    out = await orch.execute_explore(
        doc["id"], execute_fn=fake_execute,
        env_resolver=_env_resolver({}), apply_fn=fake_apply)
    assert out["applied"] == {"mc_test_case": 5}
    assert calls["apply"] == 0  # service.apply_task_outputs 未被调用


@pytest.mark.asyncio
async def test_execute_explore_passes_plan_and_ctx_to_execute_fn(monkeypatch):
    # execute_fn 收到 (plan, ctx)；ctx 带 progress 回调 + task_run_id
    doc = _task_doc(status="executing", env_id=None,
                    scope={"purpose": "补用例", "direction": ["api"]})
    _patch_service(monkeypatch, task_doc=doc)
    seen = {}

    async def fake_execute(plan, ctx):
        seen["plan"] = plan
        seen["ctx"] = ctx
        return {}

    await orch.execute_explore(doc["id"], execute_fn=fake_execute,
                               env_resolver=_env_resolver({}))
    assert set(seen["plan"]) >= {"roles", "steps", "dependency_check", "focus", "resources"}
    assert seen["ctx"]["task_run_id"] == doc["id"]
    assert callable(seen["ctx"]["progress"])


# ── 3. 状态流转护栏 ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transition_completed_to_planning_raises(monkeypatch):
    # completed 是终态 → 任何流转都非法 → TaskflowError
    assert not can_transition("completed", "planning")
    doc = _task_doc(status="completed")
    calls, _ = _patch_service(
        monkeypatch, task_doc=doc,
        transition_to_raise=("planning", "非法流转：completed → planning"))

    with pytest.raises(tf_service.TaskflowError):
        await tf_service.transition(doc["id"], to="planning")
    assert calls["transition"] == []  # 非法流转被拦下，未记录


@pytest.mark.asyncio
async def test_transition_illegal_raises_via_service_guard():
    # 直接测 service._transition_or_raise（真护栏）：completed → planning 抛 TaskflowError
    from testing_platform.taskflow.service import _transition_or_raise

    class _Run:
        status = "completed"

    with pytest.raises(tf_service.TaskflowError):
        await _transition_or_raise(_Run(), "planning")


def test_can_transition_terminal_has_no_outgoing():
    for terminal in ("completed", "failed", "cancelled"):
        assert not can_transition(terminal, "planning")
        assert not can_transition(terminal, "executing")


@pytest.mark.asyncio
async def test_execute_explore_from_completed_raises(monkeypatch):
    # 终态任务再执行 → 入口护栏先拦 → TaskflowError（不硬跑，execute_fn 不被调）
    doc = _task_doc(status="completed", env_id=None,
                    scope={"purpose": "补用例", "direction": ["api"]})
    calls, _ = _patch_service(monkeypatch, task_doc=doc)

    async def fake_execute(_plan, _ctx):  # pragma: no cover —— 不该被调
        raise AssertionError("终态任务不应进入执行")

    with pytest.raises(tf_service.TaskflowError):
        await orch.execute_explore(doc["id"], execute_fn=fake_execute,
                                   env_resolver=_env_resolver({}))
    assert calls["transition"] == []


# ── 4. apply/revert 标识翻转（staging 纯逻辑 + fake conn）─────────────────

@pytest.mark.asyncio
async def test_apply_staged_flips_unstage(monkeypatch):
    conn = _patch_staging_conn(monkeypatch)
    tid = str(uuid.uuid4())
    out = await staging.apply_staged(tid)
    # unstage：UPDATE ... SET staged = false ... WHERE ... staged = true
    flip_sqls = [sql for sql, _ in conn.executed
                 if sql.strip().upper().startswith("UPDATE")]
    assert flip_sqls, "apply 应发 UPDATE"
    assert all("SET staged = false" in s for s in flip_sqls)
    assert set(out.keys()) == set(staging.STAGED_TABLES)


@pytest.mark.asyncio
async def test_revert_staged_restage_flips_back(monkeypatch):
    conn = _patch_staging_conn(monkeypatch)
    await staging.revert_staged(str(uuid.uuid4()), action="restage")
    flip_sqls = [sql for sql, _ in conn.executed
                 if sql.strip().upper().startswith("UPDATE")]
    # restage：UPDATE ... SET staged = true ... WHERE ... staged = false
    assert flip_sqls
    assert all("SET staged = true" in s for s in flip_sqls)


@pytest.mark.asyncio
async def test_revert_staged_delete_deletes_rows(monkeypatch):
    conn = _patch_staging_conn(monkeypatch)
    await staging.revert_staged(str(uuid.uuid4()), action="delete")
    del_sqls = [sql for sql, _ in conn.executed
                if sql.strip().upper().startswith("DELETE")]
    assert del_sqls
    assert all("staged = true" in s for s in del_sqls)


@pytest.mark.asyncio
async def test_revert_staged_rejects_unknown_action(monkeypatch):
    _patch_staging_conn(monkeypatch)
    with pytest.raises(ValueError):
        await staging.revert_staged("x", action="bogus")


@pytest.mark.asyncio
async def test_staging_rejects_empty_task_run_id(monkeypatch):
    # 空 task_run_id 是 staging 的硬护栏（防 DELETE ... WHERE IS NULL 误删全表）
    _patch_staging_conn(monkeypatch)
    with pytest.raises(ValueError):
        await staging.revert_staged("", action="delete")


# ── 计划产物形状（不连库，注入 fake + 无 llm_call）─────────────────────

@pytest.mark.asyncio
async def test_plan_explore_shape_and_persist(monkeypatch):
    doc = _task_doc(status="planning",
                    scope={"purpose": "补需求", "direction": ["api", "web"],
                           "modules": ["login"], "required_env_vars": ["API_KEY"]},
                    env_id=str(uuid.uuid4()),
                    resources={"agent_count": 3, "tokens_limit": 9000})
    calls, _ = _patch_service(monkeypatch, task_doc=doc)

    plan = await orch.plan_explore(doc["id"], env_resolver=_env_resolver({}))

    # 五件齐
    assert set(plan.keys()) >= {"roles", "steps", "dependency_check", "focus", "resources"}
    assert set(plan["roles"]) == {"planner", "executor", "debugger"}
    assert {s["type"] for s in plan["steps"]} == {"api", "ui"}
    assert plan["focus"] == orch.FOCUS_BY_PURPOSE["补需求"]
    assert plan["resources"] == {"agent_count": 3, "tokens_limit": 9000}
    # 缺 API_KEY → dependency_check 记了 blocker
    assert any(b["name"] == "API_KEY" for b in plan["dependency_check"])
    # 方案已落库（update_plan）
    assert calls["plan"] and calls["plan"][-1]["roles"]


@pytest.mark.asyncio
async def test_plan_explore_default_resources(monkeypatch):
    doc = _task_doc(status="planning", env_id=None,
                    scope={"purpose": "补用例", "direction": ["api"]})
    _patch_service(monkeypatch, task_doc=doc)
    plan = await orch.plan_explore(doc["id"], env_resolver=_env_resolver({}))
    assert plan["resources"] == {"agent_count": orch.DEFAULT_AGENT_COUNT,
                                 "tokens_limit": orch.DEFAULT_TOKENS_LIMIT}


@pytest.mark.asyncio
async def test_plan_explore_llm_enrichment(monkeypatch):
    doc = _task_doc(status="planning",
                    scope={"purpose": "探索测试", "direction": ["api"]},
                    env_id=None)
    _patch_service(monkeypatch, task_doc=doc)

    async def fake_llm(_prompt):
        return {"focus": "LLM 定制的关注点",
                "steps": [{"type": "api", "action": "LLM step"}]}

    plan = await orch.plan_explore(doc["id"], llm_call=fake_llm,
                                   env_resolver=_env_resolver({}))
    assert plan["focus"] == "LLM 定制的关注点"
    assert plan["steps"] == [{"type": "api", "action": "LLM step"}]


@pytest.mark.asyncio
async def test_plan_explore_llm_failure_falls_back(monkeypatch):
    doc = _task_doc(status="planning",
                    scope={"purpose": "探索测试", "direction": ["api"]},
                    env_id=None)
    _patch_service(monkeypatch, task_doc=doc)

    async def bad_llm(_prompt):
        raise RuntimeError("llm down")

    # LLM 失败不连坐 → 仍产出确定性方案
    plan = await orch.plan_explore(doc["id"], llm_call=bad_llm,
                                   env_resolver=_env_resolver({}))
    assert plan["roles"]
    assert plan["focus"] == orch.FOCUS_BY_PURPOSE["探索测试"]


@pytest.mark.asyncio
async def test_plan_explore_api_provider_feeds_steps(monkeypatch):
    doc = _task_doc(status="planning", env_id=None,
                    scope={"purpose": "补用例", "direction": ["api"]})
    _patch_service(monkeypatch, task_doc=doc)

    async def fake_api_provider(_scope):
        return [{"id": "api-1", "path": "/login"}]

    plan = await orch.plan_explore(doc["id"], api_provider=fake_api_provider,
                                   env_resolver=_env_resolver({}))
    api_steps = [s for s in plan["steps"] if s["type"] == "api"]
    assert api_steps and api_steps[0]["apis"] == [{"id": "api-1", "path": "/login"}]
