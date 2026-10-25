"""种子数据：走完整链路造一批可看的数据（真实 HTTP 执行，非 mock）。

造：2 标签 / 2 用例 / 2 场景（真发 HTTP 到本机 8001）/ 1 任务 / 1 次执行 + 报告。
全部经 service 层入库（PG），前端 USE_MOCK_FALLBACK=false 后即可见。

用法：python testing_platform/seed_demo.py
前提：main.py 在 127.0.0.1:8001 跑着（服务自己测自己）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
# bootstrap_config 在 server/（主服务与 pytest.ini 的 pythonpath 同款）
sys.path.insert(0, str(_ROOT / "server"))

# 加载 .env（主服务启动时由 main.py 加载；独立脚本需自己加载，否则
# bootstrap_config 读不到 PG 配置，settings.database_url 为空）。
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from tortoise import Tortoise

BASE_URL = "http://127.0.0.1:8001"


async def main() -> None:
    # 真实 PG（生产库）——服务重启时 generate_schemas 已建 mc_test_* 表
    from user_platform.database import _TORTOISE_CONFIG
    await Tortoise.init(config=_TORTOISE_CONFIG, use_tz=False, _enable_global_fallback=True)

    from testing_platform.assets import (
        create_case, create_scenario, create_steps, create_tag, link_case_scenario, tag_case,
    )
    from testing_platform.tasks import service as task_service
    from testing_platform.engine import ExecutionContext, ScenarioExecutor
    from testing_platform.assets import PgScenarioLoader
    from testing_platform.reports import generate_report

    pid = uuid.uuid4()  # 演示项目（无对应真实项目，仅作数据归属）
    me = uuid.uuid4()

    # ── 标签 ──
    t_smoke = await create_tag(project_id=pid, name="smoke", category="purpose", description="冒烟")
    t_p0 = await create_tag(project_id=pid, name="p0", category="priority", description="阻塞级")
    print(f"[tags] smoke={t_smoke.code} p0={t_p0.code}")

    # ── 场景 1：未授权 401（真实 HTTP）──
    s1 = await create_scenario(
        project_id=pid, name="未授权访问 models 接口",
        purpose="验证未带 Authorization 调 /v1/models 被拒绝",
        exec_mode="script_only", local_vars={}, flow={},
    )
    await create_steps(s1.id, [{
        "step_key": "call_models_no_auth", "seq": 10, "type": "request",
        "description": "调 /v1/models 不带 Authorization",
        "params": {"method": "GET", "url": "{{env.base_url}}/v1/models"},
        "post_checks": [{"type": "status_code", "expected": 401}],
        "post_cond": ["{{local.sc1_status}} = {{response.status}}"],
    }])
    print(f"[scenario] {s1.code} 未授权401")

    # ── 场景 2：健康检查 200 ──
    s2 = await create_scenario(
        project_id=pid, name="mcp 健康检查",
        purpose="验证 /mcp/health 返回 200 且 ok=true",
        exec_mode="script_only", local_vars={}, flow={},
    )
    await create_steps(s2.id, [{
        "step_key": "check_health", "seq": 10, "type": "request",
        "description": "GET /mcp/health",
        "params": {"method": "GET", "url": "{{env.base_url}}/mcp/health"},
        "post_checks": [
            {"type": "status_code", "expected": 200},
            {"type": "json_path", "path": "$.ok", "operator": "equals", "expected": True},
        ],
    }])
    print(f"[scenario] {s2.code} 健康检查")

    # ── 用例（关联场景 + 标签）──
    c1 = await create_case(
        project_id=pid, title="未授权请求被拒绝", intent="安全：未带凭证调模型列表应 401",
        confirm_source="debug", confirm_ref={"note": "curl 实测 401"},
        created_by=me, expected={"status": 401},
    )
    await link_case_scenario(c1.id, s1.id, is_primary=True, order=10)
    await tag_case(c1.id, t_smoke.id)
    await tag_case(c1.id, t_p0.id)

    c2 = await create_case(
        project_id=pid, title="MCP 运行时健康", intent="探活：/mcp/health 返回 ok",
        confirm_source="doc", confirm_ref={"note": "main.py /mcp/health"},
        created_by=me, expected={"status": 200, "ok": True},
    )
    await link_case_scenario(c2.id, s2.id, is_primary=True, order=10)
    await tag_case(c2.id, t_smoke.id)
    print(f"[cases] {c1.code}(未授权401,smoke+p0) {c2.code}(健康检查,smoke)")

    # ── 任务（标签选例目标：smoke）──
    task = await task_service.create_task(
        project_id=pid, title="每日冒烟", purpose="验证核心接口探活与安全基线",
        scenario_tags={"all": ["smoke"]},
        exec_type="scheduled", schedule={"kind": "cron", "expr": "0 8 * * *"},
        env_id=None, global_params={"base_url": BASE_URL},
        notify={"on": ["fail"], "channels": [{"type": "webhook", "url": "https://example.com/hook"}]},
        created_by=me,
    )
    print(f"[task] {task.code} 每日冒烟(cron 08:00)")

    # ── 执行：引擎真发 HTTP + PgResultWriter 落结果 ──
    from testing_platform.engine.executor import PgResultWriter as PGW

    run = await task_service.run_task(task.id, trigger="manual")
    print(f"[run] {run.code} pending，开始执行 2 场景…")

    loader = PgScenarioLoader()
    writer = PGW(run_id=run.id, auto_create_tables=False)
    for scenario_id, case in [(s1.id, c1), (s2.id, c2)]:
        ctx = ExecutionContext()
        ctx.resolver.set_scope("env", {"base_url": BASE_URL})
        ctx.resolver.set_scope("global", dict(run.global_params or {}))
        ctx.case_refs = [str(case.id)]
        result = await ScenarioExecutor(loader).execute(scenario_id, ctx)
        await writer.write(result, ctx)
        print(f"  [{result.scenario_code}] {result.status}  {[(s.step_key, s.status) for s in result.steps]}")

    # ── 报告（生成时快照）──
    rpt = await generate_report(run.id, title="每日冒烟 · 首次端到端", test_version="v1-skeleton")
    snap = rpt.snapshot or {}
    ov = snap.get("overview", {})
    print(f"[report] {rpt.code} pass={ov.get('pass')} fail={ov.get('fail')} 用例 {ov.get('total')}")

    print("\n=== 种子数据完成。前端切 USE_MOCK_FALLBACK=false 即可见 ===")


if __name__ == "__main__":
    asyncio.run(main())
