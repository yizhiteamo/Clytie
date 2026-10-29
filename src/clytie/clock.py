"""本机 CLI 的时钟适配器；业务模块只依赖注入的 Clock。"""

import asyncio
import time
from datetime import datetime, timezone


class SystemClock:
    local_tz = timezone.utc

    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)

    async def sleep_until(self, dt):
        await self.sleep(max(0, (dt - self.now()).total_seconds()))
