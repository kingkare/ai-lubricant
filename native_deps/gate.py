"""Readiness gate used by generated supervisord commands."""
from __future__ import annotations

import argparse
import asyncio
import os

from . import clickhouse, health, postgres, redis


async def _wait(kind: str) -> None:
    if kind == "postgres":
        ok = await health.postgres_ready(
            os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            int(os.environ.get("POSTGRES_PORT", str(postgres.DEFAULT_PORT))),
            os.environ.get("POSTGRES_USER", postgres.DEFAULT_USER),
            os.environ.get("POSTGRES_DATABASE", postgres.DEFAULT_DATABASE),
            os.environ.get("POSTGRES_PASSWORD", postgres.DEFAULT_USER),
        )
        if not ok:
            raise TimeoutError("postgres is not ready")
    elif kind == "redis":
        ok, version = await redis._probe(int(os.environ.get("REDIS_PORT", str(redis.DEFAULT_PORT))))
        if not ok:
            raise TimeoutError(f"redis is not ready: {version}")
        redis.assert_version_supported(version)
    elif kind == "clickhouse":
        if not await clickhouse._probe_http():
            raise TimeoutError("clickhouse is not ready")
    else:
        raise ValueError(f"unknown dependency {kind!r}")


async def run(kinds: list[str], timeout: float) -> None:
    async def all_ready() -> bool:
        try:
            for kind in kinds:
                await _wait(kind)
            return True
        except Exception:
            return False

    await health.wait_until(all_ready, timeout=timeout, interval=1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="wait for native dependencies")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("kinds", nargs="+", choices=("postgres", "redis", "clickhouse"))
    args = parser.parse_args()
    asyncio.run(run(args.kinds, args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
