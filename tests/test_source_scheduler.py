"""源同步调度器 + webhook 测试（sync_policy=scheduled 的 cron 触发 + webhook）。

覆盖（sqlite::memory:）：

1. **find_due_sources**：cron 到点触发（水位起算）、未到点不触发、非法 schedule 跳过。
2. **run_source_sync**：A 轨文件 → fetch_file + 水位回写；B 轨 → skipped；
   失败也回写水位（不疯狂重试）。
3. **webhook 路由**：secret 校验（配了必须匹配）、触发同步。
4. **sync-due 路由**：鉴权门。

运行：``.venv/Scripts/python.exe -m pytest tests/test_source_scheduler.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

import httpx  # noqa: E402

from testing_platform.projects import service as psvc  # noqa: E402
from testing_platform.projects import models as pm  # noqa: E402
from testing_platform import scheduler as ss  # noqa: E402


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
        # 调度器后台任务必须先停，否则跨测试事件循环会挂死 pytest
        try:
            from testing_platform import scheduler as _sched
            await _sched.stop()
        except Exception:
            pass
        await Tortoise.close_connections()


@pytest.fixture
def surface_app():
    from fastapi import FastAPI

    from testing_platform.surface.api import router

    app = FastAPI()
    app.include_router(router)
    return app


# ════════════════ 1) find_due_sources ════════════════

@pytest.mark.asyncio
async def test_find_due_sources_cron(tortoise_db):
    """水位在触发点之前 → 到点；同步后水位推到触发点之后 → 不再 due。"""
    proj = await psvc.create_project(name="定时项目")
    now = datetime(2026, 10, 4, 10, 0, 0)

    # 每天 9 点：水位设成昨天 8 点 → next_fire=今天 9 点 ≤ now → 到点
    src1 = await psvc.create_source(
        project_id=proj.id, kind="prd", name="每日 PRD",
        sync_policy="scheduled", schedule={"kind": "cron", "expr": "0 9 * * *"},
    )
    src1.created_at = datetime(2026, 10, 3, 8, 0, 0)
    await src1.save()

    # 每 30 分钟：水位 1 小时前 → 到点
    src2 = await psvc.create_source(
        project_id=proj.id, kind="case", name="每半小时",
        sync_policy="scheduled", schedule={"kind": "interval", "minutes": 30},
    )
    src2.created_at = now - timedelta(hours=1)
    await src2.save()

    # 手动源 → 永不进定时扫描
    await psvc.create_source(
        project_id=proj.id, kind="case", name="手动源", sync_policy="manual",
    )

    due = await ss.find_due_sources(now=now)
    due_ids = {str(s.id) for s in due}
    assert str(src1.id) in due_ids
    assert str(src2.id) in due_ids
    assert len(due) == 2

    # 同步 src1 → 水位推到真实 now；验证水位已更新（防疯狂重试的机制基础）
    await ss.run_source_sync(src1)
    refreshed = await pm.TestProjectSource.get(id=src1.id)
    assert refreshed.last_synced_at is not None
    # 水位（真实 now）之后的下一次触发（明天 9 点）必然 > 水位 → 用真实 now 再扫不 due
    due2 = await ss.find_due_sources()  # now=真实当前时刻 ≥ 水位
    assert str(src1.id) not in {str(s.id) for s in due2}


@pytest.mark.asyncio
async def test_find_due_sources_new_source_not_immediately_due(tortoise_db):
    """新源（水位=创建时刻）不该立刻触发——cron 语义：建在 10 点的"每天 9 点"，
    首个触发点是明天 9 点。"""
    proj = await psvc.create_project(name="新源")
    now = datetime(2026, 10, 4, 10, 0, 0)
    src = await psvc.create_source(
        project_id=proj.id, kind="prd", name="九点源",
        sync_policy="scheduled", schedule={"kind": "cron", "expr": "0 9 * * *"},
    )
    src.created_at = now  # 刚创建
    await src.save()
    due = await ss.find_due_sources(now=now)
    assert str(src.id) not in {str(s.id) for s in due}


@pytest.mark.asyncio
async def test_find_due_sources_bad_schedule_skipped(tortoise_db):
    proj = await psvc.create_project(name="坏配置")
    now = datetime(2026, 10, 4, 10, 0, 0)
    src = await psvc.create_source(
        project_id=proj.id, kind="prd", name="坏 cron",
        sync_policy="scheduled", schedule={"kind": "cron", "expr": "not-a-cron"},
    )
    src.created_at = now - timedelta(days=1)
    await src.save()
    due = await ss.find_due_sources(now=now)
    assert due == []  # 非法 schedule 跳过，不炸整轮


# ════════════════ 2) run_source_sync ════════════════

@pytest.mark.asyncio
async def test_run_source_sync_builtin_file(tortoise_db, tmp_path):
    proj = await psvc.create_project(name="A 轨定时")
    f = tmp_path / "cases.json"
    f.write_text('{"cases": [{"title": "登录"}]}', encoding="utf-8")
    src = await psvc.create_source(
        project_id=proj.id, kind="case", name="JSON 源",
        adapter="builtin", adapter_type="json",
        config={"path": str(f)},
        sync_policy="scheduled", schedule={"kind": "interval", "minutes": 60},
    )
    result = await ss.run_source_sync(src)
    assert result["ok"] is True
    assert result["action"] == "fetch_file"
    assert result["created"] is True

    # 水位回写
    refreshed = await pm.TestProjectSource.get(id=src.id)
    assert refreshed.last_synced_at is not None
    assert refreshed.last_sync_result.get("ok") is True


@pytest.mark.asyncio
async def test_run_source_sync_agent_skipped(tortoise_db):
    proj = await psvc.create_project(name="B 轨定时")
    src = await psvc.create_source(
        project_id=proj.id, kind="prd", name="飞书",
        adapter="agent", adapter_type="feishu",
        sync_policy="scheduled", schedule={"kind": "interval", "minutes": 60},
    )
    result = await ss.run_source_sync(src)
    assert result["ok"] is True  # skipped 也算"同步过"（水位回写防疯狂重试）
    assert result["action"] == "skipped"


@pytest.mark.asyncio
async def test_run_source_sync_failure_writes_watermark(tortoise_db):
    proj = await psvc.create_project(name="失败源")
    src = await psvc.create_source(
        project_id=proj.id, kind="case", name="坏路径",
        adapter="builtin", adapter_type="json",
        config={"path": "Z:/不存在的路径/xx.json"},
        sync_policy="scheduled",
    )
    result = await ss.run_source_sync(src)
    assert result["ok"] is False
    # 失败也回写水位（否则到点疯狂重试）
    refreshed = await pm.TestProjectSource.get(id=src.id)
    assert refreshed.last_synced_at is not None
    assert refreshed.last_sync_result.get("ok") is False


# ════════════════ 3) webhook 路由 ════════════════

@pytest.mark.asyncio
async def test_webhook_secret_gate(tortoise_db, surface_app):
    proj = await psvc.create_project(name="webhook 项目")
    src = await psvc.create_source(
        project_id=proj.id, kind="code", name="带 secret",
        adapter="builtin", adapter_type="git",
        config={"webhook_secret": "s3cret"},
        sync_policy="webhook",
    )
    transport = httpx.ASGITransport(app=surface_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # secret 不匹配 → 401
        r = await c.post(f"/api/v1/testing/source/{src.id}/webhook",
                         json={"commit": "abc"}, headers={"X-Webhook-Secret": "wrong"})
        assert r.status_code == 401

        # 匹配 → 触发（code 源无 git_id → skipped 但 200）
        r2 = await c.post(f"/api/v1/testing/source/{src.id}/webhook",
                          json={"commit": "abc"}, headers={"X-Webhook-Secret": "s3cret"})
        assert r2.status_code == 200
        assert r2.json()["ok"] is True


@pytest.mark.asyncio
async def test_sync_due_requires_auth(tortoise_db, surface_app):
    transport = httpx.ASGITransport(app=surface_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/v1/testing/source/sync-due")
        assert r.status_code == 401  # 无登录 → 401


@pytest.mark.asyncio
async def test_ensure_started_gate(tortoise_db):
    """无定时源 → ensure_started 返回 False 且不拉后台任务；有定时源 → True 且幂等。

    fixture 的 finally 会 ``stop()`` 掉后台任务（跨测试事件循环挂死的根因）。
    """
    from testing_platform import scheduler as sched

    await sched.stop()
    sched._started = False

    # 无源 → 不启动
    assert await sched.ensure_started() is False
    assert sched.SourceSyncScheduler._task is None

    # 建一个定时源 → 启动
    proj = await psvc.create_project(name="闸门项目")
    await psvc.create_source(
        project_id=proj.id, kind="prd", name="定时",
        sync_policy="scheduled", schedule={"kind": "interval", "minutes": 60},
    )
    assert await sched.ensure_started() is True
    task1 = sched.SourceSyncScheduler._task
    assert task1 is not None

    # 幂等：第二次 ensure 不重复建任务
    assert await sched.ensure_started() is True
    assert sched.SourceSyncScheduler._task is task1

    await sched.stop()
    assert sched.SourceSyncScheduler._task is None
