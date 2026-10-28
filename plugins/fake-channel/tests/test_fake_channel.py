from pathlib import Path

from clytie.sdk.testing import MockClock, assert_channel_contract
from clytie.sdk.testing.lifecycle import plugin_from_manifest
from clytie.sdk.testing.samples import sample_event, sample_intent


async def test_reference_channel_contract(tmp_path):
    clock = MockClock()
    async with plugin_from_manifest(
        Path(__file__).parents[1] / "plugin.json", clock=clock, root=tmp_path
    ) as loaded:
        channel = loaded.factory("channel")
        assert hasattr(channel.ctx, "identity")
        await assert_channel_contract(
            channel,
            sample_intent(),
            sample_intent(react=True),
            expected_events=(sample_event(clock),),
        )
        assert not channel.connected
        assert len(channel.sent) == 2
    assert loaded.plugin.ctx is None
