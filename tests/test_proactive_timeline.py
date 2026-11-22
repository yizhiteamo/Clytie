"""使用真实宿主、Agent 及临时账本，平台与模型只采用测试替身。"""

import asyncio
from datetime import datetime, timedelta, timezone

from .host_helpers import enable
from .test_agent import event, finish, make_agent


async def test_hosted_three_day_timeline(tmp_path):
    agent, channel, model = await make_agent(tmp_path)
    # 时间线只驱动主动聊天的定时任务。回想的每分钟检查在它自身的离线用例中验证。
    memory_jobs = tuple(agent.host.memory_jobs[agent.cid])
    for job in memory_jobs:
        job.cancel()
    await asyncio.gather(*memory_jobs, return_exceptions=True)
    clock = agent.clock
    clock.local_tz = timezone(timedelta(hours=8))
    clock.set(datetime(2026, 9, 18, tzinfo=clock.local_tz))
    attempts, rows, waits = [], [], []

    async def submit(pid, cid, **options):
        if len(attempts) + 1 in (3, 9):
            model.turns.append({"text": "", "finish": "stop"})
        result = await agent.submit(cid, plugin_id=pid, **options)
        assert result.status == "accepted"
        attempts.append(
            (clock.now().astimezone(clock.local_tz), options["reason"], result.trace_id)
        )
        return result

    plugin_id = "proactive-chat"
    host = agent.host
    host.proactive_backends[agent.cid] = (submit, agent.proactive_result)
    host.configure(agent.cid, plugins={plugin_id: {"config": {"conversation": "owner_private"}}})
    owner = {
        (18, "09:55"): "owner_private",
        (18, "12:25"): "public_group",
        (18, "16:05"): "owner_private",
        (19, "10:55"): "owner_private",
        (19, "15:07"): "public_group",
        (19, "23:40"): "owner_private",
    }
    try:
        runtime = await enable(host, plugin_id)
        job = host.schedulers[agent.cid].jobs[(plugin_id, "tick")]
        for _ in range(3 * 24 * 60):
            now = clock.now().astimezone(clock.local_tz)
            scene = owner.get((now.day, now.strftime("%H:%M")))
            if scene:
                await agent.receive(event(agent, scene=scene, mention=scene == "public_group"))
                await agent.drain()
                await agent.kernel.bus.drain()
            clock.advance(60)
            while job.due <= clock.now():
                await clock.sleep(0)
            await job.task
            if len(attempts) > len(rows):
                await agent.drain()
                await agent.kernel.bus.drain()
                stamp, reason, trace = attempts[-1]
                outcome = await agent.proactive_result(plugin_id, trace)
                assert outcome.status in {"sent", "declined"}
                rows.append(
                    (stamp.strftime("%m-%d %H:%M"), reason, outcome.status, ",".join(waits))
                )
                waits.clear()
            else:
                wait = runtime.plugin.schedule.wait_reason(clock.now())
                if wait not in waits:
                    waits.append(wait)
        assert len(attempts) == len(rows) > 10
        assert len({trace for _, _, trace in attempts}) == len(attempts)
        assert [row[2] for row in rows].count("declined") == 2
        sent = {intent.trace_id for intent in channel.sent if intent.kind == "proactive"}
        assert len(sent) == len(rows) - 2
        assert [row[1] for row in rows].count("good_morning") == 3
        assert any("unanswered_240" in row[3] for row in rows)
        for row in rows:
            print(" | ".join(row))
    finally:
        await finish(agent)
