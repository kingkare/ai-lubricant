"""覆盖矩阵 + token 预算（design/coverage.py、design/token_budget.py）——纯函数，不联网不查库。

覆盖：
1. ``build_matrix``：axis 记录 → module×C01-C14 矩阵 + 缺口（接口适用机械轴才判缺）。
2. ``check_budget`` 三态：OK / RECOMMENDED / SPLIT_REQUIRED。
3. ``split_by_heading``：markdown 标题切章 + 超长兜底切。
4. ``estimate_tokens`` 启发式：CJK 感知、宁高估不低估。
"""
from __future__ import annotations

import os
import sys

import pytest

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.design.coverage import AXIS_CODES, build_matrix
from testing_platform.design.token_budget import (
    STATUS_OK,
    STATUS_RECOMMENDED,
    STATUS_SPLIT_REQUIRED,
    available_budget,
    check_budget,
    context_window,
    estimate_tokens,
    split_by_heading,
)


# ── build_matrix ─────────────────────────────────────────────
def test_matrix_counts_and_gaps():
    records = [
        {"module": "支付", "axis": "C12"}, {"module": "支付", "axis": "C12"},
        {"module": "支付", "axis": "C03"}, {"module": "支付", "axis": "C01"},
        {"module": "物流", "axis": "C01"},
    ]
    r = build_matrix(records)
    assert r["matrix"]["支付"]["C12"] == 2
    assert r["matrix"]["支付"]["C03"] == 1
    # 物流缺 C12（接口适用机械轴）→ 缺口
    gaps = {(g["module"], g["axis"]) for g in r["gaps"]}
    assert ("物流", "C12") in gaps
    assert ("支付", "C12") not in gaps
    # C05 并发不是机械轴 → 不报缺
    assert ("支付", "C05") not in gaps
    assert 0 < r["covered_rate"] < 1


def test_matrix_unknown_axis_ignored():
    r = build_matrix([{"module": "x", "axis": "C99"}])
    assert r["matrix"] == {} or r["matrix"]["x"] == dict.fromkeys(AXIS_CODES, 0)


def test_matrix_explicit_modules_show_empty():
    """传入模块全集：一条用例都没有的模块也显式出现（全 0），不隐身。"""
    r = build_matrix([{"module": "有", "axis": "C01"}], modules=["有", "空"])
    assert "空" in r["matrix"]
    assert all(v == 0 for v in r["matrix"]["空"].values())
    gaps = {(g["module"], g["axis"]) for g in r["gaps"]}
    assert ("空", "C12") in gaps  # 空模块全缺


def test_matrix_empty_records_covered_rate_one():
    assert build_matrix([])["covered_rate"] == 1.0
    assert build_matrix([])["gaps"] == []


def test_matrix_ungrouped_bucket():
    r = build_matrix([{"module": "", "axis": "C01"}])
    assert "（未分组）" in r["matrix"]


# ── token 预算三态 ───────────────────────────────────────────
def test_context_window_env_override(monkeypatch):
    monkeypatch.setenv("TESTING_GEN_CONTEXT_WINDOW", "12345")
    assert context_window(None) == 12345
    monkeypatch.delenv("TESTING_GEN_CONTEXT_WINDOW")
    assert context_window("claude-opus-4-8") == 1_000_000
    assert context_window(None) > 0  # 缺省


def test_estimate_tokens_cjk_heuristic():
    # CJK 密集文本 token 数应显著大于同长度 ASCII 的一半
    cjk = estimate_tokens("测试" * 100)     # 200 CJK chars
    ascii_text = estimate_tokens("a" * 400)  # 400 ascii chars
    assert cjk > 50
    assert ascii_text > 50
    assert estimate_tokens("") == 0


def test_check_budget_three_states(monkeypatch):
    """窗口必须大于预留下限(4000)+输出预留(4000)，否则 budget=0 恒 SPLIT。"""
    monkeypatch.setenv("TESTING_GEN_CONTEXT_WINDOW", "100000")
    budget = available_budget(None)  # 100000-4000-output4000 → 91000+
    assert budget > 50_000

    # ASCII 启发式 ≈ 4 字符/token
    ok = check_budget(["x" * (int(budget * 0.5) * 4)], model=None)
    assert ok["status"] == STATUS_OK

    mid = check_budget(["x" * (int(budget * 0.8) * 4)], model=None)
    assert mid["status"] == STATUS_RECOMMENDED

    big = check_budget(["x" * (int(budget * 1.5) * 4)], model=None)
    assert big["status"] == STATUS_SPLIT_REQUIRED


def test_tiny_window_budget_zero_is_split():
    """窗口小于预留 → budget=0，任何输入都 SPLIT（保守拒绝，不 OOM）。"""
    import os
    os.environ["TESTING_GEN_CONTEXT_WINDOW"] = "1000"
    try:
        assert available_budget(None) == 0
        assert check_budget(["a"], model=None)["status"] == STATUS_SPLIT_REQUIRED
    finally:
        os.environ.pop("TESTING_GEN_CONTEXT_WINDOW")


# ── 标题感知切分 ─────────────────────────────────────────────
def test_split_by_heading_basic():
    doc = "# 引言\n前言内容\n## 需求一\n内容A\n## 需求二\n内容B"
    chunks = split_by_heading(doc)
    headings = [c["heading"] for c in chunks]
    assert "引言" in headings and "需求一" in headings and "需求二" in headings
    assert all(c["text"] for c in chunks)


def test_split_by_heading_long_chunk_fallback():
    body = "x" * 9000
    doc = f"## 大章\n{body}"
    chunks = split_by_heading(doc, max_chars=4000)
    assert len(chunks) >= 3  # 9000/4000 → 3 段
    assert all(len(c["text"]) <= 4000 for c in chunks)
    assert all(c["heading"] == "大章" for c in chunks)


def test_split_by_heading_no_heading():
    chunks = split_by_heading("没有标题的纯文本")
    assert len(chunks) == 1 and chunks[0]["heading"] == ""


def test_split_by_heading_empty():
    assert split_by_heading("   ") == []
