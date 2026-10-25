# 中转站详情页优化实施计划

> 目标：按用户提出的 8 项需求，将中转站详情页从当前列表跳转模式升级为独立 URL 模式，并优化 UI 展示、账号/API Key 操作、余额同步和远端导入。

## 架构
- 前端：在现有 `static/admin.html` 中增加独立路由 `#relay-site/:id`，保持与现有 `navTo` 路由兼容；表格样式和交互优化集中在现有 relay UI 函数中。
- 后端：在现有 `admin.py` 和 `relay_admin.py` 中扩展远端 API 操作（删除远端 API Key、同步 API Key 列表），并调整验证接口为同步余额。
- 测试：在 `tests/test_admin_transit_station.py` 中增加对新 UI 和 API 行为的断言。

## 任务列表（按执行顺序）

### 任务1：前端独立 URL 和详情页入口
**文件：** `static/admin.html`（`renderRelaySitesPage`、`renderRelaySiteDetail`、路由表）
**步骤：**
- 在 `STATIC_PAGES` 增加 `relay-site` 页面（不依赖导航，接受 ID 参数）。
- `renderRelaySiteDetail` 支持直接通过 URL 参数（如 `?page=relay-site&id=123`）加载站点。
- 列表页“详情”按钮改为 `navTo('relay-site', site.id)`。
- 验证：列表页点击“详情”后，URL 变为 `#relay-site/123`，刷新页面后仍显示该详情。

### 任务2：表格表头居中 + 账号操作按钮 icon + 紧凑布局
**文件：** `static/admin.html`（`loadRelayAccounts`、`renderRelaySiteDetail` 的表格样式）
**步骤：**
- 在 relay 账号列表和 API Key 列表的 `<thead>` 中给所有 `th` 增加 `style="text-align:center"`。
- 将账号操作区的文本按钮改为带 icon 的按钮（`🔄`、`✅`、`💰`、`✏️`、`🗑️`），用 `gap:4px` 的 `display:flex` 包裹。
- 验证：admin UI 测试中检查 `text-align:center` 存在且操作按钮使用 icon。

### 任务3：访问令牌和 user_id 支持查看（脱敏+查看）
**文件：** `static/admin.html`（`openRelayAccountModal`、`loadRelayAccounts` 账号行）
**步骤：**
- 在账号编辑弹窗中，给 `access_token` 和 `user_id` 输入框增加脱敏显示（默认 `mask` 如 `****`）。
- 增加“查看”按钮，点击后临时显示真实值，或弹窗显示。
- 验证：测试中确认弹窗中有 `access_token` / `user_id` 的查看按钮。

### 任务4：删除账号操作区“创建 API Key”和“录入 API Key”
**文件：** `static/admin.html`（`loadRelayAccounts`）
**步骤：**
- 从账号操作区移除 `openRelayCreateKeyForAccount` 和 `openRelayApiKeyModal` 按钮。
- 验证：UI 测试中确认账号操作区不再包含这两个按钮。

### 任务5：账号编辑弹窗增加“更新账户名”按钮
**文件：** `static/admin.html`（`openRelayAccountModal`）
**步骤：**
- 在弹窗中增加“更新账户名”按钮。
- 点击后调用 `/admin/relay-sites/{siteId}/accounts/{accountId}/validate`。
- 从验证结果中提取 `name`（或 fallback `username`），回填到 `raName` 输入框。
- 验证：UI 测试中确认按钮存在，后端测试中确认验证返回结果包含账户名。

### 任务6：验证账号时自动同步余额
**文件：** `admin.py`（`/relay-sites/{siteId}/accounts/{accountId}/validate`）
**步骤：**
- 在 validate 成功后，立即调用 `relayAccountBalance` 流程（获取余额并保存到账号结果）。
- 确保余额数据写入 `balance` 字段。
- 验证：后端测试中确认验证接口返回结果同时包含 `validate` 和 `balance` 信息。

### 任务7：API Key 表格增加同步和创建按钮
**文件：** `static/admin.html`（`loadRelayDetailKeys`）、`admin.py`（新增 `/relay-sites/{id}/sync-api-keys` 和 `/relay-sites/{id}/relay-api-keys/{relay_token_id}`）
**步骤：**
- 新增后端接口 `POST /relay-sites/{siteId}/sync-api-keys`：
  - 调用 `NewApiRelayClient.list_tokens()` 或等效远端接口获取所有 token。
  - 对每个 token：upsert 到本地 `relay_site_api_keys` 表（保留 `relay_token_id`、`name`、`key_preview` 等，即使无 `api_key`）。
- 修改 `loadRelayDetailKeys`：
  - 表头增加“同步”和“创建 API Key”按钮。
  - 同步按钮调用 `POST /admin/relay-sites/{siteId}/sync-api-keys`。
  - 创建 API Key 按钮打开弹窗，弹窗自动调用 `/relay-sites/{siteId}/groups` 获取分组列表。
- 验证：API 测试确认同步后本地表有新增记录；UI 测试确认同步按钮和创建按钮存在。

### 任务8：API Key 删除需先删除远端
**文件：** `static/admin.html`（`loadRelayDetailKeys`）、`admin.py`（`/relay-sites/{siteId}/relay-api-keys/{relay_token_id}` 的 DELETE 方法）
**步骤：**
- 修改后端 DELETE 端点：先调用 `NewApiRelayClient.delete_api_key(relay_token_id)`，成功后再删除本地 `relay_site_api_keys` 记录。
- 修改前端：确认按钮增加 `confirm` 提示，提示包含“将先删除远端 API Key”的文案。
- 验证：后端测试 mock 远端删除后确认本地删除；UI 测试确认 confirm 文案。

### 任务9：API Key 创建弹窗只保留账号/名称/分组
**文件：** `static/admin.html`（`openRelayCreateKeyForAccount`）
**步骤：**
- 移除弹窗中的 `quota`、`expired_time` 等额外字段。
- 打开弹窗时自动调用 `/relay-sites/{siteId}/groups` 获取分组列表并填充下拉框。
- 提交时只传 `account_id`、`name`、`group`。
- 验证：UI 测试确认弹窗只有 3 个字段，且分组自动加载。

### 任务10：整体回归验证
**文件：** `tests/test_admin_transit_station.py`
**步骤：**
- 更新或新增测试用例覆盖所有新增/修改的功能点。
- 运行 `pytest tests/test_admin_transit_station.py -v`，确保全部通过。
- 运行 `pytest tests/ -k relay -v`，确保无回归。

---

## 执行方式

- 前端任务（任务1、2、3、4、5、7、9）之间大部分无强依赖，可以并行。
- 后端任务（任务6、7、8）与前端任务可并行，但需确保 API 端点存在后前端才可测试。
- 任务10（测试）最后执行。
