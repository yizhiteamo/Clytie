import argparse
import asyncio
from types import SimpleNamespace

from clytie.api.cli import add_parser, execute
from clytie.api.operations import companion_operation
from clytie.api.server import Server
from clytie.plugin_host.locking import file_lock
from clytie.supervisor.common import Audit

from .test_api_management import management
from .test_appearance_api import request

CID = "companion_a"
ROUTE = "/api/v1/companions/" + CID + "/proactive"
OFF = {"paused": False, "since": None}


def server(home):
    backend = management(home)
    backend.svc.notifications = SimpleNamespace(undelivered=0)
    app = Server(home, backend.clock, backend, Audit(home, backend.clock))
    app.port = 8765
    return app


async def status(app, cid=CID):
    response = await request(app, "GET", "/api/v1/status")
    assert response.status == 200
    return response.json["result"]["companions"][cid]["proactive"]


async def test_stopped_pause_persists_per_companion_and_preserves_since(tmp_path):
    app = server(tmp_path)
    assert await status(app) == OFF
    since = app.backend.clock.now().isoformat()
    on = {"paused": True, "since": since}
    assert (await request(app, "POST", ROUTE, {"paused": True})).json["result"] == on
    app.backend.clock.advance(30)
    assert (await request(app, "POST", ROUTE, {"paused": True})).json["result"] == on
    assert await status(app, "companion_b") == OFF
    restarted = server(tmp_path)
    assert await status(restarted) == on
    assert (await request(restarted, "POST", ROUTE, {"paused": False})).json["result"] == OFF
    assert await status(server(tmp_path)) == OFF


async def test_online_pause_runs_in_the_owning_companion(tmp_path):
    from clytie.agent.proactive_pause import ProactivePause

    app = server(tmp_path)
    backend = app.backend
    backend.svc.rows[CID].update(state="running", phase="running")
    pause = ProactivePause(tmp_path, CID, backend.clock)
    child = SimpleNamespace(
        home=tmp_path,
        clock=backend.clock,
        cid=CID,
        host=None,
        agent=SimpleNamespace(proactive_pause=pause),
    )
    calls = []

    async def forward(cid, operation, args, *, timeout):
        calls.append((cid, operation, args, timeout))
        return await companion_operation(child, operation, args)

    backend.svc.request = forward
    value = (await request(app, "POST", ROUTE, {"paused": True})).json["result"]
    assert calls == [(CID, "proactive", {"paused": True}, 30)]
    assert value == await pause.status() == await status(app)
    assert value["paused"] is True
    assert await ProactivePause(tmp_path, CID, backend.clock).status() == value
    assert (await request(app, "POST", ROUTE, {"paused": False})).json["result"] == OFF
    assert await pause.status() == await status(app) == OFF


async def test_proactive_rejects_invalid_input_and_transitions(tmp_path):
    app = server(tmp_path)
    for data in ({}, {"paused": "false"}, {"paused": 1}, {"paused": True, "other": True}):
        result = await request(app, "POST", ROUTE, data)
        assert result.status == 400
    assert (await request(app, "POST", ROUTE, {"paused": True}, bearer=False)).status == 401
    for state, phase in (
        ("running", "migrating"),
        ("starting", "starting"),
        ("stopping", "stopping"),
    ):
        app.backend.svc.rows[CID].update(state=state, phase=phase)
        result = await request(app, "POST", ROUTE, {"paused": True})
        assert (result.status, result.json["error"]["code"]) == (409, "companion_busy")
    app.backend.svc.rows[CID].update(state="blocked", phase="starting")
    with file_lock(tmp_path / "companions" / CID / "ledger/.lock", {"operation": "ledger"}):
        result = await request(app, "POST", ROUTE, {"paused": True})
        assert (result.status, result.json["error"]["code"]) == (409, "companion_busy")
    assert await status(app) == OFF


async def test_pause_waits_for_in_progress_start_before_selecting_its_writer(tmp_path):
    app = server(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def start(cid):
        entered.set()
        await release.wait()
        app.backend.svc.rows[cid].update(state="starting", phase="starting")

    app.backend.svc.start_companion = start
    starting = asyncio.create_task(request(app, "POST", ROUTE.replace("proactive", "start"), {}))
    await entered.wait()
    mutation = asyncio.create_task(request(app, "POST", ROUTE, {"paused": True}))
    try:
        await app.backend.clock.sleep(0)
        assert not mutation.done()
    finally:
        release.set()
        await starting
    assert (await mutation).json["error"]["code"] == "companion_busy"
    assert await status(app) == OFF


async def test_companion_proactive_cli_uses_only_the_management_api(tmp_path):
    app = server(tmp_path)
    calls = []

    class Transport:
        async def request(self, method, path, *, json=None, query=None):
            calls.append((method, path, json))
            return await request(app, method, path, json)

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))

    async def run(action, cid=CID):
        args = parser.parse_args(["companion", "proactive", cid, action, "--json"])
        return await execute(args, Transport())

    assert (await run("pause")).json["result"]["paused"] is True
    calls.clear()
    assert (await run("status")).json["result"] == await status(app)
    assert calls and all(call == ("GET", "/api/v1/status", None) for call in calls)
    assert (await run("resume")).json["result"] == OFF
    assert calls[-1] == ("POST", ROUTE, {"paused": False})
    assert (await run("status", "missing")).status == 404
