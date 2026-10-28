from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from clytie.sdk.testing import FakeContext, MockClock, assert_model_contract
from clytie.sdk.testing.samples import sample_request


async def test_reference_model_contract():
    spec = spec_from_file_location("reference_fake_model", Path(__file__).parents[1] / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = MockClock()
    model = module.Plugin(FakeContext(plugin_id="fake-model", clock=clock))
    await assert_model_contract(model, sample_request(clock))
    assert len(model.requests) == 1
    await model.quota()
    assert len(model.requests) == 1
