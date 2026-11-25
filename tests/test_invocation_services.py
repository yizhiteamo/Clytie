import asyncio
import json
from dataclasses import asdict

import pytest

from clytie.sdk import CancelToken
from clytie.sdk.testing.samples import sample_request
from clytie.sdk.types import AssetRef, Invocation, Plan, ToolResult

from .host_helpers import ROOT, SOURCE, enable, plugin
from .test_agent import event, finish, make_agent
from .test_api_management import management
from .test_appearance_requests import add_references, set_description

SOURCE_WITH_TOOL = (
    SOURCE
    + """
    def tool(self):
        return self
    def idempotency_key(self, call, inv):
        return call.call_id
    async def prepare(self, call, inv):
        from clytie.sdk.types import Plan
        return Plan(summary="fictional", heavy=True, data={"call_id": call.call_id})
    async def execute(self, plan, inv, cancel):
        return await self.run_tool(plan, inv, cancel)
    async def service(self, method, args, call):
        return await self.handle(method, args, call)
"""
)


async def assembled(tmp_path, *turns):
    declaration = json.loads((ROOT / "plugins/echo-tool/plugin.json").read_text("utf-8"))[
        "contributes"
    ]["tools"][0]
    provider = dict(declaration, name="render_fixture", factory="tool", heavy=True)
    plugin(
        tmp_path,
        "renderer",
        SOURCE_WITH_TOOL,
        permissions=["assets.write", "model.invoke"],
        contributes={
            "tools": [provider],
            "services": [
                {
                    "name": "fixture.render",
                    "methods": ["render"],
                    "version": "1.0",
                    "context": True,
                    "scope_as": "render_fixture",
                    "handler": "service",
                }
            ],
        },
    )
    plugin(
        tmp_path,
        "composer",
        SOURCE_WITH_TOOL,
        permissions=["appearance.read"],
        dependencies={"renderer": {"version": ">=1"}},
        contributes={
            "tools": [dict(declaration, name="compose_fixture", factory="tool", heavy=True)]
        },
    )
    agent, channel, _ = await make_agent(
        tmp_path,
        *turns,
        changes={
            "plugins": {"renderer": {"enabled": True}, "composer": {"enabled": True}},
            "permissions": {"owner": ["*"], "member": ["compose_fixture", "render_fixture"]},
        },
    )
    plan = agent.host.plan_enable(agent.cid, "composer")
    assert plan.appearance_readers == ("composer",)
    caller = (await enable(agent.host, "composer")).plugin
    return agent, channel, caller, agent.host.runtimes[agent.cid, "renderer"].plugin


async def test_enable_preview_requires_appearance_permission_confirmation(tmp_path):
    agent, _, _, _ = await assembled(tmp_path)
    try:
        plan = next(plan for plan in agent.host._plans.values() if plan.appearance_readers)
        backend = management(tmp_path / "manager")
        backend.plans[plan.plan_id] = asdict(plan)
        confirm, _, description = await backend.policy(
            "/companions/<cid>/plugins/apply", {"cid": agent.cid}, {"plan_id": plan.plan_id}
        )
        assert confirm and "composer" in description and "appearance.read" in description
        assert "参考图会随画图请求发给画图服务商" in description
    finally:
        await finish(agent)


async def test_scoped_service_appearance_and_usage_share_tool_record(tmp_path):
    add_references(tmp_path)
    set_description(tmp_path, "fictional appearance")
    turns = {
        "finish": "tool_calls",
        "tool_calls": [
            {"call_id": "compose", "tool": "compose_fixture", "arguments": {"text": "fictional"}}
        ],
    }
    agent, channel, caller, provider = await assembled(tmp_path, turns)
    observed = {}
    service_returned = asyncio.Event()

    async def handle(method, args, call):
        ref = AssetRef.model_validate(args["source"])
        observed.update(provider=ref.asset_id, call=call)
        assert call.cancel is observed["cancel"]
        turn = await provider.ctx.model.generate(
            sample_request(agent.clock), CancelToken(), purpose="fixture"
        )
        assert turn.finish == "stop"
        assert (await provider.ctx.assets.read(ref.asset_id)).startswith(b"\x89PNG")
        with pytest.raises(PermissionError):
            await provider.ctx.assets.path(ref.asset_id)

        async def inherited_context():
            await service_returned.wait()
            with pytest.raises(KeyError):
                await provider.ctx.assets.read(ref.asset_id)

        observed["inherited"] = asyncio.create_task(inherited_context())
        asset = await provider.ctx.asset_writer.put(b"fictional product", "image/png", "renderer")
        observed["product"] = asset
        return {
            "asset": asset.model_dump(),
            "usage": {
                "input_tokens": 5,
                "output_tokens": True,
                "total_tokens": -3,
                "unexpected": "fictional",
            },
        }

    async def compose(plan, inv, cancel):
        profile = await caller.ctx.appearance.current()
        assert profile.description == "fictional appearance"
        assert len(profile.references) == 2
        actual = [r for r in agent.appearance.references if r.state == "current"]
        for alias, entry in zip(profile.references, actual):
            assert await caller.ctx.assets.read(
                alias.asset_id
            ) == agent.host.asset_store.read_resolved(entry.resolved)
            assert alias.asset_id != entry.asset.asset_id
            with pytest.raises(KeyError):
                await caller.ctx.assets.read(entry.asset.asset_id)
        observed.update(caller=profile.references[0].asset_id, cancel=cancel)
        service = caller.ctx.services.get("fixture.render")
        assert await service.call("render", {"source": actual[0].asset.model_dump()}) == {
            "error": "denied"
        }
        response = await service.call("render", {"source": profile.references[0].model_dump()})
        service_returned.set()
        await observed["inherited"]
        asset = AssetRef.model_validate(response["asset"])
        observed["returned"] = asset.asset_id
        assert asset.asset_id != observed["product"].asset_id
        assert observed["provider"] != observed["caller"]
        assert await caller.ctx.assets.read(asset.asset_id) == b"fictional product"
        with pytest.raises(KeyError):
            await provider.ctx.assets.read(observed["provider"])
        return ToolResult(
            call_id=plan.data["call_id"], status="ok", content="ready", data={}, assets=[asset]
        )

    caller.run_tool, provider.handle = compose, handle
    try:
        assert not hasattr(provider.ctx, "appearance")
        with pytest.raises(PermissionError):
            await caller.ctx.appearance.current()
        trace = await agent.receive(event(agent))
        await agent.drain()
        call = observed["call"]
        assert (call.trace_id, call.actor.actor_id, call.conversation.conversation_id) == (
            trace,
            "owner",
            "owner_private",
        )
        product = observed["product"].asset_id
        assert [
            part.asset.asset_id
            for intent in channel.sent
            for part in intent.parts
            if part.type == "image"
        ] == [product]
        assert agent.ledger.rows("invocation")[0]["asset_ids"] == [product]
        (model_usage,) = [
            r
            for r in agent.ledger.rows("audit")
            if r["kind"] == "model.usage" and r.get("plugin_purpose") == "fixture"
        ]
        assert model_usage["trace_id"] == trace and model_usage["plugin"] == "renderer"
        (row,) = [
            r
            for r in agent.ledger.rows("audit")
            if r["kind"] == "service.call" and r["status"] == "ok"
        ]
        assert (row["trace_id"], row["plugin"], row["usage"], row["asset_ids"]) == (
            trace,
            "renderer",
            {"input_tokens": 5},
            [product],
        )
        for ref in ("caller", "returned"):
            with pytest.raises(KeyError):
                await caller.ctx.assets.info(observed[ref])
    finally:
        service_returned.set()
        await finish(agent)


def invoke(agent, msg, token, *, proactive=False):
    inv = Invocation(
        companion_id=agent.cid,
        trace_id="1" * 32,
        actor=None if proactive else msg.message.sender,
        origin="proactive" if proactive else "message",
        conversation=msg.conversation,
    )
    plan = Plan(summary="fictional", heavy=True, data={})
    return agent.host.invoke(
        agent.cid, "composer", "tool", "compose_fixture", plan, inv, token, method="execute"
    )


async def test_proactive_service_uses_private_owner_scope_without_inventing_speaker(tmp_path):
    agent, _, caller, provider = await assembled(tmp_path)
    results, calls = [], []

    async def handle(method, args, call):
        calls.append(call)
        return {"ready": True}

    async def compose(plan, inv, cancel):
        results.append(await caller.ctx.services.get("fixture.render").call("render", {}))
        return ToolResult(call_id="test", status="ok", content="", data={})

    provider.handle, caller.run_tool = handle, compose
    try:
        for message in (event(agent), event(agent, scene="public_group")):
            await invoke(agent, message, CancelToken(), proactive=True)
        agent.protection.active = True
        await invoke(agent, event(agent), CancelToken(), proactive=True)
        agent.config["plugins"].pop("renderer")
        await invoke(agent, event(agent), CancelToken(), proactive=True)
        assert results == [
            {"ready": True},
            {"error": "denied"},
            {"error": "denied"},
            {"error": "denied"},
        ]
        assert len(calls) == 1 and calls[0].actor is None
    finally:
        await finish(agent)


async def test_service_denies_outside_tool_and_rechecks_actor_scope_and_protection(tmp_path):
    agent, _, caller, _ = await assembled(tmp_path)
    service = caller.ctx.services.get("fixture.render")
    member = event(agent, role="member", scene="public_group")

    async def compose(plan, inv, cancel):
        profile = await caller.ctx.appearance.current()
        assert not profile.description and not profile.references
        assert await service.call("render", {}) == {"error": "denied"}
        return ToolResult(call_id="test", status="denied", content="", data={})

    caller.run_tool = compose
    try:
        assert await service.call("render", {}) == {"error": "denied"}
        for mode in ("permission", "scope", "protection"):
            agent.config["permissions"]["member"] = (
                [] if mode == "permission" else ["render_fixture"]
            )
            agent.config["tool_scopes"] = (
                {"render_fixture": {"group": ["owner"]}} if mode == "scope" else {}
            )
            agent.protection.active = mode == "protection"
            result = await invoke(agent, member, CancelToken())
            assert result.status == "denied"
    finally:
        await finish(agent)


async def test_service_cancellation_and_child_task_expiration(tmp_path):
    add_references(tmp_path)
    agent, _, caller, provider = await assembled(tmp_path)
    entered, after = asyncio.Event(), asyncio.Event()
    tasks = []
    token = CancelToken()

    async def handle(method, args, call):
        async def inherited():
            await after.wait()
            with pytest.raises((KeyError, PermissionError, asyncio.CancelledError)):
                await provider.ctx.assets.read(args["ref"]["asset_id"])

        tasks.append(asyncio.create_task(inherited()))
        entered.set()
        await call.cancel.wait()
        call.cancel.raise_if_cancelled()

    async def compose(plan, inv, cancel):
        ref = (await caller.ctx.appearance.current()).references[0]
        await caller.ctx.services.get("fixture.render").call("render", {"ref": ref.model_dump()})

    caller.run_tool, provider.handle = compose, handle
    task = asyncio.create_task(invoke(agent, event(agent), token))
    try:
        waiter = asyncio.create_task(entered.wait())
        done, _ = await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            pytest.fail("service handler was not entered")
        token.cancel("fictional cancellation")
        with pytest.raises(asyncio.CancelledError):
            await task
        (row,) = [r for r in agent.ledger.rows("audit") if r["kind"] == "service.call"]
        assert (row["trace_id"], row["status"]) == ("1" * 32, "cancelled")
    finally:
        after.set()
        await asyncio.gather(*tasks)
        await finish(agent)
