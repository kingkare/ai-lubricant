# 25 号文设计评审记录（三个评审 agent 原文，2026-10-17）

> 评审对象：`plans/25-case-generation-complete-design.md`（第一版）。
> 三个评审视角：①数据模型与 UI/流程一致性 ②流程完整性 ③对照 18 Skills 查缺。
> 合计 31+ 条缺陷，已确认最硬三条：需求评审整块缺失、18 Skill 仅 4 个真落位、UI 徽章无数据源。
> 25 号文将按本清单重写（重写方案见对话记录）。

---

# 评审一 · 数据模型与 UI/流程一致性（15 条）

1. **需求节点徽章无数据源** — run.status 是 run 级，requirement 表只有 analysis.status，没有 per-requirement 生命周期字段（未开始/分析中/门①待确认/生成中/门②待确认/已完成）。补：mc_test_requirement 加 gen_status 列（或 run×requirement 关联表），徽章从它渲染。
2. **TP-02 [●评审中] 徽章无字段** — testpoint.status（candidate→confirmed→generated）没有 reviewing 态；评审是 run 级状态，得不出"这个测试点正在评审"。补：testpoint 加 case_review_state，或定义从其下属 case proposal 评审态聚合的口径。
3. **proposal.status 枚举撑不起评审回路** — 评审三道闸中间态（机器打回/双评中/双评分歧/<70 重写中）无落点；拒绝原因无字段。补：proposal 加 review_state + reject_reason + review_round。
4. **proposal 缺 testpoint_id 列** — case proposal 必须挂测试点，现只能从 payload JSONB 挖。补：加 testpoint_id 实列。
5. **"3/4 端完成"子进度无计算口径** — testpoint.surfaces 可多标、proposal.surface 单值，缺"该测试点在端 X 是否已有终态 proposal"聚合定义；门①改端后与已产 case 归属冲突未处理。补：per-testpoint×surface 计数视图；门①确认后 surfaces 冻结，门②发现孤儿 case 自动 reject 或退回门①。
6. **覆盖热力图空格无落库** — 门①侧栏要显示具体空格且空位带语义名；"标不适用必填验证依据"无字段。coverage_axis 写成单值，一点多轴算错。补：新表 mc_test_coverage_cell（requirement_id/axis/surface/state: lit|gap|na/evidence/testpoint_id），coverage_axis 明确为数组。
7. **查重相似度无处存** — "与TC-0031相似87%"无数据源。补：proposal 加 dup_of_case_id + dup_score + dup_verdict。
8. **双评意见无处落** — 两位评审意见原文、一致/分歧、弃权标记无表无列，分歧仲裁流程没写。补：mc_test_review 表（proposal_id/reviewer/opinion/verdict/abstain）或 reviews JSONB + consensus 字段。
9. **AI 问题队列无实体** — 问题只活在 requirement.analysis JSONB 的 register[]，跨需求聚合要扫 N 个 JSONB；"推荐答案"字段没有；答案回填联动链缺失。补：问题入正式问题表（question_id/requirement_id/claim/recommended_answer/status/answer），register 只存引用。
10. **testpoint.status 的 candidate 态悬空** — 门①"确认后复制进正式表"则行诞生即 confirmed，candidate 永不出现；generated 置位时机没定义。补：明确行创建时机与两态置位条件（generated=全部标注端均有 confirmed case）。
11. **defer 判决无恢复路径** — defer 何时复活、按什么触发没写。补：defer 记 reason + 关联 question_id，问题回答事件自动重新入队。
12. **run 全局状态机 vs 按需求并行错位** — REQ-001 过门①、REQ-002 没过时 run.status 算什么？两门放行粒度未定义。补：per-requirement stage 或定义 run.status=各需求最小进度。
13. **重写棘轮缺版本链** — "断言比上一版少拒收"需要上一版可寻址：proposal 无 version/rewrite_of；重写是新行还是覆盖同 slot_key 行未定义。补：加 rewrite_of+version，重写落新行引用父，slot_key 允许带版本多行。
14. **执行结果四态与 traceId 无字段** — 执行结果表未加 result_state(四态)+fail_class+trace_id；bug 关联列（case_id/execution_result_id/trace_id）未说明。
15. **门② REJECT 回路对已完成 run 无机制** — run 可能已 done，重开哪个 run、testpoint 是否回退、已 import 场景怎么办没写。补：定义 defect→门② 重开机制。

次要点：用例节点徽章只有"写/完"两态；"整组保留按钮前置"无数据定义；progress.requirements.confirmed_points 与 progress.testpoints.confirmed 语义重复。

---

# 评审二 · 流程完整性（16 条，按严重度）

**1.【致命】缺独立「需求评审」环节 + 人确认门** — 流程1只叫"需求分析"，缺①独立评审报告产物②human confirmation 环节（status passed|failed 是机器判）③拷问式交互。补：拆出「需求评审」，加门⓪＝思考模型按规程评审需求→出独立评审报告→人确认（AI 可追问）→过了才进提点。铁律改"三个停顿点"。
**2.【致命】门①被改名"测试点确认"，丢掉用户"需求确认"语义与 AI 双向问询** — 用户原话叫"需求确认（ai 可以在这个阶段进行问题咨询）"，25号文缩成"测试点确认"，拷问式协议（一次一问/带推荐答案/挑战模糊/10-15问预算/TSDR）整体消失。补：门①恢复命名"需求确认"，搬回 24 号文 L127-130 全套协议。
**3.【高】Skill5（PRD转XMind）零落位** — 补：流程1 后加导出/可视化环节或思维导图承接点名。
**4.【高】Skill10（SQL安全红线）零落位** — 补：引擎 sql 步骤执行前闸（无WHERE/敏感字段/缺LIMIT→P0 拒跑）。
**5.【高】Skill12（测试数据生成）零落位** — 执行时数据集从哪来无定义。补：用例细化接 Skill12→mc_test_dataset。
**6.【高】Skill15（接口测试矩阵）零落位** — "Token必覆盖"未点名、"接口资产维度"承接消失。补：api 配方点名 Skill15。
**7.【高】Skill16 三技巧（意图驱动/自愈优先/每步留证）未点名** — 补：流程6 web/app 段列明为探索校正纪律。
**8.【高】状态机与第四部分不自洽** — 状态机缺覆盖补全/场景化/执行/缺陷/回归/上线/报告/沉淀任一状态，返工回路和 REJECT 回退无边。补：状态机补全 + 回边。
**9.【高】铁律3"只有两个人工停顿点"与用户要求冲突** — 用户要三个门。补：改三个停顿点（需求评审/测试点确认/用例确认）。
**10.【中】承诺的"六项"里「输入输出」全线缺失** — 12 个流程无一处显式列出吃什么产什么。补：每流程补输入/输出行。
**11.【中】流程5/6/7/8/9 + 门①② 六项结构塌方** — 只剩三两行。补：逐流程补六项。
**12.【中】流程3覆盖补全缺数据结构/UI，门①②缺状态流转定义** — 覆盖矩阵写哪、复制动作幂等键未定义。
**13.【中】黑话残留** — "五域/生成域/执行域/缺陷域""沉淀飞轮/飞轮"。补：换直白词（环节/问题库回流）。
**14.【中】指挥中心/子agent 分工只落一半** — 无派发/回收/通知协议、决策回合内容。补：单列"指挥中心决策回合/派发协议/回收协议"。
**15.【中】"测试点分派及生成"顺序歧义** — "生成"指测试点候选还是用例需明确；"分派"（端标注）应单列。
**16.【低】流程0需求入口缺数据结构与 UI** — L1 落库动作、入口页过目交互未写。

一句话结论：三个致命坑——①需求评审被降级成机器判字段；②"需求确认"被改名"测试点确认"并抽干拷问式问询；③状态机只画到生成结束。补的主线：流程1后加「门⓪需求评审」、门①恢复"需求确认"语义、状态机扩全、补齐 Skill5/10/12/15 与六项结构。

---

# 评审三 · 对照 18 Skills 查缺

| # | Skill | 落位 | 程度 | 缺什么 |
|---|---|---|---|---|
| 1 | 需求拆解 | ✅ 流程1 | 完整 | 九步正文只按名引用未抄规程 |
| 2 | 测试点提取 | ✅ 流程2+表 | 完整 | 唯一真完整落位 |
| 3 | 场景补全 | ✅ 流程3 | 完整 | 三态齐 |
| 4 | 用例生成 | ⚠️ 流程4 | 部分 | 六策略未点名、1正≥3异未落 |
| 5 | PRD转XMind | ❌ | 零落位 | 无导出、无"步骤/预期"叶层 |
| 6 | 用例质量评审 | ✅ 流程5 | 完整 | 五维+阈值+回派+棘轮齐 |
| 7 | Bug分析 | ⚠️ 一句 | 只留名字 | 八步内容/禁跳步/断根因判定全无 |
| 8 | 日志诊断 | ⚠️ 一句 | 只留名字 | 跨服务/代码定位规程全无 |
| 9 | 缺陷规范化 | ⚠️ 半句 | 近零 | 强制字段清单未落 |
| 10 | SQL安全 | ❌ | 零落位 | 无 sql 步骤前闸 |
| 11 | SQL性能 | ❌ | 零落位 | 全无 |
| 12 | 测试数据生成 | ❌ | 零落位 | 执行期数据断源，mc_test_dataset 未进 |
| 13 | Pytest | ❌ | 显式不做 | 24号文判✗导出适配器可选 |
| 14 | JMeter | ❌ | 显式不做 | 性能预留 |
| 15 | 接口测试矩阵 | ⚠️ | 部分 | 四层断言✅但 Token必覆盖未点名 |
| 16 | Playwright | ⚠️ | 部分 | 三技巧零出现 |
| 17 | 回归范围 | ⚠️ 一句 | 薄 | diff 来源/impact 算法未定义 |
| 18 | 测试报告 | ⚠️ 一句 | 薄 | 字段/模板未重述 |

**专项**：需求拆解≠需求评审——"需求评审"（AI 评需求质量→冒泡给人→人确认才过关）被机器判 gate 掉且被铁律3砍；19号文本有 req_review 态（无 open blocking question 才能转 req_confirmed）、21号文两段式，25 号文未继承。
**结构性发现**：自称"18 Skills 是基底"但只有 4 个完整落位，其余是宣言不是可执行清单。
**最缺 5 个**：①需求评审门 ②Skill12 测试数据 ③Skill10/11 SQL ④Skill5 XMind ⑤Skill7/8/9 缺陷三件套正文。

---

# E2E 实测修正记录（2026-10-19，真模型全链跑通后回填）

前半段（分析→门⓪→提点，15 测试点）与后半段（门①→用例 21 条→三道闸→回炉→入库 29 条→幂等）
均用真模型走通。实测暴露 5 处设计/实现偏差，修正如下（全部有回归测试钉住）：

| # | 缺陷 | 根因 | 修正 | 测试钉 |
|---|---|---|---|---|
| 1 | **EV4 逐字符叶索证 → AI 用例近乎全灭**（打回率 43.8%→60.9%，100% 根因 EV4） | `check_evidence` 对 `expected` 每个字符串叶跑 EV4（要求 file:line）——expected 是断言期望值，来源是需求不是代码行 | 纪律按**语义对象分级**：intent 跑全文纪律；expected 只跑 EV1-EV3（跳过 EV4） | `test_evidence_expected_values_exempt_from_ev4` 等 3 条 |
| 2 | **回炉死循环**：重写无法摆脱 EV4 | `apply_rework` 新行 `evidence_class` 原样继承父集（fact→static 的"直证带来源"义务带给重写稿） | 重写稿按新 payload `basis` **重算** evidence_class；payload 形态归一（`_normalize_rewrite_payload`，只归一不回退内容——棘轮照抓断言层回退） | `test_apply_rework_rederives_evidence_class_not_inherited` 等 4 条 |
| 3 | **EV4 对派生用例仍是范畴错误**（修 1/2 后打回率 55.2%，16 条全落 intent） | `basis=fact`→static 的用例 intent 是行为描述；逐字索 file:line 忽略了它的来源链本来就在 payload（`testpoint_code` 由编排器从 confirmed 测试点机械注入，非 LLM 自由填） | static 级 intent 的 EV4：payload 带测试点派生链（testpoint_code/evidence）即视为已满足来源义务；**executed 级不放宽**（声称真跑过必须给运行记录） | `test_evidence_static_intent_with_testpoint_provenance_passes` 等 3 条 |
| 4 | **评审/回炉批次无单条隔离**——一条 LLM 网关超时打断整轮（实测 429 no_available_account 频发） | `review_run_proposals`/`apply_rework` 循环无 per-item try | 单条异常→保持 machine_pending/回炉态 + `reviews.review_error` 留痕 + `errors` 计数；重跑续评（幂等） | `test_review_flow_batch_isolates_proposal_llm_error` 等 3 条 |
| 5 | **`finalize_import` 不推进 run.status**（done 永远到不了） | 入库只写 decisions，状态机停在 cases_proposed/cases_confirmed | 入库前推 `cases_confirmed→importing`，成功后 `importing→done`；有入库用例的需求 gen_status→`imported`；幂等重入不炸 | `test_finalize_import_advances_cases_confirmed_to_done` 等 2 条 |

附带小修：凭据键正则加词边界（`secretary`/`tokenizer` 不再误报，`my_password` 照报，
gates+validators 两处同口径）；`genrun.get` 回传 `scope`/`config`（剥 api_key）；
e2e 脚本 JSON 截断自动 16384 重试 + `message.content` 缺键退 `reasoning_content`。

**口径沉淀**：证据纪律的对象是「主张」不是「字符串」——intent 是主张（要依据），
expected 是断言设计（要具体，来源=需求），steps 是操作描述（要可执行）。
三级语义不同，纪律强度不同；机器门按语义分级，不按 JSON 路径一刀切。

---

# P2 落地记录（2026-10-20，六 agent 并行 + 统一接线，830 测试全绿）

25 号文 P2 清单全部落地。六块由并发 agent 各守互不相交文件产出，热区
（gen_methods.py / orchestrator.py / methods.py / waves.py）由主线会话单写者接线。

| 块 | 模块 | 接线（surface 方法） | 测试 |
|---|---|---|---|
| 探索门·按内容分级（§环节7） | `generation/explore_gate.py`（新）：gate_required（framework 免门/field_rule·logic·security·ui_flow 过门，依据=结构化信号）/ next_verify_state / trial_call_plan（api 试调清单，契约判「查无此项」直接打回）/ change_package（挂段事件→离线批量重写→待审变更包，sha1 稳定包号幂等） | `explore.plan`（判定+试调清单）/ `explore.record`（verified_by_run 三态落列）；`plan_waves(require_explored=…)` 探索门闸（缺 False 存量语义不变） | 82 + 2 波次 + 2 方法层 |
| 执行波次（§环节8） | 已在 P1 落 | — | 38 绿 |
| 缺陷六步（§环节9） | 已在 P1 落 | — | 64 绿 |
| 回归三级（§环节10） | defect_flow.regression_tiers 已在 P1 落 | — | — |
| 上线双条件门（§环节10） | `waves.release_gate`（新）：共识 go ∧ 置信≥0.9（含边界）∧ 回滚两栏事前声明齐 → go；不 go 聚合逐条理由；弃权不放行；成功口径=没回滚且没事故 | `release.judge`（纯判定不落库，处置归调用方） | 39 |
| 报告四受众（§环节10/Skill18） | `reports/audience_views.py`（新）：executor（步骤+证据+失败优先）/developer（根因+SQL+日志+关联bug+修复验证）/product（覆盖+风险，零技术细节）/manager（结论先行 Go/No-Go+失败率+置信）；结论恒为返回 dict 第一键；No Data 判 unknown 不编造 | `report.view`（report_id×audience 派发） | 30 |
| 问题库回流（§环节11） | defect_flow 补 `make_sink_entries`（批量、四写入者+两直写入口；「reason/reject」文档字面量经 canonicalize_sink_kind 归一到定版 reject_reason；strict 要么全有要么报错，无静默丢数据中间态） | `question.sink`（通用批量入口；单条走既有 case.reject_sink） | 28 |
| 风格锚回灌（§环节11） | `generation/recall.py`（新）：is_golden（confirm_ref.golden 三态兼容）/ recall_golden（同模块≤5 倒序）/ build_history_slice（英文切片：golden 仿写段在前、existing 查重段在后，超长先截 existing 保 golden） | `orchestrator._default_history_provider`：run_case_generation 不传 provider 时自动召回（需求 module 缓存 + 同测试点查重含本 run 提案）；显式 provider 可覆盖 | 31 + 2 接线 e2e |
| GenRunView 独立页面（前端） | `gen-runs-page.tsx`（新）：列表页 `/testing/gen-runs` + 详情页 `/testing/gen-runs/:runId`（整页复用 GenRunView，门态/轮询/判决零重复；状态文案永远用后端 status_label）；后端补 `genrun.list` | 入口=用例页「生成任务」按钮（不加侧栏菜单，守「不一直加新页面」）；vite build 绿 | build ✓ |

方法注册表 201→**207**（+explore.plan/explore.record/release.judge/report.view/question.sink/genrun.list）。

**并发纪律**：agent 各守新建文件零热区修改；接线（含方法注册、默认 provider、波次闸门
参数）全部主线单写者完成后全量回归。

---

# 执行引擎接线（2026-10-20 续，waves 生产调用方落地）

P2 逻辑层落地后，最后一块「库就绪没人调」补齐——**waves 纯函数的生产调用方**：

| 层 | 模块 | 作用 |
|---|---|---|
| run 级编排 | `engine/run_orchestrator.py`（新）：`select_runners`（探索门→A/B/C 波→flaky 隔离区→并行准入四闸）+ `run_waves`（逐波执行 + 冒烟硬挂停后波 + 失败分类重试 + 四态汇总） | waves 从「库」变「跑得起来」 |
| 引擎错误→failure_kind | `_error_to_failure_kind`：EngineError.kind → 四类（无命中保守 script_issue；env 显式标记走 env_flaky）；引擎不产 kind 时 fail→product_bug（否则冒烟门永远停不了后波） | 四态映射的输入侧 |
| surface 方法 | `run.execute_waves`（execute）：run.scenario_ids→候选，真跑引擎 + PgResultWriter 落 mc_test_run_scenario/step_result；波次派生 failure_kind 回写；run.status→done + 四态/波闸/隔离/拦截明细全量落 run.summary | 前端执行页数据源 |
| 前端 | 执行页四态汇总卡 + 波次闸门时间线 + 隔离区/待探索列 + 场景行四态徽章 + 「按波次执行」按钮 | runs-page |

**条级隔离**：单场景引擎异常收敛成行级 error，不连坐同批（§环节8「熔断只跳依赖方」最小面）。
**测试**：`test_engine_run_orchestrator.py` 9 + `run.execute_waves` e2e 2；后端全量 **900 绿**。
方法注册表 208（+run.execute_waves）。

---

# 功能 UI 落地记录（2026-10-20 三页，907 后端测试绿 + 前端 build 绿）

| 页面 | 能力 | 后端方法 |
|---|---|---|
| 报告页（reports-page） | 四受众 Tab（执行者/研发/产品/管理者，结论先行卡恒顶）+ 上线判定面板（共识/置信滑杆/回滚两栏→Go/No-Go+reasons） | report.view / release.judge |
| 用例页（cases-page + case-detail-panel） | 「端」「探索」两列三态徽章（未探索灰/已探索绿/探索失败红）+ golden 星标 + 详情「探索门判定→标记已探索/失败」动作链 | explore.plan / explore.record；case.list/get document 补 surface/testpoint_id/verified_by_run/golden 四列 |
| 执行页（runs-page） | 四态汇总卡 + 波次闸门时间线（A→B→C 放行/拦下/未执行+reason）+ 隔离区/待探索列 + 场景行四态徽章（与后端 RESULT_STATE_MAP 同口径）+「按波次执行」按钮 | run.execute_waves |
| 生成任务页（gen-runs-page） | 列表页 /testing/gen-runs + 详情页 :runId（整页复用 GenRunView，深链不丢进度）；入口从用例页长出不加侧栏菜单 | genrun.list |

数据链补全：reports `_collect_bugs`/`_collect_requirements` 从空桩改真采（run→bug from_run_id 归集 + 用例→需求 covered/partial/not_covered）；波次派生 failure_kind 回写 run_scenario（四态徽章数据源）。
