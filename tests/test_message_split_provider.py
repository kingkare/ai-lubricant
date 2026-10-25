"""超长消息切分在 provider 出站链路上的接入测试。

覆盖：配置开关、渠道级覆盖、anthropic 原生格式的降级/还原。
纯函数 split_oversized_messages 本身的测试见 test_message_split.py。
"""
import pytest

from config import Config
from message_utils import split_oversized_messages
from providers.custom import CustomProvider

CN = "这是一段用于测试的填充文本，包含中文与标点。"


def mk(n: int) -> str:
    return (CN * (n // len(CN) + 1))[:n]


def _provider(protocol="openai", **channel_fields):
    p = CustomProvider(
        "user", "password",
        base_url="https://example.com",
        protocol=protocol,
        api_key="test-key",
    )
    if channel_fields:
        from types import SimpleNamespace
        p._channel = SimpleNamespace(**channel_fields)
    return p


@pytest.fixture
def split_off(monkeypatch):
    """全局关闭（默认态）。"""
    monkeypatch.setattr(Config, "message_split_settings", classmethod(lambda cls: (False, 32000)))


@pytest.fixture
def split_on(monkeypatch):
    """全局开启，阈值 10000。"""
    monkeypatch.setattr(Config, "message_split_settings", classmethod(lambda cls: (True, 10000)))


# ── 开关行为 ────────────────────────────────────────────────────

def test_disabled_returns_same_list(split_off):
    """全局关闭时零改动返回原列表对象。"""
    p = _provider()
    msgs = [{"role": "user", "content": mk(100000)}]
    assert p._apply_message_split(msgs, "m") is msgs


def test_enabled_splits_oversized(split_on):
    """全局开启时超长消息被切分。"""
    p = _provider()
    msgs = [{"role": "user", "content": mk(100000)}]
    out = p._apply_message_split(msgs, "m")
    assert len(out) > 1
    assert all(m["role"] == "user" for m in out)
    assert "".join(m["content"] for m in out) == msgs[0]["content"]


def test_enabled_but_short_untouched(split_on):
    """开启但未超限时零改动。"""
    p = _provider()
    msgs = [{"role": "user", "content": "short"}]
    assert p._apply_message_split(msgs, "m") is msgs


# ── 渠道级覆盖 ──────────────────────────────────────────────────

def test_channel_can_enable_when_global_off(split_off):
    """全局关、渠道开 → 生效（这是实际用法：只对 bailu 开启）。"""
    p = _provider(message_split_enabled=True, message_split_max_tokens=10000)
    msgs = [{"role": "user", "content": mk(100000)}]
    assert len(p._apply_message_split(msgs, "m")) > 1


def test_channel_can_disable_when_global_on(split_on):
    """全局开、渠道关 → 不生效。"""
    p = _provider(message_split_enabled=False)
    msgs = [{"role": "user", "content": mk(100000)}]
    assert p._apply_message_split(msgs, "m") is msgs


def test_channel_overrides_threshold_only(split_on):
    """渠道只覆盖阈值、沿用全局启用状态。"""
    p = _provider(message_split_max_tokens=3000)
    msgs = [{"role": "user", "content": mk(100000)}]
    out = p._apply_message_split(msgs, "m")
    from usage_utils import estimate_tokens_from_text
    assert len(out) > 1
    for m in out:
        assert estimate_tokens_from_text(m["content"]) <= 3000


def test_channel_invalid_threshold_falls_back(split_on):
    """渠道阈值非法时回退到全局值，不抛异常。"""
    p = _provider(message_split_max_tokens="not-a-number")
    msgs = [{"role": "user", "content": mk(100000)}]
    assert len(p._apply_message_split(msgs, "m")) > 1


# ── anthropic 原生格式 ──────────────────────────────────────────

def test_anthropic_blocks_split_and_restore(split_on):
    """原生 anthropic 单 text block 消息：降级切分后还原为 block 列表。"""
    p = _provider(protocol="anthropic")
    original = mk(100000)
    msgs = [{"role": "user", "content": [{"type": "text", "text": original}]}]
    out = p._apply_message_split_anthropic(msgs, "m")
    assert len(out) > 1
    # 每段都是规范的 block 列表形态
    for m in out:
        assert isinstance(m["content"], list)
        assert m["content"][0]["type"] == "text"
    # 无损
    assert "".join(m["content"][0]["text"] for m in out) == original


def test_anthropic_multiblock_untouched(split_on):
    """多 block 消息（可能含 tool_use）不切。"""
    p = _provider(protocol="anthropic")
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": mk(100000)},
        {"type": "tool_result", "tool_use_id": "c1", "content": "x"},
    ]}]
    assert p._apply_message_split_anthropic(msgs, "m") is msgs


def test_anthropic_block_with_extra_fields_untouched(split_on):
    """单 block 但带 cache_control 等额外字段时不降级（保持原样，避免丢字段）。"""
    p = _provider(protocol="anthropic")
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": mk(100000), "cache_control": {"type": "ephemeral"}},
    ]}]
    assert p._apply_message_split_anthropic(msgs, "m") is msgs


def test_anthropic_disabled_untouched(split_off):
    """关闭时 anthropic 路径也零改动。"""
    p = _provider(protocol="anthropic")
    msgs = [{"role": "user", "content": [{"type": "text", "text": mk(100000)}]}]
    assert p._apply_message_split_anthropic(msgs, "m") is msgs
