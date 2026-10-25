"""One agent turn's event stream, folded into both stores — the single implementation.

Purpose: replace the two hand-copied ``on_event`` bodies in ``agent/api.py`` and
``agent/cdp_chat_service.py``. Both accumulate the same ``collected_*`` state,
stamp the same offsets, checkpoint the same projection and publish the same
enriched frame; keeping them in sync by hand is how they drifted (CDP never
aggregated subagents, its checkpoint omitted ``subagents=``).

The writer emits to two stores per event:

* **projection** (``agent_messages``) — throttled via the caller's ``persist``;
  authoritative for pagination, envelope, LLM context and the no-frame fallback.
* **event log** (``agent_message_events``) — one row per replayable frame;
  authoritative for timeline order. Replay uses this (see
  ``conversation_store.fold_frames_to_fields``).

``assistant_msg_id`` must be assigned *before* the turn runs — frames are keyed to
it from the first event.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from agent.context_manager import attach_tool_result, utf16_len

# 实时态事件：驱动活 UI，不是对话内容，不入日志（回放出来是读者用不上的行）。
_LIVE_ONLY_TYPES = frozenset({
    "turn_start", "turn_end", "retry", "conversation_state", "run_state",
    "approval_resolved",
})


def _apply_subagent_event(subagents: list[dict[str, Any]], etype: str, event: dict) -> None:
    """把一个 subagent_* 事件不可变地并入子 agent 卡片列表（实时展示 + 落库共用）。

    与前端 agent-stream-events.ts 同语义：start 建卡、content/reasoning/tool_call/
    tool_result 就地更新、end 收尾、done 记 usage（token 用量）。字段名前后端一致
    （id/name/task/status/content/reasoning/toolCalls/summary/usage），历史回放才能
    直接喂给同一套渲染。
    """
    sid = str(event.get("subagent_id") or "")
    if not sid:
        return

    def _find() -> dict[str, Any] | None:
        for s in subagents:
            if s.get("id") == sid:
                return s
        return None

    if etype == "subagent_start":
        if _find() is None:
            subagents.append({
                "id": sid,
                "name": str(event.get("subagent_name") or "子 Agent"),
                "task": str(event.get("task") or ""),
                "status": "running",
                "content": "",
                "reasoning": "",
                "toolCalls": [],
            })
        return
    s = _find()
    if s is None:
        # 没收到 start 就来的事件（订阅晚到）：补一张卡再更新，避免整段丢失。
        s = {"id": sid, "name": str(event.get("subagent_name") or "子 Agent"),
             "task": "", "status": "running", "content": "", "reasoning": "", "toolCalls": []}
        subagents.append(s)
    if etype == "subagent_content":
        s["content"] = (s.get("content") or "") + str(event.get("text") or "")
    elif etype == "subagent_reasoning":
        # 子 agent 的思考：与主 agent 的 reasoning 同待遇——独立字段累积、随卡片落库，
        # 刷新后历史才能恢复子 agent 的思考（原先只实时展示、不落库，刷新即丢）。
        s["reasoning"] = (s.get("reasoning") or "") + str(event.get("text") or "")
    elif etype == "subagent_tool_call":
        s.setdefault("toolCalls", []).append({
            "name": str(event.get("name") or "unknown"),
            "args": event.get("args"),
            "status": "running",
        })
    elif etype == "subagent_tool_result":
        for tc in reversed(s.get("toolCalls") or []):
            if tc.get("name") == event.get("name") and tc.get("status") == "running":
                tc["result"] = event.get("data")
                tc["status"] = "done"
                break
    elif etype == "subagent_end":
        s["status"] = "done"
        if event.get("summary"):
            s["summary"] = str(event.get("summary"))
    elif etype == "subagent_error":
        s["status"] = "done"
        s["error"] = str(event.get("message") or "子 agent 出错")
    elif etype == "subagent_done" and isinstance(event.get("usage"), dict):
        # 子 agent 的 token 用量：agent_loop 每轮 done 都带 usage，取最后一个（= 该子
        # agent 末轮的上下文占用），与主对话口径一致。
        s["usage"] = event["usage"]


def merge_question_into_content(content: str, event: dict) -> str:
    """把 ask_user 的提问并入正文（历史回放可见；问题正文也是上下文的一部分）。"""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    question = str(data.get("question") or event.get("message") or "")
    if not question:
        return content
    return f"{content}\n\n{question}".strip("\n")


async def resolve_context_window(model_id: str) -> int:
    """模型的有效上下文上限（token）。**PG 事实源优先**（model_groups 表）。

    前端的 token 详情原先拿「会话选中的模型名」去浏览器侧拉的模型目录里 join
    ``max_context_tokens``——模型列表请求失败退化成无上限的兜底项、自定义组名与
    真实模型名对不上，都会 join 失败显示「上限未知」，哪怕 PG 里明明配了 1M。
    服务端知道自己本轮用的是哪个模型（done.model），把上限随 usage 一起发下去，
    前端不必再猜。

    口径（server/db.load_model_catalog_source 同源）：``model_groups`` 是唯一真相
    源——kind='real' 行的 metadata JSONB 承载真实模型元数据；kind='custom' 行是
    自定义模型组，组名本身没有元数据，按成员取**最小**窗口（组能路由到的最小窗口
    才是这轮真正安全的上限）。旧 ``model_metadata`` 表与内存目录只作兜底。
    """
    if not model_id:
        return 0
    try:
        from db import PostgresClient

        if PostgresClient.pool:
            group = await PostgresClient.get_model_group(model_id)
            if group is not None:
                if str(group.get("kind") or "") == "real":
                    try:
                        window = int((group.get("metadata") or {}).get("max_context_tokens") or 0)
                    except (TypeError, ValueError):
                        window = 0
                    if window > 0:
                        return window
                # 自定义组：组名没有元数据行，成员窗口取最小（组能路由到的最小窗口
                # 才是这轮真正安全的上限）。一条 SQL 拉齐，不逐成员往返。
                members = [m for m in (group.get("models") or []) if isinstance(m, str) and m]
                if members:
                    rows = await PostgresClient.pool.fetch(
                        """
                        SELECT name, metadata->>'max_context_tokens' AS ctx
                        FROM model_groups
                        WHERE name = ANY($1::text[])
                        """,
                        members,
                    )
                    windows: list[int] = []
                    for row in rows:
                        try:
                            value = int(row["ctx"] or 0)
                        except (TypeError, ValueError):
                            value = 0
                        if value > 0:
                            windows.append(value)
                    if windows:
                        return min(windows)
            # 无 model_groups 行（或 real 行没配窗口）→ 旧表兜底（存量数据仍在）。
            direct = await PostgresClient.get_model_max_context_tokens(model_id)
            if direct:
                return int(direct)
    except Exception:  # noqa: BLE001 — PG 读不到就退内存目录，绝不阻断回合收口
        pass
    try:
        from rate_limiter import ModelClientPool

        info = ModelClientPool.get_model_info(model_id) or {}
        return int(info.get("max_context_tokens") or 0)
    except Exception:  # noqa: BLE001
        return 0


class ConversationTurnWriter:
    """Accumulates one assistant turn's events and writes both stores.

    ``persist`` is the caller's throttled projection writer; ``sign_media`` is the
    caller's transient media signer (CDP only). The event-log append and the seq
    allocation live here so both entry points share one implementation.
    """

    def __init__(
        self,
        *,
        conversation_id: str,
        assistant_msg_id: int,
        resume_from: Optional[dict] = None,
        attachment_owner: Optional[str] = None,
        sign_media: Optional[Callable[[dict], None]] = None,
        turn_number: int = 0,
    ) -> None:
        self.conversation_id = conversation_id
        self.assistant_msg_id = assistant_msg_id
        self.turn_number = turn_number
        self._attachment_owner = attachment_owner
        self._sign_media = sign_media
        self._last_persist_ts = 0.0

        self.tool_calls: list[dict[str, Any]] = []
        self.tool_results: list[Any] = []
        self.media: list[dict[str, Any]] = []
        self.usage: dict[str, Any] | None = None
        self.reasoning = ""
        self.content = ""
        self.subagents: list[dict[str, Any]] = []

        if resume_from is not None:
            # 续跑种子：旧正文/旧工具卡/旧结果保留，本轮新增的追加在后面。
            self.content = str(resume_from.get("content") or "")
            old_calls = [c for c in (resume_from.get("tool_calls") or []) if isinstance(c, dict)]
            old_results = list(resume_from.get("tool_results") or [])
            self.tool_calls = [dict(c) for c in old_calls]
            self.tool_results = old_results[: len(old_calls)]

    # ── frame → 累积器（同一套代码，读侧折叠也走这里） ────────────────

    def apply_frame(self, event: dict) -> dict[str, Any]:
        """把事件应用到累积器；返回本轮用的 offsets（供出版前取值）。"""
        etype = event.get("type")
        question_offset: int | None = None
        if etype == "tool_call":
            self.tool_calls.append({
                "id": event.get("id"),
                "name": event.get("name"),
                "args": event.get("args"),
                "status": "running",
                "content_offset": utf16_len(self.content),
                "reasoning_offset": utf16_len(self.reasoning),
            })
        elif etype == "reasoning":
            self.reasoning += str(event.get("text") or "")
        elif etype == "content":
            self.content += str(event.get("text") or "")
        elif etype == "tool_result":
            attach_tool_result(
                self.tool_calls, self.tool_results,
                call_id=event.get("id"), index=event.get("index"), result=event.get("data"),
            )
        elif etype == "question":
            # pre-merge offset（晚加入的订阅端快照里可能已含这段问题文本，靠 offset 判重）
            question_offset = utf16_len(self.content)
            self.content = merge_question_into_content(self.content, event)
            # ask_user 的候选答案要显示在问题文本之后：把该调用的 content_offset 推到合并后
            for i in range(len(self.tool_calls) - 1, -1, -1):
                if self.tool_calls[i].get("name") == "ask_user":
                    if self.tool_calls[i].get("content_offset") is not None:
                        self.tool_calls[i]["content_offset"] = utf16_len(self.content)
                    break
        elif etype == "done" and isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        elif isinstance(etype, str) and etype.startswith("subagent_"):
            _apply_subagent_event(self.subagents, etype, event)
        return {"question_offset": question_offset}

    # ── live 持有者：累积 → 记帧 → 广播（由 caller 负责）→ 节流落库 ──

    async def handle(self, event: dict) -> dict:
        etype = str(event.get("type") or "")
        # 必须在 apply_frame 之前补 context_window：done 事件的 usage 是本方法唯一
        # 拿到它的地方，而 apply_frame 会把 usage 一并收进累积器（随后落库）——晚一步
        # 补就只进了广播帧、没进消息行的 usage 列，刷新后 token 详情照样显示「上限未知」。
        if etype == "done" and isinstance(event.get("usage"), dict) and not event["usage"].get("context_window"):
            # done 帧附带本轮模型的有效上下文上限（见 resolve_context_window）。同一个
            # usage dict 既随帧广播给前端、又随 caller 的终态 update_message 落库；
            # 顶层 context_window 给不走 usage 的帧消费者兜底。
            model_name = str(event.get("model") or "")
            window = await resolve_context_window(model_name) if model_name else 0
            if window > 0:
                event["context_window"] = window
                event["usage"]["context_window"] = window

        meta = self.apply_frame(event)

        if etype == "tool_call":
            await self.checkpoint()
        elif etype in ("content", "reasoning"):
            await self._throttled_checkpoint()
        elif etype == "tool_result":
            if self._attachment_owner is not None:
                try:
                    from agent.context_manager import _collect_attachment_media
                    await _collect_attachment_media(event, self.media, self._attachment_owner, self.conversation_id)
                except Exception:  # noqa: BLE001 — 附件认领失败不阻断对话
                    logger.debug("[turn_writer] media claim failed", exc_info=True)
            if self._sign_media is not None:
                try:
                    self._sign_media(event)
                except Exception:  # noqa: BLE001
                    logger.debug("[turn_writer] media sign failed", exc_info=True)
            await self.checkpoint()
        elif etype == "question":
            if self._attachment_owner is not None:
                try:
                    from agent.context_manager import _collect_attachment_media
                    await _collect_attachment_media(event, self.media, self._attachment_owner, self.conversation_id)
                except Exception:  # noqa: BLE001
                    logger.debug("[turn_writer] media claim failed", exc_info=True)
            await self.checkpoint()
            if self._sign_media is not None:
                try:
                    self._sign_media(event)
                except Exception:  # noqa: BLE001
                    logger.debug("[turn_writer] media sign failed", exc_info=True)
        elif etype in ("subagent_end", "subagent_error"):
            await self.checkpoint()

        # 记事件帧（best-effort，失败绝不中断对话）。
        await self._append_frame(event)

        out = dict(event)
        out["message_id"] = self.assistant_msg_id
        if etype in ("content", "reasoning"):
            _txt = str(event.get("text") or "")
            _acc = self.content if etype == "content" else self.reasoning
            # offset 用 UTF-16 长度（对齐 JS String.length）：订阅端拿它与自己的
            # content.length 直接比大小，用码点长度会让含 emoji 的正文在增量对齐时
            # 错位、被当成「已含」而丢弃。纯 BMP 正文两种度量相同。
            out["offset"] = max(0, utf16_len(_acc) - utf16_len(_txt))
        elif etype == "question" and meta["question_offset"] is not None:
            out["offset"] = meta["question_offset"]
        return out

    async def checkpoint(self) -> None:
        """工具调用/附件是天然检查点：立即落库，保证刷新后看得到「说了什么、做了哪步」。

        把当前累积的投影字段（含 subagents——CDP 原先漏写）写回 assistant 消息，
        status 仍留 streaming；正常结束时 caller 的终态 update_message 覆盖成最终态。
        """
        try:
            from agent import conversation_store
            await conversation_store.update_message(
                self.assistant_msg_id,
                content=self.content,
                tool_calls=self.tool_calls or None,
                tool_results=self.tool_results or None,
                media=self.media or None,
                status="streaming",
                reasoning=self.reasoning,
                subagents=self.subagents or None,
            )
            self._last_persist_ts = time.monotonic()
        except Exception:  # noqa: BLE001 — 中途落库失败不能中断主流程
            pass

    async def _throttled_checkpoint(self) -> None:
        """高频 delta（正文/思考）最多 1.5s 落一次，保证审批挂起/断连前流出的内容不丢。"""
        if time.monotonic() - self._last_persist_ts > 1.5:
            await self.checkpoint()

    async def _append_frame(self, event: dict) -> None:
        from agent import conversation_store

        etype = str(event.get("type") or "")
        if etype in _LIVE_ONLY_TYPES or not etype:
            return
        try:
            item_type = etype
            tool_name = str(event.get("name") or "")
            if etype == "question":
                # 让读者不必解析 item_json 就能筛出 ask_user，与 tool_name 索引一致。
                tool_name = "ask_user"
            await conversation_store.add_message_event(
                self.conversation_id,
                message_id=self.assistant_msg_id,
                item_type=item_type,
                item=event,
                event_kind=etype,
                tool_name=tool_name,
                subagent_id=str(event.get("subagent_id") or ""),
                turn_number=self.turn_number,
                status=str(event.get("status") or ""),
            )
        except Exception:  # noqa: BLE001 — 记帧失败绝不阻断对话
            logger.debug("[turn_writer] append frame failed", exc_info=True)

    # ── 收尾（终态 update_message 用） ──────────────────────────────────

    def final_state(self, *, status: str, error: Optional[str] = None,
                    model: Optional[str] = None) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": self.tool_calls or None,
            "tool_results": self.tool_results or None,
            "media": self.media or None,
            "status": status,
            "reasoning": self.reasoning,
            "subagents": self.subagents or None,
            "error": error,
            "model": model,
        }


def fold_frames_to_fields(frames: list[dict]) -> dict[str, Any]:
    """把某条消息的事件帧（已按 seq 排序）重放成投影字段。

    与 live writer 跑同一段 ``apply_frame``，因此对**完整**帧集，输出 == 写入时投影的
    ``content/reasoning/tool_calls/tool_results``。``item_json`` 里存的是原始事件 dict
    （见 ConversationTurnWriter._append_frame），直接喂 apply 即可。

    返回 ``{content, reasoning, tool_calls, tool_results}``；帧为空时各为默认值。
    """
    w = ConversationTurnWriter(conversation_id="", assistant_msg_id=0)
    for frame in frames:
        item = frame.get("item") if isinstance(frame, dict) else None
        if not isinstance(item, dict):
            # 兼容直接把 item_json 解析后的 dict 传进来的调用方
            item = frame if isinstance(frame, dict) and "type" in frame else None
        if item is None:
            continue
        w.apply_frame(item)
    return {
        "content": w.content,
        "reasoning": w.reasoning,
        "tool_calls": w.tool_calls,
        "tool_results": w.tool_results,
    }

