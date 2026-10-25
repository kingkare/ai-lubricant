# 13 · 阶段 3/4/5 收口（工作流断点 + 分析师编排 + 版本捕获 + 可靠性）

> 前置：`test-project-definition.md` §九（阶段 3/4/5）。
> 本文记录**阶段 3/4/5 已落地**的内容与约定。

## 一、工作流断点收口（此前 7 角色跑不通的根因）

之前 `coder` 引用 `scenario.generate`、`debugger` 引用 `bug.triage`——两个方法**未注册**，
是 7 角色工作流的断点。本阶段补齐全部 design §17.1 声明但未注册的方法：

| 方法 | 性质 | 说明 |
|---|---|---|
| `scenario.generate` | write | coder 落地：agent 场景 → **先过 script.validate 校验门**（确定性）→ 通过才建场景+步骤 |
| `bug.triage` | write | debugger 落地：失败分流（`failure_kind`+重跑→动作，纯函数）+ 可选建 bug（指纹去重） |
| `run.status` | read | 运行状态（mc_test_run 生命周期 + summary） |
| `run.result` | read | 运行完整结果（运行+场景+步骤证据，调试器/报告直接消费） |
| `case.confirm` | write | **确认门**：pending_confirmation → active（用户拍板），agent 不得自行跨过 |
| `knowledge.profile` | read | 锚点测试画像（L4 纯派生，从边反向聚合知识/覆盖） |
| `tool.register` | write | 自定义工具注册，**强制 status=review**（不自封 active） |

> 现在 `builtin_workflow_defs()` 的每条 `tool_refs` 都能在方法注册表解析（实测 ALL RESOLVED）。
> 只剩 `scenario.validate` 不注册——校验器以 `script.validate` 暴露，不另开名字。

## 二、分析师编排（阶段 3）

**`analyst.prepare`**（execute）——B 轨 analyst 工作流的平台侧，确定性前半段：

```
source.fetch 取内容（builtin 有 path 走 fetch_file；B 轨给 agent 可读提示）
   → 开 analyst 工作流运行（pending）
   → 组装 agent 任务上下文（raw_id/源 id/字段契约/可测闸口径/置信度上限）
```

agent 拿到上下文 → 解析 → `knowledge.put`（过闸落地）→ `workflow.complete` 收口。
**平台不写 B 轨解析器**，但通过「任务上下文 + knowledge.put 的闸」保证 agent 产出不过门进不来。

## 三、版本捕获 + 影响面（阶段 4）

**`release.capture`**（write）——版本快照自动采集：读当前 git 水位（ref）+ 各源
`last_synced_at`/hash → `mc_test_project_release` 指针快照。**幂等**（同版本返回既有），
不用手填指针，点了就锁版。

**`release.impact`**（read）——增量测试根基：`diff_releases` → 哪些源变了 → 受影响
知识条目（按 source_id）→ 锚点 → 用例。回归选例只需重测 `impact.cases`。

## 四、可靠性仪表（阶段 5）

**`reliability.report`**（read）——项目知识总览：confirmed/candidate/stale 分布、
锚定率、确定性与 agent 产出比、**待确认清单**（candidate 队列，人工确认门）。

配合已有的 `project.graph.query`（力导向图数据），阶段 5 前端可视化所需数据全部就位。

## 五、测试

`tests/test_phases_345.py`（8 例）覆盖全部新方法。相关 259 例全绿（分批跑，
`test_knowledge_adapter` 需 `GRAPHIFY_VENV` 不在此列）。

## 六、仍未做（如实记录）

- **B 轨 analyst 的 LLM 侧**：`analyst.prepare` 只做确定性编排；agent（dsh）本身
  的推理循环、MCP 注入（`surface/mcp.py` 起成 MCP server + `cordis.patch.yml` 模板）未接。
- **力导向图前端**（阶段 5 前端）：数据方法已备（`project.graph.query` / `reliability.report`），
  页面未建。
- `sync_policy=scheduled` 的 cron / `webhook` 触发未接。
- 锚点闸通过后自动建 `covered_by` 边（覆盖矩阵）未做。
