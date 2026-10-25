import json, threading, time, uuid, sys, hashlib
from contextvars import ContextVar
from typing import Dict, Any, Optional, List


def log(*args, **kwargs):
    print(*args, file=sys.stderr, flush=True, **kwargs)


def _tlog(user_id, *args, **kwargs):
    prefix = f"[user:{user_id}] " if user_id is not None else ""
    print(prefix, *args, file=sys.stderr, flush=True, **kwargs)


current_session_id: ContextVar[str] = ContextVar("cdp_current_session_id", default="")

# 在任连接超过这么久没收到入站帧就判定为「死连接」，允许新连接抢占槽位。
# 客户端 keepalive 每 ~24s 发一个 ping，45s 是「还在跳」与「已经半开/被遗忘」的分界；
# 必须小于服务端空闲读超时（sse_gateway._EXT_IDLE_TIMEOUT=60s），抢占才比等超时快。
_SLOT_STALE_TIMEOUT = 45.0


class Session:
    def __init__(self, session_id, info, client=None):
        self.id, self.info = session_id, info
        self.connect_at, self.disconnect_at = time.time(), None
        self.type = info.get("type", "ext_ws")
        self.ws_client = client if self.type == "ext_ws" else None
        self.http_queue = client if self.type == "http" else None

    @property
    def url(self): return self.info.get("url", "")

    def is_active(self):
        if self.type == "http" and self.disconnect_at is None and time.time() - self.connect_at > 60:
            self.disconnect_at = time.time()
        return self.disconnect_at is None

    def reconnect(self, client, info):
        self.info, self.type = info, info.get("type", "ext_ws")
        self.ws_client = client if self.type == "ext_ws" else None
        self.http_queue = client if self.type == "http" else None
        self.connect_at, self.disconnect_at = time.time(), None

    def mark_disconnected(self):
        self.disconnect_at = self.disconnect_at or time.time()


class ClientContext:
    def __init__(self, client_id: str, name: str = ""):
        self.client_id, self.name = client_id, name
        self.pages: Dict[str, Session] = {}
        self.connected_at, self.last_active = time.time(), time.time()
        self.ws_client = None
        # 代理出口 slot：与 ws_client（控制/对话通道）平行的第二条独立 WS 连接
        # （端点 /ws/proxy）。单点登录约束只作用于控制通道；代理通道各自独立，
        # 两者互不抢占、互不断开（见 docs/browser-proxy-design.md §5.2）。
        self.proxy_ws_client = None


class UserContext:
    """MCP user state containing authenticated CDP clients and their pages."""
    def __init__(self, user_id):
        self.user_id = user_id
        self.clients: Dict[str, ClientContext] = {}
        self.results: Dict[str, Any] = {}
        self.acks: Dict[str, bool] = {}
        self.default_session_id: Optional[str] = None
        self.latest_session_id: Optional[str] = None
        self.created_at, self.last_active = time.time(), time.time()

    @property
    def sessions(self) -> Dict[str, Session]:
        return {key: page for client in self.clients.values() for key, page in client.pages.items()}

    def clean_sessions(self):
        for client in self.clients.values():
            for key, page in list(client.pages.items()):
                if not page.is_active() and time.time() - page.disconnect_at > 600:
                    del client.pages[key]

    def get_all_active_sessions(self):
        return [{"id": page.id, **page.info} for page in self.sessions.values() if page.is_active()]


class TokenManager:
    """Validates SHA-256 hashes and owns user -> clients runtime contexts."""
    def __init__(self, clients: Optional[List[dict]] = None):
        self.contexts: Dict[Any, UserContext] = {}
        self._lock = threading.Lock()
        self.clients_by_hash: Dict[str, dict] = {}
        self.update_clients(clients or [])

    def update_clients(self, clients: List[dict]):
        self.clients_by_hash = {str(row["token_hash"]): dict(row) for row in clients if row.get("enabled", True) and row.get("token_hash")}

    def authenticate(self, token: str) -> dict | None:
        if not token: return None
        return self.clients_by_hash.get(hashlib.sha256(token.encode("utf-8")).hexdigest())

    def get_context(self, user_id) -> UserContext:
        with self._lock:
            if user_id not in self.contexts: self.contexts[user_id] = UserContext(user_id)
            ctx = self.contexts[user_id]; ctx.last_active = time.time(); return ctx

    def cleanup_expired(self, max_idle=3600):
        with self._lock:
            for uid in [uid for uid, ctx in self.contexts.items() if time.time() - ctx.last_active > max_idle]:
                del self.contexts[uid]


class TMWebDriver:
    def __init__(self, clients=None, external_ws=True):
        if not external_ws:
            raise RuntimeError("standalone CDP bridge listeners are removed; use MCP runtime")
        self.external_ws = True
        self.token_manager = TokenManager(clients or [])
        self.is_remote = False

    def apply_clients(self, clients: List[dict]):
        # 会话池按 client_id 隔离（插件单点登录=一个客户端一个浏览器）。
        # 失效判定只看 client_id → token_hash 是否仍有效，不再依赖 user_id。
        old_hashes = set(self.token_manager.clients_by_hash)
        self.token_manager.update_clients(clients)
        valid_clients = {}
        for row in self.token_manager.clients_by_hash.values():
            client_id = str(row.get("instance_key") or row.get("id") or "")
            if client_id:
                valid_clients[client_id] = str(row["token_hash"])
        for client_id, ctx in list(self.token_manager.contexts.items()):
            for cid, client in list(ctx.clients.items()):
                token_hash = valid_clients.get(cid)
                active_hash = str(getattr(client.ws_client, "_token", "")) if client.ws_client is not None else ""
                proxy_hash = str(getattr(client.proxy_ws_client, "_token", "")) if client.proxy_ws_client is not None else ""
                if token_hash is None or (active_hash and active_hash != token_hash) or (proxy_hash and proxy_hash != token_hash):
                    # 吊销/轮换：控制槽与代理槽都必须断——否则代理出口还能继续用旧 token 出网。
                    self.disconnect_client(client.ws_client)
                    self.disconnect_proxy(client.proxy_ws_client)
                    ctx.clients.pop(cid, None)
        return old_hashes - set(self.token_manager.clients_by_hash)

    def authenticate_client(self, token: str, client_id: str | None = None) -> dict | None:
        row = self.token_manager.authenticate(token)
        if not row: return None
        configured_id = str(row.get("instance_key") or row.get("id"))
        if client_id and client_id != configured_id: return None
        if not configured_id: return None
        return {**row, "client_id": configured_id}

    def bind_client(self, ws_client, config: dict) -> ClientContext:
        # 会话池按 client_id 隔离：context key = client_id，每个 context 只承载
        # 同一个 client（插件单点登录=一个客户端一个浏览器）。user_id 仅作元数据保留。
        client_id = str(config["client_id"])
        user_id = config.get("user_id")
        ctx = self.token_manager.get_context(client_id)
        existing = ctx.clients.get(client_id)
        if existing and existing.ws_client is not None and existing.ws_client is not ws_client:
            # 单点登录=一个 client_id 一条活动连接。但在任连接长时间（> _SLOT_STALE_TIMEOUT）
            # 没收到入站帧 = 半开死连接 / 被遗忘的孤儿 socket（客户端 SW 重启竞态会留下这种）：
            # 直接抢占槽位、把旧连接踢掉，新连接立刻接管。否则新连接只能干等服务端空闲读
            # 超时（60s）+ 客户端退避（最长 110s）才恢复，期间插件一直显示 already_connected。
            incumbent = existing.ws_client
            if time.time() - float(getattr(incumbent, "_last_in", 0) or 0) > _SLOT_STALE_TIMEOUT:
                self.disconnect_client(incumbent, reason="slot preempted by new connection")
            else:
                raise ValueError("client token already connected")
        client = existing or ClientContext(client_id, str(config.get("name") or ""))
        client.ws_client, client.last_active = ws_client, time.time()
        ws_client._last_in = time.time()
        ctx.clients[client_id] = client
        ws_client._user_id, ws_client._client_id, ws_client._token = user_id, client_id, str(config.get("token_hash") or "")
        return client

    def disconnect_client(self, ws_client, reason="authorization revoked"):
        if ws_client is None: return
        try: ws_client.close_auth(4403, reason)
        except Exception: pass
        self.unregister_client(ws_client)

    # ── 代理出口 slot（/ws/proxy）：与控制通道平行、互不抢占 ────────────────
    def bind_proxy(self, ws_client, config: dict) -> ClientContext:
        """绑定代理出口连接。与 bind_client 的关键区别：不碰 ws_client。

        同一 client_id 允许「控制通道 + 代理通道」两条连接并存；代理通道自身
        仍是单点（一个 client 一条代理 socket），抢占语义与 stale 判定同控制通道。
        """
        client_id = str(config["client_id"])
        ctx = self.token_manager.get_context(client_id)
        existing = ctx.clients.get(client_id)
        if existing and existing.proxy_ws_client is not None and existing.proxy_ws_client is not ws_client:
            incumbent = existing.proxy_ws_client
            if time.time() - float(getattr(incumbent, "_last_in", 0) or 0) > _SLOT_STALE_TIMEOUT:
                self.disconnect_proxy(incumbent, reason="proxy slot preempted by new connection")
            else:
                raise ValueError("proxy connection already exists")
        client = existing or ClientContext(client_id, str(config.get("name") or ""))
        client.proxy_ws_client = ws_client
        ws_client._last_in = time.time()
        ctx.clients[client_id] = client
        ws_client._user_id, ws_client._client_id, ws_client._token = config.get("user_id"), client_id, str(config.get("token_hash") or "")
        return client

    def unregister_proxy(self, ws_client):
        cid = getattr(ws_client, "_client_id", None)
        if cid is None: return
        cctx = self.token_manager.get_context(cid).clients.get(cid)
        if cctx and cctx.proxy_ws_client is ws_client:
            cctx.proxy_ws_client = None

    def disconnect_proxy(self, ws_client, reason="authorization revoked"):
        if ws_client is None: return
        try: ws_client.close_auth(4404, reason)
        except Exception: pass
        self.unregister_proxy(ws_client)

    def get_proxy_client(self, client_id: str):
        """供出口网关按 client_id 取当前代理 socket；无连接返回 None。"""
        ctx = self.token_manager.contexts.get(client_id)
        if not ctx: return None
        client = ctx.clients.get(client_id)
        return client.proxy_ws_client if client else None

    def list_proxy_connected(self) -> list:
        """已连代理通道的 client_id 列表（管理端展示可用出口）。"""
        out = []
        for cid, ctx in self.token_manager.contexts.items():
            for inner_cid, client in ctx.clients.items():
                if client.proxy_ws_client is not None:
                    out.append(inner_cid)
        return out

    def get_context(self, client_id=None):
        # 会话池按 client_id 隔离；参数名沿用旧签名（token=）但语义现在是 client_id。
        if client_id is None: raise ValueError("CDP client identity required")
        return self.token_manager.get_context(client_id)

    def get_all_sessions(self, token=None): return self.get_context(token).get_all_active_sessions()

    def find_session(self, url_pattern, token=None):
        ctx = self.get_context(token)
        pages = ctx.sessions
        if not url_pattern:
            page = pages.get(ctx.latest_session_id)
            return [(page.id, page.info)] if page and page.is_active() else []
        return [(page.id, page.info) for page in pages.values() if page.is_active() and url_pattern in page.url]

    def get_session_dict(self, token=None): return {s["id"]: s.get("url", "") for s in self.get_all_sessions(token)}

    def ingest_message(self, raw, client) -> None:
        data = json.loads(raw)
        if not hasattr(client, "_user_id") or not hasattr(client, "_client_id"):
            raise ValueError("client is not authenticated")
        ctx = self.get_context(client._client_id); cctx = ctx.clients.get(client._client_id)
        if not cctx or cctx.ws_client is not client: raise ValueError("client authorization expired")
        kind = data.get("type")
        if kind in ("ext_ready", "tabs_update"):
            tabs = data.get("tabs", [])
            if not isinstance(tabs, list):
                raise ValueError("tabs must be a list")
            current = {f"{client._client_id}:{tab['id']}" for tab in tabs}
            for key, page in list(cctx.pages.items()):
                if key not in current: page.mark_disconnected()
            for tab in tabs:
                raw_id = str(tab["id"]); key = f"{client._client_id}:{raw_id}"
                info = {"url": tab.get("url"), "title": tab.get("title", ""), "connected_at": time.time(), "type": "ext_ws", "client_id": client._client_id, "page_id": raw_id}
                page = cctx.pages.get(key)
                if page: page.reconnect(client, info)
                else: cctx.pages[key] = Session(key, info, client)
                ctx.latest_session_id = key
                if ctx.default_session_id is None: ctx.default_session_id = key
        elif kind in ("ack", "result", "error"):
            target = ctx.acks if kind == "ack" else ctx.results
            target[data.get("id", "")] = True if kind == "ack" else {"success": kind == "result", "data": data.get("result") if kind == "result" else data.get("error"), "newTabs": data.get("newTabs", [])}

    def unregister_client(self, client):
        cid = getattr(client, "_client_id", None)
        if cid is None: return
        cctx = self.token_manager.get_context(cid).clients.get(cid)
        if cctx and cctx.ws_client is client:
            cctx.ws_client = None
            for page in cctx.pages.values(): page.mark_disconnected()

    def notify_agents_changed(self) -> int:
        """向所有已连接的 CDP 客户端推「可操作 agent 列表已变更」。

        管理端/用户端改了某客户端的 agent_ids 后，浏览器里的网页对话面板是拉取式
        拿列表的（只在打开面板/点切换时拉），不推就一直是旧列表。这里不判定具体是
        哪个客户端变了（调用方不带变更 id，且客户端数量=浏览器数，很小），一律通知；
        各端收到后自拉一次列表，多拉一次的代价远小于 diff 维护成本。

        用 post_message（fire-and-forget）而非 send_message：本方法可能从事件循环
        线程被调用（用户端保存请求就在循环里），send_message 的 .result() 会自锁。
        发送失败静默——连接已断的客户端重连后自然会重拉列表。
        """
        payload = json.dumps({"type": "chat_agents_changed", "protocol": 2})
        sent = 0
        for ctx in list(self.token_manager.contexts.values()):
            for client in list(ctx.clients.values()):
                ws_client = client.ws_client
                post = getattr(ws_client, "post_message", None) if ws_client is not None else None
                if post is None: continue
                try:
                    post(payload)
                    sent += 1
                except Exception:
                    continue
        return sent

    def snapshot_contexts(self, token_resolver=None):
        # 会话池按 client_id 隔离：每个 context 即一个 CDP 客户端。保留
        # {contexts:[{clients:[...]}]} 外层形状以兼容前端扁平化；context_id
        # 就是 client_id（不再有跨客户端的 user 归属）。
        result = []
        for client_key, ctx in self.token_manager.contexts.items():
            clients = []
            for cid, client in ctx.clients.items():
                pages = [{"id": p.id, "session_key": p.id, "page_id": p.info.get("page_id"), "url": p.url, "title": p.info.get("title", ""), "active": p.is_active(), "status": "connected" if p.is_active() else "disconnected", "connected_at": p.connect_at, "connect_at": p.connect_at, "disconnect_at": p.disconnect_at} for p in client.pages.values()]
                clients.append({"client_id": cid, "name": client.name, "connected": client.ws_client is not None, "proxy_connected": client.proxy_ws_client is not None, "pages": pages, "active_count": sum(p["active"] for p in pages)})
            result.append({"context_id": str(client_key), "default_session_id": ctx.default_session_id, "latest_session_id": ctx.latest_session_id, "clients": clients, "active_count": sum(c["active_count"] for c in clients)})
        return result

    def resolve_session(self, session_id=None, token=None, *, require_active=True) -> Session:
        """Resolve a public composite key or an unambiguous legacy raw tab ID."""
        ctx = self.get_context(token)
        implicit = session_id in (None, "")
        requested = str(session_id) if not implicit else ctx.default_session_id
        page = ctx.sessions.get(requested) if requested else None
        if page is None and requested and ":" not in requested:
            matches = [
                candidate for candidate in ctx.sessions.values()
                if str(candidate.info.get("page_id")) == requested
                and (candidate.is_active() or not require_active)
            ]
            if len(matches) > 1:
                raise ValueError(f"Legacy tab ID {requested} is ambiguous; use client_id:page_id")
            if matches:
                page = matches[0]
        if page is None or (require_active and not page.is_active()):
            # 隐式解析（没指名 session_id，只跟 default 走）而 default 死了：退而挑一个活跃页，
            # 而不是直接抛「未连接」。关键场景：browser_open_tab 要经 default 页中继
            # chrome.tabs.create——当 default 恰好是刚关掉/回收的 duck.ai 页时，若在这里抛，
            # duckai 收割的「换页重试」里 open_tab 自己就 43ms 秒死，永远开不出新页 → 502。
            # 显式指名了 session_id 的照旧抛，让上层（如 duckai _harvest_on_page 换页重试）感知该页已死。
            if implicit and require_active:
                for candidate in ctx.sessions.values():
                    if candidate.is_active():
                        return candidate
            raise ValueError(f"会话ID {requested} 未连接")
        return page

    def raw_tab_id(self, session_id=None, token=None) -> tuple[Session, int | str]:
        page = self.resolve_session(session_id, token)
        raw_id = str(page.info.get("page_id", page.id.split(":", 1)[-1]))
        return page, int(raw_id) if raw_id.isdigit() else raw_id

    def execute_js(self, code, timeout=15, session_id=None, token=None):
        ctx = self.get_context(token)
        requested_session = session_id or current_session_id.get("")
        try:
            page = self.resolve_session(requested_session, token)
        except ValueError:
            # 显式钉了某页（无论是 session_id 实参，还是 SiteServer._call 经
            # current_session_id ContextVar 钉的）就该如实抛「该页未连接」，让上层
            # （如 duckai _harvest_headers 的死页重试）走换页逻辑。只在**没有任何**
            # 显式钉位（裸 agent 调用：session_id 空、ContextVar 也空）时，才退化到
            # 「随便挑一个活跃页」——否则会静默劫持 active[0] 跑到别的 tab 上（串台），
            # 把「钉死本次的页」的契约撕掉，进而收割到别的账号/别的站点的头。
            if requested_session not in (None, ""):
                raise
            active = [candidate for candidate in ctx.sessions.values() if candidate.is_active()]
            if not active: raise ValueError(f"会话ID {session_id or ctx.default_session_id} 未连接")
            page = active[0]
        ctx.default_session_id = page.id
        exec_id = str(uuid.uuid4())
        raw_tab = str(page.info.get("page_id", page.id.split(":", 1)[-1]))
        page.ws_client.send_message(json.dumps({"id": exec_id, "code": code, "tabId": int(raw_tab) if raw_tab.isdigit() else raw_tab}))
        start, acked = time.time(), False
        while exec_id not in ctx.results:
            time.sleep(.2)
            if exec_id in ctx.acks: acked = True
            if time.time() - start > timeout: return {"result": f"No response data in {timeout}s ({'ACK received' if acked else 'no ACK'})"}
        result = ctx.results.pop(exec_id); ctx.acks.pop(exec_id, None)
        if not result["success"]: raise Exception(result["data"])
        return {"data": result["data"], **({"newTabs": result["newTabs"]} if result.get("newTabs") else {})}

    def clean_sessions(self, token=None):
        self.get_context(token).clean_sessions()

    def set_session(self, url_pattern, token=None):
        matched = self.find_session(url_pattern, token)
        if not matched: return False
        ctx = self.get_context(token); ctx.default_session_id = matched[0][0]; return ctx.default_session_id
    def jump(self, url, timeout=10, token=None): return self.execute_js(f"window.location.href={url!r}", timeout, token=token)
