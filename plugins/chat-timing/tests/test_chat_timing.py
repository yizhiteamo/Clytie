import importlib.util
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from clytie.sdk.behavior import Defer, Judge, Judgment, Respond, Skip
from clytie.sdk.testing import FakeContext, MockClock
from clytie.sdk.testing.samples import TRACE_ID, sample_event, sample_intent
from clytie.sdk.types import Addressed, BusEvent, ContextRequest


@pytest.fixture
def module():
    path = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "chat_timing_test", path / "plugin.py", submodule_search_locations=[str(path)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def setup(module, tmp_path, **config):
    clock = MockClock(local_tz=timezone(timedelta(hours=8)))
    plugin = module.Plugin()
    await plugin.start(FakeContext(root=tmp_path, clock=clock, config=config))
    return plugin, clock


def message(clock, index, *, actor="member", kind="group", **address):
    event = sample_event(clock)
    ref = event.message.ref.model_copy(update={"platform_message_id": f"fictional-{index}"})
    sender = event.message.sender.model_copy(
        update={"actor_id": actor, "role": "owner" if actor == "owner" else "member"}
    )
    return event.model_copy(
        update={
            "event_id": f"fake-channel:fictional-{index}",
            "message": event.message.model_copy(update={"ref": ref, "sender": sender}),
            "addressed": Addressed(**address),
            "conversation": event.conversation.model_copy(update={"kind": kind}),
        }
    )


@pytest.mark.parametrize(
    "kind,address",
    [("private", {}), ("group", {"mention": True}), ("group", {"reply_to_self": True})],
)
async def test_batch_quiet_slides_but_stops_at_eight(module, tmp_path, kind, address):
    plugin, clock = await setup(module, tmp_path)
    batch = []
    for i, seconds in enumerate([3, 3, 3, 2]):
        event = message(clock, i, kind=kind, **address)
        await plugin.incoming(event)
        batch.append(event.message)
        decision = await plugin.attention((event, batch))
        assert decision == Defer(seconds=seconds, prepare=True)
        clock.advance(2)
    if kind == "group":
        group = plugin.group("test_group", 8)
        group.fatigue, group.level, group.backoff_end = 15, 3, 100
    assert isinstance(await plugin.attention((event, batch)), Respond)


async def test_natural_waits_for_group_silence_bounded_at_twenty(module, tmp_path):
    plugin, clock = await setup(module, tmp_path, judge="rules", tuning={"medium_probability": 1})
    first = message(clock, 0)
    await plugin.incoming(first)
    for i in range(1, 6):
        assert await plugin.attention((first, [first.message])) == Defer(
            seconds=min(5, 20 - clock.monotonic())
        )
        clock.advance(4)
        await plugin.incoming(message(clock, i, actor="peer"))
    assert isinstance(await plugin.attention((first, [first.message])), Respond)


@pytest.mark.parametrize(
    "judgment,expected",
    [
        (Judgment(status="ok", calling_me=False), Skip),
        (Judgment(status="ok", calling_me=True), Respond),
        (Judgment(status="failed"), Respond),
    ],
)
async def test_name_confirmation(module, tmp_path, judgment, expected):
    plugin, clock = await setup(module, tmp_path)
    event = message(clock, 0, name="external")
    await plugin.incoming(event)
    clock.advance(3)
    query = await plugin.attention((event, [event.message]))
    assert isinstance(query, Judge) and query.facts.name_hit
    decision = await plugin.attention((event, [event.message], judgment))
    assert isinstance(decision, expected)
    if expected is Respond:
        assert decision == Respond(focus=event.message.ref)


def test_share_and_pair_fatigue(module):
    group = module.Group(module.Tuning(), 0)
    for i in range(7):
        group.incoming(0, str(i % 5), False)
    for i in range(3):
        group.replied(0, str(i), False, False)
    assert group.share() == (5, 0.3) and group.fatigue == 0
    for owner in (False, True):
        group = module.Group(module.Tuning(), 0)
        for i in range(4):
            group.incoming(i, "member", False)
            group.replied(i, "member", owner, False)
            if i < 3:
                assert group.fatigue == 0
        assert group.fatigue == (0 if owner else 1.5)


async def test_fatigue_context_decay_topic_and_stop(module, tmp_path):
    plugin, clock = await setup(module, tmp_path, tuning={"medium_probability": 1})
    event = message(clock, 0)
    await plugin.incoming(event)
    group = plugin.group("test_group", 0)
    clock.advance(5)
    group = plugin.group("test_group", 5)
    group.fatigue = 8
    assert isinstance(await plugin.attention((event, [event.message])), Skip)
    hint = await plugin.hint(
        ContextRequest(scene="public_group", conversation=event.conversation, budget_chars=100)
    )
    assert "累" in hint.text
    decision = await plugin.attention(
        (event, [event.message], Judgment(status="ok", new_topic=True))
    )
    assert isinstance(decision, Skip) and group.fatigue == 4
    clock.advance(600)
    assert plugin.group("test_group", 605).fatigue == 2


async def test_local_schedule_and_judge_fallback(module, tmp_path):
    plugin, clock = await setup(
        module,
        tmp_path,
        conversations={"test_group": "low"},
        schedule=[{"from": "22:00", "to": "09:00", "activity": "high"}],
        tuning={"high_probability": 1},
    )
    event = message(clock, 0)
    await plugin.incoming(event)
    clock.advance(5)
    assert isinstance(await plugin.attention((event, [event.message])), Judge)
    decision = await plugin.attention((event, [event.message], Judgment(status="failed")))
    assert isinstance(decision, Respond) and decision.recheck_after == 15
    assert isinstance(await plugin.attention((event, [event.message])), Skip)
    assert (await plugin.status({}))["json"]["groups"]["test_group"]["activity"] == "high"
    clock.advance(3600)
    assert (await plugin.status({}))["json"]["groups"]["test_group"]["activity"] == "low"


async def test_receipt_counted_once_and_backoff_recovers(module, tmp_path):
    plugin, clock = await setup(module, tmp_path, judge="rules", tuning={"medium_probability": 1})
    event = message(clock, 0)
    await plugin.incoming(event)
    clock.advance(5)
    assert isinstance(await plugin.attention((event, [event.message])), Respond)

    async def emit(name, **fields):
        await plugin.on_event(
            BusEvent(
                name=name,
                source="core",
                at=clock.now(),
                payload={"trace_id": TRACE_ID, "conversation_id": "test_group", **fields},
            )
        )

    await emit("message.received", event_id=event.event_id, actor_id="member", scene="public_group")
    intent = sample_intent()
    await plugin.before_send([intent])
    state = plugin.group("test_group", 5)
    assert state.natural == 0 and state.judgments == 0
    for _ in range(2):
        await emit("reply.sent", intent_ids=[intent.intent_id], task_id=None)
    assert state.natural == 1 and state.level == 0
    clock.advance(180)
    assert (await plugin.status({}))["json"]["groups"]["test_group"]["ignored_level"] == 1
    await plugin.incoming(message(clock, 1, actor="owner", reply_to_self=True))
    assert state.level == 0


def test_activity_rules_and_decay_of_backoff(module):
    state = module.Group(module.Tuning(), 0)
    state.last_reply = 0
    assert state.willingness(0, True, False, 600) == 0.15
    assert state.willingness(0, False, True, 1) == pytest.approx(0.15)
    state.fatigue = 8
    assert state.willingness(1, False, False, 600) == 0
    assert 0 < state.willingness(2, False, False, 600) < 0.3
    assert state.willingness(0, False, False, 600) == 0
    state.fatigue = 0
    for i in range(5):
        sent = i * 200
        state.replied(sent, "member", False, True)
        state.refresh(sent + 180, "today")
        assert state.backoff_end == sent + 180 + min(7200, 900 * 2**i)
    assert state.willingness(2, True, True, 1000) == pytest.approx(0.45)
    state.refresh(state.backoff_end, "tomorrow")
    assert state.level == state.natural == 0


async def test_mention_at_start_of_batch_remains_addressed(module, tmp_path):
    plugin, clock = await setup(module, tmp_path, tuning={"medium_probability": 0})
    first = message(clock, 1, mention=True)
    await plugin.incoming(first)
    clock.advance(1)
    second = message(clock, 2)
    await plugin.incoming(second)
    clock.advance(3)
    decision = await plugin.attention((second, [first.message, second.message]))
    assert decision == Respond(focus=second.message.ref)


@pytest.mark.parametrize(
    "address,actor",
    [
        ({"reply_to_self": True}, "member"),
        ({"mention": True}, "member"),
        ({"name": "external"}, "member"),
        ({"reply_to_self": True}, "owner"),
    ],
)
async def test_cold_group_and_early_recovery(module, tmp_path, address, actor):
    plugin, clock = await setup(module, tmp_path)
    group = plugin.group("test_group", 0)
    group.replied(0, "member", False, True)
    clock.advance(179)
    await plugin.incoming(message(clock, 1))
    clock.advance(1)
    assert plugin.group("test_group", 180).level == 0
    group.replied(180, "member", False, True)
    clock.advance(180)
    assert plugin.group("test_group", 360).level == 1
    await plugin.incoming(message(clock, 2))
    assert group.level == 1
    await plugin.incoming(message(clock, 3, actor=actor, **address))
    assert group.level == 0


def test_quiet_streak_restarts_after_a_warm_reply(module):
    group = module.Group(module.Tuning(), 0)
    for start, duration in [(0, 900), (200, 1800)]:
        group.replied(start, "member", False, True)
        group.refresh(start + 180, "today")
        assert group.backoff_end == start + 180 + duration
    group.replied(400, "member", False, True)
    group.incoming(410, "third", False)
    assert group.level == 2
    group.replied(500, "member", False, True)
    group.refresh(680, "today")
    assert group.level == 1 and group.backoff_end == 380 + 1800
    group.replied(2400, "member", False, True)
    group.incoming(2410, "third", False)
    group.replied(2500, "member", False, True)
    group.refresh(2680, "today")
    assert group.level == 1 and group.backoff_end == 2680 + 900


@pytest.mark.parametrize("gap,fast", [(2, True), (10, False)])
def test_paired_burst_counts_each_reply_from_round_four(module, gap, fast):
    group = module.Group(module.Tuning(), 0)
    group.incoming(0, "member", False)
    group.replied(1, "member", False, False)
    for pair in range(2):
        now = group.last_reply + gap
        group.incoming(now, "member", False)
        group.incoming(now + 1, "member", False)
        for offset in (2, 3):
            group.replied(now + offset, "member", False, False)
            rounds = 2 + pair * 2 + offset - 2
            assert group.rounds == rounds and group.fast is fast
            assert group.fatigue == max(0, rounds - 3) * (1.5 if fast else 1)


@pytest.mark.parametrize("band,misses,expected", [(1, 2, 0.05), (0, 1, 0)])
def test_cold_streak_downgrades_activity_once(module, band, misses, expected):
    group = module.Group(module.Tuning(), 0)
    for i in range(misses):
        group.replied(i * 200, "member", False, True)
        group.refresh(i * 200 + 180, "today")
    now = group.last_reply + 301
    group.refresh(now, "today")
    assert group.level == misses
    assert group.willingness(band, False, False, now) == expected


@pytest.mark.parametrize(
    "address", [{"mention": True}, {"reply_to_self": True}, {"name": "external"}]
)
async def test_addressed_focus_without_recheck_in_rules(module, tmp_path, address):
    plugin, clock = await setup(module, tmp_path, judge="rules")
    event = message(clock, 1, **address)
    await plugin.incoming(event)
    clock.advance(3)
    assert await plugin.attention((event, [event.message])) == Respond(focus=event.message.ref)


async def test_status_combines_unregistered_groups(module, tmp_path):
    plugin, clock = await setup(
        module,
        tmp_path,
        activity="low",
        conversations={"anon:first": "high"},
        schedule=[{"from": "08:00", "to": "08:01", "activity": "medium"}],
    )
    for cid, fatigue, level, judgments, natural in [
        ("anon:first", 2, 3, 7, 1),
        ("anon:second", 5, 1, 2, 6),
        ("test_group", 1, 0, 1, 0),
    ]:
        state = plugin.group(cid, clock.monotonic())
        state.fatigue, state.level, state.backoff_end = fatigue, level, 1000
        state.judgments, state.natural = judgments, natural
    response = await plugin.status({})
    assert "anon:" not in str(response)
    rows = response["json"]["groups"]
    assert set(rows) == {"未登记的群", "test_group"}
    assert rows["未登记的群"] == dict(
        activity="medium", fatigue=5, ignored_level=3, judgments_today=9, natural_today=7
    )
    clock.advance(60)
    assert (await plugin.status({}))["json"]["groups"]["未登记的群"]["activity"] == "low"


async def test_reloaded_addressed_batch_continues_without_wait(module, tmp_path):
    plugin, clock = await setup(module, tmp_path)
    event = message(clock, 1, mention=True)
    batch = (event, [event.message])
    await plugin.incoming(event)
    assert await plugin.attention(batch) == Defer(seconds=3, prepare=True)
    ctx = plugin.ctx
    await plugin.stop()
    await plugin.start(ctx)
    assert await plugin.attention(batch) == Respond(focus=event.message.ref)
