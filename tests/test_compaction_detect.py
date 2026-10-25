"""compaction_detect 单测：五家客户端指纹 + 显式标记 + 误判防护。

无需 PG，纯逻辑测试。
"""

from server.compaction_detect import detect_compaction, is_compaction, EXPLICIT_BODY_KEY


def _hit(body, expect_client, expect_source="fingerprint"):
    d = detect_compaction(body)
    assert d is not None, f"should detect {expect_client}: {body}"
    assert d["client"] == expect_client, d
    assert d["source"] == expect_source, d


# ── 显式标记（第一方 agent）─────────────────────────────────────────────
def test_explicit_marker_wins():
    _hit({EXPLICIT_BODY_KEY: "compaction", "messages": []}, "explicit", "explicit")


def test_explicit_marker_is_case_insensitive():
    _hit({EXPLICIT_BODY_KEY: "COMPACTION"}, "explicit", "explicit")


# ── 本项目 agent 的摘要指令（第一方，文本指纹覆盖）─────────────────────
def test_first_party_agent_summarizer():
    _hit({"messages": [
        {"role": "system", "content": (
            "You compress an agent's earlier conversation into a dense factual note "
            "so a continuing agent keeps the context."
        )},
        {"role": "user", "content": "transcript..."},
    ]}, "agent")


# ── Claude Code：指令在末尾 user，3 个变体 ─────────────────────────────
def test_claude_code_so_far():
    _hit({"messages": [
        {"role": "system", "content": "You are Claude Code"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": (
            "Your task is to create a detailed summary of the conversation so far, "
            "paying close attention to the user's explicit requests."
        )},
    ]}, "claude-code")


def test_claude_code_recent_portion():
    _hit({"messages": [
        {"role": "user", "content": (
            "Your task is to create a detailed summary of the RECENT portion of the "
            "conversation — messages that follow earlier retained context."
        )},
    ]}, "claude-code")


# ── Codex：COMPACT 指令 ─────────────────────────────────────────────────
def test_codex_context_checkpoint():
    _hit({"messages": [
        {"role": "system", "content": "You are Codex"},
        {"role": "user", "content": (
            "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff "
            "summary for another LLM that will resume the task."
        )},
    ]}, "codex")


# ── OpenCode：独立 system prompt ────────────────────────────────────────
def test_opencode_system_prompt():
    _hit({"messages": [
        {"role": "system", "content": (
            "You are a context summarization agent. You are given a conversation "
            "between a user and an agent. Your goal is to produce a structured summary."
        )},
        {"role": "user", "content": "long history..."},
    ]}, "opencode")


# ── Cline 家族（Cline / Roo / CodeBuddy / WorkBuddy）────────────────────
def test_cline_family_block():
    _hit({"messages": [
        {"role": "system", "content": "You are Roo Code"},
        {"role": "user", "content": (
            "The `<conversation-to-summarize>` block below contains a past "
            "conversation that you must summarize."
        )},
    ]}, "cline")


# ── Anthropic 顶层 system 位置（OpenAI 格式没有这个键）───────────────────
def test_anthropic_top_level_system():
    _hit({"system": "You are an anchored context summarization assistant for a coding session.",
          "messages": [{"role": "user", "content": "history"}]}, "atomcode")


# ── dsh：末尾 user compaction engine ────────────────────────────────────
def test_dsh_compaction_engine():
    _hit({"messages": [
        {"role": "user", "content": (
            "You are now acting as a compaction engine for this AI coding assistant. "
            "Condense the conversation ABOVE into a structured checkpoint."
        )},
    ]}, "dsh")


# ── Responses 协议 instructions 位置 ────────────────────────────────────
def test_responses_instructions_position():
    _hit({"instructions": (
        "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary."
    ), "input": []}, "codex")


# ── 误判防护：正常对话绝不能命中 ────────────────────────────────────────
def test_normal_chat_not_flagged():
    bodies = [
        {"messages": [
            {"role": "system", "content": "You are Claude Code"},
            {"role": "user", "content": "帮我总结一下这个模块的职责"},
        ]},
        # 含"summary"但非压缩指令
        {"messages": [
            {"role": "system", "content": "You output only a summary title"},
            {"role": "user", "content": "summarize the file for me"},
        ]},
        {"messages": [{"role": "user", "content": "hello"}]},
        {},
    ]
    for b in bodies:
        assert detect_compaction(b) is None, b
        assert not is_compaction(b)


def test_text_is_case_insensitive():
    _hit({"messages": [
        {"role": "user", "content": "YOU ARE PERFORMING A CONTEXT CHECKPOINT COMPACTION."},
    ]}, "codex")


def test_empty_and_non_dict_input():
    assert detect_compaction(None) is None
    assert detect_compaction("x") is None
    assert detect_compaction({}) is None
