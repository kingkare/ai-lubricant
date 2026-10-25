"""case.generate 编排器（17 号文 §4.1）——同步 L1 枚举秒回骨架，后台 Agent 只补语义。

预检契约不破（失败不留孤儿）+ 编排器新契约：
1. 有接口 → 返回 ``skeleton.created>0``，且 ``mc_test_case`` 已落骨架（run 还没跑完就有）。
2. 重跑第二次 → ``skipped`` 全跳（slot_key 幂等）。
3. scope.module 真过滤（治断点 4：scope 不再只进 prompt）。
4. 缺密钥 → 当场抛，**不写骨架、不留 run**。
5. prompt 带「不得重复产出机械点」硬规则。
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
        modules={
            "testing_platform": [
                "testing_platform.projects.models",
                "testing_platform.assets.models",
                "testing_platform.knowledge.models",
                "testing_platform.workflow.models",
            ],
        },
    )
    await Tortoise.generate_schemas()
    from testing_platform.workflow.service import ensure_builtin_defs

    await ensure_builtin_defs()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def _stub_agent(monkeypatch):
    """后台 Agent 打桩（不真起 dsh 子进程）。"""
    calls: list[str] = []

    async def _fake_run_chat(*, prompt: str, config) -> str:
        calls.append(prompt)
        return "stubbed"

    from testing_platform import agent_runtime

    monkeypatch.setattr(agent_runtime, "run_chat", _fake_run_chat)
    monkeypatch.setenv("TESTING_GEN_API_KEY", "k")
    yield calls


async def _seed_api(project_id, *, name="下单", params=None, requirement_id=None):
    from testing_platform.assets.models import TestApi

    return await TestApi.create(
        project_id=project_id, code=f"API-{uuid.uuid4().hex[:6]}", name=name,
        method="POST", path="/api/v1/orders",
        params=params if params is not None else [
            {"name": "size", "in": "query", "type": "integer", "required": True,
             "min_value": 1, "max_value": 100, "example": 20},
        ],
        requirement_id=requirement_id,
    )


@pytest.mark.asyncio
async def test_generate_returns_skeleton_synchronously(tortoise_db, _stub_agent):
    """有接口 → 同步秒回 skeleton.created>0，骨架已落库（不等 Agent）。"""
    from testing_platform.assets.models import TestCase
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm

    proj = await psvc.create_project(name="P")
    await _seed_api(proj.id)
    uid = uuid.uuid4()

    res = await sm.case_generate({"project_id": str(proj.id), "created_by": str(uid)})

    assert res["skeleton"]["created"] > 0, "编排器必须同步完成 L1 枚举"
    assert res["run_id"]
    rows = await TestCase.filter(project_id=proj.id).all()
    assert len(rows) == res["skeleton"]["created"]
    assert all(r.slot_key and r.confirm_source == "doc" for r in rows)


@pytest.mark.asyncio
async def test_generate_rerun_skips(tortoise_db, _stub_agent):
    """重跑 → created=0 全 skip（slot_key 幂等）。"""
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm

    proj = await psvc.create_project(name="P")
    await _seed_api(proj.id)
    uid = uuid.uuid4()

    first = await sm.case_generate({"project_id": str(proj.id), "created_by": str(uid)})
    second = await sm.case_generate({"project_id": str(proj.id), "created_by": str(uid)})

    assert second["skeleton"]["created"] == 0
    assert second["skeleton"]["skipped"] == first["skeleton"]["created"]


@pytest.mark.asyncio
async def test_scope_module_real_filter(tortoise_db, _stub_agent):
    """scope.module 真过滤（断点 4）：只枚举命中模块的接口。"""
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm

    proj = await psvc.create_project(name="P")
    await _seed_api(proj.id, name="支付")
    await _seed_api(proj.id, name="物流")
    uid = uuid.uuid4()

    res = await sm.case_generate({
        "project_id": str(proj.id), "created_by": str(uid),
        "scope": {"module": ["支付"]},
    })
    # 只枚了一个接口的 slot（≈10），不是两个接口的（≈20）
    assert len(res["skeleton"]["per_api"]) == 1
    assert res["context"]["scoped_api_count"] == 1


@pytest.mark.asyncio
async def test_no_key_no_side_effects(tortoise_db, monkeypatch):
    """缺密钥 → 当场抛，不写骨架不留 run（预检契约 + 编排顺序：校验先行）。"""
    from testing_platform.agent_runtime import GenerationConfigError
    from testing_platform.assets.models import TestCase
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm
    from testing_platform.workflow.models import TestWorkflowRun

    monkeypatch.delenv("TESTING_GEN_API_KEY", raising=False)
    proj = await psvc.create_project(name="P")
    await _seed_api(proj.id)

    with pytest.raises(GenerationConfigError):
        await sm.case_generate({"project_id": str(proj.id), "created_by": str(uuid.uuid4())})

    assert await TestCase.filter(project_id=proj.id).count() == 0, "校验失败不得写骨架"
    assert await TestWorkflowRun.all().count() == 0


@pytest.mark.asyncio
async def test_prompt_has_no_duplicate_rule(tortoise_db, _stub_agent):
    """prompt 带「机械点已枚举，不得重复产出」硬规则。"""
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm

    proj = await psvc.create_project(name="P")
    await _seed_api(proj.id)
    await sm.case_generate({"project_id": str(proj.id), "created_by": str(uuid.uuid4())})

    import asyncio

    await asyncio.sleep(0)  # 让 _drive 任务排上
    prompt = _stub_agent[0] if _stub_agent else ""
    assert "你不得重复产出机械点" in prompt
    assert "slot_key" in prompt
