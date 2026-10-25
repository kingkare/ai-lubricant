"""generation/recall.py 纯函数测试（25 号文 §环节11 风格锚回灌；不碰库）。

覆盖：
1. is_golden：confirm_ref 为 dict / JSON 字符串 / 缺失 / 解析失败 四态。
2. recall_golden：golden 过滤、module 同异过滤、module=None 不过滤、
   created_at 倒序、limit 截断、原行不改。
3. build_history_slice：golden 优先于 existing 保留（超长先截 existing）、
   段落结构（英文正文）、空输入返回空串。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation.recall import (
    DEFAULT_GOLDEN_LIMIT,
    build_history_slice,
    is_golden,
    recall_golden,
)


# ── 构造数据 ──────────────────────────────────────────────────────

def _case(**overrides) -> dict:
    """一条 ``mc_test_case`` 形状的用例行（字段名与列名一致）。"""
    row = {
        "code": "TC-0001",
        "title": "amount=0 创建订单被拒",
        "intent": "验证金额字段小于等于 0 被明确拒绝",
        "module": "order",
        "origin": "ai_generated",
        "confirm_ref": {"golden": True},
        "steps": [
            {"seq": 1, "action": "以已登录态提交 amount=0 的创建订单请求",
             "expected": "请求被受理并返回明确拒绝"},
            {"seq": 2, "action": "查询订单列表", "expected": "无新订单落库"},
        ],
        "expected": {"status_code": 400, "biz_code": "PARAM_INVALID",
                     "key_fields": None, "response_time": None},
        "preconditions": ["存在可用账号 {{env.user}}", "订单服务可达"],
        "scenario_tag": "boundary",
        "created_at": datetime(2026, 10, 1, 12, 0, 0),
    }
    row.update(overrides)
    return row


_GOLDEN_HEADER = "## Golden cases (same module — imitate their style, do not copy verbatim)"
_EXISTING_HEADER = "## Existing cases (do not duplicate — dedup against these)"


# ── is_golden ─────────────────────────────────────────────────────

class TestIsGolden:
    def test_confirm_ref_as_dict(self):
        assert is_golden(_case()) is True

    def test_confirm_ref_as_json_string(self):
        row = _case(confirm_ref=json.dumps({"golden": True, "slot_key": "flow:TP-1:api:v1"}))
        assert is_golden(row) is True

    def test_confirm_ref_missing(self):
        row = _case()
        row.pop("confirm_ref")
        assert is_golden(row) is False

    def test_confirm_ref_none_and_empty(self):
        assert is_golden(_case(confirm_ref=None)) is False
        assert is_golden(_case(confirm_ref={})) is False
        assert is_golden(_case(confirm_ref="")) is False

    def test_confirm_ref_parse_failure_is_false(self):
        assert is_golden(_case(confirm_ref="{not json")) is False
        # JSON 合法但不是 object（数组）→ 也算解析不出 confirm_ref
        assert is_golden(_case(confirm_ref='[1, 2]')) is False

    def test_golden_falsy_values(self):
        assert is_golden(_case(confirm_ref={"golden": False})) is False
        assert is_golden(_case(confirm_ref={"golden": None})) is False

    def test_unmarked_row_is_false(self):
        # mark_golden 常态：confirm_ref 里是 slot_key 等其它键，无 golden
        assert is_golden(_case(confirm_ref={"slot_key": "flow:TP-1:api:v1"})) is False


# ── recall_golden ─────────────────────────────────────────────────

class TestRecallGolden:
    def test_filters_to_golden_only(self):
        rows = [
            _case(code="TC-0001", confirm_ref={"golden": True}),
            _case(code="TC-0002", confirm_ref={"golden": False}),
            _case(code="TC-0003", confirm_ref={"slot_key": "x"}),
            _case(code="TC-0004", confirm_ref=None),
        ]
        got = recall_golden(rows, module=None)
        assert [r["code"] for r in got] == ["TC-0001"]

    def test_module_filter_same_module_kept(self):
        rows = [
            _case(code="TC-0001", module="order"),
            _case(code="TC-0002", module="order", created_at=datetime(2026, 10, 2)),
        ]
        got = recall_golden(rows, module="order")
        assert [r["code"] for r in got] == ["TC-0002", "TC-0001"]

    def test_module_filter_other_module_excluded(self):
        rows = [
            _case(code="TC-0001", module="order"),
            _case(code="TC-0002", module="billing"),
            _case(code="TC-0003", module=None),
            _case(code="TC-0004", module=""),
        ]
        got = recall_golden(rows, module="order")
        assert [r["code"] for r in got] == ["TC-0001"]

    def test_module_none_means_no_filter(self):
        rows = [
            _case(code="TC-0001", module="order", created_at=datetime(2026, 10, 1)),
            _case(code="TC-0002", module="billing", created_at=datetime(2026, 10, 3)),
            _case(code="TC-0003", module=None, created_at=datetime(2026, 10, 2)),
        ]
        got = recall_golden(rows, module=None)
        # module=None → 不过滤：三个模块（含 None）都进；created_at 倒序
        assert [r["code"] for r in got] == ["TC-0002", "TC-0003", "TC-0001"]

    def test_module_empty_string_is_no_filter_too(self):
        rows = [
            _case(code="TC-0001", module="order", created_at=datetime(2026, 10, 1)),
            _case(code="TC-0002", module="billing", created_at=datetime(2026, 10, 2)),
        ]
        got = recall_golden(rows, module="")
        assert [r["code"] for r in got] == ["TC-0002", "TC-0001"]

    def test_created_at_desc_order(self):
        t0 = datetime(2026, 10, 1)
        rows = [
            _case(code="TC-0001", created_at=t0),
            _case(code="TC-0002", created_at=t0 + timedelta(days=2)),
            _case(code="TC-0003", created_at=t0 + timedelta(days=1)),
        ]
        got = recall_golden(rows, module=None)
        assert [r["code"] for r in got] == ["TC-0002", "TC-0003", "TC-0001"]

    def test_rows_without_created_at_keep_order_at_end(self):
        t0 = datetime(2026, 10, 1)
        rows = [
            _case(code="TC-0001", created_at=None),
            _case(code="TC-0002", created_at=t0),
            _case(code="TC-0003", created_at=None),
            _case(code="TC-0004", created_at=t0 + timedelta(days=1)),
            _case(code="TC-0005", created_at="garbage"),
        ]
        got = recall_golden(rows, module=None)
        # 有时间的倒序在前；无/坏时间的保持原相对顺序、排在后面
        assert [r["code"] for r in got] == ["TC-0004", "TC-0002", "TC-0001", "TC-0003", "TC-0005"]

    def test_created_at_as_iso_string(self):
        rows = [
            _case(code="TC-0001", created_at="2026-10-01T00:00:00"),
            _case(code="TC-0002", created_at="2026-10-03T00:00:00"),
        ]
        got = recall_golden(rows, module=None)
        assert [r["code"] for r in got] == ["TC-0002", "TC-0001"]

    def test_limit_truncates(self):
        rows = [
            _case(code=f"TC-{i:04d}", created_at=datetime(2026, 10, i + 1))
            for i in range(8)  # TC-0000..TC-0007, Oct1..Oct8
        ]
        got = recall_golden(rows, module=None, limit=3)
        assert [r["code"] for r in got] == ["TC-0007", "TC-0006", "TC-0005"]

    def test_default_limit_constant(self):
        rows = [
            _case(code=f"TC-{i:04d}", created_at=datetime(2026, 10, i + 1))
            for i in range(10)
        ]
        assert len(recall_golden(rows, module=None)) == DEFAULT_GOLDEN_LIMIT

    def test_limit_none_uses_default(self):
        rows = [
            _case(code=f"TC-{i:04d}", created_at=datetime(2026, 10, i + 1))
            for i in range(10)
        ]
        assert len(recall_golden(rows, module=None, limit=None)) == DEFAULT_GOLDEN_LIMIT

    def test_limit_zero_or_negative_is_empty(self):
        rows = [_case(code="TC-0001"), _case(code="TC-0002")]
        assert recall_golden(rows, module=None, limit=0) == []
        assert recall_golden(rows, module=None, limit=-1) == []

    def test_returns_copies_not_mutating_input(self):
        row = _case(code="TC-0001")
        (got,) = recall_golden([row], module=None)
        assert got is not row
        got["code"] = "MUTATED"
        assert row["code"] == "TC-0001"

    def test_none_and_non_mapping_rows_skipped(self):
        got = recall_golden([None, "junk", _case(code="TC-0001")], module=None)  # type: ignore[list-item]
        assert [r["code"] for r in got] == ["TC-0001"]

    def test_empty_input(self):
        assert recall_golden([], module="order") == []
        assert recall_golden(None, module=None) == []


# ── build_history_slice ──────────────────────────────────────────

class TestBuildHistorySlice:
    def test_empty_inputs_return_empty_string(self):
        assert build_history_slice([], [], max_chars=4000) == ""
        assert build_history_slice(None, None, max_chars=4000) == ""
        assert build_history_slice([], None, max_chars=4000) == ""
        # 有 existing 无 golden → 只有 existing 段，非空
        got = build_history_slice([], [_case()], max_chars=4000)
        assert _EXISTING_HEADER in got
        assert _GOLDEN_HEADER not in got

    def test_both_sections_present_in_order(self):
        golden = _case(code="TC-0001")
        existing = _case(code="TC-0009", confirm_ref={}, title="既有用例")
        got = build_history_slice([golden], [existing])
        assert got.index(_GOLDEN_HEADER) < got.index(_EXISTING_HEADER)

    def test_golden_section_fields(self):
        golden = _case(
            code="TC-0001",
            title="金额为 0 被拒",
            intent="验证边界值 amount=0 被明确拒绝",
            scenario_tag="boundary",
        )
        got = build_history_slice([golden], [])
        for needle in (
            "- TC-0001 — 金额为 0 被拒",
            "intent: 验证边界值 amount=0 被明确拒绝",
            "以已登录态提交 amount=0 的创建订单请求 → 请求被受理并返回明确拒绝",
            "查询订单列表 → 无新订单落库",
            "expected keys: status_code, biz_code, key_fields, response_time",
            "preconditions: 存在可用账号 {{env.user}} | 订单服务可达",
            "scenario_tag: boundary",
        ):
            assert needle in got, needle

    def test_existing_section_is_brief(self):
        existing = _case(code="TC-0009", confirm_ref={}, title="既有用例",
                         intent="已经在库里的用例")
        got = build_history_slice([], [existing])
        assert "- TC-0009 — 既有用例" in got
        assert "已经在库里的用例" in got
        # existing 不展开 golden 的风格锚字段
        assert "expected keys:" not in got
        assert "scenario_tag:" not in got
        assert "2 step(s)" in got

    def test_truncation_drops_existing_before_golden(self):
        golden = _case(code="TC-0001")
        existing = [
            _case(code=f"TC-{i:04d}", confirm_ref={}, title=f"既有用例 {i}")
            for i in range(2, 9)
        ]
        # 预算恰好容下 golden 段、容不下 existing 段
        golden_only = build_history_slice([golden], [])
        full = build_history_slice([golden], existing, max_chars=10_000)
        assert len(full) > len(golden_only) > 0
        got = build_history_slice([golden], existing, max_chars=len(golden_only))
        assert _GOLDEN_HEADER in got
        assert "- TC-0001" in got          # golden 整条保留
        assert _EXISTING_HEADER not in got  # existing 整段先丢

    def test_truncation_keeps_some_existing_when_it_fits(self):
        golden = _case(code="TC-0001")
        existing = [
            _case(code=f"TC-{i:04d}", confirm_ref={}, title=f"既有用例 {i}")
            for i in range(2, 9)
        ]
        no_golden = build_history_slice([], existing, max_chars=10_000)
        budget = len(no_golden) + len(build_history_slice([golden], [])) + 1
        got = build_history_slice([golden], existing, max_chars=budget)
        assert _GOLDEN_HEADER in got and "- TC-0001" in got
        assert _EXISTING_HEADER in got
        # 丢了一部分 existing 但没丢光
        assert "TC-0008" not in got
        assert "TC-0002" in got

    def test_truncation_finally_drops_golden_tail(self):
        goldens = [
            _case(
                code=f"TC-{i:04d}",
                title=f"风格锚用例 {i} —— 这是一个相当长的标题用来撑爆预算" * 3,
                intent="意图也写得很长很长很长，用来测试 golden 段自身的逐条截断" * 3,
            )
            for i in range(1, 5)
        ]
        got = build_history_slice(goldens, [], max_chars=900)
        assert got.startswith(_GOLDEN_HEADER)
        assert "- TC-0001" in got       # 排前的 golden 保留
        assert "- TC-0004" not in got   # 尾部先丢
        assert len(got) <= 900

    def test_max_chars_zero_or_none_returns_full(self):
        golden = _case(code="TC-0001")
        got = build_history_slice([golden], [], max_chars=0)
        assert "- TC-0001" in got

    def test_non_mapping_rows_skipped(self):
        got = build_history_slice(
            [_case(code="TC-0001"), "junk", None],  # type: ignore[list-item]
            [42, _case(code="TC-0009", confirm_ref={})],  # type: ignore[list-item]
        )
        assert "- TC-0001" in got
        assert "- TC-0009" in got
        assert "junk" not in got


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
