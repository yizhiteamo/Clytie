"""Issue #37：用真实关键词索引和假重排验证名次/淡化组合。"""

import pytest

from clytie.memory.retrieval import Retrieval

from .test_memory_store import store as store


class Reranker:
    def __init__(self, scores):
        self.scores, self.seen = scores, []

    async def rerank(self, query, records, cancel):
        self.seen = records
        return self.scores


def records(store, count):
    result = []
    for index in range(count):
        result.append(
            store.write(
                dict(kind="episode", content=f"纸船活动 {index}", visibility="relationship"),
                scene="public_group",
                conversation_id="group_a",
            )
        )
        store.clock.advance(1)
    return result


def age(store, record, days):
    from datetime import timedelta

    with store.transaction():
        store.db.execute(
            "INSERT OR REPLACE INTO fade VALUES(?,?,?)",
            (record.record_id, (store.clock.now() - timedelta(days=days)).isoformat(), 30),
        )


@pytest.mark.parametrize("scores", [[2, 1], [-1, -2], [0, -1]])
async def test_raw_candidate_cutoff_signed_ranks_and_tail(store, scores):
    rows = records(store, 3)
    age(store, rows[2], 30)
    store.config["memory"]["rerank_candidates"] = 2
    reranker = Reranker(scores)
    result = await Retrieval(store, reranker=reranker).search("纸船", "public_group", "group_a")
    assert [r.record_id for r in reranker.seen] == [rows[2].record_id, rows[1].record_id]
    assert [r.record_id for r in result] == [
        rows[1].record_id,
        rows[2].record_id,
        rows[0].record_id,
    ]


async def test_competition_ties_use_same_rank_then_basis_and_newest(store):
    rows = records(store, 4)
    # 同分中靠后的候选稍新鲜，应按共同名次后的淡化系数超过前一个。
    age(store, rows[2], 0.2)
    age(store, rows[1], 0)
    reranker = Reranker([3, 2, 2, 1])
    result = await Retrieval(store, reranker=reranker).search("纸船", "public_group", "group_a")
    assert [r.record_id for r in result] == [
        rows[3].record_id,
        rows[1].record_id,
        rows[2].record_id,
        rows[0].record_id,
    ]
    with store.transaction():
        store._save(
            rows[0].model_copy(update={"revision": 2, "basis": "owner_confirmed"}), operator="owner"
        )
    result = await Retrieval(store, reranker=Reranker([1, 1, 1, 1])).search(
        "纸船", "public_group", "group_a"
    )
    assert result[0].record_id == rows[0].record_id
    assert result[1].record_id == rows[1].record_id


@pytest.mark.parametrize("scores", [[1], [float("nan"), 1], [float("inf"), 1], [-float("inf"), 1]])
async def test_invalid_reranker_falls_back_to_all_rrf_times_fade(store, scores):
    rows = records(store, 3)
    age(store, rows[2], 30)
    store.config["memory"]["rerank_candidates"] = 2
    expected = await Retrieval(store).search("纸船", "public_group", "group_a")
    actual = await Retrieval(store, reranker=Reranker(scores)).search(
        "纸船", "public_group", "group_a"
    )
    assert [r.record_id for r in actual] == [r.record_id for r in expected]
    assert actual[-1].record_id == rows[2].record_id
