"""http 步骤执行器（发送请求，走代理池）：透传 url/method/headers/body/proxy，
直连空 proxy，body 截断，异常往上抛 → 引擎标 failed。不碰真实网络（mock ProxyManager）。"""
import asyncio
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from agent.workflow_exec import WorkflowStepExecutors  # noqa: E402


class _FakeResponse:
    """OutboundResponse 的最小替身：async-with + status/headers/text。"""

    def __init__(self, status=200, headers=None, text="ok"):
        self.status = status
        self.headers = headers or {}
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeProxyManager:
    def __init__(self, response=None, error=None):
        self.response = response or _FakeResponse()
        self.error = error
        self.calls: list[dict] = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _exec(monkeypatch, manager) -> WorkflowStepExecutors:
    monkeypatch.setattr(
        "providers.proxy_manager.get_proxy_manager", lambda: manager, raising=False
    )
    return WorkflowStepExecutors(owner="u1", workspace_root="unused")


@pytest.mark.asyncio
async def test_run_http_passes_proxy_and_body(monkeypatch):
    """url/method/headers/body/proxy 全量透传给 ProxyManager；返回 status/headers/body。"""
    mgr = _FakeProxyManager(_FakeResponse(status=201, headers={"X-A": "b"}, text="created"))
    ex = _exec(monkeypatch, mgr)
    out = await ex.run_http(
        {"id": "n"}, None,
        {"url": "https://x.com/api", "method": "post", "headers": {"K": "V"},
         "body": "payload", "timeout": "9", "proxy": "proxy_abc"},
    )
    assert out["status"] == 201
    assert out["body"] == "created"
    assert out["proxy"] == "proxy_abc"
    call = mgr.calls[0]
    assert call["url"] == "https://x.com/api"
    assert call["method"] == "POST"
    assert call["headers"] == {"K": "V"}
    assert call["proxy_config_id"] == "proxy_abc"
    assert call["data"] == "payload"
    assert call["timeout"].total == 9


@pytest.mark.asyncio
async def test_run_http_direct_when_proxy_empty(monkeypatch):
    """proxy 为空 → proxy_config_id=""（ProxyManager 的直连语义），不发 body。"""
    mgr = _FakeProxyManager()
    ex = _exec(monkeypatch, mgr)
    out = await ex.run_http({"id": "n"}, None, {"url": "https://x.com", "method": "GET", "proxy": ""})
    assert out["proxy"] == "direct"
    call = mgr.calls[0]
    assert call["proxy_config_id"] == ""
    assert call["data"] is None


@pytest.mark.asyncio
async def test_run_http_body_truncated(monkeypatch):
    """响应体截断到 MAX_NODE_OUTPUT_CHARS，truncated=True。"""
    from agent.workflow import MAX_NODE_OUTPUT_CHARS

    big = "x" * (MAX_NODE_OUTPUT_CHARS + 100)
    mgr = _FakeProxyManager(_FakeResponse(text=big))
    ex = _exec(monkeypatch, mgr)
    out = await ex.run_http({"id": "n"}, None, {"url": "https://x.com"})
    assert len(out["body"]) == MAX_NODE_OUTPUT_CHARS
    assert out["truncated"] is True


@pytest.mark.asyncio
async def test_run_http_missing_url_rejected(monkeypatch):
    """url 必填（validate 也会拦，执行器防御性再拦）。"""
    mgr = _FakeProxyManager()
    ex = _exec(monkeypatch, mgr)
    with pytest.raises(RuntimeError, match="url is required"):
        await ex.run_http({"id": "n"}, None, {})


@pytest.mark.asyncio
async def test_run_http_transport_error_raises(monkeypatch):
    """网络层失败往上抛（引擎标 failed）；日志不带原文。"""
    mgr = _FakeProxyManager(error=asyncio.TimeoutError())
    ex = _exec(monkeypatch, mgr)
    with pytest.raises(asyncio.TimeoutError):
        await ex.run_http({"id": "n"}, None, {"url": "https://x.com"})
