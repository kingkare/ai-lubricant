# 20 · 执行闭环——报告·多轮策略·复盘·上线·生产运营

> 2026-10-13。承接 17（分层流水线）、18（用例生成方法）、19（版本生命周期）。
> 用户继续补充 19 号文之后的下游段：**用例编写→用例评审→用例执行**，执行要出**报告**
> （用例执行情况含自动/人工 + bug 情况 + bug 修复 + bug 周期 + 上线标准判定）；
> 可能**多轮测试**（冒烟/回归/全量）→ 需要**测试策略**；**复盘**（预期时间/卡点/是否达标/遗留）；
> 状态改**上线**（合并主分支）→ **生产环境管理**（日常巡检/用户反馈收集/复现bug/故障排查/根因/类型分析）。
>
> 外部依据（2026-10-13 两路专项调研，一手源码/官方大纲）：
> **Z=禅道 ZenTao 源码**（`easysoft/zentaopms` main @d5bfff8c，逐行读过 `module/bug/*`、
> `module/testtask/*`、`module/release/*`、`module/build/*`、`db/zentao.sql`）；
> **I=ISTQB CTFL 4.0.1 官方大纲 + 术语库 API**（进入/退出准则 §5.1.3、测试完成报告 §5.3.2、
> 复盘 §2.1.6、测试级别/类型 §2.2、度量 §5.3.1）；辅以 IEEE 829/ISO 29119、Xray 回归、
> Martin Fowler TIA、Atlassian 复盘、tutorialspoint 测试词典。
> 调研报告：`$CLAUDE_JOB_DIR/tmp/{zentao-bug-research,test-strategy-research}.md`。

---

## 1. 核心结论先行

**19 号文把「用例生成」放进了版本状态机，但状态机的后半段（执行→报告→复盘→上线→生产）
当时是空的。本文把它填满，并且确立一条原则：**

> **每一轮测试是一个有状态、有策略、有准出判据的对象；报告不是"结果汇总"，是"上线决策依据"。**

三个被漏掉的实体：

| 漏掉的实体 | 现状（已核代码） | 本文补法 |
|---|---|---|
| **测试策略** | 无此实体；`TestTask` 只有标签选例，无"这轮测什么/为什么/什么算过" | `mc_test_strategy`：轮次编排 + 进入/退出准则（I） |
| **执行轮次** | `TestRun` 无 `round_seq`/`round_kind`（冒烟/回归/全量）；`TestTask` 无轮次概念 | 策略驱动的多轮 run（Z 的 testtask 形态） |
| **报告结论** | `snapshot` 只有 overview/timing/cases/bugs/requirements，**无结论、无上线判定、无缺陷周期** | 报告加 `verdict` + `release_check` + `bug_cycle` + `round` |

**bug 域是本文最大的一块**——现状 6 态但缺禅道那套成熟机制（见 §4）。

---

## 2. 用例编写 → 用例评审 → 用例执行（状态接力）

### 2.1 三段状态接力（补全 19 号文的 case 段）

19 号文 §2.2 的状态机到 `case_confirmed` 为止。本文接上**执行段**：

```
用例编写  ──▶ 用例评审 ──▶ 用例执行 ──▶ 报告
（designer）   （case.review）  （run）      （report.generate）
   │              │              │
   ▼              ▼              ▼
draft      pending_confirmation  active      ──执行──▶ 结果
   └─ 19号文已定义 case.status ─────────────┘
```

**用例状态机（`CASE_STATUSES` 已存在，补语义）**：
`draft → pending_confirmation → active → deprecated → archived`
- `draft`：AI/人生成，未评审
- `pending_confirmation`：AI 自评完，待人确认（19 号文三段确认的第三段）
- `active`：可被执行选中
- 执行失败 → 不一定回 draft（可能只是环境问题），走 §4 的 bug 流

### 2.2 用例执行的两种执行者（用户点名"可能自动化也可能人为执行"）

**关键：用例有 `execution_mode`，报告要区分自动/人工，且人工执行也要留痕。**

| 执行方式 | `execution_mode` | 结果来源 | 报告呈现 |
|---|---|---|---|
| **自动化** | `automated` | 引擎执行场景 → `run_scenario` | pass/fail/error + 证据 |
| **人工** | `manual` | 人填执行结果（用例级） | 人工结论 + 备注/截图 |
| **半自动** | `hybrid` | 脚本跑 + 人工判 | 混合 |

现状断点：`TestCase.automation_status` 有 `automated`，但**没有 `manual` 执行的结果落点**——
`TestRunScenario` 是场景级（自动化），人工执行没有对应的结果表。

**补**：人工执行结果复用 `TestRunScenario`，`exec_mode='manual'`，`status` 由人填，
`evidence` 由人上传。**报告统一读 `run_scenario`，不区分来源**——自动/人工只是 `exec_mode` 字段。

### 2.3 新增方法

| 方法 | kind | 说明 |
|---|---|---|
| `case.review` | execute | 用例评审（18 号文五维评分卡）→ 落 change_record |
| `case.execute_manual` | write | 人工执行结果录入（用例级） |
| `run.round` | read | 按轮次看执行情况（本轮 vs 历史） |

---

## 3. 测试策略——多轮测试的编排（治「无策略实体」）

### 3.1 现状断点

`TestTask`（02 号文）是"带侧重的一次启动"——有标签选例、有用途，但**没有**：
- 这一版要**跑几轮**、每轮什么类型
- 每轮的**进入准则/退出准则**（什么条件才能开跑、什么条件算这轮过了）
- **为什么这么选**（改动面广度 → 要不要全量）

**用户原话**："这个可能也是用例去确认或者选的，比如这一次的改动是什么样子的，
他设计的面是否比较广，是否需要多轮全量测试"——**策略是从改动面推导出来的，不是拍脑袋。**

### 3.2 测试策略实体（`mc_test_strategy`）

> **术语定位（ISTQB 三层）**：`test policy`（组织级原则）→ `test strategy`（组织级"怎么做"）→
> `test plan`（项目/版本级"这次怎么做"）。**我们这张表落在第三层**——版本级的 test plan：
> 一次版本"怎么测 + 完成判据 + 多轮编排"。命名沿用 `strategy` 但语义是版本级 plan
> （ISTQB 的 entry/exit criteria、deliverables、schedule 都在 plan 里）。若将来要多项目
> 复用组织级策略，再加一层 `mc_test_test_policy`（本次不做）。

```sql
CREATE TABLE IF NOT EXISTS mc_test_strategy (
    id           UUID PRIMARY KEY,
    code         VARCHAR(32) NOT NULL,   -- STRAT-xxxx（沿用 _next_code 约定）
    project_id   UUID NOT NULL,
    version_id   UUID NOT NULL,          -- → mc_test_project_version（一版一策略）
    title        VARCHAR(256) NOT NULL,
    strategy_type VARCHAR(32),           -- ISTQB 七型（见下）
    change_scope JSONB DEFAULT '{}',     -- 改动面：{modules, transports, anchors, req_types, breadth}
    test_levels  JSONB DEFAULT '[]',     -- [component|component_integration|system|system_integration|acceptance]
    test_types   JSONB DEFAULT '[]',     -- [functional|non_functional|black_box|white_box]
    techniques   JSONB DEFAULT '[]',     -- 测试技术（等价类/边界值/判定表/状态迁移…）
    risk_profile JSONB DEFAULT '{}',     -- 风险登记册：{likelihood, impact, level}
    risk_level   VARCHAR(16),            -- low|medium|high（决定轮次）
    rounds       JSONB DEFAULT '[]',     -- 轮次编排（见下）——核心
    full_regression_policy JSONB DEFAULT '{}',  -- 要不要全量的可配阈值（§3.4）
    entry_criteria JSONB DEFAULT '[]',   -- 全局进入准则（I 5.1.3）
    exit_criteria  JSONB DEFAULT '[]',   -- 全局退出准则（I 5.1.3）
    suspension_resumption JSONB DEFAULT '{}',  -- 暂停/恢复准则（I 829 §8）
    metrics      JSONB DEFAULT '[]',     -- 要收集的度量（I 5.3.1 七类）
    basis        VARCHAR(16) DEFAULT 'to_confirm',  -- fact|assumption|to_confirm（AI 提案）
    status       VARCHAR(32) DEFAULT 'draft',        -- draft|confirmed|executing|done
    created_by   UUID,
    created_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, version_id),
    UNIQUE (project_id, code)
);
```

**`strategy_type` 枚举**（ISTQB 七型 + reactive，可配）：
`analytical / model_based / methodical / process_compliant / standard_compliant /
regression_averse / reactive / consultative`。

**`rounds` 结构（多轮编排）**：

```json
[
  {"seq": 1, "kind": "smoke",       "selector": {"stage": ["smoke"]},
   "entry": ["build_available", "env_ready"], "exit": ["smoke_all_pass"], "on_fail": "abort"},
  {"seq": 2, "kind": "confirmation", "selector": {"bug_cases": true},
   "entry": ["smoke_passed"], "exit": ["execution_rate_100"]},
  {"seq": 3, "kind": "regression",  "selector": {"changed_impact": true, "history_high_risk": true},
   "entry": ["smoke_passed"], "exit": ["regression_all_pass"]},
  {"seq": 4, "kind": "full",        "selector": {"all_cases": true},
   "entry": ["regression_all_pass"],
   "exit": [{"kind": "pass_rate_gte", "value": 95}, "open_p0_p1_zero"], "required": false}
]
```

### 3.3 轮次类型（`ROUND_TYPES`）

> **重要修正（禅道源码实证）**：禅道的"冒烟/回归/全量"**不是测试单类型**——
> 禅道测试单 `type` 是**测试种类**（`integrate/system/acceptance/performance/safety`），
> 而"冒烟/回归"是**用例的 `stage` 字段**（`smoke/bvt/feature/intergrate/system/unittest`）。
> **即：轮次类型本质是"选用例的哪个 stage 子集"，不是给运行加一个类型标签。**
> 我们的设计：`rounds[].kind` 是编排概念，落地时**通过 `selector` 选用例的 stage/标签**——
> 与禅道一致（用例带 stage，轮次选 stage 子集）。

| 轮次 kind | 选例范围 | 何时必须 | 说明 |
|---|---|---|---|
| `smoke` 冒烟 | `stage=smoke` 或 `smoke` 标签子集 | **每版必跑**（第一轮） | 判定构建可否进入详细测试（I 定义） |
| `functional` 功能 | 新功能/变更功能用例 | 有新功能 | 测新功能 |
| `confirmation` 确认 | 修复关联用例（19号文 §3.3 的 reproduced） | 有 bug 修复 | 确认缺陷修复（I: confirmation testing） |
| `regression` 回归 | **受影响用例**（TIA 圈定，见 §3.4） | 有改动即跑 | 改动的涟漪验证 |
| `full` 全量 | 全部 active | **重大改动/核心链路/发版前** | complete regression |
| `targeted` 专项 | 指定模块/需求 | 针对性验证 | — |
| `exploratory` 探索 | 无预设，人驱动 | 补充 | — |

**用例 `stage` 字段**（补 `TestCase` 加列，对齐禅道）：`smoke/bvt/feature/regression/system/unittest`。
轮次通过 `selector.stage` 选用例——**这与现有 `scenario_tags`/`case_filter` 选例机制一致**（02 号文）。

### 3.4 策略从哪来（AI 提案 + 人确认，不拍脑袋）

**AI 生成策略的输入**（都是确定性事实，不是让 AI 猜）：
1. **改动面**（19 号文 `diff_versions` / **TIA 变更→用例映射**）：变更 commit → 变更文件 → 影响模块/anchor
2. **需求类型**（19 号文 `req_type`）：`fix` 类 → 回归为主；`tech` 重构 → 行为等价验证
3. **历史缺陷密度**：该模块历史 bug 多 → 提升轮次
4. **核心链路标记**：触及核心链路 → 必须全量

**回归选例依据（TIA / Xray 准则）**：高风险区 + 高频功能 + 近期改动 + 历史缺陷。
**TIA（Test Impact Analysis）**（Google Blaze / Microsoft 实践）：变更→受影响用例映射，
只跑受本次变更影响的子集——**这正是 19 号文 `diff_versions` 圈定回归用例的正名**。

**默认决策表**（`design/strategy_rules.py`，代码常量——**是默认值，不是硬编码**）：

| 改动面广度 | 触及核心链路 | 历史缺陷密度 | 建议轮次 |
|---|---|---|---|
| 窄（≤3 模块） | 否 | 低 | smoke + functional + selective-regression |
| 中 | 否 | 中 | smoke + functional + regression |
| 宽（≥10 模块） | 任意 | 任意 | + **full** |
| 任意 | **是** | 任意 | + **full**（强制） |
| fix 类版本 | — | — | smoke + **confirmation** + regression（bug 关联用例必含） |

> **重要限定（调研实证）**：业界**没有**"改动 X 行就必须全量"的统一量化公式——
> ISTQB / Xray / Fowler 都是"impact analysis 给候选集 + 风险等级决定深度/广度"的
> 定性+风险驱动。故**上述阈值一律做成可配置策略参数**（`full_regression_policy`），
> 不做死公式。这是"这版要不要全量"的诚实答案：**由可配的风险阈值 + 变更影响面算，
> 阈值由团队按项目调**，不是平台拍一个魔法数字。

```jsonc
"full_regression_policy": {
  "triggers": ["major_change", "core_path_touched", "release_candidate", "env_migration"],
  "impact_analysis": {"enabled": true, "source": "diff_versions"},  // 变更→受影响用例
  "risk_threshold": {"likelihood_x_impact_gte": 12}                 // 风险分超阈值即升全量
}
```

**这回答用户的问题**："改动面是否比较广，是否需要多轮全量"——**由可配的风险阈值 +
变更影响面算**（不是平台写死一个数字，也不是人拍脑袋）。

### 3.5 进入/退出准则（对标 ISTQB 5.1.3）

**`ENTRY_CRITERIA_KINDS` 枚举**（ISTQB 5.1.3 分类，可配）：
`env_ready`（环境就绪）/ `tools_installed`（工具可用）/ `build_available`（可测构建可用）/
`test_data_ready`（测试数据就绪且校验）/ `testware_ready`（用例/场景就绪）/
`smoke_passed`（冒烟全通过）/ `requirements_baselined`（需求已基线）/
`previous_level_exit_met`（上一级退出达成）/ `no_blocking_question`（无阻塞疑问）

**`EXIT_CRITERIA_KINDS` 枚举**（ISTQB 5.1.3，可配阈值）：
`execution_rate_100`（执行率100%）/ `pass_rate_gte_X`（通过率≥X，常见90/95/98）/
`no_open_blocker_critical`（遗留 blocker+critical=0）/ `open_p0_p1_zero`（遗留P0/P1=0）/
`open_defects_lte_n`（遗留总数≤N）/ `medium_defects_lte_10pct`（中等严重度未解决≤10%）/
`coverage_gte_X`（覆盖率≥X）/ `regression_all_pass`（回归全通过）/ `smoke_all_pass`（冒烟全通过）/
`security_no_critical`（无高危漏洞）/ `perf_targets_met`（性能达标）/ `round_done`（required轮次完成）/
`report_delivered`（报告已交付）/ `stakeholder_signoff`（干系人签字）/
`time_budget_exhausted`（**时间/预算耗尽——ISTQB 明确是有效退出准则**，风险已接受）

**退出准则的两类**（I 5.1.3）：**度量类**（覆盖率/遗留缺陷数/失败用例数）与
**二元 yes/no 类**（计划用例已执行/发现的缺陷都已上报/回归已自动化）。
上一级的 exit criteria 常成为下一级的 entry criteria。

### 3.6 新增方法

| 方法 | kind | 说明 |
|---|---|---|
| `strategy.propose` | execute | AI 按改动面生成策略提案 |
| `strategy.create` / `.get` / `.list` | write/read | 策略 CRUD |
| `strategy.confirm` | write | 人确认策略（basis: to_confirm→fact） |
| `strategy.check` | read | 校验进入/退出准则满足情况 |

---

## 4. bug 管理——对齐禅道（本文最大一块）

### 4.1 现状断点 vs 禅道

| 维度 | 我们现状 | 禅道（源码实证） | 差距 |
|---|---|---|---|
| **状态** | `open/triaged/fixed/verified/closed/rejected`（6态） | `active/resolved/closed`（3态）+ `confirmed` 标志 | 我们状态更细，但**缺 reopen 路径** |
| **解决方案** | 只有 `resolution_note`（文本） | `resolutionList` 10 值枚举 | **缺枚举** |
| **bug 类型** | 无 | `typeList` 10 值 | **缺** |
| **确认标志** | 无 | `confirmed`（0未确认/1已确认） | **缺** |
| **重开计数** | 无 | `activatedCount`（累计激活次数） | **缺** |
| **构建归档** | 无 | `openedBuild`/`resolvedBuild`（提测构建双向） | **缺** |
| **重复bug** | 无 | `duplicateBug` | **缺** |
| **周期** | 无 deadline | `deadline` + `longlife`（默认7天） | **缺** |
| **状态方法** | 只有 create/get/list/root_cause/triage | confirm/assignTo/resolve/close/activate | **缺 5 个动作** |

### 4.2 禅道 bug 状态机（源码实证——**只有 3 态，无"已验证"态**）

> **修正**：禅道 bug 只有 `active`/`resolved`/`closed` **三态**。**没有独立的"已验证"状态**——
> "已解决但未验证"就是 `status=resolved`（列表入口 `toclosed`/待关闭）。确认位 `confirmed`(0/1)
> **是正交标志，不属于状态机**。

```
                    ┌──────────────────────────────────────┐
                    ▼                                      │
  [新建] ──▶ active ──resolve(resolution必填)──▶ resolved ──close──▶ closed
             ▲  │  (confirmed=1 是正交标志,非状态)   │              │
             │  │                                     │              │
             └──┴──────────activate(激活/reopen)───────┴──────────────┘
             仅 resolved/closed 可 activate；activatedCount+1；
             清空 resolvedDate/closedDate；assignedTo 默认=resolvedBy
```

**关键规则（`bug_control.php` / `bug_zen.php` 实证）**：
1. **resolve 必填 `resolution`**；`resolution=fixed` 必须填 `resolvedBuild`；`resolution=duplicate` 必须填 `duplicateBug`
2. **只有非 closed 的 bug 可 resolve**（closed 不可再编辑，`bug_model.php:412`）
3. **只有 resolved/closed 可 activate**（重开，否则报 `error->cannotActivate`）→ `activatedCount + 1`
4. **closed 后 `assignedTo='closed'`**（占位，非真人）
5. bug **双向关联提测构建**（`linkBugToBuild` → 同步到 `zt_release.bugs`）
6. **权限靠 `checkFlow()` 流程引擎**（可配，非硬编码角色）
7. **需求版本闸门**：story 升版后 bug 进 `needconfirm` 列表，要求人工对齐 storyVersion

**resolution 条件必填（源码实证）**：`duplicate`→必须填 `duplicateBug`；`fixed`→必须填
`resolvedBuild`；其余 resolution **不要**解决版本。

### 4.2b 禅道缺的两点（我们补上就是超越）

| 禅道**没有** | 我们补 |
|---|---|
| **无硬性上线门槛**（只暴露 bugs/leftBugs/escapedBugs 三数，让人判断） | §5.3 的 `release_check` 规则判定（可配阈值） |
| **无现成 MTTR**（`fixedRate`/`bugLiveDays` 语言包注释掉、未落地） | §4.5 的 `bug.cycle_report` 直接算 MTTR |

**逃逸 bug（escaped defect）**——禅道有、我们漏掉、**上线质量最硬的指标**：
定义 = 上线后才发现、本应被测试拦截的缺陷（I 术语库 `escaped-defect`）。
禅道算法：product 相同 且 `openedDate >= releasedDate` 且 `openedBuild` 属于该发布。
**派生指标 `defect_leakage`（逃逸率）= 逃逸 bug 数 / 总 bug 数**——越低说明测试越有效。

### 4.3 我们照搬 + 改进

> **已落地（2026-10-16）**：本节设计已实现，终版见
> [`plans/23-defect-zentao-parity.md`](./23-defect-zentao-parity.md)（状态机 7 态、动作守卫表、
> 迁移策略）。落地差异：``steps``（重现步骤）**不新建列**（复用 ``symptom`` + ``repro.steps``）；
> 新增活动流表 ``mc_test_bug_log``；``opened_build`` 保持可选（禅道必填，但我们手动录入的 bug
> 无构建）。

**状态机**（保留我们更细的态 + 补禅道机制；**实数 7 态**——本节曾误写「8 态」）：
```python
BUG_STATUSES = (
    "open",        # 新建（Z: active）
    "confirmed",   # 已确认（Z 用正交 confirmed 标志；我们保留独立态）
    "triaged",     # 已分流（Z 无，我们的 root_cause 门）
    "resolved",    # 已解决（Z: resolved）—— 需 resolution（条件必填同 Z）
    "verified",    # 已验证（Z 无独立态——**我们的改进**：验证与关闭分离）
    "closed",      # 已关闭（Z: closed，不可编辑）
    "rejected",    # 驳回（resolution=willnotfix/bydesign 的终态）
)
# 注意：**不设独立 "reopened" 状态**（对齐禅道）——reopen 是**动作**，
# 把状态打回 open/confirmed，用 activated_count 记录重开次数（返工率信号）。
```

**加列**（`_column_exists` 探测→ALTER）：
```sql
ALTER TABLE mc_test_bug ADD COLUMN resolution    VARCHAR(32);   -- 解决方案枚举
ALTER TABLE mc_test_bug ADD COLUMN bug_type      VARCHAR(32);   -- bug 类型
ALTER TABLE mc_test_bug ADD COLUMN confirmed     BOOLEAN DEFAULT FALSE;
ALTER TABLE mc_test_bug ADD COLUMN activated_count INT DEFAULT 0;  -- 重开次数（返工率信号）
ALTER TABLE mc_test_bug ADD COLUMN opened_build  VARCHAR(128);  -- 发现于哪个构建
ALTER TABLE mc_test_bug ADD COLUMN resolved_build VARCHAR(128); -- 解决于哪个构建
ALTER TABLE mc_test_bug ADD COLUMN duplicate_bug_id UUID;       -- 重复 bug
ALTER TABLE mc_test_bug ADD COLUMN deadline      TIMESTAMPTZ;   -- 处理期限
ALTER TABLE mc_test_bug ADD COLUMN resolved_at   TIMESTAMPTZ;   -- 解决时刻（周期计算）
ALTER TABLE mc_test_bug ADD COLUMN verified_at   TIMESTAMPTZ;   -- 验证时刻
ALTER TABLE mc_test_bug ADD COLUMN found_in      VARCHAR(32);   -- 发现阶段：test|production|feedback
ALTER TABLE mc_test_bug ADD COLUMN escaped       BOOLEAN DEFAULT FALSE;  -- 逃逸 bug（上线后才发现）
```

**枚举常量**（照搬禅道 `buglang.php`）：
```python
BUG_RESOLUTIONS = ("fixed", "bydesign", "duplicate", "external",
                   "notrepro", "postponed", "willnotfix", "tostory")
# fixed 已解决 / bydesign 设计如此 / duplicate 重复 / external 外部原因
# notrepro 无法重现 / postponed 延期 / willnotfix 不予解决 / tostory 转需求

BUG_TYPES = ("codeerror", "config", "install", "security", "performance",
             "standard", "automation", "designdefect", "codeimprovement", "others")
# 代码错误/配置相关/安装部署/安全相关/性能问题/标准规范/测试脚本/设计缺陷/代码改进/其他
# 注：禅道已废弃 interface/designchange/newfeature/trackthings（discardedTypes），不引入。
```

**severity/priority**：禅道 severity/pri 均 1-4（默认 3）。我们现有 `BUG_SEVERITIES`
是 blocker/critical/major/minor/trivial（5 级）——**保留我们的**（更细），不改。

### 4.4 bug 状态流转方法（补 6 个动作，对齐禅道）

| 方法 | kind | 对应禅道 | gate |
|---|---|---|---|
| `bug.confirm` | write | confirm | 仅 `status=open AND confirmed=false`（Z 守卫） |
| `bug.assign` | write | assignTo | 指派 assignee |
| `bug.resolve` | write | resolve | **resolution 必填**；仅非 closed；`fixed`→`resolved_build` 必填；`duplicate`→`duplicate_bug_id` 必填 |
| `bug.verify` | write | （禅道靠 close） | 验证通过 → verified；不通过 → 回 open |
| `bug.close` | write | close | status=resolved/verified → closed；`assigned_to='closed'` |
| `bug.reopen` | write | activate | **仅 resolved/closed**（Z 守卫）；`activated_count+1`；清 `resolved_at`；回 open |
| `bug.transition` | write | — | 统一状态转换入口（校验 gate，集中守卫表） |

**动作-前置状态守卫集中成表**（对标禅道 `isClickable`）——所有转换的前置条件
（from_status / required_fields / 副作用）放一张常量表，`bug.transition` 统一校验，
**不散落在各方法里**。这是禅道 `isClickable` 守卫表的照搬。

### 4.5 bug 周期管理（用户点名）

**周期度量**（`bug.cycle_report`，读）：
- **MTTR 平均修复时长**：`avg(resolved_at - created_at)`（**禅道没落地这个，我们补**）
- **平均验证时长**：`avg(verified_at - resolved_at)`
- **总生命周期**：`closed_at - created_at`
- **重开率**：`count(activated_count > 0) / total`（返工率信号）
- **超期 bug**：`deadline < now() AND status NOT IN (closed,rejected)`（对标 Z `longlife` 默认 7 天）
- **缺陷密度 defect_density**：缺陷数 / 工作产品单位大小（模块/千行）
- **缺陷检出率 DDP**：本阶段发现数 / (本阶段 + 后续所有手段发现数)
- **逃逸率 defect_leakage**：逃逸 bug 数 / 总 bug 数（§4.2b）
- **按维度分布**：按 severity/type/resolution/status/module/build 统计（对标 Z `report->charts`）

**禅道报表清单**（照搬，`buglang.php:296-311`）：按构建/模块/天/人/严重度/解决方案/状态/
激活次数/优先级/类型/指派统计——**这些直接变成 `bug.cycle_report` 的输出维度**。

**ISTQB 度量七类**（I 5.3.1，报告字段）：进度度量、产品质量度量（可用性/响应时间/MTTF）、
缺陷度量（found/fixed、density、DDP）、风险度量（剩余风险等级）、覆盖度量、成本度量。

### 4.6 bug 与版本/构建/用例的关联（补 19 号文 §3.3）

- `opened_build`/`resolved_build`：bug 挂在**提测构建**上（对标 Z build）
- `mc_test_bug_case`（19 号文已建）：bug↔用例（reproduced/regression/related）
- `version_id`（已有列）：bug 归属版本
- `found_in`：发现阶段（测试期 vs 生产期）——**生产期 bug 走 §7 的生产运营流**

---

## 5. 报告——从"结果汇总"升级为"上线决策依据"

### 5.1 现状断点

`TestReport.snapshot` 有 `overview/timing/cases/bugs/requirements`，但**没有**：
- **结论**（这次测试到底行不行）
- **上线判定**（能否满足上线标准）
- **缺陷周期**（bug 修了多久）
- **轮次信息**（这是第几轮、什么类型）
- **多轮汇总**（一版跑了好几轮，看总账）

### 5.2 报告扩展结构（`snapshot` 加四块）

```json
{
  "overview": { ... },              // 已有：执行情况（total/pass/fail/pass_rate）
  "execution_breakdown": {          // 新增：自动/人工拆分（用户点名）
    "automated": {"total": 100, "pass": 95},
    "manual":    {"total": 28,  "pass": 26}
  },
  "round": {"seq": 2, "kind": "regression", "strategy_id": "..."},   // 新增：轮次
  "bugs": {
    "found_this_round": [...],      // 本轮发现
    "fixed": [...],                 // 已修复
    "open": [...],                  // 遗留
    "by_severity": {"blocker":0, "critical":0, "major":2, "minor":5, "trivial":1},
    "cycle": {                      // 新增：缺陷周期（§4.5）
      "mttr_hours": 18.5,           // 平均修复时长
      "avg_verify_hours": 4.2,
      "reopen_rate": 0.05,          // 重开率
      "overdue": 1,
      "defect_density": 0.8,
      "defect_detection_pct": 0.92,
      "defect_leakage": 0.08        // 逃逸率
    }
  },
  "release_check": {                // 新增：上线标准判定（用户点名"能否满足上线标准"）
    "verdict": "conditional",       // go|no_go|conditional
    "criteria": [
      {"name": "用例执行率", "target": "100%", "actual": "98%", "pass": false},
      {"name": "通过率",     "target": "≥95%", "actual": "97%", "pass": true},
      {"name": "遗留P0/P1",  "target": "0",    "actual": "2",  "pass": false},
      {"name": "全量轮完成",  "target": "是",   "actual": "否", "pass": false}
    ],
    "blockers": ["2个P1未修复", "全量轮未跑"]
  },
  "lessons_learned": [...],         // 新增：经验教训（I 5.3.2）
  "signoff": [...]                  // 新增：干系人签字（I）
}
```

### 5.3 上线判定逻辑（`report.evaluate_release`，确定性）

**这是"能否满足上线标准"的确定答案**——纯规则计算，不交给 AI。
**15 项 release gate 清单**（I 5.1.3 + 5.3.2 汇总，阈值全部可配）：

| 门槛项 | 判据 |
|---|---|
| 用例执行率 | 计划用例 100% 执行 |
| 通过率 | ≥ 阈值（常见 90/95/98%） |
| 遗留严重缺陷 | blocker + critical = 0 |
| 遗留高优先级 | P0/P1 = 0 |
| 遗留缺陷总量 | ≤ 计划允许值，其余已 defer 且有理由 |
| 中等严重度未解决 | ≤ 10%（业界经验值） |
| 回归 | 全通过，无新增回归缺陷 |
| 冒烟 | 全通过 |
| 覆盖率 | 需求/代码覆盖 ≥ 目标 |
| 非功能 | 性能/扩展性/可用性/安全 达标 |
| 安全 | 无高危漏洞 |
| 合规 | 符合行业标准/法规 |
| 报告 | Test Completion Report 已交付 |
| 签字 | 干系人复核批准（sign-off） |
| 风险接受 | 未达项已书面接受 go-live 风险 |

```
输入：strategy.exit_criteria + 本版所有轮次结果 + bug 统计
输出：verdict ∈ {go, no_go, conditional}
  go          = 所有 required 退出准则满足
  conditional = 仅剩非阻断项（如全量轮未跑但改动面窄）+ 干系人已接受风险
  no_go       = 有阻断项（遗留P0/P1 / 执行率<100% / 冒烟失败 / 高危安全漏洞）
```

### 5.4 多轮汇总报告（一版多轮 → 一份总报告）

一版可能跑 smoke→regression→full 三轮。**需要一份"版本测试总结报告"**（对标 I 5.3.2
Test Completion Report）聚合所有轮次：
- 各轮次执行情况趋势
- 版本内 bug 总账（发现/修复/遗留）
- 最终上线判定
- 复盘输入（§6）

**新增方法**：`report.version_summary`（按 version_id 聚合所有 run 生成总报告）。

### 5.5 报告方法补全

| 方法 | kind | 说明 |
|---|---|---|
| `report.generate` | write | 已有：单 run 报告 |
| `report.version_summary` | write | 新增：版本多轮汇总（I 5.3.2） |
| `report.evaluate_release` | read | 新增：上线判定（§5.3） |
| `report.bug_cycle` | read | 新增：缺陷周期报表（§4.5） |

---

## 6. 复盘（用户点名：预期时间/卡点/是否达标/遗留）

### 6.1 现状断点

**全仓零复盘实体**。ISTQB 2.1.6 明确复盘要素：什么成功（保留）/ 什么没成功（改进）/
如何纳入改进；产出进测试完成报告。

### 6.2 复盘实体（`mc_test_retrospective`）

```sql
CREATE TABLE IF NOT EXISTS mc_test_retrospective (
    id           UUID PRIMARY KEY,
    project_id   UUID NOT NULL,
    version_id   UUID NOT NULL,
    planned      JSONB DEFAULT '{}',   -- 预期：{start, end, est_cases, est_hours, est_bugs}
    actual       JSONB DEFAULT '{}',   -- 实际：{start, end, done_cases, hours, found_bugs}
    variance     JSONB DEFAULT '{}',   -- 偏差：时间/用例数/缺陷数（机器算）
    blockers     JSONB DEFAULT '[]',   -- 卡点：[{desc, impact_hours, cause}]（I: impediments）
    leftover     JSONB DEFAULT '[]',   -- 遗留问题（未修复缺陷/未缓解风险/未达 exit criteria）
    successes    JSONB DEFAULT '[]',   -- 做得好（I: what was successful）
    improvements JSONB DEFAULT '[]',   -- 待改进（I: what was not successful）
    actions      JSONB DEFAULT '[]',   -- 改进项 + 责任人 + 期限（I: how to incorporate）
    status       VARCHAR(16) DEFAULT 'draft',  -- draft|done
    created_by   UUID,
    created_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, version_id)
);
```

**复盘维度（对标 Atlassian 五步法 + ISTQB lessons learned）**：
- **预期 vs 实际**（`variance`，机器算）：计划测试时长 vs 实际、计划缺陷数 vs 实际发现、
  计划覆盖率 vs 实际、计划用例数 vs 实际（I: Planned vs Actual Efforts）
- **卡点/阻塞**（`blockers`）：环境不可用、数据缺失、构建不稳定、依赖延期
- **遗留**（`leftover`）：未修复缺陷、未缓解风险、未完成的 exit criteria
- **改进项**（`actions`）：流程/工具/自动化/数据 改进 + owner + 期限
- 常用模板：4Ls（Liked/Learned/Lacked/Longed for）、Start-Stop-Continue、Mad-Sad-Glad

**`variance` 是机器算的**（不让人填）：预期 vs 实际的时间/用例数/缺陷数偏差。
`blockers`/`successes`/`improvements` 是 AI 从执行过程**提案** + 人确认。

### 6.3 复盘方法

| 方法 | kind | 说明 |
|---|---|---|
| `retro.propose` | execute | AI 按执行数据生成复盘提案（卡点/遗留/改进） |
| `retro.create` / `.get` / `.update` | write | 复盘 CRUD |
| `retro.finalize` | write | 定稿（改进项沉淀 → craft_asset） |

---

## 7. 上线 → 生产环境管理（用户点名）

### 7.1 上线（版本 → released，合并主分支）

19 号文状态机的 `case_confirmed → released`，本文补**上线动作**：

**上线 = 合并到主分支**（用户原话"上线之后意味着他合并到了主分支"）：
- `version.transition("released")` 的 gate：**上线判定 = go**（§5.3，conditional 需干系人签字）
- 上线后记录：`released_at` + `released_build` + 主分支 commit
- **生产环境从此可被巡检任务引用**
- **上线后开启逃逸 bug 追踪**：此后发现的、本应被测试拦截的 bug 标 `escaped=true`
  （§4.2b）——**这是衡量这次测试质量的最终指标**（禅道的 escapedBugs 机制）

**加列**：`mc_test_project_version` + `released_at` + `released_build` + `merged_commit`。

### 7.2 生产环境（19 号文 §6.6 已定义 `is_production`，本文补运营）

**生产环境管理 = 六个动作**（用户逐一点名）：

| 动作 | 实体/方法 | 说明 |
|---|---|---|
| **日常巡检** | `patrol.run`（新） | 定时对 prod 跑只读 smoke + 关键链路探针 |
| **用户反馈收集** | `feedback.intake`（新） | 用户反馈 → `mc_test_feedback` → 可转 bug |
| **复现 bug** | `feedback.reproduce` | 反馈→复现→建 bug（`found_in=production`） |
| **故障排查** | `incident.triage`（新） | 故障 → 证据收集 → 定位（复用 debugger 角色） |
| **找出 bug 原因** | `bug.root_cause`（已有） | 根因分析（证据链门） |
| **bug 类型分析** | `bug.cycle_report`（§4.5） | 按类型/模块/趋势分析 |

### 7.3 用户反馈实体（`mc_test_feedback`）

```sql
CREATE TABLE IF NOT EXISTS mc_test_feedback (
    id           UUID PRIMARY KEY,
    project_id   UUID NOT NULL,
    version_id   UUID,
    source       VARCHAR(32) NOT NULL,   -- user|monitor|support|ops
    channel      VARCHAR(64),            -- 来源渠道（客服/工单/社区）
    content      TEXT NOT NULL,          -- 反馈原文
    contact      JSONB DEFAULT '{}',     -- 用户联系信息（脱敏）
    severity     VARCHAR(16),            -- 用户感知严重度
    client_type  VARCHAR(16),            -- 端（19号文 §6）
    status       VARCHAR(32) DEFAULT 'new',  -- new|triaged|reproduced|bug_created|rejected|closed
    bug_id       UUID,                   -- 复现后建的 bug
    reproduce    JSONB DEFAULT '{}',     -- 复现结果
    created_at   TIMESTAMPTZ DEFAULT now()
);
```

**反馈流**：`new → triaged → reproduced → bug_created`（复现成功建 bug）或 `rejected`（无法复现）。
复现成功的 bug `found_in=production`，**必须补回归用例**（19 号文 §3.3 的 bug↔用例）。

### 7.4 日常巡检（`patrol`）

- **只读**（19 号文 §6.6：生产环境写操作走审批）
- 定时任务（复用 `TestTask.schedule`）
- 巡检项：关键接口探针 + 只读 smoke + 数据一致性抽查（C09）
- 失败 → 自动建 bug（`found_in=production`）+ 通知

### 7.5 故障排查（`incident`）

生产故障（非普通 bug）→ 排查流：
```
故障发现(巡检/反馈/监控) → 证据收集(日志/trace/DB状态) 
  → 定位(复用 debugger 角色 + 知识图谱 anchor) → 根因(证据链门) 
  → 修复 → 回归 → 复盘(§6)
```

**复用**：`bug.triage`（失败分流）、`bug.root_cause`（根因+证据链）、`TestBugPattern`（模式沉淀）。
**新增**：`incident.create` / `incident.timeline`（故障时间线）。

### 7.6 生产运营方法清单

| 方法 | kind | 说明 |
|---|---|---|
| `patrol.run` | execute | 巡检执行（只读） |
| `patrol.create` / `.list` | write/read | 巡检任务 |
| `feedback.intake` | write | 反馈录入 |
| `feedback.triage` | write | 反馈分流 |
| `feedback.reproduce` | execute | 复现 → 建 bug |
| `incident.create` / `.timeline` | write/read | 故障排查 |
| `bug.cycle_report` | read | bug 类型/周期分析 |

---

## 8. 数据模型改动清单（汇总）

### 8.1 加列（`_column_exists` 探测→ALTER）

| 表 | 新列 | 用途 |
|---|---|---|
| `mc_test_bug` | resolution, bug_type, confirmed, activated_count, opened_build, resolved_build, duplicate_bug_id, deadline, resolved_at, verified_at, found_in, escaped | 禅道对齐（§4.3） |
| `mc_test_project_version` | released_at, released_build, merged_commit | 上线（§7.1） |
| `mc_test_run` | round_seq, round_kind, strategy_id | 轮次（§3） |
| `mc_test_case` | execution_mode, stage | 自动/人工 + 轮次选例（§2.2/§3.3） |

### 8.2 新表（`CREATE TABLE IF NOT EXISTS`）

| 表 | 用途 | 出处 |
|---|---|---|
| `mc_test_strategy` | 测试策略（轮次+进入退出准则） | §3.2 |
| `mc_test_retrospective` | 复盘 | §6.2 |
| `mc_test_feedback` | 用户反馈 | §7.3 |
| `mc_test_bug_case` | 缺陷↔用例 | 19 号文 §3.3 |
| `mc_test_case_version` | 用例↔版本继承 | 19 号文 §3.4 |

### 8.3 枚举扩展（纯加字符串，零迁移）

- `BUG_STATUSES`：6 → 7 态（补 confirmed/verified、``fixed``→``resolved``；**不设 reopened**——reopen 是动作；落地见 23 号文）
- `BUG_RESOLUTIONS`（新，8 值）、`BUG_TYPES`（新，10 值）——照搬禅道
- `ROUND_TYPES`（新）、`ENTRY_CRITERIA_KINDS`（新）、`EXIT_CRITERIA_KINDS`（新）
- `STRATEGY_TYPES`（新，ISTQB 七型）、`TEST_LEVELS`（新，5 级）、`TEST_TYPES`（新，4 类）
- `CASE_STAGES`（新，smoke/bvt/feature/regression/system/unittest）
- `FEEDBACK_STATUSES`（新）

### 8.4 新增代码模块

| 模块 | 内容 |
|---|---|
| `design/strategy_rules.py` | 改动面→轮次的默认决策表 + `full_regression_policy`（§3.4） |
| `design/release_rules.py` | 上线判定规则（§5.3，15 项 gate） |
| `reports/cycle.py` | 缺陷周期计算（MTTR/DDP/leakage，§4.5） |
| `production/`（新域） | 巡检/反馈/故障（§7） |

---

## 9. 与 17/18/19 号文的关系

- **17** = 分层架构（L0-L4）+ 六块改进 —— 架构层
- **18** = 用例生成方法（基准/标准/分类/评审/确认/规模化）—— 内容维度
- **19** = 版本生命周期（状态机/血缘/需求分类/技术外在/多端）—— 流程·血缘·部署维度
- **20（本文）** = 执行闭环（报告/策略/复盘/上线/生产运营）—— **执行·决策·运营维度**

**四者合起来是完整生命周期**：
```
19: 建版本→需求评审→技术评审→用例生成→用例评审→确认
                                                    ↓
20:                          用例执行→报告→多轮策略→复盘→上线→生产运营 ↺
                                                    （反馈回流为 bug/需求）
```

**19 号文状态机 `case_confirmed` 之后的段，本文接续**：`case_confirmed → executing → reported
→ retro_done → released → operating`。

---

## 10. 分期（叠加在 17/18/19 之上）

### P2 · 执行段（与 19 号文 P2 并行）
用例 execution_mode + 人工执行录入 + `case.review` + 报告扩展（execution_breakdown/round）+
`report.evaluate_release`。**验收：自动+人工用例都能执行并出报告；报告给出上线判定。**

### P2.5 · 策略 + 多轮
`mc_test_strategy` + `strategy_rules.py` + 进入/退出准则 + 多轮 run（round_seq/kind）+
`report.version_summary`。**验收：给定改动面，AI 提案轮次；冒烟→回归→全量可编排执行；
版本总报告聚合多轮。**

### P3 · bug 禅道对齐 + 复盘
bug 加列（resolution/type/build/周期/escaped）+ 6 个动作方法（含 reopen）+ `bug.cycle_report` +
`mc_test_retrospective` + `retro.propose`。**验收：bug 走 open→confirmed→resolved→verified→
closed 全链，reopen 计数正确；周期报表出 MTTR/DDP/逃逸率；复盘出偏差分析。**

### P3.5 · 生产运营
`mc_test_feedback` + `patrol` + `incident` + 生产只读门 + 逃逸 bug 追踪。**验收：巡检定时跑只读；
用户反馈可复现转 bug；故障有时间线+根因；上线后逃逸 bug 可统计。**

---

## 11. 待确认（需用户拍板）

1. **bug 状态机：照搬禅道 3 态还是保留我们 7 态？**（**已拍板**：保留更细的 7 态，
   落地见 23 号文）我倾向**保留我们的更细态**
   （open/confirmed/triaged/resolved/verified/closed/rejected），但**补禅道的
   resolution/confirmed/activated_count/build/escaped 字段**——状态细、字段对齐。
   （注意：**不设 reopened 状态**——reopen 是动作，对齐禅道用 `activated_count` 记次数。）
2. **提测构建（build）实体要不要建？** 禅道 bug 挂在 build 上。我们现在只有 version。
   我倾向：**version 即 build**（一版一构建），暂不单独建 build 表——除非一个版本多次提测。
3. **测试策略是"一版一策略"还是"一轮一策略"？** 本文设计 `mc_test_strategy` 一版一策略
   （内含多轮 rounds）。若一版内策略会中途变，需拆成一轮一策略。我倾向一版一策略。
4. **人工执行结果的落点**：复用 `run_scenario`（本文方案）还是新建 `mc_test_manual_result`？
   我倾向复用（报告统一读，不区分来源）。
5. **上线判定谁拍板**：纯机器（§5.3 规则）还是机器给 verdict + 人最终确认？我倾向
   **机器给 verdict，人确认**（conditional 必须人拍 + 签字）。
6. **生产运营是新域还是并入 defects？** 我倾向**新域 `production/`**（巡检/反馈/故障与缺陷
   生命周期不同——缺陷是已确认的问题，反馈/故障是待收敛的信号）。
7. **上线门槛的默认阈值**：§5.3 的 15 项 gate 阈值（通过率 95%？遗留 P0/P1=0？）取业界默认
   还是让用户配？我倾向**给业界默认值 + 全可配**（调研实证业界无统一公式）。

---

## 12. 出处对照（哪个机制从哪来）

| 机制 | 出处 |
|---|---|
| bug 3 态机 + confirmed 正交位 + isClickable 守卫表 + activatedCount | 禅道 Z（`buglang.php`/`bug_control.php`/`bug_zen.php`） |
| resolution 8 值 + 条件必填（fixed→resolvedBuild / duplicate→duplicateBug） | 禅道 Z（`buglang.php:280`/`bug_model.php:331`） |
| bug_type 10 值、双 build 字段、linkBugToBuild、closed 不可编辑 | 禅道 Z（`buglang.php:259`/`bug_model.php:412`） |
| 逃逸 bug（escapedBugs）+ leftBugs 三数 | 禅道 Z（`release` 模块）+ ISTQB `escaped-defect` |
| 测试单 4 态 + 关联 build + 用例 stage 区分冒烟/回归 | 禅道 Z（`module_testtask_lang`/`testcase` 模块） |
| test plan 要素（entry/exit criteria/levels/types/techniques/metrics） | ISTQB CTFL §5.1.1 + IEEE 829/29119 |
| entry/exit criteria 枚举 + "时间/预算耗尽"是有效退出准则 | ISTQB CTFL §5.1.3 |
| 测试级别 5 级 / 测试类型 4 类 / strategy 七型 | ISTQB CTFL §2.2 + 术语库 |
| 冒烟/回归/全量定义 + 回归 4 类型 + 选例准则 | ISTQB §2.2.3 + Xray |
| TIA（变更→受影响用例映射） | Martin Fowler / Google Blaze / Microsoft |
| Test Completion Report 七要素 + 度量七类 | ISTQB CTFL §5.3.1/§5.3.2 |
| 复盘（went well/didn't/improve + planned vs actual） | ISTQB §2.1.6 + Atlassian 五步法 |
| **业界无统一"改动量→全量"公式**（阈值须可配） | 调研实证（ISTQB/Xray/Fowler 均为定性+风险驱动） |
| 缺陷周期 MTTR/缺陷密度/DDP/逃逸率 | ISTQB §5.3.1 + tutorialspoint 缺陷生命周期 |

> **未查到（如实标注）**：① IEEE 829 原文 PDF（付费，仅取到标准页+章节大纲）；
> ② 业界"改动量→是否全量"的统一量化公式（业界普遍定性+风险驱动，无固定阈值）；
> ③ 禅道 `found`(来源) 字段现行枚举、`fixedRate`/`bugLiveDays` 的当前实现（语言包注释掉）。
