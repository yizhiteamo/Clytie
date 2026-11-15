"""插件实例的文本 WebSocket；宿主负责取消、预算和连接回收。"""

import asyncio
import math
from urllib.parse import urljoin, urlsplit

import aiohttp

from clytie.sdk import WebSocketClosed

from .support import finish_cleanup


class WebSocketBackend:
    def __init__(self, clock, log):
        self._clock, self._log = clock, log
        self._session = None
        self._closed = False
        self._connections = set()
        self._connecting = set()

    @staticmethod
    def _url(url):
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("WebSocket URL scheme must be ws or wss")
        return parsed

    async def _redirect(self, session, context, params):
        source = self._url(str(params.url))
        target = self._url(urljoin(str(params.url), params.response.headers["Location"]))
        if source.scheme == "wss" and target.scheme != "wss":
            raise ValueError("WebSocket redirect scheme downgrade forbidden")

    async def connect(self, url, *, headers=None, timeout_s=10, max_message_bytes=16 * 1024 * 1024):
        if self._closed:
            raise RuntimeError("WebSocket backend is closed")
        if (
            not math.isfinite(timeout_s)
            or timeout_s <= 0
            or type(max_message_bytes) is not int
            or max_message_bytes <= 0
        ):
            raise ValueError("invalid WebSocket budget")
        parsed = self._url(url)
        if self._session is None:
            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(self._redirect)
            self._session = aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=None),
                cookie_jar=aiohttp.DummyCookieJar(),
                trace_configs=[trace],
            )
        owner = asyncio.current_task()
        self._connecting.add(owner)
        started = self._clock.monotonic()
        operation = asyncio.ensure_future(
            self._session.ws_connect(
                url,
                headers=headers,
                ssl=True,
                autoping=True,
                max_msg_size=max_message_bytes,
            )
        )
        timer = asyncio.create_task(self._clock.sleep(timeout_s))
        accepted = False
        try:
            done, _ = await asyncio.wait((operation, timer), return_when=asyncio.FIRST_COMPLETED)
            if operation not in done:
                raise TimeoutError("WebSocket connection budget exceeded")
            try:
                ws = await operation
            except (aiohttp.ClientError, OSError):
                raise ConnectionError("WebSocket connection failed") from None
            connection = WebSocketConnection(self, ws, parsed.hostname, started)
            self._connections.add(connection)
            accepted = True
            return connection
        finally:

            async def cleanup():
                timer.cancel()
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, timer, return_exceptions=True)
                if not accepted and not operation.cancelled() and operation.exception() is None:
                    await operation.result().close()

            _, cancelled = await finish_cleanup(cleanup())
            self._connecting.discard(owner)
            if not accepted:
                self._log.info(
                    "websocket.closed",
                    hostname=parsed.hostname,
                    duration_s=self._clock.monotonic() - started,
                    close_code=None,
                )
            if cancelled:
                if accepted:
                    await connection.close()
                raise asyncio.CancelledError

    async def close(self):
        self._closed = True
        for task in tuple(self._connecting):
            task.cancel()
        await asyncio.gather(*tuple(self._connecting), return_exceptions=True)
        await asyncio.gather(*(connection.close() for connection in tuple(self._connections)))
        if self._session is not None:
            await self._session.close()


class WebSocketConnection:
    def __init__(self, backend, ws, hostname, started):
        self._backend, self._ws = backend, ws
        self._hostname, self._started = hostname, started
        self._close_code = None
        self._logged = False

    @property
    def closed(self):
        return self._ws.closed

    @property
    def close_code(self):
        return self._close_code if self._close_code is not None else self._ws.close_code

    def _raise_closed(self):
        if self.closed:
            raise WebSocketClosed(self.close_code)

    async def send_text(self, text):
        self._raise_closed()
        try:
            await self._ws.send_str(text)
        except asyncio.CancelledError:
            await finish_cleanup(self.close())
            raise
        except (aiohttp.ClientError, OSError):
            await self.close()
            raise WebSocketClosed(self.close_code) from None

    async def receive(self, cancel):
        cancel.raise_if_cancelled()
        self._raise_closed()
        operation = asyncio.create_task(self._ws.receive())
        cancelled = asyncio.create_task(cancel.wait())
        try:
            await asyncio.wait((operation, cancelled), return_when=asyncio.FIRST_COMPLETED)
            cancel.raise_if_cancelled()
            message = await operation
            if message.type == aiohttp.WSMsgType.TEXT:
                return message.data
            if message.type == aiohttp.WSMsgType.BINARY:
                self._close_code = 1003
            elif message.type == aiohttp.WSMsgType.ERROR:
                self._close_code = getattr(message.data, "code", self._ws.close_code)
            await self.close()
            raise WebSocketClosed(self.close_code)
        except asyncio.CancelledError:
            operation.cancel()
            await finish_cleanup(self.close())
            raise
        finally:
            operation.cancel()
            cancelled.cancel()
            await asyncio.gather(operation, cancelled, return_exceptions=True)

    async def close(self):
        try:
            await self._ws.close(code=self._close_code or 1000)
        finally:
            if not self._logged:
                self._logged = True
                self._backend._connections.discard(self)
                self._backend._log.info(
                    "websocket.closed",
                    hostname=self._hostname,
                    duration_s=self._backend._clock.monotonic() - self._started,
                    close_code=self.close_code,
                )
