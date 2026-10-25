# 模型映射（Model Mapping）

创建任务 / 同步为任务弹框里，用户配置的是**模型映射**——把编辑器侧的模型角色/菜单
条目映射到平台模型 id。它取代了早期的两套东西：「可用模型」多选（子 Key 白名单）与
「默认模型」单选，**两套都已删除**。

## 统一形状：`{main, slots, categories?}`

```python
main       = 主模型。claude 是档名别名（"opus"/"sonnet"/"haiku"/"fable"，下发成
             ANTHROPIC_MODEL，缺省落 opus 档，不必填）；其余 provider 是平台模型 id，必填。
slots      = 编辑器原生用途槽 {<槽名>: {"model": str, "fallback_models": [str]}}。
             语义按 provider：claude=四档翻译表、codex=catalog 条目、
             opencode=OMO agents 表、dsh=模型条目。
categories = 仅 opencode：OMO 的第二张表 categories（deep/quick/artistry/…），
             与 slots(=agents) 并列，**不合并**——OMO 自己就是两张独立表。
```

旧形状（`roles`/`fallback`/`catalog`/`models`/`model`）读取时经
`task_service._migrate_model_mapping` 归一，存量任务不用迁移数据。

各 provider 的槽语义与落点：

| 编辑器 | 槽名 | 落点 |
|---|---|---|
| claude | 固定四档 opus/sonnet/haiku/fable | env `ANTHROPIC_DEFAULT_{档}_MODEL`（档→平台模型翻译）；main → `ANTHROPIC_MODEL` |
| codex | 目录条目显示名 | `CODEX_MODEL_CATALOG` → `~/.codex/ai-lubricant-model-catalog.json` + `model_catalog_json` |
| opencode | slots=OMO agent 名（sisyphus/explore/…）、categories=类别名（deep/quick/…） | `OPENCODE_AGENT_CONFIG` → 会话临时配置目录的 `oh-my-openagent.json`（agents 段 + categories 段两处并列）；菜单条目 → `OPENCODE_MODELS` |
| dsh | 模型条目名 | `DSH_MODELS` → `settings.yaml` models 列表 |
| gemini / cursor | 无槽 | 只有 main（CLI 参数 / ACP `params.model`） |

## 为什么 claude 的 main 是档名而不是平台模型

Claude Code 按**档位**使用模型：用户在 CLI 里 `/model` 选 opus/sonnet/haiku/fable
（或 opusplan：plan 模式用 opus、执行用 sonnet），请求的 `model` 字段就是档名，
由端点（本网关）解析成具体模型。`ANTHROPIC_DEFAULT_{档}_MODEL` 是每档的翻译表。

因此平台对 claude 的正确控制面是：

- `ANTHROPIC_MODEL = main`（档名）——设定当前主档；
- `ANTHROPIC_DEFAULT_*_MODEL = slots.<档>.model`——每档翻译成平台模型组；
- 子代理/内部按档取用对应槽（`CLAUDE_CODE_SUBAGENT_MODEL`、small-fast 等）。

**曾经的两个 bug（已修）**：旧实现把映射的 `fallback` 塞进 `ANTHROPIC_MODEL`
（= 把兜底钉成主模型，用户选的档永远轮不到）；且 `models_snapshot` 为空时
`_resolve_task_active_model` 从无序 `set` 里随机抓第一个可供给模型（实际抓到过
`gemini-embedding-001`）。现在：配了映射的任务 main 优先命中、兜底扫描改
`sorted()` 确定性。claude 任务的 `main` 没给时默认落 `opus` 档。

## 数据流

```
弹框 (ModelMappingFields，按 provider 渲染 main + slots [+ categories])
  → payload.model_mapping ({main, slots, categories?})
  → create_task / adopt_environment_session / update_task
      · _normalize_model_mapping：所有平台模型值落在父 Key 可执行集合内
        （_validate_models_for_key，含组名；不合法直接抛错，不静默丢）；
        main 必填——claude 例外（档名别名，缺省落 opus 档），其余 provider 空着
        会抛 model_mapping_fallback_required
      · 存 Task.model_mapping (JSONB, mc_tasks)
  → 派发时 _task_llm_config → _apply_model_mapping
      · 返回 main = 任务主模型；帧 model= 与 llm.model 同源（必须一致）
      · slots + categories 翻译成上表的 extra 键（opencode 的槽值带
        agent-compose/ provider 前缀；agents 与 categories 分别落两个段）
  → node proto NodeLLMConfig.extra (map<string,string>)
      · Go session.go 把每个 extra 键直灌进程 env
      · claude 由 JS claudeSettingsLlmEnv 提进 SDK settings env 块（system 档盖过操作者
        ~/.claude/settings.json 里 ccswitch 写的 env）
      · codex 由 Go writeCodexRuntimeConfig 写 catalog 文件 + 指向它
      · opencode 由 JS writeRuntimeConfig 合并进 opencode.json，并把 OMO 槽表写进
        同临时目录的 oh-my-openagent.json + OPENCODE_CONFIG_DIR 指向该目录
        （不碰操作者 HOME 的 OMO 配置）
      · dsh 由 JS writeSettingsFile 写 models 列表
```

## 与子 Key 白名单的关系

模型映射**不再收窄子 Key 的 `model_whitelist`**：映射里的槽模型不在
`models_snapshot` 里，按快照收窄会把槽外角色的请求当场挡掉。配了映射的任务，
其子 Key 继承父 Key 全量。`switch_task_model` / `add_task_models` 对配了映射的
任务也**不再收窄**白名单（`task.model_mapping` 判空守卫）。

## claude 保留别名与映射的关系

`opus`/`sonnet`/`haiku`/`fable` 是 Claude Code 内置别名，**没配映射**的任务选它们
会被 CLI 静默替换成完整型号导致路由错渠道，所以 `_reject_claude_reserved_models`
照旧拒绝。**配了映射**的任务例外（`has_mapping=True`）：main 本就是档名，且任务
自带 `ANTHROPIC_DEFAULT_*` 翻译，选档名是有意为之。

## 任务详情页：主模型 + 齿轮编辑映射

- **主模型 = 映射的 `main`**（claude 显示 `slots.opus.model`——主循环实际跑的
  那一档的平台模型）；没配映射时显示「默认模型」（前端 `taskMainModel()` 返回
  空，不再回落 `models_snapshot[0]`）。
  输入框旁的模型选择器 → `POST /{id}/model`（`switch_task_model`）：claude 写
  `slots.opus.model`、其余 provider 写 `main`，同时把新模型写进 `models_snapshot`
  头部——派发帧的 `model` 与 `llm.model` 都取 main，两者必须一致。
- **槽表走齿轮弹框**：`ModelMappingFields` 按 provider 渲染各自的原生字段
  （claude=opus/sonnet/haiku/fable 四行；opencode=model + agents + categories），
  整体 `PUT /{id}` 带 `model_mapping` → `update_task` 复用
  `_normalize_model_mapping` 校验后落库。空 dict = 清除映射。
- 详情 DTO（`_task_summary`）透出 `model_mapping`，前端 `taskMainModel()` 据此显示。
- **生效时机**：`_task_llm_config` 在每条消息发送时现算，改映射/切主模型都是
  「下一轮消息生效」，不需要热下发或重启运行时。

## 变更节点后

节点改动需重新打包分发（`script/pack-release.sh`）。隔离/共用档 claude 角色变量走进程 env
直进即可生效；system 档靠 `claudeSettingsLlmEnv` 提进 settings env 块；codex/opencode/dsh
走各自的配置写入通道。

## 相关代码

| 层 | 位置 |
|---|---|
| 前端组件 | `user-frontend/src/components/console/task/model-mapping-fields.tsx` |
| 类型 | `user-frontend/src/api/userTaskClient.ts` (`ModelMapping`) |
| 后端校验/迁移 | `user_platform/task_service.py` `_normalize_model_mapping` / `_migrate_model_mapping` |
| 后端翻译 | `user_platform/task_service.py` `_task_llm_config` / `_apply_model_mapping` |
| 主模型解析 | `user_platform/task_service.py` `_resolve_task_active_model` |
| 存储 | `user_platform/models_task.py` `Task.model_mapping` |
| 节点 claude | `nodes/runtime/javascript/src/runners/claude.ts` `claudeSettingsLlmEnv` |
| 节点 codex | `nodes/execution/editorconfig.go` `writeCodexRuntimeConfig` / `writeCodexModelCatalog` |
| 节点 opencode | `nodes/runtime/javascript/src/runners/opencode.ts` `writeRuntimeConfig`（含 OMO 文件） |
| 节点 dsh | `nodes/runtime/javascript/src/runners/dsh.ts` `writeSettingsFile` / `readDshModels` |
| 节点 gemini | `nodes/runtime/javascript/src/runners/gemini.ts` (`--model`) |

支持的编辑器 = `ENV_PROVIDERS`（`user_platform/models_environment.py`）= `SUPPORTED_EDITORS`
（`user-frontend/src/pages/manager/platform/nodes/types.ts`）= claude / codex / gemini /
opencode / cursor / dsh，六者必须同口径。
