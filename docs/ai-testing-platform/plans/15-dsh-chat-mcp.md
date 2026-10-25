# 15 · dsh 接入 + MCP server + 对话（plans/10 待做项收口）

> 前置：`plans/10-dsh-integration.md`（实测结论 + 三个待做项）。本文记录**全部待做项已落地**。

## 一、三个待做项的实现

### 1. MCP server（`surface/mcp_server.py`）

把能力面 107 个方法暴露成**真正的 MCP Streamable HTTP server**：

```
POST /api/v1/testing/mcp    JSON-RPC 2.0（stateless）
  initialize        → serverInfo + capabilities（protocolVersion 2024-11-05）
  tools/list        → surface.mcp.list_tools()（107 个，name=inputSchema 齐全）
  tools/call        → surface.mcp.call_tool()（失败留 isError 信封，不抛传输错误）
  ping              → {}
  notifications/*   → 202（无 id 的客户端通知）
GET  /api/v1/testing/mcp    自述端点（探活/调试）
```

dsh 的 mcp-client 配置（cordis patch 自动生成）：

```yaml
- insert:
    - id: testing-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: testing
        transport: streamable-http
        url: http://127.0.0.1:8001/api/v1/testing/mcp
```

### 2. dsh 驱动（`agent_runtime.py`）

- `DshConfig → DeepSeekHarnessConfig`：`base_url`/`api_key` 指**平台网关**（凭据注入点内建），
  `dsh_home`/`profile`/`patches` 指 `.dsh-home`（cordis patch 注册 testing MCP server）
- `run_chat` / `stream_chat`：SDK 的 `harness.run` 是**同步阻塞**的 → 工作线程桥回事件循环，
  `on_notification` → `notification_to_event` 归一 → SSE 事件
- 事件归一：`session.event` 拆出内层 `type`（`assistant/message` / `tool/call` / `tool/result` /
  `turn/end`…）；`session.status` → `session/status`；其余透传

### 3. 对话后端（`surface/chat.py`）

```
POST /api/v1/testing/chat/send   SSE
  鉴权：session cookie（C 端）或管理员 Bearer，皆无 → 401
  api_key_id → 明文 key（复用 agent/api.py 的 _resolve_caller_api_key；前端不碰明文）
  prompt = [上下文] project_id + 最近 6 条历史 + 本轮消息
  → agent_runtime.stream_chat（dsh + MCP 注入）→ 逐事件 yield `data: <json>`
  终态 done（session_id/final_response）→ `data: [DONE]`
GET /api/v1/testing/chat/health  探活（SDK 装没装/模型/网关/MCP 端点）
```

## 二、实测结论（真跑，非推断）

| 验证 | 结果 |
|---|---|
| dsh 子进程 + 连网关（无 MCP） | ✅ 14 事件，`finish_reason=completed`，回答正确 |
| MCP 注入（连 8011 独立实例） | ✅ dsh **自主发现并调用** `mcp__testing__tool_list_*`，拿到 107 工具并正确作答 |
| `stream_chat`（chat/send 内核） | ✅ 15 事件，含 `tool/call` + `tool/result` + `done` |
| MCP 协议（initialize/tools/call/错误路径） | ✅ 18 例测试全绿 |

探针（密钥从 `D:/pip-tmp/gw.key` 读，**不入库**）：
- `probes/testing_dsh_probe.py` — dsh 运行时（`--no-mcp` 验推理链路 / `--mcp-url` 验 MCP 链路）
- `probes/testing_mcp_server.py` — 独立跑测试能力面（8011，不碰用户主服务）

## 三、前端（`/testing/chat`）

- 选 API Key + 模型 → 发消息 → SSE 逐事件渲染
- `assistant/message` 累加文本；`tool/call`/`tool/result` 渲染**工具卡片**（amber 边框）
- `done` 收尾 + `session_id` 续轮（dsh session 维持跨轮上下文）
- `project_id` 随对话传（工具调用时它就是 project_id）
- 探活卡：SDK 状态 / 默认模型 / 网关 / MCP 端点

## 四、测试

`tests/test_dsh_chat_mcp.py`（18 例）：cordis patch 形态（含 token header）、
`DshConfig` 字段映射、缺 SDK 可读报错、notification 归一、MCP 协议全覆盖
（initialize/tools 数量=注册表/tools/call 成功+isError/ping/-32601/-32700/202/GET）、
chat health/401/prompt 构造（含 project_id 注入）/路由挂载。

## 五、部署注意（重启后生效）

主服务（8001）**重启后**自动挂上 `/api/v1/testing/mcp` 与 `/api/v1/testing/chat/*`
（`surface/api.py` 里 include，`main.py` 无需改动）。`.dsh-home` 首次使用时自动生成
cordis patch；profile 沿用 `sdk-minimal`（需已存在 `dsh_home/profiles/sdk-minimal`，
探针用的是 `D:/pip-tmp/dsh-home`，生产可设 `TESTING_DSH_HOME` 指到常驻目录）。

## 六、仍未做（已由 [16 号计划](./16-remaining-closeout.md) 收口）

- ~~**对话持久化**~~ → 已落 `mc_test_chat_session/_message` + 会话 REST（前端历史侧栏续聊）。
- ~~`sync_policy=scheduled` 的 cron / webhook 触发~~ → 已落 `scheduler.py` + `source_sync.py`。
- ~~锚点闸通过后自动建 `covered_by` 边~~ → 已在 `knowledge.put` 建 `grounded_in` 边。
- ~~analyst 工作流的自动收口~~ → 已加 `analyst.commit`。
