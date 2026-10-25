"""框架层异步适配工具（06 §1 异步约定）。

- 框架层全异步（async def），最外层都是 await。
- 步骤内方法脚本允许同步/异步混用：同步方法放线程池，异步方法直接 await。
- `maybe_await` / `run_sync_or_async` 是两个统一适配入口。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any, Callable

__all__ = ["maybe_await", "run_sync_or_async", "now_ms"]


async def maybe_await(value: Any) -> Any:
    """统一适配「值 / awaitable」（06 §1）。

    - awaitable（协程 / Task / Future）→ 直接 await；
    - 普通返回值 → 原样返回。
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def run_sync_or_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """调用同步或异步的可调用对象（工具 / 方法脚本）。

    - 协程函数 → 直接 await；
    - 同步函数 → 放线程池（`asyncio.to_thread`），不阻塞事件循环；
    - 同步函数返回了 awaitable → 回到事件循环 await（兼容「同步包装异步」）。
    """
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    result = await asyncio.to_thread(functools.partial(func, *args, **kwargs))
    return await maybe_await(result)


def now_ms() -> int:
    """当前墙钟毫秒时间戳（步骤耗时 / 报告用）。"""
    return int(time.time() * 1000)