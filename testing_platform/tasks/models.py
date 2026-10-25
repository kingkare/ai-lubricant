"""Tortoise models for the testing-platform **task + run domain**.

Implements the tables defined in ``docs/ai-testing-platform/plans/02-task-model.md``
§2 (``mc_test_task``, cross-checked against ``design.md`` §15.10) plus the
execution tables of ``design.md`` §15.7 / ``plans/04-execution-and-results.md``
§1.1–§1.4 that a task run needs:

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_task``           任务——带侧重的一次测试启动（02 §2）
``mc_test_run``            一次运行（04 §1.1 + design §15.7）
``mc_test_run_scenario``   场景级结果——执行单位是场景（04 §1.2）
``mc_test_step_result``    步骤级结果（04 §1.3）
``mc_test_evidence``       证据（04 §1.4 + design §15.7）
======  ==========================================================

Run-table conventions follow the engine's ``PgResultWriter`` bootstrap DDL
(``testing_platform/engine/executor.py``) so that both writers can coexist on
the same schema:

* ``run_scenario.scenario_id`` / ``step_result.seq`` stay **TEXT** — the engine
  writes ``str(scenario_id_or_code)`` / ``str(seq)`` verbatim (design §15.7's
  ``step_seq`` ≙ ``seq`` here);
* ``run_scenario.run_id`` and friends are plain UUID columns (logical FKs) —
  the engine inserts raw UUIDs without constraint, mirroring its DDL;
* ``healing`` follows 04 §1.2 + the engine column name (design §15.7's
  ``healing_applied`` is the same thing).

Conventions follow ``testing_platform/assets/models.py``: UUID primary keys,
``mc_`` prefix, JSONB payloads via ``JSONField``, raw ``DDL`` +
``create_tables_via_pg()`` for PostgresClient-only deployments.

Notification config (``mc_test_task.notify``) only *stores* the declaration —
钉钉/飞书/企微/webhook 四通道走 ``user_platform/notify_dispatch.py``、Expo 推送走
``notify_push.py``（design §14.3 已核对，平台无 email）。发送由集成层执行，本包不发。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# Order matters: parents before children (task → run → run_scenario →
# step_result/evidence). run_scenario/step_result mirror the engine's
# bootstrap DDL column-for-column so ``CREATE TABLE IF NOT EXISTS`` never
# produces a schema the other writer cannot insert into.
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_task (
        id             UUID PRIMARY KEY,
        code           VARCHAR(32)  NOT NULL,
        project_id     UUID         NOT NULL,
        title          VARCHAR(512) NOT NULL,
        purpose        TEXT,
        scenario_tags  JSONB        NOT NULL DEFAULT '{}'::jsonb,
        case_filter    JSONB        NOT NULL DEFAULT '{}'::jsonb,
        exec_type      VARCHAR(32)  NOT NULL DEFAULT 'manual',
        schedule       JSONB,
        env_id         UUID,
        global_params  JSONB        NOT NULL DEFAULT '{}'::jsonb,
        notify         JSONB        NOT NULL DEFAULT '{}'::jsonb,
        exec_mode      VARCHAR(32)  NOT NULL DEFAULT 'script_only',
        status         VARCHAR(32)  NOT NULL DEFAULT 'draft',
        created_by     UUID         NOT NULL,
        version        INTEGER      NOT NULL DEFAULT 1,
        created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_run (
        id            UUID PRIMARY KEY,
        code          VARCHAR(32) NOT NULL,
        project_id    UUID        NOT NULL,
        task_id       UUID,
        trigger       VARCHAR(32) NOT NULL DEFAULT 'manual',
        env_id        UUID,
        global_params JSONB       NOT NULL DEFAULT '{}'::jsonb,
        scenario_ids  JSONB       NOT NULL DEFAULT '[]'::jsonb,
        status        VARCHAR(16) NOT NULL DEFAULT 'pending',
        summary       JSONB,
        started_at    TIMESTAMPTZ,
        finished_at   TIMESTAMPTZ,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    # ↓↓ 与 engine/executor.py::_BOOTSTRAP_DDL 逐列对齐（其为准，勿收窄）↓↓
    """
    CREATE TABLE IF NOT EXISTS mc_test_run_scenario (
        id            UUID PRIMARY KEY,
        run_id        UUID,
        scenario_id   TEXT NOT NULL,
        case_refs     JSONB,
        exec_mode     VARCHAR(32),
        status        VARCHAR(16) NOT NULL,
        failure_kind  VARCHAR(32),
        duration_ms   INTEGER,
        healing       JSONB,
        dataset_row   JSONB,
        started_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_step_result (
        id              UUID PRIMARY KEY,
        run_scenario_id UUID REFERENCES mc_test_run_scenario(id),
        step_key        TEXT NOT NULL,
        seq             TEXT,
        type            VARCHAR(32),
        status          VARCHAR(16),
        attempt         INTEGER,
        expected        JSONB,
        actual          JSONB,
        diff            JSONB,
        duration_ms     INTEGER,
        checks          JSONB,
        evidence        JSONB,
        started_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_evidence (
        id              UUID PRIMARY KEY,
        run_scenario_id UUID        NOT NULL REFERENCES mc_test_run_scenario(id),
        step_result_id  UUID,
        step_seq        INTEGER,
        kind            VARCHAR(32) NOT NULL,
        uri             TEXT        NOT NULL,
        meta            JSONB       NOT NULL DEFAULT '{}'::jsonb,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_task_project   ON mc_test_task(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_task_status    ON mc_test_task(project_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_test_run_project    ON mc_test_run(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_run_task       ON mc_test_run(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_rs_run         ON mc_test_run_scenario(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_sr_run_scenario ON mc_test_step_result(run_scenario_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_ev_run_scenario ON mc_test_evidence(run_scenario_id)",
]

#: ``mc_test_task.exec_type`` 执行类型（02 §4；design §15.10）
TASK_EXEC_TYPES: tuple[str, ...] = ("manual", "scheduled", "event", "api")

#: ``mc_test_task.exec_mode`` 三模（02 §2；可覆盖用例/场景默认）
TASK_EXEC_MODES: tuple[str, ...] = ("script_only", "hybrid", "ai_only")

#: ``mc_test_task.status`` 状态机（02 §2）
TASK_STATUSES: tuple[str, ...] = ("draft", "enabled", "disabled", "archived")

#: ``mc_test_task.schedule.kind``（02 §4：cron / 间隔 / 一次性）
SCHEDULE_KINDS: tuple[str, ...] = ("cron", "interval", "once")

#: ``mc_test_task.notify.on`` 触发时机（02 §5）
NOTIFY_ON: tuple[str, ...] = ("always", "fail", "error", "pass")

#: ``notify.channels[].type`` —— 复用平台既有通道（design §14.3 已核对：
#: notify_dispatch 四通道 + notify_push 的 Expo 推送；**平台无 email**，勿加）
NOTIFY_CHANNELS: tuple[str, ...] = ("feishu", "dingtalk", "wecom", "webhook", "mobile_push")

#: ``mc_test_run.trigger``（04 §1.1 manual/scheduled/event/api；
#: design §15.7 另列 webhook/regression，一并收留）
RUN_TRIGGERS: tuple[str, ...] = ("manual", "scheduled", "event", "api", "webhook", "regression")

#: ``mc_test_run.status`` 状态机（design 十六）
RUN_STATUSES: tuple[str, ...] = ("pending", "running", "done", "aborted")

#: ``mc_test_run_scenario.status``（04 §1.2；design 十六「用例结果」）
SCENARIO_RESULT_STATUSES: tuple[str, ...] = ("pass", "fail", "error", "flaky", "skipped")

#: ``mc_test_run_scenario.failure_kind`` 失败分流（04 §4）
FAILURE_KINDS: tuple[str, ...] = ("script_issue", "product_bug", "case_outdated", "env_flaky")

#: ``mc_test_evidence.kind``（design §15.7）
EVIDENCE_KINDS: tuple[str, ...] = (
    "screenshot",
    "log",
    "network",
    "trace",
    "db_state",
    "assertion_diff",
)


class TestTask(Model):
    """任务（02 §2 / design §15.10）——**带侧重的一次测试启动**。

    不直接选用例：按 ``scenario_tags``（all/any/none）+ ``case_filter``
    选例（见 :func:`testing_platform.tasks.service.select_by_tags`）。
    ``notify`` 只存声明（通道/时机/内容），发送由集成层复用平台既有
    notify_dispatch / notify_push。

    注意 ``mc_test_task`` **≠ 平台 ``mc_tasks``**（那是开发 agent 会话）。
    """

    __test__ = False  # 类名以 Test 开头：告知 pytest 这不是测试用例

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)  # TASK-xxxx（按 project 自增）
    project_id = fields.UUIDField()
    title = fields.CharField(max_length=512)  # 任务标题
    purpose = fields.TextField(null=True)  # 任务用途描述（这次跑为了验证什么）
    scenario_tags = fields.JSONField(default=dict)  # {all: [], any: [], none: []}
    case_filter = fields.JSONField(default=dict)  # {module: [], priority: [], automation_status: []}
    exec_type = fields.CharField(max_length=32, default="manual")
    schedule = fields.JSONField(null=True)  # 定时配置（exec_type=scheduled 时必给）
    env_id = fields.UUIDField(null=True)  # → mc_test_environment（05 计划，先留占位）
    global_params = fields.JSONField(default=dict)  # 全局参数（所有场景共享）
    notify = fields.JSONField(default=dict)  # 通知声明（不发，见模块 docstring）
    exec_mode = fields.CharField(max_length=32, default="script_only")
    status = fields.CharField(max_length=32, default="draft")
    created_by = fields.UUIDField()
    version = fields.IntField(default=1)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_task"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("project_id", "status"))


class TestRun(Model):
    """一次运行（04 §1.1 + design §15.7）。

    由 :func:`testing_platform.tasks.service.run_task` 创建（status=pending，
    只建行不执行）；``scenario_ids`` 记本次标签选例结果，``global_params``
    是**本次快照**（02 §8）——后续任务配置变更不影响已建 run。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)  # RUN-xxxx（按 project 自增）
    project_id = fields.UUIDField()
    task_id = fields.UUIDField(null=True)  # → mc_test_task（手动单跑可空）
    trigger = fields.CharField(max_length=32, default="manual")
    env_id = fields.UUIDField(null=True)
    global_params = fields.JSONField(default=dict)
    scenario_ids = fields.JSONField(default=list)  # 本次选出的场景（去重后）
    status = fields.CharField(max_length=16, default="pending")
    summary = fields.JSONField(null=True)  # {total, pass, fail, error, flaky, skipped, duration_ms}
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_run"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("task_id",), ("status",))


class TestRunScenario(Model):
    """场景级结果（04 §1.2）——**执行单位是场景，非用例**。

    同一场景被多个用例引用只跑一次；``case_refs`` 记录本次引用它的用例，
    用例级结果由场景结果**投影**得出。列形状与引擎 ``PgResultWriter``
    bootstrap DDL 对齐（``scenario_id`` 为 TEXT：UUID 或 code 均可）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    run_id = fields.UUIDField()  # → mc_test_run（逻辑 FK，引擎裸写）
    scenario_id = fields.CharField(max_length=64)  # 场景 UUID 或 code（引擎 TEXT 列）
    case_refs = fields.JSONField(null=True)  # 本次引用该场景的用例 id 列表
    exec_mode = fields.CharField(max_length=32, null=True)  # 实际模式
    status = fields.CharField(max_length=16)  # pass|fail|error|flaky|skipped
    failure_kind = fields.CharField(max_length=32, null=True)  # 失败分流（04 §4）
    duration_ms = fields.IntField(null=True)
    healing = fields.JSONField(null=True)  # hybrid 自愈记录
    dataset_row = fields.JSONField(null=True)  # 本次数据行（{{data.*}}）
    started_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_run_scenario"
        indexes = (("run_id",), ("scenario_id",))


class TestStepResult(Model):
    """步骤级结果（04 §1.3）。

    列形状与引擎 bootstrap DDL 对齐：``seq`` 为 TEXT（引擎写 ``str(seq)``，
    design §15.7 的 ``step_seq`` ≙ 此列）。``expected/actual/diff`` 支撑报告
    的断言 diff 展示；``duration_ms`` 是响应耗时来源（03 §2）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    run_scenario_id = fields.UUIDField()  # → mc_test_run_scenario
    step_key = fields.CharField(max_length=128)
    seq = fields.CharField(max_length=32, null=True)
    type = fields.CharField(max_length=32, null=True)
    status = fields.CharField(max_length=16, null=True)  # pass|fail|error|skipped
    attempt = fields.IntField(null=True)
    expected = fields.JSONField(null=True)
    actual = fields.JSONField(null=True)
    diff = fields.JSONField(null=True)
    duration_ms = fields.IntField(null=True)
    checks = fields.JSONField(null=True)
    evidence = fields.JSONField(null=True)  # 引擎把证据内联在此（04 §1.4 表为正式落点）
    started_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_step_result"
        indexes = (("run_scenario_id",),)


class TestEvidence(Model):
    """证据（04 §1.4 + design §15.7）。

    正式落点是本表（引擎当前把证据内联在 step_result.evidence JSONB，
    报告生成两者都读）。大文件走附件存储（``uri``）；敏感变量写库前
    由调用方脱敏（04 §1.4 注）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    run_scenario_id = fields.UUIDField()  # → mc_test_run_scenario
    step_result_id = fields.UUIDField(null=True)  # → mc_test_step_result（04 §1.4）
    step_seq = fields.IntField(null=True)  # design §15.7 的步骤定位
    kind = fields.CharField(max_length=32)  # screenshot|log|network|trace|db_state|assertion_diff
    uri = fields.CharField(max_length=1024)
    meta = fields.JSONField(default=dict)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_evidence"
        indexes = (("run_scenario_id",), ("step_result_id",))


async def create_tables_via_pg() -> None:
    """Create ``mc_test_*`` tables through the platform raw PG client.

    Used by deployments where Tortoise is not initialized (main service path).
    Matches the platform convention ``from db import PostgresClient``.
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
