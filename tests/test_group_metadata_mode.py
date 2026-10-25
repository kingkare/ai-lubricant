"""自定义模型「模型元数据」取值模式（min/max/avg/model）聚合测试。

覆盖：compute_group_metadata 的四种模式、空/未知模式的归一化、无成员时的空返回、
以及 group_metadata 快照按 mode 取值。聚合只作用于 max_tokens / max_context_tokens，
其余字段一律取「基准成员」（max_tokens 最小者）的原值。
"""
import asyncio
from types import MappingProxyType

import pytest

import model_catalog


def _md(model_id, max_tokens, max_context_tokens=None, **extra):
    record = {"model_id": model_id, "max_tokens": max_tokens}
    if max_context_tokens is not None:
        record["max_context_tokens"] = max_context_tokens
    record.update(extra)
    return record


_METADATA = {
    "a": _md("a", 1000, 8000, input_modalities=["text"], function_calling=True),
    "b": _md("b", 2000, 12000, input_modalities=["text", "image"]),
    "c": _md("c", 3000, 16000),
}


def test_min_mode_uses_smallest_member_values():
    result = model_catalog.compute_group_metadata(["a", "b", "c"], "", "min", _METADATA)
    assert result["max_tokens"] == 1000
    assert result["max_context_tokens"] == 8000
    # 非 token 字段取基准成员（a，max_tokens 最小）原值。
    assert result["input_modalities"] == ["text"]
    assert result["function_calling"] is True


def test_max_mode_uses_largest_member_values():
    result = model_catalog.compute_group_metadata(["a", "b", "c"], "", "max", _METADATA)
    assert result["max_tokens"] == 3000
    assert result["max_context_tokens"] == 16000


def test_avg_mode_rounds_average():
    result = model_catalog.compute_group_metadata(["a", "b", "c"], "", "avg", _METADATA)
    assert result["max_tokens"] == 2000  # (1000+2000+3000)/3
    assert result["max_context_tokens"] == 12000  # (8000+12000+16000)/3


def test_model_mode_uses_named_model_verbatim():
    result = model_catalog.compute_group_metadata(["a", "b", "c"], "b", "model", _METADATA)
    assert result["max_tokens"] == 2000
    assert result["max_context_tokens"] == 12000
    assert result["input_modalities"] == ["text", "image"]
    assert "model_id" not in result


def test_aggregation_ignores_members_without_metadata():
    result = model_catalog.compute_group_metadata(["a", "missing", "c"], "", "avg", _METADATA)
    # 只对 a、c 聚合：(1000+3000)/2
    assert result["max_tokens"] == 2000


def test_no_members_with_metadata_returns_none():
    assert model_catalog.compute_group_metadata(["x", "y"], "", "min", _METADATA) is None
    assert model_catalog.compute_group_metadata([], "", "min", _METADATA) is None


def test_normalize_mode_defaults_by_metadata_model_presence():
    assert model_catalog.normalize_group_metadata_mode("", "b") == "model"
    assert model_catalog.normalize_group_metadata_mode("", "") == "min"
    assert model_catalog.normalize_group_metadata_mode(None, None) == "min"
    assert model_catalog.normalize_group_metadata_mode("MAX", "") == "max"
    assert model_catalog.normalize_group_metadata_mode("bogus", "") == "min"


def _snapshot_with_group(mode, metadata_model=""):
    source = {
        "groups": {
            "g": {
                "name": "g",
                "kind": "custom",
                "enabled": True,
                "models": ["a", "b", "c"],
                "aliases": [],
                "metadata_model": metadata_model,
                "metadata_mode": mode,
            },
        },
        "metadata": [dict(record) for record in _METADATA.values()],
        "default": {},
    }
    return model_catalog._build_snapshot(source, generation=1)


def test_snapshot_group_metadata_honors_mode():
    for mode, expected in (("min", 1000), ("max", 3000), ("avg", 2000)):
        snapshot = _snapshot_with_group(mode)
        assert snapshot.group_metadata["g"]["max_tokens"] == expected, mode


def test_snapshot_group_metadata_model_mode():
    snapshot = _snapshot_with_group("model", metadata_model="c")
    assert snapshot.group_metadata["g"]["max_tokens"] == 3000


def test_snapshot_group_metadata_frozen():
    snapshot = _snapshot_with_group("min")
    assert isinstance(snapshot.group_metadata, MappingProxyType)
    with pytest.raises(TypeError):
        snapshot.group_metadata["g"]["max_tokens"] = 1
