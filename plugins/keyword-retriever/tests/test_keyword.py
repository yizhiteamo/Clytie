from pathlib import Path

from clytie.sdk import CancelToken
from clytie.sdk.testing import MockClock, assert_retriever_contract
from clytie.sdk.testing.context_fakes import FakeCandidateReader
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.testing.samples import sample_memory, sample_query


async def test_keyword_contract_and_visible_candidates_only(tmp_path):
    clock = MockClock()
    record = sample_memory(clock)
    private = record.model_copy(update={"record_id": "d" * 32, "visibility": "private"})
    reader = FakeCandidateReader([record, private], allowed_visibility=["public"], clock=clock)
    async with plugin_from_manifest(
        Path(__file__).parents[1] / "plugin.json", clock=clock, root=tmp_path
    ) as loaded:
        retriever = loaded.factory("memory.retriever")
        query = sample_query()
        await assert_retriever_contract(retriever, query, reader)
        hits = await retriever.search(query, reader, CancelToken())
        assert [hit.record_id for hit in hits] == [record.record_id]
        assert hits[0].score == 1
        assert (
            await retriever.search(query.model_copy(update={"text": ""}), reader, CancelToken())
            == []
        )
