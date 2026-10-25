"""内置基础方法类播种测试（§5.1：全局内置 Time/Random/Crypto/Codec）。

校验：
1. 播种后全局作用域（``project_id=None``）恰好有 4 个内置类，全部 ``status=active``。
2. 二次播种返回 0（幂等，不重复建）。
3. 每个内置类都能被 ``parse_class_source`` 严格通过且方法数 > 0（含 docstring）。
4. ``list_method_classes(project_id=None)`` 能列出这 4 类含展开方法与入参说明。
5. 用户对全局类的编辑不被重播种还原（只补不覆盖）。

运行：``python -m pytest tests/test_builtin_methods.py -q``
"""
from __future__ import annotations

import os
import sys

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


@pytest.mark.asyncio
async def test_seed_creates_four_active_global_classes(tortoise_db):
    from testing_platform.assets import BUILTIN_METHOD_CLASSES, ensure_builtin_method_classes
    from testing_platform.assets.models import TestMethodModule

    created = await ensure_builtin_method_classes()
    assert created == len(BUILTIN_METHOD_CLASSES) == 4

    rows = await TestMethodModule.filter(project_id=None).order_by("code")
    assert [r.code for r in rows] == ["Codec", "Crypto", "Random", "Time"]
    assert all(r.status == "active" for r in rows), [(r.code, r.status) for r in rows]
    assert all(r.source.strip() for r in rows)


@pytest.mark.asyncio
async def test_seed_is_idempotent(tortoise_db):
    from testing_platform.assets import ensure_builtin_method_classes

    first = await ensure_builtin_method_classes()
    second = await ensure_builtin_method_classes()
    assert first == 4
    assert second == 0, "二次播种不应新建任何类"


@pytest.mark.asyncio
async def test_every_builtin_class_parses_with_methods(tortoise_db):
    from testing_platform.assets import BUILTIN_METHOD_CLASSES
    from testing_platform.method_source import parse_class_source

    for name, source in BUILTIN_METHOD_CLASSES:
        parsed = parse_class_source(source)
        assert parsed.name == name
        assert parsed.docstring, f"{name} 缺类 docstring（备注）"
        assert parsed.methods, f"{name} 无静态方法"
        for m in parsed.methods:
            assert m.docstring, f"{name}.{m.name} 缺方法 docstring"


@pytest.mark.asyncio
async def test_seeded_classes_listed_with_signature(tortoise_db):
    from testing_platform.assets import ensure_builtin_method_classes, list_method_classes

    await ensure_builtin_method_classes()
    classes = await list_method_classes(None)
    by_code = {c["code"]: c for c in classes}
    assert set(by_code) == {"Time", "Random", "Crypto", "Codec"}
    # 方法已展开且带入参说明
    time_methods = {m["name"]: m for m in by_code["Time"]["methods"]}
    assert "now" in time_methods
    assert time_methods["now"]["full_ref"] == "Time.now"
    assert time_methods["now"]["signature"]["params"][0]["name"] == "fmt"


@pytest.mark.asyncio
async def test_user_edit_to_global_class_not_overwritten(tortoise_db):
    """只补不覆盖：改过的全局类重播种不被还原。"""
    from testing_platform.assets import ensure_builtin_method_classes
    from testing_platform.assets.models import TestMethodModule

    await ensure_builtin_method_classes()
    cls = await TestMethodModule.get(project_id=None, code="Codec")
    cls.description = "用户改过的备注"
    await cls.save()

    assert await ensure_builtin_method_classes() == 0
    again = await TestMethodModule.get(project_id=None, code="Codec")
    assert again.description == "用户改过的备注"
