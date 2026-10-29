"""补齐 agent-spec §12 的账本、能力、交付与边界交叉验收。"""

import asyncio
from uuid import uuid4

import pytest

from clytie.agent.policy import text_parts
from clytie.kernel.query import local_task
from clytie.sdk import CancelToken
from clytie.sdk.types import Actor, AssetRef, QuotaSnapshot, Receipt

from .test_agent import event, finish, make_agent

ECHO = {
    "finish": "tool_calls",
    "tool_calls": [
        {"call_id": "call_a", "tool": "echo", "arguments": {"text": "fictional output"}}
    ],
}


async def test_task_query_recent_history_and_counts(tmp_path):
    agent, _, model = await make_agent(tmp_path, ECHO, {"finish": "stop", "text": "done"})
    trace = await agent.receive(event(agent))
    await agent.drain()
    row = agent.ledger.get("record", trace)
    found = local_task(row["task_id"], home=tmp_path, companion_id=agent.cid)
    assert found["ok"] and found["task"]["task_id"] == row["task_id"]
    assert found["task"]["model_calls"] == 2 and found["task"]["tool_rounds"] == 1
    assert set(found["task"]["timings"]) == {
        "queue_seconds",
        "execution_seconds",
        "delivery_seconds",
    }
    await agent.receive(event(agent, "刚才做了什么"))
    await agent.drain()
    prompt = "\n".join(text_parts(m.parts) for m in model.requests[-1].messages)
    assert row["task_id"] in prompt and "fictional output" in prompt and "echo text" in prompt
    await finish(agent)


async def test_cancel_visibility_before_permission_and_terminal(tmp_path):
    agent, _, _ = await make_agent(tmp_path, ECHO, {"finish": "stop", "text": "done"})
    trace = await agent.receive(event(agent))
    await agent.drain()
    task = agent.ledger.get("record", trace)["task_id"]
    actor = Actor(actor_id="member", role="member", channel_id="fake")
    assert await agent.kernel.cancel(agent.cid, actor, "test_group", task_id=task) == "not_found"
    assert (
        await agent.kernel.cancel(agent.cid, actor, "test_group", task_id="abcdef12") == "not_found"
    )
    owner = actor.model_copy(update={"actor_id": "owner", "role": "owner"})
    assert (
        await agent.kernel.cancel(agent.cid, owner, "owner_private", task_id=task)
        == "already_finished"
    )
    await finish(agent)


async def test_capabilities_permissions_disabled_no_tools_and_quota(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    conv = event(agent).conversation
    member = Actor(actor_id="member", role="member", channel_id="fake")
    agent.config["permissions"]["member"] = []
    specs, text = agent.tools(conv, member)
    assert not any(s.name == "echo" for s in specs) and "permission" in text
    agent.config["permissions"]["member"] = ["echo"]
    original = agent.declarations

    def declarations():
        values = original()
        pid, d = values["echo"]
        values["echo"] = (pid, {**d, "heavy": True})
        return values

    agent.declarations = declarations
    agent.protection.update(
        QuotaSnapshot(
            windows=[{"name": "window", "remaining_ratio": 0.2}],
            observed_at=agent.clock.now(),
            source="fake",
        )
    )
    assert agent.reason(member, "echo") == "protection_mode"
    owner = member.model_copy(update={"role": "owner"})
    assert agent.reason(owner, "echo") is None
    agent.protection.update(
        QuotaSnapshot(
            windows=[{"name": "window", "remaining_ratio": 0.31}],
            observed_at=agent.clock.now(),
            source="fake",
        )
    )
    assert agent.reason(member, "echo") is None
    agent.features = agent.features.model_copy(update={"supports_tools": False})
    assert agent.tools(conv, owner)[0] == [] and "model_no_tools" in agent.tools(conv, owner)[1]
    agent.features = agent.features.model_copy(update={"supports_tools": True})
    plan = agent.host.plan_disable(agent.cid, "echo-tool")
    await agent.host.apply(plan.plan_id)
    assert agent.reason(owner, "echo") == "disabled"
    await finish(agent)


@pytest.mark.parametrize("private_card", [False, True])
async def test_only_assets_card_last_or_removed(tmp_path, private_card):
    agent, channel, _ = await make_agent(tmp_path, ECHO, {"finish": "stop", "text": ""})
    reg = agent.host.registrations(agent.cid, "tool")[0]
    original = reg.implementation.execute
    asset = AssetRef(asset_id="a" * 64, media_type="image/png", size=10, origin="fictional")

    async def with_asset(plan, inv, cancel):
        result = await original(plan, inv, cancel)
        return result.model_copy(update={"assets": [asset]})

    reg.implementation.execute = with_asset
    if private_card:
        agent.config["names"]["external"] = {}
    trace = await agent.receive(event(agent, scene="public_group", mention=True))
    await agent.drain()
    assert channel.sent[0].parts[0].type == "image"
    assert len(channel.sent) == (1 if private_card else 2)
    if not private_card:
        assert agent.ledger.get("record", trace)["task_id"] in text_parts(channel.sent[-1].parts)
    else:
        assert agent.warnings
    await finish(agent)


async def test_group_unmentioned_cancel_and_no_task_falls_through(tmp_path):
    started = asyncio.Event()
    agent, channel, model = await make_agent(tmp_path, ECHO)
    reg = agent.host.registrations(agent.cid, "tool")[0]

    async def blocked(plan, inv, cancel):
        started.set()
        await cancel.wait()
        cancel.raise_if_cancelled()

    reg.implementation.execute = blocked
    trace = await agent.receive(event(agent, scene="public_group", mention=True))
    await started.wait()
    stop = event(agent, "停", scene="public_group")
    await agent.receive(stop)
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
    assert agent.ledger.get("inbound", stop.event_id)["reason"] == "cancel_only"
    assert not channel.sent and len(model.requests) == 1
    normal = event(agent, "停", scene="public_group")
    await agent.receive(normal)
    assert agent.ledger.get("inbound", normal.event_id).get("reason") is None
    await finish(agent)


async def test_notifications_roll_24_hours_and_owner_peer_exempt(tmp_path):
    agent, _, _ = await make_agent(tmp_path, changes={"boundary": {"refusal_threshold": 1}})
    notices = []

    class Notifier:
        async def notify(self, notice, cancel):
            notices.append(notice)
            return Receipt(intent_id=uuid4().hex, status="sent", at=agent.clock.now())

    agent.boundary.notifier = Notifier()
    actor = Actor(
        actor_id="member", role="member", channel_id="fake", display_name="fictional nickname"
    )
    for _ in range(2):
        await agent.boundary.report(uuid4().hex, actor, "romance", CancelToken())
    assert len(notices) == 1
    agent.clock.advance(24 * 3600)
    await agent.boundary.report(uuid4().hex, actor, "romance", CancelToken())
    assert len(notices) == 2
    for role in ["owner", "peer"]:
        exempt = actor.model_copy(update={"role": role})
        assert not agent.boundary.paused(exempt)
        specs, _ = agent.tools(event(agent).conversation, exempt)
        assert "core_report_boundary" not in [s.name for s in specs]
    await finish(agent)


async def test_tools_invalid_arguments_round_limit_and_core_only(tmp_path):
    invalid = {
        "finish": "tool_calls",
        "tool_calls": [{"call_id": "bad", "tool": "echo", "arguments": {"text": 4}}],
    }
    agent, _, model = await make_agent(
        tmp_path, invalid, ECHO, changes={"agent": {"max_tool_rounds": 1}}
    )
    trace = await agent.receive(event(agent))
    await agent.drain()
    assert not agent.ledger.rows("invocation")
    assert not model.requests[-1].tools
    assert agent.ledger.get("record", trace)["kind"] == "chat"
    assert any("invalid_arguments" in text_parts(m.parts) for m in model.requests[-1].messages)
    await finish(agent)


async def test_proactive_outbound_no_fallback_or_abilities(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path, *[{"finish": "stop", "text": "fictional_internal"}] * 2
    )
    trace = await agent.submit("test_group", topic="fictional topic", reason="test")
    await agent.drain()
    assert not channel.sent and len(model.requests) == 2
    assert agent.ledger.get("record", trace)["error"]["code"] == "outbound_blocked"
    assert all(not r.tools for r in model.requests)
    await finish(agent)
