"""Service layer for the DDNS manager: provider-profile + record CRUD.

Two levels (mirrors :mod:`.tunnel_service`' scheme → binding split):

* **Provider profile** (:class:`DdnsProvider`) — a reusable credential set (one
  阿里云 account / Cloudflare token). Its ``config`` carries only secrets,
  masked on the way out and preserved when the client echoes the masked form
  back on update — same contract as :mod:`.tunnel_service`.
* **Record** (:class:`DdnsRecord`) — one DDNS task under a provider: which
  domain/subdomains to publish and where the public IP comes from. Its
  ``config`` carries no secrets (domain/names/proxied/comment), so it is not
  masked.

Validation here is provider-agnostic shape checking; the actual sync lives in
:mod:`.ddns_sync` (owned by the tunnel_server process for the scheduler, and
called directly by the main service's "sync now" route).
"""
from __future__ import annotations

import re
import uuid

from loguru import logger

from .masking import mask_secret
from .models_ddns import DdnsProvider, DdnsRecord

# Providers handled in v1 (per the original ddns.py script).
DDNS_PROVIDERS = ("aliyun", "cloudflare")

# IP sources: where the public IP to publish comes from.
IP_SOURCES = ("server", "node", "node_proxy")

# DNS record types this DDNS manager maintains, in publish order. A record's
# ``types`` selects which of these it manages; the sync engine keys its
# reconcile plan on (sub-domain, type) — so the type is part of a record's
# identity — and publishes at most one address per selected family. ``["A"]``
# therefore never adds an AAAA just because an IPv6 probe happened to succeed.
# This is the single whitelist to extend when a new (IP-valued) type is added.
RECORD_TYPES = ("A", "AAAA")

# Provider-config keys holding a secret: masked in responses, preserved when
# the client echoes the masked form back on update. ``access_key_id`` is NOT a
# secret — it is safe to display alongside the masked secret.
_SECRET_KEYS = ("api_token", "access_key_secret")

# A single DNS label: 1-63 chars, alphanumeric + hyphen, no leading/trailing
# hyphen. A domain is one or more labels joined by dots (max 253 total).
_DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

# Optional outbound proxy URL used while fetching the public IP. Deliberately
# loose: only an absolute URL with a scheme is required (the fetcher passes it
# straight to aiohttp / curl).
_PROXY_URL = re.compile(r"^[a-z][a-z0-9+.-]*://.+$", re.IGNORECASE)

_NAME_RE = re.compile(r"^.{1,128}$")


class DdnsServiceError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _normalize_domain(value: str) -> str:
    """Lower-case + strip a trailing dot, and validate DNS shape.

    Copied from ``tunnel_service._normalize_domain`` (same validation, kept
    local so neither module reaches into the other's private helper — keep the
    two in sync if the rules ever change). Raises DdnsServiceError on failure.
    """
    domain = (value or "").strip().lower().rstrip(".")
    if not domain:
        raise DdnsServiceError("invalid_argument", "domain is empty")
    if len(domain) > 253:
        raise DdnsServiceError("invalid_argument", "domain too long")
    if "://" in domain or "/" in domain or ":" in domain or "*" in domain:
        raise DdnsServiceError("invalid_argument", f"invalid domain {value!r}")
    labels = domain.split(".")
    if len(labels) < 2 or not all(_DNS_LABEL.match(lbl) for lbl in labels):
        raise DdnsServiceError("invalid_argument", f"invalid domain {value!r}")
    return domain


def _normalize_names(value: object) -> list[str]:
    """Normalize the sub-domain list: strip, drop blanks, dedupe, keep ``@``.

    ``@`` means the root domain for both providers (aliyun RR, cloudflare
    ``@`` → zone apex), so it is kept as-is.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item or "").strip().rstrip(".").lower()
        if not name or name in seen:
            continue
        # A label or a relative multi-label name (api.dev); reject wildcards.
        if "*" in name or "/" in name or ":" in name:
            raise DdnsServiceError("invalid_argument", f"invalid sub-domain {item!r}")
        if name != "@":
            labels = name.split(".")
            if not all(_DNS_LABEL.match(lbl) for lbl in labels):
                raise DdnsServiceError("invalid_argument", f"invalid sub-domain {item!r}")
        seen.add(name)
        out.append(name)
    return out


def _normalize_types(value: object) -> list[str]:
    """Normalize the record's DNS-type list: upper-case, dedupe, whitelist.

    Missing/unset → both families (``["A", "AAAA"]``), so records created before
    ``types`` existed keep managing both — backward compatible. An explicit but
    empty selection is rejected: a record that manages no type would never
    publish anything. Output is ordered per :data:`RECORD_TYPES` (A before AAAA)
    regardless of input order, and unknown values raise rather than be silently
    dropped — the service layer is the strict gate, the sync engine is permissive.
    """
    if value is None:
        return list(RECORD_TYPES)
    if not isinstance(value, (list, tuple)):
        raise DdnsServiceError("invalid_argument", "types must be a list of record types")
    seen: set[str] = set()
    for item in value:
        t = str(item or "").strip().upper()
        if not t or t in seen:
            continue
        if t not in RECORD_TYPES:
            raise DdnsServiceError(
                "invalid_argument", f"unsupported record type {item!r}; expected one of {RECORD_TYPES}"
            )
        seen.add(t)
    if not seen:
        raise DdnsServiceError("invalid_argument", "types must select at least one of A/AAAA")
    return [t for t in RECORD_TYPES if t in seen]


def _validate_proxy(value: str | None) -> str | None:
    """Validate the optional outbound proxy URL; empty → None."""
    proxy = (value or "").strip()
    if not proxy:
        return None
    if len(proxy) > 512 or not _PROXY_URL.match(proxy):
        raise DdnsServiceError(
            "invalid_argument",
            "proxy must be an absolute URL like http://host:port (or socks5://)",
        )
    return proxy


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not _NAME_RE.match(cleaned):
        raise DdnsServiceError("invalid_argument", "name is required (1-128 chars)")
    return cleaned


def _interval(value: int | None) -> int:
    try:
        secs = int(value or 120)
    except (TypeError, ValueError):
        raise DdnsServiceError("invalid_argument", "interval_seconds must be an integer") from None
    if secs < 30 or secs > 86400:
        raise DdnsServiceError("invalid_argument", "interval_seconds must be 30-86400")
    return secs


# ── provider credential validation ────────────────────────────────────────


def validate_provider_config(provider: str, config: dict) -> dict:
    """Validate + normalize a provider profile's credential config.

    Mutates/returns a normalized copy (region default, trimmed ids). Raises
    :class:`DdnsServiceError` on any missing required credential.
    """
    provider = (provider or "").strip().lower()
    if provider not in DDNS_PROVIDERS:
        raise DdnsServiceError(
            "invalid_argument",
            f"unsupported provider {provider!r}; expected one of {DDNS_PROVIDERS}",
        )
    config = dict(config or {})
    if provider == "aliyun":
        if not str(config.get("access_key_id") or "").strip():
            raise DdnsServiceError("invalid_argument", "aliyun requires access_key_id")
        if not str(config.get("access_key_secret") or "").strip():
            raise DdnsServiceError("invalid_argument", "aliyun requires access_key_secret")
        config["access_key_id"] = str(config["access_key_id"]).strip()
        config["region_id"] = str(config.get("region_id") or "cn-hangzhou").strip() or "cn-hangzhou"
    else:  # cloudflare
        if not str(config.get("api_token") or "").strip():
            raise DdnsServiceError("invalid_argument", "cloudflare requires api_token")
    return config


# ── record config validation ──────────────────────────────────────────────


def validate_record_shape(
    *,
    provider: str,
    ip_source: str,
    node_id: str | None,
    proxy: str | None,
    config: dict,
) -> tuple[str, str | None, dict]:
    """Validate + normalize a record's IP source + domain targets.

    ``provider`` is the (denormalized) kind from the owning profile — it gates
    which write options (``proxied``/``comment``) apply. Returns
    ``(ip_source, clean_proxy, clean_config)``.
    """
    ip_source = (ip_source or "").strip().lower()
    if ip_source not in IP_SOURCES:
        raise DdnsServiceError(
            "invalid_argument", f"unsupported ip_source {ip_source!r}; expected one of {IP_SOURCES}"
        )

    clean_node = (node_id or "").strip() or None
    if ip_source in ("node", "node_proxy") and not clean_node:
        raise DdnsServiceError("invalid_argument", "ip_source 'node'/'node_proxy' requires a node_id")
    if ip_source == "server" and clean_node:
        raise DdnsServiceError("invalid_argument", "ip_source 'server' must not carry a node_id")

    # proxy only applies to server / node_proxy (probing); node ignores it.
    if ip_source == "node":
        clean_proxy = None
    else:
        clean_proxy = _validate_proxy(proxy)

    config = dict(config or {})
    config["domain"] = _normalize_domain(str(config.get("domain") or ""))
    names = _normalize_names(config.get("names"))
    if not names:
        raise DdnsServiceError("invalid_argument", "at least one sub-domain (name) is required")
    config["names"] = names

    config["types"] = _normalize_types(config.get("types"))

    if provider == "cloudflare":
        config["proxied"] = bool(config.get("proxied", False))
    else:
        config.pop("proxied", None)

    # The comment is the managed marker (provider-agnostic contract — see
    # ddns_sync.plan_reconcile): set → only manage records carrying this remark;
    # empty → takeover the sub-domains in the list (adopt user records too).
    # Aliyun stores it via UpdateDomainRecordRemark (separate call), Cloudflare
    # inline in the record payload. Empty is a real value (takeover mode), so
    # it must round-trip, never default to "ddns".
    comment = str(config.get("comment") or "").strip()
    if comment:
        # Aliyun Remark caps at 128; Cloudflare allows more. Cap by provider so a
        # long CF comment is not silently truncated to the aliyun limit.
        config["comment"] = comment[: (280 if provider == "cloudflare" else 128)]
    else:
        config.pop("comment", None)

    return ip_source, clean_proxy, config


# ── masking ──────────────────────────────────────────────────────────────


def _is_masked(value: str | None) -> bool:
    return bool(value) and "***" in (value or "")


def _public_config(config: dict | None) -> dict:
    """Copy of config with all secret fields masked (opaque ``***`` form)."""
    out = dict(config or {})
    for key in _SECRET_KEYS:
        if key in out and out[key]:
            out[key] = mask_secret(str(out[key]))
    return out


def _merge_masked_secrets(incoming: dict, stored: dict | None) -> dict:
    """Splice stored cleartext secrets into an incoming config update.

    ``incoming`` may carry the masked form (``***``) for secret fields the
    client did not re-enter. Those mean "unchanged" — replace them with the
    stored cleartext so validation sees the real value and the mask never
    overwrites the secret.
    """
    stored = stored or {}
    merged = dict(incoming)
    for key in _SECRET_KEYS:
        value = merged.get(key)
        if value is not None and _is_masked(str(value)) and stored.get(key):
            merged[key] = stored[key]
    return merged


# ── dict projections ─────────────────────────────────────────────────────


def _provider_dict(row: DdnsProvider) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "provider": row.provider,
        "config": _public_config(row.config),
        "enabled": bool(row.enabled),
        "created_at": int(row.created_at.timestamp()) if row.created_at else 0,
        "updated_at": int(row.updated_at.timestamp()) if row.updated_at else 0,
    }


def _record_dict(row: DdnsRecord) -> dict:
    return {
        "id": str(row.id),
        "provider_id": str(row.provider_id),
        "provider": row.provider,
        "name": row.name,
        "ip_source": row.ip_source,
        "node_id": row.node_id,
        "proxy": row.proxy,
        # Record config carries no secrets — pass it through verbatim.
        "config": dict(row.config or {}),
        "interval_seconds": int(row.interval_seconds or 120),
        "enabled": bool(row.enabled),
        "last_ipv4": row.last_ipv4,
        "last_ipv6": row.last_ipv6,
        "last_sync_at": int(row.last_sync_at.timestamp()) if row.last_sync_at else None,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "last_changes": row.last_changes,
        "created_at": int(row.created_at.timestamp()) if row.created_at else 0,
        "updated_at": int(row.updated_at.timestamp()) if row.updated_at else 0,
    }


# ── provider CRUD ────────────────────────────────────────────────────────


async def list_providers(user_id: str) -> list[dict]:
    rows = await DdnsProvider.filter(user_id=uuid.UUID(user_id)).order_by("-created_at")
    return [_provider_dict(r) for r in rows]


async def get_provider(user_id: str, provider_id: str) -> dict:
    row = await _owned_provider(user_id, provider_id)
    return _provider_dict(row)


async def _owned_provider(user_id: str, provider_id: str) -> DdnsProvider:
    row = await DdnsProvider.get_or_none(id=uuid.UUID(provider_id), user_id=uuid.UUID(user_id))
    if row is None:
        raise DdnsServiceError("not_found", "provider not found")
    return row


async def create_provider(
    user_id: str, *, name: str, provider: str, config: dict, enabled: bool = True
) -> dict:
    provider = (provider or "").strip().lower()
    config = validate_provider_config(provider, config)
    row = await DdnsProvider.create(
        user_id=uuid.UUID(user_id),
        name=_clean_name(name),
        provider=provider,
        config=config,
        enabled=bool(enabled),
    )
    logger.info("[ddns] provider created: id={} kind={} name={}", row.id, provider, row.name)
    return _provider_dict(row)


async def update_provider(
    user_id: str, provider_id: str, *, name: str | None = None,
    config: dict | None = None, enabled: bool | None = None,
) -> dict:
    row = await _owned_provider(user_id, provider_id)
    if config is not None:
        # A secret field echoing the masked form means "unchanged": splice the
        # stored cleartext back so the mask never overwrites the real secret.
        config = _merge_masked_secrets(config, row.config)
        config = validate_provider_config(row.provider, config)
        row.config = config
    if name is not None:
        row.name = _clean_name(name)
    if enabled is not None:
        row.enabled = bool(enabled)
    await row.save()
    logger.info("[ddns] provider updated: id={}", row.id)
    return _provider_dict(row)


async def delete_provider(user_id: str, provider_id: str) -> dict:
    row = await _owned_provider(user_id, provider_id)
    live = await DdnsRecord.filter(provider_id=row.id).count()
    if live:
        raise DdnsServiceError(
            "in_use", f"该服务商下还有 {live} 条 DDNS 记录，请先删除它们"
        )
    await row.delete()
    logger.info("[ddns] provider deleted: id={}", provider_id)
    return {"deleted": True}


# ── record CRUD ──────────────────────────────────────────────────────────


async def list_records(user_id: str, provider_id: str | None = None) -> list[dict]:
    qs = DdnsRecord.filter(user_id=uuid.UUID(user_id))
    if provider_id:
        qs = qs.filter(provider_id=uuid.UUID(provider_id))
    rows = await qs.order_by("-created_at")
    return [_record_dict(r) for r in rows]


async def get_record(user_id: str, record_id: str) -> dict:
    row = await get_row(user_id, record_id)
    return _record_dict(row)


async def get_row(user_id: str, record_id: str) -> DdnsRecord:
    """Raw row fetch (for the sync route / scheduler), owner-checked."""
    row = await DdnsRecord.get_or_none(id=uuid.UUID(record_id), user_id=uuid.UUID(user_id))
    if row is None:
        raise DdnsServiceError("not_found", "record not found")
    return row


async def create_record(
    user_id: str,
    *,
    provider_id: str,
    name: str,
    ip_source: str = "server",
    node_id: str | None = None,
    proxy: str | None = None,
    config: dict,
    interval_seconds: int = 120,
    enabled: bool = True,
) -> dict:
    # The provider profile is the truth for kind + credentials; the record only
    # carries domain targets + IP source. Denormalize the kind onto the row.
    profile = await DdnsProvider.get_or_none(
        id=uuid.UUID(provider_id), user_id=uuid.UUID(user_id)
    )
    if profile is None:
        raise DdnsServiceError("not_found", "provider not found")
    if not profile.enabled:
        raise DdnsServiceError("invalid_argument", "该服务商已停用，请先启用它")

    ip_source, proxy, config = validate_record_shape(
        provider=profile.provider, ip_source=ip_source, node_id=node_id,
        proxy=proxy, config=config,
    )
    row = await DdnsRecord.create(
        user_id=uuid.UUID(user_id),
        provider_id=profile.id,
        provider=profile.provider,
        name=_clean_name(name),
        ip_source=ip_source,
        node_id=(node_id or "").strip() or None,
        proxy=proxy,
        config=config,
        interval_seconds=_interval(interval_seconds),
        enabled=bool(enabled),
    )
    logger.info(
        "[ddns] record created: id={} provider={} kind={} ip_source={} node={} name={}",
        row.id, profile.id, profile.provider, ip_source, row.node_id, row.name,
    )
    return _record_dict(row)


async def update_record(
    user_id: str,
    record_id: str,
    *,
    name: str | None = None,
    ip_source: str | None = None,
    node_id: str | None = None,
    proxy: str | None = None,
    config: dict | None = None,
    interval_seconds: int | None = None,
    enabled: bool | None = None,
) -> dict:
    row = await get_row(user_id, record_id)
    if ip_source is not None or config is not None or proxy is not None or node_id is not None:
        final_ip_source = ip_source if ip_source is not None else row.ip_source
        final_node_id = node_id if node_id is not None else row.node_id
        final_proxy = proxy if proxy is not None else row.proxy
        final_config = config if config is not None else dict(row.config or {})
        final_ip_source, final_proxy, final_config = validate_record_shape(
            provider=row.provider, ip_source=final_ip_source, node_id=final_node_id,
            proxy=final_proxy, config=final_config,
        )
        row.ip_source = final_ip_source
        row.node_id = (final_node_id or "").strip() or None
        row.proxy = final_proxy
        row.config = final_config
    if name is not None:
        row.name = _clean_name(name)
    if interval_seconds is not None:
        row.interval_seconds = _interval(interval_seconds)
    if enabled is not None:
        row.enabled = bool(enabled)
    await row.save()
    logger.info("[ddns] record updated: id={} ip_source={}", row.id, row.ip_source)
    return _record_dict(row)


async def delete_record(user_id: str, record_id: str) -> dict:
    row = await get_row(user_id, record_id)
    await row.delete()
    logger.info("[ddns] record deleted: id={}", record_id)
    return {"deleted": True}


# ── merged config for the sync engine ────────────────────────────────────


async def effective_config(row: DdnsRecord) -> dict:
    """Credentials (from the provider profile) + targets (from the record).

    The sync engine consumes one flat config blob (``domain``/``names``/
    ``types``/``proxied``/``comment`` + ``access_key_id``/``access_key_secret``/
    ``api_token``/``region_id``), so the two levels are spliced here. Raises if
    the owning profile vanished (a record should never outlive its provider —
    delete_provider guards this, but a hard DB delete would).
    """
    profile = await DdnsProvider.get_or_none(id=row.provider_id)
    if profile is None:
        raise DdnsServiceError("not_found", f"provider {row.provider_id} not found for record {row.id}")
    merged = dict(profile.config or {})
    merged.update(row.config or {})
    return merged
