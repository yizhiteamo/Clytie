"""假向量与重排：所有候选先经过场景投影。"""

import pytest

from clytie.memory.retrieval import CandidateReader, Retrieval
from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MemoryHit


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(
        tmp_path / "memory.sqlite3",
        clock=MockClock(),
        config={"memory": {"private_scenes": ["owner_private"]}},
        instance={"conversations": {"owner_chat": {"kind": "private", "scene": "owner_private"}}},
    )
    yield store
    store.close()


def remember(store, text, *, explicit=False, visibility="public"):
    return store.write(
        {"kind": "fact", "content": text, "visibility": visibility},
        scene="owner_private",
        conversation_id="owner_chat",
        owner="owner",
        operator="owner",
        explicit=explicit,
    )


class FakeEmbedder:
    dimension = 2

    async def embed(self, texts, cancel):
        cancel.raise_if_cancelled()
        return [[1.0, 0.0] if "面" in t or "餐馆" in t else [0.0, 1.0] for t in texts]


async def test_chinese_keywords_and_equal_relevance_basis_priority(memory):
    low = remember(memory, "香菜甲")
    high = remember(memory, "香菜乙", explicit=True)
    retrieval = Retrieval(memory)
    results = await retrieval.search("香菜", "owner_private", "owner_chat")
    assert [r.record_id for r in results] == [high.record_id, low.record_id]
    assert retrieval.warnings
    assert (
        memory.db.execute(
            "SELECT count(*) FROM audit WHERE data LIKE '%memory_embedder_unavailable%'"
        ).fetchone()[0]
        == 1
    )
    await retrieval.search("香菜", "owner_private", "owner_chat")
    memory.clock.advance(3600)
    await retrieval.search("香菜", "owner_private", "owner_chat")
    assert (
        memory.db.execute(
            "SELECT count(*) FROM audit WHERE data LIKE '%memory_embedder_unavailable%'"
        ).fetchone()[0]
        == 2
    )


async def test_semantics_model_change_and_rebuild(memory):
    target = remember(memory, "拉面太咸了")
    remember(memory, "虚构的天空")
    retrieval = Retrieval(memory, embedder=FakeEmbedder(), embedder_id="fake_a")
    assert await retrieval.search("餐馆", "owner_private", "owner_chat") == []
    await retrieval.rebuild()
    first = await retrieval.search("餐馆", "owner_private", "owner_chat", limit=1)
    assert first[0].record_id == target.record_id
    assert first == await retrieval.search("餐馆", "owner_private", "owner_chat", limit=1)
    retrieval.embedder_id = "fake_b"
    assert await retrieval.search("餐馆", "owner_private", "owner_chat") == []
    await retrieval.rebuild()
    assert (await retrieval.search("餐馆", "owner_private", "owner_chat", limit=1))[
        0
    ].record_id == target.record_id
    assert retrieval.progress == {"complete": 2, "total": 2}


async def test_failure_degrades_and_reranker_never_receives_private_record(memory):
    private_text = remember(memory, "虚构的私密拉面", visibility="private")
    public = remember(memory, "公开拉面")
    seen = []

    class Reranker:
        async def rerank(self, query, records, cancel):
            seen.extend(records)
            raise ValueError("fictional failure")

    class Broken:
        async def embed(self, texts, cancel):
            raise ValueError("fictional failure")

    retrieval = Retrieval(memory, embedder=Broken(), embedder_id="broken", reranker=Reranker())
    results = await retrieval.search("拉面", "public_group", "group_a")
    assert results == [public] and seen == [public]
    candidates = CandidateReader(memory, "public_group", "group_a")
    assert await candidates.get([private_text.record_id]) == []
    assert await candidates.count() == 1
    assert [r async for r in candidates.iter()] == [[public]]
    hits = [
        MemoryHit(record_id=r.record_id, score=1, source="fake") for r in (private_text, public)
    ]
    assert await candidates.filter_hits(hits) == hits[1:]
    assert await CandidateReader(memory, "stranger_private", "unknown").count() == 0
