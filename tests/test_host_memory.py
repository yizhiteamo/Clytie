"""方法权限和每次调用场景由宿主绑定，不由插件选择。"""

import asyncio

import pytest

from clytie.plugin_host.memory import bind_scene, memory_view
from clytie.sdk.testing import MockClock, checkpoint
from clytie.sdk.testing.context_fakes import FakeWritableMemory
from clytie.sdk.testing.samples import sample_memory
from clytie.sdk.types import MemoryDraft

from .host_helpers import SOURCE, enable, host, plugin


def backend(clock):
    public = sample_memory(clock)
    private = public.model_copy(update={"record_id": "a" * 32, "visibility": "private"})
    return FakeWritableMemory(clock, [public, private], private=True, scene="owner_private")


@pytest.mark.parametrize(
    ("permissions", "methods"),
    [
        ([], set()),
        (["memory.write"], {"write"}),
        (["memory.read.public"], {"search", "get"}),
        (["memory.read.private"], {"search", "get"}),
        (["memory.read.public", "memory.write"], {"search", "get", "write", "revise", "retract"}),
    ],
)
async def test_context_contains_only_authorized_methods(tmp_path, permissions, methods):
    plugin(tmp_path, "reader", permissions=permissions)
    clock = MockClock()
    h = host(tmp_path, clock=clock, backends=lambda cid, pid: {"memory": backend(clock)})
    runtime = await enable(h, "reader")
    for name in (
        "http",
        "subprocess",
        "model",
        "peer",
        "proactive",
        "asset_writer",
        "identity",
        "services",
    ):
        assert not hasattr(runtime.ctx, name)
        with pytest.raises(AttributeError):
            getattr(runtime.ctx, name)
    assert hasattr(runtime.ctx, "memory") == bool(permissions)
    if permissions:
        for method in ("search", "get", "write", "revise", "retract"):
            assert hasattr(runtime.ctx.memory, method) == (method in methods)
        if methods == {"write"}:
            from clytie.sdk.testing import assert_memory_insert_contract

            await assert_memory_insert_contract(
                runtime.ctx.memory,
                MemoryDraft(
                    kind="fact", content="fictional", visibility="public", source_type="group"
                ),
            )
    await h.close()


async def test_private_memory_filters_reads_and_mutations_by_bound_scene():
    clock = MockClock()
    store = backend(clock)
    view = memory_view(store, ["memory.read.private", "memory.write"], ["owner_private", "family"])
    for scene, visible in [
        ("public_group", False),
        ("stranger_private", False),
        ("owner_private", True),
        ("family", True),
    ]:
        with bind_scene(scene):
            assert (await view.get("a" * 32) is not None) == visible
            assert any(r.visibility == "private" for r in await view.search("")) == visible
            with pytest.raises(TypeError):
                await view.search("", scene="owner_private")
            if not visible:
                with pytest.raises(KeyError):
                    await view.revise("a" * 32, 1, {"content": "fictional-changed"})
                with pytest.raises(KeyError):
                    await view.retract("a" * 32, 1, "fictional")
    with pytest.raises(AttributeError):
        view.scene = "owner_private"
    public_only = memory_view(store, ["memory.read.public"])
    with bind_scene("owner_private"):
        assert await public_only.get("a" * 32) is None
    write_only = memory_view(store, ["memory.write"])
    written = await write_only.write(
        MemoryDraft(kind="fact", content="fictional", visibility="private", source_type="private")
    )
    assert written.write_mode == "auto" and not hasattr(write_only, "get")


async def test_concurrent_calls_and_child_tasks_do_not_retain_private_scene(tmp_path):
    source = (
        SOURCE
        + """
    async def service(self, method, args):
        return {"private": await self.ctx.memory.get("a" * 32) is not None}
    async def before_attention(self, input):
        import asyncio
        async def later():
            await self.ctx.clock.sleep(1)
            return await self.ctx.memory.get("a" * 32)
        self.background = asyncio.create_task(later())
        await self.ctx.clock.sleep(0)
        self.visible = await self.ctx.memory.get("a" * 32) is not None
"""
    )
    plugin(
        tmp_path,
        "reader",
        source,
        permissions=["memory.read.private"],
        contributes={
            "behavior": {"before_attention": {"handler": "before_attention"}},
            "services": [
                {"name": "reader.state", "version": "1.0", "methods": ["get"], "handler": "service"}
            ],
        },
    )
    clock = MockClock()
    h = host(tmp_path, clock=clock, backends=lambda cid, pid: {"memory": backend(clock)})
    runtime = await enable(h, "reader")
    await h.invoke(
        "companion_a",
        "reader",
        "behavior.before_attention",
        "before_attention",
        None,
        scene="owner_private",
    )
    assert runtime.plugin.visible
    assert await h.invoke(
        "companion_a", "reader", "service", "reader.state", "get", {}, scene="owner_private"
    ) == {"private": False}
    for _ in range(4):
        await checkpoint()
    clock.advance(1)
    assert await runtime.plugin.background is None

    async def observe(scene):
        with bind_scene(scene):
            await clock.sleep(0)
            return await runtime.ctx.memory.get("a" * 32)

    private, public = await asyncio.gather(observe("owner_private"), observe("public_group"))
    assert private is not None and public is None
    await h.close()
