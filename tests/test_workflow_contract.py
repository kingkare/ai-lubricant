"""工作流契约校验测试（design §15.9 + §17.3）。

纯测试（sqlite::memory:），**不依赖 PG/网络**：

* 7 条内置定义齐全、executor 是 ``deterministic``、其余 6 个是 ``llm``。
* ``validate_output``：coder 产出含已注册工具 → valid；含未知工具 → invalid
  （规则 ``A4_fabricated_ref``）；designer 产出 case 无 ``confirm_source`` → invalid。
* ``complete_run``：output 过契约 → done；不过 → failed + issues 记录。
* ``mark_blocked`` → ``blocked_by_question``。
* 复用 :mod:`testing_platform.quality.errors` 的 ``ValidationResult`` / ``ValidationIssue``。
* 复用 :mod:`testing_platform.surface.methods` 的 ``_type_ok``（JSON Schema 子集类型判断）。

运行：``python -m pytest tests/test_workflow_contract.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid as _uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)


@pytest_asyncio.fixture
async def tortoise_db():
    """Tortoise sqlite::memory:，app label 用 ``workflow``。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.workflow.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 7 条内置定义齐全、executor 是 deterministic ────────────────────
def test_builtin_defs_complete(tortoise_db):
    from testing_platform.workflow import (
        ROLES,
        WORKFLOW_NATURES,
        builtin_workflow_def,
        builtin_workflow_defs,
    )

    defs = builtin_workflow_defs()
    assert len(defs) == 7
    roles = [d["role"] for d in defs]
    assert roles == list(ROLES)

    # executor 是 deterministic，其余 6 个是 llm
    for role in ROLES:
        spec = builtin_workflow_def(role)
        assert spec is not None
        if role == "executor":
            assert spec["nature"] == "deterministic"
        else:
            assert spec["nature"] == "llm"

    # tool_refs 存在
    assert set(builtin_workflow_def("planner")["tool_refs"]) == {"knowledge.query", "craft.load"}
    assert set(builtin_workflow_def("analyst")["tool_refs"]) == {"knowledge.ingest", "question.raise"}
    assert set(builtin_workflow_def("designer")["tool_refs"]) == {"case.create", "question.raise"}
    assert set(builtin_workflow_def("coder")["tool_refs"]) == {"scenario.generate", "tool.list", "craft.load"}
    assert set(builtin_workflow_def("executor")["tool_refs"]) == {"run.execute"}
    assert set(builtin_workflow_def("debugger")["tool_refs"]) == {"bug.triage", "bug.root_cause"}
    assert set(builtin_workflow_def("reporter")["tool_refs"]) == {"report.generate", "craft.load"}


# ── validate_output：结构校验 ─────────────────────────────────────
def test_validate_output_schema_valid(tortoise_db):
    from testing_platform.workflow import validate_output

    # planner 产出合法
    res = validate_output("planner", {
        "plan": {
            "summary": "验证下单闭环",
            "objectives": ["创建订单", "查询订单"],
            "scope": {"include": ["checkout"]},
        },
    })
    assert res.valid
    assert len(res.errors) == 0

    # coder 产出合法（步骤有 tool_ref，且指向已注册工具）
    res = validate_output("coder", {
        "scenarios": [
            {
                "name": "下单主流程",
                "steps": [
                    {"step_key": "create_order", "tool_ref": "http"},
                    {"step_key": "verify_order", "tool_ref": "http"},
                ],
            },
        ],
    })
    assert res.valid


# ── validate_output：coder 硬门（A4 凭空造工具）──────────────────────
def test_validate_output_coder_unknown_tool_invalid(tortoise_db):
    from testing_platform.workflow import validate_output

    # coder 产出含未知工具 → invalid（规则 A4_fabricated_ref）
    res = validate_output("coder", {
        "scenarios": [
            {
                "name": "下单主流程",
                "steps": [
                    {"step_key": "create_order", "tool_ref": "http"},
                    {"step_key": "fake_step", "tool_ref": "fabricated_tool_xyz"},  # 不存在
                ],
            },
        ],
    })
    assert not res.valid
    assert any(i.rule == "A4_fabricated_ref" for i in res.errors)
    assert any("fabricated_tool_xyz" in i.msg for i in res.errors)


def test_validate_output_coder_custom_prefix_valid(tortoise_db):
    from testing_platform.workflow import CUSTOM_TOOL_PREFIX, validate_output

    # custom: 前缀允许（框架自定义工具白名单）
    res = validate_output("coder", {
        "scenarios": [
            {
                "name": "下单主流程",
                "steps": [
                    {"step_key": "custom_check", "tool_ref": f"{CUSTOM_TOOL_PREFIX}mycheck"},
                ],
            },
        ],
    })
    assert res.valid


# ── validate_output：designer 硬门（确认门）──────────────────────────
def test_validate_output_designer_missing_confirm_source_invalid(tortoise_db):
    from testing_platform.workflow import validate_output

    # designer 产出 case 无 confirm_source → invalid
    res = validate_output("designer", {
        "design": {},
        "cases": [
            {
                "title": "下单主流程",
                "intent": "验证下单闭环",
                # 缺少 confirm_source
            },
        ],
    })
    assert not res.valid
    assert any("confirm_source" in i.msg for i in res.errors)


def test_validate_output_designer_valid_confirm_source(tortoise_db):
    from testing_platform.workflow import CONFIRM_SOURCES, validate_output

    for source in CONFIRM_SOURCES:
        res = validate_output("designer", {
            "design": {},
            "cases": [
                {
                    "title": "下单主流程",
                    "intent": "验证下单闭环",
                    "confirm_source": source,
                },
            ],
        })
        assert res.valid, f"confirm_source={source!r} 应合法"


# ── complete_run：过契约 → done，不过 → failed + issues ───────────────
@pytest.mark.asyncio
async def test_complete_run_passes_contract_done(tortoise_db):
    from testing_platform.workflow import start_run, complete_run, ensure_builtin_defs

    await ensure_builtin_defs()

    # 开一条 coder 运行
    run = await start_run("coder", {"design": {"test_plan": "..."}})
    assert run.status == "pending"

    # 产出合法（工具已注册）
    out = {
        "scenarios": [
            {
                "name": "下单主流程",
                "steps": [
                    {"step_key": "create_order", "tool_ref": "http"},
                    {"step_key": "verify_order", "tool_ref": "http"},
                ],
            },
        ],
    }
    run = await complete_run(run.id, out)
    assert run.status == "done"
    assert run.output == out


@pytest.mark.asyncio
async def test_complete_run_fails_contract_failed_with_issues(tortoise_db):
    from testing_platform.workflow import start_run, complete_run, ensure_builtin_defs

    await ensure_builtin_defs()

    run = await start_run("coder", {"design": {"test_plan": "..."}})
    assert run.status == "pending"

    # 产出非法（未知工具）
    out = {
        "scenarios": [
            {
                "name": "下单主流程",
                "steps": [
                    {"step_key": "fake", "tool_ref": "unknown_tool_xyz"},
                ],
            },
        ],
    }
    run = await complete_run(run.id, out)
    assert run.status == "failed"
    assert "issues" in run.output
    assert len(run.output["issues"]) > 0
    assert any(i.get("rule") == "A4_fabricated_ref" for i in run.output["issues"])


# ── mark_blocked → blocked_by_question ──────────────────────────────
@pytest.mark.asyncio
async def test_mark_blocked_status(tortoise_db):
    from testing_platform.workflow import start_run, mark_blocked, ensure_builtin_defs

    await ensure_builtin_defs()

    run = await start_run("designer", {"knowledge_model": [], "gaps": []})
    assert run.status == "pending"

    qid = _uuid.uuid4()
    run = await mark_blocked(run.id, qid)
    assert run.status == "blocked_by_question"
    assert run.output.get("blocked_by_question") == str(qid)


# ── get_run / list_runs ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_run_and_list_runs(tortoise_db):
    from testing_platform.workflow import (
        start_run,
        get_run,
        list_runs,
        WorkflowRunNotFoundError,
        ensure_builtin_defs,
    )

    await ensure_builtin_defs()

    run1 = await start_run("planner", {"goal": "x", "knowledge": []})
    run2 = await start_run("analyst", {"raw_knowledge": []})

    got = await get_run(run1.id)
    assert got.id == run1.id
    assert got.workflow_def_id == run1.workflow_def_id

    runs = await list_runs(limit=10)
    ids = {str(r.id) for r in runs}
    assert str(run1.id) in ids
    assert str(run2.id) in ids

    with pytest.raises(WorkflowRunNotFoundError):
        await get_run(_uuid.uuid4())


# ── 表确实落到 schema（sqlite_master 实证）───────────────────────────
@pytest.mark.asyncio
async def test_workflow_tables_registered(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {"mc_test_workflow_def", "mc_test_workflow_run"}
    assert expected <= names, f"缺表：{expected - names}"
