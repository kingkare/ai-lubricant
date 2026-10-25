"""能力面 · MCP 工具面（design §17.2「工具方法经 MCP 注册暴露」）。

**协议适配层，不含业务逻辑**：本模块把 :mod:`testing_platform.surface.methods`
里注册的方法投影成 MCP 工具定义，并把 ``tools/call`` 分发回方法注册表。

* **不依赖 mcp 三方库** —— 只产出 MCP 形态的 ``dict``（``name`` /
  ``description`` / ``inputSchema``），由任意会说 MCP 的 agent 侧适配器接走。
* 工具名保留方法的点号（``knowledge.query``）；MCP 规范建议 ``[a-zA-Z0-9_-]``，
  但点号在主流实现里可接受，且改名会让工具名与 HTTP 路径 / 注册表名分裂——
  保持单一真相（一处改名，处处一致）。
* **错误不抛穿**：``call_tool`` 恒返回 ``{"isError": bool, "content": [...]}``，
  失败时 ``structuredContent`` 带 ``{code, message, details}``（与 methods 的错误码同源）。

用法::

    from testing_platform.surface import mcp
    tools = mcp.list_tools()                      # MCP 工具定义列表
    out   = await mcp.call_tool("tool.list", {})  # 分发到 methods 注册表
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from .methods import (
    Method,
    MethodError,
    default_method_registry,
    to_jsonable,
)

#: MCP 协议版本（本适配层声明的版本；不做握手，仅元信息）。
PROTOCOL_VERSION = "2024-11-05"

#: 内容块的 MIME 类型（MCP text content）。
_CONTENT_TYPE = "text"


# ── 工具定义 ────────────────────────────────────────────────────
def _input_schema(method: Method) -> dict:
    """方法 ``params_schema`` → MCP ``inputSchema``。

    已是 JSON Schema 子集，只补齐 MCP 要求的两个形态字段：``type: object``
    与 ``properties``（空方法也要有合法空对象 schema）。
    """
    schema = dict(method.params_schema or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def method_to_tool(method: Method) -> dict:
    """一个 :class:`~testing_platform.surface.methods.Method` → MCP 工具定义。

    MCP 工具定义形态（``tools/list`` 的 ``tools[]`` 元素）::

        {"name": str, "description": str, "inputSchema": {...}}

    ``kind``（read/write/execute）与 ``returns_schema`` 放在 ``annotations``
    扩展位——不污染规范字段，agent 侧想用就用。
    """
    return {
        "name": method.name,
        "description": method.description or method.name,
        "inputSchema": _input_schema(method),
        "annotations": {
            "kind": method.kind,
            "returns_schema": dict(method.returns_schema or {}),
        },
    }


def list_tools(
    registry: Optional[Any] = None,
    *,
    names: Optional[Sequence[str]] = None,
) -> list[dict]:
    """列出 MCP 工具定义（缺省列进程默认注册表全部已实现方法）。

    :param registry: 方法注册表（缺省 :func:`default_method_registry`）；
        也接受任意有 ``all() -> list[Method]`` 的对象，便于测试隔离。
    :param names: 只列这些方法名（缺省全列）。
    """
    reg = registry or default_method_registry()
    methods = reg.all()
    if names is not None:
        wanted = set(names)
        methods = [m for m in methods if m.name in wanted]
    return [method_to_tool(m) for m in methods]


def tools_list_result(registry: Optional[Any] = None, *, names: Optional[Sequence[str]] = None) -> dict:
    """``tools/list`` 的完整结果信封（``{"tools": [...]}``）。"""
    return {"tools": list_tools(registry, names=names)}


# ── 调用分发 ────────────────────────────────────────────────────
def _content(payload: Any) -> list[dict]:
    """把返回值包成 MCP 文本内容块（结构化结果同时给 structuredContent）。"""
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return [{"type": _CONTENT_TYPE, "text": text}]


def error_result(error: MethodError) -> dict:
    """能力面错误 → MCP 工具调用失败信封（``isError=true``，不抛穿）。"""
    return {
        "isError": True,
        "content": _content(error.to_dict()),
        "structuredContent": error.to_dict(),
    }


async def call_tool(
    name: str,
    arguments: Optional[Mapping[str, Any]] = None,
    *,
    registry: Optional[Any] = None,
) -> dict:
    """分发 ``tools/call`` 到方法注册表；**恒返回信封，不抛异常**。

    成功::

        {"isError": False, "content": [{"type":"text","text": "<json>"}],
         "structuredContent": <方法返回值 JSON 化>}

    失败（未注册 / 参数非法 / 业务错误）::

        {"isError": True, "content": [...], "structuredContent": {"code","message","details"}}
    """
    reg = registry or default_method_registry()
    try:
        raw = await reg.invoke(name, dict(arguments or {}))
    except MethodError as exc:
        return error_result(exc)
    except BaseException as exc:  # noqa: BLE001 —— 适配层兜底，绝不让异常漏出协议面
        return error_result(MethodError(f"{type(exc).__name__}: {exc}", code="internal_error"))

    payload = to_jsonable(raw)
    return {
        "isError": False,
        "content": _content(payload),
        "structuredContent": payload,
    }


def server_info() -> dict:
    """适配层自述（便于 HTTP 侧拼 ``initialize`` 应答；不含握手逻辑）。"""
    registry = default_method_registry()
    return {
        "name": "testing-platform-surface",
        "version": "0.1.0",
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "tool_count": len(registry.names()),
    }


__all__ = [
    "PROTOCOL_VERSION",
    "call_tool",
    "error_result",
    "list_tools",
    "method_to_tool",
    "server_info",
    "tools_list_result",
]
