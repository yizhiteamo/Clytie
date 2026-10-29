"""重启、投影、主动上下文和取消宽限期的对话回归。"""

import asyncio
from uuid import uuid4

from clytie.agent.policy import text_parts
from clytie.kernel.ledger import SCHEMA_VERSION
from clytie.kernel.migration import LedgerRuntime
from clytie.sdk.types import Receipt

from .host_helpers import enable, plugin
from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO


async def test_a31_migration_retains_messages_and_initializes_fields(tmp_path):
    agent, _, _ = await make_agent(tmp_path)
    e = event(agent)
    agent.ledger.claim_inbound(e)
    agent.ledger.db.execute("PRAGMA user_version=1")
    clock = agent.clock
    await finish(agent)
    runtime = LedgerRuntime(tmp_path, "companion_a", clock)
    ledger = await runtime.open()
    row = ledger.get("inbound", e.event_id)
    assert row["message"]["parts"][0]["text"] == "fictional request"
    assert row["reason"] is None and row["merged_into"] is None
    assert ledger.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list((tmp_path / "backups/ledger/companion_a").glob("*/snapshot.enc"))
    ledger.restart()
    assert ledger.get("inbound", e.event_id)["state"] == "dropped_on_restart"
    runtime.close()


async def test_partial_split_context_has_three_truthful_classes_once(tmp_path):
    agent, channel, model = await make_agent(tmp_path, {"finish": "stop", "text": "one two three"})
    original = agent.host.behavior

    async def behavior(cid, phase, input, **kw):
        if (
            phase == "after_generation"
            and len(input.parts) == 1
            and input.parts[0].text == "one two three"
        ):
            from clytie.sdk.types import TextPart

            return input.model_copy(
                update={
                    "form": "split",
                    "parts": [TextPart(text=x) for x in ["one", "two", "three"]],
                }
            )
        return await original(cid, phase, input, **kw)

    agent.host.behavior = behavior
    count = 0

    async def send(intent, cancel):
        nonlocal count
        count += 1
        channel.sent.append(intent)
        return Receipt(
            intent_id=intent.intent_id,
            status="unknown" if count == 2 else "sent",
            at=agent.clock.now(),
        )

    channel.send = send
    await agent.receive(event(agent))
    await agent.drain()
    await agent.receive(event(agent, "what happened"))
    await agent.drain()
    prompt = "\n".join(text_parts(m.parts) for m in model.requests[-1].messages)
    assert (
        "第 1 段：确定发出" in prompt
        and "第 2 段：不确定是否发出" in prompt
        and "第 3 段：没有发出" in prompt
    )
    await agent.receive(event(agent, "again"))
    await agent.drain()
    assert not any("上次拆分消息" in text_parts(m.parts) for m in model.requests[-1].messages)
    await finish(agent)


async def test_context_contributor_receives_none_only_for_proactive(tmp_path):
    plugin(
        tmp_path,
        "context-example",
        """
from clytie.sdk.types import ContextSection
class Plugin:
    async def start(self, ctx):
        self.seen=[]
    async def stop(self): pass
    async def context(self, req):
        self.seen.append(req)
        return ContextSection(title="fictional",text="fictional section",priority=1)
""",
        contributes={"context_section": {"handler": "context", "budget_chars": 200}},
    )
    agent, _, model = await make_agent(tmp_path)
    runtime = await enable(agent.host, "context-example")
    await agent.submit("owner_private", topic="fictional topic", reason="test")
    await agent.drain()
    assert runtime.plugin.seen[-1].actor is None
    assert runtime.plugin.seen[-1].scene == "owner_private"
    await agent.receive(event(agent))
    await agent.drain()
    assert runtime.plugin.seen[-1].actor.role == "owner"
    assert all(
        any("fictional section" in text_parts(m.parts) for m in r.messages) for r in model.requests
    )
    await finish(agent)


async def test_core_boundary_calls_audited_without_task(tmp_path):
    calls = {
        "finish": "tool_calls",
        "tool_calls": [
            {"call_id": "a", "tool": "core_report_boundary", "arguments": {"category": "romance"}},
            {
                "call_id": "b",
                "tool": "core_report_boundary",
                "arguments": {"category": "owner_privacy"},
            },
        ],
    }
    agent, _, model = await make_agent(
        tmp_path, calls, {"finish": "stop", "text": "fictional refusal"}
    )
    trace = await agent.receive(event(agent, role="member", scene="public_group", mention=True))
    await agent.drain()
    assert agent.ledger.get("record", trace)["kind"] == "chat"
    assert not agent.ledger.rows("invocation")
    assert list(agent.boundary.state["actors"]["member"]["counts"]) == ["romance"]
    results = [text_parts(m.parts) for m in model.requests[-1].messages if m.role == "tool"]
    assert results == ["已记录", "已记录"]
    await finish(agent)


async def test_model_deadline_uses_mock_clock_and_cancels_child(tmp_path):
    started = asyncio.Event()
    seen = []

    async def blocked(req, cancel):
        seen.append(cancel)
        started.set()
        await cancel.wait()
        cancel.raise_if_cancelled()

    agent, channel, _ = await make_agent(
        tmp_path, blocked, changes={"agent": {"model_call_seconds": 10}}
    )
    trace = await agent.receive(event(agent))
    await started.wait()
    await agent.clock.advance_and_drain(10)
    await agent.drain()
    assert seen[0].cancelled and seen[0].reason == "deadline"
    assert agent.ledger.get("record", trace)["exec_status"] == "error"
    assert len(channel.sent) == 1
    await finish(agent)


async def test_pausing_actor_does_not_cancel_started_tool(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    agent, channel, _ = await make_agent(
        tmp_path,
        ECHO,
        {"finish": "stop", "text": "finished"},
        changes={"boundary": {"refusal_threshold": 1}},
    )
    reg = agent.host.registrations(agent.cid, "tool")[0]
    original = reg.implementation.execute

    async def blocked(plan, inv, cancel):
        started.set()
        await release.wait()
        return await original(plan, inv, cancel)

    reg.implementation.execute = blocked
    e = event(agent, role="member", scene="public_group", mention=True)
    trace = await agent.receive(e)
    await started.wait()
    from clytie.sdk import CancelToken

    await agent.boundary.report(uuid4().hex, e.message.sender, "romance", CancelToken())
    release.set()
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "ok" and channel.sent
    await finish(agent)


async def test_model_cancelled_result_seals_without_fallback(tmp_path):
    agent, channel, _ = await make_agent(tmp_path, {"finish": "cancelled"})
    trace = await agent.receive(event(agent))
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
    assert not channel.sent
    await finish(agent)


async def test_fallback_behavior_failure_still_seals_error(tmp_path):
    agent, channel, _ = await make_agent(tmp_path, {"finish": "error"})
    original = agent.host.behavior

    async def behavior(cid, phase, value, **kwargs):
        if phase == "after_generation":
            raise ValueError("fictional formatter failure")
        return await original(cid, phase, value, **kwargs)

    agent.host.behavior = behavior
    trace = await agent.receive(event(agent))
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "error"
    assert not channel.sent
    await finish(agent)
