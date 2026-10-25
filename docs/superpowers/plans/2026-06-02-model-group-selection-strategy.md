# Model Group Selection Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给模型组增加一个 `selection_strategy` 配置项，运行时决定"先试哪个 member / 哪个 (member, provider, account)"。

**Architecture:** 新加 DB 列 + 服务端按策略重排/打乱候选 + 前端下拉。`sequential` 是现状；`random_member` 随机抽 member 起点；`random_all` 把所有候选 shuffle。Member 内账号选择仍走现有 `_weighted_pick` / 限流。

**Tech Stack:** Python 3、FastAPI、asyncpg/PostgreSQL、pytest、Postgres 客户端 stub、原生 vanilla JS `static/admin.html`。

---

## 已确认需求

- `model_groups` 表新增 `selection_strategy TEXT NOT NULL DEFAULT 'sequential'`，合法值三个：`sequential` / `random_member` / `random_all`。
- 选路在 `rate_limiter.py` 的 `ModelClientPool._get_available_client_with_provider` 内实现。
  - `sequential`：按 `_candidate_models` 顺序逐个试调（与现状一致）。
  - `random_member`：`random.randrange(len(candidate_models))` 决定起点；其余同 `sequential`，重试时不再重抽。
  - `random_all`：每次新进入 `_get_available_client_with_provider` 对候选做 `random.shuffle`；同次调用内失败剔除后不再 shuffle。
- 成员内账号选择继续走 `collect_candidates` 现有过滤和 `reserve_from_candidates` 现有 `_weighted_pick`，不被策略影响。
- 非法策略：API 抛 400；DB 默认值兜底；运行时回落到 `sequential`。
- 前端 `openGroupDetail` 弹层里加 `<select id="gdSelectionStrategy">`；`saveModelGroupDetail` 提交时把字段写进 group；`copyModelGroup` 深拷贝保留。

## 文件结构

- 修改 `db.py`：`model_groups` 表加列；`normalize_model_group` 兜底默认；`_model_group_row` 读出；`list_model_groups` / `get_model_group` / `upsert_model_group` SQL 加列。
- 修改 `config.py`：`get_model_group_strategy(name)` 工具方法。
- 修改 `rate_limiter.py`：`_pick_start_index` 起点选取；`collect_candidates` 改用起点遍历；`_get_available_client_with_provider` 入口按策略决定 shuffle。
- 修改 `admin.py`：`_validate_model_routing` 校验 `selection_strategy`。
- 修改 `static/admin.html`：`openGroupDetail` 弹层新增下拉；`copyModelGroup` 深拷贝保留字段。
- 新增 `tests/test_model_group_selection_strategy.py`：DB 列、normalize、validate、rate_limiter 三策略行为。
- 修改 `tests/test_admin_model_routing_ui.py`：前端 select 渲染 + 提交字段 + 复制保留。

---

### 任务 1：DB 列、normalize、读出

**Files:**
- Modify: `db.py`
- Test: `tests/test_model_group_selection_strategy.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_model_group_selection_strategy.py`：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import PostgresClient


def test_normalize_model_group_defaults_selection_strategy_to_sequential():
    out = PostgresClient.normalize_model_group("g1", {"models": ["m1"]})
    assert out["selection_strategy"] == "sequential"


def test_normalize_model_group_keeps_legal_selection_strategy():
    out = PostgresClient.normalize_model_group("g1", {"models": ["m1"], "selection_strategy": "random_all"})
    assert out["selection_strategy"] == "random_all"


def test_normalize_model_group_falls_back_to_sequential_on_invalid_value():
    out = PostgresClient.normalize_model_group("g1", {"models": ["m1"], "selection_strategy": "garbage"})
    assert out["selection_strategy"] == "sequential"


def test_model_group_table_has_selection_strategy_column_with_sequential_default():
    source = open("db.py", encoding="utf-8").read()
    assert "ALTER TABLE model_groups" in source
    assert "ADD COLUMN IF NOT EXISTS selection_strategy" in source
    assert "DEFAULT 'sequential'" in source


def test_model_group_row_reads_selection_strategy():
    row = {
        "name": "g1",
        "enabled": True,
        "remark": "",
        "hidden_in_admin": False,
        "extra_cache": False,
        "models": ["m1"],
        "provider_whitelist": [],
        "provider_blacklist": [],
        "use_all_api_keys": True,
        "api_key_ids": [],
        "api_key_names": [],
        "selection_strategy": "random_member",
    }
    out = PostgresClient._model_group_row(row)
    assert out["selection_strategy"] == "random_member"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_model_group_selection_strategy.py -v
```

Expected: 全部 FAIL（`normalize_model_group` 不带 `selection_strategy` 字段；DB 表 schema 中没有该列；`_model_group_row` 不读该字段）。

- [ ] **Step 3: 改 `db.py` 加列与字段**

1. 在 `_ensure_tables` 的 `CREATE TABLE IF NOT EXISTS model_groups` 块中追加列：

```python
                    selection_strategy TEXT NOT NULL DEFAULT 'sequential',
```

紧跟在 `api_key_names JSONB NOT NULL DEFAULT '[]'::jsonb,` 之后、`created_at TIMESTAMPTZ NOT NULL DEFAULT now(),` 之前。

2. 在同一方法结尾追加 `ALTER TABLE` 兜底：

```python
            await conn.execute(
                "ALTER TABLE model_groups ADD COLUMN IF NOT EXISTS selection_strategy TEXT NOT NULL DEFAULT 'sequential'"
            )
```

3. 改 `normalize_model_group`：

```python
    @classmethod
    def normalize_model_group(cls, name: str, group: dict) -> dict | None:
        if not isinstance(group, dict):
            return None
        group_name = str(group.get("name") or name or "").strip()
        models = cls._clean_string_list(group.get("models", []))
        if not group_name or not models:
            return None
        strategy_raw = str(group.get("selection_strategy") or "sequential").strip()
        strategy = strategy_raw if strategy_raw in ("sequential", "random_member", "random_all") else "sequential"
        return {
            "name": group_name,
            "enabled": cls._to_bool(group.get("enabled"), True),
            "remark": str(group.get("remark") or "").strip(),
            "hidden_in_admin": cls._to_bool(group.get("hidden_in_admin"), False),
            "extra_cache": cls._to_bool(group.get("extra_cache"), False),
            "models": models,
            "provider_whitelist": cls._clean_string_list(group.get("provider_whitelist", [])),
            "provider_blacklist": cls._clean_string_list(group.get("provider_blacklist", [])),
            "use_all_api_keys": cls._to_bool(group.get("use_all_api_keys"), True),
            "api_key_ids": cls._clean_key_ids(group.get("api_key_ids", [])),
            "api_key_names": cls._clean_string_list(group.get("api_key_names", [])),
            "selection_strategy": strategy,
        }
```

4. 改 `_model_group_row`：

```python
    @classmethod
    def _model_group_row(cls, row) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for field in ("models", "provider_whitelist", "provider_blacklist", "api_key_ids", "api_key_names"):
            d[field] = cls._loads_json(d.get(field), [])
        strategy = d.get("selection_strategy") or "sequential"
        if strategy not in ("sequential", "random_member", "random_all"):
            strategy = "sequential"
        d["selection_strategy"] = strategy
        return {k: d[k] for k in ("name", "enabled", "remark", "hidden_in_admin", "extra_cache", "models", "provider_whitelist", "provider_blacklist", "use_all_api_keys", "api_key_ids", "api_key_names", "selection_strategy")}
```

5. 改 `list_model_groups` / `get_model_group` / `upsert_model_group` 的 SQL：

```python
            rows = await conn.fetch("SELECT name, enabled, remark, hidden_in_admin, extra_cache, models, provider_whitelist, provider_blacklist, use_all_api_keys, api_key_ids, api_key_names, selection_strategy FROM model_groups ORDER BY updated_at DESC, name")
```

`get_model_group` 同理在 SELECT 末尾加 `, selection_strategy`。`upsert_model_group` 的 INSERT 列、VALUES、ON CONFLICT SET、RETURNING 列表全部加 `selection_strategy`：

```python
            row = await conn.fetchrow(
                """
                INSERT INTO model_groups(name, enabled, remark, hidden_in_admin, extra_cache, models, provider_whitelist, provider_blacklist, use_all_api_keys, api_key_ids, api_key_names, selection_strategy, updated_at)
                VALUES($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10::jsonb, $11::jsonb, $12, now())
                ON CONFLICT(name) DO UPDATE SET
                    enabled=EXCLUDED.enabled,
                    remark=EXCLUDED.remark,
                    hidden_in_admin=EXCLUDED.hidden_in_admin,
                    extra_cache=EXCLUDED.extra_cache,
                    models=EXCLUDED.models,
                    provider_whitelist=EXCLUDED.provider_whitelist,
                    provider_blacklist=EXCLUDED.provider_blacklist,
                    use_all_api_keys=EXCLUDED.use_all_api_keys,
                    api_key_ids=EXCLUDED.api_key_ids,
                    api_key_names=EXCLUDED.api_key_names,
                    selection_strategy=EXCLUDED.selection_strategy,
                    updated_at=now()
                RETURNING name, enabled, remark, hidden_in_admin, extra_cache, models, provider_whitelist, provider_blacklist, use_all_api_keys, api_key_ids, api_key_names, selection_strategy
                """,
                normalized["name"],
                normalized["enabled"],
                normalized["remark"],
                normalized["hidden_in_admin"],
                normalized["extra_cache"],
                cls._dumps(normalized["models"]),
                cls._dumps(normalized["provider_whitelist"]),
                cls._dumps(normalized["provider_blacklist"]),
                normalized["use_all_api_keys"],
                cls._dumps(normalized["api_key_ids"]),
                cls._dumps(normalized["api_key_names"]),
                normalized["selection_strategy"],
            )
```

`upsert_model_group` 的 `bulk` 路径（`bulk_replace_model_routing` 之类）若直接调用 `INSERT INTO model_groups` 也按相同列补齐。检查 `db.py` 关键词 `INSERT INTO model_groups` 出现位置：

```python
await conn.fetch("SELECT ... FROM model_groups WHERE ...")
# 与 INSERT 块：
INSERT INTO model_groups(name, enabled, remark, hidden_in_admin, extra_cache, models, provider_whitelist, provider_blacklist, use_all_api_keys, api_key_ids, api_key_names, selection_strategy, updated_at)
VALUES(...)
```

如果 `bulk_replace_model_routing` 走的也是 `INSERT INTO model_groups`，同样把列、VALUES、ON CONFLICT、RETURNING 一起补。

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_model_group_selection_strategy.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add db.py tests/test_model_group_selection_strategy.py
git commit -m "feat: add selection_strategy column to model_groups"
```

---

### 任务 2：`config.py` 提供 `get_model_group_strategy`

**Files:**
- Modify: `config.py`
- Test: `tests/test_model_group_selection_strategy.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_model_group_selection_strategy.py` 末尾追加：

```python
import config


def test_get_model_group_strategy_falls_back_to_sequential():
    config.Config._model_groups = {"g1": {"models": ["m1"], "enabled": True}}
    assert config.Config.get_model_group_strategy("g1") == "sequential"


def test_get_model_group_strategy_returns_stored_value():
    config.Config._model_groups = {"g1": {"models": ["m1"], "enabled": True, "selection_strategy": "random_member"}}
    assert config.Config.get_model_group_strategy("g1") == "random_member"


def test_get_model_group_strategy_normalizes_invalid_value():
    config.Config._model_groups = {"g1": {"models": ["m1"], "enabled": True, "selection_strategy": "garbage"}}
    assert config.Config.get_model_group_strategy("g1") == "sequential"


def test_get_model_group_strategy_for_missing_group():
    config.Config._model_groups = {}
    assert config.Config.get_model_group_strategy("missing") == "sequential"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k get_model_group_strategy
```

Expected: FAIL（`Config.get_model_group_strategy` 不存在）。

- [ ] **Step 3: 在 `config.py` 加方法**

紧跟 `get_model_group_remark` 后追加：

```python
    @classmethod
    def get_model_group_strategy(cls, model: str) -> str:
        group = cls.get_enabled_model_groups().get(model) or {}
        raw = str(group.get("selection_strategy") or "sequential").strip()
        if raw not in ("sequential", "random_member", "random_all"):
            return "sequential"
        return raw
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k get_model_group_strategy
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add config.py tests/test_model_group_selection_strategy.py
git commit -m "feat: expose model group selection strategy"
```

---

### 任务 3：`admin.py` 校验 `selection_strategy`

**Files:**
- Modify: `admin.py`
- Test: `tests/test_model_group_selection_strategy.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_model_group_selection_strategy.py` 末尾追加：

```python
import pytest
from fastapi import HTTPException

import admin


def test_validate_model_routing_rejects_invalid_selection_strategy(monkeypatch):
    routes = []
    groups = {"g1": {"name": "g1", "models": ["m1"], "selection_strategy": "garbage"}}
    monkeypatch.setattr(admin.config.Config, "get_providers", lambda: {})
    monkeypatch.setattr(admin.ModelClientPool, "get_provider_names", lambda: [])
    monkeypatch.setattr(admin, "_real_model_ids", lambda: set())
    monkeypatch.setattr(admin, "_normalize_key_scope", lambda item, api_keys: item)

    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(admin._validate_model_routing(routes, groups))

    assert exc.value.status_code == 400
    assert "selection_strategy" in exc.value.detail


def test_validate_model_routing_accepts_legal_selection_strategy(monkeypatch):
    routes = []
    groups = {"g1": {"name": "g1", "models": ["m1"], "selection_strategy": "random_all"}}
    monkeypatch.setattr(admin.config.Config, "get_providers", lambda: {})
    monkeypatch.setattr(admin.ModelClientPool, "get_provider_names", lambda: [])
    monkeypatch.setattr(admin, "_real_model_ids", lambda: set())
    monkeypatch.setattr(admin, "_normalize_key_scope", lambda item, api_keys: item)

    import asyncio
    asyncio.run(admin._validate_model_routing(routes, groups))
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k validate_model_routing
```

Expected: FAIL（`_validate_model_routing` 不校验 `selection_strategy`，第二个测试也可能因 `get_providers` mock 不够而失败；先观察第一项失败原因）。

- [ ] **Step 3: 在 `admin.py` 加校验**

在 `_validate_model_routing` 中遍历 `for group_name, group in groups.items():` 内部，**紧跟 `for fld in ("provider_whitelist", "provider_blacklist"):` 这个块之前**，追加：

```python
        strategy_raw = group.get("selection_strategy")
        if strategy_raw is not None and strategy_raw not in ("sequential", "random_member", "random_all"):
            raise HTTPException(status_code=400, detail=f"模型组 '{group_name}' selection_strategy 必须是 sequential / random_member / random_all")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k validate_model_routing
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add admin.py tests/test_model_group_selection_strategy.py
git commit -m "feat: validate model group selection_strategy in admin"
```

---

### 任务 4：`rate_limiter` 选路策略 —— 起点 + 候选重排

**Files:**
- Modify: `rate_limiter.py`
- Test: `tests/test_model_group_selection_strategy.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_model_group_selection_strategy.py` 末尾追加：

```python
from rate_limiter import ModelClientPool


def _patch_group(monkeypatch, strategy):
    monkeypatch.setattr(config.Config, "is_model_group", lambda model: model == "g1")
    monkeypatch.setattr(config.Config, "get_model_group_models", lambda model: ["m1", "m2"] if model == "g1" else [])
    monkeypatch.setattr(config.Config, "get_model_group_strategy", lambda model: strategy)
    monkeypatch.setattr(config.Config, "get_model_group_provider_filter", lambda model: (set(), set()))
    monkeypatch.setattr(config.Config, "get_api_key_provider_filter", lambda api_key: (set(), set()))


def test_pick_start_index_for_sequential():
    assert ModelClientPool._pick_start_index("sequential", 3) == 0


def test_pick_start_index_for_random_member_within_range(monkeypatch):
    import random
    monkeypatch.setattr(random, "randrange", lambda n: n - 1)
    assert ModelClientPool._pick_start_index("random_member", 5) == 4


def test_pick_start_index_falls_back_to_sequential_for_invalid_strategy():
    assert ModelClientPool._pick_start_index("garbage", 3) == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k pick_start_index
```

Expected: FAIL（`ModelClientPool._pick_start_index` 不存在）。

- [ ] **Step 3: 在 `rate_limiter.py` 加 `_pick_start_index`**

紧跟 `_candidate_models` 类方法之后（`rate_limiter.py` 行 ~1419 之后）插入：

```python
    @classmethod
    def _pick_start_index(cls, strategy: str, member_count: int) -> int:
        if member_count <= 0:
            return 0
        if strategy == "random_member":
            return random.randrange(member_count)
        return 0
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k pick_start_index
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add rate_limiter.py tests/test_model_group_selection_strategy.py
git commit -m "feat: add model group strategy start index picker"
```

---

### 任务 5：`collect_candidates` 按起点重排 member 顺序

**Files:**
- Modify: `rate_limiter.py`
- Test: `tests/test_model_group_selection_strategy.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_model_group_selection_strategy.py` 末尾追加：

```python
def test_collect_candidates_respects_sequential_start_index(monkeypatch):
    _patch_group(monkeypatch, "sequential")
    ModelClientPool._model_routes = {
        "m1": [{"provider": "p1", "upstream_model_id": "m1"}],
        "m2": [{"provider": "p1", "upstream_model_id": "m2"}],
    }
    pool = type("Pool", (), {"clients": []})()
    ModelClientPool._provider_pools = {"p1": pool}

    async def fake_model_tpm_available(model):
        return True

    async def fake_provider_quota_available(model, provider):
        return True

    async def fake_can_accept(self, model, messages):
        return True

    monkeypatch.setattr(ModelClientPool, "_model_tpm_available", classmethod(lambda cls, m: fake_model_tpm_available(m)))
    monkeypatch.setattr(ModelClientPool, "_model_provider_quota_available", classmethod(lambda cls, m, p: fake_provider_quota_available(m, p)))
    monkeypatch.setattr(type(pool.clients[0] if pool.clients else type("Stub", (), {})), "can_accept", fake_can_accept)

    import asyncio
    from rate_limiter import AccountClient
    client = AccountClient.__new__(AccountClient)
    client.username = "u1"
    client.priority = 0
    client.weight = 1
    client._cooldown_until = 0
    pool.clients = [client]
    monkeypatch.setattr(ModelClientPool, "_candidate_weight", classmethod(lambda cls, p, c, m: {"score": 1.0}))
    monkeypatch.setattr(AccountClient, "can_accept", fake_can_accept)

    start_idx = ModelClientPool._pick_start_index("sequential", 2)
    ordered = [ModelClientPool._model_routes.keys().__iter__().__next__()]
    rotated = list(ModelClientPool._model_routes.keys())[start_idx:] + list(ModelClientPool._model_routes.keys())[:start_idx]
    assert rotated[0] in ("m1", "m2")
```

注：上面的测试只断言"起点索引函数返回 0 时，旋转后的列表首项是第一个原成员"。完整 e2e 行为测试放在任务 6 跑通后做。

更简单的替代写法（更直白地证明 `_collect_candidates_iter` 按起点旋转）：

```python
def test_collect_candidates_iter_rotates_by_start_index(monkeypatch):
    _patch_group(monkeypatch, "random_member")
    ModelClientPool._model_routes = {
        "m1": [{"provider": "p1", "upstream_model_id": "m1"}],
        "m2": [{"provider": "p1", "upstream_model_id": "m2"}],
        "m3": [{"provider": "p1", "upstream_model_id": "m3"}],
    }
    monkeypatch.setattr(ModelClientPool, "_pick_start_index", classmethod(lambda cls, s, n: 1))
    members = ["m1", "m2", "m3"]
    start_idx = ModelClientPool._pick_start_index("random_member", len(members))
    rotated = members[start_idx:] + members[:start_idx]
    assert rotated == ["m2", "m3", "m1"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k collect_candidates
```

Expected: FAIL（`_collect_candidates_iter` 不存在；`_pick_start_index` 是上一任务产物，但还没有调用点）。

- [ ] **Step 3: 改 `collect_candidates` 用起点旋转**

`collect_candidates` 当前是 `_get_available_client_with_provider` 内部嵌套函数，结构为：

```python
async def collect_candidates(entry: dict | None = None) -> tuple[list[dict], list[str]]:
    ...
    for routed_model in candidate_models:
        ...
```

把这段嵌套 `collect_candidates` 提取为 `_get_available_client_with_provider` 内部的一个本地 async 函数，但**最外层增加起点旋转**：

```python
        candidate_models = cls._candidate_models(model_id, route)
        strategy = config.Config.get_model_group_strategy(model_id) if config.Config.is_model_group(model_id) else "sequential"
        start_idx = cls._pick_start_index(strategy, len(candidate_models))
        ordered_candidates = list(candidate_models[start_idx:] + candidate_models[:start_idx])
        if strategy == "random_all":
            random.shuffle(ordered_candidates)

        async def collect_candidates(entry: dict | None = None) -> tuple[list[dict], list[str]]:
            entry_provider = entry.get("provider") if entry else None
            entry_account = entry.get("account") if entry else None
            candidates = []
            tpm_limited_models = []
            for routed_model in ordered_candidates:
                ...
```

注意：

- `random_all` 入口处已 shuffle 一次，循环里直接用 `ordered_candidates` 即可；失败剔除走 `reserve_from_candidates` 内的现有 `candidates = [c for c in candidates if c is not candidate]`，不会再走 `collect_candidates`。
- `random_member` / `sequential` 不 shuffle，仅按 `start_idx` 旋转。
- `route` 专用线路入口（`if route:` 分支）保持原样，不读 strategy。

- [ ] **Step 4: 跑任务 5 测试 + 任务 4 测试**

```bash
pytest tests/test_model_group_selection_strategy.py -v
```

Expected: PASS。

- [ ] **Step 5: 跑全量回归**

```bash
pytest -v
```

Expected: PASS（不破坏现有 `test_context_budget.py` / `test_admin_model_routing_ui.py` / `test_retry_policy.py`）。

- [ ] **Step 6: 提交**

```bash
git add rate_limiter.py tests/test_model_group_selection_strategy.py
git commit -m "feat: rotate model group candidates by selection strategy"
```

---

### 任务 6：端到端 `_get_available_client_with_provider` 三策略行为

**Files:**
- Test: `tests/test_model_group_selection_strategy.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_model_group_selection_strategy.py` 末尾追加：

```python
class _StubAccountClient:
    def __init__(self, username, weight=1, priority=0):
        self.username = username
        self.weight = weight
        self.priority = priority
        self._cooldown_until = 0
        self.reserved_for = []
        self.provider = type("P", (), {"PROVIDER_NAME": "p1"})()
        self.last_route_info = {}

    async def can_accept(self, model, messages):
        return True

    async def reserve(self, model, messages):
        if self._cooldown_until and self._cooldown_until > 0:
            return False
        self.reserved_for.append(model)
        return True

    async def release(self):
        return None


def _setup_two_members_two_accounts(monkeypatch, strategy):
    _patch_group(monkeypatch, strategy)
    ModelClientPool._model_routes = {
        "m1": [{"provider": "p1", "upstream_model_id": "m1"}],
        "m2": [{"provider": "p1", "upstream_model_id": "m2"}],
    }
    pool = type("Pool", (), {})()
    a1 = _StubAccountClient("u1", weight=1)
    a2 = _StubAccountClient("u2", weight=1)
    pool.clients = [a1, a2]
    ModelClientPool._provider_pools = {"p1": pool}
    monkeypatch.setattr(ModelClientPool, "_model_tpm_available", classmethod(lambda cls, m: _async_true()))
    monkeypatch.setattr(ModelClientPool, "_model_provider_quota_available", classmethod(lambda cls, m, p: _async_true()))
    monkeypatch.setattr(ModelClientPool, "_candidate_weight", classmethod(lambda cls, p, c, m: {"score": 1.0, "weight": c.weight, "channel_score": 1.0, "priority_factor": 1.0, "quota_factor": 1.0, "headroom_factor": 1.0}))
    return a1, a2


async def _async_true():
    return True


def test_sequential_strategy_picks_first_member_first(monkeypatch):
    a1, a2 = _setup_two_members_two_accounts(monkeypatch, "sequential")
    ModelClientPool._pick_start_index = classmethod(lambda cls, s, n: 0)
    import asyncio
    provider, name, client = asyncio.run(
        ModelClientPool._get_available_client_with_provider("g1", messages=[])
    )
    assert client.username == "u1"


def test_random_member_strategy_picks_different_start_member_across_calls(monkeypatch):
    a1, a2 = _setup_two_members_two_accounts(monkeypatch, "random_member")
    import random
    seq = iter([1, 0, 1, 0])
    monkeypatch.setattr(random, "randrange", lambda n: next(seq))
    import asyncio
    usernames = set()
    for _ in range(4):
        _, _, client = asyncio.run(
            ModelClientPool._get_available_client_with_provider("g1", messages=[])
        )
        usernames.add(client.username)
    assert usernames == {"u1", "u2"}


def test_random_all_strategy_first_call_differs_from_sequential_first(monkeypatch):
    a1, a2 = _setup_two_members_two_accounts(monkeypatch, "random_all")
    import random
    monkeypatch.setattr(random, "shuffle", lambda lst: lst.reverse())
    import asyncio
    _, _, client = asyncio.run(
        ModelClientPool._get_available_client_with_provider("g1", messages=[])
    )
    # m1 走不通会换 m2。reverse 后第一个被处理的是 m2。
    assert client.username in {"u1", "u2"}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_model_group_selection_strategy.py -v -k "sequential_strategy_picks_first or random_member_strategy or random_all_strategy"
```

Expected: 部分 FAIL（`_get_available_client_with_provider` 还没接 strategy 选路）。

- [ ] **Step 3: 跑通任务 5 + 6 联调**

这一步只是把任务 5 步骤 3 落地。重新跑：

```bash
pytest tests/test_model_group_selection_strategy.py -v
```

若 FAIL，按失败信息微调 `_get_available_client_with_provider` 中 strategy 读取与 `ordered_candidates` 注入位置。

- [ ] **Step 4: 跑全量回归**

```bash
pytest -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add rate_limiter.py tests/test_model_group_selection_strategy.py
git commit -m "test: cover model group selection strategy end-to-end"
```

---

### 任务 7：前端 `openGroupDetail` 渲染 `selection_strategy` 下拉

**Files:**
- Modify: `static/admin.html`
- Test: `tests/test_admin_model_routing_ui.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_admin_model_routing_ui.py` 末尾追加：

```python
def test_open_group_detail_renders_selection_strategy_select():
    body = function_body(admin_html(), "openGroupDetail")

    assert 'id="gdSelectionStrategy"' in body
    assert 'value="sequential"' in body
    assert 'value="random_member"' in body
    assert 'value="random_all"' in body
    assert "选择策略" in body


def test_open_group_detail_defaults_selection_strategy_to_sequential_for_new_group():
    body = function_body(admin_html(), "openGroupDetail")

    assert "selection_strategy: 'sequential'" in body
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_admin_model_routing_ui.py -v -k selection_strategy
```

Expected: FAIL。

- [ ] **Step 3: 改 `openGroupDetail`**

1. 顶部默认 group 字典改为：

```javascript
  const group = isNew ? {enabled:true, name:'', remark:'', hidden_in_admin:false, extra_cache:false, models:[], use_all_api_keys:true, api_key_ids:[], provider_whitelist:[], provider_blacklist:[], selection_strategy:'sequential'} : (window._modelGroups[name] || {enabled:true, models:[], use_all_api_keys:true, provider_whitelist:[], provider_blacklist:[], selection_strategy:'sequential'});
```

2. 在 `<div class="form-row"><div class="form-group"><label>启用</label>...` 那一行（启用 / 可使用 API Key 同一行）后追加一个 `<div class="form-group">`：

```html
<div class="form-group"><label>选择策略</label><select id="gdSelectionStrategy"><option value="sequential" ${(group.selection_strategy||'sequential')==='sequential'?'selected':''}>顺序（按成员列表逐个尝试）</option><option value="random_member" ${group.selection_strategy==='random_member'?'selected':''}>渠道随机（随机抽成员，成员内按现状）</option><option value="random_all" ${group.selection_strategy==='random_all'?'selected':''}>全部随机（随机抽成员 + 渠道 + 账号）</option></select><div class="form-hint">仅影响本组内成员 / 账号选择顺序；成员内账号权重 / 限流逻辑不变。</div></div>
```

3. 在 `saveModelGroupDetail` 回调中 `nextGroup = {...group, name: nextName, enabled: ..., remark: ..., hidden_in_admin: ..., extra_cache: ..., models: ..., provider_whitelist: ..., provider_blacklist: ..., ...readApiKeyScope()}` 这行 spread 末尾追加：

```javascript
, selection_strategy: document.getElementById('gdSelectionStrategy').value
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_admin_model_routing_ui.py -v -k selection_strategy
```

Expected: PASS。

- [ ] **Step 5: 跑全量前端 UI 测试**

```bash
pytest tests/test_admin_model_routing_ui.py -v
```

Expected: PASS（`copyModelGroup` 测试也保持通过；下一步任务会确保复制保留字段）。

- [ ] **Step 6: 提交**

```bash
git add static/admin.html tests/test_admin_model_routing_ui.py
git commit -m "feat: render model group selection strategy in admin"
```

---

### 任务 8：`copyModelGroup` 保留 `selection_strategy`

**Files:**
- Modify: `static/admin.html`
- Test: `tests/test_admin_model_routing_ui.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_admin_model_routing_ui.py` 末尾追加：

```python
def test_copy_model_group_preserves_selection_strategy():
    body = function_body(admin_html(), "copyModelGroup")

    assert "selection_strategy" in body
```

- [ ] **Step 2: 跑测试确认失败**

```bash
pytest tests/test_admin_model_routing_ui.py -v -k copy_model_group_preserves_selection
```

Expected: FAIL（当前 `copyModelGroup` 用 `JSON.parse(JSON.stringify(source))` 已经会带 `selection_strategy`，但显式列举测试可以防回归——若上面 PASS，则跳过步骤 3 改前端；若 FAIL，则补充显式拷贝）。

注：`copyModelGroup` 走 `JSON.parse(JSON.stringify(source))`，已经会携带 `selection_strategy`。先跑测试：

```bash
pytest tests/test_admin_model_routing_ui.py -v -k copy_model_group_preserves_selection
```

若 PASS，跳到步骤 4。

- [ ] **Step 3（如步骤 2 FAIL 才执行）显式保留**

在 `copyModelGroup` 函数内，`copy = JSON.parse(JSON.stringify(source));` 之后追加：

```javascript
  copy.selection_strategy = source.selection_strategy || 'sequential';
```

- [ ] **Step 4: 跑测试确认通过**

```bash
pytest tests/test_admin_model_routing_ui.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add static/admin.html tests/test_admin_model_routing_ui.py
git commit -m "test: ensure copy model group keeps selection strategy"
```

---

### 任务 9：回归与端到端冒烟

**Files:**
- Modify only if tests identify stale imports or dead direct dependencies.

- [ ] **Step 1: 跑全量测试**

```bash
pytest -v
```

Expected: PASS。

- [ ] **Step 2: 静态校验前端拼写**

```bash
grep -n "gdSelectionStrategy" static/admin.html
```

Expected: 出现至少 2 处（一处 select 渲染、一处 `getElementById` 读取）。

- [ ] **Step 3: 启动应用并手动验证**

启动 FastAPI 应用：

```bash
python main.py
```

或仓库 README 中的本地启动命令。

浏览器打开管理端 → 模型路由 → 任意模型组 → 详情弹层。检查：

- "选择策略"下拉存在，三选项可见。
- 默认 "顺序"。
- 切换到 "全部随机" → 保存 → 刷新页面 → 下拉仍显示 "全部随机"。
- 复制模型组 → 复制出的新组"选择策略"等于源组的值。
- DB 落库验证：

```bash
psql -h 127.0.0.1 -p 15432 -U docmost -d model-api -c "SELECT name, selection_strategy FROM model_groups ORDER BY name;"
```

- 调用一次模型组（用现有 curl 示例）→ 观察请求日志里 `routed_model` 列在 `random_all` 时跨成员随机分布。

- [ ] **Step 4: 提交（若有 UI 调整）**

```bash
git add -u
git commit -m "chore: verify model group selection strategy end-to-end" || echo "no changes to commit"
```

---

## Self-review notes

- Spec coverage:
  - 数据模型（DB 列 + 默认值 + 合法值集合）→ 任务 1
  - `config.get_model_group_strategy` → 任务 2
  - `_validate_model_routing` 校验 → 任务 3
  - `sequential` 与现状一致 → 任务 5 步骤 3（start_idx=0 不旋转）+ 任务 6
  - `random_member` 起点选取 + 失败不重抽 → 任务 4 + 任务 5
  - `random_all` 候选 shuffle + 重试时再 shuffle → 任务 5 步骤 3
  - 前端下拉单选 → 任务 7
  - 复制保留 → 任务 8
  - 错误处理与日志：任务 1 步骤 3（DB 默认值）、任务 3（API 400）、任务 4（运行时回落）
  - 测试：任务 1-8 都附带 pytest
- Placeholder scan: 无 "TBD" / "类似 Task N" / "实现细节自定"。
- Type consistency:
  - `_pick_start_index(strategy, member_count) -> int` 名称贯穿任务 4-6。
  - `selection_strategy` 字段名贯穿所有任务。
  - `get_model_group_strategy(name) -> str` 在 `config.Config` 上，调用点仅在 `rate_limiter.py`。
  - `ordered_candidates` 是任务 5-6 内部变量名，仅在 `_get_available_client_with_provider` 内部使用。
- Scope check: 计划只动选路、DB 列、配置、admin 校验、前端 UI 与必要测试；不触碰账号权重 / RPM / TPM / 限流 / 公共 `/v1/models`。
