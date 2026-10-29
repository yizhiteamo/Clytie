"""确定性时钟：不读取系统时间，也不进行真实等待。"""

from __future__ import annotations

import asyncio
import heapq
import math
from datetime import datetime, timedelta, timezone, tzinfo


class MockClock:
    """以 UTC 保存时间，按截止时间及注册顺序唤醒等待者。

    ``advance`` 和 ``set`` 同步推进时钟；已到期的协程在下一次事件循环
    调度时恢复。它们不会隐式推进由恢复的协程新注册的等待。
    """

    def __init__(
        self,
        start: datetime | None = None,
        *,
        local_tz: tzinfo = timezone.utc,
    ) -> None:
        self._now = self._utc(start or datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.local_tz = local_tz
        self._monotonic = 0.0
        self._sequence = 0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock datetime must include a timezone")
        return value.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    @property
    def pending_sleep_count(self) -> int:
        """仍在等待的定时器数，供测试检查取消后的资源清理。"""
        return sum(not future.done() for _, _, future in self._sleepers)

    def set(self, value: datetime) -> None:
        self.advance((self._utc(value) - self._now).total_seconds())

    def advance(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("clock advancement must be finite and nonnegative")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds
        while self._sleepers and self._sleepers[0][0] <= self._monotonic:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)

    async def sleep(self, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("sleep duration must be finite and nonnegative")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._sequence += 1
        if seconds == 0:
            loop.call_soon(self._wake, future)
        else:
            heapq.heappush(
                self._sleepers,
                (self._monotonic + seconds, self._sequence, future),
            )
        try:
            await future
        finally:
            # 取消立即移除等待项，长截止时间不会持有已取消的任务。
            if seconds and any(item[2] is future for item in self._sleepers):
                self._sleepers = [item for item in self._sleepers if item[2] is not future]
                heapq.heapify(self._sleepers)

    @staticmethod
    def _wake(future: asyncio.Future[None]) -> None:
        if not future.done():
            future.set_result(None)

    async def sleep_until(self, value: datetime) -> None:
        seconds = (self._utc(value) - self._now).total_seconds()
        await self.sleep(max(0.0, seconds))

    async def drain(self, turns: int = 32) -> None:
        """让有界的测试协程链注册下一批等待；不推进应用时间。"""
        for _ in range(turns):
            await self.sleep(0)

    async def advance_and_drain(self, seconds: float) -> None:
        """逐个到期点推进，让唤醒者先注册后续等待，再到目标时间。"""
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("clock advancement must be finite and nonnegative")
        target = self._monotonic + seconds
        await self.drain()
        while self._sleepers and self._sleepers[0][0] <= target:
            self.advance(max(0, self._sleepers[0][0] - self._monotonic))
            await self.drain()
        self.advance(max(0, target - self._monotonic))
        await self.drain()
