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
from clytie.sdk.testing import FakeLog
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
    ctx = SimpleNamespace(credentials=Credentials(), http=Http(result), log=FakeLog())
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
    assert "quality" not in fields and "size" not in fields


@pytest.mark.parametrize("size", ["1024x1024", "1024x1536", "1536x1024"])
@pytest.mark.parametrize("count", [0, 1, 8])
async def test_render_uses_ordered_reference_images_size_and_configured_quality(
    module, size, count
):
    ctx, _ = setup(module)
    backend = module.OpenAIBackend(
        ctx,
        {"model": "fictional-model", "api_key_ref": "fictional", "quality": "high", "size": "auto"},
    )
    images = [(PNG + bytes([index]), "image/png") for index in range(count)]
    assert await backend.render("fictional scene", images, size, CancelToken()) == (
        PNG,
        "image/png",
        {},
    )
    ((_, url, args),) = ctx.http.calls
    if images:
        assert url.endswith("/images/edits")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + args["headers"]["Content-Type"].encode()
            + b"\r\n\r\n"
            + args["body"]
        )
        parts = list(message.iter_parts())
        assert [
            p.get_payload(decode=True)
            for p in parts
            if p.get_param("name", header="Content-Disposition") == "image[]"
        ] == [data for data, _ in images]
        fields = {
            p.get_param("name", header="Content-Disposition"): p.get_payload(decode=True).decode()
            for p in parts
            if not p.get_filename()
        }
    else:
        assert url.endswith("/images/generations")
        fields = json.loads(args["body"])
    assert fields["size"] == size and fields["quality"] == "high"


async def test_render_only_returns_reported_numeric_usage(module):
    _, backend = setup(
        module,
        response(
            {
                "data": [{"b64_json": base64.b64encode(PNG).decode()}],
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 5,
                    "total_tokens": True,
                    "extra": "ignored",
                },
            }
        ),
    )
    _, _, usage = await backend.render("fictional", [], "1024x1024", CancelToken())
    assert usage == {"input_tokens": 0, "output_tokens": 5}


@pytest.mark.parametrize("count,size", [(9, "1024x1536"), (1, "auto")])
async def test_render_rejects_unsupported_request_before_http(module, count, size):
    ctx, backend = setup(module)
    with pytest.raises(module.BackendError) as exc:
        await backend.render("fictional scene", [(PNG, "image/png")] * count, size, CancelToken())
    assert exc.value.code == "invalid_request" and not ctx.http.calls


@pytest.mark.parametrize(
    "options", [{}, {"quality": "high"}, {"size": "1234x567"}, {"quality": "auto", "size": "auto"}]
)
async def test_optional_image_fields_generate_and_edit(module, options):
    ctx, _ = setup(module)
    backend = module.OpenAIBackend(
        ctx, {"model": "test-image-model", "api_key_ref": "image_test", **options}
    )
    await backend.generate("fictional garden", CancelToken())
    generated = json.loads(ctx.http.calls[-1][2]["body"])
    await backend.edit("add clouds", PNG, "image/png", CancelToken())
    args = ctx.http.calls[-1][2]
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + args["headers"]["Content-Type"].encode() + b"\r\n\r\n" + args["body"]
    )
    edited = {
        part.get_param("name", header="Content-Disposition"): part.get_payload(decode=True).decode()
        for part in message.iter_parts()
        if part.get_param("name", header="Content-Disposition") != "image[]"
    }
    for field in ("quality", "size"):
        if field in options:
            assert generated[field] == edited[field] == options[field]
        else:
            assert field not in generated and field not in edited


@pytest.mark.parametrize(
    "field,value",
    [
        ("quality", 1),
        ("quality", None),
        ("quality", "other"),
        ("size", 1),
        ("size", None),
        ("size", ""),
        ("size", "x" * 65),
    ],
)
def test_invalid_image_options_config(field, value):
    from jsonschema import Draft202012Validator

    manifest = json.loads((Path(__file__).parents[1] / "plugin.json").read_text(encoding="utf-8"))
    config = {
        "backends": [{"model": "test-image-model", "api_key_ref": "image_test", field: value}]
    }
    assert list(Draft202012Validator(manifest["config"]).iter_errors(config))


@pytest.mark.parametrize("quality", ["low", "medium", "high", "auto"])
@pytest.mark.parametrize("size", ["x", "x" * 64, "1234x567", "auto"])
def test_valid_image_options_config(quality, size):
    from jsonschema import Draft202012Validator

    manifest = json.loads((Path(__file__).parents[1] / "plugin.json").read_text(encoding="utf-8"))
    config = {
        "backends": [
            {
                "model": "test-image-model",
                "api_key_ref": "image_test",
                "quality": quality,
                "size": size,
            }
        ]
    }
    Draft202012Validator(manifest["config"]).validate(config)


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
    assert ctx.log.entries == [
        ("warning", "image_backend_error", {"error_code": expected, "http_status": status})
    ]
    assert MARKER not in repr(ctx.log.entries)


@pytest.mark.parametrize("error", [TimeoutError(MARKER), OSError(MARKER), ValueError(MARKER)])
async def test_safe_transport_errors(module, error):
    ctx, backend = setup(module, error)
    with pytest.raises(module.BackendError) as caught:
        await backend.generate("garden", CancelToken())
    assert caught.value.code == (
        "invalid_request" if isinstance(error, ValueError) else "transient"
    )
    assert MARKER not in "".join(traceback.format_exception(caught.value))
    assert ctx.log.entries == [
        ("warning", "image_backend_error", {"error_code": caught.value.code})
    ]
    assert MARKER not in repr(ctx.log.entries)


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
    ctx = SimpleNamespace(log=FakeLog())
    with pytest.raises(module.BackendError, match="invalid_request"):
        module.OpenAIBackend(ctx, {"api_key_ref": "image_test"})
    assert ctx.log.entries == [
        ("warning", "image_backend_error", {"error_code": "invalid_request"})
    ]


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


@pytest.mark.parametrize("code", ["unknown_ref", "not_permitted", "missing", "empty", "unreadable"])
async def test_credential_failure_does_not_start_request(module, code):
    ctx, backend = setup(module)

    async def read(ref):
        raise CredentialUnavailable(code)

    ctx.credentials.read = read
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())
    assert not ctx.http.calls
    assert ctx.log.entries == [
        (
            "warning",
            "image_backend_error",
            {"error_code": "invalid_request", "credential_code": code},
        )
    ]
    assert MARKER not in repr(ctx.log.entries)


@pytest.mark.parametrize("payload", [{"error": []}, {"error": {"code": ["bad"]}}, []])
async def test_malformed_error_structure_is_safe(module, payload):
    _, backend = setup(module, response(payload, 400))
    with pytest.raises(module.BackendError, match="invalid_request"):
        await backend.generate("garden", CancelToken())
