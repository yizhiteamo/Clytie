from datetime import timedelta
from pathlib import Path

import pytest

from clytie.sdk.behavior import valid_after_generation, valid_before_send
from clytie.sdk.testing import FakeContext, MockClock
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.testing.samples import TRACE_ID, sample_conversation, sample_event, sample_intent
from clytie.sdk.types import ContextRequest, DeliverySplit, MentionPart, ReplyDraft, TextPart

MANIFEST = Path(__file__).parents[1] / "plugin.json"


@pytest.mark.parametrize(
    ("kind", "text", "count"),
    [
        ("group", "文" * 58 + "。\n\n答", 1),
        ("group", ("段" * 22 + "\n\n") * 3 + "尾", 2),
        ("private", ("段" * 22 + "\n\n") * 3 + "尾", 3),
        ("group", ("句" * 22 + "！？") * 4, 2),
        ("private", ("句" * 22 + "。") * 4, 3),
        ("group", "长" * 80, 1),
    ],
)
async def test_split_preserves_original_text(tmp_path, kind, text, count):
    clock = MockClock()
    event = sample_event(clock)
    async with plugin_from_manifest(MANIFEST, root=tmp_path, clock=clock) as loaded:
        draft = ReplyDraft(
            trace_id=TRACE_ID,
            form="plain",
            parts=[TextPart(text=text)],
            conversation=event.conversation.model_copy(update={"kind": kind}),
        )
        result = await loaded.handler("behavior.after_generation")(draft)
        assert len(result.parts) == count
        assert "".join("".join(p.text.split()) for p in result.parts) == "".join(text.split())
        if count > 1:
            assert all(p.text == p.text.strip() for p in result.parts)
        assert result.form == ("split" if count > 1 else "plain")
        assert valid_after_generation(draft, result)


async def test_focus_task_and_chat_hint(tmp_path):
    clock = MockClock()
    event = sample_event(clock)
    async with plugin_from_manifest(MANIFEST, root=tmp_path, clock=clock) as loaded:
        shape = loaded.handler("behavior.after_generation")
        draft = ReplyDraft(
            trace_id=TRACE_ID,
            form="plain",
            task_id="aaaaaaaa",
            parts=[TextPart(text="段。\n\n" * 70)],
            conversation=event.conversation,
            focus=event.message.ref,
            interleaved=1,
        )
        result = await shape(draft)
        assert result.quote == draft.focus and result.form == "plain"
        assert result.parts == draft.parts
        assert (await shape(draft.model_copy(update={"interleaved": 0}))).quote is None
        private = event.conversation.model_copy(update={"kind": "private"})
        assert (await shape(draft.model_copy(update={"conversation": private}))).quote is None
        request = ContextRequest(
            actor=event.message.sender,
            conversation=event.conversation,
            scene=event.conversation.scene,
            budget_chars=100,
        )
        hint = await loaded.handler("context_section")(request)
        assert "空行" in hint.text and len(hint.text) <= 100


async def test_only_split_replies_are_paced(tmp_path):
    clock = MockClock()
    async with plugin_from_manifest(MANIFEST, root=tmp_path, clock=clock) as loaded:
        intents = [
            sample_intent().model_copy(
                update={"split": DeliverySplit(group_id="example", index=i, total=3)}
            )
            for i in range(3)
        ]
        asset = sample_intent()
        paced = await loaded.handler("behavior.before_send")([*intents, asset])
        times = [item.not_before or clock.now() for item in paced[:3]]
        assert all(1 <= (b - a).total_seconds() <= 2 for a, b in zip(times, times[1:]))
        assert paced[0].not_before is None and paced[-1] == asset
        assert valid_before_send([*intents, asset], paced)


@pytest.mark.parametrize("task_id", [None, "aaaaaaaa"])
async def test_split_proactive_uses_its_own_interval_even_with_a_task(tmp_path, task_id):
    clock = MockClock()
    async with plugin_from_manifest(MANIFEST, root=tmp_path, clock=clock) as loaded:
        paragraphs = ["这是虚构的第" + str(i) + "条主动消息。" for i in range(8)]
        draft = private_draft("\n\n".join(paragraphs), task_id=task_id)
        shaped = await loaded.handler("behavior.after_generation")(draft)
        assert shaped.form == "split" and shaped.task_id == task_id
        assert [part.text for part in shaped.parts] == [
            *paragraphs[:5],
            "\n\n".join(paragraphs[5:]),
        ]
        assert valid_after_generation(draft, shaped)
        first_time = clock.now() + timedelta(seconds=10)
        items = [
            sample_intent().model_copy(
                update={
                    "kind": shaped.kind,
                    "task_id": shaped.task_id,
                    "conversation_id": shaped.conversation.conversation_id,
                    "parts": [part],
                    "split": DeliverySplit(group_id="proactive", index=i, total=6),
                    "not_before": first_time if i == 0 else None,
                }
            )
            for i, part in enumerate(shaped.parts)
        ]
        send = loaded.handler("behavior.before_send")
        paced = await send(items)
        assert paced[0].not_before == first_time
        times = [intent.not_before for intent in paced]
        assert all(at is not None for at in times)
        assert all(2 <= (b - a).total_seconds() <= 4 for a, b in zip(times, times[1:]))
        assert valid_before_send(items, paced)
        regular_task = [
            item.model_copy(update={"kind": "reply", "task_id": "aaaaaaaa"}) for item in items
        ]
        assert await send(regular_task) == regular_task


def private_draft(text, *, task_id=None):
    return ReplyDraft(
        kind="proactive",
        trace_id=TRACE_ID,
        form="plain",
        task_id=task_id,
        parts=[TextPart(text=text)],
        conversation=sample_conversation().model_copy(
            update={"conversation_id": "owner_private", "kind": "private", "scene": "owner_private"}
        ),
    )


@pytest.mark.parametrize("limit", [1, 4])
async def test_proactive_respects_custom_limit_and_split_eligibility(tmp_path, limit):
    ctx = FakeContext(root=tmp_path, config={"tuning": {"max_parts_proactive": limit}})
    async with plugin_from_manifest(MANIFEST, ctx) as loaded:
        shape = loaded.handler("behavior.after_generation")
        exact = "文" * 29 + "。\n\n" + "字" * 30
        short = private_draft(exact)
        assert await shape(short) == short
        long = private_draft("这是一条虚构句子。" * 9)
        shaped = await shape(long)
        assert len(shaped.parts) == limit
        assert valid_after_generation(long, shaped)
        merged = private_draft("完整" * 40)
        assert await shape(merged) == merged
        mixed = long.model_copy(
            update={"parts": [*long.parts, MentionPart(actor_id="owner", is_self=False)]}
        )
        assert await shape(mixed) == mixed


async def test_proactive_interval_configuration_and_unsplit_delivery(tmp_path):
    tuning = {"interval_seconds": [1, 1], "interval_seconds_proactive": [3, 3]}
    ctx = FakeContext(root=tmp_path, config={"tuning": tuning})
    async with plugin_from_manifest(MANIFEST, ctx) as loaded:
        config = loaded.manifest["config"]["properties"]["tuning"]["properties"]
        assert config["max_parts_proactive"]["default"] == 6
        assert config["interval_seconds_proactive"]["default"] == [2, 4]
        items = [
            sample_intent().model_copy(
                update={"kind": "proactive", "split": DeliverySplit(group_id="p", index=i, total=2)}
            )
            for i in range(2)
        ]
        single = items[0].model_copy(update={"split": None})
        paced = await loaded.handler("behavior.before_send")([*items, single])
        assert paced[1].not_before == ctx.clock.now() + timedelta(seconds=3)
        assert paced[2] == single
    ctx = FakeContext(root=tmp_path, config={"tuning": {"interval_seconds_proactive": [4, 2]}})
    with pytest.raises(ValueError, match="interval_seconds_proactive"):
        async with plugin_from_manifest(MANIFEST, ctx):
            pass


@pytest.mark.parametrize(
    "left,right",
    [
        ('他问我"你明天来吗？"', '我想了想，说"应该会来吧。"然后…'),
        ("他问：「你来吗？」", "然后走了"),
        ("（他同意了！）", "于是大家出发"),
        ("第一句话。", "\n第二句话"),
        ("第一段", "\n\n  第二段"),
        ("看 https://example.com/?a=1。", "接着说"),
        ("看 https://example.com/one!?/two。", "接着说"),
        ("问?a答!2。", "接着说"),
        ("你来吗?", "我去"),
    ],
)
async def test_sentence_cuts_preserve_quotes_urls_and_trim(tmp_path, left, right):
    clock = MockClock()
    async with plugin_from_manifest(MANIFEST, root=tmp_path, clock=clock) as loaded:
        # 让第一个句末位于中点附近，右段自身也可含句末。
        first = "前" * 36 + left
        second = right + "后" * 36
        draft = ReplyDraft(
            trace_id=TRACE_ID,
            form="plain",
            parts=[TextPart(text=first + second)],
            conversation=sample_event(clock).conversation,
        )
        result = await loaded.handler("behavior.after_generation")(draft)
        assert [part.text for part in result.parts] == [first, second.strip()]
        assert valid_after_generation(draft, result)
