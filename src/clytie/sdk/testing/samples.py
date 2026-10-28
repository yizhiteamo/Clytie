"""手写的虚构 SDK 数据；不从任何部署、平台或历史记录读取。"""

from datetime import timedelta

from clytie.sdk.clock import Clock
from clytie.sdk.types import (
    Actor,
    Budget,
    ChatMessage,
    Conversation,
    DeliveryIntent,
    EventEnvelope,
    Invocation,
    MemoryDecay,
    MemoryQuery,
    MemoryRecord,
    Message,
    MessageRef,
    ModelRequest,
    TextPart,
    ToolCall,
)

TRACE_ID = "a" * 32
INTENT_ID = "b" * 32
RECORD_ID = "c" * 32


def sample_actor() -> Actor:
    return Actor(actor_id="owner", role="owner", channel_id="fake-channel")


def sample_conversation() -> Conversation:
    return Conversation(
        conversation_id="test_group",
        kind="group",
        scene="public_group",
        channel_id="fake-channel",
    )


def sample_message(clock: Clock) -> Message:
    return Message(
        ref=MessageRef(
            channel_id="fake-channel",
            conversation_id="test_group",
            platform_message_id="fake-message",
        ),
        sender=sample_actor(),
        parts=[TextPart(text="This is a fictional contract test.")],
        sent_at=clock.now(),
    )


def sample_event(clock: Clock) -> EventEnvelope:
    return EventEnvelope(
        event_id="fake-channel:fake-message",
        type="conversation.message",
        companion_id="companion_a",
        channel_id="fake-channel",
        conversation=sample_conversation(),
        message=sample_message(clock),
        received_at=clock.now(),
    )


def sample_intent(*, react: bool = False) -> DeliveryIntent:
    return DeliveryIntent(
        intent_id=INTENT_ID,
        companion_id="companion_a",
        conversation_id="test_group",
        channel_id="fake-channel",
        kind="react" if react else "reply",
        parts=[] if react else [TextPart(text="Fictional reply.")],
        emoji="💤" if react else None,
        target=MessageRef(
            channel_id="fake-channel",
            conversation_id="test_group",
            platform_message_id="fake-message",
        ),
        trace_id=TRACE_ID,
    )


def sample_request(clock: Clock) -> ModelRequest:
    return ModelRequest(
        messages=[ChatMessage(role="user", parts=[TextPart(text="Fictional request.")])],
        budget=Budget(deadline=clock.now() + timedelta(minutes=1)),
        trace_id=TRACE_ID,
    )


def sample_invocation() -> Invocation:
    return Invocation(
        companion_id="companion_a",
        actor=sample_actor(),
        conversation=sample_conversation(),
        trace_id=TRACE_ID,
    )


def sample_call() -> ToolCall:
    return ToolCall(call_id="fake-call", tool="fake-tool", arguments={"text": "fictional"})


def sample_memory(clock: Clock, *, kind: str = "fact") -> MemoryRecord:
    return MemoryRecord(
        record_id=RECORD_ID,
        revision=1,
        kind=kind,
        content="A fictional companion enjoys imaginary puzzles.",
        visibility="public",
        write_mode="auto",
        source_type="group",
        created_at=clock.now(),
        updated_at=clock.now(),
        decay=MemoryDecay(weight=1.0, frozen=False),
        retracted=False,
    )


def sample_query() -> MemoryQuery:
    return MemoryQuery(
        text="imaginary puzzles", scene="public_group", allowed_visibility=["public"], limit=3
    )
