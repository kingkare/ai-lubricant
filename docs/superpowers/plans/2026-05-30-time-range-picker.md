# Time Range Picker Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shared admin time range filter component for request logs and the data dashboard with compact styling, shortcut ranges, and front-end range validation.

**Architecture:** Keep the implementation in `static/admin.html` because this admin UI is currently a single-file page. Add focused helper functions for rendering the time range controls, initializing paired `flatpickr` instances, applying shortcut ranges, parsing/validating inputs, and showing inline errors. Update the two existing call sites to use the shared helpers while preserving existing backend query parameter names.

**Tech Stack:** Plain HTML/CSS/JavaScript in `static/admin.html`, existing `flatpickr` vendor files, Python pytest static HTML tests.

---

## File Structure

- Modify `static/admin.html`
  - Add CSS for `.time-range-filter`, `.time-range-fields`, `.time-shortcuts`, `.time-range-error`, and compact shortcut buttons near the existing flatpickr styles.
  - Add shared JavaScript helpers near `formatDateTimeLocal()` and `initDateTimePicker()`.
  - Replace duplicated request log and data dashboard time field markup with `timeRangeFilterHtml(...)`.
  - Use `readTimeRangeFilter(...)` before API calls to validate and parse input.
- Create `tests/test_admin_time_range_picker.py`
  - Static tests proving the shared helper exists, both pages use it, shortcut buttons are present, validation is present, and backend parameter names remain unchanged.

---

### Task 1: Add static coverage for the shared time range component

**Files:**
- Create: `tests/test_admin_time_range_picker.py`
- Read-only reference: `static/admin.html`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin_time_range_picker.py` with this content:

```python
from pathlib import Path


ADMIN_HTML = Path(__file__).resolve().parents[1] / "static" / "admin.html"


def admin_html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    assert marker in source, f"Function {name} declaration not found"
    start = source.index(marker)
    brace = source.index("{", start)
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
            elif quote == "`" and char == "$" and source[index + 1:index + 2] == "{":
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


def test_shared_time_range_component_helpers_exist():
    html = admin_html()

    assert "function timeRangeFilterHtml(" in html
    assert "function initTimeRangeFilter(" in html
    assert "function applyTimeRangeShortcut(" in html
    assert "function readTimeRangeFilter(" in html
    assert "function setTimeRangeError(" in html


def test_time_range_shortcuts_and_error_slot_are_rendered():
    html = admin_html()
    body = function_body(html, "timeRangeFilterHtml")

    assert "time-range-filter" in body
    assert "time-shortcuts" in body
    assert "data-range=\"1h\"" in body
    assert "近1小时" in body
    assert "data-range=\"24h\"" in body
    assert "近24小时" in body
    assert "data-range=\"today\"" in body
    assert "今天" in body
    assert "data-range=\"yesterday\"" in body
    assert "昨天" in body
    assert "data-range=\"default\"" in body
    assert "恢复默认" in body
    assert "time-range-error" in body


def test_request_logs_and_dashboard_use_shared_time_range_component():
    html = admin_html()
    request_filter_body = function_body(html, "requestLogFilterHtml")
    dashboard_body = function_body(html, "renderDataDashboardPage")

    assert "timeRangeFilterHtml({prefix:'rl'" in request_filter_body
    assert "timeRangeFilterHtml({prefix:'dd'" in dashboard_body
    assert "initTimeRangeFilter('rl'" in html
    assert "initTimeRangeFilter('dd'" in html


def test_time_range_validation_blocks_invalid_requests():
    html = admin_html()
    read_body = function_body(html, "readTimeRangeFilter")
    request_page_body = function_body(html, "renderRequestLogsPage")
    dashboard_body = function_body(html, "renderDataDashboardPage")

    assert "开始时间不能晚于结束时间" in read_body
    assert "请选择有效时间" in read_body
    assert "return null" in read_body
    assert "const range = readTimeRangeFilter('rl'" in request_page_body
    assert "if (!range) return" in request_page_body
    assert "const range = readTimeRangeFilter('dd'" in dashboard_body
    assert "if (!range) return" in dashboard_body


def test_backend_query_parameter_names_stay_unchanged():
    html = admin_html()
    request_page_body = function_body(html, "renderRequestLogsPage")
    dashboard_body = function_body(html, "renderDataDashboardPage")

    assert "start_time:String(start)" in request_page_body
    assert "end_time:String(end)" in request_page_body
    assert "start:String(start)" in dashboard_body
    assert "end:String(end)" in dashboard_body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_time_range_picker.py -v
```

Expected: FAIL because `timeRangeFilterHtml`, `initTimeRangeFilter`, `applyTimeRangeShortcut`, `readTimeRangeFilter`, and `setTimeRangeError` do not exist yet.

- [ ] **Step 3: Commit the failing tests**

Run:

```bash
git add tests/test_admin_time_range_picker.py
git commit -m "$(cat <<'EOF'
test: cover admin time range picker behavior

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Add shared time range markup and styles

**Files:**
- Modify: `static/admin.html:394-428`
- Modify: `static/admin.html:2665-2684`
- Test: `tests/test_admin_time_range_picker.py`

- [ ] **Step 1: Add component styles**

In `static/admin.html`, near the existing flatpickr CSS block, add these CSS rules after `.date-input{...}`:

```css
.time-range-filter{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;width:100%}
.time-range-fields{display:flex;gap:10px;flex:1;min-width:360px;flex-wrap:wrap}
.time-range-fields .form-group{min-width:170px;flex:1;margin-bottom:0}
.time-shortcuts{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.time-shortcuts .btn{padding:7px 10px;font-size:12px;line-height:1}
.time-range-error{width:100%;min-height:16px;color:var(--red);font-size:12px;line-height:1.35;margin-top:-2px}
.time-range-error:empty{display:none}
@media(max-width:760px){.time-range-fields{min-width:100%}.time-range-filter>.btn.primary{width:100%}}
```

- [ ] **Step 2: Add the shared renderer**

In `static/admin.html`, immediately after `formatDateTimeLocal(ts)`, add this function:

```javascript
function timeRangeFilterHtml({prefix, startLabel='开始时间', endLabel='结束时间', queryHandler}) {
  const startId = `${prefix}Start`;
  const endId = `${prefix}End`;
  const errorId = `${prefix}TimeError`;
  return `<div class="time-range-filter" data-time-prefix="${escAttr(prefix)}">
    <div class="time-range-fields">
      <div class="form-group"><label>${esc(startLabel)}</label><input class="date-input" id="${escAttr(startId)}" placeholder="选择开始时间"></div>
      <div class="form-group"><label>${esc(endLabel)}</label><input class="date-input" id="${escAttr(endId)}" placeholder="选择结束时间"></div>
    </div>
    <div class="time-shortcuts">
      <button type="button" class="btn ghost" data-range="1h" onclick="applyTimeRangeShortcut('${escAttr(prefix)}','1h')">近1小时</button>
      <button type="button" class="btn ghost" data-range="24h" onclick="applyTimeRangeShortcut('${escAttr(prefix)}','24h')">近24小时</button>
      <button type="button" class="btn ghost" data-range="today" onclick="applyTimeRangeShortcut('${escAttr(prefix)}','today')">今天</button>
      <button type="button" class="btn ghost" data-range="yesterday" onclick="applyTimeRangeShortcut('${escAttr(prefix)}','yesterday')">昨天</button>
      <button type="button" class="btn ghost" data-range="default" onclick="applyTimeRangeShortcut('${escAttr(prefix)}','default')">恢复默认</button>
    </div>
    <button type="button" class="btn primary" onclick="${escAttr(queryHandler)}">查询</button>
    <div class="time-range-error" id="${escAttr(errorId)}"></div>
  </div>`;
}
```

- [ ] **Step 3: Run the focused tests**

Run:

```bash
pytest tests/test_admin_time_range_picker.py::test_shared_time_range_component_helpers_exist tests/test_admin_time_range_picker.py::test_time_range_shortcuts_and_error_slot_are_rendered -v
```

Expected: `test_time_range_shortcuts_and_error_slot_are_rendered` PASS; `test_shared_time_range_component_helpers_exist` still FAIL because behavior helpers are added in Task 3.

- [ ] **Step 4: Commit markup and styles**

Run:

```bash
git add static/admin.html
git commit -m "$(cat <<'EOF'
feat: add shared time range filter markup

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add shared initialization, shortcut, and validation behavior

**Files:**
- Modify: `static/admin.html:2670-2684`
- Test: `tests/test_admin_time_range_picker.py`

- [ ] **Step 1: Replace the picker initialization block with paired helpers**

In `static/admin.html`, replace the current `initDateTimePicker(id)` and `initDateTimePickers(...ids)` functions with this block:

```javascript
function initDateTimePicker(id, options={}) {
  const el = document.getElementById(id);
  if (!el || typeof flatpickr === 'undefined') return null;
  if (el._flatpickr) el._flatpickr.destroy();
  return flatpickr(el, {
    enableTime: true,
    time_24hr: true,
    minuteIncrement: 1,
    dateFormat: 'Y-m-d H:i',
    locale: flatpickr.l10ns?.zh || 'zh',
    allowInput: true,
    disableMobile: true,
    ...options,
  });
}
function initDateTimePickers(...ids) { ids.forEach(id => initDateTimePicker(id)); }

function defaultTimeRange() {
  const now = Math.floor(Date.now()/1000);
  return {start: now - 86400, end: now + 7200};
}

function timeRangeInputIds(prefix) {
  return {startId: `${prefix}Start`, endId: `${prefix}End`, errorId: `${prefix}TimeError`};
}

function setTimeRangeError(prefix, message='') {
  const el = document.getElementById(timeRangeInputIds(prefix).errorId);
  if (el) el.textContent = message;
}

function setTimeRangeInputs(prefix, range) {
  const {startId, endId} = timeRangeInputIds(prefix);
  const startEl = document.getElementById(startId);
  const endEl = document.getElementById(endId);
  if (startEl) startEl.value = formatDateTimeLocal(range.start);
  if (endEl) endEl.value = formatDateTimeLocal(range.end);
  setTimeRangeError(prefix, '');
  syncTimeRangeLimits(prefix);
}

function syncTimeRangeLimits(prefix) {
  const {startId, endId} = timeRangeInputIds(prefix);
  const startEl = document.getElementById(startId);
  const endEl = document.getElementById(endId);
  const startDate = startEl?.value ? new Date(startEl.value) : null;
  const endDate = endEl?.value ? new Date(endEl.value) : null;
  if (endEl?._flatpickr) endEl._flatpickr.set('minDate', startDate && !Number.isNaN(startDate.getTime()) ? startDate : null);
  if (startEl?._flatpickr) startEl._flatpickr.set('maxDate', endDate && !Number.isNaN(endDate.getTime()) ? endDate : null);
}

function initTimeRangeFilter(prefix, range) {
  const {startId, endId} = timeRangeInputIds(prefix);
  initDateTimePicker(startId, {onChange: () => { setTimeRangeError(prefix, ''); syncTimeRangeLimits(prefix); }});
  initDateTimePicker(endId, {onChange: () => { setTimeRangeError(prefix, ''); syncTimeRangeLimits(prefix); }});
  if (range) setTimeRangeInputs(prefix, range);
  syncTimeRangeLimits(prefix);
}

function rangeForShortcut(shortcut) {
  const nowDate = new Date();
  const now = Math.floor(nowDate.getTime()/1000);
  if (shortcut === '1h') return {start: now - 3600, end: now};
  if (shortcut === '24h') return {start: now - 86400, end: now};
  if (shortcut === 'today') {
    const start = new Date(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate(), 0, 0, 0);
    return {start: Math.floor(start.getTime()/1000), end: now};
  }
  if (shortcut === 'yesterday') {
    const start = new Date(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate() - 1, 0, 0, 0);
    const end = new Date(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate() - 1, 23, 59, 0);
    return {start: Math.floor(start.getTime()/1000), end: Math.floor(end.getTime()/1000)};
  }
  return defaultTimeRange();
}

function applyTimeRangeShortcut(prefix, shortcut) {
  const range = rangeForShortcut(shortcut);
  setTimeRangeInputs(prefix, range);
  if (prefix === 'rl') window._rlTimeRange = range;
  if (prefix === 'dd') window._ddTimeRange = range;
}

function readTimeRangeFilter(prefix) {
  const {startId, endId} = timeRangeInputIds(prefix);
  const startInput = document.getElementById(startId)?.value;
  const endInput = document.getElementById(endId)?.value;
  const startDate = startInput ? new Date(startInput) : null;
  const endDate = endInput ? new Date(endInput) : null;
  const start = startDate ? Math.floor(startDate.getTime()/1000) : null;
  const end = endDate ? Math.floor(endDate.getTime()/1000) : null;
  if (start === null || end === null || Number.isNaN(start) || Number.isNaN(end)) {
    setTimeRangeError(prefix, '请选择有效时间');
    return null;
  }
  if (start > end) {
    setTimeRangeError(prefix, '开始时间不能晚于结束时间');
    return null;
  }
  setTimeRangeError(prefix, '');
  return {start, end};
}
```

- [ ] **Step 2: Run helper tests**

Run:

```bash
pytest tests/test_admin_time_range_picker.py::test_shared_time_range_component_helpers_exist tests/test_admin_time_range_picker.py::test_time_range_validation_blocks_invalid_requests -v
```

Expected: helper existence assertions PASS; validation test still FAIL until call sites use `readTimeRangeFilter` in Task 4.

- [ ] **Step 3: Commit behavior helpers**

Run:

```bash
git add static/admin.html
git commit -m "$(cat <<'EOF'
feat: add time range picker behavior helpers

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire the shared component into request logs

**Files:**
- Modify: `static/admin.html:2692-2736`
- Test: `tests/test_admin_time_range_picker.py`

- [ ] **Step 1: Replace request log time markup**

In `requestLogFilterHtml(provider='', model='', success='')`, replace the two time `form-group` lines and the standalone query button with the shared component. The function body should render this structure:

```javascript
function requestLogFilterHtml(provider='', model='', success='') {
  const {providers, models} = requestLogFilterOptions();
  return `<div class="card"><div class="card-header"><h3>筛选</h3></div><div class="card-body">
    <div class="toolbar-panel">
      ${timeRangeFilterHtml({prefix:'rl', queryHandler:'renderRequestLogsPage(1)'})}
      <div class="form-row" style="flex:1;margin-bottom:0">
        <div class="form-group"><label>渠道</label>${searchSelectHtml({id:'rlProvider', value:provider, placeholder:'输入或选择渠道', options:providers})}</div>
        <div class="form-group"><label>模型</label>${searchSelectHtml({id:'rlModel', value:model, placeholder:'输入或选择模型', options:models})}</div>
        <div class="form-group"><label>状态</label><select id="rlSuccess"><option value="">全部</option><option value="true" ${success==='true'?'selected':''}>成功</option><option value="false" ${success==='false'?'selected':''}>失败</option></select></div>
      </div>
    </div>
  </div></div>`;
}
```

- [ ] **Step 2: Use defaultTimeRange and initTimeRangeFilter in request log shell**

In `renderRequestLogsShell()`, replace the manual default range, input assignment, and `initDateTimePickers('rlStart','rlEnd')` calls with:

```javascript
function renderRequestLogsShell() {
  window._rlTimeRange = defaultTimeRange();
  document.getElementById('content').innerHTML = requestLogFilterHtml() + '<div class="card"><div class="card-body"><div class="empty">设置筛选条件后点击查询加载请求日志</div></div></div>';
  initTimeRangeFilter('rl', window._rlTimeRange);
  renderRequestLogsPage(1, window._rlPageSize || 20);
}
```

- [ ] **Step 3: Validate and parse request log range before querying**

In `renderRequestLogsPage(page=1, pageSize=20, refreshLatest=false)`, replace the refresh/default/input parsing section through `window._rlTimeRange = {start, end};` with:

```javascript
  if (!window._rlTimeRange || refreshLatest) {
    window._rlTimeRange = defaultTimeRange();
    initTimeRangeFilter('rl', window._rlTimeRange);
  }
  const range = readTimeRangeFilter('rl');
  if (!range) return;
  const {start, end} = range;
  window._rlTimeRange = {start, end};
```

Keep the existing provider/model/success parsing and this query parameter construction unchanged:

```javascript
  const qs = new URLSearchParams({limit:String(pageSize), offset:String(offset), start_time:String(start), end_time:String(end)});
```

- [ ] **Step 4: Keep request log re-render initialization shared**

Near the end of `renderRequestLogsPage`, replace:

```javascript
  initDateTimePickers('rlStart','rlEnd');
```

with:

```javascript
  initTimeRangeFilter('rl', window._rlTimeRange);
```

- [ ] **Step 5: Run request log focused tests**

Run:

```bash
pytest tests/test_admin_time_range_picker.py::test_request_logs_and_dashboard_use_shared_time_range_component tests/test_admin_time_range_picker.py::test_time_range_validation_blocks_invalid_requests tests/test_admin_time_range_picker.py::test_backend_query_parameter_names_stay_unchanged -v
```

Expected: request-log-related assertions PASS; dashboard-related assertions still FAIL until Task 5.

- [ ] **Step 6: Commit request log wiring**

Run:

```bash
git add static/admin.html
git commit -m "$(cat <<'EOF'
feat: use shared time range picker for request logs

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire the shared component into the data dashboard

**Files:**
- Modify: `static/admin.html:3295-3331`
- Test: `tests/test_admin_time_range_picker.py`

- [ ] **Step 1: Use shared range parsing at the start of dashboard rendering**

In `renderDataDashboardPage(forceRefresh = true)`, replace the initial range setup and parsing block:

```javascript
  if (!window._ddTimeRange) {
    const now = Math.floor(Date.now()/1000);
    window._ddTimeRange = {start: now - 24*3600, end: now + 2*3600};
  }
  const startInput = document.getElementById('ddStart')?.value;
  const endInput = document.getElementById('ddEnd')?.value;
  const start = startInput ? Math.floor(new Date(startInput).getTime()/1000) : window._ddTimeRange.start;
  const end = endInput ? Math.floor(new Date(endInput).getTime()/1000) : window._ddTimeRange.end;
  window._ddTimeRange = {start, end};
```

with:

```javascript
  if (!window._ddTimeRange) window._ddTimeRange = defaultTimeRange();
  let range = readTimeRangeFilter('dd');
  if (!range) {
    const hasInputs = document.getElementById('ddStart') && document.getElementById('ddEnd');
    if (hasInputs) return;
    range = window._ddTimeRange;
  }
  const {start, end} = range;
  window._ddTimeRange = {start, end};
```

- [ ] **Step 2: Replace dashboard time markup**

In the dashboard filter HTML inside `renderDataDashboardPage`, replace the two time `form-group` lines and standalone query button with the shared component. The filter block should include:

```javascript
      <div class="toolbar-panel">
        ${timeRangeFilterHtml({prefix:'dd', queryHandler:'renderDataDashboardPage(true)'})}
        <div class="form-row" style="flex:1;margin-bottom:0">
          <div class="form-group"><label>时间颗粒</label><select id="ddGrain"><option value="hour">小时</option><option value="day">天</option><option value="week">周</option></select></div>
          <div class="form-group"><label>渠道</label>${searchSelectHtml({id:'ddProvider', value:provider, placeholder:'全部渠道', options:providerValues, clearable:true})}</div>
          <div class="form-group"><label>模型</label>${searchSelectHtml({id:'ddModel', value:model, placeholder:'全部模型', options:modelValues, clearable:true})}</div>
        </div>
      </div>
```

- [ ] **Step 3: Initialize the dashboard range component after rendering**

Replace this code after the filter HTML is written:

```javascript
  document.getElementById('ddStart').value = formatDateTimeLocal(start);
  document.getElementById('ddEnd').value = formatDateTimeLocal(end);
  initDateTimePickers('ddStart','ddEnd');
```

with:

```javascript
  initTimeRangeFilter('dd', window._ddTimeRange);
```

Keep the existing grain assignment and query parameter construction unchanged:

```javascript
  document.getElementById('ddGrain').value = grain;
  const qs = new URLSearchParams({start:String(start), end:String(end), grain, section:'all'});
```

- [ ] **Step 4: Run all static time range tests**

Run:

```bash
pytest tests/test_admin_time_range_picker.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit dashboard wiring**

Run:

```bash
git add static/admin.html
git commit -m "$(cat <<'EOF'
feat: use shared time range picker for dashboard

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Run regression tests and manually verify the UI

**Files:**
- Verify: `static/admin.html`
- Verify: `tests/test_admin_time_range_picker.py`

- [ ] **Step 1: Run focused test suite**

Run:

```bash
pytest tests/test_admin_time_range_picker.py tests/test_admin_model_routing_ui.py -v
```

Expected: all tests PASS.

- [ ] **Step 2: Run broader related tests**

Run:

```bash
pytest tests/test_admin_model_routing_ui.py tests/test_model_routing_db.py tests/test_model_group_metadata.py -v
```

Expected: all tests PASS.

- [ ] **Step 3: Start the app for manual UI verification**

Run the project using the normal local command for this repository. If no app command is documented in the current session, inspect existing scripts or ask the user for the preferred launch command before proceeding.

Expected: the admin UI is reachable locally.

- [ ] **Step 4: Verify request logs page behavior in the browser**

In the admin UI, open the request logs page and verify:

- Default start and end show near 24 hours ago through about 2 hours in the future.
- “近1小时” sets the range to approximately now minus 1 hour through now.
- “近24小时” sets the range to approximately now minus 24 hours through now.
- “今天” sets the start to today at 00:00 and the end to now.
- “昨天” sets the start to yesterday at 00:00 and the end to yesterday at 23:59.
- “恢复默认” restores near 24 hours ago through about 2 hours in the future.
- Setting start later than end shows “开始时间不能晚于结束时间” and does not reload results.
- A valid query still loads rows and preserves `start_time` and `end_time` parameters.

- [ ] **Step 5: Verify data dashboard behavior in the browser**

In the admin UI, open the data dashboard page and verify:

- Default start and end show near 24 hours ago through about 2 hours in the future.
- Shortcut buttons update the inputs as described in Step 4.
- Setting start later than end shows “开始时间不能晚于结束时间” and does not reload the dashboard.
- A valid query still loads charts and preserves `start` and `end` parameters.
- Switching time grain still works after changing the time range.

- [ ] **Step 6: Commit any verification fixes**

If manual verification required fixes, commit only those files:

```bash
git add static/admin.html tests/test_admin_time_range_picker.py
git commit -m "$(cat <<'EOF'
fix: polish time range picker verification issues

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

If no fixes were needed, do not create an empty commit.

---

## Self-Review

- Spec coverage: the plan covers shared component rendering, compact visual styling, shortcut ranges, start/end validation, flatpickr pairing, request log query parameters, dashboard query parameters, and browser verification for both pages.
- Placeholder scan: no TBD/TODO/fill-in placeholders remain. The only conditional instruction is the app launch command because this repository's launch command must be verified from current project state before running.
- Type consistency: helper names and prefixes are consistent across tasks: `timeRangeFilterHtml`, `initTimeRangeFilter`, `applyTimeRangeShortcut`, `readTimeRangeFilter`, `setTimeRangeError`, `rl`, and `dd`.
