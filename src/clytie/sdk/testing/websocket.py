"""不访问网络的文本 WebSocket 假实现。"""

import asyncio

from clytie.sdk import WebSocketClosed


class FakeWebSocketConnection:
    def __init__(self, messages=(), *, max_message_bytes=16 * 1024 * 1024):
        self.closed, self.close_code = False, None
        self.sent = []
        self.messages = asyncio.Queue()
        self.max_message_bytes = max_message_bytes
        for message in messages:
            self.messages.put_nowait(message)

    async def send_text(self, text):
        if self.closed:
            raise WebSocketClosed(self.close_code)
        if not isinstance(text, str):
            raise TypeError("text required")
        self.sent.append(text)

    async def receive(self, cancel):
        cancel.raise_if_cancelled()
        if self.closed:
            raise WebSocketClosed(self.close_code)
        read = asyncio.create_task(self.messages.get())
        cancelled = asyncio.create_task(cancel.wait())
        try:
            await asyncio.wait((read, cancelled), return_when=asyncio.FIRST_COMPLETED)
            cancel.raise_if_cancelled()
            value = await read
            if self.closed:
                raise WebSocketClosed(self.close_code)
            if not isinstance(value, str) or len(value.encode()) > self.max_message_bytes:
                await self.close()
                raise WebSocketClosed(self.close_code)
            return value
        except BaseException:
            await self.close()
            raise
        finally:
            for task in (read, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(read, cancelled, return_exceptions=True)

    async def close(self):
        if not self.closed:
            self.closed, self.close_code = True, 1000
            self.messages.put_nowait(None)


class FakeWebSocket:
    def __init__(self, connections=()):
        self.connections = list(connections)
        self.opened = []

    async def connect(self, url, *, headers=None, timeout_s=10, max_message_bytes=16 * 1024 * 1024):
        if not url.startswith(("ws://", "wss://")) or timeout_s <= 0 or max_message_bytes <= 0:
            raise ValueError("invalid websocket request")
        connection = self.connections.pop(0) if self.connections else FakeWebSocketConnection()
        connection.max_message_bytes = max_message_bytes
        self.opened.append(connection)
        return connection

    async def close(self):
        for connection in self.opened:
            await connection.close()
