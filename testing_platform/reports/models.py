"""Tortoise model for the testing-platform **report domain**.

Implements ``mc_test_report`` as defined in
``docs/ai-testing-platform/plans/03-report-model.md`` §1 (cross-checked against
``design.md`` §15.11):

* 报告是**一等实体**（有实体、有页面、可归档）——不是对结果表的临时查询；
* ``snapshot`` 是**生成时定版**的 JSON 快照（03 §6 决策 1）——结果表可能被
  清理，快照保住当时真相，归档后仍可查看。

Conventions follow ``testing_platform/assets/models.py``: UUID primary key,
``mc_`` prefix, JSONB payload via ``JSONField``, raw ``DDL`` +
``create_tables_via_pg()`` for PostgresClient-only deployments.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# --------------------------------------------------------------------------
# Raw DDL mirroring the model below (for ``PostgresClient.pool`` deployments).
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_report (
        id           UUID PRIMARY KEY,
        code         VARCHAR(32)  NOT NULL,
        project_id   UUID         NOT NULL,
        run_id       UUID         NOT NULL,
        task_id      UUID,
        title        VARCHAR(512) NOT NULL,
        test_version VARCHAR(128),
        env_id       UUID,
        status       VARCHAR(32)  NOT NULL DEFAULT 'generating',
        snapshot     JSONB        NOT NULL DEFAULT '{}'::jsonb,
        generated_at TIMESTAMPTZ,
        generator    VARCHAR(16)  NOT NULL DEFAULT 'auto',
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (project_id, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_report_project ON mc_test_report(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_report_run     ON mc_test_report(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_report_task    ON mc_test_report(task_id)",
]

#: ``mc_test_report.status`` 状态机（03 §1）
REPORT_STATUSES: tuple[str, ...] = ("generating", "ready", "archived")

#: ``mc_test_report.generator``（03 §1：谁生成）
REPORT_GENERATORS: tuple[str, ...] = ("auto", "manual")


class TestReport(Model):
    """报告（03 §1 / design §15.11）——**生成时快照**的一等实体。

    ``snapshot`` 存 03 §2.1 的渲染结构（overview/timing/cases/bugs/
    requirements）；``generated_at`` 为生成时刻（定版时间）；``env_id`` 用
    ``UUIDField``（同 assets 场景表口径——环境表尚未建，先留占位）。
    """

    __test__ = False  # 类名以 Test 开头：告知 pytest 这不是测试用例

    id = fields.UUIDField(pk=True)
    code = fields.CharField(max_length=32)  # RPT-xxxx（按 project 自增）
    project_id = fields.UUIDField()
    run_id = fields.UUIDField()  # → mc_test_run（来自哪次运行）
    task_id = fields.UUIDField(null=True)  # → mc_test_task（来自哪个任务，可空）
    title = fields.CharField(max_length=512)
    test_version = fields.CharField(max_length=128, null=True)  # 被测系统版本/构建号
    env_id = fields.UUIDField(null=True)  # 环境
    status = fields.CharField(max_length=32, default="generating")
    snapshot = fields.JSONField(default=dict)  # 03 §2.1 渲染结构（生成时定版）
    generated_at = fields.DatetimeField(null=True)
    generator = fields.CharField(max_length=16, default="auto")  # auto|manual
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_report"
        unique_together = (("project_id", "code"),)
        indexes = (("project_id",), ("run_id",), ("task_id",))


async def create_tables_via_pg() -> None:
    """Create ``mc_test_report`` through the platform raw PG client.

    Used by deployments where Tortoise is not initialized (main service path).
    Matches the platform convention ``from db import PostgresClient``.
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
