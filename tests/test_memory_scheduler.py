"""默认插件经清单、start、factory 装配，仅调用脚本假模型。"""

import json

from .test_agent import event, finish, make_agent


async def test_idle_recollect_once_new_messages_only_and_no_platform_send(tmp_path):
    agent, channel, model = await make_agent(
        tmp_path,
        {"finish": "stop", "text": "虚构聊天答复"},
        {
            "finish": "stop",
            "text": json.dumps(
                [{"kind": "episode", "content": "一起折纸船", "evidence": [1]}], ensure_ascii=False
            ),
        },
    )
    try:
        await agent.receive(event(agent, "一起折纸船"))
        await agent.drain()
        before = len(channel.sent)
        await agent.clock.advance_and_drain(30 * 60)
        records = agent.memory.store.all_records()
        assert len(records) == 1
        assert records[0].content == "一起折纸船"
        assert records[0].basis == "observed"
        assert len(channel.sent) == before
        assert len(model.requests) == 2
        await agent.clock.advance_and_drain(30 * 60)
        assert len(model.requests) == 2
        assert len(agent.memory.store.all_records()) == 1
    finally:
        await finish(agent)


async def test_unrelated_group_skips_model_but_advances_progress(tmp_path):
    agent, channel, model = await make_agent(tmp_path)
    try:
        message = event(agent, "虚构群友闲聊", role="member", scene="public_group")
        agent.ledger.claim_inbound(message)
        agent.ledger.ignore(message.event_id)
        await agent.clock.advance_and_drain(30 * 60)
        assert not model.requests
        assert not channel.sent
        progress = agent.recollector.progress("test_group")
        assert progress["inbound"] > 0
        assert progress["written"] == 0
    finally:
        await finish(agent)


async def test_daily_limit_and_stranger_skip(tmp_path):
    agent, _, model = await make_agent(
        tmp_path,
        {"finish": "stop", "text": "[]"},
        changes={"memory": {"recollect": {"daily_limit": 1}}},
    )
    try:
        first = event(agent, "虚构第一段")
        agent.ledger.claim_inbound(first)
        agent.ledger.ignore(first.event_id)
        await agent.clock.advance_and_drain(30 * 60)
        assert len(model.requests) == 1
        second = event(agent, "虚构第二段")
        agent.ledger.claim_inbound(second)
        agent.ledger.ignore(second.event_id)
        stranger = event(agent, "陌生人的虚构消息", role="stranger", scene="stranger_private")
        agent.ledger.claim_inbound(stranger)
        agent.recollector.activity(stranger.conversation)
        await agent.clock.advance_and_drain(30 * 60)
        assert len(model.requests) == 1
        assert agent.recollector.progress("owner_private")["count"] == 1
    finally:
        await finish(agent)


async def test_retracted_window_raw_chat_unchanged_no_derived_retracted_record(tmp_path):
    agent, _, model = await make_agent(
        tmp_path,
        {
            "finish": "stop",
            "text": json.dumps(
                [
                    {"kind": "episode", "content": "虚构的撤销内容", "evidence": [1]},
                    {"kind": "episode", "content": "另一件虚构新事", "evidence": [1]},
                ],
                ensure_ascii=False,
            ),
        },
    )
    try:
        message = event(agent, "虚构的撤销内容，别记这个")
        agent.ledger.claim_inbound(message)
        trace = agent.ledger.open_record(event_id=message.event_id)
        if isinstance(trace, dict):
            trace = trace["trace_id"]
        store = agent.memory.store
        record = store.write(
            dict(kind="event", content="虚构的撤销内容", visibility="private"),
            scene="owner_private",
            conversation_id="owner_private",
            owner="owner",
            operator="owner",
            trace=trace,
        )
        store.revise(
            record.record_id,
            1,
            {"retracted": True},
            scene="owner_private",
            conversation_id="owner_private",
            operator="owner",
            trace=trace,
        )
        agent.ledger.seal(trace, "ok")
        await agent.clock.advance_and_drain(30 * 60)
        request = model.requests[0]
        body = json.loads(request.messages[-1].parts[0].text)
        assert body["window"]["messages"][0]["text"] == message.message.parts[0].text
        assert body["window"]["retracted_count"] == 1
        assert body["window"]["remembered"] == []
        assert body["candidates"] == []
        assert record.record_id not in str(request.model_dump())
        assert [r.content for r in store.all_records() if not r.retracted] == ["另一件虚构新事"]
        assert store.meta("recollect_semantic_failures") == 1
        assert agent.recollector.progress("owner_private")["inbound"] > 0
    finally:
        await finish(agent)


async def test_import_reset_baseline_skips_history(tmp_path):
    agent, _, model = await make_agent(tmp_path)
    try:
        message = event(agent, "基线之前的虚构内容")
        agent.ledger.claim_inbound(message)
        agent.ledger.ignore(message.event_id)
        with agent.memory.store.transaction():
            agent.memory.store.db.execute("DELETE FROM recollect_progress")
        agent.recollector.baseline()
        await agent.clock.advance_and_drain(30 * 60)
        assert not model.requests
        assert not agent.memory.store.all_records()
    finally:
        await finish(agent)


async def test_model_follows_chat_switch_and_explicit_model_unit(tmp_path):
    from datetime import timedelta

    from clytie.memory.scheduler import RecollectionModel
    from clytie.sdk import CancelToken
    from clytie.sdk.types import Budget, ModelRequest, ModelTurn

    from .host_helpers import enable
    from .test_agent import ScriptModel

    agent, _, first = await make_agent(tmp_path, {"finish": "stop", "text": "first"})
    try:
        backend = RecollectionModel(agent)
        request = ModelRequest(
            trace_id="a" * 32, budget=Budget(deadline=agent.clock.now() + timedelta(seconds=60))
        )
        assert (
            await backend.generate(request, CancelToken(), purpose="memory.recollect")
        ).text == "first"
        second = ScriptModel({"finish": "stop", "text": "second"})
        agent.model = second
        assert (
            await backend.generate(request, CancelToken(), purpose="memory.recollect")
        ).text == "second"
        agent.host.unit_configs[agent.cid, "fake-model"] = [
            {"role": "model", "config": {}},
            {"role": "model", "account": "recollect_model", "config": {}},
        ]
        await enable(agent.host, "fake-model")
        reg = next(
            r for r in agent.host.registrations(agent.cid, "model") if r.key == "recollect_model"
        )
        calls = []

        async def generate(req, cancel):
            calls.append(req)
            return ModelTurn(text="explicit", finish="stop", usage={})

        reg.implementation.generate = generate
        agent.config["memory"] = {"recollect": {"model": {"plugin": "fake-model"}}}
        assert (
            await backend.generate(request, CancelToken(), purpose="memory.recollect")
        ).text == "explicit"
        assert len(first.requests) == len(second.requests) == len(calls) == 1
    finally:
        await finish(agent)


async def test_disable_enable_skips_pending_and_disabled_windows(tmp_path):
    from clytie.memory.window import highwater

    agent, _, model = await make_agent(tmp_path)
    try:
        first = event(agent, "停用前尚未整理的虚构消息")
        agent.ledger.claim_inbound(first)
        agent.ledger.ignore(first.event_id)
        await agent.recollector.set_enabled(False)
        second = event(agent, "停用期间的虚构消息")
        agent.ledger.claim_inbound(second)
        agent.ledger.ignore(second.event_id)
        await agent.clock.advance_and_drain(30 * 60)
        assert not model.requests
        await agent.recollector.set_enabled(True)
        assert (
            agent.recollector.progress("owner_private")["inbound"]
            == highwater(agent.ledger)["inbound"]
        )
        row = agent.memory.store.db.execute(
            "SELECT data FROM recollect_progress WHERE conversation_id='!state'"
        ).fetchone()
        assert json.loads(row[0])["skipped"] == 1
        await agent.clock.advance_and_drain(30 * 60)
        assert not model.requests
    finally:
        await finish(agent)


async def test_disabled_state_survives_recollector_recreation(tmp_path):
    from clytie.memory.scheduler import Recollector

    agent, _, model = await make_agent(
        tmp_path, changes={"memory": {"recollect": {"enabled": False}}}
    )
    try:
        message = event(agent, "停用后的虚构内容")
        agent.ledger.claim_inbound(message)
        agent.ledger.ignore(message.event_id)
        agent.recollector.config["enabled"] = True
        restarted = Recollector(agent)
        await restarted.set_enabled(True)
        await restarted.process(message.conversation)
        await agent.clock.advance_and_drain(30 * 60)
        assert not model.requests
        assert restarted.progress("owner_private")["inbound"] > 0
    finally:
        await finish(agent)


async def test_disable_cancels_inflight_and_ignores_late_candidates(tmp_path, monkeypatch):
    import asyncio

    from clytie.sdk.types import RecollectItem

    agent, _, _ = await make_agent(tmp_path)
    entered, released = asyncio.Event(), asyncio.Event()
    captured = []
    original = agent.host.invoke

    async def invoke(cid, pid, point, key, *args, **kwargs):
        if point != "memory.consolidator":
            return await original(cid, pid, point, key, *args, **kwargs)
        captured.append(args[-1])
        entered.set()
        await released.wait()
        return [RecollectItem(kind="episode", content="迟到的虚构回想", evidence=[1])]

    monkeypatch.setattr(agent.host, "invoke", invoke)
    try:
        msg = event(agent, "虚构窗口")
        agent.ledger.claim_inbound(msg)
        agent.ledger.ignore(msg.event_id)
        agent.clock.advance(30 * 60)
        task = asyncio.create_task(agent.recollector.process(msg.conversation))
        await entered.wait()
        await agent.recollector.set_enabled(False)
        assert captured[0].cancelled
        released.set()
        await task
        assert not agent.memory.store.all_records()
        await agent.recollector.set_enabled(True)
        assert agent.recollector.progress("owner_private")["inbound"] > 0
    finally:
        released.set()
        await finish(agent)
