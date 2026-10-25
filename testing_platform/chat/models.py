"""Tortoise models for the **chat domain** (``mc_test_chat_*``).

dsh 对话的**平台侧持久化**：session 由 dsh 维持在其 ``dsh_home`` 内，但平台要能
跨刷新/跨设备续聊，就得自己存一份会话与消息（design §十 数据治理：稳定 ID +
版本 + 溯源）。

======  ==========================================================
表      说明
======  ==========================================================
``mc_test_chat_session``   对话会话（绑测试项目 + dsh session_id + 模型）
``mc_test_chat_message``   消息（user/assistant/tool 三类，按 seq 有序）
======  ==========================================================

``dsh_session_id`` 是**跨轮续接**的关键：平台会话保存它，下一轮发给 dsh 时带上，
dsh 就接着同一份上下文跑。工具调用（``tool/call`` + ``tool/result``）作为
``role=tool`` 的消息落库，回放时渲染成卡片。
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

DDL = [
    """
    CREATE TABLE IF NOT EXISTS mc_test_chat_session (
        id              UUID PRIMARY KEY,
        project_id      UUID         NOT NULL,
        title           VARCHAR(512),
        dsh_session_id  VARCHAR(255),
        model           VARCHAR(128) NOT NULL DEFAULT '',
        api_key_id      INTEGER,
        created_by      UUID,
        message_count   INTEGER      NOT NULL DEFAULT 0,
        last_message_at TIMESTAMPTZ,
        status          VARCHAR(16)  NOT NULL DEFAULT 'active',
        created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
        updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mc_test_chat_message (
        id           UUID PRIMARY KEY,
        session_id   UUID         NOT NULL,
        seq          INTEGER      NOT NULL,
        role         VARCHAR(16)  NOT NULL,
        content      TEXT         NOT NULL DEFAULT '',
        tool_name    VARCHAR(255),
        tool_payload JSONB        NOT NULL DEFAULT '{}'::jsonb,
        status       VARCHAR(16)  NOT NULL DEFAULT 'done',
        created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
        UNIQUE (session_id, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_test_chat_session_project ON mc_test_chat_session(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_chat_session_dsh      ON mc_test_chat_session(dsh_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_test_chat_message_session  ON mc_test_chat_message(session_id)",
]

#: ``mc_test_chat_session.status``
CHAT_SESSION_STATUSES: tuple[str, ...] = ("active", "archived")

#: ``mc_test_chat_message.role``（dsh 事件归一后的三类）
CHAT_ROLES: tuple[str, ...] = ("user", "assistant", "tool", "system")

#: ``mc_test_chat_message.status``（streaming 中的 assistant 消息 = streaming）
CHAT_MESSAGE_STATUSES: tuple[str, ...] = ("streaming", "done", "error")


class TestChatSession(Model):
    """对话会话（``mc_test_chat_session``）。"""

    __test__ = False  # 与 assets 模型同惯例：别让 pytest 误收集

    id = fields.UUIDField(pk=True)
    project_id = fields.UUIDField()
    title = fields.CharField(max_length=512, null=True)
    dsh_session_id = fields.CharField(max_length=255, null=True)  # dsh 侧续接键
    model = fields.CharField(max_length=128, default="")
    api_key_id = fields.IntField(null=True)  # 用了哪把平台 Key（不存明文）
    created_by = fields.UUIDField(null=True)
    message_count = fields.IntField(default=0)
    last_message_at = fields.DatetimeField(null=True)
    status = fields.CharField(max_length=16, default="active")  # active|archived
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_chat_session"
        indexes = (("project_id",), ("dsh_session_id",))


class TestChatMessage(Model):
    """对话消息（``mc_test_chat_message``，按 ``(session_id, seq)`` 有序）。"""

    __test__ = False

    id = fields.UUIDField(pk=True)
    session_id = fields.UUIDField()
    seq = fields.IntField()
    role = fields.CharField(max_length=16)  # user|assistant|tool|system
    content = fields.TextField(default="")
    tool_name = fields.CharField(max_length=255, null=True)  # role=tool 时
    tool_payload = fields.JSONField(default=dict)  # 调用参数 / 结果摘要
    status = fields.CharField(max_length=16, default="done")  # streaming|done|error
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "testing_platform"
        table = "mc_test_chat_message"
        unique_together = (("session_id", "seq"),)
        indexes = (("session_id",),)


async def create_tables_via_pg() -> None:
    """Create ``mc_test_chat_*`` tables through the platform raw PG client."""
    from db import PostgresClient

    if not PostgresClient.pool:
        raise RuntimeError("PostgresClient.pool is not initialized")
    async with PostgresClient.pool.acquire() as conn:
        for stmt in DDL:
            await conn.execute(stmt)
