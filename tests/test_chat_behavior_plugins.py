"""用生产 Companion 装配假模型、假平台和清单声明的订阅者。"""

import asyncio

import yaml

from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.sdk.testing import FakeChannel, FakeModel, MockClock
from clytie.supervisor.companion import Companion

from .host_helpers import SOURCE, plugin
from .test_agent import ScriptModel
from .test_agent_acceptance import ECHO
from .test_config import write_yaml
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
        assert "".join(p.text for item in channel.sent[:2] for p in item.parts) == "\n\n".join(
            paragraphs
        )
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
