"""Legacy PostgreSQL memory helpers for GenericAgent (slimmed, Phase 2.2).

The file-backed GA memory set (``agent/file_memory.py``: L1 insight index, L2
facts, L3 SOPs, self-improvement log) is the only live memory system; the
PG/Redis dual-track below (``agent_insight_index`` / ``agent_global_facts`` /
``agent:l2:*`` / ``agent_skills``) had zero consumers and has been removed.

What remains is the one still-called legacy surface: ``MemorySystem.get_l4_archive``
— a read-only view of ``agent_session_archives`` (a table with no remaining
writers) served by the two diagnostic memory endpoints in ``agent/api.py``.
ClickHouse ``agent_conversations`` / ``agent_messages`` retain complete
conversations; this table is retained for historical rows only.
"""
from __future__ import annotations

import logging
from typing import Any

from db import PostgresClient

logger = logging.getLogger(__name__)


class MemorySystem:
    """Legacy L4 archive reader (the only surviving PG memory surface).

    Parameters:
        agent_id: Optional agent ID for multi-tenant isolation.
                  When set, all queries filter by agent_id.
    """

    def __init__(self, agent_id: int | None = None):
        self.agent_id = agent_id

    async def get_l4_archive(
        self,
        limit: int = 10,
        session_id: str | None = None,
    ) -> list[dict]:
        """Get L4 archives. Can filter by session_id (FIX: was missing)."""
        if not PostgresClient.pool:
            return []

        conditions = []
        params: list[Any] = []
        param_idx = 1

        if self.agent_id is not None:
            conditions.append(f"agent_id=${param_idx}")
            params.append(self.agent_id)
            param_idx += 1
        if session_id:
            conditions.append(f"session_id=${param_idx}")
            params.append(session_id)
            param_idx += 1

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            "SELECT id, session_id, request_log_id, task_description, summary, "
            "key_insights, skills_used, total_turns, total_duration_ms, success, "
            "archived_at, metadata "
            "FROM agent_session_archives "
            f"WHERE {where} "
            "ORDER BY archived_at DESC "
            f"LIMIT ${param_idx}"
        )
        params.append(limit)

        async with PostgresClient.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]
