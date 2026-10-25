# 浏览器代理方案（CDP 插件，类 SwitchyOmega 3）

状态：**已全部完成**（2026-10-03）。Phase 0 三个实测闸门 ✅（本机 Chrome 153.0.8010.36，见 §11）；Phase 1–5 全部落地（含双向同步 + 跨浏览器两步导入 + 管理页），相关回归 172 passed + 真实浏览器 E2E 通过。
已拍板：Profile 真相源=新建 PG 表；SwitchyOmega 备份导入要做；**Agent 无权改出网路径**。

## 0. 结论摘要

把"代理"拆成**三个平面**，分别落在三处，互不干扰：

| 平面 | 是什么 | 落在哪 | 对应原始需求 |
|---|---|---|---|
| **出版面（Profile 平面）** | 这台浏览器自己走哪出网：direct / system / 固定代理 / PAC / 自动切换规则 | 扩展 `chrome.proxy` + 服务端持久化的 Profile 库 | 需求 2「调整网页的代理」+ 需求 3「用浏览器所在环境的代理」 |
| **数据面（出口平面）** | 别人借这台浏览器出网（浏览器当代理节点） | 独立 WS 端点 `/mcp/cdp-bridge/ws/proxy` + `ProxyManager` 新 `browser` 模式 | 需求 1「作为一个代理节点」 |
| **控制面** | 配置下发、状态回传、能力声明 | 复用 CDP 桥现有鉴权与 `client_id` 隔离 | 三条需求的公共骨架 |

关键判断：

1. **需求 2 做不到"当前页面"粒度**，Chrome 没有 per-tab 代理。SwitchyOmega 3 自己也不是 per-tab——它是 **per-request 规则（编译成 PAC）**。本方案对齐到这个粒度：**按域名/URL 规则分流**，而不是按标签页。
2. **需求 1 做不成"一个别人能填的代理地址"**。MV3 扩展没有 listen API，不能当 HTTP/SOCKS 服务器。只能做成**回拨出口**：服务端把请求经 WS 下发 → 扩展 `fetch()` → 流式回传。形态与 `NodeConnectManager` 的 node 模式完全对称。
3. **需求 3 几乎免费**：出网链路就是 `fetch()` → `chrome.proxy` → 系统/Clash，不用自己写上游链。
4. 端点必须独立（已定版）：`/ws/proxy`，**代理相关的帧全部走这条**，`/session` 一行不改。

## 1. 需求 → 能力映射（对齐 SwitchyOmega 3）

| SwitchyOmega 3 的能力 | 本方案对应 | 差异/取舍 |
|---|---|---|
| 情景模式（Profile）列表 | `ProxyProfile` 集合，服务端持久化 + 扩展本地缓存 | 多了"服务端真相源"，因为 Agent/管理端要能下发 |
| direct / proxy / pac / system / auto switch 五型 | 全部支持，`type` 字段区分 | `auto_detect` 不要（无意义） |
| 自动切换规则：host 通配 / host 正则 / URL 通配 / URL 正则 / 关键字 | 同一套 5 种 `condition.type` | 编译成 PAC `FindProxyForURL`，与 SwitchyOmega 同实现路线 |
| 弹出菜单快速切换 | popup 新增「代理」分区，切换即生效 | — |
| 不改动浏览器其它设置、可随时还原 | `appliedBy` + 租约 + 断连还原 + popup 一键还原 | **新增要求**：改的是用户日常浏览器，必须有归属和还原 |
| 方案导入/导出 | Profile JSON 导入导出（含版本字段） | 兼容读 SwitchyOmega 备份（尽力而为） |
| 代理认证（user/pass） | `fixed` 型带 `auth` | MV3 `chrome.proxy` 不直接吃账密，需 `webRequest onAuthRequired` 或 `auth` 字段方案，见 §7 坑 5 |

> **Agent 不参与改出网路径**。Profile 的 CRUD/切换只归 popup 与管理端；Agent 只有只读 `browser_proxy_status` + 借这台浏览器出网（数据面）。理由：`chrome.proxy` 改的是整台浏览器的全局出网路径，比截图/跑 JS 敏感得多，没有理由让对话里的 Agent 直接动手。原始需求 2「调整网页的代理」的执行方就是用户/管理端，不是 Agent。

## 2. 现状锚点（代码事实）

- 扩展入口 `mcp_builtin/cdp_bridge/tmwd_cdp_bridge/background.js`：
  - `handleExtMessage()` 命令表（`cdp` / `batch` / `net_*` / `tabs` / `management` / `contentSettings` / `chat_*` / `pwd_*`）——代理控制帧在这里加分支。
  - `connectWS()`（约 1536 行起）单 socket + `socketEpoch` 代次守卫 + `already_connected` 专属退避。**代理 socket 不复用这套状态机**。
- `manifest.json`：权限里没有 **`proxy`**，必须新增（`webRequest` 若做代理认证也要加）。
- 服务端 `mcp_runtime/sse_gateway.py`：
  - `@router.websocket("/{service_name}/session")` → `ext_session()`，`_EXT_IDLE_TIMEOUT = 60`，`_route_ext_frame()` 按 `frame_type` 分派。
  - `_authenticate_ext_frame()`（约 1186 行）：`authenticate_client` → `bind_client`，失败回 `already_connected`。**这条单槽约束是拆分端点的核心原因**。
- `mcp_builtin/cdp_bridge/TMWebDriver.py`：
  - `bind_client()`（143 行）一个 `client_id` 只允许一条活动连接，抢占靠 `_SLOT_STALE_TIMEOUT`。
  - `apply_clients()`（116 行）token 轮换/吊销时只断 `ws_client` —— **代理 socket 必须一起断，否则吊销后还能出网**。
  - `unregister_client()`（216 行）只在 `cctx.ws_client is client` 时清槽。
- 出口侧现成骨架（对称参照物，不改动）：
  - `providers/proxy_manager.py`：`mode ∈ {network, url_prefix, direct, node}`，`_request_via_node()` 是"把请求描述下发给远端、远端自己发、流式回传"。
  - `node_server/node_proxy_gateway.py`：`build_node_proxy_router()` 挂 `POST /internal/node-proxy/{node_id}`，状态/头走 `x-node-proxy-*` 响应头，body 流式。
  - `node_server/node_connect_manager.py`：`NodeConnectManager.request(node_id, method, url, headers, body)` → 带 `iter_chunks()` 的响应对象。
  - `node_server/shared_store.py:168 resolve_proxy_fields()`：池条目 → 帧字段；`node` 模式明确拒绝给节点自身用（同类自环判定可借鉴）。

## 3. 架构

```
 管理端/用户端/Agent
        │  CRUD Profile、切活跃 Profile、下发临时租约（HTTP）
        ▼
 主服务  ── 持久化：browser_proxy_profiles（PG）
        │
        │  proxy_ctl 帧（配置下发 / 状态回传）
        ▼
 /mcp/cdp-bridge/ws/proxy   ← 新端点，独立读循环 + 独立超时/限流
        │            ▲
        │ proxy_ctl  │ proxy_request / chunk / end / cancel（数据面）
        ▼            │
 ┌──────────────── 扩展（MV3 Service Worker）────────────────┐
 │ ProxyService：Profile 解析 → 编译 → chrome.proxy.settings  │
 │   ├ direct/system/pac → 直接映射 ProxyConfig               │
 │   └ auto  → 编译 FindProxyForURL（data URL）               │
 │ ProxyStore：chrome.storage.local 镜像（离线可用+快速切换）  │
 │ Restore：租约到期/断连/手动接管 → settings.clear()          │
 └───────────────────────────────────────────────────────────┘
        │ fetch()（出口平面）
        ▼
   chrome.proxy 生效配置 ──→ 系统代理 / Clash 7890 ──→ 出口 IP
```

三个平面在扩展里汇聚到同一个 `chrome.proxy` 状态：**出口平面（数据面）的流量天然吃出版面（Profile）的配置**——这就是需求 3 的实现方式，也是为什么 §7 坑 1（自环）必须处理。

## 4. 扩展侧设计

### 4.1 Profile 模型

```js
// 与 SwitchyOmega 的情景模式对齐；type 决定其它字段必填项
{
  id: 'pp_xxx',
  name: 'Clash 7890',
  type: 'direct' | 'system' | 'fixed' | 'pac' | 'auto',
  fixed: {                       // type=fixed
    scheme: 'http'|'https'|'socks4'|'socks5',
    host: '127.0.0.1', port: 7890,
    bypass: ['<local>', '127.0.0.1', 'bridge.example.com'],  // bypass 必含桥接 host
    auth: { username: '', password: '' }                      // 见 §7 坑 5
  },
  pac: { url: '' /* 或 data: '' */ },                          // type=pac
  auto: {                                                      // type=auto
    rules: [
      { condition: { type: 'hostWildcard', pattern: '*.example.com' }, profileId: 'pp_1' },
      { condition: { type: 'urlRegex',      pattern: '^https://x/' },  profileId: 'pp_2' }
    ],
    defaultProfileId: 'pp_direct'
  },
  scope: 'regular' | 'incognito_session_only',                 // 默认 regular
  priority: 100                                                // 多 Profile 争用时的顺序
}
```

`condition.type` 五种，语义与 SwitchyOmega 一致：`hostWildcard` / `hostRegex` / `urlWildcard` / `urlRegex` / `keyword`。

### 4.2 应用与还原（本方案最容易出事的地方）

- **单一活跃态**：`{ activeProfileId, appliedBy, leaseExpiresAt }`。`appliedBy ∈ {'user','admin'}`——**没有 `agent`**（Agent 无权改出网路径，见 §1）。
- **谁设的可覆盖谁**：`user`（popup 手动）优先级最高，`admin`（管理端下发）不得覆盖用户手动态，除非显式带 `force` 且该客户端开了此能力。
- **必还原**的三个时机，缺一不可：
  1. 租约到期（`leaseExpiresAt`，管理端下发时必带 TTL，默认 30min，上限 24h）；
  2. `/ws/proxy` 断开或 `apply_clients` 吊销；
  3. popup「一键还原」→ `chrome.proxy.settings.clear({scope})`。
- **启动对账**：`onStartup`/SW 唤醒时读服务端活跃配置比对本地，不一致就重放（扩展重启丢内存态是 MV3 常态）。
- **状态可见**：popup 代理分区常显「当前生效：auto（3 条规则）/ 由 Agent 设置 / 12 分钟后还原」。**绝不静默改用户浏览器出网路径**。

### 4.3 自动切换 → PAC 编译

`auto` 型编译成 `FindProxyForURL(url, host)`：命中规则 → `return "PROXY host:port"`（或 `SOCKS5`），无命中 → 默认 Profile。`shExpMatch`/`dnsResolve` 用 PAC 内建函数。规则里的正则要转义成 JS 字面量（`new RegExp(...)` 的源串来自服务端，**必须做长度与字符白名单**，见 §7 坑 6）。

> **闸门 A 已实测通过**（见 §11）：Chrome 153 上 `chrome.proxy.settings.set({mode:'pac_script', pacScript:{data}})` 可用——`settings.get` 回读 `mode=pac_script` 且 `levelOfControl=controlled_by_this_extension`，且 `fetch('https://api.ipify.org')` 走 PAC 指定的 7890 出口、IP=Clash 出口 IP，证明流量真的被路由，不是空跑。**auto 型走 PAC 落定，五种条件全保留，不用退化。**

## 5. 服务端设计

### 5.1 独立端点

`mcp_runtime/sse_gateway.py` 新增：

```python
@router.websocket("/{service_name}/ws/proxy")
async def ext_proxy_session(websocket, service_name):
    if service_name != "cdp-bridge": close(4404)
    # 复用 auth protocol 2 握手；但不调 bind_client
```

与 `/ws/device` 并存于同一 `service_name` 的先例已在代码里（`device_ws`），所以路径形态是既有的，不是新发明。

**独立读循环**要求：

- 独立 `_PROXY_IDLE_TIMEOUT`（流式代理空闲间隔远大于对话，取 180s 起，Phase 0 实测调）；
- 独立 `_route_proxy_frame()`，只认 `proxy_*`，**绝不进 `_route_ext_frame()`**；
- 独立并发闸：`每 client 同时在跑的出口请求数`（默认 4）、`单请求字节上限`、`单请求时长上限`；
- 每帧仍复查 `token_hash`（与 `ext_session` 同口径，防运行时轮换后旧连接继续出网）。

### 5.2 `TMWebDriver` 双槽

`ClientContext` 增加与 `ws_client` 平行的 `proxy_ws_client`：

| 方法 | 改动 |
|---|---|
| `bind_proxy(ws, config)` | 新槽，**不动 `ws_client`**；同样带半开抢占（独立 stale 阈值） |
| `unregister_proxy(ws)` | 只清 `proxy_ws_client` |
| `apply_clients()` | 循环里同时 `disconnect_client(ws_client)` + `disconnect_proxy(proxy_ws_client)`；`disconnect_proxy` 需发自己的 close code（4404）区分 |
| `snapshot_contexts()` | `connected`（控制）与 `proxy_connected`（出口）分列，前端能看出"对话在线但出口未连" |
| `notify_agents_changed()` | 只遍历 `ws_client`，代理槽不参与 |

### 5.3 帧协议

控制帧（小、JSON、reqId 关联）：

```jsonc
{ "type":"proxy_ctl", "reqId":"c1", "action":"sync_profiles", "profiles":[...], "active":{...}, "leaseExpiresAt":1234 }
{ "type":"proxy_status", "reqId":"c1", "applied":true, "effective":"auto", "appliedBy":"admin", "error":null }
{ "type":"proxy_probe", "reqId":"c1", "urls":["https://ipinfo.io/ip"] }   // 出口 IP 探测
```

数据帧（大、可分块、可取消）：

```jsonc
{ "type":"proxy_request", "proxyId":"p1", "method":"GET", "url":"https://...",
  "headers":{}, "bodyB64":"", "credentials":"omit", "timeoutMs":60000 }
{ "type":"proxy_chunk",   "proxyId":"p1", "status":200, "headers":{}, "chunkB64":"..." }
{ "type":"proxy_end",     "proxyId":"p1" }
{ "type":"proxy_error",   "proxyId":"p1", "code":"upstream_failed", "message":"..." }
{ "type":"proxy_cancel",  "proxyId":"p1" }
```

`chunkB64` 分块大小默认 64KB base64（≈48KB 原文）。

> **闸门 B 已实测**（见 §11）：同机回环、扩展 SW 经 WS 回传 50MB——**JSON+base64 文本帧 20.6 MB/s vs 二进制帧 101.8 MB/s（约 5 倍）**，base64 编码本身吃掉文本路径约一半时间。**决策：Phase 3 数据面的大体量 `proxy_chunk` 改用 WS 二进制帧**（响应头/元数据仍走文本帧或首个 JSON 帧），控制帧 `proxy_ctl/proxy_status` 保持 JSON（本来也小）。Phase 1 出版面不涉及大数据流，不受影响。

### 5.4 `ProxyManager` 新 `browser` 模式

出口池条目加第五模式，与 `node` 对称：

```jsonc
{ "id":"p-9", "mode":"browser", "client_id":"<CDP client_id>", "name":"家里 Chrome" }
```

新增 `BrowserConnectManager.request(client_id, method, url, headers, body)`，接口面与 `NodeConnectManager` 一致（`status` / `headers` / `iter_chunks()` / `close()`），`providers/proxy_manager.py` 里 `_request_via_node()` 旁挂一个 `_request_via_browser()` 分支即可，`OutboundResponse` 不用动。

数据进程→控制进程的跨进程转发同理照抄 `node_proxy_gateway.py`：挂 `POST /internal/browser-proxy/{client_id}`，状态与头走 `x-browser-proxy-*` 响应头。

`resolve_proxy_fields()` 必须像拒绝 `node` 那样**拒绝 `browser` 模式给自己用**（否则浏览器代理节点用浏览器出网 = 自环）。

### 5.5 MCP 工具（Agent 侧入口）

`mcp_builtin/cdp_bridge/` 插件**只给 Agent 暴露只读工具**：

| 工具 | 作用 |
|---|---|
| `browser_proxy_status` | 当前生效 Profile、来源、剩余租约、出口 IP（**只读**） |

`browser_proxy_apply` / `browser_proxy_restore` **不暴露给 Agent**——Agent 改不了出网路径。Profile 的应用与还原入口只有 popup 与管理端 HTTP。

数据面（需求 1）的 Agent 出口走 `ProxyManager` `browser` 模式路由：当某渠道/账号绑了 `browser` 模式的代理池条目时，出站请求自动经该浏览器出网，**Agent 不直接调代理工具**，路由是渠道/账号级配置的。

只读工具仍按场景能力模型声明，`client_id` 域内可选、越界必拒、绝不 `bound[0]`。

## 6. 安全边界

1. **默认不带凭据出网**：`proxy_request.credentials = 'omit'`。要带站点 cookie 是独立能力开关，显式 grant + 每次落审计（带 cookie 的出口 = 凭据外泄面）。
2. **URL 白/黑名单**：拒绝 `file:` / `chrome:` / `*.localhost` / RFC1918 与回环（SSRF 打到用户内网），除非该 Profile 显式允许。
3. **审计**：出口请求落 `client_id, proxyId, method, host, 字节数, 结果, 触发方(agent/task/user)`；Profile 应用/还原落 `client_id, profileId, appliedBy(user/admin), leaseExpiresAt, 结果`。
4. **配额隔离**：代理通道限流独立于对话通道，`already_connected` 语义不外溢（代理 socket 抢占不影响对话 socket）。
5. **Profile 归属**：Profile 属 `client_id`（那台浏览器），不属全局；跨客户端引用 Profile 一律拒。

## 7. 关键坑（逐条对应处置）

| # | 坑 | 处置 |
|---|---|---|
| 1 | **自环**：代理 socket 自身也吃 `chrome.proxy`，Profile 一设就可能把 `/ws/proxy` 一起代理进 Clash，规则一变就断桥（本机 Clash Verge 抖动那类问题的放大器） | `bypass` 强制注入桥接 host（服务端下发时带上，扩展侧再兜底合并）；`<local>` 恒放行；**实测：Chrome 对 loopback（127.0.0.1/localhost）目标永不代理**——桥接在 127.0.0.1 时天然免疫；桥接是远程域名时必须显式进 `bypassList`；探测到桥接断开且 `appliedBy=admin` → **立即还原**再重连 |
| 2 | **per-tab 不存在**，用户说的"当前页面走代理"会被误解成能实现 | 产品口径改成"当前站点走代理"（host 规则），文档与 UI 文案统一；别写"当前页面" |
| 3 | **全局副作用**：改 `chrome.proxy` 影响这台浏览器所有标签页 | 归属 + 租约 + 必还原（§4.2）+ popup 常显；默认 `scope:'regular'` 但提供 `incognito_session_only` 给"别污染我日常"的用法 |
| 4 | **SW 生命周期**：出口长流期间 SW 被杀 → 请求半死，服务端一直等 chunk | 代理 socket 独立 keepalive alarm；chunk 在途即视为活跃；服务端 `proxyId` 侧超时回 `proxy_error{code:'client_gone'}`，**不要**复用 `_EXT_IDLE_TIMEOUT` |
| 5 | **代理认证**：`chrome.proxy` 的 `fixed_servers` 不携带 user/pass，命中 407 需要 `webRequest.onAuthRequired` 补 | 加 `webRequest` 权限 + `<all_urls>` 已有；凭证只从服务端租约下发、不落 `chrome.storage.local` 明文（用 `pwd_cache.js` 同款 AES-GCM 本地镜像策略） |
| 6 | **规则里的正则来自服务端**，`data` URL 里的 PAC 脚本是扩展执行的 JS | 正则长度上限（≤256 字符）+ 字符白名单 + 编译期 try 校验；**绝不做字符串拼接插值**，一律 `JSON.stringify` 注入 PAC 常量表 |
| 7 | **两 socket 的代次守卫**：`socketEpoch` 共用会把代理重连误判成对话连接失效 | 两套 epoch（`chatEpoch` / `proxyEpoch`），`connectWS` 逻辑不合并复用 |
| 8 | **吊销后还能出网** | `apply_clients()` 必须双槽断开 + 每帧复查 `token_hash`（沿用 `ext_session` 口径） |

## 8. 分期实施

**Phase 0 — 实测闸门（不改业务代码）✅ 全部通过（2026-10-03，Chrome 153.0.8010.36，详见 §11）**
- 闸门 A：`pac_script{data}` MV3 可用 ✅（内联 PAC 生效且真实路由流量）→ auto 型走 PAC。
- 闸门 B：JSON 文本帧 20.6 MB/s vs 二进制帧 101.8 MB/s ✅ → 数据面大流量用二进制帧。
- 闸门 C：本机无 TUN（Clash 7890 纯 HTTP 入口，无回环）✅；**loopback 目标 Chrome 永不代理**（意外收获，防自环白送一半）；外网 host 的 `bypassList` 与 PAC `DIRECT` 均实测生效 ✅。

**Phase 1 — 出版面（需求 2+3）✅ 已完成（2026-10-03）**
1. `manifest.json` 加 `proxy` ✅（0.5.4 → 0.6.0）；
2. 扩展 `ProxyService` / `ProxyStore` ✅（`proxy.js`：direct/system/fixed/pac/auto 五型编译、还原链路、租约、SW 启动对账、代理通道 WS）；
3. `proxy_ctl` / `proxy_status` 帧 + 独立端点 `/ws/proxy` + `TMWebDriver` 双槽 ✅；
4. `browser_proxy_status` 只读 MCP 工具 ✅（Agent 无权改出网路径）。

落点：`mcp_builtin/cdp_bridge/tmwd_cdp_bridge/proxy.js`、`mcp_builtin/browser_proxy.py`（PG 表 `browser_proxy_profiles` + admin 路由）、`mcp_runtime/sse_gateway.py`、`mcp_builtin/cdp_bridge/TMWebDriver.py`、`mcp_runtime/builtin_plugins/cdp_bridge_plugin.py`、popup 代理分区。新增测试 `tests/test_browser_proxy_dual_slot.py` + `tests/test_browser_proxy_gateway.py`，相关回归 91 passed；真实出口路由实测通过（Chrome 153 上应用 fixed Profile → fetch 出口 IP = Clash 7890 出口）。

**Phase 2 — `auto` 型（SwitchyOmega 的核心体验）✅ 已完成（2026-10-03）**
规则模型 + PAC 编译 + 规则校验 + popup 规则/Profile 编辑器 + SwitchyOmega 备份导入。
> 规则模型/PAC 编译/校验 Phase 1 已落地；本阶段补 popup 编辑器（新增/编辑/删除 Profile、auto 规则编辑、导入按钮）+ `importSwitchyOmegaBackup`（五种 SO profile 类型映射、SwitchProfile 规则 name→id 解析、PAC 导入）。实测 auto 型 PAC 生效且按规则分流（命中域走代理、未命中走直连）。新增 `tests/test_browser_proxy_import.py`（4 例）。

**Phase 3 — 数据面（需求 1，浏览器当代理节点）✅ 已完成（2026-10-03）**
`proxy_request` / 二进制 `chunk` / `proxy_head` / `proxy_end` / `proxy_error` / `proxy_cancel` 帧 + `BrowserConnectManager` + `ProxyManager` `browser` 模式 + 出口 IP 探测与登记。
> 与设计稿的一处偏差：**不需要 `build_browser_proxy_router` 跨进程端点**——CDP driver 与 ProxyManager 同处主进程，browser 模式是**进程内**直连（node 模式要跨进程 HTTP hop 是因为 NodeConnect stream 只在 control 进程）。落点：`mcp_builtin/browser_proxy.py` 的 `BrowserConnectManager`（+ `_BrowserExchange` / `BrowserProxyResponse`）、`providers/proxy_manager.py` 的 `_request_via_browser` + `_BrowserOutboundResponse` + `set_browser_manager`、`mcp_builtin/cdp_bridge/server.py` 的 `_inject_browser_manager`、`sse_gateway._route_proxy_binary`（二进制帧头 `[idLen][proxyId][payload]`）、扩展 `proxy.js` 的 `handleProxyRequest`（fetch + 流式二进制回传）+ `proxy_cancel`。`node_server/shared_store.resolve_proxy_fields` 拒 `browser` 自用（防自环，同 node 口径）。**真实 E2E 通过**：真实扩展经模拟服务端，GET/POST 二进制帧往返正确。新增 `tests/test_browser_proxy_egress.py`（11 例）。

**Phase 4 — 收尾 ✅ 已完成（2026-10-03）**
前端：管理端/用户端代理池加 `browser` 模式与 `client_id` 绑定。
> `server/admin.py` 的 `_normalize_proxy_item`/`_proxy_id` 支持 browser 模式（client_id 入 seed，同名不同浏览器不撞 id）；`server/proxy_utils.to_manager_config` 映射 `browser → client_id`；前端 `proxyPool.ts`（ProxyMode 加 browser + client_id 透传）、`ProxyPool.tsx`（模式按钮 + 浏览器下拉 + 列表徽章/端点列）、`proxy-pool-create-dialog.tsx`（同上）。popup 代理分区 Phase 1 已落地。

**Phase 5 — 双向同步 / 跨浏览器两步导入 / 管理页 ✅ 已完成（2026-10-03）**

- **双向同步**：Profile 真相源在服务端 PG（按 client_id 打标）。扩展本地任何变更（新增/编辑/删除/导入）经代理通道 `proxy_sync{reqId, profiles[], deleted[]}` **全量上行**，服务端 `sync_profiles_from_extension` 做**全量替换**（`NOT IN keep` 删除 + 墓碑删除），回 `proxy_sync_ack` 让扩展清删除墓碑；空 profiles+空 deleted 触发 no-op 保护（防本地存储被清空误擦服务端）。连接 `auth_ok` 后自动补推一次（离线编辑落地）。管理端改 Profile → `push_proxy_sync_profiles` 下行全量，扩展覆盖镜像并剪对齐后的墓碑。
- **两步导入**（popup + 管理端同形态）：**第 1 步选来源**——本地文件（SwitchyOmega/导出 JSON）或**另一个浏览器**；**第 2 步勾选数据**——列候选 + 复选框，确认才 `proxy_import_apply` 落地。"另一个浏览器"来源经代理通道 `proxy_sources_request → proxy_sources`，服务端按 **owner 域**返回（`get_cdp_client_owner_user_id → list_owner_client_ids → list_profiles_for_client_ids`，帧内身份字段一律忽略）。
- **跨浏览器导入授权边界**：只允许同一 owner 名下互导；管理端 `import-sources`/`import` 端点先校验 `source_client_id ∈ list_owner_client_ids(owner)`，不在域内 403。目标侧 id 冲突自动改名（`_impN`），不覆盖目标已有。
- **管理端页面** `/ops/browser-proxy`（`BrowserProxy.tsx` + `@admin-port/api/browserProxy.ts`）：选浏览器 → Profile 表（激活/编辑/删除/还原直连）→ 编辑器（五型表单）→ 两步导入对话框。仅管理端可见（`adminOnly`）。
- 顺带修：popup `hidden` 属性被 `display:flex` 覆盖致空框常显 → 加 `[hidden]{display:none!important}`；`_pool()` async/await 误配。
- 落点新增：`mcp_builtin/browser_proxy.py`（sync/import/owner 函数）、`sse_gateway.py`（`proxy_sync`/`proxy_sources_request` 帧 + `push_proxy_sync_profiles`）、扩展 `proxy.js`（墓碑 + sync + sources 等待器 + 命令）、`popup.js`（两步导入向导）。测试 `tests/test_browser_proxy_sync.py`（10 例，内存 fake 池）。相关回归 172 passed + 真实扩展同步 E2E 三项 PASS（sync 上行 / sources / import_apply）。

> Phase 1 交付后就已经覆盖你原始三条里的两条（改代理 + 用环境代理），且不碰任何现有链路。数据面（需求 1）放最后是因为它依赖前两者的状态机先稳。

## 9. 验收

- 扩展：Profile 编译单测（五种 condition → PAC 文本、正则注入防护）、还原路径（租约到期/断连/手动 三入口都 `settings.clear`）。
- 服务端：`bind_proxy` 不影响 `ws_client` 的会话槽（回归「对话照常 + 代理独立连」双 socket 用例）；`apply_clients` 吊销同时断两槽；`resolve_proxy_fields` 拒 `browser` 自用。
- 数据面：`browser` 模式与 `node` 模式跑同一份 `OutboundResponse` 契约测试（status/headers/iter_chunks 语义一致）。
- 手工：Clash Verge 在 TUN / 规则模式下切 Profile，桥接 WS 不掉线。

## 10. 待拍板

1. **Profile 真相源**：✅ 已定——新建 PG 表 `browser_proxy_profiles`（按 `client_id` 域）。
2. **`auto` 型是否必须走 PAC**：✅ 已定——闸门 A 通过，**auto 型走 PAC**，五种条件全保留。
3. **SwitchyOmega 备份兼容读**：✅ 已定——要做（备份格式与我们的模型不是一一对应，只做"能读多少算多少"，不承诺完整映射）。
4. ~~**是否需要用户确认**~~：已删。Agent 无权改出网路径，不存在"静默改用户出网路径"的场景。

## 参考

- [Chrome Extensions: proxy API](https://developer.chrome.com/docs/extensions/reference/api/proxy)
- [ZeroOmega (SwitchyOmega 3)](https://github.com/FelisCatus/ZeroOmega)
- [SwitchyOmega wiki](https://github.com/FelisCatus/SwitchyOmega/wiki)

## 11. Phase 0 实测记录（2026-10-03）

环境：Chrome 153.0.8010.36（临时 profile，与日常浏览器隔离），Clash Verge 7890 在监听（出口 IP `172.96.160.168`），直连外网被墙。实测脚本在 `%TEMP%/pac-gate/`（临时验证用，不入库）。

实测方法：最小 MV3 扩展（`permissions: ["proxy","storage"]`）+ CDP `Extensions.loadUnpacked` 加载（Chrome 137+ 移除了 `--load-extension` 命令行开关，headless 也不加载扩展，必须 headful + `loadUnpacked`）。

### 闸门 A：`pac_script{data}` 内联 PAC —— ✅ 通过

| 步骤 | 结果 |
|---|---|
| `settings.set({mode:'pac_script', pacScript:{data}})` | OK |
| `settings.get` 回读 | `mode=pac_script, hasPac=true, levelOfControl=controlled_by_this_extension` |
| `fetch('https://api.ipify.org')`（PAC 指定 `PROXY 127.0.0.1:7890`） | 200，IP=`172.96.160.168`（= Clash 出口 IP，证明流量真的被路由） |
| `settings.clear` | OK |

**结论**：auto 型走 PAC，五种条件全保留。

### 闸门 B：50MB 回传吞吐（JSON+base64 vs 二进制帧）—— ✅ 通过

扩展 SW 经 WS 回传 50MB（64KB/块），同机回环：

| 路径 | 服务端实测 | 扩展侧自报 |
|---|---|---|
| JSON 文本帧 + base64 | **20.6 MB/s**（50MB / 2.43s） | 23 MB/s（其中 base64 编码占 ~1.0s/2.17s，约一半时间） |
| WS 二进制帧 | **101.8 MB/s**（50MB / 0.49s） | 108.1 MB/s |

**结论**：约 5 倍差距。Phase 3 数据面的 `proxy_chunk` 用二进制帧（元数据仍走文本帧）；Phase 1 出版面不涉及大数据流，不受影响。

### 闸门 C：bypass 语义与回环 —— ✅ 通过

- 本机：无 TUN 网卡（Clash 没开 TUN），系统代理关闭（`ProxyEnable=0`）→ `fixed_servers → 127.0.0.1:7890` 是干净的本地 HTTP 代理，**无回环**。
- **意外发现**：Chrome 对 loopback 目标（127.0.0.1/localhost）**永不走代理**——假代理 9500 命中 0 次、4 个场景全 DIRECT。桥接在 127.0.0.1 时天然免疫自环；桥接是远程域名时必须显式进 `bypassList`。
- 外网域名判别（`api.ipify.org`，直连被墙）：PAC 全走代理 → `172.96.160.168`；PAC 对目标域 `DIRECT` → `Failed to fetch`（真没走代理）；`fixed_servers` 无 bypass → 走代理；`bypassList` 含目标域 → `Failed to fetch`。**`bypassList` 与 PAC `DIRECT` 均实测生效，防自环机制可用。**
