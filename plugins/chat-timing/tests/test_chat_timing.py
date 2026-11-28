import importlib.util
import sys
from datetime import timedelta, timezone
from pathlib import Path
from uuid import uuid4

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


async def candidate(plugin, actor="member", **address):
    clock = plugin.ctx.clock
    event = message(clock, clock.monotonic(), actor=actor, **address)
    await plugin.incoming(event)
    supplied = (event, [event.message])
    delay = await plugin.attention(supplied)
    expected_delay = Defer(seconds=3, prepare=True) if address else Defer(seconds=5)
    assert delay == expected_delay
    clock.advance(delay.seconds)
    return supplied, await plugin.attention(supplied)


async def notify(plugin, name, **fields):
    if name == "message.received":
        fields["scene"] = "public_group"
    await plugin.on_event(
        BusEvent(
            name=name,
            source="core",
            at=plugin.ctx.clock.now(),
            payload={"trace_id": TRACE_ID, "conversation_id": "test_group", **fields},
        )
    )


async def prepare_split(plugin, *, kind="reply", task=None, actor="member"):
    (event, _), decision = await candidate(plugin, actor, mention=True)
    assert isinstance(decision, Respond)
    trace = uuid4().hex
    await notify(
        plugin, "message.received", trace_id=trace, event_id=event.event_id, actor_id=actor
    )
    intents = [
        sample_intent().model_copy(
            update={"trace_id": trace, "intent_id": key * 32, "kind": kind, "task_id": task}
        )
        for key in ("b", "c")
    ]
    await plugin.before_send(intents)
    return intents


async def sent(plugin, intent):
    await notify(
        plugin,
        "reply.sent",
        trace_id=intent.trace_id,
        intent_ids=[intent.intent_id],
        task_id=intent.task_id,
    )


@pytest.mark.parametrize(
    "actor,fatigue,roll,band,expected",
    [
        ("member", 2, 0.99, "medium", "partner"),
        ("peer", 2, 0.99, "medium", "others"),
        ("peer", 5, 0.69, "medium", "others"),
        ("peer", 5, 0.71, "medium", None),
        ("peer", 8, 0, "medium", None),
        ("peer", 8, 0.1, "high", "others"),
        ("peer", 6, 0, "low", None),
        ("owner", 20, 0.99, "medium", "others"),
    ],
)
async def test_follow_up_fatigue_only_and_owner(
    module, tmp_path, monkeypatch, actor, fatigue, roll, band, expected
):
    plugin, clock = await setup(module, tmp_path, activity=band, tuning={f"{band}_probability": 0})
    first, _ = await prepare_split(plugin)
    await sent(plugin, first)
    event = message(clock, 2, actor=actor)
    await plugin.incoming(event)
    clock.advance(5)
    state = plugin.group("test_group", clock.monotonic())
    state.fatigue, state.level, state.backoff_end = fatigue, 3, 900
    monkeypatch.setattr(plugin.ctx.random, "random", lambda: roll)
    result = await plugin.attention((event, [event.message]))
    if expected is None:
        assert isinstance(result, Skip)
    else:
        assert isinstance(result, Judge) and result.facts.follow_up == expected


async def test_split_receipts_extend_window_and_partners_expire_individually(module, tmp_path):
    plugin, clock = await setup(module, tmp_path, tuning={"medium_probability": 1})
    first, second = await prepare_split(plugin)
    await sent(plugin, first)
    _, result = await candidate(plugin, "peer")
    assert result.facts.follow_up == "others"
    clock.advance(45)
    await sent(plugin, second)
    clock.advance(75)
    _, result = await candidate(plugin)
    assert result.facts.follow_up == "partner"
    # A second reply to another person keeps the group window open.
    first, _ = await prepare_split(plugin, actor="peer")
    await sent(plugin, first)
    clock.advance(45)
    _, result = await candidate(plugin)
    assert result.facts.follow_up == "others"
    clock.advance(5)
    _, result = await candidate(plugin, "peer")
    assert result.facts.follow_up == "partner"
    clock.advance(121)
    _, result = await candidate(plugin)
    assert result.facts.follow_up == "none"


@pytest.mark.parametrize("kind,task", [("proactive", None), ("reply", "abcdabcd")])
async def test_proactive_and_task_receipts_do_not_open_follow_up(module, tmp_path, kind, task):
    plugin, _ = await setup(module, tmp_path, tuning={"medium_probability": 1})
    first, _ = await prepare_split(plugin, kind=kind, task=task)
    await sent(plugin, first)
    _, result = await candidate(plugin)
    assert result.facts.follow_up == "none"


async def test_follow_up_ten_second_gap_and_normal_thirty_second_gap(module, tmp_path):
    plugin, clock = await setup(
        module, tmp_path, tuning={"follow_up_seconds": 18, "medium_probability": 1}
    )
    _, normal = await candidate(plugin)
    assert normal.facts.follow_up == "none"
    first, _ = await prepare_split(plugin)
    await sent(plugin, first)
    _, first_query = await candidate(plugin)
    assert first_query.facts.follow_up == "partner"
    _, too_soon = await candidate(plugin)
    assert isinstance(too_soon, Skip)
    _, second_query = await candidate(plugin)
    assert second_query.facts.follow_up == "partner"
    clock.advance(4)
    _, outside = await candidate(plugin)
    assert isinstance(outside, Skip)
    clock.advance(21)
    _, normal = await candidate(plugin)
    assert normal.facts.follow_up == "none"


@pytest.mark.parametrize("actor,expected", [("owner", Respond), ("peer", Skip)])
@pytest.mark.parametrize("mode", ["rules", "model"])
async def test_follow_up_fallback_keeps_original_decision_after_expiry(
    module, tmp_path, actor, expected, mode
):
    plugin, clock = await setup(module, tmp_path, judge=mode, tuning={"medium_probability": 0})
    first, _ = await prepare_split(plugin)
    await sent(plugin, first)
    args, result = await candidate(plugin, actor)
    if mode == "model":
        assert isinstance(result, Judge)
        clock.advance(121)
        result = await plugin.attention((*args, Judgment(status="failed")))
    assert isinstance(result, expected)
    if expected is Respond:
        assert result.recheck_after == 15


async def test_rules_follow_up_throttles_owner_without_model_judgments(module, tmp_path):
    plugin, clock = await setup(
        module, tmp_path, judge="rules", tuning={"follow_up_seconds": 12, "medium_probability": 1}
    )
    first, _ = await prepare_split(plugin)
    await sent(plugin, first)
    _, decision = await candidate(plugin, "owner")
    assert isinstance(decision, Respond)
    _, decision = await candidate(plugin, "owner")
    assert isinstance(decision, Skip)
    clock.advance(3)
    _, decision = await candidate(plugin, "owner")
    assert isinstance(decision, Skip)
    assert (await plugin.status({}))["json"]["groups"]["test_group"]["judgments_today"] == 0


@pytest.mark.parametrize(
    "address", [{"mention": True}, {"reply_to_self": True}, {"name": "external"}]
)
async def test_follow_up_keeps_addressed_priority(module, tmp_path, address):
    plugin, _ = await setup(module, tmp_path, tuning={"medium_probability": 0})
    first, _ = await prepare_split(plugin)
    await sent(plugin, first)
    await candidate(plugin, "peer")
    args, decision = await candidate(plugin, "peer", **address)
    if "name" in address:
        assert (
            isinstance(decision, Judge) and decision.prepare and decision.facts.follow_up == "none"
        )
        decision = await plugin.attention((*args, Judgment(status="failed")))
    assert decision == Respond(focus=args[0].message.ref)


async def test_follow_up_recheck_fatigue_cold_and_natural_count_once(module, tmp_path):
    plugin, clock = await setup(module, tmp_path, tuning={"medium_probability": 0})
    first, _ = await prepare_split(plugin)
    await sent(plugin, first)
    args, query = await candidate(plugin)
    assert isinstance(query, Judge)
    state = plugin.group("test_group", clock.monotonic())
    state.fatigue = 4
    state.rounds = 3
    result = await plugin.attention((*args, Judgment(status="ok", join=True, new_topic=True)))
    assert result == Respond(focus=args[0].message.ref, recheck_after=15)
    assert state.fatigue == 2
    await notify(plugin, "message.received", event_id=args[0].event_id, actor_id="member")
    intent = sample_intent()
    await plugin.before_send([intent])
    await sent(plugin, intent)
    await sent(plugin, intent)
    assert state.fatigue > 2 and state.natural == 1 and state.judgments == 1
    clock.advance(180)
    assert (await plugin.status({}))["json"]["groups"]["test_group"]["ignored_level"] == 1


@pytest.mark.parametrize("target", ["history", "self", "evicted", "none"])
async def test_follow_up_partner_uses_focus_sender(module, tmp_path, target):
    plugin, clock = await setup(module, tmp_path, tuning={"medium_probability": 1})
    history = message(clock, "history", actor="peer")
    await plugin.incoming(history)
    if target == "evicted":
        for i in range(50):
            await plugin.incoming(message(clock, i, actor="member"))
    clock.advance(1)
    args, query = await candidate(plugin, "member")
    assert isinstance(query, Judge)
    own_ref = history.message.ref.model_copy(update={"platform_message_id": "fictional-output"})
    focus = {
        "history": history.message.ref,
        "evicted": history.message.ref,
        "self": own_ref,
        "none": None,
    }[target]
    decision = await plugin.attention((*args, Judgment(status="ok", join=True, focus=focus)))
    assert decision.focus == (focus or args[0].message.ref)
    await notify(plugin, "message.received", event_id=args[0].event_id, actor_id="member")
    intent = sample_intent()
    await plugin.before_send([intent])
    await sent(plugin, intent)
    _, from_peer = await candidate(plugin, "peer")
    assert from_peer.facts.follow_up == ("partner" if target == "history" else "others")
    clock.advance(5)
    _, from_member = await candidate(plugin, "member")
    assert from_member.facts.follow_up == ("others" if target == "history" else "partner")
