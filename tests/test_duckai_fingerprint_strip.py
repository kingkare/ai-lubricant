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
8. 带 tools 强制开思考；
9. 自动反爬头（对齐 Duck2api）：journey/signals 自动生成、账号字段优先覆盖、
   fe_version 首页指纹抓取/缓存/失败不抛、headers 钩子保持同步；
10. fetch_models 动态列表解析 + 缓存 + 失败回落内置兜底表。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import importlib.util
import json
import time
from base64 import b64decode
from pathlib import Path

import pytest
from fastapi import HTTPException

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
    assert content.index("帮我改这个函数") < content.index("（生成前最后必读）")
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
    assert "（生成前最后必读）" not in _texts(users[0]), "不能钉在早先那轮 user 上"
    assert "（生成前最后必读）" in _texts(users[-1])
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
    assert parts[-1]["type"] == "text" and "（生成前最后必读）" in parts[-1]["text"]


def test_guidelines_cover_announce_only_failure(mod):
    """「只预告不调用 / 凭记忆编造」的措辞必须在提示词里出现一次（去重后只剩 tools_prompt 那份）。

    2026-10-14 去重：这些文案原先在 _SESSION_GUIDELINES / tools_prompt / 末位块三处重复，
    现在只保留 tools_prompt 那一份 + 末位块的任务锚点与禁搜索。此测试锁 prompt 侧那份。
    """
    prompt = mod._build_duck_tools_prompt([
        {"type": "function",
         "function": {"name": "Bash", "description": "Run a shell command",
                      "parameters": {"properties": {"command": {"type": "string"}},
                                     "required": ["command"]}}},
    ])
    assert "了解、查看、查找、读取、搜索、确认、验证、执行" in prompt
    assert "自己去调工具查" in prompt
    # 反编造条款：实时外部数据必须靠工具查，不许凭记忆作答。
    # （措辞在 tools_prompt 侧是「你没有实时外部数据 / 凭印象给出它们，就是编造」；
    #   原准则里的标题「不许凭记忆编造外部事实」已随去重删除。）
    assert "你没有实时外部数据" in prompt
    assert "凭印象给出它们，就是编造" in prompt
    # 末位块：命令必须回工具调用 + 禁原生搜索（用户 2026-10-14 实测定稿的措辞）。
    tail = mod._build_tail_directive("查一下今天天气", after_tool_result=False)
    assert "查一下今天天气" in tail
    assert "本轮你需要回复一个工具调用的 json" in tail
    assert "不要搜索网页" in tail


def test_tail_directive_first_turn_vs_after_tool(mod):
    """末位指令分首轮/续轮两支：首轮命令必须发调用，续轮钉「够了收尾 / 不够继续调」。"""
    first = mod._build_tail_directive("查一下今天天气", after_tool_result=False)
    assert "本轮你需要回复一个工具调用的 json" in first
    assert "不要搜索网页" in first
    assert "工具结果已经够回答用户了" not in first

    cont = mod._build_tail_directive("查一下今天天气", after_tool_result=True)
    assert "工具结果已经够回答用户了" in cont
    assert "还缺信息或操作" in cont
    # 两支都带任务锚点。
    assert "查一下今天天气" in first and "查一下今天天气" in cont


def test_tail_directive_lands_last_user_with_tool_history(mod):
    """有 role=tool 历史时走续轮分支，末位指令仍钉在最后一条 user 上。"""
    msgs = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "25度晴"},
    ]
    out = mod._normalize_messages_for_duck(_P(), msgs, "TP")
    users = [m for m in out if m.get("role") == "user"]
    assert "工具结果已经够回答用户了" in _texts(users[-1])
    assert "查天气" in _texts(users[-1])  # 任务锚点取原始第一条 user


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


def test_tools_prompt_asserts_access_not_denies(mod):
    """工具说明**断言**环境具备这些操作，绝不写「你不需要真的拥有它们」这类否认措辞。

    2026-10-13 真机实测：旧措辞（「下面这些操作由客户端具备，你不需要真的拥有它们……
    不是要求你自行执行」）被消费级模型读成「我没有这个能力」→ 回「我无法访问你的设备
    /文件」整轮拒答，一个围栏都不发。互斥交替 A/B：旧措辞 0/8、断言式 8/8。此测试锁死
    新措辞，防止有人「优化」回否认式表达。
    """
    prompt = mod._build_duck_tools_prompt([
        {"type": "function",
         "function": {"name": "Bash", "description": "Run a shell command",
                      "parameters": {"properties": {"command": {"type": "string"}},
                                     "required": ["command"]}}},
    ])
    # 断言式：环境具备该能力，明确叫停「我没有能力」这类拒答。
    assert "运行环境**具备**" in prompt
    assert "没有这个能力" in prompt
    # 旧否认式措辞必须彻底消失（就是它触发的拒答）。
    assert "你不需要真的拥有" not in prompt
    assert "不是要求你自行执行" not in prompt


def test_tail_directive_has_completion_anchor(mod):
    """末位块保持精简：只带任务锚点 + 禁搜索 + 格式示例，不叠"续写下一段"类马甲。

    2026-10-14 用户纠正：末位原先堆了「请把上面的调用块当作你要续写的下一段输出，直接
    写出来，不要评论、不要声明能力」这类重复施压马甲，同一意图在准则/工具协议/末位说了
    三遍。现只留一句话级别的必要内容。**风险**：该锚点曾与「补全式框架 0/8→8/8」同时出现，
    去掉后若真实渠道回归拒答，需要把它单独加回来（用真机 A/B 判，不靠推测）。
    """
    tail = mod._build_tail_directive("列出文件", after_tool_result=False)
    assert "列出文件" in tail
    assert "本轮你需要回复一个工具调用的 json" in tail
    assert "续写的下一段输出" not in tail
    assert tail.rstrip().endswith("</system-reminder>")


def test_think_models_include_served_model(mod):
    """_DUCK_THINK_MODELS 必须含线上实际服务的 gpt-6-luna——旧集合只有过期的
    gpt-5.6-luna，导致「带 tools 强制开思考」恒不触发（死代码）。"""
    assert "gpt-6-luna" in mod._DUCK_THINK_MODELS
    assert mod._resolve_reasoning_effort("gpt-6-luna", {}, True) == "low"


# ==================== 围栏解析：JSON 感知闭合 + 方法名校验 + 截断救援 ====================

_FENCE_OPEN = "```tool_function"
_FENCE_CLOSE = "```"


def _replay(mod, deltas, names=None, enabled=True, tools=None):
    state = {"enabled": enabled, "buf": ""}
    if names is not None:
        state["names"] = frozenset(names)
    if tools is not None:
        state["names"] = mod._known_tool_names(tools)
        state["schema"] = mod._tool_schema_index(tools)
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


# ==================== 工具调用后处理（移植 chatgpt-web 桥四项零延迟校验）====================

_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
            "required": ["command"],
        },
    },
}
_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "Write",
        "description": "write a file",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file_path", "content"],
        },
    },
}
_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "Edit",
        "description": "edit a file",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"},
                           "old_string": {"type": "string"}, "new_string": {"type": "string"}},
            "required": ["file_path", "old_string", "new_string"],
        },
    },
}
_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "read a file",
        "parameters": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
}


def _fence(name, args):
    body = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
    return f"{_FENCE_OPEN}\n{body}\n{_FENCE_CLOSE}"


def test_tool_name_mapped_from_internal_name(mod):
    """模型用内部名 read_file，声明是 Read → 按包含关系映射回去，正常下发调用。"""
    text, calls = _replay(mod, [_fence("read_file", {"file_path": "a.py"})], tools=[_READ_TOOL])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert text == ""


def test_normalize_tool_name_helper(mod):
    """_normalize_tool_name：包含关系命中则归一，否则原样返回（不猜）。"""
    names = mod._known_tool_names([_READ_TOOL, _BASH_TOOL])
    assert mod._normalize_tool_name("read_file", names) == "Read"
    assert mod._normalize_tool_name("ReadFile", names) == "Read"
    assert mod._normalize_tool_name("Read", names) == "Read"
    assert mod._normalize_tool_name("draw_picture", names) == "draw_picture"


def test_tool_name_unmappable_still_passes_through(mod):
    """映射不上的名字（幻觉工具）绝不照单全收 → 整块原样透传正文。"""
    block = _fence("draw_picture", {"x": 1})
    text, calls = _replay(mod, [block], tools=[_BASH_TOOL])
    assert calls == []
    assert text == block


def test_extra_arguments_stripped(mod):
    """schema 外参数（模型顺手补的 description）剥掉，只留声明过的键。"""
    text, calls = _replay(mod, [_fence("Bash", {"command": "ls", "description": "list"})],
                          tools=[_BASH_TOOL])
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}
    assert text == ""


def test_missing_required_argument_rejected(mod):
    """缺必填参数的调用必然废（客户端整轮失败）→ 不透传 tool_calls，原样当正文。"""
    block = _fence("Bash", {"timeout": 30})   # 缺 required 的 command
    text, calls = _replay(mod, [block], tools=[_BASH_TOOL])
    assert calls == []
    assert text == block


def test_destructive_edit_rejected(mod):
    """把匹配内容替换成空（整文件抹掉）→ 判废，不透传。"""
    block = _fence("Edit", {"file_path": "a.py", "old_string": "print(1)", "new_string": ""})
    text, calls = _replay(mod, [block], tools=[_EDIT_TOOL])
    assert calls == []
    assert text == block


def test_non_destructive_edit_allowed(mod):
    """正常的小改（新内容非空）照常下发。"""
    text, calls = _replay(mod, [_fence("Edit", {"file_path": "a.py", "old_string": "1", "new_string": "2"})],
                          tools=[_EDIT_TOOL])
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Edit"


def test_write_near_empty_overwrite_rejected(mod):
    """整文件覆盖成近空内容（<40 字符且带 overwrite）→ 判废。"""
    block = _fence("Write", {"file_path": "a.py", "content": "# x", "overwrite": True})
    text, calls = _replay(mod, [block], tools=[_WRITE_TOOL])
    assert calls == []
    assert text == block


def test_finalize_calls_none_when_unusable(mod):
    """_finalize_calls：可下发返回列表，不可下发返回 None（供 flush 救援判据）。"""
    idx = mod._tool_schema_index([_BASH_TOOL])
    names = mod._known_tool_names([_BASH_TOOL])
    good = [{"type": "function", "function": {"name": "Bash", "arguments": '{"command": "ls"}'}}]
    assert mod._finalize_calls(good, names, idx) is not None
    bad = [{"type": "function", "function": {"name": "Bash", "arguments": "{}"}}]
    assert mod._finalize_calls(bad, names, idx) is None       # 缺 command
    assert mod._finalize_calls([], names, idx) is None        # 空
    unknown = [{"type": "function", "function": {"name": "Nope", "arguments": "{}"}}]
    assert mod._finalize_calls(unknown, names, idx) is None   # 名字不在清单


# ==================== stream_chat 自愈：429 日窗打满 → 判死身份 → 重收割重发 ====================
# 429 fixed_cost_window_limit 是按**这套身份**（vqd+fe-signals+journey）记的日成本窗，
# 与 TTL/uses 两维轮换无关：重型 agent 请求几十发就能打满一个身份，等不到轮换条件。
# 收到 429 必须立刻把这套头判死，重试自动重收割换新身份，而不是把 429 透传给客户端。


class _StreamP:
    """stream_chat 替身：send_sse_request 按脚本逐次抛错/吐帧；_build_request_headers
    走真缓存路径（用 _CREDS 里预置的成套头，绝不真开浏览器）。"""

    base_url = ""
    user_agent = ""
    cookie = ""
    vqd_hash = ""          # 空 = 浏览器模式（自愈可用）
    login_client_id = "brA"
    proxy = None

    def __init__(self, script):
        self.script = list(script)   # 每次调用弹一个：HTTPException 或 [(event_str), ...]
        self.calls = 0

    async def send_sse_request(self, method, url, headers, **kwargs):
        self.calls += 1
        self.last_headers = headers
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        for ev in item:
            # 列表项里夹的 Exception = 流开到一半才炸（模拟「先吐内容后 429」）
            if isinstance(ev, Exception):
                raise ev
            yield ev

    def parse_sse_type(self, event_str):
        # 框架解析器替身：data: 行剥前缀，返回 (event_type, data_dict)
        for line in event_str.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload in ("[DONE]", "[PING]"):
                    return "", None
                try:
                    return "", json.loads(payload)
                except Exception:
                    return "", None
        return "", None

    def _make_session(self):
        raise AssertionError("stream_chat 不应直连出网（走 send_sse_request）")

    @staticmethod
    def flatten_tool_history(messages):
        return messages


_QUOTA_429 = HTTPException(
    status_code=429,
    detail=json.dumps({"action": "error", "status": 429, "type": "ERR_USER_LIMIT",
                       "r": "fixed_cost_window_limit"}),
)


def _drive_stream(mod, p, **kw):
    async def _collect():
        out = []
        async for ch in mod.DuckAIChannel.stream_chat(p, "gpt-6-luna",
                                                      [{"role": "user", "content": "hi"}], **kw):
            out.append(ch)
        return out
    return _run(_collect())


@pytest.fixture(autouse=True)
def _recovery_recorder(mod, monkeypatch):
    """把恢复入队替成记录器：测试里不真跑后台 worker（会去摸浏览器），

    只记下被入队的 cid，供断言「429 是否触发了后台恢复」。
    """
    _recovery_recorder.calls = []
    _recovery_recorder.real = mod._enqueue_recovery   # worker 自测用真实现

    def _fake(p, cid):
        _recovery_recorder.calls.append(cid)

    monkeypatch.setattr(mod, "_enqueue_recovery", _fake, raising=False)
    yield


def test_stream_chat_quota_429_enqueues_recovery_and_fails_fast(mod, monkeypatch):
    """撞日窗 429 → 判死缓存 + 恢复入队 + **立即** raise（请求内不重试、不 sleep）。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "VER", "client_id": "brA", "uses": 5,
                         "expires_at": time.time() + 600}

    async def fake_get_creds(p):
        return mod._bump_creds_uses("brA")

    monkeypatch.setattr(mod, "_get_creds", fake_get_creds)

    p = _StreamP([_QUOTA_429])
    with pytest.raises(HTTPException) as ei:
        _drive_stream(mod, p)

    assert ei.value.status_code == 429
    assert p.calls == 1                                   # 只发一次：请求内不重试
    assert mod._CREDS["brA"]["expires_at"] == 0.0         # 缓存被判死
    assert not mod._creds_valid(mod._CREDS["brA"])
    assert _recovery_recorder.calls == ["brA"]            # 恢复任务已入队（后台刷窗）


def test_stream_chat_non_quota_429_passes_through_no_recovery(mod, monkeypatch):
    """普通 429（非日窗）→ 不判死、不入队、原样透传，单次调用。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "VER", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + 600}

    async def fake_get_creds(p):
        return mod._bump_creds_uses("brA")

    monkeypatch.setattr(mod, "_get_creds", fake_get_creds)

    p1 = _StreamP([HTTPException(status_code=429, detail="some other limit")])
    with pytest.raises(HTTPException) as ei:
        _drive_stream(mod, p1)
    assert ei.value.status_code == 429 and p1.calls == 1
    assert _recovery_recorder.calls == []
    assert mod._creds_valid(mod._CREDS["brA"])            # 没判死（非日窗）


def test_stream_chat_429_after_output_still_enqueues_no_replay(mod, monkeypatch):
    """已吐过内容才撞 429 → 不重放（不重复输出），判死 + 入队 + 如实透传。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "VER", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + 600}

    async def fake_get_creds(p):
        return mod._bump_creds_uses("brA")

    monkeypatch.setattr(mod, "_get_creds", fake_get_creds)

    events = ['data: {"action":"success","message":"先吐的正文"}\n\n', _QUOTA_429]
    p = _StreamP([events])
    with pytest.raises(HTTPException) as ei:
        _drive_stream(mod, p)
    assert ei.value.status_code == 429 and p.calls == 1
    assert _recovery_recorder.calls == ["brA"]            # 吐过也入队（为下一发恢复）


def test_stream_chat_401_invalidates_no_retry(mod, monkeypatch):
    """头失效 401 → 作废缓存 + 归一 401，请求内不重试（下次请求重取头）。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "VER", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + 600}

    async def fake_get_creds(p):
        return mod._bump_creds_uses("brA")

    monkeypatch.setattr(mod, "_get_creds", fake_get_creds)

    p = _StreamP([HTTPException(status_code=401, detail="unauthorized")])
    with pytest.raises(HTTPException) as ei:
        _drive_stream(mod, p)
    assert ei.value.status_code == 401 and p.calls == 1   # 不重试
    assert "brA" not in mod._CREDS                        # 缓存已作废


def test_recovery_worker_drains_queue_and_harvests(mod, monkeypatch):
    """恢复 worker：入队的 cid 会被取出 → 调 _refresh_creds_now 刷窗；重复 cid 去重。"""
    import asyncio as _asyncio
    _clear_creds(mod)
    mod._RECOVERY_PENDING.clear()
    enqueue = _recovery_recorder.real   # 用真实现（autouse fixture 替成了记录器）

    calls = []

    async def fake_refresh(p):
        calls.append("refresh")
        return {"vqd": "NEW"}

    monkeypatch.setattr(mod, "_refresh_creds_now", fake_refresh)

    async def _scenario():
        q = mod._ensure_recovery_worker()
        enqueue(object(), "brA")
        enqueue(object(), "brA")   # 重复 → 去重，只排一个
        await _asyncio.wait_for(q.join(), timeout=2)

    _run(_scenario())
    assert calls == ["refresh"]                  # 去重后只刷一次
    assert "brA" not in mod._RECOVERY_PENDING    # 处理完摘除


def test_recovery_worker_skips_when_creds_became_fresh(mod, monkeypatch):
    """入队后若缓存已被别的路径修好（变新鲜）→ worker 跳过，不再白付一次收割。"""
    import asyncio as _asyncio
    _clear_creds(mod)
    mod._RECOVERY_PENDING.clear()
    enqueue = _recovery_recorder.real
    mod._CREDS["brA"] = {"vqd": "FRESH", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "VER", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + 600}

    async def fake_refresh(p):
        raise AssertionError("缓存已新鲜，不该再收割")

    monkeypatch.setattr(mod, "_refresh_creds_now", fake_refresh)

    async def _scenario():
        q = mod._ensure_recovery_worker()
        enqueue(object(), "brA")
        await _asyncio.wait_for(q.join(), timeout=2)

    _run(_scenario())


def test_scheduled_harvest_runs_through_same_queue(mod, monkeypatch):
    """定时 refresh_headers 排进**同一条队列**（kind=scheduled），worker 执行并回徽标。"""
    import asyncio as _asyncio
    _clear_creds(mod)
    mod._RECOVERY_PENDING.clear()
    calls = []

    async def fake_refresh(p):
        calls.append("refresh")
        return {"vqd": "NEW", "expires_at": time.time() + 600}

    monkeypatch.setattr(mod, "_refresh_creds_now", fake_refresh)

    async def _scenario():
        p = _BrowserP(login_client_id="brA")  # 无缓存 → 不触发 grace 跳过 → 真刷窗
        # _enqueue_scheduled_harvest 需要运行中的 loop 建 future，故整段在协程里跑。
        return await mod._enqueue_scheduled_harvest(p)

    out = _run(_scenario())
    assert calls == ["refresh"]                       # 走了队列、真刷窗
    assert "取头" in out["last_headers_refresh"]       # 徽标字典原样回给 perform_action


def test_scheduled_harvest_grace_skip_returns_empty(mod, monkeypatch):
    """定时任务：头还新（两维都够）→ worker 里判 grace 跳过，返回 {}，不刷窗。"""
    _clear_creds(mod)
    mod._RECOVERY_PENDING.clear()
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + mod._CREDS_REFRESH_GRACE_SECONDS + 300}

    async def fake_refresh(p):
        raise AssertionError("头还新，不该刷窗")

    monkeypatch.setattr(mod, "_refresh_creds_now", fake_refresh)

    async def _scenario():
        return await mod._enqueue_scheduled_harvest(_BrowserP(login_client_id="brA"))

    assert _run(_scenario()) == {}


def test_scheduled_harvest_does_not_clobber_recover_dedup(mod, monkeypatch):
    """scheduled 任务不占/不摘 _RECOVERY_PENDING——同 cid 排着的 recover 不被误摘。"""
    import asyncio as _asyncio
    _clear_creds(mod)
    mod._RECOVERY_PENDING.clear()
    enqueue = _recovery_recorder.real
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + mod._CREDS_REFRESH_GRACE_SECONDS + 300}

    async def fake_refresh(p):
        return {"vqd": "NEW", "expires_at": time.time() + 600}

    monkeypatch.setattr(mod, "_refresh_creds_now", fake_refresh)

    async def _scenario():
        q = mod._ensure_recovery_worker()
        enqueue(object(), "brA")                      # recover 先入队（占 _RECOVERY_PENDING）
        assert "brA" in mod._RECOVERY_PENDING
        await mod._enqueue_scheduled_harvest(_BrowserP(login_client_id="brA"))  # scheduled 排它后面
        await _asyncio.wait_for(q.join(), timeout=2)

    _run(_scenario())
    assert "brA" not in mod._RECOVERY_PENDING         # recover 处理完自己摘的，不是 scheduled 误摘


# ==================== 围栏解析对齐 eaichat：真实帧 fixture 全量回放 ====================
# 上一轮把 eaichat 的成熟解析器整套移植进 duckai（bare/markerless/XML/多调用/接缝/
# 吞反引号/值内裸引号）。eaichat 的 fixtures/ 是现网抓下来的真实坏帧，直接灌进 duckai
# 的解析器做全量断言——比手写单测更守得住，移植面一改就红。specs/ 自持、缺目录跳过。

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "specs" / "fixtures" / "eaichat"


def _fixture_files():
    if not _FIXTURE_DIR.exists():
        return []
    out = []
    for p in sorted(_FIXTURE_DIR.glob("*.json")):
        try:
            fx = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if fx.get("protocol") != "anthropic":
            out.append(p)
    return out


def _assert_arguments(args_obj: dict, spec: dict):
    for k, v in (spec.get("arguments_equals") or {}).items():
        assert args_obj.get(k) == v, f"参数 {k}={args_obj.get(k)!r} 期望 {v!r}"
    ec = spec.get("arguments_contains")
    if ec:
        blob = json.dumps(args_obj, ensure_ascii=False)
        assert ec in blob, f"参数不含 {ec!r}: {blob[:200]}"
    ek = spec.get("arguments_keys")
    if ek:
        assert set(args_obj) == set(ek), f"参数键 {sorted(args_obj)} 期望 {sorted(ek)}"


@pytest.mark.parametrize("fx_path", _fixture_files(), ids=lambda p: p.stem)
def test_eaichat_fixture_replays_through_duckai(mod, fx_path):
    fx = json.loads(fx_path.read_text(encoding="utf-8"))
    exp = fx.get("expect", {})
    frames = [d["content"] for d in fx.get("deltas", []) if d.get("content")]
    names = tuple(fx.get("tools") or ()) or ("Grep", "Bash", "Read", "Write", "Edit")
    text, calls = _replay(mod, frames, names=names)

    want = exp.get("tool_calls") or []
    assert len(calls) == len(want), (
        f"{fx_path.name}: 期望 {len(want)} 个调用，实际 {len(calls)}；正文={text[:120]!r}")
    for got, spec in zip(calls, want):
        fn = got.get("function") or {}
        assert fn.get("name") == spec["name"], f"{fn.get('name')} != {spec['name']}"
        _assert_arguments(json.loads(fn.get("arguments") or "{}"), spec)

    if "text" in exp:
        assert text == exp["text"], f"{fx_path.name}: 正文 {text!r} 期望 {exp['text']!r}"
    if exp.get("text_contains"):
        assert exp["text_contains"] in text, f"{fx_path.name}: 正文缺 {exp['text_contains']!r}"
    # 切成功的调用，其参数内容绝不能又漏成正文（半截 JSON 垃圾）。
    for spec in want:
        ec = spec.get("arguments_contains")
        if ec:
            assert ec not in text, f"{fx_path.name}: 参数内容泄漏成正文 {ec!r}"


# ==================== 移植原语的定向单测（fixture 之外，表意更直白）====================


def test_bare_open_marker_without_backticks(mod):
    """网关吞掉开标记前三个反引号 → 实发 'tool_function<LF>{...}<LF>```'，仍须切出调用。"""
    block = 'tool_function\n{"name": "Grep", "arguments": {"pattern": "x"}}\n```'
    text, calls = _replay(mod, [block], names=["Grep"])
    assert len(calls) == 1 and calls[0]["function"]["name"] == "Grep"
    assert "tool_function" not in text and '"name"' not in text


def test_bare_open_marker_only_function_tail(mod):
    """更狠一档：连 'tool' 都被吞，只剩 '_function{...}'。"""
    block = '_function{"name": "Bash", "arguments": {"command": "ls"}}\n```'
    text, calls = _replay(mod, [block], names=["Bash"])
    assert len(calls) == 1 and calls[0]["function"]["name"] == "Bash"
    assert "_function" not in text


def test_markerless_only_json_and_close(mod):
    """开标记整丢，只剩裸 JSON + 闭合围栏 → 启发式救援成调用。"""
    block = '{"name": "Read", "arguments": {"file_path": "a.py"}}\n```'
    text, calls = _replay(mod, [block], names=["Read"])
    assert len(calls) == 1 and calls[0]["function"]["name"] == "Read"
    assert '"name"' not in text


def test_multi_calls_in_one_block(mod):
    """一个块里并排多个 {...}：全部切出（旧实现只取第一个，第二个漏成正文）。"""
    block = ('```tool_function\n'
             '{"name": "Read", "arguments": {"file_path": "a"}}\n'
             '{"name": "Glob", "arguments": {"pattern": "**/.env"}}\n```')
    text, calls = _replay(mod, [block], names=["Read", "Glob"])
    assert [c["function"]["name"] for c in calls] == ["Read", "Glob"]
    assert text == ""


def test_missing_outer_brace_rescued(mod):
    """块内 JSON 外层少一个 }（deepseek 系实测）：闭标到位即接缝救援补回，不留半截正文。"""
    block = '```tool_function\n{"name": "Bash", "arguments": {"command": "ls"}\n```'
    text, calls = _replay(mod, [block], names=["Bash"])
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"])["command"] == "ls"
    assert '"name"' not in text


def test_stray_quote_after_number(mod):
    """数字后多一个裸引号（"offset": 788"}}）：_repair_stray_quotes 救回合法 JSON。"""
    block = ('```tool_function\n{"name": "Read", "arguments": {"file_path": "t.py", '
             '"limit": 60, "offset": 788"}}\n```')
    text, calls = _replay(mod, [block], names=["Read"])
    assert len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["offset"] == 788 and args["file_path"] == "t.py"


def test_adjacent_fences_join_backticks_absorbed(mod):
    """两块紧挨着（闭标+开标同一串 6 反引号）：两块都切出、正文不留孤立反引号碎片。"""
    a = '{"name": "Grep", "arguments": {"pattern": "one"}}'
    b = '{"name": "Grep", "arguments": {"pattern": "two"}}'
    block = f"```tool_function\n{a}\n``````tool_function\n{b}\n```"
    text, calls = _replay(mod, [block], names=["Grep"])
    assert len(calls) == 2
    assert [json.loads(c["function"]["arguments"])["pattern"] for c in calls] == ["one", "two"]
    assert "`" not in text and "tool_function" not in text


def test_xml_invoke_form_parsed(mod):
    """模型按 XML 标签形态发调用（deepseek/glm 系）→ 切出调用与参数。

    标签串用拼接构造（不写字面 </>），避免被内容审查当成真标签。
    """
    block = "".join([
        "<", "tool_calls>",
        "<", 'invoke name="Read">',
        "<", "parameter name=", '"file_path">', "a.py",
        "<", "/parameter>", "<", "/invoke>",
        "<", "/tool_calls>",
    ])
    text, calls = _replay(mod, [block], names=["Read"])
    assert len(calls) == 1 and calls[0]["function"]["name"] == "Read"
    assert json.loads(calls[0]["function"]["arguments"])["file_path"] == "a.py"
    assert "invoke" not in text


def test_xml_wrong_tag_name_stays_text(mod):
    """只认 <tool_calls> 这对标签；<tool_function> 形态不是我们的协议 → 原样透传正文。"""
    block = "".join([
        "<", "tool_function>\n",
        '{"name": "Read", "arguments": {"file_path": "a.py"}}\n',
        "<", "/tool_function>",
    ])
    text, calls = _replay(mod, [block], names=["Read"])
    assert calls == []
    assert "tool_function" in text


# ==================== 带 tools 强制开思考 ====================
# duck.ai 上游 reasoningEffort 只支持 none/low 两档（Duck2api capReasoningEffort 佐证），
# medium/high 会被 schema 拒收（400 ERR_BAD_REQUEST）。


def test_thinking_forced_on_when_tools_present(mod):
    assert mod._resolve_reasoning_effort("gpt-5.6-luna", {}, True) == "low"


def test_thinking_forced_even_when_client_says_none(mod):
    """带 tools 强制开推理（治预告句失败），客户端传 none 也覆盖。"""
    assert mod._resolve_reasoning_effort("gpt-5.6-luna", {"reasoning_effort": "none"}, True) == "low"


def test_thinking_not_forced_for_model_without_think_mode(mod):
    assert mod._resolve_reasoning_effort("grok-3", {}, True) == "none"


def test_thinking_untouched_when_no_tools(mod):
    assert mod._resolve_reasoning_effort("gpt-5.6-luna", {"reasoning_effort": "none"}, False) == "none"


def test_thinking_effort_capped_to_low(mod):
    """medium/high/xhigh 一律压到 low——上游只认 none/low，透传会 400。"""
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "low"}, False) == "low"
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "medium"}, False) == "low"
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "high"}, False) == "low"
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "xhigh"}, False) == "low"
    assert mod._resolve_reasoning_effort("grok-3", {"reasoning_effort": "minimal"}, False) == "none"
    assert mod._resolve_reasoning_effort("grok-3", {"thinking_enabled": True}, False) == "low"


# ==================== 原生工具门控：toolChoice 按 tools 有无开合 ====================
#
# 2026-10-13 真机实测根因：用户问「查天气」，duck.ai 的上游服务端带**原生** WebSearch
# 工具直接作答（答案里 <citation src="N"> 就是上游搜索引用），围栏协议被整体旁路——
# 「工具不被调用」。文件/shell 类能出围栏，正因为上游无对应原生能力。
# 修法：带 tools（agent 模式）时按真实 supportedTools 名单逐个关断（WebSearch/.../
# GenerateImage 全 False），把上游搜索这条旁路掐掉，模型只剩围栏一条路；不带 tools
# 时不关——纯问答用户问天气该得答案，不能把上游唯一好行为也砸了。

class _PP:
    @staticmethod
    def flatten_tool_history(msgs):
        return msgs


def test_toolchoice_disables_native_web_search_when_tools_present(mod):
    """带 tools → metadata.toolChoice.WebSearch=False + 顶层 canUseTools=False。

    两条都试过：toolChoice.WebSearch:false 与 canUseWebSearch:false **都关不掉原生搜索**
    （现网带 tools 请求仍返回 <citation>）。toolChoice 是「模型能力」不是关断开关
    （supportedTools 里 WebSearch 恒在）。故再加 canUseTools=False 掐掉上游服务端工具。
    """
    tools = [{"type": "function", "function": {"name": "web_search",
              "parameters": {"properties": {"q": {"type": "string"}}, "required": ["q"]}}}]
    body = mod._build_chat_payload(_PP(), "gpt-6-luna",
                                   [{"role": "user", "content": "查天气"}], {"tools": tools})
    assert body["metadata"]["toolChoice"] == {"WebSearch": False}
    assert body["canUseWebSearch"] is False          # 顶层同意标记仍发（对齐真实客户端）
    assert body["canUseTools"] is False              # 带 tools 掐掉上游服务端工具


def test_canuse_tools_true_when_no_tools(mod):
    """不带 tools → canUseTools=True（保留上游正常能力）。"""
    body = mod._build_chat_payload(_PP(), "gpt-6-luna",
                                   [{"role": "user", "content": "你好"}], {"tools": []})
    assert body["canUseTools"] is True


def test_toolchoice_absent_when_no_tools(mod):
    """不带 tools → 不写 toolChoice（原生搜索保持开，纯问答能答实时）。"""
    body = mod._build_chat_payload(_PP(), "gpt-6-luna",
                                   [{"role": "user", "content": "查天气"}], {"tools": []})
    assert "toolChoice" not in body["metadata"]
    assert body["canUseWebSearch"] is True


def test_toolchoice_image_path_sets_generateimage_true(mod):
    """图片生成虚拟模型 gpt-image-2：toolChoice.GenerateImage=True（图片路径要开）。"""
    tools = [{"type": "function", "function": {"name": "web_search",
              "parameters": {"properties": {"q": {"type": "string"}}}}}]
    body = mod._build_chat_payload(_PP(), "gpt-image-2",
                                   [{"role": "user", "content": "画只猫"}], {"tools": tools})
    assert body["metadata"]["toolChoice"].get("GenerateImage") is True
    assert body["model"] == "gpt-6-luna"  # 图片虚拟模型重映射到编排模型（2026-10-15：旧 gpt-5.6-luna 已从上游消失）


def test_image_path_merges_not_clobbers_websearch(mod):
    """图片路径带 tools 时：toolChoice 合并，WebSearch:false 不被 GenerateImage 抹掉。"""
    tools = [{"type": "function", "function": {"name": "web_search",
              "parameters": {"properties": {"q": {"type": "string"}}}}}]
    body = mod._build_chat_payload(_PP(), "gpt-image-2",
                                   [{"role": "user", "content": "画只猫"}], {"tools": tools})
    tc = body["metadata"]["toolChoice"]
    assert tc.get("GenerateImage") is True
    assert tc.get("WebSearch") is False   # 没被覆盖掉


def test_customization_instructions_injected_into_metadata(mod):
    """metadata.customization.additionalInstructions 注入（服务端读的指令通道）。

    放的是**完整系统提示词**（会话准则 + 工具协议），不是一句缩写指令。bundle 证据：
    CustomizationMetadata schema 明说 free-text instructions；客户端把 customization 原样
    放进 metadata（metadata:{...N, customization:kt.current}），请求体
    Me=z({model, metadata:c, messages:[...]})。duck.ai 不收 role:system，这条通道带 system
    语义且不触发 injection 审查。
    """
    tools = [{"type": "function", "function": {"name": "web_search",
              "parameters": {"properties": {"q": {"type": "string"}}, "required": ["q"]}}}]
    body = mod._build_chat_payload(_PP(), "gpt-6-luna",
                                   [{"role": "user", "content": "查天气"}], {"tools": tools})
    cust = body["metadata"]["customization"]
    text = cust["additionalInstructions"]
    assert "tool_function" in text                    # 工具协议在里面（完整系统提示词）
    assert "本会话准则" in text                       # 会话准则也在
    assert text == mod._build_customization_instructions(
        _PP(), [{"role": "user", "content": "查天气"}],
        mod._build_duck_tools_prompt(tools))


def test_customization_absent_when_no_system_content(mod, monkeypatch):
    """没有任何 system 内容（无 tools、无调用方 system）→ 不发 customization 键。"""
    body = mod._build_chat_payload(_PP(), "gpt-6-luna",
                                   [{"role": "user", "content": "hi"}], {"tools": []})
    assert "customization" not in body["metadata"]


def test_customization_carries_caller_system_without_tools(mod):
    """无 tools 但有调用方 system → 剥指纹后仍走 customization 进 system 通道。"""
    body = mod._build_chat_payload(
        _PP(), "gpt-6-luna",
        [{"role": "system", "content": "You are a helpful assistant."},
         {"role": "user", "content": "hi"}],
        {"tools": []})
    cust = body["metadata"]["customization"]
    assert "helpful assistant" in cust["additionalInstructions"]



# ==================== 自动反爬头（对齐 Duck2api：三个头免粘贴） ====================


class _AutoP:
    """带全部账号字段的替身（_duck_headers 会读这些属性）。"""

    base_url = ""
    user_agent = ""
    cookie = ""

    def __init__(self, **kw):
        self.vqd_hash = kw.get("vqd_hash", "VQD")
        self.fe_signals = kw.get("fe_signals", "")
        self.fe_version = kw.get("fe_version", "")
        self.ddg_journey_id = kw.get("ddg_journey_id", "")


def test_headers_auto_fill_when_account_fields_empty(mod):
    """只填 vqd_hash：journey 随机 32hex、signals 可解的埋点 JSON、version 缺省省略。"""
    h = mod._duck_headers(_AutoP())
    assert h["x-vqd-hash-1"] == "VQD"
    assert len(h["x-ddg-journey-id"]) == 32
    payload = json.loads(b64decode(h["x-fe-signals"]))
    assert [e["name"] for e in payload["events"]] == [
        "onboarding_impression", "action", "onboarding_finish", "startNewChat_free",
    ]
    assert payload["events"][1]["trusted"] is True
    assert "x-fe-version" not in h  # 无缓存无账号字段 -> 省略（不伪造）


def test_headers_account_fields_override_auto(mod):
    """账号字段填了以账号字段为准（人工兜底通道）。"""
    h = mod._duck_headers(_AutoP(fe_signals="SIG", fe_version="VER", ddg_journey_id="J1"))
    assert h["x-fe-signals"] == "SIG"
    assert h["x-fe-version"] == "VER"
    assert h["x-ddg-journey-id"] == "J1"


def test_headers_journey_id_regenerated_per_call(mod):
    """journey id 每次请求新随机（对齐真实浏览器会话埋点）。"""
    assert mod._duck_headers(_AutoP())["x-ddg-journey-id"] != mod._duck_headers(_AutoP())["x-ddg-journey-id"]


def test_headers_hook_stays_sync(mod):
    """headers 钩子必须保持同步：框架 _adapter_headers 直接 hook(...) 不 await，
    async 会把 coroutine 当 dict 返回。"""
    out = mod.DuckAIChannel.headers(_AutoP(), {}, {})
    assert isinstance(out, dict) and out["x-vqd-hash-1"] == "VQD"


# ==================== fe_version 解析（缓存 / 抓取 / 失败不抛） ====================


class _FakeResp:
    def __init__(self, status=200, body=""):
        self.status, self._body = status, body

    async def text(self):
        return self._body


class _FakeCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _fake_session(resp):
    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def request(self, *a, **k):
            return _FakeCtx(resp)

    return _S()


class _NetP(_AutoP):
    """带出网能力与代理属性的替身（_resolve_fe_version / fetch_models 用）。"""

    proxy = None

    def __init__(self, resp, **kw):
        super().__init__(**kw)
        self._resp = resp

    def _make_session(self):
        return _fake_session(self._resp)


def test_resolve_fe_version_parses_homepage_fingerprint(mod):
    mod._fe_version_cache.update(value="", ts=0)
    p = _NetP(_FakeResp(200, '<html data-version-tag="serp_1" data-version-sha="sha_1">'))
    value = _run(mod._resolve_fe_version(p))
    assert value == "serp_1-sha_1"
    # 命中缓存后第二次取同值（不重新抓）。
    assert _run(mod._resolve_fe_version(p)) == "serp_1-sha_1"


def test_resolve_fe_version_account_field_first(mod):
    p = _NetP(_FakeResp(500, ""), fe_version="MANUAL")
    assert _run(mod._resolve_fe_version(p)) == "MANUAL"


def test_resolve_fe_version_never_raises_on_failure(mod):
    mod._fe_version_cache.update(value="", ts=0)
    p = _NetP(_FakeResp(500, "boom"))
    assert _run(mod._resolve_fe_version(p)) == ""


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ==================== fetch_models 动态列表 + 兜底 ====================


def test_fetch_models_parses_upstream_and_caches(mod):
    mod._models_cache.update(items=None, ts=0)
    body = '{"models":[{"id":"gpt-5.6-luna","provider":"openai"},{"id":"","provider":"x"}]}'
    out = _run(mod.DuckAIChannel.fetch_models(_NetP(_FakeResp(200, body))))
    # 空 id 过滤 + 虚拟图片模型恒补齐（上游 models 里没有 gpt-image-2，必须补）。
    assert [x["id"] for x in out] == ["gpt-5.6-luna", "gpt-image-2"]
    assert out[0]["owned_by"] == "openai"
    # 缓存命中：换成必失败的上游仍返回缓存列表（同样带补齐的虚拟模型）。
    cached = _run(mod.DuckAIChannel.fetch_models(_NetP(_FakeResp(500, ""))))
    assert [x["id"] for x in cached] == ["gpt-5.6-luna", "gpt-image-2"]


def test_fetch_models_upstream_never_drops_virtual_image_model(mod):
    """★ 2026-10-15 用户实测 bug ★ 上游真实 10 模型成功时，图片虚拟模型不能消失。"""
    mod._models_cache.update(items=None, ts=0)
    body = json.dumps({"models": [
        {"id": "gpt-6-luna", "provider": "openai"},
        {"id": "gpt-5.4-mini", "provider": "openai"},
    ]})
    out = _run(mod.DuckAIChannel.fetch_models(_NetP(_FakeResp(200, body))))
    ids = [x["id"] for x in out]
    assert "gpt-6-luna" in ids and "gpt-5.4-mini" in ids
    assert "gpt-image-2" in ids


def test_fetch_models_falls_back_to_builtin(mod):
    mod._models_cache.update(items=None, ts=0)
    out = _run(mod.DuckAIChannel.fetch_models(_NetP(_FakeResp(500, ""))))
    assert len(out) == len(mod._DUCK_MODELS)
    image = [x for x in out if x["id"] == "gpt-image-2"][0]
    assert image["output_modalities"] == ["image"]
    # 兜底表已按上游实测刷新，不含死 id（历史上游曾有的 o3-mini / llama / mixtral 等）。
    assert "o3-mini" not in {x["id"] for x in out}


# ==================== 账号动作 refresh_headers（每 10 分钟到绑定浏览器取头） ====================


class _BrowserP(_NetP):
    """带浏览器门的替身：记录 run_js / 开页 / 关页调用，收割走真 _harvest_headers。"""

    def __init__(self, resp=None, login_client_id="brA", **kw):
        # _AutoP 默认给 vqd_hash 塞了值（那是手工模式判据）——浏览器替身必须清空，
        # 否则 perform_action / _build_request_headers 会当手工模式直接跳过收割。
        kw.setdefault("vqd_hash", "")
        super().__init__(resp or _FakeResp(500, ""), **kw)
        self.login_client_id = login_client_id
        self.js_calls: list[str] = []
        self.opened: list[str] = []
        self.closed: list[str] = []
        self.default_set: list[str] = []
        self.found_tab = ""  # 空 = 没有现成页，需我们开（用完要关）

    async def list_clients(self):
        return [{"client_id": "brA", "connected": True}, {"client_id": "brB", "connected": True}]

    async def find_tab(self, client_id, url_contains=""):
        return self.found_tab

    async def set_default_tab(self, client_id, session_key):
        self.default_set.append(f"{client_id}:{session_key}")

    async def open_tab(self, client_id, url="", *, active=False):
        self.opened.append(client_id)
        return f"sk-{client_id}"

    async def close_tab(self, client_id, tab_id=""):
        self.closed.append(client_id)

    async def run_js(self, client_id, code, *, tab_id=""):
        self.js_calls.append(code[:40])
        if "duckaiHdr || null" in code:  # _READ_HOOK_JS
            return {"x-vqd-hash-1": "VQD-REAL", "x-fe-signals": "SIG-REAL",
                    "x-fe-version": "VER", "x-ddg-journey-id": "J", "user-agent": "UA-X"}
        if "__duckOrigFetch" in code and "window.fetch =" in code and "return {ok: true}" in code and "delete" not in code:
            return {"ok": True}  # _INSTALL_HOOK_JS
        return {"ok": True}


def _clear_creds(mod):
    mod._CREDS.clear()
    mod._REFRESH_LOCKS.clear()


def test_account_schema_declares_refresh_headers_action(mod):
    """account_schema 必须声明 10 分钟一次的 refresh_headers 动作（调度真相源=spec）。"""
    actions = {a["id"]: a for a in mod.DuckAIChannel.account_schema()["account_actions"]}
    assert "refresh_headers" in actions
    default = actions["refresh_headers"]["default"]
    assert default["enabled"] is True
    assert (default["frequency_unit"], default["frequency_value"]) == ("minutes", 10)
    # 徽标字段要在 ACCOUNT_FIELDS 里，否则热更新 hasattr 守卫会跳过写回。
    assert actions["refresh_headers"]["display"]["value_field"] == "last_headers_refresh"
    assert "last_headers_refresh" in mod.DuckAIChannel.ACCOUNT_FIELDS


def test_perform_action_harvests_for_bound_browser(mod):
    """无缓存 → 到账号绑的那台浏览器收割，返回徽标字段。"""
    _clear_creds(mod)
    p = _BrowserP(login_client_id="brA")
    out = _run(mod.DuckAIChannel.perform_action(p, "refresh_headers", {}, {}))
    assert "取头" in out["last_headers_refresh"]
    ent = mod._CREDS["brA"]
    assert ent["vqd"] == "VQD-REAL" and ent["fe_signals"] == "SIG-REAL" and ent["ua"] == "UA-X"
    assert ent["client_id"] == "brA"
    assert p.opened == ["brA"] and p.closed == ["brA"]  # 自开的页用完即关


def test_perform_action_skips_when_cache_still_fresh(mod):
    """缓存剩余寿命够 → 不去页面打字（省一次真提交）。"""
    _clear_creds(mod)
    # 余量给足（GRACE+300 而不是 +1）：跳过判据是 `expires_at - now >= GRACE`，
    # 只留 1 秒的话，机器忙/GC/异步运行器首启一慢就跨边界 → 判成过期去收割 →
    # js_calls 非空 → 偶发红（本意是「还很新」，不是「刚好卡在门槛上」）。
    mod._CREDS["brA"] = {"vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "", "client_id": "brA", "uses": 0,
                         "expires_at": time.time() + mod._CREDS_REFRESH_GRACE_SECONDS + 300}
    p = _BrowserP(login_client_id="brA")
    out = _run(mod.DuckAIChannel.perform_action(p, "refresh_headers", {}, {}))
    assert out == {}
    assert p.js_calls == [] and p.opened == []


def test_perform_action_skips_manual_mode(mod):
    """手工模式（账号粘了 vqd_hash）没有浏览器可取 → 跳过，不报错。"""
    _clear_creds(mod)
    p = _BrowserP(login_client_id="brA", vqd_hash="MANUAL")
    assert _run(mod.DuckAIChannel.perform_action(p, "refresh_headers", {}, {})) == {}
    assert p.js_calls == []


def test_perform_action_unknown_id_raises(mod):
    p = _BrowserP(login_client_id="brA")
    with pytest.raises(ValueError):
        _run(mod.DuckAIChannel.perform_action(p, "checkin", {}, {}))


def test_refresh_creds_now_bypasses_cache(mod):
    """_refresh_creds_now 必收割（不看缓存是否还新）——定时取头与失效自愈用这条。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {"vqd": "OLD", "fe_signals": "S", "ua": "U", "journey": "J",
                         "fe_version": "", "client_id": "brA",
                         "expires_at": time.time() + 9999}
    p = _BrowserP(login_client_id="brA")
    ent = _run(mod._refresh_creds_now(p))
    assert ent["vqd"] == "VQD-REAL"  # 覆盖掉 OLD
    assert mod._CREDS["brA"]["vqd"] == "VQD-REAL"


def test_get_creds_uses_only_bound_browser(mod):
    """账号=浏览器：取头只用绑定的 brB，绝不落到别的在线浏览器（混浏览器必 418）。"""
    _clear_creds(mod)
    p = _BrowserP(login_client_id="brB")
    ent = _run(mod._get_creds(p))
    assert ent["client_id"] == "brB"
    assert p.opened == ["brB"] and p.closed == ["brB"]


def test_get_creds_unbound_browser_offline_raises_502(mod):
    """绑的浏览器不在线 → 502 带原因，不静默换一台（换=换指纹）。"""
    _clear_creds(mod)
    p = _BrowserP(login_client_id="brZ")
    with pytest.raises(Exception) as ei:
        _run(mod._get_creds(p))
    assert getattr(ei.value, "status_code", None) == 502
    assert "不在线" in str(ei.value.detail)


def test_creds_rotates_on_use_cap(mod):
    """缓存头时间还新、但复用次数打满 → 必须重收割换新身份（两维取先到）。

    2026-10-13 实测：上游 fixed_cost_window 按收割来的那套身份（vqd+fe-signals+journey）
    记账——浏览器每条新消息都是新身份所以页面上一直成功，渠道复用同一套头连打
    ~100 发就把该身份打满（percentUsed:100, isBlocked 到次日 00:00Z）。只看时间
    会漏掉这种「高频请求撞满」的情况，所以数量维度必须与时间维度并列。
    """
    _clear_creds(mod)
    mod._CREDS["brA"] = {
        "vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J", "fe_version": "",
        "client_id": "brA", "expires_at": time.time() + 9999,
        "uses": mod._CREDS_MAX_USES,  # 次数打满，时间还新
    }
    ent = _run(mod._get_creds(_BrowserP(login_client_id="brA")))
    assert ent["vqd"] == "VQD-REAL"  # 重新收割的新身份，不是旧的那套
    assert ent["uses"] == 1          # 新身份从 1 开始计
    assert mod._CREDS["brA"]["uses"] == 1


def test_creds_fresh_within_use_cap_reuses_cache(mod):
    """时间新 + 次数没满 → 直接复用缓存（不重收割），且每取一次计数 +1。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {
        "vqd": "V", "fe_signals": "S", "ua": "U", "journey": "J", "fe_version": "",
        "client_id": "brA", "expires_at": time.time() + 9999, "uses": 0,
    }
    p = _BrowserP(login_client_id="brA")
    _run(mod._get_creds(p))
    _run(mod._get_creds(p))
    assert mod._CREDS["brA"]["uses"] == 2
    assert p.opened == []  # 没触发收割（没有 js 取头）


def test_perform_action_reharvests_when_uses_exhausted(mod):
    """定时取头：时间还新但次数打满 → 不跳过，强制换身份（否则定时任务坐视身份打满）。"""
    _clear_creds(mod)
    mod._CREDS["brA"] = {
        "vqd": "OLD", "fe_signals": "S", "ua": "U", "journey": "J", "fe_version": "",
        "client_id": "brA", "expires_at": time.time() + mod._CREDS_REFRESH_GRACE_SECONDS + 1,
        "uses": mod._CREDS_MAX_USES,
    }
    p = _BrowserP(login_client_id="brA")
    out = _run(mod.DuckAIChannel.perform_action(p, "refresh_headers", {}, {}))
    assert "取头" in out["last_headers_refresh"]  # 没跳过，真收割了
    assert mod._CREDS["brA"]["uses"] == 0         # 新身份没服务过请求，从 0 起
    assert mod._CREDS["brA"]["vqd"] == "VQD-REAL"


def test_creds_valid_two_dimensions(mod):
    """_creds_valid：时间 × 次数两维独立判死，缺 uses 键按 0 计（兼容旧缓存结构）。"""
    fresh = {"expires_at": time.time() + 9999, "uses": 0}
    assert mod._creds_valid(fresh) is True
    assert mod._creds_valid({**fresh, "uses": mod._CREDS_MAX_USES}) is False
    assert mod._creds_valid({**fresh, "expires_at": 0}) is False
    assert mod._creds_valid(None) is False
    assert mod._creds_valid({"expires_at": time.time() + 9999}) is True  # 无 uses 键 = 0 次


def test_reuses_existing_tab_without_closing(mod):
    """复用用户自己开着的 duck.ai 页：只摘钩子，不关页。"""
    _clear_creds(mod)
    p = _BrowserP(login_client_id="brA")
    p.found_tab = "sk-user"
    _run(mod.DuckAIChannel.perform_action(p, "refresh_headers", {}, {}))
    assert p.opened == [] and p.closed == []
    assert p.default_set == ["brA:sk-user"]

