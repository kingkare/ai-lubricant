# 01 · 资产模型：用例 / 场景 / 步骤 / 标签 / 方法脚本

**状态：设计已定** ｜ 前置：无

> 核心修正：**用例 ≠ 场景**。用例是业务视角（测什么），场景是实现视角（怎么测）。二者多对多。

## 0. 概念关系（先立骨架）

```
标签 (Tag)  ──用于划分用途，任务按标签选，不直接选用例
   │
用例 (Case) = 数据 + 场景引用 + 标签       ← 业务视角：测什么，不含实现细节
   │  ↕ 多对多（一个场景可被多个用例关联）
场景 (Scenario) = 步骤集合 + 局部变量      ← 实现视角：怎么测
   │
步骤 (Step) = 类型 + 描述 + 参数/方法 + 后置校验   ← 类型化，参数各异
   │
方法脚本 (Method Script) = 基础脚本库 + 项目脚本库  ← Python 代码住这里
```

**为什么这样分**：

- **用例不解释"这一步是干啥的"**——那样会写得很详细，用例的重点反而抓不住。用例只抓业务重点。
- **场景取具体实现细节**，它的作用就是**实现用例**。
- 一个场景可被多个用例关联，因为场景是"实现某一类步骤的集合"。

## 1. 标签（Tag）

| 字段 | 说明 |
|---|---|
| `id` / `code` | 稳定 ID |
| `project_id` | |
| `name` | 如 `smoke` / `regression` / `p0` / `payment` / `nightly` |
| `category` | 标签分类：`purpose`(用途) / `priority`(优先级) / `module`(模块) / `type`(测试类型) |
| `description` | |

**标签挂在用例上**（多对多 `mc_test_case_tag`）。**任务通过标签选用例**——"跑所有 `smoke` 标签的用例"。

标签分类（`category`）的意义：任务可以按不同维度组合选例，如"`module=payment` 且 `priority=p0`"。

## 2. 用例（Case）—— 业务视角

用例 = **数据 + 场景引用 + 标签**。不写实现细节。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `code` | UUID / varchar | `TC-xxxx` 稳定 ID |
| `project_id` | UUID | |
| `title` | varchar | 用例标题 |
| `intent` | text | 意图（一句话：测什么） |
| `preconditions` | JSONB | 前置条件（业务级描述，非实现） |
| `expected` | JSONB | 预期结果（业务级） |
| `dataset_id` | UUID FK null | **数据**（本用例绑定的数据集） |
| `confirm_source` | varchar | `debug` / `doc` / `user_confirm`（创建依据，必填） |
| `confirm_ref` | JSONB | 确认依据（question_id / 文档 / 调试记录） |
| `priority` | varchar | P0-P3 |
| `automation_status` | varchar | `not_automated` / `in_progress` / `automated` / `broken` |
| `status` | varchar | `draft` → `pending_confirmation` → `active` → `deprecated` → `archived` |
| `version` | int | |
| `created_by` | UUID | 设计者 = 责任人 |

> 用例**不含步骤**——步骤属于场景。用例通过场景引用获得实现。

### 2.1 用例 ↔ 场景（多对多）

`mc_test_case_scenario`：`id, case_id, scenario_id, is_primary(bool), order`

- 一个用例可关联多个场景（如"正常流程"+"异常流程"）
- 一个场景可被多个用例关联
- `is_primary` 标主场景（默认执行）

### 2.2 用例 ↔ 标签（多对多）

`mc_test_case_tag`：`id, case_id, tag_id`

## 3. 场景（Scenario）—— 实现视角

场景 = **步骤集合 + 局部变量管理**。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` / `code` | UUID / varchar | `SCN-xxxx` |
| `project_id` | UUID | |
| `name` | varchar | 场景名 |
| `purpose` | text | 实现用途（面向实现者） |
| `transport_id` | UUID FK | 传输层（鉴权/公共数据） |
| `env_id` | UUID FK null | 默认环境（可被任务覆盖） |
| `local_vars` | JSONB | **局部变量定义 + 初值**（见 3.1） |
| `flow` | JSONB | 编排（条件/分支/提前结束/引用前置场景） |
| `status` / `version` | | `draft` → `verified` → `active` → `broken` → `archived` |

### 3.1 局部变量管理（你指出我删掉的关键）

**场景必须能管理局部变量**——步骤之间、步骤与校验之间、场景与用例数据之间都要能互相使用。

| 变量来源 | 语法 | 作用 |
|---|---|---|
| 场景局部变量 | `{{local.xxx}}` | 场景内定义，跨步骤传递（**核心**） |
| 用例数据 | `{{data.xxx}}` | 用例绑定的数据集行 |
| 环境变量 | `{{env.xxx}}` | 环境继承链 |
| 鉴权产物 | `{{auth.xxx}}` | 传输层登录后 |
| 步骤返回 | `{{response.xxx}}` | 当前步骤 |
| 全局 | `{{global.xxx}}` | 跨场景（慎用） |

**局部变量的声明与流转**：

```yaml
local_vars:
  order_id: null          # 声明 + 初值
  token: null
  retry_count: 0

steps:
  - seq: 10
    type: request
    post_cond:            # 步骤产出写入局部变量
      - set: "{{local.order_id}} = {{response.json.data.order_id}}"
  - seq: 20
    type: request
    params:
      url: "/orders/{{local.order_id}}"   # 后续步骤消费
```

**上下用例互相使用**：通过**引用前置场景**（`flow.pre_scenarios`）实现——前置场景的 `local_vars` 与产出**传递**给后续场景。

```yaml
flow:
  pre_scenarios: [SCN-0001]   # 先跑前置场景（如登录、造数据）
  # 前置场景的 local_vars 在此场景可继续引用
```

## 4. 步骤（Step）—— 类型化

> 你指出：步骤必须有**描述**、有**类型**、参数**更灵活**（不只一个参数、允许执行方法）、后置校验也**有类型**。

### 4.1 通用字段

| 字段 | 说明 |
|---|---|
| `id` / `scenario_id` | |
| `step_key` | 场景内唯一标识（被 flow 引用） |
| `seq` | 执行序号（**唯一顺序真相**） |
| `type` | **步骤类型**（见 4.2） |
| `description` | **步骤描述**（人读，说明这步在干啥） |
| `params` | 参数（**结构随 type 变**） |
| `pre_cond` | 前置条件（断言列表） |
| `post_checks` | **后置校验**（类型化，见 4.4） |
| `post_cond` | **后置赋值——写局部变量**（`{{local.x}} = ...`，§3.1 的流转机制；引擎/校验器均以此字段名读取） |
| `on_error` | 错误策略 |
| `enabled` | |

### 4.2 步骤类型（type）

每种类型参数结构不同：

| type | 用途 | 参数要点 |
|---|---|---|
| `request` | 发送请求 | method/url/headers/body/params |
| `page` | 操作页面 | action(click/input/navigate/scroll)/selector/value |
| `android` | Android 操作 | action(tap/swipe/type)/坐标或元素 |
| `ios` | iOS 操作 | 同上 |
| `wait` | 等待 | seconds / until(条件) |
| `script` | **执行脚本** | `method_ref`（调用方法脚本）或内联 `code` |
| `check` | **校验数据** | 断言列表（见 4.4） |
| `sql` | 数据库操作 | query/execute + sql |
| `ws` | WebSocket | send/receive |
| `custom` | 自定义工具 | tool_ref + params |

**流程控制步骤**（2026-10-13 扩展，用户定稿：判断/跳出/结束场景，含引擎真实执行）：

| type | 用途 | 参数要点 |
|---|---|---|
| `if` | 判断（开块） | `params.condition`（表达式，复用 eval_expression） |
| `else_if` | 否则如果 | `params.condition`；前面分支都没命中时才求值 |
| `else` | 否则 | 前面都没命中时活跃 |
| `end_if` | 结束条件（闭块） | 无参数；每个 if 块必需 |
| `break` | 跳出 | 可选 `params.condition` 门；跳出当前块到其 end_if，顶层用则结束场景 |
| `end_scenario` | 结束场景 | 可选 `params.reason`；立即结束整个场景（判定 pass + early_exit） |

实现：平铺 seq 步骤 + **区域栈解释器**（`executor._apply_control_flow`）——`if` 开帧、
`end_if` 闭帧，落在未命中分支里的普通步骤直接 skip；不引入嵌套数组，保持
`mc_test_scenario_step` 现有形态。控制步骤不跑工具、无后置校验（R5 豁免），
成块合法性由校验器 R22（`_check_control_blocks`）强制：配对平衡、else_if/else 必须在块内。

### 4.3 参数的灵活性（你的要求）

**参数不限于"一个值"，允许：**

1. **多参数**：结构化 params（JSONB）
2. **执行一个方法**：`method_ref` 指向方法脚本里的方法（同步/异步均可）
3. **内联代码**：`code` 字段（Python 片段，受控）

```yaml
# 形态 1：普通参数
- seq: 10
  type: request
  params: { method: POST, url: "/orders", body: {...} }

# 形态 2：调用方法脚本
- seq: 20
  type: script
  description: "调用加解密方法生成签名"
  method_ref: "base.crypto.sign"        # 指向方法脚本
  args: { payload: "{{local.body}}", key: "{{env.secret}}" }
  post_cond:
    - set: "{{local.sign}} = {{result.sign}}"

# 形态 3：内联代码（受控）
- seq: 30
  type: script
  code: |
    async def run(ctx):
        return {"ts": ctx.now_ms()}
```

### 4.4 后置校验（post_checks）—— 类型化

后置校验**有类型**，且**可以执行脚本**：

| check 类型 | 说明 |
|---|---|
| `status_code` | 状态码 |
| `json_path` | JSON 字段断言 |
| `contains` / `regex` | 字符串 |
| `element_visible` / `element_text` | UI 断言 |
| `db_query` | SQL 校验 |
| `screenshot_diff` | 截图对比 |
| `method` | **执行一个方法脚本做校验** |
| `script` | **执行内联校验脚本** |

```yaml
post_checks:
  - type: status_code
    expected: 201
  - type: json_path
    path: "$.data.order_id"
    operator: not_null
  - type: method                      # 执行方法脚本校验
    method_ref: "base.assert.amount_match"
    args: { actual: "{{response.json.data.amount}}", expected: "{{data.amount}}" }
```

## 5. 方法脚本（Method Script）—— Python 代码的家

> 你指出：应该有**全局方法脚本**，分**基础脚本**和**项目脚本**两部分。

| 分类 | 内容 |
|---|---|
| **基础脚本（平台提供）** | 请求管理类、缓存脚本类（登录后 token 缓存）、网页操作类、Android 操作类、iOS 操作类、时间类、随机类、加解密类…… |
| **项目脚本（项目特有）** | 项目特定需要的类（业务封装） |

### 5.1 数据模型

**`mc_test_method_module`**（方法模块）：

| 字段 | 说明 |
|---|---|
| `id` / `code` | 如 `base.crypto` / `proj.payment` |
| `project_id` | 基础脚本 `project_id=null`（全局），项目脚本绑项目 |
| `kind` | `base` / `project` |
| `name` / `description` | |
| `source` | **Python 源码** |
| `async_native` | bool（是否异步原生） |
| `status` / `version` | |

**`mc_test_method`**（模块内方法，注册表）：

| 字段 | 说明 |
|---|---|
| `id` / `module_id` | |
| `name` | 方法名，如 `sign` |
| `full_ref` | `base.crypto.sign` |
| `signature` | 参数/返回契约 |
| `is_async` | 同步/异步 |
| `description` | |

### 5.2 异步约定（你强调的）

> **最外层都是异步；步骤内方法允许同步异步。**

- 框架层：**全异步**（`async def`）
- 方法脚本：可写同步或异步，框架统一适配（同步方法放线程池，异步方法直接 await）
- 步骤执行：`await step.run(ctx)`
- 内联代码：`async def run(ctx)`

```python
# 基础脚本示例：base.crypto
async def sign(payload: dict, key: str) -> dict:
    ...  # 异步实现

def md5(text: str) -> str:          # 同步也允许
    ...

# 步骤内调用：框架自动判定
result = await maybe_await(module.sign(payload, key))
```

## 6. 表清单（本模型）

| 表 | 说明 |
|---|---|
| `mc_test_tag` | 标签 |
| `mc_test_case` | 用例（业务视角） |
| `mc_test_case_tag` | 用例↔标签 |
| `mc_test_case_scenario` | 用例↔场景（多对多） |
| `mc_test_scenario` | 场景（实现视角，含 local_vars） |
| `mc_test_scenario_step` | 步骤（类型化） |
| `mc_test_method_module` | 方法脚本模块（base/project） |
| `mc_test_method` | 方法注册表 |
| `mc_test_dataset` | 数据集 |
| `mc_test_transport` | 传输层 |

## 7. 与旧设计的差异（对照）

| 旧 | 新 |
|---|---|
| 用例 = 扁平流程说明，含 steps | **用例 = 数据 + 场景引用 + 标签**，不含步骤 |
| 用例↔脚本 直接关联 | **用例↔场景 多对多**；场景才是步骤集合 |
| 场景只有编排 | **场景含局部变量管理**（补回） |
| 步骤无类型、无描述 | **步骤有 type + description**，参数随类型变 |
| 后置 = 变量提取 | **后置校验类型化**，可执行方法/脚本 |
| 无方法脚本概念 | **基础脚本 + 项目脚本**，Python 代码住这里 |
| 无标签 | **标签驱动任务选例** |

## 8. 设计决策

| # | 问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | 标签分类的枚举 | **固定分类 + 自由标签并存**：`category` 固定四类（purpose/priority/module/type），标签名自由 | 固定分类保证任务能按维度组合选例；标签名自由保证业务表达 |
| 2 | 方法脚本源码存储 | **DB text**（`mc_test_method_module.source`） | 与脚本/场景同库，事务一致、便于版本与 diff；对象存储留给大文件 |
| 3 | 内联代码（`code` 字段）v1 | **不开放** | 纯声明式 + 方法脚本已够；开放内联代码会撑大幻觉面与沙箱复杂度 |
| 4 | 方法脚本沙箱 | **import 白名单（标准库 + 显式允许的三方）；默认禁网；强制超时；内存上限** | 与 §08 强制层一致；白名单由平台维护、可版本化 |
