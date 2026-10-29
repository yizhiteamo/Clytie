"""停用只管理插件操作，不取消调用方长期任务。"""

import asyncio

import pytest

from .host_helpers import SOURCE, enable, host, plugin
from .test_host_upgrades import MIGRATE, update

SERVICE = {
    "services": [
        {"name": "worker.call", "version": "1.0", "methods": ["get"], "handler": "service"}
    ]
}
WORKER = (
    SOURCE
    + """
    async def service(self, method, args):
        self.entered = True
        await self.ctx.clock.sleep(args.get("delay", 0))
        return {"ready": True}
    async def stop(self):
        self.stopped = True
        await self.ctx.clock.sleep(0)
"""
)


async def pump(clock, predicate):
    for _ in range(80):
        if predicate():
            return
        await clock.sleep(0)
    assert predicate()


async def test_completed_call_does_not_make_stop_wait_for_caller(tmp_path):
    plugin(tmp_path, "worker", WORKER, contributes=SERVICE)
    h = host(tmp_path)
    runtime = await enable(h, "worker")
    ready, release = asyncio.Event(), asyncio.Event()

    async def caller():
        await h.invoke("companion_a", "worker", "service", "worker.call", "get", {})
        ready.set()
        await release.wait()
        return "caller survived"

    task = asyncio.create_task(caller())
    await ready.wait()
    await h.apply(h.plan_disable("companion_a", "worker").plan_id)
    assert runtime.plugin.stopped and not task.done()
    assert h.clock.monotonic() == 0
    release.set()
    assert await task == "caller survived"


async def test_drain_timeout_always_calls_stop_and_does_not_cancel_caller_task(tmp_path):
    plugin(tmp_path, "worker", WORKER, contributes=SERVICE)
    h = host(tmp_path)
    runtime = await enable(h, "worker")

    async def caller():
        try:
            await h.invoke("companion_a", "worker", "service", "worker.call", "get", {"delay": 100})
        except asyncio.CancelledError:
            return "operation cancelled"

    caller_task = asyncio.create_task(caller())
    await pump(h.clock, lambda: getattr(runtime.plugin, "entered", False))
    stop = asyncio.create_task(h.apply(h.plan_disable("companion_a", "worker").plan_id))
    await pump(h.clock, lambda: h.clock.pending_sleep_count == 2)
    h.clock.advance(10)
    await stop
    assert await caller_task == "operation cancelled"
    assert not caller_task.cancelled() and runtime.plugin.stopped
    assert runtime.status == "disabled"


async def test_continuous_channel_feed_ends_without_cancelling_reader(tmp_path):
    source = (
        WORKER
        + """
    def create_channel(self):
        from clytie.sdk.testing import FakeChannel
        class Stream(FakeChannel):
            async def events(stream):
                await self.ctx.clock.sleep(100)
                yield None
        return Stream(self.ctx)
"""
    )
    from clytie.sdk.testing import FakeContext

    fake = FakeContext(root=tmp_path / "fake", channel=True)
    plugin(
        tmp_path,
        "stream",
        source,
        contributes={"channel": {"factory": "create_channel", "platform": "fictional"}},
    )
    h = host(tmp_path, backends=lambda cid, pid: {"identity": fake.identity})
    runtime = await enable(h, "stream")
    reg = h.registrations("companion_a", "channel")[0]

    async def reader():
        feed = await h.invoke("companion_a", "stream", "channel", reg.key, method="events")
        async for _ in feed:
            pass
        return "reader survived"

    task = asyncio.create_task(reader())
    await pump(h.clock, lambda: bool(runtime.feeds))
    await h.apply(h.plan_disable("companion_a", "stream").plan_id)
    assert await task == "reader survived"
    assert runtime.plugin.stopped and h.clock.monotonic() == 0


async def test_stop_cancel_finishes_cleanup_before_migration_snapshot(tmp_path):
    source = WORKER.replace(
        "self.stopped = True",
        'self.stopped = True\n        await self.ctx.storage.set("value", "fictional-stopped")\n        await self.ctx.clock.sleep(100)',
    )
    path = plugin(
        tmp_path, "worker", source, contributes=SERVICE, data={"version": 1, "on_uninstall": "keep"}
    )
    h = host(tmp_path)
    runtime = await enable(h, "worker")
    stopping = asyncio.create_task(h.apply(h.plan_disable("companion_a", "worker").plan_id))
    await pump(h.clock, lambda: h.clock.pending_sleep_count == 2)
    stopping.cancel()
    update(path, script=MIGRATE + '    raise ValueError("fictional-failure")\n', source=SOURCE)
    with pytest.raises(ValueError, match="disable"):
        h.plan_migration("companion_a", "worker")
    assert not (tmp_path / "backups").exists()
    h.clock.advance(10)
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert runtime.status == "disabled"
    plan = h.plan_migration("companion_a", "worker")
    assert (await h.apply_migration(plan.plan_id))["outcome"] == "migration_failed"
    assert await runtime.storage.get("value") == "fictional-stopped"


async def test_close_uses_loaded_manifest_if_source_removed(tmp_path):
    path = plugin(tmp_path, "worker", WORKER, contributes=SERVICE)
    h = host(tmp_path)
    runtime = await enable(h, "worker")
    path.rename(path.with_suffix(".retained"))
    h.refresh()
    await h.close()
    assert runtime.plugin.stopped


async def test_start_cleanup_resists_repeated_cancel_and_cleans_all_units(tmp_path):
    source = """class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        self.entered = True
        if ctx.config["wait"]:
            await ctx.clock.sleep(100)
    async def stop(self):
        self.stopping = True
        await self.ctx.clock.sleep(100)
"""
    plugin(
        tmp_path,
        "worker",
        source,
        config={"type": "object", "properties": {"wait": {"type": "boolean"}}},
    )
    h = host(tmp_path)
    h.unit_configs["companion_a", "worker"] = [
        {"role": None, "config": {"wait": False}},
        {"role": None, "config": {"wait": True}},
    ]
    task = asyncio.create_task(h.apply(h.plan_enable("companion_a", "worker").plan_id))

    def all_started():
        runtime = h.runtimes.get(("companion_a", "worker"))
        return (
            runtime
            and len(runtime.units) == 2
            and getattr(runtime.units[1].plugin, "entered", False)
        )

    await pump(h.clock, all_started)
    task.cancel()
    runtime = h.runtimes["companion_a", "worker"]
    await pump(h.clock, lambda: getattr(runtime.units[1].plugin, "stopping", False))
    task.cancel()
    await h.clock.sleep(0)
    h.clock.advance(10)
    await pump(h.clock, lambda: getattr(runtime.units[0].plugin, "stopping", False))
    task.cancel()
    await h.clock.sleep(0)
    h.clock.advance(10)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.status == "start_failed" and runtime.registrations == ()
    assert all(unit.plugin.stopping for unit in runtime.units)
    assert any(e.name == "plugin.disabled" and e.payload["plugin_id"] == "worker" for e in h.events)
    assert not h._version("companion_a", "worker")
