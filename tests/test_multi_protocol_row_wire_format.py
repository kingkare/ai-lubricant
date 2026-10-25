"""多协议行渠道：线格式判定必须按「本次选中的协议行」，不能读渠道级首行。

## 历史 bug（bailu-2.9-preview 全天反复冻结）

渠道配两条协议行——首行 anthropic（只绑定 bailu-apex-openclaw）、次行 chat（通用）。
请求 bailu-2.9-preview 时路由**正确**落到 chat 行，URL 也正确解析为
/v1/chat/completions；但 `chat_anthropic` 的同协议直通判定读的是渠道级
`channel.protocol`，而它的定义是「第一条 enabled 行」→ 拿到 anthropic → 误判同协议。

后果：原始 anthropic body 被直发到 chat 端点，上游回 OpenAI chat.completion.chunk
帧，直通解析器只认 anthropic 事件名 → has_real_output 恒 False、usage 取不到 →
流末抛 IncompleteStreamError("upstream stream completed with empty output")，
满屏真实输出（实测 5664 completion tokens）被判空流并冻结账号 300 秒。

## 修复口径

协议选择收敛成一个方法 `resolve_chat_protocol_row(model_id, kwargs)`，返回**具体的
某一行**；`get_effective_channel_protocol()` 在其上取 protocol 字符串。所有决定线格式
（body 形态 / 解析器 / 直通判定 / 鉴权 / anthropic-beta）的地方都读它，唯一真相源
是「本次选中的行」——先选行，行说了算。

覆盖：
1. 双行渠道下 `get_effective_channel_protocol` 跟随选中行而非首行；
2. 选中 chat 行时 anthropic 入站必须走**跨协议转换**（原 bug 场景）；
3. 选中 anthropic 行时仍走**直通**（防修复反向误伤）；
4. 单协议行 / 无协议行渠道行为与改动前一致。
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel import Channel
from providers.base import IncompleteStreamError
from providers.custom import CustomProvider


# bailu 渠道的真实配置形态：首行 anthropic 绑定单模型，次行 chat 通用。
BAILU_ROWS = [
    {"id": "anthropic-chat-1", "protocol": "anthropic", "path": "/v1/messages",
     "models": ["bailu-apex-openclaw"], "enabled": True, "upstream_stream": True},
    {"id": "openai-chat-0", "protocol": "chat", "path": "/v1/chat/completions",
     "models": [], "enabled": True, "upstream_stream": True},
]

# 上游实际回帧：OpenAI chat.completion.chunk（端点是 chat/completions，必然如此）。
# reasoning + tool_calls 都要有，才能验证转换后 thinking_delta / tool_use 都落地。
OPENAI_FRAMES = [
    'data: {"id":"x","object":"chat.completion.chunk","model":"bailu-2.9-preview",'
    '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
    'data: {"id":"x","object":"chat.completion.chunk","model":"bailu-2.9-preview",'
    '"choices":[{"index":0,"delta":{"reasoning_content":"thinking hard"},"finish_reason":null}]}\n\n',
    'data: {"id":"x","object":"chat.completion.chunk","model":"bailu-2.9-preview",'
    '"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_x","type":"function",'
    '"function":{"name":"grep","arguments":"{\\"p\\":1}"}}]},"finish_reason":null}]}\n\n',
    'data: {"id":"x","object":"chat.completion.chunk","model":"bailu-2.9-preview",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
    '"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}\n\n',
    "data: [DONE]\n\n",
]

# 上游回 anthropic 帧（选中 anthropic 行的直通场景）。
ANTHROPIC_FRAMES = [
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"role":"assistant","content":[],"usage":{"input_tokens":5,"output_tokens":0}}}\n\n',
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"thinking","thinking":""}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"thinking_delta","thinking":"thinking hard"}}\n\n',
    'event: message_delta\ndata: {"type":"message_delta",'
    '"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":20}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]

RAW_ANTHROPIC_BODY = {
    "model": "bailu-2.9-preview",
    "max_tokens": 64000,
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
}


class _ReplayProvider(CustomProvider):
    """按预置帧回放；同时记录实际出站 URL 与 body 形态。"""

    frames: list = OPENAI_FRAMES

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.posts: list[dict] = []

    async def send_sse_request(self, method, url, headers, on_headers=None, **kwargs):
        body = kwargs.get("json") or {}
        self.posts.append({
            "url": url,
            # anthropic body 带 max_tokens（openai body 不带）；用这一个判别即可，
            # 避免依赖 system 是否存在（无 system 消息时 anthropic body 也没有该键）。
            "body_is_anthropic": "max_tokens" in body,
            "body_is_openai": "messages" in body and "max_tokens" not in body,
        })
        for frame in self.frames:
            yield frame


def _provider(rows=None, protocol="anthropic", frames=None):
    provider = _ReplayProvider(
        "user", "key",
        provider_name="bailu", base_url="https://bailucode.com/openapi",
        protocol=protocol, upstream_stream=True,
    )
    if rows is not None:
        provider.attach_channel(Channel("bailu", {"base_url": provider.base_url,
                                                  "chat_protocols": rows}))
    if frames is not None:
        provider.frames = frames
    return provider


def _collect_anthropic(provider, row, model_id="bailu-2.9-preview"):
    async def _run():
        return [
            chunk async for chunk in provider.chat_anthropic(
                model_id, RAW_ANTHROPIC_BODY["messages"], stream=True,
                _raw_anthropic_body=RAW_ANTHROPIC_BODY, _endpoint_config=row,
            )
        ]

    return asyncio.run(_run())


# ==================== 1. 生效协议跟随选中行 ====================

def test_effective_protocol_follows_selected_row_not_first_row():
    """双行渠道：首行 anthropic，选中 chat 行时生效协议必须是 chat。

    这正是 bug 的判定点——改动前这里返回首行的 anthropic，导致直通误判。
    """
    provider = _provider(BAILU_ROWS)

    assert provider.get_channel_protocol() == "anthropic"  # 渠道级派生视图仍是首行
    assert provider.get_effective_channel_protocol(
        "bailu-2.9-preview", {"_endpoint_config": BAILU_ROWS[1]}
    ) == "chat"
    assert provider.get_effective_channel_protocol(
        "bailu-apex-openclaw", {"_endpoint_config": BAILU_ROWS[0]}
    ) == "anthropic"


def test_effective_protocol_reselects_row_when_endpoint_config_absent():
    """没有路由钉住的行时按模型重选，而不是回退渠道级首行。"""
    provider = _provider(BAILU_ROWS)

    # 未绑定首行的模型 → 落到通用 chat 行
    assert provider.get_effective_channel_protocol("bailu-2.9-preview", {}) == "chat"
    # 绑定首行的模型 → anthropic 行
    assert provider.get_effective_channel_protocol("bailu-apex-openclaw", {}) == "anthropic"


def test_same_protocol_for_request_uses_selected_row():
    """直通判定：客户端 anthropic + 选中 chat 行 → 不同协议。"""
    provider = _provider(BAILU_ROWS)

    assert provider.same_protocol_for_request(
        "anthropic", RAW_ANTHROPIC_BODY, "messages", "bailu-2.9-preview",
        {"_endpoint_config": BAILU_ROWS[1]},
    ) is False
    assert provider.same_protocol_for_request(
        "anthropic", RAW_ANTHROPIC_BODY, "messages", "bailu-apex-openclaw",
        {"_endpoint_config": BAILU_ROWS[0]},
    ) is True


# ==================== 2. 原 bug 场景：选中 chat 行 → 走转换 ====================

def test_anthropic_client_on_selected_chat_row_converts_instead_of_passthrough():
    """原 bug：anthropic 入站 + 选中 chat 行，必须走跨协议转换而非直通。

    改动前：直通把 anthropic body 发到 chat 端点，OpenAI 回帧按 anthropic 事件名
    解析 → 满屏输出判空流抛 IncompleteStreamError。
    """
    provider = _provider(BAILU_ROWS, frames=OPENAI_FRAMES)

    events = _collect_anthropic(provider, BAILU_ROWS[1])

    # 不再抛「空输出」——这是 bug 的直接症状
    assert not any(
        isinstance(c, str) and "empty output" in c for c in events
    ), events

    # 出站 URL 与 body 都是 chat 行该有的形态
    assert provider.posts[-1]["url"] == "https://bailucode.com/openapi/v1/chat/completions"
    assert provider.posts[-1]["body_is_openai"] is True

    # OpenAI 帧被正确解析并转回 anthropic 事件流
    text = "".join(c for c in events if isinstance(c, str))
    assert "event: message_start" in text
    assert "event: message_stop" in text
    assert "thinking_delta" in text          # reasoning_content 未丢
    assert "tool_use" in text                # tool_calls 未丢


def test_selected_chat_row_usage_is_captured_not_lost():
    """转换路径必须把上游 usage 转成 anthropic usage，不能记 0。"""
    provider = _provider(BAILU_ROWS, frames=OPENAI_FRAMES)

    events = _collect_anthropic(provider, BAILU_ROWS[1])
    text = "".join(c for c in events if isinstance(c, str))

    usage_lines = [
        json.loads(line[5:].strip())
        for line in text.splitlines()
        if line.startswith("data:") and '"usage"' in line
    ]
    usages = [u["usage"] for u in usage_lines if isinstance(u.get("usage"), dict)]
    assert usages, text
    assert any(
        (u.get("output_tokens") or u.get("completion_tokens") or 0) > 0 for u in usages
    ), usages


# ==================== 3. 防反向误伤：选中 anthropic 行仍直通 ====================

def test_selected_anthropic_row_still_passthrough():
    """选中 anthropic 行 + 上游 anthropic 帧 → 仍走直通，原样转发。"""
    provider = _provider(BAILU_ROWS, frames=ANTHROPIC_FRAMES)

    events = _collect_anthropic(provider, BAILU_ROWS[0], model_id="bailu-apex-openclaw")

    assert provider.posts[-1]["url"] == "https://bailucode.com/openapi/v1/messages"
    assert provider.posts[-1]["body_is_anthropic"] is True

    text = "".join(c for c in events if isinstance(c, str))
    # 直通：上游事件原样转发（thinking 不做品牌改写以外的重排）
    assert "event: message_start" in text
    assert "event: message_stop" in text


# ==================== 4. 单协议 / 无协议行渠道行为不变 ====================

def test_single_row_channel_unchanged():
    """单协议行渠道：首行即选中行，生效协议与渠道级协议一致（改动前后同值）。"""
    rows = [{"id": "only", "protocol": "anthropic", "path": "/v1/messages",
             "models": [], "enabled": True, "upstream_stream": True}]
    provider = _provider(rows, protocol="anthropic")

    assert provider.get_channel_protocol() == "anthropic"
    assert provider.get_effective_channel_protocol("m", {}) == "anthropic"
    assert provider.same_protocol_for_request(
        "anthropic", RAW_ANTHROPIC_BODY, "messages", "m", {}
    ) is True


def test_no_protocol_row_falls_back_to_channel_protocol():
    """无协议行（旧配置 / 内置渠道）→ 生效协议回退渠道级 protocol，行为不变。

    注意：旧配置会由 `chat_protocols_from_legacy` 按顶层 protocol 合成一行，
    所以 `resolve_chat_protocol_row` 可能返回合成行——那行的 protocol 就等于渠道级
    protocol，故**生效协议**必须一致。关键契约是 `get_effective_channel_protocol`
    的值，而不是它内部走哪条分支。
    """
    provider = _provider(None, protocol="responses")

    assert provider.get_effective_channel_protocol("m", {}) == "responses"
    assert provider.get_channel_protocol() == "responses"


def test_base_provider_without_channel_rows_returns_none():
    """纯 BaseProvider（非 custom、无协议行概念）→ 占位实现返回 None，调用方回退。"""
    from providers.base import BaseProvider

    assert BaseProvider.resolve_chat_protocol_row(None, "m", {}) is None


def test_passthrough_gate_still_requires_marker():
    """直通仍需 body 带对应标记：只有协议行对上、body 形态不符时不得直通。"""
    provider = _provider(BAILU_ROWS)

    assert provider.same_protocol_for_request(
        "anthropic", {"input": "hi"}, "messages", "bailu-apex-openclaw",
        {"_endpoint_config": BAILU_ROWS[0]},
    ) is False


# ==================== 5. anthropic body 构造分支跟随生效协议行 ====================

def test_anthropic_payload_branch_follows_selected_row(monkeypatch):
    """`_build_anthropic_payload` 的同协议分支也必须按生效协议行判定。

    场景：选中 chat 行 + 伪装 claude-code（`_resolve_build_endpoint` 把 endpoint
    解析成 anthropic），此时内部消息是 **OpenAI 格式**。改动前该分支读渠道级首行
    （anthropic）→ 把 OpenAI 格式消息当 anthropic 原生处理，body 形态错误；改动后
    按生效行（chat）走 `_openai_request_to_anthropic` 正确转换。

    这里用 spy 断言「走了哪条分支」，不依赖真实 body 构造（避免 config_store 依赖）。
    """
    provider = _provider(BAILU_ROWS)
    calls = {"openai_payload": 0, "raw_passthrough": 0}

    real_build_openai = provider._build_openai_payload

    def _spy_build_openai(model_id, messages, stream, **kwargs):
        calls["openai_payload"] += 1
        return real_build_openai(model_id, messages, stream, **kwargs)

    monkeypatch.setattr(provider, "_build_openai_payload", _spy_build_openai)

    messages = [{"role": "user", "content": "hi"}]

    # 选中 chat 行 → 生效协议非 anthropic → 必须走 openai→anthropic 转换
    provider._build_anthropic_payload(
        "bailu-2.9-preview", messages, True,
        _endpoint_config=BAILU_ROWS[1],
    )
    assert calls["openai_payload"] == 1

    # 选中 anthropic 行 → 生效协议 anthropic → 走同协议构造，不经过 openai 转换
    calls["openai_payload"] = 0
    provider._build_anthropic_payload(
        "bailu-apex-openclaw", messages, True,
        _endpoint_config=BAILU_ROWS[0],
    )
    assert calls["openai_payload"] == 0


def test_anthropic_beta_header_only_for_anthropic_row(monkeypatch):
    """payload 派生的 anthropic-beta 只在生效协议行是 anthropic 时补。

    注意：伪装 preset（claude-code 等）自身会带 anthropic-beta（如 context-1m），
    那是 `_headers` 的职责，与本守卫无关。这里只断言**由 payload 字段派生**的 flag
    （mcp_servers → mcp-client-2025-04-04）不落到 chat 行上。
    """
    provider = _provider(BAILU_ROWS)

    chat_headers = provider._anthropic_headers(
        {"mcp_servers": [{"name": "mcp"}]}, {"_endpoint_config": BAILU_ROWS[1]}
    )
    anthropic_headers = provider._anthropic_headers(
        {"mcp_servers": [{"name": "mcp"}]}, {"_endpoint_config": BAILU_ROWS[0]}
    )

    def _flags(headers):
        return (headers.get("anthropic-beta") or "").split(",")

    assert "mcp-client-2025-04-04" not in _flags(chat_headers)
    assert "mcp-client-2025-04-04" in _flags(anthropic_headers)
