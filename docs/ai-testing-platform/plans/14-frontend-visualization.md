# 14 · 前端可视化（阶段 5 前端）

> 前置：`test-project-definition.md` §八（图谱=力导向图）/ §九（阶段 5）。
> 本文记录**前端可视化已落地**的内容与约定。

## 一、新增两页

### 测试项目页（`/testing/projects`）

阶段 1 的接口层早已就绪，但**前端一直缺页面**（此前只能经能力面调）。本页补齐：

- **项目 CRUD**：新建（code 自动 `TP-xxxx`）+ 列表 + 选中详情
- **git 配置**：1-N 仓库（name/repo_url/ref_type/ref_value），`credential_ref` 只存引用
- **初始化图谱**：一键「拉代码快照 → Graphify → 图谱入库」（`project.code.init` + `project.code.graph`），回显节点/边计数
- **版本快照**：`release.capture` 锁定当前水位（幂等），列出历史版本

### 知识图谱页（`/testing/graph`）

阶段 5 的核心可视化：

- **力导向图**：`react-force-graph-2d`（2D Canvas，pnpm 装）
  - 节点**颜色** = 类型（function/class/file/endpoint/route/table…）
  - 节点**透明度** = 可靠性（confirmed 实色 / candidate 半透明 / stale 淡色 + 红圈）
  - 点击节点 → 详情面板（kind/label/path/symbol/可靠性徽章）
- **可靠性仪表**：四卡（知识条目 / 已确认 / 待确认 / 可能过期）+ **candidate 待确认清单**（带未过闸原因）

## 二、前端改动清单

| 文件 | 改动 |
|---|---|
| `src/api/testingClient.ts` | +`project.*` / `graph` / `reliability` / `release.*` 系列方法（~30 个） |
| `src/pages/console/user/testing/projects-page.tsx` | 新页 |
| `src/pages/console/user/testing/graph-page.tsx` | 新页 |
| `src/App.tsx` | 挂 `/testing/projects` + `/testing/graph` 路由 |
| `src/config/modes.ts` | 菜单加「测试项目」「知识图谱」两项 |
| `src/i18n/resources/{cn,en}.ts` | `modes.testing.projects/graph` 键 |
| `package.json` | +`react-force-graph-2d` |

## 三、构建验证

- `vite build` 通过；`graph-page` 独立 chunk（147 KB，code-split，不拖主包）
- `tsc --noEmit`：新文件**零错误**（仓库另有 46 个既有错误，与本次无关）

## 四、工程坑（记录）

- 本仓前端是 **pnpm** 项目（`node_modules/.pnpm` + `pnpm-lock.yaml`）。用 `npm install` 会
  报 `Cannot read properties of null (reading 'matches')` 内部错误，**装不上**——必须用 `pnpm add`。
- `vite build` 需 `VITE_APP_EDITION=online`（否则 config 校验报 `Invalid VITE_APP_EDITION`）。
- `react-force-graph-2d` 是 canvas 组件，**必须给容器显式像素高度**（否则塌成 0）；本项目
  页面用 ResizeObserver 测宽 + 固定 520px 高。

## 五、仍未做

- **chat 对话页后端**（dsh 驱动 + SSE）：前端 `chat-page.tsx` 早前已入库，后端未做。
- **B 轨 analyst 的 agent 推理循环**：`analyst.prepare` 只做平台侧编排，dsh 侧未接。
- **MCP server 暴露**：`surface/mcp.py` 已是 MCP 形态定义，但未起成真正的 MCP server
  （stdio/HTTP）供 dsh 的 mcp-client 连接。
- `sync_policy=scheduled` 的 cron / `webhook` 触发。
