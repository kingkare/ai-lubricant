"""AI/agent 步骤工具测试（ai_judge / agent_action / agent_workflow）。

**引擎护栏**（``test_engine_never_calls_llm``）要求 engine 包不 import agent/LLM——
所以这三个工具只认 ``ctx.ai`` 的鸭子协议（judge/act/run_workflow），具体通道
（:class:`testing_platform.ai_channel.AICapability`）由 ``run.execute`` 注入。

本测试用**假 ai 通道**验证：
* 工具正确解析 params、调对应方法、把产物绑进 {{response.*}}；
* ctx.ai 为空时命中即报 ToolExecutionError（不静默、不假装通过）；
* ai_judge 的判定结果作为 actual，配合 post_checks 由引擎机械裁定 pass/fail。

运行：``python -m pytest tests/test_engine_ai_steps.py -q``
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from testing_platform.engine import (
    DictScenarioLoader,
    ExecutionContext,
    ScenarioExecutor,
    STATUS_FAIL,
    STATUS_PASS,
    ToolExecutionError,
)
from testing_platform.engine.assertions import build_default_registry as build_assertions
from testing_platform.engine.tools import build_default_registry, resolve_tool_for_step


def run(coro):
    return asyncio.run(coro)


class FakeAI:
    """假 AI 通道：记录调用，返回固定产物（不碰真 LLM/agent）。"""

    def __init__(self, *, passed=True, reason="ok", output="agent done", wf_run="wf-run-1"):
        self.passed = passed
        self.reason = reason
        self.output = output
        self.wf_run = wf_run
        self.judge_calls: list[dict] = []
        self.act_calls: list[dict] = []
        self.wf_calls: list[dict] = []

    async def judge(self, prompt, expected=None, context=None):
        self.judge_calls.append({"prompt": prompt, "expected": expected, "context": context})
        return {"passed": self.passed, "reason": self.reason, "raw": "raw"}

    async def act(self, instruction, agent_id=None):
        self.act_calls.append({"instruction": instruction, "agent_id": agent_id})
        return {"ok": True, "output": self.output, "agent_id": agent_id}

    async def run_workflow(self, workflow_id, input_vars=None):
        self.wf_calls.append({"workflow_id": workflow_id, "input_vars": input_vars})
        return {"ok": True, "run_id": self.wf_run, "status": "pending", "role": "coder"}


def _executor_with(scenario):
    registry = build_default_registry()
    return ScenarioExecutor(DictScenarioLoader([scenario]), tools=registry, assertions=build_assertions())


# ── 1) resolve：三类步骤映射到对应工具名 ──
def test_resolve_ai_step_types():
    assert resolve_tool_for_step({"type": "ai_judge"}) == "ai_judge"
    assert resolve_tool_for_step({"type": "agent_action"}) == "agent_action"
    assert resolve_tool_for_step({"type": "agent_workflow"}) == "agent_workflow"


# ── 2) ai_judge：判定通过 → 步骤 pass，{{response.passed}} 可读 ──
def test_ai_judge_pass_binds_response():
    scenario = {
        "id": "SCN-AI",
        "steps": [
            {
                "step_key": "judge",
                "seq": 10,
                "type": "ai_judge",
                "params": {"prompt": "响应是否表示成功？", "expected": "成功"},
                # 机械裁定：判定 actual 里 passed=true 才算过
                "post_checks": [{"type": "json_path", "path": "$.passed", "operator": "eq", "expected": True}],
            }
        ],
    }
    ctx = ExecutionContext()
    ai = FakeAI(passed=True)
    ctx.ai = ai
    result = run(_executor_with(scenario).execute("SCN-AI", ctx))
    assert result.status == STATUS_PASS, result.to_dict()
    assert ai.judge_calls and ai.judge_calls[0]["prompt"] == "响应是否表示成功？"
    assert result.steps[0].status == "pass"


# ── 3) ai_judge：判定 false → post_checks 不过 → 场景 fail ──
def test_ai_judge_false_fails_scenario():
    scenario = {
        "id": "SCN-AI2",
        "steps": [
            {
                "step_key": "judge",
                "seq": 10,
                "type": "ai_judge",
                "params": {"prompt": "是否成功？"},
                "post_checks": [{"type": "json_path", "path": "$.passed", "operator": "eq", "expected": True}],
            }
        ],
    }
    ctx = ExecutionContext()
    ctx.ai = FakeAI(passed=False)
    result = run(_executor_with(scenario).execute("SCN-AI2", ctx))
    assert result.status == STATUS_FAIL


# ── 4) ctx.ai 为空 → 命中 AI 步骤报 ToolExecutionError（不假装通过）──
def test_ai_step_without_channel_errors():
    from testing_platform.engine.tools import ai_judge_tool

    ctx = ExecutionContext()  # 无 ai
    step = {"step_key": "j", "type": "ai_judge", "params": {"prompt": "x"}}
    with pytest.raises(ToolExecutionError):
        run(ai_judge_tool(step, ctx))


# ── 5) agent_action：驱动假 agent，产出进 {{result.output}} ──
def test_agent_action_runs():
    from testing_platform.engine.tools import agent_action_tool

    ctx = ExecutionContext()
    ai = FakeAI(output="created user u1")
    ctx.ai = ai
    step = {"step_key": "a", "type": "agent_action", "params": {"instruction": "建个测试用户", "agent_id": 7}}
    res = run(agent_action_tool(step, ctx))
    assert res.result["output"] == "created user u1"
    assert ai.act_calls[0]["agent_id"] == 7


# ── 6) agent_action 缺 instruction → StepParamsError ──
def test_agent_action_requires_instruction():
    from testing_platform.engine.errors import StepParamsError
    from testing_platform.engine.tools import agent_action_tool

    ctx = ExecutionContext()
    ctx.ai = FakeAI()
    with pytest.raises(StepParamsError):
        run(agent_action_tool({"step_key": "a", "type": "agent_action", "params": {}}, ctx))


# ── 7) agent_workflow：开工作流，run_id 进产物 ──
def test_agent_workflow_starts():
    from testing_platform.engine.tools import agent_workflow_tool

    ctx = ExecutionContext()
    ai = FakeAI(wf_run="wf-42")
    ctx.ai = ai
    step = {"step_key": "w", "type": "agent_workflow", "params": {"workflow_id": "coder", "input_vars": {"k": 1}}}
    res = run(agent_workflow_tool(step, ctx))
    assert res.result["run_id"] == "wf-42"
    assert ai.wf_calls[0]["workflow_id"] == "coder"


# ── 8) 假 PG 通道 AICapability 的纯逻辑（judge 解析 / 兜底）──
def test_parse_judge_variants():
    from testing_platform.ai_channel import _parse_judge

    assert _parse_judge('{"passed": true, "reason": "ok"}')["passed"] is True
    assert _parse_judge('```json\n{"passed": false}\n```')["passed"] is False
    # 解析不出 → 保守 False
    assert _parse_judge("garbage")["passed"] is False
    # 文本里带 passed:true 兜底
    assert _parse_judge('the model said {"passed": true} eventually')["passed"] is True
