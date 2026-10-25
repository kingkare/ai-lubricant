"""端到端冒烟:能力面 → assets → quality → engine → 真实 HTTP 断言。

链路(01→02→06→04+design §17):
  scenario.create → script.validate → run.execute(真发 HTTP)

目标:http://127.0.0.1:8001/v1/models(需鉴权,返回 401)——测**未授权请求被正确拒绝**。
真实接口、真实断言、零 mock;网络走 {{env.base_url}} 不硬编码(校验器规则 12)。

依赖本地服务在跑:不在则整组 skip(不污染 CI)。
"""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio

BASE_URL = "http://127.0.0.1:8001"


@pytest_asyncio.fixture
async def tortoise_db():
    """sqlite::memory:，与全模块同 app label "models"（进程级注册表，统一防串扰）。"""
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={
            "testing_platform": [
                "testing_platform.knowledge.models",
                "testing_platform.assets.models",
                "testing_platform.tasks.models",
                "testing_platform.reports.models",
                "testing_platform.workflow.models",
                "testing_platform.defects.models",
            ]
        },
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _service_alive() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.get(f"{BASE_URL}/v1/models")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.asyncio
async def test_e2e_scenario_validate_execute(tortoise_db):
    """端到端:造场景 → 校验 → 执行真实 HTTP → 断言 401。"""
    # 服务不在 → skip(不因环境缺依赖而红)
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.get(f"{BASE_URL}/v1/models")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"本地服务 {BASE_URL} 不可达,跳过端到端冒烟: {type(exc).__name__}")

    from testing_platform.surface import call_tool

    pid = uuid.uuid4()

    # 1. 建场景(一次带 steps;URL 走环境变量,硬编码会被规则 12 拒)
    scn = await call_tool("scenario.create", {
        "project_id": str(pid),
        "name": "未授权访问 models 接口",
        "purpose": "验证未带 Authorization 调 /v1/models 被拒绝",
        "exec_mode": "script_only",
        "local_vars": {},
        "flow": {},
        "steps": [{
            "step_key": "call_models_no_auth",
            "seq": 10,
            "type": "request",
            "description": "调 /v1/models 不带 Authorization",
            "params": {"method": "GET", "url": "{{env.base_url}}/v1/models"},
            "post_checks": [{"type": "status_code", "expected": 401}],
        }],
    })
    assert scn["isError"] is not True, scn
    scenario_id = scn["structuredContent"]["id"]

    # 2. 校验必须通过(若 URL 硬编码会在这里被规则 12 拒)
    full = await call_tool("scenario.get", {"scenario_id": scenario_id})
    v = await call_tool("script.validate", {"scenario": full["structuredContent"]})
    assert v["structuredContent"]["valid"] is True, v["structuredContent"].get("issues")

    # 3. 执行真发 HTTP
    r = await call_tool("run.execute", {
        "scenario_id": scenario_id,
        "env": {"base_url": BASE_URL},
    })
    assert r["isError"] is not True, r
    res = r["structuredContent"]
    assert res["status"] == "pass", res
    step = res["steps"][0]
    assert step["status"] == "pass", step
