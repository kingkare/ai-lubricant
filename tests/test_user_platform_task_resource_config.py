"""Task resource config resolution + redaction + merge (Phase A1).

Pure-logic tests for the canonical Task resource path: the DTO never leaks
secret-bearing wire-spec keys (tokens, headers, env), the MCP base merges with
the per-task overlay so issue-workflow survives a hot-update, ``resource_id``
refs are folded from ``extra.skill_ids``/``extra.plugin_ids`` and unauthorized
ids are rejected (→ 403) before persistence, and audit bodies carry only
per-kind counts. No DB needed — the service helpers operate on plain dicts.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from user_platform import routes_task
from user_platform import task_service as task_service_module
from user_platform.task_service import (
    TaskService,
    _merge_mcp_config,
    _redact_resource_config,
)


# ---- redaction: secrets and URL token never leave the service -------------

def test_redact_strips_secret_keys_and_url_token():
    redacted = _redact_resource_config([
        {
            "name": "issue-workflow",
            "type": "sse",
            "url": "https://host/mcp/issue-workflow/sse?token=secret-abc&keep=1",
            "token": "secret-abc",
            "headers": {"Authorization": "Bearer x"},
            "env": {"KEY": "v"},
        },
        {"name": "plain", "url": "https://host/no-token"},
    ])
    first = redacted[0]
    assert "token" not in first
    assert "headers" not in first
    assert "env" not in first
    assert "token=" not in first["url"]
    assert "keep=1" in first["url"]  # non-token query params preserved
    assert redacted[1]["url"] == "https://host/no-token"


def test_redact_handles_non_list_and_non_dict_entries():
    assert _redact_resource_config(None) == []
    assert _redact_resource_config([None, "x", 5, {"name": "ok"}]) == [{"name": "ok"}]


# ---- merge: overlay wins, issue-workflow survives base update -------------

def test_merge_overlay_wins_over_base_by_name():
    merged = _merge_mcp_config(
        [{"name": "shared", "url": "https://old"}, {"name": "issue-workflow", "url": "https://base"}],
        [{"name": "issue-workflow", "url": "https://overlay-token"}],
    )
    by_name = {item["name"]: item for item in merged}
    assert by_name["shared"]["url"] == "https://old"
    assert by_name["issue-workflow"]["url"] == "https://overlay-token"


def test_merge_keeps_unnamed_entries_prepended():
    merged = _merge_mcp_config([{"url": "https://unnamed"}], [{"name": "x"}])
    assert merged[0]["url"] == "https://unnamed"
    assert merged[1]["name"] == "x"


# ---- resolver: skill_ids/plugin_ids folded + unauthorized rejected --------

def _user():
    return SimpleNamespace(id="user-1", role="user")


def _request():
    # request.base_url is a property on Starlette Request; the helper reads
    # ``str(request.base_url).rstrip("/")``.
    return SimpleNamespace(base_url="https://api.test/")


async def _fake_team_id(_uid: str) -> str:
    return "team-1"


@pytest.mark.asyncio
async def test_resolve_dedups_by_name(monkeypatch):
    async def fake_normalize(user_id, team_id, bindings):
        # normalize 返回服务绑定（service_id / resource_id），按绑定键去重。
        seen = set()
        out = []
        for b in bindings:
            key = str(b.get("service_id") or b.get("resource_id") or b.get("name") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append({"service_id": 1} if not b.get("resource_id") else dict(b))
        return out

    from user_platform.resource_reference_service import normalize_mcp_bindings

    monkeypatch.setattr(routes_task, "normalize_mcp_bindings", fake_normalize)
    monkeypatch.setattr(routes_task, "resolve_team_id", _fake_team_id)

    req = {"mcp_config": [{"name": "dup"}, {"name": "dup"}]}
    patch = await routes_task._resolve_task_resource_configs(_user(), req, _request())
    assert len(patch["mcp_config"]) == 1  # 同服务两条 = 一条绑定
    assert patch["mcp_config"][0] == {"service_id": 1}


# ---- audit: only counts, never full wire specs ---------------------------

def test_audit_replaces_config_lists_with_counts():
    """skill_config/plugin_config 已退役，审计只剩 mcp_config 计数。"""
    audited = routes_task._audit_resource_counts({
        "content": "hi",
        "mcp_config": [{"name": "a"}, {"name": "b"}],
        "title": "t",
    })
    assert audited["mcp_config"] == 2
    assert audited["content"] == "hi"
    assert audited["title"] == "t"


@pytest.mark.asyncio
@pytest.mark.parametrize(("load_session", "fresh"), [(True, False), (False, True)])
async def test_restart_task_runtime_maps_load_session_to_fresh(monkeypatch, load_session, fresh):
    task = SimpleNamespace(id="task-1", user_id="user-1", mcp_user_id=None)
    calls = []

    async def owned(*_args, **_kwargs):
        return task

    async def live(bound_task):
        assert bound_task is task
        return "session-1"

    class Client:
        async def restart_node_session_runtime(self, session_id, *, fresh=False):
            calls.append((session_id, fresh))
            return {"ok": True}

    # restart re-enables the task principal before resuming; stub it so the
    # minimal fixture does not need a live DB / mcp_plugin_store.
    async def enable(self, bound_task):
        return None

    service = TaskService()
    monkeypatch.setattr(service, "_owned_task_or_raise", owned)
    monkeypatch.setattr(service, "_live_node_session_id", live)
    monkeypatch.setattr(TaskService, "_enable_task_mcp_principal", enable)
    monkeypatch.setattr(task_service_module, "get_local_node_client", lambda: Client())

    result = await service.restart_task_runtime(
        "user-1", "task-1", load_session=load_session
    )

    assert calls == [("session-1", fresh)]
    assert result["load_session"] is load_session
    assert result["detail"] == {"ok": True}


# ---- resolve_reference_specs: service_id 绑定（内置/平台/个人 MCP 直选） ----


class _FakeConn:
    """Stub asyncpg connection carrying one mcp_services row."""

    def __init__(self, row):
        self.row = row

    async def fetchrow(self, query, *params):
        if "FROM mcp_services" in query:
            return self.row if self.row is not None else None
        raise AssertionError(f"unexpected query: {query}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_service_id_binding_resolves_wire_spec(monkeypatch):
    """{service_id} 绑定 → 读 mcp_services 行 + 授权校验 → 节点可用的 wire spec。"""
    import mcp_plugin_store
    import db as db_module
    from user_platform import resource_reference_service as rrs

    async def can_use(*, user_id, service_id, groups=None):
        return user_id == "user-1" and service_id == 42

    monkeypatch.setattr(mcp_plugin_store, "can_use_service", can_use)

    async def no_refs(*_a, **_kw):
        return []

    monkeypatch.setattr(rrs, "visible_references", no_refs)
    conn = _FakeConn({
        "id": 42, "name": "cdp-bridge", "transport": "remote", "command": "",
        "args": [], "env_template": {}, "url": "http://127.0.0.1:9693/sse",
        "headers": {"X-K": "v"}, "enabled": True,
    })
    monkeypatch.setattr(db_module.PostgresClient, "pool", _FakePool(conn))

    specs = await rrs.resolve_reference_specs(
        "user-1", "team-1", "mcp",
        [{"service_id": 42}, {"name": "legacy", "command": "echo"}],
    )

    assert specs[0] == {
        "name": "cdp-bridge",
        "type": "remote",
        "transport": "remote",
        "command": "",
        "args": [],
        # env/headers 是 repeated EnvVarSpec——必须是 [{name, value}] 列表。
        # 历史字典形态会被控制面 JSON 解析拒绝（repeated field must be in a
        # list），派发整体失败；行里存的 env_template/headers 字典在产出时转换。
        "env": [],
        "url": "http://127.0.0.1:9693/sse",
        "headers": [{"name": "X-K", "value": "v"}],
    }
    # 无 resource_id/service_id 的旧形态原样透传。
    assert specs[1] == {"name": "legacy", "command": "echo"}


@pytest.mark.asyncio
async def test_service_id_binding_rejects_unauthorized(monkeypatch):
    """未授权的服务 → 明确报错，绝不静默丢弃或透传浏览器摘要。"""
    import mcp_plugin_store
    from user_platform import resource_reference_service as rrs

    async def can_use(*, user_id, service_id, groups=None):
        return False

    monkeypatch.setattr(mcp_plugin_store, "can_use_service", can_use)

    async def no_refs(*_a, **_kw):
        return []

    monkeypatch.setattr(rrs, "visible_references", no_refs)

    with pytest.raises(ValueError, match="mcp_not_authorized:42"):
        await rrs.resolve_reference_specs("user-1", "team-1", "mcp", [{"service_id": 42}])


@pytest.mark.asyncio
async def test_service_id_binding_rejects_disabled_service(monkeypatch):
    """已停用的服务不可挂载（mcp_not_ready），与团队引用口径一致。"""
    import mcp_plugin_store
    import db as db_module
    from user_platform import resource_reference_service as rrs

    async def can_use(*, user_id, service_id, groups=None):
        return True

    monkeypatch.setattr(mcp_plugin_store, "can_use_service", can_use)

    async def no_refs(*_a, **_kw):
        return []

    monkeypatch.setattr(rrs, "visible_references", no_refs)
    monkeypatch.setattr(db_module.PostgresClient, "pool", _FakePool(_FakeConn(None)))

    with pytest.raises(ValueError, match="mcp_not_ready:42"):
        await rrs.resolve_reference_specs("user-1", "team-1", "mcp", [{"service_id": 42}])


# ---- _task_mcp_service_ids: retired (Phase 4) ----
# The legacy ``_task_mcp_service_ids`` / ``_mcp_service_ids_from_specs`` helpers
# read the retired ``mcp_config`` column to derive authorized service ids. With
# the column gone and the service-grant truth now projected from the environment
# (``_sync_task_mcp_service_grants_from_env``), this entry point and its test
# are removed together.
