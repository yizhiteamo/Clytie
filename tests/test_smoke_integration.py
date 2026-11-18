"""Real smoke driver, loopback fake tester, B4b's agent fixture and local summary API."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from clytie.api.management import Management
from clytie.api.operations import companion_operation
from clytie.api.server import Server
from clytie.sdk.types import ApiResponse
from clytie.smoke.runner import run
from clytie.supervisor.common import Audit

from .test_agent import event, finish, make_agent
from .test_smoke_run import Scenario, save


async def test_real_driver_agent_and_authenticated_summary(tmp_path):
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
                msg = event(agent, text=scenario.text, mention=True, scene="public_group")
                trace = await agent.receive(msg)
                await agent.drain()
                assert agent.ledger.get("record", trace)["exec_status"] == "ok"
                sent.append(trace)
                reply = channel.sent[-1]
                await socket.send_json(
                    dict(
                        post_type="message",
                        message_type="group",
                        group_id=scenario.group,
                        user_id=scenario.bot,
                        message_id="out-" + reply.intent_id,
                        message=[{"type": "text", "data": {"text": "synthetic secret reply"}}],
                    )
                )
                data = {"message_id": msg.message.ref.platform_message_id}
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
                ws_url=str(fake.make_url("/").with_scheme("ws")),
                token_file="secrets/smoke.key",
            ),
        )
        save(
            smoke / "cases.yaml",
            {
                "cases": [
                    dict(
                        id="greet",
                        text=scenario.text,
                        timeout_s=20,
                        expect=dict(reply=True, image=False, reactions="none"),
                    )
                ]
            },
        )
        await api.start()
        try:
            private = await agent.receive(event(agent, text="private unrelated"))
            await agent.drain()
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
                for _ in range(100):
                    await agent.clock.advance_and_drain(0.1)
                    if task.done():
                        break
                assert task.done()
                report = await task
                assert report["result"] == "passed", report
                assert len(sent) == 1 and report["cases"][0]["reply_s"] >= 0
                async with http.get(url + path, params=query, headers=headers) as resp:
                    rows = (await resp.json())["result"]
                assert [r["trace_id"] for r in rows] == sent and rows[0]["trace_id"] != private
                digest = json.dumps(rows)
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
