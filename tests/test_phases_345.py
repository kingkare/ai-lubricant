"""阶段 3/4/5 能力面方法测试（工作流断点收口 + 分析师编排 + 版本捕获 + 可靠性）。

覆盖（sqlite::memory:）：

1. **scenario.generate**：非法场景（无断言步骤）→ 校验门拒、不落库；合法场景 → 落库。
2. **bug.triage**：确定性分流（env_flaky/product_bug）+ 非法 kind 拒绝。
3. **run.status / run.result**：pending run 状态 + 结果查询。
4. **case.confirm**：pending_confirmation → active（确认门），非 pending 拒绝、幂等拒。
5. **tool.register**：入 review（不自封 active），缺 impl_ref 拒。
6. **analyst.prepare**：开 analyst 工作流运行 + 组装 agent 任务上下文。
7. **release.capture**：从当前 git/源水位自动采集成版本快照（幂等）。
8. **release.impact**：两版本 diff → 受影响条目/锚点/用例（增量选例）。
9. **reliability.report**：candidate/confirmed 分布 + 待确认清单。

运行：``python -m pytest tests/test_phases_345.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.surface import mcp  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.projects.models",
                "testing_platform.assets.models",
                "testing_platform.knowledge.models",
                "testing_platform.tasks.models",
                "testing_platform.reports.models",
                "testing_platform.workflow.models",
                "testing_platform.defects.models",
                "testing_platform.quality.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ════════════════ 1) scenario.generate（校验门）════════════════

@pytest.mark.asyncio
async def test_scenario_generate_gate(tortoise_db):
    from testing_platform.assets.models import TestScenario, TestScenarioStep

    pid = str(uuid.uuid4())
    owner = str(uuid.uuid4())

    # 非法：空步骤（校验门拒，不落库）
    bad = await mcp.call_tool("scenario.generate", {
        "project_id": pid, "created_by": owner,
        "scenario": {"name": "空场景", "steps": []},
    })
    assert bad["isError"] is False
    assert bad["structuredContent"]["valid"] is False
    assert bad["structuredContent"]["issues"], "应有校验问题"
    assert await TestScenario.filter(project_id=uuid.UUID(pid)).count() == 0

    # 合法：带 code + 断言 + URL 走环境变量（校验门 R1/R12 都过）
    ok = await mcp.call_tool("scenario.generate", {
        "project_id": pid, "created_by": owner,
        "scenario": {
            "code": "SCN-0001",
            "name": "下单主流程",
            "purpose": "验证下单",
            "exec_mode": "script_only",
            "local_vars": {},
            "flow": {},
            "steps": [{
                "step_key": "create_order", "seq": 10, "type": "request",
                "params": {"method": "POST", "url": "{{env.base_url}}/api/orders"},
                "post_checks": [{"type": "status_code", "expected": 201}],
            }],
        },
    })
    assert ok["isError"] is False, ok["structuredContent"]
    body = ok["structuredContent"]
    assert body["valid"] is True and body["id"]
    assert await TestScenario.filter(id=uuid.UUID(body["id"])).exists()
    assert await TestScenarioStep.filter(scenario_id=uuid.UUID(body["id"])).count() == 1


# ════════════════ 2) bug.triage（确定性分流）═══════════════════

@pytest.mark.asyncio
async def test_bug_triage_deterministic(tortoise_db):
    # 重跑即过 → 统一判 env_flaky（确定性优先）
    flaky = await mcp.call_tool("bug.triage", {"failure_kind": "product_bug", "retry_passed": True})
    assert flaky["structuredContent"]["kind"] == "env_flaky"
    assert flaky["structuredContent"]["action"] == "mark_flaky"
    assert flaky["structuredContent"]["counts_as_failure"] is False

    product = await mcp.call_tool("bug.triage", {"failure_kind": "product_bug"})
    assert product["structuredContent"]["action"] == "create_bug"
    assert product["structuredContent"]["counts_as_failure"] is True

    script = await mcp.call_tool("bug.triage", {"failure_kind": "script_issue"})
    assert script["structuredContent"]["action"] == "self_heal"
    assert script["structuredContent"]["counts_as_failure"] is False

    # 带指纹上下文 → 算出指纹
    with_fp = await mcp.call_tool("bug.triage", {
        "failure_kind": "product_bug",
        "run_scenario_id": str(uuid.uuid4()),
        "error_signature": "AssertionError: status 500",
        "step_type": "request",
        "response_diff": {"path": "$.data.id", "expected": 1, "actual": 2},
    })
    assert "fingerprint" in with_fp["structuredContent"]
    assert len(with_fp["structuredContent"]["fingerprint"]) == 64  # sha256

    # 非法 kind → 结构化错误
    bad = await mcp.call_tool("bug.triage", {"failure_kind": "nope"})
    assert bad["isError"] is True


# ════════════════ 3) run.status / run.result ══════════════════

@pytest.mark.asyncio
async def test_run_status_and_result(tortoise_db):
    from testing_platform.tasks.service import create_task, run_task

    pid = str(uuid.uuid4())
    owner = str(uuid.uuid4())
    task = await create_task(project_id=pid, title="冒烟", created_by=owner)
    run = await run_task(task.id, trigger="manual")

    status = await mcp.call_tool("run.status", {"run_id": str(run.id)})
    assert status["isError"] is False
    assert status["structuredContent"]["status"] == "pending"
    assert status["structuredContent"]["code"] == run.code

    result = await mcp.call_tool("run.result", {"run_id": str(run.id)})
    assert result["isError"] is False
    assert result["structuredContent"]["run"]["id"] == str(run.id)
    assert result["structuredContent"]["scenarios"] == []  # 尚无场景结果

    # 未知 run → not_found
    missing = await mcp.call_tool("run.status", {"run_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


# ════════════════ 4) case.confirm（确认门）════════════════════

@pytest.mark.asyncio
async def test_case_confirm_gate(tortoise_db):
    from testing_platform.assets.models import TestCase

    pid = str(uuid.uuid4())
    owner = str(uuid.uuid4())
    case = await TestCase.create(
        project_id=uuid.UUID(pid), code="TC-0001", title="登录",
        intent=None, preconditions={}, expected={},
        confirm_source="doc", confirm_ref={"source_id": "s1"},
        priority=None, automation_status="not_automated",
        status="pending_confirmation", created_by=uuid.UUID(owner),
    )

    ok = await mcp.call_tool("case.confirm", {"case_id": str(case.id), "confirmed_by": owner, "note": "已核对"})
    assert ok["isError"] is False
    assert ok["structuredContent"]["status"] == "active"

    row = await TestCase.get(id=case.id)
    assert row.confirm_ref["confirmed_by"] == owner
    assert row.confirm_ref["confirm_note"] == "已核对"

    # 再确认 → 拒（已不是 pending）
    again = await mcp.call_tool("case.confirm", {"case_id": str(case.id), "confirmed_by": owner})
    assert again["isError"] is True
    assert again["structuredContent"]["code"] == "invalid_request"


# ════════════════ 5) tool.register（走评审）═══════════════════

@pytest.mark.asyncio
async def test_tool_register_review(tortoise_db):
    from testing_platform.assets.models import TestCustomTool

    pid = str(uuid.uuid4())

    # 缺 impl_ref → 拒（design §15.5 必须有实现指向）
    bad = await mcp.call_tool("tool.register", {"project_id": pid, "code": "verify"})
    assert bad["isError"] is True

    ok = await mcp.call_tool("tool.register", {
        "project_id": pid, "code": "verify_amount", "purpose": "下单后校验金额",
        "signature": {"input": {"order_id": "string"}, "output": {"ok": "boolean"}},
        "impl_ref": {"kind": "method", "full_ref": "base.order.verify"},
    })
    assert ok["isError"] is False, ok["structuredContent"]
    assert ok["structuredContent"]["status"] == "review"  # 不自封 active

    row = await TestCustomTool.get(code="verify_amount")
    assert row.status == "review"


# ════════════════ 6) analyst.prepare（阶段 3 编排）═════════════

@pytest.mark.asyncio
async def test_analyst_prepare(tortoise_db):
    from testing_platform.projects import service as psvc
    from testing_platform.workflow.models import TestWorkflowRun

    pid = str(uuid.uuid4())
    proj = await psvc.create_project(name="PRD 项目")
    # B 轨源（feishu）——平台不取内容
    src = await psvc.create_source(
        project_id=proj.id, kind="prd", name="飞书 PRD",
        adapter="agent", adapter_type="feishu",
        config={"doc_url": "https://feishu.cn/x"},
    )

    out = await mcp.call_tool("analyst.prepare", {
        "project_id": str(proj.id), "source_id": str(src.id), "agent": "dsh",
    })
    assert out["isError"] is False, out["structuredContent"]
    task = out["structuredContent"]["task"]
    assert task["role"] == "analyst"
    assert task["run_id"]
    assert task["contract"]["可测闸"]  # 字段契约给 agent
    # B 轨 → 取内容提示（平台不取）
    assert task.get("fetch_error") or out["structuredContent"].get("raw_id") is None

    # 开了 analyst 工作流运行（pending）
    run_row = await TestWorkflowRun.get(id=uuid.UUID(task["run_id"]))
    assert run_row.status == "pending"


# ════════════════ 7) release.capture（阶段 4 采集）═════════════

@pytest.mark.asyncio
async def test_release_capture_and_impact(tortoise_db):
    from testing_platform.projects import service as psvc
    from testing_platform.projects.models import TestProjectRelease

    proj = await psvc.create_project(name="版本项目")
    git = await psvc.create_git(
        project_id=proj.id, name="backend",
        repo_url="https://github.com/x/y.git", ref_value="main",
    )
    src = await psvc.create_source(
        project_id=proj.id, kind="prd", name="PRD", adapter="agent", adapter_type="feishu",
    )

    cap1 = await mcp.call_tool("release.capture", {
        "project_id": str(proj.id), "version_code": "v1.0",
    })
    assert cap1["isError"] is False
    r1 = cap1["structuredContent"]
    assert r1["git_tags"] == [{"git_id": str(git.id), "ref": "main", "repo": "backend"}]
    assert any(p["source_id"] == str(src.id) for p in r1["prd_versions"])

    # 幂等：同版本再采集 → 返回同一行
    cap1b = await mcp.call_tool("release.capture", {
        "project_id": str(proj.id), "version_code": "v1.0",
    })
    assert cap1b["structuredContent"]["id"] == r1["id"]

    # 换个 ref → 采集第二版
    await psvc.update_git(git.id, ref_value="release/2.0")
    cap2 = await mcp.call_tool("release.capture", {
        "project_id": str(proj.id), "version_code": "v1.1",
    })
    r2 = cap2["structuredContent"]

    # impact：v1.0 → v1.1，git 变了
    impact = await mcp.call_tool("release.impact", {
        "base_release_id": r1["id"], "head_release_id": r2["id"],
    })
    assert impact["isError"] is False
    diff = impact["structuredContent"]["diff"]
    assert diff["git_tags"]["changed_ids"] == [str(git.id)]
    assert str(git.id) in impact["structuredContent"]["impact"]["changed_source_ids"]


# ════════════════ 8) reliability.report（阶段 5）═══════════════

@pytest.mark.asyncio
async def test_reliability_report(tortoise_db):
    from testing_platform.knowledge import agent_ingest as ai
    from testing_platform.knowledge.models import KnowledgeAnchor

    pid = str(uuid.uuid4())
    anchor = await KnowledgeAnchor.create(
        project_id=uuid.UUID(pid), kind="function", path="api/auth.py", symbol="login"
    )
    # agent 产出（candidate）+ 确定性产出（confirmed）
    await ai.put_items(project_id=pid, items=[{
        "kind": "rule", "key": "login-success",
        "content": {"condition": "密码正确", "expected": "跳首页"},
        "confidence": 0.9,
        "anchors": [{"path": "api/auth.py", "symbol": "login"}],
    }], agent="dsh")

    from testing_platform.knowledge.models import KnowledgeItem
    await KnowledgeItem.create(
        project_id=uuid.UUID(pid), kind="component", key="auth-service",
        content={"name": "AuthService"}, status="confirmed", confidence=1.0,
        provenance={"extractor": "graphify"}, anchor_id=anchor.id,
    )

    out = await mcp.call_tool("reliability.report", {"project_id": pid})
    assert out["isError"] is False
    summary = out["structuredContent"]["summary"]
    assert summary["items_total"] == 2
    assert summary["confirmed"] == 1
    assert summary["candidate"] == 1
    assert summary["deterministic"] == 1
    assert summary["agent_produced"] == 1
    assert summary["anchors"] == 1

    # 待确认清单：agent 产出的 candidate
    cands = out["structuredContent"]["candidates"]
    assert len(cands) == 1
    assert cands[0]["key"] == "login-success"
    assert cands[0]["confidence"] == 0.9
