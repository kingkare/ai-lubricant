# 环境会话历史：节点是内容的唯一来源，库里是显示副本

> 覆盖范围：环境详情页「会话」tab 的对话历史（外部编辑器会话的只读预览）。
> 不覆盖：已采用成平台任务的会话（那条走 `mc_task_events` + ClickHouse `task_messages`，
> 读路径是 `task_service.list_task_events`）。

## 事实模型（一句话）

**节点是会话内容的唯一真相，服务端数据库只是同步时留下的显示副本。** 所有会话内容
都先入库、再对外（读接口、轮询接口都只认库），节点只在**导入**时被读一次。

```
节点文件（唯一来源）                服务端数据库（显示副本）
┌─────────────────────────┐        ┌──────────────────────────────────┐
│ ~/.claude/projects/*/*.jsonl     │ mc_ac_node_sessions              │
│ ~/.codex/sessions/**/*.jsonl     │   ...元信息列...                 │
│ ~/.local/share/opencode/*.db     │   transcript  ← 显示副本（TEXT） │
│  （编辑器自己写，只增）  │  ──→   │     {"items":[...],              │
└─────────────────────────┘  导入  │      "head":bool,                │
                                    │      "window_lines":int}         │
                                    └──────────────────────────────────┘
```

## 三层读取顺序（详情页）

1. **打开详情 → `GET .../sessions/{sid}/transcript`：只读库**。绝不碰节点。节点
   控制面抖动（超时/离线）时详情页照常打得开——这正是本设计的起因：以前读接口
   自己 host_exec 到节点现读 jsonl，节点一超时（实测 `[WinError 121] 信号灯超时`）
   整个详情页就打不开。
2. **会话还在跑（status=running）→ 前端每 2.5s 轮询 `POST .../import {mode:"tail"}`**：
   后端读节点尾部 → 解析 → **先入库** → 返回新增条目（`after`）→ 前端追加到底部。
   贴底才跟滚（`distanceFromBottom > 160px` 不贴底，复用任务页同款判定）。
3. **库里往前翻到头 → 顶部出「查看历史」按钮**（`head=false` 时显示）：点击才
   `POST .../import {mode:"history"}`，把节点读取窗口调大、够到更早的行 → 先入库 →
   返回更早条目（`before`）→ 前端前置到顶部。显式动作，不自动触发。

## 存储：`mc_ac_node_sessions.transcript`（一列，不是新表）

会话表就是现成的账本表（一行一会儿话），**不建明细表**——一份 JSON 列足够：

```json
{"items": [NormalizedItem…], "head": false, "window_lines": 800}
```

* `items`：已导入的规范条目（与任务对话同形状，工具开/闭帧是两条、同 id）。
  **保序**，等于节点文件里一段连续区间。
* `head`：节点文件的最早一条是否已在库里（决定「查看历史」按钮消失）。
* `window_lines`：已导入覆盖的节点行数（水位，history 模式据此再往前够）。

### 为什么这列故意不进控制面模型

`mc_ac_node_sessions` 归控制面（`node_server/store.py::ACNodeSession`）注册，
数据面**故意**不注册它的 Tortoise 模型（两进程各持一份模型定义会漂）。transcript
列也不进控制面的模型：Tortoise 会 SELECT 模型声明的每个字段，控制面的
`list_node_sessions` / `count_node_sessions` 遍历会话行时会被几 MB 的 JSON 拖垮内存。
列由数据面 `user_platform/database.py::_ensure_node_session_ledger_columns` 建
（`IF NOT EXISTS`，两进程谁先启动谁建），写者只有环境的导入接口。

## 去重：内容指纹，绝不用解析器的 id

导入的幂等键是 `_item_fingerprint(item)`（`routes_nodes_files.py`）：

```
sha256(type | phase | event_kind | created_at秒 | text[:500]
       | title[:200] | tool_name[:200] | json(input)[:300] | json(output)[:300])
```

**刻意不含 `item["id"]` / `logical_event_id`（消息部分）**：codex 的消息条目没有
原生 id，解析器给的是自增 counter（`t0`/`t1`…）——窗口从 400 行调到 800 行时同一条
消息会拿到**不同的 counter**，拿它去重，历史就整段重复入库。内容字段跨窗口不变，
才是稳的。

返回给前端的 id（`_stable_item_id`）分两种：

* `tool_call` → 用 `logical_event_id`（`tool_use_id` / `call_id` / `callID`）——
  三家编辑器都是原生稳定 id，且**开帧与闭帧同 id**，前端 `reduceItems` 按 id
  合并成一张卡（否则那张卡永远转圈）。
* 消息/推理 → `env:{指纹}`——每条唯一、跨窗口不变。

已知边界：正文截 500 字、时间只到秒 → 同一秒内完全相同的两段正文判成同一条。
会话记录里这就是重复（解析器的 `_codex_dup` 相邻去重本来也会并掉它），接受。

## 顺序与水位

两条不变量让合并不需要排序：

1. **节点记录只在末尾追加**（jsonl append-only；sqlite 表只增）→ 任何窗口都是文件
   的一段**后缀**。
2. **已存条目是文件里一段连续区间** → 新条目只会出现在它的左端（更早）或右端
   （更新），不可能钻进中间。

合并 = `已存里滑出窗口的（更早，保序） + 本次窗口按文件顺序`。`before`/`after`
按新条目在合并结果里相对已存区间的位置切开。

防丢内容：tail 轮询的窗口**永远盖住已存区间 + 一段余量**
（`max(400, stored_lines + 400)`）——两次轮询之间文件若长出超过余量的行数，中间
那段就不会被跳过。`head` 在「读到的行数 < 窗口」时置 true（文件头已进窗口）。

**窗口变大的副作用**：窗口随已存增长，就会把**更早的行**也新捞进窗口——它们是
「更早的历史」，不是新输出。`before`/`after` 的切分**必须对 tail 也生效**（不能只给
history）：按新条目在合并结果里相对已存区间的位置切开，左端归 `before`、右端归
`after`。曾经 tail 分支把 `added` 整个塞进 `after`，前端轮询按增量追加到列表尾部 →
每 2.5s 堆一段旧记录，几百条后 `reduceItems` + 重组件把详情页拖死。前端轮询只读
`after`（忽略 `before`，那些条目已按正确位置入库，上翻 `loadOlder` 自然看得到）。

## 接口

| 路由 | 作用 |
|---|---|
| `GET /api/v1/teams/environments/{env_id}/sessions/{sid}/transcript?limit=&before_idx=` | 只读库。`before_idx` 是绝对下标游标（上一页回的 `oldest_idx`） |
| `POST /api/v1/teams/environments/{env_id}/sessions/{sid}/import` body `{mode, window_lines}` | 唯一碰节点拿内容的接口；先入库再应答，返回 `{before, after, total, head, window_lines}` |
| `POST /api/v1/teams/environments/{env_id}/sync` | 「同步数据」：对账元信息后对每个会话并发（≤4）导一段尾部副本 |
| `POST .../sessions/{sid}/sync` | 单条同步：元信息 + 一段尾部副本 |

## 窗口常量

| 常量 | 值 | 说明 |
|---|---|---|
| `_TAIL_WINDOW_DEFAULT` | 400 行 | 轮询/同步的尾部段 |
| `_TAIL_WINDOW_MAX` | 6000 行 | 「查看历史」的窗口上限（host_exec 输出上限 16MB 内） |
| `_HISTORY_GROW` | 800 行 | 每次点「查看历史」往前多够多少 |

极端长会话（实测最大 45MB/2.2 万行）可能一次到不了第一条——`head` 如实为 false，
按钮不消失，继续点继续往前够；绝不假装到底。

## 历史教训

* **不要走回"详情现读节点"**：节点控制面不可达是运行常态，不是异常；详情页必须
  打得开。
* **不要用解析器的 counter id 做去重/返回 id**：它随窗口漂移（codex 消息没有原生
  id）；内容指纹才稳。
* **不要给控制面模型加 transcript 字段**：Tortoise 全字段 SELECT 会把控制面的会话
  列表拖垮内存；列只归数据面。

---

# 创建任务 / 同步为任务：统一参数模型

## 两个入口共用一套参数

| 参数 | 来源 | 创建任务 | 同步为任务(adopt) |
|---|---|---|---|
| 环境 | 用户选 | 选/建环境 | 取自被同步会话的环境 |
| 会话模式（权限） | **节点会话同步** | 用户选（下拉） | 从账本只读带入 |
| 思考等级 | **节点会话同步** | 用户选（下拉） | 从账本只读带入 |
| 父 API Key | 用户选 | ✓ | ✓ |
| 模型映射列表 | 用户选 | ✓ | ✓ |
| API Key 限制 | 用户选 | ✓ | ✓ |

差别只有两处：**创建任务建 worktree**（隔离环境默认就是 worktree，见
`new-environment-form.tsx`）；**同步为任务不做任何节点动作**。

## 会话模式/思考等级从节点会话读出来

扫描会话时（`routes_nodes_files.py` 的 `_posix_session_command` /
`_windows_session_command`）多输出第 5/6 列：

| 编辑器 | 权限档（→ 平台 `mode`） | 思考等级（→ 平台 `reasoning_effort`） |
|---|---|---|
| claude | jsonl 的 `permissionMode`（default/plan/acceptEdits/bypassPermissions/auto） | assistant 帧顶层 `effort`（`max`→`xhigh`） |
| codex | `turn_context` 的 `sandbox_policy.type`（read-only/workspace-write/danger-full-access） | `turn_context` 的 `effort`（codex 词表即平台词表） |
| opencode | 读不到（`permission` 是规则数组非枚举） | 读不到 |

值原样进账本 `mode` 列（复用已有列）+ `effort` 列。对账时**非空才写**——空扫描值
（opencode/文件超限）永不覆盖已有值。

## adopt 免派发（同步时零节点动作）

`create_task(..., no_dispatch=True)`：任务行 + 子 Key + principal/grants 全部落库，
但**不碰节点**。会话/工作区/运行时推迟到**首条消息的懒派发**
（`_dispatch_persisted_task_runtime`）。

**关键约束**：`resume_thread_id`（续接原编辑器会话）不落 `config_snapshot`，只在
建任务当场的派发里有效。所以免派发后，懒派发必须从 `Task.source_editor_session_id`
重建它——见 `task_service._attach_resume_tag`。**漏了这步，节点会开全新会话，原
上下文丢失**（这是个曾经存在的 bug，免派发让它显形）。

无 Key 的采用任务也能懒派发：`_dispatch_persisted_task_runtime` 只在「本来有 Key」
时才强制它可用；从来没有 Key 是采用形态（节点用编辑器自己的登录态）。

## 已废弃字段（只写不读，不再写）

`CreateTaskReq.model_whitelist`（由 `models` 同源派生）、`AdoptSessionReq.mode`
（改为从账本读）。**`mode_label` 仍在用，不删。**

`Task.mode_capability_snapshot`、`Task.environment_snapshot`、`Task.workspace_metadata`、
`Task.workspace_source_task_id`、`Task.log_store` 已彻底删除（列 + DTO + 引用），
不再保留空列。
