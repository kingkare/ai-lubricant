"""dsh 接入 + MCP server + 对话后端测试（plans/10-dsh-integration.md 待做项）。

覆盖（不真起 dsh 子进程 —— 那需要网关 + 凭据，属探针范畴）：

1. **agent_runtime**：cordis patch 生成（stdio/streamable-http 形态）、
   ``DshConfig.to_sdk_config`` 字段映射、``notification_to_event`` 归一。
2. **MCP server**（Streamable HTTP）：initialize / tools/list（数量=注册表）/
   tools/call（成功信封 + 未注册工具的 isError）/ ping / 未知方法 → -32601 /
   解析错误 → -32700 / 客户端通知 → 202。
3. **chat 后端**：health 探活；未登录 → 401；空 prompt → 422；SSE 信封形态。

运行：``.venv/Scripts/python.exe -m pytest tests/test_dsh_chat_mcp.py -q``
（需要主 .venv —— deepseek_harness SDK 装在那边）
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

import httpx  # noqa: E402

from testing_platform import agent_runtime  # noqa: E402
from testing_platform.surface.api import router as testing_router  # noqa: E402
from testing_platform.surface import methods as surface_methods  # noqa: E402


# ── 1) agent_runtime ────────────────────────────────────────────────────

def test_cordis_patch_shape(tmp_path):
    """cordis patch 是合法的 insert 列表，指向 streamable-http + 我们的 URL。"""
    path = agent_runtime.write_cordis_patch(
        mcp_url="http://127.0.0.1:8011/api/v1/testing/mcp", dest_dir=tmp_path
    )
    text = path.read_text(encoding="utf-8")
    assert "- insert:" in text
    assert "name: '@deepseek-ai/dsh-mcp-client'" in text
    assert "serverName: testing" in text
    assert "transport: streamable-http" in text
    assert "url: http://127.0.0.1:8011/api/v1/testing/mcp" in text
    # 无 token → headers 空对象
    assert "headers:\n          {}" in text


def test_cordis_patch_headers_with_token(tmp_path):
    path = agent_runtime.write_cordis_patch(
        mcp_url="http://x/mcp", mcp_token="sk-abc", dest_dir=tmp_path
    )
    assert 'Authorization: "Bearer sk-abc"' in path.read_text(encoding="utf-8")


def test_build_config_maps_fields(tmp_path, monkeypatch):
    """build_config 把 gateway/模型/MCP patch 都落到 DshConfig。"""
    monkeypatch.setattr(agent_runtime, "DEFAULT_DSH_HOME", str(tmp_path))
    cfg = agent_runtime.build_config(
        api_key="sk-test", model="claude-haiku-4-5-20251001",
        mcp_url="http://127.0.0.1:8011/api/v1/testing/mcp",
    )
    assert cfg.api_key == "sk-test"
    assert cfg.model == "claude-haiku-4-5-20251001"
    assert cfg.base_url == agent_runtime.DEFAULT_BASE_URL
    assert len(cfg.patches) == 1
    assert cfg.patches[0].endswith(".cordis.patch.yml")
    assert Path(cfg.patches[0]).is_file()


def test_to_sdk_config_requires_sdk():
    """缺 SDK 时给可读的错误（含安装命令），不是 ImportError 裸抛。"""
    cfg = agent_runtime.DshConfig(api_key="sk-x")
    try:
        sdk = cfg.to_sdk_config()
    except agent_runtime.DshRuntimeError as exc:
        assert "deepseek-harness-sdk" in str(exc)
    else:
        # SDK 装了 → 字段映射正确
        assert sdk.api_key == "sk-x"
        assert sdk.provider == "deepseek-official"


def test_notification_to_event_normalizes():
    """Notification → SSE 事件：session.event 拆内层；status 归一；其它透传。"""
    class N:
        def __init__(self, method, payload):
            self.method = method
            self.payload = payload

    ev = agent_runtime.notification_to_event(N("session.event", {
        "sessionId": "s1",
        "event": {"type": "assistant/message", "data": {"message": {"content": []}}},
    }))
    assert ev["method"] == "session.event"
    assert ev["type"] == "assistant/message"
    assert "message" in ev["data"]

    st = agent_runtime.notification_to_event(N("session.status", {"sessionId": "s1", "status": "idle"}))
    assert st["type"] == "session/status"
    assert st["data"]["status"] == "idle"

    other = agent_runtime.notification_to_event(N("custom/thing", {"a": 1}))
    assert other["type"] == "custom/thing"
    assert other["data"] == {"a": 1}


# ── 2) MCP server（Streamable HTTP）──────────────────────────────────────

@pytest_asyncio.fixture
async def mcp_client():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(testing_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def _rpc(client, method, params=None, req_id=1):
    return await client.post("/api/v1/testing/mcp", json={
        "jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {},
    })


@pytest.mark.asyncio
async def test_mcp_initialize(mcp_client):
    r = await _rpc(mcp_client, "initialize", {"protocolVersion": "2024-11-05"})
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    result = body["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "testing-platform-mcp"
    assert result["capabilities"]["tools"]


@pytest.mark.asyncio
async def test_mcp_tools_list_matches_registry(mcp_client):
    r = await _rpc(mcp_client, "tools/list")
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == set(surface_methods.default_method_registry().names())
    # 形态：name/description/inputSchema
    sample = tools[0]
    assert sample["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_mcp_tools_call_ok_and_error(mcp_client):
    ok = await _rpc(mcp_client, "tools/call", {"name": "tool.list", "arguments": {}})
    result = ok.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert "tools" in payload

    # 未注册工具 → 结果内 isError（不是传输层错误）
    bad = await _rpc(mcp_client, "tools/call", {"name": "nope.missing", "arguments": {}})
    r2 = bad.json()["result"]
    assert r2["isError"] is True
    assert r2["structuredContent"]["code"] == "method_not_found"


@pytest.mark.asyncio
async def test_mcp_ping_and_unknown_method(mcp_client):
    ping = await _rpc(mcp_client, "ping")
    assert ping.json()["result"] == {}

    unknown = await _rpc(mcp_client, "does/not/exist")
    err = unknown.json()["error"]
    assert err["code"] == -32601


@pytest.mark.asyncio
async def test_mcp_notification_gets_202(mcp_client):
    """无 id 的客户端通知（notifications/initialized）→ 202 无体。"""
    r = await mcp_client.post("/api/v1/testing/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    })
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_mcp_parse_error(mcp_client):
    r = await mcp_client.post(
        "/api/v1/testing/mcp", content=b"not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_mcp_get_self_describe(mcp_client):
    r = await mcp_client.get("/api/v1/testing/mcp")
    body = r.json()
    assert body["tool_count"] == len(surface_methods.default_method_registry().names())
    assert "streamable-http" in body["transport"]


# ── 3) chat 后端 ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_health(mcp_client):
    r = await mcp_client.get("/api/v1/testing/chat/health")
    assert r.status_code == 200
    body = r.json()
    assert "sdk_installed" in body
    assert body["mcp_endpoint"].startswith("/api/v1/testing/mcp")
    assert body["default_model"]


@pytest.mark.asyncio
async def test_chat_send_requires_auth(mcp_client):
    """无 session cookie / 无管理员 Bearer → 401（不发请求给 dsh）。"""
    r = await mcp_client.post("/api/v1/testing/chat/send", json={
        "api_key_id": 1, "message": "hi",
    })
    assert r.status_code == 401


def test_build_prompt_uses_recent_history():
    from testing_platform.surface.chat import _build_prompt

    prompt = _build_prompt(
        [{"role": "user", "content": "上一句"}, {"role": "assistant", "content": "上一答"}],
        "这一句",
    )
    assert "[user] 上一句" in prompt
    assert "[assistant] 上一答" in prompt
    assert prompt.endswith("这一句")


def test_build_prompt_injects_project_context():
    from testing_platform.surface.chat import _build_prompt

    prompt = _build_prompt([], "为登录接口设计用例", project_id="abc-123")
    assert "[上下文] 当前测试项目 project_id=abc-123" in prompt
    assert "为登录接口设计用例" in prompt


def test_build_prompt_empty():
    from testing_platform.surface.chat import _build_prompt

    assert _build_prompt([], None) == ""
    assert _build_prompt([{"role": "user", "content": ""}], None) == ""


def test_chat_routes_registered():
    """路由挂在 surface router 下（相对前缀 /chat），最终路径 /api/v1/testing/chat/*。"""
    paths = {r.path for r in testing_router.routes}
    assert "/api/v1/testing/chat/send" in paths
    assert "/api/v1/testing/chat/health" in paths
    assert "/api/v1/testing/mcp" in paths
