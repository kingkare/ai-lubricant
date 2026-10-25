"""压缩选路（compression_candidates → 选路层）行为测试。

设计（2026-10 定稿）：
- 压缩模型列表挂在**被压缩的模型**上（model_groups.compress_models），非 api_key。
- 命中压缩时把候选列表透传给选路层：`_get_available_client_with_provider` 走压缩专用
  分支——候选改为压缩列表、关本地窗口过滤（上游用真实窗口裁决）、按窗口从大到小选。
- 上游报错直接报错，不回退原模型（两条线路）。

本文件覆盖选路层压缩分支与 _select_largest_context；识别指纹见 test_compaction_detect.py；
窗口过滤与归因见 test_input_window_fallback.py。
"""

import asyncio
import sys
from pathlib import Path
from types import MappingProxyType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import config
import model_catalog
import rate_limiter
from rate_limiter import ModelClientPool


# ── _select_largest_context：按窗口从大到小，未知窗口垫底 ───────────────────

def _candidate(routed_model: str, window: int, priority: int = 0, weight: int = 1) -> dict:
    route_info = {"routed_model": routed_model}
    if window > 0:
        route_info["extra_config"] = {"max_context_tokens": window}
    return {
        "provider_name": f"prov-{routed_model}",
        "account_client": type(
            "AC",
            (),
            {"username": "u1", "priority": priority, "weight": weight},
        )(),
        "routed_model": routed_model,
        "upstream_model_id": routed_model,
        "route_info": route_info,
    }


def test_select_largest_context_prefers_biggest_window(monkeypatch):
    monkeypatch.setattr(model_catalog, "current_snapshot", _snap)
    candidates = [_candidate("small-8k", 8000), _candidate("big-200k", 200000), _candidate("mid-32k", 32000)]
    picked = asyncio.run(ModelClientPool._select_largest_context(candidates, None, None))
    assert picked["routed_model"] == "big-200k"


def test_select_largest_context_unknown_window_sorts_last(monkeypatch):
    """窗口未知（0）的候选排在已知窗口之后，避免盲选。"""
    monkeypatch.setattr(model_catalog, "current_snapshot", _snap)
    candidates = [_candidate("unknown", 0), _candidate("known-1k", 1000)]
    picked = asyncio.run(ModelClientPool._select_largest_context(candidates, None, None))
    assert picked["routed_model"] == "known-1k"


def test_select_largest_context_metadata_fallback(monkeypatch):
    """渠道行没声明窗口时，回落到模型元数据的 max_context_tokens。"""
    snap = model_catalog.ModelCatalogSnapshot(
        generation=1,
        fingerprint="test",
        groups=MappingProxyType({}),
        group_index=MappingProxyType({}),
        metadata=MappingProxyType({"meta-big": {"max_context_tokens": 500000}}),
        default=MappingProxyType({}),
        group_metadata=MappingProxyType({}),
    )
    monkeypatch.setattr(model_catalog, "current_snapshot", lambda: snap)
    candidates = [_candidate("small-8k", 8000), _candidate("meta-big", 0)]
    picked = asyncio.run(ModelClientPool._select_largest_context(candidates, None, None))
    assert picked["routed_model"] == "meta-big"


def _snap():
    return model_catalog.ModelCatalogSnapshot(
        generation=1, fingerprint="test",
        groups=MappingProxyType({}),
        group_index=MappingProxyType({}),
        metadata=MappingProxyType({}),
        default=MappingProxyType({}),
        group_metadata=MappingProxyType({}),
    )


def test_select_largest_context_empty(monkeypatch):
    monkeypatch.setattr(model_catalog, "current_snapshot", _snap)
    assert asyncio.run(ModelClientPool._select_largest_context([], None, None)) is None


# ── 压缩分支：候选改用压缩列表 + 窗口过滤关闭 + 大窗口选择器 ────────────────

def test_compression_branch_overrides_routing():
    """压缩分支的三个不变量（源码级锁定，避免 mock 整条异步管线）。

    行为不变量：compression_candidates 非空时 → ①候选改用压缩列表；②窗口过滤清零
    （让上游裁决）；③选择器换为 _select_largest_context（按窗口从大到小）。
    这三点是压缩选路的核心，任何重构都必须保留，故用源码结构断言锁死。
    """
    import inspect
    src = inspect.getsource(ModelClientPool._get_available_client_with_provider)
    assert "if compression_candidates:" in src
    assert "input_token_estimate = 0" in src
    assert "select_fn = cls._select_largest_context" in src
    assert "candidate_models = [" in src
    # 普通路径仍走 key 的策略映射表（压缩分支在其之后覆盖，不影响非压缩请求）
    assert "strategy_methods" in src


# ── config.get_model_group_compress_models：从 model_groups 行读取 ──────────

def test_get_model_group_compress_models_from_custom_group(monkeypatch):
    """自定义模型行的 compress_models 读出来（去空去重保序）。"""
    group = {"name": "opus", "kind": "custom", "compress_models": ["flash-a", "", "flash-a", "flash-b"]}
    snap = model_catalog.ModelCatalogSnapshot(
        generation=1, fingerprint="t",
        groups=MappingProxyType({"opus": group}),
        group_index=MappingProxyType({"opus": group}),
        metadata=MappingProxyType({}), default=MappingProxyType({}),
        group_metadata=MappingProxyType({}),
    )
    result = asyncio.run(config.Config.get_model_group_compress_models("opus", snapshot=snap))
    assert result == ["flash-a", "flash-b"]


def test_get_model_group_compress_models_from_real_row(monkeypatch):
    """真实模型行（kind='real'，不在 group_index）直接读快照 groups。"""
    row = {"name": "gpt-5", "kind": "real", "compress_models": ["cheap-1"]}
    snap = model_catalog.ModelCatalogSnapshot(
        generation=1, fingerprint="t",
        groups=MappingProxyType({"gpt-5": row}),
        group_index=MappingProxyType({}),  # real 行不进 group_index
        metadata=MappingProxyType({}), default=MappingProxyType({}),
        group_metadata=MappingProxyType({}),
    )
    result = asyncio.run(config.Config.get_model_group_compress_models("gpt-5", snapshot=snap))
    assert result == ["cheap-1"]


def test_get_model_group_compress_models_empty_when_unset():
    snap = model_catalog.ModelCatalogSnapshot(
        generation=1, fingerprint="t",
        groups=MappingProxyType({}), group_index=MappingProxyType({}),
        metadata=MappingProxyType({}), default=MappingProxyType({}),
        group_metadata=MappingProxyType({}),
    )
    assert asyncio.run(config.Config.get_model_group_compress_models("nope", snapshot=snap)) == []
    assert asyncio.run(config.Config.get_model_group_compress_models(None, snapshot=snap)) == []
