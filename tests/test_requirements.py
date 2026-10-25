"""需求（主脊）测试 —— 需求 CRUD + 各表 requirement_id 挂接 + requirement.trace。

sqlite 内存，模式照 ``tests/test_assets_model.py``。
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

from testing_platform.assets import models as am  # noqa: E402
from testing_platform.assets import service as svc  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.assets.models",
                "testing_platform.defects.models",
                "testing_platform.projects.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _project() -> uuid.UUID:
    return uuid.uuid4()


@pytest.mark.asyncio
async def test_requirement_crud_and_code(tortoise_db):
    pid = _project()
    r = await svc.create_requirement(
        project_id=pid,
        title="空库存下单返回 409",
        description="下单时库存为 0 应拒绝",
        acceptance_criteria=[{"text": "返回 HTTP 409"}],
        priority="P0",
        module="下单",
    )
    assert r.code.startswith("REQ-")
    assert r.status == "draft"
    assert r.acceptance_criteria == [{"text": "返回 HTTP 409"}]

    got = await svc.get_requirement(r.id)
    assert got.title == "空库存下单返回 409"

    # 改需求走变更门
    with pytest.raises(svc.ChangeReasonRequiredError):
        await svc.update_requirement(r.id, title="新标题")
    updated = await svc.update_requirement(r.id, title="新标题", change_reason="澄清")
    assert updated.title == "新标题"

    listed = await svc.list_requirements(pid)
    assert [x.code for x in listed] == [r.code]

    await svc.delete_requirement(r.id)
    assert len(await svc.list_requirements(pid)) == 0


@pytest.mark.asyncio
async def test_requirement_code_autoincrement(tortoise_db):
    pid = _project()
    r1 = await svc.create_requirement(project_id=pid, title="A")
    r2 = await svc.create_requirement(project_id=pid, title="B")
    assert r1.code == "REQ-0001"
    assert r2.code == "REQ-0002"


@pytest.mark.asyncio
async def test_entities_link_to_requirement(tortoise_db):
    """用例/场景/接口都带 requirement_id 挂到需求。"""
    pid = _project()
    req = await svc.create_requirement(project_id=pid, title="登录")

    case = await svc.create_case(
        project_id=pid, title="登录-正确密码", confirm_source="doc",
        created_by=uuid.uuid4(), requirement_id=req.id,
    )
    assert str(case.requirement_id) == str(req.id)

    scenario = await svc.create_scenario(
        project_id=pid, name="登录流", requirement_id=req.id,
    )
    assert str(scenario.requirement_id) == str(req.id)

    transport = await svc.create_transport(
        project_id=pid, code="endpoint_post_login", requirement_id=req.id,
    )
    assert str(transport.requirement_id) == str(req.id)

    # 未挂需求的实体 requirement_id 为空
    case2 = await svc.create_case(
        project_id=pid, title="未挂", confirm_source="doc", created_by=uuid.uuid4(),
    )
    assert case2.requirement_id is None


@pytest.mark.asyncio
async def test_bug_links_to_requirement(tortoise_db):
    from testing_platform.defects import service as dsvc

    pid = _project()
    req = await svc.create_requirement(project_id=pid, title="支付")
    bug_id = await dsvc.create_bug_from_failure(
        title="支付超时", project_id=pid, requirement_id=req.id,
    )
    bug = await dsvc.get_bug(bug_id)
    assert bug["requirement_id"] == str(req.id)
    assert bug["code"].startswith("BUG-")


@pytest.mark.asyncio
async def test_requirement_delete_nulls_children(tortoise_db):
    """删需求：挂到它的用例 requirement_id 置空，用例本身不删。"""
    pid = _project()
    req = await svc.create_requirement(project_id=pid, title="X")
    case = await svc.create_case(
        project_id=pid, title="C", confirm_source="doc",
        created_by=uuid.uuid4(), requirement_id=req.id,
    )
    await svc.delete_requirement(req.id)
    fresh = await svc.get_case(case.id)
    assert fresh.requirement_id is None


@pytest.mark.asyncio
async def test_requirement_trace_and_radial_graph(tortoise_db):
    """requirement.trace 聚合产出；project.radial_graph 分层数据。"""
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as m

    proj = await psvc.create_project(name="平台回归")
    pid = proj.id
    req = await svc.create_requirement(project_id=pid, title="下单")
    await svc.create_case(
        project_id=pid, title="空库存", confirm_source="doc",
        created_by=uuid.uuid4(), requirement_id=req.id,
    )
    await svc.create_transport(project_id=pid, code="endpoint_post_orders", requirement_id=req.id)

    trace = await m.requirement_trace({"requirement_id": str(req.id)})
    assert trace["requirement"]["code"] == req.code
    assert len(trace["cases"]) == 1
    assert len(trace["transports"]) == 1

    radial = await m.project_radial_graph({"project_id": str(pid)})
    assert radial["project"]["id"] == str(pid)
    assert len(radial["requirements"]) == 1
    # 产出叶子都挂在需求上
    assert all(lf["requirement_id"] == str(req.id) for lf in radial["leaves"])
    assert {lf["type"] for lf in radial["leaves"]} == {"case", "transport"}


# ── 富文本正文 / 思维导图 / 附件（需求 = 文档）──────────────────────

def test_doc_to_text_extracts_plain_text():
    """doc_to_text：ProseMirror 文档 → 纯文本（块级换行、行内拼接）。"""
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 1}, "content": [{"type": "text", "text": "库存下单"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "空库存返回 "},
                {"type": "text", "text": "409", "marks": [{"type": "bold"}]},
            ]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "拒绝下单"}]}]}]},
        ],
    }
    text = svc.doc_to_text(doc)
    assert "库存下单" in text
    assert "空库存返回 409" in text
    assert "拒绝下单" in text
    # 块级之间换行
    assert text.splitlines()[0] == "库存下单"
    # 空/非法输入回退空串
    assert svc.doc_to_text(None) == ""
    assert svc.doc_to_text({}) == ""


@pytest.mark.asyncio
async def test_requirement_content_derives_description(tortoise_db):
    """写 content 时后端派生 description 纯文本投影（真相源=content）。"""
    pid = _project()
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "库存为 0 应拒绝下单"}]}]}
    r = await svc.create_requirement(project_id=pid, title="下单", content=doc)
    assert r.content["type"] == "doc"
    assert "库存为 0 应拒绝下单" in (r.description or "")

    # update 带 content：description 同步派生
    doc2 = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "改为库存不足返回 409"}]}]}
    r2 = await svc.update_requirement(
        r.id, change_reason="细化判据", content=doc2, mindmap={"root": {"id": "n1", "text": "下单", "children": []}}
    )
    assert "改为库存不足返回 409" in (r2.description or "")
    assert r2.mindmap["root"]["text"] == "下单"

    # 不带 content 的更新不动 description
    r3 = await svc.update_requirement(r.id, change_reason="改标题", title="下单-改")
    assert "改为库存不足返回 409" in (r3.description or "")


@pytest.mark.asyncio
async def test_requirement_attachment_crud(tortoise_db):
    """需求附件：add/list/delete（URL 由 OSS 直传得到，本层只存元数据）。"""
    from testing_platform.surface import methods as m

    pid = _project()
    req = await svc.create_requirement(project_id=pid, title="带附件的需求")

    added = await m.requirement_attachment_add({
        "requirement_id": str(req.id),
        "name": "PRD.docx",
        "url": "https://oss.example.com/prd.docx",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size_bytes": 12345,
        "kind": "docx",
    })
    aid = added["item"]["id"]
    assert added["item"]["kind"] == "docx"

    listed = await m.requirement_attachment_list({"requirement_id": str(req.id)})
    assert listed["count"] == 1
    assert listed["items"][0]["name"] == "PRD.docx"

    deleted = await m.requirement_attachment_delete({"attachment_id": aid})
    assert deleted["deleted"] is True
    after = await m.requirement_attachment_list({"requirement_id": str(req.id)})
    assert after["count"] == 0


@pytest.mark.asyncio
async def test_requirement_folder_catalog(tortoise_db):
    """需求目录：建/挂/删目录，删目录时需求 folder_id 归 NULL（同用例口径）。"""
    pid = _project()
    # 建一个需求目录（type=requirement）
    folder = await svc.create_catalog(project_id=pid, type="requirement", name="登录")
    sub = await svc.create_catalog(project_id=pid, type="requirement", name="密码", parent_id=folder.id)
    assert str(sub.parent_id) == str(folder.id)

    # 需求挂到子目录
    r = await svc.create_requirement(project_id=pid, title="登录失败锁定", folder_id=sub.id)
    assert str(r.folder_id) == str(sub.id)
    # 目录收窄只列 requirement 类型
    req_catalogs = await svc.list_catalogs(pid, "requirement")
    assert {c.name for c in req_catalogs} == {"登录", "密码"}

    # 删父目录（级联删子目录；挂的需求 folder_id 归 NULL）
    deleted = await svc.delete_catalog(folder.id)
    assert deleted >= 2
    fresh = await svc.get_requirement(r.id)
    assert fresh.folder_id is None
    assert (await svc.list_catalogs(pid, "requirement")) == []


def test_requirement_catalog_path_and_tree():
    """buildRequirementTree / catalogPath 的等价 Python 端核对（数据形态）。"""
    folders = [
        {"id": "f1", "project_id": "p", "type": "requirement", "parent_id": None, "name": "登录", "order": 0},
        {"id": "f2", "project_id": "p", "type": "requirement", "parent_id": "f1", "name": "密码", "order": 0},
    ]
    # catalogPath 等价逻辑：沿 parent_id 上溯拼路径
    by_id = {f["id"]: f for f in folders}
    segs: list[str] = []
    cur = by_id["f2"]
    while cur:
        segs.insert(0, cur["name"])
        cur = by_id[cur["parent_id"]] if cur["parent_id"] else None
    assert "/".join(segs) == "登录/密码"
