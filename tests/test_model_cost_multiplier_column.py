"""积分倍率：从「extra_config 展示快照键」提升为 provider_models.cost_multiplier 独立列。

覆盖面：
- 上游拉取时单独提取：spec 顶层 _cost_multiplier → 行顶层非下划线键 cost_multiplier
  （不再进 _display_info_from_row 的 extra_config 快照组）。
- 写库边界归一：顶层 cost_multiplier 优先、旧快照键 extra_config._cost_multiplier 回落，
  且旧键从 JSONB 里摘掉（独立列是唯一真相源）。
- 定时同步：新增行带上游倍率；已入库行上游本轮声明了倍率就覆盖（倍率会改动）。
- 取值合法性：bool/空串/非数字 → NULL，字符串数字认。
"""
import asyncio

import pytest

import rate_limiter
from channel import Channel
from db import PostgresClient
from rate_limiter import ModelClientPool


@pytest.fixture(autouse=True)
def _reset_absence_observation():
    """观察位与墓碑是类级全局态，逐例清空，避免用例间互相污染。"""
    ModelClientPool._model_sync_absent.clear()
    ModelClientPool._model_sync_deleted_extra.clear()
    yield
    ModelClientPool._model_sync_absent.clear()
    ModelClientPool._model_sync_deleted_extra.clear()


# ── 上游拉取：单独提取 ────────────────────────────────────────────────
def _build_rows(channel_cfg, models, tracked_rows, monkeypatch):
    pool = type("_Pool", (), {"channel": Channel("prov", channel_cfg)})()
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    return ModelClientPool._build_upstream_model_rows("prov", models, tracked_rows)


def test_upstream_row_exposes_cost_multiplier_top_level(monkeypatch):
    """spec 顶层 _cost_multiplier 提为行顶层 cost_multiplier（独立列的提取口径）。"""
    rows = _build_rows({}, [{"id": "glm-5.2", "_cost_multiplier": 0.7}], {}, monkeypatch)
    assert rows[0]["cost_multiplier"] == 0.7


def test_cost_multiplier_not_in_display_snapshot(monkeypatch):
    """_cost_multiplier 不再进 extra_config 展示快照组（避免重拉上游覆盖手填值）。"""
    rows = _build_rows(
        {},
        [{"id": "glm-5.2", "_cost_multiplier": 0.7, "_model_desc": "说明"}],
        {},
        monkeypatch,
    )
    # 展示键照旧：说明进快照；倍率走独立字段，不在快照里。
    assert "_model_desc" in ModelClientPool._display_info_from_row(rows[0])
    assert "_cost_multiplier" not in rows[0]
    assert "_cost_multiplier" not in ModelClientPool._display_info_from_row(rows[0])


def test_upstream_declares_nothing_no_key(monkeypatch):
    """上游没声明倍率 → 行不带 cost_multiplier 键（前端据此保留库内手填值，不臆造 1.0）。"""
    rows = _build_rows({}, [{"id": "auto"}], {}, monkeypatch)
    assert "cost_multiplier" not in rows[0]


@pytest.mark.parametrize("model,expected", [
    ({"_cost_multiplier": 0.29}, 0.29),                 # 数值
    ({"_cost_multiplier": "0.7"}, 0.7),                 # 字符串数字（老服务端透传）
    ({"cost_multiplier": 2.0}, 2.0),                    # 已是行顶层键
    ({"raw": {"costMultiplier": 4.0}}, 4.0),            # raw 驼峰兜底
    ({"_cost_multiplier": None}, None),                 # 空值
    ({"_cost_multiplier": True}, None),                 # bool 不算倍率
    ({"_cost_multiplier": "限时免费"}, None),            # 非数字
    ({}, None),                                          # 完全没声明
])
def test_cost_multiplier_from_row_sources(model, expected):
    assert ModelClientPool._cost_multiplier_from_row(model) == expected


# ── 写库边界：旧快照键搬家 ────────────────────────────────────────────
def test_payload_lifts_legacy_snapshot_key_into_column():
    """旧客户端把倍率塞在 extra_config._cost_multiplier：写库时搬到独立列并从 JSONB 摘掉。"""
    payload = PostgresClient._provider_model_payload({
        "upstream_model_id": "glm-5.2",
        "model_id": "glm-5.2",
        "extra_config": {"_cost_multiplier": 0.7, "max_tokens": 4096},
    })
    assert payload["cost_multiplier"] == 0.7
    assert "_cost_multiplier" not in payload["extra_config"]
    # 其它 extra_config 原样保留（只摘倍率旧键）。
    assert payload["extra_config"]["max_tokens"] == 4096


def test_payload_prefers_explicit_cost_multiplier():
    """顶层 cost_multiplier 与旧快照键并存时以顶层为准（新口径优先）。"""
    payload = PostgresClient._provider_model_payload({
        "upstream_model_id": "m",
        "cost_multiplier": 1.5,
        "extra_config": {"_cost_multiplier": 0.7},
    })
    assert payload["cost_multiplier"] == 1.5


def test_payload_does_not_mutate_caller_extra_config():
    """摘旧键用拷贝，不污染调用方传入的请求体 dict。"""
    extra = {"_cost_multiplier": 0.7}
    PostgresClient._provider_model_payload({"upstream_model_id": "m", "extra_config": extra})
    assert extra == {"_cost_multiplier": 0.7}


@pytest.mark.parametrize("value,expected", [
    (0, 0.0), (0.29, 0.29), ("0.7", 0.7), ("", None), ("abc", None),
    (None, None), (True, None), (False, None),
])
def test_coerce_cost_multiplier(value, expected):
    assert PostgresClient._coerce_cost_multiplier(value) == expected


# ── 定时同步：新增落列 / 已入库行上游覆盖 ─────────────────────────────
def _make_pool(models, provider=None):
    class _Client:
        disabled = False

        def __init__(self):
            self.provider = provider

    class _Pool:
        clients = []
        channel = Channel("prov", {"provider_name": "prov"})

        async def get_initialized_provider(self):
            return self.clients[0].provider

    pool = _Pool()
    pool.clients = [_Client()]
    pool.channel.register_models(models)
    return pool


def _patch_refresh(monkeypatch, pool, provider_rows):
    upserts, mult_updates = [], []

    async def list_provider_models(cls, provider=None):
        return list(provider_rows)

    async def upsert_provider_model(cls, provider, upstream_model_id, model_id, extra_config=None, enabled=None,
                                    cost_multiplier=None, cost_multiplier_set=False):
        cm = cost_multiplier if cost_multiplier_set else None
        upserts.append({"upstream_model_id": upstream_model_id, "cost_multiplier": cm})
        provider_rows.append({
            "provider": provider, "upstream_model_id": upstream_model_id, "model_id": model_id,
            "extra_config": extra_config or {}, "cost_multiplier": cm,
        })

    async def delete_provider_model(cls, provider, upstream_model_id):
        return True

    async def update_provider_model_cost_multiplier(cls, provider, upstream_model_id, cost_multiplier):
        mult_updates.append((upstream_model_id, cost_multiplier))
        for r in provider_rows:
            if r["upstream_model_id"] == upstream_model_id:
                r["cost_multiplier"] = cost_multiplier
        return True

    async def rebuild_model_response_cache(cls):
        return None

    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))
    monkeypatch.setattr(rate_limiter.PostgresClient, "upsert_provider_model", classmethod(upsert_provider_model))
    monkeypatch.setattr(rate_limiter.PostgresClient, "delete_provider_model", classmethod(delete_provider_model))
    monkeypatch.setattr(
        rate_limiter.PostgresClient, "update_provider_model_cost_multiplier",
        classmethod(update_provider_model_cost_multiplier),
    )
    monkeypatch.setattr(ModelClientPool, "rebuild_model_response_cache", classmethod(rebuild_model_response_cache))
    return upserts, mult_updates


class _Provider:
    auto_update_models = True
    _last_fetch_models_error = ""
    _models: list = []

    async def fetch_upstream_model_list(self):
        return list(self._models)


def test_sync_new_row_writes_cost_multiplier(monkeypatch):
    """定时同步新增的行：上游声明了倍率就落独立列（不再靠 extra_config 展示组）。"""
    provider = _Provider()
    provider._models = [{"id": "glm-5.2", "_cost_multiplier": 0.7}]
    pool = _make_pool([], provider=provider)
    upserts, _ = _patch_refresh(monkeypatch, pool, [])

    asyncio.run(ModelClientPool.refresh_models())

    row = next(u for u in upserts if u["upstream_model_id"] == "glm-5.2")
    assert row["cost_multiplier"] == 0.7


def test_sync_existing_row_refreshes_cost_multiplier(monkeypatch):
    """已入库行：上游本轮声明了倍率就覆盖库内值（倍率会改动，用户要看到最新值）。"""
    provider = _Provider()
    provider._models = [{"id": "glm-5.2", "_cost_multiplier": 0.5}]
    pool = _make_pool([], provider=provider)
    provider_rows = [{
        "provider": "prov", "upstream_model_id": "glm-5.2", "model_id": "glm-5.2",
        "extra_config": {}, "cost_multiplier": 0.7,  # 库内旧值
    }]
    _, mult_updates = _patch_refresh(monkeypatch, pool, provider_rows)

    asyncio.run(ModelClientPool.refresh_models())

    assert mult_updates == [("glm-5.2", 0.5)]
    assert provider_rows[0]["cost_multiplier"] == 0.5


def test_sync_upstream_silent_keeps_existing_cost_multiplier(monkeypatch):
    """上游本轮没声明倍率 → 不动已入库行的库内值（手填值保留）。"""
    provider = _Provider()
    provider._models = [{"id": "glm-5.2"}]
    pool = _make_pool([], provider=provider)
    provider_rows = [{
        "provider": "prov", "upstream_model_id": "glm-5.2", "model_id": "glm-5.2",
        "extra_config": {}, "cost_multiplier": 0.7,
    }]
    upserts, mult_updates = _patch_refresh(monkeypatch, pool, provider_rows)

    asyncio.run(ModelClientPool.refresh_models())

    assert mult_updates == []
    assert upserts == []
    assert provider_rows[0]["cost_multiplier"] == 0.7