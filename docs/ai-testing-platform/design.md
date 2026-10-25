# AI 测试平台 · 设计文档（正式版）

> **定位**：本服务内的一个能力域，与 GA 无关，不借用其概念。
> **目标**：把平台从"开发工具"升级为"生产平台"——测试是**平级角色**，不是开发的附属环节。
> **范围**：只讲框架、理念、体系；不涉及表结构、代码实现、部署步骤。

---

## 目录

1. [定位](#一-定位)
2. [设计理念](#二-设计理念)
3. [总体架构](#三-总体架构)
4. [知识体系](#四-知识体系)
5. [资产体系](#五-资产体系)
6. [能力体系](#六-能力体系)
7. [流程体系](#七-流程体系)
8. [质量体系（门禁）](#八-质量体系门禁)
9. [专项测试](#九-专项测试)
10. [数据治理](#十-数据治理)
11. [与现有服务的关系](#十一-与现有服务的关系)
12. [设计要点回顾](#十二-设计要点回顾)
13. [业界参考与取舍（对比 WHartTest）](#十三-业界参考与取舍对比-wharttest)

---

## 一、定位

平台从**开发工具**升级为**生产平台**。

开发只是其中一个角色。本设计要补的是**测试者**——一个与开发者**平级**的角色，拥有自己完整的知识、资产、能力与流程。

测试不是"给开发流程加一个环节"，而是**增加一个独立的角色**：它有自己看世界的方式（测试视角的知识）、自己产出的东西（用例、脚本、缺陷、根因）、自己的判断标准（门禁与准则）。

---

## 二、设计理念

| # | 理念 | 说明 |
|---|------|------|
| 1 | **知识是公共资产** | 抽取出来的知识服务于整个平台——开发能用、测试能用。测试只是在它之上多了一层测试视角。 |
| 2 | **AI 不裁判，框架裁判** | AI 负责"想"和"写"，但**是否合格由框架判定**。这是整个设计的根——没有它，AI 的产出无法信任。 |
| 3 | **确定性的归确定性，智能的归智能** | 执行脚本、跑断言、校验规范——这些是确定性的，不需要 AI。分析、归纳、设计、生成、诊断——这些才需要 AI。两者严格分开。 |
| 4 | **对 AI 固定，对人可扩展** | AI 只能在**已注册的能力**范围内组合；人要加新能力，走评审流程。可控性的来源。 |
| 5 | **一切皆可沉淀** | 每完成一次工作，经验（优质脚本、缺陷模式、规范约定）都回流成资产，让下一次更准。 |
| 6 | **分层解耦，变更局部化** | 知识、用例、脚本、工具各自独立分层。改一处不动全身。 |
| 7 | **变更必有因，产出必有责** | 用例和脚本的任何变更都要记录理由；用例的变更必须**显式传导**到关联脚本。设计者对自己设计的用例负责到底——不是设计完就撒手。 |
| 8 | **疑问不靠猜** | 分析和设计中**必然**产生疑问（产品不确定、设计存疑）。疑问作为一等公民进入流程，通过调试、文档或用户确认闭合，不能靠 AI 假设填平。 |

---

## 三、总体架构

```
┌──────────────────────────────────────────────────────┐
│                   AI 测试者（角色）                    │
│     规划 → 分析 → 设计 → 编码 → 执行 → 调试 → 报告      │
├──────────────────────────────────────────────────────┤
│                    测试知识引擎                        │
│   需求 · 规则 · 用例 · 缺陷 · 文档 · 代码 · 接口        │
│   历史 · 证据 · 覆盖 · 结果                            │
├──────────────────────────────────────────────────────┤
│                   软件知识图谱                         │
│          代码图谱 + 知识化 + 业务规则                   │
│        ├── 代码    ├── 文档    ├── 业务规则             │
└──────────────────────────────────────────────────────┘
```

**三层关系：**

| 层 | 是什么 | 作用 |
|---|--------|------|
| **软件知识图谱** | 最底层，是平台对"系统真相"的认识：代码是什么、文档说什么、业务规则怎么定义 | 公共底座，开发/测试共用 |
| **测试知识引擎** | 在底座之上，组织出**测试视角**的知识：要测什么、测过什么、坏过哪里、覆盖如何 | 测试域专用 |
| **AI 测试者** | 最上层，是执行测试的**角色** | 从知识引擎取知识、向它写产出 |

---

## 四、知识体系

知识来自三个源：

```
                      知识源
                        │
     ┌──────────────────┼──────────────────┐
     ▼                  ▼                  ▼
  产品知识           技术知识           测试知识
  ────────          ────────          ────────
  PRD               CodeGraph          人工用例
  产品文档            API                AI 用例
  接口文档            DB                 历史 Bug
  业务规则            MQ                 测试报告
  ADR               Config              测试结果
  Wiki              Dependency          缺陷根因
```

| 源 | 回答什么 | 性质 |
|---|----------|------|
| **产品知识** | 应该做什么 | 业务侧定义 |
| **技术知识** | 实际是什么 | 技术侧实现 |
| **测试知识** | 测过什么、坏在哪 | 测试侧产出（既是输入也是产出） |

**三源独立抽取、独立成资产。** 抽取后的知识是平台公共资产——开发查产品知识 + 技术知识，测试查全部。

**知识 ≠ 用例。** 知识是对世界的结构化认识；用例是一个流程说明。写用例**参考**知识，但知识的存在**不为了用例**。两者是独立实体。

---

## 五、资产体系

测试者产出的东西，四类资产，各自独立。

### 5.1 用例（业务视角）

用例只说"要测什么、预期是什么"。**用例 = 数据 + 场景引用 + 标签。** 它**不含实现细节**（不含步骤）。

> 为什么不把步骤写进用例：用例若解释"每一步干啥"，会写得很详细，**用例的重点反而抓不住**。步骤属于场景。

**用例的创建准则（必须确认，不能凭空）：**

一条用例的产生，其依据必须来自**可确认的来源**之一：

| 来源 | 确认方式 |
|---|----------|
| **调试** | 通过实际运行/复现确认 |
| **文档** | 通过 PRD/接口文档/业务规则确认 |
| **用户确认** | 产品不确定时，由用户拍板 |

**产品不确定 → 必须走用户确认。** 分析或设计中发现产品定义不清、行为有歧义时，用例**不得自行假设**，必须挂起为"待确认"，交用户裁决后才成立。

**用例的变更准则：**
- **变更必须有原因**——每次修改记录理由（需求变、理解纠正、覆盖补充……）
- **用例的变更传导到场景**——用例变了，关联场景**必须同步评估/更新**
- **设计者对用例负责**——用例不是"生成完就交差"，设计者对其正确性、可执行性负责到底

### 5.2 场景（实现视角）

**场景 = 步骤集合 + 局部变量管理。** 它的作用是**实现用例**。一个场景可被多个用例关联（因为它是"实现某一类步骤的集合"）。

```
标签 ──划分用途（任务按标签选，不直接选用例）
   │
用例 = 数据 + 场景引用 + 标签      ← 业务视角
   │  ↕ 多对多
场景 = 步骤集合 + 局部变量         ← 实现视角
   │
步骤 = 类型 + 描述 + 参数/方法 + 后置校验   ← 类型化
   │
方法脚本 = 基础脚本库 + 项目脚本库   ← Python 代码住这里
```

**场景的步骤类型化：**

| type | 用途 |
|---|---|
| `request` | 发送请求 |
| `page` / `android` / `ios` | 操作页面 / 移动端 |
| `wait` | 等待 |
| `script` | **执行方法脚本**或内联代码 |
| `check` | **校验数据** |
| `sql` / `ws` / `custom` | 数据库 / WebSocket / 自定义工具 |

**后置校验类型化**：可断言、可 SQL、可**执行方法脚本**。

**参数灵活**：多参数（结构化）、可**执行一个方法**（同步/异步均可）、可内联代码。

**局部变量管理**：场景内声明 `local_vars`，步骤 `post_cond` 写入、后续步骤读取；前置场景的变量**传递**给当前场景——这就是**上下场景互相使用**的机制。

### 5.2b 方法脚本（Python 代码的家）

| 分类 | 内容 |
|---|---|
| **基础脚本**（平台提供） | 请求管理类、缓存类（登录 token 缓存）、网页操作类、Android/iOS 操作类、时间类、随机类、加解密类…… |
| **项目脚本** | 项目特定需要的类 |

**异步约定**：框架层**全异步**；步骤内方法**允许同步/异步**，框架统一适配。

### 5.2c 脚本创建准则（机器强制）

| 准则 | 含义 |
|---|---|
| **设计提取，不要从头到尾** | 场景**必须尽可能拆分**，可复用部分提取为方法/自定义工具 |
| 结构边界 | 各层职责不得越界 |
| 复用优先 | 同逻辑二次出现必须提为方法/工具 |
| **校验必需** | 每个步骤至少一条后置校验 |
| **环境变量** | 环境相关的值必须走环境变量，不硬编码 |
| 变量规范 | 作用域明确、命名规范、来源可溯 |
| 依赖白名单 | 只能用注册工具/方法 |
| 错误处理 | 用声明的错误策略，禁止吞异常 |
| 清理对称 | 前后置对称，资源必释放 |
| 幂等可重跑 | 重复执行不污染 |
| 数据解耦 | 数据不硬编码进场景 |
| 溯源完整 | 场景→用例→需求 链完整 |

**核心不变式：AI 只能组合已注册的工具/方法写场景；要新能力必须走评审流程。**

**用例 ≠ 场景。** 用例是"测什么"，场景是"怎么测"，二者解耦（但用例变更必须传导到场景）。

### 5.3 缺陷与根因

失败 → 分类 → 提缺陷 → 根因分析 → 修复 → **回流**。

根因分析的产出必须**带证据链**——每条结论可溯源到代码位置、日志、证据。无证据的结论不入库。

### 5.4 沉淀（测试技术）

> 让不同 AI 产出一致的关键。

四件套：
- **约定**：命名/目录/变量/错误处理规范
- **模板**：骨架，AI 填空
- **黄金样例**：已验收的优质脚本，作参照
- **校验器**：机器判定"是否符合规范"

**一致性 = 约定(读) + 模板(填) + 样例(仿) + 校验器(判)。**

沉淀分三层：通用（跨框架）→ 框架级 → 项目级。AI 按当前上下文**按需加载**相关那层。

---

## 六、能力体系

### 6.1 AI 测试者的七个角色

```
规划 → 分析 → 设计 → 编码 → 执行 → 调试 → 报告
```

| 角色 | 职责 | 性质 |
|---|---|---|
| 规划 | 制定整体测试策略 | 智能 |
| 分析 | 归纳知识、找缺口 | 智能 |
| 设计 | 设计测试方案 | 智能 |
| 编码 | 生成用例与脚本 | 智能 |
| **执行** | 跑脚本、判结果 | **确定性** |
| 调试 | 分析失败、定位根因 | 智能 |
| 报告 | 汇总质量视图、沉淀知识 | 智能 |

**这七个是角色，不是固定的程序。** 谁来完成是部署选择。

### 6.2 Agent 抽象（本阶段不选路）

平台**不绑定任何具体 agent**。测试域对外只暴露四个面：

| 面 | 是什么 |
|---|---|
| **工具方法** | 原子可调用单元（最底层） |
| **MCP** | 工具方法的 MCP 暴露，任何会说 MCP 的 agent 都能调 |
| **接口** | 契约：输入输出、能力保证、域边界 |
| **AI 工作流** | 七个角色的编排定义 |

这样，"接 pi 还是 dsh 还是别的"是**部署选择**，不是架构改动。换 agent 不改设计。

**执行这类确定性工作流根本不需要 agent**——它由脚本引擎直接跑。

### 6.3 任务（每次启动的用途不一样）

> 不能保证所有用途是同一个作用。不同测试场景下**没必要整个用例集跑一遍**，应该有侧重点。

**任务 = 一次有侧重、有标签、有通知的启动。**

| 任务包含 | 说明 |
|---|---|
| **场景标签** | 按标签选例（不是直接选用例）——因为跑的时候是**按标签划分用途** |
| **执行类型** | 多维度启动：手动 / 定时 / 事件 / 接口 |
| **通知配置** | 执行结果需要通知（通道 + 时机 + 内容） |
| **任务用途描述** | 这次跑是为了什么 |
| **任务标题** | |
| **全局参数** | 本次任务所有场景共享的参数 |

**任务提供接口，人和 AI 都能添加/修改。**

### 6.4 标签（划分用途）

用例带**标签**，任务**按标签选用例**。标签有分类（用途 / 优先级 / 模块 / 测试类型），任务可按不同维度组合选例（如"模块=支付 且 优先级=P0"）。

---

## 七、流程体系

完整流水线，14 段，**循环**：

```
多源知识 → 分析归纳 → 知识模型 → 测试设计 → 用例
   → 脚本生成 → 脚本验证 → 执行测试 → 结果分析 → 缺陷分析
   → 缺陷提交 → 根因分析 → 测试报告 → 质量沉淀 ↺
```

| 段 | 承担角色 |
|---|---|
| 多源知识 / 分析归纳 / 知识模型 | 分析 |
| 测试设计 / 用例 | 设计 |
| 脚本生成 / 脚本验证 | 编码 |
| 执行测试 / 结果分析 | 执行（确定性） |
| 缺陷分析 / 缺陷提交 / 根因分析 | 调试 |
| 测试报告 / 质量沉淀 | 报告 |

**规划**贯穿全程：先定策略，再动态编排其余角色。

**关键：这是循环，不是直线。** 最后一段的产出回流第一段——优质脚本进沉淀，缺陷模式进知识，覆盖事实更新画像。系统**越用越准**。

### 7.1 疑问与确认（流程的一等公民）

> 分析与设计过程中**一定会产生疑问**。这些疑问不能消失，必须显式表达并收敛。

**疑问的两类来源：**

| 来源 | 例子 |
|---|---|
| **产品侧不确定** | PRD 没写清、行为有歧义、规则冲突——"这里到底该返回什么？" |
| **设计侧疑问** | 分析脚本/产品时发现的问题——"这个接口契约和文档不一致？" |

**疑问必须显式提出，不允许 AI 自行假设填平。** 疑问进入流程后，有明确的收敛路径：

```
疑问提出
   │
   ├── 调试验证（跑一下就知道）─────────┐
   ├── 文档/代码确认（查得到答案）──────┤
   └── 用户确认（产品不确定时必须走）────┤
                                        ▼
                                  结论回写知识 / 用例
```

**三类收敛方式：**
1. **调试**——能通过实际运行确定的，自己确定
2. **文档/代码**——能从既有材料确定的，自己确定
3. **用户确认**——产品不确定、设计有取舍的，**必须由用户裁决**

### 7.1.1 疑问分诊（提交用户后的第一步是"归类"）

> **上一步"用户确认"不是终点，是起点。** 提交给用户的疑问，第一件事是**判断它属于哪一类**——因为类型决定后续走哪条路。

**关键：不能把疑问直接拍成决策，也不能直接扔回去。** 要先分析它是否合理、是否存在问题，再**归类**：

| 类型 | 含义 | 后续 |
|---|---|---|
| **bug** | 已有定义被违反 / 系统缺陷 / 理解错了 | 走修 bug 流 |
| **需求问题** | 产品未定义、行为有歧义、要拍板 | 走产品决策 |
| **设计问题** | 设计不自洽、约束冲突 | 走设计修订 |
| **问错 / 信息不足** | 问题本身有问题、超范围 | 补上下文后重问 |

**与失败分流同构**：这跟 §04 的失败分流（`script_issue` / `product_bug` / `case_outdated` / `env_flaky`）是同一个思路——**先归类，再决定走哪条路**。

**两条硬规则**：
1. **不许 AI 自行假设填平**（无论多小的疑问）——自己拍 = 越权
2. **不许把未分析的疑问直接扔回用户**——先分析合理性 + 给出归类建议，再交用户裁决

```
疑问 → 分析（合理性？有无问题？）→ 归类建议 → 提交用户裁决
                                                    ↓
                                         用户判类型 → 走对应流程
```

**疑问在设计上的体现：**
- 疑问是流程中的**显式产物**，不是"跑完就没了"的中间态
- 每个疑问有状态：`open → 已解决`，附解决方式与依据
- **未解决的疑问会阻塞下游**——用例带着 open 疑问，不得进入脚本生成；脚本带着 open 疑问，不得进入执行
- 疑问是**用例确认（5.1）的来源之一**：产品侧疑问由用户确认后，才成为用例成立的依据

**"不是设计了用例就对自己设计的用例不负责"** —— 设计者要对自己产生的疑问负责到底：提出它、追踪它、收敛它。

---

## 八、质量体系（门禁）

> 整个设计的信任基础：**AI 不裁判，框架裁判。**

每个环节都有门，产物不达标不放行：

| 门 | 判什么 |
|---|---|
| 知识门 | 结构化 + 可溯源 + 可测 |
| 生成门 | 契约合法 + 只用已注册工具 + 可断言 + 可溯源 |
| **校验门** | **脚本写没写校验**——每步、每个关键结果都有可判定校验 |
| **确认门** | **用例依据是否可确认**（调试/文档/用户确认）；**未解决的疑问是否已收敛** |
| 执行门 | 断言由框架机械判定 |
| 保证门 | 可执行 + 可稳定通过 + 有增量价值 |
| 根因门 | 每条结论带证据引用 |
| **变更门** | **每次变更是否记录理由**；用例变更是否已传导到关联脚本 |

**门禁是机器，不是 AI。** 这是"不同 AI 产出一致"的机制保证。

---

## 九、专项测试

功能、性能、契约、集成、Mock——都是**测试类型**，共享同一套知识、资产、流程、门禁。差异只在**驱动**和**判定准则**。

| 类型 | 判定 |
|---|---|
| 功能测试 | 断言（浏览器 / 移动端 / 接口） |
| 性能测试 | 阈值（客观质量门）+ 历史基线 |
| 契约测试 | 契约一致性（只管请求/响应形状） |
| 集成测试 | 真实依赖 + Mock |
| Mock 测试 | 契约即代码 + 录制回放 |

**Mock 是资产**：以文本形态存在，可被 AI 读写、版本化、复用——与脚本同等待遇。

---

## 九、报告体系

> 报告是**一等实体**（有实体、有页面、可归档），不是对结果表的临时查询。

**报告包含的维度：**

| 维度 | 内容 |
|---|---|
| **用例列表及用例详情** | 本次跑的清单 + 每条步骤结果/断言/证据 |
| **关联 Bug** | 本次发现 + 相关缺陷 |
| **关联需求** | 用例↔需求映射与覆盖 |
| **测试版本** | 被测版本 / 构建号 |
| **用时** | 总耗时 / 各用例耗时 |
| **响应时间** | 各请求步骤耗时、分布、最慢 Top N |
| **趋势** | 历史通过率对比 |

**接入方式暂时空着**——报告的生成/渲染实现先不定，但**实体、字段、页面结构定死**（见 `plans/03-report-model.md`）。

---

## 十、数据治理

所有实体统一遵守三条：**稳定 ID、显式状态机、版本 + 溯源**。

**出入准则：**
- **进**：过保证门才入库，不达标降级为"候选"
- **改**：只增版本，不覆盖历史；**每次变更必须记录理由**
- **废**：标失效，不物理删除，通知依赖方
- **删**：仅限误建，人工确认

**用例/脚本的变更传导：**
- 用例变了 → 关联脚本**必须评估并更新**（或标记待更新）
- 脚本变了 → 记录原因（用例驱动？工具升级？环境适配？）
- **疑问是变更的来源之一**：产品侧疑问收敛后，可能触发用例修订

**溯源与依赖**：每个实体记录来源和依赖。**源变更 → 沿依赖链标记失效**——这是"代码变了，哪些用例要重测"的机制基础。

---

## 十一、与现有服务的关系

测试是**本服务内的一个能力域**，与 GA 那套无关，不借用其概念。

- **独立**：测试域有自己的知识、资产、能力、流程定义
- **共存**：与服务的其它能力域平级，共用服务的存储、通知、路由等基础设施
- **边界**：只通过接口 / MCP 与外部交互

---

## 十二、设计要点回顾

1. 测试是**平级角色**，不是开发流程的附属环节
2. 知识是**公共资产**，知识 ≠ 用例
3. 用例 ≠ 脚本；**分层针对脚本**，不针对用例
4. **确定性的归确定性**——执行不走 AI
5. **AI 不裁判，框架裁判**——门禁是信任基础
6. **对 AI 固定，对人可扩展**——能力注册制
7. **一切皆可沉淀**——闭环让系统越用越准
8. **Agent 是抽象**——不绑定具体实现
9. **变更必有因，产出必有责**——用例/脚本变更记录理由，用例变更传导脚本
10. **疑问不靠猜**——产品不确定必须用户确认；疑问显式表达、显式收敛、阻塞下游
11. **脚本必须写校验**——每步每关键结果可判定
12. **脚本用环境变量**——环境相关值不硬编码，且**分层继承、类型化**
13. **脚本要拆分，不要从头到尾**——设计提取、复用优先
14. **执行有三模**——脚本优先、混合、纯 AI，按用例可选

---

## 十三、业界参考与取舍（对比 WHartTest）

> 参考对象：https://github.com/MGdaasLab/WHartTest （Django + Vue + Playwright Actuator + MCP + Skills，1105 文件）

### 13.1 值得吸收的四点

**① 三模执行引擎（per case 可选模式）**

WHartTest 的每条用例可选 `hybrid / script_only / ai_only`，hybrid = 先用脚本跑、失败后 AI 介入诊断修复。这是工业级容错，且**与我们的"确定性优先"理念完全一致**。

落到我们的设计：
- **script_only**：纯脚本执行（确定性，默认）
- **hybrid**：脚本优先，失败触发 AI 诊断（我们的 Debugger 角色）
- **ai_only**：无脚本，纯 AI 执行（个别探索式用例，我们已预留）

三者共用同一套门禁——**AI 介入时仍不能自判 pass/fail**，只是多了一层"脚本跑不动时的兜底"。

**② 环境变量：分层继承 + 类型化**

WHartTest 的 `ApiEnvironment` 支持**父子继承 + 递归合并**（dev→test→staging→prod 只配差异），且变量有 **7 种类型**（string/int/float/boolean/json/list/dict），不是全字符串。

落到我们的设计（5.2 环境变量）：
- 环境**分层继承**：项目基线 → 环境组 → 具体环境，只配差异
- 变量**类型化**：保持 JSON 原生类型，避免"数字变字符串"导致的断言误判
- 防循环继承校验

**③ 变更传导：字段级选择性同步**

WHartTest 的 `ApiSyncConfig` 把"接口字段 → 用例步骤字段"**逐字段绑定**，接口变更时**只同步受影响字段**（不是全量重生成），并有历史 + 回滚。

落到我们的设计（十 数据治理）：
- 用例→脚本的传导**不是全量重建**，而是**按关联字段选择性同步**
- 传导留历史，可回滚
- 这直接实现我们说的"用例变更传导到脚本"，且避免"改一个小字段触发整个脚本重生成"

**④ 失败诊断：结构化输出 + 自愈建议**

WHartTest 的 `ai_diagnosis_service` 返回结构化 JSON：`root_cause_type` + `confidence` + `healing_suggestion`。

落到我们的设计（5.3 缺陷与根因）：
- 根因除证据链外，增加 **置信度** 与 **自愈建议**（脚本问题时的修复方向）
- 自愈建议直接喂给我们的 Debugger 角色做脚本修复（hybrid 模式的闭环）

### 13.2 WHartTest 缺失、我们已覆盖的（守住优势）

| 它有？ | 我们有 |
|---|---|
| 无知识图谱（仅文档 RAG） | Software Knowledge Graph（代码/文档/业务规则） |
| 无脚本分层（HttpRunner + 自定义函数） | 工具层/自定义工具层/脚本层/创建准则 |
| 用例无"待确认"态、无疑问追踪 | 疑问一等公民 + 用户确认门 |
| 无校验器概念 | 沉淀四件套含**校验器** |
| 无统一数据治理 | 稳定ID/状态机/版本+溯源 |
| 无 Mock / 性能 / 契约 | 设计已含 |

**结论**：吸收它工程上踩实的四点机制；我们体系上的完整性（知识图谱、脚本分层、疑问确认、校验器、数据治理、专项测试）**保持不动**。

### 13.3 暂不采纳（已核实仓库，记录原因与事实）

| 点 | 事实 | 原因 |
|---|---|---|
| **LangChain 中间件组合** | 四中间件（ModelRetry/ToolRetry/Summarization/HITL）是 **WHartTest 自己进程内**的 agent 编排层，从 `langchain.agents.middleware` 导入，在 `create_agent()` 里组装。**与被测系统无关**（agent 的工具操作 SUT，中间件只编排它自己的 agent 循环） | 它**绑定 LangChain v1 运行时**；我们把 agent 抽象为接口/MCP/工作流，不绑定 runtime，不照搬形态。但它解决的**问题**（重试/摘要/人工审批可组合）我们要，对应我们的"能力面接口 + 门禁"。**结论不变，理由修正为"不绑 runtime"** |
| **Skills 技能包管理** | 三条导入通道全为"用户提供包/URL"：本地 zip、Git URL、远程 zip URL。"Skill 中心"是**静态 manifest 商店**（默认源=其仓库 GitHub raw，用户可自配任意源）。**无榜单/评分/市场索引**——不是三方市场 | 与我们的自定义工具层 + 沉淀层职责重叠；资产量起来后再评估其 manifest 打包形态 |
| **Recorder 录制入库** | **自研录制器**：服务端 Django spawn Node → Playwright 无头 Chromium + `addInitScript` 注入捕获脚本 + CDP `Page.startScreencast` 推帧 + 前端 WebSocket 回传输入。**完全在服务端**（非扩展、非 codegen、非 Actuator） | 依赖服务端录制端，实现成本高、非核心路径；V2 再议。**不是 Playwright codegen，是自研** |

---
---

# 第二部分 · 技术设计

> 基于第一部分设计文档落地的技术层：**数据结构 + 服务结构 + 接口契约**。
> 约定：沿用服务存储惯例（Postgres + Tortoise ORM + UUID 主键 + `mc_` 表前缀）；测试域表统一 `mc_test_*` 段，**域逻辑独立于 GA，不复用其表与概念**，仅共用 PG/Redis/通知等基础设施。

---

## 十四、域边界与服务结构

### 14.1 边界

- 测试域是**本服务内的独立能力域**：有自己的表、状态机、门禁、能力面。
- **不依赖 GA**（GenericAgent / agents / mc_agent_skills 等一概不碰）。
- **共用基础设施**：Postgres、Redis、附件存储、通知通道、鉴权中间件。
- 对外只通过**能力面**（工具方法 / MCP / 接口 / AI 工作流）交互。

### 14.2 模块划分（服务结构）

```
testing/                          # 测试域根（独立于 GA）
├── knowledge/                    # 知识引擎
│   ├── sources/                  #   知识源适配（PRD/代码/bug/用例库/文档）
│   ├── extractor/                #   抽取与结构化（精华提取）
│   ├── graph/                    #   知识图谱（item/anchor/edge/profile）
│   └── gates/                    #   知识门
├── assets/                       # 资产域
│   ├── cases/                    #   用例 + 确认 + 变更传导
│   ├── scripts/                  #   脚本（场景/步骤/传输/代码/数据集）
│   ├── tools/                    #   工具层注册表 + 自定义工具
│   └── environments/             #   环境（分层继承 + 类型化变量）
├── engine/                       # 脚本引擎（确定性，不走 AI）
│   ├── interpreter/              #   场景编排解释
│   ├── drivers/                  #   驱动层（web / mobile / api）
│   ├── assertions/               #   断言库（机械判定）
│   └── variables/                #   变量系统（{{}} 模板/作用域/环境合并）
├── runtime/                      # 执行调度
│   ├── runs/                     #   test_run 生命周期
│   ├── triage/                   #   失败分流（脚本/产品/用例过时/环境）
│   └── modes/                    #   三模执行（script_only/hybrid/ai_only）
├── quality/                      # 质量体系
│   ├── gates/                    #   八道门禁
│   ├── validators/               #   校验器（沉淀四件套之一）
│   └── questions/                #   疑问追踪与确认（一等公民）
├── defects/                      # 缺陷域
│   ├── bugs/                     #   缺陷工单
│   ├── root_cause/               #   根因分析（证据链/置信度/自愈建议）
│   └── patterns/                 #   缺陷模式（沉淀回流）
├── craft/                        # 沉淀域（约定/模板/样例/校验器）
├── surface/                      # 能力面（对外唯一出口）
│   ├── methods/                  #   工具方法（原子能力）
│   ├── mcp/                      #   MCP 暴露
│   ├── api/                      #   接口契约（schema/保证/边界）
│   └── workflows/                #   AI 工作流定义（7 角色）
└── reports/                      # 报告与质量沉淀
```

### 14.3 平台能力依托（**已逐条核对实际代码，2026-09-28**）

> 本文所有"复用既有能力"的声称，均已对着平台真实实现核验过。出处在此，实现时直接看这些文件——**不要凭印象**。

| 我们声称复用的 | 核对结果 | 实际出处 | 我们怎么用 |
|---|---|---|---|
| **列级加密** | ✅ 真实存在（AES-256-GCM + 域分离密钥派生） | `server/browser_password_crypto.py` | `is_secret` 环境变量照此模式加密；密钥优先级 env→域分离派生→`.env` 兜底 |
| **通知通道** | ✅ 钉钉/飞书/企微/webhook（各带签名细节） | `user_platform/notify_dispatch.py` | 任务通知只声明通道+时机+内容 |
| **移动推送** | ✅ Expo push | `user_platform/notify_push.py` | `mobile_push` 通知类型 |
| **email 通知** | ❌ **平台不存在** | — | 02 原设计误列，**已删**（如需要需另建） |
| **定时调度** | ✅ APScheduler + CronTrigger（标准 5 位 cron + interval） | `agent/scheduler.py` | 任务 `schedule` 直接复用该模式 |
| **附件/证据存储** | ✅ `attachments` 表 + TTL + sha256 | `server/attachment_store.py` | 证据（截图/日志/trace）经 `register_attachment` 落库 |
| **Git 客户端** | ✅ 正好 7 家（github/gitlab/gitea/gitee/codeup/cnb/atomgit） | `user_platform/git_clients.py` | 知识索引的 commit 快照拉取 |
| **设备控制** | ✅ `Driver.call(cmd, args, timeout_ms)` **服务端异步直调**（不经 MCP 也可） | `mcp_builtin/device_control/driver.py` | 引擎的 `android`/`ios` 步骤直接调它 |
| **浏览器通道** | ✅ cdp_bridge MCP + Python `CdpClient` | `mcp_sdk/cdp.py`、`mcp_builtin/cdp_bridge/` | 引擎的 `page` 步骤 |
| **bug 生命周期** | ✅ `mc_test_bug`（测试域自持，状态机 `open→triaged→fixed→verified→closed/rejected`） | `testing_platform/defects/` | 与 coding 解耦，独立于 `ProjectIssue` |
| **审计** | ✅ `record_tool_audit` + `agent_tool_audit_logs` 表 | `agent/guardian.py` | 强制层 L4 审计复用 |
| **一次性审批卡** | ✅ `file_write_approval` / `code_run_approval` | `agent/file_write_approval.py` | 强制层逃生口（人批）范式 |
| **存储惯例** | ✅ PG + Tortoise + UUID + `mc_` 表前缀 | 全平台 | 测试域表 `mc_test_*` 沿用 |
| **消息流（事件）** | ✅ ClickHouse `task_messages` + PG 兜底 | `integrations/clickhouse.py` | 仅供对比；**测试运行结果直接写 PG**（不走该链路） |

**核对方法**：grep/读真实源码 + 注释，逐条比对文档声称。**原则**：设计写"复用既有"时必须同时写**出处文件**，否则视为未验证。

---

## 十五、数据结构

> 每域列核心实体与关键字段（非最终 DDL）。三条治理铁律贯穿全部实体：**稳定 ID、显式状态机、版本 + 溯源**。

### 15.1 知识域（平台级底座，**不带 test 前缀**）

> 命名修正：知识是**平台公共资产**（开发也用），表名 `mc_knowledge_*`；只有测试派生视图才带 `mc_test_` 前缀。

**`mc_knowledge_source`** — 知识源注册
`id, kind(product|tech|test), name, source_type(prd|wiki|api_doc|codegraph|db_schema|case_lib|bug_lib|report_lib), location(JSON), sync_policy(on_demand|scheduled|webhook), project_id, last_synced_at, status`

**`mc_knowledge_item`** — 知识条目（精华）
`id, source_id, kind(rule|component|api|db_table|mq|config|dependency|bug_pattern|doc_ref|coverage_fact), key, title, content(JSONB), status(candidate|confirmed|stale), confidence, provenance(JSONB 溯源), version, project_id`

**`mc_knowledge_anchor`** — 代码锚点
`id, project_id, path, symbol, kind(function|class|endpoint|route|table), content_hash`

**`mc_knowledge_edge`** — 知识图谱边
`id, from_item_id, to_item_id, relation(defines|uses|implemented_by|covered_by|derived_from|conflicts_with)`

**`mc_test_profile`** — 每锚点测试画像（测试派生视图，带前缀）
`anchor_id(unique), rule_ids[], component_ids[], bug_pattern_ids[], template_ids[], coverage_gaps(JSONB), last_tested_at, test_status(green|flaky|red)`

### 15.2 疑问域（一等公民）

**`mc_test_question`** — 疑问
`id, project_id, origin(product_uncertainty|design_doubt), raised_by(角色), context(JSONB 发现位置+关联实体), content, status(open|resolved), resolution(debug|doc|user_confirm), resolution_detail(JSONB 依据), resolved_at, blocking_refs(被阻塞的下游 case/script id)`

> 未解决疑问**阻塞下游**：用例带 open 疑问不得进脚本生成；脚本带 open 疑问不得进执行。

### 15.3 用例域

**`mc_test_case`** — 用例（业务视角：描述 + 步骤说明 + 数据 + 场景引用 + 标签）
`id, code(TC-xxxx 稳定ID), project_id, title, intent(描述), preconditions(JSONB 前置条件), expected(JSONB 业务预期=断言依据), steps(JSONB 测试步骤说明 [{seq,action,expected}] 自然语言), dataset_id(数据), confirm_source(debug|doc|user_confirm 确认依据), confirm_ref, origin(ai_generated|manual|from_history|from_requirement 来源), priority(P0-P3), module(功能模块=分组维度), feature, version_id(→ mc_test_project_version 关联版本), test_type(功能|接口|UI|AI|性能|异常|安全…), automation_status, status(draft|pending_confirmation|active|deprecated|archived), version, created_by(设计者=责任人)`
> **修正（2026-10-06）**：用例补 `steps`（业务视角的**自然语言步骤说明**，与场景的可执行步骤分层——用例描述「怎么算过」，场景描述「怎么跑」）与 `origin`（内容来源，与 `confirm_source` 确认依据正交）。
> `test_type` 枚举扩展为可理解的业务类型（functional/api/ui/ai/performance/exception/security，旧口径值保留兼容存量）。
> 可执行步骤仍属场景（§15.4）；标签走关联表。

**`mc_test_case_scenario`** — 用例↔场景（多对多）
`id, case_id, scenario_id, is_primary(bool), order`

**`mc_test_tag`** — 标签（任务按标签选例）。注：与 `ProjectIssue.tags`（free-form JSON）不同——测试标签需 `category(purpose|priority|module|type)` 支撑组合筛选，故独立表。
`id, code, project_id, name, category, description`

**`mc_test_case_tag`** — 用例↔标签 `id, case_id, tag_id`

> **版本机制统一（修正）**：不再另建 `mc_test_case_revision`——用例/场景/工具的**版本与变更理由统一走 `mc_test_change_record`**（§15.12），`field_changes` 即完整 diff，可重建任意时刻状态。一处版本真相。

### 15.4 场景域（实现视角；**原"脚本域"更名——同一东西，统一叫场景**）

> 修正：此前 design 用 `mc_test_script`，plans 已定 `mc_test_scenario`——同一东西两个名字。**统一为场景**；`mc_test_code` 已删（Code 层 v1 不开放，Python 代码统一住方法脚本 §15.5）。

**`mc_test_scenario`** — 场景（步骤集合 + 局部变量）
`id, code(SCN-xxxx), project_id, name, purpose, transport_id(鉴权/公共数据), env_id(默认环境,可被任务覆盖), local_vars(JSONB 局部变量定义+初值), flow(JSONB 条件/分支/提前结束/引用前置场景), exec_mode(已停用), status(draft|verified|active|broken|archived), version`
> 用例↔场景多对多（§15.3 关联表），**不再用 case_ids[] 数组列**。
> **修正（2026-10-06）**：场景级三模 `exec_mode(script_only|hybrid|ai_only)` **已停用**——列保留兼容存量，create/update 不再写、校验不再查。执行语义下沉到**步骤类型**：新增 `ai_judge`(AI判断)/`agent_action`(Agent操作)/`agent_workflow`(Agent工作流)，由具体步骤声明性质，而非整场景一刀切。
> **执行通道**：三类 AI 步骤由 ``run.execute`` 注入的 ``ctx.ai``（:mod:`testing_platform.ai_channel`）真跑——ai_judge 交网关 LLM 判定（判定结果作 actual，仍由步骤 post_checks 机械裁定，**AI 不直接判 pass/fail**）、agent_action 驱动平台 Agent、agent_workflow 开工作流运行。引擎包本身不 import agent/LLM（``test_engine_never_calls_llm`` 护栏），通道是注入的鸭子协议；执行含 AI 步骤的场景须给 ``run.execute`` 传 ``api_key_id``。

**`mc_test_scenario_step`** — 步骤（类型化）
`id, scenario_id, step_key(场景内唯一,被 flow 引用), seq(唯一顺序真相), name, description(步骤描述), type(request|page|android|ios|wait|script|check|sql|ws|custom|ai_judge|agent_action|agent_workflow), params(JSONB 结构随 type 变,支持{{变量}}/method_ref), pre_cond(JSONB 前置条件), post_checks(JSONB 类型化后置校验,必填≥1), post_cond(JSONB 后置赋值→写局部变量,局部变量流转机制), on_error(retry|reinit|evict|fail|skip|abort), enabled`

**`mc_test_transport`** — 传输层（鉴权/公共数据）
`id, code, kind(http|ws), auth(JSONB 登录流程+re_login_on,凭据只存引用), common_headers, common_params, path_rewrite(JSONB), status, version`

**`mc_test_dataset`** — 数据集
`id, code, project_id, schema(JSONB), rows(JSONB), version`

**`mc_test_environment`** — 测试环境（分层继承；**≠ 平台的 TaskEnvironment**——那是"代码在哪跑的执行环境"，这是"被测系统指向哪套 SUT 配置"）
`id, code, name, project_id, parent_id(基线→环境组→具体环境), is_default, status`

**`mc_test_env_variable`** — 环境变量（类型化）
`id, env_id, key, value(JSONB), type(string|int|float|boolean|json|list|dict), is_secret`
> 继承时递归合并 + 防循环继承校验；secret 脱敏。

### 15.5 方法与工具域

**`mc_test_method_module`** — 方法脚本模块（**Python 代码唯一居所**；`kind=base` 平台基础库，`kind=project` 项目库）
`id, code(base.crypto|proj.payment), project_id(null=全局), kind(base|project), name, description, source(Python 源码), async_native, status, version`

**`mc_test_method`** — 方法注册表
`id, module_id, name, full_ref(base.crypto.sign), signature(参数/返回契约), is_async, description`

**内置工具层**：代码注册表（非表）。`name, params_schema, returns_schema, driver(web|mobile|api), on_error_policy`。例：`http / ws / db / wait / browser / mobile / mock / perf`。

**`mc_test_custom_tool`** — 自定义工具（沉淀载体；**只拥有签名+指向，不拥有实现**——impl_ref 只能指向公共场景或方法，杜绝两处实现）
`id, code, scope(project|team), purpose(业务语义), signature(JSONB 输入输出契约), impl_ref(→mc_test_scenario 或 mc_test_method.full_ref), status(draft|review|active|deprecated), version`

**`mc_test_custom_tool_version`** — `id, tool_id, version, snapshot(JSONB), change_reason`

### 15.6 沉淀域

**`mc_test_craft_asset`** — 沉淀四件套
`id, type(convention|template|sample|validator), scope(common|framework|project), target(scenario|case|tool), content(JSONB/text), version, status`
> AI 按当前上下文**按需加载**相关层。

### 15.7 执行域

**`mc_test_run`** — 测试运行
`id, project_id, trigger(manual|scheduled|webhook|regression), case_ids[](全量/增量选例), env_id, status(pending|running|done|aborted), summary(JSONB pass/fail/error/flaky/skipped), started_at, finished_at`

**`mc_test_run_scenario`** — 场景结果（执行单位是场景，非用例）
`id, run_id, scenario_id, case_refs[](引用该场景的用例), exec_mode(实际模式), status(pass|fail|error|flaky|skipped), failure_kind(script_issue|product_bug|case_outdated|env_flaky 分流), duration_ms, healing_applied(hybrid 自愈记录)`
> 用例级结果由场景结果**投影**得出（同一场景多用例只跑一次）。

**`mc_test_step_result`** — 步骤结果
`id, run_scenario_id, step_seq, status, expected, actual, diff(JSONB)`

**`mc_test_evidence`** — 证据
`id, run_scenario_id, step_seq, kind(screenshot|log|network|trace|db_state|assertion_diff), uri(附件存储)`

### 15.8 缺陷域（**测试域自持 `mc_test_bug`，与 coding 彻底解耦**）

> **修正（2026-10-06）**：推翻此前「bug = `ProjectIssue(issue_type=bug)`，复用 coding 实体」的决策。
> 测试缺陷与 coding **没有任何关系**：测试项目不需要关联 coding 项目，缺陷不再混入 coding 的 issue 流。
> 旧实现里建 bug 必须先给测试项目配 `coding_project_id` 桥，没配就建不了 bug——这是错误耦合，已移除。

**`mc_test_bug`** — 测试缺陷主表（**测试项目自持**）
`id, code(BUG-xxxx), project_id(→mc_test_project 测试项目，非 coding), title, symptom, status(open|triaged|fixed|verified|closed|rejected), severity(blocker|critical|major|minor|trivial), priority(1|2|3), summary, resolution_note, fingerprint(失败指纹·去重), repro(JSONB 复现维度), case_refs[], scenario_refs[], scenario_id, from_run_scenario_id, from_run_id, assignee_id, created_by, tags[], created_at, updated_at, closed_at`

**`mc_test_bug_root_cause`** — 根因分析（1:N，可迭代）
`id, bug_id(→mc_test_bug), location, classification(logic|boundary|concurrency|null|config|data|integration|requirement_ambiguity|regression|flaky), attributed_commit, hypothesis, confidence, fix_suggestion, healing_suggestion, blast_radius, evidence_chain(JSONB 证据链，根因门必填)`

**`mc_test_bug_pattern`** — 缺陷模式（沉淀回流，独立实体）
`id, symptom_pattern, root_cause_kind, prevention, regression_template, hit_count`

> 旧 `mc_test_bug_ext`（挂 coding issue 的扩展表）**退役**，字段并入 `mc_test_bug` 主表。
> 状态机由测试域自持（`open→triaged→fixed→verified→closed/rejected`），不再依赖 coding 的人工确认门。

### 15.9 工作流域

**`mc_test_workflow_def`** — 7 角色工作流定义
`id, role(planner|analyst|designer|coder|executor|debugger|reporter), input_schema, output_schema, tool_refs[](可调工具方法), contract(必须满足), nature(llm|deterministic)`
> executor 是 deterministic，不走 agent。

**`mc_test_workflow_run`** — 工作流运行实例
`id, workflow_def_id, run_id(可空), input(JSONB), output(JSONB), status(pending|running|done|failed|blocked_by_question), agent_runtime(由哪个 runtime 驱动=部署选择)`

**`mc_test_agent_binding`** — agent 绑定（本阶段留空）
`id, capability, runtime_kind(pi|dsh|custom|...), config(JSONB)`

### 15.10 任务域

**`mc_test_task`** — 任务（**≠ 平台 `mc_tasks`**——那是开发 agent 会话；这是"一次有侧重的测试启动"）
`id, code(TASK-xxxx), project_id, title, purpose(用途描述), scenario_tags(JSONB 按标签选例), case_filter(JSONB 附加筛选), exec_type(manual|scheduled|event|api), schedule(JSONB), env_id, global_params(JSONB), notify(JSONB 通道+时机+内容), exec_mode, status(draft|enabled|disabled|archived), created_by, version`

### 15.11 报告域

**`mc_test_report`** — 报告（一等实体，生成时快照）
`id, code(RPT-xxxx), project_id, run_id, task_id(null), title, test_version(被测版本), env_id, status(generating|ready|archived), snapshot(JSONB 概览/用例/bug/需求/耗时), generated_at, generator`

### 15.12 变更域

**`mc_test_change_record`** — 变更记录（变更必有因 + 传导；**唯一的版本机制**）
`id, entity_type(case|scenario|method|custom_tool|knowledge_item|task), entity_id, change_reason(必填), field_changes(JSONB 字段级diff), propagation(JSONB 受影响下游+同步状态), rollback_ref(可回滚)`

---

## 十六、状态机汇总

| 实体 | 状态流 |
|---|---|
| 知识条目 | `candidate → confirmed → stale` |
| 疑问 | `open → resolved`（附 resolution 方式） |
| 用例 | `draft → pending_confirmation → active → deprecated → archived` |
| 脚本 | `draft → verified → active → broken → archived` |
| 自定义工具 | `draft → review → active → deprecated` |
| 运行 | `pending → running → done / aborted` |
| 用例结果 | `pass / fail / error / flaky / skipped` |
| 缺陷 | **复用现有 bug 流**：`unassigned → diagnosing → reason_pending_confirmation → reason_confirmed → fixing → fixed → closed`（人工确认门内建） |
| 工作流运行 | `pending → running → done / failed / blocked_by_question` |

> `reason_pending_confirmation → reason_confirmed` 是**人工确认门**（现有 bug 流内建），agent 不得自行跨过。

---

## 十七、接口契约（能力面）

> 对外唯一出口。任何 agent（pi/dsh/自研）都通过这一面驱动测试域；换 agent 不改契约。

### 17.1 工具方法（原子能力）

| 方法 | 职责 | 性质 |
|---|---|---|
| `knowledge.query` | 按锚点/类型查知识 | 读 |
| `knowledge.ingest` | 从源抽取结构化知识（过知识门） | 写 |
| `knowledge.profile` | 取/更新锚点测试画像 | 读/写 |
| `question.raise` | 提出疑问（阻塞下游） | 写 |
| `question.resolve` | 收敛疑问（调试/文档/用户确认） | 写 |
| `case.create` | 建用例（须带 confirm_source，过确认门） | 写 |
| `case.update` | 改用例（必填 change_reason，触发传导） | 写 |
| `case.confirm` | 用户确认用例 | 写 |
| `scenario.generate` | 生成场景（过生成门+校验门） | 写 |
| `scenario.validate` | 校验场景（校验器） | 读 |
| `scenario.update` | 改场景（记录原因） | 写 |
| `tool.list` | 列已注册工具 | 读 |
| `tool.register` | 注册自定义工具（走评审） | 写 |
| `run.execute` | 执行测试（确定性） | 执行 |
| `run.status` / `run.result` | 查运行状态/结果 | 读 |
| `bug.create` | 提缺陷（= 建 `ProjectIssue(issue_type=bug)` + ext，复用现有） | 写 |
| `bug.triage` | 失败分流 | 写 |
| `bug.root_cause` | 根因分析（过根因门） | 写 |
| `craft.load` | 按需加载沉淀（约定/模板/样例/校验器） | 读 |
| `report.generate` | 生成报告 + 质量沉淀 | 写 |

### 17.2 MCP 暴露

工具方法经 MCP 注册暴露，任何会说 MCP 的 agent 可直接调用。MCP 层只做**协议适配 + 鉴权**，不含业务逻辑。

### 17.3 AI 工作流定义（7 角色）

每个角色 = 一条 `workflow_def`：`input_schema → output_schema → tool_refs → contract`。

| 角色 | 输入 → 输出 | 可调工具 | 性质 |
|---|---|---|---|
| planner | 目标+知识 → 测试计划 | knowledge.query, craft.load | LLM |
| analyst | 原始知识 → 知识模型+缺口 | knowledge.ingest, question.raise | LLM |
| designer | 知识模型+缺口 → 测试设计+用例 | case.create, question.raise | LLM |
| coder | 设计 → 场景 | scenario.generate, tool.list, craft.load | LLM |
| executor | 场景 → 执行结果 | run.execute | **确定性** |
| debugger | 失败证据 → 分类+根因+自愈建议 | bug.triage, bug.root_cause | LLM |
| reporter | 全部产出 → 报告+沉淀 | report.generate, craft.load | LLM |

---

## 十八、关键机制落点

| 机制 | 实现 |
|---|---|
| **变更传导（字段级选择性同步）** | `change_record.field_changes` → 匹配下游绑定 → 只同步受影响字段 → 留历史 → 可回滚。避免"改一小字段触发整脚本重生成" |
| **疑问阻塞** | case/script 引用 `question_id`；门禁检查未解决疑问 → 阻塞进入下一阶段 |
| **三模执行** | `run_scenario.exec_mode`；hybrid = 脚本优先 → 失败触发 debugger 工作流 → `healing_suggestion` → 修场景 → 重跑 |
| **门禁执行点** | 知识入库→知识门；用例创建→确认门；脚本生成→生成门+校验门；执行→执行门；入库→保证门；根因→根因门；一切变更→变更门 |
| **沉淀回流** | 运行结束 → 优质脚本进 `craft_asset(sample)`、缺陷模式进 `bug_pattern`、覆盖事实更新 `profile` |

---

## 十九、自动化脚本编写规范（逻辑结构与存储）

> **存储模型先澄清**：场景的**唯一事实源是数据库**（§15.4 的 `mc_test_scenario` / `mc_test_scenario_step` / `mc_test_transport` 等表）。页面渲染、编辑、查询、引用完整性、引擎执行**全部读表**。本节的 YAML 展示的是场景的**逻辑结构与交换格式**——用于 AI 生成产物、导入导出、版本备份；**不是**"把场景存成 YAML 文件"。文档经校验门后解析入库为表行。

| 层 | 形态 | 用途 |
|---|---|---|
| **存储（事实源）** | 关系表（见 15.4） | 页面对接、查询、引用、引擎执行 |
| **交换格式** | JSON/YAML 文档 | AI 生成产物、导入/导出、Git 备份 |
| **运行形态** | 引擎从表加载后编译 | 执行 |

数据模型、文档↔表映射、页面↔表对接的详细实现见 `docs/ai-testing-platform/plans/01-asset-model.md`。

### 19.1 脚本结构总览

一个脚本 = **传输声明 + 环境引用 + 步骤序列 + 场景编排**（逻辑结构如下，入库后拆为表行）

```yaml
# ===== 脚本头 =====
script:
  code: SCR-0042
  name: "用户下单全流程"
  case_refs: [TC-0101, TC-0102]     # 关联用例
  transport: login_transport          # 引用传输层
  env: staging                        # 引用环境（分层继承）
  exec_mode: script_only              # 三模之一
  dataset: order_dataset              # 引用数据集（可选）

# ===== 步骤序列 =====
steps:
  - id: create_order
    tool: http                        # 工具层（内置）
    params:
      method: POST
      url: "{{env.base_url}}/api/v1/orders"
      headers:
        Authorization: "Bearer {{auth.token}}"
        Content-Type: application/json
      body:
        product_id: "{{dataset.product_id}}"
        quantity: "{{dataset.quantity}}"
    pre_cond:                         # 前置条件（声明式）
      - assert: "{{auth.is_logged_in}} == true"
    assertions:                       # 校验（必填，每步必须有）
      - type: status_code
        expected: 201
      - type: json_path
        path: "$.data.order_id"
        operator: not_null
      - type: json_path
        path: "$.data.status"
        operator: equals
        expected: "created"
    post_cond:                        # 后置（提取变量供后续步骤用）
      - set: "{{local.order_id}} = {{response.json.data.order_id}}"
    on_error: retry(2)               # 错误策略

  - id: verify_order
    tool: http
    params:
      method: GET
      url: "{{env.base_url}}/api/v1/orders/{{local.order_id}}"
    assertions:
      - type: json_path
        path: "$.data.status"
        operator: equals
        expected: "created"
      - type: json_path
        path: "$.data.product_id"
        operator: equals
        expected: "{{dataset.product_id}}"

  - id: check_ui
    tool: browser                     # 浏览器工具
    params:
      action: navigate
      url: "{{env.web_url}}/orders/{{local.order_id}}"
    assertions:
      - type: element_visible
        selector: "[data-testid='order-status']"
      - type: element_text
        selector: "[data-testid='order-status']"
        operator: contains
        expected: "已创建"
```

### 19.2 传输层（Transport）

传输层定义**连接方式 + 鉴权 + 公共数据**，被多个脚本复用：

```yaml
transport:
  code: login_transport
  kind: http                          # http | ws
  auth:
    type: login_flow                  # 登录流程
    steps:
      - tool: http
        params:
          method: POST
          url: "{{env.base_url}}/api/v1/auth/login"
          body:
            username: "{{env.test_user}}"
            password: "{{env.test_pass}}"
        post_cond:
          - set: "{{auth.token}} = {{response.json.data.token}}"
    re_login_on: [401, 403]           # 遇到这些状态码自动重登
  common_headers:
    X-Request-Source: "auto-test"
  common_params:
    trace_id: "{{uuid()}}"
  path_rewrite:
    "/api/v1": "{{env.api_prefix}}/v1"
```

### 19.3 场景编排（Scenario）

场景层控制**条件分支、提前结束、局部变量、前置场景引用**。**步骤执行顺序以 `steps.seq` 为准**——场景中不重复声明顺序（两处真相必打架），下面示例的 `flow` 应移除，详见实现点 `plans/01-asset-model.md`。

```yaml
scenario:
  # 执行顺序由 steps.seq 决定，场景不重复声明

  # 条件分支
  conditions:
    - if: "{{dataset.quantity}} > 100"
      then: [create_order_bulk]       # 走批量下单步骤
      else: [create_order]            # 走普通下单步骤

  # 局部变量（步骤间传递）
  local_vars:
    order_id: null                    # 由 post_cond 填充
    retry_count: 0

  # 提前结束
  early_exit:
    - after: create_order
      when: "{{response.status}} != 201"
      reason: "下单失败，后续步骤无意义"

  # 引用前置场景（复用）
  pre_scenarios:
    - ref: ensure_login_scenario      # 先确保登录态
```

### 19.4 数据集（Dataset）

数据集提供**参数化数据**，一个脚本可跑多组数据：

```yaml
dataset:
  code: order_dataset
  schema:
    product_id: string
    quantity: int
    expected_status: string
  rows:
    - { product_id: "P001", quantity: 1, expected_status: "created" }
    - { product_id: "P002", quantity: 0, expected_status: "rejected" }
    - { product_id: "P003", quantity: 999, expected_status: "created" }
```

### 19.5 变量系统

| 作用域 | 语法 | 生命周期 |
|---|---|---|
| 环境变量 | `{{env.xxx}}` | 环境继承链（项目基线→环境组→具体环境） |
| 认证变量 | `{{auth.xxx}}` | 传输层登录后可用 |
| 局部变量 | `{{local.xxx}}` | 当前脚本内，步骤间传递 |
| 数据集变量 | `{{dataset.xxx}}` | 当前数据行 |
| 响应变量 | `{{response.xxx}}` | 当前步骤的响应 |
| 全局变量 | `{{global.xxx}}` | 跨脚本（慎用） |

**变量替换规则**：
- `{{变量}}` 在运行时替换为实际值
- 支持嵌套：`{{env.base_url}}/api/{{local.version}}/orders`
- 支持表达式：`{{dataset.quantity}} > 100`（用于条件判断）
- **类型保持**：变量有类型（7 种），替换时保持 JSON 原生类型

### 19.6 断言类型（校验门强制）

| 断言类型 | 适用 | 示例 |
|---|---|---|
| `status_code` | HTTP 状态码 | `expected: 201` |
| `json_path` | JSON 字段 | `path: "$.data.id", operator: not_null` |
| `contains` | 字符串包含 | `expected: "success"` |
| `regex` | 正则匹配 | `pattern: "^ORD-\\d{8}$"` |
| `type_match` | 类型检查 | `expected_type: "integer"` |
| `element_visible` | UI 元素可见 | `selector: "[data-testid='btn']"` |
| `element_text` | UI 元素文本 | `selector: "...", operator: equals` |
| `element_count` | UI 元素数量 | `selector: ".item", operator: gte, expected: 3` |
| `screenshot_diff` | 截图对比 | `baseline: "ref.png", threshold: 0.95` |
| `db_query` | 数据库校验 | `sql: "SELECT count(*) FROM orders WHERE id=?"` |
| `custom` | 自定义工具断言 | `tool_ref: "verify_amount", params: {...}` |

**规则：每个步骤至少一个断言，否则校验门不放行。**

### 19.7 错误策略（on_error）

| 策略 | 含义 |
|---|---|
| `retry(n)` | 原地重试 n 次 |
| `reinit` | 重新初始化传输层（重登录） |
| `evict` | 关闭当前会话，重开 |
| `fail` | 直接失败 |
| `skip` | 跳过此步，继续后续 |
| `abort` | 终止整个脚本 |

### 19.8 脚本拆分准则（设计提取）

**禁止写一条长直线。** 具体规则：

| 规则 | 说明 |
|---|---|
| **单一职责** | 一个脚本只测一个场景（一个用例对应一个脚本） |
| **提取复用** | 同一段操作在 2 个以上脚本出现 → 提取为**自定义工具** |
| **传输层复用** | 相同的鉴权/公共头 → 提取为传输层 |
| **前置场景复用** | 相同的准备步骤（登录、造数据）→ 提取为前置场景 |
| **数据集分离** | 数据不写死在步骤里，走数据集 |
| **步骤粒度** | 一个步骤 = 一次工具调用；不要把多个操作塞进一个步骤 |

---

## 二十、用例层级与组织

> 用例本身是扁平的（互不关联），但**组织方式**需要层级。

### 20.1 组织结构

```
项目 (Project)
└── 模块 (Module)           # 按业务域划分：订单、用户、支付…
    └── 功能点 (Feature)    # 模块下的具体功能：创建订单、取消订单…
        └── 用例 (Case)     # 具体的测试流程说明
```

**用例字段补充**：

| 字段 | 说明 |
|---|---|
| `module` | 所属模块 |
| `feature` | 所属功能点 |
| `test_type` | 冒烟 / 功能 / 边界 / 异常 / 权限 / 安全 / 兼容 |
| `priority` | P0(阻塞) / P1(核心) / P2(一般) / P3(低优) |
| `exec_mode` | script_only / hybrid / ai_only |
| `automation_status` | not_automated / in_progress / automated / broken |
| `last_run_at` | 最近执行时间 |
| `last_run_result` | 最近执行结果 |

### 20.2 用例与脚本的关系

```
用例 (TC-0101)  ──关联──▶  脚本 (SCR-0042)  ──引用──▶  传输层 (login_transport)
     │                         │                           工具 (http, browser)
     │                         │                           数据集 (order_dataset)
     │                         │                           环境 (staging)
     │                         └──引用──▶  自定义工具 (verify_amount)
     └── 一个用例可关联多个脚本（不同驱动/不同环境）
```

### 20.3 用例生命周期

```
draft → pending_confirmation → active → deprecated → archived
  │           │                   │
  │           │                   └── 关联脚本状态同步
  │           └── 等待确认（调试/文档/用户）
  └── AI 生成 / 人工创建
```

---

## 二十一、Bug 完整维度模型

> Bug 不只是一个"标题+状态"，它是一个多维度的质量事件。

### 21.1 维度总览

| 维度 | 包含内容 |
|---|---|
| **基础信息** | 标题、描述、严重度、优先级、类型 |
| **复现维度** | 复现步骤、环境、数据、前置条件、复现率 |
| **分类维度** | 缺陷类型、所属模块、所属组件、引入阶段 |
| **影响维度** | 影响范围、影响用户数、业务损失评估 |
| **证据维度** | 截图、日志、网络抓包、trace、断言 diff、DB 状态 |
| **根因维度** | 代码位置、引入提交、假设、置信度、修复建议、自愈建议 |
| **关联维度** | 关联用例、关联脚本、关联需求、关联其他 bug |
| **解决维度** | 修复方案、验证方式、修复人、修复时间 |
| **度量维度** | 发现时间、确认时间、修复时间、重开次数、逃逸阶段 |
| **流转维度** | 状态历史、指派人变更、评论 |

### 21.2 字段明细（标注归属）

> 标注：**现有** = `ProjectIssue` 已有；**扩展** = 新增到 `mc_test_bug_ext`；**根因** = `mc_test_bug_root_cause`；**派生** = 由上述计算得出，不单独存。

**基础信息**（现有）：`id, issue_type=bug, title, priority, status, assignee_id, tags, created_at, closed_at`（`severity`/`bug_type` 若现有无则加到 ext）

**复现维度**（扩展）：`repro_steps(JSONB), repro_env(环境ID), repro_data(JSONB), preconditions(JSONB), repro_rate(always|often|sometimes|rare|once), is_reproducible(bool)`

**分类维度**（扩展 + 根因）：`module, feature, component, introduced_stage(requirement|design|coding|testing|deploy), root_cause_category(logic|boundary|concurrency|null|config|data|integration|requirement_ambiguity|regression|flaky|third_party)`

**影响维度**（扩展）：`blast_radius(影响范围描述), affected_users(估算), business_impact(high|medium|low), is_blocking(bool)`

**证据维度**（扩展，指向证据表）：`evidence_refs[](→mc_test_evidence), screenshots[], logs_uri, network_capture_uri, trace_uri, assertion_diff(JSONB), db_state_snapshot(JSONB)`

**根因维度**（根因表）：`root_cause_location(文件:行号), attributed_commit, hypothesis, confidence(0-1), fix_suggestion, healing_suggestion(脚本级), blast_radius_code(代码影响面)`

**关联维度**（扩展）：`related_case_ids[], related_scenario_ids[], related_requirement_ids[], related_bug_ids[], from_run_scenario_id(来源执行记录)`

**解决维度：**（现有：`resolution_note` + 状态机）
`resolution(fixed|wont_fix|duplicate|not_a_bug|deferred), fix_description, fix_commit, verified_by, verified_at`

**度量维度：**
**度量维度**（现有时间戳 + 派生）：`detected_at, confirmed_at, fixed_at, closed_at, time_to_detect(从引入到发现), time_to_fix(从确认到修复), reopen_count, escape_stage(哪个阶段逃逸)`

**流转维度**（现有）：`status_history(JSONB 状态+时间+操作人), assignee, assignee_history[], comments[]`（复用 `ProjectIssueComment`）

### 21.3 Bug 与 issue 的关系（修正：**彻底解耦，测试域自持**）

> **再修正（2026-10-06）**：推翻「bug = `ProjectIssue(issue_type=bug)`，一个东西」的决策。
> 测试缺陷与 coding 平台**没有任何关系**——它们不是同一个东西，不应混在 coding 的 issue 流里。

**bug = `mc_test_bug`（测试域自持），`project_id` 直接是测试项目 id。**

| 此前（错） | 修正后 |
|---|---|
| bug = `ProjectIssue(issue_type=bug)`，测试项目经 `coding_project_id` 桥接 | **测试域自持 `mc_test_bug`**，`project_id` = 测试项目 id，**不需要 coding 桥** |
| 复用 coding bug 状态机（含人工确认门） | 测试域自定义状态机：`open→triaged→fixed→verified→closed/rejected` |
| 测试字段挂扩展表 `mc_test_bug_ext` | 字段并入 `mc_test_bug` 主表；`mc_test_bug_root_cause`（1:N 根因）+ `mc_test_bug_pattern`（沉淀）保留 |

**测试发现 bug 的路径**：

```
失败分流判 product_bug
  → 建 mc_test_bug（指纹/复现/来源执行/关联用例场景）
  → diagnose 流程填根因（mc_test_bug_root_cause，过根因门）
  → 测试域状态机推进（open→triaged→fixed→verified→closed）
```

**好处**：测试缺陷与 coding 完全解耦，不需要给测试项目配 coding 关联即可建 bug；两边互不干扰。
`mc_test_bug_ext` 旧表退役（字段并入主表）。

---

## 二十二、技术要点回顾

1. 表前缀 `mc_test_*`，域独立于 GA，共用 PG/Redis/通知基础设施。
2. 全实体贯彻**稳定 ID + 状态机 + 版本溯源**三铁律。
3. **疑问、变更记录**是独立一等表，不是塞在别的实体里的字段。
4. **执行引擎是确定性模块**（engine/runtime），与 AI 工作流（surface/workflows）物理分离。
5. 能力面四件套（工具方法/MCP/接口/工作流）是对外唯一出口，agent 可插拔。
6. 内置工具层是**代码注册表**，自定义工具层是**表 + 评审流**——对 AI 固定、对人可扩展。
7. 环境变量**分层继承 + 7 类型**，脚本断言**每步必填**。
8. **脚本是 YAML 声明式**，AI 产出的是结构化数据，不是自由代码。
9. **用例按 项目→模块→功能点→用例 组织**，用例本身扁平、互不关联。
10. **Bug 是 10 维度的质量事件**，不是"标题+状态"。
11. **v1 不开放 Code 层**（纯声明式），遇到表达不了的场景走自定义工具评审。
12. **知识锚点 = 服务端自建轻量符号索引**（tree-sitter），参考 codegraph 数据模型但不依赖它。
