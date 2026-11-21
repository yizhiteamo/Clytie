"""A3.2 离线验收：真实宿主装配、脚本模型、假平台和临时目录。"""

import asyncio
from collections import deque
from uuid import uuid4

import pytest

from clytie.agent import Agent
from clytie.agent.policy import fast_cancel, text_parts
from clytie.kernel.ledger import Ledger
from clytie.kernel.service import Kernel
from clytie.sdk import CancelToken
from clytie.sdk.behavior import Defer, Respond
from clytie.sdk.testing import MockClock
from clytie.sdk.types import (
    Actor,
    ChannelFeatures,
    Conversation,
    EventEnvelope,
    ModelFeatures,
    ModelTurn,
    Receipt,
    TextPart,
)

from .host_helpers import enable, host


class ScriptModel:
    def __init__(self, *turns, tools=True):
        self.turns, self.requests = deque(turns), []
        self.tools = tools

    def features(self):
        return ModelFeatures(
            supports_tools=self.tools, supports_images=False, context_chars=20_000, agent_type=False
        )

    async def quota(self):
        return "unknown"

    async def generate(self, req, cancel):
        self.requests.append(req)
        value = (
            self.turns.popleft() if self.turns else {"text": "fictional reply", "finish": "stop"}
        )
        if callable(value):
            return await value(req, cancel)
        return ModelTurn(usage={}, **value)


class Channel:
    def __init__(self, clock):
        self.clock, self.sent, self.reacts = clock, [], []

    def features(self):
        return ChannelFeatures(
            max_text_chars=2000,
            supports_react=True,
            supports_quote=True,
            supports_card=True,
            media_limits={"image": {"max_bytes": 1024, "formats": ["image/png"]}},
        )

    async def send(self, intent, cancel):
        self.sent.append(intent)
        return Receipt(
            intent_id=intent.intent_id,
            status="sent",
            at=self.clock.now(),
            platform_message_id="out-" + intent.intent_id,
        )

    async def react(self, intent, cancel):
        self.reacts.append(intent)
        return Receipt(intent_id=intent.intent_id, status="sent", at=self.clock.now())


async def make_agent(tmp_path, *turns, changes=None, conversations=None, people=None):
    clock = MockClock()
    h = host(tmp_path, clock=clock)
    await enable(h, "echo-tool")
    model = ScriptModel(*turns)
    config = {
        "names": {"internal": "fictional_internal", "external": {"public_group": "companion"}},
        "model": {"plugin": "fake-model"},
        "channels": [{"plugin": "fake-channel", "account": "fake"}],
        "plugins": {"echo-tool": {"enabled": True}},
        "permissions": {"owner": ["*"], "member": ["echo"]},
        "persona": {
            "files": {"base": "base.md"},
            "scenes": {s: ["base"] for s in ["owner_private", "public_group", "stranger_private"]},
        },
        "task_reactions": {"min_interval_seconds": 0},
    }
    if changes:
        for key, value in changes.items():
            config[key] = value
    instance = {
        "people": {
            "owner": {
                "role": "owner",
                "accounts": [{"channel": "fake-channel", "account": "fictional-owner_private"}],
            },
            "member": {"role": "member", "accounts": []},
        },
        "conversations": {
            "test_group": {"kind": "group", "scene": "public_group", "channel": "fake-channel"},
            "owner_private": {
                "kind": "private",
                "scene": "owner_private",
                "channel": "fake-channel",
            },
        },
    }
    if people is not None:
        instance["people"] = people
    instance["conversations"].update(conversations or {})
    for cid, conversation in instance["conversations"].items():
        conversation["platform_id"] = "fictional-" + cid
    persona = tmp_path / "companions/companion_a/persona"
    persona.mkdir(parents=True, exist_ok=True)
    (persona / "base.md").write_text(
        "Fictional persona <!-- private -->fictional private detail<!-- /private -->",
        encoding="utf-8",
    )
    ledger = Ledger(tmp_path / "companions/companion_a/ledger/ledger.sqlite3", "companion_a", clock)
    channel = Channel(clock)
    kernel = Kernel(ledger, clock, {"fake": channel}, config=config)
    agent = Agent(kernel, h, config, instance, persona, model=model)
    await agent.start()
    return agent, channel, model


def event(
    agent,
    text="fictional request",
    *,
    role="owner",
    actor=None,
    scene="owner_private",
    cid=None,
    mention=False,
    reply_to=None,
):
    cid = cid or ("test_group" if scene == "public_group" else scene)
    ref = {"channel_id": "fake", "conversation_id": cid, "platform_message_id": uuid4().hex}
    return EventEnvelope(
        event_id="fake:" + ref["platform_message_id"],
        type="conversation.message",
        companion_id=agent.cid,
        channel_id="fake",
        conversation=Conversation(
            conversation_id=cid,
            channel_id="fake",
            kind="group" if scene in {"public_group", "family_group"} else "private",
            scene=scene,
        ),
        received_at=agent.clock.now(),
        message={
            "ref": ref,
            "sender": {"actor_id": actor or role, "role": role, "channel_id": "fake"},
            "parts": [
                TextPart(text=text),
                *(
                    [{"type": "mention", "actor_id": "companion_a", "is_self": True}]
                    if mention
                    else []
                ),
            ],
            "sent_at": agent.clock.now(),
            "reply_to": reply_to,
        },
    )


async def finish(agent):
    await agent.drain()
    await agent.host.close()
    agent.ledger.close()


async def test_full_offline_path_real_echo_and_task_query(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path,
        {
            "finish": "tool_calls",
            "tool_calls": [
                {"call_id": "call_a", "tool": "echo", "arguments": {"text": "fictional output"}}
            ],
        },
        {"finish": "stop", "text": "fictional done"},
    )
    trace = await agent.receive(event(agent))
    await agent.drain()
    row = agent.ledger.get("record", trace)
    assert row["kind"] == "task" and row["exec_status"] == "ok", agent.log.entries
    assert row["delivery_status"] == "sent"
    assert row["model_calls"] == 2 and row["tool_rounds"] == 1
    assert row["task_id"] in text_parts(channel.sent[-1].parts)
    assert any(
        m.role == "tool" and "fictional output" in text_parts(m.parts)
        for m in model.requests[-1].messages
    )
    assert all(
        any("能力状态" in text_parts(m.parts) for m in request.messages)
        for request in model.requests
    )
    await finish(agent)


@pytest.mark.parametrize(
    "text", ["please echo this", "could you repeat my words", "say this once again"]
)
async def test_l1_variants_all_reach_model(tmp_path, text):
    agent, _, model = await make_agent(tmp_path)
    await agent.receive(event(agent, text))
    await agent.drain()
    assert text in text_parts(model.requests[0].messages[-1].parts)
    for word in ["停车场", "别停", "停止绘画吧"]:
        assert fast_cancel(event(agent, word).message, ["停", "停止"]) is None
    assert fast_cancel(event(agent, "！停 ABCDEF12！").message, ["停"]) == {"task_id": "abcdef12"}
    await finish(agent)


@pytest.mark.parametrize("scene,expected", [("owner_private", True), ("public_group", False)])
async def test_persona_scene_floor_and_history(tmp_path, scene, expected):
    agent, _, model = await make_agent(tmp_path)
    await agent.receive(event(agent, scene=scene, mention=True))
    await agent.drain()
    text = text_parts(model.requests[0].messages[0].parts)
    assert ("fictional private detail" in text) == expected
    assert text.endswith("任何时候都不声称自己是真人；有人认真需要确认时如实说明。")
    await finish(agent)


@pytest.mark.parametrize(
    "scene,rewritten,expected",
    [
        ("public_group", "fictional_internal", 0),
        ("public_group", "safe", 1),
        ("stranger_private", "fictional_internal", 1),
        ("owner_private", "unused", 1),
    ],
)
async def test_outbound_rewrite_once_and_fallback(tmp_path, scene, rewritten, expected):
    agent, channel, model = await make_agent(
        tmp_path,
        {"finish": "stop", "text": "fictional_internal"},
        {"finish": "stop", "text": rewritten},
        conversations={
            "stranger_private": {
                "kind": "private",
                "channel": "fake-channel",
                "scene": "stranger_private",
            },
        },
    )
    trace = await agent.receive(event(agent, scene=scene, mention=True))
    await agent.drain()
    assert len(channel.sent) == expected
    assert len(model.requests) == (1 if scene == "owner_private" else 2)
    if scene == "public_group" and rewritten == "fictional_internal":
        assert agent.ledger.get("record", trace)["error"]["code"] == "outbound_blocked"
    await finish(agent)


async def test_style_rewrite_still_hit_allowed_and_reformats(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path, *[{"finish": "stop", "text": "tool_call"}] * 2
    )
    phases = []
    original = agent.host.behavior

    async def behavior(cid, phase, input, **kw):
        phases.append(phase)
        return await original(cid, phase, input, **kw)

    agent.host.behavior = behavior
    await agent.receive(event(agent))
    await agent.drain()
    assert len(model.requests) == 2 and len(channel.sent) == 1
    assert phases.count("after_generation") == 2
    assert any(
        level == "info" and message == "outbound.style" for level, message, _ in agent.log.entries
    )
    await finish(agent)


async def test_boundary_first_category_pause_filter_context_and_release(tmp_path):
    agent, _, model = await make_agent(
        tmp_path,
        changes={"boundary": {"refusal_threshold": 1, "input_filter": {"words": ["dropme"]}}},
    )
    actor = Actor(actor_id="member", role="member", channel_id="fake")
    trace = uuid4().hex
    await agent.boundary.report(trace, actor, "romance", CancelToken())
    await agent.boundary.report(trace, actor, "owner_privacy", CancelToken())
    assert list(agent.boundary.state["actors"]["member"]["counts"]) == ["romance"]
    ignored = event(agent, "paused words", role="member", scene="public_group", mention=True)
    await agent.receive(ignored)
    await agent.receive(event(agent, "dropme", role="member", scene="public_group"))
    assert len(agent.ledger.rows("inbound")) == 1
    assert not model.requests
    await agent.receive(
        event(agent, "normal", scene="public_group", mention=True, reply_to=ignored.message.ref)
    )
    await agent.drain()
    prompt = "\n".join(text_parts(m.parts) for m in model.requests[0].messages)
    assert "paused words" not in prompt and "看不到" in prompt
    assert agent.boundary.paused(actor)
    from clytie.agent.policy import Boundary

    again = Boundary(
        agent.boundary.path, agent.boundary.config, agent.clock, agent.ledger, agent.log
    )
    assert again.paused(actor)
    await again.release(agent.cid, "member")
    assert not again.paused(actor)
    agent.clock.advance(24 * 3600)
    assert not agent.boundary.paused(actor)
    await finish(agent)


async def test_active_chat_reference_cancel_propagates(tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(req, cancel):
        started.set()
        await cancel.wait()
        cancelled.set()
        cancel.raise_if_cancelled()

    agent, channel, _ = await make_agent(
        tmp_path, blocked, {"finish": "stop", "text": "cancel acknowledged"}
    )
    original = event(agent)
    trace = await agent.receive(original)
    await started.wait()
    await agent.receive(event(agent, "停", reply_to=original.message.ref))
    await agent.drain()
    assert cancelled.is_set()
    assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
    assert all(i.trace_id != trace for i in channel.sent)
    await finish(agent)


async def test_proactive_topics_decline_and_no_tools(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path, {"finish": "stop", "text": ""}, {"finish": "stop", "text": "fictional proactive"}
    )
    first = await agent.submit("owner_private", topic="fictional topic", reason="test")
    await agent.drain()
    assert not channel.sent and agent.ledger.get("record", first)["requester"] is None
    await agent.submit("owner_private", topic="fictional next", reason="test")
    await agent.drain()
    assert len(channel.sent) == 1 and channel.sent[0].kind == "proactive"
    assert all(not r.tools for r in model.requests)
    assert all("主动开口时暂不使用能力" in text_parts(r.messages[1].parts) for r in model.requests)
    with pytest.raises(ValueError):
        await agent.submit("owner_private", parts=[TextPart(text="bad")], reason="test")
    with pytest.raises(ValueError):
        await agent.submit("missing", topic="topic", reason="test")
    await finish(agent)


async def test_batches_separate_actors_and_merge_same_actor(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    original = agent.host.behavior

    async def behavior(cid, phase, input, **kw):
        if phase == "attention":
            return Defer(seconds=1) if agent.clock.monotonic() < 1 else Respond()
        return await original(cid, phase, input, **kw)

    agent.host.behavior = behavior
    first = event(agent, "member one", role="member", scene="public_group", mention=True)
    await agent.receive(first)
    await agent.receive(
        event(agent, "member two", role="member", scene="public_group", mention=True)
    )
    await agent.receive(event(agent, "owner one", scene="public_group", mention=True))
    await agent.clock.drain()
    await agent.clock.advance_and_drain(1)
    await agent.drain()
    records = agent.ledger.rows("record")
    assert len(records) == 2
    assert {r["requester"]["role"] for r in records} == {"owner", "member"}
    assert agent.ledger.get("inbound", first.event_id)["state"] == "merged"
    assert len(model.requests) == 2
    await finish(agent)
