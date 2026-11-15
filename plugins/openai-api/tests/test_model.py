"""Fake HTTP only; no account or online API access."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeLog, MockClock, assert_model_contract
from clytie.sdk.testing.samples import sample_request
from clytie.sdk.types import HttpResponse

MARKER = "fictional-model-credential"


def response(*, status=200, data=None):
    return HttpResponse(
        status=status,
        headers={},
        body=json.dumps(
            data
            or {
                "choices": [{"message": {"content": "fictional reply"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            }
        ).encode(),
    )


class Credentials:
    async def read(self, ref):
        assert ref == "model_test"
        return MARKER


class Http:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        if callable(self.result):
            return await self.result()
        return self.result


@pytest.fixture
def setup():
    spec = importlib.util.spec_from_file_location(
        "openai_api_test", Path(__file__).parents[1] / "model.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def create(result=None):
        ctx = SimpleNamespace(
            clock=MockClock(),
            credentials=Credentials(),
            http=Http(result or response()),
            log=FakeLog(),
            config={
                "base_url": "https://example.invalid/v1",
                "model": "fictional-model",
                "api_key_ref": "model_test",
                "context_chars": 20_000,
            },
        )
        return ctx, module.OpenAIModel(ctx)

    return create


async def test_contract_usage_quota_and_explicit_model(setup):
    ctx, model = setup()
    request = sample_request(ctx.clock)
    await assert_model_contract(model, request)
    turn = await model.generate(request, CancelToken())
    assert turn.usage.input_tokens == 11 and turn.usage.output_tokens == 5
    count = len(ctx.http.calls)
    assert await model.quota() == "unknown" and len(ctx.http.calls) == count
    args, kwargs = ctx.http.calls[0]
    assert args == ("POST", "https://example.invalid/v1/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer " + MARKER
    body = json.loads(kwargs["body"])
    assert body["model"] == "fictional-model" and body["stream"] is False
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    assert model.features().supports_tools and model.features().supports_images
    assert not model.features().agent_type


@pytest.mark.parametrize(
    "status,identifier,expected",
    [
        (401, "invalid_api_key", "auth_invalid"),
        (403, "forbidden", "auth_invalid"),
        (429, "insufficient_quota", "quota_exhausted"),
        (429, "rate_limit_exceeded", "rate_limited"),
        (503, "server_error", "transient"),
        (400, "bad_request", "invalid_request"),
    ],
)
async def test_safe_errors_no_retry(setup, status, identifier, expected):
    ctx, model = setup(
        response(status=status, data={"error": {"code": identifier, "message": MARKER}})
    )
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert turn.finish == "error" and turn.error.code == expected
    assert len(ctx.http.calls) == 1
    assert MARKER not in turn.model_dump_json() + repr(ctx.log.entries)


@pytest.mark.parametrize("error", [TimeoutError(MARKER), OSError(MARKER)])
async def test_transport_errors_are_safe(setup, error):
    ctx, model = setup(error)
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert turn.error.code == "transient"
    assert MARKER not in turn.model_dump_json() + repr(ctx.log.entries)


async def test_cancel_and_deadline_release_http(setup):
    for reason in ("cancelled", "deadline"):
        started, closed = asyncio.Event(), asyncio.Event()

        async def pending():
            started.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        ctx, model = setup(pending)
        request, token = sample_request(ctx.clock), CancelToken()
        task = asyncio.create_task(model.generate(request, token))
        await started.wait()
        if reason == "cancelled":
            token.cancel("test")
        else:
            await ctx.clock.advance_and_drain(
                (request.budget.deadline - ctx.clock.now()).total_seconds()
            )
        if reason == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task).finish == reason
        assert closed.is_set()


async def test_tool_call_roundtrip_and_unknown_usage(setup):
    data = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"fictional"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    ctx, model = setup(response(data=data))
    req = sample_request(ctx.clock)
    result = await model.generate(req, CancelToken())
    assert result.finish == "tool_calls" and result.tool_calls[0].arguments == {"text": "fictional"}
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    from clytie.sdk.types import ChatMessage, TextPart

    req = req.model_copy(
        update={
            "messages": [
                *req.messages,
                ChatMessage(role="assistant", tool_calls=result.tool_calls),
                ChatMessage(
                    role="tool", tool_call_id="call_a", parts=[TextPart(text="fictional output")]
                ),
            ]
        }
    )
    await model.generate(req, CancelToken())
    messages = json.loads(ctx.http.calls[-1][1]["body"])["messages"]
    assert messages[-2]["tool_calls"][0]["function"]["arguments"] == '{"text": "fictional"}'
    assert messages[-1] == {"role": "tool", "tool_call_id": "call_a", "content": "fictional output"}


async def test_image_reads_authorized_asset_and_never_fetches_raw_ref(setup):
    import base64

    from clytie.sdk.types import AssetRef, ChatMessage, ImagePart

    ctx, model = setup()
    data = b"fictional image"
    asset = AssetRef(asset_id="a" * 64, media_type="image/png", size=len(data), origin="test")
    reads = []

    class Assets:
        async def info(self, asset_id):
            reads.append(("info", asset_id))
            return asset

        async def read(self, asset_id):
            reads.append(("read", asset_id))
            return data

    ctx.assets = Assets()
    req = sample_request(ctx.clock).model_copy(
        update={"messages": [ChatMessage(role="user", parts=[ImagePart(asset=asset)])]}
    )
    assert (await model.generate(req, CancelToken())).finish == "stop"
    body = json.loads(ctx.http.calls[0][1]["body"])
    assert (
        body["messages"][0]["content"][0]["image_url"]["url"]
        == "data:image/png;base64," + base64.b64encode(data).decode()
    )
    assert reads == [("info", asset.asset_id), ("read", asset.asset_id)]
    req = req.model_copy(
        update={
            "messages": [
                ChatMessage(
                    role="user", parts=[ImagePart(raw_ref="https://example.invalid/private")]
                )
            ]
        }
    )
    assert (await model.generate(req, CancelToken())).error.code == "invalid_request"
    assert len(ctx.http.calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "x",
                                "type": "function",
                                "function": {"name": "echo", "arguments": "not json"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ],
)
async def test_malformed_or_incomplete_result_is_not_success(setup, data):
    ctx, model = setup(response(data=data))
    result = await model.generate(sample_request(ctx.clock), CancelToken())
    assert result.finish == "error" and not result.tool_calls


async def test_unavailable_credential_does_not_send(setup):
    from clytie.sdk import CredentialUnavailable

    ctx, model = setup()

    async def missing(ref):
        raise CredentialUnavailable("unknown_ref")

    ctx.credentials.read = missing
    result = await model.generate(sample_request(ctx.clock), CancelToken())
    assert result.error.code == "auth_invalid" and not ctx.http.calls
