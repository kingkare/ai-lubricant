"""ClickHouse lifecycle: minimal config generation, foreground server, stop."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from loguru import logger

from . import layout

DEFAULT_HTTP_PORT = int(__import__("os").environ.get("NATIVE_CLICKHOUSE_HTTP_PORT", "8123"))
DEFAULT_TCP_PORT = int(__import__("os").environ.get("NATIVE_CLICKHOUSE_TCP_PORT", "9000"))
DEFAULT_DATABASE = __import__("os").environ.get("NATIVE_CLICKHOUSE_DATABASE", "ai_lubricant_logs")


def data_root() -> Path:
    return layout.data_dir("clickhouse")


def config_path() -> Path:
    path = layout.config_dir() / "clickhouse-config.xml"
    if not path.exists():
        path.write_text(
            "<clickhouse>\n"
            "  <logger>\n"
            "    <level>warning</level>\n"
            "    <console>1</console>\n"
            "  </logger>\n"
            f"  <path>{data_root()}/</path>\n"
            f"  <tmp_path>{data_root()}/tmp/</tmp_path>\n"
            f"  <user_files_path>{data_root()}/user_files/</user_files_path>\n"
            "  <listen_host>127.0.0.1</listen_host>\n"
            f"  <http_port>{DEFAULT_HTTP_PORT}</http_port>\n"
            f"  <tcp_port>{DEFAULT_TCP_PORT}</tcp_port>\n"
            "  <users>\n"
            "    <default>\n"
            "      <password></password>\n"
            "      <networks><ip>::/0</ip></networks>\n"
            "      <profile>default</profile>\n"
            "      <quota>default</quota>\n"
            "      <access_management>1</access_management>\n"
            "    </default>\n"
            "  </users>\n"
            "  <default_profile>default</default_profile>\n"
            "  <default_database>default</default_database>\n"
            "</clickhouse>\n",
            encoding="utf-8",
        )
    return path


def start_command(clickhouse_exe: Path) -> list[str]:
    return [str(clickhouse_exe), "server", f"--config-file={config_path()}"]


async def _probe_http() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEFAULT_HTTP_PORT}/ping", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


async def wait_ready(timeout: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _probe_http():
            return True
        await asyncio.sleep(1.0)
    logger.warning("[native-deps] clickhouse not ready within {}s", int(timeout))
    return False


def ensure_database(clickhouse_exe: Path, name: str = DEFAULT_DATABASE) -> None:
    result = subprocess.run(
        [
            str(clickhouse_exe), "client",
            "--host", "127.0.0.1", "--port", str(DEFAULT_TCP_PORT),
            "--query", f"CREATE DATABASE IF NOT EXISTS {name}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"clickhouse CREATE DATABASE failed: {result.stderr or result.stdout}")


def stop(clickhouse_exe: Path) -> None:
    # ClickHouse honours SIGTERM; pg_ctl-style tool is not available for bare
    # binaries, so the owning supervisor is responsible for terminating it.
    result = subprocess.run(
        [str(clickhouse_exe), "client", "--host", "127.0.0.1", "--port", str(DEFAULT_TCP_PORT),
         "--query", "SYSTEM SHUTDOWN"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.warning("[native-deps] clickhouse SYSTEM SHUTDOWN: {}",
                       (result.stderr or result.stdout).strip())
