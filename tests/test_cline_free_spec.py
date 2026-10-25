"""Cline 免费池代码渠道 spec（specs/cline_free_code_channel.py）的模型列表契约。

本 spec 与「自定义渠道」同源，唯一差异是模型列表来源：上游把模型分成
``recommended`` / ``free`` 两组，本渠道默认只取 ``free``。测试锁住：

1. spec 能被 code_loader 加载（普通类 + 唯一钩子 fetch_models）；
2. 分组解析：只取 free、按 id 去重、``data``/``result`` 信封与裸数组都能取到；
3. 异常路径：非 200 / 无该分组 / 无渠道地址都写 ``_last_fetch_models_error`` 并返回兜底；
4. 成功路径清空 ``_last_fetch_models_error``（否则管理端会把「上次失败」当成本次失败报 502）。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from providers.code_loader import load_code_provider_class

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "cline_free_code_channel.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/cline_free_code_channel.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")

SAMPLE = {
    "recommended": [
        {"id": "spacexai/grok-4.7", "name": "grok-4.7", "description": "", "tags": ["NEW"]},
        {"id": "anthropic/claude-opus-5", "name": "claude-opus-5", "description": "", "tags": ["NEW"]},
    ],
    "free": [
        {
            "id": "cline-free/gemini-3.8-flash",
            "name": "Gemini 3.8 Flash",
            "description": "Google's most intelligent Flash model",
            "tags": [],
        },
        {"id": "cline-free/gpt-6-astra", "name": "GPT-6 Astra", "description": "", "tags": ["NEW"]},
        # 重复 id：去重后只留第一条
        {"id": "cline-free/gemini-3.8-flash", "name": "dup", "description": "", "tags": []},
    ],
}


@pytest.fixture(scope="module")
def spec_class():
    return load_code_provider_class("cline-free", _SOURCE)


@pytest.fixture(scope="module")
def helpers():
    """spec 模块级纯函数：直接 exec 源码取函数，不经过适配器。"""
    namespace: dict = {"__name__": "spec_cline_free_helpers"}
    exec(compile(_SOURCE, str(_SPEC_PATH), "exec"), namespace)
    return namespace


# ----------------------------- 加载 -----------------------------


def test_spec_loads_with_single_hook(spec_class):
    assert spec_class.PROVIDER_NAME == "cline-free"
    assert sorted(spec_class._spec_hooks) == ["fetch_models"]
    # 渠道地址由管理端填写，不写死域名
    assert spec_class.REQUIRES_BASE_URL is True
    # 凭据是静态的，不声明自动刷新
    assert not getattr(spec_class, "SUPPORTS_TOKEN_AUTO_REFRESH", False)


# ----------------------------- 分组解析 -----------------------------


def test_free_group_only_and_dedup(helpers):
    rows = helpers["_to_model_rows"](helpers["_model_groups"](SAMPLE))
    assert [r["id"] for r in rows] == ["cline-free/gemini-3.8-flash", "cline-free/gpt-6-astra"]
    # recommended 里的付费模型不得混进来
    assert all(r["id"].startswith("cline-free/") for r in rows)
    assert rows[0]["name"] == "Gemini 3.8 Flash"
    assert rows[0]["description"] == "Google's most intelligent Flash model"
    assert rows[1]["tags"] == ["NEW"]


def test_envelope_and_bare_list_shapes(helpers):
    assert helpers["_to_model_rows"](helpers["_model_groups"]({"data": SAMPLE}))[0]["id"] == (
        "cline-free/gemini-3.8-flash"
    )
    assert helpers["_to_model_rows"](helpers["_model_groups"]({"result": SAMPLE}))[0]["id"] == (
        "cline-free/gemini-3.8-flash"
    )
    assert helpers["_to_model_rows"](helpers["_model_groups"]([{"id": "a/b"}])) == [
        {"id": "a/b", "name": "a/b"}
    ]
    # 裸字符串元素也要认（上游某些分组是纯 id 数组）
    assert helpers["_to_model_rows"](helpers["_model_groups"]({"free": ["a/b"]})) == [
        {"id": "a/b", "name": "a/b"}
    ]


def test_malformed_groups_are_ignored(helpers):
    assert helpers["_model_groups"]({"free": {"x": 1}}) == []
    assert helpers["_model_groups"]({"free": None}) == []
    assert helpers["_model_groups"]("not-a-dict") == []
    assert helpers["_to_model_rows"](helpers["_model_groups"]({"free": [1, None, {}]})) == []


# ----------------------------- 端到端（假 session） -----------------------------


class _FakeResponse:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse, recorder: dict):
        self._response = response
        self._recorder = recorder

    def get(self, url, **kwargs):
        self._recorder["url"] = url
        self._recorder["headers"] = kwargs.get("headers")
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeProvider:
    PROVIDER_NAME = "cline-free"
    base_url = "https://upstream.example.com"
    models_path = "/api/v1/models"
    proxy = None
    timeout_seconds = 30

    def __init__(self, status: int = 200, text: str = ""):
        self._response = _FakeResponse(status, text)
        self.recorder: dict = {}
        self._last_fetch_models_error = ""

    def _make_session(self):
        return _FakeSession(self._response, self.recorder)

    def _headers(self):
        return {"Authorization": "Bearer k"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"


def _fetch(spec_class, provider):
    return asyncio.run(spec_class._spec_hooks["fetch_models"](provider))


def test_fetch_models_success_uses_configured_models_path(spec_class):
    provider = _FakeProvider(200, json.dumps(SAMPLE))
    rows = _fetch(spec_class, provider)
    # 模型路径读渠道配置（models_path），不写死 /v1/models
    assert provider.recorder["url"] == "https://upstream.example.com/api/v1/models"
    assert provider.recorder["headers"] == {"Authorization": "Bearer k"}
    assert [r["id"] for r in rows] == ["cline-free/gemini-3.8-flash", "cline-free/gpt-6-astra"]
    # 成功后必须清空上次错误，否则管理端会拿旧错误报 502
    assert provider._last_fetch_models_error == ""


def test_fetch_models_http_error_records_reason(spec_class):
    provider = _FakeProvider(500, "boom")
    assert _fetch(spec_class, provider) == []
    assert "HTTP 500" in provider._last_fetch_models_error


def test_fetch_models_missing_group_records_reason(spec_class):
    """上游 200 但没有 free 分组（改了结构）→ 按错误上报，不静默返回空列表。"""
    provider = _FakeProvider(200, json.dumps({"data": {"other": []}}))
    assert _fetch(spec_class, provider) == []
    assert "free" in provider._last_fetch_models_error


def test_fetch_models_invalid_json_records_reason(spec_class):
    provider = _FakeProvider(200, "<html>not json</html>")
    assert _fetch(spec_class, provider) == []
    assert provider._last_fetch_models_error


def test_fetch_models_requires_base_url(spec_class):
    provider = _FakeProvider(200, json.dumps(SAMPLE))
    provider.base_url = ""
    assert _fetch(spec_class, provider) == []
    assert provider._last_fetch_models_error == "base_url 未配置"


def test_adapter_fetch_upstream_model_list_goes_through_hook(spec_class):
    """框架入口（fetch_upstream_model_list）必须落到 spec 钩子，而不是 CustomProvider 的
    标准 /v1/models 解析——否则 free 分组会被当成空 data 静默丢掉。"""
    from channel import Channel

    channel = Channel("cline-free", {
        "provider_name": "cline-free",
        "base_url": "https://upstream.example.com",
        "models_path": "/api/v1/models",
        "chat_protocols": [{"id": "openai-chat-0", "protocol": "openai",
                            "path": "/v1/chat/completions", "enabled": True}],
    })
    provider = spec_class(username="u", password="k", provider_name="cline-free")
    provider.attach_channel(channel)

    recorder: dict = {}
    provider._make_session = lambda *a, **k: _FakeSession(
        _FakeResponse(200, json.dumps(SAMPLE)), recorder
    )

    rows = asyncio.run(provider.fetch_upstream_model_list())
    assert recorder["url"] == "https://upstream.example.com/api/v1/models"
    assert [r["id"] for r in rows] == ["cline-free/gemini-3.8-flash", "cline-free/gpt-6-astra"]
    assert provider._last_fetch_models_error == ""
    # 认证头由框架按活跃协议行算出，spec 不重写
    assert recorder["headers"]["Authorization"] == "Bearer k"
