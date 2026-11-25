"""通过宿主 HTTP 和凭据服务调用 OpenAI 图像接口。"""

import asyncio
import base64
import binascii
import json
from urllib.parse import urlsplit

from clytie.sdk import CredentialUnavailable

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
RENDER_SIZES = {"1024x1024", "1024x1536", "1536x1024"}
TOOL_TIMEOUT_S = 120
RENDER_TIMEOUT_S = 300
ERROR_CODES = frozenset(
    {
        "content_refused",
        "rate_limited",
        "transient",
        "quota_exceeded",
        "invalid_request",
    }
)


class BackendError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "invalid_request"
        super().__init__(self.code)


async def run_cancellable(operation, cancel):
    task = asyncio.create_task(operation)
    cancelled = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait((task, cancelled), return_when=asyncio.FIRST_COMPLETED)
        cancel.raise_if_cancelled()
        return await task
    finally:
        for pending in (task, cancelled):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, cancelled, return_exceptions=True)


class OpenAIBackend:
    def __init__(self, ctx, config):
        self.ctx = ctx
        self.model = config.get("model")
        self.api_key_ref = config.get("api_key_ref")
        self.base_url = config.get("base_url", "https://api.openai.com/v1")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.model,
                self.api_key_ref,
                self.base_url,
            )
        ):
            raise self._error("invalid_request")
        try:
            parsed = urlsplit(self.base_url)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise self._error("invalid_request")
        self.base_url = self.base_url.rstrip("/")
        self.options = {key: config[key] for key in ("quality", "size") if key in config}
        if "quality" in self.options and self.options["quality"] not in (
            "low",
            "medium",
            "high",
            "auto",
        ):
            raise self._error("invalid_request")
        if "size" in self.options and (
            not isinstance(self.options["size"], str) or not 1 <= len(self.options["size"]) <= 64
        ):
            raise self._error("invalid_request")

    def _error(self, code, *, http_status=None, credential_code=None):
        error = BackendError(code)
        fields = {"error_code": error.code}
        if http_status is not None:
            fields["http_status"] = http_status
        if credential_code is not None:
            fields["credential_code"] = credential_code
        self.ctx.log.warning("image_backend_error", **fields)
        return error

    def _fields(self, prompt):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_000:
            raise self._error("invalid_request")
        return {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "output_format": "png",
            **self.options,
        }

    async def generate(self, prompt, cancel):
        cancel.raise_if_cancelled()
        data, media, _ = await self._generate(self._fields(prompt), cancel, TOOL_TIMEOUT_S)
        return data, media

    async def _generate(self, fields, cancel, timeout_s):
        body = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        return await self._request("generations", body, "application/json", cancel, timeout_s)

    async def edit(self, prompt, image_bytes, media_type, cancel):
        cancel.raise_if_cancelled()
        data, media, _ = await self._edit(
            self._fields(prompt),
            [(image_bytes, media_type)],
            MAX_IMAGE_BYTES,
            cancel,
            TOOL_TIMEOUT_S,
        )
        return data, media

    async def render(self, prompt, images, size, cancel):
        cancel.raise_if_cancelled()
        if size not in RENDER_SIZES or len(images) > 8:
            raise self._error("invalid_request")
        fields = {**self._fields(prompt), "size": size}
        if not images:
            return await self._generate(fields, cancel, RENDER_TIMEOUT_S)
        return await self._edit(fields, images, 20 * 1024 * 1024, cancel, RENDER_TIMEOUT_S)

    async def _edit(self, fields, images, max_bytes, cancel, timeout_s):
        extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
        for image_bytes, media_type in images:
            if (
                not isinstance(image_bytes, bytes)
                or not image_bytes
                or len(image_bytes) > max_bytes
                or media_type not in extensions
            ):
                raise self._error("invalid_request")
        values = [(key, str(value).encode("utf-8")) for key, value in fields.items()]
        boundary = b"clytie-image-boundary"
        while any(boundary in value for _, value in values) or any(
            boundary in data for data, _ in images
        ):
            boundary += b"_"
        parts = []
        for key, value in values:
            parts.append(
                b"--"
                + boundary
                + b'\r\nContent-Disposition: form-data; name="'
                + key.encode()
                + b'"\r\n\r\n'
                + value
                + b"\r\n"
            )
        for index, (image_bytes, media_type) in enumerate(images):
            filename = f"image-{index}.{extensions[media_type]}"
            parts.extend(
                [
                    b"--" + boundary,
                    f'\r\nContent-Disposition: form-data; name="image[]"; filename="{filename}"\r\n'
                    f"Content-Type: {media_type}\r\n\r\n".encode(),
                    image_bytes,
                    b"\r\n",
                ]
            )
        parts.append(b"--" + boundary + b"--\r\n")
        return await self._request(
            "edits",
            b"".join(parts),
            "multipart/form-data; boundary=" + boundary.decode(),
            cancel,
            timeout_s,
        )

    async def _request(self, endpoint, body, content_type, cancel, timeout_s):
        http_status = None

        async def operation():
            nonlocal http_status
            key = await self.ctx.credentials.read(self.api_key_ref)
            cancel.raise_if_cancelled()
            response = await self.ctx.http.request(
                "POST",
                self.base_url + "/images/" + endpoint,
                headers=dict([("Authorization", "Bearer " + key), ("Content-Type", content_type)]),
                body=body,
                timeout_s=timeout_s,
                max_bytes=MAX_RESPONSE_BYTES,
            )
            http_status = response.status
            cancel.raise_if_cancelled()
            return self._decode(response)

        try:
            return await run_cancellable(operation(), cancel)
        except BackendError as error:
            raise self._error(error.code, http_status=http_status) from None
        except CredentialUnavailable as error:
            raise self._error("invalid_request", credential_code=error.code) from None
        except (ValueError, TypeError, AttributeError):
            raise self._error("invalid_request", http_status=http_status) from None
        except Exception:
            # 网络异常可能包含完整 URL 或响应，不能传出原异常文本。
            raise self._error("transient", http_status=http_status) from None

    @staticmethod
    def _decode(response):
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise BackendError("invalid_request")
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError):
            data = None
        if response.status < 200 or response.status >= 300:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            identifiers = (error.get("code"), error.get("type")) if isinstance(error, dict) else ()
            if any(
                code in {"insufficient_quota", "billing_hard_limit_reached", "quota_exceeded"}
                for code in identifiers
                if isinstance(code, str)
            ):
                raise BackendError("quota_exceeded")
            if any(
                code in {"content_policy_violation", "moderation_blocked", "content_refused"}
                for code in identifiers
                if isinstance(code, str)
            ):
                raise BackendError("content_refused")
            if response.status == 429:
                raise BackendError("rate_limited")
            if response.status >= 500 or response.status in {408, 409}:
                raise BackendError("transient")
            raise BackendError("invalid_request")
        try:
            encoded = data["data"][0]["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise ValueError
            image = base64.b64decode(encoded, validate=True)
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > MAX_IMAGE_BYTES:
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
            raise BackendError("invalid_request") from None
        counters = data.get("usage")
        usage = {
            key: counters[key]
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance(counters, dict) and type(counters.get(key)) is int and counters[key] >= 0
        }
        return image, "image/png", usage
