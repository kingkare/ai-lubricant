"""供应商编辑弹框「模型列表」Tab 的「上一次更新」时间戳。

后端两列语义：
- ``models_synced_at`` —— 最近一次从上游**成功拉取**模型列表（定时/手动同步都算）；
- ``models_updated_at`` —— 最近一次模型行集合**确实被修改**（增/删/改名/开关/行内配置）。

前端按 auto_update_models 取其一展示，所以两个方向都必须独立可写：
- 拉取失败（报错标记）不能刷 synced_at——时间戳只代表成功的拉取；
- 弹框「保存」每次都会整覆盖一次模型表，内容没变时不刷 updated_at，
  否则「最近一次修改模型」会变成「最近一次保存渠道」。
"""
import asyncio

import admin
import rate_limiter
from rate_limiter import ModelClientPool


def _cfg(auto_update: bool = True, **ts) -> dict:
    cfg = {
        "enabled": True,
        "type": "builtin",
        "builtin_type": "",
        "remark": "x",
        "accounts": [],
        "rate_limit": {},
        "auto_update_models": auto_update,
    }
    cfg.update(ts)
    return cfg


def test_base_response_exposes_model_timestamps():
    """读路径必须回传两个时间戳，前端才有值可展示（缺键 = 永远显示「—」）。"""
    resp = admin._provider_base_response("prov", _cfg(models_synced_at=111.0, models_updated_at=222.0))
    assert resp["models_synced_at"] == 111.0
    assert resp["models_updated_at"] == 222.0


def test_base_response_timestamps_default_none():
    """老渠道没写过这两列：回 None，前端显示「—」而不是 0 / 报错。"""
    resp = admin._provider_base_response("prov", _cfg())
    assert resp["models_synced_at"] is None
    assert resp["models_updated_at"] is None


def _make_pool(provider):
    class _Client:
        disabled = False

        def __init__(self):
            self.provider = provider

    class _Pool:
        def __init__(self):
            self.clients = [_Client()]
            self.channel = rate_limiter.Channel("prov", {"provider_name": "prov"})

        async def get_initialized_provider(self):
            return self.clients[0].provider

    return _Pool()


def test_refresh_models_records_synced_at(monkeypatch):
    """可信拉取后落 synced_at；同步到实际变更时再落 updated_at。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [{"id": "glm-5"}]

    pool = _make_pool(_Provider())
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))

    touched: dict[str, list[str]] = {"synced": [], "updated": []}

    async def list_provider_models(cls, provider=None, lite=False):
        return []

    async def touch_synced(cls, provider):
        touched["synced"].append(provider)

    async def touch_updated(cls, provider):
        touched["updated"].append(provider)

    async def rebuild(cls):
        return None

    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))
    monkeypatch.setattr(rate_limiter.PostgresClient, "touch_provider_models_synced_at", classmethod(touch_synced))
    monkeypatch.setattr(rate_limiter.PostgresClient, "touch_provider_models_updated_at", classmethod(touch_updated))
    monkeypatch.setattr(ModelClientPool, "rebuild_model_response_cache", classmethod(rebuild))

    asyncio.run(ModelClientPool.refresh_models())

    assert touched["synced"] == ["prov"]
    # 库内原本无行、上游拉到 1 个 → 有新增，updated_at 跟着刷。
    assert touched["updated"] == ["prov"]


def test_refresh_models_failed_pull_records_nothing(monkeypatch):
    """拉取失败（报错标记）整轮跳过：两个时间戳都不该动。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = "boom"

        async def fetch_upstream_model_list(self):
            raise AssertionError("失败标记已置位，不应再发请求")

    pool = _make_pool(_Provider())
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))

    touched: list[str] = []

    async def touch_synced(cls, provider):
        touched.append(provider)

    monkeypatch.setattr(rate_limiter.PostgresClient, "touch_provider_models_synced_at", classmethod(touch_synced))

    asyncio.run(ModelClientPool.refresh_models())
    assert touched == []


def test_bulk_replace_only_bumps_when_rows_change(monkeypatch):
    """弹框每次保存都会整覆盖：内容没变不刷 updated_at，变了才刷。"""
    import db as pg

    state = {"rows": []}
    events: list[str] = []

    class _Conn:
        async def fetch(self, q, p):
            return [dict(r) for r in state["rows"]]

        async def execute(self, q, *args):
            if "models_updated_at" in q:
                events.append("updated_at")

        def transaction(self):
            return _Txn()

    class _Txn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def acquire(self):
            return _Acquire()

    monkeypatch.setattr(pg.PostgresClient, "pool", _FakePool(), raising=False)

    def run(rows_in):
        events.clear()
        asyncio.run(pg.PostgresClient.bulk_replace_provider_models("prov", rows_in))
        return "updated_at" in events

    state["rows"] = [{"upstream_model_id": "m1", "model_id": "m1", "extra_config": "{}", "enabled": True}]

    # 内容与库内一致 → 不刷（每次保存都会走到这里，内容没变就是没改）
    assert run([{"upstream_model_id": "m1", "model_id": "m1", "extra_config": {}, "enabled": True}]) is False
    # 改名 → 刷
    assert run([{"upstream_model_id": "m1", "model_id": "renamed", "extra_config": {}, "enabled": True}]) is True
    # 行级开关翻转 → 刷
    assert run([{"upstream_model_id": "m1", "model_id": "m1", "extra_config": {}, "enabled": False}]) is True
    # 行数变化（删行）→ 刷
    assert run([]) is True
