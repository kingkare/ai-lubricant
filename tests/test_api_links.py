"""接口关联索引派生测试（``mc_test_api_scenario`` / ``mc_test_api_case``）。

真相源是步骤 ``params.api_id``；关联表是**反查索引**（供接口详情「被哪些场景/
用例引用」与影响面）。本文件验证：步骤落库后索引被正确派生（去重、悬空过滤、
幂等重建），用例沿「用例→场景→步骤」传递。

运行：``python -m pytest tests/test_api_links.py -q``（sqlite::memory:，无 PG）。
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

from testing_platform.assets import service  # noqa: E402
from testing_platform.assets.models import (  # noqa: E402
    TestApi,
    TestApiCase,
    TestApiScenario,
)


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，只注册资产模型（本测试不碰任务/报告域）。"""
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


async def _make_api(project_id: str, name: str, path: str, method: str = "GET") -> TestApi:
    return await service.create_api(
        project_id=project_id, name=name, method=method, path=path
    )


def _step(step_key: str, api_id: str | None, seq: int = 10) -> dict:
    params: dict = {"method": "GET", "url": "/x"}
    if api_id:
        params["api_id"] = api_id
    return {
        "step_key": step_key,
        "type": "request",
        "seq": seq,
        "params": params,
        "post_checks": [{"type": "status_code", "value": 200}],
    }


@pytest.mark.asyncio
async def test_steps_derive_scenario_api_links(tortoise_db):
    """建步骤带 api_id → 场景↔接口索引派生（去重 + 按首次出现定 order）。"""
    pid = str(uuid.uuid4())
    api1 = await _make_api(pid, "下单", "/orders", "POST")
    api2 = await _make_api(pid, "查单", "/orders/{id}", "GET")

    scenario = await service.create_scenario(project_id=pid, name="下单流程")
    await service.create_steps(
        scenario.id,
        [
            _step("s1", str(api1.id), 10),
            _step("s2", str(api2.id), 20),
            _step("s3", str(api1.id), 30),  # 重复引用 → 去重
            _step("s4", None, 40),  # 无引用 → 不入索引
        ],
    )

    links = await TestApiScenario.filter(scenario_id=scenario.id).order_by("order")
    assert [str(link.api_id) for link in links] == [str(api1.id), str(api2.id)]

    listed = await service.list_scenario_apis(scenario.id)
    assert [a.id for a in listed] == [api1.id, api2.id]

    # 接口侧反查：哪些场景在引用我
    by_api1 = await service.list_api_scenarios(api1.id)
    assert [s.id for s in by_api1] == [scenario.id]


@pytest.mark.asyncio
async def test_dangling_api_ref_filtered(tortoise_db):
    """步骤引用不存在的接口 → 索引过滤（不建悬空关联）。"""
    pid = str(uuid.uuid4())
    scenario = await service.create_scenario(project_id=pid, name="悬空")
    await service.create_steps(
        scenario.id, [_step("s1", str(uuid.uuid4()), 10)]  # 随机 UUID，库里没有
    )
    assert await TestApiScenario.filter(scenario_id=scenario.id).count() == 0


@pytest.mark.asyncio
async def test_resync_is_idempotent_and_rebuilds(tortoise_db):
    """重建索引幂等：再次 sync 结果不变；删掉引用后 sync 收敛。"""
    pid = str(uuid.uuid4())
    api1 = await _make_api(pid, "A", "/a")
    scenario = await service.create_scenario(project_id=pid, name="幂等")
    await service.create_steps(scenario.id, [_step("s1", str(api1.id), 10)])

    first = await service.sync_scenario_api_links(scenario.id)
    second = await service.sync_scenario_api_links(scenario.id)
    assert first == second == [api1.id]
    assert await TestApiScenario.filter(scenario_id=scenario.id).count() == 1

    # 删接口 → 再 sync 应清空（悬空过滤）
    await service.delete_api(api1.id)
    await service.sync_scenario_api_links(scenario.id)
    assert await TestApiScenario.filter(scenario_id=scenario.id).count() == 0


@pytest.mark.asyncio
async def test_case_api_links_derive_from_scenario(tortoise_db):
    """用例↔接口沿「用例→场景→步骤」传递：绑场景即派生覆盖接口。"""
    pid = str(uuid.uuid4())
    api1 = await _make_api(pid, "下单", "/orders", "POST")
    api2 = await _make_api(pid, "查单", "/orders/{id}", "GET")

    scenario = await service.create_scenario(project_id=pid, name="场景")
    await service.create_steps(
        scenario.id,
        [_step("s1", str(api1.id), 10), _step("s2", str(api2.id), 20)],
    )

    case = await service.create_case(
        project_id=pid, title="用例", confirm_source="doc", created_by=str(uuid.uuid4())
    )
    await service.link_case_scenario(case.id, scenario.id, is_primary=True)

    covered = await service.list_case_apis(case.id)
    assert [a.id for a in covered] == [api1.id, api2.id]

    # 接口侧反查覆盖用例
    cases_of_api = await service.list_api_cases(api1.id)
    assert [c.id for c in cases_of_api] == [case.id]

    # 解绑 → 覆盖接口收敛为空
    await service.unlink_case_scenario(case.id, scenario.id)
    assert await service.list_case_apis(case.id) == []


@pytest.mark.asyncio
async def test_scenario_ref_does_not_leak_api_links(tortoise_db):
    """scenario_ref 步骤的 params 无 api_id → 不产生关联（引用场景自身接口不算本场景）。"""
    pid = str(uuid.uuid4())
    api1 = await _make_api(pid, "A", "/a")
    sub = await service.create_scenario(project_id=pid, name="子场景")
    await service.create_steps(sub.id, [_step("s1", str(api1.id), 10)])

    parent = await service.create_scenario(project_id=pid, name="父场景")
    await service.create_steps(
        parent.id,
        [
            {
                "step_key": "ref",
                "type": "scenario_ref",
                "seq": 10,
                "params": {"scenario_ref": str(sub.id)},
                "post_checks": [],
            }
        ],
    )
    # 父场景只显式引用子场景，不直接引用接口 → 索引为空
    assert await TestApiScenario.filter(scenario_id=parent.id).count() == 0
