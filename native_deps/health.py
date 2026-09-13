"""Protocol-level readiness checks for native dependency services."""
from __future__ import annotations

import asyncio
import urllib.request


async def postgres_ready(host: str, port: int, user: str, database: str, password: str = "") -> bool:
    try:
        import asyncpg

        conn = await asyncio.wait_for(
            asyncpg.connect(host=host, port=port, user=user, password=password, database=database),
            timeout=3,
        )
        try:
            return await conn.fetchval("SELECT 1") == 1
        finally:
            await conn.close()
    except Exception:
        return False


async def redis_ready(host: str, port: int, db: int = 0) -> tuple[bool, str]:
    try:
        import redis.asyncio as redis

        client = redis.Redis(host=host, port=port, db=db)
        try:
            await asyncio.wait_for(client.ping(), timeout=3)
            info = await client.info("server")
            version = info.get("redis_version") or info.get(b"redis_version") or ""
            if isinstance(version, bytes):
                version = version.decode()
            return True, str(version)
        finally:
            await client.aclose()
    except Exception as exc:
        return False, str(exc)


async def clickhouse_ready(host: str, port: int) -> bool:
    def _probe() -> bool:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/ping", timeout=3) as response:
                return response.status == 200
        except Exception:
            return False

    return await asyncio.to_thread(_probe)


async def wait_until(probe, timeout: float, interval: float = 0.5) -> None:
    """Wait for an async boolean probe, raising TimeoutError with no busy loop."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await probe():
            return
        await asyncio.sleep(interval)
    raise TimeoutError(f"dependency did not become ready within {timeout:.0f}s")
