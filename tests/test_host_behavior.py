import pytest

from clytie.sdk.testing.samples import sample_event, sample_intent
from clytie.sdk.types import ReplyDraft

from .host_helpers import SOURCE, enable, host, plugin


@pytest.mark.parametrize("order", [("plugin-a", "plugin-b"), ("plugin-b", "plugin-a")])
async def test_behavior_merge_is_independent_of_enable_order(tmp_path, order):
    for pid, name in [("plugin-a", "a"), ("plugin-b", "b")]:
        source = (
            SOURCE
            + f'''
    async def before_attention(self, input):
        from clytie.sdk.behavior import Mode
        return Mode(name="{name}", params={{}})
    async def attention(self, input):
        from clytie.sdk.behavior import Defer
        return Defer(seconds={30 if name == "a" else 60})
'''
        )
        plugin(
            tmp_path,
            pid,
            source,
            contributes={
                "behavior": {
                    "before_attention": {"handler": "before_attention"},
                    "attention": {"handler": "attention"},
                }
            },
        )
    h = host(tmp_path)
    for pid in order:
        await enable(h, pid)
    event = sample_event(h.clock)
    assert (
        await h.behavior(
            "companion_a", "before_attention", (event, h.companion_state("companion_a"))
        )
    ).name == "a"
    assert (await h.behavior("companion_a", "attention", (event, []))).seconds == 60
    await h.close()


async def test_pipeline_rejects_content_edits_and_in_place_intent_mutation(tmp_path):
    source = (
        SOURCE
        + """
    async def after(self, draft):
        from clytie.sdk.types import TextPart
        return draft.model_copy(update={"parts": [TextPart(text="fictional-tampered")]})
    async def before(self, intents):
        intents.clear()
        return None
"""
    )
    plugin(
        tmp_path,
        "tampered",
        source,
        contributes={
            "behavior": {
                "after_generation": {"handler": "after"},
                "before_send": {"handler": "before"},
            }
        },
    )
    h = host(tmp_path)
    await enable(h, "tampered")
    event = sample_event(h.clock)
    draft = ReplyDraft(
        parts=sample_intent().parts,
        conversation=event.conversation,
        trace_id="a" * 32,
        form="plain",
    )
    assert await h.behavior("companion_a", "after_generation", draft) == draft
    intents = [sample_intent()]
    assert await h.behavior("companion_a", "before_send", intents) == intents
    assert len(h.log.entries) >= 2
    await h.close()


@pytest.mark.parametrize("declared", [False, True])
async def test_react_only_permission_checked_in_real_host_invocation(tmp_path, declared):
    from clytie.sdk.behavior import Normal, ReactOnly

    source = (
        SOURCE
        + """
    async def before_attention(self, input):
        from clytie.sdk.behavior import ReactOnly
        return ReactOnly(emoji="💤")
"""
    )
    plugin(
        tmp_path,
        "react-example",
        source,
        permissions=["message.react"] if declared else [],
        contributes={"behavior": {"before_attention": {"handler": "before_attention"}}},
        failure_default={"before_attention": {"type": "normal"}},
    )
    h = host(tmp_path)
    await enable(h, "react-example")
    event = sample_event(h.clock)
    result = await h.invoke(
        "companion_a",
        "react-example",
        "behavior.before_attention",
        "before_attention",
        (event, h.companion_state("companion_a")),
    )
    assert isinstance(result, ReactOnly if declared else Normal)
    assert any(e[1] == "plugin.failure" for e in h.log.entries) is not declared
    await h.close()
    await h.event_bus.drain()


async def test_owner_ignore_protection_and_invalid_reaction_fallback(tmp_path):
    from clytie.sdk.behavior import ReactOnly

    source = (
        SOURCE
        + """
    async def before_attention(self, input):
        from clytie.sdk.behavior import Ignore
        return Ignore()
"""
    )
    plugin(
        tmp_path,
        "ignore-example",
        source,
        contributes={"behavior": {"before_attention": {"handler": "before_attention"}}},
    )
    h = host(tmp_path)
    await enable(h, "ignore-example")
    event = sample_event(h.clock)
    event = event.model_copy(
        update={
            "conversation": event.conversation.model_copy(
                update={"kind": "private", "scene": "owner_private"}
            )
        }
    )
    result = await h.behavior(
        "companion_a", "before_attention", (event, h.companion_state("companion_a"))
    )
    assert isinstance(result, ReactOnly)
    assert not h.failures
    await h.close()
    await h.event_bus.drain()


async def test_failure_default_cannot_bypass_react_permission(tmp_path):
    source = (
        SOURCE
        + """
    async def before_attention(self, input):
        raise ValueError("fictional failure")
"""
    )
    plugin(
        tmp_path,
        "fallback-example",
        source,
        contributes={"behavior": {"before_attention": {"handler": "before_attention"}}},
        failure_default={"before_attention": {"type": "react_only", "emoji": "💤"}},
    )
    h = host(tmp_path)
    await enable(h, "fallback-example")
    result = await h.invoke(
        "companion_a", "fallback-example", "behavior.before_attention", "before_attention", None
    )
    assert result is None
    assert any(e[1] == "plugin.invalid_reaction_default" for e in h.log.entries)
    await h.close()
    await h.event_bus.drain()
