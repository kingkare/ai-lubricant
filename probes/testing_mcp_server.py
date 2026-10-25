"""独立跑测试能力面的最小 ASGI 应用（含 MCP server + chat 路由）。

用途：给 dsh 的 mcp-client 一个真实 HTTP MCP 端点做端到端验证，
**不碰用户正在跑的主服务（8001）**。

    .venv/Scripts/python.exe probes/testing_mcp_server.py --port 8011
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402

from testing_platform.surface.api import router as testing_router  # noqa: E402

app = FastAPI(title="testing-surface-only")
app.include_router(testing_router)


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
