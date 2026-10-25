# 06 · 脚本引擎（异步）

**状态：设计已定** ｜ 前置：01 资产模型

> 引擎是**纯确定性**模块。**框架层全异步**；步骤内方法允许同步/异步，框架统一适配。

## 0. 引擎干什么

```
读场景 + 步骤 + 传输层 + 环境 + 数据
  → 解析变量 → 传输层鉴权 → 逐步骤执行（按类型分发）→ 后置校验 → 错误策略
  → 写结果（run_scenario / step_result / evidence）
```

## 1. 异步约定（你强调的）

| 层 | 约定 |
|---|---|
| **框架层** | **全异步**（`async def`），最外层都是异步 |
| **方法脚本** | 可写同步或异步，框架用 `maybe_await` 适配 |
| **步骤执行** | `await step.run(ctx)` |
| **内联代码** | `async def run(ctx)` |

```python
async def maybe_await(value):
    """同步方法放线程池，异步方法直接 await。"""
    if inspect.isawaitable(value):
        return await value
    return value

# 步骤内调用方法脚本
result = await maybe_await(module.sign(payload, key))
```

**为什么**：请求/浏览器/设备操作本质是 IO，异步才能并发；同步的纯计算（加解密、时间格式化）不必强制改写。

## 2. 变量系统

### 2.1 作用域

| 前缀 | 来源 | 生命周期 |
|---|---|---|
| `{{local.xxx}}` | **场景局部变量**（跨步骤传递） | 单场景（前置场景可传递） |
| `{{data.xxx}}` | 用例绑定的数据集行 | 单次迭代 |
| `{{env.xxx}}` | 环境变量（含继承合并） | 整个执行 |
| `{{auth.xxx}}` | 传输层鉴权产物 | 登录后 |
| `{{response.xxx}}` | 当前步骤返回 | 单步骤 |
| `{{result.xxx}}` | 方法/脚本步骤返回 | 单步骤 |
| `{{global.xxx}}` | 任务全局参数 | 本次运行 |

**优先级**：`local > data > auth > env > global`（近者优先）。

### 2.2 解析算法

```
resolve(expr, ctx):
  1. 匹配 {{路径}}
  2. 按优先级查作用域
  3. 取到 → **保持原类型**替换；取不到 → 报未定义变量（不静默变字符串）
  4. 表达式（条件）：{{a}} > 100 → 求值为 bool
```

**两条铁律**：
- **类型保持**：int 就是 int，否则比较会错
- **未定义即报错**：不替换成空串

### 2.3 环境继承合并

```
merged_env(env_id):
  chain = 向上递归 parent
  检测环（同 id 出现两次 → 报错）
  result = {}
  for e in reversed(chain): result.update(e.vars)   # 父先子后，子覆盖父
  return result
```

### 2.4 局部变量流转（场景核心）

```
场景执行:
  ctx.local = init(scenario.local_vars)      # 声明 + 初值
  步骤 post_cond 写: ctx.local[k] = v        # 步骤产出写入
  后续步骤 params 读: {{local.k}}            # 消费
  前置场景: execute(pre, ctx) 后其 local 保留 → 当前场景可继续用
```

### 2.5 敏感变量

`is_secret` 变量：执行时服务端注入真值，**日志/报告/trace 一律脱敏**。

## 3. 步骤执行（按类型分发）

| type | 执行 |
|---|---|
| `request` | 调 `http` 工具（method/url/headers/body） |
| `page` | 调 `browser` 工具（click/input/navigate/scroll） |
| `android` | 调 `device_control`（Android 驱动） |
| `ios` | 调 `device_control`（iOS/WDA 驱动） |
| `wait` | 等待（seconds / until 条件） |
| `script` | **执行方法脚本**（`method_ref`）或内联 `code` |
| `check` | **校验数据**（断言列表） |
| `sql` | 数据库（query/execute） |
| `ws` | WebSocket 收发 |
| `custom` | 自定义工具（tool_ref） |

**参数灵活性**：
- 多参数：结构化 `params`
- 执行方法：`method_ref` + `args`（同步/异步均可）
- 内联代码：`code`（受控）

## 4. 后置校验（类型化）

| check 类型 | 执行 |
|---|---|
| `status_code` / `json_path` / `contains` / `regex` | 断言库 |
| `element_visible` / `element_text` | UI 断言 |
| `db_query` | SQL 校验 |
| `screenshot_diff` | 截图对比 |
| `method` | **执行方法脚本校验** |
| `script` | **执行内联校验脚本** |

```
run_post_checks(step, ctx):
  for c in step.post_checks:
     r = await evaluate_check(c, ctx)     # 支持 await（method/script 类）
     record(r)                            # 存 expected/actual/diff
     if not r.pass: raise CheckError(r)
```

**规则**：每步骤至少一条后置校验（校验门强制）。

## 5. 错误策略

| `on_error` | 行为 |
|---|---|
| `retry(n)` | 原地重试 n 次 |
| `reinit` | 重跑传输层鉴权 |
| `evict` | 清理会话重开 |
| `skip` | 跳过该步 |
| `abort` | 终止整个场景 |
| `fail` | 直接失败（默认） |

## 6. 方法脚本（Python 代码的家）

### 6.1 组织形式：一个脚本 = 一个类

| 项 | 约定 |
|---|---|
| 脚本单位 | **一个脚本文件 = 恰好一个顶层 Python 类**（保存时 AST 硬校验，多类/顶层函数/实例方法一律拒绝） |
| 方法 | 类内方法**全部 `@staticmethod`**（非静态、dunder 一律拒绝） |
| 说明 | 类备注=**类 docstring**，方法说明=**方法 docstring**，入参说明=docstring `Args:` 段（Google 风格） |
| 作用域 | `project_id` 为空=**全局内置类**，有值=**项目类**；列表按「全局 + 本项目」展示（不再有 `kind=base/project` 标签） |
| 类间互调 | 同作用域（本项目 + 全局）所有类源码 `exec` 进**同一命名空间**，A 类方法体内可直接 `Http.fetch(...)`；类名同作用域唯一（保存期校验） |
| 内置基础类 | 启动时全局播种 **Time / Random / Crypto / Codec** 四类（`project_id=NULL`，`status=active`，所有项目共享）；**幂等、只补不覆盖**（用户对全局类的编辑不被重启还原）。见 `testing_platform/assets/builtin_methods.py` |

### 6.2 调用

```yaml
- seq: 20
  type: script
  description: "生成请求签名"
  method_ref: "Crypto.sign"
  args: { payload: "{{local.body}}", key: "{{env.secret}}" }
  post_cond:
    - set: "{{local.sign}} = {{result.sign}}"
```

- `method_ref` 格式：`<类名>.<方法名>`（`rpartition(".")` 解析，如 `Crypto.sign` / `Payment.build_order`）
- 运行时按 `(类名, version)` 指纹编译并缓存命名空间 → 取该类静态方法 → `inspect.signature` 绑定 `args` → 同步走线程、异步直接 await（超时兜底）
- 返回写入 `{{result.*}}`（同时写 `{{response.*}}`，便于 `json_path` 断言）

### 6.3 沙箱与安全（当前口径）

| 项 | 现状 |
|---|---|
| 隔离 | **进程内 `exec`**，无 import 白名单/禁网（暂缓）；类源码仅经 AST 结构校验，内容不审查 |
| 超时 | **强制**（默认 30s，可配）；注：同步方法跑在线程里，`asyncio` 超时只放弃等待、不杀死线程 |
| 异常 | 用户代码异常包成可读错误，**不回显 args 值**（可能含密钥明文） |
| 资源限制 | 内存/CPU 限额**未做** |

> 完整沙箱（白名单 import、默认禁网、子进程 + Job Object 资源限额）留后续里程碑；当前依赖「源码由平台内有权者编写 + 变更门留痕」控制风险。

## 7. 与 AI 的边界

| 环节 | 谁做 |
|---|---|
| 生成场景/步骤（写文档） | AI |
| 校验 | **校验器**（确定性） |
| 执行 | **引擎**（确定性） |
| 后置校验判定 | **断言库/校验器**（确定性） |
| 失败诊断 | AI（Debugger，hybrid） |

**引擎不调 LLM，不接受运行时 AI 介入判定。** AI 只能改场景（改完重过校验门 + 重跑）。

## 8. 设计决策

| # | 问题 | 决策 | 理由 |
|---|---|---|---|
| 1 | 内联代码（`code`）v1 | **不开放**（同 01 §8） | 纯声明式 + 方法脚本已够 |
| 2 | 方法脚本源码存储 | **DB text**（同 01 §8） | 同库、事务一致、便于 diff |
| 3 | 方法脚本沙箱边界 | 见 §6.3 当前口径（进程内 exec + 强制超时；白名单/禁网/资源限额留后续） | |
| 4 | 引擎运行位置 | **服务端进程内**（同 04 §6） | 结果同进程直接落库 |
| 5 | 方法脚本组织 | **一个脚本 = 一个类**，全 `@staticmethod`，`method_ref = 类名.方法名` | 贴合用户心智模型；AST 保存期硬校验 + 自动登记注册表 |
