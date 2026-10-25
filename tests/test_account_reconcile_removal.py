"""账号对账反向清理 + ACCOUNT_FIELDS 清空同步单测。

覆盖两处修复：

1. _reconcile_providers 原先只遍历 DB 账号做字段对齐，不移除「DB 已删、运行池还留着」的
   账号。丢一条 EVENT_ACCOUNT 删除事件的实例会无限期继续拿已删账号发请求（更新方向有
   对账兜底，删除方向没有）。现在按 DB 账号名集合反向清池，并在真要删时直连 PG 复核一次，
   避免 add_account「先写 DB 再入池」与对账快照之间的竞态导致误删新账号。

2. _apply_account_update_in_place 对代码渠道 spec 自声明字段（ACCOUNT_FIELDS）原先用
   `new_val not in (None, "")` 守卫，把值从有改成空时内存对象保留旧值，且 60s 对账走同一
   守卫也修不回来——只有重启才生效。通用凭据字段（_ACCOUNT_CREDENTIAL_KEYS）仍保持
   非空守卫：update_account 的 secret 保留逻辑确保它们在 DB 里不会为空，跟着清会误删
   仍然有效的 OAuth token。
"""
from __future__ import annotations

import asyncio

import pytest

import admin
import runtime_handlers


class _ProviderStub:
    """带 spec 自声明账号字段的运行时 provider 替身。"""

    ACCOUNT_FIELDS = ("enterprise_id", "region")

    def __init__(self, username: str):
        self.username = username
        self.password = "pw"
        self.proxy = None
        self.proxy_config_id = ""
        self.url_prefix = None
        self.enterprise_id = "ent-old"
        self.region = "cn"
        self.access_token = "tok-old"
        self._channel = None

    def attach_channel(self, channel):
        self._channel = channel


class _AccountClientStub:
    def __init__(self, username: str):
        self.provider = _ProviderStub(username)
        self.username = username
        self.rpm_limit = 0
        self.tpm_limit = 0
        self.concurrent_limit = 0
        # 到量冻结维度：rph/tph/rpd/tpd 就地更新也会写（与建池同口径）。
        self.rph_limit = 0
        self.tph_limit = 0
        self.rpd_limit = 0
        self.tpd_limit = 0
        self.balance = None
        self.balance_threshold = 0.0
        self.disabled = False
        self.disable_reason = ""
        self.metadata = {}


class _PoolStub:
    def __init__(self, usernames: list[str]):
        self.clients = [_AccountClientStub(u) for u in usernames]
        self.channel = None
        self.client_class = _ProviderStub


def _usernames(pool: _PoolStub) -> list[str]:
    return [c.username for c in pool.clients]


@pytest.fixture
def reconcile_env(monkeypatch):
    """把 _reconcile_providers 的外部依赖全部替换成内存替身。

    返回一个 setup(snapshot_accounts, pool_usernames, db_accounts) 闭包：
    snapshot_accounts 模拟对账快照，db_accounts 模拟复核时直连 PG 读到的权威值
    （传 None 表示渠道行已不存在）。
    """
    import config

    def setup(snapshot_accounts, pool_usernames, db_accounts, db_raises=False):
        pool = _PoolStub(pool_usernames)
        cfg = {"accounts": snapshot_accounts, "rate_limit": {}}

        async def _noop_reload():
            return {}

        async def _get_providers():
            return {"p1": cfg}

        async def _read_main():
            return {}

        monkeypatch.setattr(config.Config, "reload_from_db", classmethod(lambda cls: _noop_reload()))
        monkeypatch.setattr(config.Config, "get_providers", classmethod(lambda cls: _get_providers()))
        monkeypatch.setattr(config.CONFIG_STORE, "read_main_async", _read_main)
        monkeypatch.setattr(admin, "_read_proxies", lambda: [])
        monkeypatch.setattr(
            admin.ModelClientPool,
            "get_provider_pool",
            classmethod(lambda cls, name: pool if name == "p1" else None),
        )
        # 反向清理只关心 pool.clients 的增删，不牵扯 Redis 冻结键清理与模型刷新。
        removed: list[str] = []

        def _remove(provider_name, username):
            removed.append(username)
            pool.clients = [c for c in pool.clients if c.username != username]

        monkeypatch.setattr(admin, "_remove_account_from_pool", _remove)

        from db import PostgresClient

        async def _get_provider_config(name):
            if db_raises:
                raise RuntimeError("PG 不可用")
            return None if db_accounts is None else {"accounts": db_accounts}

        monkeypatch.setattr(PostgresClient, "get_provider_config", staticmethod(_get_provider_config))

        # 代理池对账另有单测覆盖，这里短路掉。
        async def _noop_proxy():
            return None

        monkeypatch.setattr(runtime_handlers, "_refresh_proxy_runtime", _noop_proxy)
        return pool, removed

    return setup


def test_reconcile_removes_account_deleted_from_db(reconcile_env):
    """DB 只剩 u1，运行池还有 u2（丢了删除事件）→ 对账移除 u2。"""
    pool, removed = reconcile_env(
        snapshot_accounts=[{"username": "u1"}],
        pool_usernames=["u1", "u2"],
        db_accounts=[{"username": "u1"}],
    )

    asyncio.run(runtime_handlers._reconcile_providers())

    assert removed == ["u2"]
    assert _usernames(pool) == ["u1"]


def test_reconcile_clears_pool_when_all_accounts_deleted(reconcile_env):
    """渠道账号被清空 → 运行池也要清空，不能留残账号继续发请求。"""
    pool, removed = reconcile_env(
        snapshot_accounts=[],
        pool_usernames=["u1", "u2"],
        db_accounts=[],
    )

    asyncio.run(runtime_handlers._reconcile_providers())

    assert sorted(removed) == ["u1", "u2"]
    assert _usernames(pool) == []


def test_reconcile_keeps_account_confirmed_by_fresh_db_read(reconcile_env):
    """竞态：快照没有 u2 但它其实刚被 add_account 写进 DB → 复核确认后不得误删。"""
    pool, removed = reconcile_env(
        snapshot_accounts=[{"username": "u1"}],
        pool_usernames=["u1", "u2"],
        db_accounts=[{"username": "u1"}, {"username": "u2"}],
    )

    asyncio.run(runtime_handlers._reconcile_providers())

    assert removed == []
    assert _usernames(pool) == ["u1", "u2"]


def test_reconcile_skips_removal_when_provider_row_gone(reconcile_env):
    """渠道行整体消失 → 整渠道退场由 channel 事件负责，对账不越权清池。"""
    pool, removed = reconcile_env(
        snapshot_accounts=[],
        pool_usernames=["u1"],
        db_accounts=None,
    )

    asyncio.run(runtime_handlers._reconcile_providers())

    assert removed == []
    assert _usernames(pool) == ["u1"]


def test_reconcile_skips_removal_when_confirm_read_fails(reconcile_env):
    """复核读 DB 失败 → 宁可留着也不误删，下一轮对账再判。"""
    pool, removed = reconcile_env(
        snapshot_accounts=[{"username": "u1"}],
        pool_usernames=["u1", "u2"],
        db_accounts=[{"username": "u1"}],
        db_raises=True,
    )

    asyncio.run(runtime_handlers._reconcile_providers())

    assert removed == []
    assert _usernames(pool) == ["u1", "u2"]


def test_reconcile_no_confirm_read_when_pool_matches_db(reconcile_env, monkeypatch):
    """无 stale 时不触发复核读，常态零额外 DB 开销。"""
    pool, removed = reconcile_env(
        snapshot_accounts=[{"username": "u1"}],
        pool_usernames=["u1"],
        db_accounts=[{"username": "u1"}],
    )
    from db import PostgresClient

    calls: list[str] = []
    original = PostgresClient.get_provider_config

    async def _counting(name):
        calls.append(name)
        return await original(name)

    monkeypatch.setattr(PostgresClient, "get_provider_config", staticmethod(_counting))

    asyncio.run(runtime_handlers._reconcile_providers())

    assert calls == []
    assert removed == []


def _apply_in_place(monkeypatch, client, runtime_acc: dict) -> bool:
    """在事件循环内跑 _apply_account_update_in_place，返回是否触发了重认证。

    凭据变化会 asyncio.create_task(_refresh_models_after_account_init(...))，既需要
    运行中的 loop，也不该在单测里真去拉模型列表——替换成计数替身。
    """
    reauth: list[str] = []

    async def _fake_refresh(provider, acc_client):
        reauth.append(getattr(provider, "username", ""))

    monkeypatch.setattr(admin, "_refresh_models_after_account_init", _fake_refresh)

    async def run():
        admin._apply_account_update_in_place(
            _PoolStub([]), client, runtime_acc,
            rpm=0, rate_limit_extra={}, is_disabled=False,
        )
        await asyncio.sleep(0)

    asyncio.run(run())
    return bool(reauth)


def test_spec_account_field_cleared_syncs_to_runtime(monkeypatch):
    """ACCOUNT_FIELDS 字段从有改成空 → 就地更新必须跟着清，否则重启前一直用旧值。"""
    client = _AccountClientStub("u1")

    reauthed = _apply_in_place(
        monkeypatch, client,
        {"username": "u1", "enterprise_id": "", "region": "us"},
    )

    assert client.provider.enterprise_id == ""
    assert client.provider.region == "us"
    # 凭据类字段变了要重新认证，否则新 region/清空后的 enterprise_id 不会被体检确认。
    assert reauthed is True


def test_credential_field_not_cleared_by_missing_value(monkeypatch):
    """通用凭据字段传空不清值：update_account 的 secret 保留逻辑不会让 DB 里为空，
    跟着清会误删仍然有效的 OAuth token。"""
    client = _AccountClientStub("u1")

    reauthed = _apply_in_place(
        monkeypatch, client,
        {"username": "u1", "access_token": ""},
    )

    assert client.provider.access_token == "tok-old"
    # 什么都没变，不该白白重认证（丢 auth 缓存）。
    assert reauthed is False


def test_spec_account_field_unchanged_does_not_reauth(monkeypatch):
    """ACCOUNT_FIELDS 值没变时不得触发重认证——对账每 60s 跑一次，否则等于持续重认证。"""
    client = _AccountClientStub("u1")

    reauthed = _apply_in_place(
        monkeypatch, client,
        {"username": "u1", "enterprise_id": "ent-old", "region": "cn"},
    )

    assert reauthed is False


# ── 限流策略经热加载/对账后不得被抹零（「并发设了 1 却不拦」的根因回归）──────────


def _apply_in_place_with_policy(monkeypatch, client, runtime_acc: dict,
                                rate_limit_extra: dict, limit_policy: dict | None):
    """带 limit_policy 就地更新（不触发重认证的账号字段集）。"""
    async def _fake_refresh(provider, acc_client):
        return None

    monkeypatch.setattr(admin, "_refresh_models_after_account_init", _fake_refresh)

    async def run():
        admin._apply_account_update_in_place(
            _PoolStub([]), client, runtime_acc,
            rpm=0, rate_limit_extra=rate_limit_extra, is_disabled=False,
            limit_policy=limit_policy,
        )
        await asyncio.sleep(0)

    asyncio.run(run())


def test_policy_concurrency_survives_inplace_reload(monkeypatch):
    """限流策略 account_concurrent=1 → 就地重载（60s 对账走的就是它）后仍是 1。

    修复前兜底链只读「账号级 → 渠道 rate_limit」，不含策略，账号 config 不带并发键时
    直接落 0——保存策略一分钟后并发闸静默失效，请求照常打进同一账号。
    """
    client = _AccountClientStub("u1")
    client.concurrent_limit = 1  # 保存策略那一刻 _apply_policy_to_runtime 写的值

    _apply_in_place_with_policy(
        monkeypatch, client, {"username": "u1"},
        rate_limit_extra={}, limit_policy={"enabled": True, "account_concurrent": 1},
    )

    assert client.concurrent_limit == 1


def test_policy_tpm_rpm_survive_inplace_reload(monkeypatch):
    """同一兜底链覆盖 account_tpm/account_rpm（与建池口径一致，不止并发）。"""
    client = _AccountClientStub("u1")

    _apply_in_place_with_policy(
        monkeypatch, client, {"username": "u1"},
        rate_limit_extra={"rpm_per_account": 7},
        limit_policy={"enabled": True, "account_tpm": 5000, "account_rpm": 3},
    )

    assert client.tpm_limit == 5000
    # 策略缺 account_rpm 才落渠道 rpm_per_account；这里策略有值 → 策略优先。
    assert client.rpm_limit == 3


def test_account_level_concurrency_overrides_policy(monkeypatch):
    """账号级显式并发优先于渠道策略（与建池的账号覆盖顺序一致）。"""
    client = _AccountClientStub("u1")

    _apply_in_place_with_policy(
        monkeypatch, client, {"username": "u1", "concurrent_limit": 4},
        rate_limit_extra={}, limit_policy={"enabled": True, "account_concurrent": 1},
    )

    assert client.concurrent_limit == 4


def test_disabled_policy_falls_to_channel_and_keeps_account_override(monkeypatch):
    """策略停用：策略档与渠道档都归 0，但账号级显式覆盖仍生效（建池同款语义）。"""
    client = _AccountClientStub("u1")

    _apply_in_place_with_policy(
        monkeypatch, client, {"username": "u1", "concurrent_per_account": 2},
        rate_limit_extra={"concurrent_per_account": 9},
        limit_policy={"enabled": False, "account_concurrent": 1},
    )

    assert client.concurrent_limit == 2

    client2 = _AccountClientStub("u2")
    _apply_in_place_with_policy(
        monkeypatch, client2, {"username": "u2"},
        rate_limit_extra={"concurrent_per_account": 9},
        limit_policy={"enabled": False},
    )
    assert client2.concurrent_limit == 0


def test_no_policy_uses_channel_rate_limit(monkeypatch):
    """无权威策略（冷缓存/未配置）→ 落渠道 rate_limit 档，不受策略档影响。"""
    client = _AccountClientStub("u1")

    _apply_in_place_with_policy(
        monkeypatch, client, {"username": "u1"},
        rate_limit_extra={"concurrent_per_account": 5}, limit_policy=None,
    )

    assert client.concurrent_limit == 5


def test_provider_extra_omits_cold_policy(monkeypatch):
    """冷缓存不得挂全零策略模板，否则限额兜底链会把渠道档反向抹成 0。

    这是「设了并发却不生效」的另一半：60s 对账走同步读取器，而它冷缓存兜底一份
    account_* 全零的 dict——下游按权威读就归零。修复后只挂确实读到的策略。
    """
    import limit_policy_store

    limit_policy_store._policy_cache.pop("p-cold", None)
    extra = admin._provider_extra("p-cold", {"rate_limit": {"concurrent_per_account": 3}})
    assert "limit_policy" not in extra

    limit_policy_store._policy_cache["p-warm"] = (0.0, {"account_concurrent": 1, "enabled": True})
    try:
        extra = admin._provider_extra("p-warm", {"rate_limit": {}})
        assert extra.get("limit_policy") == {"account_concurrent": 1, "enabled": True}
    finally:
        limit_policy_store._policy_cache.pop("p-warm", None)


def test_reconcile_keeps_policy_concurrency_end_to_end(reconcile_env, monkeypatch):
    """端到端：策略设并发 1，跑一次 60s 对账，运行对象仍是 1（不是被重算成 0）。"""
    import limit_policy_store

    pool, _removed = reconcile_env(
        snapshot_accounts=[{"username": "u1"}],
        pool_usernames=["u1"],
        db_accounts=[{"username": "u1"}],
    )
    client = pool.clients[0]
    client.concurrent_limit = 1
    limit_policy_store._policy_cache["p1"] = (
        0.0, {"enabled": True, "account_concurrent": 1},
    )
    try:
        asyncio.run(runtime_handlers._reconcile_providers())
    finally:
        limit_policy_store._policy_cache.pop("p1", None)

    assert client.concurrent_limit == 1
