# 09 · 知识体系建设

**状态：设计** ｜ 层位：**平台级底座 + 测试派生视图**（公共资产，不限测试域）

> 之前只写了"三源 + 知识≠用例"，没写**怎么建**。本文写实：进来 → 存成什么 → 怎么关联 → 怎么查 → 怎么保鲜 → 完整例子。

---

## 1. 两层知识（先分清归属）

```
┌─────────────────────────────────────────────────────┐
│  测试派生视图 (Test Knowledge Engine)                │  ← 测试域
│  test_profile / 覆盖视图 / 缺陷模式 / 测试历史        │     由底座派生
├─────────────────────────────────────────────────────┤
│  公共知识底座 (Software Knowledge Graph)             │  ← 平台级
│  需求 · 规则 · 接口 · 代码 · 数据 · 依赖 · 文档       │     开发/测试共用
└─────────────────────────────────────────────────────┘
```

- **底座**：对"系统真相"的认识。**开发也能用**（查接口、查依赖、查业务规则）。
- **测试视图**：在底座之上派生（覆盖缺口、缺陷模式）。**只读底座，不回写底座**（回写走正规摄取）。

**关键**：测试视图**不是**知识的家，只是"以测试视角看底座"的一种投影。

## 2. 四层存储（从原始到可用）

```
L1 原始层 (Raw)        不可变的原文快照 + 溯源
      ↓ 解析/抽取
L2 结构化层 (Item)     结构化知识条目（规则/接口/组件/表…）
      ↓ 关联
L3 图谱层 (Graph)      条目 ↔ 锚点 ↔ 条目的关系
      ↓ 派生
L4 视图层 (View)       面向消费者的投影（画像/覆盖/影响面）
```

| 层 | 存什么 | 可变性 |
|---|---|---|
| L1 原始 | 文档快照、代码快照、API 规格原文 | **不可变**（新版本 = 新行） |
| L2 结构化 | 抽取出的知识条目 | 可更新（带版本） |
| L3 图谱 | 节点 + 边 | 随 L2 更新重建局部 |
| L4 视图 | 画像、覆盖、影响面 | **纯派生**（可随时重算，不手工维护） |

> **L4 不手工维护**——它是查询结果，不是数据。这是"数据只有一份真相"的保证。

## 3. 知识条目类型（L2 具体表）

**`mc_knowledge_item`** 统一表 + `kind` 区分（避免几十张表）：

| kind | 来源 | 关键字段（`content` JSONB 内） |
|---|---|---|
| `requirement` | 产品 | `title, description, acceptance_criteria[], priority, source_ref` |
| `rule` | 产品/代码 | `statement, condition, expected, module, refs[]` |
| `api_endpoint` | 代码/API 规格 | `method, path, params[], request_schema, response_schema, status_codes[]` |
| `component` | 代码 | `name, kind(class/function), signature, params[], returns, module` |
| `db_table` | 代码/DB | `name, columns[], indexes[], relations[]` |
| `mq_topic` | 代码/配置 | `name, direction, payload_schema` |
| `config_key` | 代码/配置 | `key, type, default, description` |
| `dependency` | 代码 | `name, version, kind(lib/service), purpose` |
| `doc_ref` | 文档 | `title, section, summary, uri` |
| `bug_pattern` | 测试 | `symptom, root_cause_kind, prevention` |
| `coverage_fact` | 测试 | `anchor_ref, covered_by[], gap[]` |
| `glossary` | 文档 | `term, definition, aliases[]` |

**统一字段**（每条都有）：

| 字段 | 说明 |
|---|---|
| `id` / `project_id` | |
| `kind` | 上表 |
| `key` | 稳定标识（如 `POST /api/v1/orders`、`OrderService.create`） |
| `content` | JSONB（按 kind 结构化） |
| `anchors[]` | 关联的代码锚点 |
| `source_ref` | 来自哪个源（→ L1 原始层） |
| `provenance` | 抽取方式（deterministic / agent）、时间、抽取器版本 |
| `confidence` | 置信度（agent 抽取 < 确定性抽取） |
| `status` | `candidate` / `confirmed` / `stale` |
| `version` | |

## 4. 锚点方案（具体怎么做）

> 锚点是**对齐层**：让"产品知识/技术知识/测试知识"能在同一坐标上对上。**不用向量相似度，用精确锚点。**

### 4.1 锚点身份

```
anchor = (project_id, kind, path, symbol)
```

例：`(proj_1, endpoint, "app/routes/orders.py", "POST /api/v1/orders")`

### 4.2 怎么产生（tree-sitter 自建轻量索引）

| 锚点 kind | 来源 | 解析方式 |
|---|---|---|
| `endpoint` | 路由定义 | tree-sitter 找路由装饰器/注册调用 |
| `function` | 函数/方法 | tree-sitter 符号表 |
| `class` | 类/组件 | tree-sitter 符号表 |
| `route` | 前端路由 | 路由配置文件解析 |
| `table` | DB schema | 迁移文件 / schema 文件 |
| `config_key` | 配置 | 配置文件解析 |

**流程**：

```
项目源码 (worktree)
  → tree-sitter 解析（按语言选 grammar）
  → 提取符号（name / kind / path / line / signature）
  → 生成 anchor 行（含 content_hash）
  → 入 mc_knowledge_anchor
```

**为什么不直接用 codegraph**：它是本地 IDE 工具（Node 单机、服务端零引用）。我们**参考它的数据模型**（符号/调用关系/文件路径怎么表示），但自建**按项目的小索引**。若用户本地有 codegraph，可作**输入源之一**导入，但平台不依赖它运行。

### 4.4 与 CodeGraph 的调研结论（2026-09-26 核实）

调研对象：`github.com/colbymchenry/codegraph`（MIT，72k star）。结论：**借鉴模型，不直接集成**。

**可直接抄的**：

| 借鉴点 | 内容 |
|---|---|
| 节点/边模型 | 22 种节点类型 + 12 种边类型（`calls/imports/extends/implements/references/instantiates/overrides/decorates/contains/type_of/returns/exports`），与我们的 anchor/edge 高度同构 |
| **route 一等节点** | 识别 17 个框架路由（FastAPI/Django/Express/Vue/React Router…）。我们的 `endpoint` 锚点照此做 |
| **合成边 + provenance** | 动态分发（回调/JSX/跨语言桥）用 `provenance:'heuristic'` 合成边补，**诚实标注置信度**——我们应照此，边带 `provenance` + `confidence` |
| 解析路线 | tree-sitter + 按语言 extractor + 两阶段（抽取→消解）+ `content_hash` 文件指纹 + 防抖 watcher；Python 侧用 `tree-sitter` Py 包即可 |
| 边元数据 | `metadata.resolvedBy`（exact-match/instance-method/import/framework/fuzzy…）记录消解策略——我们保留这个可诊断性 |

**不能照搬的（硬缺口）**：

| 缺口 | 说明 |
|---|---|
| **ID 不稳定** | 它的 `id = sha256(路径:kind:名:起始行)`，**行号漂移即换 ID**。我们的 `(project_id, kind, path, symbol)` 更稳，**不照搬其 ID**；若要读它的索引，以 `qualifiedName` 对齐而非 `id` |
| **形态错配** | Node 单机单用户工具；无 Python 包、无 HTTP 服务模式。我们 Python 多租户服务端只能 subprocess 调 CLI 或自运维 Node sidecar——**不进程内集成** |
| **锚点类型不全** | 只有代码符号 + 路由；**不抽 `table` / `mq_topic` / `config_key`**（需专用解析器另做），`api_endpoint` 也不抽 params/schema/status_codes 结构化字段 |
| **无版本/保鲜语义** | 只有 `updatedAt`；无版本链、无 stale 传播、无 L1 原始快照——我们的 L1→L4 与 stale 传播**全部自建** |
| **无向量检索** | 纯 FTS5 BM25 + 模糊 + 分词（**无 embedding**）；概念查询（"空库存下单"→锚点）补不上，我们仍需另选向量方案 |
| 精度天花板 | 无类型信息，消解靠名字/导入启发式（exact-match confidence 仅 0.4）；反射/DI/动态分发是盲区 |

**定位**：**可选输入源**——用户本地有 `.codegraph/` 时，把它 `--json` 输出（`query/callers/impact/files`）当**调用关系补强源**（尤其 `calls`/`impact` 边与 `route` 节点），对应 `source_type=codegraph`。需做版本探测（`indexed_with_version`）与行号漂移容错。

**运维坑（供参考）**：每项目一个本地库；>1MB 文件跳过；vendored 目录要手写 `exclude`；Windows↔WSL 不能共享同一 `.codegraph/`；本仓 6.6GB 库是 WAL 泄漏膨胀（真实数据 ~350MB），1.6.0 已修。

### 4.5 与 Graphify 的调研结论（2026-09-26 核实）

调研对象：`Graphify-Labs/graphify`（Apache-2.0，Python 包 `graphifyy`）——"文件夹（代码+文档+PDF+图片）→ 知识图谱"，GraphRAG 的轻量本地替代。结论：**借鉴机制，不直接用**。

**最有价值的部分：它怎么关联"文档↔代码"（正是我们最缺的）**

| 机制 | 做法 | 置信度 |
|---|---|---|
| **Markdown 符号引用解析** | 文档里的 `` `Widget` `` / `render()` / `pkg.Widget` / `mod.py::Widget::render` 抽成引用边，解析为 `文档 --references--> 代码节点` | **路径限定 = EXTRACTED**；**裸名唯一匹配 = INFERRED 0.95**；**歧义名直接丢弃** |
| **Rationale 节点** | docstring + `# NOTE:/IMPORTANT:/WHY:/HACK:/TODO:` 注释抽成节点，`rationale_for` 边连所属代码 | EXTRACTED |
| **ADR/RFC 引用** | 注释里的 `ADR-0011` / `RFC 793` 归一化为 `doc_ref` 节点 → 代码 --references--> ADR | EXTRACTED |
| **LLM 语义边** | `semantically_similar_to`（如"代码里的类 ↔ 论文里同一算法的概念"） | INFERRED 0.6–0.95 |

**我们要抄的三条设计**：

1. **三级置信度**：每条边标 `EXTRACTED(1.0)` / `INFERRED(0.55–0.95 五档)` / `AMBIGUOUS`，报告里展示置信度分布——**诚实标注"这是解析出来的还是猜的"**
2. **歧义即丢弃**（原话："an ambiguous name is a guess, and a guess is not an edge"）——召回换精确，与我们的门禁哲学一致
3. **注释/ADR 是一等公民**：`rationale_for` / `references` 边把"为什么这么写"接进图——**这是文档↔代码关联最便宜可靠的入口**

**其它可借鉴**：SHA256 缓存 + `manifest.json`（相对路径、可提交、队友 pull 后只重建差异）双轨增量；`graph.json` 开放产物 + MCP 查询接口；Leiden 社区发现 + god nodes + 跨社区"惊喜连接"。

**硬缺口**：

| 缺口 | 说明 |
|---|---|
| **无需求追踪语义** | 只有"引用级"关联（符号引用 + 语义相似）；**无 requirement/testcase/coverage 节点类型、无追溯矩阵、无覆盖率概念**——直接用会导致核心能力缺失 |
| **查询纯词法** | label 匹配 + IDF + trigram，**无向量**；满足不了"需求变更影响哪些代码/测试"这类精确追溯 |
| **单文件视角** | 一个目录一份 `graph.json`，跨 repo 靠事后 merge；无权限/多租户/并发写 |

**定位**：**借鉴机制，不直接用**。但它的 `markdown_resolution.py`（231 行）、tree-sitter 多语言抽取、MCP 服务可**单独抽用做原型**；`graphify extract --code-only` 可零成本先对我们某个仓库跑一遍，看 AST 图谱质量再定自研投入。

> 注：调研报告称其 12 万 star，**该数字存疑，未采信**；本节只采用有源码证据的设计机制。

### 4.6 横向对照与结论（三家 + 同类，2026-09-26）

调研对象：CodeGraph、Graphify、codebase-graph-prd-rules，以及同类（artgraph / aSPARK-graph / specgraph / grund / context4ai / StrictDoc / Doorstop / OpenFastTrace）。

**结论：都不直接集成，自建薄层；但抄它们的机制。**

#### 4.6.1 对照表

| 工具 | 方向 | 关联机制 | 变化影响面 | 测试关联 | 语言/形态 | 结论 |
|---|---|---|---|---|---|---|
| **CodeGraph** | 代码→图谱 | tree-sitter AST + 22 节点/12 边 + route 一等节点 | 无（仅 `updatedAt`） | 无 | Node 单机 | 借鉴模型 |
| **Graphify** | 代码+文档→图谱 | AST + **Markdown 反引号→references 边** + LLM 语义边 | `affected` 反向遍历（需加 `--relation contains` 才到文档页） | 无（测试是普通 code 节点） | Python 包 | 借鉴机制 |
| **codebase-graph-prd-rules** | 代码→规则文档 | 无自己的图，外包给 Graphify + LLM | **无** | 无 | 纯提示词包 | 只抄 prompt |
| **artgraph** | Spec↔代码↔测试 双向 | **显式 ID 主键**：spec `- REQ-001:` / 代码 `// @impl REQ-001` / 测试 `it("[REQ-001]…")`，同一字符串 join | **最强**：节点 contentHash + `check --gate` + `impact --diff` | **有**：`verifies` 边 + 执行 trace | **仅 TS** | 抄机制（语言不合） |
| **aSPARK-graph** | 交付物→代码 | 三档置信 `declared > extracted > inferred` | `impact` widest-path（报最弱边） | 有：`QACheck --verifies--> AC` | Python，但绑 `.spark/` 目录 | 抄机制 |
| **Doorstop** | 需求↔需求 | 显式 `links:` | **suspect-link**：父变则子链标疑 | 弱 | Python CLI | 抄 suspect-link |

#### 4.6.2 要抄的四个具体机制（按优先级）

**① 稳定规则 ID + 三处显式标记**（artgraph 模式，最值得抄）

同一条规则的 ID 出现在三处，**同一字符串 join**，确定性、可 grep、可 CI 校验：

```
白皮书/规则条目：  - TA-001: 空库存下单返回 409
代码注释：         // @impl TA-001
测试用例：         def test_empty_stock_returns_409():  # [TA-001]
```

→ 落成我们的 `knowledge_item.key`（如 `TA-001`），场景/用例以 `[TA-001]` 引用。**这比图遍历先落地，且零成本。**

**② contentHash + drift gate**

每个知识条目/锚点存 `content_hash`；变了即报 drift，`--gate` 卡 CI。这是"规则变化→影响面"的**最小可用实现**。

**更好的模型：codetospec 的「引用行区间 SHA-256」**（见 §4.6.5）——不是对节点整体做 hash，而是**对被引用的源码行区间**（`path:start-end`）做 hash：引用范围内**任何一行**被改，digest 就变。比节点级 contentHash 更精确，且有现成的 GitHub Actions 模板可抄。

**③ 三档置信 + 最弱边**（aSPARK / Graphify）

| 档 | 来源 | 例 |
|---|---|---|
| `declared` | 显式声明 | 文档里写 `<!-- source: path#L -->` |
| `extracted` | 确定性解析 | tree-sitter AST / markdown 反引号引用 |
| `inferred` | LLM 猜测 | 语义相似边 |

impact 查询报路径上的**最弱一环**——"这条影响链有多可信"一目了然。与我们 §5.2 三重闸、Graphify 的 EXTRACTED/INFERRED 是同一思想。

**④ Markdown 反引号 → 文档→代码边**（Graphify，零成本）

只要白皮书/计划/`code-map.md` 用**反引号**写符号名（如 `` `resolve_reference_specs` ``），Graphify 自动建 `文档 → 代码` 边。我们已在部分这么做，**缺的是反方向（代码→文档）和测试边**，需自建。

#### 4.6.3 现状盘点与缺口（本地已验证）

**已有**：
- 四级文档体系（`docs/project-system/`：代码→计划→白皮书→总计划，含 `Whitepaper:` / `Plans:` 双向链接约定）
- `code-map.md`（模块↔源码入口↔白皮书↔计划 三向链接）
- `docs/modules/` 模块说明
- 325 个 pytest 用例
- `.codegraph/` SQLite 符号索引（本地 IDE 工具，**不作为平台依赖**，§4.9 定）

**缺口**：
1. **白皮书规则无稳定 ID**（如 `token-accounting.md` 通篇原则文字、零代码符号引用 → 建不出任何边）
2. **测试用例与需求零关联**（325 个测试无一被文档引用）
3. **无 hash / drift / 影响面机制**

#### 4.6.4 落地路线（按投入）

| 档 | 内容 | 成本 |
|---|---|---|
| **零成本（最高优先）** | 写 `check-spec-links.sh` 式校验脚本：扫白皮书/计划的 `targets:` / `[@test]` / 反引号引用，校验文件与测试存在，坏链进 CI | 今天可做 |
| **零成本** | 白皮书/计划统一用**反引号**写符号名 + `<!-- source: path#L -->` 注释 → 自动产出文档→代码边；`code-map` 补测试文件入口 | 今天可做 |
| **小成本** | 白皮书规则编稳定 ID（`TA-xxx`），测试用例以 `[TA-xxx]` 命名/tag，**grep 即追溯** | 低 |
| **中成本** | 自建 `trace` 三元组（规则 ID→代码符号→测试用例，符号解析复用 **Graphify**）+ **引用行区间 SHA-256** drift check 进 CI（抄 codetospec） | 中 |
| **大成本** | 置信分级 impact 查询服务（widest-path）+ MCP 化供 Agent 调用 | 高 |

**抄谁（修订后）**：

| 环节 | 抄谁 |
|---|---|
| 漂移门禁 | **codetospec**（引用行区间 SHA-256，比节点级 contentHash 更精确，有 Actions 模板） |
| 链接语法 | **Tessl**（`targets:` + `[@test]`，同时定义 code 与 test 两个方向，有 bash 校验器） |
| 规则抽取 | **codebase-graph-prd-rules**（行文规范：三层规则拆分 + 证据等级）+ **codetospec**（引用必须可解析，否则 fail） |
| 图底座 | **Graphify**（唯一底座：符号边 + 文档引用边，§4.9 实测定） |

**风险提示**：`codebase-graph-prd-rules` 是"代码→反推 PRD"方向，与我们"已有 PRD 要关联代码"**相反**，且是为 Java 业务系统写的模板——**学机制，不套模板**。

### 4.7 核心架构决策：**工具即抽取器，我们做入库与统一**（2026-09-27）

> 用户明确：**不重造轮子**——跑现成工具干它们擅长的事，**数据入库这一层我们做**，把它们的输出整合进我们的库。

**架构**：

```
外部工具（无状态生产者，各干各的活）
  CodeGraph   → 代码符号 + 调用边 + 路由
  Graphify    → 文档↔代码引用边 + 概念
  codetospec  → 业务规则 + path:lines + drift digest
  Tessl 脚本  → 链接校验（约定，直接采用）
        │ ① 跑工具（subprocess）
        ▼
适配层（每工具一个 adapter）
  ② 读输出（JSON / SQLite / markdown）
  ③ 映射到我们的 schema
  ④ 幂等 upsert（按我们的稳定身份）
        ▼
我们的库（唯一查询真相）
  knowledge_item / anchor / edge
  → 用例生成、覆盖计算、影响分析、报告 都查这里
```

**五条设计要点**：

| # | 要点 | 说明 |
|---|---|---|
| 1 | **工具是生产者，我们的库是查询真相** | 工具跑完退场；下游只查我们的库，不直接调工具 |
| 2 | **每工具一个 adapter，职责单一** | `run() → parse() → map() → upsert()`；换/加工具只加 adapter，不动下游 |
| 3 | **用我们的身份，不用它的 ID** | CodeGraph 的 ID 含行号（一改就断）→ 入库时按我们的 `(project_id, kind, path, symbol)` 重建身份，它的 ID 只作"来源指纹"存 |
| 4 | **工具可缺失，不致命** | 某工具没装 → 那类知识标缺失/stale，其余照常；不因一个工具挂掉整链断 |
| 5 | **各工具管自己的增量，我们做全量对账** | 工具有自己的增量（Graphify SHA256 缓存 / CodeGraph watcher）；我们按 `content_hash` 对账，变了标 stale 并传播 |

**工具 → 知识 → 入库映射**：

| 知识 | 谁产出 | 怎么入 |
|---|---|---|
| 代码符号 / 调用边 / 路由 | **CodeGraph** | `codegraph query --json` → 映射为 anchor + edge |
| 文档↔代码引用边 | **Graphify** | `graphify extract` → 读 `graph.json` → 映射为 `references` 边 |
| 业务规则 + 引用 + drift | **codetospec** | 跑 codetospec → 读规则图 → 映射为 `rule` 条目 + digest |
| 链接校验 | **Tessl 脚本** | 直接采用其 `targets:` + `[@test]` 语法与校验脚本 |

**我们唯一要自己写的**：adapter + 入库 + 统一查询 + 门禁。**不再自建解析器。**

> **对 §4.2 / §11 的修正**：原计划"自建 tree-sitter 轻量索引"**改为**"优先用现成工具（CodeGraph/Graphify/codetospec）产出，经 adapter 入库"；只有在工具覆盖不到的地方（如 `table` / `config_key` 锚点，CodeGraph 不抽）才考虑自建专用解析器。

### 4.6.5 补充调研：两个更贴合的方案（2026-09-26 增补）

#### codetospec —— 机制最完整

`github.com/edouard-claude/codetospec`（Go，MIT）。**"确定性流水线搬运 + LLM 只做认知 + 机械校验"**：

| 机制 | 内容 |
|---|---|
| **强制引用** | LLM 抽规则**必须带 `path:lines` 引用**；机械校验（schema + 引用可解析 + references 存在），2 次重试后该 unit 标 `failed`——**LLM 无法编造无法兑现的引用** |
| **对抗交叉核对** | 全新上下文的 LLM 对每条规则投票 `supported / partial / unsupported` |
| **图模型** | 节点 `{domain, entity, endpoint, rule}`，ID 形如 `rule.billing.prorata-activation`；边 `{belongs_to, touches, exposed_by, depends_on}`；**依赖边由跨域 import 事实计算，注释明写 "never by the LLM"** |
| **漂移闭环**（最值得抄） | 每条 rule 存 `digest` = **对被引用源码行区间 `start-1:end` 的 SHA-256**；`Check()` 输出四态 `ok / stale / broken / unknown`；附带**可直接复制的 GitHub Actions**（PR 改 `src/**` 时跑 drift，非 0 退出即失败）——**完全确定性，零 LLM** |
| 产物 | 提交进仓库的 Markdown 图谱（YAML frontmatter）+ `graph.json` + `llms.txt` |

**结论**：同时做到「LLM 抽规则→强制引用→持久图→hash 漂移→CI 门禁」四件事，是调研全集里**最接近我们目标**的实现。**读源码抄机制，不作依赖**（Go 写、面向 legacy、活跃度低）。

#### Tessl tile —— 唯一显式的 spec↔code + spec↔test 双向语法

`github.com/tesslio/spec-driven-development-tile`（MIT，核心平台闭源，此 repo 是方法论 tile）：

| 机制 | 内容 |
|---|---|
| **`targets:`**（frontmatter，必填 ≥1） | spec 声明它描述的代码文件/glob（`../src/models/**/*.py`）→ **spec→code** |
| **`[@test] <路径>`**（正文行内，与需求交错） | 直接指到测试文件（`` `[@test] ../tests/test_x.py` ``）→ **spec→test** |
| **`check-spec-links.sh`** | 解析 `targets:`（glob 展开，无匹配报错）+ `[@test]`（文件不存在报错），坏链 `exit 1`。**零依赖 bash，可直接进 CI** |

**结论**：粒度只到文件级、单向、无反向索引，但**把"PRD ↔ 代码 ↔ 测试"三方链接的语法和校验脚本都定义清楚了**。抄它的 `targets:` + `[@test]` 语法与校验思路（粒度提到符号级则参考 artgraph）。

#### 修订后的落地第 1 步（**零成本，优先级最高**）

**先写一个 `check-spec-links.sh` 式的纯脚本**：扫描白皮书/计划里的 `targets:` / `[@test]` / 反引号符号引用，校验文件与测试存在，坏链退出非 0 进 CI。

**这一步不依赖任何图**，却直接补上我们最大的两个缺口（白皮书零代码引用、325 测试零关联）。

#### 生态横切结论（供参考）

- **代码知识图谱类**（Graphify / joern / scip / codeql / code-graph-rag…）：节点/边词汇收敛于 `文件/符号 + contains/calls/imports/references ± routes/DB/events`，**没有一个有「spec 章节 ↔ 代码符号」链接或业务规则抽取**。
- **Spec 驱动开发类**（spec-kit / OpenSpec / BMAD / SPARC…）：**全部是一次性生成**（spec→plan→code），维护的是 spec↔spec 链接，**没有任何工具存持久双向的符号级 spec↔code 链**；AWS Kiro（闭源）是唯一有自愈闭环的。
- **结论**：我们的需求（持久、双向、符号级、带漂移门禁的 需求↔代码↔测试 图谱）**在开源生态里没有现成实现**——**自建是必要的**，但机制可抄。

> 数据可信度提醒：本批调研报告中的 star 数（12 万、13 万量级）**明显不可信，未采信**；本节只采用有源码路径证据的机制描述。

### 4.9 Spike 实测结论（2026-09-28 已执行，取代前述推断）

> 用隔离 venv 装了 `graphifyy 0.9.71`（CLI + 纯函数两路），在**我们自己的仓库**上实测。以下数据全部实测所得，非调研推断。

#### 实测 1：真实代码切片（`user_platform`，156 个 Python 文件）

| 指标 | 结果 |
|---|---|
| 耗时 | **15.6 秒**（20 workers）→ 全仓 1,831 文件推算约 3 分钟 |
| 节点 | 4,835（`code` 3,171 / **`rationale` 1,596** / `concept` 68） |
| 边 | 12,224（`calls` 3,547 · `contains` 2,155 · `references` 1,629 · `rationale_for` 1,617 · `uses` 923 · `imports` 833 · `imports_from` 702 · `method` 493 · `inherits` 216） |
| **置信度** | **EXTRACTED 11,231（92%）** / INFERRED 993（8%） |
| 附加 | 自动跳过敏感文件（识别出 `apple_gsa_ca.pem`） |

**结论**：92% 的边是确定性抽取——正是"不靠猜"。1,596 个 rationale 节点是从 docstring/注释白捡的**业务意图**。

#### 实测 2：文档↔代码引用边（人造 fixture 验证）

CLI 完整模式**硬要 LLM key**（哪怕只解析反引号）。绕过方式已验证：**直接调纯函数**（Apache-2.0 合规）：

```python
from graphify.extractors.markdown import extract_markdown
from graphify.markdown_resolution import resolve_markdown_mentions
```

四种引用行为全部符合预期：

| 文档里的引用 | 结果 | 置信度 |
|---|---|---|
| `OrderService.create_order` | 建 references 边 | **INFERRED 0.95** |
| `app.py::Widget::render`（路径限定） | 建 references 边 | **EXTRACTED 1.0** |
| `Widget`（裸名唯一匹配） | 建 references 边 | **INFERRED 0.95** |
| `OrderService.refund_order`（**不存在**） | **丢弃，不建边** | — |

> "an ambiguous name is a guess, and a guess is not an edge" —— 实测成立，与我们的门禁哲学一致。

#### 实测 3：Graphify **不**支持 FastAPI 路由（关键缺口）

跑我们真实代码的结果：**route 节点 0 个、`decorators` 字段全空**——URL 路径只在 docstring 文本里飘（rationale 节点里能看到 `/api/v1/teams` 这类字样，但不是结构化节点）。

对比：CodeGraph 在同一仓库提取了 **1,129 个 route 节点**（17 框架识别）。

**但 endpoint 这个缺口有更好的解法**：FastAPI 这类框架的路由**不该靠静态解析**——`app.openapi()` / `/openapi.json` 直接给出全部路由 + 方法 + 请求/响应 schema，比任何静态工具都准。Graphify 自带 `--postgres DSN`（活库 introspect）即此路线的先例：**从运行时拿事实，不从源码猜**。

#### 最终选型（取代 §4.4–§4.8 的推断）

| 决定 | 内容 | 理由 |
|---|---|---|
| **代码图谱底座 = Graphify（唯一）** | Python 库**同进程 import 直调**；code-only + markdown 纯函数**零 LLM key、零 token** | 实测 15.6s/156 文件、92% EXTRACTED、独有文档边与 rationale |
| **不引入 CodeGraph** | 弃用 | Node 形态嵌不进 Python 服务端 + 6.6GB/机 + ID 用行号算（一改就断）+ 不抽表/配置 |
| **endpoint 锚点走 OpenAPI/规格导入** | 运行时导出或读 `openapi.json`；DB 表走 `--postgres` 或迁移文件 | 静态解析路由既不准也没必要；框架自己知道路由 |
| **§4.8 的 sidecar 简化** | 只剩 Python 一个运行时 → **同进程调用，sidecar 可省**；将来要接异构工具再引入 | 少一层编排 |
| **工具缺失不致命**（原样保留） | code-only 无 key 可跑；文档边纯函数无 key 可跑 | 两条主路都不依赖外部服务 |

**adapter 层因此极薄**：`extract_markdown` + `resolve_markdown_mentions` + `graphify extract --code-only` 的产物读取，加一层"映射到我们的 anchor/edge 表"。

### 4.8 运行位置与部署形态（2026-09-27，结论已被 §4.9 更新）

> 用户明确：**工具在服务端跑**——放节点会遇到"这台没装 Node / 那台没装 Go 就不能用"的问题。

**核心架构：全部在服务端运行。**

| | 在哪跑 | 输入 | 为什么 |
|---|---|---|---|
| **知识索引**（静态分析） | **服务端** | **某个 commit 的仓库快照** | 不需设备；集中一份供全局查询；不依赖节点环境 |
| **测试执行**（引擎） | **服务端** | 场景定义 + 环境变量 | 确定性执行；结果同进程直接写库；**设备/浏览器操作经现有通道下发**，不是在节点执行任务 |

```
知识索引   ← 服务端自己 clone 仓库快照，跑 Graphify
测试执行   ← 服务端引擎逐步骤执行；需要动真机/浏览器时，调现有控制通道
```

**服务端跑的四条理由**：

1. **不依赖节点环境**——不用在 N 台节点装 Node + Go + Python
2. **单一真相**——索引集中一份，生成用例/算覆盖/影响分析全查这一份
3. **查询快**——不用跨网络回节点查图
4. **版本一致**——工具版本只有一个，不会 A 节点 1.1、B 节点 1.6

**工具链部署：同进程，无 sidecar**

服务端要同时有 Node（CodeGraph）、Go（codetospec）、Python（Graphify）：

> ~~sidecar 容器~~ —— **§4.9 实测后已取消**：Graphify 是 Python 库，同进程 import，无跨语言运行时问题。

**摄取与入库（同进程版）**：

```
① 服务端 clone 仓库快照到索引区（按 commit）
② 直接调 Graphify（纯函数/CLI）产出
③ adapter（parse → map → upsert）
④ 入库（按我们的稳定身份幂等）
⑤ 清理临时快照（或按 LRU 保留）
```

**工具不可用时**：抽取失败 → 那类知识标 `stale`/缺失，其余照常（§4.7 要点 4）。

### 4.3 锚点的作用

- **对齐**：产品规则、技术接口、测试用例都指向同一锚点 → 天然对上
- **影响分析**：代码变了（content_hash 变）→ 找到锚点 → 找到关联的知识/用例
- **覆盖**：锚点 × 用例 = 覆盖矩阵

## 5. 摄取流水线（怎么进来）

```
① 源注册 → ② 拉取 → ③ 解析 → ④ 抽取 → ⑤ 锚定 → ⑥ 过门 → ⑦ 入库
```

| 步 | 确定性 / agent | 说明 |
|---|---|---|
| ① 源注册 | 人 | 登记源（PRD 仓库/URL、代码仓库、API 规格、bug 库、用例库） |
| ② 拉取 | 确定性 | 抓取原文，存 **L1 原始层**（快照 + 溯源） |
| ③ 解析 | 确定性 | 按类型解析：代码 → tree-sitter；OpenAPI → 规格解析；文档 → 分块 |
| ④ 抽取 | **混合** | 见 5.1 |
| ⑤ 锚定 | 确定性 + agent | 把条目挂到锚点（精确匹配优先，agent 兜底） |
| ⑥ 过门 | 确定性 | 知识门（结构 + 溯源 + 可测） |
| ⑦ 入库 | 确定性 | 写 L2，重建相关 L3 边 |

### 5.1 抽取：确定性 vs agent

| 源 | 抽取方式 | 理由 |
|---|---|---|
| 代码（接口/组件/表/配置/依赖） | **确定性**（tree-sitter / 解析器） | 代码是结构化的，不需 AI，且要准 |
| API 规格（OpenAPI/Swagger） | **确定性** | 规格本身就是结构 |
| DB schema / 迁移 | **确定性** | 同上 |
| PRD / 产品文档 | **agent** | 自然语言，需理解 |
| 业务规则（散落在文档） | **agent** | 同上 |
| 历史 bug | **agent**（聚类 + 归纳） | 从描述里提炼模式 |
| 历史用例 | **混合**（结构化的确定性，散文的 agent） | |

**原则：能用确定性就别用 agent。** agent 抽取的标 `confidence` 较低，且可能需人工确认才转 `confirmed`。

### 5.2 三重闸门（过门）

| 闸 | 检查 | 不过 |
|---|---|---|
| **锚点闸** | 每条知识能否锚到代码实体 | 降级 `candidate`，不进生成池 |
| **去重闸** | 与已有条目是否重复（按锚点 + key） | 合并 / 更新 |
| **可测闸** | 规则类是否有机器可判定的预期 | 标"参考"，不进生成池 |

## 6. 知识图谱（L3 边）

**`mc_knowledge_edge`**：`from_id, to_id, relation`

| relation | 含义 | 例子 |
|---|---|---|
| `defines` | 需求定义规则 | requirement → rule |
| `implements` | 代码实现规则 | component → rule |
| `exposes` | 代码暴露接口 | component → api_endpoint |
| `uses` | 依赖 | component → dependency / db_table |
| `covered_by` | 被用例覆盖 | rule → case |
| `derived_from` | 派生自 | bug_pattern → bug |
| `conflicts_with` | 冲突（需人裁决） | rule ↔ rule（矛盾） |
| `depends_on` | 依赖 | api_endpoint → db_table |

**节点** = knowledge_item + anchor（锚点也是图节点）。
**边** 让"改一处，影响谁"可计算。

## 7. 查询接口（怎么用）

知识不是"存着好看"，是**被查**。核心查询：

| 查询 | 用途 | 谁用 |
|---|---|---|
| `by_anchor(anchor)` | 这个接口/函数相关的全部知识 | 生成用例、开发查接口 |
| `by_type(kind, filter)` | 某模块的所有规则 | 测试设计 |
| `by_relation(item, relation)` | 谁实现了这条规则 / 这条规则被谁覆盖 | 追溯、影响 |
| `coverage(anchor/module)` | 覆盖缺口（规则无用例） | 缺口分析 |
| `impact(changed_paths)` | 代码变更影响哪些知识/用例 | 增量测试、回归 |
| `search(text, kind?)` | 语义/关键词检索 | 人查 |

**接口**：`knowledge.query`（能力面），同时供**开发**与**测试**使用——这正是"知识是公共资产"的落地。

## 8. 新鲜度与影响分析（怎么保鲜）

```
源变更 → 标 stale → 重新抽取 → 更新 L2 → 重建 L3 局部 → L4 视图自动刷新
```

| 机制 | 说明 |
|---|---|
| **溯源** | 每条知识记 `source_ref`（哪个源、哪个版本/commit） |
| **锚点哈希** | `content_hash` 变 → 该锚点下的知识标 `stale` |
| **依赖传播** | 一条知识 stale → 沿 `edge` 标记下游 stale |
| **L4 自动重算** | 视图层是查询结果，不需手工同步 |
| **影响面** | `impact(changed_paths)` → 受影响锚点 → 受影响用例（增量测试的基础） |

**stale 不等于删除**——保留但标失效，下游（用例）收到"需复核"信号。

## 9. 完整例子（从需求到覆盖）

**场景**：PRD 新增一条规则"空库存下单应返回 409"。

```
① 源注册     PRD 仓库登记为产品知识源
② 拉取       抓取 PRD 章节，存 L1 原始层
③ 解析       文档分块
④ 抽取       agent 读 → 产出 rule 条目：
             { kind: rule, statement: "空库存下单返回 409",
               condition: "inventory == 0", expected: "HTTP 409" }
⑤ 锚定       规则指向锚点 (endpoint, POST /api/v1/orders)
              ← 通过"下单"语义 + 接口列表匹配
⑥ 过门       结构✅ 溯源✅ 可测✅（有明确 expected）→ confirmed
⑦ 入库       L2 存 rule；L3 建边：
             requirement --defines--> rule
             rule --covered_by--> (空，还没用例)

── 之后，测试侧消费 ──

查询 coverage(POST /api/v1/orders)
  → profile 显示：rule "空库存返回409" 无用例覆盖（gap）

缺口驱动生成 → 建用例 TC-xxxx（引用该 rule 作为 confirm_ref）
             → 关联场景 SCN-xxxx（步骤：请求空库存下单 → 后置校验 status_code=409）

── 代码变更时 ──

开发者改了 orders.py 的下单逻辑
  → anchor content_hash 变 → 标 stale
  → 沿边找到 rule + 用例 TC-xxxx
  → 增量测试：选中 TC-xxxx 重跑
```

**这一个例子串起了**：源 → 原始 → 结构化 → 图谱 → 视图 → 缺口 → 生成 → 覆盖 → 影响分析。

## 10. 与其它模块的接口

| 消费方 | 用知识做什么 |
|---|---|
| **测试设计/生成** | `by_anchor` + `coverage` → 缺口驱动生成 |
| **开发** | `by_anchor` / `search` → 查接口、查规则、查依赖 |
| **影响分析** | `impact` → 增量选例 |
| **报告** | `by_relation(covered_by)` → 需求覆盖矩阵 |
| **校验器** | 查"接口/字段是否存在"→ 幻觉校验（A1/A2） |

## 11. 建设顺序（怎么落地）

> **按 §4.7 修正**：不再自建解析器，改为"跑现成工具 → adapter 入库"。

| 步 | 内容 | 依赖 |
|---|---|---|
| 1 | L1 原始层 + 源注册 | 无 |
| 2 | **adapter 框架**（`run/parse/map/upsert`）+ 统一入库（按我们的稳定身份幂等） | 无 |
| 3 | **CodeGraph adapter**：跑工具 → 符号/调用边/路由 入库 | 2 |
| 4 | L2 条目表 + L3 图谱边 + `by_anchor`/`by_type`/`coverage` 查询 | 2,3 |
| 5 | **Graphify adapter**：文档↔代码引用边入库 | 4 |
| 6 | **Tessl 脚本**：`targets:`/`[@test]` 链接校验（直接用，进 CI） | 无（独立） |
| 7 | **codetospec adapter**：业务规则 + 引用行 digest 入库 → 漂移门禁 | 4 |
| 8 | 影响分析 `impact` + 测试视图（profile / 覆盖矩阵） | 4,7 |
| 9 | bug/用例历史摄取（agent） | 4 |

> **先做 2（adapter 框架）+ 6（Tessl 校验脚本）**：前者是一切工具的接入底座，后者零依赖、立刻补上"白皮书零代码引用 / 测试零关联"两个缺口。

## 12. 设计决策（原"待确认"，已收敛）

> **反思**：此前这里列了 9 条"待确认"，但其中多数**不是客观问题，而是设计没想清楚**——把实现细节当成了需要拍板的设计问题。逐条给决策，不留问号。

| # | 原问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | tree-sitter 语言范围 | **不适用** | 已改为"用现成工具"，不自建解析器——问题本身消失 |
| 2 | 工具在哪跑 | **Graphify 同进程 import**（Python 库）；输入是服务端 clone 的 commit 快照 | sidecar 可省（§4.9）；异构工具出现时再引入 |
| 3 | L1 原始层存储 | **小文本进 DB，大文档/PDF 进对象存储**，统一用 ref 引用 | 大小差异大，没必要二选一 |
| 4 | agent 抽取确认策略 | **确定性抽取 → `confirmed`；agent 抽取 → `candidate`**（人工或交叉验证后升级） | 与既有门禁哲学一致（AI 不裁判） |
| 5 | 图谱查询实现 | **v1 纯 SQL 递归（PG 递归 CTE）** | 够用；规模/复杂度上来了再评估图库 |
| 6 | 多项目隔离 | **按 `project_id` 隔离**（锚点身份 §4.1 已含）；跨项目共享需**显式声明** | 默认隔离，共享是例外 |
| 7 | ~~sidecar 编排~~ | **已省**：Graphify 是 Python 库同进程调用 | §4.9 实测后简化 |
| 8 | 工具产物版本 | **adapter 声明支持版本 + 入库版本探测 + 不匹配走兼容层或标 stale** | 实现细节 |
| 9 | 索引触发 | **手动 + 定时 + push 三种；v1 先手动 + 定时** | push 依赖 webhook 链路，后置 |
| 10 | **endpoint 锚点来源**（§4.9 新增） | **OpenAPI/规格导入**（`app.openapi()` / `openapi.json`），DB 表走 postgres introspect 或迁移文件 | 框架自己知道路由，不靠静态猜 |

**唯一需要产品方决定的（1 条）**：

- **知识源的建设优先级**。建议顺序：**代码（Graphify）→ OpenAPI/DB 规格 → 文档（PRD）→ 规则（codetospec 式）→ bug/用例历史**。理由：代码与规格最确定、不依赖 agent、立刻可用（接口清单/组件树/依赖图/路由表）。

> **原则**：设计层只留"要什么"的取舍；"怎么做"由设计自己给答案。
