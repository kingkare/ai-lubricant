"""压缩（上下文压缩）请求识别。

长任务编码 agent 会周期性地把整段历史总结成一条摘要，好把上下文压回窗口内——这就是
「压缩请求」。它在 wire 上和普通对话请求**完全同形**：同一个 key、同一个模型、同一个端点，
唯一的区别是客户端注入的那段总结指令文本。本模块负责认出这些指令，让网关能把这类请求路由到
一个专门的压缩模型（更便宜/更快），而不是占用主模型。

两级信号，按可信度排序：

1. **显式标记**——第一方调用方（我们自己的 agent）在 body 里带保留键
   ``_purpose = "compaction"``。零误判；命中后从 body 剥离，绝不下发上游。
2. **文本指纹**——第三方客户端（Claude Code / Codex / OpenCode / Cline·CodeBuddy 家族 /
   atomcode / dsh）都内嵌一段固定的总结指令。逐字锚点命中即判定。可靠但**版本脆弱**
   （客户端一改文案就静默失配），所以只作 best-effort：命中就**加**一个压缩标记，
   永不据此拒绝请求。

识别只扫「指令可能落点」的少量文本（system/developer 轮、末尾 user 轮、Anthropic 顶层
system、Responses 的 instructions / input 尾部），与历史长度无关，恒为 O(1)。
"""

from __future__ import annotations

from typing import Any, Optional

# 第一方显式标记：body 保留键。第三方不会带它，只带文本，走指纹。
EXPLICIT_BODY_KEY = "_purpose"
EXPLICIT_VALUE = "compaction"

# 逐字锚点（小写匹配）。每条 = (客户端标签, 锚点子串元组)。
# 锚点取自本机实装的客户端产物，逐字抠出，非猜测：
#   - Claude Code：把总结指令追加为「最后一条 user」，有 3 个变体（so far / this / RECENT portion）。
#   - Codex：core/src/tasks/compact.rs 的 "CONTEXT CHECKPOINT COMPACTION"。
#   - OpenCode：独立 system prompt "You are a context summarization agent."
#   - Cline 家族（Cline / Roo / CodeBuddy / WorkBuddy）：<conversation-to-summarize> 块。
#   - atomcode：system "You are an anchored context summarization assistant."
#   - dsh：末尾 user "You are now acting as a compaction engine."
_FINGERPRINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("agent", (
        # 本项目 agent 的摘要指令（agent_loop._SUMMARY_SYSTEM 首句）。第一方、文本稳定，
        # 加了它内置 agent 的压缩也自动走压缩模型，零 agent 侧改动。
        "you compress an agent's earlier conversation into a dense factual note",
    )),
    ("claude-code", (
        "your task is to create a detailed summary of the conversation",
        "your task is to create a detailed summary of this conversation",
        "your task is to create a detailed summary of the recent portion",
    )),
    ("codex", (
        "you are performing a context checkpoint compaction",
    )),
    ("opencode", (
        "you are a context summarization agent",
    )),
    ("cline", (
        "block below contains a past conversation that you must summarize",
        "<conversation-to-summarize>",
    )),
    ("atomcode", (
        "you are an anchored context summarization assistant",
    )),
    ("dsh", (
        "you are now acting as a compaction engine",
    )),
)


def _text_of(content: Any) -> str:
    """把一条消息/块的 content 抽成纯文本（str / content-part 列表 / tool_result 都吃）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "content"):
                    val = block.get(key)
                    if isinstance(val, str):
                        parts.append(val)
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "content"):
            val = content.get(key)
            if isinstance(val, str):
                return val
    return ""


def _candidate_texts(body: dict) -> list[str]:
    """收集「总结指令可能落点」的少量文本，避免扫全量历史。"""
    if not isinstance(body, dict):
        return []
    texts: list[str] = []

    # Anthropic 顶层 system（不在 messages 里）。
    top_system = body.get("system")
    if isinstance(top_system, str) and top_system:
        texts.append(top_system)

    # Responses 协议的身份/指令在 instructions。
    instructions = body.get("instructions")
    if instructions is not None:
        t = _text_of(instructions)
        if t:
            texts.append(t)

    messages = body.get("messages")
    if isinstance(messages, list):
        last_user = ""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role in ("system", "developer"):
                texts.append(_text_of(msg.get("content")))
            elif role == "user":
                # 只记最后一条 user（压缩指令若在此，必是末条）。
                last_user = _text_of(msg.get("content"))
        if last_user:
            texts.append(last_user)

    # Responses input：字符串或 item 列表（压缩指令在尾部）。
    inp = body.get("input")
    if isinstance(inp, str):
        texts.append(inp)
    elif isinstance(inp, list):
        for item in inp[-4:]:
            if isinstance(item, dict):
                texts.append(_text_of(item.get("content")))

    return [t for t in texts if t]


def detect_compaction(body: dict) -> Optional[dict]:
    """识别一次请求是否为「压缩」。命中返回 ``{"client","marker","source"}``，否则 None。

    - source="explicit"：第一方显式标记，最可信。
    - source="fingerprint"：第三方文本指纹，best-effort。
    """
    if not isinstance(body, dict):
        return None

    # 1) 显式标记优先。
    if str(body.get(EXPLICIT_BODY_KEY) or "").strip().lower() == EXPLICIT_VALUE:
        return {"client": "explicit", "marker": EXPLICIT_BODY_KEY, "source": "explicit"}

    # 2) 文本指纹。
    candidates = [t.lower() for t in _candidate_texts(body)]
    for label, needles in _FINGERPRINTS:
        for needle in needles:
            if any(needle in text for text in candidates):
                return {"client": label, "marker": needle, "source": "fingerprint"}
    return None


def is_compaction(body: dict) -> bool:
    """便捷布尔判定。"""
    return detect_compaction(body) is not None
