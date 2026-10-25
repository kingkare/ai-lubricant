import asyncio, json, time
import importlib
from typing import Any
from contextvars import ContextVar

from . import simphtml


class _NoopMCP:
    """Compatibility shim for the vendored upstream module.

    The original cdp-bridge package exposes these functions through FastMCP.
    In this project, mcp_runtime registers them directly, so importing this
    module must not require the external ``mcp`` package.
    """

    def tool(self):
        def decorator(fn):
            return fn
        return decorator

    def run(self, *args, **kwargs):
        raise RuntimeError("Vendored cdp-bridge is loaded by mcp_runtime; standalone MCP server is disabled.")


mcp = _NoopMCP()

current_token: ContextVar[str] = ContextVar("current_token", default="")

from .TMWebDriver import TMWebDriver
driver: TMWebDriver | None = None


def init_driver(clients: list[dict] | None = None, external_ws: bool = True) -> TMWebDriver:
    """Create the runtime-owned driver. Standalone bridge listeners are disabled."""
    if not external_ws:
        raise RuntimeError("standalone CDP bridge server is removed; use MCP runtime")
    global driver
    if driver is None:
        driver = TMWebDriver(clients=clients, external_ws=True)
        _inject_browser_manager()
    return driver


def _inject_browser_manager() -> None:
    """把浏览器出口管理器注入 ProxyManager（browser 模式经代理通道 WS 转发）。"""
    try:
        from mcp_builtin.browser_proxy import get_browser_manager
        from providers.proxy_manager import set_browser_manager
        set_browser_manager(get_browser_manager())
    except Exception:
        # 注入失败不阻断 CDP bridge 初始化——browser 模式在真正出站时才报错。
        pass


def apply_config(clients: list[dict] | None = None) -> TMWebDriver:
    """Apply current CDP clients and disconnect revoked or rotated identities."""
    d = driver if driver is not None else init_driver(clients=clients)
    d.apply_clients(clients or [])
    return d


def get_driver():
    if driver is None:
        raise RuntimeError("CDP bridge is not initialized by MCP runtime")
    return driver


def _get_token() -> str | None:
    """Get the current request token from ContextVar."""
    token = current_token.get("")
    return token if token else None


def _ensure_sessions(d: TMWebDriver, token: str | None = None) -> list[dict[str, Any]]:
    sessions = d.get_all_sessions(token=token)
    if len(sessions) == 0:
        raise RuntimeError("No browser tabs connected.")
    return sessions


def _normalize_tab_id(tab_id: str | int | None) -> str | None:
    if tab_id is None or tab_id == "":
        return None
    return str(tab_id)


def _resolve_tab(d: TMWebDriver, tab_id: str | int | None, token: int | None = None):
    """Resolve a public session key and return its extension-native raw tab ID."""
    page, raw_tab_id = d.raw_tab_id(_normalize_tab_id(tab_id), token=token)
    d.get_context(token).default_session_id = page.id
    return page, raw_tab_id


def _extension_command(d: TMWebDriver, cmd: dict[str, Any], tab_id: str | int | None = None, timeout: float = 15, token: int | None = None) -> Any:
    command = dict(cmd)
    requested = tab_id if tab_id not in (None, "") else command.get("tabId")
    page, raw_tab_id = _resolve_tab(d, requested, token)
    command["tabId"] = raw_tab_id
    result = d.execute_js(json.dumps(command, ensure_ascii=False), timeout=timeout, session_id=page.id, token=token)
    return result.get("data", result)


@mcp.tool()
async def browser_get_tabs() -> str:
    """Get all open browser tabs with their IDs, URLs, and titles."""
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        sessions = d.get_all_sessions(token=token)
        for s in sessions:
            s.pop('connected_at', None)
            s.pop('type', None)
        return json.dumps({"tabs": sessions, "active_tab": ctx.default_session_id}, ensure_ascii=False)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_scan(tabs_only: bool = False, switch_tab_id: str = "", text_only: bool = False) -> str:
    """Get simplified HTML content of the active tab plus tab list. The HTML is optimized for LLM consumption (stripped of scripts, styles, invisible elements).

    Args:
        tabs_only: Only return tab list without page content (saves tokens).
        switch_tab_id: Switch to this tab before scanning.
        text_only: Return plain text instead of simplified HTML.
    """
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        if len(d.get_all_sessions(token=token)) == 0:
            return json.dumps({"status": "error", "msg": "No browser tabs connected. Ensure Chrome extension is running."}, ensure_ascii=False)

        if switch_tab_id:
            ctx.default_session_id = d.resolve_session(switch_tab_id, token).id

        tabs = []
        for sess in d.get_all_sessions(token=token):
            sess.pop('connected_at', None)
            sess.pop('type', None)
            sess['url'] = sess.get('url', '')[:80]
            tabs.append(sess)

        result = {
            "status": "success",
            "metadata": {"tabs_count": len(tabs), "tabs": tabs, "active_tab": ctx.default_session_id}
        }
        if not tabs_only:
            importlib.reload(simphtml)
            result["content"] = simphtml.get_html(d, cutlist=True, maxchars=35000, text_only=text_only, token=token)
        return json.dumps(result, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_execute_js(script: str, switch_tab_id: str = "", no_monitor: bool = False) -> str:
    """Execute JavaScript in the browser and capture results plus DOM changes.

    Args:
        script: JavaScript code to execute (or JSON command for CDP operations).
        switch_tab_id: Switch to this tab before executing.
        no_monitor: Skip DOM change monitoring (faster, less info).
    """
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        if len(d.get_all_sessions(token=token)) == 0:
            return json.dumps({"status": "error", "msg": "No browser tabs connected."}, ensure_ascii=False)
        if switch_tab_id:
            ctx.default_session_id = d.resolve_session(switch_tab_id, token).id
        importlib.reload(simphtml)
        result = simphtml.execute_js_rich(script, d, no_monitor=no_monitor, token=token)
        return json.dumps(result, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_switch_tab(tab_id: str) -> str:
    """Switch the active MCP browser tab without changing the visible Chrome tab.

    Args:
        tab_id: The tab ID to switch to (from browser_get_tabs).
    """
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        _ensure_sessions(d, token=token)
        page = d.resolve_session(tab_id, token)
        ctx.default_session_id = page.id
        return json.dumps({
            "status": "success",
            "active_tab": page.id,
            "url": page.info.get('url', ''),
        }, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_focus_tab(tab_id: str) -> str:
    """Bring a Chrome tab to the foreground: activate the tab AND focus its window.

    Unlike browser_switch_tab (which only changes the MCP-side active session
    without touching the visible Chrome UI), this actually makes the tab visible
    to the user. Use this when the user can't find the tab the agent is working
    on (e.g. across many windows / Spaces / minimized windows).

    Goes through chrome.tabs.update + chrome.windows.update (extension-native
    APIs), avoiding the chrome.debugger CDP "Not allowed" restriction on
    Target.activateTarget.

    Args:
        tab_id: The tab ID to focus (from browser_get_tabs).
    """
    token = _get_token()
    def _run():
        d = get_driver()
        _ensure_sessions(d, token=token)
        page, raw_tab_id = _resolve_tab(d, tab_id, token)
        result = _extension_command(
            d,
            {"cmd": "tabs", "method": "switch"},
            tab_id=page.id,
            timeout=10,
            token=token,
        )
        # User asked us to bring this tab to the front — they will most likely
        # operate on it next, so sync the MCP-side active session too.
        d.get_context(token).default_session_id = page.id
        return json.dumps({
            "status": "success",
            "focused_tab": page.id,
            "extension_response": result,
        }, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_open_tab(url: str = "", new_window: bool = False, active: bool = True) -> str:
    """Open a new tab, or a new browser window, at a URL.

    Goes through chrome.tabs.create / chrome.windows.create (extension-native
    APIs). The command is relayed through the currently active tab, so at least
    one tab must already be connected.

    The new tab needs a moment to load and register itself as a session, so its
    ID may not show up in browser_get_tabs immediately.

    Args:
        url: URL to open; defaults to about:blank.
        new_window: Open a separate browser window instead of a tab in the current one.
        active: Whether the new tab becomes the active tab (ignored for a new window).
    """
    token = _get_token()
    def _run():
        d = get_driver()
        _ensure_sessions(d, token=token)
        result = _extension_command(
            d,
            {
                "cmd": "tabs",
                "method": "create",
                "url": url or "about:blank",
                "newWindow": bool(new_window),
                "active": bool(active),
            },
            timeout=10,
            token=token,
        )
        return json.dumps({"status": "success", "tab": result}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_close_tab(tab_id: str) -> str:
    """Close a tab by its ID.

    Used by the site runner to evict a stuck or logged-out pooled tab before
    opening a replacement. Closing the last tab of a window closes the window.

    Args:
        tab_id: Tab ID to close (from browser_get_tabs).
    """
    token = _get_token()
    def _run():
        d = get_driver()
        _ensure_sessions(d, token=token)
        result = _extension_command(
            d,
            {"cmd": "tabs", "method": "close"},
            tab_id=tab_id,
            timeout=10,
            token=token,
        )
        return json.dumps({"status": "success", "result": result}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_batch(commands: list[dict[str, Any]], tab_id: str = "", timeout: float = 20) -> str:
    """Run multiple extension/CDP commands in one request.

    Args:
        commands: Command objects supported by the extension, such as
            {"cmd":"cdp","method":"DOM.getDocument","params":{"depth":1}}.
        tab_id: Optional tab ID inherited by commands that omit tabId.
        timeout: Seconds to wait for the batch result.
    """
    token = _get_token()
    def _run():
        d = get_driver()
        _ensure_sessions(d, token=token)
        result = _extension_command(d, {"cmd": "batch", "commands": commands}, tab_id=tab_id, timeout=timeout, token=token)
        return json.dumps({"status": "success", "results": result}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_wait(condition_js: str, timeout: float = 10, interval: float = 0.5, switch_tab_id: str = "") -> str:
    """Wait until JavaScript condition returns a truthy value.

    Args:
        condition_js: JavaScript expression or script. The return value is tested for truthiness.
        timeout: Maximum seconds to wait.
        interval: Seconds between checks.
        switch_tab_id: Optional tab ID to make active before waiting.
    """
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        _ensure_sessions(d, token=token)
        if switch_tab_id:
            ctx.default_session_id = d.resolve_session(switch_tab_id, token).id
        deadline = time.time() + max(timeout, 0)
        last_value = None
        last_error = None
        attempts = 0
        while True:
            attempts += 1
            try:
                response = d.execute_js(condition_js, timeout=min(max(interval, 0.2), 5), token=token)
                last_value = response.get("data", response.get("result"))
                last_error = None
                if last_value:
                    return json.dumps({
                        "status": "success",
                        "value": last_value,
                        "attempts": attempts,
                        "tab_id": ctx.default_session_id,
                    }, ensure_ascii=False, default=str)
            except Exception as e:
                last_error = str(e)
            if time.time() >= deadline:
                return json.dumps({
                    "status": "timeout",
                    "value": last_value,
                    "error": last_error,
                    "attempts": attempts,
                    "tab_id": ctx.default_session_id,
                }, ensure_ascii=False, default=str)
            time.sleep(max(interval, 0.1))
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_navigate(url: str) -> str:
    """Navigate the active tab to a URL.

    Args:
        url: The URL to navigate to.
    """
    token = _get_token()
    def _run():
        d = get_driver()
        if len(d.get_all_sessions(token=token)) == 0:
            return json.dumps({"status": "error", "msg": "No browser tabs connected."}, ensure_ascii=False)
        d.jump(url, timeout=10, token=token)
        return json.dumps({"status": "success", "msg": f"Navigating to {url}"}, ensure_ascii=False)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_network_start(tab_id: str = "", url_pattern: str = "", push: bool = False, capture_id: str = "") -> str:
    """Start watching a tab's network traffic (persistent CDP Network listener).

    Advertised to the Agent as ``browser_watch_network`` (the plugin's _TOOLS maps
    the public name to this function). Attaches the debugger and keeps it attached,
    buffering every HTTP request (URL, method, headers, status, timing, body) AND
    every WebSocket frame and SSE message into an in-browser buffer until stopped.
    While watching, a red "正在监听网络请求" banner is pinned to the top of the page;
    the user can stop it from there too.

    Args:
        tab_id: Tab ID to watch (from browser_get_tabs). Uses active tab if empty.
        url_pattern: Optional substring filter; only requests/frames whose URL contains
            this string are kept. Empty watches everything.
        push: Also stream each matching request/frame to the server as a ``net_event``
            WebSocket frame, tagged with capture_id. Used by the site runner to
            await a specific response without polling; default off (pull via
            browser_network_get) keeps the existing behaviour unchanged.
        capture_id: Correlation id echoed back on every pushed ``net_event``.
            Required when push is true.
    """
    if push and not capture_id:
        return json.dumps({"status": "error", "msg": "capture_id is required when push is enabled."}, ensure_ascii=False)
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        if len(d.get_all_sessions(token=token)) == 0:
            return json.dumps({"status": "error", "msg": "No browser tabs connected."}, ensure_ascii=False)
        target = tab_id or ctx.default_session_id
        cmd = {"cmd": "net_start", "urlPattern": url_pattern}
        if push:
            cmd["push"] = True
            cmd["captureId"] = capture_id
        result = _extension_command(d, cmd, tab_id=target, timeout=15, token=token)
        return json.dumps({"status": "success", "capture": result}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_network_get(
    tab_id: str = "",
    search: str = "",
    request_id: str = "",
    frame_index: int = -1,
) -> str:
    """Read captured network traffic. Capture keeps running.

    默认只回**概要**：每条请求一行（requestId / method / url / status / mime /
    大小），不灌 body/headers——先看有哪些请求，需要哪条再深入。

    Args:
        tab_id: Tab ID to read (from browser_get_tabs). Uses active tab if empty.
        search: 只回 URL 含该子串的请求概要（概要级过滤）。
        request_id: 精确取该请求的全量（headers + response body），只回一条。
        frame_index: 取第 N 个 WS/SSE 帧的完整 payload（概要里只给了长度）。
    """
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        _ensure_sessions(d, token=token)
        target = tab_id or ctx.default_session_id
        cmd: dict[str, Any] = {"cmd": "net_get"}
        if search:
            cmd["search"] = search
        if request_id:
            cmd["requestId"] = request_id
        if frame_index is not None and frame_index >= 0:
            cmd["frameIndex"] = frame_index
        result = _extension_command(d, cmd, tab_id=target, timeout=15, token=token)
        return json.dumps({"status": "success", "capture": result}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_network_stop(tab_id: str = "") -> str:
    """Stop capturing on a tab, detach the debugger, and return all captured requests.

    Args:
        tab_id: Tab ID to stop (from browser_get_tabs). Uses active tab if empty.
    """
    token = _get_token()
    def _run():
        d = get_driver()
        ctx = d.get_context(token)
        _ensure_sessions(d, token=token)
        target = tab_id or ctx.default_session_id
        result = _extension_command(d, {"cmd": "net_stop"}, tab_id=target, timeout=15, token=token)
        return json.dumps({"status": "success", "capture": result}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_screenshot(tab_id: str = "") -> str:
    """Take a screenshot of the active tab (returns base64 for the runtime spill stage).

    The Agent runtime must persist the returned bytes and expose only the saved
    workspace path to the model; callers must not inline this base64 in context.

    Args:
        tab_id: Optional tab ID to screenshot. Uses active tab if empty.
    """
    token = _get_token()
    def _run():
        d = get_driver()
        if len(d.get_all_sessions(token=token)) == 0:
            return json.dumps({"status": "error", "msg": "No browser tabs connected."}, ensure_ascii=False)
        cmd = {"cmd": "cdp", "method": "Page.captureScreenshot", "params": {"format": "png"}}
        target = tab_id or d.get_context(token).default_session_id
        result = _extension_command(d, cmd, tab_id=target, token=token)
        data = result
        if isinstance(data, dict) and 'data' in data:
            return json.dumps({"status": "success", "format": "png", "base64": data['data']}, ensure_ascii=False)
        return json.dumps({"status": "success", "data": data}, ensure_ascii=False, default=str)
    return await asyncio.to_thread(_run)


@mcp.tool()
async def browser_totp_code(url: str = "", domain: str = "") -> str:
    """Get the current TOTP 2FA verification code(s) for a site (Google Authenticator style).

    Looks up the caller's authenticator entries whose linked domains (关联网站) match
    the given site, computes the current code server-side, and returns it with the
    seconds remaining before it rotates. Use this when a login flow asks for a
    two-factor / verification code, then fill it into the page — the secret itself
    is never exposed, only the ephemeral code.

    Args:
        url: Page URL (preferred) — the hostname (+port) is matched against linked domains.
        domain: Bare domain to match when no URL is at hand (e.g. "github.com").
    """
    from urllib.parse import urlparse

    token = _get_token()
    host = ""
    if url:
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
            host = (parsed.hostname or "") + (f":{parsed.port}" if parsed.port else "")
        except (ValueError, AttributeError):
            host = ""
    host = (host or domain or "").strip().lower()
    if not host:
        return json.dumps(
            {"status": "error", "msg": "Provide url or domain to match against linked sites."},
            ensure_ascii=False,
        )

    import mcp_plugin_store

    owner = await mcp_plugin_store.get_cdp_client_owner_user_id(token or "")
    if not owner:
        return json.dumps(
            {"status": "error", "msg": "This browser client is not bound to a user; no authenticator available."},
            ensure_ascii=False,
        )

    import totp_store

    try:
        entries = await totp_store.find_by_domain(owner, host)
    except RuntimeError:
        return json.dumps({"status": "error", "msg": "Authenticator storage unavailable."}, ensure_ascii=False)
    if not entries:
        return json.dumps(
            {"status": "success", "entries": [],
             "msg": f"No TOTP authenticator is linked to '{host}'. The user can add one in the extension popup (🛡 icon) or the web console."},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "status": "success",
            "entries": [
                {
                    "id": e["id"],
                    "issuer": e["issuer"],
                    "account": e["account"],
                    "code": e["code"],
                    # 绝对失效时刻（epoch 秒）——不是剩余秒数（客户端自己算 now 差）。
                    "expiresAt": e["expiresAt"],
                }
                for e in entries
            ],
        },
        ensure_ascii=False,
    )


_FILL_RECEIPT_SECRET_KEYS = ("code", "password", "secret", "value", "secret_enc")


def _scrub_secrets(node: Any) -> Any:
    """Recursively drop every secret-shaped key from a value.

    The receipt is built from a whitelist, but ``field`` is copied verbatim from the
    extension response — a buggy or hostile extension could nest a value there. This
    is the belt to the whitelist's braces: whatever the shape, a ``code``/``password``
    key cannot survive to the model."""
    if isinstance(node, dict):
        return {k: _scrub_secrets(v) for k, v in node.items() if k not in _FILL_RECEIPT_SECRET_KEYS}
    if isinstance(node, list):
        return [_scrub_secrets(v) for v in node]
    return node


def build_fill_receipt(kind: str, entry_meta: dict | None, ext_result: Any) -> dict:
    """Assemble the model-facing receipt for a fill_credential call.

    **No credential ever leaves here.** Whatever the extension reports back (it may
    echo the value it typed), this whitelist drops every secret-shaped key so a code
    or password can never reach the model, the conversation log, or an upstream LLM.
    """
    ext = ext_result if isinstance(ext_result, dict) else {}
    receipt: dict[str, Any] = {
        "status": "ok" if ext.get("ok", True) else "error",
        "operation": "fill_credential",
        "kind": kind,
        "filled": bool(ext.get("filled")),
    }
    if ext.get("field"):
        receipt["field"] = ext["field"]
    if ext.get("reason"):
        receipt["reason"] = ext["reason"]
    if kind == "otp":
        if entry_meta:
            receipt["entry"] = {"issuer": entry_meta.get("issuer"), "account": entry_meta.get("account")}
        if entry_meta and entry_meta.get("expiresAt") is not None:
            receipt["expiresAt"] = entry_meta.get("expiresAt")
    else:  # password
        if entry_meta:
            receipt["entry"] = {"username": entry_meta.get("username"), "domain": entry_meta.get("domain")}
    if not receipt["filled"] and "reason" not in receipt:
        receipt["reason"] = ext.get("error") or "fill_failed"
    return _scrub_secrets(receipt)


@mcp.tool()
async def browser_fill_credential(
    kind: str,
    url: str = "",
    domain: str = "",
    account: str = "",
    selector: str = "",
    tab_id: str = "",
) -> str:
    """Type a saved credential (2FA code or password) INTO the page — never return it.

    The value travels server → extension → DOM. It is **never** sent back to you: the
    result is a receipt (what was filled, which entry matched), with no code and no
    password. Use this whenever a login form asks for a password or a 2FA / verification
    code and you do not need to read the value yourself. To *read* a code instead, use
    browser_totp_code.

    Args:
        kind: "otp" for a TOTP/2FA code, "password" for a saved password (+ username).
        url: Page URL — its hostname is matched against the entry's linked domains.
        domain: Bare domain to match when no URL is at hand (e.g. "github.com").
        account: Which entry to use when several match (username / TOTP account label).
            Omit it and, if more than one entry matches, nothing is filled and the
            candidates are returned (labels only, no codes/passwords) for you to retry.
        selector: Optional explicit CSS selector for the field to fill.
        tab_id: Tab to act on (from browser_get_tabs). Uses the active tab if empty.
    """
    from urllib.parse import urlparse

    kind = str(kind or "").strip().lower()
    if kind not in ("otp", "password"):
        return json.dumps({"status": "error", "msg": "kind must be 'otp' or 'password'."}, ensure_ascii=False)

    token = _get_token()
    host = ""
    if url:
        try:
            parsed = urlparse(url if "://" in url else f"https://{url}")
            host = (parsed.hostname or "") + (f":{parsed.port}" if parsed.port else "")
        except (ValueError, AttributeError):
            host = ""
    host = (host or domain or "").strip().lower()
    if not host:
        return json.dumps({"status": "error", "msg": "Provide url or domain to match the site."}, ensure_ascii=False)

    import mcp_plugin_store

    owner = await mcp_plugin_store.get_cdp_client_owner_user_id(token or "")
    if not owner:
        return json.dumps(
            {"status": "error", "msg": "This browser client is not bound to a user; no credential available."},
            ensure_ascii=False,
        )

    account = str(account or "").strip()

    if kind == "otp":
        import totp_store

        try:
            entries = await totp_store.find_by_domain(owner, host)
        except RuntimeError:
            return json.dumps({"status": "error", "msg": "Authenticator storage unavailable."}, ensure_ascii=False)
        if not entries:
            return json.dumps(
                {"status": "error", "filled": False,
                 "msg": f"No TOTP authenticator is linked to '{host}'. Ask the user to add it in the extension popup (🛡 icon) or the web console."},
                ensure_ascii=False,
            )
        if account:
            picked = next((e for e in entries if str(e.get("account") or "") == account), None)
        else:
            picked = entries[0] if len(entries) == 1 else None
        if picked is None:
            # Disambiguation only — labels, never codes.
            return json.dumps(
                {"status": "ok", "operation": "fill_credential", "kind": "otp", "filled": False,
                 "reason": "multiple_entries",
                 "entries": [{"issuer": e.get("issuer"), "account": e.get("account")} for e in entries],
                 "msg": "Several authenticator entries match; retry with `account`."},
                ensure_ascii=False,
            )
        entry_meta = {"issuer": picked.get("issuer"), "account": picked.get("account"),
                      "expiresAt": picked.get("expiresAt")}
        fill_value = str(picked.get("code") or "")
        if not fill_value:
            return json.dumps({"status": "error", "filled": False, "msg": "Empty TOTP code."}, ensure_ascii=False)
    else:  # password
        import browser_password_store as pwd_store

        try:
            metas = await pwd_store.list_passwords(owner, host)
        except RuntimeError:
            return json.dumps({"status": "error", "msg": "Password storage unavailable."}, ensure_ascii=False)
        if not metas:
            return json.dumps(
                {"status": "error", "filled": False,
                 "msg": f"No saved password for '{host}'. Ask the user to save it in the extension (🔑 icon) or the web console."},
                ensure_ascii=False,
            )
        if account:
            picked = next((m for m in metas if str(m.get("username") or "") == account), None)
        else:
            picked = metas[0] if len(metas) == 1 else None
        if picked is None:
            return json.dumps(
                {"status": "ok", "operation": "fill_credential", "kind": "password", "filled": False,
                 "reason": "multiple_entries",
                 "entries": [{"username": m.get("username"), "domain": m.get("domain")} for m in metas],
                 "msg": "Several saved passwords match; retry with `account`."},
                ensure_ascii=False,
            )
        entry_meta = {"username": picked.get("username"), "domain": picked.get("domain")}
        # Password value is resolved INSIDE the extension from its own store — the
        # server never even sees the plaintext on this path.
        fill_value = ""
        entry_id = picked.get("id")

    cmd: dict[str, Any] = {"cmd": "fill_credential", "kind": kind, "account": account,
                           "selector": selector, "domain": host}
    if kind == "otp":
        cmd["value"] = fill_value
    if kind == "password":
        cmd["username"] = entry_meta.get("username") or ""
        if entry_id is not None:
            cmd["entryId"] = entry_id

    def _run():
        d = get_driver()
        _ensure_sessions(d, token=token)
        target = tab_id or d.get_context(token).default_session_id
        return _extension_command(d, cmd, tab_id=target, timeout=15, token=token)

    try:
        ext_result = await asyncio.to_thread(_run)
    except Exception as exc:
        return json.dumps({"status": "error", "filled": False, "msg": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)

    return json.dumps(build_fill_receipt(kind, entry_meta, ext_result), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
