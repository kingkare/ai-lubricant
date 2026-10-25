# 03 · 报告模型

**状态：设计已定** ｜ 前置：01 资产模型、02 任务模型

> 你指出：应该有测试报告页面。**接入方式先空着**，但报告实体与页面必须存在。

## 1. 报告是一等实体

报告**不是**对结果表的临时查询，而是**有实体、有页面、可归档**：

**`mc_test_report`**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `code` | UUID / varchar | `RPT-xxxx` |
| `project_id` | UUID | |
| `run_id` | UUID FK | 来自哪次运行 |
| `task_id` | UUID FK null | 来自哪个任务 |
| `title` | varchar | |
| `test_version` | varchar | **测试版本**（被测系统版本 / 构建号） |
| `env_id` | UUID | 环境 |
| `status` | varchar | `generating` / `ready` / `archived` |
| `generated_at` | timestamptz | |
| `generator` | varchar | `auto` / `manual` |

## 2. 报告内容维度（你列的全部）

| 维度 | 内容 | 来源 |
|---|---|---|
| **用例列表** | 本次跑的用例/场景清单 | `mc_test_run_scenario`（用例结果由其 case_refs 投影） |
| **用例详情** | 每条的步骤结果、断言、证据 | `step_result` + `evidence` |
| **关联 Bug** | 本次发现 + 相关的缺陷 | **复用 `ProjectIssue(issue_type=bug)`** + `mc_test_bug_ext`（本 run 的来源执行） |
| **关联需求** | 用例↔需求映射 | 知识层 `requirement` |
| **测试版本** | 被测版本 | `report.test_version` |
| **用时** | 总耗时 / 各用例耗时 | `run.summary` / `run_scenario.duration_ms` |
| **响应时间** | 各请求步骤的响应耗时 | `step_result.duration_ms`（request 类） |
| **通过率/趋势** | 本次 + 历史对比 | 多次 `run.summary` |

### 2.1 报告结构（渲染用）

```json
{
  "code": "RPT-0007",
  "title": "夜间回归 · 2026-09-26",
  "test_version": "v2.3.1-rc2",
  "env": "staging",
  "overview": {
    "total": 128, "pass": 120, "fail": 5, "error": 2, "flaky": 1, "skipped": 0,
    "duration_ms": 1840000, "pass_rate": 0.9375
  },
  "timing": {
    "avg_response_ms": 142, "p95_response_ms": 380,
    "slowest_steps": [ { "step": "...", "ms": 2100 } ]
  },
  "cases": [
    {
      "case_code": "TC-0101", "title": "...", "scenario": "SCN-0042",
      "status": "fail", "duration_ms": 3200,
      "steps": [ { "seq": 10, "status": "pass", "duration_ms": 120 }, { "seq": 20, "status": "fail", "diff": {...} } ],
      "evidence": ["shot_1.png", "trace_2.zip"]
    }
  ],
  "bugs": [ { "code": "BUG-0011", "title": "...", "severity": "major", "related_cases": ["TC-0101"] } ],
  "requirements": [ { "req_id": "REQ-003", "title": "...", "covered_cases": ["TC-0101"], "coverage": "covered" } ]
}
```

## 3. 报告页面

| 区域 | 内容 |
|---|---|
| **顶部概览** | 标题、测试版本、环境、用时、通过率（大数字 + 环形图） |
| **用例列表** | 表格：用例、场景、状态、耗时、操作（查看详情）；支持按状态筛选 |
| **用例详情** | 点开：步骤级结果 + 断言 diff + 证据（截图/日志/trace） |
| **关联 Bug** | 本次发现的缺陷列表，点进缺陷详情 |
| **关联需求** | 需求覆盖情况（covered / not_covered / partial） |
| **耗时分析** | 响应时间分布、最慢步骤 Top N |
| **趋势** | 历史通过率曲线（多次 run 对比） |
| **导出** | 导出 PDF/HTML（接入方式**暂空**） |

## 4. 接入方式（v1 已定）

> 原设计此处"接入方式暂空"。**v1 决策**：生成器用服务端聚合 + 快照落库；导出 HTML 单文件；PDF 后置。

| 接入 | v1 决策 |
|---|---|
| 报告生成器 | **服务端聚合**：`report.generate` 读 run/run_scenario/step_result/evidence → 组装 JSON 快照 → 落 `mc_test_report` |
| 报告渲染 | **前端渲染**（随服务前端栈），读快照 JSON |
| 导出 | **HTML 自包含单文件**（内联样式与数据）；PDF 后置 |
| 外部报告系统对接（如 Allure） | 预留，v1 不做 |

## 5. 与运行的关系

```
task.run → run → run_scenario → step_result → evidence
                    ↓
              report.generate
                    ↓
              mc_test_report（实体）
                    ↓
              报告页面（渲染）
```

- 一次 run 可生成**一份报告**（默认）或**多份**（不同视角）
- 报告**归档**（`archived`）后仍可查看（数据快照）

## 6. 设计决策

| # | 问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | 实时聚合 vs 快照 | **生成时快照** | 报告要能归档、要可复现；结果表可能被清理，快照保住当时真相 |
| 2 | 趋势保留期 | **默认 180 天**，可配 | 够看季度趋势；超期归档 |
| 3 | 关联需求来源 | **知识层 `requirement` 条目**（经 `covered_by` 边） | 与知识体系一致；未接知识层时该区域留空 |
| 4 | 导出格式与渲染引擎 | **v1 导出 HTML（自包含单文件）**；PDF 后置；渲染引擎随前端栈 | HTML 零依赖、可直接分享；PDF 需额外链，后置 |
