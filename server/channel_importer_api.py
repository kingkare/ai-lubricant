"""外部供应商框架导入：管理端路由（预览 + 提交）。

职责边界：本模块只做「取数 → 归一 → 建/并渠道」的编排，翻译逻辑在
``channel_importers/`` 适配器里，建渠道落在 ``admin._create_provider_from_config``。

**提交时客户端重发源输入、服务端重新派生**（不做服务端预览缓存）：多实例部署下
进程内缓存不共享，而缓存密钥又要跨实例复制。重派生是无状态的，且保证
「预览所见 == 提交所得」。代价是密钥传两次——在已鉴权的 admin 通道上可接受。
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from loguru import logger

import admin as admin_mod
from channel_importers import (
    ChannelImportError,
    FetchRequest,
    PasteRequest,
    describe_importers,
    get_importer,
)
from channel_importers.payload import build_create_payload, local_provider_name, serialize_candidate

router = APIRouter(prefix="/admin", tags=["channel-importer"])

# 一次最多提交多少渠道：每个渠道都要写库 + 载入运行池 + 批量写模型，多了就是慢请求。
MAX_SELECTION = 50

# 允许客户端覆盖的候选字段白名单；其余一律服务端派生，避免前端塞任意配置进库。
_OVERRIDABLE_FIELDS = (
    "remark", "base_url", "enabled", "tags", "models",
    "account_weight", "chat_protocols", "timeout",
)

_CONFLICT_MODES = ("skip", "overwrite", "merge_accounts")


def _clean(value: Any) -> str:
    return str(value or "").strip()


async def _scan(payload: dict):
    """按请求模式取数：fetch 走远端 API，paste 走本地解析。"""
    source = _clean(payload.get("source")) or "new-api"
    try:
        importer = get_importer(source)
    except ChannelImportError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc

    mode = _clean(payload.get("mode")) or "paste"
    try:
        if mode == "fetch":
            if not importer.supports_api_fetch:
                raise HTTPException(status_code=400, detail=f"{importer.display_name} 不支持 API 拉取")
            return await importer.fetch(FetchRequest(
                base_url=_clean(payload.get("base_url")),
                token=_clean(payload.get("token")),
                user_id=_clean(payload.get("user_id")),
                proxy_config_id=_clean(payload.get("proxy_config_id")),
                page_size=payload.get("page_size") or 100,
                max_pages=payload.get("max_pages") or 50,
                include_disabled=payload.get("include_disabled", True) is not False,
            ))
        if not importer.supports_paste:
            raise HTTPException(status_code=400, detail=f"{importer.display_name} 不支持粘贴导入")
        return importer.parse(PasteRequest(
            text=str(payload.get("text") or ""),
            format=_clean(payload.get("format")) or "auto",
        ))
    except ChannelImportError as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc


async def _conflicts_by_base_url() -> dict[str, dict]:
    """现有渠道的 base_url → {provider_name, remark} 索引，用于冲突检测。

    口径与 admin._detect_quick_custom_provider 的 duplicate_providers 一致。
    """
    index: dict[str, dict] = {}
    try:
        providers = await admin_mod.config.Config.get_providers()
    except Exception as exc:  # noqa: BLE001 - 冲突检测失败不该阻断导入
        logger.warning("[channel-importer] read providers for conflict check failed: {}", exc)
        return index
    for name, cfg in (providers or {}).items():
        key = admin_mod._normalize_base_url_for_compare((cfg or {}).get("base_url"))
        if key and key not in index:
            index[key] = {"provider_name": name, "remark": (cfg or {}).get("remark") or ""}
    return index


@router.get("/channel-importers")
async def list_channel_importers(token: str = Header(None, alias="Authorization")):
    """可用的外部供应商框架清单，供前端下拉。"""
    await admin_mod._require_admin(token)
    return {"items": describe_importers()}


@router.post("/channel-importers/preview")
async def preview_channel_import(data: dict, token: str = Header(None, alias="Authorization")):
    """干跑：拉取/解析并归一成候选清单，**零写入**。"""
    await admin_mod._require_admin(token)
    payload = data or {}
    scan = await _scan(payload)
    add_source_tag = payload.get("add_source_tag") is True
    source = _clean(payload.get("source")) or "new-api"
    conflict_index = await _conflicts_by_base_url()

    candidates = []
    for channel in scan.channels:
        if add_source_tag and source not in channel.tags:
            channel.tags = [*channel.tags, source]
        key = admin_mod._normalize_base_url_for_compare(channel.base_url)
        candidates.append(serialize_candidate(
            channel,
            mask_secret=admin_mod._mask_secret,
            conflict=conflict_index.get(key),
        ))

    return {
        "ok": True,
        "source": source,
        "scanned": scan.scanned,
        "fetched": len(candidates),
        "candidates": candidates,
        "dropped": scan.dropped,
        "warnings": scan.warnings,
    }


def _apply_overrides(channel, entry: dict) -> None:
    """把客户端白名单覆盖应用到候选（仅认 _OVERRIDABLE_FIELDS）。"""
    for field in _OVERRIDABLE_FIELDS:
        if field not in entry:
            continue
        value = entry.get(field)
        if field == "enabled":
            channel.enabled = value is not False
        elif field == "account_weight":
            try:
                channel.account_weight = max(1, int(value or 1))
            except (TypeError, ValueError):
                pass
        elif field == "timeout":
            try:
                channel.timeout = max(1, min(3600, int(value or 120)))
            except (TypeError, ValueError):
                pass
        elif field == "tags":
            channel.tags = [str(item).strip() for item in (value or []) if str(item).strip()] \
                if isinstance(value, list) else []
        elif field == "models":
            if isinstance(value, list):
                channel.models = [str(item).strip() for item in value if str(item).strip()]
        elif field == "chat_protocols":
            if isinstance(value, list) and value:
                channel.chat_protocols = [dict(row) for row in value if isinstance(row, dict)]
        else:
            setattr(channel, field, _clean(value))


async def _overwrite_existing(name: str, channel, token: str, *, payload: dict) -> dict:
    """覆盖既有渠道：基础配置窄写 + 账号逐条 upsert。

    基础配置只覆盖导入侧显式声明的字段，既有渠道的其它本地配置保持不变
    （语义对齐 channel_template_service.apply_channel_template）。
    """
    new_payload = build_create_payload(channel, name=name)
    current = await admin_mod._read_provider_base_config(name)
    merged = dict(current)
    for field, value in new_payload.items():
        if field in ("name", "accounts", "models"):
            continue
        merged[field] = value
    merged["channel_config_revision"] = int(merged.get("channel_config_revision") or 0) + 1
    await admin_mod._write_provider_base(name, merged)
    accounts_added = await _upsert_accounts(name, channel, payload)

    cfg = await admin_mod._read_provider_config(name)
    if admin_mod.ModelClientPool.get_provider_pool(name):
        pool = admin_mod.ModelClientPool.get_provider_pool(name)
        if pool.channel is not None:
            pool.channel.apply(admin_mod._provider_extra(name, cfg))
    else:
        await admin_mod._load_provider_runtime(name, cfg)
    await admin_mod._log_operation(
        token, "overwrite_external_channel", "provider", name, None,
        {"source": channel.source, "source_channel_id": channel.external_id, "accounts_added": accounts_added},
    )
    return {"provider_name": name, "accounts": accounts_added}


async def _upsert_accounts(name: str, channel, payload: dict) -> int:
    """把导入账号逐条 upsert 进既有渠道；同名账号跳过，返回新增/更新条数。"""
    cfg = await admin_mod._read_provider_config(name)
    existing = {str(acc.get("username") or "") for acc in (cfg.get("accounts") or [])}
    added = 0
    for account in build_create_payload(channel, name=name).get("accounts") or []:
        username = str(account.get("username") or "")
        if not username:
            continue
        if username in existing:
            # 用户名撞车：顺延到 key-N，绝不覆盖既有账号的凭据。
            index = 1
            while f"{username}-{index}" in existing:
                index += 1
            username = f"{username}-{index}"
            account = {**account, "username": username}
        account = admin_mod._normalize_account_proxy_ref(account, admin_mod._read_proxies())
        await admin_mod._persist_account(name, account)
        existing.add(username)
        added += 1
    return added


async def _merge_accounts(name: str, channel, token: str, *, payload: dict) -> dict:
    """只把账号追加到既有渠道，不碰渠道基础配置（窄写）。"""
    added = await _upsert_accounts(name, channel, payload)
    cfg = await admin_mod._read_provider_config(name)
    if admin_mod.ModelClientPool.get_provider_pool(name):
        pool = admin_mod.ModelClientPool.get_provider_pool(name)
        if pool.channel is not None:
            pool.channel.apply(admin_mod._provider_extra(name, cfg))
    await admin_mod._log_operation(
        token, "merge_external_channel_accounts", "provider", name, None,
        {"source": channel.source, "source_channel_id": channel.external_id, "accounts_added": added},
    )
    return {"provider_name": name, "accounts": added}


@router.post("/channel-importers/commit")
async def commit_channel_import(data: dict, token: str = Header(None, alias="Authorization")):
    """按勾选把候选建/并成渠道。部分失败也返回 200，逐项报告结果。"""
    await admin_mod._require_admin(token)
    payload = data or {}
    selection = payload.get("selection")
    if not isinstance(selection, list) or not selection:
        raise HTTPException(status_code=400, detail="请先勾选要导入的渠道")
    if len(selection) > MAX_SELECTION:
        raise HTTPException(status_code=400, detail=f"一次最多导入 {MAX_SELECTION} 个渠道")

    on_conflict = _clean(payload.get("on_conflict")) or "skip"
    if on_conflict not in _CONFLICT_MODES:
        raise HTTPException(status_code=400, detail=f"未知的冲突处理方式: {on_conflict}")

    scan = await _scan(payload)
    by_id = {channel.candidate_id: channel for channel in scan.channels}
    add_source_tag = payload.get("add_source_tag") is True
    source = _clean(payload.get("source")) or "new-api"

    providers = await admin_mod.config.Config.get_providers()
    existing_names = set((providers or {}).keys()) | set(admin_mod.ModelClientPool.get_provider_names())
    conflict_index = await _conflicts_by_base_url()

    created: list[dict] = []
    overwritten: list[dict] = []
    merged: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    # 批内也要去重：同一批里两个候选指向同一 base_url 时，后者按冲突模式处理。
    batch_seen: dict[str, str] = {}

    for entry in selection:
        if not isinstance(entry, dict):
            continue
        candidate_id = _clean(entry.get("candidate_id"))
        channel = by_id.get(candidate_id)
        if channel is None:
            failed.append({"candidate_id": candidate_id, "error": "候选不存在（源数据可能已变化，请重新预览）"})
            continue
        _apply_overrides(channel, entry)
        if add_source_tag and source not in channel.tags:
            channel.tags = [*channel.tags, source]
        if not channel.base_url:
            failed.append({"candidate_id": candidate_id, "error": "渠道地址为空"})
            continue
        if not channel.importable:
            failed.append({"candidate_id": candidate_id, "error": "；".join(channel.errors) or "该渠道类型不支持导入"})
            continue

        base_key = admin_mod._normalize_base_url_for_compare(channel.base_url)
        existing = conflict_index.get(base_key) or (
            {"provider_name": batch_seen[base_key]} if base_key in batch_seen else None
        )

        try:
            if existing:
                name = existing["provider_name"]
                if on_conflict == "skip":
                    skipped.append({
                        "candidate_id": candidate_id,
                        "reason": "渠道地址已存在",
                        "existing": name,
                    })
                    continue
                if on_conflict == "merge_accounts":
                    result = await _merge_accounts(name, channel, token, payload=payload)
                    merged.append({"candidate_id": candidate_id, **result})
                    continue
                result = await _overwrite_existing(name, channel, token, payload=payload)
                overwritten.append({"candidate_id": candidate_id, **result})
                continue

            name = local_provider_name(channel, existing_names)
            create_payload = build_create_payload(channel, name=name)
            # 走 admin 的共用创建链路，保证与「添加供应商」向导语义完全一致。
            await admin_mod._create_provider_from_config(
                create_payload, token=token, is_admin=True,
                existing_names=existing_names, log_action="import_external_channels",
            )
            batch_seen[base_key] = name
            created.append({
                "candidate_id": candidate_id,
                "provider_name": name,
                "accounts": len(create_payload.get("accounts") or []),
                "models": len(create_payload.get("models") or []),
            })
        except HTTPException as exc:
            failed.append({"candidate_id": candidate_id, "error": str(exc.detail)})
        except Exception as exc:  # noqa: BLE001 - 单个渠道失败不该中断整批
            logger.warning("[channel-importer] commit failed for {}: {}", candidate_id, exc)
            failed.append({"candidate_id": candidate_id, "error": str(exc)})

    await admin_mod._log_operation(
        token, "import_external_channels", "channel_import", source, None,
        {
            "source": source,
            "remote_host": _clean(payload.get("base_url")),
            "on_conflict": on_conflict,
            "selected": len(selection),
            "created": len(created),
            "overwritten": len(overwritten),
            "merged": len(merged),
            "skipped": len(skipped),
            "failed": len(failed),
        },
    )

    warnings = list(scan.warnings)
    if any(item["accounts"] == 0 for item in created):
        warnings.append("部分导入的渠道没有账号（上游未提供密钥），请在渠道详情的账号管理里补填")

    return {
        "ok": True,
        "created": created,
        "overwritten": overwritten,
        "merged": merged,
        "skipped": skipped,
        "failed": failed,
        "warnings": warnings,
        "summary": {
            "selected": len(selection),
            "created": len(created),
            "overwritten": len(overwritten),
            "merged": len(merged),
            "skipped": len(skipped),
            "failed": len(failed),
        },
    }
