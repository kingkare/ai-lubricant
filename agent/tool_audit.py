"""Durable audit sink for GenericAgent tool execution."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from loguru import logger


async def record_tool_audit(
    *, agent_id: int | None, conversation_id: str, caller: str | None,
    tool_name: str, payload: dict[str, Any], approval_id: str | None,
    outcome: str, result: dict[str, Any] | None = None,
) -> None:
    """Insert a redacted audit row; audit failure never changes tool outcome."""
    try:
        from db import PostgresClient

        if not PostgresClient.pool:
            return
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        redacted = {
            "payload_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "payload": payload,
            "result": result,
        }
        async with PostgresClient.pool.acquire() as conn:
            # 建表已挪到 server/db.py create_agent_tables（Phase 2.2）：热路径不再
            # 每次写入跑 DDL。这里只 INSERT。
            await conn.execute(
                """INSERT INTO agent_tool_audit_logs(
                    agent_id, conversation_id, caller_id, tool_name, approval_id, outcome, detail
                ) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb)""",
                agent_id, conversation_id, caller, tool_name, approval_id, outcome,
                json.dumps(redacted, ensure_ascii=False, default=str),
            )
    except Exception:  # noqa: BLE001
        logger.warning("[agent] tool audit write failed tool={}", tool_name, exc_info=True)


__all__ = ["record_tool_audit"]
