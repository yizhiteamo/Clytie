"""Issue #26：Agent 外层取消不能跳过能力收尾和迟到审计。"""

import asyncio

from clytie.sdk.types import ToolResult

from .test_agent import event, finish, make_agent
from .test_agent_acceptance import ECHO


async def stubborn_tool(agent):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    reg = agent.host.registrations(agent.cid, "tool")[0]

    async def execute(plan, inv, cancel):
        calls.append(inv.trace_id)
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                # 故意违约，验证宿主的宽限收尾及迟到结果隔离。
                pass
        return ToolResult(call_id="call_a", status="ok", content="fictional late output", data={})

    reg.implementation.execute = execute
    return started, release, calls


def invocation_for(agent, trace):
    return next(row for row in agent.ledger.rows("invocation") if row["trace_id"] == trace)


async def assert_late_without_replay(agent, channel, trace, release, calls):
    release.set()
    await agent.clock.drain()
    await asyncio.gather(*tuple(agent.kernel.delivery.background))
    audits = [
        row
        for row in agent.ledger.rows("audit")
        if row["kind"] == "late_result" and row["trace_id"] == trace
    ]
    assert len(audits) == 1
    assert audits[0]["content"]["content"] == "fictional late output"
    assert invocation_for(agent, trace)["status"] == "cancelled"
    assert not channel.sent
    agent.ledger.restart()
    await agent.drain()
    assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
    assert invocation_for(agent, trace)["status"] == "cancelled"
    assert calls == [trace] and not channel.sent


async def test_agent_unmentioned_cancel_settles_stubborn_tool_and_audits_late(tmp_path):
    agent, channel, _ = await make_agent(tmp_path, ECHO)
    started, release, calls = await stubborn_tool(agent)
    incoming = event(agent, scene="public_group", mention=True)
    trace = await agent.receive(incoming)
    await started.wait()
    stop = event(agent, "停", scene="public_group")
    stopping = asyncio.create_task(agent.receive(stop))
    await agent.clock.advance_and_drain(agent.kernel.grace)
    await stopping
    await agent.drain()
    row = invocation_for(agent, trace)
    assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
    assert row["status"] == "cancelled"
    assert row["result"]["status"] == "cancelled"
    assert row["result"]["error"]["code"] == "cancelled"
    assert agent.ledger.get("inbound", stop.event_id)["reason"] == "cancel_only"
    assert not channel.sent
    await assert_late_without_replay(agent, channel, trace, release, calls)
    assert await agent.receive(incoming) is None
    assert calls == [trace]
    await finish(agent)


async def test_deadline_and_external_cancel_finish_invocation_once(tmp_path):
    agent, channel, _ = await make_agent(
        tmp_path, ECHO, changes={"tasks": {"default_deadline_seconds": 1}}
    )
    started, release, calls = await stubborn_tool(agent)
    writes = []
    original = agent.ledger.finish_invocation

    def finish_invocation(invocation_id, result):
        changed = original(invocation_id, result)
        writes.append((result.status, changed))
        if result.status == "cancelled":
            # 截止宽限分支已写入，外部取消在其下一个等待点到达。
            agent.kernel.record_tasks[calls[0]].cancel()
        return changed

    agent.ledger.finish_invocation = finish_invocation
    trace = await agent.receive(event(agent, scene="public_group", mention=True))
    await started.wait()
    await agent.clock.advance_and_drain(1 + agent.kernel.grace)
    await agent.drain()
    row = invocation_for(agent, trace)
    assert row["status"] == "cancelled" and row["result"]["error"]["code"] == "deadline"
    assert writes == [("cancelled", True)]
    assert agent.ledger.get("record", trace)["exec_status"] == "cancelled"
    await assert_late_without_replay(agent, channel, trace, release, calls)
    assert writes == [("cancelled", True), ("ok", False)]
    await finish(agent)
