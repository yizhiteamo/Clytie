"""平台契约；入站归一化由平台内部完成。"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .cancellation import CancelToken
from .types import ChannelFeatures, DeliveryIntent, EventEnvelope, Receipt


@runtime_checkable
class Channel(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[EventEnvelope]: ...

    async def send(self, intent: DeliveryIntent, cancel: CancelToken) -> Receipt: ...

    async def react(self, intent: DeliveryIntent, cancel: CancelToken) -> Receipt: ...

    def features(self) -> ChannelFeatures: ...
