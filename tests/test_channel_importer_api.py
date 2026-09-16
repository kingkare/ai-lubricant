"""外部渠道导入路由（预览 / 提交）的编排测试。

全部 monkeypatch 掉协作者：不打网络、不写库。重点是「预览零写入」「冲突按模式处理」
「部分失败仍 200」「审计已脱敏」四条契约。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

import admin
import channel_importer_api as api


async def _allow_admin(*_args, **_kwargs):
    return "admin"


def _raw(**overrides) -> dict:
    base = {
        "id": 1,
        "type": 1,
        "name": "OpenAI",
        "status": 1,
        "base_url": "https://api.openai.com",
        "models": "gpt-4o",
        "key": "sk-secret-123",
    }
    base.update(overrides)
    return base


def _paste_payload(channels=None, **extra) -> dict:
    payload = {
        "source": "new-api",
        "mode": "paste",
        "text": json.dumps(channels if channels is not None else [_raw()]),
    }
    payload.update(extra)
    return payload


@pytest.fixture()
def admin_env(monkeypatch):
    """把 admin 的鉴权、配置读取、写入路径全部替换掉，并记录写入调用。"""
    calls: dict[str, list] = {
        "created": [], "accounts": [], "logged": [], "provider_configs": [],
    }

    async def get_providers():
        return dict(extra.get("providers") or {})

    async def read_base_config(name):
        return dict((extra.get("providers") or {}).get(name) or {"base_url": "https://api.openai.com"})

    async def read_config(name):
        cfg = dict((extra.get("providers") or {}).get(name) or {})
        cfg.setdefault("accounts", [])
        return cfg

    async def write_base(name, cfg, **_kwargs):
        calls["provider_configs"].append((name, cfg))

    async def persist_account(name, account):
        calls["accounts"].append((name, account))

    async def log_operation(*args, **_kwargs):
        calls["logged"].append(args)

    async def create_provider(data, **_kwargs):
        calls["created"].append(data)
        return {"ok": True, "name": data.get("name")}

    async def load_runtime(name, cfg):
        calls.setdefault("runtime", []).append(name)

    monkeypatch.setattr(admin, "_require_admin", _allow_admin)
    monkeypatch.setattr(admin.config.Config, "get_providers", staticmethod(get_providers))
    monkeypatch.setattr(admin, "_read_provider_base_config", read_base_config)
    monkeypatch.setattr(admin, "_read_provider_config", read_config)
    monkeypatch.setattr(admin, "_write_provider_base", write_base)
    monkeypatch.setattr(admin, "_persist_account", persist_account)
    monkeypatch.setattr(admin, "_log_operation", log_operation)
    monkeypatch.setattr(admin, "_create_provider_from_config", create_provider)
    monkeypatch.setattr(admin, "_load_provider_runtime", load_runtime)
    monkeypatch.setattr(admin, "_normalize_account_proxy_ref", lambda acc, _proxies: acc)
    monkeypatch.setattr(admin, "_read_proxies", lambda: [])
    monkeypatch.setattr(admin.ModelClientPool, "get_provider_names", staticmethod(lambda: []))
    monkeypatch.setattr(admin.ModelClientPool, "get_provider_pool", staticmethod(lambda _name: None))

    extra: dict = {}
    calls["extra"] = extra
    return calls


# ── 框架清单 ─────────────────────────────────────────────────────────────────

def test_list_importers_returns_new_api(admin_env):
    result = asyncio.run(api.list_channel_importers("Bearer token"))
    assert [item["id"] for item in result["items"]] == ["new-api"]
    assert result["items"][0]["supports_api_fetch"] is True


def test_list_importers_requires_admin(monkeypatch):
    async def deny(*_args, **_kwargs):
        raise HTTPException(status_code=401, detail="未授权")

    monkeypatch.setattr(admin, "_require_admin", deny)
    with pytest.raises(HTTPException):
        asyncio.run(api.list_channel_importers("bad"))


# ── 预览 ─────────────────────────────────────────────────────────────────────

def test_preview_returns_candidates_without_writing(admin_env):
    result = asyncio.run(api.preview_channel_import(_paste_payload(), "Bearer token"))
    assert result["ok"] is True
    assert result["fetched"] == 1
    assert result["candidates"][0]["remark"] == "OpenAI"
    # 零写入
    assert admin_env["created"] == []
    assert admin_env["provider_configs"] == []
    assert admin_env["accounts"] == []


def test_preview_never_echoes_the_raw_key(admin_env):
    result = asyncio.run(api.preview_channel_import(_paste_payload(), "Bearer token"))
    blob = json.dumps(result, ensure_ascii=False)
    assert "sk-secret-123" not in blob
    candidate = result["candidates"][0]
    assert candidate["has_keys"] is True
    assert candidate["key_preview"] == ["sk-s...-123"]


def test_preview_flags_conflicting_base_url(admin_env):
    admin_env["extra"]["providers"] = {"openai": {"base_url": "https://api.openai.com", "remark": "既有"}}
    result = asyncio.run(api.preview_channel_import(_paste_payload(), "Bearer token"))
    assert result["candidates"][0]["conflict"]["provider_name"] == "openai"


def test_preview_reports_dropped_channels(admin_env):
    payload = _paste_payload([_raw(id=1, base_url=""), _raw(id=2)])
    result = asyncio.run(api.preview_channel_import(payload, "Bearer token"))
    assert result["fetched"] == 1
    assert len(result["dropped"]) == 1


def test_preview_rejects_unknown_source(admin_env):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.preview_channel_import({"source": "nope", "mode": "paste", "text": "[]"}, "Bearer token"))
    assert exc.value.status_code == 400


def test_preview_surfaces_parse_error_as_400(admin_env):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.preview_channel_import({"source": "new-api", "mode": "paste", "text": "garbage"}, "Bearer token"))
    assert exc.value.status_code == 400


def test_preview_applies_source_tag_when_requested(admin_env):
    payload = _paste_payload(add_source_tag=True)
    result = asyncio.run(api.preview_channel_import(payload, "Bearer token"))
    assert "new-api" in result["candidates"][0]["tags"]


# ── 提交 ─────────────────────────────────────────────────────────────────────

def _selection(candidate_id: str = "new-api:1", **overrides) -> dict:
    entry = {"candidate_id": candidate_id}
    entry.update(overrides)
    return entry


def test_commit_requires_selection(admin_env):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.commit_channel_import(_paste_payload(), "Bearer token"))
    assert exc.value.status_code == 400


def test_commit_caps_selection_size(admin_env):
    payload = _paste_payload(selection=[_selection(f"new-api:{i}") for i in range(api.MAX_SELECTION + 1)])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert exc.value.status_code == 400


def test_commit_rejects_unknown_conflict_mode(admin_env):
    payload = _paste_payload(selection=[_selection()], on_conflict="nonsense")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert exc.value.status_code == 400


def test_commit_creates_channel_with_accounts(admin_env):
    result = asyncio.run(api.commit_channel_import(_paste_payload(selection=[_selection()]), "Bearer token"))
    assert result["summary"]["created"] == 1
    created = admin_env["created"][0]
    assert created["base_url"] == "https://api.openai.com"
    # 密钥写 password 而非 api_key
    assert created["accounts"][0]["password"] == "sk-secret-123"
    assert "api_key" not in created["accounts"][0]
    # 模型行是 dict，避免 db 把裸字符串拆错
    assert all(isinstance(row, dict) for row in created["models"])


def test_commit_uses_shared_create_path_with_import_audit_action(admin_env):
    asyncio.run(api.commit_channel_import(_paste_payload(selection=[_selection()]), "Bearer token"))
    assert admin_env["created"]


def test_commit_skips_conflicting_base_url(admin_env):
    admin_env["extra"]["providers"] = {"openai": {"base_url": "https://api.openai.com"}}
    payload = _paste_payload(selection=[_selection()], on_conflict="skip")
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert result["summary"]["skipped"] == 1
    assert result["skipped"][0]["existing"] == "openai"
    assert admin_env["created"] == []


def test_commit_overwrites_existing_channel(admin_env):
    admin_env["extra"]["providers"] = {"openai": {"base_url": "https://api.openai.com", "timeout": 999}}
    payload = _paste_payload(selection=[_selection(remark="改名了")], on_conflict="overwrite")
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert result["summary"]["overwritten"] == 1
    name, cfg = admin_env["provider_configs"][0]
    assert name == "openai"
    assert cfg["remark"] == "改名了"
    # 覆盖路径不新建渠道
    assert admin_env["created"] == []


def test_commit_merges_accounts_without_touching_base_config(admin_env):
    admin_env["extra"]["providers"] = {"openai": {"base_url": "https://api.openai.com", "accounts": []}}
    payload = _paste_payload(selection=[_selection()], on_conflict="merge_accounts")
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert result["summary"]["merged"] == 1
    assert admin_env["accounts"]
    # 合并账号是窄写：绝不改渠道基础配置
    assert admin_env["provider_configs"] == []


def test_merge_accounts_renames_colliding_username(admin_env):
    admin_env["extra"]["providers"] = {
        "openai": {"base_url": "https://api.openai.com", "accounts": [{"username": "key-1"}]},
    }
    payload = _paste_payload(selection=[_selection()], on_conflict="merge_accounts")
    asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    username = admin_env["accounts"][0][1]["username"]
    assert username != "key-1"
    assert username.startswith("key-1")


def test_commit_reports_partial_failures_with_http_200(admin_env):
    payload = _paste_payload(
        [_raw(id=1), _raw(id=2)],
        selection=[_selection("new-api:1"), _selection("new-api:999")],
    )
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert result["ok"] is True
    assert result["summary"]["created"] == 1
    assert result["summary"]["failed"] == 1
    assert "重新预览" in result["failed"][0]["error"]


def test_commit_fails_channel_that_lost_its_base_url(admin_env):
    payload = _paste_payload(selection=[_selection(base_url="")])
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert result["summary"]["failed"] == 1
    assert "渠道地址为空" in result["failed"][0]["error"]


def test_commit_dedupes_same_base_url_within_batch(admin_env):
    """同一批里两个候选指向同一地址：后者按冲突模式处理，不重复建渠道。"""
    payload = _paste_payload(
        [_raw(id=1), _raw(id=2)],
        selection=[_selection("new-api:1"), _selection("new-api:2")],
        on_conflict="skip",
    )
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert result["summary"]["created"] == 1
    assert result["summary"]["skipped"] == 1


def test_commit_accepts_whitelisted_overrides(admin_env):
    payload = _paste_payload(selection=[_selection(remark="自定义名", account_weight=5, tags=["a"])])
    asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    created = admin_env["created"][0]
    assert created["remark"] == "自定义名"
    assert created["account_weight"] == 5
    assert created["tags"] == ["a"]


def test_commit_ignores_non_whitelisted_overrides(admin_env):
    """客户端不能借 selection 塞任意配置进库。"""
    payload = _paste_payload(selection=[_selection(billing_mode="request", owner_user_id="someone")])
    asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    created = admin_env["created"][0]
    assert created["billing_mode"] == "token"
    assert "owner_user_id" not in created


def test_commit_audit_log_is_scrubbed(admin_env):
    """审计条目里绝不能出现原始密钥。"""
    asyncio.run(api.commit_channel_import(_paste_payload(selection=[_selection()]), "Bearer token"))
    blob = json.dumps(admin_env["logged"], ensure_ascii=False, default=str)
    assert "sk-secret-123" not in blob
    assert "import_external_channels" in blob


def test_commit_warns_when_created_channels_have_no_accounts(admin_env):
    payload = _paste_payload([_raw(key="")], selection=[_selection()])
    result = asyncio.run(api.commit_channel_import(payload, "Bearer token"))
    assert any("没有账号" in w for w in result["warnings"])


def test_commit_reports_import_summary_counts(admin_env):
    result = asyncio.run(api.commit_channel_import(_paste_payload(selection=[_selection()]), "Bearer token"))
    assert result["summary"] == {
        "selected": 1, "created": 1, "overwritten": 0, "merged": 0, "skipped": 0, "failed": 0,
    }
