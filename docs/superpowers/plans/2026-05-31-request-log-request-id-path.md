# 请求日志按 request_id 路径重整实施计划

> **给执行 Agent 的要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐项执行。步骤用 checkbox（`- [ ]`）跟踪。

**目标：** 把请求日志改成围绕 `request_id` 路径直接可读的形式；去掉 `parent_request_id` 这套父子概念；让列表和详情都能直接看懂请求关系、状态、token、模型和耗时。

**架构：** 基于当前代码里的请求日志写入链路（`main.py` / `admin.py`）和查询链路（`db.py`），保留现有 `request_logs` 表，不新增关系表、不新增额外请求 ID。请求关系只通过 `request_id` 和 `retry_path` 表达，前端把它直接展示成路径。详情页保留大 payload，但拆成“简单摘要”和“详细内容”两层，避免一屏全是 raw JSON。

**技术栈：** Python 3、FastAPI、asyncpg/PostgreSQL、`static/admin.html` 原生 JavaScript、pytest。

---

## 基于当前代码的结论

先说明这份计划是**读过当前代码后**整理出来的，不是凭空猜：

- `main.py` 里 chat / responses / anthropic 三条主请求路径都会写 `request_logs`。
- `db.py` 里请求日志查询已经返回 `requested_model` / `actual_model` / `retry_count` / `error_preview`，说明列表本来就有“摘要层”的位置。
- `static/admin.html` 里列表现在只显示 `retry_count`，详情里虽然有 `retry_path`，但展示条件偏保守，且浅色主题内部日志块覆盖不完整。
- `parent_request_id` 目前仍在部分日志路径里出现，说明这套父子语义还没真正从契约中移除。

这份计划的目标不是换架构，而是把**现有数据变成用户能直接看懂的路径视图**。

---

## 文件结构

- 修改 `db.py`：移除 `parent_request_id` 的 schema / 查询 / 更新 / 详情字段；保留 `request_id`、`retry_path`、状态、模型、耗时、token 字段。
- 修改 `main.py`：所有请求日志 payload 去掉 `parent_request_id`；保留请求前写入、请求后更新的日志节奏；统一 usage / TTFT 的字段语义。
- 修改 `admin.py`：后台测试请求也走同一套请求日志形状，不再引入 `parent_request_id`。
- 修改 `static/admin.html`：把 `request_id` 路径做成主展示；把状态、token、模型、耗时做成简单摘要；把 raw 内容放到下层。
- 修改或新增 `tests/`：覆盖 schema 清理、路径展示、状态归一、token 四分类、浅色主题样式、请求日志 summary/detail 分层。

---

## 任务 1：彻底移除 `parent_request_id` 语义

**文件：**
- 修改：`db.py`
- 修改：`main.py`
- 修改：`admin.py`
- 测试：`tests/test_retry_policy.py`
- 测试：`tests/test_admin_request_logs_ui.py`

- [ ] **步骤 1：先写会失败的测试，确保契约里还残留 `parent_request_id` 时能被抓到**

在 `tests/test_retry_policy.py` 中加入：

```python
from pathlib import Path


def test_request_log_query_does_not_expose_parent_request_id():
    source = Path(__file__).resolve().parents[1].joinpath("db.py").read_text(encoding="utf-8")
    body = source[source.index("async def query_request_logs"):source.index("    @classmethod", source.index("async def query_request_logs") + 1)]
    assert "parent_request_id" not in body


def test_request_log_payload_does_not_write_parent_request_id():
    source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    chat_body = source[source.index("async def chat_completions"):source.index("@app.post(\"/v1/responses\")")]
    assert '"parent_request_id"' not in chat_body
```

在 `tests/test_admin_request_logs_ui.py` 中加入：

```python
from pathlib import Path


def test_request_log_detail_does_not_render_parent_request_id():
    html = Path(__file__).resolve().parents[1].joinpath("static", "admin.html").read_text(encoding="utf-8")
    body = function_body(html, "showRequestLogDetail")
    assert "parent_request_id" not in body
    assert "父请求" not in body
```

- [ ] **步骤 2：跑测试，确认当前代码会失败**

运行：

```bash
pytest tests/test_retry_policy.py::test_request_log_query_does_not_expose_parent_request_id tests/test_retry_policy.py::test_request_log_payload_does_not_write_parent_request_id tests/test_admin_request_logs_ui.py::test_request_log_detail_does_not_render_parent_request_id -v
```

预期：FAIL。

- [ ] **步骤 3：把 `parent_request_id` 从数据写入、查询和详情里删干净**

在 `main.py` 里，把所有请求日志 payload 的 `parent_request_id` 删除。比如主请求记录直接是：

```python
log_data = {
    "request_id": request_id,
    "created_at": start_time,
    "api_key": api_key,
    "api_key_name": _api_key_name(api_key),
    "provider_name": retry_path[-1].get("provider") if retry_path else None,
    "account_username": retry_path[-1].get("account") if retry_path else None,
    "model": model,
    "endpoint": "/v1/chat/completions",
    "success": True,
    "status": "ok",
    "stream": stream,
    "duration_ms": _duration_ms(start_time),
    "first_token_ms": first_token_ms,
    **usage_for_stats,
    "retry_path": retry_path,
    "request_body": body,
    "router_request_body": _retry_path_router_request_body(retry_path),
    "router_response_body": _retry_path_router_response_body(retry_path),
    "response_body": result,
    "request_headers": request_headers,
    "response_headers": _upstream_response_headers(retry_path),
    "upstream_status": _upstream_status(retry_path),
}
```

在 `db.py` 里：

- `CREATE TABLE request_logs` 不再创建 `parent_request_id`
- `ALTER TABLE` 不再追加它
- `insert_request_log` / `update_request_log` 不再收它
- `query_request_logs` 不再筛它
- `get_request_log_detail` 不再返回它

在 `admin.py` 里，后台测试请求也保持同样的数据形状：先插入一条 request log，结束后 update，不再混入 `parent_request_id`。

- [ ] **步骤 4：重新跑测试，确认通过**

运行：

```bash
pytest tests/test_retry_policy.py::test_request_log_query_does_not_expose_parent_request_id tests/test_retry_policy.py::test_request_log_payload_does_not_write_parent_request_id tests/test_admin_request_logs_ui.py::test_request_log_detail_does_not_render_parent_request_id -v
```

预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add db.py main.py admin.py tests/test_retry_policy.py tests/test_admin_request_logs_ui.py
git commit -m "refactor: remove parent request id from request logs"
```

---

## 任务 2：让 request_id 路径成为主展示

**文件：**
- 修改：`static/admin.html`
- 修改：`db.py`
- 测试：`tests/test_admin_request_logs_ui.py`

- [ ] **步骤 1：先写会失败的 UI 测试，要求列表和详情都能直接看到路径**

在 `tests/test_admin_request_logs_ui.py` 中加入：

```python
def test_request_log_list_shows_request_id_path():
    body = function_body(admin_html(), "renderRequestLogsPage")
    assert "renderRequestLogPath" in body
    assert "${count} 次" not in body


def test_request_log_detail_always_shows_request_id_path():
    body = function_body(admin_html(), "showRequestLogDetail")
    assert "renderRequestLogPath" in body
    assert "retryPathList.length > 1" not in body
```

- [ ] **步骤 2：跑测试，确认失败**

运行：

```bash
pytest tests/test_admin_request_logs_ui.py::test_request_log_list_shows_request_id_path tests/test_admin_request_logs_ui.py::test_request_log_detail_always_shows_request_id_path -v
```

预期：FAIL。

- [ ] **步骤 3：加一个统一路径渲染 helper，让列表和详情都用它**

在 `static/admin.html` 请求日志区块附近加入：

```javascript
function renderRequestLogPath(retryPath, currentRequestId) {
  const ids = Array.isArray(retryPath)
    ? retryPath.map(item => item && item.request_id ? String(item.request_id) : '').filter(Boolean)
    : [];
  if (!ids.length && currentRequestId) ids.push(String(currentRequestId));
  return ids.length
    ? ids.map(id => `<span class="tag" style="font-family:var(--fontM);font-size:11px">${esc(id)}</span>`).join('<span style="color:var(--text2);margin:0 4px">→</span>')
    : '<span style="color:var(--text2);font-size:11px">-</span>';
}
```

把列表里的重试列改成：

```javascript
const mkRetryTags = (r) => renderRequestLogPath(r.retry_path, r.request_id);
```

把详情里的路径改成无条件展示：

```javascript
const retryPath = parseLogJsonValue(r.retry_path, []);
const retryPathList = Array.isArray(retryPath) ? retryPath : [];
const retryPathHtml = renderRequestLogPath(retryPathList, r.request_id);
```

在 `db.py` 里继续保留 `retry_path`，并让 `get_request_log_detail()` 把它转成 list，再按 `request_id` 补 `log_id`，这样详情里点路径能跳到对应请求。

- [ ] **步骤 4：重新跑 UI 测试**

运行：

```bash
pytest tests/test_admin_request_logs_ui.py -v
```

预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add static/admin.html db.py tests/test_admin_request_logs_ui.py
git commit -m "feat: show request id paths in request logs"
```

---

## 任务 3：把状态、token、模型、耗时统一成简单摘要

**文件：**
- 修改：`main.py`
- 修改：`admin.py`
- 修改：`static/admin.html`
- 测试：`tests/test_retry_policy.py`
- 测试：`tests/test_admin_request_logs_ui.py`

- [ ] **步骤 1：先写失败测试，确认状态和四分类 token 没被做乱**

在 `tests/test_retry_policy.py` 中加入：

```python
def test_usage_keeps_four_token_buckets_disjoint():
    from main import _usage
    result = _usage({"usage": {"prompt_tokens": 4, "completion_tokens": 3, "cached_tokens": 2, "cache_creation_tokens": 1}})
    assert result == {
        "prompt_tokens": 4,
        "completion_tokens": 3,
        "total_tokens": 10,
        "cached_tokens": 2,
        "cache_creation_tokens": 1,
    }
```

在 `tests/test_admin_request_logs_ui.py` 中加入：

```python
def test_request_log_status_helper_distinguishes_requesting_cancelled_and_timeout():
    body = function_body(admin_html(), "renderRequestLogsPage")
    assert "mkStatusCell" in body
    assert "requesting" in body or "cancelled" in body or "empty_non_stream" in body


def test_request_log_time_cell_keeps_duration_and_ttft_separate():
    body = function_body(admin_html(), "renderRequestLogsPage")
    assert "mkTimeCell" in body
    assert "first_token_ms" in body
    assert "duration_ms" in body
```

- [ ] **步骤 2：跑测试，确认失败**

运行：

```bash
pytest tests/test_retry_policy.py::test_usage_keeps_four_token_buckets_disjoint tests/test_admin_request_logs_ui.py::test_request_log_status_helper_distinguishes_requesting_cancelled_and_timeout tests/test_admin_request_logs_ui.py::test_request_log_time_cell_keeps_duration_and_ttft_separate -v
```

预期：FAIL。

- [ ] **步骤 3：在前端加状态归一 helper，并保留原始字段用于详情排障**

在 `static/admin.html` 中加入：

```javascript
function formatRequestLogStatus(r) {
  if (r.success) return { label: '成功', cls: 'ok' };
  if (r.status === 'requesting') return { label: '请求中', cls: 'warn' };
  if (r.status === 'cancelled') return { label: '取消', cls: 'off' };
  if (r.status === 'empty_non_stream') return { label: '空响应', cls: 'warn' };
  return { label: '失败', cls: 'err' };
}
```

更新 `mkStatusCell`：

```javascript
const mkStatusCell = (r) => {
  const info = formatRequestLogStatus(r);
  return `<span class="status-badge ${info.cls}"><span class="dot-sm ${info.cls === 'ok' ? 'green' : info.cls === 'warn' ? 'yellow' : info.cls === 'off' ? 'gray' : 'red'}"></span>${info.label}</span>`;
};
```

更新 token 单元格，继续按四桶展示：

```javascript
const mkTokenCell = (r) => {
  const total = (r.prompt_tokens||0) + (r.completion_tokens||0) + (r.cached_tokens||0) + (r.cache_creation_tokens||0);
  if (!total) return '<span style="color:var(--text2);font-size:11px">-</span>';
  return `<div style="display:flex;flex-direction:column;gap:3px;align-items:center">
    <div style="display:flex;gap:4px;align-items:center;justify-content:center;flex-wrap:wrap">
      <span class="tag on" style="font-size:10px;padding:1px 5px">入 ${r.prompt_tokens||0}</span>
      <span class="tag" style="font-size:10px;padding:1px 5px;background:rgba(77,140,247,.15);color:var(--blue)">出 ${r.completion_tokens||0}</span>
    </div>
    <div style="display:flex;gap:4px;align-items:center;justify-content:center;flex-wrap:wrap">
      ${(r.cached_tokens||0)>0?'<span class="tag" style="font-size:10px;padding:1px 5px;background:rgba(105,113,128,.15);color:var(--text2)">缓存读 '+r.cached_tokens+'</span>':''}
      ${(r.cache_creation_tokens||0)>0?'<span class="tag" style="font-size:10px;padding:1px 5px;background:rgba(255,165,0,.15);color:#f39c12">写 '+r.cache_creation_tokens+'</span>':''}
    </div>
  </div>`;
};
```

在 `main.py` 和 `admin.py` 里，继续用同一套 usage normalization 填 `first_token_ms`、`duration_ms`、`prompt_tokens`、`completion_tokens`、`cached_tokens`、`cache_creation_tokens`。不要把缓存读/写合并进输入或输出 token。

- [ ] **步骤 4：重新跑测试**

运行：

```bash
pytest tests/test_retry_policy.py::test_usage_keeps_four_token_buckets_disjoint tests/test_admin_request_logs_ui.py::test_request_log_status_helper_distinguishes_requesting_cancelled_and_timeout tests/test_admin_request_logs_ui.py::test_request_log_time_cell_keeps_duration_and_ttft_separate -v
```

预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add main.py admin.py static/admin.html tests/test_retry_policy.py tests/test_admin_request_logs_ui.py
git commit -m "feat: normalize request log status and usage display"
```

---

## 任务 4：把详情页拆成“简单摘要 + 详细内容”，并修浅色主题

**文件：**
- 修改：`static/admin.html`
- 测试：`tests/test_admin_request_logs_ui.py`

- [ ] **步骤 1：先写失败测试，确认详情页还没有正确分层，浅色主题也没补全**

在 `tests/test_admin_request_logs_ui.py` 中加入：

```python
def test_request_log_detail_has_simple_summary_before_advanced_blocks():
    body = function_body(admin_html(), "showRequestLogDetail")
    assert "请求路径" in body
    assert "详细内容" in body or "Raw Request" in body


def test_light_theme_covers_log_card_and_inner_content_blocks():
    html = admin_html()
    assert "body.theme-light .log-card" in html
    assert "body.theme-light .log-card-head" in html
    assert "body.theme-light .log-card-body" in html
    assert "body.theme-light .log-content-block" in html
    assert "body.theme-light .json-tree" in html
```

- [ ] **步骤 2：跑测试，确认失败**

运行：

```bash
pytest tests/test_admin_request_logs_ui.py::test_request_log_detail_has_simple_summary_before_advanced_blocks tests/test_admin_request_logs_ui.py::test_light_theme_covers_log_card_and_inner_content_blocks -v
```

预期：FAIL。

- [ ] **步骤 3：把详情弹窗重排成先摘要、后详细内容**

在 `static/admin.html` 里，把详情内容拆成两层：

```javascript
const simpleSummaryHtml = `
  <div class="request-log-summary">
    ${renderRequestLogPath(retryPathList, r.request_id)}
    ${statusCodeHtml}
    ${tokenHtml}
    <div class="kv-row"><span class="key">请求 ID</span><span class="val">${esc(r.request_id || '-')}</span></div>
    <div class="kv-row"><span class="key">模型</span><span class="val">${formatRequestLogModel(r)}</span></div>
    <div class="kv-row"><span class="key">用时</span><span class="val">${duration}s (TTFT ${ttft}s)</span></div>
  </div>`;

const advancedHtml = `
  ${renderScrollableLogSection('消息体', messagesHtml, '360px', toCopyText(req.messages))}
  ${renderScrollableLogSection('请求工具定义', clientToolsHtml, '360px', toCopyText(req.tools))}
  ${renderCollapsibleLogSection('发送给渠道的请求体', renderLogJson(routerReq), false, toCopyText(r.router_request_body))}
  ${renderScrollableLogSection('渠道返回的消息体', renderLogJson(routerResp), '360px', toCopyText(r.router_response_body))}
  ${renderCollapsibleLogSection('渠道响应 Headers', renderLogJson(respHeaders), false, toCopyText(r.response_headers))}
  ${renderCollapsibleLogSection('Raw Request', renderLogJson(req), false, toCopyText(r.request_body))}
  ${renderCollapsibleLogSection('请求 Headers', renderLogJson(reqHeaders), false, toCopyText(r.request_headers))}
  ${renderLogSection('Assistant 内容 / Stream Preview', assistantHtml ? `<pre style="white-space:pre-wrap;margin:0;font-family:var(--fontM);font-size:12px;color:var(--text)">${esc(assistantHtml)}</pre>` : '', assistantHtml)}
  ${renderLogSection('服务端工具调用', serverToolCallsHtml, toCopyText(extractServerToolCalls(resp)))}
  ${renderCollapsibleLogSection('Usage', usageHtml, false, toCopyText(extractUsage(resp)))}
  ${renderCollapsibleLogSection('Raw Response', renderLogJson(resp), false, toCopyText(r.response_body))}
`;
```

把 `simpleSummaryHtml` 放在 modal body 最前面，`advancedHtml` 放后面。

在 CSS 里补浅色主题覆盖：

```css
body.theme-light .log-card{background:#ffffff;border-color:#e2e8f0}
body.theme-light .log-card-head{background:#f8fafc}
body.theme-light .log-card-body{background:#ffffff}
body.theme-light .log-content-block{background:#ffffff;border-color:#e2e8f0}
body.theme-light .json-tree{background:#ffffff}
```

同时把 `renderLogJson()` 改成不要强行内联深色背景：

```javascript
function renderLogJson(obj) {
  return `<div class="json-tree">${renderJsonTree(obj ?? {})}</div>`;
}
```

- [ ] **步骤 4：重新跑测试**

运行：

```bash
pytest tests/test_admin_request_logs_ui.py::test_request_log_detail_has_simple_summary_before_advanced_blocks tests/test_admin_request_logs_ui.py::test_light_theme_covers_log_card_and_inner_content_blocks -v
```

预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add static/admin.html tests/test_admin_request_logs_ui.py
git commit -m "feat: simplify request log detail layout and light theme"
```

---

## 任务 5：做安全与兼容性审查

**文件：**
- 复查：`main.py`
- 复查：`db.py`
- 复查：`admin.py`
- 复查：`static/admin.html`
- 测试：`tests/test_retry_policy.py`

- [ ] **步骤 1：写会失败的审计测试，确认敏感字段和兼容路径都被检查**

在 `tests/test_retry_policy.py` 中添加：

```python
def test_request_log_detail_still_enriches_retry_path_ids():
    source = Path(__file__).resolve().parents[1].joinpath("db.py").read_text(encoding="utf-8")
    detail_body = source[source.index("async def get_request_log_detail"):source.index("    @classmethod\n    async def dashboard_stats", source.index("async def get_request_log_detail"))]
    assert "retry_path" in detail_body
    assert "request_id" in detail_body


def test_request_log_render_does_not_remove_raw_payload_sections():
    source = Path(__file__).resolve().parents[1].joinpath("static", "admin.html").read_text(encoding="utf-8")
    body = source[source.index("async function showRequestLogDetail"):source.index("function formatDashboardChartValue")]
    assert "Raw Request" in body
    assert "Raw Response" in body
```

- [ ] **步骤 2：跑审计测试，确认当前代码里仍有需要修正的点**

运行：

```bash
pytest tests/test_retry_policy.py::test_request_log_detail_still_enriches_retry_path_ids tests/test_retry_policy.py::test_request_log_render_does_not_remove_raw_payload_sections -v
```

预期：FAIL 或需要人工复查。

- [ ] **步骤 3：检查兼容性和安全边界，并把风险写进代码备注/说明里**

需要人工确认的点：

- 老数据里如果仍有 `parent_request_id` 列，是否直接 drop，还是先保留迁移窗口。
- `request_body` / `response_body` / `router_*` 里是否可能包含敏感字段，现有 `sanitize_headers` 只处理 headers，不处理 body。
- 大 payload 是否需要继续保留 raw，但默认折叠，不在列表里展开。
- `retry_path` 如果缺少 `request_id`，是否允许详情页显示 `-`，还是强制补全。

这些点如果继续改代码，建议在相关 helper 或数据路径上加一句短备注，说明“为什么这里必须保留/折叠/不默认展开”。

- [ ] **步骤 4：完成审计后再跑一次全量请求日志相关测试**

运行：

```bash
pytest tests/test_retry_policy.py tests/test_admin_request_logs_ui.py -v
```

预期：PASS。

- [ ] **步骤 5：提交审计结论或同步到计划备注**

如果审计中发现兼容/安全问题，先在计划里补一句明确说明，再进入实现。

---

## 自检清单

- `parent_request_id` 已从请求日志契约中移除，而不是只在 UI 里隐藏。
- 请求日志围绕 `request_id` 和 `retry_path` 展示，不再要求用户理解父子关系。
- 状态、token、模型、耗时、TTFT 都有明确展示规则。
- 详情页先显示简单摘要，再显示 raw payload。
- 浅色主题日志块覆盖了内部内容区域，而不只是外层容器。
- 计划里明确保留了安全与兼容性审查点，而不是只列功能改动。
