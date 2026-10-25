# device-control 能力对齐与诚实性契约

> 2026-10-19。本文件固化 device-control 子模块（`device-control/`）在**无特权**前提下，
> 相对 mobile-next/mobile-mcp 的能力边界、已修的「假成功」类缺陷、以及新增的两条
> 扩展命令。协议与实现的规范性文本在
> [`device-control/spec/protocol-v0.md`](../device-control/spec/protocol-v0.md)
> ——尤其 §8.0「诚实性契约」与 Appendix A「能力对齐与诚实降级表」；本文件只做
> 项目侧的背景、决策依据和排障指引。

## 一、连接方向不是差异，执行主体才是

设备端是**主动拨号出**连自部署服务端（WebSocket），而不是主机 adb/go-ios 驱动——
这与 mobile-mcp 的「主机连设备」在**功能上没有区别**，只是部署与 NAT 的手段选择。
真正的能力差异来自**执行主体的权限**：

| | 执行主体 | 拿到什么能力的代价 |
|---|---|---|
| mobile-mcp (Android) | `adb shell` UID | 持有 `START_ACTIVITIES_FROM_BACKGROUND` 等 shell 特权 |
| mobile-mcp (iOS) | host 上的 go-ios/WDA | 本机 USB + 开发者信任态 |
| **本项目 Android** | 手机上的**无特权侧载 App** | 零依赖常开主机，代价是放弃全部 adb 特权能力 |
| **本项目 iOS** | 宿主节点 node（USB） | 与 mobile-mcp 同源，能力基本对齐 |

结论：**不引入特权层**是自部署极简的硬边界（`device-control/PLAN.md §1`），
所以那些依赖 `adb` 的能力在 Android 上是**永久 `unsupported`**，不是「还没实现」。
完整清单见 spec Appendix A 的降级表。

## 二、修掉的「假成功」类缺陷（无特权范围内）

根因是同一类模式：**「Android/iOS API 没抛异常 = 成功」**。这条在 mock 单测里永远
测不出来，是**真机才暴露**的一批 bug。

### Android

- **`open_app` 静默失效**（用户最初报的问题）：`context.startActivity` 在 Android 10+
  后台 Activity 启动（BAL）限制下被系统**静默丢弃**——不抛异常、logcat 才有记录，
  而 handler 照样返回 `ok=true`。修法对齐上游 ARC-MCP：dispatcher 在每条命令派发期间
  挂一个 `TYPE_ACCESSIBILITY_OVERLAY` 指示窗口，让 UID 在 BAL 检查里视作前台；再加
  **前景校验 + 一次重试 + 诚实 `device_error`**（国产 ROM 的「后台弹出界面」是 AOSP
  之外的第二道闸，只有校验才能把它变成可诊断的错误而不是假成功）。
- `scroll_to_node` 丢弃 `scrollNode` 返回的 `Result`，且第 2 次起用过期树滚。
- `type_text` / `press_key` 丢弃 `commitText`/`sendKeyEvent` 的布尔返回。

### iOS

- **`do()` 不检查 WDA 错误信封**：只看 HTTP 状态码，而 WDA 失败可能是 **200 +
  `{"value":{"error":...}}`**。所有变更类命令都传 `out=nil`，直接 `return raw, nil`
  ——**一处判断修好全部 16 条命令**。
- **`node_id` 从不赋值**：`convertElement`/`adaptDKOne` 都没设 `screen.Node.ID`，
  屏幕树的 `node_id` 列**永远为空**，所有 `node_id` 寻址（tap/scroll_to_node）必然
  `stale_node`。现按 §8.2 推荐方案派生稳定哈希（与 Android 同源）。
- **points/pixels 混用**：`screen.w/h` 报 points、树 bounds 报 pixels，按 header
  坐标点必偏。
- `ErrUnsupported` 无 `errcode.From` 分支 → 被洗成匿名 `device_error`；
  `dispatcher.mapError` 又把 `device_error` 消息洗成 "command failed on device"。
- `open_app` 把**所有**错误映射成 `not_found`，不校验效果，且 `stopIfRunning=true`
  会**杀掉正在运行的 App**（Android 是 resume，两端语义不一致）。
- `scroll` 完全忽略 `node_id`；`dismiss_keyboard` 用 `context.Background()` 绕过超时。

## 三、新增的两条扩展命令（无特权，两端对齐）

| 命令 | 为什么值得做 | Android | iOS |
|---|---|---|---|
| `get_foreground_app` | Agent 判断「现在在哪个 App」是刚需；mobile-mcp 有此工具 | 数据**已在内存里**（无障碍 `TYPE_WINDOW_STATE_CHANGED` 维护的 `currentPackageName`），只差一个 handler | WDA `/wda/activeAppInfo`；DeviceKit `device.apps.foreground` |
| `open_url` | 同样是 mobile-mcp 有、我们缺的真能力；**零特权** | `ACTION_VIEW`，与 `open_app` 同一条 BAL 通道 | WDA `POST /session/{id}/url` |

两条都加了 **scheme 门禁**（默认只放 `http`/`https`，`allow_unsafe_schemes=true`
显式放开）——自定义 scheme 是攻击面，参考工具也做同样的默认拒绝。

**扩一条命令要同时改五处**（各处都有测试钉住）：
1. `device-control/spec/protocol-v0.md` §8 表 + §8.8 说明
2. `device-control/android/.../protocol/Capabilities.kt` (`ALL`)
3. `device-control/ios/internal/caps/caps.go` (`All`)
4. `mcp_builtin/device_control/protocol.py` (`COMMANDS` + `READ_ONLY_COMMANDS`)
5. `mcp_runtime/builtin_plugins/device_control_plugin.py` (`_tools()`)

前端面板（`user-frontend/src/api/builtinToolsClient.ts` 的 `DEVICE_COMMANDS` +
`device-console-dialog.tsx`）也要跟上，否则按钮不出现。

## 四、明确**不做**的能力（诚实降级）

| 能力 | Android 为何永久 `unsupported` |
|---|---|
| `terminate_app`（静默杀） | 无 `KILL_BACKGROUND_PROCESSES`，`FORCE_STOP_PACKAGES` 不在 public API 面；「设置页强制停止」是可见且依赖界面语言的脆弱路径 |
| `uninstall_app`（静默卸载） | 需 `DELETE_PACKAGES`（signature 级） |
| `device_logs` / `crash_reports` | `READ_LOGS` 是 `signature\|privileged`；tombstones/ANR 要 root |
| `set_orientation` | 全局转屏需 `WRITE_SETTINGS`；无障碍快捷设置瓷砖只能「切换开关」，不能指定方向 |
| `set_location`（静默） | mock-location appop 只能由用户在开发者选项手动选本 App |
| `clipboard get`（干净读） | Android 10+ 限制为「系统默认 IME 或持焦点的 App」读；**我们的 a11y 虚拟 IME 不是默认 IME，不享豁免** |
| `screen_recording`（常驻） | `MediaProjection` **每次会话都要用户同意**，破坏无人值守控制 |

iOS 侧这些大多**是可做的**（宿主节点有 USB 通道），但属于 `M4` 之后的批，
不是本轮范围。

## 五、排障

- **`open_app` 还是不打开**：先查 Android 端 logcat 有无
  `Background activity start ... is blocked`；再确认手机「设置→应用→本 App→权限管理→
  后台弹出界面」是否放开（华为/EMUI 与 AOSP 是两道独立闸）。
- **命令返回 `unsupported`**：设备端没声明这个能力，或**后端不支持**（如 iOS 的
  DeviceKit 后端做不了 `set_text`/`open_url`）。这是诚实降级，不是 bug。
- **能力声明漂移**：某命令在 A 平台声明、B 平台没声明是正常的（能力集是**运行时探测**，
  不是静态常量）。协议用 `capabilities-changed` 事件同步。

## 六、验证状态（务必知悉）

- iOS `go test ./...` 全绿，含 7 条新回归测试（错误信封 / node_id 生成与稳定 /
  前景校验 / URL scheme 门禁 / 单位）。
- Android `:app:testDebugUnitTest` 全绿（49 tests）。
- 服务端 pytest 58 passed。
- 前端 `vite build --mode offline` 通过。

**但这些都只是单测级**。`device-control/` 的 PLAN.md 一直标注「真机端到端验证待做」——
本仓 VM 跑不了模拟器。**真机效果仍需插一台手机/一台 iPhone 实测**：
配对 → `get_screen_state` 拿 TSV → `tap`/`open_app`/`open_url` 真机响应 →
`DELETE` 触发 4003 擦凭据。
