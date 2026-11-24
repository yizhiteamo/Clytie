"""Host policy uses the address list supplied to the transport."""

import socket
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from clytie.plugin_host.http import HttpBackend
from clytie.plugin_host.support import HostLog
from clytie.sdk.testing import MockClock

from .test_plugin_http import server


@pytest.fixture
async def public_backend():
    backend = HttpBackend(MockClock(), HostLog(), public_only=True)
    try:
        yield backend
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.0.1",
        "100.64.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "[::]",
        "[::1]",
        "[fe80::1]",
        "[fc00::1]",
        "[ff02::1]",
        "[::ffff:127.0.0.1]",
        "[2002:0a00:0001::]",
        "[64:ff9b::a00:1]",
    ],
)
async def test_public_http_blocks_nonpublic_literal_before_connect(
    public_backend, host, monkeypatch
):
    dial = AsyncMock(side_effect=AssertionError("rejected address reached transport"))
    monkeypatch.setattr("aiohappyeyeballs.start_connection", dial)
    with pytest.raises(ValueError, match="blocked_address"):
        await public_backend.request("GET", f"http://{host}/")
    dial.assert_not_awaited()


async def test_public_http_rejects_port_and_mixed_dns_before_connect(public_backend, monkeypatch):
    resolutions = []
    dial = AsyncMock(side_effect=AssertionError("rejected address reached transport"))
    monkeypatch.setattr("aiohappyeyeballs.start_connection", dial)

    async def resolve(self, host, port, family):
        resolutions.append(host)
        return [
            dict(hostname=host, host=ip, port=port, proto=0, family=socket.AF_INET, flags=0)
            for ip in ("8.8.8.8", "127.0.0.1")
        ]

    monkeypatch.setattr(aiohttp.resolver.DefaultResolver, "resolve", resolve)
    for port in (8080, 80):
        with pytest.raises(ValueError, match="blocked_address"):
            await public_backend.request("GET", f"http://fictional.example:{port}/")
    assert resolutions == ["fictional.example"]
    dial.assert_not_awaited()


async def test_public_http_uses_checked_ip_and_checks_redirect(public_backend, monkeypatch):
    resolutions, addresses, requests = [], [], []

    async def resolve(self, host, port, family):
        resolutions.append(host)
        ip = "8.8.8.8" if len(resolutions) == 1 else "127.0.0.1"
        return [dict(hostname=host, host=ip, port=port, proto=0, family=socket.AF_INET, flags=0)]

    async def handler(request):
        requests.append(request.host)
        return web.Response(status=302, headers={"Location": "http://other.example/next"})

    monkeypatch.setattr(aiohttp.resolver.DefaultResolver, "resolve", resolve)
    import aiohappyeyeballs

    connect = aiohappyeyeballs.start_connection
    async with server(handler) as local:

        async def local_transport(*, addr_infos, **options):
            for family, kind, proto, name, address in addr_infos:
                addresses.append(address)
            assert addresses == [("8.8.8.8", 80)]
            return await connect(
                addr_infos=[(family, kind, proto, name, ("127.0.0.1", local.port))],
                **options,
            )

        monkeypatch.setattr(aiohappyeyeballs, "start_connection", local_transport)
        with pytest.raises(ValueError, match="blocked_address"):
            await public_backend.request("GET", "http://fictional.example/")
    assert resolutions == ["fictional.example", "other.example"]
    assert requests == ["fictional.example"]
