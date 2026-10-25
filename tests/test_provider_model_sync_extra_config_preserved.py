"""定时模型同步不抹掉管理员手填的 extra_config（max_context_tokens 等）。

覆盖面：
- refresh_models 的 to_remove 是「连续两轮缺失才删」：单轮缺失（多账号轮询可见集合
  不同 / 上游抖动）只记观察位不删行，行内 extra_config 原样保留；连续两轮缺失才删。
- 上游列表拉取失败的那轮不碰观察位（失败轮不算「确认缺失」），失败重试成功后缺失
  计数从头算。
- to_add（真下线后回来的模型 / 全新模型）落库时带上游声明的窗口数据
  （max_context_tokens / max_tokens），不至于连上游口径都丢。
- to_update（改名）不把上游窗口静默冲掉手填值：改名沿用原行 extra_config。
"""
import asyncio

import pytest

import rate_limiter
from channel import Channel
from rate_limiter import ModelClientPool


@pytest.fixture(autouse=True)
def _reset_absence_observation():
    """观察位与墓碑都是类级全局态，逐例清空，避免用例间互相污染。"""
    ModelClientPool._model_sync_absent.clear()
    ModelClientPool._model_sync_deleted_extra.clear()
    yield
    ModelClientPool._model_sync_absent.clear()
    ModelClientPool._model_sync_deleted_extra.clear()


def _make_pool(models, provider=None):
    """带模型表的假池。models 是 Channel.models 的运行时行。"""
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
    """挂假 PostgresClient 与池；返回 upserts/deletes 记录列表。"""
    upserts, deletes, mult_updates = [], [], []

    async def list_provider_models(cls, provider=None):
        return list(provider_rows)

    async def upsert_provider_model(cls, provider, upstream_model_id, model_id, extra_config=None, enabled=None,
                                    cost_multiplier=None, cost_multiplier_set=False):
        upserts.append({
            "provider": provider,
            "upstream_model_id": upstream_model_id,
            "model_id": model_id,
            "extra_config": extra_config,
            "cost_multiplier": cost_multiplier if cost_multiplier_set else None,
        })
        provider_rows.append({
            "provider": provider,
            "upstream_model_id": upstream_model_id,
            "model_id": model_id,
            "extra_config": extra_config or {},
            "cost_multiplier": cost_multiplier if cost_multiplier_set else None,
        })

    async def delete_provider_model(cls, provider, upstream_model_id):
        deletes.append((provider, upstream_model_id))
        provider_rows[:] = [r for r in provider_rows if r["upstream_model_id"] != upstream_model_id]
        return True

    async def update_provider_model_cost_multiplier(cls, provider, upstream_model_id, cost_multiplier):
        mult_updates.append((provider, upstream_model_id, cost_multiplier))
        for r in provider_rows:
            if r["upstream_model_id"] == upstream_model_id:
                r["cost_multiplier"] = cost_multiplier
                return True
        return False

    async def rebuild_model_response_cache(cls):
        return None

    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))
    monkeypatch.setattr(rate_limiter.PostgresClient, "upsert_provider_model", classmethod(upsert_provider_model))
    monkeypatch.setattr(rate_limiter.PostgresClient, "delete_provider_model", classmethod(delete_provider_model))
    monkeypatch.setattr(rate_limiter.PostgresClient, "update_provider_model_cost_multiplier", classmethod(update_provider_model_cost_multiplier))
    monkeypatch.setattr(ModelClientPool, "rebuild_model_response_cache", classmethod(rebuild_model_response_cache))
    return upserts, deletes


def test_refresh_models_single_round_absence_keeps_row_and_extra_config(monkeypatch):
    """模型单轮不在上游列表（多账号轮询/上游抖动）：不删行，手填 max_context_tokens 保留。

    下一轮模型回到上游列表 → 行已在库 → 不走 to_add 重建，extra_config 不被空配置覆盖。
    """
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return list(self._models)

    provider = _Provider()
    provider._models = [{"id": "other-model"}]
    pool = _make_pool([], provider=provider)

    # 库内已有模型行，带管理员手填的窗口与展示键。
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2",
        "model_id": "glm-5.2",
        "extra_config": {"max_context_tokens": 400000, "_cost_multiplier": 0.5},
        "enabled": True,
    }]

    # 第一轮：上游列表里没有 glm-5.2（单轮缺失）。
    provider._models = [{"id": "other-model"}]
    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)
    asyncio.run(ModelClientPool.refresh_models())

    # 单轮缺失：不删行。
    assert deletes == []
    # glm-5.2 不在本轮上游列表 → 不进 to_add 重建（other-model 是本轮新增，正常入库）。
    assert "glm-5.2" not in [u["upstream_model_id"] for u in upserts]
    assert [u["upstream_model_id"] for u in upserts] == ["other-model"]

    # 第二轮：模型回到上游列表。
    provider._models = [{"id": "other-model"}, {"id": "glm-5.2"}]
    upserts.clear()
    deletes.clear()
    asyncio.run(ModelClientPool.refresh_models())

    # 行还在库里 → 不新增、不删除；手填配置原样保留。
    assert deletes == []
    assert [u["upstream_model_id"] for u in upserts] == []
    assert provider_rows[0]["extra_config"] == {"max_context_tokens": 400000, "_cost_multiplier": 0.5}


def test_refresh_models_two_consecutive_absences_deletes_row(monkeypatch):
    """连续两轮缺失才确认下线删行；第一轮缺失只记观察位。"""
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        _models = [{"id": "other-model"}]

        async def fetch_upstream_model_list(self):
            return list(self._models)

    provider = _Provider()
    pool = _make_pool([], provider=provider)
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2",
        "model_id": "glm-5.2",
        "extra_config": {"max_context_tokens": 400000},
        "enabled": True,
    }]

    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)

    # 第一轮缺失：只记观察位，不删。
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == []
    assert ModelClientPool._model_sync_absent.get("prov") == {"glm-5.2"}

    # 第二轮仍缺失：确认下线，删行。
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == [("prov", "glm-5.2")]
    assert ModelClientPool._model_sync_absent.get("prov") == {"glm-5.2"}


def test_refresh_models_fetch_failure_resets_absence_counter(monkeypatch):
    """拉取失败的那轮不算「确认缺失」：观察位保持上一轮值，重试成功后缺失计数从头算。"""
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        _fail = False
        _models = [{"id": "other-model"}]

        async def fetch_upstream_model_list(self):
            if self._fail:
                self._last_fetch_models_error = "GET /v1/models → HTTP 500"
                return []
            self._last_fetch_models_error = ""
            return list(self._models)

    provider = _Provider()
    pool = _make_pool([], provider=provider)
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2",
        "model_id": "glm-5.2",
        "extra_config": {"max_context_tokens": 400000},
        "enabled": True,
    }]

    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)

    # 第一轮缺失：记观察位。
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == []
    assert ModelClientPool._model_sync_absent.get("prov") == {"glm-5.2"}

    # 第二轮：拉取失败 → continue，观察位不被覆盖成「缺失」。
    provider._fail = True
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == []
    assert ModelClientPool._model_sync_absent.get("prov") == {"glm-5.2"}

    # 第三轮：重试成功仍缺 → 删。语义是「连续两轮成功拉取都缺才删」：失败轮被跳过、
    # 不推进观察位，所以 r1(成功缺) 与 r3(成功缺) 就是相邻两轮成功缺失 → 确认下线。
    provider._fail = False
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == [("prov", "glm-5.2")]


def test_refresh_models_to_add_seeds_upstream_window(monkeypatch):
    """纯新增模型走 to_add：带上游声明的窗口数据（无用户值可保护，落上游口径）。"""
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        _models = [{"id": "glm-5.2", "context_length": 200000, "max_tokens": 32000}]

        async def fetch_upstream_model_list(self):
            return list(self._models)

    provider = _Provider()
    pool = _make_pool([], provider=provider)
    upserts, deletes = _patch_refresh(monkeypatch, pool, [])

    asyncio.run(ModelClientPool.refresh_models())

    added = [u for u in upserts if u["upstream_model_id"] == "glm-5.2"]
    assert len(added) == 1
    assert added[0]["extra_config"]["max_context_tokens"] == 200000
    assert added[0]["extra_config"]["max_tokens"] == 32000


def test_refresh_models_readd_after_offline_restores_hand_set_config(monkeypatch):
    """真下线（连缺两轮）删行 → 模型回来：to_add 重建时手填 max_context_tokens 从墓碑复活。

    ≥3 账号轮询时「仅部分账号可见」的模型可能连缺两轮被删；墓碑保证手填值不丢，
    且手填值优先于上游声明的窗口（上游 200000 不覆盖手填的 400000）。
    """
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        # 连续两轮上游都只有 other-model，glm-5.2 确认下线后第三轮带着窗口回来。
        _models = [{"id": "other-model"}]

        async def fetch_upstream_model_list(self):
            return list(self._models)

    provider = _Provider()
    pool = _make_pool([], provider=provider)
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2",
        "model_id": "glm-5.2",
        "extra_config": {"max_context_tokens": 400000, "_cost_multiplier": 0.5},
        "enabled": True,
    }]

    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)

    # 两轮缺失 → 确认下线删行（墓碑里存了手填配置）。
    asyncio.run(ModelClientPool.refresh_models())
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == [("prov", "glm-5.2")]
    assert ModelClientPool._model_sync_deleted_extra["prov"]["glm-5.2"][1] == {
        "max_context_tokens": 400000,
    }
    # 墓碑同时存了删行前的 model_id：模型回来重建时手改名要按它复活。
    assert ModelClientPool._model_sync_deleted_extra["prov"]["glm-5.2"][2] == "glm-5.2"

    # 第三轮：模型带着上游窗口声明回来 → to_add 重建，手填值从墓碑复活且优先。
    provider._models = [
        {"id": "other-model"},
        {"id": "glm-5.2", "context_length": 200000, "max_tokens": 32000,
         "credits": "x0.5"},
    ]
    upserts.clear()
    deletes.clear()
    asyncio.run(ModelClientPool.refresh_models())

    added = [u for u in upserts if u["upstream_model_id"] == "glm-5.2"]
    assert len(added) == 1
    # 手填值复活并覆盖上游声明值；展示键（下划线）照常带上游最新。
    assert added[0]["extra_config"]["max_context_tokens"] == 400000
    # max_tokens 手填时没配过 → 上游声明的 32000 补上。
    assert added[0]["extra_config"]["max_tokens"] == 32000
    # 复活后墓碑即清空，不留旧值。
    assert "glm-5.2" not in ModelClientPool._model_sync_deleted_extra.get("prov", {})


def test_refresh_models_prunes_stale_tombstones(monkeypatch):
    """墓碑按 TTL 清理：真·永久下线的模型删行后墓碑到期即清，不无限堆积。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        _models = [{"id": "other-model"}]

        async def fetch_upstream_model_list(self):
            return list(self._models)

    provider = _Provider()
    pool = _make_pool([], provider=provider)
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2",
        "model_id": "glm-5.2",
        "extra_config": {"max_context_tokens": 400000},
        "enabled": True,
    }]

    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)

    # 两轮缺失 → 删行并立墓碑。
    asyncio.run(ModelClientPool.refresh_models())
    asyncio.run(ModelClientPool.refresh_models())
    assert deletes == [("prov", "glm-5.2")]

    # 手工把墓碑时间戳拨到 TTL 之外（不 sleep，直接改时间戳）。
    tombstones = ModelClientPool._model_sync_deleted_extra["prov"]
    ts, cfg, name = tombstones["glm-5.2"]
    tombstones["glm-5.2"] = (ts - cls_ttl_expired_offset(), cfg, name)

    # 再跑一轮（模型仍缺 → 走清理路径）。
    asyncio.run(ModelClientPool.refresh_models())

    assert "prov" not in ModelClientPool._model_sync_deleted_extra


def cls_ttl_expired_offset():
    """墓碑过期偏移量：比 TTL 多一分钟，确保 time.time() - ts > TTL。"""
    return ModelClientPool.MODEL_SYNC_TOMBSTONE_TTL_SECONDS + 60


def test_refresh_models_tracked_row_unaffected_by_rules(monkeypatch):
    """已跟踪行不被规则追溯改名：model_id 与 extra_config 原样保留，同步整行不写。

    渠道配了 [-:]free$ 规则，上游仍返回 glm-5.2:free，库里这行的 model_id 是
    glm-5.2:free、手填窗口 400000（比上游声明大）。规则命中的话旧逻辑会把它改
    名成 glm-5.2 并走 to_update 写库；新语义下规则只对新进模型生效，已入库行
    一律不动——本轮不该有任何 upsert/delete。
    """
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        _models = [{"id": "glm-5.2:free", "context_length": 18000}]

        async def fetch_upstream_model_list(self):
            return list(self._models)

    pool = _make_pool([], provider=_Provider())
    pool.channel.apply({"model_id_rewrite_rules": [{"pattern": r"[-:]free$", "replacement": ""}]})
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2:free",
        "model_id": "glm-5.2:free",
        "extra_config": {"max_context_tokens": 400000},
        "enabled": True,
    }]

    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)
    asyncio.run(ModelClientPool.refresh_models())

    # 已跟踪且仍在上游：保留，不进任何写库动作。
    assert upserts == []
    assert deletes == []


def test_refresh_tombstone_restores_manual_rename_after_offline(monkeypatch):
    """模型真下线（删行）→ 又回来：手改的 model_id 从墓碑复活，不被规则名冲掉。

    复刻现网反馈「一重启模型就改回过滤后的名字」：库里 glm-5.2:free 被手改成
    my-glm（regex 规则 :free$ 本会算出 glm-5.2）。上游这行消失 → 连续两轮确认删行
    → 又回来走 to_add 重建。新语义下墓碑存了 model_id=my-glm，重建时按它复活；
    老逻辑只存 extra_config，重建名按规则算 → 手改名被冲回规则结果。
    """
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""
        _models = [{"id": "glm-5.2:free"}]

        async def fetch_upstream_model_list(self):
            return list(self._models)

    provider = _Provider()
    pool = _make_pool([], provider=provider)
    pool.channel.apply({"model_id_rewrite_rules": [{"pattern": r"[-:]free$", "replacement": ""}]})
    provider_rows = [{
        "provider": "prov",
        "upstream_model_id": "glm-5.2:free",
        "model_id": "my-glm",
        "extra_config": {"max_context_tokens": 400000},
        "enabled": True,
    }]

    upserts, deletes = _patch_refresh(monkeypatch, pool, provider_rows)
    # 上游这行消失 → 连续两轮确认删行。
    provider._models = [{"id": "other-model"}]
    asyncio.run(ModelClientPool.refresh_models())
    asyncio.run(ModelClientPool.refresh_models())
    assert ("prov", "glm-5.2:free") in deletes

    # 上游又出现这行 → to_add 重建。名字按墓碑复活成 my-glm，不是规则算的 glm-5.2。
    provider._models = [{"id": "glm-5.2:free"}]
    upserts.clear()
    deletes.clear()
    asyncio.run(ModelClientPool.refresh_models())
    rebuilt = [u for u in upserts if u["upstream_model_id"] == "glm-5.2:free"]
    assert len(rebuilt) == 1
    assert rebuilt[0]["model_id"] == "my-glm"

