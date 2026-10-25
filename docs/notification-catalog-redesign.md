# 通知分类与通知类型重构设计

> 事实源。改动 `user_platform/notify_service.py`、`notify_core.py`、`notify_dispatch.py`、
> 触发点、前端 `NotificationEvents.tsx` / 通知列表页时，以本文为准。

## 1. 现状与问题

通知体系三层（`docs/` 无专文，代码即事实源）：

```
事件目录 NOTIFY_EVENT_TYPES（notify_service.py:49）
  → emit_notification(event_type, params, envelope)（notify_core.py:630）
    → mc_notify_outbox 一行  → worker 消费（_consume_one）
      → 按 mc_notify_events 事件定义 + 渠道绑定分发
        → notify_center 写 notifications 表 / mobile_push 推 Expo / webhook POST
```

目录每项只有三个字段：`type` / `name` / `category` / `owner_scope`。其中：

- `type` = 具体事件类型（`account.frozen`、`task.ended`…）
- `category` = 一级分类，同时**兼任两件事**：前端 `CATEGORY_LABELS` 的中文分组标签，
  以及 `PARAM_FIELDS`（ NotificationEvents.tsx:75 ）选事件参数筛选维度的键。
- `owner_scope` = `platform`（管理端专属）/ `user`（用户侧可配）

问题：

1. **分类与用户认知的业务域不对齐。** 用户列出的业务域是：供应商、任务、agent、设备控制、
   执行节点、用户相关、账号登陆、密钥、自定义。当前目录的 `category` 取值是
   `account / channel / api_key / model / node / security / task / system / ddns`——
   `channel`（供应商）与 `account`（账号）混用、`node` 与执行节点、没有 agent、没有设备控制、
   没有用户/登录。
2. **大量需求事件无触发源**（探索结论：设备上下线、节点上线/审批、agent 等待输入/异常、
   用户登录均只声明或完全没有，只有状态变化点没有 emit）。
3. **部分事件有状态变化但走的是不发通知的路径**：`_apply_account_state`
   （rate_limiter.py:1641，语义冻结：额度耗尽/凭证失效）不经过 `_notify_freeze`
   （rate_limiter.py:1817），因此 `quota_exhausted` / `credential_invalid` 不发通知。
4. `emit_notification` 的 `kind` 参数默认 `event_type.split(".")[0]`，与 `category` 是
   两套并存的值，历史 emit 点各写各的（有写 `account` 也有漏写的），落到
   `notifications.kind` 列后前端无法按分类筛。

## 2. 目标模型：分类 × 类型 两维

**分类（category）** = 业务域，稳定、少量、与前端 `CATEGORY_LABELS` 一一对应。
**类型（type）** = 分类下的具体事件。

分类采用用户的业务域口径，共 10 个：

| category | 中文 | owner_scope | 说明 |
|---|---|---|---|
| `account` | 账号 | platform | 供应商下的账号状态迁移 |
| `channel` | 供应商 | platform | 供应商本体（渠道）状态 |
| `model` | 模型 | platform | 供应商模型目录变更 |
| `api_key` | 密钥 | platform | API Key |
| `security` | 安全 | platform | 安全扫描拦截 |
| `node` | 执行节点 | user | 执行节点（node_server）生命周期 |
| `device` | 设备控制 | user | 受控手机（android/ios）上下线 |
| `task` | 任务 | user | 任务生命周期 |
| `agent` | agent | user | agent 会话状态 |
| `user` | 用户 | platform | 用户账号/登录 |
| `system` | 系统 | platform | 系统运维（日志清理等） |

> `custom`（自定义）不预置：用户自己创建事件时 `category` 由所选 event_type 反推，
> 表单已有「事件名称」自由填写。若确需让用户自建事件类型，见 §7 待定项。

## 3. 事件类型全表

### 3.1 账号（account, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `account.created` | 账号新增 | server/admin.py:7681 | ①已有 |
| `account.frozen` | 账号已冻结 | rate_limiter.py:1884 | ①已有 |
| `account.unfrozen` | 账号已解冻 | — | ③无触发点（新增埋点） |
| `account.init_failed` | 账号初始化失败 | rate_limiter.py:2020 | ①已有 |
| `account.logged_out` | 账号已退登 | rate_limiter.py:2106 | ①已有 |
| `account.token_expired` | 账号 Token 失效 | admin.py `_poll_one_auth_task` expired 分支 + `account.state==credential_invalid` | ②需埋（拆自冻结） |
| `account.quota_exhausted` | 账号额度耗尽 | `_apply_account_state` category=quota | ②需埋 |
| `account.permanently_frozen` | 账号永久冻结 | `_notify_freeze` permanent=True | ①已有（用 `account.frozen` + params.permanent 区分） |
| `account.disabled` | 账号被禁用 | `_apply_disable` account 分支 | ①已有（`account.frozen` + params.disabled） |
| `account.daily_unavailable` | 账号当日不可用 | 上游每日配额类错误 | ③需确认语义（见 §7） |
| `account.deleted` | 账号已删除 | server/admin.py:7781 | ②需埋 |
| `account.expired` | 账号已过期 | credential `expires_at` 到期 | ③需确认（见 §7） |

**决策：账号类不拆成 6 个新事件，而是把「额度耗尽 / token 失效 / 永久冻结 / 禁用」做成
`account.state_changed` 下的 `state` 参数取值。** 理由：前端筛选维度是 `category`
（账号），具体差异在通知列表的 `message`/`metadata` 里已可读；拆成 12 个 type 会让
事件目录膨胀且多数无独立订阅价值。但用户显式列出了这几项，故**保留独立 type**，
见 §7 待确认项 1。

### 3.2 供应商（channel, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `channel.created` | 渠道已创建 | admin.py:7039 | ①已有 |
| `channel.frozen` | 渠道已冻结 | rate_limiter.py:1868 | ①已有 |
| `channel.disabled` | 渠道被禁用 | `_apply_disable` channel 分支 + admin.py:7339 `toggle_provider` | ①部分（冻结路径有，手动停用没有） |
| `channel.deleted` | 渠道已删除 | 删除渠道路由 | ③需埋 |

### 3.3 模型（model, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `model.new` | 新增模型 | rate_limiter.py:4022 `to_add` 非空 | ①已有 |
| `model.removed` | 模型被删除 | 同处 `to_remove` 非空 | ②需埋（用户本次新增要求） |

> 现有 `model.new` 在一个 emit 里同时报 added/updated/removed 三个列表。改为：
> `to_add` 非空 → `model.new`；`to_remove` 非空 → `model.removed`。两次 emit 各自带
> 自己的列表，避免「一条通知说什么都发生了」。`to_update`（改名）并入 `model.new`
> 的 params（updated 字段），不单独立项。

### 3.4 密钥（api_key, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `api_key.created` | 密钥已创建 | admin.py:3478 | ①已有 |
| `api_key.modified` | 密钥已修改 | admin.py:3508 | ①已有 |
| `api_key.usage_threshold` | 密钥用量达阈值 | main.py:2203 | ①已有 |
| `api_key.security_intercepted` | 安全拦截 | security/event_log.py:83 | ①已有但归在 `security`，见下 |
| `api_key.deleted` | 密钥已删除 | 删除 key 路由 | ②需埋 |

> 用户把「安全拦截」列在密钥分类下。但安全拦截的归因主体可能是匿名请求（无 key），
> 且语义更偏 `security`。**保持 `security.intercepted`（security 分类）不变**，
> 在 `api_key` 分类下不重复造。见 §7 待确认项 2。

### 3.5 安全（security, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `security.warning` | 安全告警 | event_log.py:83 | ①已有（更名 `security.intercepted`） |

> 原 `security.warning` 名字泛化。它是**请求被安全扫描拦截**的唯一出口，
> 改名 `security.intercepted` 让语义与用户的「安全拦截」对齐；旧 type 保留为别名
> （目录里保留一行 `security.warning` 标记 deprecated，历史订阅不失效）。

### 3.6 执行节点（node, user）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `node.online` | 节点已上线 | node_server `registry.register` | ②需埋（跨进程，走 `enqueue_notification`） |
| `node.offline` | 节点已离线 | `registry.unregister` / `reap_stale` | ②需埋 |
| `node.pending_approval` | 节点待审批 | `_mint_credentialed_node` status=PENDING | ②需埋 |
| `node.approved` | 节点已通过审批 | `approve_node` | ②需埋（新增，用户未列但配套） |
| `node.new_version` | 节点有新版本 | release_catalog | ①已有 |

### 3.7 设备控制（device, user）

android/ios 分开是**参数维度**（`platform`），不是两个 type——否则 android/ios
两个目录项除了名字完全一样，前端筛选还得建两套。

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `device.online` | 手机已上线 | mcp_runtime sse_gateway `driver.register` / ios 对账 present→True | ②需埋 |
| `device.offline` | 手机已离线 | `driver.unregister` / `reap_stale` / ios present→False | ②需埋 |

params 必带 `platform` ∈ {android, ios} + `device_id` + `device_name`。

### 3.8 任务（task, user）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `task.created` | 任务已创建 | task_service.py:1675 | ①已有 |
| `task.ended` | 任务已结束 | node_server/task_finalize.py:100 | ①已有 |
| `task.paused` | 任务已暂停 | task_service.py:3473 | ①已有 |
| `task.failed` | 任务失败 | — | ③无触发点（task.ended 已带 result） |
| `task.idle` | 任务停留 | — | ③无触发点 |
| `task.deleted` | 任务已删除 | 删除任务 | ②需埋 |
| `task.scheduled_failed` | 定时任务异常 | — | ③无触发点 |
| `task.waiting_input` | 等待用户输入 | agent 会话 `question` → `STATUS_WAITING_INPUT` | ②需埋（与 agent 共触发点） |
| `task.completed` | 已完成 | — | 与 task.ended 重复，不立项 |

> 用户列的「任务：等待用户输入、已完成、异常」映射为 `task.waiting_input` /
> `task.ended`（result=成功）/ `task.ended`（result=失败，severity=warn）。
> **不新增 `task.completed`/`task.failed`**：终态已由 `task.ended` 一个事件 + `status`
> 参数表达，拆开只会让同一终态发两条。

### 3.9 agent（agent, user）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `agent.waiting_input` | 等待用户输入 | run_state.py `question`/`done` → `STATUS_WAITING_INPUT` | ②需埋 |
| `agent.waiting_approval` | 等待审批 | `confirmation_required` → `STATUS_WAITING_APPROVAL` | ②需埋 |
| `agent.error` | 执行异常 | `error` → `STATUS_ERROR` | ②需埋 |
| `agent.aborted` | 已中止 | `aborted` → `STATUS_ABORTED` | ②需埋（用户未列，配套） |

> agent 与 task 共用会话状态机，但按用户的分类要求列成独立分类。
> **同一触发点按 owner 分别 emit**：会话归属 user（`agent_conversations.user_id`）
> 时发 `agent.*` 与 `task.*` 各一条？不——**只发 `agent.*`，task.* 仅用于任务域
> 生命周期（创建/暂停/结束/删除）**，避免一次状态变化两条通知轰炸。
> 即 `task.waiting_input` 不立项，等待输入只发 `agent.waiting_input`。

**修正 §3.8：删除 `task.waiting_input`。**

### 3.10 用户（user, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `user.login_succeeded` | 登录成功 | auth_service.password_login / OIDC callback | ②需埋 |
| `user.login_failed` | 登录失败 | password_login ValueError invalid_credentials | ②需埋 |
| `user.registered` | 用户注册 | routes.py register | ②需埋 |
| `user.blocked` | 用户被封禁 | 管理端封禁 | ③需埋 |

### 3.11 系统（system, platform）

| type | name | 触发点 | 状态 |
|---|---|---|---|
| `log.cleanup_done` | 日志清理完成 | rate_limiter.py:3116 | ①已有 |
| `log.cleanup_failed` | 日志清理失败 | 同 | ①已有 |
| `node.new_version` | 节点有新版本 | release catalog | ①已有，**归错类**，见下 |

> `node.new_version` 现在 category=`node`/platform。它同时服务「管理端升级提示」。
> 保持 `node` 分类、`owner_scope=user` 会让它只在用户侧出现。**改为 platform
> 且 category 单独设 `system`？** 见 §7 待确认项 3。倾向：保留在 `node` 分类但
> `owner_scope=platform`（版本发布是管理端动作）。

## 4. 数据迁移

`notifications` 表无 `category` 列，分类只存在于 `mc_notify_events.category`
（用户配置的事件实体）与目录。历史 `notifications.kind` 值散乱
（account/system/model/security/task…）。

**不做数据迁移写库。** 改为：读侧（`query_notifications` 返回的 kind）在
`_notification_row` / API 层按新目录归一：`kind = event_type.split('.')[0]`，
对老 kind 值做一次白名单映射。这样历史行的显示分类自动对齐新域，无需回填。

## 5. 触发点落地清单

| # | 文件:行 | 改什么 | emit 什么 |
|---|---|---|---|
| 1 | server/rate_limiter.py `_apply_account_state` | 补 emit（category=quota/credential 时） | `account.quota_exhausted` / `account.token_expired` |
| 2 | server/rate_limiter.py `_apply_disable` account 分支 | 已有冻结通知，补禁用专属 | `account.disabled` |
| 3 | server/admin.py `toggle_provider` | 停用时 emit | `channel.disabled` |
| 4 | server/rate_limiter.py:4022 模型同步 | 拆成两条 emit | `model.new` / `model.removed` |
| 5 | node_server `registry.register/unregister/reap_stale` | 上线/离线 emit | `node.online` / `node.offline` |
| 6 | node_server/service.py `_mint_credentialed_node` | pending emit | `node.pending_approval` |
| 7 | node_server/service.py `approve_node` | approved emit | `node.approved` |
| 8 | mcp_builtin/device_control/driver.py register/unregister/reap_stale | 上下线 emit | `device.online` / `device.offline` |
| 9 | user_platform/ios_device_reconcile.py present 翻转 | 同上（iOS） | `device.online` / `device.offline` |
| 10 | agent/run_state.py `apply` | question/error/aborted/confirmation emit | `agent.waiting_input` / `agent.error` / `agent.aborted` / `agent.waiting_approval` |
| 11 | user_platform/auth_service.py password_login | 成功/失败 emit | `user.login_succeeded` / `user.login_failed` |
| 12 | routes.py register | emit | `user.registered` |
| 13 | server/admin.py 删账号 / 删渠道 / 删 key | emit | `account.deleted` / `channel.deleted` / `api_key.deleted` |

跨进程约束：node_server 与 mcp_runtime 不能 import 数据服务的
`emit_notification`。node_server 用 `shared_store.enqueue_notification`
（已有，形状一致）；mcp_runtime 的设备驱动用**进程内 emit 转发**：
`device.*` 事件由 mcp_runtime 进程通过 Tortoise `user_platform` 连接的
outbox INSERT 发出（`emit_notification` 已支持该路径，见 notify_core.py:690）。
iOS 对账在 user_platform 进程，直接 `emit_notification_background`。

## 6. 前端

1. `NotificationEvents.tsx` `CATEGORY_LABELS` 补齐 11 个分类中文名。
2. 同文件 `PARAM_FIELDS` 按新分类调整键名（`channel` 键已是 `provider_names`，
   `device` 加 `device_ids`，`node` 加 `node_ids`）。
3. 通知列表页（`pages/console/notifications.tsx` 与
   `@admin-port/.../Notifications.tsx`）加分类筛选下拉：按 `kind` 过滤，
   选项来自新目录的 category 去重。**kind 归一放在读侧（§4）**，前端直接用。
4. `THRESHOLD_FIELDS`：新增 `account.quota_exhausted`（无阈值字段）、
   `model.removed`（无）。

## 7. 待确认项（需用户拍板）

1. **账号类是否拆成独立 type？** 用户列出了 8 种账号状态（当日不可用/已过期/额度耗尽/
   token 失效/永久冻结/被禁用…）。我的倾向：**拆**——因为用户显式列了，且
   `notifications` 列表要按具体类型筛。但「当日不可用」「已过期」的语义边界需定义：
   当日不可用 = 上游每日配额类错误（`provider_daily_quota_exhausted`）；
   已过期 = credential 的 `expires_at` 到期。**确认拆不拆、这两个语义对不对。**
2. **安全拦截归属**：现归 `security`。用户列在「密钥」下。是否要在 `api_key`
   分类下也出现（即改 category）？倾向维持 `security`。
3. **`node.new_version` 的 owner_scope**：现为 platform/category=node。
   保持还是移到 system？
4. **自定义分类**：用户列了「自定义」。当前事件类型是静态目录，用户不能自建。
   是否要开放「自定义事件」（用户填 type 字面量 + 自己的 trigger）？这需要
   `_event_allowed` 放宽 + emit 侧允许任意 type，安全面变大。倾向 v1 不做，
   由「事件名称」自由填写满足。
