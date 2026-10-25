# ios-proxy-harness — server side of the device-proxy spike

The server half of the feasibility spike in
`docs/ios-device-proxy-remote-control.md`. It speaks the app's control protocol
(see `../README.md`) and drives go-ios's Remote Service Discovery (RSD) stack
**through the relay**, answering the spec's question: can a plain iOS app act as
a network cable to a device's own RemotePairing service?

## What it does

- **Probe mode** (default) — V1–V3. Waits for the app to dial out, negotiates the
  relay to a device port, then performs the **RSD handshake over the pipe** using
  go-ios's own HTTP/2 + XPC code (`http.NewHttpConnection` →
  `ios.CreateXpcConnection`), printing the device UDID and the full RSD service
  list. With `-probe-pair` it also opens the `untrusted.tunnelservice` (the V4
  surface) and confirms the RSD checkin is accepted.
- **Bridge mode** (`-bridge-listen`) — exposes a local TCP listener where every
  connection is relayed to a device port through the app. Useful as the wire for
  a **patched** go-ios pairing entry (see "V4–V7" below); a stock `ios` CLI
  cannot use it (it still requires the device in the usbmuxd list).

The linchpin: go-ios's RSD handshake is reachable over **any**
`io.ReadWriteCloser`, so the relay pipe is a drop-in for the socket go-ios would
normally dial itself. Everything here uses go-ios's **exported** API only
(v1.3.2); nothing is patched.

## V4–V7 needs a go-ios change (not yet done)

V1–V3 (discovery + RSD handshake through the relay) run today. V4–V7 (pairing →
tunnel → `testmanagerd` → tap) do **not** run out of the box, for two reasons
confirmed in go-ios v1.3.2 source:

1. The `ios` CLI's `--address`/`--rsd-port` path still calls `ListDevices()`
   (usbmuxd) first, so with no USB device attached it exits with
   `Device not found` before ever using the address.
2. The actual pairing (`tunnelService.ManualPair`/`verifyPair`, SRP, TLV) is
   **unexported** in `ios/tunnel`, and `ManualPairAndConnectToTunnel` starts with
   mDNS discovery (`FindDeviceInterfaceAddress`) — exactly what we want to bypass.

Two ways forward (both ~a day of work, neither in this spike's scope):

- **Patch go-ios**: add an exported entry like
  `ManualPairAndConnectToTunnelAtAddr(ctx, addr, rsdPort, mgr)` that skips the
  mDNS step and takes the address the caller supplies. Then point it at the
  bridge (`--address ::1 --rsd-port <bridge>`). ~20 lines in a fork + a
  `replace` directive.
- **Port the pair protocol** (~1200 lines: `untrusted.go`, `srp.go`, `tlv.go`,
  `codec.go`, `framing.go`, `pairrecord.go`) into this harness so it runs the
  pairing against the relay pipe directly. Self-contained, no fork.

`-probe-pair` already gives the V4 pre-condition (tunnel service reachable +
RSD checkin accepted) without either change.

## Build & run

```sh
cd ios-device-proxy/harness
go build -o harness .

# Probe mode (V1–V3): listens on :9999 for the app
./harness -listen :9999
# …then on the phone, enter <server-ip>:9999 and tap 连接.

# Probe + open the tunnel service (V4 surface)
./harness -listen :9999 -probe-pair

# Bridge mode: let the stock `ios` CLI drive pairing through the relay
./harness -listen :9999 -bridge-listen 127.0.0.1:58888 -bridge-target-port 58783
# …then, with go-ios installed, point it at the bridge:
#   USBMUXD_SOCKET_ADDRESS=... # (only for the usbmux path; see below)
```

### Bridge mode and go-ios

The bridge gives a raw RSD pipe where go-ios would normally dial a device. A
**stock** `ios` CLI cannot use it directly: its `--address`/`--rsd-port` path
still requires the device in the usbmuxd list (see "V4–V7" above). The bridge is
therefore the wire for a **patched** go-ios entry (or a harness-side port of the
pair protocol) — not a drop-in for the stock CLI.

> Status: the bridge + RSD handshake are exercised by the unit tests below; the
> end-to-end V4–V7 pairing is the part that needs a real iPhone (and the go-ios
> change above) and is what the spec's V-table exists to record.

## Tests

The control protocol, relay negotiation, errno surfacing, the buffered-bytes
fix, and RSD-handshake parsing are unit-tested without a device:

```sh
go test ./...
```

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `-listen` | `:9999` | address the harness accepts the app's dial-out on |
| `-bridge-listen` | *(off)* | run as a local bridge for the `ios` CLI instead of the one-shot probe |
| `-bridge-target-port` | `58783` | device port the bridge relays to |
| `-target-port` | `58783` | device port the probe relays to (RSD) |
| `-device-ip` | *(app's first non-loopback IP)* | override `target_host` handed to the app |
| `-probe-pair` | `false` | also open the untrusted tunnel service (V4 surface) |
| `-timeout` | `30s` | per-connection I/O deadline |
