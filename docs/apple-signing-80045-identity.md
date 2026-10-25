# Apple ID 登录 -80045 的真因与修复

> 适用：Apple ID 免费签名（`user_platform/apple_signing/`）。配套
> `docs/anisette-servers.md`、`tools/probe_gsa_identity.py`。

## 一句话结论

**-80045 不是公共 anisette 服务器共享限流**（此前误判，已纠正），而是我们代码里
`X-Mme-Client-Info` 硬编码成了 **Mac+Xcode 身份**——自 2026-10-12 起 Apple 对
这种身份一律第一个请求就回 -80045。**改成 Windows 身份即解**。

## 根因（iPASide CHANGELOG 实锤 + 本仓实测）

iPASide v1.2.x CHANGELOG 记载（`github.com/pwnapplehat/iPASide`，本仓签名引擎
移植自它）：

> 自 2026-10-12 起，Apple 的 sign-in 服务器对**每个账号、每个网络**的第一个请求
> 就回 -80045。原因：anisette 数据由 Apple 的 **Android 库**生成，但请求里
> `X-Mme-Client-Info` 报的却是 **Mac+Xcode 身份**——身份对不上，Apple 拒。
> AltStore / SideStore / iloader 一起中招。iPASide 改成对**所有 Apple 服务**都
> 发 Windows 身份后恢复。

本仓实测（`tools/probe_gsa_identity.py`，用不存在的账号打 `gsa.apple.com`
的 SRP init，与 iPASide 同款安全验证法）：

| X-Mme-Client-Info | 结果 |
|---|---|
| `<Mac15,7> <macOS;27.0;26A5378j> <... akd/1.0>`（**原代码**） | ❌ -80045 |
| `<Windows> <Windows;10.0;19045> <... akd/1.0>` | ✅ -20970「账号信息有误」= 身份被接受 |

## 修复

`user_platform/apple_signing/gsa.py` 定义常量 `CLIENT_INFO_WINDOWS`（Windows
10 build 19045），三处身份（gsa / developer / remote_anisette）统一引用它，
对所有 Apple 请求（gsService / mid provisioning / 2FA / developerservices2）
发同一个 Windows 身份。akd token（`com.apple.akd/1.0`）保持不变——被 Apple
edge drop 的是 `com.apple.dt.Xcode` token，本来就没用。

重启 main 即生效，不用重新打包节点。

## 已排除的旧误判

- ~~公共 anisette 服务器 `ani.sidestore.io` 指纹共享被限流~~：实测同一套
  anisette 头，只换身份串，-80045 立刻消失。跟服务器无关。
- ~~后台有进程自动登录~~：GSA 密码登录只在 `routes_ios.py` 两个 HTTP 端点。
- ~~每次请求重新 anisette provisioning~~：有稳定缓存（`anisette.bin` /
  `anisette_v3_state.plist`）。

## 工具

`tools/probe_gsa_identity.py` —— 身份探针。Apple 若再改规则，改它重测即可：
对每个候选 `X-Mme-Client-Info` 打一次 SRP init，-80045 = 该身份仍被拒，
其它错误码 = 身份被接受。
