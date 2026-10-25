"""CDP 网络捕获投递点（push 模式帧路由表）。

扩展在 CDP ``Network.*`` 事件上按 captureId 推送 ``net_event`` 帧（见
``tmwd_cdp_bridge/background.js`` 的 handleNetStart/handleDebuggerEvent）。帧经
``mcp_runtime.sse_gateway._route_ext_frame`` 分流到这里，按 captureId 投给等待中的
调用方协程（现 ``SiteServer.capture_start/wait_capture``）。

设计约束：
- **进程内单例注册表**：captureId 由调用方生成（前缀 + 随机后缀），互不冲突。
- **投递不阻塞**：``deliver`` 从扩展 WS 的事件循环里被同步调用，队列无界，投不进去
  只返回 False，绝不 await（否则会拖住整条扩展连接的帧处理）。
- 未注册的 captureId 返回 False，让调用方回落到 driver 帧处理（cdp-bridge 自己的
  ``net_get`` 拉取路径仍要能工作）。

历史：原住 ``mcp_builtin/site_runner/capture.py``，站点服务删除后归位 CDP 管道层
（它本就只依赖帧协议，不依赖站点概念）。
"""
from __future__ import annotations

import asyncio
import uuid

# captureId -> 该捕获的队列。队列无界：抓包速率由浏览器决定，丢弃比阻塞安全。
_QUEUES: dict[str, asyncio.Queue] = {}


def new_capture_id(prefix: str = "cap") -> str:
    return f"{prefix}:{uuid.uuid4().hex[:16]}"


def register(capture_id: str) -> asyncio.Queue:
    """注册一个捕获，返回其事件队列。调用方负责 finally 里 unregister。"""
    queue: asyncio.Queue = asyncio.Queue()
    _QUEUES[str(capture_id)] = queue
    return queue


def unregister(capture_id: str) -> None:
    _QUEUES.pop(str(capture_id), None)


def deliver(frame: dict) -> bool:
    """把扩展推来的 ``net_event`` 帧投给等待中的捕获。

    返回 True 表示该帧已被站点引擎消费（调用方不应再走 driver 路径）。
    """
    capture_id = str(frame.get("captureId") or "")
    if not capture_id:
        return False
    queue = _QUEUES.get(capture_id)
    if queue is None:
        return False
    queue.put_nowait(frame)
    return True


def active_capture_ids() -> list[str]:
    """当前活跃捕获（管理端排障用）。"""
    return sorted(_QUEUES)
