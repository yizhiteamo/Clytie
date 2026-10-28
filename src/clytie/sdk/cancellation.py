"""协作式取消；宽限期和协程强制取消由本体负责。"""

from __future__ import annotations

import asyncio
import weakref


class Cancelled(asyncio.CancelledError):
    """SDK 取消，保持 asyncio 取消异常的传播语义。"""


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None
        self._event = asyncio.Event()
        self._children: weakref.WeakSet[CancelToken] = weakref.WeakSet()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str | None = None) -> None:
        """控制端发起取消；首次原因保留，重复调用无副作用。"""
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason
        self._event.set()
        for child in tuple(self._children):
            child.cancel(reason)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise Cancelled(self.reason)

    async def wait(self) -> None:
        await self._event.wait()

    def child(self) -> CancelToken:
        child = CancelToken()
        self._children.add(child)
        if self.cancelled:
            child.cancel(self.reason)
        return child
