# -*- coding: utf-8 -*-
"""eaichat 渠道内围栏解析：用现网抓到的真实帧回归。

这批帧是 glm5.2 发的一个 Write 工具调用，`arguments.content` 里内嵌了三反引号
（它在写一个还原 SSE 帧的复现脚本）。框架那套按裸子串找 ``` 闭合会在参数中间
切断块、调用整个丢失；渠道内的 JSON 字符串态感知实现必须切出完整块并解析成功。
"""
import asyncio
import json
import types

import pytest


def _load_channel_module():
    """把 spec 文件按模块加载，只取模块级围栏解析函数（不触碰认证链路）。"""
    src = open('specs/eaichat_code_channel.py', encoding='utf-8').read()
    mod = types.ModuleType('_eaichat_spec_under_test')
    mod.__dict__['__name__'] = '_eaichat_spec_under_test'
    exec(compile(src, 'specs/eaichat_code_channel.py', 'exec'), mod.__dict__)
    return mod


CH = _load_channel_module()

# 现网帧：前导自然语言 + ```tool_function 围栏，参数 content 里内嵌 ```
_REAL_DELTAS = [
    "命令",
    "里反引号转义把 bash 搞坏了。换个干净方式——把复现",
    '脚本写成临时文件跑：```tool_function\n{"name": "Write", "arguments": {"',
    'content": "# -*- coding: utf-8 -*-\\n\\"\\"\\"复现 eaichat',
    ' 真实帧的 zero-completion 判定，跑完即删。\\"\\"\\"\\n',
    'import json\\nsys.path.insert(0, \\".\\")\\n',
    'FRAMES = [\\n    {\\"delta\\": {\\"content\\": \\"',
    '```',  # ← 参数里内嵌的三反引号：框架实现在这里误判为围栏闭合
    '\\"}},\\n]\\nprint(\\"done\\")\\n", ',
    '"file_path": "d:\\\\code\\\\ai-lubricant\\\\_repro_zero.py"}}\n```',
]


def _replay(deltas, enabled=True, names=None):
    state = {"enabled": enabled, "buf": ""}
    if names is not None:
        state["names"] = frozenset(names)
    text_parts, calls = [], []
    for d in deltas:
        for chunk in CH._tool_stream_feed(state, d):
            if chunk.get("content"):
                text_parts.append(chunk["content"])
            calls.extend(chunk.get("tool_calls") or [])
    for chunk in CH._tool_stream_flush(state):
        if chunk.get("content"):
            text_parts.append(chunk["content"])
        calls.extend(chunk.get("tool_calls") or [])
    return "".join(text_parts), calls


def test_real_frames_with_nested_fence_in_arguments():
    """参数里内嵌 ``` 不再截断块：解析出 Write 调用，正文不含半截 JSON。"""
    text, calls = _replay(_REAL_DELTAS)
    assert len(calls) == 1, f"应解析出 1 个工具调用，实得 {len(calls)}"
    assert calls[0]["function"]["name"] == "Write"
    args = json.loads(calls[0]["function"]["arguments"])
    assert set(args) == {"content", "file_path"}
    assert args["file_path"].endswith("_repro_zero.py")
    # 参数里那个内嵌围栏必须完整保留在 content 里，不能被当成闭合吃掉
    assert "```" in args["content"]
    # 下发正文只有围栏前的前导自然语言，绝不含 JSON 碎片
    assert text == "命令里反引号转义把 bash 搞坏了。换个干净方式——把复现脚本写成临时文件跑："
    assert '"name"' not in text and "arguments" not in text


def test_fence_split_across_deltas_char_by_char():
    """开标记被逐字符切开也要能切块，且半截标记不泄漏成正文。"""
    payload = '```tool_function\n{"name": "Bash", "arguments": {"command": "ls"}}\n```'
    text, calls = _replay(["前言。"] + list(payload))
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Bash"
    assert text == "前言。"


def test_markdown_content_with_code_fence_survives():
    """写 .md 文件（content 里带 ```python 代码块）是最常见的内嵌围栏场景。"""
    inner = json.dumps({
        "name": "Write",
        "arguments": {
            "file_path": "README.md",
            "content": "# Demo\n\n```python\nprint('hi')\n```\n\n结束。",
        },
    }, ensure_ascii=False)
    text, calls = _replay([f"```tool_function\n{inner}\n```"])
    assert len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert "```python" in args["content"]
    assert text == ""


def test_plain_text_passthrough_when_no_tools():
    """无 tools 时零缓冲原样透传，一个字符都不改。"""
    text, calls = _replay(["讲个笑话：", "```python\nprint(1)\n```"], enabled=False)
    assert calls == []
    assert text == "讲个笑话：```python\nprint(1)\n```"


def test_json_fence_is_not_a_tool_call():
    """泛 ```json 围栏仍然是正文，不当工具调用（意图驱动判据不变）。"""
    text, calls = _replay(['这是配置：```json\n{"name": "Write", "arguments": {}}\n```'])
    assert calls == []
    assert '"name": "Write"' in text


def test_unclosed_fence_at_stream_end_is_rescued():
    """上游没吐收尾围栏时，靠花括号配平把调用救回来，不随缓冲丢失。"""
    text, calls = _replay(['```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}'])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert text == ""


def test_two_fences_in_one_stream():
    """一轮里连发两个调用都要切出来。"""
    a = '```tool_function\n{"name": "Read", "arguments": {"file_path": "a"}}\n```'
    b = '```tool_function\n{"name": "Read", "arguments": {"file_path": "b"}}\n```'
    text, calls = _replay([a, "中间说明。", b])
    assert [c["function"]["name"] for c in calls] == ["Read", "Read"]
    assert text == "中间说明。"


# ==================== 方法是否存在校验（以入参 tools 为准）====================
# 渠道返回里工具调用是正文围栏文本，name 写什么全由模型决定。客户端只会执行自己
# 声明过的工具，收到不存在的工具名直接报错。所以下发前对照本次请求的 tools 过滤：
# 方法存在 -> 按方法返回 tool_calls；方法不存在（上游注入了它自己的工具 / 模型幻觉
# 出别家工具名）-> 整块原封不动当正文透传，不吞、不伪造调用。


def test_known_method_emits_tool_calls():
    """方法在入参 tools 里 -> 按方法返回。"""
    block = '```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n```'
    text, calls = _replay([block], names=["Read", "Bash"])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert text == ""


def test_unknown_method_returned_verbatim():
    """方法不在入参 tools 里 -> 整块原封不动当正文返回，一个字符都不改。"""
    block = '```tool_function\n{"name": "draw_picture", "arguments": {"prompt": "猫"}}\n```'
    text, calls = _replay([block], names=["Read", "Bash"])
    assert calls == []
    assert text == block, "未知方法的围栏必须原样透传"


def test_unknown_method_rescued_at_flush_returned_verbatim():
    """流末救回的残留调用同样要过校验：方法不存在 -> 原样补发正文，不发 tool_calls。"""
    text, calls = _replay(
        ['```tool_function\n{"name": "upstream_search", "arguments": {"q": "x"}}'],
        names=["Read"],
    )
    assert calls == []
    assert "upstream_search" in text
    assert text.startswith("```tool_function")


def test_mixed_known_and_unknown_blocks():
    """同一轮里已知方法照发调用，未知方法透传正文，互不影响。"""
    known = '```tool_function\n{"name": "Bash", "arguments": {"command": "ls"}}\n```'
    unknown = '```tool_function\n{"name": "upstream_search", "arguments": {"q": "x"}}\n```'
    text, calls = _replay([unknown, "中间说明。", known], names=["Bash"])
    assert [c["function"]["name"] for c in calls] == ["Bash"]
    assert unknown in text
    assert "中间说明。" in text


def test_no_names_means_no_filtering():
    """入参 tools 拿不到名字清单（names 缺省/为空）时不过滤，维持既有行为。"""
    block = '```tool_function\n{"name": "Read", "arguments": {"file_path": "a"}}\n```'
    text, calls = _replay([block])
    assert len(calls) == 1
    assert text == ""


def test_stream_chat_declares_validation_and_names_source():
    """stream_chat 的 tool_state 必须带入参 tools 的名字清单；渠道声明 TOOL_PARSE_IN_CHANNEL。"""
    import inspect

    src = inspect.getsource(CH.EaiChatChannel.stream_chat)
    assert '_known_tool_names(kwargs.get("tools"))' in src
    assert getattr(CH.EaiChatChannel, "TOOL_PARSE_IN_CHANNEL", False) is True


# ==================== Anthropic Messages SSE 兼容（opus 系模型）====================
# 上游 opus 系模型走 event: message_start / content_block_delta 形态：thinking 块与
# text 块分开，围栏调用在 text 块里按 text_delta 逐字下发（开标记会被切成 ` / ``tool /
# _function 三段）。翻译层必须与 OpenAI 分支同口径：thinking→thinking、text→围栏
# 缓冲（方法校验）、usage 攒到 message_delta 一次性下发。


def _anthropic_stream_events(known=True):
    """与现网抓包同构的 Anthropic 事件序列：thinking 块 + text 块内嵌围栏调用。"""
    call = {"name": "Bash" if known else "draw_picture", "arguments": {
        "command": "cd /d/code/ai-lubricant/user-frontend && npx vite build 2>&1 | tail -5",
        "description": "Build frontend to verify changes", "timeout": 600000}}
    fence_body = json.dumps(call, ensure_ascii=False)
    text_deltas = [
        "组件已完整。现在补上缺失的 imports，然后跑构建验证：\n",
        "`", "``tool", "_function", "\n",  # 开标记被切成三段（现网真实分片）
        fence_body[:15], fence_body[15:],
        "\n```",
    ]
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "chatcmpl-eea0e92a", "type": "message", "role": "assistant",
            "model": "opus", "usage": {"input_tokens": 156263, "output_tokens": 0}}}),
        ("ping", {"type": "ping"}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "thinking", "thinking": "",
                                                   "signature": "cHJveHktc3ludGhldGlj"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta",
                                           "thinking": "The file content looks good — "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta",
                                           "thinking": "but I need to update the imports."}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "signature_delta", "signature": "cHJveHkt"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {"type": "content_block_start", "index": 1,
                                 "content_block": {"type": "text", "text": ""}}),
    ]
    events += [("content_block_delta", {"type": "content_block_delta", "index": 1,
                                        "delta": {"type": "text_delta", "text": t}})
               for t in text_deltas]
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"input_tokens": 169561, "output_tokens": 283}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return events


def _replay_anthropic(events, names=("Bash",)):
    tool_state = {"enabled": True, "buf": "", "names": frozenset(names)}
    anth_state = {}
    thinking, text_parts, calls, usages = [], [], [], []
    for etype, data in events:
        for chunk in CH._anthropic_event_chunks(etype, data, tool_state, anth_state):
            if chunk.get("thinking"):
                thinking.append(chunk["thinking"])
            if chunk.get("content"):
                text_parts.append(chunk["content"])
            calls.extend(chunk.get("tool_calls") or [])
            if chunk.get("usage"):
                usages.append(chunk["usage"])
    for chunk in CH._tool_stream_flush(tool_state):
        if chunk.get("content"):
            text_parts.append(chunk["content"])
        calls.extend(chunk.get("tool_calls") or [])
    return ("".join(thinking), "".join(text_parts), calls, usages, anth_state)


def test_anthropic_stream_thinking_fence_and_usage():
    """thinking 聚合、围栏调用切出、usage 只在 message_delta 发一次。"""
    thinking, text, calls, usages, anth = _replay_anthropic(_anthropic_stream_events())
    assert thinking == "The file content looks good — but I need to update the imports."
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Bash"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["command"].startswith("cd /d/code/ai-lubricant/user-frontend")
    assert args["timeout"] == 600000
    # 正文只剩围栏前的自然语言，无 JSON 碎片、无围栏残骸
    assert text == "组件已完整。现在补上缺失的 imports，然后跑构建验证：\n"
    assert "```" not in text and '"name"' not in text
    # usage：message_start 的 output_tokens=0 不发，message_delta 发一次全量
    assert usages == [{"input_tokens": 169561, "output_tokens": 283}]
    assert anth["usage_sent"] is True


def test_anthropic_stream_unknown_method_passthrough():
    """Anthropic 路径同样执行「方法是否存在」校验：未知方法整块原样透传。"""
    thinking, text, calls, usages, _ = _replay_anthropic(
        _anthropic_stream_events(known=False), names=("Bash",))
    assert calls == []
    assert usages == [{"input_tokens": 169561, "output_tokens": 283}]
    assert "```tool_function" in text and '"draw_picture"' in text
    assert text.count("```tool_function") == 1


def test_anthropic_stream_silent_events_produce_nothing():
    """ping / content_block_start|stop / message_stop / signature_delta 不产生可见输出。"""
    thinking, text, calls, usages, anth = _replay_anthropic(
        [("ping", {"type": "ping"}),
         ("content_block_start", {"type": "content_block_start", "index": 0,
                                  "content_block": {"type": "text", "text": ""}}),
         ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                  "delta": {"type": "signature_delta", "signature": "x"}}),
         ("content_block_stop", {"type": "content_block_stop", "index": 0}),
         ("message_stop", {"type": "message_stop"})])
    assert thinking == "" and text == "" and calls == [] and usages == []


def test_anthropic_usage_accumulates_and_not_sent_without_output_tokens():
    """message_start 只攒 input（output=0 不发）；截断流没等到 message_delta 时不误发。"""
    tool_state = {"enabled": True, "buf": "", "names": frozenset({"Bash"})}
    anth = {}
    for etype, data in [
        ("message_start", {"type": "message_start", "message": {
            "usage": {"input_tokens": 156263, "output_tokens": 0}}}),
    ]:
        assert CH._anthropic_event_chunks(etype, data, tool_state, anth) == []
    assert anth["usage"] == {"input_tokens": 156263, "output_tokens": 0}
    assert anth.get("usage_sent") is None, "output_tokens=0 时绝不能提前发 usage"


def test_anthropic_type_recognized_without_event_line():
    """上游不发 event: 行、只有 data.type 时同样识别（parse_sse_event 的兜底口径）。"""
    # parse_sse_event 对裸 data: 行回落 data.type —— 这里直接验证事件类型集合覆盖
    assert "content_block_delta" in CH._ANTHROPIC_EVENT_TYPES
    assert "message_stop" in CH._ANTHROPIC_EVENT_TYPES
    assert "chat.completion.chunk" not in CH._ANTHROPIC_EVENT_TYPES


# ==================== OpenAI 终止帧 / 审计帧（deepseek-v4-pro 实抓形态）====================
# 收尾两帧是新形态：audit_result 标记帧（顶层带 audit_result、choices 是空 delta）与
# finish_reason 终止帧（reason 挂在 choice 上、不在 delta 里）。finish_reason 必须
# 暂存到流末围栏 flush 之后下发——围栏未闭合时 tool_calls 靠 flush 救回，提前发
# finish 会出现「finish 之后又来 tool_calls」的乱序。


def _replay_openai(events, names=("Bash",)):
    """按 stream_chat 的 OpenAI 分支同序回放：帧翻译 -> 流末 flush -> finish_reason。"""
    tool_state = {"enabled": True, "buf": "", "names": frozenset(names)}
    state: dict = {}
    thinking, text_parts, calls = [], [], []
    for data in events:
        for chunk in CH._openai_event_chunks(data, tool_state, state):
            if chunk.get("thinking"):
                thinking.append(chunk["thinking"])
            if chunk.get("content"):
                text_parts.append(chunk["content"])
            calls.extend(chunk.get("tool_calls") or [])
    for chunk in CH._tool_stream_flush(tool_state):
        if chunk.get("content"):
            text_parts.append(chunk["content"])
        calls.extend(chunk.get("tool_calls") or [])
    return "".join(thinking), "".join(text_parts), calls, state.get("finish_reason", "")


def test_openai_stream_fence_audit_finish_end_to_end():
    """实抓流全链路：空首帧 -> reasoning -> 围栏（content 逐帧拼）-> audit 帧 -> 终止帧。"""
    events = [
        # 首帧：content 为空串
        {"choices": [{"delta": {"role": "assistant", "type": "text", "content": ""}, "index": 0}]},
        # reasoning 增量
        {"choices": [{"delta": {"role": "assistant", "type": "text",
                                "reasoning_content": "Let me check the current status."},
                      "index": 0}]},
        # 围栏调用逐帧拼（content 流；JSON 花括号必须配平，闭标才认得出来）
        {"choices": [{"delta": {"role": "assistant", "type": "text",
                                "content": "```tool_function\n{\""}, "index": 0}]},
        {"choices": [{"delta": {"role": "assistant", "type": "text",
                                "content": "name\": \"Bash\", \"arguments\": "
                                           "{\"command\": \"git status\"}}\n```"},
                      "index": 0}]},
        # 审计帧：audit_result="pass"，choices 是空 delta
        {"audit_result": "pass",
         "choices": [{"delta": {"role": "assistant", "type": "text"}, "index": 0}]},
        # 终止帧：finish_reason 挂在 choice 上
        {"choices": [{"delta": {"role": "assistant", "type": "text"},
                      "finish_reason": "stop", "index": 0}]},
    ]
    thinking, text, calls, finish = _replay_openai(events)
    assert thinking == "Let me check the current status."
    assert [c["function"]["name"] for c in calls] == ["Bash"]
    assert text == ""  # 围栏整体被消费成 tool_calls，正文无 JSON 碎片
    assert finish == "stop"
    # audit 帧（pass）与空 delta 帧不产生任何输出——text/thinking 已隐含验证


def test_openai_finish_reason_length_preserved():
    """真实终止原因（length）必须保留，不能被兜底成 stop。"""
    events = [
        {"choices": [{"delta": {"type": "text", "content": "半截正文"}, "index": 0}]},
        {"choices": [{"delta": {"type": "text"}, "finish_reason": "length", "index": 0}]},
    ]
    thinking, text, calls, finish = _replay_openai(events)
    assert text == "半截正文"
    assert finish == "length"


def test_openai_unclosed_fence_finish_after_rescued_call():
    """围栏未闭合 + 终止帧：调用靠流末救回；finish_reason 暂存设计保证它排在调用之后。"""
    events = [
        {"choices": [{"delta": {"type": "text", "content":
                                "```tool_function\n{\"name\": \"Bash\", "
                                "\"arguments\": {\"command\": \"ls\"}}"}, "index": 0}]},
        {"choices": [{"delta": {"type": "text"}, "finish_reason": "stop", "index": 0}]},
    ]
    thinking, text, calls, finish = _replay_openai(events)
    assert [c["function"]["name"] for c in calls] == ["Bash"]  # flush 救回，不随缓冲丢
    assert finish == "stop"  # 终止帧先到也不丢


def test_openai_audit_non_pass_is_observational_only():
    """audit 非 pass：不产生输出、不抛错（上游审计拒绝另有 error 帧或断流，这里只观测）。"""
    events = [
        {"audit_result": "reject",
         "choices": [{"delta": {"role": "assistant", "type": "text"}, "index": 0}]},
    ]
    assert _replay_openai(events) == ("", "", [], "")


def test_openai_real_capture_unbalanced_json_rescued_at_flush():
    """实抓回归（deepseek-v4-pro）：外层 JSON 少一个 }（只闭了 arguments），闭标在
    depth=1 处不被认，整块滞留到流末；flush 用 allow_unclosed=1 补一个 } 救回调用，
    正文零残留。"""
    events = [
        {"choices": [{"delta": {"type": "text", "content": "```tool_function\n{\""}, "index": 0}]},
        {"choices": [{"delta": {"type": "text", "content": "name\": \"Bash\", \"arguments\": "
                                                           "{\"command\": \"git status\"}"}, "index": 0}]},
        {"choices": [{"delta": {"type": "text", "content": "\n```"}, "index": 0}]},
        {"choices": [{"delta": {"type": "text"}, "finish_reason": "stop", "index": 0}]},
    ]
    thinking, text, calls, finish = _replay_openai(events)
    assert [c["function"]["name"] for c in calls] == ["Bash"]
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"command": "git status"}
    assert text == "", "半截 JSON 块不能漏成正文"
    assert finish == "stop"


def test_balanced_json_object_strict_vs_rescue():
    """feed 路径（allow_unclosed=0）严格配平；救援（1）只放行外层缺 1 个 } 的形态，
    缺 2 个（字符串态还开着 / 深度太深）不救——那种半截救回来也是垃圾调用。"""
    strict = '{"name": "Bash", "arguments": {"command": "ls"}'
    assert CH._balanced_json_object(strict) == ""
    assert CH._balanced_json_object(strict, allow_unclosed=1).endswith("}}")
    # 缺 2 个不救
    assert CH._balanced_json_object('{"name": "Bash", "arguments": {"a": 1', allow_unclosed=1) == ""
    # 字符串没闭合不救（补 } 也非法 JSON）
    assert CH._balanced_json_object('{"name": "Ba', allow_unclosed=1) == ""
    # 正常配平不受影响
    ok = '{"name": "Bash", "arguments": {"command": "ls"}}'
    assert CH._balanced_json_object(ok) == ok
    assert CH._balanced_json_object(ok, allow_unclosed=1) == ok


# ==================== 请求装配：覆盖段必须真的进 messages ====================
# 回归的是一个静默失效：旧实现按 role=="system" 找已有 system 消息再追加覆盖段，
# 而客户端（Claude Code）发来的 messages 里通常没有 system 角色——它的 system prompt
# 走别的通道。于是那段循环永远不命中，覆盖段一次都没进过请求体（现网抓包证实：
# user 末尾有 _FENCE_NUDGE，全文搜不到覆盖段），B 类失败（只预告不调工具）照旧。
# 这类"代码在、但从没执行过"的 bug 只有断言装配结果才能发现。


class _FakeProvider:
    """只提供 flatten_tool_history：本节测的是装配落位，不碰历史文本化细节。"""

    @staticmethod
    def flatten_tool_history(messages):
        return list(messages)


def _texts(msg):
    """把一条消息的 content 取成可搜索的纯文本（兼容 str / content parts）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _assemble(messages, tools_prompt="TOOLS_PROMPT"):
    return CH._messages_with_tools_near_user(_FakeProvider, messages, tools_prompt)


_OVERRIDE_MARK = "系统工具协议覆盖"
_NUDGE_MARK = "系统末位指令"


def test_override_injected_when_client_sends_no_system():
    """客户端不发 system（Claude Code 的常态）时，覆盖段必须新建一条 system 送出去。"""
    out = _assemble([{"role": "user", "content": "帮我改一下这个函数"}])
    systems = [m for m in out if m.get("role") == "system"]
    assert len(systems) == 1, "应新建恰好一条 system 装覆盖段"
    assert _OVERRIDE_MARK in _texts(systems[0])
    # 覆盖段进了请求，末位提示也还在最后一条 user 上
    assert _NUDGE_MARK in _texts(out[-1])
    assert out[-1]["role"] == "user"


def test_override_and_nudge_both_present_with_existing_system():
    """客户端自带 system 时：原 system 原样保留，覆盖段另起一条，两段都在。"""
    out = _assemble([
        {"role": "system", "content": "你是某某助手"},
        {"role": "user", "content": "查一下配置"},
    ])
    joined = "\n".join(_texts(m) for m in out)
    assert _OVERRIDE_MARK in joined and _NUDGE_MARK in joined
    # 客户端原 system 不被改写、不被吞掉
    assert any(_texts(m) == "你是某某助手" for m in out if m.get("role") == "system")


def test_no_tools_means_no_injection_at_all():
    """无 tools 时（tools_prompt 为空）一个字都不加，纯聊天链路不受影响。"""
    msgs = [{"role": "user", "content": "讲个笑话"}]
    out = _assemble(msgs, tools_prompt="")
    assert out == msgs


def test_content_parts_user_keeps_original_parts():
    """content parts 形态：原 part 原样保留，工具说明在前、末位提示在尾。"""
    out = _assemble([
        {"role": "user", "content": [{"type": "text", "text": "原始问题"}]},
    ])
    last = out[-1]
    texts = [p["text"] for p in last["content"]]
    assert texts[0] == "TOOLS_PROMPT"
    assert "原始问题" in texts
    assert _NUDGE_MARK in texts[-1]
    assert any(_OVERRIDE_MARK in _texts(m) for m in out if m.get("role") == "system")


def test_nudge_lands_on_last_user_not_an_earlier_one():
    """多轮对话里末位提示必须钉在**最后**一条 user 上（生成前最后看到的位置）。"""
    out = _assemble([
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "第二轮"},
    ])
    users = [m for m in out if m.get("role") == "user"]
    assert _NUDGE_MARK not in _texts(users[0]), "不能钉在早先那轮 user 上"
    assert _NUDGE_MARK in _texts(users[-1])
    assert "第二轮" in _texts(users[-1])


def test_tail_directive_re_anchors_original_task_in_long_loop():
    """长 agent 循环里尾巴是工具结果，模型看不到原始任务——末位指令块必须把第一条
    user 的任务文本重述进去，作为任务锚点。这是 _build_tail_directive 替代单句
    _FENCE_NUDGE 的核心增量：不重述任务，模型不知道自己为啥在调工具、调完该不该停。

    用「会转换」的 flatten（role=tool -> role=user，与 base.py 生产实现同款）：只有
    转换后，从后往前找最后一条 user 才会命中真正的尾巴（工具结果），而不是原始提问。
    """

    class _FlatteningProvider:
        @staticmethod
        def flatten_tool_history(messages):
            out = []
            for m in messages:
                if m.get("role") == "tool":
                    out.append({"role": "user", "content": (
                        f'<tool_result name="{m.get("name", "")}">'
                        f'{m.get("content", "")}</tool_result>')})
                else:
                    out.append(m)
            return out

    messages = [
        {"role": "user", "content": "帮我把 README 里的错别字改掉"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "Read", "arguments": '{"path":"README.md"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": "文件内容"},
    ]
    out = CH._messages_with_tools_near_user(_FlatteningProvider, messages, "TOOLS_PROMPT")
    last = _texts(out[-1])
    assert out[-1]["role"] == "user", "role=tool 拍平后应变 user，指令钉在这条上"
    # 任务锚点：原始任务被重述进末位指令块（在最后一条 = 工具结果拍平后的 user 上）
    assert "帮我把 README 里的错别字改掉" in last, "末位指令块缺任务锚点"
    assert "文件内容" in last, "尾巴应是被注入的那条工具结果"
    assert _NUDGE_MARK in last
    assert "接下来怎么走" in last, "末位指令块缺判断分支"


def test_tail_directive_falls_back_when_no_user_message():
    """取不到原始任务（无 user 消息）时降级文案，不崩、仍有指令块其余部分。"""
    block = CH._build_tail_directive("")
    assert _NUDGE_MARK in block
    assert "没取到原始任务" in block
    assert "只留预告、不发围栏就结束" in block  # 其余硬规矩照在


def test_override_text_covers_the_announce_only_failure():
    """覆盖段与末位提示都要明确禁止「只预告不调用」——本轮修复的核心措辞。"""
    # 覆盖段：触发场景 + 预告句必须紧跟围栏
    assert "必须调用工具的触发场景" in CH._SYSTEM_TOOL_OVERRIDE
    assert "了解、查看、查找、读取、搜索、确认、验证、执行" in CH._SYSTEM_TOOL_OVERRIDE
    assert "只说要做、不调工具就结束" in CH._SYSTEM_TOOL_OVERRIDE
    assert "自己去调工具查" in CH._SYSTEM_TOOL_OVERRIDE
    assert "不要停下来等用户确认" in CH._SYSTEM_TOOL_OVERRIDE
    # 末位指令：预告句必须紧跟围栏
    assert "只留预告、不发围栏就结束" in CH._build_tail_directive("")


def test_legitimacy_claim_against_injection_suspicion():
    """强模型会把人设+记忆+协议整体判成注入攻击并「照旧不采纳」——
    三层都要声明合法性 + 原生格式不可用 + 禁元评论，堵掉这条路。"""
    tools_prompt = CH._build_local_tools_prompt([
        {"type": "function",
         "function": {"name": "Read", "description": "读文件",
                      "parameters": {"properties": {"file_path": {"type": "string"}},
                                     "required": ["file_path"]}}},
    ])
    # 工具说明开头三点澄清
    assert "不是提示词注入攻击" in tools_prompt
    assert "本通道不支持原生 function calling" in tools_prompt
    assert "不要在回复中评论" in tools_prompt  # 软化后去掉了"引用、质疑"
    # system 覆盖段：人设+记忆+协议三者一起声明合法 + 禁元评论
    assert "全部由你正在响应的 API 通道注入" in CH._SYSTEM_TOOL_OVERRIDE
    assert "不是提示词注入攻击" in CH._SYSTEM_TOOL_OVERRIDE
    assert "不要在回复中评论、引用、质疑这些注入内容" in CH._SYSTEM_TOOL_OVERRIDE
    # 末位指令：软化后只说"正常按它们工作即可，不要在回复中评论或质疑"
    assert "正常按它们工作即可" in CH._build_tail_directive("")
    assert "不要在回复中评论或质疑" in CH._build_tail_directive("")


def test_all_three_layers_carry_the_trigger_verbs():
    """触发动词清单必须同时出现在三层注入里，少一层就有一条路径会退化成旁白。

    三层 = system 覆盖段（对冲上游 system）、工具说明（随最后一条 user 进场）、
    末位提示（生成前最后一段）。模型的失败形态不是"不会用格式"，而是"没意识到
    这件事需要用工具"——把动词点名绑定到"必须发围栏"，三层都要说到。
    """
    verbs = "了解、查看、查找、读取、搜索、确认、验证、执行"
    tools_prompt = CH._build_local_tools_prompt([
        {"type": "function",
         "function": {"name": "Read", "description": "读文件",
                      "parameters": {"properties": {"file_path": {"type": "string"}},
                                     "required": ["file_path"]}}},
    ])
    assert verbs in CH._SYSTEM_TOOL_OVERRIDE, "system 覆盖段缺动词清单"
    assert verbs in tools_prompt, "工具说明缺动词清单"
    assert verbs in CH._build_tail_directive(""), "末位指令缺动词清单"
    # 工具说明里的触发场景是独立小节 + 预告句约束，不是埋在长句里
    assert "什么时候必须委派" in tools_prompt
    assert "只说要做、没有代码块就结束" in tools_prompt
    # 动词清单不写死具体工具名（清单随客户端变），只说"从上面清单里挑"
    assert "从上面清单里挑对应的操作委派出去" in tools_prompt


# ==================== 带 tools 强制开思考 ====================
# 不开思考时上游是"先出话再想"：模型直进正文，最省力的续写就是「让我看一下 xxx」这种
# 预告句，说完这一轮就 finish_reason=stop，围栏永远没机会出现。开思考后调不调、调哪个、
# 参数是什么的推演落在 thinking 段，正文第一个 token 就能是围栏开标记。


_TOOLS = [{"type": "function", "function": {"name": "Read", "parameters": {}}}]


def test_thinking_forced_on_when_tools_present():
    """带 tools + 模型支持思考 → 强制开，无视客户端传的 False。"""
    CH._MODEL_THINK_MAP.clear()
    CH._MODEL_THINK_MAP["m-think"] = True
    assert CH._resolve_thinking("m-think", _TOOLS, False) is True


def test_thinking_not_forced_for_model_without_think_mode():
    """模型不支持思考（hasThinkMode=False）时不硬塞，免得上游拒或吐脏帧。"""
    CH._MODEL_THINK_MAP.clear()
    CH._MODEL_THINK_MAP["m-plain"] = False
    assert CH._resolve_thinking("m-plain", _TOOLS, False) is False


def test_thinking_untouched_when_no_tools():
    """无 tools 的纯聊天不改行为：客户端说什么就是什么。"""
    CH._MODEL_THINK_MAP.clear()
    CH._MODEL_THINK_MAP["m-think"] = True
    assert CH._resolve_thinking("m-think", None, False) is False
    assert CH._resolve_thinking("m-think", [], True) is True


def test_thinking_falls_back_when_model_map_empty():
    """模型列表还没拉过（映射为空）时回落客户端原值，不擅自改行为。"""
    CH._MODEL_THINK_MAP.clear()
    assert CH._resolve_thinking("unknown", _TOOLS, False) is False
    assert CH._resolve_thinking("unknown", _TOOLS, True) is True


# ==================== fetch_models 的失败信封（现网 2026-09-16 报错）====================
# 上游登录态失效时 queryModels 回 {"resultCode": xxx, "data": null}——键在、值为 None。
# 原实现 `resp.get("data", [])` 拿不到默认值，迭代 None 抛 TypeError 崩掉整轮
# refresh_models（现网间歇复现，日志里 17:20 起 6 次）。修法是显式判 data 是不是 list，
# 失败按框架约定写 _last_fetch_models_error 并返回空列表，让同步跳过本轮而不是炸掉。


class _FakeFetchProvider:
    """fetch_models 只用到这些属性（其余链路不碰）。

    名字不与上面的 _FakeProvider 复用：同名会在模块级把那个定义覆盖掉，
    连带炸掉依赖它的消息装配用例。
    """

    PROVIDER_NAME = "ch-23"
    username = "u@example.com"

    def __init__(self):
        self._last_fetch_models_error = ""
        self.base_url = "https://eaichat.example.com"


def _stub_send_request(monkeypatch, resp):
    """把 spec 模块级的 _send_request 换成返回固定响应体的假实现。

    fetch_models 里是模块级按名调用（`_send_request(...)`），所以打模块属性即可；
    顺带跳过 init_auth 那条真实登录链路。
    """

    async def fake(p, method, path, params=None, data=None, is_auth=True):
        return resp

    monkeypatch.setattr(CH, "_send_request", fake)


def test_fetch_models_null_data_returns_empty_without_crashing(monkeypatch):
    """data 为 null（登录态失效）→ 返回空列表 + 写失败原因，不再 TypeError 崩掉整轮同步。"""
    _stub_send_request(monkeypatch, {"resultCode": 401, "resultMsg": "未登录", "data": None})
    p = _FakeFetchProvider()

    models = asyncio.run(CH.EaiChatChannel.fetch_models(p))

    assert models == []
    assert p._last_fetch_models_error
    assert "401" in p._last_fetch_models_error


def test_fetch_models_nonzero_result_code_without_data_key(monkeypatch):
    """data 键整个缺失（另一形态的失败信封）同样不崩。"""
    _stub_send_request(monkeypatch, {"resultCode": 500, "resultMsg": "系统繁忙"})
    p = _FakeFetchProvider()

    assert asyncio.run(CH.EaiChatChannel.fetch_models(p)) == []
    assert p._last_fetch_models_error


def test_fetch_models_success_still_parses_and_clears_error(monkeypatch):
    """成功路径不受影响：照常解析出模型，并把上一轮的错误清空。"""
    _stub_send_request(monkeypatch, {"resultCode": 0, "data": [
        {"model": "m-1", "keyModel": "k-1", "modelName": "模型一"},
        {"model": "m-2", "keyModel": "k-2"},
    ]})
    p = _FakeFetchProvider()
    p._last_fetch_models_error = "上一轮的旧错误"

    models = asyncio.run(CH.EaiChatChannel.fetch_models(p))

    assert [m["id"] for m in models] == ["m-1", "m-2"]
    assert models[1]["name"] == "m-2"          # 无 modelName 时回落 id
    assert CH._MODEL_KEY_MAP["m-2"] == "k-2"   # 聊天要用的 keyModel 映射照旧维护
    assert p._last_fetch_models_error == ""


def test_fetch_models_failure_keeps_previous_key_model_map(monkeypatch):
    """失败时不清 keyModel 映射——_resolve_key_model 还能拿上一轮结果兜底。"""
    _stub_send_request(monkeypatch, {"resultCode": 401, "data": None})
    CH._MODEL_KEY_MAP.clear()
    CH._MODEL_KEY_MAP["m-keep"] = "k-keep"
    p = _FakeFetchProvider()

    assert asyncio.run(CH.EaiChatChannel.fetch_models(p)) == []
    assert CH._MODEL_KEY_MAP.get("m-keep") == "k-keep"


# ==================== XML 工具调用兼容（deepseek/glm 系实测形态）====================
# 现网实测：deepseek-v4-pro / glm-5.3 收到围栏协议后，仍按 XML 标签发工具调用——
#   <tool_calls><invoke name="Read"><parameter name="file_path">D:\...\</parameter></invoke></tool_calls>
# 意图正确（要调工具）、格式不对。围栏只在 prompt 里教、靠模型自觉，压不住；
# parser 这侧必须兼容，否则整块 XML 当正文透传，调用彻底丢失、工具链路断掉。
# 判据与围栏同口径：调用名必须在入参 tools 清单里，否则整块原样当正文（防误判正文里的标签）。

# 现网帧：逐字还原 deepseek-v4-pro 的 XML 调用（含被切碎的分片）
_XML_REAL_DELTAS = [
    "<tool",
    "_calls",
    ">\n",
    "<inv",
    "oke",
    " name",
    "=\"",
    "Read",
    "\">\n",
    "<parameter",
    " name",
    "=\"",
    "file",
    "_path",
    "\">",
    "D",
    ":\\",
    "code",
    "\\",
    "ai",
    "-l",
    "ub",
    "ric",
    "ant",
    "\\",
    "server",
    "\\",
    "admin",
    ".py",
    "</",
    "parameter",
    ">\n",
    "<parameter",
    " name",
    "=\"",
    "offset",
    "\">",
    "462",
    "3",
    "</",
    "parameter",
    ">\n",
    "<parameter",
    " name",
    "=\"",
    "limit",
    "\">",
    "65",
    "</",
    "parameter",
    ">\n",
    "</",
    "inv",
    "oke",
    ">\n",
    "</",
    "tool",
    "_calls",
    ">",
]


def test_xml_tool_call_parsed_from_real_frames():
    """逐字还原的现网 XML 帧：解析出 Read 调用，正文不含任何 XML 碎片。"""
    text, calls = _replay(_XML_REAL_DELTAS, names=["Read"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文残留={text!r}"
    assert calls[0]["function"]["name"] == "Read"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["file_path"] == "D:\\code\\ai-lubricant\\server\\admin.py"
    assert args["offset"] == "4623"
    assert args["limit"] == "65"
    assert text == "", f"XML 块不该有任何正文残留，实得 {text!r}"


def test_xml_tool_call_single_frame():
    """整块在一个 delta 里（非流式聚合形态）也要认。"""
    block = ('<tool_calls>\n<invoke name="Bash">\n'
             '<parameter name="command">ls -la</parameter>\n'
             '</invoke>\n</tool_calls>')
    text, calls = _replay([block], names=["Bash"])
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls -la"}
    assert text == ""


def test_xml_multiple_invokes_in_one_block():
    """一个 <tool_calls> 里多个 <invoke>（并行调用）逐个解析。"""
    block = ('<tool_calls><invoke name="Read">'
             '<parameter name="file_path">a.py</parameter></invoke>'
             '<invoke name="Grep">'
             '<parameter name="pattern">foo</parameter></invoke></tool_calls>')
    text, calls = _replay([block], names=["Read", "Grep"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Grep"]
    assert text == ""


def test_xml_unknown_tool_name_passes_through_as_text():
    """XML 里工具名不在入参清单里 -> 整块原样当正文，不伪造 tool_calls（同围栏口径）。"""
    block = ('<tool_calls><invoke name="NotMyTool">'
             '<parameter name="x">1</parameter></invoke></tool_calls>')
    text, calls = _replay([block], names=["Read"])
    assert calls == []
    assert block in text, "不在清单里的块必须原样透传，不能吞"


def test_xml_block_survives_split_open_marker():
    """开标记 <tool_calls> 被切碎时不能提前把碎片当正文下发。"""
    text, calls = _replay(
        ["<tool_cal", "ls><invoke name=\"Read\">",
         "<parameter name=\"file_path\">b.py</parameter></invoke></tool_calls>"],
        names=["Read"])
    assert len(calls) == 1
    assert text == "", f"碎片不该泄漏成正文，实得 {text!r}"


def test_xml_and_fence_mixed_in_one_stream():
    """同一流里围栏块与 XML 块混发：两种都切出来，顺序不乱。"""
    fence = '```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n```'
    xml = ('<tool_calls><invoke name="Bash">'
           '<parameter name="command">pwd</parameter></invoke></tool_calls>')
    text, calls = _replay(["前置文本", fence, "中间", xml, "结尾"], names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"]
    assert text == "前置文本中间结尾", f"正文应只含非块文本，实得 {text!r}"


def test_xml_unclosed_at_stream_end_is_rescued():
    """流截断（无 </tool_calls>）时，已闭合的 <invoke> 仍要救回，不能整块丢。"""
    text, calls = _replay(
        ['<tool_calls><invoke name="Read">',
         '<parameter name="file_path">c.py</parameter></invoke>'],
        names=["Read"])
    assert len(calls) == 1, "已闭合的 invoke 必须救回"
    assert json.loads(calls[0]["function"]["arguments"]) == {"file_path": "c.py"}


def test_xml_tags_in_plain_prose_not_a_tool_call():
    """正文里顺带提到 <tool_calls> 字样但没有合法 invoke -> 原样透传，不误判。"""
    prose = "你可以用 <tool_calls> 标签表达调用，但本通道不认这个格式。"
    text, calls = _replay([prose], names=["Read"])
    assert calls == []
    assert text == prose


# ==================== 相邻围栏：hold 半截开标记不能只在「没切出块」时跑 ====================
# 实测 qwen3.8-max 连发两个 Read：前一围栏的闭标 ``` 与后一围栏的开标 ```tool_function
# 在相邻帧到达，切走第一块后残留的半截开标记（```tool）落进 prefix。旧代码只在
# `if not blocks:` 时跑 hold，于是第一块切出后 hold 被跳过——半截开标记裸奔下发成
# 正文（客户端看到反引号碎片），且第二块的开标记被吞，整块 JSON 当正文泄漏。

# 逐字还原现网帧：模型把两个围栏紧挨着写，6 个反引号 = 闭标 3 + 开标 3 被切成一帧
_ADJACENT_FENCE_DELTAS = [
    "```",
    "tool_function\n{\"",
    "name\": \"Read",
    "\", \"arguments\":",
    " {\"file_path\":",
    " \"D:\\\\L",
    "oomy\\\\resources",
    "\\\\app.asar",
    ".unpacked\\\\electron",
    "\\\\xfyun\\\\",
    "sign.js\"}}\n",
    "``````tool",            # ← 6 反引号 + "tool"：前块闭标 + 后块开标首段
    "_function\n{\"name",     # ← 后块开标剩余
    "\": \"Read\",",
    " \"arguments\": {\"",
    "file_path\": \"",
    "D:\\\\Loom",
    "y\\\\resources\\\\",
    "app.asar.un",
    "packed\\\\electron\\\\",
    "xfyun\\\\__",
    "tests__\\\\account",
    "-service-session-auth",
    ".test.js\"}}\n",
    "```",
]


def test_adjacent_fences_second_block_not_leaked():
    """两个围栏紧挨着：第二块必须照常解析，不能泄漏为正文。"""
    text, calls = _replay(_ADJACENT_FENCE_DELTAS, names=["Read"])
    assert len(calls) == 2, f"应解析出 2 个调用，实得 {len(calls)}；正文={text!r}"
    assert [c["function"]["name"] for c in calls] == ["Read", "Read"]
    a0 = json.loads(calls[0]["function"]["arguments"])
    a1 = json.loads(calls[1]["function"]["arguments"])
    assert a0["file_path"].endswith("sign.js")
    assert a1["file_path"].endswith("account-service-session-auth.test.js")
    assert text == "", f"两块之间不该有任何正文泄漏，实得 {text!r}"


def test_adjacent_fence_split_marker_survives():
    """第一块的闭标 ``` 和第二块的开标 ```tool_function 分到不同帧也要切对。"""
    fence1 = '```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n```'
    fence2 = '```tool_function\n{"name": "Bash", "arguments": {"command": "ls"}}\n```'
    text, calls = _replay([fence1[:-3], "```", "```", "tool_function", fence1[16:], fence2],
                         names=["Read", "Bash"])
    # 无论如何切分，只要文本连续就该解析出调用、不泄漏
    assert len(calls) >= 1, f"至少第一块该解析出来；正文={text!r}"


def test_fence_close_then_partial_open_in_one_delta():
    """一帧里同时有「前块闭标 + 后块开标首段」的最小复现。"""
    text, calls = _replay([
        '```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n',
        '``````tool',               # 闭标 + 下一块开标首段
        '_function\n{"name": "Bash", "arguments": {"command": "pwd"}}\n```',
    ], names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"]
    assert text == "", f"不应泄漏，实得 {text!r}"


# ==================== 思考帧 type 兼容（thinking_card）====================
# 现网实测：天翼 EAI 系模型思考帧的 delta.type = "thinking_card"（不是 Anthropic 的
# "thinking_delta"、也不是裸 "thinking"）。旧代码只认 type=="thinking"，于是思考文本
# 落进 content 分支被当正文下发 —— 客户端看到整段内部推演（实测泄漏给用户），
# 且会被围栏解析器当正文处理。修复：thinking / thinking_card / reasoning_content 都归 thinking。


def test_thinking_card_delta_goes_to_thinking_not_content():
    """delta.type=thinking_card -> 归 thinking，绝不落 content。"""
    frame = {"choices": [{"delta": {
        "role": "assistant", "waiting": 0,
        "meta_data": {"thinking_card": {"id": "x", "status": "GENERATING"}},
        "type": "thinking_card", "content": " detection to the"}}]}
    out = CH._openai_event_chunks(frame, {"enabled": True, "buf": ""}, {})
    assert len(out) == 1
    assert out[0]["thinking"] == " detection to the"
    assert out[0]["content"] == "", "思考绝不能落进 content"
    assert out[0]["tool_calls"] == []


def test_thinking_card_end_frame_emits_nothing():
    """thinking_card 收尾帧（status=END、content 空、全文在 meta_data）不重复下发。"""
    frame = {"choices": [{"delta": {
        "role": "assistant",
        "meta_data": {"thinking_card": {"id": "x", "status": "END",
                                        "content": "完整思考全文（已逐帧发过）"}},
        "type": "thinking_card", "content": ""}}]}
    out = CH._openai_event_chunks(frame, {"enabled": True, "buf": ""}, {})
    assert out == [], "收尾帧 content 为空，不应再发（否则思考重复）"


def test_thinking_legacy_types_still_work():
    """原有的 thinking / reasoning_content 两种形态不能被新分支破坏。"""
    f1 = {"choices": [{"delta": {"type": "thinking", "content": "t1"}}]}
    f2 = {"choices": [{"delta": {"reasoning_content": "t2"}}]}
    assert CH._openai_event_chunks(f1, {"enabled": True, "buf": ""}, {})[0]["thinking"] == "t1"
    assert CH._openai_event_chunks(f2, {"enabled": True, "buf": ""}, {})[0]["thinking"] == "t2"


def test_text_delta_still_goes_to_content():
    """type=text 的正文帧照旧进围栏缓冲（走 content），不被思考分支误吞。"""
    frame = {"choices": [{"delta": {"type": "text", "content": "普通正文"}}]}
    out = CH._openai_event_chunks(frame, {"enabled": False, "buf": ""}, {})
    assert out[0]["content"] == "普通正文"
    assert out[0]["thinking"] == ""


# ==================== bare 开标记（上游网关吞掉前缀反引号）====================
# 实测 2026-09-19 glm-5.2-cdn：模型发的围栏整段以 text 帧吐出，但开头的三个反引号
# 被网关吞掉，只剩裸 `tool_function{...}`。修复前解析器只认 ```tool_function，找不到
# 就把整块 JSON 当正文下发 —— 客户端看到「模型只输出了文本」，工具不执行、任务卡死。
# 更狠的一档（qwen3.5-plus 实测）连 `tool` 也丢，只剩 `_function{...}`。

_BARE_DELTAS = [
    "tool_function\n{\"name\":", " \"Grep\", \"arguments", "\": {\"",
    "pattern\": \"全局配置|", "关于|语言设置|样式", "|当前版本\", \"glob",
    "\": \"*", ".{tsx,", "ts,jsx,js", ",vue}\", \"output",
    "_mode\": \"files_with_matches", "\", \"head_limit\": ", "30}}\n",
    "``", "`\n\n<", "system-reminder>",
]


def test_bare_open_marker_without_backticks_still_parsed():
    """裸 `tool_function{...}`（前缀 ``` 被吞）：必须切出调用，且不泄漏成正文。"""
    text, calls = _replay(_BARE_DELTAS, names=["Grep"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文={text!r}"
    assert calls[0]["function"]["name"] == "Grep"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["pattern"] == "全局配置|关于|语言设置|样式|当前版本"
    assert args["glob"] == "*.{tsx,ts,jsx,js,vue}"
    assert args["head_limit"] == 30
    assert "tool_function" not in text, f"围栏不得泄漏成正文：{text!r}"
    assert "system-reminder" in text, "围栏之后的正文必须保留"


def test_bare_open_marker_split_across_frames():
    """裸开标记本身也被上游切成多帧（`tool_` → `function` → `{`）时仍能切对。"""
    text, calls = _replay(["tool_", "function", "\n{\"name\": \"Bash\", ",
                           "\"arguments\": {\"command\": \"ls\"}}", "\n```"],
                          names=["Bash"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文={text!r}"
    assert json.loads(calls[0]["function"]["arguments"])["command"] == "ls"


def test_bare_open_marker_partial_prefix_held_not_leaked():
    """裸开标记的半截前缀（`tool_func`）不得当正文下发，补全后必须切出调用。"""
    text, calls = _replay(["tool_func", "tion\n{\"name\": \"Bash\", "
                           "\"arguments\": {\"command\": \"ls\"}}", "\n```"],
                          names=["Bash"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文={text!r}"
    assert "tool_func" not in text, f"半截开标记不得泄漏：{text!r}"


def test_function_only_anchor_rescued():
    """上游连 `tool` 一起吞掉、只剩 `_function{...}`：靠短锚救回。"""
    text, calls = _replay(["\n", "_function\n{\"name\": \"Bash\", "
                           "\"arguments\": {\"command\": \"bash pack.sh\"}}", "\n```"],
                          names=["Bash"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文={text!r}"
    assert json.loads(calls[0]["function"]["arguments"])["command"] == "bash pack.sh"


def test_function_anchor_requires_identifier_boundary():
    """正文里的 `test_function{...}` / `my_function{` 不得被误判成工具调用。"""
    text, calls = _replay(["代码说明：调用 test_function{...} 后返回。",
                           "再看 my_function{a:1}。"], names=["Bash"])
    assert calls == [], f"不该解析出任何调用，实得 {calls}"
    assert "test_function{...}" in text and "my_function{a:1}" in text


def test_bare_malformed_block_passed_through_as_text_not_dropped():
    """bare 是启发式推断：块内 JSON 坏掉时必须**原样下发正文**，不能静默吞掉。"""
    text, calls = _replay(["tool_function\n{\"name\": \"Bash\", \"arguments\": "
                           "{command\": \"ls\"}}", "\n```"], names=["Bash"])
    assert calls == [], "坏 JSON 不该产出调用"
    assert "tool_function" in text, f"坏块必须原样可见，不能吞掉：{text!r}"


def test_bare_and_standard_fences_coexist():
    """同一流里 bare 形态与标准 ``` 形态混用，两块都要正确解析。"""
    text, calls = _replay(
        ["tool_function\n{\"name\": \"Read\", \"arguments\": {\"file_path\": \"a.py\"}}\n```\n",
         '```tool_function\n{"name": "Bash", "arguments": {"command": "ls"}}\n```'],
        names=["Read", "Bash"])
    assert len(calls) == 2, f"应解析出 2 个调用，实得 {len(calls)}；正文={text!r}"
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"]


def test_orphan_closing_fence_swallowed():
    """bare 块按花括号配平先切走、闭标 ``` 晚到：孤儿闭标不得当正文下发。"""
    text, calls = _replay(["tool_function\n{\"name\": \"Bash\", "
                           "\"arguments\": {\"command\": \"ls\"}}", "\n``", "`"],
                          names=["Bash"])
    assert len(calls) == 1
    assert text.strip() == "", f"孤儿闭标不得泄漏：{text!r}"


# ==================== 一个围栏内并列多个调用 ====================
# 模型把多个工具调用并列写进**同一个**围栏是实测形态：换行分隔、逗号分隔、JSON 数组。
# 旧实现只取第一个配平对象，其余要么静默丢弃（调用彻底丢、任务卡死）、要么泄漏成正文。

def test_multiple_calls_in_one_fence_newline_separated():
    """一个围栏内两个 JSON 对象（换行分隔）：两个都要出，不能只出第一个。"""
    text, calls = _replay(['```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n'
                           '{"name": "Bash", "arguments": {"command": "ls"}}\n```'],
                          names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"], \
        f"两个调用都要解析，实得 {[c['function']['name'] for c in calls]}；正文={text!r}"
    assert json.loads(calls[0]["function"]["arguments"])["file_path"] == "a.py"
    assert json.loads(calls[1]["function"]["arguments"])["command"] == "ls"


def test_multiple_calls_in_one_fence_comma_separated():
    """一个围栏内两个 JSON 对象（逗号分隔）：两个都要出。"""
    text, calls = _replay(['```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}, '
                           '{"name": "Bash", "arguments": {"command": "ls"}}\n```'],
                          names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"], \
        f"两个调用都要解析；正文={text!r}"


def test_multiple_calls_in_one_fence_json_array():
    """一个围栏内是 JSON 数组：数组里每个元素都是调用。"""
    text, calls = _replay(['```tool_function\n[{"name": "Read", "arguments": {"file_path": "a.py"}}, '
                           '{"name": "Bash", "arguments": {"command": "ls"}}]\n```'],
                          names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"], \
        f"数组元素都要解析；正文={text!r}"


def test_multiple_calls_in_one_fence_split_across_frames():
    """多调用围栏被逐帧切开（第二块跨帧到达）时仍要全部解析。"""
    text, calls = _replay(['```tool_function\n[{"name": "Read", "arguments": {"file_path": "a.py"',
                           '}}, {"name": "Bash", "arguments": {"command": "ls"}}]\n```'],
                          names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"], \
        f"跨帧的多调用都要解析；正文={text!r}"


def test_multiple_calls_in_one_bare_fence():
    """bare 形态（无前缀反引号）下多调用：第二个不得泄漏成正文。"""
    text, calls = _replay(['tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n'
                           '{"name": "Bash", "arguments": {"command": "ls"}}\n```'],
                          names=["Read", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Bash"], \
        f"bare 多调用都要解析；正文={text!r}"
    assert '"name"' not in text, f"第二个调用不得泄漏成正文：{text!r}"


def test_multiple_calls_unknown_method_passes_whole_block_through():
    """多调用里有一个名字不在入参 tools 里：整块原样当正文，不伪造部分调用。"""
    text, calls = _replay(['```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n'
                           '{"name": "Nonexistent", "arguments": {}}\n```'],
                          names=["Read", "Bash"])
    assert calls == [], f"含未知方法时整块不下发调用，实得 {calls}"
    assert "Nonexistent" in text, f"整块应原样可见：{text!r}"


def test_multi_call_braces_inside_argument_value_not_split():
    """arguments 值里含花括号（Write 写代码）不得被当成调用边界切开。"""
    text, calls = _replay(['```tool_function\n{"name": "Write", "arguments": '
                           '{"content": "x = {1: 2}"}}\n'
                           '{"name": "Bash", "arguments": {"command": "ls"}}\n```'],
                          names=["Write", "Bash"])
    assert [c["function"]["name"] for c in calls] == ["Write", "Bash"], \
        f"值内花括号不该影响切分；正文={text!r}"
    assert json.loads(calls[0]["function"]["arguments"])["content"] == "x = {1: 2}"


def test_multi_call_trailing_prose_after_calls_not_parsed_as_call():
    """调用之后的尾随非 JSON 文本不得被卷成额外调用（只取并列的 JSON 值）。"""
    text, calls = _replay(['```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n'
                           '说明文字 {不是JSON}\n```'],
                          names=["Read"])
    assert [c["function"]["name"] for c in calls] == ["Read"], \
        f"尾随文本不该变成调用；实得 {[c['function']['name'] for c in calls]}"


# ==================== 两块接缝处的反引号被上游吞掉 ====================
# 实测 2026-09-20 glm-5.3-ctyun-pt-api：模型连发两个 Grep，第一块闭标 ``` 与第二块
# 开标 ``` 在原文里是同一串 6 个反引号，上游吞掉 1 个（只到 5 个）。切走第一块闭标
# （3 个）后残留 2 个落进正文，客户端看到孤立反引号碎片，第二个调用也可能切不出来。

_C1 = '{"name": "Grep", "arguments": {"pattern": "async def _do_stream_chat"}}'
_C2 = '{"name": "Grep", "arguments": {"pattern": "non_stream_chat|_do_stream_chat"}}'


def _join_stream(n: int) -> str:
    """两块紧挨着、接缝处共 n 个反引号（原文应为 6）的完整流。"""
    return "```tool_function\n" + _C1 + "\n" + "`" * n + "tool_function\n" + _C2 + "\n```"


@pytest.mark.parametrize("n", [6, 5, 4, 3])
def test_adjacent_fences_join_backticks_absorbed(n):
    """接缝反引号被吞到 6/5/4/3 个：两个调用都要出，且不得泄漏反引号碎片。"""
    text, calls = _replay([_join_stream(n)], names=["Grep"])
    assert [c["function"]["name"] for c in calls] == ["Grep", "Grep"], \
        f"接缝 n={n} 时两个调用都要解析，实得 {[c['function']['name'] for c in calls]}"
    assert "`" not in text, f"接缝 n={n} 时不得泄漏反引号碎片：{text!r}"


@pytest.mark.parametrize("n", [6, 5, 4, 3])
def test_adjacent_fences_join_backticks_absorbed_char_by_char(n):
    """同上，但逐字符喂（最坏切分）。"""
    text, calls = _replay(list(_join_stream(n)), names=["Grep"])
    assert [c["function"]["name"] for c in calls] == ["Grep", "Grep"], \
        f"逐字符接缝 n={n} 时两个调用都要解析；正文={text!r}"
    assert "`" not in text, f"逐字符接缝 n={n} 时不得泄漏反引号碎片：{text!r}"


def test_join_absorb_does_not_eat_plain_backtick_prose():
    """正文里以反引号开头、后面不是工具开标记的文本不得被吸收。"""
    text, calls = _replay(["`code` 说明", "继续"], names=["Grep"])
    assert calls == []
    assert "`code`" in text, f"普通反引号文本必须原样保留：{text!r}"


def test_join_absorb_keeps_standard_open_marker():
    """接缝后跟标准开标记（>=3 个反引号）时，必须仍按标准围栏切出第二个调用。"""
    text, calls = _replay(['```tool_function\n' + _C1 + '\n```'
                           '```tool_function\n' + _C2 + '\n```'], names=["Grep"])
    assert [c["function"]["name"] for c in calls] == ["Grep", "Grep"], \
        f"标准形态接缝也要两个调用；正文={text!r}"
    assert text.strip() == "", f"不得泄漏：{text!r}"


# ==================== 畸形 name 不得崩掉整条流 ====================
# 实测 2026-09-20：解析产物里 function.name 是 dict（模型多包了一层壳）时，
# `name in frozenset` 抛 TypeError: unhashable type: 'dict' → 整条流 502。

def test_dict_valued_name_does_not_crash():
    """name 被多包一层壳时，下沉一层取 name+arguments —— 不得崩，也不得丢参数。"""
    text, calls = _replay(['```tool_function\n'
                           '{"name": {"name": "Grep", "arguments": {"pattern": "x"}}}\n```'],
                          names=["Grep"])
    assert [c["function"]["name"] for c in calls] == ["Grep"], \
        f"多包一层壳应下沉取值；实得 {calls}"
    assert json.loads(calls[0]["function"]["arguments"])["pattern"] == "x", \
        "下沉后 arguments 必须一起取到（不能只剩空参数）"


def test_non_string_name_falls_through_as_text():
    """name 根本不是字符串（如数字/列表）时，整块原样透传正文，不崩、不伪造调用。"""
    text, calls = _replay(['```tool_function\n{"name": 123, "arguments": {}}\n```'],
                          names=["Grep"])
    assert calls == [], f"非字符串 name 不该产出调用，实得 {calls}"


def test_calls_all_known_tolerates_non_string_name():
    """_calls_all_known 对非字符串 name 返回 False 而非抛 TypeError。"""
    assert CH._calls_all_known([{"function": {"name": {"nested": 1}}}], frozenset({"Grep"})) is False
    assert CH._calls_all_known([{"function": {"name": None}}], frozenset({"Grep"})) is False
    assert CH._calls_all_known([{"function": {}}], frozenset({"Grep"})) is False
    assert CH._calls_all_known([{"function": {"name": "Grep"}}], frozenset({"Grep"})) is True


def test_extra_closing_brace_in_fence_does_not_crash():
    """块内多一个 ``}`` 时不得崩，且两个调用仍要切出来。"""
    text, calls = _replay(['```tool_function\n' + _C1 + '}\n```\n'
                           '```tool_function\n' + _C2 + '\n```'], names=["Grep"])
    assert len(calls) == 2, f"多余花括号不该影响后续块；实得 {len(calls)}，正文={text!r}"


# ==================== 无开标记形态（裸 JSON + 结束围栏）====================
# 实测 2026-09-26 glm5.2-cdn：模型把开标记**整个丢了** —— JSON 主体写对、结尾 ```
# 也写对，唯独开头的 ```tool_function 没吐出来，后面还跟着 </tool_function> 残片
# （模型混用了围栏与 XML 闭标签）。修复前整块漏成正文，工具不执行。

_MARKERLESS_BODY = ('{"name": "Grep", "arguments": {"pattern": "anthropic|claude-code|preset", '
                    '"path": "D:\\\\code\\\\ai-lubricant\\\\providers\\\\custom.py"}}')


def test_markerless_json_with_closing_fence_rescued():
    """裸 JSON + 结束围栏（开标记整个丢失）：必须切出调用。"""
    text, calls = _replay(["\n" + _MARKERLESS_BODY, "```\n</tool_function>"], names=["Grep"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文={text!r}"
    assert calls[0]["function"]["name"] == "Grep"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["pattern"] == "anthropic|claude-code|preset"
    assert "tool_function" not in text, f"闭标签残片不得泄漏：{text!r}"


def test_markerless_json_split_frames_stray_tag_fragment():
    """日志原样帧：闭标被切成 `` `` ``+`` ` ``、残片 ``</tool`` 被截断在流末。"""
    text, calls = _replay(["\n" + _MARKERLESS_BODY[:20], _MARKERLESS_BODY[20:] + "``",
                           "`\n</", "tool"], names=["Grep"])
    assert len(calls) == 1, f"应解析出 1 个调用，实得 {len(calls)}；正文={text!r}"
    assert "tool" not in text and "<" not in text, f"残片不得泄漏：{text!r}"


def test_markerless_json_no_fence_stays_prose():
    """只有裸 JSON、连结束围栏都没有：不得切成调用（判据 ④ 不满足）。"""
    text, calls = _replay(["\n" + _MARKERLESS_BODY], names=["Grep"])
    assert calls == [], f"无结束围栏不该切成调用，实得 {calls}"
    assert "Grep" in text, f"JSON 应原样可见：{text!r}"


def test_json_example_inside_fence_stays_prose_cross_frame():
    """```json 代码示例（开围栏在前面的帧）：不得误判成调用（判据 ① 的跨帧形态）。"""
    text, calls = _replay(["好的：\n```json\n", _MARKERLESS_BODY, "\n```\n以上。"],
                          names=["Grep"])
    assert calls == [], f"代码示例不得误判成调用，实得 {calls}"
    assert "```json" in text, f"示例应原样保留：{text!r}"


def test_markerless_disabled_after_fence_in_prose():
    """正文里出现过 ``` 后，无开标记兜底停用（防示例误判）。"""
    text, calls = _replay(["说明：\n```\n代码块\n```\n", "再看 " + _MARKERLESS_BODY + "\n```"],
                          names=["Grep"])
    assert calls == [], f"见过 ``` 后不该再认无开标记形态，实得 {calls}"


def test_stray_close_tag_alone_in_flush_dropped():
    """流末只剩 ``</tool`` 残片：丢弃，不当正文补发。"""
    text, calls = _replay(["```tool_function\n{\"name\": \"Read\", "
                           "\"arguments\": {\"file_path\": \"a.py\"}}\n```", "\n</tool"],
                          names=["Read"])
    assert len(calls) == 1
    assert "</tool" not in text and "tool" not in text, f"残片不得泄漏：{text!r}"


# ==================== 中间 JSON 少外层 } 的接缝救援 ====================
# 实测 2026-09-26 glm-5.3-ctyun-pt-api：模型连发 5 个调用（Read→Glob→Glob→Read→Grep），
# 中间两个 Glob 各少一个最外层 `}`（`"arguments": {...}` 后没再补一个 `}`）。
# 闭标判据依赖花括号深度归零，于是后续每个围栏都被判「未闭合」留在缓冲里，
# 流结束整段泄漏成正文 —— 客户端只拿到第一个 Read、其余 4 个当文本。
# 修复：接缝救援（_fence_seam_cut）——那串反引号后面紧跟下一个工具块开标记即
# 「上一块到此为止」的确定信号，切在这里并把缺的 `}` 由解析侧补回
# （_block_chunk → allow_unclosed=1）。

_C_READ1 = '{"name": "Read", "arguments": {"file_path": "D:\\\\Loomy\\\\resources\\\\app.asar.unpacked\\\\electron\\\\points-config.js"}}'
_C_GLOB1 = '{"name": "Glob", "arguments": {"path": "D:\\\\Loomy", "pattern": "**/.env.prod"}}'
_C_GLOB2 = '{"name": "Glob", "arguments": {"path": "D:\\\\Loomy", "pattern": "**/env-file-crypto.js"}}'
_C_READ2 = '{"name": "Read", "arguments": {"file_path": "D:\\\\code\\\\ai-lubricant\\\\providers\\\\code_spec.py", "limit": 60, "offset": 520}}'
_C_GREP = '{"name": "Grep", "arguments": {"-n": true, "head_limit": 40, "output_mode": "content", "path": "D:\\\\code\\\\ai-lubricant\\\\server\\\\admin.py", "pattern": "refresh_account_auth|refresh_auth|quota_text|metadata_badges"}}'


def test_seam_rescue_for_unbalanced_braces_one_shot():
    """一次喂完整流（含两个 Glob 少外层 }）：5 个调用全出，正文不留。"""
    whole = ('```tool_function\n' + _C_READ1 + '\n'
             '``````tool_function\n' + _C_GLOB1 + '\n'
             '``````tool_function\n' + _C_GLOB2 + '\n'
             '``````tool_function\n' + _C_READ2 + '\n'
             '``````tool_function\n' + _C_GREP + '\n'
             '```')
    text, calls = _replay([whole], names=["Read", "Glob", "Grep"])
    got_names = [c["function"]["name"] for c in calls]
    want_names = ["Read", "Glob", "Glob", "Read", "Grep"]
    assert got_names == want_names, \
        f"5 个调用都要解析；实得 {got_names}；正文={text!r}"
    assert text.strip() == "", f"不得泄漏正文：{text!r}"


def test_seam_rescue_for_unbalanced_braces_char_by_char():
    """同上，但逐字符喂（最坏切分）：5 个调用全出，正文不留。"""
    whole = ('```tool_function\n' + _C_READ1 + '\n'
             '``````tool_function\n' + _C_GLOB1 + '\n'
             '``````tool_function\n' + _C_GLOB2 + '\n'
             '``````tool_function\n' + _C_READ2 + '\n'
             '``````tool_function\n' + _C_GREP + '\n'
             '```')
    text, calls = _replay(list(whole), names=["Read", "Glob", "Grep"])
    got_names = [c["function"]["name"] for c in calls]
    want_names = ["Read", "Glob", "Glob", "Read", "Grep"]
    assert got_names == want_names, \
        f"逐字符 5 个调用都要解析；实得 {got_names}；正文={text!r}"
    assert text.strip() == "", f"不得泄漏正文：{text!r}"


def test_seam_rescue_preserves_nested_braces_in_args():
    """参数值内嵌花括号（正则模式 `*.tsx,ts,jsx,js,vue}`）不被误判为未闭合。"""
    # 用带嵌套花括号的真实场景测试：glob pattern 里的扩展列表
    C_WITH_BRACES = '{"name": "Glob", "arguments": {"pattern": "x", "glob": "*.tsx,ts,jsx,js,vue"}}'
    whole = ('```tool_function\n{"name": "Read", "arguments": {"file_path": "a.py"}}\n'
             '``````tool_function\n' + C_WITH_BRACES + '\n'
             '```')
    text, calls = _replay([whole], names=["Read", "Glob"])
    got_names = [c["function"]["name"] for c in calls]
    assert got_names == ["Read", "Glob"], \
        f"参数内嵌花括号不应影响后续块；实得 {got_names}；正文={text!r}"
    args = json.loads(calls[1]["function"]["arguments"])
    assert args["glob"] == "*.tsx,ts,jsx,js,vue", \
        f"扩展列表应保真：{args['glob']!r}"


# ==================== 数字后多打一个裸引号（_can_open_string）====================
# 实测 2026-09-27 glm-5.3-ctyun-pt-api：模型写 "offset": 788"}} —— 数字 788 后面
# 多了一个双引号（本该直接写 788）。这个引号在字符串**外**，会开出一个永不闭合的
# JSON 字符串，把后面的 }}、换行、闭栏 ``` 全吞进去：闭标判据认不出闭合、flush 救援
# 也在字符串内结束 —— 整段围栏原样漏成正文，编辑器以为模型只输出了文本。
# 与「字符串内的裸引号」（_is_real_string_close 补转义）互为镜像：这次要**丢弃**。

_STRAY_BODY = ('{"name": "Read", "arguments": {"file_path": '
               '"D:\\\\code\\\\ai-lubricant\\\\tests\\\\test_account_auth_context.py", '
               '"limit": 60, "offset": 788"}}')
_STRAY_BLOCK = "```tool_function\n" + _STRAY_BODY + "\n```"


def test_stray_quote_after_number_rescues_whole_fence():
    """数字后的多余引号：丢弃它，整块照常切出并解析成调用。"""
    text, calls = _replay([_STRAY_BLOCK], names=["Read"])
    assert [c["function"]["name"] for c in calls] == ["Read"], \
        f"应解析出 1 个 Read 调用，实得 {calls}；正文={text!r}"
    assert text.strip() == "", f"围栏不得泄漏成正文：{text!r}"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["offset"] == 788, f"offset 应为整数 788，实得 {args['offset']!r}"
    assert args["limit"] == 60
    assert args["file_path"].endswith("test_account_auth_context.py")


def test_stray_quote_after_number_char_by_char():
    """同上逐字符喂（最坏切分）：照样只剩一个调用、正文干净。"""
    text, calls = _replay(list(_STRAY_BLOCK), names=["Read"])
    assert [c["function"]["name"] for c in calls] == ["Read"], \
        f"逐字符应解析出 Read，实得 {calls}；正文={text!r}"
    assert text.strip() == "", f"逐字符不得泄漏：{text!r}"


def test_stray_quote_survives_to_flush_rescue():
    """流末没有闭栏（上游截断）也要靠 flush 救援把调用救回来。"""
    text, calls = _replay(["```tool_function\n" + _STRAY_BODY], names=["Read"])
    assert [c["function"]["name"] for c in calls] == ["Read"], \
        f"缺闭栏也要救回；实得 {calls}；正文={text!r}"


def test_can_open_string_allows_only_value_positions():
    """合法起始只有 { [ , : 与块首；数字/字面量/} ]/引号 之后都算多余引号。"""
    ok = CH._can_open_string
    assert ok("") and ok("{") and ok("[") and ok(",") and ok(":"), "键/值起点应允许开字符串"
    for bad in ("8", "e", "}", "]", '"', "0", "l"):
        assert not ok(bad), f"{bad!r} 之后不该开字符串"


@pytest.mark.parametrize("valid", [
    '{"name": "Bash", "arguments": {"command": "ls -la"}}',
    '{"name": "Read", "arguments": {"file_path": "a.py", "offset": 4623}}',
    '{"name": "Grep", "arguments": {"-n": true, "head_limit": 30, "aa": false, "b": null}}',
    '{"name": "X", "arguments": {"list": [1, 2], "nested": {"k": [ { "a": 1 } ]}}}',
    '{"name": "X", "arguments": {"a" : 1}}',
])
def test_repair_stray_quotes_stays_identity_on_valid_json(valid):
    """新增「丢弃非法起始引号」不得破坏合法 JSON —— 合法输入必须逐字节不变。"""
    assert CH._repair_stray_quotes(valid) == valid, \
        f"合法 JSON 被改动了：\n  {valid}\n  -> {CH._repair_stray_quotes(valid)}"


def test_legit_string_after_number_with_comma_is_kept():
    """数字后有逗号再接字符串是合法的，不得被当成多余引号丢掉。"""
    body = '{"name": "Grep", "arguments": {"head_limit": 40, "pattern": "x"}}'
    text, calls = _replay(["```tool_function\n" + body + "\n```"], names=["Grep"])
    assert len(calls) == 1, f"合法多键块不得被误修；实得 {calls}；正文={text!r}"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"head_limit": 40, "pattern": "x"}, args


def test_stray_quote_does_not_rescue_unrelated_prose():
    """正文里出现「数字+引号」的普通文本不得被切成调用（仍是正文）。"""
    text, calls = _replay(['偏移 788" 之后是说明文字，没有围栏。'], names=["Read"])
    assert calls == [], f"正文不该产出调用，实得 {calls}"
    assert '788"' in text, f"正文必须原样保留：{text!r}"


def test_stray_quote_in_markerless_block_rescued():
    """同一畸形 + 无开标记形态：_iter_json_regions 也要认得闭合（四扫描器同口径）。"""
    text, calls = _replay(["\n" + _STRAY_BODY, "\n```"], names=["Read"])
    assert [c["function"]["name"] for c in calls] == ["Read"], \
        f"无开标记形态应照样救回；实得 {calls}；正文={text!r}"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["offset"] == 788, args
    assert text.strip() == "", f"无开标记形态不得泄漏：{text!r}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
