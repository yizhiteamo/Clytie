"""撤销原文仅进入指定假向量模型，失败整段退为精确比对。"""

import asyncio
from types import SimpleNamespace

from clytie.memory.retracted import RetractedGuard
from clytie.memory.retrieval import Retrieval
from clytie.sdk import CancelToken
from clytie.sdk.types import RecollectItem

from .test_memory_store import store as store
from .test_memory_store import write


class Embedder:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.wait = None

    async def embed(self, texts, cancel):
        self.calls.append(list(texts))
        if self.wait:
            await self.wait.wait()
        if self.fail:
            raise ValueError("fake unavailable")
        return [[1, 0] for _ in texts]


def setup(store, embedder):
    record = write(store, "虚构的撤销内容")
    store.revise(
        record.record_id,
        record.revision,
        {"retracted": True},
        scene="owner_private",
        conversation_id="owner_chat",
        operator="owner",
    )
    records = [store.get_raw(record.record_id)]
    logs = []
    guard = RetractedGuard(
        store,
        Retrieval(store, embedder=embedder, embedder_id="fake"),
        SimpleNamespace(warning=lambda *args: logs.append(args)),
    )
    items = [
        RecollectItem(kind="episode", content="虚构的撤销内容", evidence=[1]),
        RecollectItem(kind="episode", content="不同的虚构内容", evidence=[1]),
    ]
    return guard, records, items, logs


async def test_ephemeral_vectors_semantic_rejection_and_no_cache(store):
    embedder = Embedder()
    guard, records, items, logs = setup(store, embedder)
    assert await guard.rejected(items, records, CancelToken()) == {0, 1}
    assert embedder.calls[0] == [records[0].content]
    assert store.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    assert not logs


async def test_no_embedder_and_failure_fallback_hourly_warning(store):
    guard, records, items, logs = setup(store, None)
    for _ in range(2):
        assert await guard.rejected(items, records, CancelToken()) == {0}
    assert store.meta("recollect_semantic_failures") == 2
    assert len(logs) == 1
    embedder = Embedder()
    embedder.fail = True
    guard.retrieval.embedder = embedder
    store.clock.advance(3600)
    assert await guard.rejected(items, records, CancelToken()) == {0}
    assert len(logs) == 2
    assert all(records[0].content not in str(entry) for entry in logs)


async def test_timeout_uses_mock_clock_and_returns_exact_only(store):
    embedder = Embedder()
    embedder.wait = asyncio.Event()
    guard, records, items, logs = setup(store, embedder)
    task = asyncio.create_task(guard.rejected(items, records, CancelToken()))
    await store.clock.drain()
    await store.clock.advance_and_drain(30)
    assert await task == {0}
    assert len(logs) == 1


async def test_matching_cache_reused_other_identity_never_used(store):
    from clytie.memory.store import canonical

    embedder = Embedder()
    guard, records, items, _ = setup(store, embedder)
    record = records[0]
    with store.transaction():
        store.db.execute(
            "INSERT INTO vectors VALUES(?,?,?,?)", (record.record_id, 1, "other", canonical([0, 1]))
        )
        store.db.execute(
            "INSERT INTO vectors VALUES(?,?,?,?)", (record.record_id, 1, "fake", canonical([1, 0]))
        )
    assert await guard.rejected(items, records, CancelToken()) == {0, 1}
    assert embedder.calls == [[item.content for item in items]]
    guard.retrieval.embedder_id = "new_identity"
    embedder.calls.clear()
    await guard.rejected(items, records, CancelToken())
    assert embedder.calls[0] == [record.content]
    assert store.db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 2
