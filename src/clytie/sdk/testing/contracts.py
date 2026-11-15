"""可复用的扩展点契约检查函数。

插件作者提供自己的实现与虚构输入，再从 pytest 等测试运行器 await 这些
函数。检查不会联网、不会加载插件，也不规定实现必须返回的业务文字。
需要耗时步骤的实现应另用 assert_task_cancellation 的 started 参数验证中途取消。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import (
    AsyncIterator,
    Callable,
    Mapping,
    MutableMapping,
    MutableSequence,
    Sequence,
)
from datetime import timedelta
from pathlib import Path
from typing import Any

from clytie.sdk.behavior import (
    Defer,
    Ignore,
    Mode,
    Normal,
    ReactOnly,
    Respond,
    Skip,
    valid_after_generation,
    valid_before_send,
)
from clytie.sdk.cancellation import Cancelled, CancelToken
from clytie.sdk.clock import Clock
from clytie.sdk.types import (
    QUOTA_UNKNOWN,
    BusEvent,
    ChannelFeatures,
    CompanionState,
    ContextRequest,
    ContextSection,
    DeliveryIntent,
    Denial,
    EventEnvelope,
    ImportCandidate,
    Invocation,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    Message,
    ModelFeatures,
    ModelRequest,
    ModelTurn,
    Notice,
    Plan,
    QuotaSnapshot,
    Receipt,
    ReplyDraft,
    SDKModel,
    TaskSummary,
    Tick,
    ToolCall,
    ToolResult,
)

from .helpers import assert_task_cancellation, assert_token_cancellation, checkpoint


def _assert_readonly(value: object) -> None:
    assert not isinstance(value, (MutableMapping, MutableSequence)), "mutable SDK container"
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_readonly(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_readonly(item)


def assert_roundtrip(value: SDKModel) -> None:
    """SDK 数据必须无损 JSON 往返且字段不可重新赋值。"""
    assert isinstance(value, SDKModel)
    encoded = value.model_dump_json()
    assert type(value).model_validate_json(encoded) == value
    assert json.loads(encoded) == value.model_dump(mode="json")
    for field in type(value).model_fields:
        _assert_readonly(getattr(value, field))
        try:
            setattr(value, field, getattr(value, field))
        except (TypeError, ValueError, AttributeError):
            pass
        else:
            raise AssertionError(f"SDK field is mutable: {type(value).__name__}.{field}")


async def assert_cancel_token_contract(factory: Callable[[], CancelToken] = CancelToken) -> None:
    token = factory()
    assert token.cancelled is False and token.reason is None
    token.raise_if_cancelled()
    child = token.child()
    waiter = asyncio.create_task(child.wait())
    await checkpoint()
    assert not waiter.done()
    token.cancel("contract-test")
    await waiter
    assert child.cancelled is True and child.reason == token.reason == "contract-test"
    try:
        child.raise_if_cancelled()
    except Cancelled:
        pass
    else:
        raise AssertionError("cancelled token did not raise Cancelled")
    assert token.child().cancelled is True
    independent = factory()
    independent.child().cancel("child-only")
    assert independent.cancelled is False
    await assert_task_cancellation(independent.wait)


def assert_plugin_context_contract(context: Any, *, granted: Sequence[str] = ()) -> None:
    assert isinstance(context.plugin_id, str)
    assert isinstance(context.companion_id, str)
    assert isinstance(context.config, Mapping)
    _assert_readonly(context.config)
    for name in ("log", "clock", "random", "storage", "events", "state", "assets"):
        assert getattr(context, name) is not None
    optional = (
        "credentials",
        "memory",
        "asset_writer",
        "services",
        "peer",
        "model",
        "http",
        "proactive",
        "subprocess",
        "identity",
    )
    for name in optional:
        assert hasattr(context, name) == (name in granted), (
            f"incorrect permission attribute: {name}"
        )


async def assert_clock_contract(clock: Clock) -> None:
    now = clock.now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    assert math.isfinite(clock.monotonic())
    assert clock.local_tz is not None
    before = clock.monotonic()
    await clock.sleep(0)
    await clock.sleep_until(now)
    assert clock.monotonic() >= before
    await assert_task_cancellation(lambda: clock.sleep(1))
    await assert_task_cancellation(lambda: clock.sleep_until(now + timedelta(seconds=1)))


async def assert_channel_contract(
    channel: Any,
    intent: DeliveryIntent,
    reaction: DeliveryIntent,
    *,
    expected_events: Sequence[EventEnvelope] = (),
) -> None:
    from clytie.sdk.channel import channel_state
    from clytie.sdk.types import ChannelState

    state = channel_state(channel)
    assert isinstance(state, ChannelState)
    assert_roundtrip(state)
    assert await channel.connect() is None
    try:
        features = channel.features()
        assert isinstance(features, ChannelFeatures)
        assert_roundtrip(features)
        stream = channel.events()
        assert isinstance(stream, AsyncIterator)
        try:
            for expected in expected_events:
                event = await anext(stream)
                assert isinstance(event, EventEnvelope)
                assert event == expected
                assert_roundtrip(event)
        finally:
            if hasattr(stream, "aclose"):
                await stream.aclose()
        for method, sample in ((channel.send, intent), (channel.react, reaction)):
            result = await method(sample, CancelToken())
            assert isinstance(result, Receipt)
            assert result.intent_id == sample.intent_id
            assert_roundtrip(result)
            await assert_token_cancellation(lambda cancel: method(sample, cancel))
            await assert_task_cancellation(lambda: method(sample, CancelToken()))
    finally:
        assert await channel.close() is None
    await assert_task_cancellation(channel.connect)
    await assert_task_cancellation(channel.close)


async def assert_model_contract(model: Any, request: ModelRequest) -> None:
    features = model.features()
    assert isinstance(features, ModelFeatures)
    assert_roundtrip(features)
    result = await model.generate(request, CancelToken())
    assert isinstance(result, ModelTurn)
    assert_roundtrip(result)
    quota = await model.quota()
    assert quota == QUOTA_UNKNOWN or isinstance(quota, QuotaSnapshot)
    if isinstance(quota, QuotaSnapshot):
        assert_roundtrip(quota)
    await assert_token_cancellation(lambda cancel: model.generate(request, cancel))
    await assert_task_cancellation(lambda: model.generate(request, CancelToken()))
    await assert_task_cancellation(model.quota)


async def assert_embedder_contract(embedder: Any, texts: list[str]) -> None:
    assert isinstance(embedder.dimension, int) and embedder.dimension > 0
    vectors = await embedder.embed(texts, CancelToken())
    assert isinstance(vectors, list) and len(vectors) == len(texts)
    for vector in vectors:
        assert len(vector) == embedder.dimension
        assert all(isinstance(number, float) and math.isfinite(number) for number in vector)
    assert await embedder.embed([], CancelToken()) == []
    await assert_token_cancellation(lambda cancel: embedder.embed(texts, cancel))
    await assert_task_cancellation(lambda: embedder.embed(texts, CancelToken()))


async def assert_capability_contract(capability: Any, call: ToolCall, inv: Invocation) -> None:
    key = capability.idempotency_key(call, inv)
    assert isinstance(key, str)
    assert key == capability.idempotency_key(call, inv)
    prepared = await capability.prepare(call, inv)
    assert isinstance(prepared, (Plan, Denial))
    assert_roundtrip(prepared)
    if isinstance(prepared, Plan):
        result = await capability.execute(prepared, inv, CancelToken())
        assert isinstance(result, ToolResult)
        assert result.call_id == call.call_id
        assert_roundtrip(result)
        await assert_token_cancellation(
            lambda cancel: capability.execute(prepared, inv, cancel), allow_cancelled_result=True
        )
        await assert_task_cancellation(
            lambda: capability.execute(prepared, inv, CancelToken()), allow_cancelled_result=True
        )
    await assert_task_cancellation(lambda: capability.prepare(call, inv))


async def _assert_handler(handler: Callable, input: object, allowed: tuple[type, ...]) -> object:
    result = await handler(input)
    assert result is None or isinstance(result, allowed)
    if isinstance(result, SDKModel):
        assert_roundtrip(result)
    await assert_task_cancellation(lambda: handler(input))
    return result


async def assert_message_received_contract(handler: Callable, event: EventEnvelope) -> None:
    assert await _assert_handler(handler, event, ()) is None


async def assert_before_attention_contract(
    handler: Callable, event: EventEnvelope, state: CompanionState
) -> None:
    await _assert_handler(handler, (event, state), (Ignore, ReactOnly, Mode, Normal))


async def assert_attention_contract(
    handler: Callable, event: EventEnvelope, batch: list[Message]
) -> None:
    await _assert_handler(handler, (event, batch), (Skip, Defer, Respond))


async def assert_after_generation_contract(handler: Callable, draft: ReplyDraft) -> None:
    result = await _assert_handler(handler, draft, (ReplyDraft,))
    if result is not None:
        assert valid_after_generation(draft, result)


async def assert_before_send_contract(handler: Callable, intents: list[DeliveryIntent]) -> None:
    original = tuple(intents)
    result = await _assert_handler(handler, intents, (list,))
    # 输入列表由调用者持有；返回 None 或另一列表不能掩盖原地篡改。
    assert all(isinstance(intent, DeliveryIntent) for intent in intents)
    assert valid_before_send(original, intents)
    if result is not None:
        assert len(result) == len(original)
        for after in result:
            assert isinstance(after, DeliveryIntent)
            assert_roundtrip(after)
        assert valid_before_send(original, result)


async def assert_task_completed_contract(handler: Callable, summary: TaskSummary) -> None:
    assert isinstance(summary, TaskSummary)
    assert_roundtrip(summary)
    assert await _assert_handler(handler, summary, ()) is None


async def assert_context_contributor_contract(contributor: Any, request: ContextRequest) -> None:
    result = await contributor.contribute(request)
    assert result is None or isinstance(result, ContextSection)
    if result is not None:
        assert_roundtrip(result)
    await assert_task_cancellation(lambda: contributor.contribute(request))
    proactive = request.model_copy(update={"actor": None})
    result = await contributor.contribute(proactive)
    assert result is None or isinstance(result, ContextSection)


async def assert_scheduled_handler_contract(handler: Any, tick: Tick) -> None:
    assert await handler.on_tick(tick) is None
    await assert_task_cancellation(lambda: handler.on_tick(tick))


async def assert_service_contract(service: Any, method: str, args: dict) -> None:
    json.dumps(args, allow_nan=False)
    result = await service.call(method, args)
    assert isinstance(result, dict)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    await assert_task_cancellation(lambda: service.call(method, args))


async def assert_event_subscriber_contract(subscriber: Any, event: BusEvent) -> None:
    assert await subscriber.on_event(event) is None
    await assert_task_cancellation(lambda: subscriber.on_event(event))


async def assert_retriever_contract(retriever: Any, query: MemoryQuery, candidates: object) -> None:
    hits = await retriever.search(query, candidates, CancelToken())
    assert isinstance(hits, list)
    for hit in hits:
        assert isinstance(hit, MemoryHit)
        assert_roundtrip(hit)
    await assert_token_cancellation(lambda cancel: retriever.search(query, candidates, cancel))
    await assert_task_cancellation(lambda: retriever.search(query, candidates, CancelToken()))


async def assert_importer_contract(importer: Any, source: Path) -> None:
    assert isinstance(importer.detect(source), bool)

    async def collect(cancel: CancelToken) -> list[ImportCandidate]:
        stream = importer.read(source, cancel)
        assert isinstance(stream, AsyncIterator)
        return [record async for record in stream]

    for record in await collect(CancelToken()):
        assert isinstance(record, ImportCandidate)
        assert_roundtrip(record)
    await assert_token_cancellation(collect)
    await assert_task_cancellation(lambda: collect(CancelToken()))


async def assert_reranker_contract(reranker: Any, query: str, records: list[MemoryRecord]) -> None:
    scores = await reranker.rerank(query, records, CancelToken())
    assert isinstance(scores, list) and len(scores) == len(records)
    assert all(
        isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
        for score in scores
    )
    await assert_token_cancellation(lambda cancel: reranker.rerank(query, records, cancel))
    await assert_task_cancellation(lambda: reranker.rerank(query, records, CancelToken()))


async def assert_consolidator_contract(consolidator: Any, window: object, reader: object) -> None:
    from clytie.sdk.types import RecollectItem

    result = await consolidator.run(window, reader, CancelToken())
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, RecollectItem)
        assert_roundtrip(item)
    await assert_token_cancellation(lambda cancel: consolidator.run(window, reader, cancel))
    await assert_task_cancellation(lambda: consolidator.run(window, reader, CancelToken()))


async def assert_notifier_contract(notifier: Any, notice: Notice) -> None:
    result = await notifier.notify(notice, CancelToken())
    assert isinstance(result, Receipt)
    assert_roundtrip(result)
    await assert_token_cancellation(lambda cancel: notifier.notify(notice, cancel))
    await assert_task_cancellation(lambda: notifier.notify(notice, CancelToken()))


async def assert_cli_command_contract(command: Any, args: dict, api: object) -> None:
    json.dumps(args, allow_nan=False)
    result = await command.run(args, api)
    assert type(result) is int
    await assert_task_cancellation(lambda: command.run(args, api))
