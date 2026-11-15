"""OpenAI 适配层完全离线测试。"""

import asyncio
import base64
import importlib.util
import json
import traceback
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from clytie.sdk import Cancelled, CancelToken, CredentialUnavailable
from clytie.sdk.types import HttpResponse


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location(
        "image_backend_test",
        Path(__file__).parents[1] / "backend.py",
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


MARKER = "fictional-credential-marker"
PNG = b"\x89PNG\r\n\x1a\nfake-image"


def response(data=None, status=200):
    return HttpResponse(
        status=status,
        headers={},
        body=json.dumps(
            data if data is not None else {"data": [{"b64_json": base64.b64encode(PNG).decode()}]},
        ).encode(),
    )


class Credentials:
    def __init__(self):
        self.calls = []

    async def read(self, ref):
        self.calls.append(ref)
        return MARKER


class Http:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or response()

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def setup(module, result=None):
    ctx = SimpleNamespace(credentials=Credentials(), http=Http(result))
    return ctx, module.OpenAIBackend(
        ctx, {"model": "test-image-model", "api_key_ref": "image_test"}
    )


async def test_generate_json_and_rereads_credentials(module):
    ctx, backend = setup(module)
    for _ in range(2):
        assert await backend.generate("a fictional garden", CancelToken()) == (PNG, "image/png")
    assert ctx.credentials.calls == ["image_test", "image_test"]
    method, url, args = ctx.http.calls[0]
    assert method == "POST" and url == "https://api.openai.com/v1/images/generations"
    assert args["headers"]["Authorization"] == "Bearer " + MARKER
    assert json.loads(args["body"]) == {
        "model": "test-image-model",
        "prompt": "a fictional garden",
        "n": 1,
        "output_format": "png",
    }
    assert args["max_bytes"] == module.MAX_RESPONSE_BYTES


async def test_edit_multipart(module):
    ctx, backend = setup(module)
    assert await backend.edit("add clouds", PNG, "image/png", CancelToken()) == (PNG, "image/png")
    _, url, args = ctx.http.calls[0]
    assert url.endswith("/images/edits")
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + args["headers"]["Content-Type"].encode() + b"\r\n\r\n" + args["body"],
    )
    fields = {
        part.get_param("name", header="Content-Disposition"): part for part in message.iter_parts()
    }
    assert fields["image[]"].get_payload(decode=True) == PNG
    assert fields["image[]"].get_content_type() == "image/png"
    assert fields["prompt"].get_payload(decode=True) == b"add clouds"
    assert fields["model"].get_payload(decode=True) == b"test-image-model"


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (400, "content_policy_violation", "content_refused"),
        (429, "rate_limit_exceeded", "rate_limited"),
        (429, "insufficient_quota", "quota_exceeded"),
        (500, "server_error", "transient"),
        (400, "invalid_parameter", "invalid_request"),
        (401, "invalid_api_key", "invalid_request"),
    ],
)
async def test_safe_error_mapping(module, status, code, expected):
    ctx, backend = setup(module, response({"error": {"code": code, "message": MARKER}}, status))
    with pytest.raises(module.BackendError) as caught:
        await backend.generate("garden", CancelToken())
    assert caught.value.code == expected
    assert str(caught.value) == expected
    assert MARKER not in "".join(traceback.format_exception(caught.value))
    assert len(ctx.http.calls) == 1


@pytest.mark.parametrize("error", [TimeoutError(MARKER), OSError(MARKER), ValueError(MARKER)])
async def test_safe_transport_errors(module, error):
    _, backend = setup(module, error)
    with pytest.raises(module.BackendError) as caught:
        await backend.generate("garden", CancelToken())
    assert caught.value.code == (
        "invalid_request" if isinstance(error, ValueError) else "transient"
    )
    assert MARKER not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"url": "https://example.invalid/image.png"}]},
        {"data": [{"b64_json": "%%%"}]},
        {"data": []},
        {"data": [{"b64_json": base64.b64encode(MARKER.encode()).decode()}]},
    ],
)
async def test_malformed_or_remote_response_never_downloaded(module, payload):
    ctx, backend = setup(module, response(payload))
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())
    assert len(ctx.http.calls) == 1


async def test_oversize_response_and_input(module, monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 8)
    _, backend = setup(module)
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", 8)
    ctx, backend = setup(module)
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.edit("garden", PNG, "image/png", CancelToken())
    assert not ctx.http.calls


async def test_token_cancellation_interrupts_http(module):
    entered, stopped = asyncio.Event(), asyncio.Event()
    ctx, backend = setup(module)

    async def request(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    ctx.http.request = request
    cancel = CancelToken()
    task = asyncio.create_task(backend.generate("garden", cancel))
    await entered.wait()
    cancel.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_precancel_does_not_read_credentials(module):
    ctx, backend = setup(module)
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(Cancelled):
        await backend.generate("garden", cancel)
    assert not ctx.credentials.calls


def test_model_required(module):
    with pytest.raises(module.BackendError, match="invalid_request"):
        module.OpenAIBackend(SimpleNamespace(), {"api_key_ref": "image_test"})


async def test_decoded_image_size_limit(module, monkeypatch):
    monkeypatch.setattr(module, "MAX_IMAGE_BYTES", len(PNG) - 1)
    _, backend = setup(module)
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())


async def test_task_cancellation_interrupts_credential_read(module):
    entered, stopped = asyncio.Event(), asyncio.Event()
    ctx, backend = setup(module)

    async def read(ref):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    ctx.credentials.read = read
    task = asyncio.create_task(backend.generate("garden", CancelToken()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert not ctx.http.calls


async def test_credential_failure_does_not_start_request(module):
    ctx, backend = setup(module)

    async def read(ref):
        raise CredentialUnavailable("missing")

    ctx.credentials.read = read
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())
    assert not ctx.http.calls


@pytest.mark.parametrize("payload", [{"error": []}, {"error": {"code": ["bad"]}}, []])
async def test_malformed_error_structure_is_safe(module, payload):
    _, backend = setup(module, response(payload, 400))
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())
