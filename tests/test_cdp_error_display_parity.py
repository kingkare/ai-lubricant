"""③ 错误口径三端一致：错误独立成 error 字段，绝不粘进正文。

Web 早已如此（``message.error`` + 「错误：{error}」独立行，不并入 content）。CDP
原先把错误文本 ``content += errMsg`` / push 成 text part——刷新后历史回放把报错当
正文重复渲染，且与 Web 的形态分叉。这里用源码断言钉住 CDP 已改齐。

CDP content.js 无构建步骤（chrome://extensions 直接加载），改错了没有编译期护栏，
所以这层静态断言是它唯一的回归网。
"""
from __future__ import annotations

from pathlib import Path

_CONTENT_JS = (
    Path(__file__).resolve().parent.parent
    / "mcp_builtin" / "cdp_bridge" / "tmwd_cdp_bridge" / "content.js"
)


def _src() -> str:
    return _CONTENT_JS.read_text(encoding="utf-8")


def test_live_error_sets_error_field_not_content():
    src = _src()
    # error 分支写 assistant.error。
    assert "assistant.error = errMsg;" in src
    # 不再把错误粘进正文/parts。
    assert "assistant.content = (assistant.content || '') + errMsg;" not in src
    assert "assistant.parts.push({ type: 'text', text: errMsg });" not in src


def test_live_aborted_sets_error_field():
    src = _src()
    assert "assistant.error = '已中止';" in src
    assert "assistant.parts.push({ type: 'text', text: '（已中止）' });" not in src


def test_loaded_messages_restore_error_field():
    """mapLoadedMessages 把落库的 error 列回填到 m.error，刷新后独立行 == 实时。"""
    src = _src()
    assert "error: m.error || ''," in src


def test_render_messages_shows_error_line():
    """渲染出「错误：…」独立行，且不在正文里（有 .msg-error 样式）。"""
    src = _src()
    assert "errEl.textContent = '错误：' + m.error;" in src
    assert ".msg-error{" in src
