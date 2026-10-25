"""能力面 · 写类（可操作）方法测试 —— design §17.1 落数据能力（34 方法）。

覆盖（sqlite::memory:，模式照 ``tests/test_surface_methods.py``；app label 统一
``testing_platform``）：

1. 15 个新写方法**全部注册**，且 ``kind`` / ``params_schema`` / ``returns_schema``
   形态合法（``required`` ⊆ ``properties``、描述非空）。
2. 空参数 → 结构化 ``invalid_params``（不抛穿协议面）。
3. ``task.*``：create → 能 list 到；update 缺 ``change_reason`` → 结构化
   ``change_reason_required``（与 ``case.update`` 一致）；enable/disable/run
   真改状态、真建 pending run。
4. ``scenario.update`` 变更门 + ``scenario.create_steps`` 真建步骤（返回全文）。
5. ``tag.create`` / ``case.link_scenario``（主场景互斥）/ ``case.tag``（幂等）真跑。
6. ``report.generate`` 由 run 真生成报告（status=ready）。
7. ``bug.create`` 真建 issue（复用 ``project_service.create_issue``，需 owner 项目）+
   指纹去重；``bug.root_cause`` 缺 ``evidence_chain`` → ``root_cause_evidence_required``。
8. ``workflow.start`` 真建运行；``workflow.complete`` 过契约 → done / 不过 → failed。

运行：``python -m pytest tests/test_surface_writes.py -q``
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

from testing_platform.surface import mcp, methods  # noqa: E402
from testing_platform.surface.methods import METHOD_KINDS  # noqa: E402

#: 15 个新增写方法 → 期望 kind（``task.run``/``report.generate`` 按约定算 write）。
NEW_WRITE_METHODS: dict[str, str] = {
    "task.create": "write",
    "task.update": "write",
    "task.enable": "write",
    "task.disable": "write",
    "task.run": "write",
    "scenario.update": "write",
    "scenario.create_steps": "write",
    "case.link_scenario": "write",
    "case.tag": "write",
    "tag.create": "write",
    "report.generate": "write",
    "bug.create": "write",
    "bug.root_cause": "write",
    # 缺陷域禅道对齐（plans/23-defect-zentao-parity.md）：编辑 + 状态流转 + 删除
    "bug.update": "write",
    "bug.transition": "write",
    "bug.delete": "write",
    "workflow.start": "write",
    "workflow.complete": "write",
}

#: 本轮补齐缺口表的写方法 → 期望 kind（环境/疑问/自定义工具/沉淀）。
GAP_WRITE_METHODS: dict[str, str] = {
    "env.create": "write",
    "env.update": "write",
    "env.delete": "write",
    "env.var.create": "write",
    "env.var.update": "write",
    "env.var.delete": "write",
    "question.raise": "write",
    "question.resolve": "write",
    "custom_tool.create": "write",
    "custom_tool.update": "write",
    "craft.create": "write",
    "case.unlink_scenario": "write",
    # 接口管理（mc_test_api）+ 目录 + OpenAPI 导入 + 接口↔用例/场景直连（P2-P5）
    "api.create": "write",
    "api.update": "write",
    "api.move": "write",
    "api.delete": "write",
    "api_folder.create": "write",
    "api_folder.update": "write",
    "api_folder.delete": "write",
    "api.import_openapi": "write",
    "api.source.sync": "write",
    "api.source.delete": "write",
    "api.case.link": "write",
    "api.case.unlink": "write",
    "api.scenario.link": "write",
    "api.scenario.unlink": "write",
}

#: 测试项目域写方法（``test-project-definition.md`` §十；``project.code.*``
#: 是真跑 git 的 ``execute``，不在此列）。
PROJECT_WRITE_METHODS: dict[str, str] = {
    "project.create": "write",
    "project.update": "write",
    "project.archive": "write",
    "project.git.create": "write",
    "project.git.update": "write",
    "project.git.delete": "write",
    "project.source.create": "write",
    "project.source.update": "write",
    "project.source.delete": "write",
    "project.release.create": "write",
    # 阶段 2：双轨数据源 + 用例扫描
    "source.fetch": "write",
    "case.scan": "write",
    "knowledge.put": "write",
    # 阶段 3/4/5：工作流断点 + 确认门 + 工具评审 + 版本捕获
    "scenario.generate": "write",
    "bug.triage": "write",
    "case.confirm": "write",
    "tool.register": "write",
    "release.capture": "write",
    "analyst.commit": "write",
}

#: 旧 19 + 新 15 + 补齐资产缺失域 14 + 本轮缺口 16 + 执行层级/环境变量列表 3
#: + 本轮前端缺口 4（question.list / case.scenarios / case.unlink_scenario / case.tags）
#: + 测试项目域 19（project.* / git.* / source.* / code.* / release.* / graph.query）
#: + 阶段 2 双轨数据源 6（project.code.graph / source.fetch / source.list_raw /
#:   source.adapters / case.scan / knowledge.put）
#: + 阶段 3/4/5 共 12（scenario.generate / bug.triage / run.status / run.result /
#:   case.confirm / knowledge.profile / tool.register / analyst.prepare /
#:   analyst.commit / release.capture / release.impact / reliability.report）
#: = 109（能力面当前真实边界）。
#: + 版本一等实体 7（project.version.create/list/get/update/delete/build_graph/diff）
#: + 仓库导入接口 1（transport.import_from_repo）+ AI 生成用例 1（case.generate）
#: + 工作流状态查询 1（workflow.get）= 119。
#: + 需求主脊 7（requirement.create/list/get/update/delete/trace
#:   + project.radial_graph）= 126。
#: + 远端分支列举 1（project.git.branches，修 main/master 分支报错）= 127。
#: + AI 读仓库生成接口 1（transport.generate_from_repo）= 128。
#: + 接口管理 10（api.list/get/create/update/move/delete +
#:   api_folder.list/create/update/delete；mc_test_api 一等实体，身份=id）= 138。
#: + OpenAPI 全字段导入 1（api.import_openapi，dry-run 支持）= 139。
#: + 接口↔用例/场景直连 6（api.case.link/unlink + api.cases +
#:   api.scenario.link/unlink + api.scenarios）= 145。
#: + 方法库整类化 5（method_module.classes/get/parse/update/delete；create/list 已在
#:   128 基线内——并发定版：AST 同步注册表 + kind 删列 + 变更门）= 150。
#: + 测试设计 L1/L2 2（case.enumerate 机械枚举入口 + coverage.report 覆盖矩阵）= 152。
#: + 接口反向引用 2（scenario.apis + case.apis——沿步骤 params.api_id 派生关联索引）= 154。
#: + 需求附件 3（requirement.attachment.list/add/delete；需求 = 文档：附件走 OSS 存 URL）= 157。
#: + 用例目录 4（catalog.list/create/update/delete；用例归属目录）= 161。
#: + 接口导入任务 6（api.import_job.create/list/get/update/delete/run；spec_url/spec_text + 定时）= 167。
#: + 方法库变更查询 1（method_module.changes）= 168。
#: + 测试策略 10（strategy.create/list/get/update/delete + set_cases/assign_case/
#:   record_result/remove_case/cases；冒烟/全量/回归/性能四类，用户定版 2026-10-14）= 178。
#: + 项目/版本级角色成员 1（project.version.roles 读生效角色；角色字段
#:   testers/developers/product_managers 进 project.create/update 与
#:   project.version.create/update 的 params，不新增方法；测试/开发/产品三类，
#:   用户定版 2026-10-15）= 179。
#: + 并发批次 1（环境域等由其他会话持续新增；本行作护栏，每次落库顺手对账）= 180。
#: + 接口变更历史 1（api.changes；entity_type=api 复用 change_record）= 181。
#: + 接口来源 3（api.source.list/sync/delete；来源=绑定的导入方式，一接口多来源，
#:   同步=重抓来源 URL 重导入，用户定版 2026-10-15）= 183。
#: + 数据集删除 1（dataset.delete；删数据集时挂的用例 dataset_id 置空，用例本体不删）= 184。
#: + 缺陷域禅道对齐 7（bug.update/bug.transition/bug.history/bug.enums/
#:   bug.cycle_report/bug.delete/bug.available_actions；状态机 7 态 + 动作守卫表，
#:   用户定版 2026-10-16，见 plans/23-defect-zentao-parity.md）= 191。
#: + 策略统计报告 1（strategy.report；bug 数/P0-P1[priority 1-2]/平均·最长·最短
#:   解决时长/用例数/自动化数/延期分析[计划 end_time vs actual_end_time]，
#:   用户定版 2026-10-16，见 plans/22-test-strategy.md §8）= 192。
#: + 缺陷链方法层 2（case.reject_sink 门② REJECT 原因回流 / case.mark_golden
#:   风格锚标记；25 号文 v4.1 §环节9/§环节11，P2，落 surface/gen_methods.py 尾部）= 201。
#: + 探索门/问题库/上线门/报告受众/生成任务列表/run 波次编排 7（explore.plan、
#:   explore.record、question.sink、release.judge、report.view、genrun.list、
#:   run.execute_waves；25 号文 §环节7/8/10/11 P2 接线）= 208。
EXPECTED_TOTAL = 208


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，注册能力面跨域读写所需模型。

    含任务/资产/报告/缺陷扩展/工作流 + 平台 ``Project``（``bug.create`` 复用
    ``project_service.create_issue``，需 owner 项目）与 ``ProjectIssue``。
    app label 统一 ``testing_platform``（与各模型 ``Meta.app`` 一致）。
    """
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.assets.models",
                "testing_platform.knowledge.models",
                "testing_platform.quality.models",
                "testing_platform.tasks.models",
                "testing_platform.reports.models",
                "testing_platform.defects.models",
                "testing_platform.workflow.models",
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


# ── 1) 注册表：写方法全在，形态合法 ─────────────────────────────
def test_new_write_methods_are_registered_with_valid_contract():
    names = set(methods.default_method_registry().names())
    assert len(names) == EXPECTED_TOTAL, f"注册表应为 {EXPECTED_TOTAL} 方法，实际 {len(names)}"
    for name, kind in {**NEW_WRITE_METHODS, **GAP_WRITE_METHODS, **PROJECT_WRITE_METHODS}.items():
        assert name in names, f"写方法未注册：{name}"
        spec = methods.get_method(name)
        assert spec.kind == kind, f"{name} 的 kind 应为 {kind}，实际 {spec.kind}"
        assert spec.kind in METHOD_KINDS
        schema = spec.params_schema
        assert schema.get("type") == "object", f"{name} params_schema.type 应为 object"
        assert isinstance(spec.returns_schema, dict) and spec.returns_schema
        assert spec.description, f"{name} 缺描述"
        props = set(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        assert set(required) <= props, f"{name} required={required} 不在 properties={sorted(props)} 内"
        assert required, f"{name} 至少应有一个必填参数（写方法都有主键/主体）"


def test_write_methods_kind_annotation_reaches_mcp():
    by_name = {t["name"]: t for t in mcp.list_tools()}
    assert by_name["task.run"]["annotations"]["kind"] == "write"
    assert by_name["workflow.complete"]["annotations"]["kind"] == "write"
    # 旧的查询/执行方法不被动到
    assert by_name["run.execute"]["annotations"]["kind"] == "execute"
    assert by_name["case.list"]["annotations"]["kind"] == "read"


# ── 2) 空参数 → 结构化 invalid_params（一个都不许抛穿）──────────
@pytest.mark.asyncio
async def test_empty_params_are_structured_invalid_params():
    for name in {**NEW_WRITE_METHODS, **GAP_WRITE_METHODS}:
        out = await mcp.call_tool(name, {})
        assert out["isError"] is True, f"{name} 空参数应报错"
        err = out["structuredContent"]
        assert err["code"] == "invalid_params", f"{name} 空参数应归 invalid_params，实际 {err['code']}"
        assert err["details"].get("missing"), f"{name} 应列出缺失必填字段"
        assert isinstance(err["message"], str) and err["message"]


@pytest.mark.asyncio
async def test_unknown_param_rejected_for_task_create():
    out = await mcp.call_tool(
        "task.create",
        {"project_id": str(uuid.uuid4()), "title": "T", "created_by": str(uuid.uuid4()), "titel": "x"},
    )
    assert out["isError"] is True
    assert out["structuredContent"]["code"] == "invalid_params"
    assert out["structuredContent"]["details"]["unknown"] == ["titel"]


# ── 3) task.*：建/改/启停/跑（真建数据）─────────────────────────
@pytest.mark.asyncio
async def test_task_create_update_enable_disable_run(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())

    created = await mcp.call_tool(
        "task.create",
        {
            "project_id": pid,
            "title": "夜间回归",
            "created_by": me,
            "purpose": "验证下单主链路",
            "scenario_tags": {"all": ["smoke"]},
            "exec_type": "manual",
            "notify": {"on": ["fail"], "channels": [{"type": "feishu", "webhook_ref": "x"}]},
        },
    )
    assert created["isError"] is False, created["structuredContent"]
    doc = created["structuredContent"]
    assert doc["code"] == "TASK-0001"
    assert doc["title"] == "夜间回归"
    assert doc["status"] == "draft"
    assert doc["version"] == 1
    task_id = doc["id"]

    # 能 list 到
    listed = await mcp.call_tool("task.list", {"project_id": pid})
    assert listed["isError"] is False
    assert listed["structuredContent"]["count"] == 1
    assert listed["structuredContent"]["tasks"][0]["id"] == task_id

    # 变更门：缺 change_reason → 结构化（与 case.update 一致）
    blocked = await mcp.call_tool("task.update", {"task_id": task_id, "title": "改名"})
    assert blocked["isError"] is True
    err = blocked["structuredContent"]
    assert err["code"] == "change_reason_required"
    assert "change_reason" in err["message"]

    # 带理由 → 过，version 递增
    allowed = await mcp.call_tool(
        "task.update",
        {"task_id": task_id, "change_reason": "范围扩大到支付链路", "title": "支付冒烟回归"},
    )
    assert allowed["isError"] is False, allowed["structuredContent"]
    assert allowed["structuredContent"]["title"] == "支付冒烟回归"
    assert allowed["structuredContent"]["version"] == 2

    # 未知任务 → not_found（不抛穿）
    missing = await mcp.call_tool(
        "task.update",
        {"task_id": str(uuid.uuid4()), "change_reason": "试试", "title": "x"},
    )
    assert missing["isError"] is True
    assert missing["structuredContent"]["code"] == "not_found"

    # 启 / 停
    enabled = await mcp.call_tool("task.enable", {"task_id": task_id})
    assert enabled["isError"] is False
    assert enabled["structuredContent"]["status"] == "enabled"
    disabled = await mcp.call_tool("task.disable", {"task_id": task_id, "change_reason": "暂缓"})
    assert disabled["isError"] is False
    assert disabled["structuredContent"]["status"] == "disabled"

    # 真跑 → pending run
    ran = await mcp.call_tool("task.run", {"task_id": task_id})
    assert ran["isError"] is False, ran["structuredContent"]
    run_doc = ran["structuredContent"]
    assert run_doc["code"] == "RUN-0001"
    assert run_doc["status"] == "pending"
    assert run_doc["trigger"] == "manual"
    assert run_doc["task_id"] == task_id

    runs = await mcp.call_tool("run.list", {"project_id": pid})
    assert runs["structuredContent"]["count"] == 1
    assert runs["structuredContent"]["runs"][0]["status"] == "pending"

    # trigger 枚举在 schema 层就拦
    bad_trigger = await mcp.call_tool("task.run", {"task_id": task_id, "trigger": "once"})
    assert bad_trigger["isError"] is True
    assert bad_trigger["structuredContent"]["code"] == "invalid_params"
    assert bad_trigger["structuredContent"]["details"]["field"] == "trigger"


# ── 4) scenario.update（变更门）+ scenario.create_steps ─────────
@pytest.mark.asyncio
async def test_scenario_update_gate_and_create_steps(tortoise_db):
    pid = str(uuid.uuid4())
    made = await mcp.call_tool(
        "scenario.create", {"project_id": pid, "name": "下单主流程", "purpose": "冒烟"}
    )
    assert made["isError"] is False
    sid = made["structuredContent"]["id"]
    assert made["structuredContent"]["version"] == 1

    # 缺 change_reason → 变更门结构化
    blocked = await mcp.call_tool("scenario.update", {"scenario_id": sid, "purpose": "改"})
    assert blocked["isError"] is True
    assert blocked["structuredContent"]["code"] == "change_reason_required"

    # 带理由 → 过，version+1
    allowed = await mcp.call_tool(
        "scenario.update",
        {"scenario_id": sid, "change_reason": "口径按业务文档纠正", "purpose": "支付冒烟"},
    )
    assert allowed["isError"] is False, allowed["structuredContent"]
    assert allowed["structuredContent"]["purpose"] == "支付冒烟"
    assert allowed["structuredContent"]["version"] == 2

    # 一次建两个步骤 → 返回场景全文（steps 按 seq 升序）
    steps_out = await mcp.call_tool(
        "scenario.create_steps",
        {
            "scenario_id": sid,
            "steps": [
                {
                    "step_key": "create_order",
                    "type": "request",
                    "params": {"method": "POST", "url": "{{env.base_url}}/orders"},
                    "post_checks": [{"type": "status_code", "expected": 201}],
                },
                {
                    "step_key": "verify_order",
                    "type": "request",
                    "params": {"method": "GET", "url": "{{env.base_url}}/orders/1"},
                    "post_checks": [{"type": "status_code", "expected": 200}],
                },
            ],
        },
    )
    assert steps_out["isError"] is False, steps_out["structuredContent"]
    full = steps_out["structuredContent"]
    assert full["id"] == sid
    assert [s["step_key"] for s in full["steps"]] == ["create_order", "verify_order"]
    assert [s["seq"] for s in full["steps"]] == [10, 20]

    # 未知场景 → not_found（不抛穿）
    absent = await mcp.call_tool(
        "scenario.create_steps", {"scenario_id": "SCN-404", "steps": [{"step_key": "x", "type": "request"}]}
    )
    assert absent["isError"] is True
    assert absent["structuredContent"]["code"] == "not_found"


# ── 5) tag.create / case.link_scenario / case.tag ───────────────
@pytest.mark.asyncio
async def test_tag_case_link_and_case_tag(tortoise_db):
    from testing_platform.assets.models import TestCaseScenario, TestCaseTag

    pid, me = str(uuid.uuid4()), str(uuid.uuid4())

    tag = await mcp.call_tool(
        "tag.create",
        {"project_id": pid, "name": "payment", "category": "module", "description": "支付模块"},
    )
    assert tag["isError"] is False, tag["structuredContent"]
    tag_doc = tag["structuredContent"]
    assert tag_doc["code"] == "payment"  # code 缺省取 name
    assert tag_doc["category"] == "module"
    tag_id = tag_doc["id"]

    # category 四选一，schema 层拦
    bad_tag = await mcp.call_tool(
        "tag.create", {"project_id": pid, "name": "x", "category": "severity"}
    )
    assert bad_tag["isError"] is True
    assert bad_tag["structuredContent"]["code"] == "invalid_params"
    assert bad_tag["structuredContent"]["details"]["field"] == "category"

    case = await mcp.call_tool(
        "case.create",
        {"project_id": pid, "title": "下单全流程", "confirm_source": "user_confirm", "created_by": me},
    )
    assert case["isError"] is False
    case_id = case["structuredContent"]["id"]

    scn1 = await mcp.call_tool("scenario.create", {"project_id": pid, "name": "正常流"})
    scn2 = await mcp.call_tool("scenario.create", {"project_id": pid, "name": "异常流"})
    sid1, sid2 = scn1["structuredContent"]["id"], scn2["structuredContent"]["id"]

    link1 = await mcp.call_tool(
        "case.link_scenario",
        {"case_id": case_id, "scenario_id": sid1, "is_primary": True, "order": 10},
    )
    assert link1["isError"] is False, link1["structuredContent"]
    assert link1["structuredContent"]["is_primary"] is True
    assert link1["structuredContent"]["order"] == 10

    # 第二个主场景 → 同用例旧主标记被清（主场景互斥）
    link2 = await mcp.call_tool(
        "case.link_scenario", {"case_id": case_id, "scenario_id": sid2, "is_primary": True}
    )
    assert link2["isError"] is False
    rows = await TestCaseScenario.filter(case_id=uuid.UUID(case_id))
    primaries = [r for r in rows if r.is_primary]
    assert len(primaries) == 1 and str(primaries[0].scenario_id) == sid2

    # 幂等：同 case+scenario 再链一次 → 同一行，不重复
    again = await mcp.call_tool(
        "case.link_scenario", {"case_id": case_id, "scenario_id": sid1, "is_primary": False}
    )
    assert again["isError"] is False
    assert await TestCaseScenario.filter(case_id=uuid.UUID(case_id)).count() == 2

    # 打标（幂等）
    tagged = await mcp.call_tool("case.tag", {"case_id": case_id, "tag_id": tag_id})
    assert tagged["isError"] is False, tagged["structuredContent"]
    assert tagged["structuredContent"] == {"case_id": case_id, "tag_id": tag_id}
    assert await TestCaseTag.filter(case_id=uuid.UUID(case_id)).count() == 1

    # 重复打标仍是一行（get_or_create 幂等）
    await mcp.call_tool("case.tag", {"case_id": case_id, "tag_id": tag_id})
    assert await TestCaseTag.filter(case_id=uuid.UUID(case_id)).count() == 1

    # 未知标签 → not_found
    bad = await mcp.call_tool("case.tag", {"case_id": case_id, "tag_id": str(uuid.uuid4())})
    assert bad["isError"] is True
    assert bad["structuredContent"]["code"] == "not_found"


# ── 6) report.generate：由 run 真生成报告 ───────────────────────
@pytest.mark.asyncio
async def test_report_generate_from_run(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())

    task = await mcp.call_tool(
        "task.create", {"project_id": pid, "title": "支付冒烟", "created_by": me}
    )
    task_id = task["structuredContent"]["id"]
    run = await mcp.call_tool("task.run", {"task_id": task_id})
    run_id = run["structuredContent"]["id"]

    made = await mcp.call_tool(
        "report.generate", {"run_id": run_id, "test_version": "v1.2.3", "generator": "manual"}
    )
    assert made["isError"] is False, made["structuredContent"]
    doc = made["structuredContent"]
    assert doc["code"] == "RPT-0001"
    assert doc["status"] == "ready"
    assert doc["run_id"] == run_id
    assert doc["test_version"] == "v1.2.3"
    assert doc["generator"] == "manual"
    assert "overview" in doc["snapshot"]

    listed = await mcp.call_tool("report.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 1

    # 未知 run → not_found（不抛穿）
    absent = await mcp.call_tool("report.generate", {"run_id": str(uuid.uuid4())})
    assert absent["isError"] is True
    assert absent["structuredContent"]["code"] == "not_found"

    # generator 枚举在 schema 层拦
    bad_gen = await mcp.call_tool("report.generate", {"run_id": run_id, "generator": "bot"})
    assert bad_gen["isError"] is True
    assert bad_gen["structuredContent"]["code"] == "invalid_params"
    assert bad_gen["structuredContent"]["details"]["field"] == "generator"


# ── 7) bug.create（真建 + 指纹去重）+ bug.root_cause（根因门）────
@pytest.mark.asyncio
async def test_bug_create_and_root_cause_gate(tortoise_db):
    pid = str(uuid.uuid4())  # 测试项目 id（解耦：不再需要 coding Project）
    me = uuid.uuid4()
    assignee = str(uuid.uuid4())
    run_sid = str(uuid.uuid4())

    # 标题必填在 schema 层就拦
    no_title = await mcp.call_tool(
        "bug.create",
        {"run_scenario_id": run_sid, "symptom": "500", "project_id": pid},
    )
    assert no_title["isError"] is True
    assert no_title["structuredContent"]["code"] == "invalid_params"
    assert "title" in no_title["structuredContent"]["details"]["missing"]

    created = await mcp.call_tool(
        "bug.create",
        {
            "run_scenario_id": run_sid,
            "title": "下单接口 500",
            "symptom": "POST /orders 返回 500",
            "fingerprint": "FP-" + "c" * 8,
            "repro": {"steps": ["POST /orders"], "env": "staging", "rate": 1},
            "user_id": str(me),
            "project_id": pid,
            "assignee_id": assignee,
            "priority": 1,
        },
    )
    assert created["isError"] is False, created["structuredContent"]
    bug_id = created["structuredContent"]["bug_id"]
    assert bug_id

    # 同 fingerprint 再建 → 归并到同一 bug（去重）
    dedup = await mcp.call_tool(
        "bug.create",
        {
            "run_scenario_id": run_sid,
            "title": "下单接口 500（再次）",
            "symptom": "POST /orders 返回 500",
            "fingerprint": "FP-" + "c" * 8,
            "user_id": str(me),
            "project_id": pid,
            "assignee_id": assignee,
        },
    )
    assert dedup["isError"] is False
    assert dedup["structuredContent"]["bug_id"] == bug_id

    listed = await mcp.call_tool("bug.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 1
    assert listed["structuredContent"]["bugs"][0]["id"] == bug_id

    # 根因门：空 evidence_chain → 结构化 root_cause_evidence_required
    blocked = await mcp.call_tool(
        "bug.root_cause",
        {"bug_id": bug_id, "root_cause": {"classification": "logic", "hypothesis": "空口断言"}},
    )
    assert blocked["isError"] is True, blocked["structuredContent"]
    err = blocked["structuredContent"]
    assert err["code"] == "root_cause_evidence_required"
    assert err["details"]["field"] == "evidence_chain"

    # 带证据链 → 落库返回根因文档
    allowed = await mcp.call_tool(
        "bug.root_cause",
        {
            "bug_id": bug_id,
            "root_cause": {
                "location": "api/orders.py:88",
                "classification": "logic",
                "confidence": 0.9,
                "evidence_chain": [{"step": "create_order", "evidence": "500 response"}],
            },
        },
    )
    assert allowed["isError"] is False, allowed["structuredContent"]
    rc = allowed["structuredContent"]
    assert rc["bug_id"] == bug_id
    assert rc["classification"] == "logic"
    assert rc["evidence_chain"] == [{"step": "create_order", "evidence": "500 response"}]


# ── 8) workflow.start / workflow.complete（真建 + 契约门）────────
@pytest.mark.asyncio
async def test_workflow_start_and_complete(tortoise_db):
    started = await mcp.call_tool(
        "workflow.start",
        {"workflow_def_id": "executor", "input": {"scenarios": [{"name": "下单"}]}},
    )
    assert started["isError"] is False, started["structuredContent"]
    doc = started["structuredContent"]
    assert doc["status"] == "pending"
    assert doc["input"] == {"scenarios": [{"name": "下单"}]}
    run_id = doc["id"]

    # 契约过（executor 只要求 results 数组）→ done
    ok = await mcp.call_tool(
        "workflow.complete", {"run_id": run_id, "output": {"results": [], "summary": {"total": 0}}}
    )
    assert ok["isError"] is False, ok["structuredContent"]
    assert ok["structuredContent"]["status"] == "done"
    assert ok["structuredContent"]["output"]["results"] == []

    # 第二条：契约不过（缺 required results）→ failed + 问题落 output.issues
    pending = await mcp.call_tool("workflow.start", {"workflow_def_id": "executor"})
    bad_run = pending["structuredContent"]["id"]
    failed = await mcp.call_tool("workflow.complete", {"run_id": bad_run, "output": {"oops": True}})
    assert failed["isError"] is False, failed["structuredContent"]
    body = failed["structuredContent"]
    assert body["status"] == "failed"
    assert body["output"]["submitted"] == {"oops": True}
    assert body["output"]["issues"], "契约失败必须把 issues 留档"

    # 未知角色 → not_found（不抛穿）
    absent = await mcp.call_tool("workflow.start", {"workflow_def_id": "architect"})
    assert absent["isError"] is True
    assert absent["structuredContent"]["code"] == "not_found"


# ── 9) 未实现的方法依然不注册（边界不回退）───────────────────────
def test_unimplemented_methods_still_not_registered():
    still_pending = {
        "scenario.validate",
    }
    names = set(methods.default_method_registry().names())
    for name in still_pending:
        assert name not in names, f"未实现的方法被发明了：{name}"
