from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from clytie.sdk.testing import FakeContext, MockClock, assert_channel_contract
from clytie.sdk.testing.samples import sample_event, sample_intent


async def test_reference_channel_contract():
    spec = spec_from_file_location(
        "reference_fake_channel", Path(__file__).parents[1] / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = MockClock()
    event = sample_event(clock)
    channel = module.Plugin(FakeContext(plugin_id="fake-channel", clock=clock), incoming=(event,))
    await assert_channel_contract(
        channel, sample_intent(), sample_intent(react=True), expected_events=(event,)
    )
    assert not channel.connected
    assert len(channel.sent) == 2
