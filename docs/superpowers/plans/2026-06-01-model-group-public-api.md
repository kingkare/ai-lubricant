# 模型组 Public API 语义修正实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让模型组在 public API 中成为按当前 API Key 动态追加的可调用模型 ID，避免临时不可用时从 `/v1/models` 消失或被误判为模型不存在。

**Architecture:** 真实模型列表继续由 `provider_models` / `_model_routes` 管理；模型组不再混入基础模型列表，而是在 `/v1/models` 入口按当前 API Key 权限动态追加。模型组元数据按请求实时从组内成员显式元数据计算，运行时成员不可用只影响调用阶段返回 429，不影响模型组展示。

**Tech Stack:** Python 3、FastAPI、asyncpg/PostgreSQL、pytest。

---

## 已确认需求

- `/v1/models` 需要鉴权，返回“当前 API Key 可用的真实模型 + 当前 API Key 可用的模型组”。
- 模型组不是 provider 模型；provider 模型刷新不决定模型组是否存在。
- 模型组即使组内成员当前都冻结、冷却、限流或无可用账号，也要出现在 `/v1/models`。
- 调用模型组时，如果当前无可用成员，返回 429，让客户端按临时不可用重试。
- 模型组元数据动态计算，不依赖刷新模型列表时合成的缓存。
- 模型组 `max_tokens` / `max_context_tokens` 使用组内有显式元数据成员的最小值。
- 模型组缺少可计算元数据时，不出现在 `/v1/models`；请求时返回 400。
- 创建/更新模型组时禁止和已有模型组名或真实模型名冲突。
- 不改管理端模型元数据列表页；它直接查数据库，不是 public `/v1/models`。
- 不改 provider models 刷新逻辑。
- 不改管理端页面现有接口分工。

## 文件结构

- 修改 `model_metadata.py`：新增动态模型组元数据计算函数；新路径不再依赖 `groups_by_id`。
- 修改 `rate_limiter.py`：基础模型列表只返回真实模型；新增按 API Key 生成模型组响应；模型组无候选成员时返回 429。
- 修改 `main.py`：`/v1/models` 强制鉴权并追加可访问模型组；请求校验使用动态模型组元数据。
- 修改 `admin.py`：补强模型组创建/更新重名校验，保证不会撞真实模型名或其他模型组名。
- 修改 `tests/test_model_group_metadata.py`：把旧的模型组缓存测试改成动态元数据测试。
- 修改 `tests/test_retry_policy.py` 或新增 `tests/test_model_group_public_api.py`：覆盖 `/v1/models` API Key 可见性、模型组展示、429 语义。
- 修改 `tests/test_model_routing_db.py`：覆盖模型组重名校验。

---

### 任务 1：为动态模型组元数据写失败测试

**Files:**
- Modify: `tests/test_model_group_metadata.py`
- Modify: `model_metadata.py`

- [ ] **Step 1: Replace cached-group expectations with dynamic metadata tests**

把 `tests/test_model_group_metadata.py` 中依赖 `groups_by_id` / `rebuild_group_metadata_cache()` 的测试改成下面这组动态元数据测试：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import model_metadata as mm


def _patch_group(monkeypatch, members):
    monkeypatch.setattr(config.Config, "get_enabled_model_groups", lambda: {"test": {"models": members, "enabled": True}})
    monkeypatch.setattr(config.Config, "get_model_group_models", lambda name: list(members) if name == "test" else [])
    monkeypatch.setattr(config.Config, "is_model_group", lambda m: m == "test")


def test_dynamic_group_metadata_uses_smallest_member_tokens(monkeypatch):
    _patch_group(monkeypatch, ["m1", "m2"])
    mm._apply_payload_to_cache({
        "default": {},
        "models": [
            {"model_id": "m1", "max_tokens": 8192, "max_context_tokens": 200000, "function_calling": True},
            {"model_id": "m2", "max_tokens": 4096, "max_context_tokens": 32000, "function_calling": False},
        ],
    })

    rec = mm.get_dynamic_model_group_metadata("test")

    assert rec is not None
    assert rec["max_tokens"] == 4096
    assert rec["max_context_tokens"] == 32000
    assert rec["function_calling"] is False
    assert "model_id" not in rec


def test_dynamic_group_metadata_reacts_to_group_member_changes(monkeypatch):
    members = ["m1"]
    _patch_group(monkeypatch, members)
    mm._apply_payload_to_cache({
        "default": {},
        "models": [
            {"model_id": "m1", "max_tokens": 8192, "max_context_tokens": 200000},
            {"model_id": "m2", "max_tokens": 2048, "max_context_tokens": 64000},
        ],
    })

    assert mm.get_dynamic_model_group_metadata("test")["max_tokens"] == 8192

    members[:] = ["m2"]

    assert mm.get_dynamic_model_group_metadata("test")["max_tokens"] == 2048


def test_dynamic_group_metadata_absent_when_no_member_metadata(monkeypatch):
    _patch_group(monkeypatch, ["m1", "m2"])
    mm._apply_payload_to_cache({"default": {}, "models": []})

    assert mm.get_dynamic_model_group_metadata("test") is None


def test_group_name_is_not_explicit_model_metadata(monkeypatch):
    _patch_group(monkeypatch, ["m1"])
    mm._apply_payload_to_cache({"default": {}, "models": [{"model_id": "m1", "max_tokens": 4096}]})

    assert mm.has_explicit_metadata("test") is False
    assert mm.get_explicit_metadata("test") is None
    assert mm.get_dynamic_model_group_metadata("test") is not None
```

- [ ] **Step 2: Run failing metadata tests**

Run:

```bash
pytest tests/test_model_group_metadata.py -v
```

Expected: FAIL because `get_dynamic_model_group_metadata` does not exist and old `has_explicit_metadata("test")` still treats model groups as explicit metadata.

- [ ] **Step 3: Add dynamic metadata implementation**

Modify `model_metadata.py`:

1. Add this function after `get_explicit_metadata()`:

```python
def get_dynamic_model_group_metadata(group_name: str) -> dict | None:
    """Return model-group metadata computed from current member metadata, without caching the group."""
    _ensure_cache_sync()
    if not group_name:
        return None
    try:
        import config
        if not config.Config.is_model_group(group_name):
            return None
        members = config.Config.get_model_group_models(group_name)
    except Exception:
        return None

    default = dict(_cache.get("default") or FALLBACK_DEFAULT_MODEL_METADATA)
    records = []
    by_id = _cache.get("models_by_id") or {}
    for member in members:
        record = by_id.get(member)
        if record:
            records.append({k: v for k, v in record.items() if k != "model_id"})
    if not records:
        return None

    def int_value(record: dict, key: str) -> int:
        try:
            return int(record.get(key) or default.get(key) or 0)
        except (TypeError, ValueError):
            return int(default.get(key) or 0)

    base = min(records, key=lambda item: int_value(item, "max_tokens"))
    metadata = dict(base)
    for key in ("max_tokens", "max_context_tokens"):
        values = [int_value(record, key) for record in records]
        values = [value for value in values if value > 0]
        if values:
            metadata[key] = min(values)
    return metadata
```

2. Change `has_explicit_metadata()` to only check real model metadata:

```python
def has_explicit_metadata(model_id: str) -> bool:
    _ensure_cache_sync()
    if not model_id:
        return False
    return model_id in (_cache.get("models_by_id") or {})
```

3. Keep `groups_by_id` code untouched for this task unless tests force removal. New public API paths must use `get_dynamic_model_group_metadata()` instead.

- [ ] **Step 4: Run metadata tests to verify pass**

Run:

```bash
pytest tests/test_model_group_metadata.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit metadata task**

```bash
git add model_metadata.py tests/test_model_group_metadata.py
git commit -m "fix: compute model group metadata dynamically"
```

---

### 任务 2：让基础模型列表只包含真实模型，并新增 API Key 可见模型组响应

**Files:**
- Modify: `rate_limiter.py`
- Test: `tests/test_model_group_public_api.py`

- [ ] **Step 1: Add failing tests for model group response construction**

Create `tests/test_model_group_public_api.py` with:

```python
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import model_metadata
from rate_limiter import ModelClientPool


def setup_function():
    ModelClientPool._model_routes = {}
    ModelClientPool._models = []


def test_base_models_response_excludes_model_groups(monkeypatch):
    ModelClientPool._model_routes = {"real-model": [{"provider": "p", "upstream_model_id": "real-model"}]}
    monkeypatch.setattr(model_metadata, "has_explicit_metadata", lambda model: model == "real-model")
    monkeypatch.setattr(model_metadata, "apply_model_metadata", lambda provider, item: item | {"max_tokens": 4096})
    monkeypatch.setattr(config.Config, "get_enabled_model_groups", lambda: {"GA": {"models": ["real-model"], "enabled": True}})

    response = ModelClientPool.get_models_response()

    ids = [item["id"] for item in response["data"]]
    assert ids == ["real-model"]


def test_accessible_model_groups_include_group_without_member_routes(monkeypatch):
    ModelClientPool._model_routes = {}
    monkeypatch.setattr(config.Config, "get_enabled_model_groups", lambda: {"GA": {"models": ["m1"], "enabled": True, "remark": "Group A"}})
    monkeypatch.setattr(config.Config, "get_model_group_models", lambda name: ["m1"] if name == "GA" else [])
    monkeypatch.setattr(config.Config, "get_model_group_remark", lambda name: "Group A" if name == "GA" else "")
    monkeypatch.setattr(config.Config, "model_group_hidden_in_admin", lambda name: False)
    monkeypatch.setattr(config.Config, "model_group_allows_api_key", lambda name, api_key, api_key_name: api_key == "key-a")
    monkeypatch.setattr(model_metadata, "get_dynamic_model_group_metadata", lambda name: {"max_tokens": 4096, "max_context_tokens": 32000} if name == "GA" else None)

    groups = ModelClientPool.get_accessible_model_groups_response("key-a", "Key A")

    assert groups == [{
        "id": "GA",
        "object": "model",
        "created": groups[0]["created"],
        "owned_by": "model-group",
        "type": "model_group",
        "models": ["m1"],
        "remark": "Group A",
        "name": "Group A",
        "max_tokens": 4096,
        "max_context_tokens": 32000,
    }]
    assert isinstance(groups[0]["created"], int)
    assert groups[0]["created"] <= int(time.time())


def test_accessible_model_groups_filter_api_key_and_missing_metadata(monkeypatch):
    monkeypatch.setattr(config.Config, "get_enabled_model_groups", lambda: {
        "allowed": {"models": ["m1"], "enabled": True},
        "denied": {"models": ["m2"], "enabled": True},
        "no-meta": {"models": ["m3"], "enabled": True},
    })
    monkeypatch.setattr(config.Config, "get_model_group_models", lambda name: [name + "-member"])
    monkeypatch.setattr(config.Config, "get_model_group_remark", lambda name: "")
    monkeypatch.setattr(config.Config, "model_group_allows_api_key", lambda name, api_key, api_key_name: name != "denied")
    monkeypatch.setattr(model_metadata, "get_dynamic_model_group_metadata", lambda name: None if name == "no-meta" else {"max_tokens": 4096})

    groups = ModelClientPool.get_accessible_model_groups_response("key-a", "Key A")

    assert [group["id"] for group in groups] == ["allowed"]
```

- [ ] **Step 2: Run failing response tests**

Run:

```bash
pytest tests/test_model_group_public_api.py -v
```

Expected: FAIL because `get_accessible_model_groups_response()` does not exist and base model response still includes model groups.

- [ ] **Step 3: Remove model group appending from base model response**

In `rate_limiter.py`, edit `ModelClientPool._build_models_from_routes()` so it only builds real models. The function body should be:

```python
@classmethod
def _build_models_from_routes(cls) -> list[dict]:
    model_list: list[dict] = []
    for model_id in cls._model_routes:
        if not has_explicit_metadata(model_id):
            continue
        item = {
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "model-api",
        }
        apply_model_metadata(None, item)
        model_list.append(item)
    return model_list
```

- [ ] **Step 4: Add accessible model group response builder**

In `rate_limiter.py`, add this classmethod after `get_models_response()`:

```python
@classmethod
def get_accessible_model_groups_response(cls, api_key: str | None = None, api_key_name: str | None = None) -> list[dict]:
    groups = []
    real_model_ids = set(cls._model_routes.keys())
    for group_name, group in config.Config.get_enabled_model_groups().items():
        if group_name in real_model_ids:
            continue
        if not config.Config.model_group_allows_api_key(group_name, api_key, api_key_name):
            continue
        metadata = get_dynamic_model_group_metadata(group_name)
        if not metadata:
            continue
        item = {
            "id": group_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "model-group",
            "type": "model_group",
            "models": config.Config.get_model_group_models(group_name),
        }
        remark = config.Config.get_model_group_remark(group_name)
        if remark:
            item["remark"] = remark
            item["name"] = remark
        item.update(metadata)
        groups.append(item)
    return groups
```

Also update the import at the top of `rate_limiter.py`:

```python
from model_metadata import apply_model_metadata, get_model_metadata, has_explicit_metadata, get_dynamic_model_group_metadata
```

- [ ] **Step 5: Run response tests to verify pass**

Run:

```bash
pytest tests/test_model_group_public_api.py -v
```

Expected: PASS.

- [ ] **Step 6: Run existing model group metadata tests**

Run:

```bash
pytest tests/test_model_group_metadata.py tests/test_model_group_public_api.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit response task**

```bash
git add rate_limiter.py tests/test_model_group_public_api.py
git commit -m "fix: append accessible model groups in public model list"
```

---

### 任务 3：让 `/v1/models` 鉴权并追加当前 API Key 可见模型组

**Files:**
- Modify: `main.py`
- Test: `tests/test_model_group_public_api.py`

- [ ] **Step 1: Add failing tests for `/v1/models` handler behavior**

Append to `tests/test_model_group_public_api.py`:

```python
import pytest
import main


@pytest.mark.anyio
async def test_list_models_requires_api_key_and_appends_accessible_groups(monkeypatch):
    calls = []

    async def fake_extract_api_key(request, authorization, support_x_api_key=False):
        calls.append((authorization, support_x_api_key))
        return "key-a"

    monkeypatch.setattr(main, "_extract_api_key", fake_extract_api_key)
    monkeypatch.setattr(main, "_api_key_name", lambda api_key: "Key A")
    monkeypatch.setattr(main.ModelClientPool, "get_models_response", lambda: {"object": "list", "data": [{"id": "real-model"}]})
    monkeypatch.setattr(main.ModelClientPool, "get_accessible_model_groups_response", lambda api_key, api_key_name: [{"id": "GA", "type": "model_group"}])

    response = await main.list_models(request=object(), authorization="Bearer key-a")

    assert calls == [("Bearer key-a", True)]
    assert response == {"object": "list", "data": [{"id": "real-model"}, {"id": "GA", "type": "model_group"}]}
```

- [ ] **Step 2: Run failing handler test**

Run:

```bash
pytest tests/test_model_group_public_api.py::test_list_models_requires_api_key_and_appends_accessible_groups -v
```

Expected: FAIL because `list_models()` does not accept request/authorization and does not append groups.

- [ ] **Step 3: Update `/v1/models` handler**

In `main.py`, replace:

```python
@app.get("/v1/models")
@app.get("/models")
async def list_models():
    """获取模型列表 —— 直接返回内存里聚合好的 _models，id 即 provider_models.model_id。"""
    return ModelClientPool.get_models_response()
```

with:

```python
@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request, authorization: Optional[str] = Header(None)):
    """获取当前 API Key 可用模型列表：真实模型 + 可访问模型组。"""
    api_key = await _extract_api_key(request, authorization, support_x_api_key=True)
    api_key_name = _api_key_name(api_key)
    response = ModelClientPool.get_models_response()
    return {
        "object": "list",
        "data": [
            *(response.get("data") or []),
            *ModelClientPool.get_accessible_model_groups_response(api_key, api_key_name),
        ],
    }
```

- [ ] **Step 4: Run handler tests**

Run:

```bash
pytest tests/test_model_group_public_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit `/v1/models` handler task**

```bash
git add main.py tests/test_model_group_public_api.py
git commit -m "fix: include authorized model groups in v1 models"
```

---

### 任务 4：请求校验使用动态模型组元数据，不在前置校验阶段判定成员可用性

**Files:**
- Modify: `main.py`
- Test: `tests/test_model_group_public_api.py`

- [ ] **Step 1: Add failing tests for request validation**

Append to `tests/test_model_group_public_api.py`:

```python
from fastapi import HTTPException


def test_validate_chat_request_accepts_model_group_without_routable_members(monkeypatch):
    body = {"model": "GA", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1024}

    monkeypatch.setattr(main.ModelClientPool, "get_model_routes", lambda model: [])
    monkeypatch.setattr(main.ModelClientPool, "get_model_info", lambda model: None)
    monkeypatch.setattr(main.config.Config, "is_model_group", lambda model: model == "GA")
    monkeypatch.setattr(main.config.Config, "model_group_allows_api_key", lambda model, api_key, api_key_name: True)
    monkeypatch.setattr(main.model_metadata, "get_dynamic_model_group_metadata", lambda model: {"max_tokens": 4096, "max_context_tokens": 32000})
    monkeypatch.setattr(main, "_validate_context_budget", lambda model, body, endpoint: None)

    model, messages = main._validate_chat_request(body, api_key="key-a")

    assert model == "GA"
    assert messages == body["messages"]


def test_validate_chat_request_rejects_model_group_max_tokens_over_dynamic_limit(monkeypatch):
    body = {"model": "GA", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8192}

    monkeypatch.setattr(main.ModelClientPool, "get_model_routes", lambda model: [])
    monkeypatch.setattr(main.ModelClientPool, "get_model_info", lambda model: None)
    monkeypatch.setattr(main.config.Config, "is_model_group", lambda model: model == "GA")
    monkeypatch.setattr(main.config.Config, "model_group_allows_api_key", lambda model, api_key, api_key_name: True)
    monkeypatch.setattr(main.model_metadata, "get_dynamic_model_group_metadata", lambda model: {"max_tokens": 4096, "max_context_tokens": 32000})
    monkeypatch.setattr(main, "_validate_context_budget", lambda model, body, endpoint: None)

    with pytest.raises(HTTPException) as exc:
        main._validate_chat_request(body, api_key="key-a")

    assert exc.value.status_code == 400
    assert "超过模型上限" in exc.value.detail


def test_validate_chat_request_rejects_model_group_without_metadata(monkeypatch):
    body = {"model": "GA", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1024}

    monkeypatch.setattr(main.ModelClientPool, "get_model_routes", lambda model: [])
    monkeypatch.setattr(main.config.Config, "is_model_group", lambda model: model == "GA")
    monkeypatch.setattr(main.config.Config, "model_group_allows_api_key", lambda model, api_key, api_key_name: True)
    monkeypatch.setattr(main.model_metadata, "get_dynamic_model_group_metadata", lambda model: None)

    with pytest.raises(HTTPException) as exc:
        main._validate_chat_request(body, api_key="key-a")

    assert exc.value.status_code == 400
    assert "未配置可计算元数据" in exc.value.detail
```

- [ ] **Step 2: Run failing validation tests**

Run:

```bash
pytest tests/test_model_group_public_api.py::test_validate_chat_request_accepts_model_group_without_routable_members tests/test_model_group_public_api.py::test_validate_chat_request_rejects_model_group_max_tokens_over_dynamic_limit tests/test_model_group_public_api.py::test_validate_chat_request_rejects_model_group_without_metadata -v
```

Expected: FAIL because `_validate_chat_request()` still checks `get_model_group_available_members()` and uses `has_explicit_metadata(model)` for groups.

- [ ] **Step 3: Refactor `_validate_chat_request()` model existence and metadata selection**

In `main.py`, replace the model/group validation block in `_validate_chat_request()` from line around `api_key_name = _api_key_name(api_key)` through the `requested_max_tokens` / `model_info` setup with:

```python
    api_key_name = _api_key_name(api_key)
    model_info = None
    if ModelClientPool.get_model_routes(model):
        if not model_metadata.has_explicit_metadata(model):
            raise HTTPException(status_code=400, detail=f"模型 '{model}' 未配置元数据，不可使用")
        model_info = ModelClientPool.get_model_info(model) or model_metadata.get_explicit_metadata(model) or {}
    else:
        if not config.Config.is_model_group(model):
            raise HTTPException(status_code=404, detail=f"模型 '{model}' 不存在")
        if not config.Config.model_group_allows_api_key(model, api_key, api_key_name):
            raise HTTPException(status_code=403, detail=f"API Key 无权使用模型组 '{model}'")
        group_metadata = model_metadata.get_dynamic_model_group_metadata(model)
        if not group_metadata:
            raise HTTPException(status_code=400, detail=f"模型组 '{model}' 未配置可计算元数据")
        model_info = group_metadata

    requested_max_tokens = _request_max_tokens(body)
```

Then change the later line:

```python
    model_info = ModelClientPool.get_model_info(model) or {}
```

so it is removed, because `model_info` is already set by the block above.

Keep the existing max-token comparison block:

```python
    if requested_max_tokens and model_info:
        model_max_tokens = model_info.get("max_tokens")
        ...
```

- [ ] **Step 4: Run validation tests**

Run:

```bash
pytest tests/test_model_group_public_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Run context budget tests**

Run:

```bash
pytest tests/test_context_budget.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit validation task**

```bash
git add main.py tests/test_model_group_public_api.py
git commit -m "fix: validate model groups with dynamic metadata"
```

---

### 任务 5：模型组选路无可用成员时返回 429

**Files:**
- Modify: `rate_limiter.py`
- Test: `tests/test_model_group_public_api.py`

- [ ] **Step 1: Add failing test for no routable model group members**

Append to `tests/test_model_group_public_api.py`:

```python
import pytest
from rate_limiter import NoAvailableAccountError


@pytest.mark.anyio
async def test_model_group_without_candidate_members_raises_429(monkeypatch):
    ModelClientPool._model_routes = {}
    monkeypatch.setattr(config.Config, "is_model_group", lambda model: model == "GA")
    monkeypatch.setattr(config.Config, "get_model_group_models", lambda model: ["m1"] if model == "GA" else [])
    monkeypatch.setattr(model_metadata, "has_explicit_metadata", lambda model: model == "m1")

    with pytest.raises(NoAvailableAccountError) as exc:
        await ModelClientPool._get_available_client_with_provider("GA", messages=[])

    assert exc.value.status_code == 429
    assert "当前无可用成员" in exc.value.detail
```

- [ ] **Step 2: Run failing 429 test**

Run:

```bash
pytest tests/test_model_group_public_api.py::test_model_group_without_candidate_members_raises_429 -v
```

Expected: FAIL because the current code raises `HTTPException` 400 with “不存在或模型组为空”.

- [ ] **Step 3: Change empty candidate handling**

In `rate_limiter.py`, replace:

```python
        if not candidate_models:
            raise HTTPException(status_code=400, detail=f"模型 `{model_id}` 不存在或模型组为空")
```

with:

```python
        if not candidate_models:
            if config.Config.is_model_group(model_id):
                raise NoAvailableAccountError(status_code=429, detail=f"模型组 '{model_id}' 当前无可用成员")
            raise HTTPException(status_code=404, detail=f"模型 `{model_id}` 不存在")
```

- [ ] **Step 4: Run public API tests**

Run:

```bash
pytest tests/test_model_group_public_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit 429 task**

```bash
git add rate_limiter.py tests/test_model_group_public_api.py
git commit -m "fix: return retryable error for unavailable model groups"
```

---

### 任务 6：补强模型组重名校验

**Files:**
- Modify: `admin.py`
- Modify: `tests/test_model_routing_db.py`

- [ ] **Step 1: Add failing tests for create/update group name conflicts**

Append to `tests/test_model_routing_db.py`:

```python
@pytest.mark.anyio
async def test_create_model_group_rejects_existing_real_model_name(monkeypatch):
    async def fake_require_admin(authorization=None):
        return "admin-token"

    async def fake_get_group(name):
        return None

    async def fake_list_groups():
        return {}

    monkeypatch.setattr(admin, "_require_admin", fake_require_admin)
    monkeypatch.setattr(admin.PostgresClient, "get_model_group", fake_get_group)
    monkeypatch.setattr(admin.PostgresClient, "list_model_groups", fake_list_groups)
    monkeypatch.setattr(admin, "_real_model_ids", lambda: {"real-model"})

    with pytest.raises(admin.HTTPException) as exc:
        await admin.create_model_group({"name": "real-model", "models": ["member"]}, token="Bearer admin")

    assert exc.value.status_code == 400
    assert "与真实模型冲突" in exc.value.detail


@pytest.mark.anyio
async def test_update_model_group_rejects_rename_to_existing_group(monkeypatch):
    async def fake_require_admin(authorization=None):
        return "admin-token"

    async def fake_get_group(name):
        if name == "old":
            return {"name": "old", "models": ["member"]}
        if name == "taken":
            return {"name": "taken", "models": ["member"]}
        return None

    monkeypatch.setattr(admin, "_require_admin", fake_require_admin)
    monkeypatch.setattr(admin.PostgresClient, "get_model_group", fake_get_group)

    with pytest.raises(admin.HTTPException) as exc:
        await admin.update_model_group("old", {"name": "taken", "models": ["member"]}, token="Bearer admin")

    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


@pytest.mark.anyio
async def test_update_model_group_rejects_rename_to_real_model(monkeypatch):
    async def fake_require_admin(authorization=None):
        return "admin-token"

    async def fake_get_group(name):
        if name == "old":
            return {"name": "old", "models": ["member"]}
        return None

    async def fake_list_groups():
        return {"old": {"name": "old", "models": ["member"]}}

    monkeypatch.setattr(admin, "_require_admin", fake_require_admin)
    monkeypatch.setattr(admin.PostgresClient, "get_model_group", fake_get_group)
    monkeypatch.setattr(admin.PostgresClient, "list_model_groups", fake_list_groups)
    monkeypatch.setattr(admin, "_real_model_ids", lambda: {"real-model"})

    with pytest.raises(admin.HTTPException) as exc:
        await admin.update_model_group("old", {"name": "real-model", "models": ["member"]}, token="Bearer admin")

    assert exc.value.status_code == 400
    assert "与真实模型冲突" in exc.value.detail
```

- [ ] **Step 2: Run failing conflict tests**

Run:

```bash
pytest tests/test_model_routing_db.py::test_create_model_group_rejects_existing_real_model_name tests/test_model_routing_db.py::test_update_model_group_rejects_rename_to_existing_group tests/test_model_routing_db.py::test_update_model_group_rejects_rename_to_real_model -v
```

Expected: at least one FAIL if create/update paths do not consistently check real-model conflicts.

- [ ] **Step 3: Ensure `_validate_model_group_write()` enforces real-model conflict**

In `admin.py`, make `_validate_model_group_write(name: str, group: dict)` construct the target name and validate it before delegating to `_validate_model_routing()`:

```python
async def _validate_model_group_write(name: str, group: dict):
    groups = dict(await PostgresClient.list_model_groups())
    target_name = (group.get("name") or name or "").strip()
    if not target_name:
        raise HTTPException(status_code=400, detail="model group name required")
    real_models = _real_model_ids()
    if target_name in real_models:
        raise HTTPException(status_code=400, detail=f"模型组 '{target_name}' 与真实模型冲突")
    groups.pop(name, None)
    if target_name in groups:
        raise HTTPException(status_code=409, detail="model group already exists")
    groups[target_name] = {"name": target_name, **group}
    await _validate_model_routing(await PostgresClient.list_model_routes(), groups)
```

- [ ] **Step 4: Run conflict tests**

Run:

```bash
pytest tests/test_model_routing_db.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit conflict validation task**

```bash
git add admin.py tests/test_model_routing_db.py
git commit -m "fix: prevent model group name conflicts"
```

---

### 任务 7：Regression suite and cleanup

**Files:**
- Modify only if tests identify stale imports or dead direct dependencies.

- [ ] **Step 1: Run focused regression tests**

Run:

```bash
pytest tests/test_model_group_metadata.py tests/test_model_group_public_api.py tests/test_model_routing_db.py tests/test_context_budget.py -v
```

Expected: PASS.

- [ ] **Step 2: Search for stale group metadata cache dependencies**

Run:

```bash
python - <<'PY'
from pathlib import Path
for path in [Path('model_metadata.py'), Path('rate_limiter.py'), Path('main.py'), Path('admin.py')]:
    text = path.read_text(encoding='utf-8')
    for token in ('groups_by_id', 'rebuild_group_metadata_cache', 'get_model_group_available_members'):
        if token in text:
            print(f'{path}:{token}')
PY
```

Expected: `groups_by_id` may remain only inside old unused cache code in `model_metadata.py`; `rebuild_group_metadata_cache` should not be required by new paths; `get_model_group_available_members` may remain only if existing code still uses it outside `_validate_chat_request()`.

- [ ] **Step 3: Remove stale call sites if present**

If Step 2 shows `main.py:get_model_group_available_members`, remove that call from `_validate_chat_request()` because model group member availability must be handled during routing, not validation.

Expected final `_validate_chat_request()` model-group path contains no call to:

```python
ModelClientPool.get_model_group_available_members(model)
```

- [ ] **Step 4: Run all tests**

Run:

```bash
pytest -v
```

Expected: PASS.

- [ ] **Step 5: Manual verification with running app**

Start the app using the existing project command. If there is no documented command, run the same command used by this repo's normal local development flow.

Verify:

```bash
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer <API_KEY>"
```

Expected:

- Real models are present.
- Authorized model groups are appended with `type: "model_group"`.
- A model group with currently unavailable members still appears if it has dynamic metadata.
- A model group without dynamic metadata does not appear.

Verify a temporarily unavailable model group:

```bash
curl -s http://localhost:8000/v1/messages \
  -H "Authorization: Bearer <API_KEY>" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"GA","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}'
```

Expected if no members are routable: HTTP 429 with a message containing `当前无可用成员`.

- [ ] **Step 6: Commit cleanup task**

```bash
git add main.py rate_limiter.py model_metadata.py admin.py tests/test_model_group_metadata.py tests/test_model_group_public_api.py tests/test_model_routing_db.py
git commit -m "test: verify model group public api semantics"
```

---

## Self-review notes

- Spec coverage: plan covers authenticated `/v1/models`, API Key-filtered model groups, dynamic metadata, 429 for temporarily unavailable groups, missing metadata behavior, and create/update name conflicts.
- Scope check: plan intentionally does not modify management model metadata pages, provider model refresh, channel model management, or frontend page routing.
- Placeholder scan: no task relies on unspecified implementation; each code path includes exact function names and expected tests.
- Type consistency: new function names are `get_dynamic_model_group_metadata()` and `get_accessible_model_groups_response()` throughout.
