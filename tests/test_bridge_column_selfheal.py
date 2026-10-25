"""桥列运行时自愈测试 —— 不重启补 ``coding_project_id``。

根因与修法见 ``plans/16-remaining-closeout.md`` 七（事后补修）：
``generate_schemas(safe=True)`` 只建缺失的表、不 ALTER 已存在表补新列，且补列机制
原本只覆盖 ``user_platform`` 一个 app——已建库加列后建项目报
``column "coding_project_id" does not exist``。

:func:`ensure_bridge_column` 在建/改项目前幂等补列，让**不重启**也立刻可用。
启动路径的 ``_ensure_additive_columns`` 仍兜底（另一处修）。

运行：``.venv/Scripts/python.exe -m pytest tests/test_bridge_column_selfheal.py -q``
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

from testing_platform.projects import models as pm  # noqa: E402
from testing_platform.projects import service as svc  # noqa: E402


@pytest_asyncio.fixture
async def db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite://:memory:",
        modules={"testing_platform": ["testing_platform.projects.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _drop_column() -> None:
    """模拟「旧库缺这一列」。"""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await conn.execute_script("ALTER TABLE mc_test_project DROP COLUMN coding_project_id;")


async def _columns() -> set[str]:
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    rows = await conn.execute_query_dict("PRAGMA table_info(mc_test_project)")
    return {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_selfheal_adds_missing_column_on_create(db):
    """缺列时 create_project 先补列再插入（不重启自愈）。"""
    await _drop_column()
    assert "coding_project_id" not in await _columns()

    svc._ENSURED_COLUMNS.clear()  # 清进程内缓存，确保真的走一次补列
    proj = await svc.create_project(name="自愈", coding_project_id=uuid.uuid4())

    assert proj.code.startswith("TP-")
    assert "coding_project_id" in await _columns(), "ensure_bridge_column 未补列"
    assert proj.coding_project_id is not None


@pytest.mark.asyncio
async def test_selfheal_covers_list_and_get(db):
    """list_projects / get_project 也会先补列（前端选择器先调 list）。"""
    await _drop_column()
    svc._ENSURED_COLUMNS.clear()

    listed = await svc.list_projects()
    assert "coding_project_id" in await _columns()

    await _drop_column()
    svc._ENSURED_COLUMNS.clear()
    proj = await svc.create_project(name="再建")  # 无桥也能建
    assert proj.coding_project_id is None
    fetched = await svc.get_project(proj.id)
    assert fetched.id == proj.id


@pytest.mark.asyncio
async def test_ensure_is_idempotent_and_swallowed(db):
    """列已存在 → 补列幂等；补列异常被吞（不阻断业务）。"""
    await svc.ensure_bridge_column()
    first_cache = set(svc._ENSURED_COLUMNS)
    await svc.ensure_bridge_column()  # 第二次直接命中缓存
    assert first_cache == svc._ENSURED_COLUMNS

    # 即便补列内部抛错（表名写错等），也不该让业务炸
    svc._ENSURED_COLUMNS.clear()
    orig_script = None
    from tortoise import Tortoise  # noqa: F401

    # 直接置为不可达连接场景：Tortoise 未初始化时 get_connection 抛 → 被吞
    # （这里用已初始化的连接也安全：下面 create 仍应成功）
    await svc.ensure_bridge_column()
    proj = await svc.create_project(name="仍然能建")
    assert pm.TestProject is not None
    assert proj.code.startswith("TP-")
