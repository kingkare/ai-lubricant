# Anisette 服务器：清单、切换与自部署

> 适用：Apple ID 免费签名（签名配置 → Apple ID 登录）。登录/2FA/续签的每次
> Apple 请求都要带 anisette 设备指纹；指纹来源就是「anisette 服务器」。
> 配套：`docs/ai-testing-platform/`（签名引擎规格）、`user_platform/apple_signing/`。

## 1. 一句话原理

Apple 的 GSA 登录接口要求请求头里带一台「已向 Apple 注册过的设备」的指纹
（`X-Apple-I-MD` 等字段）。我们的服务器不是 Apple 设备，所以要么：

- **远程 anisette 服务器**：向一台真实/provisioned 设备服务取指纹（推荐）；
- **本地 anisette 库**：进程内虚拟 provisioning（Windows 上指纹被 Apple 判假设备 503 拒收，仅兜底）。

服务器 URL 在 **签名配置弹框 → Anisette 服务器**（下拉）选择/填写，
后端字段 `anisette_server`，协议是 **anisette v3**（`remote_anisette.py`）。

## 2. 公共服务器清单

| 服务器 | 协议 | 说明 |
|---|---|---|
| `ani.sidestore.io` | v3 | SideStore 官方公共实例，默认值。**指纹全球共享** |

**为什么公共实例会连累你**：Apple 对「同一台设备指纹」短时间登录大量不同
Apple ID 触发风控（`-80045: This action cannot be completed at this time`）。
公共池被全社区共用，你登录时撞上的是**别人累积的风控额度**——账号本身没问题。

**结论**：偶发 -80045 → 等几分钟重试；频繁 -80045 → 自部署（§4），指纹独享。

## 3. 切换方式

签名配置弹框（资源页 → iOS 设备 → 签名配置 → 新建/重新登录）里的
**Anisette 服务器**下拉：

- 选预设（公共）；
- 选 **自定义…** 填自部署地址（`http://<IP>:6969`）；
- 下拉自动记忆：填了不在清单里的值时显示自定义输入框。

留空 = 回退本地 anisette 库（不推荐，Windows 虚拟指纹被 Apple 503）。

## 4. 自部署（推荐，指纹独享）

标准实现 [Dadoum/anisette-v3-server](https://github.com/Dadoum/anisette-v3-server)
（D 语言，支持 anisette v1+v3，兼容 AltServer-Linux）：

```bash
docker run -d --restart always --name anisette-v3 \
  -p 6969:6969 \
  --volume anisette-v3_data:/home/Alcoholic/.config/anisette-v3/lib/ \
  dadoum/anisette-v3-server
```

- 端口 `6969`（HTTP）；数据卷持久化 provisioning 状态（**重启不重新 provision**，
  设备身份稳定——稳定身份正是 Apple 想看到的）。
- 不需要 Apple ID、不需要 Mac；首次启动自动完成设备 provisioning。
- 部署后把 `http://<服务器IP>:6969` 填进签名配置弹框（选「自定义…」）。

旧实现 `SideStore/omnisette-server`（Rust）已被作者声明被上面这个取代，不推荐新装。

**部署位置建议**：跟主服务同机或同内网即可（我们的 `remote_anisette.py` 走
直连、不读代理环境变量）。内网部署注意防火墙放通 6969。

## 5. 代码侧事实（排查时用）

| 事实 | 位置 |
|---|---|
| 服务器 URL 由前端弹框传入，每次登录/2FA/SMS 请求都带 | `routes_ios.py` `anisette_server` 字段 |
| v3 协议：provisioning 状态（`adi_pb`）缓存在 `anisette_v3_state.plist`，**不会**每次重新 provision | `apple_signing/remote_anisette.py` |
| 本地库兜底缓存 `anisette.bin`，同样只 provision 一次 | `apple_signing/anisette.py` |
| GSA 密码登录**只**在登录/2FA 两个 HTTP 端点发生，无后台自动登录 | `routes_ios.py` begin_login / complete_2fa |
| -80045 从 Apple 响应 `Status.ec` 原样透出 | `apple_signing/gsa.py:_check` |
| 前端候选清单 | `resources.tsx` `ANISSETTE_SERVERS` |

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `-80045 This action cannot be completed at this time` | Apple 风控限流（公共指纹被拖累或短时反复登录）。停 10 分钟+，换自部署服务器重试 |
| `anisette 服务器取头失败（重试 3 次）` | 服务器 5xx/超时。换备用源或稍后重试（`remote_anisette.get_headers` 已内置短重试） |
| `-80009 MID is invalid` | anisette 头字段不完整/被代理改写。确认 `X-Mme-Device-Id`/`X-Apple-I-MD-LU` 用本地 keychain 派生值（代码已强制） |
| 本地库报「无法下载 provisioning 库」 | `anisette.dl.mikealmel.ooo` 不可达。直接用远程 anisette 服务器，不走本地库 |
