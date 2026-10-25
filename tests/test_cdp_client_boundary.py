"""CDP 授权边界：客户端选择只在授权域内、绝不盲取第一个。

这组测试锁住 cdp_bridge_plugin._resolve_cdp_client_id 的边界语义——它是「一次网页
对话只能操作它绑定的那个浏览器」这条安全保证的唯一裁决点。任何「退而取首个」的
改回都该被这里挡住。

不依赖 Postgres：授权集由 allowed_client_ids（场景/服务端注入）或 monkeypatch 的
身份推导提供。
"""

import os
import sys
import types

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

# server 模块的重依赖（simple-websocket/bottle 等）在部分环境上不可用。这些用例
# 只测纯解析逻辑、不碰 driver——但绝不能污染 sys.modules：其它测试文件要 import
# 真模块。所以先真导入，只在真导入失败时才装占位替身。
try:
    import mcp_builtin.cdp_bridge.server  # noqa: F401
except Exception:  # pragma: no cover - 依赖缺失的环境才走这条
    sys.modules["mcp_builtin.cdp_bridge.server"] = types.ModuleType(
        "mcp_builtin.cdp_bridge.server"
    )

from mcp_runtime.builtin_plugins import cdp_bridge_plugin as plugin


def resolve(**kw):
    import asyncio

    token = kw.pop("token", "tok")
    driver = kw.pop("driver", None)
    return asyncio.run(plugin._resolve_cdp_client_id(token, driver, **kw))


# ── 场景边界（allowed_client_ids = 会话绑定的那个浏览器）────────────────────

def test_scene_boundary_default_is_the_bound_client():
    """不传 client_id：默认落到会话绑定的浏览器，不看授权列表第一个。"""
    assert resolve(
        allowed_client_ids=("40",), bound_session_key="40:123",
    ) == "40"


def test_scene_boundary_allows_its_own_client():
    assert resolve(allowed_client_ids=("40",), requested_client_id="40") == "40"


def test_scene_boundary_refuses_another_browser():
    """会话绑 40、模型索要 1 → 拒绝。跨浏览器是这条边界的头号目的。"""
    with pytest.raises(ValueError, match="authorisation"):
        resolve(
            allowed_client_ids=("40",), requested_client_id="1",
            bound_session_key="40:123",
        )


def test_scene_boundary_refuses_when_domain_empty_and_no_choice():
    """场景绑定但没有可解析目标（无请求、无会话键）→ 报错，不凭空造。"""
    with pytest.raises(ValueError):
        resolve(allowed_client_ids=())


def test_single_client_domain_resolves_without_a_choice():
    """域内只有一台时，无需选择即可确定（plain agent 单浏览器的常见形态）。"""
    assert resolve(allowed_client_ids=("40",)) == "40"


# ── 无场景边界（身份 grant 列表）：可域内选，但多台且未选必须拒绝 ─────────

def test_plain_agent_picks_within_grant_list(monkeypatch):
    async def fake_bound(token):
        return ["1", "2"]

    monkeypatch.setattr(plugin, "_principal_cdp_client_ids", fake_bound)
    assert resolve(requested_client_id="2") == "2"
    with pytest.raises(ValueError):
        resolve(requested_client_id="9")  # 不在授权集合 → 拒


def test_plain_agent_without_choice_refuses_instead_of_first(monkeypatch):
    """多台授权、既无 client_id 也无会话键 → 报错，绝不 bound[0]。"""
    async def fake_bound(token):
        return ["1", "2"]

    monkeypatch.setattr(plugin, "_principal_cdp_client_ids", fake_bound)
    with pytest.raises(ValueError, match="authorisation"):
        resolve()


def test_plain_agent_session_prefix_selects_within_grant_list(monkeypatch):
    async def fake_bound(token):
        return ["1", "2"]

    monkeypatch.setattr(plugin, "_principal_cdp_client_ids", fake_bound)
    # 模型没显式选，但会话键指了 1 → 授权集内命中 1。
    assert resolve(bound_session_key="1:55") == "1"


def test_plain_agent_session_prefix_outside_grants_refused(monkeypatch):
    """会话键指向授权集外的客户端 → 拒绝（会话与授权不一致的接线错必须暴露）。"""
    async def fake_bound(token):
        return ["2", "3"]

    monkeypatch.setattr(plugin, "_principal_cdp_client_ids", fake_bound)
    with pytest.raises(ValueError):
        resolve(bound_session_key="1:55")


def test_no_grants_and_no_request_refuses(monkeypatch):
    """连授权集合都推不出来 → 报错（MCP token is not authorized）。"""
    import builtin_tool_store

    async def none_bound(token):
        return None

    async def none_owner(token):
        return None

    async def none_external(token):
        return None

    monkeypatch.setattr(plugin, "_principal_cdp_client_ids", none_bound)
    monkeypatch.setattr(plugin, "_owner_cdp_client_ids", none_owner)
    monkeypatch.setattr(builtin_tool_store, "resolve_external_cdp_client", none_external)
    with pytest.raises(ValueError):
        resolve()


# ── 连接 token（扩展自己）仍权威，但不许越过边界 ─────────────────────────

def test_connection_token_is_authoritative_within_boundary():
    class Driver:
        def authenticate_client(self, token):
            return {"client_id": "7"}

    assert resolve(token="conn", driver=Driver(), allowed_client_ids=("7", "8")) == "7"


def test_connection_token_outside_boundary_refused():
    class Driver:
        def authenticate_client(self, token):
            return {"client_id": "9"}

    with pytest.raises(ValueError, match="boundary"):
        resolve(token="conn", driver=Driver(), allowed_client_ids=("7", "8"))


def test_connection_token_with_no_boundary_is_authoritative():
    class Driver:
        def authenticate_client(self, token):
            return {"client_id": "7"}

    assert resolve(token="conn", driver=Driver()) == "7"
