"""MCP Streamable HTTP server for the testing capability surface.

Exposes the testing methods as real MCP tools so any MCP-speaking agent
(dsh's ``@deepseek-ai/dsh-mcp-client``) can call them:
``mcp__testing__scenario.create`` / ``mcp__testing__run.execute`` / …

Transport: **Streamable HTTP** (MCP 2025-03-26 spec, stateless mode) —
``POST /api/v1/testing/mcp`` with a JSON-RPC body, JSON-RPC response as
``application/json``. dsh's mcp-client config::

    config:
      serverName: testing
      transport: streamable-http
      url: http://127.0.0.1:8001/api/v1/testing/mcp

Protocol surface (stateless — no session bookkeeping needed since every POST
carries a complete request):

* ``initialize``           → server info + capabilities
* ``notifications/initialized`` (client notification) → 202 Accepted
* ``tools/list``           → ``{tools: [...]}`` (from :func:`surface.mcp.list_tools`)
* ``tools/call``           → ``{content, isError, structuredContent}`` (from :func:`surface.mcp.call_tool`)
* ``ping``                 → ``{}``

Errors follow JSON-RPC 2.0 (``-32601`` unknown method, ``-32700`` parse error);
tool-level failures stay inside the result envelope (``isError: true``), per
MCP semantics — the model sees them as tool output, not a transport error.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import mcp as surface_mcp

logger = logging.getLogger(__name__)

#: 子路由：相对前缀 ``/mcp``，由 :mod:`surface.api` 的 ``router``（前缀
#: ``/api/v1/testing``）include —— 最终挂在 ``/api/v1/testing/mcp``。
router = APIRouter(prefix="/mcp", tags=["testing-mcp"])

#: JSON-RPC error codes we actually emit
_ERR_PARSE = -32700
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_REQUEST = -32600

#: instructions 里的方法数**动态取数**——曾硬编码「107」过期成 184+，发给 agent
#: 的自我介绍不能撒谎。import 时注册表已就绪（纯装饰器扫描）。
_METHOD_COUNT = len(surface_mcp.list_tools())

_SERVER_INFO = {
    "name": "testing-platform-mcp",
    "version": "0.1.0",
    "protocolVersion": surface_mcp.PROTOCOL_VERSION,
    "capabilities": {"tools": {"listChanged": False}},
    "instructions": (
        f"AI 测试平台能力面：{_METHOD_COUNT} 个工具方法（knowledge.*/case.*/scenario.*/run.*/"
        "bug.*/project.*/source.*/release.*/workflow.*…）。"
        "工具名即方法名（点号保留）。所有写类方法都会过平台门禁"
        "（确认门/变更门/校验门），失败会以 isError 返回原因。"
    ),
}


def _rpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _handle_rpc(payload: dict[str, Any]) -> JSONResponse:
    """Dispatch one JSON-RPC request/notification to the surface registry."""
    method = payload.get("method") or ""
    req_id = payload.get("id")
    params = payload.get("params") or {}

    # 客户端通知（无 id）→ 202，无响应体
    if req_id is None:
        return JSONResponse(status_code=202, content={})

    if method == "initialize":
        return JSONResponse(_rpc_result(req_id, {
            "protocolVersion": surface_mcp.PROTOCOL_VERSION,
            "serverInfo": _SERVER_INFO,
            "capabilities": {"tools": {"listChanged": False}},
        }))

    if method == "tools/list":
        return JSONResponse(_rpc_result(req_id, {"tools": surface_mcp.list_tools()}))

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        # call_tool 恒返回信封（不抛穿），工具级失败 = isError 结果
        envelope = await surface_mcp.call_tool(name, arguments)
        return JSONResponse(_rpc_result(req_id, envelope))

    if method == "ping":
        return JSONResponse(_rpc_result(req_id, {}))

    return JSONResponse(_rpc_error(req_id, _ERR_METHOD_NOT_FOUND, f"未知方法：{method}"))


@router.post("")
async def mcp_post(request: Request) -> JSONResponse:
    """Streamable HTTP 入口：一个 POST = 一个 JSON-RPC 消息。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(_rpc_error(None, _ERR_PARSE, "请求体不是合法 JSON"), status_code=400)

    if isinstance(payload, list):
        # batch（少见，MCP 允许）→ 逐条处理，只收有 id 的响应
        results = []
        for item in payload:
            if isinstance(item, dict):
                resp = await _handle_rpc(item)
                if resp.status_code == 200:
                    results.append(json.loads(resp.body))
        return JSONResponse(results)

    if not isinstance(payload, dict):
        return JSONResponse(_rpc_error(None, _ERR_INVALID_REQUEST, "请求体必须是 JSON-RPC 对象"), status_code=400)

    return await _handle_rpc(payload)


@router.get("")
async def mcp_get() -> dict[str, Any]:
    """自述端点（非 MCP 协议要求；便于探活/调试）。"""
    registry = surface_mcp.default_method_registry()
    return {
        "server": _SERVER_INFO,
        "transport": "streamable-http (stateless)",
        "endpoint": "/api/v1/testing/mcp (POST JSON-RPC)",
        "tool_count": len(registry.names()),
    }


__all__ = ["router"]
