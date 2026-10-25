# 22 · 测试策略——一轮测试的执行分配层

> 2026-10-14。承接 20 号文（执行闭环，§3 提出了 ISTQB 版本级 test plan 形态的
> `mc_test_strategy` 草案）。**用户定版推翻了 20 号文 §3.2 的镀金设计**：
> 不做进入/退出准则、不做 rounds 多轮编排、不做风险登记册——只做用户点名的
> **执行分配层**：一轮一策略、类型四选一、勾选用例 + 分配人员 + 测试时间 + 测试结果。
>
> **2026-10-15 增补（用户定版）**：策略加**测试人员/开发人员**团队名单；
> **项目与版本加「测试/开发/产品」三类角色成员**（项目级 + 版本级，版本传值即覆盖、
> 否则回退项目级——见 §7）。新建策略弹框加大（`max-w-3xl`）并预填版本生效角色。

---

## 1. 核心结论先行

> **每一轮测试是一个可勾选、可分配、可录结果的对象；bug 归属 = 测试版本 × 测试策略两个维度。**

| 用户点名 | 落法 |
|---|---|
| 策略分四类：冒烟/全量/回归/性能 | `mc_test_strategy.strategy_type`，枚举 `smoke / full / regression / performance` |
| 勾选测试用例 | `mc_test_strategy_case`（策略↔用例，`unique(strategy_id, case_id)`） |
| 人工测试还是自动化测试，都要分配给人员 | `strategy_case.exec_mode`（`manual/automated/hybrid`）+ `assignee_id`——自动化那人是**责任人**，人工那人是**执行人**，两者都要能分配 |
| 要有测试结果 | `strategy_case.result`（`pending/pass/fail/blocked/skipped`）+ `note` + `executed_at`/`executed_by`——人工录入与自动化回写**同一落点** |
| 测试策略有测试时间 | `strategy.start_time` / `end_time`（计划时间） |
| bug 是测试版本 × 测试策略两维度下的 bug | `mc_test_bug.strategy_id` 加列（`version_id` 已有）；`bug.list` 可按两维度分别收窄 |

与 20 号文的关系：20 §3.2 的 `entry_criteria` / `rounds` / `change_scope` /
`full_regression_policy` **刻意不建**（表加列易，语义镀金难）。日后要轮次编排再扩本表，
不另立实体。命名沿用 `strategy`，语义是版本级的一轮执行计划。

## 2. 数据模型（`testing_platform/strategy/models.py`）

```
mc_test_strategy            一轮测试
├─ code                     STRAT-xxxx（project 内 max+1，同 assets._next_code）
├─ project_id               → mc_test_project
├─ version_id               → mc_test_project_version（可空）
├─ name / strategy_type / description
├─ status                   draft → pending → executing → done（archived 可达）
├─ start_time / end_time    计划测试时间
├─ actual_end_time          实际完成时间（用户定版 2026-10-16；报告延期分析基准）
├─ delay_analysis           延期原因分析（人工填，报告页随填随存）
├─ owner_id                 本轮负责人
├─ testers / developers     本轮测试 / 开发人员（平台用户 id 列表）
└─ version                 变更门版本（strategy.update 每改 +1）

mc_test_strategy_case       勾选 + 分配 + 结果（本轮执行的账本）
├─ strategy_id + case_id    unique 对（一个用例在一轮里一行）
├─ assignee_id              分配给谁（平台用户）
├─ exec_mode                manual | automated | hybrid
├─ result                   pending | pass | fail | blocked | skipped
├─ note                     结果备注（人工执行留痕）
└─ executed_at / executed_by  结果录入时刻/人（服务端打点）
```

**关键决策：不开「执行记录」表**。勾选 = 建行、改派 = 更新本行、录结果 = 更新本行。
历史在 `mc_test_run`（重跑一轮不改历史）；本表是**本轮当前态**。
`result != pending` 算已执行——`execution_rate = executed/total`、
`pass_rate = pass/total`（`service.summarize`）。

## 3. 服务层（`testing_platform/strategy/service.py`）

| 函数 | 说明 |
|---|---|
| `create_strategy` | 建策略（可带 `case_ids` 同步勾选）；新建 status 限 `draft/pending` |
| `update_strategy` | 变更门：`change_reason` 必填，落 `mc_test_change_record`（entity_type=`strategy`），`version+1` |
| `delete_strategy` | 真删：主行 + 勾选行；历史 run/bug 不动 |
| `set_strategy_cases` | **全量差异同步**：新勾选建行（可带默认 assignee/exec_mode）、取消删行、**已有行保留结果与分配** |
| `assign_case` | 改派/改执行方式（勾选行必须已存在——先勾选再分配） |
| `remove_case` | 取消勾选单条（连同分配与结果） |
| `record_case_result` | 录结果（人工/自动化同落点）；`executed_at` 服务端打点 |
| `get_strategy` / `list_strategies` | 读：策略 + 勾选用例 + 汇总；list 可按 version/type/status 收窄 |
| `summarize` | 汇总：总数/各结果计数/执行率/通过率（空集合不除零） |

变更门接线：`CHANGE_ENTITY_TYPES`（assets/models.py）追加 `"strategy"`——
策略的每次更新同走 `mc_test_change_record`。

## 4. 能力面（`strategy.*` 10 方法）

`strategy.create / list / get / update / delete`（CRUD）+
`strategy.set_cases / assign_case / record_result / remove_case / cases`（勾选与结果）。
全部注册进 `testing_platform/surface/methods.py`（懒导入防环，同 workflow 模式）；
`strategy.create`/`update` 返回完整详情（同 `strategy.get`）——前端创建后直接进详情
不用二次取数。`bug.create` / `bug.list` 收 `strategy_id`（第二维度入参/过滤）。

## 5. 前端（`/testing/strategies`）

**信息架构（用户定版 2026-10-16，推翻早先「详情页塞一份基本信息大表单」的形态）**：

```
/testing/strategies                    列表：筛选 + 每行四个动作
  ├─ 编辑        → 弹框改基本信息（不跳页）
  ├─ 用例详情    → /:strategyId          本轮用例：勾选 + 分配 + 结果
  ├─ Bug 详情    → /:strategyId/bugs     本轮缺陷（版本 × 策略两维度）
  └─ 报告        → /:strategyId/report   统计 + 延期分析（见 §8）
```

* **列表**（`StrategyList`）：`strategy.list`，按类型/状态/版本收窄；行显示
  code/名称/类型/状态/测试时间/测试人员/进度（执行率/通过率）；动作按钮
  **编辑 / 用例详情 / Bug 详情 / 报告** 四个；「新建策略」走同一个表单弹框。
* **基本信息只在弹框里编辑**（`StrategyFormDialog`，新建与编辑共用）：
  名称/类型/测试版本/负责人/测试人员/开发人员/计划起止/**实际完成时间**/状态/说明；
  编辑模式多一个 **变更理由**（变更门必填）→ `strategy.update`。
  弹框 `max-w-4xl` + `max-h-[95vh]`（用户要求加大加高）。新建时按当前版本预填
  生效角色（`project.version.roles`），可改。
* **三个子视图共用一个头 + 子导航**（`StrategyWorkspace`）：头只读展示
  类型/状态/负责人/测试人员，不再重复一份表单；右上「编辑」开同一个弹框。
  * `StrategyCasesPanel` 用例详情：结果汇总条（用例/自动化/已执行/通过/失败/
    阻塞/跳过/待执行）+ 勾选用例表（执行人、执行方式、结果逐行即改即存）+
    「勾选用例」弹框（搜索 + checkbox → `strategy.set_cases` 全量同步）+ 取消勾选；
  * `StrategyBugsPanel` Bug 详情：`bug.list` 按 `strategy_id` 过滤，
    状态可筛，行带 超期/重开 徽标 + 解决时长 + 解决人；顶部给 总数/P0-P1/未关闭；
  * `StrategyReportPanel` 报告：见 §8。

路由 `strategies` + `strategies/:strategyId` + `/bugs` + `/report` 共用一组件
（`useParams` 取 id、`useLocation` 判尾段分流视图）；菜单项在「测试资产」组
（`modes.testing.strategies`，cn/en 两 locale 均补键）。

## 6. 测试

`tests/test_strategy_flow.py`（7 例，sqlite::memory:，无 PG 依赖）：
四类可建 + 一版多策略 + code 自增、枚举门、勾选差异同步保留结果、
分配/结果/汇总（含空集合不除零）、变更门 + version+1、bug 两维度收窄、
能力面 10 方法接线（含 bug.create/list 收 strategy_id）。
`tests/test_project_roles.py`（5 例）：项目/版本三类角色 + 生效角色回退 +
策略团队名单 + 能力面接线。
`tests/test_strategy_report.py`（7 例）：用例段/缺陷段（P0-P1 + 三个时长 +
空值给 None 不给 0）/延时段三种判定/能力面 `strategy.report` 真库跑。
`tests/test_surface_writes.py` 的 `EXPECTED_TOTAL` 账本随每批新增方法同步
（158 → 178 策略 10 → 179 project.version.roles → 192 strategy.report，
中间含并发批次 catalog/import_job/method_module.changes/缺陷域 7 等）。

## 7. 项目/版本级角色成员（用户定版 2026-10-15）

「对项目以及版本增加测试、开发、产品」——项目级与版本级各加三类角色成员
（平台用户 id 列表，JSONB 不做 FK，前端按用户目录解析姓名）：

```
mc_test_project.testers / developers / product_managers   JSONB[]   项目级角色
mc_test_project_version.testers / developers / product_managers  JSONB[]   版本级角色
```

* **语义分工**：**策略的人是「这一轮谁测」**（`mc_test_strategy.testers/developers`，
  §5 创建弹框配）；**项目/版本的人是「这个项目谁负责哪块」**（禅道团队角色对齐）。
  不与用例级执行人（`TestStrategyCase.assignee_id`）混——那是逐例执行人。
* **生效角色**：`resolve_version_roles(project_id, version_id)` —— 版本该角色非空
  即覆盖，否则回退项目级（前端建策略时预填该版本的生效角色，省重复选人）。
* **哨兵语义**（同 `coding_project_id` 惯例）：update 传列表即覆盖、传空列表即清空、
  不传则不动（`...` 哨兵区分「没传」与「传 None」）。
* **能力面**：`project.create/update`、`project.version.create/update` 收三类角色
  字段；新增 `project.version.roles`（读生效角色）。

前端：项目弹框（`TestingProjectDialog`）+ 版本弹框（`VersionManageDialog`）各加三个
`MultiSelect`（`useCommonData().members`）；项目列表加「团队（测/开/产）」列。
新建策略弹框预填当前版本的生效测试/开发人员，可改；策略详情基本信息段亦可编辑这两组。

## 8. 策略统计报告（用户定版 2026-10-16）

「增加报告页面，点击之后显示一个统计」——报告是 **策略级一页**（不做版本汇总页），
取数 = `get_strategy(strategy_id)` + `list_bugs(project_id, strategy_id=...)`，
计算是纯函数 `testing_platform/strategy/report.py::compute_strategy_report`
（无 DB、可单测；与缺陷域 `reports/cycle.py::compute_cycle_report` 分工不重叠：
后者算 MTTR/重开率/逃逸率那套周期指标，本模块只取用户点名的几项 + 用例段 + 延时段）。

| 指标 | 口径 |
|---|---|
| 用例个数 | 本轮勾选行数 `mc_test_strategy_case` |
| 自动化用例个数 | `exec_mode == automated`（人工/混合另给，三档相加=总数） |
| Bug 数 | 该策略下缺陷数（版本 × 策略第二维度） |
| **P0/P1 Bug 数** | **`priority ∈ {1,2}`**（用户定版：按优先级字段，**不是** severity；`STRATEGY_P0_P1_PRIORITIES`） |
| **平均 Bug 解决时间** | `created_at → resolved_at` 均值，**只算已解决的那几条** |
| **最长 / 最短 Bug 解决时间** | 同上集合的 max / min；最长那条附 `slowest_bug`（code+title）便于定位 |
| 执行率 / 通过率 | 复用 `summarize`（`result != pending` 算已执行） |
| 分布 | 缺陷按 priority/severity/status；用例按 exec_mode/result |

**空值诚实**：一条已解决 bug 都没有时，三个时长指标给 `None`（前端显示「—」），
**不给 0**——0 会被读成「秒解」，是假数据。

### 8.1 延期分析

```
delay.basis =
  actual        有 actual_end_time          → 延期 = 实际完成 − 计划 end_time
  in_progress   无实际完成 且 now > end_time → 延期 = 现在 − 计划（本轮逾期未收尾）
  null          未过期 / 无计划 end_time     → delayed=false
```

* `delay_seconds` / `delay_days` 由上面口径算出；`delayed = delay_seconds > 0`。
* **`delay_analysis`（延期原因）人工填**，在报告页就地编辑（同时可填 `actual_end_time`），
  保存走 `strategy.update` 变更门（`change_reason` 由前端写「更新延期分析与实际完成时间」）。
* 前端呈现：延期时先出**红/琥珀横幅**（逾期未收尾=红，实际晚于计划=琥珀）显示
  `+X.X 天` + 计划/参考时刻 + 分析编辑区；未延期则横幅不出现，编辑区仍在（可补填）。

### 8.2 能力面

`strategy.report`（read，入参 `strategy_id`）→ 四段 `{strategy, cases, bugs, delay, generated_at}`。
`strategy.create` / `strategy.update` 的 params 收 `actual_end_time` / `delay_analysis`。
