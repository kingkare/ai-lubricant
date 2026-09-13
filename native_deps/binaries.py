"""Download, verify and unpack native dependency assets."""
from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

import aiohttp
from loguru import logger

from . import layout
from .sources import Asset, asset


class DownloadError(RuntimeError):
    """A dependency could not be downloaded, verified or unpacked."""


_TIMEOUT = aiohttp.ClientTimeout(total=float(os.environ.get("NATIVE_DEPS_DOWNLOAD_TIMEOUT", "900")))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _proxy() -> str | None:
    return os.environ.get("NATIVE_DEPS_PROXY", "").strip() or None


async def _download(url: str, dest: Path) -> None:
    proxy = _proxy()
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        try:
            async with session.get(url, proxy=proxy) as response:
                if response.status != 200:
                    raise DownloadError(f"download {url} returned HTTP {response.status}")
                with dest.open("wb") as fh:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        fh.write(chunk)
        except aiohttp.ClientError as exc:
            raise DownloadError(f"download {url} failed: {exc}") from exc


def _safe_member(root: Path, name: str) -> Path:
    dest = (root / name).resolve()
    if dest != root.resolve() and not str(dest).startswith(str(root.resolve()) + os.sep):
        raise DownloadError(f"archive member escapes destination: {name}")
    return dest


def _extract(archive_path: Path, spec: Asset, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if spec.archive == "bare":
        target = destination / (Path(spec.executable).name or "binary")
        shutil.copyfile(archive_path, target)
        return
    if spec.archive in ("tar.gz", "tar.xz"):
        mode = "r:gz" if spec.archive == "tar.gz" else "r:xz"
        with tarfile.open(archive_path, mode) as archive:
            for member in archive.getmembers():
                # Reject links entirely; dependency archives do not need them and
                # allowing links makes extraction unnecessarily dangerous.
                if member.issym() or member.islnk():
                    continue
                _safe_member(destination, member.name)
            archive.extractall(destination)
        return
    if spec.archive == "zip":
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                _safe_member(destination, name)
            archive.extractall(destination)
        return
    raise DownloadError(f"unsupported archive format: {spec.archive}")


def _find_executable(root: Path, spec: Asset, kind: str) -> Path:
    candidates = []
    if spec.executable:
        candidates.append(root / spec.executable)
        candidates.append(root / spec.root / spec.executable)
        candidates.append(root / spec.root / Path(spec.executable).name)
    names = {
        "postgres": ("postgres.exe", "postgres"),
        "redis": ("redis-server.exe", "redis-server"),
        "clickhouse": ("clickhouse.exe", "clickhouse"),
    }[kind]
    candidates.extend(root / name for name in names)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in names:
        found = next(root.rglob(name), None)
        if found:
            return found
    raise DownloadError(f"{kind} archive contains no expected executable")


async def ensure_dep(kind: str) -> Path:
    """Return an executable path, using PATH/cache/download in that order."""
    spec = asset(kind)
    names = {
        "postgres": ("postgres.exe", "postgres"),
        "redis": ("redis-server.exe", "redis-server"),
        "clickhouse": ("clickhouse.exe", "clickhouse"),
    }[kind]
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    target_dir = layout.bin_dir(kind)
    marker = target_dir / ".source-url"
    cached = target_dir / Path(spec.executable).name
    if cached.is_file() and (not spec.sha256 or _sha256(cached) == spec.sha256):
        return cached
    if not spec.url:
        raise DownloadError(
            f"no default {kind} binary is available for {platform.system()}; "
            f"set NATIVE_{kind.upper()}_DOWNLOAD_URL and NATIVE_{kind.upper()}_SHA256"
        )
    if not spec.sha256:
        raise DownloadError(
            f"{kind} asset has no pinned SHA-256; set NATIVE_{kind.upper()}_SHA256 "
            "before allowing an unverified download"
        )

    work = Path(tempfile.mkdtemp(prefix=f"native-{kind}-", dir=layout.root()))
    archive_path = work / "asset.download"
    stage = work / "stage"
    try:
        logger.info("[native-deps] downloading {} from {}", kind, spec.url)
        await _download(spec.url, archive_path)
        actual = _sha256(archive_path)
        if actual.lower() != spec.sha256.lower():
            raise DownloadError(f"{kind} SHA-256 mismatch: {actual} != {spec.sha256}")
        _extract(archive_path, spec, stage)
        executable = _find_executable(stage, spec, kind)
        # Preserve a self-contained extracted tree for multi-file PG archives;
        # install it atomically by replacing the versioned kind directory.
        new_dir = target_dir.with_name(target_dir.name + ".new")
        shutil.rmtree(new_dir, ignore_errors=True)
        shutil.copytree(stage, new_dir)
        final = _find_executable(new_dir, spec, kind)
        if os.name != "nt":
            final.chmod(final.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        shutil.rmtree(target_dir, ignore_errors=True)
        os.replace(new_dir, target_dir)
        marker.write_text(spec.url + "\n", encoding="utf-8")
        return _find_executable(target_dir, spec, kind)
    finally:
        shutil.rmtree(work, ignore_errors=True)
