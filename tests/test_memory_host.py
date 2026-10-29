"""新扩展经清单、构造、start、factory 装配；记忆来自真实本体存储。"""

from clytie.memory.retrieval import HostExtension
from clytie.sdk import CancelToken
from clytie.sdk.testing import assert_reranker_contract
from clytie.sdk.testing.samples import sample_event, sample_memory

from .host_helpers import SOURCE, enable, host, plugin


async def test_reranker_manifest_lifecycle_contract(tmp_path):
    source = (
        SOURCE
        + """
    def make_reranker(self):
        from clytie.sdk.testing import FakeReranker
        return FakeReranker(self.ctx)
"""
    )
    plugin(
        tmp_path,
        "fake-reranker",
        source,
        contributes={"memory": {"reranker": {"factory": "make_reranker"}}},
    )
    h = host(tmp_path)
    await enable(h, "fake-reranker")
    reg = h.registrations("companion_a", "memory.reranker")[0]
    assert reg.plugin_id == "fake-reranker"
    await assert_reranker_contract(reg.implementation, "fictional", [sample_memory(h.clock)])
    wrapper = HostExtension(h, "companion_a", reg)
    assert await wrapper.rerank("fictional", [sample_memory(h.clock)], CancelToken()) == [1.0]
    await h.close()


async def test_behavior_memory_origin_comes_from_host_bound_conversation(tmp_path):
    source = (
        SOURCE
        + """
    async def remember(self, event):
        from clytie.sdk.types import MemoryDraft
        self.seen.append(await self.ctx.memory.write(MemoryDraft(kind="fact", content="fictional group observation", visibility="public", source_type="private")))
"""
    )
    plugin(
        tmp_path,
        "fake-memory-observer",
        source,
        permissions=["memory.write"],
        contributes={"behavior": {"message_received": {"handler": "remember"}}},
    )
    h = host(tmp_path)
    incoming = sample_event(h.clock)
    cid = incoming.conversation.conversation_id
    store = h.memory_store(
        "companion_a",
        {"names": {"internal_scenes": ["owner_private"]}},
        {"conversations": {cid: {"scene": incoming.conversation.scene, "kind": "group"}}},
    )
    await enable(h, "fake-memory-observer")
    await h.behavior("companion_a", "message_received", incoming)
    record = store.all_records()[0]
    assert record.origin_conversation_id == cid and record.basis == "observed"
    assert store.project("public_group", "different_group") == []
    assert store.project("owner_private", "owner_chat") == [record]
    await h.close()


async def test_importer_host_validates_items_and_times_out_with_mock_clock(tmp_path):
    import asyncio

    import pytest

    from clytie.plugin_host.host import PluginFailure

    plugin(
        tmp_path,
        "fake-importer",
        SOURCE
        + """
    def make_importer(self):
        return self
    def detect(self, source):
        return True
    async def read(self, source, cancel):
        if source == "slow":
            await self.ctx.clock.sleep(100)
        yield {"not": "an SDK candidate"}
""",
        contributes={
            "memory": {"importer": {"factory": "make_importer", "formats": ["fictional"]}}
        },
    )
    h = host(tmp_path)
    await enable(h, "fake-importer")
    with pytest.raises(PluginFailure):
        await h.invoke(
            "companion_a",
            "fake-importer",
            "memory.importer",
            "importer",
            "invalid",
            CancelToken(),
            method="read",
        )
    task = asyncio.create_task(
        h.invoke(
            "companion_a",
            "fake-importer",
            "memory.importer",
            "importer",
            "slow",
            CancelToken(),
            method="read",
        )
    )
    for _ in range(10):
        await h.clock.sleep(0)
    h.clock.advance(30)
    with pytest.raises(PluginFailure):
        await task
    assert h.failures
    await h.close()
