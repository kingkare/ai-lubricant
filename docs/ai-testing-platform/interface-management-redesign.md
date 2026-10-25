# 接口管理改造报告 —— 对照 Apifox 的诊断与方案

**日期**：2026-10-13
**触发**：用户反馈「接口管理页很乱，参考 Apifox 看应该怎么管理」
**范围**：`mc_test_transport` 的「接口」用法 + `transports-page.tsx` 前端 + `openapi_importer` / `repo_import` 导入器
**不在范围**：连接配置 / 鉴权 / 公共头 / 路径替换 —— 归「环境」管，`mc_test_transport` 这块不动。

---

## 一、结论摘要（TL;DR）

「接口管理」目前**没有"接口"这个实体**。

接口最核心的东西——**方法、路径、参数、请求体、响应结构、用例**——一个字段都没有。`GET /api/v1/orders` 被压成一串 `endpoint_get_orders` 塞进 `code` 字段，列表第一列就是这种机器串，人认不出这是哪个接口。

导入时更严重：`openapi_importer.py` **为每个 endpoint 生成一条 transport 记录**（`:359`），却把 OpenAPI 里的 `parameters` / `requestBody` / `responses` / `tags` **全部丢弃**，只留一句 `summary` 当备注。于是"导入 200 个接口"的结果是——**200 行平铺的字符串，没有方法、没有路径、没有文档**。

**改造方向**：照 Apifox，把接口建成**一等实体**——目录树 + 方法/路径/参数/请求体/响应 + 标签，可被场景与用例引用。

---

## 二、现状：我们的「接口管理」到底是什么

### 2.1 数据模型（`testing_platform/assets/models.py:756` `TestTransport`）

| 字段 | 类型 | 实际含义 |
|---|---|---|
| `code` | str | **唯一承载方法+路径的地方**（`endpoint_get_orders`） |
| `kind` | http/ws | 协议 |
| `description` | str | 备注（导入时 = operation summary） |
| `module` | str | 分组维度（**纯文本**，思维导图靠它分组） |
| `auth` / `common_headers` / `common_params` / `path_rewrite` | JSONB | ← **归环境管，本次不动** |
| `status` / `version` / `requirement_id` | | 生命周期 / 变更门 / 主脊关联 |

**没有 `method`、没有 `path`、没有 `parameters`、没有 `request_body`、没有 `responses`、没有 `tags`、没有 `folder_id`。**

### 2.2 前端（`user-frontend/src/pages/console/user/testing/transports-page.tsx`）

- 列表列：`Code | 模块 | 说明 | Kind | 状态 | 操作`（`:236-289`）——主列是 `endpoint_get_orders`。
- 头部三个平级按钮：`AI 读仓库生成` / `接口导入` / `新建接口`（`:184-220`），外加「列表 / 思维导图」视图切换（`:167-182`）。
- **无搜索、无筛选、无分页**（全量 `getTransports(projectId)` 一次拉完，`:129`）。
- 思维导图视图靠**解析 code 兜底分组**（`transport-mindmap.tsx:29-37`）。

### 2.3 导入器（`testing_platform/importers/openapi_importer.py`）

每个 `method × path` → 一条记录（`:359-408`），`code = endpoint_<method>_<path>`。**丢弃**：`parameters`（除 query/header 的 default 外）、`requestBody`、`responses`、`tags`；`operationId` 仅作 summary。仓库扫描（`repo_import.py:49`）同理：`method + path` 只进 `code` 和 `description`。

### 2.4 与场景/用例的关系

**接口与场景是脱钩的**。场景步骤（`scenario-step-form.tsx`）里 request 步骤只有裸 `method` + `url` 两个文本框，**不引用任何接口实体**。接口管理页建了一堆接口，场景执行时却用不上——这也是它"独立存在的价值"没体现出来的原因。

---

## 三、Apifox 是怎么管理的（概念模型）

| Apifox 概念 | 作用 | 我们的对应物 | 差距 |
|---|---|---|---|
| **目录（树）** | 左侧可嵌套文件夹，拖拽整理，接口/用例都挂树上 | `module` 一个字符串 | ❌ 无层级、无拖拽 |
| **接口** | method + path + 参数 + 请求体 + 响应 schema + 示例 + 标签 + 负责人 | 一行 `mc_test_transport` | ❌ 除 code/备注外全无 |
| **数据模型（Schema）** | 可复用的请求/响应结构，接口引用 | 无 | ❌ 完全没有 |
| **接口用例** | 每个接口挂多组测试用例 | `mc_test_case` 独立存在 | ⚠️ 无接口↔用例直连 |
| **环境** | base_url + 变量，一键切换 | `mc_test_environment` + 变量 | ✅ 已有（本次不动） |
| **公共参数 / 认证** | 项目级配置，接口继承 | 归「环境」管 | ✅ 已有（本次不动） |
| **Mock / 前后置脚本** | 文档即测试 | 场景步骤 | ⚠️ 概念不同但可对齐 |

**Apifox 的心智模型**：`项目 → 目录树 → 接口（方法/路径/参数/响应）`，环境与认证是**横向切面**。

**我们的心智模型**：`项目 → 一堆平铺的字符串行`，方法/路径藏在字符串里，文档为零。

---

## 四、差距诊断（为什么"乱"）

| # | 问题 | 证据 | 严重度 |
|---|---|---|---|
| 1 | **没有接口实体**：方法/路径/参数/请求体/响应无字段 | `models.py:766-780` | 🔴 高 |
| 2 | **无方法/路径列**：主列是 `endpoint_get_orders` 机器串 | `transports-page.tsx:239` | 🔴 高 |
| 3 | **导入丢数据**：OpenAPI 的 params/body/responses/tags 全丢 | `openapi_importer.py:359-408` | 🔴 高 |
| 4 | **无目录树**：`module` 自由文本，思维导图解析 code 兜底 | `transport-mindmap.tsx:29-37` | 🟠 中 |
| 5 | **无搜索/筛选/分页**：设计来放几百条却不可检索 | `transports-page.tsx:129` | 🟠 中 |
| 6 | **与场景脱钩**：步骤不引用接口，接口建了用不上 | `scenario-step-form.tsx` | 🟠 中 |
| 7 | **入口过多**：三个平级按钮 + 视图切换，无主次 | `transports-page.tsx:184-220` | 🟡 低 |
| 8 | **导入无 dry-run 预览**：落库才知结果 | `transports-page.tsx:691-719` | 🟡 低 |

---

## 五、改造方案

### 5.1 新增 `mc_test_api`（真正的"接口" —— 一份完整接口文档）

Apifox 的接口是一份完整文档，字段分七组：

```sql
CREATE TABLE mc_test_api (
    -- ① 标识与归属（身份 = id；code 不是唯一键，见末尾注）
    id             UUID PRIMARY KEY,            -- 接口唯一标识（所有引用指向此 id）
    project_id     UUID         NOT NULL,
    code           VARCHAR(64),                  -- API-0001（可读句柄，非唯一标识）
    folder_id      UUID REFERENCES mc_test_api_folder(id) ON DELETE SET NULL,  -- → 目录

    -- ② 基本信息
    name           VARCHAR(256) NOT NULL,         -- 接口名称：下单
    method         VARCHAR(8)   NOT NULL,        -- GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS
    path           VARCHAR(512) NOT NULL,        -- /api/v1/orders/{id}
    protocol       VARCHAR(8)   NOT NULL DEFAULT 'http',  -- http|ws|grpc
    status         VARCHAR(32)  NOT NULL DEFAULT 'designing',
    description    TEXT,                           -- 说明/备注（同一字段，不分两栏）
    tags           JSONB NOT NULL DEFAULT '[]',    -- 标签
    owner_id       UUID,                           -- 责任人/维护人
    priority       VARCHAR(8),                     -- P0-P3（可选）
    "order"        INTEGER NOT NULL DEFAULT 0,     -- 目录内排序

    -- ③ 请求定义（**均为「结构说明」，非具体值** —— 见 5.1.1 注）
    params         JSONB NOT NULL DEFAULT '[]',    -- 参数说明: [{name,in(query|path|cookie),type,required,description,example,default}]
    headers        JSONB NOT NULL DEFAULT '[]',    -- 请求头说明: [{name,type,required,description,example,default}]
    body_type      VARCHAR(32) NOT NULL DEFAULT 'none', -- none|json|form-data|x-www-form-urlencoded|raw|xml|binary|graphql
    request_body   JSONB NOT NULL DEFAULT '{}',    -- 请求体结构说明（schema 树 + 示例）

    -- ④ 响应定义（可多个；同为「结构说明」）
    responses      JSONB NOT NULL DEFAULT '[]',    -- [{status_code,name,content_type,schema,example}]

    -- ⑤ 关联
    requirement_id UUID,                           -- → 主脊（需求）
    version_id     UUID,                            -- → 发布版本（与生产环境一致；暂不自动关联，后续跟踪）

    -- ⑥ 溯源与版本
    source         VARCHAR(32) NOT NULL DEFAULT 'manual', -- manual|openapi|repo_scan|ai
    source_ref     JSONB NOT NULL DEFAULT '{}',    -- {spec?, operationId?, file?, line?}
    version        INTEGER NOT NULL DEFAULT 1,

    -- ⑦ 审计
    created_by     UUID,                           -- 创建人
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by     UUID,                           -- 最后更新人
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()  -- 最后更新时间
    -- 注意：**身份 = id（UUID）**，code 不做唯一键（接口分版本，同一逻辑接口可跨版本出现多条）。
);
CREATE INDEX ON mc_test_api(project_id, folder_id);
CREATE INDEX ON mc_test_api(project_id, method);
CREATE INDEX ON mc_test_api(project_id, status);
CREATE INDEX ON mc_test_api(version_id);
```

#### 5.1.1 字段逐组说明

| 组 | 字段 | 说明 | Apifox 对应 |
|---|---|---|---|
| ① 归属 | `id` | **接口唯一标识**（UUID）——所有引用（场景/用例）都指向它 | 接口 ID |
| ① 归属 | `code` | 可读句柄 `API-0001`，**不做唯一键**（接口分版本，同一接口可跨版本多条） | 接口编号 |
| ① 归属 | `folder_id` | 目录 id，决定它在树上的位置 | 目录 |
| ② 基本 | `name` | 人读名称 | 接口名称 |
| ② 基本 | `method` / `path` / `protocol` | 方法 / 路径 / 协议 | 方法 / 路径 |
| ② 基本 | `status` | 接口状态机（见下） | 接口状态 |
| ② 基本 | `description` | **说明/备注**（同一字段，不分两栏） | 说明/描述 |
| ② 基本 | `tags` | 标签 | 标签 |
| ② 基本 | `owner_id` | 责任人/维护人 | 负责人 |
| ② 基本 | `priority` | P0-P3（可选，与用例/需求对齐） | — |
| ② 基本 | `order` | 目录内拖拽排序 | 排序 |
| ③ 请求 | `params` | Query/Path/Cookie 参数的**结构说明** | Params / Path |
| ③ 请求 | `headers` | **请求头的结构说明**（接口自身声明需要哪些 header，非具体值） | Headers |
| ③ 请求 | `body_type` | 请求体类型（json/form-data/…） | Body 类型 |
| ③ 请求 | `request_body` | 请求体的**结构说明**（schema 树 + 示例） | Body |
| ④ 响应 | `responses` | **多个响应**：状态码/名称/内容类型/schema/示例（同为结构说明） | 响应定义 |
| ⑤ 关联 | `requirement_id` | 挂到需求（主脊） | — |
| ⑤ 关联 | `version_id` | **发布版本**（与生产环境一致：接口发布了，参照就是发布版本）。暂不自动关联，后续跟踪 | 版本 |
| ⑥ 溯源 | `source` / `source_ref` | 从哪来（openapi/repo/ai）+ 原文定位 | — |
| ⑥ 溯源 | `version` | 版本号（变更门 +1，走 change_record） | 版本 |
| ⑦ 审计 | `created_by` / `created_at` | 创建人 + 创建时间 | — |
| ⑦ 审计 | `updated_by` / `updated_at` | **最后更新人 + 最后更新时间** | 更新时间 |

**接口状态机**（对齐 Apifox「接口状态」，与场景/用例的 draft/active 区分）：

```
designing（设计中）→ developing（开发中）→ testing（测试中/联调中）
                  → done（已完成）→ deprecated（已废弃）
```

> **⚠️ params / headers / request_body / responses 是「结构说明」，不是「具体值」**。
> 它们描述的是**这个接口长什么样**（有哪些字段、什么类型、是否必填、说明、示例），
> 而**不是**某次请求要发的具体数据（那属于用例/场景的输入）。
>
> 举例——`POST /orders` 的 `request_body` 存的是：
> ```json
> {
>   "type": "object",
>   "properties": {
>     "product_id": { "type": "string", "required": true,  "description": "商品 ID", "example": "P-1001" },
>     "quantity":   { "type": "integer", "required": true, "description": "数量",     "example": 2 }
>   }
> }
> ```
> 而不是 `{"product_id": "P-1001", "quantity": 2}` 这种具体数据。
>
> **前端含义**：编辑这些字段要的是**结构编辑器**（树形/表格：字段名 / 类型 / 必填 / 说明 / 示例），
> **不是**键值对填值框。这也正是它区别于「环境变量」的地方——环境变量是"值"，接口是"型"。

> 接口**不存** base_url / 路径前缀替换 / 认证 / 公共头 —— 这些走「环境」，执行时由环境变量注入。
> （接口自身的 `headers` 只是"声明需要哪些头"，真值由环境/用例提供。）

#### 5.1.2 与用例/场景的关联

接口与用例、场景都是**多对多**（一个接口可被多个用例覆盖、多个场景调用），走关联表，不塞数组列：

```
mc_test_api_case     (api_id, case_id, "order")   -- 接口 ↔ 用例
mc_test_api_scenario (api_id, scenario_id)         -- 接口 ↔ 场景（步骤引用）
```

> **关联是派生的，不是手工维护的**（用户定版 2026-10-14）。
> **真相源 = 场景步骤 `params.api_id`**；两张关联表降为**反查索引**，供接口详情
> 「被哪些场景/用例引用」与影响面反查。
>
> 派生链：`用例 → 场景 → 步骤(params.api_id) → 接口`。
> - 步骤落库（`create_steps`）→ `sync_scenario_api_links` 重建 场景↔接口 索引
>   （去重、只留真实存在的接口），再 `sync_cases_of_scenario` 回灌关联用例。
> - 用例绑/解绑场景（`link_case_scenario` / `unlink_case_scenario`）→
>   `sync_case_api_links` 重建 用例↔接口 索引（沿场景↔接口取并集）。
> - 删接口 → 连带清索引行（防悬空）。
> - 索引可无损重算，故**无手工 link/unlink UI**：要解除关联，回到场景步骤去掉
>   `api_id` 引用即可。`api.case.link` / `api.scenario.link` 等写方法仍保留
>   （供脚本/兼容），但正常前端流程不再调用。

### 5.2 新增 `mc_test_api_folder`（目录树）

```sql
CREATE TABLE mc_test_api_folder (
    id         UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    parent_id  UUID REFERENCES mc_test_api_folder(id) ON DELETE CASCADE,
    name       VARCHAR(128) NOT NULL,
    "order"    INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

> 删目录 → 级联删子目录，**接口不删**（`mc_test_api.folder_id` SET NULL 归根）——目录只是组织视图。

### 5.3 版本与发布（用户定版，后续跟踪）

**接口分版本，与生产环境一致**：接口发布到哪个版本，它的参照就是那个发布版本。

- `mc_test_api.version_id` 指向发布版本（可空）；
- **当前不自动关联**（哪个算"线上版本"未定）——列先落，等版本管理方案跟踪后再接；
- 同一逻辑接口跨版本 = 多条记录（各挂自己的 `version_id`），这就是**身份必须是 id 而非 code** 的原因。

### 5.4 迁移

当前**没有存量数据**，无迁移负担：直接建表，旧 `mc_test_transport` 保持不动（连接配置归环境，另行处理）。历史「endpoint_* transport」若需要回收，用解析脚本把 `code` 拆成 method/path 灌进 `mc_test_api`（`source='openapi'|'repo_scan'`）。

---

## 六、前端信息架构改造（对齐 Apifox）

```
┌─────────────┬──────────────────────────────────────────────────┐
│ 目录树        │  接口详情（右侧面板）                                │
│ (可嵌套)      │  ┌──────────────────────────────────────────┐    │
│              │  │ GET  /api/v1/orders/{id}   [设计中] [标签]   │    │
│ ▸ 订单        │  │ 名称：下单   ·  责任人：张三                 │    │
│   · 下单 GET  │  │ ─────────────────────────────────────── │    │
│   · 取消 POST │  │ 说明（单栏，可富文本/markdown）                 │    │
│ ▸ 用户        │  │ ─────────────────────────────────────── │    │
│   · 登录 POST │  │ 参数 │ Headers │ Body │ 响应   ← 结构编辑器  │    │
│ [+ 目录]      │  │ (字段名/类型/必填/说明/示例，非填值)         │    │
│              │  └──────────────────────────────────────────┘    │
│              │  （连接/环境由顶部环境选择器决定，不在此填）       │
└─────────────┴──────────────────────────────────────────────────┘
```

关键变化：
1. **左侧目录树**替代平铺列表（拖拽移动，对齐 Apifox）。
2. **主列 = 方法徽章 + 路径 + 名称**，`code` 降为次要（可悬停看）。
3. **详情面板**：上部基本信息（名称/状态/责任人/说明/标签），下部 **Params / Headers / Body / 响应** 四个**结构编辑器**——编辑字段的"型"（字段名/类型/必填/说明/示例），不是填"值"。
4. **搜索 + 筛选（方法/标签/状态）+ 分页**。
5. 入口收敛：主按钮「新建接口」+「导入 ▾」（下拉含 OpenAPI / 仓库扫描 / AI 生成），去掉三个平级按钮。

---

## 七、导入器改造

**多格式归一**（2026-10-14 补）：真实接口文档不止 OpenAPI 3 一种。新增
`testing_platform/importers/spec_normalizer.py`，把 **Swagger 2.0 / Postman
Collection v2.x / HAR 抓包** 归一成 OpenAPI 3.0，下游 `openapi_api_importer`
一套解析逻辑通吃四种格式（框架规格：「支持 swagger/apifox 等框架」）。

| 源格式 | 判定 | 归一要点 |
|---|---|---|
| OpenAPI 3.x | `openapi` / `paths` | 原样透传 |
| Swagger 2.0 | `swagger` | `host/basePath/schemes→servers`、`definitions→components.schemas`、`in:body→requestBody`、`in:formData→form 请求体`、`$ref #/definitions→#/components/schemas`、裸 schema 字段包进 `schema` |
| Postman v2.x | 顶层 `item` / `info.schema` 含 getpostman | 目录树→tag、`request→operation`、`url`→path+query 参数、`body.raw/formdata`→requestBody、`item.response[]`→responses |
| HAR | `log.entries` | 每条 entry→endpoint（method+path 去重合并）、首个路径段派生 tag、`request.postData`→requestBody、`response.content`→响应示例 |
| Apifox | 顶层 `apifox` / `apiCollection` | Apifox 导出的 OpenAPI 原样透传；原生格式 `apiCollection` 目录树→tag、`api.method/path/parameters/requestBody/responses`→operation（目录名作 tag） |

`openapi_api_importer.py` 重写映射（对归一后的 OpenAPI 3）：

| OpenAPI 字段 | 落到 |
|---|---|
| `paths.*.<method>` | `mc_test_api.method` |
| path 字符串 | `mc_test_api.path` |
| `operation.summary` | `name` |
| `operation.description` | `description`（说明） |
| `operation.tags` | `tags` + 建/挂对应目录（首个 tag；tag 里的 `/` 分层 → 嵌套目录） |
| `operation.parameters`（in=query/path/cookie） | `params` |
| `operation.parameters`（in=header） | `headers`（接口自身请求头） |
| `operation.requestBody.content.*.schema` | `request_body` + 推导 `body_type` |
| `operation.responses` | `responses`（多状态码） |
| `operation.deprecated` | `status='deprecated'` |
| `operation.operationId` | `source_ref.operationId`（溯源） |
| 源格式 | `source`（openapi/swagger/postman/har）+ `source_ref.imported_from` |

> 责任人（`owner_id`）、优先级（`priority`）OpenAPI 无对应，导入后留空由人补。

**格式判定（自动识别）**：`detect_format()` 按文档特征判定——HAR（`log.entries`）
→ **Apifox**（顶层 `apifox` / `apiCollection`）→ Postman（`item` / `info.schema`
含 getpostman）→ Swagger 2.0（`swagger`）→ OpenAPI 3（`openapi` / `paths` 兜底）。

**显式导入方式（不猜，推荐）**：`normalize_spec(spec, format=...)` 与
`api.import_openapi` 的 `format` 参数接受 `openapi3/swagger2/postman/apifox/har`，
**按用户选择解析、不依赖特征判定**；省略 / `auto` 时才自动识别。显式指定但解析失败
→ 报错里附「该文档看起来是 X，请改选该导入方式」，把「我不给协议」兜成人可操作的提示。

**dry-run 预览**：导入前返回「将新建 X / 更新 Y / 跳过 Z + **嵌套目录树**（按首个 tag 的 ``/`` 分层，如 ``用户中心/账号`` → 两级）+ 实际使用的 `format` + `total`」；无 tag 接口归「未分组」。前端以树形展示（可展开/折叠），一眼看清「哪些接口落到哪个目录」。

**手动导入进度**：`api.import_openapi` 支持 `offset/limit` 分批导入（返回 `total`
作分母），前端按 25 条一批循环调用并渲染进度条（`已处理 done / total`）。

**定时导入任务**（`mc_test_api_import_job`，用户定版 2026-10-15）：一条任务 =
文档来源（`spec_url` / `spec_text`）+ 导入方式（`source_format`，含 apifox）+ 节奏
（`schedule`，复用任务域 `next_fire` 的 `cron/interval/once`，`manual` 仅手动）。
定时任务**只能走 `spec_url`**（服务端无会话拿不到粘贴内容，service 层拦住）。
执行器 `importers/import_jobs.py`：`run_import_job` 取文档→按方式显式导入→写
`last_*`；`start_import_scheduler` 后台 asyncio 循环周期扫 `due_import_jobs()`
（enabled 且 `next_run_at` 到期）串行执行，启动挂 `user_platform.init()`，重启后继续。
surface 6 方法：`api.import_job.list/get/create/update/delete/run`。

**前端导入弹框（两步向导）**：点击「新建导入」→ **步骤一选导入方式**（6 张卡：
自动识别 / OpenAPI 3 / Swagger 2.0 / Postman / Apifox / HAR，各带说明与适用文件后缀）
→ **步骤二按方式填输入**（「粘贴/上传」与「URL 抓取」两通道切换）→ 预览（树形）。
**主按钮按通道分流**（用户定版 2026-10-22）：能否存任务取决于**文档来源**——只有 URL
服务端能重抓、存任务才有复用价值；粘贴/上传的原文在浏览器里，存不住也没意义：
- **URL 通道**：可存任务。节奏=仅手动 → 主按钮「保存并导入」（建 `kind=manual` 任务 +
  服务端跑一次 `api.import_job.run`，之后在列表「立即跑」重放）；节奏=定时 →
  「保存为定时任务」（只建任务，到点由调度器跑）。副按钮「保存为导入任务」= 只存不跑。
- **粘贴/上传通道**：不存任何任务。主按钮「立即导入」分批跑完（带进度条）；
  节奏/任务名整块隐藏。

选中的方式随请求带给后端，不靠自动识别。
工具栏另有「导入任务」按钮 → 管理弹框（列表 + 启停 + 立即跑 + 删除 + 下次/上次状态）。

---

## 八、分期实施建议

| 期 | 内容 | 依赖 | 状态 |
|---|---|---|---|
| **P1** | 数据模型：`mc_test_api` + `mc_test_api_folder`（Tortoise + DDL + 枚举；身份=id、version_id 先落） | 无 | ✅ 2026-10-13 |
| **P2** | 后端方法：`api.list/get/create/update/move/delete` + `api_folder.*`（surface 注册 10 个） | P1 | ✅ 2026-10-13 |
| **P3** | 前端：目录树 + 详情面板 + 结构化编辑器 + 搜索筛选（`apis-page.tsx`，路由 `/testing/apis`） | P2 | ✅ 2026-10-13 |
| **P4** | 导入器重写：新模块 `openapi_api_importer.py`（**全字段** → `mc_test_api` + **dry-run 预览** + 首个 tag 派生目录 + method+path 幂等）+ surface `api.import_openapi` + 前端导入弹框（预览→确认落库） | P2 | ✅ 2026-10-13 |
| **P5** | 接口↔用例/场景直连：`mc_test_api_case` + `mc_test_api_scenario` 两表 + service（幂等 link/unlink + 双向 list）+ surface 6 方法（`api.case.link/unlink`、`api.cases`、`api.scenario.link/unlink`、`api.scenarios`）；request 步骤「接口」Tab 加「引用接口」选择器（选中回填 method/URL + 记 `params.api_id`）；接口详情「关联」面板展示覆盖用例/引用场景并可解除 | P3 | ✅ 2026-10-13 |
| **P6** | 关联**改为派生**（用户定版 2026-10-14）：真相源 = 场景步骤 `params.api_id`；`mc_test_api_scenario` / `mc_test_api_case` 降为**反查索引**（步骤落库时重建，用例侧沿「用例→场景→步骤」传递）。service 新增 `sync_scenario_api_links` / `sync_case_api_links` / `sync_cases_of_scenario`（幂等重建 + 去重 + 悬空过滤）；`create_steps` / `link_case_scenario` / `unlink_case_scenario` 落库后自动同步；`delete_api` 连带清索引。surface 新增反向读 `scenario.apis` / `case.apis`。前端：场景信息卡「引用接口」汇总、用例详情「覆盖接口」汇总、request 步骤「接口」Tab 展示接口结构说明（params/headers/body/responses）；接口详情「关联」面板改只读 | P3 | ✅ 2026-10-14 |
| **P7** | 导入多格式 + 两步向导 + 弹框统一 + 进度 + 定时任务（用户定版 2026-10-15）：① 新增 `spec_normalizer.py`（Swagger 2.0 / Postman / **Apifox** / HAR → OpenAPI 3.0），导入器 `format` 参数显式指定（不猜）+ `auto`；② 导入弹框改**两步向导**（先选导入方式 → 再填输入：粘贴/上传 or URL 抓取）；③ 手动导入**分批进度**（`offset/limit` + `total` 分母 + 进度条）；④ 新增 `mc_test_api_import_job` 表 + service CRUD + 6 个 surface 方法（`api.import_job.list/get/create/update/delete/run`）+ `importers/import_jobs.py` 执行器与**定时调度器**（asyncio 后台循环，复用任务域 `next_fire` cron/interval/once，启动挂 `user_platform.init()`）+ 前端「导入任务」管理弹框；⑤ 接口管理页去掉页头标题/说明（用户要求），弹框统一走 `FormDialogShell`；⑥ `API_SOURCES` 扩为 `openapi/apifox/swagger/postman/har` | P2 | ✅ 2026-10-15 |

> **旧导入器保留**：`openapi_importer.py`（→ `mc_test_transport`，每 endpoint 一条）原样不动——存量测试/`transport.import_openapi` 在用。新导入器只复用其解析层（`load_document` 含最小 YAML 子集），落到接口实体表。

**兼容**：P1 迁移期间旧页面继续可用；新页面走 `mc_test_api`，稳定后下线旧页。

---

## 九、风险与注意

1. **迁移不可逆**：`code` 解析 method/path 需先备份（`backup/` 已有先例）。
2. **旧 code 引用**：若 `endpoint_*` 已被场景/用例引用，迁移时保留映射表（old_code → new_api_id）。
3. **版本机制**：新表须接入 `mc_test_change_record` 变更门（design §15.12），别绕开。
4. **前端高度链**：新页面注意 `platform-scope` 是 block 滚动容器，卡片不要挂 `flex-1`/`h-full`（six mode console ia 记忆）。
5. **环境边界**：接口只管"文档"，连接/鉴权/公共头/路径替换一律走环境，别把这两件事再合回一张表。

---

## 十、一页纸对照

| 维度 | 现在 | 目标（Apifox 式） |
|---|---|---|
| 实体 | 字符串行（方法/路径藏在 code 里） | 接口（method/path/params/headers/body/responses + 说明/状态/责任人/标签） |
| 组织 | 平铺 + 自由文本 module | 可嵌套目录树（folder_id + order） |
| 列表主列 | `endpoint_get_orders` | `GET` 徽章 + `/orders/{id}` |
| 请求/响应 | 无 | 结构化 params/headers/body（含 body_type）+ 多响应 |
| 管理属性 | 只有 status/version | + 说明/责任人/标签/优先级/排序/创建更新人时 |
| 导入 | 每 endpoint 一条 + 丢字段 | 目录 + 接口（全字段） |
| 检索 | 无 | 搜索 + 筛选 + 分页 |
| 与场景/用例 | 脱钩 | 关联表多对多 |
