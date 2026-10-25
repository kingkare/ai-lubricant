"""DDNS sync engine: acquire the public IP (three sources) + provider writes.

This is the ported core of ``ddns.py`` (aliyun + cloudflare), restructured for
the platform:

* **IP acquisition** is dispatched on ``ip_source`` —
  - ``server``: probe this process's egress with the platform's global IP-lookup
    URL list (``GetPublicIPLookupConfig``, fetched via the node client; falls
    back to built-in URLs when unset/unreachable), optionally through a proxy;
  - ``node``: read the execution node's heartbeat-reported public IP
    (``capabilities.public_ip`` / ``public_ipv6``) — zero probing;
  - ``node_proxy``: dispatch a one-shot ``curl`` probe to the node via
    ``host_exec`` (through a proxy when set) and parse its stdout.
* **Provider writes** update A/AAAA records only when the value actually
  changed, and return a narrow change list the caller persists + notifies on.

The aliyun SDK is a synchronous dependency: it is imported lazily inside
:class:`AliyunProvider` so this module stays importable (and the cloudflare /
IP-acquisition paths testable) without ``aliyunsdkcore`` installed, and its
calls run in a thread executor so the event loop is never blocked.

The actual IP URLs come from the **global** lookup config, never from the
record — a record carries only ``ip_source`` + ``node_id`` + ``proxy``.
"""
from __future__ import annotations

import asyncio
import re
import shlex
from collections import Counter
from typing import Any

import aiohttp
from loguru import logger

# ── IP extraction (from the original ddns.py) ─────────────────────────────
IPV4_REG = (
    r"((25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])\.){3,3}"
    r"(25[0-5]|(2[0-4]|1{0,1}[0-9]){0,1}[0-9])"
)
IPV6_REG = (
    r"(([0-9a-fA-F]{1,4}:){7,7}[0-9a-fA-F]{1,4}"
    r"|([0-9a-fA-F]{1,4}:){1,7}:"
    r"|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}"
    r"|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}"
    r"|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}"
    r"|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}"
    r"|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}"
    r"|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})"
    r"|:((:[0-9a-fA-F]{1,4}){1,7}|:))"
)

# Fallback lookup URLs when the global config is unset / the control plane is
# unreachable. Same list as ``marketplace.node_ip_config.DEFAULT_*`` — that
# table is only ever editable through the node server, so a local copy keeps
# this module free of that import.
FALLBACK_IPV4_URLS: list[str] = ["https://ip4.me/", "http://httpbin.org/ip"]
FALLBACK_IPV6_URLS: list[str] = [
    "https://ip6only.me/",
    "http://v6.ipv6-test.com/api/myip.php",
]

_CF_API_BASE = "https://api.cloudflare.com/client/v4"
_PROBE_TIMEOUT_MS = 20_000


class DdnsSyncError(Exception):
    """A sync attempt failed (IP acquisition or provider write)."""


# ── IP lookup URL source (global config) ──────────────────────────────────


def _clean_url_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _pick_urls(raw: Any, fallback: list[str]) -> list[str]:
    urls = _clean_url_list(raw)
    return urls or list(fallback)


async def resolve_ip_lookup_urls() -> tuple[list[str], list[str]]:
    """The platform's global IPv4/IPv6 probe URL lists.

    Read via the node client RPC so the **same** list every execution node
    already uses backs DDNS. Connect JSON is camelCase (``ipv4Urls``) but a
    snake_case key is also accepted; any failure (no control plane, empty list)
    degrades to the built-in fallback rather than raising.
    """
    try:
        from .node_client import get_local_node_client

        cfg = await get_local_node_client().get_public_ip_lookup_config()
    except Exception as exc:  # noqa: BLE001 — DDNS must still run without the control plane
        logger.debug("[ddns] lookup URL config unavailable, using fallback: {}", exc)
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    return (
        _pick_urls(cfg.get("ipv4_urls") or cfg.get("ipv4Urls"), FALLBACK_IPV4_URLS),
        _pick_urls(cfg.get("ipv6_urls") or cfg.get("ipv6Urls"), FALLBACK_IPV6_URLS),
    )


# ── local probe (server source) ───────────────────────────────────────────


async def _fetch_ip(session: aiohttp.ClientSession, url: str, reg: str) -> str | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
            text = await response.text()
            match = re.search(reg, text)
            if match:
                return match.group(0)
    except Exception:  # noqa: BLE001 — a dead source is just a skipped vote
        pass
    return None


async def get_best_ip(urls: list[str], reg: str, proxy: str | None = None) -> str | None:
    """Probe every URL concurrently and return the majority IP (mode).

    Port of the original ``IPGetter.get_best_ip``: SSL verification is off
    (several probe sites have expired certs), and a proxy is supported per the
    record's ``proxy`` field.
    """
    if not urls:
        return None
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector, proxy=proxy or None) as session:
        results = await asyncio.gather(*[_fetch_ip(session, url, reg) for url in urls])
    valid = [ip for ip in results if ip]
    if not valid:
        return None
    return Counter(valid).most_common(1)[0][0]


# ── node probe (node_proxy source) ────────────────────────────────────────


def _cmd_safe(value: str) -> str:
    """Strip cmd.exe metacharacters from a value interpolated into quotes."""
    for ch in ('"', "%", "&", "|", "<", ">", "^"):
        value = value.replace(ch, "")
    return value


def probe_command(urls: list[str], proxy: str | None, family: int, *, is_windows: bool) -> str:
    """A one-shot shell command that prints every URL's body (newline separated).

    ``curl -4``/``-6`` pins the address family (so a v4-capable URL still yields
    a v6 when probing v6). POSIX uses a ``for`` loop with ``shlex.quote``;
    Windows uses ``cmd.exe`` chained ``curl`` calls with metacharacters
    stripped (both ``curl.exe`` ships on Win10+ and the node's host shell).
    """
    if not urls:
        return "true"
    if is_windows:
        proxy_part = f' --proxy "{_cmd_safe(proxy)}"' if proxy else ""
        return " & ".join(
            f'curl -fsS -{family} --max-time 10{proxy_part} "{_cmd_safe(url)}"' for url in urls
        )
    proxy_part = f" --proxy {shlex.quote(proxy)}" if proxy else ""
    quoted = " ".join(shlex.quote(url) for url in urls)
    return (
        f'for u in {quoted}; do '
        f'curl -fsS -{family} --max-time 10{proxy_part} "$u" 2>/dev/null; echo; done'
    )


def parse_probe_output(stdout: str) -> tuple[str | None, str | None]:
    """Extract the majority v4 + v6 from a probe's combined stdout.

    ``finditer(...).group(0)`` rather than ``re.findall`` — both regexes carry
    capture groups, and ``findall`` would return the *groups* instead of the
    whole match.
    """
    text = stdout or ""
    v4_matches = [m.group(0) for m in re.finditer(IPV4_REG, text)]
    v4 = Counter(v4_matches).most_common(1)[0][0] if v4_matches else None
    # IPv6 candidates can contain a v4 tail; drop exact-v4 matches first.
    v6_matches = [
        ip for ip in (m.group(0) for m in re.finditer(IPV6_REG, text))
        if ip not in v4_matches and ":" in ip
    ]
    v6 = Counter(v6_matches).most_common(1)[0][0] if v6_matches else None
    return v4, v6


def _find_node(nodes: list[dict], node_id: str) -> dict | None:
    for node in nodes:
        if str(node.get("node_id") or "") == str(node_id):
            return node
    return None


async def _acquire_from_node(record: Any, *, probe: bool) -> tuple[str | None, str | None]:
    """Read (``probe=False``) or on-demand probe (``probe=True``) a node's IP."""
    from .node_client import get_local_node_client

    client = get_local_node_client()
    node = _find_node(await client.list_nodes(), record.node_id)
    if node is None:
        raise DdnsSyncError(f"节点 {record.node_id} 不存在或不可见")
    caps = node.get("capabilities") or {}

    if not probe:
        # Heartbeat-reported address — no probing.
        ipv4 = str(caps.get("public_ip") or "").strip() or None
        ipv6 = str(caps.get("public_ipv6") or "").strip() or None
        if not ipv4 and not ipv6:
            raise DdnsSyncError("节点未上报公网 IP，请确认节点在线且探测地址可用，或改用『服务器』来源")
        return ipv4, ipv6

    if not node.get("online"):
        raise DdnsSyncError("节点离线，无法执行探测命令")
    if str(caps.get("host_exec") or "").lower() != "true":
        raise DdnsSyncError(
            "该节点不支持 host 探测，请改用『节点』来源或换一个支持 host_exec 的节点"
        )
    is_windows = str(caps.get("os") or "").strip().lower() == "windows"
    ipv4_urls, ipv6_urls = await resolve_ip_lookup_urls()
    proxy = (record.proxy or "").strip() or None

    async def _run(urls: list[str], family: int) -> str | None:
        cmd = probe_command(urls, proxy, family, is_windows=is_windows)
        result = await client.host_exec(
            record.node_id, cmd, cwd="", timeout_ms=_PROBE_TIMEOUT_MS, max_output_bytes=65536
        )
        v4, v6 = parse_probe_output(str(result.get("stdout") or ""))
        return v4 if family == 4 else v6

    return await _run(ipv4_urls, 4), await _run(ipv6_urls, 6)


async def acquire_ip(record: Any) -> tuple[str | None, str | None]:
    """Resolve the (ipv4, ipv6) to publish, dispatched on ``ip_source``."""
    source = (record.ip_source or "server").strip().lower()
    if source == "server":
        ipv4_urls, ipv6_urls = await resolve_ip_lookup_urls()
        proxy = (record.proxy or "").strip() or None
        return (
            await get_best_ip(ipv4_urls, IPV4_REG, proxy=proxy),
            await get_best_ip(ipv6_urls, IPV6_REG, proxy=proxy),
        )
    if source == "node":
        return await _acquire_from_node(record, probe=False)
    if source == "node_proxy":
        return await _acquire_from_node(record, probe=True)
    raise DdnsSyncError(f"未知 ip_source: {source!r}")


# ── reconciliation plan (provider-agnostic, pure) ─────────────────────────


# The DNS families the planner can publish, and the address each draws from.
# Extend this map (and ``ddns_service.RECORD_TYPES``) to support a new
# IP-valued record type; the reconcile key is (sub-domain, type) throughout, so
# a record's identity already carries the type — adding one is a whitelist edit,
# not a logic change.
_FAMILY_ADDR = (("A", "ipv4"), ("AAAA", "ipv6"))
_DEFAULT_TYPES = tuple(t for t, _ in _FAMILY_ADDR)


def plan_reconcile(
    existing: list[dict],
    *,
    domain: str,
    names: list[str],
    ipv4: str | None,
    ipv6: str | None,
    comment: str | None,
    types: list[str] | None = None,
) -> list[dict]:
    """Compute the change list that makes the provider's records match this
    record's configuration exactly — the invariant is **(domain + comment) must
    equal this record's config**, and only for the **selected DNS types**.

    ``existing`` is the provider's raw A/AAAA records for ``domain``, each row a
    dict with **relative** keys: ``name`` (RR / sub-domain), ``type``, ``value``
    (or ``content``), the record ``id``, and the provider's remark field
    (``remark`` for aliyun, ``comment`` for cloudflare). ``comment`` is the
    managed marker:

    * **comment set** — the manageable universe is only records that carry a
      remark ("备注了的", whatever the text). Unmarked user records are never
      touched. Such records are then reconciled to exactly this config: value
      pointed at the current IP, remark refreshed to ``comment`` when it differs
      (so editing the comment re-marks old records instead of orphaning them),
      sub-domains outside ``names`` deleted, duplicates collapsed.
    * **comment empty (takeover)** — the universe is every A/AAAA whose
      sub-domain is in ``names``, marked or not (user-created records inside the
      list are adopted and pointed at the current IP). Duplicates beyond the
      first per key are deleted; sub-domains outside ``names`` are never touched.

    ``types`` is the record's DNS-type whitelist (subset of ``A``/``AAAA``;
    ``None`` → both, so pre-``types`` records keep managing both families). It
    is the fix for "added an A, next sync also added an AAAA": a family is only
    *published* when it is selected **and** an address for it resolved this run.
    A selected family whose address is missing this run is left completely
    alone (a transient IPv6 probe failure must not delete the AAAA we made
    earlier). A **deselected** family is out of scope: in marker mode its marked
    records are deleted (turning AAAA off cleans up the AAAA we created), while
    in takeover mode nothing outside the selected set is touched (consistent
    with "sub-domains outside ``names`` are never touched").

    Returns ``{action, record, type, ip, id?}`` actions, action ∈ ``updated`` /
    ``added`` / ``deleted`` / ``adopted`` (``id`` present for every action except
    ``added``). ``adopted`` means the exact ``(sub, type, value)`` triple already
    exists but lies outside the manageable universe: the provider only stamps
    the marker onto it and must not create a record, because providers reject a
    fully duplicated triple (aliyun ``DomainRecordDuplicate``, Cloudflare the
    same). Pure: no I/O, so the delete/update semantics are unit-testable
    without a provider.
    """
    names_set = set(names)
    selected = {t for t in (types or _DEFAULT_TYPES)}
    addrs = {"A": ipv4, "AAAA": ipv6}

    def _sub(row: dict) -> str:
        # DNS names are case-insensitive; providers may echo back a differently
        # cased RR than the configured one. Normalize so an existing record is
        # recognized as the same key (else the planner would add a duplicate).
        return str(row.get("name") or "").strip().rstrip(".").lower()

    def _value(row: dict) -> str:
        return str(row.get("value") or row.get("content") or "")

    def _marked(row: dict) -> bool:
        # "备注了的" — any non-empty remark marks a record as platform-managed.
        return bool(str(row.get("remark") or row.get("comment") or "").strip())

    # Every provider row, manageable or not, indexed by its full triple — used
    # to detect an existing but unmanaged record that already carries the
    # desired value (the DomainRecordDuplicate case).
    present: dict[tuple[str, str, str], dict] = {}
    for row in existing:
        ip_type = str(row.get("type") or "")
        if ip_type not in _DEFAULT_TYPES:
            continue
        present.setdefault((_sub(row), ip_type, _value(row)), row)

    # Group the manageable existing rows by (sub, type). Duplicates within a key
    # are preserved so the extras can be deleted.
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in existing:
        ip_type = str(row.get("type") or "")
        if ip_type not in _DEFAULT_TYPES:
            continue
        sub = _sub(row)
        if comment:
            if not _marked(row):
                continue  # unmarked = user-created; never touch
        elif sub not in names_set:
            continue  # takeover only adopts the listed sub-domains
        groups.setdefault((sub, ip_type), []).append(row)

    desired: dict[tuple[str, str], str] = {}
    for sub in names:
        for ip_type, _ in _FAMILY_ADDR:
            if ip_type in selected and addrs.get(ip_type):
                desired[(sub, ip_type)] = addrs[ip_type]

    # Families we should be publishing this run: selected AND an address
    # resolved. A selected family with no address is left alone below.
    active_types = {t for (_, t) in desired}

    plan: list[dict] = []
    # Delete: a deselected family (marker mode only — clean up what we made),
    # keys not desired (stale leftovers / sub removed from the list), plus every
    # duplicate beyond the first per desired key (collapse to exactly one).
    for key, rows in groups.items():
        sub, ip_type = key
        if ip_type not in selected:
            # Deselected family. Marker mode: these are platform leftovers →
            # delete. Takeover mode: out of scope → never touch.
            if comment:
                for row in rows:
                    plan.append({"action": "deleted", "record": sub, "type": ip_type,
                                 "ip": _value(row), "id": str(row.get("id") or "")})
            continue
        if ip_type not in active_types:
            continue  # selected but no address this run → leave the family alone
        if key in desired:
            for extra in rows[1:]:
                plan.append({"action": "deleted", "record": sub, "type": ip_type,
                             "ip": _value(extra), "id": str(extra.get("id") or "")})
        else:
            for row in rows:
                plan.append({"action": "deleted", "record": sub, "type": ip_type,
                             "ip": _value(row), "id": str(row.get("id") or "")})

    # Update / add / adopt: every desired key converges to one record with the
    # current IP.
    for key, current in desired.items():
        sub, ip_type = key
        rows = groups.get(key)
        if rows:
            keep = rows[0]
            if _value(keep) != current:
                plan.append({"action": "updated", "record": sub, "type": ip_type,
                             "ip": current, "id": str(keep.get("id") or "")})
            continue
        # Nothing manageable for this key. A same-value record that is merely
        # unmanaged (marker mode: the user's own record already points here)
        # must be adopted, not duplicated — providers reject the identical
        # (name, type, value) triple outright.
        same = present.get((sub, ip_type, current))
        if same is not None:
            plan.append({"action": "adopted", "record": sub, "type": ip_type,
                         "ip": current, "id": str(same.get("id") or "")})
        else:
            plan.append({"action": "added", "record": sub, "type": ip_type, "ip": current})
    return plan


# ── Cloudflare provider ───────────────────────────────────────────────────


class CloudflareProvider:
    """Sync A/AAAA records for each configured sub-domain via the CF v4 API."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.api_token = str(config.get("api_token") or "")
        self.domain = str(config.get("domain") or "")
        self.names = list(config.get("names") or [])
        # Selected DNS types (A/AAAA). Missing → both (pre-types records).
        self.types = list(config.get("types") or _DEFAULT_TYPES)
        self.proxied = bool(config.get("proxied", False))
        # The managed marker. Empty = takeover mode (adopt every A/AAAA under
        # the listed sub-domains); no implicit "ddns" default, or the marker and
        # takeover modes could never be told apart.
        self.comment = str(config.get("comment") or "").strip()

    async def _request(
        self, session: aiohttp.ClientSession, method: str, path: str, **kw: Any
    ) -> Any:
        url = f"{_CF_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        async with session.request(method, url, headers=headers, **kw) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise DdnsSyncError(f"Cloudflare {method} {path} HTTP {resp.status}: {text[:300]}")
            try:
                data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                raise DdnsSyncError(f"Cloudflare {method} {path}: invalid JSON") from exc
        if not isinstance(data, dict) or not data.get("success", False):
            raise DdnsSyncError(f"Cloudflare {method} {path}: {data.get('errors') if isinstance(data, dict) else data}")
        return data.get("result")

    async def sync(self, ipv4: str | None, ipv6: str | None) -> list[dict]:
        if not self.api_token or not self.domain:
            raise DdnsSyncError("Cloudflare 配置缺少 api_token 或 domain")
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            zones = await self._request(
                session, "GET", "/zones", params={"name": self.domain}
            )
            if not zones:
                raise DdnsSyncError(f"Cloudflare: 未找到 zone {self.domain!r}")
            zone_id = zones[0]["id"]
            dns = await self._request(session, "GET", f"/zones/{zone_id}/dns_records") or []

            # Normalize CF records to the planner's relative shape (sub = the
            # part before the apex), so a takeover/marker mode works the same as
            # for aliyun's RR. CF stores the FQDN as ``name``; strip the apex.
            apex = self.domain
            suffix = "." + apex

            def _sub(name: str) -> str:
                n = str(name or "").rstrip(".").lower()
                if n == apex:
                    return "@"
                if n.endswith(suffix):
                    return n[: -len(suffix)]
                return n

            existing = [
                {
                    "name": _sub(r.get("name")),
                    "type": r.get("type"),
                    "value": r.get("content"),
                    "comment": r.get("comment"),
                    "id": r.get("id"),
                }
                for r in dns
            ]
            plan = plan_reconcile(
                existing, domain=self.domain, names=self.names,
                ipv4=ipv4, ipv6=ipv6, comment=self.comment or None, types=self.types,
            )
            changes: list[dict] = []
            for action in plan:
                sub = action["record"]
                full = apex if sub == "@" else f"{sub}.{apex}"
                ip_type = action["type"]
                ip = action["ip"]
                payload = {
                    "type": ip_type,
                    "name": full,
                    "content": ip,
                    "proxied": self.proxied,
                }
                if self.comment:
                    payload["comment"] = self.comment
                if action["action"] == "added":
                    await self._request(session, "POST", f"/zones/{zone_id}/dns_records", json=payload)
                    changes.append({"action": "added", "record": full, "type": ip_type, "ip": ip})
                elif action["action"] == "updated":
                    await self._request(
                        session, "PUT",
                        f"/zones/{zone_id}/dns_records/{action['id']}", json=payload,
                    )
                    changes.append({"action": "updated", "record": full, "type": ip_type, "ip": ip})
                elif action["action"] == "adopted":
                    # An identical record already exists (no marker). A POST would
                    # be rejected as a duplicate, so PUT the same content plus our
                    # marker. Not a resolution change → not reported / notified.
                    if self.comment:
                        await self._request(
                            session, "PUT",
                            f"/zones/{zone_id}/dns_records/{action['id']}", json=payload,
                        )
                else:  # deleted
                    await self._request(
                        session, "DELETE",
                        f"/zones/{zone_id}/dns_records/{action['id']}",
                    )
                    changes.append({"action": "deleted", "record": full, "type": ip_type, "ip": ip})
            return changes


# ── Aliyun provider ───────────────────────────────────────────────────────


class AliyunProvider:
    """Sync A/AAAA records via the Aliyun DNS SDK (synchronous, threaded).

    ``aliyunsdkcore`` is imported lazily so the module imports without the
    dependency; each SDK round-trip runs in the default executor to keep the
    event loop free.
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.domain = str(config.get("domain") or "")
        self.names = list(config.get("names") or [])
        # Selected DNS types (A/AAAA). Missing → both (pre-types records).
        self.types = list(config.get("types") or _DEFAULT_TYPES)
        # Managed marker (== remark on the record). Empty = takeover mode.
        self.comment = str(config.get("comment") or "").strip()

    @staticmethod
    def _build_client(config: dict):
        try:
            from aliyunsdkcore.client import AcsClient
        except ImportError as exc:
            raise DdnsSyncError(
                "缺少阿里云 SDK（pip install aliyun-python-sdk-core "
                "aliyun-python-sdk-alidns；Docker 部署需重建镜像）"
            ) from exc

        return AcsClient(
            str(config.get("access_key_id") or ""),
            str(config.get("access_key_secret") or ""),
            str(config.get("region_id") or "cn-hangzhou"),
        )

    def _do(self, client, request) -> dict:
        import json as _json

        return _json.loads(client.do_action_with_exception(request))

    async def sync(self, ipv4: str | None, ipv6: str | None) -> list[dict]:
        if not self.config.get("access_key_id") or not self.config.get("access_key_secret"):
            raise DdnsSyncError("Aliyun 配置缺少 access_key_id 或 access_key_secret")
        try:
            from aliyunsdkalidns.request.v20150109 import (
                AddDomainRecordRequest,
                DeleteDomainRecordRequest,
                DescribeDomainRecordsRequest,
                UpdateDomainRecordRemarkRequest,
                UpdateDomainRecordRequest,
            )
        except ImportError as exc:
            # A missing dependency is an operator-actionable condition, not a
            # code bug: name the package and both install paths (local venv vs
            # a rebuilt image), so the record's error column says what to do
            # instead of surfacing a bare ModuleNotFoundError.
            raise DdnsSyncError(
                "缺少阿里云 DNS SDK（"
                "pip install aliyun-python-sdk-core aliyun-python-sdk-alidns；"
                "Docker 部署需重建镜像后重启主服务与穿透服务）"
            ) from exc

        loop = asyncio.get_event_loop()
        client = await loop.run_in_executor(None, self._build_client, self.config)

        async def _run(request, label: str) -> dict:
            try:
                return await loop.run_in_executor(None, self._do, client, request)
            except Exception as exc:  # noqa: BLE001 — surface as a sync failure, not a crash
                raise DdnsSyncError(f"Aliyun {label} 失败: {exc}") from exc

        req = DescribeDomainRecordsRequest.DescribeDomainRecordsRequest()
        req.set_DomainName(self.domain)
        req.set_PageSize(500)
        res = await _run(req, "DescribeDomainRecords")
        records = (res.get("DomainRecords", {}).get("Record") or [])

        existing = [
            {
                # Aliyun RRs are case-insensitive: normalize so a hand-created
                # "WWW" matches the configured "www" (otherwise the planner
                # would add a second record and trip DomainRecordDuplicate).
                "name": str(r.get("RR") or "").strip().rstrip(".").lower(),
                "type": str(r.get("Type") or "").strip().upper(),
                "value": r.get("Value"),
                "remark": r.get("Remark"),
                "id": str(r.get("RecordId") or ""),
            }
            for r in records
        ]
        by_id = {r["id"]: r for r in existing}
        plan = plan_reconcile(
            existing, domain=self.domain, names=self.names,
            ipv4=ipv4, ipv6=ipv6, comment=self.comment or None, types=self.types,
        )

        changes: list[dict] = []
        remarked: set[str] = set()  # record ids already refreshed this run

        async def _ensure_remark(record_id: str, current: str) -> None:
            """Set the marker remark, only when it actually differs
            ("comment 不更新则不调用")."""
            if not self.comment or not record_id or record_id in remarked:
                return
            if str(current or "").strip() == self.comment:
                return
            await self._set_remark(loop, client, UpdateDomainRecordRemarkRequest, record_id, self.comment)
            remarked.add(record_id)

        for action in plan:
            sub = action["record"]
            ip_type = action["type"]
            ip = action["ip"]
            kind = action["action"]
            if kind == "added":
                a_req = AddDomainRecordRequest.AddDomainRecordRequest()
                a_req.set_DomainName(self.domain)
                a_req.set_RR(sub)
                a_req.set_Type(ip_type)
                a_req.set_Value(ip)
                resp = await _run(a_req, "AddDomainRecord")
                # A brand-new record has no remark yet.
                await _ensure_remark(str(resp.get("RecordId") or ""), "")
                changes.append({"action": "added", "record": sub, "type": ip_type, "ip": ip})
            elif kind == "adopted":
                # An identical (RR, Type, Value) record already exists but is
                # unmanaged: Add would fail with DomainRecordDuplicate, so just
                # stamp the marker onto the existing record. No DNS change —
                # this never shows up in the change list / notification.
                await _ensure_remark(action["id"], (by_id.get(action["id"]) or {}).get("remark"))
            elif kind == "updated":
                u_req = UpdateDomainRecordRequest.UpdateDomainRecordRequest()
                u_req.set_RecordId(action["id"])
                u_req.set_RR(sub)
                u_req.set_Type(ip_type)
                u_req.set_Value(ip)
                await _run(u_req, "UpdateDomainRecord")
                row = by_id.get(action["id"]) or {}
                await _ensure_remark(action["id"], row.get("remark"))
                changes.append({"action": "updated", "record": sub, "type": ip_type, "ip": ip})
            else:  # deleted
                d_req = DeleteDomainRecordRequest.DeleteDomainRecordRequest()
                d_req.set_RecordId(action["id"])
                await _run(d_req, "DeleteDomainRecord")
                changes.append({"action": "deleted", "record": sub, "type": ip_type, "ip": ip})

        # Marker maintenance sweep: platform records (marked) whose remark
        # drifted from the configured comment — e.g. the comment was edited —
        # are re-marked even when their value needed no change. Takeover mode
        # (no comment) never marks. Selected types only: a deselected family's
        # records were deleted by the plan above, so re-marking them would
        # target a record that no longer exists.
        if self.comment:
            names_set = set(self.names)
            addrs = {"A": ipv4, "AAAA": ipv6}
            active_types = {t for t in self.types if addrs.get(t)}
            for row in existing:
                if row["type"] not in active_types or row["name"] not in names_set:
                    continue
                if not str(row.get("remark") or "").strip():
                    continue  # 备注了的 only
                await _ensure_remark(row["id"], row.get("remark"))
        return changes

    async def _set_remark(self, loop, client, request_cls, record_id: str, remark: str) -> None:
        r_req = request_cls.UpdateDomainRecordRemarkRequest()
        r_req.set_RecordId(record_id)
        r_req.set_Remark(remark)
        try:
            await loop.run_in_executor(None, self._do, client, r_req)
        except Exception as exc:  # noqa: BLE001 — a remark failure must not undo the value write
            logger.warning("[ddns] Aliyun UpdateDomainRecordRemark failed for {}: {}", record_id, exc)


def _provider_for(record: Any, config: dict):
    """Build the provider writer for ``record`` with its **merged** config.

    ``config`` is the caller's flattened blob (credentials from the provider
    profile + domain/names from the record — see
    ``ddns_service.effective_config``); the record itself no longer carries
    credentials.
    """
    provider = (record.provider or "").strip().lower()
    if provider == "aliyun":
        return AliyunProvider(config)
    if provider == "cloudflare":
        return CloudflareProvider(config)
    raise DdnsSyncError(f"未知 provider: {provider!r}")


# ── one full record sync ──────────────────────────────────────────────────


async def run_sync(
    record: Any, config: dict | None = None
) -> tuple[str | None, str | None, list[dict]]:
    """Acquire the IP and write the provider records. Returns the result.

    ``config`` is the merged provider-credentials + domain-targets blob; when
    omitted it is read back off the record's own ``config`` (only the domain
    targets — the caller is expected to pass credentials). Raises
    :class:`DdnsSyncError` on any failure (caller turns it into a
    ``last_status=failed`` + notification); an empty change list means the DNS
    was already correct (a successful no-op).
    """
    if config is None:
        config = dict(record.config or {})
    ipv4, ipv6 = await acquire_ip(record)
    if not ipv4 and not ipv6:
        raise DdnsSyncError("未能获取到任何有效公网 IP（v4/v6 全空）")
    changes = await _provider_for(record, config).sync(ipv4, ipv6)
    return ipv4, ipv6, changes


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _emit(
    event_type: str, record: Any, *, severity: str, title: str,
    message: str, params: dict, dedupe: str | None,
) -> None:
    """Fire one DDNS notification. Never raises — emit_notification is best-effort
    (and writes the outbox over the Tortoise connection, so it works in both the
    main service and the tunnel_server process)."""
    try:
        from .notify_core import emit_notification

        await emit_notification(
            event_type,
            params=params,
            owner_type="user",
            owner_id=str(record.user_id),
            severity=severity,
            kind="ddns",
            source="ddns",
            title=title,
            message=message,
            dedupe_key=dedupe,
        )
    except Exception:  # noqa: BLE001 — notification is a side effect, never fatal
        logger.exception("[ddns] notification emit failed for {}", record.id)


def _change_summary(changes: list[dict]) -> str:
    verbs = {"updated": "更新", "added": "新增", "deleted": "删除"}
    parts = []
    for c in changes:
        verb = verbs.get(c.get("action"), c.get("action"))
        parts.append(f"{verb} {c.get('record')} ({c.get('type')}) → {c.get('ip')}")
    return "；".join(parts)


async def run_record(record: Any) -> dict:
    """One full sync for a (persisted) record: acquire IP + provider write, then
    persist the status columns and notify on a real change or failure.

    Credentials live on the owning provider profile, so the merged config is
    loaded first (``ddns_service.effective_config``). Returns a small result
    dict for the caller (the sync route reads it; the scheduler ignores it).
    All failures are caught here so the scheduler loop never dies — they become
    ``last_status=failed`` + a notification.
    """
    label = record.name or str(record.id)

    async def _fail(message: str, title: str, *, unexpected: bool = False) -> dict:
        if unexpected:
            logger.exception("[ddns] unexpected sync error for {}", record.id)
        record.last_status = "failed"
        record.last_error = message[:2000]
        record.last_sync_at = _utcnow()
        await record.save(update_fields=["last_status", "last_error", "last_sync_at", "updated_at"])
        await _emit(
            "ddns.failed", record,
            severity="error", title=title, message=f"「{label}」{message}",
            params={"record_id": str(record.id), "name": label, "provider": record.provider,
                    "error": message, "ip_source": record.ip_source},
            dedupe=f"ddns-failed:{record.id}",
        )
        return {"ok": False, "last_status": "failed", "last_error": message,
                "last_ipv4": record.last_ipv4, "last_ipv6": record.last_ipv6, "last_changes": []}

    # Merge credentials (provider profile) with targets (this record). A missing
    # profile is a config error, not a crash.
    from . import ddns_service

    try:
        config = await ddns_service.effective_config(record)
    except Exception as exc:  # noqa: BLE001 — e.g. provider row hard-deleted
        return await _fail(f"同步失败：{exc}", "DDNS 同步失败")

    try:
        ipv4, ipv6, changes = await run_sync(record, config)
    except DdnsSyncError as exc:
        return await _fail(f"同步失败：{exc}", "DDNS 同步失败")
    except Exception as exc:  # noqa: BLE001 — an unexpected error must not kill the loop
        return await _fail(f"同步异常：{type(exc).__name__}: {exc}", "DDNS 同步异常", unexpected=True)

    record.last_ipv4 = ipv4
    record.last_ipv6 = ipv6
    record.last_sync_at = _utcnow()
    record.last_status = "ok"
    record.last_error = None
    record.last_changes = changes[:200]
    await record.save(update_fields=[
        "last_ipv4", "last_ipv6", "last_sync_at", "last_status", "last_error",
        "last_changes", "updated_at",
    ])
    logger.info(
        "[ddns] sync ok: id={} provider={} ip_source={} v4={} v6={} changes={}",
        record.id, record.provider, record.ip_source, ipv4, ipv6, len(changes),
    )
    if changes:
        await _emit(
            "ddns.changed", record,
            severity="info", title="DDNS 解析变更",
            message=f"「{label}」({_change_summary(changes)})",
            params={"record_id": str(record.id), "name": label, "provider": record.provider,
                    "ipv4": ipv4, "ipv6": ipv6, "changed": len(changes)},
            dedupe=None,
        )
    return {
        "ok": True, "last_status": "ok", "last_error": None,
        "last_ipv4": ipv4, "last_ipv6": ipv6, "last_changes": changes,
    }

