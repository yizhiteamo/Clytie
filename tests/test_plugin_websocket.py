"""生产 WebSocket 的回环测试，不使用真实账号。"""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from clytie.plugin_host.support import HostLog
from clytie.sdk import Cancelled, CancelToken
from clytie.sdk.testing.clock import MockClock


@asynccontextmanager
async def server(handler, **kwargs):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    instance = TestServer(app, host="127.0.0.1")
    await instance.start_server(**kwargs)
    try:
        yield (
            str(instance.make_url("/fictional-path?key=fictional-query"))
            .replace("https://", "wss://", 1)
            .replace("http://", "ws://", 1)
        )
    finally:
        await instance.close()


@pytest.fixture
async def backend():
    from clytie.plugin_host.websocket import WebSocketBackend

    instance = WebSocketBackend(MockClock(), HostLog())
    try:
        yield instance
    finally:
        await instance.close()


async def echo(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for message in ws:
        await ws.send_str(message.data)
    return ws


async def test_websocket_roundtrip_and_safe_log(backend, monkeypatch):
    monkeypatch.setenv("WS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    async def handler(request):
        assert request.headers["Authorization"] == "test-token"
        return await echo(request)

    async with server(handler) as url:
        connection = await backend.connect(url, headers=dict(Authorization="test-token"))
        assert not connection.closed
        await connection.send_text("fictional-message")
        assert await connection.receive(CancelToken()) == "fictional-message"
        backend._clock.advance(2)
        await connection.close()
        assert connection.closed
        assert connection.close_code == 1000
        await connection.close()
    assert len(backend._log.entries) == 1
    fields = backend._log.entries[0][2]
    assert fields == {"hostname": "127.0.0.1", "duration_s": 2, "close_code": 1000}


@pytest.mark.parametrize("kind,code", [("binary", 1003), ("oversize", 1009)])
async def test_websocket_rejects_nontext_and_oversize(backend, kind, code):
    from clytie.sdk import WebSocketClosed

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if kind == "binary":
            await ws.send_bytes(b"fictional")
        else:
            await ws.send_str("界" * 20)
        async for _ in ws:
            pass
        return ws

    async with server(handler) as url:
        connection = await backend.connect(url, max_message_bytes=32)
        with pytest.raises(WebSocketClosed) as caught:
            await connection.receive(CancelToken())
        assert caught.value.close_code == code
        assert connection.closed
        for operation in (connection.send_text("x"), connection.receive(CancelToken())):
            with pytest.raises(WebSocketClosed):
                await operation


@pytest.mark.parametrize("mode", ["token", "task", "already"])
async def test_websocket_receive_cancellation(backend, mode):
    async with server(echo) as url:
        connection = await backend.connect(url)
        token = CancelToken()
        if mode == "already":
            token.cancel()
        task = asyncio.create_task(connection.receive(token))
        await backend._clock.drain()
        if mode == "token":
            token.cancel()
        elif mode == "task":
            task.cancel()
        # Python 3.10 的 Task 边界会把取消子类规范化为 CancelledError。
        with pytest.raises(asyncio.CancelledError):
            await task
        if mode == "already":
            with pytest.raises(Cancelled):
                await connection.receive(token)
        if mode != "already":
            assert connection.closed
        assert backend._clock.pending_sleep_count == 0
        await connection.close()


@pytest.mark.parametrize("mode", ["timeout", "cancel", "stop"])
async def test_websocket_connect_cleanup(backend, mode):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return web.Response()

    async with server(handler) as url:
        task = asyncio.create_task(backend.connect(url, timeout_s=1))
        await entered.wait()
        try:
            if mode == "timeout":
                backend._clock.advance(1)
            elif mode == "cancel":
                task.cancel()
            else:
                await backend.close()
            with pytest.raises(TimeoutError if mode == "timeout" else asyncio.CancelledError):
                await task
            assert backend._clock.pending_sleep_count == 0
            if backend._session.connector is not None:
                assert not backend._session.connector._acquired
        finally:
            release.set()


async def test_websocket_backend_close_all(backend):
    from clytie.sdk import WebSocketClosed

    async with server(echo) as url:
        first = await backend.connect(url)
        second = await backend.connect(url)
        pending = asyncio.create_task(first.receive(CancelToken()))
        await backend._clock.drain()
        await backend.close()
        await backend.close()
        with pytest.raises(WebSocketClosed):
            await pending
        assert first.closed and second.closed and backend._session.closed
        with pytest.raises(RuntimeError, match="closed"):
            await backend.connect(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1", "file:///fictional", "ws://"])
async def test_websocket_url_validation(backend, url):
    with pytest.raises(ValueError):
        await backend.connect(url)


@pytest.mark.parametrize("budget", [0, -1, 1.5, float("nan"), True])
async def test_websocket_rejects_invalid_message_budget(backend, budget):
    with pytest.raises(ValueError, match="budget"):
        await backend.connect("ws://127.0.0.1:1", max_message_bytes=budget)


async def test_websocket_ping_and_peer_close_code(backend):
    from clytie.sdk import WebSocketClosed

    async def handler(request):
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        await ws.ping(b"fictional-ping")
        message = await ws.receive()
        assert message.type == aiohttp.WSMsgType.PONG
        await ws.close(code=4001)
        return ws

    async with server(handler) as url:
        connection = await backend.connect(url)
        with pytest.raises(WebSocketClosed) as caught:
            await connection.receive(CancelToken())
        assert caught.value.close_code == 4001
        assert connection.closed and connection.close_code == 4001
        with pytest.raises(WebSocketClosed):
            await connection.send_text("fictional-message")


async def test_websocket_does_not_store_cookies(backend):
    async def handler(request):
        assert "Cookie" not in request.headers
        ws = web.WebSocketResponse()
        ws.set_cookie("fictional-cookie", "fictional-value")
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws

    async with server(handler) as url:
        for _ in range(2):
            connection = await backend.connect(url)
            await connection.close()
    assert not list(backend._session.cookie_jar)


async def test_websocket_tls_verification_and_no_downgrade(backend, tmp_path, monkeypatch):
    from tests.test_plugin_http import tls_contexts

    serving, trusted = tls_contexts(tmp_path)
    seen = []

    async def handler(request):
        seen.append(request.scheme)
        return web.Response(status=302, headers={"Location": "ws://127.0.0.1:1/"})

    async with server(handler, ssl=serving) as url:
        with pytest.raises(ConnectionError, match="WebSocket connection failed"):
            await backend.connect(url)
        assert seen == []
        monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", trusted)
        with pytest.raises(ValueError, match="downgrade"):
            await backend.connect(url)
        assert seen == ["https"]


async def test_websocket_handshake_error_redacts_secrets(backend):
    async def handler(request):
        return web.Response(status=401, text="fictional-secret")

    async with server(handler) as url:
        with pytest.raises(ConnectionError) as caught:
            await backend.connect(url, headers=dict(Authorization="test-token"))
        assert str(caught.value) == "WebSocket connection failed"
        assert "fictional" not in repr(backend._log.entries)
        assert "test-token" not in repr(backend._log.entries)
