"""生成入口预检 + 失败留档测试（不联网、不起 dsh）。

覆盖这次修的核心痛点：
1. ``case.generate`` / ``transport.generate_from_repo`` **缺密钥当场报错**
   （GenerationConfigError），不再开一个永远 pending 的孤儿 run。
2. ``requirement_id`` 无效 → 显式报错（不静默生成孤儿用例）。
3. ``fail_run`` 把异常落进 ``run.output.error`` + ``status=failed``。
4. ``_build_generation_prompt`` 带需求上下文（验收标准 + requirement_id 指令）。
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
def _no_env_key(monkeypatch):
    """确保环境里没有生成密钥（否则 build_generation_config 不会抛）。"""
    monkeypatch.delenv("TESTING_GEN_API_KEY", raising=False)


@pytest.mark.asyncio
async def test_case_generate_requires_key(tortoise_db):
    """缺密钥 → case.generate 当场抛 GenerationConfigError（不开孤儿 run）。"""
    from testing_platform.agent_runtime import GenerationConfigError
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm
    from testing_platform.workflow.models import TestWorkflowRun

    proj = await psvc.create_project(name="P")
    with pytest.raises(GenerationConfigError):
        await sm.case_generate({"project_id": str(proj.id)})
    # 关键：失败预检**不该**留下 run
    assert await TestWorkflowRun.all().count() == 0


@pytest.mark.asyncio
async def test_generate_from_repo_requires_key(tortoise_db):
    """缺密钥 → transport.generate_from_repo 当场抛（不开孤儿 run）。"""
    from testing_platform.agent_runtime import GenerationConfigError
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm
    from testing_platform.workflow.models import TestWorkflowRun

    proj = await psvc.create_project(name="P")
    git = await psvc.create_git(project_id=proj.id, name="r", repo_url="https://x/y.git")
    ver = await psvc.create_version(
        project_id=proj.id, version_code="v1",
        repos=[{"git_id": str(git.id), "ref": "master", "is_primary": True}],
    )
    with pytest.raises(GenerationConfigError):
        await sm.transport_generate_from_repo({"project_id": str(proj.id), "version_id": str(ver.id)})
    assert await TestWorkflowRun.all().count() == 0


@pytest.mark.asyncio
async def test_generate_from_repo_no_repos_errors(tortoise_db, monkeypatch):
    """版本没配仓库 → 显式报错（预检 ②）。"""
    monkeypatch.setenv("TESTING_GEN_API_KEY", "k")  # 有密钥，过预检①
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm

    proj = await psvc.create_project(name="P")
    ver = await psvc.create_version(project_id=proj.id, version_code="v1", repos=[])
    with pytest.raises(ValueError, match="没有配仓库"):
        await sm.transport_generate_from_repo({"project_id": str(proj.id), "version_id": str(ver.id)})


@pytest.mark.asyncio
async def test_case_generate_bad_requirement_errors(tortoise_db, monkeypatch):
    """requirement_id 无效 → 显式报错（不静默生成孤儿用例）。"""
    monkeypatch.setenv("TESTING_GEN_API_KEY", "k")
    from testing_platform.projects import service as psvc
    from testing_platform.surface import methods as sm

    proj = await psvc.create_project(name="P")
    with pytest.raises(ValueError, match="requirement_id 无效"):
        await sm.case_generate({"project_id": str(proj.id), "requirement_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_fail_run_records_error(tortoise_db):
    """fail_run 把异常落 output.error + status=failed（前端轮询拿得到原因）。"""
    from testing_platform.workflow.service import fail_run, get_run, start_run

    run = await start_run("designer", {"project_id": "x"})
    await fail_run(run.id, RuntimeError("dsh 起不来：缺 deepseek-harness-sdk"))
    fresh = await get_run(run.id)
    assert fresh.status == "failed"
    assert "deepseek-harness-sdk" in fresh.output["error"]


def test_generation_prompt_includes_requirement():
    """分析提示词带需求正文与四标准（25 号文：提示词在 generation/req_analysis，
    旧的 _build_generation_prompt 已随 case.generate 改造删除）。"""
    from testing_platform.generation.req_analysis import build_analysis_prompt

    prompt = build_analysis_prompt("库存为 0 时下单应返回 409")
    assert "库存为 0 时下单应返回 409" in prompt      # 需求正文带入
    assert "register" in prompt                       # 唯一登记表
    assert "condition" in prompt and "expected" in prompt  # 验收标准结构化
    assert "HARD" in prompt.upper()                   # 四标准是硬规则段
