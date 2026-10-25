#!/usr/bin/env python3
"""一键重编 DeviceProxy spike app：Mac xcodebuild → 拉回 ipa → 签名 → USB 安装。

替代「编译然后来来回回拉扯」：改完 Swift，跑一次本脚本即可。

链路（每一步都可单跑，见 tools/ios_spike_*）：
  1. tools/mac_exec.py upload  把 ios-device-proxy/ 源码推到 Mac 的 ~/spike/DeviceProxy
  2. tools/mac_exec.py exec    xcodebuild archive + export unsigned ipa
  3. 下载 ipa 回 PC（HostFileDownload 走 base64 分块）
  4. tools/sign_device_proxy.py 复用平台签名配置签名（profile id=2）
  5. go-ios `ios install` 装到 USB 手机

用法：python tools/rebuild_device_proxy.py [--only-build|--only-install] [--yes]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPIKE = REPO / "ios-device-proxy"
OUT = REPO / "spike-work"
MAC_ROOT_REL = os.environ.get("SPIKE_MAC_ROOT", "spike")   # under the Mac's $HOME
BUNDLE = os.environ.get("SPIKE_BUNDLE", "com.ailubricant.deviceproxy")
GO_IOS = Path(r"C:\Users\Administrator\Downloads\go-ios-win\ios.exe")
BASE = os.environ.get("AGENT_COMPOSE_BASE_URL", "http://127.0.0.1:8003")
TOKEN = os.environ.get("NODE_CONTROL_TOKEN", "tok")
MAC_NODE_ID = os.environ.get("MAC_NODE_ID", "node-3405c7e3-57bb-4db8-9951-6287da6572a6")
CHUNK = 1 << 20

SRC_FILES = [
    "DeviceProxy/AppDelegate.swift",
    "DeviceProxy/ViewController.swift",
    "DeviceProxy/TCPProxy.swift",
    "DeviceProxy/Info.plist",
    "DeviceProxy.xcodeproj/project.pbxproj",
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _post(method: str, payload: dict, timeout: float, *, retries: int = 8) -> dict:
    """POST to the node control plane, retrying the transient "node is offline"
    503s this Mac node emits while it flaps its connection."""
    import urllib.error

    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            f"{BASE}/agentcompose.v2.NodeService/{method}",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            if e.code == 503 and "offline" in body:
                last = f"503 node offline"
                log(f"    node offline, retry {attempt}/{retries} in 10s …")
                time.sleep(10)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = str(e)
            log(f"    network error ({e}), retry {attempt}/{retries} in 10s …")
            time.sleep(10)
    raise RuntimeError(f"{method}: node unavailable after {retries} retries ({last})")


def mac_exec(command: str, timeout_ms: int = 600000) -> str:
    log(f"mac$ {command}")
    d = _post("HostExec", {"nodeId": MAC_NODE_ID, "command": command,
                           "timeoutMs": timeout_ms, "maxOutputBytes": 200000},
              timeout=timeout_ms / 1000 + 60)
    out = (d.get("stdout") or "") + (d.get("stderr") or "")
    if not d.get("success"):
        raise RuntimeError(f"mac exec failed (exit={d.get('exitCode')}):\n{out[-3000:]}")
    if out.strip():
        print(out.strip()[-4000:], flush=True)
    return out


def upload(local: Path, remote: str) -> None:
    data = local.read_bytes()
    total, sha, upload_id, offset = len(data), __import__("hashlib").sha256(data).hexdigest(), __import__("uuid").uuid4().hex, 0
    while True:
        chunk = data[offset:offset + CHUNK]
        final = offset + len(chunk) >= total
        d = _post("HostFileUpload", {"nodeId": MAC_NODE_ID, "uploadId": upload_id, "path": remote,
                                     "offset": offset, "data": base64.b64encode(chunk).decode("ascii"),
                                     "totalSize": total, "sha256": sha if final else "",
                                     "overwrite": True, "final": final}, timeout=300)
        if not d.get("ok"):
            raise RuntimeError(f"upload {local.name} chunk@{offset} failed: {json.dumps(d)[:500]}")
        offset += int(d.get("bytesWritten") or len(chunk))
        if final:
            return


def download(remote: str, local: Path) -> None:
    """Pull a file back from the Mac. There is no HostFileDownload RPC — the
    spike has only HostExec/HostFileUpload — so base64 the file over HostExec
    (an .ipa is a few hundred KB; one shot with a raised output cap)."""
    d = _post("HostExec", {
        "nodeId": MAC_NODE_ID,
        "command": f"base64 < {remote}",
        "timeoutMs": 300000,
        "maxOutputBytes": 50_000_000,
    }, timeout=360)
    if not d.get("success"):
        raise RuntimeError(f"base64 {remote} failed: {(d.get('stderr') or '')[:500]}")
    raw = (d.get("stdout") or "").strip()
    data = base64.b64decode(raw)
    local.write_bytes(data)
    log(f"downloaded {remote} -> {local} ({len(data)} bytes)")


def mac_home() -> str:
    """Ask the Mac for its real $HOME — never expanduser() a remote path."""
    out = mac_exec("echo $HOME", timeout_ms=30000)
    home = out.strip().splitlines()[-1].strip()
    if not home.startswith("/"):
        raise RuntimeError(f"unexpected Mac $HOME: {home!r}")
    return home


def build() -> Path:
    root = f"{mac_home()}/{MAC_ROOT_REL}"
    mac_exec(f"mkdir -p {root}/DeviceProxy {root}/DeviceProxy.xcodeproj")
    for rel in SRC_FILES:
        upload(SPIKE / rel, f"{root}/{rel}")
        log(f"uploaded {rel}")
    # Build with the recipe proven in docs/ios-device-proxy-verification-result.md §2:
    # derivedDataPath (not archive) — no team/profile needed, codesign disabled.
    # NOTE: each shell statement is `;`-delimited — HostExec runs one `sh -c`, so
    # adjacent string literals MUST be separated or `cd` swallows the next command.
    mac_exec(
        "set -e; cd {root}; rm -rf build;"
        " xcodebuild -project DeviceProxy.xcodeproj -scheme DeviceProxy"
        " -configuration Release -destination 'generic/platform=iOS' -derivedDataPath build"
        " CODE_SIGN_IDENTITY='' CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO"
        " CODE_SIGN_ENTITLEMENTS= ENABLE_BITCODE=NO clean build 2>&1 | tail -35".format(root=root),
        timeout_ms=900000,
    )
    mac_exec(
        "set -e; cd {root};"
        " rm -rf Payload; mkdir -p Payload;"
        " cp -R build/Build/Products/*-iphoneos/DeviceProxy.app Payload/;"
        " rm -f DeviceProxy-unsigned.ipa;"
        " zip -qry DeviceProxy-unsigned.ipa Payload;"
        " ls -la DeviceProxy-unsigned.ipa".format(root=root),
        timeout_ms=300000,
    )
    local_ipa = OUT / "unsigned4.ipa"
    download(f"{root}/DeviceProxy-unsigned.ipa", local_ipa)
    return local_ipa


def install() -> None:
    signed = OUT / "device-proxy-signed.ipa"
    if not signed.exists():
        raise SystemExit(f"missing {signed} — run without --only-install first")
    log("installing on the USB iPhone …")
    proc = subprocess.run([str(GO_IOS), "install", "--path", str(signed)],
                          capture_output=True, text=True, timeout=300)
    print(proc.stdout.strip()[-1500:])
    if proc.stderr.strip():
        print("stderr:", proc.stderr.strip()[-1500:])
    if proc.returncode != 0:
        raise SystemExit("go-ios install failed")
    log("installed. 在手机上打开「设备代理」并保持前台。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-build", action="store_true", help="only build+download the unsigned ipa")
    ap.add_argument("--only-install", action="store_true", help="only sign+install")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if not args.only_install:
        unsigned = build()
        # 符号链复用：把 build 产出的 ipa 接给签名脚本的输入名
        target = OUT / "unsigned.ipa"
        target.write_bytes(unsigned.read_bytes())
        log(f"unsigned ipa ready: {target}")
        if args.only_build:
            return
        log("== signing with the platform's apple-id profile ==")
        env = dict(os.environ)
        subprocess.run([sys.executable, str(REPO / "tools" / "sign_device_proxy.py")],
                       check=True, cwd=str(REPO), env=env)
    install()


if __name__ == "__main__":
    main()
