"""生成环节数据模型（25 号文 v4.1 第二部分）测试——纯单测，不依赖 PG。

覆盖（sqlite::memory:，模式照 ``tests/test_design_skeleton.py``）：

1. 模型字段契约：五张表逐表「模型列集合 == DDL 列集合」（防模型/DDL 漂移）、
   默认值与可空性、unique_together。
2. 枚举常量与 25 号文原文一致（六 run_type / 10 态 / 门态等）。
3. DDL 语法 sanity：sqlite 内存连接把整份 DDL（建表 + 索引）跑一遍，再跑一遍
   证明 ``IF NOT EXISTS`` 幂等；同时 Tortoise ``generate_schemas`` 走模型建表
   路径插真实行读回，证明模型定义本身可落库、DB 层默认值成立。
4. ``ensure_question_columns``：sqlite 临时表上扩列 + 幂等（跑两次不报错、
   第二次 added=[]、列存在、旧数据保留）。

运行：``python -m pytest tests/test_generation_models.py -q``
"""
from __future__ import annotations

import os
import re
import sys
import uuid

import pytest
import pytest_asyncio

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from testing_platform.generation import models as gm  # noqa: E402


@pytest_asyncio.fixture
async def tortoise_db():
    from tortoise import Tortoise

    await Tortoise.init(
        db_url="sqlite::memory:",
        modules={"testing_platform": ["testing_platform.generation.models"]},
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


# ── 表 → 模型 映射 + DDL 解析工具 ─────────────────────────────

_TABLES = {
    "mc_test_gen_run": gm.TestGenRun,
    "mc_test_testpoint": gm.TestTestpoint,
    "mc_test_proposal": gm.TestProposal,
    "mc_test_coverage_cell": gm.TestCoverageCell,
    "mc_test_rule": gm.TestRule,
}

_CONSTRAINT_KEYWORDS = frozenset(
    {"UNIQUE", "PRIMARY", "FOREIGN", "CHECK", "CONSTRAINT", "EXCLUDE"}
)


def _ddl_create(table: str) -> str:
    """从模块 DDL 里取某张表的 CREATE TABLE 语句（索引语句单独跑）。"""
    for stmt in gm.DDL:
        if f"CREATE TABLE IF NOT EXISTS {table}" in stmt:
            return stmt
    raise AssertionError(f"DDL 里没有 {table} 的建表语句")


def _ddl_columns(create_sql: str) -> set[str]:
    """解析 CREATE TABLE 顶层列名（去约束行/索引行；引号列名去引号）。"""
    # +1 去掉外层建表括号本体，否则第一层 depth 恒为 1、逗号不切分
    body = create_sql[create_sql.index("(") + 1 : create_sql.rindex(")")]
    # 先去 SQL 行注释（DDL 里给 DBA 看的 -- 中文说明，列名解析要剔除）
    body = re.sub(r"--[^\n]*", "", body)
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
            if depth == 0:
                continue  # 理论不可达；列内括号（VARCHAR(32)）depth 已 +1，字符保留无害
        elif ch == ")":
            depth -= 1
        if depth == 0 and ch == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    cols: set[str] = set()
    for part in parts:
        token = part.split()[0].strip('"') if part.split() else ""
        if token.upper() in _CONSTRAINT_KEYWORDS:
            continue
        cols.add(token)
    return cols


def _model_columns(model) -> set[str]:
    """模型的真实 DB 列名集合（含 pk；本包无 FK 对象，fields_map 即列名）。"""
    return {name for name in model._meta.fields_map if not name.startswith("~")}


def _resolved_default(field):
    """解析字段默认值：JSONField(default=dict) 的 default 属性是可调用对象本身。"""
    d = field.default
    return d() if callable(d) else d


# ── 1) 模型字段契约 ──────────────────────────────────────────


@pytest.mark.parametrize("table", sorted(_TABLES))
def test_model_columns_match_ddl(table):
    """每张表：模型列集合 == DDL 列集合（模型/DDL 漂移当场红）。"""
    model = _TABLES[table]
    ddl_cols = _ddl_columns(_ddl_create(table))
    model_cols = _model_columns(model)
    assert model_cols == ddl_cols, (
        f"{table}: 模型与 DDL 列集合不一致 "
        f"model_only={sorted(model_cols - ddl_cols)} ddl_only={sorted(ddl_cols - model_cols)}"
    )


def test_ddl_contains_all_tables_and_indexes():
    """DDL 必须包含五张表的建表语句与配套索引。"""
    for table in _TABLES:
        assert any(f"CREATE TABLE IF NOT EXISTS {table}" in stmt for stmt in gm.DDL), (
            f"missing CREATE TABLE for {table}"
        )
    create_stmts = sum(1 for s in gm.DDL if "CREATE TABLE" in s)
    assert create_stmts == 5, f"DDL 应恰有 5 条建表语句，实际 {create_stmts}"
    index_stmts = [s for s in gm.DDL if "CREATE INDEX" in s]
    assert len(index_stmts) >= 18, "索引数量异常（每表至少 3-4 条）"
    # 每张表至少有索引（不能漏建）
    for table in _TABLES:
        assert any(table in s for s in index_stmts), f"{table} 缺索引"


def test_gen_run_fields_and_meta():
    """§2.0：gen_run 字段名/默认值/表名。"""
    m = gm.TestGenRun._meta
    assert m.db_table == "mc_test_gen_run"
    assert m.app == "testing_platform"
    fields_map = m.fields_map
    for name in (
        "id", "project_id", "version_id", "run_type", "parent_run_id", "scope",
        "status", "progress", "budget", "decisions", "config",
        "created_by", "created_at", "updated_at",
    ):
        assert name in fields_map, f"gen_run 缺字段 {name}"
    assert fields_map["status"].default == "pending"
    assert _resolved_default(fields_map["scope"]) == {}
    assert _resolved_default(fields_map["progress"]) == {}
    assert _resolved_default(fields_map["budget"]) == {}
    assert _resolved_default(fields_map["decisions"]) == []
    assert _resolved_default(fields_map["config"]) == {}
    assert fields_map["version_id"].null
    assert fields_map["parent_run_id"].null
    assert fields_map["created_by"].null
    assert not fields_map["project_id"].null
    assert not fields_map["run_type"].null
    # unique_together 未声明（身份=id，发号由 service 管）
    assert not m.unique_together


def test_testpoint_fields_and_meta():
    """§2.2：testpoint 字段名/默认值/unique_together(project_id, code)。"""
    m = gm.TestTestpoint._meta
    assert m.db_table == "mc_test_testpoint"
    fields_map = m.fields_map
    for name in (
        "id", "code", "project_id", "requirement_id", "title", "trigger",
        "sample_data", "verify_result", "impact", "evidence", "coverage_axis",
        "surfaces", "surfaces_decided_by", "design_method", "batch_tier",
        "basis", "status", "case_review_state", "created_at", "updated_at",
    ):
        assert name in fields_map, f"testpoint 缺字段 {name}"
    # v4 修正：门①确认后才建行，初始即 confirmed（不存在 candidate）
    assert fields_map["status"].default == "confirmed"
    assert gm.TESTPOINT_STATUSES == ("confirmed", "generated")
    assert fields_map["surfaces_decided_by"].default == "ai"
    assert fields_map["basis"].default == "assumption"
    assert fields_map["case_review_state"].default == "none"
    assert _resolved_default(fields_map["coverage_axis"]) == []
    assert _resolved_default(fields_map["surfaces"]) == []
    assert fields_map["requirement_id"].null
    assert fields_map["design_method"].null
    assert fields_map["batch_tier"].null
    assert (("project_id", "code"),) == m.unique_together


def test_proposal_fields_and_meta():
    """§2.3：proposal 字段名/默认值；slot_key 无唯一约束（允许多版本行）。"""
    m = gm.TestProposal._meta
    assert m.db_table == "mc_test_proposal"
    fields_map = m.fields_map
    for name in (
        "id", "run_id", "project_id", "kind", "payload", "source_agent",
        "surface", "testpoint_id", "evidence_class", "review_state",
        "review_round", "rewrite_of", "version", "score", "score_breakdown",
        "reviews", "dup_of_case_id", "dup_score", "dup_verdict",
        "applied_rules", "question_id", "slot_key", "status", "provenance",
        "created_at", "updated_at",
    ):
        assert name in fields_map, f"proposal 缺字段 {name}"
    assert fields_map["review_state"].default == "machine_pending"
    assert fields_map["review_round"].default == 0
    assert fields_map["version"].default == 1
    assert fields_map["status"].default == "proposed"
    assert _resolved_default(fields_map["payload"]) == {}
    assert _resolved_default(fields_map["applied_rules"]) == []
    assert _resolved_default(fields_map["score_breakdown"]) == {}
    assert _resolved_default(fields_map["reviews"]) == {}
    assert _resolved_default(fields_map["provenance"]) == {}
    # slot_key 允许带版本多行 → 不能出现在 unique_together
    assert not m.unique_together
    assert fields_map["run_id"].null is False  # AI 唯一写入口必挂 run
    assert fields_map["project_id"].null is False


def test_coverage_cell_fields_and_meta():
    """§2.5：coverage_cell 字段 + unique_together(requirement_id, axis, surface)。"""
    m = gm.TestCoverageCell._meta
    assert m.db_table == "mc_test_coverage_cell"
    fields_map = m.fields_map
    for name in (
        "id", "project_id", "requirement_id", "axis", "surface", "state",
        "evidence", "testpoint_id", "created_at", "updated_at",
    ):
        assert name in fields_map, f"coverage_cell 缺字段 {name}"
    assert fields_map["state"].default == "gap"
    assert (("requirement_id", "axis", "surface"),) == m.unique_together
    assert not fields_map["requirement_id"].null
    assert not fields_map["axis"].null
    assert not fields_map["surface"].null
    assert not fields_map["project_id"].null
    assert fields_map["evidence"].null  # na 必填由调用方强制，列可空
    assert fields_map["testpoint_id"].null


def test_rule_fields_and_meta():
    """§2.6：rule 字段 + enforcement 默认 shadow + hits/violations 计数。"""
    m = gm.TestRule._meta
    assert m.db_table == "mc_test_rule"
    fields_map = m.fields_map
    for name in (
        "id", "project_id", "content", "kind", "scope", "target_type",
        "target_id", "enforcement", "hits", "violations",
        "created_at", "updated_at",
    ):
        assert name in fields_map, f"rule 缺字段 {name}"
    assert fields_map["enforcement"].default == "shadow"  # 新规则默认 shadow（§7）
    assert fields_map["hits"].default == 0
    assert fields_map["violations"].default == 0
    assert fields_map["target_type"].null
    assert fields_map["target_id"].null
    assert not m.unique_together


# ── 2) 枚举常量与 25 号文一致 ────────────────────────────────


def test_enums_match_design_doc():
    """枚举字面量与 25 号文 v4.1 原文逐一对齐（§2.0/§2.2/§2.3/§2.5/§2.6/§3.1）。"""
    # §2.0 run_type 六枚举
    assert gm.GEN_RUN_TYPES == (
        "full", "plan", "exec", "pre_submit_edit", "incremental", "reopen",
    )
    # §3.1 状态机取值域：主链 10 + 旁路 3（整合后与 state.GEN_RUN_STATUSES 对齐）
    assert gm.GEN_RUN_STATUSES == (
        "pending", "analyzing", "req_reviewing", "points_proposed",
        "generating", "evaluating", "cases_proposed", "cases_confirmed",
        "importing", "done",
        "paused", "failed", "cancelled",
    )
    # 跨模块 parity（2026-10-18 整合约定：DB 取值域 == 转换表的态集）
    from testing_platform.generation import state as gs
    assert gm.GEN_RUN_STATUSES == gs.GEN_RUN_STATUSES
    # §6.2 三个门态（通知协议）
    assert gm.GEN_RUN_GATE_STATUSES == ("req_reviewing", "points_proposed", "cases_proposed")
    # §2.0 budget.state 三取值
    assert gm.GEN_RUN_BUDGET_STATES == ("normal", "pressure", "blocking")
    # §2.2
    assert gm.TESTPOINT_SURFACES == ("api", "web", "app", "security")
    assert gm.TESTPOINT_SURFACES_DECIDED_BY == ("ai", "user")
    assert gm.TESTPOINT_BATCH_TIERS == ("A", "B", "C")
    assert gm.TESTPOINT_BASES == ("fact", "assumption", "to_confirm")
    assert gm.TESTPOINT_CASE_REVIEW_STATES == (
        "machine_pending", "in_duel", "split", "scored", "need_rework", "none",
    )
    # §2.3
    assert gm.PROPOSAL_KINDS == ("testpoint", "case", "dataset")
    assert gm.PROPOSAL_EVIDENCE_CLASSES == ("executed", "static", "inferred", "claimed")
    assert gm.PROPOSAL_REVIEW_STATES == (
        "machine_pending", "machine_rejected", "in_duel", "scored", "need_rework",
    )
    assert gm.PROPOSAL_STATUSES == ("proposed", "keep", "modify", "reject", "defer")
    assert gm.PROPOSAL_RULE_DISPOSITIONS == ("followed", "not_applicable", "violated")
    assert gm.PROPOSAL_CONSENSUS == ("agree", "split", "inconclusive")
    # §2.5 / §2.6
    assert gm.COVERAGE_CELL_STATES == ("lit", "gap", "na")
    assert gm.RULE_KINDS == ("hard", "soft")
    assert gm.RULE_SCOPES == ("global", "module", "requirement", "surface")
    assert gm.RULE_ENFORCEMENTS == ("active", "shadow", "disabled")
    assert gm.RULE_DEFAULT_ENFORCEMENT == "shadow"
    # §2.4 扩列
    assert gm.QUESTION_GATES == ("req_review", "point_confirm")
    assert "risk_accepted" in gm.QUESTION_RESOLUTIONS_GENERATION


def test_design_method_has_chinese_labels():
    """design_method 英文 slug 与中文对照一一对应（§2.2 原文七法）。"""
    assert len(gm.TESTPOINT_DESIGN_METHODS) == len(gm.TESTPOINT_DESIGN_METHOD_LABELS)
    assert set(gm.TESTPOINT_DESIGN_METHOD_LABELS) == set(gm.TESTPOINT_DESIGN_METHODS)
    assert "等价类" in set(gm.TESTPOINT_DESIGN_METHOD_LABELS.values())


def test_rule_enums_match_rules_module():
    """models 的 RULE_* 常量与 generation.rules 纯函数层同源（两处不得漂移）。"""
    from testing_platform.generation.rules import (
        DEFAULT_ENFORCEMENT,
        ENFORCEMENTS,
        RULE_KINDS as RULES_RULE_KINDS,
        SCOPES,
    )

    assert gm.RULE_KINDS == RULES_RULE_KINDS
    assert gm.RULE_SCOPES == SCOPES
    assert gm.RULE_ENFORCEMENTS == ENFORCEMENTS
    assert gm.RULE_DEFAULT_ENFORCEMENT == DEFAULT_ENFORCEMENT


# ── 3) DDL 语法 sanity + Tortoise 建表路径 ───────────────────


@pytest.mark.asyncio
async def test_models_create_real_rows(tortoise_db):
    """Tortoise generate_schemas 走模型建表（sqlite 真落库路径）：五表各插一行读回。"""
    project_id = uuid.uuid4()
    run = await gm.TestGenRun.create(
        project_id=project_id,
        run_type="full",
        scope={"requirement_ids": [str(uuid.uuid4())]},
        budget={"tokens_used": 0, "tokens_limit": 100000, "state": "normal"},
        decisions=[{"action": "dispatch", "evidence": "fact"}],
        config={"model": "cmd_center"},
    )
    assert run.status == "pending"
    assert run.progress == {}

    tp = await gm.TestTestpoint.create(
        code="TP-0001",
        project_id=project_id,
        title="正常登录",
        coverage_axis=["C01"],
        surfaces=["api", "web"],
        basis="fact",
    )
    assert tp.status == "confirmed"
    assert tp.surfaces_decided_by == "ai"
    assert tp.case_review_state == "none"

    proposal = await gm.TestProposal.create(
        run_id=run.id,
        project_id=project_id,
        kind="case",
        payload={"title": "边界值：密码长度 5/6/20/21"},
        testpoint_id=tp.id,
        slot_key="flow:REQ-1:TP-0001:1",
    )
    assert proposal.review_state == "machine_pending"
    assert proposal.review_round == 0

    cell = await gm.TestCoverageCell.create(
        project_id=project_id,
        requirement_id=uuid.uuid4(),
        axis="C01",
        surface="api",
        state="lit",
        testpoint_id=tp.id,
    )
    assert cell.state == "lit"

    rule = await gm.TestRule.create(
        project_id=project_id,
        content="每功能点 1 正向 + ≥3 异常/边界",
        kind="hard",
        scope="global",
    )
    assert rule.enforcement == "shadow"
    assert rule.hits == 0

    # 读回 JSONB 往返
    got = await gm.TestGenRun.get(id=run.id)
    assert got.scope == {"requirement_ids": run.scope["requirement_ids"]}
    assert got.budget["state"] == "normal"
    assert got.decisions[0]["action"] == "dispatch"

    # unique_together：同 (project_id, code) 再插测试点必须炸
    from tortoise.exceptions import IntegrityError

    with pytest.raises(IntegrityError):
        await gm.TestTestpoint.create(code="TP-0001", project_id=project_id, title="重复")


@pytest.mark.asyncio
async def test_coverage_cell_unique_triple(tortoise_db):
    """coverage_cell 唯一键三元组拦截同格重复行。"""
    from tortoise.exceptions import IntegrityError

    project_id = uuid.uuid4()
    req = uuid.uuid4()
    await gm.TestCoverageCell.create(
        project_id=project_id, requirement_id=req, axis="C03", surface="web", state="gap"
    )
    with pytest.raises(IntegrityError):
        await gm.TestCoverageCell.create(
            project_id=project_id, requirement_id=req, axis="C03", surface="web", state="na"
        )


def _pg_ddl_for_sqlite(stmt: str) -> str:
    """把 PG 方言 DDL 翻成 sqlite 能解析的等价语句（仅用于语法 sanity 跑一遍）。

    只动 PG 专属 token，列集合/约束/索引结构原样保留——这样 sqlite 跑通即证明
    DDL 语法与结构自洽；真正的 JSONB/TIMESTAMPTZ 落库由整合步骤在 PG 上做。
    """
    stmt = re.sub(r"::jsonb", "", stmt)                 # '{}'/ '[]' 字面量直接当默认值
    stmt = re.sub(r"\bnow\(\)", "CURRENT_TIMESTAMP", stmt)
    stmt = re.sub(r"\bJSONB\b", "TEXT", stmt)           # sqlite 无 JSONB 亲和
    stmt = re.sub(r"\bTIMESTAMPTZ\b", "TIMESTAMP", stmt)
    stmt = re.sub(r"\bUUID\b", "TEXT", stmt)            # sqlite 无 UUID 类型
    return stmt


def test_ddl_executes_on_sqlite():
    """DDL 语法 sanity：用全新 sqlite 内存库把整份 DDL（建表 + 索引）跑一遍，再跑一遍幂等。

    独立 sqlite 库（不经 Tortoise ``generate_schemas``）——表只由本 DDL 建出，
    因此 DB 层默认值（``status='pending'`` 等）由 DDL 自己生效，验证「列集合 +
    约束 + 索引 + 默认值」四件套。``_pg_ddl_for_sqlite`` 只翻 PG 专属 token，
    列结构原样；真正 PG 落库由整合步骤在真 PG 上做。
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        sqlite_ddl = [_pg_ddl_for_sqlite(s) for s in gm.DDL]
        for stmt in sqlite_ddl:
            conn.executescript(stmt)
        # 幂等：IF NOT EXISTS，第二遍不炸
        for stmt in sqlite_ddl:
            conn.executescript(stmt)

        for table in _TABLES:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert rows, f"{table} 必须真实建出"
            actual = {r[1] for r in rows}
            assert actual == _ddl_columns(_ddl_create(table)), f"{table} 建表列与 DDL 不符"

        # 索引真实存在（每表都有）
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_test_%'"
        ).fetchall()
        assert len(idx) >= 18, f"索引未建齐：{len(idx)}"

        # 插一行只给必填列（run_type），其余靠 DB 默认值——验证 DDL 的 DEFAULT 生效
        conn.execute(
            "INSERT INTO mc_test_gen_run (id, project_id, run_type) "
            "VALUES (?, ?, 'plan')",
            (str(uuid.uuid4()), str(uuid.uuid4())),
        )
        status = conn.execute("SELECT status FROM mc_test_gen_run").fetchone()[0]
        assert status == "pending"
    finally:
        conn.close()


# ── 4) ensure_question_columns ───────────────────────────────


async def _create_question_table(conn) -> None:
    """造一张现库形态的 mc_test_question（quality.models DDL 的 sqlite 可执行子集）。"""
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS mc_test_question (
            id                VARCHAR(36) PRIMARY KEY,
            project_id        VARCHAR(36) NOT NULL,
            origin            VARCHAR(32) NOT NULL,
            raised_by         VARCHAR(32),
            content           TEXT        NOT NULL,
            context           TEXT        NOT NULL DEFAULT '{}',
            status            VARCHAR(16) NOT NULL DEFAULT 'open',
            resolution        VARCHAR(32),
            resolution_detail TEXT        NOT NULL DEFAULT '{}',
            resolved_at       TIMESTAMP,
            blocking_refs     TEXT        NOT NULL DEFAULT '[]',
            created_at        TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at        TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


@pytest.mark.asyncio
async def test_ensure_question_columns_adds_and_idempotent(tortoise_db):
    """§2.4 扩列：六列补齐 + 幂等（跑两次不报错、第二次 added=[]、旧数据保留）。"""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await _create_question_table(conn)

    # 插一条存量行（老疑问，无新列值）——验证扩列后旧数据保留
    old_id = str(uuid.uuid4())
    await conn.execute_script(
        f"INSERT INTO mc_test_question (id, project_id, origin, content) "
        f"VALUES ('{old_id}', '{uuid.uuid4()}', 'design_doubt', '登录态怎么算')"
    )

    first = await gm.ensure_question_columns(conn)
    assert set(first) == {
        "requirement_id", "testpoint_id", "recommended_answer",
        "answer", "round_no", "gate",
    }, f"首次应补齐六列，实际 {first}"

    # 幂等：第二/三次不炸、不再补
    assert await gm.ensure_question_columns(conn) == []
    assert await gm.ensure_question_columns(conn) == []

    cols = {r["name"] for r in await conn.execute_query_dict("PRAGMA table_info(mc_test_question)")}
    for name in (
        "requirement_id", "testpoint_id", "recommended_answer",
        "answer", "round_no", "gate",
    ):
        assert name in cols, f"扩列后缺 {name}"
    # 原有列未动
    assert {"id", "project_id", "origin", "content", "status", "resolution", "blocking_refs"} <= cols

    # 新列可写可读
    new_id = str(uuid.uuid4())
    await conn.execute_script(
        f"INSERT INTO mc_test_question (id, project_id, origin, content, "
        f"requirement_id, testpoint_id, recommended_answer, answer, round_no, gate) "
        f"VALUES ('{new_id}', '{uuid.uuid4()}', 'product_uncertainty', 'Q', "
        f"'{uuid.uuid4()}', '{uuid.uuid4()}', '推荐', '人答', 3, 'req_review')"
    )
    row = (
        await conn.execute_query_dict(
            f"SELECT round_no, gate, recommended_answer, answer FROM mc_test_question "
            f"WHERE id = '{new_id}'"
        )
    )[0]
    assert row["round_no"] == 3
    assert row["gate"] == "req_review"
    assert row["recommended_answer"] == "推荐"
    assert row["answer"] == "人答"

    # 旧行新列为 NULL（旧数据保留、新列可空）
    old = (
        await conn.execute_query_dict(
            f"SELECT gate, round_no FROM mc_test_question WHERE id = '{old_id}'"
        )
    )[0]
    assert old["gate"] is None and old["round_no"] is None


@pytest.mark.asyncio
async def test_ensure_question_columns_missing_table_raises(tortoise_db):
    """表不存在时响亮失败（不静默 no-op——静默会把启动顺序问题拖成运行时缺列）。"""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    # sqlite 下 PRAGMA 对不存在的表返回空 → 判列不存在 → ALTER 报错
    with pytest.raises(Exception):
        await gm.ensure_question_columns(conn)


def test_question_new_columns_constant_shape():
    """扩列清单常量：六列、类型口径与文档一致。"""
    assert len(gm._QUESTION_NEW_COLUMNS) == 6
    types = dict(gm._QUESTION_NEW_COLUMNS)
    assert types["requirement_id"] == "UUID"
    assert types["testpoint_id"] == "UUID"
    assert types["recommended_answer"] == "TEXT"
    assert types["answer"] == "TEXT"
    assert types["round_no"] == "INTEGER"
    assert types["gate"] == "VARCHAR(16)"


def test_no_cross_package_imports():
    """硬边界：本模块不 import assets/quality——防与并发会话耦合（模块 docstring 声明）。"""
    import testing_platform.generation.models as mod

    banned = ("testing_platform.assets", "testing_platform.quality")
    source = open(mod.__file__, encoding="utf-8").read()
    imports = re.findall(r"^\s*(?:from|import)\s+(\S+)", source, re.M)
    for stmt in imports:
        for b in banned:
            assert not stmt.startswith(b), f"generation.models 不得 import {b}（发现 {stmt}）"
