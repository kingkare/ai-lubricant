# testing_platform —— AI 测试平台（服务端能力域）

> **设计文档**：[`docs/ai-testing-platform/`](../docs/ai-testing-platform/)（README + design.md + 9 份计划）
> 本文件只讲**代码怎么用**；为什么这么设计看设计文档。

## 定位

测试是**本服务内的一个能力域**，与 GA（GenericAgent）无关、不借用其概念。它把平台从"开发工具"补成"生产平台"——测试是与开发**平级**的角色。

**三条不可动摇的原则**（贯穿全部代码）：

| 原则 | 代码里的体现 |
|---|---|
| **AI 不裁判，框架裁判** | 校验器/断言库/契约校验都是确定性代码；有测试断言"引擎源码不含任何 LLM import" |
| **确定性的归确定性** | `engine/` 全异步但不调 LLM；LLM 角色只在 `workflow/` 里**存定义**，由外部 agent 经能力面跑 |
| **不重复建模** | bug 复用平台 `ProjectIssue`（`defects/FORBIDDEN_TABLES` 防误建 `mc_test_bug`）；版本统一走 `mc_test_change_record` |

## 目录结构

```
testing_platform/
├── projects/     # 测试项目（整个项目维度的独立实体；git 配置 + 代码快照）
├── engine/       # 确定性执行引擎（场景→步骤→断言→结果）
├── knowledge/    # 知识摄取（Graphify + 双轨源 L1/L2 + 用例扫描 + agent 落地）
├── assets/       # 资产模型（用例/场景/步骤/标签）+ PG 场景加载器
├── quality/      # 校验器（脚本创建准则的机器强制）
├── surface/      # 能力面（工具方法 + MCP + HTTP 骨架）
├── tasks/        # 任务（标签选例 + 多维启动 + 通知）
├── reports/      # 报告（生成时快照）
├── workflow/     # 工作流域（7 角色定义 + 契约校验）
└── defects/      # 缺陷闭环（复用 ProjectIssue）
```

## 数据流

```
知识源(PRD/代码/历史)
   │ knowledge/  Graphify 抽取 → 映射 → mc_knowledge_* 幂等入库
   ▼
知识库(mc_knowledge_item/anchor/edge + mc_test_profile)
   │ workflow/  7 角色定义（LLM 由外部 agent 跑，平台只存定义+校验产出）
   ▼
资产(mc_test_case / mc_test_scenario / mc_test_scenario_step / mc_test_tag)
   │ quality/   校验器（硬规则；不过不入库）
   │ assets/    PgScenarioLoader
   ▼
执行(mc_test_run / run_scenario / step_result / evidence)
   │ engine/    ScenarioExecutor（服务端进程内，确定性）
   ▼
报告(mc_test_report 快照) / 缺陷(mc_project_issues + mc_test_bug_ext/root_cause/pattern)
```

## 快速上手

### 跑一个场景（引擎）

```python
from testing_platform.engine import ScenarioExecutor, DictScenarioLoader, ExecutionContext

loader = DictScenarioLoader({"SCN-1": {
    "code": "SCN-1", "local_vars": {}, "flow": {},
    "steps": [{
        "step_key": "s1", "seq": 10, "type": "request",
        "params": {"method": "GET", "url": "https://example.com"},
        "post_checks": [{"type": "status_code", "expected": 200}],
    }],
}})
result = await ScenarioExecutor(loader).execute("SCN-1", ExecutionContext())
print(result.status)  # pass / fail / error
```

### 从库里取场景跑（生产路径）

```python
from testing_platform.assets import PgScenarioLoader
from testing_platform.engine import ScenarioExecutor

result = await ScenarioExecutor(PgScenarioLoader()).execute("SCN-0001", ExecutionContext())
```

### 校验一个场景（入库前）

```python
from testing_platform.quality import validate
from testing_platform.engine import default_tool_registry, default_assertion_registry

res = validate(scenario_dict, default_tool_registry(), default_assertion_registry())
if not res.valid:
    for issue in res.issues:      # {path, rule, msg, severity, hint}
        print(issue.rule, issue.msg)
```

### 跑一个测试项目（项目域）

```python
from testing_platform.projects import create_project, create_git, init_snapshot

proj  = await create_project(name="平台回归")                    # code 自动 TP-xxxx
git   = await create_git(project_id=proj.id, name="backend",
                         repo_url="https://github.com/x/y.git",
                         ref_type="branch", ref_value="main",
                         credential_ref={"kind": "env", "name": "GIT_TOKEN"})
snap  = await init_snapshot(git.id)   # 克隆到快照目录（幂等：已有则 fetch+reset）
print(snap["path"], snap["commit"])  # 项目**不拥有代码**，代码是按需拉的可丢弃快照
```

### 经能力面调用（agent 入口）

```python
from testing_platform.surface import call_tool, list_tools

list_tools()                                    # MCP 工具定义列表
await call_tool("script.validate", {...})       # 分发到方法注册表
```

## 建表

各子包提供 `create_tables_via_pg()`（走 `from db import PostgresClient`）：

```python
from testing_platform.assets import create_tables_via_pg as create_assets
await create_assets()
```

或把模型注册进 Tortoise 的 `MODEL_MODULES`（`user_platform/database.py` 约定）。

## 测试

```bash
# 本域全部（188 例，约 16s）
python -m pytest tests/test_{engine_executor,assets_model,quality_validators,surface_methods,tasks_service,reports_service,workflow_contract,defects_flow,check_spec_links}.py -q

# 知识 adapter（需隔离 venv 跑 Graphify 子进程）
GRAPHIFY_VENV="D:/code/ai-lubricant/.venv-graphify" .venv/Scripts/python -m pytest tests/test_knowledge_adapter.py -q
```

> 测试全用 `sqlite::memory:`，不依赖 PG / 网络。**注意**：各文件 fixture 的 Tortoise app label 必须统一为 `"models"`（进程级注册表，混用会互相覆盖 schema）。

## 外部依赖

| 依赖 | 用途 | 约束 |
|---|---|---|
| **Graphify**（`graphifyy`） | 知识摄取（代码符号/调用边/文档↔代码引用） | **必须装隔离 venv**（`.venv-graphify`），用 `GRAPHIFY_VENV` 环境变量定位；**不装进项目共享 `.venv`** |
| FastAPI | `surface/api.py` 的路由骨架 | 项目已有；**未挂载到 main.py** |
| Tortoise / PostgresClient | 持久化 | 平台惯例 |

## 尚未接入（v1 边界）

- **未挂载路由**：`surface/api.py` 只建 APIRouter，未注册到 `main.py`（避免碰现有启动链路）
- **未实现的方法**：`question.*` / `bug.*` / `report.*` / `craft.*` 未注册（等对应模块）；`methods.py` 顶部 docstring 有"待实现"表
- **未接真实执行**：`tasks.run_task` 只建 pending run 行，未接引擎
- **未实现的步骤类型**：`browser` / `mobile` / `custom` / `sql` / `ws` 在引擎里是 stub（报"未注册"）
- **报告 bugs/requirements 区**：留空数组（缺陷域/知识层未接线）

## 关键实现约定

1. **稳定身份**：锚点 `(project_id, kind, path, symbol)`、条目 `(project_id, kind, key)`——**不用外部工具的 id**（Graphify 的 id 含行号会漂移）
2. **全异步**：框架层 `async def`；同步方法用 `engine.aio.run_sync_or_async` 适配
3. **变量作用域**：`local > data > auth > env > global`（+ `response`/`result` 单步骤临时域）；类型保持，未定义即报错
4. **变更门**：`update_*` 必带 `change_reason`，否则抛错；变更写 `mc_test_change_record`
5. **根因门**：`attach_root_cause` 的 `evidence_chain` 为空则拒收（"每条结论带证据"）
