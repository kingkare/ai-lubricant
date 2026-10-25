# 设备端 Proxy 远程控制 iOS —— 验证结论（一页）

> **状态**：✅ 真机验证完成（2026-10-19）。本文是
> `docs/ios-device-proxy-remote-control.md` 的第 9.3 号产出物：**一页结论**。
> **照着做的操作手册**：[`ios-device-proxy-howto.md`](./ios-device-proxy-howto.md)。

## 0. 结论

| 项 | 结果 |
|---|---|
| **假设 H**（手机装个 app 当网线，服务器上的 go-ios 就能远程控制这台手机） | ❌ **证伪** |
| 判定依据 | **官方 go-ios 用自己的网络模式连手机，握手超时**（`--address fe80::…%17 --rsd-port 32498 rsd ls` → `dial timed out after 15s`）。手机在局域网上只广播 `_apple-mobdev2._tcp`（配对入口），**没有广播 `_remoted._tcp`（RSD 服务）**——没有任何网络入口可拨。 |
| 一句话 | 手机的 remoted/RSD 当前不通过网络暴露：官方 CLI 连不上、app 中继更连不上。 |

**关键点：这条证伪与 app 的代码质量无关。** 上面那条命令里根本没有 app 参与，
是 go-ios 官方 CLI（PC 上的原生程序，无 iOS 沙箱）自己拨的 —— 同样超时。
所以问题不在"app 写错没"，而在"手机没开这条网络路径"。

**实测证据链**（2026-10-19 深夜，PC `10.31.2.46` / `172.20.10.6` ↔ iPhone）：

| # | 验的东西 | 结果 |
|---|---|---|
| A | PC ↔ 手机是否同一二层、mDNS 能否看到广播 | ✅ 通（手机在接口 14/17 都广播） |
| B | 手机广播了哪些服务 | 只有 `_apple-mobdev2._tcp` → `iPhone.local:32498`（带 `supportsRP-24`）；**`_remoted._tcp` 为空** |
| C | 拨手机广播端口 `32498`（PC 直连，不经 app） | v4 → **REFUSED**（RST）；v6 链路本地错误 scope → TIMEOUT |
| D | **官方 go-ios 走网络模式做 RSD 握手** | ❌ **`dial timed out after 15s`** ← 决定性 |

> 注：早先（17:43–18:01）用 app 拨 `62078` 全地址 EPERM、拨 `58783`/`50529`
> refused/timeout 的记录，当时被解读成"沙箱墙 + 架构墙"。**该解读已作废** ——
> 那些端口本就是猜的/非 RSD，不能作为结论依据。最终判定以本节的 B/D 两项为准：
> 官方工具、正确地址，依然连不上。

## 1. 实测记录（2026-10-19，真机 iPhone，iOS 18.7，USB 在线）

设备 UDID `00008120-001175642EE0A01E`；服务器 `10.31.2.46:9999`；app 从
`10.31.0.195` 拨入。RSD 真端口由 `ios tunnel ls` 实时取得（**每次会话都变**：
17:24 是 `fd5e:6643:528a::1:50526`，17:36 变成 `fd7b:dc8b:db15::1:50529`）。

| # | 步骤 | 期望 | 判定 | 实测输出 |
|---|---|---|---|---|
| V1 | app 拨号连服务器 | 服务器 accept | ✅ **通过** | `[scan] app connected from 10.31.0.195:…`，稳定复现 |
| V2 | app 连本机地址的 remoted/RSD/lockdown 端口 | 连接建立 | ❌ **失败** | app 机内 mDNS 拿不到端口（`_remoted._tcp` 未广播）；官方 CLI 走 LAN 同样 15s 超时 |
| V3 | 服务器经代理完成 RSD 握手 | 拿到服务列表 | ⛔ 未达 | 无网络入口可拨 |
| V4–V7 | pair / 隧道 / testmanagerd / tap | — | ⛔ 未达 | 前置 V2 已断 |

**V1 是唯一被证明可行的部分**：app 作为 TCP 客户端主动拨出到服务器，稳定可复现。
这也解释了为什么一路的失败都不像"app 坏了"—— app 的拨出能力没问题，问题是
**目标不存在**。

## 2. 根因：手机不把 RSD 暴露到局域网

实测（§0 证据链 B/D）显示两件事：

1. **手机上唯一在 LAN 广播的是 `_apple-mobdev2._tcp`**，端口动态（本轮 `32498`），
   带 `supportsRP-24`。它是**配对入口**，不是 RSD —— 直接 TCP 拨它得 REFUSED。
2. **`_remoted._tcp`（go-ios 的 `discover.go` 所 browse 的那个）广播为空**。
   没有广播，就没有可拨的地址端口对。

因此 go-ios 官方 CLI 用 `--address/--rsd-port` 走网络时，只能拿配对端口去当
RSD 端口试 → 15 秒拨不通。**结论与 app 无关：官方原生程序在同一条路上也一样断。**

**为什么 `tunnel ls` 里有 `fd7b::1:50529` 却用不了**：那是 **PC 侧 go-ios agent
用 USB 配对现建的 userspace 隧道端点**（`userspaceTun: true`），地址属于 PC 的
utun 接口，不是手机 en0 上的监听。手机内部或网络上的第三方都拨不到它 ——
这也解释了 §1 里 app 拨 `fd7b::1` 必超时的现象（并非"沙箱墙"）。

**go-ios 能跑通（`ios rsd ls` 列出 60+ 服务）是靠 USB**，不是靠"连手机 LAN 上某个端口"。

## 3. 可行的替代方向（不在本轮范围，仅记录）

1. **USB 优先**：go-ios 走 usbmuxd 已完全可用（本轮 `ios rsd ls` 即证据）。
   远程场景用「手机常连一台跑 go-ios 的中继机」。
2. **隧道中继**：若必须无线，让中继机与手机建 RemotePairing 隧道（go-ios
   `tunnel start`，需 wintun），服务器连中继机暴露的隧道端口——**不是**连手机 LAN。
3. **Network Extension**：app 侧用 NE 才能把隧道端点暴露成可拨端口，工程量
   远大于本 spike，且仍受隧道模型约束。

## 4. 交付物（均已落地，可复现）

| # | 产出 | 位置 | 状态 |
|---|---|---|---|
| 1 | 最小 iOS app（Swift，无 entitlement，拨出+字节管道+机内 mDNS） | `ios-device-proxy/DeviceProxy/` | ✅ |
| 2 | Xcode 工程（手写 pbxproj） | `ios-device-proxy/DeviceProxy.xcodeproj/` | ✅ 已真机编译安装 |
| 3 | 服务器 harness（Go，单会话多探针 + 真 RSD 握手） | `ios-device-proxy/harness/` | ✅ |
| 4 | 一键重编脚本（Mac xcodebuild→ipa→签名→USB 装） | `tools/rebuild_device_proxy.py` | ✅ 跑通 |
| 5 | 签名脚本（复用平台 Apple-ID profile） | `tools/sign_device_proxy.py` | ✅ |
| 6 | 操作手册 | `docs/ios-device-proxy-howto.md` | ✅ |
| 7 | 本结论页 | `docs/ios-device-proxy-verification-result.md` | ✅ |

## 5. 本轮踩过的坑（供后续复用）

- **Windows 防火墙按程序记绝对路径**：harness.exe 换路径编译后旧规则失效，
  9999 被静默拦。改用**按端口**的入站规则（`-LocalPort 9999`）。
- **隧道地址/端口每次会话都变**：`tunnel ls` 的 `address`/`rsdPort` 不是常量，
  必须**查完立刻拨**，隔几分钟就失效（拨旧地址得 ETIMEDOUT）。
- **后台 `(... &)` 会被回收**：harness 要前台 `timeout N ./harness.exe` 跑。
- **app 的 fail 行 JSON 有个错位的 `}`**：harness 侧做了 salvage（从原文抠 errno、
  保活会话），不致命。
- **`_remoted._tcp` 在手机上不广播**（本轮实测 browse 为空，只有 `_apple-mobdev2._tcp`）：
  app 机内 mDNS 报 `rsd_port=0` 是"正确地什么都没看到"，**不是** resolver bug、
  也不是"本地网络权限没给"（权限已确认允许）。曾怀疑的 2 秒 resolve 超时 bug
  已在源码修正，但它不是卡点 —— **没有广播，多长的超时都拿不到端口**。
- **别把 PC 侧隧道端点当手机端口**：`tunnel ls` 的 `address/rsdPort` 是 **PC 自己
  utun** 上的，拨它必超时，跟沙箱无关。
