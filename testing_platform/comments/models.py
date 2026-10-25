"""评论域（26 号文 §2.3）——需求/测试点上的讨论串。

**为什么需要**（用户原话）：让不同的人进行评论，最终得出一个结论，
并且**将评论的历史记录下来**。

现状：60 张 ``mc_test_`` 表里**没有任何 comment 表**。最接近的
``mc_test_question`` 是「提问/收敛」语义（流程一等公民、未解决阻塞下游），
不是评论串；缺陷的 ``mc_test_bug_log.comment`` 只是流转附言。
⇒ 本模块是**新增域**，不改造既有表。

对外表面（``__all__``）
----------------------

====================================  =========================================
:data:`COMMENT_ENTITY_TYPES`           挂载对象（requirement / testpoint）
:data:`COMMENT_STATUSES`              open / resolved
:data:`RESOLUTIONS`                   结论取值（conclusion / agree / …）
:class:`TestComment`                  ``mc_test_comment`` 表
:class:`TestCommentReaction`          ``mc_test_comment_reaction`` 表
====================================  =========================================

依赖纪律
--------
不 import 任何业务模型（``assets`` / ``generation``）——**挂载对象用
``entity_type`` + ``entity_id`` 双字段表达，不加外键**（需求/测试点/用例
分属不同包，加 FK 会造成包间循环依赖；裸 UUID 是本仓既有惯例）。

歧义记录
--------
1. **「有新的评论」怎么判定**：需求上的未知项（``analysis.register`` 里
   ``kind=to_confirm`` 的条目）要显示「有新的评论」徽章。本域提供
   :func:`count_unread_by_entity`（按 ``entity_id`` + ``created_at > since``
   计数），具体"未知项 ↔ 评论"的关联由调用方用 ``entity_ref``（见下）串。
2. **``entity_ref``**：评论除了挂实体（``entity_id``），还可挂实体上的**某个具体条目**
   （如某条未知项）——用 ``entity_ref``（自由 key，如 ``register:idx3``）表达，
   不建子表。
3. **嵌套**：``parent_id`` 自引用，一层即可（评论是对话，不是树）。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

__all__ = [
    "DDL",
    "COMMENT_ENTITY_TYPES",
    "COMMENT_ENTITY_LABELS",
    "COMMENT_STATUSES",
    "RESOLUTIONS",
    "REACTION_KINDS",
    "TestComment",
    "TestCommentReaction",
    "create_tables_via_pg",
]

#: 挂载对象类型（26 号文 §2.3：范围是需求、测试点）。
COMMENT_ENTITY_TYPES: tuple[str, ...] = ("requirement", "testpoint")

#: 人读标签。
COMMENT_ENTITY_LABELS: dict[str, str] = {
    "requirement": "需求",
    "testpoint": "测试点",
}

#: 评论状态。
COMMENT_STATUSES: tuple[str, ...] = ("open", "resolved")

#: 结论取值（收敛时记一条——用户原话「最终得出一个结论」）。
RESOLUTIONS: tuple[str, ...] = (
    "conclusion",   # 已得出结论（默认）
    "agreed",       # 达成一致
    "rejected",     # 否决（这条不采纳）
    "need_work",    # 需返工
    "duplicate",    # 重复
)

#: 表情回应种类。
REACTION_KINDS: tuple[str, ...] = ("ack", "agree", "disagree", "question")


DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS mc_test_comment (
        id           UUID PRIMARY KEY,
        project_id   UUID         NOT NULL,
        entity_type  VARCHAR(32)  NOT NULL,
        entity_id    UUID         NOT NULL,
        entity_ref   VARCHAR(128),
        parent_id    UUID,
        content      TEXT         NOT NULL,
        status       VARCHAR(16)  NOT NULL DEFAULT 'open',
        resolution   VARCHAR(32),
        resolution_detail JSONB   NOT NULL DEFAULT '{}'::jsonb,
        resolved_by  UUID,
        resolved_at  TIMESTAMPTZ,
        created_by   UUID         NOT NULL,
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        CONSTRAINT ck_test_comment_entity_type
            CHECK (entity_type IN ('requirement','testpoint')),
        CONSTRAINT ck_test_comment_status
            CHECK (status IN ('open','resolved'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_comment_reaction (
        id          UUID PRIMARY KEY,
        comment_id  UUID         NOT NULL,
        kind        VARCHAR(16)  NOT NULL,
        created_by  UUID         NOT NULL,
        created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
        CONSTRAINT uq_test_comment_reaction UNIQUE (comment_id, kind, created_by),
        CONSTRAINT ck_test_comment_reaction_kind
            CHECK (kind IN ('ack','agree','disagree','question'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_comment_entity   ON mc_test_comment(project_id, entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_comment_parent   ON mc_test_comment(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_comment_created  ON mc_test_comment(project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_test_comment_reaction ON mc_test_comment_reaction(comment_id)",
)


class TestComment(Model):
    """评论 —— ``mc_test_comment``（26 号文 §2.3）。

    - ``entity_type`` + ``entity_id``：挂载对象（裸 UUID，**不加 FK**——需求在
      ``assets`` 包、测试点在 ``generation`` 包，加 FK 会造成包间循环依赖）。
    - ``entity_ref``：挂实体上的**某个具体条目**（如某条未知项
      ``register:idx3``）——用字符串 key 表达，不建子表。
    - ``parent_id``：回复（自引用，一层即可）。
    - ``status``：open / resolved。
    - ``resolution``：结论取值（:data:`RESOLUTIONS`）。
    - ``resolution_detail``：结论的补充（结构化 JSON）。
    - **历史留存**：本表**只增不删**（编辑走 ``content`` 覆写 + 保留 resolved 态）；
      「记录下来」= 永不物理删除，评论一直可查。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    entity_type = fields.CharField(max_length=32)      # COMMENT_ENTITY_TYPES
    entity_id = fields.UUIDField()                     # 裸 UUID，无 FK
    entity_ref = fields.CharField(max_length=128, null=True)
    parent_id = fields.UUIDField(null=True)             # 回复（自引用）
    content = fields.TextField()
    status = fields.CharField(max_length=16, default="open")
    resolution = fields.CharField(max_length=32, null=True)
    resolution_detail = fields.JSONField(default=dict)
    resolved_by = fields.UUIDField(null=True)
    resolved_at = fields.DatetimeField(null=True)
    created_by = fields.UUIDField()
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_comment"
        indexes = (
            ("project_id", "entity_type", "entity_id"),
            ("parent_id",),
            ("project_id", "created_at"),
        )


class TestCommentReaction(Model):
    """评论表情 —— ``mc_test_comment_reaction``（26 号文 §2.3）。

    唯一约束 ``(comment_id, kind, created_by)``：同一人对同一条同种表情只记一行
    （再点 = 取消）。
    """

    __test__ = False

    id = fields.UUIDField(pk=True)
    comment_id = fields.UUIDField()                    # 裸 UUID，无 FK
    kind = fields.CharField(max_length=16)              # REACTION_KINDS
    created_by = fields.UUIDField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_comment_reaction"
        unique_together = (("comment_id", "kind", "created_by"),)
        indexes = (("comment_id",),)


async def create_tables_via_pg() -> None:
    """通过平台原生 PG client 建评论两张表（幂等）。"""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
