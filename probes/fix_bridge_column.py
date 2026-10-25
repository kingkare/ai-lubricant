"""给已建库补 ``mc_test_project.coding_project_id``（不重启即时生效）。

平台跑着的进程加载的是旧代码，Python 不热重载——所以代码里的运行时自愈
（``ensure_bridge_column``）要重启才跑得到。DDL 是数据库侧的：直接对
PostgreSQL 执行 ``ALTER TABLE ... ADD COLUMN``，当前服务立刻就不报错。

用平台自己的配置派生连接（``POSTGRES_*``），不打印任何凭据。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bootstrap_config import get_postgres_config  # noqa: E402


async def main() -> int:
    import asyncpg

    pg = get_postgres_config()
    dsn = f"postgresql://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['database']}"
    print(f"[fix] {pg['host']}:{pg['port']}/{pg['database']} as {pg['user']}")

    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='mc_test_project' AND column_name='coding_project_id'"
        )
        if exists:
            print("[fix] column already exists — nothing to do")
            return 0
        table = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_name='mc_test_project'"
        )
        if not table:
            print("[fix] table mc_test_project 不存在 — 需重启让 generate_schemas 建表")
            return 2
        await conn.execute("ALTER TABLE mc_test_project ADD COLUMN coding_project_id UUID;")
        print("[fix] ADDED mc_test_project.coding_project_id")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
