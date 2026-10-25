"""Tortoise models for the testing-platform **question + craft domains**.

Two "process first-class citizen" domains from
``docs/ai-testing-platform/design.md``:

=====================  =================================================
表                     说明
=====================  =================================================
``mc_test_question``   疑问（design §15.2，**一等公民**：未解决阻塞下游）
``mc_test_craft_asset`` 沉淀四件套（design §15.6：约定/模板/样例/校验器）
=====================  =================================================

**疑问（design §15.2 的核心约束）**：未解决疑问**阻塞下游**——用例带
open 疑问不得进脚本生成；脚本带 open 疑问不得进执行。因此本表不是
"随手记一条"：``raise`` 必须带 ``blocking_refs``（被阻塞的下游实体），
``resolve`` 必须带 ``resolution``（debug/doc/user_confirm）与依据。

**沉淀（design §15.6）**：AI 按当前上下文**按需加载**相关层
（common 通用 → framework 框架 → project 项目），故表带
``scope`` / ``target`` 两个正交维度，``craft.load`` 按 type+target 读。

Conventions follow ``testing_platform/assets/models.py``: UUID primary
keys, ``mc_`` prefix, JSONB via ``JSONField``, raw ``DDL`` +
``create_tables_via_pg()`` for PostgresClient-only deployments.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


# --------------------------------------------------------------------------
# 领域异常（供 quality.service 共用；base 放在模型包最底层，避免循环导入）。
# --------------------------------------------------------------------------
class QuestionError(Exception):
    """疑问域错误基类。"""


class QuestionValidationError(QuestionError, ValueError):
    """入参不合法（枚举越界、缺必填、引用不存在）。继承 ``ValueError``。"""


class QuestionNotFoundError(QuestionError, LookupError):
    """疑问不存在。"""


class QuestionBlockingError(QuestionError, ValueError):
    """未解决疑问**阻塞下游**（design §15.2）。

    ``ref_id`` 是被阻塞的下游实体（用例 / 场景）id，``question_ids`` 是
    阻塞它的 open 疑问列表。
    """

    code = "question_blocking"

    def __init__(self, ref_id, question_ids) -> None:
        self.ref_id = ref_id
        self.question_ids = list(question_ids)
        super().__init__(
            f"实体 {ref_id!r} 被 {len(self.question_ids)} 个未解决疑问阻塞，不得进入下游："
            f"{self.question_ids}"
        )
        self.details = {
            "code": self.code,
            "ref_id": str(ref_id),
            "question_ids": [str(q) for q in self.question_ids],
        }


class CraftValidationError(QuestionError, ValueError):
    """沉淀资产入参不合法。"""


class CraftNotFoundError(QuestionError, LookupError):
    """沉淀资产不存在。"""


# --------------------------------------------------------------------------
# 枚举（design §15.2 / §15.6）
# --------------------------------------------------------------------------
#: ``mc_test_question.origin`` 疑问来源（design §15.2）
QUESTION_ORIGINS: tuple[str, ...] = ("product_uncertainty", "design_doubt")

#: ``mc_test_question.status`` 疑问状态机（design §十六：open → resolved）
QUESTION_STATUSES: tuple[str, ...] = ("open", "resolved")

#: ``mc_test_question.resolution`` 收敛方式（design §15.2 / 十六）。
#: ``risk_accepted`` 为生成流程死锁出口（25 号文 v4.1 门⓪）：拷问预算耗尽仍有未答
#: 问题时按推荐答案假设放行，DB 列是 VARCHAR 无约束，枚举扩展零迁移。
QUESTION_RESOLUTIONS: tuple[str, ...] = ("debug", "doc", "user_confirm", "risk_accepted")

#: ``mc_test_craft_asset.type`` 沉淀四件套（design §15.6）
CRAFT_TYPES: tuple[str, ...] = ("convention", "template", "sample", "validator")

#: ``mc_test_craft_asset.scope`` 加载层（design §15.6：按需分层加载）
CRAFT_SCOPES: tuple[str, ...] = ("common", "framework", "project")

#: ``mc_test_craft_asset.target`` 作用目标（design §15.6）
CRAFT_TARGETS: tuple[str, ...] = ("scenario", "case", "tool")

#: ``mc_test_craft_asset.status`` 生命周期（同方法模块库风格）
CRAFT_STATUSES: tuple[str, ...] = ("draft", "active", "deprecated", "archived")


# --------------------------------------------------------------------------
# Raw DDL mirroring the models below (for ``PostgresClient.pool`` deployments).
# --------------------------------------------------------------------------
DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_question (
        id                UUID PRIMARY KEY,
        project_id        UUID        NOT NULL,
        origin            VARCHAR(32) NOT NULL,
        raised_by         VARCHAR(32),
        content           TEXT        NOT NULL,
        context           JSONB       NOT NULL DEFAULT '{}'::jsonb,
        status            VARCHAR(16) NOT NULL DEFAULT 'open',
        resolution        VARCHAR(32),
        resolution_detail JSONB       NOT NULL DEFAULT '{}'::jsonb,
        resolved_at       TIMESTAMPTZ,
        blocking_refs     JSONB       NOT NULL DEFAULT '[]'::jsonb,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_craft_asset (
        id          UUID PRIMARY KEY,
        type        VARCHAR(16)  NOT NULL,
        scope       VARCHAR(16)  NOT NULL DEFAULT 'project',
        target      VARCHAR(16)  NOT NULL,
        content     JSONB        NOT NULL DEFAULT '{}'::jsonb,
        version     INTEGER      NOT NULL DEFAULT 1,
        status      VARCHAR(32)  NOT NULL DEFAULT 'active',
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_question_project  ON mc_test_question(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_question_status   ON mc_test_question(status)",
    "CREATE INDEX IF NOT EXISTS idx_test_craft_type        ON mc_test_craft_asset(type)",
    "CREATE INDEX IF NOT EXISTS idx_test_craft_scope       ON mc_test_craft_asset(scope)",
    "CREATE INDEX IF NOT EXISTS idx_test_craft_target      ON mc_test_craft_asset(target)",
]


class TestQuestion(Model):
    """疑问（design §15.2）——**流程一等公民**，未解决阻塞下游。

    ``origin`` 区分「产品不确定」/「设计疑虑」；``context`` 记发现位置 +
    关联实体；``status`` 走 open → resolved；``resolution`` 记收敛方式
    （debug/doc/user_confirm）；``blocking_refs`` 是被该疑问阻塞的下游实体
    （用例/场景）id 列表 —— resolve 时才真正解除阻塞。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    origin = fields.CharField(max_length=32)  # product_uncertainty|design_doubt
    raised_by = fields.CharField(max_length=32, null=True)  # 提出角色（analyst/designer/...）
    content = fields.TextField()  # 疑问正文
    context = fields.JSONField(default=dict)  # 发现位置 + 关联实体
    status = fields.CharField(max_length=16, default="open")  # open|resolved
    resolution = fields.CharField(max_length=32, null=True)  # debug|doc|user_confirm
    resolution_detail = fields.JSONField(default=dict)  # 收敛依据
    resolved_at = fields.DatetimeField(null=True)
    blocking_refs = fields.JSONField(default=list)  # 被阻塞的下游 case/scenario id
    # ── 生成流程扩列（25 号文 v4.1 §2.4；DB 列由 generation/models.ensure_question_columns
    #    补，此处声明让 Tortoise 真正持久化——不声明则 create 传这些 kwarg 会被静默吞）──
    requirement_id = fields.UUIDField(null=True)  # 挂哪需求
    testpoint_id = fields.UUIDField(null=True)  # 挂哪测试点
    recommended_answer = fields.TextField(null=True)  # AI 推荐答案（拷问式带默认）
    answer = fields.TextField(null=True)  # 人给的答案/处置说明
    round_no = fields.IntField(null=True)  # 第几问（门⓪①各 10-15 预算计数）
    gate = fields.CharField(max_length=16, null=True)  # req_review|point_confirm
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_question"
        indexes = (("project_id",), ("status",))


class TestCraftAsset(Model):
    """沉淀四件套（design §15.6）——约定 / 模板 / 样例 / 校验器。

    ``type`` 决定资产形态（convention=约定、template=模板、sample=样例、
    validator=校验器）；``scope`` 决定加载层（common 通用 / framework 框架 /
    project 项目），AI 按当前上下文**按需加载**相关层；``target`` 是作用
    目标（scenario/case/tool）。``content`` 存结构化或文本（JSONB）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    type = fields.CharField(max_length=16)  # convention|template|sample|validator
    scope = fields.CharField(max_length=16, default="project")  # common|framework|project
    target = fields.CharField(max_length=16)  # scenario|case|tool
    content = fields.JSONField(default=dict)  # 结构化或文本（JSONB）
    version = fields.IntField(default=1)
    status = fields.CharField(max_length=32, default="active")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_craft_asset"
        indexes = (("type",), ("scope",), ("target",))


async def create_tables_via_pg() -> None:
    """Create question/craft tables through the platform raw PG client."""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)