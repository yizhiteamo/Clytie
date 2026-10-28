"""唯一允许插件读取时间的运行时协议。"""

from datetime import datetime, tzinfo
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    @property
    def local_tz(self) -> tzinfo: ...

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...

    async def sleep_until(self, dt: datetime) -> None: ...
