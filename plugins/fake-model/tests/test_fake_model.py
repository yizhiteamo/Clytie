from pathlib import Path

from clytie.sdk.testing import MockClock, assert_model_contract
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.testing.samples import sample_request


async def test_reference_model_contract(tmp_path):
    clock = MockClock()
    async with plugin_from_manifest(
        Path(__file__).parents[1] / "plugin.json", clock=clock, root=tmp_path
    ) as loaded:
        model = loaded.factory("model")
        await assert_model_contract(model, sample_request(clock))
        assert len(model.requests) == 1
        await model.quota()
        assert len(model.requests) == 1
    assert loaded.plugin.ctx is None
