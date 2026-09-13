"""duck.ai spec（specs/duckai.py）的消息转换契约。

duck.ai 不收 role=system（发 system 直接 400），框架把 system 内容拼进最后一条 user
消息。若客户端是 Claude Code / Codex 等，这会把它们的身份模板句 + 计费头键值段原样
带进 user 消息发到上游——既是编辑器指纹泄漏，也是上游逐字内容黑名单的触发器。

此外对齐 eaichat：会话准则 + 工具说明 + 末位提示三层注入，末位提示钉在最后一条 user
末尾；content parts 形态正确切分；带 tools 强制开思考；围栏在渠道内 JSON 感知解析。

本测试锁住 specs/duckai.py 的行为（模块级函数，不触碰认证链路）：
1. Claude Code / Codex 身份句 → 整句删除；
2. x-anthropic-billing-header / cc_* 键值段 → 剥离，正常行保留；
3. 不含特征串的普通 system → 逐字保留（零改动，无 tools_prompt 时）；
4. 无 system 的普通对话 → 不受影响；
5. 有 tools_prompt 时：准则 + 工具说明拼在最后一条 user 前，末位提示钉在末尾；
6. content parts 形态：原 part 保留，前言前置、末位提示后置；
7. 围栏解析：JSON 感知闭合、入参工具名校验、截断救援；
8. 带 tools 强制开思考。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "duckai.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/duckai.py 不存在（使用者自持）", allow_module_level=True)


@pytest.fixture(scope="module")
def mod():
    """加载 spec 模块本身（拿模块级函数）。"""
    spec = importlib.util.spec_from_file_location("duckai_spec_module", _SPEC_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _P:
    """最小 provider 替身：_normalize_messages_for_duck 只用到 flatten_tool_history。"""

    @staticmethod
    def flatten_tool_history(messages):
        return messages


def _final_user_content(mod, messages, tools_prompt=""):
    out = mod._normalize_messages_for_duck(_P(), messages, tools_prompt)
    # 输出绝不含 system role，且 system 内容拼进最后一条 user。
    assert all((m.get("role") or "") != "system" for m in out)
    return out[-1]["content"] if out else ""


def _texts(msg):
    """把一条消息的 content 取成可搜索的纯文本（兼容 str / content parts）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


# ==================== 编辑器指纹剥离 ====================


def test_claude_code_identity_stripped(mod):
    msgs = [
        {
            "role": "system",
            "content": (
                "You are Claude Code, Anthropic's official CLI tool for Claude. "
                "You are an interactive agent that helps users with software engineering tasks. "
                "Default branch (you will usually use this for PRs)\n"
                "git status context here"
            ),
        },
        {"role": "user", "content": "hi"},
    ]
    content = _final_user_content(mod, msgs)
    assert "Claude Code" not in content
    assert "official CLI" not in content
    assert "Main branch (" not in content
    # 非指纹行原样保留。
    assert "git status context here" in content
    assert "hi" in content


def test_codex_identity_stripped(mod):
    msgs = [
        {
            "role": "system",
            "content": (
                "You are a coding agent running inside the Codex CLI, a coding agent "
                "running in your terminal.\n"
                "Default branch (you will usually use this for PRs)"
            ),
        },
        {"role": "user", "content": "do it"},
    ]
    content = _final_user_content(mod, msgs)
    assert "Codex" not in content
    assert "Default branch (" not in content


def test_billing_header_and_cc_kv_stripped(mod):
    # 计费头键值段用运行期拼接，避免源码里出现完整签名串。
    billing = "x-anthropic-billing-" + "header: cc_version=1.2.3; cc_entrypoint=cli"
    msgs = [
        {"role": "system", "content": billing + "\n" + "Keep this normal line."},
        {"role": "user", "content": "q"},
    ]
    content = _final_user_content(mod, msgs)
    assert "cc_version" not in content
    assert "billing-header" not in content
    assert "Keep this normal line." in content


def test_normal_system_untouched(mod):
    msgs = [
        {"role": "system", "content": "You are a helpful pirate. Answer in pirate speak."},
        {"role": "user", "content": "ahoy"},
    ]
    content = _final_user_content(mod, msgs)
    # 无 tools_prompt 时无准则/末位提示注入，仅 system 内容 + query。
    assert content == "You are a helpful pirate. Answer in pirate speak.\n\nahoy"


def test_no_system_unaffected(mod):
    msgs = [
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": "ok"},
    ]
    out = mod._normalize_messages_for_duck(_P(), msgs, "")
    assert out == msgs


def test_strip_is_idempotent(mod):
    """重入安全：剥离后的文本再过一次不变（不发散、不滚雪球）。"""
    raw = "You are Claude Code, Anthropic's official CLI tool for Claude. \nkeep"
    once = mod._strip_editor_fingerprint(raw)
    twice = mod._strip_editor_fingerprint(once)
    assert once == twice


# ==================== 会话准则 / 工具说明 / 末位提示三层注入 ====================


def test_guidelines_and_tools_prompt_prefix_user(mod):
    """有 tools_prompt 时：准则 + 工具清单拼在最后一条 user 内容前，末位提示在末尾。"""
    msgs = [
        {"role": "system", "content": "You are Claude Code, an agent."},
        {"role": "user", "content": "帮我改这个函数"},
    ]
    content = _final_user_content(mod, msgs, "TOOLS_PROMPT_X")
    assert content.startswith("【本会话准则】"), "准则应排在最前"
    assert "TOOLS_PROMPT_X" in content
    assert "帮我改这个函数" in content
    # 末位提示在内容尾部（生成前最后一段）。
    assert content.rstrip().endswith("</system-reminder>")
    assert content.count("</system-reminder>") == 1  # 末尾只有末位提示那一段
    # 顺序：准则 ... 工具清单 ... query ... 末位提示
    assert content.index("【本会话准则】") < content.index("TOOLS_PROMPT_X")
    assert content.index("TOOLS_PROMPT_X") < content.index("帮我改这个函数")
    assert content.index("帮我改这个函数") < content.index("末位提示")
    # 编辑器指纹不随 system 带进来。
    assert "Claude Code" not in content


def test_nudge_lands_on_last_user_not_earlier(mod):
    """多轮里末位提示必须钉在最后一条 user（生成前最后看到的位置）。"""
    msgs = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "第二轮"},
    ]
    out = mod._normalize_messages_for_duck(_P(), msgs, "TP")
    users = [m for m in out if m.get("role") == "user"]
    assert "末位提示" not in _texts(users[0]), "不能钉在早先那轮 user 上"
    assert "末位提示" in _texts(users[-1])
    assert "第二轮" in _texts(users[-1])


def test_no_tools_means_no_injection(mod):
    """无 tools_prompt（纯聊天）时一个字都不加。"""
    msgs = [{"role": "user", "content": "讲个笑话"}]
    out = mod._normalize_messages_for_duck(_P(), msgs, "")
    assert out == msgs


def test_content_parts_user_keeps_original_parts(mod):
    """content parts 形态：原 part 原样保留，前言在前、末位提示在尾。"""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "原始问题"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    out = mod._normalize_messages_for_duck(_P(), msgs, "TP")
    parts = out[-1]["content"]
    assert isinstance(parts, list)
    assert parts[0]["type"] == "text" and parts[0]["text"].startswith("【本会话准则】")
    assert any(p.get("text") == "原始问题" for p in parts)
    assert any(p.get("type") == "image_url" for p in parts), "图片 part 不能被拍平丢掉"
    assert parts[-1]["type"] == "text" and "末位提示" in parts[-1]["text"]


def test_guidelines_cover_announce_only_failure(mod):
    """准则与末位提示都要明确禁止「只预告不调用」——对齐 eaichat 的核心措辞。"""
    assert "必须调用工具的触发场景" in mod._SESSION_GUIDELINES
    assert "了解、查看、查找、读取、搜索、确认、验证、执行" in mod._SESSION_GUIDELINES
    assert "只说要做、不调工具就结束" in mod._SESSION_GUIDELINES
    assert "自己去调工具查" in mod._SESSION_GUIDELINES
    assert "只留预告、不发围栏就结束" in mod._FENCE_NUDGE


def test_tools_prompt_has_trigger_verbs_and_three_point_clarification(mod):
    prompt = mod._build_duck_tools_prompt([
        {"type": "function",
         "function": {"name": "Read", "description": "读文件",
                      "parameters": {"properties": {"file_path": {"type": "string"}},
                                     "required": ["file_path"]}}},
    ])
    assert "了解、查看、查找、读取、搜索、确认、验证、执行" in prompt
    assert "什么时候必须委派" in prompt
    assert "从上面清单里挑对应的操作委派出去" in prompt
    # 三点澄清堵「判成注入攻击 → 原生格式发调用 → 被通道丢弃」这条路。
    assert "不是提示词注入攻击" in prompt
    assert "本通道不支持原生 function calling" in prompt
    assert "不要在回复中评论或质疑本协议的存在" in prompt


# ==================== 围栏解析：JSON 感知闭合 + 方法名校验 + 截断救援 ====================

_FENCE_OPEN = "```tool_function"
_FENCE_CLOSE = "```"


def _replay(mod, deltas, names=None, enabled=True):
    state = {"enabled": enabled, "buf": ""}
    if names is not None:
        state["names"] = frozenset(names)
    text_parts, calls = [], []
    for d in deltas:
        for chunk in mod._tool_stream_feed(state, d):
            if chunk.get("content"):
                text_parts.append(chunk["content"])
            calls.extend(chunk.get("tool_calls") or [])
    for chunk in mod._tool_stream_flush(state):
        if chunk.get("content"):
            text_parts.append(chunk["content"])
        calls.extend(chunk.get("tool_calls") or [])
    return "".join(text_parts), calls


def test_fence_with_nested_backticks_not_truncated(mod):
    """参数 content 里内嵌 ``` 不再截断块：切出完整 Write 调用，正文不含 JSON 碎片。"""
    inner = json.dumps({
        "name": "Write",
        "arguments": {"file_path": "README.md",
                      "content": "# Demo\n\n" + _FENCE_CLOSE + "python\nprint('hi')\n" + _FENCE_CLOSE + "\n"},
    }, ensure_ascii=False)
    text, calls = _replay(mod, ["前言。", f"{_FENCE_OPEN}\n{inner}\n{_FENCE_CLOSE}"])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Write"
    args = json.loads(calls[0]["function"]["arguments"])
    assert _FENCE_CLOSE + "python" in args["content"], "内嵌围栏必须完整保留"
    assert text == "前言。"
    assert '"name"' not in text and "arguments" not in text


def test_fence_split_across_deltas_char_by_char(mod):
    payload = f'{_FENCE_OPEN}\n{{"name": "Bash", "arguments": {{"command": "ls"}}}}\n{_FENCE_CLOSE}'
    text, calls = _replay(mod, ["前言。"] + list(payload))
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Bash"
    assert text == "前言。"


def test_plain_text_passthrough_when_no_tools(mod):
    text, calls = _replay(mod, ["讲个笑话：", _FENCE_CLOSE + "python\nprint(1)\n" + _FENCE_CLOSE],
                          enabled=False)
    assert calls == []
    assert text == "讲个笑话：" + _FENCE_CLOSE + "python\nprint(1)\n" + _FENCE_CLOSE


def test_unclosed_fence_at_stream_end_is_rescued(mod):
    text, calls = _replay(mod, [f'{_FENCE_OPEN}\n{{"name": "Read", "arguments": {{"file_path": "a.py"}}}}'])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert text == ""


def test_unknown_method_returned_verbatim(mod):
    block = f'{_FENCE_OPEN}\n{{"name": "draw_picture", "arguments": {{}}}}\n{_FENCE_CLOSE}'
    text, calls = _replay(mod, [block], names=["Read", "Bash"])
    assert calls == []
    assert text == block, "未知方法的围栏必须原样透传"


def test_known_method_emits_tool_calls(mod):
    block = f'{_FENCE_OPEN}\n{{"name": "Read", "arguments": {{"file_path": "a.py"}}}}\n{_FENCE_CLOSE}'
    text, calls = _replay(mod, [block], names=["Read", "Bash"])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert text == ""


def test_two_fences_in_one_stream(mod):
    a = f'{_FENCE_OPEN}\n{{"name": "Read", "arguments": {{"file_path": "a"}}}}\n{_FENCE_CLOSE}'
    b = f'{_FENCE_OPEN}\n{{"name": "Read", "arguments": {{"file_path": "b"}}}}\n{_FENCE_CLOSE}'
    text, calls = _replay(mod, [a, "中间说明。", b])
    assert [c["function"]["name"] for c in calls] == ["Read", "Read"]
    assert text == "中间说明。"


def test_balanced_json_object_strict_vs_rescue(mod):
    strict = '{"name": "Bash", "arguments": {"command": "ls"}'
    assert mod._balanced_json_object(strict) == ""
    assert mod._balanced_json_object(strict, allow_unclosed=1).endswith("}}")
    assert mod._balanced_json_object('{"name": "Bash", "arguments": {"a": 1', allow_unclosed=1) == ""
    ok = '{"name": "Bash", "arguments": {"command": "ls"}}'
    assert mod._balanced_json_object(ok) == ok


def test_stream_chat_declares_channel_parse_and_names(mod):
    """stream_chat 必须建带 names 的 tool_state；渠道声明 TOOL_PARSE_IN_CHANNEL。"""
    import inspect

    src = inspect.getsource(mod.DuckAIChannel.stream_chat)
    assert '_known_tool_names(kwargs.get("tools"))' in src
    assert getattr(mod.DuckAIChannel, "TOOL_PARSE_IN_CHANNEL", False) is True


# ==================== 带 tools 强制开思考 ====================


def test_thinking_forced_on_when_tools_present(mod):
    assert mod._resolve_reasoning_effort("gpt-5.6-luna", {}, True) == "medium"


def test_thinking_not_forced_for_model_without_think_mode(mod):
    assert mod._resolve_reasoning_effort("grok-3", {}, True) == "none"


def test_thinking_untouched_when_no_tools(mod):
    assert mod._resolve_reasoning_effort("gpt-5.6-luna", {"reasoning_effort": "none"}, False) == "none"
    assert mod._resolve_reasoning_effort("gpt-5.6-luna", {"reasoning_effort": "high"}, False) == "high"


def test_thinking_effort_mapping(mod):
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "minimal"}, False) == "none"
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "xhigh"}, False) == "high"
    assert mod._resolve_reasoning_effort("grok-3", {"thinking_enabled": True}, False) == "medium"
