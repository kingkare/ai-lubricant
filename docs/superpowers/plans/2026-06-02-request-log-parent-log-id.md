# Request Log Path: parent_log_id Self-FK

> **给执行 Agent 的要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐项执行。步骤用 checkbox（`- [ ]`）跟踪。

**目标：** 删 `parent_request_id`、删 `retry_path`、新增 `parent_log_id BIGINT REFERENCES request_logs(id)`；主行 `parent_log_id=NULL`，attempt 行 `parent_log_id=主行 id`，各管各的 token；统计和详情都按新口径走。

**架构：** `request_logs` 自引用父子关系。每次用户请求先插一条主行（`parent_log_id=NULL`）拿到主行 `id`，后续每次渠道调用插 attempt 行（`parent_log_id=主行 id`）。详情接口按 `parent_log_id` 反查 attempt 行，统计按 `parent_log_id IS NULL / IS NOT NULL` 区分用户请求和渠道调用。

**技术栈：** Python 3、FastAPI、asyncpg/PostgreSQL、`static/admin.html` 原生 JavaScript、pytest。

---

## 已确认需求

- 字段：删除 `parent_request_id`、删除 `retry_path`；新增 `parent_log_id BIGINT REFERENCES request_logs(id)`（可空）。
- 写入：主行 `parent_log_id=NULL`；attempt 行 `parent_log_id=主行的 id`；主行和 attempt 行各自的 token 字段独立写入、各自汇总。
- `admin.py` 测试请求：`parent_log_id=NULL`（单一顶层行）。
- 统计：
  - 用户请求数 = `count(*) WHERE parent_log_id IS NULL`
  - 渠道调用数 = `count(*) WHERE parent_log_id IS NOT NULL AND success=true`
- 前端：列表显示 request_id；详情按 `parent_log_id` 关联展示路径（按 `created_at` 排序）。
- 清理：删 `_channel_attempt_request_id`、`get_request_log_detail` 中 retry_path 反查逻辑、按新口径重写所有统计 SQL。
- 测试：覆盖新契约。

**Why:** 这版契约相比 docs/superpowers/plans/2026-06-01-request-log-path-db-id.md 改用自引用 BIGINT FK 替代 `retry_path` JSONB 列表。FK 是关系数据库的原生表达方式，比 JSONB 列表更易查询、统计、保证一致性；删除 `retry_path` 和 `parent_request_id` 之后表更紧凑，关系只由一个 `parent_log_id` 列表达。
**How to apply:** 任何写入、读取、统计 SQL 都按 `parent_log_id` 处理；不再出现 `parent_request_id` 和 `retry_path` 字段或相关分支逻辑。

## 文件结构

- 修改 `db.py`：schema 迁移、`insert_request_log` 接受 `parent_log_id`、`query_request_logs` 默认只看主行、`get_request_log_detail` 按 `parent_log_id` 反查 attempt 行、统计 SQL 全部按新口径重写。
- 修改 `main.py`：先插主行拿主行 id，attempt 行用 `parent_log_id=主行 id`；删 `_channel_attempt_request_id`；最终更新主行时不再写 `retry_path`。
- 修改 `admin.py`：测试请求保持单行 `parent_log_id=NULL`；使用 short request id；token 通过 `normalize_usage` 写入。
- 修改 `static/admin.html`：列表显示 request_id；详情路径面板按 attempt 行渲染；不再有 retry_path 字段。
- 修改 `tests/test_retry_policy.py`：覆盖新契约。
- 修改 `tests/test_admin_request_logs_ui.py`：覆盖新契约。

---

### 任务 1：写新契约的失败测试

**文件：**
- 修改：`tests/test_retry_policy.py`
- 修改：`tests/test_admin_request_logs_ui.py`

- [ ] **步骤 1：在 `tests/test_retry_policy.py` 添加契约测试**

```python
def test_request_logs_schema_uses_parent_log_id_not_parent_request_id():
    source = open("db.py", encoding="utf-8").read()
    create_body = source[source.index("CREATE TABLE IF NOT EXISTS request_logs"):source.index(")", source.index("CREATE TABLE IF NOT EXISTS request_logs"))]
    assert "parent_log_id" in create_body
    assert "parent_request_id" not in create_body
    assert "retry_path" not in create_body

    migrate = source[source.index("ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS parent_log_id"):source.index("\n", source.index("ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS parent_log_id"))]
    assert "BIGINT REFERENCES request_logs(id)" in migrate


def test_insert_request_log_writes_parent_log_id_column():
    source = open("db.py", encoding="utf-8").read()
    body = source[source.index("async def insert_request_log"):source.index("    @classmethod", source.index("async def insert_request_log") + 1)]
    assert "parent_log_id" in body
    assert "parent_request_id" not in body
    assert "retry_path" not in body
    assert "$" in body and "::int" in body  # parent_log_id 作为 int/bigint 参数


def test_query_request_logs_lists_all_rows_not_filtered_by_parent_log_id():
    source = open("db.py", encoding="utf-8").read()
    body = source[source.index("async def query_request_logs"):source.index("    @classmethod", source.index("async def query_request_logs") + 1)]
    assert "parent_log_id IS NULL" not in body
    assert "parent_request_id" not in body
    assert "retry_path" not in body
    assert "jsonb_array_length" not in body


def test_get_request_log_detail_loads_attempts_via_parent_log_id():
    source = open("db.py", encoding="utf-8").read()
    body = source[source.index("async def get_request_log_detail"):source.index("    @classmethod\n    async def dashboard_stats", source.index("async def get_request_log_detail"))]
    assert "parent_log_id = $1" in body
    assert "parent_request_id" not in body
    assert "WHERE request_id = ANY" not in body
    assert "retry_path" not in body
    assert "attempts" in body


def test_dashboard_stats_and_billing_use_parent_log_id_filter():
    db_source = open("db.py", encoding="utf-8").read()
    for method in ("dashboard_stats", "billing_usage", "recent_logs", "total_successful_request_count", "token_stats", "account_daily_usage"):
        start = db_source.index(f"async def {method}")
        end = db_source.index("    @classmethod", start + 1)
        body = db_source[start:end]
        assert "parent_log_id" in body, f"{method} should filter by parent_log_id"
        assert "parent_request_id" not in body
        assert "retry_path" not in body


def test_main_inserts_main_row_first_and_attempts_use_parent_log_id():
    main_source = open("main.py", encoding="utf-8").read()
    assert "_channel_attempt_request_id" not in main_source
    assert "parent_log_id" in main_source
    assert "parent_request_id" not in main_source
    assert "retry_path" not in main_source or main_source.count("retry_path") == 0


def test_admin_test_request_writes_parent_log_id_null():
    admin_source = open("admin.py", encoding="utf-8").read()
    test_body = admin_source[admin_source.index("async def test_provider_accounts"):admin_source.index("@router.get", admin_source.index("async def test_provider_accounts"))]
    assert "parent_log_id" not in test_body or "parent_log_id=None" in test_body
    assert "parent_request_id" not in test_body
    assert "retry_path" not in test_body
```

- [ ] **步骤 2：在 `tests/test_admin_request_logs_ui.py` 添加前端契约测试**

```python
def test_request_logs_table_renders_request_id_for_all_rows():
    body = function_body(admin_html(), "renderRequestLogsPage")
    assert "r.request_id" in body
    assert "r.parent_log_id" in body
    assert "parent_request_id" not in body
    assert "retry_path" not in body


def test_request_log_detail_renders_path_from_attempts_sorted_by_created_at():
    body = function_body(admin_html(), "showRequestLogDetail")
    assert "attempts" in body
    assert "created_at" in body
    assert "parent_log_id" in body
    assert "parent_request_id" not in body
    assert "retry_path" not in body
    assert "parseLogJsonValue(r.retry_path" not in body
```

- [ ] **步骤 3：运行失败测试**

```bash
pytest tests/test_retry_policy.py::test_request_logs_schema_uses_parent_log_id_not_parent_request_id tests/test_retry_policy.py::test_insert_request_log_writes_parent_log_id_column tests/test_retry_policy.py::test_query_request_logs_filters_to_top_level_rows_only tests/test_retry_policy.py::test_get_request_log_detail_loads_attempts_via_parent_log_id tests/test_retry_policy.py::test_dashboard_stats_and_billing_use_parent_log_id_filter tests/test_retry_policy.py::test_main_inserts_main_row_first_and_attempts_use_parent_log_id tests/test_retry_policy.py::test_admin_test_request_writes_parent_log_id_null tests/test_admin_request_logs_ui.py::test_request_logs_table_renders_request_id_for_all_rows tests/test_admin_request_logs_ui.py::test_request_log_detail_renders_path_from_attempts_sorted_by_created_at -v
```

预期：FAIL（因为当前代码使用 `parent_request_id` 和 `retry_path`）。

---

### 任务 2：db.py schema 与 insert 迁移

**文件：**
- 修改：`db.py`

- [ ] **步骤 1：调整 `CREATE TABLE request_logs`**

把表定义里的 `parent_request_id TEXT,` 和 `retry_path JSONB NOT NULL DEFAULT '[]'::jsonb,` 替换成：

```sql
parent_log_id BIGINT REFERENCES request_logs(id),
```

- [ ] **步骤 2：调整迁移行**

替换：

```python
await conn.execute("ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS parent_request_id TEXT")
```

为：

```python
await conn.execute("ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS parent_log_id BIGINT REFERENCES request_logs(id)")
await conn.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS parent_request_id")
await conn.execute("ALTER TABLE request_logs DROP COLUMN IF EXISTS retry_path")
```

- [ ] **步骤 3：调整 `insert_request_log` SQL**

INSERT 列与值改为：

```python
row = await conn.fetchrow(
    """
    INSERT INTO request_logs(
        request_id, parent_log_id, created_at, api_key, api_key_name, provider_name, account_username,
        model, endpoint, success, status, stream, duration_ms, first_token_ms,
        prompt_tokens, completion_tokens, total_tokens, cached_tokens, cache_creation_tokens,
        request_body, router_request_body, router_response_body, response_body,
        request_headers, response_headers, upstream_status, error
    ) VALUES(
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20::jsonb,$21::jsonb,$22::jsonb,$23::jsonb,$24::jsonb,$25::jsonb,$26,$27
    )
    RETURNING id
    """,
    data.get("request_id"),
    data.get("parent_log_id"),
    created_at,
    data.get("api_key"), data.get("api_key_name"),
    data.get("provider_name"), data.get("account_username"), data.get("model"), data.get("endpoint"),
    bool(data.get("success")), data.get("status"), bool(data.get("stream")), int(data.get("duration_ms") or 0),
    data.get("first_token_ms"),
    int(data.get("prompt_tokens") or 0), int(data.get("completion_tokens") or 0),
    int(data.get("total_tokens") or 0), int(data.get("cached_tokens") or 0), int(data.get("cache_creation_tokens") or 0),
    cls._dumps(data.get("request_body")) if data.get("request_body") is not None else None,
    cls._dumps(data.get("router_request_body")) if data.get("router_request_body") is not None else None,
    cls._dumps(data.get("router_response_body")) if data.get("router_response_body") is not None else None,
    cls._dumps(data.get("response_body")) if data.get("response_body") is not None else None,
    cls._dumps(data.get("request_headers")) if data.get("request_headers") is not None else None,
    cls._dumps(data.get("response_headers")) if data.get("response_headers") is not None else None,
    data.get("upstream_status"), data.get("error"),
)
```

返回值即 `parent_log_id`（如果调用方传了）。SQL 现在不写 `retry_path` 和 `parent_request_id`。

- [ ] **步骤 4：运行契约测试**

```bash
pytest tests/test_retry_policy.py::test_request_logs_schema_uses_parent_log_id_not_parent_request_id tests/test_retry_policy.py::test_insert_request_log_writes_parent_log_id_column -v
```

预期：db.py 部分通过；其他契约测试仍可能因 main.py/admin.py/前端未改而失败。

---

### 任务 3：db.py 查询与统计重写

**文件：**
- 修改：`db.py`

- [ ] **步骤 1：`query_request_logs` 不过滤父/子（列表展示全部行）**

不要加 `where.append("parent_log_id IS NULL")`。SELECT 不再返回 `parent_request_id` / `retry_path`，也不再需要 `jsonb_array_length` 计算 retry_count。

新版 SQL：

```sql
SELECT id, request_id, extract(epoch from created_at)::float AS time, api_key_name, provider_name,
       account_username, model, model AS requested_model, model AS actual_model,
       endpoint, success, status, stream, duration_ms, first_token_ms,
       upstream_status,
       prompt_tokens, completion_tokens, total_tokens, cached_tokens, cache_creation_tokens,
       error IS NOT NULL AND error <> '' AS has_error,
       CASE WHEN error IS NULL THEN '' ELSE left(error, 300) END AS error_preview
FROM request_logs
```

`actual_model` 暂保持为 `model`（如果详情接口需要，单独从 `attempts` 末行取）。

- [ ] **步骤 2：`get_request_log_detail` 按 `parent_log_id` 反查 attempt 行**

```python
@classmethod
async def get_request_log_detail(cls, log_id: int) -> dict | None:
    async with cls.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT *, extract(epoch from created_at) AS time FROM request_logs WHERE id=$1",
            log_id,
        )
        if not row:
            return None
        data = dict(row)
        data["request_body"] = cls._loads_json(data.get("request_body"), {})
        data["router_request_body"] = cls._loads_json(data.get("router_request_body"), {})
        data["router_response_body"] = cls._loads_json(data.get("router_response_body"), {})
        data["response_body"] = cls._loads_json(data.get("response_body"), {})
        data["request_headers"] = cls._loads_json(data.get("request_headers"), {})
        data["response_headers"] = cls._loads_json(data.get("response_headers"), {})

        attempt_rows = await conn.fetch(
            """
            SELECT id, request_id, extract(epoch from created_at)::float AS time,
                   provider_name, account_username, model, endpoint, success, status, stream,
                   duration_ms, first_token_ms, upstream_status,
                   prompt_tokens, completion_tokens, total_tokens, cached_tokens, cache_creation_tokens,
                   error IS NOT NULL AND error <> '' AS has_error,
                   CASE WHEN error IS NULL THEN '' ELSE left(error, 300) END AS error_preview
            FROM request_logs
            WHERE parent_log_id = $1
            ORDER BY created_at ASC, id ASC
            """,
            log_id,
        )
    data["attempts"] = [dict(r) for r in attempt_rows]
    data["requested_model"] = data.get("model")
    actual_model = data.get("model")
    if attempt_rows:
        actual_model = attempt_rows[-1].get("model") or actual_model
    data["actual_model"] = actual_model
    return data
```

- [ ] **步骤 3：dashboard_stats 改用 `parent_log_id` 过滤**

```python
request_where_sql = " WHERE " + " AND ".join(where) if where else ""
channel_where_sql = request_where_sql + (" AND " if request_where_sql else " WHERE ") + "parent_log_id IS NOT NULL"
client_where_sql = request_where_sql + (" AND " if request_where_sql else " WHERE ") + "parent_log_id IS NULL"
exception_where_sql = channel_where_sql + " AND (success=false OR error IS NOT NULL OR status NOT IN ('ok','success','200'))"
```

summary 查询改用 `client_where_sql`。所有原本 `client_where_sql` 引用（by_model、model_call_distribution、model_token_distribution、model_response_average）保持 `parent_log_id IS NULL`。所有原本 `channel_where_sql` 引用（by_provider、provider_*、exception_distribution）改为 `parent_log_id IS NOT NULL`。删除 `retry_count` 字段（不再有 `retry_path`）。

- [ ] **步骤 4：`billing_usage` 改 `parent_log_id IS NULL`**

```python
"WHERE api_key=$1 AND success=true AND parent_log_id IS NULL"
```

- [ ] **步骤 5：`recent_logs` 改 `parent_log_id IS NULL`**

```python
"WHERE parent_log_id IS NULL"
```

- [ ] **步骤 6：`total_successful_request_count` 改 `parent_log_id IS NULL`**

```python
"SELECT count(*) FROM request_logs WHERE success=true AND parent_log_id IS NULL"
```

- [ ] **步骤 7：`token_stats` 改 `parent_log_id IS NULL`**

```python
"WHERE success=true AND parent_log_id IS NULL"
```

- [ ] **步骤 8：`account_daily_usage` 改 `parent_log_id IS NOT NULL`**

```python
where = "account_username=$1 AND success=true AND parent_log_id IS NOT NULL AND created_at >= $2 AND created_at < $3"
```

- [ ] **步骤 9：运行契约测试**

```bash
pytest tests/test_retry_policy.py::test_dashboard_stats_and_billing_use_parent_log_id_filter tests/test_retry_policy.py::test_get_request_log_detail_loads_attempts_via_parent_log_id tests/test_retry_policy.py::test_query_request_logs_filters_to_top_level_rows_only -v
```

预期：db.py 全部通过。

---

### 任务 4：main.py 重构写入流程

**文件：**
- 修改：`main.py`

- [ ] **步骤 1：删除 `_channel_attempt_request_id`**

直接删除函数定义和所有调用。

- [ ] **步骤 2：插入主行 helper**

```python
async def _insert_main_request_log(
    *,
    request_id: str,
    created_at: float,
    api_key: str | None,
    api_key_name: str | None,
    model: str,
    endpoint: str,
    stream: bool,
    request_body: dict,
    request_headers: dict | None,
) -> int | None:
    log_data = {
        "request_id": request_id,
        "created_at": created_at,
        "api_key": api_key,
        "api_key_name": api_key_name,
        "model": model,
        "endpoint": endpoint,
        "stream": stream,
        "success": False,
        "status": "requesting",
        "duration_ms": 0,
        "request_body": request_body,
        "request_headers": request_headers,
    }
    return await _log_request(log_data)
```

主行先插 `parent_log_id=NULL`（默认）。

- [ ] **步骤 3：attempt 行使用 `parent_log_id=main_id`**

替换 `_build_channel_attempt_log` 内部生成的 log_data：

```python
def _build_channel_attempt_log(
    *,
    parent_log_id: int | None,
    request_id: str,
    route_info: dict,
    model: str,
    messages: list,
    stream: bool,
    api_key: str | None,
    api_key_name: str | None,
    request_headers: dict | None,
    start_time: float,
    duration_ms: int,
    success: bool,
    status: str,
    response_body: dict | None,
    error: str,
    usage: dict | None = None,
) -> dict:
    provider_name = route_info.get("provider")
    username = route_info.get("account")
    log_data = {
        "request_id": request_id,
        "parent_log_id": parent_log_id,
        "created_at": start_time,
        "api_key": api_key,
        "api_key_name": api_key_name,
        "provider_name": provider_name,
        "account_username": username,
        "model": model,
        "endpoint": _channel_attempt_endpoint(provider_name, username),
        "success": success,
        "status": status,
        "stream": stream,
        "duration_ms": duration_ms,
        "first_token_ms": route_info.get("ttft_ms"),
        "request_body": {"model": model, "messages": messages, "stream": stream},
        "router_request_body": route_info.get("router_request_body"),
        "router_response_body": route_info.get("router_response_body"),
        "response_body": response_body,
        "request_headers": request_headers,
        "response_headers": route_info.get("response_headers"),
        "upstream_status": route_info.get("upstream_status"),
        "error": error,
    }
    log_data.update(usage or _zero_usage())
    return log_data
```

不再设置 `parent_request_id`、不再写 `retry_path`。

- [ ] **步骤 4：`_finalize_channel_attempt_log` 接受 `parent_log_id`**

签名改为：

```python
async def _finalize_channel_attempt_log(
    *,
    parent_log_id: int | None,
    request_id: str | None,
    route_info: dict,
    model: str,
    messages: list,
    stream: bool,
    api_key: str | None,
    api_key_name: str | None,
    request_headers: dict | None,
    attempt_start: float,
    success: bool,
    status: str,
    response_body: dict | None,
    error: str,
    usage: dict | None = None,
) -> None:
    if not request_id or not route_info.get("provider"):
        return
    log_data = _build_channel_attempt_log(
        parent_log_id=parent_log_id,
        request_id=request_id,
        route_info=route_info,
        model=model,
        messages=messages,
        stream=stream,
        api_key=api_key,
        api_key_name=api_key_name,
        request_headers=request_headers,
        start_time=attempt_start,
        duration_ms=_duration_ms(attempt_start),
        success=success,
        status=status,
        response_body=response_body,
        error=error,
        usage=usage,
    )
    if route_info.get("log_id"):
        await PostgresClient.update_request_log(route_info["log_id"], log_data)
    else:
        _schedule_log_request(log_data)
```

- [ ] **步骤 5：`update_request_log` 写 `parent_log_id`**

UPDATE 语句接受 `parent_log_id`：

```python
await conn.execute(
    """
    UPDATE request_logs SET
        parent_log_id=$2,
        success=$3,
        status=$4,
        duration_ms=$5,
        first_token_ms=$6,
        prompt_tokens=$7,
        completion_tokens=$8,
        total_tokens=$9,
        cached_tokens=$10,
        cache_creation_tokens=$11,
        router_request_body=$12::jsonb,
        router_response_body=$13::jsonb,
        response_body=$14::jsonb,
        response_headers=$15::jsonb,
        upstream_status=$16,
        error=$17
    WHERE id=$1
    """,
    log_id,
    data.get("parent_log_id"),
    bool(data.get("success")),
    data.get("status"),
    int(data.get("duration_ms") or 0),
    data.get("first_token_ms"),
    int(data.get("prompt_tokens") or 0),
    int(data.get("completion_tokens") or 0),
    int(data.get("total_tokens") or 0),
    int(data.get("cached_tokens") or 0),
    int(data.get("cache_creation_tokens") or 0),
    cls._dumps(data.get("router_request_body")) if data.get("router_request_body") is not None else None,
    cls._dumps(data.get("router_response_body")) if data.get("router_response_body") is not None else None,
    cls._dumps(data.get("response_body")) if data.get("response_body") is not None else None,
    cls._dumps(data.get("response_headers")) if data.get("response_headers") is not None else None,
    data.get("upstream_status"),
    data.get("error"),
)
```

- [ ] **步骤 6：`_chat_with_retry` 使用 `parent_log_id`**

入口接收 `parent_log_id` 关键字参数并传递给 `_build_channel_attempt_log` / `_finalize_channel_attempt_log`。在每个 attempt 起始：

```python
pending_log = _build_channel_attempt_log(
    parent_log_id=parent_log_id,
    request_id=request_id,
    route_info=route_info,
    ...
)
route_info["log_id"] = await _log_request(pending_log)
```

调用 `_finalize_channel_attempt_log` 时传 `parent_log_id=parent_log_id, request_id=request_id, ...`。

- [ ] **步骤 7：三个端点（chat / responses / messages）先插主行再调 `_chat_with_retry`**

`chat_completions` 非流式分支起点附近：

```python
request_id = _new_request_id()
kwargs["request_id"] = request_id
kwargs["request_headers"] = request_headers
start_time = time.time()
main_log_id = await _insert_main_request_log(
    request_id=request_id,
    created_at=start_time,
    api_key=api_key,
    api_key_name=_api_key_name(api_key),
    model=model,
    endpoint="/v1/chat/completions",
    stream=stream,
    request_body=body,
    request_headers=request_headers,
)
kwargs["parent_log_id"] = main_log_id
```

流式分支同样在 yield 前插入主行；在 `finally` 中 `await PostgresClient.update_request_log(main_log_id, log_data)` 更新主行（不要写 `retry_path`）。

非流式分支成功时：

```python
await PostgresClient.update_request_log(main_log_id, {
    "parent_log_id": None,
    "success": True,
    "status": "ok",
    "duration_ms": _duration_ms(start_time),
    "first_token_ms": first_token_ms,
    **usage_for_stats,
    "response_body": result,
    "router_request_body": _last_route_field(last_route_info, "router_request_body"),
    "router_response_body": _last_route_field(last_route_info, "router_response_body"),
    "response_headers": _last_route_response_headers(last_route_info),
    "upstream_status": _last_route_upstream_status(last_route_info),
})
```

非流式分支失败时同样用 update 写主行。

把 `responses_api` / `anthropic_messages` 端点做对应改动。

- [ ] **步骤 8：删除 retry_path 相关的所有写入**

搜索 `main.py` 中所有 `retry_path` 出现，逐一改为不写 retry_path。原本写在主行日志里的 `retry_path` 字段直接删除。流式分支的 `finally` 不再累加 `retry_path` 列表；`last_route_info` 仍保留用于 `first_token_ms` / `upstream_status` 等字段。

- [ ] **步骤 9：运行契约测试**

```bash
pytest tests/test_retry_policy.py::test_main_inserts_main_row_first_and_attempts_use_parent_log_id -v
```

预期：PASS。

---

### 任务 5：admin.py 测试请求适配

**文件：**
- 修改：`admin.py`

- [ ] **步骤 1：保持单行 `parent_log_id=NULL`**

`test_provider_accounts` 中 `log_data` 字典不再出现 `parent_request_id` / `retry_path`。不显式设置 `parent_log_id`（默认 NULL）即可。

- [ ] **步骤 2：使用 short request id**

```python
"request_id": f"req_{uuid.uuid4().hex[:12]}",
```

- [ ] **步骤 3：运行测试**

```bash
pytest tests/test_retry_policy.py::test_admin_test_request_writes_parent_log_id_null -v
```

预期：PASS。

---

### 任务 6：前端列表与详情

**文件：**
- 修改：`static/admin.html`

- [ ] **步骤 1：`renderRequestLogsPage` 列表行展示 request_id**

行单元格增加：

```javascript
<span class="tag" style="font-family:var(--fontM);font-size:10px">${esc(r.request_id || '-')}</span>
```

列表展示全部行（主行与 attempt 行同表），不再读 `r.retry_path`；详情仍按 `attempts` 渲染。

- [ ] **步骤 2：`showRequestLogDetail` 详情用 `attempts` 渲染路径**

替换原 retry_path 渲染：

```javascript
const attempts = Array.isArray(r.attempts) ? r.attempts : [];
const attemptsSorted = [...attempts].sort((a, b) => (a.time || 0) - (b.time || 0));
const pathHtml = renderAttemptsPath(attemptsSorted, r.request_id, r.id);
```

新增 `renderAttemptsPath`：

```javascript
function renderAttemptsPath(attempts, requestId, currentLogId) {
  if (!attempts.length) return `<div class="empty">请求 ID ${esc(requestId || '-')} 没有记录渠道请求路径</div>`;
  return `<div style="display:flex;flex-direction:column;gap:8px">
    <div class="kv-row"><span class="key">请求 ID</span><span class="val" style="font-family:var(--fontM)">${esc(requestId || '-')}</span></div>
    ${attempts.map(item => {
      const logId = item.id;
      const provider = item.provider_name || '-';
      const account = item.account_username || '-';
      const model = item.model || '-';
      const status = item.success ? '成功' : (item.status || '失败');
      const duration = ((item.duration_ms || 0) / 1000).toFixed(2);
      const ttft = item.first_token_ms ? `${(item.first_token_ms / 1000).toFixed(2)}s` : '-';
      const tokens = (item.prompt_tokens || 0) + (item.completion_tokens || 0);
      return `<div class="log-block" style="padding:10px;border-radius:10px">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">
          <button class="btn sm" onclick="showRequestLogDetail(${Number(logId) || 0})">DB #${esc(logId)}</button>
          <span class="tag ${item.success ? 'on' : 'off'}">${esc(status)}</span>
          <span class="tag">${esc(provider)} / ${esc(account)} / ${esc(model)}</span>
        </div>
        <div style="font-size:12px;color:var(--text2)">用时 ${duration}s · TTFT ${ttft} · Token ${tokens}</div>
        ${item.error_preview ? `<div style="font-size:12px;color:var(--red);margin-top:6px">${esc(formatLogError(item.error_preview))}</div>` : ''}
      </div>`;
    }).join('')}
  </div>`;
}
```

- [ ] **步骤 3：删除 `renderRetryPathPanel` 中的 retry_path 解析**

旧 `renderRetryPathPanel(retryPath, currentLogId)` 替换为上面 `renderAttemptsPath(attempts, requestId, currentLogId)`，调用方不再传 `retry_path`。

- [ ] **步骤 4：运行前端契约测试**

```bash
pytest tests/test_admin_request_logs_ui.py::test_request_logs_table_renders_request_id_for_all_rows tests/test_admin_request_logs_ui.py::test_request_log_detail_renders_path_from_attempts_sorted_by_created_at -v
```

预期：PASS。

---

### 任务 7：清理 main.py / db.py 中残留旧字段

**文件：**
- 修改：`main.py`
- 修改：`db.py`

- [ ] **步骤 1：删除 `parent_request_id` 文本**

在 main.py / db.py 中搜索 `parent_request_id` 并全部删除。

- [ ] **步骤 2：删除 `retry_path` 文本**

在 main.py / db.py 中搜索 `retry_path` 并全部删除（包括字段写入、SQL、helper 函数、JSON 字段处理）。

- [ ] **步骤 3：删除 `_channel_attempt_request_id` 及其调用**

```bash
grep -n "_channel_attempt_request_id" main.py tests/test_retry_policy.py
```

预期：0 行。

- [ ] **步骤 4：运行契约测试**

```bash
pytest tests/test_retry_policy.py::test_request_log_code_does_not_use_parent_request_id -v 2>/dev/null || true
grep -rn "parent_request_id\|retry_path\|_channel_attempt_request_id" main.py db.py admin.py static/admin.html tests/test_retry_policy.py
```

预期：无残留。

---

### 任务 8：完整验证

**文件：**
- 不修改源码，除非验证发现失败。

- [ ] **步骤 1：运行请求日志相关测试**

```bash
pytest tests/test_admin_request_logs_ui.py tests/test_retry_policy.py -v
```

预期：PASS。

- [ ] **步骤 2：运行相关 DB/model 测试**

```bash
pytest tests/test_model_routing_db.py tests/test_model_group_metadata.py tests/test_model_group_selection_strategy.py tests/test_admin_channel_optimization.py tests/test_admin_limit_policy_ui.py tests/test_admin_model_routing_ui.py tests/test_admin_time_range_picker.py -v
```

预期：PASS。

- [ ] **步骤 3：运行全量测试**

```bash
pytest -v
```

预期：PASS。

- [ ] **步骤 4：手动验证管理端**

启动项目后在管理端验证：

```text
1. 发起一次无重试成功请求。
2. 发起一次至少重试一次的请求。
3. 打开请求日志列表。
4. 确认列表只显示主行（parent_log_id IS NULL），每行带 request_id。
5. 打开详情。
6. 确认“请求路径”显示 request ID，以及每个 DB # 的渠道/账号/模型/状态/耗时/token，按 created_at 排序。
7. 确认没有“父请求”/`parent_request_id`/`retry_path` 字段。
8. 确认 token 字段按 输入/输出/缓存读/缓存写入 显示，且 total 只等于输入+输出。
9. 确认主行与 attempt 行的 token 各自独立。
```

- [ ] **步骤 5：提交（仅在用户授权时）**

```bash
git add main.py db.py admin.py static/admin.html tests/test_retry_policy.py tests/test_admin_request_logs_ui.py
git commit -m "feat: replace parent_request_id + retry_path with parent_log_id self-FK"
```

---

## 自检

- 需求覆盖：parent_log_id FK、主行/attempt 写入、admin 测试请求、统计口径、前端展示、清理任务、测试均映射到任务。
- 类型一致：所有路径 `parent_log_id` 使用 BIGINT；插入与查询都按 BIGINT 处理。
- 风险说明：`ALTER TABLE DROP COLUMN parent_request_id` 与 `DROP COLUMN retry_path` 是有意的破坏性迁移；上线前应确认历史日志是否需要备份。
