# 18 · 测试用例自动创建——基准·标准·分类·评审·确认·规模化

> 2026-10-13。承接 17 号文（分层流水线）。本文只讲一件事：**用例到底怎么自动创建**——
> 以什么为基准、生成标准是什么、怎么分类细化、怎么贴合实际、怎么评审、怎么人工确认，
> 以及「一万个需求」时怎么不把 token 烧成天文数字。
>
> 外部依据：A=《18个Skills》（测试点五字段/C01-C14/五维评分卡/事实-猜测三分）；
> B=TestBrain（最小差异输出/参数白名单/降级重试/按接口 map）；
> C=测灵智测实测（需求→场景→测试点→用例四级真表、GB/T技能树、四步可重跑、dry-run→apply）；
> D=WHartTest（六级降级切分/六项需求评审/context_limits token预算；反面：测试点只在prompt）。

---

## 1. 核心结论先行

**用例不是「以文档为基准」生成的，是「以三个正交基准」生成的，每个基准的粒度不同：**

| 基准 | 粒度（生成的最小单元） | 生成方式 | token |
|---|---|---|---|
| **接口契约**（transport + param schema） | **一个参数** | 机械枚举（变换表） | **0** |
| **业务规则**（rule / acceptance_criteria） | **一条规则** | 判定表/等价类，规则级枚举 | **0** |
| **业务流程**（需求文档 / 场景） | **一个功能点** | AI 提案 + 人确认 | 受预算 |

**关键洞察：前两个基准的产物是「算」出来的，不是「问」出来的。**
一万个需求之所以炸 token，是因为把「业务规则」和「接口契约」也丢给了 AI——
而这两类里 80%+ 的用例本可以由规则引擎枚举。真正需要 AI 的只有第三类：跨步骤的业务流。

---

## 2. 「以什么为基准」——三个基准的精确定义

### 基准一：接口契约（覆盖 API 类用例，0 token）

> **⚠ 地基变更（2026-10-13）**：本节原写「`mc_test_transport` + 17 号文新表 `transport_param`」。
> 并发工作已落地 **`mc_test_api`**（接口一等实体，Apifox 式；`params`/`headers`/`request_body`
> /`responses` 结构化 JSONB，身份=id）——**基准一的事实源改为读 `mc_test_api`**，不再建
> transport_param（同一份参数结构存两处=新冗余）。
> 实现已落地：`testing_platform/design/transform_table.py` 的 `param_variants` /
> `enumerate_api`（读 `params[]` + `request_body.properties`，纯函数 0 token，测试
> `tests/test_design_transform_table.py` 30 例全过）。slot_key 格式：
> 参数轴 `api:{api_id}:{param}:{axis}:{variant}`，通用轴 `auth:{api_id}:{variant}` /
> `idem:{api_id}:{variant}`。

**基准实体**：`mc_test_transport`（已有）+ `mc_test_transport_param`（17 号文新表）。

**生成单元**：**一个 (transport, param, 变体)** 三元组 = 一条用例。

**基准从哪来**（三级回退，保证基准不是空的）：
1. `source='openapi'`：导入 OpenAPI 时 `_collect_parameters` 已解析 name/type/required/default，
   整表落 `transport_param`（当前 `_merge_common_data` 把结构扔了——这是要修的第一处）。
2. `source='scanner'`：route_scanner 扫到路由但无参数信息 → 标记待补。
3. `source='ai'`：AI 读 handler 代码补参数（L2 批1，只对 description 为空的接口跑）。

**贴合实际**：参数类型/边界直接来自真实 schema（`min`/`max`/`enum`/`example`），
不是 AI 猜的——这是「贴合实际」的第一层含义。

### 基准二：业务规则（覆盖规则类用例，0 token）

**基准实体**：`KnowledgeItem(kind='rule')`——已有可测闸 `check_testability` 要求
`condition` + `expected` 两字段（agent_ingest.py:37）。**有这两个字段的规则 = 可直接机械生成**。

**生成单元**：**一条 rule**（一个 condition→expected 对）= 一个判定表行。

**基准从哪来**：analyst 链路（`knowledge.ingest` / `analyst.commit`）从文档抽 rule，
过可测闸（缺 condition/expected 的**不进基准**，走 question.raise 挂起）。
`acceptance_criteria`（TestRequirement 的 `[{id, text}]`）同理——但注意：**它现在只有 text，
没有 condition/expected 结构**，所以要么升级为结构化，要么只作业务流基准的输入。

**贴合实际**：规则的 condition/expected 是从需求/代码 docstring 里抽的事实，
生成的是「规则被违反时会怎样」——这是错误推测法（A）的确定性版本。

### 基准三：业务流程（覆盖场景类用例，AI + 人确认）

**基准实体**：需求文档的**功能点**（不是整篇文档！见 §6 切分）。

**生成单元**：**一个功能点** = 一组测试点提案。

**为什么必须 AI**：跨接口的状态流转、备选流、时序——这些机器枚举不出来。
C 的四步流水线（analyze-scenarios → generate-points → generate-cases）就是这一层。

**这一层才是「测试点」的归属**——测试点挂在功能点下，是 AI 产出的、待确认的中间粒度。

---

## 3. 「生成的标准是什么」——可执行的判定标准

每条生成物必须满足的**机器可判定标准**（不过关不落库）：

### 3.1 机械枚举类（基准一/二）的标准

| 标准 | 判定方式 |
|---|---|
| 参数名在 schema 里 | `transport_param` join 命中 |
| 变体来自变换表 | variant ∈ 预定义枚举 |
| 三层断言结构 | `expected` 必须含 `status_code` + `biz_code` + `key_fields` |
| slot 唯一 | `(project_id, slot_key)` 部分唯一索引 |

### 3.2 变换表（基准一，落成 `design/transform_table.py` 代码常量）

| 参数类型 | 变体（每变体=1条用例） | 断言 |
|---|---|---|
| required | 合法 / 缺失 / 空串 / null | 合法→200；其余→400 |
| integer(min~max) | 合法 / min-1 / max+1 / 非数 / 0 | 边界外→400 |
| string(len m~n) | 合法 / 空 / 超长(m-1,n+1) / 特殊字符 / 注入串 | 超限→400 |
| enum | 每个合法值 / 一个非法值 | 非法→400 |
| boolean | true / false / "true"(字符串) | 类型错→400 |
| array | 合法 / 空数组 / null / 超量 | 空/null→400 |
| **全接口通用** | 无token / 过期token / 越权id / 重复提交 | 401/403/409 |

### 3.3 判定表（基准二，规则级）

一条 rule 的 `condition`（如 `amount > 1000 && user.level == 'vip'`）：
- 拆 condition 的原子谓词（`amount>1000`、`user.level=='vip'`）
- 判定表行 = 谓词的**真/假组合**，每行一条用例，expected 取 rule 的 `expected`
- 简化：n 个谓词 → 2^n 行，n>4 时用正交（A）取样

### 3.4 覆盖标准（外部标准表，治「漏用例」）

`design/coverage_axes.py`：**C01-C14**（A 原文，代码常量）——
正常流/异常流/边界值/权限/并发/重试/审计/会话恢复/数据一致性/上下游依赖/幂等性/
输入校验/状态转换/安全约束。

**覆盖度 = SQL 算的**：`testpoint.coverage_axis × transport` 矩阵 count，
`coverage_gaps` 从此有写入者（现全仓零写入）。

### 3.5 条数标准（治「冗余」）

| 层级 | 条数约束 |
|---|---|
| 单接口 | ≥ (参数数 × 每参数变体数) + 4（通用轴）——机械下限 |
| 单功能点 | 1 正向 + ≥3 异常/边界（A）——AI 批约束 |
| 单需求 | ≥ 其 acceptance_criteria 条数（每条验收标准至少 1 条用例） |

---

## 4. 「怎么分类怎么细化」

### 4.1 三级分类（对标 C 的字段分组）

| 维度 | 字段 | 取值 |
|---|---|---|
| **功能分组** | `module` → `feature` | 功能模块 / 功能点 |
| **质量特性** | `test_type` | functional/boundary/exception/permission/security/api/performance/ui/ai |
| **设计方法** | `intent` 内嵌 + `testpoint.design_method` | 等价类/边界值/判定表/状态迁移/场景法/错误推测/正交/因果图 |
| **覆盖轴** | `testpoint.coverage_axis` | C01-C14 |

### 4.2 细化顺序（从粗到细，每级可独立统计）

```
需求 (REQ-xxxx)
  └ 功能点 (module.feature)            ← AI 从文档切出的功能点
      └ 测试点 (TP-xxxx)               ← 生成单元，挂 design_method + coverage_axis
          └ 用例 (TC-xxxx)             ← 最小落库单元，挂 testpoint_id
```

**测试点是关键细化层**——它是「可分批、可查漏、可人工确认、可统计覆盖」的最小单位。
C 建了真表，D 只在 prompt 里（教训：没有 DB 实体就没法分批确认）。

### 4.3 分类的「自动 vs 人工」

| 分类字段 | 谁填 |
|---|---|
| module/feature/requirement_id/transport_id | **平台预填**（从 scope/需求/接口上下文推导） |
| test_type/coverage_axis/design_method | 机械枚举**自动定**；AI 批**提案** |
| priority | 机械枚举按 coverage_axis 映射（C04权限/C14安全→P0）；AI 可调 |

---

## 5. 「怎么贴合实际」——三道真实性闸

1. **基准真实性**：参数来自真实 schema、规则来自真实 condition/expected——
   **AI 不得凭空造基准**（17 号文硬规则）。
2. **参数白名单**（B 实证）：AI 产出的用例里出现的参数名，**不在 transport_param 里就 warn+丢弃**，
   不兜底造。这是防幻觉最硬的一道。
3. **可追溯**（A 的三分）：每条用例的 `confirm_source` + `basis_ref` 指向真实依据——
   机械点 `confirm_source=doc`（依据=参数schema本身），AI 点 `to_confirm` 进人审队列。

---

## 6. 「一万个需求怎么办」——切分与预算（核心）

**前提事实（已核代码）**：`ingest_raw`（source_fetch.py:267）**整份文档存一行 content_text，
不切分**。所以现在的链路天然会把整篇 PRD 塞进 prompt。要治必须引入切分层。

### 6.1 三层切分（D 的六级降级 + 我们自己的锚点）

```
文档 → 章节(chunk) → 功能点 → 测试点
```

- **文档→章节**：标题感知切分（B 的 chunk_by_title / D 的六级降级：
  标题层级→文档结构→章节→字符长度→平分）。新表 `mc_knowledge_chunk`（raw_id, seq, heading_path, text, content_hash）。
- **章节→功能点**：AI 抽（小单元，一次一章），产出功能点清单（挂 requirement/module）。
- **功能点→测试点**：AI 提案（§2 基准三）。

### 6.2 token 预算（D 的 context_limits 思路，落成代码）

`design/token_budget.py`：
- 模型上下文表 + tiktoken 计算 + 5% 预留（下限 4000）
- 每批的输入预算 = 模型窗口 − 预留 − 输出预估
- 三态：`OK / RECOMMENDED（该缩）/ SPLIT_REQUIRED（必须拆）`

### 6.3 一次加载多少（批次编排，治 token 天文数字）

**核心：永远不整篇加载，永远按功能点分批，每批上下文封闭。**

| 批次 | 输入（封闭） | 输出 | 大小控制 |
|---|---|---|---|
| 补参数 | 1 个接口的 handler 源码 | 该接口的参数 schema | 按接口逐个，≤1 接口 |
| 抽功能点 | 1 个章节 chunk | 该章节的功能点清单 | 按章节，≤1 chunk |
| 业务测试点 | 1 个功能点 + 其关联的 rule/接口参数**摘要** | 该功能点的测试点提案 | 按功能点，≤1 功能点 |
| 用例提案 | 1 个测试点 + 已有用例的 slot 摘要 | 该测试点的用例提案 | 按测试点，≤1 测试点 |

**关键：批次大小由 token_budget 动态定，不由「需求数」定。**
一万个需求 = 一万次小批次调用（可并行、可断点续跑、失败不连坐），
不是一次巨型调用。每次调用输入 < 8k token，输出 < 2k token。

### 6.4 增量（治重复加载）

- 文档级：`ingest_raw` 已有 content_hash 幂等（同文档不重入）。
- 章节级：`mc_knowledge_chunk.content_hash` → 只对变化的章节重抽功能点。
- 功能点级：功能点的 `source_chunk_hash` → 只对受影响功能点重生成测试点。
- **重跑生成 = 全 skip**（slot 幂等），不是全量重来。

---

## 7. 「怎么评审用例」——五维评分卡（A）+ 机器优先

`case.review`，**能机器算的先机器算**：

| 维度 | 权重 | 判定 |
|---|---|---|
| 覆盖完整性 | 25% | **机器**：该用例覆盖的 coverage_axis / 应有轴数 |
| 断言质量 | 25% | **机器**：expected 是否三层结构 + 断言是否可执行 |
| 异常流覆盖 | 20% | **机器**：该功能点的异常/边界用例占比 ≥3:1（A） |
| 可执行性 | 15% | AI 辅助：步骤是否明确可执行（无「验证功能正常」这类空话） |
| 可维护性 | 15% | **机器**：是否引用 slot_key/变量而非硬编码值 |

- ≥85 合格 / 70-84 需补充 / <70 重生成（A 的阈值）
- 评审结果落 `change_record`，驱动状态流转

---

## 8. 「怎么人工确认」——三段式确认，不是一次性

**确认不发生在最后，发生在三个点**（C 的 dry-run→人审→apply 分三处）：

| 确认点 | 确认对象 | 谁 | 批量性 |
|---|---|---|---|
| **① 需求确认** | 功能点清单是否对（AI 切的章节功能点） | 人 | 按章节批量过 |
| **② 测试点确认** | 测试点提案（`status=candidate→confirmed`） | 人 | 按功能点批量过 |
| **③ 用例确认** | 确认后的测试点的用例提案（`pending_confirmation→active`） | 人 | 按测试点批量过 |

**机械枚举的用例（基准一/二）不需要 AI 确认，但需人抽检**——
它们 `confirm_source=doc` 自动 active，进抽检队列（不阻塞）。

**确认界面**（前端）：按 module/功能点/测试点分组，支持批量 approve / reject /
edit-then-apply（C 的 `editedContents` 模式）。

---

## 9. 数据流总图

```
                    ┌─ 接口契约 (transport_param) ─→ 机械枚举 ─┐
基准（三个正交）────┼─ 业务规则 (rule.condition/expected) ─→ 判定表 ─┤
                    └─ 业务流程 (章节→功能点) ─→ AI提案 ──────────┤
                                                                  ↓
                                              ┌─────────── 用例池 (mc_test_case) ───────────┐
                                              │ slot_key 幂等 · testpoint_id 挂点 · 三层断言 │
                                              └──────────────────────────────────────────────┘
                                                                  ↓
                        机器门（白名单/幂等/去重/覆盖核对/断言结构）→ 五维评审
                                                                  ↓
                                        三段确认（需求→测试点→用例）→ active
```

---

## 10. 与 17 号文的关系

- 17 号文 = 分层架构（L0-L4）+ 六块改进（需求/图谱/知识库/生成/管理/执行）。
- 18 号文（本文）= 17 号文里「用例生成」这一块的**展开**：基准、标准、分类、评审、确认、规模化。
- 新增表（相对 17 号文）：`mc_knowledge_chunk`（切分单元）。
- 新增代码模块：`design/transform_table.py`、`design/coverage_axes.py`、`design/token_budget.py`。
- 17 号文的 P0/P1 分期不变，本文补充 P0 的具体验收（见 §6.3）。

---

## 11. 待确认（本文未定死、需用户拍板）

1. **acceptance_criteria 是否结构化**：现在 `[{id, text}]` 只有文本。若升级为
   `[{id, condition, expected}]` 则可进基准二机械生成；否则只作业务流输入。
2. **测试点是否独立成表**：17/18 都假设独立表（C 做法）。若坚持轻量，可退化为
   case 上的 `testpoint_name` 字段——但会失去「分批确认」和「覆盖统计」两个能力。
3. **质量特性树**：C 用 GB/T 25000.10 八特性。我们是否引入国标树，还是用现有
   test_type 枚举 + C01-C14 足够。
4. **AI 批次并行度**：同项目并发生成的上限（C 是全局 6）。起步建议同项目串行、
   跨项目并行，防同项目 slot 冲突。
