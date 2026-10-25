"""统一实时网关（Realtime Gateway）：一条 WS 收编全部业务实时通道。

设计（见 docs 方案）：

- **一个客户端实例一条连接**。监听 / 发送 / 订阅 / 退订 / 心跳全部在这条 WS 上，
  按 ``channel`` 多路复用（agent / chat / task / cdp / device / terminal / voice）。
- **连接即身份**：握手阶段完成鉴权（cookie / CDP client token / 内部 token），
  之后所有帧不再带凭据。权限在 subscribe / send 时按频道做归属校验。
- **服务端无状态**：不记「客户端欠什么」。断线 = 连接级退订；重连 = 重新
  subscribe + 重收 snapshot。慢消费者队列满即丢 + ``lagged`` 帧，客户端自行
  拉快照对齐。
- **每条 ref 必回 ack 或 error**：任何失败不允许静默。

帧协议（固定信封）：

    上行：{"type":"subscribe","ref","channel","target"}
          {"type":"unsubscribe","ref","channel","target"}
          {"type":"send","ref","channel","target","action","payload"}
          {"type":"ping","ref"}
    下行：{"type":"hello","protocol","heartbeat","principal"}
          {"type":"ack","ref","data"}
          {"type":"error","ref","code","message","detail"}
          {"type":"snapshot","ref","channel","target","seq","data"}
          {"type":"event","channel","target","seq","event"}
          {"type":"lagged","channel","target"}
          {"type":"pong","ref","ts"}
          {"type":"bye","code","message"}

顺序规则：先登记订阅者、再取快照（快照带 seq，之后 event 从 seq+1 起）；
同一 (channel, target) 内 seq 单调递增；跨 target 无顺序保证。
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter(prefix="/agent", tags=["agent-realtime"])

PROTOCOL_VERSION = 1
HEARTBEAT_SECONDS = 15
# 服务端 60s 收不到任何帧 → 关连接（客户端 15s ping 一次，正常不会触发）。
IDLE_TIMEOUT_SECONDS = 60
# 每订阅者出站队列容量。满了宁可丢也不阻塞业务（丢后发 lagged，客户端重拉快照）。
_QUEUE_SIZE = 256
# 每连接最多订阅数（防订阅炸弹）。
_MAX_SUBSCRIPTIONS = 50
# ref 无回包的客户端侧超时建议值（写进 hello，供客户端参考）。
REF_TIMEOUT_SECONDS = 10

# channel -> 订阅者队列集合。key 是 (channel, target)。
_watchers: dict[tuple[str, str], set[asyncio.Queue]] = {}
# channel -> (target, seq)。seq 按 (channel, target) 独立单调计数。
_seqs: dict[tuple[str, str], int] = {}


# ── 身份 ────────────────────────────────────────────────────────────────


def _parse_cookies(cookie_header: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (cookie_header or "").split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


async def _load_user_role(uid: str) -> str | None:
    """读用户行取 role；不存在/已删/已封/停用一律按未认证处理（fail closed）。"""
    try:
        from user_platform.models import User

        user = await User.get_or_none(id=uid)
        if user is None or user.is_deleted or user.is_blocked or user.status != "active":
            return None
        return str(user.role or "")
    except Exception:  # noqa: BLE001 — 握手期读库失败按未认证处理
        return None


async def resolve_principal(websocket: WebSocket) -> dict[str, Any] | None:
    """握手鉴权：cookie / CDP client token / 内部 token → principal。

    返回 ``{"kind": "user"|"service", "id": ..., "role": ..., "client_id": ...}``，
    失败返回 None（调用方 close(4401)）。

    ``role`` 是任务频道归属校验要用的（``task_service.get_task(..., role=)`` 里
    ``admin`` 走特权分支），所以握手时一并解析——连接即身份，后续帧不再带凭据。
    """
    headers = dict(websocket.headers or {})
    query = dict(websocket.query_params or {})

    # 1) CDP 扩展：X-Cdp-Client-Id + 内部 token → owner user_id。
    client_id = (headers.get("x-cdp-client-id") or query.get("client_id") or "").strip()
    internal = (
        headers.get("x-internal-token")
        or query.get("token")
        or ""
    ).strip()
    if client_id and internal:
        try:
            import os

            expected = os.environ.get("AGENT_INTERNAL_TOKEN", "")
            if expected and internal == expected:
                import mcp_plugin_store

                owner = await mcp_plugin_store.get_cdp_client_owner_user_id(client_id)
                if owner:
                    role = await _load_user_role(str(owner))
                    if role is None:
                        return None
                    return {
                        "kind": "user",
                        "id": str(owner),
                        "role": role,
                        "client_id": client_id,
                    }
        except Exception:  # noqa: BLE001 — 鉴权失败即 None，不抛
            pass
        return None

    # 2) C 端 session cookie → user_id + role。
    try:
        from admin import _current_user_cookie
        from user_platform.session import session_store, USER_SESSION_COOKIE

        cookies = _parse_cookies(headers.get("cookie"))
        cookie = cookies.get(USER_SESSION_COOKIE)
        if cookie:
            _current_user_cookie.set(cookie)
            data = await session_store.get(USER_SESSION_COOKIE, cookie)
            if data and data.uid:
                role = await _load_user_role(str(data.uid))
                if role is None:
                    return None
                return {"kind": "user", "id": str(data.uid), "role": role}
    except Exception:  # noqa: BLE001
        pass

    # 3) 管理员 Bearer → 管理员身份（全量）。id 置 None 是 agent/chat 归属校验的
    #    「管理员可见全部」口径；role=admin 让任务频道走特权分支。
    authorization = (headers.get("authorization") or "").strip()
    if authorization.startswith("Bearer "):
        try:
            from agent.api import _is_valid_admin

            if await _is_valid_admin(authorization):
                return {"kind": "user", "id": None, "role": "admin"}
        except Exception:  # noqa: BLE001
            pass

    # 4) 内部服务 token（无 client_id）→ 服务身份。
    if internal:
        try:
            import os

            if os.environ.get("AGENT_INTERNAL_TOKEN", "") and internal == os.environ.get("AGENT_INTERNAL_TOKEN", ""):
                return {"kind": "service", "id": None, "role": "admin"}
        except Exception:  # noqa: BLE001
            pass

    return None


# ── 权限 ────────────────────────────────────────────────────────────────


async def authorize(channel: str, target: str, principal: dict[str, Any]) -> tuple[bool, int, str]:
    """归属校验：(True, 0, "") 或 (False, code, message)。复用现有实现，不重写。"""
    kind = principal.get("kind")
    pid = principal.get("id")
    if kind == "service":
        return True, 0, ""
    if channel in ("agent", "chat"):
        try:
            from agent import conversation_store

            conv = await conversation_store.get_conversation_owned(pid, target)
            if conv is None:
                return False, 403, "无权访问该对话"
            return True, 0, ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime] authorize agent/chat 失败: {}", exc)
            return False, 500, "鉴权服务异常"
    if channel == "task":
        try:
            from user_platform import task_service as _ts

            # get_task(user_id, task_id, role=) 是任务侧现成的归属闸门：非特权用户
            # 比 task.user_id，特权（role=admin）直接放行。user_id 传 "" 是给特权
            # 身份用的——那条分支根本不看 user_id。
            task = await _ts.task_service.get_task(
                str(pid or ""), target, role=principal.get("role"),
            )
            if task is None:
                return False, 403, "无权访问该任务"
            return True, 0, ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime] authorize task 失败: {}", exc)
            return False, 500, "鉴权服务异常"
    if channel == "cdp":
        client_id = principal.get("client_id") or ""
        if client_id and client_id == target:
            return True, 0, ""
        try:
            import mcp_plugin_store

            owner = await mcp_plugin_store.get_cdp_client_owner_user_id(target)
            if owner and pid and str(owner) == str(pid):
                return True, 0, ""
        except Exception:  # noqa: BLE001
            pass
        return False, 403, "无权访问该浏览器客户端"
    if channel == "subagent":
        # 子 agent 频道：target = child_id。主 agent（同进程，kind=service）可 send
        # kill/send；客户端（kind=user）按**父会话归属**只读订阅子 agent 事件流——
        # 归属经子 agent 的父 conv 判定（子 agent 的 conversation_id = 父 conv）。
        if kind == "service":
            return True, 0, ""
        try:
            from agent import realtime as _rt

            conv_id = _rt.subagent_owner_conversation(target)
            if conv_id:
                from agent import conversation_store

                conv = await conversation_store.get_conversation_owned(pid, conv_id)
                if conv is not None:
                    return True, 0, ""
            return False, 403, "无权访问该子 agent"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[realtime] authorize subagent 失败: {}", exc)
            return False, 500, "鉴权服务异常"
    # device / terminal / voice：本期只搭架子，默认放行（后续按各自归属收紧）。
    return True, 0, ""


# ── 订阅表 ──────────────────────────────────────────────────────────────


def _key(channel: str, target: str) -> tuple[str, str]:
    return (str(channel or ""), str(target or ""))


def subscribe(channel: str, target: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    _watchers.setdefault(_key(channel, target), set()).add(queue)
    return queue


def unsubscribe(channel: str, target: str, queue: asyncio.Queue) -> None:
    subs = _watchers.get(_key(channel, target))
    if subs is None:
        return
    subs.discard(queue)
    if not subs:
        _watchers.pop(_key(channel, target), None)


async def publish(channel: str, target: str, event: dict) -> int:
    """广播一个业务事件，返回分配的 seq。永不阻塞业务：满队即丢该订阅者。

    队列最多只装到 ``_QUEUE_SIZE - 1``，留一格给 ``lagged``——否则满队时连
    ``lagged`` 都塞不进去，客户端永远收不到「你落后了，去重拉快照」的信号，
    只会安静地烂在原地（这正是 §3.3 要求 lagged 必达的原因）。
    """
    key = _key(channel, target)
    seq = _seqs.get(key, 0) + 1
    _seqs[key] = seq
    subs = _watchers.get(key)
    if not subs:
        return seq
    frame = {"type": "event", "channel": channel, "target": target, "seq": seq, "event": event}
    for queue in tuple(subs):
        try:
            if queue.qsize() >= _QUEUE_SIZE - 1:
                # 溢出：摘掉订阅者、清空积压、只留 lagged。
                #
                # 「清空」不是可选的：泵读到积压里的普通帧就会发现自己已被摘除而
                # 退出，那帧躺在队尾的 lagged 永远轮不到——客户端也就永远不知道
                # 自己落后、不会重拉快照。而且 lagged 的语义本就是「你跟不上了，
                # 以快照为准」，中间那批事件留着也没有意义。
                subs.discard(queue)
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait({"type": "lagged", "channel": channel, "target": target})
                continue
            queue.put_nowait(frame)
        except Exception:  # noqa: BLE001 — 扇出失败不能影响业务
            subs.discard(queue)
    return seq


def current_seq(channel: str, target: str) -> int:
    return _seqs.get(_key(channel, target), 0)


def watcher_count(channel: str, target: str) -> int:
    return len(_watchers.get(_key(channel, target)) or ())


def reset() -> None:
    """仅测试用：清空订阅表与 seq。"""
    _watchers.clear()
    _seqs.clear()
    _subagent_tasks.clear()
    _subagent_conversations.clear()


# ── 快照 ────────────────────────────────────────────────────────────────


# 已接入的频道。方案 §10-3 逐个收编 cdp/terminal/device/voice，未接入前对其
# subscribe/send 明确回 501——不能落进 snapshot 的 None 分支回成
# 「500 快照读取失败」，那会把「还没接」误导成「读挂了」。
_WIRED_CHANNELS = frozenset({"agent", "chat", "task", "subagent"})
_KNOWN_CHANNELS = frozenset({"agent", "chat", "task", "subagent", "cdp", "device", "terminal", "voice"})


# ── 子 agent 注册表（父→子 kill 的定位表）─────────────────────────────────
# child_id (= request_logs.subagent_id / subagent/<id> 频道的 target) → asyncio.Task。
# 子 agent 是同进程协程，父 spawn 时登记、退出时注销。kill 动作按此 cancel 任务。
_subagent_tasks: dict[str, "asyncio.Task"] = {}
# child_id → 父会话 conv_id。authorize 的 user 子频道按父 conv 归属校验。
_subagent_conversations: dict[str, str] = {}


def register_subagent(child_id: str, task, parent_conv_id: str = "") -> None:
    """登记一个子 agent 的 asyncio.Task（父 spawn 时调用）。"""
    if child_id and task is not None:
        _subagent_tasks[str(child_id)] = task
        if parent_conv_id:
            _subagent_conversations[str(child_id)] = str(parent_conv_id)


def unregister_subagent(child_id: str) -> None:
    """注销子 agent（子的 _run_subagent_task 退出时调用）。"""
    _subagent_tasks.pop(str(child_id), None)
    _subagent_conversations.pop(str(child_id), None)


def kill_subagent(child_id: str) -> bool:
    """cancel 一个子 agent 的任务；返回是否命中。任务自身 finally 会发 subagent_end。"""
    task = _subagent_tasks.get(str(child_id))
    if task is None or task.done():
        return False
    task.cancel()
    return True


def has_subagent(child_id: str) -> bool:
    return str(child_id) in _subagent_tasks


def subagent_owner_conversation(child_id: str) -> str:
    """子 agent 的父会话 conv_id（authorize 用）；未知返回空串。"""
    return _subagent_conversations.get(str(child_id), "")


async def snapshot(channel: str, target: str, principal: dict[str, Any]) -> tuple[list | dict | None, int]:
    """取订阅时刻的权威状态 + 当前 seq。失败返回 (None, seq)。

    带 ``principal`` 是因为任务频道的快照读取要走任务侧的归属校验
    （``list_task_events`` 内部 ``_owned_task`` 比对 user_id/role）。
    """
    if channel not in _KNOWN_CHANNELS:
        raise _ActionError(422, f"未知频道: {channel}")
    if channel not in _WIRED_CHANNELS:
        raise _ActionError(501, f"频道尚未接入: {channel}")
    seq = current_seq(channel, target)
    try:
        if channel in ("agent", "chat"):
            from agent import conversation_store

            messages = await conversation_store.get_messages(target, timeline=True)
            return messages, seq
        if channel == "task":
            from user_platform import task_service as _ts

            # list_task_events 返回 {rows, next_before}；rows 是按 (seq,id) 稳定
            # 排序的历史页，直接作为快照 data 下发。
            page = await _ts.task_service.list_task_events(
                str(principal.get("id") or ""),
                target,
                limit=200,
                role=principal.get("role"),
            )
            return page.get("rows") or [], seq
        if channel == "subagent":
            # 子 agent 事件流是实时的（one-shot 子不落对话历史）：快照返回空，
            # 客户端订阅后收后续 live 帧。子已结束则返回空 + 当前 seq。
            return [], seq
    except _ActionError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[realtime] snapshot {}:{} 失败: {}", channel, target, exc)
    return None, seq


# ── WS 端点 ─────────────────────────────────────────────────────────────


async def _send(ws: WebSocket, frame: dict) -> None:
    await ws.send_text(json.dumps(frame, ensure_ascii=False))


@router.websocket("/realtime")
async def realtime(websocket: WebSocket):
    """统一实时网关：一条连接承载全部业务频道的订阅/发送/心跳。"""
    principal = await resolve_principal(websocket)
    if principal is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await _send(websocket, {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "heartbeat": HEARTBEAT_SECONDS,
        "principal": {"kind": principal.get("kind"), "id": principal.get("id")},
    })

    # 本连接的订阅：(channel, target) -> 出站队列。断开即全部退订。
    subs: dict[tuple[str, str], asyncio.Queue] = {}
    pumps: dict[tuple[str, str], asyncio.Task] = {}  # key -> pump task ref
    outbox: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE * 4)
    sender_done = asyncio.Event()
    sender = asyncio.create_task(_sender_loop(websocket, outbox, sender_done))

    async def reply(frame: dict) -> None:
        try:
            outbox.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning("[realtime] outbox full, dropping ack/error frame")

    def drop_subscription(key: tuple[str, str]) -> None:
        """摘掉本连接的一个订阅：退订 hub 队列 + 撤泵。幂等。"""
        pump = pumps.pop(key, None)
        if pump is not None and not pump.done():
            pump.cancel()
        queue = subs.pop(key, None)
        if queue is not None:
            unsubscribe(key[0], key[1], queue)
        if key[0] == "task":
            # 引用计数：最后一个退订收线共享 follower。
            try:
                from user_platform.task_service import release_task_follower

                release_task_follower(key[1])
            except Exception:  # noqa: BLE001
                pass

    async def handle_subscribe(msg: dict) -> None:
        ref = msg.get("ref")
        channel = str(msg.get("channel") or "")
        target = str(msg.get("target") or "")
        if not channel or not target:
            await reply({"type": "error", "ref": ref, "code": 422, "message": "channel 与 target 必填"})
            return
        if len(subs) >= _MAX_SUBSCRIPTIONS:
            await reply({"type": "error", "ref": ref, "code": 429, "message": "订阅数超限"})
            return
        ok, code, message = await authorize(channel, target, principal)
        if not ok:
            await reply({"type": "error", "ref": ref, "code": code, "message": message})
            return
        key = _key(channel, target)
        if key in subs:
            await reply({"type": "ack", "ref": ref, "data": {"channel": channel, "target": target, "resumed": True}})
            return
        # 先登记订阅者、再取快照：快照读完前到达的 live 帧只进队列，
        # 快照带 seq，之后 event 从 seq+1 起 → 快照与增量无空窗。
        queue = subscribe(channel, target)
        subs[key] = queue
        if channel == "task":
            # 任务频道是拉取式生成器：没有驱动者就没有事件。首个订阅者拉起共享
            # follower（一个任务一条 node 流），最后一个退订由 drop_subscription 收线。
            from user_platform.task_service import acquire_task_follower

            await acquire_task_follower(
                target, str(principal.get("id") or ""), principal.get("role"),
            )
        try:
            data, seq = await snapshot(channel, target, principal)
        except _ActionError as exc:
            drop_subscription(key)
            await reply({"type": "error", "ref": ref, "code": exc.code, "message": exc.message})
            return
        if data is None:
            drop_subscription(key)
            await reply({"type": "error", "ref": ref, "code": 500, "message": "快照读取失败"})
            return
        await reply({
            "type": "snapshot", "ref": ref, "channel": channel, "target": target,
            "seq": seq, "data": data,
        })
        pump = asyncio.create_task(_pump_to_outbox(channel, target, queue, outbox))
        pumps[key] = pump
        pump.add_done_callback(lambda t, k=key: pumps.pop(k, None) if pumps.get(k) is t else None)

    async def handle_unsubscribe(msg: dict) -> None:
        ref = msg.get("ref")
        channel = str(msg.get("channel") or "")
        target = str(msg.get("target") or "")
        if channel and target:
            drop_subscription(_key(channel, target))
        elif channel:
            for key in [k for k in subs if k[0] == channel]:
                drop_subscription(key)
        else:
            for key in list(subs):
                drop_subscription(key)
        await reply({"type": "ack", "ref": ref, "data": {"ok": True}})

    async def handle_send(msg: dict) -> None:
        ref = msg.get("ref")
        channel = str(msg.get("channel") or "")
        target = str(msg.get("target") or "")
        action = str(msg.get("action") or "")
        payload = msg.get("payload") or {}
        if not channel or not target or not action:
            await reply({"type": "error", "ref": ref, "code": 422, "message": "channel / target / action 必填"})
            return
        ok, code, message = await authorize(channel, target, principal)
        if not ok:
            await reply({"type": "error", "ref": ref, "code": code, "message": message})
            return
        try:
            result = await dispatch_action(channel, target, action, payload, principal)
        except _ActionError as exc:
            await reply({"type": "error", "ref": ref, "code": exc.code, "message": exc.message, "detail": exc.detail})
            return
        except Exception as exc:  # noqa: BLE001 — 任何异常必回 error，不静默
            logger.warning("[realtime] send {}:{}:{} 失败: {}", channel, target, action, exc)
            await reply({"type": "error", "ref": ref, "code": 500, "message": "服务异常", "detail": str(exc)[:300]})
            return
        await reply({"type": "ack", "ref": ref, "data": result})

    try:
        while True:
            # 同时等「收到一帧」与「写者退出」：写者死 = 连接已断，若只等接收会
            # 空转满 60s idle 超时，期间客户端的每条请求都得不到回包。
            recv = asyncio.create_task(websocket.receive_text())
            done, _pending = await asyncio.wait(
                {recv, sender}, timeout=IDLE_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                # 60s 无帧无写失败 → idle 超时收线。
                recv.cancel()
                break
            if sender in done:
                # 写者退出（发送失败或已收线）→ 连接结束。
                recv.cancel()
                break
            try:
                raw = recv.result()
            except WebSocketDisconnect:
                break
            except Exception:  # noqa: BLE001 — 单帧接收异常按断线处理
                break
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            kind = str(msg.get("type") or "")
            if kind == "ping":
                await reply({"type": "pong", "ref": msg.get("ref"), "ts": int(time.time() * 1000)})
            elif kind == "subscribe":
                await handle_subscribe(msg)
            elif kind == "unsubscribe":
                await handle_unsubscribe(msg)
            elif kind == "send":
                await handle_send(msg)
            else:
                await reply({"type": "error", "ref": msg.get("ref"), "code": 422, "message": f"未知帧类型: {kind}"})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        sender.cancel()
        for pump in pumps.values():
            pump.cancel()
        pumps.clear()
        for (channel, target), queue in subs.items():
            unsubscribe(channel, target, queue)
        subs.clear()


async def _pump_to_outbox(channel: str, target: str, queue: asyncio.Queue, outbox: asyncio.Queue) -> None:
    """把 hub 队列的帧搬进连接出站队列。订阅被摘除（退订/断线）即退出。

    两件事要同时成立，顺序不能反：

    - ``lagged`` 帧是 publish 在把该订阅者摘出 ``_watchers`` **之后**才塞进队列的，
      所以「已摘除」不能当作直接 return 的信号——否则唯一的 lagged 帧会被丢掉，
      客户端永远不知道自己落后、也就永远不会重拉快照。先把它转出去站再退出。
    - 退订只把队列从 ``_watchers`` 摘走、不再有人 ``put``，泵若死等 ``get()``
      会泄漏一个唤不醒的 task。挂在 5s 超时上定期自查（最坏多活 5s，不泄漏）。
    """
    key = _key(channel, target)

    def _watched() -> bool:
        subs = _watchers.get(key)
        return subs is not None and queue in subs

    def _flush_lagged_then_return() -> None:
        """已摘除：扫队列捞出唯一的 lagged 帧转出去，再退出。

        publish 摘除时已把积压清空、只留一帧 lagged；但泵可能先读到积压里的
        普通帧——那种情况下 lagged 还躺在队尾，必须在退出前把它捞出来，否则
        客户端永远收不到「你落后了，去重拉快照」（§3.3 lagged 必达）。
        """
        while True:
            try:
                leftover = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if leftover.get("type") == "lagged":
                try:
                    outbox.put_nowait(leftover)
                except asyncio.QueueFull:
                    pass
                return

    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                if not _watched():
                    return
                continue
            if frame.get("type") == "lagged":
                # 溢出补偿帧：无论是否仍被订阅都要送达，然后结束本泵。
                try:
                    outbox.put_nowait(frame)
                except asyncio.QueueFull:
                    pass
                return
            if not _watched():
                _flush_lagged_then_return()
                return
            try:
                outbox.put_nowait(frame)
            except asyncio.QueueFull:
                # outbox 满 = 连接写不动（网速极慢/假死）。退出泵；客户端靠
                # lagged/重连兜底，服务端不替它攒。
                return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass


async def _sender_loop(websocket: WebSocket, outbox: asyncio.Queue, done: asyncio.Event) -> None:
    """连接唯一的写者：串行写 WS。写失败 = 连接已死，set(done) 让接收循环退出。"""
    try:
        while True:
            frame = await outbox.get()
            await websocket.send_text(json.dumps(frame, ensure_ascii=False))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass
    finally:
        done.set()


class _ActionError(Exception):
    def __init__(self, code: int, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


async def dispatch_action(
    channel: str, target: str, action: str, payload: dict, principal: dict[str, Any],
) -> dict:
    """把 send 动作分发到现有 HTTP handler。只转发，不重写业务。

    handler 是 FastAPI 路由函数，但首参都接受显式传入（``Depends`` 仅在框架调用
    时才生效），直接 ``await`` 即可。它们抛 ``HTTPException``，这里统一转成
    ``_ActionError``，``code`` 透传——404/403/409 的口径与 REST 一致。
    """
    if channel in ("agent", "chat"):
        from agent import api as agent_api
        from fastapi import HTTPException

        caller = principal.get("id")
        try:
            if action == "send":
                # 复用 send_message：它自己做归属校验、孤儿 streaming 对账、409
                # 运行中拦截、reasoning_effort 归一、占位消息创建、轮次启动。
                # 这里只负责把 payload 塞进 SendMessageRequest——agent_loop 事件
                # 走 hub 广播，WS 订阅者自然收到，发起方与旁观方同一序列。
                content = str((payload or {}).get("content") or "").strip()
                if not content:
                    raise _ActionError(422, "消息不能为空")
                fields = ("content", "max_turns", "terminal_context",
                          "reasoning_effort", "mode", "goal_config")
                kwargs = {
                    k: (payload or {})[k]
                    for k in fields
                    if k in (payload or {}) and (payload or {})[k] is not None
                }
                kwargs.setdefault("content", content)
                try:
                    req = agent_api.SendMessageRequest(**kwargs)
                except Exception as exc:  # pydantic ValidationError → 422
                    raise _ActionError(422, f"消息参数不合法: {exc}") from exc
                return await agent_api.send_message(target, req, caller)
            if action == "retry":
                message_id = (payload or {}).get("message_id")
                if message_id is None:
                    raise _ActionError(422, "message_id 必填")
                return await agent_api.retry_message(target, int(message_id), caller)
            if action == "abort":
                return await agent_api.abort_conversation(target, caller)
            raise _ActionError(422, f"未知动作: {action}")
        except HTTPException as exc:
            # 透传 status_code（404 对话不存在 / 403 越权节点 / 409 本轮在跑），
            # detail 原样回给客户端——不翻译、不重写。
            raise _ActionError(exc.status_code, str(exc.detail)) from exc
    if channel == "task":
        from user_platform import task_service as _ts

        uid = str(principal.get("id") or "")
        role = principal.get("role")
        try:
            if action == "cancel":
                return await _ts.task_service.cancel_task_turn(uid, target, role=role)
            if action == "stop":
                ok = await _ts.task_service.stop_task(uid, target, role=role)
                return {"ok": bool(ok)}
            if action == "control":
                # control 是「把已停的任务再驱动起来」的通用入口：start 重启已停
                # 运行时、restart 清状态重启。具体走哪个由 payload.action 决定，
                # 不在协议里另造动词——复用 task_service 现有 handler。
                sub = str((payload or {}).get("action") or "").strip().lower()
                if sub == "start":
                    return await _ts.task_service.start_task_runtime(uid, target, role=role)
                if sub == "restart":
                    return await _ts.task_service.restart_task_runtime(
                        uid, target, load_session=bool((payload or {}).get("load_session", True)),
                        role=role,
                    )
                raise _ActionError(422, "control 需指定 payload.action: start|restart")
            raise _ActionError(422, f"未知动作: {action}")
        except ValueError as exc:
            # _owned_task_or_raise / _live_node_session_id 用 ValueError 表态：
            # task_not_found → 404，runtime_not_bound → 409，node_server_unavailable → 503。
            reason = str(exc)
            code = 404 if "not_found" in reason else (409 if "runtime" in reason else 503)
            raise _ActionError(code, reason) from exc
    if channel == "subagent":
        # 子 agent 控制：Phase 1 只做 kill（cancel 子任务）。interrupt/send 留后续。
        # 这是**用户/前端**发起的「停掉这个子 agent」——父 agent 在 one-shot 下正
        # await 子，无法并发 kill 自己的子；父若要中止走 abort_conversation。
        if action == "kill":
            ok = kill_subagent(target)
            if not ok:
                raise _ActionError(404, f"子 agent 不存在或已结束: {target}")
            return {"ok": True, "subagent_id": target, "killed": True}
        raise _ActionError(422, f"未知动作: {action}")
    if channel in _KNOWN_CHANNELS:
        # 已知但还没收编的频道（§10-3 的用户）：明确 501，别回成「未知频道」。
        raise _ActionError(501, f"频道尚未接入: {channel}")
    raise _ActionError(422, f"未知频道: {channel}")


__all__ = [
    "router", "resolve_principal", "authorize", "subscribe", "unsubscribe",
    "publish", "current_seq", "watcher_count", "reset", "snapshot",
    "dispatch_action", "PROTOCOL_VERSION", "HEARTBEAT_SECONDS",
]
