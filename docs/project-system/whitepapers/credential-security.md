# 凭证安全白皮书（CDP 密码金库）

## 背景

平台用 CDP 驱动真实浏览器登录第三方 AI 账号，浏览器插件（tmwd_cdp_bridge）把页面内容
回传给模型消费。这条链路上存在密码明文：Chrome autofill 保存的密码、用户手动输入的密码。
审查（2026-08-30）确认明文会进入 LLM prompt、tool result、ClickHouse 会话存储与服务端日志，
且既有遮蔽管线（mask_credentials）对 agent 路径不生效、规则匹配不了无特征的用户密码。

## 问题

凭证的「录入—存储—传输—使用」四段中，任何一段把明文交给 AI 可见的文本（prompt、tool
参数/结果、日志、会话落库），都构成「AI 参与密码读取」。对称加密本身不解决这个——把解密
放在 AI 的消费路径上，加密只是换了个位置泄漏。真正决定安全性的是**解密边界画在哪**。

## 原则

- AI 是凭证的**触发方**，永远不是**消费方**。AI 发起（ask_user 录入、占位符引用），
  但明文不进入任何 AI 可见文本。
- **明文不出浏览器**。解密只发生在插件（WebCrypto，私钥 non-extractable）；服务端与
  WS 通道全程只见密文。
- **不存在任何取回明文的接口**。vault 的 API 只有：录入密文、更新密文（覆盖）、
  按绑定取密文、目录（元数据）。永不回显、永不导出。
- **引用不可枚举**。entry_id 用 UUIDv4；数字 id 允许遍历引导。
- **录入走旁路**。密码经 ask_user 触发，但由前端旁路 REST 提交；聊天消息流、
  `reply-question` 应答、agent 上下文中只出现 UUID。
- **审计只记 ID 不记内容**。解封/替换事件记 security_events：谁、哪个 entry、何时。

## 决策

- 密封方案：插件客户端公钥密封（每客户端独立密钥对，服务端不可解）。
  代价：浏览器重装 = 该端 seal 全部失效需重录；服务端不留可解备份。
- ask_user 是唯一密码录入/更新入口（含失效更新，update_for 覆盖同一行）。
- CDP 执行时插件侧对 `{{password:<uuid>}}` 做正则替换；服务端选路零改动，
  与正常 browser_execute_js 完全一致。
- vault 目录经系统提示注入（登录场景），不新增前端管理页面。

## 非目标

- 不防填入 DOM 后的主动读取（任意 JS 权限下 `input.value` 可读）——金库消灭被动泄漏
  （scan 回读、日志、落库、拖库），不承诺对抗有意的模型行为。
- 不防编码变换后外传（base64/分段）。v2 可加插件侧危险模式检测，属加强项非前提。
- 不做服务端可解的「找回密码」能力——该能力与「明文不出浏览器」互斥。

## Plans

- [`../plans/2026-08-30-credential-vault.md`](../plans/2026-08-30-credential-vault.md)

## 已落地（插件侧密码管理器，2026-09-19）

上面描述的密封金库（`{{password:uuid}}`、服务端不可解）**仍是独立未实施计划**。实际先落地的是
另一条**取舍不同**的路线：**服务端可解密的密码管理器**——用户在插件里登录成功后，插件询问是否
保存，密码 AES-256-GCM 列加密后存进服务端 `browser_passwords` 表并关联登录用户，配套候选下拉
自动填充、popup 管理列表与随机密码生成。

与本文档原则的**明确偏差**（需要知情）：

- 本文档要求「明文不出浏览器、服务端不可解」；密码管理器按用户明确选择做**服务端可解**存储
  （密钥 `BROWSER_PASSWORD_KEY`），因为要支持跨设备在任意浏览器会话中取回填充。
- 因此本功能**不承诺**对抗服务端拖库：拿到密钥 + 库即得明文（列加密挡的是「只拖表」与备份泄漏）。
- 密钥轮换 = 已存密码全部无法解密，`pwd_get` 会回「记录无法解密（服务端密钥已变更）」，**绝不静默轮换**。

**已强制落地的两条配套**（与本文档原则一致，本功能因此没有扩大 AI 侧的暴露面）：

- `mcp_builtin/cdp_bridge/simphtml.py`：新增 `isSecretField()`，密码类字段的 value **不再进入
  `browser_scan` 输出**，只标 `data-secret="true"`；token 摘要链同步加 `secret` 标记。
- `mcp_builtin/cdp_bridge/sop/tmwebdriver_sop.md`：删除「autofill 获取与登录 / 一键释放与登录」，
  改写为「登录与密码（插件密码管理器）」——禁止 agent 读取密码值、禁止把密码写进脚本/参数/日志、
  禁止代替用户输入密码，需要登录时用 `ask_user` 请用户自行在浏览器完成。
  （2026-10-04 追加：该文件经查明是孤儿文档——无任何代码把它喂给 AI，管理端 SOP 编辑 tab 亦已一并下线，
  整个 `mcp_builtin/cdp_bridge/sop/` 目录删除；AI 侧浏览器 SOP 的唯一事实源是 `agent/sop/browser_sop.md`。
  强制防护在代码层：`simphtml.py` `isSecretField()` 令密码字段 value 永不出 `browser_scan`，只标 `data-secret="true"`。）

其余原则（AI 不是消费方、审计只记 ID 不记内容）在密码管理器里同样成立：WS 帧不落日志、审计
`security_events` 的 detail 只有 owner/entry_id/domain，**不含账号与密码**。

### 管理页面（2026-09-19 追加）

`/devices?kind=cdp` 顶部、「添加浏览器」旁边新增「密码管理」按钮，打开后是**当前用户**的密码列表。

**密码是用户级资源**（`browser_passwords.owner_user_id` = C 端用户 id），不挂在某个 CDP 客户端上——
同一个用户的所有浏览器客户端共享同一份密码库，所以管理弹框是全局的，没有「每个客户端一份」的语义。

这条入口刻意做了三重收敛，避免把「服务端可解」放大成「谁都能看」：

- **归属即当前用户**：路由无 `resource_id` 参数，owner 恒为 `str(user.id)`，不存在跨用户读取的路径。
- **明文查看需二次鉴权**：列表只回 meta（`id/domain/username/url/updatedAt`，**永不含密文**）；
  要看明文必须再次输入当前账号密码，`verify_password` 通过才解密，且明文 15 秒后自动收回。
- **每次查看写审计**：`security_events` 记 `browser_password_viewed`（`severity="warning"`，
  `tag="browser_password_admin"`），detail 同样只有 owner/entry_id/domain。

明文消费面因此没有变成「任意 HTTP 调用即可拿密码」：**没有二次鉴权就拿不到明文**，且每次取用都留痕。
风险仍在（服务端可解 + 网页端可查看，等于多了一条明文出口），这是用户明确选择「服务端可解存储 +
网页管理」时接受的取舍。
