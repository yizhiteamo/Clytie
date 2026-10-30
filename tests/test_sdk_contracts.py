import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from clytie.sdk.cancellation import CancelToken
from clytie.sdk.testing import (
    FakeAfterGeneration,
    FakeApiClient,
    FakeAttention,
    FakeBeforeAttention,
    FakeBeforeSend,
    FakeCandidateReader,
    FakeCapability,
    FakeChannel,
    FakeCliCommand,
    FakeConsolidator,
    FakeContext,
    FakeContextContributor,
    FakeEmbedder,
    FakeEventSubscriber,
    FakeImporter,
    FakeMessageReceived,
    FakeModel,
    FakeNotifier,
    FakeRetriever,
    FakeScheduledHandler,
    FakeService,
    FakeTaskCompleted,
    MockClock,
    assert_after_generation_contract,
    assert_attention_contract,
    assert_before_attention_contract,
    assert_before_send_contract,
    assert_cancel_token_contract,
    assert_capability_contract,
    assert_channel_contract,
    assert_cli_command_contract,
    assert_consolidator_contract,
    assert_context_contributor_contract,
    assert_embedder_contract,
    assert_event_subscriber_contract,
    assert_importer_contract,
    assert_message_received_contract,
    assert_model_contract,
    assert_notifier_contract,
    assert_plugin_context_contract,
    assert_retriever_contract,
    assert_scheduled_handler_contract,
    assert_service_contract,
    assert_task_cancellation,
    assert_task_completed_contract,
    checkpoint,
)
from clytie.sdk.testing.samples import (
    TRACE_ID,
    sample_actor,
    sample_call,
    sample_conversation,
    sample_event,
    sample_import_candidate,
    sample_intent,
    sample_invocation,
    sample_memory,
    sample_query,
    sample_request,
    sample_task_summary,
)
from clytie.sdk.types import (
    BusEvent,
    CompanionState,
    ContextRequest,
    Notice,
    Plan,
    ReplyDraft,
    TextPart,
    Tick,
)


@pytest.fixture
def clock():
    return MockClock()


@pytest.fixture
def ctx(clock):
    return FakeContext(clock=clock)


async def test_channel_contract(ctx):
    event = sample_event(ctx.clock)
    channel = FakeChannel(ctx, incoming=(event,))
    await assert_channel_contract(
        channel, sample_intent(), sample_intent(react=True), expected_events=(event,)
    )
    assert len(channel.sent) == 2


async def test_model_contract_and_quota_does_not_generate(ctx):
    model = FakeModel(ctx)
    await assert_model_contract(model, sample_request(ctx.clock))
    assert len(model.requests) == 1
    await model.quota()
    assert len(model.requests) == 1


async def test_embedder_contract(ctx):
    await assert_embedder_contract(FakeEmbedder(ctx), ["fictional text", ""])


async def test_capability_contract(ctx):
    await assert_capability_contract(FakeCapability(ctx), sample_call(), sample_invocation())


async def test_behavior_observer_contracts(ctx):
    await assert_message_received_contract(FakeMessageReceived(ctx), sample_event(ctx.clock))
    await assert_task_completed_contract(FakeTaskCompleted(ctx), sample_task_summary(ctx.clock))


async def test_attention_behavior_contracts(ctx):
    event = sample_event(ctx.clock)
    state = CompanionState(companion_id="companion_a", states={})
    await assert_before_attention_contract(FakeBeforeAttention(ctx), event, state)
    await assert_attention_contract(FakeAttention(ctx), event, [event.message])


async def test_delivery_behavior_contracts(ctx):
    draft = ReplyDraft(
        parts=[TextPart(text="Fictional reply.")],
        conversation=sample_conversation(),
        trace_id=TRACE_ID,
        form="plain",
    )
    await assert_after_generation_contract(FakeAfterGeneration(ctx), draft)
    await assert_before_send_contract(FakeBeforeSend(ctx), [sample_intent()])


async def test_context_contributor_contract():
    await assert_context_contributor_contract(
        FakeContextContributor(),
        ContextRequest(scene="public_group", actor=sample_actor(), budget_chars=100),
    )


async def test_schedule_contract(clock):
    tick = Tick(schedule_id="fake-tick", scheduled_at=clock.now(), now=clock.now(), missed=0)
    handler = FakeScheduledHandler()
    await assert_scheduled_handler_contract(handler, tick)
    assert handler.ticks == [tick]


async def test_service_contract():
    await assert_service_contract(FakeService(), "get", {"nested": [1, "fictional", None]})


async def test_event_subscriber_contract(clock):
    event = BusEvent(name="fake.changed", payload={}, at=clock.now(), source="fake")
    subscriber = FakeEventSubscriber()
    await assert_event_subscriber_contract(subscriber, event)
    assert subscriber.events == [event]


async def test_retriever_contract(ctx):
    await assert_retriever_contract(FakeRetriever(ctx), sample_query(), FakeCandidateReader())


async def test_importer_contract_without_reading_source(ctx):
    importer = FakeImporter((sample_import_candidate(ctx.clock),), ctx)
    await assert_importer_contract(importer, Path("fictional-source.jsonl"))


async def test_consolidator_contract(ctx):
    consolidator = FakeConsolidator((sample_memory(ctx.clock, kind="episode"),), ctx)
    await assert_consolidator_contract(
        consolidator,
        ctx.clock.now() - timedelta(hours=1),
        ctx.clock.now(),
        FakeCandidateReader(),
    )


async def test_notifier_contract(ctx):
    notice = Notice(level="info", title="Fictional notice", body="Test only.", dedupe_key="fake")
    await assert_notifier_contract(FakeNotifier(ctx), notice)


async def test_cli_contract():
    await assert_cli_command_contract(FakeCliCommand(), {}, FakeApiClient())


async def test_cancel_token_reusable_contract():
    await assert_cancel_token_contract()


@pytest.mark.parametrize("method", ["generate", "send", "react"])
@pytest.mark.parametrize("cancel_kind", ["token", "task"])
async def test_running_fake_operations_cancel_without_side_effects(clock, method, cancel_kind):
    ctx = FakeContext(clock=clock)
    if method == "generate":
        plugin = FakeModel(ctx, delay=30)
        argument = sample_request(clock)
    else:
        plugin = FakeChannel(ctx, delay=30)
        argument = sample_intent(react=method == "react")
    cancel = CancelToken()
    task = asyncio.create_task(getattr(plugin, method)(argument, cancel))
    await checkpoint()
    await checkpoint()
    assert clock.pending_sleep_count == 1
    if cancel_kind == "token":
        cancel.cancel("contract-test")
    else:
        task.cancel()
    # Python 3.10 的 Task 边界可能将 Cancelled 子类还原为 CancelledError。
    with pytest.raises(asyncio.CancelledError):
        await task
    if cancel_kind == "token":
        assert cancel.reason == "contract-test"
    assert clock.pending_sleep_count == 0
    assert not (plugin.requests if method == "generate" else plugin.sent)


async def test_running_token_cancellation_on_remaining_fake_interfaces(ctx):
    now = ctx.clock.now()
    inv = sample_invocation()
    plan = Plan(summary="Fictional plan", heavy=False, data={"call_id": "fake-call"})
    notice = Notice(level="info", title="Fictional", body="Notice", dedupe_key="fake")

    async def import_records(cancel):
        return [
            record
            async for record in FakeImporter((sample_import_candidate(ctx.clock),), ctx).read(
                Path("fictional.jsonl"), cancel
            )
        ]

    operations = [
        lambda cancel: FakeEmbedder(ctx).embed(["fictional"], cancel),
        lambda cancel: FakeCapability(ctx).execute(plan, inv, cancel),
        lambda cancel: FakeRetriever(ctx).search(sample_query(), FakeCandidateReader(), cancel),
        import_records,
        lambda cancel: FakeConsolidator((), ctx).run(now, now, FakeCandidateReader(), cancel),
        lambda cancel: FakeNotifier(ctx).notify(notice, cancel),
    ]
    for operation in operations:
        cancel = CancelToken()
        task = asyncio.create_task(operation(cancel))
        await checkpoint()
        assert not task.done()
        cancel.cancel("contract-test")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancel.reason == "contract-test"
    assert ctx.clock.pending_sleep_count == 0


def test_fake_context_has_only_granted_fields_and_deeply_readonly_config():
    original = {"nested": {"items": ["fictional"]}}
    ctx = FakeContext(config=original)
    assert_plugin_context_contract(ctx)
    original["nested"]["items"].append("outside mutation")
    assert len(ctx.config["nested"]["items"]) == 1
    with pytest.raises((TypeError, AttributeError)):
        ctx.config["nested"]["items"].append("forbidden")
    for attribute in (
        "memory",
        "asset_writer",
        "services",
        "peer",
        "model",
        "http",
        "proactive",
        "subprocess",
    ):
        with pytest.raises(AttributeError):
            getattr(ctx, attribute)


async def test_contract_rejects_invalid_model_return_type(ctx):
    class BrokenModel(FakeModel):
        async def generate(self, request, cancel):
            return {"text": "not an SDK model"}

    with pytest.raises(AssertionError):
        await assert_model_contract(BrokenModel(ctx), sample_request(ctx.clock))


async def test_contract_rejects_embedder_dimension_mismatch(ctx):
    class BrokenEmbedder(FakeEmbedder):
        async def embed(self, texts, cancel):
            return [[1.0] for text in texts]

    with pytest.raises(AssertionError):
        await assert_embedder_contract(BrokenEmbedder(ctx), ["fictional"])


async def test_contract_rejects_changed_generated_text(ctx):
    draft = ReplyDraft(
        parts=[TextPart(text="Original fictional reply.")],
        conversation=sample_conversation(),
        trace_id=TRACE_ID,
        form="plain",
    )

    async def changed_text(input):
        return type(input).model_validate(
            {**input.model_dump(), "parts": [TextPart(text="Changed content.")]}
        )

    with pytest.raises(AssertionError):
        await assert_after_generation_contract(changed_text, draft)


@pytest.mark.parametrize(
    "change",
    [
        {"conversation_id": "different_group"},
        {"kind": "proactive"},
        {"parts": [TextPart(text="Changed content.")]},
    ],
)
async def test_contract_rejects_changed_delivery_content_or_destination(change):
    async def changed_intents(input):
        return [type(input[0]).model_validate({**input[0].model_dump(), **change})]

    with pytest.raises(AssertionError):
        await assert_before_send_contract(changed_intents, [sample_intent()])


async def test_contract_rejects_in_place_deletion_of_delivery_intents():
    async def changed_intents(input):
        input.clear()
        return input

    with pytest.raises(AssertionError):
        await assert_before_send_contract(changed_intents, [sample_intent()])


@pytest.mark.parametrize("return_copy", [False, True])
@pytest.mark.parametrize("mutation", ["clear", "destination", "parts"])
async def test_contract_rejects_input_mutation_even_with_valid_or_empty_opinion(
    return_copy, mutation
):
    async def changed_intents(input):
        original = list(input)
        if mutation == "clear":
            input.clear()
        else:
            change = (
                {"conversation_id": "different_group"}
                if mutation == "destination"
                else {"parts": [TextPart(text="Changed content.")]}
            )
            input[0] = type(input[0]).model_validate({**input[0].model_dump(), **change})
        return original if return_copy else None

    with pytest.raises(AssertionError):
        await assert_before_send_contract(changed_intents, [sample_intent()])


async def test_running_cancellation_contract_rejects_swallowed_exception():
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def swallowing_operation():
        started.set()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            return "incorrectly swallowed cancellation"

    with pytest.raises(AssertionError, match="swallowed"):
        await assert_task_cancellation(swallowing_operation, started=started.wait())


async def test_running_cancellation_contract_accepts_propagation():
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def operation():
        started.set()
        await blocker.wait()

    await assert_task_cancellation(operation, started=started.wait())
