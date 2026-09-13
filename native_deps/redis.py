"""Redis lifecycle. Windows uses a community 7.x build (configurable); the
daemon is started in foreground so a parent supervisor owns its lifetime."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

from . import layout

DEFAULT_PORT = int(__import__("os").environ.get("NATIVE_REDIS_PORT", "6479"))
MIN_VERSION = (6, 0)


def data_root() -> Path:
    return layout.data_dir("redis")


def config_path() -> Path:
    path = layout.config_dir() / "redis.conf"
    if not path.exists():
        path.write_text(
            "\n".join([
                "# native-deps overlay",
                f"port {DEFAULT_PORT}",
                "bind 127.0.0.1",
                f"dir {data_root()}",
                "appendonly yes",
                "maxmemory-policy noeviction",
                "save \"\"",
                "protected-mode no",
            ]) + "\n",
            encoding="utf-8",
        )
    return path


def start_command(redis_exe: Path) -> list[str]:
    return [str(redis_exe), str(config_path()), "--port", str(DEFAULT_PORT)]


async def _probe(port: int = DEFAULT_PORT) -> tuple[bool, str]:
    try:
        import redis.asyncio as aioredis  # type: ignore
    except Exception:
        return False, "redis client unavailable"
    try:
        client = aioredis.Redis(host="127.0.0.1", port=port)
        try:
            await asyncio.wait_for(client.ping(), timeout=3)
            info = await client.info("server")
            version = info.get("redis_version") or info.get(b"redis_version")
            if isinstance(version, bytes):
                version = version.decode()
            return True, str(version)
        finally:
            with __import__("contextlib").suppress(Exception):
                await client.aclose()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def wait_ready(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    last = "no probe result"
    while time.monotonic() < deadline:
        ok, detail = await _probe()
        if ok:
            assert_version_supported(detail)
            return True
        last = detail
        await asyncio.sleep(0.5)
    logger.warning("[native-deps] redis not ready within {}s: last={}", int(timeout), last)
    return False


def assert_version_supported(version: str) -> None:
    try:
        major, minor = (int(p) for p in version.split(".")[:2])
    except (ValueError, AttributeError):
        raise RuntimeError(f"cannot parse redis version {version!r}")
    if (major, minor) < MIN_VERSION:
        raise RuntimeError(
            f"redis {version} < {MIN_VERSION[0]}.{MIN_VERSION[1]}: coredis forces RESP3 HELLO; "
            "set NATIVE_REDIS_DOWNLOAD_URL to a Redis 6+ Windows build or install Memurai."
        )
