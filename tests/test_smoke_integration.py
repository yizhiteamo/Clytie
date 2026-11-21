"""Real smoke driver, loopback fake tester, B4b's agent fixture and local summary API."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from clytie.api.management import Management
from clytie.api.operations import companion_operation
from clytie.api.server import Server
from clytie.sdk.behavior import Defer, Respond
from clytie.sdk.smoke import SmokeDriverError
from clytie.sdk.types import ApiResponse
from clytie.smoke.config import ConfiguredSecrets
from clytie.smoke.runner import load_driver, run
from clytie.smoke.summary import records
from clytie.supervisor.common import Audit

from .test_agent import event, finish, make_agent
from .test_smoke_run import Scenario, save


async def test_older_request_with_a_new_sent_receipt_is_visible(tmp_path, monkeypatch):
    agent, channel, _ = await make_agent(tmp_path)
    send = channel.send

    async def delayed_send(intent, cancel):
        agent.clock.advance(1)
        return await send(intent, cancel)

    monkeypatch.setattr(channel, "send", delayed_send)
    try:
        trace = await agent.receive(event(agent, mention=True, scene="public_group"))
        await agent.drain()
        since = agent.clock.now().isoformat()
        (row,) = records(agent.ledger, "test_group", since)
        assert row["trace_id"] == trace and row["created_at"] < since
        assert any(d["status"] == "sent" for d in row["deliveries"])
        agent.clock.advance(1)
        assert records(agent.ledger, "test_group", agent.clock.now().isoformat()) == []
    finally:
        await finish(agent)


@pytest.mark.parametrize("burst", [False, True], ids=["single", "merged"])
async def test_real_driver_agent_and_authenticated_summary(tmp_path, burst):
    scenario = Scenario(tmp_path, "passed")
    agent, channel, _ = await make_agent(tmp_path)
    runtime = SimpleNamespace(
        home=tmp_path, clock=agent.clock, cid=agent.cid, host=agent.host, agent=agent
    )

    async def transfer(cid, op, args, *, timeout):
        return await companion_operation(runtime, op, args)

    manager = object.__new__(Management)
    manager.report = SimpleNamespace(instance={"test_conversation": "test_group"})
    manager.svc = SimpleNamespace(
        companions=[agent.cid], rows={agent.cid: {"state": "running"}}, request=transfer
    )
    api = Server(tmp_path, agent.clock, manager, Audit(tmp_path, agent.clock), {"port": 0})
    sent = []
    incoming = []
    companion_sent = asyncio.Event()

    async def protocol(request):
        assert request.headers["Authorization"].endswith("synthetic-marker")
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for frame in socket:
            call = json.loads(frame.data)
            if call["action"] == "get_group_member_list":
                data = [{"user_id": a} for a in (scenario.bot, scenario.login)]
            elif call["action"] == "get_login_info":
                data = {"user_id": scenario.login}
            else:
                assert (
                    call["action"] == "send_group_msg"
                    and call["params"]["group_id"] == scenario.group
                )
                msg = event(
                    agent,
                    text=next(
                        p["data"]["text"] for p in call["params"]["message"] if p["type"] == "text"
                    ),
                    actor="tester",
                    role="member",
                    mention=any(p["type"] == "at" for p in call["params"]["message"]),
                    scene="public_group",
                )
                incoming.append(msg.event_id)
                await agent.receive(msg)
                if not burst or len(incoming) == 3:
                    if burst:
                        await companion_sent.wait()
                    await agent.drain()
                    row = agent.ledger.rows("record")[-1]
                    assert row["source_event_id"] == msg.event_id and row["exec_status"] == "ok"
                    sent.append(row["trace_id"])
                    reply = channel.sent[-1]
                    await socket.send_json(
                        dict(
                            post_type="message",
                            message_type="group",
                            group_id=scenario.group,
                            user_id=scenario.bot,
                            message_id="tester-view-" + reply.intent_id,
                            message=[{"type": "text", "data": {"text": "synthetic secret reply"}}],
                        )
                    )
                data = {"message_id": f"tester-incoming-{len(incoming)}"}
                assert data["message_id"] != msg.message.ref.platform_message_id
            await socket.send_json(
                {"echo": call["echo"], "status": "ok", "retcode": 0, "data": data}
            )
        return socket

    app = web.Application()
    app.router.add_get("/", protocol)
    async with TestServer(app) as fake:
        smoke = tmp_path / "smoke"
        save(
            smoke / "smoke.yaml",
            dict(
                tester="tester",
                companion="companion_a",
                driver={
                    "plugin": "qq-onebot",
                    "config": {
                        "ws_url": str(fake.make_url("/").with_scheme("ws")),
                        "token_file": "secrets/smoke.key",
                    },
                },
            ),
        )
        save(
            smoke / "cases.yaml",
            {
                "cases": [
                    dict(
                        id="greet",
                        **(
                            {"texts": [scenario.text, "second", "third"]}
                            if burst
                            else {"text": scenario.text}
                        ),
                        timeout_s=20,
                        expect=dict(reply=True, image=False, reactions="none"),
                    )
                ]
            },
        )
        await api.start()
        try:
            earlier = await agent.receive(
                event(agent, actor="tester", role="member", mention=True, scene="public_group")
            )
            await agent.drain()
            await agent.clock.advance_and_drain(1)
            private = await agent.receive(event(agent, text="private unrelated"))
            await agent.drain()
            if burst:
                behavior = agent.host.behavior
                send = channel.send

                async def observe_send(intent, cancel):
                    receipt = await send(intent, cancel)
                    companion_sent.set()
                    return receipt

                channel.send = observe_send

                async def defer_until_batch(cid, phase, args, **kwargs):
                    if phase == "attention":
                        return Respond() if len(args[1]) == 3 else Defer(seconds=1)
                    return await behavior(cid, phase, args, **kwargs)

                agent.host.behavior = defer_until_batch
            url = f"http://127.0.0.1:{api.port}/api/v1"
            path = "/companions/companion_a/smoke/records"
            async with aiohttp.ClientSession() as http:
                headers = {}
                headers["Authorization"] = "Bearer " + api.auth.token
                query = {"since": agent.clock.now().isoformat()}
                async with http.get(url + path, params=query) as res:
                    assert res.status == 401
                manager.report.instance.clear()
                async with http.get(url + path, params=query, headers=headers) as res:
                    assert (
                        res.status == 409
                        and (await res.json())["error"]["code"] == "no_test_conversation"
                    )
                manager.report.instance["test_conversation"] = "test_group"

                class LocalClient:
                    async def request(self, method, route, query=None):
                        if route.endswith("/status"):
                            return await scenario.request(method, route, query)
                        async with http.get(url + path, params=query, headers=headers) as resp:
                            return ApiResponse(status=resp.status, json=await resp.json())

                out = []
                task = asyncio.create_task(
                    run(
                        tmp_path,
                        clock=agent.clock,
                        client_type=lambda home: LocalClient(),
                        ask=lambda prompt: "y",
                        emit=out.append,
                    )
                )
                for _ in range(300):
                    await agent.clock.advance_and_drain(0.1)
                    if task.done():
                        break
                assert task.done()
                report = await task
                assert report["result"] == "passed", report
                assert len(sent) == 1 and report["cases"][0]["reply_s"] >= 0
                if burst:
                    assert len(incoming) == 3 and len(report["cases"][0]["replies"]) == 1
                    for key in incoming[:-1]:
                        row = agent.ledger.get("inbound", key)
                        assert row["state"] == "merged" and row["merged_into"] == sent[0]
                async with http.get(url + path, params=query, headers=headers) as resp:
                    rows = (await resp.json())["result"]
                assert [r["trace_id"] for r in rows] == sent and rows[0]["trace_id"] != private
                assert earlier and earlier not in sent
                digest = json.dumps(rows)
                assert rows[0]["sender_id"] == "tester"
                assert "platform_message_id" not in digest
                assert all(not delivery["status_reaction"] for delivery in rows[0]["deliveries"])
                for marker in (
                    scenario.text,
                    "private unrelated",
                    "synthetic secret reply",
                    scenario.bot,
                    scenario.group,
                    scenario.login,
                    "fictional private detail",
                ):
                    assert marker not in digest + repr(report) + repr(out)
                assert "platform_message_id" not in json.dumps(report)
        finally:
            await api.close()
            await finish(agent)


async def test_driver_refuses_redirect_before_sending_credentials_to_destination(tmp_path):
    scenario = Scenario(tmp_path, "passed")
    contacted = []

    async def redirect(request):
        raise web.HTTPFound("/destination")

    async def destination(request):
        contacted.append(True)
        return web.Response()

    app = web.Application()
    app.router.add_get("/", redirect)
    app.router.add_get("/destination", destination)
    driver = load_driver(tmp_path, "qq-onebot", scenario.clock)
    async with TestServer(app) as server:
        config = {
            "ws_url": str(server.make_url("/").with_scheme("ws")),
            "token_file": "secrets/smoke.key",
        }
        try:
            with pytest.raises(SmokeDriverError, match="ws_redirect"):
                await driver.connect(config, ConfiguredSecrets(tmp_path, config))
        finally:
            await driver.close()
    assert contacted == []
