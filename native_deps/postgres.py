"""PostgreSQL lifecycle: initdb, foreground server, idempotent database create, stop."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from loguru import logger

from . import layout

DEFAULT_PORT = int(__import__("os").environ.get("NATIVE_POSTGRES_PORT", "15432"))
DEFAULT_USER = __import__("os").environ.get("NATIVE_POSTGRES_USER", "ai_lubricant")
DEFAULT_DATABASE = __import__("os").environ.get("NATIVE_POSTGRES_DATABASE", "ai_lubricant")


def _bin_dir(postgres_exe: Path) -> Path:
    return postgres_exe.resolve().parent


def data_root() -> Path:
    return layout.data_dir("postgres")


def initialized() -> bool:
    return (data_root() / "PG_VERSION").is_file()


def ensure_cluster(postgres_exe: Path) -> None:
    """Run ``initdb`` once. Idempotent: a present ``PG_VERSION`` skips it."""
    if initialized():
        return
    data = data_root()
    data.mkdir(parents=True, exist_ok=True)
    initdb = _bin_dir(postgres_exe) / ("initdb.exe" if __import__("sys").platform == "win32" else "initdb")
    cmd = [
        str(initdb),
        "-D", str(data),
        "-U", DEFAULT_USER,
        "--auth=trust",
        "--encoding=UTF8",
    ]
    logger.info("[native-deps] initdb {}", data)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"initdb failed: {result.stderr or result.stdout}")
    # Pin a stable local config: bind loopback only, fixed port, sane buffers.
    with (data / "postgresql.conf").open("a", encoding="utf-8") as fh:
        fh.write("\n# native-deps overlay\n")
        fh.write(f"listen_addresses = '127.0.0.1'\n")
        fh.write(f"port = {DEFAULT_PORT}\n")
        fh.write("shared_buffers = 128MB\n")


def start_command(postgres_exe: Path) -> list[str]:
    """Foreground server argv for a supervisor/supervisord to exec."""
    return [str(postgres_exe), "-D", str(data_root())]


def _psql(postgres_exe: Path, sql: str, database: str = "postgres") -> subprocess.CompletedProcess:
    psql = _bin_dir(postgres_exe) / ("psql.exe" if __import__("sys").platform == "win32" else "psql")
    cmd = [str(psql), "-h", "127.0.0.1", "-p", str(DEFAULT_PORT), "-U", DEFAULT_USER, "-d", database, "-tc", sql]
    return subprocess.run(cmd, capture_output=True, text=True)


def ensure_database(postgres_exe: Path, name: str = DEFAULT_DATABASE) -> None:
    exists = _psql(postgres_exe, f"SELECT 1 FROM pg_database WHERE datname='{name}'")
    if exists.returncode != 0:
        raise RuntimeError(f"postgres probe failed: {exists.stderr or exists.stdout}")
    if "1" in (exists.stdout or ""):
        return
    result = _psql(postgres_exe, f'CREATE DATABASE "{name}"')
    if result.returncode != 0:
        raise RuntimeError(f"CREATE DATABASE failed: {result.stderr or result.stdout}")


def stop(postgres_exe: Path) -> None:
    pg_ctl = _bin_dir(postgres_exe) / ("pg_ctl.exe" if __import__("sys").platform == "win32" else "pg_ctl")
    result = subprocess.run(
        [str(pg_ctl), "-D", str(data_root()), "stop", "-m", "fast"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning("[native-deps] pg_ctl stop: {}", (result.stderr or result.stdout).strip())


async def wait_ready(postgres_exe: Path, timeout: float = 60.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = _psql(postgres_exe, "SELECT 1")
            if result.returncode == 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False
