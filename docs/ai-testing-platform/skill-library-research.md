# 外部测试技能包调研清单（Skill Library Inventory）

> 2026-10-16。方法=GitHub API 搜索 `claude skills testing / qa skills / test-case generation`，按 star 排序取头部；
> WebSearch/WebFetch 本会话不可用（代理屏蔽），curl+API 可用。用途：给 24 号文（用例生成重构）挑**方法/技巧** skill，
> 技术栈 skill（playwright/selenium/appium 用法类）**不取**（归引擎工具层，见 24 号文 §0）。
> 本文是原始清单+映射，不是设计。

## 库 1：petrkindlmann/qa-skills ★187（50 个，MIT，SKILL.md 标准包，最像"我们该抄的样子"）

**需求/计划**：test-planning · test-strategy · risk-based-testing · shift-left-testing · qa-project-bootstrap · qa-project-context · qa-start · qa-do
**设计/生成**：ai-test-generation（staged pipeline：抽取→风险→覆盖矩阵→场景→**oracle 设计**→代码→人审）· ai-qa-review · coverage-analysis · contract-testing · unit-testing · database-testing · service-virtualization
**用例管理**：test-case-management · test-suite-curation · test-data-management · test-reliability · test-environments · test-migration
**执行/探索**：exploratory-testing（SBTM/charter/HICCUPS）· agentic-browser-testing · selector-drift-recovery（离线批量重写漂移→人审PR）· chaos-engineering · synthetic-monitoring · testing-in-production · ci-cd-integration
**缺陷**：ai-bug-triage · bug-reproduction · quality-postmortem
**专项**：security-testing · compliance-testing · accessibility-testing · performance-testing · mobile-testing · visual-testing · cross-browser-testing · payment-testing · email-testing · analytics-tracking-testing · observability-driven-testing
**度量/报告**：qa-metrics · qa-dashboard · release-readiness · qa-report-humanizer
**技术栈（✗不取，留作引擎实现参考）**：playwright-automation · cypress-automation · ai-system-testing（测产品里的 AI，另一题）

## 库 2：openqa-cn/codexqa ★183（11 个，中文，正好是中文 prompt 范本）

codexqa-requirement-analyzer（需求分析）· codexqa-testcase-generator（用例生成）· codexqa-testdata-generator（测试数据）· codexqa-change-analysis（变更影响）· codexqa-defect-analyzer（缺陷分析）· codexqa-rootcause-analyzer（根因）· codexqa-code-analyzer / code-reviewer / code-wiki · codexqa-jev-browser（浏览器探索执行）· codexqa-skill-router（**多技能路由器**——主agent 调度的现成形态）
> 待办：本会话 raw.githubusercontent 拉正文超时，正文留待下次拉全再对照。

## 库 3：PramodDutta/qaskills ★237（seed-skills **约 470 个**，目录巨大，挑方法类）

**设计技术（我们方法库的直接补强）**：pairwise-test-generator（正交/全对）· boundary-value-generator · negative-test-generator（反向用例生成器）· state-machine-test-generator（状态迁移生成器）· equivalence-class（相关散见）· mutation-testing / mutation-test-generator（**变异测试=断言质量度量**，我们完全没有）· property-based-testing · approval-testing · golden-file-testing
**探索**：session-based-exploratory-testing · exploratory-test-charter-generator（**charter 生成器**）· angry-user-simulator（愤怒用户模拟=错误推测的角色化）· first-time-user-tester · vibe-testing · browser-agent-qa-testing
**计划/策略**：test-strategy-design · test-plan-generation · smoke-test-suite · production-smoke-suite · risk-based-testing · test-architecture-patterns · shift（左移类见散件）
**影响/回归**：test-impact-analysis · regression-test-selection（**回归选择**）· pr-test-impact-analyzer · changelog-test-mapper · api-backward-compatibility · api-schema-evolution
**可靠性**：flaky-test-doctor（**flaky 医生**）· flaky-test-quarantine（**隔离区**）· test-flakiness-detection · retry-reliability-testing · race-condition-finder · concurrent-testing · test-isolation-strategies · test-hermetic-patterns
**需求/规格**：test-case-generator-user-stories · spec-compliance-testing · requirement（散见）
**数据**：test-data-generation · test-data-factory · faker-test-data · test-data-anonymization · secure-test-data-engineer · data-integrity-testing
**API 专项**：openapi-test-generation · api-test-suite-generator · api-contract-validator · contract-testing-pact · graphql/grpc/soap 各型 · api-fuzzing · api-rate-limit-testing
**安全（方法部分）**：owasp-security · auth-bypass-tester · xss-testing-patterns · sql-injection-testing · jwt-security-testing · oauth-security-testing · cors/csp 检测 · llm-security-testing · security-threat-model · nuclei/zap/burp 扫描类（技术）
**度量**：test-metrics-kpis-dashboards · test-debt-calculator（**技术债**）· test-coverage-gap-finder · coverage（多处）· code-coverage
**管理**：testrail-test-management · jira-qa-workflows · allure-reporting · xray-zephyr · quality-gates-ci · release-readiness-checklist
**技术栈（✗不取）**：playwright-*（约 20 个）· selenium-* · cypress-* · appium-mobile · maestro-mobile · espresso/xcuitest/detox/flutter 等——仅作引擎 page/device 工具实现参考

## 库 4：fugazi/test-automation-skills-agents ★252（10 skill + agents + instructions）

qa-manual-istqb（**ISTQB 方法论**：等价类/边界值/判定表/状态图的教科书口径）· qa-investigation（调查流程）· grill-me-qa（**拷问式需求评审**=我们"需求确认门 AI 提问"的现成形态）· api-testing · webapp/selenium/playwright 各型（技术）· a11y 两型

## 库 5：proffesor-for-testing/agentic-qe ★504（Agentic QE Fleet——不止 skill，是整套 QE 编排）

含 agent 舰队/工作流/基准（packages/benchmarks/config/.agentic-qe）。待深挖：它的**多 agent 编排骨架**与我们调度器/主agent 分工直接可比。

## 其他备查
- deanpeters/Product-Manager-Skills ★7422：需求侧技能（PRD→用户故事→验收标准），可做 Skill1 需求拆解的上游参考。
- squirrelscan/skills ★100：网站 QA 300+ 审计规则（web 端专项规则库，security-testing/ai-bug-triage/quality-postmortem/qa-metrics/visual-testing/release-readiness）。
- RayFernando1337/rayfernando-skills ★129：running-bug-review（跑批 bug 复核）。
- alchaincyf/darwin-skill ★6375：**skill 进化系统**（评估→改进→测试→保留/回滚）——我们"两库"的生命周期管理可参考其元流程。

## 对 24 号文的差距增量（相比上轮融合表，这些是**新增没记录的**）
1. **设计技术生成器族**：pairwise / boundary-value / negative / state-machine / mutation——我们的"方法库"不该只存八种方法的说明，应是**可执行的生成器**（输入参数表/状态图→输出组合用例），这四个是现成配方。
2. **变异测试**（mutation-testing）：评"断言质量"的硬手段（不只看有没有三层断言，还要"杀掉变异体"）→ reviewer 机器分的升级。
3. **flaky 三件套**：flaky-test-doctor（诊断）/ flaky-test-quarantine（**隔离区**——flaky 用例隔离不进波次闸，我们没有）/ test-flakiness-detection。执行章节（§2 阶段三）要加。
4. **charter 生成器**（exploratory-test-charter-generator）+ angry-user-simulator / first-time-user-tester：**角色化探索视角**，探索校正的提示词配方。
5. **grill-me-qa 拷问式评审**：需求确认门 AI 提问的具体交互形态（连续追问直到无 [待确认]）。
6. **回归选择**（regression-test-selection + pr-test-impact-analyzer + changelog-test-mapper）：比 release.impact 更细的输入源（changelog/diff→用例映射）。
7. **测试债务**（test-debt-calculator）+ 覆盖缺口定位（test-coverage-gap-finder）：度量层。
8. **spec-compliance-testing / api-schema-evolution / api-backward-compatibility**：契约维度的方法条目。
9. **skill-router**（codexqa）：**技能路由**——主agent 的第一步"这个活调哪个 skill"，我们的能力包需要一个 router 而不是硬编码。
10. **test-hermetic / isolation-strategies**：执行隔离（数据/环境不互染）方法论，波次并行前置条件。

---

# 第二轮深读（正文级，2026-10-16 晚）

已拉全文精读 8 份 SKILL.md（本地 `Temp/skills2/`），以下为**可直接抄进我们流程**的干货。

## R1. petrkindlmann `ai-test-generation` v2.0（全文 19K）——Skill4 的内部七步链
```
Step1 Extract  → 结构化抽取：Entities / Business Rules(编号) / Explicit[REQ-N] / Implicit[IMP-N]
                ★显式与推断分离，IMP 全部待人确认（=事实/猜测三分的操作化）
Step2 Analyze  → 风险表(Risk|Likelihood|Impact|来源REQ) + ★Invariants(恒真式:stock>=0/总额=Σ+税+运)
                + ★Ambiguities(必须显式记下问人，禁止悄悄选一个) + 风险推导的边界用例
Step3 Map      → ★覆盖矩阵(需求→场景→类别→优先级→ORACLE类型)「最重要产物，防漏防重」
                自检4条：每需求≥1正1负 / 每不变式有直接测试 / 每风险有场景 / 无两行测同一事
Step4 Generate → Given/When/Then+测试数据；7类别：happy/boundary/negative/security/a11y/状态迁移/并发
Step5 Oracles  → ★场景与断言刻意分离（先场景后oracle）；4类：UI态/数据(查API/DB)/★反向断言(不该发生)/副作用(埋点/邮件<30s)
                oracle质量：断业务结果不断实现、用最具体断言、必须含反向断言、查数据完整不只查UI
Step6 Code     → 仅当1-5存在后才写；带追溯注释；复用项目Page Object/fixtures
Step7 Review   → 人审11项清单 + ★KEEP/MODIFY/REJECT/DEFER 四判决（暂存区状态机直接用它）
护栏：Step1-3没齐禁止出代码；断结果不断实现；场景先于断言；中间产物永远要出（可简）
幻觉机器门：tsc/pyflakes + grep 选择器/endpoint 对码库核 → MISSING 即打回，不过机器门不给人审；跑挂的先隔离再审
★分步选模型：Step1/3 便宜模型，Step5/6 强模型（与 testing-ai-config-per-purpose 完全对上）
完成判据+★复现元数据：模型ID/输入hash/skill版本 记录在案（提案行 provenance）
前置：.agents/qa-project-context.md 项目上下文文件（技术栈/命名/选择器策略/已知风险区）——每项目该有
```

## R2. codexqa `testcase-generator` V56（全文 14.5K）——入口模式路由 + 阶段闸（我们最缺的骨架）
- **入口五模式**：Plan(只要方案) / Exec(只要用例,须有方案) / 全量(Plan→问一次→Exec) / **提测前改用例** / **提测后增量**(PR/git diff→在冻结基线上补,无基线先bootstrap)——我们完全没有增量模式
- Plan 阶段链：0需求落盘→1术语/流程/规则/功能/测试对象→2影响与风险→3覆盖判定→4对象场景设计→**4-1三轮设计审计(就地改)**→5合成 test_design.md(只整合不新增)
- Exec：端路由(**Web/Server/APP 三端模板**)→按模板分解→覆盖检查→**双写 initialcase/+cases/**→HTML 聚合报告（缺端显式空态）
- ★**确定性阶段闸**：close_stage.py / check_run_gate.py + run-status.json currentStage（**断点续跑**）；gate 拒收标题空壳/占位index/空循环体；**没拿到 ok:true 只能说"草稿"不许说"完成"**
- ★**run_dir 执行工作区**＝我们「暂存区+设计记忆」的落地形态（每 run 一目录，工件按阶段落盘）
- 边界：Plan不写用例/Exec不重做方案；不许裁阶段；缺信息标待澄清/**TBD 禁止编**；只写本地不连平台
- 版本号 V56 = 长期高频打磨件，值得持续跟踪 upstream

## R3. codexqa `requirement-analyzer`（全文 6.4K）——需求门①的固定管线（用户点5"需求阶段补齐"的直接答案）
- 固定链：intake→短输入闸→结构盘点→输入审计→质量/措辞坏味(smells)/NFR→成功指标→行为+UX态完备→追溯与目标对齐→依赖网格→**★唯一一份缺口冲突登记表**(不许再散列多个清单)→风险算法→**★可测性与oracle检查**→阻塞项与下一步
- ★P0/P1 条目必须带**可执行验证**：前置/激励/预期/所需证据/VerifyMethod/RiskClass；P0 还要 FailureMode
- ★**没有可观察失败条件的条目=不可测，禁止写成可执行 P0**
- ★**禁假精确**：不许造 RPN/百分制/六维红绿灯分（分数只当提醒的极端版）
- 源先审计可信度/可比性，陈旧/不可比源禁止标"已对齐"；冲突保两方观点+建议拍板人；事实/角色内容/工作假设/待验假设四分离

## R4. fugazi `grill-me-qa`（全文 15.6K）——门①的人机交互形态
- **一次只问一个问题**；每问必带**推荐答案**（给可接受/可反驳的默认）；**能查库就先查不问**（尊重时间）；按决策树系统走；挑战模糊回答（"看情况"不算答案）；全程记决策，最后出 **TSDR 决策记录**（每维 Resolved/Deferred/Open + 决策日志 + 遗留）
- 问题预算 **10-15 封顶**（背景发现不计）；五维：策略覆盖/框架工具/测试架构(隔离·数据·环境·并行·重试)/AI介入(闸门问法)/CI(质量门·预算·选例·fail-fast·quarantine)
- 反模式红旗清单 12 条（硬等待/脆选择器/顺序依赖/倒金字塔/断实现/无数据策略/每用例全登录/无flaky遥测/覆盖虚荣/超预算不并行/**AI测试未审就合**/flaky被无视）——reviewer 的机器检查项直接抄

## R5. qaskills 四生成器（全文 15-48K，方法库正文）
- **boundary-value**：数字/浮点·货币/日期时间/集合大小/**分页**/**限流**/文件大小——每类给生成规则+可运行实现（比我们 transform_table 细，分页与限流边界是我们没有的轴）
- **negative**：类型违规/边界违规/格式违规/**注入载荷库**/API负向/畸形请求/**鉴权·越权负向**/**并发冲突** 八个内置生成器——与我们 INJECTION_SAMPLES 合并成载荷库
- **state-machine**：状态建模→**从状态图生成路径**→全状态/全迁移覆盖→**守卫条件测试**→**非法迁移测试**→进出动作验证（我们完全没有状态机生成器）
- **pairwise**：生成算法/从需求提参数/**约束建模**/2-way vs 3-way 强度选择
- 定位：这四个=方法库的「可执行生成器」正文样板（输入表→输出组合），不是说明文

## R6. agentic-qe（★504，46 skill 名单，docs/skills.md 已读）——补候选
- `test-design-techniques`（设计规范库正文）· `testability-scoring`（**可测性评分**）· `six-thinking-hats`/`brutal-honesty-review`/`sherlock-review`（**对抗式/多视角评审**——reviewer 升级候选）· `context-driven-testing`（情境驱动+启发式）· `holistic-testing-pact`（象限模型）· `exploratory-testing-advanced` · `regression-testing` · `shift-right-testing` · `bug-reporting-excellence` · `cicd-pipeline-qe-orchestrator`（编排参照）
- 待深挖：它 packages/benchmarks 与 fleet 编排实作（本轮只拿名单）

---

# R7. agentic-qe 编排内核深读（2026-10-16 第三轮，7188 文件 / 170 agent）

读的是实作（`src/kernel·coordination·routing·context·workers·learning` + `.claude/agents/v3/*`），不是 README。结论：**它把"多 agent 测试编排"做成了一个操作系统**——我们不必抄这么重，但有 6 块设计直接可摘。

## 架构分层（它的，不是我们的）
- **Queen Coordinator**（`.claude/agents/v3/qe-queen-coordinator.md`）= 最高编排者。**强制 9 阶段 MCP 协议**：
  ①fleet init ②health check ③查历史模式(semantic/HNSW) ④**任务→域→agent 映射表** ⑤spawn ⑥task submit
  ⑦轮询监控 ⑧收结果 ⑨store learnings。铁律：「**你必须真的调 MCP 工具 spawn，不许描述 agent 会怎么做**」——
  这条正是治"AI 假装在编排"的。
- **Fleet Commander** = 生命周期/扩缩容/负载均衡/健康（跨域）。
- **域协调器 + 专职 agent**（test-generation/coverage-analysis/security-compliance/quality-assessment 四域，每域 2-3 agent）。
- **行为树**（`src/coordination/behavior-tree/qe-trees.ts`）= 声明式工作流编排（非硬编码 if-else）。
- **MCP 工具安全**（`docs/MCP-TOOL-SAFETY.md`）+ 依赖图（`agent-dependency-graph.ts`）。

## ★可摘 6 块（按对我们的价值排序）

### 1. 证据分级纪律（ADR-105）——**最该抄的一块，直接升级我们的幻觉门**
每个 agent 产出的每条结论必须打标签：
- **EXECUTED**：真跑了命令，附命令+输出为工件
- **STATIC**：从数据推导（覆盖率文件/AST/lockfile/schema），点名数据源
- **INFERRED**：无执行的推理，**禁止用"已验证"的口吻呈现**
- **CONJECTURE**：模式匹配/外推，必须自标
**门禁规则：质量门只对 EXECUTED/STATIC 放行；INFERRED 转对抗式复核；CONJECTURE 永不门控。能廉价执行就别推理，执行后升级标签。**
→ 我们的 reviewer 机器门 + ai_judge「工具故障伪装产品失败(假红)」的根治法。落到 `mc_test_proposal.evidence_class`。

### 2. 上下文预算 5 态 + 分层压缩（`context/compaction/`）——升级我们 token_budget.py
按剩余 token 五态：Normal(>25k)/Warning(≤25k)/Pressure(≤18k→Tier1微压缩)/AutoCompact(≤13k→Tier2会话摘要+Tier3 LLM压缩)/Blocking(≤3k→Tier4反应式,**阻塞新工具调用**)。
四档压缩：tier1-microcompact / tier2-session-summary / tier3-llm-compact / tier4-reactive。
→ 我们 token_budget.py 现在只有三态 OK/RECOMMENDED/SPLIT_REQUIRED，补「Blocking=直接不让再起子agent」这一刀。

### 3. 经济路由（`routing/economic-routing.ts`）——分步选模型的量化版
每档(booster/haiku/sonnet/opus)记 avgIn/avgOut token + costPerTask，按 **quality-per-dollar** 打分，
预算约束下产出 cost-adjusted reward 喂神经路由器,让它学会偏好性价比档。
→ 我们 §2「抽取用轻模型/oracle用强模型」的落地：不是硬编码映射，是带成本表+质量反馈的路由。

### 4. 顾问协议（advisor）——主agent 可向上咨询强模型
每个 agent frontmatter 带 `advisor:{model, max_uses:3, redact:strict}`，**在任务分解前、在宣告多agent完成前**
调一次更强模型拿战略指导，最多3次、严格脱敏。
→ 我们主agent 卡壳/收口前的"二次意见"机制。

### 5. 记忆命名空间 + 语义检索（`learning/` + HNSW）——设计记忆+场景问题库的工业形态
每 agent 声明读/写命名空间(`aqe/fleet/*`、`learning/*`)；`memory search --semantic` 走 HNSW 向量。
**learning engine**：dream cycles(离线整理洞察)/causal-verifier(因果校验,防伪相关)/asymmetric-learning。
→ 我们 §4 两库的检索层：问题库=语义索引的 pitfall；设计记忆=run 命名空间。causal-verifier 是飞轮防污染的关键。

### 6. 任务→域→agent 映射表（Queen PHASE 4）——skill-router 的编排版
```
Test generation → test-generation 域 → qe-test-architect, qe-tdd-specialist
Coverage        → coverage-analysis 域 → qe-coverage-specialist, qe-gap-detector
Security        → security-compliance 域 → qe-security-scanner, auditor
Quality gate    → quality-assessment 域 → qe-quality-gate, risk-assessor
Full release    → 上述全部 → 8-12 agent
```
→ 我们调度器派发就是这张表：任务类型→激活哪些 skill 能力包→扇出几个子agent。

## 对我们的取舍（**不照搬**的部分）
- 它的 swarm/共识/CRDT/Raft/Byzantine 协调（219 agent 里一大半是分布式共识玩具）——**单团队测试平台用不上，全略**。
- 它的 n8n/SAP 专用 agent 族——领域绑定，不取。
- 我们要的是它的**编排纪律**（证据分级/预算态/经济路由/顾问/映射表/记忆命名空间），不是它的 agent 数量。

---

# R8. 编排协调族逐agent分析（2026-10-16 第四轮，多 agent 并行调研）

路径勘误：hierarchical/adaptive 在 `.claude/agents/swarm/`；orchestrator-task/memory-coordinator 在 `.claude/agents/templates/`；load-balancer/resource-allocator 在 `.claude/agents/optimization/`。

## ★优先摘 4 个（逐项覆盖调度器五要素）

### 1. qe-queen-coordinator — hard/soft/peer 三类依赖 + 分相并行
- "Phase 1: Agents with no unsatisfied hard deps … Phase 2: Agents whose hard deps completed in Phase 1"
- **Soft deps: never delay for them**（软依赖永不等待）· **Missing deps: log advisory warning and proceed**（缺依赖只告警继续）
- → 我们的派发算法：按「测试点×端」的子agent 依赖分相派发，软依赖不阻塞，缺依赖降级告警不卡流水线。

### 2. qe-retry-handler — 失败不连坐的完整实现（我们最缺的一块）
- **失败四分类**：transient(网络)/deterministic(逻辑)/resource(资源)/flaky → 先分类再决定重不重
- 策略表：网络→指数退避×3；DB→线性×2；**断言失败→不重试**（重跑也是错）；限流→60s×5；flaky→抖动×2
- **Retry Budget 整轮封顶**：防重试成本失控
- **熔断示例**：同依赖 30s 内第 5 次失败→开断→**SKIP 该依赖剩余用例但继续非依赖用例**
- → 我们的错误策略 on_error：按失败类分派 retry/skip/abort；熔断只隔离坏依赖不连坐。

### 3. qe-quality-gate + fleet-commander — 事实证据才拦门 + 带审计的 override
- "Quality gates block only on EXECUTED/STATIC; INFERRED routes to adversarial verification; CONJECTURE never gates"
- **override 必须带**：人/条件/过期时间/审计落库（四要素缺一不可）
- hotfix 走 Minimal viable 快通道（分级门：Commit/PR/Release/Hotfix）
- → 我们的确认门：机械门只被事实证据拦；override=我们的人工确认，四要素写进 change_record。

### 4. qe-parallel-executor — 历史耗时均衡切批（不是按数量切）
- **time-balanced**：按历史耗时切批（不是按文件数/条数）；执行前必读 timing-history、后必回写
- **straggler 动态再分配**（慢的动态调走）
- 每 worker 资源隔离；并发 1-16 + **增速衰减表**（8→7.2x/90%，16→13.8x/86%）——**并行不是越高越好，有收益递减点**
- → 我们的切批器：按端/按历史耗时切，回写 timing 供下次切批；设并发上限与减速阈值。

## 部分有用的

| agent | 摘什么 | 不适用部分 |
|---|---|---|
| qe-fleet-commander | 健康三档阈值表（队列<10/10-50/>50，错误率<1/1-5/>5%）+ 超阈值免确认 + **5min 冷却** | 扩缩容自动化 |
| orchestrator-task | 四策略(Par/Seq/Adaptive/Balanced) + **"只把真依赖设串行"** + 反模式「过细分解=协调开销」 | — |
| hierarchical-coordinator | 4 步分派(能力过滤→历史表现→负载→选优，0.5/0.3/0.2 加权) + 升级阈值「<70%成功率或>2x时长即重派」+ YAML pre/post hooks | 后半段 hyperbolic attention/GraphRoPE 是 `Math.random()` 假嵌入，无价值 |
| adaptive-coordinator | 量化拓扑决策矩阵 + **切换前快照+20%增益门槛** + 回滚触发器(性能退化0.25/错误率+0.15) | 拓扑运行时自适应/RL/神经网络 |
| load-balancer | 四级优先级权重(critical 0.4/high 0.3/normal 0.2/low 0.1) + **优先级老化防饿死** + EDF 准入 Σ(exec/period)≤1.0 + 熔断三态 | work-stealing/CFS/多worker 迁移 |
| memory-coordinator | **四层记忆 TTL** Global→Project→Session→Task；命名空间=TTL（swarm 24h/agents 1h/tasks 4h/patterns 7d/decisions 30d/notifications 5m） | CRDT/向量时钟 |

## 确实不适用的
mesh-coordinator / quorum-manager / resource-allocator / collective-intelligence-coordinator / adaptive 的 ML 段——纯分布式共识、CRDT、ML 预测扩容。单团队同进程无多资源池；Bulkhead 思想我们用 limit_policy 更贴切。

## 修正我上一轮的判断
我上轮把 swarm/分布式族整批判「不适用」——**错了**。去掉分布式假设后，byzantine→多 agent 交叉验证、quorum→多数决 这些方法内核仍有参考价值（详见 R9 swarm 族专项分析）。正确做法是「去分布式化后看方法内核」，不是整批否。

---

# R9. Swarm/分布式/工具链族逐agent验证（2026-10-16 第四轮）

**总判断：原「没用」不成立——10 个里 4 个确凿有用、2 个部分有用、3 个确实不适用**（真无用的只有 swarm.md 云产品 shim、consensus 主体的矩阵求解、以及各文件 GNN/FlashAttention 装饰层）。
路径勘误：真实文件在子目录（consensus/、github/、swarm/、sublinear/、templates/、v3/helpers/quality-criteria/）。

## ★最高优先 4 件

### 1. evidence-classification（零分布式内容，纯反幻觉规范，性价比最高）
- **三级证据逐级义务**：Direct 必带 `file_path:line_range`（窄，10-15 行）并列原文；Inferred 必展推理链；**Claimed 是一等公民非失败态**——"Must state requires verification… Must NOT speculate about what could/might happen"
- **禁用模式可做校验器**：禁裸百分数（"禁止 85% confident"）、禁模糊影响面（"affects 19 agents" 非 "affects many"）、Claimed 里禁推测（非 "could be X or Y"）、禁关键词匹配声称
- **推理三段式**：`{代码做什么} → {对质量意味什么} → {可能出什么错}` → 直接作 ai_judge 输出契约
- **负证据留痕**：`N/A (verified via Glob/Grep search) - NOT: <pattern>` → Skill3 覆盖地图的「不适用」格必须引用据以判定的检查
- → 落 `quality/validators.py`（几十行文本级校验）+ ai_judge 契约；把 24号文「事实/假设/待验四分离」细化成**机器可查规则**。零改造。

### 2. byzantine-coordinator（文件最小内核最纯）
- `f < n/3` → LLM judge 的故障是「**自信胡说**」(Byzantine) 非宕机，**阈值取 2/3 而非简单多数**（一个高置信错答在 1/2 规则下能翻盘）
- 三阶段 → **独立首评→互看分歧必回应→commit**，分歧被制度性前置
- view change → **主判者卡死换人**，不挂死 run
- 恶意检测=离群剔除（mean±2σ，实现在 mesh-coordinator）
- → 我们多 agent 交叉验证协议骨架；未达 2/3 一致转人工，**不取多数**。

### 3. quorum-manager
- **独立域得分压过一切**："Independence score (nodes in different failure domains get higher scores) +40"（四因子 independence*40/reliability*30/diversity*20/recovery*10）
  → **选 3 个 judge 绝不用同模型三拷贝**——相关失效会造出假自信；应取不同模型/提示词/证据源
- **多驱动取严**：`Math.max(byzantineMinimum, partitionMinimum)` → 测试策略轮次分配从改动面/历史缺陷密度/风险矩阵/时间预算四路独立推算**取 max**（对应 20 号文「策略从改动面推导」）
- **变更事务化**：validate→plan→execute→verify→rollback → env.deployed_version_id 指针切换/限流策略改走此骨架，非裸 UPDATE

### 4. release-manager
- **Go/No-Go 双条件门**：`consensus==='go' && confidence > 0.90` —— **共识 go 但置信不够照样拦**；stopped 输出聚合理由 → 报告 verdict/上线判定就该长这样（与 20 号文最紧）
- **回滚触发器预声明且分栏**：automatic:[failed_tests, build_failures] / manual:[user_reported, perf_degradation] = 事故前先写「谁自动回滚、谁必人拍」
- **成功定义**：`success: !rollbackOccurred && incidents.length === 0` = 没回滚且没引发事故才算成功（复盘采纳此口径）

## 有用（次优先）

| agent | 去分布化内核 | 落到我们哪 |
|---|---|---|
| **crdt-synchronizer**（25KB 唯一有真实现） | 并发写合并语义清单：GCounter 逐项 max+按源分键 / OR-Set **add-wins**（≥1 非墓碑 tag 即在）/ LWW **双键**(updated_at,id)（只比 updated_at 静默分叉）/ delta+水位线 / optimisticRead vs strongRead | 当**合并语义设计清单**不引库——直接治我们已知 bug（`limit-policy-zeroed-by-reconcile` 限流被对账清零、`model-group-partial-put` 单组 PUT 回源） |
| **code-review-swarm** | ①**按维度分片评审**（security/perf/style/arch/a11y 各一 agent，各出 findings+confidence 再聚合）= Skill6 五维评分卡该到的形态（单轴打分避免晕轮）；②**误报率是一等信号**：`falsePositives/issuesFound < 0.15` 成功率定义成误报率低；③**失败记忆**：从历史评审失败中学 | Skill6 评审 + ai_judge 契约（平台现无误报记账） |
| **issue-tracker** | ①**分诊反馈环**：`correctPriority: assignedPriority === actualPriority` = AI 分诊拿人工改判当标准答案记分 → defects/triage.py 补 ai_severity vs final_severity 一致率；②**创建前查重** `threshold:0.85` 对 open 缺陷相似检索压重复单 | 23 号文缺陷域两个现成缺口 |
| **mesh-coordinator** | 唯一值得直接摘代码：`detectByzantineNodes` 的 mean±2σ 离群剔除（byzantine 内核的落地实现） | 交叉验证的离群剔除 |

## 部分有用
- **consensus-coordinator**：只捞一条「按历史准确率给 judge 加权」（PageRank），quorum 有更朴素版，二取一；矩阵求解是玄学，区块链段纯噪声。
- **github-pr-manager**：状态机形状平台已有；可取 draft 语义（草稿不可批只可评）+ **hotfix 快车道**（只跑关键用例+override 留痕+**回灌版本基线**，补 19 号文继承缺口）+ 合并策略从历史学。

## 确实不适用
`swarm.md`（实为 flow-nexus/swarm.md，云产品 API shim 无实现/唯一内容是一句拓扑口号）· consensus 主体的 Neumann 矩阵求解 · 各文件的 GNN/FlashAttention/RL 装饰层。

## ★系统性发现（重要，防止被它带偏）
1. **四个文件的「自学习」frontmatter 逐字相同**（ReasoningBank/GNN/FlashAttention 标签），反复出现无出处的「+12.4% accuracy」「2.49x-7.47x faster」；三个协调器里 `outputsToEmbeddings` 竟是 `Array.from({length:384},()=>Math.random())`——**「注意力共识」跑在随机向量上，是装饰性伪代码**。→ 吸收方法（分片/置信门控/失败记忆），**永不引用其数字**；「attention consensus better than voting」无任何论证，落地就用朴素多数或 2-of-3。
2. **路径不符**：平铺 `.claude/agents/*.md` 全 404，真实在子目录；evidence-classification 仓内存三份拷贝。引用须以 git tree 为准。

---

# R10. 同类开源项目对照（2026-10-16 第四轮，10 组关键词 × 22 仓深读）

## ★方法学结论（最重要）
**这个细分赛道没有成熟高星开源标杆**——搜索头部命中几乎全是渗透测试/通用编码流水线；真正做「测试用例生成编排」的集中在 <30★ 尾部仓。
**市面上没有第二个「代码调度器切批+token预算 → 主agent提取测试点 → 测试点×端扇出 → 暂存 → 人审 → 入库」的完整实现。**
最接近的 `XuePengJu/ai-testflow`（0★，链路同构但无预算/无扇出准入/无防灌水棘轮）。→ **我们的结构自己已经定对了，缺的是把 8 条成熟纪律补进调度器**（见下）。

## 值得看的 5 个仓
1. **ZaxbyHub/opencode-swarm**（503★/MIT/TS，活跃）——「上下文预算」的生产级形态：Budget Guard 0.7 advisory/0.9 critical 双阈值，**只裁出站请求、掩掉已完成的老工具输出，持久历史与磁盘工件永不动**；窗口按活模型真实上限解析；预算告警只发给 architect。并行准入=**文件范围可证明两两不相交，否则自动退回串行（fail-closed）**；每 worker 独立 worktree，收尾 squash 成**待审暂存改动**；Epic（大扇出）**只能人开启，agent 开不了**。
2. **romiluz13/cc10x**（164★/MIT）——工程纪律最全：**Dispatch by reference, not by blob**（正文落盘、提示词只传路径；自陈一次真实派发 42k 字符 99% 是粘贴历史）；工作流产物=唯一事实源（`workflows/{wf}.json` + append-only 事件流，**survives compaction**，上下文压缩后读盘恢复）；**plan-gap-reviewer 在任何东西建出来之前先审计划**；**anti-anchored review**（评审者从没见过规划者的理由）；Test Honesty Gates（grep mock/`as any`/`setTimeout` 等待，命中不能算 PASS；置信≥80 必须带逐字 `file:line` 否则自动降级，verifier 独立重读）。
3. **krishagarwal314/CodeJury**（148★/MIT）——评审闸门最佳设计：默认 2 位**不同供应商**评审、**各拿一半职责、互不见对方意见、必须一致**才放行；**弃权（超时/限流/解析失败）=INCONCLUSIVE 而非通过**；没有仲裁者（「arbiter 只是另一个能把正确结论劝掉的模型」）；切批粒度**由下游模型单请求上限反推**，截断落行边界并**自我标注**；评审只花总成本的 3%——**闸门可以奢侈，生成必须节制**。
4. **XuePengJu/ai-testflow**（0★/中文）——唯一逐段同构：`Parser→Generator→Reviewer→Exporter` + **测试点×角色双层循环、跨角色合并去重** + 全局队列/5 Worker 池/重启自动恢复/断点续跑 + 实时子进度「第 N/M 个测试点·已生成 X 条」+「先沟通、点生成才真正生成」（人启闸门）。缺：预算/扇出准入/棘轮。
5. **Armur-Ai/Pentest-Swarm-AI**（2992★/AGPL 只读设计不引码）——唯一解决「**运行中的新发现如何折回**」：黑板+信号素衰减+每 agent 自带触发条件。它批评的「pipeline 顺序、发现折不回、被最慢阶段卡死」正是我们单向链路的已知短板。**受控版抄法：子agent 可向暂存区提交「候选测试点」，由代码调度器决定是否新开一批——绝不让 agent 自己递归展开**；`--verify` 用**非 LLM oracle**（带外回调/差分/时序），结论**优先于模型自述**。

次选：`ChanningLua/prax-agent`（272★，口号 **"HITL at decisions. AFK in execution. Evidence at every boundary."**；task.json 把显式 scope/累积预算/失败分类/pause/guarded resume 做成一等字段；blocker 回到人而非悄悄扩任务）· `moonrunnerkc/swarm-orchestrator`（111★，**ratchet 棘轮=重试不得用删测试/删断言/降覆盖率换绿灯**；outcome 三态含 **unjudged**；agent 自己的总结永远不是事实源）· `rcortx/kiwiq`（2181★，图 schema→LangGraph→Prefect+HITL 暂停+WebSocket 进度，只抄 schema/节点/HITL 不抄栈）· `a-saven/agent-skills`（2★，DAG **按节点路由到满足验收标准的最便宜模型档**；工程师不写测试/测试作者不写代码/评审者只报告——职责分离是链能安全自动化的原因）· `iAviPro/testteller-agent`（28★，评审过的高质量产出**回灌成 few-shot**）· `trace-autogen-engine`（2★，**"不让 AI 猜，让它复刻我们已有质量标准"**——评审过的历史用例当风格锚+先查重）· `saiGou-14H/qa-test-orchestrator`（1★中文，按**真实运行态的责任人/端/业务域**拆分归档、目录即键，拒绝固定岗位模板）· `vora-co/multi-agent-harness`（8★，依赖图预解析注入 Leader 上下文；checkpoint 续跑+卡死回 pending 写 recovery_note；**便宜的静态检查排在最贵的执行之前**；版本化 pydantic schema `extra="forbid"`）。

## 编排形态六型（分类产出）
A 代码调度器+固定流水线（≈我们）/ B 扇出-汇聚 / C 闸门陪审 / D 黑板涌现（**不抄**：与代码定批次冲突、不可预算不可复现）/ E 工作流引擎（kiwiq 重栈不抄部署只抄三件事）/ F 执行期自愈（与生成层正交）。

## 要补进我们调度器的 8 条纪律（本调研的最终产出）
1. **预算只作用出站视图，不动事实源**（opencode-swarm）
2. **切批粒度由下游模型单请求上限反推；截断落行边界并自我标注**（CodeJury）
3. **dispatch-by-reference**：扇出内容落盘、提示词只传路径（cc10x）
4. **扇出准入=范围可证明不相交否则退串行（fail-closed）；大扇出只能人开启**（opencode-swarm）
5. **弃权≠通过（=INCONCLUSIVE）+ 跨供应商分半职责的一致性评审**（CodeJury，呼应 R9 的 2/3 阈值与独立性加权）
6. **ratchet 棘轮**：返工重试不得用删断言/降标准换绿灯；评审前先跑 plan-gap（cc10x/swarm-orchestrator）
7. **outcome 三态含 unjudged**：裁决不了就明说没裁决，agent 的总结不是事实源（swarm-orchestrator）
8. **入库回灌闭环**：评审过的高质量用例当 few-shot 风格锚 + 新需求先查重后仿写（testteller/trace-autogen）

补：中途发现折回的受控版（候选测试点→暂存区→调度器决定开新批）+ 非 LLM oracle 优先于模型自述（Pentest-Swarm-AI）。

---

# R11. 通用开发/工作流族逐agent分析（2026-10-16 第四轮，全文见 Temp/skills2/analysis-general-dev.md，10 agent 全覆盖）

**去伪判断（同 R9 系统性发现）**：A 类=可摘思维框架（Planning/Review Process、产物契约、门禁表）；B 类=营销伪代码（V3 Self-Learning/GNN/HNSW/FlashAttention/EWC++/SONA，占篇幅 50%+，虚构 API、无据 +12.4%）——只取 B 类背后的抽象意图「执行前检索相似失败、执行后回写带 reward+critique 的轨迹」，不抄任何 API。
路径勘误：仓库已重组——core/（coder/planner/reviewer/researcher）、sparc/（refinement/specification/pseudocode）、flow-nexus/（workflow）、_shared/（executor-preamble）、v3/（sparc-orchestrator）。

## ★五份真有"思维框架"的货

### 1. executor-preamble（顾问协议 ADR-092）——**最新颖，整段吸收**
给任何执行 agent 装上「执行者↔顾问」二元结构（执行者可请教更强模型）：
- **先落地再请教**（durability-before-completion）："make your deliverable durable first: write the file… The advisor call takes time; if the session ends during it, a durable result persists and an unwritten one does not."
- **orientation vs substantive 分界**：找文件/取数据=定向（自己干）；开始写/下结论/建在假设上=实质工作（先问顾问）
- **杀手句（对 AI 自称通过是一记解毒）**："A passing self-test is not evidence the advice is wrong — it is evidence your test does not check what the advice is checking."
- **冲突不偷换**：已取数据指 A、顾问指 B → 不静默倒戈，再发一次 reconcile（"I found X, you suggest Y, which constraint breaks the tie?"）
- **预算**：每任务 3 call/每会话 10 硬顶；超限不重试，无顾问继续
- → dsh 子agent→更强 reviewer 模型的顾问钩子，整套协议照搬。

### 2. sparc-orchestrator——与"主agent统筹"最同构
- **每阶段三元契约**：Agent（谁做）/ Outputs（交付物）/ Quality Gate（过闸标准），例：Specification→Gate: All requirements testable, no ambiguity
- **门禁表全 Blocking=Yes**：五道闸全部阻断，无"软通过"，未过闸不进下一阶段
- **run full vs run <phase> 双模式**：整链跑 or 单环重跑（=我们"一键全量 vs 只重跑某批"）
- ReasoningBank 四抽象意图（不抄 API）：按项目类型重排阶段权重/据相似项目预判问题埋防——主agent 的元规则

### 3. planner——计划 schema
- 产物契约 YAML：tasks[{id, description, agent, dependencies[], estimated_time, priority}] + critical_path + risks + success_criteria
- 硬约束："Ensure each task has clear inputs and outputs"（没输入输出不算拆完）/"Build in validation checkpoints"（计划内嵌校验点，非最后一次验）/"Update plans based on execution feedback"（活文档）
- 反完美主义："A good plan executed now is better than a perfect plan executed never"

### 4. reviewer——评审格式契约
- 输出 schema：Strengths / Critical Issues（每条带 Impact+Fix+行号）/ Suggestions / Metrics / Action Items
- **成功=零 critical 硬门禁**；严重度阶梯 Critical→Major→Minor→Suggestion
- **上下文调权**：同一问题按开发阶段/时间约束/技术债可降级，防一刀切误报
- **<400 行分批评审**（大块必拆）

### 5. refinement——迭代不变量
- Red-Green-Refactor：**"每轮改进且已绿测试必须保持全绿"**（我们子agent 返工的安全绳——返工不许破坏既有通过项）
- **baseline-vs-after**：改进必须量化为前后差，不是声称
- retry 指数退避 + CircuitBreaker 三态（CLOSED/OPEN/HALF_OPEN）——执行引擎抖动处理现成模式

## 次级碎片
- **coder**："先澄清歧义再动手"+"先定接口契约"+"成功=测试真过（非 LLM 自称）"三条纪律进子agent 提示词
- **specification**：acceptance_criteria+use_case 四段式（**exceptions=现成反向用例**）+Gherkin AC+8 项 Validation Checklist+可测性是一等评分——需求→测试资产的派生契约
- **researcher**：gaps_identified[{area, impact, suggestion}] 契约+"researcher 给 tester 喂边界用例"链路
- **pseudocode**：规格→实现之间显式加"设计中间层"（先蓝图后实现的纪律）
- **workflow**：编排模式清单（Multi-Stage Review/Conditional）+步骤三元组(id,action,agent)+运行中 step injection+audit-trail 是质量标准

## 四份调研合成：调度器总吸收表（R8-R11 交集优先级）
| 层 | 吸收组合 | 一句话 |
|---|---|---|
| 派发算法 | R8 queen 三类依赖分相并行 + R10 范围不相交准入 | 硬依赖分相、软依赖不等待、范围证不相交否则退串行 |
| 切批 | R8 历史耗时均衡 + R10 下游模型上限反推 | 双约束切批，timing 回写，截断自标 |
| 预算 | R7 五态+Blocking + R10 出站视图裁剪 + R8 重试预算封顶 | 预算作用出站不动事实源；耗尽阻塞新扇出 |
| 失败处理 | R8 失败四分类+熔断不连坐 + R11 已绿不破不变量 | 分类退避、断言不重试、返工不破既有通过 |
| 评审协议 | R9 2/3+独立性加权 + R10 异供应商分半职责+弃权≠通过 + R11 零critical门禁+上下文调权 | 三源同指一个设计：独立、多元、弃权明示 |
| 顾问钩子 | R11 executor-preamble 整段 + R7 advisor | 先落地再请教/冲突reconcile/预算3次 |
| 反灌水 | R10 ratchet 棘轮+unjudged + R9 evidence-classification 校验器 + R7 证据分级 | 删断言换绿灯禁止；待验证一等态；门只认 EXECUTED/STATIC |
| 计划契约 | R11 sparc 三元契约+planner schema | 每子任务带交付物schema+过闸标准，全阻断门禁 |
| 发现折回 | R10 受控版候选测试点 | 子agent可提名，调度器独裁开新批 |
| 入库回灌 | R10 few-shot 风格锚 | 评审过的用例回灌，先查重后仿写 |
