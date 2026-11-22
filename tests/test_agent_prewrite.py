"""B7c §10.12. Exercise intake, the real timing plugin and the delivery ledger offline."""

import asyncio
import json

import pytest

from clytie.agent.policy import text_parts
from clytie.sdk.behavior import Defer, merge_attention
from clytie.sdk.types import ModelTurn

from .host_helpers import enable
from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO
from .test_agent_judgment import ANSWER, group
from .test_agent_model_usage import usages


async def timing_agent(tmp_path, *turns):
    agent, channel, model = await make_agent(tmp_path, *turns)
    await enable(agent.host, "chat-timing")
    return agent, channel, model


def draft(agent, item):
    key = (item.conversation.conversation_id, item.message.sender.actor_id)
    return agent.batches[key]["draft"]


def discarded(agent):
    return [r for r in agent.ledger.rows("audit") if r["kind"] == "prepare_discarded"]


def prompt(request):
    return "\n".join(text_parts(m.parts) for m in request.messages)


async def test_mention_prepares_once_before_quiet_delivers_after_and_times_model(tmp_path):
    started = asyncio.Event()

    async def respond(req, token):
        started.set()
        await agent.clock.sleep(1)
        return ModelTurn(
            text="fictional answer",
            finish="stop",
            usage={
                "input_tokens": 17,
                "output_tokens": 3,
                "cached_input_tokens": 5,
            },
        )

    agent, channel, model = await timing_agent(tmp_path, respond)
    item = group(agent, "hello", mention=True)
    await agent.receive(item)
    await started.wait()
    await agent.clock.advance_and_drain(1)
    trace = draft(agent, item).trace
    await draft(agent, item).task
    assert len(model.requests) == 1 and not channel.sent and not agent.ledger.rows("record")
    await agent.clock.advance_and_drain(2)
    await agent.drain()
    assert len(channel.sent) == 1 and len(model.requests) == 1 and not discarded(agent)
    assert agent.ledger.get("record", trace)["model_calls"] == 1
    (row,) = usages(agent)
    assert (
        row["trace_id"],
        row["purpose"],
        row["prepared"],
        row["duration_ms"],
        row["input_tokens"],
        row["output_tokens"],
        row["cached_input_tokens"],
    ) == (trace, "reply", True, 1000, 17, 3, 5)
    await finish(agent)


@pytest.mark.parametrize("count", [2, 4])
async def test_same_speaker_replaces_draft_at_most_three_times(tmp_path, count):
    agent, channel, model = await timing_agent(tmp_path)
    traces = []
    for i in range(count):
        item = group(agent, f"fictional item {i}", mention=i == 0)
        await agent.receive(item)
        if i < 3:
            current = draft(agent, item)
            traces.append(current.trace)
            await current.task
        assert len(model.requests) == min(3, i + 1)
        assert not channel.sent
    await agent.clock.advance_and_drain(3)
    await agent.drain()
    assert len(channel.sent) == 1 and len(model.requests) == count
    assert all(f"fictional item {i}" in prompt(model.requests[-1]) for i in range(count))
    drops = discarded(agent)
    assert [d["reason"] for d in drops] == ["batch_changed"] * (count - 1)
    assert [d["trace_id"] for d in drops] == traces[: count - 1]
    for row in drops:
        assert set(row) == {"kind", "at", "trace_id", "conversation_id", "reason"}
        assert agent.ledger.get("record", row["trace_id"]) is None
    (record,) = agent.ledger.rows("record")
    assert record["model_calls"] == 1
    rows = usages(agent)
    assert len(rows) == count
    assert rows[-1]["prepared"] == (count == 2)
    assert rows[-1]["trace_id"] == record["trace_id"]
    await finish(agent)


async def test_other_speaker_does_not_restart_and_second_batch_rewrites_stale(tmp_path):
    agent, channel, model = await timing_agent(
        tmp_path,
        {"text": "fictional first answer", "finish": "stop"},
        {"text": "fictional stale answer", "finish": "stop"},
        {"text": "fictional fresh answer", "finish": "stop"},
    )
    first = group(agent, "first speaker", mention=True)
    second = group(agent, "second speaker", mention=True, actor="peer")
    await agent.receive(first)
    first_draft = draft(agent, first)
    await first_draft.task
    await agent.clock.advance_and_drain(1)
    await agent.receive(second)
    await draft(agent, second).task
    assert draft(agent, first) is first_draft and not discarded(agent)
    await agent.clock.advance_and_drain(2)
    await agent.drain()
    assert len(channel.sent) == 1
    await agent.clock.advance_and_drain(1)
    await agent.drain()
    assert [text_parts(i.parts) for i in channel.sent] == [
        "fictional first answer",
        "fictional fresh answer",
    ]
    (drop,) = discarded(agent)
    assert drop["reason"] == "stale" and agent.ledger.get("record", drop["trace_id"]) is None
    assert [r["prepared"] for r in usages(agent)] == [True, True, False]
    assert "fictional first answer" in prompt(model.requests[-1])
    await finish(agent)


async def test_message_after_respond_is_next_batch_even_while_draft_finishes(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(req, cancel):
        started.set()
        await release.wait()
        return ModelTurn(text="fictional first", finish="stop", usage={})

    agent, channel, model = await timing_agent(tmp_path, slow)
    await agent.receive(event(agent, "before"))
    await started.wait()
    await agent.clock.advance_and_drain(3)
    assert not agent.batches and not channel.sent
    await agent.receive(event(agent, "after"))
    assert len(model.requests) == 1 and not discarded(agent)
    release.set()
    await agent.drain()
    await agent.clock.advance_and_drain(3)
    await agent.drain()
    assert len(channel.sent) == 2 and len(agent.ledger.rows("record")) == 2
    assert "after" not in prompt(model.requests[0]) and "after" in prompt(model.requests[1])
    assert [r["prepared"] for r in usages(agent)] == [True, False]
    await finish(agent)


@pytest.mark.parametrize(
    "judgment,adopted", [(json.dumps(dict(ANSWER, calling_me=False)), False), ("bad", True)]
)
async def test_named_judge_runs_during_draft_and_decides_tool_adoption(tmp_path, judgment, adopted):
    started, release = asyncio.Event(), asyncio.Event()
    judge_started = asyncio.Event()

    async def prepare(req, cancel):
        started.set()
        await release.wait()
        return ModelTurn(usage={}, **ECHO)

    async def judge(req, cancel):
        judge_started.set()
        await release.wait()
        return ModelTurn(text=judgment, finish="stop", usage={})

    agent, channel, model = await timing_agent(tmp_path, prepare, judge)
    await agent.receive(group(agent, "companion hello"))
    await started.wait()
    await agent.clock.advance_and_drain(3)
    await judge_started.wait()
    assert not channel.sent and not agent.ledger.rows("invocation")
    release.set()
    # Await the decision and its record submission as well as the delivery.
    await asyncio.gather(*(b["timer"] for b in list(agent.batches.values())))
    await agent.drain()
    assert len(channel.sent) == int(adopted)
    assert len(agent.ledger.rows("invocation")) == int(adopted)
    assert len(agent.ledger.rows("record")) == int(adopted)
    if adopted:
        assert agent.ledger.rows("record")[0]["model_calls"] == 2
        assert not discarded(agent)
    else:
        assert [r["reason"] for r in discarded(agent)] == ["skipped"]
    assert len([r for r in usages(agent) if r.get("prepared")]) == 1
    await finish(agent)


@pytest.mark.parametrize("last", ["error", "stopped", "safety_paused"])
async def test_failed_prepare_falls_back_and_stop_or_pause_discards(tmp_path, last):
    agent, channel, model = await timing_agent(
        tmp_path, {"text": "fictional draft", "finish": "error" if last == "error" else "stop"}
    )
    item = group(agent, "first", mention=True)
    await agent.receive(item)
    current = draft(agent, item)
    await asyncio.gather(current.task, return_exceptions=True)
    if last == "stopped":
        agent.shutting_down = True
        await agent.prewrites.stop()
    elif last == "safety_paused":
        agent.safety_belt.paused = lambda cid: True
    await agent.clock.advance_and_drain(3)
    await agent.drain()
    assert [r["reason"] for r in discarded(agent)] == [last]
    assert agent.ledger.get("record", current.trace) is None
    assert len(channel.sent) == int(last == "error")
    assert len(model.requests) == 1 + int(last == "error")
    await finish(agent)


@pytest.mark.parametrize("reason", ["cancel", "pending", "running", "paused", "mixed", "natural"])
async def test_prepare_ineligible(tmp_path, reason):
    agent, channel, model = await timing_agent(tmp_path)
    item = group(agent, "停" if reason == "cancel" else "fictional", mention=reason != "natural")
    if reason in {"pending", "running"}:
        with agent.ledger.transaction():
            agent.ledger.put(
                "record",
                "fake",
                {"kind": "chat", "conversation_id": "test_group", "exec_status": reason},
            )
    if reason == "paused":
        agent.safety_belt.paused = lambda cid: True
    if reason == "mixed":
        original = agent.host.behavior

        async def behavior(cid, phase, value, **kw):
            opinion = await original(cid, phase, value, **kw)
            if phase == "attention" and isinstance(opinion, Defer):
                return merge_attention(
                    [(1, "a", opinion), (2, "b", Defer(seconds=1))],
                    private=False,
                    mentioned=True,
                    owner_private=False,
                )
            return opinion

        agent.host.behavior = behavior
    await agent.receive(item)
    await agent.clock.drain()
    assert not model.requests and not agent.prewrites.active
    agent.shutting_down = True
    await agent.clock.advance_and_drain(6)
    await finish(agent)


async def test_discarded_context_consumes_neither_memory_nor_partial_notice(tmp_path):
    from clytie.memory.fade import state

    agent, channel, model = await make_agent(tmp_path)
    # Seed a real delivered reply, then retain a split with one failed sibling.
    await agent.receive(group(agent, "first", mention=True))
    await agent.drain()
    (old,) = agent.ledger.rows("intent")
    with agent.ledger.transaction():
        for index, status in enumerate(["sent", "failed"]):
            key = old["intent_id"] if index == 0 else "fictional-failed-part"
            agent.ledger.put(
                "intent",
                key,
                dict(
                    old,
                    intent_id=key,
                    status=status,
                    split={"group_id": "fictional-split", "index": index, "count": 2},
                ),
            )
    store = agent.memory.store
    memory = store.write(
        dict(kind="jargon", content="虚构纸船活动", visibility="relationship"),
        scene="public_group",
        conversation_id="test_group",
    )
    await enable(agent.host, "chat-timing")
    for i in range(3):
        item = group(agent, f"纸船 {i}", mention=i == 0)
        await agent.receive(item)
        await draft(agent, item).task
        assert "虚构纸船活动" in prompt(model.requests[-1])
        assert "上次拆分消息" in prompt(model.requests[-1])
        assert state(store, memory)[1] == 30
        assert not any(r["kind"] == "partial_group_context" for r in agent.ledger.rows("audit"))
    await agent.clock.advance_and_drain(3)
    await agent.drain()
    assert len(discarded(agent)) == 2 and len(channel.sent) == 2
    assert state(store, memory)[1] == 45
    notices = [r for r in agent.ledger.rows("audit") if r["kind"] == "partial_group_context"]
    assert [r["group_id"] for r in notices] == ["fictional-split"]
    await finish(agent)


async def test_cancelled_inflight_draft_releases_and_counts_only_returned_usage(tmp_path):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def uncooperative(req, token):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert token.cancelled
            cancelled.set()
            return ModelTurn(
                text="discarded-marker",
                finish="stop",
                usage={"input_tokens": 7, "output_tokens": 2},
            )

    agent, channel, model = await timing_agent(tmp_path, uncooperative)
    await agent.receive(event(agent, "first"))
    await entered.wait()
    second = event(agent, "second")
    await agent.receive(second)
    await cancelled.wait()
    await draft(agent, second).task
    await agent.clock.advance_and_drain(3)
    await agent.drain()
    assert len(channel.sent) == 1 and len(model.requests) == 2
    rows = usages(agent)
    assert len(rows) == 2 and sum(r["input_tokens"] or 0 for r in rows) == 7
    assert sum(r["output_tokens"] or 0 for r in rows) == 2
    assert all(r["prepared"] for r in rows)
    (late,) = [r for r in rows if r["timing"] == "late"]
    assert late["trace_id"] == discarded(agent)[0]["trace_id"]
    assert "discarded-marker" not in str(agent.ledger.rows("audit"))
    await finish(agent)


async def test_prepare_deadline_includes_context_but_usage_times_only_model(tmp_path, monkeypatch):
    started = asyncio.Event()
    agent, channel, model = await timing_agent(tmp_path)
    agent.config["agent"] = {"model_call_seconds": 2}
    build = agent.context.build

    async def slow_context(*args, **kwargs):
        if kwargs.get("use") is not None:
            started.set()
            await agent.clock.sleep(20)
        return await build(*args, **kwargs)

    monkeypatch.setattr(agent.context, "build", slow_context)
    await agent.receive(event(agent))
    await started.wait()
    await agent.clock.advance_and_drain(3)
    await agent.drain()
    assert [d["reason"] for d in discarded(agent)] == ["error"]
    assert len(model.requests) == len(channel.sent) == 1
    (usage,) = usages(agent)
    assert (usage["duration_ms"], usage["prepared"], usage["cached_input_tokens"]) == (
        0,
        False,
        None,
    )
    await finish(agent)
