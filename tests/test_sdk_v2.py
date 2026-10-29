"""v0.2 的接口覆盖、隐私失败路径及二进制往返，只用手写虚构数据。"""

import asyncio
import base64
import inspect
import json

import pytest
from pydantic import ValidationError

from clytie.sdk import context
from clytie.sdk import types as t
from clytie.sdk.cancellation import Cancelled, CancelToken
from clytie.sdk.testing import (
    FakeCapability,
    FakeContext,
    MockClock,
    assert_capability_contract,
    checkpoint,
)
from clytie.sdk.testing import context_contracts as checks
from clytie.sdk.testing.context_fakes import (
    FakeApiClient,
    FakeCandidateReader,
    FakeIdentity,
    FakeServiceProxy,
    FakeSubprocess,
)
from clytie.sdk.testing.contracts import assert_plugin_context_contract
from clytie.sdk.testing.helpers import assert_task_cancellation, assert_token_cancellation
from clytie.sdk.testing.samples import (
    sample_actor,
    sample_call,
    sample_conversation,
    sample_invocation,
    sample_memory,
    sample_request,
)


def records(clock):
    public = sample_memory(clock)
    private = public.model_copy(update={"record_id": "a" * 32, "visibility": "private"})
    retracted = public.model_copy(update={"record_id": "b" * 32, "retracted": True})
    frozen = public.model_copy(
        update={"record_id": "d" * 32, "decay": {"weight": 1, "frozen": True}}
    )
    return public, private, retracted, frozen


@pytest.fixture
def ctx(tmp_path):
    clock = MockClock()
    actor, conversation = sample_actor(), sample_conversation()
    return FakeContext(
        clock=clock,
        root=tmp_path,
        records=records(clock),
        permissions=(
            "assets.write",
            "memory.read.private",
            "memory.write",
            "peer",
            "model.invoke",
            "network",
            "message.proactive",
            "subprocess",
            "gpu",
        ),
        services={"fake.state": FakeServiceProxy(clock)},
        channel=True,
        identity=FakeIdentity(
            "fake-channel",
            actors={"1000000001": actor},
            conversations={(conversation.kind, "1000000002"): conversation},
        ),
    )


async def test_all_context_protocols_have_functional_contracts(ctx):
    assert_plugin_context_contract(
        ctx,
        granted=(
            "asset_writer",
            "memory",
            "services",
            "peer",
            "model",
            "http",
            "proactive",
            "subprocess",
            "identity",
        ),
    )
    assert not hasattr(ctx, "gpu")
    checks.assert_logger_contract(ctx.log)
    await checks.assert_storage_contract(ctx.storage)
    await checks.assert_events_contract(ctx.events, ctx.plugin_id)
    await checks.assert_state_contract(
        ctx.state, lambda: ctx.state.snapshot, lambda: ctx.events.published, ctx.plugin_id
    )
    ref = await checks.assert_asset_writer_contract(ctx.asset_writer)
    await checks.assert_assets_contract(ctx.assets, ref, b"fictional")
    await checks.assert_memory_view_contract(
        ctx.memory, record=records(ctx.clock)[0], hidden_id="a" * 32
    )
    draft = t.MemoryDraft(
        kind="fact", content="fictional", visibility="public", source_type="group"
    )
    await checks.assert_memory_writer_contract(ctx.memory, draft)
    proxy = ctx.services.get("fake.state")
    checks.assert_services_contract(ctx.services, "fake.state", proxy)
    await checks.assert_service_proxy_contract(
        proxy, unavailable=FakeServiceProxy(ctx.clock, available=False)
    )
    await checks.assert_peer_contract(ctx.peer)
    await checks.assert_model_invoker_contract(ctx.model, sample_request(ctx.clock))
    assert ctx.log.entries[-1][2]["purpose"] == "contract"
    await checks.assert_http_contract(ctx.http)
    await checks.assert_proactive_contract(ctx.proactive, [t.TextPart(text="fictional")])
    await checks.assert_subprocess_contract(ctx.subprocess)
    assert ctx.subprocess.calls[0][-1] == ctx.storage.path
    checks.assert_identity_contract(
        ctx.identity, "1000000001", sample_actor(), "1000000002", sample_conversation()
    )


async def test_candidate_reader_filters_before_get_count_and_batches(ctx):
    public, private, retracted, frozen = records(ctx.clock)
    reader = FakeCandidateReader([public, private, retracted, frozen], clock=ctx.clock)
    await checks.assert_candidate_reader_contract(
        reader, [public], hidden_ids=[r.record_id for r in (private, retracted, frozen)]
    )
    reader = FakeCandidateReader(
        [public, private, retracted, frozen],
        allowed_visibility=["public", "private"],
        clock=ctx.clock,
    )
    await checks.assert_candidate_reader_contract(
        reader, [public, private], hidden_ids=[retracted.record_id, frozen.record_id]
    )
    assert [b async for b in reader.iter(kinds=[])] == []
    assert [b async for b in reader.iter(scope="another_group")] == []
    with pytest.raises(ValueError):
        _ = [b async for b in reader.iter(batch=0)]
    await checks.assert_api_client_contract(FakeApiClient(clock=ctx.clock))


async def test_private_memory_requires_both_permission_and_private_scene(tmp_path):
    clock = MockClock()
    for permissions in ([], ["memory.read.public"], ["memory.read.private"]):
        ctx = FakeContext(
            root=tmp_path, clock=clock, records=records(clock), permissions=permissions
        )
        if not permissions:
            assert not hasattr(ctx, "memory")
            continue
        assert not hasattr(ctx.memory, "write")
        for scene in ("owner_private", "public_group", "stranger_private"):
            ctx = FakeContext(
                root=tmp_path,
                clock=clock,
                records=records(clock),
                permissions=permissions,
                scene=scene,
            )
            results = await ctx.memory.search("")
            expected = "memory.read.private" in permissions and scene == "owner_private"
            assert any(r.visibility == "private" for r in results) == expected


@pytest.mark.parametrize("payload", [b"", b"\xfb\xff\x00\xfe", bytes(range(256))])
def test_bytes_are_standard_base64_not_text_or_urlsafe(payload):
    result = t.ProcessResult(returncode=0, stdout=payload, stderr=payload, truncated=False)
    value = json.loads(result.model_dump_json())
    assert value["stdout"] == base64.b64encode(payload).decode("ascii")
    assert t.ProcessResult.model_validate_json(result.model_dump_json()) == result
    response = t.HttpResponse(status=200, headers={}, body=payload)
    assert json.loads(response.model_dump_json())["body"] == value["stdout"]
    assert t.HttpResponse.model_validate_json(response.model_dump_json()) == response


async def test_subprocess_truncates_combined_binary_output_without_spawning(ctx, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn"))
    proc = FakeSubprocess(
        ctx.storage.path,
        ctx.clock,
        t.ProcessResult(returncode=7, stdout=b"\xffab", stderr=b"c\xfe", truncated=False),
    )
    for limit in (0, 2, 4, 5, 8):
        result = await proc.run(["fictional-command"], timeout_s=1, max_output_bytes=limit)
        assert len(result.stdout) + len(result.stderr) == min(limit, 5)
        assert result.truncated == (limit < 5)
        assert result.returncode == 7


@pytest.mark.parametrize("kind", ["exception", "asyncio", "result"])
async def test_capability_contract_accepts_each_cancel_form(ctx, kind):
    class Capability(FakeCapability):
        async def execute(self, plan, inv, cancel):
            if cancel.cancelled:
                if kind == "exception":
                    raise Cancelled("fictional")
                if kind == "asyncio":
                    raise asyncio.CancelledError()
                return t.ToolResult(
                    call_id=plan.data["call_id"], status="cancelled", content="", data={}
                )
            return await super().execute(plan, inv, cancel)

    await assert_capability_contract(Capability(ctx), sample_call(), sample_invocation())


async def test_cancel_contract_rejects_ok_and_bounds_unresponsive_execution():
    async def incorrect(token):
        return t.ToolResult(call_id="fake-call", status="ok", content="", data={})

    with pytest.raises(AssertionError):
        await assert_token_cancellation(incorrect, allow_cancelled_result=True)

    async def stuck(token):
        await asyncio.Event().wait()

    with pytest.raises(AssertionError, match="grace"):
        await assert_token_cancellation(stuck, allow_cancelled_result=True)


def test_notice_discriminator_and_core_event_fields_are_validated(ctx):
    from clytie.sdk.testing.samples import sample_event

    event = sample_event(ctx.clock).model_dump()
    event.update(
        type="conversation.request",
        message=None,
        notice={"kind": "friend", "actor_id": "owner", "request_ref": "fake"},
    )
    request = t.EventEnvelope.model_validate(event)
    assert isinstance(request.notice, t.ConversationRequest)
    event["type"] = "conversation.notice"
    with pytest.raises(ValidationError):
        t.EventEnvelope.model_validate(event)
    event["notice"] = {"kind": "poke"}
    assert isinstance(t.EventEnvelope.model_validate(event).notice, t.ConversationNotice)
    for name, model in t.CORE_PAYLOAD_TYPES.items():
        from .test_sdk_types import samples

        payload = next(x for x in samples() if type(x) is model).model_dump(mode="json")
        assert t.BusEvent(name=name, payload=payload, at=ctx.clock.now(), source="core")
        payload["message_body"] = "fictional but forbidden field"
        with pytest.raises(ValidationError):
            t.BusEvent(name=name, payload=payload, at=ctx.clock.now(), source="core")


def test_iso_days_reject_non_numbers_and_out_of_range():
    for days in ([0], [8], [True], ["1"]):
        with pytest.raises(ValidationError):
            t.ScheduleDeclaration(id="daily", at="09:00", handler="tick", days=days)


async def test_cancel_grace_uses_injected_clock_and_accepts_running_cancel_result():
    clock = MockClock()

    async def delayed(token):
        await clock.sleep(10)

    task = asyncio.create_task(assert_token_cancellation(delayed, clock=clock))
    for _ in range(8):
        await checkpoint()
    clock.advance(4)
    assert not task.done()
    clock.advance(1)
    with pytest.raises(AssertionError, match="grace"):
        await task
    started = asyncio.Event()

    async def completed():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return t.ToolResult(call_id="fake-call", status="cancelled", content="", data={})

    await assert_task_cancellation(completed, started=started.wait(), allow_cancelled_result=True)


def test_context_protocol_inventory_is_covered():
    # 新增协议却没有契约入口时本测试失败。
    expected = {
        "Logger": "logger",
        "Storage": "storage",
        "Events": "events",
        "State": "state",
        "Assets": "assets",
        "AssetWriter": "asset_writer",
        "MemoryView": "memory_view",
        "MemoryWriter": "memory_insert",
        "WritableMemoryView": "memory_writer",
        "ServiceProxy": "service_proxy",
        "Services": "services",
        "Peer": "peer",
        "ModelInvoker": "model_invoker",
        "Http": "http",
        "Proactive": "proactive",
        "Subprocess": "subprocess",
        "Identity": "identity",
    }
    found = {
        name
        for name, value in vars(context).items()
        if inspect.isclass(value)
        and value.__module__ == context.__name__
        and getattr(value, "_is_protocol", False)
    }
    assert found == set(expected) | {"PluginContext"}
    assert all(callable(getattr(checks, f"assert_{name}_contract")) for name in expected.values())


@pytest.mark.parametrize(
    "operation",
    [
        "get",
        "set",
        "delete",
        "keys",
        "publish",
        "state",
        "api",
        "candidate",
        "memory",
        "peer",
        "model",
        "http",
        "proactive",
        "subprocess",
    ],
)
async def test_running_context_operations_propagate_cancellation(tmp_path, operation):
    class GateClock(MockClock):
        async def sleep(self, seconds):
            entered.set()
            await asyncio.Event().wait()

    entered = asyncio.Event()
    clock = GateClock()
    ctx = FakeContext(
        root=tmp_path,
        clock=clock,
        permissions=[
            "memory.write",
            "memory.read.public",
            "peer",
            "model.invoke",
            "network",
            "message.proactive",
            "subprocess",
        ],
    )
    operations = {
        "get": lambda: ctx.storage.get("fake"),
        "set": lambda: ctx.storage.set("fake", {}),
        "delete": lambda: ctx.storage.delete("fake"),
        "keys": ctx.storage.keys,
        "publish": lambda: ctx.events.publish("fake-plugin.changed", {}),
        "state": lambda: ctx.state.set({}),
        "api": lambda: FakeApiClient(clock=clock).request("GET", "/api/v1/status"),
        "candidate": lambda: FakeCandidateReader(clock=clock).count(),
        "memory": lambda: ctx.memory.get("fake"),
        "peer": lambda: ctx.peer.send("companion_b", "fake", {}),
        "model": lambda: ctx.model.generate(
            sample_request(clock), CancelToken(), purpose="contract"
        ),
        "http": lambda: ctx.http.request("GET", "https://example.invalid/"),
        "proactive": lambda: ctx.proactive.submit("test_group", parts=[], reason="contract"),
        "subprocess": lambda: ctx.subprocess.run(["fake"], timeout_s=1),
    }
    await assert_task_cancellation(operations[operation], started=entered.wait())
    assert ctx.storage._values == {} and ctx.events.published == []


async def test_retriever_only_receives_projected_candidates_and_private_get_is_scoped(tmp_path):
    from clytie.sdk.testing import FakeRetriever
    from clytie.sdk.testing.samples import sample_query

    clock = MockClock()
    data = records(clock)
    ctx = FakeContext(
        root=tmp_path,
        clock=clock,
        records=data,
        permissions=["memory.read.private"],
        scene="owner_private",
    )
    reader = FakeCandidateReader(data, clock=clock)
    hits = await FakeRetriever(ctx).search(sample_query(), reader, CancelToken())
    assert [hit.record_id for hit in hits] == [data[0].record_id]
    assert await ctx.memory.get(data[1].record_id) == data[1]
    public = FakeContext(
        root=tmp_path,
        clock=clock,
        records=data,
        permissions=["memory.read.public"],
        scene="owner_private",
    )
    assert await public.memory.get(data[1].record_id) is None


def test_fake_context_rejects_path_escape_before_creating_storage(tmp_path):
    with pytest.raises(ValueError, match="plugin id"):
        FakeContext(plugin_id="../outside", root=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("encoded", ["-_8=", "not base64", "123", "!!!!"])
def test_binary_json_rejects_nonstandard_or_invalid_base64(encoded):
    with pytest.raises(ValidationError):
        t.HttpResponse.model_validate_json(
            json.dumps({"status": 200, "headers": {}, "body": encoded})
        )
