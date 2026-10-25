"""模型元数据窗口字段（max_context_tokens / max_tokens）的增量保护。

model_groups 的 metadata JSONB 是整块替换（ON CONFLICT SET metadata=EXCLUDED.metadata）：
批量导入 / 自动导入的负载常常不含窗口字段（目录来源不提供），整块替换会把管理员
手填的值抹成空（现网反馈「模型元数据被清空」的根因）。
_merge_window_fields 在替换前把库内旧值回填进负载：负载显式带有效值才更新。
单条 upsert_real_model_metadata 与 bulk_upsert_real_model_metadata 共用同一实现。
"""
import pytest

from db import PostgresClient


class TestMergeWindowFields:
    def test_missing_fields_backfilled_from_existing(self):
        """负载没带窗口字段 → 从库内旧值回填（增量，不清空）。"""
        effective = PostgresClient._merge_window_fields(
            {"name": "m1"}, {"max_context_tokens": 400000, "max_tokens": 32000},
        )
        assert effective["max_context_tokens"] == 400000
        assert effective["max_tokens"] == 32000
        assert effective["name"] == "m1"

    def test_explicit_values_win(self):
        """负载显式带了有效值 → 用负载的（显式更新仍生效）。"""
        effective = PostgresClient._merge_window_fields(
            {"max_context_tokens": 128000}, {"max_context_tokens": 400000},
        )
        assert effective["max_context_tokens"] == 128000

    def test_partially_set_only_fills_missing(self):
        """只带了 max_tokens → 只补缺失的 max_context_tokens，不碰已带的。"""
        effective = PostgresClient._merge_window_fields(
            {"max_tokens": 8192}, {"max_context_tokens": 400000, "max_tokens": 32000},
        )
        assert effective["max_context_tokens"] == 400000
        assert effective["max_tokens"] == 8192

    def test_zero_or_empty_payload_does_not_blank_existing(self):
        """负载带了 0 / 空串 / None（上游「未知」占位）→ 不覆盖库内有效值。"""
        for blank in (0, "", None):
            effective = PostgresClient._merge_window_fields(
                {"max_context_tokens": blank}, {"max_context_tokens": 400000},
            )
            assert effective["max_context_tokens"] == 400000

    def test_existing_metadata_corrupt_returns_payload(self):
        """库内 metadata 是脏数据（非 dict）→ 原样返回负载，不抛异常。"""
        for corrupt in (None, "not-json", "[1,2]"):
            effective = PostgresClient._merge_window_fields(
                {"max_context_tokens": 128000}, corrupt,
            )
            assert effective == {"max_context_tokens": 128000}

    def test_both_empty_stays_empty(self):
        """负载和库内都没有窗口 → 不凭空造键。"""
        effective = PostgresClient._merge_window_fields({"name": "m1"}, {})
        assert "max_context_tokens" not in effective
        assert "max_tokens" not in effective
