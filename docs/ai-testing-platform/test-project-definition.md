# 测试项目与知识引擎 · 定案（2026-10-03）

> 本文是**根设计**，纠正之前三处理解偏差。后续所有数据模型/接口/页面以此为准。

## 一、"测试项目"的定义（关键纠正）

**测试项目 ≠ 被测系统，也 ≠ coding 项目。**

| | 被测系统 | coding 项目 | **测试项目** |
|---|---|---|---|
| 是什么 | 庞大的被测对象世界（你们整个平台） | 只站**开发维度**（代码/issue） | **整个项目维度**（开发+产品+测试+管理） |
| 能否被概括 | ❌ 不能 | 能 | 能——它是**测试工作的项目容器** |
| 关系 | — | 两个东西，**不混** | 独立实体，**全新表，与 coding 零重合** |

**测试项目 = 围绕测试组织起来的项目实体**，它承载全维度信息，不同角色（开发/产品/测试/管理）从不同角度切入——**身份是其次，项目是主体**。

**初期完全独立**（不复用 coding 任何表）；将来可让 coding 作为子集接入。

## 二、测试项目**不拥有代码，只配 git**

- 测试项目**不存代码**，只存 **git 配置**（1-N 个仓库：URL/branch/凭据引用）
- 需要代码时**去拉快照**（服务端 clone）→ 跑 Graphify → 图谱
- 代码、需求、用例、管理 —— **全是测试项目自己的新表**

## 三、版本是横切维度（关键补充）

同一测试项目，**不同版本知识不同**：
- 代码图谱：不同 commit/tag
- 需求：不同 PRD 版本
- 用例：随版本演进

**每条知识带 `version` 标签**，支持**版本间 diff** → 决定"哪些用例要重测"（增量测试根基）。之前的 `stale` 只是单点失效，不够。

## 四、四层缺口的定论（用户点破）

之前只做了内部 adapter，**对外四层全缺**：

| 层 | 缺什么 |
|---|---|
| **数据层** | 测试项目表、源配置表、版本表、测试知识表（bug_pattern/用例关联/profile） |
| **接口层** | 初始化图谱/更新图谱/扫描用例/读取PRD 的全套方法 |
| **AI 接入层** | PRD 读取、bug 归纳、锚定（用 dsh） |
| **页面显示层** | 项目配置、图谱查看、导入、可靠性仪表 |

## 五、数据源是"适配器"问题（用户关注的外在形式）

外面千姿百态，内部归一：

**用例源**：XMind 思维导图 / 禅道 / 自家平台 / 数据库 / Excel / 通用 JSON 兜底
**PRD 源**：飞书 / 钉钉 / Word / 内部文档 / 通用文本兜底
**代码源**：1-N 个 git 仓库

**统一原则**：每种外部形式 = 一个**源适配器**，只做"拉取+解析成中间格式"，**抽取（规则/锚定）走统一流水线**。

## 六、同步触发（用户问的"定时还是 git 更新"）

每种源配自己的触发，不一刀切：
- **手动**（所有源，页面点）
- **定时**（PRD/用例，cron，复用平台 scheduler）
- **git push webhook**（代码源，仿 `webhook_review_worker`）
- **按需**（导入文件后立即触发）

代码图谱更新：push → 拉 diff → Graphify 增量重抽 → 锚点 hash 变 → 沿边标 stale。

## 七、可靠性对外呈现（用户问的"图谱是否可靠"）

**门禁的对外呈现**，不隐藏：
- **来源徽章**：`deterministic`（Graphify，可信）vs `ai`（dsh，需确认）
- **置信度**：`EXTRACTED(1.0)` / `INFERRED(0.55-0.95)` / `candidate`
- **锚定状态**：锚到代码=confirmed，锚不到=candidate
- **stale**：代码变了没更新 → 标红"可能过期"

页面每条知识带这些标记，一眼看出哪条可靠。

## 八、已确认的架构决策（2026-10-03）

用户确认：

1. **数据源双轨**：
   - **A 轨（内置适配器）**：确定性格式纯代码解析——excel / csv / json / markdown / word(.docx) / xmind / git
   - **B 轨（Agent 通道）**：平台**不写解析器**，只提供**取内容**的通道（接口 / MCP / Skill 三种任选）——飞书 / 钉钉 / 禅道 / Web URL / 内部 API / 任意 DB
   - **B 轨即 analyst 工作流**：`source.fetch → AI 解析 → knowledge.ingest（过锚点闸+可测闸）`——"读 PRD"和"读飞书里的 PRD"是同一条路
2. **版本 = 人工版本号 + 可选 git tag**：`mc_test_project_release` 是**版本指针快照**（记录"v1.2 时各源分别是什么版本"），支持 release 间 diff → 增量测试根基
3. **图谱 = 力导向图**：选 **react-force-graph-2d**（2D Canvas 高性能 + 节点/边交互 + Graphify graph.json 格式直接兼容）
   - 节点**颜色**=类型（代码锚点/规则/用例/bug）
   - 节点**透明度**=可靠性（confirmed 实色 / candidate 半透明 / stale 红边）
   - 点击节点 → 详情面板（置信度/来源徽章/锚定状态/版本）

## 九、实施阶段

| 阶段 | 内容 | 产出 |
|---|---|---|
| **1** | 测试项目 CRUD + git 配置 + 代码源 init/sync + 图谱 query | 能看到真代码图谱 |
| 2 | 双轨数据源（内置 Excel/JSON + `source.fetch` 通道）+ 用例扫描 | 能导入数据并锚定 |
| 3 | PRD agent 摄取（analyst 工作流）+ 锚定 + 可靠性 | 三源齐 |
| 4 | 版本 release + diff | 增量测试能做 |
| 5 | 力导向图前端 + 可靠性仪表 | 可视化 |

## 十、阶段 1 数据模型（4 张新表）

**`mc_test_project`**（测试项目）
`id, code(TP-xxxx), name, description, status(active|archived), version, created_by, created_at, updated_at`

**`mc_test_project_git`**（git 配置，1-N）
`id, project_id(FK), name(仓库名如 backend), repo_url, ref_type(branch|tag|commit), ref_value(可空——分支属于版本), credential_ref(JSONB 凭据引用不存明文), status, created_at`

**`mc_test_project_source`**（源配置）
`id, project_id(FK), kind(prd|case|code|openapi), adapter(builtin|agent) ←A/B轨, adapter_type(excel|word|json|xmind|feishu|dingtalk|zentao|custom), name, config(JSONB), sync_policy(manual|scheduled|webhook), schedule(JSONB), last_synced_at, status, created_at`

**`mc_test_project_release`**（版本指针快照，**deprecated**）
`id, project_id(FK), version_code(人工填 v1.2), git_tags(JSONB [{git_id,tag}]), prd_versions(JSONB), case_versions(JSONB), note, created_by, created_at`

**`mc_test_project_version`**（版本一等实体）
`id, project_id(FK), version_code(UNIQUE/项目), name, repos(JSONB [{git_id, ref_type, ref, is_primary, resolved_commit}]), is_initial(首版自动 true), status(draft|graph_built), note, created_by, created_at, updated_at`

## 十一、版本一等实体（2026-10-08 流程修正）

**用户点破的流程错误**：知识图谱应跟**版本**绑定，不是跟项目绑定。

正确流程：

```
建项目 → 配可用仓库（项目只管"有哪些仓库"）
       → 建版本（版本去选仓库分支，每仓各自 branch/tag/commit，多仓多选）
       → 代码 diff → 生成该版本的知识图谱
```

* **项目**：只管仓库集合（`mc_test_project_git`）——不含分支。
* **版本**（`mc_test_project_version`）：每仓选各自 ref；`is_primary=true` 的仓是主分支
  （master/main，代表该版本的生成状态）；首版自动 `is_initial=true`（初始版本必然存在）。
* **图谱按版本过滤**：`mc_knowledge_anchor/item/edge` 加 `version_id`（进唯一身份）；
  构建时逐仓 `init_snapshot(ref_override)` → Graphify → `upsert(version_id)`，
  resolved commit 回写版本行（可复现）。切版本 = 看那个版本的图谱；旧版本保留可查。
* **用例/缺陷**：加 `module`（分组维度）+ `version_id`（关联版本）。
* **入口**：侧栏项目选择器下方加「版本」下拉 + 齿轮（版本管理弹框）；项目管理页
  （项目选择器旁齿轮）收编可用仓库配置 + Git 身份管理。

**与 coding 项目零重合**，全新表；将来 coding 才作子集接入。


## 十二、需求主脊（2026-10-09 流程修正）

**用户点破的核心**：知识图谱应以**需求**为中心，而不是以代码为中心。现在图谱是代码
符号力导向图（function/class/file + 调用边），看不出"这条数据是干啥的、属于哪个需求"。

**同心分层**（从内到外）：

```
        需求描述 · 业务规则 · 代码 · 测试用例 · bug   ← 最外层（产出）
                     需求点                        ← 第 3 层
                      版本                         ← 第 2 层
                  当前测试项目（平台）              ← 圆心
```

即 **项目 → 版本 → 需求 → {需求描述/业务规则/代码/用例/场景/接口/bug}**。用例页的
思维导图也按同一分层显示。**一切挂到需求上，需求是主脊**——每条数据都知道"服务于哪个需求"。

### 数据模型

**`mc_test_requirement`**（需求，主脊）
`id, code(REQ-xxxx/项目内自增), project_id, version_id(→ mc_test_project_version 归属版本), title, description, content(JSONB 富文本正文), mindmap(JSONB 思维导图树), acceptance_criteria(JSONB [{text}] 验收标准=可测判据), priority(P0-P3), module(业务标签), folder_id(→ mc_test_catalog type=requirement 归属目录，裸 UUID 无 FK，删目录 service 置 NULL 归根), origin(manual|from_doc|from_import), source_ref(JSONB 溯源), status(draft|active|deprecated|archived), version, created_by, created_at, updated_at`
UNIQUE(project_id, code)；indexes(project_id, version_id, module, folder_id)。

> **需求分组 = 目录树**（2026-10-15，用户要「分组同用例页」）：分组走真目录表
> ``mc_test_catalog``(``type=requirement``)，`folder_id` 指向它；`module` 降为业务标签。
> 前端需求文档页左树（可增删改嵌套，复用 `catalog-dialogs` + 同构于 `case-tree-model` 的
> `requirement-tree-model`），选中目录过滤需求列表；列表页也按目录全路径分组。

> **`description` = `content` 的纯文本投影**（2026-10-14 需求文档化）：需求正文改成富文本
> （Tiptap/ProseMirror 文档 JSON 存 `content`，真相源）；写入时后端用 `doc_to_text` 派生
> `description`（AI 生成提示词 / 列表预览 / 向后兼容读它）。老数据无 `content` 时回退 `description`。

**`mc_test_requirement_attachment`**（需求附件，需求=文档：Word/导图/图片/文件）
`id, requirement_id, project_id, name, url(OSS), mime_type, size_bytes, kind(file|image|docx|xmind), source_ref, uploaded_by, created_at`
indexes(requirement_id, project_id)。文件本体在 OSS（浏览器直传拿 URL，服务端不碰字节）；
删需求只解关联（`requirement_id` 置空），附件行与 OSS 对象不动。

### 挂接方式（各表加列，非关联表）

`mc_test_case` / `mc_test_scenario` / `mc_test_transport` / `mc_test_bug` 各加
`requirement_id UUID NULL` → 指向 `mc_test_requirement`。存量行 NULL（=未关联需求）。
启动期 `_ensure_additive_columns` 自动补列。

### 能力面

* `requirement.create/list/get/update/delete`（update 走**变更门**：change_reason 必填；
  create/update 支持 `content`/`mindmap`）。
* **`requirement.trace`**（读）：给一个需求 → 返回它挂的全部产出（用例/场景/接口/缺陷）。
* **`requirement.attachment.list/add/delete`**（读/写/写）：需求附件（URL 由浏览器直传 OSS）。
* **`project.radial_graph`**（读）：一次拉全项目分层数据 → `{project, versions[], requirements[], leaves[]}`，
  供图谱页同心圆渲染。

### 需求文档导入（multipart，2026-10-14）

能力面新增收文件路由（挂在 `/api/v1/testing/requirement/import/*`，父 router 已 include，main.py 不动）：

* `POST /requirement/import/docx`：`.docx` → **python-docx** 按文档顺序遍历段落/表格，
  标题样式 → `<h1..h6>`，产出 **Tiptap 文档 JSON** + 纯文本（前端 `setContent` 进正文编辑器）。
* `POST /requirement/import/xmind`：`.xmind` → 复用 `knowledge.source_fetch._parse_xmind`
  （XMind Zen `content.json` / XMind 8 `content.xml`），转成 `{id,text,children}` 思维导图树。

守卫：后缀白名单 + 20 MiB 上限；解析失败 422（与能力面错误口径一致）。

### 前端

* **图谱页**：默认「需求图」（径向同心分层，`@xyflow/react` 手算极坐标：版本圈 r1 →
  需求圈 r2 → 产出圈 r3）；保留「代码图」（力导向）作为可选视图。
* **用例页**：视图切换（列表 / 思维导图），思维导图复用同一径向组件（项目→版本→需求→用例）。
* **需求列表页**（`/testing/requirements`）：只当**入口**（按 module 分组 + 版本过滤）；
  点行/编辑 → 进需求文档页。
* **需求文档页**（`/testing/requirements/:reqId`，`new`=新建）：**飞书文档式**全屏编辑——
  左需求树 + 右侧文档体（大标题 + Tiptap 富文本正文 + 思维导图编辑器 + 附件面板 +
  Word/XMind 导入）。富文本图片粘贴走 OSS 上传；思维导图复用 `@xyflow/react`（可编辑树）。
* 用例/场景/接口/缺陷的编辑弹框加「关联需求」下拉。


## 十三、代码图谱 / 接口生成 / 分支（2026-10-09 澄清）

### 代码图谱 = 挂版本（不挂需求）

**澄清**：代码图谱（Graphify AST 抽取的锚点/条目/边）**归属版本**（`version_id`），
**不挂需求**。这样"构建时不知道构建的是啥"有了答案：**它属于某个版本**——是"这个版本
的代码里有什么"。需求是后加的意图层，通过**同版本**看到该版本的代码图谱。

- **没有需求也能构建**（先有代码、后有需求是常态）。
- 需求 → 版本 → 代码图谱：需求经 `version_id` **间接**关联到代码图谱，不需给锚点加
  `requirement_id`。
- 若将来要"需求↔代码符号"的精确映射，另建映射表由 AI 按需补（本轮不做）。

### 「构建代码图谱」入口

- 命名：**构建代码图谱**（不再叫"构建图谱"——那是知识图谱的歧义）。
- 位置：从「版本管理弹框」**移到「知识图谱」页**（按钮针对**当前版本**）。
- **可重复构建**：`project.version.build_graph` 逐仓 `init_snapshot` → Graphify →
  `upsert(version_id)`，upsert 幂等（同锚点重复构建 = 更新），可多次重跑。

### 「从仓库导入接口」= AI 读仓库生成

**纠正**：不是"确定性扫描 + AI 补 description"，而是 **AI 读仓库为主体**。

- 新方法 `transport.generate_from_repo`（execute，异步）：`init_snapshot` → 把 Agent
  的**工作目录设为快照目录**（Agent 直接读代码）→ 驱动 Agent 读源码理解对外 HTTP
  接口 → 经 MCP `transport.create/update` 落库（code 幂等）。
- 方法论提示词：`testing_platform/prompts/interface_generation.md`。
- 前端 `repo-import-dialog.tsx` 改为「AI 读仓库生成」（选当前版本 + 额外要求 →
  轮询 `workflow.get`）。
- OpenAPI 规格文件导入（`transport.import_openapi`）**保留**，作为独立的「以接口方式」
  入口（用户：接口有"手工/规格"和"AI 读仓库"两种来源）。
- `transport.import_from_repo`（确定性扫描器）保留为能力面方法，前端不再主推。

### 远端分支下拉（修 "couldn't find remote ref main"）

**根因**：版本仓库项的 `ref` 被硬编码兜底 `"main"`，远端默认分支是 `master` 时
`git fetch origin main` 报 128。三处硬编码：`_normalize_version_repos`（`git.ref_value or "main"`）、
前端表单（`git.ref_value || "main"`）、`_detect_default_branch` 兜底。

**修法**：
- 后端 `_normalize_version_repos` **不再兜底 "main"**——留空则 `init_snapshot`
  探测远端默认分支。
- 新读方法 `project.git.branches`（`git ls-remote --heads` + `--symref HEAD`）→
  版本表单分支改成**远端真实分支下拉**，默认选远端默认分支。用户选的永远是远端存在的 ref。

### ref 自愈（修存量脏数据）

上两条只防"以后不再产生脏 ref"。**库里已有的脏 ref**（版本行 `repos` JSONB 里存着
`main`、远端却是 `master`）仍会让 `git fetch origin main` 报 128。`init_snapshot`
加自愈：

- 拉 fast 前先 `ls-remote --heads --tags <url> <ref>` 校验 ref 真在远端；不在
  （且非 sha 形态）→ 探测远端默认分支替换。
- fetch 仍报 "couldn't find remote ref"（TOCTOU / 服务端差异）→ 再探一次默认分支重试。
- 返回 `ref_corrected=True` + `requested_ref`；`project.version.build_graph` 据此把
  **纠正后的 ref 写回版本行**——一次修好，之后 `ls-remote` 校验恒过。

> ⚠️ 后端代码改动需**重启 Python 服务**才生效（dsh 子进程、FastAPI 进程都加载的是
> 改动前的模块）。改了不重启 = 跑的还是会 128 的旧逻辑。




