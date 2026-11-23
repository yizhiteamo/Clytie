import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest

from clytie.sdk import CancelToken, ProactiveOutcome, ProactiveSubmission
from clytie.sdk.testing import FakeChannel, FakeContext, FakeModel, MockClock
from clytie.sdk.testing.samples import sample_event, sample_intent, sample_request
from clytie.sdk.types import Addressed, BusEvent, ChatMessage, ReplyDraft, TextPart, Tick

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
TARGET = "test_private"


def local(day, hm):
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=TZ)


class Submit:
    def __init__(self):
        self.calls = []
        self.admission = "accepted"
        self.outcomes = {}
        self.queries = []

    async def submit(self, cid, *, topic, reason, tools):
        self.calls.append((cid, topic, reason, tools))
        if cid != TARGET:
            raise ValueError("proactive target denied")
        if self.admission != "accepted":
            return ProactiveSubmission(status=self.admission)
        trace = f"{len(self.calls):032x}"
        self.outcomes[trace] = ProactiveOutcome(status="pending")
        return ProactiveSubmission(status="accepted", trace_id=trace)

    async def result(self, trace):
        self.queries.append(trace)
        return self.outcomes.get(trace, ProactiveOutcome(status="not_found"))


def load_plugin():
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    entry, name = manifest["entry"].split(":")
    path = ROOT / f"{entry}.py"
    assert path.is_file(), "缺少主动私聊插件入口"
    spec = importlib.util.spec_from_file_location(
        "proactive_chat_test", path, submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return getattr(module, name)()


async def setup(tmp_path, start=None, seed=0, **config):
    clock = MockClock(start or local("2026-06-15", "10:00"), local_tz=TZ)
    ctx = FakeContext(
        root=tmp_path,
        plugin_id="proactive-chat",
        clock=clock,
        random=Random(seed),
        config={"conversation": TARGET, **config},
    )
    ctx.proactive = Submit()
    plugin = load_plugin()
    await plugin.start(ctx)
    return plugin, ctx


async def tick(plugin, clock):
    await plugin.on_tick(
        Tick(schedule_id="tick", scheduled_at=clock.now(), now=clock.now(), missed=0)
    )


def incoming(clock, *, actor="owner", kind="private", index=1, **marks):
    incoming = sample_event(clock)
    ref = incoming.message.ref.model_copy(
        update={
            "conversation_id": TARGET if kind == "private" else "test_group",
            "platform_message_id": f"fake-{index}",
        }
    )
    sender = incoming.message.sender.model_copy(update={"role": actor, "actor_id": f"test_{actor}"})
    return incoming.model_copy(
        update={
            "event_id": f"fake-channel:fake-{index}",
            "addressed": Addressed(**marks),
            "message": incoming.message.model_copy(update={"ref": ref, "sender": sender}),
            "conversation": incoming.conversation.model_copy(
                update={
                    "conversation_id": ref.conversation_id,
                    "kind": kind,
                    "scene": "public_group" if kind == "group" else "owner_private",
                }
            ),
        }
    )


def bus(clock, name, **payload):
    return BusEvent(name=name, at=clock.now(), source="core", payload=payload)


async def receipt(plugin, clock, trace, *, cid=TARGET, intent_id="2" * 32):
    await plugin.on_event(
        bus(clock, "reply.sent", trace_id=trace, conversation_id=cid, intent_ids=[intent_id])
    )


def draft(clock, trace, text, *, task_id=None, focus=None):
    return ReplyDraft(
        conversation=incoming(clock).conversation,
        form="plain",
        trace_id=trace,
        task_id=task_id,
        focus=focus,
        parts=[TextPart(text=text)] if text else [],
    )


async def status(plugin):
    response = await plugin.status({})
    assert response["status"] == 200
    return response["json"]


def test_declares_only_schedule_observation_status_and_submit_permission():
    path = ROOT / "plugin.json"
    assert path.is_file(), "缺少插件清单"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["permissions"] == ["message.proactive"]
    assert manifest["config"]["required"] == ["conversation"]
    assert manifest["contributes"]["api_routes"][0]["path"] == "/plugins/proactive-chat/status"
    assert manifest["contributes"]["schedule"][0]["every"] == "PT1M"
    assert set(manifest["contributes"]["events"]["subscribe"]) == {"message.received", "reply.sent"}


async def test_unsettled_opening_cannot_overlap_a_new_topic_or_be_replayed_after_restart(tmp_path):
    p, ctx = await setup(tmp_path, start=local("2026-06-15", "00:00"), morning_jitter_minutes=0)
    ctx.clock.advance(9.5 * 3600)
    await tick(p, ctx.clock)
    assert [call[2] for call in ctx.proactive.calls] == ["good_morning"]
    ctx.clock.advance(60)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 1
    await p.stop()
    ctx.clock.advance(25 * 3600)
    p = load_plugin()
    await p.start(ctx)
    assert ctx.proactive.queries[-1] == f"{1:032x}"
    ctx.clock.advance(3 * 3600)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 1
    assert (await status(p))["declines_today"] == 0


async def test_split_group_reply_uses_actual_focus_and_last_part_receipt(tmp_path):
    p, ctx = await setup(tmp_path)
    member = incoming(ctx.clock, kind="group", index=1, actor="member")
    owner = incoming(ctx.clock, kind="group", index=2)
    for event in (member, owner):
        await p.incoming(event)
    trace = "c" * 32
    await p.on_event(
        bus(
            ctx.clock,
            "message.received",
            event_id=member.event_id,
            trace_id=trace,
            scene="public_group",
            conversation_id="test_group",
            actor_id="test_member",
        )
    )
    await p.focus(
        draft(ctx.clock, trace, "test text", focus=owner.message.ref).model_copy(
            update={"conversation": owner.conversation}
        )
    )
    first = sample_intent().model_copy(update={"trace_id": trace})
    second = first.model_copy(update={"intent_id": "9" * 32})
    await p.before_send([first, second])
    await receipt(p, ctx.clock, trace, cid="test_group", intent_id=first.intent_id)
    ctx.clock.advance(3)
    await receipt(p, ctx.clock, trace, cid="test_group", intent_id=second.intent_id)
    assert p.schedule.state.last_chat == ctx.clock.now().timestamp()
    assert "test text" not in json.dumps(await ctx.storage.get("schedule"))


async def test_uses_topic_and_tools_and_counts_the_first_sent_at_once(tmp_path):
    p, ctx = await setup(tmp_path, topics=["long_silence"])
    clock = ctx.clock
    clock.advance(3 * 3600)
    await tick(p, clock)
    assert len(ctx.proactive.calls) == 1
    cid, topic, reason, tools = ctx.proactive.calls[0]
    assert (cid, reason, tools) == (TARGET, "long_silence", True)
    assert topic and "没回" not in topic and reason not in topic
    assert (await status(p))["attempts_today"] == 1
    assert (await status(p))["openings_today"] == 0
    trace = f"{1:032x}"
    await receipt(p, clock, trace)
    await receipt(p, clock, trace, intent_id="4" * 32)
    ctx.proactive.outcomes[trace] = ProactiveOutcome(status="sent", sent_at=clock.now())
    await tick(p, clock)
    assert ctx.proactive.queries == [trace]
    assert (await status(p))["openings_today"] == 1
    assert (await status(p))["unanswered"] == 1
    clock.advance(3 * 3600)
    await tick(p, clock)
    assert "不要追问" in ctx.proactive.calls[-1][1]
    assert "不要抱怨" in ctx.proactive.calls[-1][1]
    assert "fictional" not in str(await status(p))


@pytest.mark.parametrize(
    "kind,marks,actor,chat",
    [
        ("private", {}, "owner", True),
        ("group", {"mention": True}, "owner", True),
        ("group", {"reply_to_self": True}, "owner", True),
        ("group", {"name": "internal"}, "owner", True),
        ("group", {}, "owner", False),
        ("group", {"mention": True}, "member", False),
    ],
)
async def test_only_interaction_with_owner_resets_silence(tmp_path, kind, marks, actor, chat):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[2, 2])
    clock = ctx.clock
    clock.advance(90 * 60)
    await p.incoming(incoming(clock, actor=actor, kind=kind, **marks))
    clock.advance(30 * 60)
    await tick(p, clock)
    assert len(ctx.proactive.calls) == (0 if chat else 1)


async def test_sent_group_reply_to_owner_counts_without_a_mention(tmp_path):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[2, 2])
    clock = ctx.clock
    clock.advance(3600)
    event = incoming(clock, kind="group")
    await p.incoming(event)
    trace = "a" * 32
    await p.on_event(
        bus(
            clock,
            "message.received",
            event_id=event.event_id,
            trace_id=trace,
            scene="public_group",
            conversation_id="test_group",
            actor_id="test_owner",
        )
    )
    sent = sample_intent().model_copy(update={"trace_id": trace})
    await p.before_send([sent])
    await receipt(p, clock, trace, cid="test_group", intent_id="d" * 32)
    assert p.schedule.state.last_chat is None
    await receipt(p, clock, trace, cid="test_group", intent_id=sent.intent_id)
    clock.advance(3600)
    await tick(p, clock)
    assert not ctx.proactive.calls
    clock.advance(3600)
    await tick(p, clock)
    assert len(ctx.proactive.calls) == 1


@pytest.mark.parametrize("admission", ["busy", "paused"])
async def test_busy_and_paused_retry_at_next_tick_without_an_attempt(tmp_path, admission):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[2, 2])
    ctx.proactive.admission = admission
    ctx.clock.advance(2 * 3600)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 1
    assert (await status(p))["attempts_today"] == (await status(p))["openings_today"] == 0
    ctx.proactive.admission = "accepted"
    ctx.clock.advance(60)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 2
    assert (await status(p))["attempts_today"] == 1
    assert (await status(p))["openings_today"] == 0


async def test_invalid_target_stops_attempting(tmp_path):
    p, ctx = await setup(tmp_path, conversation="test_group", topics=["long_silence"])
    ctx.clock.advance(3 * 3600)
    await tick(p, ctx.clock)
    assert (await status(p))["configuration_error"]
    ctx.clock.advance(3 * 3600)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 1


async def test_restarting_the_plugin_does_not_repeat_missed_openings(tmp_path):
    p, ctx = await setup(tmp_path, silence_hours=[2, 2])
    ctx.clock.advance(2 * 3600)
    await tick(p, ctx.clock)
    trace = f"{1:032x}"
    ctx.proactive.outcomes[trace] = ProactiveOutcome(status="sent", sent_at=ctx.clock.now())
    await p.stop()
    ctx.clock.advance(26 * 3600)
    p = load_plugin()
    await p.start(ctx)
    assert (await status(p))["unanswered"] == 1
    assert ctx.proactive.queries == [trace]
    assert (await status(p))["openings_today"] == 0
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 1
    ctx.clock.advance(2 * 3600)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 2


async def test_reading_status_at_midnight_preserves_the_new_days_schedule(tmp_path):
    p, ctx = await setup(tmp_path, start=local("2026-06-15", "23:59"))
    ctx.clock.advance(60)
    await status(p)
    saved = await ctx.storage.get("schedule")
    assert saved["day"] == "2026-06-16"
    assert saved["morning_at"] == p.schedule.state.morning_at
    assert saved["festival_at"] == p.schedule.state.festival_at


@pytest.mark.parametrize("outcome", ["failed", "interrupted", "not_found"])
async def test_unsent_outcomes_wait_thirty_minutes_for_a_new_attempt(tmp_path, outcome):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[1, 1])
    clock = ctx.clock
    clock.advance(3600)
    await tick(p, clock)
    first = f"{1:032x}"
    ctx.proactive.outcomes[first] = ProactiveOutcome(status=outcome)
    clock.advance(60)
    await tick(p, clock)
    before = await status(p)
    assert before["openings_today"] == before["unanswered"] == before["declines_today"] == 0
    assert before["attempts_today"] == 1
    assert p.schedule.wait_reason(clock.now()) == "after_" + outcome
    clock.advance(1799)
    await tick(p, clock)
    assert len(ctx.proactive.calls) == 1
    clock.advance(1)
    await tick(p, clock)
    assert len(ctx.proactive.calls) == 2
    assert list(p.schedule.state.pending) == [f"{2:032x}"]
    assert ctx.proactive.queries == [first]


async def test_confirmed_decline_retries_only_its_topic_and_counts_once(tmp_path):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[1, 1])
    ctx.clock.advance(3600)
    await tick(p, ctx.clock)
    trace = f"{1:032x}"
    ctx.clock.advance(60)
    ctx.proactive.outcomes[trace] = ProactiveOutcome(status="declined")
    now = ctx.clock.now()
    await tick(p, ctx.clock)
    row = await status(p)
    assert row["declines_today"] == row["attempts_today"] == 1
    assert row["openings_today"] == row["unanswered"] == 0
    due = datetime.fromisoformat(row["next_opening_at"])
    assert now + timedelta(hours=1) <= due <= now + timedelta(hours=2)
    ctx.clock.set(due)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 2
    assert (await status(p))["declines_today"] == 1


async def test_morning_failure_retries_only_within_configured_wake_window(tmp_path):
    p, ctx = await setup(
        tmp_path,
        start=local("2026-06-15", "00:00"),
        seed=1,
        topics=["good_morning"],
        morning_jitter_minutes=60,
    )
    ctx.clock.set(local("2026-06-15", "09:56"))
    await tick(p, ctx.clock)
    trace = f"{1:032x}"
    assert len(ctx.proactive.calls) == 1
    ctx.proactive.outcomes[trace] = ProactiveOutcome(status="failed")
    ctx.clock.advance(60)
    await tick(p, ctx.clock)
    ctx.clock.advance(30 * 60)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 2


async def test_superseded_counts_as_chatting_preserving_actual_owner_time(tmp_path):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[1, 1])
    clock = ctx.clock
    clock.advance(3600)
    await tick(p, clock)
    trace = f"{1:032x}"
    clock.advance(15)
    await p.incoming(incoming(clock))
    owner_at = clock.now()
    ctx.proactive.outcomes[trace] = ProactiveOutcome(status="superseded")
    clock.advance(45)
    await tick(p, clock)
    row = await status(p)
    assert row["openings_today"] == row["unanswered"] == row["declines_today"] == 0
    assert row["attempts_today"] == 1
    assert datetime.fromisoformat(row["next_opening_at"]) == owner_at + timedelta(hours=1)


@pytest.mark.parametrize("outcome", ["unknown", "interrupted", "not_found"])
async def test_restart_reconciles_persisted_trace_and_never_replays_it(tmp_path, outcome):
    p, ctx = await setup(tmp_path, topics=["long_silence"], silence_hours=[1, 1])
    ctx.clock.advance(3600)
    await tick(p, ctx.clock)
    trace = f"{1:032x}"
    await p.stop()
    ctx.clock.advance(24 * 3600)
    if outcome == "not_found":
        del ctx.proactive.outcomes[trace]
    else:
        ctx.proactive.outcomes[trace] = ProactiveOutcome(status=outcome)
    p = load_plugin()
    await p.start(ctx)
    assert ctx.proactive.queries == [trace] and len(ctx.proactive.calls) == 1
    assert not p.schedule.state.pending
    row = await status(p)
    assert row["attempts_today"] == 0 and row["declines_today"] == 0
    assert row["openings_today"] == row["unanswered"] == (1 if outcome == "unknown" else 0)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 1
    ctx.clock.advance(3600)
    await tick(p, ctx.clock)
    assert len(ctx.proactive.calls) == 2 and list(p.schedule.state.pending) == [f"{2:032x}"]


class TimelineModel(FakeModel):
    scenario = "sent"

    async def generate(self, request, cancel):
        result = await super().generate(request, cancel)
        if self.scenario == "failed":
            return result.model_copy(update={"finish": "error", "text": None})
        if self.scenario == "declined":
            return result.model_copy(update={"text": ""})
        return result


class TimelineChannel(FakeChannel):
    uncertain = False

    async def send(self, intent, cancel):
        result = await super().send(intent, cancel)
        if self.uncertain:
            return result.model_copy(update={"status": "unknown", "platform_message_id": None})
        return result


async def model_outcome(clock, model, channel, trace, topic, scenario):
    model.scenario = scenario
    req = sample_request(clock).model_copy(
        update={
            "trace_id": trace,
            "messages": [ChatMessage(role="system", parts=[TextPart(text=topic)])],
        }
    )
    turn = await model.generate(req, CancelToken())
    if turn.finish == "error":
        return ProactiveOutcome(status="failed")
    if not turn.text:
        return ProactiveOutcome(status="declined")
    if scenario == "superseded":
        return ProactiveOutcome(status="superseded")
    intent = sample_intent().model_copy(
        update={
            "kind": "proactive",
            "conversation_id": TARGET,
            "intent_id": trace,
            "trace_id": trace,
            "parts": [TextPart(text=turn.text)],
        }
    )
    channel.uncertain = scenario == "unknown"
    received = await channel.send(intent, CancelToken())
    return ProactiveOutcome(
        status=received.status, sent_at=received.at if received.status == "sent" else None
    )


async def test_three_day_timeline(tmp_path):
    """按清单启动插件。假宿主只通过正式 submit/result 协议返回准入与交付结果。"""
    start = local("2026-09-18", "00:00")
    p, ctx = await setup(tmp_path, start=start)
    clock = ctx.clock
    model, channel = TimelineModel(ctx), TimelineChannel(ctx)
    script = iter(
        ["sent", "declined", "superseded", "failed", "interrupted", "not_found", "unknown"]
    )
    owner = {
        (18, "09:55"): "private",
        (18, "12:25"): "group",
        (18, "16:05"): "private",
        (19, "10:55"): "private",
        (19, "15:07"): "group",
        (19, "23:40"): "private",
    }
    assert [p.calendar.day((start + timedelta(days=n)).date())[0] for n in range(3)] == [
        True,
        False,
        True,
    ]
    rows, waits = [], []
    accepted = 0
    for minute in range(3 * 24 * 60):
        now = clock.now().astimezone(TZ)
        kind = owner.get((now.day, now.strftime("%H:%M")))
        if kind:
            await p.incoming(incoming(clock, index=minute, kind=kind, mention=kind == "group"))
        calls = ctx.proactive.calls
        ctx.proactive.admission = {0: "busy", 1: "paused"}.get(len(calls), "accepted")
        await tick(p, clock)
        if len(calls) == len(rows):
            wait = p.schedule.wait_reason(now)
            if wait not in waits:
                waits.append(wait)
        else:
            cid, topic, reason, tools = calls[-1]
            assert cid == TARGET and tools
            trace = f"{len(calls):032x}"
            outcome = ctx.proactive.admission
            if outcome == "accepted":
                accepted += 1
                scenario = next(script, "sent")
                if scenario == "interrupted":
                    await p.stop()
                    ctx.proactive.outcomes[trace] = ProactiveOutcome(status="interrupted")
                    p = load_plugin()
                    await p.start(ctx)
                    assert ctx.proactive.queries[-1] == trace and len(calls) == len(rows) + 1
                    outcome = "interrupted"
                elif scenario == "not_found":
                    del ctx.proactive.outcomes[trace]
                    outcome = "not_found"
                else:
                    if scenario == "superseded":
                        await p.incoming(incoming(clock, index=minute))
                    result = await model_outcome(clock, model, channel, trace, topic, scenario)
                    ctx.proactive.outcomes[trace] = result
                    outcome = result.status
            rows.append(
                (
                    now.strftime("%m-%d %H:%M"),
                    reason,
                    outcome,
                    ",".join(waits) or "next_tick",
                )
            )
            waits.clear()
        if now.strftime("%H:%M") == "23:59":
            date_rows = [row[2] for row in rows if row[0].startswith(now.strftime("%m-%d"))]
            daily = await status(p)
            assert daily["attempts_today"] == sum(v not in {"busy", "paused"} for v in date_rows)
            assert daily["openings_today"] == sum(v in {"sent", "unknown"} for v in date_rows)
            assert daily["declines_today"] == date_rows.count("declined")
        clock.advance(60)
    outcomes = [row[2] for row in rows]
    assert set(outcomes) == {
        "busy",
        "paused",
        "sent",
        "unknown",
        "declined",
        "superseded",
        "failed",
        "interrupted",
        "not_found",
    }
    assert accepted == len(rows) - 2
    assert len(model.requests) == accepted - 2
    assert len(channel.sent) == outcomes.count("sent") + outcomes.count("unknown")
    assert len(set(ctx.proactive.queries)) == accepted
    assert len({row[0][:5] for row in rows}) == 3
    assert all(row[3] for row in rows)
    assert (
        sum(reason == "good_morning" and outcome == "sent" for _, reason, outcome, _ in rows) == 3
    )
    assert (await status(p))["unanswered"] >= 4
    assert any("unanswered_240" in row[3] for row in rows)
    for row in rows:
        print(" | ".join(row))
