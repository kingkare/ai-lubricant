"""手动压缩上下文（/compact 或压缩按钮）服务层。

通用 agent 对话的自动压缩在 agent_loop 每轮 LLM 调用前跑（compress_history +
trim_messages_async，内存内、不落库）；手动压缩把摘要**持久化**到 DB，让压缩跨轮
生效——agent 每轮从 ClickHouse 全量重建历史，不落库的话下一轮就回去了。

持久化机制（零 schema 迁移）：
- 摘要作为一条 role=user、内容含 ``<history_summary>`` 标记的消息插入 agent_messages；
- ``rebuild_history_messages``（context_manager）把最后一条标记消息之前的全部行
  丢弃——摘要本身就是被压缩前缀的幸存记录，标记之后的行 + 摘要喂给 LLM；
- 显示层读 get_messages 不受影响，用户仍能看到完整历史。

web 与 CDP 两条链路（鉴权不同）共用本模块的 ``compact_conversation``：
- web：caller 走 get_conversation_owned 归属校验（api.py 端点做）。
- CDP：client_id 走 _require_conv_owned_by_client（cdp_chat_service.py 端点做）。
本模块只做压缩本身，不管鉴权；agent_id/agent_row 由端点从会话行传入。
"""
from __future__ import annotations

from typing import Any

from loguru import logger


async def compact_conversation(
    conv_id: str,
    *,
    agent_id: int,
    agent_row: dict | None = None,
) -> dict[str, Any]:
    """压缩一个会话的历史：LLM 摘要 → 插 <history_summary> 标记消息。

    返回 {ok, summarized, summary_chars, message_id?, error?}。
    summarized=False 表示没有可压缩的前缀（历史太短或已被压缩过）。
    """
    from agent import conversation_store
    from agent.agent_main import GenericAgent
    from agent.agent_loop import _make_summarizer
    from agent.context_manager import (
        make_summary_message,
        rebuild_history_messages,
        summarize_history_for_compact,
    )

    if not agent_id:
        return {"ok": False, "summarized": False, "error": "conversation has no agent"}

    history = await conversation_store.get_messages(conv_id)
    if len(history) <= 12:  # keep_recent(10) + 少量余量：太短没有可压缩的前缀
        return {"ok": True, "summarized": False, "summary_chars": 0}

    # LLM 走该 Agent 绑定的网关 key（与对话轮次同一条链路，计费/归属闭环）。
    ga = GenericAgent(agent_id=int(agent_id), agent_row=agent_row)
    llm = await ga._ensure_llm()
    summarizer = _make_summarizer(llm)
    if summarizer is None:
        return {"ok": False, "summarized": False, "error": "agent LLM has no chat capability"}

    # 对 rebuild 后的有效历史（system + 配对的 role=tool 线形状）做摘要——与每轮
    # 自动压缩看到的是同一种消息形状，摘要口径一致。
    rebuilt = rebuild_history_messages(history, system_prompt="")
    summary = await summarize_history_for_compact(rebuilt, summarizer, keep_recent=10)
    if not summary:
        return {"ok": True, "summarized": False, "summary_chars": 0}

    msg = await conversation_store.add_message(
        conv_id,
        role="user",
        content=make_summary_message(summary)["content"],
        status="done",
        turn_number=0,
    )
    logger.info(f"[compact] conversation {conv_id} compacted: {len(summary)} chars summary, msg_id={msg.get('id')}")
    return {"ok": True, "summarized": True, "summary_chars": len(summary), "message_id": msg.get("id")}
