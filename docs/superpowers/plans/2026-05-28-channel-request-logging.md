# Channel Request Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every real outbound channel request as its own existing request-log table row and make retry-path request IDs clickable in the existing request detail modal.

**Architecture:** Keep the existing `request_logs` table and existing admin request-log table. Move per-attempt log creation into `_chat_with_retry` so every acquired channel attempt logs once, while endpoint handlers keep logging the final client-facing request summary. Attach each attempt's log `request_id` into `retry_path`, and have the detail UI render those IDs as links to `showRequestLogDetail(id)` once the detail endpoint enriches path items with matching log row IDs.

**Tech Stack:** Python 3, FastAPI, asyncpg/PostgreSQL JSONB, pytest, vanilla JavaScript in `static/admin.html`.

---

## File Structure

- Modify `main.py`: add small helpers for attempt request IDs, attempt log payloads, and attempt finalization; call them inside `_chat_with_retry` success/error paths. Do not add a new table or new relationship model.
- Modify `db.py`: enrich `get_request_log_detail()` retry-path items with matching `log_id` values by looking up `request_id` in `request_logs`; include `parent_request_id` in list rows for visibility/debugging.
- Modify `static/admin.html`: show request ID in the existing request-log table and render retry-path request IDs as clickable controls inside the existing detail modal.
- Modify `tests/test_retry_policy.py`: add unit tests for the new pure helper behavior in `main.py`, avoiding network/database calls.

## Important Existing Context

- `main.py:422` defines `_log_request(data)` and `main.py:429` defines `_schedule_log_request(data)`.
- `main.py:1061` defines `_chat_with_retry(...)`, where actual channel calls happen.
- `main.py:1085-1138` is the successful attempt path; `main.py:1144-1251` are failure/retry paths.
- `main.py:829`, `main.py:1282`, and later Anthropic handlers currently create one endpoint-level `request_id` and write one summary log.
- `db.py:809` inserts request logs.
- `db.py:846` lists request logs.
- `db.py:907` returns request-log details.
- `static/admin.html:2202` renders retry-count tags in the table.
- `static/admin.html:2446` renders retry-path cards.
- `static/admin.html:2651` opens the request detail modal.

## Task 1: Add Pure Helpers for Per-Attempt Channel Logs

**Files:**
- Modify: `main.py:422-524`
- Test: `tests/test_retry_policy.py`

- [ ] **Step 1: Write failing helper tests**

Append these tests to `tests/test_retry_policy.py`:

```python
def test_channel_attempt_request_id_uses_parent_and_retry_index():
    assert main._channel_attempt_request_id("req_parent", 0) == "req_parent_attempt_0"
    assert main._channel_attempt_request_id("req_parent", 2) == "req_parent_attempt_2"


def test_build_channel_attempt_log_uses_attempt_request_id_and_parent_request_id():
    route_info = {
        "provider": "openai-compatible",
        "account": "account-a",
        "requested_model": "public-model",
        "routed_model": "routed-model",
        "retry": 1,
        "router_request_body": {"model": "upstream-model", "messages": []},
        "router_response_body": {"id": "resp_1"},
        "response_headers": {":status": "429"},
        "upstream_status": "429",
        "error": "rate limited",
    }

    log = main._build_channel_attempt_log(
        parent_request_id="req_parent",
        route_info=route_info,
        model="public-model",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
        api_key="sk-test",
        api_key_name="test-key",
        request_headers={"x-test": "1"},
        start_time=100.0,
        duration_ms=234,
        success=False,
        status="error",
        response_body={"error": "rate limited"},
        error="rate limited",
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0, "cache_creation_tokens": 0},
    )

    assert log["request_id"] == "req_parent_attempt_1"
    assert log["parent_request_id"] == "req_parent"
    assert log["provider_name"] == "openai-compatible"
    assert log["account_username"] == "account-a"
    assert log["endpoint"] == "channel://openai-compatible/account-a"
    assert log["retry_path"] == [{**route_info, "request_id": "req_parent_attempt_1"}]
    assert log["router_request_body"] == {"model": "upstream-model", "messages": []}
    assert log["router_response_body"] == {"id": "resp_1"}
    assert log["response_headers"] == {":status": "429"}
    assert log["upstream_status"] == "429"
    assert log["error"] == "rate limited"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_retry_policy.py::test_channel_attempt_request_id_uses_parent_and_retry_index tests/test_retry_policy.py::test_build_channel_attempt_log_uses_attempt_request_id_and_parent_request_id -v
```

Expected: FAIL with `AttributeError: module 'main' has no attribute '_channel_attempt_request_id'`.

- [ ] **Step 3: Add minimal helpers in `main.py`**

Insert this code after `_schedule_log_request(data)` in `main.py`:

```python
def _channel_attempt_request_id(parent_request_id: str, retry_index: int) -> str:
    return f"{parent_request_id}_attempt_{retry_index}"


def _channel_attempt_endpoint(provider_name: str | None, username: str | None) -> str:
    provider = provider_name or "unknown"
    account = username or "unknown"
    return f"channel://{provider}/{account}"


def _build_channel_attempt_log(
    *,
    parent_request_id: str,
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
    retry_index = int(route_info.get("retry") or 0)
    attempt_request_id = _channel_attempt_request_id(parent_request_id, retry_index)
    route_info["request_id"] = attempt_request_id
    provider_name = route_info.get("provider")
    username = route_info.get("account")
    log_data = {
        "request_id": attempt_request_id,
        "parent_request_id": parent_request_id,
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
        "retry_path": [dict(route_info)],
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

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_retry_policy.py::test_channel_attempt_request_id_uses_parent_and_retry_index tests/test_retry_policy.py::test_build_channel_attempt_log_uses_attempt_request_id_and_parent_request_id -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_retry_policy.py
git commit -m "test: cover channel attempt log payloads"
```

## Task 2: Log Every Channel Attempt from `_chat_with_retry`

**Files:**
- Modify: `main.py:1061-1279`
- Test: `tests/test_retry_policy.py`

- [ ] **Step 1: Write failing scheduling tests**

Append these tests to `tests/test_retry_policy.py`:

```python
def test_finalize_channel_attempt_log_schedules_one_attempt_log(monkeypatch):
    scheduled = []
    monkeypatch.setattr(main, "_schedule_log_request", scheduled.append)

    route_info = {
        "provider": "p1",
        "account": "a1",
        "requested_model": "m1",
        "routed_model": "m1",
        "retry": 0,
        "status": "success",
    }

    main._finalize_channel_attempt_log(
        parent_request_id="req_parent",
        route_info=route_info,
        model="m1",
        messages=[{"role": "user", "content": "hi"}],
        stream=False,
        api_key=None,
        api_key_name="",
        request_headers={},
        attempt_start=100.0,
        success=True,
        status="ok",
        response_body={"id": "resp"},
        error="",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cached_tokens": 0, "cache_creation_tokens": 0},
    )

    assert len(scheduled) == 1
    assert scheduled[0]["request_id"] == "req_parent_attempt_0"
    assert scheduled[0]["success"] is True
    assert route_info["request_id"] == "req_parent_attempt_0"


def test_finalize_channel_attempt_log_skips_without_provider(monkeypatch):
    scheduled = []
    monkeypatch.setattr(main, "_schedule_log_request", scheduled.append)

    main._finalize_channel_attempt_log(
        parent_request_id="req_parent",
        route_info={"retry": 0, "status": "error"},
        model="m1",
        messages=[],
        stream=False,
        api_key=None,
        api_key_name="",
        request_headers={},
        attempt_start=100.0,
        success=False,
        status="error",
        response_body={"error": "no provider"},
        error="no provider",
    )

    assert scheduled == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_retry_policy.py::test_finalize_channel_attempt_log_schedules_one_attempt_log tests/test_retry_policy.py::test_finalize_channel_attempt_log_skips_without_provider -v
```

Expected: FAIL with `AttributeError: module 'main' has no attribute '_finalize_channel_attempt_log'`.

- [ ] **Step 3: Add `_finalize_channel_attempt_log` helper**

Insert this code after `_build_channel_attempt_log(...)` in `main.py`:

```python
def _finalize_channel_attempt_log(
    *,
    parent_request_id: str | None,
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
    if not parent_request_id or not route_info.get("provider"):
        return
    log_data = _build_channel_attempt_log(
        parent_request_id=parent_request_id,
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
    _schedule_log_request(log_data)
```

- [ ] **Step 4: Pass request headers into `_chat_with_retry` callers**

In `main.py`, add `kwargs["request_headers"] = request_headers` immediately after `kwargs["request_id"] = request_id` in `chat_completions()`:

```python
    request_id = f"req_{uuid.uuid4().hex[:12]}_{int(time.time()*1000)}"
    kwargs["request_id"] = request_id
    kwargs["request_headers"] = request_headers
    start_time = time.time()
```

In `responses_api()`, add `full_kwargs["request_headers"] = request_headers` immediately after `full_kwargs["request_id"] = request_id`:

```python
    request_id = f"req_{uuid.uuid4().hex[:12]}_{int(time.time()*1000)}"
    full_kwargs["request_id"] = request_id
    full_kwargs["request_headers"] = request_headers
    start_time = time.time()
```

For the Anthropic messages handler, find its `request_id = ...` and kwargs passed into `_chat_with_retry(..., chat_method="chat_anthropic", ...)`; add the same `request_headers` entry to that kwargs dict before `_chat_with_retry` is called:

```python
    kwargs["request_id"] = request_id
    kwargs["request_headers"] = request_headers
```

- [ ] **Step 5: Call `_finalize_channel_attempt_log` inside `_chat_with_retry`**

At the top of `_chat_with_retry`, after `last_error = None`, add:

```python
    parent_request_id = kwargs.get("request_id")
    request_headers = kwargs.pop("request_headers", None)
```

In the success path just before `yield {"_retry_path": retry_path}`, replace the current success append block:

```python
            route_info["status"] = "success"
            retry_path.append(route_info)
            yield {"_retry_path": retry_path}
```

with:

```python
            route_info["status"] = "success"
            retry_path.append(route_info)
            _finalize_channel_attempt_log(
                parent_request_id=parent_request_id,
                route_info=route_info,
                model=model,
                messages=messages,
                stream=stream,
                api_key=api_key,
                api_key_name=api_key_name,
                request_headers=request_headers,
                attempt_start=attempt_start,
                success=True,
                status="ok",
                response_body=route_info.get("router_response_body") or {},
                error="",
            )
            yield {"_retry_path": retry_path}
```

In each failure branch where `route_info` is appended to `retry_path` and `provider_name` is truthy, call `_finalize_channel_attempt_log(...)` immediately after the append and before `continue` or `raise`:

```python
                _finalize_channel_attempt_log(
                    parent_request_id=parent_request_id,
                    route_info=route_info,
                    model=model,
                    messages=messages,
                    stream=stream,
                    api_key=api_key,
                    api_key_name=api_key_name,
                    request_headers=request_headers,
                    attempt_start=attempt_start,
                    success=False,
                    status="error",
                    response_body={"error": error_text},
                    error=error_text,
                )
```

For the `HTTPException` branch, use:

```python
                _finalize_channel_attempt_log(
                    parent_request_id=parent_request_id,
                    route_info=route_info,
                    model=model,
                    messages=messages,
                    stream=stream,
                    api_key=api_key,
                    api_key_name=api_key_name,
                    request_headers=request_headers,
                    attempt_start=attempt_start,
                    success=False,
                    status="error",
                    response_body={"error": e.detail},
                    error=str(e.detail),
                )
```

Do not call `_finalize_channel_attempt_log` in `NoAvailableAccountError` or the `provider_name is None` placeholder branch, because no real channel request happened.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_retry_policy.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_retry_policy.py
git commit -m "feat: log each channel retry attempt"
```

## Task 3: Enrich Request Details with Retry-Path Log Row IDs

**Files:**
- Modify: `db.py:846-920`
- Test: no new DB integration test; verify with existing tests and manual UI/API check in Task 5.

- [ ] **Step 1: Include `parent_request_id` in list rows**

In `db.py:877`, change the SELECT list from:

```sql
SELECT id, request_id, extract(epoch from created_at)::float AS time, api_key_name, provider_name,
```

to:

```sql
SELECT id, request_id, parent_request_id, extract(epoch from created_at)::float AS time, api_key_name, provider_name,
```

- [ ] **Step 2: Enrich retry-path items in details**

In `db.py`, replace `get_request_log_detail()` with:

```python
    @classmethod
    async def get_request_log_detail(cls, log_id: int) -> dict | None:
        async with cls.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT *, extract(epoch from created_at) AS time FROM request_logs WHERE id=$1", log_id)
            if not row:
                return None
            data = dict(row)
            data["retry_path"] = cls._loads_json(data.get("retry_path"), [])
            data["request_body"] = cls._loads_json(data.get("request_body"), {})
            data["router_request_body"] = cls._loads_json(data.get("router_request_body"), {})
            data["router_response_body"] = cls._loads_json(data.get("router_response_body"), {})
            data["response_body"] = cls._loads_json(data.get("response_body"), {})
            data["request_headers"] = cls._loads_json(data.get("request_headers"), {})
            data["response_headers"] = cls._loads_json(data.get("response_headers"), {})

            request_ids = [
                str(item.get("request_id"))
                for item in data["retry_path"]
                if isinstance(item, dict) and item.get("request_id")
            ]
            if request_ids:
                rows = await conn.fetch(
                    "SELECT id, request_id FROM request_logs WHERE request_id = ANY($1::text[])",
                    request_ids,
                )
                id_by_request_id = {row["request_id"]: row["id"] for row in rows}
                for item in data["retry_path"]:
                    if isinstance(item, dict) and item.get("request_id") in id_by_request_id:
                        item["log_id"] = id_by_request_id[item["request_id"]]
            return data
```

- [ ] **Step 3: Run existing tests**

Run:

```bash
pytest tests/test_retry_policy.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add db.py
git commit -m "feat: link retry path entries to request logs"
```

## Task 4: Render Request IDs and Clickable Retry-Path Links in Existing UI

**Files:**
- Modify: `static/admin.html:2202-2248`
- Modify: `static/admin.html:2446-2457`
- Modify: `static/admin.html:2651-2706`
- Test: manual browser check in Task 5.

- [ ] **Step 1: Add request ID column to existing request-log table**

In `static/admin.html`, update `tableRows` inside `renderRequestLogsPage()` so the first cells include request ID after time:

```javascript
  const requestIdCell = (r) => `<div style="font-family:var(--fontM);font-size:11px;max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escAttr(r.request_id || '')}">${esc(r.request_id || '-')}</div>${r.parent_request_id ? `<div style="font-family:var(--fontM);font-size:10px;color:var(--text2);max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escAttr(r.parent_request_id)}">父 ${esc(r.parent_request_id)}</div>` : ''}`;
```

Then change the row template from:

```javascript
    <td style="padding:7px 8px;white-space:nowrap">${new Date(r.time*1000).toLocaleString()}</td>
    <td style="padding:7px 8px">${mkProviderCell(r)}</td>
```

to:

```javascript
    <td style="padding:7px 8px;white-space:nowrap">${new Date(r.time*1000).toLocaleString()}</td>
    <td style="padding:7px 8px">${requestIdCell(r)}</td>
    <td style="padding:7px 8px">${mkProviderCell(r)}</td>
```

Update the table header from:

```html
<th style="padding:7px 8px">时间</th><th style="padding:7px 8px">渠道/账号</th>
```

to:

```html
<th style="padding:7px 8px">时间</th><th style="padding:7px 8px">请求 ID</th><th style="padding:7px 8px">渠道/账号</th>
```

Update empty-state colspan from `10` to `11`.

- [ ] **Step 2: Render retry-path request IDs as clickable detail links**

Replace `renderRetryPathPanel(retryPath)` in `static/admin.html` with:

```javascript
function renderRetryPathPanel(retryPath, currentLogId=null) {
  if (!Array.isArray(retryPath) || !retryPath.length) return '';
  return retryPath.map((x,i) => {
    const provider = x.provider || x.provider_name || x.channel || '?';
    const account = x.account || x.account_username || x.username || '?';
    const model = x.routed_model || x.model || x.requested_model || '?';
    const requestId = x.request_id || '';
    const logId = x.log_id || null;
    const ok = x.ok !== false && x.status !== 'error' && x.status !== 'empty_non_stream';
    const body = renderLogJson(x);
    const arrow = i > 0 ? '<span style="color:var(--text2);margin-right:6px">→</span>' : '';
    const requestIdHtml = requestId
      ? (logId && logId !== currentLogId
          ? `<button class="btn sm ghost" style="font-family:var(--fontM);font-size:11px;padding:1px 6px" onclick="event.stopPropagation();showRequestLogDetail(${logId})">${esc(requestId)}</button>`
          : `<span class="tag" style="font-family:var(--fontM);font-size:11px">${esc(requestId)}</span>`)
      : '';
    const title = `${arrow}<span class="tag ${ok?'on':'err'}">${esc(ok ? '成功' : '失败')}</span>${requestIdHtml}<span>${esc(provider)} / ${esc(account)} / ${esc(model)}</span>`;
    const meta = x.retry !== undefined ? `<span class="tag">retry ${esc(x.retry)}</span>` : '';
    return renderLogItem(title, meta, body, false, formatLogError(x.error || ''));
  }).join('');
}
```

- [ ] **Step 3: Pass current log ID to retry-path renderer**

In `showRequestLogDetail(id)`, change:

```javascript
  const retryPathHtml = renderRetryPathPanel(retryPathList);
```

to:

```javascript
  const retryPathHtml = renderRetryPathPanel(retryPathList, id);
```

- [ ] **Step 4: Add current request ID to detail summary**

Inside the detail modal summary grid, add this row before the channel row:

```javascript
    <div class="kv-row" style="min-width:0"><span class="key">请求 ID</span><span class="val" style="font-family:var(--fontM);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0" title="${escAttr(r.request_id||'-')}">${esc(r.request_id||'-')}</span></div>
```

If `r.parent_request_id` is present, add this row immediately after it:

```javascript
    ${r.parent_request_id ? `<div class="kv-row" style="min-width:0"><span class="key">父请求 ID</span><span class="val" style="font-family:var(--fontM);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0" title="${escAttr(r.parent_request_id)}">${esc(r.parent_request_id)}</span></div>` : ''}
```

- [ ] **Step 5: Run syntax smoke checks**

Run:

```bash
python -m py_compile main.py db.py admin.py
pytest tests/test_retry_policy.py -v
```

Expected: `py_compile` exits 0 and tests PASS.

- [ ] **Step 6: Commit**

```bash
git add static/admin.html
git commit -m "feat: link retry path request ids"
```

## Task 5: Manual Verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run automated checks**

Run:

```bash
pytest tests/test_retry_policy.py tests/test_context_budget.py -v
```

Expected: PASS.

- [ ] **Step 2: Start the app**

Run the project using its existing command. If the repository already has a known run command, use that. Otherwise run:

```bash
python main.py
```

Expected: the FastAPI app starts without import or startup errors.

- [ ] **Step 3: Create a successful channel request**

Using the configured local endpoint and an available model/API key, send one non-stream chat request through `/v1/chat/completions`.

Expected: the request succeeds and the admin request-log table shows a row whose endpoint is the normal client endpoint plus at least one row whose endpoint starts with `channel://`.

- [ ] **Step 4: Create a retrying channel request**

Temporarily use an existing disabled/bad channel account or a low retry-test route that produces one failed channel attempt followed by another attempt. Do not delete accounts or permanently change provider configuration.

Expected: the admin request-log table shows one row per real channel attempt. The failed attempt and later attempt appear as separate rows in timestamp order.

- [ ] **Step 5: Verify detail modal request relationships**

Open the final client-facing request detail and expand `重试路径`.

Expected: each retry-path item displays request ID, channel account, and error summary for failed items. Clicking a request ID opens the corresponding channel attempt detail in the same modal flow.

- [ ] **Step 6: Commit verification-only adjustments if any**

If Task 5 required no code changes, do not commit. If small fixes were needed, commit only the touched files:

```bash
git add main.py db.py static/admin.html tests/test_retry_policy.py
git commit -m "fix: verify channel request logging"
```

## Self-Review

- Spec coverage: every real channel request is logged by Task 2; existing table display is preserved and enhanced by Task 4; request ID/path relationship is preserved through `retry_path.request_id` and `log_id` enrichment in Task 3; test-channel requests already write logs in `admin.py:1736-1902` and remain in the same table.
- Placeholder scan: no TBD/TODO/fill-in placeholders remain. Manual verification steps require project-specific configured credentials because no safe static credentials exist in the repo.
- Type consistency: helper names are `_channel_attempt_request_id`, `_build_channel_attempt_log`, and `_finalize_channel_attempt_log`; retry-path fields are `request_id` and `log_id`; UI calls `showRequestLogDetail(logId)`.
