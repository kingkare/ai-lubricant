# Agent 体系横向对比与自查（dsh × 我们 × GA × pi）

> 形成：2026-10-04。基于真实源码扫描（agent/、.dsh-home/、.venv、C:/Users/.../.pi/、D:/code/GenericAgent/）。所有结论附 file:line。dsh 因原生包二进制是 Node pkg 快照，JS 可读；GA/pi 全可读。

四个对比对象：

| 代号 | 是什么 | 落点 |
|---|---|---|
| **dsh** | DeepSeek Harness 官方 SDK（Cordis 插件 OS，236 个 @deepseek-ai 包） | `.dsh-home/`、`.venv/Lib/site-packages/deepseek_harness*` |
| **GA** | GenericAgent（个人单机 agent，Python，~3K 行核） | `D:/code/GenericAgent/`、`D:/code/_ga_ref/` |
| **pi** | Mario Zechner 的 `@earendil-works/pi-coding-agent`（终端编码 agent CLI） | `C:/Users/.../.pi/`、`.dsh-home/.../@earendil-works/pi-ai` |
| **我们** | ai-lubricant 的 `agent/`（DB 驱动多租户平台 agent，~20K 行） | `agent/` |

---

## 一、定位与哲学

| | 哲学 | 控制面在哪 | 隔离手段 |
|---|---|---|---|
| dsh | 「插件 OS + 事件溯源日志 = 唯一真相源」；每个域（goal/schedule/sandbox/permission）都是 session log 的 fold | Cordis 插件组合（YAML patch 全替换）；fail-closed 是硬约束 | OS 级沙箱（bwrap/Landlock/Seatbelt/WinACL restricted token）+ CAS 读前置写 |
| GA | 「最小工具集 + L0-L4 记忆 + SOP 自进化」；65 行循环，政策全在工具/SOP | 9 个 `do_*` 方法 + Markdown SOP；`StepOutcome` 决定终止 | 几乎无（真实子进程、真实浏览器、`eval` in-process）；审批是 prompt/SOP 级 |
| pi | 「最小 harness，自己造功能」；明确**不做**子 agent / plan / 权限，留给扩展和容器 | 单核心 + 30+ 事件扩展 API；`agent_before_settle`/`agent_settled` 干净停止边界 | 不带；推容器（Gondolin micro-VM / Docker / OpenShell）+ 扩展读 MCP `annotations` 走 `tool_call` 拦截 |
| 我们 | 「GA 血统 + 平台多租户 + capability_call 统一出口」；DB 配置 + 网关 LLM | 9 工具 + `capability_call`（workflow + MCP 服务）+ Guardian 三模式 | 审批卡（code_run / file_write 越界 / node_shell）+ `denied_patterns` 黑名单；**无 OS 沙箱** |

一句话：dsh 把「隔离」当一等公民，pi 把「可组合性」当一等公民，GA 把「token 密度 + SOP 自进化」当一等公民，我们把「平台接入 + 审批」当一等公民——**KV-cache、隔离、steering 这三块，我们明显薄**。

---

## 二、Agent Loop

| 维度 | dsh | GA | pi | 我们 |
|---|---|---|---|---|
| 循环结构 | `ReactLoopAgent` 驱动 + **durable inbox**（每改一行 commit 一个事件，投影同步 fold） | 65 行 generator；每轮只发一条新 user 消息，全量历史在 session 里 | session 核 + tree 结构；`agent_before_settle`/`agent_settled` 边界 | `agent_loop._agent_runner_loop_impl`（1001 行）每轮 compress+trim→流式 chat→分发 tool_calls→`<anchor>` 注入 |
| 中途插话 | `steer()`（next-step）/`followup()`（next-turn）/`inject()`（不唤醒）三级，inbox 持久 | `_done_hooks` 队列 + `turn_end_callback` 消费 `_intervene`/`_stop`/`_keyinfo` | steering 消息进当前 assistant 轮后；abort 把队列消息退回编辑器 | **无 loop 级 steering 原语**——会话级只有整轮重发/重试；`on_event` 是单向 SSE |
| 并行工具调用 | `maxParallelToolCalls`（默认 10），独占调用做 ordering barrier | 同一轮多个 `do_*` 串行 dispatch，按 `_tool_num` 均分预算 | codemode 内 `Promise.allSettled` 并行调工具；常规串行 | **串行 dispatch**（`for index, tool_call in enumerate(tool_calls)`，`agent_loop.py:802`），无并行 |
| 终止 | turn 边界 durable；inbox 投影判空 | `StepOutcome.should_exit` / 空 `next_prompt` → done；`no_tool` 政策钩 | `agent_settled` 终态 | 模型纯文本 / `should_exit` / `turn >= 75` 硬退出 / `max_turns` |
| 重试 | `dsh-llm-retry` 在 step 边界；稳定错误码 | loop 无重试，下沉到 `llmcore._stream_with_retry` + MixinSession 故障转移 | provider 层 | `agent_loop` 对**瞬时 429**做指数退避；网络异常归 `LLMBridge`；已吐内容不重试 |

**我们的 loop 问题**：
- **硬编码 75 轮强退**（`agent_loop.py:931-936`），但 `AgentConfig.max_turns` 默认 80、DB 默认 80（`config.py:36`、`db.py:903`）。配 80 的 agent 实际只能跑到 75，且配 100 也被砍到 75——**配置被静默覆盖**。
- **无 loop 级 steering**：dsh 三级 inbox、GA `_intervene` 文件、pi abort+queue 都能中途插话/纠偏；我们只能等本轮结束。`supervisor_sop.md` 写的「一句话干预」根本进不来。
- **工具串行**：5 个独立 file_read 不能并行，dsh/pi 都能。

---

## 三、上下文 / 压缩 / Spill

| 维度 | dsh | GA | pi | 我们 |
|---|---|---|---|---|
| 触发 | 80% 窗口阈值 + 上下文溢出恢复 | `cap = context_win*3` chars，目标 `cap*0.6` | `contextTokens > contextWindow - reserveTokens`（reserve 16384） | `cap = context_window*3`，默认 `context_window=30000`（`context_manager.py:26-27`） |
| 窗口来源 | routed context window（按模型） | per-model（deepseek 70000） | 按模型真实 token | **硬编码 30000，从不按模型派生**——`agent_runner_loop(context_window=90000)` 默认值，全代码无 caller 传 `context_window=`（见下「问题 §2」） |
| 摘要 | log-recorded bracket；唯一面变更是 `user/message replace` 带 `<compacted-summary>`+checkpoint preamble；byte-identical 重放复用 provider 前缀缓存 | tag 级 truncate + 整条 pop；无 LLM 摘要 | 结构化摘要 prompt + 迭代上下文；CompactionEntry 进 tree | `trim_messages_async` 在 GA pop 之上叠 LLM 摘要，`<history_summary>` 块**原位替换** dropped 切片（不追加） |
| 工具结果剪枝 | **prune 在摘要前**：>8192 码点 → head 4096 + tail 1024，原 event 留 log | `compress_history_tags` truncate | 随 compaction | **无独立 prune**——`_clamp` 在 execute 后做头尾裁剪（15000/工具数），溢出走 `tool_spill`，但 compaction 阶段不再 prune |
| Spill | `ctx.spillStore.saveText()` → `{locator, bytes, retrievalHint}`；`maxInlineBytes: 50000`；read 结果豁免防循环 | 无（`save_to_file` 是 web_execute_js 的参数） | 无 | `tool_spill.py`：超预算存全量到 workspace/spill/ + 预览 + locator，`file_read` 豁免防 read→spill 死循环 |
| KV-cache 纪律 | **一等公民**：每包带 `Model Experience`；`in-history` prompt 更新；摘要器 byte-identical 重放；fork 继承 route；plan mode 工具目录恒定 | `_stamp_oai_cache_markers` 给最后 2 条 user 加 `cache_control:ephemeral`；NativeClaude system + 最后 tool 加 cache_control | `cacheWarming`（streaming/idle，门槛 $0.05）；摘要请求禁 cache 写 | **几乎没有**：`llm_bridge.py` 无 `cache_control`/`prompt_cache_key`；靠网关 providers 透传，agent 侧不主动管 |

**我们的上下文问题**（详见 §六）：
- `context_window` 全链路硬编码，长上下文模型被白瞎、短上下文模型会溢。
- `_compress_counter` 是**模块级全局**（`context_manager.py:36,117-120`），多会话共享 → 压缩节奏跨会话泄漏。

---

## 四、记忆 / SOP / 技能

| 维度 | dsh | GA | pi | 我们 |
|---|---|---|---|---|
| 记忆分层 | session log 是唯一 store；goal/schedule/todo/sandbox/permission/plan 全是 log fold；projection 提供 `asOfSeq` 切片 | L0-L4：L1≤30 行索引每轮注入；L2 事实；L3 SOP/脚本；L4 归档会话 | tree 结构 session JSONL；CompactionEntry 进 tree；分支摘要 | L1/L2/L3/L4 真文件（`data/agents/<id>/memory/`）；`meta_memory.py` 宪法每轮注入；`self_improvement_log.py` 跨任务教训 |
| SOP 载体 | skill 包 + durable `<available_skills>` catalog 消息 | `memory/*.md`，L1 索引发现，**绝不整段注入** | Agent Skills spec；name+description 进 prompt，正文按需 load | `agent/sop/*.md`（22 个）seed 进每个 agent 的 `memory/sop/`，L1 触发表（`_SOP_TRIGGERS`）+ 场景内联（browser_sop） |
| 自进化 | skill 注册多 provider merge | 「No Execution, No Memory」+ `start_long_term_update` 蒸馏 | 用户/包安装 skill | `start_long_term_update`（≥10 轮才允许）+ `_route_long_term_memory` 后端定 L2/L3 |
| 存在性编码 | catalog 消息只列 name+description | L1 只标「能力存在」，深度靠 file_read | skill name+description 进 prompt | L1 `_builtin_sop_index_lines` 聚合成 `L3:` 行 + 触发词 |

**我们的 SOP 问题**（详见 §六）：3 个 SOP 引用已删除的能力（`scheduler.*`）；5 个 SOP 让模型 spawn subagent 但**没有这个工具**；`meta_memory.py:46` 写「10 first-class tools」实际 9 个。

---

## 五、模式 / 子 agent / 沙箱 / 审批 / 调度

| 维度 | dsh | GA | pi | 我们 |
|---|---|---|---|---|
| Plan | 软引导（prompt order 500 + `exit_plan_mode`，工具目录恒定）；log-only whole-value `plan/mode` | 五阶段 + plan.md checklist；**拦截过早 complete**（`no_tool` 钩要求 `[VERIFY]`） | 明确不做 | `plan_mode.py` PlanSession（explore→plan→confirm→execute→verify），但 **state machine 半死**——只 `create()` 被 caller 用，`set_steps/mark_step/update_phase` 无 caller；loop 不拦截过早 complete |
| Goal | 同 session 一个；阶段 active/paused/blocked/complete；`defaultMaxGoalRounds:256`；**create/edit/pause/resume 要求 runtime-root turn 里直接人类消息**（host-attested `{kind:'user'}`） | Guardian 创造/检验/改进循环直到预算耗尽 | 无 | Guardian 三相（creation/inspection/improvement）+ 预算耗尽 wrap-up + **独立校验 agent**（`verify_goal_completion` 试图 refute） |
| Ralph | `dsh-tool-ralph`：前景固定脚本循环，fresh children，immutable objective，bounded handoff（16384），terminal complete/blocked/budget-limited；worker 自声明，无 evaluator | 无 | 无 | `ralph.py` + `guardian.py` ralph 状态机；每轮 **fresh handler**（与 goal 的本质差别）；strict JSON report（continue/complete/blocked）；`parse_round_report` 校验 |
| 子 agent | named-provider registry（spawn fresh / fork seeded）；one-shot vs continuable；child scope **fresh-flat**（不继承权限）；parent 只拿 final output + settlement notice | 三套：file-IO 子进程 / Conductor HTTP / Goal Hive BBS；编排者**禁止下场干活** | 明确不做（codemode 可编排） | `spawn_subagent_async` 存在（`agent_main.py:923`）但**只被 `ralph.verify_goal_completion` 内部调用**——**模型无工具可达**；`subagent_mailbox.py` 死代码（无 importer） |
| 沙箱 | 三档 read-only/workspace-write/danger-full-access；policy rides the call；bwrap/Landlock/Seatbelt/WinACL restricted token；**fail-closed `SANDBOX_UNAVAILABLE`**；CAS 读前置写（`FS_NOT_OBSERVED`/`FS_STALE_VERSION`） | 几乎无 | 无（推容器） | **无 OS 沙箱**；`WorkspaceSecurity` 只做路径白名单 + `denied_patterns` 黑名单正则；`code_run` 拿服务器全权跑 |
| 审批 | `dsh-user-approval` channel-neutral one-shot；`ask`/`never`（headless）；`approval/asked`+`approval/decided` 审计对；无 allow-always | `ask_user` 是原语，模型自己决定何时调；conductor 转发不存 | 推扩展读 MCP `annotations` + `ctx.ui.confirm` | `CodeRunApprovalBatch`/`FileWriteApprovalBatch`/node_shell batch 三套**各写各的**；auto 模式全放行；越界 file_write 单次卡；无通用 action-approval 框架 |
| 调度/后台 | `dsh-schedule`（session-local reminders）+ `dsh-jobs`（background registry）+ `dsh-workflow`（model 写 JS 在 worker thread 跑） | `reflect/scheduler.py` 轮询 JSON 任务 + socket 单例 + 12h L4 归档 cron | 无 | **scheduler.py/scheduled_script.py 已删**（工作流接管 cron）；`background_workers.py` 每 60s 扫所有 enabled agent 起 reflect/autonomous worker；`workflow_scheduler.py` APScheduler 单例 |
| 工作流 | `dsh-workflow`：模型写 plain-JS body，worker-thread 引擎，result never rejects | 无 | 无 | `workflow.py` 标准工作流图（nodes+edges，条件路由在边上；code_run/file_read/file_write/web/run_mcp/run_session/cache 七步骤）+ 声明式入参 + 静态校验 + 版本化 + cron；**用户资产，与 agent 解耦**（agent 只能启动） |

---

## 六、我们 agent 的问题清单（SOP / 设计 / 流程）

> 全部基于真实扫描，附 file:line。按严重度排序。

### A. SOP 与现实脱节（高）

1. **`scheduled_task_sop.md` 是死 SOP**（171 行，8.2KB，`agent/sop/scheduled_task_sop.md`）。
   - 它教模型用 `capability_call(name="scheduler.create"...)` 创建定时任务，并描述 `agent/scheduler.py` + `AgentTaskRunner` + script hash 锁。
   - 但 `agent/scheduler.py`、`agent/scheduled_script.py` 已删（git status 显示 `D`），`capability_call` 现在**只路由 `workflow.*` 和 MCP 服务**（`tools.py:1286` `if scope == "workflow"`），`scheduler.*` 会落到 MCP 分支报「未绑定 MCP 服务: scheduler」。
   - 它仍被 `ensure_builtin_sops` seed 进每个 agent，仍在 `_SOP_TRIGGERS`（`file_memory.py:98`），仍在 L1 索引每轮进 system prompt。
   - **修法**：删 SOP，或改写成「定时任务统一走 `workflow.*` + cron 挂载」（`workflow_sop.md` 已是真相）。

2. **3 个 SOP 让模型 spawn subagent，但没有这个工具**。
   - `subagent_sop.md`、`plan_sop.md`（「spawn a subagent via the parent's spawn_subagent flow」+ `[VERIFY]` 子 agent）、`supervisor_sop.md`（「立刻启动 subagent」）都让模型派生子 agent。
   - 但 `spawn_subagent_async`（`agent_main.py:923`）**唯一调用方是 `ralph.py:228` 的 `verify_goal_completion`**——是 goal 模式收尾的内部校验，**不是模型能调的工具**。9 个 first-class 工具里没有 spawn，`capability_call` 也不路由它。
   - `subagent_mailbox.py`（83 行）**无任何 importer**——死代码。
   - 模型照 SOP 做会卡住或用 `capability_call(name="subagent.*")` 报错。
   - **修法**：要么补一个 `spawn_subagent` 工具（暴露 `spawn_subagent_async`），要么改 SOP 说「子 agent 派生是平台内部能力，模型只能靠 goal/ralph 验证或 workflow.agent 节点」。

3. **`meta_memory.py:46` 写「10 first-class tools」实际 9 个**。
   - `_register_phase1`（`tools.py:316-327`）注册：code_run/file_read/file_write/file_patch/web/update_working_checkpoint/start_long_term_update/ask_user/capability_call = **9**。
   - meta_memory 每轮注入 system prompt，模型被告知一个错误数字。`sop/README.md:40` 也写「9 个」，自相矛盾。
   - **修法**：改 meta_memory 为 9。

### B. 设计问题（高/中）

4. **`context_window` 全链路硬编码，不按模型派生**。
   - `agent_runner_loop(context_window=90000)` 默认值（`agent_loop.py:378,428`），但 **api.py / cdp_chat_service.py / background_workers.py 无一传 `context_window=`**（grep 全空）。
   - `context_manager.DEFAULT_CONTEXT_WINDOW=30000`（`context_manager.py:26`），`cap = 30000*3 = 90000` chars。
   - 模型元数据里有 `max_context_tokens`（`api.py:2883` `_humanize_context_window` 在用），但**没传进 loop**。结果：200K 上下文模型被当 90K chars（≈22K token）压；32K 模型可能压晚了溢出。
   - **修法**：`_start_conversation_turn` 把模型 `max_context_tokens` 乘 0.8 当 `context_window` 传进去。

5. **`_compress_counter` 是模块级全局**（`context_manager.py:36,117-120`）。
   - 多会话共享一个计数器，`compress_history` 的「每 5 次才压」节奏跨会话泄漏——A 会话调 4 次、B 会话调 1 次时 B 触发压缩，A 永远不压。
   - **修法**：counter 放到 per-run handler 或 messages 闭包里。

6. **`code_run` 前景路径在 async 里直接 `subprocess.run`**（`tools.py:482`）。
   - `_code_run` 是 async，但前景用阻塞 `subprocess.run(timeout=...)`，默认 60s、可配更高。整个事件循环被一个 code_run 卡住期间，同进程所有会话的 SSE/心跳/WS 全停。
   - 后台路径用了 `asyncio.create_subprocess_exec`（`jobs.py:88`）是对的——前景路径没改。
   - **修法**：前景也走 `asyncio.create_subprocess_exec`，或 `asyncio.to_thread(subprocess.run, ...)`。

7. **`max_turns` 配置被 75 硬退覆盖**（见 §二）。
   - `config.py:36` 默认 80，DB 默认 80（`db.py:903,1078`），但 `agent_loop.py:932` `if turn >= 75: ... MAX_TURNS_EXCEEDED`。
   - 配 100 也只能跑 75。anchor 里写「turn: N/80」但永远到不了 80。
   - **修法**：删 75 硬退，让 `range(1, max_turns+1)` 兜底。

8. **`api.py` 与 `cdp_chat_service.py` 大段重复**。
   - `_start_conversation_turn`（`api.py:1545-2210`，~665 行单函数）和 `cdp_chat_service.py` 各有一份 mode-driver（plan/goal/ralph hint + 续跑循环 + verify + 落库）。grep 确认两文件都有 `mode_eff == "goal"` 等 9 / 7 处匹配。
   - 已经在漂移：cdp 对 `goal_config.objective` 空值单独 raise，api 没有；两边的 `effective_system_prompt` 拼接顺序略不同。
   - **修法**：抽 `agent/mode_driver.py` 共用，两入口只留 event 收集差异。

9. **无 OS 沙箱**（见 §五）。
   - `code_run` 拿服务器用户全权；多租户下，一个被批的 code_run = 服务器 RCE，能读 `data/agents/<id>/`（所有租户）、`.env`（DB 凭据）、网关 key。
   - dsh 的 Windows ACL restricted-token（`dsh-sandbox-windows-acl`）在同世界能落地，我们也在 Windows——**这是最大安全债**。
   - **修法**：要么 Docker 执行后端（volume 只挂工作区），要么学 dsh 的 restricted-token + Job Object。

10. **审批三套各写各的**（`code_run_approval.py` / `file_write_approval.py` / node shell batch）。
    - 无通用 action-approval 服务，无 ask/never/auto 三档统一抽象，无每决策落审计。
    - 记忆里 dsh 对比拍板第 7 项就是「审批重构抄 dsh 作业……先记住暂不动工」——**仍欠债**。

### C. 流程问题（中）

11. **`subagent_mailbox.py` 死代码**（83 行，无 importer）。删。

12. **`background_workers.py` 每 60s 扫所有 enabled agent**（`_reconcile_workers`）。
    - N 个 agent = N 个常驻 reflect worker（`_ensure_reflect` 无条件起）+ N 个 autonomous worker（开了才起），每个 5s tick 一次 `glob` + `stat`。
    - 大租户下是 N 个常驻 asyncio task + 60s 一次 DB 全扫。**修法**：reflect worker 改成「有脚本才起」，`_ensure_reflect` 加存在性判断。

13. **goal 续跑 `initial_messages=None`**（`api.py:2009,2083`）。
    - 每个 goal 续跑/ralph 轮都是全新 messages，**只靠 handler 的 working anchor 跨轮**。设计如此（与 GA 一致），但意味着 goal 模式跨轮无对话历史——模型只记得 anchor 里写的。这是特性不是 bug，但 **SOP 没写清楚**，模型会以为记得前面。

14. **`run_task`（无人值守入口：reflect/autonomous/background dispatch）不挂审批**。
    - `background_workers._dispatch` → `GenericAgent.run_task` → `agent_runner_loop`，全程不 `set_code_run_approval`。
    - 结果：无人值守路径下 `code_run` 直接 `denied`（`tools.py:357-363` `self._code_run_approval is None` → `approval_required`）。
    - 即 reflect/autonomous worker 派出的任务**根本跑不了 code_run**——autonomous 模式形同虚设。
    - **修法**：无人值守要么走工作流（workflow 节点有授权），要么给 run_task 一个「无人值守审批策略」（白名单只读 / 指定脚本 hash 预批）。

15. **9 工具但 `web` schema 8775 字符**（`_WEB_SCHEMA`）。
    - 一个工具描述塞了 14 个 operation 的语义。每轮 system prompt 都带它。
    - dsh 把 web 拆成 `dsh-tool-web` + 各 operation；pi 有 `tool_search` 按需声明。
    - **修法**：考虑把 web 的罕见 operation（batch/wait/watch_traffic）改成 `capability_call` 路由，主 schema 只留 read_page/execute/tabs。

---

## 七、平台优化点（按 ROI 排序）

### P0（安全 + 正确性，立刻做）

1. **补 OS 沙箱**（问题 9）。Docker 执行后端或 dsh 式 restricted-token。多租户平台没有沙箱是 blocker。
2. **删 `scheduled_task_sop.md`**（问题 1）。死 SOP 每轮占 token 还误导。
3. **subagent 工具化 or 改 SOP**（问题 2）。当前 3 个 SOP 指向不存在的工具。
4. **修 `context_window` 按模型派生**（问题 4）。一行改动，长上下文模型立省 token。
5. **修 75 硬退**（问题 7）。让 max_turns 配置生效。
6. **`code_run` 前景走 async subprocess**（问题 6）。防事件循环卡死。

### P1（架构债，近期做）

7. **抽 mode_driver 共用层**（问题 8）。api/cdp 两份 665 行重复。
8. **通用 action-approval 服务**（问题 10）。三套审批合一，ask/never/auto + 审计。
9. **`run_task` 无人值守审批策略**（问题 14）。让 autonomous/reflect 真能干活。
10. **loop 级 steering 原语**（§二）。对齐 dsh `steer/followup/inject`，让 supervisor 模式能落地。

### P2（对齐 dsh/pi 的工程红利）

11. **KV-cache 纪律**：`llm_bridge` 给 system block + 最后稳定 user 加 `cache_control`；mode_eff 切换不要改 system prompt（改放 user 消息）；摘要器重放保持 byte-identical。
12. **工具结果 prune 前置**（§三）：compaction 摘要前先 prune（head 4096 + tail 1024），可能省掉一次 LLM 摘要。
13. **并行工具调用**（§二）：独立 tool_call 并发 dispatch。
14. **`background_workers` 按需起 reflect**（问题 12）。
15. **token meter / 上下文压力投影**：像 dsh `dsh-token-meter` 一样做 replay-based 估算，把「还剩多少窗口」喂进 anchor。

### P3（清理）

16. 删 `subagent_mailbox.py`（问题 11）。
17. 改 `meta_memory` 的「10→9」（问题 3）。
18. `_compress_counter` 改 per-run（问题 5）。

---

## 八、一句话总结

我们的 agent 在**平台接入（DB/网关/审批/场景/工作流）**上明显比 dsh/GA/pi 成熟，但在**隔离、KV-cache、steering、上下文自适应**这四块是肉眼可见的薄。SOP 体系是 GA 血统的优势，但**已经和代码漂移**（死 SOP、不可达 subagent、计数错误）——这是「SOP 即真相源」哲学的反噬：SOP 改得快、代码改得慢，没人校对就漂。建议把 P0 的 6 项当一轮收口，再启动 P1 的 mode_driver / 通用审批两个重构。
