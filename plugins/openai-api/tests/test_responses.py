"""Responses contract using fictional HTTP responses and injected time only."""

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeLog, MockClock
from clytie.sdk.testing.samples import sample_request
from clytie.sdk.types import (
    AssetRef,
    ChatMessage,
    HttpResponse,
    ImagePart,
    TextPart,
    ToolCall,
    ToolSpec,
)

ROOT = Path(__file__).parents[1]
OPAQUE = "fictional-encrypted-reasoning"


def output(*items, status="completed", **extra):
    return {"status": status, "output": list(items), **extra}


def text_item(text="fictional reply"):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def call_item(cid="call_a", arguments='{"value":1}'):
    return {"type": "function_call", "call_id": cid, "name": "fake_tool", "arguments": arguments}


def reasoning():
    return {
        "type": "reasoning",
        "id": "rs_fake",
        "encrypted_content": OPAQUE,
        "summary": [],
        "extra": "discard",
    }


@pytest.fixture
def create():
    spec = importlib.util.spec_from_file_location("responses_test_model", ROOT / "model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def factory(data=None, status=200, **config):
        class Credentials:
            async def read(self, ref):
                return "fictional-credential"

        class Http:
            def __init__(self):
                self.calls = []
                self.data = data if data is not None else output(text_item())

            async def request(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return HttpResponse(status=status, headers={}, body=json.dumps(self.data).encode())

        ctx = SimpleNamespace(
            clock=MockClock(),
            log=FakeLog(),
            credentials=Credentials(),
            http=Http(),
            config={
                "base_url": "https://example.invalid/v1",
                "model": "fictional-model",
                "api_key_ref": "fake",
                "context_chars": 20_000,
                "api": "responses",
                **config,
            },
        )
        return ctx, module.OpenAIModel(ctx)

    return factory


def followup(clock, cid="call_a"):
    req = sample_request(clock)
    messages = [
        ChatMessage(
            role="assistant",
            tool_calls=[ToolCall(call_id=cid, tool="fake_tool", arguments={"value": 1})],
        ),
        ChatMessage(role="tool", tool_call_id=cid, parts=[TextPart(text="done")]),
    ]
    return req.model_copy(update={"messages": messages})


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("effort", [None, "medium", "futurevalue"])
async def test_effort_position_and_omission(create, api, effort):
    ctx, model = create(api=api, **({"reasoning_effort": effort} if effort else {}))
    body = json.loads(await model._body(sample_request(ctx.clock), CancelToken()))
    if api == "responses":
        assert body.get("reasoning") == ({"effort": effort} if effort else None)
        assert "reasoning_effort" not in body
    else:
        assert body.get("reasoning_effort") == effort and "reasoning" not in body


@pytest.mark.parametrize(
    "field,value",
    [
        ("api", "other"),
        ("reasoning_effort", ""),
        ("reasoning_effort", "A"),
        ("reasoning_effort", "a" * 17),
        ("reasoning_effort", "a\n"),
        ("reasoning_effort", 1),
        ("reasoning_effort", None),
    ],
)
def test_config_rejects_invalid_options(field, value):
    schema = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["config"]
    config = {
        "base_url": "https://example.invalid",
        "model": "fake",
        "api_key_ref": "fake",
        "context_chars": 10,
        field: value,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(config, schema)


async def test_responses_text_usage_and_fixed_fields(create):
    ctx, model = create(
        output(text_item("a"), text_item("b"), usage={"input_tokens": 11, "output_tokens": 5})
    )
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert turn.text == "ab" and turn.finish == "stop"
    assert turn.usage.input_tokens == 11 and turn.usage.output_tokens == 5
    args, kwargs = ctx.http.calls[0]
    assert args == ("POST", "https://example.invalid/v1/responses")
    body = json.loads(kwargs["body"])
    assert body["store"] is False and body["stream"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in body and "messages" not in body
    assert body["input"][0]["content"] == [{"type": "input_text", "text": "Fictional request."}]


@pytest.mark.parametrize(
    "data,code",
    [
        (output(text_item(), status="incomplete"), "incomplete_response"),
        (
            output({"type": "message", "content": [{"type": "refusal", "refusal": OPAQUE}]}),
            "incomplete_response",
        ),
        (output(call_item(arguments="[]")), "invalid_response"),
        (output(call_item(arguments="broken")), "invalid_response"),
    ],
)
async def test_invalid_or_incomplete_never_success(create, data, code):
    ctx, model = create(data)
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert turn.finish == "error" and turn.error.code == code
    assert OPAQUE not in repr(ctx.log.entries) + turn.model_dump_json()


async def test_parallel_calls_text_and_missing_usage(create):
    ctx, model = create(output(text_item(), call_item(), call_item("call_b")))
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert turn.finish == "tool_calls" and turn.text == "fictional reply"
    assert [c.call_id for c in turn.tool_calls] == ["call_a", "call_b"]
    assert turn.usage.input_tokens is None and turn.usage.output_tokens is None


async def test_reasoning_once_order_whitelist_and_trace_isolation(create):
    ctx, model = create(output(reasoning(), call_item()))
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert OPAQUE not in turn.model_dump_json() + repr(ctx.log.entries)
    req = followup(ctx.clock)
    other = req.model_copy(update={"trace_id": "b" * 32})
    assert OPAQUE not in (await model._body(other, CancelToken())).decode()
    body = json.loads(await model._body(req, CancelToken()))
    assert [i["type"] for i in body["input"]] == [
        "reasoning",
        "function_call",
        "function_call_output",
    ]
    assert set(body["input"][0]) == {"type", "id", "encrypted_content", "summary"}
    assert body["input"][0]["encrypted_content"] == OPAQUE
    assert body["input"][-1] == {
        "type": "function_call_output",
        "call_id": "call_a",
        "output": "done",
    }
    assert OPAQUE not in (await model._body(req, CancelToken())).decode()
    _, restarted = create()
    assert OPAQUE not in (await restarted._body(req, CancelToken())).decode()


@pytest.mark.parametrize("age,present", [(599, True), (600, False), (601, False)])
async def test_reasoning_ttl(create, age, present):
    ctx, model = create(output(reasoning(), call_item()))
    await model.generate(sample_request(ctx.clock), CancelToken())
    ctx.clock.advance(age)
    assert (OPAQUE in (await model._body(followup(ctx.clock), CancelToken())).decode()) is present


async def test_reasoning_capacity(create):
    ctx, model = create()
    for index in range(65):
        ctx.http.data = output(reasoning(), call_item(f"call_{index}"))
        await model.generate(sample_request(ctx.clock), CancelToken())
    assert OPAQUE not in (await model._body(followup(ctx.clock, "call_0"), CancelToken())).decode()
    for index in (1, 64):
        assert (
            OPAQUE
            in (await model._body(followup(ctx.clock, f"call_{index}"), CancelToken())).decode()
        )


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
async def test_responses_safe_errors_without_retry(create, status, identifier, expected):
    ctx, model = create(
        {
            "error": {
                "code": identifier,
                "type": "invalid_request_error",
                "param": "tools[0].name",
                "message": OPAQUE,
            }
        },
        status=status,
    )
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert turn.error.code == expected and len(ctx.http.calls) == 1
    assert OPAQUE not in repr(ctx.log.entries) + turn.model_dump_json()
    fields = ctx.log.entries[-1]
    assert identifier in repr(fields) and "tools[0].name" in repr(fields)


async def test_all_message_roles_images_and_flat_tools(create):
    ctx, model = create()
    raw = b"fictional image"
    asset = AssetRef(asset_id="a" * 64, media_type="image/png", size=len(raw), origin="test")
    reads = []

    class Assets:
        async def info(self, asset_id):
            reads.append(("info", asset_id))
            return asset

        async def read(self, asset_id):
            reads.append(("read", asset_id))
            return raw

    ctx.assets = Assets()
    tools = [
        ToolSpec(
            name="fake_tool",
            description="fictional",
            parameters={"type": "object"},
            when_not_to_use="never real",
            examples=[{"input": "fictional", "call": True}],
        )
    ]
    req = sample_request(ctx.clock).model_copy(
        update={
            "tools": tools,
            "messages": [
                ChatMessage(role="system", parts=[TextPart(text="first")]),
                ChatMessage(role="system", parts=[TextPart(text="second")]),
                ChatMessage(role="user", parts=[TextPart(text="image"), ImagePart(asset=asset)]),
                ChatMessage(
                    role="assistant",
                    parts=[TextPart(text="prior")],
                    tool_calls=[
                        ToolCall(call_id="call_a", tool="fake_tool", arguments={"value": 1}),
                        ToolCall(call_id="call_b", tool="fake_tool", arguments={"value": 2}),
                    ],
                ),
                ChatMessage(
                    role="tool", tool_call_id="call_b", parts=[TextPart(text="second result")]
                ),
                ChatMessage(
                    role="tool", tool_call_id="call_a", parts=[TextPart(text="first result")]
                ),
            ],
        }
    )
    body = json.loads(await model._body(req, CancelToken()))
    items = body["input"]
    assert items[:2] == [
        {"role": "developer", "content": "first"},
        {"role": "developer", "content": "second"},
    ]
    assert items[2]["content"] == [
        {"type": "input_text", "text": "image"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(raw).decode(),
        },
    ]
    assert reads == [("info", asset.asset_id), ("read", asset.asset_id)]
    assert items[3] == {"role": "assistant", "content": "prior"}
    assert [i["call_id"] for i in items[4:]] == ["call_a", "call_b", "call_b", "call_a"]
    assert json.loads(items[4]["arguments"]) == {"value": 1}
    _, chat = create(api="chat_completions")
    chat.ctx.assets = ctx.assets
    chat_body = json.loads(await chat._body(req, CancelToken()))
    assert body["tools"] == [
        {"type": "function", **chat_body["tools"][0]["function"], "strict": False}
    ]


@pytest.mark.parametrize(
    "part",
    [
        ImagePart(raw_ref="https://example.invalid/image"),
        ImagePart(asset=AssetRef(asset_id="a" * 64, media_type="image/png", size=1, origin="test")),
    ],
)
async def test_responses_unauthorized_images_never_sent(create, part):
    ctx, model = create()

    class Denied:
        async def info(self, asset_id):
            raise ValueError("denied")

    ctx.assets = Denied()
    req = sample_request(ctx.clock).model_copy(
        update={"messages": [ChatMessage(role="user", parts=[part])]}
    )
    assert (await model.generate(req, CancelToken())).finish == "error"
    assert not ctx.http.calls


@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        True,
        {},
        [],
        "",
        "a" * 65,
        "a\n",
        "space value",
        "path/value",
        "é",
        "fictional:credential",
    ],
)
@pytest.mark.parametrize("field", ["code", "type", "param"])
async def test_responses_diagnostics_reject_unsafe_fields(create, value, field):
    ctx, model = create({"error": {field: value, "message": OPAQUE}}, status=400)
    turn = await model.generate(sample_request(ctx.clock), CancelToken())
    assert "openai_error_" + field not in ctx.log.entries[-1][2]
    assert OPAQUE not in turn.model_dump_json() + repr(ctx.log.entries)


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
async def test_responses_cancel_deadline_release_http(create, ending):
    ctx, model = create()
    started, closed = asyncio.Event(), asyncio.Event()

    async def request(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()

    ctx.http.request = request
    req, cancel = sample_request(ctx.clock), CancelToken()
    task = asyncio.create_task(model.generate(req, cancel))
    await started.wait()
    if ending == "cancel":
        cancel.cancel("fictional")
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await ctx.clock.advance_and_drain(60)
        assert (await task).finish == "deadline"
    assert closed.is_set()


async def test_invalid_response_does_not_cache_partial_reasoning(create):
    ctx, model = create(output(reasoning(), call_item(), call_item("bad", "[]")))
    assert (await model.generate(sample_request(ctx.clock), CancelToken())).finish == "error"
    assert OPAQUE not in (await model._body(followup(ctx.clock), CancelToken())).decode()
