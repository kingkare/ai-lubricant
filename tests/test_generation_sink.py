"""问题库回流「统一 sink」写入器（25 号文 v4.1 §环节11；P2）测试。

被测面 = :mod:`testing_platform.generation.defect_flow` 的 §环节11 尾部新增：

* :data:`SINK_KINDS` 五类齐（文档字面量 ``reason/reject`` ↔ 落库定版
  ``reject_reason`` 的对照由 :data:`SINK_DOC_KIND_ALIASES` /
  :func:`canonicalize_sink_kind` 给出）；
* :func:`make_sink_entries` 批量写入器——四类**写入者**（门② REJECT /
  门① 打回 / bug 结案 / 返工教训）+ ``pitfall`` / ``risk_scenario`` 两个直写
  入口 → ``mc_test_question`` 行形状 dict 列表；
* 纯函数不碰 DB（defect_flow 不 import tortoise），全部用构造数据。

与 ``tests/test_generation_defect_flow.py`` 的分工：那边测**单条**造形
（``make_sink_entry``）与 surface 真写（``case.reject_sink`` 走 SQLite）；
这边测**批量转写**（``make_sink_entries``）与 kind/source 口径，不建库。

运行：``python -m pytest tests/test_generation_sink.py -q``
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation import defect_flow as df


# ════════════════════════════════════════════════════════════════
# (a) 五类 kind 齐 —— 文档五类的落库枚举一个都不能少
# ════════════════════════════════════════════════════════════════

def test_sink_kinds_cover_the_five_doc_kinds():
    """§环节11 点名的五类 kind 全在 :data:`SINK_KINDS` 里。"""
    assert set(df.SINK_KINDS) == {
        "reject_reason",   # 文档字面量 reason/reject（门② REJECT / 门① 打回）
        "bug_rootcase",    # bug 结案根因
        "rework_lesson",   # 返工教训
        "pitfall",         # 踩过的坑
        "risk_scenario",   # 风险场景
    }
    assert len(df.SINK_KINDS) == 5


def test_doc_kind_literals_resolve_to_stored_enums():
    """文档写法 ``reason/reject`` 归一到定版 ``reject_reason``，其余四类原样。

    25 号文写的是 ``reason/reject``（带斜杠），任务定版成 ``reject_reason``——
    五类**不缺**，差的只是拼法，故在入侧归一而不是扩表（歧义记录 10）。
    """
    assert df.canonicalize_sink_kind("reason/reject") == "reject_reason"
    assert df.canonicalize_sink_kind("REJECT") == "reject_reason"
    assert df.canonicalize_sink_kind(" reject_reason ") == "reject_reason"
    for doc_kind in ("bug_rootcase", "rework_lesson", "pitfall", "risk_scenario"):
        assert df.canonicalize_sink_kind(doc_kind) == doc_kind
    # 归一结果必然能直接喂给单条造形器（不会归一出一个 make_sink_entry 不认的值）
    for alias in df.SINK_DOC_KIND_ALIASES:
        assert df.canonicalize_sink_kind(alias) in df.SINK_KINDS


def test_canonicalize_sink_kind_rejects_unknown_and_empty():
    for bad in ("", None, "   ", "reason_reject", "rootcase", "reject reason"):
        with pytest.raises(ValueError):
            df.canonicalize_sink_kind(bad)


# ════════════════════════════════════════════════════════════════
# (b) 门② / 门① REJECT 原因 → kind=reject_reason（文档的 reason/reject）
# ════════════════════════════════════════════════════════════════

def test_gate2_reject_maps_to_reject_reason_kind():
    entries = df.make_sink_entries(
        "gate2_reject",
        [{"reason": "断言太弱，只有 status=200", "proposal_id": "P-1"}],
    )
    assert len(entries) == 1
    entry = entries[0]
    assert entry["origin"] == "reject_reason"
    assert entry["context"]["kind"] == "reject_reason"
    assert entry["content"] == "断言太弱，只有 status=200"
    assert entry["context"]["source_ref"]["source"] == "gate2_reject"


def test_gate1_sendback_shares_reject_reason_kind_but_keeps_source():
    """门① 打回与门② REJECT **两个写入者共用一个 kind**（文档只给 reason/reject），
    落在哪道门由 trace 里的来源区分。"""
    entries = df.make_sink_entries(
        "gate1_reject", [{"sendback_reason": "需求边界没定义清楚"}]
    )
    entry = entries[0]
    assert entry["origin"] == "reject_reason"
    ref = entry["context"]["source_ref"]
    assert ref["source"] == "gate1_reject"
    assert ref["written_by"] == "gate1_reject"


def test_reject_reason_falls_back_to_provenance_bag():
    """真实提案行把理由藏在 ``provenance.reject_reason``（``case.reject_sink``
    的取数口径）——顶层没有也能取到，顶层有则顶层优先。"""
    entries = df.make_sink_entries("gate2_reject", [
        {"provenance": {"reject_reason": "步骤没写前置数据"}},
        {"reason": "顶层理由", "provenance": {"reject_reason": "袋子里的"}},
    ])
    assert [e["content"] for e in entries] == ["步骤没写前置数据", "顶层理由"]


def test_reject_reason_alias_order_prefers_reason_over_comment():
    entries = df.make_sink_entries(
        "gate2_reject", [{"comment": "顺手备注", "reason": "正式拒绝理由"}]
    )
    assert entries[0]["content"] == "正式拒绝理由"


# ════════════════════════════════════════════════════════════════
# (c) bug 结案根因 → kind=bug_rootcase
# ════════════════════════════════════════════════════════════════

def test_bug_close_maps_to_bug_rootcase_kind():
    entries = df.make_sink_entries("bug_close", [
        {"bug_id": "BUG-01", "root_cause": "库存缓存未失效，结案时确认"},
    ])
    entry = entries[0]
    assert entry["origin"] == "bug_rootcase"
    assert entry["content"] == "库存缓存未失效，结案时确认"
    assert entry["context"]["source_ref"]["bug_id"] == "BUG-01"
    assert entry["context"]["source_ref"]["source"] == "bug_close"


def test_bug_close_root_cause_aliases():
    entries = df.make_sink_entries("bug_close", [
        {"rootcause": "别名 2"}, {"root_case": "别名 3"}, {"conclusion": "别名 4"},
    ])
    assert [e["content"] for e in entries] == ["别名 2", "别名 3", "别名 4"]
    assert {e["origin"] for e in entries} == {"bug_rootcase"}


def test_rework_lesson_and_direct_write_sources():
    """返工教训（文档第四写入者）+ pitfall / risk_scenario 两个直写入口。"""
    entries = df.make_sink_entries("rework_lesson", [
        {"lesson": "并发场景整批漏测", "case_id": "TC-9"},
    ])
    assert entries[0]["origin"] == "rework_lesson"
    assert entries[0]["context"]["source_ref"]["case_id"] == "TC-9"

    pitfall = df.make_sink_entries("pitfall", [{"pitfall": "测试环境时钟是 UTC"}])
    assert pitfall[0]["origin"] == "pitfall"

    risk = df.make_sink_entries("risk_scenario", [{"scenario": "大促并发下单"}])
    assert risk[0]["origin"] == "risk_scenario"


# ════════════════════════════════════════════════════════════════
# (d) module 字段正确带出（文档「按 module 列索引」→ 进 context.module）
# ════════════════════════════════════════════════════════════════

def test_module_from_item_wins_over_batch_default():
    entries = df.make_sink_entries(
        "gate2_reject",
        [{"reason": "A", "module": "order"}, {"reason": "B"}],
        module="payment",
    )
    assert entries[0]["context"]["module"] == "order"
    assert entries[1]["context"]["module"] == "payment"   # 批量兜底
    # 顶层 module 扩展键同步（raw 写路径用；ORM 会丢弃，见歧义记录 9）
    assert entries[0]["module"] == "order"


def test_module_from_payload_bag_and_surface_alias():
    entries = df.make_sink_entries("gate2_reject", [
        {"reason": "A", "payload": {"module": "cart"}},
        {"reason": "B", "surface": "checkout"},
    ])
    assert entries[0]["context"]["module"] == "cart"
    assert entries[1]["context"]["module"] == "checkout"


def test_module_absent_is_none_not_empty_string():
    entries = df.make_sink_entries("gate2_reject", [{"reason": "只有理由"}])
    assert entries[0]["context"]["module"] is None
    assert entries[0]["module"] is None


# ════════════════════════════════════════════════════════════════
# (e) 非法 source / 非法条目 —— 明确拒绝
# ════════════════════════════════════════════════════════════════

def test_unknown_source_raises():
    for bad in ("", None, "gate3_reject", "bug", "reject_reason", "sink"):
        with pytest.raises(ValueError):
            df.make_sink_entries(bad, [{"reason": "x"}])


def test_source_is_the_documented_writer_set():
    """合法 source = 文档四写入者 + 两 kind 的直写入口。"""
    assert set(df.SINK_SOURCES) == set(df.SINK_SOURCE_KINDS)
    assert set(df.SINK_SOURCES) == {
        "gate2_reject", "gate1_reject", "bug_close",
        "rework_lesson", "pitfall", "risk_scenario",
    }
    # 每个 source 的默认 kind 必是五类之一
    for kind in df.SINK_SOURCE_KINDS.values():
        assert kind in df.SINK_KINDS


def test_source_is_case_insensitive_and_trimmed():
    entries = df.make_sink_entries("  Gate2_Reject ", [{"reason": "x"}])
    assert entries[0]["context"]["source_ref"]["source"] == "gate2_reject"


def test_item_must_be_mapping():
    with pytest.raises(ValueError):
        df.make_sink_entries("gate2_reject", ["not-a-mapping"])
    with pytest.raises(ValueError):
        df.make_sink_entries("gate2_reject", [None])


def test_missing_text_raises_in_strict_and_skips_in_loose():
    with pytest.raises(ValueError, match="items\\[0\\]"):
        df.make_sink_entries("gate2_reject", [{"proposal_id": "P-1"}])
    # 报错要指出是第几条（批量回流不能静默吞一条）
    with pytest.raises(ValueError, match="items\\[1\\]"):
        df.make_sink_entries(
            "bug_close", [{"root_cause": "有"}, {"note": "   "}]
        )
    loose = df.make_sink_entries(
        "gate2_reject", [{"reason": "有"}, {"reason": "  "}], strict=False
    )
    assert len(loose) == 1


def test_empty_items_returns_empty_list():
    assert df.make_sink_entries("gate2_reject", []) == []
    assert df.make_sink_entries("bug_close", ()) == []


def test_item_kind_override_canonicalizes_but_top_level_kind_is_ignored():
    """``sink_kind`` 可覆盖来源默认值；提案行自带的顶层 ``kind`` 列**不算**覆盖
    （那是 case/proposal 分类，不是回流种类）。覆盖 kind 时正文允许来自原 kind
    的列（一条 bug 根因记录里顺手标成 pitfall，正文仍在 ``root_cause``）。"""
    entries = df.make_sink_entries("bug_close", [
        {"root_cause": "顺手记个坑", "sink_kind": "pitfall"},
        {"root_cause": "正常根因", "kind": "bug"},
    ])
    assert entries[0]["origin"] == "pitfall"
    assert entries[0]["content"] == "顺手记个坑"
    assert entries[1]["origin"] == "bug_rootcase"
    assert entries[1]["content"] == "正常根因"
    with pytest.raises(ValueError):
        df.make_sink_entries("bug_close", [{"root_cause": "x", "sink_kind": "nonsense"}])


# ════════════════════════════════════════════════════════════════
# 行形状与单条造形器 / 入库纪律一致
# ════════════════════════════════════════════════════════════════

def test_entries_have_same_shape_as_make_sink_entry():
    """批量结果必须和 :func:`make_sink_entry` 逐键同构——调用方一套入库代码通吃。"""
    ref = uuid.UUID(int=42)
    entries = df.make_sink_entries("gate2_reject", [
        {"reason": "理由", "module": "order", "proposal_id": ref, "run_id": ref},
    ])
    expected = df.make_sink_entry(
        "reject_reason", "理由", module="order",
        source_ref=entries[0]["context"]["source_ref"],
    )
    assert entries[0] == expected
    assert set(entries[0]) == set(df.make_sink_entry("pitfall", "x"))


def test_entries_are_resolved_and_never_blocking():
    """沉淀是已定案记录：``status=resolved`` + 空 ``blocking_refs``，
    不进 ``pending.unanswered``、不阻塞下游（歧义记录 8）。"""
    entries = df.make_sink_entries("gate1_reject", [
        {"reason": "A"}, {"reason": "B"}, {"reason": "C"},
    ])
    assert len(entries) == 3
    for entry in entries:
        assert entry["status"] == "resolved"
        assert entry["blocking_refs"] == []
        assert entry["resolution"] is None
        assert entry["raised_by"] is None
        assert entry["context"]["gate"] == df.SINK_GATE == "sink"
        assert entry["context"]["sink"] is True


def test_trace_carries_source_refs_and_gate_goes_to_from_gate():
    """来源 trace：哪条 case / 哪次 reject / 哪个 bug + 判决人；
    条目自带的 ``gate`` 归一到 ``from_gate``（不顶掉 ``context.gate="sink"``）。"""
    pid = uuid.UUID(int=1)
    entries = df.make_sink_entries("gate2_reject", [{
        "reason": "断言不足",
        "id": pid,                       # 行主键 → proposal_id（门来源）
        "run_id": uuid.UUID(int=2),
        "gate": "gate2",
        "decided_by": uuid.UUID(int=3),
        "payload": {"title": "TC-0001"},
    }])
    ref = entries[0]["context"]["source_ref"]
    assert ref["proposal_id"] == str(pid)          # JSONB 可写形态：UUID → str
    assert ref["run_id"] == str(uuid.UUID(int=2))
    assert ref["decided_by"] == str(uuid.UUID(int=3))
    assert ref["from_gate"] == "gate2"
    assert "gate" not in ref
    assert ref["title"] == "TC-0001"
    assert entries[0]["context"]["gate"] == "sink"  # 两个口径互不覆盖


def test_caller_source_ref_bag_merges_but_cannot_forge_source():
    entries = df.make_sink_entries("bug_close", [{
        "root_cause": "根因",
        "source_ref": {"source": "hacked", "written_by": "hacked", "ticket": "ZT-9"},
    }])
    ref = entries[0]["context"]["source_ref"]
    assert ref["source"] == "bug_close"
    assert ref["written_by"] == "bug_close"
    assert ref["ticket"] == "ZT-9"      # 自定义引用键照常保留


def test_bug_close_row_id_falls_back_to_bug_id():
    entries = df.make_sink_entries("bug_close", [{"id": "BUG-7", "root_cause": "x"}])
    assert entries[0]["context"]["source_ref"]["bug_id"] == "BUG-7"


def test_rework_source_row_id_is_not_guessed_as_case_id():
    """返工行主键不等于用例 id——不猜，要 trace 就显式给 ``case_id``。"""
    entries = df.make_sink_entries("rework_lesson", [{"id": "RR-1", "lesson": "x"}])
    ref = entries[0]["context"]["source_ref"]
    assert "case_id" not in ref
    assert ref["written_by"] == "rework_lesson"


def test_bulk_conversion_of_the_four_documented_writers():
    """四写入者一批转完，kind 分布 = 文档要求的四类。"""
    entries = []
    entries += df.make_sink_entries("gate2_reject", [{"reason": "门②理由"}])
    entries += df.make_sink_entries("gate1_reject", [{"reason": "门①理由"}])
    entries += df.make_sink_entries("bug_close", [{"root_cause": "根因"}])
    entries += df.make_sink_entries("rework_lesson", [{"lesson": "教训"}])
    assert [e["origin"] for e in entries] == [
        "reject_reason", "reject_reason", "bug_rootcase", "rework_lesson",
    ]
    assert [e["context"]["source_ref"]["source"] for e in entries] == [
        "gate2_reject", "gate1_reject", "bug_close", "rework_lesson",
    ]
    # 入库方只需一套剥顶层扩展键 + create（顶层 gate/module/kind 无真列）
    for entry in entries:
        assert {"gate", "module", "kind"} <= set(entry)


def test_module_only_from_batch_default_when_items_are_bare():
    entries = df.make_sink_entries(
        "rework_lesson", [{"lesson": "A"}, {"lesson": "B"}], module="  auth  "
    )
    assert [e["context"]["module"] for e in entries] == ["auth", "auth"]
