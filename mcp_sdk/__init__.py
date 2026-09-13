"""MCP SDK：把内置 MCP 服务包成「像调本地方法一样」的客户端。

用法与 MCP 现有约定完全一致——底层就是 ``agent.mcp_client.MCPManager`` 的
``_sse_call`` → ``_gateway_rpc`` → 进程内直调 ``mcp_runtime.sse_gateway._handle_rpc``。
鉴权、token→client_id 解析、CDP tab 租约 ContextVar 注入都在网关/插件侧原样发生，
SDK 只负责把 HTTP 味的 ``tools/call`` 信封折成方法调用。
"""
from .cdp import CdpClient

__all__ = ["CdpClient"]
