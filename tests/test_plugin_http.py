"""生产 HTTP 仅访问回环服务器，超时由模拟时钟驱动。"""

import asyncio
import ipaddress
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from clytie.plugin_host.context import Http
from clytie.plugin_host.http import HttpBackend
from clytie.plugin_host.support import HostLog
from clytie.sdk.testing.clock import MockClock


@asynccontextmanager
async def server(handler, **kwargs):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    instance = TestServer(app, host="127.0.0.1")
    await instance.start_server(**kwargs)
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def backend():
    instance = HttpBackend(MockClock(), HostLog())
    try:
        yield instance
    finally:
        await instance.close()


async def test_http_normal_logs_only_safe_fields(backend, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer fictional-marker"
        assert await request.read() == b"fictional-body"
        backend._clock.advance(2)
        return web.Response(body=b"ok", status=201)

    async with server(handler) as instance:
        result = await backend.request(
            "POST",
            str(instance.make_url("/fictional-path?key=fictional-query")),
            headers=dict([("Authorization", "Bearer fictional-marker")]),
            body=b"fictional-body",
        )
    assert result.status == 201
    assert result.body == b"ok"
    assert backend._log.entries == [
        (
            "info",
            "http.request",
            {
                "method": "POST",
                "hostname": "127.0.0.1",
                "status": 201,
                "duration_s": 2,
            },
        )
    ]


async def test_http_stream_aborts_before_complete_body(backend):
    release = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"x" * 65)
        await release.wait()
        return response

    async with server(handler) as instance:
        try:
            with pytest.raises(ValueError, match="max_bytes"):
                await backend.request("GET", str(instance.make_url("/")), max_bytes=64)
        finally:
            release.set()


@pytest.mark.parametrize("cancel", [False, True])
async def test_http_timeout_and_cancel_release_connection(backend, cancel):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return web.Response(body=b"ok")

    async with server(handler) as instance:
        task = asyncio.create_task(
            Http(backend, backend._clock).request(
                "GET",
                str(instance.make_url("/")),
                timeout_s=1,
            )
        )
        await entered.wait()
        if cancel:
            task.cancel()
        else:
            backend._clock.advance(1)
        try:
            with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
                await task
            await backend._clock.drain()
            assert not backend._session.connector._acquired
        finally:
            release.set()


@pytest.mark.parametrize("count", [3, 4])
async def test_http_redirect_limit(backend, count):
    seen = []

    async def handler(request):
        step = int(request.match_info["tail"])
        seen.append(step)
        if step < count:
            return web.Response(status=302, headers={"Location": f"/{step + 1}"})
        return web.Response(body=b"done")

    async with server(handler) as instance:
        if count == 3:
            result = await backend.request("GET", str(instance.make_url("/0")))
            assert result.body == b"done"
        else:
            with pytest.raises(ValueError, match="redirect"):
                await backend.request("GET", str(instance.make_url("/0")))
    assert seen == [0, 1, 2, 3]


@pytest.mark.parametrize("url", ["file:///example", "ftp://127.0.0.1/example"])
async def test_http_rejects_other_protocols(backend, url):
    with pytest.raises(ValueError, match="scheme"):
        await backend.request("GET", url)


async def test_http_close_is_idempotent_and_prevents_reuse(backend):
    async def handler(request):
        return web.Response(body=b"ok")

    async with server(handler) as instance:
        url = str(instance.make_url("/"))
        await backend.request("GET", url)
        session = backend._session
        await backend.close()
        await backend.close()
        assert session.closed
        with pytest.raises(RuntimeError, match="closed"):
            await backend.request("GET", url)


async def test_http_cross_origin_redirect_removes_credentials_and_logs_actual_method(backend):
    async def destination(request):
        assert request.method == "GET"
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert await request.read() == b""
        return web.Response(body=b"ok")

    async with server(destination) as target:

        async def redirect(request):
            return web.Response(status=302, headers={"Location": str(target.make_url("/"))})

        async with server(redirect) as source:
            result = await backend.request(
                "POST",
                str(source.make_url("/")),
                headers=dict(
                    [("Authorization", "Bearer fictional-marker"), ("Cookie", "key=fake")]
                ),
                body=b"data",
            )
    assert result.body == b"ok"
    assert [entry[2]["method"] for entry in backend._log.entries] == ["POST", "GET"]


def tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=2**16 + 1, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-server")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=365 * 100))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    serving = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    serving.load_cert_chain(cert_path, key_path)
    trusted = ssl.create_default_context(cafile=str(cert_path))
    return serving, trusted


async def test_http_tls_verification_and_downgrade(backend, tmp_path, monkeypatch):
    serving, trusted = tls_contexts(tmp_path)
    seen = []

    async def handler(request):
        seen.append(request.scheme)
        return web.Response(status=302, headers={"Location": "http://127.0.0.1:1/"})

    async with server(handler, ssl=serving) as instance:
        url = str(instance.make_url("/"))
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await backend.request("GET", url)
        assert seen == []
        monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", trusted)
        with pytest.raises(ValueError, match="downgrade"):
            await backend.request("GET", url)
        assert seen == ["https"]
