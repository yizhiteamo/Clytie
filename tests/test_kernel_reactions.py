"""状态回应仅使用内存假平台、MockClock 和临时账本。"""

from uuid import uuid4

import pytest

from clytie.kernel.delivery import Delivery
from clytie.kernel.ledger import Ledger
from clytie.kernel.reactions import Reactions
from clytie.sdk.testing import MockClock
from clytie.sdk.types import (
    Actor,
    ChannelFeatures,
    Conversation,
    EventEnvelope,
    Invocation,
    Receipt,
    ToolCall,
)


class Channel:
    def __init__(self, clock, *, remove=True, supports=True, status="sent"):
        self.clock, self.remove, self.supports, self.status = clock, remove, supports, status
        self.calls, self.markers, self.text = [], [], []

    def features(self):
        return ChannelFeatures(
            max_text_chars=1000,
            supports_react=self.supports,
            supports_react_remove=self.remove,
            supports_quote=True,
            supports_card=False,
            media_limits={},
        )

    async def react(self, intent, cancel):
        self.calls.append(intent)
        if self.status == "sent":
            if self.remove and intent.replaces_emoji in self.markers:
                self.markers.remove(intent.replaces_emoji)
            self.markers.append(intent.emoji)
        return Receipt(intent_id=intent.intent_id, status=self.status, at=self.clock.now())

    async def send(self, intent, cancel):
        self.text.append(intent)
        raise AssertionError("state reactions must never send text")


def fixture(tmp_path, *, role="owner", sourced=True, config=None, **channel_kwargs):
    clock = MockClock()
    ledger = Ledger(tmp_path / "ledger" / "ledger.sqlite3", "companion_a", clock)
    channel = Channel(clock, **channel_kwargs)
    actor = Actor(actor_id="owner" if role == "owner" else "member", role=role, channel_id="fake")
    conv = Conversation(
        conversation_id="test_group", kind="group", scene="public_group", channel_id="fake"
    )
    inv = Invocation(
        companion_id="companion_a", actor=actor, conversation=conv, trace_id=uuid4().hex
    )
    if sourced:
        event = EventEnvelope(
            event_id="fake:example",
            type="conversation.message",
            companion_id="companion_a",
            channel_id="fake",
            conversation=conv,
            received_at=clock.now(),
            message=dict(
                ref=dict(
                    channel_id="fake", conversation_id="test_group", platform_message_id="example"
                ),
                sender=actor,
                parts=[dict(type="text", text="Fictional task")],
                sent_at=clock.now(),
            ),
        )
        ledger.claim_inbound(event)
        record = ledger.open_record(event_id=event.event_id, invocation=inv)
    else:
        record = ledger.open_record(invocation=inv)
    ledger.claim_invocation(
        record["trace_id"],
        "echo-tool",
        ToolCall(call_id="call", tool="echo", arguments={}),
        "example",
        timeout=600,
        schema={},
    )
    delivery = Delivery(ledger, clock, {"fake": channel})
    reactions = Reactions(ledger, delivery, clock, config)
    return ledger, reactions, channel, clock, record["trace_id"]


async def test_four_states_replace_last_confirmed_marker(tmp_path):
    ledger, reactions, channel, clock, trace = fixture(tmp_path, config={"min_interval_seconds": 0})
    for state in ("accepted", "running", "done", "failed"):
        await reactions.request(trace, state)
        await reactions.drain()
    assert [i.emoji for i in channel.calls] == ["👌", "⏳", "✅", "❌"]
    assert [i.replaces_emoji for i in channel.calls] == [None, "👌", "⏳", "✅"]
    assert channel.markers == ["❌"]
    assert ledger.get("record", trace)["delivery_status"] == "none"
    assert channel.text == []
    ledger.close()


async def test_merge_within_two_seconds_and_terminal_not_delayed(tmp_path):
    ledger, reactions, channel, clock, trace = fixture(tmp_path)
    await reactions.request(trace, "accepted")
    await clock.drain()
    clock.advance(1)
    await reactions.request(trace, "running")
    await clock.drain()
    assert not channel.calls
    await clock.advance_and_drain(2)
    assert [i.emoji for i in channel.calls] == ["⏳"]
    await reactions.request(trace, "done")
    await reactions.drain()
    assert [i.emoji for i in channel.calls] == ["⏳", "✅"]
    ledger.close()


@pytest.mark.parametrize("status", ["failed", "unknown"])
async def test_failed_or_unknown_reaction_never_retries_or_sends_text(tmp_path, status):
    ledger, reactions, channel, clock, trace = fixture(tmp_path, status=status)
    await reactions.request(trace, "failed")
    await reactions.drain()
    await clock.advance_and_drain(100)
    assert len(channel.calls) == 1 and not channel.text
    assert ledger.get("record", trace)["delivery_status"] == "none"
    ledger.close()


async def test_platform_without_remove_only_adds(tmp_path):
    ledger, reactions, channel, clock, trace = fixture(
        tmp_path, remove=False, config={"min_interval_seconds": 0}
    )
    await reactions.request(trace, "running")
    await reactions.drain()
    await reactions.request(trace, "done")
    await reactions.drain()
    assert channel.markers == ["⏳", "✅"]
    ledger.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(role="member", config={"audience": "owner"}),
        dict(sourced=False),
        dict(supports=False),
        dict(config={"enabled": False}),
    ],
)
async def test_ineligible_tasks_have_no_reactions(tmp_path, kwargs):
    ledger, reactions, channel, clock, trace = fixture(tmp_path, **kwargs)
    await reactions.request(trace, "accepted")
    await reactions.request(trace, "done")
    await clock.advance_and_drain(5)
    assert channel.calls == [] and channel.text == []
    ledger.close()


async def test_cancelled_task_can_register_failed_after_seal(tmp_path):
    ledger, reactions, channel, clock, trace = fixture(tmp_path)
    ledger.seal(trace, "cancelled")
    with ledger.transaction():
        r = ledger.get("record", trace)
        r["cancelled"] = True
        ledger.put("record", trace, r)
    await reactions.request(trace, "failed")
    await reactions.drain()
    assert [i.emoji for i in channel.calls] == ["❌"]
    assert ledger.get("record", trace)["exec_status"] == "cancelled"
    ledger.close()


async def test_restart_marks_interrupted_and_updates_reaction_once(tmp_path):
    ledger, reactions, channel, clock, trace = fixture(tmp_path, config={"min_interval_seconds": 0})
    await reactions.request(trace, "running")
    await reactions.drain()
    await reactions.restart(ledger.restart())
    await reactions.restart(ledger.restart())
    assert [i.emoji for i in channel.calls] == ["⏳", "❌"]
    assert ledger.get("record", trace)["exec_status"] == "interrupted"
    assert channel.text == []
    ledger.close()


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (["error"], "❌"),
        (["denied"], "❌"),
        (["cancelled"], "❌"),
        (["error", "ok"], "✅"),
        ([], "✅"),
    ],
)
async def test_final_reaction_uses_last_heavy_result_only(tmp_path, statuses, expected):
    ledger, reactions, channel, _, trace = fixture(tmp_path, config={"min_interval_seconds": 0})
    try:
        for status in statuses:
            with ledger.transaction():
                ledger.audit("tool.heavy_result", trace_id=trace, status=status)
            reactions.record_heavy_result(trace, status)
        await reactions.request(trace, "done")
        await reactions.drain()
        assert channel.calls[-1].emoji == expected
        assert trace not in reactions.heavy_results
        outcomes = [a for a in ledger.rows("audit") if a.get("reaction_outcome") == "tool_failed"]
        assert bool(outcomes) == (expected == "❌")
    finally:
        ledger.close()


async def test_plain_task_never_reads_audit(tmp_path):
    import sqlite3

    ledger, reactions, channel, _, trace = fixture(tmp_path, config={"min_interval_seconds": 0})
    reads = []

    def authorize(action, table, column, database, source):
        if action == sqlite3.SQLITE_READ and table == "audit":
            reads.append(column)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        with ledger.transaction():
            ledger.audit("tool.heavy_result", trace_id=trace, status="error")
        ledger.db.set_authorizer(authorize)
        await reactions.request(trace, "done")
        await reactions.drain()
        assert channel.calls[-1].emoji == "✅"
        assert reads == []
    finally:
        ledger.db.set_authorizer(None)
        ledger.close()


@pytest.mark.parametrize("state,enabled", [("done", False), ("failed", True)])
async def test_terminal_reaction_clears_heavy_state(tmp_path, state, enabled):
    ledger, reactions, _, _, trace = fixture(tmp_path, config={"enabled": enabled})
    try:
        reactions.record_heavy_result(trace, "error")
        reactions.record_heavy_result("other_trace", "ok")
        await reactions.request(trace, state)
        await reactions.drain()
        assert reactions.heavy_results == {"other_trace": "ok"}
        reactions.forget("other_trace")
        reactions.forget("other_trace")
        assert reactions.heavy_results == {}
    finally:
        ledger.close()
