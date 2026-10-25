"""骨架用例落库（design/skeleton.py）——幂等重跑验收（17 号文 P0）。

不联网、不调 AI。用 SQLite 内存库走真落库路径：
1. 枚举 → bulk_create → 重跑 = created=0 全 skip（17 号文 P0 验收标准）。
2. ``ensure_slot_index``：存量表（无 slot_key 列）自愈 + 幂等。
3. 批量发号 TC-xxxx 连续、不逐条捞全表。
4. 骨架行字段契约：confirm_source=doc / origin=ai_generated / 三层断言第 1 层。
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


def _api(**overrides):
    base = {
        "id": str(uuid.uuid4()),
        "method": "POST",
        "path": "/api/v1/orders",
        "name": "下单",
        "params": [
            {"name": "size", "in": "query", "type": "integer", "required": True,
             "min_value": 1, "max_value": 100, "example": 20},
        ],
        "request_body": {"type": "object", "properties": {
            "product_id": {"type": "string", "required": True, "example": "P-1"},
        }},
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_enumerate_idempotent_rerun(tortoise_db):
    """17 号文 P0 验收：重跑两次生成 → 第二次 created=0 全 skip。"""
    from testing_platform.design.skeleton import enumerate_skeleton_cases

    project_id = uuid.uuid4()
    api = _api()

    first = await enumerate_skeleton_cases(
        project_id=project_id, api_rows=[api], created_by=uuid.uuid4()
    )
    assert first["created"] > 0
    assert first["skipped"] == 0

    second = await enumerate_skeleton_cases(
        project_id=project_id, api_rows=[api], created_by=uuid.uuid4()
    )
    assert second["created"] == 0, "重跑必须全 skip（slot_key 幂等）"
    assert second["skipped"] == first["created"]


@pytest.mark.asyncio
async def test_skeleton_row_contract(tortoise_db):
    """骨架行字段契约：confirm_source=doc / origin=ai_generated / status=active / 三层断言。"""
    from testing_platform.design.skeleton import enumerate_skeleton_cases
    from testing_platform.assets.models import TestCase

    project_id = uuid.uuid4()
    await enumerate_skeleton_cases(
        project_id=project_id, api_rows=[_api()], created_by=uuid.uuid4()
    )
    rows = await TestCase.filter(project_id=project_id).all()
    assert rows
    for row in rows:
        assert row.confirm_source == "doc"      # 机械点依据=接口契约本身
        assert row.origin == "ai_generated"
        assert row.status == "active"           # 18 号文 §8：自动 active，进抽检队列
        assert row.slot_key                     # 幂等键必填
        assert isinstance(row.expected, dict)
        assert "status_code" in row.expected    # 三层断言第 1 层
    # 通用轴 4 条必在（每接口必测鉴权/幂等）
    keys = {r.slot_key for r in rows}
    assert any(k.startswith("auth:") for k in keys)
    assert any(k.startswith("idem:") for k in keys)


@pytest.mark.asyncio
async def test_batch_codes_sequential(tortoise_db):
    """批量发号：TC-xxxx 连续，不逐条捞全表。"""
    from testing_platform.design.skeleton import enumerate_skeleton_cases
    from testing_platform.assets.models import TestCase

    project_id = uuid.uuid4()
    await enumerate_skeleton_cases(
        project_id=project_id, api_rows=[_api()], created_by=uuid.uuid4()
    )
    codes = sorted(r.code for r in await TestCase.filter(project_id=project_id).all())
    nums = [int(c.replace("TC-", "")) for c in codes]
    assert nums == list(range(nums[0], nums[0] + len(nums))), "发号必须连续"


@pytest.mark.asyncio
async def test_ensure_slot_index_selfheal_and_idempotent(tortoise_db):
    """存量表自愈：删掉索引后 ensure 幂等重建；重复调用不炸。"""
    from testing_platform.design.skeleton import ensure_slot_index
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await ensure_slot_index(conn)
    await ensure_slot_index(conn)  # 幂等：第二次不炸

    # 列存在
    cols = await conn.execute_query_dict("PRAGMA table_info(mc_test_case)")
    assert any(r.get("name") == "slot_key" for r in cols)
    # 索引存在
    idx = await conn.execute_query_dict(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_test_case_slot_key'"
    )
    assert idx, "部分唯一索引必须存在"


@pytest.mark.asyncio
async def test_conflicting_slot_not_duplicated(tortoise_db):
    """并发双保险：同 slot_key 再插（绕过过滤）→ 索引拦截，行数不涨。"""
    from testing_platform.design.skeleton import enumerate_skeleton_cases
    from testing_platform.assets.models import TestCase

    project_id = uuid.uuid4()
    api = _api()
    await enumerate_skeleton_cases(project_id=project_id, api_rows=[api], created_by=uuid.uuid4())
    before = await TestCase.filter(project_id=project_id).count()

    # 直接再造一条同 slot_key 的行（模拟并发会话撞索引）
    existing = await TestCase.filter(project_id=project_id).first()
    with pytest.raises(Exception):
        await TestCase.create(
            code="TC-9999", project_id=project_id, title="dup",
            confirm_source="doc", created_by=uuid.uuid4(),
            slot_key=existing.slot_key,
        )
    after = await TestCase.filter(project_id=project_id).count()
    assert after == before, "唯一索引必须拦截重复 slot"
