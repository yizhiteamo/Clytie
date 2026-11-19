"""平台契约；入站归一化由平台内部完成。"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .cancellation import CancelToken
from .plugin import PluginUnavailable
from .types import ChannelFeatures, DeliveryIntent, EventEnvelope, Receipt


@runtime_checkable
class Channel(Protocol):
    """supports_membership 为真时，另提供 async is_member(conversation_id, actor_id) -> bool | None。

    supports_media_fetch 为真时，另提供 async fetch_media(part: MediaPart, cancel) -> MediaContent。
    两个方法均为可选扩展，原有平台可以继续实现基础协议。
    """

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[EventEnvelope]: ...

    async def send(self, intent: DeliveryIntent, cancel: CancelToken) -> Receipt: ...

    async def react(self, intent: DeliveryIntent, cancel: CancelToken) -> Receipt: ...

    def features(self) -> ChannelFeatures: ...


class ChannelUnavailable(PluginUnavailable):
    """平台连接阶段的结构化失败。"""


def channel_state(channel):
    """兼容没有实现可选状态查询的旧插件；不探测网络。"""
    from .types import ChannelState

    return channel.state() if callable(getattr(channel, "state", None)) else ChannelState()
