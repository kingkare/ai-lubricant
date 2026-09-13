#!/usr/bin/env python3
"""Initialize secrets in .env before docker compose creates containers.

Compose substitutes environment variables at container creation time. If
NODE_CONTROL_TOKEN is empty, node-server may generate and write it back later,
but the already-created ai-lubricant container still has an empty token in its
environment and the node page reports:

    未配置控制面 agent_compose_base_url/token

Run this script before `docker compose up` to make the shared secrets stable and
visible to every container from the first start.
"""
from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def _read_lines() -> list[str]:
    if not ENV.exists():
        if EXAMPLE.exists():
            ENV.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        else:
            ENV.write_text("", encoding="utf-8", newline="\n")
    return ENV.read_text(encoding="utf-8").splitlines()


def _upsert(lines: list[str], key: str, value: str) -> bool:
    prefix = key + "="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            if line[len(prefix):].strip():
                return False
            lines[i] = prefix + value
            return True
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# compose generated secrets")
    lines.append(prefix + value)
    return True


def main() -> int:
    lines = _read_lines()
    changed = []
    if _upsert(lines, "NODE_CONTROL_TOKEN", secrets.token_urlsafe(32)):
        changed.append("NODE_CONTROL_TOKEN")
    if _upsert(lines, "NODE_CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32)):
        changed.append("NODE_CREDENTIAL_ENCRYPTION_KEY")
    if _upsert(lines, "AGENT_ATTACHMENT_SIGNING_KEY", secrets.token_urlsafe(48)):
        changed.append("AGENT_ATTACHMENT_SIGNING_KEY")
    if changed:
        ENV.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print("updated .env:", ", ".join(changed))
    else:
        print(".env secrets already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
