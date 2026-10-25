"""执行期 AI/agent 通道（ai_judge / agent_action / agent_workflow 步骤用）。

**放在平台根、不放 ``engine/`` 包**——引擎护栏 ``test_engine_never_calls_llm``
禁止 engine 包 import agent/LLM（引擎是确定性模块，与 AI 物理分离）。本模块是
「AI 能力」的**可注入 provider**：``run.execute`` 在带 ``api_key_id`` 时构造一个
:class:`AICapability` 塞进 ``ExecutionContext.ai``（engine 只见鸭子类型，不 import 本模块），
三个 AI 步骤工具从 ctx 取用；不带时命中即报可读错误（不静默、不假装通过）。

三类能力：

* :meth:`AICapability.judge` —— **AI 判断**：把 ``prompt``（可含 ``{{}}``，执行器
  已解析）+ ``expected`` 交给平台网关 LLM，返回结构化判定 ``{passed, reason, raw}``。
  判定结果作为步骤的 actual，配合步骤 post_checks（如 ``{"type":"contains","expected":"true"}``）
  由引擎机械裁定——**AI 不直接判 pass/fail**（design 十六：AI 介入时仍由门禁裁定）。
* :meth:`AICapability.act` —— **Agent 操作**：驱动一个平台 Agent（``agent_id``）
  跑一轮任务，返回其最终产出文本。
* :meth:`AICapability.run_workflow` —— **Agent 工作流**：开一条工作流运行（role 或
  定义 UUID），返回运行 id 与初始状态（契约校验由 ``workflow.complete`` 收口）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .quality.evidence import normalize_evidence_class

logger = logging.getLogger(__name__)


class AICapabilityError(RuntimeError):
    """AI/agent 通道调用失败（缺 key、上游报错、agent 不可用…）。"""


#: AI 判断步骤的默认系统提示——要求模型只回 JSON，便于机械裁定。
#: verdict 四态（24 号文 Skill6 / skill-library-research R9）：
#:   passed/failed = 产品结论；inconclusive = 证据不足以裁定；blocked = 环境/凭据/工具不可用。
#:   ★inconclusive/blocked 不许写成 failed——工具故障不是产品失败（防假红）。
#: 输出禁裸百分数置信度；reasoning 走三段式（observation→implication→risk）。
_JUDGE_SYSTEM = (
    "You are a test assertion judge. Given a prompt (the assertion/question) and an "
    "expected outcome, decide whether the observed context satisfies it. Reply with a "
    'single JSON object: {"verdict": "passed|failed|inconclusive|blocked", '
    '"observation": "<what the evidence shows>", "implication": "<why that matters>", '
    '"risk": "<what could still be wrong / unverified>", '
    '"evidence_class": "executed|static|inferred|claimed"}. '
    "Use inconclusive when evidence is insufficient to decide, blocked when the "
    "environment/credentials/tooling prevented the check — never fake a failed. "
    "No confidence percentages. No prose outside the JSON."
)

#: verdict 归一：模型常见变体 → 规范四态。
_VERDICT_ALIASES: dict[str, str] = {
    "pass": "passed", "true": "passed", "success": "passed",
    "fail": "failed", "false": "failed", "failure": "failed",
    "unknown": "inconclusive", "unclear": "inconclusive", "skip": "inconclusive",
    "error": "blocked", "env": "blocked", "unavailable": "blocked",
}


def _normalize_verdict(raw: Any) -> str:
    """verdict 归一到四态；不认识的值保守落 inconclusive（不假装 passed/failed）。"""
    v = str(raw or "").strip().lower()
    if v in ("passed", "failed", "inconclusive", "blocked"):
        return v
    return _VERDICT_ALIASES.get(v, "inconclusive")


@dataclass
class AICapability:
    """一次执行期可用的 AI/agent 能力（由 run.execute 构造并注入 ctx）。

    ``api_key`` 是平台网关明文 key（由 api_key_id 解析得到）；``model`` 是推理模型。
    ``caller`` 是发起用户（agent_action 时按调用者身份跑，与对话页同口径）。
    """

    api_key: str
    model: str
    caller: Optional[str] = None
    api_key_name: str = ""
    #: 判定结果落库/回带用（可选）。
    usage: dict = field(default_factory=dict)

    async def judge(self, prompt: str, expected: Any = None, context: Any = None) -> dict[str, Any]:
        """AI 判断：返回 ``{passed, reason, raw}``（passed 由模型 JSON 解析，缺省 False）。"""
        if not self.api_key:
            raise AICapabilityError("ai_judge 需要平台 API Key（run.execute 传 api_key_id）")
        user = prompt or ""
        if expected is not None:
            user += f"\n\n[expected] {expected}"
        if context is not None:
            user += f"\n\n[context] {_short(context)}"
        content = await self._chat([
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user},
        ])
        verdict = _parse_judge(content)
        verdict["raw"] = content
        return verdict

    async def act(self, instruction: str, agent_id: Optional[int] = None) -> dict[str, Any]:
        """Agent 操作：驱动平台 Agent 跑一轮任务，返回 ``{ok, output, agent_id}``。"""
        if not instruction:
            raise AICapabilityError("agent_action 缺少 instruction")
        try:
            from agent.agent_main import GenericAgent
        except ImportError as exc:  # pragma: no cover - 部署相关
            raise AICapabilityError(f"agent_action 不可用（agent 模块缺失）：{exc}") from exc

        agent = GenericAgent(agent_id=agent_id, run_as_caller=self.caller or None)
        try:
            events = await agent.run_task(instruction)
        except Exception as exc:  # noqa: BLE001 — 统一包成 AICapabilityError
            raise AICapabilityError(f"agent_action 运行失败：{type(exc).__name__}: {exc}") from exc
        # run_task 返回事件列表；取最后一条 assistant 文本作为产出。
        output = _last_assistant_text(events)
        return {"ok": True, "output": output, "agent_id": agent_id}

    async def run_workflow(self, workflow_id: str, input_vars: Any = None) -> dict[str, Any]:
        """Agent 工作流：开一条工作流运行，返回 ``{ok, run_id, status, role}``。"""
        if not workflow_id:
            raise AICapabilityError("agent_workflow 缺少 workflow_id")
        try:
            from testing_platform.workflow import start_run
            from testing_platform.workflow.service import _resolve_def  # type: ignore[attr-defined]
        except ImportError as exc:  # pragma: no cover
            raise AICapabilityError(f"agent_workflow 不可用（workflow 模块缺失）：{exc}") from exc
        try:
            wdef = await _resolve_def(workflow_id)
            run = await start_run(workflow_id, input_vars or {}, agent_runtime="engine")
        except Exception as exc:  # noqa: BLE001
            raise AICapabilityError(f"agent_workflow 启动失败：{type(exc).__name__}: {exc}") from exc
        return {"ok": True, "run_id": str(run.id), "status": run.status, "role": wdef.role}

    async def _chat(self, messages: list[dict]) -> str:
        """经平台网关调一次非流式 LLM，返回 assistant 文本。"""
        try:
            from agent.llm_bridge import GatewayLLMBridge
        except ImportError as exc:  # pragma: no cover
            raise AICapabilityError(f"LLM 通道不可用（llm_bridge 缺失）：{exc}") from exc
        bridge = GatewayLLMBridge(
            api_key=self.api_key, api_key_name=self.api_key_name, model=self.model,
        )
        try:
            resp = await bridge.chat(messages)
        except Exception as exc:  # noqa: BLE001
            raise AICapabilityError(f"AI 调用失败：{type(exc).__name__}: {exc}") from exc
        if resp.usage:
            self.usage = dict(resp.usage)
        return resp.content or ""


def _parse_judge(content: str) -> dict[str, Any]:
    """从模型输出里抽 {verdict, observation, implication, risk, evidence_class}。

    四态 verdict（24 号文 Skill6）：passed/failed/inconclusive/blocked——
    解析不出 → inconclusive（保守，不假装通过也不假装失败：工具故障≠产品失败）。
    兼容旧二态输出（{"passed": true|false}）。
    """
    text = (content or "").strip()
    # 去掉可能的 ```json 围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    data: Any = None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        pass
    if isinstance(data, dict):
        if "verdict" in data:
            verdict = _normalize_verdict(data.get("verdict"))
        elif "passed" in data:
            # 旧二态兼容
            verdict = "passed" if bool(data.get("passed")) else "failed"
        else:
            verdict = "inconclusive"
        return {
            "verdict": verdict,
            "passed": verdict == "passed",  # 旧消费者兼容位
            "observation": str(data.get("observation") or ""),
            "implication": str(data.get("implication") or ""),
            "risk": str(data.get("risk") or ""),
            "evidence_class": normalize_evidence_class(data.get("evidence_class")),
            "reason": str(data.get("reason") or data.get("observation") or ""),  # 旧键兼容
        }
    # 兜底：在文本里找 true/false 关键词（保守——找不到= inconclusive 非 failed）
    low = text.lower()
    if '"verdict": "passed"' in low or '"passed": true' in low or "'passed': true" in low:
        return {"verdict": "passed", "passed": True, "observation": text[:200],
                "implication": "", "risk": "", "evidence_class": None, "reason": text[:200]}
    if '"verdict": "failed"' in low or '"passed": false' in low or "'passed': false" in low:
        return {"verdict": "failed", "passed": False, "observation": text[:200],
                "implication": "", "risk": "", "evidence_class": None, "reason": text[:200]}
    return {"verdict": "inconclusive", "passed": False, "observation": text[:200],
            "implication": "", "risk": "解析不出结构化判定", "evidence_class": None,
            "reason": text[:200]}


def _last_assistant_text(events: Any) -> str:
    """从 run_task 的事件列表里取最后一条 assistant 文本。"""
    if not isinstance(events, list):
        return str(events or "")
    for item in reversed(events):
        if isinstance(item, dict):
            if item.get("type") == "assistant" and item.get("content"):
                return str(item["content"])
            if item.get("role") == "assistant" and item.get("content"):
                return str(item["content"])
    return ""


def _short(value: Any, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = ["AICapability", "AICapabilityError"]
