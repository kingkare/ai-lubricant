"""模型元数据自动导入。

扫描「有渠道行但缺元数据」的运行时模型，按来源顺序逐个尝试**精确匹配**目录条目
（llm-metadata → OpenRouter → models.dev，可选再试各渠道上游），命中即批量写入
``model_groups(kind='real')``。未命中一律不猜，留给管理端手工导入。

触发方式两种，共用同一份实现：
- 后台定时循环 ``sync_loop()``：仅当主配置 ``model_metadata_auto_import.enabled`` 为真
  时执行；未开启时空转复查配置（便于热开启），不抓取、不写库。
- 管理端「立即执行」：POST ``/model-metadata/auto-import/run``，不受开关限制。

匹配口径（用户口径：仅精确匹配）：
- 平台 model_id 与候选条目的 id / id 末段 / name 做**归一化相等**比较：先小写去空白，
  再忽略 ``- _ . / : @ #`` 等分隔符。归一化后相等才算命中，不做子串/编辑距离等模糊匹配。
- 同一条目被多个 key 命中时按索引顺序取第一个；不同候选撞同一 key 时先到先得并记日志。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

import model_metadata as _mm

# ── 配置（存主配置 blob 的 CONFIG_KEY 键，与市场源配置同表）──
CONFIG_KEY = "model_metadata_auto_import"
DEFAULT_INTERVAL_MINUTES = 360
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 10080  # 7 天

# 目录来源顺序：挨个尝试，命中即停（已命中该模型的来源不再重复命中）。
CATALOG_SOURCES = ("llm-metadata", "openrouter", "modelsdev")
SOURCE_LABELS = {
    "llm-metadata": "llm-metadata",
    "openrouter": "OpenRouter",
    "modelsdev": "models.dev",
    "channels": "渠道上游",
}

# 归一化时忽略的分隔符：这些字符差异不算差异（claude-sonnet-4.5 == claude_sonnet_4_5）。
_SEPARATORS = " -_./:@#|+"

_progress: dict[str, Any] = {"running": False}
_last_result: dict[str, Any] | None = None
_lock = asyncio.Lock()
# 手动「立即执行」触发的后台任务句柄（定时循环走 sync_loop 直接 await run_once，不经这里）。
_background_task: asyncio.Task | None = None


# ==================== 匹配 ====================


def match_keys(name: Any) -> set[str]:
    """一个名字的全部匹配 key（归一化后）。空名返回空集合。"""
    text = str(name or "").strip().lower()
    if not text:
        return set()
    keys = {text}
    squeezed = "".join(ch for ch in text if ch not in _SEPARATORS)
    if squeezed:
        keys.add(squeezed)
    # 带路径的源 id（如 anthropic/claude-sonnet-4.5）额外提供末段 key——
    # 仍是归一化相等比较，不是子串匹配。
    if "/" in text:
        tail = text.rsplit("/", 1)[-1]
        if tail:
            keys.add(tail)
            tail_squeezed = "".join(ch for ch in tail if ch not in _SEPARATORS)
            if tail_squeezed:
                keys.add(tail_squeezed)
    return keys


class _Index:
    """归一化 key → 候选条目。先到先得，撞 key 只记日志。"""

    def __init__(self, source: str) -> None:
        self.source = source
        self.by_key: dict[str, dict] = {}
        self.count = 0

    def add(self, item: dict, *, keys: list[str], model_id: str, provider: str) -> None:
        entry = {"source": self.source, "model_id": model_id, "provider": provider, "metadata": item}
        self.count += 1
        for raw in keys:
            for key in match_keys(raw):
                if key in self.by_key:
                    if self.by_key[key]["model_id"] != model_id:
                        logger.debug(
                            "[metadata-auto-import] {} key 冲突 {}：{} 保留，忽略 {}",
                            self.source, key, self.by_key[key]["model_id"], model_id,
                        )
                    continue
                self.by_key[key] = entry

    def lookup(self, model_id: str) -> dict | None:
        for key in sorted(match_keys(model_id), key=len, reverse=True):
            entry = self.by_key.get(key)
            if entry:
                return entry
        return None


# ==================== 来源加载 ====================


async def _load_catalog_index(source: str) -> _Index:
    """按来源拉目录并建索引。单源失败由调用方兜住，不拖垮其它源。"""
    index = _Index(source)
    if source == "llm-metadata":
        raw = await _mm.fetch_llm_metadata()
        for entry in _mm.flatten_llm_metadata(raw):
            item = _mm.normalize_llm_metadata_model(entry["provider"], entry["item"])
            index.add(item, keys=[entry["id"], entry["item"].get("name")],
                      model_id=entry["id"], provider=entry["provider"])
    elif source == "openrouter":
        for row in await _mm.fetch_openrouter_models():
            if not isinstance(row, dict) or not row.get("id"):
                continue
            item = _mm.normalize_openrouter_model(row)
            index.add(item, keys=[row["id"], row.get("name")], model_id=row["id"], provider="openrouter")
    elif source == "modelsdev":
        raw = await _mm.fetch_modelsdev()
        for entry in _mm.flatten_modelsdev(raw):
            item = _mm.normalize_modelsdev_model(entry["provider"], entry["item"])
            index.add(item, keys=[entry["id"], entry["item"].get("name")],
                      model_id=entry["id"], provider=entry["provider"])
    else:
        raise ValueError(f"未知目录来源: {source}")
    return index


async def _load_channel_indices(providers: list[str]) -> list[_Index]:
    """逐个渠道拉上游模型列表建索引。单个渠道失败只跳过该渠道。"""
    from rate_limiter import ModelClientPool

    indices: list[_Index] = []
    for provider in providers:
        try:
            payload = await ModelClientPool.fetch_provider_upstream_models(provider)
        except Exception as exc:  # noqa: BLE001 — 单渠道不可用不影响其它渠道
            logger.warning("[metadata-auto-import] 渠道 {} 上游模型列表获取失败: {}", provider, exc)
            continue
        index = _Index(f"channels:{provider}")
        for row in payload.get("upstream_models") or []:
            if not isinstance(row, dict):
                continue
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            merged = {**raw}
            if row.get("name") and not merged.get("name"):
                merged["name"] = row["name"]
            item = _channel_metadata(merged, row)
            index.add(
                item,
                keys=[row.get("model_id"), row.get("upstream_model_id"), row.get("name")],
                model_id=str(row.get("model_id") or row.get("upstream_model_id") or ""),
                provider=provider,
            )
        indices.append(index)
    return indices


# 渠道上游 raw 里可能出现的字段名 → 本系统 metadata 字段。只取这些，其余丢弃。
_CHANNEL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "display_name", "id", "title"),
    "owned_by": ("owned_by", "owner", "organization", "author"),
    "object": ("object", "type"),
    "created": ("created", "created_at"),
    "max_context_tokens": (
        "max_context_tokens", "context_length", "context_window",
        "context_window_tokens", "input_token_limit", "max_input_tokens",
    ),
    "max_tokens": (
        "max_tokens", "max_output_tokens", "output_limit",
        "output_token_limit", "max_completion_tokens",
    ),
    "input_modalities": ("input_modalities", "supported_input_modalities"),
    "output_modalities": ("output_modalities", "supported_output_modalities"),
    "multimodal": ("multimodal",),
    "function_calling": ("function_calling", "supports_function_calling", "supports_tool_calls"),
    "auto_thinking": ("auto_thinking", "supports_thinking"),
    "is_thinking": ("is_thinking", "thinking", "reasoning", "supports_reasoning"),
    "auto_search": ("auto_search", "supports_search"),
    "capabilities": ("capabilities",),
    "icon_url": ("icon_url", "icon"),
}


def _channel_metadata(raw: dict, row: dict) -> dict:
    """渠道上游条目 → 本系统 metadata（不含 model_id），与前端导入弹框同口径。"""
    picked: dict[str, Any] = {}
    for field, aliases in _CHANNEL_FIELD_ALIASES.items():
        for alias in aliases:
            value = raw.get(alias)
            if value is not None and value != "":
                picked[field] = value
                break
    if not picked.get("name"):
        picked["name"] = row.get("name") or row.get("model_id") or row.get("upstream_model_id")
    if not picked.get("owned_by"):
        picked["owned_by"] = row.get("provider")
    picked.setdefault("object", "model")
    # 渠道 raw 未必有模态字段；缺失时不编造，留空由前端/调用方决定。
    return {k: v for k, v in picked.items() if v is not None}


# ==================== 主流程 ====================


def _runtime_routes() -> dict[str, list[str]]:
    """运行时模型 → 具备该模型的渠道列表（来自渠道路由快照）。"""
    from rate_limiter import ModelClientPool

    routes: dict[str, list[str]] = {}
    for model_id, rows in ModelClientPool.get_runtime_routes_snapshot().items():
        routes[model_id] = sorted({str(r.get("provider") or "") for r in rows if r.get("provider")})
    return routes


async def run_once(*, run_by: str = "manual") -> dict[str, Any]:
    """跑一次自动导入。已在跑时直接返回，不排队。

    返回 report：``imported`` 写入条数、``matched`` 逐模型命中来源、``unmatched`` 未命中
    的模型 id 列表、``per_source`` 各来源贡献条数、``errors`` 各来源失败原因。
    """
    global _last_result

    if _progress.get("running"):
        return {"ok": False, "skipped": True, "detail": "已有自动导入在运行中"}

    started = datetime.now(timezone.utc).isoformat()
    _progress.update({"running": True, "phase": "scan"})
    logs: list[dict[str, str]] = []

    def _log(phase: str, detail: str) -> None:
        logs.append({"ts": datetime.now(timezone.utc).isoformat(), "phase": phase, "detail": detail})

    try:
        async with _lock:
            cfg = await get_config_async()
            runtime_models = _runtime_routes()
            existing = {
                str(m.get("model_id") or m.get("id"))
                for m in (await _mm.list_metadata_from_db_async()).get("models") or []
            }
            pending = sorted(mid for mid in runtime_models if mid and mid not in existing)
            _log("scan", f"运行时模型 {len(runtime_models)}，其中缺元数据 {len(pending)}")
            if not pending:
                result = _finish(
                    started, imported=0, matched={}, per_source={}, errors=[], logs=logs,
                    detail="没有缺元数据的模型", pending_count=0,
                )
                return result

            items: list[dict] = []
            matched: dict[str, dict] = {}
            per_source: dict[str, int] = {}
            errors: list[str] = []

            sources: list[str] = list(CATALOG_SOURCES)
            if cfg.get("include_channels"):
                sources.append("channels")

            for source in sources:
                remaining = [mid for mid in pending if mid not in matched]
                if not remaining:
                    break
                _progress.update({"phase": source})
                try:
                    if source == "channels":
                        providers = sorted({
                            p
                            for mid in remaining
                            for p in runtime_models.get(mid, [])
                            if p
                        })
                        if not providers:
                            continue
                        indices = await _load_channel_indices(providers)
                    else:
                        indices = [await _load_catalog_index(source)]
                except Exception as exc:  # noqa: BLE001 — 单源失败继续下一源
                    errors.append(f"{SOURCE_LABELS.get(source, source)}: {exc}")
                    _log("fail", f"{SOURCE_LABELS.get(source, source)} 拉取失败: {str(exc)[:150]}")
                    logger.warning("[metadata-auto-import] {} 拉取失败: {}", source, exc)
                    continue

                hit = 0
                for model_id in remaining:
                    for index in indices:
                        entry = index.lookup(model_id)
                        if not entry:
                            continue
                        metadata = dict(entry["metadata"])
                        metadata.pop("id", None)
                        metadata["model_id"] = model_id
                        items.append(metadata)
                        matched[model_id] = {
                            "source": entry["source"],
                            "source_model_id": entry["model_id"],
                            "provider": entry["provider"],
                        }
                        hit += 1
                        break
                if hit:
                    per_source[SOURCE_LABELS.get(source, source)] = hit
                _log("match", f"{SOURCE_LABELS.get(source, source)} 命中 {hit} / 待匹配 {len(remaining)}")

            imported = 0
            if items:
                _progress.update({"phase": "write"})
                try:
                    imported = await _mm.bulk_import_async(items)
                    from admin import _after_metadata_write

                    await _after_metadata_write("__all__")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"写入失败: {exc}")
                    _log("fail", f"写入失败: {str(exc)[:150]}")
                    logger.warning("[metadata-auto-import] 写入失败: {}", exc)

            unmatched = [mid for mid in pending if mid not in matched]
            detail = f"缺元数据 {len(pending)}，命中 {len(matched)}，写入 {imported}"
            if unmatched:
                detail += f"，未命中 {len(unmatched)}"
            if errors:
                detail += f"，{len(errors)} 项失败"
            _log("done", detail)
            return _finish(
                started, imported=imported, matched=matched, per_source=per_source,
                errors=errors, logs=logs, detail=detail, pending_count=len(pending),
                unmatched=unmatched, run_by=run_by,
            )
    finally:
        _progress.update({"running": False, "phase": ""})


def _finish(
    started: str,
    *,
    imported: int,
    matched: dict[str, dict],
    per_source: dict[str, int],
    errors: list[str],
    logs: list[dict[str, str]],
    detail: str,
    pending_count: int,
    unmatched: list[str] | None = None,
    run_by: str = "manual",
) -> dict[str, Any]:
    global _last_result

    result = {
        "ok": not errors,
        "ran_at": started,
        "run_by": run_by,
        "detail": detail,
        "pending_count": pending_count,
        "imported": imported,
        "matched_count": len(matched),
        "per_source": per_source,
        "matched": matched,
        "unmatched": (unmatched or [])[:200],
        "errors": errors[:20],
        "logs": logs[-50:],
    }
    _last_result = result
    _remember_run(started, result)
    return result


def _remember_run(ran_at: str, result: dict[str, Any]) -> None:
    """把最近一次执行时间与摘要写进主配置 blob，重启后管理端仍能回显。"""
    try:
        from config import CONFIG_STORE

        blob = CONFIG_STORE.read_main()
        if not isinstance(blob, dict):
            return
        section = dict(blob.get(CONFIG_KEY) or {})
        section["last_run_at"] = ran_at
        section["last_detail"] = str(result.get("detail") or "")[:300]
        section["last_imported"] = int(result.get("imported") or 0)
        blob[CONFIG_KEY] = section
        CONFIG_STORE.write_main(blob)
    except Exception as exc:  # noqa: BLE001 — 记录失败不影响结果
        logger.debug("[metadata-auto-import] 记录执行摘要失败: {}", exc)


# ==================== 配置 ====================


def _normalize_config(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    try:
        interval = int(data.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MINUTES
    return {
        "enabled": data.get("enabled") is True,
        "interval_minutes": max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, interval)),
        "include_channels": data.get("include_channels") is True,
        "last_run_at": str(data.get("last_run_at") or ""),
        "last_detail": str(data.get("last_detail") or ""),
        "last_imported": int(data.get("last_imported") or 0),
    }


def get_config() -> dict[str, Any]:
    try:
        from config import CONFIG_STORE

        blob = CONFIG_STORE.read_main()
    except Exception:
        blob = {}
    section = blob.get(CONFIG_KEY) if isinstance(blob, dict) else None
    return _normalize_config(section)


async def get_config_async() -> dict[str, Any]:
    return get_config()


async def update_config(patch: dict[str, Any]) -> dict[str, Any]:
    """按字段合并保存（未传的字段不动）。"""
    current = get_config()
    merged = dict(current)
    if "enabled" in patch:
        merged["enabled"] = patch.get("enabled") is True
    if "interval_minutes" in patch:
        try:
            interval = int(patch.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL_MINUTES
        merged["interval_minutes"] = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, interval))
    if "include_channels" in patch:
        merged["include_channels"] = patch.get("include_channels") is True

    from config import CONFIG_STORE

    blob = await _read_main_async()
    section = dict(blob.get(CONFIG_KEY) or {})
    section.update({k: merged[k] for k in ("enabled", "interval_minutes", "include_channels")})
    blob[CONFIG_KEY] = section
    if hasattr(CONFIG_STORE, "write_main_async"):
        await CONFIG_STORE.write_main_async(blob)
    else:
        CONFIG_STORE.write_main(blob)
    return get_config()


async def _read_main_async() -> dict[str, Any]:
    from config import CONFIG_STORE

    if hasattr(CONFIG_STORE, "read_main_async"):
        data = await CONFIG_STORE.read_main_async()
    else:
        data = CONFIG_STORE.read_main()
    return data if isinstance(data, dict) else {}


def public_state() -> dict[str, Any]:
    """管理端读接口形态：配置 + 最近一次执行结果（内存优先，重启后回落持久化摘要）。"""
    cfg = get_config()
    return {
        **cfg,
        "running": bool(_progress.get("running")),
        "phase": str(_progress.get("phase") or ""),
        "sources": list(CATALOG_SOURCES),
        "last_result": _last_result,
    }


def trigger_background_run() -> dict[str, Any]:
    """手动「立即执行」：后台起任务跑 run_once，请求立即返回。

    已在跑（定时或手动）时不重复起，直接返回当前进度。返回的 ``accepted`` 告诉
    前端是新起的还是复用的已有执行。
    """
    global _background_task

    if _progress.get("running"):
        return {"ok": True, "accepted": False, **public_state()}
    if _background_task is not None and not _background_task.done():
        return {"ok": True, "accepted": False, **public_state()}

    async def _runner() -> None:
        try:
            await run_once(run_by="manual")
        except Exception as exc:  # noqa: BLE001 — 后台异常只记日志，不炸事件循环
            logger.warning("[metadata-auto-import] 后台执行失败: {}", exc)

    _background_task = asyncio.ensure_future(_runner())
    return {"ok": True, "accepted": True, **public_state()}


# ==================== 后台循环 ====================


async def sync_loop() -> None:
    """后台定时自动导入。开关关闭时空转（每 5 分钟复查配置，便于热开启）。

    与其它 sync_loop 同款 asyncio 后台循环（不是 cron）：每 ``interval_minutes``
    跑一次 ``run_once``；同轮已在跑由 ``_progress.running`` 挡掉，不并发。
    """
    while True:
        try:
            cfg = await get_config_async()
            if cfg["enabled"]:
                await run_once(run_by="schedule")
                await asyncio.sleep(max(MIN_INTERVAL_MINUTES, cfg["interval_minutes"]) * 60)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 循环不能因单次失败退出
            logger.warning("[metadata-auto-import] sync_loop error: {}", exc)
        await asyncio.sleep(300)
