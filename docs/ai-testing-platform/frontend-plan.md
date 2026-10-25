# AI 测试平台 · 前端方案

**状态：设计** ｜ 前置：`docs/ai-testing-platform/`（设计）+ `testing_platform/`（后端骨架）

> 目标：给已落地的 `testing_platform/` 后端骨架配上前端。本文只讲**前端怎么做**——落在哪、分几期、复用什么、有哪些硬约束。

---

## 一、落在哪：**新增第七个模式 `/testing`**

现有六模式是「唯一顶层结构」（`src/config/modes.ts`）：`/llm`（模型网关）`/agent-mode`（Agent）`/coding`（编码）`/resources`（资源）`/devices`（设备）`/ops`（运维）。

**测试是平级角色**（设计定位：测试是与开发平级的能力域），**不是 coding 的子页**。所以：

```ts
// src/config/modes.ts 新增
{
  id: "testing",
  prefix: "/testing",
  labelKey: "modes.testing",
  fallback: "测试",
  icon: FlaskConical,          // lucide
  items: [
    { to: "/cases",      fallback: "用例",     icon: ListChecks },
    { to: "/scenarios",  fallback: "场景",     icon: Workflow },
    { to: "/tasks",      fallback: "任务",     icon: CalendarClock },
    { to: "/runs",       fallback: "执行记录", icon: PlayCircle },
    { to: "/reports",    fallback: "报告",     icon: FileBarChart },
    { to: "/bugs",       fallback: "缺陷",     icon: Bug },
    { to: "/knowledge",  fallback: "知识库",   icon: Network, adminOnly: true },
    { to: "/enveronments", fallback: "环境",   icon: SlidersHorizontal },
  ],
}
```

> 前缀 `/testing` 需**先核对未被后端占用**（照 `modes.ts` 顶部注释的规矩：`/agent` 被占故 Agent 模式走 `/agent-mode`）。建新前缀前 grep `main.py` 的 `_API_PREFIXES`。

---

## 二、分期（按后端就绪度）

后端现状（`testing_platform/`）：**引擎/知识/校验器/资产/能力面/任务/报告/工作流/缺陷** 九模块骨架已落，但**路由未挂载**、部分方法未注册。

| 期 | 内容 | 依赖后端 |
|---|---|---|
| **P1 只读看板** | 报告页（`mc_test_report` 快照渲染）+ 执行记录页（`run`/`run_scenario`/`step_result`） | `reports.get_report` / `tasks.get_run` ✅ 已有 |
| **P2 资产管理** | 用例列表/详情、场景列表/**场景编辑器**（步骤增删改排序）、标签 | `assets.service` ✅ 已有 |
| **P3 任务与触发** | 任务列表/编辑（标签选例 + 通知 + 全局参数）、手动触发、执行状态 | `tasks.service` ✅ 已有（`run_task` 只建 pending，需接引擎） |
| **P4 知识与缺陷** | 知识库浏览（锚点/条目/图谱视图）、缺陷列表（复用 ProjectIssue 详情页） | `knowledge` ✅ / `defects` ✅ |
| **P5 生成侧** | 工作流定义页 + 触发 agent 生成（经能力面） | `workflow` ✅（LLM 由外部 agent 跑） |

**先做 P1**——报告和执行记录是纯读、后端已就绪、且是"这套东西到底跑没跑通"的最直观证明。

---

## 三、复用现有前端基建（别重造）

| 复用 | 出处 | 用途 |
|---|---|---|
| **API 客户端模式** | `src/api/*Client.ts`（如 `nodes.ts`、`environmentClient.ts`） | 照此建 `src/api/testingClient.ts` |
| **统一请求层** | `src/api/shared-api.ts` / `Api.ts` | 走既有 axios 封装，别新造 |
| **剪贴板** | `@/utils/clipboard` 的 `copyToClipboard()` | **禁裸 `navigator.clipboard`**（HTTP/360/失焦会失败） |
| **页面壳** | `pages/console/app-shell.tsx` | 自动获得面包屑/侧栏/角色过滤 |
| **UI 组件** | `src/components/ui/*`（shadcn 风格：Card/Table/Dialog/Tabs…） | 别自造 |
| **图表** | `recharts`（已在依赖） | 趋势图/通过率环形图 |
| **通知** | `sonner` 的 `toast` | 操作反馈 |
| **i18n** | `react-i18next`（`cn.ts`/`en.ts`） | 新页文案**必须**加键（你们有 i18n 审计记忆） |
| **状态** | `zustand` | 局部状态；**无 react-query**，取数照现有页自研模式 |

---

## 四、硬约束（**开工前必读，否则必踩**）

### 1. ⚠️ 高度链：**别挂 `flex-1`**
> 来自你们的 `six mode console ia` 记忆：`platform-scope` 是 **block 滚动容器**，页面挂 `flex-1` 高度链会让**卡片塌成 48px**。

新页面（报告/列表/详情）**一律用文档流高度**（自然高度 + 容器内滚动），不要 `h-full`/`flex-1` 撑满。

### 2. 场景编辑器是**表单**，不是 YAML 文本框
设计定死（01 §3）：场景的**唯一事实源是库表**，YAML 只是交换格式。所以编辑器是**步骤行表格**（增删改 + 拖拽排序 → `seq` 重排）+ 每步断言子表单，**不是**让用户/AI 贴 YAML。

### 3. 步骤类型驱动表单
`mc_test_scenario_step.type` 十种（request/page/android/ios/wait/script/check/sql/ws/custom），**每种参数结构不同**——参数字段按 type 动态渲染（注册表驱动，照 `surface/methods.py` 的 schema 子集）。

### 4. 变量语法高亮（可选但强烈建议）
场景里 `{{env.x}}`/`{{local.y}}` 出现频繁，输入框给**变量提示**（列出当前场景可用的 local_vars/env/auth 域）能大幅降低出错——对应校验器规则 10/11（作用域合法、定义在前）。

### 5. 校验错误要**定位到字段**
`quality.validate` 返回 `{path, rule, msg, severity, hint}`——前端要把 `path`（如 `steps[0].assertions`）**映射到表单字段高亮**，不是弹个 toast 了事。

### 6. 报告页是**快照渲染**
`generate_report` 是生成时定版（03 §6）——报告页**只读快照 JSON**，不回查结果表。这点要在 UI 上体现（如标"生成于 X 时"）。

---

## 五、关键页面设计

### 报告页（P1 首个）
```
┌─ 顶部概览：标题 · 测试版本 · 环境 · 用时 · 通过率(环形) ─┐
├─ 用例表格：用例 / 场景 / 状态 / 耗时 / 操作              │
│    └ 展开 → 步骤级结果 + 断言 diff + 证据(截图/日志)     │
├─ 关联缺陷列表                                            │
├─ 耗时分析：响应时间分布 + 最慢步骤 Top N (recharts)      │
└─ 导出 HTML（后端 v1 只出快照，导出后补）
```

### 场景编辑器（P2 重头）
```
左：场景基本信息（name/transport/env/exec_mode）
中：步骤列表（行=步骤；拖拽排序；点开=该步详情表单）
     └ 步骤类型下拉 → 参数字段随类型变
     └ 后置校验子表单（断言类型下拉 → 断言参数随类型变）
右：校验结果面板（实时调 script.validate，错误定位到字段）
```

### 知识库（P4）
- 列表视图：锚点 / 条目 / 边（按 kind 筛）
- **图谱视图**：v1 用 recharts 做不了力导向图——如要做，需引 `@xyflow/react` 或 d3（**新增依赖，需你确认**）；否则 v1 只做列表 + 关系表格

---

## 六、要你拍板的 3 条

| # | 事项 | 我的建议 |
|---|---|---|
| 1 | **测试模式前缀**：`/testing`（新第七模式） vs 挂进 `/coding` 下 | **新第七模式**——测试是平级角色，不是 coding 子页 |
| 2 | **知识图谱可视化**：要不要力导向图 | **v1 不做**（需引新依赖）；先列表+关系表，图后置 |
| 3 | **首期做什么** | **P1 报告 + 执行记录**（纯读、后端已就绪、最能证明跑通） |

---

## 七、前置动作（开工前）

1. **后端路由挂载**：`testing_platform/surface/api.py` 的 APIRouter **尚未挂到 `main.py`**——前端要调，得先挂（一个 `include_router`）。这一步会碰 `main.py`，需按你们的规矩（共享 worktree、精确提交）小心做。
2. **核对 `/testing` 前缀未被后端占用**（grep `main.py` 的 `_API_PREFIXES`）。
3. **`testingClient.ts`**：照 `nodes.ts` 模式建 API 客户端。
