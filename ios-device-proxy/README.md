# DeviceProxy — iOS TCP proxy (feasibility spike)

A minimal one-page iOS app that lets a **remote server reach a port on the
iPhone's own network stack** (e.g. the device's `remoted`/RSD service on
`58783`, or `testmanagerd`) **without USB and without being on the same LAN**.

The trick: the phone **dials OUT** over TCP to our server (outbound connections
traverse NAT / carrier-grade NAT / any network), and the app then becomes a
**transparent byte pipe** between that server connection and a local port on the
device. The server drives everything over that pipe. There is **no control of
the device, no entitlements, no accessibility** — the app is a dumb byte relay.

This is a **spike**, not a product: ~200 lines of Swift, no third-party
dependencies, no SwiftPM/CocoaPods.

## Layout

```
ios-device-proxy/
  DeviceProxy.xcodeproj/          hand-written, minimal single-target project
    project.pbxproj
    xcshareddata/xcschemes/DeviceProxy.xcscheme
  DeviceProxy/
    AppDelegate.swift             programmatic UI, no storyboard
    ViewController.swift          minimal UI (host/port fields, connect toggle, log)
    TCPProxy.swift                the proxy engine (BSD sockets)
    Info.plist                    NSLocalNetworkUsageDescription + UILaunchScreen
  project.yml                     XcodeGen spec (fallback / regeneration path)
  README.md                       this file
```

## The exact control protocol (app side)

The app runs a **reconnect loop**. **Each server connection carries exactly ONE
device-side connection.**

1. Connect TCP to `serverHost:serverPort` (from the UI fields).
2. Immediately send **one newline-terminated JSON line** — the `hello`, carrying
   the device's own active IPv4 addresses (enumerated via `getifaddrs`,
   loopback excluded) so the server can pick a LAN address for `target_host`:

   ```
   {"hello":"device-proxy/1","ips":["192.168.1.23","10.0.0.7"]}\n
   ```

3. Read **one newline-terminated JSON control line** from the server:

   ```
   {"target_host":"192.168.1.23","target_port":58783}\n
   ```

4. Attempt a TCP connect to `target_host:target_port`.
   - **On success**: send `{"ok":true}\n`, then enter a **pure bidirectional
     byte pipe** (raw bytes — no framing, no parsing) until either side closes.
   - **On failure**: send
     `{"ok":false,"errno":<int>,"error":"<short msg>"}\n`, close the server
     connection, and log it. **`errno` is the real POSIX errno** recovered from
     the failed `connect()` (via `SO_ERROR`) — this is the spike's key output.
5. After the pipe closes, go back to step 1 (reconnect, after a short delay).

Direction of travel, in one place:

| Step | App emits / expects |
|------|---------------------|
| 2 | app → server: `{"hello":"device-proxy/1","ips":[...]}\n` |
| 3 | server → app: `{"target_host":"...","target_port":N}\n` |
| 4a | app → server (success): `{"ok":true}\n` then raw bytes both ways |
| 4b | app → server (failure): `{"ok":false,"errno":N,"error":"..."}\n` then close |

Notes:
- `target_host` will typically be **one of the `ips` we advertised** (a LAN
  address of the phone itself), so the server reaches the device's *own* stack
  through the pipe.
- `errno` values to expect when probing `remoted`/`testmanagerd` from an
  unprivileged app: `1` (EPERM), `13` (EACCES), or `61` (ECONNREFUSED). The
  number is the finding.

## Build

The app is packaged by our own build system (the same path as the WDA runner —
see `user_platform/routes_build.py`): a mac node clones/downloads the source and
runs `xcodebuild`, producing an **unsigned** `.ipa`. Signing happens later,
out-of-band, via the go-ios / ASC path.

Equivalent local build:

```sh
cd ios-device-proxy

xcodebuild -project DeviceProxy.xcodeproj -scheme DeviceProxy \
  -destination 'generic/platform=iOS' -derivedDataPath build \
  CODE_SIGN_IDENTITY='' CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO

mkdir -p Payload && cp -R build/Build/Products/*-iphoneos/DeviceProxy.app Payload/
zip -qry unsigned.ipa Payload
```

- `-destination 'generic/platform=iOS'` → a device build (`*-iphoneos`); a
  simulator build (`*-iphonesimulator`) will not install on a real device.
- `-derivedDataPath build` keeps the product inside the workspace so the `cp`
  can find it.
- `CODE_SIGN_*` all off — signing is a separate step, exactly like WDA.

Regenerating the project (fallback): if `project.pbxproj` is ever lost, the
checked-in `project.yml` describes the same target and settings, so
`xcodegen generate` reproduces an equivalent project.

## Verification steps (V1–V7)

The app itself only implements **V1/V2 mechanics** — dial out, forward bytes to
a local port, report the connect errno. **V3 is driven by the server harness over
the pipe today; V4–V7 need a small go-ios change first** (see
`harness/README.md` → "V4–V7 needs a go-ios change"). The app just shuttles bytes
either way.

| Step | What it verifies | Where it's driven |
|------|------------------|-------------------|
| **V1** | Dial-out: the phone connects to our server from any network (NAT traversal). | App + server. Server sees the `hello` line and the device's `ips`. |
| **V2** | Forward to the device's own `remoted`/RSD port: server asks for `{"target_host": <device-lan-ip>, "target_port": 58783}`; app connects and reports `{"ok":true}` or the real errno. | App + server. The errno is the key result. |
| **V3** | RSD handshake: the server speaks the RSD/RemotePairing protocol through the pipe and gets a valid response. | Server harness over the pipe (runs today). |
| **V4** | Device shows the **"Trust This Computer"** prompt (pairing prompt appears on-screen). | Server harness — **needs a go-ios pairing entry that accepts a caller-supplied pipe** (not yet done). |
| **V5** | Tunnel established: the server holds a working RSD tunnel to the device. | Server harness (same dependency as V4). |
| **V6** | `testmanagerd` / DeviceKit reachable through the tunnel (e.g. list installed apps / start a test session). | Server harness (same dependency). |
| **V7** | Tap works: a real UI interaction lands on the device. | Server harness (e.g. via WDA/testmanagerd; same dependency). |

So a successful spike run is: install the app → enter server host/port → tap
「连接」→ watch the log show the `hello`, the control line, the target connect
result (and errno on failure) → the server harness drives V3 over the pipe today,
and V4–V7 once the go-ios pairing entry is added.

## Info.plist / entitlements

- `NSLocalNetworkUsageDescription` is set (Chinese) so iOS prompts for local
  network access: *用于把远程服务器的流量转发到本机 iOS 服务，以远程控制本设备*.
- `UILaunchScreen` is an empty dict (required for a modern launch screen).
- **No special entitlement is required.** The app only makes outbound TCP
  connections and dials `127.0.0.1`-style local ports; it does not need the
  `com.apple.developer.networking.*` or any device-management entitlement.
