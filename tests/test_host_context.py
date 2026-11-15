"""实际宿主上下文通过 SDK 契约，所有外部能力均由假后端注入。"""

from types import SimpleNamespace

import pytest

from clytie.sdk.testing import FakeContext, MockClock, assert_plugin_context_contract
from clytie.sdk.testing import context_contracts as checks
from clytie.sdk.testing.samples import sample_memory, sample_request
from clytie.sdk.types import MemoryDraft, TextPart

from .host_helpers import SOURCE, enable, host, plugin


async def test_host_context_contracts_with_injected_backends_only(tmp_path):
    clock = MockClock()
    permissions = [
        "assets.write",
        "memory.read.private",
        "memory.write",
        "peer",
        "model.invoke",
        "network",
        "message.proactive",
        "subprocess",
        "gpu",
    ]
    public = sample_memory(clock)
    private = public.model_copy(update={"record_id": "a" * 32, "visibility": "private"})
    fake = FakeContext(
        root=tmp_path / "fake",
        clock=clock,
        permissions=permissions,
        records=[public, private],
        scene="owner_private",
        channel=True,
    )

    async def run(args, **kwargs):
        assert kwargs.pop("shell") is False
        assert kwargs.pop("cwd").is_relative_to(tmp_path)
        return await fake.subprocess.run(args, **kwargs)

    injected = {
        name: getattr(fake, name)
        for name in (
            "assets",
            "asset_writer",
            "memory",
            "peer",
            "model",
            "http",
            "websocket",
            "proactive",
            "identity",
        )
    }
    injected["subprocess"] = SimpleNamespace(run=run)
    provider_source = (
        SOURCE + '\n    async def service(self, method, args):\n        return {"ready": True}\n'
    )
    plugin(
        tmp_path,
        "provider",
        provider_source,
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
        "all-context",
        permissions=permissions,
        dependencies={"provider": {"version": ">=1"}},
        contributes={"events": {"publish": ["all-context.changed"]}},
    )
    h = host(tmp_path, clock=clock, backends=lambda cid, pid: injected)
    runtime = await enable(h, "all-context")
    ctx = runtime.ctx
    assert_plugin_context_contract(
        ctx,
        granted=(
            "asset_writer",
            "memory",
            "services",
            "peer",
            "model",
            "http",
            "websocket",
            "proactive",
            "subprocess",
        ),
    )
    checks.assert_logger_contract(ctx.log)
    from clytie.sdk.testing import assert_clock_contract

    await assert_clock_contract(ctx.clock)
    await checks.assert_storage_contract(ctx.storage)
    await checks.assert_events_contract(ctx.events, ctx.plugin_id)
    await checks.assert_state_contract(
        ctx.state, lambda: h.companion_state("companion_a"), lambda: h.events, ctx.plugin_id
    )
    ref = await checks.assert_asset_writer_contract(ctx.asset_writer)
    await checks.assert_assets_contract(ctx.assets, ref, b"fictional")
    await checks.assert_memory_view_contract(ctx.memory, record=public, hidden_id="a" * 32)
    await checks.assert_memory_writer_contract(
        ctx.memory,
        MemoryDraft(kind="fact", content="fictional", visibility="public", source_type="group"),
    )
    assert await ctx.services.get("example.state").call("get", {}) == {"ready": True}
    await checks.assert_peer_contract(ctx.peer)
    await checks.assert_model_invoker_contract(ctx.model, sample_request(clock))
    await checks.assert_http_contract(ctx.http)
    await checks.assert_proactive_contract(ctx.proactive, [TextPart(text="fictional")])
    await checks.assert_subprocess_contract(ctx.subprocess)
    assert not hasattr(ctx.assets, "put") and not hasattr(ctx, "identity")
    # F5：公开对象不能泄漏整个宿主、原始记忆、全局日志或可修改的属性字典。
    for target in (
        ctx,
        ctx.events,
        ctx.state,
        ctx.services,
        ctx.memory,
        ctx.log,
        ctx.storage,
        ctx.assets,
        ctx.http,
        ctx.subprocess,
        ctx.services.get("example.state"),
    ):
        for attribute in ("_host", "_backend", "entries", "__dict__", "_proxies"):
            with pytest.raises(AttributeError):
                getattr(target, attribute)
    assert not hasattr(ctx.events.publish, "__self__")
    with pytest.raises(AttributeError):
        ctx.memory._backend = fake.memory
    assert await ctx.memory.get(private.record_id) is None
    await h.close()
