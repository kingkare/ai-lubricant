# 11 · 测试项目域（阶段 1 实施记录）

> 前置：`test-project-definition.md`（定案）§十。本文记录**阶段 1 已落地**的内容与约定。

## 一、建了什么

### 数据层（4 张新表，`testing_platform/projects/models.py`）

| 表 | 说明 | 稳定身份 |
|---|---|---|
| `mc_test_project` | 测试项目（整个项目维度，与 coding 零重合） | `code`（TP-xxxx 全平台唯一） |
| `mc_test_project_git` | git 配置（1-N，**不拥有代码只配 git**） | `(project_id, name)` |
| `mc_test_project_source` | 源配置（A 轨 builtin / B 轨 agent） | `(project_id, kind, name)` |
| `mc_test_project_release` | 版本指针快照（人工版本号 + 各源指针） | `(project_id, version_code)` |

- 已注册进 `user_platform/database.py` 的 `TESTING_MODEL_MODULES`（启动即建表）。
- 镜像 DDL + `create_tables_via_pg()` 兜底（同 assets/knowledge 惯例）。
- `credential_ref` 是 **JSONB 引用**（`{"kind":"env","name":"GIT_TOKEN"}`），**永不存明文**。

### 接口层（19 个能力面方法，`surface/methods.py`）

| 组 | 方法 | kind |
|---|---|---|
| 项目 | `project.create / list / get / update / archive` | write×3 + read×2 |
| git | `project.git.create / list / update / delete` | write×3 + read×1 |
| 源 | `project.source.create / list / update / delete` | write×3 + read×1 |
| 代码快照 | `project.code.init / sync` | **execute**（真跑 git，确定性） |
| 版本 | `project.release.create / list / diff` | write×1 + read×2 |
| 图谱 | `project.graph.query` | read（力导向图 nodes/edges） |

变更门：`project.update` 的 status/version 结构性变更必须带 `change_reason`。

### 代码快照（`projects/git_source.py`）

- `init_snapshot(git_id)`：clone 到 `$TESTING_SNAPSHOT_ROOT/<project_id>/<git_id>`（缺省 `.claude/testing-snapshots/`）；**幂等**——目录已存在走 `fetch --depth 1 + checkout + reset --hard`。
- 先 clone 默认分支再 fetch 配置的 ref（branch/tag/commit 统一走 fetch+checkout 路径；`clone --branch` 不认 commit）。
- 凭据解析：`{"kind":"env","name":...}` → 注入 https userinfo（**进程内，不落盘**）；私有仓库缺凭据**显式报错，绝不静默**。
- `sync_all(project_id)`：项目下全部 active git 配置逐个同步，单仓失败不拖垮整批（errors 里逐条报）。

### 图谱 query（`project.graph.query`）

读 `mc_knowledge_anchor` + `mc_knowledge_edge`，产出力导向图数据（阶段 5 前端直接喂 react-force-graph-2d）：

```json
{"nodes": [{"id","kind","label","path","symbol","reliability","confidence"}],
 "edges": [{"id","source","target","relation","confidence"}],
 "meta":  {"node_kinds":[...],"edge_kinds":[...]}}
```

- `reliability ∈ confirmed|candidate|stale`（前端映射透明度/红边）。
- **悬挂边过滤**：两端必须都在返回节点集合内（图渲染不出现幽灵边）。

## 二、release diff（阶段 4 提前铺）

`diff_releases(base, head)` 纯函数：三组指针（git_tags / prd_versions / case_versions）逐项比 `ref`，输出 `added / removed / changed / changed_ids`。`changed_ids` = 需要重新摄取的源 = 增量选例输入。

## 三、测试

- `tests/test_projects_domain.py`（6 例）：建表 / CRUD / 冲突 / 变更门 / diff / **真实 git 克隆**（file:// 临时仓库，幂等二次调用验证）。
- `tests/test_surface_methods.py`：注册表边界（90 方法）+ 项目域 4 个方法组端到端（含 graph.query 悬挂边过滤）。
- `tests/test_surface_writes.py`：`EXPECTED_TOTAL` 71 → 90。

## 四、留给阶段 2+

- 源配置表已建，但 **A 轨适配器**（excel/json/xmind/word 解析）与 **B 轨 `source.fetch` 通道**未接——`project.source.*` 现在只存配置不执行。
- `sync_policy=scheduled` 的 cron 接线（复用平台 scheduler）未做。
- git push webhook（`sync_policy=webhook`）未做。
- release 的指针**自动采集**（从当前各源水位生成快照）未做——现在靠调用方显式传。
