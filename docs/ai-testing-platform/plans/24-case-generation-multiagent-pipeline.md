# 24 · 用例生成重构——以《18个Skills》为方法基底的落地设计

> 2026-10-16 v2（v1 已被用户否决：「找错方向，方式方法 html 里已经列好了」——v1 自创流程作废）。
> **2026-10-17 首件落地**：Skill6 的证据分级校验器 + ai_judge 四态裁决已进代码（见 §14）。
>
> 本文立场修正：**平台不做方法论发明者，做宿主**。方式方法 = 《18个Skills》六阶段十八个 Skill
> （输入/输出/核心规则照原文，不重写、不裁剪）。平台只做四件事：
> ①把每个 Skill 做成**能力包**（提示词+输入输出契约+MCP 工具）；②**调度**它们串成流水线；
> ③产物**落库**（暂存→确认→正式）；④**页面**呈现与人工门。
> 用户 8 点增量要求（端类型/存储/流程/两库/需求富化/场景策略/执行/缺陷链）作为落地约束揉进对应阶段，标注〔用户〕。

---

## 0. 方法基底（外部已定，照单全收）——且只取**方法/技巧**类，技术 skill 不进 skill 层

《18个Skills》18 条混了三类东西，平台必须切开：

- **方法 skill**（大脑怎么想——设计什么、怎么查漏、怎么评、怎么诊断）= **平台做宿主，落成能力包**。
- **技巧 skill**（方法里的具体规程——八步、分页全量、红线表、五维权重）= **方法 skill 的正文规则**，不单列。
- **技术 skill**（手脚怎么动——playwright/appium/pytest/jmeter/SQL 驱动）= **引擎工具层的实现**，
  不是 skill。`engine/tools.py` 已是工具注册表（request/page/android/ios/script/check/ai_judge…），
  playwright/appium 就是 `page`/`android`/`ios` 这些**占位工具的底层实现**——要接是扩展 `ToolRegistry`，
  不占「skill」这个位。

故 18 条重新归类如下（平台只把 ☑ 的做成 skill 能力包；✗ 的归引擎/适配器层，本文不展开）：

| Skill | 名称 | 归类 | 平台承载 |
|---|---|---|---|
| 1 | 需求拆解 | 方法 | ☑ 能力包（主agent） |
| 2 | 测试点提取（五字段+C01-C14查漏+A/B/C分批） | 方法+技巧 | ☑ 能力包（主agent） |
| 3 | 场景补全（覆盖地图·已覆盖/待补充/不适用） | 方法+技巧 | ☑ 能力包（主agent+确定性矩阵） |
| 4 | 用例生成（六策略+1正向≥3异常+三层断言） | 方法+技巧 | ☑ 能力包（子agent，按端配方） |
| 5 | PRD转XMind（结构化大纲） | 方法（表达） | ☑ 导出/可视化（可选） |
| 6 | 用例质量评审（五维评分卡·权重表·阈值） | 方法+技巧 | ☑ 能力包（reviewer子agent+机器分） |
| 7 | Bug分析（八步流程·禁止跳步·无日志禁断言） | 方法+技巧 | ☑ 能力包（执行后） |
| 8 | 日志诊断（traceId·分页全量≤20页·跨服务·代码定位） | 方法+技巧 | ☑ 能力包（执行后） |
| 9 | 缺陷报告规范化（强制字段清单） | 方法（规范化） | ☑ 能力包（接23号文bug域） |
| 10 | SQL安全红线（红线表：无WHERE/敏感字段/缺LIMIT→P0） | 方法+技巧 | ☑ 能力包（sql步骤前闸） |
| 11 | SQL性能分析 | 技术（执行计划/索引诊断） | ✗ 引擎/sql 驱动诊断 |
| 12 | 测试数据生成（合法/异常/边界/脱敏策略） | 方法+技巧 | ☑ 能力包（→mc_test_dataset） |
| 13 | Pytest工程生成 | 技术（脚本工程产出） | ✗ 导出适配器（可选） |
| 14 | JMeter | 技术（性能，预留） | ✗ 不做 |
| 15 | 接口测试矩阵（四层断言·Token/越权必覆盖） | 方法+技巧 | ☑ 能力包（接口资产维度） |
| 16 | Playwright E2E | **技术**（自愈选择器/意图驱动是技巧） | ✗ 引擎 page 工具底层；技巧拆出 |
| 17 | 回归范围分析（必测/建议测/抽测分级） | 方法+技巧 | ☑ 能力包（接release.impact） |
| 18 | 测试报告（结论先行·4类受众视图） | 方法（表达） | ☑ 能力包（report） |

**Skill16 的价值不在 playwright 这个技术，而在它揭示的三条技巧**，这三条拆出来归方法层：
①意图驱动（自然语言目标，不固化选择器）；②自愈优先（选择器失效从可访问性树找替代）；
③每步留证（截图归档）。技术实现交给引擎 `page` 工具去接 playwright/appium，平台 skill 只收技巧。

**Skill13 同理**：pytest 是技术（导出适配器，可选）；「三层断言+数据独立管理+工程结构」是方法，
已在 Skill4/15 里承载，不重复。

**C01-C14、三层断言、五维评分、事实/猜测三分、A/B/C 分批、八步、分页≤20、红线表**——
全部按原文作为代码常量与能力包提示词正文（`design/transform_table.py` 的 COVERAGE_AXES 是 Skill2/3 的矩阵本体，保留）。

---

## 1. Skill 怎么「拿过来」——能力包五元组

每个 Skill 落成：

```
prompts/skills/<stage>/<skill>.md     # 提示词正文 = 原文「输入输出 + 核心规则」（不改写方法论）
   + 输入/输出契约                     # 输出按原文产物形状结构化（JSON schema）
   + MCP 工具                          # 读写平台数据的白名单方法（skill 只能经工具落库）
   + 执行者                            # 主agent / 子agent / 确定性代码
   + 落库映射                          # 产物进哪张表：暂存 or 正式 or 只渲染
   + 页面                              # 在哪看、在哪确认
```

- **主agent = 工作流设计者**（原文结语「你定规则，矩阵执行」）：主agent 只做「理解→拆解→分派→鉴定→汇总」，
  不写具体用例；具体产出由 Skill 按契约交给**子agent** 或确定性代码。〔用户：主agent思维/子agent细化〕
- **项目上下文文件（抄 qa-project-context 形态）**：每测试项目持有一份上下文（技术栈/测试框架/命名规约/
  选择器策略/已知风险区），生成 run 起手注入——"context is everything，喂得越多返工越少"。
- **skill 只收方法/技巧**（§0 归类表）：playwright/appium/pytest/jmeter 等技术是引擎工具层
  （`engine/tools.py` ToolRegistry）的底层实现，**不做成 skill**。Skill16 拆出的三条技巧
  （意图驱动/自愈优先/每步留证）进 web/app 端子agent 的配方与探索校正的执行纪律。
  现成可借（联网受限，选型待核）：**SKILL.md 打包规范**（能力包文件形态）、
  **Gherkin**（测试点/场景可读表达，可选）。

---

## 2. 流水线总图（Skill 编排 + 用户 8 点落位 + 第二轮深读融合）

**入口五模式（抄 codexqa-testcase-generator V56）**：Plan(仅出测试设计) / Exec(仅出用例，须先有落盘的设计) /
全量(Plan→问一次→确认后 Exec) / **提测前改用例**（改草案不动设计）/ **提测后增量**（PR/git diff→在冻结用例基线上补，
无基线先自举首轮）——增量模式是我们此前完全没有的。范围=当前版本全部需求〔用户〕，模式是另一个维度。
**阶段闸（同上）**：每阶段产物落工作区（run_dir=暂存区+设计记忆的文件形态）→ 确定性脚本核闸（拒绝空壳产物）
→ run-status 记 currentStage → 断点续跑；**没过闸只能说「草稿」**。

**调度派发=任务→能力包→子agent 映射表（抄 agentic-qe Queen PHASE4）**：任务类型→激活哪些 skill 能力包→扇出几个子agent
（如 用例生成→api/web/app/security 四族；覆盖分析→coverage+gap-detector 两族；质量门→quality-gate+risk 两族）。
**铁律**：调度器必须真的起子agent、不许在主agent里"描述 agent 会怎么做"（抄 Queen 强制协议）。
**派发算法（抄 R8 queen）**：hard/soft/peer 三类依赖分相并行——硬依赖分相（无未满足硬依赖的先跑，其硬依赖完成的再跑），
**软依赖永不等待**，缺硬依赖只告警继续；按关键路径排（planner：每子任务必有输入+输出）。
**扇出准入（抄 R10 opencode-swarm）**：范围证明两两不相交才并行，否则**自动退回串行（fail-closed）**；
大扇出开关只能人按（agent 按不了）；**dispatch-by-reference**：给子agent 传路径不传正文（正文落盘，避免 42k 粘贴历史）。
**切批（抄 R8 parallel-executor + R10 CodeJury）**：按历史耗时均衡切批（不是按条数），跑完回写 timing；切批粒度由下游模型
单请求上限反推，截断落行边界并自我标注；并发有减速点（8→90%/16→86%），设上限。
**上下文预算 5 态（抄 agentic-qe）**：Normal/Warning/Pressure/AutoCompact/**Blocking(≤阈值=阻塞新子agent)**；
Blocking 是我们 token_budget.py 三态之上的硬刀——预算耗尽就不让再扇出。
**预算作用范围（抄 R10 opencode-swarm）**：超限时只裁出站请求、掩已完成的老工具输出，**落盘工件与持久历史永不动**；
预算告警只发给主agent。

```
【阶段一 · 需求分析】  范围=当前版本全部需求（默认全量，勾选只是收窄过滤器）〔用户〕
   Skill1 需求拆解（主agent，逐需求封闭上下文）
      └ 需求富化落库：功能点/操作链路/显性规则/隐含约束[待确认]
        + 结构化验收标准[{id,condition,expected}] + 端预判 + 风险标注
        → 用例阶段从富化需求起跑，不从原始PRD起跑（少带东西）〔用户点5〕
      ★固定管线（抄 codexqa-requirement-analyzer）：结构盘点→输入审计→质量/坏味/NFR→
        成功指标→行为+UX态完备→追溯对齐→依赖网格→★唯一一份缺口冲突登记表→
        风险算法→★可测性与oracle检查→阻塞项
      ★硬规则：P0/P1 条目必须带可执行验证（前置/激励/预期/证据/VerifyMethod/RiskClass，P0加FailureMode）；
        没有可观察失败条件的=不可测，禁止写成可执行P0；禁假精确（RPN/百分制/红绿灯分不许造）；
        事实/假设/待验四分离；冲突保两方观点+建议拍板人
   Skill2 测试点提取（主agent）
      └ 五字段+C01-C14查漏+A/B/C分批；前置风险矩阵（impact×probability 排方向，抄 risk-based-testing）
      ★新增不变式与歧义两桶（抄 ai-test-generation Step2）：Invariants(恒真式，每条必得直接测试) +
        Ambiguities(显式登记问人，禁止悄悄选)
      └ 产物进暂存区，不入正式表〔用户点3〕
   Skill3 场景补全（主agent+确定性矩阵）
      └ 升级为覆盖矩阵（抄 ai-test-generation Step3）：需求→场景→类别→优先级→oracle类型
        自检4条：每需求≥1正1负/每不变式有直测/每风险有场景/无两行测同一事
   【门①·需求确认】AI 可在此 question.raise 问人（双向门）；★拷问式（抄 grill-me-qa 全套交互协议）：
      一次只问一个、每问必带推荐答案、能查库先查不问、挑战模糊回答（"看情况"不算答案）、
      10-15问预算封顶、产出决策记录 TSDR（每维 Resolved/Deferred/Open + 决策日志 + 遗留）；
      人：确认测试点 / 改端归属 / 答疑。〔用户点3〕
      端类型 surfaces(api/web/app/security)=测试点与用例的归属维度〔用户点1〕

【阶段二 · 用例设计】——Skill4 内部 = 七步链（抄 ai-test-generation，改造为我们的域）
   Step1 抽取：Entities/规则编号/显式[REQ-N]/隐式[IMP-N]（IMP 全标待人确认）
   Step2 分析：风险表 + 不变式 + 歧义 + 风险推边界
   Step3 矩阵：需求×场景×类别×优先级×oracle类型（上面 Skill3 的表）
   Step4 场景：Given/When/Then+测试数据；类别=happy/boundary/negative/security/a11y/状态迁移/并发
   Step5 oracle：★与场景刻意分离；四类=UI态/数据(查API·DB)/反向断言(不该发生)/副作用(埋点·消息)；
      断业务结果不断实现；用最具体断言；必含反向断言
   调度器（代码，非LLM）按「测试点 × surface」扇出子agent（需要子节点）〔用户点3〕
   Skill4 用例生成（子agent族：api-case/web-case/app-case/security-case，按端模板写——抄 codexqa 三端模板形态）
      └ 每个吃封闭上下文：1测试点+1端配方+历史bug/历史用例召回切片
      └ 产物→暂存区（不直接入库）〔用户点5〕；历史用例查重命中→标 dedup 不重复造
      ★主agent 下发计划契约（抄 R11 sparc-orchestrator/planner）：每子任务带
        交付物schema+过闸标准（Agent/Outputs/Quality Gate 三元），闸默认阻断；支持整链跑/单环重跑
      ★子agent 顾问钩子（抄 R11 executor-preamble 整段）：子agent 可请教更强 reviewer 模型——
        定向(找文件取数据)自己做、实质工作(写/下结论)先问顾问；**先落盘再请教**（会话中断结果不丢）；
        "自检通过≠顾问错，是你的测试没覆盖顾问所查"；冲突不静默倒戈、发一次 reconcile；每任务≤3次超限不重试
      ★候选测试点折回（抄 R10 受控版）：子agent 生成中发现新测试点→只提名到暂存区，
        由调度器决定是否开新批——**子agent 不许自己递归展开**
      ★护栏：Step1-3没齐禁止出用例；场景先于断言；中间产物永远要出
      ★方法库=可执行生成器：pairwise/boundary-value/negative/state-machine 四生成器正文（qaskills，
        见 skill-library-research R5）——boundary/negative 的变体表与载荷库与我们 transform_table/INJECTION_SAMPLES 合并
   Skill6 用例质量评审（reviewer子agent+机器分）
      ★证据分级纪律（抄 agentic-qe ADR-105，提案行带 evidence_class）：每条结论标
        EXECUTED(真跑命令附输出)/STATIC(从数据推导,点名数据源)/INFERRED(推理,禁"已验证"口吻)/CONJECTURE(外推,自标)；
        **质量门只对 EXECUTED/STATIC 放行；INFERRED 转对抗复核；CONJECTURE 永不门控**；能廉价执行就别推理
        → 落 `quality/validators.py` 文本级校验器（禁裸百分数/禁"影响很多"必写具体数/Claimed 禁推测/推理必三段式
          "做什么→为何重要→会怎么错"/Claimed 是一等态非失败态），R9 evidence-classification 全套规则照搬
      ★机器幻觉门先机器后人工：参数/选择器/endpoint 白名单核库，MISSING 即打回；跑挂的先隔离再审
      ★评审一致性协议（三源同指一个设计，照搬）：评审=两位**不同供应商**模型、各拿一半职责、互不见对方意见、
        **必须一致才放行**；阈值**取 2/3 而非简单多数**（一个高置信错答能在 1/2 规则下翻盘）；**弃权(超时/限流/解析失败)
        =INCONCLUSIVE 而非通过**；选 judge 绝不用同模型三拷贝（独立性权重最高，相关失效造假自信）
      ★反灌水三件套（防子agent/返工换绿灯）：
        - **棘轮 ratchet**：返工重试不得用删断言/降标准/删测试换通过（抄 swarm-orchestrator）
        - **outcome 三态含 unjudged**：裁决不了就明说，agent 自报的总结永远不是事实源
        - **anti-anchored review**：评审者从没见过规划者的理由，避免被锚定（抄 cc10x）
      ★评分格式契约：Strengths/Critical(每条带 Impact+Fix+行号)/Suggestions/Metrics/Action Items；
        **成功=零 critical 硬门禁**；严重度阶梯 Critical→Major→Minor→Suggestion；上下文调权（按阶段/技术债降级，防一刀切）
      ★对抗式评审候选：six-thinking-hats / brutal-honesty-review / sherlock-review（agentic-qe）
      ★红绿灯反模式12条清单（grill-me-qa）当机器检查项：硬等待/脆选择器/顺序依赖/断实现/
        无数据策略/每用例全登录/无flaky遥测/覆盖虚荣/AI未审就合…
   【返工回路】<70 → 回派原子agent 重写该批（回路，非直线）〔用户点3〕
      ★返工不变量（抄 R11 refinement）：已绿测试不许破坏——返工重写不许删既有通过项换"看起来通过"
      ★失败四分类+熔断不连坐（抄 R8 retry-handler）：transient/deterministic/resource/flaky 四类；
        网络退避×3 / 断言失败**不重试**（重跑也是错）/ 熔断只 SKIP 坏依赖、其余用例继续 / Retry Budget 整轮封顶
   【门②·用例评审】★四判决=暂存区状态机（抄 ai-test-generation Step7）：KEEP/MODIFY/REJECT/DEFER
      + edit-then-apply → 入库
      入库=确认动作：proposal(accepted)→ mc_test_case（slot_key 幂等+origin 留痕
      +★复现元数据：模型ID/输入hash/skill版本）
      ★分步选模型：抽取/矩阵用轻模型，oracle/用例用强模型（接 testing-ai-config-per-purpose，每用途再分层）

【阶段二.5 · 场景化（用例→可执行场景）】〔用户点6〕
   = 七步链的 Step6（代码=机械翻译，带追溯注释）
   方案选择=配置项，per 端：
      api   ：先生成后执行（契约/参数schema已知，离线可生请求+断言）
      web/app：生成骨架+探索校正（agent 活跑真实系统、录真轨迹固化步骤；
               技巧照 Skill16 拆出的：意图驱动/自愈优先/每步截图；技术底座=引擎 page 工具接 playwright/appium）
      均可手动切「纯探索记录」（AI真跑→记步骤→编脚本，10-15 既定决策）
      前提闸：该端有活动环境+凭据才解锁探索类；无环境回退「先生成后执行」
   落库走 scenario.generate 确定性校验门（工具白名单/不过不落库）

【阶段三 · 执行】〔用户点7〕
   波次闸：A冒烟先跑，硬挂即停后续波次（省时）；→B重点→C扩展
   隔离区闸：flaky quarantine 先过滤（诊断 flaky-test-doctor / 检测 test-flakiness-detection），
      被隔离用例不进波次（抄 qaskills flaky 三件套）
   依赖感知：标注依赖的串行、无依赖并行；设备一锁一；共享登录态按 worker 隔离
   隔离方法：数据/环境不互染（test-hermetic/isolation-strategies）= 并行前置条件
   容量闸：复用 rate_limiter/limit_policy；不稳重试=env_flaky 重跑一次（bug.triage 口径）
   进度/取消/分片落库：realtime gateway 现成

【阶段四 · 执行后】〔用户点8〕
   失败→ bug.triage 确定性分流（product_bug/env_flaky/script_error）
   Skill9  缺陷规范化：口语→标准报告（接23号文 bug 状态机/指纹/活动流）
   Skill7  Bug分析：八步（含推荐日志SQL/关联历史bug/回归范围/复核点）
   Skill8  日志诊断：traceId 分页全量（≤20页）/跨服务链路/代码定位
   Skill17 回归范围：必测/建议测/抽测 → 生成回归 run（接22号文测试策略）
   Skill18 测试报告：结论先行+4类受众（report 承接）
   沉淀：根因/pitfall → 场景问题库（飞轮，见 §4）

【横切 · 数据与SQL】Skill10/11/12
   Skill10 SQL安全红线=engine sql 步骤执行前闸（无WHERE/敏感字段/缺LIMIT→P0 拒跑）
   Skill11 性能分析=辅助诊断（同上）
   Skill12 测试数据生成 → mc_test_dataset（用例 dataset_id 直接消费）

【技术层（非 skill，本文不展开）】Skill13 Pytest 导出适配器 / Skill14 JMeter（性能预留）/
   Skill11 执行计划诊断 / Skill16 的执行底座=引擎 page·device 工具接 playwright/appium——
   要做的是扩展 ToolRegistry 实现这些占位工具，方法/技巧已全部收进上面的能力包
```

---

## 3.〔用户点1〕端类型——用例归属端

- 测试点带 `surfaces[]`（主agent 鉴定 + 门①人改判）；用例带 `surface` 单值列。
- 用例 body 按 surface 类型化（对齐 TestScenarioStep「type 决定 params」惯例）：
  common 信封（编号/标题/需求/测试点/优先级/状态/依据/来源）+ 端专属 body：
  - api：请求定义 + 四层断言（HTTP+业务code+关键字段+响应时间，Skill15 口径）
  - web：页面步骤 + 元素/文案/URL 断言（黑盒看结果）
  - app：设备动作 + 前台态/持久化态/崩溃日志断言
  - security：拒绝码 + 审计记录 + 数据不可见断言（横切，可与 api 叠加）
- 性能（Skill14）本轮不做，枚举留位。

## 4.〔用户点2/4〕存储与两库

- **AI 产物轨迹**（探索记录、子agent 原始输出、截图）：追加式产物存储（OSS 文件或 ClickHouse，
  按体积与查询频度选），整 run 可一键丢弃不污染业务库。
- **run 工作区（抄 codexqa run_dir 形态）**：每个生成 run 一个工作区目录/前缀，阶段工件（缺口登记表、
  测试点清单、覆盖矩阵、oracle 定义、草案）按阶段落盘；`run-status`（currentStage/completed）由
  **确定性阶段闸脚本**推进——闸拒绝空壳产物（标题党报告/占位清单），**没过闸产物只能标「草稿」**，
  这是「设计记忆」的可核查形态，也是断点续跑/单阶段重跑的基础。
- **评审表** `mc_test_proposal`：由**投影器**从 AI 产物物化（人要 edit/状态流转/join 的那份），
  确认才写正式表。AI 永不直写 `mc_test_case`/`mc_test_testpoint`。
  提案状态机=四判决：`proposed → keep/modify/reject/defer`（抄 ai-test-generation Step7），
  每行带★复现元数据（模型ID/输入hash/skill版本）。
- **方法库** = Skill 包本体（§1）：版本化、启用开关、按端/按轴索引。主agent 按配方选，子agent 按配方写。
  **关键升级（第二轮调研）**：方法库条目不是"方法说明文"，而是**可执行生成器**——每条方法带输入→输出规程
  （如正交=参数表→全对组合、边界值=范围→min-1/min/max/max+1、状态迁移=状态图→合法/非法跃迁、反向=正向用例→镜像异常），
  抄 qaskills 的 pairwise/boundary-value/negative/state-machine-test-generator。正文措辞以 ISTQB 口径兜底（fugazi qa-manual-istqb）。
- **场景问题库**：pitfall / risk_scenario 条目（knowledge kind 已预留）；来源=bug 结案沉淀+返工暴露+覆盖缺口；
  消费=子agent 错误推测原料。**沉淀→消费→再沉淀 = 飞轮**（抄 quality-postmortem 复盘规程）。

## 5.〔接口用例重定位〕Skill15 的正确用法

- Skill15「接口测试矩阵」属于**接口资产维度**：矩阵挂接口详情（`mc_test_api` 关联面），
  做该接口的四层断言体检（含 Token 失效/越权必覆盖）——**不再灌业务用例库、不再 status=active 冒充业务用例**。
- 业务链路里出现接口用例的唯一正道：测试点被鉴定 `surfaces含api` → api-case 子agent 依需求语义产
  （带 requirement_id/testpoint_id）。每条接口用例回答「为哪个测试点存在」。
- 现 `design/skeleton.py` 枚举降级为接口体检的确定性内核，不默认写 `mc_test_case`。

## 6. 落库映射（Skill 产物 → 表）

| Skill | 产物 | 落哪 |
|---|---|---|
| 1 需求拆解 | 功能点/规则/约束/富化 | requirement 富化字段 + knowledge_item（过可测闸） |
| 2 测试点 | 五字段测试点 | proposal(kind=testpoint) → 门① → `mc_test_testpoint` |
| 3 场景补全 | 覆盖地图 | run 产物+前端渲染（不落正式表） |
| 4 用例生成 | 按端用例 | proposal(kind=case) → 门② → `mc_test_case`(+surface/testpoint_id) |
| 6 评审 | 五维分+建议 | proposal.score / change_record |
| 12 测试数据 | 数据集 | `mc_test_dataset` |
| 15 接口矩阵 | 接口覆盖矩阵 | 接口资产关联面（不进业务用例） |
| 16 技巧 | 探索纪律（意图驱动/自愈/留证） | 进 web/app 端子agent 配方；执行轨迹+截图落 AI 产物存储 → 编译 scenario steps（过校验门） |
| 7/8/9 缺陷 | bug/诊断报告 | `mc_test_bug` + attachment/log 镜像 |
| 17/18 | 回归集/报告 | 22号文策略 + report |

新表：`mc_test_testpoint`（测试点中枢，需求维度）、`mc_test_proposal`（评审投影）。
加列：`mc_test_case.surface` / `testpoint_id`。用例↔用例关联 `mc_test_case_link`（依赖/互斥/成组）。

## 7. 页面与配置〔用户〕

**页面**：
- 流水线看板：需求 × 阶段矩阵，每格亮 Skill 进度（提取/分派/补全/确认/评审/返工）
- 测试点评审页：C01-C14 覆盖图 + 测试点清单 + [待确认]高亮 + AI 问题内联作答（门①）
- 用例评审页：测试点×端 草案 + 五维分 + 去重命中 + edit-then-apply（门②）
- 场景策略配置页（per 端方案选择）+ 执行看板（波次/并发/资源/flaky）
- 缺陷分流页：triage→Skill7/8 报告→回归集→「加入问题库」
- 库管理页：方法库（启用/版本）+ 场景问题库（CRUD/自动沉淀记录/使用统计）

**使用者配置**（不写死）：范围（默认版本全量+可选收窄）、per端场景策略、执行计划（并发/波次/超时/重试）、
方法库启用集、问题库自动喂开关、AI 提问路由、每用途 Key+模型（沿用 use-generate-config）、预算（单测试点用例上限/token 上限/扇出上限）。

## 8. 分期

- **P0**：Skill1-3 编排 + 测试点确认门① + 暂存投影 + Skill4（先单端 api）写暂存不写正式表 + Skill15 迁出业务用例。
  **验收：生成一次，`mc_test_case` 零自动写入；测试点/用例全在评审页确认后才生效。**
- **P1**：四端子agent 扇出 + Skill6 评审返工回路 + 历史查重/bug 召回 + 两库 v1 + 门②UI + 场景化策略选择。
- **P2**：执行波次/依赖/资源闸 + Skill7/8/9/17/18 缺陷链 + 沉淀飞轮 + Skill13 导出。

## 9. 外部 skill 融合表（找现成高分 skill，抄进我们的流水线）

调研两个方法/技巧类 skill 库（仅取方法技巧，技术栈类按 §0 归引擎层）：
- `petrkindlmann/qa-skills`（187★，50 个 skill，MIT，SKILL.md 标准包）
- `openqa-cn/codexqa`（183★，11 个中文 skill）

| 我们的阶段/Skill | 抄哪个外部 skill | 抄什么（方法/技巧，非技术） |
|---|---|---|
| Skill2 测试点提取（前） | qa-skills `risk-based-testing` | **在提测试点之前加风险矩阵**：impact×probability 给候选测试点打分、对 top 项做 failure mode analysis，再按风险区排测试密度——比纯 C01-C14 更有方向。6 phase：识别→量化→失效模式→覆盖映射→干系人访谈→重评 |
| Skill3 场景补全 | qa-skills `coverage-analysis` | 覆盖图从「C01-C14 二维」升级为「**按风险的覆盖缺口**」+ 变异测试思想判**断言质量**（行覆盖≠能拦 bug，payment 30%行覆盖绿是假象）；缺口按风险而非行数暴露 |
| Skill4 用例生成 | qa-skills `ai-test-generation` 的 **staged pipeline** | 它的链 requirements extraction→risk analysis→coverage matrix→scenario→**oracle 设计**→code→human review 与本流水线高度同构——抄两件：①「**强制结构化中间产物先出，再出代码**」（与我们暂存区一致，互为印证）；②**oracle 设计**环节（预期结果当一等产物，不是顺手写），补我们三层断言的产出规程。护栏：白名单 API/弱断言检测——可作 reviewer 的机器分规则 |
| 场景化探索执行（web/app） | qa-skills `exploratory-testing`（SBTM） | 抄 **Session-Based Test Management**：每测试点一份 **charter（章程）**驱动 AI 探索；HICCUPS/FEW HICCUPS 启发式清单当探索词汇；findings→转确定性场景。这就是「AI 真跑→记步骤→编脚本」的方法骨架（替代散漫的"让 agent 自己跑"） |
| 脚本失败/重生成那段 | qa-skills `selector-drift-recovery` | 抄它的**离线批量重写**模式（不是运行时自愈）：**事件触发**（重构合并/某步批量挂）→ aria-snapshot diff 定位漂移→批量重写该批→挂单人审签。我们 §2 返工与重生成按此重构：drift 信号→离线批量重写→人审 |
| 需求富化（点5，shift-left） | qa-skills `shift-left-testing` | 抄 **Three Amigos 需求前评审** + **PR 可测性清单** + **Definition of Done 含质量门**——需求阶段就把可测性/AC 结构化做掉（落地为我 24号文的「需求富化」） |
| Skill1 需求拆解 / Skill4 用例生成 | codexqa `codexqa-requirement-analyzer` / `codexqa-testcase-generator` | 中文现成 prompt 范本，作 Skill1/Skill4 能力包提示词的**起稿**（我们的提示词至今太薄） |
| Skill7 Bug分析 / Skill8 日志诊断 / Skill17 回归范围 | codexqa `codexqa-defect-analyzer` / `codexqa-rootcause-analyzer` / `codexqa-change-analysis` | 中文实现，直接对接 23号文 bug 域：defect-analyzer=Skill7 八步 prompt；rootcause-analyzer=Skill8；change-analysis=Skill17（接 release.impact） |
| 场景问题库飞轮 | qa-skills `quality-postmortem` | 抄**事后复盘规程**→沉淀进场景问题库（pitfall/risk_scenario），即我 §4 飞轮的现成方法 |

**包装形态（§1 落地）**：采用 SKILL.md 标准格式（frontmatter: name/description/license/metadata.category + objective/Quick Route/references 段），与上述外部库同构——便于直接借鉴正文、未来可分享/可替换。能力包目录 `prompts/skills/<stage>/<skill>.md`。

**未深读**：只完整读了 risk-based-testing / coverage-analysis / exploratory-testing / ai-test-generation / shift-left-testing / selector-drift-recovery 的头部与 objective；其余按 description 与 skill 名映射。落地前对应 SKILL.md 正文需逐份精读再改写为我们的口径（保留方法、替换技术引用）。

### 9.1 第二轮扩大调研（全量清单见 [`skill-library-research.md`](../skill-library-research.md)）——我们缺的方法/技巧

| # | 缺什么 | 抄哪个现成 skill（库） | 落到我们哪 |
|---|---|---|---|
| 1 | **设计技术=可执行生成器**（不是八法说明文）：正交全对/边界值/反向/状态迁移各自输入→输出组合 | pairwise-test-generator · boundary-value-generator · negative-test-generator · state-machine-test-generator（qaskills） | §4 方法库：每方法带生成规程；Skill4 子agent 按配方产组合 |
| 2 | **变异测试判断言质量**（杀变异体才算断言有效，比"有没有三层断言"硬） | mutation-testing / mutation-test-generator（qaskills）+ coverage-analysis 的 mutation 段（petrkindlmann） | Skill6 评审机器分升级 |
| 3 | **flaky 三件套：诊断/隔离区/检测**——flaky 用例隔离不进波次闸，我们现在没有隔离区概念 | flaky-test-doctor · flaky-test-quarantine · test-flakiness-detection（qaskills）+ test-reliability（petrkindlmann） | §2 阶段三执行：波次闸前加 quarantine 过滤 |
| 4 | **charter 生成器 + 角色化探索视角**（愤怒用户/首次用户模拟=错误推测角色化） | exploratory-test-charter-generator · angry-user-simulator · first-time-user-tester（qaskills） | 场景化探索校正（web/app）的子agent 提示词配方 |
| 5 | **拷问式需求评审**（连续追问直到无[待确认]——比"raise 一个问题"强） | grill-me-qa（fugazi） | 门①需求确认：AI 提问的交互形态 |
| 6 | **回归选择细输入源**（changelog/diff→用例映射，比 release.impact 全表捞细） | regression-test-selection · pr-test-impact-analyzer · changelog-test-mapper（qaskills） | Skill17 回归范围 |
| 7 | **测试债务度量** | test-debt-calculator · test-coverage-gap-finder（qaskills）+ qa-metrics（petrkindlmann） | §7 度量页（后列） |
| 8 | **契约维度方法**（spec 合规/schema 演进/向后兼容） | spec-compliance-testing · api-schema-evolution · api-backward-compatibility（qaskills）+ contract-testing（petrkindlmann） | api 端子agent 配方 + Skill15 矩阵 |
| 9 | **技能路由器**（主agent 第一步=这活调哪个 skill，不硬编码） | codexqa-skill-router（codexqa）+ darwin-skill 的评估→改进→回滚元流程（备参考） | §1 能力包调度层 |
| 10 | **执行隔离方法论**（数据/环境不互染=波次并行的前置条件） | test-hermetic-patterns · test-isolation-strategies（qaskills） | §2 阶段三并发规则 |
| 11 | **ISTQB 教科书口径**（等价类/边界值/判定表/状态图的规范定义，兜底我们方法库措辞） | qa-manual-istqb（fugazi） | §4 方法库正文 |
| 12 | **多 agent QE 编排骨架**（整套舰队：角色/工作流/基准，与调度器直接可比） | agentic-qe（★504，待深挖 packages/benchmarks） | §2 调度器设计对照 |

> 技术栈类（playwright/selenium/appium/cypress 用法 ≈200 个）一律不取，仅当引擎 `page/device` 工具实现参考。

## 10. 出处对照

| 机制 | 来源 |
|---|---|
| 六阶段 18 Skill 及全部输入/输出/规则 | 《18个Skills》原文（方法基底，不重写） |
| **方法/技巧/技术三分**：技术（playwright/appium/pytest/jmeter）=引擎工具层实现，不占 skill 位 | 〔用户〕「我要的是方法skill、技巧skill，不包括技术skill」+ `engine/tools.py` 注册表现状 |
| 主agent设计者/子agent执行 | 原文结语 + 〔用户〕「主agent思维、子agent细化」 |
| 流程顺序（提取→分派→补全→确认→评审→返工） | 〔用户点3〕，Skill 按 §2 编排 |
| 七步链 + KEEP/MODIFY/REJECT/DEFER 四判决 + 机械幻觉门 + 分步选模型 | petrkindlmann `ai-test-generation`（全文精读） |
| 入口五模式 + 阶段闸(run_dir/run-status/close_stage/check_run_gate) + 三端模板 + 增量模式 | codexqa `testcase-generator` V56（全文精读） |
| 需求固定管线 + 唯一缺口登记表 + 可执行P0验证 + 不可测禁P0 + 禁假精确 | codexqa `requirement-analyzer`（全文精读） |
| 拷问式交互协议（一次一问/带推荐答案/10-15问预算/TSDR）+ 反模式红旗12条 | fugazi `grill-me-qa`（全文精读） |
| 方法库四生成器（pairwise/boundary/negative/state-machine 正文） | qaskills 四个 seed skill（全文精读） |
| 对抗式评审/可测性评分/设计规范库 | agentic-qe `six-thinking-hats`·`brutal-honesty-review`·`sherlock-review`·`testability-scoring`·`test-design-techniques` |
| 证据分级(质量门只放行EXECUTED/STATIC) + 上下文预算Blocking态 + 任务→域→子agent映射表 + 「真spawn不许描述」铁律 | agentic-qe 编排内核（kernel/coordination/routing/context + agents/v3 正文，详 skill-library-research R7） |
| 派发分相并行(hard/soft/peer) + 失败四分类熔断不连坐 + 历史耗时均衡切批 + 质量门override四要素 | agentic-qe 协调族（R8：queen/retry-handler/parallel-executor/quality-gate） |
| 评审 2/3 阈值+judge独立性加权+弃权≠通过 + Go/No-Go 双条件门 + 误报率一等信号 + 分诊反馈环 + CRDT 合并语义清单 | agentic-qe swarm/工具链族去分布式化内核（R9：byzantine/quorum/release-manager/code-review-swarm/issue-tracker/crdt） |
| 预算只裁出站不动事实源 + 扇出范围不相交准入 + dispatch-by-reference + ratchet棘轮 + unjudged三态 + 大扇出只能人启 | opencode-swarm/cc10x/CodeJury/swarm-orchestrator（R10 同类项目对照，含"赛道无标杆、结构自定"结论） |
| 子任务三元契约(Agent/Outputs/Gate)全阻断 + planner计划schema + 返工"已绿不破"不变量 + 顾问钩子(先落地再请教/自检≠顾问错/reconcile) + reviewer格式契约(零critical门禁) | agentic-qe 通用开发族（R11：sparc-orchestrator/planner/refinement/executor-preamble/reviewer） |
| 端类型归属用例 | 〔用户点1〕 |
| 产物/评审存储分离 | 〔用户点2〕 |
| 方法库+场景问题库+飞轮 | 〔用户点4〕+ 原文 Skill7/17 沉淀思想 |
| 需求阶段富化 | 〔用户点5〕+ 原文 Skill1 输出契约 |
| 场景化方案选择 | 〔用户点6〕+ Skill16 拆出的技巧（意图驱动/自愈/留证）+ qa-skills `exploratory-testing` SBTM charter |
| 执行波次/并发/资源 | 〔用户点7〕+ 平台既有 rate_limiter/limit_policy/realtime |
| 缺陷链 Skill7/8/9/17/18 | 〔用户点8〕+ 23号文 bug 域承接 |
| 暂存→确认→入库 | 〔用户点5〕「确定之后再入库」 |
| 范围默认版本全量 | 〔用户〕「不应一个个勾」 |

---

## 11. 待拍板（四条均为**推荐**，等你批；v2 重写时本节曾丢失，2026-10-17 补回）

| # | 事项 | 推荐 | 理由 | 状态 |
|---|---|---|---|---|
| 1 | 端归属 | **主agent逐测试点判 surfaces + 门①人改判**；入口只留「本版本只做哪些端」的**收窄配置**（可选，非前提） | 端判定是语义活，正是主agent 的职责；入口全局圈端粒度太粗（同需求下不同测试点触不同端） | 推荐待批 |
| 2 | 历史用例查重 | **起步 `pg_trgm` `similarity()`，不引 pgvector** | 查重第一目标是拦明显重复不是语义召回；中文下 to_tsvector 默认词典分不了 CJK，pg_trgm 自带可用零扩展；假阴性出现再升向量 | 推荐待批 |
| 3 | 接口参数枚举(`design/skeleton.py`) | **留作接口资产维度的「参数覆盖体检」，不删；但停止灌 `mc_test_case`、不再 status=active**（§5 已定形态） | 枚举逻辑没错、错的是定位；它是接口详情唯一的确定性覆盖信号，删了就没有了 | 推荐待批 |
| 4 | 今日代码提交 | **只 add 本轮我动的 8 个文件**（evidence.py/quality __init__/ai_channel/engine tools/测试/24号文/research/README），绝不 `git add -A`（工作区大量 M 是并发会话的） | 代码与设计未决项正交，是干净基线；共享 worktree 不提交有被并发覆盖风险 | **等用户点头才动** |

> 1/2/3 批了即从「推荐待批」改「已定」并同步 §0/§5/§7 相应措辞；第 4 条不批就压着，等 P0 表建齐一起提。

## 14. 落地记录（P0 首件，2026-10-17）

**已落地**：Skill6 的「证据分级 + 文本级反幻觉校验」+ ai_judge 四态裁决——从外部吸收（R9：ADR-105 四级 + evidence-classification 禁用模式）到代码，零 LLM、纯函数、不碰并发区主体。

- `testing_platform/quality/evidence.py`（新）：四级 `executed/static/inferred/claimed`，`GATING_CLASSES={executed,static}`（只这两级过门禁），`validate_evidence_claim()` 机器化 R9 的禁用模式——`EV1` 禁裸百分数置信度 / `EV2` 禁模糊影响面（要具体数）/ `EV3` claimed 禁推测语气 / `EV4` 直证必须带 `file:line`/表+查询 / `EV5` 行号区间过宽 warn / `EV0` 等级必申报。`enforce_three_part_reasoning()` 查推理三段式形状。
- `testing_platform/ai_channel.py`：`_parse_judge` 升**四态** `passed/failed/inconclusive/blocked` + 三段式 + `evidence_class`；旧二态/旧 `$.passed` 键**向后兼容**（`passed` 仍为 bool）。
- `testing_platform/engine/tools.py`：`ai_judge_tool` 的 evidence 带 verdict 四态。
- `testing_platform/quality/__init__.py`：导出面。
- 测试 `tests/test_quality_evidence.py`（14 例，纯函数）：等级归一/门禁语义/四条文本禁用模式/三段式/四态裁决/旧二态兼容/**解析失败落 inconclusive（防假红）**。

**验收**：74 passed（60 既有 + 14 新）+ 57 quality validators + 引入链无环；`passed` 兼容位保 `$.passed eq true` 旧断言不破。

**为何先落这一件**：它是治「ai_judge 工具故障伪装成产品失败（假红）」+「AI 把猜的当测过的」两条历史毛病的根刀，零改造、纯文本规则、可独立上线，且是后续评审一致性协议（2/3 阈值/独立性加权/ratchet）的底座——评审结论都先过这道机器门。**尚未落**：暂存区 `mc_test_proposal`、测试点表 `mc_test_testpoint`、调度器切批/扇出准入/顾问钩子、评审一致性协议——按 P0→P1→P2 推进（§8）。
