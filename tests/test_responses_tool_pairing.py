"""Responses→OpenAI 转换的 tool 配对完整性（复现上游 400 code=11148）。

上游 CodeBuddy/WorkBuddy 对出站 body 做工具配对校验：assistant.tool_calls 的每个
id 都必须有紧跟其后的 role=tool 结果，反之 role=tool 也必须有前置 tool_call。任一
侧残缺上游即回 400 code=11148（tool_call_sequence_broken），且该坏历史会被每次请求
原样重放——整条会话报废，客户端看到的提示是「start a new conversation」。

本模块锁三件事：
1. 并行 function_call 合并进同一条 assistant（拆成多条会切断配对）；
2. reasoning item 的文本挂到 assistant.reasoning_content，不单独成 user 消息；
3. 无正文的 item 不下发（旧逻辑把 reasoning 等原生 item 塞成空 user 消息）。
"""
from message_utils import (
    cleanup_orphan_tool_calls,
    repack_tool_result_blocks,
    responses_to_openai_messages,
)


def _pairing_breaks(messages: list[dict]) -> list[tuple]:
    """返回配对断裂处 (assistant 下标, 期望的 id 集合, 实际拿到的 id 集合)。"""
    breaks = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            want = {tc["id"] for tc in msg["tool_calls"]}
            j = i + 1
            got = set()
            while j < len(messages) and messages[j].get("role") == "tool":
                got.add(messages[j].get("tool_call_id"))
                j += 1
            if got != want:
                breaks.append((i, want, got))
            i = j
        else:
            i += 1
    return breaks


def test_parallel_function_calls_merge_into_one_assistant():
    """Codex 并行工具调用：连续 function_call 必须合并成一条 assistant.tool_calls。

    旧行为是逐条拆成独立 assistant 消息，前一条的 tool_call 后面跟的是下一条
    assistant 而非结果，上游判 11148。
    """
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run"}]},
            {"type": "function_call", "call_id": "c0", "name": "exec", "arguments": "{}"},
            {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "exec", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c0", "output": "o0"},
            {"type": "function_call_output", "call_id": "c1", "output": "o1"},
            {"type": "function_call_output", "call_id": "c2", "output": "o2"},
        ],
    })

    assistants = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistants) == 1, f"并行调用应合并成 1 条 assistant，实际 {len(assistants)}"
    assert [tc["id"] for tc in assistants[0]["tool_calls"]] == ["c0", "c1", "c2"]
    assert _pairing_breaks(messages) == []


def test_reasoning_between_call_and_output_keeps_pairing():
    """真实 Codex 载荷的主力形态：reasoning 夹在 function_call 与其 output 之间。

    reasoning 文本挂到 assistant.reasoning_content，不能变成 user 消息——否则既切断
    配对，又把模型自己的推理当用户输入回灌。
    """
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
            {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "I should run ls"}]},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ],
    })

    assert _pairing_breaks(messages) == []
    # reasoning 不得成为独立 user 消息
    users = [m for m in messages if m.get("role") == "user"]
    assert len(users) == 1 and users[0]["content"] == "go"
    # 文本归位到承载该 tool_call 的 assistant
    assistant = next(m for m in messages if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == "I should run ls"
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["c1"]


def test_reasoning_summary_only_falls_back_to_summary_text():
    """reasoning 只有 summary、content 为 null 时取 summary 文本（与反方向语义对称），
    并与同一轮次的 assistant 正文合并成一条消息。"""
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}], "content": None},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
        ],
    })

    assert [m.get("role") for m in messages] == ["user", "assistant"]
    assert messages[1]["reasoning_content"] == "thinking"
    assert messages[1]["content"] == "ok"


def test_items_without_text_do_not_become_empty_user_messages():
    """无正文的 item 不下发，避免 compaction 把十几条消息撑成上百条空 user。"""
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "summary": [], "content": None},
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
        ],
    })

    assert all(not (m.get("role") == "user" and not m.get("content")) for m in messages)
    assert [m.get("role") for m in messages] == ["user", "assistant"]


def test_orphan_function_call_output_still_pairs_with_preceding_call():
    """回归：output 缺 call_id 时回退到最早未配对的调用（原 pending 队列语义）。"""
    messages, _ = responses_to_openai_messages({
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
            {"type": "function_call_output", "output": "ok"},
        ]
    })

    assistant = next(m for m in messages if m.get("role") == "assistant")
    tool = next(m for m in messages if m.get("role") == "tool")
    assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert tool.get("name") == "lookup"


def test_reasoning_flushes_before_following_user_message():
    """reasoning 后跟新的 user 消息时，reasoning 归入前一条 assistant，不污染 user。"""
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
            {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "why"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next"}]},
        ],
    })

    assert messages[0]["role"] == "assistant"
    assert messages[0]["reasoning_content"] == "why"
    assert [tc["id"] for tc in messages[0]["tool_calls"]] == ["c1"]
    assert messages[1] == {"role": "user", "content": "next"}


def test_assistant_text_between_call_and_output_merges_into_same_turn():
    """真实 Codex 载荷形态：function_call → reasoning → assistant 正文 → output。

    assistant 正文必须并入承载该 tool_call 的同一条消息；单独建消息等于在
    tool_calls 与其结果之间插一条 assistant，重新制造 11148。
    """
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
            {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "thinking"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "running it"}]},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ],
    })

    assert _pairing_breaks(messages) == []
    assert [m.get("role") for m in messages] == ["user", "assistant", "tool"]
    assistant = messages[1]
    assert assistant["content"] == "running it"
    assert assistant["reasoning_content"] == "thinking"
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["c1"]


def test_multimodal_message_without_text_is_kept():
    """纯图片等多模态 message 提取不到文本，仍须保留（不能当空 item 丢弃）。"""
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "http://x/y.png"}]},
        ],
    })

    assert [m.get("role") for m in messages] == ["user"]


def test_developer_notice_between_calls_and_outputs_stays_contiguous():
    """真实 Codex 载荷形态：并行 function_call 之后、output 之前夹 developer 通知。

    实测 rollout 中 Codex 会把「Approved command prefix saved」developer 消息插在
    function_call 与 function_call_output 之间。若按原顺序直出，assistant.tool_calls
    后面跟的是 developer 而不是结果，上游判 11148。通知须移到整组结果之后。
    """
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "function_call", "call_id": "c0", "name": "shell_command", "arguments": "{}"},
            {"type": "function_call", "call_id": "c1", "name": "shell_command", "arguments": "{}"},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Approved command prefix saved"}]},
            {"type": "function_call_output", "call_id": "c0", "output": "o0"},
            {"type": "function_call_output", "call_id": "c1", "output": "o1"},
        ],
    })

    assert _pairing_breaks(messages) == []
    assert [m.get("role") for m in messages] == ["assistant", "tool", "tool", "system"]
    assert messages[0]["tool_calls"][0]["id"] == "c0"
    assert messages[3]["content"] == "Approved command prefix saved"


def test_parallel_calls_with_interleaved_reasoning_stay_contiguous():
    """并行调用 + reasoning 交错：合并成一条 assistant，结果紧随其后。"""
    messages, _ = responses_to_openai_messages({
        "model": "m",
        "input": [
            {"type": "function_call", "call_id": "c0", "name": "exec", "arguments": "{}"},
            {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "r0"}]},
            {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}"},
            {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "r1"}]},
            {"type": "function_call_output", "call_id": "c0", "output": "o0"},
            {"type": "function_call_output", "call_id": "c1", "output": "o1"},
        ],
    })

    assert _pairing_breaks(messages) == []
    assistants = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistants) == 1
    assert [tc["id"] for tc in assistants[0]["tool_calls"]] == ["c0", "c1"]
    assert assistants[0]["reasoning_content"] == "r0\nr1"


# ==================== 出站安全网（移植自 workbuddy2api tool_pairing.go） ====================


def test_repack_moves_interleaved_message_after_all_results():
    """插在两份并行结果中间的消息（Codex image_resize_notice 形态）必须挪到整组之后。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c0", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
        {"role": "developer", "content": "<image_resize_notice/>"},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    ]

    out = repack_tool_result_blocks(messages)

    assert [m.get("role") for m in out] == ["assistant", "tool", "tool", "developer"]
    assert [m.get("tool_call_id") for m in out if m.get("role") == "tool"] == ["c0", "c1"]
    assert _pairing_breaks(out) == []


def test_repack_is_noop_without_interleaved_messages():
    """无插入消息时零改动，返回原列表对象。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c0", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
    ]

    assert repack_tool_result_blocks(messages) is messages


def test_repack_does_not_swallow_next_assistant_group_header():
    """下一组 assistant.tool_calls 是新的组头，不能被当成插入物吞掉。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c0", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c0", "content": "r0"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    ]

    out = repack_tool_result_blocks(messages)

    assert [m.get("role") for m in out] == ["assistant", "tool", "assistant", "tool"]
    assert _pairing_breaks(out) == []


def test_cleanup_drops_tool_call_without_result():
    """工具执行失败留下的孤儿 tool_call（无结果）被剔除。

    与参考实现一致：assistant 消息本身保留（只删 tool_calls 键），删掉的是无法配对的
    调用，宁可丢一轮工具上下文也不让整条会话报废。
    """
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c9", "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "continue"},
    ]

    out = cleanup_orphan_tool_calls(messages)

    assert all(not m.get("tool_calls") for m in out)
    assert [m.get("role") for m in out] == ["user", "assistant", "user"]
    assert _pairing_breaks(out) == []


def test_cleanup_drops_orphan_tool_result():
    """无对应 tool_call 的孤儿 tool 结果整条删除。"""
    messages = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},
    ]

    out = cleanup_orphan_tool_calls(messages)

    assert [m.get("role") for m in out] == ["user"]


def test_cleanup_trims_partial_batch_symmetrically():
    """批 [c1,c2] 只回了 c1：两侧按同一份 keepCalls 对称裁剪，不留半截配对。

    非对称裁剪（整批删调用但保留 tool{c1}）会产出「无 tool_calls 的 assistant +
    孤儿 tool」，正是上游判 11148 的形态。
    """
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    ]

    out = cleanup_orphan_tool_calls(messages)

    assistant = out[0]
    assert [tc["id"] for tc in assistant["tool_calls"]] == ["c1"]
    assert [m["tool_call_id"] for m in out if m.get("role") == "tool"] == ["c1"]
    assert _pairing_breaks(out) == []


def test_cleanup_is_noop_without_tool_traffic():
    """无工具流量时原列表原样返回（零分配）。"""
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    assert cleanup_orphan_tool_calls(messages) is messages


def test_cleanup_preserves_valid_pairing():
    """合法配对整段保留，绝不吞掉正确内容。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    ]

    assert cleanup_orphan_tool_calls(messages) is messages


# ── 2026-10-16 对齐上游 68c08d13：通知可前置于首份结果，但绝不跨越轮边界 ──


def test_repack_moves_notice_before_first_result():
    """通知插在 assistant 与首份结果之间：结果必须前移紧贴 assistant（旧版漏修）。"""
    notice = {"role": "developer", "content": "URI guard"}
    result = {"role": "tool", "tool_call_id": "c1", "content": "ok"}
    nxt = {"role": "user", "content": "next"}
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        notice, result, nxt,
    ]

    out = repack_tool_result_blocks(messages)

    assert out is not messages
    assert [m.get("role") for m in out] == ["assistant", "tool", "developer", "user"]
    assert out[1] is result and out[2] is notice and out[3] is nxt
    assert _pairing_breaks(out) == []


def test_repack_keeps_notices_within_block_and_respects_turn_boundary():
    """并行结果被通知拆开：通知整组后移到结果之后；user 之后的结果不被前移。"""
    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "f", "arguments": "{}"}},
    ]}
    dev = {"role": "developer", "content": "dev"}
    sysm = {"role": "system", "content": "system"}
    r1 = {"role": "tool", "tool_call_id": "c1", "content": "one"}
    r2 = {"role": "tool", "tool_call_id": "c2", "content": "two"}
    user = {"role": "user", "content": "boundary"}

    out = repack_tool_result_blocks([call, dev, r1, sysm, r2, user])
    assert out is not user  # 非退化断言：确保拿到的是新列表
    assert [m.get("role") for m in out] == ["assistant", "tool", "tool", "developer", "system", "user"]
    assert [m.get("tool_call_id") for m in out if m.get("role") == "tool"] == ["c1", "c2"]
    assert _pairing_breaks(out) == []

    # user 是轮边界：通知虽在，但 c1 结果已被 user 隔开——整段零改动，交给 cleanup 兜底
    boundary = [call, dev, user, r1]
    assert repack_tool_result_blocks(boundary) is boundary


def test_repack_trailing_notice_is_noop():
    """通知已在整组末尾：无需改动，返回原列表对象。"""
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "developer", "content": "notice"},
    ]

    assert repack_tool_result_blocks(messages) is messages
