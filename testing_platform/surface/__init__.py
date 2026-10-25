"""AI 测试平台 · 能力面（design §17「对外唯一出口」）。

四个面里本包先落三个（AI 工作流 ``workflows/`` 待七角色模块后再接）：

* **工具方法** :mod:`.methods` —— 原子能力注册表（已实现的模块才注册）
* **MCP**      :mod:`.mcp`      —— 方法的 MCP 投影（纯 dict，不依赖 mcp 三方库）
* **接口**     :mod:`.api`      —— HTTP 路由骨架（**只建 APIRouter，不挂载**，
                                  集成方负责 ``app.include_router(surface.router)``）

agent 可插拔（design §6.2）：平台不绑定任何具体 agent，任何会说 MCP 的 agent
通过 :func:`.mcp.call_tool` 即可驱动测试域。

用法::

    from testing_platform.surface import call_method, list_tools
    tools = list_tools()                              # 能力发现
    out   = await call_tool("tool.list", {})          # MCP 语义（恒返回信封）
    val   = await call_method("script.validate", {...})  # 直调，异常抛穿
"""
from __future__ import annotations

from . import api, mcp, methods
from . import gen_methods  # noqa: F401 — 门①②/进度方法（testpoint.confirm/proposal.list/genrun.get）
from . import taskflow_methods  # noqa: F401 — 任务层 + 评论域（taskrun.* / comment.*，26 号文）
from .mcp import PROTOCOL_VERSION, call_tool, list_tools, server_info
from .methods import (
    METHOD_KINDS,
    Method,
    MethodError,
    MethodNotFoundError,
    MethodParamError,
    MethodRegistry,
    call_method,
    default_method_registry,
    get_method,
    has_method,
    list_methods,
    register_method,
    to_jsonable,
    validate_params,
)

__all__ = [
    "METHOD_KINDS",
    "PROTOCOL_VERSION",
    "Method",
    "MethodError",
    "MethodNotFoundError",
    "MethodParamError",
    "MethodRegistry",
    "api",
    "call_method",
    "call_tool",
    "default_method_registry",
    "get_method",
    "has_method",
    "list_methods",
    "list_tools",
    "mcp",
    "methods",
    "register_method",
    "server_info",
    "to_jsonable",
    "validate_params",
]

__version__ = "0.1.0"
