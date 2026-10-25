# 19 · 版本生命周期驱动用例生成——版本关联·需求分类·技术落地·多端兼容

> 2026-10-13。承接 17（分层流水线）、18（用例生成方法）。用户补充指出 18 号文
> 的三个盲区：**① 没考虑版本之间的关联**（"这个版本的用例是用来优化某功能的，意味着
> 它可能是修复动作，修复动作会存在关联用例"）；**② 没考虑需求分类、技术实现落地的
> 具体外在是什么**；**③ 没考虑多端兼容怎么做、用例怎么设计**。并给出完整流程：
>
> 建版本 → 需求评审（导入需求 → AI 评审 → 人工验证 → 生成版本用例）→ 技术评审
> （导入技术方案 → AI 评审 → AI 优化用例 → AI 用例评审 → 使用者确认），全程 AI 不确定处提问。
>
> 本文把这三点补进 18 号文的生成方法之上——**用例生成不是孤立动作，是版本生命周期的一环**。

---

## 1. 核心结论先行

**18 号文回答的是「一条用例怎么生成」；本文回答「一个版本的用例在什么状态下、以什么为
输入、被谁确认而生成」——版本是流程的载体，状态是流程的驱动。**

三条被漏掉的轴：

| 漏掉的轴 | 现状（已核代码） | 本文补法 |
|---|---|---|
| **版本间关联** | `TestProjectVersion` 无 `parent_version_id`/`version_kind`；`VERSION_STATUSES` 只有 `draft/graph_built` | 版本血缘 + 版本 diff + 用例跨版本继承/回归圈定 |
| **需求分类** | `TestRequirement` 有 module/priority/origin，**无 `req_type`** | 五类需求 × 各自的生成基准与覆盖侧重 |
| **技术落地外在** | 无「技术方案」实体，无评审对象定义 | 技术方案的**四类外在**（接口/数据/交互/依赖）+ 需求↔技术对账 |
| **多端兼容** | 全仓无 `client_type`；`test_type=compatibility` 是类型不是端 | 端=执行维度（矩阵）非用例维度（乘法）+ 端能力差异表 |

**一句话**：18 号文的三基准（接口契约/业务规则/业务流程）是**内容维度**；
本文补的是**流程维度**（状态机）、**血缘维度**（版本间）、**部署维度**（多端）。
三者正交，合起来才是完整的生成输入。

---

## 2. 版本状态机——状态驱动流程（治「版本只有 2 态」）

### 2.1 现状断点

`projects/models.py:244`：`VERSION_STATUSES = ("draft", "graph_built")`。
`draft`=配了仓库未建图谱，`graph_built`=已生成图谱。**用户的整条评审流程无状态可挂**，
所以流程只能靠人记，无法"跟随状态做流程转换"。

### 2.2 目标状态机（逐字对应用户描述的流程）

```
draft ──导入需求──▶ req_review ──AI评审+人工验证──▶ req_confirmed
                                                       │
                                              生成版本用例(18号文L1-L3)
                                                       ▼
                    tech_review ◀──导入技术方案──┐
                         │                       │
                    AI技术评审+需求↔技术对账       │
                         ▼                       │
                   tech_confirmed ──AI优化用例──▶ case_review
                                                       │
                                            AI用例评审 + 使用者确认
                                                       ▼
                                               case_confirmed ──执行──▶ released
                                                                          │
                                                                      archived
```

`VERSION_STATUSES` 扩展（**纯加字符串，零迁移**，`_validate_enum` 白名单自动生效）：

```python
VERSION_STATUSES = (
    "draft",           # 创建，未导入需求
    "req_review",      # 需求评审中（导入需求 + AI 评审）
    "req_confirmed",   # 需求人工验证通过
    "tech_review",     # 技术评审中（导入技术方案 + AI 评审）
    "tech_confirmed",  # 技术评审通过
    "case_review",     # 用例评审中（AI 优化 + AI 自评）
    "case_confirmed",  # 用例使用者确认通过
    "released",        # 已发布
    "archived",
    # legacy：graph_built 保留（存量数据兼容，图谱构建回写用）
    "graph_built",
)
```

### 2.3 状态转换表（`projects/version_fsm.py`，代码常量）

每个转换声明 **准入条件（gate）/ 产出物 / 执行者 / 是否需人工**——这就是"跟随状态做流程转换"：

| from → to | 触发 | gate（准入条件，不满足拒绝转换） | 产出物 | 执行者 |
|---|---|---|---|---|
| draft → req_review | 导入需求 | ≥1 条 `TestRequirement` | requirement 行 | 人 |
| req_review → req_confirmed | AI 评审完 + 人工确认 | **无 open 的 blocking question** + 评审 issue 全部 `handled` | 需求评审报告 | AI 评 + 人确认 |
| req_confirmed → tech_review | 导入技术方案 | ≥1 份技术方案文档 | knowledge_item(kind=tech) | 人 |
| tech_review → tech_confirmed | AI 技术评审完 | 需求↔技术对账无缺口 | 技术评审报告 + 影响清单 | AI 评 + 人确认 |
| tech_confirmed → case_review | 触发优化 | 用例已生成/优化 | 用例集 | AI |
| case_review → case_confirmed | AI 自评 + 使用者确认 | `case.review` 分数 ≥85 或人工放行 | 用例评审报告 | AI 评 + 人确认 |
| case_confirmed → released | 执行通过 | 关键用例 active 且执行 pass | run 结果 | 系统 |
| released → archived | 归档 | — | — | 人 |

**gate 是硬门**：`version.transition(to)` 校验 gate，不满足抛错并回报缺什么。
这把"流程"从人的记忆变成代码的断言。

### 2.4 新增方法

| 方法 | kind | 说明 |
|---|---|---|
| `version.transition` | write | 状态转换（校验 gate） |
| `version.fsm` | read | 返回当前状态 + 合法后继 + 每个后继的 gate 满足情况 |
| `version.review` | execute | 触发当前阶段的 AI 评审（需求/技术/用例三态各一套 prompt） |

---

## 3. 版本间关联——血缘·修复·用例继承（治「版本之间没有关联」）

> **⚠ 本节血缘模型已被 21 号文修正**：本节的 `parent_version_id` 单亲链 +
> `diff_versions(parent,head)` 默认了"分析基线=上一个开发版本"，在**选择性发布**下是错的。
> 正确基线是**「当前版本 + 生产版本 + 已发布版本」**（不是历史版本、不是 parent）。
> 见 `21-production-baseline-and-requirement-classification.md`：`parent_version_id`→
> `base_version_id`（仅开发分叉点）+ `release_status` + `mc_test_deployment`（生产指针）+
> `diff_against_production`。本节的 `version_kind`/`req_type`/`mc_test_case_version` 仍有效。

### 3.1 现状断点

- `TestProjectVersion` **无 `parent_version_id`** → 版本之间是孤立列表，不知道谁基于谁。
- **无 `version_kind`** → "这个版本是优化某功能/修复动作"无字段承载（用户原话）。
- `TestBug.case_refs` 是 **JSONB 字符串数组**（defects/models.py:198）→ **修复动作的关联用例物理上不能反查/不能 join**。
- `diff_releases`（projects/service.py:651）基于 **deprecated** 的 `TestProjectRelease`，且 diff 的是**分支名指针**（不可复现），不是新版本实体的 `resolved_commit`。

### 3.2 版本血缘（`TestProjectVersion` 加列）

```sql
ALTER TABLE mc_test_project_version ADD COLUMN parent_version_id UUID;  -- 基于哪个版本
ALTER TABLE mc_test_project_version ADD COLUMN version_kind VARCHAR(16); -- 见下
ALTER TABLE mc_test_project_version ADD COLUMN baseline_commit VARCHAR(64); -- 从 repos[].resolved_commit 派生
```

```python
VERSION_KINDS = ("feature", "fix", "optimize", "hotfix", "refactor")
# feature  新功能版本
# fix      缺陷修复版本（"修复动作"——重点，见 3.3）
# optimize 功能优化版本（"用来优化某一个功能"——用户原话）
# hotfix   线上紧急修复
# refactor 技术重构（行为不变）
```

**版本 diff（`diff_versions(base_id, head_id)`，新写，替代 deprecated 的 diff_releases）**：
基于两版本的 `repos[].resolved_commit` 做 commit diff → 变更文件 → 知识图谱 anchor
（`KnowledgeAnchor.content_hash` 变化即"该 anchor 所在代码变了"）。
**这比 diff_releases 的"分支名比较"可复现**——commit 是确定性的。

### 3.3 修复动作的关联用例（用户重点）

**修复流程的完整链路**：

```
线上 Bug (TestBug, status=open)
   │  ① 复现用例：case_refs 现在指向的用例（缺陷是怎么被发现的）
   │  ② 受影响用例：diff_versions 算出被改代码 anchor → 反查覆盖它的用例
   │  ③ 模块回归：该 bug 的 module 下的 smoke/regression 标签用例
   ▼
修复版本 (version_kind=fix, parent_version_id=发现 bug 的版本)
   │
   ▼
回归用例集 = ①∪②∪③  → 打包成 release 前的回归任务
```

**要补的洞**：`TestBug.case_refs` JSON 数组 → 加关联表：

```sql
CREATE TABLE IF NOT EXISTS mc_test_bug_case (
    id         UUID PRIMARY KEY,
    bug_id     UUID NOT NULL,     -- → mc_test_bug
    case_id    UUID NOT NULL,     -- → mc_test_case
    relation   VARCHAR(16) NOT NULL,  -- reproduced|regression|related
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (bug_id, case_id, relation)
);
```

- `relation=reproduced`：发现该 bug 的用例（修复后必须转 pass）
- `relation=regression`：该 bug 的回归用例（防复发）
- `relation=related`：相关用例（同模块，可选跑）

`TestBugPattern.regression_template`（defects/models.py:261，**已预留未用**）就是"这类 bug
的回归模板"——`hit_count` 命中的模式 → 自动生成 regression 关联。**这个字段等到了它的用途**。

### 3.4 用例跨版本继承（治「每版从零生成」）

**核心：用例在版本间是"继承 + 增量"，不是重新生成。**

```sql
CREATE TABLE IF NOT EXISTS mc_test_case_version (
    id         UUID PRIMARY KEY,
    case_id    UUID NOT NULL,
    version_id UUID NOT NULL,
    scope      VARCHAR(16) NOT NULL,   -- inherited|new|modified|regressed|deprecated
    basis      VARCHAR(32),            -- 为什么进入本版本（diff anchor / 需求变更 / bug 修复）
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (case_id, version_id)
);
```

新版本用例生成流程（改 18 号文 §6 的编排）：
1. `diff_versions(parent, head)` → 变更 anchor 集
2. 变更 anchor → 关联需求/用例 → 标 `scope=modified`（要改）/ `regressed`（要回归）
3. 未受影响的上版 active 用例 → `scope=inherited`（直接继承，**0 token**）
4. 只有 `modified` + 新需求的用例才进 AI 生成批 → **token 只花在真正变化的部分**

**这就是"版本之间的关联"的真正价值**：版本 diff 是增量生成的输入，让第 N 版的生成量
= 变更量，不是全量。

---

## 4. 需求分类——分类驱动生成策略（治「需求没分类」）

### 4.1 现状断点

`TestRequirement`（assets/models.py:510）有 `module/priority/origin`，**无 `req_type`**。
`knowledge_item.kind`（rationale|concept|rule|...）是**知识类型**不是**需求类型**。

### 4.2 需求分类（`TestRequirement` 加列）

```sql
ALTER TABLE mc_test_requirement ADD COLUMN req_type VARCHAR(16) DEFAULT 'functional';
ALTER TABLE mc_test_requirement ADD COLUMN parent_id UUID;        -- 需求血缘（子需求）
ALTER TABLE mc_test_requirement ADD COLUMN related_bug_id UUID;   -- fix 类需求关联的 bug
```

```python
REQ_TYPES = ("functional", "optimize", "fix", "tech", "compliance")
```

### 4.3 分类驱动的生成策略（这是用户问的"需求分类怎么影响生成"）

| req_type | 生成基准侧重（18号文三基准） | 覆盖轴侧重 | 评审侧重 |
|---|---|---|---|
| **functional** 功能 | 业务流程 + 接口契约 | C01正常/C02异常/C04权限 | 业务覆盖完整性 |
| **optimize** 优化 | 接口契约 + 性能 | C05并发/C14安全/C02异常 | 性能基线不退化 |
| **fix** 修复 | **不生成新用例为主** → 圈定回归集（3.3） | C02异常/C11幂等/C09数据一致性 | 回归充分性（bug 必转 pass） |
| **tech** 重构 | 接口契约（**行为等价验证**） | C09数据一致性/C10上下游 | 行为不变（改前改后同结果） |
| **compliance** 合规 | 业务规则（判定表） | C14安全/C07审计 | 合规项全覆盖 |

**关键：`fix` 类需求不触发业务流 AI 生成**——它是"回归圈定"而非"新用例生成"。
这一条直接治用户说的"修复动作会存在关联用例"：修复的产物是**回归用例集**，不是新用例。

### 4.4 需求分类从哪来

- 导入需求时 AI 预分类（analyst 链路，`req_type` 进 output）+ 人工可改
- `origin=from_doc` 的需求从文档章节标题推断（"修复""优化"等词）
- `related_bug_id` 由缺陷转需求时自动填

---

## 5. 技术评审——技术实现落地的"外在"（治「技术落地具体外在是什么」）

### 5.1 技术方案的"外在"= 四类可对账物

用户问"技术的实现落地具体外在是什么"。答案：技术方案不是一篇文档，它的**外在**是
四类**可 diff、可对账**的东西：

| 外在 | 实体 | 与什么对账 | 触发生成 |
|---|---|---|---|
| **接口外在** | 新增/变更的 API | `transport` diff（OpenAPI/路由） | 接口契约基准（18号文基准一） |
| **数据外在** | 新增/变更的表/字段 | `KnowledgeAnchor(kind=db_table)` diff | 数据一致性用例（C09） |
| **交互外在** | 多端适配方案 | 端能力差异表（§6） | 端特定用例 |
| **依赖外在** | 新增第三方/中间件 | 依赖清单 diff | 集成/上下游用例（C10） |

**技术方案评审 = 拿这四类外在，与需求对账 + 与现有用例对账。**

### 5.2 AI 技术评审产出（`tech.review`）

```
输入：技术方案文档 + 需求条目集 + 现有用例集 + 代码 anchor
输出：
  ① 需求↔技术 覆盖缺口：需求说要，技术方案没说怎么做的条目 → question.raise
  ② 技术变更↔用例 影响清单：四类外在的 diff → 受影响用例（改/删/增）
  ③ 技术风险点 → 测试点提案（basis=assumption）
```

**②就是用户说的"AI 去优化他的用例"的输入**——AI 不是凭空优化，是**技术 diff 驱动的定向优化**。

### 5.3 需求↔技术对账矩阵

```
              技术方案条目
            │ 接口  │ 数据  │ 交互  │ 依赖  │
需求条目 ────┼──────┼──────┼──────┼──────┤
  REQ-001    │  ✓   │  ✓   │  —   │  —   │
  REQ-002    │  ✓   │  —   │  ✓   │  —   │   ← 需求要交互适配，技术方案没提 → 缺口
  REQ-003    │  —   │  —   │  —   │  —   │   ← 需求完全没被技术方案覆盖 → 严重缺口
```

矩阵空格（需求条目 × 无对应技术外在）= 对账缺口 → question。**这是"验证是否按需求实现"的确定性版本**，
也是知识图谱在本文里的正当用途之一（与 17 号文 §5.2 一致：图谱用于对账，不用于生成）。

### 5.4 技术方案实体

技术方案 = `KnowledgeItem(kind=tech)`（knowledge_item.kind 已支持 `tech`，
`KNOWLEDGE_SOURCE_KINDS=("product","tech","test")`）。**不新建表**——复用语料体系，
章节切分（18号文 §6.1）与 content_hash 增量照旧。

---

## 6. 多端兼容——端是执行维度，不是用例维度（治「多端怎么做兼容」）

### 6.1 现状断点

全仓无 `client_type`。`CASE_TEST_TYPES` 有 `compatibility`（assets/models.py:388），
但那是**测试类型**（兼容性测试），**不是"哪个端"**。用户问"涉及多端怎么设计用例"——
现在没有任何字段能表达"这条用例要在 iOS 跑还是 Android 跑"。

### 6.2 核心原则：端是矩阵的列，不是用例的倍数

**错误做法（爆炸）**：一个功能 × N 端 = 生成 N 份用例 → 用例数 × N，维护地狱。
**正确做法（矩阵）**：用例设计**一次**，执行时决定**在哪些端跑** → 用例数不变，执行矩阵 × N。

```
                用例
            │ TC-001 │ TC-002 │ TC-003 │
端   ───────┼────────┼────────┼────────┤
  web       │   ✓    │   ✓    │   —    │
  ios       │   ✓    │   —    │   ✓    │
  android   │   ✓    │   —    │   ✓    │
  miniprog  │   ✓    │   —    │   —    │
```

### 6.3 三级用例设计（端维度）

| 层级 | 端相关性 | 用例数 | 说明 |
|---|---|---|---|
| **端无关层** | 业务逻辑，全端一致 | **1 份** | 绝大多数功能用例。`client_scope=[]`（空=全端） |
| **端适配层** | 交互/能力差异 | 按差异表生成 | 由**端能力差异表**（§6.4）驱动。`client_scope=["ios"]` |
| **兼容层** | 跨端一致性 | 少量 | 同账号 A 端操作 B 端可见（C09 数据一致性） |

### 6.4 端能力差异表（`design/client_capabilities.py`，代码常量，类比 18号文的变换表）

| 能力维度 | web | ios | android | miniprogram |
|---|---|---|---|---|
| 本地存储 | ✓ | ✓ | ✓ | ✗ |
| 系统返回键 | ✗ | ✓(手势) | ✓ | ✗ |
| 深链/URL | ✓ | ✓ | ✓ | △ |
| 推送 | ✗ | ✓ | ✓ | ✓ |
| 文件上传 | ✓ | ✓ | ✓ | △ |
| 支付通道 | △ | △ | △ | △ |

**每个 ✗/△ 格 = 一条端特定用例**（如"小程序无本地存储 → 断网重进数据不丢"）。
**这就是"多端兼容用例怎么设计"的确定答案**：不是让 AI 想，是查能力差异表。

### 6.5 数据模型（端建模）

```sql
-- 环境加端（一个版本 × 端 = 一个执行环境）
ALTER TABLE mc_test_environment ADD COLUMN client_type VARCHAR(16);
ALTER TABLE mc_test_environment ADD COLUMN tier VARCHAR(16) DEFAULT 'test';  -- dev|test|staging|prod
ALTER TABLE mc_test_environment ADD COLUMN is_production BOOLEAN DEFAULT FALSE;  -- 真实生产环境（§6.6）
-- 用例加端范围
ALTER TABLE mc_test_case ADD COLUMN client_scope JSONB DEFAULT '[]';  -- []=全端
```

```python
CLIENT_TYPES = ("web", "ios", "android", "miniprogram", "h5", "api", "desktop")
ENV_TIERS = ("dev", "test", "staging", "prod")
```

### 6.6 生产环境（用户点名"生产环境是指现在生产的环境"）

- `tier='prod'` + `is_production=true` 的环境 = **真实生产**。
- **生产环境只跑只读/巡检类用例**：`is_production` 环境下，写操作类用例（C01 正常流里
  有副作用的）默认拒绝执行 → 走审批门（复用现有审批框架）。
- 生产验证的场景：版本 `released` 后 → 生产巡检（smoke 子集 + 只读校验）。
- 现 `TestEnvironment.code` 有 `prod_sim`（生产模拟）——本文区分 **`prod_sim`（模拟，可写）**
  vs **`prod`（真实，只读）**。

### 6.7 执行矩阵与端环境映射

- 一条用例的 `client_scope` × 项目的 `CLIENT_TYPES` 交集 = 该用例要跑的端
- 每端 → 一个 `TestEnvironment(client_type=X, tier=Y)` → 一个执行环境
- `run.execute` 按 (scenario, env) 展开执行矩阵（18 号文执行闭环的扩展）

---

## 7. AI 提问机制贯穿全流程（用户强调："生成过程中 AI 不确定也会提问"）

### 7.1 现状：已是一等公民，但只接了 scenario

`TestQuestion`（quality/models.py:145）：`origin`（product_uncertainty|design_doubt）、
`raised_by`、`content`、`context`、`status(open|resolved)`、`resolution`、
**`blocking_refs`**（被阻塞的下游实体）。`question.raise` 方法已注册。

**但只被 `scenario.generate` 校验门用**（17 号文断点 1）。本文把它接进版本流程的每个 gate。

### 7.2 提问点（贯穿状态机）

| 阶段 | 提问 origin | 例子 | 阻塞对象 |
|---|---|---|---|
| 需求评审 | product_uncertainty | "需求说'快速响应'，具体阈值是多少？" | req_confirmed 转换 |
| 技术评审 | design_doubt | "技术方案说改 A 表，但没提数据迁移，存量数据怎么办？" | tech_confirmed 转换 |
| 用例生成 | product_uncertainty | "接口没定义非法枚举值的行为" | 该用例落库 |
| 用例评审 | design_doubt | "这条用例的预期结果与需求验收标准不符" | case_confirmed 转换 |

### 7.3 question 是 gate 的阻塞项（关键机制）

**状态转换 gate 包含"无 open 的 blocking question"**：
- 阶段内 AI 提问 → `blocking_refs` 指向待产出实体 → 该实体挂起（不落库）
- 人 resolve（`resolution=doc|user_confirm|debug`）→ 解除阻塞 → 阶段可进下一态
- **这治"AI 认知模糊"**：AI 不猜，挂起；人确认；再生成。**不是 AI 猜错了后面返工。**

### 7.4 提问的批量呈现（前端）

按阶段分组：需求评审页 / 技术评审页 / 用例评审页各有一个"待确认疑问"面板，
resolve 后自动解除对应阻塞、推进状态机。复用 18 号文 §8 的三段确认 UI。

---

## 8. 数据模型改动清单（汇总）

### 8.1 加列（走 `_column_exists` 探测→ALTER，projects/service.py:85 模式）

| 表 | 新列 | 用途 |
|---|---|---|
| `mc_test_project_version` | `parent_version_id`, `version_kind`, `baseline_commit` | 版本血缘/类型 |
| `mc_test_requirement` | `req_type`, `parent_id`, `related_bug_id` | 需求分类/血缘 |
| `mc_test_case` | `client_scope`, `testpoint_id`（18号文）, `slot_key`（18号文） | 端范围/测试点/幂等 |
| `mc_test_environment` | `client_type`, `tier`, `is_production` | 端/环境层级/生产标记 |

### 8.2 新表（`CREATE TABLE IF NOT EXISTS`）

| 表 | 用途 | 出处 |
|---|---|---|
| `mc_test_bug_case` | 缺陷↔用例关联（治 case_refs JSON 数组） | 本文 §3.3 |
| `mc_test_case_version` | 用例↔版本（继承/新增/修改/回归） | 本文 §3.4 |
| `mc_test_transport_param` | 接口参数 schema | 18 号文 §3.1 |
| `mc_test_testpoint` | 测试点 | 18 号文 §3.2 |
| `mc_knowledge_chunk` | 文档切分单元 | 18 号文 §6.1 |

### 8.3 枚举扩展（纯加字符串，零迁移）

- `VERSION_STATUSES`：2 → 9 态（§2.2）
- `REQ_TYPES`（新）、`VERSION_KINDS`（新）、`CLIENT_TYPES`（新）、`ENV_TIERS`（新）
- `CASE_TEST_TYPES` 已含 `compatibility`，无需改

### 8.4 新增代码模块

| 模块 | 内容 |
|---|---|
| `projects/version_fsm.py` | 状态机转换表（§2.3） |
| `design/client_capabilities.py` | 端能力差异表（§6.4） |
| `design/transform_table.py` / `coverage_axes.py` / `token_budget.py` | 18 号文已有 |

---

## 9. 与 17/18 号文的关系

- **17 号文** = 分层架构（L0-L4）+ 六块改进。**18 号文** = 用例生成方法（基准/标准/分类/评审/确认/规模化）。
- **本文（19）= 把生成放进版本生命周期**：
  - 18 号文的生成方法 → 本文 §2.3 状态机的 `req_confirmed→case_review` 段
  - 18 号文的"三段确认" → 本文 §2 状态机的三个 gate（需求/技术/用例）
  - 18 号文的"三基准" → 本文 §4.3 按 `req_type` 选哪个基准
  - 18 号文的"分批" → 本文 §3.4 版本 diff 决定"哪些批要跑"（增量）
- 三者正交：**18=内容维度，19=流程/血缘/部署维度**。实现时 18 的 P0 先行（枚举骨架），
  本文的版本状态机可与 18 的 P1 并行。

---

## 10. 分期（叠加在 17/18 的 P0/P1/P2 之上）

### P1.5 · 版本生命周期（与 18 的 P1 并行）
`VERSION_STATUSES` 扩 9 态 + `version_fsm.py` + `version.transition`/`.fsm` 方法 +
question 接入 gate。**验收：建版本→导入需求→AI评审→人工确认→导入技术方案→
AI评审→用例评审→确认 全链路状态可推进，每步 gate 拦住缺料。**

### P2 · 版本血缘 + 需求分类 + 修复关联
版本血缘列 + `diff_versions` + `mc_test_bug_case` + `mc_test_case_version` +
`req_type` + 分类驱动策略。**验收：fix 类版本生成"回归用例集"而非新用例；
新版本 80%+ 用例标 inherited（0 token）；bug 关联用例可反查。**

### P2.5 · 多端兼容
`client_type`/`tier`/`is_production`/`client_scope` + `client_capabilities.py` +
执行矩阵展开 + 生产环境只读门。**验收：一个功能 4 端 → 1 份端无关用例 + 端差异表
生成的 N 条端特定用例；执行矩阵按端展开；生产环境拒跑写操作。**

---

## 11. 待确认（需用户拍板）

1. **状态粒度**：9 态是否合适？是否需要"评审被驳回"的回退态（`req_review→draft`）？
   我倾向：驳回=不转换 + 记录评审 issue，不加回退态（少一个状态少一堆分支）。
2. **版本血缘是单亲还是多亲**：`parent_version_id` 单亲（简单）；多亲（如 hotfix 同时基于
   两个版本）需要关联表。我倾向单亲起步（99% 场景够用），hotfix 用 `baseline_commit` 兜。
3. **用例继承是"复制行"还是"关联表"**：本文用 `mc_test_case_version` 关联表（用例本体一份，
   版本是关联）——省存储、改一次全版本生效。若某版本要独立改用例，则复制行。我倾向关联表。
4. **生产环境写操作门**：是"拒绝执行"还是"需审批"？我倾向审批（复用现有审批框架，
   `is_production` + 写操作 → 审批卡）。
5. **端能力差异表的维护**：代码常量（跟变换表一样）还是入库可配？我倾向代码常量起步
   （跟 18 号文变换表一致），差异表稳定、不常改。
6. **技术方案载体**：复用 `KnowledgeItem(kind=tech)`（本文假设）还是独立 `mc_test_tech_plan` 表？
   我倾向复用——已有切分/版本/content_hash 全套，独立表要重建一遍。
