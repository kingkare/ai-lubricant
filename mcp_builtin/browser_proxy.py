"""浏览器代理 Profile 的服务端真相源 + 激活/还原下发 + 数据面出口。

对应 docs/browser-proxy-design.md：

- 出版面（Phase 1/2）：Profile CRUD 落在 PG 表 ``browser_proxy_profiles``（按 client_id 域），
  激活/还原经控制面下发，实际 chrome.proxy 生效由扩展做。
- 数据面（Phase 3，需求 1「浏览器当代理节点」）：``BrowserConnectManager`` 把 HTTP 请求
  经代理通道 WS 下发给扩展、扩展 fetch 后流式回传，接口面与 ``NodeConnectManager`` 同构。
  CDP driver 与 ProxyManager 同处主进程，故这里是**进程内**直连（无 node 那种跨进程 HTTP hop）。
- Agent 只有只读（browser_proxy_status）；改出网路径只归 popup/管理端。

扩展本地也有一份镜像（proxy.js），但那是离线可用性兜底，**真相源在这里**。
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any, AsyncIterator, Optional

from loguru import logger

from server import db

# 单客户端 Profile 数量上限（防止无界膨胀）。
MAX_PROFILES_PER_CLIENT = 200

# 数据面：单请求体上限（与 node 隧道 32MB 同量级）、并发闸（每 client）。
_REQUEST_BODY_LIMIT = 32 << 20
_PER_CLIENT_CONCURRENCY = 4
# 出口请求默认超时（秒）。扩展侧 fetch 也用这个值兜底。
_DEFAULT_EGRESS_TIMEOUT = 60.0
# 等待响应头的额外宽限（首字节前的静默）。
_HEAD_WAIT_GRACE = 5.0


async def _pool():
    """取全局 PG 池；未初始化返回 None（调用方按「存储不可用」处理）。"""
    return db.PostgresClient.pool


async def ensure_schema() -> None:
    """幂等建表（对齐 server/db.py 的 create_tables 风格）。"""
    pool = await _pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS browser_proxy_profiles (
                id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL DEFAULT 'direct',
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                scope TEXT NOT NULL DEFAULT 'regular',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (client_id, id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browser_proxy_profiles_client ON browser_proxy_profiles(client_id)"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_browser_proxy_profiles_updated ON browser_proxy_profiles(updated_at)"
        )


def _profile_row_to_dict(row) -> dict:
    """DB 行 → 扩展原生 Profile 形状。

    落库只存 id/name/type/scope 四列 + 一个 ``config`` JSONB（type 专属子树：
    fixed→代理参数、pac→脚本、auto→规则）。扩展侧 ``sanitizeProfile`` 读的是
    嵌套的 ``fixed``/``pac``/``auto``，故这里把子树展开回对应键，两端形状一致。
    """
    cfg = row["config"] if isinstance(row["config"], dict) else {}
    p = {
        "id": row["id"],
        "client_id": row["client_id"],
        "name": row["name"],
        "type": row["type"],
        "scope": row["scope"],
    }
    ptype = row["type"]
    if ptype in ("fixed", "pac", "auto"):
        p[ptype] = cfg
    return p


def _profile_wire_to_config(profile: dict) -> dict:
    """扩展原生 Profile → DB 的 config 子树（按 type 取对应键）。"""
    ptype = str(profile.get("type") or "direct").strip()
    if ptype in ("fixed", "pac", "auto"):
        sub = profile.get(ptype)
        return sub if isinstance(sub, dict) else {}
    return {}


async def list_profiles(client_id: str) -> list[dict]:
    await ensure_schema()
    pool = await _pool()
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, client_id, name, type, config, scope FROM browser_proxy_profiles "
            "WHERE client_id = $1 ORDER BY updated_at DESC",
            client_id,
        )
    return [_profile_row_to_dict(r) for r in rows]


async def get_profile(client_id: str, profile_id: str) -> Optional[dict]:
    await ensure_schema()
    pool = await _pool()
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, client_id, name, type, config, scope FROM browser_proxy_profiles "
            "WHERE client_id = $1 AND id = $2",
            client_id,
            profile_id,
        )
    return _profile_row_to_dict(row) if row else None


async def upsert_profile(client_id: str, profile: dict) -> dict:
    """新增/更新一个 Profile。返回规范化后的行。

    profile 字段：id / name / type(direct|system|fixed|pac|auto) / scope / config。
    type 决定 config 的必填项，前端/扩展各自校验；这里做白名单校验兜底。
    """
    await ensure_schema()
    pid = str(profile.get("id") or "").strip()
    if not pid:
        raise ValueError("Profile 缺少 id")
    name = str(profile.get("name") or "").strip()[:128]
    ptype = str(profile.get("type") or "direct").strip()
    if ptype not in ("direct", "system", "fixed", "pac", "auto"):
        raise ValueError(f"非法 Profile 类型: {ptype}")
    scope = str(profile.get("scope") or "regular").strip()
    if scope not in ("regular", "incognito_session_only"):
        scope = "regular"
    config = _profile_wire_to_config(profile)

    pool = await _pool()
    if pool is None:
        raise RuntimeError("PostgreSQL 未初始化，无法保存代理 Profile")
    async with pool.acquire() as conn:
        # 上限：同一 client 最多 MAX_PROFILES_PER_CLIENT 条。
        cnt = await conn.fetchval(
            "SELECT COUNT(*) FROM browser_proxy_profiles WHERE client_id = $1", client_id
        )
        existing = await conn.fetchval(
            "SELECT 1 FROM browser_proxy_profiles WHERE client_id = $1 AND id = $2", client_id, pid
        )
        if not existing and cnt >= MAX_PROFILES_PER_CLIENT:
            raise ValueError(f"该客户端 Profile 数量已达上限 {MAX_PROFILES_PER_CLIENT}")
        await conn.execute(
            """
            INSERT INTO browser_proxy_profiles (id, client_id, name, type, config, scope, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, now())
            ON CONFLICT (client_id, id) DO UPDATE
              SET name = EXCLUDED.name, type = EXCLUDED.type, config = EXCLUDED.config,
                  scope = EXCLUDED.scope, updated_at = now()
            """,
            pid, client_id, name, ptype, json.dumps(config, ensure_ascii=False), scope,
        )
    out = {"id": pid, "client_id": client_id, "name": name, "type": ptype, "scope": scope}
    if ptype in ("fixed", "pac", "auto"):
        out[ptype] = config
    return out


async def delete_profile(client_id: str, profile_id: str) -> bool:
    await ensure_schema()
    pool = await _pool()
    if pool is None:
        return False
    async with pool.acquire() as conn:
        res = await conn.execute(
            "DELETE FROM browser_proxy_profiles WHERE client_id = $1 AND id = $2",
            client_id, profile_id,
        )
    return res == "DELETE 1"


async def activate_profile(client_id: str, profile_id: str, lease_ms: int = 0) -> dict:
    """激活某 Profile：校验存在 + 下发意图给扩展 + 记录审计。返回下发结果。

    实际 chrome.proxy 生效由扩展做；若扩展代理通道未连，则下发失败（调用方决定
    是否回滚/报错）。这里不落「生效态」到 PG——生效态是运行态，由 sse_gateway 的
    _PROXY_STATUS 内存表承载。
    """
    from mcp_runtime.sse_gateway import push_proxy_activate

    prof = await get_profile(client_id, profile_id)
    if prof is None:
        raise ValueError(f"Profile 不存在: {profile_id}")
    ok = await push_proxy_activate(client_id, profile=prof, lease_ms=lease_ms)
    # 审计（fire-and-forget，失败不阻断）。
    try:
        pool = await _pool()
        if pool is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO operation_logs (operator, action, target_type, target_name, new_data) "
                    "VALUES ($1, $2, $3, $4, $5::jsonb)",
                    "admin",
                    "browser_proxy_activate",
                    "browser_proxy",
                    client_id,
                    json.dumps({"profile_id": profile_id, "lease_ms": lease_ms, "delivered": ok}, ensure_ascii=False),
                )
    except Exception as exc:  # operation_logs 列结构可能因版本而异，容错。
        logger.debug("browser_proxy audit failed: {}", exc)
    return {"client_id": client_id, "profile_id": profile_id, "delivered": ok}


async def restore_proxy(client_id: str) -> dict:
    """下发「还原出网路径」给扩展。"""
    from mcp_runtime.sse_gateway import push_proxy_restore

    ok = await push_proxy_restore(client_id)
    return {"client_id": client_id, "delivered": ok}


# ── 双向同步 / 跨浏览器导入 ─────────────────────────────────────────────────

async def sync_profiles_from_extension(client_id: str, profiles: list, deleted: list | None = None) -> dict:
    """扩展把本地 Profile 全量推上来 → **全量替换**该 client 的服务端镜像。

    全量替换而非增量：扩展本地是完整集合（popup 可增删），整表对齐比逐条 diff
    更不易漂移。空列表保护：profiles 与 deleted 都为空时 no-op——避免扩展本地
    存储被清空后误把服务端真相源也擦掉。调用方保证 client_id 来自**已鉴权连接**
    （不取帧内字段），故一个浏览器只能覆盖自己的那一行。
    """
    await ensure_schema()
    deleted = [str(d) for d in (deleted or []) if str(d)]
    clean = [p for p in (profiles or []) if isinstance(p, dict) and p.get("id")]
    if not clean and not deleted:
        return {"client_id": client_id, "replaced": 0, "deleted": 0, "noop": True}
    if deleted:
        await delete_profiles(client_id, deleted)
    await replace_profiles(client_id, clean)
    return {"client_id": client_id, "replaced": len(clean), "deleted": len(deleted), "noop": False}


async def delete_profiles(client_id: str, profile_ids: list) -> int:
    if not profile_ids:
        return 0
    pool = await _pool()
    if pool is None:
        return 0
    ids = [str(pid) for pid in profile_ids if str(pid)]
    if not ids:
        return 0
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM browser_proxy_profiles WHERE client_id = $1 AND id = ANY($2::text[])",
            client_id, ids,
        )
    return len(ids)


async def replace_profiles(client_id: str, profiles: list) -> dict:
    """用整组 Profile 替换该 client 的全部服务端镜像（同步的底层动作）。"""
    await ensure_schema()
    pool = await _pool()
    if pool is None:
        raise RuntimeError("PostgreSQL 未初始化")
    keep = [str(p.get("id") or "") for p in (profiles or []) if isinstance(p, dict) and p.get("id")]
    async with pool.acquire() as conn:
        if keep:
            await conn.execute(
                "DELETE FROM browser_proxy_profiles WHERE client_id = $1 AND NOT (id = ANY($2::text[]))",
                client_id, keep,
            )
        else:
            await conn.execute("DELETE FROM browser_proxy_profiles WHERE client_id = $1", client_id)
    saved = 0
    for raw in profiles or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        try:
            await upsert_profile(client_id, raw)
            saved += 1
        except Exception as exc:
            logger.debug("browser_proxy replace skip invalid: {}", exc)
    return {"client_id": client_id, "saved": saved}


async def list_owner_client_ids(owner_user_id: str) -> list[str]:
    """列出某 owner 名下的所有 CDP 客户端 id（跨浏览器导入的候选域）。

    返回的是客户端的对外身份（新客户端随机串、存量回落资源行 id），与
    browser_proxy_profiles.client_id / 会话池隔离键同源。
    """
    if not owner_user_id:
        return []
    import builtin_tool_store

    resources = await builtin_tool_store.list_resources(
        "cdp_client", owner_user_id=owner_user_id, enabled=True,
    )
    return [c for c in (builtin_tool_store.cdp_client_id_of(r) for r in resources) if c]


async def list_profiles_for_client_ids(client_ids: list[str]) -> dict[str, list[dict]]:
    """一次读多个 client 的 Profile（owner 域内）。用于「从另一个浏览器导入」的第 1 步。"""
    await ensure_schema()
    if not client_ids:
        return {}
    pool = await _pool()
    if pool is None:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, client_id, name, type, config, scope FROM browser_proxy_profiles "
            "WHERE client_id = ANY($1::text[]) ORDER BY updated_at DESC",
            [str(c) for c in client_ids],
        )
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["client_id"], []).append(_profile_row_to_dict(r))
    return grouped


async def import_profiles(target_client_id: str, profile_ids: list[str], *, source_client_id: str | None = None) -> dict:
    """把一批 Profile（按 id 列表，来自某 client）复制到目标 client。

    只接受**同一 owner 域**内的 source：跨浏览器导入由调用方先用
    list_owner_client_ids 校验 source_client_id 属于同 owner，再传进来；这里再做一次
    source==target 的短路保护。目标侧 id 冲突时生成新 id（保留 name），避免覆盖目标
    已有配置。
    """
    if not profile_ids:
        return {"imported": 0}
    if source_client_id and str(source_client_id) == str(target_client_id):
        raise ValueError("源浏览器与目标浏览器相同，无需导入")
    grouped = await list_profiles_for_client_ids([str(source_client_id or target_client_id)])
    src_profiles = grouped.get(str(source_client_id or target_client_id), [])
    by_id = {p["id"]: p for p in src_profiles}
    existing = {p["id"] for p in await list_profiles(target_client_id)}
    imported = 0
    for pid in profile_ids:
        src = by_id.get(str(pid))
        if src is None:
            continue
        new = {**src}
        if new["id"] in existing:
            new["id"] = f"{src['id']}_imp{imported}"
        existing.add(new["id"])
        await upsert_profile(target_client_id, new)
        imported += 1
    return {"imported": imported}


# ── 数据面（Phase 3，需求 1）：浏览器当代理节点 ──────────────────────────────

class _BrowserExchange:
    """单次出口请求的回帧聚合器：head/error/end + 流式 chunk 队列。

    由 BrowserConnectManager.request 创建、由 sse_gateway 的代理通道读循环喂帧。
    head_event 在收到 proxy_head 或 proxy_error 时置位（request() 据此等首块）。
    """

    __slots__ = ("proxy_id", "client_id", "queue", "head_event", "status",
                 "headers", "error", "done", "cancelled", "created_at")

    def __init__(self, proxy_id: str, client_id: str):
        self.proxy_id = proxy_id
        self.client_id = client_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.head_event = asyncio.Event()
        self.status: int = 0
        self.headers: list = []
        self.error: Optional[str] = None
        self.done: bool = False
        self.cancelled: bool = False
        self.created_at = asyncio.get_event_loop().time()

    def feed_head(self, status: int, headers: list) -> None:
        self.status = int(status or 0)
        self.headers = headers if isinstance(headers, list) else []
        self.head_event.set()

    def feed_chunk(self, data: bytes) -> None:
        if self.done or self.cancelled:
            return
        self.queue.put_nowait(data)

    def feed_end(self) -> None:
        if self.done or self.cancelled:
            return
        self.done = True
        self.queue.put_nowait(None)  # sentinel

    def feed_error(self, message: str) -> None:
        if self.done or self.cancelled:
            return
        self.error = message
        self.head_event.set()
        self.queue.put_nowait(None)  # 让 iter_chunks 退出


class BrowserProxyResponse:
    """对齐 NodeProxyResponse：``status`` / ``headers`` / ``iter_chunks()`` / ``close()``。"""

    def __init__(self, exchange: _BrowserExchange, manager: "BrowserConnectManager"):
        self._ex = exchange
        self._manager = manager
        self.status = exchange.status
        self.headers = exchange.headers
        self._closed = False

    async def iter_chunks(self) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await self._ex.queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._manager._finish(self._ex)


class BrowserConnectManager:
    """进程内：把 HTTP 请求经浏览器代理通道 WS 转发出去。

    与 ``NodeConnectManager`` 接口同构（``request(client_id, *, method, url, headers, body)``），
    供 ``ProxyManager`` 的 ``browser`` 模式调用。CDP driver 与 ProxyManager 同处主进程，
    故无需跨进程 HTTP hop（与 node 的 RemoteNodeConnectManager 不同）。
    """

    def __init__(self):
        self._pending: dict[str, _BrowserExchange] = {}
        # 每 client 的在途请求数（并发闸，防一条 WS 被大请求占满）。
        self._inflight: dict[str, int] = {}

    def _driver(self):
        try:
            from mcp_builtin.cdp_bridge.server import get_driver
            return get_driver()
        except Exception:
            return None

    def _finish(self, ex: _BrowserExchange) -> None:
        self._pending.pop(ex.proxy_id, None)
        cnt = self._inflight.get(ex.client_id, 0)
        if cnt > 0:
            self._inflight[ex.client_id] = cnt - 1

    async def request(
        self,
        client_id: str,
        *,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: bytes = b"",
        timeout: Optional[float] = None,
        credentials: str = "omit",
    ) -> BrowserProxyResponse:
        driver = self._driver()
        if driver is None:
            raise ConnectionError("CDP driver 未就绪")
        ws = driver.get_proxy_client(client_id)
        if ws is None:
            raise ConnectionError(f"浏览器 {client_id} 的代理通道未连接")

        # 并发闸：单 client 同步在途上限。
        if self._inflight.get(client_id, 0) >= _PER_CLIENT_CONCURRENCY:
            raise ConnectionError(f"浏览器 {client_id} 代理通道并发已满")

        body = body or b""
        if len(body) > _REQUEST_BODY_LIMIT:
            raise ConnectionError(f"出口请求体超限（>{_REQUEST_BODY_LIMIT} 字节）")

        proxy_id = "bp_" + uuid.uuid4().hex[:12]
        ex = _BrowserExchange(proxy_id, client_id)
        self._pending[proxy_id] = ex
        self._inflight[client_id] = self._inflight.get(client_id, 0) + 1

        timeout_s = float(timeout) if timeout and float(timeout) > 0 else _DEFAULT_EGRESS_TIMEOUT
        frame = {
            "type": "proxy_request",
            "proxyId": proxy_id,
            "method": method,
            "url": url,
            "headers": headers or {},
            "bodyB64": base64.b64encode(body).decode("ascii") if body else "",
            "credentials": credentials,
            "timeoutMs": int(timeout_s * 1000),
            "protocol": 2,
        }
        try:
            ws.post_message(json.dumps(frame, ensure_ascii=False))
        except Exception as exc:
            self._finish(ex)
            raise ConnectionError(f"代理通道发送失败: {exc}") from exc

        try:
            await asyncio.wait_for(ex.head_event.wait(), timeout=timeout_s + _HEAD_WAIT_GRACE)
        except asyncio.TimeoutError:
            self._finish(ex)
            # 通知扩展取消（连接可能还在，让其收尾 fetch）。
            try:
                ws.post_message(json.dumps({"type": "proxy_cancel", "proxyId": proxy_id, "protocol": 2}))
            except Exception:
                pass
            raise ConnectionError(f"等待浏览器出口响应超时（{timeout_s:.0f}s）")

        if ex.error:
            self._finish(ex)
            raise ConnectionError(f"浏览器出口失败: {ex.error}")

        return BrowserProxyResponse(ex, self)


# 进程内单例：ProxyManager 注入这个，browser 模式经它转发。
_browser_manager: Optional[BrowserConnectManager] = None


def get_browser_manager() -> BrowserConnectManager:
    global _browser_manager
    if _browser_manager is None:
        _browser_manager = BrowserConnectManager()
    return _browser_manager


# ── sse_gateway 代理通道读循环的帧分发回调 ──────────────────────────────────
# sse_gateway 收到 proxy_head/proxy_end/proxy_error（文本）与二进制 chunk 后调这里。

def deliver_proxy_head(proxy_id: str, status: int, headers: list) -> None:
    ex = _browser_manager_pending().get(proxy_id) if _browser_manager else None
    if ex is not None:
        ex.feed_head(status, headers)


def deliver_proxy_chunk(proxy_id: str, data: bytes) -> None:
    mgr = _browser_manager
    if mgr is None:
        return
    ex = mgr._pending.get(proxy_id)
    if ex is not None:
        ex.feed_chunk(data)


def deliver_proxy_end(proxy_id: str) -> None:
    mgr = _browser_manager
    if mgr is None:
        return
    ex = mgr._pending.get(proxy_id)
    if ex is not None:
        ex.feed_end()


def deliver_proxy_error(proxy_id: str, message: str) -> None:
    mgr = _browser_manager
    if mgr is None:
        return
    ex = mgr._pending.get(proxy_id)
    if ex is not None:
        ex.feed_error(message)


def _browser_manager_pending() -> dict:
    return _browser_manager._pending if _browser_manager else {}

