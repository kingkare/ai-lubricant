"""Tortoise models for the DDNS manager.

Two levels, mirroring the tunnel manager's scheme → binding split:

* :class:`DdnsProvider` — a reusable *provider credential profile* (one 阿里云
  account or Cloudflare API token). Created first; its ``config`` holds only
  credentials and is masked on the way out (same contract as
  :mod:`.tunnel_service`).
* :class:`DdnsRecord` — one DDNS task under a provider: which domain/subdomains
  to publish and **where the public IP comes from** (``ip_source``). The owning
  provider's kind is denormalized onto ``provider`` for display; credentials
  never live here.

IP sources (``ip_source``):

* ``server``     — the machine this process runs on; probe via the platform's
  global IP-lookup URL list (``GetPublicIPLookupConfig``) locally.
* ``node``       — an execution node's heartbeat-reported public IP
  (``capabilities.public_ip`` / ``public_ipv6``); zero probing.
* ``node_proxy`` — run a one-shot probe command on a node via ``host_exec``
  (optionally through a user-supplied proxy URL) and parse its stdout.

Sync state columns are written back by the scheduler in the tunnel_server
process (or the on-demand "sync now" route in the main service).

Tables use the ``mc_`` prefix to live in the compatibility app's namespace
alongside the other platform domain models.
"""
from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class DdnsProvider(Model):
    """A reusable provider credential profile (the admin-configured pool).

    ``config`` is a JSONB blob whose shape depends on ``provider`` and holds
    **only credentials** (masked on read):

    * ``aliyun``     — ``{access_key_id, access_key_secret, region_id?}``
    * ``cloudflare`` — ``{api_token}``

    Domain/sub-domain targets and CF write options (``proxied``/``comment``)
    live on each :class:`DdnsRecord`, so one provider account can back many
    domains.
    """

    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField()  # owner
    name = fields.CharField(max_length=128)
    provider = fields.CharField(max_length=32)  # aliyun | cloudflare
    config = fields.JSONField(default=dict)
    enabled = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_ddns_providers"
        indexes = (("user_id",), ("provider",))


class DdnsRecord(Model):
    """One DDNS task under a provider: domain targets + an IP source."""

    id = fields.UUIDField(pk=True)
    user_id = fields.UUIDField()  # owner
    # Owning provider profile. Immutable: change it via delete+create.
    provider_id = fields.UUIDField()
    # Denormalized from the provider row at create time (display only; the
    # provider row is the truth for kind + credentials).
    provider = fields.CharField(max_length=32)
    name = fields.CharField(max_length=128)

    # server | node | node_proxy
    ip_source = fields.CharField(max_length=16, default="server")
    # node/node_proxy: which execution node. server: must be empty.
    node_id = fields.CharField(max_length=128, null=True)
    # server/node_proxy optional outbound proxy URL (http/https/socks5) used
    # only while fetching the public IP. Never a proxy-pool reference.
    proxy = fields.CharField(max_length=512, null=True)

    # Domain targets + provider write options (no secrets here):
    #   {domain, names: [], types?: ["A","AAAA"], proxied?, comment?}
    #   types: which DNS families to publish (A=IPv4 / AAAA=IPv6); missing → both.
    #   (proxied/comment: cloudflare / marker-only)
    config = fields.JSONField(default=dict)

    interval_seconds = fields.IntField(default=120)
    enabled = fields.BooleanField(default=True)

    # ── sync state (written back by the scheduler / sync route) ──────────────
    last_ipv4 = fields.CharField(max_length=64, null=True)
    last_ipv6 = fields.CharField(max_length=64, null=True)
    last_sync_at = fields.DatetimeField(null=True)
    # pending | ok | failed
    last_status = fields.CharField(max_length=32, default="pending")
    last_error = fields.TextField(null=True)
    # [{action: updated|added, record: <name>, type: A|AAAA, ip: <addr>}]
    last_changes = fields.JSONField(null=True)

    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "mc_ddns_records"
        indexes = (("user_id",), ("provider_id",), ("enabled",), ("node_id",))
