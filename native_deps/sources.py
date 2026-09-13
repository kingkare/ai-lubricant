"""Pinned native dependency assets.

URLs and hashes are intentionally overrideable: upstream release URLs move, and
CN/air-gapped deployments need a private mirror. A missing hash is rejected for
network downloads rather than silently accepting an unverified binary.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str = ""
    archive: str = "bare"  # bare | zip | tar.gz | tar.xz
    executable: str = ""
    root: str = ""


# Versions are deliberately centralized. Point releases can be overridden by
# the *_DOWNLOAD_URL / *_SHA256 environment variables without a code release.
POSTGRES_VERSION = os.environ.get("NATIVE_POSTGRES_VERSION", "16.4")
REDIS_VERSION = os.environ.get("NATIVE_REDIS_VERSION", "7.2.5")
CLICKHOUSE_VERSION = os.environ.get("NATIVE_CLICKHOUSE_VERSION", "24.8.6.70")


def platform_tokens() -> tuple[str, str, str]:
    system = platform.system().lower()
    if system.startswith("win"):
        os_token, exe = "windows", ".exe"
    elif system == "darwin":
        os_token, exe = "darwin", ""
    else:
        os_token, exe = "linux", ""
    machine = (platform.machine() or "").lower()
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "386"
    else:
        arch = "amd64"
    return os_token, arch, exe


def _override(kind: str, default: Asset) -> Asset:
    key = kind.upper()
    url = os.environ.get(f"NATIVE_{key}_DOWNLOAD_URL", "").strip() or default.url
    digest = os.environ.get(f"NATIVE_{key}_SHA256", "").strip() or default.sha256
    return Asset(url=url, sha256=digest, archive=default.archive,
                 executable=default.executable, root=default.root)


def asset(kind: str) -> Asset:
    os_token, arch, exe = platform_tokens()
    # These defaults are intentionally explicit and version-pinned. Operators
    # should set NATIVE_*_DOWNLOAD_URL + NATIVE_*_SHA256 for their mirror; the
    # manifest must never silently accept an unverified arbitrary download.
    if kind == "postgres":
        if os_token == "windows":
            default = Asset(
                f"https://get.enterprisedb.com/postgresql/postgresql-{POSTGRES_VERSION}-1-windows-x64-binaries.zip",
                archive="zip", executable="bin/postgres.exe", root="pgsql",
            )
        elif os_token == "linux":
            default = Asset(
                f"https://ftp.postgresql.org/pub/postgresql/v{POSTGRES_VERSION.rsplit('.', 1)[0]}/postgresql-{POSTGRES_VERSION}-linux-x64-binaries.tar.gz",
                archive="tar.gz", executable="bin/postgres", root="",
            )
        else:
            default = Asset(
                f"https://ftp.postgresql.org/pub/postgresql/v{POSTGRES_VERSION.rsplit('.', 1)[0]}/postgresql-{POSTGRES_VERSION}-osx-binaries.tar.gz",
                archive="tar.gz", executable="bin/postgres", root="",
            )
    elif kind == "redis":
        # Redis has no official Windows binary. This URL is a configurable
        # community-build slot; reject it without a digest at download time.
        if os_token == "windows":
            url = os.environ.get("NATIVE_REDIS_DOWNLOAD_URL", "").strip()
            default = Asset(url, archive="zip", executable="redis-server.exe")
        else:
            default = Asset(
                f"https://download.redis.io/releases/redis-{REDIS_VERSION}.tar.gz",
                archive="tar.gz", executable="src/redis-server",
            )
    elif kind == "clickhouse":
        base = f"https://packages.clickhouse.com/tgz/stable/clickhouse-common-static-{CLICKHOUSE_VERSION}"
        if os_token == "windows":
            default = Asset(base + "-amd64.zip", archive="zip", executable="clickhouse.exe")
        else:
            default = Asset(base + "-amd64.tgz", archive="tar.gz", executable="usr/bin/clickhouse")
    else:
        raise ValueError(f"unsupported native dependency: {kind}")
    return _override(kind, default)
