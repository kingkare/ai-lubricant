"""站点服务 —— 浏览器操作的**静态方法类**（无实例状态，不预设业务）。

定位：一层「公共操作方法」，把「在指定 CDP 浏览器里跑 JS / 管 tab」封装成可复用的静态
方法。**不做初始化、不做定时、不认业务**——那些由外层（spec / site 服务）做，外层调本类
的方法即可。

    spec / site 服务（外层：init / 定时任务 / 路由 / 业务）
        │  SiteServer.run_js(client_id, code) / open_tab / close_tab / list_tabs
        ▼
    SiteServer（本模块，静态方法类，内部自管 tab 选择）
        │  ContextVar 路由 current_token=client_id → cdp-bridge
        ▼
    cdp-bridge.execute_js(code, token=client_id)   ← cdp 侧就一个 function
        │
        ▼
    浏览器执行 JS → 返回值

tab 管理（内部，调用方不感知）：按 client_id 记一个「本模块的常驻 tab」（首次用时开、
后续复用）；也可显式传 tab_id 指定。代码不在 cdp 上执行，只是**利用** cdp 去执行。
"""

from __future__ import annotations

import asyncio
import json
import time

from loguru import logger

# 新 tab 注册成 session 的上限（秒）。扩展上报是异步的，正常 1s 内。
_TAB_REGISTER_TIMEOUT = 20.0
_TAB_REGISTER_INTERVAL = 0.3

# client_id → 本模块为它维护的常驻 tab session_key（内部状态，调用方不感知）。
_DEFAULT_TABS: dict[str, str] = {}


def _parse_tool_json(raw):
    """cdp 工具函数返回 JSON 字符串；解析失败原样返回。"""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


class SiteServer:
    """浏览器操作的静态方法类。所有方法以 client_id 定位浏览器，tab 内部自管。"""

    # ── 底层：在指定 client 上下文里调 cdp 工具 ────────────────────────────────

    @staticmethod
    async def _call(client_id: str, fn, *args, session_key: str = "", **kwargs):
        """ContextVar 路由到 client_id（+ 可选 session），调 cdp 工具函数。"""
        from mcp_builtin.cdp_bridge import server as cdp_server
        from mcp_builtin.cdp_bridge.TMWebDriver import current_session_id

        tok_t = cdp_server.current_token.set(client_id)
        tok_s = current_session_id.set(session_key or "")
        try:
            return await fn(*args, **kwargs)
        finally:
            cdp_server.current_token.reset(tok_t)
            current_session_id.reset(tok_s)

    # ── tab 管理（内部自管） ───────────────────────────────────────────────────

    @staticmethod
    async def open_tab(client_id: str, url: str = "", *, active: bool = False,
                       as_default: bool = True) -> str:
        """开一个 tab（默认记为该 client 的常驻 tab），等它注册成 session，返回 session_key。"""
        from mcp_builtin.cdp_bridge import server as cdp_server

        raw = await SiteServer._call(
            client_id, cdp_server.browser_open_tab, url=url or "about:blank", active=active,
        )
        payload = _parse_tool_json(raw)
        tab = payload.get("tab") if isinstance(payload, dict) else None
        raw_id = str((tab or {}).get("id") or "")
        if not raw_id:
            raise RuntimeError(f"开 tab 未返回 id: {str(payload)[:200]}")
        session_key = f"{client_id}:{raw_id}"
        deadline = time.monotonic() + _TAB_REGISTER_TIMEOUT
        while time.monotonic() < deadline:
            if session_key in await SiteServer.list_tabs(client_id):
                break
            await asyncio.sleep(_TAB_REGISTER_INTERVAL)
        else:
            raise RuntimeError(f"tab {session_key} 未在 {_TAB_REGISTER_TIMEOUT}s 内注册")
        if as_default:
            _DEFAULT_TABS[client_id] = session_key
        logger.info(f"[site-server] client={client_id} tab ready ({session_key})")
        return session_key

    @staticmethod
    async def default_tab(client_id: str) -> str:
        """取该 client 的常驻 tab；没有/已死就开一个（about:blank）。

        常驻 tab 可能被用户关掉或被 Chrome 回收，``_DEFAULT_TABS`` 里的记号就过期了。
        必须校验它还活着（``list_tabs`` 只列活跃页）——否则 run_js 钉到一张死页上，
        execute_js 会如实抛「未连接」（不再静默劫持别的 tab），没带 tab_id 的调用方
        就会无端失败。死了就当没有，重开一个常驻页。
        """
        sk = _DEFAULT_TABS.get(client_id)
        if sk:
            try:
                if sk in await SiteServer.list_tabs(client_id):
                    return sk
            except Exception as exc:  # noqa: BLE001 - 列举失败不挡，回落按「记号过期」重开
                logger.debug(f"[site-server] default_tab 校验 {sk} 失败: {exc}")
            _DEFAULT_TABS.pop(client_id, None)
        return await SiteServer.open_tab(client_id, "about:blank", as_default=True)

    @staticmethod
    async def set_default_tab(client_id: str, session_key: str) -> None:
        """把某已有 tab 认作该 client 的常驻 tab（复用浏览器里已存在的页面，不新开）。"""
        if session_key:
            _DEFAULT_TABS[client_id] = session_key

    @staticmethod
    async def find_tab(client_id: str, url_contains: str = "") -> str:
        """在该 client 已注册的 tab 里找 URL 含 ``url_contains`` 的第一个，返回 session_key（无则 ""）。

        用于「检测后复用」：重启后 ``_DEFAULT_TABS`` 空了，但浏览器里可能已有目标页的 tab，
        找到它认作常驻 tab，避免重复开 tab。
        """
        if not url_contains:
            return ""
        from mcp_builtin.cdp_bridge import server as cdp_server

        payload = _parse_tool_json(await SiteServer._call(client_id, cdp_server.browser_get_tabs))
        tabs = payload.get("tabs") if isinstance(payload, dict) else None
        for item in tabs or []:
            if not isinstance(item, dict):
                continue
            if url_contains in str(item.get("url") or ""):
                sid = str(item.get("id") or item.get("session_key") or "")
                if sid:
                    return sid if ":" in sid else f"{client_id}:{sid}"
        return ""

    @staticmethod
    async def list_tabs(client_id: str) -> set[str]:
        """列出该 client 当前已注册的 tab session_key 集合。"""
        from mcp_builtin.cdp_bridge import server as cdp_server

        payload = _parse_tool_json(await SiteServer._call(client_id, cdp_server.browser_get_tabs))
        tabs = payload.get("tabs") if isinstance(payload, dict) else None
        out: set[str] = set()
        for item in tabs or []:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or item.get("session_key") or "")
            if sid:
                out.add(sid if ":" in sid else f"{client_id}:{sid}")
        return out

    @staticmethod
    async def close_tab(client_id: str, tab_id: str = "") -> None:
        """关 tab（默认关该 client 的常驻 tab）。失败不抛。"""
        sk = tab_id or _DEFAULT_TABS.get(client_id, "")
        if not sk:
            return
        from mcp_builtin.cdp_bridge import server as cdp_server

        try:
            await SiteServer._call(client_id, cdp_server.browser_close_tab, sk)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[site-server] close tab {sk} failed: {exc}")
        finally:
            if _DEFAULT_TABS.get(client_id) == sk:
                _DEFAULT_TABS.pop(client_id, None)

    # ── 公共操作 ───────────────────────────────────────────────────────────────

    @staticmethod
    async def run_js(client_id: str, code: str, *, tab_id: str = "", timeout: float | None = None):
        """在 client_id 的浏览器里执行 JS，返回其值。

        tab_id 空 → 用/建该 client 的常驻 tab（内部自管）。
        code 约定：末行 ``return <expr>``（execute_js 走 AsyncFunction，支持 await）。
        失败（非 success）抛 RuntimeError。
        """
        session_key = tab_id or await SiteServer.default_tab(client_id)
        from mcp_builtin.cdp_bridge import server as cdp_server

        raw = await SiteServer._call(
            client_id, cdp_server.browser_execute_js, code,
            session_key=session_key, no_monitor=True,
        )
        payload = _parse_tool_json(raw)
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise RuntimeError(f"run_js 失败: {str(payload)[:200]}")
        # browser_execute_js → execute_js_rich 的返回值键是 js_return；兼容 data/value 变体。
        for key in ("js_return", "data", "value"):
            if key in payload:
                return payload[key]
        return None

    @staticmethod
    async def navigate(client_id: str, url: str, *, tab_id: str = "") -> None:
        """把指定 tab（默认常驻 tab）导航到 url（经 JS location.href，落点确定）。"""
        await SiteServer.run_js(client_id, f"location.href = {json.dumps(url)}; return null",
                                tab_id=tab_id)

    # ── 浏览器发现 ─────────────────────────────────────────────────────────────

    @staticmethod
    async def list_clients(owner_user_id: str = "") -> list[dict]:
        """列出可驱动的 CDP 浏览器（owner 非空则过滤到该 owner 的 cdp_client 资源）。

        返回 ``[{client_id, name, connected, tabs}]``：在线态来自 driver.snapshot_contexts()，
        授权集来自 builtin_tool_store.list_resources('cdp_client', owner_user_id=owner)。

        安全边界：site 实例带 owner 时必须传 owner，否则列到别人浏览器 = 「驱动别人浏览器」
        的洞重现（cdp_client_id 可枚举，见旧 assert_browsers_owned 注释）。
        """
        from mcp_builtin.cdp_bridge import server as cdp_server

        # 授权集：owner 给定时过滤到该 owner 的 cdp_client；空时给所有（管理员视图）
        try:
            import builtin_tool_store
            owned = [builtin_tool_store.cdp_client_id_of(r) for r in
                     await builtin_tool_store.list_resources("cdp_client",
                                                              owner_user_id=owner_user_id or None)]
            owned = [c for c in owned if c]
        except Exception as exc:  # noqa: BLE001 - DB 未就绪不阻断，回落空列表
            logger.debug(f"[site-server] list_clients DB 失败: {exc}")
            owned = []
        if not owned:
            return []

        # 在线态：driver 内存快照
        live: dict[str, dict] = {}
        driver = getattr(cdp_server, "driver", None)
        if driver is not None:
            try:
                for ctx_row in driver.snapshot_contexts():
                    for client in ctx_row.get("clients", []):
                        cid = str(client.get("client_id") or "")
                        if cid:
                            live[cid] = client
            except Exception:  # noqa: BLE001
                live = {}

        out: list[dict] = []
        for cid in owned:
            info = live.get(cid) or {}
            pages = info.get("pages") or []
            out.append({
                "client_id": cid,
                "name": info.get("name") or "",
                "connected": bool(info.get("connected")),
                "tabs": [
                    {"tab_id": p.get("id"), "url": p.get("url"),
                     "title": p.get("title"), "active": bool(p.get("active"))}
                    for p in pages
                ],
            })
        return out

    # ── 网络监听（push 模式：扩展推 net_event → capture 路由表 → wait_capture 收）──

    @staticmethod
    async def capture_start(client_id: str, url_pattern: str = "") -> str:
        """开始监听该浏览器 tab 的网络（push 模式）。返回 capture_id。

        底层：capture.new_capture_id() → browser_network_start(push=True, capture_id=...)。
        帧由扩展推 net_event → sse_gateway 路由到 capture.deliver → 等待中的 wait_capture。
        """
        from mcp_builtin.cdp_bridge import server as cdp_server
        from mcp_builtin.cdp_bridge import net_capture as site_capture

        capture_id = site_capture.new_capture_id(f"site:{client_id}")
        session_key = await SiteServer.default_tab(client_id)
        raw_tab_id = session_key.split(":", 1)[1] if ":" in session_key else session_key
        await SiteServer._call(
            client_id, cdp_server.browser_network_start,
            tab_id=raw_tab_id, url_pattern=url_pattern, push=True, capture_id=capture_id,
        )
        return capture_id

    @staticmethod
    async def wait_capture(client_id: str, capture_id: str, *, timeout: float = 30) -> list[dict]:
        """等捕获帧（HTTP 请求 / WS / SSE）。超时抛 TimeoutError。跑完自动 unregister。

        与 capture_start 配对：start 后注册队列，本方法 await 收帧，结束 unregister。
        """
        from mcp_builtin.cdp_bridge import net_capture as site_capture

        queue = site_capture.register(capture_id)
        try:
            frames: list[dict] = []
            deadline = time.monotonic() + max(timeout, 1.0)
            while time.monotonic() < deadline:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    break
                frames.append(frame)
            if not frames:
                raise asyncio.TimeoutError(f"capture {capture_id} 等待超时（{timeout}s 无帧）")
            return frames
        finally:
            site_capture.unregister(capture_id)

    @staticmethod
    async def capture_stop(client_id: str, capture_id: str) -> None:
        """停监听（browser_network_stop + capture.unregister）。失败不抛。"""
        from mcp_builtin.cdp_bridge import server as cdp_server
        from mcp_builtin.cdp_bridge import net_capture as site_capture

        session_key = _DEFAULT_TABS.get(client_id, "")
        raw_tab_id = session_key.split(":", 1)[1] if ":" in session_key else session_key
        try:
            await SiteServer._call(client_id, cdp_server.browser_network_stop, tab_id=raw_tab_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[site-server] capture_stop {capture_id} failed: {exc}")
        finally:
            site_capture.unregister(capture_id)
