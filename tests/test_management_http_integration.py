"""Real HTTP, supervisor, child and synchronously blocked importer; virtual stop deadlines."""

import asyncio
import json
from uuid import uuid4

import pytest
from aiohttp import ClientSession

from clytie.api.management import Management
from clytie.api.management_journal import Journal, executor_alive
from clytie.api.server import Server
from clytie.config import load_home
from clytie.sdk.testing import MockClock
from clytie.supervisor.entry import FrontendCommands
from clytie.supervisor.processes import ProcessLauncher
from clytie.supervisor.service import Supervisor

from .management_integration_helpers import (
    arguments,
    close_exited_launcher,
    home_with_importer,
    until,
)
from .test_management_start_integration import memory_owner


@pytest.mark.parametrize("stop_source", ["cancel", "wait_escalate", "window", "eof"])
async def test_real_sync_importer_http_disconnect_query_and_parallel_stop(tmp_path, stop_source):
    home, source = home_with_importer(tmp_path)
    clock = MockClock()
    launcher = ProcessLauncher(home)
    svc = Supervisor(home, ["companion_a", "companion_b"], launcher, clock)
    api = Management(svc, load_home(home))
    server = Server(home, clock, api, svc.audit, {"port": 0})
    svc.finalize = server.close
    control = FrontendCommands(svc)

    # Same post-start wiring as daemon_main; only the orchestration clock is
    # injected. Neither HTTP handlers, workers, child IPC nor stop is stubbed.
    def request_stop(mode):
        svc.stopping = api.memory_workers.stopping = True
        if control.stopping is None:
            control.stopping = asyncio.create_task(svc.stop(mode, source="internal"))

    svc.request_stop = request_stop
    key = uuid4().hex
    path = home / "companions/companion_a/management/operations" / (key + ".json")
    eof = None
    pending = None
    client = None
    try:
        await svc.start()
        await until(lambda: all(r["state"] == "running" for r in svc.rows.values()))
        await server.start()
        base = f"http://127.0.0.1:{server.port}/api/v1"
        headers = {}
        headers["Authorization"] = "Bearer " + server.auth.token
        async with ClientSession(headers=headers) as observer:

            async def post(route, data=None):
                async with observer.post(base + route, json=data or {}) as response:
                    return response.status, await response.json()

            async def get(route):
                async with observer.get(base + route) as response:
                    assert response.status == 200
                    return (await response.json())["result"]

            status, body = await post("/companions/companion_a/stop", {"confirm": True})
            assert status == 200 and body["result"]["state"] == "stopped"
            other = svc.children["companion_b"]
            assert other.process.poll() is None
            client = ClientSession(headers=headers)
            pending = asyncio.create_task(
                client.post(
                    base + "/companions/companion_a/memory/import/plan",
                    json={**arguments(source), "operation_id": key},
                )
            )
            await until(lambda: (source.parent / "entered").exists())
            journal = Journal.load(path, clock)
            identity = journal.value["executor"]
            assert identity["pid"] == int((source.parent / "entered").read_text().strip())
            assert memory_owner(home)["pid"] == identity["pid"]
            assert identity != other.identity()
            route = "/companions/companion_a/memory/operations/" + key
            assert (await get(route))["state"] == "preparing"
            # Disconnect the actual HTTP transport, not the business task.
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await client.close()
            assert key in api.memory_workers.tasks
            assert not journal.cancel_path.exists()
            status, duplicate = await post(
                "/companions/companion_a/memory/import/plan",
                {**arguments(source), "operation_id": key},
            )
            assert status == 200 and duplicate["result"]["state"] == "preparing"
            status, busy = await post(
                "/companions/companion_a/memory/import/plan", arguments(source)
            )
            assert status == 409 and busy["error"]["code"] == "management_busy"

            if stop_source in {"cancel", "wait_escalate"}:
                status, _ = await post(
                    "/supervisor/stop",
                    {
                        "confirm": True,
                        "task_mode": "wait" if stop_source == "wait_escalate" else "cancel",
                    },
                )
                assert status == 200
            elif stop_source == "window":
                await control.receive({"type": "stop", "source": "close", "seq": 1})
            else:
                eof = asyncio.create_task(control.receive(None))
            await until(lambda: svc.rows["companion_b"]["state"] == "stopped")
            assert other.process.poll() == 0
            assert svc.stop_results["companion_b"]["state"] == "stopped"
            assert not control.stopping.done()
            assert executor_alive(identity) is True
            if stop_source == "wait_escalate":
                clock.advance(121)
                await clock.drain()
                assert api.memory_workers.cancel_since is None
                assert not journal.cancel_path.exists()
                assert not (await get("/status"))["management"]["still_finishing"]
                assert (await post("/supervisor/escalate"))[0] == 200
            await until(lambda: api.memory_workers.cancel_since is not None)
            since = api.memory_workers.cancel_since
            assert journal.cancel_path.exists()
            clock.advance(119)
            await clock.drain()
            assert not (await get("/status"))["management"]["still_finishing"]
            assert (await post("/supervisor/stop", {"confirm": True, "task_mode": "cancel"}))[
                0
            ] == 200
            assert (await post("/supervisor/escalate"))[0] == 200
            assert api.memory_workers.cancel_since == since
            clock.advance(2)
            await clock.drain()
            status = await get("/status")
            assert status["management"]["still_finishing"]
            assert not control.stopping.done() and executor_alive(identity) is True
            assert (await get(route))["state"] == "preparing"
            rejected, body = await post(
                "/companions/companion_a/memory/import/plan", arguments(source)
            )
            assert rejected == 409 and body["error"]["code"] == "supervisor_stopping"
            assert len((source.parent / "entered").read_text().splitlines()) == 1

            (source.parent / "release").touch()
            await until(lambda: control.stopping.done())
            result = await control.stopping
            assert result["companion_b"]["state"] == "stopped"
            if eof:
                assert await eof is True
            assert Journal.load(path, clock).value["state"] == "cancelled"
            assert not Journal.load(path, clock).value["committed"]
            assert not api.memory_workers.tasks and executor_alive(identity) is False
            assert server.runner is None
            assert not list((home / "run/memory-plans").glob("*.json"))
            # The process exited because the barrier opened, without advancing
            # the virtual clock further or waiting another fixed grace period.
            assert clock.monotonic() - since == 121
        # Fresh supervisor and real startup: interrupted work is not replayed.
        fresh = Supervisor(home, ["companion_a"], launcher, MockClock())
        fresh_api = Management(fresh, load_home(home))
        fresh_server = Server(home, fresh.clock, fresh_api, fresh.audit, {"port": 0})
        fresh.finalize = fresh_server.close
        try:
            await fresh.start()
            await until(lambda: fresh.rows["companion_a"]["state"] == "running")
            await fresh_server.start()
            headers["Authorization"] = "Bearer " + fresh_server.auth.token
            async with ClientSession(headers=headers) as restarted_client:
                async with restarted_client.get(
                    f"http://127.0.0.1:{fresh_server.port}/api/v1" + route
                ) as response:
                    assert response.status == 200
                    saved = (await response.json())["result"]
                    assert saved["state"] == "cancelled" and not saved["committed"]
                    assert "fictional memory" not in json.dumps(saved)
            assert len((source.parent / "entered").read_text().splitlines()) == 1
            assert Journal.load(path, clock).value["state"] == "cancelled"
        finally:
            await fresh.stop("cancel")
    finally:
        (source.parent / "release").touch()
        if client:
            await client.close()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        if svc.lock:
            await svc.stop("cancel")
        if eof:
            await asyncio.gather(eof, return_exceptions=True)
        await server.close()
        for child in launcher.children:
            await child.kill()
        close_exited_launcher(launcher)
