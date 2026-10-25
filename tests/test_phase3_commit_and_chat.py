"""阶段 3 收口 + 覆盖矩阵边 + 对话持久化测试。

覆盖（sqlite::memory:）：

1. **chat.service**：会话 CRUD / 消息追加（seq 单调）/ dsh_session 回写 / 归档删除。
2. **knowledge.put 覆盖矩阵边**：锚点闸通过 → 自动建 ``item --grounded_in--> anchor`` 边。
3. **analyst.commit**：items 落地 + 工作流契约收口（done）+ gaps 疑问落库。

运行：``.venv/Scripts/python.exe -m pytest tests/test_phase3_commit_and_chat.py -q``
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

from testing_platform.chat import models as cm  # noqa: E402
from testing_platform.chat import service as chat_svc  # noqa: E402
from testing_platform.knowledge import models as km  # noqa: E402
from testing_platform.knowledge import agent_ingest as ai  # noqa: E402


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
                "testing_platform.workflow.models",
                "testing_platform.chat.models",
                "testing_platform.quality.models",
                "testing_platform.tasks.models",
                "testing_platform.reports.models",
                "testing_platform.defects.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ════════════════ 1) chat 持久化 ════════════════

@pytest.mark.asyncio
async def test_chat_session_and_messages(tortoise_db):
    pid = uuid.uuid4()
    owner = uuid.uuid4()
    session = await chat_svc.create_session(project_id=pid, model="claude-haiku", api_key_id=42, created_by=owner)
    assert session.status == "active"
    assert session.message_count == 0

    # 首条 user 消息 → 自动当标题
    m1 = await chat_svc.append_message(session.id, role="user", content="为登录设计用例")
    assert m1.seq == 1
    await session.refresh_from_db()
    assert session.title == "为登录设计用例"
    assert session.message_count == 1

    # assistant + tool 消息，seq 单调递增
    m2 = await chat_svc.append_message(session.id, role="assistant", content="", status="streaming")
    m3 = await chat_svc.append_tool_message(session.id, tool_name="mcp__testing__case.create", payload={"a": 1})
    assert (m1.seq, m2.seq, m3.seq) == (1, 2, 3)
    assert m3.role == "tool"
    assert m3.tool_name == "mcp__testing__case.create"

    # 流式收尾回写
    await chat_svc.update_message(m2.id, content="已设计 3 条", status="done")
    rows = await chat_svc.list_messages(session.id)
    assert [r.seq for r in rows] == [1, 2, 3]
    assert rows[1].content == "已设计 3 条"
    assert rows[1].status == "done"

    # dsh session 回写
    await chat_svc.attach_dsh_session(session.id, "session-abc")
    session2 = await chat_svc.get_session(session.id)
    assert session2.dsh_session_id == "session-abc"

    # 列表 + 归档
    listed = await chat_svc.list_sessions(pid)
    assert len(listed) == 1
    await chat_svc.archive_session(session.id)
    assert (await chat_svc.get_session(session.id)).status == "archived"


@pytest.mark.asyncio
async def test_chat_incremental_messages_and_delete(tortoise_db):
    pid = uuid.uuid4()
    s = await chat_svc.create_session(project_id=pid)
    await chat_svc.append_message(s.id, role="user", content="a")
    await chat_svc.append_message(s.id, role="assistant", content="b")
    await chat_svc.append_message(s.id, role="user", content="c")

    # after_seq 增量拉
    partial = await chat_svc.list_messages(s.id, after_seq=1)
    assert [r.seq for r in partial] == [2, 3]

    await chat_svc.delete_session(s.id)
    with pytest.raises(chat_svc.ChatNotFoundError):
        await chat_svc.get_session(s.id)
    assert await cm.TestChatMessage.filter(session_id=s.id).count() == 0


@pytest.mark.asyncio
async def test_chat_validation(tortoise_db):
    pid = uuid.uuid4()
    s = await chat_svc.create_session(project_id=pid)
    with pytest.raises(chat_svc.ChatValidationError):
        await chat_svc.append_message(s.id, role="ghost", content="x")
    with pytest.raises(chat_svc.ChatNotFoundError):
        await chat_svc.get_session(uuid.uuid4())


# ════════════════ 2) knowledge.put 覆盖矩阵边 ════════════════

@pytest.mark.asyncio
async def test_knowledge_put_grounded_edges(tortoise_db):
    pid = uuid.uuid4()
    anchor = await km.KnowledgeAnchor.create(
        project_id=pid, kind="function", path="api/auth.py", symbol="login"
    )
    out = await ai.put_items(project_id=pid, items=[
        {
            "kind": "rule", "key": "login-success",
            "content": {"condition": "密码正确", "expected": "跳首页"},
            "anchors": [{"path": "api/auth.py", "symbol": "login"}],
        },
        {
            "kind": "rule", "key": "un-anchored",
            "content": {"condition": "x", "expected": "y"},
            "anchors": [{"path": "不存在.py"}],
        },
    ])
    # 锚点闸通过 → grounded_in 边
    docs = {d["key"]: d for d in out["items"]}
    assert docs["login-success"]["anchor_edges"] == 1
    assert docs["un-anchored"]["anchor_edges"] == 0

    edges = await km.KnowledgeEdge.filter(project_id=pid, relation="grounded_in")
    assert len(edges) == 1
    assert edges[0].from_kind == "item"
    assert edges[0].to_kind == "anchor"
    assert edges[0].to_id == anchor.id

    # 幂等重跑 → grounded_in 边不重复（get_or_create）
    await ai.put_items(project_id=pid, items=[{
        "kind": "rule", "key": "login-success",
        "content": {"condition": "密码正确", "expected": "跳首页"},
        "anchors": [{"path": "api/auth.py", "symbol": "login"}],
    }])
    assert await km.KnowledgeEdge.filter(project_id=pid, relation="grounded_in").count() == 1


# ════════════════ 3) analyst.commit 自动收口 ════════════════

@pytest.mark.asyncio
async def test_analyst_commit_via_surface(tortoise_db):
    from testing_platform.surface import mcp

    pid = str(uuid.uuid4())
    # 直接开一条 analyst 运行（不走 analyst.prepare —— 那会试取源）
    from testing_platform.workflow import start_run

    run = await start_run("analyst", {"project_id": pid, "source_id": "src-1"})

    out = await mcp.call_tool("analyst.commit", {
        "run_id": str(run.id),
        "project_id": pid,
        "items": [
            {"kind": "rule", "key": "r1", "content": {"condition": "a", "expected": "b"}},
        ],
        "gaps": [{"content": "空库存到底返回什么？"}],
        "agent": "dsh",
        "raise_questions": True,
    })
    assert out["isError"] is False, out["structuredContent"]
    body = out["structuredContent"]
    assert body["workflow_status"] == "done"
    assert body["put"]["created"] == 1

    # 知识条目落了 L2
    item = await km.KnowledgeItem.get(project_id=uuid.UUID(pid), kind="rule", key="r1")
    assert item.status == "candidate"

    # gap → 疑问落库（一等公民）
    from testing_platform.quality.models import TestQuestion

    q = await TestQuestion.filter(project_id=uuid.UUID(pid)).first()
    assert q is not None
    assert q.origin == "design_doubt"

    # 工作流运行 = done，output 契约合法（有 knowledge_model + gaps）
    from testing_platform.workflow import get_run

    done = await get_run(run.id)
    assert done.status == "done"
    assert "knowledge_model" in done.output
    assert done.output["gaps"] == [{"content": "空库存到底返回什么？"}]


@pytest.mark.asyncio
async def test_analyst_commit_contract_failure(tortoise_db):
    """items 非法 → put 报错；gaps 缺失 → 没办法过契约（required 校验在 complete_run）。"""
    from testing_platform.surface import mcp
    from testing_platform.workflow import start_run

    run = await start_run("analyst", {"project_id": str(uuid.uuid4())})
    # 非法 item（缺 kind/key）→ knowledge.put 拒收，方法返回结构化错误
    out = await mcp.call_tool("analyst.commit", {
        "run_id": str(run.id),
        "project_id": str(uuid.uuid4()),
        "items": [{"content": {}}],
        "gaps": [],
    })
    assert out["isError"] is True