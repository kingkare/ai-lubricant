# 任务模式与需求/bug 管理 — 设计计划

> 状态：探索/议题进行中。本文件记录已验证的事实与待定项，非最终实现方案。
> 关联 spec 待议题收敛后写入 `docs/superpowers/specs/2026-08-03-task-mode-and-issue-tracker-design.md`。

## 0. 议题状态

- [x] 编辑器能力上报与节点选择链路（已查证）
- [x] OpenCode 模式执行边界与参数生成位置（已查证 + 实测）
- [x] 模式档位由编辑器定义（用户拍板：服务端不设枚举）
- [x] 需求/bug 双状态线（用户拍板：按类型分两条线）
- [x] 回写机制（用户拍板：任务内置 MCP 工具）
- [x] **议题 B（已收敛）**：能实时查询当前模型和当前模式
  - **结论**：三个编辑器都支持
    - Claude: `get_settings` 控制请求（model + permissionMode + 全部状态）
    - OpenCode: `session/info` / `session/status` / `session/active`（model + mode + 状态）
    - Codex: `thread/read` / `thread/info`（model + approvalPolicy + collaborationMode + 状态）
  - 详见 §6「议题 B 实测结论」

## 1. 已验证事实

### 1.1 协议层已就位（无需改 proto）

- `NodeCreateSession.mode`（field 16）：`user_platform/nodeserver/proto/agentcompose_v2.proto:1614-1618`
- `ConfigureSessionMode` + `ConfigureNodeSessionModeRequest`：proto 1701-1708 / 2083-2086
- 帧槽 `NodeDownstreamFrame.configure_session_mode = 19`
- 服务端 `configure_session_mode`：`user_platform/nodeserver/service.py:1158`
- 节点处理 `handler.go:100-103` → `configureMode`

### 1.2 runtime 源码在仓库内

- `agent-compose/runtime/javascript/src/runners/mode.ts`：per-provider 模式映射已实现
- `cli.ts:34` 已有 `--mode`，贯通至 `RunnerOptions.mode`
- 节点三条执行路径均传 mode：`executor.go` / `docker_executor.go` / `stream_executor.go:90-95`

### 1.3 当前"必须重启"的根因（三层各自原因）

**节点侧**：`configureMode`（session.go:543）只改内存 `session.mode`，注释明写"applied at restart"；
`configureLLM`（session.go:485）写盘但同样需重启生效；`recordRevisionLocked` 在 `applied>effective` 时置 `restart_required=true`。

**runtime 侧**：`--model`/`--mode` 经 cli→prompt→RunnerOptions，只在启动时读一次。
- codex：`threadOptions()` 仅在 `startThread`/`resumeThread` 时调（codex.ts:203-204）
- claude：`queryOptions()` 一次性 query
- opencode/gemini：argv 启动时固定

**stream 协议侧（根本原因）**：`streamExecutor.deliver` 只认 `human_message`/`eof`/`cancel`（stream_executor.go:128-144）；
runtime `stream.ts:42-93` 只处理 `start`/`human_message`/`command`/`cancel`/`eof`，`start` 帧重复发直接报 `stream has already been started`。
**协议层没有留"运行中改配置"的口** —— 这是必须重启的根本原因，非节点/runtime 偷懒。

### 1.4 重启代价不大

`restartRuntime`（session.go:334）只 cancel `runCtx`（派生自 baseCtx，不杀 session）：workDir/git/stateRoot/home 保留；上下文靠 provider thread 恢复（codex `resumeThread`、claude `resume`、opencode `--session`）。是"换进程不丢上下文"，非从零开始。

## 2. 实测结论：各编辑器会话内切换能力

本机环境：claude 2.1.206、opencode 1.18.9 已装；codex/gemini/agent-compose-runtime 未装。
SDK 在 `agent-compose/runtime/javascript/node_modules`：`@openai/codex-sdk@0.144.1`、`@anthropic-ai/claude-agent-sdk@0.3.206`。

### 2.1 OpenCode（已实测真热切，不重启）

`opencode serve` 起长驻 HTTP server，OpenAPI（`/doc`）确认有：

```
POST /api/session/{sessionID}/agent   v2.session.switchAgent   "Switch the agent used by subsequent provider turns"
POST /api/session/{sessionID}/model   v2.session.switchModel   "Switch the model used by subsequent provider turns"
POST /api/session/{sessionID}/prompt  v2.session.prompt       Send message
```

实测流程：建会话 → 切 plan（HTTP 204）→ 切 build（HTTP 204）→ 切 bogus agent（仍 204，校验延迟到下一轮取用）。
**结论：OpenCode 会话内切模式/模型即时生效、不重启进程。** 之前 runtime 用 `opencode run`（一次性子进程）才没这能力。

OpenCode 内置 agent（即"模式"）：
- `plan`：`Plan mode. Disallows all edit tools.`（edit 权限 deny）
- `build`：`The default agent. Executes tools based on configured permissions.`
- 另有 `general`(subagent) / `explore` / `compaction` / `summary` / `title`
- agent 可由用户自定义（`opencode agent create`），非固定枚举 → 支撑"档位由编辑器定义"

> 注：OpenCode 内部把"模式"这个概念叫 "agent"，是它的术语，不要与 agent-compose 的 agent 混淆。

### 2.2 Claude（SDK 提供热切，不重启）

`claude-agent-sdk` 的 `Query` 接口（sdk.d.ts:2230）在 streaming-input 模式下有运行时控制方法：

```ts
interrupt(): Promise<SDKControlInterruptResponse | undefined>;       // 2244
setPermissionMode(mode: PermissionMode): Promise<void>;             // 2251 运行中改权限模式
setModel(model?: string): Promise<void>;                             // 2278 运行中改模型
setMaxThinkingTokens(...): Promise<void>;                           // 2301
applyFlagSettings(settings): Promise<void>;                          // 2320 合并 flag settings
```

实现 `sdk.mjs:90` 确认：`setPermissionMode` 发 `subtype:"set_permission_mode"`、`setModel` 发控制请求。
硬约束：注释写明 "Only available in streaming input mode"。当前 `ClaudeRunner` 走一次性 `query()`，未用 streaming-input。
**结论：claude 模式/模型可真热切，需 runtime 改 streaming-input 持有 Query 对象。**

### 2.3 Codex（SDK 无热切口，必须重启）

`@openai/codex-sdk@0.144.1` 类型定义（index.d.ts）：

```ts
type TurnOptions = { outputSchema?: unknown; signal?: AbortSignal };  // 167-172，无 mode/model
runStreamed(input, turnOptions?: TurnOptions)  // 206，per-turn 只能传 schema/signal
type ThreadOptions = { model?, sandboxMode?, approvalPolicy?, ... }  // 239-250，thread 级
startThread(options?: ThreadOptions)   // 265
resumeThread(id, options?: ThreadOptions)  // 273
```

**结论：codex 模式/模型在当前 SDK 下无法会话内热切，只能 resumeThread（=重启进程读旧会话）。**

### 2.4 Gemini

子进程 `gemini -p ...`，argv 启动时固定。靠会话日志续接，下一轮自然生效，无热切意义。

## 3. 真相表（以 CLI 原生 stdio 协议为准，非 Node SDK）

> §2.1–2.4 是先按 **Node SDK** 得出的初步结论，其中 codex 一栏已被 §5.2 推翻：
> SDK 没暴露 ≠ CLI 没能力。下表为最终结论。

| provider | 原生 stdio 协议 | 会话内切 model | 会话内切模式 | 实时查询当前 model/模式 |
|---|---|---|---|---|
| claude | `claude -p --input-format=stream-json` | ✅ `set_model` | ✅ `set_permission_mode` | ✅ `get_settings`（实测返回 `applied.model`） |
| codex | `codex app-server`（JSON-RPC） | ✅ `turn/start.model` sticky | ✅ `turn/start.approvalPolicy`/`permissions`/`collaborationMode`；另有 `thread/settings/update` | ✅ `thread/read` |
| opencode | `opencode acp`（ACP JSON-RPC） | ✅ `session/set_model` | ✅ `session/set_mode` | ✅ `session/new.configOptions`、`session/info`、`session/status` |
| gemini | 子进程 `gemini -p` | 下一轮 | 下一轮 | 无 |

三者均**不需要常驻 HTTP daemon**，只需 runtime 持有编辑器 stdio 子进程（多轮对话本来就需要）。

## 4. 实测发现的安全/正确性坑

- **OpenCode 切 agent 不做提交校验**：传不存在的 agent 也返 204，校验延迟到下一轮 provider 取用。→ 服务端校验档位集合这道防线不能少。
- **codex 模型在当前 runtime 走 env 不走 `--model`**：codex.ts 无 model 字段，模型靠 `llmEnv`（session.go:636）注入 `LLM_MODEL` 等环境变量，进程创建后不可改。→ 这是**现有 runtime 集成方式**的限制，不是 Codex CLI 的限制；改走 app-server 后由 `turn/start.model` 覆盖（见 §5.2）。
- **不采用 `opencode serve` 常驻路线**：该路线默认无密码（`OPENCODE_SERVER_PASSWORD` 未设时裸奔）且需常驻 HTTP daemon。§5.3 的 ACP stdio 路线可达成同样效果且无此问题，故弃用。

## 5. 议题 A 实测结论：无需常驻，统一走 stdio 控制协议

用户的判断正确：编辑器 UI 中的 `/model`、模式切换，底层并不是必须经常驻 HTTP 服务，而是客户端向当前运行中的编辑器进程发送一条结构化控制指令。SDK/API 只是封装层。

### 5.1 Claude Code：stream-json stdin 控制请求（已实际跑通）

命令本身仍是单个 `claude -p` 进程：

```bash
claude -p --input-format=stream-json --output-format=stream-json --verbose
```

往 stdin 写：

```json
{"request_id":"r1","type":"control_request","request":{"subtype":"set_model","model":"claude-haiku-4-5-20251001"}}
{"request_id":"r2","type":"control_request","request":{"subtype":"set_permission_mode","mode":"plan"}}
```

本机 Claude Code 2.1.206 实测输出：
- `set_model` → `control_response subtype=success request_id=r1`，后续事件的 `model` 已变成目标模型；
- `set_permission_mode` → `control_response subtype=success request_id=r2`，后续事件的 `permissionMode` 为 `plan`。

结论：不重启、不另开常驻服务；`Query.setModel` / `setPermissionMode` 本质正是给当前 CLI stdin 写 control_request。

### 5.2 Codex：app-server stdio JSON-RPC，turn/start 支持 sticky override（已验证 schema）

本机安装 `codex-cli 0.146.0`（用户授权安装）。之前只看 `@openai/codex-sdk@0.144.1` 的窄 `Thread` API 得出"不能热切"是错误的：该 SDK 没暴露不等于 Codex CLI 没能力。

Codex CLI 自带：

```bash
codex app-server   # stdin/stdout JSON-RPC，不是 HTTP 常驻服务
```

由 CLI 自生成 `codex app-server generate-json-schema --experimental` 协议，`turn/start` 的 `TurnStartParams` 明确支持：
- `model`："Override the model for this turn and subsequent turns"；
- `approvalPolicy`：本轮及后续轮覆盖；
- `permissions`：选择 named permissions profile，本轮及后续轮生效；
- `collaborationMode`：预设协作模式，优先于 model/reasoning/developer instructions；
- `cwd`、`effort`、`personality` 等也都是本轮及后续轮 sticky override。

结论：保持同一个 app-server 进程和 thread，每次发 `turn/start` 时携带新 model/mode/permissions 即可；不重启、不用 HTTP 常驻服务。

### 5.3 OpenCode：ACP stdio JSON-RPC（已实际启动并探测）

OpenCode 1.18.9 自带：

```bash
opencode acp
```

它走 stdin/stdout JSON-RPC。实测 `initialize` 成功，返回 ACP protocolVersion 1、`loadSession=true`、session close/fork/list/resume 能力；创建 session 后会返回 `availableModes` 与 `currentModeId`。二进制实际包含标准 ACP 方法：

```text
session/set_mode
session/set_model
```

结论：无需 `opencode serve`，无需 HTTP 端口，也无此前讨论的常驻 server 密码问题；runtime 直接持有 `opencode acp` 子进程，经 stdin 发 `session/set_mode` / `session/set_model`。

### 5.4 统一 runtime 设计方向

对三个编辑器统一成：

```text
agent-compose-runtime 启动编辑器的 stdio 协议进程
  ├─ Claude: stream-json control_request
  ├─ Codex: app-server JSON-RPC turn/start override
  └─ OpenCode: ACP JSON-RPC session/set_mode / session/set_model

Node ConfigureSessionLLM / ConfigureSessionMode
  → runtime configure 帧
  → provider adapter 写入当前子进程 stdin
  → 等待 provider ack
  → 节点返回 applied_immediately=true，restart_required=false
```

这仍然是 session 存续期间有一个编辑器子进程（本来就需要处理多轮对话），不是额外启动一个 HTTP daemon。空闲时 CPU 接近零；内存只保留当前 session 的编辑器进程，随 session 关闭而退出。

只有 provider 协议明确拒绝热切、进程已退出、或控制请求失败时才退化为 restart/resume。不能再把 restart 作为所有配置变更的默认路径。

## 5A. 议题 B 实测结论：可实时查询当前模型与当前模式

三个编辑器都能在 session 运行中随时查询当前 model 与当前 mode，无需 runtime 自己缓存推断。

### 5A.1 Claude — `get_settings`（已实测）

发控制请求 `{"subtype":"get_settings"}`，实测返回：

```json
"response":{"applied":{"model":"claude-opus-4-8[1m]","effort":"high",...}}
```

另外每条 `init` 与 `status` 系统消息都带 `model` + `permissionMode`（sdk.d.ts:4297/4301），无需单独查询也能被动获知。

### 5A.2 OpenCode — `session/info` / `session/status` / `session/active`（已实测 configOptions）

`session/new` 的 result 直接给当前状态（实测）：

```text
id=model | current="opencode/big-pickle" | 59 个可选
id=mode  | current="Sisyphus - ultraworker" | 3 个可选
```

运行中还可主动查 `session/info` / `session/status` / `session/active`（二进制方法表已确认存在）。

### 5A.3 Codex — `thread/read`（schema 已确认）

`thread/read`（可带 `includeTurns`）返回 thread 元数据；`turn/start` 的 model/approvalPolicy/permissions/collaborationMode 均为 sticky，thread 状态里可回读。另有 `Model/list`、`CollaborationMode/list` 枚举可选项。

### 5A.4 对 runtime 的要求

- runtime configure 帧的 ack 里回带"生效后的 model/mode"，节点据此更新 session 状态镜像；
- 另提供一个"查询当前 model/mode"的 runtime 控制帧，透传到对应 provider 的查询方法，供前端"当前模式/模型"实时展示与切换失败纠偏；
- 节点侧维护经确认的 `session.current_model` / `session.current_mode`，避免每次前端刷新都往编辑器进程打查询。



## 6. 已定的设计骨架

- 档位完全由编辑器定义，服务端不设枚举；`Task.mode` 自由字符串，合法性由"∈节点上报档位集"决定。
- 节点上报 `EditorCapability.modes[]`（含语义标记 `semantics`），服务端按 semantics 选默认档、按 mode 精确匹配节点。
- 模型切换与模式切换分开设计：两者走同一套 stdio 控制协议（§5.4），模型侧沿用现有 `ConfigureSessionLLM`+HTTP 顺序约束，模式侧补接口。
- 需求/bug 双状态线（ISSUE_TRANSITIONS 表），已确认类状态只由人触发，agent 不可自我确认。
- 回写走任务内置 MCP（`issue-workflow`），上下文由服务端从 session 反查注入，按 task_role 限权。
- 交互式多轮：三个 provider 各走自己的 stdio 协议进程（stream-json / app-server / ACP），非 codex 不再局限于单次 prompt。

## 7. 实施阶段

| 阶段 | 内容 |
|---|---|
| P1 | 能力上报：proto 扩展 EditorCapability/EditorModeSpec；节点探测各编辑器档位与语义；服务端落库返回 |
| P2 | 任务模式：mc_tasks 加 mode/快照；CreateTaskReq 加 mode；_build_session 写 mode+mcps |
| P3 | 匹配与执行：_node_mode_satisfies_spec；节点解析配方下发；runtime 按 §5.4 各 provider 的 stdio 协议驱动 |
| P4 | 会话内切换：节点配置帧 → runtime configure 帧 → 各 provider stdin 控制请求；失败才退化为 restart/resume |
| P5 | issue 模型：type/bug_reason/pending_items/resolution_note；状态机校验；存量迁移 |
| P6 | 分配与关联：assign/confirm 接口；task_role；两阶段+幂等 |
| P7 | 内置 MCP：issue-workflow 服务；上下文注入；按角色限权 |
| P8 | Web 前端：Tab 改名、类型字段、动态模式选择器、详情分区、确认与打回 |
| P9 | 移动端：issue API 与界面补齐 |
