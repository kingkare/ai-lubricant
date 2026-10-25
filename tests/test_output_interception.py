import re

import admin
import config
import main


def _set_main_config(monkeypatch, value: dict) -> None:
    monkeypatch.setattr(config.Config, "_load", classmethod(lambda cls: value))
    monkeypatch.setattr(config, "_OUTPUT_INTERCEPTION_CACHE", None)


def test_output_interception_defaults_are_disabled(monkeypatch):
    _set_main_config(monkeypatch, {})

    rules = config.Config.get_output_interception_rules()

    assert rules == config.DEFAULT_OUTPUT_INTERCEPTION_RULES
    assert config.Config.get_compiled_output_interception_patterns() == []


def test_output_interception_compiles_enabled_valid_rules(monkeypatch):
    _set_main_config(monkeypatch, {
        "retry": {
            "output_interception_rules": {
                "enabled": True,
                "rules": [
                    {"name": "placeholder", "enabled": True, "match_type": "regex", "pattern": r"No response requested\.?"},
                    {"name": "disabled", "enabled": False, "match_type": "regex", "pattern": "ignored"},
                    {"name": "literal", "enabled": True, "match_type": "text", "pattern": "[Tool use interrupted]"},
                    {"name": "invalid", "enabled": True, "match_type": "regex", "pattern": "["},
                ],
            }
        }
    })

    rules = config.Config.get_output_interception_rules()
    compiled = config.Config.get_compiled_output_interception_patterns()

    assert [item["name"] for item in rules["rules"]] == ["placeholder", "disabled", "literal"]
    assert all("match_type" in item for item in rules["rules"])
    assert len(compiled) == 2
    assert compiled[0][0] == "placeholder"
    assert compiled[0][1] == "regex"
    assert compiled[0][2].search("No response requested.")
    assert compiled[1][0] == "literal"
    assert compiled[1][1] == "text"
    assert compiled[1][2] == "[Tool use interrupted]"


def test_extract_response_content_text_supports_openai_parts_and_nested_body():
    assert main._extract_response_content_text({
        "choices": [{"message": {"content": "No response requested."}}]
    }) == "No response requested."
    assert main._extract_response_content_text({
        "choices": [{"message": {"content": [
            {"type": "text", "text": "No response "},
            {"type": "text", "text": "requested."},
        ]}}]
    }) == "No response requested."
    assert main._extract_response_content_text({
        "_passthrough_anthropic": True,
        "body": {"content": [{"type": "text", "text": "nested"}]},
    }) == "nested"
    assert main._extract_response_content_text({
        "response": {"output_text": "No response requested."},
    }) == "No response requested."
    assert main._extract_response_content_text({
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "No response requested."}]}],
    }) == "No response requested."


def test_collect_stream_response_content_text_is_protocol_agnostic():
    cases = [
        [
            'data: {"choices":[{"delta":{"content":"[Tool use "}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"interrupted]"}}]}\n\n',
        ],
        [
            'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"[Tool use "}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"interrupted]"}}\n\n',
        ],
        [
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"[Tool use "}\n\n',
            'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"interrupted]"}\n\n',
        ],
        [
            'data: {"candidates":[{"content":{"parts":[{"text":"[Tool use "}]}}]}\n\n',
            'data: {"candidates":[{"content":{"parts":[{"text":"interrupted]"}]}}]}\n\n',
        ],
    ]

    for chunks in cases:
        parts: list[str] = []
        for chunk in chunks:
            main._collect_stream_response_content_text(chunk, parts)
        assert "".join(parts) == "[Tool use interrupted]"


def test_intercepted_by_output_rule_returns_rule_name(monkeypatch):
    monkeypatch.setattr(
        main.config.Config,
        "get_compiled_output_interception_patterns",
        classmethod(lambda cls: [
            ("placeholder", "regex", re.compile(r"No response requested\.?")),
            ("literal", "text", "[Tool use interrupted]"),
        ]),
    )

    assert main._intercepted_by_output_rule("No response requested.") == "placeholder"
    assert main._intercepted_by_output_rule("normal response") is None
    assert main._intercepted_by_output_rule("") is None
    assert main._intercepted_by_output_rule("[Tool use interrupted]") == "literal"
    # 文本匹配大小写敏感
    assert main._intercepted_by_output_rule("[tool use interrupted]") is None
    # 子串匹配即可，不要求整段相等
    assert main._intercepted_by_output_rule("前缀 [Tool use interrupted] 后缀") == "literal"


def test_attempt_failure_status_distinguishes_interception_from_empty_response():
    from providers.base import (
        EmptyNonStreamResponseError,
        IncompleteStreamError,
        is_output_intercepted_error,
    )

    # 拦截命中（流式/非流式）→ output_intercepted，不再和真空响应混为一谈
    stream_hit = IncompleteStreamError("output intercepted by rule: 白鹿拦截", "output_intercepted")
    nonstream_hit = EmptyNonStreamResponseError(
        "output intercepted by rule: 白鹿拦截", code="output_intercepted"
    )
    assert is_output_intercepted_error(stream_hit)
    assert is_output_intercepted_error(nonstream_hit)
    assert main._attempt_failure_status(stream_hit) == "output_intercepted"
    assert main._attempt_failure_status(nonstream_hit) == "output_intercepted"

    # 真空响应仍是 empty_non_stream（默认 code 不变，向后兼容）
    genuine_empty = EmptyNonStreamResponseError("upstream returned stop response with empty output")
    assert genuine_empty.code == "upstream_empty_non_stream_response"
    assert not is_output_intercepted_error(genuine_empty)
    assert main._attempt_failure_status(genuine_empty) == "empty_non_stream"

    # 其它失败仍是 error
    assert main._attempt_failure_status(ValueError("boom")) == "error"


def test_intercepted_non_stream_error_keeps_retryable_semantics():
    """拦截命中的非流式异常仍是 EmptyNonStreamResponseError 子类型——
    重试分类（retryable_incomplete_response）与「不冻账号」口径都不变。"""
    from providers.base import EmptyNonStreamResponseError

    err = EmptyNonStreamResponseError("output intercepted by rule: 白鹿拦截", code="output_intercepted")
    assert isinstance(err, EmptyNonStreamResponseError)
    assert err.code == "output_intercepted"


def test_admin_clean_output_interception_rules_drops_invalid_entries():
    cleaned = admin._clean_output_interception_rules({
        "enabled": True,
        "rules": [
            {"name": "placeholder", "enabled": True, "match_type": "regex", "pattern": r"No response requested\.?"},
            {"name": "", "enabled": True, "pattern": "missing-name"},
            {"name": "invalid", "enabled": True, "match_type": "regex", "pattern": "["},
            {"name": "literal", "enabled": True, "match_type": "text", "pattern": "[Tool use interrupted]"},
        ],
    })

    assert cleaned == {
        "enabled": True,
        "rules": [
            {"name": "placeholder", "enabled": True, "match_type": "regex", "pattern": r"No response requested\.?"},
            {"name": "literal", "enabled": True, "match_type": "text", "pattern": "[Tool use interrupted]"},
        ],
    }


def test_admin_clean_output_interception_rules_defaults_match_type():
    cleaned = admin._clean_output_interception_rules({
        "enabled": True,
        "rules": [
            {"name": "legacy", "enabled": True, "pattern": r"No response requested\.?"},
        ],
    })

    assert cleaned == {
        "enabled": True,
        "rules": [
            {"name": "legacy", "enabled": True, "match_type": "regex", "pattern": r"No response requested\.?"},
        ],
    }
