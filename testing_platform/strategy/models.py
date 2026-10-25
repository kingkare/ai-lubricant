"""Tortoise models for the testing-platform **strategy domain**（测试策略）.

用户定版（2026-10-14）：测试策略是**一轮测试的执行单元**，四类
``strategy_type``——冒烟测试 / 全量测试 / 回归测试 / 性能测试。它挂在一个
**测试版本**（``mc_test_project_version``）下，声明本轮**测哪些用例**（勾选）、
**谁测**（分配人员，人工或自动执行皆可）、**什么时候测**（测试时间），以及
**测成什么样**（用例级结果 + 汇总）。

> 与 20 号文 §3.2 的关系：那份设计是 ISTQB「版本级 test plan」（进入/退出准则、
> 多轮 rounds 编排、风险登记册）。本版**只做用户点名的执行分配层**——一轮一策略、
> 类型四选一、用例勾选 + 人员分配 + 结果录入。``entry_criteria`` / ``rounds`` /
> ``change_scope`` 等编排字段**刻意不建**（表加列易，语义镀金难）。日后要轮次编排
> 再扩本表，不另立实体。

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_strategy``        测试策略（一轮测试：类型 + 版本 + 时间 + 状态）
``mc_test_strategy_case``   策略↔用例 勾选 + 分配人员 + 执行方式 + 测试结果
======  ==========================================================

**缺陷的第二维度**：``mc_test_bug.strategy_id``（在 :mod:`testing_platform.defects.models`）
——bug 归属 = 测试版本（``version_id``）× 测试策略（``strategy_id``）两个维度。

Conventions follow the sibling ``testing_platform/tasks/models.py``: UUID 主键、
``mc_test_`` 表前缀、JSONB 走 ``JSONField``、**无跨包 FK**（service 层保证引用一致）、
``DDL`` + :func:`create_tables_via_pg` 供 ``PostgresClient``-only 部署建表。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
#: ``mc_test_strategy.strategy_type`` 四类（用户点名：冒烟/全量/回归/性能）
STRATEGY_TYPES: tuple[str, ...] = ("smoke", "full", "regression", "performance")

#: ``mc_test_strategy.status`` 状态机：草稿 → 待执行 → 执行中 → 已完成（可归档）
STRATEGY_STATUSES: tuple[str, ...] = ("draft", "pending", "executing", "done", "archived")

#: ``mc_test_strategy_case.exec_mode`` 执行方式——人工执行还是自动化执行
#: （用户：「不管是人工测试还是手工测试，需要分配给人员」——两种都要能分配、都要有结果）
STRATEGY_EXEC_MODES: tuple[str, ...] = ("manual", "automated", "hybrid")

#: ``mc_test_strategy_case.result`` 用例级结果。
#: ``pass``/``fail``/``skipped`` 与执行域 ``SCENARIO_RESULT_STATUSES`` 同词；
#: ``pending`` = 尚未执行，``blocked`` = 被前置条件/环境卡住（人工测试常备态）。
STRATEGY_CASE_RESULTS: tuple[str, ...] = ("pending", "pass", "fail", "blocked", "skipped")

#: ``mc_test_strategy_case.verified_by`` 的对照——结果由谁录入（人/引擎）留 note，不建枚举。

# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# order: strategy 先，strategy_case 后（逻辑父子，无硬 FK）。
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_strategy (
        id            UUID PRIMARY KEY,
        code          VARCHAR(32)  NOT NULL,
        project_id    UUID         NOT NULL,
        version_id    UUID,
        name          VARCHAR(256) NOT NULL,
        strategy_type VARCHAR(32)  NOT NULL DEFAULT 'smoke',
        description   TEXT,
        status        VARCHAR(32)  NOT NULL DEFAULT 'draft',
        start_time    TIMESTAMPTZ,
        end_time      TIMESTAMPTZ,
        actual_end_time TIMESTAMPTZ,
        delay_analysis  TEXT,
        owner_id      UUID,
        testers       JSONB        NOT NULL DEFAULT '[]'::jsonb,
        developers    JSONB        NOT NULL DEFAULT '[]'::jsonb,
        created_by    UUID,
        version       INTEGER      NOT NULL DEFAULT 1,
        created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_strategy_case (
        id          UUID PRIMARY KEY,
        strategy_id UUID         NOT NULL,
        case_id     UUID         NOT NULL,
        assignee_id UUID,
        exec_mode   VARCHAR(32)  NOT NULL DEFAULT 'manual',
        result      VARCHAR(32)  NOT NULL DEFAULT 'pending',
        note        TEXT,
        executed_at TIMESTAMPTZ,
        executed_by UUID,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (strategy_id, case_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_project ON mc_test_strategy(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_version ON mc_test_strategy(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_type    ON mc_test_strategy(strategy_type)",
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_status  ON mc_test_strategy(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_case_strategy ON mc_test_strategy_case(strategy_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_case_case     ON mc_test_strategy_case(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_strategy_case_assignee ON mc_test_strategy_case(assignee_id)",
]


class TestStrategy(Model):
    """测试策略（一轮测试）——**类型四选一 + 挂版本 + 测试时间 + 状态**。

    ``code`` 形如 ``STRAT-0001``，按 ``project_id`` 内 max+1 自增（service 层）。
    ``version_id`` → ``mc_test_project_version``（测试版本，缺陷的第一维度）；
    一轮一策略，故**不做** ``(project_id, version_id)`` 唯一——同一版本常要同时
    排一场冒烟和一场回归。``strategy_type`` 见 :data:`STRATEGY_TYPES`。
    ``start_time``/``end_time`` 是本轮计划测试时间（用户点名「测试策略有测试时间」）。
    ``owner_id`` 本轮负责人；``testers``/``developers`` 是本轮**测试人员/开发人员**
    （平台用户 id 列表，用户定版 2026-10-15；与用例级执行人分配分工——
    这是整轮的团队名单，用例级的 ``TestStrategyCase.assignee_id`` 是逐例执行人）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)  # STRAT-0001
    project_id = fields.UUIDField()  # → mc_test_project（测试项目）
    version_id = fields.UUIDField(null=True)  # → mc_test_project_version（测试版本）
    name = fields.CharField(max_length=256)
    strategy_type = fields.CharField(max_length=32, default="smoke")
    description = fields.TextField(null=True)
    status = fields.CharField(max_length=32, default="draft")
    start_time = fields.DatetimeField(null=True)  # 计划开始
    end_time = fields.DatetimeField(null=True)  # 计划结束
    #: 实际完成时间（用户定版 2026-10-16：报告页延期分析 = 计划 end_time vs 本字段；
    #: 未填且已过 end_time 则按当前时间算「已超期」）。
    actual_end_time = fields.DatetimeField(null=True)
    #: 延期原因分析（人工填：为什么延期、影响、对策——报告页展示）。
    delay_analysis = fields.TextField(null=True)
    owner_id = fields.UUIDField(null=True)  # 本轮负责人
    #: 本轮团队名单（平台用户 id 列表）：测试人员 / 开发人员（用户定版 2026-10-15）。
    testers = fields.JSONField(default=list)
    developers = fields.JSONField(default=list)
    created_by = fields.UUIDField(null=True)
    version = fields.IntField(default=1)  # 变更门版本（update_strategy 每改 +1）
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_strategy"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("version_id",), ("strategy_type",))


class TestStrategyCase(Model):
    """策略↔用例（勾选）+ 分配人员 + 执行方式 + 测试结果——**本轮执行的账本**。

    一个用例在同一策略里只一行（``unique_together``），所以
    ``result``/``assignee_id`` 直接落本行，不必再开「执行记录」表：
    **勾选 = 建行，改派 = 更新本行，录结果 = 更新本行**。
    重跑一轮不改历史（历史在 ``mc_test_run``），本表是**本轮当前态**。

    ``exec_mode`` 区分人工/自动化执行——两种都要分配人员（自动化那人是**责任人**，
    人工那人是**执行人**），``result`` 两种都要有：人工由 ``case.execute_manual``
    语义录入，自动化可由调用方从 run 结果回写。
    ``case_id`` → ``mc_test_case``（裸 UUID，无 FK，同 ``mc_test_case_scenario`` 惯例）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    strategy_id = fields.UUIDField()  # → mc_test_strategy.id
    case_id = fields.UUIDField()  # → mc_test_case.id
    assignee_id = fields.UUIDField(null=True)  # 分配给谁（平台用户）
    exec_mode = fields.CharField(max_length=32, default="manual")  # manual|automated|hybrid
    result = fields.CharField(max_length=32, default="pending")  # pending|pass|fail|blocked|skipped
    note = fields.TextField(null=True)  # 结果备注（人工执行留痕/失败说明）
    executed_at = fields.DatetimeField(null=True)  # 结果录入时刻
    executed_by = fields.UUIDField(null=True)  # 结果录入人
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_strategy_case"
        unique_together = (("strategy_id", "case_id"),)
        indexes = (("strategy_id",), ("case_id",), ("assignee_id",))


async def create_tables_via_pg() -> None:
    """Create ``mc_test_strategy*`` tables through the platform raw PG client."""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
