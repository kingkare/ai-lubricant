# 设备端 Proxy 远程控制 iOS —— 操作手册（照着做）

> 配套规格 `ios-device-proxy-remote-control.md`、结论页 `ios-device-proxy-verification-result.md`。
> 本文是**给执行的人一步一步照着做**的：从「代码在仓库里」到「手机弹出信任框、屏幕上出现点击」。
> 全程两个角色：**你**（在电脑前操作网页 + 手机）和**服务器/harness**（后台进程）。

---

## 0. 一句话原理（先看懂再动手）

手机装一个 app，它**主动拨号**连到你的服务器，然后变成一根**网线**：服务器发的字节
→ 转发给手机**本机**的 `remoted`/RSD 端口（58783），回包原路返回。服务器上跑 go-ios，
对它而言手机就像「网络上的一台设备」。

这样绕开了 USB、同网段、NAT 三个限制。**关键假设**：app 能不能连上手机自己的
`remoted` 端口（V2）、以及能不能让手机弹「信任此电脑」（V4）——这两个过不了，方案就死。

---

## 1. 需要准备什么

| 角色 | 要求 |
|---|---|
| **手机** | iPhone，iOS 18.7（本验证目标）。系统语言随意。 |
| **服务器** | 跑着 Ai Lubricant 的机器（本地 `127.0.0.1:8001` 或公网都行）。这台机器**不要求是 Mac**。 |
| **Mac 节点** | 一台装了 Xcode 的 Mac，注册成平台的执行节点/ iOS 宿主节点。**只用来编译 app**（编完就可以不管）。 |
| **签名材料** | 一套能签 iOS app 的证书：免费 Apple ID（平台全自动）或自备 p12+描述文件。见 §4。 |

> ⚠️ **现在没有 Mac 节点在线**。本手册的 §3（构建）必须先有一台 Mac 节点注册并在线。
> 在此之前，§1–§2 可以先看，§3 起会卡住。

---

## 2. 服务器侧：把 harness 跑起来（V1–V3 用）

harness 是服务器侧那个「和 app 对话、并驱动 go-ios」的程序，源码在
`ios-device-proxy/harness/`。

**在服务器上**（Linux/Mac/Windows 都行）：

```sh
cd ios-device-proxy/harness
go build -o harness .
./harness -listen :9999
```

看到这行就绪：

```
[V1] waiting for the app to dial out on :9999
```

**这行命令就是「服务器侧 listener」**。它现在等手机来连。先别关，转去装 app。

> 端口 `9999` 可改，但手机那边要填同一个。服务器有防火墙/安全组的话，放通这个 TCP 端口。

---

## 3. 构建 app（用平台，需要 Mac 节点）

app 源码已经放进仓库（`ios-device-proxy/`），走平台的**构建 tab**编出 `.ipa`。

> ⚠️ **前置：先把 `ios-device-proxy/` 提交并推到 recipe 指向的仓**。recipe 默认从
> GitHub codeload 拉 `main` 分支 tarball（`wuxin-gh/ai-lubricant`，公开可下）；推到
> 别的仓/分支的话，改 `user_platform/routes_build.py::DEVICE_PROXY_RECIPE` 的
> `source_url`（或调 API 时用 `source_url` 覆盖）。**没推送就构建会报
> 「project root not found」**——tarball 里没有这个目录。

1. 打开网页控制台 → 进任意项目 → 左侧「**构建**」页
   （URL 形如 `/coding/project/<项目ID>/build`）。
2. **配方** 选：**「设备代理验证 app（iOS 远程控制 spike）」**。
3. **构建节点** 选那台 Mac 节点。
   - 下拉里只有**装好 Xcode + iOS 平台组件**的 Mac 节点可选。缺的会标注「缺 Xcode」或
     「缺 iOS 平台组件」；后者在那台 Mac 上跑一次 `xcodebuild -downloadPlatform iOS`，
     再去节点详情点「刷新标签」。
4. 点「**开始构建**」，等进度到 100%（首次编译较慢，超时上限 30 分钟）。
5. 完成后点「**下载 .ipa**」，得到一个 `device-proxy-0.1.0-ios.ipa`（**未签名**）。

> 构建只编译打包，**绝不签名**。签名在下一步单独做（和平台现有的 WDA runner 同款流程）。

**如果构建报错**：`step_failed` 且日志提到 `xcodebuild`，多半是 pbxproj 问题——
这台 app 的 Xcode 工程是手写的，没在真 Xcode 打开过。上机第一次建议先在 Mac 节点
的**节点终端**（节点详情页有终端入口）里跑一次：

```sh
xcodebuild -list -project ios-device-proxy/DeviceProxy.xcodeproj
```

能列出 scheme 就没事；报错就 `cd ios-device-proxy && xcodegen generate` 重建工程再构建。

---

## 4. 给 app 签名 + 装到手机

app 是**另一个 app**（不是 WDA runner），平台的「初始化」按钮是给 WDA 用的，**不适用**。
有两个办法，选一个：

### 办法 A：用平台的 Apple ID 免费签名引擎（推荐，全自动）

平台已经有一套「免费 Apple ID 自动签名」引擎（`script/auto_apple_sign.py` +
控制台的「签名配置」）。它产出 `apple_identity.p12` + `apple_profile.mobileprovision`。

1. 在**签名配置**里建一个 `apple_id` 类型配置（填 Apple ID，走 2FA），或用现成的。
   - 入口：控制台「资源」页的签名配置区（与 WDA 用的是同一套）。
2. 拿到 p12 + mobileprovision 后，用 go-ios 的 CLI 签名并安装。
   **在 Mac 节点上**（有终端），把构建出的 `device-proxy-0.1.0-ios.ipa` 传上去，然后：

   ```sh
   # 重签（改 bundle id 成你 profile 覆盖的那个 App ID）
   ios sign app \
     --path device-proxy-0.1.0-ios.ipa \
     --p12file apple_identity.p12 \
     --profile apple_profile.mobileprovision \
     --bundleid com.ailubricant.deviceproxy \
     --output device-proxy-signed.ipa \
     --install
   ```

   `--install` 会直接推给手机（走 zipconduit，和 Xcode 装 app 同一条路）。

   > 若节点上没有 `ios` 这个 CLI（`go-ios` 的二进制），先在 Mac 上装：
   > `brew install go-ios` 或从 go-ios release 下 `ios` 二进制。它和本项目用的是
   > 同一个 go-ios v1.3.2。

### 办法 B：手动用 Apple ID 脚本（没有平台签名配置时）

在 Mac 上跑仓库里的脚本，按提示输入 Apple ID / 密码 / 2FA / **手机 UDID** / bundle id：

```sh
python3 script/auto_apple_sign.py
```

产物是 `./apple_identity.p12` 和 `./apple_profile.mobileprovision`，然后同上用
`ios sign app ... --install` 装上。

> 免费证书 **7 天过期**，重装重签即可；平台配置了自动续签，手动脚本则要自己重跑。

### 装完确认

手机上应出现一个图标为默认样式的 app，名字「设备代理」。

---

## 5. 跑验证：V1–V3（服务器 + app 对话）

1. 服务器上的 `./harness -listen :9999` 还在跑。
2. 手机上打开 app，填：
   - **服务器地址**：填服务器 IP（手机能连到的那个；同网段填局域网 IP，公网填公网 IP/域名）
   - **服务器端口**：`9999`
3. 点「**连接**」。

**看服务器 harness 的日志**，逐级判断：

| 看到 | 含义 | 判定 |
|---|---|---|
| `[V1] app connected from …` | 手机拨号成功 | ✅ V1 过 |
| `[V2] relay established → device <ip>:58783` | app 连上了**手机本机**的 RSD 端口 | ✅ **关键①** |
| `[V2] relay negotiation FAILED: … (errno=N)` | app 连不上本机端口，`errno` 是**核心产出** | ❌ **假设证伪** |
| `[V3] RSD handshake OK: udid=… services=N` | RSD 协议跑通，拿到服务列表 | ✅ **关键②** |
| `[V3] RSD handshake FAILED` | 通了但协议没对上（多半端口选错，不是沙箱） | ⚠️ 可修 |

**V2 失败就是终点**。把 errno 记下来（`1`=EPERM、`13`=EACCES、`61`=ECONNREFUSED），
填进结论页，方案终止。

> app 日志区也会打印每一步，手机上能对照看。连局域网地址时手机会弹「查找并连接本地
> 网络上的设备」，**必须点允许**，否则流量被静默丢弃。

### 5.1 扫描模式（一条安装、一次连接，探完所有地址 × 真实端口）

不想「一个组合重装一次 app」时用 `-scan`：它**接受一条 app 连接后一直留着**，依次把
每个候选地址交给 app 去连，连上了就在同一条管道上跑**真实的 RSD 握手**，最后打一张
判定表。**失败的探测不会掉线**（老版本 app「失败即断开」也兼容：harness 会自动等它
重拨）。端口**不写死**——优先用 app 机内 mDNS 发现的 `_remoted._tcp` 端口。

```sh
./harness -scan -listen :9999                 # 探 app 上报的所有地址 + 127.0.0.1
./harness -scan -listen :9999 -host 10.31.0.195   # 只探你选定的那一个地址
./harness -scan -listen :9999 -rsd-port 58783     # 端口已知时直接指定，不等 mDNS
```

关键行为：

- **端口来源优先级**：`-rsd-port`（已知）→ app 机内 mDNS 上报的 `rsd_port` → 回退 58783。
- **一条连接多个探测**：app 侧 v4 在**失败后保持连接**；只有**成功建立管道**才消耗连接，
  之后 app 自动重拨，harness 重新 accept 继续探。
- `-host` 就是「**探所有 IP，让你挨个选**」里的「选」：不填=全探，填=只探那一个。

### 想顺便探一下 V4 面

服务器 harness 加 `-probe-pair` 再跑：

```sh
./harness -listen :9999 -probe-pair
```

V3 之后它会再开一条到 `untrusted.tunnelservice` 的连接并做 RSD checkin，打印
`[V4] tunnel service RSD checkin ACCEPTED`。**这只证明「配对入口够得着」**，
真正的「信任此电脑」弹框在下一步。

---

## 6. 跑验证：V4–V7（真正的配对 + 隧道 + 点击）

> ⚠️ **这一段需要一个前置改动，见 §6.0。** 直接照原样跑会失败。

### 6.0 为什么 V4–V7 不能直接跑（先读）

go-ios 的两个现实约束（读 v1.3.2 源码确认）：

1. **CLI 的 `--address`/`--rsd-port` 仍要求设备出现在 usbmuxd 列表里**
   （`ios/utils.go:GetDeviceWithAddress` 先调 `ListDevices()`）。服务器上没有插 USB 的
   iPhone，所以 `ios --address ::1 --rsd-port 58888 …` 会在 `Device not found` 直接退出。
2. **真正的配对逻辑是私有的**：`tunnelService.ManualPair()`、`verifyPair()`、SRP/TLV
   全在 go-ios 的 `ios/tunnel` 包里，**都是未导出函数**（约 1200 行）。而且
   `ManualPairAndConnectToTunnel` 第一步走的是 **mDNS**（`FindDeviceInterfaceAddress`），
   正是我们要绕开的东西。

**结论**：V4–V7 要么给 go-ios 打一个小补丁（把配对入口改成接受一条现成的连接），
要么把那 ~1200 行配对协议移植进 harness。两条路都可行，但都**不是本轮交付物**。

### 6.1 补丁方案（推荐，约 20 行）

`ManualPairAndConnectToTunnel` 的内部步骤里，**只有第一步（mDNS 找地址）需要局域网**；
第 2–4 步（RSD 取隧道服务端口 → 连它 → `ManualPair()`）在 relay 管道上完全能做——
这正是 harness 已经打通的路径。所以：

- 给 go-ios 加一个导出入口，例如
  `tunnel.ManualPairAndConnectToTunnelAtAddr(ctx, addr, rsdPort, pairRecordMgr)`，
  把 mDNS 那步换成「用调用方给的地址」。
- 然后 harness 用 `-bridge-listen` 暴露的本地端口作为 `addr:rsdPort` 传进去。

打完补丁后：

```sh
# 服务器上（harness 同机）
./harness -listen :9999 -bridge-listen 127.0.0.1:58888 -bridge-target-port 58783
# 另开一个终端，用补丁后的 go-ios：
ios-patched --address ::1 --rsd-port 58888 <配对/隧道命令>
```

### 6.2 移植方案（自包含，约 1200 行）

把 go-ios `ios/tunnel` 里的配对协议（`untrusted.go` 的 `ManualPair`/`verifyPair`/
`setupSessionKey`/`exchangeDeviceInfo`、`srp.go`、`tlv.go`、`codec.go`、`framing.go`、
`pairrecord.go`）移植进 harness，直接对着 relay 管道跑。**好处**：不依赖 fork，
harness 一个二进制全搞定。**代价**：代码量大，且**没有真机时无法验证**。

### 6.3 逐级判定（无论走哪条路）

| # | 动作 | 期望 | 判定 |
|---|---|---|---|
| V4 | 触发一次配对（走上面补丁/移植后的入口） | **手机弹「信任此电脑」** | ✅ **关键③** |
| V5 | 点「信任」后隧道建立 | 拿到隧道地址 + RSD 端口 | ✅ **关键④** |
| V6 | 经隧道连 `testmanagerd` / 拉起 DeviceKit | `authorized=true` | ✅ **关键⑤** |
| V7 | 下发一次 `device.io.tap` | **手机屏幕响应** | ✅ **终判** |

> V4 不弹信任框 = 配对流在代理下不被接受 → **基本判死**（规格 §6）。
> V4–V7 是**唯一需要真机、且尚未实现**的部分；如实记录即可。

### 6.4 现在就能拿到的 V4 半信号

即使还没打补丁，`-probe-pair` 也能给出一个**部分结论**：它证明
「untrusted.tunnelservice 通过 relay 够得着，且 RSD checkin 被接受」——
这正是 `ManualPair` 的前置条件。若这一步就失败，V4 不必再试。

```sh
./harness -listen :9999 -probe-pair
# 期待：[V4] tunnel service RSD checkin ACCEPTED — the pairing surface is reachable
```

---

## 7. 把结果填进结论页

打开 `docs/ios-device-proxy-verification-result.md`，把 V1–V7 的实测输出逐行填进表格，
然后回答页首的「假设 H：成立 / 证伪」。**在真机跑完前不要下结论。**

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| harness 起不来，`bind: address already in use` | 9999 被占，换 `-listen :其它端口`，手机填同一个 |
| **手机点连接没反应（实测踩过）** | ① 服务器防火墙没放通 9999；② **Windows 上按程序放行的规则记的是 exe 的旧路径** —— 重新 `go build` 换了 exe 位置后规则就失配了，入站被默认阻止。改用**按端口放行**的规则（与路径无关）：`New-NetFirewallRule -DisplayName 'ios-device-proxy-harness-9999' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9999 -Profile Private,Public`；③ 手机填的 IP 连不到服务器 |
| 手机填哪个服务器地址 | 填**手机能路由到的 PC 地址**。App 上报的地址列表里，`127.x`/`172.20.10.x`（热点网关）/公网蜂窝地址都不行；要选和服务器同网段的那个（实测 PC=10.31.2.46/16、手机=10.31.0.195，互 ping 通） |
| V2 报 errno=61 | app 连不上本机 58783——**端口不要写死**：iOS 的 `remoted` 端口是 mDNS `_remoted._tcp` 广播出来的（go-ios 的 `discover.go` 就是靠它），实测只在**手机自己**的网段可见，PC 侧看不见 → 所以端口发现必须在 app 内做，App 通过 `{"rsd_port":N}` 回传 |
| App 没上报 rsd_port | 机内 mDNS 没解析：检查**本地网络权限**是否允许（首次 browse 会弹框，必须点允许），以及设备是否真在广播 `_remoted._tcp` |
| V2 报 errno=1/13 | 沙箱/权限拦——**核心结论**，如实记录 |
| 构建报缺「iOS 平台组件」 | 在 Mac 上 `xcodebuild -downloadPlatform iOS`，再点节点详情「刷新标签」 |
| 构建 step_failed 提到 xcodebuild | pbxproj 有问题，`cd ios-device-proxy && xcodegen generate` 重建 |
| `ios: command not found` | Mac 上装 go-ios CLI：`brew install go-ios` |
| 签名后装不上（profile 不匹配） | bundle id 要用你描述文件覆盖的那个 App ID；免费签名必须是**你自己创建**的 App ID |

---

## 附：角色与产物一览

| 产物 | 在哪 | 谁用 |
|---|---|---|
| app 源码 | `ios-device-proxy/DeviceProxy/` | Mac 节点编译 |
| 服务器 harness | `ios-device-proxy/harness/`（`go build`） | 服务器上跑，V1–V7 |
| 构建配方 | 平台「构建」页 → 设备代理验证 app | 出 unsigned ipa |
| 签名/安装 | `ios sign app … --install`（Mac 节点终端） | 装到手机 |
| 结论页 | `docs/ios-device-proxy-verification-result.md` | 记录 V1–V7 结果 |
