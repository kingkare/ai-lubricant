"""统一目录表（``mc_test_catalog``）+ 用例三处 schema 增强的落库测试。

覆盖：
* 目录：建 / 列（按 type 收窄）/ 改名换父 / 防环 / 跨类型拒绝 / 删除级联 + 挂的用例归根。
* 用例 ``folder_id``：建/改用例带目录；删目录后用例 folder_id 置空。
* ``test_types`` 多值 + 读侧 coalesce（旧 test_type 补位）。
* ``preconditions`` 列表化：list/dict/str 三种入参都归一成 string[]；document 吐 list。

无 PG 依赖（sqlite::memory:），模式照 ``tests/test_assets_model.py``。
运行：``python -m pytest tests/test_catalog.py -q``
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
    create_catalog,
    create_case,
    delete_catalog,
    get_catalog,
    list_catalogs,
    update_case,
    update_catalog,
)
from testing_platform.surface import mcp  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，app label 用 ``testing_platform``。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.assets.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 目录 CRUD ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_catalog_crud_and_typing(tortoise_db):
    pid = uuid.uuid4()
    root = await create_catalog(project_id=pid, type="case", name="登录")
    assert root.type == "case"
    child = await create_catalog(project_id=pid, type="case", name="密码", parent_id=root.id)
    assert str(child.parent_id) == str(root.id)

    rows = await list_catalogs(pid, "case")
    assert {r.name for r in rows} == {"登录", "密码"}
    # 按 type 收窄：需求目录不串进来
    await create_catalog(project_id=pid, type="requirement", name="下单")
    assert all(r.type == "case" for r in await list_catalogs(pid, "case"))
    assert len(await list_catalogs(pid)) == 3  # 不传 type = 全部

    # 改名 + 换父
    renamed = await update_catalog(child.id, name="口令", parent_id=None)
    assert renamed.name == "口令"
    assert renamed.parent_id is None

    fetched = await get_catalog(child.id)
    assert fetched.name == "口令"


@pytest.mark.asyncio
async def test_catalog_cross_type_and_cycle_rejected(tortoise_db):
    pid = uuid.uuid4()
    case_root = await create_catalog(project_id=pid, type="case", name="用例目录")
    req_root = await create_catalog(project_id=pid, type="requirement", name="需求目录")

    # 跨类型挂靠：拒绝
    with pytest.raises(AssetValidationError):
        await create_catalog(project_id=pid, type="case", name="子", parent_id=req_root.id)

    # 非法 type：拒绝
    with pytest.raises(AssetValidationError):
        await create_catalog(project_id=pid, type="bogus", name="坏")

    # 换父成环：父目录是它自己的子孙
    a = await create_catalog(project_id=pid, type="case", name="A")
    b = await create_catalog(project_id=pid, type="case", name="B", parent_id=a.id)
    with pytest.raises(AssetValidationError):
        await update_catalog(a.id, parent_id=b.id)


@pytest.mark.asyncio
async def test_catalog_delete_cascades_and_unroots_cases(tortoise_db):
    pid = uuid.uuid4()
    me = uuid.uuid4()
    root = await create_catalog(project_id=pid, type="case", name="支付")
    child = await create_catalog(project_id=pid, type="case", name="退款", parent_id=root.id)
    case = await create_case(
        project_id=pid,
        title="退款成功",
        confirm_source="doc",
        created_by=me,
        folder_id=child.id,
    )
    assert str(case.folder_id) == str(child.id)

    deleted = await delete_catalog(root.id)
    assert deleted == 2  # 根 + 子都删
    # 挂的用例 folder_id 置空（归根，用例本体不删）
    from testing_platform.assets import get_case

    after = await get_case(case.id)
    assert after.folder_id is None


@pytest.mark.asyncio
async def test_scenario_folder_id_roundtrip_and_unroot(tortoise_db):
    """场景目录（type=scenario）：建目录 → 场景带 folder_id → 删目录后场景归根（本体不删）。"""
    pid = str(uuid.uuid4())
    root = await create_catalog(project_id=pid, type="scenario", name="下单链路")
    child = await create_catalog(project_id=pid, type="scenario", name="支付", parent_id=root.id)

    from testing_platform.assets import create_scenario, get_scenario, list_scenarios

    scenario = await create_scenario(project_id=pid, name="下单流程", folder_id=child.id)
    assert str(scenario.folder_id) == str(child.id)

    # 按目录收窄 + 全文带回 folder_id
    rows = await list_scenarios(pid, folder_id=child.id)
    assert [s.id for s in rows] == [scenario.id]
    from testing_platform.assets import get_scenario_full

    doc = await get_scenario_full(scenario.id)
    assert doc["folder_id"] == str(child.id)

    # 删根目录：级联删子目录，挂的场景 folder_id 归 NULL（场景本体不删）
    deleted = await delete_catalog(root.id)
    assert deleted == 2
    after = await get_scenario(scenario.id)
    assert after.folder_id is None


# ── 用例 folder_id / test_types / preconditions ──────────────────
@pytest.mark.asyncio
async def test_case_test_types_coalesce(tortoise_db):
    pid, me = uuid.uuid4(), uuid.uuid4()
    c = await create_case(
        project_id=pid,
        title="多类型",
        confirm_source="doc",
        created_by=me,
        test_types=["functional", "api"],
    )
    assert set(c.test_types) == {"functional", "api"}
    # 非法值逐项拒绝
    with pytest.raises(AssetValidationError):
        await create_case(
            project_id=pid, title="坏", confirm_source="doc", created_by=me, test_types=["bogus"]
        )
    # 旧单值补位
    legacy = await create_case(
        project_id=pid, title="旧单值", confirm_source="doc", created_by=me, test_type="smoke"
    )
    assert legacy.test_type == "smoke"


@pytest.mark.asyncio
async def test_case_preconditions_three_shapes(tortoise_db):
    pid, me = uuid.uuid4(), uuid.uuid4()
    # list 入参
    c1 = await create_case(
        project_id=pid, title="列表", confirm_source="doc", created_by=me,
        preconditions=["已登录", "  ", "余额充足"],
    )
    assert c1.preconditions == ["已登录", "余额充足"]
    # dict 入参（旧/AI 形状）→ 拍平成 "k: v"
    c2 = await create_case(
        project_id=pid, title="字典", confirm_source="doc", created_by=me,
        preconditions={"user": "已登录"},
    )
    assert c2.preconditions == ["user: 已登录"]
    # str 入参 → 按行拆
    c3 = await create_case(
        project_id=pid, title="字符串", confirm_source="doc", created_by=me,
        preconditions="A\nB\n\nC",
    )
    assert c3.preconditions == ["A", "B", "C"]
    # 编辑：列表整体替换
    c1b = await update_case(c1.id, change_reason="改", preconditions=["只剩这条"])
    assert c1b.preconditions == ["只剩这条"]


# ── 方法面：case.list 文档形状 + catalog.list ────────────────────
@pytest.mark.asyncio
async def test_mcp_case_document_shapes(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    made = await mcp.call_tool(
        "case.create",
        {
            "project_id": pid,
            "title": "接口用例",
            "confirm_source": "doc",
            "created_by": me,
            "preconditions": ["前置一", "前置二"],
            "test_types": ["api", "security"],
        },
    )
    assert made["isError"] is False
    # _case_document 经 create 返回的是 document（顶层就是文档字段，无 envelope）
    doc = made["structuredContent"]
    assert doc["preconditions"] == ["前置一", "前置二"]
    assert set(doc["test_types"]) == {"api", "security"}

    listed = await mcp.call_tool("case.list", {"project_id": pid})
    assert listed["isError"] is False
    body = listed["structuredContent"]
    assert body["count"] == 1
    assert body["cases"][0]["preconditions"] == ["前置一", "前置二"]


@pytest.mark.asyncio
async def test_mcp_catalog_roundtrip(tortoise_db):
    pid = str(uuid.uuid4())
    created = await mcp.call_tool(
        "catalog.create", {"project_id": pid, "type": "case", "name": "登录"}
    )
    assert created["isError"] is False
    fid = created["structuredContent"]["id"]

    listed = await mcp.call_tool("catalog.list", {"project_id": pid, "type": "case"})
    assert listed["isError"] is False
    assert listed["structuredContent"]["count"] == 1
    assert listed["structuredContent"]["folders"][0]["type"] == "case"

    upd = await mcp.call_tool("catalog.update", {"folder_id": fid, "name": "登录模块"})
    assert upd["isError"] is False
    assert upd["structuredContent"]["name"] == "登录模块"

    deleted = await mcp.call_tool("catalog.delete", {"folder_id": fid})
    assert deleted["isError"] is False
    assert deleted["structuredContent"]["deleted"] == 1


# ── 数据集删除：挂的用例 dataset_id 置空，用例本体不删 ────────────
@pytest.mark.asyncio
async def test_mcp_dataset_delete_unroots_cases(tortoise_db):
    pid, me = str(uuid.uuid4()), str(uuid.uuid4())
    made = await mcp.call_tool(
        "dataset.create",
        {"project_id": pid, "code": "DS-1", "schema": {"product_id": "string"}, "rows": [{"product_id": "p1"}]},
    )
    assert made["isError"] is False
    did = made["structuredContent"]["id"]

    case = await mcp.call_tool(
        "case.create",
        {
            "project_id": pid,
            "title": "参数化用例",
            "confirm_source": "doc",
            "created_by": me,
            "dataset_id": did,
        },
    )
    assert case["isError"] is False
    cid = case["structuredContent"]["id"]
    assert case["structuredContent"]["dataset_id"] == did

    deleted = await mcp.call_tool("dataset.delete", {"dataset_id": did})
    assert deleted["isError"] is False
    assert deleted["structuredContent"]["deleted"] == 1

    # 用例本体不删，dataset_id 置空
    after = await mcp.call_tool("case.get", {"case_id": cid})
    assert after["isError"] is False
    assert after["structuredContent"]["dataset_id"] is None
    # 列表里也没有这个数据集了
    rows = await mcp.call_tool("dataset.list", {"project_id": pid})
    assert rows["structuredContent"]["count"] == 0
