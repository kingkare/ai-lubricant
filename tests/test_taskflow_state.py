"""任务层状态机 + 暂存护栏的纯函数单测（26 号文 §1.4/§1.8）。

**不需要 PG 基线**——全部走纯函数（照 ``generation/state.py`` 的测试风格），
不碰 ORM、不连库。对应记忆「tests-need-postgres-baseline」里被豁免的那类纯逻辑测试。
"""
from __future__ import annotations

import pytest

from testing_platform.taskflow.models import (
    CANCEL_EDGES,
    PHASE_OF_STATUS,
    TASK_PHASES,
    TASK_RUN_STATUSES,
    TASK_TRANSITIONS,
    TASK_TYPES,
    TERMINAL_TASK_STATUSES,
    WAITING_TASK_STATUSES,
    can_transition,
    explore_scope_required,
    is_terminal,
    is_waiting_on_human,
    phase_of,
)


# ── 状态机基本不变式 ─────────────────────────────────────────────


def test_每个状态都有阶段映射():
    assert set(PHASE_OF_STATUS) == set(TASK_RUN_STATUSES)
    assert all(p in TASK_PHASES for p in PHASE_OF_STATUS.values())


def test_未知状态回退到创建段():
    assert phase_of("nonsense") == "create"


def test_blocked_是等人态且属执行段():
    assert "blocked" in WAITING_TASK_STATUSES
    assert phase_of("blocked") == "execute"


def test_planned_是等人态():
    assert "planned" in WAITING_TASK_STATUSES
    assert phase_of("planned") == "confirm"


@pytest.mark.parametrize("status", sorted(TERMINAL_TASK_STATUSES))
def test_终态无出边(status):
    assert is_terminal(status)
    for target in TASK_RUN_STATUSES:
        assert not can_transition(status, target), f"{status} → {target} 不该合法"


def test_未知状态不可流转():
    assert not can_transition("bogus", "planning")
    assert not can_transition("draft", "bogus")


# ── 合法主链 ─────────────────────────────────────────────────────


def test_主链逐段合法():
    chain = [
        ("draft", "planning"),
        ("planning", "planned"),
        ("planned", "executing"),
        ("executing", "applying"),
        ("applying", "completed"),
    ]
    for a, b in chain:
        assert can_transition(a, b), f"{a} → {b} 应合法"


def test_方案可迭代():
    assert can_transition("planning", "planning")


def test_可打回重规划():
    assert can_transition("planned", "planning")


def test_撞缺依赖进_blocked_并可恢复():
    assert can_transition("executing", "blocked")
    assert can_transition("blocked", "executing")
    assert can_transition("blocked", "planning")  # 依赖影响方案 → 重规划


def test_blocked_不能跳去_applying():
    assert not can_transition("blocked", "applying")


def test_draft_不能直接执行():
    assert not can_transition("draft", "executing")


def test_任意非终态可取消():
    for status in TASK_RUN_STATUSES:
        if status in TERMINAL_TASK_STATUSES:
            continue
        assert ("status", "cancelled") or can_transition(status, "cancelled"), status
        assert can_transition(status, "cancelled"), f"{status} 应可取消"


def test_任意非终态可失败():
    for status in TASK_RUN_STATUSES:
        if status in TERMINAL_TASK_STATUSES:
            continue
        assert can_transition(status, "failed"), f"{status} 应可失败"


def test_cancel_edges_与终态互斥():
    for src, dst in CANCEL_EDGES:
        assert dst == "cancelled"
        assert src not in TERMINAL_TASK_STATUSES


# ── 类型维度 ─────────────────────────────────────────────────────


def test_五类任务齐():
    assert TASK_TYPES == (
        "explore", "gen_case", "gen_scenario", "req_analysis", "execution",
    )


def test_探索必填维度():
    req = explore_scope_required()
    assert set(req) == {"purpose", "direction", "client", "target"}


def test_每个状态恰好映射到一个阶段():
    for status, phase in PHASE_OF_STATUS.items():
        assert phase in TASK_PHASES
    # executing 与 blocked 同属 execute 段（blocked 是执行段内的一次停顿）
    assert PHASE_OF_STATUS["executing"] == PHASE_OF_STATUS["blocked"] == "execute"


def test_transition_表里没有指向终态之外的非法项():
    # 一致性：TASK_TRANSITIONS 每条边的目标都必须是合法状态名
    for src, dst in TASK_TRANSITIONS:
        assert src in TASK_RUN_STATUSES and dst in TASK_RUN_STATUSES


def test_is_waiting_仅planned和blocked():
    for status in TASK_RUN_STATUSES:
        assert is_waiting_on_human(status) == (status in ("planned", "blocked"))
