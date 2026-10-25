# 17 · 生成链路分层——从「全交给 AI」到「确定性枚举 + AI 补全 + 机器门」

> 2026-10-12。触发：用户批评「很多东西想得很好但没着手——比如创建用例，怎么创建并没有具体方法，
> 完全交给 AI，导致冗余、漏用例、幻觉、杂乱、token 烧太快」。本文给出六块（需求/图谱/知识库/
> 用例生成/用例管理/执行）的具体改进方案。
>
> 外部依据（同日调研）：A=《18个Skills》文章（变换表/C01-C14/五维评分卡）；
> B=TestBrain（最小差异输出+参数白名单+降级重试+按接口 map）；
> C=测灵智测平台 36.134.215.31:8081 实测（测试点真表+四步流水线+GB/T技能树+dry-run→apply+全局队列）;
> D=WHartTest（ModuleSplitter 六级降级+需求六项评审+context_limits token 预算；反面：测试点只在 prompt）。

---

## 0. 现状六断点（全部验过代码）

| # | 断点 | 位置 | 后果 |
|---|---|---|---|
| 1 | `scenario.generate` 有全套确定性校验门（工具白名单/未注册即幻觉/不过不落库）但**生产零调用**，`start_run("coder")` 只存在于 docstring | methods.py:5215 / workflow/__init__.py:22 | 用例→脚本整段断线 |
| 2 | **transport 不存参数 schema**：`_collect_parameters` 解析了 type/required/default，`_merge_common_data` 只挑 header/query 的默认值，结构扔掉 | openapi_importer.py:496,507 / models.py:756 | AI 连接口几个参数都不知道——「只能全丢给 AI」的真因 |
| 3 | 用例**无幂等键**（code=自增序号），`_next_code` 每插一条捞全表算 max（O(n²)）且无锁 | assets/service.py:130,209 | 重跑必产重复；批量生成底层不支持 |
| 4 | `case.generate` 的 `scope` 只 `json.dumps` 进 prompt，**从不用于过滤**；`case.list`/`transport.list` 无分页（128 方法仅 2 个带 limit） | methods.py:3116,1674,3937 | 几百接口时 Agent 一次全量 dump 灌爆上下文 |
| 5 | `coverage_gaps` 列存在但**全仓零写入**；无覆盖标准表 | knowledge/models.py:374 | 漏没漏无法度量，只能信 AI |
| 6 | `complete_run(..., {"cases": []})` 硬编码空；`running` 态无人置 | methods.py:3080 / workflow/service.py | 生成产出无法对账；前端 5 分钟轮询超时即弃 |

**根因一句话：流水线建成了「词汇表」（7 角色 + 128 方法），不是「流程」（角色间产物契约）。**
design.md:285 的 14 段里「测试设计」段（designer 的本职产出=测试点）被 case_generation.md 的
「需求→用例」两段直跳吞掉了——没有可分批、可查漏、可确认的中间粒度，一切只能压给 AI 自觉。

---

## 1. 设计原则（每条都有出处）

1. **按「确定性 vs 需要模型判断」分层**，不按抽象层分。机器能枚举的绝不问模型（B 的固定断言、
   A 的变换表、C 的四步拆分）。
2. **AI 产出「提案」，平台裁判落库**。C 的 dry-run→人审→apply；我们的 scenario.generate 校验门
   已是此形态——把它推广到用例域。
3. **覆盖度必须有外部标准表**，否则不可度量。A=C01-C14，C=GB/T 25000.10 技能树。我们取
   C01-C14 做代码常量轴 + 子特性树后续可扩。
4. **每批上下文封闭**：一个批次的输入 = 一条需求/一个模块的接口参数摘要 + 已有用例的
   slot 清单（不是全文）。token 预算显式计算（D 的 context_limits：模型表+tiktoken+5%预留）。
5. **最小差异输出**（B）：骨架字段平台预填，AI 只填业务语义差量；参数名不在 schema →
   warn+丢弃，不兜底造。
6. **拒绝 D 的反向策略**（单条闭环+逐条子代理审批）：在「几百接口×每接口≥4条」的量级下极慢极贵；
   我们用「批量枚举 + 批次提案 + 机器门 + 队列人审」替代。

---

## 2. 目标流水线（L0–L4）

```
L0 确定性事实（0 token）
   导入时落参数 schema → mc_test_transport_param
        ↓
L1 机械枚举（0 token，秒级）
   变换表 × 参数 schema × 通用轴(鉴权/幂等/越权)
   → 骨架用例 slot 批量 bulk_create（幂等键去重）
        ↓
L2 语义补全（受预算的少量 token，分批异步队列）
   批1：description 为空的接口 → 按 module 分批补业务语义（B 模式）
   批2：需求驱动的业务流用例 → 按 requirement 分批，产出「测试点提案」
        ↓  （人工确认测试点——C 的 scenario/testpoint 两级真表）
   批3：确认后的测试点 → 用例提案（最小差异：只填 title/intent/steps/expected 差量）
        ↓
L3 机器门（0 token）
   参数白名单 join / slot 幂等冲突 / 指纹去重 / 条数预算 / C01-C14 覆盖核对 /
   expected 必须三层断言结构(status_code+业务code+关键字段)
        ↓
L4 人工确认
   pending_confirmation 队列按 module/测试点分组，批量 approve/reject/edit-then-apply
        ↓
L5（后续）用例→场景：骨架场景同样可枚举（request 步骤从 transport+param_schema 生成，
   断言从 expected 生成），AI 只补多步业务流——走已建好的 scenario.generate 校验门
```

---

## 3. 数据结构改动

### 3.1 新表 `mc_test_transport_param`（治断点 2）

> **⚠ 地基变更（2026-10-13，随 interface-management-redesign 落地）**：本节方案**已被取代**。
> 并发工作把接口建成一等实体 `mc_test_api`（Apifox 式）：`params`/`headers`/`request_body`
> /`responses` 全部结构化落 JSONB（见 `docs/ai-testing-platform/interface-management-redesign.md`
> §5.1），**不建 `mc_test_transport_param`**——否则同一份参数结构存两处（正是要根治的冗余）。
> L1 枚举器（`testing_platform/design/transform_table.py`，已落地）直接读
> `mc_test_api.params[]` + `request_body.properties` 产出骨架 slot。本节保留作历史记录。

```sql
CREATE TABLE IF NOT EXISTS mc_test_transport_param (
    id           UUID PRIMARY KEY,
    transport_id UUID NOT NULL,             -- → mc_test_transport
    project_id   UUID NOT NULL,
    name         VARCHAR(128) NOT NULL,
    location     VARCHAR(16) NOT NULL,      -- query|path|header|body|cookie
    data_type    VARCHAR(32),               -- integer|string|number|boolean|array|object|enum
    required     BOOLEAN DEFAULT FALSE,
    min_value    JSONB,  max_value JSONB,   -- 数值/长度边界
    enum_values  JSONB DEFAULT '[]',
    example      JSONB,
    source       VARCHAR(16) DEFAULT 'openapi',  -- openapi|scanner|ai|human
    created_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (transport_id, name, location)
);
```

- `import_openapi` 改：`_collect_parameters` 的结果**整表落库**（现在只挑 header/query 默认值）。
- route_scanner 扫出的 handler 参数暂缺 → source='scanner'，留给 L2 批1 由 AI 读代码补。
- 加列/建表迁移：照 projects/service.py:85-125 的 `_column_exists` 探测→ALTER 模式；
  新表走 DDL 列表 `IF NOT EXISTS` 即可。

### 3.2 新表 `mc_test_testpoint`（补「测试设计」层，C 是真表、D 是教训）

```sql
CREATE TABLE IF NOT EXISTS mc_test_testpoint (
    id            UUID PRIMARY KEY,
    project_id    UUID NOT NULL,
    code          VARCHAR(32) NOT NULL,      -- TP-0001（号段批量取，见 3.5）
    requirement_id UUID,                      -- 主脊：挂需求
    transport_id  UUID,                       -- 接口驱动的点可挂 transport
    title         VARCHAR(512) NOT NULL,
    design_method VARCHAR(32),                -- 等价类|边界值|判定表|状态迁移|场景法|错误推测|正交|因果图
    coverage_axis VARCHAR(8),                 -- C01..C14（机械枚举的轴）
    est_cases     INT DEFAULT 1,
    basis         VARCHAR(16) NOT NULL,       -- fact|assumption|to_confirm（A 的事实/猜测三分）
    basis_ref     JSONB DEFAULT '{}',         -- 需求条目/文档位置/参数名
    status        VARCHAR(32) DEFAULT 'candidate', -- candidate→confirmed→generated
    created_by    UUID NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, code)
);
```

- L1 骨架枚举**同时产测试点**（coverage_axis 有值的那些）——测试点不是 AI 专属，机械枚举的
  点直接入库，AI 批只产 `basis='assumption'|'to_confirm'` 的业务流点。
- 用例加列 `testpoint_id UUID`（挂点，可空——手工用例不必有）。

### 3.3 `mc_test_case` 加两列（治断点 3）

```sql
ALTER TABLE mc_test_case ADD COLUMN slot_key   VARCHAR(256);  -- 幂等键，见下
ALTER TABLE mc_test_case ADD COLUMN testpoint_id UUID;
-- 部分唯一索引：slot_key 非空时项目内唯一
CREATE UNIQUE INDEX ... ON mc_test_case(project_id, slot_key) WHERE slot_key IS NOT NULL;
```

`slot_key` 格式：
- 机械点：`api:{transport_code}:{param}:{class}:{variant}`
  （class=C12输入校验 等；variant=missing|empty|min-1|max+1|type-error|...）
- 通用轴：`auth:{transport_code}:no-token|expired|cross-tenant`、`idem:{transport_code}:repeat`
- AI 业务点：`flow:{requirement_code}:{tp_code}:{seq}`

重跑生成 = `bulk_create(ignore_conflicts=True)`，冲突即跳过——**冗余从存储层根治**。
`TC-` code 保留为展示号，批量场景一次取号段（`SELECT max` 一次 + 内存自增），
并发插号加 advisory lock（治 O(n²) 与竞态）。

### 3.4 覆盖标准（治断点 5）

`testing_platform/design/coverage_axes.py`：C01-C14 常量表（照 A 原文），每条含
`{code, name, applies_to}`（接口适用轴/流程适用轴）。覆盖统计 = `testpoint.coverage_axis`
× `transport` 矩阵的 SQL count——`coverage_gaps` 从此有写入者：每次生成/确认后重算。

### 3.5 `workflow run.output` 契约（治断点 6）

designer 的 output 从 `{"cases": []}` 改为：

```json
{"skeleton": {"created": 1832, "skipped": 141, "axes": {"C12": 620, ...}},
 "batches": [{"batch": "desc:支付", "status": "done", "tokens": 41230}, ...],
 "testpoints": {"candidate": 57, "confirmed": 40},
 "pending_confirmation": 97}
```

`running` 态由批处理调度器置（见 4.3）。

---

## 4. 能力面方法改动

### 4.1 改造 `case.generate` → orchestrator

```
入参不变（project_id/scope/requirement_id/instruction + 新增 max_ai_cases 预算）
同步部分（秒回）：
  1. scope **真过滤**：module→transport 集合、requirement_id→其挂的 transports+已有 testpoints
  2. L1 枚举：变换表 × param_schema → 骨架用例+机械测试点，bulk_create
  3. 开 run，置 running，返回 {run_id, skeleton: {created, skipped}}
后台部分（队列）：
  4. 批1（补 description）：description 为空 ∩ scope → 按 module 分批，每批 ≤20 接口
  5. 批2（业务测试点提案）：按需求分批，产出 testpoint(basis=assumption) → candidate
  6. 人工确认测试点（testpoint.confirm）→ 批3：按测试点生成用例提案
  7. 每批完成回写 run.output.batches；失败仅 fail 该批（C 的「失败不连坐、可单批重跑」）
```

### 4.2 新方法（最小集）

| 方法 | kind | 说明 |
|---|---|---|
| `transport.params.put` / `transport.params.list` | write/read | 参数 schema 读写（导入器与 L2 批1 共用；白名单校验数据源） |
| `case.enumerate` | execute | 纯 L1（可单独重跑，0 token） |
| `testpoint.create` / `.list` / `.confirm` / `.generate_cases` | write/read | 测试点四件套；`.confirm` 是人工门；`.generate_cases` 按点出提案 |
| `case.review` | read | 五维评分卡（A）：覆盖/断言结构/异常流占比可机器算，可执行性 AI 辅助 |
| `case.batch_update` / `.batch_confirm` / `.batch_reject` / `.delete` | write | 队列人审的批量操作（补上无 delete_case 的洞） |
| `coverage.report` | read | module × C01-C14 矩阵 + gaps 写回 |
| `requirement.review` | execute | 六项专项分析（D：完整/可测/可行/清晰/一致/逻辑），产出 ReviewIssue → 走 question.raise |

分页：`case.list`/`transport.list`/`scenario.list`/`testpoint.list` 一律加
`limit/offset`（照 knowledge.query:589）。**这是硬前提**——否则封闭上下文批次无法实现。

### 4.3 批次队列

单进程起步：`asyncio.Semaphore(N)` + run 内批次串行/并行；`workflow.cancel(run_id)`
支持取消（C 的 queue/cancel 模式）。暂不引 Celery（D 用 Celery 是 Django 单体形态，
我们是 FastAPI 同进程，先闸后队）。

### 4.4 prompt 改动（`case_generation.md`）

- **删「代码图谱」上下文来源**；加硬规则：「不得以代码实现结构（调用关系/分支）作为用例依据」。
- 生成协议改为：「骨架用例（slot_key 机械点）已由平台枚举，**你不得重复产出机械点**；
  你只产：①业务流测试点提案 ②确认后测试点的用例差量」。
- 每条用例字段契约加：`expected` 必须三层断言结构 `{status_code, biz_code, key_fields}`（A/B）。
- 配比硬规则：每功能点 1 正向 + ≥3 异常/边界（A）——机械枚举天然满足，AI 批只补业务流。
- 变换表（A×B 合成，落进 `design/transform_table.py` 成代码，不再只是 prompt 文字）：

| 参数类型 | 机械变体 |
|---|---|
| 必填 | 合法 / 缺失 / 空串 / null |
| 整型(min~max) | 合法 / min-1 / max+1 / 非数 / 0 / 负 |
| 字符串(m~n) | 合法 / 空 / 超长 / 特殊字符 / 注入串 |
| 枚举 | 每合法值 / 非法值 |
| 布尔 | true / false / 传字符串 |
| 数组 | 合法 / 空数组 / null / 巨量 |
| 全接口通用 | 无 token / 过期 token / 越权 id / 重复提交 |

禁止退化：不许产出 `"a".repeat(256)` 这类公式表达式（B 实证）。

---

## 5. 六块改进建议（用户点名）

### 5.1 需求
- 需求是主脊的定位**不变**（requirement_id 全链已挂、`requirement.trace` 走 ID 反查——
  比 C 靠 `source_doc_title` 文本反查强，别改坏）。
- 补「文档→需求条目」抽取：analyst 链路已产 knowledge_item(kind=requirement 走可测闸)，
  加一步把 confirmed 的 requirement 条目升 `TestRequirement`（`requirement.ingest`）。
- 加 `requirement.review`（D 六项），验收标准缺 condition/expected 的走 question.raise
  ——疑问一等公民已建好，接上即可。
- 大文档切分：source_fetch 现按整文档入库；加标题感知 chunk（B 的 chunk_by_title、
  D 的六级降级），chunk 级 content_hash 做增量。

### 5.2 知识图谱——**重定位**（用户判断成立，外部全部印证）
- **移出**生成上下文（5.4 的 prompt 改动）。四路外部证据：A 全文无图谱；C 的图谱是
  需求侧实体（功能模块/业务规则/数据实体/接口/角色权限），不是代码调用图；B 无图谱，
  调用图只用于影响分析；D 的图谱=需求文档抽取。
- **移入三个岗位**：
  1. **影响分析/增量选例**：`release.impact` 现在是全表捞+Python 比对（methods.py:5829-5854），
     改走 KnowledgeEdge SQL join（anchor→item→case.confirm_ref）；接任务选例。
  2. **需求对账**：requirement↔anchor 覆盖检查，产「实现了但没需求」「有需求没实现」
     两类缺口 → question.raise。**这才是图谱的正经用途**。
  3. **缺陷辅助**：bug 定位推荐代码位置（A 的 Bug分析 skill），anchor 表就是现成的索引。
- `project.graph.query` 保持前端可视化定位，从 Agent 工具清单摘掉。

### 5.3 知识库
- 版本隔离补实：`list_items`/`knowledge.query` 加 version_id 过滤（现恒 NULL legacy）。
- item kind 补四类（C 实证有效）：`business_rule / test_pattern / pitfall / risk_scenario`；
  **pitfall+risk_scenario 是真正该喂生成的知识**（历史踩坑=错误推测法的原料），
  与「图谱不喂生成」不矛盾——喂的是缺陷模式，不是调用结构。
- 增量：`graphify_runner.update()` 已实现无人调——接进 `project.code.sync`；
  content_hash 变化→标 stale（现 stale 只有读侧判定，零写入者）；upsert 补删除传播
  （全模块零 delete，失活节点永存）。

### 5.4 用例生成
- 全部见 §2-§4。一句话版：**机械点枚举（0 token）→ 业务点分批提案（预算内）→
  机器门（白名单/幂等/去重/覆盖）→ 队列人审**。AI 不再「创建用例」，AI 创建的是
  过门前的「提案」。
- `confirm_source` 确认门保留且前移：机械点自动 `doc`（依据=参数 schema 本身），
  AI 点默认 `to_confirm` 进队列。

### 5.5 用例管理
- 幂等/分页/批量/delete 见 3.3/4.2。
- 状态机走现成 `draft→pending_confirmation→active→deprecated→archived`，
  **不学 C 分两张表**（AI 库/人工库）——我们有 origin+status+确认门，单表+队列即可；
  C 分库是因为它没有状态机，是 workaround 不是优点。
- 加 `case.review` 五维评分卡（A）：可机器算的先机器算（覆盖占比/断言结构/异常流配比），
  主观维度 AI 辅助、结果落 change_record。
- 覆盖度视图：module × C01-C14 矩阵 + 按需求/测试点的 covered 率（C 的
  characteristicCovered/subCharacteristicCovered 形态）。

### 5.6 其他（执行闭环——生成得再好，跑不起来等于零）
- `run.execute` 接 `PgResultWriter`（现只有 seed_demo 传）；`TestRun.status` 补状态流转。
- transport 物化进执行：executor 读 `scenario.transport_id` → 注入 auth/common_headers/
  path_rewrite（现生产路径不可达，tools.py:198 等 params 里凭空出现的 transport）。
- 方法脚本注册表接进 `script` 步骤型（现直接抛未注册）——沙箱议题另开 spec。
- `TestTask.schedule` 无任何组件触发——接顶层 scheduler（现只调源同步）。
- L5 用例→场景：骨架场景可枚举（request 步骤+断言从 transport/param/expected 生成），
  走已建好的 `scenario.generate` 校验门；AI 只补多步业务流。把 `start_run("coder")`
  从 docstring 变成真调用。
- 遥测：批次 token 用量、队列状态端点（C 的 /queue/status 形态）、日志镜像进度
  （B 的 ProgressMirrorHandler 思路，接我们的 realtime gateway）。

---

## 6. 分期与验收

### P0 · 打通「几百接口零 token 出骨架」（1-2 周）
transport_param 表+导入器落库；slot_key+唯一索引+号段发号；`design/` 枚举器+变换表；
`case.enumerate`；`case.generate` 改 orchestrator（L1 同步+批队列起步）；
scope 真过滤；四个 list 加分页。
**验收：300 接口项目重跑两次生成 → 第二次 created=0 全 skip；骨架 ≥1500 条；
token 消耗=0；`coverage.report` 出 C0x 矩阵。**

### P1 · 测试点层+人审队列（1-2 周）
mc_test_testpoint；批1 补 description；批2/批3 提案流；batch_confirm/reject；
`case.review`；前端生成弹框改三段展示（骨架数/批次进度/待审数）+ 取消。
**验收：需求→测试点→用例提案→人审→active 全链路走通；AI 产出被白名单拦截率可观测。**

### P2 · 图谱重定位+执行闭环（后续）
release.impact 走边查询；需求对账；stale 写入+graphify update；
run.execute 落库+transport 物化；L5 骨架场景+coder 接线。

### 风险
- 存量用例无 slot_key：允许 NULL（部分唯一索引），一次性回填脚本按
  (transport,title 正则) 尽力匹配，匹配不上保持 NULL——不与新数据冲突。
- `_next_code` 改造影响 10+ 调用点：先只改 TestCase 批量路径，其余保持。
- dsh 单轮回合承载批任务：每批一个独立 run_chat（cwd/上下文封闭），不共享会话。

---

## 7. 出处对照（哪个机制从哪来）

| 机制 | 出处 |
|---|---|
| 变换表按类型枚举变体 | A 的 C01-C14 + B 的 prompt 规则表（合成落代码） |
| 最小差异输出/参数白名单/固定断言/降级重试 | B iface_case_generator |
| 测试点/场景真表、四步流水线、单步重跑 | C 实测（D 的反面教训：只在 prompt=没有） |
| 覆盖=外部标准表可算数 | A（C01-C14）+ C（GB/T 树） |
| 事实/假设/待确认三分 | A 需求拆解 + 我们的 confirm_source 天然对应 |
| token 预算→批次大小 | D context_limits（tiktoken+5%预留三态） |
| dry-run→人审→apply | C ai-bulk |
| 全局队列+取消+僵尸判定 | C /api/queue |
| 日志镜像进度遥测 | B ProgressMirrorHandler |
| 确定性优先+校验门 | 我们自己的 scenario.validate 设计（已建，待接线） |
