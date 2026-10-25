"""获取模型列表：空列表视为成功；仅新增模型 ID 去首个 "/" 前缀，已有模型不覆盖。"""
import asyncio

import pytest
from fastapi import HTTPException

import rate_limiter
from rate_limiter import ModelClientPool


@pytest.fixture(autouse=True)
def _reset_absence_observation():
    """refresh_models 的「连续两轮缺失才删」观察位是类级全局态，逐例清空避免用例互染。"""
    ModelClientPool._model_sync_absent.clear()
    yield
    ModelClientPool._model_sync_absent.clear()


# ----------------------------- 需求 1：空列表视为成功 -----------------------------


def test_fetch_unsaved_custom_provider_models_empty_success_returns_empty(monkeypatch):
    """上游 200 返回空列表（无 _last_fetch_models_error）→ 返回空列表，不报 502。"""
    import admin

    class _Provider:
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return []

    monkeypatch.setattr(admin, "_runtime_accounts", lambda accounts: [{"username": "u", "password": "k"}])
    monkeypatch.setattr(admin, "_read_proxies", lambda: [])
    monkeypatch.setattr(admin, "_provider_extra", lambda name, cfg: {})
    monkeypatch.setattr(admin, "CustomProvider", lambda **kwargs: _Provider())

    payload = {
        "base_url": "https://example.com",
        "accounts": [{"password": "k"}],
        "chat_protocols": [{"protocol": "openai", "path": "/v1/chat/completions"}],
    }

    result = asyncio.run(admin._fetch_unsaved_custom_provider_models(payload))

    assert result == []


def test_fetch_unsaved_custom_provider_models_error_raises_502(monkeypatch):
    """上游请求失败（_last_fetch_models_error 有值）→ 仍抛 502，保持异常路径不变。"""
    import admin

    class _Provider:
        _last_fetch_models_error = "GET https://x/v1/models → HTTP 500"

        async def fetch_upstream_model_list(self):
            return []

    monkeypatch.setattr(admin, "_runtime_accounts", lambda accounts: [{"username": "u", "password": "k"}])
    monkeypatch.setattr(admin, "_read_proxies", lambda: [])
    monkeypatch.setattr(admin, "_provider_extra", lambda name, cfg: {})
    monkeypatch.setattr(admin, "CustomProvider", lambda **kwargs: _Provider())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin._fetch_unsaved_custom_provider_models({
            "base_url": "https://example.com",
            "accounts": [{"password": "k"}],
            "chat_protocols": [{"protocol": "openai", "path": "/v1/chat/completions"}],
        }))
    assert exc.value.status_code == 502
    assert "上游模型列表获取失败" in exc.value.detail


# ----------------------------- 需求 2：仅新增模型去 "/" 前缀 -----------------------------


def test_strip_owner_prefix_only_for_new_models():
    """owner/model → 仅保留首个 / 之后；无 / 或尾部为空则原样返回。

    切前缀的实现已从 ModelClientPool._strip_owner_prefix 移到 channel 模块，
    好让 model_id 改写规则复用同一份逻辑；此处断言的行为契约不变。
    """
    from channel import strip_model_owner_prefix as strip
    assert strip("1111/glm-5.2") == "glm-5.2"
    assert strip("openai/gpt-4o") == "gpt-4o"
    assert strip("glm-5.2") == "glm-5.2"
    assert strip("org/team/model") == "team/model"
    assert strip("org/") == "org/"
    assert strip("") == ""


def _make_pool_with_provider(provider):
    from channel import Channel

    class _Pool:
        clients = [type("Client", (), {"provider": provider})()]
        channel = Channel("prov", {"provider_name": "prov"})

        async def get_initialized_provider(self):
            return self.clients[0].provider

    return _Pool()


def test_fetch_provider_upstream_models_new_prefers_short_id_tracked_keeps_existing(monkeypatch):
    """手动获取：新模型 model_id/current_model_id 去前缀；已跟踪模型沿用表内 model_id，不覆盖。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [
                {"id": "1111/glm-5.2"},   # 新模型 → 去前缀
                {"id": "acme/kept"},      # 已跟踪 → 不覆盖
            ]

    pool = _make_pool_with_provider(_Provider())

    async def list_provider_models(cls, provider=None):
        return [{"provider": "prov", "upstream_model_id": "acme/kept", "model_id": "custom-kept"}]

    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    by_upstream = {m["upstream_model_id"]: m for m in result["upstream_models"]}

    new = by_upstream["1111/glm-5.2"]
    assert new["model_id"] == "glm-5.2"
    assert new["regex_model_id"] == "glm-5.2"  # 没配规则 → 退化回 model_id

    kept = by_upstream["acme/kept"]
    # 已跟踪：两个名字都用表内值，不被裁剪覆盖。
    assert kept["model_id"] == "custom-kept"
    assert kept["regex_model_id"] == "custom-kept"


def test_fetch_provider_upstream_models_returns_both_names(monkeypatch):
    """预览同时返回 model_id（未套规则）与 regex_model_id（套规则后），
    前端按「有没有勾选过滤」二选一显示，无需重新拉取。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [{"id": "1111/glm-5.2:free"}]  # 含 owner/ 与后缀

    pool = _make_pool_with_provider(_Provider())
    pool.channel.apply({"model_id_rewrite_rules": [{"pattern": r":free$", "replacement": ""}]})

    async def list_provider_models(cls, provider=None):
        return []

    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    row = {m["upstream_model_id"]: m for m in result["upstream_models"]}["1111/glm-5.2:free"]
    # 未入库新行 + 配了规则：model_id 只切前缀，regex_model_id 是套规则后的名字
    assert row["model_id"] == "glm-5.2:free"
    assert row["regex_model_id"] == "glm-5.2"


def test_fetch_provider_upstream_models_names_differ_only_when_rules_hit(monkeypatch):
    """两列名字的区分只在「配了规则且规则真改了名」时出现。

    前端「模拟过滤及改写规则」开关按渠道有没有配规则来显示两个名字：model_id 未套
    规则，regex_model_id 套了规则。判定基线必须是切完前缀之后的值——否则带 owner/
    的模型会被误判成命中和没命中一样，两列永远同值、开关形同虚设。
    """

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [
                {"id": "1111/glm-5.2:free"},  # 规则命中
                {"id": "1111/glm-5.2"},       # 只切前缀，规则没动它
                {"id": "plain"},              # 既无前缀也不命中
            ]

    pool = _make_pool_with_provider(_Provider())
    pool.channel.apply({"model_id_rewrite_rules": [{"pattern": r"[-:]free$", "replacement": ""}]})

    async def list_provider_models(cls, provider=None):
        return []

    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    by_upstream = {m["upstream_model_id"]: m for m in result["upstream_models"]}

    # 命中规则：两列不同
    assert by_upstream["1111/glm-5.2:free"]["model_id"] == "glm-5.2:free"
    assert by_upstream["1111/glm-5.2:free"]["regex_model_id"] == "glm-5.2"
    # 规则没动它：两列同值
    assert by_upstream["1111/glm-5.2"]["model_id"] == "glm-5.2"
    assert by_upstream["1111/glm-5.2"]["regex_model_id"] == "glm-5.2"
    # 既无前缀也不命中：两列同值
    assert by_upstream["plain"]["model_id"] == "plain"
    assert by_upstream["plain"]["regex_model_id"] == "plain"


def test_fetch_provider_upstream_models_no_rules_identical_names(monkeypatch):
    """没配规则的渠道全员两列同值：regex_model_id 退化回 model_id（切前缀）。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [{"id": "1111/glm-5.2:free"}]

    pool = _make_pool_with_provider(_Provider())

    async def list_provider_models(cls, provider=None):
        return []

    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    row = result["upstream_models"][0]
    assert row["model_id"] == "glm-5.2:free"
    assert row["regex_model_id"] == "glm-5.2:free"


def test_fetch_provider_upstream_models_preserves_root_and_explicit_raw_metadata(monkeypatch):
    """Custom 根级字段可导入；显式 raw 存在时仍优先返回上游原始对象。"""

    explicit_raw = {"id": "raw/model", "context_length": 131072}

    class _Provider:
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [
                {"id": "root/model", "max_context_tokens": 32768, "is_thinking": True},
                {"id": "wrapped/model", "max_context_tokens": 4096, "raw": explicit_raw},
            ]

    pool = _make_pool_with_provider(_Provider())

    async def list_provider_models(cls, provider=None):
        return []

    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    by_upstream = {m["upstream_model_id"]: m for m in result["upstream_models"]}

    assert by_upstream["root/model"]["raw"]["max_context_tokens"] == 32768
    assert by_upstream["root/model"]["raw"]["is_thinking"] is True
    assert by_upstream["wrapped/model"]["raw"] is explicit_raw


def test_refresh_models_strips_prefix_only_on_add_not_update(monkeypatch):
    """自动同步：新增模型用裁剪后的 model_id；已有模型映射不被前缀裁剪改动。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [{"id": "1111/glm-5.2"}]

    pool = _make_pool_with_provider(_Provider())

    provider_rows = []
    upserts = []
    deletes = []

    async def list_provider_models(cls, provider=None):
        return list(provider_rows)

    async def upsert_provider_model(cls, provider, upstream_model_id, model_id, extra_config=None, **kwargs):
        upserts.append((provider, upstream_model_id, model_id))
        provider_rows.append({"provider": provider, "upstream_model_id": upstream_model_id, "model_id": model_id})

    async def delete_provider_model(cls, provider, upstream_model_id):
        deletes.append((provider, upstream_model_id))
        return True

    async def rebuild_model_response_cache(cls):
        return None

    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))
    monkeypatch.setattr(rate_limiter.PostgresClient, "upsert_provider_model", classmethod(upsert_provider_model))
    monkeypatch.setattr(rate_limiter.PostgresClient, "delete_provider_model", classmethod(delete_provider_model))
    monkeypatch.setattr(ModelClientPool, "rebuild_model_response_cache", classmethod(rebuild_model_response_cache))

    asyncio.run(ModelClientPool.refresh_models())

    # 新增时裁剪为 glm-5.2
    assert upserts == [("prov", "1111/glm-5.2", "glm-5.2")]
    assert deletes == []

    # 再次同步：该行已存在且映射未漂移 → 不再 update、不重复写、不删。
    upserts.clear()
    asyncio.run(ModelClientPool.refresh_models())
    assert upserts == []
    assert deletes == []


def _refresh_pool_with_rules(rules):
    """带改写规则的渠道池，供 refresh_models 过滤测试复用。"""
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [{"id": "glm-5.2"}, {"id": "glm-5.2:free"}]

    pool = _make_pool_with_provider(_Provider())
    pool.channel.apply({"model_id_rewrite_rules": rules})
    return pool


def _patch_refresh(monkeypatch, pool, provider_rows):
    upserts, deletes = [], []

    async def list_provider_models(cls, provider=None):
        return list(provider_rows)

    async def upsert_provider_model(cls, provider, upstream_model_id, model_id, extra_config=None, **kwargs):
        upserts.append((provider, upstream_model_id, model_id))
        provider_rows.append({"provider": provider, "upstream_model_id": upstream_model_id, "model_id": model_id})

    async def delete_provider_model(cls, provider, upstream_model_id):
        deletes.append((provider, upstream_model_id))
        return True

    async def rebuild_model_response_cache(cls):
        return None

    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))
    monkeypatch.setattr(rate_limiter.PostgresClient, "upsert_provider_model", classmethod(upsert_provider_model))
    monkeypatch.setattr(rate_limiter.PostgresClient, "delete_provider_model", classmethod(delete_provider_model))
    monkeypatch.setattr(ModelClientPool, "rebuild_model_response_cache", classmethod(rebuild_model_response_cache))
    return upserts, deletes


def test_refresh_keeps_only_regex_hit_models(monkeypatch):
    """规则就是过滤条件：满足（is_regex=true）的留下并改名，不满足的丢掉。

    上游 glm-5.2 / glm-5.2:free，规则 [-:]free$ 只搜到 glm-5.2:free →
    它留下、改名成 glm-5.2 进库；glm-5.2 没被搜到，不进库。
    """
    pool = _refresh_pool_with_rules([{"pattern": r"[-:]free$", "replacement": ""}])
    upserts, deletes = _patch_refresh(monkeypatch, pool, [])

    asyncio.run(ModelClientPool.refresh_models())

    by_uid = {uid: mid for _, uid, mid in upserts}
    # 满足规则的 free 模型保留，改名为 glm-5.2
    assert "glm-5.2:free" in by_uid and by_uid["glm-5.2:free"] == "glm-5.2"
    # 不满足规则的 glm-5.2 不进库
    assert "glm-5.2" not in by_uid
    assert deletes == []


def test_refresh_keeps_existing_non_matching_models(monkeypatch):
    """已入库但不满足规则的模型，同步**保留**——规则只是新模型的准入条件，
    不是已入库模型的生死线。

    规则改版后不再命中的行，若按 is_regex 一刀切会「删行 → 回来按规则重建」，
    把管理员手改的名字冲回规则算的结果（现网反馈「一重启模型就改回过滤后的名字」
    的根因）。已入库行留在 targets 里（名字取库内现名），上游真下线仍按两轮确认删。
    """
    pool = _refresh_pool_with_rules([{"pattern": r"[-:]free$", "replacement": ""}])
    # 库里已有 glm-5.2（不满足规则），上游也有 glm-5.2:free（满足）
    rows = [{"provider": "prov", "upstream_model_id": "glm-5.2", "model_id": "glm-5.2"}]
    upserts, deletes = _patch_refresh(monkeypatch, pool, rows)

    asyncio.run(ModelClientPool.refresh_models())
    # 两轮都不删：行还活在上游列表里，只是不再命中规则 → 保留原样。
    asyncio.run(ModelClientPool.refresh_models())
    assert ("prov", "glm-5.2") not in deletes
    # 不命中也不再被改名/重写：无任何 upsert 触及它。
    assert "glm-5.2" not in {uid for _, uid, _ in upserts}
    # 上游新出现的命中模型照常进库。
    assert "glm-5.2:free" in {uid for _, uid, _ in upserts}


def test_refresh_no_rules_keeps_all_unchanged(monkeypatch):
    """没配规则的渠道：所有上游模型照常导入，不过滤。"""
    pool = _refresh_pool_with_rules([])  # 空规则 → has_rules=False
    upserts, deletes = _patch_refresh(monkeypatch, pool, [])

    asyncio.run(ModelClientPool.refresh_models())

    upserted_ids = [uid for _, uid, _ in upserts]
    assert "glm-5.2" in upserted_ids
    assert "glm-5.2:free" in upserted_ids
    assert deletes == []


def test_fetch_upstream_models_search_only_rule_hits_full_name(monkeypatch):
    """只搜索规则：pattern 写全量名（含 owner/ 前缀）也能命中。

    普通规则跑在切完前缀的名字上，带前缀的 pattern 永远搜不到——这条测试锁定
    只搜索规则的全量名判定。只搜索规则不参与改名：regex_model_id 仍是切前缀的
    默认形态（与 model_id 同值），命中与否只体现在定时同步的过滤上。
    """

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [
                {"id": "deepseek-ai/deepseek-v4"},      # 命中只搜索规则
                {"id": "deepseek-ai/deepseek-v4-chat"},  # 前缀不同，不命中
                {"id": "openai/gpt-4o"},                 # 不命中
            ]

    pool = _make_pool_with_provider(_Provider())
    pool.channel.apply({"model_id_rewrite_rules": [
        {"pattern": r"^deepseek-ai/deepseek-v4$", "replacement": "", "search_only": True},
    ]})

    async def list_provider_models(cls, provider=None):
        return []

    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    by_upstream = {m["upstream_model_id"]: m for m in result["upstream_models"]}

    hit = by_upstream["deepseek-ai/deepseek-v4"]
    # 只搜索规则不改名：两列都是切完前缀的默认形态。
    assert hit["model_id"] == "deepseek-v4"
    assert hit["regex_model_id"] == "deepseek-v4"
    assert by_upstream["deepseek-ai/deepseek-v4-chat"]["model_id"] == "deepseek-v4-chat"
    assert by_upstream["openai/gpt-4o"]["model_id"] == "gpt-4o"


def test_refresh_keeps_only_search_rule_matched_models(monkeypatch):
    """自动同步：只搜索规则命中的模型保留（名字走切前缀默认），不命中的丢掉。"""

    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return [{"id": "1111/glm-5.2"}, {"id": "other/drop-me"}]

    pool = _make_pool_with_provider(_Provider())
    pool.channel.apply({"model_id_rewrite_rules": [
        {"pattern": r"^1111/", "replacement": "", "search_only": True},
    ]})
    upserts, deletes = _patch_refresh(monkeypatch, pool, [])

    asyncio.run(ModelClientPool.refresh_models())

    by_uid = {uid: mid for _, uid, mid in upserts}
    assert "1111/glm-5.2" in by_uid and by_uid["1111/glm-5.2"] == "glm-5.2"
    assert "other/drop-me" not in by_uid
    assert deletes == []


# ------------------- 需求 3：拉上游模型列表不选禁用账号 -------------------


def _stub_client(username: str, *, disabled: bool, is_init: bool = True):
    """最小 AccountClient 替身：只提供 get_initialized_provider 用到的三个面。"""
    provider = type("P", (), {"PROVIDER_NAME": "prov", "is_init": lambda self: is_init})()
    return type("C", (), {"username": username, "disabled": disabled, "provider": provider})()


# ------------------- 需求 4：本次拉取可不可信，只看失败标记 -------------------
# 契约：请求与取值同在一个 try 里，任何一步抛异常都写 _last_fetch_models_error；
# 没写标记就是正常返回，返回什么就按什么处理（空列表也照常对账、含全量下线）。
# 判据绝不能退回「结果空不空」——那会把错误信封当空列表清空模型表。


def _patch_refresh_simple(monkeypatch, provider, provider_rows):
    """打桩 refresh_models 的 DB/缓存依赖，返回 (upserts, deletes)。"""
    pool = _make_pool_with_provider(provider)
    upserts, deletes = [], []

    async def list_provider_models(cls, provider=None):
        return list(provider_rows)

    async def upsert_provider_model(cls, provider, upstream_model_id, model_id, extra_config=None, **kwargs):
        upserts.append((provider, upstream_model_id, model_id))
        provider_rows.append({"provider": provider, "upstream_model_id": upstream_model_id, "model_id": model_id})

    async def delete_provider_model(cls, provider, upstream_model_id):
        deletes.append((provider, upstream_model_id))
        return True

    async def rebuild_model_response_cache(cls):
        return None

    monkeypatch.setattr(ModelClientPool, "get_provider_names", classmethod(lambda cls: ["prov"]))
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    monkeypatch.setattr(ModelClientPool, "_provider_pools", {"prov": pool})
    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))
    monkeypatch.setattr(rate_limiter.PostgresClient, "upsert_provider_model", classmethod(upsert_provider_model))
    monkeypatch.setattr(rate_limiter.PostgresClient, "delete_provider_model", classmethod(delete_provider_model))
    monkeypatch.setattr(ModelClientPool, "rebuild_model_response_cache", classmethod(rebuild_model_response_cache))
    return upserts, deletes


def test_refresh_empty_list_is_normal_and_removes_offline_models(monkeypatch):
    """正常返回空列表 = 上游确实没有模型：照常对账，库里旧行全量下线。

    这是与「拉取失败」的对照面——两者都表现为「拿到 0 个模型」，
    但空列表必须清库，失败必须不动库。

    删除是「连续两轮确认」才执行（防单轮抖动抹掉行内 extra_config），
    故跑两轮：第一轮只记观察位，第二轮才下线。
    """
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = ""

        async def fetch_upstream_model_list(self):
            return []

    rows = [{"provider": "prov", "upstream_model_id": "gone", "model_id": "gone"}]
    upserts, deletes = _patch_refresh_simple(monkeypatch, _Provider(), rows)

    asyncio.run(ModelClientPool.refresh_models())
    # 第一轮：记观察位，不删（保留行内配置）。
    assert ("prov", "gone") not in deletes

    asyncio.run(ModelClientPool.refresh_models())
    # 第二轮仍为空 → 确认全量下线。
    assert upserts == []
    assert ("prov", "gone") in deletes


def test_refresh_failure_flag_skips_round_without_touching_db(monkeypatch):
    """写了失败标记（接口报错/超时/异常/格式不符）→ 整轮不增、不改、不删。"""
    class _Provider:
        auto_update_models = True
        # 失败标记由 fetch_upstream_model_list 内部写入；这里模拟它返回空 + 写标记
        async def fetch_upstream_model_list(self):
            self._last_fetch_models_error = "GET https://x/v1/models → HTTP 502"
            return []

    rows = [{"provider": "prov", "upstream_model_id": "kept", "model_id": "kept"}]
    upserts, deletes = _patch_refresh_simple(monkeypatch, _Provider(), rows)

    asyncio.run(ModelClientPool.refresh_models())

    assert upserts == []
    assert deletes == []
    assert rows == [{"provider": "prov", "upstream_model_id": "kept", "model_id": "kept"}]


def test_refresh_stale_error_flag_is_cleared_before_call(monkeypatch):
    """上一轮残留的失败标记不污染本轮：调用前清标记，成功即正常对账。

    代码渠道的 fetch_models 由作者实现，不一定成功时清空标记；若不清，
    一次失败会让该渠道后续每一轮都被误判成失败、永久停止同步。
    """
    class _Provider:
        auto_update_models = True
        _last_fetch_models_error = "上一轮的旧错误"

        async def fetch_upstream_model_list(self):
            # 本次成功，且按约定清空标记（框架要求）
            self._last_fetch_models_error = ""
            return [{"id": "fresh"}]

    upserts, deletes = _patch_refresh_simple(monkeypatch, _Provider(), [])

    asyncio.run(ModelClientPool.refresh_models())

    assert upserts == [("prov", "fresh", "fresh")]
    assert deletes == []


# ------------------- 需求 3（续）：get_initialized_provider -------------------


def _real_pool(clients):
    from rate_limiter import ProviderPool

    pool = ProviderPool("prov", type("CC", (), {}))
    pool.clients = list(clients)
    return pool


def test_get_initialized_provider_skips_disabled_accounts():
    """禁用账号即使凭据已初始化也不被选中去拉上游模型列表。"""
    disabled = _stub_client("off", disabled=True)
    live = _stub_client("on", disabled=False)
    pool = _real_pool([disabled, live])

    # 轮询起点落在禁用账号上，也必须跳过它选到 live。
    for _ in range(3):
        assert asyncio.run(pool.get_initialized_provider()) is live.provider


def test_get_initialized_provider_returns_none_when_all_disabled():
    """账号全部禁用 → 返回 None，不退化成随便挑一个。"""
    pool = _real_pool([_stub_client("a", disabled=True), _stub_client("b", disabled=True)])
    assert asyncio.run(pool.get_initialized_provider()) is None


def test_get_initialized_provider_still_ignores_uninitialized():
    """未初始化的账号仍然跳过（原有语义不变）。"""
    pool = _real_pool([_stub_client("a", disabled=False, is_init=False)])
    assert asyncio.run(pool.get_initialized_provider()) is None


def test_fetch_upstream_models_all_disabled_reports_disabled_not_init_failure(monkeypatch):
    """账号全禁用时报「已全部禁用」，而不是误导性的「均未完成初始化」。"""
    pool = _real_pool([_stub_client("a", disabled=True)])
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    assert exc.value.status_code == 400
    assert "已全部禁用" in exc.value.detail


# ------------------- 需求 5：拉列表回报所用账号 + 「重新获取」锁定同一账号 -------------------


def _pin_pool(provider_by_username):
    """每个 username 对应一个可 fetch_upstream_model_list 的 provider 替身。"""
    clients = []
    for username, provider in provider_by_username.items():
        clients.append(type("C", (), {
            "username": username,
            "disabled": False,
            "provider": provider,
        })())
    return _real_pool(clients)


def _patch_no_existing_models(monkeypatch):
    async def list_provider_models(cls, provider=None):
        return []

    monkeypatch.setattr(rate_limiter.PostgresClient, "list_provider_models", classmethod(list_provider_models))


def test_fetch_upstream_models_reports_account_used(monkeypatch):
    """不锁账号时按池轮询挑号，响应必须回读实际用的 username（前端要显示是哪个账号拉的）。"""

    class _Provider:
        _last_fetch_models_error = ""

        def __init__(self, name):
            self.username = name
            self.calls = 0

        def is_init(self):
            return True

        async def fetch_upstream_model_list(self):
            self.calls += 1
            return [{"id": "openai/gpt-4o"}]

    a = _Provider("a")
    b = _Provider("b")
    pool = _pin_pool({"a": a, "b": b})
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    _patch_no_existing_models(monkeypatch)

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov"))
    # 轮询首个命中 a；username 随响应返回，前端据此显示 + 重新获取回传。
    assert result["account"] == "a"
    assert a.calls == 1 and b.calls == 0


def test_fetch_upstream_models_pinned_account_is_used_not_rotated(monkeypatch):
    """「重新获取」传 account → 必须只用这个账号，即使轮询本来会挑到别的号。"""

    class _Provider:
        _last_fetch_models_error = ""

        def __init__(self, name):
            self.username = name
            self.calls = 0

        def is_init(self):
            return True

        async def fetch_upstream_model_list(self):
            self.calls += 1
            return [{"id": "openai/gpt-4o"}]

    a = _Provider("a")
    b = _Provider("b")
    pool = _pin_pool({"a": a, "b": b})
    # 先把轮询指针推到 b 上，证明锁定生效不是「碰巧轮到」。
    pool._current_index = 1
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))
    _patch_no_existing_models(monkeypatch)

    result = asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov", "a"))
    assert result["account"] == "a"
    assert a.calls == 1 and b.calls == 0


def test_fetch_upstream_models_pinned_unknown_account_raises(monkeypatch):
    """锁定的账号在渠道下不存在 → 直接报错，不静默换成别的号（否则显示的账号名与数据不符）。"""

    class _Provider:
        _last_fetch_models_error = ""

        def is_init(self):
            return True

        async def fetch_upstream_model_list(self):
            return [{"id": "m"}]

    pool = _pin_pool({"a": _Provider()})
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov", "ghost"))
    assert exc.value.status_code == 400
    assert "ghost" in exc.value.detail


def test_fetch_upstream_models_pinned_uninit_failure_raises_503(monkeypatch):
    """锁定的账号初始化不起来 → 503 点名该账号，不换号兜底。"""

    class _Provider:
        _last_fetch_models_error = ""

        def __init__(self):
            self.init_calls = 0

        def is_init(self):
            return False

        async def init_auth(self, _force):
            self.init_calls += 1
            raise RuntimeError("登录失败")

        async def fetch_upstream_model_list(self):
            return [{"id": "m"}]

    p = _Provider()
    pool = _pin_pool({"a": p})
    monkeypatch.setattr(ModelClientPool, "get_provider_pool", classmethod(lambda cls, name: pool))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(ModelClientPool.fetch_provider_upstream_models("prov", "a"))
    assert exc.value.status_code == 503
    assert "a" in exc.value.detail
    assert p.init_calls == 1
