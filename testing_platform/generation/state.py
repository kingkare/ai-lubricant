"""生成环节状态机（25 号文 **第三部分 §3.1/§3.2/§3.3** 的落地件）。

**纯函数 / 纯常量模块**：零 DB（不 import Tortoise、不接 ORM、不收 queryset）、
零 LLM、零 IO。输入输出全是 dict / 标量，可直接单测。调用方（编排器/调度器）
负责把行取成 dict 再喂进来——本模块只做"这张状态图允不允许"和"计数怎么聚合"。

三个表面，对应设计文档三节：

* **§3.1 run.status**（任务态）：``GEN_RUN_STATUSES`` / ``RUN_TYPES`` /
  ``RUN_MAIN_CHAIN`` / ``run_type_span`` / ``TRANSITIONS`` / ``TERMINAL_RUN_STATUSES``
  / ``can_transition`` / ``assert_transition``。
* **§3.2 gen_status**（每需求态，需求节点徽章数据源）：``GEN_STATUSES`` /
  ``GEN_STATUS_STAGE_INDEX`` / ``gen_status_to_run_status``。
* **§3.3 progress**（``gen_run.progress``，前端唯一渲染源）：``compute_progress``。
* **§6.2 通知协议最小面**：``notify_gate_open``（进门态 → 门标识）。

对外表面::

    from testing_platform.generation.state import (
        GEN_RUN_STATUSES, RUN_TYPES, GEN_STATUSES,
        run_type_span, gen_status_to_run_status,
        can_transition, assert_transition, compute_progress, notify_gate_open,
    )

    assert_transition("evaluating", "generating")          # 回边（返工）合法
    run_type_span("plan")                                  # ("pending", "points_proposed")
    gen_status_to_run_status(["gate1_pending", "case_generating"])  # → "points_proposed"（取最慢）
    progress = compute_progress(requirements, proposals, cells, questions, budget)
    gate = notify_gate_open("cases_proposed")              # → "gate2"（叫责任人开门）

字段名口径
----------
输入 dict 的键名对齐 **设计文档 §2.2 / §2.3 / §2.4 / §2.5**（``mc_test_*`` 列名）：

``mc_test_proposal``（§2.3）：``kind``（testpoint|case|dataset）、
``review_state``（machine_pending|machine_rejected|in_duel|scored|need_rework）、
``status``（proposed|keep|modify|reject|defer）、``reviews`` JSONB
（内含 ``consensus``：agree|split|inconclusive）、``testpoint_id``、``surface``、
``slot_key``、``score``。

``mc_test_testpoint``（§2.2）：``status``（confirmed|generated）、``requirement_id``、
``case_review_state``（machine_pending|in_duel|split|scored|need_rework|none）。

``mc_test_coverage_cell``（§2.5）：``state``（lit|gap|na）。

``mc_test_question``（§2.4 扩列复用现表）：``status``（open|resolved）。

``mc_test_requirement.gen_status``（§2.1 加列，§3.2 12 态）：本模块的 ``GEN_STATUSES``。

``gen_run.budget``（§2.0）：``{tokens_used, tokens_limit, state}``。

"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

__all__ = [
    # §3.1 run.status
    "GEN_RUN_STATUSES",
    "RUN_MAIN_CHAIN",
    "RUN_BYPASS_STATUSES",
    "RUN_STATUS_LABELS",
    "RUN_STATUS_ORDER",
    "TERMINAL_RUN_STATUSES",
    "ACTIVE_RUN_STATUSES",
    "RUN_TYPES",
    "RUN_TYPE_LABELS",
    "RUN_TYPE_SPANS",
    "run_type_span",
    "TRANSITIONS",
    "REWORK_BACK_EDGES",
    "MAIN_EDGES",
    "PAUSE_EDGES",
    "RESUME_EDGES",
    "CANCEL_EDGES",
    "can_transition",
    "assert_transition",
    "is_terminal_run_status",
    # §3.2 gen_status
    "GEN_STATUSES",
    "GEN_STATUS_LABELS",
    "GEN_STATUS_ALIASES",
    "GEN_STATUS_STAGE_INDEX",
    "GEN_STATUS_TO_RUN_STATUS",
    "normalize_gen_status",
    "gen_status_stage",
    "gen_status_to_run_status",
    # §3.3 progress
    "compute_progress",
    "RUN_TYPE_DATASET_COUNTED",
    # §6.2 通知协议最小面
    "GATE_STATUSES",
    "GATE_IDS",
    "notify_gate_open",
]


# ═══════════════════════════════════════════════════════════════════
# §3.1 · 生成任务 mc_test_gen_run.status（10 主链 + 3 旁路）
# ═══════════════════════════════════════════════════════════════════

#: 主链（§3.1 正文顺序）：pending → analyzing → req_reviewing（门⓪）
#: → points_proposed（门①）→ generating → evaluating → cases_proposed（门②）
#: → cases_confirmed → importing → done
RUN_MAIN_CHAIN: tuple[str, ...] = (
    "pending",            # 未开始：已建行、尚未派第一阶段
    "analyzing",          # 环节1 需求分析（九步+入库四标准）
    "req_reviewing",      # 门⓪ 需求评审（内含停顿点一）
    "points_proposed",    # 环节3/4 + 门① 需求确认（内含停顿点二）
    "generating",         # 环节5 用例细化（调度器扇出，dispatch-by-reference）
    "evaluating",         # 环节6 评审三道闸（机器门/双评/五维打分）
    "cases_proposed",     # 门② 用例确认（内含停顿点三）
    "cases_confirmed",    # 门② 判完，准备入库（§3.1 映射表"已过门②"对应位）
    "importing",          # 入库事务（case→mc_test_case，dataset→mc_test_dataset 回填）
    "done",               # 终态（=已入库；之后走场景化/执行/缺陷/报告等已有实体）
)

#: 旁路态（任务级控制态，§3.1 未逐条列举，本轮按任务口径补：
#: ``paused`` 由人点暂停（环节5 UI「暂停/取消」），``cancelled`` 由人取消，
#: ``failed`` 由异常/熔断收口。三者**不进 gen_status 映射**（映射只在主链上）。
RUN_BYPASS_STATUSES: tuple[str, ...] = (
    "paused",     # 暂停：记下暂停前态（resume_from），恢复时回原态
    "failed",     # 失败：异常/预算 Blocking 收口/熔断；不可恢复为业务态
    "cancelled",  # 取消：人为中止；终态
)

#: 全部合法 run.status（主链 + 旁路）。``mc_test_gen_run.status`` 的取值域。
GEN_RUN_STATUSES: tuple[str, ...] = RUN_MAIN_CHAIN + RUN_BYPASS_STATUSES

#: 状态序号（"最慢阶段"比较用）。主链 0..9；旁路态不参与比较（None 语义）。
RUN_STATUS_ORDER: dict[str, int] = {s: i for i, s in enumerate(RUN_MAIN_CHAIN)}

#: 终态：**无出边**（任何转换都拒）。``paused`` **不是**终态（可恢复回原态）。
#: 注意 ``req_reviewing`` / ``points_proposed`` / ``cases_proposed`` 是门态、
#: ``importing`` 是入库态，都不是终态——门态等的是人，不是流程结束。
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

#: 非终态（=旁路 paused/cancelled/failed 的可达源集，§3.1 旁路口径"任意非终态"）。
ACTIVE_RUN_STATUSES: frozenset[str] = frozenset(GEN_RUN_STATUSES) - TERMINAL_RUN_STATUSES

#: 中文标签（UI 徽章/日志可读用；不参与判定）。
RUN_STATUS_LABELS: dict[str, str] = {
    "pending": "未开始",
    "analyzing": "需求分析",
    "req_reviewing": "需求评审（门⓪）",
    "points_proposed": "需求确认（门①）",
    "generating": "用例生成",
    "evaluating": "用例评审",
    "cases_proposed": "用例确认（门②）",
    "cases_confirmed": "已过门②",
    "importing": "入库中",
    "done": "已入库",
    "paused": "已暂停",
    "failed": "失败",
    "cancelled": "已取消",
}

# ── run_type × 起止表（§3.1 v4.1 补，终审#6）─────────────────────

#: 六种生成类型（§2.0 ``mc_test_gen_run.run_type``）。
RUN_TYPES: tuple[str, ...] = (
    "full",              # 设计+用例，走全链
    "plan",              # 只到门⓪①，产设计（plan 型 run 停在 points_proposed）
    "exec",              # 从已确认测试点起（不重跑分析/提点）
    "pre_submit_edit",   # 提测前改草案：不重跑设计
    "incremental",       # 提测后增量：只增量范围
    "reopen",            # 门②后 REJECT 已入库用例派生的新 run（parent_run_id 指向原 run）
)

#: run_type 的人读标签。
RUN_TYPE_LABELS: dict[str, str] = {
    "full": "全量生成",
    "plan": "只做设计",
    "exec": "从测试点起执行生成",
    "pre_submit_edit": "提测前改草案",
    "incremental": "提测后增量",
    "reopen": "重开（派生）",
}

#: 起止表（§3.1 原表）。``closed=True`` 表示"到该态即关闭 run"——文档对 ``plan``
#: 写的是「points_proposed **关闭**」，即该门开完人确认后 run 收尾（不继续往下生成），
#: 所以 plan 的终点标 done；其余类型终点就是字面终态。
RUN_TYPE_SPANS: dict[str, tuple[str, str, bool]] = {
    "full": ("pending", "done", False),
    "plan": ("pending", "points_proposed", True),
    "exec": ("generating", "done", False),
    "pre_submit_edit": ("generating", "cases_proposed", False),
    "incremental": ("analyzing", "done", False),
    "reopen": ("generating", "done", False),
}


def run_type_span(run_type: str) -> tuple[str, str]:
    """返回某种 ``run_type`` 的 ``(起点态, 终点态)``（§3.1 起止表）。

    只回两个状态名（任务口径）。"终点态"的语义按类型不同：

    * ``full`` / ``exec`` / ``incremental`` / ``reopen`` → ``done``：走完全链收尾。
    * ``pre_submit_edit`` → ``cases_proposed``：改草案不重跑设计，开到门②即止。
    * ``plan`` → ``points_proposed``：文档写的是「points_proposed **关闭**」。
      ``plan_run_close_status()`` 给出该类型 run 真正收尾时写入的 status（``done``），
      因为现库/前端的"已关闭"判定看的是终态；若业务要把 plan run 就停在
      ``points_proposed`` 不写 done，用本函数返回的起点/终点做区间校验即可。

    Raises:
        ValueError: 未知 run_type。
    """
    span = RUN_TYPE_SPANS.get(run_type)
    if span is None:
        raise ValueError(
            f"未知 run_type={run_type!r}（合法：{'|'.join(RUN_TYPES)}）"
        )
    return span[0], span[1]


def plan_run_close_status(run_type: str) -> str:
    """该 run_type 走完后应写入的收尾 status。

    ``plan`` 特殊：起止表终点是门① ``points_proposed``（"关闭"= 不再往下生成），
    但收尾后 run 应当对"已关闭"过滤器可见，故返回 ``done``。其余类型返回起止表终点。
    """
    span = RUN_TYPE_SPANS.get(run_type)
    if span is None:
        raise ValueError(f"未知 run_type={run_type!r}（合法：{'|'.join(RUN_TYPES)}）")
    start, end, closes_at_gate = span
    return "done" if closes_at_gate else end


# ── 合法转换边集合 ────────────────────────────────────────────────
#
# 主链边（§3.1 正文序列）
MAIN_EDGES: frozenset[tuple[str, str]] = frozenset(
    (a, b) for a, b in zip(RUN_MAIN_CHAIN, RUN_MAIN_CHAIN[1:])
)
# 即 pending→analyzing→req_reviewing→points_proposed→generating→evaluating
#   →cases_proposed→cases_confirmed→importing→done（9 条）

#: 回边（§3.1「回边：evaluating → generating（返工：分数<70 或双评分歧回派，带 rewrite 链）」）。
#: 任务口径要求"含回边"；本轮只落文档明写的那条。见文件末「歧义」注释：
#: 是否补 cases_proposed→generating（门② modify 打回返工）未定。
REWORK_BACK_EDGES: frozenset[tuple[str, str]] = frozenset({("evaluating", "generating")})

#: 暂停边：任意非终态 → paused（3.1 无此枚举，按任务口径补旁路）。
PAUSE_EDGES: frozenset[tuple[str, str]] = frozenset(
    (s, "paused") for s in ACTIVE_RUN_STATUSES if s != "paused"
)

#: 恢复边（resume_from 语义）：paused → **暂停前那个态**，不是固定态。
#:
#: 本模块**不持久化** ``resume_from``——纯函数没有地方存它。约定：
#: 调用方在 ``paused = run.status`` 之前把原态记进 run（``mc_test_gen_run.status``
#: 无此列，本轮建议放 ``gen_run.config`` 或 ``decisions`` 流水里，由编排器负责），
#: 恢复时显式传入 ``assert_transition("paused", <原态>)``。
#: 这里只声明"回哪都合法"的边集（=任意非终态、且非 paused 本身）。
#: 语义铁律：**不得**把 paused 直接映射到某个固定主链态（会跳阶段丢产物）。
RESUME_EDGES: frozenset[tuple[str, str]] = frozenset(
    ("paused", s) for s in ACTIVE_RUN_STATUSES if s != "paused"
)

#: 取消边：任意非终态 → cancelled（终态，不可恢复）。
CANCEL_EDGES: frozenset[tuple[str, str]] = frozenset(
    (s, "cancelled") for s in ACTIVE_RUN_STATUSES if s != "paused"
)

#: 失败边：任意非终态 → failed。异常/预算跨 Blocking 线收口/熔断走这条。
FAIL_EDGES: frozenset[tuple[str, str]] = frozenset(
    (s, "failed") for s in ACTIVE_RUN_STATUSES if s != "paused"
)

#: paused 也可被取消/判失败（停久了收口），否则 paused 会成为死角。
PAUSED_EXIT_EDGES: frozenset[tuple[str, str]] = frozenset(
    {("paused", "cancelled"), ("paused", "failed")}
)

#: **全部合法转换边**。改这张表 = 改状态机策略；其余判定都从它派生。
#:
#: 不包含：旁路 → 主链（除 ``paused`` 恢复）、跨阶段前进（如 pending→generating）、
#: 倒退（如 generating→analyzing）、主链 → paused 之外的旁路直转。
TRANSITIONS: frozenset[tuple[str, str]] = (
    MAIN_EDGES
    | REWORK_BACK_EDGES
    | PAUSE_EDGES
    | RESUME_EDGES
    | CANCEL_EDGES
    | FAIL_EDGES
    | PAUSED_EXIT_EDGES
)

#: 旁路态集合（转换判定的"允许进入"侧）。
_BYPASS = set(RUN_BYPASS_STATUSES)


def _known(status: str) -> bool:
    return status in GEN_RUN_STATUSES


def is_terminal_run_status(status: str) -> bool:
    """该 run.status 是否终态（无出边）。未知态按非终态处理并由调用方报错。"""
    return status in TERMINAL_RUN_STATUSES


def can_transition(frm: str, to: str) -> bool:
    """``run.status`` 从 ``frm`` 到 ``to`` 是否合法（§3.1 状态图 + 旁路口径）。

    * 未知状态名 → ``False``（不抛，供上层做守卫式判断；要报错用 ``assert_transition``）。
    * 自环（``frm == to``）→ ``False``：状态机只在**变化**时走边，幂等写回由
      调用方判断"同态即跳过"，不给自环开绿灯（防"重复推进"被当成合法转换掩盖）。
    * 终态（done/failed/cancelled）作为源 → ``False``（不可复活；重开走
      ``run_type=reopen`` 新建 run，见 §3.1 派生说明）。
    """
    if not _known(frm) or not _known(to):
        return False
    if frm == to:
        return False
    return (frm, to) in TRANSITIONS


def assert_transition(frm: str, to: str) -> None:
    """校验转换边，非法则抛 ``ValueError``（带可读信息 + 可选目标清单）。

    读侧/编排侧用它做写库前的最后一道闸；纯展示判断用 ``can_transition``。
    """
    if not _known(frm):
        raise ValueError(
            f"未知 run.status：{frm!r}（合法：{'|'.join(GEN_RUN_STATUSES)}）"
        )
    if not _known(to):
        raise ValueError(
            f"未知 run.status：{to!r}（合法：{'|'.join(GEN_RUN_STATUSES)}）"
        )
    if frm == to:
        raise ValueError(
            f"run.status 转换非法：{frm!r} → {to!r}（同态自环不算转换，写回请自行判等跳过）"
        )
    if (frm, to) in TRANSITIONS:
        return
    if is_terminal_run_status(frm):
        raise ValueError(
            f"run.status 转换非法：{frm!r} 是终态，出边为空"
            "（重开请新建 gen_run(run_type=reopen, parent_run_id=原run)，不改本 run）"
        )
    allowed = sorted({b for a, b in TRANSITIONS if a == frm})
    raise ValueError(
        f"run.status 转换非法：{frm!r} → {to!r}；{frm!r} 的合法目标："
        f"{allowed}（旁路 paused/cancelled/failed 另算）"
    )


# ═══════════════════════════════════════════════════════════════════
# §3.2 · 每需求进度 mc_test_requirement.gen_status（12 态，英文蛇形）
# ═══════════════════════════════════════════════════════════════════
#
# 设计文档用中文名（未开始 → 分析中 → 需求评审 → 已过评审 → 提点中 → 门①待确认
# → 已过门① → 用例生成 → 用例评审 → 门②待确认 → 已过门② → 已入库）。
# 落库要英文蛇形，命名口径：**与 run.status 一一对应**（§3.2 v4 命名修正——
# "需求侧叫需求评审、用例侧叫用例评审，不再共用评审中"）。
# 中文注释保留原字，前端按 GEN_STATUS_LABELS 渲染徽章。

#: 12 态（顺序=§3.2 链）。
GEN_STATUSES: tuple[str, ...] = (
    "not_started",        # 未开始（灰节点，不进流水线）
    "analyzing",          # 分析中（环节1）
    "req_reviewing",      # 需求评审（门⓪）
    "req_reviewed",       # 已过评审
    "point_proposing",    # 提点中（环节3/4）
    "gate1_pending",      # 门①待确认
    "gate1_passed",       # 已过门①
    "case_generating",    # 用例生成（环节5）
    "case_reviewing",     # 用例评审（环节6）
    "gate2_pending",      # 门②待确认
    "gate2_passed",       # 已过门②
    "imported",           # 已入库
)

#: 中文标签（与 §3.2 原文逐字对齐）。
GEN_STATUS_LABELS: dict[str, str] = {
    "not_started": "未开始",
    "analyzing": "分析中",
    "req_reviewing": "需求评审",
    "req_reviewed": "已过评审",
    "point_proposing": "提点中",
    "gate1_pending": "门①待确认",
    "gate1_passed": "已过门①",
    "case_generating": "用例生成",
    "case_reviewing": "用例评审",
    "gate2_pending": "门②待确认",
    "gate2_passed": "已过门②",
    "imported": "已入库",
}

#: 英文蛇形的常见别名/直传中文名归一到标准值（前端/导入脚本可能直传中文，
#: 文档正文是中文口径，故这里做双向兼容；未知值归一为 ``None`` 由调用方决定）。
GEN_STATUS_ALIASES: dict[str, str] = {
    **{zh: en for en, zh in GEN_STATUS_LABELS.items()},
    "unstarted": "not_started",
    "notstarted": "not_started",
    "point_proposed": "point_proposing",
    "points_proposing": "point_proposing",
    "gate1_confirming": "gate1_pending",
    "gate2_confirming": "gate2_pending",
    "case_proposed": "gate2_pending",
    "done": "imported",
    "importing": "gate2_passed",
}


def normalize_gen_status(value: Any) -> str | None:
    """把任意写法（英文/中文/常见别名）归一成 ``GEN_STATUSES`` 里的标准值。

    未知值 → ``None``（不抛：progress 聚合要能容忍脏数据/老行缺列，
    脏值按"未开始"参与"取最慢"，见 ``gen_status_stage``）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in GEN_STATUS_LABELS:
        return text
    if text in GEN_STATUS_ALIASES:
        return GEN_STATUS_ALIASES[text]
    lowered = text.lower().replace("-", "_").replace(" ", "_")
    if lowered in GEN_STATUS_LABELS:
        return lowered
    if lowered in GEN_STATUS_ALIASES:
        return GEN_STATUS_ALIASES[lowered]
    return None


#: 阶段序号（"取最慢"的比较基准）。链上单调递增，故 min=最慢、max=最快。
GEN_STATUS_STAGE_INDEX: dict[str, int] = {s: i for i, s in enumerate(GEN_STATUSES)}


def gen_status_stage(gen_status: Any) -> int:
    """该 gen_status 的阶段序号（越小越慢）。未知/空值按 ``not_started``（0）。"""
    std = normalize_gen_status(gen_status)
    if std is None:
        return GEN_STATUS_STAGE_INDEX["not_started"]
    return GEN_STATUS_STAGE_INDEX[std]


# ── gen_status → run.status 映射（§3.1 表，12 对 10）────────────
#
#   未开始/分析中        → analyzing
#   需求评审/已过评审     → req_reviewing
#   提点中/门①待确认      → points_proposed
#   已过门①/用例生成      → generating
#   用例评审            → evaluating     ← §3.1 括号「评审中=evaluating」
#   门②待确认           → cases_proposed
#   已过门②            → cases_confirmed
#   已入库             → done
#
# run 态里 ``pending``（run 自己已建行还没派活）与 ``importing``（入库事务，
# run 级动作、不属任一需求）没有需求级对应 —— 所以是 12 对 10 而非 12 对 12。
GEN_STATUS_TO_RUN_STATUS: dict[str, str] = {
    "not_started": "analyzing",
    "analyzing": "analyzing",
    "req_reviewing": "req_reviewing",
    "req_reviewed": "req_reviewing",
    "point_proposing": "points_proposed",
    "gate1_pending": "points_proposed",
    "gate1_passed": "generating",
    "case_generating": "generating",
    "case_reviewing": "evaluating",
    "gate2_pending": "cases_proposed",
    "gate2_passed": "cases_confirmed",
    "imported": "done",
}


def _slowest_gen_status(values: Iterable[Any]) -> str:
    """在一组 gen_status 里取**最慢**阶段的标准值（§3.2「总闸」口径）。"""
    slowest: str | None = None
    slowest_idx = GEN_STATUS_STAGE_INDEX["imported"] + 1
    for raw in values:
        std = normalize_gen_status(raw)
        idx = GEN_STATUS_STAGE_INDEX["not_started"] if std is None else GEN_STATUS_STAGE_INDEX[std]
        if idx < slowest_idx:
            slowest_idx = idx
            slowest = std if std is not None else "not_started"
    return slowest or "not_started"


def gen_status_to_run_status(gen_statuses: Any = None, /, **kwargs: Any) -> str:
    """需求们的 ``gen_status`` 集合 → ``run.status``（取**最慢**需求映射，§3.1/§3.2）。

    接受多种输入形状（编排侧调用方便取）：

    * 字符串/中文/英文混列：``["points_proposing…", "gate1_passed"]``
    * dict 列表（需求行 dict，读 ``gen_status`` 键；缺键按未开始）
    * ``requirements=`` 关键字（同上）

    规则：
    1. 空集合 → ``pending``（还没需求可算，run 尚未派活）。
    2. 否则取最慢需求的阶段，按 ``GEN_STATUS_TO_RUN_STATUS`` 映射。
    3. 脏值/缺列按 ``not_started`` 参与比较（=最慢，宁可不前进也不误报进度）。
    4. 若当前 run.status 已比映射结果更靠前（例如已 ``generating`` 而某需求
       回落到 ``gate1_pending``），本函数**只看需求集合**，不读旧 status——
       "总闸"由调用方决定是否回写；状态图是否允许该回写另用 ``can_transition`` 判。

    Returns: 主链上的 run.status 取值之一（永不返回 paused/failed/cancelled）。
    """
    values: list[Any] = []
    if gen_statuses is not None:
        # 容忍裸字符串：单个 gen_status 写成 str 而非 [str]
        if isinstance(gen_statuses, (str, bytes)):
            values = [gen_statuses]
        elif isinstance(gen_statuses, Mapping):
            values = [gen_statuses]
        else:
            try:
                values = list(gen_statuses)
            except TypeError:
                values = [gen_statuses]
    if not values and "requirements" in kwargs:
        values = list(kwargs["requirements"] or [])
    if not values:
        return "pending"

    std_values: list[Any] = []
    for item in values:
        if isinstance(item, Mapping):
            std_values.append(item.get("gen_status"))
        else:
            std_values.append(item)
    return GEN_STATUS_TO_RUN_STATUS[_slowest_gen_status(std_values)]


# ═══════════════════════════════════════════════════════════════════
# §3.3 · progress 对象（gen_run.progress，前端唯一渲染源）
# ═══════════════════════════════════════════════════════════════════

#: 哪些 run_type 的 dataset 提案要计入 ``progress.datasets.proposed``。
#: 其余类型（如纯 exec/reopen）不产数据集草案，计数恒 0，避免前端出现空卡。
RUN_TYPE_DATASET_COUNTED: frozenset[str] = frozenset(
    {"full", "plan", "incremental", "pre_submit_edit"}
)

#: ``proposal.review_state`` 取值域（§2.3 列定义）。
#: ``split`` / ``inconclusive`` **不是列值**——它们来自 ``reviews.consensus``
#: （§2.3：``{a:…, b:…, consensus: agree|split|inconclusive}``）。
PROPOSAL_REVIEW_STATES: tuple[str, ...] = (
    "machine_pending",
    "machine_rejected",
    "in_duel",
    "scored",
    "need_rework",
)

#: ``proposal.status`` 取值域（§2.3：proposed → keep|modify|reject|defer）。
PROPOSAL_STATUSES: tuple[str, ...] = ("proposed", "keep", "modify", "reject", "defer")

#: ``mc_test_coverage_cell.state`` 取值域（§2.5）。
COVERAGE_STATES: tuple[str, ...] = ("lit", "gap", "na")

#: ``reviews.consensus`` 取值域（§2.3）。
CONSENSUS_VALUES: tuple[str, ...] = ("agree", "split", "inconclusive")


def _iter_dicts(rows: Any) -> list[Mapping[str, Any]]:
    """把入参收敛成"dict 列表"：None→[]，单个 dict→[它]，其它可迭代→过滤非 dict。"""
    if rows is None:
        return []
    if isinstance(rows, Mapping):
        return [rows]
    if isinstance(rows, (str, bytes)):
        return []
    out: list[Mapping[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            out.append(row)
    return out


def _get(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """按候选键名取第一个非 None 值（容忍 ``status`` / ``gen_status`` 等别名写法）。"""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _consensus_of(proposal: Mapping[str, Any]) -> str | None:
    """取提案的双评共识（``reviews.consensus``）；兼容顶层 ``consensus``。"""
    reviews = _get(proposal, "reviews", default=None)
    if isinstance(reviews, Mapping):
        value = reviews.get("consensus")
        if value is not None:
            return str(value).strip().lower() or None
    value = _get(proposal, "consensus")
    return (str(value).strip().lower() or None) if value is not None else None


def compute_progress(
    requirements: Any = None,
    proposals: Any = None,
    cells: Any = None,
    questions: Any = None,
    budget: Any = None,
    *,
    run_type: str | None = None,
    testpoints: Any = None,
) -> dict[str, Any]:
    """聚合 ``gen_run.progress``（§3.3 结构，前端唯一渲染源）。**纯计数，无 ORM**。

    Args:
        requirements: 需求行 dict 列表（§2.1 ``mc_test_requirement``）。读的键：
            ``gen_status``（缺键/脏值按未开始计数）。
        proposals: 提案行 dict 列表（§2.3 ``mc_test_proposal``，全 kind 混投）。
            读的键：``kind``（testpoint|case|dataset）、``status``
            （proposed|keep|modify|reject|defer）、``review_state``
            （machine_pending|machine_rejected|in_duel|scored|need_rework）、
            ``reviews.consensus``（agree|split|inconclusive）。
        cells: 覆盖格 dict 列表（§2.5 ``mc_test_coverage_cell``）。读 ``state``（lit|gap|na）。
        questions: 问题 dict 列表（§2.4 ``mc_test_question``）。读 ``status``（open|resolved）。
        budget: ``gen_run.budget`` JSONB：``{tokens_used, tokens_limit, state}``。
            透传为 ``{tokens_used, limit, state}``——§3.3 叫 ``limit``，
            列名口径是 ``tokens_limit``（§2.0），故两个键名都吃、输出用 ``limit``。
        run_type: 本次 run 类型，只影响 ``datasets.proposed`` 是否计数
            （见 ``RUN_TYPE_DATASET_COUNTED``）。None 按 ``full`` 处理。
        testpoints: **正式**测试点行 dict 列表（§2.2 ``mc_test_testpoint``，可选）。
            §3.3 的 ``testpoints.confirmed`` / ``generated`` 是"已入库正式行"口径
            （门①建行的 status=confirmed→generated），**不能**从暂存提案推；
            调用方若已另行取到正式行就传进来，不传则回落到提案近似口径（见下）。

    Returns:
        §3.3 的字典结构（键名逐字对齐）::

            {
              "requirements": {"total": int, "by_status": {<12 个 gen_status>: int}},
              "testpoints":   {"proposed": int, "confirmed": int,
                               "generated": int, "rejected": int},
              "cases":        {"proposed": int, "passed_machine": int, "in_duel": int,
                               "split": int, "scored": int, "need_rework": int,
                               "kept": int, "rejected": int, "deferred": int,
                               "machine_rejected": int, "inconclusive": int},
              "coverage":     {"lit": int, "gap": int, "na": int},
              "pending":      {"unanswered": int, "budget_used": int, "budget_limit": int},
              "budget":       {"tokens_used": int, "limit": int, "state": str},
              "datasets":     {"proposed": int},
            }

    计数口径（每键一条）
    -------------------
    * ``requirements.total`` = 传入需求条数；``by_status`` 恒含 12 个键（缺失补 0），
      脏值/缺列并进 ``not_started``。
    * ``testpoints.proposed`` = ``kind=testpoint`` 且 ``status=proposed`` 的提案数。
    * ``testpoints.confirmed``：传了 ``testpoints`` → 正式行 ``status=confirmed`` 计数；
      否则 = ``kind=testpoint`` 且 ``status`` ∈ {keep, modify}（门①"确认入选"的提案态近似，
      含 modify=人改后端归属后入选）。
    * ``testpoints.generated``：传了 ``testpoints`` → 正式行 ``status=generated`` 计数；
      否则 = ``kind=case`` 且 ``status`` ∈ {keep, modify} 的提案去重 ``testpoint_id`` 数
      （§2.2 generated=每端都有 confirmed 用例，这里只按"该点已有入选用例"近似）。
    * ``testpoints.rejected`` = ``kind=testpoint`` 且 ``status=reject``（§3.3 明写的聚合口径）。
    * ``cases.proposed`` = ``kind=case`` 的全部提案数（进暂存即算提出，含后续判决）。
    * ``cases.passed_machine`` = ``kind=case`` 且 ``review_state`` ∈
      {in_duel, scored}（已过机器门；含正在双评的）。
    * ``cases.in_duel`` = ``review_state=in_duel`` 且 consensus ∈ {agree, None}
      （尚未出双评结论/一致通过）。
    * ``cases.split`` = consensus=split（**分歧不仲裁**，带两份意见进门②，§环节6）。
    * ``cases.inconclusive`` = consensus=inconclusive（**弃权进人**；§3.3 cases 键没列它，
      但 UI「整组保留前置」把它与 split 同等对待，故单列一个键，不动文档已有的 9 个键）。
    * ``cases.scored`` = ``review_state=scored``（五维打分完成，门②可审）。
    * ``cases.need_rework`` = ``review_state=need_rework``（<70 回派，review_round+1）。
    * ``cases.machine_rejected`` = ``review_state=machine_rejected``（机器门打回；
      同样是为 UI 徽章 machine_rejected 单列——§环节6 UI 徽章有这个态，§3.3 cases 没列，
      补键不删键，向后兼容）。
    * ``cases.kept`` = ``kind=case`` 且 ``status`` ∈ {keep, modify}（门②保留/改后保留）。
    * ``cases.rejected`` = ``kind=case`` 且 ``status=reject``。
    * ``cases.deferred`` = ``kind=case`` 且 ``status=defer``（挂 question_id，答完自动复活）。
    * ``coverage.{lit,gap,na}`` = 覆盖格按 ``state`` 计数；未知 state 不丢，
      额外并入 ``coverage.other``（前端不读不报错，只防统计口径悄悄掉格）。
    * ``pending.unanswered`` = question 中 ``status`` 非 resolved（含 None/脏值）的条数
      —— 现库语义"open 即阻塞"（§2.4 assert_not_blocked），故只排除明确 resolved。
    * ``pending.budget_used`` / ``pending.budget_limit`` = 同 ``budget`` 段的两个数值
      （§3.3 写的是「budget_used/上限」，落在提问预算上=门⓪①各 10-15 问，
      本轮按任务口径透传 budget；真正的提问计数需要 question 带 ``gate``/``round_no``
      列时再补，见「歧义」注释）。
    * ``budget`` = 透传（缺键补 0 / normal）。
    * ``datasets.proposed`` = ``kind=dataset`` 且 ``status=proposed``（§3.3 未列，
      但门②入库映射含 dataset，前端要有数才不空转，故单列一段，不影响已有键）。
    """
    req_rows = _iter_dicts(requirements)
    pro_rows = _iter_dicts(proposals)
    cell_rows = _iter_dicts(cells)
    q_rows = _iter_dicts(questions)
    tp_rows = _iter_dicts(testpoints)

    # ── requirements ──────────────────────────────────────────────
    by_status: dict[str, int] = {s: 0 for s in GEN_STATUSES}
    for row in req_rows:
        std = normalize_gen_status(_get(row, "gen_status", "status"))
        by_status[std if std in by_status else "not_started"] += 1

    # ── proposals 分桶 ────────────────────────────────────────────
    tp_proposed = tp_confirmed = tp_generated = tp_rejected = 0
    c_proposed = c_passed_machine = c_in_duel = c_split = 0
    c_scored = c_need_rework = c_machine_rejected = c_inconclusive = 0
    c_kept = c_rejected = c_deferred = 0
    ds_proposed = 0
    kept_testpoint_ids: set[Any] = set()

    for row in pro_rows:
        kind = str(_get(row, "kind", default="") or "").strip().lower()
        status = str(_get(row, "status", default="") or "").strip().lower()
        review_state = str(_get(row, "review_state", default="") or "").strip().lower()
        consensus = _consensus_of(row)

        if kind == "testpoint":
            if status == "proposed":
                tp_proposed += 1
            elif status in ("keep", "modify"):
                tp_confirmed += 1
            elif status == "reject":
                tp_rejected += 1
        elif kind == "case":
            c_proposed += 1
            if status == "keep":
                c_kept += 1
                kept_testpoint_ids.add(_get(row, "testpoint_id", "tp_id"))
            elif status == "modify":
                c_kept += 1
                kept_testpoint_ids.add(_get(row, "testpoint_id", "tp_id"))
            elif status == "reject":
                c_rejected += 1
            elif status == "defer":
                c_deferred += 1
            # 评审态维度（与判决维度正交：一条提案两边各有值）
            if review_state in ("in_duel", "scored"):
                c_passed_machine += 1
            if review_state == "in_duel":
                if consensus in (None, "", "agree"):
                    c_in_duel += 1
            elif review_state == "scored":
                c_scored += 1
            elif review_state == "need_rework":
                c_need_rework += 1
            elif review_state == "machine_rejected":
                c_machine_rejected += 1
            # consensus 维度独立计数（split/inconclusive 是评审结论，不是列值）
            if consensus == "split":
                c_split += 1
            elif consensus == "inconclusive":
                c_inconclusive += 1
        elif kind == "dataset":
            if status in ("", "proposed"):
                ds_proposed += 1

    # 正式测试点行优先（口径准确：§2.2 status 只有 confirmed→generated 两态）；
    # 没传正式行时，用提案近似——generated = 该点已有 keep/modify 用例的点去重数。
    if tp_rows:
        formal_statuses = [
            str(_get(row, "status", default="") or "").strip().lower() for row in tp_rows
        ]
        tp_confirmed = sum(1 for s in formal_statuses if s == "confirmed")
        tp_generated = sum(1 for s in formal_statuses if s == "generated")
    else:
        tp_generated = len({tid for tid in kept_testpoint_ids if tid is not None})

    # ── coverage ──────────────────────────────────────────────────
    cov = {s: 0 for s in COVERAGE_STATES}
    cov_other = 0
    for row in cell_rows:
        state = str(_get(row, "state", default="") or "").strip().lower()
        if state in cov:
            cov[state] += 1
        else:
            cov_other += 1

    # ── questions（pending）───────────────────────────────────────
    unanswered = 0
    for row in q_rows:
        status = str(_get(row, "status", default="") or "").strip().lower()
        if status != "resolved":
            unanswered += 1

    # ── budget（§2.0 列名 tokens_limit → §3.3 输出键 limit）────────
    if isinstance(budget, Mapping):
        tokens_used = int(_get(budget, "tokens_used", "used", default=0) or 0)
        limit = int(
            _get(budget, "limit", "tokens_limit", "tokens_used_limit", default=0) or 0
        )
        state = str(_get(budget, "state", default="") or "").strip().lower() or "normal"
    else:
        tokens_used, limit, state = 0, 0, "normal"

    counted_run_type = run_type or "full"
    datasets = {"proposed": ds_proposed if counted_run_type in RUN_TYPE_DATASET_COUNTED else 0}

    return {
        "requirements": {"total": len(req_rows), "by_status": by_status},
        "testpoints": {
            "proposed": tp_proposed,
            "confirmed": tp_confirmed,
            "generated": tp_generated,
            "rejected": tp_rejected,
        },
        "cases": {
            "proposed": c_proposed,
            "passed_machine": c_passed_machine,
            "in_duel": c_in_duel,
            "split": c_split,
            "scored": c_scored,
            "need_rework": c_need_rework,
            "kept": c_kept,
            "rejected": c_rejected,
            "deferred": c_deferred,
            # 补键（不删文档已有键）：机器门打回 / 弃权进人
            "machine_rejected": c_machine_rejected,
            "inconclusive": c_inconclusive,
        },
        "coverage": {**cov, "other": cov_other},
        "pending": {
            "unanswered": unanswered,
            "budget_used": tokens_used,
            "budget_limit": limit,
        },
        "budget": {"tokens_used": tokens_used, "limit": limit, "state": state},
        "datasets": datasets,
    }


# ═══════════════════════════════════════════════════════════════════
# §6.2 · 通知协议最小面：进门态 → 门标识
# ═══════════════════════════════════════════════════════════════════

#: 三个人工停顿点（§6.2 通知协议用的是**实际枚举名**，v4.1 修正终审#6）。
GATE_STATUSES: dict[str, str] = {
    "req_reviewing": "gate0",     # 门⓪ 需求评审
    "points_proposed": "gate1",   # 门① 需求确认
    "cases_proposed": "gate2",    # 门② 用例确认
}

#: 门标识 → 人读名称（通知文案用；本模块只给 id，发送由 realtime/站内通道做）。
GATE_IDS: dict[str, str] = {
    "gate0": "需求评审（门⓪）",
    "gate1": "需求确认（门①）",
    "gate2": "用例确认（门②）",
}


def notify_gate_open(run_status: Any) -> str | None:
    """进入门态时返回门标识（``gate0|gate1|gate2``），否则 ``None``。

    通知协议的最小面：调用方拿到非 None 就走现有通道（realtime + 站内）
    叫责任人开门，**不打弹窗**（§6.2）。中文门名也可传入（归一后判断）。
    """
    if run_status is None:
        return None
    text = str(run_status).strip()
    # 兼容中文标签（run 态标签里含"（门⓪）"等后缀，先精确查再按别名归一）
    if text in GATE_STATUSES:
        return GATE_STATUSES[text]
    aliases = {
        "req_reviewing": "req_reviewing",
        "points_proposed": "points_proposed",
        "cases_proposed": "cases_proposed",
        "需求评审": "req_reviewing",
        "需求确认": "points_proposed",
        "用例确认": "cases_proposed",
        "gate0": "req_reviewing",
        "gate1": "points_proposed",
        "gate2": "cases_proposed",
    }
    std = aliases.get(text) or aliases.get(text.lower())
    return GATE_STATUSES[std] if std else None


# ═══════════════════════════════════════════════════════════════════
# 歧义与自行裁量记录（供评审回看）
# ═══════════════════════════════════════════════════════════════════
#
# 1. **§3.1 明写的回边只有 ``evaluating → generating``**。任务要求"含回边"，
#    本轮**只落文档明写的那条**，不自行加 ``cases_proposed → generating``
#    （门② modify 打回返工）——门② modify 的口径是"就地编辑提案后重新过评审"
#    （环节6/门②步骤3），编辑后停在 ``cases_proposed`` 等下一轮判决更贴文档；
#    若产品确认"打回重生成"，加一条边即可（加进 REWORK_BACK_EDGES）。
#
# 2. **旁路态（paused/failed/cancelled）§3.1 正文未列**，只在任务描述里要求。
#    本轮按任务口径实现为"任意非终态可达 + paused 可恢复"，并把
#    ``paused`` 恢复的**原态记忆**（``resume_from``）留给调用方——
#    本模块无存储，不可能自己记；建议写在 ``gen_run.config`` 或 ``decisions`` 流水里。
#
# 3. **``plan`` 型的终点**：§3.1 表写「points_proposed 关闭」。本模块
#    ``run_type_span`` 返回字面值 ``("pending", "points_proposed")``，
#    另给 ``plan_run_close_status()`` = ``done``（现库/前端按终态过滤"已关闭"）。
#    两处口径分离是为了不逼产品改一种读法。
#
# 4. **§3.3 progress 的 ``cases`` 只列 9 键**，但环节6 UI 徽章另有
#    ``machine_rejected``，双评另有 ``inconclusive``。本轮**补两个键不删原键**
#    （前端读原键不受影响；新键前端不读也不报错）。
#    ``datasets.proposed`` 同理补一段（门②入库映射含 dataset）。
#
# 5. **``pending.budget_used/budget_limit``**：§3.3 说"从 question 查"，但真正
#    的提问计数依赖 §2.4 扩列的 ``gate``/``round_no``（现库尚未上线）。
#    本轮**透传 run 级 budget** 作为预算显示源；等 question 扩列落地后，
#    由调用方把 question 聚合出的提问数以 ``pending.budget_used`` 覆盖即可。
#
# 6. **``requirements.by_status`` 的键名**：文档写「各 gen_status 计数」没给
#    具体键名。本模块用**英文蛇形全量 12 键**（与 ``GEN_STATUSES`` 一致、
#    恒含补 0），不用中文做 JSON 键（前端按 ``GEN_STATUS_LABELS`` 渲染）。
