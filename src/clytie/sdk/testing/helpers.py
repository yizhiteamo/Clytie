"""离线假实现和插件契约测试共用的小工具。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from clytie.sdk.cancellation import Cancelled, CancelToken
from clytie.sdk.clock import Clock

T = TypeVar("T")


async def checkpoint() -> None:
    """只让出一次事件循环，不等待真实时间。"""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def ready() -> None:
        if not future.done():
            future.set_result(None)

    loop.call_soon(ready)
    await future


async def cancellable_wait(clock: Clock, seconds: float, cancel: CancelToken) -> None:
    """假实现的可取消耗时步骤；时间仅来自注入时钟。"""
    cancel.raise_if_cancelled()
    sleeper = asyncio.create_task(clock.sleep(seconds))
    cancelled = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait((sleeper, cancelled), return_when=asyncio.FIRST_COMPLETED)
        cancel.raise_if_cancelled()
        await sleeper
    finally:
        for task in (sleeper, cancelled):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleeper, cancelled, return_exceptions=True)


async def assert_token_cancellation(
    operation: Callable[[CancelToken], Awaitable[T]],
) -> None:
    """预先取消的令牌必须传播 SDK 的 Cancelled。"""
    cancel = CancelToken()
    cancel.cancel("contract-test")
    try:
        await operation(cancel)
    except Cancelled:
        return
    raise AssertionError("operation did not propagate Cancelled")


async def assert_task_cancellation(
    operation: Callable[[], Awaitable[T]],
    *,
    started: Awaitable[object] | None = None,
) -> None:
    """验证协程取消传播；可传 started 等待实现进入耗时步骤。"""
    task = asyncio.create_task(operation())
    if started is not None:
        await started
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    raise AssertionError("operation swallowed asyncio.CancelledError")
