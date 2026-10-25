# 凭证金库（Credential Vault）实施计划

Whitepaper: [`../whitepapers/credential-security.md`](../whitepapers/credential-security.md)
Master plan item: `docs/project-system/master-plan.md` 中的「凭证安全（CDP 密码金库）」

## 背景

2026-08-30 的 CDP 账号安全审查发现：浏览器插件抓到的页面内容（含 Chrome autofill 释放的密码）
会经 `browser_scan` 进入 LLM prompt 并明文落 ClickHouse（`mcp_builtin/cdp_bridge/simphtml.py:24`
无条件拷贝 INPUT value，无 `type==='password'` 判断）；SOP（`sop/tmwebdriver_sop.md:116-120`）
主动教模型破解 autofill 保护；`mask_credentials` 只覆盖外部 API 端点，agent 路径
（`agent/llm_bridge.py:415` 直调 `dispatch_entry`）完全绕过遮蔽；且遮蔽规则本身只匹配云厂商
API Key 前缀，对无特征的用户密码无效。

本计划的目标是把「AI 参与密码读取」这条链路整体拆除：AI 全程只见 UUID 引用，
密码明文不出浏览器，服务端与 WS 通道全程只有密文。

## 已确认需求（多轮设计收敛结果）

1. **ask_user 是唯一密码录入/更新入口。** `ask_user` 新增 `secret_request` 参数
   （`{site, username?, update_for?}`）。AI 在登录被拒或需要凭证时发起询问；前端识别该标记，
   渲染密码输入框；用户输入后前端走**旁路 REST 接口**直接加密存储——密码绝不进入
   `reply-question` 应答对象、不进聊天消息流、不落 agent 上下文。回答给 AI 的只有
   `{"entry_id": "<uuid>", "status": "saved"}`。
2. **entry_id 用 UUIDv4，不用自增数字 id。** 数字 id 可被遍历（`{{password:17}}` 试到 18），
   UUID 让猜引用无意义。
3. **不新增前端管理页面。** vault 目录（`{entry_id, site, username}`）在登录类场景经
   系统提示注入；目录中没有的 site，AI 调 `ask_user(secret_request)` 向用户要。
   「AI 不知道密码的情况下选择一个密码」即由此覆盖。
4. **失效更新与录入同入口。** 密码不能用了 → `ask_user(secret_request={site, username,
   update_for: entry_id})` → 同一个密码框更新同一行（新密文覆盖旧密文）→ AI 拿回新引用继续用。
5. **CDP 执行时插件侧替换，服务端零改动。** `browser_execute_js` 脚本原样下发（与正常调用
   完全一致）。插件 background.js 在 `Runtime.evaluate` 前对 code 做一次
   `/\{\{password:([0-9a-f-]{36})\}\}/g` 匹配：命中 → 沿现有已认证 WS 发 `vault_fetch(entry_id)`
   → 服务端只回**密文 blob** → 插件本地解密 → 替换进 code → 执行。未命中原样执行，零行为变化。
6. **密封方案：插件客户端公钥密封（服务端不可解）。** 插件首启生成密钥对（WebCrypto，
   私钥 non-extractable，存扩展 IndexedDB），公钥 JWK 随 WS 握手上报。管理端/前端录入时用
   目标客户端公钥加密，服务端从录入那一刻起只见密文。代价已确认接受：浏览器重装/换机 =
   相关 seal 全部失效，需重新录入（服务端不留可解备份，留备份即破坏前提）。
7. **回程清洗（防 return 值泄漏）。** 插件把 `Runtime.evaluate` 返回值中出现的明文替换回
   `{{password:<uuid>}}` 占位符后再回传服务端。AI 读到的永远是引用。
8. **同批配套（缺一则金库形同虚设）：**
   - `simphtml.py:24` 加 `type === 'password'` 守卫（+ name/id/autocomplete 含 pwd 启发式遮蔽），
     堵住 scan 回读；
   - `sop/tmwebdriver_sop.md` 删 autofill 破解段（「一键释放与登录」），改写为占位符用法；
   - 每次解封/替换记 security_events 审计：只记 entry_id 与请求方，不记内容。

## 已知边界（诚实声明，写入白皮书）

- **填入 DOM 后的读取防不住**：有任意 JS 执行权就有 DOM 读权（`input.value`）。金库消灭的是
  被动泄漏（scan 回读、日志、落库、DB 拖库），不改变主动读取的暴露面。
- **变换外传防不住**：AI 写 `return btoa({{password:...}})` 这类编码后外传，串匹配洗不回。
  v2 可加插件侧危险模式检测（base64/fetch 外发/localStorage 写入特征 + 密钥引用 → 拒绝执行）。
- **主密钥/私钥失守 = 金库失守**：对称加密的宿命。缓解：私钥 non-extractable 且不出浏览器。

## 交付件清单

| # | 件 | 位置 | 说明 |
|---|---|---|---|
| 1 | vault 表 + store | `server/db.py` 新表 `vault_entries`（entry_id UUID PK, site, username, label, enabled, owner 预留）+ `vault_seals`（entry_id, client_id, blob）+ `server/vault_store.py` | 无读取明文接口；cdp_clients 增公钥列 |
| 2 | ask_user 扩展 | `agent/tools.py` `_ASK_USER_SCHEMA` 加 `secret_request`；`_ask_user` 透传标记 | AI 发起语义：录入 / 更新（update_for） |
| 3 | 密码框组件 | `user-frontend/src/components/console/task/message-ask-user-question.tsx` 加 secret_request 分支（type=password） | 前端唯一 UI 改动；提交走旁路 REST，`reply-question` 只回 UUID |
| 4 | 旁路 REST 路由 | server 新路由（HTTPS） | 前端拉目标客户端公钥 → 浏览器内加密 → POST 密文 |
| 5 | WS `vault_fetch` + 目录注入 | `mcp_runtime/sse_gateway.py`；`agent/agent_main.py` `_ensure_resources` | 扩展请求 → 校验 client 绑定 → 只回密文；登录场景系统提示注入目录 |
| 6 | 插件解密+替换+回洗 | `mcp_builtin/cdp_bridge/tmwd_cdp_bridge/background.js` | 正则匹配 → vault_fetch → WebCrypto 解密 → 替换 → 执行 → 结果反向替换 |
| 7 | simphtml 守卫 | `mcp_builtin/cdp_bridge/simphtml.py:24` | `type==='password'` 不进 value 集合 |
| 8 | SOP 重写 | `mcp_builtin/cdp_bridge/sop/tmwebdriver_sop.md:116-120` | 删破解教法，写占位符用法与 ask_user 录入流程 |

## 已落地配套（插件侧密码管理器）

与上面这条密封金库路线**并存但独立**：插件侧密码管理器（`server/browser_password_store.py` +
`browser_passwords` 表，服务端可解密存储，2026-09-19 落地）只取本计划的 #7/#8 作为**强制配套**，
因为该功能会主动把密码填进 DOM，scan 回读必须堵死。密封金库（`{{password:uuid}}`）本身仍未实施。

| 原 # | 件 | 落地位置 | 实际做法 |
|---|---|---|---|
| #7 | simphtml 密码守卫 | `mcp_builtin/cdp_bridge/simphtml.py` | 新增 `isSecretField()`（`type=password` 或 name/id/autocomplete/aria-label/placeholder 命中 `/(pass|pwd|密码|secret|token)/i`）；value 不进输出，改标 `data-secret="true"`；token 摘要链加 `secret` 标记 |
| #8 | SOP 重写 | `mcp_builtin/cdp_bridge/sop/tmwebdriver_sop.md` | 删「autofill获取与登录」全段（含「一键释放与登录」），改写为「登录与密码（插件密码管理器）」：禁止读密码值 / 禁止代填 / 需要登录时 `ask_user` 请用户自行完成 |
| 附 | 审计 | `server/browser_password_store.py` | 保存/删除写 `security_events`（`tag="browser_password"`，detail 仅 owner/entry_id/domain，**不含账号与密码**） |
| 附 | 管理页面 | `user_platform/routes_browser_passwords.py` + `user-frontend/src/pages/console/user/resources.tsx` | `/devices?kind=cdp` 顶部「密码管理」按钮（用户级全局入口，不绑客户端）：列表只回 meta、明文查看需二次鉴权（`verify_password`）、每次查看写 `browser_password_viewed` 审计 |

差异说明：原 #8 的「占位符用法」依赖 #1–#6 的 vault_fetch 链路（未实施），本功能的对应表述改为
「插件密码管理器 + ask_user 请用户自行登录」。

## 实施步骤

- [ ] #1 vault 表结构 + vault_store（含 cdp_clients 公钥列、迁移）
- [ ] #2 ask_user `secret_request` 参数（schema + 前端透传标记）
- [ ] #3 密码框组件（旁路提交，回答只含 UUID）
- [ ] #4 旁路 REST（公钥下发 + 密文上传 + 更新覆盖）
- [ ] #5 WS vault_fetch（client 绑定校验）+ 目录系统提示注入
- [ ] #6 插件：密钥对生命周期（首启生成、握手上报公钥）→ 解密替换 → 回洗
- [x] #7 simphtml password 守卫（已落地为 #7a：isSecretField + data-secret）
- [x] #8 SOP 重写 + security_events 审计（已落地为 #8a：登录与密码章节 + browser_passwords 审计）
- [ ] 测试：替换正则、回洗、UUID 不可枚举（越权 fetch 返回无此 entry）、
      simphtml 守卫（password 字段不出现在 scan 输出）、审计落库

## 验证方式

1. `browser_execute_js("return {{password:<uuid>}}")` → AI 收到的 tool result 中该位置
   仍是占位符，不是明文。
2. `browser_scan` 输出不含任何 `type=password` 字段的 value。
3. DB 直查 `vault_entries`/`vault_seals`：只有密文；无任何端点能返回明文。
4. WS 抓帧：vault_fetch 往返均为密文。
5. 密码失效场景：ask_user(update_for) 后新引用生效，旧密文被覆盖。
