# 设备端 Proxy 远程控制 iOS —— 可行性验证规格

> **状态**：待验证（2026-10-14 立）。这是**一页验证规格**，不是实施方案。目的只有一个：
> 用最小原型回答「手机上的 app 能不能当一根网线，把远程服务器的 go-ios 流量转发到
> 手机本机的 RemotePairing 服务上」。**在真机跑出结论前，任何一方都不该下结论。**
>
> **产出物已就绪**（2026-10-14）：见 §9 清单与
> [`ios-device-proxy-verification-result.md`](./ios-device-proxy-verification-result.md)
> （一页结论骨架，含 V1–V7 实测表）。
> **照着做的操作手册**：[`ios-device-proxy-howto.md`](./ios-device-proxy-howto.md)。
> 上机前先读那两份。

## 0. 一句话目标

写一个最小 iOS app，它**不控制任何东西**，只做 TCP 代理：主动拨号连远程服务器，
把服务器发来的字节转给手机**本机**的 `remoted`/RSD 端口，再把回包转回服务器。
服务器上跑 go-ios，对服务器而言手机就是「网络上的一台设备」。若成立，则
**不插 USB、不要求同局域网、可穿 NAT** 地远程控制 iPhone。

## 1. 为什么值得验证（架构动机）

现状两条路都有缺陷：
- **USB**：必须插线。
- **局域网无 USB**（go-ios mDNS + 网络隧道）：要求手机与 host 同网段；go-ios 自动隧道
  管理器还主动跳过网络设备（`tunnel_api.go:494`），且 17.4+ 的网络隧道分支未接线。
- **纯远程**：手机在 NAT 后无公网入站，mDNS 不跨网段 —— 现有机制够不到。

设备端 proxy 方案绕开以上全部：**app 主动拨出去**（能穿 NAT），服务器当 host。

## 2. 核心假设（待证伪）

> **假设 H**：一个装在 iPhone 上的普通 app，能通过 TCP 连接到**本机的 RemotePairing
> 服务**（`remoted` / RSD / `testmanagerd` 所在的端口），并在服务器发起配对时完成
> pair-verify 信任握手。

**若 H 成立** → 方案可行。
**若 H 不成立**（沙箱拦截 / 服务只接受外部主机 / 配对流无法在代理下完成）→ 方案死。

**注意**：本验证**不**假设必须走 loopback。目标端口应优先尝试手机的**局域网地址**
（`remoted` 挂在网络接口上，见 go-ios `discover.go` 里连的是 `entry.AddrIPv6`），
loopback 仅作对照。

## 3. 已知事实（来自 go-ios v1.3.2 源码，非推测）

| 事实 | 出处 |
|---|---|
| `remoted` 通过 mDNS 广播 `_remoted._tcp`，host 连设备的 **IPv6 地址**（非 localhost） | `ios/discover.go` |
| 连上后走 RSD 握手 → 取 `untrusted-tunnel` 服务端口 → `ManualPair`（设备弹「信任」） | `tunnel/tunnel.go`、`tunnel/untrusted.go` |
| 配对用 host 的 ed25519 身份（`pairRecords.selfId`）做 pair-verify | `tunnel/untrusted.go:305` |
| DeviceKit runner 由 `testmanagerd` 拉起 + 运行时 `authorize`（非自启 app） | `testmanagerd/xcuitestrunner.go:500` |
| go-ios 的 host 侧唯一 usbmuxd 入口可用 `USBMUXD_SOCKET_ADDRESS=tcp://…` 指向远程 | `ios/usbmuxconnection.go:32` |

## 4. 最小原型设计

### 4.1 app 做什么（只做这些）

1. **拨出**：连远程服务器的一个 TCP 端口（地址/端口由 app 内配置填写）。
2. **转发**：收到服务器字节 → 写进「到本机目标端口」的连接；反向同理。**逐字节透明，
   不改协议**。
3. **目标端口**：由服务器在连接建立时下发（或 app 侧固定几个候选：RSD 端口、
   `testmanagerd.remote` 端口、隧道端口）。
4. **上报**：把「连本机端口成功/失败 + 失败原因（errno）」回传服务器，**这是本验证的
   核心产出**。

### 4.2 app **不做**什么

- 不控制设备、不用无障碍、不启动 DeviceKit（那是 testmanagerd 的事）。
- 不需要 `com.apple.private.xctest` 之类私有 entitlement。
- 不碰 localhost（除非做对照）。

### 4.3 服务器侧做什么

- 一个裸 TCP listener，收到 app 连接后，用 go-ios 的 RSD/tunnel 逻辑对着这条连接说话。
- 先**手动指定**目标端口（不经 mDNS），跑通后再接 `discover.go`。

## 5. 验证步骤与判定

| # | 步骤 | 期望 | 判定 |
|---|---|---|---|
| V1 | app 拨号连服务器 | 服务器 accept | ✅ 基线，必过 |
| V2 | app 连**本机局域网地址**的 `remoted` 端口（通常是 62078 或 mDNS 通告的端口） | 连接建立 | **关键①** |
| V3 | 服务器经代理完成 RSD 握手 | 拿到服务列表 | **关键②** |
| V4 | 服务器发起 pair（首次） | **手机弹「信任此电脑」** | **关键③** |
| V5 | 用户点信任 → 隧道建立 | go-ios 拿到隧道地址+RSD 端口 | **关键④** |
| V6 | 经隧道连 `testmanagerd` → 拉起 DeviceKit | `authorized=true` + 12004 可达 | **关键⑤** |
| V7 | 下发一次 `device.io.tap` | 手机屏幕响应 | **终判** |

**只要 V2 或 V4 失败，H 即证伪，方案终止。**

## 6. 各步失败的含义（避免误判）

| 失败点 | 含义 | 是否可绕过 |
|---|---|---|
| V2 连不上本机端口 | 沙箱拦截 / 服务只听外部主机 | 试局域网地址 vs loopback；试其它端口。若全失败 → 死 |
| V3 握手失败 | 协议没转对（代理层问题） | 可修（代理是透明的，多半是端口选错） |
| V4 不弹信任 | 配对流在代理下不被接受 | 难绕过 → 基本判死 |
| V5 建隧道失败 | 隧道数据面（TLS-PSK）在代理下不通 | 可能可修 |
| V6 授权失败 | testmanagerd 不认这条来路 | 难绕过 |
| V7 点击无响应 | runner 起来了但控制链断 | 排查 |

## 7. 构建与真机（mac 节点）

- **环境**：mac 节点（支持 Xcode 编译上传）。
- **app 形态**：最小 Swift app（单个 ViewController + 一个 TCP 转发类），
  或复用 `mobile/` 工程的 iOS 原生模块思路（见 `docs/lan-discovery.md` 的
  `withLanDiscovery.js` 模式）。
- **后台存活**（本验证阶段**不管**）：iOS 会挂起后台 app。验证阶段保持 app 前台即可；
  若要产品化，再评估 Network Extension（`NEPacketTunnelProvider`，需特殊 entitlement）。
- **签名**：普通开发签名即可（本验证不需要特殊权限）。

## 8. 边界与风险

- **本地网络权限**（iOS 14+）：连局域网地址会触发「查找并连接本地网络上的设备」授权，
  首次要允许。
- **iOS 版本差异**：手机是 **18.7**。`remoted`/RSD 在 17.4+ 有行为分支
  （go-ios `tunnel_api.go:592`：`>17.4` 与 `17.0–17.3` 走不同路径），验证需覆盖 18.x。
- **配对记录**：pair-verify 用的是 host 身份。代理下，host 是**服务器**——
  设备信任的是「服务器这台 host」。这一点正是 V4 要验的。
- **本轮之前我两次口头结论（「17.4 不行」「app 不行」）都是错的/未验证的**，
  本规格以代码事实为准，不以口头结论为准。

## 9. 产出物

1. 最小 app 源码（mac 节点可编译）。
2. 服务器侧最小 listener + go-ios 对接脚本。
3. **一页结论**：H 成立 / 证伪，附 V1–V7 的实测输出。

## 10. 若 H 成立，后续（不在本验证范围）

- app 后台常驻方案（Network Extension）。
- 服务器侧把「裸 listener + 手动端口」升级为「mDNS 发现 + 自动隧道」。
- 与现有节点（`nodes/`）的集成点设计。
