# 25 · 测试平台 · 完整设计（从需求到报告，自包含）

> 2026-10-17 **v4.1 终版**。三轮评审链：v2→31 条缺陷（存档 findings）→v3 闭 24 剩 17→v4 闭 17 但终审再查出 10 处
> （4 个 P0 阻塞：撞现库同名表/直写通道冲突/逆转 catalog 定版/验收排期矛盾）→ **v4.1 全闭合**（对照见附录 C）。
> 终审"可动工判定"：修掉四阻塞后**可动工**，主体设计未动。
>
> **本文回答九个问题**：大架构 / 每张表结构 / 状态机 / 每环节谁做·输入输出·步骤·UI /
> 思维导图 / agent 分工 / 模型分层 / 用户规则 / 落地分期。
> 方法基底=《18个Skills》原文（18 个全落位，见第八部分）；吸收出处=`skill-library-research.md` R1-R11；
> 规则调研=`rules-research.md`。
> **用词约定**：不自造词。"回流"不说飞轮、"环节"不说域、需求分析产物不说富化。

---

# 第一部分 · 大架构

## 1.1 平台在做什么

把**需求文档**变成**测试报告**。五个环节，前者产物=后者输入：

```
需求环节 → 生成环节 → 执行环节 → 缺陷环节 → 报告环节
```

共享同一套资产（需求/测试点/用例/场景/接口/数据集/模块）、同一个 agent 底座（与日常聊天同源）、
同一套用户规则。

## 1.2 生成环节三层结构

```
        调度器（确定性代码）      ← 跑流程、切批、算预算、过机器门、推进状态、叫人
              │ 派发 / 回收
   ┌──────────┴──────────┐
 指挥中心（思考模型）   子agent群（专用角色）   ← 想和派 / 各写各的
   └──────────┬──────────┘
              ▼
        暂存区 mc_test_proposal    ← AI 唯一写入口
              ▼ 人确认（三道门）
        正式表（测试点/用例/数据集）
```

**铁律**：AI 产物先落暂存区，人确认后才进正式表。门②之前正式用例表零写入。

**现库直写通道的迁移声明（v4.1 补，终审指出的硬冲突）**：
现库已上线三条直写 `mc_test_case` 的通道——`case.generate`（L1 骨架 bulk_create 直写+Agent 经 case.create 直落）、`case.enumerate`（机械枚举）、`case.scan`（文档扫描）。本设计落地时：
- `case.generate` → **改造为 gen_run 编排器**（同步跑分析+提点产暂存、停门①），原直写路径废止
- `case.enumerate` → 明确**豁免**：它是纯确定性机械枚举（非 AI 产物），产物=接口覆盖体检数据，**不写 `mc_test_case` 改挂接口覆盖矩阵**（15 号文 Skill15 的资产维度）
- `case.scan` → **豁免**：文档导入通道（人给的文档），产物走原 import 路径带 source 标记，不算"AI 生成直写"

## 1.3 三条全局规则

1. AI 产物先暂存、确认后入库
2. 需求→测试点→用例三层树，测试点挂需求不挂模块
3. 三个人工停顿点：需求评审（门⓪）、需求确认（门①）、用例确认（门②）

---

# 第二部分 · 数据模型

## 2.0 新表〇 `mc_test_gen_run`（生成任务，全文所有 "run" 指它）

> v3 的硬伤：通篇用 run.status/run.decisions 却没给表，且与现库执行用的 `mc_test_run`
> （pending/running/done/aborted）撞名不兼容。生成是长事务多阶段，与执行 run 分开建模：

```
id / project_id / version_id
run_type        full（设计+用例）| plan（只到门⓪①产设计）| exec（从确认测试点起）
                | pre_submit_edit（提测前改草案）| incremental（提测后增量）| reopen（门②派生）
parent_run_id   reopen/派生链（门② REJECT 重开用）
scope           {requirement_ids 默认=版本全部, surfaces?}
status          状态机见 3.1
progress JSONB  3.3 的结构（前端唯一渲染源）
budget   JSONB  {tokens_used, tokens_limit, state: normal|pressure|blocking}
decisions JSONB 指挥中心决策回合流水（每条带证据等级，可回放）
config   JSONB 本次用的模型档/规程版本快照（复现）
created_by / created_at / updated_at
```

## 2.1 已有表改动

| 表 | 改动 |
|---|---|
| `mc_test_requirement` | 加列 `analysis JSONB`（环节1产物）、`review JSONB`（门⓪报告+TSDR）、`gen_status`（3.2，每需求进度=导图需求节点徽章的数据源） |
| `mc_test_case` | 加列 `surface`、`testpoint_id`、`verified_by_run`（未探索/已探索/探索失败）；`dataset_id` 已有 |
| `mc_test_bug` | 加列 `case_id`（复现步骤继承用例）；trace 不进列——执行侧走现有 evidence 表（`EVIDENCE_KINDS` 已含 trace），日志诊断按 bug.case_id→结果→evidence 取 |
| `mc_test_catalog` | **P0 不动存量四棵树**；只加 `type=module` 新分支：模块节点自己一棵树、带四类资产计数（关联表派生），四类资产与模块的关系按"资产自己的 module 列"对齐（用例/缺陷/需求/场景都已有 module 字段）。"四棵树合并成一棵"是 **P2 迁移题**，需先出对齐规则+迁移脚本+UI，且必须你重新批准（现语义是你 2026-10-14 定版的：四类无关、按 type 各一棵树，本设计不推翻） |
| 执行结果 | **不加新列**：四态是 `mc_test_run_scenario.status + failure_kind` 的全组合派生映射（见环节 8 映射表）；trace 走现有 evidence(kind=trace)，不加 trace_id |

## 2.2 新表一 `mc_test_testpoint`（测试点，中枢；v4 起它只出现在"新表"，不再混进改动清单）

```
code / project_id（v4.1 补，全表惯例）/ requirement_id（挂需求，不挂模块）
title / trigger / sample_data / verify_result / impact / evidence      ← 五字段+依据
coverage_axis  C01-C14 【数组】（一个点可多轴）
surfaces       [api, web, app, security] 数组
surfaces_decided_by   ai | user（门①人改端记 user，徽章数据源）
design_method  等价类/边界值/判定表/状态迁移/场景法/错误推测/正交
batch_tier     A 冒烟 / B 重点 / C 扩展
basis          事实 fact / 推断 assumption / 待确认 to_confirm
status         confirmed → generated
               ※v4 修正：门①确认后才建行，初始即 confirmed（不存在 candidate）；
               generated = 该点 surfaces 的每一端都有 confirmed 用例；人改端后自动重算
case_review_state  聚合态：子 case proposal 的评审态聚合（machine_pending|in_duel|split|scored|need_rework|none）
               ※v4.1 补（终审#7）：TP 节点"用例评审中"徽章的数据源
```

## 2.3 新表二 `mc_test_proposal`（暂存区，AI 唯一写入口）

```
run_id → mc_test_gen_run / kind（testpoint|case|dataset）/ payload JSONB
source_agent（哪个角色产的）/ surface / testpoint_id（case 挂父点）
evidence_class   executed|static|inferred|claimed
review_state     machine_pending|machine_rejected|in_duel|scored|need_rework
review_round     第几轮重写（棘轮比较用）
rewrite_of / version   重写版本链（新行引父 proposal，slot_key 允许带版本多行）
score / score_breakdown JSONB   五维总分+分项分（门②组头"五维迷你条"数据源）
reviews JSONB    {a:{model,opinion,verdict,abstain}, b:{...}, consensus: agree|split|inconclusive}
dup_of_case_id / dup_score / dup_verdict   查重命中+处置（并入/另立/维持）
applied_rules JSONB   规则回执 [{rule_id, followed|not_applicable|violated, why}]（未声明=违规）
question_id      defer 时关联的问题（回答后自动重新入队）
slot_key 幂等键 / status: proposed→keep|modify|reject|defer / provenance（模型ID/输入hash/skill_version）
```

## 2.4 问题队列 = **扩列复用已有 `mc_test_question`**（v4.1 修正：此表现库已上线，不新建）

现库已有：`project_id/origin/content/context/status=open|resolved/resolution=debug|doc|user_confirm/blocking_refs`，
且 `quality.service.assert_not_blocked` **已实现"open 疑问阻塞下游"**——正好是问题队列的现成地基，只补生成流程缺的列：

```
加列：requirement_id / testpoint_id（挂哪）、recommended_answer（AI 推荐答案）、
      answer（人答）、round_no（第几问，门⓪①各 10-15 预算计数）、gate（req_review|point_confirm）
resolution 枚举扩展：+risk_accepted（超预算降级出口，resolve 时清 blocking_refs——沿用现语义）
```

`analysis.register` 只存 `question_id` 引用；答完问题→挂起测试点/defer 用例自动激活（联动链见门①②）。
现库 assert_not_blocked 与门的衔接：**未答 blocking 问题 → 该需求自动阻塞在门前**，这就是现成能力，不重造。

## 2.5 新表四 `mc_test_coverage_cell`

`requirement_id/axis/surface/state(lit|gap|na)/evidence(na 必填验证依据)/testpoint_id`

## 2.6 新表五 `mc_test_rule`（用户规则，机制见第七部分）

```
project_id（全表惯例）/ content 人话 / kind hard|soft / scope global|module|requirement|surface
target_type + target_id（作用对象实体：哪个模块/需求/端——v4 补，否则"支付模块"无处挂）
enforcement active|shadow|disabled / hits / violations / created_at
```

> 其余新表（proposal/question 扩列/coverage_cell）同补 `project_id` 与时间戳（现库全表惯例，v4.1 补终审#10）。

## 2.7 数据五作用域标准（用例-场景-数据的归位规矩）

| 作用域 | 住什么 | 怎么用 |
|---|---|---|
| 环境 env | 环境地址、账号池 | `{{env.xxx}}` |
| 鉴权 auth | token/session | 引擎自动注入 |
| 局部变量 local | 跨步骤中间结果 | post_cond 写、后续读 |
| 数据集行 data | 同一用例多变体输入 | 按行迭代 |
| 全局 global | 项目共享常量 | 任务级注入 |

- 硬编码（值不在五处又写死步骤里）→ 机器门判可维护性不合格（纯代码可判）
- 数据集 schema 从测试点 sample_data+参数变体推导；rows 至少正常/边界/异常三类，缺类不收
- 同场景多用例共用一份数据集（schema 同才可共用），用例引用不复制
- 前置账号/订单走方法脚本造数（Skill12），行里存引用不存真值；脱敏：example.com、禁真实敏感数据

---

# 第三部分 · 状态机

## 3.1 生成任务 `mc_test_gen_run.status`

```
pending → analyzing → req_reviewing（内含门⓪）→ points_proposed（内含门①）
        → generating → evaluating → cases_proposed（内含门②）
        → cases_confirmed → importing → done
回边：evaluating → generating（返工：分数<70 或双评分歧回派，带 rewrite 链）
派生：门②后 REJECT 已入库用例 → 新建 gen_run(run_type=reopen, parent_run_id=原run)
      只重跑该测试点；原 run 不动
```

**run_type × 状态起止表（v4.1 补，终审#6）**：

| run_type | 起点 | 终点 |
|---|---|---|
| full | pending | done（走全链） |
| plan | pending | points_proposed 关闭（只到门①，产设计） |
| exec | generating | done（从已确认测试点起） |
| pre_submit_edit | generating | cases_proposed（改草案不重跑设计） |
| incremental | analyzing（只增量范围） | done |
| reopen | generating | done（只该测试点） |

**gen_status ↔ run.status 映射（12 对 10，多出的 3 个是需求级并行细节，run 态取最慢需求映射）**：

| gen_status | 映射 run.status |
|---|---|
| 未开始/分析中 | analyzing |
| 需求评审/已过评审 | req_reviewing |
| 提点中/门①待确认 | points_proposed |
| 已过门①/用例生成 | generating |
| 用例评审/门②待确认 | cases_proposed（评审中=evaluating） |
| 已过门②/已入库 | cases_confirmed→importing→done |

**衔接声明**：gen_run 到 done 为止（=入库）。**之后环节走已有实体**：
场景化=场景表+用例 `verified_by_run` 流转；执行=`mc_test_run`（pending/running/done/aborted，已有，
执行波次是它内部的批次属性）；缺陷=`mc_test_bug`；报告=report 实体。一条 reopen 链的完整历史
靠 `parent_run_id` + 各资产外键可回放。

## 3.2 每需求进度 `mc_test_requirement.gen_status`（需求节点徽章数据源）

```
未开始 → 分析中 → 需求评审 → 已过评审 → 提点中 → 门①待确认 → 已过门①
      → 用例生成 → 用例评审 → 门②待确认 → 已过门② → 已入库
```

run.status = 全部需求里**最慢**的阶段（总闸）；门按需求放行——REQ-001 过门①即进生成，不等 REQ-002。
（v4 命名修正：需求侧叫"需求评审"、用例侧叫"用例评审"，与 run 态一一对应，不再共用"评审中"。）

## 3.3 progress 对象（gen_run.progress，前端唯一渲染源）

```
requirements {total, 各 gen_status 计数}
testpoints   {proposed, confirmed, generated, rejected}   ← rejected 从 proposal.status=reject(kind=testpoint) 聚合
cases        {proposed, passed_machine, in_duel, split, scored, need_rework, kept, rejected, deferred}
coverage     {lit, gap, na}（从 coverage_cell 查）
pending      {unanswered, budget_used/上限}（从 question 查）
budget       {tokens_used, limit, state}
```

---

# 第四部分 · 逐环节设计（统一六项：目的/谁做/输入/输出/步骤/UI）

## 环节 0 · 需求入口

- **目的**：外部文档变成结构化需求记录。
- **谁做**：人 + 可选 AI。
- **输入**：PRD/页面/文件/手工。**输出**：`mc_test_requirement` + `mc_knowledge_raw`。
- **五条入口**（全汇 L1）：①CDP 会话（浏览器连着打开飞书/Confluence 让 agent 读页面提需求，权限在人）②dsh 会话（本地文档/仓库）③URL 拉取（source.fetch）④文件上传（Word/MD/Excel）⑤人工录入（Tiptap）。
- **步骤**：落 L1 raw（content_hash 幂等）→ 建需求行 → 人过目。
- **UI**：需求页（现有）。未分析需求在导图灰节点，不进流水线。

## 环节 1 · 需求分析

- **目的**：把需求翻译成生成流程用的结构化产物，后续只吃它不回头读原文。
- **谁做**：思考模型，每需求一个会话，并行。
- **输入**：需求正文+验收标准。**输出**：`requirement.analysis` JSONB + 大纲导出（Skill5，见下）+ 问题行。
- **步骤**（九步，按 codexqa 正文）：结构盘点（每块全/薄/无）→ 输入审计 → 坏味扫描 → NFR 八格 → 行为完备 → 追溯对齐 → 依赖网格 → **唯一缺口冲突登记表**（事实[引原文]/假设/待确认三分）→ 可测性检查（验收标准转条件→预期，转不出标不可测）。
  - **入库四标准**（机器判，evidence.py 纪律）：三分齐全/验收 ≥80% 结构化/禁假精确/P0-P1 带验证方法。不合格=停留"分析未过"。
  - **Skill5 PRD转大纲**（v4 落正文）：分析同时产"根→模块→功能→测试点→步骤/预期"五级大纲——前两级此刻就有（模块/功能），后三级随门①②逐层填入导图；导图顶部「导出 XMind/Markdown」按钮按当前深度导出。**导图叶层（用例行）展开即步骤+预期**，导出时完整。
- **UI**：需求卡片徽章（未分析/已分析/过期——正文改动自动 stale）；点开折叠看登记表，不跳页。

## 门⓪ · 需求评审（停顿点一）

- **目的**：AI 评需求文档本身质量，人确认需求过关才往下（v2 整个丢掉的环节）。
- **谁做**：思考模型出评审报告 + 人确认。
- **输入**：analysis。**输出**：`requirement.review` JSONB（报告+TSDR 决策记录）+ `mc_test_question`。
- **步骤**：
  1. 评审清单逐项过：完整（缺角色/状态/失败路径）/ 可测（能写条件→预期）/ 一致（目标-功能-需求链）/ 可行（依赖 ready）/ 清晰（坏味）/ 风险（P0/P1 标注）
  2. 每条发现带**证据（原文位置）+ 建议动作**，无证据不出现
  3. **拷问式提问（全套协议，v4 补齐）**：一次只问一个 / 每问带推荐答案 / 能查库先查不问 / "看情况"必须追到具体 / **10-15 问预算封顶**（question.round_no 计数）/ 结束出 TSDR（每决定=问题/推荐/实际/理由）
  4. **死锁出口（v4 补）**：预算耗尽仍有未答问题 → 自动降级 `risk_accepted`（=按推荐答案假设走，测试点标 to_confirm），人可改回；门⓪不被 unanswered 卡死
- **UI**：需求详情"评审"Tab：报告+问题侧栏（一次一问卡片流）+「需求过关」按钮（有 risk_accepted 时明示"含 N 项接受风险"）。

## 环节 3 · 测试点提取（含分派，用户点名的顺序）

- **目的**：拆出测试点（写用例/确认/算覆盖的单位）并按端分派。
- **谁做**：四方分工——

| 角色 | 谁 | 干什么 |
|---|---|---|
| 风险定调 | 指挥中心（思考） | 方向"影响×概率"打分→权重表 |
| 提点 | 子agent群（每方向一，并行，思考） | 六规程产点：criteria 直转（≥1正1反）/ invariants 直转（C09 直测）/ 高风险方向按生成器展开（边界组合/状态图路径/判定表行）/ register 待确认→挂起占位点 / 每点五字段+轴+方法+批次 / 自检全覆盖 |
| 轴端标注 | 代码先行+AI 补语义 | "接口方向必出 C12/C03/C04"机械铺；AI 判端（basis=assumption） |
| 完整性复核 | **另一供应商**轻模型 | 查漏轴/重复/恒真式没直测 |

- **输入**：analysis 切片+已过评审标记。**输出**：`proposal(kind=testpoint)`。
- **分派及生成**（单列）：提点后指挥中心按"测试点×端"定 `surfaces`——此判定即扇出计划，写在测试点上。
- **UI**：导图测试点节点：端标签/批次/依据三色徽章（绿事实/黄推断/红待确认）。

## 环节 4 · 覆盖补全

- **目的**：查漏查重。**谁做**：纯代码为主。
- **输入**：测试点 proposal 全量。**输出**：`coverage_cell` 全量 + 补点候选（暂存）。
- **步骤**：铺"轴×端"矩阵 → 四条自检（每需求≥1正1反/恒真式直测/风险有场景/无两行同测）→ 空格 state=gap → 补点（叫 AI 现场补，进暂存）或标不适用（**必填验证依据**）。
- **UI**：热力图三态格（亮/灰/斜纹），点格出操作。

## 门① · 需求确认（停顿点二）

- **目的**：人确认测试点、改端归属、答问题。**谁做**：人（AI 可提问）。
- **输入**：测试点 proposal+问题队列+覆盖图。**输出**：`mc_test_testpoint` 正式行（status=confirmed）。
- **步骤/UI**（导图上就地+侧栏，协议与门⓪同一套拷问式含预算/TSDR，v4 补齐）：
  1. 问题队列（一次一问+推荐答案）；答完 → 挂起占位点激活（basis to_confirm→fact）
  2. 端 chips 点改 → `surfaces_decided_by=user`（门①人改端的留痕就这一个字段）
  3. 依据徽章点开看证据；热力图补点/填不适用依据
  4. 勾选「确认入选」（建正式测试点行）/「打回」（原因记 question 库同表，source=manual）——打回原因与 REJECT 原因统一落点见环节 11
- **放行**：该需求全确认 → gen_status=已过门① → 自动进环节 5。

## 环节 5 · 用例细化

- **目的**：测试点×端 → 用例。**谁做**：调度器派 fan-out，子 agent（专用角色）写。
- **输入**：confirmed 测试点。**输出**：`proposal(kind=case|dataset)`。
- **步骤**：
  1. 调度器按"测试点×端"发子任务（派发**传路径不传正文**——dispatch-by-reference）
  2. 子 agent 内部：**六策略显式过一遍**（正向/反向/边界值/等价类/场景法/状态与流程，A 原文）→ 先写行为（GWT/请求/设备序列）→ **单独设计断言**（api 四层含响应时间；web 元素/文案/URL；app 态/持久化/崩溃；security 拒绝+审计+不可见）。**api 端 Token 失效与越权两场景必覆盖**（Skill15，v4.1 落正文终审#9）
  3. **配比硬规则**：每功能点 1 正向 + ≥3 异常/边界（A 原文，测试点级"≥1正1反"是下限，用例级这条是配额——评审覆盖维度按它算）
  4. 数据按 2.7 归位 + 聚合数据集草案（schema 从 sample_data+变体推，rows 三类齐）→ `kind=dataset` 提案
  5. 三条纪律：先落盘再请教（顾问≤3 次）；冲突发对账不偷换；发现新测试点只提名不开批（指挥中心攒够再派）
  6. 查重 pg_trgm → dup_of_case_id/score/verdict
- **UI**：全自动；用例节点实时长出，测试点节点"3/4 端完成"=各端终态 proposal 计数；暂停/取消；Blocking 线自动停扇出。

## 环节 6 · 评审（三道闸）

- **目的**：人和钱之前先把明显废的筛掉。**谁做**：代码+双模型+打分。
- **输入**：case proposal。**输出**：proposal.review_state/score/reviews。
- **步骤**：
  1. **机器规则**：证据等级（只 executed/static 可判通过）/ 幻觉核验（参数·接口·选择器对 `mc_test_api`/场景库查，MISSING 打回）/ 弱断言（只有 status=200 不合格）/ 硬编码 / 配比配额 / **hard 类用户规则**（回执 applied_rules：未声明=违规）/ 幂等查重
  2. **双评**：两家不同供应商、各半职责（业务对不对/写得对不对）、互不见、必须一致；**弃权=inconclusive 进人**；**分歧不自动仲裁**——consensus=split 的提案带着两份意见一起进门②给人看（评审不设仲裁模型：仲裁只是"另一个能把正确结论劝掉的模型"）
  3. **五维打分**（覆盖25/断言25/异常流20/可执行15/可维护15）：分项存 score_breakdown；<70→need_rework 回派原子 agent（review_round+1，**棘轮**：新版断言数<旧版拒收）
- **UI**：节点徽章 machine_rejected/in_duel/split/need_rework/scored；点开看意见与分项分。

## 门② · 用例确认（停顿点三）

- **目的**：最终拍板。**谁做**：人。
- **输入**：scored/split/inconclusive 的提案+数据集提案。**输出**：正式表三行。
- **步骤/UI**（导图就地）：
  1. 按"测试点×端"分组：组头=五维迷你条（score_breakdown）/双评徽章（consensus）/证据分布
  2. 整组保留前置=全过机器门+一致+≥85；split/inconclusive 组**不给批量按钮**（逐条）
  3. 四判决：keep / modify（就地编辑，保存才弹理由）/ reject（**必选原因**→问题库）/ defer（挂 question_id）
  4. **defer 复活**（v4 补）：关联问题 answered → 该提案自动重新入评审（review_state=machine_pending, round+1）
  5. **入库映射（v4 补全三种 kind）**：testpoint（门①已入）/ case→`mc_test_case`（surface/testpoint_id/留痕）/ dataset→`mc_test_dataset` 且回填 `case.dataset_id`（同事务）
  6. **重开机制**：done 之后缺陷链判"用例错" → 建 `gen_run(reopen, parent_run_id)` 只重跑该测试点；原用例标 rework 不删，**`slot_key` 置 NULL**（让位，绕现库 `mc_test_case(project_id, slot_key)` 部分唯一索引，v4.1 补终审#5）；reopen 新提案入库时 slot_key 带 `:v2` 后缀
- **出口**：该需求全判 → gen_status=已入库；run 全需求 done → `importing→done`。

## 环节 7 · 场景化（探索门）

- **目的**：用例（业务视角）→ 可执行场景。**谁做**：api 机械翻译；web/app 探索。
- **输入**：入库用例。**输出**：`mc_test_scenario`+steps，**经已有 `mc_test_case_scenario` 关联表挂回用例**（不是单列；用例↔场景多对多本来就是表）。
- **探索门按内容分（不按端）**：

| 内容 | 过门 | 依据 |
|---|---|---|
| 参数类型/必填/格式/可行性（框架测试） | **不过** | 契约可定，request+check 步骤机械翻译直接可跑 |
| 字段规则/接口逻辑/接口安全 | **过** | 断言值是推测的，必须真调验证 |
| web/app 全部 | **过** | 选择器/流程全是推测 |

- **步骤**：api=试调一遍（断言字段/业务码真实存在，不存在打回）；web/app=dsh 走引擎 page/device 工具真跑+每步截图（意图驱动/自愈优先/每步留证三技巧），真实轨迹替换推测步骤；挂的段攒成事件→**离线批量重写**→待审变更包人签（不做运行时自愈）。
- **状态**：`case.verified_by_run` 未探索/已探索/探索失败；未过门不进波次。

## 环节 8 · 执行

- **目的**：按策略跑，出可信结果。**谁做**：调度器+引擎（已有），模型只产解释文本。
- **输入**：场景+策略（22号文选例）+数据集行。**输出**：`mc_test_run`（已有实体）+ run_scenario + step_result。
- **步骤**：**sql 型步骤执行前红线闸**（Skill10：无 WHERE 的 UPDATE/DELETE、返回敏感字段、缺 LIMIT 的大表查询→P0 拒跑，v4.1 落正文终审#9）→ 波次=A/B/C（冒烟硬挂停后波）→ flaky 隔离区先滤 → 并行准入（数据范围不相交；设备一机一锁）→ 失败四分类（`failure_kind` 已有：script_issue/product_bug/case_outdated/env_flaky——网络类退避重试、**断言失败不重试**、限流等待、flaky 抖动×2、Retry Budget 封顶）→ 熔断只跳依赖方。
- **结果四态（全组合派生映射，不加新列；v4.1 补齐终审#8 未归属组合）**：

| status × failure_kind | 四态 | 处置 |
|---|---|---|
| pass × 任意 | **过** | — |
| fail/error × product_bug | **挂** | 进缺陷链 |
| fail/error × env_flaky | **判不了** | 不算失败率、不产 bug；回隔离区 |
| fail/error × script_issue | **判不了** | 回场景化重写该场景 |
| fail × case_outdated | **没裁** | 回门② REJECT 流（用例过期） |
| flaky（现库已有态） | **判不了** | 抖动，进隔离区重测 |
| skipped / 超时 | **没裁** | 不算失败率分母 |

  **判不了/没裁不算失败率、不产 bug。**
- **UI**：执行页波次分组+隔离区列+四态徽章；"判不了"点开显示缺的证据；断言失败行重跑按钮禁用。

## 环节 9 · 缺陷链

- **目的**：挂的变可处理的 bug。**谁做**：分流代码+分析 agent。
- **输入**："挂"态结果（trace 经结果 evidence(kind=trace) 取，不加列）。**输出**：`mc_test_bug`（+case_id 关联）+分析报告。
- **六步**：①建单前查重（pg_trgm≥0.85 提示疑似同因，并单留痕）②规范化六段单（**复现步骤继承用例 steps**）③AI 定级 vs 人工改级一致率记账（反馈环）④**八步分析**（现象→影响模块→推荐日志→推荐 SQL[**Skill11 慢查询诊断在此**：执行计划/索引/JOIN/ORDER BY 五维]→关联历史 bug→回归范围→风险→复核点；**无日志禁断根因**）⑤日志诊断（traceId→分页≤20→跨服务→类名方法名定位代码快照行）⑥修复验证七点（原路径复现过/有先失败的测试/改动最小/边界过/回归划哪/关闭证据）。

## 环节 10 · 回归 + 上线判定 + 报告（终点）

- **回归（Skill17）**：release.impact diff（变更代码→入口方法→关联用例一跳）→ **必测**（覆盖变更直接功能）/**建议测**（一跳关联模块）/**抽测**（同模块抽样）→ 回归 run。
- **上线判定**：双条件门——共识 go **且**置信≥0.9 才 go；不够拦下输出聚合理由；回滚触发器事前声明两栏（自动回/人来回）；成功=没回滚且没事故。
- **报告（Skill18）**：数据驱动（结论必须有执行数）、结论先行、分级呈现、四类受众视图。
- **UI**：回归清单页三级；上线判定页 Go/No-Go+理由；报告页受众切换。

## 环节 11 · 问题库回流

- **写入者**：门② REJECT 原因、门①打回原因、bug 结案、返工教训。**统一 sink=已有 `mc_test_question` 表**（扩展 kind：reason/reject、bug_rootcase、rework_lesson、pitfall、risk_scenario；按 module 列索引——不改表结构只扩枚举，v4.1 补终审#10 的 sink 缺失）。
- **风格锚回灌**：不加新表——评审 keep 且分≥85 的用例，`mc_test_case` 打 `golden` 标记（origin 或扩展标记列）；下一轮同模块生成时子 agent"相关历史"切片优先召回 golden 用例（先查重再仿写）。

---

# 第五部分 · UI 全景（思维导图唯一载体）

```
版本X
├ REQ-001 登录改造 [已入库✓]
│  ├ TP-01 正常登录 [api,web] [4条]（展开：步骤/预期=叶层，可导出）
│  │  ├ TC-0001 api [✓已探索]
│  │  ├ TC-0003 api [✗reject:断言不足]
│  │  └ 待确认● 并发登录(未答)
│  ├ TP-02 越权 [用例评审中]
│  └ 空位：权限边界值 [灰]
├ REQ-002 支付 [门①待确认▲]
└ REQ-003 [未开始○]
```

**每个徽章都有数据源（v4 全量闭合）**：

| UI 元素 | 来源 |
|---|---|
| 需求节点徽章 | `requirement.gen_status` |
| 测试点节点徽章 | `testpoint.status/case_review_state` + 端 chips（`surfaces`,`surfaces_decided_by`）|
| 依据三色徽章 | `basis`（fact绿/assumption黄/to_confirm红）|
| 用例节点徽章 | `proposal.status + review_state + verified_by_run` |
| 五维迷你条 | `proposal.score_breakdown` |
| 双评徽章 | `proposal.reviews.consensus` |
| 相似 87% | `proposal.dup_score/dup_verdict` |
| 红点问题 | `mc_test_question(unanswered)` + `round_no` 进度 |
| 灰格 | `coverage_cell.gap`；斜纹=na（点开看依据）|
| "3/4 端完成" | 该测试点各端终态 proposal 计数 |
| 待审（门②入口）| proposal.status=proposed 且 review_state=scored/split |
| 顶部预算条 | `gen_run.budget.state` 四档 |

三道门交互=导图就地+侧栏（门⓪评审Tab/门①②见环节正文）。「只看待确认」开关=红点支路树。
不新增页面：全部长在需求页/用例页/模块页/规则页/执行页/报告页上。

---

# 第六部分 · agent 分工

## 6.1 复用平台底座 + 专用小 agent（不复制父 agent）

角色存表（`agent 角色配置`：名称/规程版本/**工具白名单**/模型用途键/预算），Agent 页统一管理：

| 角色 | 工具白名单 | 模型档 |
|---|---|---|
| 指挥中心 | 读 run 状态/发指令/读产物摘要 | `cmd_center` |
| 需求分析 | requirement.get/question.put | `req_analyze` |
| 需求评审 | 同上+review.put | `req_review` |
| 提点 | 读 analysis/proposal.put | `point_gen` |
| 结构抽取 | 读原文/写 analysis 骨架 | `structure_extract`（v4.1 补行，终审#10） |
| 复核 | 只读测试点+review.put | `point_audit`（**异供应商**）|
| 端用例 | 接口/历史查询+proposal.put+dataset.put | `case_gen_api|web|app|security`（security 中档，v4.1 补） |
| 评审 | 只读 proposal+review.put | `review_a/b`（两家）|
| 探索 | 引擎 page/device+scenario.put | `explore` |

**简化项**：不开闲聊、不带浏览器全套、动作面只有"读上下文+写暂存"。

## 6.2 指挥中心三协议（v4 补全回收与通知）

- **决策回合**：输入=run 状态+产物摘要+预算态；输出=动作（派/停/进下阶段/回派/攒候选开新批）；落 `decisions`（带证据等级可回放）。铁律：只想和派，不亲写。
- **回收协议**：子 agent 产物归并=按 slot_key 去重 → 按测试点×端定序 → 写 progress；对账冲突（同 slot 多版本）取 rewrite_of 链最新；回收即触发机器闸。
- **通知协议**：进入 `req_reviewing / points_proposed / cases_proposed` 三个门态（实际枚举名，v4.1 修正终审#6）→ 走现有通知通道（realtime + 站内）叫责任人开门，**不打弹窗**；预算跨 Blocking 线→通知指挥中心收口。

---

# 第七部分 · 用户规则机制（调研定稿，见 `rules-research.md`）

- 模型=2.6 `mc_test_rule`（kind hard/soft；scope+target_type/target_id 定位到模块/需求/端；enforcement active/**shadow**/disabled；hits/violations 计数）
- 四环节生效：生成期渲染进子 agent 提示词+**回执 applied_rules（未声明=违规）**；校验期 hard 代码判；评审期违反明细+**逐条豁免带过期日**；报表期命中/违反统计
- **新规则默认 shadow** 跑一轮看违反率再 active
- 前端：规则管理页+资产详情侧栏"本项目生效规则"+配置勾选
- 三坑规避：规则必有服务端执行点/未声明=违规/不退化自由文本

---

# 第八部分 · 18 Skill 落位总表（正文级，不再只有表头）

| Skill | 正文在哪个环节哪一步 | 配置入口 |
|---|---|---|
| 1 需求拆解 | 环节1 九步 | 分析规程勾选 |
| 2 测试点提取 | 环节3 提点六规程 | 方法库页 |
| 3 场景补全 | 环节4 四条自检 | 覆盖规则 |
| 4 用例生成 | 环节5 六策略+1正3异配额 | 端配方页 |
| 5 PRD转大纲 | **环节1 步骤内**（五级大纲+导图叶层+导出按钮） | 导出选项 |
| 6 用例评审 | 环节6 三道闸 | 评分卡页 |
| 7 Bug分析 | **环节9 第④步八步全文** | 缺陷配置 |
| 8 日志诊断 | **环节9 第⑤步（traceId/≤20页/跨服务/代码定位）** | 缺陷配置 |
| 9 缺陷规范化 | **环节9 第②步六段单** | 缺陷配置 |
| 10 SQL安全 | **环节8 步骤内**：引擎 sql 步骤执行前红线闸（无WHERE/敏感字段/缺LIMIT→P0 拒跑） | 执行配置 |
| 11 SQL性能 | **环节9 第④步内**（慢查询五维） | 缺陷配置 |
| 12 测试数据 | **环节5 步骤4**（schema 推导/三类 rows/脱敏/造数脚本） | 数据配置 |
| 13 Pytest | 导出适配器（**显式降级：可选后置**） | — |
| 14 JMeter | **显式不做**（性能预留枚举位） | — |
| 15 接口矩阵 | 环节5 api 配方（四层+Token/越权必覆盖）；接口资产维度=覆盖体检，不灌用例表 | api 配方页 |
| 16 Playwright 三技巧 | 环节7 探索纪律（意图驱动/自愈/每步留证） | 探索配置 |
| 17 回归范围 | 环节10（impact 一跳算法+三级） | 回归配置 |
| 18 测试报告 | 环节10（数据驱动/结论先行/四受众） | 报告模板 |

---

# 第九部分 · 模型分层

`cmd_center/req_analyze/req_review/point_gen`=思考档；`case_gen_api` 思考、`case_gen_web|app` 中档；
`structure_extract/point_audit` 便宜（`point_audit` 与提点不同源）；`review_a/review_b` **两家不同供应商**（配置页强制不同族）；`explore/ai_judge` 中档。
纪律：指挥中心每回合读 `budget.state`；Blocking 只收口；扇出不相交才并行；派发传路径。

---

# 第十部分 · 落地分期

- **P0**：`mc_test_gen_run`+testpoint+proposal+question 扩列+coverage_cell+rule 六张表 → gen_status/run 状态机+progress → 分析九步+四标准 → 门⓪（评审+拷问+死锁出口）→ 提点+分派+门①（导图）→ 用例提案只写暂存 → **机器门最小版**（幻觉核验+硬编码+幂等+hard 规则四查，无双评无打分，终审#4 矛盾修正）→ api 框架类不过探索门 → 规则 shadow+回执字段 → agent 角色表+白名单 → catalog 加 type=module 分支不动存量
  **验收：生成一次正式用例表零写入；门⓪①两道走通、门②有提案列表可审（双评/打分在 P1 才有 score_breakdown，v4.1 修正验收与排期矛盾）。**
- **P1**：端用例扇出+三道闸（双评/棘轮/分项分）+门②（defer 复活/入库三映射/reopen 链）→ 四生成器进方法库 → 查重 → 模块统一视图 → 覆盖统计 → 五作用域机器门+数据链闭环（dataset 提案→入库回填）
  **P1 落地记录（2026-10-18，492+117 测试全绿）**：
  - `generation/case_gen.py`（新）：fanout_tasks（测试点×surface）/build_case_prompt（六策略+≥1正≥3异+五作用域+三纪律）/case_to_payload（slot_key+evidence_class 映射）
  - `generation/reviewer.py`+`review_flow.py`（新）：三道闸——machine_pass 复用 gates、merge_dual（都过 agree/任一弃权 inconclusive 优先/否则 split，不仲裁）、score_dimensions（25/25/20/15/15+零维封顶69）、ratchet_check（断言变少拒收）、apply_rework（rewrite_of 版本链+:v2 后缀+≤3轮 defer）
  - `generation/method_lib.py`（新）：四生成器可执行体——pairwise_combinations（IPOG 贪心，8参数×4值→45条 vs 全组合65536）/boundary_values（复用 transform_table+补 min/max 本体+分页/每页/限流三轴）/negative_payloads（injection/auth/malformed/concurrency 四类别带轴归属）/state_machine_paths（全状态/全迁移/守卫/非法迁移）+ SURFACE_RECIPES 四端配方 + inject_rules_block（调 rules 模块）
  - `generation/dedup.py`（新）：trigram_similarity（纯 Python Jaccard，生产换 pg_trgm 唯一替换点）/find_duplicates/verdict_for 三档（merge≥0.85/keep_both≥0.6/独立）/run_dedup
  - `generation/coverage.py`（新）：build_coverage_matrix/coverage_gaps（只报适用轴）/self_check_requirements（CC1-CC4 四条）/sync_coverage_cells（lit/gap/na 幂等，na 必填依据）
  - `generation/advance.py`（新）：门①全确认→run_case_generation→review_run_proposals→落 cases_proposed（门②）；接线在 testpoint.confirm 末尾 asyncio.create_task
  - `orchestrator.py` 追加：run_case_generation / finalize_import（用例+dataset 双入库+回填 dataset_id+幂等）
  - `gen_methods.py` 追加：case.verdict（四判决，reject 必带 reason）/case.import/coverage.summary
  - `TestCase` 补 surface/testpoint_id/verified_by_run 三列（P0 遗漏，P1 补）
  - 方法注册表 195→198（+case.verdict/case.import/coverage.summary）
  **验收：门①确认后自动扇出用例→评审→停门②；入库带 surface+testpoint_id+来源留痕；重跑幂等不重复（slot_key+imported_at）。**
- **P2**：web/app 探索校正 → 执行波次/四分类/隔离区/四态映射 → 缺陷六步（含 Skill11 慢查询）→ 回归/上线双条件/报告 → 问题库回流闭环+风格锚回灌

---

# 附录 A · 24 号文（框架）与本文（实现）关系
24=方法基底与流程框架宣言+外部吸收对照表；25=可落地的表/状态机/环节/UI。冲突以 25 为准。

# 附录 C · 终审（v4）10 处修复对照（v4.1）

| 终审条目 | v4.1 闭合位置 |
|---|---|
| #1 mc_test_question 撞现库同名表 | 2.4 改为**扩列复用**（resolution +risk_accepted，沿用 assert_not_blocked 阻塞语义），不新建 |
| #2 现库三条直写通道与铁律冲突 | 1.2 迁移声明（case.generate 改造为 gen_run 编排器；enumerate/scan 明确豁免定性） |
| #3 catalog 合并逆转 10-14 定版 | 2.1 改"P0 只加 type=module 分支不动存量；合并降 P2 需重批" |
| #4 P0 验收与门②排期矛盾 | 第十部分 P0 补机器门最小版，验收改"两道门走通+门②有列表可审" |
| #5 slot_key 唯一索引挡 reopen | 门②步骤6：rework 行 slot_key 置 NULL 让位，reopen 带 :v2 后缀 |
| #6 状态名对不上/*_pending 无此枚举 | 3.1 补 run_type×起止表+gen_status↔run.status 映射表；6.2 用实际枚举名 |
| #7 幻列复发（case_review_state/user_confirm） | 2.2 补 case_review_state 聚合列；门①删 basis 记 user_confirm 句 |
| #8 四态映射不封口（flaky/case_outdated/trace） | 环节8 全组合映射表补齐 7 行；trace 走 evidence 不加列 |
| #9 Skill10/15 只落表头复发 | 环节8 步骤首 sql 红线闸；环节5 步骤2 Token/越权必覆盖 |
| #10 sink 缺失/角色表缺行/表缺 project_id | 环节11 统一 sink=question 表扩 kind+golden 标记列；6.1 补两行；2.x 补 project_id/时间戳惯例 |

# 附录 B · v3 复审 17 处修复对照

| 复审条目 | v4 闭合位置 |
|---|---|
| 生成 run 没表 | 2.0 新表〇 gen_run |
| 状态机止于 done/无回边/无 parent_run_id | 3.1 回边+reopen+衔接声明；3.2 命名对齐 |
| testpoint candidate 悬空 | 2.2（confirmed 起，generated 定义+重算） |
| 门①协议半搬 | 门①步骤（全套拷问式含预算/TSDR） |
| Skill5 名实不符 | 环节1 大纲+导图叶层+导出；第八部分表 |
| Skill10/11 只落表头 | 环节8 红线闸 / 环节9 第④步 |
| 六项半塌 | 全部环节统一六项头 |
| 回收/通知协议缺 | 6.2 |
| review_round/dup_verdict 缺列 | 2.3 |
| 双评分歧无仲裁流程 | 环节6 第2闸（split 带两意见进人，不设仲裁模型）+门②不给批量钮 |
| defer 复活路径 | 环节6 问⑤…门②步骤4 |
| bug 关联列 | 2.1 mc_test_bug 行 |
| Skill4 1正3异/六策略 | 环节5 步骤2-3 |
| Skill17 impact 算法 | 环节10 |
| 表分类矛盾（testpoint 两处） | 2.1 删行，只留 2.2 |
| 引用不存在列（score/user_confirm/link_scenario/progress.rejected/五维条） | 2.3 全补列；cases 走已有 case_scenario 关联表（环节7）；rejected 聚合口径（3.3） |
| 数据集入库断链 | 门②步骤5 三 kind 映射 |
| 门⓪死锁 | 门⓪步骤4 risk_accepted 出口+round_no |
| rule 缺作用对象/applied_rules 无存储/待审徽章无源 | 2.6 target_*；2.3 applied_rules；第五部分徽章表 |
| 执行结果表不明/四态重叠 | 2.1"不加新列"+环节8 派生映射表 |
| "沉淀"残留 | 环节11/分期均改"回流" |