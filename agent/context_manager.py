"""
Context Manager — history compression and trimming.

Pure code implementation for the mechanical path (no token counting library);
thresholds are character-based, matching GenericAgent's approach. An optional
LLM summarization pass (:func:`trim_messages_async`) is layered on top of the
GA pop-and-drop trim: the oldest stretch that is about to be discarded gets
condensed into one summary block first, so early intent survives compression
instead of vanishing. The summarizer is injected as a callback so this module
stays LLM-library-free (the loop binds it to the active client).

GenericAgent reference: llmcore.py compress_history_tags() + trim_messages_history()
"""

from __future__ import annotations

import inspect
import json
import re
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Default thresholds (characters, not tokens)
DEFAULT_CONTEXT_WINDOW = 30000      # matches GenericAgent BaseSession.context_win
TRIGGER_MULTIPLIER = 3              # cap = context_window * 3
DEFAULT_KEEP_RATE = 0.6             # trim target = cap * keep_rate
MIN_MESSAGES_AFTER_TRIM = 9         # never trim below this many messages

# Compression defaults
DEFAULT_KEEP_RECENT = 10            # don't compress messages newer than this
DEFAULT_MAX_TAG_LEN = 800           # truncate tag content to this many chars

# Call counter for interval-based compression
_compress_counter = 0


def _estimate_chars(messages: list[dict[str, Any]]) -> int:
    """Estimate total character count of messages (like GenericAgent's len(json.dumps(m)))."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(json.dumps(block, ensure_ascii=False))
                else:
                    total += len(str(block))
        # Also count tool_calls if present
        tc = m.get("tool_calls")
        if tc:
            total += len(json.dumps(tc, ensure_ascii=False))
    return total


def _truncate_tag_content(content: str, max_len: int) -> str:
    """Truncate content inside an XML-like tag, keeping head and tail."""
    if len(content) <= max_len:
        return content
    half = max_len // 2
    return content[:half] + "\n...[Truncated]...\n" + content[-half:]


def _compress_tool_calls(tool_calls: list[dict], max_len: int) -> list[dict]:
    """Truncate arguments in tool_calls."""
    compressed = []
    for tc in tool_calls:
        tc_copy = dict(tc)
        func = tc_copy.get("function", {})
        if isinstance(func, dict):
            func_copy = dict(func)
            args_str = func_copy.get("arguments", "")
            if isinstance(args_str, str) and len(args_str) > max_len:
                func_copy["arguments"] = _truncate_tag_content(args_str, max_len)
            tc_copy["function"] = func_copy
        compressed.append(tc_copy)
    return compressed


def _compress_tool_results(content: Any, max_len: int) -> Any:
    """Truncate tool result content."""
    if isinstance(content, str):
        if len(content) > max_len:
            return _truncate_tag_content(content, max_len)
        return content
    elif isinstance(content, list):
        compressed = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if len(text) > max_len:
                    block = {**block, "text": _truncate_tag_content(text, max_len)}
            compressed.append(block)
        return compressed
    return content


def compress_history(
    messages: list[dict[str, Any]],
    keep_recent: int = DEFAULT_KEEP_RECENT,
    max_len: int = DEFAULT_MAX_TAG_LEN,
    interval: int = 5,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Compress old messages by truncating tool-related content.

    Matches GenericAgent's compress_history_tags() behavior:
    - Operates on messages older than keep_recent from the end
    - Truncates tool_calls arguments and tool_results content
    - Only fires every 'interval' calls unless force=True

    Returns the messages list (modified in-place for efficiency).
    """
    global _compress_counter
    _compress_counter += 1

    if not force and (_compress_counter % interval != 0):
        return messages

    if len(messages) <= keep_recent:
        return messages

    # Compress messages from index 0 to len-keep_recent
    cutoff = len(messages) - keep_recent
    for i in range(cutoff):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "assistant":
            # Truncate tool_calls arguments
            tc = msg.get("tool_calls")
            if tc and isinstance(tc, list):
                msg["tool_calls"] = _compress_tool_calls(tc, max_len)

            # Truncate long assistant content (thinking blocks etc.)
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_len * 2:
                msg["content"] = _truncate_tag_content(content, max_len * 2)

        elif role in ("user", "tool"):
            # role=tool 是工具结果的标准载体（本项目 agent_loop 就是这么发的），
            # 早期这里只压 assistant/user，导致最大的一类消息完全不参与压缩，
            # 长任务下 role=tool 无上限堆积。
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > max_len:
                msg["content"] = _truncate_tag_content(content, max_len)
            elif isinstance(content, list):
                msg["content"] = _compress_tool_results(content, max_len)

    return messages


def _sanitize_leading_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """If the first message is a user message with tool_results, convert to plain text.

    This prevents orphaned tool_results after trimming (matching GenericAgent's
    _sanitize_leading_user_msg).
    """
    if not messages:
        return messages

    first = messages[0]
    if first.get("role") != "user":
        return messages

    content = first.get("content", "")
    if isinstance(content, list):
        # Convert tool_result blocks to text
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = block.get("content", "")
                if isinstance(text, list):
                    text = " ".join(
                        b.get("text", "") for b in text if isinstance(b, dict)
                    )
                new_content.append({"type": "text", "text": f"[tool_result] {text}"})
            else:
                new_content.append(block)
        first["content"] = new_content

    return messages


def _message_char_count(msg: dict[str, Any]) -> int:
    """Estimate character count of a single message."""
    total = 0
    content = msg.get("content", "")
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                total += len(json.dumps(block, ensure_ascii=False))
            else:
                total += len(str(block))
    tc = msg.get("tool_calls")
    if tc:
        total += len(json.dumps(tc, ensure_ascii=False))
    return total


def trim_messages(
    messages: list[dict[str, Any]],
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    keep_rate: float = DEFAULT_KEEP_RATE,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    max_tag_len: int = DEFAULT_MAX_TAG_LEN,
) -> list[dict[str, Any]]:
    """Trim messages when total size exceeds context window threshold.

    Matches GenericAgent's trim_messages_history() behavior:
    - cap = context_window * 3 (characters)
    - target = cap * keep_rate
    - First do aggressive compression (keep_recent=4, force=True)
    - If still over target, pop messages from front (keeping user/assistant pairs)
    - Minimum MIN_MESSAGES_AFTER_TRIM messages retained
    - After popping, sanitize new leading message
    """
    cap = context_window * TRIGGER_MULTIPLIER
    target = int(cap * keep_rate)

    current_chars = _estimate_chars(messages)
    if current_chars <= cap:
        return messages

    # Step 1: Aggressive compression
    compress_history(messages, keep_recent=4, max_len=max_tag_len, force=True)

    current_chars = _estimate_chars(messages)
    if current_chars <= target:
        return messages

    # Step 2: Pop messages from front, keeping user/assistant pairs. Protected
    # messages (the system prompt, runtime <history_summary> blocks) are never
    # popped: dropping either strips context that cannot be rebuilt.
    while current_chars > target and len(messages) > MIN_MESSAGES_AFTER_TRIM:
        # Find the first droppable message (skip protected ones at the front).
        drop_at = 0
        while drop_at < len(messages) and _is_protected(messages[drop_at]):
            drop_at += 1
        if drop_at >= len(messages) or len(messages) - drop_at <= MIN_MESSAGES_AFTER_TRIM:
            break
        messages.pop(drop_at)
        current_chars = _estimate_chars(messages)

        # Ensure we pop in user/assistant pairs:
        # If the new first message is 'assistant' (not 'system'), pop it too.
        while (
            messages
            and len(messages) > MIN_MESSAGES_AFTER_TRIM
            and not _is_protected(messages[0])
            and messages[0].get("role") not in ("system", "user")
        ):
            messages.pop(0)
            current_chars = _estimate_chars(messages)

    # Step 3: Sanitize new leading message
    _sanitize_leading_message(messages)

    logger.debug(
        "Context trimmed: %d messages, ~%d chars (target %d, cap %d)",
        len(messages),
        _estimate_chars(messages),
        target,
        cap,
    )

    return messages


# --- LLM summarization pass (optional; layer on top of the GA trim) ----------------

# The minimum size of the dropped prefix worth summarizing — one or two stray
# messages are not worth an LLM call. Threshold covers a handful of turns.
_SUMMARY_MIN_DROPPED = 6
# Cap on the summary text the LLM returns; summaries above this would defeat the
# trim. The summarizer is trusted to honor this; we also hard-cap defensively.
_SUMMARY_MAX_CHARS = 2500
# One run pays for a few summary calls per trim pass — drop after this many
# message bodies.
_SUMMARY_BATCH_LIMIT = 24

# System-prompt instruction for the summarizer. Folds to a single user-role
# summary message that survives the trim and is re-sent each turn until it too
# is summarized (compaction-basic's "retain tail, summarize head" shape).
_SUMMARY_INSTRUCTION = (
    "Summarize the conversation chunk below into a dense factual record for a "
    "continuing agent. Preserve: stated goals, decisions, key findings, exact "
    "file paths, commands tried and their outcomes, and any unresolved blockers. "
    "Do NOT narrate, do NOT opine, do NOT add anything not in the source. Use "
    "terse bullet lines. Limit to ~300 words."
)


def _transcript(chunk: list[dict[str, Any]]) -> str:
    """Flatten a message list into a compact labeled transcript for the summarizer."""
    lines: list[str] = []
    for m in chunk:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_result":
                        parts.append(f"[tool_result] {block.get('content','')}")
                else:
                    parts.append(str(block))
            content = "\n".join(p for p in parts if p)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        # Cap each message body so a huge tool result doesn't crowd the summary
        # prompt — the trim is already discarding it; the summary only needs the
        # gist, not the verbatim blob.
        if len(content) > _SUMMARY_MAX_CHARS:
            content = content[:_SUMMARY_MAX_CHARS] + "…"
        tc = m.get("tool_calls")
        if tc:
            names = ",".join(str(c.get("function", {}).get("name", "")) for c in tc if isinstance(c, dict))
            content = f"[calls {names}] {content}" if content else f"[calls {names}]"
        lines.append(f"{role}: {content}")
    out = "\n".join(lines)
    return out[: _SUMMARY_MAX_CHARS * _SUMMARY_BATCH_LIMIT // 4]


def _summary_message(summary: str) -> dict[str, Any]:
    """Wrap a summary string as a user-role message the loop and replay accept.

    Marked with a ``<history_summary>`` tag so the next GA compress pass treats
    it as a single compactable block; user-role so it survives the pair-aware
    trim and is itself summarizable later. The leading line is a plain marker
    so message_utils never treats it as user-authored bulk content.
    """
    return {
        "role": "user",
        "content": (
            "[Conversation summary generated by the runtime to preserve earlier "
            f"context — not a new user request.]\n<history_summary>\n{summary}\n</history_summary>"
        ),
    }


async def _summarize_chunk(
    summarizer: Callable[[str], Awaitable[str]],
    chunk: list[dict[str, Any]],
) -> str | None:
    """Run the summarizer over a chunk; return None on any failure."""
    if not chunk:
        return None
    transcript = _transcript(chunk)
    if not transcript.strip():
        return None
    try:
        summary = await summarizer(transcript)
    except Exception as exc:  # noqa: BLE001 - summary failure must not break the turn
        logger.debug("[context] summarizer call failed: %s", exc)
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None
    return summary.strip()[:_SUMMARY_MAX_CHARS]


def _is_summary_message(msg: dict[str, Any]) -> bool:
    """True for a runtime-injected ``<history_summary>`` block."""
    content = msg.get("content", "")
    return isinstance(content, str) and "<history_summary>" in content


def _is_protected(msg: dict[str, Any]) -> bool:
    """Messages the trim must never drop: the system prompt, and a summary block.

    Dropping the system prompt strips persona/rules/capabilities; dropping a
    summary block destroys the only surviving record of the dropped prefix. Both
    role checks (system) and content checks (summary) are deliberate: the summary
    is user-role, so a role check alone would not protect it.
    """
    return msg.get("role") == "system" or _is_summary_message(msg)


def _plan_dropped_prefix(
    messages: list[dict[str, Any]],
    *,
    target: int,
    keep_recent: int,
) -> tuple[list[dict[str, Any]], int]:
    """Simulate the GA pop loop to find the dropped prefix without mutating yet.

    Returns ``(dropped_messages, split_index)`` where ``messages[split_index:]``
    is the surviving tail. Pairs are kept so the new leading message is a user
    message (matching the sync trim's pair-aware walk and the post-trim
    sanitization).
    """
    if len(messages) <= MIN_MESSAGES_AFTER_TRIM:
        return [], 0
    chars = _estimate_chars(messages)
    if chars <= target:
        return [], 0
    # Find the surviving split, mirroring trim_messages' pair-aware pop.
    i = 0
    while chars > target and len(messages) - i > MIN_MESSAGES_AFTER_TRIM:
        chars -= _message_char_count(messages[i])
        i += 1
        # Pop trailing assistant messages so the new leading message is a user.
        while (
            len(messages) - i > MIN_MESSAGES_AFTER_TRIM
            and messages[i].get("role") not in ("system", "user")
        ):
            chars -= _message_char_count(messages[i])
            i += 1
    # Keep a recent window un-summarized: only summarize messages[1:i] (skip the
    # system prompt at 0). The actual drop still happens in trim_messages.
    system_end = 1 if messages and messages[0].get("role") == "system" else 0
    if i <= system_end + keep_recent:
        return [], i
    dropped = messages[system_end:i]
    return dropped, i


async def trim_messages_async(
    messages: list[dict[str, Any]],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    keep_rate: float = DEFAULT_KEEP_RATE,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    max_tag_len: int = DEFAULT_MAX_TAG_LEN,
    summary_keep_recent: int = DEFAULT_KEEP_RECENT,
    summarizer: Optional[Callable[[str], Awaitable[str]]] = None,
) -> list[dict[str, Any]]:
    """Like :func:`trim_messages` but condenses the dropped prefix first.

    When a ``summarizer`` is supplied and the trim would drop at least
    ``_SUMMARY_MIN_DROPPED`` messages, the to-be-dropped chunk is summarized
    into one user-role ``<history_summary>`` block and inserted after the system
    prompt. The sync trim then runs unchanged over the result, so the existing
    char budget / pair-aware / sanitize logic is the single source of truth.

    A summarizer failure (or a None summarizer) falls through to plain drop —
    the GA trim is the floor; summarization is only an upgrade, never a
    correctness path. Returns the (mutated in place) ``messages``.
    """
    if summarizer is None:
        trim_messages(messages, context_window=context_window, keep_rate=keep_rate,
                      keep_recent=keep_recent, max_tag_len=max_tag_len)
        return messages

    cap = context_window * TRIGGER_MULTIPLIER
    target = int(cap * keep_rate)
    if _estimate_chars(messages) <= cap:
        return messages  # nothing to trim → no need to summarize

    dropped, split = _plan_dropped_prefix(
        messages, target=target, keep_recent=summary_keep_recent,
    )
    system_end = 1 if messages and messages[0].get("role") == "system" else 0
    # Only spend an LLM call when the dropped stretch carries real new material.
    # If it is (almost) entirely a summary we already produced, re-summarizing
    # would just burn tokens on the same text — skip straight to the plain trim.
    fresh = [m for m in dropped if not _is_summary_message(m)]
    if len(fresh) >= _SUMMARY_MIN_DROPPED and split > system_end:
        dropped_capped = dropped[:_SUMMARY_BATCH_LIMIT]
        summary = await _summarize_chunk(summarizer, dropped_capped)
        if summary:
            # Compaction, not annotation: REPLACE the to-be-dropped slice with the
            # summary, so the list shrinks (never grows) and the summary sits where
            # its source did. The sync trim below then only pops if still over
            # budget — and the summary (now the front data message) is the last
            # thing it would reach.
            messages[system_end:split] = [_summary_message(summary)]
            logger.debug(
                "[context] compacted %d dropped messages into a %d-char summary",
                len(dropped_capped), len(summary),
            )

    # The existing sync trim is the floor: it re-evaluates the char budget against
    # the compacted list, drops what still exceeds target, and sanitizes the
    # leading message. If summarization was skipped or failed, this is the plain
    # GA trim — summarization is an upgrade, never a correctness path.
    trim_messages(messages, context_window=context_window, keep_rate=keep_rate,
                   keep_recent=keep_recent, max_tag_len=max_tag_len)
    return messages


def serialize_tool_result(value: Any) -> str:
    """Serialize one tool result deterministically for an OpenAI tool message.

    Plain strings pass through unquoted; everything else becomes stable JSON so
    the same result never produces two different payloads across turns. This is
    the canonical serializer shared by the live loop (agent_loop) and the
    history-rebuild path below so a replayed turn and a live turn serialize
    identically.
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def utf16_len(s: str) -> int:
    """UTF-16 code-unit length, matching JS ``String.length``.

    History replay slices ``content`` by the ``content_offset`` we stamp on each
    tool_call (the length of already-collected content the moment the tool_call
    event fired). The slice offsets must use the same code-unit metric as the
    frontend's ``String.prototype.slice``; Python's ``len`` counts code points
    and would drift on astral characters (emoji etc.), mis-slicing the text.
    """
    return len(s.encode("utf-16-le")) // 2


def attach_tool_result(
    tool_calls: list[dict],
    tool_results: list[Any],
    *,
    call_id: Any,
    index: Any,
    result: Any,
) -> None:
    """Match a tool_result event to its tool_call and mark it done.

    ``agent_loop`` emits ``index`` as *this turn's* nth tool_call, resetting to 0
    each turn, while ``tool_calls`` accumulates across turns — so indexing into
    it by ``index`` mis-pairs a later turn's result onto an earlier turn's call
    (re-marking it done) and leaves the actually-running call stuck on
    ``running``. That stuck status is what surfaces as a permanently-spinning
    tool card in history replay.

    Match by ``call_id`` (the ``tool_call_id`` ``agent_loop`` guarantees is
    identical on the ``tool_call`` and ``tool_result`` events) instead, falling
    back to the last still-running call when an id is missing. ``tool_results``
    is kept parallel to ``tool_calls`` by position because history replay pairs
    them by index.
    """
    call_idx = -1
    call: dict | None = None
    if call_id is not None:
        for i, c in enumerate(tool_calls):
            if c.get("id") == call_id:
                call_idx, call = i, c
                break
    if call is None and isinstance(index, int):
        # id missing: prefer a still-running call at the same per-turn index,
        # else any still-running call. Never re-mark an already-done call — a
        # per-turn index collides across turns, and reusing a done call leaves
        # the real running call stuck (the permanent-spinner bug).
        running = [(i, c) for i, c in enumerate(tool_calls) if c.get("status") == "running"]
        if running:
            same_idx = [(i, c) for i, c in running if i == index]
            call_idx, call = same_idx[0] if same_idx else running[-1]
    if call is None:
        # No matching call at all (id absent and nothing running, or a stray
        # result after a reset): keep the result for display without a paired
        # call, appended in order — never overwrite a done call's result.
        tool_results.append(result)
        return
    while len(tool_results) <= call_idx:
        tool_results.append(None)
    tool_results[call_idx] = result
    call["status"] = "done"


# ── Agent → 用户 文件/图片展示（outbound media parts）──────────────────────
# Agent 通过 ask_user(attachments) 把工作区文件 / 公网 URL / base64 二进制交给
# 用户展示；二进制经 attachment_store 落成 attachment。这里把工具/问题事件里的
# 附件形状结果抽成消息 media part 并认领归属——形状驱动，不认工具名。
# chat 路径（agent/api.py）与定时任务路径（agent/scheduler.py）共用，避免两边
# 各抄一份日后漂移。

async def _media_part_from_result(
    data: Any, sink: list[dict[str, Any]], caller: str | None, conv_id: str,
) -> None:
    """一个「附件形状」的结果 dict → media part + 认领归属。

    形状驱动，不认工具名：任何工具的返回只要带 ``kind=url|attachment``，就抽成
    media part。这样 show_file / ask_user / 未来任何产出媒体的工具共用一套逻辑，
    不必每加一个工具就在这里加一个 if 分支。

    - ``url``：公网 URL，直接透传成 media part（带 url，无 attachment_id）。
    - ``attachment``：经 attachment_store 登记的，带 attachment_id；同时把未认领
      的附件认领给当前会话用户（caller），context_ref=conv_id。仅 owner=NULL 时
      认领，防越权抢占。

    media part 统一 ``type:"attachment"``，客户端按 mime_type 决定内联图片
    还是下载卡。

    认领只在**首次**（该附件尚未进 sink）发生时执行：同一附件常被两条路径喂进来
    （ask_user 的 tool_result 与其后的 question 事件携带同一份 media），若在去重前
    认领，就会对同一附件重复调 claim。claim 本身幂等（owner 已设即空转），但每次
    调用都是一趟数据库写 —— 去重后再认领才是正确口径。
    """
    if not isinstance(data, dict):
        return
    kind = data.get("kind")
    mime = str(data.get("mime_type") or "")
    if kind == "url":
        url = data.get("url")
        if not url:
            return
        part = {
            "type": "attachment",
            "url": url,
            "name": data.get("name"),
            "mime_type": mime,
        }
    elif kind == "attachment":
        attachment_id = data.get("id") or data.get("attachment_id")
        if attachment_id is None:
            return
        part = {
            "type": "attachment",
            "attachment_id": attachment_id,
            "name": data.get("name"),
            "mime_type": mime,
            "size": data.get("size"),
            "status": data.get("status"),
            "expires_at": data.get("expires_at"),
        }
    else:
        return
    # 去重：同一 attachment_id / url 只保留一张卡（流里重连回放可能重复到达）。
    key = part.get("attachment_id") or part.get("url")
    if any((p.get("attachment_id") or p.get("url")) == key for p in sink):
        return
    sink.append(part)
    # 去重后再认领：仅首次见到该附件时才认领（避免 ask_user 双路径重复调用）。
    if kind == "attachment" and caller:
        try:
            import attachment_store
            await attachment_store.claim_for_owner(int(part["attachment_id"]), caller, context_ref=conv_id)
        except Exception:  # noqa: BLE001 — 认领失败不阻断对话
            logger.warning(
                "[attachment] claim failed id=%s caller=%s", part.get("attachment_id"), caller, exc_info=True,
            )


async def _collect_attachment_media(
    event: dict, sink: list[dict[str, Any]], caller: str | None, conv_id: str,
) -> None:
    """工具结果事件 → media part(s)。

    两种载荷形状都收，与工具名无关：

    - 结果自身就是附件（``kind=url|attachment``）—— 旧 show_file 的形状。
    - 结果带 ``media`` 列表（每项一个附件形状）—— ask_user 的形状。
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return
    await _media_part_from_result(data, sink, caller, conv_id)
    media = data.get("media")
    if isinstance(media, list):
        for item in media:
            await _media_part_from_result(item, sink, caller, conv_id)


def rebuild_history_messages(
    history: list[dict],
    *,
    skip_msg_id: Any = None,
    system_prompt: str = "",
) -> list[dict]:
    """Rebuild stored conversation rows into an OpenAI-shaped LLM message list.

    Stored assistant tool_calls carry the display shape ``{id, name, args, status}``
    plus a sibling ``tool_results`` list indexed in parallel. The live loop
    (agent_loop) emits wire shape ``{id, type, function:{name, arguments}}`` with
    one paired ``role=tool`` message per executed call — this function does the
    same for replayed history so resumed conversations stay valid for upstreams
    that enforce tool-call correlation (Anthropic, Responses, strict OpenAI).

    Rules:
    - Only calls that completed (``status == "done"`` or absent) **and** have a
      matching result are replayed. An orphaned ``tool_call`` without a
      ``role=tool`` reply is dropped — some upstreams 400 on it.
    - ``status == "running"`` entries (mid-batch persists left after a crash or
      approval hang) are dropped for the same reason: they never finished.
    - The legacy non-standard ``tool_results`` sibling field is consumed here to
      build the paired ``role=tool`` messages and is never sent upstream.
    - An assistant entry that ends up with empty content and no replayed
      tool_calls is dropped entirely so we don't send a blank turn upstream.
    - **手动压缩持久化**：最后一条 ``<history_summary>`` 标记消息之前的历史整段
      丢弃，只保留该摘要（它本身就是被压缩前缀的幸存记录）。这样「压缩上下文」
      跨轮生效——否则每轮都从 DB 全量重建，手动压缩下一轮就白做。摘要由
      ``compact_conversation`` 写入；标记机制复用 ``_is_summary_message``。
    """
    # 压缩 cutoff：找最后一条 summary 标记消息，丢弃它之前的所有行（摘要即幸存记录）。
    # 显示层读 get_messages 不受影响（用户仍看到完整历史），只有喂给 LLM 的上下文被裁。
    cutoff = 0
    for idx, msg in enumerate(history):
        if _is_summary_message(msg):
            cutoff = idx
    effective_history = history[cutoff:]

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in effective_history:
        if skip_msg_id is not None and msg.get("id") == skip_msg_id:
            continue
        role = msg.get("role") or "user"
        content = msg.get("content", "") or ""
        entry: dict[str, Any] = {"role": role, "content": content}
        messages.append(entry)
        if role != "assistant":
            continue
        calls = msg.get("tool_calls") or []
        results = msg.get("tool_results") or []
        wire_calls: list[dict] = []
        tool_messages: list[dict] = []
        for idx, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            # Skip calls that never finished or have no result to pair with.
            if call.get("status") not in (None, "done"):
                continue
            data = results[idx] if idx < len(results) else None
            if data is None:
                continue
            func = call.get("function") if isinstance(call.get("function"), dict) else {}
            call_id = str(call.get("id") or func.get("id") or f"hist-{msg.get('id')}-{idx}")
            name = call.get("name") or func.get("name") or ""
            args = call.get("args")
            if args is None:
                args = func.get("arguments")
            if isinstance(args, str):
                arguments = args or "{}"
            else:
                arguments = json.dumps(args if args is not None else {}, ensure_ascii=False)
            wire_calls.append({
                "id": call_id,
                "type": "function",
                "function": {"name": str(name), "arguments": arguments},
            })
            tool_messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": str(name),
                "content": serialize_tool_result(data),
            })
        if wire_calls:
            entry["tool_calls"] = wire_calls
            messages.extend(tool_messages)
        # Drop a now-empty assistant turn (no content, no replayed tool_calls).
        if not entry.get("content") and not entry.get("tool_calls"):
            messages.pop()
    return messages


async def summarize_history_for_compact(
    history: list[dict],
    summarizer: Callable[[str], Awaitable[str]],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> str | None:
    """把会话历史（截至最近 keep_recent 条之前）压成一条摘要文本。

    手动压缩（/compact 或压缩按钮）用：对**当前有效历史**（已跳过更早的 summary
    标记）做一次 LLM 摘要，返回摘要文本；没有可压缩的前缀 / 摘要失败返回 None。
    与 ``trim_messages_async`` 的内部摘要口径一致（同一 transcript 拼接 + 同一
     summarizer 回调），只是不由字符预算触发、而是无条件对前缀做一次。
    """
    # 只压缩到最近 keep_recent 条之前：尾部窗口保留原文，摘要只覆盖前缀。
    effective = history
    cutoff = 0
    for idx, msg in enumerate(history):
        if _is_summary_message(msg):
            cutoff = idx
    effective = history[cutoff:]
    if len(effective) <= keep_recent:
        return None
    chunk = effective[: len(effective) - keep_recent]
    # 丢掉 system 前缀（压缩产物是历史摘要，不含 system 人设）。
    chunk = [m for m in chunk if m.get("role") != "system"]
    if not chunk:
        return None
    return await _summarize_chunk(summarizer, chunk)


def make_summary_message(summary: str) -> dict[str, Any]:
    """把摘要文本包成可落库的标记消息（role=user + <history_summary>）。

    落库后 ``rebuild_history_messages`` 会把它当压缩 cutoff：它之前的历史全丢，
    摘要本身是唯一幸存记录。显示层仍能读到这条（内容可读、带标记前缀）。
    """
    return _summary_message(summary)
