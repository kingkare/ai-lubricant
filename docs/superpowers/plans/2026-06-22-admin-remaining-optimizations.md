# 管理端剩余优化实施方案（2026-06-22）

承接上一轮（已完成：渠道 ID 体系、上游模型失败改 alert）。本轮做完所有剩余项。

决策（已与用户确认推荐方案）：
- 操练场鉴权：后端支持 x-api-key 且 **x-api-key 优先于登录 Bearer**。
- 渠道编辑弹框复用：抽成共享组件 `src/components/ProviderEditModal/`。

---

## A. 渠道页 Channels.tsx（任务4）

文件：`web/admin-frontend/src/pages/Channels/Channels.tsx`

1. **编辑弹框模型列表**：当前已是内联表格，无需改。
2. **复制按钮改草稿**：`handleCopy` 不再直接 `createCustomProvider`；改为 `getProviderDetail` 后把完整配置存入 `copyPreset` 状态并打开新增弹框，用户点"创建"才落库。扩展 `CreateCustomProviderModal` 的 `presetData` 结构覆盖更多字段。
3. **tab 重排 + 改名 + 条件隐藏**：把 `account_priority`/`account_weight`/`minimal_model_params`/`client_preset` 从"渠道配置"移到"基础配置"；"渠道配置" → "协议"；非自定义渠道（`!isCustom`）不显示"协议"tab。
4. **先详情后打开**：`loadData` 拆两阶段——先 `getProviderDetail` 设 `detail` 并初始化编辑态立即渲染；models 与 limit-policy 并行加载各自 loading；渲染 gate 从 `!detail || !limitPolicy` 改为只 gate `!detail`。
5. **新增弹框与编辑弹框统一**：`CreateCustomProviderModal` 改为 tab 布局；`QuickAddModal` 不变。
6. **搜索与筛选**：搜索框宽度 220→360；`statusFilter` 增加 `frozen`/`authenticated`/`unauthenticated`。

## B. 共享渠道编辑弹框

- 先 `export` 现有 `UnifiedProviderModal`，RequestLogs 复用；确认通过后再物理抽离到 `components/ProviderEditModal/`（可选）。

## C. AgentChat（任务5）

1. 顶部 actions 增加"设置"按钮 → 弹框内嵌 Agent 管理；背景改用 CSS 变量与全站一致。
2. 首次进入空白草稿态；`handleNewConversation` 仅切草稿态不调接口；`handleSend` 在 `!activeConvId || isDraft` 时先 `createConversation` 再发送，用局部 `convId`。

## D. 模型广场 ModelMetadata（任务6）

1. 其他参数：移除多余 `expandedOtherParams` 按钮，直接用 `OtherParams` 内置 `<details>`。
2. 卡片按钮一排：`renderActions` 容器 `flexWrap: wrap`→`nowrap`。
3. max_tokens：双 range 之间加可视连接轨道。
4. 统计移入列表表头：`StatGrid` 移到"模型列表" SectionCard。
5. 默认元数据移入全局配置弹框；移除顶部"默认元数据"按钮。
6. 新增/刷新元数据移入列表表头 toolbar。

## E. 其他修复（任务7）

1. **操练场 x-api-key**（`main.py`）：`_extract_api_key` 在 `support_x_api_key=True` 且带 x-api-key 时 x-api-key 优先；给 `/v1/chat/completions`(1769)、`/v1/responses`(2497)、`/v1/images/generations`(3531)、`/v1/audio/speech`(3575) 传 `support_x_api_key=True`。
2. **McpMarket 崩溃**：`listMcpServices` 归一化为数组；`filtered` 加数组守卫。
3. **请求日志点击渠道**：渠道/账号列加 `cursor:pointer`+蓝色下划线，点击用 `providers.find(p => p.id === log.provider_name)` 匹配后打开共享渠道编辑弹框。

---

## 实施顺序
1. 后端 main.py（E1）
2. McpMarket（E2）
3. ModelMetadata（D）
4. Channels.tsx（A）+ 导出 UnifiedProviderModal
5. RequestLogs 复用弹框（E3）
6. AgentChat（C）
7. `npm run build` 校验

## 风险
- Channels.tsx 体量大，拆 loadData 需保证 handleSave 在 limitPolicy 未加载时不误清空策略。
- 共享弹框先导出复用，避免大迁移回归。
- x-api-key 优先级仅影响 support_x_api_key=True 的端点。
