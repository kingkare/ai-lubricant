"""会话状态事件 → ``ConversationState.run_status`` 的映射层。

历史（见 git）：本模块曾自带一套 ``run_state`` JSON 的「最新状态快照」——内存、
落库（``agent_conversations.run_state`` 列）、广播 ``run_state`` 事件三件套。
框架重构后，这三件事收敛到 `agent/conversation_state.py` 的 ``update``（改内存 →
写穿 ``run_status``/``run_status_info`` 两列 → 推 ``conversation_state`` 事件），
本模块只保留**一件事**：把 agent 事件流翻译成状态枚举。

调用方（`agent/api.py` / `agent/cdp_chat_service.py` 的 ``on_event``）仍在每个
agent 事件后调 ``await apply(conv_id, event)``——一个入口覆盖状态的全部更新。

状态枚举与中文标签见 `agent/conversation_state.py`。高频 delta（content/reasoning）
也进本映射，靠 ``conversation_state.update`` 的字段去重短路——只有状态**真变化**
（thinking ↔ processing 翻转）才落库/推送，一回合写库次数 ≈ 阶段切换数。
"""

from __future__ import annotations

import logging

from agent import conversation_state as _cs

logger = logging.getLogger(__name__)


def _stage_of(event: dict) -> tuple[str, str] | None:
    """agent 事件 → ``(run_status, run_status_info)``；返回 None = 不改状态。"""
    etype = str(event.get("type") or "")

    if etype == "turn_start":
        return _cs.STATUS_SENDING, ""

    if etype == "reasoning":
        # 思考流式中：有 reasoning 增量即「思考中」。
        return _cs.STATUS_THINKING, ""

    if etype == "content":
        # 正文流式中：已有正文即「处理中」（模型在出结果）。
        return _cs.STATUS_PROCESSING, ""

    if etype == "tool_call":
        return _cs.STATUS_EXECUTING, str(event.get("name") or "")

    if etype == "retry":
        # 带第几次 + 倒计时。delay_ms 为 0/缺失（无退避）时只报次数。
        attempt = int(event.get("attempt") or 0)
        max_m = int(event.get("max") or event.get("max_attempts") or 0)
        info = ""
        if max_m > 0 and attempt > 0:
            info = f"第{attempt}/{max_m}次"
        elif attempt > 0:
            info = f"第{attempt}次"
        delay_ms = int(event.get("delay_ms") or 0)
        if delay_ms > 0:
            secs = max(1, round(delay_ms / 1000))
            info = f"{info} · {secs}s后重试" if info else f"{secs}s后重试"
        return _cs.STATUS_RETRYING, info

    if etype == "confirmation_required":
        # 等待审批：按设计不显示 status_info（审批卡本身就在底部，附言重复）。
        return _cs.STATUS_WAITING_APPROVAL, ""

    if etype == "question":
        # ask_user 提问 → 本轮结束、球在用户。附言带问题正文（首行截断）。
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        q = str(data.get("question") or event.get("message") or "").strip()
        return _cs.STATUS_WAITING_INPUT, q.split("\n", 1)[0][:200]

    if etype in ("error", "aborted"):
        msg = str(event.get("message") or event.get("error") or "")
        status = _cs.STATUS_ERROR if etype == "error" else _cs.STATUS_ABORTED
        return status, msg[:400]

    if etype == "done":
        # 正常完结 → 等待用户输入（静息态，不再是清空）。上一条工具因审批被拒/超时
        # 中止时，on_event 已在别处发过 error/aborted，这里不再覆盖。
        return _cs.STATUS_WAITING_INPUT, ""

    return None


def is_stage_event(event: dict) -> bool:
    """是否会改状态的事件（供调用方短路掉 tool_result/usage 等无关事件）。"""
    return _stage_of(event) is not None


async def apply(conv_id: str, event: dict) -> None:
    """把 agent 事件翻译成状态并更新（内存 + 落库 + 推送）。绝不抛、绝不阻断对话。

    幂等：同一事件重放写入同一状态，``conversation_state.update`` 字段去重短路。
    不同进程/重启后的状态漂移由「首个阶段事件覆盖写」自然收敛。
    """
    mapped = _stage_of(event)
    if mapped is None:
        return
    status, info = mapped
    try:
        await _cs.update(conv_id, run_status=status, run_status_info=info)
    except Exception as exc:  # noqa: BLE001 — 状态是尽力而为的显示，失败只记日志
        logger.debug("[run_state] apply failed conv=%s: %s", conv_id, exc)


# 兼容旧调用方（publish 与 apply 曾成对调用）。状态广播已由 update 内置完成，
# 这里保留为 no-op —— 改调用方要动两个文件，且两处都还要 apply。
async def publish(conv_id: str, event: dict) -> None:
    """已并入 ``apply``（update 内含广播）。保留签名避免改动两个调用方。"""
    return None


def restore(conv: dict | None) -> dict | None:
    """从会话行恢复状态（供加载会话时显示）。读新 ``run_status`` 列。

    返回 ``{"run_status":..., "run_status_info":...}`` 形状，或 None（尚未开始/
    老会话无此数据）。旧 ``run_state`` JSON 列已废弃、不再读取。
    """
    if not conv:
        return None
    status = str(conv.get("run_status") or "")
    if not status:
        return None
    return {
        "run_status": status,
        "run_status_info": str(conv.get("run_status_info") or ""),
    }
