import asyncio
from types import SimpleNamespace

import pytest

from clytie.api.auth import APIError
from clytie.api.operations import companion_operation
from clytie.sdk.testing import MockClock

from .host_helpers import SOURCE, enable, host, plugin
from .test_supervisor import spin


async def test_real_plugin_route_mount_panel_and_unavailable(tmp_path):
    source = (
        SOURCE
        + """
    async def route(self, request):
        return {"status": 201, "json": {"method":request["method"], "value":"harmless"}}
"""
    )
    plugin(
        tmp_path,
        "api-example",
        source,
        contributes={
            "api_routes": [
                {
                    "method": "POST",
                    "path": "/plugins/api-example/update",
                    "handler": "route",
                    "audit": True,
                },
                {
                    "method": "GET",
                    "path": "/plugins/api-example/read",
                    "handler": "route",
                    "audit": False,
                },
            ],
            "webui_panels": [
                {
                    "id": "summary",
                    "kind": "kv",
                    "title": "Example",
                    "data_route": "/plugins/api-example/read",
                }
            ],
        },
    )
    h = host(tmp_path)
    try:
        await enable(h, "api-example")
        runtime = SimpleNamespace(home=tmp_path, clock=h.clock, cid="companion_a", host=h)
        args = {
            "pid": "api-example",
            "subpath": "update",
            "method": "POST",
            "query": {},
            "json": {},
        }
        assert await companion_operation(runtime, "plugin_route", args) == {
            "status": 201,
            "json": {"method": "POST", "value": "harmless"},
        }
        assert (await companion_operation(runtime, "panels", {}))[0]["id"] == "summary"
        await h.apply(h.plan_disable("companion_a", "api-example").plan_id)
        with pytest.raises(APIError, match="plugin_unavailable"):
            await companion_operation(runtime, "plugin_route", args)
    finally:
        await h.close()


@pytest.mark.parametrize(
    "value",
    [
        {"status": True, "json": {}},
        {"status": 204, "json": {}},
        {"status": 401, "json": {}},
        {"status": 200, "json": float("nan")},
        {"status": 200, "json": object()},
        {"status": 200, "json": "x" * (1024 * 1024 + 1)},
    ],
)
async def test_plugin_response_invalid_or_oversized(tmp_path, value):
    async def route(request):
        return value

    reg = SimpleNamespace(
        plugin_id="api-example",
        declaration={"path": "/plugins/api-example/read", "method": "GET"},
        implementation=route,
    )
    h = SimpleNamespace(registrations=lambda *_: [reg])
    runtime = SimpleNamespace(home=tmp_path, clock=MockClock(), cid="companion_a", host=h)
    with pytest.raises(APIError) as exc:
        await companion_operation(
            runtime,
            "plugin_route",
            {"pid": "api-example", "subpath": "read", "method": "GET", "query": {}, "json": {}},
        )
    assert exc.value.code in {"plugin_invalid_response", "plugin_response_too_large"}


async def test_plugin_timeout_uses_clock(tmp_path):
    clock = MockClock()

    async def route(request):
        await clock.sleep(100)

    reg = SimpleNamespace(
        plugin_id="api-example",
        declaration={"path": "/plugins/api-example/read", "method": "GET"},
        implementation=route,
    )
    runtime = SimpleNamespace(
        home=tmp_path,
        clock=clock,
        cid="companion_a",
        host=SimpleNamespace(registrations=lambda *_: [reg]),
    )
    task = asyncio.create_task(
        companion_operation(
            runtime,
            "plugin_route",
            {"pid": "api-example", "subpath": "read", "method": "GET", "query": {}, "json": {}},
        )
    )
    await spin()
    clock.advance(10)
    await spin()
    with pytest.raises(APIError, match="plugin_timeout"):
        await task


# Spawned interpreters exercise the same instance exclusion table and OS locks.
def _uninstall_worker(home, cid, target, ready, go, result):
    import asyncio
    from pathlib import Path

    from clytie.clock import SystemClock
    from clytie.plugin_host import PluginHost
    from clytie.plugin_host.host import PlanStale

    from .host_helpers import ROOT

    async def run():
        h = PluginHost(ROOT, Path(home), clock=SystemClock())
        h.configure(cid)
        plan = h.plan_uninstall(cid, target)
        ready.set()
        await asyncio.to_thread(go.wait, 10)
        try:
            for _ in range(3):
                try:
                    await h.apply(plan.plan_id)
                    result.put("ok")
                    return
                except PlanStale:
                    h.refresh()
                    plan = h.plan_uninstall(cid, target)
            result.put("stale")
        finally:
            await h.close()

    asyncio.run(run())


def test_cross_process_exclusion_updates_do_not_lose_writes(tmp_path, monkeypatch):
    import multiprocessing
    from pathlib import Path

    from clytie.supervisor.common import read

    # spawn imports the target by module name, outside pytest's importlib loader.
    # Give that fresh interpreter the explicit location of this fictional fixture.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))

    plugin(tmp_path, "optional-a")
    plugin(tmp_path, "optional-b")
    ctx = multiprocessing.get_context("spawn")
    go = ctx.Event()
    results = ctx.Queue()
    ready = [ctx.Event(), ctx.Event()]
    children = [
        ctx.Process(
            target=_uninstall_worker,
            args=(str(tmp_path), f"companion_{i}", pid, ready[i], go, results),
        )
        for i, pid in enumerate(("optional-a", "optional-b"))
    ]
    try:
        for child in children:
            child.start()
        assert all(event.wait(15) for event in ready)
        go.set()
        for child in children:
            child.join(20)
            assert child.exitcode == 0
        assert [results.get(timeout=2) for _ in children] == ["ok", "ok"]
        assert read(tmp_path / "plugins/.host/uninstalled.json") == ["optional-a", "optional-b"]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)
        results.close()
