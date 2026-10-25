"""进程内对话事件扇出（任务侧 ``follow_session_events`` 的进程内等价物）。

聊天侧把「发送」与「订阅」拆开后，一轮 agent 运行产生的事件不再只进发起方那条
HTTP 响应的单消费者队列，而是广播给**当前订阅该会话的全部连接**——发起方与旁观方
（同浏览器其它标签页、Web 控制台、手机）消费同一条序列。

设计约束（有意为之）：

- **只存连接，不存事件**。``_watchers`` 是进程内存里的一张 ``conv_id → set[q]``，
  不是数据库表，也没有环形缓冲/seq 重放。断线的客户端自己重连 + REST 拉一次历史
  （``get_messages``，权威在 ClickHouse）再重新订阅，服务端不替任何客户端补数据。
- **绝不阻塞生产者**。``publish`` 跑在 agent 的事件回调里，只做 ``put_nowait``；
  慢消费者满队即丢最旧 delta（由 ``checkpoint`` 的落库兜底，重连时回读）——一个假死的
  手机不可能把 agent 主循环冻在半路。
- **丢帧要吭声**。满队丢 delta 时，下一次投递前补一帧 ``{"type":"reset"}``（按
  ``_RESET_MIN_INTERVAL_S`` 节流，避免风暴），客户端据此重拉权威快照补缺口。与
  ``realtime.py`` 溢出时的 ``lagged`` 帧同义——服务端不为不可靠传输做送达保证，
  只在丢帧时给一个「你落后了」的信号，重拉是客户端的事。
- **进程重启即空**。客户端重连重拉，无需任何服务端恢复逻辑。
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger

# 每个订阅者队列的容量。够覆盖一次快照后的增量突发；满了宁可丢也不阻塞 agent。
_QUEUE_SIZE = 256

# 溢出丢帧后补发 reset 帧的最小间隔（秒）。队列持续被打满时，每丢一帧都发一次
# reset 会让客户端陷入「不停重拉快照」的风暴——按时间节流：一轮持续溢出里只发
# 一次，客户端重拉一次就够把前缀对齐，之后的溢出等下一次窗口再报。
_RESET_MIN_INTERVAL_S = 3.0

# 某订阅者队列上一轮溢出丢过帧、且已过节流窗口 → 待发一帧 reset。
_overflow_dirty: set[asyncio.Queue] = set()
# 每个订阅者上次发 reset 的时间（monotonic），节流用。
_last_reset_at: dict[asyncio.Queue, float] = {}

# terminal 事件（结束/异常/提问/状态切换）一旦丢了，前端状态栏会永远停在
# 「处理中」，只能靠手动刷新恢复——这正是「agent 已结束但页面还卡处理中」的
# 根因。中间帧（content/reasoning delta）丢了可以靠 reset 重拉补全。故满队时
# 优先丢最旧的中间帧给新事件腾位，terminal 帧绝不主动丢。
_TERMINAL = ("done", "error", "question", "conversation_state", "end", "aborted")

# conv_id -> 该会话当前的订阅者队列
_watchers: dict[str, set[asyncio.Queue]] = {}


def subscribe(conv_id: str) -> asyncio.Queue:
    """登记一个订阅者，返回它专属的出站队列。

    调用方负责在连接结束时 ``unsubscribe``——本模块不追踪谁还活着，
    谁断开谁清理（与「服务端不擦屁股」同一条边界）。
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
    _watchers.setdefault(conv_id, set()).add(queue)
    return queue


def unsubscribe(conv_id: str, queue: asyncio.Queue) -> None:
    subs = _watchers.get(conv_id)
    # 订阅者走了，它专属的溢出记账跟着清（否则队列对象被这两张表钉住回收不掉）。
    _overflow_dirty.discard(queue)
    _last_reset_at.pop(queue, None)
    if subs is None:
        return
    subs.discard(queue)
    if not subs:
        _watchers.pop(conv_id, None)


async def publish(conv_id: str, event: dict) -> None:
    """把一个事件投递给该会话的全部订阅者。非阻塞：满队即丢该订阅者。

    同时转发给统一实时网关（``agent/realtime``，channel="agent"）：WS 订阅者
    与这里的 SSE 订阅者消费的是**同一条业务序列**，不是两条独立流。转发在
    本模块做而不是 15 个生产点各加一行——一处一行（方案 §10-2）。
    """
    from agent import realtime

    await realtime.publish("agent", conv_id, event)

    subs = _watchers.get(conv_id)
    if not subs:
        return
    is_terminal = event.get("type") in _TERMINAL
    for queue in tuple(subs):
        try:
            _deliver(queue, event, is_terminal)
        except Exception:  # noqa: BLE001 — 扇出失败不能影响 agent 运行
            logger.debug("[conversation_hub] fanout failed conv={}", conv_id, exc_info=True)


def _deliver(queue: asyncio.Queue, event: dict, is_terminal: bool) -> None:
    """给单个订阅者投递一帧；满了就丢最旧腾位，并把这次丢帧登记成待补的 reset。

    关键差异（原先这里是静默丢帧）：客户端只有知道自己「落后了」才会去重拉权威
    快照。丢帧不吭声 = 缺口永远不补，面板上表现为思考过程/正文被截断。所以非
    terminal 被丢时置 ``_overflow_dirty``，下一次投递前补一帧 ``reset``——语义与
    realtime.py 溢出时发的 ``lagged`` 帧一致，只是这里保留订阅者不摘（本模块的
    「只丢 delta、保订阅者」哲学不变，补一个信号即可）。
    """
    # 上轮丢过帧、且过节流窗口 → 先把 reset 补进去，让客户端去重拉。
    if queue in _overflow_dirty:
        now = time.monotonic()
        if now - _last_reset_at.get(queue, 0.0) >= _RESET_MIN_INTERVAL_S:
            if _squeeze_reset(queue):
                _overflow_dirty.discard(queue)
                _last_reset_at[queue] = now
            # 挤不进去（队里只剩 terminal 帧）：留着标记，下一轮再补。

    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass

    if is_terminal:
        # 腾位：先丢最旧的非 terminal 帧；仍满则丢最旧的 terminal（保最新终态）。
        kept: list[dict] = []
        while not queue.empty():
            try:
                old = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if old.get("type") in _TERMINAL:
                kept.append(old)
        for old in kept:
            try:
                queue.put_nowait(old)
            except asyncio.QueueFull:
                break
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
        # 为了挤进终态可能又丢了几帧 delta，同样要补 reset。
        _mark_overflow(queue)
    else:
        # 非 terminal：丢最旧腾位，订阅者保留；这次丢帧记下来待补 reset。
        try:
            queue.get_nowait()
            queue.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass
        _mark_overflow(queue)


def _squeeze_reset(queue: asyncio.Queue) -> bool:
    """往满队里挤一帧 reset，尽量不丢 terminal 帧；挤不进去返回 False。

    满队时倒出全部：非 terminal 帧全部丢掉（它们本就是溢出源，慢订阅者已经落后
    了，靠 reset 重拉补全），terminal 帧保留重排。腾出的位置放 reset——reset
    在前、terminal 在后，让客户端先收到「去重拉」再收到后续终态。队列里 terminal
    帧多到一个都腾不出时放弃本轮（返回 False），保持 dirty 标记下一轮再补。
    """
    reset = {"type": "reset"}
    try:
        queue.put_nowait(reset)
        return True
    except asyncio.QueueFull:
        pass
    kept: list[dict] = []
    while not queue.empty():
        try:
            old = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if old.get("type") in _TERMINAL:
            kept.append(old)
    # 队列已排空，reset 一定放得下。
    try:
        queue.put_nowait(reset)
    except asyncio.QueueFull:  # pragma: no cover — 刚排空不会满
        for old in kept:
            try:
                queue.put_nowait(old)
            except asyncio.QueueFull:
                break
        return False
    # terminal 帧排到 reset 之后。
    for old in kept:
        try:
            queue.put_nowait(old)
        except asyncio.QueueFull:
            break
    return True


def _mark_overflow(queue: asyncio.Queue) -> None:
    """登记一次丢帧，下一轮投递前补 reset（按 _RESET_MIN_INTERVAL_S 节流）。"""
    now = time.monotonic()
    if now - _last_reset_at.get(queue, 0.0) >= _RESET_MIN_INTERVAL_S:
        _overflow_dirty.add(queue)


def watcher_count(conv_id: str) -> int:
    """当前订阅该会话的连接数（诊断/测试用）。"""
    return len(_watchers.get(conv_id) or ())


def is_watching(conv_id: str) -> bool:
    """是否还有任何连接在看（测试与「是否需要广播」的快速判断用）。"""
    return bool(_watchers.get(conv_id))


def reset() -> None:
    """仅测试用：清空全部订阅与溢出记账。"""
    _watchers.clear()
    _overflow_dirty.clear()
    _last_reset_at.clear()


__all__ = ["subscribe", "unsubscribe", "publish", "watcher_count", "is_watching", "reset"]
