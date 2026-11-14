"""真实专用存储线程与假检索插件的异步边界回归。"""

import asyncio
import threading

import pytest

from clytie.backup.coordination import run_sync
from clytie.memory.retrieval import CandidateReader, Retrieval
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MemoryHit
from clytie.storage.handle import StoreHandle
from clytie.storage.owner import StorageBusy, StorageCancelled, StorageFailed, StorageOwner


@pytest.fixture
async def owned_memory(tmp_path):
    clock = MockClock()
    config = {"memory": {"private_scenes": ["owner_private"]}}
    instance = {
        "conversations": {
            "owner_chat": {
                "kind": "private",
                "scene": "owner_private",
                "channel": "fake-channel",
                "platform_id": "fake-owner",
            }
        }
    }
    path = tmp_path / "memory.sqlite3"

    class Resources:
        def __init__(self):
            self.memory = MemoryStore(path, clock=clock, config=config, instance=instance)

        def close(self):
            self.memory.close()

    owner = await StorageOwner.start(Resources, companion_id="companion_a", clock=clock)
    handle = StoreHandle(
        owner,
        "memory",
        MemoryStore,
        {"config": config, "instance": instance, "clock": clock, "path": path},
    )
    try:
        yield handle
    finally:
        await owner.close()


async def remember(store, text, visibility="public"):
    return await run_sync(
        store,
        store.write,
        {"kind": "fact", "content": text, "visibility": visibility},
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
        owner="owner",
    )


async def test_candidate_reader_projects_and_materializes_inside_owner(owned_memory):
    store = owned_memory
    public = await remember(store, "虚构纸船")
    private = await remember(store, "虚构私密纸船", "private")
    reader = CandidateReader(store, "public_group", "group_a")
    assert await reader.get([public.record_id, private.record_id]) == [public]
    assert await reader.count() == 1
    assert [batch async for batch in reader.iter(batch=1)] == [[public]]
    hits = [MemoryHit(record_id=row.record_id, score=1, source="fake") for row in (public, private)]
    assert await reader.filter_hits(hits) == hits[:1]
    assert await CandidateReader(store, "stranger_private", "unknown").count() == 0


async def test_keyword_search_and_degraded_audit_use_owned_commands(owned_memory):
    store = owned_memory
    record = await remember(store, "虚构纸船")
    retrieval = Retrieval(store)
    assert await retrieval.search("纸船", "public_group", "group_a") == [record]
    assert await retrieval.search("纸船", "public_group", "group_a") == [record]
    assert retrieval.warnings
    audits = await run_sync(
        store,
        lambda: store.db.execute(
            "SELECT count(*) FROM audit WHERE data LIKE '%memory_embedder_unavailable%'"
        ).fetchone()[0],
    )
    assert audits == 1


async def test_rebuild_semantic_and_rerank_keep_plugins_on_event_loop(owned_memory):
    store = owned_memory
    record = await remember(store, "虚构纸船")
    await remember(store, "私密纸船", "private")
    main = threading.get_ident()
    calls = []

    class Embedder:
        @property
        def cache_identity(self):
            assert threading.get_ident() == main
            return "fake_embedding_v1"

        async def embed(self, texts, cancel):
            assert threading.get_ident() == main
            calls.append(("embed", list(texts)))
            return [[1.0, 0.0] for _ in texts]

    class Reranker:
        async def rerank(self, query, records, cancel):
            assert threading.get_ident() == main
            assert records == [record]
            calls.append(("rerank", query))
            return [1.0]

    retrieval = Retrieval(store, embedder=Embedder(), reranker=Reranker())
    await retrieval.rebuild()
    assert retrieval.progress == {"complete": 2, "total": 2}
    assert await run_sync(store, store.meta, "embedding_progress") == retrieval.progress
    assert await retrieval.search("没有关键词", "public_group", "group_a") == [record]
    assert any(kind == "rerank" for kind, _ in calls)
    assert not retrieval.warnings


@pytest.mark.parametrize("change", ["revision", "identity"])
async def test_rebuild_rechecks_revision_and_embedding_identity(owned_memory, change):
    store = owned_memory
    record = await remember(store, "虚构纸船")
    entered, release = asyncio.Event(), asyncio.Event()
    main = threading.get_ident()

    class Embedder:
        identity = "fake_v1"

        @property
        def cache_identity(self):
            assert threading.get_ident() == main
            return self.identity

        async def embed(self, texts, cancel):
            entered.set()
            await release.wait()
            return [[1.0, 0.0]]

    embedder = Embedder()
    retrieval = Retrieval(store, embedder=embedder)
    task = asyncio.create_task(retrieval.rebuild())
    try:
        # Fail promptly if an access escaped its owner before the fake plugin call.
        reached = asyncio.create_task(entered.wait())
        await asyncio.wait((task, reached), return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            await task
        assert entered.is_set()
        if change == "revision":
            await run_sync(
                store,
                store.revise,
                record.record_id,
                record.revision,
                {"content": "修订的虚构纸船"},
                scene="owner_private",
                conversation_id="owner_chat",
                operator="owner",
            )
        else:
            embedder.identity = "fake_v2"
        release.set()
        await task
        assert (
            await run_sync(
                store, lambda: store.db.execute("SELECT count(*) FROM vectors").fetchone()[0]
            )
            == 0
        )
    finally:
        release.set()
        reached.cancel()
        await asyncio.gather(task, reached, return_exceptions=True)


@pytest.mark.parametrize("error_type", [StorageBusy, StorageCancelled, StorageFailed])
@pytest.mark.parametrize("operation", ["search", "rebuild"])
async def test_owner_control_failure_is_not_reported_as_embedding_degradation(
    owned_memory, monkeypatch, error_type, operation
):
    store = owned_memory
    await remember(store, "虚构纸船")

    class Embedder:
        async def embed(self, texts, cancel):
            return [[1.0, 0.0] for _ in texts]

    retrieval = Retrieval(store, embedder=Embedder(), embedder_id="fake")

    def fail(*args):
        raise error_type()

    monkeypatch.setattr(
        retrieval, "_vector_rows" if operation == "search" else "_save_vector", fail
    )
    with pytest.raises(error_type):
        if operation == "search":
            await retrieval.search("纸船", "owner_private", "owner_chat")
        else:
            await retrieval.rebuild()
    assert not retrieval.warnings
    assert not await run_sync(store, store.meta, "embedder_unavailable", False)


async def test_successful_search_does_not_write_unchanged_health_metadata(owned_memory):
    store = owned_memory
    await remember(store, "虚构纸船")

    class Embedder:
        async def embed(self, texts, cancel):
            return [[1.0, 0.0] for _ in texts]

    retrieval = Retrieval(store, embedder=Embedder(), embedder_id="fake")
    await retrieval.rebuild()
    generation = await run_sync(store, store.meta, "generation")
    assert await retrieval.search("纸船", "owner_private", "owner_chat")
    assert await run_sync(store, store.meta, "generation") == generation
