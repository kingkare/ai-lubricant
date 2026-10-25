"""超长单条消息切分（split_oversized_messages）的单元测试。

背景与实测数据见 message_utils.split_oversized_messages 的模块注释：
bailu 等第三方上游对单条消息另设 ~40000 token 上限，与上下文窗口无关。
"""
from message_utils import split_oversized_messages

CN = "这是一段用于测试的填充文本，包含中文与标点。"
EN = "This is filler text used for testing the splitter. "


def mk(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


# ── 不触发切分的情形 ────────────────────────────────────────────

def test_short_message_untouched():
    """未超限时返回原列表对象（零改动）。"""
    msgs = [{"role": "user", "content": "hi"}]
    assert split_oversized_messages(msgs, 32000) is msgs


def test_disabled_by_zero_budget():
    """max_tokens<=0 视为关闭，不做任何处理。"""
    msgs = [{"role": "user", "content": mk(CN, 100000)}]
    assert split_oversized_messages(msgs, 0) is msgs
    assert split_oversized_messages(msgs, -1) is msgs


def test_tool_message_not_split():
    """role=tool 的消息不切：tool_call_id 配对不可拆，切开上游回 code=11148。"""
    msgs = [{"role": "tool", "content": mk(CN, 100000), "tool_call_id": "c1"}]
    assert split_oversized_messages(msgs, 1000) is msgs


def test_assistant_with_tool_calls_not_split():
    """带 tool_calls 的 assistant 消息不切，理由同上。"""
    msgs = [{
        "role": "assistant",
        "content": mk(CN, 100000),
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "f", "arguments": "{}"}}],
    }]
    assert split_oversized_messages(msgs, 1000) is msgs


def test_multimodal_content_not_split():
    """content 为 block 列表（多模态 / 原生 anthropic 形态）不切。"""
    msgs = [{"role": "user", "content": [{"type": "text", "text": mk(CN, 100000)}]}]
    assert split_oversized_messages(msgs, 1000) is msgs


def test_system_message_is_split():
    """system 必须纳入切分：agent 类客户端的 system 同样会超上游单条上限。"""
    original = mk(CN, 100000)
    out = split_oversized_messages([{"role": "system", "content": original}], 10000)
    assert len(out) > 1
    assert all(m["role"] == "system" for m in out)
    assert "".join(m["content"] for m in out) == original


# ── 触发切分的情形 ──────────────────────────────────────────────

def test_oversized_user_message_is_split():
    """超限 user 消息被切成多条，且全部为同一角色。"""
    msgs = [{"role": "user", "content": mk(CN, 100000)}]
    out = split_oversized_messages(msgs, 10000)
    assert len(out) > 1
    assert all(m["role"] == "user" for m in out)


def test_split_is_lossless():
    """切分无损：拼接后与原文逐字相同。"""
    original = mk(CN, 100000)
    out = split_oversized_messages([{"role": "user", "content": original}], 10000)
    assert "".join(m["content"] for m in out) == original


def test_each_piece_within_budget():
    """每段都不超过预算（估算口径）。"""
    from usage_utils import estimate_tokens_from_text
    out = split_oversized_messages([{"role": "user", "content": mk(CN, 100000)}], 10000)
    for m in out:
        assert estimate_tokens_from_text(m["content"]) <= 10000


def test_split_breaks_at_natural_boundary():
    """优先在句末/换行处断开，而不是硬切在词中间。"""
    text = ("第一句话结束。" * 200) + "\n\n" + ("第二段内容。" * 200)
    out = split_oversized_messages([{"role": "user", "content": text}], 500)
    assert len(out) > 1
    # 除最后一段外，每段都应以句末或换行结尾（自然边界）。
    for piece in [m["content"] for m in out[:-1]]:
        assert piece.endswith(("。", "\n", "！", "？", "；", ". ", "，", "、", " "))


def test_english_uses_char_ratio_not_token_count():
    """英文按字符/token 比例换算预算，不会因 token 数当字符数而切得过碎。"""
    out = split_oversized_messages([{"role": "user", "content": mk(EN, 200000)}], 10000)
    # 英文 ~4 字符/token，200K 字符 ≈50K token，切成 10K token/段应约 5 段。
    assert 3 <= len(out) <= 8, f"got {len(out)} pieces"


def test_other_messages_preserved_in_order():
    """只切超限的那条，其余消息原样且保持顺序。"""
    big = mk(CN, 100000)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": big},
        {"role": "assistant", "content": "ok"},
    ]
    out = split_oversized_messages(msgs, 10000)
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[-1] == {"role": "assistant", "content": "ok"}
    assert "".join(m["content"] for m in out[1:-1]) == big


def test_cache_control_kept_on_last_piece_only():
    """cache_control 只保留在最后一段（它标记的是这段内容的末尾）。"""
    msgs = [{"role": "user", "content": mk(CN, 100000), "cache_control": {"type": "ephemeral"}}]
    out = split_oversized_messages(msgs, 10000)
    assert len(out) > 1
    assert "cache_control" not in out[0]
    assert out[-1]["cache_control"] == {"type": "ephemeral"}


def test_original_message_not_mutated():
    """不修改输入消息对象（dict 是浅拷贝后改写）。"""
    msg = {"role": "user", "content": mk(CN, 100000)}
    original_content = msg["content"]
    split_oversized_messages([msg], 10000)
    assert msg["content"] == original_content
