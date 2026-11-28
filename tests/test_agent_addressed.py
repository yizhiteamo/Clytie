"""群聊行为 §10.1–5：从真实入站、收据和模型请求核对本体判断。"""

import asyncio
from types import SimpleNamespace

import pytest

from clytie.sdk.behavior import Defer, Judge, JudgeFacts, Respond, Skip, merge_attention
from clytie.sdk.types import Addressed, MentionPart, ModelTurn

from .test_agent import event, finish, make_agent


def group(agent, text="fictional request", **kw):
    return event(agent, text, scene="public_group", role="member", **kw)


def observe(agent, monkeypatch, hook):
    behavior = agent.host.behavior

    async def inspect(cid, phase, value, **kwargs):
        result = hook(phase, value)
        if result is not None:
            return result
        return await behavior(cid, phase, value, **kwargs)

    monkeypatch.setattr(agent.host, "behavior", inspect)


async def test_addressed_from_text_mention_and_confirmed_receipt(tmp_path, monkeypatch):
    agent, channel, model = await make_agent(tmp_path)
    seen = []

    def spy(phase, value):
        if phase == "message_received":
            seen.append(value.addressed)

    observe(agent, monkeypatch, spy)
    initial = group(agent, "unrelated").model_copy(update={"addressed": Addressed(mention=True)})
    await agent.receive(initial)
    assert seen[-1] == Addressed() and not model.requests
    for text, kind in (("Fictional_Internal", "internal"), ("CoMpAnIoN", "external")):
        await agent.receive(group(agent, text))
        assert seen[-1] == Addressed(name=kind)
    everyone = group(agent)
    everyone = everyone.model_copy(
        update={
            "message": everyone.message.model_copy(
                update={"parts": [MentionPart(actor_id="all", is_self=False)]}
            )
        }
    )
    await agent.receive(everyone)
    assert seen[-1] == Addressed() and not model.requests
    called = group(agent, "Fictional_Internal", mention=True)
    await agent.receive(called)
    await agent.drain()
    assert seen[-1] == Addressed(mention=True, name="internal")
    note = model.requests[-1].messages[-1]
    assert note.role == "system"
    assert (
        "内部名字" in note.parts[0].text
        and "fictional_internal" not in note.parts[0].text.casefold()
    )
    ref = called.message.ref.model_copy(
        update={"platform_message_id": "out-" + channel.sent[-1].intent_id}
    )
    for other in (called.message.ref, ref.model_copy(update={"conversation_id": "elsewhere"})):
        await agent.receive(group(agent, reply_to=other))
        assert seen[-1] == Addressed()
    await agent.receive(group(agent, reply_to=ref))
    await agent.drain()
    assert seen[-1] == Addressed(reply_to_self=True) and len(channel.sent) == 2
    await agent.receive(event(agent))
    await finish(agent)
    assert len(channel.sent) == 3


def facts():
    return JudgeFacts(
        participants=1,
        share=0.0,
        rounds_with_same_person=0,
        counterpart_fast=False,
        fatigue="none",
        ignored_recently=False,
        owner_spoke=False,
        name_hit=False,
    )


def test_judge_priority_typed_facts_and_response_options():
    decision = Judge(facts=facts())
    response = Respond(recheck_after=15)

    def merge(*items):
        return merge_attention(
            [(n, "fixture", item) for n, item in enumerate(items)],
            private=False,
            mentioned=False,
            owner_private=False,
        )

    assert merge(response, decision) == decision
    assert merge(decision, Defer(seconds=1)).type == "defer"
    assert merge(decision, Defer(seconds=1), Skip()).type == "skip"
    assert merge(response) == response
    from pydantic import ValidationError

    for change in (
        {"participants": "1"},
        {"owner_spoke": "fictional text"},
        {"follow_up": "unrestricted text"},
        {"notes": "extra"},
    ):
        with pytest.raises(ValidationError):
            JudgeFacts.model_validate(facts().model_dump() | change)


async def test_one_pending_judge_resumes_only_origin_and_owner_private_bypasses(
    tmp_path, monkeypatch
):
    agent, channel, model = await make_agent(
        tmp_path, {"finish": "stop", "text": '{"join":true,"new_topic":false,"focus":1}'}
    )
    regs, invoke = agent.host.registrations, agent.host.invoke
    judges = [
        SimpleNamespace(priority=i, plugin_id=pid, key="attention")
        for i, pid in enumerate(("fictional-judge", "fictional-old"))
    ]
    calls = []

    def registrations(cid, point):
        return judges if point == "behavior.attention" else regs(cid, point)

    async def perform(cid, pid, point, key, *args, **kw):
        if point != "behavior.attention":
            return await invoke(cid, pid, point, key, *args, **kw)
        (data,) = args
        calls.append((pid, len(data)))
        if pid == "fictional-old":
            assert len(data) == 2
            return None
        if len(data) == 2:
            return Judge(facts=facts())
        assert data[2].status == "ok" and data[2].focus == data[0].message.ref
        return Respond(focus=data[2].focus)

    monkeypatch.setattr(agent.host, "registrations", registrations)
    monkeypatch.setattr(agent.host, "invoke", perform)
    assert await agent.receive(group(agent)) is None
    assert len(agent.batches) == 1
    for _ in range(64):
        if not agent.batches:
            break
        await agent.clock.drain()
    assert not agent.batches
    await agent.drain()
    assert calls == [("fictional-judge", 2), ("fictional-old", 2), ("fictional-judge", 3)]
    assert len(channel.sent) == 1
    await agent.receive(event(agent))
    await agent.drain()
    assert len(channel.sent) == 2
    assert (
        len([row for row in agent.ledger.rows("audit") if row.get("purpose") == "attention"]) == 1
    )
    await finish(agent)


@pytest.mark.parametrize(
    ("count", "direct", "checks"), [(14, False, 0), (15, False, 1), (15, True, 0)]
)
async def test_recheck_threshold_and_direct_exemption(tmp_path, monkeypatch, count, direct, checks):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(req, cancel):
        entered.set()
        await release.wait()
        return ModelTurn(finish="stop", text="fictional draft", usage={})

    agent, channel, model = await make_agent(
        tmp_path, generate, {"finish": "stop", "text": '{"send":false}'}
    )
    first = group(agent, mention=direct)
    drafts = []

    def behavior(phase, value):
        if phase == "attention":
            if value[0].event_id == first.event_id:
                return Respond(focus=first.message.ref, recheck_after=15)
            return Skip()
        if phase == "after_generation":
            drafts.append(value)

    observe(agent, monkeypatch, behavior)
    trace = await agent.receive(first)
    await entered.wait()
    for _ in range(count):
        await agent.receive(group(agent, actor="another_member"))
    release.set()
    await agent.drain()
    assert len(model.requests) == 1 + checks
    assert drafts[0].focus == first.message.ref and drafts[0].interleaved == count
    assert len(channel.sent) == 1 - checks
    row = agent.ledger.get("record", trace)
    assert row["exec_status"] == "ok"
    if checks:
        assert row["delivery_status"] == "none"
        assert not agent.ledger.rows("intent")
        assert any(
            x["kind"] == "reply_withdrawn" and x["trace_id"] == trace
            for x in agent.ledger.rows("audit")
        )
    await finish(agent)


async def test_natural_reply_and_recheck_use_owned_ledger(tmp_path, monkeypatch):
    from clytie.backup.coordination import run_sync

    from .test_storage_agent_lifecycle import inbound, ready_runtime

    async with ready_runtime(tmp_path) as runtime:
        agent = runtime.agent
        first = inbound(runtime, scene="public_group", role="member")
        generation = asyncio.Event()
        proceed = asyncio.Event()

        def decide(phase, data):
            if phase == "attention":
                if data[0].event_id == first.event_id:
                    return Respond(focus=first.message.ref, recheck_after=1)
                return Skip()

        observe(agent, monkeypatch, decide)
        replies = []
        original = agent.model_call

        async def scripted(method, *args, **kw):
            if method != "generate":
                return await original(method, *args, **kw)
            replies.append(args[0])
            if len(replies) == 1:
                generation.set()
                await proceed.wait()
                return ModelTurn(finish="stop", text="fictional draft", usage={})
            return ModelTurn(finish="stop", text='{"send":false}', usage={})

        monkeypatch.setattr(agent, "model_call", scripted)
        trace = await agent.receive(first)
        await generation.wait()
        await agent.receive(
            inbound(runtime, scene="public_group", role="member", actor="another_member")
        )
        proceed.set()
        await agent.drain()
        record = await run_sync(agent.ledger, agent.ledger.get, "record", trace)
        assert (record["exec_status"], record["delivery_status"]) == ("ok", "none")
        assert len(replies) == 2
    assert not runtime.storage_owner._thread.is_alive()
