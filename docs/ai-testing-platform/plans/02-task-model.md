# 02 · 任务模型（Task）

**状态：设计已定** ｜ 前置：01 资产模型

> 核心修正：**每次启动的用途不一样**，不能把整个用例集无差别跑一遍。任务 = 带侧重、带标签、带通知的一次启动。

## 1. 为什么需要任务

| 问题 | 任务的回答 |
|---|---|
| 不同场景侧重点不同 | 任务用**场景标签**选例，只跑相关部分 |
| 启动方式多样 | 任务支持**多维度启动**（定时 / 手动 / 事件） |
| 结果要通知 | 任务带**通知配置** |
| 要说明"这次跑是为了啥" | 任务带**用途描述 + 标题** |
| 需要公共参数 | 任务带**全局参数** |

## 2. 任务模型

**`mc_test_task`**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `code` | UUID / varchar | `TASK-xxxx` |
| `project_id` | UUID | |
| `title` | varchar | **任务标题** |
| `purpose` | text | **任务用途描述**（这次跑是为了验证什么） |
| `scenario_tags` | JSONB | **场景标签**（按标签选例，见 3 节） |
| `case_filter` | JSONB | 附加筛选（模块/优先级/状态），可选 |
| `exec_type` | varchar | **执行类型**（见 4 节） |
| `schedule` | JSONB | 定时配置（cron/间隔/一次性） |
| `env_id` | UUID FK | 环境（可多套，见 6 节） |
| `global_params` | JSONB | **全局参数**（本次任务所有场景共享，如版本号、目标地址） |
| `notify` | JSONB | **通知配置**（见 5 节） |
| `exec_mode` | varchar | `script_only` / `hybrid` / `ai_only`（可覆盖用例默认） |
| `status` | varchar | `draft` / `enabled` / `disabled` / `archived` |
| `created_by` | UUID | |
| `version` | int | |

## 3. 按标签选例

任务**不直接选用例**，而是**按标签组合选**：

```json
{
  "scenario_tags": {
    "all": ["smoke"],
    "any": ["payment", "order"],
    "none": ["wip"]
  },
  "case_filter": {
    "module": ["payment"],
    "priority": ["P0", "P1"],
    "automation_status": ["automated"]
  }
}
```

**选例算法**：

```
candidates = 所有 active 用例
  ∩ 用例标签满足 scenario_tags（all 全含 / any 含其一 / none 都不含）
  ∩ 用例满足 case_filter（若给）
  ∩ 用例已 automated（automation_status=automated）
→ 得到本次要跑的场景集合（通过 case→scenario 关联取场景）
```

> 注意：**执行单位是场景**（用例通过场景实现）。同一场景被多个选中用例引用 → 只跑一次（去重）。

## 4. 执行类型（exec_type）

多维度启动方式：

| 类型 | 说明 | 触发 |
|---|---|---|
| `manual` | 手动启动 | 人点"运行" |
| `scheduled` | 定时启动 | cron / 间隔 / 一次性 |
| `event` | 事件触发 | 代码 push（增量）/ bug 修复（回归） |
| `api` | 接口触发 | 外部系统调接口 |

`scheduled` 的 `schedule`：

```json
{ "kind": "cron", "expr": "0 2 * * *" }        // 每天 2 点
{ "kind": "interval", "minutes": 30 }           // 每 30 分钟
{ "kind": "once", "at": "2026-10-01T09:00:00" } // 一次性
```

## 5. 通知配置（notify）

执行结果**必须能通知**：

```json
{
  "on": ["fail", "error"],              // 触发时机：always / fail / error / pass
  "channels": [
    { "type": "feishu", "webhook_ref": "feishu_test_room" },
    { "type": "dingtalk", "webhook_ref": "..." },
    { "type": "wecom", "webhook_ref": "..." },
    { "type": "webhook", "url": "..." },
    { "type": "mobile_push", "token_ref": "..." }
  ],
  "template": "default",                // 通知模板
  "include": ["summary", "failed_cases", "report_link"]
}
```

> 通知通道**复用服务既有能力**（已核对实际，2026-09-28）：`user_platform/notify_dispatch.py` 提供钉钉/飞书/企微/webhook 四通道（含各自签名细节）；`user_platform/notify_push.py` 提供 Expo 移动推送；另有站内通知中心。**平台无 email 通道**（原设计误列，已删）。任务只声明"发给谁、什么时候发、发什么"。

## 6. 环境（多套，人为管理）

> 你指出：可以有多套环境用于不同任务，一般是**人为管理**使用的。

- 环境是**独立的可管理实体**（`mc_test_environment`，分层继承 + 类型化变量）
- **一个任务绑定一个环境**（通过 `env_id`）
- 不同任务可用不同环境（dev 任务用 dev 环境，nightly 任务用 staging）
- 环境由人管理（增删改、变量维护、密钥脱敏），任务只引用

详见 `05-environment.md`。

## 7. 任务的接口（人 / AI 均可操作）

任务**提供接口**，人和 AI 都能添加/修改：

| 方法 | 说明 |
|---|---|
| `task.create` | 创建任务 |
| `task.update` | 修改任务（改配置/标签/通知） |
| `task.enable` / `task.disable` | 启停 |
| `task.run` | 立即启动一次（手动触发） |
| `task.list` / `task.get` | 查询 |
| `task.result` | 取最近/历史运行结果 |

- **人**：页面表单操作
- **AI**：经能力面调上述方法（如"给我建一个每晚跑 smoke 的任务，失败通知飞书"）

> 任务创建/修改也走**变更门**（记录 change_reason）。

## 8. 任务 → 运行

```
task.run（手动/定时/事件）
  → 按标签选例（第 3 节）→ 得到场景集合
  → 合并 global_params + 环境变量 → 执行上下文
  → 逐场景执行（引擎）
  → 汇总为 mc_test_run
  → 按 notify 配置发通知
  → 生成报告
```

**`mc_test_run` 增加字段**：`task_id`（来自哪个任务）、`global_params`（本次快照）。

## 9. 与旧设计的差异

| 旧 | 新 |
|---|---|
| 只有 run（运行），无任务 | **任务**是一等实体：标签选例 + 执行类型 + 通知 + 用途 + 标题 + 全局参数 |
| 直接选脚本/用例跑 | **按标签选例**，用例→场景→执行 |
| 无通知 | 通知配置（通道/时机/内容） |
| 无启动方式概念 | 多维度启动（手动/定时/事件/接口） |
| 任务不可被 AI 操作 | 提供接口，人/AI 均可增改 |

## 10. 设计决策

| # | 问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | 选例粒度 | **执行单位是场景**；结果挂在场景上，`case_refs` 记录引用它的用例 | 同一场景多用例时只跑一次（去重）；用例级结果由场景结果**投影**得出 |
| 2 | `event` 触发 | **v1 只做 manual + scheduled**；`event` 预留接口，待知识图谱就绪再接 | 增量选例依赖影响分析，后置 |
| 3 | 任务并发 | **同任务默认互斥**（前一次未结束则拒绝或排队）；可配 `allow_concurrent` | 避免同任务重复触发污染结果与资源 |
| 4 | 通知模板 | **平台提供默认模板 + 可自定义**（模板变量：summary/failed_cases/report_link） | 开箱可用，又不锁死 |
