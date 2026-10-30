"""A2：计划确认、按伴侣状态、故障与服务不可用，仅手写测试插件。"""

import asyncio
import json

import pytest

from clytie.plugin_host import CORE_MODULES, PlanStale
from clytie.sdk.context import ServiceUnavailable
from clytie.sdk.testing import checkpoint
from clytie.sdk.testing.samples import sample_event

from .host_helpers import SOURCE, enable, host, plugin


async def test_cascade_plan_is_read_only_confirmed_stale_and_restorable(tmp_path):
    plugin(tmp_path, "target")
    plugin(tmp_path, "middle", dependencies={"target": {"version": ">=1"}})
    plugin(tmp_path, "leaf", dependencies={"middle": {"version": ">=1"}})
    h = host(tmp_path)
    await enable(h, "leaf")
    plan = h.plan_disable("companion_a", "target")
    assert plan.steps == ("leaf", "middle", "target")
    assert h._active("companion_a") == ["leaf", "middle", "target"]
    assert not h._metadata("companion_a", "cascades.json").exists()
    assert h.plan_disable("companion_a", "target").plan_id == plan.plan_id
    h.configure("companion_b")
    await enable(h, "target", "companion_b")
    result = await h.apply(plan.plan_id)
    assert result["stopped"] == ["leaf", "middle", "target"]
    assert h.is_active("companion_b", "target")
    records = json.loads(h._metadata("companion_a", "cascades.json").read_text(encoding="utf-8"))
    assert [r["plugin"] for r in records] == ["leaf", "middle"]
    alone = h.plan_enable("companion_a", "target")
    assert alone.previously_cascaded == ("leaf", "middle") and alone.steps == ("target",)
    together = h.plan_enable("companion_a", "target", restore_cascaded=True)
    assert together.steps == ("target", "middle", "leaf")
    await h.apply(together.plan_id)
    with pytest.raises(PlanStale):
        await h.apply(alone.plan_id)
    await h.apply(h.plan_disable("companion_a", "target").plan_id)
    await enable(h, "target")
    await h.apply(h.plan_disable("companion_a", "target").plan_id)
    assert h.plan_enable("companion_a", "target").previously_cascaded == ()
    await h.close()


async def test_external_manifest_dependency_change_invalidates_confirmed_plan(tmp_path):
    path = plugin(tmp_path, "target")
    h = host(tmp_path)
    plan = h.plan_enable("companion_a", "target")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["dependencies"] = {"missing": {"version": ">=1"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PlanStale):
        await h.apply(plan.plan_id)
    assert not h.is_active("companion_a", "target")


@pytest.mark.parametrize("core", sorted(CORE_MODULES))
def test_core_cannot_be_disabled_or_uninstalled(tmp_path, core):
    h = host(tmp_path)
    assert not h.plan_disable("companion_a", core).ok
    assert not h.plan_uninstall("companion_a", core).ok


async def test_optional_dependency_event_and_dynamic_service_proxy(tmp_path):
    provider = (
        SOURCE
        + """
    async def service(self, method, args):
        await self.ctx.clock.sleep(0)
        return {"ready": True}
"""
    )
    plugin(
        tmp_path,
        "provider",
        provider,
        contributes={
            "services": [
                {
                    "name": "example.state",
                    "version": "1.0",
                    "methods": ["get"],
                    "handler": "service",
                }
            ]
        },
    )
    plugin(
        tmp_path,
        "consumer",
        dependencies={"provider": {"version": ">=1", "optional": True}},
        contributes={"events": {"subscribe": ["plugin.*"], "handler": "on_event"}},
    )
    h = host(tmp_path)
    consumer = await enable(h, "consumer")
    assert consumer.ctx.services.get("example.state") is None
    assert consumer.ctx.services.get("not.declared") is None
    await enable(h, "provider")
    proxy = consumer.ctx.services.get("example.state")
    assert await proxy.call("get", {}) == {"ready": True}
    with pytest.raises(ValueError):
        await proxy.call("not_declared", {})
    await h.apply(h.plan_disable("companion_a", "provider").plan_id)
    await h.event_bus.drain()
    assert h.is_active("companion_a", "consumer")
    assert any(
        e.name == "plugin.disabled" and e.payload["plugin_id"] == "provider"
        for e in consumer.plugin.seen
    )
    assert consumer.ctx.services.get("example.state") is None
    with pytest.raises(ServiceUnavailable):
        await proxy.call("get", {})
    await h.close()


async def test_start_failure_never_assembles_and_dependents_do_not_start(tmp_path):
    source = """
class Plugin:
    async def start(self, ctx):
        raise ValueError("fictional-sensitive-body")
    def create_model(self):
        raise AssertionError("factory must not execute")
    async def stop(self):
        pass
"""
    plugin(tmp_path, "failed", source, contributes={"model": {"factory": "create_model"}})
    plugin(tmp_path, "dependent", dependencies={"failed": {"version": ">=1"}})
    h = host(tmp_path)
    result = await h.apply(h.plan_enable("companion_a", "dependent").plan_id)
    assert result["failed"] == ["failed", "dependent"]
    assert h.registrations("companion_a") == ()
    assert "fictional-sensitive-body" not in json.dumps(h.log.entries)


async def test_behavior_exception_uses_default_and_other_plugin_runs(tmp_path):
    bad = (
        SOURCE
        + """
    async def before_attention(self, input):
        raise RuntimeError("fictional-sensitive-body")
"""
    )
    good = (
        SOURCE
        + """
    async def before_attention(self, input):
        from clytie.sdk.behavior import Normal
        self.seen.append(input)
        return Normal()
"""
    )
    contrib = {"behavior": {"before_attention": {"handler": "before_attention"}}}
    plugin(
        tmp_path,
        "bad",
        bad,
        contributes=contrib,
        failure_default={"before_attention": {"type": "react_only", "emoji": "fictional"}},
        permissions=["message.react"],
    )
    plugin(tmp_path, "good", good, contributes=contrib)
    h = host(tmp_path)
    await enable(h, "bad")
    healthy = await enable(h, "good")
    event = sample_event(h.clock)
    result = await h.behavior(
        "companion_a", "before_attention", (event, h.companion_state("companion_a"))
    )
    assert result.type == "react_only" and len(healthy.plugin.seen) == 1
    assert h.is_active("companion_a", "bad") and h.failures
    await h.close()


async def test_behavior_timeout_uses_mock_clock_and_external_cancel_propagates(tmp_path):
    source = (
        SOURCE
        + """
    async def before_attention(self, input):
        await self.ctx.clock.sleep(30)
"""
    )
    plugin(
        tmp_path,
        "slow",
        source,
        contributes={"behavior": {"before_attention": {"handler": "before_attention"}}},
    )
    h = host(tmp_path)
    await enable(h, "slow")
    args = (sample_event(h.clock), h.companion_state("companion_a"))
    task = asyncio.create_task(h.behavior("companion_a", "before_attention", args))
    for _ in range(8):
        await checkpoint()
    h.clock.advance(2)
    assert await task is None
    task = asyncio.create_task(h.behavior("companion_a", "before_attention", args))
    for _ in range(8):
        await checkpoint()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await h.close()


async def test_hot_reload_schema_rejection_restart_hint_and_state_preservation(tmp_path):
    h = host(tmp_path)
    runtime = await enable(h, "example-behavior")
    await runtime.ctx.storage.set("ticks", 7)
    assert (await h.reconfigure("companion_a", "example-behavior", {"quiet": "invalid"}))[
        "outcome"
    ] == "rejected"
    assert h.runtimes["companion_a", "example-behavior"] is runtime
    assert (await h.reconfigure("companion_a", "example-behavior", {"quiet": True}))[
        "outcome"
    ] == "applied"
    changed = h.runtimes["companion_a", "example-behavior"]
    assert changed is not runtime and changed.plugin.ticks == 7
    assert changed.ctx.config["quiet"] is True
    with pytest.raises(AttributeError):
        changed.ctx.config = {}
    await enable(h, "echo-tool")
    assert (await h.reconfigure("companion_a", "echo-tool", {}))["outcome"] == "restart_required"
    await h.close()
