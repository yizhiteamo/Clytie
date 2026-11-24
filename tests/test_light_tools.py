"""Light invocations retain their ledger result without claiming a task."""

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clytie.sdk.types import ToolCall

from .host_helpers import host
from .test_kernel_service import Capability, new_record, reply, setup


def test_manifest_light_and_public_network_are_independent_restrictions():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/plugin.schema.json").read_text("utf-8"))
    manifest = json.loads((root / "plugins/echo-tool/plugin.json").read_text("utf-8"))
    validate = Draft202012Validator(schema).is_valid
    tool = manifest["contributes"]["tools"][0]
    tool.update(light=True, heavy=False)
    manifest["permissions"] = ["network.public"]
    assert validate(manifest)
    tool["heavy"] = True
    assert not validate(manifest)
    tool["heavy"] = False
    manifest["permissions"].append("network")
    assert not validate(manifest)


async def test_invalid_web_backend_cannot_start_and_reports_status(tmp_path):
    registry = host(tmp_path)
    registry.configure(
        "companion_a",
        plugins={
            "web-tools": {
                "config": {
                    "backend": "fictional-unsupported",
                    "model": "fictional",
                    "credential": "fake",
                }
            }
        },
    )
    try:
        plan = registry.plan_enable("companion_a", "web-tools")
        result = await registry.apply(plan.plan_id)
        assert result["failed"]
        assert not registry.is_active("companion_a", "web-tools")
        assert registry.runtimes["companion_a", "web-tools"].status == "start_failed"
    finally:
        await registry.close()


@pytest.mark.parametrize("follow_with_ordinary", [False, True])
async def test_light_invocation_only_upgrades_for_ordinary_tool(tmp_path, follow_with_ordinary):
    kernel, channel, clock = setup(tmp_path)
    row, invocation, _ = await new_record(kernel)
    trace = row["trace_id"]
    capability = Capability(clock)
    call = ToolCall(call_id="lookup", tool="lookup", arguments={})
    try:
        result = await kernel.execute(trace, "example", call, capability, invocation, light=True)
        assert result.status == "ok"
        duplicate = await kernel.execute(trace, "example", call, capability, invocation, light=True)
        assert duplicate["result"]["content"] == result.content
        audits = [a for a in kernel.ledger.rows("audit") if a["kind"] == "tool.light"]
        assert len(audits) == 1 and audits[0]["trace_id"] == trace
        assert capability.executed == 1
        assert kernel.ledger.get("record", trace)["task_id"] is None
        assert not channel.calls
        if follow_with_ordinary:
            ordinary = ToolCall(call_id="make", tool="make", arguments={})
            await kernel.execute(trace, "example", ordinary, capability, invocation)
        row = kernel.ledger.get("record", trace)
        assert bool(row["task_id"]) is follow_with_ordinary
        await kernel.seal(trace, "ok", [reply(row)])
        await kernel.delivery.deliver(trace)
        await clock.advance_and_drain(kernel.reactions.interval)
        await kernel.reactions.drain()
        await kernel.bus.drain()
        assert bool(channel.calls) is follow_with_ordinary
        assert [event.name for event in kernel.events].count("task.started") == int(
            follow_with_ordinary
        )
        assert len(channel.text) == 1
    finally:
        kernel.ledger.close()


async def test_light_result_with_assets_rejected_before_persistence(tmp_path):
    from clytie.sdk.types import AssetRef

    class WithAsset(Capability):
        async def execute(self, plan, inv, cancel):
            result = await super().execute(plan, inv, cancel)
            asset = AssetRef(asset_id="f" * 64, media_type="image/png", size=4, origin="example")
            return result.model_copy(update={"assets": [asset]})

    kernel, _, clock = setup(tmp_path)
    try:
        row, invocation, _ = await new_record(kernel)
        result = await kernel.execute(
            row["trace_id"],
            "example",
            ToolCall(tool="lookup", call_id="call", arguments={}),
            WithAsset(clock),
            invocation,
            light=True,
        )
        assert result.status == "error"
        assert not result.assets
        (stored,) = kernel.ledger.rows("invocation")
        assert stored["status"] == "error" and stored["asset_ids"] == []
    finally:
        kernel.ledger.close()


async def test_light_invocation_cancelled_with_record(tmp_path):
    kernel, channel, clock = setup(tmp_path)
    try:
        row, inv, event = await new_record(kernel, role="member")
        work = asyncio.create_task(
            kernel.execute(
                row["trace_id"],
                "example",
                ToolCall(call_id="call", tool="lookup", arguments={}),
                Capability(clock, block=True),
                inv,
                light=True,
            )
        )
        await clock.drain()
        assert (
            await kernel.cancel("companion_a", inv.actor, "test_group", reply_to=event.message.ref)
            == "cancelled"
        )
        assert (await work).status == "cancelled"
        assert kernel.ledger.get("record", row["trace_id"])["task_id"] is None
        assert not channel.calls
    finally:
        await kernel.bus.drain()
        kernel.ledger.close()
