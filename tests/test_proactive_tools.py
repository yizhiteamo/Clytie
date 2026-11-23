"""主动开口使用能力；场景、账号和模型回复均为虚构。"""

import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from clytie.agent.policy import text_parts
from clytie.backup.coordination import run_sync
from clytie.config import load_home
from clytie.kernel.query import task_view
from clytie.sdk.testing import FakeChannel, MockClock
from clytie.sdk.types import ImagePart, ToolCall
from clytie.supervisor.companion import Companion

from .host_helpers import plugin
from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO
from .test_agent_placeholders import with_parts
from .test_appearance_requests import image_channel, single_reference, vision_model
from .test_config import make_home, write_yaml
from .test_kernel_reactions import fixture
from .test_storage_agent_lifecycle import private_home


@pytest.mark.parametrize(
    "name,daily,valid",
    [
        ("echo", 1, True),
        ("echo", 200, True),
        ("echo", 0, False),
        ("echo", 201, False),
        ("echo", True, False),
        ("missing", 1, False),
        ("core_recall", 1, False),
    ],
)
def test_proactive_tool_config_requires_active_capability_and_daily_limit(
    tmp_path, name, daily, valid
):
    home, root, _, companion = make_home(tmp_path)
    src = Path(__file__).resolve().parents[1] / "plugins/echo-tool/plugin.json"
    manifest = json.loads(src.read_text(encoding="utf-8"))
    path = root / "plugins/echo-tool/plugin.json"
    path.parent.mkdir()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    companion.setdefault("plugins", {})["echo-tool"] = {"enabled": True, "config": {}}
    companion["proactive"] = {"tools": {name: {"daily": daily}}}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=root)
    assert report.valid is valid, report.errors
    if not valid:
        assert any("proactive" in e.pointer for e in report.errors)


@pytest.mark.parametrize("condition", ["allowed", "unlisted", "forbidden", "scope", "protection"])
async def test_proactive_tool_offers_only_allowed_owner_private_capabilities(
    tmp_path, condition, monkeypatch
):
    agent, _, model = await make_agent(
        tmp_path, changes={"proactive": {"tools": {"echo": {"daily": 3}}}}
    )
    try:
        if condition == "unlisted":
            agent.config["proactive"]["tools"] = {}
        elif condition == "forbidden":
            agent.config["permissions"]["owner"] = []
        elif condition == "scope":
            agent.config["tool_scopes"] = {"echo": {"private": []}}
        elif condition == "protection":
            pid, declaration = agent.declarations()["echo"]
            monkeypatch.setattr(
                agent, "declarations", lambda: {"echo": (pid, {**declaration, "heavy": True})}
            )
            agent.protection.active = True
        await agent.submit("owner_private", topic="fictional", reason="contract", tools=True)
        await agent.drain()
        names = {spec.name for spec in model.requests[0].tools}
        assert names == ({"core_recall", "echo"} if condition == "allowed" else {"core_recall"})
    finally:
        await finish(agent)


async def test_proactive_tool_quota_is_per_local_day_and_ignores_ordinary_invocations(tmp_path):
    final = {"finish": "stop", "text": "fictional result"}
    agent, channel, model = await make_agent(
        tmp_path,
        ECHO,
        final,
        ECHO,
        final,
        final,
        final,
        changes={"proactive": {"tools": {"echo": {"daily": 1}}}},
    )
    try:
        agent.clock.local_tz = ZoneInfo("Asia/Shanghai")
        local_now = agent.clock.now().astimezone(agent.clock.local_tz)
        agent.clock.set(local_now.replace(hour=23, minute=59, second=0))
        await agent.receive(event(agent))
        await agent.drain()
        submission = await agent.submit(
            "owner_private", topic="fictional", reason="contract", tools=True
        )
        trace = submission.trace_id
        await agent.drain()
        record = agent.ledger.get("record", trace)
        assert record["requester"] is None and record["exec_status"] == "ok"
        assert record["task_id"] and task_view(agent.ledger, record)["origin"] == "proactive"
        assert "echo" in {s.name for s in model.requests[2].tools}
        assert not any(i.trace_id == trace for i in channel.reacts)
        assert all(
            i.kind == "proactive" and record["task_id"] not in text_parts(i.parts)
            for i in channel.sent
            if i.trace_id == trace
        )
        assert any(
            a.get("trace_id") == trace and a["kind"] == "proactive.submitted"
            for a in agent.ledger.rows("audit")
        )
        await agent.submit("owner_private", topic="again", reason="contract", tools=True)
        await agent.drain()
        assert "echo" not in {s.name for s in model.requests[-1].tools}
        assert any("proactive_limit" in text_parts(m.parts) for m in model.requests[-1].messages)
        agent.clock.advance(61)
        await agent.submit("owner_private", topic="tomorrow", reason="contract", tools=True)
        await agent.drain()
        assert "echo" in {s.name for s in model.requests[-1].tools}
    finally:
        await finish(agent)


async def test_proactive_tools_require_registered_owner_private_account(tmp_path):
    agent, _, model = await make_agent(
        tmp_path,
        conversations={
            "other": {"kind": "private", "channel": "fake-channel", "scene": "owner_private"},
        },
    )
    try:
        for target in ["test_group", "other", "unregistered"]:
            with pytest.raises(ValueError):
                await agent.submit(target, topic="fictional", reason="contract", tools=True)
        assert not agent.ledger.rows("record") and not model.requests
    finally:
        await finish(agent)


async def test_owner_can_cancel_a_proactive_task_while_ordinary_reply_waits(tmp_path, monkeypatch):
    agent, channel, model = await make_agent(
        tmp_path, ECHO, changes={"proactive": {"tools": {"echo": {"daily": 1}}}}
    )
    entered, release = asyncio.Event(), asyncio.Event()
    capability = agent.host.registrations(agent.cid, "tool")[0].implementation
    original = capability.execute

    async def block(plan, invocation, cancel):
        entered.set()
        waiters = [asyncio.create_task(release.wait()), asyncio.create_task(cancel.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        cancel.raise_if_cancelled()
        return await original(plan, invocation, cancel)

    monkeypatch.setattr(capability, "execute", block)
    try:
        submission = await agent.submit(
            "owner_private", topic="fictional", reason="test", tools=True
        )
        trace = submission.trace_id
        if queued := agent.kernel.queued.get(trace):
            started = asyncio.create_task(entered.wait())
            try:
                await asyncio.wait({started, queued}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                started.cancel()
                await asyncio.gather(started, return_exceptions=True)
        assert entered.is_set()
        task = agent.ledger.get("record", trace)
        assert task["task_id"] and task["requester"] is None
        owner_message = event(agent, "fictional incoming")
        following = await agent.receive(owner_message)
        assert agent.ledger.get("record", following)["exec_status"] == "pending"
        assert len(model.requests) == 1
        assert (
            await agent.kernel.cancel(
                agent.cid, owner_message.message.sender, "owner_private", task_id=task["task_id"]
            )
            == "cancelled"
        )
        release.set()
        await agent.drain()
        assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
        assert agent.ledger.get("record", following)["exec_status"] == "ok"
        assert all(i.trace_id != trace for i in channel.reacts + channel.sent)
    finally:
        release.set()
        await finish(agent)


async def test_proactive_read_only_core_tools_follow_the_current_image_context(
    tmp_path, monkeypatch
):
    vision_model(monkeypatch)
    single_reference(tmp_path)
    agent, channel, model = await make_agent(
        tmp_path,
        changes={
            "appearance": {"self_check": False},
            "vision": {"max_images": 2},
        },
    )
    try:
        image_channel(channel)
        images = [ImagePart(raw_ref=color) for color in ["red", "blue", "yellow"]]
        await agent.receive(with_parts(event(agent, ""), images))
        await agent.drain()
        await agent.submit("owner_private", topic="fictional", reason="test", tools=True)
        await agent.drain()
        assert {s.name for s in model.requests[-1].tools} == {
            "core_recall",
            "core_view_image",
            "core_view_appearance",
        }
        agent.config["tool_scopes"] = {"core_view_image": {"private": []}}
        await agent.submit("owner_private", topic="fictional", reason="test", tools=True)
        await agent.drain()
        assert {s.name for s in model.requests[-1].tools} == {"core_recall"}
    finally:
        await finish(agent)


async def test_companion_pauses_every_plugin_before_record_or_model(tmp_path, monkeypatch):
    home = private_home(tmp_path)
    path = home / "companions/companion_a/companion.yaml"
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    plugins = ("fictional-a", "fictional-b")
    for pid in plugins:
        plugin(home, pid, permissions=["message.proactive"])
        settings["plugins"][pid] = {"enabled": True, "config": {}}
    write_yaml(path, settings)
    connect = FakeChannel.connect

    async def quiet(channel):
        channel.incoming = ()
        await connect(channel)

    monkeypatch.setattr(FakeChannel, "connect", quiet)
    for turn in range(2):
        companion = Companion(home, "companion_a", MockClock())
        await companion.start()
        try:
            await asyncio.gather(*companion.feeds)
            if turn == 0:
                await companion.agent.proactive_pause.set(True)
            for pid, payload in zip(
                plugins, [{"topic": "fictional topic", "tools": True}, {"parts": []}]
            ):
                ctx = companion.host.runtimes[companion.cid, pid].ctx
                submission = await ctx.proactive.submit("owner_private", reason="sample", **payload)
                assert submission.status == "paused" and submission.trace_id is None
            await companion.agent.drain()
            ledger = companion.kernel.ledger
            assert not await run_sync(ledger, ledger.rows, "record")
            audit = await run_sync(ledger, ledger.rows, "audit")
            rows = [a for a in audit if a["kind"] == "proactive.paused"]
            assert [a["plugin"] for a in rows] == list(plugins) * (turn + 1)
            assert all(a["reason"] == "sample" for a in rows)
            assert not any(a["kind"] == "model.usage" for a in audit)
            assert not next(iter(companion.channels.values())).sent
        finally:
            await companion.stop()


def test_daily_allowance_is_claimed_once_per_idempotency_key_and_persists(tmp_path):
    ledger, _, _, clock, trace = fixture(tmp_path)
    try:
        clock.local_tz = ZoneInfo("Asia/Shanghai")
        local = clock.now().astimezone(clock.local_tz)
        clock.set(local.replace(hour=23, minute=59))
        call = ToolCall(call_id="fictional", tool="echo", arguments={})

        def claim(key):
            return ledger.claim_invocation(
                trace, "echo-tool", call, key, timeout=60, schema={}, proactive_limit=1
            )[0]

        assert ledger.proactive_counts() == {}
        first = claim("proactive")
        assert first["invocation_id"] == claim("proactive")["invocation_id"]
        assert claim("another") is None
        path, cid = ledger.path, ledger.companion_id
        ledger.close()
        ledger = type(ledger)(path, cid, clock)
        assert ledger.proactive_counts() == {"echo": 1}
        clock.advance(61)
        assert ledger.proactive_counts() == {}
        assert claim("another") is not None
    finally:
        ledger.close()
