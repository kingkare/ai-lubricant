# 用户自定义规则驱动 AI / 执行 —— 开源测试平台与框架调研

调研目的：为「用户定义规则、AI 按规则干活」机制提供设计参考。我们的场景是
AI 测试平台用例生成流程（AI 提测试点 → 生成用例 → AI 评审 → 人确认），
规则需在 **生成 / 评审 / 校验** 多环节生效，典型规则如
"支付模块用例必须有并发场景"、"涉金额断言必须查数据库"。

调研方法：git clone（api.github.com 匿名限流 60/h 很快耗尽，raw.githubusercontent.com 被墙）
+ GitHub API contents(raw) + 仓库内源码与文档。所有引用均给出仓库内真实路径。

---

## 0. 结论速览：最值得抄的 5 个机制

| # | 机制 | 来源 | 数据模型 | 生效方式 | 前端形态 |
|---|------|------|----------|----------|----------|
| 1 | **规则=「作用域滤镜 × 执行器绑定 × 输出口径」三段式**，一条规则不改代码即可换判定器 | Langfuse `EvaluationRule` + `Evaluator` + `ScoreConfig` | Rule{filter(类型化条件数组), sampling, timeScope} → Assignment{evaluatorId, variableMapping} → Evaluator{版本化, LLM判官/代码/决策模型} → ScoreConfig{dataType, categories, min/max} | 落库后由 worker 对**新流入数据**按 filter 匹配后流式执行；可选回填 | 独立 Evaluators / Rules 页；创建规则时可视化 filter pills + 变量映射 + 测试面板 + 采样率与成本预估 |
| 2 | **规则=注册表里的「类型 + 参数 + 阈值 + 权重 + 判定理由」**，断言可分组加权、可被全局 `defaultTest` 继承、可单测关掉 | promptfoo assertions | Assertion{type, value, threshold, weight, metric, provider, rubricPrompt, config, transform}；assert-set{assert[], threshold, weight} 可嵌套 | 生成时（redteam policy 生成探针）/ 执行时（ASSERTION_HANDLERS 注册表派发）/ 报告时（namedScores 聚合） | eval-creator 可视化向导（AssertsForm.tsx）+ Assertion 前置依赖校验 + YAML |
| 3 | **自然语言规则 → 生成器模板 + 判官模板成对定义**，一条业务规则同时驱动"造用例"和"评用例" | promptfoo redteam Custom Plugin / Policy Plugin | plugin.yaml{generator(Nunjucks), grader(Nunjucks), threshold, metric, id} | generator 渲染进生成 prompt 造测试；grader 渲染成 llm-rubric 逐条判定 | redteam 设置向导 → Application Details → Plugins → Custom Policies 页：Add Policy / 上传 CSV / **从应用描述自动生成策略建议** / 逐条预览生成的用例 |
| 4 | **规则集分层：作用域用 fnmatch 命中，多条规则叠加取"最严"，且有条"只评估不拦截"的影子模式** | GitHub rulesets | Ruleset{name, target(fnmatch 分支/标签), rules[], bypass_actors, enforcement} | 写入/合并时服务端强制；enforcement=Evaluate 只记录"本来会违反哪条" | Settings → Rules → Rulesets（仓库级/组织级），任何有读权限的人都能看到生效规则 |
| 5 | **规则=判定树（DAG），叶子节点是"裁决→分数"的确定映射，只有判定节点交给 LLM** | DeepEval `DAGMetric` | TaskNode / BinaryJudgementNode / NonBinaryJudgementNode + add_verdict(verdict, score\|then)，构图时校验无环/完备 | 执行时逐节点走树，最终 score→与 threshold 比较 | 代码（pytest），`verbose_mode` 打印走了哪些节点与裁决 |

次级值得一提：
- SonarQube 的 **Rule / ActiveRule / QualityProfile / QualityGate 四层拆分**
  （规则定义 vs 规则在某档位下的启用+参数覆盖+严重度覆盖 vs 档案 vs 聚合门禁）+
  **档案继承（NONE/OVERRIDES/INHERITED）+ 变更留痕（changelog）**；
- **禅道工作流引擎**（`zt_workflowfield.rules → zt_workflowrule` 可复用规则库 +
  `zt_workflowgroup` 按组织对象作用域 + `status='wait'` 硬闸 SQL 过滤）
  —— 传统测试平台里最完整的"用户定义规则引擎"；
- Guardrails AI 的 **on_fail 动作矩阵（reask/fix/filter/refrain/exception/fix_reask/custom）**
  把"违反规则之后做什么"变成规则自身的一等属性；
- Schemathesis 的 **4 层作用域 + 每规则 kwargs 自动转发 + 按违规身份去重统计 +
  带 expires 的基线豁免**（`@schemathesis.check` 注册表 + `after_response/after_run` 两段式）；
- TestLink 的**反面教材**：`valid_regexp/length_min/length_max` 存了、导出了、从未被执行 ——
  规则必须有服务端执行点。

---

## 1. AI 测试 / LLM 评估工具的「自定义断言 / 规则」

### 1.1 promptfoo（26k★）—— 断言注册表 + 模型判官 + 规则驱动生成

**A. 规则数据模型**

断言对象（`site/docs/configuration/expected-outputs/index.md` "Assertion properties"）：

| 字段 | 说明 |
|------|------|
| `type` | 断言类型（必填），见下注册表 |
| `value` | 期望值/标准，可为 `file://...` 外链文件（js/py/json schema/txt） |
| `threshold` | 阈值（similar/cost/javascript/python/ruby/llm-rubric 等） |
| `weight` | 权重，默认 1.0，参与用例最终加权得分 |
| `provider` | 该断言用的**判官模型**（与"被测模型"分开） |
| `rubricPrompt` | 覆盖判官的提示词模板（可 `file://`） |
| `config` | 任意字典，透传给自定义 js/py/ruby 断言函数 |
| `transform` | 先对输出做变换再断言 |
| `metric` | **命名指标标签**，在 Web UI 里以独立指标行出现（D 的关键） |
| `contextTransform` | 动态构造 RAG 上下文相关断言所需上下文 |

分组：`assert-set{ assert: Assertion[], threshold, weight, metric }` —— 一组断言里只要达到
threshold 比例即通过（如 4 条各 0.25 权重，threshold 0.25 表示 1/4 通过即可），
可嵌套、可加权、可命名。反向断言用 `not-` 前缀（`not-equals`/`not-regex`…）。

用例级：`tests[].threshold` 覆写"整条用例"判定（加权总分 ≥ threshold 即通过），
不设则要求所有非零权重断言都通过。

作用域与继承：
- `defaultTest`（`site/docs/configuration/guide.md` L274+）：对所有用例生效的"全局规则"，
  含 `assert`/`vars`/`metadata`/`threshold`/`options`；单条用例可 `options.disableDefaultAsserts: true`
  **只关掉继承来的断言、仍保留 vars/metadata** —— 继承 + 精细化退出 的干净模型。
- `scenarios[].tests[]`：把"一组数据 × 一组断言"绑定成场景包（同断言跑多组数据）。
- 配置可整体 `file://` 外链（modular-configs.md）：`prompts/providers/tests/defaultTest/env`
  都能拆成独立文件，甚至整个目录。

类型注册表（`src/assertions/index.ts`）：`ASSERTION_HANDLERS: Record<AssertionType, handler>` +
`MODEL_GRADED_ASSERTION_TYPES` 集合 + `TRACE_AWARE_ASSERTION_TYPES` 集合。
确定性断言 ~45 种（contains/regex/is-json/contains-sql/latency/cost/word-count/
trajectory:tool-sequence/trace-span-count…），模型判官断言（llm-rubric、g-eval、
context-faithfulness、factuality、select-best…）。

判官输出契约（`src/matchers/rubric.ts`、llm-rubric.md）固定为
`{ reason, score: 0-1, pass: bool }`，`threshold` 与 `pass` 同时成立才算通过。

**B. 生效环节 / 注入方式**
- **执行时（判定）**：断言注册表派发；llm-rubric 用 Nunjucks 渲染 rubric（支持 `{{var}}`
  与 `{{output.text}}`），把被测输出包进判官 prompt；自定义判定 = 用户写
  JS/Python/Ruby 函数（`file://assert.js`，签名
  `(output, {prompt, vars, test, config, provider, providerResponse, metadata}) => string|bool|number|GradingResult`）
  或 webhook（返回 `{pass:true}`）。
- **生成时（造用例）**：这是 promptfoo 对"用户规则驱动 AI 生成"最直接的回答 ——
  redteam 把规则分成 `plugins[]`：
  - `id: policy` + `config.policy`（纯自然语言业务规则，如
    "不得在退货窗口外承诺退款，除非有经理审批码"），`numTests` 指定**据此生成多少条探针**，
    `severity` 指定严重度；多条 policy 各自独立生成与报告；
  - `id: file://./xxx-plugin.yaml` 自定义插件：文件里**成对**给出
    `generator`（Nunjucks 模板 → 生成对抗输入）与 `grader`（Nunjucks 模板 →
    渲染成 llm-rubric 判官），外加 `threshold`/`metric`/`id`；
    generator 可用 `{{n}}`/`{{purpose}}`/`{{outputFormat}}`/`{{examples}}`。
- **作用域上下文**：`redteam.purpose`（应用描述，作为生成与判分的共同基础）+
  `contexts[]`（每个 context 可覆写 purpose，缺省继承根 purpose）—— 规则作用域带继承。
- `redteam.plugins` 还支持框架预设别名（`owasp:llm:06`、`nist:ai:measure:1.1`、
  `toxicity` 这类集合）—— 规则集"打包引用"。

**C. 前端入口**
`src/app/src/pages/eval-creator/`（可视化建用例向导，含 `AssertsForm.tsx` 断言编辑表单、
`assertionPrerequisites.ts` 断言前置依赖检查、`assertionValueValidation.ts` 值校验）+ 
redteam 设置向导：**Application Details** 填应用描述 → **Plugins** → **Custom Policies** 页
（Add Policy / Upload CSV 批量导入 / **Generate Suggestions 从应用描述让 AI 建议规则** /
每行可"预览该规则生成的测试用例"）。也可纯 YAML。

**D. 规则命中/违反统计**
- 每条断言有 `pass/score/reason`（GradingResult），报告里逐条可见，含 `componentResults`
  （assert-set 内部每条子断言的明细）。
- `metric` 字段把断言归入**命名指标**，参与 `namedScores` 聚合（可用自定义
  `assertScoringFunction(namedScores, context) => {pass, score, reason}` 重算总分），
  并输出 `namedScoreWeights` 供回溯分母 —— 即"每条规则一个得分列"的报表。
- redteam 侧：每条 policy 结果为 `PolicyViolation:<policy id>` 指标，逐规则算
  **攻击成功率(ASR)** → 风险分（severity 基分 + 可利用率 + 人因 + 复杂度惩罚，
  见 `site/docs/red-team/risk-scoring.md`）；生成用例的 metadata 保留
  `pluginId`/`strategyId` 便于溯源。
- 判官审计：GradingResult.metadata 带 `renderedGradingPrompt`（真正发给判官的提示词原文）。

**对我们的启示**：①断言/规则是"可注册的类型 + 参数 + 阈值 + 权重 + 命名指标"，
②`defaultTest` 是全局规则继承且可单点退出，③**一条自然语言业务规则要成对定义
"生成器"与"判官"** —— 这正好对应我们"生成"与"评审"两个环节，
④policy 可批量 CSV 导入 + AI 依据应用描述自动建议规则。

---

### 1.2 DeepEval（18.9k★）—— 声明式判据 + 判定树 + 自建指标

**A. 规则数据模型**
- `GEval(criteria=..., evaluation_steps=[...], evaluation_params=[...], rubric=[...], threshold, model, strict_mode, include_reason)`
  （`docs/content/docs/(custom)/metrics-llm-evals.mdx`）：用**日常语言写判据**；
  `evaluation_steps` 可手写（更稳定）或省略由 LLM 依 criteria 自动生成链式推理步骤；
  `rubric` 是"分数区间 → 结论"的约束（如 score>0.5 才算 relevant），把开放式打分收窄；
  `strict_mode` 把阈值强制为 1（全对才算过）。
- `DAGMetric`（`docs/content/docs/(custom)/metrics-dag.mdx`）：把复杂判据拆成**有向无环判定树**。
  节点三类：`TaskNode`（只做抽取/加工，不打分）、`BinaryJudgementNode`、
  `NonBinaryJudgementNode`；用 `add_verdict(verdict, score)` 或 `add_verdict(verdict, then=next_node)`
  连边，**每条路径必须终止于 score 或另一个 metric**；构图时校验：无环、二元判定必须有
  恰好一对 True/False、判决字符串唯一、不得同时给 score 与 then。可复用共享节点（多父）。
  → 只有"判定"交给 LLM，**分数映射由用户确定**（可复现性远好于纯 LLM 打分）。
- 自建指标（`metrics-custom.mdx`）：继承 `BaseMetric`，实现 `measure()/a_measure()`，
  必须设 `self.score` 与 `self.success`，可选 `self.reason`；构造参数含
  `threshold`（唯一必需）、`evaluation_model`、`include_reason`、`strict_mode`、`async_mode`；
  也可在 `measure()` 里**组合其它 DeepEval 指标**（如 relevancy+faithfulness 合成一个新规则）。

**B. 生效环节 / 注入方式**
执行时（评测期）逐用例跑指标；`assert_test(test_case, [metric1, metric2], run_async=True)`
即 pytest 断言。注入方式 = 代码实例化 + 判官 prompt 由框架拼装（criteria→steps→prompt），
`model` 可换成任意自定义 LLM（`DeepEvalBaseLLM`）。它**不做生成**，是纯"评审/校验"侧参照。

**C. 前端入口**
代码优先（pytest 风格）；Web 侧是 Confident AI 平台（非本仓库）展示运行结果、
每条指标的 score/reason、人工标注。**没有独立"规则页"**，规则就是代码/配置对象。

**D. 规则命中/违反统计**
逐指标给出 score + reason + 是否过 threshold；`verbose_mode` 打印 DAG 走过的节点与裁决；
`verbose_logs`/trace 记录判官交互；Confident AI 侧有指标汇总与人工标注（"这条规则判得对不对"）。

**对我们启示**：DAG 判定树非常适合把"支付模块必须有并发场景"这种规则落到
**结构化可复现的判定**（先 TaskNode 抽取用例里的场景标签，再 BinaryJudgementNode 判定
"是否含并发"，叶子给分）——比让 AI 自由发挥更可控；`rubric`（分数区间→结论）也值得抄。

---

### 1.3 OpenAI evals（19.6k★）—— 注册表 + 模板化判官 + 元评估

**A. 规则数据模型**（`docs/custom-eval.md`、`docs/eval-templates.md`、`docs/build-eval.md`、
OpenAI 现行 Graders API 类型 `openai-python/src/openai/types/graders/*`）
- 评测注册表 = 一堆 YAML，键是评测名（`arithmetic` 是 `arithmetic.dev.match-v1` 的别名），
  字段：`id`/`class`（点路径）/`args`（构造参数，任意 JSONL URI）/`metrics[]`（**第一个为主指标**）/
  `description`。注册表目录可被用户目录覆盖：先加载 `evals/registry/evals`，再加载 `.evals/evals`
  （**用户自定义规则目录叠加在包自带规则之上**，这是很省事的"用户规则"机制）。
- 模板分两层：**基础模板**（`Match`/`Includes`/`FuzzyMatch`/`JsonMatch`，`elsuite/basic/*`）
  与 **模型判官模板**（`elsuite/modelgraded/classify.py:ModelBasedClassify`）：
  `prompt`（含 `{key}` 占位，从数据或 args 填充）、`choice_strings`（期望模型输出的选项，
  如 "ABCDE" 或 ["Yes","No","Unsure"]，其它归 `__invalid__`）、`eval_type`（响应格式约束）、
  `output_template`。`closedqa.yaml` 明说这是一种"criteria-checking"评测：
  **把 rubric 的每条 criteria 逐条喂进去做 yes/no 判定**。
- **现行 Graders API 把"判官"本身变成四种可组合的规则对象**（判官即数据，非代码）：
  - `StringCheckGrader{input, reference, operation: eq|ne|like|ilike}` —— 确定性字符串规则；
  - `LabelModelGrader{input, labels[], passing_labels[](必须是 labels 子集), model}` —— 
    **模型打标签 + "哪些标签算过"单列**；
  - `ScoreModelGrader{input[], model, range:[0,1], sampling_params{temperature,seed,
    reasoning_effort,max_completions_tokens}}` —— 模型打分，**分数区间显式声明**；
  - `MultiGrader{graders[], calculate_output: 公式}` —— **多判官组合成一个分数**（公式字段）。
  判官输入支持模板字符串（`{key}` 占位）；`reasoning_effort`/`seed` 让判官可复现。

**B. 生效环节 / 注入方式**：执行期；规则即"包裹模型输出 + 追加判官 prompt + 解析选项"。
**C. 前端入口**：CLI `oaieval`，无 UI；规则就是仓库里的 YAML。
**D. 规则命中/违反统计**：`recorder.get_events("match")` → accuracy；`metrics.py` 的
比对函数（模糊匹配/包含/JSON 相等）；逐样本事件日志含 `check/correct/error_span`；
最亮的是 **元评估（meta-eval）**：官方建议每条模型判官规则都附一份"人类选择的 choice 标签"
数据集，跑 `*-meta` 得到 metascore，用来验证**判官规则本身靠不靠谱**（接近 1.0 才算好规则）。

**对我们启示**：①"用户规则目录叠加在系统规则目录之上"；②用户贡献规则时**要求附带人类
标注样本**并用 metascore 验证这条规则本身的质量 —— 我们可对"AI 评审规则"做同样的事；
③判官规则要"选项化"（choice_strings）而非开放式打分，便于统计。

---

## 2. 质量门禁类：SonarQube（11k★）—— 四层拆分 + 档案继承 + 变更留痕

源码：`SonarSource/sonarqube`（本地 clone 已 checkout 关键文件）。

**A. 规则数据模型（四层，非常值得抄）**

1. **Rule（规则定义，`server/sonar-db-dao/.../db/rule/RuleDto.java`）**
   `repositoryKey + ruleKey`（唯一键）、`name`、描述（`RuleDescriptionSectionDto` 可分段，
   支持 HTML/Markdown、含 `defaultKey` 段）、`severity`（旧口径）、
   `defaultImpacts: Set<ImpactDto>`（新口径：**一个规则可对多个"软件质量维度"
   各自给严重度**，如 Reliability=HIGH、Maintainability=MEDIUM）、
   `cleanCodeAttribute`、`tags`/`systemTags`、`scope`(MAIN/TEST/ALL)、
   修复成本（remediation function/gap/base effort）、`status`、
   **`isTemplate` + `templateUuid`**（模板规则 → 用户可实例化出"自定义规则"，
   实例可带 `adHocName/adHocDescription/adHocSeverity/adHocType`，
   `isExternal`/`isAdHoc` 表示外部/临时规则可以**不激活也出问题**）。
   规则参数（ParamDefinition）在 `RuleDefinitionsLoader`/`NewRuleCreator` 侧注册。
2. **ActiveRule（规则在某档案里的启用态，`ActiveRule.java`）**
   `ruleKey + severity(覆盖) + params(覆盖的参数字典) + qProfileKey + impacts(覆盖) + updatedAt`。
   → "规则定义"与"规则在这个团队档位下的具体取值"分离。
3. **QualityProfile（规则档案，`QualityProfile.java`+`ActiveRuleInheritance.java`）**
   按语言（`languageKey`）一档；档案有 `parentKee`（父档案）形成祖先链，
   每条启用规则带继承态 **`NONE / OVERRIDES / INHERITED`**；
   提供 `ChangeParentAction`/`CompareAction`/`CopyAction`/`ChangelogAction`/`InheritanceAction`
   （后者还统计"覆盖了父档案多少条规则"）。一个项目按语言各选一个档案（`AddProjectAction`）。
   支持 `ActivateRulesAction` **按查询批量启用/改严重度**（ruleQuery 先过滤，再 bulk activate）。
4. **QualityGate（门禁，`QualityGateDto` + `QualityGateConditionDto` + `Condition.java`）**
   门禁 = **一组条件（作用在"度量"上，不是作用在规则上）**：
   `{metricUuid, operator: GT|LT, errorThreshold, createdAt/updatedAt}`；
   `Condition.useVariation` = 指标键以 `new_` 开头则比较"增量"而非总量；
   `QualityGateDto.isBuiltIn`（内置门禁）、**`aiCodeSupported`**（门禁是否支持 AI 生成代码口径）。
   项目与门禁的关联 `ProjectQgateAssociationDto`（一项目一档）。
   `ConditionEvaluator` 把度量与阈值比较 → `EvaluationResult(Level.OK|ERROR, actualValue)`；
   `EvaluatedCondition` 逐条记录"哪个条件、实际值、阈值、比较结果"。

**B. 生效环节 / 注入方式**
- 规则在**分析时**逐文件/行触发 → Issue（这是"生成/写作时刻"的规则校验）；
- **规则参数**由分析器通过"规则设置"接口下发（档案启用 + 参数覆盖 → 分析器行为改变）；
- 门禁在**分析结束**由 CE 任务对**聚合度量**求值（`QualityGateMeasuresStep`、
  `QualityGateEventsStep`），生成门禁状态与事件；
- PR 装饰/Smart Scan 把"是否会破门禁"提前反馈。

**C. 前端入口**
`Rules`（规则搜索页，`SearchAction` + `RuleMapper`；facets 有
languages/repositories/tags/cleanCodeAttributeCategory/impactSeverities；
可按 `activation=true` 返回"**这条规则被哪些档案启用、各自参数与严重度**"）、
`Quality Profiles`（按语言，启用/停用/批量改严重度/复制/导出/比较/变更日志/继承设置）、
`Quality Gates`（条件 = 度量 + 操作符 + 阈值，支持"总体代码/新增代码"两种口径；项目级
Settings→General→Quality Gate 里选档）；规则详情页含参数与"被哪些档案启用"。
（`server/sonar-webserver-webapi/.../rule/ws/*`、`.../qualityprofile/ws/*`、`.../qualitygates/ws/*`）

**D. 规则命中/违反统计**
- Issue 计数按规则/标签/严重度聚合（搜索 facets 返回各维度计数）；
- 规则页可见"被 N 个档案启用""分析器状态"、`UpdateQualityProfilesLastUsedDateStep`
  维护档案**最后使用时间**（规则有没有在用一目了然）；
- 门禁逐条件留痕 `EvaluatedCondition`（度量/操作符/阈值/实际值/比较结果）；
- **档案变更留痕**：`QProfileChangeDto`(`server/sonar-db-dao/.../db/qualityprofile/`) +
  `QualityProfileTextGenerator` 生成人类可读变更摘要（"3 new rules, 1 deactivated rule and
  2 modified rules"），并有 `ChangelogAction` 查询接口。

**对我们启示**：①**四层拆分**（规则定义 / 规则在某档位下的启用+参数覆盖 / 档案 / 聚合门禁）
直接可映射到我们的"规则定义 / 项目启用与覆盖 / 规则集 / 质量门禁"；
②档案可**继承 + 逐条 OVERRIDES/INHERITED** + 比较 + 变更日志；
③门禁与规则解耦（门禁作用在**聚合度量**上，规则作用在**单条发现**上）；
④规则实例化（模板规则 + adHoc 覆盖）与"按查询批量改规则";
⑤`aiCodeSupported` 提示门禁要区分"AI 产出"的口径。

---

## 3. AI 规则 / 护栏类：规则的"多环节生效"与"违反后怎么办"

### 3.1 NeMo Guardrails（NVIDIA，7.3k★）—— 五阶段轨道 + 自然语言流

**A. 规则数据模型**
`config.yml`：`rails.input.flows[] | output.flows[] | retrieval | dialog | execution`（**每条规则
声明它挂在哪个阶段**），加 `rails.config.*` 给各内建护栏传参（如 PII 要遮哪些实体）；
规则本体是 Colang `.co` 文件里的 `define flow`（可用自然语言 NLD 描述 + `$var` 变量），
或用 Python action；`prompts.yml` 覆盖判官的提示词。
`docs/about/rail-types.mdx` 给出**五阶段表**：Input（调 LLM 前）/ Retrieval（RAG 取文后）/
Dialog（多轮流程）/ Execution（工具调用前后）/ Output（模型答完）。
`config.yml` 例：
```yaml
rails:
  input:  { flows: [check jailbreak, mask sensitive data on input] }
  output: { flows: [self check facts, self check hallucination] }
  config: { sensitive_data_detection: { input: { entities: [PERSON, EMAIL_ADDRESS] } } }
```
`rail-outcomes.mdx` 定义规则命中后的动作（reject/modify/stop/log…）。

**B. 生效环节 / 注入方式**：五阶段显式挂钩；规则 = 订阅事件的 flow（可含 LLM 生成动作
`await ...`，Colang 2.0 甚至用自然语言描述让 LLM 生成后续，见
`docs/.../colang-2/getting-started/llm-flows.mdx`）。
**C. 前端入口**：无 UI，规则=仓库里 `config/` 目录（config.yml + rails.co + actions.py）；
`guardrail-catalog` 是可浏览的内建护栏目录；`.agents/skills/*/SKILL.md` 里还带一个
**"帮开发者写护栏配置"的 skill**（用 AI 帮助用户写规则）。
**D. 命中统计**：事件流 `context.get_events()` / `log` / 评估框架
（`docs/evaluation/evaluate-guardrails.mdx`）逐护栏统计 TP/FP；vulnerability scanning 报告
每个护栏的成功率。

**要点**：**「一条规则必须声明它生效的阶段」** —— 这正是我们"生成 / 评审 / 校验多环节"的
最强先例；且规则本身允许用自然语言写（NLD）。

### 3.2 Guardrails AI（7.5k★）—— Validator 注册表 + on_fail 动作矩阵

**A. 规则数据模型**
`Validator`（`guardrails/validator_base.py`）：`rail_alias`、`on_fail`、元数据
（hub 上的 name/package/**infrastructure 标签 ML/LLM/Logic**、purpose、prompt）、
`validate(value, metadata, ...) -> PassResult | FailResult`；
`FailResult{error_message, fix_value, error_span{start,end}}`（**错误定位到字符区间**）。
规则声明处：RAIL XML（`test_spec.rail`、`docs/how_to_guides/output.md`）：
```xml
<string name="text" format="two-words" on-fail-two-words="reask"/>
<float  name="score" format="min-val: 0" on-fail-min-val="fix"/>
```
或在 pydantic `Field(validators=[...])` / `.use_validator()` 上挂；`ValidatorMap = Dict[字段路径, List[Validator]]`
（`guardrails/types/validator.py`）→ **规则按字段路径绑定**。
`OnFailAction`（`guardrails/types/on_fail.py`）8 种：`reask / fix / filter / refrain / noop /
exception / fix_reask / custom` —— **"违反规则之后做什么"是规则自身的一等属性**。

**B. 生效环节 / 注入方式**：`guard.run()` 时对输出（及输入）校验；
结构性规则 = JSON Schema 一等公民（`schema/validator.py` 用 Draft2020-12 校验整个 payload）；
**reask 循环**：判失败→把 `error_message` 回灌给模型重新生成（默认 retry）——最直接的
"AI 因违反规则而重做"。
**C. 前端入口**：Guardrails Hub（按 `hub://` URI `install` 规则 + 元数据检索）+ CLI，
规则以包的形式共享；无"规则页"。
**D. 命中统计**：`ValidationSummary`（按字段 × 校验器：outcome、error_message、fix_value、
metadata）挂在 `validation_history`、`history.as_dict()`；每轮 iteration 的 `validator_logs`；
hub telemetry 上报校验器使用情况。

**要点**：①**on_fail 动作矩阵**（违反→改/重问/过滤/拒绝/抛错/自定义）非常适合我们
"AI 评审不过 → 自动回炉重生成 or 标记违规"的选择；②`error_span` 精确到字符区间，
让"哪条规则在哪命中"可视化；③规则按"字段/资产路径"绑定。

### 3.3 Langfuse（36k★）—— 生产环境的"规则引擎"（对"多环节 + 命中统计"最强的参照）

源码：`langfuse/langfuse`，`packages/shared/prisma/schema.prisma` 与
`web/src/features/evals/**`。

**A. 规则数据模型（三段式，强烈建议抄）**
1. **Rule（`EvaluationRule` / 旧 `JobConfiguration`）**
   `{projectId, name, status(ACTIVE/INACTIVE), targetObject(EVENT|EXPERIMENT),
     filter(Json), sampling(0..1), delay(ms), timeScope["NEW"|"EXISTING"]}` —— 
   **规则的第一等公民是"作用范围滤镜 + 采样 + 时间窗口"**，而不是判定逻辑本身。
   `filter` 是**类型化条件数组**（`packages/shared/src/interfaces/filters.ts`）：
   `{column, operator, value, type}`，type ∈ datetime/string/stringOptions/number/
   arrayOptions/stringObject(带 key, 如 metadata.environment)/numberObject/boolean/null/
   positionInTrace，操作符按类型给（`>=`、`contains`、`any of`、`none of`、`all of`、
   `is set`…）。Zod 校验（`rules/ruleTypes.ts`），并**用 filter 自动兜底生成规则名**
   （`fallbackRuleName`：`column operator value · ...`，截断 200 字）。
2. **Assignment（`EvaluationRuleEvaluatorAssignment`）**：`{ruleId, evaluatorId, variableMapping}`
   —— 一条规则可挂**多个执行器**，`variableMapping` = 模板变量 → 观测字段/来源
   （trace/observation/level + 路径），即"用数据的哪部分喂给这条规则"。
   UI 有专门的 `VariableMapping` 组件与 `EvaluatorMappingRow`。
3. **Evaluator + EvaluatorVersion（`Evaluator`/`EvaluatorVersion`）**：`{name, type, isBuiltIn,
   description, versions[]}`，类型三选一（`evaluatorTypes.ts` 判别联合）：
   - `LLM_AS_JUDGE`：`promptMessages + provider/model/modelParams + vars[] + outputDefinition`
     （输出口径 schema）
   - `CODE`：`sourceCode + sourceCodeLanguage(PYTHON|TYPESCRIPT)`（**用户写代码当规则**）
   - `DECISION_MODEL`：`questions[]`（一组**类型化问题**，每个问题各自产出一个分数）+
     `variableMapping` 作为状态
   注意 `EvalTemplate{projectId 可空, name+version 唯一, partner, prompt, type,
   model/provider/modelParams, vars[], outputDefinition, sourceCode, sourceCodeLanguage}`：
   **projectId 为空 = 全局模板**，可被项目复用。
4. **ScoreConfig（`ScoreConfig`）**：规则输出口径的字典 —— `{name, dataType:
   NUMERIC|CATEGORICAL|BOOLEAN|TEXT, minValue, maxValue, categories(Json), description,
   isArchived}`。**先定义"分数的语义"，再让规则往里填**。
5. 执行留痕：**`JobExecution`**（`status: PENDING|COMPLETED|ERROR|CANCELLED|DELAYED`、
   startTime/endTime、`error`、`jobInputTraceId/ObservationId/DatasetItemId`、token 用量/成本）。
6. 人工闸门：**`AnnotationQueue{name, scoreConfigIds[]}`** —— 人工评审队列直接声明
   "必须人工填哪些分数（即哪些规则维度）"。
7. 健康闸门：`Evaluator.blockedAt/blockReason/blockMessage` +
   `enum EvaluatorBlockReason{LLM_CONNECTION_AUTH_INVALID, BILLING_EXHAUSTED,
   ENDPOINT_UNREACHABLE, MISSING, DEFAULT_EVAL_MODEL_MISSING, EVAL_MODEL_CONFIG_INVALID,
   EVAL_MODEL_UNAVAILABLE, PROVIDER_ACCOUNT_NOT_READY}` —— **规则因自身故障被自动停用并给出原因**。

**B. 生效环节 / 注入方式**
- **流式**：新数据持续流入（trace/observation）→ 按 filter 匹配 + sampling 抽样 →
  入队 JobExecution → 执行器（判官/代码/决策模型）产出 score；
  `timeScope=["NEW"]` 只对新数据，`["EXISTING"]` 触发回填（backfill）。
- **实验/数据集**：同一套规则可对 dataset item/experiment 跑。
- 注入方式严格数据驱动（DB 里的 filter + evaluator 版本 + 变量映射），**没有任何硬编码 prompt**。
- 激活前有**测试面板**（`EvaluatorTestPanel`）+ **激活成本预估**（`activationCostService.ts`、
  `EvaluatorCostCalculationTooltipContent`）+ 采样率确认弹窗（`ActivationConfirmationDialog`）。

**C. 前端入口**
**有专门的规则页**：`web/src/features/evals/v2/components/` 下
`Rules/{CreateRuleDialog, EditRuleDialog, RulesTable, RuleFilterPills, RuleSetup,
EvaluatorAssignmentsEditor, EvaluatorRuleRelationships, ActivationConfirmationDialog}` +
`Evaluators/{EvaluatorGalleryView, EvaluatorSavedDialog..., EvaluatorRecommendedCard…}` +
`VariableMapping/` + `Stepper`；`ps`/`evals/v2/pages`。规则列表可搜索、按 enabled/upgradeRequired 过滤。

**D. 规则命中/违反统计**
- 每次触发都有 `JobExecution` 记录（状态/错误/耗时/成本）→ "这条规则有没有真的在跑、跑了多少、
  失败没有";
- score 本身即规则的裁决，按 name 在 trace/session/dataset 维度聚合（分数分析看板）；
- `blockReason` 暴露"规则自身故障"；
- `upgradeRequired` 暴露"规则有新版"；
- 人工标注队列（AnnotationQueue）可做**AI 判 vs 人判的一致性**核对。

**要点**：这是我们最该整体借鉴的对象 —— **规则 = (作用范围滤镜 + 采样 + 时间窗) × (执行器绑定 +
变量映射) × (分数口径)**，**判定逻辑可换（LLM/代码/决策模型）且版本化**，
**规则命中逐次留痕**，**故障自动停用**，**激活带成本预估与影子测试**。

---

## 4. 其他学科的"用户规则"参照

### 4.1 GitHub rulesets —— 作用域命中 + 叠加取最严 + "只评估不拦截"

来源：`github/docs`，`content/.../managing-rulesets/about-rulesets.md`、
`available-rules-for-rulesets.md`、`data/reusables/repositories/rulesets-about-enforcement-statuses.md`。

- **数据模型**：ruleset = 具名规则集；`target` 用 **fnmatch 模式**命中分支/标签
  （如 `releases/**/*`），也可针对仓库/fork 网络；`rules[]` 是**类型化规则**（Restrict
  creations/updates/deletions、Require linear history、Require signed commits、
  **Require status checks**（即"必须跑过某些检查"）、Require deployments to succeed…），
  每条规则自带参数；`bypass_actors`（角色/团队/GitHub App + 权限级别 + 计数）声明谁能豁免。
  上限 75/仓库 + 75/组织。
- **生效环节**：写入/合并时服务端强制 + PR 检查。**enforcement 三态**：
  **Active（强制）/ Evaluate（不强制但记录"本来会违反哪条"，页面叫 Rule Insights）/
  Disabled** —— 影子模式是极其值得抄的机制（"先观察规则命中情况再决定是否强制"）。
- **叠加语义（层级）**：ruleset **没有优先级**，多条命中同一分支的规则**聚合**，
  同一规则出现多个版本时**取最严的那版**；且与分支保护规则叠加。
- **前端入口**：Settings → Rules → Rulesets（仓库级 / 组织级）；
  **"任何有读权限的人都能看到生效中的 ruleset"**（规则可见性被当成一等特性，方便审计）。
- **命中统计**：Evaluate 模式的 **Rule Insights** 页 + API，展示"这次动作会不会违反规则/
  违反了哪条"。

### 4.2 AGENTS.md / SKILL.md —— AI 时代的"规则文件"

- **AGENTS.md**（`openai/agents.md`）：纯 Markdown、无 schema，放在仓库里被 AI 工具读取，
  就近目录覆盖/聚合；本质是"人写给 AI 的自然语言规则"。
- **SKILL.md**（Claude Code 插件、NeMo 也用了同一约定）：**YAML front-matter
  （`name` + 长 `description` + 触发关键词）**，正文是操作手册 —— 靠 `description` 让模型
  **自行判断何时加载**这条规则（`plugins/frontend-design/skills/frontend-design/SKILL.md`、
  `nemo/.agents/skills/guardrails-developer-create-guardrails/SKILL.md`）。
  启示：**规则可用自然语言写，用 `description`/触发词做"作用域"，由 AI 自己决定挂不挂**；
  复杂规则可拆成"规则文件 + 引用文件"（`references/*.md`），只按需加载。

---

## 5. BDD / 框架级用户规则

### 5.1 Schemathesis（API 测试，自定义校验的标杆）

**A. 规则数据模型**
- 自定义校验 = 函数 **或** 类，注册进全局类型化注册表：
  `src/schemathesis/checks.py:250` `CHECKS = Registry[CheckFunction | CheckClass]()`，
  装饰器 `@schemathesis.check`（`checks.py:343`）。注册表实现
  `src/schemathesis/core/registries.py`（~25 行，按 `item.__name__` 建键，
  `get_all()/get_one(name)/get_by_names(names)`）。
- 函数签名（`checks.py:37`）：`CheckFunction = Callable[["CheckContext", "Response", "Case"], bool | None]`，
  违反时 `raise AssertionError(...)`（或 `Failure` 子类）。
- 类校验可实现 `after_response(self, ctx, response, case)`（逐响应）和/或
  `after_run(self, ctx)`（整轮跑完后一次，用于"跨全量"不变式）。类**每轮只实例化一次**，
  `__init__` 关键字参数由 `schemathesis.toml` 的 `[checks.<ClassName>]` 段**自动转发**
  （`checks.py:253-270` `_instantiate_check`，未知键报 ConfigError 并提示合法键）。
  方法名/签名有校验：`CHECK_METHODS = {"after_response": ResponseCheck, "after_run": RunCheck}`，
  未知 `after_*` 或参数元数不符 → `IncorrectUsage`。
- `CheckContext`（`checks.py:77-166`）给每条校验注入：`config`（按操作/阶段解析的
  `ChecksConfig`）、`phase`、`_recorder`（`ScenarioRecorder`，可 `find_parent/find_related/
  record_case`）、`_auth/_headers/_override`、跨全轮共享的 `auth_enforced_operations: set[str]`。
- 每条校验的开关与参数：`SimpleCheckConfig{enabled}`、`CheckConfig{enabled, expected_statuses}`、
  `MaxResponseTimeConfig{enabled, limit}`（`config/_checks.py`）。

**B. 生效环节 / 注入方式**
- **执行时、逐响应**。流程（`docs/reference/hooks.md:44`）：
  `before_call → HTTP → after_call → run_checks() → filter_failure → after_validate`。
  `run_checks(...)` 遍历激活的校验列表，捕获 `(Failure, AssertionError, FailureGroup)`，
  失败回调 `on_failure(name, collected, failure)` → `recorder.record_check_failure(...)`。
  `after_run` 经 `collect_after_run_failures` 在跑完后触发。
- 另一族是 **hooks**（`@schemathesis.hook`）：`map_<part>/filter_<part>/flatmap_<part>`
  （**生成期**，改变生成的数据 —— 相当于"生成规则"）与
  `before_call/after_call/after_validate/filter_failure`（生命周期）。
  `.apply_to(path_regex=, method=, name=, tag=, operation_id=)` / `.skip_for(...)` 做**条件作用域**。
- `@cli.handler()` 事件处理器（ScenarioStarted/Finished…）可 `ctx.add_summary_line/add_report`。
- 自定义数据格式：`schemathesis.openapi.format("credit_card", strategy)` —— 用户定义格式策略，
  schema 引用即生效（生成侧规则）。

**C. 作用域 / 作者入口**
- **4 层配置层级**（`config/_projects.py:505-525` `checks_config_for(operation, phase)`）：
  按操作+阶段 → 按操作 → 按阶段 → 项目全局，`ChecksConfig.from_hierarchy` 合并。
- `[checks.<name>]` TOML 段控 `enabled` + 每校验 kwargs；未知 TOML 段会被当作自定义校验的
  `__init__` kwargs（`config/_checks.py:262-273`）。CLI：`-c/--checks a,b`、`--exclude-checks`、
  `--max-response-time`，include/exclude **同时作用于内建与注册用户校验**。
- **无 UI**。规则 = 一个 Python 文件（CLI 用 `hooks.py`，pytest 用 `conftest.py`），
  经 `SCHEMATHESIS_HOOKS` 环境变量 importlib 加载并按 realpath 去重
  （`src/schemathesis/core/hooks.py`）。两种形式：模块名 `hooks` 或文件路径 `/path/hooks.py`。

**D. 命中/违反统计**
- `ScenarioRecorder.checks: dict[case_id, list[CheckNode]]`，`CheckNode=(name, status, failure_info)`。
- `Statistic`（`engine/statistic.py:100-254`）：`failures: dict[label, dict[case_id, GroupedFailures]]`、
  `unique_failures_map`、`observed_failures`（按 `failure_identity(failure, check.name)` 去重）、
  `filtered_failures` 计数、`cases_with_failures`、`cases_without_checks`、
  `operations_without_checks`、`errored_cases`。
- CLI 摘要 `reduce_failures`（`cli/summary.py:399-422`）按 `failure.title` 分组给
  `count + operations[] + severity`，按 severity 排序。输出 HTML/JUnit XML/HAR/JSON/Allure。
- **基线豁免（baseline waiver）**：已知失败文件带 `expires` 日期，
  `known_failures/unobserved_ids/expired_ids` —— 按"违规身份"逐条接受技术债，而非整条规则关掉。

**对我们的启示**：`@check` 注册表 + 类校验的 after_response/after_run 两段式 +
TOML 参数转发 + 4 层作用域 + 按身份去重的违规计数 + 带过期日的基线豁免。
"涉金额断言必须查数据库"= 函数校验；"支付模块用例必须有并发场景"= `after_run` 类校验
（跨全量集合，注意文档警告：**累加要幂等，用 set/min/max 别用计数器**，因为 shrinking/replay 会重跑）。

### 5.2 Robot Framework —— 用户关键字成为一等公民

**A. 规则数据模型**
- 用户关键字写在 `.robot/.resource` 的 `*** Keywords ***` 表；
  设置行 `[Arguments] ${name}=default`、`[Documentation]`、`[Tags]`、`[Setup]/[Teardown]/[Timeout]`、
  `RETURN`（`doc/userguide/.../CreatingUserKeywords.rst`）。
- **嵌入参数（embedded arguments）**：关键字名内嵌 `${arg}`（如 `Select ${animal} from list`），
  `EmbeddedArgumentParser.parse`（`running/arguments/embedded.py:123-214`）编译成正则捕获组，
  可自定义片段模式 `${x: \d+}`，默认 `.*?`。
- `ArgumentSpec`（`arguments/argumentspec.py:37`）：positional_only / positional_or_named /
  var_positional / named_only / var_named / defaults / embedded / types / return_type / raises。
- `KeywordImplementation` 是 `UserKeyword` 与 `LibraryKeyword` 的共同基类，
  `matches(name)` 走正则（嵌入）或大小写/空格/下划线不敏感相等。**库的 Dynamic API**
  （`running/dynamicmethods.py`）：`get_keyword_names()/run_keyword(name,args,kwargs)/
  get_keyword_arguments/types/tags/documentation/source`，框架同时探测 snake_case 与 camelCase。

**B. 生效环节 / 解析模型**
执行期。`Namespace`（`running/namespace.py:35`）持 `KeywordStore`
（libraries OrderedDict + resources ImportCache + search_order）。
解析顺序（`namespace.py:323-340`）：剥 BDD 前缀 → 套件文件用户关键字 → 显式 `Library.Keyword`
→ 隐式（资源文件再库）。`KeywordFinder`（`keywordfinder.py:30-91`）对普通名用
`NormalizedDict`（忽略 `_`）缓存、嵌入名单独列表。多义消解（`namespace.py:397-462`）：
精确名胜过嵌入；`_is_better_match` 按嵌入正则子集关系；`search_order` 优先级；
同文件/private 优先；标准库与自定义冲突时告警并建议显式全名。
`UserKeywordRunner.run` 与库关键字走同一 runner 协议 —— **用户关键字与内建关键字完全同级**。

**C. 作用域 / 入口**：纯文本文件。资源文件关键字对 import 它的文件可用；套件文件关键字
文件内可见；`__init__.robot` 套件内可见；`robot:private` 标签限 owner 内可见。
`libdoc` 生成 HTML/JSON/XML 关键字文档（`doc/schema/libdoc.json` 是序列化关键字 schema）。

**D. 命中/违反统计**：`src/robot/result/` 记录每次关键字执行的 status/起止/消息，
`output.xml → log.html/report.html`；统计有 **per-suite、per-tag、per-keyword** 的通过/失败与时长
—— **标签即规则选择器**（"所有 `payment` 标签的用例过没过"无需自建规则引擎）。

### 5.3 Cucumber（cucumber-js）—— 步骤定义 + 表达式 + 标签作用域钩子

**A.** `Given/When/Then/defineStep(pattern, fn)` 注册步骤定义
（`src/support_code_library_builder/index.ts:159-188`）；pattern 是 CucumberExpression 字符串或 RegExp。
`StepDefinition` extends `Definition`（`code,id,line,options{timeout,wrapperOptions},order,uri,
keyword,pattern,expression`）。匹配（`step_definition.ts:25-43`）：`expression.match(step.text)`
→ `Parameter[]`，逐个 `arg.getValue(world)` 转换。**自定义参数类型**：
`defineParameterType({name, regexp, transformer, useForSnippets, preferForRegexpMatch})`
→ `SourcedParameterTypeRegistry`。钩子选项 `IHookDefinitionOptions` 多 `{name?, tags?}`。

**B.** 执行期。`SupportCodeLibraryBuilder` 在模块加载期收集 configs，`finalize()` 构建
`SupportCodeLibrary` 注册表供运行时查询。钩子带 `tagExpression` + `PickleTagFilter`：
标签串经 `@cucumber/tag-expressions` 解析成 AST，`matchesAllTagExpressions(pickle)` 求值
（`src/pickle_filter.ts:124-139`）。`Before({tags:"@foo and @bar"}, fn)`，支持 and/or/not；
多个 Before 按注册序、After 逆序。

**C.** 纯 JS/TS 文件（`features/step_definitions/*.js`、`features/support/*.js`），
`--require/--import` 加载。**亮点：片段生成（snippet generation）** —— 步骤无匹配定义时，
snippets formatter 自动生成骨架步骤定义（`Then('I remove the todo {string}', function(){return 'pending'})`），
把"规则未定义"从构建失败变成建设性建议。

**D.** `UsageFormatter`（`src/formatter/usage_formatter.ts`）：逐步骤定义表 —— pattern、平均时长、
最多 5 条示例匹配（步骤文本+时长+位置）、**零匹配标 `UNUSED`**；`UsageJsonFormatter` 同构 JSON。
这就是"规则有没有被用到"的直接答案。

### 5.4 Karate —— `match` 断言 DSL + 可复用 feature + 标签值谓词

**A. match DSL**：操作符 `==, !=, contains, !contains, contains deep/only/any, within, !within`
（`match/Match.java:33-54`），`each` 量词前缀 `EACH_`。模糊标记（期望侧）：
`#number #string #boolean #array #object #null #notnull #uuid #ignore #present #notpresent
#regex <pat> #? <js谓词> #[N] / #[_.length<5] #(...) ##(...)`（可选），
在 `match/Operation.java:298-419` 与 `match/Validators.java:62-86` 派发。
`MarkerPolicy.trusted(marker)` 决定每个标记走调用方 JS 引擎（可见变量）还是全新无变量引擎 ——
**用户断言表达式的安全边界，逐标记在求值时决定**。

**B. 复用与全局配置**：`FeatureCall`（`core/FeatureCall.java`）= Feature + 可选 tagSelector，
支撑 `call read('foo.feature@tag')`。`call/callonce`：callonce **每 FeatureRuntime 只跑一次**并缓存
（key `"callonce:call read('<path>')"`，`core/ScenarioRuntime.java:646-656`）。
`karate-config.js`(+`karate-base.js`/`karate-config-<env>.js`) 由一个无步骤的
ScenarioRuntime 配置阶段求值（`core/ConfigLoader.java:36-109`）—— **全局配置本身就是一个规则钩子**。

**C. 标签选择（比布尔强得多）**：`TagSelector`（`core/TagSelector.java:39-143`）支持
`anyOf/allOf/not` 以及 **`valuesFor('@tag')` 的谓词族：isPresent / isAnyOf / isAllOf / isOnly /
isEach(fn)**，可把谓词函数作用在标签的**值**上；多行 `@suite=x @suite=y` 聚合为
`valuesFor('suite')==[x,y]`。→ 规则可写"对每个 `@tier=critical` 的用例，要求 2 个并发场景"，
检查的是标签值而非存在性。`Ext` SPI（`core/Ext.java`）：`boot.ext('name')` 解析
`io.karatelabs.ext.name.NameExt`，注册为 RunListener 观察每个 RunEvent。

**D.** 逐 Gherkin 步骤执行；`Match.execute(...)` 返回 `Result`(pass+message)；
`ScenarioResult/StepResult/SuiteResult` 聚合，`JsonLinesEventWriter` 流式写
`karate-events.jsonl`，HTML 报告显示每条 match 失败的 actual-vs-expected diff。

### 5.5 Gauge —— concept 内联复用 + 自动提取

`.cpt` concept 文件（标题 + 嵌套步骤）由 `parser/conceptParser.go` 解析；
`ConceptDictionary`（`gauge/concept.go`）以 `Step.Value` 为键存 `ConceptsMap[stepValue]` →
`ConceptStep`。`ReplaceNestedConceptSteps`（`concept.go:30-52`）**在解析期把嵌套 concept 递归
内联**进引用步骤 —— 执行器只见原语步骤，而作者/AI 只见一个名字。
`conceptExtractor/conceptExtractor.go` **自动把重复的步骤序列提取成 concept** ——
"AI 发现你重复了 5 次这个断言，要不要升格成规则？"的现成模型。

### 5.6 框架级"值得抄"清单（子调研浓缩）

1. **注册表 + 装饰器自注册**（Schemathesis `CHECKS`+`@check`）：生成器的 prompt 由
   `Registry.get_all()` 自省（名字+docstring+签名）拼出 —— **加一条规则自动约束生成**；
   校验器拉同一注册表执行。
2. **4 层作用域 + 参数 kwargs 自动转发**（`checks_config_for(op,phase)` + `[checks.<Name>]`）：
   非程序员改 TOML 调规则，不碰代码；`__init__` 签名即契约。
3. **env-var 指向规则文件 + import 副作用即注册**（`SCHEMATHESIS_HOOKS`）：规则包=可 git 追踪目录。
4. **DSL 模板 + 嵌入参数编译成正则 + 最优匹配消解**（Robot embedded args / KeywordStore 解析序）。
5. **两段式校验**：`after_response`（逐条）+ `after_run`（全量不变式，**幂等累加**）。
6. **逐条 CheckNode + 按身份去重 + 按严重度分组报表**（"规则 X 在 47 个支付用例的 12 个中违规"）。
7. **失败类型分类学 + `filter_failure` 钩子 + 带 expires 的基线豁免**。
8. **规则本身可被 call/callonce 复用 + 标签值谓词**（Karate）。
9. **concept 字典内联 + 自动提取重复为规则**（Gauge）。
10. **未定义规则的片段生成**（Cucumber）：把"规则不存在"变成可落盘骨架。

---

## 6. 测试管理平台（Kiwi TCMS / TestLink / 禅道）

### 6.1 禅道 zentao —— 三档用户规则 + AI 输出契约由规则元数据编译（本次最惊喜）

仓库自带 `CLAUDE.md`/`QWEN.md`，且有真实 AI 模块 `module/ai`。

**A. 规则数据模型（三层）**
1. **`zt_config(vision, owner, module, section, key, value)`**（`db/zentao.sql:684`）：
   万能"DB 覆盖代码默认值"。`framework/router.class.php::loadConfig()` 先读
   `module/<m>/config.php` 再 merge `zt_config` 行，`owner='system'`（全局）或
   `owner=<账号>`（个人）。于是 `$config->testcase->needReview=0`
   （`module/testcase/config.php:5`）、每方法的必填字段清单
   （`module/custom/model.php::saveRequiredFields()/getRequiredFields()`，
   候选来自 `$config->custom->fieldList['testcase']['create']`）
   都能被管理员改，无需改 schema。
   `$config->custom->customFields['testcase'] = ['custom' => ['createFields','batchCreateFields','batchEditFields']]`
   —— 管理员挑"每个表单显示哪些字段"。
2. **工作流引擎**（`zt_workflow*`，`db/zentao.sql:13125-13445`；引擎代码在付费版，
   但 OSS schema + `lib/form/form.class.php` + zin 组件把模型完整描述出来了）：
   - `zt_workflowgroup(id, objectID, type, projectModel, projectType, name, code,
     disabledModules, disabledFeatures, main, exclusive, deliverable, vision)`
     —— **作用域对象**：把整套工作流绑到组织对象（项目/产品/程序）上，`main`=默认档，
     `exclusive`=独占。
   - `zt_workflow(group, module, table, name, buildin, approval, status, vision)`
   - `zt_workflowfield(group, module, field, type, length, name, control, expression,
     options, default, **rules**, placeholder, canExport, canSearch, isValue, readonly,
     buildin, role)` —— 自定义字段，`rules` 是**逗号分隔的规则 ID 列表**，
     唯一键 `(group, module, field)`。
   - `zt_workflowrule(id, type∈{system,regex,func}, name, rule, builtin)`
     —— **可复用规则库**：内建 notempty/unique/date/email/float/phone/ip + 自定义正则/函数。
   - `zt_workflowaction(..., conditions, **verifications**, **hooks**, **linkages**, js, css, blocks)`
     —— 每个动作的显示条件/校验/后置钩子/字段联动（JSON 块）。
   - `zt_workflowlayout(group, module, action, ui, field, order, width, position,
     readonly, mobileShow, summary, defaultValue, **layoutRules**, vision)`
   - `zt_workflowdatasource(type∈{option,sql,system}) / zt_workflowsql / zt_workflowversion`
     —— 共享选项源 + **工作流版本快照**（升级安全）。
   - 审批：`zt_approvalflow(name,code,workflow,version)` +
     `zt_approvalflowspec(flow,version,nodes=JSON)` + `zt_approvalflowobject(root,flow,objectType,objectID)`
     + `zt_approvalnode(type='review', reviewType='manual', agentType='pass',
     multipleType='and', percent, needAll, solicit, prev, next, status, result, opinion,
     revertTo, forwardBy)` + `zt_approvalrole(code,name,users)`。
   - 运行时：`framework/model.class.php::saveFileByWorkflow()`
     → `getGroupIDByDataID → getByModuleAndAction → getUIByDataID → getPageFields →
     workflowhook->execute()`；每个 model 的 insert/update 链上都挂 `->checkFlow()`
     （`module/testcase/tao.php:372`）。`lib/form/form.class.php:160-225` 展示规则如何落地：
     `linkages` 对 rawdata 求值、`required = field.rules 包含 notemptyRule.id`。
3. **用例级评审状态**：`zt_case.status ∈ {wait(待评审), normal, blocked, investigate}` +
   `reviewedBy/reviewedDate`；`zt_casespec(case, version, ...)` 每版本快照；
   `zt_casestep(parent, case, version, type∈{step,group})` 步骤是**树**；
   `zt_case.lib + fromCaseID + fromCaseVersion` = **用例库跨产品复用**，
   `fromCaseVersion == version` 即"已同步"；评审会 `zt_review/zt_reviewresult/zt_reviewissue`
   （issue 带 `injection`/`identify` 阶段字段 → 缺陷预防度量）。

**B. 生效环节 / 注入方式**
- 创建：`$config->testcase->create->requiredFields='product,title,type'`
  → `dao->batchCheck(..., 'notempty')` + `checkFlow()`（服务端硬校验）。
- 评审闸门：`getStatusForCreate()`（`module/testcase/zen.php:3090`）
  `if (forceNotReview() || !post->needReview) return 'normal'; return 'wait';`
  `forceNotReview()`（`model.php:1449-1462`）= needReview 关时只有 `forceReview` 名单里的人仍要评审；
  needReview 开时 `forceNotReview` 名单里的人豁免 —— **按用户覆盖规则**。
  硬闸门：`testtask->getAllLinkableCases()` 里 SQL 过滤 `status != 'wait'`
  —— **未评审用例不能挂进测试任务**；`isClickable('runcase')` 亦要求 `status != 'wait'`
  （`model.php:1187/1195`，逐行按钮态由规则决定）。
- AI 注入（**最关键的一条**）：`getWorkflowPromptFields(module, action)`
  （`module/ai/model.php:2306`）取该页工作流字段 → `buildDynamicSchema(fields, prompt, isBatch)`
  （`model.php:2566`）**把用户定义的字段元数据编译成 JSON Schema**（enum 来自 `field.options`、
  required 来自 `field.required`、编辑器控件 → `format:'html'`），用于
  **function-calling**（`getWorkflowFunctionCallSchema`）；`ensureWorkflowTargetForm/
  buildWorkflowTargetFormVars` 把 AI 产出落回工作流定义的目标表单。
  → **"AI 只能输出用户定义过的字段"，规则元数据 == AI 的输出契约。**
  另有 `$config->ai->injectAuditButton->locations['testcase']['create'|'edit'|'batchcreate']`
  与 `zin/wg/aiforminject/v1.php`（自动嵌进每个 formpanel），
  session 10 分钟审计窗；`filterPromptsForExecution()` 按权限/状态过滤"谁能看到哪些提示词"。
- AI 资产表：`zt_ai_model / zt_ai_agent(code,type∈{normal,timer},model,knowledgeLib,skill,
  module,displayPosition,actionPurpose,operation∈{report,risk,notify},cycleType,
  notifyRule JSON,targetForm,purpose,role,status) / zt_ai_agentfield(appID,name,type,
  placeholder,options,required) / zt_ai_prompt / zt_ai_knowledgelib(acl,groups,users) /
  zt_ai_knowledgeitem(objectType,objectID,objectData JSON)` —— **AI 的输入表单字段本身也是 DB 里用户配的**。

**C. 前端入口**：后台→自定义（`module/custom`，含 `set/required/flow` 动作页）配必填字段与流程；
工作流（max 版）是可视化设计器，有 `lib/zin/wg/workflowfield`、`workflowcondition`
（conditionType∈{data,sql} + 逻辑运算符）、`workflowfieldcondition` 组件，`zt_workflowrule` 有独立管理页。
规则**就地显示**：表单上必填高亮、用例列表"待评审"状态 + 行级 review 按钮。

**D. 命中/违反统计**：`zt_action(objectType,objectID,actor,action,comment,extra,project,...)`
+ `zt_actionproduct/zt_actionrecent` + **`zt_history(action, field, old, oldValue, new, newValue, diff)`**
（字段级 diff，详情页"历史记录"）；`status='wait'` 本身就是可筛选的规则未满足态；
`zt_reviewissue.injection/identify` = 评审有效性度量。

### 6.2 TestLink —— 三表作用域 + 分阶段取值（字段模型的教科书）

**A. 规则数据模型**（`install/sql/mysql/testlink_create_tables.sql`）
- `custom_fields`(:159) = **定义，全局唯一 name**：`label, type, possible_values,
  default_value, valid_regexp, length_min, length_max`，加
  **六个阶段开关** `show_on_design/enable_on_design、show_on_execution/enable_on_execution、
  show_on_testplan_design/enable_on_testplan_design`。
- `cfield_node_types(field_id, node_type_id)` = 作用于哪类节点（testsuite/testcase/
  requirement/plan/build…）。
- `cfield_testprojects(field_id, testproject_id, display_order, location, active, required,
  required_on_design, required_on_execution, monitorable)` = **按项目作用域 + 覆盖必填/顺序/位置**。
- **取值按阶段分表**：`cfield_design_values(field_id,node_id,value)`(:107)、
  `cfield_execution_values(field_id,execution_id,testplan_id,tcversion_id,value)`(:116)、
  `cfield_testplan_design_values`(:148)、`cfield_build_design_values`
  —— 同一字段在"设计/执行/计划设计"各存各的值，执行值还按 (plan,tcversion) 区分。
- 类型注册表在代码里（`lib/functions/cfield_mgr.class.php:80-92`）：0 string/1 numeric/2 float/
  4 email/5 checkbox/6 list/7 multiselect/8 date/9 radio/10 datetime/20 textarea/
  **500 script/501 server**；可经 `$tlCfg->gui->custom_fields->types` 扩展。
- `locations`(:135-146) 给用例表单定义 8 个命名槽位，含 `before_preconditions`、
  **`hide_because_is_used_as_variable`**（该字段不在 UI 渲染，只作 `[tlVar]` 模板变量 →
  **字段即生成文本的输入**，正对应"AI 填模板变量"）。
- 关键词 `keywords(id,keyword,testproject_id)` + `testcase_keywords(testcase_id,tcversion_id,
  keyword_id)`（**标签按版本作用域**）+ 通用挂载 `object_keywords`(:615)。
- 计划关联规则：`testplan_tcversions(..., urgency)`，`testPlanUrgency.class.php:151,216`
  算 **`priority = urgency × importance`** 并分 HIGH/MEDIUM/LOW。
- 插件：`plugins(basename, enabled)` + `plugins_configuration(testproject_id, config_key,
  config_type, config_value)`（**按项目覆盖插件配置**，`plugin_config_get(...,$project)` 全局兜底）。

**B. 生效环节 / 注入方式**：设计期 `get_linked_cfields_at_design()`
（`testcase.class.php:4949-4958`，先 `getTestProjectFromTestCase()` 沿节点树反查项目）→
`html_table_of_custom_field_inputs()` 按 `location` 渲染进表单；执行期/计划设计期各自渲染。
校验：HTML `required` + `class="required"`（来自按项目的 required 位，`cfield_mgr.class.php:627`）
+ 浏览器端 `gui/javascript/cfield_validation.js::validateCustomFields()`。
事件钩子：`lib/functions/events_inc.php` 注册
`EVENT_TEST_CASE_CREATE/UPDATE/DELETE`、`EVENT_TEST_REQUIREMENT_*`、`EVENT_EXECUTE_TEST`、
`EVENT_TESTRUN_DISPLAY` + UI 槽位事件；插件 `tlPlugin.class.php:92 hooks()` 返回钩子表。
> **重大反面教材**：`valid_regexp / length_min / length_max` **存了、能导出（cfieldsExport.php:94）、
> 但全仓库没有任何一处真正执行**（grep 只出现在 SELECT 与注释里）。规则没有服务端执行点=装饰品。
> 评审类型 `$tlCfg->review_types`（`cfg/const.inc.php:850`，可自定义 typo/recommendation/
> question/unclear/major problem）在当前分支只有配置没有流程 UI。

**C. 前端入口**：有**专门规则页** `lib/cfields/`：`cfieldsView.php`（列表）、
`cfieldsEdit.php`（按节点类型驱动 show_on/enable_on 组合，`cfieldsEdit.tpl:55-92`）、
**`cfieldsTprojectAssign.php`**（分配/取消/重排 + active/required/monitorable 布尔批量，
`:19-46`）、`cfieldsImport.php/cfieldsExport.php`（XML）。字段就地出现在资产页的指定槽位。
`cfg/userRightMatrix.php` 把每个页面 URL 映射到所需权限。

**D. 命中/违反统计**：`events(id, transaction_id, log_level, source, description, fired_at,
activity, object_id, object_type)` + `transactions`（一次请求一组），
`logAuditEvent()`（`logging.inc.php:37`）写 AUDIT 级；字段的 assign/unassign/activate/
deactivate/required/monitorable 变更都有 `audit_cfield_*` 记录
（`cfield_mgr.class.php:1078,1106,1144,2950-2963`），在 `lib/events/eventviewer.php` 查看。
`monitorable` 位存在但**没有任何报表消费它**。

### 6.3 Kiwi TCMS —— 极简：状态位闸门 + 名称:值 属性 + 字段级历史

> 事实更正：老 `django-custom-fields / ExtraField` **在当前 master 已不存在**（回查至 v5.3 标签
> 只有 `extra_link` URL 字段与 `Property`）。所以"把自定义字段作用域到 product+version+plan"
> 的答案是：**Kiwi 不做作用域**，只有按对象的自由 name:value。

- **A**：`tcms/core/models/abstract.py::Property(name, value)`（abstract）→
  `tcms/testcases/models.py:222 class Property(abstract.Property): case = FK(TestCase)`；
  `TestCase`(:51) 有 `case_status FK`、`category FK(按 Product unique_together)`、`priority`、
  `author/default_tester/reviewer` FK、`plan M2M through TestCasePlan(plan,case,sortkey)`
  （`unique_together=("plan","case")`）。`TestCaseStatus(name, description, is_confirmed)`(:18)
  —— **规则性信息只有一个布尔位**。`Template(name, text)`(:382) 全局、无作用域、无类型。
  `Environment + EnvironmentProperty`、`TestExecutionProperty` 同构 name:value。
- **B**：
  - 起草期：`Template` 纯前端预填 —— `tcms/testcases/static/testcases/js/mutable.js:5`
    `$('#id_template').change(... codemirror.setValue(...))`，**服务端零校验**。
  - **评审闸门**：`tcms/rpc/api/testrun.py:39`
    `if not case.case_status.is_confirmed: raise RuntimeError(f"TC-{case.pk} status is not confirmed")`
    —— 唯一确认的服务端硬闸门：**未确认用例不能建执行**。
    `TestCaseAdmin`/`TestCaseStatus` admin 有"至少保留一个已确认/一个未确认状态"的删除保护
    （`testcases/admin.py:42-61`）。
  - 执行期：**属性并集 × 矩阵展开** —— `TestRun.create_execution()` 把
    run 级 property ∪ case 级 property 做笛卡尔积（或 `allpairspy` 两两组合），
    每个组合生成一条 `TestExecution` 并快照 `TestExecutionProperty`
    （`tcms/testruns/models.py:119-155`）—— **简单 name:value 规则驱动执行变体扇出**。
  - 扩展点：`entry_points().select(group="kiwitcms.plugins")` 注入 INSTALLED_APPS 与菜单；
    `EXTERNAL_BUG_TRACKERS` 是点路径类名列表（`tcms/settings/common.py:511-520`），
    `BugSystemAdmin.save_model` 会**实时探活**集成（`testcases/admin.py:182-228`）。
- **C**：无独立规则页，全在 Django admin（Template/TestCaseStatus/Category/BugSystem/PlanType/
  Environment）；Property 就地编辑 `tcms/testcases/templates/testcases/get.html:141`
  `{% include "include/properties_card.html" %}`。
- **D**：`KiwiHistoricalRecords`（`tcms/core/history.py:78-122`）给任意模型加历史表，
  `pre_save` 取旧实例、`post_save` 用 `diff_objects()` 生成**逐字段 unified diff** 存进
  `history_change_reason`，`ReadOnlyHistoryAdmin` 只读展示，还能 `history_email_for()` 发邮件。
  没有"规则命中"统计（因为几乎没有规则）。

---

## 7. 交叉结论与落地映射（对我们平台）

### 7.1 各工具 A/B/C/D 对照表

| 工具 | 规则形态 | 作用域 | 生效环节 | 注入方式 | 前端入口 | 命中统计 |
|---|---|---|---|---|---|---|
| promptfoo | Assertion{type,value,threshold,weight,metric} / redteam policy 文本 | 全局 defaultTest（可单点关）/ scenario / contexts | 生成(policy→探针) + 判定 + 报告 | 注册表 ASSERTION_HANDLERS + Nunjucks 渲染判官 prompt | eval-creator 向导 + Custom Policies 页(可 AI 建议/CSV 导入) | namedScores + metric + reason + renderedGradingPrompt |
| DeepEval | GEval{criteria,steps,rubric,threshold} / DAG 判定树 / BaseMetric 子类 | 代码实例化，按 test case | 判定 | 框架拼 prompt + CoT 生成步骤 | 无（pytest） | score+reason 逐指标；verbose 打节点 |
| OpenAI evals | registry YAML + modelgraded 模板(prompt/choice_strings/eval_type)；现行 Graders API 四对象(StringCheck/LabelModel/ScoreModel/Multi+公式) | 目录叠加（.evals 覆盖包内） | 判定 | 模板 + 选项解析；判官即数据可组合 | CLI | accuracy + 元评估 metascore 验规则本身 |
| SonarQube | Rule / ActiveRule(严重度+参数覆盖) / Profile / Gate(条件作用于度量) | 档案→项目(按语言)，档案可继承 | 分析期逐文件；门禁期聚合 | 档案下发给分析器 | Rules/Profiles/Gates 三页 | issue 分面计数 + 逐条件 EvaluatedCondition + 档案 changelog |
| NeMo Guardrails | config.yml rails.{input,retrieval,dialog,execution,output}.flows + Colang flow | 配置目录级 | **五阶段显式** | 事件流订阅 | 无（仓库文件） | 事件日志 + 逐护栏 TP/FP |
| Guardrails AI | Validator{on_fail} 绑到字段路径；RAIL XML | 字段/Guard 级 | 输出后（含流式） | 注册表 + schema + reask 回环 | Hub 按 URI 安装 | ValidationSummary(字段×校验器) + error_span |
| Langfuse | Rule(filter+采样+时间窗) × Evaluator(版本化,LLM/代码/决策模型) × ScoreConfig | 项目；filter 类型化条件 | 落库流式 + 回填 + 实验 | DB 数据驱动 + variableMapping | **专门 Rules/Evaluators 页** + 测试面板 + 成本预估 | JobExecution 逐次留痕 + blockReason + upgradeRequired |
| GitHub rulesets | Ruleset{target fnmatch, rules[], bypass_actors, enforcement} | 分支/标签/仓库/组织 | 写入/合并时 | 服务端聚合，**最严优先** | Settings→Rules；只读可见 | Evaluate 模式 Rule Insights |
| Schemathesis | `@check` 函数 / 类(after_response+after_run) + 每校验 kwargs | **4 层**：项目→阶段→操作→操作+阶段 | 逐响应 + 整轮 | 注册表 + TOML/env 文件 | 无（hooks.py/conftest.py） | CheckNode 逐条 + 身份去重 + 分组报表 + 基线豁免(expires) |
| Robot | 用户关键字（表格式 + 嵌入参数） | 文件/资源/套件 | 执行 | 名称解析器 + Dynamic API | .robot 文本 + libdoc | per-tag/per-keyword 统计 |
| Cucumber | 步骤定义 + 自定义参数类型 + 标签钩子 | 目录 require | 执行 | SupportCodeLibrary 注册表 + 标签表达式 AST | feature+JS 文件 | **UsageFormatter：每条定义命中次数 / UNUSED** |
| Karate | `match` DSL + 模糊标记 + 可 call 的 feature | 项目(config.js)/feature | 逐步 | DSL 解析 + 校验器映射 + Ext SPI | .feature 文本 | 逐步 match diff + events.jsonl |
| Kiwi TCMS | Property(name:value) + TestCaseStatus.is_confirmed + Template | 按对象（无作用域层） | 执行期硬闸门 | 服务端 raise | Django admin + 就地卡 | 字段级 history diff |
| TestLink | custom_fields(6 阶段开关) × node_types × testprojects(required/位置/顺序) | **按项目 + 按节点类型** | 设计/执行/计划设计三阶段各自取值 | DB 元数据渲染表单 + 事件钩子 | **cfields 专门页** | events 审计 + monitorable(未被消费) |
| 禅道 | zt_config 覆盖 + workflowfield.rules → workflowrule 库 + 审批节点 | **workflowgroup 绑组织对象** | 表单/保存/checkFlow/按钮态 | 元数据编译成 **AI function-call JSON Schema** | 后台自定义页 + 工作流设计器 | zt_action+zt_history diff + review issue injection/identify |

### 7.2 反复出现的 8 条设计公理

1. **规则必须显式声明生效阶段**（NeMo 五轨 / TestLink 六开关 / Schemathesis 阶段层 /
   Langfuse timeScope）。我们应有 `rule.stages = [generate, review, validate, report]` 多选，
   并且**不同阶段可存不同结论**（TestLink 的分阶段取值表）。
2. **"作用域"是独立的一等对象**，不是隐式继承：Langfuse filter+targetObject、
   TestLink `cfield_testprojects`、禅道 `workflowgroup`、SonarQube profile→project、
   GitHub ruleset target。**定义与绑定分表**（否则无法按项目覆盖必填/严重度/顺序）。
3. **继承要有状态位而不是拷贝**：SonarQube `NONE/OVERRIDES/INHERITED` + 父档案链 +
   compare/changelog API；GitHub "无优先级、聚合、取最严"；promptfoo `defaultTest` +
   `disableDefaultAsserts` 单点退出。
4. **规则需要"违反后动作"属性**：Guardrails `on_fail ∈ reask/fix/filter/refrain/exception/
   fix_reask/custom/noop`；NeMo rail-outcomes；SonarQube 门禁 ERROR vs（旧）WARN。
   我们的语义应为：仅提示 / 阻断 AI 提交 / 回炉重生成 / 转人工。
5. **影子模式（只评估不拦截）是规则上线的正确姿势**：GitHub `Evaluate` + Rule Insights；
   Langfuse 的 test panel + 采样率 + 成本预估 + backfill；OpenAI evals 的 meta-eval（用人类
   choice 标签验证判官规则本身准不准）。
6. **规则可用性/命中必须可观测**：Cucumber `UsageFormatter`（逐定义命中次数 + `UNUSED`）；
   Schemathesis 逐 case CheckNode + 按违规身份去重 + 分组计数；promptfoo `metric`→namedScores；
   Langfuse `JobExecution`（含 error/耗时/成本 + `blockReason` 自动停用）；
   SonarQube profile `lastUsed` + 规则 changelog。
7. **规则的粒度要能"逐违规豁免"而不是整条关**：Schemathesis baseline 文件带 `expires`。
8. **规则元数据可以直接变成 AI 的输出契约**：禅道 `buildDynamicSchema(workflowfield)` →
   function-calling schema；promptfoo `assert-set/policy` → 生成 + 判分双模板。
   这条对我们最有价值：**用户配的字段/规则决定 AI 只能产出什么**。

### 7.3 建议的数据模型（综合 15 个参照）

```
Rule（定义，全局或项目内可共享）
  id, name, description(自然语言规则原文), kind ∈ {structural, semantic, coverage, process}
  severity / weight            ← SonarQube impacts + promptfoo weight
  stages[]: generate|review|validate|gate   ← NeMo / TestLink
  on_violate: warn|block_ai_commit|regenerate|to_human   ← Guardrails on_fail
  enforcement: active|evaluate|disabled     ← GitHub rulesets（先影子跑）
  check:                       ← 三选一（可混）
    - type+params  （注册表里的确定性校验，promptfoo/Schemathesis 式，如 must_have_step_type）
    - prompt_rubric（自然语言判据 + 可选 steps/rubric 区间，DeepEval G-Eval 式）
    - code         （用户写脚本，Langfuse CODE / promptfoo javascript 式）
  generator_hint               ← promptfoo plugin.generator：规则如何影响"造用例"
  required_assets              ← 规则对 AI 输出契约的字段约束（禅道 schema 编译）
RuleBinding（作用域与覆盖，独立表）
  rule_id, scope_type ∈ {global, project, module/catalog, requirement, case, api}
  scope_ref, matcher(类型化条件数组，抄 Langfuse filters：column/operator/value/type)
  enabled, required(bool), params_override{}, severity_override{}, sampling, time_scope
RuleSet / Profile（分组、可继承、可被选为默认）  ← SonarQube Profile + GitHub rulesets
RuleExecution（逐次留痕）                     ← Langfuse JobExecution + Schemathesis CheckNode
  rule_id, binding_id, asset_id, stage, status(pass|violated|error|skipped),
  score, reason, evidence(span/字段路径), cost/tokens, judge_prompt_rendered
RuleStat 视图：每条规则 命中数/违反率/误报申诉数/UNUSED 告警 + changelog
BaselineWaiver（rule_id, asset_id, expires） ← Schemathesis baseline
```

生成期注入：把当前作用域解析出的规则集渲染成 AI 提示词块（带规则 ID，要求 AI 逐条回执
`applied_rules[]`）；校验期：同一批规则走 `check.type`/`code` 硬判 + `prompt_rubric` 软判，
产出 `RuleExecution`；评审期：人确认界面按规则显示"这条用例违反了哪几条 + 证据 + 申诉/豁免入口"；
报表期：`RuleStat` + 门禁（对聚合度量设条件，抄 SonarQube Gate，而不是对单条用例设条件）。

### 7.4 要避开的三个坑（都有实锤）

1. **TestLink**：`valid_regexp/length_min/length_max` 存了、导出了、**从未被执行**
   —— 只在前端渲染。任何"没有服务端执行点"的规则等于没做。
2. **Kiwi TCMS**：`Template` 只做前端预填、零校验；`Property` 无类型/无 schema/无作用域
   —— 规则退化成自由文本，无法统计也无法约束 AI。
3. **promptfoo `guardrails` 断言**：官方文档明确警告"响应里没有 guardrails 字段时按
   `flagged:false` 处理，于是断言白拿满分"—— **缺证据 ≠ 通过**。
   我们生成回执 `applied_rules[]` 时，"未声明"必须判为违规而非合规。
   另注：Schemathesis 文档也警告 `after_run` 在 pytest-xdist 多进程下只看局部数据（会假过），
   跨全量规则必须在单一聚合点执行。