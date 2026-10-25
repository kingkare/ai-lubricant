"""生成环节状态机（25 号文 §3.1/§3.2/§3.3）测试。

纯函数测试——不建库、不联网、不依赖 PG，全部用构造 dict。

覆盖：
1. 转换边全枚举（合法边过、非法边拒、终态无出边、自环拒绝、未知态拒绝）
2. 回边（evaluating→generating）/ 旁路（paused/cancelled/failed 可达 + paused 恢复）
3. run_type × 起止表
4. gen_status → run.status 映射（12 态逐个 + 混合需求取最慢 + 脏值/别名/空集）
5. compute_progress 每个计数口径（含 testpoint rejected 聚合、split/inconclusive 区分、
   coverage 三态、unanswered、budget 透传、datasets 按 run_type 计数）
6. notify_gate_open 三门（gate0/gate1/gate2，非门态 None）

运行：``python -m pytest tests/test_generation_state.py -q``
"""

from __future__ import annotations

import itertools
import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from testing_platform.generation.state import (
    ACTIVE_RUN_STATUSES,
    COVERAGE_STATES,
    GEN_RUN_STATUSES,
    GEN_STATUSES,
    GEN_STATUS_ALIASES,
    GEN_STATUS_STAGE_INDEX,
    GEN_STATUS_TO_RUN_STATUS,
    GEN_STATUS_LABELS,
    GATE_IDS,
    GATE_STATUSES,
    MAIN_EDGES,
    PROPOSAL_REVIEW_STATES,
    PROPOSAL_STATUSES,
    REWORK_BACK_EDGES,
    RUN_BYPASS_STATUSES,
    RUN_MAIN_CHAIN,
    RUN_STATUS_LABELS,
    RUN_STATUS_ORDER,
    RUN_TYPES,
    TERMINAL_RUN_STATUSES,
    TRANSITIONS,
    assert_transition,
    can_transition,
    compute_progress,
    gen_status_stage,
    gen_status_to_run_status,
    is_terminal_run_status,
    normalize_gen_status,
    notify_gate_open,
    plan_run_close_status,
    run_type_span,
)


# ═══════════════════════════════════════════════════════════════════
# 0 · 常量自洽（枚举内聚性：标签/序号/取值域不漏不重）
# ═══════════════════════════════════════════════════════════════════

def test_run_statuses_shape():
    # 10 主链 + 3 旁路
    assert len(RUN_MAIN_CHAIN) == 10
    assert len(RUN_BYPASS_STATUSES) == 3
    assert set(RUN_BYPASS_STATUSES) == {"paused", "failed", "cancelled"}
    assert len(GEN_RUN_STATUSES) == 13
    assert len(set(GEN_RUN_STATUSES)) == 13  # 无重复
    # 序号覆盖主链且单调
    assert sorted(RUN_STATUS_ORDER.values()) == list(range(10))
    assert set(RUN_STATUS_ORDER) == set(RUN_MAIN_CHAIN)
    # 标签全覆盖
    assert set(RUN_STATUS_LABELS) == set(GEN_RUN_STATUSES)


def test_run_status_labels_are_chinese():
    # 中文口径逐字对齐 §3.1（门态带门号，供 UI/日志）
    assert RUN_STATUS_LABELS["req_reviewing"].startswith("需求评审")
    assert RUN_STATUS_LABELS["points_proposed"].startswith("需求确认")
    assert RUN_STATUS_LABELS["cases_proposed"].startswith("用例确认")
    assert RUN_STATUS_LABELS["done"] == "已入库"
    assert RUN_STATUS_LABELS["paused"] == "已暂停"


def test_terminal_and_active_partition():
    # 终态三兄弟 + 非终态集合恰好划分全集
    assert TERMINAL_RUN_STATUSES == {"done", "failed", "cancelled"}
    assert ACTIVE_RUN_STATUSES | TERMINAL_RUN_STATUSES == set(GEN_RUN_STATUSES)
    assert not (ACTIVE_RUN_STATUSES & TERMINAL_RUN_STATUSES)
    # paused 可恢复，不是终态
    assert "paused" in ACTIVE_RUN_STATUSES
    for s in TERMINAL_RUN_STATUSES:
        assert is_terminal_run_status(s)
    assert not is_terminal_run_status("paused")
    assert not is_terminal_run_status("importing")


def test_gen_statuses_shape():
    assert len(GEN_STATUSES) == 12
    assert len(set(GEN_STATUSES)) == 12
    assert set(GEN_STATUS_LABELS) == set(GEN_STATUSES)
    # 中文标签逐字对齐 §3.2
    assert GEN_STATUS_LABELS["not_started"] == "未开始"
    assert GEN_STATUS_LABELS["req_reviewing"] == "需求评审"
    assert GEN_STATUS_LABELS["req_reviewed"] == "已过评审"
    assert GEN_STATUS_LABELS["point_proposing"] == "提点中"
    assert GEN_STATUS_LABELS["gate1_pending"] == "门①待确认"
    assert GEN_STATUS_LABELS["gate1_passed"] == "已过门①"
    assert GEN_STATUS_LABELS["case_reviewing"] == "用例评审"
    assert GEN_STATUS_LABELS["gate2_pending"] == "门②待确认"
    assert GEN_STATUS_LABELS["gate2_passed"] == "已过门②"
    assert GEN_STATUS_LABELS["imported"] == "已入库"
    # 阶段序号单调
    assert sorted(GEN_STATUS_STAGE_INDEX.values()) == list(range(12))


# ═══════════════════════════════════════════════════════════════════
# 1 · 转换边全枚举
# ═══════════════════════════════════════════════════════════════════

def test_transitions_is_exactly_the_union():
    # TRANSITIONS = 主链 + 回边 + 旁路四族；不允许旁路成员之外的黑边
    from testing_platform.generation import state as st

    expected = (
        st.MAIN_EDGES
        | st.REWORK_BACK_EDGES
        | st.PAUSE_EDGES
        | st.RESUME_EDGES
        | st.CANCEL_EDGES
        | st.FAIL_EDGES
        | st.PAUSED_EXIT_EDGES
    )
    assert TRANSITIONS == expected


def test_all_main_chain_edges_legal():
    for frm, to in zip(RUN_MAIN_CHAIN, RUN_MAIN_CHAIN[1:]):
        assert can_transition(frm, to), f"主链边 {frm}->{to} 应合法"
        assert_transition(frm, to)  # 不抛


def test_main_edges_cover_full_chain():
    assert len(MAIN_EDGES) == 9
    assert ("pending", "analyzing") in MAIN_EDGES
    assert ("req_reviewing", "points_proposed") in MAIN_EDGES
    assert ("cases_confirmed", "importing") in MAIN_EDGES
    assert ("importing", "done") in MAIN_EDGES


def test_forward_skips_rejected():
    # 不能跨阶段前进
    for frm, to in [
        ("pending", "generating"),
        ("pending", "done"),
        ("analyzing", "points_proposed"),
        ("analyzing", "cases_proposed"),
        ("req_reviewing", "generating"),
        ("points_proposed", "evaluating"),
        ("generating", "cases_proposed"),
        ("generating", "done"),
        ("evaluating", "cases_confirmed"),
        ("cases_proposed", "importing"),
        ("cases_confirmed", "done"),
    ]:
        assert not can_transition(frm, to), f"跨阶段 {frm}->{to} 应拒绝"


def test_backward_moves_rejected_except_rework():
    # 除明写回边外，一切倒退拒绝
    for frm, to in [
        ("generating", "analyzing"),
        ("generating", "req_reviewing"),
        ("generating", "points_proposed"),
        ("evaluating", "points_proposed"),
        ("cases_proposed", "generating"),  # 门② 不直接打回生成（见歧义注释）
        ("cases_proposed", "evaluating"),
        ("importing", "generating"),
        ("done", "cases_proposed"),
        ("cases_confirmed", "cases_proposed"),
    ]:
        assert not can_transition(frm, to), f"倒退 {frm}->{to} 应拒绝"
    # 明写回边：evaluating → generating（返工：分数<70 或双评分歧回派）
    assert REWORK_BACK_EDGES == {("evaluating", "generating")}
    assert can_transition("evaluating", "generating")
    assert_transition("evaluating", "generating")


def test_full_enumeration_matches_edge_set():
    # 全枚举：can_transition(frm, to) ⟺ (frm, to) ∈ TRANSITIONS（自环恒 False）
    for frm, to in itertools.product(GEN_RUN_STATUSES, repeat=2):
        expected = frm != to and (frm, to) in TRANSITIONS
        assert can_transition(frm, to) == expected, f"{frm}->{to}"


def test_self_loops_rejected():
    for s in GEN_RUN_STATUSES:
        assert not can_transition(s, s)
        with pytest.raises(ValueError):
            assert_transition(s, s)


def test_terminal_states_have_no_outgoing_edges():
    for term in TERMINAL_RUN_STATUSES:
        for to in GEN_RUN_STATUSES:
            assert not can_transition(term, to), f"终态 {term} 不应有出边到 {to}"


def test_unknown_status_rejected():
    assert not can_transition("whatever", "done")
    assert not can_transition("pending", "whatever")
    assert not can_transition("", "done")
    assert not can_transition("running", "done")  # mc_test_run 的态不是本状态机的态


def test_assert_transition_error_messages_are_readable():
    with pytest.raises(ValueError, match="未知 run.status.*whatever"):
        assert_transition("whatever", "done")
    with pytest.raises(ValueError, match="终态"):
        assert_transition("done", "pending")
    with pytest.raises(ValueError, match="pending.*generating|合法目标"):
        assert_transition("pending", "generating")


# ═══════════════════════════════════════════════════════════════════
# 2 · 旁路：paused / failed / cancelled
# ═══════════════════════════════════════════════════════════════════

def test_pause_reachable_from_every_non_terminal():
    for s in sorted(ACTIVE_RUN_STATUSES):
        if s == "paused":
            continue
        assert can_transition(s, "paused"), f"{s} 应可暂停"
        assert_transition(s, "paused")


def test_cancel_and_fail_reachable_from_every_non_terminal():
    for s in sorted(ACTIVE_RUN_STATUSES):
        if s == "paused":
            continue  # paused 的取消/失败由 paused 专边覆盖
        assert can_transition(s, "cancelled"), f"{s} 应可取消"
        assert can_transition(s, "failed"), f"{s} 应可判失败"
    # paused 停久了也可收口
    assert can_transition("paused", "cancelled")
    assert can_transition("paused", "failed")


def test_paused_resumes_to_any_non_terminal():
    # 恢复回原态：原态可能是任意非终态（本模块不持久化 resume_from，
    # 边集声明"回哪都合法"，具体回哪个由调用方记的原态决定）
    for s in sorted(ACTIVE_RUN_STATUSES):
        if s == "paused":
            continue
        assert can_transition("paused", s), f"paused 应可恢复到 {s}"
    # 恢复回不到 done（暂停前不可能是终态，done=真正跑完才能到）
    assert not can_transition("paused", "done")
    # paused 停久了可收口（fail/cancelled 是人为收尾，不是"恢复"）
    assert can_transition("paused", "failed")
    assert can_transition("paused", "cancelled")


def test_bypass_targets_from_main_chain_are_exactly_the_three():
    # 主链态（除终态 done）能进旁路的出口恰好是 paused/failed/cancelled 三个控制态——
    # 不存在其它旁路成员、也没有主链态借道旁路的黑边
    for s in RUN_MAIN_CHAIN:
        if s in TERMINAL_RUN_STATUSES:
            assert not any(can_transition(s, to) for to in GEN_RUN_STATUSES)
            continue
        targets = {to for to in GEN_RUN_STATUSES if can_transition(s, to)}
        bypass_targets = targets & set(RUN_BYPASS_STATUSES)
        assert bypass_targets == {"paused", "failed", "cancelled"}, f"{s} 的旁路出口: {bypass_targets}"
        # 主链内目标恰好是：下一态 +（evaluating 的回边）
        main_targets = targets & set(RUN_MAIN_CHAIN)
        expected_main = {RUN_MAIN_CHAIN[RUN_MAIN_CHAIN.index(s) + 1]}
        if s == "evaluating":
            expected_main = {"generating", "cases_proposed"}  # 回边 + 前进
        assert main_targets == expected_main, f"{s} 的主链出口: {main_targets}"


def test_no_edge_back_from_cancelled_failed():
    # cancelled/failed 是死态；paused 只能回主链或收口，不能变成另一个旁路
    assert not can_transition("cancelled", "paused")
    assert not can_transition("failed", "paused")
    assert not can_transition("cancelled", "failed")
    assert not can_transition("failed", "cancelled")


# ═══════════════════════════════════════════════════════════════════
# 3 · run_type × 起止表（§3.1 v4.1 补）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "run_type,start,end",
    [
        ("full", "pending", "done"),
        ("plan", "pending", "points_proposed"),
        ("exec", "generating", "done"),
        ("pre_submit_edit", "generating", "cases_proposed"),
        ("incremental", "analyzing", "done"),
        ("reopen", "generating", "done"),
    ],
)
def test_run_type_span_table(run_type, start, end):
    assert run_type_span(run_type) == (start, end)


def test_run_type_span_unknown_raises():
    with pytest.raises(ValueError, match="未知 run_type"):
        run_type_span("nope")


def test_run_types_all_have_spans_and_labels():
    from testing_platform.generation import state as st

    assert set(RUN_TYPES) == set(st.RUN_TYPE_SPANS)
    assert set(RUN_TYPES) == set(st.RUN_TYPE_LABELS)
    assert set(RUN_TYPES) == {"full", "plan", "exec", "pre_submit_edit", "incremental", "reopen"}


def test_plan_run_close_status():
    # plan 的起止表终点是门①（"关闭"），收尾写 done（终态过滤口径）
    assert plan_run_close_status("plan") == "done"
    # 其余类型按起止表终点
    assert plan_run_close_status("full") == "done"
    assert plan_run_close_status("pre_submit_edit") == "cases_proposed"
    with pytest.raises(ValueError):
        plan_run_close_status("nope")


def test_span_starts_are_legal_entry_points():
    # 每种 run_type 的起点应能走到它的终点（沿合法边）
    chain_ok = {
        "full": ("pending", "done"),
        "plan": ("pending", "points_proposed"),
        "exec": ("generating", "done"),
        "pre_submit_edit": ("generating", "cases_proposed"),
        "incremental": ("analyzing", "done"),
        "reopen": ("generating", "done"),
    }
    for rt, (start, end) in chain_ok.items():
        assert run_type_span(rt) == (start, end)
        # 起点必须非终态（run 建出来就得能动）
        assert start in ACTIVE_RUN_STATUSES


# ═══════════════════════════════════════════════════════════════════
# 4 · gen_status → run.status 映射（§3.1 表 + §3.2 取最慢）
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "gen_status,run_status",
    [
        ("not_started", "analyzing"),
        ("analyzing", "analyzing"),
        ("req_reviewing", "req_reviewing"),
        ("req_reviewed", "req_reviewing"),
        ("point_proposing", "points_proposed"),
        ("gate1_pending", "points_proposed"),
        ("gate1_passed", "generating"),
        ("case_generating", "generating"),
        ("case_reviewing", "evaluating"),  # §3.1 括号「评审中=evaluating」
        ("gate2_pending", "cases_proposed"),
        ("gate2_passed", "cases_confirmed"),
        ("imported", "done"),
    ],
)
def test_single_gen_status_mapping(gen_status, run_status):
    assert gen_status_to_run_status([gen_status]) == run_status
    assert gen_status_to_run_status(gen_status) == run_status  # 裸字符串容忍
    # 映射表逐键一致
    assert GEN_STATUS_TO_RUN_STATUS[gen_status] == run_status


def test_mapping_only_targets_main_chain():
    # 12 对 10：目标必须是主链态；pending/importing 无需求级对应（故 12≠10）
    targets = set(GEN_STATUS_TO_RUN_STATUS.values())
    assert targets <= set(RUN_MAIN_CHAIN)
    assert targets == {
        "analyzing",
        "req_reviewing",
        "points_proposed",
        "generating",
        "evaluating",
        "cases_proposed",
        "cases_confirmed",
        "done",
    }
    assert "pending" not in targets
    assert "importing" not in targets


def test_mapping_is_monotone_with_stage_index():
    # 阶段越靠后，映射的 run 态序号不回退
    prev = -1
    for gs in GEN_STATUSES:
        rs = GEN_STATUS_TO_RUN_STATUS[gs]
        cur = RUN_STATUS_ORDER[rs]
        assert cur >= prev, f"{gs} 的映射回退了"
        prev = cur


def test_mixed_requirements_take_slowest():
    # 总闸 = 最慢需求（§3.2：run.status = 全部需求里最慢的阶段）
    assert gen_status_to_run_status(["imported", "imported"]) == "done"
    assert gen_status_to_run_status(["imported", "gate1_pending"]) == "points_proposed"
    assert gen_status_to_run_status(["gate1_passed", "case_generating"]) == "generating"
    assert gen_status_to_run_status(["gate2_pending", "case_generating"]) == "generating"
    assert gen_status_to_run_status(["not_started", "imported", "done"]) == "analyzing"
    # 一个没开始的卡住全部
    assert gen_status_to_run_status(["case_reviewing", "not_started"]) == "analyzing"


def test_mixed_requirements_with_dict_rows():
    rows = [
        {"id": "r1", "gen_status": "gate2_pending"},
        {"id": "r2", "gen_status": "case_generating"},
        {"id": "r3"},  # 缺 gen_status 列 = 未开始（老行兼容）
    ]
    assert gen_status_to_run_status(rows) == "analyzing"

    rows2 = [
        {"id": "r1", "gen_status": "gate2_pending"},
        {"id": "r2", "gen_status": "case_generating"},
        {"id": "r3", "gen_status": "case_reviewing"},
    ]
    # 最慢 = case_generating(7) → generating
    assert gen_status_to_run_status(rows2) == "generating"
    # requirements= 关键字形式
    assert gen_status_to_run_status(requirements=rows2) == "generating"


def test_dirty_and_chinese_values_normalize():
    # 中文直传（文档口径）
    assert gen_status_to_run_status(["门①待确认"]) == "points_proposed"
    assert gen_status_to_run_status(["已入库"]) == "done"
    assert normalize_gen_status("未开始") == "not_started"
    assert normalize_gen_status("用例评审") == "case_reviewing"
    # 别名
    assert normalize_gen_status("unstarted") == "not_started"
    assert normalize_gen_status("done") == "imported"
    assert normalize_gen_status("Done") == "imported"
    assert normalize_gen_status("case_proposed") == "gate2_pending"
    # 脏值 → None（按 not_started 参与比较）
    assert normalize_gen_status("active") is None
    assert normalize_gen_status("") is None
    assert normalize_gen_status(None) is None
    assert gen_status_stage("瞎写的") == 0
    # 脏值把 run 拉回最慢
    assert gen_status_to_run_status(["imported", "active"]) == "analyzing"


def test_empty_set_maps_to_pending():
    # 还没有需求可算 → run 尚未派活
    assert gen_status_to_run_status([]) == "pending"
    assert gen_status_to_run_status(None) == "pending"
    assert gen_status_to_run_status() == "pending"


def test_gen_status_stage_order():
    assert gen_status_stage("not_started") == 0
    assert gen_status_stage("gate1_pending") == 5
    assert gen_status_stage("imported") == 11
    # 全链每个阶段可从 GEN_STATUSES 顺序推
    for i, gs in enumerate(GEN_STATUSES):
        assert GEN_STATUS_STAGE_INDEX[gs] == i


# ═══════════════════════════════════════════════════════════════════
# 5 · compute_progress（§3.3，构造 dict 纯计数）
# ═══════════════════════════════════════════════════════════════════

def _proposal(kind="case", status="proposed", review_state=None, consensus=None,
              testpoint_id=None, **extra):
    row = {"kind": kind, "status": status}
    if review_state is not None:
        row["review_state"] = review_state
    if consensus is not None:
        row["reviews"] = {"a": {}, "b": {}, "consensus": consensus}
    if testpoint_id is not None:
        row["testpoint_id"] = testpoint_id
    row.update(extra)
    return row


def test_progress_requirements_counts():
    reqs = [
        {"id": "r1", "gen_status": "imported"},
        {"id": "r2", "gen_status": "imported"},
        {"id": "r3", "gen_status": "gate1_pending"},
        {"id": "r4", "gen_status": "瞎写的"},  # 脏值并入 not_started
        {"id": "r5"},  # 缺列 = not_started
    ]
    p = compute_progress(requirements=reqs)
    assert p["requirements"]["total"] == 5
    by = p["requirements"]["by_status"]
    assert set(by) == set(GEN_STATUSES)  # 恒含全 12 键
    assert by["imported"] == 2
    assert by["gate1_pending"] == 1
    assert by["not_started"] == 2
    assert sum(by.values()) == 5


def test_progress_testpoints_from_proposals():
    proposals = [
        _proposal(kind="testpoint", status="proposed"),
        _proposal(kind="testpoint", status="proposed"),
        _proposal(kind="testpoint", status="keep"),
        _proposal(kind="testpoint", status="modify"),   # 人改端后入选
        _proposal(kind="testpoint", status="reject"),   # 门①打回
        _proposal(kind="testpoint", status="reject"),
        _proposal(kind="testpoint", status="reject"),
        _proposal(kind="case", status="reject"),        # 不串桶：case 的 reject 不算点
        _proposal(kind="dataset", status="proposed"),
    ]
    p = compute_progress(requirements=[], proposals=proposals)
    tp = p["testpoints"]
    assert tp["proposed"] == 2
    assert tp["confirmed"] == 2  # keep + modify
    assert tp["rejected"] == 3   # §3.3：rejected 从 proposal.status=reject(kind=testpoint) 聚合


def test_progress_testpoints_generated_fallback_and_formal_rows():
    # 无正式行：generated = 有入选用例的测试点去重数（提案近似）
    proposals = [
        _proposal(kind="case", status="keep", testpoint_id="tp1"),
        _proposal(kind="case", status="modify", testpoint_id="tp1"),  # 同点去重
        _proposal(kind="case", status="keep", testpoint_id="tp2"),
        _proposal(kind="case", status="keep", testpoint_id=None),     # 没挂点，忽略
        _proposal(kind="case", status="reject", testpoint_id="tp3"),  # reject 不算
    ]
    p = compute_progress(proposals=proposals)
    assert p["testpoints"]["generated"] == 2

    # 传正式行：§2.2 口径 status=confirmed|generated
    formal = [
        {"id": "tp1", "status": "confirmed"},
        {"id": "tp2", "status": "confirmed"},
        {"id": "tp3", "status": "generated"},
    ]
    p2 = compute_progress(proposals=proposals, testpoints=formal)
    assert p2["testpoints"]["confirmed"] == 2
    assert p2["testpoints"]["generated"] == 1


def test_progress_cases_all_keys():
    proposals = [
        # 刚落暂存，还没过机器门
        _proposal(kind="case", status="proposed", review_state="machine_pending"),
        # 机器门打回
        _proposal(kind="case", status="proposed", review_state="machine_rejected"),
        # 双评中（未出结论）
        _proposal(kind="case", status="proposed", review_state="in_duel"),
        # 双评中但已一致通过
        _proposal(kind="case", status="proposed", review_state="in_duel", consensus="agree"),
        # 分歧（不仲裁，带两份意见进门②）
        _proposal(kind="case", status="proposed", review_state="scored", consensus="split"),
        # 弃权（进人）
        _proposal(kind="case", status="proposed", review_state="in_duel", consensus="inconclusive"),
        # 打分完成
        _proposal(kind="case", status="proposed", review_state="scored"),
        # 打分完成但 <70 回派
        _proposal(kind="case", status="proposed", review_state="need_rework"),
        # 门②判决四态
        _proposal(kind="case", status="keep", review_state="scored"),
        _proposal(kind="case", status="modify", review_state="scored"),
        _proposal(kind="case", status="reject", review_state="scored"),
        _proposal(kind="case", status="defer", review_state="scored", question_id="q1"),
        # 不串桶
        _proposal(kind="testpoint", status="proposed"),
    ]
    p = compute_progress(requirements=[], proposals=proposals)
    c = p["cases"]
    assert c["proposed"] == 12          # kind=case 全部
    assert c["passed_machine"] == 9     # 过机器门=in_duel(4) + scored(5)
    assert c["in_duel"] == 2            # in_duel 且 consensus∈{None, agree}
    assert c["split"] == 1              # consensus=split（无论 review_state 落哪）
    assert c["inconclusive"] == 1       # 弃权单列（补键）
    assert c["scored"] == 6             # review_state=scored（含 split 那条）
    assert c["need_rework"] == 1
    assert c["machine_rejected"] == 1   # 补键
    assert c["kept"] == 2               # keep + modify
    assert c["rejected"] == 1
    assert c["deferred"] == 1


def test_progress_split_and_inconclusive_distinct():
    # split=两家分歧；inconclusive=弃权。两者都进人，但口径必须分开
    proposals = [
        _proposal(kind="case", consensus="split", review_state="scored"),
        _proposal(kind="case", consensus="inconclusive", review_state="in_duel"),
        _proposal(kind="case", consensus="agree", review_state="scored"),
    ]
    p = compute_progress(proposals=proposals)
    c = p["cases"]
    assert c["split"] == 1
    assert c["inconclusive"] == 1
    # agree 不进 split/inconclusive；scored 按列值算（split 那条列值也是 scored）
    assert c["scored"] == 2
    # in_duel 只数"还没出双评结论"的（consensus∈{None, agree}）；
    # inconclusive 那条列值虽是 in_duel，但结论已出（弃权进人），不重复计双评中
    assert c["in_duel"] == 0


def test_progress_coverage_states():
    cells = [
        {"state": "lit"},
        {"state": "lit"},
        {"state": "gap"},
        {"state": "na", "evidence": "无支付端，凭据见 PRD §3"},
        {"state": "WEIRD"},  # 未知态不丢，进 other
    ]
    p = compute_progress(cells=cells)
    cov = p["coverage"]
    assert cov["lit"] == 2
    assert cov["gap"] == 1
    assert cov["na"] == 1
    assert cov["other"] == 1
    assert set(COVERAGE_STATES) == {"lit", "gap", "na"}


def test_progress_pending_unanswered():
    questions = [
        {"status": "open"},
        {"status": "open"},
        {"status": "resolved", "resolution": "user_confirm"},
        {},  # 缺 status 按未答（宁严勿松）
    ]
    p = compute_progress(questions=questions)
    assert p["pending"]["unanswered"] == 3
    assert p["pending"]["budget_used"] == 0
    assert p["pending"]["budget_limit"] == 0
    # 无问题
    assert compute_progress(questions=None)["pending"]["unanswered"] == 0


def test_progress_budget_passthrough():
    # §2.0 列名 tokens_limit → §3.3 输出键 limit（两个键名都吃）
    budget = {"tokens_used": 42000, "tokens_limit": 100000, "state": "pressure"}
    p = compute_progress(budget=budget)
    assert p["budget"] == {"tokens_used": 42000, "limit": 100000, "state": "pressure"}
    assert p["pending"]["budget_used"] == 42000
    assert p["pending"]["budget_limit"] == 100000

    # limit 键名直给也行
    p2 = compute_progress(budget={"tokens_used": 1, "limit": 2, "state": "blocking"})
    assert p2["budget"]["limit"] == 2

    # 缺省
    p3 = compute_progress(budget=None)
    assert p3["budget"] == {"tokens_used": 0, "limit": 0, "state": "normal"}
    assert p3["pending"]["budget_used"] == 0


def test_progress_datasets_by_run_type():
    proposals = [
        _proposal(kind="dataset", status="proposed"),
        _proposal(kind="dataset", status="proposed"),
        _proposal(kind="dataset", status="keep"),
    ]
    # 计数型 run_type（默认 full）
    assert compute_progress(proposals=proposals)["datasets"]["proposed"] == 2
    assert compute_progress(proposals=proposals, run_type="plan")["datasets"]["proposed"] == 2
    assert compute_progress(proposals=proposals, run_type="incremental")["datasets"]["proposed"] == 2
    # 不产数据集的类型恒 0（不出现空卡）
    for rt in ("exec", "reopen"):
        assert compute_progress(proposals=proposals, run_type=rt)["datasets"]["proposed"] == 0


def test_progress_empty_everything():
    p = compute_progress()
    assert p["requirements"] == {"total": 0, "by_status": {s: 0 for s in GEN_STATUSES}}
    assert p["testpoints"] == {"proposed": 0, "confirmed": 0, "generated": 0, "rejected": 0}
    assert p["cases"]["proposed"] == 0
    assert p["coverage"] == {"lit": 0, "gap": 0, "na": 0, "other": 0}
    assert p["pending"] == {"unanswered": 0, "budget_used": 0, "budget_limit": 0}
    assert p["budget"]["state"] == "normal"
    assert p["datasets"] == {"proposed": 0}


def test_progress_proposal_enums_documented():
    # 取值域常量与 §2.3 列定义对齐（守防漂移）
    assert set(PROPOSAL_REVIEW_STATES) == {
        "machine_pending", "machine_rejected", "in_duel", "scored", "need_rework",
    }
    assert set(PROPOSAL_STATUSES) == {"proposed", "keep", "modify", "reject", "defer"}
    assert "split" not in PROPOSAL_REVIEW_STATES      # split 是 consensus 值，不是列值
    assert "inconclusive" not in PROPOSAL_REVIEW_STATES


def test_progress_consensus_read_from_reviews_jsonb():
    # consensus 藏在 reviews JSONB 里；兼容顶层 consensus 写法
    nested = {"kind": "case", "status": "proposed", "reviews": {"consensus": "split"}}
    flat = {"kind": "case", "status": "proposed", "consensus": "inconclusive"}
    p = compute_progress(proposals=[nested, flat])
    assert p["cases"]["split"] == 1
    assert p["cases"]["inconclusive"] == 1


# ═══════════════════════════════════════════════════════════════════
# 6 · notify_gate_open（§6.2 通知协议最小面）
# ═══════════════════════════════════════════════════════════════════

def test_three_gates_map():
    assert notify_gate_open("req_reviewing") == "gate0"
    assert notify_gate_open("points_proposed") == "gate1"
    assert notify_gate_open("cases_proposed") == "gate2"
    assert GATE_STATUSES == {
        "req_reviewing": "gate0",
        "points_proposed": "gate1",
        "cases_proposed": "gate2",
    }
    assert set(GATE_IDS) == {"gate0", "gate1", "gate2"}


def test_non_gate_statuses_return_none():
    for s in GEN_RUN_STATUSES:
        if s in GATE_STATUSES:
            continue
        assert notify_gate_open(s) is None, f"{s} 不是门态"


def test_gate_none_and_chinese():
    assert notify_gate_open(None) is None
    assert notify_gate_open("") is None
    assert notify_gate_open("随便") is None
    # 中文标签（§6.2 的门名直传也认）
    assert notify_gate_open("需求评审") == "gate0"
    assert notify_gate_open("需求确认") == "gate1"
    assert notify_gate_open("用例确认") == "gate2"
    # 门标识反向传入（调用方存了 gate id 再问一轮）
    assert notify_gate_open("gate1") == "gate1"


def test_gate_only_when_status_actually_reached():
    # 门态只在真正到达时通知：evaluating（评审中）不是门②
    assert notify_gate_open("evaluating") is None
    assert notify_gate_open("cases_confirmed") is None  # 已过门②不再叫门


# ═══════════════════════════════════════════════════════════════════
# 7 · 端到端小剧本（状态机推进一路 + progress 跟随）
# ═══════════════════════════════════════════════════════════════════

def test_full_run_lifecycle_walk():
    # full 型 run 沿主链推进，每个相邻对都合法；门态通知在正确时机触发
    chain = list(RUN_MAIN_CHAIN)
    for frm, to in zip(chain, chain[1:]):
        assert can_transition(frm, to)
    # 中途暂停/恢复
    assert can_transition("generating", "paused")
    assert can_transition("paused", "generating")
    # 返工回边后再走回来
    assert can_transition("evaluating", "generating")
    assert can_transition("generating", "evaluating")
    # done 后只能新开 reopen run
    assert not can_transition("done", "cases_proposed")


def test_reopen_is_a_new_run_not_a_transition():
    # §3.1 派生：门②后 REJECT → 新建 gen_run(run_type=reopen, parent_run_id)
    # 原 run 不动——所以 done 没有出边，reopen 是新 run 从 generating 起跑
    assert run_type_span("reopen") == ("generating", "done")
    assert not any(a == "done" for a, _ in TRANSITIONS)


def test_slowest_requirement_gates_the_run():
    # 门按需求放行，但 run 总闸取最慢（§3.2 原文场景）：
    # REQ-001 已过门①进生成，REQ-002 还在门①待确认 → run 停在 points_proposed
    reqs = [
        {"gen_status": "case_generating"},   # REQ-001 已放行
        {"gen_status": "gate1_pending"},     # REQ-002 还在门口
    ]
    assert gen_status_to_run_status(reqs) == "points_proposed"
    # 门①人确认 REQ-002 后 → run 进 generating
    reqs[1]["gen_status"] = "gate1_passed"
    assert gen_status_to_run_status(reqs) == "generating"
