"""双轨数据源 + 用例扫描（阶段 2）测试。

覆盖（sqlite::memory:）：

1. **A 轨内置适配器**：json / csv / markdown / excel(stdlib 解析) / word(docx) /
   xmind(zen+legacy) → L1 ``mc_knowledge_raw``；content_hash 幂等（重拉不重复）。
2. **B 轨 agent 通道**：无解析器，内容原样落 L1。
3. **case.scan**：excel/csv/json/xmind → ``mc_test_case`` upsert
   （列名中英自动识别 + 显式 column_map + 幂等重扫）。
4. **knowledge.put**：agent 产出落 L2——可测闸/锚点闸/置信度钳制/candidate 默认态。

运行：``python -m pytest tests/test_source_dual_track.py -q``
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import uuid
import zipfile

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.knowledge import models as km  # noqa: E402
from testing_platform.knowledge import source_fetch as sf  # noqa: E402
from testing_platform.knowledge import case_scan as cs  # noqa: E402
from testing_platform.knowledge import agent_ingest as ai  # noqa: E402
from testing_platform.projects import service as psvc  # noqa: E402
from testing_platform.assets import models as am  # noqa: E402


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
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _minimal_xlsx(rows: list[list[str]]) -> bytes:
    """手工造一个最小 xlsx（sharedStrings + 一张 sheet），无需 openpyxl。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        shared = sorted({c for row in rows for c in row})
        sst = "".join(f'<si><t>{c}</t></si>' for c in shared)
        zf.writestr("xl/sharedStrings.xml",
                    f'<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared)}" uniqueCount="{len(shared)}">{sst}</sst>')
        zf.writestr("xl/workbook.xml",
                    '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                    '<sheets><sheet name="用例" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        sheet_rows = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, val in enumerate(row):
                ref = f"{chr(65 + c)}{r}"
                idx = shared.index(val)
                cells.append(f'<c r="{ref}" t="s"><v>{idx}</v></c>')
            sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData>' + "".join(sheet_rows) + '</sheetData></worksheet>')
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '</Types>')
    return buf.getvalue()


def _minimal_docx(paragraphs: list[str]) -> bytes:
    """手工造一个最小 docx（只有段落）。"""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml",
                    f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def _minimal_xmind_zen(tree: dict) -> bytes:
    """手工造一个 XMind Zen 包（content.json）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps([{"id": "s1", "title": "登录", "rootTopic": tree}]))
    return buf.getvalue()


# ════════════════ 1) A 轨内置适配器 ════════════════

@pytest.mark.asyncio
async def test_builtin_json_and_idempotent(tortoise_db):
    proj = await psvc.create_project(name="源项目")
    data = json.dumps({"cases": [{"title": "登录成功"}]}).encode()
    out1 = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                               adapter_type="json", data=data, title="用例.json")
    assert out1["created"] is True
    assert out1["parsed"] == {"parser": "json"}
    assert out1["raw"]["content_keys"] == ["cases"]

    # 重拉同一份内容 → 幂等，不产生新行
    out2 = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                               adapter_type="json", data=data, title="用例.json")
    assert out2["created"] is False
    assert out2["id"] == out1["id"]
    assert await km.KnowledgeRaw.all().count() == 1


@pytest.mark.asyncio
async def test_builtin_csv_text_and_markdown(tortoise_db):
    proj = await psvc.create_project(name="源项目2")
    csv_data = "标题,预期\n登录成功,跳首页\n".encode("utf-8-sig")
    out = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                              adapter_type="csv", data=csv_data)
    assert out["created"] is True
    raw = await km.KnowledgeRaw.get(id=out["id"])
    assert raw.content["rows"][0] == ["标题", "预期"]
    assert raw.content_text.startswith("标题,预期")

    md = "# PRD\n登录要快".encode()
    out_md = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                 adapter_type="markdown", data=md)
    raw_md = await km.KnowledgeRaw.get(id=out_md["id"])
    assert raw_md.content == {}
    assert "PRD" in raw_md.content_text


@pytest.mark.asyncio
async def test_builtin_excel_parser(tortoise_db):
    proj = await psvc.create_project(name="excel 项目")
    xlsx = _minimal_xlsx([
        ["用例标题", "预期结果", "优先级"],
        ["登录成功", "跳转首页", "P0"],
        ["密码错误", "提示错误", "P1"],
    ])
    out = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                              adapter_type="excel", data=xlsx)
    assert out["parsed"] == {"parser": "excel"}
    raw = await km.KnowledgeRaw.get(id=out["id"])
    sheet = raw.content["sheets"]["用例"]
    assert sheet["rows"][0] == ["用例标题", "预期结果", "优先级"]
    assert sheet["rows"][1] == ["登录成功", "跳转首页", "P0"]


@pytest.mark.asyncio
async def test_builtin_word_parser(tortoise_db):
    proj = await psvc.create_project(name="word 项目")
    docx = _minimal_docx(["登录模块 PRD", "", "账号密码登录"])
    out = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                              adapter_type="word", data=docx)
    raw = await km.KnowledgeRaw.get(id=out["id"])
    assert raw.content["paragraphs"][0] == "登录模块 PRD"
    assert "登录模块 PRD" in raw.content_text


@pytest.mark.asyncio
async def test_builtin_xmind_parser(tortoise_db):
    proj = await psvc.create_project(name="xmind 项目")
    tree = {
        "id": "root", "title": "登录模块",
        "children": {"attached": [
            {"id": "a", "title": "正常登录", "children": {"attached": [
                {"id": "a1", "title": "正确密码登录成功", "notes": "跳转首页", "labels": ["P0"]},
            ]}},
            {"id": "b", "title": "异常登录", "children": {"attached": [
                {"id": "b1", "title": "密码错误提示", "notes": "toast 提示", "labels": ["P1"]},
            ]}},
        ]},
    }
    out = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                              adapter_type="xmind", data=_minimal_xmind_zen(tree))
    assert out["parsed"] == {"parser": "xmind"}
    raw = await km.KnowledgeRaw.get(id=out["id"])
    root = raw.content["sheets"][0]["root"]
    assert root["title"] == "登录模块"
    leaf = root["children"]["attached"][0]["children"]["attached"][0]
    assert leaf["title"] == "正确密码登录成功"
    assert "正确密码登录成功" in raw.content_text


@pytest.mark.asyncio
async def test_agent_track_no_parser(tortoise_db):
    """B 轨：agent 取回的内容原样落 L1（json 可解则结构化，否则纯文本）。"""
    proj = await psvc.create_project(name="agent 项目")
    payload = json.dumps({"doc": "飞书 PRD 内容"}).encode()
    out = await sf.ingest_raw(project_id=proj.id, adapter="agent",
                              adapter_type="feishu", data=payload,
                              locator="https://feishu.cn/doc/x")
    assert out["created"] is True
    assert out["parsed"] is None  # B 轨不解析
    raw = await km.KnowledgeRaw.get(id=out["id"])
    assert raw.adapter == "agent"
    assert raw.content == {"doc": "飞书 PRD 内容"}

    # 非 JSON 内容 → content_text
    out2 = await sf.ingest_raw(project_id=proj.id, adapter="agent",
                               adapter_type="feishu", data="纯文本 PRD".encode())
    raw2 = await km.KnowledgeRaw.get(id=out2["id"])
    assert raw2.content_text == "纯文本 PRD"


@pytest.mark.asyncio
async def test_empty_data_rejected(tortoise_db):
    proj = await psvc.create_project(name="空项目")
    with pytest.raises(sf.SourceFetchError):
        await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                            adapter_type="json", data=b"")


# ════════════════ 2) case.scan ════════════════

@pytest.mark.asyncio
async def test_scan_excel_cases_upsert(tortoise_db):
    """excel 用例表 → 抽用例 → upsert；重扫更新不重复。"""
    proj = await psvc.create_project(name="扫描项目")
    owner = uuid.uuid4()
    xlsx = _minimal_xlsx([
        ["用例标题", "预期结果", "优先级", "标签"],
        ["登录成功", "跳转首页", "P0", "冒烟,登录"],
        ["密码错误", "提示错误", "P1", "异常"],
        ["", "没标题不算用例", "", ""],
    ])
    fetched = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                  adapter_type="excel", data=xlsx, locator="cases.xlsx")
    result = await cs.scan_cases(raw_id=fetched["id"], project_id=proj.id, created_by=owner)
    assert result["extracted"] == 2  # 无标题行跳过
    assert result["created"] == 2
    cases = await am.TestCase.all()
    titles = {c.title for c in cases}
    assert titles == {"登录成功", "密码错误"}
    by_title = {c.title: c for c in cases}
    login = by_title["登录成功"]
    assert login.confirm_source == "doc"
    assert login.confirm_ref["external_key"] == "row-0000"
    assert login.confirm_ref["raw_id"] == fetched["id"]
    assert login.priority == "P0"

    # 重扫同一份 → 幂等（unchanged=2，不重复建）
    result2 = await cs.scan_cases(raw_id=fetched["id"], project_id=proj.id, created_by=owner)
    assert result2["created"] == 0 and result2["unchanged"] == 2
    assert await am.TestCase.all().count() == 2

    # 同一份文件内容变了（同 locator）→ 重扫更新而非重复建
    xlsx_v2 = _minimal_xlsx([
        ["用例标题", "预期结果", "优先级", "标签"],
        ["登录成功", "跳转首页并显示用户名", "P0", "冒烟,登录"],
        ["密码错误", "提示错误", "P1", "异常"],
    ])
    fetched2 = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                   adapter_type="excel", data=xlsx_v2, locator="cases.xlsx")
    result3 = await cs.scan_cases(raw_id=fetched2["id"], project_id=proj.id, created_by=owner)
    # 同 scan_scope（locator 相同）+ 同 external_key → 更新已有行，绝不新建
    assert result3["created"] == 0
    assert await am.TestCase.all().count() == 2
    updated_case = await am.TestCase.get(title="登录成功")
    assert updated_case.expected == {"value": "跳转首页并显示用户名"}
    # 内容变的行被更新；confirm_ref.raw_id 也随新快照刷新
    assert updated_case.confirm_ref["raw_id"] == fetched2["id"]
    assert updated_case.confirm_ref["scan_scope"] == "locator:cases.xlsx"


@pytest.mark.asyncio
async def test_scan_csv_with_column_map(tortoise_db):
    proj = await psvc.create_project(name="csv 扫描")
    csv_data = "name,expected_result\n下单成功,订单创建\n".encode()
    fetched = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                  adapter_type="csv", data=csv_data, locator="a.csv")
    # 英文列名也能自动识别（expected_result 别名）
    result = await cs.scan_cases(raw_id=fetched["id"], project_id=proj.id, created_by=uuid.uuid4())
    assert result["created"] == 1
    case = await am.TestCase.first()
    assert case.title == "下单成功"

    # 显式 column_map 覆盖（另一份文件 → 不同 scan_scope，独立建）
    csv2 = "自定义列,结果\n标题甲,预期乙\n".encode()
    fetched2 = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                   adapter_type="csv", data=csv2, locator="b.csv")
    result2 = await cs.scan_cases(raw_id=fetched2["id"], project_id=proj.id,
                                  created_by=uuid.uuid4(),
                                  column_map={"自定义列": "title", "结果": "expected"})
    assert result2["created"] == 1
    assert await am.TestCase.filter(title="标题甲").exists()


@pytest.mark.asyncio
async def test_scan_xmind_cases(tortoise_db):
    proj = await psvc.create_project(name="xmind 扫描")
    tree = {
        "id": "root", "title": "登录模块",
        "children": {"attached": [
            {"id": "a", "title": "正常登录", "children": {"attached": [
                {"id": "a1", "title": "正确密码登录成功", "notes": "跳转首页", "labels": ["P0"]},
            ]}},
        ]},
    }
    fetched = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                  adapter_type="xmind", data=_minimal_xmind_zen(tree))
    result = await cs.scan_cases(raw_id=fetched["id"], project_id=proj.id, created_by=uuid.uuid4())
    assert result["created"] == 1
    case = await am.TestCase.first()
    assert case.title == "正确密码登录成功"
    assert case.priority == "P0"
    assert case.module == "登录模块"
    assert case.feature == "正常登录"


@pytest.mark.asyncio
async def test_scan_word_returns_nothing(tortoise_db):
    """word/markdown 是 PRD 源不是用例源 → 抽出空集。"""
    proj = await psvc.create_project(name="word 扫描")
    fetched = await sf.ingest_raw(project_id=proj.id, adapter="builtin",
                                  adapter_type="word",
                                  data=_minimal_docx(["这是 PRD 不是用例"]))
    result = await cs.scan_cases(raw_id=fetched["id"], project_id=proj.id, created_by=uuid.uuid4())
    assert result["extracted"] == 0


@pytest.mark.asyncio
async def test_scan_project_mismatch(tortoise_db):
    proj1 = await psvc.create_project(name="项目A")
    proj2 = await psvc.create_project(name="项目B")
    fetched = await sf.ingest_raw(project_id=proj1.id, adapter="builtin",
                                  adapter_type="json", data=b'{"cases":[]}')
    with pytest.raises(cs.CaseScanError):
        await cs.scan_cases(raw_id=fetched["id"], project_id=proj2.id, created_by=uuid.uuid4())


# ════════════════ 3) knowledge.put（agent 落地）════════════════

@pytest.mark.asyncio
async def test_knowledge_put_gates_and_cap(tortoise_db):
    """可测闸/锚点闸/置信度钳制/candidate 默认态/幂等。"""
    proj = await psvc.create_project(name="agent 落地")
    pid = proj.id

    # 可通过的 rule（有 condition+expected）+ 可解析锚点
    anchor = await km.KnowledgeAnchor.create(
        project_id=pid, kind="function", path="api/auth.py", symbol="login"
    )
    items = [
        {
            "kind": "rule",
            "key": "login-success",
            "title": "登录成功规则",
            "content": {"condition": "密码正确", "expected": "跳转首页"},
            "confidence": 0.9,
            "anchors": [{"path": "api/auth.py", "symbol": "login"}],
        },
        {
            "kind": "rule",
            "key": "login-lock",
            "title": "锁定规则（缺 expected → 可测闸不过）",
            "content": {"condition": "连续失败 5 次"},
            "confidence": 0.99,
        },
        {
            "kind": "api_endpoint",
            "key": "post-login",
            "content": {"method": "POST", "path": "/api/v1/login"},
            "anchors": [{"path": "不存在的路径.py"}],
        },
    ]
    out = await ai.put_items(project_id=pid, items=items, agent="dsh-analyst")
    assert out["created"] == 3
    docs = {d["key"]: d for d in out["items"]}

    # 1) 正常条目：candidate + 置信度 + 锚点解析成功
    d1 = docs["login-success"]
    assert d1["status"] == "candidate"
    assert d1["confidence"] == 0.9
    assert d1["anchors_resolved"] == [str(anchor.id)]
    assert d1["testable"] is True

    # 2) 缺 expected → 可测闸 gate
    d2 = docs["login-lock"]
    assert d2["testable"] is False
    gated_keys = {g["key"] for g in out["gated"]}
    assert "login-lock" in gated_keys
    row2 = await km.KnowledgeItem.get(kind="rule", key="login-lock")
    assert row2.content["_gate"]["testability"]
    # 置信度钳到 0.95（AI 不得自封 1.0 档）
    assert row2.confidence == 0.95

    # 3) 锚点解析不到 → 记录不阻断
    d3 = docs["post-login"]
    assert d3["anchors_resolved"] == []
    assert d3["anchors_unresolved"][0]["path"] == "不存在的路径.py"
    row3 = await km.KnowledgeItem.get(kind="api_endpoint", key="post-login")
    assert row3.content["_gate"]["unresolved_anchors"]
    assert row3.anchor_id is None

    # 幂等：重跑 → unchanged（内容没变不重写版本）
    out2 = await ai.put_items(project_id=pid, items=items, agent="dsh-analyst")
    assert out2["created"] == 0 and out2["unchanged"] == 3

    # AI 产出一律 candidate，即使声称 confirmed
    out3 = await ai.put_items(project_id=pid, items=[{
        "kind": "rule", "key": "self-promote",
        "content": {"condition": "x", "expected": "y"},
        "confidence": 1.0, "status": "confirmed",  # 试图自封
    }])
    row = await km.KnowledgeItem.get(kind="rule", key="self-promote")
    assert row.status == "candidate"
    assert row.confidence == 0.95


@pytest.mark.asyncio
async def test_knowledge_put_links(tortoise_db):
    """links → mc_knowledge_edge（item→item / item→anchor）。"""
    proj = await psvc.create_project(name="边项目")
    pid = proj.id
    anchor = await km.KnowledgeAnchor.create(
        project_id=pid, kind="endpoint", path="api/x.py", symbol="do"
    )
    out = await ai.put_items(project_id=pid, items=[
        {"kind": "rule", "key": "r1", "content": {"condition": "a", "expected": "b"},
         "links": [
             {"to_kind": "rule", "to_key": "r2", "relation": "depends_on"},
             {"to_kind": "anchor", "path": "api/x.py", "symbol": "do", "relation": "implemented_by"},
             {"to_kind": "rule", "to_key": "ghost", "relation": "references"},  # 目标不存在 → 跳过
         ]},
        {"kind": "rule", "key": "r2", "content": {"condition": "c", "expected": "d"}},
    ])
    edges = await km.KnowledgeEdge.all()
    assert len(edges) == 2  # ghost 被跳过
    relations = {e.relation for e in edges}
    assert relations == {"depends_on", "implemented_by"}


@pytest.mark.asyncio
async def test_knowledge_put_validation(tortoise_db):
    proj = await psvc.create_project(name="校验项目")
    with pytest.raises(ai.KnowledgePutError):
        await ai.put_items(project_id=proj.id, items=[])
    with pytest.raises(ai.KnowledgePutError):
        await ai.put_items(project_id=proj.id, items=[{"kind": "rule"}])  # 缺 key
    with pytest.raises(ai.KnowledgePutError):
        await ai.put_items(project_id=proj.id, items=[{"kind": "rule", "key": "k"}],
                           source_id="not-a-uuid")
