# dsh 接入实测结论（2026-10-03）

> 用户定的方向：**测试平台接 dsh（官方 Python SDK）**，dsh 当测试 agent，跑测试工作流、调测试平台的 71 个方法。本文件是**实测**结论（非推断）。

## 一、包与安装（**纠正之前的错误判断**）

- **官方包名 = `deepseek-harness-sdk`**（用户确认 + 官方文档）。**PyPI 的 `deepseek-harness`（不带 -sdk）是第三方 DeepSeek API 客户端，接错的东西**。
- 从 GitHub 源装（PyPI 索引搜不到，但包名对）：
  ```bash
  git clone https://github.com/deepseek-ai/deepseek-harness.git
  cd deepseek-harness && python -m pip install deepseek-harness-sdk
  ```
  或用 `pip install "deepseek-harness-sdk @ git+https://github.com/deepseek-ai/deepseek-harness.git"`
- 装了两个 wheel：`deepseek-harness-sdk`（13KB）+ `deepseek-harness-runtime-bin`（**72MB 原生运行时**，含 `dsh` 命令）
- **import 名 = `deepseek_harness`**（不是 `deepseek_harness_sdk`）
- 官方明说：**普通 SDK 运行不需要系统 Node.js**

## 二、真实 API（实测）

```python
from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, Notification, RunResult

cfg = DeepSeekHarnessConfig(
    provider="deepseek-official",
    model="claude-haiku-4-5-20251001",
    base_url="http://127.0.0.1:8001/v1",   # ← 平台网关（凭据注入点内建！）
    api_key=<平台 key>,                     # ← 直接配，不用写 settings.yaml（比 TS SDK 简单）
    max_tokens=49152,
    cwd=<workspace>, dsh_home=<隔离 HOME>,
    profile="sdk-minimal",
    request_timeout_seconds=60,
)
with DeepSeekHarness(cfg) as h:
    result = h.run("指令", session_id=..., on_notification=lambda n: ...)
    result.final_response
```

- `DeepSeekHarnessConfig` 字段：`provider/model/reasoning_effort/max_tokens/cwd/runtime_cwd/dsh_bin/profile/patches/dsh_home/env/initialize_timeout_seconds/request_timeout_seconds/shutdown_timeout_seconds/`**`base_url`/`api_key`**
- 方法：`start()` / `run(input, *, session_id=None, on_notification=None)` / `start_session()` / `close()`
- `Notification{method, payload}` —— method=事件类型，payload=内容
- `HarnessClient` 低层协议客户端

## 三、实测：**跑通了**（连平台网关 + 出结果）

用平台 key（`127.0.0.1:8001` 创建）+ `model=claude-haiku-4-5-20251001`：
- ✅ dsh 子进程起来、`initialize` 握手成功
- ✅ 连上平台网关（`base_url=http://127.0.0.1:8001/v1`）
- ✅ 返回正确结果（问 "1+1" → "1+1 等于 2。"）
- ✅ `on_notification` 收到 **15 个 Notification**（事件流可用 → 天然做 SSE）

## 四、工具注入：**走 MCP**（实测 + 官方文档）

- `DeepSeekHarnessConfig` **没有** `tools=` 参数——工具不走 config
- 走 **`@deepseek-ai/dsh-mcp-client` 插件**（dsh 自己的配置层）：
  - 挂载 Cordis overlay → 连**配置好的 stdio 服务器**或**Streamable HTTP URL** 的 MCP 工具
  - 调用形式 `mcp__<serverName>__<tool>`
  - 配在 `$DSH_HOME/profiles/<name>/cordis.patch.yml`（profile 级）或 `$DSH_HOME/cordis.patch.yml`（全局）
- **含义**：给 dsh 配一个 MCP server 指向**测试平台的 71 个方法**（`surface/mcp.py` 已把这些方法包成 MCP 工具）→ dsh 就能 `mcp__testing__scenario.create` 这样调

## 五、接入架构（据此定稿）

```
dsh agent（Python SDK，测试服务进程内跑）
   │
   ├─ base_url/api_key → 平台网关 → opus/haiku/sonnet 推理
   └─ MCP → 测试平台 MCP server（71 方法）→ dsh 调 scenario.create/run.execute/...
```

**待做**：
1. 把 `surface/mcp.py` 起成 **MCP server**（HTTP URL 或 stdio）——dsh 的 mcp-client 连它
2. 写 `cordis.patch.yml` 模板（注册 testing MCP server）
3. 对话后端：`DeepSeekHarness` 驱动 + `on_notification` → SSE

## 六、环境坑（记录）

- 官方 runtime wheel 72MB，装的时候要过 C 盘 TEMP；**C 盘满会 `No space left on device`**。解法：`TMP=D:/pip-tmp TEMP=D:/pip-tmp` + `--cache-dir D:/pip-cache`
- 平台 key 格式 `sk-xxxx`（46 字符），不是 `sk-proj-`（那是 OpenAI 官方格式，平台不认）
