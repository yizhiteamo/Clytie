"""B3d: model-only placeholders over real ledger history and quotes."""

from uuid import uuid4

import pytest

from clytie.agent.policy import actor_label, fast_cancel, text_parts
from clytie.sdk.types import (
    AudioPart,
    CardPart,
    DeliveryIntent,
    FilePart,
    ImagePart,
    LinkPart,
    MentionPart,
    MessageRef,
    QuotePart,
    Receipt,
    StickerPart,
    TextPart,
    UnknownPart,
    VideoPart,
)

from .test_agent import event, finish, make_agent

RAW = "fictional-opaque-media-ref"
REF = MessageRef(
    channel_id="fake", conversation_id="owner_private", platform_message_id="fictional-quoted"
)
CASES = [
    (ImagePart(raw_ref=RAW), "[图片]"),
    (
        StickerPart(sticker_id="fictional-sticker", raw_ref=RAW, summary="虚构微笑"),
        "[表情：虚构微笑]",
    ),
    (StickerPart(sticker_id="fictional-sticker", raw_ref=RAW), "[表情]"),
    (MentionPart(actor_id="fictional-self", is_self=True), "[@你]"),
    (MentionPart(actor_id="fictional-owner", is_self=False), "[@主人]"),
    (MentionPart(actor_id="fictional-other", is_self=False), "[@某人]"),
    (AudioPart(raw_ref=RAW), "[语音]"),
    (VideoPart(raw_ref=RAW), "[视频]"),
    (FilePart(raw_ref=RAW), "[文件]"),
    (QuotePart(message_ref=REF), "[引用]"),
    (UnknownPart(raw_type="fictional-unknown"), "[不支持的消息]"),
]


def with_parts(incoming, parts):
    return incoming.model_copy(
        update={"message": incoming.message.model_copy(update={"parts": parts})}
    )


def save_outbound(agent, conversation, parts, status):
    record = agent.ledger.open_record(conversation=conversation)
    intent = DeliveryIntent(
        intent_id=uuid4().hex,
        companion_id=agent.cid,
        conversation_id=conversation.conversation_id,
        channel_id=conversation.channel_id,
        kind="reply",
        parts=parts,
        trace_id=record["trace_id"],
    )
    agent.ledger.seal(record["trace_id"], "ok", [intent])
    attempt = agent.ledger.begin_attempt(intent.intent_id)
    receipt = Receipt(
        intent_id=intent.intent_id,
        status=status,
        at=agent.clock.now(),
        platform_message_id="fictional-out",
    )
    agent.ledger.record_receipt(intent.intent_id, attempt, receipt)
    return MessageRef(
        channel_id=conversation.channel_id,
        conversation_id=conversation.conversation_id,
        platform_message_id=receipt.platform_message_id,
    )


@pytest.mark.parametrize("part,placeholder", CASES)
@pytest.mark.parametrize("status", ["sent", "unknown"])
async def test_placeholders_current_history_and_both_quote_directions(
    tmp_path, part, placeholder, status
):
    agent, _, _ = await make_agent(tmp_path)
    agent.instance["people"]["fictional-owner"] = {"role": "owner", "accounts": []}
    try:
        previous = with_parts(event(agent, ""), [part])
        agent.ledger.claim_inbound(previous)
        agent.ledger.ignore(previous.event_id)
        agent.clock.advance(1)
        ref = save_outbound(agent, previous.conversation, [part], status)
        agent.clock.advance(1)
        current = with_parts(event(agent, "", reply_to=previous.message.ref), [part])
        queries = []

        async def memory(trace, conversation, query):
            queries.append(query)
            return []

        agent.memory.build = memory
        context = await agent.context.build(
            current.conversation, current.message.sender, [current], ""
        )
        texts = [text_parts(m.parts) for m in context]
        label = f"owner {actor_label(previous.message.sender)}："
        assert label + placeholder in texts
        assert ("[不确定是否发出]" if status == "unknown" else "") + placeholder in texts
        assert texts[-1] == label + placeholder + "\n引用：" + placeholder
        assert await agent.context.quote(ref, current.conversation) == placeholder
        assert queries == [
            "\n".join(
                [
                    text_parts([part]),
                    label + text_parts([part]),
                    ("[不确定是否发出]" if status == "unknown" else "") + text_parts([part]),
                ]
            )
        ]
        assert RAW not in "\n".join(texts)
        assert "fictional-self" not in "\n".join(texts)
        assert "fictional-owner" not in "\n".join(texts)
        assert "fictional-other" not in "\n".join(texts)
    finally:
        await finish(agent)


async def test_mixed_parts_preserve_order_and_text_only_policy(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    try:
        parts = [
            TextPart(text="fictional start"),
            ImagePart(raw_ref=RAW),
            TextPart(text="fictional end"),
            LinkPart(url="https://example.invalid", title="link"),
            CardPart(title="card", body="body"),
        ]
        incoming = with_parts(event(agent), parts)
        await agent.receive(incoming)
        await agent.drain()
        text = text_parts(model.requests[0].messages[-1].parts)
        assert "fictional start\n[图片]\nfictional end\nlink\ncard\nbody" in text
        assert text_parts(parts) == "fictional start\n\nfictional end\nlink\ncard\nbody"
        assert (
            fast_cancel(
                with_parts(incoming, [TextPart(text="取消"), ImagePart(raw_ref=RAW)]).message,
                ["取消"],
            )
            is None
        )
        assert (
            fast_cancel(
                with_parts(
                    incoming,
                    [TextPart(text="取消"), MentionPart(actor_id="fake-self", is_self=True)],
                ).message,
                ["取消"],
            )
            == {}
        )
    finally:
        await finish(agent)


async def test_media_only_received_message_reaches_model_without_asset_access(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    try:
        incoming = with_parts(event(agent, ""), [ImagePart(raw_ref=RAW)])
        await agent.receive(incoming)
        await agent.drain()
        assert len(model.requests) == 1
        assert text_parts(model.requests[0].messages[-1].parts).endswith("：[图片]")
        assert RAW not in model.requests[0].model_dump_json()
    finally:
        await finish(agent)


async def test_outbound_check_uses_original_text_not_model_placeholders(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    original = agent.host.behavior
    parts = [
        TextPart(text="fictional reply"),
        ImagePart(raw_ref=RAW),
        MentionPart(actor_id="fictional-other", is_self=False),
        StickerPart(sticker_id="fictional-sticker", summary="虚构说明"),
    ]
    seen = []

    async def behavior(cid, phase, value, **kwargs):
        if phase == "after_generation":
            return value.model_copy(update={"parts": parts})
        return await original(cid, phase, value, **kwargs)

    async def check(text, scene):
        seen.append(text)
        return []

    agent.host.behavior, agent.outbound.check = behavior, check
    try:
        await agent.receive(event(agent))
        await agent.drain()
        assert seen and seen[0] == text_parts(parts)
        assert all("[图片]" not in s and "[@某人]" not in s and "[表情" not in s for s in seen)
    finally:
        await finish(agent)


async def test_placeholder_history_obeys_existing_character_budget(tmp_path):
    agent, _, _ = await make_agent(
        tmp_path, changes={"context": {"max_chars": 3, "max_single_message_chars": 3}}
    )
    try:
        incoming = event(agent)
        save_outbound(
            agent, incoming.conversation, [ImagePart(raw_ref=RAW), FilePart(raw_ref=RAW)], "sent"
        )
        context = await agent.context.build(
            incoming.conversation, incoming.message.sender, [incoming], ""
        )
        assert [text_parts(m.parts) for m in context if m.role == "assistant"] == ["[图片"]
    finally:
        await finish(agent)
