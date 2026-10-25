"""单组 PUT /model-groups/{name} 缺 metadata_mode/metadata_model 键的回源兜底。

回归「每次重启都把自定义模型的『模型元数据』选择强制改回最小」：

旧前端 bundle / 旧 App 在「元数据取值模式」功能上线前的构建不带这两个键。
后端单组 PUT 对 schemes/active_scheme 做了缺键回源，唯独没对 metadata_mode /
metadata_model 回源，缺键经 normalize_group_metadata_mode 静默塌回 min——重启后
旧页面只要编辑一次成员保存，用户配好的 max/avg/model 就被改成 min。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))
if str(_PROJ / "server") not in sys.path:
    sys.path.insert(0, str(_PROJ / "server"))

import admin
from db import PostgresClient


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _old_group(mode="max", mmodel=""):
    return {
        "name": "opus", "kind": "custom", "enabled": True, "remark": "",
        "models": ["m1"], "aliases": [], "provider_whitelist": [], "provider_blacklist": [],
        "selection_strategy": "intelligent", "backup_group": "", "response_model": "",
        "metadata_model": mmodel, "metadata_mode": mode, "compress_models": [],
        "metadata": {}, "schemes": [], "active_scheme": "",
    }


def _patch_common(monkeypatch, old):
    monkeypatch.setattr(admin, "_require_admin", AsyncMock(return_value="t"))
    monkeypatch.setattr(admin.PostgresClient, "get_model_group", AsyncMock(return_value=old))

    async def passthrough_write(name, group):
        # 仍走真实 normalize（不查库），验证回源值最终经 normalize 不塌缩。
        return PostgresClient.normalize_model_group(name, group) or group

    monkeypatch.setattr(admin, "_validate_model_group_write", passthrough_write)
    monkeypatch.setattr(admin, "_after_model_routing_write", AsyncMock())
    monkeypatch.setattr(admin, "_log_operation", AsyncMock())


def test_missing_metadata_keys_backfill_from_existing(monkeypatch):
    """旧客户端整组 PUT 只带 models（两个 metadata 键都缺）：保留现值，不塌回 min。"""
    old = _old_group(mode="max", mmodel="")
    captured = {}

    async def fake_upsert(name, data):
        # 真实 upsert 返回 normalize+重投影后的行；这里复用真实 normalize 模拟。
        captured["payload"] = data
        return PostgresClient.normalize_model_group(name, data) or data

    _patch_common(monkeypatch, old)
    monkeypatch.setattr(admin.PostgresClient, "upsert_model_group", fake_upsert)

    result = _run(admin.update_model_group("opus", {"name": "opus", "models": ["m1", "m2"]}))
    # 回源到 DB 现值
    assert captured["payload"]["metadata_mode"] == "max"
    assert captured["payload"]["metadata_model"] == ""
    # 真实 normalize 不再把 max 塌成 min
    assert result["model_group"]["metadata_mode"] == "max"


def test_missing_mode_with_model_mode_preserves_model(monkeypatch):
    """旧客户端在 model 模式 + 指定模型下编辑成员：mode='model' 也得保住。"""
    old = _old_group(mode="model", mmodel="qwen3.6-plus")
    captured = {}

    async def fake_upsert(name, data):
        # 真实 upsert 返回 normalize+重投影后的行；这里复用真实 normalize 模拟。
        captured["payload"] = data
        return PostgresClient.normalize_model_group(name, data) or data

    _patch_common(monkeypatch, old)
    monkeypatch.setattr(admin.PostgresClient, "upsert_model_group", fake_upsert)

    result = _run(admin.update_model_group("haiku", {"name": "haiku", "models": ["m1", "m2"]}))
    assert captured["payload"]["metadata_mode"] == "model"
    assert captured["payload"]["metadata_model"] == "qwen3.6-plus"
    assert result["model_group"]["metadata_mode"] == "model"


def test_explicit_metadata_mode_is_honored(monkeypatch):
    """新客户端显式传 metadata_mode=null（清空指定模型）+ mode：按 payload 走，不回源。"""
    old = _old_group(mode="model", mmodel="qwen3.6-plus")
    captured = {}

    async def fake_upsert(name, data):
        # 真实 upsert 返回 normalize+重投影后的行；这里复用真实 normalize 模拟。
        captured["payload"] = data
        return PostgresClient.normalize_model_group(name, data) or data

    _patch_common(monkeypatch, old)
    monkeypatch.setattr(admin.PostgresClient, "upsert_model_group", fake_upsert)

    # 新前端切回聚合 max：metadata_model=null（清空）、metadata_mode='max'
    result = _run(admin.update_model_group(
        "haiku",
        {"name": "haiku", "models": ["m1"], "metadata_model": None, "metadata_mode": "max"},
    ))
    assert captured["payload"]["metadata_mode"] == "max"
    assert result["model_group"]["metadata_mode"] == "max"
