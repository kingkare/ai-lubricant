"""``mc_test_method_module.kind`` 删列迁移回归（§5.1 一脚本=一类重构）。

老库的 ``kind`` 列是 ``NOT NULL``，新代码 INSERT 不再带它 → 撞 NOT NULL 直接
IntegrityError（线上实测）。启动迁移 ``_drop_method_module_kind()`` 必须：
- 列存在 → 发 ``ALTER TABLE ... DROP COLUMN IF EXISTS kind``；
- 列不存在（新库 / 已迁过）→ 什么都不做（幂等）。

用假连接脚本化 information_schema 的答案，不连 PG（同 ``test_ddns.py`` 范式）。
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)


class _MigrateConn:
    """Fake Tortoise connection scripting the information_schema answers."""

    def __init__(self, columns: list[dict]):
        self._columns = columns
        self.scripts: list[str] = []

    async def execute_query_dict(self, sql, values=None):
        if "information_schema" in sql:
            return self._columns
        return []

    async def execute_script(self, sql):
        self.scripts.append(sql)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "columns,expect_drop",
    [
        ([], False),  # 新库：无 kind 列，什么都不做
        ([{"column_name": "kind"}], True),  # 老库：kind 在 → DROP
    ],
)
async def test_method_module_kind_drop_migration(monkeypatch, columns, expect_drop):
    from tortoise import Tortoise

    from user_platform.database import _drop_method_module_kind

    conn = _MigrateConn(columns)
    monkeypatch.setattr(Tortoise, "get_connection", lambda _name: conn)
    await _drop_method_module_kind()

    drops = [s for s in conn.scripts if "DROP COLUMN" in s]
    assert bool(drops) is expect_drop
    if expect_drop:
        assert "mc_test_method_module" in drops[0]
        assert "kind" in drops[0]
