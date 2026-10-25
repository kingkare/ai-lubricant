# Channel Limit Policy Card Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the channel detail page show Channel Configuration, Limit Policy, and Model List as one row, with Limit Policy reduced to a read-only key-parameter summary and edited through a modal.

**Architecture:** Keep the change localized to `static/admin.html`. Split the current limit-policy renderer into a summary-card renderer plus a reusable editor-body renderer so the page and modal do not parse HTML with regex. Preserve the existing PUT API payload and refresh behavior.

**Tech Stack:** Single-file HTML/CSS/vanilla JavaScript admin UI, existing `openModal`, `api`, `refreshProviderPage`, and pytest for lightweight static UI regression checks.

---

## File Structure

- Modify `static/admin.html`: update channel detail layout CSS, add compact summary styles, split limit policy card/editor rendering, and wire the edit modal to the editor renderer.
- Create `tests/test_admin_limit_policy_ui.py`: static regression tests for the HTML/JS source to lock in the three-card row, compact summary-only page card, modal editor, and no fragile regex stripping.

---

### Task 1: Add static regression tests for compact limit-policy layout

**Files:**
- Create: `tests/test_admin_limit_policy_ui.py`
- Inspect: `static/admin.html`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_admin_limit_policy_ui.py` with this exact content:

```python
from pathlib import Path


ADMIN_HTML = Path(__file__).resolve().parents[1] / "static" / "admin.html"


def admin_html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1:index]
    raise AssertionError(f"Function {name} body not found")


def test_channel_detail_layout_renders_three_top_cards_in_one_row():
    html = admin_html()

    assert ".channel-detail-grid" in html
    body = function_body(html, "renderChannelLayout")
    assert "channel-detail-grid" in body
    assert "renderChannelConfig(name, p, viewType, ctx)" in body
    assert "renderLimitPolicyCard(name, ctx.limitPolicy" in body
    assert "renderModelConfig(name, p, viewType, ctx)" in body
    assert "admin-split-layout" not in body


def test_limit_policy_page_card_is_summary_only_and_opens_editor_modal():
    html = admin_html()
    body = function_body(html, "renderLimitPolicyCard")

    assert "showLimitPolicy(" in body
    assert "保存策略" not in body
    assert "id=\"lpEnabled\"" not in body
    assert "policy-editor-grid" not in body
    for label in ["策略状态", "账号 RPM", "账号 TPM", "模型 TPM", "账号并发", "余额限制", "冷却策略"]:
        assert label in body


def test_limit_policy_editor_modal_uses_dedicated_editor_renderer():
    html = admin_html()
    modal_body = function_body(html, "showLimitPolicy")
    editor_body = function_body(html, "renderLimitPolicyEditor")

    assert "renderLimitPolicyEditor(policy, methods)" in modal_body
    assert ".replace(" not in modal_body
    for element_id in ["lpEnabled", "lpRpm", "lpTpm", "lpModelTpm", "lpConcurrent", "lpBalanceEnabled", "lpBalanceMethod", "lpBalanceMin", "lpBalanceTtl", "lpCooldownRows"]:
        assert element_id in editor_body
```

- [ ] **Step 2: Run tests to verify they fail for the missing UI refactor**

Run:

```bash
pytest tests/test_admin_limit_policy_ui.py -q
```

Expected: FAIL. At least one failure should mention `.channel-detail-grid` missing or `renderLimitPolicyEditor` not found.

- [ ] **Step 3: Do not change production code in this task**

No production code changes belong in Task 1.

---

### Task 2: Change the channel detail top area to a three-card row

**Files:**
- Modify: `static/admin.html:230-241`
- Modify: `static/admin.html:1049-1053`
- Test: `tests/test_admin_limit_policy_ui.py`

- [ ] **Step 1: Replace the old two-column layout CSS with the three-card grid CSS**

In `static/admin.html`, replace the CSS block starting at `.admin-split-layout` and ending at `.policy-editor-grid .full{grid-column:1/-1}` with:

```css
.channel-detail-grid{display:grid;grid-template-columns:minmax(300px,.85fr) minmax(260px,.65fr) minmax(520px,1.35fr);gap:14px;width:100%;align-items:stretch;margin-bottom:14px}
.channel-detail-grid>.card{min-width:0;height:100%;display:flex;flex-direction:column}
.provider-summary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;align-content:start;flex:1}
.provider-summary-grid.single{display:flex;flex-direction:column}
.limit-summary-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;align-content:start}
.limit-summary-item{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:10px;min-width:0}
.limit-summary-item .label{font-size:11px;color:var(--text2);margin-bottom:5px}
.limit-summary-item .value{font-size:15px;font-weight:700;color:var(--textH);font-family:var(--fontM);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.limit-summary-item .hint{font-size:11px;color:var(--text2);margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.policy-editor-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.policy-editor-grid .wide{grid-column:span 2}
.policy-editor-grid .full{grid-column:1/-1}
@media (max-width:1400px){.channel-detail-grid{grid-template-columns:1fr 1fr}.channel-detail-grid>.card:last-child{grid-column:1/-1}}
@media (max-width:900px){.channel-detail-grid{grid-template-columns:1fr}.channel-detail-grid>.card:last-child{grid-column:auto}.policy-editor-grid{grid-template-columns:1fr}.policy-editor-grid .wide{grid-column:auto}}
```

- [ ] **Step 2: Update `renderChannelLayout` to return three sibling cards**

Replace the full `renderChannelLayout` function with:

```javascript
function renderChannelLayout(name, p, viewType, ctx) {
  return `<div class="channel-detail-grid">
    ${renderChannelConfig(name, p, viewType, ctx)}
    ${renderLimitPolicyCard(name, ctx.limitPolicy, ctx.balanceMethods)}
    ${renderModelConfig(name, p, viewType, ctx)}
  </div>`;
}
```

- [ ] **Step 3: Run the focused tests**

Run:

```bash
pytest tests/test_admin_limit_policy_ui.py -q
```

Expected: still FAIL because `renderLimitPolicyCard` is not summary-only yet and `renderLimitPolicyEditor` does not exist.

---

### Task 3: Split the limit-policy editor out of the page card

**Files:**
- Modify: `static/admin.html:1229-1265`
- Test: `tests/test_admin_limit_policy_ui.py`

- [ ] **Step 1: Replace `renderLimitPolicyCard` and `showLimitPolicy` with summary card plus editor renderer**

In `static/admin.html`, replace the existing `renderLimitPolicyCard` function and `showLimitPolicy` function with this exact code:

```javascript
function formatLimitValue(value) {
  const n = Number(value || 0);
  return n > 0 ? String(n) : '不限';
}

function summarizeCooldownPolicy(policy={}) {
  const entries = Object.entries(policy && typeof policy === 'object' ? policy : {});
  if (!entries.length) return '默认';
  return entries.map(([codes, seconds]) => `${codes}:${seconds}s`).join(' · ');
}

function renderLimitSummaryItem(label, value, hint='') {
  return `<div class="limit-summary-item"><div class="label">${label}</div><div class="value" title="${escAttr(value)}">${esc(value)}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>`;
}

function renderLimitPolicyCard(name, policy, methods=[]) {
  if (!policy) {
    return `<div class="card"><div class="card-header"><h3>限制策略</h3></div><div class="card-body"><div class="empty">限制策略加载失败</div></div></div>`;
  }
  const balanceText = policy.balance_enabled === true ? '开启' : '关闭';
  const cooldownText = summarizeCooldownPolicy(policy.cooldown_policy || {429:'60', error:'60'});
  return `<div class="card"><div class="card-header"><h3>限制策略</h3><button class="btn sm" onclick="showLimitPolicy('${name}')">编辑</button></div><div class="card-body">
    <div class="limit-summary-grid">
      ${renderLimitSummaryItem('策略状态', policy.enabled!==false?'启用':'关闭', '账号级调度')}
      ${renderLimitSummaryItem('账号 RPM', formatLimitValue(policy.account_rpm), '0 表示不限')}
      ${renderLimitSummaryItem('账号 TPM', formatLimitValue(policy.account_tpm), '0 表示不限')}
      ${renderLimitSummaryItem('模型 TPM', formatLimitValue(policy.model_tpm), '0 表示不限')}
      ${renderLimitSummaryItem('账号并发', formatLimitValue(policy.account_concurrent), '每账号并发')}
      ${renderLimitSummaryItem('余额限制', balanceText, policy.balance_enabled === true ? '低余额停用' : '不检查余额')}
      ${renderLimitSummaryItem('冷却策略', cooldownText, '-1 表示当天不可用')}
    </div>
  </div></div>`;
}

function renderLimitPolicyEditor(policy, methods=[]) {
  const methodOptions = ['<option value="">不选择</option>'].concat((methods || []).map(m => `<option value="${m.id}" ${String(policy.balance_method_id || '')===String(m.id)?'selected':''}>${esc(m.name)} (${esc(m.type)})</option>`)).join('');
  const cooldownRows = renderCooldownRows(policy.cooldown_policy || {429:'60', error:'60'});
  return `<div class="policy-editor-grid">
    <div class="form-group"><label>启用策略</label><select id="lpEnabled"><option value="true" ${policy.enabled!==false?'selected':''}>启用</option><option value="false" ${policy.enabled===false?'selected':''}>关闭</option></select></div>
    <div class="form-group"><label>账号 RPM</label><input type="number" id="lpRpm" value="${policy.account_rpm || 0}"></div>
    <div class="form-group"><label>账号 TPM</label><input type="number" id="lpTpm" value="${policy.account_tpm || 0}"></div>
    <div class="form-group"><label>模型 TPM</label><input type="number" id="lpModelTpm" value="${policy.model_tpm || 0}"></div>
    <div class="form-group"><label>账号并发</label><input type="number" id="lpConcurrent" value="${policy.account_concurrent || 0}"></div>
    <div class="form-group"><label>余额限制</label><select id="lpBalanceEnabled"><option value="false" ${policy.balance_enabled!==true?'selected':''}>关闭</option><option value="true" ${policy.balance_enabled===true?'selected':''}>开启</option></select></div>
    <div class="form-group wide"><label>余额方式</label><select id="lpBalanceMethod">${methodOptions}</select><div class="form-hint">可在左侧“余额方式”页面维护。</div></div>
    <div class="form-group"><label>最低余额 / remaining</label><input type="number" id="lpBalanceMin" value="${policy.balance_min_value ?? 0}"></div>
    <div class="form-group"><label>Redis 缓存 TTL（秒）</label><input type="number" id="lpBalanceTtl" value="${policy.balance_cache_ttl_seconds || 300}"></div>
    <div class="form-group full"><label>冷却策略</label><div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="color:var(--text2);border-bottom:1px solid var(--border)"><th style="padding:6px;text-align:left">状态码组合</th><th style="padding:6px;text-align:left">冷却秒数</th><th style="padding:6px;text-align:left">操作</th></tr></thead><tbody id="lpCooldownRows">${cooldownRows}</tbody></table></div><button class="btn sm" style="margin-top:8px" onclick="addCooldownRow()">+ 添加状态码组</button><div class="form-hint">状态码可写 429 或 500,502；error 不可删除，未命中状态码或异常时使用 error；-1 表示当天不可用。</div></div>
  </div>`;
}

async function showLimitPolicy(name) {
  const p = S.providers.find(x => x.name === name) || await loadProviderDetail(name);
  const [policy, methods] = await Promise.all([
    api(`/admin/providers/${encodeURIComponent(name)}/limit-policy`),
    api('/admin/balance-methods').catch(() => []),
  ]);
  openModal(`限制策略 - ${esc(p?.remark || name)}`, renderLimitPolicyEditor(policy, methods), async () => saveLimitPolicyCard(name, false));
}
```

- [ ] **Step 2: Run the focused tests**

Run:

```bash
pytest tests/test_admin_limit_policy_ui.py -q
```

Expected: PASS.

- [ ] **Step 3: Run existing Python tests**

Run:

```bash
pytest tests -q
```

Expected: PASS for all tests.

---

### Task 4: Manual browser verification of the channel detail page

**Files:**
- Verify: `static/admin.html`

- [ ] **Step 1: Start the app**

Run:

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Expected: server starts and logs Uvicorn running at `http://127.0.0.1:8000`.

- [ ] **Step 2: Open the admin UI**

Open:

```text
http://127.0.0.1:8000/admin
```

Expected: admin login or admin shell loads.

- [ ] **Step 3: Verify the visual layout**

Navigate to any channel detail page. Expected:

- The top row shows `渠道配置`, `限制策略`, and `渠道模型列表` in the same horizontal row on wide screens.
- The `限制策略` card is compact and read-only.
- The visible limit-policy fields are exactly: `策略状态`, `账号 RPM`, `账号 TPM`, `模型 TPM`, `账号并发`, `余额限制`, `冷却策略`.
- There are no inline editable limit-policy fields on the page.

- [ ] **Step 4: Verify modal editing**

Click `编辑` on the `限制策略` card. Expected:

- A modal opens titled `限制策略 - <渠道名或备注>`.
- The modal contains the full editable form, including enable switch, RPM/TPM/model TPM/concurrency, balance settings, TTL, and cooldown rows.
- Saving the modal sends the existing limit-policy PUT request, closes the modal, and keeps the page usable.

- [ ] **Step 5: Stop the app**

Stop the Uvicorn process with Ctrl+C.

---

### Task 5: Final verification and commit

**Files:**
- Modify: `static/admin.html`
- Create: `tests/test_admin_limit_policy_ui.py`

- [ ] **Step 1: Run all tests**

Run:

```bash
pytest tests -q
```

Expected: PASS.

- [ ] **Step 2: Review the diff**

Run:

```bash
git diff -- static/admin.html tests/test_admin_limit_policy_ui.py
```

Expected:

- `static/admin.html` only changes the channel detail top layout and limit-policy summary/modal rendering.
- `tests/test_admin_limit_policy_ui.py` contains the static regression tests from Task 1.
- No unrelated files are changed by this work.

- [ ] **Step 3: Commit only the relevant files if the user requested a commit**

Only if explicitly asked to commit, run:

```bash
git add static/admin.html tests/test_admin_limit_policy_ui.py
git commit -m "优化渠道限制策略卡片布局"
```

Expected: a new commit is created. Do not commit unrelated working-tree changes.

---

## Self-Review

- Spec coverage: The plan covers the three-card row, compact read-only limit-policy summary, key fields including model TPM, modal-based editing, preserved save API, responsive fallback, and verification.
- Placeholder scan: No TBD/TODO/fill-later placeholders remain.
- Type consistency: Function names and element IDs are consistent across tests and implementation steps: `renderLimitPolicyCard`, `renderLimitPolicyEditor`, `showLimitPolicy`, `lpEnabled`, `lpRpm`, `lpTpm`, `lpModelTpm`, `lpConcurrent`, `lpBalanceEnabled`, `lpBalanceMethod`, `lpBalanceMin`, `lpBalanceTtl`, `lpCooldownRows`.
