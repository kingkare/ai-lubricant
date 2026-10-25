"""测试点 + 用例关联的行为验证（26 号文 §2.2）。

验三处 2026-10-22 后端增量：
1. ``testpoint.list`` 的 ``project_id`` 入口（原方法只有 run_id/requirement_ids）
2. ``testpoint.get`` 新增（UUID 或 TP-xxxx code）
3. ``case.list`` 带 ``testpoint_id`` 过滤（测试点详情/覆盖导图用）

照 ``tests/test_assets_env_question.py`` 的 sqlite::memory: 基线，不连 PG。
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.generation.models",
                "testing_platform.assets.models",
                "testing_platform.quality.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _seed():
    """建一个测试点 + 两条用例挂它，返回 (project_id, requirement_id, tp)。"""
    from testing_platform.assets.models import TestCase
    from testing_platform.generation.models import TestTestpoint

    pid = uuid.uuid4()
    rid = uuid.uuid4()
    tp = await TestTestpoint.create(
        code="TP-0001", project_id=pid, requirement_id=rid, title="下单成功",
        surfaces=["api", "web"], coverage_axis=["C01"], basis="fact", status="confirmed",
    )
    await TestCase.create(
        code="TC-0001", project_id=pid, title="正常下单", testpoint_id=tp.id,
        confirm_source="debug", created_by=pid, status="active",
    )
    await TestCase.create(
        code="TC-0002", project_id=pid, title="越权下单", testpoint_id=tp.id,
        confirm_source="debug", created_by=pid, status="active",
    )
    return pid, rid, tp


@pytest.mark.asyncio
async def test_testpoint_list项目级入口(tortoise_db):
    from testing_platform.surface.methods import call_method

    pid, _, tp = await _seed()
    res = await call_method("testpoint.list", {"project_id": str(pid)})
    assert res["count"] == 1
    assert res["items"][0]["code"] == "TP-0001"
    assert res["items"][0]["surfaces"] == ["api", "web"]


@pytest.mark.asyncio
async def test_testpoint_list按需求过滤(tortoise_db):
    from testing_platform.surface.methods import call_method

    pid, rid, _ = await _seed()
    res = await call_method("testpoint.list", {"requirement_ids": [str(rid)]})
    assert res["count"] == 1


@pytest.mark.asyncio
async def test_testpoint_list无入口报错(tortoise_db):
    from testing_platform.surface.methods import call_method

    with pytest.raises(Exception):
        await call_method("testpoint.list", {"status": "confirmed"})


@pytest.mark.asyncio
async def test_testpoint_get按UUID(tortoise_db):
    from testing_platform.surface.methods import call_method

    _, _, tp = await _seed()
    doc = await call_method("testpoint.get", {"testpoint_id": str(tp.id)})
    assert doc["title"] == "下单成功"
    assert doc["basis"] == "fact"


@pytest.mark.asyncio
async def test_testpoint_get按code(tortoise_db):
    from testing_platform.surface.methods import call_method

    _, _, tp = await _seed()
    doc = await call_method("testpoint.get", {"testpoint_id": "TP-0001"})
    assert doc["id"] == str(tp.id)


@pytest.mark.asyncio
async def test_testpoint_get不存在报not_found(tortoise_db):
    from testing_platform.surface.methods import MethodError, call_method

    with pytest.raises(MethodError):
        await call_method("testpoint.get", {"testpoint_id": "TP-9999"})


@pytest.mark.asyncio
async def test_case_list按testpoint_id过滤(tortoise_db):
    from testing_platform.surface.methods import call_method

    pid, _, tp = await _seed()
    res = await call_method("case.list", {"project_id": str(pid), "testpoint_id": str(tp.id)})
    assert res["count"] == 2
    codes = {c["code"] for c in res["cases"]}
    assert codes == {"TC-0001", "TC-0002"}
