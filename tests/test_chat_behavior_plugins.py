"""用生产 Companion 装配假模型、假平台和清单声明的订阅者。"""

import asyncio
import json

import pytest
import yaml

from clytie.clock import SystemClock
from clytie.plugin_host.identity import IdentityResolver
from clytie.plugin_host.support import timed
from clytie.sdk.testing import FakeChannel, FakeModel, FakeWebSocket, MockClock
from clytie.sdk.types import Addressed, ModelTurn, ToolCall
from clytie.supervisor.companion import Companion

from .host_helpers import SOURCE, enable, plugin
from .test_agent import ScriptModel, finish, make_agent
from .test_agent_acceptance import ECHO
from .test_agent_addressed import group, observe
from .test_config import write_yaml
from .test_kernel_service import new_record, reply
from .test_openai_api_integration import Protocol
from .test_storage_agent_lifecycle import inbound, private_home


async def test_companion_group_receipts_and_task_events(tmp_path, monkeypatch):
    home = private_home(tmp_path)
    source = "import asyncio\n" + SOURCE.replace(
        "self.seen = []", "self.seen = []\n        self.observed = asyncio.Queue()"
    ).replace(
        "self.seen.append(event)",
        "self.seen.append(event)\n        self.observed.put_nowait(event.name)",
    )
    plugin(
        home,
        "fictional-listener",
        source,
        contributes={
            "events": {
                "subscribe": ["message.received", "reply.sent", "task.*"],
                "handler": "on_event",
            }
        },
    )
    path = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["plugins"].update(
        {
            "fictional-listener": {"enabled": True, "config": {}},
            "chat-timing": {
                "enabled": True,
                "config": {
                    "judge": "rules",
                    "activity": "low",
                    "schedule": [{"from": "08:00", "to": "08:01", "activity": "high"}],
                    "tuning": {"high_probability": 1},
                },
            },
            "reply-form": {"enabled": True, "config": {"tuning": {"interval_seconds": [0, 0]}}},
            "echo-tool": {"enabled": True, "config": {}},
        }
    )
    config["task_reactions"] = {"min_interval_seconds": 0}
    write_yaml(path, config)
    paragraphs = ["虚构的短句。" * 9, "另一段虚构回复。" * 5]
    script = ScriptModel(
        {"finish": "stop", "text": "\n\n".join(paragraphs)},
        {"finish": "stop", "text": "虚构第二条"},
        ECHO,
        {"finish": "stop", "text": "虚构完成"},
    )

    async def generate(self, req, cancel):
        return await script.generate(req, cancel)

    monkeypatch.setattr(FakeModel, "generate", generate)
    monkeypatch.setattr(FakeModel, "features", lambda self: script.features())
    connect = FakeChannel.connect

    async def quiet_connect(self):
        self.incoming = ()
        await connect(self)

    monkeypatch.setattr(FakeChannel, "connect", quiet_connect)

    clock = MockClock()
    watchdog = SystemClock()
    runtime = Companion(home, "companion_a", clock)
    await runtime.start()
    try:
        await asyncio.gather(*runtime.feeds)
        h = runtime.host
        listener = h.runtimes[runtime.cid, "fictional-listener"].plugin
        unit = h.runtimes[runtime.cid, "chat-timing"]
        assert unit.ctx.clock.local_tz.key == "Asia/Shanghai"

        async def status():
            route = next(
                r for r in h.registrations(runtime.cid, "api_route") if r.plugin_id == "chat-timing"
            )
            return (await h.invoke(runtime.cid, route.plugin_id, route.point, route.key, {}))[
                "json"
            ]["groups"]["test_group"]

        for i in range(2):
            await runtime.agent.receive(
                inbound(runtime, text="虚构的入站标记", scene="public_group", role="member")
            )
            await clock.advance_and_drain(5)
            assert await timed(listener.observed.get(), watchdog, 10) == "message.received"
            for _ in range(2 if i == 0 else 1):
                assert await timed(listener.observed.get(), watchdog, 10) == "reply.sent"
            await runtime.agent.drain()
            await h.event_bus.drain()
            seen = [e.name for e in listener.seen]
            assert seen.count("message.received") == i + 1
            assert seen.count("reply.sent") == 2 + i
            assert (await status())["natural_today"] == i + 1
        channel = next(iter(runtime.channels.values()))
        assert [p.text for item in channel.sent[:2] for p in item.parts] == paragraphs
        assert (await status())["activity"] == "high"
        await clock.advance_and_drain(50)
        assert (await status())["activity"] == "low"
        await runtime.agent.receive(inbound(runtime, text="虚构任务标记"))
        await clock.advance_and_drain(3)

        async def wait_for_task():
            while await listener.observed.get() != "task.completed":
                pass

        await timed(wait_for_task(), watchdog, 10)
        await runtime.agent.drain()
        await h.event_bus.drain()
        seen = [e.name for e in listener.seen]
        assert seen.count("task.started") == seen.count("task.completed") == 1
        assert seen.count("message.received") == 3
        assert set(seen) == {"task.started", "task.completed", "message.received", "reply.sent"}
        assert "plugin.enabled" in {e.name for e in h.events}
        for mark in ["虚构的入站标记", "虚构任务标记", "虚构第二条", paragraphs[0]]:
            assert all(mark not in str(e.payload) for e in listener.seen)
    finally:
        await runtime.stop()
    assert runtime.kernel.bus.closed


async def connect_onebot(agent, tmp_path):
    host = agent.host
    protocol = Protocol()
    backends = host.backends
    host.backends = lambda cid, pid: {
        **backends(cid, pid),
        **({"websocket": FakeWebSocket([protocol])} if pid == "qq-onebot" else {}),
    }
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/test.txt").write_text("fictional-token", encoding="utf-8")
    secret_field = "secret"
    host.accounts = {
        "fake": {"channel": "qq-onebot", "account": "1000000001", secret_field: "test.txt"}
    }
    host.identities[agent.cid, "qq-onebot", "fake"] = IdentityResolver(
        "fake",
        "qq-onebot",
        {
            "accounts": host.accounts,
            "people": {
                "member": {
                    "role": "member",
                    "accounts": [{"channel": "qq-onebot", "account": "1000000002"}],
                }
            },
            "conversations": {
                "test_group": {
                    "channel": "qq-onebot",
                    "kind": "group",
                    "platform_id": "1000000003",
                    "scene": "public_group",
                }
            },
        },
    )
    host.unit_configs[agent.cid, "qq-onebot"] = [
        {"role": "channel", "account": "fake", "config": {"url": "ws://example.invalid/"}}
    ]
    host.configure(
        agent.cid,
        plugins={
            "chat-timing": {"config": {"judge": "rules", "tuning": {"medium_probability": 0}}},
            "reply-form": {"config": {"tuning": {"interval_seconds": [0, 0]}}},
        },
    )
    for pid in ("qq-onebot", "chat-timing", "reply-form"):
        await enable(host, pid)
    channel = host.runtimes[agent.cid, "qq-onebot"].plugin.create_channel()
    agent.kernel.delivery.channels["fake"] = channel
    await channel.connect()
    return protocol, channel


@pytest.mark.parametrize("interleaved", [False, True])
async def test_addressed_split_quotes_first_onebot_send_only(tmp_path, interleaved):
    entered, resume = asyncio.Event(), asyncio.Event()
    paragraphs = ["虚构第一句。" * 8, "虚构第二句。" * 8]

    async def generate(request, cancel):
        entered.set()
        await resume.wait()
        return ModelTurn(finish="stop", text="\n\n".join(paragraphs), usage={})

    agent, _, model = await make_agent(tmp_path, generate)
    try:
        protocol, _ = await connect_onebot(agent, tmp_path)
        called = group(agent, mention=True)
        ref = called.message.ref.model_copy(update={"platform_message_id": "-7"})
        called = called.model_copy(
            update={
                "event_id": "fake:-7",
                "message": called.message.model_copy(update={"ref": ref}),
            }
        )
        await agent.receive(called)
        await agent.clock.advance_and_drain(3)
        await timed(entered.wait(), SystemClock(), 5)
        if interleaved:
            await agent.receive(group(agent, actor="another_member"))
            await agent.clock.advance_and_drain(5)
        resume.set()
        await agent.drain()
        messages = [
            c["params"]["message"]
            for raw in protocol.sent
            if (c := json.loads(raw))["action"] == "send_group_msg"
        ]
        first = [{"type": "text", "data": {"text": paragraphs[0]}}]
        if interleaved:
            first.insert(0, {"type": "reply", "data": {"id": "-7"}})
        assert messages == [first, [{"type": "text", "data": {"text": paragraphs[1]}}]]
        assert len(model.requests) == 1
    finally:
        resume.set()
        await finish(agent)


async def onebot_message(protocol, events, identifier, *segments):
    protocol.messages.put_nowait(
        json.dumps(
            {
                "post_type": "message",
                "message_type": "group",
                "self_id": "1000000001",
                "user_id": "1000000002",
                "group_id": "1000000003",
                "message_id": identifier,
                "time": 0,
                "message": segments,
            }
        )
    )
    return await timed(anext(events), SystemClock(), 5)


async def test_onebot_reply_to_sent_message_addresses_and_quotes_original(tmp_path, monkeypatch):
    original = "Fictional original reply."
    agent, _, model = await make_agent(tmp_path, {"finish": "stop", "text": original})
    seen = []

    def record_mark(phase, event):
        if phase == "message_received":
            seen.append(event.addressed)

    observe(agent, monkeypatch, record_mark)
    try:
        protocol, channel = await connect_onebot(agent, tmp_path)
        events = channel.events()
        try:
            text = {"type": "text", "data": {"text": "Fictional question"}}
            called = await onebot_message(
                protocol, events, 10, {"type": "at", "data": {"qq": "1000000001"}}, text
            )
            await agent.receive(called)
            await agent.clock.advance_and_drain(3)
            await agent.drain()
            (receipt,) = [row["receipt"] for row in agent.ledger.rows("attempt")]
            assert receipt["status"] == "sent"
            reply = {"type": "reply", "data": {"id": receipt["platform_message_id"]}}
            await agent.receive(await onebot_message(protocol, events, 11, reply, text))
            await agent.clock.advance_and_drain(3)
            await agent.drain()
            assert seen[-1] == Addressed(reply_to_self=True)
            assert len(model.requests) == 2
            assert any(
                "引用：" + original in part.text
                for msg in model.requests[-1].messages
                for part in msg.parts
                if part.type == "text"
            )
            other = {"type": "reply", "data": {"id": called.message.ref.platform_message_id}}
            await agent.receive(await onebot_message(protocol, events, 12, other, text))
            await agent.clock.advance_and_drain(5)
            await agent.drain()
            assert seen[-1] == Addressed()
            assert len(model.requests) == 2
        finally:
            await events.aclose()
    finally:
        await finish(agent)


async def test_onebot_group_quote_cancel_selects_sent_reply_record(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    try:
        protocol, channel = await connect_onebot(agent, tmp_path)
        # Keep a newer task open: falling back to the latest task would cancel the wrong one.
        records = []
        for name in ("first", "second"):
            record, _, _ = await new_record(agent.kernel, role="member", name=name)
            trace = record["trace_id"]
            agent.ledger.claim_invocation(
                trace,
                "echo-tool",
                ToolCall(call_id=name, tool="echo", arguments={}),
                name,
                timeout=60,
                schema={},
            )
            records.append(agent.ledger.get("record", trace))
        sent = reply(records[0])
        agent.ledger.register_intents(sent.trace_id, [sent])
        await agent.kernel.delivery.send_one(sent.intent_id)
        (receipt,) = [row["receipt"] for row in agent.ledger.rows("attempt")]
        assert receipt["status"] == "sent"
        events = channel.events()
        try:
            stop = await onebot_message(
                protocol,
                events,
                13,
                {"type": "reply", "data": {"id": receipt["platform_message_id"]}},
                {"type": "text", "data": {"text": "停"}},
            )
            await agent.receive(stop)
            row = agent.ledger.get("inbound", stop.event_id)
            assert (row["state"], row["reason"]) == ("ignored", "cancel_only")
            assert agent.ledger.get("record", sent.trace_id)["exec_status"] == "cancelled"
            assert agent.ledger.get("record", records[1]["trace_id"])["exec_status"] == "running"
            assert not model.requests
        finally:
            await events.aclose()
    finally:
        await agent.clock.advance_and_drain(5)
        await finish(agent)
