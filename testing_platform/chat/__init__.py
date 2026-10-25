"""对话持久化域 —— dsh 会话与消息的平台侧存储。

* ``models``   ``mc_test_chat_session`` / ``mc_test_chat_message``
* ``service``  会话 CRUD + 消息追加/回写 + dsh_session_id 回写
"""
from .models import (
    CHAT_MESSAGE_STATUSES,
    CHAT_ROLES,
    CHAT_SESSION_STATUSES,
    DDL,
    TestChatMessage,
    TestChatSession,
    create_tables_via_pg,
)
from .service import (
    ChatNotFoundError,
    ChatValidationError,
    append_message,
    append_tool_message,
    archive_session,
    attach_dsh_session,
    create_session,
    delete_session,
    get_session,
    list_messages,
    list_sessions,
    rename_session,
    update_message,
)

__all__ = [
    "CHAT_MESSAGE_STATUSES",
    "CHAT_ROLES",
    "CHAT_SESSION_STATUSES",
    "ChatNotFoundError",
    "ChatValidationError",
    "DDL",
    "TestChatMessage",
    "TestChatSession",
    "append_message",
    "append_tool_message",
    "archive_session",
    "attach_dsh_session",
    "create_session",
    "create_tables_via_pg",
    "delete_session",
    "get_session",
    "list_messages",
    "list_sessions",
    "rename_session",
    "update_message",
]
