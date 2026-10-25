# 04 · 执行与结果模型

**状态：设计已定** ｜ 前置：01 资产模型、02 任务模型

> 执行是**确定性**的（引擎跑）；失败分流可含 AI。执行单位是**场景**（不是用例）。

## 1. 表设计

### 1.1 mc_test_run（一次运行）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `code` | UUID / varchar | `RUN-xxxx` |
| `project_id` | UUID | |
| `task_id` | UUID FK null | 来自哪个任务（手动单跑可空） |
| `trigger` | varchar | `manual` / `scheduled` / `event` / `api` |
| `env_id` | UUID | |
| `global_params` | JSONB | 本次全局参数快照（来自任务） |
| `scenario_ids` | JSONB | 本次选出的场景（标签选例结果） |
| `status` | varchar | `pending` → `running` → `done` / `aborted` |
| `summary` | JSONB | `{total, pass, fail, error, flaky, skipped, duration_ms}` |
| `started_at` / `finished_at` | timestamptz | |

### 1.2 mc_test_run_scenario（场景级结果）

> 执行单位是**场景**；一个场景可能被多个用例引用，结果挂在场景上，同时记录引用它的用例。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `run_id` | UUID FK | |
| `scenario_id` | UUID FK | |
| `case_refs` | JSONB | 本次引用该场景的用例（多个） |
| `exec_mode` | varchar | 实际模式 |
| `status` | varchar | `pass` / `fail` / `error` / `flaky` / `skipped` |
| `failure_kind` | varchar | `script_issue` / `product_bug` / `case_outdated` / `env_flaky` |
| `duration_ms` | int | |
| `healing` | JSONB | hybrid 自愈记录 |
| `dataset_row` | JSONB | 本次数据行 |

### 1.3 mc_test_step_result（步骤级结果）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `run_scenario_id` | UUID | |
| `step_key` / `seq` | | |
| `type` | varchar | 步骤类型（request/page/wait/script/check…） |
| `status` | varchar | `pass` / `fail` / `error` / `skipped` |
| `attempt` | int | 第几次尝试 |
| `expected` / `actual` / `diff` | JSONB | 断言期望/实际/差异 |
| `duration_ms` | int | **响应时间来源** |

### 1.4 mc_test_evidence（证据）

| 字段 | 说明 |
|---|---|
| `id, run_scenario_id, step_result_id` | |
| `kind` | `screenshot` / `log` / `network` / `trace` / `db_state` / `assertion_diff` |
| `uri` / `meta` | 附件地址 + 元数据 |

> 大文件走附件存储；敏感变量写入前脱敏。

## 2. 执行流程

```
run = create_run(task, trigger)
scenarios = select_by_tags(task.scenario_tags, task.case_filter)   # 标签选例
ctx0 = merge(env, task.global_params)

for scn in scenarios:                        # 执行单位=场景
    ctx = {**ctx0, local: scn.local_vars}
    for pre in scn.flow.pre_scenarios:       # 前置场景先跑，local_vars 传递
        execute_scenario(pre, ctx)
    for step in scn.steps (order by seq):
        if not eval_conditions(step, ctx): continue
        await step.run(ctx)                  # 类型化执行
        await run_post_checks(step, ctx)     # 后置校验（类型化）
        apply_post_cond(step, ctx)           # 写局部变量
        if early_exit_hit(ctx): break
    write run_scenario(...)

finalize_run(run)  → summary
generate_report(run)
notify(task.notify, run)
```

## 3. 三模执行

| 模式 | 流程 |
|---|---|
| `script_only` | 引擎跑 → 判定（默认） |
| `hybrid` | 引擎跑 → 失败 → Debugger 诊断 → 自愈建议 → 修场景/步骤 → 重跑 |
| `ai_only` | 无场景，AI 直接执行（仍只用注册工具/方法，断言仍机械判） |

**hybrid 自愈边界**：只改场景/步骤（不改被测代码）；有次数/时间/token 预算；改完**重新过校验门**；重跑后仍由断言库判定（AI 不能自判 pass/fail）。

## 4. 失败分流（triage）

| 结果 | 判据 | 处理 |
|---|---|---|
| `script_issue` | 定位/时序/断言写法问题 | 自愈或转人工 |
| `product_bug` | 断言合理但系统行为不符 | 建 `ProjectIssue(issue_type=bug)` + `mc_test_bug_ext`（复用现有 issue，不另建 bug 表） |
| `case_outdated` | 需求已变，预期过时 | 标用例 deprecated |
| `env_flaky` | 重跑即过 | 标 flaky，不计失败 |

**判据顺序（确定性优先）**：重跑一次 → 比对知识库预期 → 判失败层（脚本层 vs 系统层）→ 不确定才交 AI。

**去重**：失败指纹 `hash(错误签名 + 步骤类型 + 响应 diff 特征)` 相同 → 归并到同一 bug。

## 5. 触发

| 触发 | 说明 |
|---|---|
| manual | 任务页点运行 / 单场景调试 |
| scheduled | 任务定时（cron/间隔/一次性） |
| event | 代码 push（增量）/ bug 修复（回归）——**依赖知识图谱，v1 先 manual/scheduled** |
| api | 外部系统调接口 |

## 6. 设计决策

| # | 问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | `dataset` 多行 | **默认串行**；可显式声明 `parallel`（并行时**每行独立 ctx**，变量不共享） | 串行最稳（数据行间常有依赖）；并行是显式选择 |
| 2 | `flaky` 阈值 | **重跑 2 次内过即判 flaky**；仍失败则按真实失败走分流 | 与"自愈预算"对齐，避免重跑烧资源 |
| 3 | 自愈预算 | **同一场景最多 2 次**、总时长 ≤ 5 min、token ≤ 50k，超限交人 | 有界，防无限循环 |
| 4 | 引擎运行位置 | **服务端进程内**（确定性执行与写库，设备/浏览器操作经现有通道下达；无节点执行与回传问题） | 结果同进程直接落库，无需跨机通道 |
| 5 | 证据保留期 | **默认 30 天**，失败证据 90 天，可配 | 失败证据更值得留；成功证据定期清 |
