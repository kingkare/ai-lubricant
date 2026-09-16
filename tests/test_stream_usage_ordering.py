"""SSE usage 顺序归一化回归测试。

历史问题：部分 OpenAI 兼容上游在流末尾先发 ``data: [DONE]``，再把 usage chunk
补在它*之后*（不合规范）。规范客户端（OpenAI SDK、dsh 等）读到 [DONE] 即结束
流，永远看不到 usage，客户端侧用量统计全丢。

修复：转发层暂存 [DONE]，peek 下一个 chunk；若是纯 usage chunk 则先发 usage
再发 [DONE]，其它情况原样按序发出。只暂存一条 chunk，不缓冲整条流。
"""
import asyncio

import main as app_main


def _collect(chunks):
    async def gen():
        for c in chunks:
            yield c

    async def run():
        out = []
        async for c in app_main._normalize_usage_before_done(gen()):
            out.append(c)
        return out

    return asyncio.run(run())


def _usage_chunk(prompt=7, completion=13):
    import json
    return "data: " + json.dumps({
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }) + "\n\n"


_DONE = "data: [DONE]\n\n"
_CONTENT = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'


# ── 基本顺序修正 ──────────────────────────────────────────────────────

def test_usage_after_done_is_moved_before():
    """上游把 usage 放在 [DONE] 之后 → 归一后 usage 在 [DONE] 之前。"""
    out = _collect([_CONTENT, _DONE, _usage_chunk()])
    assert out == [_CONTENT, _usage_chunk(), _DONE]


def test_usage_before_done_unchanged():
    """usage 顺序本就正确的流 → 原样透传。"""
    chunks = [_CONTENT, _usage_chunk(), _DONE]
    assert _collect(chunks) == chunks


def test_no_usage_at_all_unchanged():
    """没有 usage chunk → [DONE] 原样透传。"""
    chunks = [_CONTENT, _DONE]
    assert _collect(chunks) == chunks


def test_done_at_end_without_trailing_chunk():
    """[DONE] 是最后一个 chunk → 流结束前 flush，不丢。"""
    chunks = [_CONTENT, _DONE]
    assert _collect(chunks) == chunks


# ── 边界：归一不能误伤 ────────────────────────────────────────────────

def test_content_after_done_is_preserved_in_order():
    """[DONE] 后若还有内容 chunk（异常/非标准上游），顺序保持、不丢内容。"""
    out = _collect([_CONTENT, _DONE, _CONTENT])
    assert out == [_CONTENT, _DONE, _CONTENT]


def test_two_usage_chunks_after_done():
    """[DONE] 后有两个 usage chunk → 都提到前面，且保持相对顺序。"""
    u1, u2 = _usage_chunk(1, 2), _usage_chunk(3, 4)
    out = _collect([_CONTENT, _DONE, u1, u2])
    assert out == [_CONTENT, u1, u2, _DONE]


def test_dict_chunks_pass_through_untouched():
    """内部 dict 块（如 _last_route_info / _passthrough_done）原样透传。"""
    internal = {"_last_route_info": {"status": "ok"}}
    out = _collect([_CONTENT, internal, _DONE])
    assert out == [_CONTENT, internal, _DONE]


def test_dict_chunk_after_done_flushes_done_first():
    """[DONE] 后跟内部 dict 块 → [DONE] 先出，dict 原样跟上。"""
    internal = {"_passthrough_done": True}
    out = _collect([_DONE, internal])
    assert out == [_DONE, internal]


def test_done_embedded_with_other_payload_not_treated_as_done_only():
    """一个 chunk 里同时有 [DONE] 和内容 → 不算纯 [DONE]，不动它。"""
    mixed = _CONTENT + _DONE
    out = _collect([mixed])
    assert out == [mixed]


def test_empty_stream():
    assert _collect([]) == []


def test_chunk_with_choices_and_usage_is_hoisted():
    """带 choices+delta+usage 的 chunk（如 OpenAI 规范末帧）出现在 [DONE] 之后也提前，
    整块移到 [DONE] 前——客户端在 [DONE] 前拿到内容与 usage。"""
    import json
    chunk = "data: " + json.dumps({
        "choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }) + "\n\n"
    out = _collect([_DONE, chunk])
    assert out == [chunk, _DONE]


def test_content_chunk_without_usage_after_done_keeps_order():
    """[DONE] 后跟无 usage 的内容 chunk → 保持原序（[DONE] 先发）。

    上游在 [DONE] 后补内容是非标准行为；客户端在 [DONE] 处停止读取，本就
    看不到它。我们不做"提前内容"——只提前 usage。"""
    out = _collect([_DONE, _CONTENT])
    assert out == [_DONE, _CONTENT]
