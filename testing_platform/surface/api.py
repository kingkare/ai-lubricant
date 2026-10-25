"""能力面 · HTTP 路由骨架（design §17「接口契约」）。

**只建 APIRouter，不挂载**——本文件不 import main.py，也不注册进现有启动链路；
后续集成方在组装 FastAPI 应用时 ``app.include_router(surface.router)`` 即可。

路由:

* ``POST /api/v1/testing/methods/{method_name}`` —— 统一方法调用入口
* ``GET  /api/v1/testing/tools`` —— 列 MCP 工具定义（= 方法注册表投影）

**鉴权留占位**：``require_surface_principal`` 依赖目前放行所有请求并返回一个
占位主体；接入正式鉴权时替换该依赖（与平台既有鉴权中间件对齐），业务路由不动。

错误映射:

* 方法未注册 → ``404 method_not_found``
* 入参非法 / 业务校验失败 → ``422 invalid_params | invalid_request``
* 其余域错误 → 502/500，错误信封统一 ``{error: {code, message, details}}``

本模块不依赖 mcp 三方库，仅复用 :mod:`testing_platform.surface.mcp` 的工具定义投影。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from .methods import (
    MethodError,
    MethodNotFoundError,
    MethodParamError,
    default_method_registry,
    to_jsonable,
)
from .mcp import list_tools

#: 能力面路由；集成方决定挂在哪个 prefix 之下（本骨架自带 /api/v1/testing）。
router = APIRouter(prefix="/api/v1/testing", tags=["testing-surface"])


# ── 鉴权占位（design 十四「共用基础设施」——正式接入时替换本依赖）──
class SurfacePrincipal:
    """占位鉴权主体。后续替换为真实用户/角色解析（如 JWT 或会话）。"""

    def __init__(self, user_id: str = "surface-anonymous", roles: tuple[str, ...] = ("test",)) -> None:
        self.user_id = user_id
        self.roles = roles


async def require_surface_principal() -> SurfacePrincipal:
    """鉴权依赖（占位）：当前恒放行，返回匿名主体。

    TODO(surface-auth): 接入平台既有鉴权后，从请求上下文解析真实主体；
    需“写”类方法时检查角色，需“执行”类方法时做运行配额校验。
    """
    return SurfacePrincipal()


PrincipalDep = Annotated[SurfacePrincipal, Depends(require_surface_principal)]


# ── 路由 ────────────────────────────────────────────────────────
@router.post("/methods/{method_name}")
async def call_method_http(
    method_name: str,
    _principal: PrincipalDep,
    payload: dict = Body(default={}),
) -> dict:
    """统一方法调用入口：``{"params": {...}}`` → 方法返回值 JSON。"""
    body = payload or {}
    params = body.get("params", body) if isinstance(body, dict) else {}
    registry = default_method_registry()

    if not registry.has(method_name):
        raise HTTPException(status_code=404, detail={"code": "method_not_found", "message": f"方法未注册：{method_name!r}"})

    try:
        raw = await registry.invoke(method_name, dict(params or {}))
    except MethodParamError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None
    except MethodNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from None
    except MethodError as exc:
        # 业务门禁/引擎错误 → 请求本身可理解，用 422；未知异常归 500
        raise HTTPException(status_code=422, detail=exc.to_dict()) from None

    return {"ok": True, "method": method_name, "result": to_jsonable(raw)}


@router.get("/tools")
async def list_tools_http(_principal: PrincipalDep) -> dict:
    """列出 MCP 工具定义（name/description/inputSchema），供外部 agent 发现能力。"""
    return {"tools": list_tools()}


# ── 子路由（MCP server + 对话后端 + 源同步触发）──────────────────
# 三个子路由带**相对前缀**（``/mcp`` / ``/chat`` / ``/source``），include 进本
# router 后落在 ``/api/v1/testing/{mcp,chat,source}`` ——main.py 无需改动
# （它已 include 本 router）。import 容错：任一子模块缺依赖（如 dsh SDK 未装）
# 不影响其它路由与主服务。
try:
    from .mcp_server import router as _mcp_server_router

    router.include_router(_mcp_server_router)
except Exception:  # noqa: BLE001 — 子路由挂载失败只记日志，不阻断能力面
    import logging as _logging

    _logging.getLogger(__name__).exception("[testing-surface] MCP server router skipped")

try:
    from .chat import router as _chat_router

    router.include_router(_chat_router)
except Exception:  # noqa: BLE001
    import logging as _logging

    _logging.getLogger(__name__).exception("[testing-surface] chat router skipped")

try:
    from .source_sync import router as _source_sync_router

    router.include_router(_source_sync_router)
except Exception:  # noqa: BLE001
    import logging as _logging

    _logging.getLogger(__name__).exception("[testing-surface] source-sync router skipped")

# 需求文档导入（multipart）：.docx → 富文本正文，.xmind → 思维导图树。
# 能力面首次收文件——单独子路由（相对前缀 /requirement），模式同上。
try:
    from .requirement_import import router as _requirement_import_router

    router.include_router(_requirement_import_router)
except Exception:  # noqa: BLE001
    import logging as _logging

    _logging.getLogger(__name__).exception("[testing-surface] requirement-import router skipped")


__all__ = [
    "SurfacePrincipal",
    "call_method_http",
    "list_tools_http",
    "require_surface_principal",
    "router",
]
