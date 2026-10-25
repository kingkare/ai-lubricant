"""项目/版本级角色成员 + 策略团队名单 flow 测试（用户定版 2026-10-15）。

覆盖：
* **项目三类角色** —— ``create_project`` 带 ``testers``/``developers``/
  ``product_managers``；去重保序；``update_project`` 传列表覆盖、空列表清空、
  不传不动（``...`` 哨兵语义）；
* **版本三类角色** —— ``create_version``/``update_version`` 同款；
* **生效角色** —— ``resolve_version_roles``：版本传值即覆盖，否则回退项目级；
* **策略团队名单** —— ``strategy.testers``/``developers``（本轮测试/开发人员），
  create/update 同款语义 + 变更门 + version+1；
* **能力面接线** —— ``project.create``/``project.update``/``project.version.create``/
  ``project.version.update`` 收角色字段；``project.version.roles`` 读生效角色；
  ``strategy.create``/``strategy.update`` 收团队名单。

无 PG 依赖（sqlite::memory:，Tortoise 建表）。
运行：``python -m pytest tests/test_project_roles.py -q``
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

from testing_platform.projects import (  # noqa: E402
    create_project,
    create_version,
    resolve_version_roles,
    update_project,
    update_version,
)
from testing_platform.projects.service import _normalize_role_list  # noqa: E402
from testing_platform.strategy import (  # noqa: E402
    create_strategy,
    get_strategy,
    update_strategy,
)
from testing_platform.projects.service import ProjectValidationError  # noqa: E402
from testing_platform.strategy.service import StrategyNotFoundError  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.assets.models",
                "testing_platform.projects.models",
                "testing_platform.strategy.models",
                "testing_platform.defects.models",
            ],
        },
        use_tz=False,
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 1) 项目三类角色 + 去重 + 哨兵语义 ──────────────────────────
@pytest.mark.asyncio
async def test_project_roles(tortoise_db):
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = await create_project(name="角色项目", created_by=u1,
                             testers=[u1, str(u2)], developers=[u3],
                             product_managers=[str(u1), u3, None, ""])
    assert p.testers == [str(u1), str(u2)]
    assert p.developers == [str(u3)]
    assert p.product_managers == [str(u1), str(u3)], "去重 + 丢弃空值"

    # 哨兵：不传 = 不动；空列表 = 清空
    p2 = await update_project(p.id, testers=[])
    assert p2.testers == [] and p2.developers == [str(u3)] and p2.product_managers == [str(u1), str(u3)]

    p3 = await update_project(p.id, name="改名")  # 角色字段不动
    assert p3.developers == [str(u3)]


# ── 2) 归一函数：单值/列表/非法输入 ─────────────────────────────
def test_normalize_role_list():
    uid = uuid.uuid4()
    assert _normalize_role_list(None) == []
    assert _normalize_role_list(uid) == [str(uid)]
    assert _normalize_role_list([uid, str(uid), "", None]) == [str(uid)]
    with pytest.raises(ProjectValidationError):
        _normalize_role_list(["junk"])


# ── 3) 版本角色 + 生效角色（覆盖/回退）─────────────────────────
@pytest.mark.asyncio
async def test_version_roles_and_inheritance(tortoise_db):
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = await create_project(name="角色项目", created_by=u1,
                             testers=[u1, u2], developers=[u3], product_managers=[u1])

    v = await create_version(project_id=p.id, version_code="v1.0", repos=[], testers=[u2])
    assert v.testers == [str(u2)] and v.developers == []

    # 生效角色：版本 testers 覆盖；developers/product_managers 回退项目级
    eff = await resolve_version_roles(p.id, v.id)
    assert eff == {
        "testers": [str(u2)],
        "developers": [str(u3)],
        "product_managers": [str(u1)],
    }

    # 版本补 developers 后覆盖
    v2 = await update_version(v.id, developers=[u1, u2])
    assert v2.developers == [str(u1), str(u2)]
    eff2 = await resolve_version_roles(p.id, v.id)
    assert eff2["developers"] == [str(u1), str(u2)]

    # 只取项目级（version_id 缺省）—— 走能力面分支验证，见 test 5

    # version 不属于该 project → 拒
    p_other = await create_project(name="另一个", created_by=u1)
    v_other = await create_version(project_id=p_other.id, version_code="v9.9", repos=[])
    with pytest.raises(ProjectValidationError):
        await resolve_version_roles(p.id, v_other.id)


# ── 4) 策略团队名单（测试/开发人员）+ 变更门 ────────────────────
@pytest.mark.asyncio
async def test_strategy_team_lists(tortoise_db):
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = await create_project(name="P", created_by=u1)

    s = await create_strategy(project_id=p.id, name="冒烟", strategy_type="smoke",
                              owner_id=u1, testers=[u1, u2, str(u1)], developers=[u3], created_by=u1)
    doc = await get_strategy(s.id)
    assert doc["testers"] == [str(u1), str(u2)], "去重保序"
    assert doc["developers"] == [str(u3)]

    # update：换人 + version+1；变更门在 service 层（必填 change_reason）
    with pytest.raises(Exception):
        await update_strategy(s.id, change_reason="", values={"testers": [u3]})
    s2 = await update_strategy(s.id, change_reason="换人", values={"testers": [u3], "developers": []},
                               created_by=u1)
    doc2 = await get_strategy(s.id)
    assert doc2["testers"] == [str(u3)] and doc2["developers"] == []
    assert s2.version == 2

    # 不可改字段拒（code 不在 allowed）
    from testing_platform.assets.service import AssetValidationError
    with pytest.raises(AssetValidationError):
        await update_strategy(s.id, change_reason="x", values={"code": "STRAT-9999"})

    # 不存在的策略
    with pytest.raises(StrategyNotFoundError):
        await get_strategy(uuid.uuid4())


# ── 5) 能力面接线 ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_surface_methods_roles(tortoise_db):
    from testing_platform.surface import methods as surface

    assert surface.has_method("project.version.roles")
    u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    proj = await surface.call_method("project.create", {
        "name": "角色项目", "created_by": str(u1),
        "testers": [str(u1), str(u2)], "developers": [str(u3)], "product_managers": [str(u1)],
    })
    pid = proj["id"]
    assert proj["testers"] == [str(u1), str(u2)]

    # project.update 覆盖
    p2 = await surface.call_method("project.update", {"project_id": pid, "testers": [str(u3)]})
    assert p2["item"]["testers"] == [str(u3)]
    assert p2["item"]["developers"] == [str(u3)], "不传字段不动"

    # version 带角色 + roles 读取
    ver = await surface.call_method("project.version.create", {
        "project_id": pid, "version_code": "v1.0", "repos": [], "testers": [str(u2)],
    })
    eff = await surface.call_method("project.version.roles", {"project_id": pid, "version_id": ver["id"]})
    assert eff["testers"] == [str(u2)]
    assert eff["developers"] == [str(u3)], "版本未传 → 回退项目级"

    # 只取项目级（version_id 空串）
    proj_roles = await surface.call_method("project.version.roles", {"project_id": pid, "version_id": ""})
    assert proj_roles["testers"] == [str(u3)]

    # strategy.create/update 收团队名单
    s = await surface.call_method("strategy.create", {
        "project_id": pid, "name": "冒烟", "strategy_type": "smoke",
        "testers": [str(u1), str(u2)], "developers": [str(u3)], "created_by": str(u1),
    })
    assert s["testers"] == [str(u1), str(u2)]
    s2 = await surface.call_method("strategy.update", {
        "strategy_id": s["id"], "change_reason": "换人", "testers": [str(u3)], "created_by": str(u1),
    })
    assert s2["testers"] == [str(u3)]
