"""能力面（surface）测试 —— design §17 工具方法 + MCP + HTTP 骨架。

覆盖（纯测试，sqlite::memory:，模式照 ``tests/test_assets_model.py``）：

1. 注册表**只**暴露已实现的方法；未实现的（``scenario.validate``——校验器
   以 ``script.validate`` 暴露，不另开名字）**不在**表里
   （写类方法已实现，见 test_surface_writes.py）。
2. ``mcp.list_tools()`` 产出的工具定义符合 MCP 形态（name/description/inputSchema）。
3. ``mcp.call_tool("script.validate", …)`` 分发正确、返回结构化校验结果。
4. ``mcp.call_tool("case.update", …)`` 缺 ``change_reason`` → **结构化错误**（变更门，不抛穿）。
5. ``mcp.call_tool`` 未知方法 → 结构化错误。
6. ``knowledge.ingest`` 只测「注册存在 + 参数校验」——**不真跑** Graphify
   （子进程慢且依赖隔离 venv）。
7. HTTP 路由骨架的两种形态（且未被挂载到 main）。
8. 查询类 list/get 方法（report/run/case/scenario/task/bug/knowledge）：
   空库返回空集、建数据后返回正确文档、缺失 id → not_found。

运行：``python -m pytest tests/test_surface_methods.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.surface import mcp, methods  # noqa: E402
from testing_platform.surface.methods import (  # noqa: E402
    METHOD_KINDS,
    MethodError,
    MethodNotFoundError,
    MethodParamError,
    MethodRegistry,
)

#: 已实现、应当注册的方法（= 能力面当前真实边界，多一个都算发明）。
#: 写类方法的注册与真跑覆盖在 ``tests/test_surface_writes.py``。
IMPLEMENTED = {
    "knowledge.query",
    "knowledge.list",
    "knowledge.ingest",
    "script.validate",
    "case.create",
    "case.update",
    "case.list",
    "case.get",
    "case.link_scenario",
    "case.unlink_scenario",
    "case.tag",
    "case.scenarios",
    "case.tags",
    "scenario.create",
    "scenario.update",
    "scenario.create_steps",
    "scenario.get",
    "scenario.list",
    "run.execute",
    "run.list",
    "run.get",
    "run.scenarios",
    "run.steps",
    "task.create",
    "task.update",
    "task.enable",
    "task.disable",
    "task.run",
    "task.list",
    "tag.create",
    "tag.list",
    "bug.create",
    "bug.get",
    "bug.root_cause",
    "bug.list",
    # 缺陷域禅道对齐（plans/23-defect-zentao-parity.md）：状态机 + 附加字段 + 报表
    "bug.update",
    "bug.transition",
    "bug.history",
    "bug.enums",
    "bug.cycle_report",
    "bug.delete",
    "bug.available_actions",
    "report.generate",
    "report.list",
    "report.get",
    "workflow.start",
    "workflow.complete",
    "workflow.get",
    "tool.list",
    "transport.import_openapi",
    "transport.import_from_repo",
    "transport.list",
    "transport.create",
    "transport.get",
    "transport.update",
    "transport.delete",
    # 接口管理（mc_test_api，Apifox 式一等实体；身份=id，连接配置归环境）
    "api.list",
    "api.get",
    "api.changes",
    "api.create",
    "api.update",
    "api.move",
    "api.delete",
    "api_folder.list",
    "api_folder.create",
    "api_folder.update",
    "api_folder.delete",
    # 统一目录（mc_test_catalog：需求/用例/场景/接口共用一张表，type 区分）
    "catalog.list",
    "catalog.create",
    "catalog.update",
    "catalog.delete",
    "api.import_openapi",
    # 接口来源（绑定的导入方式；一接口多来源；可同步）
    "api.source.list",
    "api.source.sync",
    "api.source.delete",
    # 接口↔用例/场景 直连（P5）
    "api.case.link",
    "api.case.unlink",
    "api.cases",
    "api.scenario.link",
    "api.scenario.unlink",
    "api.scenarios",
    # 接口导入任务（mc_test_api_import_job：OpenAPI/Swagger/Postman/HAR 异步导入）
    "api.import_job.create",
    "api.import_job.get",
    "api.import_job.list",
    "api.import_job.run",
    "api.import_job.update",
    "api.import_job.delete",
    # 测试策略（mc_test_strategy：按标签/用例集合组织执行计划）
    "strategy.create",
    "strategy.get",
    "strategy.list",
    "strategy.update",
    "strategy.delete",
    "strategy.set_cases",
    "strategy.assign_case",
    "strategy.remove_case",
    "strategy.cases",
    "strategy.record_result",
    "method_module.create",
    "method_module.update",
    "method_module.delete",
    "method_module.get",
    "method_module.list",
    "method_module.classes",
    "method_module.parse",
    "method_module.changes",
    "method.create",
    "method.list",
    "dataset.list",
    "dataset.create",
    "dataset.update",
    "dataset.delete",
    # 环境管理（design §15.4 / 05：分层继承 + 7 类型变量 + 密钥脱敏）
    "env.list",
    "env.create",
    "env.update",
    "env.delete",
    "env.var.create",
    "env.var.update",
    "env.var.delete",
    "env.var.list",
    "env.vars.resolve",
    # 疑问（design §15.2：流程一等公民）
    "question.raise",
    "question.resolve",
    "question.list",
    # 自定义工具（design §15.5：只拥有签名 + 指向）
    "custom_tool.list",
    "custom_tool.create",
    "custom_tool.update",
    # 沉淀四件套（design §15.6）
    "craft.list",
    "craft.create",
    "craft.load",
    # 测试项目域（test-project-definition.md §十：项目/git/源/release/图谱）
    "project.create",
    "project.list",
    "project.get",
    "project.update",
    "project.archive",
    "project.git.create",
    "project.git.list",
    "project.git.update",
    "project.git.delete",
    "project.source.create",
    "project.source.list",
    "project.source.update",
    "project.source.delete",
    "project.code.init",
    "project.code.sync",
    "project.release.create",
    "project.release.list",
    "project.release.diff",
    "project.graph.query",
    # 版本一等实体（test-project-definition.md §十一）：仓库分支多选 + 图谱按版本
    "project.version.create",
    "project.version.list",
    "project.version.get",
    "project.version.update",
    "project.version.delete",
    "project.version.build_graph",
    "project.version.diff",
    # 版本级角色成员（testers/developers/product_managers，用户定版 2026-10-15）
    "project.version.roles",
    # 阶段 2：双轨数据源 + 用例扫描（test-project-definition.md §八/§九）
    "project.code.graph",
    "source.fetch",
    "source.list_raw",
    "source.adapters",
    "case.scan",
    "case.generate",
    "knowledge.put",
    # 阶段 3/4/5：工作流断点收口 + 分析师编排 + 版本捕获/影响面 + 可靠性
    "scenario.generate",
    "bug.triage",
    "run.status",
    "run.result",
    "case.confirm",
    "knowledge.profile",
    "tool.register",
    "analyst.prepare",
    "analyst.commit",
    "release.capture",
    "release.impact",
    "reliability.report",
    # 需求主脊（test-project-definition.md §十二）：需求实体 + 产出聚合 + 径向分层图
    "requirement.create",
    "requirement.list",
    "requirement.get",
    "requirement.update",
    "requirement.delete",
    "requirement.trace",
    # 需求附件（需求 = 文档：附件走 OSS 存 URL）
    "requirement.attachment.list",
    "requirement.attachment.add",
    "requirement.attachment.delete",
    "project.radial_graph",
    # 测试设计 L1/L2（机械枚举 + 覆盖矩阵）
    "case.enumerate",
    "coverage.report",
    # 接口反向引用（沿步骤 params.api_id 派生关联索引）
    "case.apis",
    "scenario.apis",
    # 远端分支列举（版本表单分支下拉，修 main/master 报错）
    "project.git.branches",
    # AI 读仓库生成接口（替代确定性扫描）
    "transport.generate_from_repo",
    # 策略统计报告（并发改动补账本：strategy.report 已注册于 tests/test_surface_writes.py EXPECTED_TOTAL）
    "strategy.report",
    # 生成环节门①②/进度（25 号文 v4.1；surface/gen_methods.py）
    "testpoint.confirm",
    "proposal.list",
    "genrun.get",
    # 用例细化/门②判决/入库/覆盖统计（25 号文 P1；surface/gen_methods.py）
    "testpoint.list",
    "case.verdict",
    "case.import",
    "coverage.summary",
    # 缺陷链方法层（25 号文 v4.1 §环节9/§环节11；P2；surface/gen_methods.py 尾部）
    "case.reject_sink",  # 门② REJECT 原因回流到 mc_test_question（统一 sink）
    "case.mark_golden",  # 风格锚标记（confirm_ref.golden，不新增列）
    "question.sink",     # 问题库回流通用沉淀（四写入者批量→mc_test_question）
    # P2：探索门（§环节7）+ 风格锚召回 + 上线判定门 + 报告受众 + run 波次编排
    "explore.plan",      # 探索门判定（按内容分，不按端）
    "explore.record",    # verified_by_run 三态落列
    "release.judge",     # 上线判定双条件门（共识∧置信≥0.9∧回滚两栏）
    "report.view",       # 报告四受众视图（结论先行）
    "genrun.list",       # 生成任务历史（独立列表页）
    "run.execute_waves", # run 级波次编排执行（§环节8 waves 接线）
}

#: design §17.1 列了但对应模块未建/未接线 —— 必须**不**注册。
#: 阶段 3/4/5 已把 knowledge.profile / case.confirm / scenario.generate /
#: tool.register / run.status / run.result / bug.triage 全部实现并注册，
#: 只剩 scenario.validate（校验器以 script.validate 暴露，不另开名字）。
NOT_IMPLEMENTED = {
    "scenario.validate",
}


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，注册能力面跨域读写所需模型。

    覆盖知识 / 资产 / 任务与运行 / 报告 / 缺陷扩展域 + 平台 ``ProjectIssue``
    （``bug.list`` 读侧组合 ``mc_project_issues``）。app label 统一
    ``testing_platform``（与各模型 ``Meta.app`` 一致）。
    """
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.projects.models",
                "testing_platform.assets.models",
                "testing_platform.knowledge.models",
                "testing_platform.quality.models",
                "testing_platform.tasks.models",
                "testing_platform.reports.models",
                "testing_platform.defects.models",
            ],
            "user_platform": [
                "user_platform.models_project",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 1) 注册表边界：只暴露已实现的，未实现的一律不在 ──────────────
def test_registry_exposes_only_implemented_methods():
    names = set(methods.default_method_registry().names())
    assert names == IMPLEMENTED, f"注册表与实际能力边界不符：多={names - IMPLEMENTED} 少={IMPLEMENTED - names}"

    for missing in NOT_IMPLEMENTED:
        assert missing not in names, f"未实现的方法被发明了：{missing}"
    assert not methods.has_method("scenario.validate")


def test_every_method_declares_its_contract():
    for spec in methods.list_methods():
        assert spec.kind in METHOD_KINDS, f"{spec.name} kind 非法：{spec.kind}"
        assert isinstance(spec.params_schema, dict) and spec.params_schema.get("type") == "object"
        assert isinstance(spec.returns_schema, dict)
        assert callable(spec.handler)
        assert spec.description, f"{spec.name} 缺描述"


def test_method_kinds_match_design_seventeen():
    expected = {
        "knowledge.query": "read",
        "knowledge.ingest": "write",
        "script.validate": "read",
        "case.create": "write",
        "case.update": "write",
        "scenario.create": "write",
        "scenario.get": "read",
        "run.execute": "execute",
        "tool.list": "read",
    }
    assert {name: methods.get_method(name).kind for name, _ in expected.items()} == expected


def test_duplicate_registration_is_rejected():
    reg = MethodRegistry()

    @reg.register("a.one", kind="read")
    async def _one(params):
        return {"ok": True}

    with pytest.raises(ValueError):
        @reg.register("a.one", kind="read")
        async def _dup(params):
            return {}


@pytest.mark.asyncio
async def test_invoke_rejects_unknown_and_mistyped_params():
    reg = MethodRegistry()

    @reg.register(
        "x.echo",
        kind="read",
        params_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
            "required": ["text"],
        },
    )
    async def _echo(params):
        return {"text": params["text"] * int(params.get("times", 1))}

    assert (await reg.invoke("x.echo", {"text": "ab", "times": 2}))["text"] == "abab"

    with pytest.raises(MethodNotFoundError):
        await reg.invoke("x.nope", {})
    with pytest.raises(MethodParamError) as missing:
        await reg.invoke("x.echo", {})
    assert missing.value.code == "invalid_params"
    assert missing.value.details["missing"] == ["text"]
    with pytest.raises(MethodParamError):
        await reg.invoke("x.echo", {"text": "a", "times": "many"})
    with pytest.raises(MethodParamError) as extra:
        await reg.invoke("x.echo", {"text": "a", "typo_field": 1})
    assert extra.value.details["unknown"] == ["typo_field"]


@pytest.mark.asyncio
async def test_invoke_translates_domain_errors_to_method_errors():
    """业务异常（如变更门）在注册表里归一成 MethodError，协议层无需认识各域类型。"""
    reg = MethodRegistry()

    @reg.register("x.gate", kind="write")
    async def _gate(params):
        from testing_platform.assets import ChangeReasonRequiredError

        raise ChangeReasonRequiredError("change_reason 必填")

    with pytest.raises(MethodError) as excinfo:
        await reg.invoke("x.gate", {})
    assert excinfo.value.code == "change_reason_required"


# ── 2) MCP 工具面形态 ───────────────────────────────────────────
def test_list_tools_has_mcp_tool_shape():
    tools = mcp.list_tools()
    assert len(tools) == len(IMPLEMENTED)
    for tool in tools:
        assert set(tool) >= {"name", "description", "inputSchema"}
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str) and tool["description"]
        schema = tool["inputSchema"]
        assert isinstance(schema, dict)
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties"), dict)
        required = schema.get("required")
        if required is not None:
            assert isinstance(required, list) and set(required) <= set(schema["properties"])


def test_list_tools_carries_kind_annotation():
    by_name = {t["name"]: t for t in mcp.list_tools()}
    assert by_name["run.execute"]["annotations"]["kind"] == "execute"
    assert by_name["knowledge.query"]["inputSchema"]["required"] == ["project_id"]
    assert by_name["tool.list"]["inputSchema"]["type"] == "object"


def test_list_tools_names_are_exactly_the_registry():
    assert {t["name"] for t in mcp.list_tools()} == set(methods.default_method_registry().names())
    assert "scenario.validate" not in {t["name"] for t in mcp.list_tools()}


# ── 3) call_tool 分发 ──────────────────────────────────────────
GOOD_SCENARIO = {
    "code": "SCN-0001",
    "name": "下单主流程",
    "local_vars": {"order_id": None},
    "flow": {},
    "steps": [
        {
            "step_key": "create_order",
            "seq": 10,
            "type": "request",
            "params": {"method": "POST", "url": "{{env.base_url}}/api/v1/orders"},
            "post_checks": [{"type": "status_code", "expected": 201}],
            "post_cond": ["{{local.order_id}} = {{response.body.data.order_id}}"],
        },
        {
            "step_key": "verify_order",
            "seq": 20,
            "type": "request",
            "params": {"method": "GET", "url": "{{env.base_url}}/api/v1/orders/{{local.order_id}}"},
            "post_checks": [{"type": "status_code", "expected": 200}],
        },
    ],
}

BAD_SCENARIO = {
    "code": "SCN-BAD",
    "name": "缺后置校验",
    "steps": [
        {
            "step_key": "hit",
            "seq": 10,
            "type": "request",
            "params": {"method": "GET", "url": "https://hardcoded.example/x"},
        }
    ],
}


@pytest.mark.asyncio
async def test_call_tool_script_validate_returns_structured_result():
    ok = await mcp.call_tool("script.validate", {"scenario": GOOD_SCENARIO})
    assert ok["isError"] is False
    body = ok["structuredContent"]
    assert body["valid"] is True, body["issues"]
    assert body["issues"] == []

    bad = await mcp.call_tool("script.validate", {"scenario": BAD_SCENARIO})
    assert bad["isError"] is False  # 校验不过 ≠ 调用失败（门禁结论在 result 里）
    issues = bad["structuredContent"]["issues"]
    assert bad["structuredContent"]["valid"] is False
    rules = {i["rule"] for i in issues}
    assert "R5_post_checks_required" in rules
    assert "R12_no_hardcoded_env" in rules
    for issue in issues:
        assert set(issue) == {"path", "rule", "msg", "severity", "hint"}


@pytest.mark.asyncio
async def test_call_tool_script_validate_rejects_missing_scenario():
    out = await mcp.call_tool("script.validate", {})
    assert out["isError"] is True
    assert out["structuredContent"]["code"] == "invalid_params"
    assert "scenario" in out["structuredContent"]["details"]["missing"]


@pytest.mark.asyncio
async def test_call_tool_script_validate_method_whitelist():
    """给出 methods 白名单后，未登记的 method_ref 触发规则 8（框架裁判）。"""
    scenario = {
        "code": "SCN-M",
        "name": "调不存在的方法",
        "steps": [
            {
                "step_key": "sign",
                "seq": 10,
                "type": "script",
                "method_ref": "base.crypto.nope",
                "params": {},
                "post_checks": [{"type": "status_code", "expected": 200}],
            }
        ],
    }
    with_ref = await mcp.call_tool(
        "script.validate", {"scenario": scenario, "methods": ["base.crypto.sign"]}
    )
    assert with_ref["structuredContent"]["valid"] is False
    assert "R8_method_ref_exists" in {i["rule"] for i in with_ref["structuredContent"]["issues"]}

    without_ref = await mcp.call_tool("script.validate", {"scenario": scenario})
    assert "R8_method_ref_exists" not in {
        i["rule"] for i in without_ref["structuredContent"]["issues"]
    }


@pytest.mark.asyncio
async def test_call_tool_tool_list():
    out = await mcp.call_tool("tool.list", {})
    assert out["isError"] is False
    names = {t["name"] for t in out["structuredContent"]["tools"]}
    assert {"http", "wait", "assert"} <= names


# ── 4) 变更门：case.update 缺 change_reason → 结构化错误，不抛穿 ──
@pytest.mark.asyncio
async def test_call_tool_case_update_without_change_reason_is_structured_error(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    created = await mcp.call_tool(
        "case.create",
        {
            "project_id": pid,
            "title": "用户下单全流程",
            "confirm_source": "user_confirm",
            "created_by": me,
            "expected": {"status": 201},
        },
    )
    assert created["isError"] is False
    case_id = created["structuredContent"]["id"]
    assert created["structuredContent"]["code"] == "TC-0001"

    # 缺 change_reason —— 变更门拦截，且**不抛穿**协议面
    blocked = await mcp.call_tool("case.update", {"case_id": case_id, "title": "改标题"})
    assert blocked["isError"] is True
    err = blocked["structuredContent"]
    assert err["code"] == "change_reason_required"
    assert "change_reason" in err["message"]

    # 带 reason —— 通过，version 递增
    allowed = await mcp.call_tool(
        "case.update",
        {"case_id": case_id, "change_reason": "口径按业务文档纠正", "title": "新标题"},
    )
    assert allowed["isError"] is False
    assert allowed["structuredContent"]["version"] == 2
    assert allowed["structuredContent"]["title"] == "新标题"

    # 未知用例 —— not_found 结构化错误
    missing = await mcp.call_tool(
        "case.update", {"case_id": str(uuid.uuid4()), "change_reason": "试试", "title": "x"}
    )
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_call_tool_case_create_enums_are_enforced(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    bad = await mcp.call_tool(
        "case.create",
        {"project_id": pid, "title": "T", "confirm_source": "guesswork", "created_by": me},
    )
    assert bad["isError"] is True
    assert bad["structuredContent"]["code"] == "invalid_params"
    assert bad["structuredContent"]["details"]["field"] == "confirm_source"


@pytest.mark.asyncio
async def test_scenario_create_then_get_roundtrip(tortoise_db):
    pid = str(uuid.uuid4())
    made = await mcp.call_tool(
        "scenario.create",
        {
            "project_id": pid,
            "name": "下单主流程",
            "local_vars": {"order_id": None},
            "steps": [
                {
                    "step_key": "create_order",
                    "seq": 10,
                    "type": "request",
                    "params": {"method": "POST", "url": "{{env.base_url}}/orders"},
                    "post_checks": [{"type": "status_code", "expected": 201}],
                }
            ],
        },
    )
    assert made["isError"] is False
    doc = made["structuredContent"]
    assert doc["code"] == "SCN-0001"
    assert [s["step_key"] for s in doc["steps"]] == ["create_order"]

    fetched = await mcp.call_tool("scenario.get", {"scenario_id": doc["code"]})
    assert fetched["isError"] is False
    assert fetched["structuredContent"]["id"] == doc["id"]

    absent = await mcp.call_tool("scenario.get", {"scenario_id": "SCN-9999"})
    assert absent["isError"] is True
    assert absent["structuredContent"]["code"] == "not_found"


# ── 5) 未知方法 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_call_tool_unknown_method_is_structured_error():
    out = await mcp.call_tool("scenario.validate", {"content": "这里该返回什么？"})
    assert out["isError"] is True
    err = out["structuredContent"]
    assert err["code"] == "method_not_found"
    assert "scenario.validate" in err["message"]
    assert "script.validate" in err["message"]  # 报错里带上已注册清单，便于 agent 自救


@pytest.mark.asyncio
async def test_call_tool_content_block_is_json_text():
    out = await mcp.call_tool("tool.list", {})
    block = out["content"][0]
    assert block["type"] == "text"
    import json

    assert json.loads(block["text"])["count"] == out["structuredContent"]["count"]


# ── 6) knowledge：query 读写 + ingest 只测注册与参数校验 ────────
@pytest.mark.asyncio
async def test_knowledge_query_by_kind_and_anchor(tortoise_db):
    from testing_platform.knowledge.models import KnowledgeAnchor, KnowledgeItem

    pid = uuid.uuid4()
    anchor = await KnowledgeAnchor.create(
        id=uuid.uuid4(), project_id=pid, kind="function", path="svc/pay.py", symbol="refund"
    )
    await KnowledgeItem.create(
        id=uuid.uuid4(),
        project_id=pid,
        kind="rationale",
        key="svc/pay.py::refund",
        title="退款需幂等",
        content={"statement": "退款需幂等"},
        status="confirmed",
        anchor_id=anchor.id,
    )
    await KnowledgeItem.create(
        id=uuid.uuid4(),
        project_id=pid,
        kind="rule",
        key="RULE-77",
        title="对账规则",
        content={"rule": "T+1 对账"},
        status="candidate",
    )

    by_kind = await mcp.call_tool("knowledge.query", {"project_id": str(pid), "kind": "rule"})
    assert by_kind["isError"] is False
    assert [i["key"] for i in by_kind["structuredContent"]["items"]] == ["RULE-77"]

    by_anchor = await mcp.call_tool(
        "knowledge.query",
        {"project_id": str(pid), "anchor": {"path": "svc/pay.py", "symbol": "refund"}},
    )
    items = by_anchor["structuredContent"]["items"]
    assert [i["key"] for i in items] == ["svc/pay.py::refund"]
    assert items[0]["anchor_id"] == str(anchor.id)

    none = await mcp.call_tool(
        "knowledge.query", {"project_id": str(pid), "anchor": {"path": "nope.py"}}
    )
    assert none["structuredContent"] == {"items": [], "count": 0}

    bad = await mcp.call_tool("knowledge.query", {"project_id": "not-a-uuid"})
    assert bad["isError"] is True
    assert bad["structuredContent"]["code"] == "invalid_params"


@pytest.mark.asyncio
async def test_knowledge_ingest_is_registered_and_validates_params():
    """**不真跑** Graphify（子进程 + 隔离 venv，慢且环境相关）：只验注册与参数校验。"""
    spec = methods.get_method("knowledge.ingest")
    assert spec.kind == "write"
    assert set(spec.params_schema["required"]) == {"project_id", "project_path"}

    missing = await mcp.call_tool("knowledge.ingest", {"project_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "invalid_params"
    assert "project_path" in missing["structuredContent"]["details"]["missing"]

    typo = await mcp.call_tool(
        "knowledge.ingest", {"project_id": "p", "project_path": "/tmp/x", "dry_rn": True}
    )
    assert typo["isError"] is True
    assert typo["structuredContent"]["details"]["unknown"] == ["dry_rn"]


@pytest.mark.asyncio
async def test_run_execute_scenario_missing_is_structured_error(tortoise_db):
    """run.execute 走引擎：场景不存在时引擎不抛穿，产出 status=error 的结果。"""
    out = await mcp.call_tool("run.execute", {"scenario_id": "SCN-404"})
    assert out["isError"] is False
    body = out["structuredContent"]
    assert body["status"] == "error"
    assert body["error"]["kind"] == "scenario_not_found"


# ── 7) HTTP 路由骨架：两条路由存在，且未被挂载 ──────────────────
def test_api_router_shape_without_mounting():
    from testing_platform.surface import api

    paths = {r.path for r in api.router.routes}
    # 方法调用入口 + 工具列表 + MCP server（POST JSON-RPC + GET 自述）
    # + 对话后端（send + health + sessions 列表/详情）+ 源同步触发（sync/webhook/sync-due）
    # + 需求文档导入（multipart：docx/xmind）
    assert paths == {
        "/api/v1/testing/methods/{method_name}",
        "/api/v1/testing/tools",
        "/api/v1/testing/mcp",
        "/api/v1/testing/chat/send",
        "/api/v1/testing/chat/health",
        "/api/v1/testing/chat/sessions",
        "/api/v1/testing/chat/sessions/{session_id}",
        "/api/v1/testing/source/sync-due",
        "/api/v1/testing/source/{source_id}/sync",
        "/api/v1/testing/source/{source_id}/webhook",
        "/api/v1/testing/requirement/import/docx",
        "/api/v1/testing/requirement/import/xmind",
    }
    by_path = {r.path: r for r in api.router.routes}
    assert by_path["/api/v1/testing/tools"].methods == {"GET"}
    assert "POST" in by_path["/api/v1/testing/methods/{method_name}"].methods


@pytest.mark.asyncio
async def test_api_handler_dispatch(tortoise_db):
    """直调路由协程（不起服务），确认鉴权占位放行 + 错误映射。"""
    from fastapi import HTTPException

    from testing_platform.surface import api

    principal = await api.require_surface_principal()
    assert principal.user_id

    ok = await api.call_method_http("tool.list", principal, {})
    assert ok["ok"] is True and "tools" in ok["result"]

    with pytest.raises(HTTPException) as excinfo:
        await api.call_method_http("scenario.validate", principal, {})
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "method_not_found"

    with pytest.raises(HTTPException) as bad:
        await api.call_method_http("script.validate", principal, {"params": {}})
    assert bad.value.status_code == 422


# ── 8) 查询类 list/get 方法：注册 + 空库 + 建数据后返回正确 ──────
def test_query_methods_are_registered_as_read():
    query_names = {
        "report.list",
        "report.get",
        "run.list",
        "run.get",
        "case.list",
        "case.get",
        "scenario.list",
        "task.list",
        "bug.list",
        "knowledge.list",
    }
    for name in query_names:
        assert methods.has_method(name), f"{name} 未注册"
        assert methods.get_method(name).kind == "read", f"{name} 应为 read"

    # 缺失 id 的 get 方法必须带 required 主键参数
    for name, required in [
        ("report.get", "report_id"),
        ("run.get", "run_id"),
        ("case.get", "case_id"),
    ]:
        schema = methods.get_method(name).params_schema
        assert required in schema["required"], f"{name} 缺必填参数 {required}"


@pytest.mark.asyncio
async def test_report_list_and_get(tortoise_db):
    from testing_platform.reports.models import TestReport

    pid = uuid.uuid4()
    # 空库 → []
    empty = await mcp.call_tool("report.list", {"project_id": str(pid)})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"reports": [], "count": 0}

    # 建数据 → 列表返回文档（snapshot 原样）
    run_id = uuid.uuid4()
    report = await TestReport.create(
        id=uuid.uuid4(),
        code="RPT-0001",
        project_id=pid,
        run_id=run_id,
        title="支付冒烟",
        status="ready",
        generator="auto",
        snapshot={"overview": {"total": 3, "pass": 3}},
    )
    listed = await mcp.call_tool("report.list", {"project_id": str(pid)})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["reports"][0]
    assert doc["id"] == str(report.id)
    assert doc["code"] == "RPT-0001"
    assert doc["project_id"] == str(pid)
    assert doc["run_id"] == str(run_id)
    assert doc["status"] == "ready"
    assert doc["snapshot"] == {"overview": {"total": 3, "pass": 3}}

    # get → 单文档
    got = await mcp.call_tool("report.get", {"report_id": str(report.id)})
    assert got["isError"] is False
    assert got["structuredContent"]["id"] == str(report.id)
    assert got["structuredContent"]["snapshot"]["overview"]["total"] == 3

    # 缺失 → not_found（不抛穿）
    missing = await mcp.call_tool("report.get", {"report_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_run_list_and_get(tortoise_db):
    from testing_platform.tasks.models import TestRun

    pid = uuid.uuid4()
    empty = await mcp.call_tool("run.list", {"project_id": str(pid)})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"runs": [], "count": 0}

    run = await TestRun.create(
        id=uuid.uuid4(),
        code="RUN-0001",
        project_id=pid,
        trigger="manual",
        status="pending",
        scenario_ids=["SCN-0001"],
        global_params={"base_url": "https://staging.example"},
    )
    listed = await mcp.call_tool("run.list", {"project_id": str(pid)})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["runs"][0]
    assert doc["id"] == str(run.id)
    assert doc["code"] == "RUN-0001"
    assert doc["status"] == "pending"
    assert doc["trigger"] == "manual"
    assert doc["scenario_ids"] == ["SCN-0001"]

    # 状态过滤
    filtered = await mcp.call_tool(
        "run.list", {"project_id": str(pid), "status": "done"}
    )
    assert filtered["structuredContent"] == {"runs": [], "count": 0}

    got = await mcp.call_tool("run.get", {"run_id": str(run.id)})
    assert got["isError"] is False
    assert got["structuredContent"]["id"] == str(run.id)
    assert got["structuredContent"]["global_params"]["base_url"] == "https://staging.example"

    missing = await mcp.call_tool("run.get", {"run_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_case_list_and_get(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    empty = await mcp.call_tool("case.list", {"project_id": pid})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"cases": [], "count": 0, "total": 0}

    created = await mcp.call_tool(
        "case.create",
        {
            "project_id": pid,
            "title": "用户下单全流程",
            "confirm_source": "user_confirm",
            "created_by": me,
            "expected": {"status": 201},
            "priority": "P1",
            "status": "active",
        },
    )
    assert created["isError"] is False
    case_id = created["structuredContent"]["id"]

    listed = await mcp.call_tool("case.list", {"project_id": pid})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["cases"][0]
    assert doc["id"] == case_id
    assert doc["code"] == "TC-0001"
    assert doc["title"] == "用户下单全流程"
    assert doc["priority"] == "P1"
    assert doc["status"] == "active"

    got = await mcp.call_tool("case.get", {"case_id": case_id})
    assert got["isError"] is False
    assert got["structuredContent"]["id"] == case_id

    missing = await mcp.call_tool("case.get", {"case_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_scenario_list(tortoise_db):
    pid = str(uuid.uuid4())
    empty = await mcp.call_tool("scenario.list", {"project_id": pid})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"scenarios": [], "count": 0, "total": 0}

    made = await mcp.call_tool(
        "scenario.create",
        {
            "project_id": pid,
            "name": "下单主流程",
            "purpose": "冒烟",
            "exec_mode": "script_only",
            "status": "active",
            "steps": [
                {
                    "step_key": "create_order",
                    "seq": 10,
                    "type": "request",
                    "params": {"method": "POST", "url": "{{env.base_url}}/orders"},
                    "post_checks": [{"type": "status_code", "expected": 201}],
                }
            ],
        },
    )
    assert made["isError"] is False

    listed = await mcp.call_tool("scenario.list", {"project_id": pid})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["scenarios"][0]
    assert doc["id"] == made["structuredContent"]["id"]
    assert doc["code"] == "SCN-0001"
    assert doc["name"] == "下单主流程"
    assert doc["exec_mode"] == "script_only"
    assert doc["status"] == "active"

    # 状态过滤
    filtered = await mcp.call_tool("scenario.list", {"project_id": pid, "status": "archived"})
    assert filtered["structuredContent"] == {"scenarios": [], "count": 0, "total": 0}


@pytest.mark.asyncio
async def test_task_list(tortoise_db):
    from testing_platform.tasks.models import TestTask

    pid = uuid.uuid4()
    empty = await mcp.call_tool("task.list", {"project_id": str(pid)})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"tasks": [], "count": 0}

    task = await TestTask.create(
        id=uuid.uuid4(),
        code="TASK-0001",
        project_id=pid,
        title="夜间回归",
        exec_type="manual",
        exec_mode="script_only",
        status="enabled",
        created_by=uuid.uuid4(),
    )
    listed = await mcp.call_tool("task.list", {"project_id": str(pid)})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["tasks"][0]
    assert doc["id"] == str(task.id)
    assert doc["code"] == "TASK-0001"
    assert doc["title"] == "夜间回归"
    assert doc["exec_type"] == "manual"
    assert doc["status"] == "enabled"


@pytest.mark.asyncio
async def test_bug_list(tortoise_db):
    from testing_platform.defects.models import TestBug

    pid = uuid.uuid4()
    empty = await mcp.call_tool("bug.list", {"project_id": str(pid)})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"bugs": [], "count": 0}

    # 直插测试域自持 bug（mc_test_bug，project_id=测试项目 id）
    await TestBug.create(
        id=uuid.uuid4(),
        code="BUG-0001",
        project_id=pid,
        title="下单接口 500",
        symptom="POST /orders 返回 500",
        status="open",
        severity="critical",
        priority=1,
        tags=["payment", "test_discovered"],
        fingerprint="FP-aaaaaaaaaaaaaaaa",
        repro={"steps": ["POST /orders"], "env": "staging", "rate": 1},
        from_run_id=uuid.uuid4(),
    )
    listed = await mcp.call_tool("bug.list", {"project_id": str(pid)})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["bugs"][0]
    assert doc["title"] == "下单接口 500"
    assert doc["status"] == "open"
    assert doc["severity"] == "critical"
    assert doc["fingerprint"] == "FP-aaaaaaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_knowledge_list(tortoise_db):
    from testing_platform.knowledge.models import KnowledgeItem

    pid = uuid.uuid4()
    empty = await mcp.call_tool("knowledge.list", {"project_id": str(pid)})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"items": [], "count": 0}

    await KnowledgeItem.create(
        id=uuid.uuid4(),
        project_id=pid,
        kind="rule",
        key="RULE-77",
        title="对账规则",
        content={"rule": "T+1 对账"},
        status="confirmed",
    )
    listed = await mcp.call_tool("knowledge.list", {"project_id": str(pid)})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    item = body["items"][0]
    assert item["key"] == "RULE-77"
    assert item["kind"] == "rule"
    assert item["status"] == "confirmed"

    # kind 过滤
    filtered = await mcp.call_tool(
        "knowledge.list", {"project_id": str(pid), "kind": "rationale"}
    )
    assert filtered["structuredContent"] == {"items": [], "count": 0}

    # 非法 UUID → invalid_params
    bad = await mcp.call_tool("knowledge.list", {"project_id": "not-a-uuid"})
    assert bad["isError"] is True
    assert bad["structuredContent"]["code"] == "invalid_params"


# ── 11) 补齐前端缺失的 4 个方法：question.list / case.scenarios / ──
#      case.unlink_scenario / case.tags —— 注册 + 真跑（建数据→查→解绑→再查）
NEW_FRONTEND_METHODS: dict[str, str] = {
    "question.list": "read",
    "case.scenarios": "read",
    "case.unlink_scenario": "write",
    "case.tags": "read",
}


def test_new_frontend_methods_registered_with_valid_contract():
    names = set(methods.default_method_registry().names())
    for name, kind in NEW_FRONTEND_METHODS.items():
        assert name in names, f"前端缺失方法未注册：{name}"
        spec = methods.get_method(name)
        assert spec.kind == kind, f"{name} 的 kind 应为 {kind}，实际 {spec.kind}"
        schema = spec.params_schema
        assert schema.get("type") == "object"
        props = set(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        assert required, f"{name} 至少应有一个必填参数"
        assert set(required) <= props, f"{name} required={required} 不在 properties={sorted(props)} 内"
        assert isinstance(spec.returns_schema, dict) and spec.returns_schema
        assert spec.description, f"{name} 缺描述"


def test_new_frontend_methods_required_params():
    for name, required in [
        ("question.list", "project_id"),
        ("case.scenarios", "case_id"),
        ("case.tags", "case_id"),
    ]:
        schema = methods.get_method(name).params_schema
        assert schema["required"] == [required], f"{name} 必填参数应为 {required}"
    # unlink 需 case_id + scenario_id 两个主键
    assert set(methods.get_method("case.unlink_scenario").params_schema["required"]) == {
        "case_id",
        "scenario_id",
    }


@pytest.mark.asyncio
async def test_question_list_orders_desc_and_filters_status(tortoise_db):
    """question.list：按项目列疑问，created_at 倒序，status 可收窄。"""
    pid = str(uuid.uuid4())

    empty = await mcp.call_tool("question.list", {"project_id": pid})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"questions": [], "count": 0}

    first = await mcp.call_tool(
        "question.raise",
        {"project_id": pid, "origin": "product_uncertainty", "content": "先提的疑问"},
    )
    assert first["isError"] is False
    second = await mcp.call_tool(
        "question.raise",
        {"project_id": pid, "origin": "design_doubt", "content": "后提的疑问"},
    )
    assert second["isError"] is False
    # 收敛第二条 → 只应出现在 resolved 过滤里
    resolved = await mcp.call_tool(
        "question.resolve", {"question_id": second["structuredContent"]["id"], "resolution": "doc"}
    )
    assert resolved["isError"] is False

    # 显式错开 created_at（连建两条可能同微秒，倒序会退化成不稳定序）
    from datetime import timezone

    from testing_platform.quality.models import TestQuestion

    await TestQuestion.filter(id=first["structuredContent"]["id"]).update(
        created_at=datetime(2026, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
    )
    await TestQuestion.filter(id=second["structuredContent"]["id"]).update(
        created_at=datetime(2026, 10, 3, 10, 0, 5, tzinfo=timezone.utc)
    )

    listed = await mcp.call_tool("question.list", {"project_id": pid})
    assert listed["isError"] is False, listed["structuredContent"]
    body = listed["structuredContent"]
    assert body["count"] == 2
    # created_at 倒序：后提的在前
    assert [q["id"] for q in body["questions"]] == [
        second["structuredContent"]["id"],
        first["structuredContent"]["id"],
    ]
    assert body["questions"][0]["status"] == "resolved"
    assert body["questions"][1]["status"] == "open"
    assert body["questions"][1]["content"] == "先提的疑问"

    # status=open 过滤 → 只剩第一条
    opened = await mcp.call_tool("question.list", {"project_id": pid, "status": "open"})
    assert opened["structuredContent"]["count"] == 1
    assert opened["structuredContent"]["questions"][0]["id"] == first["structuredContent"]["id"]

    # 非法 status → invalid_params（枚举校验）
    bad = await mcp.call_tool("question.list", {"project_id": pid, "status": "nope"})
    assert bad["isError"] is True
    assert bad["structuredContent"]["code"] == "invalid_params"


@pytest.mark.asyncio
async def test_case_scenarios_and_unlink_roundtrip(tortoise_db):
    """case.scenarios 返关联 + 场景详情；case.unlink_scenario 幂等解绑。"""
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    case = await mcp.call_tool(
        "case.create",
        {"project_id": pid, "title": "下单", "confirm_source": "doc", "created_by": me},
    )
    case_id = case["structuredContent"]["id"]

    # 空关联 → 空集
    empty = await mcp.call_tool("case.scenarios", {"case_id": case_id})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"links": [], "count": 0}

    scn = await mcp.call_tool(
        "scenario.create",
        {"project_id": pid, "name": "下单主流程", "status": "active"},
    )
    sid = scn["structuredContent"]["id"]
    scn_code = scn["structuredContent"]["code"]

    await mcp.call_tool(
        "case.link_scenario", {"case_id": case_id, "scenario_id": sid, "is_primary": True, "order": 10}
    )

    listed = await mcp.call_tool("case.scenarios", {"case_id": case_id})
    assert listed["isError"] is False, listed["structuredContent"]
    body = listed["structuredContent"]
    assert body["count"] == 1
    link = body["links"][0]
    assert link["case_id"] == case_id
    assert link["scenario_id"] == sid
    assert link["is_primary"] is True
    assert link["order"] == 10
    # join 出的场景详情
    assert link["code"] == scn_code
    assert link["name"] == "下单主流程"
    assert link["status"] == "active"

    # 解绑 → 删除 1 行；再查空
    unlinked = await mcp.call_tool(
        "case.unlink_scenario", {"case_id": case_id, "scenario_id": sid}
    )
    assert unlinked["isError"] is False, unlinked["structuredContent"]
    assert unlinked["structuredContent"] == {"deleted": 1}
    after = await mcp.call_tool("case.scenarios", {"case_id": case_id})
    assert after["structuredContent"] == {"links": [], "count": 0}

    # 幂等：再解绑一次 → deleted=0，不报错
    again = await mcp.call_tool(
        "case.unlink_scenario", {"case_id": case_id, "scenario_id": sid}
    )
    assert again["isError"] is False
    assert again["structuredContent"] == {"deleted": 0}

    # 未知用例 → not_found（不抛穿）
    missing = await mcp.call_tool("case.scenarios", {"case_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_case_tags_lists_joined_tags(tortoise_db):
    """case.tags：返回该用例已打标签（join mc_test_tag），category/code 升序。"""
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    case = await mcp.call_tool(
        "case.create",
        {"project_id": pid, "title": "下单", "confirm_source": "doc", "created_by": me},
    )
    case_id = case["structuredContent"]["id"]

    empty = await mcp.call_tool("case.tags", {"case_id": case_id})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"tags": [], "count": 0}

    tag = await mcp.call_tool(
        "tag.create", {"project_id": pid, "name": "smoke", "category": "purpose"}
    )
    tag_id = tag["structuredContent"]["id"]
    await mcp.call_tool("case.tag", {"case_id": case_id, "tag_id": tag_id})

    listed = await mcp.call_tool("case.tags", {"case_id": case_id})
    assert listed["isError"] is False, listed["structuredContent"]
    body = listed["structuredContent"]
    assert body["count"] == 1
    doc = body["tags"][0]
    assert doc["id"] == tag_id
    assert doc["name"] == "smoke"
    assert doc["category"] == "purpose"
    assert doc["project_id"] == pid



# ── 9) 补齐资产缺失域的方法：注册 + kind + 契约形态 ──────────────
#: 4 张缺失表 + 接口导入对应的能力面方法 → 期望 kind。
NEW_ASSET_METHODS: dict[str, str] = {
    "transport.import_openapi": "write",
    "transport.import_from_repo": "execute",
    "transport.list": "read",
    "transport.create": "write",
    "transport.get": "read",
    "transport.update": "write",
    "transport.delete": "write",
    "method_module.create": "write",
    "method_module.update": "write",
    "method_module.delete": "write",
    "method_module.get": "read",
    "method_module.list": "read",
    "method_module.classes": "read",
    "method_module.parse": "read",
    "method_module.changes": "read",
    "method.create": "write",
    "method.list": "read",
    "dataset.list": "read",
    "dataset.create": "write",
    "dataset.update": "write",
    "dataset.delete": "write",
    "tag.list": "read",
    # 需求主脊：需求实体 CRUD + 产出聚合 + 径向图数据源。
    "requirement.create": "write",
    "requirement.list": "read",
    "requirement.get": "read",
    "requirement.update": "write",
    "requirement.delete": "write",
    "requirement.trace": "read",
    "requirement.attachment.list": "read",
    "requirement.attachment.add": "write",
    "requirement.attachment.delete": "write",
    "project.radial_graph": "read",
    "project.git.branches": "read",
    "transport.generate_from_repo": "execute",
    # 接口管理（mc_test_api）：CRUD/移动 + 目录 + OpenAPI 全字段导入（dry-run 支持）。
    "api.list": "read",
    "api.get": "read",
    "api.changes": "read",
    "api.source.list": "read",
    "api.source.sync": "write",
    "api.source.delete": "write",
    "api.create": "write",
    "api.update": "write",
    "api.move": "write",
    "api.delete": "write",
    "api_folder.list": "read",
    "api_folder.create": "write",
    "api_folder.update": "write",
    "api_folder.delete": "write",
    # 统一目录（mc_test_catalog：需求/用例/场景/接口共用一张表，type 区分）
    "catalog.list": "read",
    "catalog.create": "write",
    "catalog.update": "write",
    "catalog.delete": "write",
    "api.import_openapi": "write",
    # 接口↔用例/场景 直连（P5）
    "api.case.link": "write",
    "api.case.unlink": "write",
    "api.cases": "read",
    "api.scenario.link": "write",
    "api.scenario.unlink": "write",
    "api.scenarios": "read",
}


def test_missing_asset_methods_are_registered_with_valid_contract():
    names = set(methods.default_method_registry().names())
    for name, kind in NEW_ASSET_METHODS.items():
        assert name in names, f"缺失资产域方法未注册：{name}"
        spec = methods.get_method(name)
        assert spec.kind == kind, f"{name} 的 kind 应为 {kind}，实际 {spec.kind}"
        assert spec.params_schema.get("type") == "object"
        props = set(spec.params_schema.get("properties") or {})
        required = list(spec.params_schema.get("required") or [])
        assert set(required) <= props, f"{name} required 不在 properties 内"
        assert isinstance(spec.returns_schema, dict) and spec.returns_schema
        assert spec.description, f"{name} 缺描述"


def test_import_openapi_method_accepts_spec_or_url():
    """接口导入：spec（dict/字符串）与 spec_url 两种入口都在 schema 里声明。"""
    schema = methods.get_method("transport.import_openapi").params_schema
    assert schema["required"] == ["project_id"]
    assert "spec" in schema["properties"]
    assert "spec_url" in schema["properties"]
    # spec 同时接受 object（已解析）与 string（粘贴文本）
    assert set(schema["properties"]["spec"]["type"]) == {"object", "string"}


@pytest.mark.asyncio
async def test_transport_dataset_method_roundtrip(tortoise_db):
    """transport/dataset 的 create→list→get 真跑（含接口导入后的可见性）。"""
    pid = str(uuid.uuid4())

    made = await mcp.call_tool(
        "transport.create",
        {"project_id": pid, "code": "endpoint_get_orders", "kind": "http", "auth": {"re_login_on": [401]}},
    )
    assert made["isError"] is False
    assert made["structuredContent"]["code"] == "endpoint_get_orders"
    assert made["structuredContent"]["auth"] == {"re_login_on": [401]}

    listed = await mcp.call_tool("transport.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 1

    got = await mcp.call_tool("transport.get", {"transport_id": "endpoint_get_orders"})
    assert got["isError"] is False
    assert got["structuredContent"]["id"] == made["structuredContent"]["id"]

    # 变更门
    blocked = await mcp.call_tool("transport.update", {"transport_id": "endpoint_get_orders", "kind": "ws"})
    assert blocked["isError"] is True
    assert blocked["structuredContent"]["code"] == "change_reason_required"

    # dataset create → list
    ds = await mcp.call_tool(
        "dataset.create",
        {"project_id": pid, "code": "order_dataset", "schema": {"product_id": "string"}, "rows": [{"product_id": "P1"}]},
    )
    assert ds["isError"] is False
    assert ds["structuredContent"]["rows"] == [{"product_id": "P1"}]
    ds_list = await mcp.call_tool("dataset.list", {"project_id": pid})
    assert ds_list["structuredContent"]["count"] == 1


# ── 10) 执行记录层级（run → run_scenario → step_result）+ 环境变量列取 ──
def test_run_hierarchy_and_env_var_list_are_read_methods():
    """3 个新查询方法注册为 read，必填主键参数到位。"""
    for name, required in [
        ("run.scenarios", "run_id"),
        ("run.steps", "run_scenario_id"),
        ("env.var.list", "env_id"),
    ]:
        assert methods.has_method(name), f"{name} 未注册"
        assert methods.get_method(name).kind == "read", f"{name} 应为 read"
        schema = methods.get_method(name).params_schema
        assert schema["type"] == "object"
        assert schema["required"] == [required], f"{name} 必填参数应为 {required}"


@pytest.mark.asyncio
async def test_run_scenarios_and_steps(tortoise_db):
    """run.scenarios 返场景级行（倒序）；run.steps 返步骤级行（seq 升序）。"""
    from testing_platform.tasks.models import TestRun, TestRunScenario, TestStepResult

    pid = uuid.uuid4()
    run = await TestRun.create(
        id=uuid.uuid4(),
        code="RUN-0001",
        project_id=pid,
        trigger="manual",
        status="done",
        scenario_ids=["SCN-0001"],
    )
    # 两个场景结果（显式错开 started_at → 倒序可见）
    rs_old = await TestRunScenario.create(
        id=uuid.uuid4(),
        run_id=run.id,
        scenario_id="SCN-0001",
        exec_mode="script_only",
        status="pass",
        duration_ms=120,
        case_refs=[str(uuid.uuid4())],
        dataset_row={"product_id": "P1"},
        started_at=datetime(2026, 10, 3, 10, 0, 0),
    )
    rs_new = await TestRunScenario.create(
        id=uuid.uuid4(),
        run_id=run.id,
        scenario_id="SCN-0002",
        exec_mode="hybrid",
        status="fail",
        failure_kind="product_bug",
        duration_ms=340,
        started_at=datetime(2026, 10, 3, 10, 0, 5),
    )
    # 步骤结果（乱序插入，seq 应升序返回）
    await TestStepResult.create(
        id=uuid.uuid4(),
        run_scenario_id=rs_new.id,
        step_key="verify_order",
        seq="20",
        type="request",
        status="fail",
        attempt=2,
        expected={"status_code": 200},
        actual={"status_code": 500},
        diff={"status_code": {"expected": 200, "actual": 500}},
        duration_ms=80,
    )
    step_first = await TestStepResult.create(
        id=uuid.uuid4(),
        run_scenario_id=rs_new.id,
        step_key="create_order",
        seq="10",
        type="request",
        status="pass",
        attempt=1,
        duration_ms=40,
    )

    # run.scenarios → 倒序（最新 rs_new 在前）
    out = await mcp.call_tool("run.scenarios", {"run_id": str(run.id)})
    assert out["isError"] is False, out["structuredContent"]
    body = out["structuredContent"]
    assert body["count"] == 2
    assert [d["scenario_id"] for d in body["scenarios"]] == ["SCN-0002", "SCN-0001"]
    newest = body["scenarios"][0]
    assert newest["id"] == str(rs_new.id)
    assert newest["run_id"] == str(run.id)
    assert newest["status"] == "fail"
    assert newest["failure_kind"] == "product_bug"
    assert newest["exec_mode"] == "hybrid"
    assert newest["duration_ms"] == 340
    oldest = body["scenarios"][1]
    assert oldest["id"] == str(rs_old.id)
    assert oldest["case_refs"] == list(rs_old.case_refs or [])
    assert oldest["dataset_row"] == {"product_id": "P1"}

    # run.steps → seq 升序（create_order 的 "10" 在 verify_order 的 "20" 前）
    steps_out = await mcp.call_tool("run.steps", {"run_scenario_id": str(rs_new.id)})
    assert steps_out["isError"] is False, steps_out["structuredContent"]
    sbody = steps_out["structuredContent"]
    assert sbody["count"] == 2
    assert [s["step_key"] for s in sbody["steps"]] == ["create_order", "verify_order"]
    assert [s["seq"] for s in sbody["steps"]] == ["10", "20"]
    first = sbody["steps"][0]
    assert first["id"] == str(step_first.id)
    assert first["status"] == "pass"
    assert first["type"] == "request"
    assert first["duration_ms"] == 40
    second = sbody["steps"][1]
    assert second["attempt"] == 2
    assert second["expected"] == {"status_code": 200}
    assert second["actual"] == {"status_code": 500}
    assert second["diff"] == {"status_code": {"expected": 200, "actual": 500}}

    # 未知 run → not_found（不抛穿）
    missing = await mcp.call_tool("run.scenarios", {"run_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"

    # 无步骤结果的 run_scenario → 空集（不报错）
    empty = await mcp.call_tool("run.steps", {"run_scenario_id": str(rs_old.id)})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"steps": [], "count": 0}

    # 非法 UUID → invalid_request（service 层 AssetValidationError，与 run.get 同口径）
    bad = await mcp.call_tool("run.steps", {"run_scenario_id": "not-a-uuid"})
    assert bad["isError"] is True
    assert bad["structuredContent"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_env_var_list_masks_secrets(tortoise_db):
    """env.var.list 列变量，key 升序；is_secret 值脱敏为 ***+末4位。"""
    pid = str(uuid.uuid4())
    env = await mcp.call_tool(
        "env.create", {"project_id": pid, "code": "dev", "name": "开发环境", "status": "active"}
    )
    assert env["isError"] is False, env["structuredContent"]
    env_id = env["structuredContent"]["id"]

    # 空环境 → 空集
    empty = await mcp.call_tool("env.var.list", {"env_id": env_id})
    assert empty["isError"] is False
    assert empty["structuredContent"] == {"variables": [], "count": 0}

    # 普通变量 + 密钥变量（直接落库，避免 create 回显真值干扰断言）
    from testing_platform.assets.models import TestEnvVariable

    await TestEnvVariable.create(
        env_id=uuid.UUID(env_id), key="base_url", value="https://staging.example",
        type="string", is_secret=False,
    )
    await TestEnvVariable.create(
        env_id=uuid.UUID(env_id), key="api_key", value="sk-abcdef123456",
        type="string", is_secret=True,
    )

    listed = await mcp.call_tool("env.var.list", {"env_id": env_id})
    assert listed["isError"] is False, listed["structuredContent"]
    body = listed["structuredContent"]
    assert body["count"] == 2
    # key 升序：api_key 在 base_url 前
    assert [v["key"] for v in body["variables"]] == ["api_key", "base_url"]
    by_key = {v["key"]: v for v in body["variables"]}
    assert by_key["base_url"]["value"] == "https://staging.example"
    assert by_key["base_url"]["is_secret"] is False
    # 密钥：***+末4位（脱敏在 service 层，方法只展平）
    assert by_key["api_key"]["value"] == "***3456"
    assert by_key["api_key"]["is_secret"] is True
    assert by_key["api_key"]["type"] == "string"

    # 未知环境 → not_found（不抛穿）
    missing = await mcp.call_tool("env.var.list", {"env_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


# ════════════════════════════════════════════════════════════════
# 测试项目域（test-project-definition.md §十）
# ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_project_crud_and_git_config(tortoise_db):
    """project.create/list/get/update/archive + git 配置 CRUD 全链路。"""
    created = await mcp.call_tool(
        "project.create", {"name": "平台回归", "description": "整个项目维度"}
    )
    assert created["isError"] is False, created["structuredContent"]
    proj = created["structuredContent"]
    assert proj["code"].startswith("TP-")
    assert proj["status"] == "active"

    pid = proj["id"]

    # git 配置：credential_ref 只存引用
    git = await mcp.call_tool(
        "project.git.create",
        {
            "project_id": pid,
            "name": "backend",
            "repo_url": "https://github.com/x/y.git",
            "ref_type": "branch",
            "ref_value": "main",
            "credential_ref": {"kind": "env", "name": "GIT_TOKEN"},
        },
    )
    assert git["isError"] is False, git["structuredContent"]
    git_id = git["structuredContent"]["id"]
    assert git["structuredContent"]["credential_ref"] == {"kind": "env", "name": "GIT_TOKEN"}

    # 同名冲突 → 结构化错误
    dup = await mcp.call_tool(
        "project.git.create",
        {"project_id": pid, "name": "backend", "repo_url": "https://x", "ref_value": "main"},
    )
    assert dup["isError"] is True
    assert dup["structuredContent"]["code"] == "invalid_request"

    listed = await mcp.call_tool("project.git.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 1

    updated = await mcp.call_tool(
        "project.git.update", {"git_id": git_id, "ref_value": "release/1.2"}
    )
    assert updated["structuredContent"]["item"]["ref_value"] == "release/1.2"

    # 项目 update：结构性变更（status）必须带 change_reason
    no_reason = await mcp.call_tool(
        "project.update", {"project_id": pid, "status": "archived"}
    )
    assert no_reason["isError"] is True

    ok = await mcp.call_tool(
        "project.update", {"project_id": pid, "status": "archived", "change_reason": "项目结项"}
    )
    assert ok["structuredContent"]["item"]["status"] == "archived"

    # 不存在的项目 → not_found
    missing = await mcp.call_tool("project.get", {"project_id": str(uuid.uuid4())})
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_project_source_crud(tortoise_db):
    """源配置：A/B 轨 + 同步策略 + 冲突校验。"""
    proj = (await mcp.call_tool("project.create", {"name": "源项目"}))["structuredContent"]
    pid = proj["id"]

    src = await mcp.call_tool(
        "project.source.create",
        {
            "project_id": pid,
            "kind": "prd",
            "name": "飞书 PRD",
            "adapter": "agent",
            "adapter_type": "feishu",
            "config": {"doc_url": "https://feishu.cn/x"},
            "sync_policy": "scheduled",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"},
        },
    )
    assert src["isError"] is False, src["structuredContent"]
    assert src["structuredContent"]["adapter"] == "agent"

    # 同 kind+name 冲突
    dup = await mcp.call_tool(
        "project.source.create",
        {"project_id": pid, "kind": "prd", "name": "飞书 PRD"},
    )
    assert dup["isError"] is True

    listed = await mcp.call_tool("project.source.list", {"project_id": pid, "adapter": "agent"})
    assert listed["structuredContent"]["count"] == 1

    updated = await mcp.call_tool(
        "project.source.update",
        {"source_id": src["structuredContent"]["id"], "sync_policy": "manual"},
    )
    assert updated["structuredContent"]["item"]["sync_policy"] == "manual"

    deleted = await mcp.call_tool(
        "project.source.delete", {"source_id": src["structuredContent"]["id"]}
    )
    assert deleted["structuredContent"]["deleted"] is True


@pytest.mark.asyncio
async def test_project_release_and_diff(tortoise_db):
    """release 指针快照 + 两版本 diff（增量测试根基）。"""
    proj = (await mcp.call_tool("project.create", {"name": "版本项目"}))["structuredContent"]
    pid = proj["id"]

    r1 = (await mcp.call_tool(
        "project.release.create",
        {
            "project_id": pid,
            "version_code": "v1.0",
            "git_tags": [{"git_id": "g1", "ref": "release/1.0"}],
        },
    ))["structuredContent"]
    r2 = (await mcp.call_tool(
        "project.release.create",
        {
            "project_id": pid,
            "version_code": "v1.1",
            "git_tags": [{"git_id": "g1", "ref": "release/1.1"}],
            "case_versions": [{"source_id": "s1", "ref": "42"}],
        },
    ))["structuredContent"]

    listed = await mcp.call_tool("project.release.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 2

    diff = (await mcp.call_tool(
        "project.release.diff",
        {"base_release_id": r1["id"], "head_release_id": r2["id"]},
    ))["structuredContent"]["diff"]
    assert diff["base"] == "v1.0" and diff["head"] == "v1.1"
    assert diff["git_tags"]["changed_ids"] == ["g1"]
    assert diff["git_tags"]["changed"]["g1"] == {"from": "release/1.0", "to": "release/1.1"}
    assert diff["case_versions"]["added"] == ["s1"]

    # 重复 version_code → 结构化错误
    dup = await mcp.call_tool(
        "project.release.create", {"project_id": pid, "version_code": "v1.0"}
    )
    assert dup["isError"] is True


@pytest.mark.asyncio
async def test_project_graph_query(tortoise_db):
    """图谱 query：锚点/边 → 力导向图数据；悬挂边被过滤。"""
    from testing_platform.knowledge.models import KnowledgeAnchor, KnowledgeEdge

    proj = (await mcp.call_tool("project.create", {"name": "图谱项目"}))["structuredContent"]
    pid = uuid.UUID(proj["id"])
    other = uuid.uuid4()

    a1 = await KnowledgeAnchor.create(
        project_id=pid, kind="function", path="a.py", symbol="f1", label="f1"
    )
    a2 = await KnowledgeAnchor.create(
        project_id=pid, kind="class", path="b.py", symbol="C1", label="C1"
    )
    await KnowledgeAnchor.create(
        project_id=other, kind="function", path="z.py", symbol="zzz", label="zzz"
    )
    e1 = await KnowledgeEdge.create(
        project_id=pid, relation="calls",
        from_kind="anchor", from_id=a1.id, to_kind="anchor", to_id=a2.id,
    )
    # 悬挂边：to 指向别的项目（不在节点集合）
    await KnowledgeEdge.create(
        project_id=pid, relation="calls",
        from_kind="anchor", from_id=a1.id, to_kind="anchor", to_id=other,
    )

    out = (await mcp.call_tool("project.graph.query", {"project_id": proj["id"]}))[
        "structuredContent"
    ]
    assert out["count"] == 2
    node_ids = {n["id"] for n in out["nodes"]}
    assert str(a1.id) in node_ids and str(a2.id) in node_ids
    assert all(n["reliability"] in ("confirmed", "candidate", "stale") for n in out["nodes"])
    # 只保留两端都在集合内的边
    assert [str(e1.id)] == [e["id"] for e in out["edges"]]
    assert out["meta"]["node_kinds"] == ["class", "function"]


# ── run.execute_waves —— run 级波次编排（§环节8 接线）────────────
@pytest.mark.asyncio
async def test_run_execute_waves_end_to_end(tortoise_db):
    """缺省 cases 展开（run.scenario_ids）→ 逐波真跑引擎 → 四态 summary + done。"""
    from testing_platform.assets.service import create_scenario
    from testing_platform.tasks.models import TestRun, TestRunScenario

    pid = uuid.uuid4()
    # 零步骤场景（steps 在 mc_test_step 表，flow 只是控制面 dict）→ 引擎判 pass
    scn_pass = await create_scenario(project_id=pid, name="波次-通过场景")
    run = await TestRun.create(
        project_id=pid, code="RUN-W1",
        scenario_ids=[str(scn_pass.id)], status="pending",
    )

    out = await mcp.call_tool("run.execute_waves", {"run_id": str(run.id)})
    assert out["isError"] is False, out["structuredContent"]
    body = out["structuredContent"]
    assert body["run"]["status"] == "done"
    assert body["summary"]["total"] >= 1
    # 四态计数与 total 对账（states 是键固定「过/挂/判不了/没裁」的汇总）
    assert sum(body["summary"]["states"].values()) == body["summary"]["total"]
    assert body["wave_gates"], "至少跑完一波才有闸门记录"
    # run.status 已收尾（summary 落库）
    fresh = await TestRun.get(id=run.id)
    assert fresh.status == "done"
    assert (fresh.summary or {}).get("states") == body["summary"]["states"]


@pytest.mark.asyncio
async def test_run_execute_waves_empty_candidates_error(tortoise_db):
    """run.scenario_ids 空且未传 cases → 结构化报错（不静默空跑）。"""
    from testing_platform.tasks.models import TestRun

    run = await TestRun.create(
        project_id=uuid.uuid4(), code="RUN-EMPTY",
        scenario_ids=[], status="pending",
    )
    out = await mcp.call_tool("run.execute_waves", {"run_id": str(run.id)})
    assert out["isError"] is True
    assert out["structuredContent"]["code"] == "invalid_params"
