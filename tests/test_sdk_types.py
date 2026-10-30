"""只使用手写虚构数据验证 SDK 的可序列化边界。"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from clytie.sdk import types as t

AT = datetime(2000, 1, 1, tzinfo=timezone.utc)


def samples():
    actor = t.Actor(actor_id="owner", role="owner", channel_id="fake-channel")
    conversation = t.Conversation(
        conversation_id="test_group", kind="group", scene="public_group", channel_id="fake-channel"
    )
    ref = t.MessageRef(
        channel_id="fake-channel", conversation_id="test_group", platform_message_id="fake-message"
    )
    asset = t.AssetRef(asset_id="a" * 64, media_type="image/png", size=4, origin="handwritten")
    parts = [
        t.TextPart(text="fictional text"),
        t.MentionPart(actor_id="companion_a", is_self=True),
        t.ImagePart(asset=asset),
        t.AudioPart(raw_ref="fake-audio"),
        t.VideoPart(raw_ref="fake-video"),
        t.FilePart(raw_ref="fake-file"),
        t.LinkPart(url="https://example.com", title="Example"),
        t.QuotePart(message_ref=ref),
        t.StickerPart(sticker_id="fake-sticker", asset=asset),
        t.CardPart(title="fictional", body=" text"),
        t.UnknownPart(raw_type="fake-part"),
    ]
    message = t.Message(ref=ref, sender=actor, parts=parts, sent_at=AT)
    event = t.EventEnvelope(
        event_id="fake-channel:fake-message",
        type="conversation.message",
        companion_id="companion_a",
        channel_id="fake-channel",
        conversation=conversation,
        message=message,
        received_at=AT,
    )
    split = t.DeliverySplit(group_id="fake-split", index=0, total=1)
    delivery = t.DeliveryIntent(
        intent_id="b" * 32,
        companion_id="companion_a",
        conversation_id="test_group",
        channel_id="fake-channel",
        kind="reply",
        parts=parts,
        trace_id="c" * 32,
        split=split,
        target=ref,
        task_id="abcdabcd",
        not_before=AT,
    )
    error = t.ErrorInfo(code="fake-error", message="fictional diagnostic")
    receipt = t.Receipt(intent_id=delivery.intent_id, status="unknown", at=AT, error=error)
    call = t.ToolCall(
        call_id="fake-call", tool="echo-tool", arguments={"values": [1, {"ok": True}]}
    )
    chat = t.ChatMessage(role="assistant", parts=parts, tool_calls=[call])
    example = t.ToolExample(input="fictional request", call=True)
    tool = t.ToolSpec(
        name="echo-tool",
        description="echo fictional text",
        when_not_to_use="discussion",
        examples=[example],
        parameters={"type": "object", "properties": {}},
    )
    budget = t.Budget(deadline=AT)
    request = t.ModelRequest(messages=[chat], tools=[tool], budget=budget, trace_id="c" * 32)
    usage = t.TokenUsage(input_tokens=2, output_tokens=3)
    window = t.QuotaWindow(name="fake-window", remaining_ratio=0.5, resets_at=AT)
    quota = t.QuotaSnapshot(windows=[window], observed_at=AT, source="fake-model")
    turn = t.ModelTurn(finish="tool_calls", tool_calls=[call], usage=usage, quota=quota)
    invocation = t.Invocation(
        companion_id="companion_a", actor=actor, conversation=conversation, trace_id="c" * 32
    )
    plan = t.Plan(summary="fictional plan", heavy=False, data={"nested": [{"ready": True}]})
    denial = t.Denial(reason_code="fake-denial", message_for_model="fictional denial")
    result = t.ToolResult(
        call_id=call.call_id, status="ok", content="fictional", assets=[asset], data={}
    )
    decay = t.MemoryDecay(weight=1, frozen=False)
    record = t.MemoryRecord(
        record_id="d" * 32,
        revision=1,
        kind="fact",
        content="handwritten fictional fact",
        visibility="public",
        write_mode="confirmed",
        source_type="group",
        created_at=AT,
        updated_at=AT,
        decay=decay,
        retracted=False,
        tags=["fictional"],
    )
    query = t.MemoryQuery(
        text="fictional", scene="public_group", allowed_visibility=["public"], limit=3
    )
    from clytie.sdk.testing import MockClock
    from clytie.sdk.testing.samples import sample_task_summary

    summary = sample_task_summary(MockClock())
    new_models = [
        t.ApiResponse(status=200, json={"nested": [True]}),
        t.HttpResponse(
            status=200, headers={"content-type": "application/octet-stream"}, body=b"\xfb\xff"
        ),
        t.ProcessResult(returncode=0, stdout=b"\xfb\xff", stderr=b"\xfe", truncated=False),
        t.MemoryDraft(kind="fact", content="fictional", visibility="public", source_type="group"),
        t.ConversationNotice(kind="recall", target=ref),
        t.ConversationRequest(kind="friend", actor_id="owner", request_ref="fake-request"),
        t.MessageReceivedPayload(
            event_id=event.event_id,
            trace_id="c" * 32,
            conversation_id="test_group",
            scene="public_group",
            actor_id="owner",
        ),
        t.ReplySentPayload(trace_id="c" * 32, conversation_id="test_group", intent_ids=["b" * 32]),
        t.TaskStartedPayload(
            task_id="abcdabcd",
            trace_id="c" * 32,
            tool="fake-tool",
            actor_id="owner",
            conversation_id="test_group",
        ),
        summary,
        t.CompanionStateChangedPayload(
            companion_id="companion_a", plugin_id="fake-plugin", state={"ready": True}
        ),
        t.QuotaChangedPayload(model_plugin="fake-model", protection_mode=False),
        t.PluginChangedPayload(plugin_id="fake-plugin", companion_id="companion_a"),
    ]
    return [
        t.RecollectMessage(number=1, speaker="speaker_1", role="owner", text="fictional"),
        t.RecollectRemembered(
            label="[记忆1]", kind="episode", content="fictional", visibility="private"
        ),
        t.RecollectWindow(scene="owner_private", conversation_kind="private", messages=[]),
        t.RecollectItem(kind="episode", content="fictional", evidence=[1]),
        *new_models,
        actor,
        conversation,
        ref,
        asset,
        t.MediaPart(),
        *parts,
        message,
        event,
        split,
        delivery,
        error,
        receipt,
        call,
        chat,
        example,
        tool,
        budget,
        request,
        usage,
        window,
        quota,
        turn,
        invocation,
        plan,
        denial,
        result,
        decay,
        record,
        t.ImportAccount(channel="fake-channel", account="fictional-owner"),
        t.ImportConversation(channel="fake-channel", kind="private", platform_id="fictional-chat"),
        t.ImportCandidate(
            record_id="d" * 32,
            kind="fact",
            content="fictional import",
            visibility="public",
            created_at=AT,
            updated_at=AT,
        ),
        query,
        t.MemoryHit(record_id=record.record_id, score=0.5, source="fake-retriever"),
        t.BusEvent(name="fake.changed", payload={"nested": [True, None]}, at=AT, source="fake"),
        t.Notice(level="info", title="fictional notice", body="fictional", dedupe_key="fake-key"),
        t.Tick(schedule_id="fake-tick", scheduled_at=AT, now=AT, missed=0),
        t.ChannelFeatures(
            max_text_chars=200,
            supports_react=True,
            supports_quote=True,
            supports_card=True,
            media_limits={"image": {"max_bytes": 20, "formats": ["image/png"]}},
        ),
        t.ModelFeatures(
            supports_tools=True, supports_images=False, context_chars=300, agent_type=False
        ),
        t.ReplyDraft(parts=parts, conversation=conversation, trace_id="c" * 32, form="plain"),
        t.CompanionState(companion_id="companion_a", states={"fake.state": {"ready": True}}),
        t.ContextRequest(scene="public_group", actor=actor, budget_chars=20),
        t.ContextSection(title="fictional context", text="fictional", priority=100),
        t.ApiRouteDeclaration(handler="on_status", method="GET", path="/plugins/fake-plugin/state"),
        t.WebuiPanelDeclaration(
            id="fake-panel", title="fictional", kind="kv", data_route="/plugins/fake-plugin/state"
        ),
        t.ServiceDeclaration(
            handler="on_service", name="fake.state", version="1.0", methods=["get"]
        ),
        t.ScheduleDeclaration(handler="on_tick", id="fake-tick", every="PT1M"),
    ]


@pytest.mark.parametrize("model", samples(), ids=lambda model: type(model).__name__)
def test_every_sdk_model_roundtrips(model):
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model
    assert restored.model_dump(mode="json") == model.model_dump(mode="json")


def test_roundtrip_registry_covers_all_concrete_types():
    declared = {
        cls
        for cls in vars(t).values()
        if isinstance(cls, type) and issubclass(cls, t.SDKModel) and cls is not t.SDKModel
    }
    assert {type(model) for model in samples()} == declared


def test_nested_containers_are_immutable_and_detached():
    incoming = {"items": [{"name": "fictional"}]}
    call = t.ToolCall(call_id="fake-call", tool="echo-tool", arguments=incoming)
    incoming["items"][0]["name"] = "changed"
    assert call.arguments["items"][0]["name"] == "fictional"
    with pytest.raises(TypeError):
        call.arguments["items"][0]["name"] = "changed"
    with pytest.raises(AttributeError):
        call.arguments["items"].append("changed")
    with pytest.raises(TypeError):
        list.append(call.arguments["items"], "changed")
    with pytest.raises(TypeError):
        dict.__setitem__(call.arguments, "extra", "changed")
    with pytest.raises(ValidationError):
        call.tool = "changed"


@pytest.mark.parametrize(
    "first,second",
    [
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}),
        (
            {"a": [{"x": 1, "y": {"left": True, "right": None}}], "b": 2},
            {"b": 2, "a": [{"y": {"right": None, "left": True}, "x": 1}]},
        ),
    ],
)
def test_frozen_mapping_equality_and_hash_ignore_key_order(first, second):
    left, right = t.FrozenDict(first), t.FrozenDict(second)
    assert left == right
    assert not (left != right)
    assert hash(left) == hash(right)
    assert len({left, right}) == 1
    call_left = t.ToolCall(call_id="fake-call", tool="echo", arguments=first)
    call_right = t.ToolCall(call_id="fake-call", tool="echo", arguments=second)
    assert call_left == call_right
    assert not (call_left != call_right)
    assert hash(call_left) == hash(call_right)


def test_frozen_mapping_inequality_matches_equality():
    left = t.FrozenDict({"a": 1})
    right = t.FrozenDict({"a": 2})
    assert left != right
    assert not (left == right)
    assert left == {"a": 1}
    assert not (left != {"a": 1})


def test_copy_and_construct_cannot_introduce_mutable_values():
    call = t.ToolCall(call_id="fake-call", tool="echo-tool", arguments={})
    for copied in (
        call.model_copy(update={"arguments": {"nested": []}}),
        t.ToolCall.model_construct(call_id="fake-call", tool="echo-tool", arguments={"nested": []}),
    ):
        with pytest.raises(AttributeError):
            copied.arguments["nested"].append("changed")
    with pytest.raises(ValidationError):
        call.model_copy(update={"unknown_field": "rejected"})


def test_timestamps_require_zone_and_normalize_to_utc():
    shifted = datetime(2000, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    assert t.Budget(deadline=shifted).deadline == AT
    assert t.Budget(deadline=shifted).deadline.tzinfo is timezone.utc
    with pytest.raises(ValidationError, match="timezone"):
        t.Budget(deadline=datetime(2000, 1, 1))


@pytest.mark.parametrize("ratio", [-0.1, 1.1, float("inf"), float("nan")])
def test_quota_ratio_rejects_invalid_values(ratio):
    with pytest.raises(ValidationError):
        t.QuotaWindow(name="fake-window", remaining_ratio=ratio)


def test_shape_constraints_and_unknown_fields():
    with pytest.raises(ValidationError):
        t.Actor(actor_id="owner", role="owner", channel_id="fake-channel", extra="rejected")
    with pytest.raises(ValidationError):
        t.AssetRef(asset_id="not-a-hash", media_type="text/plain", size=1, origin="fictional")
    with pytest.raises(ValidationError, match="tool_calls"):
        t.ChatMessage(
            role="user", tool_calls=[{"call_id": "fake", "tool": "echo", "arguments": {}}]
        )
    delivery = next(value for value in samples() if isinstance(value, t.DeliveryIntent))
    with pytest.raises(ValidationError, match="react"):
        delivery.model_copy(update={"kind": "react"})
    event = next(value for value in samples() if isinstance(value, t.EventEnvelope))
    with pytest.raises(ValidationError, match="requires message"):
        event.model_copy(update={"message": None})


def test_arbitrary_objects_cannot_cross_json_boundary():
    with pytest.raises(ValidationError):
        t.ToolCall(call_id="fake-call", tool="echo", arguments={"value": object()})


def test_collection_defaults_do_not_use_none():
    message = t.ChatMessage(role="user")
    assert list(message.parts) == []
    assert list(message.tool_calls) == []
    assert message.tool_call_id is None


def test_plugin_route_requires_audit_and_scoped_path():
    with pytest.raises(ValidationError, match="audit"):
        t.ApiRouteDeclaration(
            handler="on_status", method="POST", path="/plugins/fake-plugin/action"
        )
    assert t.ApiRouteDeclaration(
        handler="on_status", method="POST", path="/plugins/fake-plugin/action", audit=True
    ).audit
    with pytest.raises(ValidationError):
        t.ApiRouteDeclaration(handler="on_status", method="GET", path="/management/action")
    with pytest.raises(ValidationError):
        t.ApiRouteDeclaration(
            handler="on_status", method="GET", path="/plugins/fake-plugin/../../management"
        )


def test_declarative_panel_does_not_accept_scripts():
    with pytest.raises(ValidationError):
        t.WebuiPanelDeclaration(
            id="fake",
            title="fictional",
            kind="text",
            data_route="/plugins/fake/state",
            script="alert('fictional')",
        )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"at": "12:00", "every": "PT1M"},
        {"at": "24:00"},
        {"every": "tomorrow"},
        {"every": "P"},
        {"every": "PT"},
        {"every": "P1DT"},
        {"every": "PT1M", "days": ["fictional-day"]},
    ],
)
def test_schedule_rejects_ambiguous_or_invalid_time(fields):
    with pytest.raises(ValidationError):
        t.ScheduleDeclaration(handler="on_tick", id="fake-tick", **fields)


@pytest.mark.parametrize("duration", ["PT1M", "P1D", "P1W", "P3DT4H5M6S", "PT0.5S"])
def test_duration_forms_and_iso_days_roundtrip(duration):
    value = t.ScheduleDeclaration(handler="on_tick", id="fake-tick", every=duration)
    assert t.ScheduleDeclaration.model_validate_json(value.model_dump_json()) == value
    daily = t.ScheduleDeclaration(handler="on_tick", id="fake-daily", at="23:00", days=[1, 7])
    assert t.ScheduleDeclaration.model_validate_json(daily.model_dump_json()) == daily
