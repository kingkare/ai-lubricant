"""渠道模型输入窗口兜底过滤补测。

背景：``_collect_candidates`` 原先只读渠道行 ``extra_config.max_context_tokens`` 做输入窗口
过滤；未声明时直接放行——但这会把「渠道没设、可模型元数据声明了上下文窗口」的请求送
到装不下的渠道，发过去必然 context_too_large。修复后：渠道行声明优先，未声明时回落到
模型元数据 ``max_context_tokens`` 兜底。本文件聚焦该兜底与优先级。
"""
import asyncio
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import model_catalog
from rate_limiter import ModelClientPool


async def _async_true():
    return True


class _StubAccountClient:
    def __init__(self, username, provider_name="p1"):
        self.username = username
        self.weight = 1
        self.priority = 0
        self._cooldown_until = 0
        self.is_frozen = False
        self.disabled = False
        self._requests = []
        self._token_usages = []
        self.rpm_limit = 0
        self.tpm_limit = 0
        self.provider = type("P", (), {"PROVIDER_NAME": provider_name})()
        self.last_route_info = {}

    def cooldown_remaining(self, now=None):
        return 0

    def is_model_frozen(self, model_id):
        return False

    @property
    def daily_requests_remaining(self):
        return None

    @property
    def daily_requests_limit(self):
        return None

    @property
    def model_daily_remaining(self):
        return {}

    async def can_accept(self, model, messages):
        return True

    async def reserve(self, model, messages):
        return True

    async def release(self):
        return None


def _channel_stub(enabled=True, tags=()):
    ch = type("Ch", (), {})()
    ch.enabled = enabled
    ch.tags = frozenset(tags)
    ch.get_chat_protocol_candidates = lambda model, protocol: []
    return ch


def _snapshot_with_metadata(metadata: dict):
    """构造带 metadata 的 catalog 快照（group_metadata 留空）。"""
    return model_catalog.ModelCatalogSnapshot(
        generation=1,
        fingerprint="test",
        groups=MappingProxyType({}),
        group_index=MappingProxyType({}),
        metadata=MappingProxyType(metadata),
        default=MappingProxyType({}),
        group_metadata=MappingProxyType({}),
    )


def _patch_pool(monkeypatch, rows, *, provider="p1", snapshot=None):
    """装一个最小 pool + 必要的类方法桩，让 _collect_candidates 能跑完。

    rows: 该渠道的 provider_models 行（dict），含 extra_config 等。
    """
    acct = _StubAccountClient("u1")
    pool = type("Pool", (), {})()
    pool.clients = [acct]
    pool.channel = _channel_stub(True)
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {provider: pool})

    def iter_candidates(cls, model_id, include_disabled=False):
        for r in rows:
            if r.get("model_id") == model_id or r.get("upstream_model_id") == model_id:
                yield r["provider"], cls._provider_pools.get(r["provider"]), r

    monkeypatch.setattr(ModelClientPool, "iter_model_candidates", classmethod(iter_candidates))
    monkeypatch.setattr(ModelClientPool, "has_model_route", classmethod(lambda cls, m: True))
    monkeypatch.setattr(ModelClientPool, "all_model_ids", classmethod(lambda cls: {"m1"}))
    monkeypatch.setattr(ModelClientPool, "_model_tpm_available", classmethod(lambda cls, m: _async_true()))
    monkeypatch.setattr(
        ModelClientPool, "_route_supports_operation",
        classmethod(lambda cls, route, model, op, snapshot=None: _async_true()),
    )
    monkeypatch.setattr(ModelClientPool, "_model_provider_quota_available", classmethod(lambda cls, m, p: True))
    monkeypatch.setattr(ModelClientPool, "_provider_supports_operation", classmethod(lambda cls, provider, name, op: True))
    if snapshot is not None:
        monkeypatch.setattr(model_catalog, "current_snapshot", lambda: snapshot)


def _run(model_id, snapshot, input_token_estimate):
    return asyncio.run(ModelClientPool._collect_candidates(
        model_id, [model_id], [], [], None,
        set(), set(), set(), set(),
        snapshot=snapshot, input_token_estimate=input_token_estimate,
    ))


def test_falls_back_to_metadata_when_channel_does_not_declare(monkeypatch):
    """渠道行未声明 max_context_tokens 时，按模型元数据 max_context_tokens 兜底过滤。"""
    snap = _snapshot_with_metadata({"m1": {"max_context_tokens": 100}})
    rows = [{"provider": "p1", "model_id": "m1", "upstream_model_id": "m1", "extra_config": {}}]
    _patch_pool(monkeypatch, rows, snapshot=snap)

    # 输入超过元数据窗口 → 被兜底过滤
    candidates, _, skip = _run("m1", snap, input_token_estimate=200)
    assert candidates == []
    assert skip.get("context_window_exceeded") == 1

    # 输入未超元数据窗口 → 放行
    candidates, _, skip = _run("m1", snap, input_token_estimate=50)
    assert len(candidates) == 1
    assert "context_window_exceeded" not in skip


def test_channel_declaration_overrides_metadata(monkeypatch):
    """渠道行显式声明的 max_context_tokens 优先于模型元数据（更大或更小都按渠道算）。"""
    snap = _snapshot_with_metadata({"m1": {"max_context_tokens": 100}})

    # 渠道声明更大（300）→ 即使元数据是 100，200 的输入放行
    rows = [{"provider": "p1", "model_id": "m1", "upstream_model_id": "m1",
             "extra_config": {"max_context_tokens": 300}}]
    _patch_pool(monkeypatch, rows, snapshot=snap)
    candidates, _, skip = _run("m1", snap, input_token_estimate=200)
    assert len(candidates) == 1
    assert "context_window_exceeded" not in skip

    # 渠道声明更小（50）→ 即使元数据是 100，80 的输入被过滤（渠道声明为准）
    rows = [{"provider": "p1", "model_id": "m1", "upstream_model_id": "m1",
             "extra_config": {"max_context_tokens": 50}}]
    _patch_pool(monkeypatch, rows, snapshot=snap)
    candidates, _, skip = _run("m1", snap, input_token_estimate=80)
    assert candidates == []
    assert skip.get("context_window_exceeded") == 1


def test_no_limit_anywhere_means_no_filter(monkeypatch):
    """渠道行和模型元数据都没有 max_context_tokens 时，不过滤（保持原行为）。"""
    snap = _snapshot_with_metadata({"m1": {}})  # 元数据无窗口
    rows = [{"provider": "p1", "model_id": "m1", "upstream_model_id": "m1", "extra_config": {}}]
    _patch_pool(monkeypatch, rows, snapshot=snap)

    candidates, _, skip = _run("m1", snap, input_token_estimate=999999)
    assert len(candidates) == 1
    assert "context_window_exceeded" not in skip


def test_zero_input_estimate_skips_filter(monkeypatch):
    """input_token_estimate=0 表示不做窗口过滤（探测/估算关闭路径）。"""
    snap = _snapshot_with_metadata({"m1": {"max_context_tokens": 100}})
    rows = [{"provider": "p1", "model_id": "m1", "upstream_model_id": "m1", "extra_config": {}}]
    _patch_pool(monkeypatch, rows, snapshot=snap)

    candidates, _, skip = _run("m1", snap, input_token_estimate=0)
    assert len(candidates) == 1
    assert "context_window_exceeded" not in skip


def test_metadata_fallback_uses_routed_model_not_upstream(monkeypatch):
    """兜底按 routed_model 取元数据；upstream_model_id 不同名时不误用。"""
    # 元数据键是 public model id "m1"，upstream 是 "upstream-m1"
    snap = _snapshot_with_metadata({"m1": {"max_context_tokens": 100}})
    rows = [{"provider": "p1", "model_id": "m1", "upstream_model_id": "upstream-m1",
             "extra_config": {}}]
    _patch_pool(monkeypatch, rows, snapshot=snap)

    candidates, _, skip = _run("m1", snap, input_token_estimate=200)
    assert candidates == []
    assert skip.get("context_window_exceeded") == 1


# ── 归因：窗口超限的主导原因必须归 context_too_large（不是 no_available_account）────
def test_dominant_reason_maps_context_window_to_context_too_large():
    """窗口超限是主导原因时归 context_too_large —— 供终端收口成 400 而非 429 重试。"""
    from rate_limiter import _dominant_reason_code

    code, _ = _dominant_reason_code({"context_window_exceeded": 13})
    assert code == "context_too_large"


def test_dominant_reason_context_window_beats_lesser_reasons():
    """窗口超限计数最大时压过其它次要原因。"""
    from rate_limiter import _dominant_reason_code

    code, _ = _dominant_reason_code({"context_window_exceeded": 5, "account_cooldown": 2})
    assert code == "context_too_large"


def test_dominant_reason_other_top_still_no_available():
    """非窗口主导时仍归 no_available_account（不误伤既有归因）。"""
    from rate_limiter import _dominant_reason_code

    code, _ = _dominant_reason_code({"account_cooldown": 9, "context_window_exceeded": 1})
    assert code == "no_available_account"


def test_no_available_account_error_carries_context_too_large_code():
    """NoAvailableAccountError 携带 context_too_large code（终端据此转 400）。"""
    from rate_limiter import NoAvailableAccountError, _dominant_reason_code

    code, _ = _dominant_reason_code({"context_window_exceeded": 1})
    err = NoAvailableAccountError(status_code=429, detail="x", code=code)
    assert err.code == "context_too_large"
    assert err.status_code == 429  # 抛出仍是 429；终端 handler 按 code 收口成 400


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
