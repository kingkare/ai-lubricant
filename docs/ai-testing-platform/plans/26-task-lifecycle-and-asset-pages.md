# 26 号文 · 任务生命周期与资产页改造（用户口径梳理）

> **本文是用户口述需求的规格化整理，不是实现方案。** 需求来自用户在 2026-10-21/22
> 连续多轮讨论中反复提出并纠正的内容。凡与既有设计文档（19-25 号文）冲突处，
> 以本文为准，并在文末「与既有文档的冲突」列出待裁决项。
>
> 上游文档：`19-version-lifecycle` / `20-execution-loop` / `24-multiagent-pipeline` /
> `25-case-generation-complete-design`（生成链本体）· `05-environment`（环境）·
> `06-script-engine`（脚本引擎）· `skill-library-research.md`（方法基底）

---

## 第一部分 · 任务：一类动作 = 一个任务 = 五个阶段

### 1.1 五类任务

平台要提供的**五类任务**（不是五种资产，是五种"要干的活"）：

| 任务 | 要回答的问题 | 关注点 |
|------|--------------|--------|
| **探索** | 探索目的是什么？补齐用例 / 补齐需求 / 探索测试 / 其他 | 目的不同 → 每个方案分别关注的东西不同 |
| **生成用例** | 生成哪些客户端的、哪些模块的、哪些功能的 | 端 × 模块 × 功能 三个维度 |
| **生成场景** | 把用例变成可跑的步骤 | 端相关的步骤形态 |
| **需求分析** | 有没有矛盾？合不合理？未知项是啥？怎么分析 | 矛盾 / 不合理 / 未知项 三类产出 |
| **测试执行** | 执行哪一些？执行方案是什么 | 范围 + 执行方案 |

**关键**：「探索」可以**没有需求输入**——用户可能直接启动探索来**生成需求**。
因此探索是**入口任务**，不是生成链的下游附属环节。

### 1.2 五段生命周期（所有任务统一）

```
创建 → 生成方案 → 确定方案 → 执行方案 → 应用方案
```

| 阶段 | 产出 | 谁决定 |
|------|------|--------|
| **创建** | 任务实例 + 该任务类型专属的维度/参数表单 | 用户 |
| **生成方案** | 角色分工、执行策略、资源计划、依赖检查 | agent（可多次迭代） |
| **确定方案** | 冻结的方案（可编辑后确认） | 用户确认 |
| **执行方案** | 实时进度 + 每个 agent 在做什么 + 证据 | 系统执行，用户可见 |
| **应用方案** | 结果入库 / 变更包 / 回滚 | 用户确认应用 |

### 1.3 为什么要这五段（用户的五条理由 → 设计约束）

1. **用户需要交互**：不清楚你到底怎么做的、思路是什么、怎么回滚、是不是他需要的、
   思路对不对。
   → **方案必须是可见、可编辑、可回滚的产物**，不是黑盒执行。
2. **不能一套模板跑完**：环境可能不支持某种操作。
   → **方案要显式声明依赖**，缺依赖要能被识别出来。
3. **任务量不同成本不同**：用户希望派发多少 agent、能动用多少资源，都需要关注。
   → **方案里有资源计划**（agent 数量 / token 预算），创建时可调。
4. **环境配置怎么弄**：agent 测试过程中要用账号、要配置，**需要研究 qa-skill 怎么做**
   （→ 见 3.2 结论）。交互方式可能不一样；**遇到这种情况必须停下来等用户**。
   → **缺依赖时任务停在 blocked 态等人**，不硬跑、不瞎编。
5. **每个任务维度不同**：全局探索 vs 某一个需求的探索，参数完全不同。
   → **创建表单按任务类型专属**，不是一套通用模板。

### 1.4 两层架构（D-1 已裁：不冲突，不泛化）

**用户裁决**：任务与 25 号文是**两层**，各管一件事：

| 层 | 管什么 | 落点 |
|---|---|---|
| **25 号文 = 环节/方法层** | **生成方案的方法 + 具体执行过程 + 执行过程的方式**（怎么分析、怎么过门、怎么推进、怎么算通过） | `generation/state.py`（13 态）· `orchestrator.py` · 门⓪①② |
| **任务层 = 交互/状态层** | **梳理关系、跟用户对齐**（交互方式、实现状态、进度可见、什么时候叫人） | 本节五段生命周期 · 任务面板 |

⇒ **25 号文那套 13 态继续服务生成任务，不改、不泛化成"通用状态机"**。
任务层调用它，任务层态 ↔ 环节态是**包含关系**（一个 executing 任务内部在跑
analyzing→points_proposed→…），不是替代关系。

**任务层自己的阶段态**（新增，服务全部五类任务）：

```
draft ──创建──→ planning ──生成方案──→ planned ──确定──→ executing ──执行──→ completed
                    ↑                        ↑                   ↓              ↓
                    └──── 方案迭代 ──────────┘            blocked        applying
                                                            ↓              ↓
                                                        待人处理         applied
```

- `planning`：agent 在生成方案（可多轮）
- `planned`：方案已出，**等人确认**（任务层的停顿点，与 25 号文的门态语义同源但不同层）
- `executing`：执行中，可见进度
- `blocked`：**缺依赖等人**（新增态，对应理由 4）——25 号文没有这个语义
  （只有 `paused` = 人手动暂停，不是"agent 撞到缺依赖自动停"）
- `applying`：结果应用（转正式 / 生成变更包）
- 旁路：`paused` / `failed` / `cancelled`

### 1.5 统一任务面板

用户明确要「**任务面板**」——五类任务共用一个入口/列表，而不是各散一处：

- 按类型切 tab（探索 / 生成用例 / 生成场景 / 需求分析 / 测试执行）
- 列表项显示：状态、进度、当前阶段、**每个 agent 在做什么**、资源用量
- 顶部：新建任务入口 → 走该类型的「创建表单」

现有可复用：
- `mc_test_gen_run`（13 态 + `progress` 字段，25 号文 §3.3「前端唯一渲染源」）
- `gen-run-view.tsx`（进度条 + 树形提案 + 门①门②操作区 —— **可以改造成任务面板的一个 tab**）
- `agent_runtime.stream_chat()`（dsh SSE 逐事件输出 —— **"执行方案"实时可见的技术基础**）
- `mc_test_task`（执行类任务已有表：exec_type/schedule/env_id/global_params/notify）

**缺口**：
- 现有 `gen-run-view` 是「**跑完给你看结果**」，不是「**跑前先给你规划审**」。
  → 缺「生成方案 → 确定方案」这一段的 UI 与后端状态。
- 缺 `blocked`（依赖阻塞等人）态。
- 缺任务级的**方案实体**（谁来存"这份方案"）。
- 缺**关联任务**机制：直接改了数据、做完不知道怎么恢复、用户只能一个一个处理
  （见 1.8，D-6 裁决的一部分）。

### 1.6 各任务的「创建表单」维度（用户点名的）

| 任务 | 创建时用户要选/填的 |
|------|---------------------|
| **探索** | 探索目的（补齐用例/补齐需求/探索测试/其他）· 探索客户端 · 被探索的配置 · 说明 · 范围（全局/某需求/某模块）· 端（api/web/app） |
| **生成用例** | 哪些客户端（端）· 哪些模块 · 哪些功能 |
| **生成场景** | （同上，端相关） |
| **需求分析** | 分析哪些需求 |
| **测试执行** | 执行哪一些 · 执行方案 |

**「探索客户端」和「被探索的配置」** 的含义：探索用什么手段/工具去探
（浏览器？设备？HTTP 客户端？），以及探哪个目标（URL/接口集/应用包）。

### 1.7 探索任务：方案里要有什么

以探索为例，「生成方案」阶段 agent 要产出（用户可编辑）：

1. **角色分工**：各 agent 负责什么、怎么安排（谁定路径、谁真跑、谁判失败）
2. **探索方案**：具体路径/清单（如 `/login → /orders → /pay`）
3. **探索目的**与**对应关注点**（目的不同 → 关注点不同）：
   - 补齐用例 → 关注"哪些接口/页面还没有可信用例"
   - 补齐需求 → 关注"探索发现的未文档化行为 → 回写成需求"
   - 探索测试 → 关注"charter 驱动，发现缺陷"
4. **资源计划**：派几个 agent、token 预算
5. **依赖检查**：环境/凭据/客户端是否具备，缺的标红
6. **探索范围：全量 / 增量**（D-4 裁决）：
   - 全量探索 = 范围内所有接口/页面都探
   - 增量探索 = **按字段定位**（如"只探 `status=changed` 的接口""只探指定模块"）
   - 探索方法（怎么探）归入本任务流程内，不作为独立按钮

### 1.8 「暂存 → 确认 → 正式」与关联任务（D-6 裁决）

**用户裁决原文**：需求/用例/测试点**不落独立暂存分区**，**共用正常数据表**，
只**增加额外字段标识**；同时**做关联任务**——之前是直接做，
做了之后也不知道怎么处理、怎么恢复，用户需要**一个一个处理**。

⇒ **机制**：

1. **不建暂存表/不建暂存分区**。数据仍写 `mc_test_case` / `mc_test_requirement` /
   `mc_test_testpoint` 等**正式表**。
2. **加标识字段**（建议）：
   - `task_run_id`：由哪个任务产出（裸 UUID，无 FK）
   - `is_staged` / 或复用现有 `status`（`draft` 即未应用）
   - 列表默认过滤 `is_staged=true`，**只在任务视图里可见**
3. **应用 = 把标识翻正**（`is_staged=false`），之后全局可见。
   **不是"搬到另一张表"**。
4. **关联任务**：每条数据都挂 `task_run_id` → 从任务能反查它产出了什么，
   **用户能一个一个处理**（逐条确认/丢弃/修改），也能整批应用/回滚。

> 与 25 号文 `mc_test_proposal` 的关系：**生成链已有自己的暂存表**（`mc_test_proposal`），
> 那是 25 号文环节层的方法。D-6 说的是**任务层**的统一机制——
> 探索/需求分析等其他任务产出的资产，直接进正式表 + 打标识，
> 不再各建一套暂存表。两者并存，生成链维持原样（D-1 两层不冲突）。

---

## 第二部分 · 页面改造

### 2.1 需求页（已有页面，做结构化 + 增区块）

现状（`requirement-doc-page.tsx`，路由 `/testing/requirements/:reqId`）：
大标题 → 元信息小标签 → **单个富文本框**（占位文字「写需求描述：业务规则 / 背景 / 约束…」）
→ 验收标准（纯 textarea）→ 思维导图（`MindMapEditor`）→ 附件。

**问题**：九要素全塞在一个富文本框里；`requirement.analysis`（九步产物，含未知项）
**根本没渲染**。

**要做的**：

1. **九要素结构化**（用户原话列的顺序）：
   业务规则 · 背景 · 关联冲突需求 · 关联历史需求 · 使用用户与背景 · 约束 ·
   优先级 · 未知项 · 验收标准
   - 至少「未知项」「验收标准」要从自由文本变成**结构化字段**（其余可先在富文本里分段）
2. **未知项的处理**（用户原话）：
   - 标红显示，让用户去处理
   - 状态：`待确认` / `OK` / `有新的评论`
   - **数据来源已存在**：`analysis.register` 里 `kind=to_confirm` 的条目
     （`req_analysis.py` 已产出，**只是没渲染**）
   - 「有新的评论」→ 与评论系统联动
3. **覆盖导图**（新增区块）：`需求 → 测试点 → 用例` 三层展开
   - 数据源：`testpoint.list(requirement_id)` + `case.list(testpoint_id)`
   - 组件可复用 `RadialMindmap` / `MindMapEditor`
   - 目的（用户原话）：「很直观的看到每个需求从测试点到实际用例的一个情况」
4. **内嵌 agent 对话框**（见 2.4）

### 2.2 测试点页（新建）

现状：**完全没有页面**。后端有 `testpoint.list` 方法（`gen_methods.py:435`），
前端连封装都没有；测试点只在生成链的缩进树里作为可勾选提案出现
（`gen-run-view.tsx`，其注释已自认这个缺口）。

**要做的**：
1. 列表页 `/testing/testpoints` + 详情页
2. **测试点关联需求**（`requirement_id` 已有列）、**用例关联测试点**
   （`testpoint_id` 已有列）—— 数据层已通，缺 UI
3. **测试点的覆盖导图**：`测试点 → 它的用例（按端分组）`
4. 评论（见 2.3）
5. 内嵌 agent 对话框（见 2.4）

### 2.3 评论系统（新建，通用）

**范围**：需求、测试点。

**用途**（用户原话）：让不同的人进行评论，最终得出一个结论，
**将评论的历史记录下来**。

**现状**：60 张 `mc_test_` 表里**没有任何 comment 表**。最接近的 `mc_test_question`
是"提问/收敛"语义（流程一等公民、未解决阻塞下游），不是评论串。
缺陷的 `mc_test_bug_log.comment` 只是流转附言。

**设计要点**：
- 一张 `mc_test_comment` 表，`entity_type ∈ {requirement, testpoint}` + `entity_id`
- 支持**嵌套回复**（`parent_id`）
- 支持**结论**（收敛后标记一条为结论 / 或独立 `resolution` 字段）
- 全量留存历史
- 「待确认 / OK / 有新的评论」状态与之联动

### 2.4 内嵌 agent 对话框（新建，通用）

**用户原话**：
- 在**需求页、测试点页**：想改动需求、对测试点做事情、认为不合理时，
  跟 agent 沟通，agent 做对应调整 —— **不然在不同页面来回跳很麻烦**
- 在**用例页**：也要对话框，**需要把页面的一些参数给到 agent**，
  让它知道我在说什么

**现状**：
- `/testing/chat` 是**整页**专用组件（`chat-page.tsx`），无受控 props，不能内嵌
- `chat-adapter.ts` 是**纯逻辑层**（事件/历史 → 展示态），不含发送/SSE/输入
- 全站已有内嵌先例：`components/console/agent/embedded-agent-chat.tsx`
  （FloatingPanel 形态，市场管理页在用）—— **照它做**
- `mc_test_chat_message` 只挂 `session_id`，**没有 entity 外键** →
  对话无法绑定到具体的需求/测试点/用例

**要做的**：
1. 抽出可内嵌的 `EmbeddedAgentChat`（testing 版）
2. **上下文注入**：对话要带上"当前页面的哪些参数"（哪条需求、哪些字段、
   哪些未知项、当前用例内容）→ agent 才知道"你在说什么"
3. **对话与实体绑定**：`mc_test_chat_session/message` 加 entity 外键，
   或用 `confirm_ref` 之类的关联位
4. **agent 的修改也走"生成方案→确定→应用"**，不直接改库 —— 与任务模型一致

---

## 第三部分 · 环境与凭据（用户口径，与上文任务模型对接）

### 3.1 用户的两条纠正（重要）

**纠正 1：不存 `auth.token`，存密码；token 是运行时产物。**

正确模型（用户原话）：把请求提取出来，请求包括**前置 hook** 和**后置 hook**：

| 部分 | 职责 |
|------|------|
| **前置 hook** | 做登录 → 登录完成**缓存 token** → 全局使用直接读缓存 |
| **后置 hook** | 判断 token 是否过期 → 过期则去登录 → 然后**重新发送** |

**ws 同理**（连接建立/重连也走这套）。

因此**需要增加一个缓存操作，在步骤内**——用户原话：**"这个可以先空着，后续做技术讨论"**。

**落到平台已有实体上（D-2 已裁）**：**保留 `mc_test_transport`**，做在它的 `auth` 字段上，
**不做方法库「请求类」**。

用户原话：
> 保留这个，去掉之前说的请求方法库，改为使用这个。
> 主要我以为你没做这个，因为在 http 步骤你没有这个的选择。

**⇒ 现状核实（2026-10-22 复查）**：**表和方法齐了，但引擎没接线**——
这正是用户说"http 步骤没有这个选择"的原因。三处断点：

| 断点 | 证据 |
|------|------|
| 场景的 `transport_id` 引擎不读 | `executor.py` 搜 `transport` 只有 `_reinit_transport`（清空 auth 作用域，注释"真鉴权由传输层适配器接管"）与其调用点；**无任何代码加载 `transport.auth`** |
| http 步骤拿不到登录流程 | `tools.py:198-203` 只合并 `params.transport.common_headers` 与 `path_rewrite`——这个 `params.transport` 是**调用方手动塞进步骤参数**的，不是从 `transport_id` 自动加载 |
| `re_login_on` 无人执行 | 全仓搜 `re_login_on` **只有模型定义与文档**，无执行代码 |

⇒ **一期要补的接线**（属二期技术实现，见第四部分）：
1. 场景 `transport_id` → 引擎自动加载 `transport`（`common_headers`/`path_rewrite`/`auth`）
2. `auth.login_flow`（前置 hook）：执行登录 → 从响应提取 token → **缓存** → 写 `auth` 作用域
3. `auth.re_login_on`（后置 hook）：判过期 → 重登 → **重发**
4. **缓存操作**（步骤内 cache 存取）—— 用户原话："先空着，后续做技术讨论"
5. ws 同理（`kind=ws` 已有该字段位）

**注意**：hook 属于**传输层**（连接配置，被多场景复用），
**与环境变量表无关**——环境只提供**凭据真值**（密码），
hook 决定**何时登录、怎么取 token、怎么判过期**。

**纠正 2：加密问题本期不解决。**

用户原话：配置是读取用的、**不需要给 agent 看**，所以加密问题**应该不存在**；
但 app/web 上无法避免 —— **暂时不考虑，本期直接明文**；
后续研究技术方案，**不在本期范围**。

**⇒ 本期不做**：列级加密、app/web 的凭据保护。
**⇒ 本期做**：`is_secret` 的**读面脱敏**（已存在）保持不变；
服务端注入时用真值。

### 3.2 qa-skill 的研究结论（用户点名要求研究的）

**结论：环境/凭据这块没有可抄的，qa-skill 两库在这块很粗糙。**

- 纪要（`skill-library-research.md`）对 `petrkindlmann/qa-skills`（★187）与
  `PramodDutta/qaskills`（★237）**只列 skill 名，无环境/凭据/setup 正文**
- 深读件（vendored 在系统临时目录 `Temp/skills2/`）显示其做法就是
  `process.env.API_BASE_URL || 'http://localhost:3000'` +
  **把 Bearer token 字面量写死在测试代码里** —— 无凭据抽象
- 唯一像样的模式来自 qmax-mcp（R10）：
  **Playwright `storageState` 登录态文件 + MCP 工具参数传路径**
  （路径须在 workspace 内、禁绝对路径/穿越/符号链接，凭据值不回传）
- 可借鉴的另一个点：要求每项目一份 `.agents/qa-project-context.md`
  （技术栈/命名/选择器策略/已知风险区），agent 开工先读它
- **仓内已有一个更好的样板**（不用抄 qa-skill）：
  `mcp_runtime` cdp-bridge 的 `browser_fill_credential` ——
  agent 只表达"往这个框填密码"，**服务端解析真值、交给浏览器扩展打进输入框，
  值永不出现在 agent 的结果/对话/日志里**，agent 只拿回执。
  这正是"给他环境但不给他密码"的成熟形态。

### 3.3 环境如何进入任务（用户核心诉求：让 agent 有目标、有环境）

用户原话：**"让他搜索我们的环境，或者说在启动一些操作的时候就将这个给他放入进去，
这样他就有目标，有环境"**。

两个时机**都要，分工不同**：

| 时机 | 做什么 | 现状 |
|------|--------|------|
| **创建/规划** | agent 自己调 `env.list` / `env.vars.resolve` **搜环境** | ✅ 方法已存在，可直接被 agent 调 |
| **执行** | 选定的 `env_id` 冻结成任务参数，执行时**服务端注入** | ❌ 缺接线（见下） |

搜环境时 agent 看到什么：
- 非 secret 变量（base_url、开关）→ **真值**，直接写进探索方案
- secret 变量 → **`***1234` 形态**（够做依赖检查：知道有/没有、属哪个环境）

**⇒ 规划阶段的"依赖检查"就是 agent 搜环境搜出来的**，缺的自动标红等你。

**执行阶段要补的三条断线**（现状已核实）：

1. **`run.execute` 不自动解析环境**：`envs` 必须调用方显式传
   （`methods.py:1422/1451`）；`run_task` 虽把 `env_id` 存进 run 行
   （`tasks/service.py:487`），但注释明说"只建 run 行，不执行引擎"，
   **env_id → 环境链 → 引擎**这条线没接。
2. **`auth` 作用域无生产者**：生成门强制 `Authorization: {{auth.token}}`
   （`gates.py:382`），但引擎里 auth 只有清空（`executor.py:971`
   "真鉴权由传输层适配器接管"）—— **那个适配器不存在**。
   正确做法应是：**登录步骤拿到的 token 由服务端写入 auth 作用域**，
   而不是让配置里预存 token。
3. **secret 脱敏管线未接线**：`register_secrets`（`variables.py:862`）
   生产代码零调用 → 落库 redactor 脱不到环境密钥。

**app/web 的凭据注入**（本期不做，二期）：照 cdp-bridge 的
`browser_fill_credential` 模式做 —— agent 说"填这个框"，
服务端从 env 的 secret 变量解析注入，值不进 agent 上下文。

---

## 第四部分 · 分期

### 本期（一期）
- **任务框架**：任务层状态机（§1.4）+ 五段生命周期的后端骨架
- **任务面板**：统一入口，五类任务 tab，列表 + 进度 + agent 动态
- **创建表单**：按任务类型专属维度（§1.6）
- **生成方案 → 确定方案**的 UI 与后端（含方案实体、编辑、确认）
- **执行方案**的实时进度（复用 dsh SSE + `gen-run-view` 改造）
- **blocked 态**（缺依赖等人）
- **暂存 → 确认 → 正式（D-6）**：正式表加标识字段（`task_run_id`/`is_staged`）+
  关联任务 + 逐条/整批应用
- **探索范围选择**（D-4）：全量/增量（按字段定位）+ 探索方法归入任务流程
- **页面**：需求页九要素结构化 + 未知项标红/待确认/OK/新评论
- **页面**：测试点列表页 + 详情页 + 覆盖导图
- **页面**：需求页 / 测试点页 / 用例页的覆盖导图
- **评论系统**：表 + API + 组件（需求、测试点）
- **内嵌 agent 对话框**：需求/测试点/用例三处 + 上下文注入
- **环境对接**：agent 搜环境（已有方法，接到规划阶段）+ 依赖检查

### 二期（技术实现，本期不落）
- **transport 登录/重登接线**（D-2 裁决，见 3.1 断点表）：
  场景 `transport_id` 自动加载 → `auth.login_flow` 执行登录 → 缓存 token →
  写 `auth` 作用域 → `re_login_on` 判过期 → 重登 → 重发 → ws 同理
- **缓存操作**（步骤内 cache 存取）—— 用户原话："先空着，后续做技术讨论"
- `run.execute` 的 env_id 自动解析、secret 登记接线（`register_secrets` 生产零调用）
- 引擎 `page` / `device_control` 工具从 stub 做实（web/app 真跑）
- app/web 凭据注入（照 `browser_fill_credential` 模式）
- 密钥列级加密（`studio_deploy.py` 的 seal/open 搬过来）—— 明确不在本期

### 三期 / 待议
- 生成场景任务的完整形态
- 缺陷回流、报告等既有链路的任务化

---

## 第六部分 · 落地记录（2026-10-22 实做状态）

> 本节随实现滚动更新；以上各节是**设计口径**，本节是**已落地的事实**。

### 6.1 后端已落地

| 模块 | 文件 | 状态 |
|------|------|------|
| 任务层模型 | `testing_platform/taskflow/models.py` | ✅ TestTaskRun + 9 态状态机（含 `blocked`）+ 探索维度契约（`required_env_vars`/`scope_mode`/`direction`/`purpose` 取值域） |
| 任务层服务 | `taskflow/service.py` | ✅ create/plan/confirm/transition/progress/apply/revert + `assert_executable` 护栏 |
| 暂存标识 | `taskflow/staging.py` | ✅ D-6 标识列（task_run_id/staged）+ 空值硬护栏 |
| 探索编排 | `taskflow/explore_orchestrator.py` | ✅ 依赖检查→blocked、execute_fn 注入、应用收口 |
| 评论域 | `testing_platform/comments/` | ✅ 表 + 服务（只增不删留痕） |
| 方法注册 | `surface/taskflow_methods.py` | ✅ 15 方法：`taskrun.*`（9）+ `comment.*`（6） |

**方法命名**：统一任务层用 **`taskrun.*`**（既有 `task.*` 是执行域配置，撞名直接抛「重复注册」）。

**后端补的既有方法缺口**（共享文件编辑）：
- `testpoint.list` 加 `project_id` 入口 + `requirement_ids` 单独可用（反查项目，行为测试抓的真 bug）
- `testpoint.get` 新增（UUID/TP-xxxx code 双认）
- `case.list` 加 `testpoint_id` 过滤
- `requirement.get` 序列化 `analysis`/`review`/`gen_status`/`elements`（之前后端有、前端拿不到）
- `requirement.create`/`update` 接受 `elements`（九要素结构化）
- `mc_test_requirement.elements` 新列（`_ensure_additive_columns` 自动补）

### 6.2 前端已落地

| 页面/组件 | 文件 | 状态 |
|---|---|---|
| 任务面板 | `taskflow/task-panel-page.tsx` + 面板/徽章/步骤条 | ✅ 五类 tab + 列表 + 进度 |
| 探索任务 | `taskflow/explore-task-*.tsx`（5 个） | ✅ 五段全流程 |
| **通用任务视图** | `taskflow/generic-task-view.tsx` + `generic-task-create-form.tsx` | ✅ 生成用例/生成场景/需求分析/测试执行 四类共用（复用方案/进度/产物三个通用面板） |
| 评论组件 | `taskflow/comment-thread.tsx` | ✅ 已挂需求/测试点页 |
| 内嵌对话 | `taskflow/embedded-agent-chat.tsx` | ✅ 已挂需求/测试点/用例页 |
| 测试点页 | `testpoints/testpoints-page.tsx` + `testpoint-detail-page.tsx` | ✅ 列表 + 详情 + 覆盖视图 |
| 需求-分析产物 | `requirements/requirement-analysis-panel.tsx` | ✅ 未知项标红（`analysis.register to_confirm`） |
| 需求-覆盖链 | `requirements/requirement-coverage-panel.tsx` | ✅ 需求→测试点→用例三层 |
| 需求-agent | `requirements/requirement-agent-panel.tsx` | ✅ 上下文注入 |
| 需求-九要素 | `requirements/requirement-elements-panel.tsx` | ✅ 六文本域 + 手工未知项增删改 |
| 前端契约 | `api/taskflowClient.ts` + `api/testpointClient.ts` | ✅ 独立文件，避开 140KB 热文件 |

**路由**：`/testing/taskruns`、`/testing/taskruns/:taskId`、`/testing/taskruns/new`、`/testing/testpoints`、`/testing/testpoints/:tpId`（App.tsx）+ 导航项「任务中心」「测试点」（modes.ts）。

### 6.3 验证

- **后端**：77 个测试全绿（纯函数 + sqlite ORM 行为 + mock 回归 + 编排器）
  - 行为测试抓到 3 个真 bug（类型检查抓不到）：
    1. `_transition_or_raise` 写成 `async def` 但三处调用漏 `await` → 状态机护栏静默失效
    2. `apply_task_outputs` 在 applying 态会卡住（翻转与流转未解耦）
    3. `testpoint.list` 传 `requirement_ids` 单独用会报错（project_id 无从推导）
- **前端**：tsc 零错误（taskflow/ + testpoints/ + requirements/ + 修改的共享文件）；仓库既有的 ~110 个旧 tsc 错误是历史存量，与本批无关
- **未跑真 PG**：`staging.py` 用 PG 专有 SQL（`information_schema`/`$1`），sqlite 跑不了 → 那部分只 mock 测，真 PG 上的 `ensure_stage_columns`/apply/revert 待人工验一次

### 6.4 没做的（明确告知）

- ~~**生成场景/需求分析/测试执行** 三类任务的详情视图~~ → **2026-10-22 补齐**：`generic-task-view.tsx`（五段骨架复用 + 类型专属创建表单，`?type=` 决定初始类型）
- **transport 登录/重登接线**：D-2 裁了保留 transport，但引擎侧 `transport_id`→`auth.login_flow`→`re_login_on` 的执行链是二期技术实现，本期未做
- **`mc_test_api` 列接口 service 入口**：探索编排器的 `api_provider` 是占位（缺 `assets.service.list_apis`）
- **富文本→九要素迁移工具**：agent 评估后跳过（Tiptap 自由文档按标题拆命中率低）
- **真 PG 上的 staging 验证**：脚本已备好（`scripts/verify_taskflow_pg.py`，7 步断言自清理），因写库操作待人工跑一次
- ~~**`elements.related_*` 的消费方解析**~~ → **2026-10-22 完成**：`requirement-agent-context.ts`（纯逻辑模块，6 个 node:test 全绿）——UUID→`REQ-xxxx 标题` 映射（`reqLabels` 入参，由面板拉 `getRequirements` 构造）、九要素文本非空才进上下文、手工/AI 未知项分字段、旧字符串形态按逗号切分向后兼容。Agent 现在能读到「跟 REQ-0002 退款规则打架」而不是一串裸 UUID。

---

## 第五部分 · 待裁决项（D-1..D-6，2026-10-22 用户已裁）

> **用户裁决**：
> **D-1 已定**：不冲突，两层。25 号文 = **生成方案的方法与具体执行过程**
> （怎么分析、怎么过门、怎么推进）；任务层 = **梳理关系、跟用户对齐**
> （交互方式、实现状态、进度可见）。25 号文那套状态机继续服务生成任务，
> 作为任务层的一个具体实现，**不需要泛化成通用状态机**。
>
> **D-2 已定**：保留 `mc_test_transport`，**不做方法库请求类**。
> 登录/重登（前置 hook 登录→缓存 token / 后置 hook 判过期→重登→重发）做在
> `transport.auth` 上。用户此前误以为未实现，实测**表+方法齐但引擎未接线**
> （见 3.4），故一期要补接线。
>
> **D-3 已定**：**环节与任务不冲突，是两层**。用户原话：
> "环节应该都是独立的，不是说能一步到尾的；环节只是说有这些事情，
> 任务只是说怎么去做某一个细节——交互方式、实现状态。"
> ⇒ 25 号文的五个环节**保持独立**（不合并成一个大流程），
> **任务 = 对某一个环节细节的一次具体执行**（怎么交互、状态怎么走、进度怎么显示）。
> 另：用户指出"不能一上来就框框一顿干"——这正是任务层存在的理由
> （先规划、先对齐、再执行）。
>
> **D-4 已定**：`explore.plan` / `explore.record` 的本意是「**用例是否经过探索**」。
> 探索任务化后，探索时选**全量探索 / 增量探索**（按字段定位范围），
> **探索方法归入任务流程内**（成为探索任务的执行方式之一），
> 不再作为独立按钮挂在用例详情页。
>
> **D-5 已定**：本期**明文** + 读面脱敏（`is_secret` 已实现），
> 列级加密不在本期范围，后续研究技术方案。
>
> **D-6 已定**：需求/用例/测试点**不落独立暂存分区**，**共用正常数据表**，
> 只**增加额外字段标识**（如 `task_run_id` / `is_staged` / `status`），
> 并且**做关联任务**——用户需要一个一个处理，而不是之前"直接做、
> 做完不知道怎么恢复"。

---

## 附：已核实的现状事实（本次调研，带出处）

**UI**
- 需求有独立列表页 `requirements-page.tsx`（`/testing/requirements`）
  与详情页 `requirement-doc-page.tsx`（`/testing/requirements/:reqId`）
- 测试点**无任何页面**；前端未封装 `testpoint.list`
- 思维导图三组件：`MindMapEditor`（读写 `requirement.mindmap`，唯一）·
  `RadialMindmap`（现场拼数据，与 requirement.mindmap 无关）·
  `TransportMindmap`（接口树）
- 用例页 `cases-page.tsx` + 详情弹框 `case-detail-panel.tsx`（三 Tab：概览/步骤/关联）
- **无任何评论功能**（60 张 `mc_test_` 表无 comment 表）
- `chat-page.tsx` 是整页专用，无受控 props；`chat-adapter.ts` 是纯逻辑层
- 全站内嵌先例：`components/console/agent/embedded-agent-chat.tsx`

**后端**
- `mc_test_gen_run`（13 态 + `progress` + `scope` + `budget` + `decisions`）
- `mc_test_proposal`（AI 唯一写入口，暂存区；`kind` ∈ testpoint/case/dataset）
- `mc_test_testpoint`（`requirement_id`/`surfaces`/`basis`/`batch_tier`/`case_review_state`）
- `mc_test_requirement`（`analysis` 九步产物 / `review` / `gen_status` 12 态 /
  `mindmap` / `acceptance_criteria` / `priority`）
- `mc_test_transport`（`auth` JSONB / `common_headers` / `path_rewrite` / `kind` http|ws）
  —— **引擎未接线**：`executor.py` 只有 `_reinit_transport`（清 auth 作用域），
  无加载 `transport.auth` 的代码；http 工具只合并调用方手动塞的 `params.transport`
  （`tools.py:198-203`）；`re_login_on` 全仓只有定义无执行
- `mc_test_scenario.transport_id` 列存在但注释"先留占位"（`assets/models.py:983`）
- `mc_test_method_module`（一个脚本=一个类，`code`=类名，`project_id=null` 全局内置）
  + `mc_test_method`（`full_ref`=`Class.method`，`signature` JSONB）
- `mc_test_env_variable`（`is_secret` + 读面脱敏 `mask_secret_value`）
  + `resolve_environment_vars`（继承合并，原生类型）
- 引擎五作用域 `local > data > auth > env > global` + 临时 `response`/`result`
- `register_secrets` 生产零调用；`auth` 作用域无生产者
- `run.execute` 不自动解析 env_id（`envs` 由调用方显式传）
- 引擎 `page`/`mobile`/`android`/`ios`/`custom`/`sql`/`ws` 均为 stub
