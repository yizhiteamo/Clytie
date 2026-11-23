"""插件实例独享的生产 HTTP 会话；时间预算由 context.Http 管理。"""

from contextvars import ContextVar
from ipaddress import IPv6Address, IPv6Network, ip_address
from urllib.parse import urljoin, urlsplit

import aiohttp
from yarl import URL

from clytie.sdk.types import HttpResponse

media_download_limit = ContextVar("media_download_limit", default=None)


class ResponseTooLarge(ValueError):
    """The streamed response exceeded the requested byte budget."""


_NAT64 = IPv6Network("64:ff9b::/96")


def _is_public(value):
    try:
        address = ip_address(value)
    except ValueError:
        return False
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return _is_public(address.ipv4_mapped)
        if address in _NAT64:
            return _is_public(int(address) & 0xFFFFFFFF)
        if address.sixtofour is not None and not _is_public(address.sixtofour):
            return False
        if address.teredo is not None or address.scope_id is not None:
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


class PublicConnector(aiohttp.TCPConnector):
    async def _resolve_host(self, host, port, traces=None):
        # Validate the actual candidates aiohttp passes to the socket connector.
        if port not in (80, 443):
            raise ValueError("blocked_address")
        addresses = await super()._resolve_host(host, port, traces)
        if not addresses or not all(_is_public(entry["host"]) for entry in addresses):
            raise ValueError("blocked_address")
        return addresses


class HttpBackend:
    def __init__(self, clock, log, *, public_only=False):
        self._clock, self._log = clock, log
        self._public_only = public_only
        self._session = None
        self._closed = False

    @staticmethod
    def _url(url):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HTTP URL scheme must be http or https")
        return parsed

    async def request(
        self,
        method,
        url,
        *,
        headers=None,
        body=None,
        timeout_s=30,
        max_bytes=20 * 1024 * 1024,
        follow_redirects: bool = True,
    ):
        if self._closed:
            raise RuntimeError("HTTP backend is closed")
        if timeout_s <= 0 or max_bytes < 0:
            raise ValueError("invalid HTTP budget")
        self._url(url)
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=PublicConnector(use_dns_cache=False, force_close=True)
                if self._public_only
                else None,
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=None),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        method = method.upper()
        headers = dict(headers or {})
        for redirects in range(4):
            parsed = self._url(url)
            if self._public_only:
                destination = URL(url)
                if destination.user is not None or destination.port not in (80, 443):
                    raise ValueError("blocked_address")
            request_method = method
            started, status = self._clock.monotonic(), None
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=body,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    location = response.headers.get("Location")
                    if (
                        follow_redirects
                        and status in {301, 302, 303, 307, 308}
                        and location is not None
                    ):
                        if redirects == 3:
                            raise ValueError("HTTP redirect limit exceeded")
                        target_url = urljoin(url, location)
                        target = self._url(target_url)
                        if parsed.scheme == "https" and target.scheme == "http":
                            raise ValueError("HTTP redirect scheme downgrade forbidden")
                        if self._origin(parsed) != self._origin(target):
                            headers = {
                                key: value
                                for key, value in headers.items()
                                if key.lower() not in {"authorization", "cookie", "host"}
                            }
                        if (status == 303 and method != "HEAD") or (
                            status in {301, 302} and method == "POST"
                        ):
                            method, body = "GET", None
                            headers = {
                                key: value
                                for key, value in headers.items()
                                if key.lower() not in {"content-length", "content-type"}
                            }
                        url = target_url
                        continue
                    content = bytearray()
                    while True:
                        chunk = await response.content.read(
                            min(64 * 1024, max_bytes - len(content) + 1)
                        )
                        if not chunk:
                            break
                        if len(content) + len(chunk) > max_bytes:
                            raise ResponseTooLarge("HTTP response exceeds max_bytes")
                        content.extend(chunk)
                    return HttpResponse(
                        status=status,
                        headers=dict(response.headers),
                        body=bytes(content),
                    )
            finally:
                self._log.info(
                    "http.request",
                    method=request_method,
                    hostname=parsed.hostname,
                    status=status,
                    duration_s=self._clock.monotonic() - started,
                )

    @staticmethod
    def _origin(parsed):
        return (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
