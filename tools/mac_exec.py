"""Ad-hoc driver for the iOS device-proxy spike on the Mac node.

Uses the node control plane (AGENT_COMPOSE_BASE_URL/NODE_CONTROL_TOKEN from .env,
defaults http://127.0.0.1:8003 / tok) directly, so it works outside the app process.

Subcommands:
  exec   "<shell command>" [timeout_ms]     run on the Mac, print stdout/stderr
  upload <local-file> <remote-path>         push a whole file onto the Mac
"""
import base64
import hashlib
import json
import os
import sys
import urllib.request
import uuid

MAC_NODE_ID = os.environ.get("MAC_NODE_ID", "node-3405c7e3-57bb-4db8-9951-6287da6572a6")
BASE = os.environ.get("AGENT_COMPOSE_BASE_URL", "http://127.0.0.1:8003")
TOKEN = os.environ.get("NODE_CONTROL_TOKEN", "tok")
CHUNK = 1 << 20  # 1 MiB, matches the node's maxUploadChunkBytes


def _post(method: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{BASE}/agentcompose.v2.NodeService/{method}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def cmd_exec(command: str, timeout_ms: int = 60000) -> None:
    d = _post("HostExec", {
        "nodeId": MAC_NODE_ID, "command": command,
        "timeoutMs": timeout_ms, "maxOutputBytes": 200000,
    }, timeout=timeout_ms / 1000 + 30)
    print(f"exit={d.get('exitCode')} success={d.get('success')}")
    if d.get("stdout"):
        print(d["stdout"])
    if d.get("stderr"):
        print("--- stderr ---")
        print(d["stderr"][:4000])
    if not d.get("success"):
        print("--- raw ---")
        print(json.dumps(d, ensure_ascii=False)[:1500])


def cmd_upload(local: str, remote: str) -> None:
    data = open(local, "rb").read()
    total = len(data)
    sha = hashlib.sha256(data).hexdigest()
    upload_id = uuid.uuid4().hex
    offset = 0
    while True:
        chunk = data[offset:offset + CHUNK]
        is_final = offset + len(chunk) >= total
        d = _post("HostFileUpload", {
            "nodeId": MAC_NODE_ID,
            "uploadId": upload_id,
            "path": remote,
            "offset": offset,
            "data": base64.b64encode(chunk).decode("ascii"),
            "totalSize": total,
            "sha256": sha if is_final else "",
            "overwrite": True,
            "final": is_final,
        }, timeout=120)
        if not d.get("ok"):
            print(f"chunk@{offset} FAILED: {json.dumps(d, ensure_ascii=False)[:800]}")
            sys.exit(2)
        written = int(d.get("bytesWritten") or len(chunk))
        offset += written
        if is_final:
            print(f"uploaded {total} bytes -> {remote} (sha256 {sha[:12]}…)")
            return


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sub = sys.argv[1]
    if sub == "exec":
        cmd_exec(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 60000)
    elif sub == "upload":
        cmd_upload(sys.argv[2], sys.argv[3])
    else:
        print(f"unknown subcommand {sub!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()
