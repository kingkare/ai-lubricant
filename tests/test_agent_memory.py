import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from db import PostgresClient
from agent.memory import MemorySystem


def run(coro):
    return asyncio.run(coro)


class FakeConn:
    def __init__(self, fetch_rows=None):
        self.execute = AsyncMock()
        self.fetch = AsyncMock(side_effect=list(fetch_rows or []))


@pytest.fixture
def reset_pool():
    original = PostgresClient.pool
    PostgresClient.pool = None
    yield
    PostgresClient.pool = original


@pytest.fixture
def fake_db(reset_pool):
    conn = FakeConn()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    PostgresClient.pool = pool
    return conn


def test_get_l4_archive_read_only(fake_db):
    """memory.py 只剩 L4 归档只读（Phase 2.2 瘦身）：L1/L2/L3 走 file_memory，无 PG 路径。"""
    rows = [
        {"session_id": "s2", "summary": "newest"},
        {"session_id": "s1", "summary": "older"},
    ]
    fake_db.fetch.side_effect = [rows]
    memory = MemorySystem()

    result = run(memory.get_l4_archive(limit=2))

    fetch_sql, *fetch_params = fake_db.fetch.await_args.args
    assert "FROM agent_session_archives" in fetch_sql
    assert "ORDER BY archived_at DESC" in fetch_sql
    assert fetch_params == [2]
    assert result == rows


def test_get_l4_archive_no_pool_returns_empty(reset_pool):
    """无 PG 池（如无人值守/测试环境）安全返回空列表，不抛。"""
    memory = MemorySystem(agent_id=7)
    assert run(memory.get_l4_archive(limit=3)) == []


def test_memory_system_no_dead_surface():
    """瘦身护栏：L1/L2/L3 的 PG/Redis 死路径不应再回来（文件那套是唯一真相源）。"""
    assert not hasattr(MemorySystem, "get_l1_insights")
    assert not hasattr(MemorySystem, "upsert_l1_insight")
    assert not hasattr(MemorySystem, "get_l2_facts")
    assert not hasattr(MemorySystem, "upsert_l2_fact")
    assert not hasattr(MemorySystem, "_l2_redis")
    assert not hasattr(MemorySystem, "learn_skill")
    assert not hasattr(MemorySystem, "archive_session")
    assert hasattr(MemorySystem, "get_l4_archive")
