"""Regression coverage for empty Anthropic-compatible streams."""

import pytest

from main import _chunk_has_stream_content, _validate_stream_completion, IncompleteStreamError


def test_empty_anthropic_structural_trace_is_not_content():
    """Structural Anthropic events with zero output must not count as content."""
    empty_trace = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"content":[],"usage":{"input_tokens":0,"output_tokens":0}}}\n\n'
        'event: ping\n'
        'data: {"type":"ping"}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":0,"output_tokens":0}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    assert not _chunk_has_stream_content(empty_trace)
    with pytest.raises(IncompleteStreamError):
        _validate_stream_completion(None, had_content=False)


def test_anthropic_non_empty_blocks_remain_content():
    assert _chunk_has_stream_content(
        'event: content_block_start\n'
        'data: {"type":"content_block_start","content_block":{"type":"text","text":"hello"}}\n\n'
    )
    assert _chunk_has_stream_content(
        'event: content_block_start\n'
        'data: {"type":"content_block_start","content_block":{"type":"tool_use","name":"lookup"}}\n\n'
    )


def test_thinking_only_anthropic_stream_counts_as_content():
    """纯思考流（正文为空、只有 thinking）是合法响应，不能判成 zero completion 空流。

    线上 trace：上游 reasoning 打满 max_tokens，content 全程为空、finish_reason=length，
    但 usage 有 completion_tokens=64000。修复前 `_chunk_has_stream_content` 不认
    `thinking_delta`，stream_had_content 恒为 False → 抛
    `upstream response usage reports zero completion tokens` 并冻结账号。
    """
    thinking_delta = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"let me think"}}\n\n'
    )
    thinking_start = (
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"thinking","thinking":"","signature":"sig"}}\n\n'
    )
    # Responses 协议的思考增量同理。
    reasoning_delta = (
        'event: response.reasoning_text.delta\n'
        'data: {"type":"response.reasoning_text.delta","delta":"thought"}\n\n'
    )

    assert _chunk_has_stream_content(thinking_delta)
    assert _chunk_has_stream_content(reasoning_delta)
    # thinking 块的空 start 帧（只有签名、无正文）仍不算内容。
    assert not _chunk_has_stream_content(thinking_start)

    # 有思考输出后，结束时的零 completion usage 不应再判失败。
    _validate_stream_completion(
        {"prompt_tokens": 291076, "completion_tokens": 64000},
        had_content=_chunk_has_stream_content(thinking_delta),
    )


def test_thinking_delta_with_empty_text_is_not_content():
    """thinking_delta 的 thinking 为空（纯结构帧）仍不算内容，保留原判空语义。"""
    assert not _chunk_has_stream_content(
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":""}}\n\n'
    )
