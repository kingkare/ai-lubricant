"""补齐缺口表测试 —— 环境管理（05）+ 疑问（§15.2）+ 自定义工具（§15.5）+ 沉淀（§15.6）。

覆盖（sqlite::memory:，app label 统一 ``testing_platform``）：

1. **7 张缺口表**确实落到 schema（sqlite_master 实证）。
2. **环境 CRUD + 分层继承合并**（本轮核心，05 §3）：
   基线 → 环境组 → 具体环境，读取时**父先子后、子覆盖父**，类型保持原生
   （int 就是 int，05 §4）；密钥变量读取脱敏（05 §5）；
   **循环继承被拒**（自环 / 跨级回指，05 §3「父链不得出现自身」）。
3. **env.update 变更门**：缺 change_reason → ChangeReasonRequiredError。
4. **疑问 raise → resolve**：open 期间阻塞下游（:func:`assert_not_blocked`
   抛 QuestionBlockingError），resolve 后解除；已 resolved 重复 resolve 拒绝。
5. **自定义工具 CRUD**：impl_ref 只能指向 scenario 或 method.full_ref；
   update 走变更门并落版本快照（mc_test_custom_tool_version）。
6. **沉淀 CRUD**：craft.create / list / load（只返 active，按 scope 分层排序）。
7. **surface 方法真跑**：env.*/question.*/custom_tool.*/craft.* 经 MCP 面往返。

运行：``python -m pytest tests/test_assets_env_question.py -q``
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

from testing_platform.assets import (  # noqa: E402
    AssetValidationError,
    ChangeReasonRequiredError,
    EnvironmentCycleError,
    EnvironmentNotFoundError,
    create_custom_tool,
    create_env_variable,
    create_environment,
    delete_environment,
    delete_env_variable,
    get_custom_tool,
    get_env_chain_documents,
    get_environment,
    list_custom_tool_versions,
    list_custom_tools,
    list_environments,
    list_env_variables,
    resolve_environment_vars,
    update_custom_tool,
    update_environment,
    update_env_variable,
)
from testing_platform.assets.models import (  # noqa: E402
    TestChangeRecord,
    TestCustomToolVersion,
    TestEnvironment,
    TestEnvVariable,
)
from testing_platform.quality import (  # noqa: E402
    CraftValidationError,
    QuestionBlockingError,
    QuestionNotFoundError,
    QuestionValidationError,
    assert_not_blocked,
    create_craft,
    get_question,
    list_craft,
    list_questions,
    load_craft,
    raise_question,
    resolve_question,
    update_craft,
)
from testing_platform.quality.models import TestCraftAsset, TestQuestion  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，注册本轮新增的模型模块。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.assets.models",
                "testing_platform.quality.models",
                "testing_platform.knowledge.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 1) 7 张缺口表确实建出来 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_gap_tables_registered(tortoise_db):
    from tortoise import connections

    conn = connections.get("default")
    rows = await conn.execute_query_dict("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    expected = {
        "mc_test_environment",
        "mc_test_env_variable",
        "mc_test_question",
        "mc_test_custom_tool",
        "mc_test_custom_tool_version",
        "mc_test_craft_asset",
        "mc_knowledge_source",
    }
    assert expected <= names, f"缺表：{expected - names}"


# ── 2) 环境 CRUD（05 §2.1）──────────────────────────────────────
@pytest.mark.asyncio
async def test_environment_crud(tortoise_db):
    pid = uuid.uuid4()
    base = await create_environment(project_id=pid, code="base", name="基线")
    assert base.parent_id is None  # 根基线
    assert base.status == "active"
    assert base.version == 1

    got = await get_environment(base.id)
    assert got.id == base.id
    by_code = await get_environment("base")  # code 也可查
    assert by_code.id == base.id

    rows = await list_environments(pid)
    assert [r.code for r in rows] == ["base"]

    # 变更门：缺 change_reason 拦截
    with pytest.raises(ChangeReasonRequiredError):
        await update_environment(base.id, name="改名")
    updated = await update_environment(base.id, change_reason="基线口径调整", name="全局基线")
    assert updated.version == 2
    assert updated.name == "全局基线"
    # 变更必有记录（design §15.12）
    changes = await TestChangeRecord.filter(entity_type="environment", entity_id=base.id)
    assert len(changes) == 1
    assert changes[0].field_changes["name"]["to"] == "全局基线"

    # 未知环境 → EnvironmentNotFoundError
    with pytest.raises(EnvironmentNotFoundError):
        await get_environment(uuid.uuid4())


# ── 3) 分层继承合并：父先子后、子覆盖父（05 §3 核心）─────────────
@pytest.mark.asyncio
async def test_environment_inheritance_merge(tortoise_db):
    pid = uuid.uuid4()
    base = await create_environment(project_id=pid, code="base", name="基线")
    group = await create_environment(project_id=pid, code="staging-group", name="预发组", parent_id=base.id)
    leaf = await create_environment(project_id=pid, code="staging-1", name="预发1", parent_id=group.id)

    # 公共变量在父级
    await create_env_variable(env_id=base.id, key="base_url", value="https://api.x.com", type="string")
    await create_env_variable(env_id=base.id, key="port", value=8080, type="int")
    await create_env_variable(env_id=base.id, key="timeout", value=1.5, type="float")
    await create_env_variable(env_id=base.id, key="debug", value=False, type="boolean")
    # 子环境只配差异（05 §3）
    await create_env_variable(env_id=group.id, key="base_url", value="https://staging.x.com", type="string")
    await create_env_variable(env_id=leaf.id, key="tenant", value="t1", type="string")

    merged = await resolve_environment_vars(leaf.id)
    # 父先子后：base_url 被组覆盖；port/timeout/debug 继承自基线
    assert merged == {
        "base_url": "https://staging.x.com",
        "port": 8080,
        "timeout": 1.5,
        "debug": False,
        "tenant": "t1",
    }
    # 类型保持：int 就是 int（05 §4「port > 8000 这类判断不能错」）
    assert isinstance(merged["port"], int) and not isinstance(merged["port"], bool)
    assert isinstance(merged["timeout"], float)
    assert isinstance(merged["debug"], bool)

    # 根基线自身：只有父层
    assert await resolve_environment_vars(base.id) == {
        "base_url": "https://api.x.com",
        "port": 8080,
        "timeout": 1.5,
        "debug": False,
    }

    # 链文档（引擎 run.execute 的 envs 形态）：根在前、叶在后
    from testing_platform.assets import get_env_chain_documents

    chain = await get_env_chain_documents(leaf.id)
    assert [doc["code"] if "code" in doc else doc["id"] for doc in chain] == [
        str(base.id),
        str(group.id),
        str(leaf.id),
    ]
    assert chain[0]["parent_id"] is None
    assert chain[2]["parent_id"] == str(group.id)


# ── 4) 防循环继承（05 §3「父链不得出现自身」）───────────────────
@pytest.mark.asyncio
async def test_environment_cycle_rejected(tortoise_db):
    pid = uuid.uuid4()
    a = await create_environment(project_id=pid, code="a", name="A")
    b = await create_environment(project_id=pid, code="b", name="B", parent_id=a.id)
    c = await create_environment(project_id=pid, code="c", name="C", parent_id=b.id)

    # 自环：a 的父 = a 自身
    with pytest.raises(EnvironmentCycleError):
        await update_environment(a.id, change_reason="试试自环", parent_id=a.id)

    # 跨级回指：a 的父 = c（会让 c→b→a→c 成环）
    with pytest.raises(EnvironmentCycleError):
        await update_environment(a.id, change_reason="试试回指", parent_id=c.id)

    # 合法改父不受影响（b 挂到 c 下 → 会成环：c→b→c，故同样被拒）
    with pytest.raises(EnvironmentCycleError):
        await update_environment(b.id, change_reason="换父", parent_id=c.id)

    # 合法：把 c 移到另一棵树的根 b2 下
    b2 = await create_environment(project_id=pid, code="b2", name="B2")
    moved = await update_environment(c.id, change_reason="换父到 b2", parent_id=b2.id)
    assert str(moved.parent_id) == str(b2.id)

    # 父级必须同项目
    other = uuid.uuid4()
    foreign = await create_environment(project_id=other, code="foreign", name="他项目")
    with pytest.raises(AssetValidationError):
        await create_environment(project_id=pid, code="bad", name="坏", parent_id=foreign.id)
    with pytest.raises(AssetValidationError):
        await update_environment(a.id, change_reason="跨项目挂父", parent_id=foreign.id)


# ── 5) 环境变量：7 类型 + 密钥脱敏（05 §2.2/§4/§5）───────────────
@pytest.mark.asyncio
async def test_env_variable_types_and_secret_masking(tortoise_db):
    pid = uuid.uuid4()
    env = await create_environment(project_id=pid, code="dev", name="开发")

    await create_env_variable(env_id=env.id, key="token", value="secret-abcd1234", type="string", is_secret=True)
    await create_env_variable(env_id=env.id, key="api_prefix", value="/v1", type="string")
    await create_env_variable(env_id=env.id, key="flags", value=["a", "b"], type="list")
    # dict 已并入 json（同一校验分支）：这里用 json 建对象型变量。
    await create_env_variable(env_id=env.id, key="feature", value={"k": "v"}, type="json")
    await create_env_variable(env_id=env.id, key="cfg", value={"nested": {"n": 1}}, type="json")

    # 读取默认脱敏（05 §5）
    rows = await list_env_variables(env.id)
    by_key = {r.key: r for r in rows}
    assert by_key["token"].value == "***1234"
    assert by_key["api_prefix"].value == "/v1"
    assert by_key["flags"].value == ["a", "b"]
    assert by_key["feature"].value == {"k": "v"}

    # 不脱敏（引擎执行注入用）
    raw = await resolve_environment_vars(env.id, mask_secrets=False)
    assert raw["token"] == "secret-abcd1234"
    assert raw["api_prefix"] == "/v1"

    # 非法类型值拒绝（类型保持的守卫）
    with pytest.raises(AssetValidationError):
        await create_env_variable(env_id=env.id, key="bad_port", value="8080", type="int")
    with pytest.raises(AssetValidationError):
        await create_env_variable(env_id=env.id, key="bad_flag", value="yes", type="boolean")
    with pytest.raises(AssetValidationError):
        await create_env_variable(env_id=env.id, key="bad_type", value=1, type="yaml")

    # 改值 → version 递增
    var = await TestEnvVariable.get(env_id=env.id, key="api_prefix")
    updated = await update_env_variable(var.id, value="/v2")
    assert updated.version == 2
    assert updated.value == "/v2"

    # 用途说明（purpose）：建时可带、读回一致、可改可清空（optional 自由文本）
    pv = await create_env_variable(
        env_id=env.id, key="base_url", value="https://api.x.com", type="string",
        purpose="指向生产 SUT 网关",
    )
    assert pv.purpose == "指向生产 SUT 网关"
    assert (await TestEnvVariable.get(id=pv.id)).purpose == "指向生产 SUT 网关"
    pv2 = await update_env_variable(pv.id, purpose="改指向预发")
    assert pv2.purpose == "改指向预发"
    pv3 = await update_env_variable(pv.id, purpose=None)
    assert pv3.purpose is None

    # 删
    assert await delete_env_variable(var.id) == 1
    assert await delete_env_variable(var.id) == 0

    # 默认环境项目内唯一（切换默认时自动清掉旧默认）
    d1 = await create_environment(project_id=pid, code="d1", name="D1", is_default=True)
    d2 = await create_environment(project_id=pid, code="d2", name="D2", is_default=True)
    assert (await get_environment(d1.id)).is_default is False
    assert (await get_environment(d2.id)).is_default is True
    # 归档不删（05 §6）：归档后可查、可切回
    arch = await update_environment(d1.id, change_reason="下线", status="archived")
    assert arch.status == "archived"
    # active 默认过滤掉 archived 的 d1；含 archived 才见全部
    active_codes = [r.code for r in await list_environments(pid)]
    assert "d1" not in active_codes and "d2" in active_codes and "dev" in active_codes
    all_codes = [r.code for r in await list_environments(pid, include_archived=True)]
    assert set(all_codes) == {"dev", "d1", "d2"}
    assert [r.code for r in await list_environments(pid, status="archived")] == ["d1"]
    # 根链删除：d2 无子可直接删
    assert await delete_environment(d2.id) == 1


# ── 6) 疑问：raise → 阻塞 → resolve → 解除（design §15.2）────────
@pytest.mark.asyncio
async def test_question_raise_blocks_then_resolve(tortoise_db):
    pid = uuid.uuid4()
    case_id = str(uuid.uuid4())

    q = await raise_question(
        project_id=pid,
        origin="product_uncertainty",
        content="退款金额上限到底按什么口径算？",
        raised_by="analyst",
        context={"where": "prd §4.2", "entity": case_id},
        blocking_refs=[case_id],
    )
    assert q.status == "open"
    assert q.resolved_at is None

    # 未解决 → 阻塞下游
    with pytest.raises(QuestionBlockingError) as excinfo:
        await assert_not_blocked(pid, case_id)
    assert str(q.id) in excinfo.value.details["question_ids"]
    assert excinfo.value.details["code"] == "question_blocking"

    # 无关实体不受阻塞
    await assert_not_blocked(pid, str(uuid.uuid4()))

    listed = await list_questions(pid)
    assert [r.id for r in listed] == [q.id]

    # resolve 必填 resolution（枚举）
    with pytest.raises(QuestionValidationError):
        await resolve_question(q.id, resolution="guesswork")
    resolved = await resolve_question(
        q.id, resolution="user_confirm", resolution_detail={"confirmed_by": "PM", "note": "按实付金额"}
    )
    assert resolved.status == "resolved"
    assert resolved.resolution == "user_confirm"
    assert resolved.resolution_detail["confirmed_by"] == "PM"
    assert resolved.resolved_at is not None

    # 解除阻塞
    await assert_not_blocked(pid, case_id)

    # 重复 resolve 拒绝（状态机单向 open → resolved）
    with pytest.raises(QuestionValidationError):
        await resolve_question(q.id, resolution="doc")

    # content 必填 / 未知疑问
    with pytest.raises(QuestionValidationError):
        await raise_question(project_id=pid, origin="design_doubt", content="  ")
    with pytest.raises(QuestionNotFoundError):
        await get_question(uuid.uuid4())


# ── 7) 自定义工具 CRUD（design §15.5）───────────────────────────
@pytest.mark.asyncio
async def test_custom_tool_crud(tortoise_db):
    pid = uuid.uuid4()
    tool = await create_custom_tool(
        project_id=pid,
        code="sign_order",
        purpose="下单前生成签名（沉淀给多个场景复用）",
        signature={"inputs": {"payload": "dict"}, "returns": "str"},
        impl_ref={"kind": "method", "full_ref": "base.crypto.sign"},
    )
    assert tool.status == "draft"
    assert tool.version == 1
    assert tool.impl_ref == {"kind": "method", "full_ref": "base.crypto.sign"}

    # impl_ref 只能指向 scenario 或 method —— 不拥有实现
    with pytest.raises(AssetValidationError):
        await create_custom_tool(
            project_id=pid, code="bad", impl_ref={"kind": "python", "source": "def f(): ..."}
        )
    with pytest.raises(AssetValidationError):
        await create_custom_tool(project_id=pid, code="bad2", impl_ref={"kind": "method"})
    with pytest.raises(AssetValidationError):
        await create_custom_tool(project_id=pid, code="bad3", impl_ref={"kind": "scenario"})

    # 也支持指向公共场景
    scn_tool = await create_custom_tool(
        project_id=pid,
        code="login_flow",
        impl_ref={"kind": "scenario", "id": str(uuid.uuid4())},
        scope="team",
    )
    assert scn_tool.scope == "team"

    rows = await list_custom_tools(pid)
    assert [r.code for r in rows] == ["login_flow", "sign_order"]
    assert [r.code for r in await list_custom_tools(pid, scope="team")] == ["login_flow"]

    # 变更门 + 版本快照
    with pytest.raises(ChangeReasonRequiredError):
        await update_custom_tool(tool.id, status="active")
    updated = await update_custom_tool(
        tool.id,
        change_reason="评审通过，工具上线",
        status="review",
        signature={"inputs": {"payload": "dict", "key": "str"}, "returns": "str"},
    )
    assert updated.version == 2
    assert updated.status == "review"
    versions = await list_custom_tool_versions(tool.id)
    assert len(versions) == 1
    assert versions[0].version == 2
    assert versions[0].snapshot["signature"]["inputs"]["key"] == "str"
    assert versions[0].change_reason == "评审通过，工具上线"

    # code 查询可用
    assert (await get_custom_tool("sign_order")).id == tool.id


# ── 8) 沉淀四件套（design §15.6）────────────────────────────────
@pytest.mark.asyncio
async def test_craft_crud_and_load(tortoise_db):
    conv = await create_craft(
        type="convention",
        target="scenario",
        scope="common",
        content={"text": "步骤必须有后置校验；变量一律走 {{env.*}}"},
    )
    tpl = await create_craft(type="template", target="scenario", scope="framework", content={"steps": [{"type": "request"}]})
    sample = await create_craft(type="sample", target="case", scope="project", content={"title": "下单主链路样例"})
    validator = await create_craft(type="validator", target="tool", scope="project", content={"text": "签名工具校验器"})

    # 枚举越界拒绝
    with pytest.raises(QuestionValidationError):
        await create_craft(type="recipe", target="scenario")
    with pytest.raises(QuestionValidationError):
        await create_craft(type="template", target="report")

    # load：只返 active；按 type+target 读；scope 分层（common→framework→project）
    loaded = await load_craft(type="template", target="scenario")
    assert [a.id for a in loaded] == [tpl.id]

    by_target = await load_craft(target="scenario")
    assert [a.id for a in by_target] == [conv.id, tpl.id]  # common 在前，project 在后

    # 非 active 不进加载面
    await update_craft(validator.id, status="deprecated")
    assert await load_craft(target="tool") == []
    assert await list_craft(type="validator")  # list 仍可查（含非 active）

    listed = await list_craft(target="scenario")
    assert {a.id for a in listed} == {conv.id, tpl.id}
    assert await list_craft(scope="common", type="convention")

    # 空 update 拒绝
    with pytest.raises(CraftValidationError):
        await update_craft(sample.id)

    # version 递增
    bumped = await update_craft(conv.id, content={"text": "改：步骤必须有类型化后置校验"})
    assert bumped.version == 2
    assert bumped.content["text"].startswith("改：")
    assert isinstance(bumped, TestCraftAsset)

    # 表行数核对（sample/validator 仍在库里）
    assert await TestCraftAsset.all().count() == 4

    # 疑问/沉淀模型互不串表
    assert await TestQuestion.all().count() == 0


# ── 9) 环境删除级联 + 有子拒删（05 §6：归档不删是常规，删除需保证链完整）
@pytest.mark.asyncio
async def test_environment_delete_cascade(tortoise_db):
    pid = uuid.uuid4()
    parent = await create_environment(project_id=pid, code="p", name="父")
    child = await create_environment(project_id=pid, code="c", name="子", parent_id=parent.id)
    await create_env_variable(env_id=child.id, key="k", value="v")

    with pytest.raises(AssetValidationError):
        await delete_environment(parent.id)  # 仍作父级

    deleted = await delete_environment(child.id)
    assert deleted >= 1
    # 变量级联删（sqlite 外键级联会把删除行数一并算进返回）
    assert await TestEnvVariable.all().count() == 0
    with pytest.raises(EnvironmentNotFoundError):
        await get_environment(child.id)
    assert await delete_environment(parent.id) == 1


# ════════════════════════════════════════════════════════════════
# surface 面：本轮方法真跑（MCP 协议层）
# ════════════════════════════════════════════════════════════════
@pytest_asyncio.fixture
async def surface_db(tortoise_db):
    """能力面 handler 直连 sqlite（复用 tortoise_db 的 schema）。"""
    yield


@pytest.mark.asyncio
async def test_surface_env_methods(surface_db):
    from testing_platform.surface import mcp

    pid = str(uuid.uuid4())
    created = await mcp.call_tool(
        "env.create", {"project_id": pid, "code": "base", "name": "基线"}
    )
    assert created["isError"] is False, created["structuredContent"]
    base_doc = created["structuredContent"]
    assert base_doc["status"] == "active" and base_doc["version"] == 1

    child = await mcp.call_tool(
        "env.create",
        {"project_id": pid, "code": "staging-1", "name": "预发1", "parent_id": base_doc["id"]},
    )
    assert child["isError"] is False
    assert child["structuredContent"]["parent_id"] == base_doc["id"]

    var = await mcp.call_tool(
        "env.var.create",
        {"env_id": base_doc["id"], "key": "base_url", "value": "https://api.x", "type": "string"},
    )
    assert var["isError"] is False
    assert var["structuredContent"]["value"] == "https://api.x"
    assert var["structuredContent"]["env_id"] == base_doc["id"]

    await mcp.call_tool(
        "env.var.create",
        {"env_id": child["structuredContent"]["id"], "key": "base_url", "value": "https://staging", "type": "string"},
    )
    await mcp.call_tool(
        "env.var.create",
        {"env_id": child["structuredContent"]["id"], "key": "port", "value": 9090, "type": "int"},
    )

    resolved = await mcp.call_tool("env.vars.resolve", {"env_id": child["structuredContent"]["id"]})
    assert resolved["isError"] is False, resolved["structuredContent"]
    assert resolved["structuredContent"]["vars"] == {
        "base_url": "https://staging",
        "port": 9090,
    }

    # 变更门结构化（不抛穿协议面）
    blocked = await mcp.call_tool("env.update", {"env_id": base_doc["id"], "name": "X"})
    assert blocked["isError"] is True
    assert blocked["structuredContent"]["code"] == "change_reason_required"

    # 循环继承 → invalid_request（EnvironmentCycleError 是 ValueError 家族）
    cyc = await mcp.call_tool(
        "env.update",
        {"env_id": base_doc["id"], "change_reason": "试试", "parent_id": child["structuredContent"]["id"]},
    )
    assert cyc["isError"] is True
    assert cyc["structuredContent"]["code"] == "invalid_request"
    assert "成环" in cyc["structuredContent"]["message"]

    # 变量改/删
    upd = await mcp.call_tool("env.var.update", {"var_id": var["structuredContent"]["id"], "value": "/v2"})
    assert upd["isError"] is False
    assert upd["structuredContent"]["version"] == 2
    dele = await mcp.call_tool("env.var.delete", {"var_id": var["structuredContent"]["id"]})
    assert dele["structuredContent"]["deleted"] == 1

    listed = await mcp.call_tool("env.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 2

    bad = await mcp.call_tool("env.update", {"env_id": str(uuid.uuid4()), "change_reason": "x", "name": "y"})
    assert bad["structuredContent"]["code"] == "not_found"

    # 有子 → 删父被拒
    refuse = await mcp.call_tool("env.delete", {"env_id": base_doc["id"]})
    assert refuse["isError"] is True
    assert refuse["structuredContent"]["code"] == "invalid_request"

    # 枚举在 schema 层拦
    bad_type = await mcp.call_tool("env.var.create", {"env_id": base_doc["id"], "key": "k", "type": "yaml"})
    assert bad_type["isError"] is True
    assert bad_type["structuredContent"]["code"] == "invalid_params"


@pytest.mark.asyncio
async def test_surface_question_methods(surface_db):
    from testing_platform.surface import mcp

    pid = str(uuid.uuid4())
    raised = await mcp.call_tool(
        "question.raise",
        {
            "project_id": pid,
            "origin": "design_doubt",
            "content": "这里重试该不该算新 bug？",
            "blocking_refs": ["case-1"],
        },
    )
    assert raised["isError"] is False, raised["structuredContent"]
    doc = raised["structuredContent"]
    assert doc["status"] == "open" and doc["origin"] == "design_doubt"

    no_gate = await mcp.call_tool("question.raise", {"project_id": pid, "content": "缺 origin"})
    assert no_gate["isError"] is True
    assert no_gate["structuredContent"]["details"]["missing"] == ["origin"]

    resolved = await mcp.call_tool(
        "question.resolve",
        {"question_id": doc["id"], "resolution": "debug", "resolution_detail": {"log": "复现三次均 200"}},
    )
    assert resolved["isError"] is False
    body = resolved["structuredContent"]
    assert body["status"] == "resolved"
    assert body["resolution"] == "debug"
    assert body["resolved_at"]

    again = await mcp.call_tool("question.resolve", {"question_id": doc["id"], "resolution": "doc"})
    assert again["isError"] is True
    assert again["structuredContent"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_surface_custom_tool_and_craft_methods(surface_db):
    from testing_platform.surface import mcp

    pid = str(uuid.uuid4())
    tool = await mcp.call_tool(
        "custom_tool.create",
        {
            "project_id": pid,
            "code": "sign_order",
            "purpose": "下单签名",
            "signature": {"inputs": {"payload": "dict"}, "returns": "str"},
            "impl_ref": {"kind": "method", "full_ref": "base.crypto.sign"},
        },
    )
    assert tool["isError"] is False, tool["structuredContent"]
    assert tool["structuredContent"]["code"] == "sign_order"

    impl_bad = await mcp.call_tool(
        "custom_tool.create",
        {"project_id": pid, "code": "bad", "impl_ref": {"kind": "inline", "code": "x"}},
    )
    assert impl_bad["isError"] is True
    assert "scenario" in impl_bad["structuredContent"]["message"]

    listed = await mcp.call_tool("custom_tool.list", {"project_id": pid})
    assert listed["structuredContent"]["count"] == 1

    upd = await mcp.call_tool(
        "custom_tool.update",
        {"tool_id": tool["structuredContent"]["id"], "change_reason": "评审通过", "status": "active"},
    )
    assert upd["isError"] is False
    assert upd["structuredContent"]["version"] == 2

    craft = await mcp.call_tool(
        "craft.create",
        {"type": "convention", "target": "scenario", "scope": "common", "content": {"text": "步骤必须有后置校验"}},
    )
    assert craft["isError"] is False, craft["structuredContent"]
    craft_id = craft["structuredContent"]["id"]

    load_out = await mcp.call_tool("craft.load", {"type": "convention", "target": "scenario"})
    assert load_out["isError"] is False
    assert load_out["structuredContent"]["count"] == 1
    assert load_out["structuredContent"]["assets"][0]["id"] == craft_id
    assert load_out["structuredContent"]["assets"][0]["content"] == {"text": "步骤必须有后置校验"}

    list_out = await mcp.call_tool("craft.list", {"scope": "common"})
    assert list_out["structuredContent"]["count"] == 1
