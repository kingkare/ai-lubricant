# AI 测试平台

测试域设计文档与实施计划。

## 文档

| 文档 | 内容 |
|---|---|
| [`design.md`](./design.md) | **设计文档**：定位、理念、总体架构、知识体系、资产体系、能力体系、流程体系、质量体系、专项测试、数据治理；第二部分为技术设计 |
| [`test-project-definition.md`](./test-project-definition.md) | **测试项目与知识引擎定案**（根设计，纠正"项目"三处理解偏差；双轨数据源/版本/release/力导向图/阶段划分） |
| [`gap-audit.md`](./gap-audit.md) | 落地缺口审计（设计 34 表 vs 实际建表，逐项补齐记录） |

## 核心模型（一句话）

```
标签 ──划分用途，任务按标签选，不直接选用例
用例 = 数据 + 场景引用 + 标签       ← 业务视角（测什么，不含实现细节）
场景 = 步骤集合 + 局部变量          ← 实现视角（怎么测），可被多个用例关联
步骤 = 类型 + 描述 + 参数/方法 + 后置校验   ← 类型化
方法脚本 = 基础脚本库 + 项目脚本库   ← Python 代码（全异步）
任务 = 场景标签 + 执行类型 + 通知 + 用途 + 标题 + 全局参数
```

## 实施计划

按「先做方法，不接流程/agent」推进：先立确定性核心（资产模型 → 引擎 → 校验器），再叠任务/报告，最后接知识与 AI。

| # | 计划 | 内容 | 状态 |
|---|---|---|---|
| 01 | [资产模型](./plans/01-asset-model.md) | 用例/场景/步骤/标签/方法脚本的数据模型 | 设计已定 |
| 02 | [任务模型](./plans/02-task-model.md) | 任务（标签选例 + 执行类型 + 通知 + 用途 + 全局参数），人/AI 可操作 | 设计已定 |
| 03 | [报告模型](./plans/03-report-model.md) | 报告实体与页面（用例/bug/需求/版本/用时/响应时间） | 设计已定 |
| 04 | [执行与结果](./plans/04-execution-and-results.md) | run/run_scenario/step_result/evidence、三模执行、失败分流 | 设计已定 |
| 05 | [环境管理](./plans/05-environment.md) | 多套环境、分层继承、类型化变量、密钥脱敏 | 设计已定 |
| 06 | [脚本引擎](./plans/06-script-engine.md) | 异步引擎、变量系统、步骤类型分发、方法脚本 | 设计已定 |
| 07 | [校验器与门禁](./plans/07-validators-and-gates.md) | 校验规则、八道门禁落点 | 设计已定 |
| 08 | [Agent 规则强制](./plans/08-agent-rule-enforcement.md) | **平台级底座**：如何让任意 agent 强制遵守规矩（四层防御） | 设计已定 |
| 09 | [知识体系建设](./plans/09-knowledge-system.md) | **平台级底座**：工具即抽取器、四层存储、图谱、查询、保鲜 | 设计已定 |
| 10 | [dsh 接入](./plans/10-dsh-integration.md) | 官方 Python SDK 实测（安装/API/工具走 MCP/环境坑） | 实测已入库 |
| 11 | [测试项目域（阶段 1）](./plans/11-test-project-phase1.md) | 4 张新表 + 19 个方法 + 代码快照 + 图谱 query 的落地记录 | **已完成** |
| 12 | [双轨数据源 + 用例扫描（阶段 2）](./plans/12-source-dual-track.md) | A/B 轨 + L1 原始层 + 用例扫描 + agent 落地（过闸）的落地记录 | **已完成** |
| 13 | [阶段 3/4/5 收口](./plans/13-phase-345-closeout.md) | 工作流断点 + 分析师编排 + 版本捕获/影响面 + 可靠性的落地记录 | **已完成** |
| 14 | [前端可视化（阶段 5 前端）](./plans/14-frontend-visualization.md) | 测试项目管理页 + 力导向图谱页 + 可靠性仪表的落地记录 | **已完成** |
| 15 | [dsh 接入 + MCP server + 对话](./plans/15-dsh-chat-mcp.md) | dsh 驱动 + Streamable HTTP MCP server（107 方法）+ 对话后端 SSE 的落地记录 | **已完成** |
| 16 | [四件遗留收口](./plans/16-remaining-closeout.md) | 对话持久化 / 覆盖矩阵边 / analyst 自动收口 / 定时源同步的落地记录 | **已完成** |
| 22 | [测试策略](./plans/22-test-strategy.md) | 冒烟/全量/回归/性能四类策略：勾选用例 + 分配人员 + 测试时间 + 测试结果；bug 归属 = 测试版本 × 测试策略；项目/版本角色成员（测/开/产）；列表行内编辑 + 用例/Bug/报告三视图（含 P0-P1、解决时长、延期分析） | **已完成** |
| 23 | [缺陷域对齐禅道](./plans/23-defect-zentao-parity.md) | 补枚举/字段/状态机：resolution 8 值 + bug_type 10 值 + confirmed 位 + activated_count + 双 build + deadline + escaped；7 态状态机 + 动作守卫表（7 动作）+ `mc_test_bug_log` 活动流 + 周期报表（MTTR/逃逸率）+ 前端筛选/彩标/动作菜单 + **缺陷目录（mc_test_catalog type=bug，左树右表）** | **已完成** |
| 24 | [用例生成重构·18Skills 落地](./plans/24-case-generation-multiagent-pipeline.md) | 方法基底与流程框架（18Skills 为基底、方法/技巧/技术三分、外部 skill 融合表 R1-R11） | **框架已定** |
| 25 | [测试平台·完整设计](./plans/25-case-generation-complete-design.md) | v4.1 终版（三轮评审 58 处缺陷全闭合，附录 B/C 对照）：五环节大架构 → 数据模型（gen_run/testpoint/proposal/question/coverage_cell/rule 五新表+question 扩列+执行结果四态）→ run+需求双层状态机 → 逐环节六项式（门⓪需求评审+拷问协议、测试点分派、探索门按内容分、数据五作用域、双评一致性、棘轮、重开）→ 思维导图UI → agent 底座+白名单 → 用户规则四环节 → 18 Skill 落位 → 模型分层 → **分期含 P0/P1 落地记录** | **P0+P1 已落地** |
| 25r | [25号文评审记录](./plans/25-design-review-findings.md) | 三评审agent原文：数据/UI一致性15条+流程完整性16条+18Skills对照（v3已逐条闭合，附录有对照表） | 存档 |

> 后续（用例生成、缺陷闭环、根因、沉淀回流）在方法层立住后另开计划。24 号文为用例生成的重构方向，尚未动代码。

## 测试项目 · 阶段推进（见 [test-project-definition.md](./test-project-definition.md) §九）

| 阶段 | 内容 | 状态 |
|---|---|---|
| **1** | 测试项目 CRUD + git 配置 + 代码源 init/sync + 图谱 query | **已完成**（4 张新表 + `project.*` 19 个方法 + `projects/` 模块 + 测试） |
| 2 | 双轨数据源（内置 Excel/JSON + `source.fetch` 通道）+ 用例扫描 | **已完成**（A 轨 7 格式 + B 轨 agent 通道 + `mc_knowledge_raw` L1 + `case.scan` + `knowledge.put`） |
| 3 | PRD agent 摄取（analyst 工作流）+ 锚定 + 可靠性 | **已完成**（`analyst.prepare` 编排 + `knowledge.put` 过闸 + `reliability.report`） |
| 4 | 版本 release + diff | **已完成**（`release.capture` 自动采集 + `release.diff` + `release.impact` 增量选例） |
| 5 | 力导向图前端 + 可靠性仪表 | **已完成**（`/testing/graph` 力导向图 + `/testing/projects` 项目管理） |

## 关键设计决策（已定，不再列为待确认）

| # | 事项 | 决策 |
|---|---|---|
| 1 | 内联代码（步骤 `code` 字段） | **v1 不开放**，走方法脚本 + 自定义工具 |
| 2 | 知识锚点实现 | **Graphify（唯一底座）**：Python 库同进程直调（code-only + markdown 纯函数，零 LLM key），实测 15.6s/156 文件、92% EXTRACTED；endpoint 走 OpenAPI 导入；**不引入 CodeGraph、不需 sidecar**（09 §4.9 实测定） |
| 3 | 测试 bug 与现有 issue 关系 | ~~bug = ProjectIssue 复用~~ **已推翻**（2026-10）：bug 是测试域自持的 `mc_test_bug`（独立实体，`project_id` 直接是测试项目 id），与 coding 项目无关；状态机与字段对齐禅道（23 号文） |
| 4 | 方法脚本源码存储 | **DB text**（同库、事务一致、便于 diff） |
| 5 | 引擎运行位置 | **服务端进程内**（确定性执行，设备/浏览器操作走现有控制通道下达，无节点回传问题） |
| 6 | 工具运行位置 | **服务端 + commit 快照**（Graphify 同进程 import；无 sidecar） |
| 7 | 报告生成/导出 | **生成时快照**；v1 导出 HTML 单文件 |

> 设计层只留"要什么"的取舍；"怎么做"由设计自己给答案（各计划文末"设计决策"节）。
