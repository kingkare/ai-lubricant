"""CDP Bridge built-in adapter.

Bridges the vendored ``mcp_builtin.cdp_bridge`` capability into the MCP
runtime registry. The upstream package is a FastMCP server that talks to a
real Chrome session through a companion extension over WebSocket. Instead of
spawning it as a stdio subprocess, we import its tool functions directly and
re-register them with the runtime's ``PluginRegistrar`` so they are callable
through the SSE gateway like any other loaded plugin.

The Chrome-extension WebSocket endpoint is owned by ``mcp_runtime`` and uses
mandatory auth protocol 2. Activating this adapter only creates the in-process
driver state; it never starts standalone HTTP/WebSocket listeners.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Awaitable, Callable

from mcp_runtime.plugin_loader import PluginContext, PluginLoadError, PluginRegistrar

# 缓存一次 vendored cdp-bridge 的函数表；可选依赖缺失时置错误原因，register/apply 时抛出。
_CDP_TOOLS: dict[str, Callable] | None = None
_CDP_IMPORT_ERROR: str | None = None


def _load_cdp_bridge_tools() -> dict[str, Callable]:
    """Import vendored cdp-bridge once and cache it.

    The registry imports this adapter at module load even if optional browser
    bridge dependencies are missing (heavy deps only surface here). On missing
    deps we raise PluginLoadError so the UI shows a clear reason.
    """
    global _CDP_TOOLS, _CDP_IMPORT_ERROR
    if _CDP_TOOLS is not None:
        return _CDP_TOOLS
    if _CDP_IMPORT_ERROR is not None:
        raise PluginLoadError(_CDP_IMPORT_ERROR)
    try:
        from mcp_builtin.cdp_bridge.server import (
            apply_config,
            browser_batch,
            browser_close_tab,
            browser_execute_js,
            browser_fill_credential,
            browser_focus_tab,
            browser_get_tabs,
            browser_navigate,
            browser_network_get,
            browser_network_start,
            browser_network_stop,
            browser_open_tab,
            browser_scan,
            browser_screenshot,
            browser_switch_tab,
            browser_totp_code,
            browser_wait,
            init_driver,
        )
    except ModuleNotFoundError as exc:
        missing = exc.name or str(exc)
        _CDP_IMPORT_ERROR = (
            f"cdp-bridge 运行依赖缺失: {missing}. "
            "请安装 simple-websocket-server、bottle、beautifulsoup4 后再启动。"
        )
        raise PluginLoadError(_CDP_IMPORT_ERROR) from exc
    _CDP_TOOLS = {
        "browser_get_tabs": browser_get_tabs,
        "browser_scan": browser_scan,
        "browser_execute_js": browser_execute_js,
        "browser_switch_tab": browser_switch_tab,
        "browser_focus_tab": browser_focus_tab,
        "browser_open_tab": browser_open_tab,
        "browser_close_tab": browser_close_tab,
        "browser_batch": browser_batch,
        "browser_wait": browser_wait,
        "browser_navigate": browser_navigate,
        "browser_screenshot": browser_screenshot,
        "browser_totp_code": browser_totp_code,
        "browser_fill_credential": browser_fill_credential,
        "browser_network_start": browser_network_start,
        "browser_network_get": browser_network_get,
        "browser_network_stop": browser_network_stop,
        "init_driver": init_driver,
        "apply_config": apply_config,
    }
    return _CDP_TOOLS


def _driver_config(resources: dict | None, auth_enabled: bool, allowed_tokens) -> dict:
    """Translate materialized CDP client hashes into live driver configuration.

    会话池按 client_id（instance_key）隔离，认证/绑定只需 token_hash + client_id；
    「可操作用户」(user_ids) 不再决定会话归属，仅用于 token→client 路由与服务级
    连接鉴权，因此这里不要求 user_id。保留 user_ids 供配置检视。
    """
    resources = resources or {}
    clients = []
    for item in resources.get("clients") or []:
        if not item.get("enabled", True) or not item.get("token_hash"):
            continue
        if not (item.get("instance_key") or item.get("id")):
            continue
        clients.append(dict(item))
    return {"clients": clients}


async def _principal_cdp_client_ids(token: str) -> list[str] | None:
    """把 token 解析到其 principal 绑定的 CDP 客户端**会话池隔离键**集（读 principal
    的 cdp_client_id grants，多行=多实例）。

    principal kind 直取；identity kind（agent/task）经 mcp_plugin_store.
    resolve_identity_to_principal 反查（公共实现，替掉本插件原先只认 agent 的
    手写版）。没绑任何 cdp_client_id grant 返回 None（交给后续外部 token /
    owner 全量路径）。

    grant_value 存的是资源行 id（与 device/mail 的 param 行同口径，前端实例
    选择器也按行 id 勾选），而会话池按客户端的对外身份隔离（新客户端=随机
    client_id），故这里逐条翻译成会话键。
    """
    import mcp_plugin_store

    principal_id = await mcp_plugin_store.resolve_identity_to_principal(token)
    if principal_id is None:
        return None
    values = await mcp_plugin_store.get_principal_grant_values(principal_id, "cdp_client_id")
    if not values:
        return None
    return [await mcp_plugin_store.cdp_client_session_key(v) for v in values]


async def _principal_cdp_client_id(token: str) -> str | None:
    """单值兼容读法：取首个绑定的 CDP 客户端 id（多绑定场景下由调用方显式选）。"""
    values = await _principal_cdp_client_ids(token)
    return values[0] if values else None


async def _owner_cdp_client_ids(token: str) -> list[str] | None:
    """未绑实例时的默认全量回退：token 身份对应的 owner 名下全部 enabled 的
    cdp_client 的会话池隔离键。解析不出身份/owner（external 平台 principal 等）返回
    None——那条路没有「默认全量」可言，交由外部 token 路径兜底。"""
    import builtin_tool_store

    owner = await builtin_tool_store.resolve_token_owner_user_id(token)
    if not owner:
        return None
    resources = await builtin_tool_store.list_resources(
        resource_type="cdp_client", owner_user_id=owner, enabled=True,
    )
    ids = [builtin_tool_store.cdp_client_id_of(r) for r in resources]
    return [c for c in ids if c] or None


async def _authorised_client_ids(
    token: str, driver, allowed_client_ids: tuple[str, ...] = (),
) -> list[str]:
    """The set of CDP clients this call may operate — the authorisation domain.

    Priority:
    1. ``allowed_client_ids`` — the server-side boundary a scene binding injects
       (a browser conversation is bound to exactly one browser). Authoritative.
    2. A CDP **connection** token authenticates as one specific browser.
    3. External MCP token: the principal's ``cdp_client_id`` grants, else the
       owner's full enabled set, else a token bound to one resource.

    Returns an ordered list (possibly empty). Callers must treat "more than one
    and nothing chosen" as a refusal, never as "pick the first".
    """
    if allowed_client_ids:
        return [str(c) for c in allowed_client_ids if str(c)]
    if driver is not None:
        config = driver.authenticate_client(token)
        if config and config.get("client_id"):
            return [str(config["client_id"])]
    try:
        bound = await _principal_cdp_client_ids(token)
    except Exception:
        bound = None
    if bound is not None:
        return [str(c) for c in bound]
    try:
        all_ids = await _owner_cdp_client_ids(token)
    except Exception:
        all_ids = None
    if all_ids:
        return [str(c) for c in all_ids]
    import builtin_tool_store

    one = await builtin_tool_store.resolve_external_cdp_client(token)
    return [str(one)] if one else []


async def _resolve_cdp_client_id(
    token: str,
    driver,
    *,
    allowed_client_ids: tuple[str, ...] = (),
    requested_client_id: Any = None,
    bound_session_key: str = "",
) -> str:
    """Pick the one CDP client this call drives, inside the authorisation domain.

    The domain comes from :func:`_authorised_client_ids`. The choice, in order:
    an explicit ``requested_client_id``, else the bound session key's prefix, else
    the sole client when the domain has exactly one. A request outside the domain
    — or an ambiguous domain with nothing chosen — is refused, **never** silently
    resolved to the first client. A CDP connection token (the browser
    authenticating as itself) is authoritative and short-circuits the choice, but
    is still checked against the boundary so a scene wiring bug cannot drive
    another browser.
    """
    requested = str(requested_client_id or "").strip()

    # A connection token resolves to its own browser — authoritative.
    if driver is not None:
        config = driver.authenticate_client(token)
        if config and config.get("client_id"):
            client_id = str(config["client_id"])
            if allowed_client_ids and client_id not in allowed_client_ids:
                raise ValueError(
                    f"CDP connection token resolves to client {client_id}, outside this "
                    f"call's authorisation boundary"
                )
            return client_id

    authorised = await _authorised_client_ids(token, driver, allowed_client_ids)
    if not authorised:
        raise ValueError("MCP token is not authorized to operate any CDP client")
    if not requested and bound_session_key and ":" in bound_session_key:
        requested = bound_session_key.split(":", 1)[0]
    if not requested and len(authorised) == 1:
        requested = authorised[0]
    if not requested or requested not in authorised:
        raise ValueError(
            f"CDP client {requested or '(none)'} is not in this call's authorisation "
            f"domain {authorised}"
        )
    return requested


def _wrap(fn: Callable[..., Awaitable[str]]) -> Callable[[dict, PluginContext], Awaitable[Any]]:
    """Adapt a cdp-bridge tool function to the runtime's (args, ctx) signature.

    cdp-bridge tool functions are ``async def fn(**kwargs) -> str`` returning a
    JSON string. The runtime calls handlers as ``fn(args: dict, ctx)``. We
    forward matching kwargs and parse the JSON string into a dict so the SSE
    gateway can shape it as MCP content.

    此外把 runtime 通用 current_request_token 桥接到 cdp 的 current_token，把请求 token
    解析成要驱动的 CDP 客户端 client_id（会话池隔离键），并以
    ``current_cdp_client_ids``（服务端注入的授权边界：场景绑定的那个浏览器，或 plain
    agent 的授权集）作为硬边界——模型在域内才放行，跨浏览器一律拒绝。

    session 透传：网页对话/Agent MCP 调用会把要操作的标签页写进
    ``current_cdp_session_id``（进程内 ContextVar，同 task 自然透传）。本桥把它设到
    cdp 的 ``current_session_id``，``execute_js`` 据此解析目标 tab。
    """
    from mcp_builtin.cdp_bridge import server as cdp_server
    from mcp_builtin.cdp_bridge.server import current_token as cdp_current_token
    from mcp_builtin.cdp_bridge.TMWebDriver import current_session_id as cdp_current_session_id
    from mcp_runtime.plugin_loader import (
        current_cdp_client_ids,
        current_cdp_session_id,
        current_request_token,
    )

    async def handler(args: dict, ctx: PluginContext) -> Any:
        token = current_request_token.get("")
        driver = cdp_server.driver
        allowed = current_cdp_client_ids.get(())
        bound_session = current_cdp_session_id.get("")
        client_id = await _resolve_cdp_client_id(
            token, driver,
            allowed_client_ids=allowed,
            requested_client_id=(args or {}).get("client_id"),
            bound_session_key=bound_session,
        )
        requested_session = str(
            (args or {}).get("session_id")
            or (args or {}).get("switch_tab_id")
            or (args or {}).get("tab_id")
            or bound_session
            or ""
        )
        if requested_session and ":" not in requested_session:
            requested_session = f"{client_id}:{requested_session}"
        # If the resolved session names another client, the request crossed the
        # authorisation boundary — refuse rather than drive another browser.
        if requested_session and ":" in requested_session:
            sess_client = requested_session.split(":", 1)[0]
            if sess_client != client_id and (not allowed or sess_client not in allowed):
                raise ValueError(
                    f"CDP session {requested_session} is outside this call's authorisation boundary"
                )
        tok = cdp_current_token.set(str(client_id))
        sess_tok = cdp_current_session_id.set(requested_session)
        try:
            kwargs = {k: v for k, v in (args or {}).items() if k in fn.__code__.co_varnames}
            result = await fn(**kwargs)
        finally:
            cdp_current_session_id.reset(sess_tok)
            cdp_current_token.reset(tok)
        if isinstance(result, str):
            try:
                return json.loads(result)
            except Exception:
                return {"text": result}
        return result

    return handler


# Tool name -> (function key, description, JSON Schema params). Function objects are
# resolved lazily in register() so importing this adapter does not require the
# optional browser bridge dependencies.
_TOOLS: list[tuple[str, str, str, dict]] = [
    (
        "browser_get_tabs",
        "browser_get_tabs",
        "List all connected browser tabs with their IDs, URLs, and titles.",
        {"type": "object", "properties": {}},
    ),
    (
        "browser_scan",
        "browser_scan",
        "Return optimized HTML/text of the active browser tab plus the tab list.",
        {
            "type": "object",
            "properties": {
                "tabs_only": {"type": "boolean", "default": False},
                "switch_tab_id": {"type": "string", "default": ""},
                "text_only": {"type": "boolean", "default": False},
            },
        },
    ),
    (
        "browser_execute_js",
        "browser_execute_js",
        "Execute JavaScript in the browser and capture the returned value, plus (by "
        "default) the page's DOM changes. Do not use this merely to read the current "
        "URL or title — browser_get_tabs already returns every tab's url/title and the "
        "active tab. For a read-only probe pass no_monitor=true to skip the whole-page "
        "HTML diff.",
        {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "switch_tab_id": {"type": "string", "default": ""},
                "no_monitor": {"type": "boolean", "default": False},
            },
            "required": ["script"],
        },
    ),
    (
        "browser_switch_tab",
        "browser_switch_tab",
        "Switch the active MCP browser tab without changing the visible Chrome tab.",
        {"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]},
    ),
    (
        "browser_focus_tab",
        "browser_focus_tab",
        "Bring a Chrome tab to the foreground: activate the tab and focus its window.",
        {"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]},
    ),
    (
        "browser_open_tab",
        "browser_open_tab",
        "Open a new tab, or a new browser window, at a URL. The new tab needs a moment "
        "to register itself, so its ID may not appear in browser_get_tabs immediately.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "default": ""},
                "new_window": {"type": "boolean", "default": False},
                "active": {"type": "boolean", "default": True},
            },
        },
    ),
    (
        "browser_close_tab",
        "browser_close_tab",
        "Close a browser tab by its ID (from browser_get_tabs). Use it to clean up a "
        "tab you opened once you are done with it, or to evict a stuck / logged-out "
        "tab before opening a replacement. Do NOT close the tab this conversation is "
        "bound to unless the user asked you to — later operations on a closed tab_id "
        "will fail. Closing the last tab of a window closes the window.",
        {"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]},
    ),
    (
        "browser_batch",
        "browser_batch",
        "Run multiple extension/CDP commands in one request.",
        {
            "type": "object",
            "properties": {
                "commands": {"type": "array", "items": {"type": "object"}},
                "tab_id": {"type": "string", "default": ""},
                "timeout": {"type": "number", "default": 20},
            },
            "required": ["commands"],
        },
    ),
    (
        "browser_wait",
        "browser_wait",
        "Wait until a JavaScript condition returns a truthy value.",
        {
            "type": "object",
            "properties": {
                "condition_js": {"type": "string"},
                "timeout": {"type": "number", "default": 10},
                "interval": {"type": "number", "default": 0.5},
                "switch_tab_id": {"type": "string", "default": ""},
            },
            "required": ["condition_js"],
        },
    ),
    (
        "browser_navigate",
        "browser_navigate",
        "Navigate the active tab to a URL.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
    ),
    (
        "browser_screenshot",
        "browser_screenshot",
        "Take a screenshot of the active tab. The platform Agent runtime intercepts "
        "the result and spills the PNG bytes to a workspace file before the model "
        "ever sees them, so the returned `path` is the only way to reference the "
        "image — never read or inline `base64`. Pass `path` to `ask_user` "
        "attachments to show the user.",
        {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "default": ""},
                "path": {
                    "type": "string",
                    "description": (
                        "Destination workspace path for the PNG; defaults to "
                        "workspace/screenshots/shot_<timestamp>.png. The returned "
                        "result carries this path — the bytes are never inlined."
                    ),
                },
            },
        },
    ),
    # 网络监听三件套的对外名带 watch/read/unwatch 动词——原名 network_start/get/stop
    # 里 "start" 会被模型读成「发起一个网络请求」，且看不出「这是个持续监听、要记得
    # unwatch 的状态机」，于是模型宁可在页面里手写 JS hook fetch/XHR 抓包。
    # fn_key 仍是服务器函数原名（_CDP_TOOLS 的键），只改 advertised name，
    # 不动 SiteServer 等按属性直调 cdp_server.browser_network_* 的调用方。
    (
        "browser_watch_network",
        "browser_network_start",
        "Start LISTENING to everything this tab sends and receives over the network: "
        "HTTP(S) requests AND WebSocket frames AND SSE event-stream messages, each with "
        "URL, method, request/response headers, status, timing, and the full response "
        "body / WS frame payload. This is the way to observe a page's traffic — do NOT "
        "hand-write JS in browser_execute_js to hook fetch/XHR/WebSocket.prototype: "
        "page-level hooks cannot see pre-existing connections or cross-origin requests, "
        "are bypassed by some code, and pollute the page. CDP sees all of it natively. "
        "Keeps the debugger attached and a red 'watching' banner on the page until "
        "browser_unwatch_network is called; read incrementally with browser_read_network.",
        {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "default": ""},
                "url_pattern": {"type": "string", "default": ""},
            },
        },
    ),
    (
        "browser_read_network",
        "browser_network_get",
        "Read captured network traffic WITHOUT stopping the watch. Returns a SUMMARY by "
        "default: one row per request (requestId, method, url, status, mimeType, size, "
        "duration) plus WS/SSE frame summaries — no bodies, no headers. Then drill in "
        "with search (url substring), request_id (full request: headers + response "
        "body), or frame_index (one WS/SSE frame payload). Poll it repeatedly while "
        "watching to see traffic as it arrives.",
        {"type": "object", "properties": {
            "tab_id": {"type": "string", "default": ""},
            "search": {"type": "string", "default": "",
                       "description": "Only rows whose URL contains this substring."},
            "request_id": {"type": "string", "default": "",
                           "description": "Return the full record (headers + response body) of this one request."},
            "frame_index": {"type": "integer", "default": -1,
                            "description": "Return the full payload of this WS/SSE frame (index in the summary)."},
        }},
    ),
    (
        "browser_unwatch_network",
        "browser_network_stop",
        "Stop the network watch on a tab, detach the debugger, hide the banner, and "
        "return everything captured (requests + wsFrames). Always call this when done — "
        "an abandoned watch keeps the debugger attached and the banner visible to the user.",
        {"type": "object", "properties": {"tab_id": {"type": "string", "default": ""}}},
    ),
    (
        "browser_fill_credential",
        "browser_fill_credential",
        "Type a saved credential (2FA code or password) INTO the page — it is NEVER "
        "returned to you. Pass `kind` ('otp' for a 2FA/verification code, 'password' for "
        "a login password) plus the login page `url` (or bare `domain`) and, when several "
        "entries exist, the `account`. The value is resolved server-side, handed to the "
        "browser extension, and typed straight into the field — it never appears in this "
        "result, the conversation, or any log. You get back a receipt only (status, "
        "filled, field, entry, expiresAt for otp). Use this whenever a login form "
        "asks for a password or a 2FA code. Do NOT use it to *read* a code — that is "
        "browser_totp_code. Pass `selector` only if the field must be pinned explicitly.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["otp", "password"],
                         "description": "'otp' = 2FA/verification code; 'password' = login password."},
                "url": {"type": "string", "default": "",
                        "description": "Login page URL; its hostname(+port) selects the matching credential."},
                "domain": {"type": "string", "default": "",
                           "description": "Bare domain to match when no URL is at hand (e.g. github.com)."},
                "account": {"type": "string", "default": "",
                            "description": "Account/username to disambiguate when the site has several saved entries."},
                "selector": {"type": "string", "default": "",
                             "description": "Optional CSS selector to pin the target field explicitly."},
                "tab_id": {"type": "string", "default": ""},
            },
            "required": ["kind"],
        },
    ),
    (
        "browser_totp_code",
        "browser_totp_code",
        "Get the current two-factor (TOTP) verification code for a site the user has "
        "saved in their authenticator. Pass the login page `url` (its hostname is "
        "matched against the entry's linked domains) or a bare `domain`. Returns one or "
        "more entries with `code` + `expiresAt` (absolute epoch seconds when the code "
        "rotates). Use it only when you must *read* "
        "a code yourself — to fill a login form, prefer browser_fill_credential, which "
        "keeps the code out of your context. If the result is empty, tell the user to "
        "add the site's QR in the extension popup (🛡 icon) or the web console — do NOT "
        "invent a code.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "default": "",
                        "description": "Login page URL; its hostname(+port) is matched against linked sites."},
                "domain": {"type": "string", "default": "",
                           "description": "Bare domain to match when no URL is at hand (e.g. github.com)."},
            },
        },
    ),
]


async def _sessions_view(_args: dict, ctx: PluginContext) -> dict:
    from mcp_builtin.cdp_bridge import server as cdp_server
    driver = cdp_server.driver
    service_id = ctx.resources.get("service_id")
    if driver is None:
        return {"service_id": service_id, "contexts": [], "note": "driver 未初始化"}

    contexts = driver.snapshot_contexts()
    return {"service_id": service_id, "contexts": contexts}


def cdp_builtin_state() -> list[dict]:
    """返回 CDP 实时会话快照（供管理端经 registry 取数，不直读 driver 单例）。

    driver 未初始化或缺依赖时返回空列表。这是 cdp-bridge 对外暴露的**唯一**运行态
    读取入口——管理端 /api_builtin_tools 不再 ``from mcp_builtin.cdp_bridge import server``
    直抓全局 driver，改走 registry.get_builtin_state("cdp-bridge")，保证读到的永远是
    registry 当前持有的那个插件实例的状态（与 SSE 网关实际驱动的 driver 一致）。
    """
    try:
        from mcp_builtin.cdp_bridge import server as cdp_server
    except ModuleNotFoundError:
        return []
    driver = cdp_server.driver
    if driver is None:
        return []
    return driver.snapshot_contexts()


async def _browser_proxy_status(args: dict, ctx: PluginContext) -> dict:
    """Read-only: 这台浏览器当前走哪出网。

    只读工具——**不给 Agent 任何改出网路径的能力**（apply/restore 只归 popup 与
    管理端，见 docs/browser-proxy-design.md §1/§5.5）。返回的是扩展经代理通道上报
    的最近生效态（sse_gateway._PROXY_STATUS），加代理通道是否连通。
    """
    from mcp_runtime.plugin_loader import current_cdp_client_ids, current_request_token

    token = current_request_token.get("")
    driver = None
    try:
        from mcp_builtin.cdp_bridge import server as cdp_server
        driver = cdp_server.driver
    except Exception:
        pass
    allowed = current_cdp_client_ids.get(())
    ids = await _authorised_client_ids(token, driver, allowed)
    bound = ""
    # session_key 形如 "<client_id>:<tab_id>"，本工具按绑定 client 默认，域内多台需显式 client_id。
    try:
        from mcp_runtime.plugin_loader import current_cdp_session_id
        sk = current_cdp_session_id.get("")
        if sk and ":" in sk:
            bound = sk.split(":", 1)[0]
    except Exception:
        pass
    client_id = str(args.get("client_id") or "").strip()
    if not client_id:
        if bound and bound in ids:
            client_id = bound
        elif len(ids) == 1:
            client_id = ids[0]
        else:
            # 多台可选且未指定：绝不 bound[0]，直接要求显式 client_id。
            return {"error": "multiple_browsers_available", "client_ids": ids,
                    "hint": "pass client_id explicitly"}
    if client_id not in ids:
        return {"error": "client_not_in_scope", "client_id": client_id}

    from mcp_runtime.sse_gateway import get_proxy_status
    status = get_proxy_status(client_id)
    proxy_connected = False
    if driver is not None:
        try:
            proxy_connected = driver.get_proxy_client(client_id) is not None
        except Exception:
            proxy_connected = False
    if not status:
        return {"client_id": client_id, "proxy_connected": proxy_connected,
                "applied": False, "note": "该客户端尚未上报代理状态（可能未连接代理通道或从未应用过 Profile）"}
    return {"client_id": client_id, "proxy_connected": proxy_connected, **status}


async def _browser_clients(args: dict, ctx: PluginContext) -> dict:
    """List the browsers (CDP clients) the current caller may operate.

    这是「授权域可见性」的出口：模型从中知道手里有哪些浏览器、哪台是本会话
    绑定的、各自叫什么/是否在线。授权域 = current_cdp_client_ids（服务端注入的
    边界）或身份 grant 推导；每条附 tabs 概览（活动 tab 在前）。它不驱动任何
    tab——列表本身不消耗 tab 租约。
    """
    from mcp_builtin.cdp_bridge import server as cdp_server
    from mcp_runtime.plugin_loader import (
        current_cdp_client_ids,
        current_cdp_session_id,
        current_request_token,
    )

    token = current_request_token.get("")
    driver = cdp_server.driver
    allowed = current_cdp_client_ids.get(())
    ids = await _authorised_client_ids(token, driver, allowed)
    bound_client = ""
    session_key = current_cdp_session_id.get("")
    if session_key and ":" in session_key:
        bound_client = session_key.split(":", 1)[0]

    # driver 实时状态（name/connected/pages）与授权域求交：离线客户端也列出
    # （模型需要知道"这台浏览器此刻不在线"），但没有连接的 client 不凭空造。
    live: dict[str, dict] = {}
    if driver is not None:
        try:
            for ctx_row in driver.snapshot_contexts():
                for client in ctx_row.get("clients", []):
                    live[str(client.get("client_id"))] = client
        except Exception:
            live = {}

    clients = []
    for cid in ids:
        info = live.get(cid) or {}
        pages = info.get("pages") or []
        clients.append({
            "client_id": cid,
            "name": info.get("name") or "",
            "connected": bool(info.get("connected")),
            "bound": cid == bound_client,
            "tabs": [
                {"tab_id": p.get("id"), "url": p.get("url"), "title": p.get("title"), "active": bool(p.get("active"))}
                for p in pages
            ],
        })
    return {
        "clients": clients,
        "count": len(clients),
        "bound_client_id": bound_client,
        "hint": (
            "pass client_id on browser calls to pick one of these; the session-bound "
            "client is the default target"
            if len(clients) > 1
            else "this call is bound to a single browser"
        ),
    }


def register(reg: PluginRegistrar) -> None:
    """Register all cdp-bridge tools with the runtime registrar.

    Builtin cdp-bridge is part of the runtime: register() creates the process
    driver once and registers static tool definitions. Later auth/grant changes
    use apply_config(ctx) and do not rebuild tools or restart connections.
    """
    tools = _load_cdp_bridge_tools()
    cfg = _driver_config(reg.resources, reg.auth_enabled, reg.allowed_tokens)
    tools["init_driver"](
        clients=cfg["clients"],
        external_ws=True,
    )

    for name, fn_key, description, params in _TOOLS:
        props = params.setdefault("properties", {})
        props["client_id"] = {
            "type": "string",
            "description": "Browser client ID. Required when this MCP principal can access multiple clients.",
        }
        reg.tool(name=name, description=description, params=params)(_wrap(tools[fn_key]))
    reg.tool(
        name="browser_clients",
        description=(
            "List the browsers (CDP clients) you may operate, each with client_id, "
            "display name, connection state, its tabs, and which one this "
            "conversation is bound to. Read-only; call this before picking a "
            "client_id for other browser calls."
        ),
        params={"type": "object", "properties": {}},
    )(_browser_clients)
    reg.tool(
        name="browser_proxy_status",
        description=(
            "Read-only: report the current browser outbound-proxy state for a CDP "
            "client (which proxy Profile is active, who applied it, remaining lease). "
            "You cannot change the proxy path — that is reserved for the popup and the "
            "admin console. Call this to understand how a browser is currently routing "
            "its network traffic before relying on it as an egress."
        ),
        params={"type": "object", "properties": {}},
    )(_browser_proxy_status)
    reg.view(name="sessions", description="Runtime browser session snapshot")(_sessions_view)


def apply_config(ctx: PluginContext) -> None:
    """Push updated auth/session config into the live cdp-bridge driver."""
    tools = _load_cdp_bridge_tools()
    cfg = _driver_config(ctx.resources, ctx.auth_enabled, ctx.allowed_tokens) if ctx.enabled else {"clients": []}
    tools["apply_config"](clients=cfg["clients"])


__all__ = ["register", "apply_config", "cdp_builtin_state"]
