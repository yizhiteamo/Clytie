"""插件实例独享的生产 HTTP 会话；时间预算由 context.Http 管理。"""

from contextvars import ContextVar
from urllib.parse import urljoin, urlsplit

import aiohttp

from clytie.sdk.types import HttpResponse

media_download_limit = ContextVar("media_download_limit", default=None)


class ResponseTooLarge(ValueError):
    """The streamed response exceeded the requested byte budget."""


class HttpBackend:
    def __init__(self, clock, log):
        self._clock, self._log = clock, log
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
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=None),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        method = method.upper()
        headers = dict(headers or {})
        for redirects in range(4):
            parsed = self._url(url)
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
