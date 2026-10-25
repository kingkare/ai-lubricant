"""DDNS manager: validation + masking + IP acquisition + provider syncs + scheduler.

Pure logic — no network, no real Postgres: validation/masking run against plain
dicts; acquire_ip and the providers run over monkey-patched node client /
aiohttp request stubs / the aliyun SDK import surface. The scheduler runs on an
in-memory sqlite with the DDNS model registered.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest
import pytest_asyncio
from tortoise import Tortoise

from user_platform import ddns_service as svc
from user_platform import ddns_sync as sync


# ── provider profile validation ───────────────────────────────────────────


def _cred(provider, **extra):
    base = {"access_key_id": "id", "access_key_secret": "sec"} if provider == "aliyun" else {"api_token": "tok"}
    base.update(extra)
    return base


def _target(provider, **extra):
    base = {"domain": "example.com", "names": ["@"]}
    if provider == "cloudflare":
        base.update({"proxied": False, "comment": "ddns"})
    base.update(extra)
    return base


def test_provider_rejects_unknown_kind():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_provider_config("rackspace", _cred("aliyun"))


def test_provider_aliyun_requires_credentials():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_provider_config("aliyun", {"access_key_id": "id"})
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_provider_config("cloudflare", {})


def test_provider_aliyun_defaults_region():
    config = svc.validate_provider_config("aliyun", _cred("aliyun"))
    assert config["region_id"] == "cn-hangzhou"


# ── record validation ─────────────────────────────────────────────────────


def test_validate_node_source_requires_node_id():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="aliyun", ip_source="node", node_id=None, proxy=None,
            config=_target("aliyun"),
        )


def test_validate_node_proxy_requires_node_id():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="cloudflare", ip_source="node_proxy", node_id=None, proxy=None,
            config=_target("cloudflare"),
        )


def test_validate_server_source_rejects_node_id():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="aliyun", ip_source="server", node_id="node-1", proxy=None,
            config=_target("aliyun"),
        )


def test_validate_rejects_unknown_ip_source():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="aliyun", ip_source="moon", node_id=None, proxy=None,
            config=_target("aliyun"),
        )


def test_validate_normalizes_domain_and_dedupes_names():
    ip_source, proxy, config = svc.validate_record_shape(
        provider="cloudflare", ip_source="server", node_id=None, proxy=None,
        config=_target("cloudflare", domain="Example.COM.", names=["@", "www", "www", "  "],
                       proxied=True, comment="c"),
    )
    assert ip_source == "server" and proxy is None
    assert config["domain"] == "example.com"
    assert config["names"] == ["@", "www"]
    assert config["proxied"] is True


def test_validate_aliyun_drops_proxied_keeps_comment():
    # proxied is Cloudflare-only; comment is the managed marker (kept on both).
    _, _, config = svc.validate_record_shape(
        provider="aliyun", ip_source="server", node_id=None, proxy=None,
        config=_target("aliyun", proxied=True, comment="mymark"),
    )
    assert "proxied" not in config and config["comment"] == "mymark"


def test_validate_comment_defaults_to_empty_not_ddns():
    # The marker default is empty (takeover), never "ddns" — otherwise takeover
    # mode (adopt user records in the list) could never be reached.
    _, _, config = svc.validate_record_shape(
        provider="aliyun", ip_source="server", node_id=None, proxy=None,
        config=_target("aliyun"),
    )
    assert "comment" not in config, "empty comment must not be stored at all"


def test_validate_requires_names():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="cloudflare", ip_source="server", node_id=None, proxy=None,
            config={"domain": "example.com", "names": []},
        )


def test_validate_types_defaults_to_both():
    # Records created before ``types`` existed keep managing both families.
    _, _, config = svc.validate_record_shape(
        provider="aliyun", ip_source="server", node_id=None, proxy=None,
        config=_target("aliyun"),
    )
    assert config["types"] == ["A", "AAAA"]


def test_validate_types_normalizes_upper_and_dedupes():
    _, _, config = svc.validate_record_shape(
        provider="aliyun", ip_source="server", node_id=None, proxy=None,
        config=_target("aliyun", types=["aaaa", "A", "a", "AAAA", "A"]),
    )
    assert config["types"] == ["A", "AAAA"]


def test_validate_types_rejects_unknown_and_empty():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="aliyun", ip_source="server", node_id=None, proxy=None,
            config=_target("aliyun", types=["CNAME"]),
        )
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="aliyun", ip_source="server", node_id=None, proxy=None,
            config=_target("aliyun", types=[]),
        )


def test_validate_proxy_url_shape():
    _, proxy, _ = svc.validate_record_shape(
        provider="cloudflare", ip_source="server", node_id=None,
        proxy="http://user:pass@host:3128", config=_target("cloudflare"),
    )
    assert proxy == "http://user:pass@host:3128"


def test_validate_rejects_bad_proxy():
    with pytest.raises(svc.DdnsServiceError):
        svc.validate_record_shape(
            provider="cloudflare", ip_source="server", node_id=None,
            proxy="not-a-url", config=_target("cloudflare"),
        )


def test_validate_node_source_drops_proxy():
    # A proxy on a "node" record is meaningless → silently dropped, not rejected.
    _, proxy, _ = svc.validate_record_shape(
        provider="cloudflare", ip_source="node", node_id="n1",
        proxy="http://host:3128", config=_target("cloudflare"),
    )
    assert proxy is None


# ── masking ───────────────────────────────────────────────────────────────


def test_public_config_masks_secrets():
    out = svc._public_config({
        "access_key_id": "LTAIxyz", "access_key_secret": "supersecret",
        "api_token": "abcdef0123456789", "region_id": "cn-hangzhou",
    })
    assert out["api_token"].startswith("***") and out["api_token"].endswith("6789")
    assert out["access_key_secret"].startswith("***")
    assert out["access_key_id"] == "LTAIxyz", "access_key_id must not be masked"
    assert out["region_id"] == "cn-hangzhou"


def test_merge_masked_secrets_keeps_stored_cleartext():
    stored = {"api_token": "real-token-123456", "access_key_secret": "real-sec"}
    incoming = {"api_token": svc.mask_secret("real-token-123456")}
    merged = svc._merge_masked_secrets(incoming, stored)
    assert merged["api_token"] == "real-token-123456", "masked form means unchanged"


def test_record_dict_does_not_mask_targets():
    # Record config carries no secrets, so it must pass through unmasked.
    row = types.SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111", provider_id="22222222-2222-2222-2222-222222222222",
        provider="cloudflare", name="r", ip_source="server", node_id=None, proxy=None,
        config={"domain": "e.com", "names": ["@"], "proxied": True, "comment": "c"},
        interval_seconds=120, enabled=True, last_ipv4=None, last_ipv6=None,
        last_sync_at=None, last_status="pending", last_error=None, last_changes=None,
        created_at=None, updated_at=None,
    )
    out = svc._record_dict(row)
    assert out["config"]["domain"] == "e.com"
    assert out["provider_id"] == "22222222-2222-2222-2222-222222222222"


# ── resolve_ip_lookup_urls ────────────────────────────────────────────────


def _patch_client(monkeypatch, client):
    import user_platform.node_client as nc

    monkeypatch.setattr(nc, "get_local_node_client", lambda: client)


@pytest.mark.asyncio
async def test_resolve_urls_accepts_camel_case(monkeypatch):
    class _C:
        async def get_public_ip_lookup_config(self):
            return {"revision": 1, "ipv4Urls": ["https://a.test/ip"], "ipv6Urls": ["https://b.test/ip"]}
    _patch_client(monkeypatch, _C())
    v4, v6 = await sync.resolve_ip_lookup_urls()
    assert v4 == ["https://a.test/ip"]
    assert v6 == ["https://b.test/ip"]


@pytest.mark.asyncio
async def test_resolve_urls_falls_back_when_empty(monkeypatch):
    class _C:
        async def get_public_ip_lookup_config(self):
            return {}
    _patch_client(monkeypatch, _C())
    v4, v6 = await sync.resolve_ip_lookup_urls()
    assert v4 == sync.FALLBACK_IPV4_URLS
    assert v6 == sync.FALLBACK_IPV6_URLS


@pytest.mark.asyncio
async def test_resolve_urls_swallows_rpc_failure(monkeypatch):
    class _C:
        async def get_public_ip_lookup_config(self):
            raise RuntimeError("control plane down")
    _patch_client(monkeypatch, _C())
    v4, v6 = await sync.resolve_ip_lookup_urls()
    assert v4 and v6, "fallback must be returned, never raised"


# ── server-source acquire_ip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_server_uses_get_best_ip(monkeypatch):
    seen: dict[str, Any] = {}

    async def fake_best(urls, reg, proxy=None):
        seen.setdefault("proxies", []).append(proxy)
        return "1.2.3.4" if urls == ["https://v4.test"] else "::1"

    async def fake_resolve():
        return (["https://v4.test"], ["https://v6.test"])

    monkeypatch.setattr(sync, "get_best_ip", fake_best)
    monkeypatch.setattr(sync, "resolve_ip_lookup_urls", fake_resolve)

    rec = types.SimpleNamespace(ip_source="server", node_id=None, proxy="http://px:3128")
    ipv4, ipv6 = await sync.acquire_ip(rec)
    assert (ipv4, ipv6) == ("1.2.3.4", "::1")
    assert seen["proxies"] == ["http://px:3128", "http://px:3128"]


# ── node-source acquire_ip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_node_reads_reported_ip(monkeypatch):
    class _C:
        async def list_nodes(self):
            return [{"node_id": "n1", "online": True,
                     "capabilities": {"public_ip": "9.9.9.9", "public_ipv6": "fe80::1"}}]
    _patch_client(monkeypatch, _C())
    rec = types.SimpleNamespace(ip_source="node", node_id="n1", proxy=None)
    assert await sync.acquire_ip(rec) == ("9.9.9.9", "fe80::1")


@pytest.mark.asyncio
async def test_acquire_node_missing_ip_raises(monkeypatch):
    class _C:
        async def list_nodes(self):
            return [{"node_id": "n1", "online": True, "capabilities": {}}]
    _patch_client(monkeypatch, _C())
    rec = types.SimpleNamespace(ip_source="node", node_id="n1", proxy=None)
    with pytest.raises(sync.DdnsSyncError):
        await sync.acquire_ip(rec)


@pytest.mark.asyncio
async def test_acquire_node_unknown_raises(monkeypatch):
    class _C:
        async def list_nodes(self):
            return []
    _patch_client(monkeypatch, _C())
    rec = types.SimpleNamespace(ip_source="node", node_id="ghost", proxy=None)
    with pytest.raises(sync.DdnsSyncError):
        await sync.acquire_ip(rec)


# ── node_proxy-source acquire_ip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_node_proxy_parses_stdout(monkeypatch):
    class _C:
        async def list_nodes(self):
            return [{"node_id": "n1", "online": True,
                     "capabilities": {"host_exec": "true", "os": "linux"}}]

        async def host_exec(self, node_id, command, *, cwd="", timeout_ms=0, max_output_bytes=0):
            return {"stdout": "5.5.5.5\n", "stderr": "", "exit_code": 0, "success": True}
    _patch_client(monkeypatch, _C())

    async def fake_resolve():
        return (["https://v4.test"], ["https://v6.test"])
    monkeypatch.setattr(sync, "resolve_ip_lookup_urls", fake_resolve)

    rec = types.SimpleNamespace(ip_source="node_proxy", node_id="n1", proxy=None)
    ipv4, ipv6 = await sync.acquire_ip(rec)
    assert ipv4 == "5.5.5.5"
    assert ipv6 is None


@pytest.mark.asyncio
async def test_acquire_node_proxy_requires_host_exec(monkeypatch):
    class _C:
        async def list_nodes(self):
            return [{"node_id": "n1", "online": True, "capabilities": {"os": "linux"}}]
    _patch_client(monkeypatch, _C())
    async def fake_resolve():
        return (["https://v4.test"], ["https://v6.test"])
    monkeypatch.setattr(sync, "resolve_ip_lookup_urls", fake_resolve)
    rec = types.SimpleNamespace(ip_source="node_proxy", node_id="n1", proxy=None)
    with pytest.raises(sync.DdnsSyncError):
        await sync.acquire_ip(rec)


def test_probe_command_posix_has_proxy():
    cmd = sync.probe_command(["https://a.test/ip"], "http://u:p@host:3128", 4, is_windows=False)
    assert "curl" in cmd and "--proxy" in cmd and "-4" in cmd
    assert "https://a.test/ip" in cmd


def test_probe_command_windows_chains_curl():
    cmd = sync.probe_command(["https://a.test/ip", "https://b.test/ip"], None, 6, is_windows=True)
    assert cmd.count("curl") == 2 and "&" in cmd and "-6" in cmd
    assert "--proxy" not in cmd


# ── reconciliation plan (pure) ────────────────────────────────────────────


def _row(rid, name, rtype, value, remark=None):
    return {"id": rid, "name": name, "type": rtype, "value": value, "remark": remark}


def test_plan_marker_mode_updates_adds_and_ignores_unmarked():
    # comment set: unmarked user records are never touched; marked stale → updated
    # (whatever the old remark text); missing → added.
    existing = [
        _row("1", "@", "A", "9.9.9.9", remark="old-note"),   # marked, stale → updated
        _row("2", "www", "A", "8.8.8.8", remark=None),       # unmarked user record → untouched
        _row("3", "www", "A", "7.7.7.7", remark="ddns"),     # marked, stale → updated
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["@", "www"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    actions = {(p["action"], p["record"], p["type"]) for p in plan}
    assert actions == {("updated", "@", "A"), ("updated", "www", "A")}, plan
    updated_ids = {p["id"] for p in plan if p["action"] == "updated"}
    assert updated_ids == {"1", "3"}, "the unmarked user record is never updated"


def test_plan_marker_mode_deletes_stale_marked_leftovers():
    # A marked record outside the names list is a platform leftover → deleted.
    existing = [
        _row("1", "old", "A", "5.5.5.5", remark="ddns"),
        _row("2", "user", "A", "6.6.6.6", remark=None),  # unmarked → untouched
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["home"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    deleted = [p for p in plan if p["action"] == "deleted"]
    assert [(d["record"], d["type"], d["id"]) for d in deleted] == [("old", "A", "1")]
    added = [p for p in plan if p["action"] == "added"]
    assert [(a["record"], a["type"]) for a in added] == [("home", "A")]


def test_plan_takeover_adopts_user_records_in_the_list():
    # comment empty: every A/AAAA whose sub is in names is adopted — user
    # records included — pointed at the current IP; extras collapse.
    existing = [
        _row("1", "www", "A", "9.9.9.9"),          # user record inside the list → adopted+updated
        _row("2", "mail", "A", "7.7.7.7"),         # outside the list → untouched
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment=None,
    )
    assert [(p["action"], p["record"], p["type"]) for p in plan] == [("updated", "www", "A")]


def test_plan_takeover_never_touches_outside_subdomains():
    existing = [_row("1", "mail", "A", "7.7.7.7"), _row("2", "_dmarc", "TXT", "v=1")]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment=None,
    )
    assert [(p["action"], p["record"]) for p in plan] == [("added", "www")]
    assert not any(p["record"] in ("mail", "_dmarc") for p in plan)


def test_plan_collapses_duplicates():
    existing = [
        _row("1", "www", "A", "9.9.9.9", remark="ddns"),
        _row("2", "www", "A", "6.6.6.6", remark="ddns"),  # duplicate → deleted
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    deleted = [p for p in plan if p["action"] == "deleted"]
    assert [(d["id"]) for d in deleted] == ["2"]
    updated = [p for p in plan if p["action"] == "updated"]
    assert [(u["record"], u["ip"]) for u in updated] == [("www", "8.8.8.8")]


def test_plan_leaves_families_without_an_ip_alone():
    # No IPv6 this run → the marked AAAA must NOT be deleted (transient probe
    # failure would otherwise wipe the family). IPv4 still works normally.
    existing = [
        _row("1", "www", "AAAA", "fe80::1", remark="ddns"),
        _row("2", "www", "A", "9.9.9.9", remark="ddns"),  # stale → updated
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    actions = [(p["action"], p["record"], p["type"]) for p in plan]
    assert actions == [("updated", "www", "A")], plan
    assert not any(p["type"] == "AAAA" for p in plan), "AAAA untouched without an IPv6"


def test_plan_adopts_identical_unmarked_record_instead_of_adding():
    """Regression: aliyun/CF reject a duplicate (name,type,value) triple
    (DomainRecordDuplicate). An unmarked record that already points at the
    target IP must be adopted (marker stamped), never re-created."""
    existing = [_row("1", "www", "A", "8.8.8.8", remark=None)]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    assert [(p["action"], p["record"], p["type"], p["id"]) for p in plan] == [
        ("adopted", "www", "A", "1")
    ], plan


def test_plan_adds_when_no_record_has_the_target_value():
    # Same sub+type but a different value → a second (multi-value) record is a
    # legitimate add; only an identical triple would be rejected.
    existing = [_row("1", "www", "A", "9.9.9.9", remark=None)]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    assert [(p["action"], p["record"]) for p in plan] == [("added", "www")]


def test_plan_ignores_provider_record_case():
    """A hand-created "WWW" record is the same RR as the configured "www" —
    matching must be case-insensitive or the planner adds a duplicate."""
    existing = [_row("1", "WWW", "A", "8.8.8.8", remark=None)]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment="ddns",
    )
    assert [(p["action"], p["record"]) for p in plan] == [("adopted", "www")], plan


def test_plan_takeover_never_emits_adopted():
    # Takeover mode already manages every row in the list, so an exact-value
    # record is updated-or-kept, never adopted.
    existing = [_row("1", "www", "A", "8.8.8.8", remark=None)]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6=None, comment=None,
    )
    assert plan == [], plan


def test_plan_noop_when_everything_matches():
    existing = [
        _row("1", "www", "A", "8.8.8.8", remark="ddns"),
        _row("2", "www", "AAAA", "::1", remark="ddns"),
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns",
    )
    assert plan == [], plan


# ── types (DNS family selection) ──────────────────────────────────────────


def test_plan_types_a_only_never_adds_aaaa():
    """Regression for "added an IPv4, next sync also added an IPv6": a record
    limited to type A must not publish an AAAA even when an IPv6 resolved."""
    existing = [_row("1", "www", "A", "9.9.9.9", remark="ddns")]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns", types=["A"],
    )
    # The A is updated to the new IP; no AAAA action at all.
    assert [(p["action"], p["record"], p["type"]) for p in plan] == [("updated", "www", "A")], plan


def test_plan_types_aaaa_only_never_adds_a():
    existing = []
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns", types=["AAAA"],
    )
    assert [(p["action"], p["record"], p["type"]) for p in plan] == [("added", "www", "AAAA")], plan


def test_plan_types_both_publishes_both():
    plan = sync.plan_reconcile(
        [], domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns", types=["A", "AAAA"],
    )
    assert {(p["type"], p["action"]) for p in plan} == {("A", "added"), ("AAAA", "added")}


def test_plan_types_missing_defaults_to_both():
    # Pre-types records (no ``types`` key) keep managing both families.
    plan = sync.plan_reconcile(
        [], domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns", types=None,
    )
    assert {(p["type"]) for p in plan} == {"A", "AAAA"}


def test_plan_types_a_only_leaves_existing_aaaa_alone():
    # types narrowed A-only AFTER an AAAA was created: the AAAA is out of scope.
    # Marker mode → deleted (platform leftover cleanup); no add/update for it.
    existing = [
        _row("1", "www", "A", "8.8.8.8", remark="ddns"),
        _row("2", "www", "AAAA", "::1", remark="ddns"),
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns", types=["A"],
    )
    actions = [(p["action"], p["type"], p["id"]) for p in plan]
    assert ("deleted", "AAAA", "2") in actions, plan
    assert not any(t == "AAAA" and a != "deleted" for a, t, _ in actions)


def test_plan_types_takeover_never_touches_deselected_family():
    # Takeover mode treats a deselected family as out of scope (consistent with
    # "sub-domains outside names are never touched") — user AAAA records stay.
    existing = [
        _row("1", "www", "A", "9.9.9.9"),
        _row("2", "www", "AAAA", "::1"),
    ]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment=None, types=["A"],
    )
    assert [(p["action"], p["record"], p["type"]) for p in plan] == [("updated", "www", "A")], plan


def test_plan_types_deselected_family_not_adopted_even_if_identical():
    # The AAAA matches what we'd publish, but the record is A-only: adopting
    # (stamping the marker) would drag it back under management. The A is still
    # added (it doesn't exist); the AAAA is neither adopted nor updated.
    existing = [_row("1", "www", "AAAA", "::1", remark=None)]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4="8.8.8.8", ipv6="::1", comment="ddns", types=["A"],
    )
    assert [(p["action"], p["record"], p["type"]) for p in plan] == [("added", "www", "A")], plan


def test_plan_types_a_only_transients_still_protected():
    # A-only record whose IPv4 probe failed this run: nothing to publish, and
    # the family protection (leave-alone) applies as before.
    existing = [_row("1", "www", "A", "9.9.9.9", remark="ddns")]
    plan = sync.plan_reconcile(
        existing, domain="example.com", names=["www"],
        ipv4=None, ipv6="::1", comment="ddns", types=["A"],
    )
    assert plan == [], plan


# ── cloudflare provider ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cloudflare_skips_unchanged_and_updates_diff(monkeypatch):
    prov = sync.CloudflareProvider(
        {"domain": "example.com", "names": ["@", "www"],
         "api_token": "tok", "proxied": False, "comment": "ddns"}
    )
    dns = [
        {"id": "r1", "name": "example.com", "type": "A", "content": "1.1.1.1", "comment": "ddns"},
        {"id": "r2", "name": "www.example.com", "type": "A", "content": "9.9.9.9", "comment": "ddns"},
    ]
    mutations: list[tuple[str, str, dict]] = []

    async def fake_request(session, method, path, **kw):
        if method == "GET" and path == "/zones":
            return [{"id": "z1", "name": "example.com"}]
        if method == "GET":
            return dns
        mutations.append((method, path, dict(kw.get("json") or {})))
        return {"id": "new"}

    monkeypatch.setattr(prov, "_request", fake_request)
    changes = await prov.sync(ipv4="1.1.1.1", ipv6=None)
    # apex A matches (no-op); www A differs (updated); no AAAA (v6 None).
    assert [c["action"] for c in changes] == ["updated"]
    assert changes[0]["record"] == "www.example.com"
    assert len(mutations) == 1 and mutations[0][0] == "PUT"


@pytest.mark.asyncio
async def test_cloudflare_adds_missing(monkeypatch):
    prov = sync.CloudflareProvider(
        {"domain": "example.com", "names": ["api"],
         "api_token": "tok", "proxied": False, "comment": "ddns"}
    )

    async def fake_request(session, method, path, **kw):
        if method == "GET" and path == "/zones":
            return [{"id": "z1", "name": "example.com"}]
        if method == "GET":
            return []
        return {"id": "new"}
    monkeypatch.setattr(prov, "_request", fake_request)

    changes = await prov.sync(ipv4="8.8.8.8", ipv6="::1")
    assert {(c["record"], c["type"], c["action"]) for c in changes} == {
        ("api.example.com", "A", "added"),
        ("api.example.com", "AAAA", "added"),
    }


@pytest.mark.asyncio
async def test_cloudflare_adopts_instead_of_posting_duplicate(monkeypatch):
    """Same regression for CF: an identical unmarked record is PUT (marker
    stamped), never POSTed (which CF rejects as a duplicate)."""
    prov = sync.CloudflareProvider(
        {"domain": "example.com", "names": ["www"],
         "api_token": "tok", "proxied": False, "comment": "ddns"}
    )
    dns = [{"id": "r1", "name": "www.example.com", "type": "A", "content": "8.8.8.8", "comment": ""}]
    mutations: list[tuple[str, str]] = []

    async def fake_request(session, method, path, **kw):
        if method == "GET" and path == "/zones":
            return [{"id": "z1", "name": "example.com"}]
        if method == "GET":
            return dns
        mutations.append((method, path))
        return {"id": "x"}

    monkeypatch.setattr(prov, "_request", fake_request)
    changes = await prov.sync(ipv4="8.8.8.8", ipv6=None)
    assert changes == [], "adopting is not a resolution change"
    # exactly one PUT (stamp marker), no POST.
    assert mutations == [("PUT", "/zones/z1/dns_records/r1")], mutations


# ── aliyun provider ───────────────────────────────────────────────────────


class _AliyunReq:
    """Stand-in for the SDK request objects: records the set_* calls."""

    def __init__(self):
        self.fields: dict[str, Any] = {}

    def set_DomainName(self, v): self.fields["DomainName"] = v
    def set_PageSize(self, v): self.fields["PageSize"] = v
    def set_RecordId(self, v): self.fields["RecordId"] = v
    def set_RR(self, v): self.fields["RR"] = v
    def set_Type(self, v): self.fields["Type"] = v
    def set_Value(self, v): self.fields["Value"] = v
    def set_Remark(self, v): self.fields["Remark"] = v
    def set_Lang(self, v): self.fields["Lang"] = v


def _install_fake_alidns(monkeypatch):
    """Make ``from aliyunsdkalidns.request.v20150109 import (...)`` resolve to
    modules whose same-named attribute is :class:`_AliyunReq`. ``_build_client``
    and ``_do`` are patched separately, so aliyunsdkcore never runs."""

    def _inner(name):
        m = types.ModuleType(name)
        setattr(m, name.rsplit(".", 1)[-1], _AliyunReq)
        return m

    pkg = types.ModuleType("aliyunsdkalidns.request.v20150109")
    pkg.DescribeDomainRecordsRequest = _inner(
        "aliyunsdkalidns.request.v20150109.DescribeDomainRecordsRequest")
    pkg.UpdateDomainRecordRequest = _inner(
        "aliyunsdkalidns.request.v20150109.UpdateDomainRecordRequest")
    pkg.AddDomainRecordRequest = _inner(
        "aliyunsdkalidns.request.v20150109.AddDomainRecordRequest")
    pkg.DeleteDomainRecordRequest = _inner(
        "aliyunsdkalidns.request.v20150109.DeleteDomainRecordRequest")
    pkg.UpdateDomainRecordRemarkRequest = _inner(
        "aliyunsdkalidns.request.v20150109.UpdateDomainRecordRemarkRequest")

    for name, mod in {
        "aliyunsdkalidns": types.ModuleType("aliyunsdkalidns"),
        "aliyunsdkalidns.request": types.ModuleType("aliyunsdkalidns.request"),
        "aliyunsdkalidns.request.v20150109": pkg,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.mark.asyncio
async def test_aliyun_marker_mode_full_flow(monkeypatch):
    """Marker mode (comment set): unmarked user record untouched, marked stale
    updated (remark refreshed only when it differs), marked duplicate collapsed."""
    _install_fake_alidns(monkeypatch)
    prov = sync.AliyunProvider(
        {"domain": "example.com", "names": ["@", "www"],
         "access_key_id": "id", "access_key_secret": "sec", "region_id": "cn-hangzhou",
         "comment": "ddns"}
    )
    describe = {"DomainRecords": {"Record": [
        {"RecordId": "100", "RR": "@", "Type": "A", "Value": "9.9.9.9", "Remark": "ddns"},
        {"RecordId": "200", "RR": "www", "Type": "A", "Value": "8.8.8.8", "Remark": None},
        {"RecordId": "300", "RR": "www", "Type": "A", "Value": "7.7.7.7", "Remark": "ddns"},
        {"RecordId": "400", "RR": "mail", "Type": "A", "Value": "6.6.6.6", "Remark": None},
    ]}}

    monkeypatch.setattr(prov, "_build_client", lambda cfg: object(), raising=False)
    calls: list[dict] = []

    def fake_do(client, request):
        calls.append(request.fields)
        if len(calls) == 1:
            return describe
        return {"RecordId": "500"} if "Value" in request.fields else {}

    monkeypatch.setattr(prov, "_do", fake_do)

    changes = await prov.sync(ipv4="8.8.8.8", ipv6=None)
    actions = {(c["action"], c["record"]) for c in changes}
    # "@" (100): marked stale → updated. "www": 300 marked stale → updated;
    # 200 unmarked (user record) → never touched. "mail": unmarked → untouched.
    assert actions == {("updated", "@"), ("updated", "www")}, changes
    assert not any(c["record"] == "mail" for c in changes)

    # value-write: Update for 100 and 300. No remark calls (both already marked
    # with the configured comment → comment 不更新则不调用).
    updates = [c for c in calls if "Value" in c and "Remark" not in c]
    remarks = [c for c in calls if "Remark" in c]
    assert sorted(u["RecordId"] for u in updates) == ["100", "300"]
    assert remarks == [], "marks already match the configured comment"


@pytest.mark.asyncio
async def test_aliyun_add_sets_remark_once(monkeypatch):
    """A brand-new record has no remark → UpdateDomainRecordRemark is called
    once with the marker; subsequent syncs with the matching remark skip it."""
    _install_fake_alidns(monkeypatch)
    prov = sync.AliyunProvider(
        {"domain": "example.com", "names": ["api"],
         "access_key_id": "id", "access_key_secret": "sec", "region_id": "cn-hangzhou",
         "comment": "ddns"}
    )
    monkeypatch.setattr(prov, "_build_client", lambda cfg: object(), raising=False)
    calls: list[dict] = []

    def fake_do(client, request):
        calls.append(request.fields)
        if len(calls) == 1:
            return {"DomainRecords": {"Record": []}}
        if "Remark" in request.fields:
            return {}
        return {"RecordId": "900"}  # add response carries the new RecordId
    monkeypatch.setattr(prov, "_do", fake_do)

    changes = await prov.sync(ipv4="8.8.8.8", ipv6=None)
    assert [(c["action"], c["record"]) for c in changes] == [("added", "api")]
    adds = [c for c in calls if "Value" in c and "Remark" not in c]
    remarks = [c for c in calls if "Remark" in c]
    assert len(adds) == 1 and adds[0]["RR"] == "api"
    assert len(remarks) == 1 and remarks[0]["RecordId"] == "900" and remarks[0]["Remark"] == "ddns"


@pytest.mark.asyncio
async def test_aliyun_takeover_adopts_user_records(monkeypatch):
    """comment empty (takeover): user records inside the list are adopted and
    pointed at the current IP; outside the list untouched; no remark calls."""
    _install_fake_alidns(monkeypatch)
    prov = sync.AliyunProvider(
        {"domain": "example.com", "names": ["www"],
         "access_key_id": "id", "access_key_secret": "sec", "region_id": "cn-hangzhou"}
    )
    describe = {"DomainRecords": {"Record": [
        {"RecordId": "100", "RR": "www", "Type": "A", "Value": "9.9.9.9", "Remark": "old"},
        {"RecordId": "200", "RR": "mail", "Type": "A", "Value": "7.7.7.7", "Remark": None},
    ]}}
    monkeypatch.setattr(prov, "_build_client", lambda cfg: object(), raising=False)
    calls: list[dict] = []

    def fake_do(client, request):
        calls.append(request.fields)
        return describe if len(calls) == 1 else {}
    monkeypatch.setattr(prov, "_do", fake_do)

    changes = await prov.sync(ipv4="8.8.8.8", ipv6=None)
    assert [(c["action"], c["record"]) for c in changes] == [("updated", "www")]
    updates = [c for c in calls if "Value" in c and "Remark" not in c]
    assert len(updates) == 1 and updates[0]["RecordId"] == "100" and updates[0]["Value"] == "8.8.8.8"
    assert not any("Remark" in c for c in calls), "takeover mode never calls remark"
    assert not any(c.get("RecordId") == "200" for c in calls), "outside the list untouched"


@pytest.mark.asyncio
async def test_aliyun_never_adds_when_identical_record_exists(monkeypatch):
    """Regression for DomainRecordDuplicate: an unmarked record already pointing
    at the target IP is adopted (remark stamped) — AddDomainRecord is never
    called, so the provider can never answer 400 DomainRecordDuplicate."""
    _install_fake_alidns(monkeypatch)
    prov = sync.AliyunProvider(
        {"domain": "example.com", "names": ["www"],
         "access_key_id": "id", "access_key_secret": "sec", "region_id": "cn-hangzhou",
         "comment": "ddns"}
    )
    describe = {"DomainRecords": {"Record": [
        {"RecordId": "100", "RR": "www", "Type": "A", "Value": "8.8.8.8", "Remark": None},
    ]}}
    monkeypatch.setattr(prov, "_build_client", lambda cfg: object(), raising=False)
    calls: list[dict] = []

    def fake_do(client, request):
        calls.append(request.fields)
        return describe if len(calls) == 1 else {}
    monkeypatch.setattr(prov, "_do", fake_do)

    changes = await prov.sync(ipv4="8.8.8.8", ipv6=None)
    assert changes == [], "adopting an identical record is not a DNS change"
    # One Describe + one remark stamp; no Add, no Update, no Delete.
    assert len(calls) == 2, calls
    assert "Value" not in calls[1], "Add/Update must not be issued for an adopted record"
    assert calls[1].get("RecordId") == "100" and calls[1].get("Remark") == "ddns"


@pytest.mark.asyncio
async def test_aliyun_refreshes_remark_when_comment_changed(monkeypatch):
    """A marked record whose remark differs from the configured comment gets the
    remark refreshed (comment 变了 → 重新打标记), with no DNS value change."""
    _install_fake_alidns(monkeypatch)
    prov = sync.AliyunProvider(
        {"domain": "example.com", "names": ["www"],
         "access_key_id": "id", "access_key_secret": "sec", "region_id": "cn-hangzhou",
         "comment": "newmark"}
    )
    describe = {"DomainRecords": {"Record": [
        {"RecordId": "100", "RR": "www", "Type": "A", "Value": "8.8.8.8", "Remark": "oldmark"},
    ]}}
    monkeypatch.setattr(prov, "_build_client", lambda cfg: object(), raising=False)
    calls: list[dict] = []

    def fake_do(client, request):
        calls.append(request.fields)
        return describe if len(calls) == 1 else {}
    monkeypatch.setattr(prov, "_do", fake_do)

    # Value already correct → only the remark is refreshed, which is not a DNS change.
    changes = await prov.sync(ipv4="8.8.8.8", ipv6=None)
    assert changes == []
    remarks = [c for c in calls if "Remark" in c]
    assert len(remarks) == 1 and remarks[0]["Remark"] == "newmark" and remarks[0]["RecordId"] == "100"


@pytest.mark.asyncio
async def test_aliyun_types_a_only_skips_aaaa_writes(monkeypatch):
    """End-to-end through the aliyun provider: types=["A"] must publish no AAAA
    even though an IPv6 was probed — the original bug at the provider level."""
    _install_fake_alidns(monkeypatch)
    prov = sync.AliyunProvider(
        {"domain": "example.com", "names": ["www"],
         "access_key_id": "id", "access_key_secret": "sec", "region_id": "cn-hangzhou",
         "comment": "ddns", "types": ["A"]}
    )
    describe = {"DomainRecords": {"Record": [
        {"RecordId": "100", "RR": "www", "Type": "A", "Value": "9.9.9.9", "Remark": "ddns"},
    ]}}
    monkeypatch.setattr(prov, "_build_client", lambda cfg: object(), raising=False)
    calls: list[dict] = []

    def fake_do(client, request):
        calls.append(request.fields)
        return describe if len(calls) == 1 else {}
    monkeypatch.setattr(prov, "_do", fake_do)

    changes = await prov.sync(ipv4="8.8.8.8", ipv6="::1")
    assert [(c["action"], c["type"]) for c in changes] == [("updated", "A")]
    writes = [c for c in calls if "Value" in c]
    assert all(c.get("Type") == "A" for c in writes), calls
    assert len(writes) == 1, "only the stale A is rewritten; no AAAA call at all"


# ── scheduler + two-level service (sqlite) ───────────────────────────────


@pytest_asyncio.fixture
async def db():
    await Tortoise.init(
        config={
            "connections": {"user_platform": "sqlite://:memory:"},
            "apps": {"user_platform": {
                "models": ["user_platform.models_ddns"], "default_connection": "user_platform",
            }},
        },
        use_tz=False,
        _enable_global_fallback=True,
    )
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _stub_run_record(monkeypatch, ran: list[str]):
    async def fake_record(record):
        ran.append(str(record.id))
        return {"ok": True, "last_status": "ok", "last_changes": []}

    monkeypatch.setattr(sync, "run_record", fake_record)


UID = "00000000-0000-0000-0000-000000000001"


async def _make_provider(**kw) -> str:
    row = await svc.create_provider(
        UID, name=kw.get("name", "acct"), provider=kw.get("provider", "cloudflare"),
        config=_cred(kw.get("provider", "cloudflare")), enabled=kw.get("enabled", True),
    )
    return row["id"]


async def _make_record(provider_id: str, **kw) -> str:
    row = await svc.create_record(
        UID, provider_id=provider_id, name=kw.get("name", "r"),
        ip_source=kw.get("ip_source", "server"), node_id=kw.get("node_id"),
        proxy=kw.get("proxy"), config=_target("cloudflare"),
        interval_seconds=kw.get("interval_seconds", 120), enabled=kw.get("enabled", True),
    )
    return row["id"]


@pytest.mark.asyncio
async def test_scheduler_skips_not_due(db, monkeypatch):
    from datetime import datetime, timezone

    from user_platform.ddns_scheduler import DdnsScheduler
    from user_platform.models_ddns import DdnsRecord

    pid = await _make_provider()
    await _make_record(pid)
    # Just-synced with a 1h interval → not due.
    await DdnsRecord.filter(provider_id=pid).update(
        interval_seconds=3600, last_sync_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    ran: list[str] = []
    _stub_run_record(monkeypatch, ran)
    await DdnsScheduler().tick()
    assert ran == [], "a record synced 1s ago with a 1h interval must be skipped"


@pytest.mark.asyncio
async def test_scheduler_runs_due_and_disabled(db, monkeypatch):
    from user_platform.ddns_scheduler import DdnsScheduler

    pid = await _make_provider()
    due = await _make_record(pid, name="due", interval_seconds=60)
    await _make_record(pid, name="off", interval_seconds=60, enabled=False)
    ran: list[str] = []
    _stub_run_record(monkeypatch, ran)
    await DdnsScheduler().tick()
    assert ran == [due], "only the enabled, due record runs"


@pytest.mark.asyncio
async def test_scheduler_skips_disabled_provider(db, monkeypatch):
    """A record under a disabled profile is skipped, never run (no failure spam)."""
    from user_platform.ddns_scheduler import DdnsScheduler
    from user_platform.models_ddns import DdnsProvider

    pid = await _make_provider()
    await _make_record(pid, interval_seconds=60)
    await DdnsProvider.filter(id=pid).update(enabled=False)
    ran: list[str] = []
    _stub_run_record(monkeypatch, ran)
    await DdnsScheduler().tick()
    assert ran == [], "records under a disabled provider must not run"


@pytest.mark.asyncio
async def test_record_denormalizes_provider_kind(db):
    """create_record copies the profile's kind onto the record (display) and
    refuses an unknown / disabled provider."""
    pid = await _make_provider(provider="aliyun")
    row = await svc.create_record(
        UID, provider_id=pid, name="r", ip_source="server",
        config={"domain": "example.com", "names": ["@"]},
    )
    assert row["provider"] == "aliyun"
    # aliyun records never keep cloudflare write options.
    assert "proxied" not in row["config"] and "api_token" not in row["config"]

    with pytest.raises(svc.DdnsServiceError):
        await svc.create_record(
            UID, provider_id="33333333-3333-3333-3333-333333333333", name="x",
            ip_source="server", config={"domain": "e.com", "names": ["@"]},
        )


@pytest.mark.asyncio
async def test_effective_config_merges_credentials_and_targets(db):
    """The sync blob = profile credentials + record targets, spliced."""
    pid = await _make_provider(provider="aliyun")
    rid = await _make_record(pid)
    from user_platform.models_ddns import DdnsRecord

    row = await DdnsRecord.get(id=rid)
    merged = await svc.effective_config(row)
    assert merged["access_key_id"] == "id" and merged["access_key_secret"] == "sec"
    assert merged["region_id"] == "cn-hangzhou"
    assert merged["domain"] == "example.com" and merged["names"] == ["@"]


@pytest.mark.asyncio
async def test_delete_provider_blocked_by_records(db):
    pid = await _make_provider()
    await _make_record(pid)
    with pytest.raises(svc.DdnsServiceError) as exc:
        await svc.delete_provider(UID, pid)
    assert exc.value.code == "in_use"
    # After the record is gone, the profile deletes.
    rows = await svc.list_records(UID, provider_id=pid)
    await svc.delete_record(UID, rows[0]["id"])
    assert (await svc.delete_provider(UID, pid))["deleted"] is True


@pytest.mark.asyncio
async def test_update_provider_keeps_masked_secret(db):
    pid = await _make_provider(provider="cloudflare")
    # The client round-trips the masked form → the stored cleartext survives.
    masked = (await svc.get_provider(UID, pid))["config"]["api_token"]
    assert "***" in masked
    updated = await svc.update_provider(UID, pid, config={"api_token": masked})
    assert updated["config"]["api_token"] == masked  # still masked on the way out

    from user_platform.models_ddns import DdnsProvider

    stored = await DdnsProvider.get(id=pid)
    assert stored.config["api_token"] == "tok", "mask must not overwrite the real secret"


# ── pre-release schema reshape migration ──────────────────────────────────


class _MigrateConn:
    """Fake Tortoise connection scripting the information_schema answers."""

    def __init__(self, columns: list[dict], count: int = 0):
        self._columns = columns
        self._count = count
        self.scripts: list[str] = []

    async def execute_query_dict(self, sql, values=None):
        if "information_schema" in sql:
            return self._columns
        if "COUNT(*)" in sql:
            return [{"n": self._count}]
        return []

    async def execute_script(self, sql):
        self.scripts.append(sql)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "columns,expect_drop",
    [
        ([], False),  # fresh database: nothing to do
        ([{"column_name": "provider_id"}, {"column_name": "config"}], False),  # new shape
        ([{"column_name": "provider"}, {"column_name": "config"}], True),  # old shape
    ],
)
async def test_ddns_reshape_migration(monkeypatch, columns, expect_drop):
    from tortoise import Tortoise

    from user_platform.database import _migrate_ddns_two_level

    conn = _MigrateConn(columns, count=2 if expect_drop else 0)
    monkeypatch.setattr(Tortoise, "get_connection", lambda _name: conn)
    await _migrate_ddns_two_level()
    drops = [s for s in conn.scripts if "DROP TABLE" in s]
    assert bool(drops) is expect_drop
    if expect_drop:
        assert "mc_ddns_records" in drops[0]
