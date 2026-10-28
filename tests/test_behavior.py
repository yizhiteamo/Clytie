from datetime import datetime, timezone

import pytest

from clytie.sdk.behavior import (
    Defer,
    Ignore,
    Mode,
    Normal,
    ReactOnly,
    Respond,
    Skip,
    collect_context_sections,
    merge_attention,
    merge_before_attention,
    valid_after_generation,
    valid_before_send,
)
from clytie.sdk.types import (
    CardPart,
    ContextSection,
    Conversation,
    DeliveryIntent,
    ReplyDraft,
    TextPart,
)


@pytest.mark.parametrize(
    "directive",
    [
        Ignore(),
        ReactOnly(emoji="💤"),
        Mode(name="fictional", params={"nested": [True]}),
        Normal(),
        Skip(),
        Defer(seconds=2),
        Respond(),
    ],
)
def test_directives_roundtrip(directive):
    assert type(directive).model_validate_json(directive.model_dump_json()) == directive


def test_strength_priority_and_plugin_id_are_deterministic():
    opinions = [
        (200, "plugin-b", ReactOnly(emoji="b")),
        (100, "plugin-c", ReactOnly(emoji="c")),
        (100, "plugin-a", ReactOnly(emoji="a")),
        (0, "plugin-first", Normal()),
    ]
    assert merge_before_attention(opinions, owner_private=False) == ReactOnly(emoji="a")
    assert merge_before_attention(list(reversed(opinions)), owner_private=False) == ReactOnly(
        emoji="a"
    )
    assert isinstance(
        merge_before_attention([*opinions, (500, "last", Ignore())], owner_private=False), Ignore
    )


def test_owner_private_protection_and_attention_defaults():
    assert merge_before_attention([(100, "fake", Ignore())], owner_private=True) == ReactOnly(
        emoji="💤"
    )
    assert merge_before_attention([], owner_private=False) is None
    assert isinstance(
        merge_attention([], private=False, mentioned=False, owner_private=False), Skip
    )
    assert isinstance(
        merge_attention([], private=False, mentioned=True, owner_private=False), Respond
    )
    assert isinstance(
        merge_attention([], private=True, mentioned=False, owner_private=False), Respond
    )
    assert isinstance(
        merge_attention([(100, "fake", Skip())], private=True, mentioned=False, owner_private=True),
        Respond,
    )


def test_delay_uses_longest_and_enforces_cap_unless_skipped():
    opinions = [(100, "a", Defer(seconds=3)), (100, "b", Defer(seconds=200))]
    assert merge_attention(opinions, private=False, mentioned=False, owner_private=False) == Defer(
        seconds=120
    )
    assert isinstance(
        merge_attention(
            [*opinions, (200, "c", Skip())], private=False, mentioned=False, owner_private=False
        ),
        Skip,
    )


def draft():
    return ReplyDraft(
        parts=[TextPart(text="fictional text")],
        conversation=Conversation(
            conversation_id="test_group",
            kind="group",
            scene="public_group",
            channel_id="fake-channel",
        ),
        trace_id="a" * 32,
        form="plain",
    )


def test_reply_form_changes_cannot_change_content_or_destination():
    original = draft()
    card = original.model_copy(
        update={"parts": [CardPart(title="fictional", body=" text")], "form": "card"}
    )
    assert valid_after_generation(original, card)
    assert not valid_after_generation(
        original, card.model_copy(update={"parts": [TextPart(text="changed")]})
    )
    other = original.conversation.model_copy(update={"conversation_id": "other_group"})
    assert not valid_after_generation(original, original.model_copy(update={"conversation": other}))


def test_delivery_shape_and_content_are_preserved():
    original = DeliveryIntent(
        intent_id="a" * 32,
        companion_id="companion_a",
        conversation_id="test_group",
        channel_id="fake-channel",
        kind="reply",
        parts=[TextPart(text="fictional")],
        trace_id="b" * 32,
    )
    timed = original.model_copy(update={"not_before": datetime(2000, 1, 1, tzinfo=timezone.utc)})
    assert valid_before_send([original], [timed])
    assert not valid_before_send([original], [])
    assert not valid_before_send([original], [original, original])
    assert not valid_before_send(
        [original], [original.model_copy(update={"parts": [TextPart(text="changed")]})]
    )
    assert not valid_before_send(
        [original], [original.model_copy(update={"companion_id": "companion_b"})]
    )


def test_context_sections_truncate_then_drop_lowest_priority():
    high = ContextSection(title="high", text="abcdefgh", priority=10)
    low = ContextSection(title="low", text="ijkl", priority=100)
    result = collect_context_sections([(low, 4), (high, 6)], total_budget_chars=8)
    assert [section.title for section in result] == ["high"]
    assert result[0].text == "abcdef"
    assert high.text == "abcdefgh"
