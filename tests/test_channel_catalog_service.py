from __future__ import annotations

import asyncio
from types import SimpleNamespace

from user_platform.marketplace import channel_catalog


def test_catalog_always_merges_custom_system_and_remote(monkeypatch):
    channel_catalog.configure_builtin_loader(lambda: [{
        "id": "demo", "name": "Demo", "description": "D", "tags": [],
        "builtin_type": "demo", "preset": {"remark": "old", "billing_mode": "token"},
    }])
    monkeypatch.setattr(channel_catalog, "_snapshot", {
        "remote_items": [{
            "id": "demo-channel", "name": "Demo 渠道", "description": "new", "tags": ["推荐"],
            "builtin_type": "demo", "preset": {"remark": "new"},
        }, {
            "id": "vendor", "name": "Vendor", "description": "V", "tags": [],
            "builtin_type": "", "preset": {"billing_mode": "request"},
        }],
        "updated_at": "now", "stale": False,
    })

    async def headers():
        return []

    monkeypatch.setattr(channel_catalog.PostgresClient, "get_header_templates", headers)
    result = asyncio.run(channel_catalog.get_catalog())
    by_id = {item["id"]: item for item in result["items"]}

    assert set(by_id) == {"custom", "code", "demo", "vendor"}
    assert by_id["demo"]["name"] == "Demo 渠道"
    assert by_id["demo"]["builtin_type"] == "demo"
    assert by_id["demo"]["preset"]["billing_mode"] == "token"
    assert by_id["demo"]["preset"]["remark"] == "new"


def _catalog_with_remote(monkeypatch, remote_items, builtin=None):
    channel_catalog.configure_builtin_loader(lambda: list(builtin or []))

    async def headers():
        return []

    monkeypatch.setattr(channel_catalog, "_snapshot", {
        "remote_items": remote_items, "updated_at": "now", "stale": False,
    })
    monkeypatch.setattr(channel_catalog.PostgresClient, "get_header_templates", headers)
    return asyncio.run(channel_catalog.get_catalog())


def test_remote_builtin_type_custom_does_not_hijack_generic_entry(monkeypatch):
    """存量脏模板：自定义供应商的 builtin_type 被回落成 'custom'。

    它必须按自己的 id 单列，不能命中「通用供应商」槽位——否则加供应商弹窗里
    通用供应商那张卡会被换成该模板（name/base_url 全被顶掉）。
    """
    result = _catalog_with_remote(monkeypatch, [{
        "id": "local.aliyun-plan", "name": "aliyun", "description": "aliyun",
        "tags": [], "builtin_type": "custom",
        "preset": {"remark": "aliyun", "base_url": "https://coding.example.com/apps/anthropic"},
    }])
    by_id = {item["id"]: item for item in result["items"]}

    assert by_id["custom"]["name"] == "通用供应商"
    assert by_id["custom"]["builtin_type"] == ""
    assert (by_id["custom"]["preset"] or {}).get("base_url") == ""
    # 模板自己仍在目录里，只是单列成一张卡。
    assert by_id["local.aliyun-plan"]["name"] == "aliyun"


def test_remote_builtin_type_code_does_not_hijack_custom_entry(monkeypatch):
    """代码模板（builtin_type='code'）不得覆盖「自定义供应商」的空白起点。

    合并会把 _code_entry() 的 EchoChannel 示例源码换成发布者的代码，之后点
    「自定义供应商」建出来的是别人的渠道而非空白模板。按 id 单列即可，
    前端仍按 builtin_type='code' 走自定义供应商分支。
    """
    result = _catalog_with_remote(monkeypatch, [{
        "id": "local.my-code", "name": "我的代码渠道", "description": "",
        "tags": [], "builtin_type": "code",
        "preset": {"remark": "我的代码渠道", "builtin_type": "code", "code": "class Mine: pass"},
    }])
    by_id = {item["id"]: item for item in result["items"]}

    assert by_id["code"]["name"] == "自定义供应商"
    assert "class EchoChannel" in by_id["code"]["preset"]["code"]
    assert by_id["local.my-code"]["preset"]["code"] == "class Mine: pass"


def test_remote_builtin_type_still_merges_into_real_builtin_entry(monkeypatch):
    """真内置类型的合并行为不变：模板更新那张卡的展示与预设，不多出一张重复卡。"""
    result = _catalog_with_remote(monkeypatch, [{
        "id": "local.cf", "name": "CF 模板", "description": "new",
        "tags": ["推荐"], "builtin_type": "cloudflare", "preset": {"timeout": 300},
    }], builtin=[{
        "id": "cloudflare", "name": "Cloudflare Workers AI", "description": "old",
        "tags": [], "builtin_type": "cloudflare", "preset": {"timeout": 120},
    }])
    by_id = {item["id"]: item for item in result["items"]}

    assert "local.cf" not in by_id
    assert by_id["cloudflare"]["name"] == "CF 模板"
    assert by_id["cloudflare"]["description"] == "new"
    assert by_id["cloudflare"]["preset"]["timeout"] == 300


def test_remote_template_id_colliding_with_system_entry_is_skipped(monkeypatch):
    """兜底：没有 builtin_type 但 id 直接叫 'custom' 的模板跳过，不占系统槽位。"""
    result = _catalog_with_remote(monkeypatch, [{
        "id": "custom", "name": "冒名顶替", "description": "", "tags": [],
        "builtin_type": "", "preset": {"base_url": "https://evil.example.com"},
    }])
    by_id = {item["id"]: item for item in result["items"]}

    assert by_id["custom"]["name"] == "通用供应商"


def test_catalog_resolves_header_name_to_local_id(monkeypatch):
    channel_catalog.configure_builtin_loader(lambda: [])
    monkeypatch.setattr(channel_catalog, "_snapshot", {
        "remote_items": [{
            "id": "vendor", "name": "Vendor", "description": "", "tags": [], "builtin_type": "",
            "preset": {"chat_protocols": [{"protocol": "openai", "path": "/v1/chat/completions", "header_template_name": "Claude"}]},
        }],
        "updated_at": "now", "stale": False,
    })

    async def headers():
        return [{"id": "header-1", "name": "Claude", "headers": {}}]

    monkeypatch.setattr(channel_catalog.PostgresClient, "get_header_templates", headers)
    result = asyncio.run(channel_catalog.get_catalog())
    vendor = next(item for item in result["items"] if item["id"] == "vendor")
    assert vendor["preset"]["chat_protocols"][0]["header_template"] == "header-1"
    assert "header_template_name" not in vendor["preset"]["chat_protocols"][0]


def test_failed_refresh_keeps_last_good_snapshot(monkeypatch):
    original = {"remote_items": [{"id": "old", "name": "Old", "description": "", "tags": [], "builtin_type": "", "preset": {}}], "updated_at": "old", "stale": False}
    monkeypatch.setattr(channel_catalog, "_snapshot", original.copy())
    monkeypatch.setattr(channel_catalog.mp_config, "settings", SimpleNamespace(
        enabled=True, modules=("channels",), github_owner="owner", github_repo="repo",
        github_branch="main", index_name="index.json",
    ))

    async def headers():
        return []

    monkeypatch.setattr(channel_catalog.PostgresClient, "get_header_templates", headers)
    channel_catalog.configure_builtin_loader(lambda: [])

    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("offline")
        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(channel_catalog.aiohttp, "ClientSession", lambda **_kwargs: BrokenSession())
    result = asyncio.run(channel_catalog.refresh(publish=False))

    assert result["ok"] is False
    assert any(item["id"] == "old" for item in result["items"])


def _authoritative_channel_manifest(item_id: str = "local.new", summary: str = "") -> dict:
    return {
        "schema": "ai-lubricant.channel-template/v1",
        "id": item_id,
        "name": item_id.removeprefix("local."),
        "display_name": "New Channel",
        "version": "1.0.0",
        "summary": summary,
        "kind": "channel_template",
        "resource": {
            "type": "channel_template",
            "channel": {
                "name": "New Channel",
                "base_url": "https://api.example.com/v1",
                "billing_mode": "token",
                "chat_protocols": [{"enabled": True, "protocol": "openai", "path": "/v1/chat/completions"}],
            },
            "freeze_policy": {"enabled": True, "rules": []},
        },
    }


def test_apply_authoritative_manifests_merges_without_raw_fetch(monkeypatch):
    channel_catalog.configure_builtin_loader(lambda: [])
    monkeypatch.setattr(channel_catalog, "_snapshot", {
        "remote_items": [
            {"id": "local.old", "name": "Old", "description": "old", "tags": [], "builtin_type": "", "preset": {}},
            {"id": "local.new", "name": "Old New", "description": "old new", "tags": [], "builtin_type": "", "preset": {}},
        ],
        "updated_at": "old",
        "stale": True,
    })

    persisted = []
    published = []

    async def set_config(_cls, key, value):
        persisted.append((key, value))

    async def headers():
        return []

    async def publish(event, scope):
        published.append((event, scope))

    monkeypatch.setattr(channel_catalog.PostgresClient, "set_config", classmethod(set_config))
    monkeypatch.setattr(channel_catalog.PostgresClient, "get_header_templates", headers)
    monkeypatch.setattr(channel_catalog, "_fetch_json", lambda *_args: (_ for _ in ()).throw(AssertionError("raw fetch is not allowed")))

    import runtime_sync
    monkeypatch.setattr(runtime_sync, "publish", publish)
    result = asyncio.run(channel_catalog.apply_authoritative_manifests([
        _authoritative_channel_manifest("local.new", "fresh"),
    ]))

    assert result["ok"] is True
    assert result["stale"] is False
    by_id = {item["id"]: item for item in result["items"] if item["id"].startswith("local.")}
    assert by_id["local.old"]["name"] == "Old"
    assert by_id["local.new"]["description"] == "fresh"
    assert persisted[0][0] == channel_catalog.SNAPSHOT_KEY
    assert persisted[0][1]["stale"] is False
    assert published == [(runtime_sync.EVENT_CHANNEL_CATALOG, "__all__")]


def test_apply_authoritative_manifests_rejects_invalid_without_mutation(monkeypatch):
    original = {
        "remote_items": [{"id": "local.old", "name": "Old", "description": "", "tags": [], "builtin_type": "", "preset": {}}],
        "updated_at": "old",
        "stale": False,
    }
    monkeypatch.setattr(channel_catalog, "_snapshot", original.copy())
    called = []

    async def set_config(_cls, *_args):
        called.append(True)

    monkeypatch.setattr(channel_catalog.PostgresClient, "set_config", classmethod(set_config))
    invalid = _authoritative_channel_manifest("local.invalid")
    invalid["resource"]["channel"]["base_url"] = ""

    try:
        asyncio.run(channel_catalog.apply_authoritative_manifests([invalid]))
    except ValueError as exc:
        assert "base_url" in str(exc)
    else:
        raise AssertionError("invalid manifest must be rejected")

    assert channel_catalog._snapshot == original
    assert called == []
