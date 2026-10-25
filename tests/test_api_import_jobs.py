"""接口导入任务测试（``mc_test_api_import_job`` + 执行器）。

覆盖：任务 CRUD、``schedule`` 校验（manual/hourly/daily/weekly + time/weekday）、
定时必须 spec_url、``next_run_at`` 推导、``due_import_jobs`` 到期扫描、
``run_import_job`` 真跑（text 任务）并写 ``last_*``。

运行：``python -m pytest tests/test_api_import_jobs.py -q``（sqlite::memory:，无 PG、无外网）。
"""
from __future__ import annotations

import datetime as dt
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
    create_import_job,
    delete_import_job,
    due_import_jobs,
    get_import_job,
    list_import_jobs,
    mark_import_job_run,
    update_import_job,
)
from testing_platform.assets.service import _next_run_at, _normalize_schedule  # noqa: E402
from testing_platform.importers.import_jobs import resolve_job_spec, run_import_job  # noqa: E402

MINIMAL_OPENAPI = (
    '{"openapi":"3.0.0","paths":{'
    '"/x":{"get":{"summary":"x","tags":["x"],"responses":{"200":{"description":"ok"}}}},'
    '"/y":{"post":{"summary":"y","tags":["y"],"responses":{"201":{"description":"c"}}}}}}'
)


@pytest_asyncio.fixture
async def tortoise_db():
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


# ── schedule 校验与推导（纯函数）────────────────────────────────
def test_schedule_normalize_validates_kind_and_fields():
    assert _normalize_schedule({"kind": "manual"}) == {"kind": "manual"}
    assert _normalize_schedule({"kind": "cron", "expr": "0 3 * * *"}) == {
        "kind": "cron", "expr": "0 3 * * *",
    }
    assert _normalize_schedule({"kind": "interval", "minutes": 30}) == {
        "kind": "interval", "minutes": 30,
    }
    assert _normalize_schedule({"kind": "once", "at": "2099-01-01T09:00:00"}) == {
        "kind": "once", "at": "2099-01-01T09:00:00",
    }
    with pytest.raises(AssetValidationError):
        _normalize_schedule({"kind": "cron"})
    with pytest.raises(AssetValidationError):
        _normalize_schedule({"kind": "interval"})  # 缺 minutes
    with pytest.raises(AssetValidationError):
        _normalize_schedule({"kind": "cron", "expr": "bad expr"})


def test_next_run_at_per_kind():
    base = dt.datetime(2026, 10, 14, 10, 30, tzinfo=dt.timezone.utc)  # 周三
    assert _next_run_at({"kind": "manual"}, from_time=base) is None
    interval = _next_run_at({"kind": "interval", "minutes": 30}, from_time=base)
    assert interval == dt.datetime(2026, 10, 14, 11, 0, tzinfo=dt.timezone.utc)
    daily = _next_run_at({"kind": "cron", "expr": "0 3 * * *"}, from_time=base)
    assert daily == dt.datetime(2026, 10, 15, 3, 0, tzinfo=dt.timezone.utc)
    daily_ahead = _next_run_at({"kind": "cron", "expr": "0 23 * * *"}, from_time=base)
    assert daily_ahead == dt.datetime(2026, 10, 14, 23, 0, tzinfo=dt.timezone.utc)
    weekly = _next_run_at({"kind": "cron", "expr": "0 2 * * 1"}, from_time=base)
    assert weekly == dt.datetime(2026, 10, 19, 2, 0, tzinfo=dt.timezone.utc)  # 下个周一


# ── 任务 CRUD + 校验 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_and_list_job(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="daily petstore", source_format="openapi3",
        spec_url="https://petstore.example.com/v3/api-docs",
        schedule={"kind": "cron", "expr": "0 3 * * *"},
    )
    assert job.enabled and job.next_run_at is not None
    rows = await list_import_jobs(pid)
    assert [r.id for r in rows] == [job.id]


@pytest.mark.asyncio
async def test_scheduled_job_requires_url(tortoise_db):
    pid = str(uuid.uuid4())
    with pytest.raises(AssetValidationError):
        await create_import_job(
            project_id=pid, name="bad", source_format="auto",
            spec_text=MINIMAL_OPENAPI, schedule={"kind": "interval", "minutes": 30},
        )


@pytest.mark.asyncio
async def test_manual_job_requires_some_source(tortoise_db):
    pid = str(uuid.uuid4())
    with pytest.raises(AssetValidationError):
        await create_import_job(project_id=pid, name="empty", source_format="auto")


@pytest.mark.asyncio
async def test_update_reschedules_and_toggles(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="j", source_format="har",
        spec_url="https://x/har.json", schedule={"kind": "cron", "expr": "0 3 * * *"},
    )
    assert job.next_run_at is not None
    paused = await update_import_job(job.id, {"enabled": False})
    assert paused.enabled is False and paused.next_run_at is None
    back = await update_import_job(job.id, {"enabled": True})
    assert back.next_run_at is not None
    renamed = await update_import_job(job.id, {"name": "改名", "source_format": "auto"})
    assert renamed.name == "改名" and renamed.source_format == "auto"
    with pytest.raises(AssetValidationError):
        await update_import_job(job.id, {"source_format": "grpc"})


@pytest.mark.asyncio
async def test_delete_job(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(project_id=pid, name="j", spec_url="https://x/a.json")
    assert await delete_import_job(job.id) == 1
    assert await list_import_jobs(pid) == []


# ── 执行 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_run_text_job_records_result(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="once", source_format="openapi3",
        spec_text=MINIMAL_OPENAPI, schedule={"kind": "manual"},
    )
    res = await run_import_job(job.id)
    assert res["created"] == 2 and res["format"] == "openapi3"
    fresh = await get_import_job(job.id)
    assert fresh.last_status == "ok" and fresh.last_run_at is not None
    assert fresh.last_result["created"] == 2
    assert "endpoints" not in fresh.last_result  # 大清单不落任务行


@pytest.mark.asyncio
async def test_run_dry_run_does_not_record(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="preview", source_format="openapi3",
        spec_text=MINIMAL_OPENAPI, schedule={"kind": "manual"},
    )
    res = await run_import_job(job.id, dry_run=True)
    assert res["dry_run"] is True and res["total"] == 2
    fresh = await get_import_job(job.id)
    assert fresh.last_status is None


@pytest.mark.asyncio
async def test_run_failure_records_error(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="broken", source_format="openapi3",
        spec_text="not json at all {{{", schedule={"kind": "manual"},
    )
    with pytest.raises(Exception):
        await run_import_job(job.id)
    fresh = await get_import_job(job.id)
    assert fresh.last_status == "error" and fresh.last_message


@pytest.mark.asyncio
async def test_resolve_job_spec_prefers_text_else_raises(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="t", source_format="auto",
        spec_text=MINIMAL_OPENAPI, schedule={"kind": "manual"},
    )
    assert "/x" in await resolve_job_spec(job)

    # 清空来源后 → 抛错（URL 抓不到内容时同理）
    from testing_platform.importers import OpenAPIImportError

    cleared = await update_import_job(job.id, {"spec_text": None, "spec_url": None})
    assert cleared.spec_text is None and cleared.spec_url is None
    with pytest.raises(OpenAPIImportError):
        await resolve_job_spec(cleared)


@pytest.mark.asyncio
async def test_due_jobs_scans_overdue(tortoise_db):
    from tortoise import Tortoise

    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="overdue", source_format="openapi3",
        spec_url="https://x/a.json", schedule={"kind": "interval", "minutes": 30},
    )
    assert not await due_import_jobs()
    await Tortoise.get_connection("default").execute_query(
        "UPDATE mc_test_api_import_job SET next_run_at=? WHERE id=?",
        [dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc), str(job.id)],
    )
    due = await due_import_jobs()
    assert [j.id for j in due] == [job.id]


@pytest.mark.asyncio
async def test_mark_run_recomputes_next(tortoise_db):
    pid = str(uuid.uuid4())
    job = await create_import_job(
        project_id=pid, name="j", spec_url="https://x/a.json",
        schedule={"kind": "interval", "minutes": 30},
    )
    first = job.next_run_at
    await mark_import_job_run(job.id, status="ok", message="done", result={"created": 1})
    again = await get_import_job(job.id)
    assert again.last_status == "ok" and again.last_run_at is not None
    # 重算：同一小时内的两次计算会得到同一个整点（>=），跨过整点才会推后
    assert again.next_run_at is not None and again.next_run_at >= first
