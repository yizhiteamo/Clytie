"""生产 HTTP 仅访问回环服务器，超时由模拟时钟驱动。"""

import asyncio
import ipaddress
import socket
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
import truststore
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


@pytest.mark.parametrize("public_only", [False, True])
async def test_http_connectors_use_system_trust(public_only):
    backend = HttpBackend(MockClock(), HostLog(), public_only=public_only)

    async def handler(request):
        return web.Response()

    try:
        async with server(handler) as local:
            if public_only:
                with pytest.raises(ValueError, match="blocked_address"):
                    await backend.request("GET", "http://127.0.0.1/")
            else:
                await backend.request("GET", str(local.make_url("/")))
        ctx = backend._session.connector._ssl
        assert isinstance(ctx, truststore.SSLContext)
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    finally:
        await backend.close()


async def test_http_dns_failure(backend, monkeypatch):
    async def missing(self, host, port, family):
        raise socket.gaierror(socket.EAI_NONAME, "fictional lookup")

    monkeypatch.setattr(aiohttp.resolver.DefaultResolver, "resolve", missing)
    with pytest.raises(OSError, match="^dns_failed$"):
        await backend.request("GET", "http://fictional.invalid/")
    assert backend._log.entries == [("info", "http.request", {"reason": "dns_failed"})]


async def test_http_connection_refused(backend):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        with pytest.raises(OSError, match="^connection_refused$"):
            await backend.request("GET", f"http://127.0.0.1:{reserved.getsockname()[1]}/")
    assert backend._log.entries == [("info", "http.request", {"reason": "connection_refused"})]


@pytest.mark.parametrize("reason", ["connection_reset", "invalid_payload"])
async def test_http_interrupted_connection_and_invalid_encoding(backend, reason):
    async def handler(request):
        if reason == "connection_reset":
            request.transport.close()
        return web.Response(body=b"fictional non-gzip bytes", headers={"Content-Encoding": "gzip"})

    async with server(handler) as local:
        with pytest.raises(OSError, match=f"^{reason}$"):
            await backend.request("GET", str(local.make_url("/")))
    assert backend._log.entries == [("info", "http.request", {"reason": reason})]


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


async def test_http_first_response_without_contacting_redirect_target(backend):
    targets = []

    async def target(request):
        targets.append(request.path)
        return web.Response(body=b"unexpected")

    async with server(target) as destination:

        async def redirect(request):
            return web.Response(status=302, headers={"Location": str(destination.make_url("/"))})

        async with server(redirect) as origin:
            result = await Http(backend, backend._clock).request(
                "GET", str(origin.make_url("/")), follow_redirects=False
            )
    assert result.status == 302
    assert targets == []


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
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
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
    return serving, ssl.create_default_context(cafile=str(cert_path))


async def test_http_tls_verification_and_downgrade(backend, tmp_path):
    serving, _ = tls_contexts(tmp_path)
    seen = []

    async def handler(request):
        seen.append(request.scheme)
        return web.Response(status=302, headers={"Location": "http://127.0.0.1:1/"})

    async with server(handler, ssl=serving) as instance:
        url = str(instance.make_url("/"))
        with pytest.raises(OSError, match="^tls_failed$"):
            await backend.request("GET", url)
        assert seen == []
        assert backend._log.entries == [("info", "http.request", {"reason": "tls_failed"})]
        backend._ssl_context.load_verify_locations(cafile=str(tmp_path / "cert.pem"))
        with pytest.raises(ValueError, match="downgrade"):
            await backend.request("GET", url)
        assert seen == ["https"]


async def test_inbound_media_uses_allowlisted_tls_without_credentials_or_redirect(
    backend, tmp_path
):
    from types import SimpleNamespace

    from clytie.plugin_host.manifests import discover
    from clytie.sdk import CancelToken
    from clytie.sdk.types import ImagePart

    from .host_helpers import ROOT, host

    ctx = SimpleNamespace(
        config={},
        identity=SimpleNamespace(self_account=lambda: None),
        clock=backend._clock,
        http=Http(backend, backend._clock),
        log=backend._log,
    )
    registry = host(tmp_path, clock=backend._clock)
    manifest = discover(ROOT / "plugins", tmp_path / "plugins", repo_root=ROOT)["qq-onebot"]
    plugin_type, _ = registry._load_class(manifest)
    channel_type = __import__(plugin_type.__module__, fromlist=["OneBotChannel"]).OneBotChannel
    serving, _ = tls_contexts(tmp_path)
    backend._ssl_context.load_verify_locations(cafile=str(tmp_path / "cert.pem"))
    hits, targets = [], []

    async def dest(req):
        targets.append(True)
        return web.Response()

    async with server(dest) as target:

        async def handler(req):
            hits.append((req.headers.get("Authorization"), req.headers.get("Cookie")))
            if req.path == "/redirect":
                return web.Response(status=302, headers={"Location": str(target.make_url("/"))})
            return web.Response(body=b"synthetic", content_type="image/png")

        async with server(handler, ssl=serving) as src:
            channel = channel_type(ctx)
            url = str(src.make_url("/"))
            for raw in (url, str(target.make_url("/")), "filename"):
                with pytest.raises(ValueError):
                    await channel.fetch_media(ImagePart(raw_ref=raw), CancelToken())
            assert not hits and not targets
            ctx.config["inbound_media"] = {"allowed_hosts": ["127.0.0.1"]}
            data = await channel.fetch_media(ImagePart(raw_ref=url), CancelToken())
            assert data.data == b"synthetic"
            with pytest.raises(ValueError):
                await channel.fetch_media(
                    ImagePart(raw_ref=str(src.make_url("/redirect"))), CancelToken()
                )
            assert hits == [(None, None)] * 2
            assert targets == []
            from clytie.agent.vision import ImageFetcher

            fetcher = ImageFetcher(ctx.clock, max_bytes=4)
            assert await fetcher.fetch(channel, ImagePart(raw_ref=url)) == ("too_large", None)
            await asyncio.gather(*fetcher.workers)
    await registry.close()
