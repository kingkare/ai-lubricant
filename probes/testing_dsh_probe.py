"""dsh 运行时探针：验证 agent_runtime 能驱动 dsh（连平台网关 + MCP 注入）。

用法（主 .venv）：
    .venv/Scripts/python.exe probes/testing_dsh_probe.py [--no-mcp] [--mcp-url URL]

密钥从 ``D:/pip-tmp/gw.key`` 读取，**不打印**（只回显末 4 位做核对）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testing_platform import agent_runtime  # noqa: E402


def _load_key() -> str:
    key = Path("D:/pip-tmp/gw.key").read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("gw.key 为空")
    return key


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mcp", action="store_true", help="不注入 testing MCP（只验推理链路）")
    ap.add_argument("--mcp-url", default=agent_runtime.DEFAULT_MCP_URL)
    ap.add_argument("--prompt", default="用一句话回答：1+1 等于几？")
    ap.add_argument("--dsh-home", default="D:/pip-tmp/dsh-home")
    args = ap.parse_args()

    key = _load_key()
    print(f"[probe] key: ...{key[-4:]} (len={len(key)})")
    print(f"[probe] base_url={agent_runtime.DEFAULT_BASE_URL} model={agent_runtime.DEFAULT_MODEL}")
    print(f"[probe] dsh_home={args.dsh_home}")

    os.environ.setdefault("TESTING_DSH_HOME", args.dsh_home)
    os.environ.setdefault("TESTING_DSH_CWD", "D:/pip-tmp/dsh-workspace")
    Path("D:/pip-tmp/dsh-workspace").mkdir(parents=True, exist_ok=True)

    cfg = agent_runtime.build_config(api_key=key, mcp_url=args.mcp_url)
    if args.no_mcp:
        cfg.patches = ()
    print(f"[probe] patches={cfg.patches}")

    events: list[dict] = []

    def on_event(ev: dict) -> None:
        events.append(ev)

    try:
        result = await agent_runtime.run_chat(
            prompt=args.prompt, config=cfg, on_event=None
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] {type(exc).__name__}: {exc}")
        return 1

    evs = result.get("events") or []
    print(f"[probe] 事件数={len(evs)}  事件类型={sorted({e.get('type','') for e in evs})[:12]}")
    print(f"[probe] finish_reason={result.get('finish_reason')}")
    print(f"[probe] session_id={result.get('session_id')}")
    resp = (result.get("final_response") or "").strip()
    print(f"[probe] final_response: {resp[:300] if resp else '(空)'}")
    return 0 if resp else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
