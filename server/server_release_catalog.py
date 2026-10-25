"""服务端（数据服务/控制面/Tunnel Runtime）发行版本目录。

与节点/移动端/设备控制三条线**完全同构**：publish 打 tag 时把版本信息写成
``server-releases/version.json`` 提交进市场仓；本模块定时从市场仓拉取并缓存到
PostgreSQL，让升级卡片不必每次都打 GitHub。

``version.json`` 形状（写入侧 publish_github.sh 产出）::

    {
      "schema": "ai-lubricant.server-release/v1",
      "version": "v260912",
      "version_notes": "...",
      "release_tag": "v260912",
      "download_url": "https://github.com/wuxin-gh/ai-lubricant/releases/download/v260912/ai-lubricant-v260912.tar.gz",
      "digest": "sha256:...",
      "size_bytes": 25165824,
      "updated_at": "..."
    }

**发布即推送**：打了 tag 就等于发布（用户侧下次轮询就能看到），中间没有"登记"
这道人工闸门——与节点/移动端同款。要"发了但不推给用户"时用 draft 语义（不写
version.json）。

升级包放在**主仓自己的 Release**（不另建发行仓）：包是主仓产物，放主仓语义最顺。
"""
from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import json
import os
from typing import Any

import aiohttp
from loguru import logger

from db import PostgresClient
from user_platform.marketplace import config as mp_config

SNAPSHOT_KEY = "server_release_snapshot"
MAX_FILE_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 20
DEFAULT_SYNC_SECONDS = 600

_lock = asyncio.Lock()
_snapshot: dict[str, Any] = {
    "version": "",
    "version_notes": "",
    "release_tag": "",
    "download_url": "",
    "digest": "",
    "size_bytes": 0,
    "updated_at": "",
    "stale": True,
}


async def load_snapshot() -> None:
    global _snapshot
    try:
        stored = await PostgresClient.get_config(SNAPSHOT_KEY)
    except Exception as exc:
        logger.warning("[server-release] load snapshot failed: {}", exc)
        return
    if isinstance(stored, dict) and stored.get("version"):
        _snapshot = copy.deepcopy(stored)
        logger.info(
            "[server-release] loaded snapshot version={}", _snapshot.get("version") or "(empty)"
        )


async def get_latest_release() -> dict[str, Any]:
    """返回当前缓存的最新版本（或空骨架）。永远不抛。"""
    return copy.deepcopy(_snapshot)


async def apply_release(release: dict[str, Any], *, publish: bool = True) -> dict[str, Any]:
    """立即应用写侧刚生成的版本信息（publish 脚本同进程场景）。

    空 payload（version 为空）= 清空快照，撤回最新版后升级卡片不再推荐它。
    """
    global _snapshot
    version = str(release.get("version") or "").strip()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    next_snapshot = {
        "version": version,
        "version_notes": str(release.get("version_notes") or ""),
        "release_tag": str(release.get("release_tag") or version),
        "download_url": str(release.get("download_url") or ""),
        "digest": str(release.get("digest") or ""),
        "size_bytes": int(release.get("size_bytes") or 0),
        "updated_at": release.get("updated_at") or now,
        "stale": False,
        "fetched_at": now,
    }
    async with _lock:
        await PostgresClient.set_config(SNAPSHOT_KEY, next_snapshot)
        _snapshot = copy.deepcopy(next_snapshot)
    if publish:
        import runtime_sync

        await runtime_sync.publish(runtime_sync.EVENT_SERVER_RELEASE, "__all__")
    logger.info("[server-release] applied version={}", _snapshot.get("version") or "(empty)")
    return copy.deepcopy(_snapshot)


def _raw_url() -> str:
    from user_platform.marketplace import urls

    return urls.consumer_raw_url("server-releases/version.json")


def _clean_version(value: Any) -> str:
    """归一版本号：去前导 v/V、空白，统一成裸串用于比较。"""
    return str(value or "").strip().lstrip("vV").strip()


def server_upgrade_status(
    latest: dict[str, Any], *, current_version: str
) -> dict[str, Any]:
    """计算服务端当前/最新/是否可升级。

    版本号是日期串（``vYYMMDD``），字典序即时间序，字符串比较即可。
    """
    current = _clean_version(current_version)
    latest_version = _clean_version(latest.get("version") or "")
    needs = bool(latest_version) and current != latest_version
    return {
        "stale": bool(latest.get("stale")),
        "current": current,
        "latest": latest_version,
        "release_tag": str(latest.get("release_tag") or ""),
        "version_notes": str(latest.get("version_notes") or ""),
        "download_url": str(latest.get("download_url") or ""),
        "digest": str(latest.get("digest") or ""),
        "size_bytes": int(latest.get("size_bytes") or 0),
        "needs_upgrade": needs,
        "updated_at": str(latest.get("updated_at") or ""),
    }


def current_version() -> str:
    """本进程运行版本：读仓库根 ``VERSION`` 文件，没有则回落 ``dev``。

    ``VERSION`` 由 publish 打升级包时写入（内容 = git tag，如 ``v260912``），
    只存在于发布产物里、不在 master 工作区。所以：
      - 部署机解压升级包 → 有该文件 → 显示真实版本
      - 开发机（master）→ 无该文件 → 显示 ``dev``（诚实：确实不是发布版）

    曾经用环境变量 ``APP_VERSION``，问题是「别人告诉进程的」——谁忘了传就撒谎
    （生产曾显示 dev），且多容器形态下要靠外部注入对齐。文件随代码走，不会撒谎。
    """
    from project_paths import version_file

    try:
        text = version_file().read_text(encoding="utf-8").strip()
        if text:
            return text
    except Exception:  # noqa: BLE001 - 文件缺失/不可读都回落 dev，不影响启动
        pass
    return "dev"


async def _render_from_store() -> dict[str, Any]:
    """writable 部署：store 是真相源，从 store 渲染 version.json。

    与 node 线同款：登记条目落在 ``marketplace_items``，本部署直接从 store 渲染，
    不读仓库 raw（避免 raw 传播延迟把刚登记的数据冲掉）。
    """
    import marketplace_store as store
    from user_platform.marketplace.render import render_server_release

    if not await store.is_populated():
        raise RuntimeError("store 未填充（bootstrap 前）")
    manifests = await store.list_manifests("server-versions", include_hidden=True)
    payload, errors = render_server_release(manifests)
    if errors:
        raise RuntimeError("version.json 渲染失败: " + "; ".join(errors))
    return payload


async def _fetch_from_repo() -> dict[str, Any]:
    """只读部署：从市场仓 raw 拉 version.json（publish 打 tag 时写入的那份）。"""
    from providers.proxy_manager import get_proxy_manager

    resp = await get_proxy_manager().request(
        url=_raw_url(),
        method="GET",
        headers={"User-Agent": "ai-lubricant-server-release"},
        timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS),
        proxy_config_id=mp_config.settings.proxy_id or None,
    )
    if resp.status != 200:
        raise RuntimeError(f"version.json returned HTTP {resp.status}")
    raw = await resp.read()
    if len(raw) > MAX_FILE_BYTES:
        raise RuntimeError(f"version.json exceeds {MAX_FILE_BYTES} bytes")
    return json.loads(raw.decode("utf-8"))


async def refresh(*, publish: bool = True) -> dict[str, Any]:
    """刷新版本快照：writable 部署从 store 渲染，只读部署从市场仓 raw 拉。

    与 node 线完全同构。
    """
    global _snapshot
    settings = mp_config.consumer_settings
    if not settings.enabled or "server-versions" not in settings.modules:
        return {"ok": False, "error": "服务端版本源未启用", **await get_latest_release()}
    async with _lock:
        try:
            data = (
                await _render_from_store()
                if mp_config.settings.writable
                else await _fetch_from_repo()
            )
            previous_version = str(_snapshot.get("version") or "")
            next_version = str(data.get("version") or "")
            now = _dt.datetime.now(_dt.timezone.utc).isoformat()
            next_snapshot = {
                "version": next_version,
                "version_notes": str(data.get("version_notes") or ""),
                "release_tag": str(data.get("release_tag") or next_version),
                "download_url": str(data.get("download_url") or ""),
                "digest": str(data.get("digest") or ""),
                "size_bytes": int(data.get("size_bytes") or 0),
                "updated_at": data.get("updated_at") or now,
                "stale": False,
                "fetched_at": now,
            }
            # 先更新内存：即使 PG 写失败（本地无 DB），消费侧仍能拿到本次数据
            _snapshot = copy.deepcopy(next_snapshot)
            try:
                await PostgresClient.set_config(SNAPSHOT_KEY, next_snapshot)
            except Exception as exc:
                logger.debug("[server-release] set_config failed (in-memory updated): {}", exc)
            if publish:
                import runtime_sync

                await runtime_sync.publish(runtime_sync.EVENT_SERVER_RELEASE, "__all__")
            if next_version and previous_version and next_version != previous_version:
                from user_platform.notify_core import emit_notification_background

                emit_notification_background(
                    "server.new_version",
                    params={
                        "version": next_version,
                        "previous_version": previous_version,
                        "version_notes": str(data.get("version_notes") or ""),
                    },
                    owner_type="platform",
                    severity="info",
                    source="server_release_catalog",
                    message=f"服务端版本已从 {previous_version} 更新为 {next_version}",
                    dedupe_key=f"server.new_version:{next_version}",
                    dedupe_window_seconds=86400,
                )
            logger.info("[server-release] synced version={}", next_version or "(empty)")
            return {"ok": True, **copy.deepcopy(_snapshot)}
        except Exception as exc:
            _snapshot["stale"] = True
            logger.warning("[server-release] refresh failed; keeping last snapshot: {}", exc)
            return {"ok": False, "error": str(exc), **copy.deepcopy(_snapshot)}


async def reload_from_db() -> None:
    await load_snapshot()


async def sync_loop() -> None:
    interval = max(60, int(os.getenv("SERVER_RELEASE_SYNC_INTERVAL", str(DEFAULT_SYNC_SECONDS))))
    while True:
        try:
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[server-release] sync cycle failed: {}", exc)
        await asyncio.sleep(interval)
