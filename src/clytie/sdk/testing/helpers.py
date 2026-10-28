"""离线假实现和插件契约测试共用的小工具。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from clytie.sdk.cancellation import CancelToken
from clytie.sdk.clock import Clock
from clytie.sdk.types import ToolResult

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
    *,
    allow_cancelled_result: bool = False,
    clock: Clock | None = None,
    grace_s: float = 5,
) -> None:
    """预取消必须传播取消异常；能力也可返回已取消结果。"""
    cancel = CancelToken()
    cancel.cancel("contract-test")
    from .clock import MockClock
    from .lifecycle import with_clock_timeout

    driver = None
    if clock is None:
        clock = MockClock()

        async def advance_budget():
            # 默认针对应立即响应预取消的实现；自定义耗时测试传入并驱动自己的时钟。
            for _ in range(16):
                await checkpoint()
            clock.advance(grace_s)

        driver = asyncio.create_task(advance_budget())
    try:
        result = await with_clock_timeout(operation(cancel), clock, grace_s)
    except asyncio.CancelledError:
        return
    except TimeoutError as exc:
        raise AssertionError("operation exceeded cancellation grace period") from exc
    finally:
        if driver is not None:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
    if allow_cancelled_result and isinstance(result, ToolResult) and result.status == "cancelled":
        return
    raise AssertionError("operation did not propagate Cancelled")


async def assert_task_cancellation(
    operation: Callable[[], Awaitable[T]],
    *,
    started: Awaitable[object] | None = None,
    allow_cancelled_result: bool = False,
) -> None:
    """验证协程取消传播；可传 started 等待实现进入耗时步骤。"""
    task = asyncio.create_task(operation())
    if started is not None:
        await started
    task.cancel()
    try:
        result = await task
    except asyncio.CancelledError:
        return
    if allow_cancelled_result and isinstance(result, ToolResult) and result.status == "cancelled":
        return
    raise AssertionError("operation swallowed asyncio.CancelledError")
