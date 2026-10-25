"""Minimal MCP-over-SSE gateway for loaded custom plugins.

This implements the common MCP SSE shape:
- GET  /mcp/{service}/sse       opens an event stream and publishes the message endpoint
- POST /mcp/{service}/messages  accepts JSON-RPC requests and sends responses to that stream

Supported methods for first cut:
- initialize
- tools/list
- tools/call
- ping

For custom plugins this is enough for external MCP clients to discover and call tools.

鉴权：当服务的 PluginContext.auth_enabled 为真时，SSE 握手与每个 RPC 都校验
token（Authorization: Bearer 或 ?token=）。token 必须在该服务的 allowed_tokens
集合内，否则 401/403。校验通过的 token 被存进 session，并经 current_request_token
ContextVar 透传给插件 handler（如 cdp-bridge 的会话池隔离）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from loguru import logger

from cryptography.exceptions import InvalidTag  # noqa: E402 —— 仅密码帧用于「密钥已变更」判定

from mcp_builtin.device_control import protocol as proto
from mcp_builtin.device_control.driver import DeviceContext
from .registry import registry
from .plugin_loader import current_request_token

router = APIRouter(prefix="/mcp", tags=["mcp-runtime-sse"])

# 扩展连接空闲读超时（秒）：客户端每 ~24s 发一次 keepalive ping，
# 超过这个窗口没收到任何帧即判定为半开死连接（MV3 worker 被杀、断网、休眠
# 都不会发 TCP FIN，receive_text 会永久阻塞），主动关闭以触发 finally 里的
# unregister_client 释放 client_id 槽位，让同一浏览器的正常重连能成功。
_EXT_IDLE_TIMEOUT = 60
# 泵向 WS 发送一帧的超时：客户端标签页节流/冻结时 asyncio 写缓冲过水位会让 send
# 挂住，超时即撤泵——WS 不可靠传输，服务端不为它做送达保证。
_CHAT_PUMP_SEND_TIMEOUT_S = 5.0
# MCP Runtime 已并入主服务，CDP 网页对话的内部 HTTP 路由也挂在同一
# FastAPI app 上。固定回环主服务端口，不再支持独立 Agent 服务地址。
# 该地址仅作**回退**：正常走进程内 ASGI 直连（见 _get_asgi_app），拿不到 app
# 或进程内派发失败时才回落到回环 HTTP。
_AGENT_INTERNAL_BASE = "http://127.0.0.1:8001"

# 进程内 ASGI 直连：/agent/client/* 与 sse_gateway 同进程、同事件循环，走回环
# HTTP 要额外付一次 TCP 建连 + 每个请求新建 aiohttp ClientSession 的代价；事件
# 循环繁忙时回环 sock_connect 还会超时（日志里的
# ``ConnectionTimeoutError('Connection timeout to host http://127.0.0.1:8001/...')``），
# 把「发送受理」拖过面板的受理看门狗 → 面板误判失败，而服务端其实已在跑。
# httpx.ASGITransport 把请求直接交给同一个 app 处理，不落 socket、不排 accept
# 队列。惰性取 app（main 已在本模块挂载前 import 完成，不存在循环导入）。
# 置 CDP_CHAT_INPROC=0 可强制回退回环（排障用）。
_ASGI_APP: Any = None
_ASGI_APP_READY = False


def _get_asgi_app() -> Any:
    """惰性取主服务 app（进程内派发用）。取不到返回 None → 回退回环。"""
    global _ASGI_APP, _ASGI_APP_READY
    if _ASGI_APP_READY:
        return _ASGI_APP
    _ASGI_APP_READY = True
    try:
        import main as _main

        _ASGI_APP = getattr(_main, "app", None)
    except Exception as exc:  # noqa: BLE001 — 拿不到 app 就回退回环
        logger.debug("[cdp-chat] 取主 app 失败，回退回环：%r", exc)
        _ASGI_APP = None
    return _ASGI_APP


@dataclass
class _Session:
    queue: asyncio.Queue[str]
    service_name: str
    token: str = ""


_sessions: dict[str, _Session] = {}


def _sse(event: str, data: Any) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _extract_token(request: Request) -> str:
    """Authorization: Bearer 优先，回退 query ?token=（EventSource 无法设 header）。"""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "").strip()


# 内置工具服务名 → 该服务鉴权所需的 principal param key。
# 声明源在 mcp_builtin.catalog.BuiltinServiceSpec.required_param；本映射在模块加载时
# 从 catalog 派生，sse_gateway / 派发器 / 前端编辑器统一读它，不再各处手写。
def _load_builtin_param_key() -> dict[str, str]:
    try:
        from mcp_builtin.catalog import BUILTIN_SERVICE_SPECS
        return {spec.name: spec.required_param for spec in BUILTIN_SERVICE_SPECS if spec.required_param}
    except Exception:
        return {}


_BUILTIN_SERVICE_PARAM_KEY = _load_builtin_param_key()

# 内置工具服务名 → 该服务可操作的一级资源类型。
_BUILTIN_SERVICE_RESOURCE_TYPE = {
    "cdp-bridge": {"cdp_client"},
    "mail": {"mail_account"},
    "issue-workflow": set(),
    "device-control": {"device"},
}


async def _check_service_auth(service_name: str, token: str) -> None:
    """校验 token；不通过抛 HTTPException(401/403)。

    统一走 builtin_tool_store.resolve_token：token → 解析授权目标。
    - 外部 MCP：token 绑 service_id，解析出的服务名必须与本次 service_name 一致。
    - 内置工具（cdp-bridge/mail/device-control）：token 绑 resource_id，资源类型必须
      与本次服务对应（cdp_client/mail_account/device）。会话/设备归属再由各自
      连接层按 resource id / device_id 隔离。
    - principal / identity token：按 mcp_grants 的 service 行判权——principal 被授权
      该服务即放行；具体操作哪个 CDP 客户端 / 邮箱账户 / 设备由插件 driver 读
      principal 的 param grant 收窄（无 grant = 该 owner 名下全部 enabled 实例，
      见 cdp_bridge_plugin / mail_plugin / device_control_plugin 的 _resolve_*_id）。
      网关不做「有没有 param」的前置拦截，校验下沉到工具侧。

    鉴权一律强制（不再看服务的 auth_enabled 开关）：MCP 网关是所有服务的统一
    安全边界，「auth_enabled=false 匿名放行」曾让拿到 SSE 地址的任何人直连资源，
    且造成 agent 侧按 DB 列跳过 token 收集 vs 网关强制要求的两边口径不一致（401）。
    auth_enabled 列/ctx 字段降级为仅展示。
    """
    plugin = registry.get(service_name)
    if not plugin:
        raise HTTPException(404, f"MCP service not loaded: {service_name}")
    ctx = plugin.ctx
    if not ctx.enabled:
        raise HTTPException(503, "该 MCP 服务已停用")
    if service_name == "marketplace-status":
        # 市场工具永远要求本次 Agent 对话签发的用户身份 token。
        if not token:
            raise HTTPException(401, "市场管理 MCP 需要管理员身份 token")
        import builtin_tool_store
        resolved = await builtin_tool_store.resolve_token(token)
        if not resolved or resolved.get("kind") != "identity":
            raise HTTPException(403, "token 未被授权访问市场管理 MCP")
        if (resolved.get("token") or {}).get("target_type") == "user":
            return
        raise HTTPException(403, "市场管理 MCP 需要用户身份 token")
    if not token:
        raise HTTPException(401, "该 MCP 服务已开启访问控制，请提供 token")

    import builtin_tool_store
    resolved = await builtin_tool_store.resolve_token(token)
    if not resolved:
        raise HTTPException(403, "token 未被授权访问该 MCP 服务")
    kind = resolved["kind"]

    async def _granted_via_service(principal_id: int) -> bool:
        """principal 被授权该服务（mcp_grants 的 service 行）即放行，不分内置/自定义。"""
        import mcp_plugin_store

        svc = await mcp_plugin_store.get_service_by_name(service_name)
        if not svc or svc.get("id") is None:
            return False
        return await mcp_plugin_store.mcp_user_granted_service(int(principal_id), int(svc["id"]))

    if kind == "principal":
        principal_id = (resolved.get("target") or {}).get("id")
        if await _granted_via_service(int(principal_id)):
            return
    elif kind == "service":
        if resolved["target"].get("name") == service_name:
            return
    elif kind == "resource":
        want_type = _BUILTIN_SERVICE_RESOURCE_TYPE.get(service_name)
        if want_type and resolved["target"].get("resource_type") in want_type:
            return
    elif kind == "identity":
        # identity token：解到使用方身份（agent/task）→ 其绑定的 principal → service grant。
        # 内置工具的实例范围（哪个浏览器/邮箱/设备）由插件 driver 读 principal 的 param
        # grant 收窄；网关只判「能不能用这个服务」。
        token_meta = resolved.get("token") or {}
        target_type = token_meta.get("target_type")
        target_id = token_meta.get("target_id")
        import mcp_plugin_store

        principal_id: int | None = None
        if target_type == "agent" and target_id and str(target_id).isdigit():
            principal_id = await mcp_plugin_store.get_agent_mcp_principal_id(int(target_id))
        elif target_type == "task" and target_id:
            # task identity token（任务派发的内置/sse MCP 走网关用）：解 task → 其 principal。
            # 替代 issue-workflow 旧实现拿 "agent" 类型装 task id 的 hack。
            from user_platform.task_service import Task
            try:
                task_row = await Task.filter(id=str(target_id)).first()
            except Exception:
                task_row = None
            if task_row is not None:
                principal_id = getattr(task_row, "mcp_user_id", None)
        if principal_id is not None and await _granted_via_service(principal_id):
            return
        if target_type == "user" and target_id:
            # editor 会话的 user identity token：无 principal，按用户级授权判权——
            # sse/custom 服务走 can_use_service（与编辑器 create/update 校验同源）；
            # 内置工具服务直接放行——实例范围（哪个浏览器/邮箱/设备）由插件 driver
            # resolve_identity_to_principal 返回 None → 走 owner 全量路径收窄。
            svc = await mcp_plugin_store.get_service_by_name(service_name)
            if svc and svc.get("id") is not None:
                if await mcp_plugin_store.can_use_service(
                    user_id=str(target_id), service_id=int(svc["id"]),
                ):
                    return
        # Session-scoped internal tools (issue-workflow / review-result) use an
        # identity token. The plugin itself narrows target_type/target_id to one
        # task / one review event and derives the issue from that task, so the
        # gateway only verifies that this is an authenticated identity and leaves
        # object-level authorization to the adapter.
        if service_name in {"issue-workflow", "review-result", "marketplace-status"}:
            if service_name == "issue-workflow" and target_type in ("agent", "node", "task"):
                # task：任务派发的 issue-workflow overlay 现签 issue_token("task", task.id)
                # （替代旧实现拿 "agent" 类型装 task id 的 hack），插件按 target_id 收口到该任务。
                return
            if service_name == "review-result" and target_type == "agent":
                # token 的 target_id 是 webhook event id（issue_token("agent", event.id)），
                # 不是 agents 行 id——上面的 principal 反查必然落空；对象级授权由
                # review_result_plugin._scope 按 event 收口。
                return
            if service_name == "marketplace-status" and target_type == "user":
                return
    raise HTTPException(403, "token 未被授权访问该 MCP 服务")


def _tools_for_service(service_name: str) -> list[dict]:
    plugin = registry.get(service_name)
    if not plugin:
        raise KeyError(service_name)
    return [
        {
            "name": t.name,
            "description": t.description or f"MCP tool {t.name}",
            "inputSchema": t.params or {"type": "object", "properties": {}},
        }
        for t in plugin.tools
    ]


async def _handle_rpc(service_name: str, payload: dict, token: str) -> dict | None:
    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    # 所有方法都受 auth_enabled 约束（已开鉴权则必须带合法 token）。
    await _check_service_auth(service_name, token)

    if method == "initialize":
        return _jsonrpc_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": f"ai-lubricant-mcp-runtime/{service_name}", "version": "0.1.0"},
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _jsonrpc_result(req_id, {})
    if method == "tools/list":
        try:
            return _jsonrpc_result(req_id, {"tools": _tools_for_service(service_name)})
        except KeyError:
            return _jsonrpc_error(req_id, -32004, f"service not loaded: {service_name}")
    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        # 把本次请求 token 注入 ContextVar，插件可读取用于隔离（cdp-bridge 会桥接）。
        tok = current_request_token.set(token)
        try:
            result = await registry.call_tool(service_name, tool_name, args)
        except Exception as e:
            return _jsonrpc_error(req_id, -32000, str(e))
        finally:
            current_request_token.reset(tok)
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
        else:
            content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
        return _jsonrpc_result(req_id, {"content": content})

    return _jsonrpc_error(req_id, -32601, f"method not found: {method}")


# ── 形态 C：节点托管 stdio MCP 的代理端点 ──────────────────────────────────────
#
# 路径比 /mcp/{service_name}/sse 多一段，不与之冲突。这里**不经 registry**：托管进程
# 在节点上，本进程没有它的插件对象，只做纯 I/O 转发（服务端连不到节点内网端口，必须
# 由节点代发）。对 agent/编辑器而言这就是一个普通 remote SSE 端点。


@router.get("/node-hosted/{service_id}/sse")
async def node_hosted_sse(service_id: int, request: Request):
    """把节点上托管进程的 SSE 流原样代理出来。"""
    return await _proxy_node_hosted(service_id, request, method="GET", path="/sse")


@router.post("/node-hosted/{service_id}/messages")
async def node_hosted_messages(service_id: int, request: Request):
    """转发 JSON-RPC 消息到节点上的托管进程。"""
    query = request.url.query
    path = "/messages" + (f"?{query}" if query else "")
    return await _proxy_node_hosted(
        service_id, request, method="POST", path=path, body=await request.body()
    )


async def _proxy_node_hosted(
    service_id: int, request: Request, *, method: str, path: str, body: bytes = b""
) -> StreamingResponse:
    import mcp_plugin_store
    from mcp_runtime.node_hosted import NodeHostedError, proxy_request

    service = await mcp_plugin_store.get_service(service_id)
    if not service:
        raise HTTPException(404, f"MCP service {service_id} not found")
    if (service.get("deploy_scope") or "") != "node_hosted":
        raise HTTPException(400, f"service {service_id} is not node-hosted")
    # 鉴权沿用服务级开关：托管 MCP 与其他服务同一套访问控制，不因为多了一跳就放宽。
    token = _extract_token(request)
    await _check_service_auth(service.get("name") or "", token)

    try:
        resp = await proxy_request(service, method=method, path=path, body=body)
    except NodeHostedError as e:
        raise HTTPException(502, str(e))

    async def relay():
        try:
            async for chunk in resp.iter_chunks():
                yield chunk
        finally:
            await resp.close()

    # 保留上游 content-type（SSE 必须是 text/event-stream，否则客户端不会按流处理）
    media = resp.headers.get("Content-Type") or resp.headers.get("content-type") or "text/event-stream"
    return StreamingResponse(relay(), status_code=resp.status or 200, media_type=media)


@router.get("/{service_name}/sse")
async def open_sse(service_name: str, request: Request):
    """打开 MCP-over-SSE 事件流，先下发 endpoint 事件告知 messages 地址。

    服务必须已加载进 registry（否则 404）；按服务级鉴权开关校验 token
    （Authorization: Bearer 或 ?token=，不通过 401/403）。校验通过的 token 存入
    session，供后续 RPC 透传给插件 handler。每 20s 无数据发一条 keepalive，
    客户端断连时清理 session。
    """
    if not registry.get(service_name):
        raise HTTPException(404, f"MCP service not loaded: {service_name}")
    token = _extract_token(request)
    await _check_service_auth(service_name, token)
    session_id = uuid.uuid4().hex
    queue: asyncio.Queue[str] = asyncio.Queue()
    _sessions[session_id] = _Session(queue=queue, service_name=service_name, token=token)
    sse_url = f"/mcp/{service_name}/sse"
    if token:
        sse_url = f"{sse_url}?token={token}"

    async def gen():
        try:
            endpoint = f"/mcp/{service_name}/messages?session_id={session_id}"
            yield _sse("endpoint", endpoint)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=20)
                    yield item
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sessions.pop(session_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/{service_name}/messages")
async def post_message(service_name: str, request: Request, session_id: str):
    """接收外部 MCP 客户端发来的 JSON-RPC 请求，响应回写到该 session 的 SSE 流。

    session_id 对应 open_sse 建立的事件流，不存在返回 404；鉴权沿用建流时的
    token。纯通知（无 id）不回响应，只返回 {"ok": true}。
    """
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(404, "SSE session not found")
    payload = await request.json()
    response = await _handle_rpc(session.service_name, payload, session.token)
    if response is not None:
        await session.queue.put(_sse("message", response))
    return {"ok": True}


class _ExtWsAdapter:
    """把 FastAPI WebSocket 适配成 driver 期望的 send_message(str) 客户端。

    driver 的命令下发在工作线程里同步调用 send_message，这里用
    run_coroutine_threadsafe 回投到事件循环执行 ws.send_text。
    """

    __slots__ = ("_ws", "_loop", "_token", "_user_id", "_client_id", "_client_alias", "_last_in")

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self._ws = ws
        self._loop = loop
        self._token = ""
        self._user_id = None
        self._client_id = None
        self._client_alias = ""
        # 最近一次收到本连接入站帧的时刻（bind_client 写初值、ext_session 逐帧刷新）。
        # 供 bind_client 判定在任连接是不是半开死连接/被遗忘的孤儿 socket。
        self._last_in = 0.0

    def send_message(self, payload: str) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._ws.send_text(payload), self._loop)
            fut.result(timeout=10)
        except Exception:
            # 发送失败（连接已断等）静默：driver 的超时轮询会兜底。
            pass

    def post_message(self, payload: str) -> None:
        """向该连接 fire-and-forget 投递一帧，**不阻塞调用线程**。

        send_message 会 ``.result(timeout=10)`` 等待，适合 driver 同步 exec 的往返；
        但「配置变更 → 通知所有已连接浏览器刷新 agent 列表」这类广播如果在事件
        循环线程里调用会死锁（在自己循环上 .result() 永远排不上），所以这里只把
        协程调度进连接所属的 loop 就返回，发送失败静默（连接已断则客户端重连后
        自然重拉一次列表）。
        """
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send_text(payload), self._loop)
        except Exception:
            pass
    def close_auth(self, code: int, reason: str) -> None:
        # 可能从两种上下文被调用：
        # - driver 工作线程（execute_js 等）：跨线程，用 run_coroutine_threadsafe 并等待。
        # - WebSocket 事件循环本身（config reload 撤销）：此时不能在本循环上 .result()
        #   阻塞，否则关闭协程永远排不上队 → 卡死 10s。直接 fire-and-forget 调度关闭即可。
        coro = self._ws.close(code=code, reason=reason)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._loop.create_task(coro)
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            fut.result(timeout=10)
        except Exception:
            pass


def _frame_type(raw: str) -> str:
    """取扩展帧的 type；非 JSON 或非对象返回空串。

    原先 ping/chat 各解析一次，再加 net_event 就是每帧三次 parse。收敛成一次。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("type") or "")


async def _route_ext_frame(raw: str, websocket: WebSocket, adapter: "_ExtWsAdapter", driver) -> None:
    """已认证扩展连接的逐帧分发：探活 / 聊天 / 抓包推送 / cdp driver 帧。"""
    frame_type = _frame_type(raw)
    if frame_type == "ping":
        await _send_ws_protocol(websocket, "pong")
        return
    if frame_type.startswith("chat_"):
        await _handle_chat_frame(raw, websocket, adapter, driver)
        return
    if frame_type.startswith("pwd_"):
        await _handle_pwd_frame(raw, websocket, adapter)
        return
    if frame_type.startswith("totp_"):
        await _handle_totp_frame(raw, websocket, adapter)
        return
    if frame_type == "net_event":
        # 抓包推送（browser_watch_network push=true）：HTTP 请求在 frame["request"]，
        # WebSocket/SSE 帧在 frame["wsFrame"]（site runner 的 await 通道按形状取用）。
        # 投递成功即消费掉；未注册的 captureId 返回 False，落回 driver 帧路径
        # （拉取式抓包不受影响）。
        from mcp_builtin.cdp_bridge import net_capture as site_capture

        try:
            frame = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        if site_capture.deliver(frame):
            return
    await asyncio.to_thread(driver.ingest_message, raw, adapter)


async def _send_chat_frame(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))


def _chat_identity(adapter: _ExtWsAdapter, frame: dict) -> dict:
    """只从已认证连接构造聊天身份；页面帧中的身份/token 字段一律忽略。

    会话池按 client_id 隔离，网页对话由 CDP 客户端驱动，身份以 client_id 为主。
    """
    tab_id = frame.get("tabId")
    session_key = f"{adapter._client_id}:{tab_id}" if tab_id is not None else ""
    return {
        "user_id": adapter._user_id,
        "client_id": str(adapter._client_id or ""),
        "client_alias": adapter._client_alias or str(adapter._client_id or ""),
        "session_key": session_key,
    }


def _owned_chat_page(driver, adapter: _ExtWsAdapter, session_key: str):
    """返回该认证连接实际拥有的活动页面，拒绝仅靠帧声明的页面。"""
    if not session_key:
        return None
    try:
        # 会话池按 client_id 隔离，context key = client_id。
        page = driver.get_context(str(adapter._client_id or "")).sessions.get(session_key)
    except (TypeError, ValueError):
        return None
    if not page or not page.is_active() or page.ws_client is not adapter:
        return None
    return page


# ── 浏览器插件密码管理器帧 ────────────────────────────────────────────────────

async def _send_pwd_frame(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))


async def _send_pwd_error(websocket: WebSocket, req_id: str, message: str) -> None:
    await _send_pwd_frame(
        websocket, {"type": "pwd_error", "reqId": req_id, "ok": False, "message": message}
    )


def _pwd_int_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# 存储层 ValueError code → 用户可读中文（口径见 browser_password_store）。
_PWD_VALUE_ERRORS = {
    "domain_required": "域名无效",
    "username_required": "账号不能为空",
    "password_required": "密码不能为空",
    "password_too_long": "密码过长",
    "username_too_long": "账号过长",
    "domain_too_long": "域名过长",
    "password_quota_exceeded": "保存的密码数量已达上限（500 条）",
    "duplicate_entry": "该站点下已存在同名账号记录",
    "invalid_blob": "记录无法解密（服务端密钥已变更）",
    "unsupported_blob_version": "记录无法解密（服务端密钥已变更）",
}


async def _handle_pwd_frame(raw: str, websocket: WebSocket, adapter: "_ExtWsAdapter") -> None:
    """处理浏览器插件密码管理器帧（list/get/save/delete）。

    身份只取自已鉴权连接（client_id → owner_user_id），帧内任何身份字段一律
    忽略——同 ``_chat_identity`` 口径，防止页面伪造 owner 读写他人密码。

    ⚠ 帧负载含明文密码：本函数内除 ``logger.exception`` 外**不得记录任何 payload
    内容**，也不得把 ``raw`` / ``frame`` 交给 logger。
    """
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(frame, dict):
        return
    req_id = str(frame.get("reqId") or "")
    if not req_id:
        return

    client_id = str(adapter._client_id or "")
    if not client_id:
        await _send_pwd_error(websocket, req_id, "连接身份无效")
        return

    import mcp_plugin_store

    owner = await mcp_plugin_store.get_cdp_client_owner_user_id(client_id)
    if not owner:
        await _send_pwd_error(websocket, req_id, "该客户端未绑定用户，无法使用密码管理")
        return

    import browser_password_store as pwd_store

    kind = str(frame.get("type") or "")
    try:
        if kind == "pwd_list":
            entries = await pwd_store.list_passwords(owner, frame.get("domain") or None)
            await _send_pwd_frame(websocket, {"type": "pwd_list_result", "reqId": req_id, "ok": True, "entries": entries})
        elif kind == "pwd_get":
            entry = await pwd_store.get_password(owner, _pwd_int_id(frame.get("id")))
            if entry is None:
                await _send_pwd_error(websocket, req_id, "记录不存在")
            else:
                await _send_pwd_frame(websocket, {"type": "pwd_get_result", "reqId": req_id, "ok": True, "entry": entry})
        elif kind == "pwd_save":
            entry = await pwd_store.upsert_password(
                owner,
                domain=frame.get("domain") or "",
                username=frame.get("username") or "",
                password=frame.get("password") or "",
                url=frame.get("url") or "",
                # 插件侧自动保存 → browser；网页端编辑走 REST（见 routes_browser_passwords）
                source="browser",
            )
            await _send_pwd_frame(websocket, {"type": "pwd_save_result", "reqId": req_id, "ok": True, "entry": entry})
        elif kind == "pwd_delete":
            entry_id = _pwd_int_id(frame.get("id"))
            if not await pwd_store.delete_password(owner, entry_id):
                await _send_pwd_error(websocket, req_id, "记录不存在")
            else:
                await _send_pwd_frame(websocket, {"type": "pwd_delete_result", "reqId": req_id, "ok": True, "id": entry_id})
        else:
            await _send_pwd_error(websocket, req_id, "不支持的密码管理操作")
    except ValueError as exc:
        await _send_pwd_error(websocket, req_id, _PWD_VALUE_ERRORS.get(str(exc), "参数无效"))
    except InvalidTag:
        # 密钥已轮换 / 密文被篡改：给出可操作提示而非通用失败。
        await _send_pwd_error(websocket, req_id, "记录无法解密（服务端密钥已变更）")
    except Exception:
        # 不记录 payload（含明文密码）；仅记异常栈。
        logger.exception("[cdp-bridge] pwd frame failed")
        await _send_pwd_error(websocket, req_id, "密码管理操作失败，请稍后重试")


# ── 浏览器插件身份验证器（TOTP）帧 ────────────────────────────────────────────
#
# 与 pwd_ 帧同构，但**回帧永不含明文密钥**：扩展/网页只拿到 meta + 当前 6 位码 +
# 剩余秒数（get/list_by_domain 才带码；list 只带 meta）。密钥的解密只发生在
# totp_store 内部瞬时内存，任何出口都不外传——这是比密码库更严的口径。

_TOTP_VALUE_ERRORS = {
    "secret_required": "密钥不能为空",
    "secret_invalid_base32": "密钥格式无效（需 base32，或粘贴完整 otpauth:// 链接）",
    "account_required": "账号不能为空",
    "domains_required": "至少关联一个网站域名",
    "digits_unsupported": "验证码位数仅支持 6/7/8",
    "period_unsupported": "翻转周期需在 15~300 秒之间",
    "algorithm_unsupported": "算法仅支持 SHA1/SHA256/SHA512",
    "totp_quota_exceeded": "保存的验证器数量已达上限（300 条）",
    "duplicate_entry": "该站点下已存在同名账号的验证器",
    "uri_required": "otpauth 链接不能为空",
    "uri_not_otpauth": "不是 otpauth:// 链接",
    "uri_not_totp": "仅支持 TOTP（时间型）验证器",
    "invalid_blob": "记录无法解密（服务端密钥已变更）",
    "unsupported_blob_version": "记录无法解密（服务端密钥已变更）",
}


async def _send_totp_frame(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_text(json.dumps(payload, ensure_ascii=False))


async def _send_totp_error(websocket: WebSocket, req_id: str, message: str) -> None:
    await _send_totp_frame(
        websocket, {"type": "totp_error", "reqId": req_id, "ok": False, "message": message}
    )


def _totp_int_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _handle_totp_frame(raw: str, websocket: WebSocket, adapter: "_ExtWsAdapter") -> None:
    """处理浏览器插件身份验证器帧（list/get/save/delete/by_domain）。

    身份只取自已鉴权连接（client_id → owner_user_id），帧内任何身份字段一律忽略
    ——同 ``_handle_pwd_frame`` 口径，防止页面伪造 owner 读写他人验证器。

    ⚠ 帧负载可能含明文密钥（``totp_save``）：本函数内除 ``logger.exception`` 外
    **不得记录任何 payload 内容**，也不得把 ``raw`` / ``frame`` 交给 logger。
    回帧永不含密钥。
    """
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(frame, dict):
        return
    req_id = str(frame.get("reqId") or "")
    if not req_id:
        return

    client_id = str(adapter._client_id or "")
    if not client_id:
        await _send_totp_error(websocket, req_id, "连接身份无效")
        return

    import mcp_plugin_store

    owner = await mcp_plugin_store.get_cdp_client_owner_user_id(client_id)
    if not owner:
        await _send_totp_error(websocket, req_id, "该客户端未绑定用户，无法使用身份验证器")
        return

    import totp_store as totp_svc
    from totp_code import parse_otpauth

    kind = str(frame.get("type") or "")
    try:
        if kind == "totp_list":
            domain = frame.get("domain") or None
            # 一次取表、同一时刻算码（list_with_codes）——替代 list_credentials +
            # 逐条 get_credential 的 N+1。回帧带 code + **expiresAt（绝对失效时刻）**。
            # 密钥不出服务端 → 本机无法本地算码，列表直接带回当前码。
            entries = await totp_svc.list_with_codes(owner, domain)
            await _send_totp_frame(websocket, {"type": "totp_list_result", "reqId": req_id, "ok": True, "entries": entries})
        elif kind == "totp_get":
            entry = await totp_svc.get_credential(owner, _totp_int_id(frame.get("id")))
            if entry is None:
                await _send_totp_error(websocket, req_id, "记录不存在")
            else:
                await _send_totp_frame(websocket, {"type": "totp_get_result", "reqId": req_id, "ok": True, "entry": entry})
        elif kind == "totp_by_domain":
            entries = await totp_svc.find_by_domain(owner, frame.get("domain") or "")
            await _send_totp_frame(websocket, {"type": "totp_codes_result", "reqId": req_id, "ok": True, "entries": entries})
        elif kind == "totp_refresh_codes":
            # 拉式批量刷（网页端/兜底用；扩展已走推模型）：只对这些 id 解密算码，
            # 回 {id, code, expiresAt}——比 totp_list 全表重算轻。
            ids = frame.get("ids") or []
            if not isinstance(ids, list):
                ids = []
            ids = [i for i in (_totp_int_id(x) for x in ids) if i > 0][:200]
            refreshed = []
            for eid in ids:
                entry = await totp_svc.get_credential(owner, eid)
                if entry:  # 属于别人的 id 直接返回 None（无 oracle），静默跳过
                    refreshed.append({
                        "id": entry["id"],
                        "code": entry.get("code", ""),
                        "expiresAt": entry.get("expiresAt", 0),
                    })
            await _send_totp_frame(websocket, {"type": "totp_refresh_codes_result", "reqId": req_id, "ok": True, "entries": refreshed})
        elif kind == "totp_subscribe":
            # **推模型（服务端内存）**：订阅后服务端**立即**把内存里的列表（meta + code +
            # expiresAt）推给这条连接，客户端不用等下一次翻转才见数。此后每次翻转，服务端
            # 先刷新内存、再推给该 owner 的全部订阅者——订阅回帧与推送携带**同一份**数据
            # （同一 expiresAt）。domain 空串 = 全部（「全部」页签）。
            from mcp_runtime import totp_push

            await totp_push.subscribe(adapter, owner, str(frame.get("domain") or ""))
            await _send_totp_frame(websocket, {"type": "totp_subscribed", "reqId": req_id, "ok": True})
        elif kind == "totp_unsubscribe":
            from mcp_runtime import totp_push

            totp_push.unsubscribe(adapter)
            await _send_totp_frame(websocket, {"type": "totp_unsubscribed", "reqId": req_id, "ok": True})
        elif kind == "totp_save":
            secret = str(frame.get("secret") or "")
            issuer = str(frame.get("issuer") or "")
            account = str(frame.get("account") or "")
            label = str(frame.get("label") or "")
            digits = frame.get("digits") or 6
            period = frame.get("period") or 30
            algorithm = frame.get("algorithm") or "SHA1"
            domains = frame.get("domains") or []
            # 扩展扫到的是整条 otpauth:// URI：以解析结果为准，请求里的展示字段叠加。
            if secret.strip().lower().startswith("otpauth://"):
                parsed = parse_otpauth(secret)
                issuer = issuer or parsed["issuer"]
                account = account or parsed["account"]
                label = label or parsed["label"]
                digits, period, algorithm = parsed["digits"], parsed["period"], parsed["algorithm"]
                secret = parsed["secret"]
                if not domains:
                    domains = [issuer.lower()] if issuer else []
            entry = await totp_svc.upsert_credential(
                owner,
                secret=secret,
                label=label, issuer=issuer, account=account,
                domains=domains, note=str(frame.get("note") or ""),
                url=str(frame.get("url") or ""),
                digits=digits, period=period, algorithm=algorithm,
                source="browser",
            )
            full = await totp_svc.get_credential(owner, entry["id"])
            # 增删改后**先刷新内存、再推**：所有开着的 popup 立刻看到新列表。
            from mcp_runtime import totp_push

            await totp_push.invalidate(owner)
            await _send_totp_frame(websocket, {"type": "totp_save_result", "reqId": req_id, "ok": True, "entry": full or entry})
        elif kind == "totp_update":
            # 编辑**展示字段**（站点/账号/关联网站/备注/URL/显示名）——**不改密钥**
            # （换密钥 = 站点重新发二维码，走 totp_save/upsert）。按 id 定位，不新增行。
            entry_id = _totp_int_id(frame.get("id"))
            if entry_id <= 0:
                await _send_totp_error(websocket, req_id, "记录不存在")
            else:
                meta = await totp_svc.update_credential(
                    owner, entry_id,
                    label=frame.get("label"),
                    issuer=frame.get("issuer"),
                    account=frame.get("account"),
                    domains=frame.get("domains"),
                    note=frame.get("note"),
                    url=frame.get("url"),
                )
                if meta is None:
                    await _send_totp_error(websocket, req_id, "记录不存在")
                else:
                    full = await totp_svc.get_credential(owner, entry_id)
                    from mcp_runtime import totp_push

                    await totp_push.invalidate(owner)
                    await _send_totp_frame(websocket, {"type": "totp_update_result", "reqId": req_id, "ok": True, "entry": full or meta})
        elif kind == "totp_delete":
            entry_id = _totp_int_id(frame.get("id"))
            if not await totp_svc.delete_credential(owner, entry_id):
                await _send_totp_error(websocket, req_id, "记录不存在")
            else:
                from mcp_runtime import totp_push

                await totp_push.invalidate(owner)
                await _send_totp_frame(websocket, {"type": "totp_delete_result", "reqId": req_id, "ok": True, "id": entry_id})
        elif kind == "totp_import":
            # 批量导入（扩展 popup 选 .txt 后整包发来）：逐行解析 + upsert，回逐行结果。
            # 与网页 /import 同一套存储层逻辑，source 恒标 browser（扩展侧动作）。
            from totp_code import parse_otpauth

            content = str(frame.get("content") or "")
            if len(content) > 1_000_000:
                await _send_totp_error(websocket, req_id, "导入内容过大（上限 1MB）")
            else:
                imported = overwritten = 0
                errors = []
                for idx, line in enumerate(content.splitlines(), start=1):
                    raw = line.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    if not raw.lower().startswith("otpauth://"):
                        if len(errors) < 20:
                            errors.append({"line": idx, "reason": "不是 otpauth:// 链接"})
                        continue
                    try:
                        parsed = parse_otpauth(raw)
                    except ValueError as exc:
                        if len(errors) < 20:
                            errors.append({"line": idx, "reason": _TOTP_VALUE_ERRORS.get(str(exc), "该行无法解析")})
                        continue
                    try:
                        existed = await totp_svc.credential_exists(owner, parsed["issuer"], parsed["account"])
                        await totp_svc.upsert_credential(
                            owner,
                            secret=parsed["secret"], label=parsed["label"],
                            issuer=parsed["issuer"], account=parsed["account"],
                            # 域名：issuer 是**站点名**（"Google"）不是域名（"accounts.
                            # google.com"），硬当域名存会让本站页签/agent 取码永远
                            # 匹配不上。存 issuer 小写 token 仍值得做（host_matches_
                            # domains 按 token 兜底匹配），但真实域名只能靠用户后续
                            # 在「编辑」里补——导入文件里没有这信息，不编造。
                            domains=[parsed["issuer"].lower()] if parsed["issuer"] else [],
                            note="", url="",
                            digits=parsed["digits"], period=parsed["period"], algorithm=parsed["algorithm"],
                            source="browser",
                        )
                        if existed:
                            overwritten += 1
                        else:
                            imported += 1
                    except ValueError as exc:
                        # account 空（该行 otpauth 没带账号）→ **跳过**而非失败：
                        # 唯一键要有落点，但用户能从行号自己看出来缺了什么。文案区分。
                        code = str(exc)
                        reason = _TOTP_VALUE_ERRORS.get(code, "该行无法保存")
                        if code == "account_required":
                            reason = "该行没有账号（otpauth 缺 account），已跳过；可在添加后手动补"
                        if len(errors) < 20:
                            errors.append({"line": idx, "reason": reason, "skipped": code == "account_required"})
                await _send_totp_frame(websocket, {
                    "type": "totp_import_result", "reqId": req_id, "ok": True,
                    "imported": imported, "overwritten": overwritten,
                    "errors": errors, "error_count": sum(1 for _ in errors),
                    # 区分「跳过」与「失败」：缺账号的行是数据本身没带，不是出错。
                    "skipped_count": sum(1 for e in errors if e.get("skipped")),
                })
                await totp_svc.audit_bulk(owner, action="import", detail_extra={
                    "imported": imported, "errors": len(errors),
                })
                # 导入后**先刷新内存、再推**：所有开着的 popup 立刻看到新条目。
                from mcp_runtime import totp_push

                await totp_push.invalidate(owner)
        elif kind == "totp_export":
            # 备份导出（扩展 popup）：与网页 /export 同口径——**必须账号密码二次鉴权**，
            # 恒为本人数据，导出内容含明文密钥，本函数内不得记录 payload。
            from totp_code import build_otpauth_uri

            password = str(frame.get("password") or "")
            if not await totp_svc.verify_owner_password(owner, password):
                await _send_totp_error(websocket, req_id, "账号密码不正确，无法导出")
            else:
                rows = await totp_svc.export_credentials(owner)
                lines = [
                    build_otpauth_uri(
                        issuer=r["issuer"], account=r["account"], secret=r["secret"],
                        digits=r["digits"], period=r["period"], algorithm=r["algorithm"],
                    )
                    for r in rows
                ]
                content = "\n".join(lines) + ("\n" if lines else "")
                await _send_totp_frame(websocket, {
                    "type": "totp_export_result", "reqId": req_id, "ok": True,
                    "count": len(lines), "content": content,
                })
                await totp_svc.audit_bulk(owner, action="export", detail_extra={"count": len(lines)})
        else:
            await _send_totp_error(websocket, req_id, "不支持的身份验证器操作")
    except ValueError as exc:
        await _send_totp_error(websocket, req_id, _TOTP_VALUE_ERRORS.get(str(exc), "参数无效"))
    except InvalidTag:
        await _send_totp_error(websocket, req_id, "记录无法解密（服务端密钥已变更）")
    except Exception:
        # 不记录 payload（可能含明文密钥）；仅记异常栈。
        logger.exception("[cdp-bridge] totp frame failed")
        await _send_totp_error(websocket, req_id, "身份验证器操作失败，请稍后重试")


async def _handle_chat_frame(raw: str, websocket: WebSocket, adapter: _ExtWsAdapter, driver=None) -> None:
    """处理网页侧 Agent 对话帧，身份仅来自已认证 CDP WebSocket。"""
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    req_id = str(frame.get("reqId") or "")
    if not req_id:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "missing request id"})
        return

    import mcp_plugin_store

    identity = _chat_identity(adapter, frame)
    client_id = identity["client_id"]
    # 网页对话由 CDP 客户端驱动：可选 agent = 授权了该客户端的 agent（agent 侧 grants）。
    if not client_id:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "连接身份无效"})
        return

    kind = frame.get("type")
    if kind == "chat_list_agents":
        agents = await mcp_plugin_store.list_agents_for_cdp_client(client_id)
        await _send_chat_frame(websocket, {"type": "chat_agents", "reqId": req_id, "agents": agents})
        return

    if kind == "chat_list_models":
        await _proxy_chat_list_models(frame, identity, websocket)
        return

    if kind == "chat_list_skills":
        await _proxy_chat_list_skills(frame, identity, websocket)
        return

    if kind == "chat_watch":
        # 面板把某会话切到前台 → 确保该会话的泵在跑。快照由面板自己走
        # chat_load_conversation（已有路径），这里不重复拉一次历史。
        conv_id = str(frame.get("conversationId") or "").strip()
        if not conv_id:
            await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId is required"})
            return
        _ensure_conversation_pump(websocket, conv_id)
        await _send_chat_frame(websocket, {"type": "chat_watch_ok", "reqId": req_id, "conversationId": conv_id})
        return

    if kind == "chat_unwatch":
        # 该 WS 上已无标签页看这个会话 → 撤泵。只是在释放订阅，不涉及任何状态恢复。
        conv_id = str(frame.get("conversationId") or "").strip()
        if conv_id:
            drop_conversation_pump(websocket, conv_id)
        await _send_chat_frame(websocket, {"type": "chat_unwatch_ok", "reqId": req_id, "conversationId": conv_id})
        return

    if kind == "chat_abort":
        await _proxy_chat_abort(frame, {}, identity, websocket)
        return

    if kind == "chat_list_conversations":
        await _proxy_chat_list_conversations(frame, identity, websocket)
        return

    if kind == "chat_new_conversation":
        await _proxy_chat_new_conversation(frame, identity, websocket)
        return

    if kind == "chat_load_conversation":
        await _proxy_chat_load_conversation(frame, identity, websocket)
        return

    if kind == "chat_update_settings":
        await _proxy_chat_update_settings(frame, identity, websocket)
        return

    if kind == "chat_upload_attachment":
        # 附件上传可能拖到 60s，作为后台任务跑，别堵住 WS 读循环（空闲读超时 +
        # 其余会话帧都会被这一个上传卡住）。回帧按 reqId 关联，任务里自己发。
        task = asyncio.create_task(_proxy_chat_upload_attachment(frame, identity, websocket))
        _CHAT_TASKS[req_id] = task
        task.add_done_callback(lambda _: _CHAT_TASKS.pop(req_id, None))
        return

    if kind == "chat_compact":
        # 压缩要跑一次 LLM 摘要（数十秒），同样后台跑。
        task = asyncio.create_task(_proxy_chat_compact(frame, identity, websocket))
        _CHAT_TASKS[req_id] = task
        task.add_done_callback(lambda _: _CHAT_TASKS.pop(req_id, None))
        return

    if kind == "chat_resolve_approval":
        await _proxy_chat_resolve_approval(frame, identity, websocket)
        return

    if kind == "chat_retry":
        conv_id = str(frame.get("conversationId") or "").strip()
        message_id = frame.get("messageId")
        if not conv_id or message_id is None:
            await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId and messageId are required"})
            return
        # retry 走 client_send_message 同一套鉴权，agent 由服务端从会话取，
        # 这里不重复 page 归属校验（会话已属本 client，服务端再核 client/session）。
        task = asyncio.create_task(_proxy_chat_retry(frame, identity, websocket))
        _CHAT_TASKS[req_id] = task
        task.add_done_callback(lambda _: _CHAT_TASKS.pop(req_id, None))
        return

    if kind != "chat_send":
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "unsupported chat frame"})
        return

    agent_id = frame.get("agentId")
    content = str(frame.get("content") or "").strip()
    if not isinstance(agent_id, int) or not content:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "agentId and content are required"})
        return
    page = _owned_chat_page(driver, adapter, identity["session_key"]) if driver is not None else None
    if page is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "page does not belong to this client"})
        return
    # URL/title 同样以 driver 已认证页面快照为准，不信任 chat frame。
    frame = {
        **frame,
        "url": page.info.get("url") or "",
        "title": page.info.get("title") or "",
    }

    allowed = await mcp_plugin_store.list_agents_for_cdp_client(client_id)
    if not any(agent.get("id") == agent_id for agent in allowed):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "agent is not allowed for this client"})
        return

    task = asyncio.create_task(_proxy_chat_stream(frame, {}, identity, websocket))
    _CHAT_TASKS[req_id] = task
    task.add_done_callback(lambda _: _CHAT_TASKS.pop(req_id, None))


def _relay_failure_message(exc: BaseException) -> str:
    """把内部异常转成给网页用户看的话术。

    异常原文带回环地址和内部路径（aiohttp 的连接超时原文就是
    "Connection timeout to host http://127.0.0.1:8001/agent/client/messages"），
    直接回投等于把内部端口暴露给页面。真实原因只进服务端日志。
    """
    import aiohttp

    # ServerTimeoutError 是 ClientConnectionError 的子类，超时判断必须在前。
    if isinstance(exc, (aiohttp.ClientConnectorError, aiohttp.ServerTimeoutError, asyncio.TimeoutError)):
        return "网络异常：连接对话服务超时，请稍后重试"
    if isinstance(exc, (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError)):
        return "网络异常：对话连接中断，请重试"
    return "对话服务异常，请稍后重试"


# ── 会话事件泵：conversation_hub → 扩展 WS ────────────────────────────────────
#
# 一条扩展 WS 对每个会话最多一个泵；泵把 hub 广播的事件转成带 conversationId 的
# chat_event 帧发回扩展（不带 reqId —— 它不绑定某一次请求，由扩展侧按
# convWatchers 扇给所有正在看该会话的标签页）。
#
# 生命周期只跟着连接走：WS 断开 / 发送失败 → 泵退出并退订；面板不再看某会话时
# 由 chat_unwatch 主动撤泵。服务端不记「这个连接欠了哪些事件」——断线客户端自己
# 重连 + 重拉历史（chat_watch 的首帧快照）。
_CONV_PUMPS: dict[tuple[int, str], asyncio.Task] = {}


def _ensure_conversation_pump(websocket: WebSocket, conv_id: str) -> None:
    key = (id(websocket), conv_id)
    task = _CONV_PUMPS.get(key)
    if task is not None and not task.done():
        return
    task = asyncio.create_task(_conversation_pump(websocket, conv_id))
    _CONV_PUMPS[key] = task
    task.add_done_callback(
        lambda t, k=key: _CONV_PUMPS.pop(k, None) if _CONV_PUMPS.get(k) is t else None
    )


def drop_conversation_pump(websocket: WebSocket, conv_id: str) -> None:
    """撤掉某会话的泵（该 WS 上已无标签页在看它）。"""
    task = _CONV_PUMPS.pop((id(websocket), conv_id), None)
    if task is not None and not task.done():
        task.cancel()


async def _conversation_pump(websocket: WebSocket, conv_id: str) -> None:
    from agent import conversation_hub

    queue = conversation_hub.subscribe(conv_id)
    try:
        while True:
            event = await queue.get()
            # 发送加超时：客户端标签页被节流/冻结、手机进隧道时，asyncio 写缓冲过
            # 水位会让 send 一直挂住（websockets 的 pause_writing 背压），泵就永久
            # 卡在这一帧上——事件全堵在队列里，面板再也收不到东西。超时即撤泵：
            # WS 本就是不可靠传输，客户端重连/重拉快照自愈，服务端不为它做送达保证。
            try:
                await asyncio.wait_for(
                    _send_chat_frame(
                        websocket,
                        {"type": "chat_event", "conversationId": conv_id, "event": event},
                    ),
                    timeout=_CHAT_PUMP_SEND_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.debug(
                    "[cdp-chat] 会话泵发送超时（客户端读不动），撤泵 conv={}", conv_id,
                )
                return
    except Exception:  # noqa: BLE001 — 连接断开/发送失败：泵退出，不留任何状态
        pass
    finally:
        try:
            conversation_hub.unsubscribe(conv_id, queue)
        except Exception:  # noqa: BLE001
            pass


async def _post_chat_turn(
    path: str, identity: dict, payload: dict | None, req_id: str, websocket: WebSocket,
    conv_hint: str = "", *, panel_v: int = 0,
) -> str:
    """POST 发起一轮（发送与订阅已分离：响应是 JSON，事件走会话泵）。

    成功 → 回 chat_send_ok{conversationId, assistantMessageId} 并确保该会话的泵在跑；
    失败 → 把 detail 透回 chat_error（409=本轮执行中、403=无权、404=会话不存在）。
    返回 conversation_id（失败为空串）。

    panel_v：发起面板的协议版本。旧版 content script（panel_v=0，发送与订阅分离
    之前装的）不认 chat_send_ok，只会一直等 chat_event.done——事件改走会话泵后它
    等不到任何帧，永远卡在 sending。对它回一条明确的 chat_error 提示刷新扩展，
    而不是无声挂住。
    """
    # 异常必须变可见：本函数跑在 _handle_chat_frame 起的 create_task 里，done_callback
    # 只 pop 不取异常，任何抛出都会被 asyncio 记成 "Task exception was never
    # retrieved" 后静默消失，面板则永远等不到回帧（表现为「点了没反应」）。
    try:
        status, data = await _agent_internal_request("POST", path, identity, json_body=payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] POST {path} 失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return ""
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return ""
    if status >= 400 or not isinstance(data, dict):
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, str) and detail:
            message = detail
        elif detail is not None:
            message = json.dumps(detail, ensure_ascii=False)
        else:
            message = f"agent service HTTP {status}"
        # 机器可读 code：面板据此区分「上一轮还在跑（409）」与普通失败——前者不是
        # 真失败，是服务端已受理并仍在执行，面板应改为「接管这一轮」而不是报错。
        code = ""
        if status == 409:
            code = "turn_running"
        elif status == 403:
            code = "forbidden"
        elif status == 404:
            code = "not_found"
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": message, "code": code})
        return ""
    conv_id = str(data.get("conversation_id") or conv_hint or "")
    # 旧面板（panel_v < 2）：不认 chat_send_ok，事件也走会话泵它收不到 → 发了也是
    # 白发，对面永远卡在 sending。回一条明确的 chat_error + 提示刷新，让用户知道
    # 要更新扩展，而不是无声挂住。
    if panel_v < 2:
        await _send_chat_frame(websocket, {
            "type": "chat_error", "reqId": req_id,
            "message": "扩展版本过旧：发送与订阅已分离，请刷新页面让面板加载新版（chrome://extensions → 刷新 → 再刷新本页）",
        })
        # 仍起泵：旧面板收不到，但同浏览器的其它标签页（若已是新版）能看到这轮。
        if conv_id:
            _ensure_conversation_pump(websocket, conv_id)
        return conv_id
    await _send_chat_frame(websocket, {
        "type": "chat_send_ok",
        "reqId": req_id,
        "conversationId": conv_id,
        "assistantMessageId": data.get("assistant_message_id"),
    })
    if conv_id:
        _ensure_conversation_pump(websocket, conv_id)
    return conv_id


async def _proxy_chat_stream(frame: dict, user: dict, identity: dict, websocket: WebSocket) -> None:
    """发起一轮网页对话：组装 payload 后 POST 启动；事件由会话泵广播。"""
    req_id = str(frame["reqId"])
    payload = {
        "agent_id": frame["agentId"],
        "content": frame["content"],
        "tab_id": frame.get("tabId"),
        "url": frame.get("url") or "",
        "title": frame.get("title") or "",
        "conversation_id": frame.get("conversationId") or "",
    }
    # 对话级覆盖（面板输入区选择）：显式给了才透传，None/空由服务端沿用会话已存值。
    if frame.get("model"):
        payload["model"] = str(frame["model"])
    if frame.get("reasoningEffort") is not None:
        payload["reasoning_effort"] = str(frame.get("reasoningEffort") or "")
    mode = str(frame.get("mode") or "interact").lower()
    # auto：与 interact 同执行流程，但服务端把所有审批自动放行（不弹卡）。漏了它
    # 面板切「自动」也送不到服务端，永远落回 interact → 用户以为免审却仍被审核。
    if mode in ("interact", "plan", "goal", "auto"):
        payload["mode"] = mode
    if mode == "goal":
        objective = str(frame.get("goalObjective") or "").strip()
        try:
            minutes = float(frame.get("goalBudgetMinutes") or 15)
        except (TypeError, ValueError):
            minutes = 15.0
        payload["goal_config"] = {
            "objective": objective,
            "budget_seconds": max(60, int(minutes * 60)),
        }
    await _post_chat_turn(
        "/agent/client/messages",
        identity, payload, req_id, websocket,
        conv_hint=str(frame.get("conversationId") or ""),
        panel_v=int(frame.get("panelV") or 0),
    )


async def _proxy_chat_retry(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """重试一条失败的 assistant 消息：POST 启动新一轮，事件由会话泵广播。"""
    req_id = str(frame["reqId"])
    conv_id = str(frame.get("conversationId") or "").strip()
    message_id = frame.get("messageId")
    if not conv_id or message_id is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId and messageId are required"})
        return
    await _post_chat_turn(
        f"/agent/client/conversations/{conv_id}/messages/{message_id}/retry",
        identity, None, req_id, websocket, conv_hint=conv_id,
        panel_v=int(frame.get("panelV") or 0),
    )



async def _proxy_chat_abort(frame: dict, _user: dict, identity: dict, websocket: WebSocket) -> None:
    """取消本 runtime 的流任务，并通知主服务取消对应 conversation。"""
    import os
    import aiohttp

    req_id = str(frame.get("reqId") or "")
    task = _CHAT_TASKS.pop(req_id, None)
    if task and not task.done():
        task.cancel()
    conversation_id = str(frame.get("conversationId") or "")
    if conversation_id:
        base = _AGENT_INTERNAL_BASE
        internal_token = os.environ.get("AGENT_INTERNAL_TOKEN", "")
        if internal_token:
            try:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        f"{base}/agent/client/conversations/{conversation_id}/abort",
                        headers={
                            "X-Internal-Token": internal_token,
                            "X-Cdp-Client-Id": identity["client_id"],
                            "X-Cdp-Client-Alias": identity["client_alias"],
                            "X-Cdp-Session-Key": identity["session_key"],
                        },
                    )
            except Exception:
                pass
    await _send_chat_frame(websocket, {"type": "chat_aborted", "reqId": req_id})


def _internal_agent_headers(identity: dict, internal_token: str) -> dict:
    return {
        "X-Internal-Token": internal_token,
        "X-Cdp-Client-Id": identity["client_id"],
        "X-Cdp-Client-Alias": identity["client_alias"],
        "X-Cdp-Session-Key": identity["session_key"],
    }


async def _agent_internal_request(
    method: str, path: str, identity: dict, *,
    json_body: dict | None = None, raw_body: bytes | None = None, timeout_s: float = 30,
):
    """向同进程内的 CDP Agent 端点发一次非流式请求，返回 (status, json_or_none)。

    优先走**进程内 ASGI 直连**（同 app、同事件循环，不落 socket）：/agent/client/*
    与 sse_gateway 同进程，回环 HTTP 在事件循环繁忙时会 sock_connect 超时，把
    「发送受理」拖过面板看门狗（根因见 _get_asgi_app 注释）。取不到 app 或进程内
    派发失败才回落到回环 HTTP。

    未配置 internal token → (None, None)。网络/解析异常 → (status, None) 或 (None, None)。
    raw_body 用于附件上传（原始字节 + 文件 mime），与 json_body 互斥。
    """
    import os

    internal_token = os.environ.get("AGENT_INTERNAL_TOKEN", "")
    if not internal_token:
        return None, None
    headers = _internal_agent_headers(identity, internal_token)
    if raw_body is not None:
        headers = {**headers, "Content-Type": "application/octet-stream"}

    if os.environ.get("CDP_CHAT_INPROC", "1") != "0":
        app = _get_asgi_app()
        if app is not None:
            try:
                return await _agent_internal_request_asgi(
                    app, method, path, headers, timeout_s, json_body, raw_body,
                )
            except Exception as exc:  # noqa: BLE001 — 进程内失败回落回环，不让面板吃瘪
                logger.debug("[cdp-chat] 进程内派发 %s %s 失败，回退回环：%r", method, path, exc)

    return await _agent_internal_request_http(method, path, headers, timeout_s, json_body, raw_body)


async def _agent_internal_request_asgi(
    app: Any, method: str, path: str, headers: dict, timeout_s: float,
    json_body: dict | None, raw_body: bytes | None,
):
    """进程内 ASGI 直连：httpx.ASGITransport 把请求交给同一个 app 处理。

    不落 socket，故事件循环再忙也不会 sock_connect 超时。响应体仍按 JSON 解析
    （失败 → None），与回环路径同契约。httpx 用 ``content=`` 传原始字节。
    """
    import httpx

    transport = httpx.ASGITransport(app=app)
    timeout = httpx.Timeout(timeout_s, connect=timeout_s)
    kwargs: dict = {"content": raw_body} if raw_body is not None else {"json": json_body}
    async with httpx.AsyncClient(transport=transport, base_url="http://internal", timeout=timeout) as client:
        response = await client.request(method, path, headers=headers, **kwargs)
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            data = None
        return response.status_code, data


async def _agent_internal_request_http(
    method: str, path: str, headers: dict, timeout_s: float,
    json_body: dict | None, raw_body: bytes | None,
):
    """回环 HTTP 回退路径（进程内直连不可用/失败时）。aiohttp 用 ``data=`` 传字节。"""
    import aiohttp

    base = _AGENT_INTERNAL_BASE
    kwargs: dict = {"data": raw_body} if raw_body is not None else {"json": json_body}
    timeout = aiohttp.ClientTimeout(total=timeout_s, sock_connect=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, f"{base}{path}", headers=headers, **kwargs) as response:
            status = response.status
            try:
                data = await response.json()
            except Exception:  # noqa: BLE001
                data = None
            return status, data


async def _proxy_chat_list_models(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """列出该 agent 网关 key 白名单内的可选模型，回投 chat_models 帧。"""
    req_id = str(frame.get("reqId") or "")
    agent_id = frame.get("agentId")
    if not isinstance(agent_id, int):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "agentId is required"})
        return
    try:
        status, data = await _agent_internal_request(
            "GET", f"/agent/client/models?agent_id={agent_id}", identity
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 取模型列表失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": f"agent service HTTP {status}"})
        return
    await _send_chat_frame(websocket, {
        "type": "chat_models",
        "reqId": req_id,
        "models": data.get("data") or [],
        "defaultModel": data.get("default_model") or "",
    })


async def _proxy_chat_list_skills(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """列出该 agent 可用的技能目录（SOP + MCP），回投 chat_skills 帧。"""
    req_id = str(frame.get("reqId") or "")
    agent_id = frame.get("agentId")
    if not isinstance(agent_id, int):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "agentId is required"})
        return
    try:
        status, data = await _agent_internal_request(
            "GET", f"/agent/client/skills?agent_id={agent_id}", identity
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 取技能列表失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": f"agent service HTTP {status}"})
        return
    await _send_chat_frame(websocket, {
        "type": "chat_skills",
        "reqId": req_id,
        "skills": data.get("skills") or [],
    })


async def _proxy_chat_list_conversations(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """列出本页面名下的会话，回投 chat_conversations 帧。"""
    req_id = str(frame.get("reqId") or "")
    try:
        status, data = await _agent_internal_request("GET", "/agent/client/conversations", identity)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 取会话列表失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": f"agent service HTTP {status}"})
        return
    await _send_chat_frame(websocket, {"type": "chat_conversations", "reqId": req_id, "conversations": data.get("conversations") or []})


async def _proxy_chat_new_conversation(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """显式新建一个空会话，回投 chat_conversation_created 帧。"""
    req_id = str(frame.get("reqId") or "")
    agent_id = frame.get("agentId")
    if not isinstance(agent_id, int):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "agentId is required"})
        return
    body = {"agent_id": agent_id, "title": str(frame.get("title") or "")}
    try:
        status, data = await _agent_internal_request("POST", "/agent/client/conversations", identity, json_body=body)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 新建会话失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": f"agent service HTTP {status}"})
        return
    await _send_chat_frame(websocket, {"type": "chat_conversation_created", "reqId": req_id, "conversation": data.get("conversation") or {}})


async def _proxy_chat_load_conversation(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """取一个会话的消息历史，回投 chat_conversation_loaded 帧。"""
    req_id = str(frame.get("reqId") or "")
    conv_id = str(frame.get("conversationId") or "").strip()
    if not conv_id:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId is required"})
        return
    # 按用户轮次分页：cursor（更早一页）/ limit 面板显式给了才带，默认服务端 7 轮。
    query = ""
    cursor = frame.get("cursor")
    limit = frame.get("limit")
    params: list[str] = []
    if cursor is not None:
        params.append(f"cursor={int(cursor)}")
    if limit is not None:
        params.append(f"limit={max(1, min(int(limit), 50))}")
    if params:
        query = "?" + "&".join(params)
    try:
        status, data = await _agent_internal_request("GET", f"/agent/client/conversations/{conv_id}{query}", identity)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 载入会话 {conv_id} 失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": f"agent service HTTP {status}"})
        return
    await _send_chat_frame(websocket, {
        "type": "chat_conversation_loaded",
        "reqId": req_id,
        "conversation": data.get("conversation") or {},
        "messages": data.get("messages") or [],
        # 翻页：面板滚到顶部加载更早时按 next_cursor 再拉。
        "page": data.get("page") or {"next_cursor": None, "has_more": False},
        # 未裁决的审批卡：审批是纯事件态、不入库，整页导航/刷新后原卡就丢了，
        # agent 却仍在 await 这个裁决。载入会话时按会话回带，让面板恢复可操作卡。
        "pending_approvals": data.get("pending_approvals") or [],
    })


async def _proxy_chat_upload_attachment(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """CDP 面板上传附件：base64 解码后 POST 原始字节到主服务，回投 chat_attachment_uploaded 帧。

    扩展面板没有 web 的登录态（web 上传走 bearer key），鉴权靠已认证 WS 连接
    的 client 身份 + 主服务的会话归属校验。10MiB 上限与服务端 _read_attachment_body
    一致，超限直接拒（base64 膨胀约 33%，帧体积按 14MiB 卡）。
    """
    import base64

    req_id = str(frame.get("reqId") or "")
    conv_id = str(frame.get("conversationId") or "").strip()
    filename = str(frame.get("filename") or "").strip()
    b64 = str(frame.get("content") or "")
    if not conv_id or not filename or not b64:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId, filename and content are required"})
        return
    if len(b64) > 14 * 1024 * 1024:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "附件不能超过 10 MiB"})
        return
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:  # noqa: BLE001
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "附件内容不是合法的 base64"})
        return
    if not raw or len(raw) > 10 * 1024 * 1024:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "附件内容为空" if not raw else "附件不能超过 10 MiB"})
        return
    from urllib.parse import quote

    try:
        status, data = await _agent_internal_request(
            "POST",
            f"/agent/client/conversations/{conv_id}/attachments?filename={quote(filename)}",
            identity, raw_body=raw, timeout_s=60,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 上传附件 {filename} 失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        detail = data.get("detail") if isinstance(data, dict) else None
        message = detail if isinstance(detail, str) and detail else f"agent service HTTP {status}"
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": message})
        return
    await _send_chat_frame(websocket, {
        "type": "chat_attachment_uploaded",
        "reqId": req_id,
        "path": data.get("path") or "",
        "filename": data.get("filename") or filename,
        "size": data.get("size") or 0,
    })


async def _proxy_chat_compact(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """CDP 面板手动压缩上下文：转发到主服务，回投 chat_compacted 帧。

    压缩要跑一次 LLM 摘要（可能数十秒），作为后台任务跑，别堵住 WS 读循环。
    """
    req_id = str(frame.get("reqId") or "")
    conv_id = str(frame.get("conversationId") or "").strip()
    if not conv_id:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId is required"})
        return
    try:
        status, data = await _agent_internal_request(
            "POST", f"/agent/client/conversations/{conv_id}/compact", identity,
            json_body={}, timeout_s=120,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 压缩会话 {conv_id} 失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        detail = data.get("detail") if isinstance(data, dict) else None
        message = detail if isinstance(detail, str) and detail else f"agent service HTTP {status}"
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": message})
        return
    await _send_chat_frame(websocket, {
        "type": "chat_compacted",
        "reqId": req_id,
        "ok": bool(data.get("ok")),
        "summarized": bool(data.get("summarized")),
        "summaryChars": int(data.get("summary_chars") or 0),
    })


async def _proxy_chat_update_settings(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """立即生效的对话级设置（mode/model/思考等级）：PATCH 转发，回投 chat_settings_ok。

    mode 是会话状态、一改就生效：切到 auto 时服务端还会把在途 pending 审批一次性
    放行（released_approvals 记放行条数，面板据此提示「已放行 n 个审批」）。
    """
    req_id = str(frame.get("reqId") or "")
    conv_id = str(frame.get("conversationId") or "").strip()
    if not conv_id:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId is required"})
        return
    body: dict = {}
    if frame.get("mode"):
        body["mode"] = str(frame["mode"])
    if frame.get("model") is not None:
        body["model"] = str(frame["model"])
    if frame.get("reasoningEffort") is not None:
        body["reasoning_effort"] = str(frame["reasoningEffort"])
    try:
        status, data = await _agent_internal_request(
            "PATCH", f"/agent/client/conversations/{conv_id}/settings", identity, json_body=body,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 更新会话设置 {conv_id} 失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400 or not isinstance(data, dict):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": f"agent service HTTP {status}"})
        return
    await _send_chat_frame(websocket, {
        "type": "chat_settings_ok",
        "reqId": req_id,
        "mode": data.get("mode"),
        "model": data.get("model"),
        "reasoningEffort": data.get("reasoning_effort"),
        "releasedApprovals": int(data.get("released_approvals") or 0),
    })


async def _proxy_chat_resolve_approval(frame: dict, identity: dict, websocket: WebSocket) -> None:
    """把面板的审批裁决转给主服务，回投 chat_approval_resolved 帧。

    裁决唤醒的是主服务里 await 着的那一轮 agent（approval_registry 单例持有
    future），本 runtime 只做转发——正在跑的 SSE 流会自己继续吐事件。
    """
    req_id = str(frame.get("reqId") or "")
    conv_id = str(frame.get("conversationId") or "").strip()
    confirmation_id = str(frame.get("confirmationId") or "").strip()
    result = str(frame.get("result") or "").strip().lower()
    command_hash = str(frame.get("commandHash") or "")
    if not conv_id or not confirmation_id:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "conversationId and confirmationId are required"})
        return
    if result not in ("allow", "deny"):
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "result must be allow or deny"})
        return
    try:
        status, data = await _agent_internal_request(
            "POST",
            f"/agent/client/conversations/{conv_id}/approvals/{confirmation_id}",
            identity,
            json_body={"result": result, "command_hash": command_hash},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[cdp-chat] 提交审批 {confirmation_id} 失败: {exc!r}")
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": _relay_failure_message(exc)})
        return
    if status is None:
        await _send_chat_frame(websocket, {"type": "chat_error", "reqId": req_id, "message": "AGENT_INTERNAL_TOKEN is not configured"})
        return
    if status >= 400:
        # 审批已被处理/过期/哈希不符：把可读原因回投，面板据此把卡片翻成终态。
        message = ""
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, str):
                message = detail.strip()
        await _send_chat_frame(websocket, {
            "type": "chat_approval_resolved",
            "reqId": req_id,
            "ok": False,
            "result": result,
            "message": message or f"审批提交失败（HTTP {status}）",
        })
        return
    await _send_chat_frame(websocket, {
        "type": "chat_approval_resolved",
        "reqId": req_id,
        "ok": True,
        "result": result,
    })


_CHAT_TASKS: dict[str, asyncio.Task] = {}


async def _send_ws_protocol(websocket: WebSocket, kind: str, **payload) -> None:
    await websocket.send_text(json.dumps({"type": kind, "protocol": 2, **payload}, ensure_ascii=False))


async def _authenticate_ext_frame(raw: str, websocket: WebSocket, driver, adapter: _ExtWsAdapter) -> bool:
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        await _send_ws_protocol(websocket, "auth_error", code="invalid_json", message="first frame must be auth protocol 2")
        return False
    if frame.get("type") != "auth" or frame.get("protocol") != 2:
        await _send_ws_protocol(websocket, "auth_error", code="protocol_required", message="auth protocol 2 required")
        return False
    token, client_id = str(frame.get("token") or ""), str(frame.get("client_id") or "")
    config = driver.authenticate_client(token, client_id or None)
    if not config:
        await _send_ws_protocol(websocket, "auth_error", code="invalid_token", message="invalid or revoked CDP client token")
        return False
    try:
        driver.bind_client(adapter, config)
    except ValueError:
        await _send_ws_protocol(websocket, "auth_error", code="already_connected", message="this token already has an active connection")
        return False
    adapter._client_alias = str(config.get("name") or config["client_id"])
    # 会话池按 client_id 隔离，CDP 客户端不再归属单个 user_id；auth_ok 只回 client_id。
    await _send_ws_protocol(
        websocket,
        "auth_ok",
        client_id=config["client_id"],
    )
    return True


async def _authenticate_proxy_frame(raw: str, websocket: WebSocket, driver, adapter: _ExtWsAdapter) -> bool:
    """代理通道握手：复用 auth protocol 2，但 bind_proxy（独立 slot，不抢控制通道）。

    与 _authenticate_ext_frame 的唯一区别是 bind_proxy 替代 bind_client——同一
    client_id 允许「控制通道 + 代理通道」两条连接并存（见 docs/browser-proxy-design.md §5.2）。
    """
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        await _send_ws_protocol(websocket, "auth_error", code="invalid_json", message="first frame must be auth protocol 2")
        return False
    if frame.get("type") != "auth" or frame.get("protocol") != 2:
        await _send_ws_protocol(websocket, "auth_error", code="protocol_required", message="auth protocol 2 required")
        return False
    token, client_id = str(frame.get("token") or ""), str(frame.get("client_id") or "")
    config = driver.authenticate_client(token, client_id or None)
    if not config:
        await _send_ws_protocol(websocket, "auth_error", code="invalid_token", message="invalid or revoked CDP client token")
        return False
    try:
        driver.bind_proxy(adapter, config)
    except ValueError:
        await _send_ws_protocol(websocket, "auth_error", code="already_connected", message="this token already has an active proxy connection")
        return False
    adapter._client_alias = str(config.get("name") or config["client_id"])
    await _send_ws_protocol(websocket, "auth_ok", client_id=config["client_id"])
    return True


@router.websocket("/{service_name}/session")
async def ext_session(websocket: WebSocket, service_name: str):
    """CDP extension WebSocket using mandatory explicit auth protocol 2.

    The first frame must be ``{"type":"auth","protocol":2,"token":...}``.
    The token determines the authoritative client ID (its ``instance_key``).
    Legacy MCP-user tokens and unauthenticated ``ext_ready`` first frames are rejected.
    """
    if service_name != "cdp-bridge":
        await websocket.close(code=4404)
        return
    plugin = registry.get(service_name)
    if not plugin or not plugin.ctx.enabled:
        await websocket.close(code=4403, reason="service disabled")
        return
    if not plugin.ctx.auth_enabled:
        await websocket.close(code=4403, reason="CDP Bridge requires MCP user authentication")
        return
    try:
        from mcp_builtin.cdp_bridge.server import get_driver
        driver = get_driver()
    except Exception:
        driver = None
    if driver is None:
        await websocket.close(code=4503)
        return

    await websocket.accept()
    loop = asyncio.get_running_loop()
    adapter = _ExtWsAdapter(websocket, loop)
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if not await _authenticate_ext_frame(first, websocket, driver, adapter):
            await websocket.close(code=4401)
            return
        while True:
            # 空闲读超时：半开连接（MV3 worker 被杀 / 网络切换 / 睡眠）不会发 TCP FIN，
            # receive_text() 会永远阻塞，WebSocketDisconnect 永不触发 → finally 里的
            # unregister_client 永不执行 → ws_client 一直非空 → 同一浏览器正常重连撞
            # already_connected 被锁死。客户端每 ~24s 发一次 ping（keepalive），故 60s
            # 内一帧都收不到即判定连接已死，主动关闭让 finally 释放会话槽。
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=_EXT_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                break
            # 收到帧=这条连接还活着；bind_client 据此判定是否抢占半开/孤儿槽位。
            adapter._last_in = time.time()
            # Re-check the persisted hash snapshot on every frame so a runtime
            # reload after revoke/rotation invalidates the live connection.
            if not adapter._token or adapter._token not in driver.token_manager.clients_by_hash:
                await _send_ws_protocol(websocket, "auth_error", code="revoked", message="client authorization revoked")
                await websocket.close(code=4403)
                return
            await _route_ext_frame(raw, websocket, adapter, driver)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 订阅清理必须先于/伴随注销：否则推送任务会继续对死 socket fire-and-forget。
        try:
            from mcp_runtime import totp_push
            totp_push.drop_adapter(adapter)
        except Exception:
            pass
        try:
            driver.unregister_client(adapter)
        except Exception:
            pass


# 代理通道空闲读超时：出口请求的流式回传在首字节/长下载期间帧间隔可能很大
# （大文件下载中途无帧是正常的），故远大于控制通道的 _EXT_IDLE_TIMEOUT=60。
# 客户端代理 socket 每 ~24s 仍发 ping，正常情况下不会触顶。
_PROXY_IDLE_TIMEOUT = 180


async def _route_proxy_frame(raw: str, websocket: WebSocket, adapter: "_ExtWsAdapter") -> None:
    """代理通道逐帧分发：只认 ping 与 proxy_*，绝不进 _route_ext_frame。

    独立读循环的意义：大块 proxy_chunk 流量不会挤到对话/CDP 帧的处理队列，
    对话链路的 lagged 必达语义不受代理流量影响。
    """
    frame_type = _frame_type(raw)
    if frame_type == "ping":
        await _send_ws_protocol(websocket, "pong")
        return
    if frame_type.startswith("proxy_"):
        await _handle_proxy_frame(raw, websocket, adapter)
        return
    # 未知帧：忽略（代理通道不处理任何业务帧）。


async def _handle_proxy_frame(raw: str, websocket: WebSocket, adapter: "_ExtWsAdapter") -> None:
    """处理扩展上行的 proxy_* 文本帧。

    - proxy_status：扩展上报当前生效态（出版面）。
    - proxy_head / proxy_end / proxy_error：数据面出口请求的回帧，喂给 BrowserConnectManager。
    chunk 是二进制帧，经 _route_proxy_binary 处理（不走这里）。
    """
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    ftype = str(frame.get("type") or "")
    if ftype == "proxy_status":
        client_id = str(adapter._client_id or "")
        if not client_id:
            return
        status = frame.get("status") or {}
        _PROXY_STATUS[client_id] = {
            "client_id": client_id,
            "applied": bool(status.get("applied")),
            "active_profile_id": str(status.get("activeProfileId") or ""),
            "active_name": str(status.get("activeName") or ""),
            "active_type": str(status.get("activeType") or ""),
            "applied_by": str(status.get("appliedBy") or ""),
            "lease_expires_at": int(status.get("leaseExpiresAt") or 0),
            "level_of_control": str(status.get("levelOfControl") or ""),
            "reported_at": time.time(),
        }
        return
    # 同步上行：扩展把本地 Profile 全量推上来，覆盖服务端镜像（client_id 以连接身份为准）。
    if ftype == "proxy_sync":
        client_id = str(adapter._client_id or "")
        req_id = str(frame.get("reqId") or "")
        if not client_id:
            return
        import mcp_builtin.browser_proxy as bp
        try:
            result = await bp.sync_profiles_from_extension(
                client_id,
                frame.get("profiles") or [],
                frame.get("deleted") or [],
            )
            await _send_ws_protocol(websocket, "proxy_sync_ack", reqId=req_id, ok=True, **result)
        except Exception as exc:
            logger.debug("proxy_sync from {} failed: {}", client_id, exc)
            await _send_ws_protocol(websocket, "proxy_sync_ack", reqId=req_id, ok=False, error=str(exc))
        return
    # 「从另一个浏览器导入」第一步：拉同 owner 域内的浏览器列表 + 各自 Profile。
    if ftype == "proxy_sources_request":
        req_id = str(frame.get("reqId") or "")
        client_id = str(adapter._client_id or "")
        if not client_id:
            return
        import mcp_builtin.browser_proxy as bp
        import mcp_plugin_store
        browsers = []
        try:
            owner = await mcp_plugin_store.get_cdp_client_owner_user_id(client_id)
            ids = await bp.list_owner_client_ids(owner) if owner else [client_id]
            grouped = await bp.list_profiles_for_client_ids(ids)
            # 附浏览器名（builtin_tool_resources.name），并标记哪台是本机。
            name_by_id = await _cdp_client_names(ids)
            for cid in ids:
                browsers.append({
                    "client_id": cid,
                    "name": name_by_id.get(cid) or cid,
                    "self": cid == client_id,
                    "profiles": grouped.get(cid, []),
                })
        except Exception as exc:
            logger.debug("proxy_sources_request failed: {}", exc)
        await _send_ws_protocol(websocket, "proxy_sources", reqId=req_id, browsers=browsers)
        return
    # 数据面回帧（Phase 3）。
    proxy_id = str(frame.get("proxyId") or "")
    if not proxy_id:
        return
    from mcp_builtin.browser_proxy import deliver_proxy_head, deliver_proxy_end, deliver_proxy_error
    if ftype == "proxy_head":
        deliver_proxy_head(proxy_id, int(frame.get("status") or 0), frame.get("headers") or [])
    elif ftype == "proxy_end":
        deliver_proxy_end(proxy_id)
    elif ftype == "proxy_error":
        deliver_proxy_error(proxy_id, str(frame.get("message") or frame.get("code") or "error"))


def _route_proxy_binary(data: bytes, adapter: "_ExtWsAdapter") -> None:
    """代理通道二进制帧 = 数据面 chunk。帧头 [1 字节 idLen][proxyId][payload]。"""
    if not data or len(data) < 2:
        return
    id_len = data[0]
    if id_len <= 0 or 1 + id_len > len(data):
        return
    try:
        proxy_id = data[1:1 + id_len].decode("utf-8", "replace")
    except Exception:
        return
    payload = data[1 + id_len:]
    from mcp_builtin.browser_proxy import deliver_proxy_chunk
    deliver_proxy_chunk(proxy_id, payload)


# 扩展代理运行态（内存，仅当前已连代理通道的 client）。管理端/Agent 只读。
_PROXY_STATUS: dict[str, dict] = {}


def get_proxy_status(client_id: str) -> dict | None:
    """取某 client 最近一次上报的代理生效态。代理 socket 断开时调用方应视为不可知。"""
    return _PROXY_STATUS.get(client_id)


def list_proxy_status() -> list[dict]:
    return list(_PROXY_STATUS.values())


# Phase 1 出版面控制：管理端改活跃 Profile 后，服务端把激活意图下发给扩展。
# 扩展自己应用（chrome.proxy）并回 proxy_status，服务端不直接持有 chrome.proxy。
async def push_proxy_activate(client_id: str, *, profile: dict, lease_ms: int = 0, force: bool = False) -> bool:
    """向指定 client 的代理通道下发「应用某 Profile」控制帧。连接在则返回 True。

    真相源在服务端 Profile 库；这里把**完整 Profile 定义**随激活意图一起下发
    （扩展 upsert 本地镜像后按 id 编译应用），保证「服务端存什么、浏览器生效什么」。
    实际 chrome.proxy 生效由扩展做。
    """
    driver = _cdp_driver()
    if driver is None:
        return False
    ws = driver.get_proxy_client(client_id)
    if ws is None:
        return False
    frame = {
        "type": "proxy_ctl",
        "action": "activate",
        "profileId": str((profile or {}).get("id") or ""),
        "profiles": [profile] if profile else [],
        "appliedBy": "admin",
        "leaseMs": int(lease_ms or 0),
        "force": bool(force),
        "protocol": 2,
    }
    ws.post_message(json.dumps(frame, ensure_ascii=False))
    return True


async def push_proxy_restore(client_id: str) -> bool:
    """向指定 client 下发「还原出网路径」控制帧。"""
    driver = _cdp_driver()
    if driver is None:
        return False
    ws = driver.get_proxy_client(client_id)
    if ws is None:
        return False
    ws.post_message(json.dumps({"type": "proxy_ctl", "action": "restore", "protocol": 2}, ensure_ascii=False))
    return True


async def push_proxy_sync_profiles(client_id: str, profiles: list) -> bool:
    """向指定 client 下发「全量 Profile 同步」控制帧。

    用于双向同步的下行：服务端 Profile 库被管理端改动后，把该 client 的全量
    列表推下去，扩展覆盖本地镜像。扩展上行同步（proxy_sync）走 _handle_proxy_frame。
    """
    driver = _cdp_driver()
    if driver is None:
        return False
    ws = driver.get_proxy_client(client_id)
    if ws is None:
        return False
    ws.post_message(json.dumps({
        "type": "proxy_ctl",
        "action": "sync_profiles",
        "profiles": profiles or [],
        "protocol": 2,
    }, ensure_ascii=False))
    return True


def _cdp_client_names(client_ids: list[str]) -> dict[str, str]:
    """读这些 client 的展示名。同步取自 driver 的 snapshot_contexts（name 在连接态里）。

    未连/已断的 client 名字读不到，回落空名（调用方用 client_id 兜底显示）。
    """
    out: dict[str, str] = {}
    try:
        driver = _cdp_driver()
        if driver is None:
            return out
        wanted = {str(c) for c in client_ids}
        for ctx_row in driver.snapshot_contexts():
            for client in ctx_row.get("clients", []):
                cid = str(client.get("client_id") or "")
                if cid in wanted:
                    out[cid] = str(client.get("name") or "")
    except Exception:
        pass
    return out


def _cdp_driver():
    try:
        from mcp_builtin.cdp_bridge.server import get_driver
        return get_driver()
    except Exception:
        return None


@router.websocket("/{service_name}/ws/proxy")
async def ext_proxy_session(websocket: WebSocket, service_name: str):
    """CDP 扩展代理出口 WebSocket（独立于 /session 控制通道）。

    协议复用 auth protocol 2，但绑定到 driver 的**代理 slot**（bind_proxy），
    因此同一 client_id 的控制通道与代理通道互不抢占。代理通道只承载
    proxy_* 数据帧与 ping（见 docs/browser-proxy-design.md §5）。
    """
    if service_name != "cdp-bridge":
        await websocket.close(code=4404)
        return
    plugin = registry.get(service_name)
    if not plugin or not plugin.ctx.enabled:
        await websocket.close(code=4403, reason="service disabled")
        return
    if not plugin.ctx.auth_enabled:
        await websocket.close(code=4403, reason="CDP Bridge requires MCP user authentication")
        return
    try:
        from mcp_builtin.cdp_bridge.server import get_driver
        driver = get_driver()
    except Exception:
        driver = None
    if driver is None:
        await websocket.close(code=4503)
        return

    await websocket.accept()
    loop = asyncio.get_running_loop()
    adapter = _ExtWsAdapter(websocket, loop)
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        if not await _authenticate_proxy_frame(first, websocket, driver, adapter):
            await websocket.close(code=4401)
            return
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=_PROXY_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                break
            # 代理通道混跑文本帧（控制/状态/head/end）与二进制帧（chunk）。
            if message.get("type") == "websocket.disconnect":
                break
            adapter._last_in = time.time()
            if not adapter._token or adapter._token not in driver.token_manager.clients_by_hash:
                await _send_ws_protocol(websocket, "auth_error", code="revoked", message="client authorization revoked")
                await websocket.close(code=4403)
                return
            text = message.get("text")
            data = message.get("bytes")
            if text is not None:
                await _route_proxy_frame(text, websocket, adapter)
            elif data is not None:
                _route_proxy_binary(data, adapter)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            driver.unregister_proxy(adapter)
        except Exception:
            pass


# ── device-control：Android App 长连端点（spec v0）─────────────────────────────
#
# 与 cdp-bridge 的 /session 不同协议：首帧是 ``register``（spec §4），鉴权是
# ``auth.scheme == "token"`` + ``builtin_tool_store.authenticate_device``，不是 CDP
# 的 auth protocol 2。帧用 spec 的 close code（4002-4010），不是 CDP 的 4xxx。
# 两套读循环刻意不共用骨架：close 码、帧类型、鉴权复查条件都不同，硬抽会扭曲。


def _device_driver():
    """device-control driver 单例取法。模块顶层 import 会触发重依赖，故延迟。"""
    from mcp_builtin.device_control import driver as dc_driver
    return dc_driver.get_driver()


@router.websocket("/{service_name}/ws/device")
async def device_ws(websocket: WebSocket, service_name: str):
    """device-control 设备 WebSocket（spec §4 握手 + §3 帧路由 + §7 空闲收割）。"""
    if service_name != "device-control":
        # 未知服务名直接拒，不让别的服务意外挂上设备端点。
        await websocket.close(code=4404)
        return
    plugin = registry.get(service_name)
    if not plugin or not plugin.ctx.enabled:
        await websocket.close(code=4403, reason="service disabled")
        return
    try:
        driver = _device_driver()
    except Exception:
        driver = None
    if driver is None:
        await websocket.close(code=4503, reason="device-control driver not loaded")
        return

    await websocket.accept()
    # [req] 中间件只记 HTTP，WebSocket 握手在日志里是黑的——排障时手机 WS 到底
    # 有没有拨进来无从判断。这里 accept 一打就记一行：远程地址 + 服务名，把 WS
    # 连接的「到没到」从日志盲区捞出来。下方 _device_register 的 auth failed /
    # registered 后续行据此串得起来。
    client_addr = "-"
    try:
        client = websocket.client
        if client is not None:
            client_addr = f"{client.host}:{client.port}"
    except Exception:
        client_addr = "?"
    logger.info(f"[device-control] ws open from {client_addr}")
    _ws_opened_at = asyncio.get_event_loop().time()
    ctx = None
    try:
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=proto.REGISTER_TIMEOUT_S)
        except asyncio.TimeoutError:
            # spec §4.1：10s 内没收到 register → close 4008。区别于「发了坏帧」的协议错误。
            logger.info(f"[device-control] ws end {client_addr} close=4008 (no register within 10s)")
            await websocket.close(code=proto.CLOSE_REGISTER_TIMEOUT, reason="no register within deadline")
            return
        if len(first.encode("utf-8")) > proto.MAX_FRAME_BYTES:
            await websocket.close(code=proto.CLOSE_FRAME_TOO_LARGE, reason="frame exceeds 4MiB")
            return
        ctx = await _device_register(first, websocket, driver)
        if ctx is None:
            logger.info(f"[device-control] ws end {client_addr} (register rejected)")
            return  # 握手失败已按 spec close
        await driver.register(ctx)
        await _device_read_loop(websocket, ctx, driver)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[device-control] ws error: {exc}")
    finally:
        # 收尾日志：存活时长 + 是否进过 register。串起 ws open → (auth failed|registered)
        # → ws end 三段，手机拨号在哪一跳断的，服务端日志即可定位。
        _alive = asyncio.get_event_loop().time() - _ws_opened_at
        _tag = getattr(ctx, "device_id", None) or client_addr
        logger.info(f"[device-control] ws end {_tag} alive={_alive:.1f}s")
        if ctx is not None:
            # 旧连接的 read loop 走到这里收尾；unregister 仅在 ctx 仍是当前登记的那条时摘。
            driver.unregister(ctx)
            # 幂等收尾：把残留 pending future 失败掉（断连置异常）。close 已断的连接是 no-op。
            try:
                await ctx.close(proto.CLOSE_STALE, "connection ended")
            except Exception:
                pass
            # 断连即落库 last_seen：网页据此展示「最后在线」，不依赖 worker 内存
            # 快照。last_error 不在这里造——app 下次 register 会带人话断开原因
            # （网络错误/心跳超时/…）全量覆盖，比服务端猜 close code 准。
            # 失败只记日志——收尾路径绝不能反过来把连接清理也拖崩。
            try:
                import builtin_tool_store
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                await builtin_tool_store.update_device_runtime(
                    ctx.resource_id,
                    last_seen_at=now,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[device-control] persist disconnect state failed: {exc}")


async def _device_register(raw: str, websocket: WebSocket, driver) -> Any:
    """处理 register 首帧（spec §4）。返回 DeviceContext 或 None（已 close）。"""
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        await websocket.close(code=1002, reason="first frame must be valid JSON")
        return None
    if not isinstance(frame, dict) or frame.get("type") != proto.TYPE_REGISTER:
        await websocket.close(code=1002, reason="first frame must be register")
        return None

    # 协议版本是服务端权威（spec §14）。不假定兼容；不匹配直接 close 4004。
    pv = frame.get("protocol_version")
    if not isinstance(pv, int) or pv != proto.VERSION:
        await websocket.close(code=proto.CLOSE_VERSION_UNSUPPORTED, reason="unsupported protocol_version")
        return None

    auth = frame.get("auth")
    if not isinstance(auth, dict) or auth.get("scheme") != proto.SCHEME_TOKEN:
        # 未知 scheme 当鉴权失败（spec §11），不是版本失败。
        await websocket.close(code=proto.CLOSE_AUTH_FAILED, reason="unsupported auth scheme")
        return None
    device_id = str(frame.get("device_id") or "").strip()
    token = str(auth.get("token") or "").strip()
    if not device_id or not token:
        await websocket.close(code=proto.CLOSE_AUTH_FAILED, reason="invalid credentials")
        return None

    # authenticate_device 走 sha256 常量时间比对；不匹配返回 None → 4003。
    import builtin_tool_store
    detail = await builtin_tool_store.authenticate_device(device_id, token)
    if detail is None:
        logger.info(f"[device-control] auth failed: device_id={device_id}")
        await websocket.close(code=proto.CLOSE_AUTH_FAILED, reason="invalid credentials")
        return None

    caps_raw = frame.get("capabilities")
    capabilities = [str(c) for c in caps_raw if isinstance(c, str)] if isinstance(caps_raw, list) else []
    device_info = frame.get("device_info") if isinstance(frame.get("device_info"), dict) else {}
    # detail 是 _resource_value 的扁平化 dict，顶层带 id（builtin_tool_resources.id）。
    # 断连/device_status 事件落库运行态时要按这行定位。
    resource_id = detail.get("id")

    session_id = proto.new_id("ses_")
    send_lock = asyncio.Lock()

    async def send(frame: dict) -> None:
        # 串行化 send_text：并发下发 call + call-cancel 会交错，Starlette 不保证并发安全。
        async with send_lock:
            await websocket.send_text(json.dumps(frame, ensure_ascii=False))

    async def close(code: int, reason: str) -> None:
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    ctx = DeviceContext(
        device_id=device_id,
        session_id=session_id,
        token_hash=str(detail.get("token_hash") or ""),
        capabilities=capabilities,
        device_info=device_info,
        send=send,
        close=close,
        resource_id=int(resource_id) if resource_id is not None else None,
    )
    # spec §4：认证通过后回 registered 帧，把协商参数（心跳间隔/超时、session_id、
    # 接受的能力）下发给设备。设备收到后才认为握手完成、可开始收 call。
    from datetime import datetime, timezone
    await send(proto.registered_frame(
        device_id=device_id,
        session_id=session_id,
        server_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        accepted_capabilities=capabilities,
    ))
    # register 成功即把 app 上报的 device_info（版本/系统/机型/无障碍开关/上次
    # 断连原因）落库：driver 内存快照只活在 worker 进程里，落库后设备离线/服务重启
    # 网页仍能展示。app 端每次重连现造，这里全量覆盖即可。失败只记日志——握手已
    # 成功，不能因为落库抖动把设备踢掉。
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await builtin_tool_store.update_device_runtime(
            int(resource_id),
            device_info=device_info,
            last_seen_at=now,
            last_error_at=now,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[device-control] persist register state failed: {exc}")
    return ctx


async def _device_read_loop(websocket: WebSocket, ctx, driver) -> None:
    """设备帧路由循环（spec §3、§7）。"""
    try:
        while True:
            # 空闲读超时：安卓半开连接（doze / 蜂窝↔WiFi 切换 / 进程被后台回收）不发 TCP FIN，
            # receive_text 会永久阻塞。心跳 15s 一次，60s 内一帧都收不到即判定连接已死，
            # close 4010 触发 driver.register 收尾（pending future 置 DeviceOffline）。
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=proto.HEARTBEAT_TIMEOUT_S)
            except asyncio.TimeoutError:
                await ctx.close(proto.CLOSE_STALE, "heartbeat timeout")
                return
            # spec §2：4 MiB 帧上限。超了 close 4002，别让恶意/异常帧撑爆内存。
            if len(raw.encode("utf-8")) > proto.MAX_FRAME_BYTES:
                await ctx.close(proto.CLOSE_FRAME_TOO_LARGE, "frame exceeds 4MiB")
                return
            # 任意帧都刷新活性（spec §7），含 heartbeat / call-response / event。
            ctx.touch()
            # 每帧复查授权快照：用户在前端解除配对/轮换 token 后，活连接当场断（close 4003），
            # 不等心跳超时。与 cdp 的 clients_by_hash 复查同一思路。
            if not driver.is_authorized(ctx.token_hash):
                await ctx.close(proto.CLOSE_AUTH_FAILED, "credential revoked")
                return
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue  # 坏帧忽略：不要因为一条坏 JSON 断掉整条设备连接
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            if ftype == proto.TYPE_HEARTBEAT:
                continue  # spec §10：心跳无 ack，Touch 已记活性
            if ftype == proto.TYPE_CALL_RESPONSE:
                ctx.deliver(
                    str(frame.get("request_id") or ""),
                    bool(frame.get("ok")) if isinstance(frame.get("ok"), bool) else None,
                    frame.get("data"),
                    frame.get("error") if isinstance(frame.get("error"), dict) else None,
                )
            elif ftype == proto.TYPE_EVENT:
                await _device_handle_event(frame, ctx)
            elif ftype == proto.TYPE_REGISTER:
                # 一条连接上的第二次 register 是致命错误（spec §4.3）。
                await ctx.close(proto.CLOSE_DUPLICATE_REGISTER, "duplicate register")
                return
            # 未知 type：忽略（spec §3 前向兼容铰链），不当错误。
    except WebSocketDisconnect:
        pass


async def _device_handle_event(frame: dict, ctx) -> None:
    """处理设备上报事件（spec §9）。"""
    kind = str(frame.get("kind") or "")
    if kind == proto.EVENT_CAPABILITIES_CHANGED:
        caps = frame.get("capabilities")
        if isinstance(caps, list):
            ctx.set_capabilities([str(c) for c in caps if isinstance(c, str)])
    elif kind == proto.EVENT_DEVICE_STATUS:
        # app 会话内上报的设备态（无障碍开关变化等）：merge 进 ctx.device_info
        # （网页走内存快照立即看到）+ 落库（离线/重启后仍在）。未知字段忽略。
        data = frame.get("data")
        if isinstance(data, dict):
            info = dict(ctx.device_info or {})
            info.update({str(k): v for k, v in data.items()})
            ctx.device_info = info
            try:
                import builtin_tool_store
                if ctx.resource_id is not None:
                    await builtin_tool_store.update_device_runtime(
                        ctx.resource_id, device_info=info,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[device-control] persist device_status failed: {exc}")
    elif kind == proto.EVENT_CONTROL_REVOKED:
        # 设备侧主动关控制权（spec §9）：当不可控直到下次 register。
        await ctx.close(1000, "control revoked on device")
    # 未知 event kind：忽略（spec §9）。


@router.post("/device-control/pair")
async def device_pair(request: Request) -> dict:
    """兑换配对码为长期设备凭据（spec §11）。

    无鉴权——配对码本身就是凭证。返回 ``{device_id, token, protocol_version}``，
    token 明文只此一次返回；DB 只存 sha256。403 对「不存在 / 过期 / 已用」不做区分
    （spec 要求），App 据此统一提示。

    body 可带 ``device_info``（App 配对时一并上报的本机信息：app 版本 / 系统 / 机型 /
    无障碍开关 / 内网 IP）。落到设备记录的 device_info——网页在配对完成那一刻就能
    展示手机信息，不必等 WS register；WS register 时会再刷新一次（带上 last_error）。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid JSON body")
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "code is required")
    device_info = body.get("device_info") if isinstance(body.get("device_info"), dict) else None

    import builtin_tool_store
    from mcp_builtin.device_control import store as dc_store
    try:
        payload, label = await dc_store.redeem_pairing_code(code)
    except dc_store.UnknownCodeError:
        # 不区分 unknown/expired/used（spec §11）：App 端 PairingClient 已按此实现。
        raise HTTPException(403, "unknown or expired pairing code")
    except dc_store.RedisUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc

    # 兑换成功才签发凭据；配对码此时已被 Redis GETDEL 消费（单次使用）。
    # payload 带 owner 前缀（"owner:<uid>"）——资源模型里设备直接归属用户。
    owner_part, _, instance_part = str(payload).partition("|")
    if owner_part.startswith("owner:"):
        owner_user_id = owner_part[len("owner:"):]
        if instance_part.isdigit() and int(instance_part) != 0:
            # 兼容旧 payload：数值部分曾是实例 id，改用该实例 owner。
            legacy = await builtin_tool_store.get_resource(int(instance_part))
            if legacy is not None:
                owner_user_id = str(legacy.get("owner_user_id") or owner_user_id)
        resource, device_id, token = await builtin_tool_store.create_device(
            owner_user_id, label, device_info=device_info,
        )
    else:
        # 旧 payload 是裸实例 id——迁移前签出的码仍要能兑换。
        resource, device_id, token = await builtin_tool_store.create_device(
            instance_part or "0", label, device_info=device_info,
        )
    # 把新凭据立刻推给活着的 driver，否则刚配对的设备要等下次 admin reload 才能连上。
    driver = _device_driver()
    if driver is not None:
        driver.apply_config(await builtin_tool_store.device_authorized_hashes())
    logger.info(f"[device-control] device paired: device_id={device_id}")
    return {
        "device_id": device_id,
        "token": token,
        "protocol_version": proto.VERSION,
    }

