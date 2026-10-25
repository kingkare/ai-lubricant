# 渠道管理与模型线路优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add duplicate token detection, remove channel-level retry behavior, add channel sorting/model filtering, and simplify model route detail editing to channel-level switches.

**Architecture:** Keep the current FastAPI admin API, vanilla JavaScript admin page, and existing model route `entries` storage. Add small pure helpers for backend behavior and front-end rendering decisions, then wire them into the existing large `admin.py`, `config.py`, and `static/admin.html` files without restructuring the app.

**Tech Stack:** Python 3, FastAPI, pytest, vanilla JavaScript in `static/admin.html`, existing `config.Config` and admin endpoints.

---

## File Structure

- Modify `admin.py`: add pure duplicate-token helpers, add duplicate-token endpoint, remove provider retry writes from create/update paths, and return stable channel update timestamps in provider summaries.
- Modify `config.py`: change `Config.get_provider_retry_count()` so provider `retry_count` no longer overrides global retry config.
- Modify `static/admin.html`: add channel sort/model filter controls, duplicate token modal button, remove retry-count UI fields, and replace model route entry editor with channel-level switches.
- Modify `tests/test_retry_policy.py`: add retry-count behavior tests.
- Create `tests/test_admin_channel_optimization.py`: backend helper tests and static admin HTML tests for the new UI contract.
- Optional hygiene: add `.superpowers/` to `.gitignore` so visual brainstorming artifacts are not accidentally committed.

## Important Existing Context

- `admin.py:1217` defines `GET /admin/providers` and uses `_provider_summary_response()`.
- `admin.py:1259` defines `GET /admin/providers/{name}/accounts`.
- `admin.py:1418` creates custom providers and currently writes `retry_count`.
- `admin.py:1488` defines the channel-level retry-count endpoint that should be removed or left unused with tests confirming UI no longer calls it.
- `admin.py:1632` updates custom providers and currently accepts `retry_count`.
- `config.py:262` currently lets provider `retry_count` override global retry config.
- `static/admin.html:900` renders the channels page.
- `static/admin.html:1546` renders the account-list header.
- `static/admin.html:1964` opens the model route detail modal.
- `static/admin.html:2069` renders custom channel retry-count UI.

## Task 1: Add Backend Duplicate Token Helpers

**Files:**
- Create: `tests/test_admin_channel_optimization.py`
- Modify: `admin.py:117-170`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_admin_channel_optimization.py` with this content:

```python
from pathlib import Path

import admin


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


def test_mask_secret_preserves_edges_without_exposing_full_token():
    assert admin._mask_secret("sk-1234567890abcdef") == "sk-1...cdef"
    assert admin._mask_secret("short") == "*****"


def test_find_duplicate_account_tokens_groups_only_non_empty_duplicate_tokens():
    accounts = [
        {"username": "a", "token": "sk-same-token", "switch": True},
        {"username": "b", "token": "sk-same-token", "switch": False},
        {"username": "c", "token": "sk-unique-token", "switch": True},
        {"username": "d", "token": "", "switch": True},
        {"username": "e", "switch": True},
    ]

    duplicates = admin._find_duplicate_account_tokens(accounts)

    assert duplicates == [
        {
            "token_masked": "sk-s...oken",
            "count": 2,
            "accounts": [
                {"username": "a", "enabled": True},
                {"username": "b", "enabled": False},
            ],
        }
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_mask_secret_preserves_edges_without_exposing_full_token tests/test_admin_channel_optimization.py::test_find_duplicate_account_tokens_groups_only_non_empty_duplicate_tokens -v
```

Expected: FAIL with `AttributeError: module 'admin' has no attribute '_mask_secret'`.

- [ ] **Step 3: Add minimal duplicate-token helpers**

In `admin.py`, insert after `_builtin_provider_default_config()`:

```python
def _mask_secret(value: str) -> str:
    text = str(value or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _find_duplicate_account_tokens(accounts: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for account in accounts or []:
        token = (account.get("token") or "").strip()
        if not token:
            continue
        grouped.setdefault(token, []).append(account)

    duplicates = []
    for token, token_accounts in grouped.items():
        if len(token_accounts) < 2:
            continue
        duplicates.append({
            "token_masked": _mask_secret(token),
            "count": len(token_accounts),
            "accounts": [
                {
                    "username": account.get("username") or "",
                    "enabled": account.get("switch") is not False,
                }
                for account in token_accounts
            ],
        })
    return duplicates
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_mask_secret_preserves_edges_without_exposing_full_token tests/test_admin_channel_optimization.py::test_find_duplicate_account_tokens_groups_only_non_empty_duplicate_tokens -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add admin.py tests/test_admin_channel_optimization.py
git commit -m "feat: add duplicate account token grouping"
```

## Task 2: Add Duplicate Token Admin Endpoint

**Files:**
- Modify: `admin.py:1259-1266`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing endpoint-shape test**

Append this test to `tests/test_admin_channel_optimization.py`:

```python
def test_duplicate_token_endpoint_is_registered_after_accounts_endpoint():
    routes = [getattr(route, "path", "") for route in admin.router.routes]

    assert "/providers/{name}/accounts/duplicate-tokens" in routes
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_duplicate_token_endpoint_is_registered_after_accounts_endpoint -v
```

Expected: FAIL because the route is not registered.

- [ ] **Step 3: Add the endpoint**

In `admin.py`, insert after `get_provider_accounts()`:

```python
@router.get("/providers/{name}/accounts/duplicate-tokens")
async def get_provider_duplicate_tokens(name: str, token: str = Header(None, alias="Authorization")):
    await _require_admin(token)
    cfg = await _read_provider_config(name)
    duplicates = _find_duplicate_account_tokens(cfg.get("accounts", []))
    return {"ok": True, "duplicates": duplicates}
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_duplicate_token_endpoint_is_registered_after_accounts_endpoint -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add admin.py tests/test_admin_channel_optimization.py
git commit -m "feat: expose duplicate account token check"
```

## Task 3: Make Runtime Retry Count Global Only

**Files:**
- Modify: `config.py:261-266`
- Test: `tests/test_retry_policy.py`

- [ ] **Step 1: Write failing retry behavior tests**

Append these tests to `tests/test_retry_policy.py`:

```python
def test_provider_retry_count_uses_global_retry_even_when_provider_has_retry_count(monkeypatch):
    monkeypatch.setattr(
        main.config.Config,
        "get_providers",
        classmethod(lambda cls: {"p1": {"retry_count": 9}}),
    )
    monkeypatch.setattr(
        main.config.Config,
        "_load",
        classmethod(lambda cls: {"retry": {"max_retries": 2}}),
    )

    assert main.config.Config.get_provider_retry_count("p1") == 2


def test_provider_retry_count_defaults_to_three_when_global_retry_missing(monkeypatch):
    monkeypatch.setattr(
        main.config.Config,
        "get_providers",
        classmethod(lambda cls: {"p1": {"retry_count": 9}}),
    )
    monkeypatch.setattr(main.config.Config, "_load", classmethod(lambda cls: {}))

    assert main.config.Config.get_provider_retry_count("p1") == 3
```

- [ ] **Step 2: Run tests to verify the first one fails**

Run:

```bash
pytest tests/test_retry_policy.py::test_provider_retry_count_uses_global_retry_even_when_provider_has_retry_count tests/test_retry_policy.py::test_provider_retry_count_defaults_to_three_when_global_retry_missing -v
```

Expected: first test FAILS with `assert 9 == 2`.

- [ ] **Step 3: Update retry count implementation**

Replace `Config.get_provider_retry_count()` in `config.py` with:

```python
    @classmethod
    def get_provider_retry_count(cls, provider: str) -> int:
        return int(cls._load().get("retry", {}).get("max_retries", 3) or 0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_retry_policy.py::test_provider_retry_count_uses_global_retry_even_when_provider_has_retry_count tests/test_retry_policy.py::test_provider_retry_count_defaults_to_three_when_global_retry_missing -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_retry_policy.py
git commit -m "fix: use global retry count for all channels"
```

## Task 4: Stop Writing Channel-Level Retry Config

**Files:**
- Modify: `admin.py:117-138`, `admin.py:1418-1450`, `admin.py:1632-1645`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing source-contract tests**

Append these tests to `tests/test_admin_channel_optimization.py`:

```python
def test_builtin_default_config_does_not_include_retry_count():
    cfg = admin._builtin_provider_default_config("kimi-ai")

    assert "retry_count" not in cfg


def test_create_custom_provider_source_does_not_write_retry_count():
    source = Path(admin.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def create_custom_provider"):source.index("@router.get(\"/custom-providers\")")]

    assert '"retry_count"' not in body


def test_update_custom_provider_source_does_not_accept_retry_count():
    source = Path(admin.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def update_custom_provider_config"):source.index("# ==================== 账号操作")]

    assert '"retry_count"' not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_builtin_default_config_does_not_include_retry_count tests/test_admin_channel_optimization.py::test_create_custom_provider_source_does_not_write_retry_count tests/test_admin_channel_optimization.py::test_update_custom_provider_source_does_not_accept_retry_count -v
```

Expected: FAIL because `retry_count` is still present in provider config creation/update paths.

- [ ] **Step 3: Remove provider retry writes**

In `admin.py`, remove this line from `_builtin_provider_default_config()`:

```python
            "retry_count": config.Config._load().get("retry", {}).get("max_retries", 3),
```

In `create_custom_provider()`, remove this line from `cfg`:

```python
        "retry_count": data.get("retry_count", config.Config._load().get("retry", {}).get("max_retries", 3)),
```

In `update_custom_provider_config()`, remove `"retry_count"` from the key tuple so it becomes:

```python
    for key in (
        "remark", "protocol", "base_url", "chat_path", "models_path",
        "account_priority", "account_weight", "supports_stream", "auto_update_models",
        "timeout", "health_check", "balance", "client_preset", "rate_limit"
    ):
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_builtin_default_config_does_not_include_retry_count tests/test_admin_channel_optimization.py::test_create_custom_provider_source_does_not_write_retry_count tests/test_admin_channel_optimization.py::test_update_custom_provider_source_does_not_accept_retry_count -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add admin.py tests/test_admin_channel_optimization.py
git commit -m "fix: stop storing channel retry counts"
```

## Task 5: Add Provider Summary Updated Timestamp

**Files:**
- Modify: `admin.py:1170-1214`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing provider summary test**

Append this test to `tests/test_admin_channel_optimization.py`:

```python
def test_provider_summary_includes_numeric_updated_at_ts(monkeypatch):
    monkeypatch.setattr(admin, "_provider_models", lambda name: [])

    summary = admin._provider_summary_response(
        "p1",
        {
            "enabled": True,
            "accounts": [],
            "updated_at": 1710000000,
        },
    )

    assert summary["updated_at_ts"] == 1710000000
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_provider_summary_includes_numeric_updated_at_ts -v
```

Expected: FAIL with `KeyError: 'updated_at_ts'`.

- [ ] **Step 3: Add timestamp normalization helper and summary field**

In `admin.py`, insert after `_find_duplicate_account_tokens()`:

```python
def _provider_updated_at_ts(cfg: dict) -> float:
    raw = cfg.get("updated_at_ts", cfg.get("updated_at", 0))
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
```

In `_provider_summary_response()`, add this field before `"models"`:

```python
        "updated_at_ts": _provider_updated_at_ts(cfg),
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_provider_summary_includes_numeric_updated_at_ts -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add admin.py tests/test_admin_channel_optimization.py
git commit -m "feat: expose provider update timestamps"
```

## Task 6: Add Duplicate Token Button and Modal UI

**Files:**
- Modify: `static/admin.html:1545-1556`, add helper near account operations after `filterAccounts()`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing static UI tests**

Append these tests to `tests/test_admin_channel_optimization.py`:

```python
def test_account_list_header_has_duplicate_token_button():
    html = admin_html()

    assert "检测重复秘钥" in html
    assert "showDuplicateTokens(" in html


def test_duplicate_token_modal_calls_backend_endpoint_and_handles_empty_state():
    html = admin_html()
    body = function_body(html, "showDuplicateTokens")

    assert "/accounts/duplicate-tokens" in body
    assert "未发现重复秘钥" in body
    assert "token_masked" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_account_list_header_has_duplicate_token_button tests/test_admin_channel_optimization.py::test_duplicate_token_modal_calls_backend_endpoint_and_handles_empty_state -v
```

Expected: FAIL because the button and `showDuplicateTokens()` do not exist.

- [ ] **Step 3: Add button to account-list header**

In `static/admin.html`, replace the account-list header button group at `static/admin.html:1546` with:

```javascript
  html += `<div class="card"><div class="card-header"><h3>账号列表</h3><div style="display:flex;gap:6px;align-items:center"><span class="meta" id="accountCount">${p.accounts.length} 个</span><button class="btn sm" onclick="showDuplicateTokens('${name}')">检测重复秘钥</button><button class="btn sm" onclick="refreshAccountList('${name}')">刷新</button><button class="btn primary sm" onclick="showAddAccount('${name}')">+ 添加账号</button><button class="btn sm" onclick="showTestAccounts('${name}')">模型测试</button></div></div><div class="card-body">
```

- [ ] **Step 4: Add duplicate-token modal function**

Insert after `filterAccounts(name)` in `static/admin.html`:

```javascript
async function showDuplicateTokens(name) {
  try {
    const result = await api(`/admin/providers/${encodeURIComponent(name)}/accounts/duplicate-tokens`);
    const duplicates = result.duplicates || [];
    const html = duplicates.length ? duplicates.map(group => `
      <div class="account-card" style="align-items:flex-start">
        <div class="a-left">
          <div class="avatar warn">!</div>
          <div class="a-info">
            <div class="a-name">${esc(group.token_masked || '')}</div>
            <div class="a-meta"><span>重复 ${group.count || 0} 个账号</span></div>
            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
              ${(group.accounts || []).map(account => `<span class="tag ${account.enabled ? 'on' : 'off'}">${esc(account.username || '-')} · ${account.enabled ? '启用' : '停用'}</span>`).join('')}
            </div>
          </div>
        </div>
      </div>
    `).join('') : '<div class="empty">未发现重复秘钥</div>';
    openModal('重复秘钥检测', html, null);
  } catch (e) {
    openModal('重复秘钥检测', `<div class="empty">检测失败：${esc(e.message || String(e))}</div>`, null);
  }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_account_list_header_has_duplicate_token_button tests/test_admin_channel_optimization.py::test_duplicate_token_modal_calls_backend_endpoint_and_handles_empty_state -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add static/admin.html tests/test_admin_channel_optimization.py
git commit -m "feat: add duplicate token check modal"
```

## Task 7: Remove Channel Retry UI From Admin Page

**Files:**
- Modify: `static/admin.html:1081-1122`, `static/admin.html:2050-2107`, `static/admin.html:4930-4938`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing static UI tests**

Append these tests to `tests/test_admin_channel_optimization.py`:

```python
def test_channel_config_cards_do_not_render_retry_count():
    html = admin_html()

    assert "重试次数" not in function_body(html, "buildBuiltinChannelConfig")
    assert "重试次数" not in function_body(html, "showCustomConfig")
    assert "ccRetryCount" not in html


def test_custom_channel_creation_no_longer_posts_retry_count():
    html = admin_html()
    body = function_body(html, "createCustomChannel")

    assert "retry_count" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_channel_config_cards_do_not_render_retry_count tests/test_admin_channel_optimization.py::test_custom_channel_creation_no_longer_posts_retry_count -v
```

Expected: FAIL because `重试次数`, `ccRetryCount`, and `retry_count` are still present in UI paths.

- [ ] **Step 3: Remove built-in retry display**

In `buildBuiltinChannelConfig()`, delete this block:

```javascript
    <div class="kv-col">
      <span class="key">重试次数</span>
      <span class="val">${p.retry_count ?? '-'}</span>
    </div>
```

- [ ] **Step 4: Remove custom channel retry editor**

In `showCustomConfig()`, remove this form group from the account priority/weight row:

```javascript
      <div class="form-group"><label>重试次数</label><input type="number" id="ccRetryCount" value="${cfg.retry_count ?? ''}" placeholder="留空使用全局"><div class="form-hint">单个请求最多换账号重试几次，留空走全局配置。</div></div>
```

Also remove this object property from the `data` object:

```javascript
      retry_count: (() => {
        const v = (document.getElementById('ccRetryCount')?.value || '').trim();
        if (v === '') return null;
        const n = parseInt(v);
        return Number.isFinite(n) && n >= 0 ? n : null;
      })(),
```

- [ ] **Step 5: Remove custom channel creation retry payload**

In `createCustomChannel()`, remove this object property:

```javascript
    retry_count: parseInt(document.getElementById('ccRetryCount')?.value) || 3,
```

If the create wizard has a retry-count input, remove that input markup too.

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_channel_config_cards_do_not_render_retry_count tests/test_admin_channel_optimization.py::test_custom_channel_creation_no_longer_posts_retry_count -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add static/admin.html tests/test_admin_channel_optimization.py
git commit -m "fix: remove channel retry controls"
```

## Task 8: Add Channel Sorting and Exact Model Filter

**Files:**
- Modify: `static/admin.html:900-972`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing static UI tests**

Append these tests to `tests/test_admin_channel_optimization.py`:

```python
def test_channels_page_has_sort_and_model_filter_controls():
    html = admin_html()
    body = function_body(html, "renderChannelsPage")

    assert "channelSortBy" in body
    assert "channelSortDir" in body
    assert "channelModelFilter" in body
    assert "全部模型" in body


def test_channel_cards_filter_by_exact_model_and_sort_by_updated_at_default():
    html = admin_html()
    body = function_body(html, "renderChannelCards")

    assert "modelFilter" in body
    assert "channelHasModel" in body
    assert "updated_at_ts" in body
    assert "没有支持该模型的渠道" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_channels_page_has_sort_and_model_filter_controls tests/test_admin_channel_optimization.py::test_channel_cards_filter_by_exact_model_and_sort_by_updated_at_default -v
```

Expected: FAIL because the controls and helper do not exist.

- [ ] **Step 3: Add model filter helpers**

Insert before `renderChannelsPage()`:

```javascript
function channelModelValues() {
  return [...new Set((S.providers || []).flatMap(p => (p.models || []).map(m => m.id || m).filter(Boolean)))].sort();
}

function channelHasModel(provider, model) {
  if (!model) return true;
  return (provider.models || []).some(m => (m.id || m) === model);
}
```

- [ ] **Step 4: Update channel filter controls**

In `renderChannelsPage()`, replace the `const f = ...` line with:

```javascript
  const f = S.channelFilters || {name:'',status:'',type:'',model:'',sortBy:'updated_at',sortDir:'desc'};
  S.channelFilters = f;
  const modelOptions = channelModelValues().map(m => `<option value="${escAttr(m)}" ${f.model===m?'selected':''}>${esc(m)}</option>`).join('');
```

In the `.channel-filters` markup, after the type filter select, add:

```javascript
      <select id="channelModelFilter" onchange="S.channelFilters.model=this.value;renderChannelCards()">
        <option value="">全部模型</option>${modelOptions}
      </select>
      <select id="channelSortBy" onchange="S.channelFilters.sortBy=this.value;renderChannelCards()">
        <option value="updated_at" ${f.sortBy==='updated_at'?'selected':''}>按更新时间</option>
        <option value="name" ${f.sortBy==='name'?'selected':''}>按渠道名称</option>
        <option value="account_count" ${f.sortBy==='account_count'?'selected':''}>按账号总数</option>
        <option value="enabled_account_count" ${f.sortBy==='enabled_account_count'?'selected':''}>按可用账号数</option>
      </select>
      <select id="channelSortDir" onchange="S.channelFilters.sortDir=this.value;renderChannelCards()">
        <option value="desc" ${f.sortDir!=='asc'?'selected':''}>倒序</option>
        <option value="asc" ${f.sortDir==='asc'?'selected':''}>升序</option>
      </select>
```

- [ ] **Step 5: Apply model filtering and sorting**

In `renderChannelCards()`, replace the filter initialization with:

```javascript
  const filters = S.channelFilters || {name:'',status:'',type:'',model:'',sortBy:'updated_at',sortDir:'desc'};
  const nameFilter = (filters.name || '').trim().toLowerCase();
  const statusFilter = filters.status || '';
  const typeFilter = filters.type || '';
  const modelFilter = filters.model || '';
  const sortBy = filters.sortBy || 'updated_at';
  const sortDir = filters.sortDir === 'asc' ? 1 : -1;
```

Inside the `S.providers.filter(...)` callback, before `return true;`, add:

```javascript
    if (modelFilter && !channelHasModel(p, modelFilter)) return false;
```

After the filter callback, add sorting:

```javascript
  filteredProviders.sort((a, b) => {
    const valueOf = (p) => {
      if (sortBy === 'name') return (p.remark || p.name || '').toLowerCase();
      if (sortBy === 'account_count') return Number(p.account_count || 0);
      if (sortBy === 'enabled_account_count') return Number(p.enabled_account_count || 0);
      return Number(p.updated_at_ts || 0);
    };
    const av = valueOf(a);
    const bv = valueOf(b);
    if (av < bv) return -1 * sortDir;
    if (av > bv) return 1 * sortDir;
    return (a.name || '').localeCompare(b.name || '');
  });
```

At the end of `renderChannelCards()`, replace the empty text assignment with:

```javascript
  if (cardsEl) cardsEl.innerHTML = htmlRows || `<div class="empty">${modelFilter ? '没有支持该模型的渠道' : '无匹配渠道'}</div>`;
```

- [ ] **Step 6: Run tests to verify they pass**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_channels_page_has_sort_and_model_filter_controls tests/test_admin_channel_optimization.py::test_channel_cards_filter_by_exact_model_and_sort_by_updated_at_default -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add static/admin.html tests/test_admin_channel_optimization.py
git commit -m "feat: add channel sorting and model filter"
```

## Task 9: Replace Model Route Detail Entries With Channel Switches

**Files:**
- Modify: `static/admin.html:1922-1970`
- Test: `tests/test_admin_channel_optimization.py`

- [ ] **Step 1: Write failing static UI tests**

Append these tests to `tests/test_admin_channel_optimization.py`:

```python
def test_route_detail_uses_supported_channel_switches_not_manual_entry_add_delete():
    html = admin_html()
    body = function_body(html, "openRouteDetail")

    assert "routeChannelRows" in body
    assert "支持该模型的渠道" in body
    assert "+ 增加顺序项" not in body
    assert "re-account" not in body


def test_route_channel_rows_preserve_existing_enabled_order_and_default_all_enabled():
    html = admin_html()

    assert "function routeChannelRows(" in html
    assert "function collectRouteChannelEntries(" in html
    rows_body = function_body(html, "routeChannelRows")
    collect_body = function_body(html, "collectRouteChannelEntries")

    assert "supportedProviders" in rows_body
    assert "route.entries" in rows_body
    assert "route.__defaultAllChannels" in rows_body
    assert "route-channel-enabled" in rows_body
    assert "route-channel-row" in collect_body
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_route_detail_uses_supported_channel_switches_not_manual_entry_add_delete tests/test_admin_channel_optimization.py::test_route_channel_rows_preserve_existing_enabled_order_and_default_all_enabled -v
```

Expected: FAIL because model route detail still uses manual provider/account rows.

- [ ] **Step 3: Add supported-channel helpers**

Insert before `openRouteDetail(i)` in `static/admin.html`:

```javascript
function providerSupportsModel(provider, model) {
  if (!model) return false;
  return (provider.models || []).some(m => (m.id || m) === model);
}

function routeChannelRows(route) {
  const providers = ((window._modelRoutingMeta || {}).providers || []);
  const model = route.model || '';
  const supportedProviders = providers.filter(p => providerSupportsModel(p, model));
  const entries = Array.isArray(route.entries) ? route.entries : [];
  const enabledNames = new Set(entries.map(e => e.provider).filter(Boolean));
  const byName = new Map(supportedProviders.map(p => [p.name, p]));
  const ordered = [];
  entries.forEach(entry => {
    const provider = byName.get(entry.provider);
    if (provider && !ordered.some(p => p.name === provider.name)) ordered.push(provider);
  });
  supportedProviders.forEach(provider => {
    if (!ordered.some(p => p.name === provider.name)) ordered.push(provider);
  });
  route.__defaultAllChannels = entries.length === 0;
  return ordered.map((provider, index) => {
    const enabled = route.__defaultAllChannels || enabledNames.has(provider.name);
    const accountCount = (provider.accounts || []).length;
    return `<tr class="route-channel-row" data-provider="${escAttr(provider.name)}" style="border-bottom:1px solid var(--border)">
      <td style="padding:7px"><button class="btn sm" onclick="moveRouteEntry(this,-1)">↑</button><button class="btn sm" onclick="moveRouteEntry(this,1)">↓</button></td>
      <td style="padding:7px">${toggleHtml(enabled, `routeChannelEnabled_${index}`, 'on')}<input class="route-channel-enabled" type="hidden" value="${enabled ? 'true' : 'false'}"></td>
      <td style="padding:7px;font-family:var(--fontM)">${esc(provider.name)}</td>
      <td style="padding:7px;color:var(--text2)">账号 ${accountCount}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="4" class="empty">没有支持该模型的渠道</td></tr>';
}

function bindRouteChannelSwitches() {
  document.querySelectorAll('#routeChannelBody .route-channel-row').forEach(row => {
    const checkbox = row.querySelector('.switch input');
    const hidden = row.querySelector('.route-channel-enabled');
    if (checkbox && hidden) checkbox.onchange = () => { hidden.value = checkbox.checked ? 'true' : 'false'; };
  });
}

function collectRouteChannelEntries() {
  return [...document.querySelectorAll('#routeChannelBody .route-channel-row')]
    .filter(row => row.querySelector('.route-channel-enabled')?.value === 'true')
    .map(row => ({provider: row.dataset.provider, account: ''}))
    .filter(entry => entry.provider);
}
```

- [ ] **Step 4: Replace `openRouteDetail()` modal body and save logic**

Replace the whole `openRouteDetail(i)` function with:

```javascript
function openRouteDetail(i) {
  const route = window._modelRoutes[i];
  openModal('模型专用线路详情', `<div class="form-row"><div class="form-group"><label>请求模型</label><input id="rdModel" list="rdModelList" value="${escAttr(route.model || '')}" placeholder="输入或选择模型" onchange="document.getElementById('routeChannelBody').innerHTML = routeChannelRows({...route, model:this.value.trim()}); bindRouteChannelSwitches();"><datalist id="rdModelList">${modelDatalistOptions()}</datalist></div><div class="form-group"><label>启用</label><select id="rdEnabled"><option value="true" ${route.enabled!==false?'selected':''}>启用</option><option value="false" ${route.enabled===false?'selected':''}>禁用</option></select></div></div>${apiKeyScopeHtml(route)}<div class="form-group"><label>支持该模型的渠道</label><table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr><th style="padding:7px;width:90px">排序</th><th style="padding:7px;width:90px">状态</th><th style="padding:7px">渠道</th><th style="padding:7px;width:120px">账号概览</th></tr></thead><tbody id="routeChannelBody">${routeChannelRows(route)}</tbody></table><div class="form-hint">开启的渠道会按当前顺序写入专用线路；关闭的渠道不参与路由。</div></div>`, async () => {
    window._modelRoutes[i] = {...route, enabled: document.getElementById('rdEnabled').value === 'true', model: document.getElementById('rdModel').value.trim(), entries: collectRouteChannelEntries(), ...readApiKeyScope()};
    await saveModelRouting();
  });
  setTimeout(bindRouteChannelSwitches, 0);
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
pytest tests/test_admin_channel_optimization.py::test_route_detail_uses_supported_channel_switches_not_manual_entry_add_delete tests/test_admin_channel_optimization.py::test_route_channel_rows_preserve_existing_enabled_order_and_default_all_enabled -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add static/admin.html tests/test_admin_channel_optimization.py
git commit -m "feat: switch model routes by channel"
```

## Task 10: Ignore Visual Brainstorming Artifacts

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add ignore entry**

Append this line to `.gitignore`:

```gitignore
.superpowers/
```

- [ ] **Step 2: Verify status no longer lists `.superpowers/`**

Run:

```bash
git status --short
```

Expected: output does not include `?? .superpowers/`.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: ignore superpowers artifacts"
```

## Task 11: Run Focused Test Suite

**Files:**
- Test only.

- [ ] **Step 1: Run all touched tests**

Run:

```bash
pytest tests/test_admin_channel_optimization.py tests/test_retry_policy.py tests/test_admin_limit_policy_ui.py -v
```

Expected: PASS.

- [ ] **Step 2: Fix any failures using the smallest change**

If a failure is in `tests/test_admin_channel_optimization.py`, update the implementation or the static assertion so the test matches the approved design. If a failure is in `tests/test_admin_limit_policy_ui.py`, preserve the existing three-card channel detail layout and limit-policy modal behavior.

- [ ] **Step 3: Commit any test-fix changes**

If Step 2 changed files, run:

```bash
git add admin.py config.py static/admin.html tests/test_admin_channel_optimization.py tests/test_retry_policy.py tests/test_admin_limit_policy_ui.py
git commit -m "test: stabilize channel optimization coverage"
```

If Step 2 changed nothing, skip this commit.

## Task 12: Manual Admin UI Verification

**Files:**
- No code changes unless verification finds a bug.

- [ ] **Step 1: Start the app**

Run the project’s normal local server command. If no project-specific command exists, try:

```bash
python main.py
```

Expected: admin app starts without import errors.

- [ ] **Step 2: Verify channel detail duplicate-token flow**

Open the admin UI, navigate to a channel detail page, and click “检测重复秘钥”. Expected:

- With duplicate account tokens, modal shows masked token groups and account names.
- With no duplicate tokens, modal shows “未发现重复秘钥”.
- Full token values are not visible.

- [ ] **Step 3: Verify channel retry controls are gone**

On built-in and custom channel detail pages, confirm there is no channel-level “重试次数” field. Open the custom channel edit modal and custom channel creation flow; confirm no retry-count field appears.

- [ ] **Step 4: Verify channel list sorting and model filtering**

Open the channels page. Expected:

- Default order is更新时间倒序.
- Sorting by name, account total, and available account count changes the card order.
- Choosing a model from “全部模型” only shows channels that support that model.
- A model with no matching channels displays “没有支持该模型的渠道”.

- [ ] **Step 5: Verify model route detail channel switches**

Open “模型路由/分组”, then a model route detail modal. Expected:

- Modal lists all channels that support the selected model.
- New route with no entries shows all supported channels enabled.
- Existing route preserves enabled entry order and appends unsupported-in-route but model-supporting channels as disabled.
- Saving writes only enabled providers into the route entries.
- Manual add/delete entry controls are absent.

- [ ] **Step 6: Fix and commit any manual verification bugs**

If verification finds bugs, make the smallest code change, rerun the focused tests from Task 11, and commit:

```bash
git add admin.py config.py static/admin.html tests/test_admin_channel_optimization.py tests/test_retry_policy.py .gitignore
git commit -m "fix: complete channel admin verification"
```
