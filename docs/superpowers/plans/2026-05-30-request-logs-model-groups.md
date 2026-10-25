# Request Logs and Model Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make request logs use a single detail-only request ID, fix grouped model and cached-token display, make model group names immutable with copy support, and close time range popovers on outside clicks.

**Architecture:** Keep the existing single-file admin UI pattern in `static/admin.html` and existing FastAPI/Postgres service boundaries. Backend changes stay focused on request log row shape and model group immutability; frontend changes add display helpers and modal interactions without introducing new frameworks.

**Tech Stack:** Python FastAPI, asyncpg/Postgres access in `db.py`, static vanilla JavaScript admin UI in `static/admin.html`, pytest text/behavior tests.

---

## File Structure

- Modify `db.py`: request-log query fields; model-group upsert no longer deletes old names when payload name differs from path name.
- Modify `admin.py`: model-group update endpoint ignores payload rename attempts; first-token capture uses meaningful chunks only.
- Modify `static/admin.html`: request-log table/detail display, cached-token wording, model-group immutable/copy UI, API-key scope layout, time-range outside click handling.
- Modify `tests/test_admin_model_routing_ui.py`: assertions for immutable group names, copy modal, duplicate validation, API-key layout.
- Modify `tests/test_admin_time_range_picker.py`: assertions for outside-click handling.
- Create/modify `tests/test_admin_request_logs_ui.py`: request-log table/detail and token wording assertions.
- Modify/create `tests/test_model_routing_db.py`: DB-level group rename prevention if existing tests support async Postgres client stubbing.

### Task 1: Request log UI table/detail display

**Files:**
- Modify: `static/admin.html`
- Test: `tests/test_admin_request_logs_ui.py`

- [ ] **Step 1: Write failing UI tests**

Add `tests/test_admin_request_logs_ui.py` if missing, using the same `function_body` helper style as existing admin UI tests:

```python
from pathlib import Path


ADMIN_HTML = Path(__file__).resolve().parents[1] / "static" / "admin.html"


def admin_html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    assert marker in source, f"Function {name} declaration not found"
    start = source.index(marker)
    params_start = source.index("(", start)
    params_depth = 0
    for index in range(params_start, len(source)):
        char = source[index]
        if char == "(":
            params_depth += 1
        elif char == ")":
            params_depth -= 1
            if params_depth == 0:
                brace = source.index("{", index)
                break
    else:
        raise AssertionError(f"Function {name} parameters not closed")
    depth = 0
    quote = None
    escaped = False
    template_expr_depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif quote == "`" and source[index + 1:index + 2] == "{":
                template_expr_depth += 1
            elif quote == "`" and char == "}" and template_expr_depth:
                template_expr_depth -= 1
            elif char == quote and not template_expr_depth:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1:index]
    raise AssertionError(f"Function {name} body not found")


def test_request_log_table_does_not_render_request_id_column():
    html = admin_html()
    body = function_body(html, "renderRequestLogsPage")

    assert "requestIdCell" not in body
    assert "父 " not in body
    assert "<th" in body
    assert "请求ID" not in body
    assert "父请求" not in body


def test_request_log_detail_keeps_request_id_as_detail_only_metadata():
    html = admin_html()
    body = function_body(html, "showRequestLogDetail")

    assert "请求ID" in body
    assert "parent_request_id" not in body
    assert "父请求" not in body


def test_request_log_model_cell_displays_group_and_actual_model():
    html = admin_html()
    body = function_body(html, "renderRequestLogsPage")

    assert "formatRequestLogModel" in body
    helper = function_body(html, "formatRequestLogModel")
    assert "actual_model" in helper
    assert "requested_model" in helper
    assert "${requested}(${actual})" in helper or "requested + '(' + actual + ')'" in helper


def test_request_log_cached_tokens_label_says_cache_read():
    html = admin_html()
    body = function_body(html, "renderRequestLogsPage")

    assert "缓存读" in body
    assert "读 '+r.cached_tokens" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin_request_logs_ui.py -v`

Expected: FAIL because request ID is still rendered in the table, parent request ID text exists, no `formatRequestLogModel` helper exists, and cached tokens still say `读`.

- [ ] **Step 3: Implement minimal UI changes**

In `static/admin.html`, inside `renderRequestLogsPage`, replace `mkModelCell` with:

```javascript
  const mkModelCell = (r) => `<span style="color:var(--blue)">${esc(formatRequestLogModel(r))}</span>`;
```

Add this top-level helper near the request-log functions:

```javascript
function formatRequestLogModel(r) {
  const requested = r.requested_model || r.model || '-';
  const actual = r.actual_model || r.upstream_model || '';
  if (actual && requested && actual !== requested) return `${requested}(${actual})`;
  return requested;
}
```

In `mkTokenCell`, change the cached-token tag text to:

```javascript
      ${(r.cached_tokens||0)>0?'<span class="tag" style="font-size:10px;padding:1px 5px;background:rgba(105,113,128,.15);color:var(--text2)">缓存读 '+r.cached_tokens+'</span>':''}
```

Remove the `requestIdCell` helper and remove the request-ID `<td>` from `tableRows`:

```javascript
  const tableRows = rows.map(r => `<tr style="font-size:12px;border-bottom:1px solid var(--border)">
    <td style="padding:7px 8px;white-space:nowrap">${new Date(r.time*1000).toLocaleString()}</td>
    <td style="padding:7px 8px">${mkProviderCell(r)}</td>
    <td style="padding:7px 8px">${esc(r.api_key_name||'-')}</td>
    <td style="padding:7px 8px;font-family:var(--fontM)">${mkModelCell(r)}</td>
    <td style="padding:7px 8px;text-align:center">${mkTimeCell(r)}</td>
    <td style="padding:7px 8px;text-align:center">${mkTokenCell(r)}</td>
    <td style="padding:7px 8px">${mkRetryTags(r)}</td>
    <td style="padding:7px 8px">${mkStatusCell(r)}</td>
    <td style="padding:7px 8px">${mkErrorCell(r)}</td>
    <td style="padding:7px 8px"><button class="btn sm" onclick="showRequestLogDetail(${r.id})">详情</button></td>
  </tr>`).join('');
```

Update the table header in the same function to remove the request-ID column and any parent-ID text. Keep the detail button column.

In `showRequestLogDetail`, keep only the current `request_id` row and remove any `parent_request_id` display.

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_admin_request_logs_ui.py -v`

Expected: PASS.

### Task 2: Backend request-log row shape and first-token collection

**Files:**
- Modify: `db.py`
- Modify: `admin.py`
- Test: `tests/test_admin_request_logs_ui.py` or existing backend request-log test file if present

- [ ] **Step 1: Write failing tests**

Append to `tests/test_admin_request_logs_ui.py`:

```python
def test_request_log_query_selects_requested_and_actual_model_fields():
    db_py = (Path(__file__).resolve().parents[1] / "db.py").read_text(encoding="utf-8")

    assert "AS requested_model" in db_py
    assert "AS actual_model" in db_py


def test_first_token_time_ignores_empty_stream_chunks():
    admin_py = (Path(__file__).resolve().parents[1] / "admin.py").read_text(encoding="utf-8")

    assert "def _has_first_token_content" in admin_py
    assert "if first_token_ms is None and _has_first_token_content(chunk):" in admin_py
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin_request_logs_ui.py::test_request_log_query_selects_requested_and_actual_model_fields tests/test_admin_request_logs_ui.py::test_first_token_time_ignores_empty_stream_chunks -v`

Expected: FAIL because query aliases and helper do not exist yet.

- [ ] **Step 3: Implement backend row fields**

In `db.py` request-log query SELECT, change the selected model columns to expose current DB model as both the old `model` field and explicit requested/actual fields until a dedicated actual-model column exists:

```sql
                       account_username, model, model AS requested_model, model AS actual_model, endpoint, success, status, stream, duration_ms, first_token_ms,
```

This keeps frontend compatibility and gives the UI stable field names. If the existing schema already has a routed-model column in this checkout, use that column for `actual_model` instead of `model AS actual_model`.

- [ ] **Step 4: Implement meaningful first-token helper**

In `admin.py`, add near other local helpers or before the account test endpoint:

```python
def _has_first_token_content(chunk) -> bool:
    if chunk is None:
        return False
    if isinstance(chunk, str):
        for line in chunk.split("\n"):
            if not line.startswith("data:"):
                continue
            line_data = line[5:].strip()
            if not line_data or line_data == "[DONE]":
                continue
            try:
                event = json.loads(line_data)
            except json.JSONDecodeError:
                return True
            for choice in event.get("choices", []) or []:
                delta = choice.get("delta") or {}
                message = choice.get("message") or {}
                if delta.get("content") or message.get("content"):
                    return True
        return bool(chunk.strip()) and not chunk.strip().startswith("data: [DONE]")
    if isinstance(chunk, dict):
        for choice in chunk.get("choices", []) or []:
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            if delta.get("content") or message.get("content"):
                return True
        return bool(chunk.get("content") or chunk.get("text"))
    return bool(chunk)
```

In the account test stream loop, replace:

```python
                    if first_token_ms is None and chunk:
                        first_token_ms = int((time.time() - start_time) * 1000)
```

with:

```python
                    if first_token_ms is None and _has_first_token_content(chunk):
                        first_token_ms = max(1, int((time.time() - start_time) * 1000))
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_admin_request_logs_ui.py -v`

Expected: PASS.

### Task 3: Model group immutable name and copy modal UI

**Files:**
- Modify: `static/admin.html`
- Test: `tests/test_admin_model_routing_ui.py`

- [ ] **Step 1: Write failing UI tests**

Append to `tests/test_admin_model_routing_ui.py`:

```python
def test_model_group_detail_makes_existing_name_readonly_and_keeps_new_name_editable():
    html = admin_html()
    body = function_body(html, "openGroupDetail")

    assert "readonly" in body
    assert "isNew ? '' : 'readonly'" in body or "isNew ? '' : 'readonly disabled-name'" in body
    assert "if (!isNew && nextName !== name)" not in body
    assert "delete window._modelGroups[name]" not in body


def test_model_group_cards_and_detail_offer_copy_flow():
    html = admin_html()
    card_body = function_body(html, "groupCardHtml")

    assert "copyModelGroup" in html
    assert "openCopyModelGroup" in html
    assert "复制" in card_body
    assert "复制模型组" in html


def test_model_group_copy_modal_validates_duplicate_name_before_submit():
    html = admin_html()
    body = function_body(html, "validateCopyModelGroupName")

    assert "window._modelGroups" in body
    assert "模型组名称已存在" in body
    assert "copyModelGroupConfirm" in html
    assert "disabled" in body


def test_api_key_scope_uses_grouped_action_layout():
    html = admin_html()
    body = function_body(html, "apiKeyScopeHtml")

    assert "api-key-scope" in body
    assert "api-key-scope-actions" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin_model_routing_ui.py -v`

Expected: FAIL for missing readonly name, copy flow, duplicate validation, and API key scope layout.

- [ ] **Step 3: Update API-key scope layout**

Replace `apiKeyScopeHtml` in `static/admin.html` with:

```javascript
function apiKeyScopeHtml(item) {
  const keys = ((window._modelRoutingMeta || {}).api_keys || []);
  const selectedIds = new Set((item.api_key_ids || []).map(String));
  return `<div class="api-key-scope form-group"><div class="api-key-scope-actions"><label><input type="checkbox" id="scopeAll" ${item.use_all_api_keys!==false?'checked':''} onchange="document.getElementById('scopeKeys').disabled=this.checked"> 全部 API Key 可使用</label></div><label>可使用 API Key</label><select id="scopeKeys" multiple size="6" ${item.use_all_api_keys!==false?'disabled':''}>${keys.map(k => `<option value="${escAttr(k.id)}" ${selectedIds.has(String(k.id))?'selected':''}>${esc(k.label || k.name || k.id)}</option>`).join('')}</select><div class="form-hint">来自数据库 API Key；名称为空时显示脱敏 Key。</div></div>`;
}
```

- [ ] **Step 4: Make existing model group name read-only**

In `openGroupDetail`, change the group name input HTML to:

```javascript
<input id="gdName" value="${escAttr(isNew ? '' : name)}" ${isNew ? '' : 'readonly'}>
```

Remove this rename block from the save callback:

```javascript
    if (!isNew && nextName !== name) delete window._modelGroups[name];
```

Ensure existing groups save back under their original name:

```javascript
    const targetName = isNew ? nextName : name;
    window._pendingModelGroup = nextGroup;
    window._modelGroups[targetName] = {...nextGroup, name: targetName};
    await saveModelGroupDetail(isNew ? null : name);
```

- [ ] **Step 5: Add copy buttons and modal functions**

In `groupCardHtml`, add a copy button next to details:

```javascript
<button class="btn sm" onclick="openCopyModelGroup('${escAttr(name)}')">复制</button>
```

Also include a copy action in existing group detail modal by adding a small button near the title area or modal body:

```javascript
${isNew ? '' : `<div style="display:flex;justify-content:flex-end;margin-bottom:10px"><button class="btn sm" onclick="openCopyModelGroup('${escAttr(name)}')">复制模型组</button></div>`}
```

Add these top-level functions near model group functions:

```javascript
function nextCopyGroupName(sourceName) {
  const groups = window._modelGroups || {};
  let candidate = `${sourceName}-copy`;
  let index = 2;
  while (groups[candidate]) {
    candidate = `${sourceName}-copy-${index}`;
    index += 1;
  }
  return candidate;
}

function validateCopyModelGroupName() {
  const input = document.getElementById('copyGroupName');
  const error = document.getElementById('copyGroupNameError');
  const confirm = document.getElementById('copyModelGroupConfirm');
  const name = (input?.value || '').trim();
  let message = '';
  if (!name) message = '模型组名称不能为空';
  else if ((window._modelGroups || {})[name]) message = '模型组名称已存在';
  if (error) error.textContent = message;
  if (confirm) confirm.disabled = !!message;
  return !message;
}

function openCopyModelGroup(sourceName) {
  const source = (window._modelGroups || {})[sourceName];
  if (!source) { toast('模型组不存在', 'error'); return; }
  const candidate = nextCopyGroupName(sourceName);
  openModal('复制模型组', `<div class="form-group"><label>来源组</label><input value="${escAttr(sourceName)}" readonly></div><div class="form-group"><label>新组名</label><input id="copyGroupName" value="${escAttr(candidate)}" oninput="validateCopyModelGroupName()"><div class="form-hint">将复制组内模型、渠道过滤、缓存和 API Key 范围。</div><div class="time-range-error" id="copyGroupNameError"></div></div>`, async () => {
    if (!validateCopyModelGroupName()) throw new Error(document.getElementById('copyGroupNameError')?.textContent || '模型组名称无效');
    await copyModelGroup(sourceName, document.getElementById('copyGroupName').value.trim());
  });
  setTimeout(() => {
    const confirm = document.querySelector('.modal-footer .btn.primary');
    if (confirm) confirm.id = 'copyModelGroupConfirm';
    validateCopyModelGroupName();
  }, 0);
}

async function copyModelGroup(sourceName, nextName) {
  const source = (window._modelGroups || {})[sourceName];
  if (!source) throw new Error('模型组不存在');
  const copy = JSON.parse(JSON.stringify(source));
  copy.name = nextName;
  await api('/admin/model-groups', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(copy)});
  toast('模型组已复制', 'success');
  await renderModelRoutingPage();
}
```

- [ ] **Step 6: Run tests to verify pass**

Run: `pytest tests/test_admin_model_routing_ui.py -v`

Expected: PASS.

### Task 4: Backend model group rename prevention

**Files:**
- Modify: `admin.py`
- Modify: `db.py`
- Test: `tests/test_model_routing_db.py`

- [ ] **Step 1: Write failing tests**

Append or adjust `tests/test_model_routing_db.py` with text-level assertions if the file does not use a live database fixture:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_model_group_upsert_does_not_delete_old_name_for_payload_rename():
    db_py = (ROOT / "db.py").read_text(encoding="utf-8")

    assert "DELETE FROM model_groups WHERE name=$1" not in db_py


def test_update_model_group_ignores_payload_name_for_existing_group():
    admin_py = (ROOT / "admin.py").read_text(encoding="utf-8")

    assert 'payload["name"] = name' in admin_py
    assert "new_name != name" not in admin_py
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_model_routing_db.py -v`

Expected: FAIL because `db.py` still deletes old group names and `admin.py` still allows/handles `new_name != name`.

- [ ] **Step 3: Update DB upsert behavior**

In `db.py` `upsert_model_group`, remove this block:

```python
            if normalized["name"] != name:
                await conn.execute("DELETE FROM model_groups WHERE name=$1", name)
```

Ensure the first SQL parameter remains `normalized["name"]` for create calls. Existing update calls will pass payload with `name` forced to the path name by `admin.py`.

- [ ] **Step 4: Update admin endpoint behavior**

In `admin.py` `update_model_group`, remove the branch that permits a `new_name`. Force the payload name to the path parameter before validation and save:

```python
    payload = dict(data or {})
    payload["name"] = name
    try:
        await _validate_model_group_write(name, payload)
        group = await PostgresClient.upsert_model_group(name, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

Keep the 404 check for missing `old_group`, logging, cache refresh, and response structure unchanged.

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest tests/test_model_routing_db.py -v`

Expected: PASS.

### Task 5: Time range outside-click close

**Files:**
- Modify: `static/admin.html`
- Test: `tests/test_admin_time_range_picker.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_admin_time_range_picker.py`:

```python
def test_time_range_registers_outside_click_handler_and_ignores_picker_widgets():
    html = admin_html()

    assert "function bindTimeRangeOutsideClick(" in html
    body = function_body(html, "bindTimeRangeOutsideClick")
    assert "document.addEventListener('click'" in body
    assert ".time-range-filter" in body
    assert ".flatpickr-calendar" in body
    assert "closeAllTimeRangePanels" in body


def test_time_range_open_closes_other_panels_first():
    html = admin_html()
    body = function_body(html, "openTimeRangePanel")

    assert "closeAllTimeRangePanels(prefix)" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_admin_time_range_picker.py::test_time_range_registers_outside_click_handler_and_ignores_picker_widgets tests/test_admin_time_range_picker.py::test_time_range_open_closes_other_panels_first -v`

Expected: FAIL because no outside-click handler exists.

- [ ] **Step 3: Implement outside-click helpers**

Add near time range helpers in `static/admin.html`:

```javascript
function closeAllTimeRangePanels(exceptPrefix='') {
  document.querySelectorAll('.time-range-filter').forEach(filter => {
    const prefix = filter.dataset.timePrefix || '';
    if (prefix && prefix !== exceptPrefix) hideTimeRangePanel(prefix);
  });
}

function bindTimeRangeOutsideClick() {
  if (window._timeRangeOutsideClickBound) return;
  window._timeRangeOutsideClickBound = true;
  document.addEventListener('click', (event) => {
    const target = event.target;
    if (target?.closest?.('.time-range-filter')) return;
    if (target?.closest?.('.flatpickr-calendar')) return;
    closeAllTimeRangePanels('');
  });
}
```

At the start of `openTimeRangePanel(prefix)`, add:

```javascript
  closeAllTimeRangePanels(prefix);
```

At the start or end of `initTimeRangeFilter(prefix, range)`, add:

```javascript
  bindTimeRangeOutsideClick();
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest tests/test_admin_time_range_picker.py -v`

Expected: PASS.

### Task 6: Full verification

**Files:**
- No code changes unless tests reveal issues.

- [ ] **Step 1: Run focused test suite**

Run: `pytest tests/test_admin_request_logs_ui.py tests/test_admin_model_routing_ui.py tests/test_admin_time_range_picker.py tests/test_model_routing_db.py -v`

Expected: PASS.

- [ ] **Step 2: Inspect working tree**

Run: `git status --short`

Expected: Only intended files changed. Do not stage unrelated pre-existing changes.

- [ ] **Step 3: Manual UI verification if practical**

Start the app using the project’s normal command, open the admin UI, and verify:

- Request log table has no request-ID column.
- Request log detail shows request ID.
- Cached-token label says `缓存读`.
- Model group detail has read-only name for existing groups.
- Copy model group modal blocks duplicate names and creates a new group.
- Time range panel closes after clicking elsewhere.

- [ ] **Step 4: Commit only this change set if requested**

If the user asks for a commit, stage only files changed for this task:

```bash
git add admin.py db.py static/admin.html tests/test_admin_request_logs_ui.py tests/test_admin_model_routing_ui.py tests/test_admin_time_range_picker.py tests/test_model_routing_db.py docs/superpowers/specs/2026-05-30-request-logs-model-groups-design.md docs/superpowers/plans/2026-05-30-request-logs-model-groups.md
git commit -m "fix: update request logs and model group admin flows"
```
