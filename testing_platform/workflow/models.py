"""工作流域 Tortoise 模型（design §15.9）。

平台**不绑定任何具体 agent**（design §6.2）——本模块只存两条表：

* ``mc_test_workflow_def`` —— 7 角色**工作流定义**：``input_schema →
  output_schema → tool_refs → contract``。LLM 角色由外部 agent 经能力面跑，
  平台只存定义 + 事後做契约校验，**绝不内嵌 LLM 客户端**。
* ``mc_test_workflow_run`` —— 一次工作流**运行实例**：带 ``input`` / ``output``
  与状态机 ``pending → running → done / failed / blocked_by_question``。

约定同 ``testing_platform/assets/models.py``：UUID 主键、``mc_`` 前缀、
JSONB 走 ``JSONField``、裸 ``DDL`` + ``create_tables_via_pg()`` 供只装
``PostgresClient.pool``（Tortoise 未初始化）的部署建表。

executor 角色是 ``nature='deterministic'``（不走 agent）；其余 6 角色
``nature='llm'``（外部 agent 驱动，平台不跑模型）。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# Order matters: def before run (run references def logically).
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_workflow_def (
        id           UUID PRIMARY KEY,
        role         VARCHAR(32)  NOT NULL,
        input_schema JSONB        NOT NULL DEFAULT '{}'::jsonb,
        output_schema JSONB       NOT NULL DEFAULT '{}'::jsonb,
        tool_refs    JSONB        NOT NULL DEFAULT '[]'::jsonb,
        contract     JSONB        NOT NULL DEFAULT '{}'::jsonb,
        nature       VARCHAR(16)  NOT NULL DEFAULT 'llm',
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_workflow_run (
        id             UUID PRIMARY KEY,
        workflow_def_id UUID        NOT NULL,
        run_id         UUID,
        input          JSONB       NOT NULL DEFAULT '{}'::jsonb,
        output         JSONB       NOT NULL DEFAULT '{}'::jsonb,
        status         VARCHAR(32) NOT NULL DEFAULT 'pending',
        agent_runtime  VARCHAR(64),
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_workflow_run_def    ON mc_test_workflow_run(workflow_def_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_workflow_run_status ON mc_test_workflow_run(status)",
]

#: ``mc_test_workflow_def.role`` —— 7 角色（design §17.3）
WORKFLOW_ROLES: tuple[str, ...] = (
    "planner",
    "analyst",
    "designer",
    "coder",
    "executor",
    "debugger",
    "reporter",
)

#: ``mc_test_workflow_def.nature`` —— LLM 角色 vs 确定性角色（design §17.3）
WORKFLOW_NATURES: tuple[str, ...] = ("llm", "deterministic")

#: ``mc_test_workflow_run.status`` 状态机（design 十六「工作流运行」）
WORKFLOW_RUN_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "done",
    "failed",
    "blocked_by_question",
)


class TestWorkflowDef(Model):
    """7 角色工作流定义（design §15.9 ``mc_test_workflow_def``）。

    ``tool_refs`` 是该角色可调的工具方法名（design §17.1，如
    ``scenario.generate``）；``contract`` 是必须满足的自然语言契约（一句
    要求）；``nature='deterministic'`` 表示该角色不走 agent（executor）。
    """

    __test__ = False  # 类名以 Test 开头：告知 pytest 这不是测试用例

    id = fields.UUIDField(pk=True)
    role = fields.CharField(max_length=32, unique=True)
    input_schema = fields.JSONField(default=dict)   # JSON Schema 子集（入参结构）
    output_schema = fields.JSONField(default=dict)  # JSON Schema 子集（产出结构）
    tool_refs = fields.JSONField(default=list)      # 该角色可调的工具方法名
    contract = fields.JSONField(default=dict)       # 自然语言契约（必须满足）
    nature = fields.CharField(max_length=16, default="llm")  # llm|deterministic
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_workflow_def"


class TestWorkflowRun(Model):
    """一次工作流运行实例（design §15.9 ``mc_test_workflow_run``）。

    ``run_id`` 是**关联的引擎执行运行**（可空，指向 ``mc_test_run``）——注意
    与本行的主键 ``id`` 区分：主键 ``id`` 是这个工作流运行自己的身份，
    ``run_id`` 是它绑定到/产出的引擎执行运行。``agent_runtime`` 记录由哪个
    runtime 驱动（部署选择，本阶段留空）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    workflow_def_id = fields.UUIDField()   # → mc_test_workflow_def
    run_id = fields.UUIDField(null=True)   # → mc_test_run（可空）
    input = fields.JSONField(default=dict)     # 角色输入
    output = fields.JSONField(default=dict)    # 角色产出（过契约校验）
    status = fields.CharField(max_length=32, default="pending")
    agent_runtime = fields.CharField(max_length=64, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_workflow_run"
        indexes = (("workflow_def_id",), ("status",))


async def create_tables_via_pg() -> None:
    """Create ``mc_test_workflow_*`` tables through the platform raw PG client.

    Used by deployments where Tortoise is not initialized (main service path).
    Matches the platform convention ``from db import PostgresClient``.
    """
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
